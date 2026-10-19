"""Diagnostics support for NuVo M3."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL_PATH

TO_REDACT = {CONF_HOST, CONF_PORT, CONF_SERIAL_PATH}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    client = entry.runtime_data
    data = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "server_status": client.server_status,
        "versions": client.versions,
        "connection": asdict(client.diagnostics),
        "outputs": {key: asdict(value) for key, value in client.outputs.items()},
        "managed_playlists": {
            name: len(tracks)
            for name, tracks in client.playlist_manager.playlists.items()
        },
        "artwork_enabled": client.artwork_manager is not None,
        "library_index": asdict(client.library_manager.status),
    }
    return data
