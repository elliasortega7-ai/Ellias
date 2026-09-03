"""The MQTT part: publish inverter readings AND receive control commands.

This is the two-way MQTT layer from the API overview document.

Outbound ("push data out over MQTT"): after the poller decodes a reading it
publishes it to an MQTT broker (e.g. Mosquitto) so any number of subscribers -
a cloud service, Home Assistant, Grafana, a phone app - receive the data in
real time without polling the inverter themselves.

Inbound ("Cloud Control"): the poller also SUBSCRIBES to a command topic. When
the cloud publishes a setting there, this class hands it to a callback, and the
poller translates it into a Modbus write to the inverter. This is exactly the
"the Pi subscribes to an MQTT topic; a cloud message becomes a Modbus write
command" flow described in the requirements.

Topics (prefix configurable, default solar/inverter):
    solar/inverter/data           full reading as JSON, every poll        (out)
    solar/inverter/<name>         each metric on its own topic            (out)
    solar/inverter/status         "online" / "offline" (retained, LWT)    (out)
    solar/inverter/controls       current control settings as JSON        (out)
    solar/inverter/controls/<name>  each setting on its own topic         (out)
    solar/inverter/cmd            {"power_limit_pct": 80, ...} JSON        (in)
    solar/inverter/cmd/<name>     single setting, payload is the value     (in)

Design notes:
- Runs fully non-blocking with an automatic-reconnect loop, so if the broker
  is down or missing the poller keeps working and MQTT reconnects on its own.
- Uses a Last Will and Testament so the broker marks us "offline" if the
  poller dies unexpectedly.
- Commands arrive under .../cmd/#; state is echoed under .../controls/# (a
  different subtree) so our own acks never loop back in as new commands.
"""

import json
import logging

import paho.mqtt.client as mqtt


class MqttPublisher:
    """paho-mqtt wrapper: publishes readings and relays inbound commands."""

    def __init__(self, config, command_handler=None):
        self.enabled = config.get("enabled", False)
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 1883)
        self.prefix = config.get("topic_prefix", "solar/inverter").rstrip("/")
        self.qos = config.get("qos", 0)
        self.per_field = config.get("publish_per_field", True)
        self.accept_commands = config.get("accept_commands", True)
        self.username = config.get("username") or None
        self.password = config.get("password") or None
        # Called with a dict of {control_name: value} when a command arrives.
        # The poller sets this to its Modbus-write function.
        self.command_handler = command_handler
        self.client = None

    @property
    def status_topic(self):
        return f"{self.prefix}/status"

    @property
    def cmd_topic(self):
        return f"{self.prefix}/cmd"

    def start(self):
        if not self.enabled:
            logging.info("MQTT disabled (set mqtt.enabled=true in config.json to publish)")
            return
        # VERSION2 is the current paho callback API (paho-mqtt >= 2.0).
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if self.username:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # Last Will: if we vanish, the broker publishes "offline" for us.
        self.client.will_set(self.status_topic, "offline", qos=self.qos, retain=True)
        try:
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()  # background network thread with auto-reconnect
            logging.info("MQTT publishing to %s:%s under topic '%s/#'",
                         self.host, self.port, self.prefix)
        except Exception as exc:
            logging.warning("MQTT could not start: %s", exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logging.info("MQTT connected to %s:%s", self.host, self.port)
            client.publish(self.status_topic, "online", qos=self.qos, retain=True)
            if self.accept_commands and self.command_handler is not None:
                client.subscribe(f"{self.cmd_topic}/#", qos=self.qos)
                client.subscribe(self.cmd_topic, qos=self.qos)
                logging.info("MQTT listening for commands on '%s/#'", self.cmd_topic)
        else:
            logging.warning("MQTT connection failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, *args):
        logging.warning("MQTT disconnected; will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        """Turn an inbound command message into a {name: value} dict and hand it
        to the poller's Modbus-write handler."""
        if self.command_handler is None:
            return
        payload = msg.payload.decode(errors="replace").strip()
        try:
            if msg.topic == self.cmd_topic:
                # Whole-object command: JSON of {name: value, ...}
                updates = json.loads(payload)
                if not isinstance(updates, dict):
                    raise ValueError("cmd payload must be a JSON object")
            else:
                # Per-field command: topic .../cmd/<name>, payload is the value.
                name = msg.topic[len(self.cmd_topic) + 1:]
                updates = {name: json.loads(payload) if payload else payload}
        except (ValueError, json.JSONDecodeError) as exc:
            logging.warning("Ignoring bad command on %s (%r): %s", msg.topic, payload, exc)
            return
        try:
            self.command_handler(updates)
        except Exception as exc:
            logging.warning("Command handler failed for %s: %s", updates, exc)

    def publish(self, reading):
        """Publish one decoded reading. Safe to call even if MQTT is disabled/down."""
        if not self.enabled or self.client is None:
            return
        try:
            self.client.publish(f"{self.prefix}/data", json.dumps(reading), qos=self.qos)
            if self.per_field:
                for name, value in reading.items():
                    if name == "timestamp":
                        continue
                    self.client.publish(f"{self.prefix}/{name}", str(value), qos=self.qos)
        except Exception as exc:
            logging.warning("MQTT publish failed: %s", exc)

    def publish_controls(self, controls):
        """Publish the current control settings (retained) so subscribers can see
        the inverter's live configuration and confirm a command was applied."""
        if not self.enabled or self.client is None:
            return
        try:
            self.client.publish(f"{self.prefix}/controls", json.dumps(controls),
                                qos=self.qos, retain=True)
            if self.per_field:
                for name, value in controls.items():
                    self.client.publish(f"{self.prefix}/controls/{name}", str(value),
                                        qos=self.qos, retain=True)
        except Exception as exc:
            logging.warning("MQTT controls publish failed: %s", exc)

    def stop(self):
        if self.client is not None:
            try:
                self.client.publish(self.status_topic, "offline", qos=self.qos, retain=True)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
