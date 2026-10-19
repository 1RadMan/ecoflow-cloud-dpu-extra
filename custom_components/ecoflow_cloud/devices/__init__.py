import dataclasses
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar, cast, override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.api.message import JSONDict, JSONMessage, Message
from custom_components.ecoflow_cloud.device_data import DeviceData
from custom_components.ecoflow_cloud.devices.data_coordinator import DeviceDataCoordinator
from custom_components.ecoflow_cloud.devices.data_holder import EcoflowDataHolder, PreparedData
from custom_components.ecoflow_cloud.devices.status_tracker import StatusTracker
from custom_components.ecoflow_cloud.devices.stream_watch import (
    REPLY_EMPTY,
    REPLY_FRESH,
    REPLY_REPLAYED,
    WATCH_TICK_SEC,
    StreamWatch,
)

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class EcoflowDeviceInfo:
    public_api: bool
    sn: str
    name: str
    device_type: str
    status: int
    data_topic: str
    set_topic: str
    set_reply_topic: str
    get_topic: str | None
    get_reply_topic: str | None
    status_topic: str | None = None

    quota_all: str = "/iot-open/sign/device/quota/all"

    def topics(self) -> list[str]:
        topics: list[str | None] = [
            self.data_topic,
            self.get_topic,
            self.get_reply_topic,
            self.set_topic,
            self.set_reply_topic,
            self.status_topic,
        ]
        return [t for t in topics if t is not None]


class NoQuotaMessageError(Exception):
    pass


