"""R8-4 (chart-data-integrity-fixes round 9, 2026-10-18): migration 6
regression tests -- legacy-pointer backfill, repeated-refresh/migration
idempotency, the price_bar_current_latest view, the UNIQUE-per-date
invariant, and bounded revision growth for still-non-final dates.

All tests use an isolated tmp_path-backed SQLite file (or ':memory:'),
never the real deployed DB -- same convention as test_ingestion.py.
This migration is evaluated for use ONLY against this project's own
sandbox/local database, never a deployed instance.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.ingestion import (
    IngestionCatalog,
    PRICE_BAR_REVISION_KEEP,
    _prune_superseded_price_bar_revisions,
)
from app.migrations import MIGRATIONS, _ensure_schema_version_table, _utcnow_iso, run_migrations


def _apply_migrations_up_to(conn, max_version):
    _ensure_schema_version_table(conn)
    conn.commit()
    for m in sorted(MIGRATIONS, key=lambda m: m.version):
        if m.version > max_version:
            continue
        conn.executescript(m.sql)
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (m.version, m.name, _utcnow_iso()),
        )
        conn.commit()


def test_migration_6_backfills_pointer_for_legacy_pre_migration_5_row(tmp_path):
    """A price_bars row inserted directly (simulating real data cataloged
    BEFORE migration 5/6 ever ran, i.e. no price_bar_current row exists
    for it at all) must get a pointer backfilled the next time
    migrations run -- conservatively marked NOT final."""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    _apply_migrations_up_to(conn, 5)  # stop BEFORE migration 6 -- the real "legacy DB" state
    conn.execute(
        "INSERT INTO price_bars (source_id, ticker, event_time, retrieved_at, timeframe, "
        "open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-spy-1", "SPY", "2026-09-01", "2026-09-01T21:00:00+00:00", "1d",
         760.0, 762.0, 758.0, 761.0, 1_000_000),
    )
    conn.commit()
    # Confirm the pre-migration-6 state really has no pointer (sanity check on the fixture itself).
    assert conn.execute("SELECT COUNT(*) FROM price_bar_current").fetchone()[0] == 0
    conn.close()

    # Opening via IngestionCatalog runs run_migrations, applying version 6.
    cat = IngestionCatalog(db_path)
    conn2 = sqlite3.connect(db_path)
    row = conn2.execute(
        "SELECT is_final, price_bar_id FROM price_bar_current "
        "WHERE ticker = 'SPY' AND timeframe = '1d' AND event_time = '2026-09-01'"
    ).fetchone()
    conn2.close()
    assert row is not None, "migration 6 must backfill a pointer for the legacy row"
    assert row[0] == 0, "a backfilled legacy pointer must be conservatively marked NOT final, never guessed True"

    # And the R8-3 read path now correctly sees it (previously reported
    # None for a pure-legacy ticker with no pointer at all).
    assert cat.get_latest_daily_bar_final_status("SPY", "1d") is False


def test_migration_6_backfill_never_duplicates_an_existing_real_pointer(tmp_path):
    """A date that ALREADY has a real pointer (from a normal live fetch
    that happened before migration 6 ever ran) must be left completely
    untouched by the backfill -- never overwritten, never duplicated."""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    _apply_migrations_up_to(conn, 5)
    conn.execute(
        "INSERT INTO price_bars (source_id, ticker, event_time, retrieved_at, timeframe, "
        "open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("real-spy-1", "SPY", "2026-09-02", "2026-09-02T21:00:00+00:00", "1d",
         760.0, 762.0, 758.0, 761.5, 1_000_000),
    )
    bar_id = conn.execute("SELECT id FROM price_bars WHERE source_id = 'real-spy-1'").fetchone()[0]
    conn.execute(
        "INSERT INTO price_bar_current (ticker, timeframe, event_time, price_bar_id, is_final, retrieved_at) "
        "VALUES ('SPY', '1d', '2026-09-02', ?, 1, '2026-09-02T21:00:00+00:00')",
        (bar_id,),
    )
    conn.commit()
    conn.close()

    cat = IngestionCatalog(db_path)
    conn2 = sqlite3.connect(db_path)
    rows = conn2.execute(
        "SELECT is_final, price_bar_id FROM price_bar_current WHERE ticker = 'SPY' AND event_time = '2026-09-02'"
    ).fetchall()
    conn2.close()
    assert len(rows) == 1, "backfill must never create a second pointer for an already-pointered date"
    assert rows[0] == (1, bar_id), "the pre-existing real (final) pointer must be left completely unmodified"


def test_running_migrations_twice_against_the_same_db_is_idempotent(tmp_path):
    """Re-running migrations (the real startup behavior -- every process
    boot calls run_migrations again) must be a safe no-op the second
    time, including migration 6's backfill INSERT."""
    db_path = str(tmp_path / "idempotent.db")
    cat = IngestionCatalog(db_path)
    cat.catalog_price_bars("SPY", "1d", [{"t": "2026-09-03", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}])

    conn = sqlite3.connect(db_path)
    newly_applied_again = run_migrations(conn, db_path)
    assert newly_applied_again == [], "every migration (including 6) must already be recorded as applied -- a no-op"
    pointer_count = conn.execute(
        "SELECT COUNT(*) FROM price_bar_current WHERE ticker = 'SPY' AND event_time = '2026-09-03'"
    ).fetchone()[0]
    conn.close()
    assert pointer_count == 1, "repeated migration runs must never duplicate a pointer row"


