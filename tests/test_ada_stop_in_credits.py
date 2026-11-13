"""K8: Stop pressed inside end credits counts the item as watched."""

from unittest.mock import Mock

import pytest

import jellyfin_kodi.player as player_module

RUNTIME_TICKS = 13200000000  # 1320 s


@pytest.fixture(autouse=True)
def stop_env(monkeypatch):
    monkeypatch.setattr(player_module, "window", lambda *a, **k: None)
    monkeypatch.setattr(player_module, "settings", lambda *a, **k: False)
    monkeypatch.setattr(player_module.xbmcvfs, "exists", lambda path: False)
    monkeypatch.setattr(player_module.xbmcvfs, "translatePath", lambda path: path)


def credits(start, end, kind="Credits"):
    return {"seg": {"Type": kind, "Start": start, "End": end}}


def stop(position, segments, runtime=RUNTIME_TICKS, live=None):
    server = Mock()
    server.jellyfin.get_item.return_value = {}
    item = {
        "Id": "ep",
        "MediaSourceId": "source",
        "PlaySessionId": "session",
        "PlayMethod": "DirectPlay",
        "Runtime": runtime,
        "CurrentPosition": position,
        "Server": server,
    }
    if live is not None:
        item["LivePosition"] = live
    player = object.__new__(player_module.Player)
    player.played = {"file": item}
    player.skip_segments = {"ep": segments} if segments else {}
    player.skip_prompted = set()
    player._reset_segment_checker = Mock()
    player._reset_skip_dialog = Mock()
    player.stop_playback()
    return server.jellyfin.session_stop.call_args[0][0]["PositionTicks"]


def test_stop_in_end_credits_reports_runtime():
    assert stop(1175, credits(1170.0, 1320.0)) == RUNTIME_TICKS


def test_stop_before_credits_is_unchanged():
    assert stop(600, credits(1170.0, 1320.0)) == 6000000000


def test_live_position_inside_credits_counts():
    # Polled position is before the credits, the screen is inside them.
    assert stop(1165, credits(1170.0, 1320.0), live=1171.5) == RUNTIME_TICKS


def test_credits_ending_before_the_end_do_not_count():
    # Mid-file credits (a post-credits scene follows) keep the real position.
    assert stop(1100, credits(1050.0, 1200.0)) == 11000000000


def test_credits_within_window_of_end_count():
    assert stop(1300, credits(1170.0, 1318.0)) == RUNTIME_TICKS


def test_intro_segment_does_not_count():
    assert stop(1175, credits(1170.0, 1320.0, kind="Intro")) == 11750000000


def test_no_segments_is_unchanged():
    assert stop(1175, None) == 11750000000


def test_zero_runtime_is_unchanged():
    assert stop(1175, credits(1170.0, 1320.0), runtime=0) == 11750000000


def test_seconds_runtime_from_kodi_fallback_is_understood():
    # set_item can fill Runtime with Kodi seconds (1320) instead of ticks.
    assert stop(1175, credits(1170.0, 1320.0), runtime=1320) == RUNTIME_TICKS


def test_broken_segment_is_ignored():
    assert stop(1175, credits(1320.0, 1170.0)) == 11750000000


def test_credits_marked_past_the_runtime_count():
    assert stop(1175, credits(1170.0, 1325.0)) == RUNTIME_TICKS


def test_stop_after_credits_end_near_the_end_counts():
    # Credits end 2 s before the runtime; Stop pressed in the last second.
    assert stop(1319, credits(1170.0, 1318.0)) == RUNTIME_TICKS
