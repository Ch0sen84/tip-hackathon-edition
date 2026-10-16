"""Phase D, item 8: authenticated settings controls + the Market-wide
Radar read route. Every mutating route is behind require_auth (same
pattern as every other mutating route in this app); the status/
market-wide routes are read-only but ALSO behind require_auth,
consistent with the existing diagnostics-route pattern (Phase B's
/api/diagnostics/uw-requests)."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_scheduler_status_requires_auth(app_main):
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    res = client.get("/api/scheduler/status")
    assert res.status_code == 401


def test_scheduler_mode_post_requires_auth(app_main):
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    res = client.post("/api/scheduler/mode", json={"mode": "dry_run"})
    assert res.status_code == 401


def test_market_wide_radar_requires_auth(app_main):
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    res = client.get("/api/radar/market-wide")
    assert res.status_code == 401


def test_scheduler_status_defaults_to_disabled(app_main):
    client = TestClient(app_main.app)
    res = client.get("/api/scheduler/status")
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "disabled"
    assert body["last_cycle_candidates_evaluated"] is None
    assert body["freshness_window_s"] == 20 * 60


def test_set_scheduler_mode_to_dry_run_then_status_reflects_it(app_main):
    client = TestClient(app_main.app)
    res = client.post("/api/scheduler/mode", json={"mode": "dry_run"})
    assert res.status_code == 200
    assert res.json()["mode"] == "dry_run"
    status = client.get("/api/scheduler/status").json()
    assert status["mode"] == "dry_run"


def test_set_scheduler_mode_rejects_invalid_value(app_main):
    client = TestClient(app_main.app)
    res = client.post("/api/scheduler/mode", json={"mode": "turbo"})
    assert res.status_code == 400


def test_all_three_modes_settable_via_the_route(app_main):
    client = TestClient(app_main.app)
    for mode in ("dry_run", "enabled", "disabled"):
        res = client.post("/api/scheduler/mode", json={"mode": mode})
        assert res.status_code == 200
        assert res.json()["mode"] == mode


def test_market_wide_radar_empty_before_any_discovery_snapshot(app_main):
    client = TestClient(app_main.app)
    res = client.get("/api/radar/market-wide")
    assert res.status_code == 200
    body = res.json()
    assert body["candidates"] == []
    assert "note" in body


def test_market_wide_radar_shows_cataloged_candidates_with_freshness(app_main, monkeypatch):
    from app import main as _main

    catalog = _main.get_catalog()
    catalog.catalog_discovery_snapshot(
        filters={}, candidates=[{"ticker": "NVDA"}, {"ticker": "TSLA"}],
        snapshot_at="2026-10-15T14:00:00+00:00",
    )
    client = TestClient(app_main.app)
    res = client.get("/api/radar/market-wide")
    assert res.status_code == 200
    body = res.json()
    tickers = {c["ticker"] for c in body["candidates"]}
    assert tickers == {"NVDA", "TSLA"}
    for c in body["candidates"]:
        assert c["freshness"] == "unevaluated"  # cataloged as a discovery candidate, never scanned yet
    assert "coverage_note" in body


def test_scheduler_status_exposes_effective_config_d9(app_main):
    """QA remediation D9: effective running configuration (not just
    documented defaults) is visible via the existing authenticated
    diagnostics/status endpoint."""
    client = TestClient(app_main.app)
    body = client.get("/api/scheduler/status").json()
    assert body["uw_max_concurrent_requests"] is None or isinstance(body["uw_max_concurrent_requests"], int)
    assert "lease_ttl_s" in body
    assert "poll_interval_s" in body
    assert "cycle_in_progress" in body


def test_scheduler_run_once_requires_auth(app_main):
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    res = client.post("/api/scheduler/run-once")
    assert res.status_code == 401


def test_scheduler_run_once_refuses_when_disabled(app_main):
    client = TestClient(app_main.app)
    res = client.post("/api/scheduler/run-once")
    assert res.status_code == 200
    assert res.json()["reason"] == "disabled"


def test_scheduler_run_once_runs_a_cycle_in_dry_run_mode(app_main, monkeypatch):
    from app import main as _main

    class _FakeAdapter:
        def discover_candidates(self, filters=None):
            return {
                "candidates": [{"ticker": "NVDA", "net_premium": 1}], "snapshot_at": "2026-10-15T14:00:00+00:00",
                "screener_requests_used": 1, "source": "fake", "unavailable": False, "reason": None,
            }

        def get_universe(self):
            return []

    monkeypatch.setattr(_main, "get_adapter", lambda: _FakeAdapter())
    client = TestClient(app_main.app)
    client.post("/api/scheduler/mode", json={"mode": "dry_run"})
    res = client.post("/api/scheduler/run-once")
    assert res.status_code == 200
    body = res.json()
    assert body["ran"] is True
    assert body["triggered_by"] == "run_once"
    assert body["candidates_evaluated"] == 0  # dry_run never evaluates


def test_market_wide_radar_marks_current_vs_unevaluated(app_main):
    from app import main as _main
    from datetime import datetime, timezone

    catalog = _main.get_catalog()
    catalog.catalog_discovery_snapshot(
        filters={}, candidates=[{"ticker": "NVDA"}, {"ticker": "TSLA"}],
        snapshot_at="2026-10-15T14:00:00+00:00",
    )
    catalog.catalog_signal_evaluation(
        "NVDA", {"state": "WATCHING", "confidence": "LOW"},
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
    client = TestClient(app_main.app)
    res = client.get("/api/radar/market-wide")
    body = res.json()
    by_ticker = {c["ticker"]: c for c in body["candidates"]}
    assert by_ticker["NVDA"]["freshness"] == "current"
    assert by_ticker["TSLA"]["freshness"] == "unevaluated"
