# -*- coding: utf-8 -*-
"""#1227: a segment that runs to the end of the file must not be skipped by seeking
to just before EOF (Kodi's decoder can stall there and the next episode never starts)."""
from unittest.mock import Mock

import jellyfin_kodi.player as player_module

import pytest

RUNTIME = 1236.106  # S04E05 from the #1227 report


class FakePlaylist:
    def __init__(self, position, size):
        self._position = position
        self._size = size

    def getposition(self):
        return self._position

    def size(self):
        return self._size


@pytest.fixture
def player(monkeypatch):
    p = player_module.Player()
    p.skip_segments = {}
    p.skip_prompted = set()
    p.up_next = True  # keep next_up() (server call) out of these tests
    p.seekTime = Mock()
    p.stop = Mock()
    p.playnext = Mock()
    p.getTotalTime = Mock(return_value=RUNTIME)
    p.getTime = Mock(return_value=1200.0)
    p.played = {}
    monkeypatch.setattr(p, "get_playing_file", Mock(return_value="file"))
    monkeypatch.setattr(p, "is_playing_file", Mock(return_value=True))
    p._item = {"Id": "ep", "Runtime": int(RUNTIME * 10000000)}
    monkeypatch.setattr(p, "get_file_info", Mock(return_value=p._item))
    monkeypatch.setattr(player_module, "dialog", Mock())
    monkeypatch.setattr(
        player_module.xbmc, "PlayList", Mock(return_value=FakePlaylist(0, 1))
    )
    return p


def quiet_monitor():
    return Mock(abortRequested=Mock(return_value=False), waitForAbort=Mock(return_value=False))


def use_mode(monkeypatch, mode):
    monkeypatch.setattr(player_module, "settings", Mock(return_value=mode))


def credits(player, start, end, kind="Credits"):
    player.skip_segments["ep"] = {
        "c1": {"EpisodeId": "ep", "Type": kind, "Start": start, "End": end}
    }


@pytest.mark.parametrize("end", [RUNTIME, RUNTIME + 5.0, RUNTIME - 1.0, RUNTIME - 2.9])
def test_auto_skip_credits_to_end_stops_instead_of_seeking(monkeypatch, player, end):
    use_mode(monkeypatch, 1)
    credits(player, 1190.0, end)

    player.check_skip_segments(player._item, 1200)

    player.seekTime.assert_not_called()
    player.stop.assert_called_once()
    assert player._item["FinishPosition"] == pytest.approx(RUNTIME)


def test_auto_skip_credits_to_end_plays_next_playlist_entry(monkeypatch, player):
    use_mode(monkeypatch, 1)
    monkeypatch.setattr(
        player_module.xbmc, "PlayList", Mock(return_value=FakePlaylist(0, 2))
    )
    credits(player, 1190.0, RUNTIME)

    player.check_skip_segments(player._item, 1200)

    player.seekTime.assert_not_called()
    player.playnext.assert_called_once()
    player.stop.assert_not_called()


def test_last_playlist_entry_stops(monkeypatch, player):
    use_mode(monkeypatch, 1)
    monkeypatch.setattr(
        player_module.xbmc, "PlayList", Mock(return_value=FakePlaylist(1, 2))
    )
    credits(player, 1190.0, RUNTIME)

    player.check_skip_segments(player._item, 1200)

    player.playnext.assert_not_called()
    player.stop.assert_called_once()


def test_credits_ending_well_before_eof_still_seek(monkeypatch, player):
    use_mode(monkeypatch, 1)
    credits(player, 1100.0, 1150.0)  # a post-credits scene follows
    player.getTime = Mock(return_value=1110.0)

    player.check_skip_segments(player._item, 1110)

    player.seekTime.assert_called_once_with(pytest.approx(1150.0))
    player.stop.assert_not_called()


def test_intro_is_unchanged(monkeypatch, player):
    use_mode(monkeypatch, 1)
    credits(player, 60.0, 90.0, kind="Introduction")
    player.getTime = Mock(return_value=61.0)

    player.check_skip_segments(player._item, 61)

    player.seekTime.assert_called_once_with(pytest.approx(90.0))


def test_unknown_runtime_keeps_old_seek(monkeypatch, player):
    use_mode(monkeypatch, 1)
    player.getTotalTime = Mock(return_value=0)
    player._item["Runtime"] = 0
    credits(player, 1190.0, 1236.0)

    player.check_skip_segments(player._item, 1200)

    player.stop.assert_not_called()
    player.seekTime.assert_called_once_with(pytest.approx(1236.0))


