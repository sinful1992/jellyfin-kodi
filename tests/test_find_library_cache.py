# -*- coding: utf-8 -*-
"""K5 (upstream #848): one Ancestors call per series/folder, not per item."""
from unittest.mock import Mock

import jellyfin_kodi.database as database_module
import jellyfin_kodi.helper.utils as utils

import pytest

SHOWS = {"Id": "lib-shows", "Name": "Shows"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    getattr(utils, "_LIBRARY_CACHE", {}).clear()
    sync = {"Whitelist": ["lib-shows", "Mixed:lib-mixed"], "Libraries": []}
    monkeypatch.setattr(database_module, "get_sync", Mock(return_value=sync))
    return sync


def server_with(*ancestor_lists):
    server = Mock()
    server.jellyfin.get_ancestors.side_effect = list(ancestor_lists) * 50
    return server


def episodes(series, n):
    return [{"Id": "%s-e%d" % (series, i), "SeriesId": series} for i in range(n)]


def test_one_call_per_series():
    server = server_with([{"Id": "season"}, {"Id": "series"}, SHOWS])
    for item in episodes("friends", 20):
        assert utils.find_library(server, item) == SHOWS
    assert server.jellyfin.get_ancestors.call_count == 1


def test_two_series_two_calls():
    server = server_with([SHOWS])
    for item in episodes("a", 5) + episodes("b", 5):
        utils.find_library(server, item)
    assert server.jellyfin.get_ancestors.call_count == 2


def test_mixed_library_prefix_still_matches():
    mixed = {"Id": "lib-mixed"}
    server = server_with([mixed])
    assert utils.find_library(server, {"Id": "m1", "ParentId": "folder"}) == mixed


def test_item_without_series_or_parent_is_not_cached():
    server = server_with([SHOWS])
    utils.find_library(server, {"Id": "x"})
    utils.find_library(server, {"Id": "x"})
    assert server.jellyfin.get_ancestors.call_count == 2


def test_not_found_is_not_cached_and_returns_empty():
    server = server_with([{"Id": "other"}], [SHOWS])
    item = {"Id": "e1", "SeriesId": "s"}
    assert utils.find_library(server, item) == {}
    assert utils.find_library(server, item) == SHOWS  # library added later is found


def test_whitelist_change_invalidates(fresh):
    server = server_with([SHOWS])
    item = {"Id": "e1", "SeriesId": "s"}
    utils.find_library(server, item)
    fresh["Whitelist"] = ["lib-shows", "lib-new"]
    utils.find_library(server, item)
    assert server.jellyfin.get_ancestors.call_count == 2


def test_removed_library_is_not_served_from_cache(fresh):
    server = server_with([SHOWS])
    item = {"Id": "e1", "SeriesId": "s"}
    assert utils.find_library(server, item) == SHOWS
    fresh["Whitelist"] = []
    assert utils.find_library(server, item) == {}


def test_none_ancestors_answer():
    server = Mock()
    server.jellyfin.get_ancestors.return_value = None  # http.py returns None on a 500
    assert utils.find_library(server, {"Id": "e1", "SeriesId": "s"}) == {}


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(utils, "_LIBRARY_CACHE_MAX", 3)
    server = server_with([SHOWS])
    for i in range(10):
        utils.find_library(server, {"Id": "e%d" % i, "SeriesId": "s%d" % i})
    assert len(utils._LIBRARY_CACHE) <= 3
