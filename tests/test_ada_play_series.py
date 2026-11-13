"""K6: "Play series" starts the right episode, also for a rewatch or a new show."""

from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from jellyfin_kodi.entrypoint import context as context_module
from jellyfin_kodi.entrypoint.playseries import (
    pick_series_episode,
    play_url,
    series_queue,
)


def ep(season, number, played=False, last=None, position=0, kind="Episode"):
    return {
        "Id": "s%02de%02d" % (season, number),
        "Type": kind,
        "ParentIndexNumber": season,
        "IndexNumber": number,
        "UserData": {
            "Played": played,
            "LastPlayedDate": last,
            "PlaybackPositionTicks": position,
        },
    }


def pick_id(next_up, episodes):
    episode, resume = pick_series_episode(next_up, episodes)
    return (episode["Id"] if episode else None), resume


def test_resumable_episode_wins_and_resumes():
    episodes = [
        ep(1, 1, True, "2026-01-01"),
        ep(1, 2, False, "2026-01-02", position=6000000000),
    ]
    assert pick_id([ep(1, 3)], episodes) == ("s01e02", 600.0)


def test_most_recent_resumable_of_several():
    episodes = [
        ep(1, 1, False, "2026-01-05", position=10000000),
        ep(1, 2, False, "2026-02-01", position=20000000),
    ]
    assert pick_id([], episodes) == ("s01e02", 2.0)


def test_next_up_used_when_nothing_to_resume():
    assert pick_id([ep(2, 4)], [ep(1, 1, True, "2026-01-01")]) == ("s02e04", None)


def test_rewatch_continues_after_the_latest_played():
    # Fully watched, rewatching: S01E01-E02 seen again recently, the rest long ago.
    episodes = [
        ep(1, 1, True, "2026-11-01"),
        ep(1, 2, True, "2026-11-02"),
        ep(1, 3, True, "2025-01-03"),
        ep(1, 4, True, "2025-01-04"),
    ]
    assert pick_id([], episodes) == ("s01e03", None)


def test_finale_watched_last_starts_again_at_the_first():
    episodes = [ep(1, 1, True, "2025-01-01"), ep(1, 2, True, "2026-01-01")]
    assert pick_id([], episodes) == ("s01e01", None)


def test_never_started_show_starts_at_first_episode():
    assert pick_id([], [ep(1, 2), ep(1, 1), ep(2, 1)]) == ("s01e01", None)


def test_first_unplayed_when_played_has_no_dates():
    episodes = [ep(1, 1, True), ep(1, 2), ep(1, 3)]
    assert pick_id([], episodes) == ("s01e02", None)


def test_specials_are_skipped():
    episodes = [ep(0, 1), ep(1, 1, True, "2026-01-01"), ep(1, 2)]
    assert pick_id([], episodes) == ("s01e02", None)


def test_only_specials_still_plays():
    assert pick_id([], [ep(0, 2), ep(0, 1)]) == ("s00e01", None)


def test_non_episodes_are_ignored():
    assert pick_id([], [ep(1, 1, kind="Trailer"), ep(1, 2)]) == ("s01e02", None)


def test_empty_show():
    assert pick_id([], []) == (None, None)
    assert pick_id(None, None) == (None, None)


def test_missing_user_data_and_numbers():
    assert pick_id([], [{"Id": "x", "Type": "Episode"}]) == ("x", None)


def test_play_url():
    url = urlparse(play_url("ep1", "srv", 61.5))
    params = parse_qs(url.query)
    assert url.scheme == "plugin" and url.netloc == "plugin.video.jellyfin"
    assert params == {
        "mode": ["play"],
        "id": ["ep1"],
        "server": ["srv"],
        "timestamp": ["61.500"],
    }
    assert parse_qs(urlparse(play_url("ep1")).query) == {
        "mode": ["play"],
        "id": ["ep1"],
    }


# context menu


@pytest.fixture(autouse=True)
def distinct_labels(monkeypatch):
    # Kodistubs translate() returns "", which would make every option equal.
    for key in list(context_module.OPTIONS):
        monkeypatch.setitem(context_module.OPTIONS, key, key)


def make_context(item_type, monkeypatch, next_up=None, episodes=None, series_id="show"):
    ctx = object.__new__(context_module.Context)
    ctx.item = {"Id": "sel", "Type": item_type, "UserData": {}}
    ctx.server_id = None
    ctx.api_client = Mock()
    ctx.api_client.get_item.return_value = {"SeriesId": series_id}

    def shows(handler, params):
        if handler == "/NextUp":
            assert params["SeriesId"] == (series_id if item_type == "Season" else "sel")
            return {"Items": next_up or []}
        return {"Items": episodes or []}

    ctx.api_client.shows.side_effect = shows
    played = []

    class FakePlaylist:
        def __init__(self, kind):
            assert kind == context_module.xbmc.PLAYLIST_VIDEO

        def clear(self):
            played.clear()

        def add(self, url, listitem):
            played.append(url)

    class FakePlayer:
        def play(self, playlist):
            played.append("PLAY")

    monkeypatch.setattr(context_module.xbmc, "PlayList", FakePlaylist)
    monkeypatch.setattr(context_module.xbmc, "Player", FakePlayer)
    return ctx, played


