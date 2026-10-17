"""Round 13 item 1 regression test (ux-nav-watchlist-reliability round 13)
-- THE highest-priority fix this round.

Real bug, traced to the ROUTE itself (not a frontend render-skip like the
P1 fix two rounds ago): _radar_impl's cache-only read path builds `items`
for every watchlist ticker and feeds them all into
tip_evidence.present(), which -- correctly, for its OWN purpose --
returns only items where qualifies(item) is True. A watchlist ticker that
WAS genuinely evaluated this cycle (real, non-empty `gates`) but passed
ZERO qualifying gates (e.g. a brand-new ARTNA/MSEX) is therefore silently
dropped from BOTH `highest` and `other` -- unlike the never-evaluated
case (scan is None), which already had its own force-append path. Add
ARTNA and MSEX alongside 2 pre-existing (never-evaluated / qualifying)
tickers, evaluate all 4: this test proves ALL 4 appear in the real
GET /api/radar response, correctly bucketed and flagged, with zero UW
calls (strictly cache-only, per the task's hard requirement).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter


def _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist):
    mock_session = MagicMock()
    adapter = LiveUWAdapter(api_key="fake-key", base_url="https://api.unusualwhales.com",
                             client_api_id="fake", watchlist=list(watchlist))
    adapter.client.session = mock_session
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter, mock_session


def _seed_qualifying_evaluation(app_main, ticker: str, state: str = "BUILDING"):
    catalog = app_main.get_catalog()
    scan = {
        "ticker": ticker, "state": state, "previous_state": "NEW", "transition_rule": "first_qualifying_gate",
        "confidence": "medium", "signal_type": "dark_pool_anomaly", "confidence_reason": "seeded",
        "qualifies": True, "qualifying_days_count": 1,
        "gates_passed": ["dark_pool_anomaly"],
        "gates": {
            "dark_pool_anomaly": {"passed": True, "metrics": {"ratio": 3.2}},
            "options_flow_anomaly": {"passed": False, "metrics": {}},
            "price_move": {"passed": False, "metrics": {}},
            "catalyst_identified": {"passed": False, "metrics": {}},
        },
        "stock_state": {"last_price": 101.5, "change_percent": 1.2},
        "raw": {"flow_alerts": [], "darkpool_trades": []},
    }
    catalog.catalog_signal_evaluation(ticker, scan, kind="on_demand")
    return scan


def _seed_zero_gate_evaluation(app_main, ticker: str):
    """A ticker that WAS genuinely evaluated this cycle (real, non-empty
    `gates`) but passed zero qualifying gates -- the exact case
    present()'s qualifies() filter correctly excludes from
    highest/other, which the route must now force-include honestly."""
    catalog = app_main.get_catalog()
    scan = {
        "ticker": ticker, "state": "NEW", "previous_state": None, "transition_rule": "no_qualifying_evidence_yet",
        "confidence": "LOW", "signal_type": "none", "confidence_reason": "No gates passed this scan.",
        "qualifies": False, "qualifying_days_count": 0,
        "gates_passed": [],
        "gates": {
            "dark_pool_anomaly": {"passed": False, "metrics": {"ratio": None}},
            "options_flow_anomaly": {"passed": False, "metrics": {}},
            "price_move": {"passed": False, "metrics": {}},
            "catalyst_identified": {"passed": False, "metrics": {}},
        },
        "stock_state": {"last_price": 10.0, "change_percent": 0.0},
        "raw": {"flow_alerts": [], "darkpool_trades": []},
    }
    catalog.catalog_signal_evaluation(ticker, scan, kind="on_demand")
    return scan


def test_evaluated_zero_gate_tickers_appear_in_radar_response_alongside_the_other_two_states(app_main, monkeypatch):
    tickers = ["NEVRX", "ARTNA", "MSEX", "QUALZ"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    # NEVRX: never cataloged at all.
    _seed_zero_gate_evaluation(app_main, "ARTNA")
    _seed_zero_gate_evaluation(app_main, "MSEX")
    _seed_qualifying_evaluation(app_main, "QUALZ")

    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_ticker = {e["ticker"]: e for e in body["highest"] + body["other"]}

    # THE core fix: every watchlist ticker must appear, regardless of
    # evaluation outcome -- this is the real bug, nothing else.
    assert set(by_ticker) == set(tickers), (
        f"expected all 4 watchlist tickers in the response, got {sorted(by_ticker)} -- "
        "ARTNA/MSEX missing means the route is still silently excluding evaluated-but-"
        "zero-gate tickers (the round 13 item 1 regression)"
    )

    # Three distinct, representable states.
    assert by_ticker["NEVRX"]["not_evaluated"] is True
    assert by_ticker["NEVRX"]["state"] is None
    assert not by_ticker["NEVRX"].get("zero_gate")

    for zg in ("ARTNA", "MSEX"):
        entry = by_ticker[zg]
        assert entry.get("zero_gate") is True, f"{zg} must be flagged zero_gate"
        assert entry.get("not_evaluated") in (False, None) or entry.get("not_evaluated") is False
        assert entry["state"] == "NEW", f"{zg} must show its REAL evaluated state, not a placeholder"
        assert entry.get("transition_rule") == "no_qualifying_evidence_yet"

    qual = by_ticker["QUALZ"]
    assert not qual.get("zero_gate")
    assert not qual.get("not_evaluated")
    assert qual["state"] == "BUILDING"

    # Zero-gate tickers must NEVER land in `highest` (never presented as
    # a qualifying/noteworthy signal) -- they belong in `other`, tagged.
    highest_tickers = {e["ticker"] for e in body["highest"]}
    assert "ARTNA" not in highest_tickers
    assert "MSEX" not in highest_tickers

    # Existing counts contract (re-verified after this deeper fix, per
    # the task's explicit requirement): watchlist_size counts everyone;
    # evaluated_count must count ARTNA/MSEX/QUALZ (evaluated) but not
    # NEVRX (never evaluated) -- zero_gate tickers WERE genuinely
    # evaluated, so they count here even though they don't "qualify".
    assert body["watchlist_size"] == 4
    assert body["evaluated_count"] == 3

    # S1/W2 security contract must hold even after this deeper fix:
    # strictly cache-only, zero real UW calls on a plain GET.
    assert mock_session.get.call_count == 0, (
        f"GET /api/radar made {mock_session.get.call_count} real UW call(s) -- "
        "item 1's fix must stay strictly read-only/cache-only."
    )
    assert body["served_from_cache"] is True


def test_zero_gate_tickers_never_counted_as_qualifying_by_the_backend_either(app_main, monkeypatch):
    """Belt-and-suspenders: the backend-side qualifies() semantics
    (tip_evidence.qualifies, untouched by this fix) must still say False
    for these items -- this fix forces VISIBILITY, never qualification."""
    from app.core import tip_evidence

    tickers = ["ZGON"]
    app_main._settings_store.set_watchlist(tickers)
    _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    scan = _seed_zero_gate_evaluation(app_main, "ZGON")
    item = app_main._to_evidence_item(scan)
    assert tip_evidence.qualifies(item) is False

    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    body = resp.json()
    entry = next(e for e in body["highest"] + body["other"] if e["ticker"] == "ZGON")
    assert entry["zero_gate"] is True
