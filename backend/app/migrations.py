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
    # Phase D (2026-10-15): scheduler lease + durable mode/state tables.
    # Both ADDITIVE ONLY, same as every migration above -- new tables,
    # never an ALTER/DROP.
    #
    #   scheduler_lease -- a SINGLE-ROW (id=1) table used as an atomic
    #   mutual-exclusion lease so a container restart, a crashed-and-
    #   restarted process, or (if it ever happens) multiple worker
    #   processes cannot run overlapping background collection cycles.
    #   Acquire/renew/release are each ONE atomic UPDATE statement with a
    #   WHERE clause that only succeeds for the rightful owner or an
    #   expired lease (see app/core/scheduler_store.py) -- never a
    #   read-then-write race.
    #
    #   scheduler_state -- a SINGLE-ROW (id=1) table holding the durable
    #   scheduler mode ('disabled' | 'dry_run' | 'enabled') plus
    #   last/next-run bookkeeping. CRITICAL: this migration's CREATE
    #   TABLE IF NOT EXISTS is a no-op against a database that already
    #   has this table from a prior run of this exact migration -- so an
    #   upgrade that re-runs this migration (always safe/idempotent, see
    #   module docstring) NEVER resets an existing operator-set mode row
    #   back to the hardcoded 'disabled' default. The default row is only
    #   ever inserted (via INSERT OR IGNORE in scheduler_store.py, not
    #   here) the FIRST time the table is empty -- a genuinely fresh
    #   install. See backend/tests/test_scheduler_store.py's
    #   test_upgrade_preserves_existing_mode for the exact regression this
    #   guards against.
    Migration(
        version=4,
        name="scheduler_lease_and_state",
        sql="""
CREATE TABLE IF NOT EXISTS scheduler_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    worker_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lease_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'disabled',
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    last_run_started_at TEXT,
    last_run_finished_at TEXT,
    next_run_at TEXT,
    last_cycle_candidates_evaluated INTEGER,
    pause_reason TEXT
);
""",
    ),
    # chart-data-integrity-fixes (2026-10-18, Issue 2 -- stale daily price
    # bar): root cause was `catalog_price_bars`'s plain INSERT OR IGNORE,
    # keyed on a source_id that is a fingerprint of (ticker, timeframe,
    # date) ONLY -- never the bar's own OHLC content. Once any row for a
    # given date existed, a LATER re-fetch for that SAME date (e.g. after
    # the real session closed and UW's own numbers settled) produced the
    # exact same source_id and was silently discarded by the UNIQUE
    # constraint every single time, permanently locking in whichever
    # value -- complete or partial-session -- happened to be cataloged
    # first. Confirmed as the real cause of a genuine cross-installation
    # SPY Oct-16 discrepancy ($771.36 cataloged mid-session vs. $763.25
    # the real final close).
    #
    # DELIBERATELY a NEW table, same design rule as every migration
    # above (CREATE TABLE/INDEX IF NOT EXISTS only, never ALTER/DROP on
    # an existing table -- see darkpool_coverage_status's own comment
    # for this same pattern) -- and `price_bars` itself stays genuinely
    # append-only (a corrected/more-complete bar for an existing date is
    # inserted as a BRAND NEW row, never an UPDATE on the old one,
    # consistent with this module's "never UPDATE a raw observation row"
    # design rule stated in ingestion.py's own docstring). This table is
    # a POINTER/bookkeeping table (same category as scheduler_state and
    # ingestion_jobs above, which this codebase already updates in place
    # -- it is not itself a raw observation row) recording, per
    # (ticker, timeframe, event_time), which `price_bars.id` is currently
    # the authoritative bar for that date and whether that bar is
    # confirmed FINAL (its real trading session had genuinely closed, by
    # NYSE calendar time, at the moment it was fetched -- see
    # ingestion.py's `_bar_is_final`). Once a bar is confirmed final it
    # is permanently locked: no later fetch, however different, is ever
    # allowed to replace it through this path (a genuine post-close UW
    # correction would need the existing, explicit, caller-reviewed
    # reconcile_correction/source_corrections mechanism, same as
    # darkpool/flow corrections already use -- never this automatic
    # path). A non-final bar CAN be replaced, but only by a fetch that is
    # not older/staler (directional: `retrieved_at` must not regress).
    Migration(
        version=5,
        name="price_bar_freshness_tracking",
        sql="""
CREATE TABLE IF NOT EXISTS price_bar_current (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    event_time TEXT NOT NULL,
    price_bar_id INTEGER NOT NULL REFERENCES price_bars(id),
    is_final INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT NOT NULL,
    UNIQUE(ticker, timeframe, event_time)
);
CREATE INDEX IF NOT EXISTS idx_price_bar_current_ticker_timeframe_event
    ON price_bar_current(ticker, timeframe, event_time);
""",
    ),
    # R8-4 (chart-data-integrity-fixes round 9, 2026-10-18): investigation
    # finding -- a database that already had `price_bars` rows from
    # BEFORE migration 5 ever ran (i.e. before `price_bar_current`
    # existed at all) never gets a pointer row backfilled for those
    # pre-existing dates. `catalog_price_bars` only ever INSERTs a
    # pointer the next time that exact date is genuinely re-fetched --
    # a legacy date that's never refetched again (common for an older
    # historical daily bar once a ticker stops being actively charted)
    # stays pointer-less forever. That's harmless for
    # `get_cached_price_bars` (its LEFT JOIN already treats
    # `cur.price_bar_id IS NULL` as "keep the one legacy row, no
    # resolution needed" -- confirmed correct, see that method's own
    # comment), but it DOES mean `get_latest_daily_bar_final_status`
    # (R8-3, reads `price_bar_current` directly) would wrongly report
    # None (looks like "nothing cataloged") for a ticker whose most
    # recent daily bar predates migration 5, even though real OHLC data
    # exists for it.
    #
    # Separately: does the schema allow more than one "current" row for
    # the same (ticker, timeframe, event_time)? No -- `price_bar_current`
    # has carried `UNIQUE(ticker, timeframe, event_time)` since migration
    # 5, and every write path (`catalog_price_bars`'s own
    # `INSERT ... ON CONFLICT(...) DO UPDATE`) is a single atomic
    # upsert statement -- SQLite's own file-level write serialization
    # plus that UNIQUE constraint together already guarantee exactly one
    # row per key even under concurrent writers or a crash mid-write (a
    # partial transaction is rolled back entirely, never left as a
    # duplicate row; the UNIQUE index makes a second row for the same
    # key impossible even if application logic somehow tried an INSERT
    # instead of the upsert). No schema change needed for that
    # guarantee -- it already held before this migration. What WAS
    # missing is the backfill above, and a documented single query path
    # for "the current bar for date X" (the VIEW below) so no future
    # caller hand-rolls the price_bar_current/price_bars join
    # differently (and potentially wrongly) elsewhere.
    #
    # SAFE TO RUN against an existing populated database: this is a
    # NEW, ADDITIVE migration (idempotent backfill INSERT with a
    # NOT EXISTS guard + a CREATE VIEW IF NOT EXISTS) -- never an
    # ALTER/DROP, never touches an existing row's own columns, and
    # re-running it (e.g. migration replay in a test) is a safe no-op
    # the second time since every already-pointered date is skipped by
    # the NOT EXISTS clause. This is evaluated and intended for use ONLY
    # against this project's own sandbox/local database -- never a
    # deployed/production instance -- per this round's hard constraints.
    Migration(
        version=6,
        name="price_bar_current_backfill_and_view",
        sql="""
INSERT INTO price_bar_current (ticker, timeframe, event_time, price_bar_id, is_final, retrieved_at)
SELECT pb.ticker, pb.timeframe, pb.event_time, pb.id,
    -- Backfilled legacy pointers are conservatively marked NOT final
    -- (0), never guessed True -- same safe-default convention
    -- ingestion.py's `_bar_is_final` already uses for any uncertain
    -- case. A non-final pointer can still be freely corrected by the
    -- normal catalog_price_bars path the next time this date is
    -- genuinely refetched; a wrongly-guessed-final pointer could not.
    0,
    pb.retrieved_at
FROM price_bars pb
WHERE pb.timeframe = '1d'
    -- Exactly one row backfilled per legacy date: the MOST RECENT
    -- (highest id -> latest INSERT) of however many legacy rows that
    -- date happens to have, consistent with "the newest value we ever
    -- saw is the best guess absent a pointer" -- this mirrors
    -- get_cached_price_bars's own existing selection intent for an
    -- unpointered date, just made explicit here as a real pointer row.
    AND pb.id = (
        SELECT MAX(pb2.id) FROM price_bars pb2
        WHERE pb2.ticker = pb.ticker AND pb2.timeframe = pb.timeframe
            AND pb2.event_time = pb.event_time
    )
    -- Idempotent / safe-to-rerun guard: never backfill a date that
    -- already has a real pointer (whether from a normal live fetch or
    -- an earlier run of this same migration).
    AND NOT EXISTS (
        SELECT 1 FROM price_bar_current pc
        WHERE pc.ticker = pb.ticker AND pc.timeframe = pb.timeframe
            AND pc.event_time = pb.event_time
    );

-- The ONE documented, unambiguous SQL-level path for "the current bar
-- for (ticker, timeframe, event_time)" -- any future direct-SQL caller
-- should query this VIEW rather than hand-rolling the
-- price_bar_current/price_bars join (which must always resolve via the
-- pointer, never a raw MAX(id)/ORDER BY event_time scan over
-- price_bars alone -- that would pick up a superseded/non-current
-- revision whenever one exists). get_latest_daily_bar_final_status /
-- get_cached_price_bars in ingestion.py are the Python-level
-- equivalents of this same resolution rule.
CREATE VIEW IF NOT EXISTS price_bar_current_latest AS
SELECT pc.ticker, pc.timeframe, pc.event_time, pc.is_final, pc.retrieved_at,
    pb.id AS price_bar_id, pb.open, pb.high, pb.low, pb.close, pb.volume
FROM price_bar_current pc
JOIN price_bars pb ON pb.id = pc.price_bar_id;
""",
    ),
    # Signal-integrity remediation (audit B5/B6/B7/E7/E8): episode-scoped,
    # trading-session-counted lifecycle. ADDITIVE ONLY -- the legacy
    # ticker_scan_log / ticker_current_state tables are left exactly as
    # they are (audit history, possibly containing pre-isolation Demo rows;
    # see tools/audit_demo_live_contamination.py) and are no longer read
    # or written by lifecycle decisions.
    #   lifecycle_episodes        one row per qualification episode
    #   lifecycle_session_evidence per (episode, NYSE trading session):
    #                              whether a complete scan in that session
    #                              had qualifying / confirmed evidence, plus
    #                              the evidence fingerprints seen
    #   lifecycle_scans            every complete scan's decision (audit log)
    Migration(
        version=7,
        name="lifecycle_episodes",
        sql="""
CREATE TABLE IF NOT EXISTS lifecycle_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    opened_session TEXT NOT NULL,
    state TEXT NOT NULL,
    reference_price REAL,
    last_qualifying_session TEXT,
    closed_at TEXT,
    closed_session TEXT,
    close_reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_episodes_ticker ON lifecycle_episodes(ticker, id DESC);

CREATE TABLE IF NOT EXISTS lifecycle_session_evidence (
    episode_id INTEGER NOT NULL REFERENCES lifecycle_episodes(id),
    session_date TEXT NOT NULL,
    qualifying INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    scans INTEGER NOT NULL DEFAULT 0,
    fingerprints TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (episode_id, session_date)
);

CREATE TABLE IF NOT EXISTS lifecycle_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    episode_id INTEGER,
    scanned_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    qualifying INTEGER NOT NULL,
    confirmed INTEGER NOT NULL,
    state_before TEXT,
    state_after TEXT,
    rule TEXT,
    evidence_fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_scans_ticker ON lifecycle_scans(ticker, id);
""",
    ),
]

