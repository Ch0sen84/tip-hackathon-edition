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

import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls
from pathlib import Path
from typing import Optional

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
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def record_scan(self, ticker: str, any_gate_passed: bool, state: str,
                     scan_date: Optional[str] = None) -> None:
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
