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

from flask import Flask, jsonify, send_from_directory
from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from database import ReadingStore
from mqtt_publisher import MqttPublisher

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
REGISTER_MAP = json.loads((ROOT / "register_map.json").read_text())["registers"]

READ_START = min(r["address"] for r in REGISTER_MAP)
READ_COUNT = max(r["address"] + r["words"] for r in REGISTER_MAP) - READ_START

# In-memory fallback history, used when the SQLite store is disabled.
history = deque(maxlen=CONFIG["poller"]["history_samples"])
latest = {"connected": False}
lock = threading.Lock()

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


def poll_loop():
    inv = CONFIG["inverter"]
    interval = CONFIG["poller"]["interval_seconds"]
    client = build_client()
    while True:
        start = time.monotonic()
        sample = None
        try:
            if client.connected or client.connect():
                rr = client.read_input_registers(
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
            client.close()

        if sample:
            store_reading(sample)
            if mqtt_pub is not None:
                mqtt_pub.publish(sample)
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


def main():
    global store, mqtt_pub
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if CONFIG.get("database", {}).get("enabled", False):
        db_path = ROOT / CONFIG["database"]["path"]
        store = ReadingStore(db_path, REGISTER_MAP)
        logging.info("History persists to SQLite (%s rows so far)", store.row_count())

    mqtt_pub = MqttPublisher(CONFIG.get("mqtt", {}))
    mqtt_pub.start()

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
