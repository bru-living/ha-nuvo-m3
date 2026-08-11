"""Constants for the NuVo M3 integration."""

from typing import Final

DOMAIN: Final = "nuvo_m3"
PLATFORMS: Final = ["media_player", "sensor"]
CONF_PORT: Final = "port"
CONF_BAUD_RATE: Final = "baud_rate"
CONF_CONNECTION_TYPE: Final = "connection_type"
CONF_SERIAL_PATH: Final = "serial_path"
CONF_ARTWORK: Final = "enable_artwork"
CONNECTION_TCP: Final = "tcp"
CONNECTION_SERIAL: Final = "serial"
DEFAULT_TCP_PORT: Final = 4999
DEFAULT_BAUD_RATE: Final = 57600
OUTPUTS: Final = ("A", "B", "C")
MANUFACTURER: Final = "NuVo Technologies"
MODEL: Final = "NV-M3 Music Server"
SIGNAL_UPDATE: Final = f"{DOMAIN}_update"
