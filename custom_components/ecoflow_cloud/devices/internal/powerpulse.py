"""EcoFlow PowerPulse EV Charger (9.6 kW, US).

State and controls come from the charger's own messages, decoded from
captures (field map and evidence in powerpulse_protocol.py):

- Sensors: charger state, active fault codes, last event, charge request
  (the last Start/Stop sent by the EcoFlow app or Home Assistant), charging power,
  current and voltage, session and lifetime energy, control pilot level and
  duty cycle, the power the system allots the charger, Wi-Fi signal.
- Fault binary sensor.
- Controls, each sending the EcoFlow app's own command byte for byte:
  Charging (start/stop), Charging Mode (Fast Charge / Custom / Solar and
  Battery), Custom Current (6-40 A; like the app, setting it selects Custom).
  The charger acknowledges every command; one that isn't acknowledged within
  ACK_TIMEOUT_SEC is sent once more.
- The charger is asked for a full snapshot every POLL_CHARGING_SEC while a
  session runs and every POLL_IDLE_SEC otherwise (the empty-header request the
  app sends when it opens a device page). A snapshot can be a moment stale, so
  it never overrides a value a live frame reported in the last
  SNAPSHOT_YIELD_SEC.

Diagnostic capture (how the above was worked out; see powerpulse_capture.py):
- Off at every start of Home Assistant; nothing is written until the
  "Diagnostic Capture" switch is turned on. Each turn-on starts a new file in
  <config>/ecoflow_capture/powerpulse_<serial>_<local time>.jsonl with every
  frame the charger sends, every command sent to it (the app's included), and
  the paired Smart Gateway's frames.
- While on, a snapshot is requested every CAPTURE_POLL_SEC.
- Stops by itself after CAPTURE_MAX_MINUTES or CAPTURE_MAX_BYTES.
- "Capture Marker" writes a numbered marker line, to separate attempts.
- The integration's diagnostics download carries a compact timeline of the
  current or last capture, plus the latest frame of each kind the charger sent.

Add it with the app-login (manual) flow: device type POWERPULSE, the charger's
serial (C101…) as the device ID.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, override

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.api.message import Message, PrivateAPIMessageProtocol
from custom_components.ecoflow_cloud.binary_sensor import MiscBinarySensorEntity
from custom_components.ecoflow_cloud.device_data import DeviceData
from custom_components.ecoflow_cloud.devices import BaseInternalDevice, EcoflowDeviceInfo
from custom_components.ecoflow_cloud.devices.data_holder import PreparedData
from custom_components.ecoflow_cloud.devices.internal import powerpulse_protocol as pp
from custom_components.ecoflow_cloud.devices.internal.powerpulse_capture import (
    CaptureSession,
    decode_frame,
    dp3_headers,
    flatten,
    frame_payloads,
)
from custom_components.ecoflow_cloud.devices.internal.proto import ef_dp3_iobroker_pb2 as dp3
from custom_components.ecoflow_cloud.devices.internal.smart_gateway import _GetAllRequest
from custom_components.ecoflow_cloud.entities import BaseSensorEntity, EcoFlowAbstractEntity
from custom_components.ecoflow_cloud.number import ValueUpdateEntity
from custom_components.ecoflow_cloud.select import DictSelectEntity
from custom_components.ecoflow_cloud.sensor import (
    AmpSensorEntity,
    EnergySensorEntity,
    MiscSensorEntity,
    VoltSensorEntity,
    WattsSensorEntity,
)
from custom_components.ecoflow_cloud.switch import EnabledEntity

_LOGGER = logging.getLogger(__name__)

CAPTURE_DIR = "ecoflow_capture"
CAPTURE_POLL_SEC = 10
CAPTURE_MAX_MINUTES = 120
CAPTURE_MAX_BYTES = 200 * 1024 * 1024
# How often the capture sensor's counters refresh while recording.
CAPTURE_NOTIFY_SEC = 5

# Snapshot requests outside a capture: while a session runs / otherwise.
POLL_CHARGING_SEC = 10
POLL_IDLE_SEC = 60
# A snapshot never overrides a value a live frame reported this recently.
SNAPSHOT_YIELD_SEC = 15
# Commands: how long to wait for the charger's acknowledgement before sending
# once more.
ACK_TIMEOUT_SEC = 5.0
ACK_POLL_SEC = 0.25

# Other devices in the same config entry whose frames are recorded alongside.
WATCHED_DEVICE_TYPES = frozenset({"SMART_GATEWAY"})

# Gateway fields left out of the in-memory timeline (not the file): the flows
# and per-leg grid figures that change every frame (see smart_gateway.py).
_GATEWAY_NOISY_FIELDS = frozenset(
    {"515", "516", "517", "518", "956", "957", "958", "959", "962", "963", "1063", "1064", "1544", "1547"}
)


# Header "from" of a command -> who sent it.
_ORIGINS = {"ios": "EcoFlow app", "android": "EcoFlow app", "homeassistant": "Home Assistant"}


# Gateway fields never put in the timeline: the site's configured location
# (see smart_gateway.py). The timeline goes into the diagnostics download,
# which people attach to public issues.
_GATEWAY_PRIVATE_FIELDS = frozenset({"1236", "1238", "1240"})


def _gateway_timeline_filter(path: str, value: Any) -> bool:
    top = path.split(".", 1)[0].split("[", 1)[0]
    if top in _GATEWAY_PRIVATE_FIELDS:
        return False
    return top not in _GATEWAY_NOISY_FIELDS and not isinstance(value, float)


def _has_state(kind: str, decoded: dict[str, Any]) -> bool:
    """Whether a frame reports the charger's state (vs. a command or an error reply)."""
    if kind == "data":
        return decoded.get("format") != "raw"
    if kind != "get_reply":
        return False
    if decoded.get("format") == "dp3":
        return True
    body = decoded.get("json")
    return isinstance(body, dict) and isinstance(body.get("data"), dict) and "quotaMap" in body["data"]


