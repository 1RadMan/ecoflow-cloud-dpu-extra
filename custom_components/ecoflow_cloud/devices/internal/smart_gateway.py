"""EcoFlow Smart Gateway (200A) — internal / app-API device profile.

The Smart Gateway (HR65…) is a whole-home transfer panel with 6 smart circuits
that pairs with up to three DELTA Pro Ultra units. It speaks the Smart Home
Panel 3 dialect — same DP3 header, DisplayPropertyUpload = 254/21, the same
per-circuit array at fields 1015.. and the same circuit label/240V-link blocks
at 794.. — so it subclasses SmartHomePanel3 with 6 circuits.

What differs is the meaning of the aggregate flow fields. On the SHP3, field 516
is the home load; on the gateway it is only the smart-circuit load, and the
storage/solar/home figures live in fields the SHP3 profile doesn't read. Field
map, checked against live frames from a gateway paired with two DELTA Pro
Ultras, both with the house on battery/solar (grid ~0) and with the gateway
switched to grid (house on grid, DPUs idle), every identity holding to within
a few watts:

  515  grid active power, W (F32)            == grid L1 + L2 (962 + 963)
  516  smart-circuit load, W (F32)           == sum of circuits 1..6 power
  517  solar input, all paired DPUs, W (F32) == sum of per-DPU PV (1010+.9),
                                                matches each DPU's own PV sensor
  518  battery net charge, W (F32, +charge)  == 517 - DPU output
  1544 home load, all sources, W (int)       == grid + DPU output
  1547 load excluding smart circuits, W (int) == 1544 - 516
  DPU output (computed) = 1544 - 515         == -sum of per-DPU AC (1010+.8)

Solar is DC-coupled (the panels feed the DPUs), so DPU output is solar and
battery combined; the battery's own share is the charging/discharging split
of 518.
  956/957  grid L1/L2 voltage (F32)          (same fields as SHP3)
  958/959  grid L1/L2 current (F32)
  962/963  grid L1/L2 active power (F32)
  1063/1064 backup-side L1/L2 voltage (F32)
  461  backup reserve, % (int)               matches the paired DPUs' setting

Frame semantics: the gateway sends periodic full frames (zeros encoded
explicitly) and, in between, incremental frames that carry only fields whose
value changed. An absent field therefore means "unchanged" here, not 0, so
every flow is written only when present (unlike the SHP3, whose incremental
frames rely on zero-omission for grid import).

240V circuits: the gateway reports a 240V circuit as two linked 120V legs (all
six circuit entries always arrive in the same frame). Each 240V pair is exposed
once, on its lower-numbered leg: power (and its energy) is the sum of both legs,
voltage is line-to-line (L1 + L2) and current is the more heavily loaded leg.
The other leg gets no entities. Which circuits are paired comes from the
gateway's circuit metadata (sub-field 2 = {1: 1, 2: partner}, absent when
unpaired), persisted across restarts, so any mix of 120V circuits and 240V
pairs is handled. When the reported pairing differs from what the entities
were built for (first setup, before the gateway has reported it; or circuits
paired/split in the app), the entry reloads itself, and entities left on a
pair's second leg are removed from the registry at startup.

Refresh: with the EcoFlow app closed, the gateway only pushes an update every
few minutes. Every QUOTA_REFRESH_SEC the profile sends the request the app sends
when a device page is opened (one empty DP3 header, as for the DELTA 3), and the
gateway answers on get_reply with a multi-header snapshot, decoded here through
the same pipeline as live frames.

Operating mode: field 1245 reports the energy strategy as a sub-message of
per-mode enable flags; sub-field 1 is Self-Powered, and Backup is the message
with no flag set. The app changes it with a single ConfigWrite (254/17) to the
gateway (dest 11) carrying the same sub-message in field 544: {1: 1} for
Self-Powered, empty for Backup. Captured from the app switching Self-Powered ->
Backup -> Self-Powered; 1245 followed each write within 150 ms. Other modes
(TOU etc.) are reported as an unknown mode and can't be selected from here.

Commands sent to the device (get/set topics, including our own, which the
integration also receives) are kept as raw hex and not decoded: they carry no
device state, and some carry no payload at all. The EcoFlow app's control
commands (header "from" = "ios"/"android") are read only to add a logbook line
on the control they change ("... in the EcoFlow app (iOS)"), since the state
change itself carries no origin. Commands from other accounts sharing the
gateway arrive on their own topics and aren't seen.

Circuit control: each circuit's metadata block (794..799, see above) also
carries its state: sub-field 8 = 1 while the circuit is switched off (absent
when on), sub-field 6 = the source setting (1 Auto, 2 Grid), and sub-field 1 =
the source it is actually on (0 off, 1 grid, 2 battery). The app changes a
circuit with a ConfigWrite carrying field 386 + n = {1: 1 on / 2 off, 2: 1 Auto
/ 2 Grid} (always both values), and writes a 240V circuit as both legs in one
command. Captured from the app switching circuit 2 and the 14-50 pair (3/4)
off, on, to Grid and back to Auto; the metadata followed each write within
about a second.

Backup reserve: field 461 reports it; the app sets it with a ConfigWrite
carrying field 102 = the percentage (a varint, the same field the DELTA 3 uses
for its backup reserve). Captured from the app setting 10 -> 11 -> 10%; 461
followed each write within about 1.2 s. In Backup mode, raising the reserve
above the batteries' charge makes the paired DPUs charge from the grid up to
it (seen at ~3.7 kW for two DPUs); the gateway stops at the reserve by itself.
The app's maximum is 70%, so the control's is too.

Smart Inlet Box ports (AC1..AC3, i = 0..2), where the DPUs and an EV charger
connect: field 1224 + i reports each port, sub-field 1 = its power setting (1
on, 2 off), 2 = what's attached (1 DPU, 3 EV charger; absent while nothing is),
3 = its bus address (AC1 1 with the EV charger, 5 with a DPU; AC2 6; AC3 7),
4 = Charge Now (1 on, 2 off), 5 = 1 while something is attached / 2 while not,
6 = seemingly what the port is doing (seen as 1 off, 2 idle, 3 supplying the
house, 4 grid charging). The app switches a port with a ConfigWrite carrying field 535 + i =
{1: 1} on / {1: 2, 2: 2} off (switching off also clears Charge Now), and its
Charge Now with the same field = {1: 1, 2: 1} on (which also switches the port
on) / {2: 2} off, sending Backup (544, empty) alongside; so both are read
together for the logbook. Switching a port off only stops the gateway drawing
AC from what's attached: a DPU stays on and keeps charging from solar. The
inlet box's own port buttons are reported the same way, and the gateway
switches a port off by itself when its device is unplugged and back on once it
recognises a new one. Captured from the app switching each port off and on
(AC1 with an EV charger and with a DPU, AC2/AC3 with DPUs) and turning Charge
Now on and off on all three; 1224 + i followed each write within 1-3 s (Charge
Now within ~10 s). With Charge Now on, the DPU's AC input rose to ~1.9 kW
within seconds.

The attached device's serial is sub-field 3 of field 1009 + i for a DPU (seen
for AC1 as 1009, AC2/AC3 as 1010/1011) and of field 1255 + i for an EV charger
(rating, 9600 W, in sub-field 5). These blocks can keep a serial after the
device is unplugged, so a port with no device type has no device. A port's
controls are named after the attached device (its name in HA when the serial
matches an EcoFlow device there, else its type) and are unavailable while
nothing is attached. Charge Now on AC2/AC3 keeps the state keys from when it
was exposed per DPU slot ("DPU 1/2 Charge Now"), so those entities keep their
unique IDs.

Battery charging power ("System device settings > Battery charging" in the
app): the grid-charging power limit, in W, applied to each paired DPU. Reported
in field 1244 (varint); the app sets it with a ConfigWrite carrying field 542 =
the watts. Captured from the app setting 2000 -> 12000 -> 5000 W; 1244 followed
each write within ~0.1 s. With it at 5000 W and Charge Now on for two DPUs,
grid draw rose by ~10 kW (2 x 5 kW); at 2000 W the two DPUs drew ~1.9 kW each.

All gateway controls check that the gateway reports the new state, and send
the command once more if it hasn't within CONFIRM_TIMEOUT_SEC (see
_ConfirmedCommand).

The frames also carry the site's configured location (fields 1236/1238/1240);
it is deliberately never surfaced.
"""

