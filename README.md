## About this fork

This fork of [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) adds:

- **[EcoFlow Smart Gateway](#ecoflow-smart-gateway) support:** monitoring and control of the 200A Smart Gateway (HR65) and its paired DELTA Pro Ultras.
- **[Per-battery sensors](#delta-pro-ultra-per-battery-sensors) for the EcoFlow DELTA Pro Ultra** (developer API connection). Submitted upstream as [pull request #927](https://github.com/tolwi/hassio-ecoflow-cloud/pull/927).
- **[River 3 and River 3 Plus](#river-3-and-river-3-plus) fixes:** battery level, settings and switches now fill in, and a River 3 Plus is recognized automatically, with its own charging range and LED control.
- **[DELTA Pro Ultra X](#delta-pro-ultra-x) output sensors**, and **[stall detection](#telemetry-stalls-smart-gateway-and-delta-pro-ultra-x)** for the gateway's and the DPU X's live data, which can freeze without any sign in Home Assistant.
- **[Fewer recorder writes](#fewer-recorder-writes)** from power, remaining-time and energy sensors, for every device.
- **[A capture decoder](#decoding-a-debug-capture)** that turns a DEBUG log into readable frames, for diagnosing devices.

Everything else is identical to upstream. For general setup and the other supported devices, see the [upstream README](https://github.com/tolwi/hassio-ecoflow-cloud#readme).

### Installing this fork

1. In HACS, open the ⋮ menu → **Custom repositories**, add `https://github.com/1RadMan/ecoflow-cloud-dpu-extra` with type **Integration**.
2. If you already have the upstream EcoFlow Cloud integration from HACS, remove it **in HACS** first (not under Settings → Integrations). Both use the same integration, so your EcoFlow configuration and existing entities carry over.
3. Download **EcoFlowCloud** from this fork's repository and restart Home Assistant.

## EcoFlow Smart Gateway

The integration had no profile for the Smart Gateway. It speaks the same protocol as the Smart Home Panel 3, so this adds it as a variant of that device with the gateway's own flows and controls.

### Adding the gateway

The gateway is only reachable through EcoFlow's app connection, not the developer API. In the integration, add an entry with your **EcoFlow account** (email and password), choose the device type **`SMART_GATEWAY`**, and enter the gateway's serial number (starts with `HR65`). Your DELTA Pro Ultras can stay on their existing (developer API) entry.

### Sensors

- Home load, grid (total and per leg: power, voltage, current), solar from all paired DPUs, DPU output, and battery charging / discharging, each with a kWh total for the Energy dashboard
- Combined battery level, backup reserve, charge / discharge time remaining
- Per smart circuit: power (with kWh), voltage and current, named after the circuit labels set in the EcoFlow app. 240V circuits appear once, with both legs combined. Pairing or splitting circuits in the app is picked up automatically.
- **S.I.B. AC1–AC3 Activity:** what each Smart Inlet Box port is doing: Off, Idle, Supplying home or Grid charging (another value shows as "Unknown (n)").
- **Reporting Interval** (diagnostic): how often the gateway is pushing updates, normally 2 s. See [telemetry stalls](#telemetry-stalls-smart-gateway-and-delta-pro-ultra-x).
- **Firmware Version** (diagnostic).
- **Telemetry Stalled** and **Data Stale** (diagnostic): see [telemetry stalls](#telemetry-stalls-smart-gateway-and-delta-pro-ultra-x).

### Controls

| Control | What it does |
|---|---|
| Operating Mode | Self-Powered or Backup. Other modes (such as TOU) show as unknown and can't be selected. |
| Smart Circuit N | Turns the circuit on or off (both legs of a 240V circuit). |
| Smart Circuit N Source | Auto (follows the operating mode) or Grid. |
| Backup Reserve | 5–70% (70% is the app's maximum). In Backup mode, raising it above the battery level makes the DPUs charge from the grid up to it. |
| Battery Charging Power per DPU | Grid-charging power limit, applied to each paired DPU, 1,000–12,000 W. |
| S.I.B. AC1–AC3 | Turns a Smart Inlet Box (S.I.B.) port on or off, named after what's attached, e.g. "S.I.B. AC2 (DPU 1)". Off stops the gateway drawing power from that DPU or EV charger; a DPU stays on and keeps charging from solar. Switching a port off also cancels its Charge Now, as in the app. Follows the inlet box's own port buttons too. |
| S.I.B. AC1–AC3 Chg Now | Charges the DPU on that port from the grid until it reaches its max-charge setting (switching the port on if it was off). On a port with an EV charger, this is the app's Charge now for the car. |

The inlet box controls are unavailable while nothing is attached to their port. Before this version, Charge Now was named "DPU 1 / DPU 2 Charge Now" (ports AC2 / AC3); those two keep their entity IDs, so existing automations carry on working.

Each control checks that the gateway reported the change and sends it once more if it didn't. Changes made in the EcoFlow app get a logbook entry saying so.

### Tested with, and limits

- Built and tested on one Smart Gateway with two DELTA Pro Ultras (firmware as of October 2026). The message fields were decoded from that setup; other firmware may differ. Please open an issue with a diagnostics download if something looks wrong.
- **Inlet box controls** were tested with DELTA Pro Ultras on all three ports and an EV charger on AC1. Port on/off and Charge Now use the same commands on every port, whatever is attached. Charge Now with an EV charger and the DELTA Pro Ultra X are untested.
- **Inlet port names** use the attached device's name in Home Assistant when it's also set up in this integration (matched by serial number), otherwise its type ("DPU", "EV Charger"). A DELTA Pro Ultra X reports as a DPU. A device type the integration doesn't recognize shows only the port, e.g. "S.I.B. AC1"; a diagnostics download shows its type code (`inlet_acN_type`), so please open an issue with one.
- **Inlet port activity** only arrives with the gateway's live updates, not with the full snapshots, so it can take a minute to show after a restart. Its values were seen with DPUs supplying the house, idle and grid charging; an EV charger may report others.
- The backup reserve's minimum and the charging power's minimum and step size are assumptions; the app's own limits win if they differ.
- With the EcoFlow app closed the gateway only pushes updates every few minutes, so the integration requests a full snapshot every 30 seconds.

## DELTA Pro Ultra X

The DELTA Pro Ultra X (app connection, device type `DELTA_PRO_ULTRA_X`) gets these diagnostic sensors on top of its existing ones. Together they show the moment its AC output stops, which the power sensors alone can't tell apart from a quiet house:

| Sensor | What it shows |
|---|---|
| AC Port Voltage | Voltage at its AC port: about 241 V while it supplies the house, about 0 V while its output is off. Once the gateway has switched the backup side to the grid, it reads the grid voltage (about 245 V) instead. |
| AC Output State | "Supplying" or "Not supplying". The raw value (2 or 1) is an attribute. |
| Energy Backup State | 0 while supplying, 2 while not (the DPU X's own backup flag). |
| AC Port Current | Disabled by default. Follows the load, but reads 15–40% above output power ÷ voltage and about 4 A even while idle on grid, so it isn't a measure of real power. |
| Telemetry Stalled, Data Stale | See below. |

These were decoded from captures of a DPU X on a Smart Gateway through two unexplained drops of its output and a normal fallback to grid at the backup reserve. In all three, AC Output State and Energy Backup State changed within half a second of the voltage, and the error code stayed 0.

An automation can flag a drop with a trigger on **AC Output State** changing to "Not supplying" (or **AC Port Voltage** below 50 V), with conditions that the gateway is in Self-Powered mode and the battery is above the backup reserve.

The DPU X also sends two or three messages bundled together about once a minute. The integration used to read only the first, so some updates, including the per-leg output power, were dropped. All of them are read now.

## Telemetry stalls (Smart Gateway and DELTA Pro Ultra X)

The Smart Gateway and the DPU X push an update about every 2 seconds. Captures from a gateway showed it going silent for 28 seconds to 4.4 minutes. During the long silence, its replies to the integration's 30-second refresh kept arriving but carried a frozen copy of its last update, and two came back empty. Home Assistant kept showing the frozen values, with the device online, for the whole time.

Both devices now have two diagnostic binary sensors:

- **Telemetry Stalled** turns on after 15 seconds without an update. Its attributes show when the stall started, the last stall's length, and what the refreshes got back.
- **Data Stale** turns on while stalled and nothing new has arrived for 15 seconds. A refresh reply that only repeats values already known is no longer treated as new data.

For the gateway, the integration also asks for a fresh snapshot 10 seconds into a stall and then every 5 seconds for up to 3 minutes, instead of waiting for the next 30-second refresh. If the gateway reports that it has dropped to its slow update rate (a **Reporting Interval** above 2 s, seen after it reconnects to EcoFlow's cloud), it asks straight away, which puts it back on 2-second updates.

Each stall fires the Home Assistant events `ecoflow_cloud_stream_stalled` and `ecoflow_cloud_stream_resumed` (with the serial, name, start time, duration and refresh counts), for automations or the logbook.

## DELTA Pro Ultra per-battery sensors

### Why

The DELTA Pro Ultra reports a full battery-management record for every battery in the stack, but the integration only exposed stack-level values (one charge level for the whole stack). That made it impossible to see how individual batteries behave, for example when a replacement or refurbished battery is mixed in with older ones.

### What it adds

One set of sensors per installed battery, on the existing DPU device:

| Sensor | Unit | Updates | Default |
|---|---|---|---|
| Level | % | every minute | On |
| Charge Power / Discharge Power | W | every minute | On |
| Temperature | °C | every minute | On |
| Current (negative while discharging) | A | every 5 min | On |
| State of Health | % | every 3 min | On |
| Position (slot in the stack, 1–5) | – | every 5 min | On (diagnostic) |
| Cycles | – | every 5 min | On (diagnostic) |
| Cell Voltage Difference (highest minus lowest cell) | mV | every 3 min | On (diagnostic) |
| Max / Min Cell Temperature, Board and Shunt Temperature | °C | every 3–5 min | On (diagnostic) |
| Cell Temperature 1–7, MOSFET Temperature 1–4, Heater Temperature 1–4 | °C | every 5 min | On (diagnostic) |
| Min Cell Volts / Max Cell Volts | V | every 3 min | Off |
| Full Capacity / Remaining Capacity | mAh | every 3 min | Off |

Sensors are named by the last four digits of the battery's serial number, for example `DPU 1 Battery 4028 Level`. If two batteries in the same DPU share those four digits, both use their full serial instead.

### Where the data comes from

The inverter reports per-battery data three ways, and each sensor shows whichever was reported most recently:

- **A per-slot summary from the inverter** every minute: level, charge/discharge power and temperature for each slot.
- **Each battery's own report** every 3 minutes per battery: most battery-management values, identified by serial number.
- **EcoFlow's cloud record**, polled every 5 minutes: the complete record, including current, cycle count and every temperature probe. It's EcoFlow's copy of each battery's latest report, so it can already be a few minutes old when it arrives. Newer live values are never overwritten by it.

### Batteries are tracked by serial number, not slot

Each sensor is tied to its battery's serial number. On every update it looks up which slot currently holds that battery, so if you swap batteries between positions, each battery's history goes with it. The Position sensor shows where the battery sits now.

### Notes

- **Charge Power isn't always solar or grid.** A battery can charge from the other batteries in its stack while the inverter has no input at all. The inverter does this to even out their levels.
- **Battery power is measured on the battery side.** Discharge Power across a stack adds up to more than the inverter's AC output, because of conversion losses.
- **Stale records are ignored.** EcoFlow's cloud can keep returning an old record for a slot that no longer holds a battery, sometimes repeating the serial of a battery that has moved. Only slots up to the installed battery count are read, and if a serial still appears twice, the newest record wins.
- **Adding a battery:** a newly added battery gets its sensors after the integration is reloaded or Home Assistant restarts.
- **Moving between inverters:** a battery moved to a different DPU starts a new set of sensors under that DPU.
- **DELTA Pro Ultra X:** unaffected. It uses a separate part of the integration that already offers per-battery level and temperature by position. Its battery data doesn't include a serial number, so this approach doesn't apply there.

## River 3 and River 3 Plus

### What was wrong

A River 3 only ever showed its power readings: battery level, charge limits and every switch and select stayed unknown. The River 3 ignores the request the integration used for a full status update, so only values that changed after startup ever arrived. This fork sends the request the River 3 does answer, so everything fills in within a minute or two of a restart. It also:

- fixes **DC (12V) Enabled**, which never showed a state
- stops the "can't decode" error lines logged each time the EcoFlow app opens the device page

### Adding a River 3 or River 3 Plus

The integration supports the River 3 only through the EcoFlow app connection (the developer API refuses a River 3 Plus). In the integration, add the device to an entry with your **EcoFlow account** (email and password), choose the device type **`RIVER_3`** for either model (there is no separate Plus type), and enter the serial number from the EcoFlow app.

### River 3 Plus

A River 3 Plus is recognized by its serial number (starting `R631`, `R634` or `R635`) and gets these differences:

| | River 3 (as upstream) | River 3 Plus |
|---|---|---|
| AC Charging Power | 50–305 W | 50 W up to the maximum the unit reports (650 W on a US unit) |
| LED Mode | – | Off, Dim, Bright or SOS |
| Beeper | Yes | No (the EcoFlow app has no beeper setting for the Plus) |
| AC In Volts, PCS AC / DC Temperature | Yes | No (see below) |
| State of Health | Yes | Yes, read from the field the Plus reports in its regular updates |

The Plus only sends AC input voltage and the PCS temperatures when the EcoFlow app asks for a full snapshot, and those values don't change between snapshots (identical readings about 45 minutes apart while the live readings moved on). As sensors they would show old values as if current, so the Plus doesn't get them.

The AC charging slider moves in 50 W steps, the River 3 default. For finer control, set a smaller **power step** in the device's options (Configure on the integration entry).

### Tested with, and limits

- Tested on one River 3 Plus (US model, no extra battery, firmware as of October 2026). The status fix should apply to every River 3 model (it uses the same request as the DELTA 3), but hasn't been tested on a base River 3. On a base River 3, AC In Volts, the PCS temperatures and State of Health may have the same stale-data problem as on the Plus; please open an issue with a diagnostics download if you can check.
- Extra batteries (EB300 / EB600) aren't shown separately.
- Settings changed in the EcoFlow app show up in Home Assistant within seconds.

## Fewer recorder writes

EcoFlow devices push readings every few seconds and the values jitter, so nearly every update became a new state and a recorder row (a single Smart Gateway circuit wrote more than 10,000 rows a day). This fork, for every device:

- **Power sensors** publish straight away when a reading moves by at least 5 W or 2% (whichever is larger) or starts / stops, and otherwise at least once a minute.
- **Remaining-time sensors** publish when they move by at least 5 minutes or 5%, and otherwise at least every 5 minutes.
- **kWh totals** still integrate every reading, but write their state at most once a minute.

Graphs, 5-minute statistics and energy totals stay accurate (within a few Wh a day in testing).

## Decoding a debug capture

`scripts/decode_capture.py` reads a Home Assistant log with DEBUG enabled for `custom_components.ecoflow_cloud.api.ecoflow_mqtt` and decodes every EcoFlow message in it: each header (source, destination, command, sequence) and the payload's fields by number. It needs only Python 3.10 or later, not Home Assistant.

```
python3 scripts/decode_capture.py home-assistant.log                               # every frame, changed fields only
python3 scripts/decode_capture.py home-assistant.log --sn P101 --fields 4,6,120,756 # chosen fields of one device
python3 scripts/decode_capture.py home-assistant.log --gaps 10                      # update pauses over 10 s, repeated snapshots
python3 scripts/decode_capture.py home-assistant.log --from 22:36:50 --to 22:37:30
```

To enable the DEBUG log, add this to `configuration.yaml` and restart:

```yaml
logger:
  default: warning
  logs:
    custom_components.ecoflow_cloud.api.ecoflow_mqtt: debug
```
