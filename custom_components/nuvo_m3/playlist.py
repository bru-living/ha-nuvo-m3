"""Persistent Home Assistant-managed playlists for the NuVo M3."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, OUTPUTS
from .protocol import NuvoM3Client

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.playlists"


@dataclass(slots=True)
class PlaylistTrack:
    """A stable M3 menu path captured from the media browser."""

    title: str
    path: list[list[int]]


class PlaylistManager:
    """Store playlists and sequence their tracks through an M3 output."""

    def __init__(self, hass: HomeAssistant, client: NuvoM3Client) -> None:
        self.hass = hass
        self.client = client
        self.playlists: dict[str, list[PlaylistTrack]] = {}
        self.last_selection: dict[str, PlaylistTrack] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load(self) -> None:
        """Load playlists from Home Assistant storage."""
        stored = await self._store.async_load() or {}
        self.playlists = {
            name: [PlaylistTrack(str(item["title"]), item["path"]) for item in items]
            for name, items in stored.get("playlists", {}).items()
        }

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "playlists": {
                    name: [asdict(track) for track in tracks]
                    for name, tracks in self.playlists.items()
                }
            }
        )

    async def async_create(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Playlist name cannot be empty")
        self.playlists.setdefault(name, [])
        await self.async_save()

    async def async_delete(self, name: str) -> None:
        self.playlists.pop(name, None)
        await self.async_save()

    def remember_selection(
        self, output: str, title: str, path: list[tuple[int, int, int]]
    ) -> None:
        self.last_selection[output] = PlaylistTrack(
            title, [list(part) for part in path]
        )

    async def async_add_current(self, name: str, output: str) -> None:
        if name not in self.playlists:
            raise ValueError(f"Playlist {name!r} does not exist")
        if output not in self.last_selection:
            raise ValueError(
                f"No browser-selected track has been played on Output {output}"
            )
        self.playlists[name].append(self.last_selection[output])
        await self.async_save()

    async def async_remove_track(self, name: str, index: int) -> None:
        tracks = self.playlists.get(name)
        if tracks is None:
            raise ValueError(f"Playlist {name!r} does not exist")
        if index < 1 or index > len(tracks):
            raise ValueError("Track number is outside the playlist")
        tracks.pop(index - 1)
        await self.async_save()

    def play(self, name: str, output: str) -> None:
        """Start a persistent playlist without blocking a service call."""
        if output not in OUTPUTS:
            raise ValueError("Output must be A, B or C")
        tracks = self.playlists.get(name)
        if tracks is None:
            raise ValueError(f"Playlist {name!r} does not exist")
        if not tracks:
            raise ValueError(f"Playlist {name!r} is empty")
        self.stop(output)
        self._tasks[output] = asyncio.create_task(
            self._run_playlist(output, list(tracks)),
            name=f"nuvo_m3_playlist_{output.lower()}",
        )

    def stop(self, output: str) -> None:
        if task := self._tasks.pop(output, None):
            task.cancel()

    async def async_stop_all(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _run_playlist(
        self, output: str, tracks: list[PlaylistTrack]
    ) -> None:
        """Play stored single-track menu selections one after another."""
        try:
            for track in tracks:
                path = [tuple(part) for part in track.path]
                if not path:
                    continue
                await self.client.play_menu_item(output, path[:-1], path[-1])
                await self._wait_for_track(output)
        finally:
            self._tasks.pop(output, None)

    async def _wait_for_track(self, output: str) -> None:
        """Wait for playing, then for the single-track list to finish."""
        for _ in range(20):
            if self.client.outputs[output].play_status in (2, 4, 5, 6, 7, 8):
                break
            await asyncio.sleep(0.5)
        while True:
            state = self.client.outputs[output]
            if state.play_status == 1:
                return
            if state.play_status == 3:
                await asyncio.sleep(1)
                continue
            remaining = max(1.0, state.duration - state.elapsed)
            await asyncio.sleep(min(remaining + 0.5, 10))
            await self.client.send(f"OUT'{output}'STATUS?")
