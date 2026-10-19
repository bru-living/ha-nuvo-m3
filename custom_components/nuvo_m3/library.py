"""Persistent, locally browsable NV-M3 music-library index."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .protocol import MenuItem, MenuPage, NuvoM3Client, NuvoM3ProtocolError

_LOGGER = logging.getLogger(__name__)
_STORE_VERSION = 1
_PAGE_SIZE = 20
_SCAN_MENU_TIMEOUT = 30
_SCAN_MENU_ATTEMPTS = 3


@dataclass(slots=True)
class LibraryStatus:
    """Live scan status exposed to Home Assistant."""

    state: str = "not_indexed"
    phase: str = "idle"
    progress: int = 0
    artists: int = 0
    albums: int = 0
    tracks: int = 0
    global_tracks: int = 0
    skipped_artists: int = 0
    skipped_albums: int = 0
    last_indexed: str | None = None
    error: str | None = None


class LibraryManager:
    """Scan the M3 menu tree and persist a fast local catalogue."""

    def __init__(self, hass: HomeAssistant, client: NuvoM3Client, entry_id: str) -> None:
        self.hass = hass
        self.client = client
        self._store = Store(hass, _STORE_VERSION, f"nuvo_m3.library.{entry_id}")
        self.catalogue: dict[str, Any] = {"artists": []}
        self.status = LibraryStatus()
        self._task: asyncio.Task[None] | None = None

    async def async_load(self) -> None:
        """Load a previous successful index."""
        data = await self._store.async_load()
        if not isinstance(data, dict) or not isinstance(data.get("artists"), list):
            return
        self.catalogue = data
        self.status.state = "ready"
        self.status.last_indexed = data.get("indexed_at")
        self._update_counts()

    def start(self, output: str = "A") -> None:
        """Start a scan without blocking an HA service call."""
        if self._task and not self._task.done():
            raise ValueError("A library scan is already running")
        self._task = self.hass.async_create_task(
            self._async_scan(output), "NuVo M3 library scan"
        )

    def cancel(self) -> None:
        """Cancel the active scan, retaining the last complete index."""
        if self._task and not self._task.done():
            self._task.cancel()

    async def async_stop(self) -> None:
        """Stop background work during unload."""
        self.cancel()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _async_scan(self, output: str) -> None:
        previous_state = "ready" if self.catalogue.get("artists") else "not_indexed"
        self.status = LibraryStatus(state="scanning", phase="discovering", progress=0)
        self.client.notify_update()
        try:
            root = await self._browse_with_retry(output, [])
            artists_item = next(
                (item for item in root.items if item.title.casefold() == "artists"), None
            )
            if artists_item is None:
                raise ValueError("The M3 Main Menu does not contain Artists")
            tracks_item = next(
                (item for item in root.items if item.title.casefold() == "tracks"), None
            )
            if tracks_item is None:
                raise ValueError("The M3 Main Menu does not contain Tracks")
            artists_path = [self._path_part(root, artists_item)]
            artist_rows = await self._all_items(output, artists_path)
            artist_rows = [item for item in artist_rows if item.can_expand]
            new_artists: list[dict[str, Any]] = []
            total_artists = max(1, len(artist_rows))
            self.status.phase = "cataloguing"

            for artist_number, artist_item in enumerate(artist_rows, 1):
                artist_path = [*artists_path, self._path_part_for_parent(artist_item)]
                try:
                    album_rows = await self._all_items(output, artist_path)
                except NuvoM3ProtocolError as err:
                    self.status.skipped_artists += 1
                    _LOGGER.warning(
                        "Skipping M3 artist %r because its album menu failed: %s",
                        artist_item.title,
                        err,
                    )
                    self._update_scan_progress(
                        artist_number, total_artists, new_artists
                    )
                    continue
                albums: list[dict[str, Any]] = []
                for album_item in album_rows:
                    if not album_item.can_expand:
                        continue
                    album_path = [*artist_path, self._path_part_for_parent(album_item)]
                    try:
                        track_rows = await self._all_items(output, album_path)
                    except NuvoM3ProtocolError as err:
                        self.status.skipped_albums += 1
                        _LOGGER.warning(
                            "Skipping M3 album %r by %r because its track menu failed: %s",
                            album_item.title,
                            artist_item.title,
                            err,
                        )
                        continue
                    tracks = [
                        {
                            "title": track.title,
                            "path": [*album_path, list(self._path_part_for_parent(track))],
                        }
                        for track in track_rows
                        if not track.disabled
                    ]
                    if tracks:
                        albums.append(
                            {
                                "title": album_item.title,
                                "path": [list(part) for part in album_path],
                                "artwork": None,
                                "tracks": tracks,
                            }
                        )
                if albums:
                    new_artists.append({"name": artist_item.title, "albums": albums})
                self._update_scan_progress(artist_number, total_artists, new_artists)

            self.status.phase = "global_tracks"
            tracks_path = [self._path_part(root, tracks_item)]
            global_track_rows = await self._all_items(output, tracks_path)
            global_tracks: list[dict[str, Any]] = []
            for track in global_track_rows:
                if track.disabled:
                    continue
                global_tracks.append(
                    {
                        "title": track.title,
                        "path": [
                            list(tracks_path[0]),
                            list(self._path_part_for_parent(track)),
                        ],
                        "artist": None,
                        "album": None,
                        "artwork": None,
                    }
                )
            self.status.global_tracks = len(global_tracks)
            self.client.notify_update()

            if self.client.artwork_manager is not None:
                self.status.phase = "artwork"
                albums = [album for artist in new_artists for album in artist["albums"]]
                for number, album in enumerate(albums, 1):
                    artist = next(
                        item["name"] for item in new_artists if album in item["albums"]
                    )
                    first_track = album["tracks"][0]["title"]
                    album["artwork"] = await self.client.artwork_manager.async_get_artwork(
                        artist, first_track, album["title"]
                    )
                    self.status.progress = 90 + round(number / max(1, len(albums)) * 10)
                    self.client.notify_update()

            # Artwork is resolved after the menu scan, so associate metadata only
            # once the album records contain their final artwork URLs.
            metadata = self._track_metadata(new_artists)
            for track in global_tracks:
                candidates = metadata.get(self._normalise_title(track["title"]), [])
                if len(candidates) == 1:
                    track.update(candidates[0])

            indexed_at = datetime.now(UTC).isoformat()
            catalogue = {
                "indexed_at": indexed_at,
                "artists": new_artists,
                "global_tracks": global_tracks,
                "skipped_artists": self.status.skipped_artists,
                "skipped_albums": self.status.skipped_albums,
            }
            await self._store.async_save(catalogue)
            self.catalogue = catalogue
            self.status.state = "ready"
            self.status.phase = "complete"
            self.status.progress = 100
            self.status.last_indexed = indexed_at
            skipped = self.status.skipped_artists + self.status.skipped_albums
            self.status.error = (
                f"Scan completed with {skipped} inaccessible menu item(s) skipped"
                if skipped
                else None
            )
            self._update_counts()
        except asyncio.CancelledError:
            self.status.state = previous_state
            self.status.phase = "cancelled"
            self.status.progress = 0
            raise
        except Exception as err:  # preserve the previous known-good index
            _LOGGER.exception("NV-M3 library scan failed")
            self.status.state = previous_state
            self.status.phase = "failed"
            self.status.progress = 0
            self.status.error = str(err)
        finally:
            self.client.notify_update()

    def _update_scan_progress(
        self,
        artist_number: int,
        total_artists: int,
        artists: list[dict[str, Any]],
    ) -> None:
        """Publish monotonic catalogue progress and current totals."""
        self.status.progress = max(
            self.status.progress,
            min(90, round(artist_number / total_artists * 90)),
        )
        self.status.artists = len(artists)
        self.status.albums = sum(len(item["albums"]) for item in artists)
        self.status.tracks = sum(
            len(album["tracks"])
            for item in artists
            for album in item["albums"]
        )
        self.client.notify_update()

    async def _all_items(
        self, output: str, path: list[tuple[int, int, int] | list[int]]
    ) -> list[MenuItem]:
        """Read every page for one menu path."""
        normalised = [tuple(int(value) for value in part) for part in path]
        first = await self._browse_with_retry(output, normalised, 0)
        items = list(first.items)
        start = first.start + first.returned
        while start < first.total:
            page = await self._browse_with_retry(output, normalised, start)
            items.extend(page.items)
            if page.returned <= 0:
                break
            start = page.start + page.returned
        return items

    async def _browse_with_retry(
        self,
        output: str,
        path: list[tuple[int, int, int]],
        start: int = 0,
    ) -> MenuPage:
        """Allow slow M3 library generation and retry transient timeouts."""
        for attempt in range(1, _SCAN_MENU_ATTEMPTS + 1):
            try:
                return await self.client.browse_menu(
                    output,
                    path,
                    start,
                    timeout=_SCAN_MENU_TIMEOUT,
                )
            except NuvoM3ProtocolError as err:
                if "Timed out" not in str(err) or attempt == _SCAN_MENU_ATTEMPTS:
                    raise
                _LOGGER.warning(
                    "M3 library page timed out (attempt %s/%s, offset %s); retrying",
                    attempt,
                    _SCAN_MENU_ATTEMPTS,
                    start,
                )
                await asyncio.sleep(attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _path_part(page: MenuPage, item: MenuItem) -> tuple[int, int, int]:
        return page.menu_id, item.item_id, item.index

    @staticmethod
    def _path_part_for_parent(item: MenuItem) -> tuple[int, int, int]:
        # The parent menu id is filled in from the path returned by the M3.  Item
        # IDs are globally useful, but MENUSELECT also requires its menu id.
        return item.menu_id, item.item_id, item.index

    def _update_counts(self) -> None:
        artists = self.catalogue.get("artists", [])
        self.status.artists = len(artists)
        self.status.albums = sum(len(item.get("albums", [])) for item in artists)
        self.status.tracks = sum(
            len(album.get("tracks", []))
            for item in artists
            for album in item.get("albums", [])
        )
        self.status.skipped_artists = int(self.catalogue.get("skipped_artists", 0))
        self.status.skipped_albums = int(self.catalogue.get("skipped_albums", 0))
        self.status.global_tracks = len(self.catalogue.get("global_tracks", []))

    @staticmethod
    def _normalise_title(title: str) -> str:
        """Normalise a title for conservative metadata association."""
        return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))

    @classmethod
    def _track_metadata(
        cls, artists: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Build title matches; duplicate titles deliberately remain ambiguous."""
        result: dict[str, list[dict[str, Any]]] = {}
        for artist in artists:
            for album in artist.get("albums", []):
                for track in album.get("tracks", []):
                    result.setdefault(cls._normalise_title(track["title"]), []).append(
                        {
                            "artist": artist["name"],
                            "album": album["title"],
                            "artwork": album.get("artwork"),
                        }
                    )
        return result

    @property
    def available(self) -> bool:
        return bool(self.catalogue.get("artists"))
