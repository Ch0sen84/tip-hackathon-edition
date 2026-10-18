"""Phase A: migration-runner tests. Applies migrations to a copy of a
realistic PRE-EXISTING db (constructed fixture with the two real existing
tables, populated with rows -- never the real deployed DB) and confirms
original data survives untouched plus every new catalog table exists.
All paths here are tmp_path-backed, isolated temp files -- never
':memory:'-only (a real backup-on-first-run needs a real file to copy).
"""
from __future__ import annotations

import sqlite3

from app.migrations import MIGRATIONS, backup_db_file, run_migrations
from app.storage import QualifyingDaysStore


def _build_pre_existing_fixture_db(path) -> None:
    """A tiny, realistic stand-in for a real deployed DB: the two tables
    that exist TODAY (ticker_scan_log/ticker_current_state), populated
    with real-shaped rows, and NO schema_version table at all -- exactly
    what a pre-Phase-A deployed install's DB file looks like."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE ticker_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            scan_date TEXT NOT NULL,
            any_gate_passed INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE ticker_current_state (
            ticker TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO ticker_scan_log (ticker, scan_date, any_gate_passed, state) VALUES "
        "('FLUX', '2026-10-01', 1, 'CONFIRMED')"
    )
    conn.execute(
        "INSERT INTO ticker_scan_log (ticker, scan_date, any_gate_passed, state) VALUES "
        "('FLUX', '2026-10-02', 1, 'STABLE')"
    )
    conn.execute("INSERT INTO ticker_current_state (ticker, state) VALUES ('FLUX', 'STABLE')")
    conn.commit()
    conn.close()


NEW_CATALOG_TABLES = (
    "options_flow_observations",
    "darkpool_observations",
    "darkpool_daily_coverage",
    "price_bars",
    "discovery_snapshots",
    "discovery_candidates",
    "signal_evaluations",
    "signal_transitions",
    "ai_investigations",
    "ingestion_jobs",
    "price_bar_current",
)


def _table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_migrations_create_schema_version_and_all_catalog_tables(tmp_path):
    db_path = tmp_path / "pre_existing.db"
    _build_pre_existing_fixture_db(db_path)

    conn = sqlite3.connect(str(db_path))
    applied = run_migrations(conn, str(db_path))
    conn.commit()

    assert applied == [m.version for m in MIGRATIONS]
    tables = _table_names(conn)
    assert "schema_version" in tables
    for table in NEW_CATALOG_TABLES:
        assert table in tables, f"expected catalog table {table!r} to exist after migration"
    conn.close()


def test_migrations_never_touch_pre_existing_data(tmp_path):
    db_path = tmp_path / "pre_existing.db"
    _build_pre_existing_fixture_db(db_path)

    conn = sqlite3.connect(str(db_path))
    run_migrations(conn, str(db_path))
    conn.commit()

    rows = conn.execute(
        "SELECT ticker, scan_date, any_gate_passed, state FROM ticker_scan_log ORDER BY id"
    ).fetchall()
    assert rows == [
        ("FLUX", "2026-10-01", 1, "CONFIRMED"),
        ("FLUX", "2026-10-02", 1, "STABLE"),
    ]
    current = conn.execute("SELECT ticker, state FROM ticker_current_state").fetchall()
    assert current == [("FLUX", "STABLE")]
    conn.close()


def test_migrations_take_one_backup_on_first_run_against_a_real_file(tmp_path):
    db_path = tmp_path / "pre_existing.db"
    _build_pre_existing_fixture_db(db_path)

    before = set(tmp_path.iterdir())
    conn = sqlite3.connect(str(db_path))
    run_migrations(conn, str(db_path))
    conn.commit()
    conn.close()
    after = set(tmp_path.iterdir())

    new_files = after - before
    backups = [p for p in new_files if ".pre-migration-" in p.name and p.name.endswith(".bak")]
    assert len(backups) == 1, f"expected exactly one backup file, got {backups}"

    # The backup is a real, byte-identical pre-migration snapshot -- it
    # must NOT have schema_version or any new catalog table (it was taken
    # BEFORE migration 1 ran).
    backup_conn = sqlite3.connect(str(backups[0]))
    backup_tables = _table_names(backup_conn)
    assert "schema_version" not in backup_tables
    assert "options_flow_observations" not in backup_tables
    assert "ticker_scan_log" in backup_tables
    backup_rows = backup_conn.execute("SELECT ticker FROM ticker_scan_log").fetchall()
    assert backup_rows == [("FLUX",), ("FLUX",)]
    backup_conn.close()


def test_migrations_are_idempotent_second_run_applies_nothing_new(tmp_path):
    db_path = tmp_path / "pre_existing.db"
    _build_pre_existing_fixture_db(db_path)

    conn = sqlite3.connect(str(db_path))
    first_run = run_migrations(conn, str(db_path))
    conn.commit()
    assert first_run  # something was actually applied the first time

    second_run = run_migrations(conn, str(db_path))
    conn.commit()
    assert second_run == []  # nothing new to apply -- already up to date
    conn.close()


def test_migrations_never_run_twice_create_two_backups(tmp_path):
    """The backup only happens on the run where schema_version doesn't
    exist yet -- a second process start against the same, now-migrated
    file must not create a second backup."""
    db_path = tmp_path / "pre_existing.db"
    _build_pre_existing_fixture_db(db_path)

    conn = sqlite3.connect(str(db_path))
    run_migrations(conn, str(db_path))
    conn.commit()
    conn.close()

    before = set(tmp_path.iterdir())
    conn2 = sqlite3.connect(str(db_path))
    run_migrations(conn2, str(db_path))
    conn2.commit()
    conn2.close()
    after = set(tmp_path.iterdir())
    assert after == before  # no new backup file created on the second run


def test_backup_db_file_is_a_noop_for_memory_and_nonexistent_paths(tmp_path):
    assert backup_db_file(":memory:") is None
    assert backup_db_file(str(tmp_path / "does_not_exist.db")) is None


def test_qualifying_days_store_runs_migrations_on_construction(tmp_path):
    """End-to-end: QualifyingDaysStore (the real class the app uses) wires
    the migration runner in automatically -- not just unit-testable in
    isolation."""
    db_path = tmp_path / "real_store.db"
    store = QualifyingDaysStore(db_path=str(db_path))
    store.record_scan("TESTX", True, "NEW")

    conn = sqlite3.connect(str(db_path))
    tables = _table_names(conn)
    assert "schema_version" in tables
    for table in NEW_CATALOG_TABLES:
        assert table in tables
    # Pre-existing functionality still works, untouched.
    assert store.qualifying_days_count("TESTX") >= 1
    conn.close()
