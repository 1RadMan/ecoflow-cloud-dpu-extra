## About this fork

This fork of [tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud) adds per-battery sensors for the **EcoFlow DELTA Pro Ultra** (developer API connection). Everything else is identical to upstream. The change has been submitted upstream as [pull request #927](https://github.com/tolwi/hassio-ecoflow-cloud/pull/927); until it's merged there, install this fork to get the sensors. For general setup and the other supported devices, see the [upstream README](https://github.com/tolwi/hassio-ecoflow-cloud#readme).

### Installing this fork

1. In HACS, open the ⋮ menu → **Custom repositories**, add `https://github.com/1RadMan/ecoflow-cloud-dpu-extra` with type **Integration**.
2. If you already have the upstream EcoFlow Cloud integration from HACS, remove it **in HACS** first (not under Settings → Integrations). Both use the same integration, so your EcoFlow configuration and existing entities carry over.
3. Download **EcoFlowCloud** from this fork's repository and restart Home Assistant.

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