class _ChargerCommand(PrivateAPIMessageProtocol):
    """A 2/81 command to the charger, framed exactly as the EcoFlow app frames it
    (verified byte for byte against captured app commands, apart from seq and
    the "from" label)."""

    def __init__(self, device_sn: str, values: dict[int, int]) -> None:
        pdata = pp.command_pdata(values)
        self._packet = dp3.DP3SendHeaderMsg()
        header = self._packet.msg.add()
        header.pdata = pdata
        header.src = 32
        header.dest = pp.CMD_DEST
        header.d_src = 1
        header.d_dest = 1
        header.check_type = 3
        header.cmd_func = pp.CMD_FUNC
        header.cmd_id = pp.CMD_ID
        header.data_len = len(pdata)
        header.need_ack = 1
        header.seq = Message.gen_seq()
        header.version = 3
        header.payload_ver = 1
        setattr(header, "from", "HomeAssistant")  # "from" is a Python keyword
        header.device_sn = device_sn
        self.seq: int = header.seq
        self._values = values

    @override
    def to_mqtt_payload(self) -> bytes:
        return self._packet.SerializeToString()

    @override
    def to_dict(self) -> dict:
        return {"cmdFunc": pp.CMD_FUNC, "cmdId": pp.CMD_ID, "seq": self.seq, "fields": dict(self._values)}


def _charge_command(device_sn: str) -> Callable[[int], _ChargerCommand]:
    return lambda value: _ChargerCommand(device_sn, pp.charge_values(bool(value)))


def _mode_command(device_sn: str) -> Callable[[int, dict[str, Any]], _ChargerCommand]:
    return lambda value, params: _ChargerCommand(device_sn, pp.mode_values(value, params.get("pp_custom_current")))


def _current_command(device_sn: str) -> Callable[[int], _ChargerCommand]:
    return lambda value: _ChargerCommand(device_sn, pp.current_values(value))


