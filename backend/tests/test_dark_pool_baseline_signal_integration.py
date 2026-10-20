"""Phase C: proves the new baseline module is wired into
signal_engine.evaluate_gates' EXISTING interface (tier1_hit/
tier1_bootstrap/tier1_ratio/tier1_z) WITHOUT changing evaluate_gates,
classify_confidence, or determine_transition themselves -- and that the
two hardest requirements hold end to end through scan_service:

  (e) a real, STRONG-tier, high-confidence dark-pool-anomaly pass -- now
      flowing through a genuine baseline computation instead of being
      impossible in Live Mode -- STILL caps at "medium"/BUILDING, never
      CONFIRMED, alone. The 2-independent-category rule is unchanged.
  (f) incomplete/unknown/insufficient-tier coverage cannot produce a
      passing dark_pool_anomaly gate no matter how extreme the day's raw
      numbers are -- proven directly against signal_engine.evaluate_gates,
      not just against the baseline module in isolation.
"""
from __future__ import annotations

import datetime as dt

from app.core import dark_pool_baseline as dpb
from app.core import scan_service
from app.core import signal_engine as se
from app.core import trading_calendar as tc
from app.dark_pool_pagination import COMPLETE, INCOMPLETE, UNKNOWN

TICKER = "FLUX"


def _coverage_row(coverage_date: str, status: str) -> dict:
    return {"coverage_date": coverage_date, "status": status, "pages_walked": 5,
            "session_start_verified": status == COMPLETE, "retrieved_at": "2026-01-01T00:00:00+00:00"}


def _obs(event_time_iso: str, premium: float) -> dict:
    return {"event_time": event_time_iso, "premium": premium}


def _tier1_kwargs(baseline_result: dpb.BaselineResult) -> dict:
    """evaluate_gates only accepts tier1_hit/tier1_bootstrap/tier1_ratio/
    tier1_z (not `total_premium`, which scan_service.build_gate_inputs
    separately folds into `today_premium` for the historical_percentile
    gate) -- this strips BaselineResult.as_gate_inputs() down to exactly
    the keyword arguments evaluate_gates itself expects."""
    gi = baseline_result.as_gate_inputs()
    return {k: v for k, v in gi.items() if k != "total_premium"}


def _nth_trading_day_before(anchor: dt.date, n: int) -> dt.date:
    d = anchor
    found = 0
    while found < n:
        d -= dt.timedelta(days=1)
        if tc.is_trading_day(d):
            found += 1
    return d


def _strong_qualifying_baseline_result(today: dt.date) -> dpb.BaselineResult:
    """A genuinely STRONG-tier (60+ eligible sessions), statistically
    qualifying dark-pool baseline -- the strongest possible single piece
    of dark-pool evidence this module can produce."""
    dates = [_nth_trading_day_before(today, n) for n in range(1, 61)]
    coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
    # Small, realistic day-to-day variance (9,000-11,000) -- avoids a
    # degenerate zero-stdev fixture (z_score would be None, which is a
    # legitimate edge case but isn't what this particular test is about).
    obs_by_date = {
        d.isoformat(): [_obs(tc.session_start_utc(d).isoformat(), 10_000.0 + (i % 5) * 100.0)]
        for i, d in enumerate(dates)
    }
    today_open = tc.session_start_utc(today)
    today_obs = [_obs(today_open.isoformat(), 80_000.0)]  # 8x every historical session, same elapsed point
    as_of = today_open + dt.timedelta(minutes=1)
    return dpb.compute_baseline(
        ticker=TICKER, today_date=today, as_of_utc=as_of,
        today_coverage_status=None, today_observations=today_obs,
        historical_coverage_rows=coverage_rows, historical_observations_by_date=obs_by_date,
    )


class TestStrongDarkPoolAnomalyAloneStillCannotReachConfirmed:
    def test_evaluate_gates_dark_pool_anomaly_passes_but_not_multi_signal(self):
        today = dt.date(2026, 3, 12)
        result = _strong_qualifying_baseline_result(today)
        assert result.tier == dpb.TIER_STRONG
        assert result.anomaly_qualifies is True
        gate_inputs = result.as_gate_inputs()
        assert gate_inputs["tier1_hit"] is True
        assert gate_inputs["tier1_bootstrap"] is False

        gates = se.evaluate_gates(TICKER, **_tier1_kwargs(result))
        assert gates["dark_pool_anomaly"]["passed"] is True
        # No options-flow/catalyst evidence supplied at all -- the
        # 2-independent-category bar must NOT be cleared by dark-pool
        # evidence alone, exactly as today (pre-Phase-C, this gate could
        # never even BE real in Live Mode; now that it can be real, the
        # rule governing it must still be completely unchanged).
        assert gates["multi_signal_confirmation"]["passed"] is False

        confidence, signal_type, reason, qualifies = se.classify_confidence(gates)
        assert confidence == "medium"
        assert signal_type == "single-signal-dark-pool"
        assert qualifies is True  # it IS a real, qualifying single signal...

        new_state, rule_id = se.determine_transition(
            current_state="NEW", gates=gates, qualifying_days_count=5,
            price_move_pct=None, direction_heuristic=None,
        )
        assert new_state == "BUILDING"  # ...but never CONFIRMED alone.

    def test_end_to_end_through_scan_service_evaluate_ticker_scan(self):
        """Same proof, but through the actual integration point
        (scan_service.evaluate_ticker_scan's `catalog_dark_pool_baseline`
        parameter) rather than calling signal_engine directly -- this is
        the literal code path main.py's Live Mode route now exercises."""
        today = dt.date(2026, 3, 12)
        result = _strong_qualifying_baseline_result(today)

        scan = scan_service.evaluate_ticker_scan(
            ticker=TICKER, flow_alerts=[], darkpool_trades=[],
            current_state="NEW", qualifying_days_count=1,
            catalog_dark_pool_baseline=result.as_gate_inputs(),
        )
        assert scan["gates"]["dark_pool_anomaly"]["passed"] is True
        assert scan["gates"]["multi_signal_confirmation"]["passed"] is False
        assert scan["confidence"] == "medium"
        assert scan["state"] != "CONFIRMED"

    def test_omitting_catalog_baseline_reproduces_pre_phase_c_behavior_exactly(self):
        """Regression: the default (no catalog_dark_pool_baseline
        supplied) path must still behave byte-for-byte as before this
        phase -- same darkpool_trades heuristic, same bootstrap-without-
        baseline result, for every pre-existing caller."""
        scan_without_baseline = scan_service.evaluate_ticker_scan(
            ticker=TICKER, flow_alerts=[], darkpool_trades=[{"premium": 500_000}],
            current_state=None, qualifying_days_count=0,
        )
        assert scan_without_baseline["gates"]["dark_pool_anomaly"]["metrics"]["bootstrap"] is True
        assert scan_without_baseline["gates"]["dark_pool_anomaly"]["passed"] is False


