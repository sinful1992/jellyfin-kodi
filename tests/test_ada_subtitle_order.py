"""K13: subtitle choice maps Jellyfin indexes to Kodi's list by an explicit table,
not by assuming one video stream followed by the audio streams."""

from unittest.mock import Mock

import jellyfin_kodi.player as player_module
from jellyfin_kodi.helper.playutils import embedded_subtitle_indexes

# Video 0, cover image 1, audio 2-3, subtitles 4-5: the old arithmetic
# (subtitle - audio_count - 1) is off by one for this layout.
COVER_LAYOUT = [
    {"Index": 0, "Type": "Video"},
    {"Index": 1, "Type": "EmbeddedImage"},
    {"Index": 2, "Type": "Audio"},
    {"Index": 3, "Type": "Audio"},
    {"Index": 4, "Type": "Subtitle"},
    {"Index": 5, "Type": "Subtitle"},
]


def test_embedded_table_skips_sidecars_and_sorts():
    streams = [
        {"Index": 6, "Type": "Subtitle"},
        {"Index": 0, "Type": "Subtitle", "IsExternal": True},
        {"Index": 1, "Type": "Video"},
        {"Index": 5, "Type": "Subtitle", "IsExternal": False},
    ]
    assert embedded_subtitle_indexes(streams) == [5, 6]
    assert embedded_subtitle_indexes(None) == []


def make_player(item, audio_count=2):
    player = object.__new__(player_module.Player)
    player.get_playing_file = lambda: "f"
    player.is_playing_file = lambda _f: True
    player.get_file_info = lambda _f: item
    player.getAvailableAudioStreams = lambda: [None] * audio_count
    player.getAvailableSubtitleStreams = lambda: [1, 2, 3]
    player.setAudioStream = Mock()
    player.setSubtitleStream = Mock()
    player.showSubtitles = Mock()
    return player


def item(mapping=None, embedded=None):
    return {
        "SubsMapping": mapping or {},
        "KodiAudioStreamIndexes": [2, 3],
        "KodiEmbeddedSubtitleIndexes": embedded,
    }


def test_set_subtitle_with_cover_stream():
    player = make_player(item(embedded=embedded_subtitle_indexes(COVER_LAYOUT)))
    player.set_audio_subs(None, 5)
    player.setSubtitleStream.assert_called_once_with(1)


def test_set_subtitle_after_external_ones():
    player = make_player(item(mapping={0: 9}, embedded=[4, 5]))
    player.set_audio_subs(None, 5)
    player.setSubtitleStream.assert_called_once_with(2)


def test_set_external_subtitle_by_mapping():
    player = make_player(item(mapping={0: 9, 1: 10}, embedded=[4, 5]))
    player.set_audio_subs(None, 10)
    player.setSubtitleStream.assert_called_once_with(1)


def test_set_subtitle_without_table_keeps_old_rule():
    # Transcodes and older play items carry no table.
    player = make_player(item(embedded=None))
    player.set_audio_subs(None, 4)
    player.setSubtitleStream.assert_called_once_with(1)


def detect(player_item, kodi_ordinal):
    player = make_player(player_item)

    class JSONRPC:
        def __init__(self, _):
            pass

        def execute(self, _):
            return {
                "result": {
                    "currentaudiostream": {"index": 0},
                    "currentsubtitle": {"index": kodi_ordinal},
                    "subtitleenabled": True,
                }
            }

    original = player_module.JSONRPC
    player_module.JSONRPC = JSONRPC
    try:
        player.detect_audio_subs(player_item)
    finally:
        player_module.JSONRPC = original
    return player_item["SubtitleStreamIndex"]


def test_detect_subtitle_with_cover_stream():
    assert detect(item(embedded=[4, 5]), 1) == 5


def test_detect_subtitle_after_external_ones():
    assert detect(item(mapping={"0": 9}, embedded=[4, 5]), 2) == 5
    assert detect(item(mapping={"0": 9}, embedded=[4, 5]), 0) == 9


def test_detect_out_of_table_falls_back_to_old_rule():
    assert detect(item(embedded=[4]), 3) == 3 + 2 + 1


def test_set_and_detect_round_trip():
    table = embedded_subtitle_indexes(COVER_LAYOUT)
    for jellyfin_index in table:
        player = make_player(item(embedded=table))
        player.set_audio_subs(None, jellyfin_index)
        ordinal = player.setSubtitleStream.call_args[0][0]
        assert detect(item(embedded=table), ordinal) == jellyfin_index
