"""END-TO-END Live-mode pipeline on RECORDED real Unusual Whales data over a
real HTTP socket (signal-integrity remediation, requirement J).

tools/uw_replay_server.py serves the recorded 2026-10-15..17 capture with
UW's cursor semantics; the backend is configured through its real Settings
flow (save key -> Test Connection -> Live Mode), so every layer runs
unmodified: requests -> UWRestClient (status handling, retries) ->
LiveUWAdapter normalization -> session collector -> catalog -> session
baselines -> gates -> confidence -> episode lifecycle -> SQLite -> API.
Nothing is inserted into the database by hand.

What the recorded data can and cannot show (asserted, not worked around):
every captured scan saw only the newest page of each endpoint, so no
session can be verified as collected from the open and no baseline can
form -> both independent gates are "insufficient" and nothing qualifies.
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests import recorded_uw

NY = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 16, 15, 2, 30, tzinfo=timezone.utc)   # just after the PLTR 15:02Z capture
WATCH = ["SPY", "QQQ", "NVDA", "TSLA", "PLTR"]


def _replay_module():
    path = Path(__file__).resolve().parents[2] / "tools" / "uw_replay_server.py"
    spec = importlib.util.spec_from_file_location("uw_replay_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def replay():
    recorded_uw.require()
    server, url, state = _replay_module().start(0, AS_OF)
    yield url, state
    server.shutdown()


@pytest.fixture
def live(app_main, replay, monkeypatch, tmp_path):
    url, state = replay
    monkeypatch.setattr(app_main.settings, "uw_base_url", url)
    monkeypatch.setattr(app_main.settings, "db_path", str(tmp_path / "live.db"))
    monkeypatch.setattr(app_main.settings, "demo_db_path", str(tmp_path / "live.demo.db"))
    app_main._store = app_main._demo_store = app_main._catalog = None
    monkeypatch.setenv("TIP_REPLAY_CLOCK", AS_OF.isoformat())
    client = TestClient(app_main.app)
    assert client.post("/api/settings/uw-key", json={"api_key": "replay-test-key"}).status_code == 200
    assert client.post("/api/settings/uw-key/test").json()["result"] == "success"
    assert client.post("/api/settings/mode", json={"mode": "live"}).status_code == 200
    assert client.put("/api/settings/watchlist", json={"watchlist": WATCH}).status_code == 200
    assert client.get("/api/health").json()["replay_clock"] == AS_OF.isoformat()
    return app_main, client, url, state


def test_recorded_data_traverses_every_layer_and_nothing_is_overstated(live):
    m, client, url, state = live
    radar = client.post("/api/radar/refresh").json()
    entries = {e["ticker"]: e for e in radar["highest"] + radar["other"]}
    assert set(entries) == set(WATCH)
    assert radar["highest"] == []                                         # nothing qualifies
    for t, e in entries.items():
        assert e["eval_freshness"] == "current", t
        assert all(c["status"] == "ok" for c in e["collection"].values()), (t, e["collection"])
        assert e["confidence"] == "insufficient", (t, e["confidence"])
        assert e["state"] is None and e["transition_rule"] == "held_insufficient_evidence", t

    pltr = client.get("/api/ticker/PLTR").json()                          # cache-first read
    assert pltr["served_from_cache"] is True
    assert pltr["evidence_session"] == "2026-10-16"
    assert pltr["stock_state"]["change_percent"] == pytest.approx(-3.456, abs=0.01)
    g = pltr["gates"]
    assert g["dark_pool_anomaly"]["status"] == "insufficient"
    assert g["options_flow_anomaly"]["status"] == "insufficient"
    assert g["historical_percentile"]["status"] == "unsupported"
    assert g["catalyst_identified"]["status"] == "unsupported"
    assert g["price_confirmation"]["status"] == "not_evaluated"
    assert g["price_confirmation"]["metrics"]["direction_heuristic"] == "bearish-lean"
    assert g["data_quality"]["status"] == "pass"
    assert "probab" not in pltr["confidence_reason"].lower()

    # session-scoped, no look-ahead: every alert shown is from the evidence session, at/before as-of
    for a in pltr["raw"]["flow_alerts"]:
        ts = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
        assert ts.astimezone(NY).date().isoformat() == "2026-10-16" and ts <= AS_OF
    assert pltr["raw_counts"]["flow_alerts"] > 100      # session-to-date, not just the newest 100

    # persisted honestly: no qualification episode, held scans logged, coverage never "complete"
    store = m.get_store("live")
    assert store.episodes("PLTR") == [] and store.current_state("PLTR") is None
    assert store.history("PLTR")[-1]["transition_rule"] == "held_insufficient_evidence"
    for dataset in ("darkpool", "options_flow"):
        cov = m.get_catalog().get_session_coverage(dataset, "PLTR", "2026-10-16")
        assert cov is not None and cov["status"] != "complete", (dataset, cov)

    # every request went to the local replay socket
    assert state["calls"] and all(c.startswith("/api/") for c in state["calls"])


def test_backfill_on_recorded_data_cannot_manufacture_a_baseline(live):
    m, client, url, state = live
    body = client.post("/api/ticker/SPY/baseline/backfill?max_pages=40").json()
    for dataset in ("darkpool", "options_flow"):
        assert body["datasets"][dataset]["complete_prior_sessions"] == 0
        assert body["datasets"][dataset]["baseline"]["status"] == "insufficient"
    after = client.post("/api/ticker/SPY/refresh").json()
    assert after["confidence"] == "insufficient"


def test_chart_refresh_uses_recorded_bars_and_discloses_marker_provenance(live):
    m, client, url, state = live
    body = client.post("/api/ticker/NVDA/ohlc/refresh?range=1M").json()
    assert body["candle_count"] > 0 and body["unavailable"] is False
    assert body["marker_sources"]["origin"] == "live_fetch"
    assert body["marker_sources"]["options_flow"]["status"] == "ok"
    cached = client.get("/api/ticker/NVDA/ohlc?range=1M").json()
    assert cached["served_from_cache"] is True and cached["marker_sources"]["origin"] == "catalog"


@pytest.mark.parametrize("fault,expected", [("fail", "error"), ("malformed", "malformed")])
def test_injected_provider_faults_over_http_are_never_negative_evidence(live, fault, expected):
    m, client, url, state = live
    import requests
    if fault == "fail":
        requests.post(f"{url}/__control/fail?path=flow-alerts&status=503", timeout=5)
    else:
        requests.post(f"{url}/__control/malformed?path=flow-alerts", timeout=5)
    body = client.post("/api/ticker/SPY/refresh").json()
    assert body["collection"]["options_flow"]["status"] == expected
    assert body["gates"]["options_flow_anomaly"]["status"] == "unavailable"
    assert body["confidence"] == "insufficient"
    assert body["transition_rule"] == "held_incomplete_collection"
    assert body["eval_freshness"] == "collection_failed"
    flow_calls = [c for c in requests.get(f"{url}/__control/calls", timeout=5).json()["calls"] if "flow-alerts" in c]
    assert 1 <= len(flow_calls) <= 3                                      # bounded retries
    assert m.get_store("live").history("SPY") == []                       # nothing written to lifecycle


def test_unknown_ticker_is_an_empty_answer_not_fabricated_evidence(live):
    m, client, url, state = live
    body = client.post("/api/ticker/ZZZZ/refresh").json()
    # flow/dark-pool answered with zero rows (a real, empty answer) ...
    assert body["collection"]["options_flow"]["status"] == "ok"
    assert body["collection"]["darkpool"]["status"] == "ok"
    assert body["raw_counts"] == {"flow_alerts": 0, "darkpool_trades": 0, "cap": 500}
    # ... but an empty stock-state object is not a valid snapshot: reported,
    # never filled in, and the scan is held rather than evaluated
    assert body["collection"]["stock_state"]["status"] == "malformed"
    assert body["collection_complete"] is False
    assert body["transition_rule"] == "held_incomplete_collection"
    assert body["state"] is None and body["confidence"] == "insufficient"


def test_replay_clock_is_refused_for_a_non_loopback_uw_url(app_main, monkeypatch):
    monkeypatch.setenv("TIP_REPLAY_CLOCK", AS_OF.isoformat())
    monkeypatch.setattr(app_main.settings, "uw_base_url", "https://api.unusualwhales.com")
    assert app_main._replay_clock() is None
