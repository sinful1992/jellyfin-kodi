# -*- coding: utf-8 -*-
"""#1041: native-mode path replacement. Fixture: a server whose two NAS volumes both hold movies and tv."""
from jellyfin_kodi.helper.api import replace_path

import pytest

OURS = {
    "/data/movies": "nfs://nas.example/vol1/media/movies",
    "/data/tv": "nfs://nas.example/vol1/media/tv",
    "/data/movies2": "nfs://nas.example/vol2/media2/movies",
    "/data/tv2/": "nfs://nas.example/vol2/media2/tv/",
}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/data/movies/Film A (2009)/a.mkv",
         "nfs://nas.example/vol1/media/movies/Film A (2009)/a.mkv"),
        # the bug: "/data/movies" is also the start of "/data/movies2"
        ("/data/movies2/Film B (1951)/b.mkv",
         "nfs://nas.example/vol2/media2/movies/Film B (1951)/b.mkv"),
        ("/data/tv2/Show C/S04E05.mkv",
         "nfs://nas.example/vol2/media2/tv/Show C/S04E05.mkv"),
        ("/data/tv/Show D/S01E01.mkv",
         "nfs://nas.example/vol1/media/tv/Show D/S01E01.mkv"),
        ("/data/movies", "nfs://nas.example/vol1/media/movies"),
        ("/config/task39-test/x.mkv", "/config/task39-test/x.mkv"),  # no match: unchanged
        # a key in the middle of a path is not a prefix
        ("/mnt/data/movies/x.mkv", "/mnt/data/movies/x.mkv"),
    ],
)
def test_our_mounts(path, expected):
    assert replace_path(path, OURS) == expected


def test_dict_order_does_not_matter():
    reordered = dict(reversed(list(OURS.items())))
    p = "/data/movies2/A/a.mkv"
    assert replace_path(p, reordered) == replace_path(p, OURS)


def test_replaced_once_no_chaining():
    rules = {"/a": "/b", "/b": "/c"}
    assert replace_path("/a/x", rules) == "/b/x"


def test_windows_server_path_to_smb():
    rules = {"D:\\Media": "smb://nas/media/"}
    assert replace_path("D:\\Media\\Movies\\Up.mkv", rules) == "smb://nas/media/Movies/Up.mkv"


def test_unc_path_kept_after_get_file_path_conversion():
    # get_file_path turns \\\\nas\\share into smb://nas/share before replacing
    rules = {"smb://nas/share": "nfs://nas/export"}
    assert replace_path("smb://nas/share/tv/x.mkv", rules) == "nfs://nas/export/tv/x.mkv"


def test_local_windows_target():
    rules = {"/data/movies": "M:\\"}
    assert replace_path("/data/movies/Up/Up.mkv", rules) == "M:\\Up\\Up.mkv"


@pytest.mark.parametrize("rules", [{}, None, {"": "nfs://x"}, {"/data": None}])
def test_empty_or_bad_rules_leave_path(rules):
    assert replace_path("/data/movies/x.mkv", rules) == "/data/movies/x.mkv"


def test_empty_path():
    assert replace_path("", OURS) == ""


def test_get_file_path_uses_it(monkeypatch):
    from jellyfin_kodi.helper import api as api_module

    a = api_module.API.__new__(api_module.API)
    a.item = {"Path": "/data/movies2/A/a.mkv"}
    a.path_data = OURS
    assert a.get_file_path() == (
        "nfs://nas.example/vol2/media2/movies/A/a.mkv"
    )


@pytest.mark.parametrize(
    "remote,local",
    [
        ("/data/movies", "nfs://nas/movies"),
        ("/data/movies/", "nfs://nas/movies"),
        ("/data/movies", "nfs://nas/movies/"),
        ("/data/movies/", "nfs://nas/movies/"),
    ],
)
def test_trailing_slashes_in_settings(remote, local):
    """ada #11190: trailing slash on either side; file at the share root; path == prefix."""
    rules = {remote: local}
    assert replace_path("/data/movies/x.mkv", rules) == "nfs://nas/movies/x.mkv"
    assert replace_path("/data/movies", rules) == "nfs://nas/movies"
    assert replace_path("/data/movies2/x.mkv", rules) == "/data/movies2/x.mkv"
