# Solar Inverter Monitor (with simulator)

Poll live data from a solar inverter and display it on a web dashboard.
Since the real inverter isn't available yet, this repo includes a **simulator**
that behaves like one, speaking the same protocol a real inverter would —
so when the hardware arrives, you point the poller at it and nothing else
changes.

## Architecture

This follows the standard local-polling pattern for inverters (see the API
overview doc): the inverter exposes its data as scaled 16-bit **Modbus**
registers, and an edge client (e.g., a Raspberry Pi) acts as the Modbus
*master* — the inverter never volunteers data, the client must ask for it.
The poller then fans the data out three ways: it **stores** it in SQLite,
**publishes** it over MQTT, and **serves** it to the dashboard.

It also works the **other way** — *Cloud Control*. The poller subscribes to an
MQTT command topic; when the cloud (or the dashboard) sends a setting, the
poller turns it into a Modbus **write** (FC 06) to the inverter's holding
registers — so you can cap output power, limit battery charge/discharge, or
switch the inverter on/off remotely. This is the "a cloud message becomes a
Modbus write command" flow from the API overview doc.

```
┌─────────────────────┐   Modbus (FC 04 read)  ┌──────────────────────────┐  HTTP/JSON  ┌───────────┐
│ simulator/           │ <── poll every 3s ─────│ poller/                   │ ─────────── │  Browser   │
│  inverter_sim.py     │  16-bit registers ────>│  poller.py (edge gateway) │ /api/live   │  dashboard │
│  (fake inverter,     │                        │   decode + scale          │ /api/history│  + controls│
│   Modbus TCP :5020)  │ <── FC 06 write ───────│        │        │         │ /api/command└───────────┘
└─────────────────────┘   (control settings)    │        ▼        ▼         │
                                                │   SQLite DB   MQTT pub/sub │<─> MQTT broker <─> cloud / HA / …
                                                │   (history)  (solar/...)   │   (Mosquitto)   data out + cmd in
                                                └──────────────────────────┘
```

Both extra outputs are optional and configured in `config.json`
(`database.enabled`, `mqtt.enabled`) — turn either off and the rest keeps
working.

- **`simulator/inverter_sim.py`** — a Modbus TCP server that models a realistic
  8 kW hybrid inverter: solar day curve with passing clouds, two PV strings,
  house load with appliance spikes, a 10 kWh battery that charges from surplus
  and discharges at night, temperature, daily/lifetime energy counters.
  By default simulated time runs at **60×** (a full day in 24 minutes) so you
  can watch the whole solar curve; set `simulator.time_speed` to `1` in
  `config.json` for real time.
