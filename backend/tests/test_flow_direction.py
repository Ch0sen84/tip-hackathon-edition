"""Call/put- and side-aware direction (signal-integrity remediation, audit
B3/E3/E4). The old heuristic read ONE ask-side share across calls+puts, so
aggressive put buying was labeled bullish."""
import pytest
from fastapi.testclient import TestClient

from app.core import scan_service
from app.core.flow_direction import classify_flow
from tests import recorded_uw
from tests.uw_fakes import fake_live_adapter, ok


def alert(kind, ask, bid, total=None, multileg=False):
    return {"type": kind, "total_ask_premium": ask, "total_bid_premium": bid,
            "total_premium": total if total is not None else ask + bid, "has_multileg": multileg}


def test_call_buying_is_a_bullish_lean():
    assert classify_flow([alert("call", 900, 100), alert("put", 100, 50)])["lean"] == "bullish-lean"


def test_put_buying_is_a_bearish_lean_even_though_ask_side_dominates():
    flows = [alert("put", 3_000_000, 200_000), alert("call", 500_000, 400_000)]
    assert classify_flow(flows)["lean"] == "bearish-lean"
    # the pre-remediation combined-ask-share heuristic called this bullish:
    combined_ask_share = 3_500_000 / (3_500_000 + 600_000)
    assert combined_ask_share >= 0.65


def test_selling_alone_produces_no_lean():
    d = classify_flow([alert("call", 0, 2_000_000), alert("put", 0, 1_000_000)])
    assert d["lean"] is None
    assert "no aggressive" in d["ambiguity"]
    assert d["observed"]["call_bid"] == 2_000_000  # still reported as an observation


def test_mixed_buying_is_ambiguous():
    d = classify_flow([alert("call", 1_000_000, 0), alert("put", 900_000, 0)])
    assert d["lean"] is None and "mixed" in d["ambiguity"]


def test_mostly_unsided_premium_is_ambiguous():
    d = classify_flow([alert("call", 100, 0, total=10_000)])
    assert d["lean"] is None and "without a clear side" in d["ambiguity"]


def test_multileg_alerts_are_excluded_from_the_lean():
    d = classify_flow([alert("put", 5_000_000, 0, multileg=True), alert("call", 900, 100)])
    assert d["lean"] == "bullish-lean"
    assert d["observed"]["multileg_alerts"] == 1


def test_missing_option_type_is_not_guessed():
    d = classify_flow([{"total_ask_premium": 1_000_000, "total_bid_premium": 0, "total_premium": 1_000_000}])
    assert d["lean"] is None
    assert d["observed"]["unknown_type_premium"] == 1_000_000


# --- recorded real data ------------------------------------------------------

def _normalized_flow(scan):
    adapter = fake_live_adapter({"flow-alerts": ok(scan["flow_alerts"])})
    return adapter.collect_flow_alerts(scan["ticker"])["rows"]


def test_pltr_recorded_payload_is_not_labeled_bullish():
    scan = recorded_uw.scan("PLTR", "2026-10-16T15:02")
    assert scan["captured_engine"]["state"] == "INVALIDATED"  # what 05c47ca did with this payload
    d = classify_flow(_normalized_flow(scan))
    assert d["lean"] == "bearish-lean"
    assert d["observed"]["put_ask"] > 2 * d["observed"]["call_ask"]


def test_pltr_recorded_payload_no_longer_falsely_invalidates(app_main, monkeypatch):
    """Real PLTR rows replayed through the real Live adapter, session
    collector and API at the captured instant."""
    from datetime import datetime, timezone

    from tests.uw_paginated import PaginatedUW

    scan = recorded_uw.scan("PLTR", "2026-10-16T15:02")
    uw = PaginatedUW(darkpool=scan["darkpool"], flow=scan["flow_alerts"],
                     static={"stock-state": ok(scan["stock_state"])})
    adapter = uw.adapter(["PLTR"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    monkeypatch.setattr(app_main, "_utcnow", lambda: datetime(2026, 10, 16, 15, 2, 27, tzinfo=timezone.utc))
    store = app_main.get_store("live")
    store.record_scan("PLTR", True, "NEW", scan_date="2026-10-15")
    store.record_scan("PLTR", True, "BUILDING", scan_date="2026-10-16")

    body = TestClient(app_main.app).post("/api/ticker/PLTR/refresh", json={"confirm": True}).json()

    assert body["stock_state"]["change_percent"] == pytest.approx(-3.456, abs=0.01)
    assert body["gates"]["price_confirmation"]["metrics"]["direction_heuristic"] == "bearish-lean"
    assert body["state"] != "INVALIDATED"
    # no session baseline exists in this capture: both gates are honestly
    # insufficient and the qualification is held (not invalidated, not faded)
    assert body["gates"]["options_flow_anomaly"]["status"] == "insufficient"
    assert body["transition_rule"] == "held_insufficient_evidence"


def test_pltr_lean_is_consistent_with_the_drop_even_when_options_flow_qualifies():
    """Engine path where the options gate DOES pass: the -3.46% move agrees
    with the put-dominated lean, so price corroborates instead of invalidating."""
    from datetime import date

    from app.core import lifecycle

    scan = recorded_uw.scan("PLTR", "2026-10-16T15:02")
    flows = _normalized_flow(scan)
    ctx = lifecycle.LifecycleContext(episode_id=1, state="BUILDING", opened_session="2026-10-15",
                                     reference_price=200.5, qualifying_sessions=("2026-10-15",),
                                     last_qualifying_session="2026-10-15")
    r = scan_service.evaluate_ticker_scan(
        "PLTR", flows, [], "BUILDING", 1, price_move_pct=-3.456, mode="live",
        options_flow_baseline={"status": "pass", "metrics": {"note": "assumed qualifying for this check"}},
        lifecycle_context=ctx, session_date=date(2026, 10, 16), price_last=193.57,
        evidence_fingerprint="pltr-1502")
    assert r["gates"]["price_confirmation"]["status"] == "pass"
    assert r["state"] == "BUILDING" and r["state"] != "INVALIDATED"


def test_recorded_scans_lean_always_matches_buy_side_dominance():
    for scan in recorded_uw.scans():
        d = classify_flow(_normalized_flow(scan))
        o = d["observed"]
        if d["lean"] == "bullish-lean":
            assert o["call_ask"] > o["put_ask"]
        elif d["lean"] == "bearish-lean":
            assert o["put_ask"] > o["call_ask"]


def test_scan_service_uses_the_call_put_aware_lean():
    flows = [alert("put", 3_000_000, 200_000), alert("call", 500_000, 400_000)]
    inputs = scan_service.build_gate_inputs("XYZ", flows, [], price_move_pct=-1.0)
    assert inputs["direction_heuristic"] == "bearish-lean"
