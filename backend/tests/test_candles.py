"""candles.py: production OHLC gotchas (sort, negative-volume clamp, 1d
dedup, limit clamp) + evidence-marker construction, ported by value from
webapp/backend.py::_build_candles (read-only production reference)."""
from app.core.candles import build_candles, build_markers, clamp_limit


def test_bars_are_sorted_chronological_even_when_input_is_newest_first():
    raw = [
        {"date": "2026-10-03", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
        {"date": "2026-10-01", "open": 8, "high": 9, "low": 7, "close": 8.5, "volume": 100},
        {"date": "2026-10-02", "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 100},
    ]
    candles, neg = build_candles(raw, dedupe_daily=True)
    assert [c["t"] for c in candles] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert neg == 0


def test_negative_volume_is_clamped_to_zero_but_raw_value_preserved():
    raw = [{"date": "2026-10-01", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": -42}]
    candles, neg = build_candles(raw, dedupe_daily=True)
    assert neg == 1
    assert candles[0]["v"] == 0
    assert candles[0]["v_raw"] == -42


def test_positive_volume_bar_has_no_v_raw_field():
    raw = [{"date": "2026-10-01", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 500}]
    candles, neg = build_candles(raw, dedupe_daily=True)
    assert neg == 0
    assert "v_raw" not in candles[0]


def test_1d_dedup_keeps_one_bar_per_date():
    raw = [
        {"date": "2026-10-01", "open": 1, "high": 2, "low": 0.5, "close": 1.1, "volume": 10},
        {"date": "2026-10-01", "open": 1, "high": 2, "low": 0.5, "close": 1.9, "volume": 10},
    ]
    candles, _ = build_candles(raw, dedupe_daily=True)
    assert len(candles) == 1


def test_intraday_bars_use_start_time_not_date():
    raw = [{"start_time": "2026-10-01T09:30:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.1, "volume": 10}]
    candles, _ = build_candles(raw, dedupe_daily=False)
    assert candles[0]["t"] == "2026-10-01T09:30:00Z"


def test_bar_missing_timestamp_is_skipped_not_fabricated():
    raw = [{"open": 1, "high": 2, "low": 0.5, "close": 1.1, "volume": 10}]
    candles, _ = build_candles(raw, dedupe_daily=False)
    assert candles == []


def test_clamp_limit_never_exceeds_uw_422_threshold():
    assert clamp_limit(5000) == 2499
    assert clamp_limit(0) == 180
    assert clamp_limit(-5) == 180
    assert clamp_limit(50) == 50
    assert clamp_limit("not-a-number") == 180


def test_markers_darkpool_trade_has_real_price_and_timestamp():
    markers = build_markers([], [{"executed_at": "2026-10-12T11:02:17Z", "price": 70.12}])
    assert len(markers) == 1
    assert markers[0]["kind"] == "darkpool_trade"
    assert markers[0]["price"] == 70.12
    assert markers[0]["time"] == "2026-10-12T11:02:17Z"


def test_markers_flow_alert_with_underlying_price_gets_a_price_marker():
    markers = build_markers([{"created_at": "2026-10-12T14:31:05Z", "underlying_price": 84.12, "is_call": True}], [])
    assert markers[0]["price"] == 84.12
    assert "call" in markers[0]["label"]


def test_markers_flow_alert_without_price_is_time_only_never_invented():
    markers = build_markers([{"created_at": "2026-10-12T14:31:05Z"}], [])
    assert markers[0]["price"] is None
    assert markers[0]["time"] == "2026-10-12T14:31:05Z"


def test_markers_are_sorted_by_time_across_both_sources():
    alerts = [{"created_at": "2026-10-12T15:00:00Z"}]
    trades = [{"executed_at": "2026-10-12T09:00:00Z", "price": 1.0}]
    markers = build_markers(alerts, trades)
    assert [m["time"] for m in markers] == ["2026-10-12T09:00:00Z", "2026-10-12T15:00:00Z"]


def test_markers_skip_events_with_no_timestamp_at_all():
    markers = build_markers([{"underlying_price": 1.0}], [{"price": 1.0}])
    assert markers == []
