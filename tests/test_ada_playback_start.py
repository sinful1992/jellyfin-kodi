"""K11 and the start-of-playback track choice.

The tracks the add-on chose must be applied 2 s in, even when Kodi reports
other tracks at start, and an intro at 0:00 must not hold onPlayBackStarted.
"""

from unittest.mock import Mock

import pytest

import jellyfin_kodi.player as player_module


def start(
    monkeypatch,
    kodi_audio=None,
    subs_enabled=False,
    kodi_subs=0,
    option="Addon",
    segments=False,
    fetch_error=False,
    audio=4,
    subtitle=7,
):
    server = Mock()
    item = {
        "Id": "item",
        "Path": "movie",
        "PlayOption": option,
        "KodiAudioStreamIndexes": [3, 4],
        "SubsMapping": {},
        "AudioStreamIndex": audio,
        "SubtitleStreamIndex": subtitle,
        "MediaSourceId": "source",
        "PlayMethod": "DirectPlay",
        "Volume": 100,
        "CurrentPosition": 0,
        "Paused": False,
        "Muted": False,
        "PlaySessionId": "session",
        "Server": server,
    }
    player = object.__new__(player_module.Player)
    player.stop_playback = Mock()
    player._reset_state = Mock()
    player.getPlayingFile = Mock(return_value="movie")
    player.set_item = Mock()
    player.set_audio_subs = Mock()
    player.getAvailableSubtitleStreams = lambda: [1, 2] if subs_enabled else []
    player.getAvailableAudioStreams = lambda: [1, 2]
    player.getTime = lambda: 0.0
    player.check_skip_segments = Mock()
    player._fetch_skip_segments = Mock(
        side_effect=RuntimeError("x") if fetch_error else None
    )

    monitor = Mock()
    monitor.waitForAbort.return_value = False
    monkeypatch.setattr(player_module.xbmc, "Monitor", Mock(return_value=monitor))
    monkeypatch.setattr(
        player_module,
        "settings",
        lambda key, *a: segments if key == "mediaSegmentsEnabled.bool" else False,
    )

    result = {"currentsubtitle": {"index": kodi_subs}, "subtitleenabled": subs_enabled}
    result["currentaudiostream"] = {} if kodi_audio is None else {"index": kodi_audio}

    class JSONRPC:
        def __init__(self, _):
            pass

        def execute(self, _):
            return {"result": result}

    monkeypatch.setattr(player_module, "JSONRPC", JSONRPC)
    monkeypatch.setattr(
        player_module,
        "window",
        lambda name, value=None, **k: (
            [item] if name == "jellyfin_play.json" and value is None else None
        ),
    )
    player.onPlayBackStarted()
    return player, item, server


def test_planned_audio_wins_over_what_kodi_opened(monkeypatch):
    # Kodi already reports its first track (ordinal 0 = Jellyfin index 3).
    player, item, _ = start(monkeypatch, kodi_audio=0)
    player.set_audio_subs.assert_called_once_with(4, 7)
    assert item["AudioStreamIndex"] == 4


def test_planned_subtitle_survives_subs_off_at_start(monkeypatch):
    player, item, _ = start(monkeypatch, subs_enabled=False)
    player.set_audio_subs.assert_called_once_with(4, 7)
    assert item["SubtitleStreamIndex"] == 7


def test_session_start_reports_what_kodi_shows(monkeypatch):
    _, _, server = start(monkeypatch, kodi_audio=0)
    sent = server.jellyfin.session_playing.call_args[0][0]
    assert sent["AudioStreamIndex"] == 3 and sent["SubtitleStreamIndex"] is None


def test_planned_subtitles_off_are_applied(monkeypatch):
    player, _, _ = start(monkeypatch, subtitle=-1)
    player.set_audio_subs.assert_called_once_with(4, -1)


def test_intro_check_is_left_to_the_checker_thread(monkeypatch):
    player, _, _ = start(monkeypatch, segments=True)
    player._fetch_skip_segments.assert_called_once()
    player.check_skip_segments.assert_not_called()
    player.set_audio_subs.assert_called_once()


def test_segment_fetch_error_still_sets_tracks(monkeypatch):
    player, _, _ = start(monkeypatch, segments=True, fetch_error=True)
    player.set_audio_subs.assert_called_once_with(4, 7)


@pytest.mark.parametrize("segments", [False, True])
def test_native_mode_does_not_force_tracks(monkeypatch, segments):
    player, item, _ = start(
        monkeypatch, kodi_audio=0, option="Native", segments=segments
    )
    player.set_audio_subs.assert_not_called()
    assert item["AudioStreamIndex"] == 3
