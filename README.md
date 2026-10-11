## About this fork

This fork of [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) adds:

- **[EcoFlow Smart Gateway](#ecoflow-smart-gateway) support:** monitoring and control of the 200A Smart Gateway (HR65) and its paired DELTA Pro Ultras.
- **[Per-battery sensors](#delta-pro-ultra-per-battery-sensors) for the EcoFlow DELTA Pro Ultra** (developer API connection). Submitted upstream as [pull request #927](https://github.com/tolwi/hassio-ecoflow-cloud/pull/927).
- **[Fewer recorder writes](#fewer-recorder-writes)** from power, remaining-time and energy sensors, for every device.

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
- **Inlet port names** use the attached device's name in Home Assistant when it's also set up in this integration (matched by serial number), otherwise its type ("DPU", "EV Charger"). A device type the integration doesn't recognize (possibly the DELTA Pro Ultra X) shows only the port, e.g. "S.I.B. AC1"; a diagnostics download shows its type code (`inlet_acN_type`), so please open an issue with one.
- The backup reserve's minimum and the charging power's minimum and step size are assumptions; the app's own limits win if they differ.
- With the EcoFlow app closed the gateway only pushes updates every few minutes, so the integration requests a full snapshot every 30 seconds.

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

## Fewer recorder writes

EcoFlow devices push readings every few seconds and the values jitter, so nearly every update became a new state and a recorder row (a single Smart Gateway circuit wrote more than 10,000 rows a day). This fork, for every device:

- **Power sensors** publish straight away when a reading moves by at least 5 W or 2% (whichever is larger) or starts / stops, and otherwise at least once a minute.
- **Remaining-time sensors** publish when they move by at least 5 minutes or 5%, and otherwise at least every 5 minutes.
- **kWh totals** still integrate every reading, but write their state at most once a minute.

Graphs, 5-minute statistics and energy totals stay accurate (within a few Wh a day in testing).