class TestIncompleteCoverageCannotProducePassingGateRegardlessOfMagnitude:
    def _extreme_but_structurally_blocked_result(self, today: dt.date, today_status) -> dpb.BaselineResult:
        dates = [_nth_trading_day_before(today, n) for n in range(1, 61)]
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        obs_by_date = {d.isoformat(): [_obs(tc.session_start_utc(d).isoformat(), 1_000.0)] for d in dates}
        today_open = tc.session_start_utc(today)
        # 500x the historical mean -- about as extreme as a real number gets.
        today_obs = [_obs(today_open.isoformat(), 500_000.0)]
        as_of = today_open + dt.timedelta(minutes=1)
        return dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=today_status, today_observations=today_obs,
            historical_coverage_rows=coverage_rows, historical_observations_by_date=obs_by_date,
        )

    def test_incomplete_today_status_cannot_pass_the_gate(self):
        today = dt.date(2026, 3, 12)
        result = self._extreme_but_structurally_blocked_result(today, INCOMPLETE)
        assert result.ratio_vs_mean >= 100  # the raw magnitude really is extreme
        gates = se.evaluate_gates(TICKER, **_tier1_kwargs(result))
        assert gates["dark_pool_anomaly"]["passed"] is False

    def test_unknown_today_status_cannot_pass_the_gate(self):
        today = dt.date(2026, 3, 12)
        result = self._extreme_but_structurally_blocked_result(today, UNKNOWN)
        assert result.ratio_vs_mean >= 100
        gates = se.evaluate_gates(TICKER, **_tier1_kwargs(result))
        assert gates["dark_pool_anomaly"]["passed"] is False

    def test_preliminary_tier_cannot_pass_the_gate_even_with_a_valid_today_session(self):
        today = dt.date(2026, 3, 12)
        dates = [_nth_trading_day_before(today, n) for n in range(1, 8)]  # 7 sessions -- PRELIMINARY
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        obs_by_date = {d.isoformat(): [_obs(tc.session_start_utc(d).isoformat(), 1_000.0)] for d in dates}
        today_open = tc.session_start_utc(today)
        today_obs = [_obs(today_open.isoformat(), 10_000_000.0)]  # absurd
        as_of = today_open + dt.timedelta(minutes=1)
        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=None, today_observations=today_obs,
            historical_coverage_rows=coverage_rows, historical_observations_by_date=obs_by_date,
        )
        assert result.tier == dpb.TIER_PRELIMINARY
        gates = se.evaluate_gates(TICKER, **_tier1_kwargs(result))
        assert gates["dark_pool_anomaly"]["passed"] is False
        # data_quality also keys off tier1_bootstrap -- confirms the
        # existing gate's OWN bootstrap semantics (unmodified) are what
        # makes this hold, not a new conditional bolted onto signal_engine.
        # insufficient history is reported on the dark-pool gate itself;
        # data_quality now tracks collection success (signal-integrity remediation)
        assert gates["dark_pool_anomaly"]["status"] == "insufficient"

    def test_blocked_gate_cannot_reach_confirmed_even_combined_with_a_second_category(self):
        """Belt-and-suspenders: even if a genuinely independent SECOND
        category (options_flow_anomaly) also passes this scan, a
        structurally-blocked dark-pool gate must not silently count
        toward the 2-category tally."""
        today = dt.date(2026, 3, 12)
        result = self._extreme_but_structurally_blocked_result(today, UNKNOWN)
        gates = se.evaluate_gates(
            TICKER, options_flow_hit=True, options_flow_metrics={"note": "real 2nd category"},
            **_tier1_kwargs(result),
        )
        assert gates["dark_pool_anomaly"]["passed"] is False
        assert gates["options_flow_anomaly"]["passed"] is True
        # Only ONE independent category passed (options flow) -- still
        # not enough for multi_signal_confirmation.
        assert gates["multi_signal_confirmation"]["passed"] is False
        confidence, _, _, _ = se.classify_confidence(gates)
        assert confidence != "high"
