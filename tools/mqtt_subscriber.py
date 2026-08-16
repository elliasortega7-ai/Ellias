"""Standalone MQTT subscriber - a verification/demo tool.

Run this in a third terminal (while the simulator and poller are running,
with mqtt.enabled=true in config.json) to watch the inverter data the poller
publishes. This is the "receiving end" of MQTT - the role a cloud service or
Home Assistant would play.

    python3 tools/mqtt_subscriber.py

It subscribes to the whole topic tree and prints each message as it arrives.
"""

import json
import time
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())["mqtt"]
PREFIX = CONFIG.get("topic_prefix", "solar/inverter").rstrip("/")


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        client.subscribe(f"{PREFIX}/#")
        print(f"Subscribed to {PREFIX}/#  (Ctrl+C to quit)\n")
    else:
        print("Connection failed:", reason_code)


def on_message(client, userdata, msg):
    ts = time.strftime("%H:%M:%S")
    if msg.topic.endswith("/data"):
        # The full JSON reading - pull out a few headline fields.
        try:
            d = json.loads(msg.payload)
            print(f"[{ts}] {msg.topic}: "
                  f"PV={d.get('pv_power')}W  load={d.get('load_power')}W  "
                  f"batt={d.get('battery_power')}W  SOC={d.get('battery_soc')}%")
        except json.JSONDecodeError:
            print(f"[{ts}] {msg.topic}: {msg.payload.decode(errors='replace')}")
    else:
        print(f"[{ts}] {msg.topic} = {msg.payload.decode(errors='replace')}")


def main():
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if CONFIG.get("username"):
        client.username_pw_set(CONFIG["username"], CONFIG.get("password"))
    client.on_connect = on_connect
    client.on_message = on_message
    host, port = CONFIG.get("host", "127.0.0.1"), CONFIG.get("port", 1883)
    print(f"Connecting to broker {host}:{port} ...")
    client.connect(host, port, keepalive=30)
    client.loop_forever()


if __name__ == "__main__":
    main()
