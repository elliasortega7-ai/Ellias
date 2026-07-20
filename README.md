# Solar Inverter Monitor (with simulator)

Poll live data from a solar inverter and display it on a web dashboard.
Since the real inverter isn't available yet, this repo includes a **simulator**
that behaves like one, speaking the same protocol a real inverter would —
so when the hardware arrives, you point the poller at it and nothing else
changes.

## Architecture

This follows the standard local-polling pattern for inverters (see the API
overview doc): the inverter exposes its data as scaled 16-bit **Modbus TCP**
registers, and an edge client (e.g., a Raspberry Pi) acts as the Modbus
*master* — the inverter never volunteers data, the client must ask for it.

```
┌─────────────────────┐   Modbus TCP (FC 04)   ┌──────────────────────┐   HTTP/JSON   ┌───────────┐
│ simulator/           │ <── poll every 3 s ── │ poller/               │ ──────────── │  Browser   │
│  inverter_sim.py     │                        │  poller.py            │               │  dashboard │
│  (fake inverter,     │  16-bit registers ──> │  decode + scale       │  /api/live    │            │
│   Modbus TCP :5020)  │                        │  history buffer       │  /api/history │            │
└─────────────────────┘                        │  Flask on :8080       │               └───────────┘
                                               └──────────────────────┘
```

- **`simulator/inverter_sim.py`** — a Modbus TCP server that models a realistic
  8 kW hybrid inverter: solar day curve with passing clouds, two PV strings,
  house load with appliance spikes, a 10 kWh battery that charges from surplus
  and discharges at night, temperature, daily/lifetime energy counters.
  By default simulated time runs at **60×** (a full day in 24 minutes) so you
  can watch the whole solar curve; set `simulator.time_speed` to `1` in
  `config.json` for real time.
- **`poller/poller.py`** — the Modbus client (uses `pymodbus`, the same library
  you'd use on a Raspberry Pi). Polls the registers on an interval, applies the
  scaling from `register_map.json`, keeps a rolling history, and serves the
  dashboard and a JSON API.
- **`poller/static/index.html`** — live dashboard: status, stat tiles, power
  flow chart (PV / load / battery), battery state of charge, and a table view.
- **`register_map.json`** — the register map (addresses, scaling, units),
  shared by simulator and poller. This mimics a manufacturer's Modbus map.
- **`config.json`** — hosts, ports, poll interval, simulator parameters.

## Run it

```bash
pip install -r requirements.txt
./run.sh
```

Then open **http://localhost:8080**. The dashboard refreshes every poll
interval (3 s), supports light/dark mode, and shows tooltips on hover.

Run the pieces separately if you prefer:

```bash
python3 simulator/inverter_sim.py   # terminal 1 - the "inverter"
python3 poller/poller.py            # terminal 2 - poller + dashboard
```

## JSON API

| Endpoint | Returns |
|---|---|
| `GET /api/live` | Latest decoded reading (plus `connected` flag) |
| `GET /api/history` | Rolling buffer of readings (default: 1 h at 3 s) |

## Swapping in the real inverter

1. Get the manufacturer's **Modbus register map** from the manual/vendor
   portal and rewrite `register_map.json` to match (addresses, scale factors,
   signedness, 32-bit registers).
2. Point `config.json` → `inverter.host` / `inverter.port` at the inverter's
   IP (Modbus TCP is usually port **502**; over RS-485 you'd swap
   `ModbusTcpClient` for `ModbusSerialClient` in `poller/poller.py`).
3. Stop running the simulator.

Notes from the inverter-integration doc worth keeping in mind:

- Poll every 2–5 s; polling too fast can crash some inverters' interfaces.
- Values usually need scaling (e.g., raw `1500` ÷ 10 = `150.0 W`) — that's
  what the `scale` field in the register map does.
- If multiple devices share an RS-485 bus, they need unique unit IDs
  (`inverter.device_id` in `config.json`).
