"""Standalone MQTT command sender - the "cloud" end of Cloud Control.

Publishes a control command to the inverter's MQTT command topic. The poller is
subscribed there; it receives the message and translates it into a Modbus write
(FC 06) to the inverter. This is the exact flow from the API overview: a cloud
message becomes a Modbus write command.

Examples (simulator + poller running, mqtt.enabled=true, a broker up):

    # Limit AC output to 50% of rated power
    python3 tools/send_command.py power_limit_pct 50

    # Turn the inverter off, then back on
    python3 tools/send_command.py inverter_enable 0
    python3 tools/send_command.py inverter_enable 1

    # Cap battery charge power to 1500 W
    python3 tools/send_command.py battery_charge_limit 1500

    # Send several settings at once (published as one JSON message)
    python3 tools/send_command.py power_limit_pct=80 battery_discharge_limit=2000

Watch the effect with tools/mqtt_subscriber.py or the dashboard's Controls card.
"""

import json
import sys
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())["mqtt"]
PREFIX = CONFIG.get("topic_prefix", "solar/inverter").rstrip("/")


def parse_args(argv):
    """Accept either `name value` or one/more `name=value` pairs."""
    if len(argv) == 2 and "=" not in argv[0]:
        return {argv[0]: _num(argv[1])}
    updates = {}
    for arg in argv:
        if "=" not in arg:
            sys.exit(f"Bad argument {arg!r}: expected name=value")
        name, value = arg.split("=", 1)
        updates[name] = _num(value)
    return updates


def _num(text):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    updates = parse_args(sys.argv[1:])

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if CONFIG.get("username"):
        client.username_pw_set(CONFIG["username"], CONFIG.get("password"))
    host, port = CONFIG.get("host", "127.0.0.1"), CONFIG.get("port", 1883)
    qos = CONFIG.get("qos", 0)
    client.connect(host, port, keepalive=30)
    client.loop_start()

    if len(updates) == 1:
        # Per-field topic: .../cmd/<name>, payload is the raw value.
        (name, value), = updates.items()
        topic = f"{PREFIX}/cmd/{name}"
        client.publish(topic, json.dumps(value), qos=qos).wait_for_publish()
        print(f"published {topic} = {value}")
    else:
        # Whole-object topic: .../cmd, payload is a JSON object.
        topic = f"{PREFIX}/cmd"
        client.publish(topic, json.dumps(updates), qos=qos).wait_for_publish()
        print(f"published {topic} = {json.dumps(updates)}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
