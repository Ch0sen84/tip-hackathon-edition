"""User-initiated historical baseline builds (Phase 4).

A job walks one or more tickers back through prior NYSE sessions in BOUNDED
steps (`step_pages` UW requests per step, at most `max_pages_per_ticker` per
ticker), reusing the verified session-coverage collector
(session_collection.backfill_baseline via main._run_baseline_backfill).

Safety properties (each covered by tests/test_baseline_jobs.py):
- Never started automatically: only POST /api/baseline/jobs creates a job, and
  a job interrupted by a restart comes back PAUSED, never running.
- One active job at a time; it shares the explicit-backfill lock and the
  per-ticker collection locks with interactive scans, so two writers never
  extend the same ticker's coverage concurrently.
- Durable checkpoints: session coverage is persisted per window by the
  collector itself; the job row records per-ticker progress after every step,
  so a crash loses at most the step in flight, and resuming never re-fetches
  windows already verified complete.
- Pause / cancel take effect between steps (a step is bounded, so promptly).
- A provider failure pauses the job with the error recorded (no blind retry
  loop); a provider history cutoff marks that ticker finished.
- Status reads (GET) never touch the provider.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

_log = logging.getLogger(__name__)

ACTIVE = ("running", "pause_requested", "paused", "cancel_requested")
DEFAULT_MAX_PAGES_PER_TICKER = 400
DEFAULT_STEP_PAGES = 40
MAX_PAGES_PER_TICKER_CAP = 2000
STEP_PAGES_RANGE = (10, 200)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobConflict(Exception):
    pass


class BaselineJobStore:
    """`conn_factory` is the catalog's own connection context manager, so jobs
    live in the same database file (and the same in-memory DB under test)."""

    _create_lock = threading.Lock()

    def __init__(self, db_path: str, conn_factory, owner=None):
        self.db_path, self._conn, self.owner = db_path, conn_factory, owner

    @staticmethod
    def _row(r) -> Optional[dict]:
        if not r:
            return None
        return {"id": r[0], "created_at": r[1], "updated_at": r[2], "status": r[3], "tickers": json.loads(r[4]),
                "max_pages_per_ticker": r[5], "step_pages": r[6], "pages_used": r[7],
                "progress": json.loads(r[8]), "last_error": r[9], "target_sessions": r[10]}

    _COLS = ("id, created_at, updated_at, status, tickers_json, max_pages_per_ticker, step_pages, pages_used, "
             "progress_json, last_error, target_sessions")

    def create(self, tickers: list, max_pages_per_ticker: int, step_pages: int, requested_by: str,
               target_sessions: int = 20) -> dict:
        progress = {t: {"state": "pending", "pages_used": 0, "steps": 0,
                        "datasets": {}, "last_error": None} for t in tickers}
        with self._create_lock, self._conn() as conn:
            active = conn.execute(f"SELECT id FROM baseline_jobs WHERE status IN ({','.join('?' * len(ACTIVE))})",
                                  ACTIVE).fetchone()
            if active:
                raise JobConflict(f"baseline job {active[0]} is still active; pause/cancel it first")
            cur = conn.execute(
                "INSERT INTO baseline_jobs (created_at, updated_at, status, tickers_json, max_pages_per_ticker, "
                "step_pages, target_sessions, pages_used, progress_json, requested_by) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, 0, ?, ?)",
                (_now(), _now(), json.dumps(tickers), max_pages_per_ticker, step_pages, target_sessions,
                 json.dumps(progress), requested_by))
            conn.commit()
            return self.get(cur.lastrowid)

    def get(self, job_id: int) -> Optional[dict]:
        with self._conn() as conn:
            return self._row(conn.execute(f"SELECT {self._COLS} FROM baseline_jobs WHERE id = ?", (job_id,)).fetchone())

    def latest(self) -> Optional[dict]:
        with self._conn() as conn:
            return self._row(conn.execute(f"SELECT {self._COLS} FROM baseline_jobs ORDER BY id DESC LIMIT 1").fetchone())

    def update(self, job_id: int, **fields) -> None:
        cols, vals = [], []
        for k, v in fields.items():
            col = {"progress": "progress_json"}.get(k, k)
            cols.append(f"{col} = ?")
            vals.append(json.dumps(v) if k == "progress" else v)
        with self._conn() as conn:
            conn.execute(f"UPDATE baseline_jobs SET {', '.join(cols)}, updated_at = ? WHERE id = ?",
                         (*vals, _now(), job_id))
            conn.commit()

    def transition(self, job_id: int, allowed_from: tuple, to: str) -> Optional[dict]:
        """Atomic compare-and-set of the job status."""
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE baseline_jobs SET status = ?, updated_at = ? WHERE id = ? AND status IN "
                f"({','.join('?' * len(allowed_from))})", (to, _now(), job_id, *allowed_from))
            conn.commit()
            changed = cur.rowcount == 1
        return self.get(job_id) if changed else None

    def recover_after_restart(self) -> list:
        """A job that was running when the process stopped comes back PAUSED (never
        auto-resumed: no provider requests without a fresh user action)."""
        with self._conn() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM baseline_jobs WHERE status IN ('running', 'pause_requested')").fetchall()]
            for i in ids:
                conn.execute("UPDATE baseline_jobs SET status = 'paused', last_error = ?, updated_at = ? WHERE id = ?",
                             ("interrupted by an application restart; resume to continue", _now(), i))
            conn.execute("UPDATE baseline_jobs SET status = 'cancelled', updated_at = ? "
                         "WHERE status = 'cancel_requested'", (_now(),))
            conn.commit()
        return ids


class BaselineJobRunner:
    """Runs at most one job thread. `run_step(ticker, pages) -> dict` performs one
    bounded backfill step (main._run_baseline_backfill under the proper locks)
    and returns its per-dataset result."""

    def __init__(self, store: BaselineJobStore, run_step: Callable[[str, int], dict],
                 on_error: Optional[Callable[[Exception], None]] = None):
        self.store, self.run_step = store, run_step
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.on_error = on_error

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, job_id: int) -> bool:
        with self._lock:
            if self.is_running():
                return False
            self._thread = threading.Thread(target=self._run, args=(job_id,), name=f"baseline-job-{job_id}",
                                            daemon=True)
            self._thread.start()
            return True

    def join(self, timeout: Optional[float] = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    @staticmethod
    def _ticker_done(entry: dict, target: int) -> Optional[str]:
        ds = entry.get("datasets") or {}
        if ds and all((d.get("complete_prior_sessions") or 0) >= target for d in ds.values()):
            return "complete"
        if ds and all(d.get("history_cutoff") or (d.get("complete_prior_sessions") or 0) >= target
                      for d in ds.values()):
            return "history_cutoff"
        return None

    def _run(self, job_id: int) -> None:
        try:
            self._loop(job_id)
        except Exception as exc:  # noqa: BLE001 -- never leave a job stuck "running"
            _log.exception("baseline job %s crashed", job_id)
            self.store.update(job_id, status="paused", last_error=f"internal error: {exc}")
            if self.on_error:
                self.on_error(exc)

    def _loop(self, job_id: int) -> None:
        while True:
            job = self.store.get(job_id)
            if job is None:
                return
            if job["status"] == "pause_requested":
                self.store.transition(job_id, ("pause_requested",), "paused")
                return
            if job["status"] == "cancel_requested":
                self.store.transition(job_id, ("cancel_requested",), "cancelled")
                return
            if job["status"] != "running":
                return
            progress = job["progress"]
            ticker = next((t for t in job["tickers"] if progress[t]["state"] in ("pending", "running")), None)
            if ticker is None:
                self.store.transition(job_id, ("running",), "completed")
                return
            entry = progress[ticker]
            remaining = job["max_pages_per_ticker"] - entry["pages_used"]
            if remaining <= 0:
                entry["state"] = "budget_exhausted"
                self.store.update(job_id, progress=progress)
                continue
            entry["state"] = "running"
            pages = min(job["step_pages"], remaining)
            result = self.run_step(ticker, pages)
            used, failed, ds_state = 0, None, {}
            for ds, r in result.items():
                touched = r.get("sessions_touched") or []
                used += int(r.get("pages_used") or 0)
                cutoff = any(t.get("coverage") == "unavailable" for t in touched)
                err = next((t for t in touched if t.get("status") not in ("ok", None)
                            and t.get("coverage") != "unavailable"), None)
                if err and not failed:
                    failed = f"{ds} {err.get('session_date')}: {err.get('status')} ({err.get('reason') or 'no detail'})"
                prev = (entry["datasets"].get(ds) or {})
                ds_state[ds] = {"complete_prior_sessions": r.get("complete_prior_sessions"),
                                "history_cutoff": bool(cutoff or prev.get("history_cutoff")),
                                "last_pages_used": r.get("pages_used")}
            entry["datasets"] = ds_state
            entry["pages_used"] += used
            entry["steps"] += 1
            entry["last_error"] = failed
            job = self.store.get(job_id)            # pick up any pause/cancel requested during the step
            if failed:
                entry["state"] = "running"
                self.store.update(job_id, progress=progress, pages_used=job["pages_used"] + used,
                                  last_error=failed)
                self.store.transition(job_id, ("running", "pause_requested"), "paused")
                self.store.transition(job_id, ("cancel_requested",), "cancelled")
                return
            done = self._ticker_done(entry, job["target_sessions"])
            if done:
                entry["state"] = done
            elif used == 0:
                entry["state"] = "no_more_work"      # nothing fetchable inside the lookback window
            self.store.update(job_id, progress=progress, pages_used=job["pages_used"] + used, last_error=None)
