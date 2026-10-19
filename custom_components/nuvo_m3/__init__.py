"""NuVo NV-M3 integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ARTWORK,
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_PORT,
    CONF_SERIAL_PATH,
    CONNECTION_SERIAL,
    DEFAULT_BAUD_RATE,
    DOMAIN,
)
from .artwork import ArtworkManager
from .protocol import NuvoM3Client, NuvoM3ConnectionError, NuvoM3ProtocolError
from .playlist import PlaylistManager
from .library import LibraryManager

PLATFORMS = [Platform.BUTTON, Platform.MEDIA_PLAYER, Platform.SENSOR, Platform.SWITCH]
type NuvoM3ConfigEntry = ConfigEntry[NuvoM3Client]


async def async_setup_entry(hass: HomeAssistant, entry: NuvoM3ConfigEntry) -> bool:
    """Set up an M3 config entry."""
    client = NuvoM3Client(entry.data, entry.data.get(CONF_BAUD_RATE, DEFAULT_BAUD_RATE))
    try:
        await client.connect()
        await client.initialize()
        await client.wait_for_server_status()
    except (NuvoM3ConnectionError, NuvoM3ProtocolError) as err:
        await client.disconnect()
        raise ConfigEntryNotReady from err
    entry.runtime_data = client
    manager = PlaylistManager(hass, client)
    await manager.async_load()
    client.playlist_manager = manager
    client.artwork_manager = ArtworkManager(hass) if entry.data.get(CONF_ARTWORK, False) else None
    library = LibraryManager(hass, client, entry.entry_id)
    await library.async_load()
    client.library_manager = library
    _async_register_services(hass, manager, library)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NuvoM3ConfigEntry) -> bool:
    """Unload an M3 config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.playlist_manager.async_stop_all()
        await entry.runtime_data.library_manager.async_stop()
        for service in (
            "create_playlist",
            "delete_playlist",
            "add_current_track",
            "remove_playlist_track",
            "play_playlist",
            "stop_playlist",
            "scan_library",
            "cancel_library_scan",
        ):
            hass.services.async_remove(DOMAIN, service)
        await entry.runtime_data.disconnect()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate the original serial-only entry to the shared connection schema."""
    if entry.version == 1:
        data = dict(entry.data)
        legacy_path = data.pop(CONF_PORT)
        data.pop(CONF_BAUD_RATE, None)
        data.update(
            {
                CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
                CONF_SERIAL_PATH: legacy_path,
            }
        )
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


def _async_register_services(
    hass: HomeAssistant, manager: PlaylistManager, library: LibraryManager
) -> None:
    """Register persistent-playlist actions for the single M3 entry."""

    async def handle(call: ServiceCall) -> None:
        try:
            if call.service == "create_playlist":
                await manager.async_create(call.data["name"])
            elif call.service == "delete_playlist":
                await manager.async_delete(call.data["name"])
            elif call.service == "add_current_track":
                await manager.async_add_current(call.data["name"], call.data["output"])
            elif call.service == "remove_playlist_track":
                await manager.async_remove_track(call.data["name"], call.data["track_number"])
            elif call.service == "play_playlist":
                manager.play(call.data["name"], call.data["output"])
            elif call.service == "stop_playlist":
                manager.stop(call.data["output"])
            elif call.service == "scan_library":
                library.start(call.data["output"])
            elif call.service == "cancel_library_scan":
                library.cancel()
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    name_schema = vol.Schema({vol.Required("name"): cv.string})
    output_schema = vol.In(["A", "B", "C"])
    schemas = {
        "create_playlist": name_schema,
        "delete_playlist": name_schema,
        "add_current_track": vol.Schema(
            {vol.Required("name"): cv.string, vol.Required("output"): output_schema}
        ),
        "remove_playlist_track": vol.Schema(
            {
                vol.Required("name"): cv.string,
                vol.Required("track_number"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            }
        ),
        "play_playlist": vol.Schema(
            {vol.Required("name"): cv.string, vol.Required("output"): output_schema}
        ),
        "stop_playlist": vol.Schema({vol.Required("output"): output_schema}),
        "scan_library": vol.Schema(
            {vol.Optional("output", default="A"): output_schema}
        ),
        "cancel_library_scan": vol.Schema({}),
    }
    for service, schema in schemas.items():
        hass.services.async_register(DOMAIN, service, handle, schema=schema)
