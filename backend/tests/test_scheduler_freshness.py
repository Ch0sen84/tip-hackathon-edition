"""Phase D, item 3: current/stale/unevaluated classification at the
exact boundary, plus the dedup rule (most recent evaluation wins
regardless of watchlist-vs-discovery source)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core import scheduler_freshness as sf
from app.ingestion import IngestionCatalog


def _catalog(tmp_path):
    return IngestionCatalog(str(tmp_path / "db.sqlite"))


def test_unevaluated_when_no_evaluation_row_exists():
    result = sf.classify_freshness(None, "NVDA", now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert result.state == sf.UNEVALUATED
    assert result.evaluation is None
    assert result.age_s is None


def test_current_strictly_inside_the_window():
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    retrieved_at = (now - timedelta(seconds=100)).isoformat()
    result = sf.classify_freshness({"retrieved_at": retrieved_at}, "NVDA", now=now, window_s=1200)
    assert result.state == sf.CURRENT


def test_current_exactly_at_the_boundary_inclusive():
    """age_s == window_s is CURRENT (<=), the inclusive edge."""
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    retrieved_at = (now - timedelta(seconds=1200)).isoformat()
    result = sf.classify_freshness({"retrieved_at": retrieved_at}, "NVDA", now=now, window_s=1200)
    assert result.state == sf.CURRENT
    assert result.age_s == 1200.0


def test_stale_one_second_past_the_boundary():
    """age_s == window_s + 1 is STALE -- the exact other side of the
    same inequality the previous test checks."""
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    retrieved_at = (now - timedelta(seconds=1201)).isoformat()
    result = sf.classify_freshness({"retrieved_at": retrieved_at}, "NVDA", now=now, window_s=1200)
    assert result.state == sf.STALE
    assert result.age_s == 1201.0


def test_stale_well_past_the_window():
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    retrieved_at = (now - timedelta(hours=5)).isoformat()
    result = sf.classify_freshness({"retrieved_at": retrieved_at}, "NVDA", now=now, window_s=1200)
    assert result.state == sf.STALE


def test_default_window_is_twenty_minutes():
    assert sf.DEFAULT_FRESHNESS_WINDOW_S == 20 * 60


def test_window_configurable_via_env(monkeypatch):
    monkeypatch.setenv("TIP_EVAL_FRESHNESS_WINDOW_S", "600")
    assert sf.freshness_window_s() == 600


def test_window_env_var_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TIP_EVAL_FRESHNESS_WINDOW_S", "not-a-number")
    assert sf.freshness_window_s() == sf.DEFAULT_FRESHNESS_WINDOW_S


def test_malformed_retrieved_at_treated_as_unevaluated():
    result = sf.classify_freshness({"retrieved_at": "not-a-timestamp"}, "NVDA")
    assert result.state == sf.UNEVALUATED


# --- dedup: watchlist-sourced vs discovery-sourced, same ticker, same day --

def test_dedup_prefers_most_recent_regardless_of_source(tmp_path):
    """Simulates the exact scenario Chad flagged: NVDA gets evaluated
    once via the watchlist-scan path and once via the discovery/
    scheduler path, same day. Both write into the SAME signal_evaluations
    table via catalog_signal_evaluation (no source column, no second
    store) -- classify_candidate must surface exactly ONE evaluation:
    whichever is more recent, never a conflicting pair."""
    catalog = _catalog(tmp_path)
    # "Watchlist" scan at 09:35, confidence=LOW
    catalog.catalog_signal_evaluation(
        "NVDA", {"state": "WATCHING", "confidence": "LOW"},
        retrieved_at="2026-10-15T09:35:00+00:00",
    )
    # "Discovery/scheduler" evaluation at 10:05 (later), confidence=HIGH
    catalog.catalog_signal_evaluation(
        "NVDA", {"state": "CONFIRMED", "confidence": "HIGH"},
        retrieved_at="2026-10-15T10:05:00+00:00",
    )
    latest = catalog.get_latest_signal_evaluation("NVDA")
    assert latest is not None
    assert latest["retrieved_at"] == "2026-10-15T10:05:00+00:00"
    assert latest["state"] == "CONFIRMED"
    assert latest["confidence"] == "HIGH"

    result = sf.classify_candidate(
        catalog, "NVDA", now=datetime(2026, 10, 15, 10, 10, tzinfo=timezone.utc),
    )
    assert result.evaluation["retrieved_at"] == "2026-10-15T10:05:00+00:00"
    assert result.state == sf.CURRENT


def test_dedup_most_recent_wins_even_when_discovery_is_older_than_watchlist(tmp_path):
    """Order shouldn't matter -- only recency. Here the DISCOVERY
    evaluation happens first, then watchlist re-scans later; watchlist's
    row must win."""
    catalog = _catalog(tmp_path)
    catalog.catalog_signal_evaluation(
        "TSLA", {"state": "WATCHING", "confidence": "LOW"},
        retrieved_at="2026-10-15T09:00:00+00:00",
    )
    catalog.catalog_signal_evaluation(
        "TSLA", {"state": "BUILDING", "confidence": "MEDIUM"},
        retrieved_at="2026-10-15T11:30:00+00:00",
    )
    latest = catalog.get_latest_signal_evaluation("TSLA")
    assert latest["retrieved_at"] == "2026-10-15T11:30:00+00:00"
    assert latest["state"] == "BUILDING"


def test_classify_candidate_unevaluated_for_a_ticker_never_scanned(tmp_path):
    catalog = _catalog(tmp_path)
    result = sf.classify_candidate(catalog, "OBSCURE_TICKER")
    assert result.state == sf.UNEVALUATED
