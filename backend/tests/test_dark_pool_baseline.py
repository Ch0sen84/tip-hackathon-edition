"""Phase C: dark-pool baseline methodology tests. Every fixture here is
constructed Python data -- synthetic darkpool_observations/
darkpool_coverage_status rows built directly in the test, never a live UW
call and never a new fixture file."""
from __future__ import annotations

import datetime as dt

from app.core import dark_pool_baseline as dpb
from app.core import trading_calendar as tc
from app.dark_pool_pagination import COMPLETE, INCOMPLETE, UNKNOWN

TICKER = "FLUX"


def _coverage_row(coverage_date: str, status: str) -> dict:
    return {"coverage_date": coverage_date, "status": status, "pages_walked": 5,
            "session_start_verified": status == COMPLETE, "retrieved_at": "2026-01-01T00:00:00+00:00"}


def _obs(event_time_iso: str, premium: float) -> dict:
    return {"event_time": event_time_iso, "premium": premium}


def _nth_trading_day_before(anchor: dt.date, n: int) -> dt.date:
    """Walks backward from `anchor` to the n-th real NYSE trading day
    strictly before it (skips weekends/holidays) -- used to build
    historical fixture dates that are genuinely valid trading sessions."""
    d = anchor
    found = 0
    while found < n:
        d -= dt.timedelta(days=1)
        if tc.is_trading_day(d):
            found += 1
    return d


class TestEligibleSessionsStructurallyExcludeIncompleteAndUnknown:
    def test_only_complete_rows_survive(self):
        rows = [
            _coverage_row("2026-01-02", COMPLETE),
            _coverage_row("2026-01-05", INCOMPLETE),
            _coverage_row("2026-01-06", UNKNOWN),
            _coverage_row("2026-01-07", COMPLETE),
        ]
        eligible = dpb.eligible_sessions(rows)
        assert {r["coverage_date"] for r in eligible} == {"2026-01-02", "2026-01-07"}

    def test_empty_input_yields_empty_eligible_set(self):
        assert dpb.eligible_sessions([]) == []

    def test_all_incomplete_or_unknown_yields_empty_eligible_set(self):
        rows = [_coverage_row("2026-01-02", INCOMPLETE), _coverage_row("2026-01-05", UNKNOWN)]
        assert dpb.eligible_sessions(rows) == []


class TestTierClassificationBoundaries:
    def test_19_complete_sessions_is_preliminary_not_baseline_ready(self):
        assert dpb.classify_tier(19) == dpb.TIER_PRELIMINARY

    def test_20_complete_sessions_is_baseline_ready(self):
        assert dpb.classify_tier(20) == dpb.TIER_BASELINE_READY

    def test_59_complete_sessions_is_still_baseline_ready_not_strong(self):
        assert dpb.classify_tier(59) == dpb.TIER_BASELINE_READY

    def test_60_complete_sessions_is_strong(self):
        assert dpb.classify_tier(60) == dpb.TIER_STRONG

    def test_zero_sessions_is_preliminary(self):
        assert dpb.classify_tier(0) == dpb.TIER_PRELIMINARY

    def test_tier_alone_never_appears_in_anomaly_qualifies_without_the_stats_test(self):
        """Pins the single most important rule down directly: even at the
        STRONG tier (60+ sessions), if today's time-of-day cumulative is
        unremarkable, anomaly_qualifies must be False. Session count is
        necessary, never sufficient."""
        today = dt.date(2026, 3, 12)
        assert tc.is_trading_day(today)
        dates = [_nth_trading_day_before(today, n) for n in range(1, 61)]
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        obs_by_date = {}
        for d in dates:
            open_utc = tc.session_start_utc(d)
            obs_by_date[d.isoformat()] = [_obs(open_utc.isoformat(), 10_000.0)]
        today_open = tc.session_start_utc(today)
        today_obs = [_obs(today_open.isoformat(), 10_050.0)]  # basically identical to every historical session

        as_of = today_open + dt.timedelta(minutes=5)
        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=None,  # in-progress, no row yet -- valid since we're inside the session
            today_observations=today_obs, historical_coverage_rows=coverage_rows,
            historical_observations_by_date=obs_by_date,
        )
        assert result.tier == dpb.TIER_STRONG
        assert result.eligible_session_count == 60
        assert result.anomaly_qualifies is False


