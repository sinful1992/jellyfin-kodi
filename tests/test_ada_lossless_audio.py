"""K4: prefer a lossless track (TrueHD / DTS-HD MA) in the default track's language."""

from unittest.mock import Mock

import pytest

from jellyfin_kodi.helper import playutils as playutils_module
from jellyfin_kodi.helper.playutils import pick_audio_stream


def audio(index, codec, channels, language="eng", profile=None, title=None):
    return {
        "Index": index,
        "Type": "Audio",
        "Codec": codec,
        "Channels": channels,
        "Language": language,
        "Profile": profile,
        "Title": title,
    }


VIDEO = {"Index": 0, "Type": "Video", "Codec": "hevc"}


def test_truehd_beats_default_ac3():
    streams = [
        VIDEO,
        audio(1, "ac3", 6),
        audio(2, "truehd", 8),
        audio(3, "truehd", 8, "fra"),
    ]
    assert pick_audio_stream(streams, 1) == 2


def test_dts_hd_ma_beats_default_dts_core():
    streams = [
        VIDEO,
        audio(1, "dts", 6, profile="DTS"),
        audio(2, "dts", 8, profile="DTS-HD MA"),
    ]
    assert pick_audio_stream(streams, 1) == 2


def test_other_language_lossless_is_not_chosen():
    streams = [VIDEO, audio(1, "ac3", 6), audio(2, "truehd", 8, "jpn")]
    assert pick_audio_stream(streams, 1) == 1


def test_commentary_is_not_chosen():
    streams = [
        VIDEO,
        audio(1, "ac3", 6),
        audio(2, "flac", 2, title="Director's Commentary"),
    ]
    assert pick_audio_stream(streams, 1) == 1


def test_default_already_lossless_stays():
    streams = [VIDEO, audio(1, "truehd", 8), audio(2, "dts", 8, profile="DTS-HD MA")]
    assert pick_audio_stream(streams, 1) == 1


def test_same_tier_does_not_switch():
    # E-AC3 5.1 default vs DTS core 5.1: same tier, keep the server's choice.
    streams = [VIDEO, audio(1, "eac3", 6), audio(2, "dts", 6, profile="DTS")]
    assert pick_audio_stream(streams, 1) == 1


def test_more_channels_win_within_best_tier():
    streams = [VIDEO, audio(1, "ac3", 6), audio(2, "truehd", 6), audio(3, "truehd", 8)]
    assert pick_audio_stream(streams, 1) == 3


def test_missing_language_matches_missing_language():
    streams = [VIDEO, audio(1, "ac3", 6, None), audio(2, "truehd", 8, None)]
    assert pick_audio_stream(streams, 1) == 2


def test_unknown_default_index_is_returned_unchanged():
    assert pick_audio_stream([VIDEO, audio(1, "ac3", 6)], 7) == 7
    assert pick_audio_stream([], None) is None
    assert pick_audio_stream(None, 3) == 3


def test_missing_fields_do_not_crash():
    streams = [
        {"Index": 1, "Type": "Audio"},
        {"Index": 2, "Type": "Audio", "Codec": "TrueHD"},
    ]
    assert pick_audio_stream(streams, 1) == 2


# PlayUtils.get wiring


