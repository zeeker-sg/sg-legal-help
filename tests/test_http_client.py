"""PoliteClient: httpx transport + tenacity retry + circuit breaker, offline.

Exercises httpx (Client, MockTransport, Limits, Response.raise_for_status,
redirect following) and tenacity (Retrying, stop_after_attempt, wait_none,
retry_if_exception_type, reraise).
"""

from __future__ import annotations

import httpx
import pytest

import http_client
from http_client import CircuitBreakerOpen, PoliteClient


def client_for(handler, **kwargs) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), **kwargs)


def test_success_sends_honest_user_agent_and_params():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="<p>ok</p>")

    with client_for(handler) as c:
        resp = c.get("https://example.test/page", params={"a": "1"}, headers={"X-T": "y"})
    assert resp.text == "<p>ok</p>"
    req = seen[0]
    assert req.headers["user-agent"] == http_client.USER_AGENT
    assert "text/html" in req.headers["accept"]
    assert req.headers["x-t"] == "y"
    assert req.url.params["a"] == "1"


def test_5xx_then_200_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="finally")

    with client_for(handler) as c:
        resp = c.get("https://example.test/")
    assert resp.text == "finally"
    assert len(calls) == 3  # MAX_RETRIES attempts
    assert c.consecutive_failures == 0


def test_transport_error_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text="ok")

    with client_for(handler) as c:
        assert c.get("https://example.test/").status_code == 200
    assert len(calls) == 2


def test_persistent_5xx_gives_up_after_max_retries_and_reraises():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    with client_for(handler) as c:
        with pytest.raises(httpx.HTTPStatusError):
            c.get("https://example.test/")
        assert c.consecutive_failures == 1
    assert len(calls) == http_client.MAX_RETRIES


def test_4xx_is_not_retried_but_raises():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    with client_for(handler) as c:
        with pytest.raises(httpx.HTTPStatusError) as err:
            c.get("https://example.test/missing")
    assert err.value.response.status_code == 404
    assert len(calls) == 1


def test_redirects_are_followed():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.test/new"})
        return httpx.Response(200, text="new page")

    with client_for(handler) as c:
        assert c.get("https://example.test/old").text == "new page"


def test_circuit_breaker_opens_after_consecutive_failures_and_blocks_requests():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    with client_for(handler, max_consecutive_failures=3) as c:
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                c.get("https://example.test/")
        with pytest.raises(CircuitBreakerOpen):  # the failure that trips it
            c.get("https://example.test/")
        n = len(calls)
        with pytest.raises(CircuitBreakerOpen):  # now refuses without a request
            c.get("https://example.test/")
        assert len(calls) == n


def test_success_resets_failure_counter():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(404 if state["n"] == 1 else 200, text="ok")

    with client_for(handler) as c:
        with pytest.raises(httpx.HTTPStatusError):
            c.get("https://example.test/")
        assert c.consecutive_failures == 1
        c.get("https://example.test/")
        assert c.consecutive_failures == 0


def test_no_delay_env_disables_sleep(monkeypatch):
    def boom(_):
        raise AssertionError("slept despite SGLEGALHELP_NO_DELAY=1")

    monkeypatch.setattr(http_client.time, "sleep", boom)
    with client_for(lambda r: httpx.Response(200)) as c:
        c.get("https://example.test/")


def test_polite_delay_is_applied_when_enabled(monkeypatch):
    monkeypatch.delenv("SGLEGALHELP_NO_DELAY")
    slept = []
    monkeypatch.setattr(http_client.time, "sleep", slept.append)
    with client_for(lambda r: httpx.Response(200), delay_seconds=2.0, jitter_seconds=0.0) as c:
        c.get("https://example.test/")
    assert slept == [2.0]


def test_retry_wait_is_floored_at_delay_when_enabled(monkeypatch):
    # Real tenacity wait strategy (exponential, min >= delay) — only the sleep
    # function is stubbed so the test stays instant.
    monkeypatch.delenv("SGLEGALHELP_NO_DELAY")
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)
    waits = []
    c = client_for(lambda r: httpx.Response(500), delay_seconds=1.5, jitter_seconds=0.0)
    c._retrying.sleep = waits.append
    with c:
        with pytest.raises(httpx.HTTPStatusError):
            c.get("https://example.test/")
    assert len(waits) == http_client.MAX_RETRIES - 1
    assert all(w >= 1.5 for w in waits)


def test_cache_write_is_atomic_and_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / "page.html"
    http_client.cache_write(target, "héllo")
    assert target.read_text(encoding="utf-8") == "héllo"
    assert not list(target.parent.glob("*.tmp"))
    http_client.cache_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
