"""Solar inverter simulator.

Stands in for a real hybrid solar inverter by exposing the same interface a
real one would: a Modbus TCP server (the protocol most inverters speak over
their LAN port, usually on port 502). The poller talks to this exactly as it
would talk to the real device, so swapping in real hardware later is a
config change, not a code change.

The Modbus TCP framing (MBAP header + PDU) is implemented directly with the
standard library; the server answers function codes 03 (Read Holding
Registers) and 04 (Read Input Registers) from the same register image, as
many inverters do.

The model simulates:
  - a clear-sky solar curve (sunrise ~06:30, sunset ~19:30) with passing
    clouds (smoothed random walk on irradiance),
  - two PV strings at MPP voltage with currents derived from power,
  - a house load (base load + random appliance spikes),
  - a battery that absorbs PV surplus and covers the load at night,
  - inverter conversion losses and temperature that tracks output power,
  - daily energy (resets at simulated midnight) and lifetime energy.

Values are encoded into 16-bit registers with the scaling factors declared
in register_map.json - the same way a real inverter's register map works.

Run:  python3 simulator/inverter_sim.py
"""

import json
import logging
import math
import random
import socketserver
import struct
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())["simulator"]
REGISTER_MAP = json.loads((ROOT / "register_map.json").read_text())["registers"]
REGISTER_COUNT = max(r["address"] + r["words"] for r in REGISTER_MAP)

SUNRISE = 6.5
SUNSET = 19.5
NOMINAL_GRID_V = 230.0
NOMINAL_GRID_HZ = 50.0
BATTERY_NOMINAL_V = 51.2  # 16s LiFePO4
INVERTER_EFFICIENCY = 0.965
MAX_CHARGE_W = 5000
MAX_DISCHARGE_W = 5000

STATUS_STANDBY, STATUS_GENERATING, STATUS_FAULT = 0, 1, 2


