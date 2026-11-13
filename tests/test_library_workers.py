# -*- coding: utf-8 -*-
"""K15: sort and notify workers were never pruned/recorded; views cleared the wrong window properties."""
from unittest.mock import Mock

import jellyfin_kodi.library as library_module
import jellyfin_kodi.views as views_module

import pytest


class FakeWorker:
    started = 0

    def __init__(self, *args):
        self.is_done = False

    def start(self):
        FakeWorker.started += 1


@pytest.fixture
def lib(monkeypatch):
    FakeWorker.started = 0
    monkeypatch.setattr(library_module, "SortWorker", FakeWorker)
    monkeypatch.setattr(library_module, "NotifyWorker", FakeWorker)
    lib = object.__new__(library_module.Library)
    lib.download_threads = []
    lib.writer_threads = {"updated": [], "userdata": [], "removed": []}
    lib.jellyfin_threads = []
    lib.notify_threads = []
    lib.removed_queue = Mock(qsize=Mock(return_value=3))
    lib.removed_output = Mock()
    lib.notify_output = Mock(qsize=Mock(return_value=1))
    lib.player = Mock()
    return lib


def test_sort_workers_are_capped_at_two(lib):
    for _ in range(10):  # service() runs every 2 s while items wait
        lib.worker_sort()
    assert FakeWorker.started == 2


def test_finished_sort_worker_is_replaced(lib):
    lib.worker_sort()
    lib.worker_sort()
    for thread in lib.jellyfin_threads:
        thread.is_done = True
    lib._prune_threads()
    lib.worker_sort()
    assert FakeWorker.started == 3
    assert len(lib.jellyfin_threads) == 1


def test_notify_worker_restarts_after_finishing(lib):
    lib.worker_notify()
    lib.notify_threads[0].is_done = True
    lib._prune_threads()
    lib.worker_notify()
    assert FakeWorker.started == 2


def test_running_notify_worker_is_kept(lib):
    lib.worker_notify()
    lib._prune_threads()
    lib.worker_notify()
    assert FakeWorker.started == 1


def test_window_clear_clears_type_and_the_named_nodes(monkeypatch):
    calls = []

    def fake_window(key, value=None, clear=False):
        if clear:
            calls.append(key)
            return None
        return "2" if key.endswith(".total") else None

    monkeypatch.setattr(views_module, "window", fake_window)
    views = object.__new__(views_module.Views)
    views.window_clear("Jellyfin.wnodes")

    assert "Jellyfin.wnodes.0.type" in calls
    assert "Jellyfin.wnodes.1.inprogress.content" in calls
    assert not any(c.startswith("Jellyfin.nodes.") for c in calls)
    assert not any("typeinprogress" in c for c in calls)


def test_window_clear_default_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        views_module,
        "window",
        lambda key, value=None, clear=False: calls.append(key) if clear else "1",
    )
    object.__new__(views_module.Views).window_clear()
    assert "Jellyfin.nodes.0.type" in calls
