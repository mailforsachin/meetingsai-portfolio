from __future__ import annotations

import httpx
import pytest

from meetingsai_recorder.sync import AuthError, SyncError, VpsClient


def _client(handler) -> VpsClient:
    vc = VpsClient.__new__(VpsClient)
    vc.base_url = "https://test"
    vc._password = "pw"
    vc._timeout = 5.0
    vc._client = httpx.Client(base_url="https://test",
                              transport=httpx.MockTransport(handler))
    vc._logged_in = False
    return vc


def test_login_ok():
    def h(req):
        assert req.url.path == "/api/auth/login"
        return httpx.Response(200, json={"ok": True})
    vc = _client(h)
    vc.login()
    assert vc._logged_in is True


def test_login_rejected():
    def h(req):
        return httpx.Response(401, json={"detail": "invalid credentials"})
    vc = _client(h)
    with pytest.raises(AuthError):
        vc.login()


def test_push_batch_ok_after_login():
    calls = {"n": 0}
    def h(req):
        calls["n"] += 1
        if req.url.path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True})
        if req.url.path.endswith("/transcript"):
            return httpx.Response(200, json={"accepted": 2, "duplicates": 0})
        return httpx.Response(404)
    vc = _client(h)
    vc.login()
    res = vc.push_batch("m1", [{"segment_id": "a", "text": "hi"}])
    assert res.accepted == 2 and res.duplicates == 0


def test_push_batch_relogins_on_401():
    state = {"authed": False}
    def h(req):
        if req.url.path == "/api/auth/login":
            state["authed"] = True
            return httpx.Response(200, json={"ok": True})
        if req.url.path.endswith("/transcript"):
            if state["authed"]:
                return httpx.Response(200, json={"accepted": 1, "duplicates": 0})
            return httpx.Response(401)
        return httpx.Response(404)
    vc = _client(h)
    vc._logged_in = True  # claim we are logged in but server disagrees
    res = vc.push_batch("m1", [{"segment_id": "a", "text": "hi"}])
    assert res.accepted == 1


def test_push_batch_meeting_not_found():
    def h(req):
        if req.url.path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True})
        if req.url.path.endswith("/transcript"):
            return httpx.Response(404, json={"detail": "meeting not found"})
        return httpx.Response(404)
    vc = _client(h)
    vc.login()
    with pytest.raises(SyncError):
        vc.push_batch("no-such", [{"segment_id": "a", "text": "hi"}])


def test_push_batch_empty_is_noop():
    def h(req):
        return httpx.Response(500)
    vc = _client(h)
    res = vc.push_batch("m1", [])
    assert res.accepted == 0 and res.duplicates == 0
