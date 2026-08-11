"""Optional MusicBrainz and Cover Art Archive artwork enrichment."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
_USER_AGENT = "ha-nuvo-m3/0.8.1 (https://github.com/bru-living/ha-nuvo-m3)"


def _normalise(value: str) -> str:
    """Normalise common punctuation and edition suffixes for title comparison."""
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\([^)]*(?:remaster|edition|version|deluxe)[^)]*\)", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


class ArtworkManager:
    """Resolve and cache cover-art URLs without delaying playback."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._session = async_get_clientsession(hass)
        self._cache: dict[tuple[str, str, str], str | None] = {}
        self._inflight: dict[tuple[str, str, str], asyncio.Task[str | None]] = {}
        self._rate_lock = asyncio.Lock()
        self._last_musicbrainz_request = 0.0

    async def async_get_artwork(
        self, artist: str, title: str, album: str = ""
    ) -> str | None:
        """Return a cached 500 px cover URL for the best matching release."""
        key = (artist.casefold().strip(), title.casefold().strip(), album.casefold().strip())
        if key in self._cache:
            return self._cache[key]
        if task := self._inflight.get(key):
            return await asyncio.shield(task)
        task = self._inflight[key] = asyncio.create_task(self._lookup(key))
        try:
            result = await asyncio.shield(task)
            self._cache[key] = result
            return result
        finally:
            self._inflight.pop(key, None)

    async def _lookup(self, key: tuple[str, str, str]) -> str | None:
        artist, title, album = key
        if not artist or not title:
            return None
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        escaped_artist = artist.replace("\\", "\\\\").replace('"', '\\"')
        base_query = (
            f'recording:"{escaped_title}" AND artist:"{escaped_artist}" '
            "AND status:official AND primarytype:album "
            "AND NOT secondarytype:compilation AND NOT secondarytype:live "
            'AND NOT secondarytype:remix AND NOT secondarytype:"dj-mix"'
        )
        try:
            releases = await self._search_releases(base_query)
            for release_id in self._release_candidates(releases, artist, album, title):
                async with self._session.get(
                    f"https://coverartarchive.org/release/{release_id}/",
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                    timeout=ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        continue
                    cover: dict[str, Any] = await response.json()
                images = cover.get("images", [])
                front = next((image for image in images if image.get("front")), None)
                if front is None and images:
                    front = images[0]
                if front is not None:
                    thumbnails = front.get("thumbnails", {})
                    return thumbnails.get("500") or thumbnails.get("250") or front.get("image")
            return None
        except (ClientError, TimeoutError, ValueError):
            _LOGGER.debug("Artwork lookup failed for %s - %s", artist, title, exc_info=True)
            return None

    async def _search_releases(self, query: str) -> list[dict[str, Any]]:
        """Find official album releases while respecting the API rate limit."""
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            wait = 1.05 - (loop.time() - self._last_musicbrainz_request)
            if wait > 0:
                await asyncio.sleep(wait)
            async with self._session.get(
                "https://musicbrainz.org/ws/2/recording/",
                params={"query": query, "fmt": "json", "limit": 25},
                headers={"User-Agent": _USER_AGENT},
                timeout=ClientTimeout(total=10),
            ) as response:
                self._last_musicbrainz_request = loop.time()
                if response.status != 200:
                    return []
                data: dict[str, Any] = await response.json()
        releases: list[dict[str, Any]] = []
        for recording in data.get("recordings", []):
            recording_score = int(recording.get("score", 0))
            recording_artists = [
                str(credit.get("name") or (credit.get("artist") or {}).get("name", ""))
                for credit in recording.get("artist-credit", [])
                if isinstance(credit, dict)
            ]
            for release in recording.get("releases", []):
                item = dict(release)
                item["score"] = recording_score
                item["_recording_artists"] = recording_artists
                item["_recording_first_release_date"] = recording.get(
                    "first-release-date", ""
                )
                releases.append(item)
        return releases

    @staticmethod
    def _release_candidates(
        releases: list[dict[str, Any]], artist: str, album: str, track_title: str
    ) -> list[str]:
        """Rank likely original/official covers ahead of unrelated compilations."""
        wanted_album = _normalise(album)
        wanted_track = _normalise(track_title)
        wanted_artist = _normalise(artist)
        ranked: list[tuple[tuple[Any, ...], str]] = []
        for release_order, release in enumerate(releases):
            search_score = int(release.get("score", 0))
            if search_score and search_score < 80:
                continue
            release_id = release.get("id")
            if not release_id:
                continue
            title = _normalise(str(release.get("title", "")))
            exact_album = bool(wanted_album and title == wanted_album)
            partial_album = bool(
                wanted_album and title and (wanted_album in title or title in wanted_album)
            )
            title_named_release = bool(wanted_track and title == wanted_track)
            group = release.get("release-group") or {}
            primary_type = str(group.get("primary-type", "")).casefold()
            secondary_types = {
                str(value).casefold() for value in group.get("secondary-types", [])
            }
            undesirable = bool(
                secondary_types.intersection(
                    {
                        "compilation",
                        "live",
                        "dj-mix",
                        "mixtape/street",
                        "demo",
                        "remix",
                    }
                )
            )
            official = str(release.get("status", "")).casefold() == "official"
            release_artists = {
                _normalise(
                    str(credit.get("name") or (credit.get("artist") or {}).get("name", ""))
                )
                for credit in release.get("artist-credit", [])
                if isinstance(credit, dict)
            }
            exact_release_artist = wanted_artist in release_artists
            recording_artists = {
                _normalise(str(value))
                for value in release.get("_recording_artists", [])
            }
            exact_recording_artist = wanted_artist in recording_artists
            text_representation = release.get("text-representation") or {}
            language = str(text_representation.get("language", "")).casefold()
            language_rank = 0 if language == "eng" else 1 if not language else 2
            country = str(release.get("country", "")).upper()
            country_rank = (
                0
                if country in {"GB", "US", "IE", "CA", "AU", "NZ", "XW"}
                else 1 if not country else 2
            )
            date = str(
                release.get("date")
                or release.get("_recording_first_release_date")
                or "9999"
            )
            rank = (
                not official,
                not (exact_release_artist or exact_recording_artist),
                undesirable,
                primary_type != "album",
                language_rank,
                country_rank,
                date,
                -search_score,
                not exact_album,
                not partial_album,
                not title_named_release,
                release_order,
            )
            ranked.append((rank, release_id))
        ranked.sort(key=lambda item: item[0])
        return list(dict.fromkeys(release_id for _, release_id in ranked))[:10]
