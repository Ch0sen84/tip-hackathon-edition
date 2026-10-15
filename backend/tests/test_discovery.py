"""Market Discovery (C2b/C2c) tests: adapter.discover_candidates() on both
the live (mocked HTTP) and synthetic (fixture-backed) adapters, plus the
/api/radar/discover endpoint's bounded-single-call behavior, filter
passthrough, server-side limit cap, candidate/evaluated state distinction,
and ranking."""
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import DISCOVERY_LIMIT_CAP, LiveUWAdapter, UWRestClient
from app.adapters.synthetic_adapter import SyntheticDataAdapter

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _mock_session(status_code, json_body=None, text_body=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text_body
    resp.json.return_value = json_body if json_body is not None else {}
    session = MagicMock()
    session.get.return_value = resp
    return session


# --- LiveUWAdapter: mocked HTTP, bounded single call -----------------------

def test_live_discover_candidates_makes_exactly_one_request():
    session = _mock_session(200, {"data": [{"ticker": "NVDA", "net_premium": 1_000_000}]})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", client=client)
    result = adapter.discover_candidates({"limit": 10})
    assert session.get.call_count == 1  # exactly one HTTP call, never a loop
    assert result["screener_requests_used"] == 1
    assert result["unavailable"] is False
    assert len(result["candidates"]) == 1
    assert "snapshot_at" in result and result["snapshot_at"]


def test_live_discover_candidates_passes_filters_through_to_the_request():
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", client=client)
    adapter.discover_candidates({
        "min_premium": 500_000, "min_call_volume": 1000, "min_put_volume": 200,
        "min_iv_rank": 50, "min_change": 1.0, "max_change": 10.0, "limit": 15,
    })
    _, kwargs = session.get.call_args
    params = kwargs.get("params", {})
    assert params.get("min_premium") == 500_000
    assert params.get("min_call_volume") == 1000
    assert params.get("min_put_volume") == 200
    assert params.get("min_iv_rank") == 50
    assert params.get("min_change") == 1.0
    assert params.get("max_change") == 10.0
    assert params.get("limit") == 15


def test_live_discover_candidates_clamps_limit_server_side_regardless_of_request():
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", client=client)
    adapter.discover_candidates({"limit": 999999})
    _, kwargs = session.get.call_args
    assert kwargs.get("params", {}).get("limit") == DISCOVERY_LIMIT_CAP
    assert kwargs.get("params", {}).get("limit") <= 50


def test_live_discover_candidates_never_auto_paginates_even_with_full_page():
    """A full page of exactly `limit` rows must never trigger a second
    request looking for more -- this adapter makes ONE call, period."""
    full_page = [{"ticker": f"T{i}", "net_premium": i} for i in range(DISCOVERY_LIMIT_CAP)]
    session = _mock_session(200, {"data": full_page})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", client=client)
    result = adapter.discover_candidates({"limit": DISCOVERY_LIMIT_CAP})
    assert session.get.call_count == 1
    assert len(result["candidates"]) == DISCOVERY_LIMIT_CAP


def test_live_discover_candidates_handles_unauthorized_without_crashing():
    session = _mock_session(401, text_body="unauthorized")
    client = UWRestClient("bad-key", session=session)
    adapter = LiveUWAdapter(api_key="bad-key", client=client)
    result = adapter.discover_candidates({})
    assert result["unavailable"] is True
    assert result["candidates"] == []
    assert result["screener_requests_used"] == 1


# --- SyntheticDataAdapter: fixture-backed ----------------------------------

def test_synthetic_discover_candidates_loads_fixture():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    result = adapter.discover_candidates({})
    assert result["unavailable"] is False
    assert len(result["candidates"]) > 0
    assert all(c.get("synthetic") is True for c in result["candidates"])
    assert result["screener_requests_used"] == 1
    assert result["snapshot_at"]


def test_synthetic_discover_candidates_applies_named_filters():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    all_result = adapter.discover_candidates({})
    filtered = adapter.discover_candidates({"min_premium": 20_000_000})
    assert len(filtered["candidates"]) < len(all_result["candidates"])
    assert all(c["net_premium"] >= 20_000_000 for c in filtered["candidates"])


def test_synthetic_discover_candidates_respects_limit_cap():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    result = adapter.discover_candidates({"limit": 2})
    assert len(result["candidates"]) <= 2
    result2 = adapter.discover_candidates({"limit": 999})
    assert len(result2["candidates"]) <= DISCOVERY_LIMIT_CAP


# --- /api/radar/discover endpoint ------------------------------------------

def _client(app_main):
    return TestClient(app_main.app)


def test_endpoint_returns_ranked_candidates_with_disclosure_fields(app_main):
    client = _client(app_main)
    res = client.post("/api/radar/discover", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["screener_requests_used"] == 1
    assert "snapshot_at" in body
    assert body["scan_scope"] == "market_wide_screener"
    assert len(body["candidates"]) > 0
    # ranked descending by |net_premium|
    premiums = [abs(c["net_premium"]) for c in body["candidates"]]
    assert premiums == sorted(premiums, reverse=True)


def test_endpoint_candidates_are_never_pre_labeled_as_evaluated(app_main):
    client = _client(app_main)
    res = client.post("/api/radar/discover", json={})
    body = res.json()
    for c in body["candidates"]:
        assert c["discovery_state"] == "candidate"
        assert "state" not in c  # no signal_engine state leaks onto a raw candidate


def test_endpoint_filter_passthrough_reduces_results(app_main):
    client = _client(app_main)
    all_res = client.post("/api/radar/discover", json={}).json()
    filtered_res = client.post("/api/radar/discover", json={"min_premium": 20_000_000}).json()
    assert filtered_res["count"] < all_res["count"]


def test_endpoint_limit_is_capped_at_50_even_if_a_larger_value_is_requested(app_main):
    client = _client(app_main)
    res = client.post("/api/radar/discover", json={"limit": 500})
    assert res.status_code == 200
    body = res.json()
    assert body["limit_cap"] == 50
    assert body["count"] <= 50


def test_evaluate_in_tip_reuses_the_existing_per_ticker_pipeline(app_main):
    """'Evaluate in TIP' on a candidate = the SAME GET /api/ticker/{t}
    endpoint My Watchlist uses -- no parallel evaluation pipeline. Use a
    discovery candidate ticker that's also a real demo scenario (FLUX)."""
    client = _client(app_main)
    discover_res = client.post("/api/radar/discover", json={}).json()
    flux = next((c for c in discover_res["candidates"] if c["ticker"] == "FLUX"), None)
    assert flux is not None, "fixture must include FLUX for this test to be meaningful"
    assert flux["discovery_state"] == "candidate"
    eval_res = client.get("/api/ticker/FLUX")
    assert eval_res.status_code == 200
    evaluated = eval_res.json()
    assert evaluated["state"] in ("NEW", "BUILDING", "CONFIRMED", "STABLE", "FADING", "INVALIDATED")
    assert "gates" in evaluated  # real gate evaluation happened, not a canned label