class TestIncompleteCoverageStructurallyCannotQualify:
    def test_today_flagged_incomplete_blocks_regardless_of_magnitude(self):
        today = dt.date(2026, 3, 12)
        dates = [_nth_trading_day_before(today, n) for n in range(1, 25)]
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        obs_by_date = {d.isoformat(): [_obs(tc.session_start_utc(d).isoformat(), 10_000.0)] for d in dates}
        today_open = tc.session_start_utc(today)
        # Deliberately extreme: 50x the historical mean, every other
        # requirement satisfied -- the ONLY problem is today's own
        # coverage status.
        today_obs = [_obs(today_open.isoformat(), 500_000.0)]
        as_of = today_open + dt.timedelta(minutes=5)

        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=INCOMPLETE,
            today_observations=today_obs, historical_coverage_rows=coverage_rows,
            historical_observations_by_date=obs_by_date,
        )
        assert result.tier == dpb.TIER_BASELINE_READY  # tier itself is fine
        assert result.ratio_vs_mean is not None and result.ratio_vs_mean >= 10  # the raw signal IS extreme
        assert result.today_session_valid is False
        assert result.anomaly_qualifies is False
        gate_inputs = result.as_gate_inputs()
        assert gate_inputs["tier1_hit"] is False
        assert gate_inputs["tier1_bootstrap"] is True

    def test_today_flagged_unknown_blocks_regardless_of_magnitude(self):
        today = dt.date(2026, 3, 12)
        dates = [_nth_trading_day_before(today, n) for n in range(1, 25)]
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        obs_by_date = {d.isoformat(): [_obs(tc.session_start_utc(d).isoformat(), 10_000.0)] for d in dates}
        today_open = tc.session_start_utc(today)
        today_obs = [_obs(today_open.isoformat(), 900_000.0)]
        as_of = today_open + dt.timedelta(minutes=5)

        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=UNKNOWN,
            today_observations=today_obs, historical_coverage_rows=coverage_rows,
            historical_observations_by_date=obs_by_date,
        )
        assert result.anomaly_qualifies is False
        assert result.as_gate_inputs()["tier1_bootstrap"] is True

    def test_preliminary_tier_blocks_regardless_of_magnitude(self):
        today = dt.date(2026, 3, 12)
        dates = [_nth_trading_day_before(today, n) for n in range(1, 10)]  # only 9 -- PRELIMINARY
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        obs_by_date = {d.isoformat(): [_obs(tc.session_start_utc(d).isoformat(), 5_000.0)] for d in dates}
        today_open = tc.session_start_utc(today)
        today_obs = [_obs(today_open.isoformat(), 1_000_000.0)]  # absurdly extreme
        as_of = today_open + dt.timedelta(minutes=5)

        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=None, today_observations=today_obs,
            historical_coverage_rows=coverage_rows, historical_observations_by_date=obs_by_date,
        )
        assert result.tier == dpb.TIER_PRELIMINARY
        assert result.today_session_valid is True  # today itself is fine
        assert result.anomaly_qualifies is False  # tier alone blocks it
        assert result.as_gate_inputs()["tier1_bootstrap"] is True

    def test_genuinely_in_progress_today_with_no_row_yet_is_valid(self):
        """The positive case: today has NO coverage_status row at all
        (pagination for today hasn't finished/started), but we are
        genuinely inside the live trading session right now -- this must
        NOT be treated the same as incomplete/unknown."""
        today = dt.date(2026, 3, 12)
        today_open = tc.session_start_utc(today)
        today_close = tc.session_end_utc(today)
        as_of = today_open + dt.timedelta(hours=1)
        assert today_open <= as_of <= today_close
        valid = dpb._today_session_valid(None, today_open, today_close, as_of)
        assert valid is True


