"""Synthetic / context / corroborating gates have zero decision weight
(signal-integrity remediation, audit B2/E6).

Before: any_gate_passed counted historical_percentile (a static premium
bucket), persistence and price_confirmation, so the synthetic estimator alone
promoted NEW -> BUILDING, held BUILDING, and "reheated" FADING -> BUILDING.
"""
from app.core import scan_service, signal_engine as se

# options flow FAILS (no sweep, balanced sides) but premium is large enough
# that the synthetic bucket estimator would "pass".
QUIET_BIG = [{"type": "call", "total_premium": 12_000_000, "total_ask_premium": 5_000_000,
              "total_bid_premium": 5_000_000, "has_sweep": False}]


def scan(state, mode, flow=QUIET_BIG, darkpool=None, **kw):
    return scan_service.evaluate_ticker_scan("ABCD", flow, darkpool or [], state, 1, price_move_pct=0.5,
                                             mode=mode, **kw)


def test_demo_synthetic_bucket_is_shown_but_never_passes():
    r = scan(None, "demo")
    g = r["gates"]["historical_percentile"]
    assert g["status"] == "synthetic" and g["metrics"]["bucket_passed"] is True
    assert g["passed"] is False and g["decision_weight"] == "none"
    assert "historical_percentile" not in r["gates_passed"]
    assert r["independent_gates_passed"] == []


def test_synthetic_estimator_is_not_even_computed_in_live():
    r = scan(None, "live")
    assert r["gates"]["historical_percentile"]["status"] == "unsupported"


def test_synthetic_alone_never_opens_promotes_holds_or_revives():
    for mode in ("demo", "live"):
        assert scan(None, mode)["transition_rule"] == "no_qualifying_evidence_yet"
        assert scan("NEW", mode)["state"] == "NEW"
        assert scan("BUILDING", mode)["state"] == "FADING"
        assert scan("FADING", mode)["state"] == "FADING"


def test_price_confirmation_alone_cannot_hold_a_qualification():
    gates = se.evaluate_gates("ABCD", price_move_pct=2.0, direction_heuristic="bullish-lean")
    assert se.determine_transition("BUILDING", gates, 3, 2.0, "bullish-lean")[0] == "FADING"


def test_catalyst_flag_cannot_pass_in_live():
    r = scan(None, "live", catalyst_identified=True)
    assert r["gates"]["catalyst_identified"]["status"] == "unsupported"
    assert r["gates"]["catalyst_identified"]["passed"] is False


def test_live_never_uses_a_fixture_style_baseline_field():
    trade = [{"premium": 50_000_000, "avg_premium_baseline": 1_000_000, "executed_at": "2026-10-16T14:00:00Z"}]
    live = scan(None, "live", darkpool=trade)
    assert live["gates"]["dark_pool_anomaly"]["status"] == "insufficient"
    demo = scan(None, "demo", darkpool=trade)
    assert demo["gates"]["dark_pool_anomaly"]["status"] == "pass"
    assert demo["gates"]["dark_pool_anomaly"]["source"] == "demo_fixture_baseline"


def test_no_evaluable_independent_gate_means_insufficient_confidence():
    gates = se.evaluate_gates("ABCD", tier1_bootstrap=True, options_flow_status="insufficient", mode="live")
    conf, sig, _reason, qualifies = se.classify_confidence(gates)
    assert (conf, sig, qualifies) == ("insufficient", "insufficient_evidence", False)


def test_confidence_reason_never_mentions_probability():
    for state in (None, "BUILDING"):
        for mode in ("demo", "live"):
            reason = scan(state, mode)["confidence_reason"].lower()
            assert "probab" not in reason and "%" not in reason
