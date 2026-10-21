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
- Hard page budget: a step's pages are charged to the persisted job BEFORE its
  first provider request and settled to the pages actually used afterwards, so
  an exception or restart mid-step can never hand that budget back to a resume.
- Pause / cancel are cooperative: honoured between steps. A step is bounded in logical
  pages, not in time or HTTP attempts (retries, timeouts and Retry-After waits apply
  inside the collector).
- A provider failure pauses the job with the error recorded (no blind retry
  loop); a provider history cutoff marks that ticker finished.
- Per-dataset target (Phase 9): once a dataset has `target_sessions` complete
  prior sessions (or hit a history cutoff), later steps skip it and spend the
  step on the datasets still short of the target. This changes only which
  pages this job requests -- never which sessions count, the lookback, the
  20-session requirement or any signal rule. Current-session evidence is
  collected by scans/refreshes, not by jobs, so skipping a dataset here never
  stops an explicitly requested evaluation from collecting fresh evidence.
- Status reads (GET) never touch the provider.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

_log = logging.getLogger(__name__)

ACTIVE = ("running", "pause_requested", "paused", "cancel_requested")
DEFAULT_MAX_PAGES_PER_TICKER = 400
# Pause/Cancel are honoured between steps, so the step size bounds how many more
# LOGICAL pages can follow a click (10 = 5 per dataset). It does not bound HTTP
# attempts or time: each page may be retried, and timeouts / Retry-After waits apply.
# Coverage is checkpointed per window, so small steps re-fetch nothing.
DEFAULT_STEP_PAGES = 10
MAX_PAGES_PER_TICKER_CAP = 2000
STEP_PAGES_RANGE = (10, 200)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobConflict(Exception):
    pass


