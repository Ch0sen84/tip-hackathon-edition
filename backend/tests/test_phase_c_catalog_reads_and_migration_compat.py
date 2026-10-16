"""Phase C, deliverable 5: NO new migration/table was added this phase --
see the module docstring in app/core/dark_pool_baseline.py and this file's
own docstring below for why on-demand computation off the EXISTING Phase B
tables (darkpool_observations, darkpool_coverage_status) is sufficient at
this scale, plus the two new READ-ONLY accessor methods added to
IngestionCatalog (app/ingestion.py) to support it.

This file proves two things:
  1. Those two new read methods (`list_coverage_status`,
     `list_darkpool_observations_by_date`) work correctly against a
     realistic, already-populated Phase A+B catalog.
  2. Running the full existing migration set (unchanged by this phase)
     against a realistic PRE-EXISTING Phase-A+B-populated DB still
     succeeds and preserves every row already in it -- i.e. Phase C
     shipped with ZERO schema changes is genuinely a safe, additive
     no-op from a migration-compatibility standpoint.
"""
from __future__ import annotations

import json
import sqlite3

from app.ingestion import IngestionCatalog
from app.migrations import MIGRATIONS, run_migrations


def _build_phase_a_plus_b_populated_fixture(path) -> None:
    """A realistic stand-in for a real deployed DB that has already run
    through Phase A's migrations AND Phase B's ingestion -- i.e. it
    already has catalog tables AND real rows in darkpool_observations /
    darkpool_coverage_status, not just empty schema."""
    conn = sqlite3.connect(str(path))
    run_migrations(conn, str(path))
    conn.commit()
    conn.execute(
        "INSERT INTO darkpool_observations (source_id, ticker, event_time, retrieved_at, raw_json) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fp_1", "FLUX", "2026-01-02T09:35:00+00:00", "2026-01-02T09:36:00+00:00",
         json.dumps({"premium": 12345.0, "size": 100, "price": 123.45})),
    )
    conn.execute(
        "INSERT INTO darkpool_observations (source_id, ticker, event_time, retrieved_at, raw_json) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fp_2", "FLUX", "2026-01-02T10:00:00+00:00", "2026-01-02T10:01:00+00:00",
         json.dumps({"premium": 999.5, "size": 10, "price": 99.95})),
    )
    conn.execute(
        "INSERT INTO darkpool_observations (source_id, ticker, event_time, retrieved_at, raw_json) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fp_3", "FLUX", "2026-01-05T09:40:00+00:00", "2026-01-05T09:41:00+00:00",
         json.dumps({"premium": 5000.0, "size": 50, "price": 50.0})),
    )
    conn.execute(
        "INSERT INTO darkpool_coverage_status "
        "(ticker, coverage_date, status, pages_walked, session_start_verified, retrieved_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("FLUX", "2026-01-02", "complete", 3, 1, "2026-01-02T16:05:00+00:00"),
    )
    conn.execute(
        "INSERT INTO darkpool_coverage_status "
        "(ticker, coverage_date, status, pages_walked, session_start_verified, retrieved_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("FLUX", "2026-01-05", "incomplete", 50, 0, "2026-01-05T16:05:00+00:00"),
    )
    conn.commit()
    conn.close()


def test_list_coverage_status_reads_back_real_rows(tmp_path):
    db_path = tmp_path / "phase_a_b_populated.db"
    _build_phase_a_plus_b_populated_fixture(db_path)
    catalog = IngestionCatalog(db_path=str(db_path))

    rows = catalog.list_coverage_status("FLUX")
    by_date = {r["coverage_date"]: r for r in rows}
    assert by_date["2026-01-02"]["status"] == "complete"
    assert by_date["2026-01-02"]["session_start_verified"] is True
    assert by_date["2026-01-05"]["status"] == "incomplete"
    assert by_date["2026-01-05"]["session_start_verified"] is False


def test_list_darkpool_observations_by_date_groups_and_extracts_premium(tmp_path):
    db_path = tmp_path / "phase_a_b_populated.db"
    _build_phase_a_plus_b_populated_fixture(db_path)
    catalog = IngestionCatalog(db_path=str(db_path))

    grouped = catalog.list_darkpool_observations_by_date("FLUX")
    assert set(grouped.keys()) == {"2026-01-02", "2026-01-05"}
    assert len(grouped["2026-01-02"]) == 2
    premiums = sorted(o["premium"] for o in grouped["2026-01-02"])
    assert premiums == [999.5, 12345.0]
    assert grouped["2026-01-05"][0]["premium"] == 5000.0


def test_list_methods_return_empty_for_a_ticker_never_cataloged(tmp_path):
    db_path = tmp_path / "phase_a_b_populated.db"
    _build_phase_a_plus_b_populated_fixture(db_path)
    catalog = IngestionCatalog(db_path=str(db_path))

    assert catalog.list_coverage_status("NEVERSEEN") == []
    assert catalog.list_darkpool_observations_by_date("NEVERSEEN") == {}


def test_full_migration_set_against_realistic_phase_a_b_fixture_preserves_data(tmp_path):
    """The migration set itself is UNCHANGED by Phase C (no new table --
    see this file's module docstring). This pins down that re-running it
    against a DB that already has real Phase B rows in it is still a
    safe, idempotent no-op that preserves every row."""
    db_path = tmp_path / "phase_a_b_populated.db"
    _build_phase_a_plus_b_populated_fixture(db_path)

    conn = sqlite3.connect(str(db_path))
    applied = run_migrations(conn, str(db_path))
    conn.commit()
    assert applied == []  # already fully migrated -- nothing new to apply

    obs_count = conn.execute("SELECT COUNT(*) FROM darkpool_observations").fetchone()[0]
    coverage_count = conn.execute("SELECT COUNT(*) FROM darkpool_coverage_status").fetchone()[0]
    assert obs_count == 3
    assert coverage_count == 2
    conn.close()
