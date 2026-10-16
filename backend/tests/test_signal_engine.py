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

    def test_dark_pool_strong_ratio_is_medium_not_high(self):
        """SIGNAL-INTEGRITY RULE CHANGE: a single real gate -- even a
        strong dark-pool ratio/z-score -- can no longer reach "high" on
        its own. Only genuine >=2-category multi_signal_confirmation does."""
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 1.0, "bootstrap": False}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"
        assert sig_type == "single-signal-dark-pool"
        assert qualifies is True

    def test_dark_pool_weak_ratio_is_medium(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 1.2, "z_score": 1.0, "bootstrap": False}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"

    def test_historical_percentile_alone_never_qualifies(self):
        """SIGNAL-INTEGRITY RULE CHANGE: the SYNTHETIC historical_percentile
        gate has zero weight in confidence now, even when it passes strongly
        (percentile=0.98) -- it is excluded from INDEPENDENT_SIGNAL_GATES
        and classify_confidence no longer has a branch for it at all."""
        gates = make_gates(historical_percentile={"passed": True, "metrics": {"percentile": 0.98, "window_days": 30}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert (conf, sig_type, qualifies) == ("low", "none", False)

    def test_persistence_alone_never_qualifies(self):
        """SIGNAL-INTEGRITY RULE CHANGE: `persistence` is excluded from
        INDEPENDENT_SIGNAL_GATES (calendar-date-only, not an independent
        data source -- see signal_engine.py docstring) and no longer has
        its own confidence branch; passing alone stays low/non-qualifying."""
        gates = make_gates(persistence={"passed": True, "metrics": {"tier2_qualifying_prints": 3, "tier2_max_pct": 0.5, "is_new_qualifying_day": False}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert (conf, sig_type, qualifies) == ("low", "none", False)

    def test_price_confirmation_alone_is_medium_not_high(self):
        """price_confirmation is a real gate but is excluded from
        INDEPENDENT_SIGNAL_GATES (its direction input is sourced from the
        same options-flow feed as options_flow_anomaly) -- passing alone
        caps at medium, never high/CONFIRMED-eligible."""
        gates = make_gates(price_confirmation={"passed": True, "metrics": {"confirmed": True}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"
        assert sig_type == "single-signal-price-confirmation"

    def test_catalyst_identified_alone_is_medium_not_high(self):
        gates = make_gates(catalyst_identified={"passed": True, "metrics": {}})
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"
        assert sig_type == "single-signal-catalyst"

    def test_two_independent_categories_reach_high_confidence(self):
        """dark_pool_anomaly + options_flow_anomaly are two genuinely
        independent real data sources (dark-pool trades vs options flow) --
        this is the "2 independent categories CAN reach CONFIRMED" case."""
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 4.0, "bootstrap": False}},
            options_flow_anomaly={"passed": True, "metrics": {}},
        )
        gates["multi_signal_confirmation"] = {
            "passed": True,
            "metrics": {"contributing_gates": ["dark_pool_anomaly", "options_flow_anomaly"]},
        }
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "high"
        assert sig_type == "multi-signal"

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
        assert rule == "first_qualifying_gate"

    def test_new_ticker_first_eval_with_zero_qualifying_gates_gets_honest_reason(self):
        """ZERO-GATE LABEL FIX (Chad-approved, 2026-10-16): the confirmed
        real-world defect -- tickers ARTNA/MSEX/CZWI (Windows QA) showing
        ZERO qualifying gates on their FIRST evaluation (current_state is
        None) were still labeled "first_qualifying_gate", a reason string
        that falsely implied a gate had actually qualified. The lifecycle
        state is still correctly NEW; only the reason string changes when
        no gate passed on this first scan."""
        gates = make_gates()  # nothing passing -- zero qualifying evidence
        state, rule = se.determine_transition(None, gates, 0, None, None)
        assert state == "NEW"
        assert rule == "no_qualifying_evidence_yet"

    def test_new_ticker_first_eval_with_a_qualifying_gate_is_unchanged(self):
        """The real qualifying path on first evaluation must be completely
        unchanged by the zero-gate label fix above."""
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 4.0, "bootstrap": False}})
        state, rule = se.determine_transition(None, gates, 0, None, None)
        assert state == "NEW"
        assert rule == "first_qualifying_gate"

    def test_new_to_building_on_single_signal_reeval(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("NEW", gates, 1, None, None)
        assert state == "BUILDING"

    def test_new_to_confirmed_on_multi_signal(self):
        gates = make_gates(multi_signal_confirmation={"passed": True, "metrics": {"contributing_gates": ["a", "b"]}})
        state, rule = se.determine_transition("NEW", gates, 1, None, None)
        assert state == "CONFIRMED"

    def test_new_stays_new_on_reevaluation_with_zero_qualifying_gates(self):
        """L1 FIX (Chad-approved, 2026-10-16): the confirmed defect -- a
        second scan of a NEW ticker with NO qualifying gate on the CURRENT
        evaluation (repeated same-day scan, zero new evidence) must NOT
        promote to BUILDING. Before this fix, determine_transition's NEW
        branch returned ("BUILDING", "single_signal_reevaluated")
        unconditionally whenever `confirmed` was False, regardless of
        whether any gate passed at all this scan."""
        gates = make_gates()  # nothing passing -- zero qualifying evidence
        state, rule = se.determine_transition("NEW", gates, 0, None, None)
        assert state == "NEW"
        assert rule == "no_gate_passed_remains_new"

    def test_new_promotes_to_building_when_genuinely_new_qualifying_evidence_arrives(self):
        """The real promotion path must still work: a NEW ticker whose
        CURRENT scan has a genuinely qualifying single gate (e.g. a fresh
        dark-pool anomaly) still correctly promotes to BUILDING."""
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 4.0, "bootstrap": False}})
        state, rule = se.determine_transition("NEW", gates, 0, None, None)
        assert state == "BUILDING"
        assert rule == "single_signal_reevaluated"

    def test_new_repeated_identical_scan_with_no_evidence_never_drifts_to_building(self):
        """Simulates multiple repeated same-day re-evaluations (e.g. a
        polling refresh hitting the cache / re-scoring the same empty
        result) against a NEW ticker that never actually produced a
        qualifying gate -- it must stay NEW across every repetition, never
        drift to BUILDING just from being evaluated more than once."""
        gates = make_gates()
        state = "NEW"
        for _ in range(5):
            state, rule = se.determine_transition(state, gates, 0, None, None)
        assert state == "NEW"

    def test_incomplete_historical_coverage_bootstrap_gate_does_not_count_as_qualifying(self):
        """A dark_pool_anomaly gate that failed specifically because of
        insufficient/incomplete historical coverage (tier1_bootstrap=True,
        per dark_pool_baseline.py's `anomaly_qualifies` structural guard)
        must never be misread as qualifying evidence for the NEW ->
        BUILDING promotion -- evaluate_gates already sets `passed=False`
        whenever bootstrap is True (`tier1_hit and not tier1_bootstrap`),
        so this pins that the state machine correctly treats it as
        no-qualifying-gate, not as a pass."""
        gates = make_gates(
            dark_pool_anomaly={"passed": False, "metrics": {"ratio": None, "z_score": None, "bootstrap": True}},
        )
        state, rule = se.determine_transition("NEW", gates, 0, None, None)
        assert state == "NEW"
        assert rule == "no_gate_passed_remains_new"

    def test_single_gate_does_not_confirm_even_with_two_qualifying_days(self):
        """SIGNAL-INTEGRITY RULE CHANGE: the old `qualifying_days_count >= 2`
        bypass to CONFIRMED is removed. A single real gate (dark_pool_anomaly
        only here, multi_signal_confirmation still false) seen on 2+
        calendar days must NOT reach CONFIRMED -- this was the exact
        loophole (persistence of unchanged single-category evidence
        masquerading as independent corroboration) the rule change closes."""
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("NEW", gates, 2, None, None)
        assert state == "BUILDING"

    def test_two_independent_gates_do_confirm(self):
        """Two genuinely independent real gates (dark_pool_anomaly +
        options_flow_anomaly -- different UW feeds) CAN reach CONFIRMED,
        even on the very first re-evaluation (qualifying_days_count=0)."""
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}},
            options_flow_anomaly={"passed": True, "metrics": {}},
        )
        gates["multi_signal_confirmation"] = {
            "passed": True,
            "metrics": {"contributing_gates": ["dark_pool_anomaly", "options_flow_anomaly"]},
        }
        state, rule = se.determine_transition("NEW", gates, 0, None, None)
        assert state == "CONFIRMED"

    def test_repeated_same_day_scans_cannot_manufacture_confirmation(self):
        """Re-running determine_transition multiple times against the SAME
        unchanged single-gate snapshot (simulating repeated same-day scans
        -- qualifying_days_count never increments for a repeated same-day
        scan per storage.py's COUNT(DISTINCT scan_date)) must never drift
        toward CONFIRMED just from being evaluated repeatedly."""
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state = None
        for _ in range(5):
            state, rule = se.determine_transition(state, gates, 1, None, None)
        assert state in ("NEW", "BUILDING")
        assert state != "CONFIRMED"

    def test_stale_confirmed_is_demoted_when_multi_signal_no_longer_holds(self):
        """SIGNAL-INTEGRITY RULE CHANGE item 4: a ticker already sitting in
        CONFIRMED from a PRIOR scan (e.g. reached under the old rules) is
        demoted to BUILDING the moment multi_signal_confirmation no longer
        passes on the current scan, as long as at least one real gate still
        passed -- never silently kept as CONFIRMED."""
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("CONFIRMED", gates, 3, None, None)
        assert state == "BUILDING"
        assert rule == "demoted_lost_multi_signal_confirmation"

    def test_stale_confirmed_fades_when_nothing_passes_at_all(self):
        gates = make_gates()
        state, rule = se.determine_transition("CONFIRMED", gates, 3, None, None)
        assert state == "FADING"

    def test_stale_stable_is_demoted_when_multi_signal_no_longer_holds(self):
        gates = make_gates(options_flow_anomaly={"passed": True, "metrics": {}})
        state, rule = se.determine_transition("STABLE", gates, 10, None, None)
        assert state == "BUILDING"
        assert rule == "demoted_lost_multi_signal_confirmation"

    def test_building_to_fading_when_nothing_passes(self):
        gates = make_gates()
        state, rule = se.determine_transition("BUILDING", gates, 1, None, None)
        assert state == "FADING"

    def test_building_stays_building_on_continued_single_signal(self):
        gates = make_gates(dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}})
        state, rule = se.determine_transition("BUILDING", gates, 1, None, None)
        assert state == "BUILDING"

    def test_confirmed_to_stable_after_three_qualifying_days_with_price_confirmation(self):
        """STABLE now additionally requires multi_signal_confirmation to
        still hold on the transitioning scan itself (see signal_engine.py
        docstring rule 5) -- so this needs a genuine 2-category pass, not
        just dark_pool_anomaly + price_confirmation (price_confirmation is
        excluded from the independent tally, see INDEPENDENT_SIGNAL_GATES)."""
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}},
            options_flow_anomaly={"passed": True, "metrics": {}},
            price_confirmation={"passed": True, "metrics": {}},
        )
        gates["multi_signal_confirmation"] = {
            "passed": True,
            "metrics": {"contributing_gates": ["dark_pool_anomaly", "options_flow_anomaly"]},
        }
        state, rule = se.determine_transition("CONFIRMED", gates, se.STABLE_MIN_QUALIFYING_DAYS, 2.0, "bullish-lean")
        assert state == "STABLE"

    def test_confirmed_single_gate_with_price_confirmation_does_not_reach_stable(self):
        """The narrower companion case: dark_pool_anomaly + price_confirmation
        passing is only 1 independent category (price_confirmation doesn't
        count) -- multi_signal_confirmation is false, so even with
        qualifying_days_count satisfied this must demote, not promote."""
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 2.0, "z_score": 1.0, "bootstrap": False}},
            price_confirmation={"passed": True, "metrics": {}},
        )
        state, rule = se.determine_transition("CONFIRMED", gates, se.STABLE_MIN_QUALIFYING_DAYS, 2.0, "bullish-lean")
        assert state == "BUILDING"

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


