"""Small fixes from the Fable scan (K9 filter part, K15 items in ada's files)."""

import importlib
import json
import sys
from unittest.mock import Mock

import pytest

import jellyfin_kodi.player as player_module
from jellyfin_kodi.entrypoint import service as service_module

# K9: the screensaver hook was filtered out before it could run fast_sync.


class InlineThread:
    """Runs the target at start() so the test sees its effect."""

    started = []

    def __init__(self, target, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self):
        InlineThread.started.append(self)
        self.target()


def make_service(monkeypatch, abort=False):
    service = object.__new__(service_module.Service)
    service.library_thread = Mock()
    service.waitForAbort = Mock(return_value=abort)
    InlineThread.started = []
    monkeypatch.setattr(service_module.threading, "Thread", InlineThread)
    monkeypatch.setattr(
        service_module.xbmc, "sleep", Mock(side_effect=AssertionError("blocks"))
    )
    return service


def test_screensaver_end_runs_fast_sync_in_a_thread(monkeypatch):
    service = make_service(monkeypatch)
    service.onNotification("xbmc", "GUI.OnScreensaverDeactivated", json.dumps({}))
    service.library_thread.fast_sync.assert_called_once_with()
    assert InlineThread.started[0].daemon is True
    service.waitForAbort.assert_called_once_with(5)


def test_screensaver_sync_stops_on_kodi_exit(monkeypatch):
    service = make_service(monkeypatch, abort=True)
    service.onNotification("xbmc", "GUI.OnScreensaverDeactivated", json.dumps({}))
    service.library_thread.fast_sync.assert_not_called()


def test_screensaver_without_library_thread_is_safe(monkeypatch):
    service = make_service(monkeypatch)
    service.library_thread = None
    service.onNotification("xbmc", "GUI.OnScreensaverDeactivated", json.dumps({}))


def test_other_kodi_notifications_still_filtered(monkeypatch):
    service = make_service(monkeypatch)
    service.onNotification("xbmc", "GUI.OnScreensaverActivated", json.dumps({}))
    service.library_thread.fast_sync.assert_not_called()


# K15: maxDaysInNextEpisodes "0" (a string from settings) meant "last 0 days".


def load_default(monkeypatch):
    # default.py reads the plugin handle from sys.argv when imported.
    monkeypatch.setattr(sys, "argv", ["plugin://plugin.video.jellyfin/", "1", "?"])
    return importlib.import_module("jellyfin_kodi.entrypoint.default")


def next_episode_filters(monkeypatch, value):
    default_module = load_default(monkeypatch)
    calls = []

    class FakeDb:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return Mock()

        def __exit__(self, *a):
            return False

    class FakeJellyfinDb:
        def __init__(self, cursor):
            pass

        def get_view_name(self, item_id):
            return "TV"

    class FakeRpc:
        def __init__(self, method):
            self.method = method

        def execute(self, params):
            calls.append(params)
            return {}

    monkeypatch.setattr(default_module, "Database", FakeDb)
    monkeypatch.setattr(default_module.jellyfin_db, "JellyfinDatabase", FakeJellyfinDb)
    monkeypatch.setattr(default_module, "JSONRPC", FakeRpc)
    monkeypatch.setattr(default_module, "settings", lambda key, *a: value)
    default_module.get_next_episodes("view", 5)
    return [f["operator"] for f in calls[0]["filter"]["and"]]


def test_zero_days_means_no_limit(monkeypatch):
    assert "inthelast" not in next_episode_filters(monkeypatch, "0")


def test_days_limit_still_applies(monkeypatch):
    assert "inthelast" in next_episode_filters(monkeypatch, "30")


@pytest.mark.parametrize("value", ["", None, "abc"])
def test_bad_days_value_means_no_limit(monkeypatch, value):
    assert "inthelast" not in next_episode_filters(monkeypatch, value)


# K15: an extra get_item HTTP call on every stop, only needed for offerDelete.


def stop_with(monkeypatch, offer_delete):
    monkeypatch.setattr(player_module, "window", lambda *a, **k: None)
    monkeypatch.setattr(
        player_module,
        "settings",
        lambda key, *a: offer_delete if key == "offerDelete.bool" else False,
    )
    monkeypatch.setattr(player_module.xbmcvfs, "exists", lambda path: False)
    monkeypatch.setattr(player_module.xbmcvfs, "translatePath", lambda path: path)
    server = Mock()
    server.jellyfin.get_item.return_value = {}
    item = {
        "Id": "ep",
        "MediaSourceId": "s",
        "PlaySessionId": "p",
        "PlayMethod": "DirectStream",
        "Runtime": 0,
        "CurrentPosition": 10,
        "Server": server,
    }
    player = object.__new__(player_module.Player)
    player.played = {"f": item}
    player.skip_segments = {}
    player.skip_prompted = set()
    player._reset_segment_checker = Mock()
    player._reset_skip_dialog = Mock()
    player.stop_playback()
    return server


def test_stop_does_not_fetch_item_without_offer_delete(monkeypatch):
    assert not stop_with(monkeypatch, False).jellyfin.get_item.called


def test_stop_fetches_item_for_offer_delete(monkeypatch):
    stop_with(monkeypatch, True).jellyfin.get_item.assert_called_once_with("ep")


# K15: next_up used the last loop item when the current episode was missing.


def test_next_up_missing_current_episode_sends_nothing(monkeypatch):
    events = []
    monkeypatch.setattr(player_module, "settings", lambda *a: True)
    monkeypatch.setattr(player_module, "event", lambda *a, **k: events.append(a))
    server = Mock()
    server.jellyfin.get_adjacent_episodes.return_value = {
        "Items": [{"Id": "a"}, {"Id": "b"}]
    }
    item = {
        "Type": "Episode",
        "Id": "ep",
        "CurrentEpisode": {"tvshowid": 1},
        "Server": server,
    }
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "f"
    player.get_file_info = lambda _f: item
    player.next_up()
    assert events == []


def test_next_up_empty_answer_sends_nothing(monkeypatch):
    events = []
    monkeypatch.setattr(player_module, "settings", lambda *a: True)
    monkeypatch.setattr(player_module, "event", lambda *a, **k: events.append(a))
    server = Mock()
    server.jellyfin.get_adjacent_episodes.return_value = {"Items": []}
    item = {
        "Type": "Episode",
        "Id": "ep",
        "CurrentEpisode": {"tvshowid": 1},
        "Server": server,
    }
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "f"
    player.get_file_info = lambda _f: item
    player.next_up()
    assert events == []


def test_screensaver_sync_error_is_logged_not_raised(monkeypatch):
    service = make_service(monkeypatch)
    service.library_thread.fast_sync.side_effect = RuntimeError("server down")
    logged = []
    monkeypatch.setattr(
        service_module.LOG, "exception", lambda *a, **k: logged.append(a)
    )
    service._screensaver_sync()
    assert logged
    # The lock is released, so the next screensaver end syncs again.
    service.library_thread.fast_sync.side_effect = None
    service._screensaver_sync()
    assert service.library_thread.fast_sync.call_count == 2


def test_second_screensaver_end_while_syncing_is_dropped(monkeypatch):
    service = make_service(monkeypatch)
    calls = []

    def nested_sync():
        calls.append("outer")
        service._screensaver_sync()  # a second end arrives during the sync

    service.library_thread.fast_sync.side_effect = nested_sync
    service._screensaver_sync()
    assert calls == ["outer"]
    assert service.library_thread.fast_sync.call_count == 1