MIGRATIONS.append(Migration(
    version=8,
    name="session_coverage",
    sql="""
-- Signal-integrity remediation (audit B1/B8): which part of an NYSE trading
-- session this app has VERIFIABLY collected, per dataset ('darkpool' |
-- 'options_flow'). [covered_from_utc, covered_through_utc] is one
-- contiguous interval that backward pagination walked without a gap.
-- status: 'complete' (regular session open..close covered), 'partial'
-- (contiguous interval that does not yet span the session), 'unknown'
-- (coverage could not be established: empty first page, pagination
-- parameter not honored, non-advancing cursor, provider error).
CREATE TABLE IF NOT EXISTS session_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    session_date TEXT NOT NULL,
    status TEXT NOT NULL,
    covered_from_utc TEXT,
    covered_through_utc TEXT,
    pages_walked INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE(dataset, ticker, session_date)
);
CREATE INDEX IF NOT EXISTS idx_session_coverage_lookup ON session_coverage(dataset, ticker, session_date DESC);
""",
))

# Migrations at or above this version take a fresh pre-migration backup of an
# existing, non-empty database file before they are applied (in addition to
# the one-time first-run backup), so upgrading a real Live install is always
# reversible by restoring that file.
BACKUP_BEFORE_VERSION = 7


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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = src.with_name(f"{src.name}.pre-migration-{stamp}.bak")
    n = 1
    while backup_path.exists():  # never overwrite an earlier backup
        backup_path = src.with_name(f"{src.name}.pre-migration-{stamp}-{n}.bak")
        n += 1
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


def _has_user_rows(conn: sqlite3.Connection) -> bool:
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if conn.execute(f"SELECT 1 FROM [{name}] LIMIT 1").fetchone():
            return True
    return False


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
    if first_run_ever and _has_user_rows(conn):
        # (a brand-new, empty database has nothing to protect -- backing it
        # up only left meaningless .bak files on every fresh install)
        backup_db_file(db_path)
    _ensure_schema_version_table(conn)
    conn.commit()

    applied_already = _applied_versions(conn)
    pending = [m for m in MIGRATIONS if m.version not in applied_already]
    if (not first_run_ever and applied_already
            and any(m.version >= BACKUP_BEFORE_VERSION for m in pending)):
        backup_db_file(db_path)
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
