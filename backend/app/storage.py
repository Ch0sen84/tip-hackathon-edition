"""Local SQLite persistence for lifecycle state + qualifying-days counting.

DESIGN DECISION: production TIP's `qualifying_days_count()` (and the
state-transition history it depends on) is a Postgres query over
`signal_gate_results` collected continuously by cron jobs over weeks. This
hackathon app has no Postgres, no cron, and no multi-week history -- and the
task spec explicitly says not to port the Postgres-only persistence helpers.

Instead: every time a ticker is scanned (via either adapter), this module
appends one row (ticker, date, any_gate_passed, state) to a local SQLite
file created on first run. `qualifying_days_count(ticker)` is then just
`COUNT(DISTINCT date)` over that ticker's TRUE rows -- same semantics as
production (distinct calendar days with a qualifying gate pass), just fed by
however many scans have actually happened in *this* app's lifetime instead
of weeks of cron history. In demo/synthetic mode this still produces real
NEW -> BUILDING -> CONFIRMED -> STABLE transitions as you re-run scans or
feed multiple days of fixtures; it is just honestly a much shorter history
than production ever has.

The DB file path is configurable via TIP_DB_PATH (see app/config.py);
defaults to ./data/tip_hackathon.db, created automatically.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import date as date_cls
from pathlib import Path
from typing import Optional

from .migrations import run_migrations
from .storage_guard import ONDEMAND, StorageGuard, default_mount_point

REAL_DEFAULT_DB_PATH = "./data/tip_hackathon.db"


class StorageGuardPausedError(RuntimeError):
    """Raised by record_scan() when the Phase A storage-guard circuit
    breaker (app/storage_guard.py) finds the real volume below the
    on-demand free-space threshold. Callers (see main.py's _run_scan) are
    expected to turn this into a clear HTTP 507, never a generic 500 --
    this is an expected, structured "paused" condition, not a crash. No
    data is ever deleted by this guard; it only ever blocks ONE new write
    until free space recovers."""

    def __init__(self, result):
        super().__init__(result.reason)
        self.result = result


def _running_under_pytest() -> bool:
    """True whenever a pytest process is actually driving this code --
    never true for the real running app (uvicorn/Docker), so this guard
    can ONLY ever refuse something inside a test run, never in production."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def assert_safe_test_db_path(db_path: str) -> None:
    """SAFETY CRITICAL (#38, 2026-10-15 round 2) -- FAIL CLOSED.

    Backend pytest previously wrote synthetic test records straight into
    the DEPLOYED SQLite volume whenever it inherited a real TIP_DB_PATH
    from the shell/container it happened to run inside (instead of each
    test using its own isolated DB). That could pollute/corrupt a user's
    actual persistent TIP installation just by running the test suite.

    This function is the hard backstop: called from
    QualifyingDaysStore.__init__ on EVERY construction, it refuses (raises
    RuntimeError, does not open/touch the file) unless `db_path` is either
    literally ":memory:" or resolves to somewhere inside the OS temp
    directory (tempfile.gettempdir()) -- which is exactly where
    tempfile.mkdtemp() and pytest's own `tmp_path` fixture put their files.
    Anything else -- including the real default
    './data/tip_hackathon.db', any ambient TIP_DB_PATH inherited from a
    shell/container pointed at a real deployed install, or any other
    non-temp path a test might pass by mistake -- is refused loudly rather
    than silently opened and written to.

    Only active when pytest is actually running (_running_under_pytest());
    the real app (uvicorn/Docker) is never affected by this check and keeps
    using whatever db_path it's configured with, including the real
    default, exactly as before.
    """
    if not _running_under_pytest():
        return
    if db_path == ":memory:":
        return
    resolved = os.path.realpath(db_path)
    real_default = os.path.realpath(REAL_DEFAULT_DB_PATH)
    tmp_root = os.path.realpath(tempfile.gettempdir())
    is_in_tmp = resolved == tmp_root or resolved.startswith(tmp_root + os.sep)
    if resolved == real_default or not is_in_tmp:
        raise RuntimeError(
            f"REFUSING to open SQLite DB at {db_path!r} (resolved to {resolved!r}) "
            f"during a pytest run: this is not an isolated test path (expected "
            f"':memory:' or a path under {tmp_root!r} created by "
            f"tempfile.mkdtemp()/pytest's tmp_path fixture). This guard exists "
            f"because pytest previously wrote synthetic test records into the "
            f"real deployed TIP_DB_PATH volume -- see conftest.py and the #38 "
            f"regression tests in tests/test_db_isolation.py. Fix the test to use "
            f"an isolated temp DB path; never let it use an inherited TIP_DB_PATH "
            f"or the real default."
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    any_gate_passed INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scan_log_ticker ON ticker_scan_log(ticker);
CREATE TABLE IF NOT EXISTS ticker_current_state (
    ticker TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


class QualifyingDaysStore:
    """Thin SQLite wrapper. One instance per process is fine; SQLite handles
    the file locking. Safe to point at ':memory:' in tests."""

    def __init__(self, db_path: str = "./data/tip_hackathon.db"):
        assert_safe_test_db_path(db_path)
        self.db_path = db_path
        # sqlite3's ':memory:' creates a BRAND NEW empty database on every
        # connect() call -- a fresh connection per method call (as the file
        # case correctly does, for safe multi-process file locking) would
        # silently lose the schema/data after __init__'s own connection
        # closes. Keep one persistent connection alive for the lifetime of
        # this store when using ':memory:' (test/ephemeral use only); file
        # paths keep the original open-close-per-call pattern.
        # check_same_thread=False: FastAPI's TestClient (and sync routes in
        # general) run request handlers in a worker threadpool, so this one
        # persistent in-memory connection is legitimately used from more
        # than one thread across the store's lifetime. SQLite itself is
        # fine with that as long as access isn't concurrent, which it
        # isn't here (one request at a time through this store).
        self._persistent_conn = (
            sqlite3.connect(db_path, check_same_thread=False) if db_path == ":memory:" else None
        )
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Phase A storage guard: no real disk-usage concept for ':memory:'
        # (there's nothing on disk to run out of), so it's simply skipped
        # in that case -- never a fabricated check.
        self._storage_guard: Optional[StorageGuard] = (
            None if db_path == ":memory:" else StorageGuard(default_mount_point(db_path))
        )
        self._init_schema()

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

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
            # Phase A: versioned, additive migration runner -- safe to run
            # against a real pre-existing DB file (takes a timestamped
            # backup the first time it ever runs against one; never
            # touches ticker_scan_log/ticker_current_state). Runs on
            # every store construction, which is a safe no-op once
            # everything is already applied. See app/migrations.py.
            run_migrations(conn, self.db_path)

    def record_scan(self, ticker: str, any_gate_passed: bool, state: str,
                     scan_date: Optional[str] = None, kind: str = ONDEMAND) -> None:
        # Phase D: `kind` lets a background-sourced scan (the scheduler's
        # bounded per-ticker evaluation) check the SAME storage_guard
        # circuit breaker other background writes already use (the
        # earlier, more conservative BACKGROUND threshold -- see
        # app/storage_guard.py) instead of always checking the on-demand
        # threshold regardless of who's actually writing. Every existing
        # caller is unaffected: the default stays ONDEMAND, byte-for-byte
        # the same behavior as before this phase.
        if self._storage_guard is not None:
            result = self._storage_guard.check_before_write(kind)
            if not result.allowed:
                raise StorageGuardPausedError(result)
        scan_date = scan_date or date_cls.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO ticker_scan_log (ticker, scan_date, any_gate_passed, state) "
                "VALUES (?, ?, ?, ?)",
                (ticker, scan_date, int(any_gate_passed), state),
            )
            conn.execute(
                "INSERT INTO ticker_current_state (ticker, state, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(ticker) DO UPDATE SET state=excluded.state, updated_at=datetime('now')",
                (ticker, state),
            )
            conn.commit()

    def qualifying_days_count(self, ticker: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT scan_date) FROM ticker_scan_log "
                "WHERE ticker = ? AND any_gate_passed = 1",
                (ticker,),
            ).fetchone()
            return row[0] or 0

    def current_state(self, ticker: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state FROM ticker_current_state WHERE ticker = ?", (ticker,)
            ).fetchone()
            return row[0] if row else None

    def history(self, ticker: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT scan_date, any_gate_passed, state, created_at FROM ticker_scan_log "
                "WHERE ticker = ? ORDER BY id ASC",
                (ticker,),
            ).fetchall()
            return [
                {"scan_date": r[0], "any_gate_passed": bool(r[1]), "state": r[2], "created_at": r[3]}
                for r in rows
            ]
