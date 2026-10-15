"""DECISION 2 regression tests: dark_pool_anomaly must report
insufficient-history (bootstrap) rather than PASS when no real multi-day
baseline exists for a ticker's dark-pool premium -- the exact real-UW-data
shape LiveUWAdapter.get_darkpool_trades() returns (no `avg_premium_baseline`
field at all; see live_uw_adapter.py and scan_service._darkpool_metrics).

Never build a synthetic/fabricated baseline substitute here (explicitly
forbidden) -- these tests lock in the honest "insufficient history" result
as the permanent contract, not a get-it-passing-somehow shortcut.
"""
from app.core import scan_service
from app.core import signal_engine as se


def _real_shaped_darkpool_trades(premium: float) -> list:
    """Shaped exactly like LiveUWAdapter.get_darkpool_trades()'s real
    output: real UW darkpool fields only, NEVER an avg_premium_baseline key
    (that field only ever appears in this repo's demo/fixture JSON -- see
    fixtures/README_SYNTHETIC.md)."""
    return [{
        "ticker": "NVDA", "premium": premium, "size": 500_000,
        "price": 182.4, "executed_at": "2026-10-15T14:00:00Z",
    }]


class TestDarkPoolNeverPassesWithoutRealBaseline:
    def test_real_shaped_trade_with_huge_premium_still_does_not_pass(self):
        """Even a huge, genuinely real premium figure must not make the
        gate PASS when there is no real baseline to compare it against --
        size alone is never a substitute for a statistical anomaly claim."""
        inputs = scan_service.build_gate_inputs(
            "NVDA", flow_alerts=[], darkpool_trades=_real_shaped_darkpool_trades(500_000_000),
        )
        assert inputs["tier1_bootstrap"] is True
        assert inputs["tier1_hit"] is False
        gates = se.evaluate_gates(**inputs)
        assert gates["dark_pool_anomaly"]["passed"] is False
        assert gates["dark_pool_anomaly"]["metrics"]["bootstrap"] is True
        assert gates["dark_pool_anomaly"]["metrics"]["ratio"] is None

    def test_real_shaped_trade_never_reaches_confirmed_or_high(self):
        inputs = scan_service.build_gate_inputs(
            "NVDA", flow_alerts=[], darkpool_trades=_real_shaped_darkpool_trades(500_000_000),
        )
        gates = se.evaluate_gates(**inputs)
        confidence, signal_type, reason, qualifies = se.classify_confidence(gates)
        assert confidence == "low"
        assert signal_type == "t1-bootstrap"
        assert qualifies is False
        assert "insufficient history" in reason.lower()
        state, rule = se.determine_transition(None, gates, 0, None, None)
        assert state == "NEW"

    def test_no_darkpool_trades_at_all_is_also_bootstrap_not_pass(self):
        inputs = scan_service.build_gate_inputs("NVDA", flow_alerts=[], darkpool_trades=[])
        gates = se.evaluate_gates(**inputs)
        assert gates["dark_pool_anomaly"]["passed"] is False
        assert gates["dark_pool_anomaly"]["metrics"]["bootstrap"] is True

    def test_fixture_provided_baseline_is_a_demo_only_input_not_a_live_capability(self):
        """Demo/synthetic fixtures are allowed to carry an explicit
        avg_premium_baseline field (clearly a fixture-authoring choice, see
        fixtures/README_SYNTHETIC.md) -- this is NOT the same as Live Mode
        fabricating one. Confirms the gate DOES behave differently (can
        genuinely pass) only when a real-looking baseline value is present,
        proving the bootstrap result above isn't just a hardcoded False."""
        trades_with_baseline = [{
            "ticker": "DEMO", "premium": 20_000_000, "size": 400_000,
            "price": 55.0, "executed_at": "2026-10-15T14:00:00Z",
            "avg_premium_baseline": 2_000_000,
        }]
        inputs = scan_service.build_gate_inputs("DEMO", flow_alerts=[], darkpool_trades=trades_with_baseline)
        assert inputs["tier1_bootstrap"] is False
        gates = se.evaluate_gates(**inputs)
        assert gates["dark_pool_anomaly"]["passed"] is True
        assert gates["dark_pool_anomaly"]["metrics"]["bootstrap"] is False
