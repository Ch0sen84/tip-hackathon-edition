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
import logging
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


_log = logging.getLogger(__name__)

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

    # --- episode-scoped lifecycle (signal-integrity remediation) ---------
    #
    # Lifecycle state now lives in lifecycle_episodes /
    # lifecycle_session_evidence / lifecycle_scans (migration 7, see
    # core/lifecycle.py for the model). The legacy ticker_scan_log /
    # ticker_current_state tables are no longer read or written: they hold
    # unscoped, server-calendar-dated history (and, before the Demo/Live
    # split, Demo rows) and are kept only as audit history.

    def _guard(self, kind: str) -> None:
        if self._storage_guard is not None:
            result = self._storage_guard.check_before_write(kind)
            if not result.allowed:
                raise StorageGuardPausedError(result)

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _latest_episode(self, conn, ticker: str):
        return conn.execute(
            "SELECT id, state, opened_session, reference_price, last_qualifying_session, "
            "closed_at, closed_session, close_reason FROM lifecycle_episodes "
            "WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()

    def context(self, ticker: str, session_date: str):
        """LifecycleContext for `ticker` as seen from trading session
        `session_date` (ISO)."""
        from .core.lifecycle import LifecycleContext
        import json as _json
        with self._conn() as conn:
            ep = self._latest_episode(conn, ticker)
            if ep is None:
                return LifecycleContext()
            ep_id, state, opened, ref, last_q, closed_at, closed_sess, reason = ep
            if closed_at is not None:
                return LifecycleContext(
                    last_closed_reason=reason, last_closed_session=closed_sess,
                    display_state="INVALIDATED" if reason == "invalidated" else None)
            rows = conn.execute(
                "SELECT session_date, qualifying, confirmed, fingerprints FROM lifecycle_session_evidence "
                "WHERE episode_id = ?", (ep_id,)).fetchall()
            prev = conn.execute(
                "SELECT closed_session, close_reason FROM lifecycle_episodes WHERE ticker = ? AND id < ? "
                "ORDER BY id DESC LIMIT 1", (ticker, ep_id)).fetchone()
        earlier = set()
        for sd, _q, _c, fps in rows:
            if sd != session_date:
                earlier.update(_json.loads(fps or "[]"))
        return LifecycleContext(
            episode_id=ep_id, state=state, opened_session=opened, reference_price=ref,
            qualifying_sessions=tuple(sorted(r[0] for r in rows if r[1])),
            confirmed_sessions=tuple(sorted(r[0] for r in rows if r[2])),
            last_qualifying_session=last_q,
            earlier_fingerprints=tuple(sorted(earlier)),
            last_closed_reason=prev[1] if prev else None,
            last_closed_session=prev[0] if prev else None,
        )

    def apply(self, ticker: str, decision, kind: str = ONDEMAND, scanned_at: Optional[str] = None) -> None:
        """Persist one lifecycle decision atomically."""
        import json as _json
        if decision.action == "hold" and not decision.write_scan_row:
            return
        self._guard(kind)
        now = scanned_at or self._now()
        sd = decision.session_date
        with self._conn() as conn:
            if self._persistent_conn is None:
                conn.execute("BEGIN IMMEDIATE")        # read-then-write under the database write lock
            ep = self._latest_episode(conn, ticker)
            open_id = ep[0] if ep is not None and ep[5] is None else None
            if decision.action == "open" and open_id is not None and not decision.expire_open_episode:
                # PE-P3 defence in depth: never a second open episode for a ticker -- the decision was
                # made from a stale context; leave the open episode as it is.
                _log.warning("lifecycle: refusing to open a second episode for %s (episode %s is open)",
                             ticker, open_id)
                conn.rollback()
                return
            if decision.expire_open_episode and open_id is not None:
                conn.execute(
                    "UPDATE lifecycle_episodes SET closed_at = ?, closed_session = ?, close_reason = 'expired', "
                    "updated_at = ? WHERE id = ?", (now, sd, now, open_id))
                open_id = None
            episode_id = open_id
            if decision.action == "open":
                cur = conn.execute(
                    "INSERT INTO lifecycle_episodes (ticker, opened_at, opened_session, state, reference_price, "
                    "last_qualifying_session, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ticker, now, sd, decision.state_after, decision.reference_price,
                     sd if decision.qualifying else None, now))
                episode_id = cur.lastrowid
            elif decision.action == "update" and episode_id is not None:
                conn.execute("UPDATE lifecycle_episodes SET state = ?, updated_at = ? WHERE id = ?",
                             (decision.state_after, now, episode_id))
                if decision.qualifying:
                    conn.execute("UPDATE lifecycle_episodes SET last_qualifying_session = ? WHERE id = ? "
                                 "AND (last_qualifying_session IS NULL OR last_qualifying_session < ?)",
                                 (sd, episode_id, sd))
            elif decision.action == "close_invalidated" and episode_id is not None:
                conn.execute(
                    "UPDATE lifecycle_episodes SET state = 'INVALIDATED', closed_at = ?, closed_session = ?, "
                    "close_reason = 'invalidated', updated_at = ? WHERE id = ?", (now, sd, now, episode_id))
            if episode_id is not None and decision.action in ("open", "update"):
                row = conn.execute(
                    "SELECT qualifying, confirmed, scans, fingerprints FROM lifecycle_session_evidence "
                    "WHERE episode_id = ? AND session_date = ?", (episode_id, sd)).fetchone()
                fps = set(_json.loads(row[3])) if row else set()
                if decision.fingerprint:
                    fps.add(decision.fingerprint)
                if row is None:
                    conn.execute(
                        "INSERT INTO lifecycle_session_evidence (episode_id, session_date, qualifying, confirmed, "
                        "scans, fingerprints, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                        (episode_id, sd, int(decision.qualifying), int(decision.confirmed),
                         _json.dumps(sorted(fps)), now, now))
                else:
                    conn.execute(
                        "UPDATE lifecycle_session_evidence SET qualifying = ?, confirmed = ?, scans = ?, "
                        "fingerprints = ?, last_seen_at = ? WHERE episode_id = ? AND session_date = ?",
                        (int(bool(row[0]) or decision.qualifying), int(bool(row[1]) or decision.confirmed),
                         row[2] + 1, _json.dumps(sorted(fps)), now, episode_id, sd))
            if decision.write_scan_row:
                conn.execute(
                    "INSERT INTO lifecycle_scans (ticker, episode_id, scanned_at, session_date, qualifying, "
                    "confirmed, state_before, state_after, rule, evidence_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ticker, episode_id, now, sd, int(decision.qualifying), int(decision.confirmed),
                     decision.state_before, decision.state_after, decision.rule, decision.fingerprint))
            conn.commit()

    def record_scan(self, ticker: str, any_gate_passed: bool, state: str,
                     scan_date: Optional[str] = None, kind: str = ONDEMAND) -> None:
        """Direct state write -- for seeding and tests only (the scan
        pipeline uses context()/apply()). Records `state` for `ticker` on
        trading session `scan_date` (default: the current evidence session)
        inside the episode model: opens an episode if none is open, closes
        it when `state` is INVALIDATED."""
        from .core.lifecycle import LifecycleDecision
        if state is None:
            return
        if scan_date is None:
            from datetime import datetime, timezone
            from .core.trading_calendar import evidence_session_date
            scan_date = evidence_session_date(datetime.now(timezone.utc)).isoformat()
        with self._conn() as conn:
            ep = self._latest_episode(conn, ticker)
        is_open = ep is not None and ep[5] is None
        before = ep[1] if ep is not None else None
        if not is_open:
            action = "open"
        elif state == "INVALIDATED":
            action = "close_invalidated"
        else:
            action = "update"
        decision = LifecycleDecision(
            state_before=before, state_after=state, rule="recorded_directly", action=action,
            session_date=scan_date, qualifying=bool(any_gate_passed),
            confirmed=bool(any_gate_passed) and state in ("CONFIRMED", "STABLE"))
        self.apply(ticker, decision, kind=kind)
        if action == "open" and state == "INVALIDATED":
            self.apply(ticker, LifecycleDecision(
                state_before=state, state_after=state, rule="recorded_directly", action="close_invalidated",
                session_date=scan_date, write_scan_row=False), kind=kind)

    def qualifying_days_count(self, ticker: str) -> int:
        """Distinct NYSE trading sessions with qualifying independent
        evidence in the CURRENT open episode (0 when none is open)."""
        with self._conn() as conn:
            ep = self._latest_episode(conn, ticker)
            if ep is None or ep[5] is not None:
                return 0
            row = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_session_evidence WHERE episode_id = ? AND qualifying = 1",
                (ep[0],)).fetchone()
        return row[0] or 0

    def current_state(self, ticker: str) -> Optional[str]:
        """State of the open episode; "INVALIDATED" when the latest episode
        was invalidated (until a new episode opens); None when there is no
        episode or the latest one expired."""
        with self._conn() as conn:
            ep = self._latest_episode(conn, ticker)
        if ep is None:
            return None
        if ep[5] is None:
            return ep[1]
        return "INVALIDATED" if ep[7] == "invalidated" else None

    def history(self, ticker: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT session_date, qualifying, state_after, scanned_at, rule, episode_id, state_before "
                "FROM lifecycle_scans WHERE ticker = ? ORDER BY id ASC",
                (ticker,),
            ).fetchall()
        return [
            {"scan_date": r[0], "any_gate_passed": bool(r[1]), "state": r[2] if r[2] is not None else r[6],
             "created_at": r[3], "transition_rule": r[4], "episode_id": r[5]}
            for r in rows
        ]

    def episodes(self, ticker: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, opened_at, opened_session, state, closed_at, closed_session, close_reason "
                "FROM lifecycle_episodes WHERE ticker = ? ORDER BY id", (ticker,)).fetchall()
        return [{"id": r[0], "opened_at": r[1], "opened_session": r[2], "state": r[3], "closed_at": r[4],
                 "closed_session": r[5], "close_reason": r[6]} for r in rows]
