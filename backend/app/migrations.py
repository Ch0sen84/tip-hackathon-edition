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
    # Phase B (2026-10-15): two ADDITIVE-ONLY mechanisms on top of the
    # Phase A catalog-foundation schema above -- never an ALTER/DROP on
    # any existing table or column.
    #
    #   1. signal_evaluation_evidence -- a many-to-many join table so one
    #      raw observation (an options_flow_observations/
    #      darkpool_observations/price_bars row, etc.) can be cited by
    #      MULTIPLE signal_evaluations rows, and one evaluation can cite
    #      MULTIPLE observations (Chad's explicit requirement). Keyed by
    #      (observation_table, observation_id) rather than a hard FK to
    #      one specific table, since the cited observation can legally
    #      live in any one of several raw tables.
    #
    #   2. source_corrections -- append-only correction history. A
    #      revised/corrected upstream record is NEVER applied as an
    #      in-place UPDATE on the original observation row (that would
    #      destroy the original value this app once legitimately saw).
    #      Instead the normal ingestion write path inserts a brand-new
    #      row into the SAME raw table (options_flow_observations etc.,
    #      new id, new source_id) for the corrected value, and this
    #      table records one append-only link: the NEW row
    #      `supersedes_observation_id` the OLD row. Both rows -- and the
    #      full chain across however many corrections happen over time --
    #      remain readable forever; nothing is ever deleted or rewritten.
    Migration(
        version=2,
        name="signal_evidence_and_source_corrections",
        sql="""
CREATE TABLE IF NOT EXISTS signal_evaluation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL REFERENCES signal_evaluations(id),
    observation_table TEXT NOT NULL,
    observation_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(evaluation_id, observation_table, observation_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_evaluation_evidence_evaluation
    ON signal_evaluation_evidence(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_signal_evaluation_evidence_observation
    ON signal_evaluation_evidence(observation_table, observation_id);

CREATE TABLE IF NOT EXISTS source_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_table TEXT NOT NULL,
    -- The NEW (corrected) row's id in `observation_table` -- this is
    -- the row application code should now treat as current for this
    -- source_id lineage. UNIQUE: a given new observation row can only
    -- ever supersede exactly one prior row.
    observation_id INTEGER NOT NULL,
    -- The OLD (now-superseded) row's id in the SAME `observation_table`.
    -- Never deleted, never updated -- this column is the only thing
    -- that changes: a pointer recorded alongside the new row.
    supersedes_observation_id INTEGER NOT NULL,
    correction_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(observation_table, observation_id)
);
CREATE INDEX IF NOT EXISTS idx_source_corrections_supersedes
    ON source_corrections(observation_table, supersedes_observation_id);
CREATE INDEX IF NOT EXISTS idx_source_corrections_table_new
    ON source_corrections(observation_table, observation_id);
""",
    ),
    # Phase B dark-pool pagination coverage tracking. DELIBERATELY a NEW
    # table rather than an ALTER TABLE ... ADD COLUMN on the existing
    # (Phase A, schema-only) `darkpool_daily_coverage` table: this
    # module's own design rule, stated above and enforced by every prior
    # migration here, is CREATE TABLE/INDEX IF NOT EXISTS only, never an
    # ALTER on an existing table -- so the three-state (complete /
    # incomplete / unknown) coverage classification pagination needs
    # gets its own additive table instead of bending that rule. See
    # app/dark_pool_pagination.py for the writer and the Phase B FINAL
    # REPORT for this deviation's rationale (flagged there explicitly,
    # not silently substituted).
    Migration(
        version=3,
        name="darkpool_coverage_status",
        sql="""
CREATE TABLE IF NOT EXISTS darkpool_coverage_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    coverage_date TEXT NOT NULL,
    -- One of: 'complete' (pagination verifiably walked back to/through
    -- the session's real calendar-aware start time), 'incomplete'
    -- (pagination stopped before that, e.g. hit max_pages), or
    -- 'unknown' (coverage could not be confidently established at all --
    -- e.g. a non-advancing cursor, an empty first page, or an
    -- unavailable/erroring response). NEVER inferred from a short page
    -- alone.
    status TEXT NOT NULL,
    pages_walked INTEGER NOT NULL DEFAULT 0,
    session_start_verified INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT NOT NULL,
    UNIQUE(ticker, coverage_date)
);
CREATE INDEX IF NOT EXISTS idx_darkpool_coverage_status_ticker_date
    ON darkpool_coverage_status(ticker, coverage_date DESC);
""",
    ),
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_db_file(db_path: str) -> Optional[str]:
    """Timestamped, crash-consistent backup, same directory as the
    original -- taken ONCE, the very first time any migration ever runs
    against this db file. Returns the backup path, or None when there is
    nothing to back up (':memory:', or a brand-new DB file that doesn't
    exist yet).

    SAFETY FIX (Phase B, 2026-10-15): this used to be `shutil.copy2(src,
    backup_path)` -- a raw filesystem copy. SQLite in WAL mode (or mid-
    write in rollback-journal mode) can have in-flight pages that only
    live in the `-wal`/`-journal` sidecar file or the page cache, not yet
    flushed to the main `.db` file on disk; a plain file copy of just the
    main file can silently capture an inconsistent/truncated snapshot
    that looks fine until someone tries to actually restore it. Use
    SQLite's own backup API instead (`sqlite3.Connection.backup`, which
    does the same safe, consistent, online copy as `VACUUM INTO`) --
    it opens its own connection to the source, takes SQLite's native
    page-level backup lock, and is safe to call even while another
    connection (this same process's) might be mid-transaction.
    """
    if db_path == ":memory:":
        return None
    src = Path(db_path)
    if not src.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = src.with_name(f"{src.name}.pre-migration-{stamp}.bak")
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
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