import asyncio
import json
import logging
from functools import partial
from typing import Any, ClassVar, Self, override

from google.protobuf.json_format import MessageToDict
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.storage import Store

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.api.message import Message, PrivateAPIMessageProtocol
from custom_components.ecoflow_cloud.devices import const
from custom_components.ecoflow_cloud.devices.data_holder import PreparedData
from custom_components.ecoflow_cloud.devices.internal.proto import ef_dp3_iobroker_pb2 as dp3
from custom_components.ecoflow_cloud.devices.internal.smart_home_panel_3 import (
    WIRE_F32,
    WIRE_LEN,
    WIRE_VARINT,
    FieldMap,
    SmartHomePanel3,
    _CircuitNamed,
    _first,
    _parse_fields,
)
from custom_components.ecoflow_cloud.number import ValueUpdateEntity
from custom_components.ecoflow_cloud.select import DictSelectEntity
from custom_components.ecoflow_cloud.sensor import (
    BatteryLimitSensorEntity,
    InAmpSensorEntity,
    InVoltSensorEntity,
    InWattsSensorEntity,
    LevelSensorEntity,
    OutVoltSensorEntity,
    QuotaScheduledStatusSensorEntity,
    RemainSensorEntity,
    SolarPowerSensorEntity,
    WattsSensorEntity,
)
from custom_components.ecoflow_cloud.switch import EnabledEntity

_LOGGER = logging.getLogger(__name__)

# How often to ask the gateway for a full snapshot (seconds).
QUOTA_REFRESH_SEC = 30

# 254/21 field numbers (see module docstring for how each was verified).
F_GRID_PWR = 515
F_CIRCUITS_PWR = 516
F_SOLAR_PWR = 517
F_BATT_NET_PWR = 518
F_HOME_PWR = 1544
F_PANEL_PWR = 1547
F_GRID_L1_VOL, F_GRID_L2_VOL = 956, 957
F_GRID_L1_AMP, F_GRID_L2_AMP = 958, 959
F_GRID_L1_PWR, F_GRID_L2_PWR = 962, 963
F_BACKUP_L1_VOL, F_BACKUP_L2_VOL = 1063, 1064
F_BACKUP_RESERVE = 461
F_OPERATE_MODE = 1245

# ConfigWrite (254/17) field carrying the operating mode, and the gateway's
# DP3 address (the src of its own frames).
CFG_OPERATE_MODE = 544
GATEWAY_ADDR = 11

# Operating mode as a bitmask of the enabled strategy flags (bit n-1 = sub-field
# n of 1245/544). Only the two modes captured from the app are selectable.
OPERATE_MODES: dict[str, int] = {"Self-Powered": 1, "Backup": 0}

# Circuit state, in each circuit's metadata block.
M_LINK = 2  # 240V pairing: {1: 1 linked, 2: partner circuit}; absent if unpaired
M_SOURCE_CFG = 6  # 1 Auto, 2 Grid
M_OFF = 8  # 1 = switched off

# When the gateway reports a different 240V pairing than the entities were built
# for, wait this long for the rest of the circuit blocks, then reload the entry.
TOPOLOGY_SETTLE_SEC = 15
# Pairing each gateway (by serial) was last reloaded for, so a pairing that
# somehow can't be applied doesn't cause a reload loop.
_TOPOLOGY_RELOADED: dict[str, frozenset[int]] = {}

# Backup reserve: ConfigWrite field carrying the percentage, and the range
# offered (the controller only ever uses 10..100).
CFG_BACKUP_RESERVE = 102
BACKUP_RESERVE_MIN, BACKUP_RESERVE_MAX = 5, 70

# Charge Now (per inlet port, see below): the gateway takes up to ~10 s to report it.
CHARGE_NOW_CONFIRM_SEC = 15.0

# Battery charging power limit per DPU, W: command field, state field, range.
CFG_CHARGE_POWER = 542
F_CHARGE_POWER = 1244
CHARGE_POWER_MIN, CHARGE_POWER_MAX, CHARGE_POWER_STEP = 1000, 12000, 100

# Smart Inlet Box ports AC1..AC3 (i = 0..2): ConfigWrite field, the port block
# in 254/21, and the blocks carrying the attached DPU's / EV charger's serial.
INLET_PORTS = 3
CFG_INLET_BASE = 535
F_INLET_BASE = 1224
F_INLET_DPU_BASE = 1009
F_INLET_ACC_BASE = 1255
INLET_ON, INLET_OFF = 1, 2
INLET_TYPE_DPU, INLET_TYPE_EV_CHARGER = 1, 3
INLET_DEVICE_TYPES: dict[int, str] = {INLET_TYPE_DPU: "DPU", INLET_TYPE_EV_CHARGER: "EV Charger"}


def _charge_now_key(port: int) -> str:
    """State key of port ACn's Charge Now. AC2/AC3 keep the keys from when it
    was exposed per DPU slot ("DPU 1/2 Charge Now"), so their unique IDs hold."""
    return f"gw_dpu_{port - 1}_charge_now" if port >= 2 else f"inlet_ac{port}_charge_now"


# Circuit settings: ConfigWrite field CFG_CIRCUIT_BASE + n for circuit n.
CFG_CIRCUIT_BASE = 386
CIRCUIT_ON, CIRCUIT_OFF = 1, 2
CIRCUIT_SOURCES: dict[str, int] = {"Auto": 1, "Grid": 2}

# How long a control waits for the gateway to report its new state before
# sending the command once more (the gateway normally reports within ~1 s).
CONFIRM_TIMEOUT_SEC = 5.0
CONFIRM_POLL_SEC = 0.25

# Float fields, written as-is (rounded) when present. Grid power and the grid
# per-leg fields are the same fields as on the SHP3, so they keep the SHP3 keys
# (and therefore the same entity unique IDs).
_FLOAT_FIELDS: tuple[tuple[str, int], ...] = (
    ("shp_grid_pwr", F_GRID_PWR),
    ("gw_circuits_pwr", F_CIRCUITS_PWR),
    ("gw_solar_pwr", F_SOLAR_PWR),
    ("gw_batt_pwr", F_BATT_NET_PWR),
    ("shp_l1_vol", F_GRID_L1_VOL),
    ("shp_l2_vol", F_GRID_L2_VOL),
    ("shp_grid_l1_amp", F_GRID_L1_AMP),
    ("shp_grid_l2_amp", F_GRID_L2_AMP),
    ("shp_grid_l1_pwr", F_GRID_L1_PWR),
    ("shp_grid_l2_pwr", F_GRID_L2_PWR),
    ("gw_backup_l1_vol", F_BACKUP_L1_VOL),
    ("gw_backup_l2_vol", F_BACKUP_L2_VOL),
)

