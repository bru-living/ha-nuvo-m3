"""Tests for the dependency-light NV-M3 protocol layer."""

import asyncio
from datetime import UTC
from unittest.mock import AsyncMock

import pytest

from custom_components.nuvo_m3.protocol import MenuItem, MenuPage, NuvoM3Client


def test_connection_urls_match_t2fam_options() -> None:
    serial = NuvoM3Client({"connection_type": "serial", "serial_path": "/dev/ttyUSB0"})
    tcp = NuvoM3Client({"connection_type": "tcp", "host": "192.0.2.10", "port": 4999})
    assert serial._connection_url == "/dev/ttyUSB0"
    assert tcp._connection_url == "socket://192.0.2.10:4999"


@pytest.mark.asyncio
async def test_play_all_uses_native_main_menu_item() -> None:
    client = NuvoM3Client("loop://")
    play_all = MenuItem(7, "Play All", 0, 5, 4294967295)
    client.browse_menu = AsyncMock(
        return_value=MenuPage(4294967295, "Main Menu", 7, 0, 7, 0, [play_all])
    )
    client.play_menu_item = AsyncMock()

    await client.play_all("A")

    client.play_menu_item.assert_awaited_once_with(
        "A", [], (4294967295, 7, 5)
    )


def test_parse_version_and_server_status() -> None:
    client = NuvoM3Client("loop://")
    assert client.parse_packet("#VER,1.10.0194,1.10.0155,1.10.0156,1.10.0157\r")
    assert client.versions == ("1.10.0194", "1.10.0155", "1.10.0156", "1.10.0157")
    assert client.parse_packet("#STATUS,NORMAL\r")
    assert client.server_status == "NORMAL"


def test_parse_nul_prefixed_server_status_from_tcp_bridge() -> None:
    client = NuvoM3Client("loop://")
    assert client.parse_packet("\x00\x00#STATUS,NORMAL\r\n")
    assert client.server_status == "NORMAL"


def test_parse_output_status_with_quoted_commas() -> None:
    client = NuvoM3Client("loop://")
    packet = '#OUT\'A\'STATUS,2,3,12,"Earth, Wind & Fire","Greatest Hits","September",625,2150,1,0\r'
    assert client.parse_packet(packet)
    state = client.outputs["A"]
    assert state.artist == "Earth, Wind & Fire"
    assert state.title == "September"
    assert state.current_track == 3
    assert state.total_tracks == 12
    assert state.elapsed == 62.5
    assert state.duration == 215
    assert state.shuffle is True
    assert state.repeat is False
    assert state.updated_at is not None and state.updated_at.tzinfo == UTC


def test_combined_play_status_and_license_error() -> None:
    client = NuvoM3Client("loop://")
    client.parse_packet('#OUT\'C\'STATUS,8,1,1,"A","B","C",0,100,1,1\r')
    assert client.outputs["C"].play_status == 8
    client.parse_packet("#OUT'C'LICENSEERROR\r")
    assert client.outputs["C"].license_errors == 1


def test_invalid_packet_is_counted_without_crashing() -> None:
    client = NuvoM3Client("loop://")
    assert client.parse_packet("#OUT'B'STATUS,not,enough\r")
    assert client.diagnostics.protocol_errors == 1


@pytest.mark.asyncio
async def test_command_generation() -> None:
    client = NuvoM3Client("loop://")
    client._minimum_write_gap = 0
    client.send = AsyncMock()
    await client.play("A")
    await client.pause("B")
    await client.next_track("C")
    await client.previous_track("A")
    await client.skip("B", 12.34)
    await client.skip("C", -5)
    await client.set_shuffle("A", True)
    await client.set_repeat("B", False)
    assert [call.args[0] for call in client.send.await_args_list] == [
        "OUT'A'PLAY", "OUT'B'PAUSE", "OUT'C'NEXTTRACK", "OUT'A'PREVIOUSTRACK",
        "OUT'B'SKIPFORWARD,123", "OUT'C'SKIPBACK,50", "OUT'A'SHUFFLE,1", "OUT'B'REPEAT,0",
    ]


