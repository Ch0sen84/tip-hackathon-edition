"""Phase B: UW HTTP-call wrapper tests (concurrency ceiling, retry/
backoff/jitter, Retry-After, 401/403/429/5xx handling, circuit breaker,
telemetry). Every test mocks requests.Session -- NO real network calls
are made anywhere in this test module."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from app.adapters.live_uw_adapter import (
    DEFAULT_MAX_CONCURRENT_REQUESTS, CircuitBreaker, CircuitOpenError, RequestTelemetry, UWRestClient,
)


def _resp(status_code, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.ok = 200 <= status_code < 300
    r.text = text
    r.headers = headers or {}
    r.json.return_value = {"data": []}
    return r


def _client(session, **kwargs):
    kwargs.setdefault("max_retries", 2)
    kwargs.setdefault("retry_base_delay_s", 0.001)
    kwargs.setdefault("sleep_fn", lambda s: None)  # no real wall-clock delay in tests
    kwargs.setdefault("jitter_fn", lambda: 0.0)
    return UWRestClient("fake-key", session=session, **kwargs)


def test_default_max_concurrent_requests_is_three():
    """CONFIRMED live this session: UW still enforces a hard 3-
    concurrent-request ceiling even though daily/per-minute caps were
    removed. This default must never silently drift."""
    assert DEFAULT_MAX_CONCURRENT_REQUESTS == 3
    client = UWRestClient("k", session=MagicMock())
    assert client.max_concurrent_requests == 3


def test_max_concurrent_requests_configurable_via_env(monkeypatch):
    monkeypatch.setenv("TIP_UW_MAX_CONCURRENT_REQUESTS", "7")
    client = UWRestClient("k", session=MagicMock())
    assert client.max_concurrent_requests == 7


def test_semaphore_caps_real_concurrent_in_flight_requests():
    """Launches more concurrent get() calls than the ceiling allows and
    confirms the number of REQUESTS ACTUALLY IN FLIGHT at once never
    exceeds the configured ceiling -- not just that total count is
    right eventually."""
    max_concurrent = 3
    in_flight = {"current": 0, "max_seen": 0}
    lock = threading.Lock()

    def slow_get(*args, **kwargs):
        with lock:
            in_flight["current"] += 1
            in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
        time.sleep(0.05)
        with lock:
            in_flight["current"] -= 1
        return _resp(200)

    session = MagicMock()
    session.get.side_effect = slow_get
    client = _client(session, max_concurrent_requests=max_concurrent, max_retries=0)

    threads = [threading.Thread(target=client.get, args=(f"/api/x{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert in_flight["max_seen"] <= max_concurrent


def test_429_respects_retry_after_header_and_eventually_succeeds():
    session = MagicMock()
    session.get.side_effect = [_resp(429, headers={"Retry-After": "0"}), _resp(200)]
    sleeps = []
    client = _client(session, sleep_fn=lambda s: sleeps.append(s))
    body = client.get("/api/x")
    assert body == {"data": []}
    assert session.get.call_count == 2
    assert sleeps  # a backoff delay was actually computed/used


def test_429_exhausts_retries_and_returns_structured_error():
    session = MagicMock()
    session.get.return_value = _resp(429, text="rate limited")
    client = _client(session, max_retries=2)
    body = client.get("/api/x")
    assert body["status"] == "error"
    assert body["status_code"] == 429
    assert session.get.call_count == 3  # 1 + 2 retries, never more


def test_5xx_retries_with_backoff_then_succeeds():
    session = MagicMock()
    session.get.side_effect = [_resp(503), _resp(200)]
    client = _client(session)
    body = client.get("/api/x")
    assert body == {"data": []}
    assert session.get.call_count == 2


def test_401_never_retried_surfaced_immediately():
    session = MagicMock()
    session.get.return_value = _resp(401, text="bad key")
    client = _client(session, max_retries=3)
    body = client.get("/api/x")
    assert body["status"] == "unauthorized"
    assert session.get.call_count == 1  # no retry on a definitive auth failure


def test_403_never_retried_surfaced_immediately():
    session = MagicMock()
    session.get.return_value = _resp(403, text="not on this tier")
    client = _client(session, max_retries=3)
    body = client.get("/api/x")
    assert body["status"] == "unavailable_on_tier"
    assert session.get.call_count == 1


def test_network_exception_retries_then_surfaces_error():
    import requests
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("boom")
    client = _client(session, max_retries=2)
    body = client.get("/api/x")
    assert body["status"] == "error"
    assert session.get.call_count == 3


def test_circuit_breaker_opens_after_consecutive_failures_and_short_circuits():
    session = MagicMock()
    session.get.return_value = _resp(503)
    breaker = CircuitBreaker(threshold=2, cooldown_s=60)
    client = _client(session, max_retries=0, circuit_breaker=breaker)

    client.get("/api/flaky")  # failure 1
    client.get("/api/flaky")  # failure 2 -- trips the breaker
    calls_before = session.get.call_count

    body = client.get("/api/flaky")  # should short-circuit, no new HTTP call
    assert session.get.call_count == calls_before
    assert "Circuit breaker open" in body["detail"] or body["status"] == "error"


def test_circuit_breaker_is_per_path_not_global():
    session = MagicMock()

    def side_effect(url, **kwargs):
        if "flaky" in url:
            return _resp(503)
        return _resp(200)

    session.get.side_effect = side_effect
    breaker = CircuitBreaker(threshold=1, cooldown_s=60)
    client = _client(session, max_retries=0, circuit_breaker=breaker)

    client.get("/api/flaky")  # trips breaker for /api/flaky only
    healthy_body = client.get("/api/healthy")
    assert healthy_body == {"data": []}


def test_circuit_breaker_resets_on_success():
    session = MagicMock()
    session.get.side_effect = [_resp(503), _resp(200), _resp(503)]
    breaker = CircuitBreaker(threshold=2, cooldown_s=60)
    client = _client(session, max_retries=0, circuit_breaker=breaker)

    client.get("/api/x")  # failure 1
    client.get("/api/x")  # success -- resets consecutive-failure count
    client.get("/api/x")  # failure 1 again, not failure 2 -- must not trip yet
    # Confirm the breaker did not open: one more real call should still
    # reach the session (not short-circuited).
    session.get.side_effect = None
    session.get.return_value = _resp(200)
    body = client.get("/api/x")
    assert body == {"data": []}


def test_telemetry_records_counts_latency_and_outcome_breakdown():
    session = MagicMock()
    session.get.side_effect = [_resp(429, headers={"Retry-After": "0"}), _resp(200), _resp(401)]
    client = _client(session)
    client.get("/api/x")   # retried -> success
    client.get("/api/y")   # immediate 401

    summary = client.telemetry.summary()
    assert summary["/api/x"]["count"] == 1
    assert summary["/api/x"]["success"] == 1
    assert summary["/api/x"]["retried"] == 1
    assert summary["/api/y"]["failed"] == 1
    assert "unauthorized" in summary["/api/y"]["by_outcome"]


def test_missing_api_key_never_consumes_the_semaphore_or_makes_a_request():
    session = MagicMock()
    client = _client(session)
    client.api_key = ""
    body = client.get("/api/x")
    assert body["status"] == "unauthorized"
    session.get.assert_not_called()
