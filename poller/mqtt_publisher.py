"""The MQTT part: publish inverter readings to an MQTT broker.

This is the "push data out over MQTT" step from the API overview document.
After the poller decodes a reading, it publishes it to an MQTT broker
(e.g. Mosquitto) so any number of subscribers - a cloud service, Home
Assistant, Grafana, a phone app - can receive the data in real time without
polling the inverter themselves. MQTT is publish/subscribe: the poller
publishes once, the broker fans it out to everyone subscribed.

Published topics (prefix configurable):
    solar/inverter/data          full reading as JSON, every poll
    solar/inverter/<name>        each metric on its own topic (e.g. .../ac_power)
    solar/inverter/status        "online" / "offline" (retained, via last will)

Design notes:
- Runs fully non-blocking with an automatic-reconnect loop, so if the broker
  is down or missing the poller keeps working and MQTT reconnects on its own.
- Uses a Last Will and Testament so the broker marks us "offline" if the
  poller dies unexpectedly.
"""

import json
import logging

import paho.mqtt.client as mqtt


class MqttPublisher:
    """Thin wrapper around paho-mqtt for one-way publishing of readings."""

    def __init__(self, config):
        self.enabled = config.get("enabled", False)
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 1883)
        self.prefix = config.get("topic_prefix", "solar/inverter").rstrip("/")
        self.qos = config.get("qos", 0)
        self.per_field = config.get("publish_per_field", True)
        self.username = config.get("username") or None
        self.password = config.get("password") or None
        self.client = None

    @property
    def status_topic(self):
        return f"{self.prefix}/status"

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
        else:
            logging.warning("MQTT connection failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, *args):
        logging.warning("MQTT disconnected; will auto-reconnect")

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

    def stop(self):
        if self.client is not None:
            try:
                self.client.publish(self.status_topic, "offline", qos=self.qos, retain=True)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