def run_get(
    monkeypatch,
    prefer=True,
    audio_arg=None,
    transcode=False,
    passthrough=("truehd", "dtshd"),
):
    monkeypatch.setattr(playutils_module, "window", lambda *a, **k: None)
    monkeypatch.setattr(playutils_module.client, "get_device_id", lambda: "device")
    monkeypatch.setattr(
        playutils_module,
        "settings",
        lambda key, *a: prefer if key == "preferLosslessAudio.bool" else False,
    )

    class Api:
        def get_file_path(self, _):
            return "movie"

    monkeypatch.setattr(playutils_module.api, "API", lambda *a, **k: Api())
    monkeypatch.setattr(
        playutils_module, "kodi_lossless_passthrough", lambda: set(passthrough)
    )

    api_client = Mock()
    api_client.config.data = {"auth.token": ""}
    item = {"Id": "film", "Type": "Movie", "MediaType": "Video"}
    source = {
        "Id": "source",
        "Path": "film.mkv",
        "SupportsDirectPlay": False,
        "SupportsDirectStream": not transcode,
        "MediaStreams": [VIDEO, audio(1, "ac3", 6), audio(2, "truehd", 8)],
        "DefaultAudioStreamIndex": 1,
        "DefaultSubtitleStreamIndex": None,
    }
    play_utils = playutils_module.PlayUtils(
        item, server="http://server", api_client=api_client
    )
    play_utils.info["Path"] = "film.mkv"

    def fake_direct_url(_source):
        play_utils.info["Method"] = "DirectStream"

    def fake_transcode(_source, *_a):
        play_utils.info["Method"] = "Transcode"

    play_utils.direct_url = fake_direct_url
    play_utils.transcode = fake_transcode
    play_utils.get(source, audio=audio_arg)
    return item["PlaybackInfo"]["AudioStreamIndex"]


def test_get_picks_truehd_on_direct_stream(monkeypatch):
    assert run_get(monkeypatch) == 2


def test_get_keeps_default_when_setting_off(monkeypatch):
    assert run_get(monkeypatch, prefer=False) == 1


def test_get_leaves_transcode_alone(monkeypatch):
    assert run_get(monkeypatch, transcode=True) == 1


def test_get_respects_explicit_audio_choice(monkeypatch):
    assert run_get(monkeypatch, audio_arg=1) == 1


@pytest.mark.parametrize("key", ["30700", "30701"])
def test_setting_strings_exist(key):
    import pathlib

    po = (
        pathlib.Path(__file__).parents[1]
        / "resources/language/resource.language.en_gb/strings.po"
    ).read_text(encoding="utf-8")
    assert 'msgctxt "#%s"' % key in po


def test_no_passthrough_keeps_the_default():
    # TrueHD would be decoded to PCM; over ARC that becomes stereo.
    streams = [VIDEO, audio(1, "eac3", 6), audio(2, "truehd", 8)]
    assert pick_audio_stream(streams, 1, set()) == 1


def test_only_formats_with_passthrough_on_count():
    streams = [
        VIDEO,
        audio(1, "ac3", 6),
        audio(2, "truehd", 8),
        audio(3, "dts", 8, profile="DTS-HD MA"),
    ]
    assert pick_audio_stream(streams, 1, {"dtshd"}) == 3
    assert pick_audio_stream(streams, 1, {"truehd"}) == 2


def test_flac_is_not_an_upgrade():
    streams = [VIDEO, audio(1, "ac3", 6), audio(2, "flac", 8)]
    assert pick_audio_stream(streams, 1) == 1


def test_get_without_passthrough_keeps_default(monkeypatch):
    assert run_get(monkeypatch, passthrough=set()) == 1


class FakeRpc:
    values = {}

    def __init__(self, method):
        assert method == "Settings.GetSettingValue"

    def execute(self, params):
        value = self.values.get(params["setting"])
        if isinstance(value, Exception):
            raise value
        return {"result": {"value": value}}


@pytest.mark.parametrize(
    "values,expected",
    [
        (
            {"audiooutput.passthrough": False, "audiooutput.truehdpassthrough": True},
            set(),
        ),
        (
            {
                "audiooutput.passthrough": True,
                "audiooutput.truehdpassthrough": True,
                "audiooutput.dtshdpassthrough": False,
            },
            {"truehd"},
        ),
        (
            {
                "audiooutput.passthrough": True,
                "audiooutput.truehdpassthrough": True,
                "audiooutput.dtshdpassthrough": True,
            },
            {"truehd", "dtshd"},
        ),
        ({"audiooutput.passthrough": RuntimeError("no rpc")}, set()),
    ],
)
def test_kodi_lossless_passthrough_reads_settings(monkeypatch, values, expected):
    import jellyfin_kodi.helper as helper

    FakeRpc.values = values
    monkeypatch.setattr(helper, "JSONRPC", FakeRpc)
    assert playutils_module.kodi_lossless_passthrough() == expected
