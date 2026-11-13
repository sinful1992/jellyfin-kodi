"""K2: audio/subtitle picks are remembered per series and used for the next episode."""

import json
from unittest.mock import Mock

import pytest

import jellyfin_kodi.player as player_module
from jellyfin_kodi.helper import playutils as pu

DAY = 86400.0
NOW = 1_800_000_000.0


def stream(index, kind, language, codec="ac3", channels=None, title=None, forced=False):
    return {
        "Index": index,
        "Type": kind,
        "Language": language,
        "Codec": codec,
        "Channels": channels,
        "Title": title,
        "IsForced": forced,
    }


STREAMS = [
    {"Index": 0, "Type": "Video", "Codec": "hevc"},
    stream(1, "Audio", "eng", "eac3", 6),
    stream(2, "Audio", "jpn", "flac", 2),
    stream(3, "Subtitle", "eng", "subrip", title="Signs", forced=True),
    stream(4, "Subtitle", "eng", "subrip", title="Full"),
    stream(5, "Subtitle", "fra", "subrip"),
]


# pure functions


def test_compact_streams_keeps_audio_and_subtitles_only():
    compact = pu.compact_streams(STREAMS)
    assert [s["Index"] for s in compact] == [1, 2, 3, 4, 5]
    assert set(compact[0]) == set(pu._STREAM_FIELDS)
    assert pu.compact_streams(None) == []


def test_describe_stream():
    assert pu.describe_stream(STREAMS, 2) == {
        "Language": "jpn",
        "Codec": "flac",
        "Channels": 2,
        "Title": None,
        "IsForced": False,
    }
    assert pu.describe_stream(STREAMS, 99) is None
    assert pu.describe_stream(None, 1) is None


def test_match_needs_the_language():
    assert pu.match_stream(STREAMS, {"Language": "jpn"}, "Audio") == 2
    assert pu.match_stream(STREAMS, {"Language": "ger"}, "Audio") is None
    assert pu.match_stream(STREAMS, None, "Audio") is None


def test_match_keeps_forced_apart_from_full():
    full = {"Language": "eng", "Codec": "subrip", "Title": None, "IsForced": False}
    forced = {"Language": "eng", "Codec": "subrip", "Title": None, "IsForced": True}
    assert pu.match_stream(STREAMS, full, "Subtitle") == 4
    assert pu.match_stream(STREAMS, forced, "Subtitle") == 3


def test_match_title_wins():
    assert (
        pu.match_stream(
            STREAMS, {"Language": "eng", "Title": "Full", "IsForced": True}, "Subtitle"
        )
        == 4
    )


def test_match_codec_case_and_channels_break_ties():
    streams = [stream(1, "Audio", "eng", "AC3", 2), stream(2, "Audio", "eng", "ac3", 6)]
    assert (
        pu.match_stream(
            streams, {"Language": "eng", "Codec": "ac3", "Channels": 6}, "Audio"
        )
        == 2
    )


def test_remember_and_recall():
    memory = pu.remember_tracks({}, "u/s", {"Language": "jpn"}, "off", NOW)
    assert pu.recall_tracks(memory, "u/s", NOW + DAY)["Subtitle"] == "off"
    assert pu.recall_tracks(memory, "u/other", NOW) is None


def test_memory_expires_after_60_days_and_is_pruned():
    memory = pu.remember_tracks({}, "old", {"Language": "jpn"}, "off", NOW)
    assert pu.recall_tracks(memory, "old", NOW + 61 * DAY) is None
    pu.remember_tracks(memory, "new", None, "off", NOW + 61 * DAY)
    assert "old" not in memory and "new" in memory


def test_apply_off_means_minus_one():
    audio, subtitle = pu.apply_track_memory(
        {"Audio": {"Language": "jpn"}, "Subtitle": "off"}, STREAMS
    )
    assert (audio, subtitle) == (2, -1)


def test_apply_unknown_subtitle_language_leaves_default():
    audio, subtitle = pu.apply_track_memory(
        {"Audio": None, "Subtitle": {"Language": "ger"}}, STREAMS
    )
    assert (audio, subtitle) == (None, None)


def test_save_and_load_roundtrip_creates_folder(tmp_path):
    path = str(tmp_path / "new" / "track_memory.json")
    pu.save_track_memory({"u/s": {"Saved": NOW}}, path)
    assert pu.load_track_memory(path) == {"u/s": {"Saved": NOW}}


def test_load_bad_or_missing_file_is_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert pu.load_track_memory(str(bad)) == {}
    assert pu.load_track_memory(str(tmp_path / "missing.json")) == {}
    listy = tmp_path / "list.json"
    listy.write_text("[1, 2]")
    assert pu.load_track_memory(str(listy)) == {}


# PlayUtils.get wiring


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    path = tmp_path / "track_memory.json"
    monkeypatch.setattr(pu.xbmcvfs, "translatePath", lambda _p: str(path))
    return path