class JobBlocked(Exception):
    """A step cannot run right now for a reason the user must resolve (e.g. the app
    left Live Mode). The job is paused with this message; nothing is retried."""


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

    def start(self, job_id: int, wait_s: float = 5.0) -> bool:
        """Starts the job thread. A previous thread that is only finishing (its final status is already
        written) is waited for up to `wait_s` instead of being treated as busy (J1/J2)."""
        with self._lock:
            if self.is_running():
                self._thread.join(wait_s)
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
    def _dataset_met(d: dict, target: int) -> bool:
        return bool(d.get("history_cutoff")) or (d.get("complete_prior_sessions") or 0) >= target

    @classmethod
    def _datasets_needed(cls, entry: dict, target: int) -> Optional[list]:
        """None before the first step (every dataset runs); afterwards the
        datasets still below the target that still had fetchable work (a
        dataset given pages that used none has nothing left in the lookback)."""
        ds = entry.get("datasets") or {}
        if not ds:
            return None
        return [name for name, d in ds.items() if not cls._dataset_met(d, target) and not d.get("no_more_work")]

    @staticmethod
    def _ticker_done(entry: dict, target: int) -> Optional[str]:
        ds = entry.get("datasets") or {}
        if ds and all((d.get("complete_prior_sessions") or 0) >= target for d in ds.values()):
            return "complete"
        if ds and all(d.get("history_cutoff") or (d.get("complete_prior_sessions") or 0) >= target
                      for d in ds.values()):
            return "history_cutoff"
        return None

    def _settle_after_failure(self, job_id: int, last_error: str) -> None:
        """A failed step never overrides the user's Cancel (J3), and a database error while recording the
        failure is retried rather than leaving the job 'running' with no thread (J4; the watchdog in
        reconcile_orphans covers anything left)."""
        for attempt in range(3):
            try:
                if self.store.transition(job_id, ("cancel_requested",), "cancelled") is None:
                    self.store.transition(job_id, ("running", "pause_requested"), "paused")
                self.store.update(job_id, last_error=last_error)
                return
            except Exception:  # noqa: BLE001
                _log.exception("baseline job %s: could not record the failure (attempt %s)", job_id, attempt + 1)
                time.sleep(0.5 * (attempt + 1))

    def reconcile_orphans(self) -> None:
        """Watchdog: a job recorded as active while no job thread is alive (a crash between status writes,
        a failed start) is put back to a resumable or final state, never left 'running' forever."""
        if self.is_running():
            return
        job = self.store.latest()
        if not job:
            return
        if job["status"] == "cancel_requested":
            self.store.transition(job["id"], ("cancel_requested",), "cancelled")
        elif job["status"] in ("running", "pause_requested"):
            if self.store.transition(job["id"], ("running", "pause_requested"), "paused") is not None:
                self.store.update(job["id"], last_error="paused: the job worker was not running -- resume to continue")

    def _run(self, job_id: int) -> None:
        try:
            self._loop(job_id)
        except JobBlocked as exc:
            self._settle_after_failure(job_id, str(exc))
        except Exception as exc:  # noqa: BLE001 -- never leave a job stuck "running"
            _log.exception("baseline job %s crashed", job_id)
            self._settle_after_failure(job_id, f"internal error: {exc}")
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
            counters = (job.get("pages_used"), entry.get("pages_used"), job.get("max_pages_per_ticker"))
            if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counters):
                raise JobBlocked("paused: inconsistent persisted job progress (page counters); "
                                 "no request was made -- start a new job")
            remaining = job["max_pages_per_ticker"] - entry["pages_used"]
            if remaining <= 0:
                entry["state"] = "budget_exhausted"
                self.store.update(job_id, progress=progress)
                continue
            entry["state"] = "running"
            self.store.update(job_id, progress=progress)     # visible as "in progress" during the step
            pages = min(job["step_pages"], remaining)
            needed = self._datasets_needed(entry, job["target_sessions"])
            if needed == []:                         # nothing left to request for this ticker
                entry["state"] = self._ticker_done(entry, job["target_sessions"]) or "no_more_work"
                self.store.update(job_id, progress=progress)
                continue
            # Charge the step BEFORE any provider request: if the step raises or the process
            # stops mid-step, the pages stay counted (a resume can under-spend, never overspend).
            entry["pages_used"] += pages
            entry["reserved_pages"] = pages
            self.store.update(job_id, progress=progress, pages_used=job["pages_used"] + pages)
            try:
                if needed is None or len(needed) == len(entry["datasets"]):
                    result = self.run_step(ticker, pages)
                else:
                    result = self.run_step(ticker, pages, datasets=needed)
            except JobBlocked:                        # raised before any provider request: release the reservation
                entry["pages_used"] -= pages
                entry.pop("reserved_pages", None)
                self.store.update(job_id, progress=progress, pages_used=job["pages_used"])
                raise
            except Exception as exc:  # noqa: BLE001 -- record the HTTP attempts it made; the pages stay reserved
                entry["http_attempts"] = (entry.get("http_attempts") or 0) + int(getattr(exc, "tip_http_attempts", 0) or 0)
                self.store.update(job_id, progress=progress)
                raise
            used, failed, attempts, starved = 0, None, 0, False
            ds_state = {name: {**d, "last_pages_used": 0, "last_pages_allocated": 0}
                        for name, d in (entry.get("datasets") or {}).items() if name not in result}
            for ds, r in result.items():
                touched = r.get("sessions_touched") or []
                used += int(r.get("pages_used") or 0)
                cutoff = bool(r.get("history_cutoff")) or any(t.get("coverage") == "unavailable" for t in touched)
                err = next((t for t in touched if t.get("status") not in ("ok", None)
                            and t.get("coverage") != "unavailable"), None)
                if err and not failed:
                    failed = f"{ds} {err.get('session_date')}: {err.get('status')} ({err.get('reason') or 'no detail'})"
                prev = (entry["datasets"].get(ds) or {})
                allocated = r.get("pages_allocated")
                ds_used = int(r.get("pages_used") or 0)
                attempts += int(r.get("http_attempts") or 0)
                starved = starved or allocated == 0
                ds_state[ds] = {"complete_prior_sessions": r.get("complete_prior_sessions"),
                                "history_cutoff": bool(cutoff or prev.get("history_cutoff")),
                                "last_pages_used": r.get("pages_used"),
                                "last_pages_allocated": allocated,
                                "no_more_work": bool(prev.get("no_more_work")) or (bool(allocated) and ds_used == 0),
                                "pages_used": (prev.get("pages_used") or 0) + ds_used,
                                "http_attempts": (prev.get("http_attempts") or 0) + int(r.get("http_attempts") or 0),
                                "bounds_retries": (prev.get("bounds_retries") or 0)
                                + sum(t.get("bounds_retries") or 0 for t in touched),
                                "bounds_recovered": (prev.get("bounds_recovered") or 0)
                                + sum(t.get("bounds_recovered") or 0 for t in touched)}
                ds_state[ds]["target_met"] = self._dataset_met(ds_state[ds], job["target_sessions"])
            entry["datasets"] = ds_state
            entry["pages_used"] += used - pages               # settle the reservation to the pages actually used
            entry.pop("reserved_pages", None)
            entry["http_attempts"] = (entry.get("http_attempts") or 0) + attempts     # HTTP layer, incl. retries
            entry["steps"] += 1
            entry["last_error"] = failed
            job = self.store.get(job_id)            # pick up any pause/cancel requested during the step
            if failed:
                entry["state"] = "running"
                self.store.update(job_id, progress=progress, pages_used=job["pages_used"] - pages + used,
                                  last_error=failed)
                self.store.transition(job_id, ("running", "pause_requested"), "paused")
                self.store.transition(job_id, ("cancel_requested",), "cancelled")
                return
            done = self._ticker_done(entry, job["target_sessions"])
            if done:
                entry["state"] = done
            elif used == 0 and not starved:
                entry["state"] = "no_more_work"      # nothing fetchable inside the lookback window
            # used == 0 with a dataset allocated 0 pages (the last page went to a dataset that had
            # nothing to do): that dataset is now marked no_more_work, so the next step gives the
            # remaining page to the one that was starved.
            self.store.update(job_id, progress=progress, pages_used=job["pages_used"] - pages + used,
                              last_error=None)