def test_price_bar_current_latest_view_resolves_the_correct_joined_row(tmp_path):
    """The documented VIEW this round adds must return the exact same
    current bar the Python-level pointer resolution already returns --
    confirming it's a real, usable, correct single query path."""
    cat = IngestionCatalog(str(tmp_path / "view.db"))
    cat.catalog_price_bars("NVDA", "1d", [{"t": "2026-09-04", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 500}])
    conn = sqlite3.connect(cat.db_path)
    row = conn.execute(
        "SELECT ticker, timeframe, event_time, open, high, low, close, volume, is_final "
        "FROM price_bar_current_latest WHERE ticker = 'NVDA'"
    ).fetchone()
    conn.close()
    # No explicit retrieved_at was passed -- catalog_price_bars defaults
    # it to "now" (real wall-clock time), which is long past 2026-09-04's
    # real session close, so this bar correctly resolves as final.
    assert row == ("NVDA", "1d", "2026-09-04", 10.0, 11.0, 9.0, 10.5, 500.0, 1)


def test_price_bar_current_unique_constraint_rejects_a_genuine_duplicate(tmp_path):
    """The schema-level invariant this round's investigation confirmed
    already held (UNIQUE(ticker, timeframe, event_time) since migration
    5) -- a raw second INSERT (not an upsert) for the same key must be
    rejected by SQLite itself, not just by application-level discipline."""
    cat = IngestionCatalog(str(tmp_path / "unique.db"))
    cat.catalog_price_bars("SPY", "1d", [{"t": "2026-09-05", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}])
    conn = sqlite3.connect(cat.db_path)
    bar_id = conn.execute("SELECT id FROM price_bars WHERE ticker = 'SPY'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price_bar_current (ticker, timeframe, event_time, price_bar_id, is_final, retrieved_at) "
            "VALUES ('SPY', '1d', '2026-09-05', ?, 0, 'x')",
            (bar_id,),
        )
    conn.close()


def test_non_final_date_revision_rows_are_bounded_not_unbounded(tmp_path):
    """R8-4 requirement: repeated genuine content changes for a STILL-
    NON-FINAL date must not accumulate unbounded raw price_bars rows --
    the oldest superseded revisions beyond PRICE_BAR_REVISION_KEEP are
    pruned, while the CURRENT pointer row is always preserved."""
    cat = IngestionCatalog(str(tmp_path / "bounded.db"))
    ticker, tf, trading_date = "SPY", "1d", "2026-09-06"
    # Simulate a live, still-in-progress session polled many times with
    # genuinely different content each time (well beyond the keep bound).
    n = PRICE_BAR_REVISION_KEEP + 15
    for i in range(n):
        bar = [{"t": trading_date, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0 + i * 0.01, "v": 1000 + i}]
        retrieved_at = f"2026-09-06T15:{i:02d}:00+00:00"  # all mid-session -- never final
        cat.catalog_price_bars(ticker, tf, bar, retrieved_at=retrieved_at)

    conn = sqlite3.connect(cat.db_path)
    total_rows = conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE ticker = ? AND timeframe = ? AND event_time = ?",
        (ticker, tf, trading_date),
    ).fetchone()[0]
    pointer = conn.execute(
        "SELECT price_bar_id, is_final FROM price_bar_current WHERE ticker = ? AND event_time = ?",
        (ticker, trading_date),
    ).fetchone()
    conn.close()

    assert total_rows <= PRICE_BAR_REVISION_KEEP + 1, (
        f"expected bounded growth (<= {PRICE_BAR_REVISION_KEEP + 1} rows: current + {PRICE_BAR_REVISION_KEEP} kept "
        f"superseded), got {total_rows} -- revision rows are accumulating unbounded"
    )
    assert pointer is not None and pointer[1] == 0, "still non-final after this many mid-session updates -- sanity check on the fixture"
    # The CURRENT pointer's own bar must always survive pruning.
    conn = sqlite3.connect(cat.db_path)
    current_still_exists = conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE id = ?", (pointer[0],)
    ).fetchone()[0]
    conn.close()
    assert current_still_exists == 1, "the CURRENT pointer's own row must never be pruned"


def test_final_date_revision_rows_are_never_pruned_further(tmp_path):
    """Once a date is confirmed final, _prune_superseded_price_bar_revisions
    must be a no-op for it -- a final bar's prior revision history (e.g.
    the mid-session values leading up to the real close) is frozen in
    place, never retroactively thinned out just because more OTHER
    dates get pruning activity."""
    cat = IngestionCatalog(str(tmp_path / "frozen.db"))
    ticker, tf, trading_date = "SPY", "1d", "2026-10-14"  # confirmed real NYSE trading day (see test_ingestion.py's own fixture)
    from datetime import timedelta
    from app.core.trading_calendar import session_end_utc
    from datetime import date as _date
    close_utc = session_end_utc(_date(2026, 10, 14))
    assert close_utc is not None, "2026-10-14 must be a real NYSE trading day for this fixture"
    mid_session = (close_utc - timedelta(hours=2)).isoformat()
    after_grace = (close_utc + timedelta(minutes=25)).isoformat()

    cat.catalog_price_bars(ticker, tf, [{"t": trading_date, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}], retrieved_at=mid_session)
    cat.catalog_price_bars(ticker, tf, [{"t": trading_date, "o": 1, "h": 2, "l": 0.5, "c": 1.6, "v": 11}], retrieved_at=after_grace)

    conn = sqlite3.connect(cat.db_path)
    before_count = conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE ticker = ? AND event_time = ?", (ticker, trading_date)
    ).fetchone()[0]
    _prune_superseded_price_bar_revisions(conn, ticker, tf, trading_date, keep=0)
    conn.commit()
    after_count = conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE ticker = ? AND event_time = ?", (ticker, trading_date)
    ).fetchone()[0]
    conn.close()
    assert before_count == 2, "sanity check: two real revisions exist (mid-session + final)"
    assert after_count == before_count, "a FINAL date's revisions must never be pruned, even with keep=0"