class PowerPulse(BaseInternalDevice):
    def __init__(self, device_info: EcoflowDeviceInfo, device_data: DeviceData):
        super().__init__(device_info, device_data)
        self._hass: HomeAssistant | None = None
        self._client: EcoflowApiClient | None = None
        self._capture: CaptureSession | None = None
        self._last_capture: CaptureSession | None = None
        self._capture_unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._last_message: datetime | None = None
        self._last_message_shown: datetime | None = None
        self._last_notify = 0.0
        # Latest state frame per payload key ("254_21", "json", ...).
        self._latest: dict[str, dict[str, Any]] = {}
        # When a live frame last reported each named value (monotonic), so a
        # slightly stale snapshot doesn't override it.
        self._live_at: dict[str, float] = {}
        # seqs of commands the charger has acknowledged.
        self._acked: deque[int] = deque(maxlen=64)
        self._last_poll = 0.0
        self._own_kinds: dict[str, str] = {
            topic: kind
            for kind, topic in (
                ("data", device_info.data_topic),
                ("set", device_info.set_topic),
                ("set_reply", device_info.set_reply_topic),
                ("get", device_info.get_topic),
                ("get_reply", device_info.get_reply_topic),
                ("status", device_info.status_topic),
            )
            if topic
        }

    @override
    def configure(self, hass: HomeAssistant) -> None:
        super().configure(hass)
        self._hass = hass

    # -- entities ------------------------------------------------------------ #

    @override
    def sensors(self, client: EcoflowApiClient) -> Sequence[SensorEntity]:
        self._client = client
        return [
            ChargerStateSensorEntity(client, self, "pp_state", "Charger State").attr("pp_state", "Code", None),
            BaseSensorEntity(client, self, "pp_fault_codes", "Fault Codes").with_icon("mdi:alert-circle-outline"),
            MiscSensorEntity(client, self, "pp_last_event", "Last Event")
            .with_icon("mdi:history")
            .attr("pp_last_event_code", "Code", None)
            .attr("pp_last_event_start", "Started", None)
            .attr("pp_last_event_end", "Ended", None),
            BaseSensorEntity(client, self, "pp_charge_request", "Charge Request")
            .with_icon("mdi:gesture-tap-button")
            .attr("pp_charge_request_from", "From", None)
            .attr("pp_charge_request_at", "At", None),
            WattsSensorEntity(client, self, "pp_power", "Charging Power"),
            AmpSensorEntity(client, self, "pp_current", "Charging Current"),
            VoltSensorEntity(client, self, "pp_voltage", "Voltage", diagnostic=True),
            EnergySensorEntity(client, self, "pp_session_energy", "Session Energy"),
            EnergySensorEntity(client, self, "pp_total_energy", "Total Energy"),
            VoltSensorEntity(client, self, "pp_pilot_voltage", "Pilot Signal", diagnostic=True),
            MiscSensorEntity(client, self, "pp_pilot_duty", "Pilot Duty Cycle")
            .with_unit_of_measurement(PERCENTAGE)
            .with_icon("mdi:sine-wave"),
            WattsSensorEntity(client, self, "pp_allotted_power", "Allotted Power", diagnostic=True),
            MiscSensorEntity(client, self, "pp_wifi_rssi", "Wi-Fi Signal", enabled=False)
            .with_unit_of_measurement(SIGNAL_STRENGTH_DECIBELS_MILLIWATT)
            .with_device_class(SensorDeviceClass.SIGNAL_STRENGTH),
            CaptureStatusSensor(client, self),
            LastMessageSensor(client, self),
        ]

    @override
    def binary_sensors(self, client: EcoflowApiClient) -> Sequence[BinarySensorEntity]:
        return [
            MiscBinarySensorEntity(client, self, "pp_fault", "Fault").with_device_class(BinarySensorDeviceClass.PROBLEM)
        ]

    @override
    def switches(self, client: EcoflowApiClient) -> Sequence[SwitchEntity]:
        self._client = client
        sn = self.device_info.sn
        return [
            ChargingSwitchEntity(
                client, self, "pp_charging", "Charging", _charge_command(sn), enableValue=1, disableValue=0
            ),
            CaptureSwitch(client, self),
        ]

    @override
    def buttons(self, client: EcoflowApiClient) -> Sequence[ButtonEntity]:
        return [CaptureMarkerButton(client, self)]

    @override
    def numbers(self, client: EcoflowApiClient) -> Sequence[NumberEntity]:
        return [
            CustomCurrentNumberEntity(
                client,
                self,
                "pp_custom_current",
                "Custom Current",
                pp.CURRENT_MIN_A,
                pp.CURRENT_MAX_A,
                _current_command(self.device_info.sn),
            )
        ]

    @override
    def selects(self, client: EcoflowApiClient) -> Sequence[SelectEntity]:
        return [
            ChargingModeSelectEntity(
                client, self, "pp_mode", "Charging Mode", pp.MODES, _mode_command(self.device_info.sn)
            )
        ]

    @override
    def get_quota_message(self) -> Message:
        """The app's "send everything" request (one empty DP3 header)."""
        return _GetAllRequest()

    # -- state for the entities ---------------------------------------------- #

    @property
    def capturing(self) -> bool:
        return self._capture is not None

    @property
    def capture_session(self) -> CaptureSession | None:
        return self._capture or self._last_capture

    @property
    def last_message(self) -> datetime | None:
        """Time of the last state frame from the charger, to the minute."""
        return self._last_message_shown

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self, force: bool = False) -> None:
        """Tell the entities to refresh: always when forced, when the last
        message's minute changes, and every CAPTURE_NOTIFY_SEC while capturing."""
        minute = self._last_message.replace(second=0, microsecond=0) if self._last_message else None
        due = self._capture is not None and time.monotonic() - self._last_notify >= CAPTURE_NOTIFY_SEC
        if not (force or due or minute != self._last_message_shown):
            return
        self._last_message_shown = minute
        self._last_notify = time.monotonic()
        for listener in list(self._listeners):
            listener()

    # -- incoming frames (MQTT thread) --------------------------------------- #

    @override
    def update_data(self, raw_data: bytes, data_type: str) -> bool:
        kind = self._own_kinds.get(data_type)
        if kind is None:
            # Another device's topic: record it if a capture is watching it.
            session = self._capture
            if session is not None:
                hit = session.watch.get(data_type)
                if hit is not None:
                    label, sn, watched_kind = hit
                    decoded = decode_frame(raw_data, dp3.DP3HeaderMessage)
                    self._record(session, label, sn, watched_kind, raw_data, decoded, _gateway_timeline_filter)
            return False

        decoded = decode_frame(raw_data, dp3.DP3HeaderMessage)
        now = dt_util.utcnow()
        if _has_state(kind, decoded):
            params: dict[str, Any] = {}
            for key, payload in frame_payloads(decoded):
                self._latest[key] = {"t": now.isoformat(), "kind": kind, "payload": payload}
                if isinstance(payload, (dict, list)):
                    params.update({f"{key}.{path}": v for path, v in flatten(payload).items()})
            params.update(self._named_state(raw_data, live=kind == "data"))
            self.data.add_data(PreparedData(None, {"params": params}, {"decoded": decoded}))
            self._last_message = now
        elif kind == "set":
            request = self._charge_request(raw_data)
            if request:
                # A command, not a report from the charger: don't count it as the charger being online.
                self.data.add_data(PreparedData(None, {"params": request}, None, is_auto=False))
            self.data.add_set_message(PreparedData(None, None, decoded))
        elif kind == "set_reply":
            for info, _ in dp3_headers(raw_data, dp3.DP3HeaderMessage) or []:
                if "seq" in info:
                    self._acked.append(int(info["seq"]))
            self.data.add_set_reply_message(PreparedData(None, None, decoded))
        elif kind == "get":
            self.data.add_get_message(PreparedData(None, None, decoded))
        elif kind == "get_reply":
            self.data.add_get_reply_message(PreparedData(None, None, decoded))

        session = self._capture
        if session is not None:
            self._record(session, "powerpulse", self.device_info.sn, kind, raw_data, decoded, None)
        self._notify()
        return True

    def _record(
        self,
        session: CaptureSession,
        label: str,
        sn: str,
        kind: str,
        raw: bytes,
        decoded: dict[str, Any],
        timeline_filter: Callable[[str, Any], bool] | None,
    ) -> None:
        try:
            ok = session.record_frame(dt_util.now(), label, sn, kind, raw, decoded, timeline_filter)
        except Exception:
            _LOGGER.exception("PowerPulse capture: failed to record a %s frame from %s", kind, sn)
            return
        if not ok and session.is_full and self._hass is not None:
            self._hass.loop.call_soon_threadsafe(self._stop_soon, "size limit reached")

    def _stop_soon(self, reason: str) -> None:
        if self._hass is not None and self._capture is not None:
            self._hass.async_create_task(self.async_stop_capture(reason))

    def _named_state(self, raw: bytes, live: bool) -> dict[str, Any]:
        """Decoded values (pp_*) from a frame. A snapshot's value is dropped when
        a live frame reported that value within SNAPSHOT_YIELD_SEC."""
        out: dict[str, Any] = {}
        for info, pdata in dp3_headers(raw, dp3.DP3HeaderMessage) or []:
            try:
                out.update(pp.decode_state(int(info.get("cmd_func", 0)), int(info.get("cmd_id", 0)), pdata))
            except Exception as e:  # reverse-engineered layout; never break the frame
                _LOGGER.debug("PowerPulse: couldn't decode %s/%s: %s", info.get("cmd_func"), info.get("cmd_id"), e)
        now = time.monotonic()
        if live:
            for key in out:
                self._live_at[key] = now
            return out
        return {k: v for k, v in out.items() if now - self._live_at.get(k, -SNAPSHOT_YIELD_SEC) >= SNAPSHOT_YIELD_SEC}

    def _charge_request(self, raw: bytes) -> dict[str, Any]:
        """The last Start/Stop command sent to the charger, by the EcoFlow app or
        by Home Assistant (both arrive on the account's set topic), so an
        automation can tell the charger giving up from a user stopping it."""
        for info, pdata in dp3_headers(raw, dp3.DP3HeaderMessage) or []:
            if (info.get("cmd_func"), info.get("cmd_id")) != (pp.CMD_FUNC, pp.CMD_ID):
                continue
            request = pp.charge_request(pp.command_fields(pdata))
            if request:
                origin = str(info.get("from", ""))
                return {
                    "pp_charge_request": request,
                    "pp_charge_request_from": _ORIGINS.get(origin.lower(), origin or "unknown"),
                    "pp_charge_request_at": dt_util.utcnow(),
                }
        return {}

    def acked(self, seq: int) -> bool:
        return seq in self._acked

    @property
    def session_active(self) -> bool:
        return self.data.params.get("pp_state") in (pp.STATE_CHARGING, pp.STATE_FAULT)

    async def async_poll_tick(self, _now: datetime | None = None) -> None:
        """Called every POLL_CHARGING_SEC: ask for a snapshot when due (a capture
        polls on its own)."""
        if self._capture is not None:
            return
        interval = POLL_CHARGING_SEC if self.session_active else POLL_IDLE_SEC
        if time.monotonic() - self._last_poll >= interval - 1:
            self.request_snapshot()

    # -- capture control (event loop) ---------------------------------------- #

    def _watch_topics(self) -> dict[str, tuple[str, str, str]]:
        watch: dict[str, tuple[str, str, str]] = {}
        if self._client is None:
            return watch
        for device in self._client.devices.values():
            if device is self or device.device_info.device_type not in WATCHED_DEVICE_TYPES:
                continue
            info = device.device_info
            for kind, topic in (
                ("data", info.data_topic),
                ("get_reply", info.get_reply_topic),
                ("set", info.set_topic),
                ("set_reply", info.set_reply_topic),
            ):
                if topic:
                    watch[topic] = ("gateway", info.sn, kind)
        return watch

    def _watched_params(self, watch: dict[str, tuple[str, str, str]]) -> dict[str, Any]:
        """Each watched device's decoded state at the start, as a baseline."""
        if self._client is None:
            return {}
        out: dict[str, Any] = {}
        for _, sn, _ in watch.values():
            device = self._client.devices.get(sn)
            if device is not None and sn not in out:
                out[sn] = dict(sorted(device.data.params.items()))
        return out

    async def async_start_capture(self) -> None:
        hass = self._hass
        if self._capture is not None or hass is None:
            return
        now = dt_util.now()
        path = hass.config.path(CAPTURE_DIR, f"powerpulse_{self.device_info.sn}_{now:%Y%m%d-%H%M%S}.jsonl")
        session = CaptureSession(path, now, dt_util.get_default_time_zone(), CAPTURE_MAX_BYTES)
        await hass.async_add_executor_job(session.open)
        session.watch = self._watch_topics()
        session.record_event(
            now,
            "capture_start",
            charger=self.device_info.sn,
            watching=sorted({sn for _, sn, _ in session.watch.values()}),
            max_minutes=CAPTURE_MAX_MINUTES,
            baseline={"charger": dict(self._latest), "watched": self._watched_params(session.watch)},
        )
        self._capture = session
        self._capture_unsubs = [
            async_track_time_interval(hass, self._async_poll, timedelta(seconds=CAPTURE_POLL_SEC)),
            async_call_later(hass, CAPTURE_MAX_MINUTES * 60, self._async_time_limit),
        ]
        self.request_snapshot()
        _LOGGER.info("PowerPulse %s: capture started -> %s", self.device_info.sn, path)
        self._notify(force=True)

    async def async_stop_capture(self, reason: str) -> None:
        session = self._capture
        if session is None or self._hass is None:
            return
        self._capture = None
        for unsub in self._capture_unsubs:
            unsub()
        self._capture_unsubs = []
        now = dt_util.now()
        session.stopped = now
        session.stop_reason = reason
        session.record_event(now, "capture_stop", reason=reason, summary=session.summary())
        await self._hass.async_add_executor_job(session.close)
        self._last_capture = session
        _LOGGER.info("PowerPulse %s: capture stopped (%s) -> %s", self.device_info.sn, reason, session.path)
        self._notify(force=True)

    def add_marker(self) -> None:
        session = self._capture
        if session is not None:
            session.record_marker(dt_util.now())
            self._notify(force=True)

    async def _async_poll(self, _now: datetime) -> None:
        self.request_snapshot()

    async def _async_time_limit(self, _now: datetime) -> None:
        await self.async_stop_capture(f"time limit ({CAPTURE_MAX_MINUTES} min)")

    def request_snapshot(self) -> None:
        if self._client is None:
            return
        self._last_poll = time.monotonic()
        try:
            self._client.send_get_message(self.device_info.sn, _GetAllRequest())
        except Exception as e:
            _LOGGER.debug("PowerPulse %s: snapshot request failed: %s", self.device_info.sn, e)

    # -- diagnostics ----------------------------------------------------------- #

    def diagnostics_extra(self) -> dict[str, Any]:
        session = self.capture_session
        return {
            "capturing": self.capturing,
            "last_message": self._last_message.isoformat() if self._last_message else None,
            "topics": dict(self._own_kinds),
            "latest_frames": dict(self._latest),
            "capture": session.summary() if session else None,
            "timeline": session.timeline_snapshot() if session else [],
        }


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


