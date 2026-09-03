"""Inverter poller and dashboard server.

Acts as the "edge gateway" from the API overview document: it is the Modbus
master/client - the inverter never volunteers data, so this process asks for
it on an interval (FC 04, Read Input Registers), applies the scaling factors
from register_map.json to turn raw 16-bit registers into engineering units,
then fans the data out three ways:

  - stores it in SQLite               (database.py  - persistent history)
  - publishes it to an MQTT broker    (mqtt_publisher.py - pub/sub push)
  - serves a live web dashboard + JSON API:
        GET /              dashboard
        GET /api/live      latest decoded reading
        GET /api/history   rolling buffer of decoded readings (from SQLite)

The Modbus connection is chosen by config.json's inverter.mode:
  "tcp"    -> ModbusTcpClient  (networked inverter, or the simulator)
  "serial" -> ModbusSerialClient (RS-485 via a USB adapter)
so switching to real hardware is a config change, not a code change.

Run:  python3 poller/poller.py
"""

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from database import ReadingStore
from mqtt_publisher import MqttPublisher

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
_MAP = json.loads((ROOT / "register_map.json").read_text())
REGISTER_MAP = _MAP["registers"]
CONTROLS = _MAP.get("controls", [])            # writable holding registers
CONTROL_BY_NAME = {c["name"]: c for c in CONTROLS}

READ_START = min(r["address"] for r in REGISTER_MAP)
READ_COUNT = max(r["address"] + r["words"] for r in REGISTER_MAP) - READ_START

# In-memory fallback history, used when the SQLite store is disabled.
history = deque(maxlen=CONFIG["poller"]["history_samples"])
latest = {"connected": False}
controls_latest = {}   # last-read control settings, for the dashboard/API
lock = threading.Lock()

# One Modbus client is shared by the poll loop (reads) and command handling
# (writes); client_lock serializes access so the two threads never overlap on
# the wire. A Modbus transaction must complete before the next one starts.
client = None
client_lock = threading.Lock()

store = None      # ReadingStore, set up in main() if database.enabled
mqtt_pub = None   # MqttPublisher, set up in main()


def build_client():
    """Create the right Modbus client for the configured connection mode."""
    inv = CONFIG["inverter"]
    if inv.get("mode", "tcp") == "serial":
        s = inv["serial"]
        logging.info("Modbus RTU (serial) on %s @ %s baud", s["port"], s["baudrate"])
        return ModbusSerialClient(
            port=s["port"], baudrate=s["baudrate"], parity=s["parity"],
            stopbits=s["stopbits"], bytesize=s["bytesize"], timeout=3,
        )
    logging.info("Modbus TCP to %s:%s", inv["host"], inv["port"])
    return ModbusTcpClient(inv["host"], port=inv["port"], timeout=3)


def decode(words):
    """Turn a raw register block into scaled engineering values."""
    out = {}
    for reg in REGISTER_MAP:
        idx = reg["address"] - READ_START
        if reg["words"] == 2:
            raw = (words[idx] << 16) | words[idx + 1]
        else:
            raw = words[idx]
            if reg["signed"] and raw >= 0x8000:
                raw -= 0x10000
        value = raw * reg["scale"]
        out[reg["name"]] = round(value, 3) if reg["scale"] < 1 else value
    return out


def store_reading(sample):
    """Save a good reading to SQLite (if enabled) and the in-memory buffer."""
    if store is not None:
        try:
            store.insert(sample)
        except Exception as exc:
            logging.warning("DB insert failed: %s", exc)
    with lock:
        history.append(sample)
        latest.clear()
        latest.update(sample, connected=True)


def _ensure_client():
    """Return the shared Modbus client, building and connecting it on demand.
    The caller must hold client_lock."""
    global client
    if client is None:
        client = build_client()
    if not client.connected:
        client.connect()
    return client


def _reset_client():
    """Drop the shared client after an error so the next call rebuilds it."""
    global client
    with client_lock:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            client = None


def read_controls():
    """Read the inverter's current control settings (FC 03 Read Holding
    Registers) and return them as scaled {name: value}."""
    if not CONTROLS:
        return {}
    inv = CONFIG["inverter"]
    start = min(c["address"] for c in CONTROLS)
    count = max(c["address"] + c["words"] for c in CONTROLS) - start
    with client_lock:
        c = _ensure_client()
        rr = c.read_holding_registers(start, count=count, device_id=inv["device_id"])
    if rr.isError():
        raise IOError(f"control read error: {rr}")
    out = {}
    for ctl in CONTROLS:
        raw = rr.registers[ctl["address"] - start]
        if ctl.get("signed") and raw >= 0x8000:
            raw -= 0x10000
        value = raw * ctl["scale"]
        out[ctl["name"]] = round(value, 3) if ctl["scale"] < 1 else value
    return out