class InverterModel:
    """Produces a coherent set of electrical readings for a simulated moment."""

    def __init__(self, rated_power_w, battery_capacity_kwh, time_speed, start_hour):
        self.rated_power_w = rated_power_w
        self.battery_capacity_wh = battery_capacity_kwh * 1000
        self.time_speed = time_speed
        self.sim_seconds = start_hour * 3600  # seconds since sim midnight
        self.last_tick = time.monotonic()

        self.cloud_factor = 1.0
        self.battery_soc = 65.0
        self.load_spike_w = 0.0
        self.load_spike_until = 0.0
        self.energy_today_wh = 0.0
        self.energy_total_wh = 2_437_000.0  # a plausibly used inverter
        self.temperature_c = 25.0

    def hour_of_day(self):
        return (self.sim_seconds % 86400) / 3600

    def _clear_sky_fraction(self, hour):
        if hour <= SUNRISE or hour >= SUNSET:
            return 0.0
        # Half-sine over the daylight window, peak at solar noon.
        return math.sin(math.pi * (hour - SUNRISE) / (SUNSET - SUNRISE))

    def _step_clouds(self, dt_sim):
        # Random walk pulled back toward clear sky, clamped to [0.25, 1.0].
        drift = (1.0 - self.cloud_factor) * 0.002 * dt_sim
        noise = random.gauss(0, 0.004) * math.sqrt(max(dt_sim, 1e-9))
        self.cloud_factor = max(0.25, min(1.0, self.cloud_factor + drift + noise))

    def _step_load(self, dt_sim):
        if self.sim_seconds >= self.load_spike_until and random.random() < 0.002 * dt_sim:
            # An appliance turns on (kettle, oven, EV charger...) for 5-30 sim minutes.
            self.load_spike_w = random.choice([800, 1200, 1800, 2400, 3200])
            self.load_spike_until = self.sim_seconds + random.uniform(300, 1800)
        if self.sim_seconds >= self.load_spike_until:
            self.load_spike_w = 0.0
        hour = self.hour_of_day()
        # Base load: higher in morning and evening, lower overnight.
        base = 250 + 200 * math.exp(-((hour - 8) ** 2) / 8) + 350 * math.exp(-((hour - 20) ** 2) / 6)
        return base + self.load_spike_w + random.uniform(-20, 20)

    def tick(self):
        """Advance the model to 'now' and return a dict of readings."""
        now = time.monotonic()
        dt_sim = (now - self.last_tick) * self.time_speed
        self.last_tick = now
        prev_day = int(self.sim_seconds // 86400)
        self.sim_seconds += dt_sim
        if int(self.sim_seconds // 86400) != prev_day:
            self.energy_today_wh = 0.0

        self._step_clouds(dt_sim)
        hour = self.hour_of_day()

        pv_power = self.rated_power_w * self._clear_sky_fraction(hour) * self.cloud_factor
        pv_power = max(0.0, pv_power + random.uniform(-30, 30))

        load_power = self._step_load(dt_sim)

        # Battery balances PV against load: surplus charges, deficit discharges.
        surplus = pv_power * INVERTER_EFFICIENCY - load_power
        if surplus >= 0:
            battery_power = -min(surplus, MAX_CHARGE_W) if self.battery_soc < 100 else 0.0
        else:
            battery_power = min(-surplus, MAX_DISCHARGE_W) if self.battery_soc > 10 else 0.0
        self.battery_soc -= (battery_power * dt_sim / 3600) / self.battery_capacity_wh * 100
        self.battery_soc = max(5.0, min(100.0, self.battery_soc))

        ac_power = max(0.0, pv_power * INVERTER_EFFICIENCY + battery_power)

        self.energy_today_wh += pv_power * dt_sim / 3600
        self.energy_total_wh += pv_power * dt_sim / 3600

        # Heatsink temperature relaxes toward ambient + loading-dependent rise.
        target_temp = 25.0 + 30.0 * (ac_power / self.rated_power_w)
        self.temperature_c += (target_temp - self.temperature_c) * min(1.0, 0.001 * dt_sim)

        generating = pv_power > 15
        if generating:
            # MPP voltage sags slightly as power rises; strings split ~55/45.
            pv1_v = 385 - 20 * (pv_power / self.rated_power_w) + random.uniform(-2, 2)
            pv2_v = 378 - 18 * (pv_power / self.rated_power_w) + random.uniform(-2, 2)
            pv1_i = (pv_power * 0.55) / pv1_v
            pv2_i = (pv_power * 0.45) / pv2_v
        else:
            pv1_v = pv2_v = random.uniform(0, 40)  # open-circuit drift at night
            pv1_i = pv2_i = 0.0

        ac_voltage = NOMINAL_GRID_V + random.uniform(-2.5, 2.5)
        battery_voltage = BATTERY_NOMINAL_V * (0.94 + 0.12 * self.battery_soc / 100)

        return {
            "status": STATUS_GENERATING if generating else STATUS_STANDBY,
            "pv1_voltage": pv1_v,
            "pv1_current": pv1_i,
            "pv2_voltage": pv2_v,
            "pv2_current": pv2_i,
            "pv_power": pv_power,
            "ac_voltage": ac_voltage,
            "ac_current": ac_power / ac_voltage,
            "ac_power": ac_power,
            "grid_frequency": NOMINAL_GRID_HZ + random.uniform(-0.04, 0.04),
            "load_power": load_power,
            "battery_power": battery_power,
            "battery_soc": self.battery_soc,
            "battery_voltage": battery_voltage,
            "temperature": self.temperature_c,
            "energy_today": self.energy_today_wh / 1000,
            "energy_total": self.energy_total_wh / 1000,
        }


def encode(readings):
    """Encode scaled readings into a flat 16-bit register image."""
    regs = [0] * REGISTER_COUNT
    for reg in REGISTER_MAP:
        raw = round(readings[reg["name"]] / reg["scale"])
        if reg["words"] == 2:
            raw = max(0, min(0xFFFFFFFF, raw))
            regs[reg["address"]] = (raw >> 16) & 0xFFFF
            regs[reg["address"] + 1] = raw & 0xFFFF
        else:
            if reg["signed"]:
                raw = max(-32768, min(32767, raw)) & 0xFFFF
            else:
                raw = max(0, min(0xFFFF, raw))
            regs[reg["address"]] = raw
    return regs


class RegisterBank:
    """Thread-safe register image shared between model and Modbus server."""

    def __init__(self):
        self._regs = [0] * REGISTER_COUNT
        self._lock = threading.Lock()

    def write(self, regs):
        with self._lock:
            self._regs = list(regs)

    def read(self, address, count):
        with self._lock:
            if address < 0 or count < 1 or address + count > len(self._regs):
                return None
            return self._regs[address:address + count]


BANK = RegisterBank()

ILLEGAL_FUNCTION, ILLEGAL_DATA_ADDRESS = 0x01, 0x02


class ModbusTCPHandler(socketserver.BaseRequestHandler):
    """Answers Modbus TCP read requests (FC 03/04) from the register bank."""

    def handle(self):
        while True:
            header = self._recv_exact(7)
            if header is None:
                return
            transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", header)
            pdu = self._recv_exact(length - 1)
            if pdu is None or protocol_id != 0:
                return
            function_code = pdu[0]
            if function_code in (0x03, 0x04) and len(pdu) == 5:
                address, count = struct.unpack(">HH", pdu[1:5])
                regs = BANK.read(address, count) if 1 <= count <= 125 else None
                if regs is None:
                    response = struct.pack(">BB", function_code | 0x80, ILLEGAL_DATA_ADDRESS)
                else:
                    response = struct.pack(">BB", function_code, count * 2)
                    response += struct.pack(f">{count}H", *regs)
            else:
                response = struct.pack(">BB", function_code | 0x80, ILLEGAL_FUNCTION)
            mbap = struct.pack(">HHHB", transaction_id, 0, len(response) + 1, unit_id)
            try:
                self.request.sendall(mbap + response)
            except OSError:
                return

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            try:
                chunk = self.request.recv(n - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def updater(model, period=1.0):
    while True:
        readings = model.tick()
        BANK.write(encode(readings))
        hh, mm = int(model.hour_of_day()), int(model.hour_of_day() * 60) % 60
        logging.info(
            "sim %02d:%02d  PV %4.0fW  load %4.0fW  batt %+5.0fW  SOC %3.0f%%",
            hh, mm, readings["pv_power"], readings["load_power"],
            readings["battery_power"], readings["battery_soc"],
        )
        time.sleep(period)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    model = InverterModel(
        rated_power_w=CONFIG["rated_power_w"],
        battery_capacity_kwh=CONFIG["battery_capacity_kwh"],
        time_speed=CONFIG["time_speed"],
        start_hour=CONFIG["start_hour"],
    )
    BANK.write(encode(model.tick()))
    threading.Thread(target=updater, args=(model,), daemon=True).start()

    addr = (CONFIG["listen_host"], CONFIG["listen_port"])
    logging.info("Inverter simulator: Modbus TCP on %s:%s (time x%s)", *addr, CONFIG["time_speed"])
    with ThreadingTCPServer(addr, ModbusTCPHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
