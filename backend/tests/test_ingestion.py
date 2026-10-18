"""Phase B: shared ingestion-layer tests. Every test uses an isolated
tmp_path-backed IngestionCatalog (or ':memory:') -- never the real
deployed DB (see app/storage.py's assert_safe_test_db_path pattern this
mirrors)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from datetime import date, timedelta

from app.core.trading_calendar import session_end_utc
from app.ingestion import IngestionCatalog, StorageGuardPausedError, fingerprint, should_refetch
from app.storage_guard import BACKGROUND, ONDEMAND


def _catalog(tmp_path):
    return IngestionCatalog(str(tmp_path / "catalog.db"))


# chart-data-integrity-fixes (2026-10-18, Issue 2) fixtures: a fixed real
# trading date (Wednesday, a genuine NYSE session day) with its real
# session-close instant, so "mid-session" vs. "after close" retrieved_at
# values are deterministic rather than depending on when these tests
# happen to run.
_TRADING_DATE = date(2026, 10, 14)
_CLOSE_UTC = session_end_utc(_TRADING_DATE)
assert _CLOSE_UTC is not None, "2026-10-14 must be a real NYSE trading day for this fixture"
_MID_SESSION = (_CLOSE_UTC - timedelta(hours=2)).isoformat()
# R8-2 (chart-data-integrity-fixes round 9, 2026-10-18): _bar_is_final now
# requires DAILY_BAR_FINALITY_GRACE_SECONDS (20 minutes) past the real
# session close, not just >= close. _WITHIN_GRACE (close+5min) is
# deliberately still NOT final -- it exercises the grace window itself.
# _AFTER_CLOSE is redefined to be genuinely past the grace period
# (close+25min) so every pre-existing test that relied on it meaning
# "the bar is now final" keeps meaning that; nothing here changes what
# those tests assert, only how far past close "final" now requires.
_WITHIN_GRACE = (_CLOSE_UTC + timedelta(minutes=5)).isoformat()
_AFTER_CLOSE = (_CLOSE_UTC + timedelta(minutes=25)).isoformat()
_AFTER_CLOSE_LATER = (_CLOSE_UTC + timedelta(hours=1)).isoformat()
_AFTER_CLOSE_MUCH_LATER = (_CLOSE_UTC + timedelta(hours=3)).isoformat()




def test_signal_evaluation_history_exposes_each_stored_transition_rule_read_only(tmp_path):
    cat = _catalog(tmp_path)
    cat.catalog_signal_evaluation("NVDA", {"state": "NEW", "confidence": "low", "transition_rule": "no_qualifying_evidence_yet"}, retrieved_at="2026-10-14T12:00:00Z")
    cat.catalog_signal_evaluation("NVDA", {"state": "BUILDING", "confidence": "medium", "transition_rule": "new_independent_signal"}, retrieved_at="2026-10-15T12:00:00Z")
    assert [e["transition_rule"] for e in cat.list_signal_evaluation_rules("NVDA")] == [
        "no_qualifying_evidence_yet", "new_independent_signal"
    ]


def test_catalog_options_flow_dedupes_by_explicit_id(tmp_path):
    cat = _catalog(tmp_path)
    alert = {"id": "abc123", "created_at": "2026-10-15T14:00:00Z", "total_premium": 1_000_000}
    first = cat.catalog_options_flow_observations("NVDA", [alert])
    second = cat.catalog_options_flow_observations("NVDA", [alert])  # exact re-ingest
    assert first == 1
    assert second == 0  # suppressed by UNIQUE(source_id), not double-inserted


def test_catalog_darkpool_missing_identifier_uses_fingerprint_not_crash(tmp_path):
    """Real UW dark-pool rows (confirmed during this phase's live
    validation) do not reliably carry a usable unique id field -- must
    never crash, and the SAME logical trade re-ingested twice must still
    dedupe via the fingerprint."""
    cat = _catalog(tmp_path)
    trade = {"executed_at": "2026-10-15T14:05:00Z", "price": 450.1, "size": 1000, "premium": 450100.0}
    first = cat.catalog_darkpool_observations("SPY", [trade])
    second = cat.catalog_darkpool_observations("SPY", [trade])
    assert first == 1
    assert second == 0


def test_fingerprint_is_order_and_value_sensitive():
    a = fingerprint("SPY", "2026-10-15", 1.0)
    b = fingerprint("SPY", "2026-10-15", 2.0)
    c = fingerprint("2026-10-15", "SPY", 1.0)
    assert a != b
    assert a != c


def test_catalog_price_bars_dedupe_on_repeated_fetch(tmp_path):
    cat = _catalog(tmp_path)
    candles = [{"t": "2026-10-14", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]
    first = cat.catalog_price_bars("NVDA", "1d", candles)
    second = cat.catalog_price_bars("NVDA", "1d", candles)
    assert first == 1
    assert second == 0


# --- chart-data-integrity-fixes (2026-10-18, Issue 2 -- stale SPY daily
# candle) regression tests. All use the fixed _TRADING_DATE/_MID_SESSION/
# _AFTER_CLOSE fixtures above so finality is deterministic, never
# dependent on when the test suite happens to run. ------------------------

def test_catalog_price_bars_daily_incomplete_then_complete_replaces(tmp_path):
    """The exact real-world shape of Issue 2: a daily bar cataloged
    MID-SESSION (not yet final) must be REPLACED when a later, genuinely
    different fetch for the SAME date arrives -- this is what would have
    turned SPY's stale $771.36 into the real $763.25 close, had the old
    INSERT-OR-IGNORE-forever bug not blocked it."""
    cat = _catalog(tmp_path)
    partial = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 771.36, "v": 1000}]
    final = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 763.25, "v": 42_000_000}]

    first = cat.catalog_price_bars("SPY", "1d", partial, retrieved_at=_MID_SESSION)
    bars_after_partial = cat.get_cached_price_bars("SPY", "1d")
    second = cat.catalog_price_bars("SPY", "1d", final, retrieved_at=_AFTER_CLOSE)
    bars_after_final = cat.get_cached_price_bars("SPY", "1d")

    assert first == 1
    assert bars_after_partial == [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 771.36, "v": 1000}]
    assert second == 1, "the more-complete post-close bar must be recognized as a real change"
    assert bars_after_final == [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 763.25, "v": 42_000_000}]
    assert len(bars_after_final) == 1, "exactly one bar for this date -- the old partial row must not also appear"


def test_catalog_price_bars_daily_final_bar_rejects_staler_or_same_instant_regression(tmp_path):
    """R8-2 (round 9, 2026-10-18): a confirmed-final bar is no longer an
    UNCONDITIONAL lock (see test_catalog_price_bars_daily_final_bar_accepts_genuine_late_correction
    below for the legitimate-correction path this test does NOT exercise)
    -- but it must still reject any attempt to replace it with data that
    is NOT both strictly newer AND itself final. A same-instant refetch
    (no genuine new information) must never regress an already-final bar,
    however different its content."""
    cat = _catalog(tmp_path)
    final = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 763.25, "v": 42_000_000}]
    different_same_instant = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 999.99, "v": 1}]

    first = cat.catalog_price_bars("SPY", "1d", final, retrieved_at=_AFTER_CLOSE)
    second = cat.catalog_price_bars("SPY", "1d", different_same_instant, retrieved_at=_AFTER_CLOSE)
    bars = cat.get_cached_price_bars("SPY", "1d")

    assert first == 1
    assert second == 0, "a same-instant/no-new-information refetch must never regress a confirmed-final bar"
    assert bars[0]["c"] == 763.25


def test_catalog_price_bars_daily_final_bar_accepts_genuine_late_correction(tmp_path):
    """R8-2 REQUIREMENT (round 9, 2026-10-18): 'a correction arriving
    after the grace period should still be accepted if it's more
    complete than what's stored -- don't make the grace-period itself a
    new way to permanently lock in a bad bar.' A real exchange
    correction, fetched strictly LATER than the already-final bar and
    itself computing as final (a later fetch for an already-past session
    always does), must be accepted -- never silently discarded the way
    Issue 2's original bug discarded SPY's real final close."""
    cat = _catalog(tmp_path)
    final = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 763.25, "v": 42_000_000}]
    real_correction = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 763.40, "v": 42_050_000}]

    first = cat.catalog_price_bars("SPY", "1d", final, retrieved_at=_AFTER_CLOSE)
    second = cat.catalog_price_bars("SPY", "1d", real_correction, retrieved_at=_AFTER_CLOSE_MUCH_LATER)
    bars = cat.get_cached_price_bars("SPY", "1d")

    assert first == 1
    assert second == 1, "a genuine, strictly-later, itself-final correction must be accepted, not permanently blocked"
    assert bars[0]["c"] == 763.40, "the corrected (more complete/trustworthy) value must now be the authoritative one"
    assert len(bars) == 1, "append-only history must still resolve to exactly one CURRENT bar for this date"

    conn = sqlite3.connect(cat.db_path)
    raw_row_count = conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE ticker = 'SPY' AND timeframe = '1d'"
    ).fetchone()[0]
    conn.close()
    assert raw_row_count == 2, "both the original final value AND the correction remain in history -- append-only, never UPDATE/delete"


def test_catalog_price_bars_daily_final_bar_rejects_staler_correction_attempt(tmp_path):
    """The non-regression guarantee must hold even for the NEW late-
    correction path: an incoming fetch with an OLDER retrieved_at than
    the already-final pointer must still be rejected outright, final or
    not -- a correction can only ever move forward in time."""
    cat = _catalog(tmp_path)
    final = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 763.25, "v": 42_000_000}]
    staler_different = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 999.99, "v": 1}]

    first = cat.catalog_price_bars("SPY", "1d", final, retrieved_at=_AFTER_CLOSE_LATER)
    second = cat.catalog_price_bars("SPY", "1d", staler_different, retrieved_at=_AFTER_CLOSE)
    bars = cat.get_cached_price_bars("SPY", "1d")

    assert first == 1
    assert second == 0, "a staler retrieved_at must never override an already-final bar, however different"
    assert bars[0]["c"] == 763.25


def test_catalog_price_bars_daily_repeated_refresh_same_date_idempotent(tmp_path):
    """Repeated refreshes of the SAME date with the SAME real value must
    never create duplicate rows, before OR after finality."""
    cat = _catalog(tmp_path)
    bar = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 765.0, "v": 500}]

    first = cat.catalog_price_bars("SPY", "1d", bar, retrieved_at=_MID_SESSION)
    second = cat.catalog_price_bars("SPY", "1d", bar, retrieved_at=_MID_SESSION)
    third = cat.catalog_price_bars("SPY", "1d", bar, retrieved_at=_AFTER_CLOSE)  # unchanged content, now final (past the R8-2 grace period)
    fourth = cat.catalog_price_bars("SPY", "1d", bar, retrieved_at=_AFTER_CLOSE_LATER)  # already final, unchanged content -> no-op

    assert first == 1
    assert second == 0
    # `third` DOES count as a real change: the bar is now confirmed final
    # for the first time (same price, but finality genuinely improved).
    assert third == 1
    assert fourth == 0
    assert len(cat.get_cached_price_bars("SPY", "1d")) == 1

    conn = sqlite3.connect(cat.db_path)
    raw_row_count = conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE ticker = 'SPY' AND timeframe = '1d'"
    ).fetchone()[0]
    conn.close()
    assert raw_row_count == 2, "append-only: one row for the mid-session value, one for the final confirmation -- never more"


# --- R8-2 (chart-data-integrity-fixes round 9, 2026-10-18): finality
# grace-period policy tests -- weekend/holiday, early-close (half-day),
# and the grace-window boundary itself. --------------------------------

def test_bar_is_final_within_grace_window_not_yet_final(tmp_path):
    """A bar fetched close+5min (inside the 20-minute grace period) must
    NOT be treated as final yet -- the whole point of the grace period."""
    from app.ingestion import _bar_is_final
    assert _bar_is_final(_TRADING_DATE.isoformat(), _WITHIN_GRACE) is False


def test_bar_is_final_past_grace_window_is_final(tmp_path):
    """A bar fetched close+25min (past the 20-minute grace period) IS
    final."""
    from app.ingestion import _bar_is_final
    assert _bar_is_final(_TRADING_DATE.isoformat(), _AFTER_CLOSE) is True


def test_bar_is_final_weekend_never_final_no_real_session_to_have_closed(tmp_path):
    """A 'trading date' that is actually a real weekend day has no real
    session to have closed -- must never be marked final regardless of
    how far in the future `retrieved_at` is. (2026-10-17 is a Saturday.)"""
    from app.ingestion import _bar_is_final
    saturday = date(2026, 10, 17).isoformat()
    far_future = "2026-10-20T23:00:00+00:00"
    assert _bar_is_final(saturday, far_future) is False


def test_bar_is_final_holiday_never_final_no_real_session_to_have_closed(tmp_path):
    """Same as the weekend case but for a real NYSE full-market-closure
    holiday (2026 Thanksgiving, 2026-11-26) -- no session, so never
    final, regardless of fetch time."""
    from app.ingestion import _bar_is_final
    holiday = date(2026, 11, 26).isoformat()
    far_future = "2026-11-27T23:00:00+00:00"
    assert _bar_is_final(holiday, far_future) is False


def test_bar_is_final_early_close_session_becomes_final_at_1pm_not_4pm(tmp_path):
    """REQUIREMENT: an early-close (half-day) session must become final
    relative to its REAL 1:00pm ET close (+ grace period), never 4:00pm.
    2026-11-27 (day after Thanksgiving) is a real NYSE 1:00pm ET
    early-close day -- confirmed via trading_calendar's own early-close
    table, not hardcoded here."""
    from app.ingestion import _bar_is_final
    from app.core.trading_calendar import is_early_close
    early_close_date = date(2026, 11, 27)
    assert is_early_close(early_close_date), "2026-11-27 must be a real NYSE early-close day for this fixture"
    early_close_utc = session_end_utc(early_close_date)
    # 30 minutes after the REAL 1:00pm ET close (past the grace period) -> final.
    past_1pm_grace = (early_close_utc + timedelta(minutes=30)).isoformat()
    assert _bar_is_final(early_close_date.isoformat(), past_1pm_grace) is True
    # 2 hours after 1:00pm ET (i.e. 3:00pm ET) is still BEFORE the normal
    # 4:00pm close a non-early-close day would use -- if this module
    # wrongly used 4pm for every day, this would incorrectly read as
    # not-final. It must already read as final, because the REAL close
    # for this date is 1pm, not 4pm.
    three_pm_et_equivalent = (early_close_utc + timedelta(hours=2)).isoformat()
    assert _bar_is_final(early_close_date.isoformat(), three_pm_et_equivalent) is True


def test_catalog_price_bars_daily_stale_older_refresh_rejected(tmp_path):
    """An attempted refresh with an OLDER retrieved_at than what's
    already cached must be rejected -- never regress to staler data,
    even when not yet final."""
    cat = _catalog(tmp_path)
    newer = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 770.0, "v": 900}]
    older_different = [{"t": _TRADING_DATE.isoformat(), "o": 760.0, "h": 772.0, "l": 759.0, "c": 123.45, "v": 1}]

    first = cat.catalog_price_bars("SPY", "1d", newer, retrieved_at=_AFTER_CLOSE_LATER)
    second = cat.catalog_price_bars("SPY", "1d", older_different, retrieved_at=_MID_SESSION)
    bars = cat.get_cached_price_bars("SPY", "1d")

    assert first == 1
    assert second == 0, "an older retrieved_at must never override the already-cached value"
    assert bars[0]["c"] == 770.0


def test_catalog_price_bars_intraday_timeframe_unaffected_by_daily_fix(tmp_path):
    """Issue 2's fix is explicitly scoped to daily (1d) bars -- intraday
    bars must keep their pre-existing plain dedupe-by-(ticker,timeframe,t)
    behavior unchanged (same assertion shape as the pre-existing daily
    dedupe test, just timeframe='5m')."""
    cat = _catalog(tmp_path)
    candles = [{"t": "2026-10-14T14:30:00+00:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]
    first = cat.catalog_price_bars("NVDA", "5m", candles)
    second = cat.catalog_price_bars("NVDA", "5m", candles)
    assert first == 1
    assert second == 0
    conn = sqlite3.connect(cat.db_path)
    pointer_rows = conn.execute("SELECT COUNT(*) FROM price_bar_current WHERE ticker = 'NVDA'").fetchone()[0]
    conn.close()
    assert pointer_rows == 0, "the pointer table is a daily-bar-only mechanism -- intraday writes must never touch it"


def test_catalog_signal_evaluation_links_evidence_rows(tmp_path):
    cat = _catalog(tmp_path)
    scan = {"state": "CONFIRMED", "previous_state": "BUILDING", "confidence": "HIGH",
            "transition_rule": "r1"}
    evaluation_id = cat.catalog_signal_evaluation("NVDA", scan, evidence_refs=[
        ("options_flow_observations", 1), ("darkpool_observations", 1),
    ])
    assert evaluation_id is not None
    conn = sqlite3.connect(cat.db_path)
    links = conn.execute(
        "SELECT observation_table, observation_id FROM signal_evaluation_evidence WHERE evaluation_id = ?",
        (evaluation_id,),
    ).fetchall()
    assert set(links) == {("options_flow_observations", 1), ("darkpool_observations", 1)}
    transitions = conn.execute(
        "SELECT from_state, to_state FROM signal_transitions WHERE ticker = 'NVDA'"
    ).fetchall()
    assert transitions == [("BUILDING", "CONFIRMED")]
    conn.close()


def test_catalog_signal_evaluation_no_transition_row_when_state_unchanged(tmp_path):
    cat = _catalog(tmp_path)
    scan = {"state": "STABLE", "previous_state": "STABLE", "confidence": "HIGH"}
    cat.catalog_signal_evaluation("NVDA", scan)
    conn = sqlite3.connect(cat.db_path)
    transitions = conn.execute("SELECT * FROM signal_transitions").fetchall()
    assert transitions == []
    conn.close()


def test_one_evaluation_can_cite_multiple_observations_and_vice_versa(tmp_path):
    """Chad's explicit requirement: the join table must support a
    many-to-many relationship, not just one evidence row per evaluation."""
    cat = _catalog(tmp_path)
    scan1 = {"state": "NEW", "previous_state": None, "confidence": "LOW"}
    scan2 = {"state": "BUILDING", "previous_state": "NEW", "confidence": "MEDIUM"}
    eval1 = cat.catalog_signal_evaluation("NVDA", scan1, evidence_refs=[("darkpool_observations", 5)])
    eval2 = cat.catalog_signal_evaluation("NVDA", scan2, evidence_refs=[("darkpool_observations", 5)])
    assert eval1 != eval2
    conn = sqlite3.connect(cat.db_path)
    rows = conn.execute(
        "SELECT evaluation_id FROM signal_evaluation_evidence WHERE observation_table = 'darkpool_observations' "
        "AND observation_id = 5 ORDER BY evaluation_id"
    ).fetchall()
    assert {r[0] for r in rows} == {eval1, eval2}
    conn.close()


def test_reconcile_correction_never_updates_original_row_appends_new(tmp_path):
    """A revised source record must create a NEW row, never an UPDATE
    that destroys the original."""
    cat = _catalog(tmp_path)
    original = {"id": "trade-1", "executed_at": "2026-10-15T14:00:00Z", "price": 100.0, "size": 10}
    cat.catalog_darkpool_observations("SPY", [original])
    conn = sqlite3.connect(cat.db_path)
    old_id = conn.execute(
        "SELECT id FROM darkpool_observations WHERE source_id = 'trade-1'"
    ).fetchone()[0]
    conn.close()

    corrected = {"id": "trade-1-corrected", "executed_at": "2026-10-15T14:00:00Z", "price": 101.5, "size": 10}
    cat.catalog_darkpool_observations("SPY", [corrected])
    conn = sqlite3.connect(cat.db_path)
    new_id = conn.execute(
        "SELECT id FROM darkpool_observations WHERE source_id = 'trade-1-corrected'"
    ).fetchone()[0]
    cat.reconcile_correction("darkpool_observations", new_id, old_id, reason="UW republished a corrected price")

    original_row = conn.execute("SELECT raw_json FROM darkpool_observations WHERE id = ?", (old_id,)).fetchone()
    assert json.loads(original_row[0])["price"] == 100.0  # untouched -- the correction never overwrote it

    correction = conn.execute(
        "SELECT observation_id, supersedes_observation_id FROM source_corrections WHERE observation_table = ?",
        ("darkpool_observations",),
    ).fetchone()
    assert correction == (new_id, old_id)
    conn.close()


def test_storage_guard_rejects_ondemand_write_when_critical(tmp_path, monkeypatch):
    from collections import namedtuple
    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    cat = _catalog(tmp_path)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: DiskUsage(total=10_000 * 1024 * 1024, used=9_999 * 1024 * 1024, free=1 * 1024 * 1024),
    )
    with pytest.raises(StorageGuardPausedError):
        cat.catalog_darkpool_observations("SPY", [{"id": "t1", "executed_at": "x"}], kind=ONDEMAND)


def test_storage_guard_background_pauses_earlier_than_ondemand(tmp_path, monkeypatch):
    from collections import namedtuple
    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    cat = _catalog(tmp_path)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: DiskUsage(total=10_000 * 1024 * 1024, used=9_000 * 1024 * 1024, free=1_000 * 1024 * 1024),
    )
    # 1000MB free: allowed on-demand, paused in background -- same
    # contract as app/storage_guard.py's own tests, exercised here
    # through the NEW ingestion write path.
    inserted = cat.catalog_darkpool_observations("SPY", [{"id": "t1", "executed_at": "x"}], kind=ONDEMAND)
    assert inserted == 1
    with pytest.raises(StorageGuardPausedError):
        cat.catalog_darkpool_observations("SPY", [{"id": "t2", "executed_at": "y"}], kind=BACKGROUND)


def test_ingestion_job_checkpoint_resume_avoids_double_insert(tmp_path):
    """Simulates an interrupted job: start a job, checkpoint progress,
    then look it up again (as if the process restarted) and confirm the
    checkpoint cursor is recoverable and resuming does not need to
    re-fetch/double-insert anything already cataloged."""
    cat = _catalog(tmp_path)
    job_id = cat.start_ingestion_job("darkpool_day_pagination",
                                      {"ticker": "SPY", "trading_date": "2026-10-14", "older_than": None, "pages": 0})
    trade = {"id": "t1", "executed_at": "2026-10-14T15:00:00Z"}
    cat.catalog_darkpool_observations("SPY", [trade])
    cat.update_ingestion_job(job_id, "running",
                              {"ticker": "SPY", "trading_date": "2026-10-14",
                               "older_than": "2026-10-14T15:00:00Z", "pages": 1})

    # "process restart" -- look the job up fresh by (ticker, date).
    resumed = cat.find_resumable_job("darkpool_day_pagination", {"ticker": "SPY", "trading_date": "2026-10-14"})
    assert resumed is not None
    assert resumed["detail"]["older_than"] == "2026-10-14T15:00:00Z"
    assert resumed["detail"]["pages"] == 1

    # Re-ingesting the same already-collected trade (as a naive resume
    # might do) is still safely deduped, never double-inserted.
    again = cat.catalog_darkpool_observations("SPY", [trade])
    assert again == 0


def test_should_refetch_true_when_nothing_cataloged_yet():
    assert should_refetch(None, freshness_window_s=300) is True


def test_should_refetch_false_within_freshness_window():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    assert should_refetch(now, freshness_window_s=3600) is False


def test_should_refetch_true_outside_freshness_window():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert should_refetch(old, freshness_window_s=300) is True


def test_discovery_snapshot_and_candidates_cataloged_together(tmp_path):
    cat = _catalog(tmp_path)
    filters = {"min_premium": 1_000_000}
    candidates = [{"ticker": "NVDA", "net_premium": 5_000_000}, {"ticker": "SPY", "net_premium": 2_000_000}]
    snap_id = cat.catalog_discovery_snapshot(filters, candidates, snapshot_at="2026-10-15T14:00:00Z")
    assert snap_id is not None
    conn = sqlite3.connect(cat.db_path)
    rows = conn.execute("SELECT ticker FROM discovery_candidates WHERE snapshot_id = ?", (snap_id,)).fetchall()
    assert {r[0] for r in rows} == {"NVDA", "SPY"}
    conn.close()