# Integer (varint) fields; power values are signed int64 on the wire.
_INT_FIELDS: tuple[tuple[str, int], ...] = (
    ("gw_home_pwr", F_HOME_PWR),
    ("gw_panel_pwr", F_PANEL_PWR),
    ("gw_backup_reserve", F_BACKUP_RESERVE),
    ("gw_chg_pwr_limit", F_CHARGE_POWER),
)


def _signed(v: int) -> int:
    """Reinterpret a protobuf varint as a signed 64-bit integer."""
    return v - (1 << 64) if v >= (1 << 63) else v


class _GetAllRequest(PrivateAPIMessageProtocol):
    """The EcoFlow app's "send everything" request: a single empty DP3 header
    carrying only src/dest/seq/from (the same request the DELTA 3 profile uses)."""

    def __init__(self) -> None:
        self._packet = dp3.DP3SendHeaderMsg()
        header = self._packet.msg.add()
        header.src = 32
        header.dest = 32
        header.seq = Message.gen_seq()
        setattr(header, "from", "HomeAssistant")  # "from" is a Python keyword

    @override
    def to_mqtt_payload(self) -> bytes:
        return self._packet.SerializeToString()

    @override
    def to_dict(self) -> dict:
        return {"DP3SendHeaderMsg": MessageToDict(self._packet, preserving_proto_field_name=True)}


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _len_field(no: int, data: bytes) -> bytes:
    """A length-delimited protobuf field."""
    return _varint(no << 3 | WIRE_LEN) + _varint(len(data)) + data


class _ConfigWrite(PrivateAPIMessageProtocol):
    """A ConfigWrite (254/17) to the gateway, framed as the EcoFlow app frames it.

    Built by hand because the app's header also repeats the serial in fields 26
    and 27, which the DP3 proto doesn't declare.
    """

    def __init__(self, device_sn: str, pdata: bytes) -> None:
        header = dp3.DP3Header()
        header.pdata = pdata
        header.src = 32
        header.dest = GATEWAY_ADDR
        header.d_src = 1
        header.d_dest = 1
        header.check_type = 3
        header.cmd_func = 254
        header.cmd_id = 17
        header.data_len = len(pdata)
        header.need_ack = 1
        header.seq = Message.gen_seq()
        header.version = 4
        header.payload_ver = 1
        setattr(header, "from", "HomeAssistant")  # "from" is a Python keyword
        header.device_sn = device_sn
        sn = device_sn.encode()
        body = header.SerializeToString() + _len_field(26, sn) + _len_field(27, sn)
        self._payload = _len_field(1, body)  # DP3SendHeaderMsg.msg
        self._seq = header.seq
        self._pdata = pdata

    @override
    def to_mqtt_payload(self) -> bytes:
        return self._payload

    @override
    def to_dict(self) -> dict:
        return {"ConfigWrite": {"cmdFunc": 254, "cmdId": 17, "seq": self._seq, "pdata": self._pdata.hex()}}


def _operate_mode_command(device_sn: str, mode: int) -> _ConfigWrite:
    """Field 544 = the strategy flags set in `mode` (empty for Backup)."""
    flags = b""
    for bit in range(mode.bit_length()):
        if mode >> bit & 1:
            flags += _varint((bit + 1) << 3 | WIRE_VARINT) + b"\x01"  # sub-field bit+1 = true
    return _ConfigWrite(device_sn, _len_field(CFG_OPERATE_MODE, flags))


def _backup_reserve_command(device_sn: str, percent: int) -> _ConfigWrite:
    """Field 102 = backup reserve, % (varint)."""
    return _ConfigWrite(device_sn, _varint(CFG_BACKUP_RESERVE << 3 | WIRE_VARINT) + _varint(percent))


def _charge_power_command(device_sn: str, watts: int) -> _ConfigWrite:
    """Field 542 = battery charging power limit per DPU, W (varint)."""
    return _ConfigWrite(device_sn, _varint(CFG_CHARGE_POWER << 3 | WIRE_VARINT) + _varint(watts))


def _charge_now_command(device_sn: str, port: int, on: bool) -> _ConfigWrite:
    """Field 535 + i for port ACn (i = n - 1) = {1: 1, 2: 1} to start Charge Now
    (which also switches the port on), {2: 2} to stop it."""
    body = (_varint(1 << 3 | WIRE_VARINT) + b"\x01" if on else b"") + _varint(2 << 3 | WIRE_VARINT) + (
        b"\x01" if on else b"\x02"
    )
    return _ConfigWrite(device_sn, _len_field(CFG_INLET_BASE + port - 1, body))


class _ConfirmedCommand:
    """Mixin for gateway controls: send a command once more if the gateway
    hasn't reported the new state within CONFIRM_TIMEOUT_SEC.

    The gateway acts on (and reports) a write within about a second, but it has
    been seen to ignore one: the first circuit command after a restart had no
    effect, while the identical command 95 s later worked. Every command sets an
    absolute state, so repeating it is harmless. A newer command for the same
    entity cancels the pending check.
    """

    _cmd_gen = 0
    _confirm_timeout = CONFIRM_TIMEOUT_SEC

    def _confirm_later(self, value: Any) -> None:
        self._cmd_gen += 1
        self.hass.async_create_task(self._async_confirm(value, self._cmd_gen))  # type: ignore[attr-defined]

    async def _async_confirm(self, value: Any, gen: int) -> None:
        for attempt in (1, 2):
            for _ in range(int(self._confirm_timeout / CONFIRM_POLL_SEC)):
                await asyncio.sleep(CONFIRM_POLL_SEC)
                if gen != self._cmd_gen:
                    return  # superseded by a newer command
                if self._device.reported(self.mqtt_key) == value:  # type: ignore[attr-defined]
                    return
            if attempt == 1:
                _LOGGER.warning(
                    "%s: gateway didn't confirm %s within %ss, sending again",
                    self.entity_id,  # type: ignore[attr-defined]
                    value,
                    self._confirm_timeout,
                )
                await self.hass.async_add_executor_job(self._resend, value)  # type: ignore[attr-defined]
        _LOGGER.warning("%s: gateway didn't confirm %s after a second attempt", self.entity_id, value)  # type: ignore[attr-defined]

    def _resend(self, value: Any) -> None:
        self.send_set_message(value, self.command_dict(value))  # type: ignore[attr-defined]


class _ConfirmedSelect(_ConfirmedCommand):
    async def async_select_option(self, option: str) -> None:
        await self.hass.async_add_executor_job(self.select_option, option)  # type: ignore[attr-defined]
        self._confirm_later(self._options_dict[option])  # type: ignore[attr-defined]


class OperatingModeSelectEntity(_ConfirmedSelect, DictSelectEntity):
    """Gateway operating mode. A mode it can't set (TOU etc.) shows as unknown."""

    _attr_entity_category = None  # a primary control, not a config setting
    _attr_icon = "mdi:home-switch"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._current_option: str | None = None

    @override
    def _update_value(self, val: Any) -> bool:
        if super()._update_value(val):
            return True
        if self._current_option is None:
            return False
        self._current_option = None
        return True

    @property
    @override
    def current_option(self) -> str | None:
        return self._current_option


