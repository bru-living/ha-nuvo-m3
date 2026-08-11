"""M3 Options exposed as friendly Home Assistant switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NuvoM3ConfigEntry
from .const import OUTPUTS
from .entity import NuvoM3Entity
from .protocol import NuvoM3Error


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NuvoM3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(NuvoM3JukeboxSwitch(entry, output) for output in OUTPUTS)


class NuvoM3JukeboxSwitch(NuvoM3Entity, SwitchEntity):
    """Juke Box Mode for one physical M3 output."""

    _attr_icon = "mdi:playlist-plus"
    _attr_should_poll = False

    def __init__(self, entry: NuvoM3ConfigEntry, output: str) -> None:
        super().__init__(entry)
        self.output = output
        self._attr_name = f"Output {output} Juke Box mode"
        self._attr_unique_id = f"{entry.entry_id}_output_{output.lower()}_jukebox"
        self._attr_is_on = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        try:
            self._attr_is_on = await self.client.get_jukebox_mode(self.output)
        except NuvoM3Error:
            self._attr_is_on = None

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = await self.client.set_jukebox_mode(self.output, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = await self.client.set_jukebox_mode(self.output, False)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh on an explicit homeassistant.update_entity request."""
        self._attr_is_on = await self.client.get_jukebox_mode(self.output)
