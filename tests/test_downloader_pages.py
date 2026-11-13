# -*- coding: utf-8 -*-
"""K15: an empty page in the paged item download must not end the full sync."""
import jellyfin_kodi.downloader as downloader

import pytest


@pytest.fixture
def pages(monkeypatch):
    limits = {"limitIndex": "2", "limitThreads": "1"}
    monkeypatch.setattr(downloader, "settings", limits.get)
    answers = {}

    def fake_get(url, params, server_id=None):
        if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
            return {"TotalRecordCount": answers["total"]}
        return answers["pages"].get(params["StartIndex"])

    monkeypatch.setattr(downloader, "_get", fake_get)
    return answers


def run(query):
    gen = downloader._get_items.__wrapped__(query)  # skip the @stop online check
    out = []
    for batch in gen:
        out.extend(dict(i) for i in batch["Items"])
    return out


def item(i, year=2000):
    return {"Id": str(i), "ProductionYear": year}


def test_empty_last_page_does_not_raise(pages):
    pages["total"] = 4
    pages["pages"] = {0: {"Items": [item(0), item(1)]}, 2: {"Items": []}}
    assert [i["Id"] for i in run({"url": "x"})] == ["0", "1"]


def test_none_page_from_500_does_not_raise(pages):
    pages["total"] = 4
    pages["pages"] = {0: None, 2: {"Items": [item(2), item(3)]}}
    assert [i["Id"] for i in run({"url": "x"})] == ["2", "3"]


def test_bad_year_fixed_on_every_item_not_only_the_first(pages):
    pages["total"] = 2
    pages["pages"] = {0: {"Items": [item(0, 2001), item(1, 99999)]}}
    out = run({"url": "x"})
    assert out[0]["ProductionYear"] == 2001
    assert out[1]["ProductionYear"] is None


def test_non_int_year_is_cleared(pages):
    pages["total"] = 1
    pages["pages"] = {0: {"Items": [item(0, "abc")]}}
    assert run({"url": "x"})[0]["ProductionYear"] is None
