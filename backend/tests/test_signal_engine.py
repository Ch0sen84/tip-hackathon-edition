"""Tests for the ported pure signal_engine logic. Inputs are synthetic
gate-level dicts (not fixture files) so these tests pin down the
engine's state-machine behavior directly, independent of scan_service's
heuristics for deriving gate inputs from raw UW data."""
from app.core import signal_engine as se


def make_gates(**overrides):
    """A baseline 'nothing passed' gate set, with overrides for specific
    named gates so each test only has to state what it cares about."""
    gates = {
        "dark_pool_anomaly": {"passed": False, "metrics": {"ratio": None, "z_score": None, "bootstrap": False}},
        "options_flow_anomaly": {"passed": False, "metrics": {}},
        "historical_percentile": {"passed": False, "metrics": {}},
        "persistence": {"passed": False, "metrics": {"tier2_qualifying_prints": 0, "tier2_max_pct": 0.0, "is_new_qualifying_day": False}},
        "price_confirmation": {"passed": False, "metrics": {}},
        "multi_signal_confirmation": {"passed": False, "metrics": {"contributing_gates": []}},
        "catalyst_identified": {"passed": False, "metrics": {}},
        "data_quality": {"passed": True, "metrics": {"api_errors": 0, "bootstrap": False}},
    }
    for k, v in overrides.items():
        gates[k] = v
    return gates


class TestEvaluateGates:
    def test_dark_pool_anomaly_passes_when_tier1_hit_and_not_bootstrap(self):
        gates = se.evaluate_gates("ABC", tier1_hit=True, tier1_bootstrap=False, tier1_ratio=5.0, tier1_z=4.0)
        assert gates["dark_pool_anomaly"]["passed"] is True
        assert gates["dark_pool_anomaly"]["metrics"]["ratio"] == 5.0

    def test_dark_pool_anomaly_blocked_by_bootstrap(self):
        gates = se.evaluate_gates("ABC", tier1_hit=True, tier1_bootstrap=True, tier1_ratio=5.0)
        assert gates["dark_pool_anomaly"]["passed"] is False

    def test_options_flow_anomaly_passes_on_flag(self):
        gates = se.evaluate_gates("ABC", options_flow_hit=True, options_flow_metrics={"foo": 1})
        assert gates["options_flow_anomaly"]["passed"] is True
        assert gates["options_flow_anomaly"]["metrics"] == {"foo": 1}

    def test_historical_percentile_uses_default_synthetic_estimator(self):
        gates = se.evaluate_gates("ABC", today_premium=60_000_000)
        assert gates["historical_percentile"]["passed"] is True
        assert gates["historical_percentile"]["metrics"]["synthetic"] is True

    def test_historical_percentile_not_evaluated_without_premium(self):
        gates = se.evaluate_gates("ABC")
        assert gates["historical_percentile"]["passed"] is False
        assert "reason" in gates["historical_percentile"]["metrics"]

    def test_custom_percentile_gate_fn_is_used_instead_of_default(self):
        calls = []

        def fake_gate(ticker, premium):
            calls.append((ticker, premium))
            return {"passed": True, "percentile": 0.99, "window_days": 10}

        gates = se.evaluate_gates("XYZ", today_premium=1000, percentile_gate_fn=fake_gate)
        assert calls == [("XYZ", 1000)]
        assert gates["historical_percentile"]["passed"] is True
        assert gates["historical_percentile"]["metrics"]["window_days"] == 10

    def test_persistence_passes_on_new_qualifying_day(self):
        gates = se.evaluate_gates("ABC", is_new_qualifying_day=True)
        assert gates["persistence"]["passed"] is True

    def test_price_confirmation_bullish_lean_confirmed(self):
        gates = se.evaluate_gates("ABC", price_move_pct=2.0, direction_heuristic="bullish-lean")
        assert gates["price_confirmation"]["passed"] is True

    def test_price_confirmation_bullish_lean_not_confirmed_when_price_down(self):
        gates = se.evaluate_gates("ABC", price_move_pct=-2.0, direction_heuristic="bullish-lean")
        assert gates["price_confirmation"]["passed"] is False

    def test_multi_signal_confirmation_needs_two_of_four(self):
        gates = se.evaluate_gates(
            "ABC", tier1_hit=True, tier1_bootstrap=False, tier1_ratio=5.0,
            options_flow_hit=True, options_flow_metrics={},
        )
        assert gates["multi_signal_confirmation"]["passed"] is True
        assert set(gates["multi_signal_confirmation"]["metrics"]["contributing_gates"]) == {
            "dark_pool_anomaly", "options_flow_anomaly",
        }

    def test_multi_signal_confirmation_false_on_single_gate(self):
        gates = se.evaluate_gates("ABC", tier1_hit=True, tier1_bootstrap=False, tier1_ratio=5.0)
        assert gates["multi_signal_confirmation"]["passed"] is False

    def test_data_quality_fails_with_api_errors(self):
        gates = se.evaluate_gates("ABC", api_errors=2)
        assert gates["data_quality"]["passed"] is False

    def test_data_quality_fails_when_bootstrap(self):
        gates = se.evaluate_gates("ABC", tier1_bootstrap=True)
        assert gates["data_quality"]["passed"] is False

    def test_no_gates_pass_on_empty_input_nothing_crashes(self):
        gates = se.evaluate_gates("ABC")
        assert all(name in gates for name in se.GATE_NAMES)
        assert gates["dark_pool_anomaly"]["passed"] is False
        assert gates["options_flow_anomaly"]["passed"] is False