def run_get(
    monkeypatch, series_id="series", remember=True, transcode=False, user="user"
):
    monkeypatch.setattr(pu, "window", lambda *a, **k: None)
    monkeypatch.setattr(pu.client, "get_device_id", lambda: "device")
    monkeypatch.setattr(
        pu,
        "settings",
        lambda key, *a: remember if key == "rememberSeriesTracks.bool" else False,
    )
    monkeypatch.setattr(pu.time, "time", lambda: NOW)

    class Api:
        def get_file_path(self, _):
            return "ep.mkv"

    monkeypatch.setattr(pu.api, "API", lambda *a, **k: Api())
    api_client = Mock()
    api_client.config.data = {"auth.token": "", "auth.user_id": user}
    item = {"Id": "ep", "Type": "Episode", "MediaType": "Video"}
    if series_id:
        item["SeriesId"] = series_id
    source = {
        "Id": "source",
        "Path": "ep.mkv",
        "SupportsDirectPlay": False,
        "SupportsDirectStream": not transcode,
        "MediaStreams": STREAMS,
        "DefaultAudioStreamIndex": 1,
        "DefaultSubtitleStreamIndex": 3,
    }
    play_utils = pu.PlayUtils(item, server="http://server", api_client=api_client)
    play_utils.info["Path"] = "ep.mkv"

    def direct(_s):
        play_utils.info["Method"] = "DirectStream"

    def trans(_s, *_a):
        play_utils.info["Method"] = "Transcode"

    play_utils.direct_url = direct
    play_utils.transcode = trans
    play_utils.get(source)
    return item["PlaybackInfo"]


def write_memory(path, key, audio, subtitle, saved=NOW):
    path.write_text(
        json.dumps({key: {"Audio": audio, "Subtitle": subtitle, "Saved": saved}})
    )


def test_next_episode_starts_with_remembered_tracks(monkeypatch, memory_file):
    write_memory(
        memory_file,
        "user/series",
        {"Language": "jpn"},
        {"Language": "eng", "Title": "Full"},
    )
    info = run_get(monkeypatch)
    assert (info["AudioStreamIndex"], info["SubtitleStreamIndex"]) == (2, 4)
    assert info["TrackMemoryKey"] == "user/series"


def test_remembered_subtitles_off(monkeypatch, memory_file):
    write_memory(memory_file, "user/series", None, "off")
    info = run_get(monkeypatch)
    assert (info["AudioStreamIndex"], info["SubtitleStreamIndex"]) == (1, -1)


def test_other_user_gets_server_defaults(monkeypatch, memory_file):
    write_memory(memory_file, "user/series", {"Language": "jpn"}, "off")
    info = run_get(monkeypatch, user="kid")
    assert (info["AudioStreamIndex"], info["SubtitleStreamIndex"]) == (1, 3)


def test_expired_memory_is_ignored(monkeypatch, memory_file):
    write_memory(
        memory_file, "user/series", {"Language": "jpn"}, "off", saved=NOW - 61 * DAY
    )
    info = run_get(monkeypatch)
    assert info["AudioStreamIndex"] == 1


def test_film_without_series_is_untouched(monkeypatch, memory_file):
    info = run_get(monkeypatch, series_id=None)
    assert info["AudioStreamIndex"] == 1 and "TrackMemoryKey" not in info


def test_setting_off_is_untouched(monkeypatch, memory_file):
    write_memory(memory_file, "user/series", {"Language": "jpn"}, "off")
    info = run_get(monkeypatch, remember=False)
    assert (info["AudioStreamIndex"], info["SubtitleStreamIndex"]) == (1, 3)


def test_transcode_is_untouched(monkeypatch, memory_file):
    write_memory(memory_file, "user/series", {"Language": "jpn"}, "off")
    info = run_get(monkeypatch, transcode=True)
    assert info["AudioStreamIndex"] == 1


def test_set_properties_carries_memory_fields(monkeypatch):
    store = {}

    def fake_window(name, value=None, **kwargs):
        if value is None:
            return store.get(name)
        store[name] = value

    monkeypatch.setattr(pu, "window", fake_window)
    monkeypatch.setattr(pu.client, "get_device_id", lambda: "device")
    item = {
        "Type": "Episode",
        "Id": "ep",
        "PlaybackInfo": {
            "Path": "ep.mkv",
            "AudioStreamIndex": 2,
            "SubtitleStreamIndex": -1,
            "TrackMemoryKey": "user/series",
            "TrackStreams": [1],
        },
    }
    pu.set_properties(item, "DirectStream", "server")
    played = store["jellyfin_play.json"][0]
    assert played["TrackMemoryKey"] == "user/series"
    assert played["TrackStreams"] == [1]
    assert (played["StartAudioStreamIndex"], played["StartSubtitleStreamIndex"]) == (
        2,
        -1,
    )


# player: saving at stop


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(
        player_module, "settings", lambda key, *a: key == "rememberSeriesTracks.bool"
    )
    monkeypatch.setattr(pu, "load_track_memory", lambda path=None: {})
    monkeypatch.setattr(
        pu, "save_track_memory", lambda memory, path=None: calls.append(memory)
    )
    monkeypatch.setattr(pu.time, "time", lambda: NOW)
    return calls