class BackupReserveNumberEntity(_ConfirmedCommand, ValueUpdateEntity):
    """Gateway backup reserve. In Backup mode, setting it above the batteries'
    charge grid-charges the DPUs up to it."""

    _attr_entity_category = None  # a primary control, not a config setting
    _attr_icon = "mdi:battery-lock"
    _attr_native_unit_of_measurement = PERCENTAGE

    async def async_set_native_value(self, value: float) -> None:
        percent = int(value)
        await self.hass.async_add_executor_job(self.send_set_message, percent, self.command_dict(percent))
        self._confirm_later(percent)


class ChargePowerNumberEntity(_ConfirmedCommand, ValueUpdateEntity):
    """Grid-charging power limit, applied to each paired DPU."""

    _attr_entity_category = None  # a primary control, not a config setting
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_step = CHARGE_POWER_STEP
    _attr_mode = NumberMode.BOX

    async def async_set_native_value(self, value: float) -> None:
        watts = int(round(value / CHARGE_POWER_STEP) * CHARGE_POWER_STEP)
        await self.hass.async_add_executor_job(self.send_set_message, watts, self.command_dict(watts))
        self._confirm_later(watts)


def _charge_now_switch_command(device_sn: str, port: int):
    return lambda value: _charge_now_command(device_sn, port, bool(value))


def _inlet_command(device_sn: str, port: int, on: bool) -> _ConfigWrite:
    """Field 535 + i for port ACn (i = n - 1): {1: 1} to switch it on, {1: 2, 2: 2}
    to switch it off (as the app does: off also clears the port's Charge Now)."""
    body = _varint(1 << 3 | WIRE_VARINT) + _varint(INLET_ON if on else INLET_OFF)
    if not on:
        body += _varint(2 << 3 | WIRE_VARINT) + _varint(2)
    return _ConfigWrite(device_sn, _len_field(CFG_INLET_BASE + port - 1, body))


def _inlet_switch_command(device_sn: str, port: int):
    return lambda value: _inlet_command(device_sn, port, bool(value))


