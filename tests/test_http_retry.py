# -*- coding: utf-8 -*-
"""A retried request must keep its HTTP method (a retried POST used to become a GET)."""
from unittest.mock import Mock

import requests

import jellyfin_kodi.jellyfin.http as http_module
from jellyfin_kodi.jellyfin.http import HTTP

import pytest


@pytest.fixture
def http(monkeypatch):
    client = Mock()
    client.config.data = {
        "auth.server": "http://server",
        "auth.server-id": "sid",
        "http.timeout": 5,
        "app.name": "Kodi",
        "app.version": "1",
        "app.device_name": "test",
        "app.device_id": "dev",
        "auth.token": "tok",
        "auth.user_id": "uid",
        "http.user_agent": None,
    }
    h = HTTP(client)
    h.session = None
    h.keep_alive = False
    monkeypatch.setattr(http_module.time, "sleep", Mock())
    monkeypatch.setattr(h, "_get_header", Mock())
    return h


def ok_response():
    r = Mock(status_code=204, content=b"", headers={})
    r.raise_for_status = Mock()
    return r


@pytest.mark.parametrize("method", ["POST", "DELETE", "GET"])
@pytest.mark.parametrize(
    "error", [requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout]
)
def test_retry_keeps_method(http, monkeypatch, method, error):
    calls = []

    def fake_requests(session, action, **kwargs):
        calls.append(action)
        if len(calls) == 1:
            raise error("first try fails")
        return ok_response()

    monkeypatch.setattr(http, "_requests", fake_requests)

    http.request({"type": method, "handler": "Sessions/Playing/Stopped", "json": {}})

    assert calls == [method, method]


def test_502_retry_keeps_post(http, monkeypatch):
    calls = []
    bad = Mock(status_code=502, content=b"", headers={})
    bad.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError("502"))

    def fake_requests(session, action, **kwargs):
        calls.append(action)
        return bad if len(calls) == 1 else ok_response()

    monkeypatch.setattr(http, "_requests", fake_requests)

    http.request({"type": "POST", "handler": "Items/x/Refresh"})

    assert calls == ["POST", "POST"]


def test_no_type_defaults_to_get(http, monkeypatch):
    calls = []
    monkeypatch.setattr(
        http, "_requests", lambda s, a, **k: calls.append(a) or ok_response()
    )

    http.request({"handler": "System/Info"})

    assert calls == ["GET"]
