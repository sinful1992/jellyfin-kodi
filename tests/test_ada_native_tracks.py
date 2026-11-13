"""Native mode (Kodi plays the NAS file itself): series track memory and the
lossless pick work by matching Kodi's own stream list (linus #11195)."""

from unittest.mock import Mock

import pytest

import jellyfin_kodi.player as player_module
from jellyfin_kodi.helper import playutils as pu

NOW = 1_800_000_000.0

KODI = {
    "audiostreams": [
        {"index": 0, "language": "eng", "codec": "ac3", "channels": 6, "name": "Main"},
        {"index": 1, "language": "eng", "codec": "truehd", "channels": 8, "name": ""},
        {"index": 2, "language": "jpn", "codec": "dtshd_ma", "channels": 6, "name": ""},
    ],
    "subtitles": [
        {"index": 0, "language": "eng", "name": "Signs", "isforced": True},
        {"index": 1, "language": "eng", "name": "Full", "isforced": False},
    ],
    "currentaudiostream": {
        "index": 0,
        "language": "eng",
        "codec": "ac3",
        "channels": 6,
        "name": "Main",
    },
    "currentsubtitle": {},
    "subtitleenabled": False,
}


@pytest.fixture
def env(monkeypatch):
    state = {
        "settings": {
            "rememberSeriesTracks.bool": True,
            "preferLosslessAudio.bool": True,
        },
        "memory": {},
        "saved": [],
        "passthrough": {"truehd", "dtshd"},
        "props": dict(KODI),
    }
    monkeypatch.setattr(
        player_module, "settings", lambda key, *a: state["settings"].get(key, False)
    )
    monkeypatch.setattr(pu, "load_track_memory", lambda path=None: state["memory"])
    monkeypatch.setattr(
        pu,
        "save_track_memory",
        lambda memory, path=None: state["saved"].append(dict(memory)),
    )
    monkeypatch.setattr(pu, "kodi_lossless_passthrough", lambda: state["passthrough"])
    monkeypatch.setattr(pu.time, "time", lambda: NOW)
    monkeypatch.setattr(
        player_module.Player, "_kodi_streams", staticmethod(lambda: state["props"])
    )
    return state


def native_item(kind="Episode", series="show"):
    server = Mock()
    server.jellyfin.get_item.return_value = {"SeriesId": series} if series else {}
    server.config.data = {"auth.user_id": "user"}
    return {"Id": "ep", "Type": kind, "PlayOption": "Native", "Server": server}


def make_player():
    player = object.__new__(player_module.Player)
    player.setAudioStream = Mock()
    player.setSubtitleStream = Mock()
    player.showSubtitles = Mock()
    return player


def test_from_kodi_maps_dts_family_and_blanks():
    converted = player_module.Player._from_kodi(KODI["audiostreams"][2], "Audio")
    assert (converted["Codec"], converted["Profile"]) == ("dts", "DTS-HD MA")
    blank = player_module.Player._from_kodi(
        {"index": 3, "language": "", "name": ""}, "Subtitle"
    )
    assert (
        blank["Language"] is None and blank["Title"] is None and blank["Codec"] is None
    )


def test_lossless_pick_in_native_mode(env):
    player = make_player()
    player.apply_native_tracks(native_item(kind="Movie"))
    player.setAudioStream.assert_called_once_with(1)


def test_no_passthrough_no_switch(env):
    env["passthrough"] = set()
    player = make_player()
    player.apply_native_tracks(native_item(kind="Movie"))
    player.setAudioStream.assert_not_called()


def test_series_memory_applied_in_native_mode(env):
    env["memory"]["user/show"] = {
        "Audio": {"Language": "jpn"},
        "Subtitle": {"Language": "eng", "Title": "Full", "IsForced": False},
        "Saved": NOW,
    }
    player = make_player()
    item = native_item()
    player.apply_native_tracks(item)
    player.setAudioStream.assert_called_once_with(2)
    player.setSubtitleStream.assert_called_once_with(1)
    player.showSubtitles.assert_called_once_with(True)
    assert item["TrackMemoryKey"] == "user/show"


def test_memory_subtitles_off(env):
    env["memory"]["user/show"] = {"Audio": None, "Subtitle": "off", "Saved": NOW}
    player = make_player()
    player.apply_native_tracks(native_item())
    player.showSubtitles.assert_called_once_with(False)
    # No remembered audio: the lossless pick still applies.
    player.setAudioStream.assert_called_once_with(1)


def test_memory_language_missing_falls_back_to_lossless(env):
    env["memory"]["user/show"] = {
        "Audio": {"Language": "ger"},
        "Subtitle": None,
        "Saved": NOW,
    }
    player = make_player()
    player.apply_native_tracks(native_item())
    player.setAudioStream.assert_called_once_with(1)
    player.setSubtitleStream.assert_not_called()


def test_both_settings_off_does_nothing(env, monkeypatch):
    env["settings"] = {}
    called = []
    monkeypatch.setattr(
        player_module.Player, "_kodi_streams", staticmethod(lambda: called.append(1))
    )
    player = make_player()
    player.apply_native_tracks(native_item())
    assert called == []


def test_rpc_failure_does_not_raise(env, monkeypatch):
    def boom():
        raise RuntimeError("rpc down")

    monkeypatch.setattr(player_module.Player, "_kodi_streams", staticmethod(boom))
    make_player().apply_native_tracks(native_item())


def test_movie_and_missing_series_have_no_key(env):
    assert player_module.Player._native_memory_key(native_item(kind="Movie")) is None
    assert player_module.Player._native_memory_key(native_item(series=None)) is None
    assert player_module.Player._native_memory_key(native_item()) == "user/show"


def test_start_is_recorded_after_applying(env):
    item = native_item()
    make_player().apply_native_tracks(item)
    assert item["NativeStart"] == item["NativeTracks"]
    assert item["NativeStart"][1] == "off"


def test_av_change_records_and_stop_saves_a_change(env):
    item = native_item()
    player = make_player()
    player.apply_native_tracks(item)
    player.get_playing_file = lambda: "f"
    player.is_playing_file = lambda _f: True
    player.get_file_info = lambda _f: item
    env["props"] = dict(
        KODI,
        currentaudiostream=KODI["audiostreams"][2],
        currentsubtitle=KODI["subtitles"][1],
        subtitleenabled=True,
    )
    player.onAVChange()
    player_module.Player._remember_series_tracks(item)
    saved = env["saved"][-1]["user/show"]
    assert saved["Audio"]["Language"] == "jpn"
    assert saved["Subtitle"]["Title"] == "Full"


def test_no_change_saves_nothing(env):
    item = native_item()
    make_player().apply_native_tracks(item)
    player_module.Player._remember_series_tracks(item)
    assert env["saved"] == []


def test_av_change_before_start_record_is_ignored(env):
    item = native_item()
    player = make_player()
    player.get_playing_file = lambda: "f"
    player.is_playing_file = lambda _f: True
    player.get_file_info = lambda _f: item
    player.detect_audio_subs = Mock()
    player.onAVChange()
    assert "NativeTracks" not in item
    player.detect_audio_subs.assert_not_called()