class TestTimeOfDayVsWholeDayComparison:
    """THE hardest-to-get-right requirement: a correct implementation
    compares today's cumulative up to the current elapsed-time-since-open
    against the SAME elapsed-time slice of history -- never whole-day
    totals. Both naive mistakes are reproduced explicitly here (as
    intentionally-wrong helper functions, never used by production code)
    to prove the real implementation avoids both failure modes."""

    def _build_fixture(self, today: dt.date, n_sessions: int):
        dates = [_nth_trading_day_before(today, n) for n in range(1, n_sessions + 1)]
        coverage_rows = [_coverage_row(d.isoformat(), COMPLETE) for d in dates]
        return dates, coverage_rows

    def test_naive_whole_day_comparison_misses_a_genuine_front_loaded_anomaly(self):
        """Direction 1 ("vice versa" case): a REAL anomaly -- unusually
        large premium concentrated in the first hour -- that a naive
        'compare today's partial total straight to historical whole-day
        totals' approach would MISS (false negative), because the whole-
        day historical mean is inflated by a large, routine, LATE-session
        closing-auction surge that has nothing to do with the first hour.
        The correct time-of-day comparison catches it; the naive one does
        not."""
        today = dt.date(2026, 3, 12)
        dates, coverage_rows = self._build_fixture(today, 25)
        obs_by_date = {}
        whole_day_totals = []
        for d in dates:
            open_utc = tc.session_start_utc(d)
            early = _obs((open_utc + dt.timedelta(minutes=10)).isoformat(), 10_000.0)
            late = _obs((open_utc + dt.timedelta(hours=5)).isoformat(), 290_000.0)  # routine closing surge
            obs_by_date[d.isoformat()] = [early, late]
            whole_day_totals.append(10_000.0 + 290_000.0)

        today_open = tc.session_start_utc(today)
        elapsed = dt.timedelta(hours=1)
        as_of = today_open + elapsed
        # Today: genuinely anomalous early premium (6x the normal first-hour
        # value) but the session is still only 1 hour old -- no late-day
        # trades exist YET in the catalog (there's nothing to have yet).
        today_obs = [_obs((today_open + dt.timedelta(minutes=10)).isoformat(), 60_000.0)]

        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=None, today_observations=today_obs,
            historical_coverage_rows=coverage_rows, historical_observations_by_date=obs_by_date,
        )
        assert result.tier == dpb.TIER_BASELINE_READY
        assert result.today_session_valid is True
        # Correct time-of-day method: ratio vs mean of the SAME 1-hour
        # elapsed slice (10,000 historically) is 6x -- clears both bars.
        assert result.ratio_vs_mean is not None and result.ratio_vs_mean >= dpb.RATIO_THRESHOLD
        assert result.percentile == 1.0
        assert result.anomaly_qualifies is True

        # The naive ("wrong") whole-day comparison: today's partial total
        # (60,000) against the historical WHOLE-DAY mean (300,000).
        naive_whole_day_mean = sum(whole_day_totals) / len(whole_day_totals)
        naive_ratio = result.today_cumulative / naive_whole_day_mean
        assert naive_ratio < dpb.RATIO_THRESHOLD  # the naive method would have missed this real anomaly

    def test_naive_extrapolated_projection_falsely_flags_normal_early_variance(self):
        """Direction 2: completely NORMAL early-session variance that a
        naive 'project today's partial total out to a full day, then
        compare to the historical whole-day mean' approach would WRONGLY
        flag as an anomaly (false positive), purely because projecting a
        tiny, very-early sample amplifies ordinary noise. The correct
        time-of-day comparison (no projection, same-elapsed slice only)
        correctly does NOT flag it."""
        today = dt.date(2026, 3, 12)
        dates, coverage_rows = self._build_fixture(today, 25)
        obs_by_date = {}
        whole_day_totals = []
        for d in dates:
            open_utc = tc.session_start_utc(d)
            early = _obs((open_utc + dt.timedelta(minutes=5)).isoformat(), 10_000.0)
            rest_of_day = _obs((open_utc + dt.timedelta(hours=3)).isoformat(), 30_000.0)
            obs_by_date[d.isoformat()] = [early, rest_of_day]
            whole_day_totals.append(10_000.0 + 30_000.0)

        today_open = tc.session_start_utc(today)
        elapsed = dt.timedelta(minutes=10)
        as_of = today_open + elapsed
        # Today: totally ordinary early fluctuation (+15% over the
        # historical first-10-minute mean) -- not remotely anomalous.
        today_obs = [_obs((today_open + dt.timedelta(minutes=5)).isoformat(), 11_500.0)]

        result = dpb.compute_baseline(
            ticker=TICKER, today_date=today, as_of_utc=as_of,
            today_coverage_status=None, today_observations=today_obs,
            historical_coverage_rows=coverage_rows, historical_observations_by_date=obs_by_date,
        )
        # Correct time-of-day method: ratio ~1.15x vs the real same-elapsed
        # historical mean (10,000) -- nowhere near the 3x/95th-pct bar.
        assert result.ratio_vs_mean is not None and result.ratio_vs_mean < dpb.RATIO_THRESHOLD
        assert result.anomaly_qualifies is False

        # The naive projection: scale today's 10-minute partial up to a
        # full ~6.5-hour session, then compare to the historical whole-day
        # mean (40,000). Small-sample projection wildly amplifies routine
        # variance -- this is the realistic version of the "whole-day
        # comparison" bug the spec calls out.
        full_session_seconds = (tc.session_end_utc(today) - today_open).total_seconds()
        elapsed_seconds = elapsed.total_seconds()
        naive_projected_full_day = result.today_cumulative * (full_session_seconds / elapsed_seconds)
        naive_whole_day_mean = sum(whole_day_totals) / len(whole_day_totals)
        naive_ratio = naive_projected_full_day / naive_whole_day_mean
        assert naive_ratio >= dpb.RATIO_THRESHOLD  # the naive method would have falsely flagged this


class TestAsGateInputsShape:
    def test_qualifying_result_shapes_into_tier1_fields(self):
        result = dpb.BaselineResult(
            ticker=TICKER, as_of_utc=dt.datetime.now(dt.timezone.utc), tier=dpb.TIER_STRONG,
            eligible_session_count=60, today_coverage_status=None, today_session_valid=True,
            today_cumulative=60_000.0, historical_cumulatives=(10_000.0,) * 60,
            historical_mean=10_000.0, historical_stdev=0.0, ratio_vs_mean=6.0, z_score=None,
            percentile=1.0, anomaly_qualifies=True, reason="qualifies",
        )
        gi = result.as_gate_inputs()
        assert gi == {
            "tier1_hit": True, "tier1_bootstrap": False,
            "tier1_ratio": 6.0, "tier1_z": None, "total_premium": 60_000.0,
        }