def played_item(audio, subtitle, start_audio=1, start_subtitle=3):
    return {
        "TrackMemoryKey": "user/series",
        "TrackStreams": pu.compact_streams(STREAMS),
        "AudioStreamIndex": audio,
        "SubtitleStreamIndex": subtitle,
        "StartAudioStreamIndex": start_audio,
        "StartSubtitleStreamIndex": start_subtitle,
    }


def test_changed_pick_is_saved(saved):
    player_module.Player._remember_series_tracks(played_item(2, 4))
    entry = saved[0]["user/series"]
    assert entry["Audio"]["Language"] == "jpn"
    assert entry["Subtitle"]["Title"] == "Full"


def test_subtitles_turned_off_are_saved_as_off(saved):
    player_module.Player._remember_series_tracks(played_item(1, None))
    assert saved[0]["user/series"]["Subtitle"] == "off"


def test_unchanged_pick_is_not_saved(saved):
    player_module.Player._remember_series_tracks(played_item(1, 3))
    assert saved == []


def test_off_at_start_and_off_now_is_unchanged(saved):
    player_module.Player._remember_series_tracks(
        played_item(2, None, start_audio=2, start_subtitle=-1)
    )
    assert saved == []


def test_no_key_or_setting_off_saves_nothing(saved, monkeypatch):
    item = played_item(2, 4)
    item["TrackMemoryKey"] = None
    player_module.Player._remember_series_tracks(item)
    monkeypatch.setattr(player_module, "settings", lambda *a: False)
    player_module.Player._remember_series_tracks(played_item(2, 4))
    assert saved == []


def test_unknown_indexes_save_nothing(saved):
    player_module.Player._remember_series_tracks(played_item(77, 88))
    assert saved == []


def test_save_error_does_not_break_stop(monkeypatch, saved):
    def boom(memory, path=None):
        raise RuntimeError("disk")

    monkeypatch.setattr(pu, "save_track_memory", boom)
    player_module.Player._remember_series_tracks(played_item(2, 4))


def test_stop_playback_saves_the_pick(saved, monkeypatch):
    monkeypatch.setattr(player_module, "window", lambda *a, **k: None)
    monkeypatch.setattr(player_module.xbmcvfs, "exists", lambda path: False)
    monkeypatch.setattr(player_module.xbmcvfs, "translatePath", lambda path: path)
    server = Mock()
    server.jellyfin.get_item.return_value = {}
    item = played_item(2, 4)
    item.update(
        {
            "Id": "ep",
            "MediaSourceId": "s",
            "PlaySessionId": "p",
            "PlayMethod": "DirectStream",
            "Runtime": 0,
            "CurrentPosition": 10,
            "Server": server,
        }
    )
    player = object.__new__(player_module.Player)
    player.played = {"f": item}
    player.skip_segments = {}
    player.skip_prompted = set()
    player._reset_segment_checker = Mock()
    player._reset_skip_dialog = Mock()
    player.stop_playback()
    assert "user/series" in saved[0]


def test_av_change_detects_tracks_now():
    item = {}
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "f"
    player.is_playing_file = lambda _f: True
    player.get_file_info = lambda _f: item
    player.detect_audio_subs = Mock()
    player.onAVChange()
    player.detect_audio_subs.assert_called_once_with(item)


def test_av_change_survives_errors():
    player = object.__new__(player_module.Player)

    def gone():
        raise RuntimeError("not playing")

    player.get_playing_file = gone
    player.onAVChange()


@pytest.mark.parametrize("key", ["30702", "30703"])
def test_setting_strings_exist(key):
    import pathlib

    po = (
        pathlib.Path(__file__).parents[1]
        / "resources/language/resource.language.en_gb/strings.po"
    ).read_text(encoding="utf-8")
    assert 'msgctxt "#%s"' % key in po


def test_save_leaves_no_temp_file_and_replaces(tmp_path):
    path = str(tmp_path / "track_memory.json")
    pu.save_track_memory({"a": {"Saved": 1}}, path)
    pu.save_track_memory({"b": {"Saved": 2}}, path)
    assert pu.load_track_memory(path) == {"b": {"Saved": 2}}
    assert [f.name for f in tmp_path.iterdir()] == ["track_memory.json"]


def test_save_failure_keeps_the_old_file(tmp_path, monkeypatch):
    path = str(tmp_path / "track_memory.json")
    pu.save_track_memory({"a": {"Saved": 1}}, path)

    def broken_dump(*a, **k):
        raise IOError("disk full")

    monkeypatch.setattr(pu.json, "dump", broken_dump)
    pu.save_track_memory({"b": {"Saved": 2}}, path)
    assert pu.load_track_memory(path) == {"a": {"Saved": 1}}


def test_match_codec_case_from_review():
    streams = [{"Type": "Audio", "Index": 3, "Language": "eng", "Codec": "eac3"}]
    assert pu.match_stream(streams, {"Language": "eng", "Codec": "EAC3"}, "Audio") == 3