class _AckedCommand:
    """Mixin for charger controls: send a command once more if the charger
    hasn't acknowledged it within ACK_TIMEOUT_SEC, and ask for a snapshot once
    it has, so the entity shows the charger's new state promptly. Every command
    sets an absolute state, so repeating one is harmless."""

    _resending = False

    def send_set_message(self, target_value: Any, command: dict | Message) -> None:
        super().send_set_message(target_value, command)  # type: ignore[misc]
        seq = getattr(command, "seq", None)
        hass = self.hass  # type: ignore[attr-defined]
        if seq is None or hass is None:
            return
        asyncio.run_coroutine_threadsafe(self._async_await_ack(target_value, seq, not self._resending), hass.loop)

    async def _async_await_ack(self, value: Any, seq: int, retry: bool) -> None:
        device: PowerPulse = self._device  # type: ignore[attr-defined]
        for _ in range(int(ACK_TIMEOUT_SEC / ACK_POLL_SEC)):
            await asyncio.sleep(ACK_POLL_SEC)
            if device.acked(seq):
                device.request_snapshot()
                return
        entity_id = self.entity_id  # type: ignore[attr-defined]
        if retry:
            _LOGGER.warning(
                "%s: charger didn't acknowledge %s within %ss, sending again", entity_id, value, ACK_TIMEOUT_SEC
            )
            await self.hass.async_add_executor_job(self._resend, value)  # type: ignore[attr-defined]
        else:
            _LOGGER.warning("%s: charger didn't acknowledge %s after a second attempt", entity_id, value)

    def _resend(self, value: Any) -> None:
        self._resending = True
        try:
            self.send_set_message(value, self.command_dict(value))  # type: ignore[attr-defined]
        finally:
            self._resending = False


