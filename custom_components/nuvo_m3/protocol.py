"""Async RS232 protocol client for the NuVo NV-M3."""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import re
from typing import Any, Callable, Mapping

import serial_asyncio_fast

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_SERIAL_PATH,
    CONNECTION_SERIAL,
    DEFAULT_BAUD_RATE,
    OUTPUTS,
)

_LOGGER = logging.getLogger(__name__)
_OUTPUT_STATUS = re.compile(r"^#OUT'?([ABC])'?STATUS,(.*)$", re.IGNORECASE)
_LICENSE_ERROR = re.compile(r"^#OUT'?([ABC])'?LICENSEERROR$", re.IGNORECASE)
_MENU = re.compile(r"^#OUT'?([ABC])'?MENU,(.*)$", re.IGNORECASE)
_MENU_ITEM = re.compile(r"^#OUT'?([ABC])'?MENUITEM,(.*)$", re.IGNORECASE)
_MENU_UNAVAILABLE = re.compile(r"^#OUT'?([ABC])'?MENUUNAVAILABLE$", re.IGNORECASE)
_MENU_EXIT = re.compile(r"^#OUT'?([ABC])'?MENUEXIT$", re.IGNORECASE)


class NuvoM3Error(Exception):
    """Base M3 error."""


class NuvoM3ConnectionError(NuvoM3Error):
    """M3 connection failed."""


class NuvoM3ProtocolError(NuvoM3Error):
    """M3 rejected a command."""


@dataclass(slots=True)
class OutputState:
    """State of one M3 audio output."""

    output: str
    play_status: int = 1
    current_track: int = 0
    total_tracks: int = 0
    artist: str = ""
    album: str = ""
    title: str = ""
    elapsed: float = 0
    duration: float = 0
    shuffle: bool = False
    repeat: bool = False
    updated_at: datetime | None = None
    license_errors: int = 0


