"""K3: the stop report sends the position on screen, not the last ~11 s progress poll."""

import ast
import pathlib
from unittest.mock import Mock

import pytest

import jellyfin_kodi.player as player_module


@pytest.fixture
def stop_env(monkeypatch):
    windows = {}

    def fake_window(key, value=None, clear=False):
        if clear:
            windows.pop(key, None)
            return None
        if value is not None:
            windows[key] = value
            return None
        return windows.get(key)

    monkeypatch.setattr(player_module, "window", fake_window)
    monkeypatch.setattr(player_module, "settings", lambda *a, **k: False)
    monkeypatch.setattr(player_module.xbmcvfs, "exists", lambda path: False)
    monkeypatch.setattr(player_module.xbmcvfs, "translatePath", lambda path: path)
    return windows


def make_player(item):
    player = object.__new__(player_module.Player)
    player.played = {"file": item}
    player._reset_state = Mock()
    return player


def make_item(**extra):
    server = Mock()
    server.jellyfin.get_item.return_value = {}
    item = {
        "Id": "item",
        "MediaSourceId": "source",
        "PlaySessionId": "session",
        "PlayMethod": "DirectPlay",
        "Runtime": 13616520000,
        "CurrentPosition": 100,
        "Server": server,
    }
    item.update(extra)
    return item


def sent_ticks(item):
    return item["Server"].jellyfin.session_stop.call_args[0][0]["PositionTicks"]


def test_stop_sends_live_position(stop_env):
    item = make_item(LivePosition=109.4)
    make_player(item).stop_playback()
    assert sent_ticks(item) == 1094000000


def test_stop_without_live_position_keeps_polled_position(stop_env):
    item = make_item()
    make_player(item).stop_playback()
    assert sent_ticks(item) == 1000000000


def test_finish_position_beats_live_position(stop_env):
    # linus's #1227 finish (credits skipped to the end) must still count as watched.
    item = make_item(LivePosition=1300.0, FinishPosition=1361.652)
    make_player(item).stop_playback()
    assert sent_ticks(item) == 13616520000


def test_live_position_is_used_once(stop_env):
    item = make_item(LivePosition=50.0)
    make_player(item).stop_playback()
    assert "LivePosition" not in item


def test_live_position_zero_is_sent(stop_env):
    # 0.0 is a real position (stopped right at the start), not "missing".
    item = make_item(LivePosition=0.0)
    make_player(item).stop_playback()
    assert sent_ticks(item) == 0


def test_track_position_stores_float_time():
    item = {}
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "file"
    player.is_playing_file = lambda _: True
    player.get_file_info = lambda _: item
    player.getTime = lambda: 4242.75
    player.track_position()
    assert item["LivePosition"] == 4242.75


def test_track_position_ignores_files_we_do_not_play():
    item = {}
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "other"
    player.is_playing_file = lambda _: False
    player.get_file_info = lambda _: item
    player.getTime = lambda: 10.0
    player.track_position()
    assert "LivePosition" not in item


def test_track_position_survives_player_gone():
    item = {"LivePosition": 12.0}
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "file"
    player.is_playing_file = lambda _: True
    player.get_file_info = lambda _: item

    def gone():
        raise RuntimeError("Kodi is not playing any media file")

    player.getTime = gone
    player.track_position()
    assert item["LivePosition"] == 12.0


def test_seek_records_position_first(monkeypatch):
    calls = []
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "file"
    player.is_playing_file = lambda _: True
    player.track_position = lambda: calls.append("track")
    player.report_playback = lambda *a, **k: calls.append("report")
    monkeypatch.setattr(player_module, "settings", lambda *a, **k: False)
    player.onPlayBackSeek(1000, 0)
    assert calls == ["track", "report"]


def _calls_in_service_loop():
    source = (
        pathlib.Path(__file__).parents[1]
        / "jellyfin_kodi"
        / "entrypoint"
        / "service.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "service":
            for loop in ast.walk(node):
                if isinstance(loop, ast.While):
                    return [
                        c.func.attr
                        for c in ast.walk(loop)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    ]
    return []


def test_service_loop_tracks_position_every_tick():
    assert "track_position" in _calls_in_service_loop()


def test_external_player_reports_runtime_in_seconds(stop_env):
    # Runtime holds ticks; it was sent as seconds, i.e. 1e7 times too far.
    stop_env["jellyfin.external.bool"] = True
    item = make_item(CurrentPosition=1)
    make_player(item).stop_playback()
    assert sent_ticks(item) == 13616520000


def test_external_player_with_seconds_runtime(stop_env):
    stop_env["jellyfin.external.bool"] = True
    item = make_item(CurrentPosition=1, Runtime=1361)
    make_player(item).stop_playback()
    assert sent_ticks(item) == 13610000000


def test_runtime_seconds_bad_values():
    assert player_module.Player._runtime_seconds({"Runtime": None}) == 0.0
    assert player_module.Player._runtime_seconds({"Runtime": "x"}) == 0.0
    assert player_module.Player._runtime_seconds({}) == 0.0
