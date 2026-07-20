"""Inverter poller and dashboard server.

Acts as the "edge gateway" from the API overview document: it is the Modbus
master/client - the inverter never volunteers data, so this process asks for
it on an interval (FC 04, Read Input Registers), applies the scaling factors
from register_map.json to turn raw 16-bit registers into engineering units,
keeps a rolling history in memory, and serves a live web dashboard plus a
small JSON API:

    GET /              dashboard
    GET /api/live      latest decoded reading
    GET /api/history   rolling buffer of decoded readings

Point config.json's inverter.host/port at real hardware later and everything
downstream stays the same.

Run:  python3 poller/poller.py
"""

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from pymodbus.client import ModbusTcpClient

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
REGISTER_MAP = json.loads((ROOT / "register_map.json").read_text())["registers"]

READ_START = min(r["address"] for r in REGISTER_MAP)
READ_COUNT = max(r["address"] + r["words"] for r in REGISTER_MAP) - READ_START

history = deque(maxlen=CONFIG["poller"]["history_samples"])
latest = {"connected": False}
lock = threading.Lock()


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


def poll_loop():
    inv = CONFIG["inverter"]
    interval = CONFIG["poller"]["interval_seconds"]
    client = ModbusTcpClient(inv["host"], port=inv["port"])
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
                logging.warning("Cannot connect to inverter at %s:%s", inv["host"], inv["port"])
        except Exception as exc:
            logging.warning("Poll failed: %s", exc)
            client.close()

        with lock:
            if sample:
                history.append(sample)
                latest.clear()
                latest.update(sample, connected=True)
            else:
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
    with lock:
        return jsonify({
            "interval_seconds": CONFIG["poller"]["interval_seconds"],
            "samples": list(history),
        })


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    threading.Thread(target=poll_loop, daemon=True).start()
    dash = CONFIG["dashboard"]
    logging.info("Dashboard on http://%s:%s (polling %s:%s every %ss)",
                 dash["host"], dash["port"],
                 CONFIG["inverter"]["host"], CONFIG["inverter"]["port"],
                 CONFIG["poller"]["interval_seconds"])
    app.run(host=dash["host"], port=dash["port"], threaded=True)


if __name__ == "__main__":
    main()