class TestClassifyConfidence:
    def test_watchlist_overrides_everything(self):
        gates = make_gates()
        conf, sig_type, reason, qualifies = se.classify_confidence(gates, is_watchlist=True)
        assert (conf, sig_type, qualifies) == ("manual", "watchlist-manual", True)

    def test_multi_signal_is_high_confidence(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["dark_pool_anomaly", "options_flow_anomaly"]}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "high"
        assert sig_type == "multi-signal"
        assert qualifies is True

    def test_dark_pool_strong_ratio_is_high(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 1.0, "bootstrap": False}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "high"
        assert sig_type == "t1-baseline"

    def test_dark_pool_weak_ratio_is_medium(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 1.2, "z_score": 1.0, "bootstrap": False}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"

    def test_historical_percentile_high_at_97(self):
        gates = make_gates(historical_percentile={"passed": True, "metrics": {"percentile": 0.98, "window_days": 30}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "high"
        assert sig_type == "historical-percentile"

    def test_persistence_with_prints_qualifies(self):
        gates = make_gates(persistence={"passed": True, "metrics": {"tier2_qualifying_prints": 3, "tier2_max_pct": 0.5, "is_new_qualifying_day": False}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "high"
        assert sig_type == "t2-pctvol"

    def test_bootstrap_only_is_low_not_qualifying(self):
        gates = make_gates(dark_pool_anomaly={"passed": False, "metrics": {"ratio": None, "z_score": None, "bootstrap": True}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert (conf, sig_type, qualifies) == ("low", "t1-bootstrap", False)

    def test_nothing_passed_is_low_none(self):
        gates = make_gates()
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert (conf, sig_type, qualifies) == ("low", "none", False)


class TestDetermineTransition:
    def test_new_ticker_with_no_current_state_is_new(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition(None, gates, 0, None, None)
        assert state == "NEW"

    def test_new_to_building_on_single_signal_reeval(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("NEW", gates, 1, None, None)
        assert state == "BUILDING"

    def test_new_to_confirmed_on_multi_signal(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["a", "b"]}})
        state, rule = se.determine_transition("NEW", gates, 1, None, None)
        assert state == "CONFIRMED"

    def test_new_to_confirmed_on_two_plus_qualifying_days(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("NEW", gates, 2, None, None)
        assert state == "CONFIRMED"

    def test_building_to_fading_when_nothing_passes(self):
        gates = make_gates()
        state, rule = se.determine_transition("BUILDING", gates, 1, None, None)
        assert state == "FADING"

    def test_building_stays_building_on_continued_single_signal(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("BUILDING", gates, 1, None, None)
        assert state == "BUILDING"

    def test_confirmed_to_stable_after_three_qualifying_days_with_price_confirmation(self):
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}},
            price_confirmation={"passed": True, "metrics": {}},
        )
        state, rule = se.determine_transition("CONFIRMED", gates, se.STABLE_MIN_QUALIFYING_DAYS, 2.0, "bullish-lean")
        assert state == "STABLE"

    def test_confirmed_to_fading_when_nothing_passes(self):
        gates = make_gates()
        state, rule = se.determine_transition("CONFIRMED", gates, 3, None, None)
        assert state == "FADING"

    def test_stable_to_fading_when_nothing_passes(self):
        gates = make_gates()
        state, rule = se.determine_transition("STABLE", gates, 5, None, None)
        assert state == "FADING"

    def test_fading_reheats_to_confirmed_on_multi_signal(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["a", "b"]}})
        state, rule = se.determine_transition("FADING", gates, 1, None, None)
        assert state == "CONFIRMED"

    def test_fading_reheats_to_building_on_single_signal(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("FADING", gates, 1, None, None)
        assert state == "BUILDING"

    def test_fading_stays_fading_when_still_nothing(self):
        gates = make_gates()
        state, rule = se.determine_transition("FADING", gates, 1, None, None)
        assert state == "FADING"

    def test_invalidated_is_terminal(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["a", "b"]}})
        state, rule = se.determine_transition("INVALIDATED", gates, 5, None, None)
        assert state == "INVALIDATED"

    def test_adverse_price_move_invalidates_bullish_lean(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["a", "b"]}})
        state, rule = se.determine_transition("CONFIRMED", gates, 3, -4.0, "bullish-lean")
        assert state == "INVALIDATED"

    def test_adverse_price_move_invalidates_bearish_lean(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["a", "b"]}})
        # bearish-lean expects price to fall; here it rose 4% -- adverse.
        state, rule = se.determine_transition("CONFIRMED", gates, 3, 4.0, "bearish-lean")
        assert state == "INVALIDATED"

    def test_favorable_move_does_not_invalidate(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("CONFIRMED", gates, 2, 4.0, "bullish-lean")
        assert state != "INVALIDATED"
