"""Regression tests for the 2026-10-15 OHLC session-handling fix.

Root cause (see app/core/candles.py module docstring for the full
writeup): UW returns up to 3 raw rows per trading date for daily (1d)
bars -- market_time "pr" (pre-market), "r" (regular session), "po"
(post-market) -- with genuinely different OHLC/volume, not duplicates.
The old dedupe-by-date-last-write-wins kept whichever happened to be
last in the response array (incidentally "r" every time observed live,
never guaranteed) and discarded 2 of every 3 requested raw rows -- the
actual cause of the "UW only returns 1/3 of requested candles" finding.
"""
from app.core.candles import (
    build_candles, build_markers, raw_limit_for_trading_days, select_regular_session_bars,
)


def _session_row(date, market_time, open_, close, volume):
    return {"date": date, "market_time": market_time, "open": open_, "high": open_ + 1,
            "low": open_ - 1, "close": close, "volume": volume, "total_volume": volume}


class TestSelectRegularSessionBars:
    def test_three_session_records_same_date_keeps_only_regular(self):
        raw = [
            _session_row("2026-10-14", "pr", 229.7, 225.65, 2_675_512),
            _session_row("2026-10-14", "po", 221.51, 217.89, 24_546_863),
            _session_row("2026-10-14", "r", 225.53, 221.3, 118_730_084),
        ]
        selected = select_regular_session_bars(raw)
        assert len(selected) == 1
        assert selected[0]["market_time"] == "r"
        assert selected[0]["open"] == 225.53

    def test_correct_selection_regardless_of_response_order(self):
        """The old bug depended on array order (last-write-wins). This
        must select 'r' even when 'r' appears FIRST or in the MIDDLE,
        not just when it happens to be last."""
        orders = [
            ["r", "pr", "po"],
            ["po", "r", "pr"],
            ["pr", "po", "r"],
        ]
        for order in orders:
            raw = [_session_row("2026-10-14", mt, 100 + i, 101 + i, 1000 * (i + 1))
                   for i, mt in enumerate(order)]
            selected = select_regular_session_bars(raw)
            assert len(selected) == 1, f"order {order}"
            assert selected[0]["market_time"] == "r", f"order {order}"

    def test_multiple_distinct_dates_each_keep_one_regular_row(self):
        raw = []
        for date in ("2026-10-12", "2026-10-13", "2026-10-14"):
            raw.extend([
                _session_row(date, "pr", 100, 101, 1000),
                _session_row(date, "r", 102, 103, 5000),
                _session_row(date, "po", 104, 105, 2000),
            ])
        selected = select_regular_session_bars(raw)
        assert len(selected) == 3
        assert {s["date"] for s in selected} == {"2026-10-12", "2026-10-13", "2026-10-14"}
        assert all(s["market_time"] == "r" for s in selected)

    def test_regular_session_volume_excludes_premarket_contribution(self):
        """UW's 'r' row volume is cumulative pre+regular (live-verified);
        this must subtract the matching 'pr' row's volume so the
        returned candle's volume matches its regular-session-only OHLC
        prices, not an inflated cumulative figure."""
        raw = [
            _session_row("2026-10-14", "pr", 229.7, 225.65, 2_675_512),
            _session_row("2026-10-14", "r", 225.53, 221.3, 118_730_084),  # cumulative pr+r
        ]
        selected = select_regular_session_bars(raw)
        assert len(selected) == 1
        assert selected[0]["volume"] == 118_730_084 - 2_675_512

    def test_missing_premarket_row_leaves_volume_unmodified(self):
        """Holiday / no premarket activity recorded for that date: no 'pr'
        row exists, so there is nothing to subtract -- the 'r' row's own
        volume is used as-is rather than guessing."""
        raw = [_session_row("2026-10-14", "r", 225.53, 221.3, 50_000_000)]
        selected = select_regular_session_bars(raw)
        assert selected[0]["volume"] == 50_000_000

    def test_rows_without_market_time_pass_through_unchanged(self):
        """Synthetic fixtures (no market_time field at all) must be
        completely unaffected -- nothing to select between."""
        raw = [
            {"date": "2026-10-12", "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 100},
            {"date": "2026-10-13", "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 200},
        ]
        selected = select_regular_session_bars(raw)
        assert selected == raw

    def test_subtraction_never_produces_negative_volume_fallback(self):
        """If pr volume somehow exceeds r volume (data anomaly), fall
        back to the raw r volume rather than emitting a negative number
        that would then get mis-flagged as the unrelated neg-volume
        artifact path."""
        raw = [
            _session_row("2026-10-14", "pr", 100, 101, 999_999_999),  # implausibly large
            _session_row("2026-10-14", "r", 102, 103, 1_000),
        ]
        selected = select_regular_session_bars(raw)
        assert selected[0]["volume"] == 1_000  # unchanged, not negative


class TestRawLimitForTradingDays:
    def test_requests_more_raw_rows_than_desired_trading_days(self):
        """The actual fix for the '1/3 of requested candles' bug: must
        ask UW for MORE than the desired unique-date count, since up to
        3 raw session rows come back per date."""
        assert raw_limit_for_trading_days(22) > 22
        assert raw_limit_for_trading_days(252) > 252

    def test_factor_is_enough_to_cover_three_sessions_per_day(self):
        # 4x covers 3 sessions/day plus headroom; must never request LESS
        # than 3x the desired count.
        for desired in (22, 65, 130, 252):
            assert raw_limit_for_trading_days(desired) >= desired * 3

    def test_result_is_capped_at_uws_hard_request_ceiling(self):
        assert raw_limit_for_trading_days(252) <= 2499
        assert raw_limit_for_trading_days(10_000) <= 2499


class TestBuildCandlesWithSessionSelection:
    def test_full_pipeline_252_raw_rows_yields_84_unique_dates_old_bug_vs_fix(self):
        """Reproduces the EXACT live-observed bug shape (252 raw rows,
        3/date => 84 unique dates) and proves the fix recovers all 84
        real trading days correctly with regular-session OHLC, matching
        what raw_limit_for_trading_days()'s over-fetch is designed to
        compensate for at the route layer (which requests ~1008 raw rows
        for an 84-unique-date 1Y ask, not 252)."""
        raw = []
        for i in range(84):
            date = f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}"
            raw.extend([
                _session_row(date, "pr", 100 + i, 101 + i, 1000),
                _session_row(date, "r", 102 + i, 103 + i, 5000),
                _session_row(date, "po", 104 + i, 105 + i, 2000),
            ])
        assert len(raw) == 252
        selected = select_regular_session_bars(raw)
        assert len(selected) == 84
        candles, _ = build_candles(selected, dedupe_daily=True)
        assert len(candles) == 84
        assert all(c["v"] == 5000 - 1000 for c in candles)  # regular-only volume, not cumulative

class TestBuildMarkersValueField:
    """2026-10-15 compact evidence interface: markers now carry a real
    `value` (premium) field so day/category aggregates are genuine, not
    estimated -- None when the source data has no premium, never 0 or
    a guessed figure."""

    def test_darkpool_trade_value_is_the_real_premium(self):
        markers = build_markers([], [{"executed_at": "2026-10-14T20:00:00Z", "price": 45.0, "premium": 8_000_000}])
        assert markers[0]["value"] == 8_000_000.0

    def test_flow_alert_value_is_the_real_total_premium(self):
        markers = build_markers([{"created_at": "2026-10-14T19:00:00Z", "total_premium": 1_800_000, "is_call": True}], [])
        assert markers[0]["value"] == 1_800_000.0

    def test_missing_premium_is_none_not_zero_or_estimated(self):
        markers = build_markers([{"created_at": "2026-10-14T19:00:00Z", "is_call": True}], [])
        assert markers[0]["value"] is None


class TestIntradayRowsUnaffected:
    def test_intraday_rows_are_never_session_filtered_or_deduped(self):
        """5m/other intraday rows each have a distinct start_time already
        -- selection logic must be a no-op for them (caller passes
        dedupe_daily=False for intraday, verified at the adapter layer
        in test_live_uw_adapter.py)."""
        raw = [
            {"start_time": "2026-10-14T13:30:00Z", "open": 1, "high": 2, "low": 0, "close": 1.5,
             "volume": 100, "market_time": "r"},
            {"start_time": "2026-10-14T13:35:00Z", "open": 1.5, "high": 2.5, "low": 1, "close": 2,
             "volume": 150, "market_time": "r"},
        ]
        candles, _ = build_candles(raw, dedupe_daily=False)
        assert len(candles) == 2  # both distinct buckets kept, no date-collision dedup applied