class TestClassifyConfidenceNoneTierOneZRegression:
    """Pre-Phase-D, Chad-approved narrow fix: classify_confidence used to
    crash with TypeError on `f"...z={z:.1f}"` whenever tier1_ratio was
    truthy but tier1_z was None. See signal_engine.py's classify_confidence
    for the fix itself; this class is the required regression coverage
    (zero-variance baseline, None z-score generally, and a golden-value
    snapshot proving the ordinary case is byte-for-byte unchanged)."""

    def test_zero_variance_historical_baseline_no_longer_crashes(self):
        """A real scenario this fix targets: dark_pool_baseline.compute_baseline
        can produce `z_score=None` even with `ratio_vs_mean` present whenever
        historical stdev is exactly 0.0 (< 2 eligible historical sessions, or
        every eligible session happened to have identical cumulative premium)
        -- see compute_baseline's `z = ... if stdev > 0 else None`. Before
        this fix, feeding that combination into classify_confidence crashed.
        """
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 4.5, "z_score": None, "bootstrap": False}}
        )
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)  # must not raise
        assert conf == "medium"
        assert sig_type == "single-signal-dark-pool"
        assert qualifies is True
        assert "4.5x avg" in reason
        assert "z=" not in reason  # no z-score clause when z is genuinely unavailable

    def test_missing_z_score_generally_does_not_crash(self):
        """Same None-z fallback, independent of the specific zero-variance
        baseline scenario above -- any caller that supplies tier1_z=None
        with a real ratio must get a sane non-crashing result."""
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 10.0, "z_score": None, "bootstrap": False}}
        )
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"
        assert qualifies is True
        assert "10.0x avg" in reason

    def test_missing_ratio_and_missing_z_both_none_does_not_crash(self):
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": None, "z_score": None, "bootstrap": False}}
        )
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"
        assert reason == "Single-category dark-pool anomaly only -- not independently corroborated"

    def test_golden_value_ordinary_ratio_and_z_case_unchanged_by_this_fix(self):
        """SNAPSHOT/GOLDEN-VALUE test: a normal case where BOTH ratio and
        z_score are real numbers must produce the EXACT SAME reason string
        this function always produced before the None-z fix -- proving the
        fix changed nothing about the pre-existing, already-working
        behavior. This is the literal pre-fix f-string's output for these
        inputs, pinned down verbatim."""
        gates = make_gates(
            dark_pool_anomaly={"passed": True, "metrics": {"ratio": 5.0, "z_score": 3.25, "bootstrap": False}}
        )
        conf, sig_type, reason, qualifies = se.classify_confidence(gates)
        assert conf == "medium"
        assert sig_type == "single-signal-dark-pool"
        assert qualifies is True
        assert reason == (
            "Single-category dark-pool anomaly only (5.0x avg, z=3.2) -- "
            "real, but not independently corroborated by a 2nd category"
        )