class ChargerStateSensorEntity(BaseSensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = pp.STATE_OPTIONS
    _attr_icon = "mdi:ev-station"

    def _update_value(self, val: Any) -> bool:
        try:
            name = pp.STATE_NAMES.get(int(val), "unknown")
        except (TypeError, ValueError):
            return False
        return super()._update_value(name)


class ChargingSwitchEntity(_AckedCommand, EnabledEntity):
    """On while a session runs (charging, or faulted and about to retry)."""

    _attr_icon = "mdi:ev-plug-type1"


class ChargingModeSelectEntity(_AckedCommand, DictSelectEntity):
    _attr_icon = "mdi:car-cog"
    _attr_entity_category = None


class CustomCurrentNumberEntity(_AckedCommand, ValueUpdateEntity):
    """Custom-mode current. Setting it selects Custom mode, as the app does."""

    _attr_icon = "mdi:current-ac"
    _attr_entity_category = None
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_mode = NumberMode.SLIDER


class _PowerPulseEntity(EcoFlowAbstractEntity):
    """Entities that act on the capture, not on the charger."""

    _attr_should_poll = False
    # For docs/gen.py, which lists each control's state key and command.
    mqtt_key = "(local)"

    def __init__(self, client: EcoflowApiClient, device: PowerPulse, title: str, key: str):
        super().__init__(client, device, title, key)
        self._pp = device

    def command_dict(self, value: Any) -> dict[str, Any]:
        raise ValueError("acts on the local capture; sends no command to the charger")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._pp.add_listener(self._schedule_write))

    def _schedule_write(self) -> None:
        """Called from the MQTT thread or the event loop."""
        hass = self.hass
        if hass is None:
            return

        def write() -> None:
            if self.hass is not None and self.entity_id:
                self.async_write_ha_state()

        hass.loop.call_soon_threadsafe(write)


