"""Diagnostic sensors for NuVo M3."""

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NuvoM3ConfigEntry
from .entity import NuvoM3Entity


@dataclass(frozen=True, kw_only=True)
class M3SensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda client: None


SENSORS = (
    M3SensorDescription(
        key="server_status",
        translation_key="server_status",
        value_fn=lambda c: c.server_status,
    ),
    M3SensorDescription(
        key="main_version",
        translation_key="main_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.versions[0] if c.versions else None,
    ),
    M3SensorDescription(
        key="output_a_version",
        translation_key="output_a_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.versions[1] if c.versions else None,
    ),
    M3SensorDescription(
        key="output_b_version",
        translation_key="output_b_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.versions[2] if c.versions else None,
    ),
    M3SensorDescription(
        key="output_c_version",
        translation_key="output_c_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.versions[3] if c.versions else None,
    ),
    M3SensorDescription(
        key="library_index",
        translation_key="library_index",
        value_fn=lambda c: c.library_manager.status.state,
    ),
    M3SensorDescription(
        key="library_scan_progress",
        translation_key="library_scan_progress",
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.library_manager.status.progress,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NuvoM3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(NuvoM3Sensor(entry, description) for description in SENSORS)


class NuvoM3Sensor(NuvoM3Entity, SensorEntity):
    entity_description: M3SensorDescription

    def __init__(self, entry: NuvoM3ConfigEntry, description: M3SensorDescription) -> None:
        super().__init__(entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.client)

    @property
    def extra_state_attributes(self):
        if self.entity_description.key != "library_index":
            return None
        status = self.client.library_manager.status
        return {
            "phase": status.phase,
            "artists": status.artists,
            "albums": status.albums,
            "tracks": status.tracks,
            "global_tracks": status.global_tracks,
            "skipped_artists": status.skipped_artists,
            "skipped_albums": status.skipped_albums,
            "last_indexed": status.last_indexed,
            "error": status.error,
        }