@dataclass(slots=True)
class Diagnostics:
    """Connection diagnostics."""

    connected: bool = False
    packets_received: int = 0
    commands_sent: int = 0
    reconnects: int = 0
    protocol_errors: int = 0
    last_packet: str | None = None
    last_packet_at: datetime | None = None
    last_error: str | None = None
    menu_keepalives_sent: int = 0


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One dynamically generated M3 menu item."""

    item_id: int
    title: str
    flags: int
    index: int

    @property
    def can_expand(self) -> bool:
        return bool(self.flags & 1) and not self.disabled

    @property
    def can_play(self) -> bool:
        return (bool(self.flags & 2) or not self.can_expand) and not self.disabled

    @property
    def disabled(self) -> bool:
        return bool(self.flags & 4)


@dataclass(slots=True)
class MenuPage:
    """A complete M3 menu, assembled from blocks of up to 20 items."""

    menu_id: int
    title: str
    total: int
    start: int
    returned: int
    active_index: int
    items: list[MenuItem] = field(default_factory=list)


UpdateCallback = Callable[[], None]


class NuvoM3Client:
    """Maintain an RS232 connection and parse solicited/unsolicited packets."""

    def __init__(
        self, config: Mapping[str, Any] | str, baud_rate: int = DEFAULT_BAUD_RATE
    ) -> None:
        """Create a client from config-entry data or a direct serial URL."""
        if isinstance(config, str):
            self.config = {
                CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
                CONF_SERIAL_PATH: config,
            }
        else:
            self.config = dict(config)
        self.baud_rate = baud_rate
        self.outputs = {output: OutputState(output) for output in OUTPUTS}
        self.server_status: str | None = None
        self.versions: tuple[str, str, str, str] | None = None
        self.diagnostics = Diagnostics()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._closing = False
        self._write_lock = asyncio.Lock()
        self._last_write = 0.0
        self._minimum_write_gap = 0.25
        self._callbacks: set[UpdateCallback] = set()
        self._connected_event = asyncio.Event()
        self._status_event = asyncio.Event()
        self._menu_locks = {output: asyncio.Lock() for output in OUTPUTS}
        self._menu_futures: dict[str, asyncio.Future[MenuPage] | None] = {output: None for output in OUTPUTS}
        self._menu_pages: dict[str, MenuPage | None] = {output: None for output in OUTPUTS}
        self._menu_keepalive_tasks: dict[str, asyncio.Task[None] | None] = {
            output: None for output in OUTPUTS
        }
        self._menu_keepalive_interval = 10.0
        self._menu_keepalive_timeout = 60.0

    def add_update_callback(self, callback: UpdateCallback) -> Callable[[], None]:
        self._callbacks.add(callback)
        return lambda: self._callbacks.discard(callback)

    async def connect(self) -> None:
        """Open the serial connection and start the reader."""
        self._closing = False
        try:
            self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
                url=self._connection_url,
                baudrate=self.baud_rate,
                bytesize=8,
                parity="N",
                stopbits=1,
                xonxoff=False,
                rtscts=False,
            )
        except (OSError, ValueError) as err:
            self.diagnostics.last_error = str(err)
            raise NuvoM3ConnectionError(str(err)) from err
        self.diagnostics.connected = True
        self.diagnostics.last_error = None
        self._connected_event.set()
        self._reader_task = asyncio.create_task(self._read_loop(), name="nuvo_m3_reader")
        self._status_task = asyncio.create_task(
            self._status_poll_loop(), name="nuvo_m3_status_poll"
        )

    @property
    def _connection_url(self) -> str:
        """Return a pyserial URL for a local serial device or raw TCP bridge."""
        if self.config[CONF_CONNECTION_TYPE] == CONNECTION_SERIAL:
            return str(self.config[CONF_SERIAL_PATH])
        return f"socket://{self.config['host']}:{self.config['port']}"

    async def probe(self) -> tuple[str, str, str, str]:
        """Connect and require a valid four-part M3 version response."""
        self._version_event = asyncio.Event()
        try:
            await asyncio.wait_for(self.connect(), timeout=8)
            await self.send("VER?")
            await asyncio.wait_for(self._version_event.wait(), timeout=5)
            assert self.versions is not None
            return self.versions
        finally:
            await self.disconnect()

    async def disconnect(self) -> None:
        """Close the connection and cancel reconnects."""
        self._closing = True
        for task in (
            self._reader_task,
            self._reconnect_task,
            self._status_task,
            *self._menu_keepalive_tasks.values(),
        ):
            if task and task is not asyncio.current_task():
                task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, AttributeError):
                pass
        self._set_disconnected()

    async def initialize(self) -> None:
        """Request initial server and output state."""
        await self.send("VER?")
        await self.send("STATUS?")
        for output in OUTPUTS:
            await self.send(f"OUT'{output}'STATUS?")

    async def wait_for_server_status(self) -> str:
        """Require a valid server status, retrying if startup traffic missed it."""
        if self.server_status is not None:
            return self.server_status
        for _attempt in range(3):
            self._status_event.clear()
            await self.send("STATUS?")
            try:
                await asyncio.wait_for(self._status_event.wait(), timeout=3)
            except TimeoutError:
                continue
            if self.server_status is not None:
                return self.server_status
        raise NuvoM3ProtocolError("No valid #STATUS response received from the NV-M3")

    async def send(self, command: str) -> None:
        """Send one command, reconnecting in the background after failures."""
        if not self._writer or not self.diagnostics.connected:
            raise NuvoM3ConnectionError("Serial connection is not available")
        async with self._write_lock:
            try:
                loop = asyncio.get_running_loop()
                remaining = self._minimum_write_gap - (loop.time() - self._last_write)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                if not self._writer or not self.diagnostics.connected:
                    raise NuvoM3ConnectionError("Serial connection was lost before the command was sent")
                self._writer.write(f"*{command}\r".encode("iso-8859-1"))
                await self._writer.drain()
                self._last_write = loop.time()
                self.diagnostics.commands_sent += 1
            except (OSError, ConnectionError) as err:
                self.diagnostics.last_error = str(err)
                self._connection_lost()
                raise NuvoM3ConnectionError(str(err)) from err

    async def play(self, output: str) -> None:
        await self.send(f"OUT'{output}'PLAY")

    async def pause(self, output: str) -> None:
        await self.send(f"OUT'{output}'PAUSE")

    async def next_track(self, output: str) -> None:
        await self.send(f"OUT'{output}'NEXTTRACK")

    async def previous_track(self, output: str) -> None:
        await self.send(f"OUT'{output}'PREVIOUSTRACK")

    async def skip(self, output: str, seconds: float) -> None:
        tenths = max(0, round(abs(seconds) * 10))
        direction = "SKIPFORWARD" if seconds >= 0 else "SKIPBACK"
        await self.send(f"OUT'{output}'{direction},{tenths}")

    async def set_shuffle(self, output: str, enabled: bool) -> None:
        await self.send(f"OUT'{output}'SHUFFLE,{int(enabled)}")

    async def set_repeat(self, output: str, enabled: bool) -> None:
        await self.send(f"OUT'{output}'REPEAT,{int(enabled)}")

    async def browse_menu(
        self, output: str, path: list[tuple[int, int, int]], start_index: int = 0
    ) -> MenuPage:
        """Replay a path and return one M3 menu block of at most 20 items."""
        async with self._menu_locks[output]:
            self._cancel_menu_keepalive(output)
            await self._reset_menu(output)
            page = await self._request_menu(output, f"OUT'{output}'MENUUP,0,0,0")
            for menu_id, item_id, index in path:
                page = await self._request_menu(
                    output, f"OUT'{output}'MENUSELECT,{menu_id},{item_id},{index}"
                )
            if start_index and start_index != page.start:
                page = await self._request_menu(
                    output,
                    f"OUT'{output}'MENUREQUEST,{page.menu_id},{start_index}",
                )
            self._start_menu_keepalive(output, page.menu_id)
            return page

    async def play_menu_item(
        self, output: str, parents: list[tuple[int, int, int]], item: tuple[int, int, int]
    ) -> None:
        """Replay a menu path and play its final item."""
        async with self._menu_locks[output]:
            self._cancel_menu_keepalive(output)
            await self._reset_menu(output)
            page = await self._request_menu(output, f"OUT'{output}'MENUUP,0,0,0")
            for menu_id, item_id, index in parents:
                page = await self._request_menu(
                    output, f"OUT'{output}'MENUSELECT,{menu_id},{item_id},{index}"
                )
            menu_id, item_id, index = item
            if page.menu_id == menu_id and not (
                page.start <= index < page.start + page.returned
            ):
                page_start = (index // 20) * 20
                await self._request_menu(
                    output,
                    f"OUT'{output}'MENUREQUEST,{menu_id},{page_start}",
                )
            await self.send(f"OUT'{output}'MENUPLAY,{menu_id},{item_id},{index}")
            # The M3 normally sends unsolicited output status after playback,
            # but explicitly refresh it for bridges/controllers that miss that packet.
            await asyncio.sleep(0.5)
            await self.send(f"OUT'{output}'STATUS?")

    async def get_jukebox_mode(self, output: str) -> bool:
        """Read the checkmarked Juke Box Mode option from the dynamic menu."""
        async with self._menu_locks[output]:
            self._cancel_menu_keepalive(output)
            try:
                page = await self._open_options_menu(output)
                item = self._find_jukebox_option(page)
                return bool(item.flags & 8)
            finally:
                await self.send(f"OUT'{output}'MENUEXIT")

    async def set_jukebox_mode(self, output: str, enabled: bool) -> bool:
        """Set Juke Box Mode through the M3's checkmarked Options menu."""
        async with self._menu_locks[output]:
            self._cancel_menu_keepalive(output)
            try:
                page = await self._open_options_menu(output)
                item = self._find_jukebox_option(page)
                current = bool(item.flags & 8)
                if current == enabled:
                    return current
                page = await self._request_menu(
                    output,
                    f"OUT'{output}'MENUSELECT,{page.menu_id},{item.item_id},{item.index}",
                )
                return bool(self._find_jukebox_option(page).flags & 8)
            finally:
                await self.send(f"OUT'{output}'MENUEXIT")

    async def _open_options_menu(self, output: str) -> MenuPage:
        """Enter the firmware-defined M3 Options menu."""
        await self._reset_menu(output)
        root = await self._request_menu(output, f"OUT'{output}'MENUUP,0,0,0")
        option = next(
            (item for item in root.items if "option" in item.title.casefold()),
            None,
        )
        if option is None:
            raise NuvoM3ProtocolError("The M3 Options menu was not found")
        return await self._request_menu(
            output,
            f"OUT'{output}'MENUSELECT,{root.menu_id},{option.item_id},{option.index}",
        )

    @staticmethod
    def _find_jukebox_option(page: MenuPage) -> MenuItem:
        item = next(
            (
                item
                for item in page.items
                if "jukebox" in item.title.casefold().replace(" ", "")
            ),
            None,
        )
        if item is None:
            raise NuvoM3ProtocolError("Juke Box Mode is not available on this M3")
        return item

    def _start_menu_keepalive(self, output: str, menu_id: int) -> None:
        """Keep a successfully browsed menu alive for a bounded idle period."""
        self._cancel_menu_keepalive(output)
        self._menu_keepalive_tasks[output] = asyncio.create_task(
            self._menu_keepalive_loop(output, menu_id),
            name=f"nuvo_m3_menu_keepalive_{output}",
        )

    def _cancel_menu_keepalive(self, output: str) -> None:
        task = self._menu_keepalive_tasks[output]
        if task and task is not asyncio.current_task():
            task.cancel()
        self._menu_keepalive_tasks[output] = None

    async def _menu_keepalive_loop(self, output: str, menu_id: int) -> None:
        """Send MENUACTIVE while Home Assistant's recent browse is considered active."""
        loop = asyncio.get_running_loop()
        expires_at = loop.time() + self._menu_keepalive_timeout
        try:
            while True:
                remaining = expires_at - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(self._menu_keepalive_interval, remaining))
                if loop.time() >= expires_at:
                    break
                async with self._menu_locks[output]:
                    if self._menu_keepalive_tasks[output] is not asyncio.current_task():
                        return
                    await self.send(f"OUT'{output}'MENUACTIVE,{menu_id}")
                    self.diagnostics.menu_keepalives_sent += 1
            async with self._menu_locks[output]:
                if self._menu_keepalive_tasks[output] is asyncio.current_task():
                    await self.send(f"OUT'{output}'MENUEXIT")
        except asyncio.CancelledError:
            raise
        except (NuvoM3ConnectionError, OSError):
            return
        finally:
            if self._menu_keepalive_tasks[output] is asyncio.current_task():
                self._menu_keepalive_tasks[output] = None

    async def _reset_menu(self, output: str) -> None:
        """Exit any prior controller menu before beginning a deterministic path."""
        await self.send(f"OUT'{output}'MENUEXIT")
        # MENUEXIT responds only with #OK, but allow slower bridges to deliver
        # any pending menu-exit notification before a new request future exists.
        await asyncio.sleep(0.35)

    async def _request_menu(self, output: str, command: str) -> MenuPage:
        """Send one menu command and await its multi-packet response."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MenuPage] = loop.create_future()
        self._menu_futures[output] = future
        self._menu_pages[output] = None
        await self.send(command)
        try:
            return await asyncio.wait_for(future, 8)
        except TimeoutError as err:
            raise NuvoM3ProtocolError("Timed out waiting for an M3 menu response") from err
        finally:
            self._menu_futures[output] = None
            self._menu_pages[output] = None

    def parse_packet(self, packet: str) -> bool:
        """Parse a protocol response; return True when state changed."""
        # Raw serial bridges can prefix responses with NUL bytes; CRLF bridges
        # can also leave a leading LF after the CR-delimited read.
        packet = packet.lstrip("\x00\r\n ").rstrip("\x00\r\n ")
        self.diagnostics.packets_received += 1
        self.diagnostics.last_packet = packet[:500]
        self.diagnostics.last_packet_at = datetime.now(UTC)
        if packet == "#OK" or not packet:
            return False
        if packet == "#?":
            self.diagnostics.protocol_errors += 1
            self.diagnostics.last_error = "M3 rejected the last command"
            return True
        if packet.upper().startswith("#VER,"):
            values = tuple(packet[5:].split(","))
            if len(values) == 4:
                self.versions = values  # type: ignore[assignment]
                if hasattr(self, "_version_event"):
                    self._version_event.set()
                return True
            return False
        if packet.upper().startswith("#STATUS,"):
            self.server_status = packet.split(",", 1)[1].upper()
            self._status_event.set()
            return True
        match = _MENU_UNAVAILABLE.match(packet)
        if match:
            self._fail_menu(match.group(1).upper(), "M3 menu is unavailable or already in use")
            return True
        match = _MENU_EXIT.match(packet)
        if match:
            output = match.group(1).upper()
            self._cancel_menu_keepalive(output)
            self._fail_menu(output, "M3 menu session exited")
            return True
        match = _MENU.match(packet)
        if match:
            output = match.group(1).upper()
            try:
                values = next(csv.reader([match.group(2)], skipinitialspace=True))
                if len(values) != 6:
                    raise ValueError(f"expected 6 menu fields, got {len(values)}")
                page = MenuPage(
                    int(values[0]), values[1], int(values[2]), int(values[3]), int(values[4]), int(values[5])
                )
                self._menu_pages[output] = page
                if page.returned == 0:
                    self._finish_menu(output)
            except (ValueError, csv.Error) as err:
                self._fail_menu(output, f"Invalid menu packet: {err}")
            return True
        match = _MENU_ITEM.match(packet)
        if match:
            output = match.group(1).upper()
            page = self._menu_pages[output]
            if page is None:
                return False
            try:
                values = next(csv.reader([match.group(2)], skipinitialspace=True))
                if len(values) != 3:
                    raise ValueError(f"expected 3 menu-item fields, got {len(values)}")
                page.items.append(MenuItem(int(values[0]), values[1], int(values[2]), page.start + len(page.items)))
                if len(page.items) >= page.returned:
                    self._finish_menu(output)
            except (ValueError, csv.Error) as err:
                self._fail_menu(output, f"Invalid menu item: {err}")
            return True
        match = _LICENSE_ERROR.match(packet)
        if match:
            self.outputs[match.group(1).upper()].license_errors += 1
            return True
        match = _OUTPUT_STATUS.match(packet)
        if not match:
            return False
        try:
            fields = next(csv.reader([match.group(2)], skipinitialspace=True))
            if len(fields) != 10:
                raise ValueError(f"expected 10 fields, got {len(fields)}")
            state = self.outputs[match.group(1).upper()]
            state.play_status = int(fields[0])
            state.current_track = int(fields[1])
            state.total_tracks = int(fields[2])
            state.artist, state.album, state.title = fields[3:6]
            state.elapsed = int(fields[6]) / 10
            state.duration = int(fields[7]) / 10
            state.shuffle = fields[8] == "1"
            state.repeat = fields[9] == "1"
            state.updated_at = datetime.now(UTC)
        except (ValueError, csv.Error) as err:
            self.diagnostics.protocol_errors += 1
            self.diagnostics.last_error = f"Invalid packet: {err}"
            _LOGGER.warning("Invalid NV-M3 packet %r: %s", packet, err)
            return True
        return True

    def _finish_menu(self, output: str) -> None:
        future = self._menu_futures[output]
        page = self._menu_pages[output]
        if future and not future.done() and page:
            future.set_result(page)

    def _fail_menu(self, output: str, message: str) -> None:
        future = self._menu_futures[output]
        if future and not future.done():
            future.set_exception(NuvoM3ProtocolError(message))
        self.diagnostics.protocol_errors += 1
        self.diagnostics.last_error = message

    async def _read_loop(self) -> None:
        assert self._reader is not None
        missed_heartbeats = 0
        try:
            while not self._closing:
                try:
                    raw = await asyncio.wait_for(self._reader.readuntil(b"\r"), timeout=20)
                except TimeoutError:
                    missed_heartbeats += 1
                    if missed_heartbeats >= 2:
                        raise ConnectionError(
                            "NuVo M3 did not answer two status heartbeats"
                        )
                    _LOGGER.debug("No M3 data for 20 seconds; requesting status")
                    await self.send("STATUS?")
                    continue
                missed_heartbeats = 0
                packet = raw.decode("iso-8859-1", errors="replace")
                if self.parse_packet(packet):
                    for callback in tuple(self._callbacks):
                        callback()
        except asyncio.CancelledError:
            raise
        except (
            OSError,
            ConnectionError,
            NuvoM3ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as err:
            if not self._closing:
                self.diagnostics.last_error = str(err)
                _LOGGER.warning("NV-M3 serial connection lost: %s", err)
                self._connection_lost()

    async def _status_poll_loop(self) -> None:
        """Refresh server status even when unsolicited output traffic is active."""
        try:
            await asyncio.sleep(5)
            while not self._closing and self.diagnostics.connected:
                await self.send("STATUS?")
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except (NuvoM3ConnectionError, OSError):
            return

    def _connection_lost(self) -> None:
        if self._status_task and self._status_task is not asyncio.current_task():
            self._status_task.cancel()
        self._set_disconnected()
        if not self._closing and (not self._reconnect_task or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _set_disconnected(self) -> None:
        self.diagnostics.connected = False
        self._connected_event.clear()
        for output in OUTPUTS:
            self._cancel_menu_keepalive(output)
        for callback in tuple(self._callbacks):
            callback()

    async def _reconnect_loop(self) -> None:
        delay = 2
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self.connect()
                self.diagnostics.reconnects += 1
                await self.initialize()
                return
            except NuvoM3ConnectionError:
                delay = min(delay * 2, 60)