def test_already_past_segment_end_does_nothing(monkeypatch, player):
    use_mode(monkeypatch, 1)
    credits(player, 1190.0, RUNTIME - 1.0)
    player.getTime = Mock(return_value=RUNTIME - 0.5)

    player._handle_skip_segment("Credits", 1190.0, RUNTIME - 1.0, 1)

    player.stop.assert_not_called()
    player.seekTime.assert_not_called()


def test_skip_button_to_end_finishes_instead_of_seeking(monkeypatch, player):
    use_mode(monkeypatch, 2)
    shown = {}

    def fake_show(segment_type, duration, end_time):
        shown["end_time"] = end_time
        # the user presses Skip
        player.skip_dialog = Mock(is_skip=Mock(return_value=True))
        player._skip_end_time = end_time
        player._monitor_skip_dialog()

    monkeypatch.setattr(player, "_show_skip_button", fake_show)
    monkeypatch.setattr(player_module.xbmc, "Monitor", quiet_monitor)
    credits(player, 1190.0, RUNTIME)

    player.check_skip_segments(player._item, 1200)

    assert shown["end_time"] == pytest.approx(RUNTIME - player_module.END_OF_FILE_WINDOW)
    player.seekTime.assert_not_called()
    player.stop.assert_called_once()
    assert player._skip_to_end is False


def test_skip_button_mid_file_still_seeks(monkeypatch, player):
    use_mode(monkeypatch, 2)
    player._skip_to_end = True  # left over from an earlier segment

    def fake_show(segment_type, duration, end_time):
        player.skip_dialog = Mock(is_skip=Mock(return_value=True))
        player._skip_end_time = end_time
        player._monitor_skip_dialog()

    monkeypatch.setattr(player, "_show_skip_button", fake_show)
    monkeypatch.setattr(player_module.xbmc, "Monitor", quiet_monitor)
    credits(player, 60.0, 90.0, kind="Introduction")
    player.getTime = Mock(return_value=61.0)

    player.check_skip_segments(player._item, 61)

    player.seekTime.assert_called_once_with(pytest.approx(90.0))
    player.stop.assert_not_called()


def test_stop_reports_finish_position_so_item_counts_as_played(monkeypatch, player):
    server = Mock()
    server.jellyfin.get_item.return_value = {}
    item = {
        "Id": "ep",
        "MediaSourceId": "ms",
        "PlaySessionId": "ps",
        "PlayMethod": "DirectPlay",
        "CurrentPosition": 1200,
        "FinishPosition": RUNTIME,
        "Runtime": int(RUNTIME * 10000000),
        "Server": server,
    }
    player.played = {"file": item}
    monkeypatch.setattr(player, "get_file_info", Mock(return_value=item))
    monkeypatch.setattr(player_module, "window", Mock(return_value=False))
    monkeypatch.setattr(player_module.xbmcvfs, "exists", Mock(return_value=False))
    monkeypatch.setattr(player_module, "settings", Mock(return_value=False))

    player.stop_playback()

    data = server.jellyfin.session_stop.call_args[0][0]
    assert data["PositionTicks"] == int(RUNTIME * 10000000)


@pytest.mark.parametrize(
    "stored,expected",
    [(int(RUNTIME * 10000000), RUNTIME), (int(RUNTIME), int(RUNTIME))],
)
def test_runtime_fallback_reads_ticks_and_seconds(monkeypatch, player, stored, expected):
    """getTotalTime() failing: the item's Runtime is ticks (server) or seconds (Kodi fill)."""
    player.getTotalTime = Mock(side_effect=RuntimeError("not playing"))
    player._item["Runtime"] = stored

    assert player._get_runtime_for_seek() == pytest.approx(expected)


def test_finish_position_in_seconds_when_kodi_runtime_unavailable(monkeypatch, player):
    use_mode(monkeypatch, 1)
    player.getTotalTime = Mock(side_effect=RuntimeError("not playing"))
    credits(player, 1190.0, RUNTIME)

    player.check_skip_segments(player._item, 1200)

    player.stop.assert_called_once()
    assert player._item["FinishPosition"] == pytest.approx(RUNTIME)
