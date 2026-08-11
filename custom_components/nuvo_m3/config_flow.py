"""Config flow for NuVo NV-M3."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_ARTWORK,
    CONF_CONNECTION_TYPE,
    CONF_SERIAL_PATH,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    DEFAULT_TCP_PORT,
    DOMAIN,
)
from .protocol import NuvoM3Client, NuvoM3ConnectionError, NuvoM3ProtocolError

_LOGGER = logging.getLogger(__name__)


class NuvoM3ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an M3 using the same connection choices as NV-T2FAM."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose TCP or a local serial device."""
        if user_input is not None:
            self.context[CONF_CONNECTION_TYPE] = user_input[CONF_CONNECTION_TYPE]
            self.context[CONF_ARTWORK] = user_input[CONF_ARTWORK]
            return await self.async_step_connection()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECTION_TYPE, default=CONNECTION_TCP
                    ): vol.In([CONNECTION_TCP, CONNECTION_SERIAL]),
                    vol.Required(CONF_ARTWORK, default=False): bool,
                }
            ),
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and verify the selected endpoint."""
        connection_type = self.context[CONF_CONNECTION_TYPE]
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_CONNECTION_TYPE: connection_type,
                CONF_ARTWORK: self.context.get(CONF_ARTWORK, False),
                **user_input,
            }
            if await self._async_test_connection(data, errors):
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="NuVo M3", data=data)
        return self.async_show_form(
            step_id="connection",
            data_schema=self._connection_schema(connection_type),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow the connection type and endpoint to be changed."""
        entry = self._get_reconfigure_entry()
        current = dict(entry.data)
        errors: dict[str, str] = {}

        if user_input is not None and CONF_CONNECTION_TYPE in user_input:
            self.context[CONF_CONNECTION_TYPE] = user_input[CONF_CONNECTION_TYPE]
            self.context[CONF_ARTWORK] = user_input[CONF_ARTWORK]
            self.context["reconfigure_data"] = current
            return await self.async_step_reconfigure_connection()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECTION_TYPE,
                        default=current[CONF_CONNECTION_TYPE],
                    ): vol.In([CONNECTION_TCP, CONNECTION_SERIAL]),
                    vol.Required(
                        CONF_ARTWORK, default=current.get(CONF_ARTWORK, False)
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and verify a replacement endpoint."""
        entry = self._get_reconfigure_entry()
        current = self.context["reconfigure_data"]
        connection_type = self.context[CONF_CONNECTION_TYPE]
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_CONNECTION_TYPE: connection_type,
                CONF_ARTWORK: self.context.get(CONF_ARTWORK, False),
                **user_input,
            }
            if await self._async_test_connection(data, errors):
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure_connection",
            data_schema=self._connection_schema(connection_type, current),
            errors=errors,
        )

    async def _async_test_connection(
        self, data: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        client = NuvoM3Client(data)
        try:
            await client.probe()
        except (NuvoM3ConnectionError, NuvoM3ProtocolError, OSError, TimeoutError, asyncio.IncompleteReadError):
            errors["base"] = "cannot_connect"
        except Exception:  # pragma: no cover - defensive flow boundary
            _LOGGER.exception("Unexpected error while connecting to the NuVo M3")
            errors["base"] = "unknown"
        else:
            return True
        return False

    @staticmethod
    def _connection_schema(
        connection_type: str, current: dict[str, Any] | None = None
    ) -> vol.Schema:
        current = current or {}
        if connection_type == CONNECTION_SERIAL:
            serial_key = vol.Required(CONF_SERIAL_PATH)
            if CONF_SERIAL_PATH in current:
                serial_key = vol.Required(
                    CONF_SERIAL_PATH, default=current[CONF_SERIAL_PATH]
                )
            return vol.Schema({serial_key: str})
        host_key = vol.Required(CONF_HOST)
        port_key = vol.Required(CONF_PORT, default=DEFAULT_TCP_PORT)
        if CONF_HOST in current:
            host_key = vol.Required(CONF_HOST, default=current[CONF_HOST])
        if CONF_PORT in current:
            port_key = vol.Required(CONF_PORT, default=current[CONF_PORT])
        return vol.Schema(
            {
                host_key: str,
                port_key: vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            }
        )
