"""Phase A: versioned, additive, idempotent SQLite migrations.

SAFE to run against the REAL existing persistent SQLite file (the one
with `ticker_scan_log` / `ticker_current_state` already in it, real rows
and all) -- every migration here is ADDITIVE ONLY (new tables/indexes via
CREATE TABLE/INDEX IF NOT EXISTS), never an ALTER or DROP on an existing
table. The runner also takes a timestamped file-copy backup of the DB file
the FIRST time any migration ever runs against it (detected by the
absence of a `schema_version` table), before touching anything else.

Each migration is applied inside its own transaction and recorded in
`schema_version` (version, name, applied_at) so re-running this on every
process start is always a safe no-op once everything is already applied.

Phase A only creates the catalog foundation tables (schema only) --
nothing in this phase ever writes a row into them; Phase B populates them.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional


class Migration(NamedTuple):
    version: int
    name: str
    sql: str


# All timestamp columns are TEXT/ISO8601, matching the rest of this
# codebase's convention (see storage.py's existing `scan_date`/
# `created_at`). `event_time` (source truth, i.e. when UW says the event
# happened) and `retrieved_at` (our truth, i.e. when THIS app fetched it)
# are always kept as separate columns on every raw-observation table --
# never collapsed into one "timestamp" field -- so a replay/backfill can
# always tell the two apart. `UNIQUE(source_id)` on every raw table is the
# dedup key for safe re-ingestion of the same upstream record.
MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="catalog_foundation_tables",
        sql="""
CREATE TABLE IF NOT EXISTS options_flow_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_options_flow_obs_ticker_event_time
    ON options_flow_observations(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS darkpool_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_darkpool_obs_ticker_event_time
    ON darkpool_observations(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS darkpool_daily_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    coverage_date TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE(ticker, coverage_date)
);
CREATE INDEX IF NOT EXISTS idx_darkpool_coverage_ticker_date
    ON darkpool_daily_coverage(ticker, coverage_date DESC);

CREATE TABLE IF NOT EXISTS price_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL
);
CREATE INDEX IF NOT EXISTS idx_price_bars_ticker_event_time
    ON price_bars(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS discovery_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    filters_json TEXT,
    candidate_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_discovery_snapshots_event_time
    ON discovery_snapshots(event_time DESC);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    snapshot_id INTEGER NOT NULL REFERENCES discovery_snapshots(id),
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_ticker_event_time
    ON discovery_candidates(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS signal_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    state TEXT NOT NULL,
    confidence TEXT,
    engine_rule_version TEXT NOT NULL,
    superseded_by_evaluation_id INTEGER REFERENCES signal_evaluations(id),
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_evaluations_ticker_event_time
    ON signal_evaluations(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS signal_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    transition_rule TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_transitions_ticker_event_time
    ON signal_transitions(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS ai_investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    transport TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_investigations_ticker_event_time
    ON ai_investigations(ticker, event_time DESC);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    job_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_type_event_time
    ON ingestion_jobs(job_type, event_time DESC);
""",
    ),
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_db_file(db_path: str) -> Optional[str]:
    """Timestamped file-copy backup, same directory as the original --
    taken ONCE, the very first time any migration ever runs against this
    db file. Returns the backup path, or None when there is nothing to
    back up (':memory:', or a brand-new DB file that doesn't exist yet)."""
    if db_path == ":memory:":
        return None
    src = Path(db_path)
    if not src.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = src.with_name(f"{src.name}.pre-migration-{stamp}.bak")
    shutil.copy2(src, backup_path)
    return str(backup_path)


def _schema_version_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    return row is not None


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {r[0] for r in rows}


def run_migrations(conn: sqlite3.Connection, db_path: str) -> list:
    """Applies every not-yet-applied migration from MIGRATIONS, in
    version order, each in its own transaction, recording it in
    `schema_version`. Takes one pre-migration backup (see
    backup_db_file()) the first time this is ever called against a real,
    pre-existing db file that doesn't already have a `schema_version`
    table. Returns the list of version numbers actually applied in this
    call (empty if everything was already up to date). Never
    destroys/alters `ticker_scan_log` or `ticker_current_state` -- every
    migration here only ever adds new tables/indexes.
    """
    first_run_ever = not _schema_version_table_exists(conn)
    if first_run_ever:
        backup_db_file(db_path)
    _ensure_schema_version_table(conn)
    conn.commit()

    applied_already = _applied_versions(conn)
    newly_applied: list = []
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version in applied_already:
            continue
        conn.executescript(migration.sql)
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, _utcnow_iso()),
        )
        conn.commit()
        newly_applied.append(migration.version)
    return newly_applied
