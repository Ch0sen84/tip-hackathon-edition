"""Phase D: durable scheduler lease + mode/state, backed by the SAME
SQLite file every other store in this app uses (app/migrations.py's
version-4 `scheduler_lease` / `scheduler_state` tables) -- NOT a second
database or a second storage mechanism.

LEASE (`scheduler_lease`, single row id=1): a real atomic mutual-
exclusion lock so a container restart, a crashed-and-restarted process,
or (if it ever happens) multiple worker processes cannot run
overlapping background collection cycles. Every state change is exactly
ONE UPDATE statement whose WHERE clause only matches for the rightful
owner or an expired lease -- the rowcount of that single statement IS
the atomicity: there is no read-then-decide-then-write gap for a second
caller to race into, because SQLite serializes writers against one
database file and this module never does more than one statement that
matters per decision.

STATE (`scheduler_state`, single row id=1): the durable mode
('disabled' | 'dry_run' | 'enabled') plus run bookkeeping. A fresh
install sees an empty table and `get_or_init_state()` seeds exactly one
row with mode='disabled' via INSERT OR IGNORE. An upgrade that already
has this row (from a previous run of this same app, e.g. the operator
turned on dry_run yesterday) must NEVER see that row reset -- INSERT OR
IGNORE is a no-op when the row already exists, so whatever mode is
already there survives untouched. This is the whole mechanism for
"upgrades preserve an existing explicit setting" -- not a special case,
just what INSERT OR IGNORE already does.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..migrations import run_migrations

MODES = ("disabled", "dry_run", "enabled")
DEFAULT_MODE = "disabled"

EPOCH_ISO = "1970-01-01T00:00:00+00:00"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_worker_id(prefix: str = "worker") -> str:
    import os

    return f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class InvalidModeError(ValueError):
    pass


class SchedulerStore:
    """Thin SQLite wrapper, modeled on app/ingestion.py's IngestionCatalog
    (same ':memory:' persistent-connection special case, same
    open/close-per-call pattern for a real file) -- deliberately NOT a
    new/competing store; `db_path` is the SAME file every other store in
    this app already uses."""

    def __init__(self, db_path: str = "./data/tip_hackathon.db"):
        self.db_path = db_path
        self._persistent_conn = (
            sqlite3.connect(db_path, check_same_thread=False) if db_path == ":memory:" else None
        )
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            run_migrations(conn, self.db_path)

    @contextmanager
    def _conn(self):
        if self._persistent_conn is not None:
            yield self._persistent_conn
            return
        conn = sqlite3.connect(self.db_path, timeout=30)   # P2-6
        try:
            yield conn
        finally:
            conn.close()

    # --- scheduler_state: mode + run bookkeeping -------------------------

    def _ensure_state_row(self, conn) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_state (id, mode, updated_at) VALUES (1, ?, ?)",
            (DEFAULT_MODE, utcnow_iso()),
        )

    def get_state(self) -> dict:
        with self._conn() as conn:
            self._ensure_state_row(conn)
            conn.commit()
            row = conn.execute(
                "SELECT mode, updated_at, updated_by, last_run_started_at, last_run_finished_at, "
                "next_run_at, last_cycle_candidates_evaluated, pause_reason "
                "FROM scheduler_state WHERE id = 1"
            ).fetchone()
        return {
            "mode": row[0], "updated_at": row[1], "updated_by": row[2],
            "last_run_started_at": row[3], "last_run_finished_at": row[4],
            "next_run_at": row[5], "last_cycle_candidates_evaluated": row[6],
            "pause_reason": row[7],
        }

    def get_mode(self) -> str:
        return self.get_state()["mode"]

    def set_mode(self, mode: str, actor: str = "unknown") -> dict:
        """Explicit, authenticated-caller-only mode change (main.py's
        route enforces require_auth; this layer just enforces the mode
        vocabulary). Never auto-called by the scheduler loop itself."""
        if mode not in MODES:
            raise InvalidModeError(f"mode must be one of {MODES}, got {mode!r}")
        with self._conn() as conn:
            self._ensure_state_row(conn)
            conn.execute(
                "UPDATE scheduler_state SET mode = ?, updated_at = ?, updated_by = ?, pause_reason = NULL "
                "WHERE id = 1",
                (mode, utcnow_iso(), actor),
            )
            conn.commit()
        return self.get_state()

    def record_run_started(self, started_at: Optional[str] = None) -> None:
        with self._conn() as conn:
            self._ensure_state_row(conn)
            conn.execute(
                "UPDATE scheduler_state SET last_run_started_at = ? WHERE id = 1",
                (started_at or utcnow_iso(),),
            )
            conn.commit()

    def record_run_finished(self, candidates_evaluated: int, next_run_at: Optional[str] = None,
                             pause_reason: Optional[str] = None, finished_at: Optional[str] = None) -> None:
        with self._conn() as conn:
            self._ensure_state_row(conn)
            conn.execute(
                "UPDATE scheduler_state SET last_run_finished_at = ?, next_run_at = ?, "
                "last_cycle_candidates_evaluated = ?, pause_reason = ? WHERE id = 1",
                (finished_at or utcnow_iso(), next_run_at, candidates_evaluated, pause_reason),
            )
            conn.commit()

    def record_pause(self, reason: str) -> None:
        with self._conn() as conn:
            self._ensure_state_row(conn)
            conn.execute("UPDATE scheduler_state SET pause_reason = ? WHERE id = 1", (reason,))
            conn.commit()

    # --- scheduler_lease: atomic cross-restart mutual exclusion ----------

    def _ensure_lease_row(self, conn) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_lease "
            "(id, worker_id, acquired_at, heartbeat_at, expires_at, lease_token) "
            "VALUES (1, '', ?, ?, ?, '')",
            (EPOCH_ISO, EPOCH_ISO, EPOCH_ISO),
        )

    def get_lease(self) -> dict:
        with self._conn() as conn:
            self._ensure_lease_row(conn)
            conn.commit()
            row = conn.execute(
                "SELECT worker_id, acquired_at, heartbeat_at, expires_at, lease_token "
                "FROM scheduler_lease WHERE id = 1"
            ).fetchone()
        return {
            "worker_id": row[0], "acquired_at": row[1], "heartbeat_at": row[2],
            "expires_at": row[3], "lease_token": row[4],
        }

    def try_acquire_lease(self, worker_id: str, ttl_s: float, now: Optional[datetime] = None) -> Optional[str]:
        """ONE atomic UPDATE: succeeds (rowcount==1) only when the
        current lease is already expired, OR this exact worker_id
        already holds it (idempotent re-acquire/renew-as-acquire).
        Returns a brand-new lease_token on success, None when another
        live worker holds the lease. Never a read-then-write race: the
        WHERE clause is evaluated by SQLite atomically against the
        single row being updated in this one statement."""
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_s)).isoformat()
        token = uuid.uuid4().hex
        with self._conn() as conn:
            self._ensure_lease_row(conn)
            conn.commit()
            cur = conn.execute(
                "UPDATE scheduler_lease SET worker_id = ?, acquired_at = ?, heartbeat_at = ?, "
                "expires_at = ?, lease_token = ? "
                "WHERE id = 1 AND (expires_at < ? OR worker_id = ?)",
                (worker_id, now_iso, now_iso, expires_iso, token, now_iso, worker_id),
            )
            conn.commit()
            if cur.rowcount == 1:
                return token
            return None

    def heartbeat_lease(self, worker_id: str, lease_token: str, ttl_s: float,
                         now: Optional[datetime] = None) -> bool:
        """ONE atomic UPDATE that only succeeds while `worker_id` +
        `lease_token` STILL match the row -- i.e. nobody else has taken
        over since this worker last touched it. False means this worker
        has LOST the lease (e.g. it stalled past expiry and a new owner
        already claimed it) and MUST NOT perform any further writes it
        no longer owns until it re-acquires a fresh lease."""
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_s)).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE scheduler_lease SET heartbeat_at = ?, expires_at = ? "
                "WHERE id = 1 AND worker_id = ? AND lease_token = ?",
                (now_iso, expires_iso, worker_id, lease_token),
            )
            conn.commit()
            return cur.rowcount == 1

    def release_lease(self, worker_id: str, lease_token: str, now: Optional[datetime] = None) -> bool:
        """Graceful shutdown: immediately expires the lease (sets
        expires_at to the epoch) so the NEXT acquirer doesn't have to
        wait out the TTL. Only succeeds for the actual current owner."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE scheduler_lease SET expires_at = ? "
                "WHERE id = 1 AND worker_id = ? AND lease_token = ?",
                (EPOCH_ISO, worker_id, lease_token),
            )
            conn.commit()
            return cur.rowcount == 1