class CaptureSwitch(_PowerPulseEntity, SwitchEntity):
    _attr_icon = "mdi:record-rec"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, client: EcoflowApiClient, device: PowerPulse):
        super().__init__(client, device, "Diagnostic Capture", "diagnostic_capture")

    @property
    def is_on(self) -> bool:
        return self._pp.capturing

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._pp.async_start_capture()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._pp.async_stop_capture("turned off")

    async def async_will_remove_from_hass(self) -> None:
        await self._pp.async_stop_capture("integration unloaded")
        await super().async_will_remove_from_hass()


class CaptureMarkerButton(_PowerPulseEntity, ButtonEntity):
    _attr_icon = "mdi:map-marker-plus"

    def __init__(self, client: EcoflowApiClient, device: PowerPulse):
        super().__init__(client, device, "Capture Marker", "capture_marker")

    @property
    def available(self) -> bool:
        return self._pp.capturing

    async def async_press(self) -> None:
        self._pp.add_marker()


class CaptureStatusSensor(_PowerPulseEntity, SensorEntity):
    _attr_icon = "mdi:file-document-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "recording", "stopped"]
    # Counters change every few seconds while recording; keep them out of history.
    _unrecorded_attributes = frozenset(
        {"charger_frames", "gateway_frames", "app_commands", "markers", "size_mb", "timeline_entries"}
    )

    def __init__(self, client: EcoflowApiClient, device: PowerPulse):
        super().__init__(client, device, "Capture", "capture_status")

    @property
    def native_value(self) -> str:
        if self._pp.capturing:
            return "recording"
        return "stopped" if self._pp.capture_session is not None else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        session = self._pp.capture_session
        if session is None:
            return None
        s = session.summary()
        return {
            "file": s["file"],
            "started": s["started"],
            "stopped": s["stopped"],
            "stop_reason": s["stop_reason"],
            "charger_frames": s["frames"].get("powerpulse", 0),
            "gateway_frames": s["frames"].get("gateway", 0),
            "app_commands": s["app_commands"],
            "markers": s["markers"],
            "size_mb": round(s["bytes_written"] / 1048576, 2),
            "timeline_entries": s["timeline_entries"],
        }


class LastMessageSensor(_PowerPulseEntity, SensorEntity):
    _attr_icon = "mdi:message-badge-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, client: EcoflowApiClient, device: PowerPulse):
        super().__init__(client, device, "Last Message", "last_message")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Snapshot polling lives as long as this entity does.
        self.async_on_remove(
            async_track_time_interval(self.hass, self._pp.async_poll_tick, timedelta(seconds=POLL_CHARGING_SEC))
        )

    @property
    def native_value(self) -> datetime | None:
        return self._pp.last_message