class _InletPortControl(_ConfirmedCommand):
    """A Smart Inlet Box port control, named after the attached device:
    "S.I.B. ACn (device)" plus SUFFIX (S.I.B. = Smart Inlet Box; kept short so
    the names fit on the device page), so a port's controls sort together. The
    entity ID comes from "Inlet ACn" plus ID_SUFFIX instead (e.g.
    switch.smart_gateway_inlet_ac1_charge_now, or with the device's area
    prefixed), so it doesn't depend on the device. Unavailable while nothing
    is attached to the port.
    """

    SUFFIX = ""
    ID_SUFFIX = ""
    _ICONS: ClassVar[dict[int, str]] = {}
    _DEFAULT_ICON = "mdi:power-plug"

    def for_port(self, n: int) -> Self:
        self._port = n
        return self

    @property
    def name(self) -> str:
        label = self._device.inlet_device_label(self._port, getattr(self, "hass", None))  # type: ignore[attr-defined]
        name = f"S.I.B. AC{self._port}" + (f" ({label})" if label else "")
        return f"{name} {self.SUFFIX}" if self.SUFFIX else name

    def title(self) -> str:
        return self.name

    @property
    def icon(self) -> str:
        return self._ICONS.get(self._device.inlet_device_type(self._port), self._DEFAULT_ICON)  # type: ignore[attr-defined]

    @property
    def suggested_object_id(self) -> str:
        return f"Inlet AC{self._port} {self.ID_SUFFIX}".strip()

    @property
    def available(self) -> bool:
        return bool(super().available) and self._device.inlet_device_type(self._port) is not None  # type: ignore[misc,attr-defined]

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(partial(self.turn_on, **kwargs))  # type: ignore[attr-defined]
        self._confirm_later(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(partial(self.turn_off, **kwargs))  # type: ignore[attr-defined]
        self._confirm_later(0)


class InletPortSwitchEntity(_InletPortControl, EnabledEntity):
    """A Smart Inlet Box port (AC1..AC3): whether the gateway can draw AC from
    the device attached to it (a DPU stays on and keeps charging from solar)."""

    _ICONS: ClassVar[dict[int, str]] = {INLET_TYPE_DPU: "mdi:home-battery", INLET_TYPE_EV_CHARGER: "mdi:ev-station"}


class ChargeNowSwitchEntity(_InletPortControl, EnabledEntity):
    """A port's Charge Now: grid-charges the DPU attached to it up to its own
    max-charge setting (for an EV charger, the app's Charge now for the car)."""

    SUFFIX = "Chg Now"
    ID_SUFFIX = "Charge Now"
    _ICONS: ClassVar[dict[int, str]] = {INLET_TYPE_EV_CHARGER: "mdi:car-electric"}
    _DEFAULT_ICON = "mdi:battery-charging-high"
    _confirm_timeout = CHARGE_NOW_CONFIRM_SEC


def _circuit_command(
    device_sn: str, params: dict[str, Any], n: int, *, on: bool | None = None, source: int | None = None
) -> _ConfigWrite:
    """Settings write for circuit n (both legs of a 240V circuit).

    The gateway takes on/off and source together, so whichever one isn't being
    changed is sent as currently reported. Refuses rather than guess it (a wrong
    guess would e.g. move a Grid circuit to Auto while switching it off).
    """
    if on is None:
        if f"ch_{n}_on" not in params:
            raise HomeAssistantError(f"Circuit {n}: on/off state not reported by the gateway yet")
        on = bool(params[f"ch_{n}_on"])
    if source is None:
        source = params.get(f"ch_{n}_src_cfg")
        if source not in CIRCUIT_SOURCES.values():
            raise HomeAssistantError(f"Circuit {n}: Auto/Grid setting not reported by the gateway yet")
    settings = _varint(1 << 3 | WIRE_VARINT) + _varint(CIRCUIT_ON if on else CIRCUIT_OFF)
    settings += _varint(2 << 3 | WIRE_VARINT) + _varint(source)
    legs = sorted({n, params.get(f"ch_{n}_partner") or n})
    return _ConfigWrite(device_sn, b"".join(_len_field(CFG_CIRCUIT_BASE + leg, settings) for leg in legs))


def _app_origin(label: str) -> str:
    """Where an app command came from, for the logbook (header "from" label)."""
    platform = {"ios": "iOS", "android": "Android"}.get(label.lower())
    if platform:
        return f"in the EcoFlow app ({platform})"
    return f"by another EcoFlow client ({label})" if label else "by another EcoFlow client"


# Command factories (the entities pass (value, params) to a 2-argument command).
def _circuit_switch_command(device_sn: str, n: int):
    return lambda value, params: _circuit_command(device_sn, params, n, on=bool(value))


def _circuit_source_command(device_sn: str, n: int):
    return lambda value, params: _circuit_command(device_sn, params, n, source=int(value))


class _ControlCircuitNamed(_CircuitNamed):
    """Names a circuit control "Smart Circuit N (label)", plus a suffix; a 240V
    circuit is "Smart Circuit N/M (label, 240V)".

    HA lists a device's controls alphabetically (numbers in numeric order), so
    these follow "Operating Mode" in circuit order, each switch directly above
    its source select. The entity ID comes from "Circuit N <ID_SUFFIX>" instead,
    so it doesn't depend on the label (switch.smart_gateway_circuit_1_switch).
    """

    ID_SUFFIX = ""

    def _circuit_name(self) -> str:
        data = getattr(self._device, "data", None)
        params = data.params if data is not None else {}
        n = self._circuit_no
        partner = params.get(f"ch_{n}_partner")
        detail = ", ".join(x for x in (params.get(f"ch_{n}_name"), "240V" if partner else None) if x)
        name = f"Smart Circuit {n}/{partner}" if partner else f"Smart Circuit {n}"
        if detail:
            name += f" ({detail})"
        return f"{name} {self._circuit_suffix}" if self._circuit_suffix else name

    @property
    def suggested_object_id(self) -> str:
        return f"Circuit {self._circuit_no} {self.ID_SUFFIX}"


class CircuitSwitchEntity(_ConfirmedCommand, _ControlCircuitNamed, EnabledEntity):
    """A smart circuit's on/off switch (a 240V circuit switches both legs)."""

    _attr_icon = "mdi:power-socket-us"
    ID_SUFFIX = "Switch"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(partial(self.turn_on, **kwargs))
        self._confirm_later(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(partial(self.turn_off, **kwargs))
        self._confirm_later(0)


class CircuitSourceSelectEntity(_ConfirmedSelect, _ControlCircuitNamed, DictSelectEntity):
    """A smart circuit's source setting: Auto (follows the operating mode) or Grid."""

    ID_SUFFIX = "Source"
    _attr_entity_category = None  # a primary control, not a config setting
    _attr_icon = "mdi:transmission-tower"


class SmartGateway(SmartHomePanel3):
    """EcoFlow Smart Gateway (HR65…, private / app API)."""

    # 6 smart circuits; the metadata block for them is 794..799 and the power
    # array 1015..1020 (two further empty slots, 7-8, are not exposed).
    CIRCUITS = 6
    NAME_FIELDS: ClassVar[list[int]] = list(range(794, 800))
    # Keep the Controls section to the operating mode and the circuits.
    RECONNECT_ENTITY_CATEGORY = EntityCategory.CONFIG

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Control states as last reported by the gateway itself. (params also
        # holds the value a control optimistically writes when it sends.)
        self._reported: dict[str, Any] = {}
        # Second legs of 240V pairs when the circuit entities were built.
        self._built_secondary: frozenset[int] | None = None
        self._topology_check_pending = False
        # What's attached to each Smart Inlet Box port, by port number:
        # {"type": INLET_TYPE_*, "dpu_sn": ..., "acc_sn": ...}; persisted, so
        # the port switches are named correctly right after a restart.
        self._inlet: dict[int, dict[str, Any]] = {}

    def reported(self, key: str) -> Any:
        return self._reported.get(key)

    @override
    def configure(self, hass: HomeAssistant) -> None:
        super().configure(hass)
        self._inlet_store: Store[dict] = Store(hass, 1, f"ecoflow_cloud.{self.device_info.sn}.inlet_ports")

    @override
    async def async_restore_state(self) -> None:
        """Restore labels/pairing (parent), then drop entities of second legs;
        and restore what's attached to each inlet port."""
        await super().async_restore_state()
        self._remove_secondary_leg_entities()
        store = getattr(self, "_inlet_store", None)
        data = await store.async_load() if store is not None else None
        if isinstance(data, dict):
            for n, port in data.items():
                if str(n).isdigit() and isinstance(port, dict):
                    self._inlet[int(n)] = dict(port)

    def _save_inlet(self) -> None:
        """Persist the inlet ports (called from decoding, on the MQTT thread)."""
        store, hass = getattr(self, "_inlet_store", None), getattr(self, "_hass", None)
        if store is None or hass is None:
            return
        snapshot = {str(n): dict(p) for n, p in self._inlet.items()}
        asyncio.run_coroutine_threadsafe(store.async_save(snapshot), hass.loop)

    def inlet_device_type(self, n: int) -> int | None:
        return self._inlet.get(n, {}).get("type")

    def inlet_device_label(self, n: int, hass: HomeAssistant | None) -> str | None:
        """What's attached to port ACn: the name of the EcoFlow device in HA with
        its serial, else its type ("DPU", "EV Charger"); None while nothing is
        attached (the serial blocks can outlive the device) or it's unknown."""
        port = self._inlet.get(n, {})
        if port.get("type") is None:
            return None
        sn = port.get("dpu_sn") or port.get("acc_sn")
        if sn and hass is not None:
            try:
                name = self._registry_device_name(hass, sn)
            except Exception as e:  # naming is cosmetic; never break the entity over it
                _LOGGER.debug("Smart Gateway: device lookup for %s failed: %s", sn, e)
                name = None
            if name:
                return name
        return INLET_DEVICE_TYPES.get(port.get("type"))  # type: ignore[arg-type]

    @staticmethod
    def _registry_device_name(hass: HomeAssistant, sn: str) -> str | None:
        """Name of this integration's device with serial `sn` (developer-API or
        app entry), from any config entry. async_get_devices where available:
        async_get_device is deprecated (HA 2027.8), and the replacement
        async_get_device_by_identifier needs the device's config entry."""
        from custom_components.ecoflow_cloud import ECOFLOW_DOMAIN

        registry = dr.async_get(hass)
        get_devices = getattr(registry, "async_get_devices", None)
        for identifier in (f"api-{sn}", sn):
            ids = {(ECOFLOW_DOMAIN, identifier)}
            if get_devices is not None:
                devices = list(get_devices(identifiers=ids))
            else:
                devices = [d for d in (registry.async_get_device(identifiers=ids),) if d is not None]
            if devices:
                return devices[0].name_by_user or devices[0].name
        return None

    def _secondary_legs(self) -> frozenset[int]:
        """Circuits that are the second (higher-numbered) leg of a 240V pair.

        From the pairing record (restored at startup, updated as frames are
        decoded, persisted), which is current even mid-decode, unlike params.
        """
        meta = getattr(self, "_meta", {})
        return frozenset(n for n in range(1, self.CIRCUITS + 1) if (p := meta.get(n, {}).get("partner")) and p < n)

    def _remove_secondary_leg_entities(self) -> None:
        """Remove registry entries left from circuits that are now a pair's second leg.

        A pair's entities live on its first leg, so anything registered for the
        second (e.g. created on first setup, before the pairing was reported, or
        before the circuits were paired in the app) would otherwise stay behind
        as unavailable. Matches only this gateway's per-circuit unique IDs
        ("ecoflow-<sn>-ch-<n>-...", energy totals included).
        """
        secondary = self._secondary_legs()
        hass = getattr(self, "_hass", None)
        if not secondary or hass is None:
            return
        from custom_components.ecoflow_cloud import ECOFLOW_DOMAIN

        registry = er.async_get(hass)
        base = f"ecoflow-{'api-' if self.device_info.public_api else ''}{self.device_data.sn}-ch-"
        prefixes = tuple(f"{base}{n}-" for n in sorted(secondary))
        stale = [
            e.entity_id
            for e in list(registry.entities.values())
            if e.platform == ECOFLOW_DOMAIN and e.unique_id.startswith(prefixes)
        ]
        for entity_id in stale:
            _LOGGER.info("Smart Gateway: removing %s, its circuit is now the second leg of a 240V pair", entity_id)
            registry.async_remove(entity_id)

    def _check_topology(self) -> None:
        """Called from decoding (MQTT thread): schedule a reload if the pairing
        no longer matches the entities that were built."""
        hass = getattr(self, "_hass", None)
        if hass is None or self._built_secondary is None or self._topology_check_pending:
            return
        if self._secondary_legs() == self._built_secondary:
            return
        self._topology_check_pending = True
        hass.loop.call_soon_threadsafe(lambda: hass.async_create_task(self._async_apply_topology()))

    async def _async_apply_topology(self) -> None:
        await asyncio.sleep(TOPOLOGY_SETTLE_SEC)
        self._topology_check_pending = False
        current = self._secondary_legs()
        if current == self._built_secondary:
            return
        sn = self.device_data.sn
        if _TOPOLOGY_RELOADED.get(sn) == current:
            _LOGGER.warning("Smart Gateway %s: 240V pairing %s still not applied after a reload", sn, sorted(current))
            return
        from custom_components.ecoflow_cloud import ECOFLOW_DOMAIN

        hass = self._hass
        for entry in hass.config_entries.async_entries(ECOFLOW_DOMAIN):
            client = hass.data.get(ECOFLOW_DOMAIN, {}).get(entry.entry_id)
            if client is not None and self in getattr(client, "devices", {}).values():
                _TOPOLOGY_RELOADED[sn] = current
                _LOGGER.info(
                    "Smart Gateway %s: 240V pairing changed (second legs %s, were %s); reloading to update circuits",
                    sn,
                    sorted(current),
                    sorted(self._built_secondary or ()),
                )
                hass.config_entries.async_schedule_reload(entry.entry_id)
                return

    @override
    def sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        out: list[SensorEntity] = [
            LevelSensorEntity(client, self, "cms_batt_soc", const.COMBINED_BATTERY_LEVEL),
            WattsSensorEntity(client, self, "gw_home_pwr", "Home Load Power")
            .with_icon("mdi:home-lightning-bolt")
            .with_energy(),
            InWattsSensorEntity(client, self, "shp_grid_pwr", "Grid Power").with_energy(),
            SolarPowerSensorEntity(client, self, "gw_solar_pwr", "Solar Power").with_energy(),
            # Everything the paired DPUs deliver into the gateway (home - grid):
            # solar and battery combined, since the panels feed the DPUs. Key kept
            # from when this was labelled "Storage Output", so the unique ID holds.
            WattsSensorEntity(client, self, "gw_storage_pwr", "DPU Output Power")
            .with_icon("mdi:home-battery")
            .with_energy(),
            # Signed: positive = batteries charging, negative = discharging.
            WattsSensorEntity(client, self, "gw_batt_pwr", "Battery Power").with_icon("mdi:battery-charging"),
            # The same split into two non-negative flows, each with an energy
            # total — the pair the Energy dashboard's battery entry expects.
            WattsSensorEntity(client, self, "gw_batt_chg_pwr", "Battery Charging Power")
            .with_icon("mdi:battery-arrow-up")
            .with_energy(),
            WattsSensorEntity(client, self, "gw_batt_dsg_pwr", "Battery Discharging Power")
            .with_icon("mdi:battery-arrow-down")
            .with_energy(),
            WattsSensorEntity(client, self, "gw_circuits_pwr", "Smart Circuits Power").with_icon("mdi:electric-switch"),
            WattsSensorEntity(client, self, "gw_panel_pwr", "Home Load excl. Smart Circuits Power", False),
            RemainSensorEntity(client, self, "cms_chg_rem_time", const.CHARGE_REMAINING_TIME),
            RemainSensorEntity(client, self, "cms_dsg_rem_time", const.DISCHARGE_REMAINING_TIME),
            BatteryLimitSensorEntity(client, self, "gw_backup_reserve", const.BACKUP_RESERVE_LEVEL),
            # Same entity (key "status") as before; additionally asks the gateway
            # for a full snapshot every QUOTA_REFRESH_SEC.
            QuotaScheduledStatusSensorEntity(client, self, QUOTA_REFRESH_SEC, "Status", "status"),
            # Grid-side per-leg detail — enabled, regular sensors.
            InWattsSensorEntity(client, self, "shp_grid_l1_pwr", "Grid L1 Power"),
            InWattsSensorEntity(client, self, "shp_grid_l2_pwr", "Grid L2 Power"),
            InVoltSensorEntity(client, self, "shp_l1_vol", "Grid L1 Voltage"),
            InVoltSensorEntity(client, self, "shp_l2_vol", "Grid L2 Voltage"),
            InAmpSensorEntity(client, self, "shp_grid_l1_amp", "Grid L1 Current"),
            InAmpSensorEntity(client, self, "shp_grid_l2_amp", "Grid L2 Current"),
            # Backup-side voltage — disabled by default, diagnostic.
            OutVoltSensorEntity(client, self, "gw_backup_l1_vol", "Backup L1 Voltage", False, diagnostic=True),
            OutVoltSensorEntity(client, self, "gw_backup_l2_vol", "Backup L2 Voltage", False, diagnostic=True),
        ]
        return out + self.circuit_sensors(client)

    @override
    def numbers(self, client: EcoflowApiClient) -> list[NumberEntity]:
        sn = self.device_info.sn
        return [
            BackupReserveNumberEntity(
                client,
                self,
                "gw_backup_reserve",
                "Backup Reserve",
                BACKUP_RESERVE_MIN,
                BACKUP_RESERVE_MAX,
                lambda value: _backup_reserve_command(sn, int(value)),
            ),
            ChargePowerNumberEntity(
                client,
                self,
                "gw_chg_pwr_limit",
                "Battery Charging Power per DPU",
                CHARGE_POWER_MIN,
                CHARGE_POWER_MAX,
                lambda value: _charge_power_command(sn, int(value)),
            ),
        ]

    @override
    def selects(self, client: EcoflowApiClient) -> list[SelectEntity]:
        sn = self.device_info.sn
        return [
            OperatingModeSelectEntity(
                client,
                self,
                "gw_operate_mode",
                "Operating Mode",
                OPERATE_MODES,
                lambda value: _operate_mode_command(sn, int(value)),
            ),
        ] + [
            CircuitSourceSelectEntity(
                client,
                self,
                f"ch_{n}_src_cfg",
                f"Smart Circuit {n} Source",
                CIRCUIT_SOURCES,
                _circuit_source_command(sn, n),
            ).for_circuit(n, "Source")
            for n in self._primary_circuits()
        ]

    @override
    def switches(self, client: EcoflowApiClient) -> list[SwitchEntity]:
        sn = self.device_info.sn
        return (
            [
                CircuitSwitchEntity(
                    client, self, f"ch_{n}_on", f"Smart Circuit {n}", _circuit_switch_command(sn, n)
                ).for_circuit(n, "")
                for n in self._primary_circuits()
            ]
            + [
                InletPortSwitchEntity(
                    client, self, f"inlet_ac{n}_on", f"S.I.B. AC{n}", _inlet_switch_command(sn, n)
                ).for_port(n)
                for n in range(1, INLET_PORTS + 1)
            ]
            + [
                ChargeNowSwitchEntity(
                    client,
                    self,
                    _charge_now_key(n),
                    f"S.I.B. AC{n} Chg Now",
                    _charge_now_switch_command(sn, n),
                ).for_port(n)
                for n in range(1, INLET_PORTS + 1)
            ]
        )

    def _primary_circuits(self) -> list[int]:
        """Circuits that get entities: all but the second leg of each 240V pair."""
        secondary = self._secondary_legs()
        return [n for n in range(1, self.CIRCUITS + 1) if n not in secondary]

    @override
    def circuit_sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        """Circuit sensors, with each 240V pair exposed once (on its primary leg)."""
        self._built_secondary = self._secondary_legs()
        primary = set(self._primary_circuits())
        return [e for e in super().circuit_sensors(client) if getattr(e, "_circuit_no", None) in primary]

    @override
    def _combine_split_phase(self, result: dict[str, Any]) -> None:
        """Fold each 240V pair onto its primary leg: power summed (parent), plus
        line-to-line voltage (L1 + L2) and the higher of the two leg currents."""
        super()._combine_split_phase(result)
        for n, m in self._meta.items():
            p = m.get("partner")
            if not p or n > p:
                continue
            if f"ch_{n}_vol" in result and f"ch_{p}_vol" in result:
                result[f"ch_{n}_vol"] = round(result[f"ch_{n}_vol"] + result[f"ch_{p}_vol"], 2)
            if f"ch_{n}_amp" in result and f"ch_{p}_amp" in result:
                result[f"ch_{n}_amp"] = max(result[f"ch_{n}_amp"], result[f"ch_{p}_amp"])

    @override
    def _decode_metadata(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Labels (parent), plus each circuit's 240V pairing, on/off and source setting.

        A metadata block always arrives whole, so within one an absent off flag
        means on, and an absent link means the circuit is not (or no longer)
        paired. The parent only ever adds a pairing; here it is also cleared,
        so splitting a 240V circuit in the app takes effect.
        """
        super()._decode_metadata(fields, result)
        after_parent = {n: dict(m) for n, m in self._meta.items()}
        for n, field_no in enumerate(self.NAME_FIELDS, 1):
            entry = _first(fields, field_no, WIRE_LEN)
            if entry is None:
                continue
            meta = _parse_fields(entry)
            result[f"ch_{n}_on"] = self._reported[f"ch_{n}_on"] = 0 if _first(meta, M_OFF, WIRE_VARINT) else 1
            source = _first(meta, M_SOURCE_CFG, WIRE_VARINT)
            if source:
                result[f"ch_{n}_src_cfg"] = self._reported[f"ch_{n}_src_cfg"] = source
            partner = self._linked_partner(n, meta)
            circuit = self._meta.setdefault(n, {})
            if partner:
                circuit["partner"] = partner
            else:
                circuit.pop("partner", None)
            result[f"ch_{n}_partner"] = partner or 0
        if self._meta != after_parent:
            self._save_meta()
        self._check_topology()

    def _linked_partner(self, n: int, meta: FieldMap) -> int | None:
        """The circuit n is paired with for 240V, from its metadata block."""
        link = _first(meta, M_LINK, WIRE_LEN)
        if link is None:
            return None
        fields = _parse_fields(link)
        partner = _first(fields, 2, WIRE_VARINT)
        if not _first(fields, 1, WIRE_VARINT) or not partner or partner == n or partner > self.CIRCUITS:
            return None
        return partner

    @override
    def _decode_flows(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Gateway aggregate flows; absent = unchanged, so write only what's present."""
        for key, no in _FLOAT_FIELDS:
            v = _first(fields, no, WIRE_F32)
            if v is not None:
                result[key] = round(v, 2)
        for key, no in _INT_FIELDS:
            v = _first(fields, no, WIRE_VARINT)
            if v is not None:
                result[key] = _signed(v)
        for key in ("gw_backup_reserve", "gw_chg_pwr_limit"):
            if key in result:
                self._reported[key] = result[key]

        self._decode_inlet(fields, result)

        # Operating mode: bitmask of the strategy flags set (0 = Backup).
        strategy = _first(fields, F_OPERATE_MODE, WIRE_LEN)
        if strategy is not None:
            mode = 0
            for no, vals in _parse_fields(strategy).items():
                if any(wt == WIRE_VARINT and v for wt, v in vals):
                    mode |= 1 << (no - 1)
            result["gw_operate_mode"] = self._reported["gw_operate_mode"] = mode

        # Battery net charge split into charging / discharging (both >= 0), so
        # their integrated energy only ever counts up.
        if "gw_batt_pwr" in result:
            net = result["gw_batt_pwr"]
            result["gw_batt_chg_pwr"] = round(max(net, 0.0), 2)
            result["gw_batt_dsg_pwr"] = round(max(-net, 0.0), 2)

        # DPU output = home load - grid. The two can arrive in different
        # frames, so combine the last-seen pair. Grid counts as 0 until first
        # reported: while the house runs off the DPUs the gateway can go a long
        # time without resending an unchanged 0, and on grid the value changes
        # (and so is sent) every frame. Clamped at 0: a negative value would be
        # the DPUs taking AC in (grid charging), which the battery charging
        # sensor already covers, and must not run the energy total down.
        for k in ("gw_home_pwr", "shp_grid_pwr"):
            if k in result:
                self._flows[k] = result[k]
        if "gw_home_pwr" in self._flows and ("gw_home_pwr" in result or "shp_grid_pwr" in result):
            grid = self._flows.get("shp_grid_pwr", 0.0)
            result["gw_storage_pwr"] = round(max(self._flows["gw_home_pwr"] - grid, 0.0), 2)

    def _decode_inlet(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Smart Inlet Box ports: on/off, Charge Now, and what's attached (type,
        serial).

        Each block arrives whole when present, so a sub-field missing from a
        present block means there is none: no device type = nothing attached,
        no serial = none (e.g. 1009 while AC1 has an EV charger).
        """
        before = {n: dict(p) for n, p in self._inlet.items()}
        for i in range(INLET_PORTS):
            n = i + 1
            block = _first(fields, F_INLET_BASE + i, WIRE_LEN)
            if block is not None:
                values = _parse_fields(block)
                power = _first(values, 1, WIRE_VARINT)
                if power in (INLET_ON, INLET_OFF):
                    key = f"inlet_ac{n}_on"
                    result[key] = self._reported[key] = 1 if power == INLET_ON else 0
                charge = _first(values, 4, WIRE_VARINT)
                if charge in (1, 2):
                    key = _charge_now_key(n)
                    result[key] = self._reported[key] = 1 if charge == 1 else 0
                kind = _first(values, 2, WIRE_VARINT)
                self._inlet.setdefault(n, {})["type"] = kind
                result[f"inlet_ac{n}_type"] = kind or 0  # 0 = nothing attached
            for slot, base in (("dpu_sn", F_INLET_DPU_BASE), ("acc_sn", F_INLET_ACC_BASE)):
                device = _first(fields, base + i, WIRE_LEN)
                if device is None:
                    continue
                raw = _first(_parse_fields(device), 3, WIRE_LEN)
                sn = raw.decode("utf-8", "ignore").strip("\x00 ") if raw else ""
                self._inlet.setdefault(n, {})[slot] = sn if sn.isprintable() and sn else None
        if self._inlet != before:
            self._save_inlet()

    @override
    def get_quota_message(self) -> Message:
        """Protobuf "get all" request (the JSON latestQuotas default is ignored)."""
        return _GetAllRequest()

    @override
    def _prepare_data_get_reply_topic(self, raw_data: bytes) -> PreparedData:
        """Decode the multi-header snapshot sent back for a "get all" request."""
        if raw_data[:1] == b"{":
            # JSON reply (e.g. "device not online"): keep it for diagnostics only.
            try:
                return PreparedData(None, None, json.loads(raw_data))
            except ValueError:
                return PreparedData(None, None, {"raw": raw_data.hex()})
        try:
            header_msg = dp3.DP3HeaderMessage()
            header_msg.ParseFromString(raw_data)
        except Exception as e:  # not a protobuf snapshot
            _LOGGER.debug("Smart Gateway get_reply parse failed: %s", e)
            return PreparedData(None, None, {"proto": raw_data.hex()})

        merged: dict[str, Any] = {}
        for header in header_msg.header:
            if not header.pdata:
                continue
            info = {
                "src": header.src,
                "encType": header.enc_type,
                "seq": header.seq,
                "cmdFunc": header.cmd_func,
                "cmdId": header.cmd_id,
            }
            try:
                decoded = self._decode_message_by_type(self._perform_xor_decode(header.pdata, info), info)
            except Exception as e:
                _LOGGER.debug("Smart Gateway get_reply header %s/%s skipped: %s", info["cmdFunc"], info["cmdId"], e)
                continue
            if decoded:
                merged.update(decoded)

        if not merged:
            return PreparedData(None, None, {"proto": raw_data.hex()})
        return PreparedData(
            None, {"params": self._flatten_dict(merged), "all_fields": merged}, {"proto": raw_data.hex()}
        )

    @override
    def _prepare_data_get_topic(self, raw_data: bytes) -> PreparedData:
        """Keep "get" messages for diagnostics without decoding them.

        The integration subscribes to the get topic it publishes on, so each
        "get all" request comes straight back here. It is an empty header (no
        payload), and running it through the DP3 decoder only logs two
        "No pdata/payload" warnings every refresh.
        """
        return PreparedData(None, None, {"proto": raw_data.hex()})

    @override
    def _prepare_data_set_topic(self, raw_data: bytes) -> PreparedData:
        """Keep "set" messages (ours and the app's) undecoded, as for "get":
        they are commands to the gateway, and several of the app's carry no
        payload (same two warnings).

        The app's own control commands are read for one thing: a logbook line
        saying the change came from the app, since the state change itself
        carries no origin. Our own commands (from "HomeAssistant") are skipped;
        HA already logs those with the user who made them.
        """
        try:
            changes = self._app_command_changes(raw_data)
        except Exception as e:  # attribution is best-effort; never break message handling
            _LOGGER.debug("Smart Gateway: couldn't read set message for attribution: %s", e)
            changes = []
        hass = getattr(self, "_hass", None)
        if changes and hass is not None:
            hass.loop.call_soon_threadsafe(self._log_app_changes, changes)
        return PreparedData(None, None, {"proto": raw_data.hex()})

    def _app_command_changes(self, raw_data: bytes) -> list[tuple[str, str, str]]:
        """(key, description, origin) for each control change in an app command.

        Runs before the gateway reports the new state, so the reported state is
        still the old one and tells which of a circuit's two values changed.
        """
        header_msg = dp3.DP3HeaderMessage()
        header_msg.ParseFromString(raw_data)
        changes: list[tuple[str, str, str]] = []
        for header in header_msg.header:
            label = getattr(header, "from")  # "from" is a Python keyword
            if (header.cmd_func, header.cmd_id) != (254, 17) or not header.pdata or label == "HomeAssistant":
                continue
            origin = _app_origin(label)
            fields = _parse_fields(header.pdata)
            flags = _first(fields, CFG_OPERATE_MODE, WIRE_LEN)
            if flags is not None:
                mode = 0
                for no, vals in _parse_fields(flags).items():
                    if any(wt == WIRE_VARINT and v for wt, v in vals):
                        mode |= 1 << (no - 1)
                name = next(
                    (k for k, v in OPERATE_MODES.items() if v == mode), f"a mode this integration can't set ({mode})"
                )
                changes.append(("gw_operate_mode", f"set to {name}", origin))
            reserve = _first(fields, CFG_BACKUP_RESERVE, WIRE_VARINT)
            if reserve is not None:
                changes.append(("gw_backup_reserve", f"set to {reserve}%", origin))
            watts = _first(fields, CFG_CHARGE_POWER, WIRE_VARINT)
            if watts is not None:
                changes.append(("gw_chg_pwr_limit", f"set to {watts} W", origin))
            for i in range(INLET_PORTS):
                block = _first(fields, CFG_INLET_BASE + i, WIRE_LEN)
                if block is not None:
                    changes += [(k, d, origin) for k, d in self._inlet_command_changes(i, _parse_fields(block))]
            secondary = self._secondary_legs()
            for n in range(1, self.CIRCUITS + 1):
                setting = _first(fields, CFG_CIRCUIT_BASE + n, WIRE_LEN)
                if setting is None or n in secondary:  # a pair is described once, on its first leg
                    continue
                values = _parse_fields(setting)
                on = _first(values, 1, WIRE_VARINT) == CIRCUIT_ON
                source = _first(values, 2, WIRE_VARINT)
                source_name = next((k for k, v in CIRCUIT_SOURCES.items() if v == source), None)
                was_on, was_source = self.reported(f"ch_{n}_on"), self.reported(f"ch_{n}_src_cfg")
                on_changed = was_on is None or bool(was_on) != on
                source_changed = source_name is not None and source != was_source
                if on_changed:
                    changes.append((f"ch_{n}_on", f"turned {'on' if on else 'off'}", origin))
                if source_changed:
                    changes.append((f"ch_{n}_src_cfg", f"set to {source_name}", origin))
                if not on_changed and not source_changed:
                    # Same as reported (a repeat, or the new state already arrived): say what was set.
                    detail = f"{'on' if on else 'off'}, {source_name}" if source_name else ("on" if on else "off")
                    changes.append((f"ch_{n}_on", f"set to {detail}", origin))
        return changes

    def _inlet_command_changes(self, i: int, values: FieldMap) -> list[tuple[str, str]]:
        """(key, description) for an app command to inlet port i's block.

        The block carries the port's power (sub-field 1) and its Charge Now
        (sub-field 2): {1: 1} / {1: 2, 2: 2} switch the port on / off, {1: 1,
        2: 1} / {2: 2} turn Charge Now on / off. Each control gets a line when
        the command changes it from the reported state (a port switched off
        only mentions Charge Now if it cancels an active one); a command that
        changes nothing (a repeat, or the new state already arrived) gets one
        line on the control it was for.
        """
        power = _first(values, 1, WIRE_VARINT)
        charge = _first(values, 2, WIRE_VARINT)
        charge_command = charge == 1 or power is None
        port_key = f"inlet_ac{i + 1}_on"
        charge_key = _charge_now_key(i + 1)
        lines: list[tuple[str, str]] = []
        for key, value in ((port_key, power), (charge_key, charge)):
            if value not in (1, 2):
                continue
            was = self.reported(key)
            if key == charge_key and not charge_command:
                changed = was == 1 and value == 2
            else:
                changed = was is None or bool(was) != (value == 1)
            if changed:
                lines.append((key, f"turned {'on' if value == 1 else 'off'}"))
        if not lines:
            if charge_command:
                if charge_key is not None and charge in (1, 2):
                    lines.append((charge_key, f"set to {'on' if charge == 1 else 'off'}"))
            elif power in (1, 2):
                lines.append((port_key, f"set to {'on' if power == 1 else 'off'}"))
        return lines

    def _log_app_changes(self, changes: list[tuple[str, str, str]]) -> None:
        """Event loop: one logbook line per change, on the control it concerns."""
        from custom_components.ecoflow_cloud import ECOFLOW_DOMAIN

        hass = self._hass
        registry = er.async_get(hass)
        base = f"ecoflow-{'api-' if self.device_info.public_api else ''}{self.device_data.sn}-"
        for key, description, origin in changes:
            if key in ("gw_backup_reserve", "gw_chg_pwr_limit"):
                domain = "number"
            elif key.endswith(("_on", "_charge_now")):
                domain = "switch"
            else:
                domain = "select"
            entity_id = registry.async_get_entity_id(domain, ECOFLOW_DOMAIN, base + key.replace("_", "-"))
            if entity_id is None:
                continue
            state = hass.states.get(entity_id)
            hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": state.name if state is not None else entity_id,
                    "message": f"{description} {origin}",
                    "domain": ECOFLOW_DOMAIN,
                    "entity_id": entity_id,
                },
            )
