"""Shared NuVo M3 entity."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import NuvoM3ConfigEntry
from .const import DOMAIN, MANUFACTURER, MODEL
from .protocol import NuvoM3Client


class NuvoM3Entity(Entity):
    """Base entity tied to one M3 server."""

    _attr_has_entity_name = True

    def __init__(self, entry: NuvoM3ConfigEntry) -> None:
        self.client: NuvoM3Client = entry.runtime_data
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=entry.title,
        )
        self._remove_callback = None

    @property
    def available(self) -> bool:
        return self.client.diagnostics.connected

    async def async_added_to_hass(self) -> None:
        self._remove_callback = self.client.add_update_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_callback:
            self._remove_callback()

    def _handle_update(self) -> None:
        self.async_write_ha_state()

