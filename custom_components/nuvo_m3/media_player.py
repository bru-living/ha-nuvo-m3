"""Media player entities for the three M3 outputs."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import json
from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaType,
    RepeatMode,
)
from homeassistant.components.media_player.const import MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NuvoM3ConfigEntry
from .const import OUTPUTS
from .entity import NuvoM3Entity

HA_PLAYLISTS_ID = "nuvo-m3-ha-playlists"
HA_PLAYLIST_PREFIX = "nuvo-m3-ha-playlist:"
INDEX_PREFIX = "nuvo-m3-index:"
LIVE_LIBRARY_ID = "nuvo-m3-live-library"
INDEX_TRACK_PAGE_SIZE = 100

FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.PLAY_MEDIA
)


def _encode_path(
    output: str, path: list[tuple[int, int, int]], start_index: int = 0
) -> str:
    """Create an opaque, URL-safe media identifier."""
    payload = json.dumps(
        {"output": output, "path": path, "start": start_index},
        separators=(",", ":"),
    ).encode()
    return "nuvo-m3:" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_path(
    media_id: str, expected_output: str
) -> tuple[list[tuple[int, int, int]], int]:
    """Decode and validate a media identifier belonging to this output."""
    if not media_id.startswith("nuvo-m3:"):
        raise ValueError("Not a NuVo M3 media identifier")
    encoded = media_id.removeprefix("nuvo-m3:")
    encoded += "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded))
    if isinstance(payload, list):
        output, raw_path = payload
        start_index = 0
    else:
        output = payload.get("output")
        raw_path = payload.get("path")
        start_index = int(payload.get("start", 0))
    if output != expected_output or not isinstance(raw_path, list):
        raise ValueError("Media identifier belongs to another output")
    path = [tuple(int(value) for value in item) for item in raw_path]
    if any(len(item) != 3 for item in path):
        raise ValueError("Invalid NuVo M3 menu path")
    if start_index < 0:
        raise ValueError("Invalid NuVo M3 page offset")
    return path, start_index


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NuvoM3ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(NuvoM3MediaPlayer(entry, output) for output in OUTPUTS)


class NuvoM3MediaPlayer(NuvoM3Entity, MediaPlayerEntity):
    """One physical NV-M3 output."""

    _attr_media_content_type = MediaType.MUSIC
    _attr_media_image_remotely_accessible = True
    _attr_supported_features = FEATURES

    def __init__(self, entry: NuvoM3ConfigEntry, output: str) -> None:
        super().__init__(entry)
        self.output = output
        self._attr_name = f"Output {output}"
        self._attr_unique_id = f"{entry.entry_id}_output_{output.lower()}"
        self._browse_titles: dict[str, str] = {}
        self._global_track_index: int | None = None
        self._artwork_key: tuple[str, str, str] | None = None
        self._artwork_task: asyncio.Task[None] | None = None
        self._artwork_status = (
            "idle" if self.client.artwork_manager is not None else "disabled"
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_artwork_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._artwork_task is not None:
            self._artwork_task.cancel()
        await super().async_will_remove_from_hass()

    def _handle_update(self) -> None:
        self._schedule_artwork_update()
        super()._handle_update()

    def _schedule_artwork_update(self) -> None:
        manager = self.client.artwork_manager
        if manager is None:
            return
        state = self.client.outputs[self.output]
        key = (state.artist.strip(), state.title.strip(), state.album.strip())
        if key == self._artwork_key:
            return
        self._artwork_key = key
        self._attr_media_image_url = None
        self._attr_entity_picture = None
        self._artwork_status = "searching"
        if self._artwork_task is not None:
            self._artwork_task.cancel()
        if not key[0] or not key[1]:
            self._artwork_task = None
            self._artwork_status = "waiting_for_metadata"
            return
        self._artwork_task = self.hass.async_create_task(
            self._async_update_artwork(key), f"NuVo M3 artwork {self.output}"
        )

    async def _async_update_artwork(self, key: tuple[str, str, str]) -> None:
        try:
            image_url = await self.client.artwork_manager.async_get_artwork(*key)
        except asyncio.CancelledError:
            return
        if key != self._artwork_key:
            return
        self._attr_media_image_url = image_url
        # NuvoM3Entity precedes MediaPlayerEntity in the MRO, so Entity's
        # entity_picture property wins. Set it explicitly for dashboard cards.
        self._attr_entity_picture = image_url
        self._artwork_status = "found" if image_url else "not_found"
        self.async_write_ha_state()

    @property
    def state(self) -> MediaPlayerState:
        status = self.client.outputs[self.output].play_status
        if status in (2, 4, 5, 6, 7, 8):
            return MediaPlayerState.PLAYING
        if status == 3:
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def media_title(self) -> str | None:
        return self.client.outputs[self.output].title or None

    @property
    def media_artist(self) -> str | None:
        return self.client.outputs[self.output].artist or None

    @property
    def media_album_name(self) -> str | None:
        return self.client.outputs[self.output].album or None

    @property
    def media_duration(self) -> int | None:
        duration = self.client.outputs[self.output].duration
        return round(duration) if duration else None

    @property
    def media_position(self) -> int | None:
        state = self.client.outputs[self.output]
        if state.updated_at is None:
            return None
        return round(state.elapsed)

    @property
    def media_position_updated_at(self) -> datetime | None:
        return self.client.outputs[self.output].updated_at

    @property
    def shuffle(self) -> bool:
        return self.client.outputs[self.output].shuffle

    @property
    def repeat(self) -> RepeatMode:
        return RepeatMode.ALL if self.client.outputs[self.output].repeat else RepeatMode.OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.client.outputs[self.output]
        return {
            "current_track": state.current_track,
            "total_tracks": state.total_tracks,
            "artwork_status": self._artwork_status,
        }

    async def async_media_play(self) -> None:
        await self.client.play(self.output)

    async def async_media_pause(self) -> None:
        await self.client.pause(self.output)

    async def async_media_next_track(self) -> None:
        if await self._async_step_global_track(1):
            return
        await self.client.next_track(self.output)

    async def async_media_previous_track(self) -> None:
        if await self._async_step_global_track(-1):
            return
        await self.client.previous_track(self.output)

    async def _async_step_global_track(self, direction: int) -> bool:
        """Move within the indexed global Tracks list when it started playback."""
        if self._global_track_index is None:
            return False
        tracks = self.client.library_manager.catalogue.get("global_tracks", [])
        if not tracks:
            self._global_track_index = None
            return False
        index = (self._global_track_index + direction) % len(tracks)
        track = tracks[index]
        path = [tuple(part) for part in track["path"]]
        self._global_track_index = index
        self.client.playlist_manager.remember_selection(
            self.output, track["title"], path
        )
        await self.client.play_menu_item(self.output, path[:-1], path[-1])
        return True

    async def async_media_seek(self, position: float) -> None:
        state = self.client.outputs[self.output]
        current = state.elapsed
        if state.updated_at is not None and self.state == MediaPlayerState.PLAYING:
            current += (datetime.now(UTC) - state.updated_at).total_seconds()
        if state.duration:
            current = min(current, state.duration)
        await self.client.skip(self.output, position - current)

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self.client.set_shuffle(self.output, shuffle)

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        await self.client.set_repeat(self.output, repeat != RepeatMode.OFF)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the M3's dynamic music-library menu."""
        if media_content_id is None and self.client.library_manager.available:
            return self._browse_index_root()
        if media_content_id and media_content_id.startswith(INDEX_PREFIX):
            return self._browse_index(media_content_id)
        if media_content_id == LIVE_LIBRARY_ID:
            media_content_id = None
        if media_content_id == HA_PLAYLISTS_ID:
            return self._browse_ha_playlists()
        if media_content_id and media_content_id.startswith(HA_PLAYLIST_PREFIX):
            return self._browse_ha_playlist(media_content_id)
        if media_content_id is None:
            path, start_index = [], 0
        else:
            path, start_index = _decode_path(media_content_id, self.output)
        page = await self.client.browse_menu(self.output, path, start_index)
        children: list[BrowseMedia] = []
        if page.start > 0:
            previous_start = max(0, page.start - 20)
            children.append(
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=_encode_path(self.output, path, previous_start),
                    media_content_type=MediaType.MUSIC,
                    title=f"Previous ({previous_start + 1}-{page.start})",
                    can_play=False,
                    can_expand=True,
                )
            )
        for item in page.items:
            if item.disabled:
                continue
            item_path = [*path, (page.menu_id, item.item_id, item.index)]
            item_media_id = _encode_path(self.output, item_path)
            self._browse_titles[item_media_id] = item.title
            children.append(
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY if item.can_expand else MediaClass.MUSIC,
                    media_content_id=item_media_id,
                    media_content_type=MediaType.MUSIC,
                    title=item.title,
                    can_play=item.can_play,
                    can_expand=item.can_expand,
                )
            )
        if not path and page.start == 0:
            children.insert(
                0,
                BrowseMedia(
                    media_class=MediaClass.PLAYLIST,
                    media_content_id=HA_PLAYLISTS_ID,
                    media_content_type=MediaType.MUSIC,
                    title="Home Assistant Playlists",
                    can_play=False,
                    can_expand=True,
                ),
            )
            if self.client.library_manager.available:
                children.insert(
                    1,
                    BrowseMedia(
                        media_class=MediaClass.DIRECTORY,
                        media_content_id=LIVE_LIBRARY_ID,
                        media_content_type=MediaType.MUSIC,
                        title="Live M3 menus",
                        can_play=False,
                        can_expand=True,
                    ),
                )
        next_start = page.start + page.returned
        if next_start < page.total:
            children.append(
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=_encode_path(self.output, path, next_start),
                    media_content_type=MediaType.MUSIC,
                    title=f"Next ({next_start + 1}-{min(next_start + 20, page.total)} of {page.total})",
                    can_play=False,
                    can_expand=True,
                )
            )
        page_title = page.title
        if page.total > page.returned:
            page_title = (
                f"{page.title} ({page.start + 1}-{page.start + page.returned} "
                f"of {page.total})"
            )
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=_encode_path(self.output, path, page.start),
            media_content_type=MediaType.MUSIC,
            title=page_title,
            can_play=False,
            can_expand=True,
            children=children,
        )

    @staticmethod
    def _index_id(kind: str, *indexes: int) -> str:
        return INDEX_PREFIX + kind + ":" + ":".join(str(index) for index in indexes)

    def _browse_index_root(self) -> BrowseMedia:
        """Return the persistent catalogue without contacting the M3."""
        artists = self.client.library_manager.catalogue.get("artists", [])
        artist_thumbnails = [
            album.get("artwork")
            for artist in artists
            for album in artist.get("albums", [])
            if album.get("artwork")
        ]
        children = [
            BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id=self._index_id("artists"),
                media_content_type=MediaType.MUSIC,
                title=f"Artists ({len(artists)})",
                can_play=False,
                can_expand=True,
                thumbnail=artist_thumbnails[0] if artist_thumbnails else None,
            ),
            BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id=self._index_id("tracks", 0),
                media_content_type=MediaType.MUSIC,
                title=(
                    f"Tracks ({self.client.library_manager.status.global_tracks})"
                    if "global_tracks" in self.client.library_manager.catalogue
                    else "Tracks (rescan library required)"
                ),
                can_play=False,
                can_expand=True,
                thumbnail=artist_thumbnails[0] if artist_thumbnails else None,
            ),
        ]
        children.insert(
            0,
            BrowseMedia(
                media_class=MediaClass.PLAYLIST,
                media_content_id=HA_PLAYLISTS_ID,
                media_content_type=MediaType.MUSIC,
                title="Home Assistant Playlists",
                can_play=False,
                can_expand=True,
            ),
        )
        children.append(
            BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id=LIVE_LIBRARY_ID,
                media_content_type=MediaType.MUSIC,
                title="Live M3 menus",
                can_play=False,
                can_expand=True,
            )
        )
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=self._index_id("root"),
            media_content_type=MediaType.MUSIC,
            title="Indexed M3 Library",
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_index(self, media_id: str) -> BrowseMedia:
        """Browse artists, albums and tracks from the local catalogue."""
        parts = media_id.removeprefix(INDEX_PREFIX).split(":")
        kind = parts[0]
        indexes = [int(value) for value in parts[1:] if value]
        artists = self.client.library_manager.catalogue.get("artists", [])
        if kind == "root":
            return self._browse_index_root()
        if kind == "artists":
            children = [
                BrowseMedia(
                    media_class=MediaClass.ARTIST,
                    media_content_id=self._index_id("artist", artist_index),
                    media_content_type=MediaType.MUSIC,
                    title=artist["name"],
                    can_play=False,
                    can_expand=True,
                    thumbnail=next(
                        (
                            album.get("artwork")
                            for album in artist.get("albums", [])
                            if album.get("artwork")
                        ),
                        None,
                    ),
                )
                for artist_index, artist in enumerate(artists)
            ]
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id=media_id,
                media_content_type=MediaType.MUSIC,
                title=f"Artists ({len(children)})",
                can_play=False,
                can_expand=True,
                children=children,
            )
        if kind == "tracks" and len(indexes) == 1:
            offset = indexes[0]
            global_tracks = self.client.library_manager.catalogue.get("global_tracks", [])
            indexed_tracks = sorted(
                enumerate(global_tracks), key=lambda item: item[1]["title"].casefold()
            )
            page = indexed_tracks[offset : offset + INDEX_TRACK_PAGE_SIZE]
            children = []
            if offset == 0 and global_tracks:
                children.append(
                    BrowseMedia(
                        media_class=MediaClass.PLAYLIST,
                        media_content_id=self._index_id("play_all_tracks"),
                        media_content_type=MediaType.MUSIC,
                        title="Play all tracks",
                        can_play=True,
                        can_expand=False,
                        thumbnail=next(
                            (
                                track.get("artwork")
                                for track in global_tracks
                                if track.get("artwork")
                            ),
                            None,
                        ),
                    )
                )
            for track_index, track in page:
                artist = track.get("artist")
                children.append(
                    BrowseMedia(
                        media_class=MediaClass.TRACK,
                        media_content_id=self._index_id("global_track", track_index),
                        media_content_type=MediaType.MUSIC,
                        title=(
                            f'{track["title"]} — {artist}'
                            if artist
                            else track["title"]
                        ),
                        can_play=True,
                        can_expand=False,
                        thumbnail=track.get("artwork"),
                    )
                )
            next_offset = offset + len(page)
            if next_offset < len(indexed_tracks):
                children.append(
                    BrowseMedia(
                        media_class=MediaClass.DIRECTORY,
                        media_content_id=self._index_id("tracks", next_offset),
                        media_content_type=MediaType.MUSIC,
                        title=(
                            f"Next ({next_offset + 1}-"
                            f"{min(next_offset + INDEX_TRACK_PAGE_SIZE, len(indexed_tracks))}"
                            f" of {len(indexed_tracks)})"
                        ),
                        can_play=False,
                        can_expand=True,
                    )
                )
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id=media_id,
                media_content_type=MediaType.MUSIC,
                title=(
                    f"Tracks ({offset + 1}-"
                    f"{min(offset + len(page), len(indexed_tracks))}"
                    f" of {len(indexed_tracks)})"
                ),
                can_play=False,
                can_expand=True,
                children=children,
            )
        if kind == "artist" and len(indexes) == 1:
            artist_index = indexes[0]
            artist = artists[artist_index]
            children = [
                BrowseMedia(
                    media_class=MediaClass.ALBUM,
                    media_content_id=self._index_id(
                        "album", artist_index, album_index
                    ),
                    media_content_type=MediaType.MUSIC,
                    title=album["title"],
                    can_play=True,
                    can_expand=True,
                    thumbnail=album.get("artwork"),
                )
                for album_index, album in enumerate(artist.get("albums", []))
            ]
            return BrowseMedia(
                media_class=MediaClass.ARTIST,
                media_content_id=media_id,
                media_content_type=MediaType.MUSIC,
                title=artist["name"],
                can_play=False,
                can_expand=True,
                children=children,
            )
        if kind == "album" and len(indexes) == 2:
            artist = artists[indexes[0]]
            album = artist["albums"][indexes[1]]
            children = [
                BrowseMedia(
                    media_class=MediaClass.PLAYLIST,
                    media_content_id=self._index_id(
                        "play_all_album", indexes[0], indexes[1]
                    ),
                    media_content_type=MediaType.MUSIC,
                    title="Play all album tracks",
                    can_play=True,
                    can_expand=False,
                    thumbnail=album.get("artwork"),
                )
            ]
            for track_index, track in enumerate(album.get("tracks", [])):
                track_id = self._index_id(
                    "track", indexes[0], indexes[1], track_index
                )
                children.append(
                    BrowseMedia(
                        media_class=MediaClass.TRACK,
                        media_content_id=track_id,
                        media_content_type=MediaType.MUSIC,
                        title=track["title"],
                        can_play=True,
                        can_expand=False,
                        thumbnail=album.get("artwork"),
                    )
                )
            return BrowseMedia(
                media_class=MediaClass.ALBUM,
                media_content_id=media_id,
                media_content_type=MediaType.MUSIC,
                title=album["title"],
                can_play=bool(children),
                can_expand=True,
                thumbnail=album.get("artwork"),
                children=children,
            )
        raise ValueError("Invalid indexed-library media identifier")

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a library item selected in Home Assistant's media browser."""
        if media_id.startswith(HA_PLAYLIST_PREFIX):
            self._global_track_index = None
            name = self._decode_playlist_name(media_id)
            self.client.playlist_manager.play(name, self.output)
            return
        if media_id.startswith(INDEX_PREFIX):
            parts = media_id.removeprefix(INDEX_PREFIX).split(":")
            kind = parts[0]
            indexes = [int(value) for value in parts[1:] if value]
            artists = self.client.library_manager.catalogue.get("artists", [])
            if kind == "play_all_tracks" and not indexes:
                self._global_track_index = None
                await self.client.play_all(self.output)
                return
            if kind in ("album", "play_all_album") and len(indexes) == 2:
                self._global_track_index = None
                album = artists[indexes[0]]["albums"][indexes[1]]
                path = [tuple(part) for part in album["path"]]
                title = album["title"]
            elif kind == "track" and len(indexes) == 3:
                self._global_track_index = None
                track = artists[indexes[0]]["albums"][indexes[1]]["tracks"][indexes[2]]
                path = [tuple(part) for part in track["path"]]
                title = track["title"]
            elif kind == "global_track" and len(indexes) == 1:
                self._global_track_index = indexes[0]
                track = self.client.library_manager.catalogue["global_tracks"][indexes[0]]
                path = [tuple(part) for part in track["path"]]
                title = track["title"]
            else:
                raise ValueError("This indexed library item cannot be played")
            self.client.playlist_manager.remember_selection(self.output, title, path)
            await self.client.play_menu_item(self.output, path[:-1], path[-1])
            return
        path, _ = _decode_path(media_id, self.output)
        self._global_track_index = None
        if not path:
            raise ValueError("The M3 root menu cannot be played")
        title = self._browse_titles.get(media_id, "M3 track")
        self.client.playlist_manager.remember_selection(self.output, title, path)
        await self.client.play_menu_item(self.output, path[:-1], path[-1])

    def _browse_ha_playlists(self) -> BrowseMedia:
        """Return persistent Home Assistant playlist names."""
        children = [
            BrowseMedia(
                media_class=MediaClass.PLAYLIST,
                media_content_id=self._encode_playlist_name(name),
                media_content_type=MediaType.MUSIC,
                title=f"{name} ({len(tracks)} tracks)",
                can_play=bool(tracks),
                can_expand=True,
            )
            for name, tracks in sorted(self.client.playlist_manager.playlists.items())
        ]
        return BrowseMedia(
            media_class=MediaClass.PLAYLIST,
            media_content_id=HA_PLAYLISTS_ID,
            media_content_type=MediaType.MUSIC,
            title="Home Assistant Playlists",
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_ha_playlist(self, media_id: str) -> BrowseMedia:
        """Show the stored tracks in one Home Assistant playlist."""
        name = self._decode_playlist_name(media_id)
        tracks = self.client.playlist_manager.playlists.get(name, [])
        children: list[BrowseMedia] = []
        for track in tracks:
            path = [tuple(part) for part in track.path]
            track_id = _encode_path(self.output, path)
            self._browse_titles[track_id] = track.title
            children.append(
                BrowseMedia(
                    media_class=MediaClass.MUSIC,
                    media_content_id=track_id,
                    media_content_type=MediaType.MUSIC,
                    title=track.title,
                    can_play=True,
                    can_expand=False,
                )
            )
        return BrowseMedia(
            media_class=MediaClass.PLAYLIST,
            media_content_id=media_id,
            media_content_type=MediaType.MUSIC,
            title=name,
            can_play=bool(tracks),
            can_expand=True,
            children=children,
        )

    @staticmethod
    def _encode_playlist_name(name: str) -> str:
        return HA_PLAYLIST_PREFIX + base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_playlist_name(media_id: str) -> str:
        encoded = media_id.removeprefix(HA_PLAYLIST_PREFIX)
        encoded += "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded).decode()