def test_play_series_from_series(monkeypatch):
    ctx, played = make_context(
        "Series", monkeypatch, episodes=[ep(1, 1, True, "2026-01-01"), ep(1, 2)]
    )
    ctx.play_series()
    assert played == [play_url("s01e02"), "PLAY"]


def test_play_series_from_season_uses_its_series(monkeypatch):
    ctx, played = make_context("Season", monkeypatch, next_up=[ep(3, 1)])
    ctx.play_series()
    assert played == [play_url("s03e01"), "PLAY"]


def test_play_series_with_no_episodes_does_nothing(monkeypatch):
    ctx, played = make_context("Series", monkeypatch)
    ctx.play_series()
    assert played == []


def test_play_series_season_without_series_does_nothing(monkeypatch):
    ctx, played = make_context("Season", monkeypatch, series_id=None)
    ctx.play_series()
    assert played == [] and not ctx.api_client.shows.called


@pytest.mark.parametrize(
    "item_type,shown",
    [("Series", True), ("Season", True), ("Movie", False), ("Episode", False)],
)
def test_menu_offers_play_series_for_shows_only(monkeypatch, item_type, shown):
    captured = {}

    class Menu:
        def __init__(self, *a):
            pass

        def set_options(self, options):
            captured["options"] = options

        def doModal(self):
            pass

        def is_selected(self):
            return False

    monkeypatch.setattr(context_module.context, "ContextMenu", Menu)
    monkeypatch.setattr(context_module, "settings", lambda *a: False)
    ctx = object.__new__(context_module.Context)
    ctx.item = {"Id": "x", "Type": item_type, "UserData": {}}
    ctx.select_menu()
    assert (context_module.OPTIONS["PlaySeries"] in captured["options"]) is shown


def test_action_menu_runs_play_series(monkeypatch):
    ctx = object.__new__(context_module.Context)
    ctx._selected_option = context_module.OPTIONS["PlaySeries"]
    ctx.play_series = Mock()
    ctx.action_menu()
    ctx.play_series.assert_called_once_with()


def test_play_series_queues_the_rest_and_resumes_only_the_first(monkeypatch):
    episodes = [
        ep(1, 1, True, "2026-01-01"),
        ep(1, 2, False, "2026-01-02", position=6000000000),
        ep(1, 3),
        ep(1, 4),
    ]
    ctx, played = make_context("Series", monkeypatch, episodes=episodes)
    ctx.play_series()
    assert played == [
        play_url("s01e02", None, 600.0),
        play_url("s01e03"),
        play_url("s01e04"),
        "PLAY",
    ]


def test_queue_order_limit_and_specials():
    episodes = [ep(2, 1), ep(0, 1), ep(1, 2), ep(1, 1), ep(1, 3)]
    assert [e["Id"] for e in series_queue(episodes, ep(1, 2))] == [
        "s01e02",
        "s01e03",
        "s02e01",
    ]
    assert [e["Id"] for e in series_queue(episodes, ep(1, 1), limit=2)] == [
        "s01e01",
        "s01e02",
    ]


def test_queue_for_episode_not_in_list():
    special = ep(0, 5)
    assert series_queue([ep(1, 1)], special) == [special]


# linus review #11194 / jellyfin/task151/kodi/linus-review-11192.py


def test_old_abandoned_episode_does_not_beat_later_progress():
    eps = [
        ep(1, 1, True, "2026-01-01T00:00:00Z"),
        ep(1, 2, False, "2026-01-02T00:00:00Z", position=3000000000),
    ]
    eps += [ep(1, i, True, "2026-03-0%dT00:00:00Z" % i) for i in (3, 4, 5)] + [
        ep(1, 6),
        ep(1, 7),
    ]
    assert pick_id([ep(1, 6)], eps) == ("s01e06", None)
    # Without Next Up the same rule gives the episode after the latest played.
    assert pick_id([], eps) == ("s01e06", None)


def test_rewatch_in_progress_resumes():
    eps = [
        ep(1, 1, True, "2026-01-01T00:00:00Z"),
        ep(1, 2, False, "2026-11-01T00:00:00Z", position=6000000000),
        ep(1, 3, True, "2026-01-03T00:00:00Z"),
    ]
    assert pick_id([], eps) == ("s01e02", 600.0)
