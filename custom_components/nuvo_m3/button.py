"""Library indexing controls for NuVo NV-M3."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NuvoM3ConfigEntry
from .entity import NuvoM3Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NuvoM3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities((ScanLibraryButton(entry), CancelLibraryScanButton(entry)))


class ScanLibraryButton(NuvoM3Entity, ButtonEntity):
    """Start a full scan using output A."""

    _attr_translation_key = "scan_library"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: NuvoM3ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_scan_library"

    async def async_press(self) -> None:
        self.client.library_manager.start("A")


class CancelLibraryScanButton(NuvoM3Entity, ButtonEntity):
    """Cancel the active scan."""

    _attr_translation_key = "cancel_library_scan"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: NuvoM3ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_cancel_library_scan"

    async def async_press(self) -> None:
        self.client.library_manager.cancel()
