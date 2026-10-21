import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.binary_sensor import stream_watch_binary_sensors
from custom_components.ecoflow_cloud.devices import const
from custom_components.ecoflow_cloud.devices.internal.delta_pro_3 import DeltaPro3
from custom_components.ecoflow_cloud.devices.internal.smart_home_panel_3 import (
    WIRE_F32,
    WIRE_LEN,
    WIRE_VARINT,
    _first,
    _parse_fields,
)
from custom_components.ecoflow_cloud.devices.internal.proto import (
    ef_delta_pro_ultra_x_pb2 as dpux,
)
from custom_components.ecoflow_cloud.sensor import (
    AmpSensorEntity,
    BatteryLimitSensorEntity,
    FirmwareVersionSensorEntity,
    FrequencySensorEntity,
    InRawVoltSolarSensorEntity,
    InWattsSensorEntity,
    LevelSensorEntity,
    MiscSensorEntity,
    OutWattsSensorEntity,
    QuotaStatusSensorEntity,
    RemainSensorEntity,
    StateOfHealthSensorEntity,
    TempSensorEntity,
    VoltSensorEntity,
)

_LOGGER = logging.getLogger(__name__)

# The DPU X supports up to 10 external battery packs.
MAX_PACKS = 10

# DisplayPropertyUpload (254/21) fields absent from the DP3 proto, read with a
# raw pass. From captures of a DPU X on a Smart Gateway (inlet AC3), with the
# house on battery, through two drops of its AC output and a normal fallback to
# grid at the backup reserve:
#   120  AC port voltage, V (F32): ~241 while it supplies the house, ~0 while
#        its output is off; reads the grid (~245) once the gateway has
#        switched the backup side to grid. Sent when it changes.
#   121  AC port current, A (F32): follows the load, but runs 15-40% above
#        output power / voltage and reads ~4.4 A while idle on grid, so it is
#        not a measure of real power.
#   756  2 while the DPU X supplies the house from its batteries, 1 otherwise:
#        in those drops and when on grid, but also during solar surplus,
#        while it supplies the house at ~241 V from solar alone (seen on the
#        same DPU X since). So it seems to follow battery discharge, not the
#        AC output; the port voltage is the reliable sign of the output.
#        Flipped with energy_backup_state (field 6: 0 / 2) within ~0.3 s in
#        the drops.
F_AC_PORT_VOL = 120
F_AC_PORT_AMP = 121
F_AC_OUTPUT_STATE = 756
AC_OUTPUT_STATES: dict[int, str] = {1: "Not supplying", 2: "Supplying"}

# Per-pack array (field 786), sent whenever a pack's values change: each
# entry's sub-field 2 is the bay, 3 the pack's state (0 when absent), which the
# proto doesn't name. Confirmed on a 10-pack DPU X: 1 = discharging, 0 = idle
# (it draws from the fuller packs first), 2 = charging (from solar, emptiest
# packs first, or from fuller packs while it balances them, which it also does
# while supplying the house). Exposed as one digit per populated bay in bay
# order, with each pack's state spelled out as an attribute.
F_PACKS = 786
PACK_STATES: dict[int, str] = {0: "Idle", 1: "Discharging", 2: "Charging"}


class _PhaseWattsSensorEntity(InWattsSensorEntity):
    """Reports phase power (W) by multiplying a voltage key with an amperage key."""

    def __init__(
        self,
        client: EcoflowApiClient,
        device: Any,
        volt_key: str,
        amp_key: str,
        title: str,
        enabled: bool = False,
    ) -> None:
        super().__init__(client, device, volt_key, title, enabled)
        self._amp_key = amp_key
        self._attr_unique_id += f"-{amp_key.replace('_', '-')}-watts"

    def _update_value(self, val: Any) -> bool:
        amp_val = self._device.data.params.get(self._amp_key, 0.0)
        return super()._update_value(float(val) * float(amp_val))


