# -*- coding: utf-8 -*-
"""Pick the episode "Play series" should start: works for a show in progress,
a show never started, and a fully watched show being rewatched (where the
server's Next Up and Kodi's in-progress list are both empty)."""

from urllib.parse import urlencode

PLUGIN_URL = "plugin://plugin.video.jellyfin/"


def _user_data(episode):
    return episode.get("UserData") or {}


def _order(episode):
    return (episode.get("ParentIndexNumber") or 0, episode.get("IndexNumber") or 0)


def _regular_sorted(episodes):
    episodes = [e for e in episodes or [] if e.get("Type", "Episode") == "Episode"]
    regular = [e for e in episodes if (e.get("ParentIndexNumber") or 0) != 0]
    return sorted(regular or episodes, key=_order)


def _last_played(episode):
    return _user_data(episode).get("LastPlayedDate") or ""


def pick_series_episode(next_up, episodes):
    """Return (episode, resume_seconds or None), or (None, None) when there is nothing.

    Order: the unfinished episode watched most recently, resumed, but only when
    nothing in the show was watched after it (an episode abandoned in January
    does not beat episodes finished in March); else the server's Next Up; else
    the episode after the most recently played one (rewatch); else the first
    unplayed episode; else the first episode. Specials (season 0) are skipped
    unless the show has nothing else.
    """
    episodes = _regular_sorted(episodes)

    resumable = [
        e
        for e in episodes
        if _user_data(e).get("PlaybackPositionTicks")
        and not _user_data(e).get("Played")
    ]
    touched = [
        e
        for e in episodes
        if _user_data(e).get("Played") or _user_data(e).get("PlaybackPositionTicks")
    ]
    if resumable:
        episode = max(resumable, key=_last_played)
        if _last_played(episode) >= max(_last_played(e) for e in touched):
            return episode, _user_data(episode)["PlaybackPositionTicks"] / 10000000.0

    if next_up:
        return next_up[0], None

    if not episodes:
        return None, None

    played = [i for i, e in enumerate(episodes) if _user_data(e).get("Played")]
    if played:
        latest = max(played, key=lambda i: _last_played(episodes[i]))
        if latest + 1 < len(episodes):
            return episodes[latest + 1], None

    for episode in episodes:
        if not _user_data(episode).get("Played"):
            return episode, None

    return episodes[0], None


def series_queue(episodes, picked, limit=50):
    """The picked episode followed by the rest of the series in order, so playback
    (and a credits skip) carries on to the next episode. A picked episode that is
    not in the list (a special from Next Up) is queued alone."""
    ordered = _regular_sorted(episodes)
    ids = [e.get("Id") for e in ordered]
    if picked.get("Id") not in ids:
        return [picked]
    start = ids.index(picked.get("Id"))
    return ordered[start : start + limit]


def play_url(item_id, server_id=None, resume_seconds=None):
    """The add-on's own play URL for an episode, resumed when resume_seconds is set."""
    params = {"mode": "play", "id": item_id}
    if server_id:
        params["server"] = server_id
    if resume_seconds:
        params["timestamp"] = "%.3f" % resume_seconds
    return PLUGIN_URL + "?" + urlencode(params)