- **`poller/poller.py`** — the edge gateway. A Modbus client (uses `pymodbus`,
  the same library you'd use on a Raspberry Pi) that polls the registers on an
  interval, applies the scaling from `register_map.json`, then stores +
  publishes + serves the data. TCP or RS-485 serial, chosen by config.
- **`poller/database.py`** — *the SQL part*. Writes every reading to a SQLite
  file (`inverter_data.db`) so history survives restarts and is queryable.
- **`poller/mqtt_publisher.py`** — *the MQTT part*. Publishes each reading to an
  MQTT broker (pub/sub) for cloud services, Home Assistant, Grafana, etc., and
  subscribes to the command topic to receive Cloud Control commands.
- **`tools/mqtt_subscriber.py`** — a standalone subscriber to watch the MQTT
  data flow (the "receiving end"); handy for verifying MQTT works.
- **`tools/send_command.py`** — a standalone command sender (the "cloud" end of
  Cloud Control): publishes a setting to the command topic so the poller writes
  it to the inverter over Modbus.
- **`poller/static/index.html`** — live dashboard: status, stat tiles, power
  flow chart (PV / load / battery), battery state of charge, a table view, and
  a **Controls** card (power limit, battery charge/discharge limits, on/off).
- **`register_map.json`** — the register map (addresses, scaling, units): the
  read-only `registers` (measurements) plus the writable `controls` (settings),
  shared by simulator and poller. This mimics a manufacturer's Modbus map.
- **`config.json`** — connection mode, hosts, ports, poll interval, database,
  MQTT, and simulator parameters.

## Data outputs: SQL and MQTT

**SQLite (the SQL part).** Enabled by default (`database.enabled`). Every
reading is written to `inverter_data.db` in the project root, with one column
per register. History survives restarts, and you can query it directly:

```bash
sqlite3 inverter_data.db "SELECT MAX(pv_power), ROUND(AVG(battery_soc),1) FROM readings;"
```

Delete the `.db` file to reset it (also do this if you change
`register_map.json`, since the columns come from it).

**MQTT (the MQTT part).** Enabled by default (`mqtt.enabled`) and expects a
broker on `127.0.0.1:1883`. Install one (e.g. **Mosquitto**), then the poller
publishes:

| Topic | Direction | Payload |
|---|---|---|
| `solar/inverter/data` | out | full reading as JSON, every poll |
| `solar/inverter/<name>` | out | each metric on its own topic (e.g. `.../ac_power`) |
| `solar/inverter/status` | out | `online` / `offline` (retained) |
| `solar/inverter/controls` | out | current control settings as JSON (retained) |
| `solar/inverter/controls/<name>` | out | each setting on its own topic (retained) |
| `solar/inverter/cmd` | **in** | `{"power_limit_pct": 80, ...}` — set several at once |
| `solar/inverter/cmd/<name>` | **in** | set one setting; payload is the value (e.g. `50`) |

To watch it, in a third terminal run `python3 tools/mqtt_subscriber.py`
(or `mosquitto_sub -t 'solar/inverter/#' -v`). If no broker is running the
poller keeps working and reconnects automatically; set `mqtt.enabled` to
`false` to turn it off.

## Cloud Control (writing settings to the inverter)

The read path above is one direction; Cloud Control is the return path. The
poller subscribes to `solar/inverter/cmd/#`, and each command becomes a Modbus
**write** (FC 06 Preset Single Register) to the inverter's holding registers.
The dashboard's **Controls** card uses the very same path via `POST
/api/command` — it's just another client.

Writable settings (defined in `register_map.json` under `controls`):

| Setting | Range | Effect |
|---|---|---|
| `inverter_enable` | 0 / 1 | Turn the inverter output off / on |
| `power_limit_pct` | 0–100 % | Cap AC output to this share of rated power |
| `battery_charge_limit` | 0–5000 W | Maximum battery charge power |
| `battery_discharge_limit` | 0–5000 W | Maximum battery discharge power |

Send a command from the "cloud" (needs a broker running):

```bash
python3 tools/send_command.py power_limit_pct 50      # curtail AC output to 50%
python3 tools/send_command.py inverter_enable 0       # switch the inverter off
python3 tools/send_command.py battery_charge_limit=1500 power_limit_pct=80
```

Values are validated against the register map (unknown names ignored,
out-of-range values clamped). The simulator honors the write and changes its
behaviour, so you can watch the effect on the dashboard or in the data stream.
Set `mqtt.accept_commands` to `false` in `config.json` to make the poller
publish-only (the dashboard `POST /api/command` still works).

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
| `GET /api/controls` | Control definitions (min/max/label) + current values |
| `POST /api/command` | Write settings, e.g. `{"power_limit_pct": 50}`; returns what was applied |

## Swapping in the real inverter

1. Get the manufacturer's **Modbus register map** from the manual/vendor
   portal and rewrite `register_map.json` to match (addresses, scale factors,
   signedness, 32-bit registers). Map the read-only measurements under
   `registers` and the writable settings under `controls` (with the real
   holding-register addresses and min/max limits) — some inverters expect
   FC 16 or a control-mode "unlock" register before writes are accepted.
2. Set the connection in `config.json` → `inverter`:
   - **Networked inverter (Ethernet/Wi-Fi):** keep `"mode": "tcp"`, set
     `host` to the inverter's IP and `port` to **502**.
   - **RS-485 (USB adapter):** set `"mode": "serial"` and fill in the
     `serial` block (`port` is `COM3` etc. on Windows, `/dev/ttyUSB0` on
     Linux; plus `baudrate`/`parity`/`stopbits` from the manual). No code
     change needed — the poller picks the client based on `mode`.
   - Set `device_id` to the inverter's Modbus unit/slave id.
3. Stop running the simulator.

Notes from the inverter-integration doc worth keeping in mind:

- Poll every 2–5 s; polling too fast can crash some inverters' interfaces.
- Values usually need scaling (e.g., raw `1500` ÷ 10 = `150.0 W`) — that's
  what the `scale` field in the register map does.
- If multiple devices share an RS-485 bus, they need unique unit IDs
  (`inverter.device_id` in `config.json`).