class _ChargingStateTextEntity(MiscSensorEntity):
    """Translates cms_chg_dsg_state (int) into a descriptive text label."""

    _LABELS: ClassVar[dict[int, str]] = {0: "Not Charging", 1: "Discharging", 2: "Charging"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._attr_unique_id += "-text"

    def _update_value(self, val: Any) -> bool:
        return super()._update_value(self._LABELS.get(int(val), "Unknown"))


class _ReportedTempSensorEntity(TempSensorEntity):
    """Unknown until the DPU X reports a value, instead of 0 °C (32 °F) after
    every start: most temperatures only come in the runtime upload, a few
    minutes apart. With zero_unreported, 0 also counts as not reported: the
    DPU X sends its PCS AC/DC temperatures as 0 in every frame."""

    _attr_native_value = None  # type: ignore[assignment]  # TempSensorEntity starts at 0

    def __init__(self, *args: Any, zero_unreported: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._zero_unreported = zero_unreported

    def _update_value(self, val: Any) -> bool:
        if self._zero_unreported and val == 0:
            return False
        return super()._update_value(val)


class _ReportedVoltSensorEntity(VoltSensorEntity):
    """Unknown until the DPU X reports a value, instead of 0 V after every
    start (its AC port voltage only comes when it changes, or with the full
    report every ~2 min)."""

    _attr_default_value = None


class _ReportedAmpSensorEntity(AmpSensorEntity):
    """As _ReportedVoltSensorEntity, for the AC port current."""

    _attr_default_value = None


class _PackStatesEntity(MiscSensorEntity):
    """Per-pack states (field 786) as digits in bay order, e.g. "1111122022",
    with each populated bay's state spelled out as a battery_<bay> attribute."""

    _attr_icon = "mdi:battery-sync"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for n in range(1, MAX_PACKS + 1):
            self.attr(f"bp_{n}_state", f"battery_{n}", None)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        attrs = super().extra_state_attributes or {}
        return {k: PACK_STATES.get(v, f"Unknown ({v})") for k, v in attrs.items() if v is not None}


class _AcOutputStateEntity(MiscSensorEntity):
    """Field 756 as text ("Supplying" / "Not supplying"), raw value as an attribute."""

    def _update_value(self, val: Any) -> bool:
        return super()._update_value(AC_OUTPUT_STATES.get(int(val), f"Unknown ({int(val)})"))


class DeltaProUltraX(DeltaPro3):
    """DELTA Pro Ultra X (private / app API).

    Speaks the Delta Pro 3 protobuf dialect (same header, cmdFunc/cmdId routing
    and DisplayPropertyUpload field numbers), so the DP3 decode pipeline is
    inherited unchanged. Overrides only the entity set: the DPU X has no internal
    main battery (energy is in external packs), so bms_* main-battery sensors are
    dropped and cms_batt_soc is the headline. Control entities are suppressed for
    now — read-only until actuation is deliberately in scope.

    Its push stream is watched (see stream_watch.py): it normally pushes every
    ~2 s, so a 15 s gap shows as "Telemetry Stalled". It isn't asked for
    snapshots: the stall flags only report.
    """

    STREAM_STALL_SEC = 15.0

    @override
    def sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        # Enabled-by-default = the at-a-glance headline set; everything else is
        # registered but disabled by default (`False`).
        return [
            # --- Headline (enabled) ---
            LevelSensorEntity(client, self, "cms_batt_soc", const.COMBINED_BATTERY_LEVEL),
            # Total in/out power with integrated energy (kWh, total_increasing)
            # = charge / discharge energy for the HA Energy dashboard's battery
            # slot. (Native accu_chg/dsg_energy don't populate for the DPU X's
            # external-pack topology, so integrate here.)
            InWattsSensorEntity(client, self, "pow_in_sum_w", const.TOTAL_IN_POWER).with_energy(),
            OutWattsSensorEntity(client, self, "pow_out_sum_w", const.TOTAL_OUT_POWER).with_energy(),
            RemainSensorEntity(client, self, "cms_chg_rem_time", const.CHARGE_REMAINING_TIME),
            RemainSensorEntity(client, self, "cms_dsg_rem_time", const.DISCHARGE_REMAINING_TIME),
            QuotaStatusSensorEntity(client, self),
            # --- AC output (diagnostic) ---
            # Whether the DPU X is actually supplying the house: the voltage at
            # its AC port drops to ~0 and 756 / energy_backup_state flip when
            # its output stops, which the power sensors alone can't tell from
            # a quiet load.
            _ReportedVoltSensorEntity(client, self, "ac_port_vol", "AC Port Voltage", diagnostic=True),
            _AcOutputStateEntity(client, self, "ac_output_state", "AC Output State").attr(
                "ac_output_state", "raw_value", None
            ),
            MiscSensorEntity(client, self, "energy_backup_state", "Energy Backup State"),
            _ReportedAmpSensorEntity(client, self, "ac_port_amp", "AC Port Current", False, diagnostic=True),
            # Per-pack states, e.g. "1111122022", plus battery_<bay> attributes (see F_PACKS).
            _PackStatesEntity(client, self, "bp_states", "Battery Pack States", False),
            # --- Detail (disabled by default) ---
            # Per-pack SoC (field 786), all 10 bays; unpopulated bays stay
            # unavailable.
            *[
                LevelSensorEntity(client, self, f"bp_{n}_soc", const.BATTERY_N_LEVEL % n, False)
                for n in range(1, MAX_PACKS + 1)
            ],
            # AC input: DPU X does not send pow_get_ac_in; expose per-phase V/A instead.
            VoltSensorEntity(client, self, "plug_in_info_l1_vol", "AC Input L1 Voltage", False),
            AmpSensorEntity(client, self, "plug_in_info_l1_amp", "AC Input L1 Current", False),
            _PhaseWattsSensorEntity(client, self, "plug_in_info_l1_vol", "plug_in_info_l1_amp", "AC Input L1 Power", False),
            VoltSensorEntity(client, self, "plug_in_info_l2_vol", "AC Input L2 Voltage", False),
            AmpSensorEntity(client, self, "plug_in_info_l2_amp", "AC Input L2 Current", False),
            _PhaseWattsSensorEntity(client, self, "plug_in_info_l2_vol", "plug_in_info_l2_amp", "AC Input L2 Power", False),
            OutWattsSensorEntity(client, self, "pow_get_ac", const.AC_OUT_POWER, False),
            # HV/LV = the 240V (line-to-line) and 120V (line-to-neutral) rails of the
            # split-phase output — the X has no HV/LV concept; name by voltage.
            OutWattsSensorEntity(client, self, "pow_get_ac_hv_out", "AC 240V Output Power", False),
            OutWattsSensorEntity(client, self, "pow_get_ac_lv_out", "AC 120V Output Power", False),
            # Two symmetric high-voltage PV inputs (80-500 V, 5 kW each) — not HV/LV
            # like the non-X Delta Pro Ultra; name them as circuits 1/2 per the manual.
            InRawVoltSolarSensorEntity(client, self, "pv_vin_ref", const.SOLAR_1_IN_VOLTS, False),
            InRawVoltSolarSensorEntity(client, self, "pv2_vin_ref", const.SOLAR_2_IN_VOLTS, False),
            FrequencySensorEntity(client, self, "ac_out_freq", "AC Output Frequency", False),
            # Per-phase output power, signed to preserve direction.
            OutWattsSensorEntity(client, self, "pow_get_l1", "AC Output Power L1", False),
            OutWattsSensorEntity(client, self, "pow_get_l2", "AC Output Power L2", False),
            # SoC limits — config thresholds, not remaining charge (no BATTERY device class).
            BatteryLimitSensorEntity(client, self, "cms_max_chg_soc", const.MAX_CHARGE_LEVEL, False),
            BatteryLimitSensorEntity(client, self, "cms_min_dsg_soc", const.MIN_DISCHARGE_LEVEL, False),
            *[
                _ReportedTempSensorEntity(client, self, f"bp_{n}_temp", const.BATTERY_N_TEMP % n, False)
                for n in range(1, MAX_PACKS + 1)
            ],
            # Inverter / LLC / PCS temperatures.
            _ReportedTempSensorEntity(client, self, "inv_ntc_temp2", "Inverter Temperature 2", False),
            _ReportedTempSensorEntity(client, self, "inv_ntc_temp3", "Inverter Temperature 3", False),
            _ReportedTempSensorEntity(client, self, "llc_ntc_temp", "LLC Temperature", False),
            _ReportedTempSensorEntity(client, self, "temp_pcs_ac", "PCS AC Temperature", False, zero_unreported=True),
            _ReportedTempSensorEntity(client, self, "temp_pcs_dc", "PCS DC Temperature", False, zero_unreported=True),
            # Power conversion bus metrics.
            VoltSensorEntity(client, self, "llc_bat_vol", "Battery Bus Voltage", False),
            AmpSensorEntity(client, self, "llc_bat_cur", "Battery Bus Current", False),
            VoltSensorEntity(client, self, "cms_batt_vol", "CMS Battery Voltage", False),
            AmpSensorEntity(client, self, "cms_batt_amp", "CMS Battery Current", False),
            StateOfHealthSensorEntity(client, self, "cms_batt_soh", "Battery State of Health", False),
            VoltSensorEntity(client, self, "mppt_bat_vol", "MPPT Battery Voltage", False),
            AmpSensorEntity(client, self, "mppt_bat_amp", "MPPT Battery Current", False),
            VoltSensorEntity(client, self, "inv_bus_vol", "Inverter Bus Voltage", False),
            # Charging state (0=idle, 1=discharging, 2=charging per DP3 proto).
            MiscSensorEntity(client, self, "cms_chg_dsg_state", "Charging State", False),
            _ChargingStateTextEntity(client, self, "cms_chg_dsg_state", "Charging State Text", False),
            # MPPT pause-event counters.
            MiscSensorEntity(client, self, "pv_pause_cnt", "Solar 1 MPPT Pause Count", False),
            MiscSensorEntity(client, self, "pv2_pause_cnt", "Solar 2 MPPT Pause Count", False),
            # Firmware / hardware versions (diagnostic), as a.b.c.d. PD and LLC
            # come in the full runtime upload (every ~5 min); BMS and IoT have
            # been 0 (not reported) in every capture so far, so they stay unknown.
            FirmwareVersionSensorEntity(client, self, "bms_firm_ver", "BMS Firmware Version", False),
            FirmwareVersionSensorEntity(client, self, "pd_firm_ver", "PD Firmware Version", False),
            FirmwareVersionSensorEntity(client, self, "llc_firm_ver", "LLC Firmware Version", False),
            FirmwareVersionSensorEntity(client, self, "iot_firm_ver", "IoT Firmware Version", False),
            MiscSensorEntity(client, self, "mppt_hardware_ver", "MPPT Hardware Version", False),
            # Error codes (diagnostic).
            MiscSensorEntity(client, self, "errcode", "Error Code", False),
            MiscSensorEntity(client, self, "bms_err_code", "BMS Error Code", False),
            MiscSensorEntity(client, self, "mppt_err_code", "MPPT Error Code", False),
            MiscSensorEntity(client, self, "pd_err_code", "PD Error Code", False),
        ]

    @override
    def numbers(self, client: EcoflowApiClient) -> list[NumberEntity]:
        return []

    @override
    def switches(self, client: EcoflowApiClient) -> list[SwitchEntity]:
        return []

    @override
    def selects(self, client: EcoflowApiClient) -> list[SelectEntity]:
        return []

    @override
    def binary_sensors(self, client: EcoflowApiClient) -> Sequence[BinarySensorEntity]:
        return stream_watch_binary_sensors(client, self)

    @override
    def _decode_message_by_type(self, pdata: bytes, header_info: dict[str, Any]) -> dict[str, Any]:
        # DP3 decode drops the per-pack array (DisplayPropertyUpload field 786,
        # absent from its proto). Recover it with a second pass over the same
        # payload and inject flat bp_<bay>_soc / bp_<bay>_temp keys.
        result = super()._decode_message_by_type(pdata, header_info)
        if header_info.get("cmdFunc") == 254 and header_info.get("cmdId") == 21:
            try:
                extra = dpux.DPUXDisplayPropertyExtra()
                extra.ParseFromString(pdata)
                for pack in extra.bp_info.packs:
                    if not pack.HasField("bay"):
                        continue
                    result[f"bp_{pack.bay}_soc"] = pack.soc
                    result[f"bp_{pack.bay}_temp"] = pack.temp
            except Exception as e:
                _LOGGER.debug("DPU X per-pack (field 786) parse skipped: %s", e)
            # AC port fields (see F_AC_PORT_VOL...): written only when present,
            # as incremental frames carry only what changed.
            fields = _parse_fields(pdata)
            states = self._pack_states(fields)
            if states:
                result["bp_states"] = "".join(str(states[bay]) for bay in sorted(states))
                for bay, state in states.items():
                    result[f"bp_{bay}_state"] = state
            for key, no, wire in (
                ("ac_port_vol", F_AC_PORT_VOL, WIRE_F32),
                ("ac_port_amp", F_AC_PORT_AMP, WIRE_F32),
                ("ac_output_state", F_AC_OUTPUT_STATE, WIRE_VARINT),
            ):
                v = _first(fields, no, wire)
                if v is not None:
                    result[key] = round(v, 3) if wire == WIRE_F32 else v
        return result

    @staticmethod
    def _pack_states(fields: Any) -> dict[int, int]:
        """Field 786's per-pack states, {bay: state}, for the populated bays."""
        block = _first(fields, F_PACKS, WIRE_LEN)
        if block is None:
            return {}
        packs: dict[int, int] = {}
        for wire, entry in _parse_fields(block).get(1, []):
            if wire != WIRE_LEN:
                continue
            pack = _parse_fields(entry)
            bay = _first(pack, 2, WIRE_VARINT)
            if bay:
                packs[bay] = _first(pack, 3, WIRE_VARINT) or 0
        return packs
