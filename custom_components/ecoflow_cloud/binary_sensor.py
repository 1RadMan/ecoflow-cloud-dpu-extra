from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.ecoflow_cloud import ECOFLOW_DOMAIN
from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.devices import BaseDevice
from custom_components.ecoflow_cloud.devices.stream_watch import StreamWatch
from custom_components.ecoflow_cloud.entities import EcoFlowAbstractEntity, EcoFlowDictEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    client: EcoflowApiClient = hass.data[ECOFLOW_DOMAIN][entry.entry_id]
    for sn, device in client.devices.items():
        sensors = device.binary_sensors(client)
        async_add_entities(sensors)


class MiscBinarySensorEntity(BinarySensorEntity, EcoFlowDictEntity):
    def _update_value(self, val: Any) -> bool:
        self._attr_is_on = bool(val)
        return True


class StreamWatchBinarySensorEntity(BinarySensorEntity, EcoFlowAbstractEntity):
    """A flag from the device's push-stream watchdog (devices/stream_watch.py).

    Written only when the watchdog's state changes, not on every push.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(
        self,
        client: EcoflowApiClient,
        device: BaseDevice,
        key: str,
        title: str,
        flag: Callable[[StreamWatch], bool],
        with_attributes: bool = False,
    ):
        super().__init__(client, device, title, key)
        assert device.stream_watch is not None
        self._watch: StreamWatch = device.stream_watch
        self._flag = flag
        self._with_attributes = with_attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._watch.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._flag(self._watch)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return self._watch.attributes() if self._with_attributes else None


def stream_watch_binary_sensors(client: EcoflowApiClient, device: BaseDevice) -> list[BinarySensorEntity]:
    """The "Telemetry Stalled" and "Data Stale" flags, for a device with a stream watch."""
    if device.stream_watch is None:
        return []
    return [
        StreamWatchBinarySensorEntity(
            client, device, "stream_stalled", "Telemetry Stalled", lambda w: w.stalled, with_attributes=True
        ),
        StreamWatchBinarySensorEntity(client, device, "data_stale", "Data Stale", lambda w: w.data_stale),
    ]