def write_controls(updates):
    """Apply {name: value} settings to the inverter with Modbus writes (FC 06
    Preset Single Register) - the "Cloud Control" path. Unknown names are
    ignored and out-of-range values are clamped to register_map.json limits.
    Returns the dict of values actually written."""
    inv = CONFIG["inverter"]
    applied = {}
    try:
        with client_lock:
            c = _ensure_client()
            for name, value in updates.items():
                ctl = CONTROL_BY_NAME.get(name)
                if ctl is None:
                    logging.warning("Ignoring unknown control '%s'", name)
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    logging.warning("Ignoring non-numeric value for '%s': %r", name, value)
                    continue
                value = max(ctl.get("min", 0), min(ctl.get("max", 0xFFFF), value))
                raw = round(value / ctl["scale"]) & 0xFFFF
                rr = c.write_register(ctl["address"], raw, device_id=inv["device_id"])
                if rr.isError():
                    logging.warning("Modbus write failed for '%s': %s", name, rr)
                    continue
                applied[name] = int(value) if ctl["scale"] >= 1 else round(value, 3)
                logging.info("control write: %s <- %s", name, applied[name])
    except Exception as exc:
        logging.warning("Control write failed: %s", exc)
        _reset_client()
    if applied:
        refresh_controls()   # re-read + broadcast the new state (lock already released)
    return applied


def refresh_controls():
    """Re-read the control settings and push them to the dashboard cache and MQTT."""
    try:
        current = read_controls()
    except Exception as exc:
        logging.warning("Control read failed: %s", exc)
        return None
    with lock:
        controls_latest.clear()
        controls_latest.update(current)
    if mqtt_pub is not None:
        mqtt_pub.publish_controls(current)
    return current


def poll_loop():
    inv = CONFIG["inverter"]
    interval = CONFIG["poller"]["interval_seconds"]
    while True:
        start = time.monotonic()
        sample = None
        try:
            with client_lock:
                c = _ensure_client()
                if c.connected:
                    rr = c.read_input_registers(
                        READ_START, count=READ_COUNT, device_id=inv["device_id"]
                    )
                    if not rr.isError():
                        sample = decode(rr.registers)
                        sample["timestamp"] = time.time()
                    else:
                        logging.warning("Modbus error response: %s", rr)
                else:
                    logging.warning("Cannot connect to inverter (%s)", inv.get("mode", "tcp"))
        except Exception as exc:
            logging.warning("Poll failed: %s", exc)
            _reset_client()

        if sample:
            store_reading(sample)
            if mqtt_pub is not None:
                mqtt_pub.publish(sample)
            refresh_controls()
        else:
            with lock:
                latest["connected"] = False
        time.sleep(max(0.1, interval - (time.monotonic() - start)))


app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"))


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/live")
def api_live():
    with lock:
        return jsonify(latest)


@app.get("/api/history")
def api_history():
    limit = CONFIG["poller"]["history_samples"]
    if store is not None:
        samples = store.recent(limit)
    else:
        with lock:
            samples = list(history)
    return jsonify({
        "interval_seconds": CONFIG["poller"]["interval_seconds"],
        "samples": samples,
    })


@app.get("/api/controls")
def api_controls():
    """Control definitions (name/label/min/max/unit) plus their current values,
    so the dashboard can build the control widgets and show live settings."""
    with lock:
        values = dict(controls_latest)
    return jsonify({"controls": CONTROLS, "values": values})


@app.post("/api/command")
def api_command():
    """Accept {name: value, ...} and write it to the inverter over Modbus.
    Same path as an MQTT command - the dashboard is just another cloud client."""
    updates = request.get_json(force=True, silent=True)
    if not isinstance(updates, dict):
        return jsonify({"error": "body must be a JSON object of {control: value}"}), 400
    applied = write_controls(updates)
    with lock:
        values = dict(controls_latest)
    status = 200 if applied else 400
    return jsonify({"applied": applied, "values": values}), status


def main():
    global store, mqtt_pub
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if CONFIG.get("database", {}).get("enabled", False):
        db_path = ROOT / CONFIG["database"]["path"]
        store = ReadingStore(db_path, REGISTER_MAP)
        logging.info("History persists to SQLite (%s rows so far)", store.row_count())

    mqtt_pub = MqttPublisher(CONFIG.get("mqtt", {}), command_handler=write_controls)
    mqtt_pub.start()
    if CONTROLS:
        logging.info("Cloud Control enabled: %d writable settings (%s)",
                     len(CONTROLS), ", ".join(CONTROL_BY_NAME))

    threading.Thread(target=poll_loop, daemon=True).start()

    dash = CONFIG["dashboard"]
    logging.info("Dashboard on http://%s:%s (open http://localhost:%s in your browser)",
                 dash["host"], dash["port"], dash["port"])
    try:
        app.run(host=dash["host"], port=dash["port"], threaded=True)
    finally:
        if mqtt_pub is not None:
            mqtt_pub.stop()


if __name__ == "__main__":
    main()