class BaseDevice(ABC):
    # Push-stream watchdog (see stream_watch.py), for devices that push every
    # couple of seconds: seconds without a push before the stream counts as
    # stalled (None = not watched), and whether to send get_quota_message()
    # early while it is.
    STREAM_STALL_SEC: ClassVar[float | None] = None
    STREAM_REFRESH: ClassVar[bool] = False

    def __init__(self, device_info: EcoflowDeviceInfo, device_data: DeviceData):
        super().__init__()
        self.coordinator: DeviceDataCoordinator
        self.device_info: EcoflowDeviceInfo = device_info
        self.power_step: int = device_data.options.power_step
        self.device_data: DeviceData = device_data

        self.status_tracker = StatusTracker(
            self.device_data.options.assume_offline_sec,
            device_info.status,
        )

        module_sn = self.device_data.sn if self.device_data.parent is not None else None
        self.data = EcoflowDataHolder(
            module_sn,
            self.device_data.options.diagnostic_mode,
            status_callback=self.status_tracker,
        )
        self.stream_watch: StreamWatch | None = (
            StreamWatch(device_info.sn, self.STREAM_STALL_SEC, self.STREAM_REFRESH)
            if self.STREAM_STALL_SEC is not None
            else None
        )

    def configure(self, hass: HomeAssistant):
        self.coordinator = DeviceDataCoordinator(hass, self.data, self.device_data.options.refresh_period)

    def start_stream_watch(self, hass: HomeAssistant, client: EcoflowApiClient) -> Callable[[], None] | None:
        """Run the push-stream watchdog; returns its stop function (None if not watched)."""
        watch = self.stream_watch
        if watch is None:
            return None
        watch.reset(dt.utcnow())
        name = self.device_data.display_name or self.device_data.name

        @callback
        def _tick(_now: datetime) -> None:
            result = watch.tick(dt.utcnow())
            mqtt = getattr(client, "mqtt_client", None)
            # Not while disconnected: paho would queue every request and send them all on reconnect.
            if result.refresh and mqtt is not None and mqtt.is_connected():
                hass.async_create_background_task(
                    client.quota_all(self.device_info.sn), f"ecoflow stream refresh {self.device_info.sn}"
                )
            for event, data in result.events:
                hass.bus.async_fire(event, {**data, "name": name})
            if result.changed:
                watch.notify()

        return async_track_time_interval(hass, _tick, timedelta(seconds=WATCH_TICK_SEC))

    def _screen_reply(self, data: PreparedData) -> PreparedData:
        """Classify a get_reply for the stream watch, and drop replayed state.

        While pushes are stalled, a reply that changes none of the values we
        already have is the device (or cloud) repeating a frozen snapshot: it
        is kept for diagnostics but not merged, so it doesn't count as data.
        """
        watch = self.stream_watch
        assert watch is not None
        params = data.params.get("params") if data.params else None
        if not params:
            watch.on_reply(REPLY_EMPTY)
            return data
        current = self.data.params
        if watch.push_stalled() and all(k in current and current[k] == v for k, v in params.items()):
            watch.on_reply(REPLY_REPLAYED)
            return PreparedData(data.online, None, data.raw_data, data.is_auto)
        watch.on_reply(REPLY_FRESH)
        return data

    @staticmethod
    def default_charging_power_step() -> int:
        return 100

    def charging_power_step(self) -> int:
        if self.power_step == -1:
            return self.default_charging_power_step()
        else:
            return self.power_step

    def flat_json(self) -> bool:
        return True

    async def async_restore_state(self):
        """Restore persisted device state on startup. Override in subclasses."""

    def extract_quota_data(self, message: JSONDict) -> PreparedData | None:
        return None

    @abstractmethod
    def sensors(self, client: EcoflowApiClient) -> Sequence[SensorEntity]:
        pass

    @abstractmethod
    def numbers(self, client: EcoflowApiClient) -> Sequence[NumberEntity]:
        pass

    @abstractmethod
    def switches(self, client: EcoflowApiClient) -> Sequence[SwitchEntity]:
        pass

    @abstractmethod
    def selects(self, client: EcoflowApiClient) -> Sequence[SelectEntity]:
        pass

    def binary_sensors(self, client: EcoflowApiClient) -> Sequence[BinarySensorEntity]:
        return []

    def buttons(self, client: EcoflowApiClient) -> Sequence[ButtonEntity]:
        return []

    def update_data(self, raw_data: bytes, data_type: str) -> bool:
        if data_type == self.device_info.data_topic:
            data = self._prepare_data_data_topic(raw_data)
            self.data.add_data(data)
            if self.stream_watch is not None:
                self.stream_watch.on_push()
        elif data_type == self.device_info.set_topic:
            data = self._prepare_data_set_topic(raw_data)
            self.data.add_set_message(data)
        elif data_type == self.device_info.set_reply_topic:
            data = self._prepare_data_set_reply_topic(raw_data)
            self.data.add_set_reply_message(data)
        elif data_type == self.device_info.get_topic:
            data = self._prepare_data_get_topic(raw_data)
            self.data.add_get_message(data)
            if self.stream_watch is not None:
                self.stream_watch.on_request()
        elif data_type == self.device_info.get_reply_topic:
            data = self._prepare_data_get_reply_topic(raw_data)
            if self.stream_watch is not None:
                data = self._screen_reply(data)
            self.data.add_get_reply_message(data)
        elif data_type == self.device_info.status_topic:
            data = self._prepare_data_status_topic(raw_data)
            self.data.add_status(data)
        else:
            return False
        return True

    def _prepare_data_data_topic(self, raw_data: bytes) -> PreparedData:
        data = self._prepare_data(raw_data)
        return PreparedData(None, data, data)

    def _prepare_data_set_topic(self, raw_data: bytes) -> PreparedData:
        return PreparedData(None, None, self._prepare_data(raw_data))

    def _prepare_data_set_reply_topic(self, raw_data: bytes) -> PreparedData:
        return PreparedData(None, None, self._prepare_data(raw_data))

    def _prepare_data_get_topic(self, raw_data: bytes) -> PreparedData:
        return PreparedData(None, None, self._prepare_data(raw_data))

    def _prepare_data_get_reply_topic(self, raw_data: bytes) -> PreparedData:
        return PreparedData(None, None, self._prepare_data(raw_data))

    def _prepare_data_status_topic(self, raw_data: bytes) -> PreparedData:
        data = self._prepare_data(raw_data)
        if "params" in data and "status" in data["params"]:
            return PreparedData(int(data["params"]["status"]) == 1, None, data)
        return PreparedData(None, None, data)

    def _prepare_data(self, raw_data: bytes) -> dict[str, Any]:
        try:
            try:
                payload = raw_data.decode("utf-8", errors="ignore")
                return json.loads(payload)
            except UnicodeDecodeError as error:
                _LOGGER.warning(f"UnicodeDecodeError: {error}. Trying to load json.")
                return json.loads(raw_data)
            except Exception as error:
                _LOGGER.warning(f"Exception: {error}. Trying to load json.")
                return json.loads(raw_data)
        except Exception as error1:
            _LOGGER.error(f"constant: {error1}. Ignoring message and waiting for the next one.")
            return {}


class BaseInternalDevice(BaseDevice):
    def __init__(self, device_info: EcoflowDeviceInfo, device_data: DeviceData):
        super().__init__(device_info, device_data)

    @override
    def _prepare_data_get_reply_topic(self, raw_data: bytes) -> PreparedData:
        data = self._prepare_data(raw_data)
        if "operateType" in data and data["operateType"] == "latestQuotas":
            message_data = data["data"]
            assert isinstance(message_data, dict)

            online = int(cast(bool | int | str, message_data["online"]))
            if online == 1:
                return PreparedData(online == 1, {"params": message_data["quotaMap"], "time": dt.utcnow()}, data)
            else:
                return PreparedData(False, None, data)
        return PreparedData(None, None, data)

    def get_quota_message(self) -> Message:
        return JSONMessage(
            {
                "version": "1.1",
                "moduleType": 0,
                "operateType": "latestQuotas",
                "params": {},
            }
        )


class DiagnosticDevice(BaseDevice):
    def sensors(self, client: EcoflowApiClient) -> Sequence[SensorEntity]:
        return []

    def numbers(self, client: EcoflowApiClient) -> Sequence[NumberEntity]:
        return []

    def switches(self, client: EcoflowApiClient) -> Sequence[SwitchEntity]:
        return []

    def buttons(self, client: EcoflowApiClient) -> Sequence[ButtonEntity]:
        return []

    def selects(self, client: EcoflowApiClient) -> Sequence[SelectEntity]:
        return []
