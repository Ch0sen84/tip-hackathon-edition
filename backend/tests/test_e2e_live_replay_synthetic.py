"""PUBLIC end-to-end Live pipeline test on the SYNTHETIC replay fixture
(fixtures/synthetic_uw/, built deterministically by
tools/build_synthetic_uw_fixture.py -- contains no real market data).

Same real path as the private recorded-data test (test_e2e_live_replay.py):
real Settings flow -> requests over a real local socket -> UWRestClient ->
LiveUWAdapter normalization -> session collector (verified UW pagination
semantics, served by tools/uw_replay_server.py) -> catalog -> session
baselines -> gates -> confidence -> episode store -> API. Nothing is inserted
into the database by hand."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
from fastapi.testclient import TestClient

from tests.recorded_uw import SYNTHETIC_FIXTURE

NY = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 16, 15, 2, 30, tzinfo=timezone.utc)
WATCH = ["SYNP", "SYNC", "SYNQ"]


def _replay_module():
    path = Path(__file__).resolve().parents[2] / "tools" / "uw_replay_server.py"
    spec = importlib.util.spec_from_file_location("uw_replay_server_syn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def live(app_main, monkeypatch, tmp_path):
    assert SYNTHETIC_FIXTURE.exists(), "the public synthetic fixture must ship with the repository"
    server, url, state = _replay_module().start(0, AS_OF, SYNTHETIC_FIXTURE)
    monkeypatch.setattr(app_main.settings, "uw_base_url", url)
    monkeypatch.setattr(app_main.settings, "db_path", str(tmp_path / "live.db"))
    monkeypatch.setattr(app_main.settings, "demo_db_path", str(tmp_path / "live.demo.db"))
    app_main._store = app_main._demo_store = app_main._catalog = None
    monkeypatch.setenv("TIP_REPLAY_CLOCK", AS_OF.isoformat())
    client = TestClient(app_main.app)
    assert client.post("/api/settings/uw-key", json={"api_key": "synthetic-test-key"}).status_code == 200
    assert client.post("/api/settings/uw-key/test").json()["result"] == "success"
    assert client.post("/api/settings/mode", json={"mode": "live"}).status_code == 200
    assert client.put("/api/settings/watchlist", json={"watchlist": WATCH}).status_code == 200
    yield app_main, client, url, state
    server.shutdown()


def test_fixture_declares_itself_synthetic():
    import gzip
    import json
    with gzip.open(SYNTHETIC_FIXTURE, "rt", encoding="utf-8") as fh:
        prov = json.load(fh)["provenance"]
    assert prov["contains_real_market_data"] is False and "SYNTHETIC" in prov["source"]


def test_synthetic_data_traverses_every_layer_honestly(live):
    m, client, url, state = live
    assert requests.get(f"{url}/__control/info", timeout=5).json()["synthetic"] is True
    radar = client.post("/api/radar/refresh").json()
    entries = {e["ticker"]: e for e in radar["highest"] + radar["other"]}
    assert set(entries) == set(WATCH) and radar["highest"] == []
    for t, e in entries.items():
        assert all(c["status"] == "ok" for c in e["collection"].values()), (t, e["collection"])
        assert e["confidence"] == "insufficient" and e["state"] is None, (t, e)

    synp = client.get("/api/ticker/SYNP").json()
    assert synp["served_from_cache"] is True and synp["evidence_session"] == "2026-10-16"
    assert synp["stock_state"]["change_percent"] == pytest.approx(-3.456, abs=0.01)
    g = synp["gates"]
    assert g["dark_pool_anomaly"]["status"] == "insufficient"
    assert g["options_flow_anomaly"]["status"] == "insufficient"
    assert g["historical_percentile"]["status"] == "unsupported"
    assert g["catalyst_identified"]["status"] == "unsupported"
    assert g["price_confirmation"]["metrics"]["direction_heuristic"] == "bearish-lean"   # put buying
    for a in synp["raw"]["flow_alerts"]:
        ts = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
        assert ts.astimezone(NY).date().isoformat() == "2026-10-16" and ts <= AS_OF
    # print identity end to end: computed independently from the fixture --
    # distinct prints (several share a tracking_id), canceled ones excluded,
    # executed by as_of - 60 s (the collector's reporting lag)
    import gzip
    import json
    from datetime import timedelta
    with gzip.open(SYNTHETIC_FIXTURE, "rt", encoding="utf-8") as fh:
        prints = [r for r in json.load(fh)["darkpool"].values() if r["ticker"] == "SYNP"]
    cutoff = AS_OF - timedelta(seconds=60)
    expected = {(r["tracking_id"], r["executed_at"], r["price"], r["size"], r["volume"]) for r in prints
                if not r["canceled"] and r["executed_at"].startswith("2026-10-16")
                and datetime.fromisoformat(r["executed_at"].replace("Z", "+00:00")) <= cutoff}
    shared = len(prints) - len({r["tracking_id"] for r in prints})
    assert shared > 0                                   # the scenario really has shared tracking_ids
    assert synp["raw_counts"]["darkpool_trades"] == len(expected)
    assert m.get_store("live").episodes("SYNP") == []


def test_backfill_hits_the_replay_history_cutoff_and_stays_insufficient(live):
    m, client, url, state = live
    body = client.post("/api/ticker/SYNC/baseline/backfill?max_pages=40").json()
    for dataset in ("darkpool", "options_flow"):
        b = body["datasets"][dataset]["baseline"]
        assert b["status"] == "insufficient" and b["eligible_sessions"] < 20
        assert any(t["reason"] == "provider_history_cutoff" for t in body["backfill"][dataset]["sessions_touched"])


def test_chart_and_marker_provenance(live):
    m, client, url, state = live
    body = client.post("/api/ticker/SYNC/ohlc/refresh?range=1M").json()
    assert body["candle_count"] > 0 and body["marker_sources"]["origin"] == "live_fetch"
    assert client.get("/api/ticker/SYNC/ohlc?range=1M").json()["marker_sources"]["origin"] == "catalog"


@pytest.mark.parametrize("fault,expected", [("fail", "error"), ("malformed", "malformed")])
def test_injected_faults_are_unavailable_not_negative(live, fault, expected):
    m, client, url, state = live
    if fault == "fail":
        requests.post(f"{url}/__control/fail?path=flow-alerts&status=503", timeout=5)
    else:
        requests.post(f"{url}/__control/malformed?path=flow-alerts", timeout=5)
    body = client.post("/api/ticker/SYNC/refresh").json()
    assert body["collection"]["options_flow"]["status"] == expected
    assert body["gates"]["options_flow_anomaly"]["status"] == "unavailable"
    assert body["transition_rule"] == "held_incomplete_collection"
    assert body["eval_freshness"] == "collection_failed"
    assert m.get_store("live").history("SYNC") == []
