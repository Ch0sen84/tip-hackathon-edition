"""Phase B: shared historical-catalog ingestion layer.

Both the EXISTING on-demand Radar / Ticker-Analysis / Discovery routes in
main.py and any FUTURE scheduled background worker (Phase D -- not built
here) call through this ONE module to persist whatever the adapter
already fetched into the Phase-A/B catalog tables (app/migrations.py).

HARD DESIGN RULE (do not violate): this module is a WRITE-ONLY shadow of
whatever the existing live-fetch path already returned to the caller. It
never decides what a Radar/Ticker-Analysis/Discovery response body
contains, never substitutes a cataloged/cached row for a fresh adapter
call, and a failure here must never surface as a failure of the calling
route -- every public function in this module is safe to wrap in a bare
`try/except Exception: pass` by its caller (main.py does exactly that).
backend/app/core/signal_engine.py is never imported or touched here.

Dedup: every raw table has `UNIQUE(source_id)` (see migrations.py). When
the real UW row carries a genuine unique id, that is `source_id`
directly. When it doesn't (confirmed live for dark-pool rows during this
phase's bounded validation -- see FINAL REPORT), a deterministic
fingerprint (sha256 of the fields that together uniquely identify one
real event) stands in, so the exact same upstream event re-ingested
twice (e.g. two overlapping paginated requests) is still suppressed by
the same UNIQUE constraint rather than double-inserted.

Corrections: `catalog_*` functions NEVER UPDATE an existing raw row.
When a caller determines a previously-cataloged source_id's data has
changed upstream (see `reconcile_correction`), the new value is inserted
as a BRAND NEW row (new id, new source_id suffix) and a
`source_corrections` link records old-id -> new-id. The original row is
never touched.

Storage guard: every write here goes through app/storage_guard.py's
EXISTING circuit breaker (no second storage-guard mechanism). Callers
pass `kind="on_demand"` (today's only real caller) or `kind="background"`
(for the Phase D scheduler once it exists) -- see `IngestionWriteGuard`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .migrations import run_migrations
from .storage_guard import BACKGROUND, ONDEMAND, StorageGuard, default_mount_point

ON_DEMAND = ONDEMAND  # re-exported under the name this module's own docs/tests use
BACKGROUND_KIND = BACKGROUND


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(*parts: object) -> str:
    """Deterministic dedup key for an upstream record that carries no
    genuine unique id of its own. Order-sensitive and type-stable (every
    part is str()'d first) so the SAME logical event always produces the
    SAME fingerprint across repeated/overlapping fetches -- that's what
    lets the UNIQUE(source_id) constraint do real dedup work for it."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return "fp_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


class StorageGuardPausedError(RuntimeError):
    """Mirrors app.storage.StorageGuardPausedError but for ingestion
    writes specifically -- raised instead of silently writing when the
    shared storage-guard circuit breaker reports the configured
    `kind` threshold is breached. Callers that want ingestion to be
    best-effort (main.py's on-demand wiring) catch this; a future
    background worker (Phase D) can instead use it to skip a cycle."""

    def __init__(self, result):
        super().__init__(result.reason)
        self.result = result


class IngestionCatalog:
    """Thin SQLite wrapper over the Phase A/B catalog tables, deliberately
    modeled on app/storage.py's QualifyingDaysStore (same ':memory:'
    persistent-connection special case, same file-path open/close-per-call
    pattern, same storage-guard wiring) so it behaves identically under
    test and in the real Docker deployment -- it is NOT a second database;
    `db_path` is the SAME file QualifyingDaysStore already uses, so the
    catalog tables created by app/migrations.py live right alongside
    ticker_scan_log/ticker_current_state in one file, inside the one
    `tip-data` Docker volume.
    """

    def __init__(self, db_path: str = "./data/tip_hackathon.db"):
        self.db_path = db_path
        self._persistent_conn = (
            sqlite3.connect(db_path, check_same_thread=False) if db_path == ":memory:" else None
        )
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._storage_guard: Optional[StorageGuard] = (
            None if db_path == ":memory:" else StorageGuard(default_mount_point(db_path))
        )
        # Runs the SAME versioned migration runner QualifyingDaysStore
        # uses (app/migrations.py) -- idempotent, safe to call from both
        # stores against the same underlying file; whichever constructs
        # first actually creates the schema, the other's call is a
        # no-op. Never a second/competing schema definition.
        with self._conn() as conn:
            run_migrations(conn, self.db_path)

    @contextmanager
    def _conn(self):
        if self._persistent_conn is not None:
            yield self._persistent_conn
            return
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _check_guard(self, kind: str) -> None:
        if self._storage_guard is None:
            return
        result = self._storage_guard.check_before_write(kind)
        if not result.allowed:
            raise StorageGuardPausedError(result)

    # --- raw-observation catalog writes --------------------------------

    def catalog_options_flow_observations(
        self, ticker: str, flow_alerts: Iterable[dict], retrieved_at: Optional[str] = None,
        kind: str = ON_DEMAND,
    ) -> int:
        """Persists each already-normalized flow-alert dict (the EXACT
        shape live_uw_adapter._normalize_flow_alert already produces --
        ask_side/bid_side premium under their real UW names are preserved
        verbatim in raw_json; no new field semantics invented here).
        Returns the number of NEW rows actually inserted (dupes silently
        suppressed by UNIQUE(source_id), not counted as an error)."""
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        inserted = 0
        with self._conn() as conn:
            for alert in flow_alerts or []:
                source_id = (
                    alert.get("id") or alert.get("alert_id")
                    or fingerprint(
                        ticker, alert.get("created_at"), alert.get("total_premium"),
                        alert.get("total_ask_premium"), alert.get("total_bid_premium"),
                        alert.get("type"),
                    )
                )
                event_time = alert.get("created_at") or retrieved_at
                cur = conn.execute(
                    "INSERT OR IGNORE INTO options_flow_observations "
                    "(source_id, ticker, event_time, retrieved_at, raw_json) VALUES (?, ?, ?, ?, ?)",
                    (str(source_id), ticker, event_time, retrieved_at, json.dumps(alert, default=str)),
                )
                if cur.rowcount:
                    inserted += 1
            conn.commit()
        return inserted

    def catalog_darkpool_observations(
        self, ticker: str, darkpool_trades: Iterable[dict], retrieved_at: Optional[str] = None,
        kind: str = ON_DEMAND,
    ) -> int:
        """Confirmed during this phase's bounded live validation: real UW
        dark-pool rows do NOT carry a field that is reliably a genuine
        unique id across repeated/paginated fetches (see FINAL REPORT) --
        a deterministic fingerprint over (ticker, executed_at, price,
        size, premium) is used as source_id whenever no id-shaped field
        is present, which is the common case. When UW DOES send an
        explicit id-like field (`id`/`trade_id`), it is preferred."""
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        inserted = 0
        with self._conn() as conn:
            for trade in darkpool_trades or []:
                source_id = (
                    trade.get("id") or trade.get("trade_id")
                    or fingerprint(
                        ticker, trade.get("executed_at"), trade.get("price"),
                        trade.get("size"), trade.get("premium"),
                    )
                )
                event_time = trade.get("executed_at") or retrieved_at
                cur = conn.execute(
                    "INSERT OR IGNORE INTO darkpool_observations "
                    "(source_id, ticker, event_time, retrieved_at, raw_json) VALUES (?, ?, ?, ?, ?)",
                    (str(source_id), ticker, event_time, retrieved_at, json.dumps(trade, default=str)),
                )
                if cur.rowcount:
                    inserted += 1
            conn.commit()
        return inserted

    def catalog_price_bars(
        self, ticker: str, timeframe: str, candles: Iterable[dict], retrieved_at: Optional[str] = None,
        kind: str = ON_DEMAND,
    ) -> int:
        """Persists the OUTPUT of candles.build_candles() as-is (post
        session-classification/delta-correction) -- never re-derives or
        second-guesses that logic, just stores its result. Candles carry
        no upstream id at all, so source_id is always a fingerprint over
        (ticker, timeframe, t) -- stable across repeated requests for the
        same bar."""
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        inserted = 0
        with self._conn() as conn:
            for bar in candles or []:
                source_id = fingerprint(ticker, timeframe, bar.get("t"))
                event_time = bar.get("t") or retrieved_at
                cur = conn.execute(
                    "INSERT OR IGNORE INTO price_bars "
                    "(source_id, ticker, event_time, retrieved_at, timeframe, open, high, low, close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(source_id), ticker, event_time, retrieved_at, timeframe,
                     bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"), bar.get("v")),
                )
                if cur.rowcount:
                    inserted += 1
            conn.commit()
        return inserted

    # --- S1-b fix (2026-10-16): read-only cache accessors for the OHLC
    # route's GET path -- mirror catalog_price_bars/catalog_options_flow_
    # observations/catalog_darkpool_observations exactly (same tables,
    # same shapes), so GET /api/ticker/{ticker}/ohlc can be made
    # structurally read-only without inventing a second storage layer.
    # These never write anything.

    def get_cached_price_bars(self, ticker: str, timeframe: str, limit: Optional[int] = None) -> list[dict]:
        """Real cataloged candles for (ticker, timeframe), chronological
        (ASC). When more than `limit` rows exist, returns the most
        RECENT `limit` (mirrors how a live fetch's own `limit` behaves --
        "give me the most recent N bars"), never an arbitrary slice."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_time, open, high, low, close, volume FROM price_bars "
                "WHERE ticker = ? AND timeframe = ? ORDER BY event_time ASC",
                (ticker, timeframe),
            ).fetchall()
        bars = [
            {"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in rows
        ]
        if limit is not None and len(bars) > limit:
            bars = bars[-limit:]
        return bars

    def get_cached_flow_observations(self, ticker: str) -> list[dict]:
        """Every real cataloged options-flow-alert raw_json for `ticker`,
        deserialized back to the exact normalized dict shape
        catalog_options_flow_observations was given -- the same shape
        `build_markers` already consumes from a live fetch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM options_flow_observations WHERE ticker = ? ORDER BY event_time ASC",
                (ticker,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def get_cached_darkpool_observations(self, ticker: str) -> list[dict]:
        """Every real cataloged dark-pool-trade raw_json for `ticker`,
        same shape contract as get_cached_flow_observations above."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM darkpool_observations WHERE ticker = ? ORDER BY event_time ASC",
                (ticker,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def get_price_bars_cached_at(self, ticker: str, timeframe: str) -> Optional[str]:
        """Most recent REAL retrieved_at among this ticker/timeframe's
        cataloged price bars -- lets a cache-only response honestly
        disclose how stale its chart data is, instead of ever implying
        the read itself refreshed anything (see should_refetch's
        docstring for the sibling freshness-decision use of this same
        column; this is a plain disclosure read, not a refetch decision)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(retrieved_at) FROM price_bars WHERE ticker = ? AND timeframe = ?",
                (ticker, timeframe),
            ).fetchone()
        return row[0] if row and row[0] else None

    def catalog_discovery_snapshot(
        self, filters: dict, candidates: Iterable[dict], snapshot_at: str,
        kind: str = ON_DEMAND,
    ) -> Optional[int]:
        """One discovery_snapshots row (keyed by its own fingerprint over
        snapshot_at+filters, since a screener snapshot has no upstream id
        either) plus one discovery_candidates row per candidate. Returns
        the snapshot row id (new or the pre-existing one on a dedup hit),
        or None if the guard paused the write."""
        self._check_guard(kind)
        candidates = list(candidates or [])
        source_id = fingerprint("discovery_snapshot", snapshot_at, json.dumps(filters or {}, sort_keys=True, default=str))
        retrieved_at = utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO discovery_snapshots "
                "(source_id, event_time, retrieved_at, filters_json, candidate_count) VALUES (?, ?, ?, ?, ?)",
                (source_id, snapshot_at, retrieved_at, json.dumps(filters or {}, default=str), len(candidates)),
            )
            row = conn.execute(
                "SELECT id FROM discovery_snapshots WHERE source_id = ?", (source_id,)
            ).fetchone()
            snapshot_id = row[0] if row else None
            if snapshot_id is not None:
                for c in candidates:
                    ticker = c.get("ticker") or c.get("symbol") or ""
                    cand_source_id = fingerprint("discovery_candidate", source_id, ticker)
                    conn.execute(
                        "INSERT OR IGNORE INTO discovery_candidates "
                        "(source_id, snapshot_id, ticker, event_time, retrieved_at, raw_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (cand_source_id, snapshot_id, ticker, snapshot_at, retrieved_at,
                         json.dumps(c, default=str)),
                    )
            conn.commit()
        return snapshot_id

    def catalog_signal_evaluation(
        self, ticker: str, scan: dict, engine_rule_version: str = "v1",
        retrieved_at: Optional[str] = None, kind: str = ON_DEMAND,
        evidence_refs: Optional[list] = None,
    ) -> Optional[int]:
        """Persists ONE scan_service.evaluate_ticker_scan() result (the
        exact dict main.py already builds -- never re-derived) as a
        signal_evaluations row, a signal_transitions row when the state
        genuinely changed, and links to whatever raw observations were
        cited as evidence via signal_evaluation_evidence.
        `evidence_refs`: list of (observation_table, observation_id)
        tuples -- e.g. the ids catalog_options_flow_observations/
        catalog_darkpool_observations just inserted/matched for this same
        scan, so the evaluation is traceably linked to its real inputs.
        """
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        event_time = retrieved_at
        source_id = fingerprint("signal_evaluation", ticker, event_time, scan.get("state"), scan.get("confidence"))
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO signal_evaluations "
                "(source_id, ticker, event_time, retrieved_at, state, confidence, engine_rule_version, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, ticker, event_time, retrieved_at, scan.get("state"),
                 scan.get("confidence"), engine_rule_version, json.dumps(scan, default=str)),
            )
            row = conn.execute(
                "SELECT id FROM signal_evaluations WHERE source_id = ?", (source_id,)
            ).fetchone()
            evaluation_id = row[0] if row else None

            previous_state = scan.get("previous_state")
            new_state = scan.get("state")
            if evaluation_id is not None and previous_state != new_state:
                transition_source_id = fingerprint(
                    "signal_transition", ticker, event_time, previous_state, new_state
                )
                conn.execute(
                    "INSERT OR IGNORE INTO signal_transitions "
                    "(source_id, ticker, event_time, retrieved_at, from_state, to_state, transition_rule) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (transition_source_id, ticker, event_time, retrieved_at,
                     previous_state, new_state, scan.get("transition_rule")),
                )

            if evaluation_id is not None:
                for observation_table, observation_id in (evidence_refs or []):
                    if observation_id is None:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO signal_evaluation_evidence "
                        "(evaluation_id, observation_table, observation_id, created_at) VALUES (?, ?, ?, ?)",
                        (evaluation_id, observation_table, observation_id, retrieved_at),
                    )
            conn.commit()
        return evaluation_id

    def catalog_ai_investigation(
        self, ticker: str, result: dict, simulated: bool, transport: str,
        retrieved_at: Optional[str] = None, kind: str = ON_DEMAND,
    ) -> Optional[int]:
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        source_id = fingerprint("ai_investigation", ticker, retrieved_at)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ai_investigations "
                "(source_id, ticker, event_time, retrieved_at, simulated, transport, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_id, ticker, retrieved_at, retrieved_at, int(simulated), transport,
                 json.dumps(result, default=str)),
            )
            row = conn.execute(
                "SELECT id FROM ai_investigations WHERE source_id = ?", (source_id,)
            ).fetchone()
            conn.commit()
            return row[0] if row else None

    # --- source corrections (append-only, never UPDATE) -----------------

    def reconcile_correction(
        self, observation_table: str, new_observation_id: int, old_observation_id: int,
        reason: str = "", kind: str = ON_DEMAND,
    ) -> None:
        """Records that `new_observation_id` (a brand-new row this caller
        JUST inserted into `observation_table` via one of the catalog_*
        methods above) supersedes `old_observation_id` in that SAME
        table. Never touches either row's own columns -- append-only."""
        self._check_guard(kind)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO source_corrections "
                "(observation_table, observation_id, supersedes_observation_id, correction_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (observation_table, new_observation_id, old_observation_id, reason, utcnow_iso()),
            )
            conn.commit()

    # --- ingestion job checkpointing (resumable pagination jobs) --------

    def start_ingestion_job(self, job_type: str, detail: dict, kind: str = ON_DEMAND) -> str:
        """Creates one `ingestion_jobs` row with status='running' and
        returns its source_id (the job's own handle, used by
        update_ingestion_job / find_resumable_job below). `detail` is a
        free-form JSON blob the caller controls -- for dark-pool
        pagination this is where the checkpoint cursor lives (see
        DarkPoolPaginator)."""
        self._check_guard(kind)
        now = utcnow_iso()
        source_id = fingerprint("ingestion_job", job_type, now, json.dumps(detail, sort_keys=True, default=str))
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ingestion_jobs (source_id, job_type, event_time, retrieved_at, status, detail) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (source_id, job_type, now, now, json.dumps(detail, default=str)),
            )
            conn.commit()
        return source_id

    def update_ingestion_job(self, source_id: str, status: str, detail: dict) -> None:
        """Checkpoint update for a resumable job. This IS an UPDATE (not
        an append-only observation row) -- `ingestion_jobs` is job
        bookkeeping/progress state, not a raw-observation catalog table,
        so overwriting its own checkpoint in place is correct and
        intentional; it never touches any catalog table's rows."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET status = ?, detail = ?, retrieved_at = ? WHERE source_id = ?",
                (status, json.dumps(detail, default=str), utcnow_iso(), source_id),
            )
            conn.commit()

    def get_ingestion_job(self, source_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT source_id, job_type, status, detail FROM ingestion_jobs WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if not row:
                return None
            return {"source_id": row[0], "job_type": row[1], "status": row[2],
                    "detail": json.loads(row[3]) if row[3] else {}}

    def find_resumable_job(self, job_type: str, match: dict) -> Optional[dict]:
        """Finds the most recent `status='running'` job of this type whose
        detail dict contains every key/value in `match` (e.g.
        {"ticker": "SPY"}) -- used to resume an interrupted pagination
        run instead of starting over from scratch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source_id, job_type, status, detail FROM ingestion_jobs "
                "WHERE job_type = ? AND status = 'running' ORDER BY id DESC",
                (job_type,),
            ).fetchall()
        for row in rows:
            detail = json.loads(row[3]) if row[3] else {}
            if all(detail.get(k) == v for k, v in match.items()):
                return {"source_id": row[0], "job_type": row[1], "status": row[2], "detail": detail}
        return None

    # --- dark-pool coverage classification (three distinct states) ------

    def record_coverage_status(
        self, ticker: str, coverage_date: str, status: str, pages_walked: int,
        session_start_verified: bool, kind: str = ON_DEMAND,
    ) -> None:
        """Upserts the darkpool_coverage_status row for (ticker, date).
        This IS allowed to overwrite in place (unlike a raw-observation
        table): coverage status is this app's own derived bookkeeping
        about how much of a day we've paginated through, not an upstream
        source record, so there is nothing here that 'destroys history'
        by being updated -- re-running pagination for the same day is
        expected to refine (never silently downgrade undetected) this
        row. `status` must be one of 'complete'/'incomplete'/'unknown'."""
        if status not in ("complete", "incomplete", "unknown"):
            raise ValueError(f"invalid coverage status: {status!r}")
        self._check_guard(kind)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO darkpool_coverage_status "
                "(ticker, coverage_date, status, pages_walked, session_start_verified, retrieved_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker, coverage_date) DO UPDATE SET "
                "status=excluded.status, pages_walked=excluded.pages_walked, "
                "session_start_verified=excluded.session_start_verified, retrieved_at=excluded.retrieved_at",
                (ticker, coverage_date, status, pages_walked, int(session_start_verified), utcnow_iso()),
            )
            conn.commit()

    def get_coverage_status(self, ticker: str, coverage_date: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status, pages_walked, session_start_verified, retrieved_at "
                "FROM darkpool_coverage_status WHERE ticker = ? AND coverage_date = ?",
                (ticker, coverage_date),
            ).fetchone()
            if not row:
                return None
            return {"status": row[0], "pages_walked": row[1],
                    "session_start_verified": bool(row[2]), "retrieved_at": row[3]}

    # --- Phase C: read-only accessors for the baseline methodology module
    # (app/core/dark_pool_baseline.py). Both are pure reads of whatever
    # Phase B already cataloged -- neither triggers a fetch, a backfill,
    # or any live call of any kind.

    def list_coverage_status(self, ticker: str) -> list:
        """Every darkpool_coverage_status row cataloged for `ticker`,
        oldest first -- includes 'complete'/'incomplete'/'unknown' rows
        alike; filtering those down to the eligible ('complete'-only) set
        is deliberately NOT done here (see
        dark_pool_baseline.eligible_sessions, the one place that happens)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT coverage_date, status, pages_walked, session_start_verified, retrieved_at "
                "FROM darkpool_coverage_status WHERE ticker = ? ORDER BY coverage_date",
                (ticker,),
            ).fetchall()
        return [
            {
                "coverage_date": row[0], "status": row[1], "pages_walked": row[2],
                "session_start_verified": bool(row[3]), "retrieved_at": row[4],
            }
            for row in rows
        ]

    def list_darkpool_observations_by_date(self, ticker: str) -> dict:
        """Every cataloged darkpool_observations row for `ticker`, grouped
        by the calendar date (the first 10 chars of its ISO `event_time`)
        it falls on, each shaped as {"event_time": ..., "premium": float}
        -- the minimal shape dark_pool_baseline.cumulative_through_elapsed
        needs. `premium` is read out of the stored `raw_json` (the exact
        upstream UW field `catalog_darkpool_observations` stored verbatim,
        never re-derived)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_time, raw_json FROM darkpool_observations "
                "WHERE ticker = ? ORDER BY event_time",
                (ticker,),
            ).fetchall()
        grouped: dict = {}
        for event_time, raw_json in rows:
            date_key = (event_time or "")[:10]
            premium = 0.0
            if raw_json:
                try:
                    data = json.loads(raw_json)
                    premium = float(data.get("premium") or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    premium = 0.0
            grouped.setdefault(date_key, []).append({"event_time": event_time, "premium": premium})
        return grouped

    # --- Phase D: read-only accessor for freshness/dedup (market-wide
    # Radar). ONE query, ORDER BY retrieved_at DESC -- this is the whole
    # dedup mechanism between watchlist-sourced and discovery-sourced
    # evaluations of the same ticker: both paths write into this SAME
    # table via catalog_signal_evaluation above, so "most recent wins
    # regardless of source" falls straight out of this one query with no
    # separate source-tracking column needed. See
    # core/scheduler_freshness.py for the classification built on top of
    # this.

    def get_latest_signal_evaluation(self, ticker: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, ticker, event_time, retrieved_at, state, confidence, "
                "engine_rule_version, raw_json FROM signal_evaluations "
                "WHERE ticker = ? ORDER BY retrieved_at DESC, id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "ticker": row[1], "event_time": row[2], "retrieved_at": row[3],
            "state": row[4], "confidence": row[5], "engine_rule_version": row[6],
            "raw_json": json.loads(row[7]) if row[7] else None,
        }

    # --- freshness decision (used by backfill/pagination jobs ONLY --
    # never by the on-demand Radar/Ticker-Analysis/Discovery routes,
    # which must always reflect a fresh live fetch; see module docstring) --

    def most_recent_retrieved_at(
        self, table: str, ticker: str, params_fingerprint: Optional[str] = None,
    ) -> Optional[str]:
        """Most recent `retrieved_at` already cataloged for this
        ticker (optionally further scoped by a caller-computed
        `params_fingerprint`, e.g. a pagination job's window) -- or None
        if nothing is cataloged yet. Used only by `should_refetch` /
        backfill-style callers deciding whether to spend a NEW live
        request; the on-demand request/response path never consults
        this."""
        query = f"SELECT MAX(retrieved_at) FROM {table} WHERE ticker = ?"  # nosec - table is from a fixed internal allowlist, never user input
        if table not in _CATALOG_TABLES:
            raise ValueError(f"unknown catalog table: {table!r}")
        with self._conn() as conn:
            row = conn.execute(query, (ticker,)).fetchone()
            return row[0] if row and row[0] else None


_CATALOG_TABLES = {
    "options_flow_observations", "darkpool_observations", "price_bars",
    "discovery_snapshots", "discovery_candidates", "signal_evaluations",
    "signal_transitions", "ai_investigations",
}


def should_refetch(most_recent_retrieved_at: Optional[str], freshness_window_s: float) -> bool:
    """Pure decision helper for backfill/pagination-style callers (NOT the
    on-demand live routes, see module docstring): True when there is no
    cataloged observation yet, or the most recent one is older than
    `freshness_window_s`. Never raises on a malformed timestamp -- treats
    it as "no usable cache", i.e. refetch."""
    if not most_recent_retrieved_at:
        return True
    try:
        ts = datetime.fromisoformat(most_recent_retrieved_at)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age >= freshness_window_s