@pytest.mark.asyncio
async def test_menu_response_and_path_commands() -> None:
    client = NuvoM3Client("loop://")
    sent: list[str] = []

    async def send(command: str) -> None:
        sent.append(command)
        if "MENUUP" in command:
            client.parse_packet('#OUT\'A\'MENU,4294967295,"Main Menu",2,0,2,0\r')
            client.parse_packet('#OUT\'A\'MENUITEM,2,"Albums",1\r')
            client.parse_packet('#OUT\'A\'MENUITEM,5,"Playlists",1\r')

    client.send = send
    page = await client.browse_menu("A", [])
    assert page.title == "Main Menu"
    assert [(item.item_id, item.title, item.index) for item in page.items] == [
        (2, "Albums", 0), (5, "Playlists", 1)
    ]
    assert all(item.menu_id == 4294967295 for item in page.items)
    assert sent[:2] == ["OUT'A'MENUEXIT", "OUT'A'MENUUP,0,0,0"]
    client._cancel_menu_keepalive("A")


@pytest.mark.asyncio
async def test_truncated_menu_item_is_skipped_without_losing_position() -> None:
    client = NuvoM3Client("loop://")

    async def send(command: str) -> None:
        if "MENUUP" in command:
            client.parse_packet('#OUT\'A\'MENU,6,"Tracks",3,20,3,0\r')
            client.parse_packet('#OUT\'A\'MENUITEM,100,"First",0\r')
            client.parse_packet('#OUT\'A\'MENUITEM,broken,"Truncated\r')
            client.parse_packet('#OUT\'A\'MENUITEM,102,"Third",0\r')

    client.send = send
    page = await client.browse_menu("A", [])

    assert [item.index for item in page.items] == [20, 21, 22]
    assert page.items[1].disabled
    assert page.items[2].item_id == 102
    client._cancel_menu_keepalive("A")


@pytest.mark.asyncio
async def test_browse_menu_uses_requested_timeout() -> None:
    client = NuvoM3Client("loop://")
    client._reset_menu = AsyncMock()
    page = MenuPage(4294967295, "Main Menu", 0, 0, 0, 0, [])
    client._request_menu = AsyncMock(return_value=page)

    assert await client.browse_menu("A", [], timeout=30) is page
    client._request_menu.assert_awaited_once_with(
        "A", "OUT'A'MENUUP,0,0,0", timeout=30
    )
    client._cancel_menu_keepalive("A")


@pytest.mark.asyncio
async def test_menu_keepalive_is_bounded_and_releases_session() -> None:
    client = NuvoM3Client("loop://")
    client._menu_keepalive_interval = 0.01
    client._menu_keepalive_timeout = 0.035
    sent: list[str] = []

    async def send(command: str) -> None:
        sent.append(command)

    client.send = send
    client._start_menu_keepalive("B", 42)
    await asyncio.sleep(0.06)

    assert "OUT'B'MENUACTIVE,42" in sent
    assert sent[-1] == "OUT'B'MENUEXIT"
    assert client.diagnostics.menu_keepalives_sent >= 1
    assert client._menu_keepalive_tasks["B"] is None


@pytest.mark.asyncio
async def test_jukebox_option_discovery_and_toggle() -> None:
    client = NuvoM3Client("loop://")
    sent: list[str] = []
    enabled = True

    async def send(command: str) -> None:
        nonlocal enabled
        sent.append(command)
        if "MENUUP" in command:
            client.parse_packet('#OUT\'A\'MENU,4294967295,"Main Menu",1,0,1,0\r')
            client.parse_packet('#OUT\'A\'MENUITEM,8,"Options",1\r')
        elif "MENUSELECT,4294967295" in command:
            client.parse_packet('#OUT\'A\'MENU,8,"Options",1,0,1,0\r')
            flags = 9 if enabled else 1
            client.parse_packet(f'#OUT\'A\'MENUITEM,3,"Juke Box Mode",{flags}\r')
        elif "MENUPLAY,8,3" in command:
            enabled = not enabled
            client.parse_packet("#OK\r")

    client.send = send
    assert await client.get_jukebox_mode("A") is True
    assert await client.set_jukebox_mode("A", False) is False
    assert "OUT'A'MENUPLAY,8,3,0" in sent
    assert await client.set_jukebox_mode("A", False) is False
    assert sent.count("OUT'A'MENUPLAY,8,3,0") == 1
    assert await client.set_jukebox_mode("A", True) is True
    assert sent.count("OUT'A'MENUPLAY,8,3,0") == 2
