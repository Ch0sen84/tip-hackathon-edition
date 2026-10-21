"""Phase D: market-wide background discovery + bounded evaluation
scheduler.

STRUCTURAL CONSTRAINT (Chad's item 7, enforced by
backend/tests/test_scheduler_no_ai_import.py): this module, and
everything it imports, must have ZERO path to app.ai.* (the AI
orchestrator / anything that calls /api/investigate/*). The scheduler
NEVER triggers a paid Anthropic call, full stop -- not "it currently
doesn't", structurally cannot, because this module simply does not
import that code.

REUSE, NOT A SECOND PIPELINE (Chad's item 6): every piece of real work
this module does is a call into code that already existed before this
phase:
  * Discovery/ranking   -> core.discovery.run_discovery (the SAME
                            function app.main's POST /api/radar/discover
                            route calls -- moved there, not duplicated).
  * Snapshot persistence -> IngestionCatalog.catalog_discovery_snapshot
                            (Phase B, unchanged).
  * Per-ticker evaluation -> an injected `run_ticker_scan` callable that
                            IS app.main._run_scan wrapped for background
                            use (same scan_service.evaluate_ticker_scan,
                            same storage_guard, same catalog writes) --
                            see app/main.py's `_scheduler_run_ticker_scan`
                            for the thin wrapper. This module never
                            builds its own evaluation logic.

DEPENDENCY INJECTION, NOT A CIRCULAR IMPORT: app.main constructs the
one SchedulerRunner instance for the process, passing it get_adapter /
get_catalog / run_ticker_scan callables that close over main.py's own
module-level state. This file never imports app.main -- the scheduler
has no idea main.py exists; it only knows about the callables it was
handed. That is what makes "the scheduler calls the real pipeline"
compatible with "the scheduler module has zero AI import": app.main
(which DOES import the AI orchestrator, for /api/investigate/*) is
simply never imported here, in either direction.

MODES (see core/scheduler_store.py for the durable storage):
  disabled -- `_maybe_run_cycle` returns immediately. Zero background
              UW calls, period.
  dry_run  -- ONE discovery call per cycle (via core.discovery), its
              result written via catalog_discovery_snapshot. Explicitly
              NO per-ticker evaluation, NO UW calls beyond that single
              screener call.
  enabled  -- dry_run's discovery step, THEN up to `top_n` of the
              ranked candidates are run through `run_ticker_scan` (the
              real per-ticker pipeline) -- bounded, never the whole
              candidate list.

MARKET-HOURS AWARENESS: a cycle is only attempted when
core.trading_calendar says "yes, a real NYSE session is open right
now" (reusing Phase C's calendar -- never a second one).

PRIORITY: every UW call this module makes is wrapped in
core.uw_concurrency.background_priority() so it never starves a
concurrent interactive request for the shared concurrency budget.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timezone
from typing import Callable, Optional

from .adapters.synthetic_adapter import SyntheticDataAdapter
from .core import discovery as discovery_module
from .core import uw_concurrency
from .core.scheduler_store import SchedulerStore, new_worker_id
from .core.trading_calendar import session_info
from .ingestion import StorageGuardPausedError as IngestionStorageGuardPausedError
from .storage import StorageGuardPausedError

logger = logging.getLogger(__name__)

# UX item (final round, 2026-10-16): execution-depth values for a
# one-time run that is fully DECOUPLED from the persisted recurring
# `mode`. See SchedulerRunner.run_once_with_depth's docstring -- these
# are NOT a third/fourth scheduler "mode"; they only describe how deep
# ONE manually-triggered cycle goes, and are never persisted anywhere.
DEPTH_DISCOVERY_ONLY = "discovery_only"
DEPTH_DISCOVERY_PLUS_EVALUATION = "discovery_plus_evaluation"
RUN_ONCE_DEPTHS = (DEPTH_DISCOVERY_ONLY, DEPTH_DISCOVERY_PLUS_EVALUATION)

DEFAULT_TOP_N = 5
# Conservative default: a per-ticker evaluation is itself several UW
# calls (flow-alerts, darkpool, stock-state), and this budget is SHARED
# with interactive traffic via the same concurrency gate -- 5 keeps one
# scheduler cycle's total UW call count bounded and small relative to
# the 2-request concurrency ceiling, rather than trying to evaluate
# every screener row returned (up to 50; see DISCOVERY_LIMIT_CAP).
DEFAULT_SCAN_INTERVAL_S = 15 * 60  # 15 minutes -- market-hours-aware; see module docstring
DEFAULT_LEASE_TTL_S = 90
DEFAULT_POLL_INTERVAL_S = 20


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class SchedulerRunner:
    def __init__(
        self,
        db_path: str,
        get_adapter: Callable[[], object],
        get_catalog: Callable[[], object],
        run_ticker_scan: Callable[[object, str], dict],
        worker_id: Optional[str] = None,
        run_baseline_backfill: Optional[Callable[[object], dict]] = None,
        top_n: Optional[int] = None,
        scan_interval_s: Optional[int] = None,
        lease_ttl_s: Optional[int] = None,
        poll_interval_s: Optional[int] = None,
        now_fn=None,
    ):
        self.store = SchedulerStore(db_path)
        self.get_adapter = get_adapter
        self.get_catalog = get_catalog
        self.run_ticker_scan = run_ticker_scan
        # Signal-integrity remediation (audit B1): the bounded, background-
        # priority session-coverage backfill that makes the real baseline
        # gates reachable in normal Live operation. Runs only on cycles that
        # evaluate (scheduler mode "enabled" / depth plus-evaluation).
        self.run_baseline_backfill = run_baseline_backfill
        self.worker_id = worker_id or new_worker_id("scheduler")
        self.top_n = top_n if top_n is not None else _env_int("TIP_SCHEDULER_TOP_N", DEFAULT_TOP_N)
        self.scan_interval_s = (
            scan_interval_s if scan_interval_s is not None
            else _env_int("TIP_SCHEDULER_INTERVAL_S", DEFAULT_SCAN_INTERVAL_S)
        )
        self.lease_ttl_s = (
            lease_ttl_s if lease_ttl_s is not None else _env_int("TIP_SCHEDULER_LEASE_TTL_S", DEFAULT_LEASE_TTL_S)
        )
        self.poll_interval_s = (
            poll_interval_s if poll_interval_s is not None
            else _env_int("TIP_SCHEDULER_POLL_S", DEFAULT_POLL_INTERVAL_S)
        )
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lease_token: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # QA remediation D9: an in-process, non-blocking mutual-exclusion
        # lock around the ACT of running a cycle -- separate from (and in
        # addition to) the cross-process SQLite lease above. The lease
        # alone does not stop two concurrent calls FROM THE SAME
        # worker_id (e.g. the background loop thread and a manual
        # run-once HTTP request both using this one SchedulerRunner
        # instance) from both successfully acquiring/renewing it, since
        # try_acquire_lease is intentionally idempotent for its own
        # worker_id. This lock is what actually guarantees "a second
        # run-once call while one is already in flight is rejected, not
        # queued" -- a non-blocking acquire() either gets it immediately
        # or returns False, there is no waiting/queueing behavior at all.
        self._cycle_lock = threading.Lock()

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tip-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Graceful shutdown: releases the lease immediately (so the next
        worker -- e.g. after a redeploy -- doesn't have to wait out the
        TTL) rather than just letting it expire."""
        self._stop.set()
        if self._lease_token:
            try:
                self.store.release_lease(self.worker_id, self._lease_token)
            except Exception:  # noqa: BLE001 -- shutdown must never hang/crash on this
                logger.exception("scheduler: failed to release lease on shutdown")
            self._lease_token = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        # S4: a persisted dry_run/enabled mode never makes a provider request at process start: the first
        # recurring cycle is at least one full interval after boot (a restart is not a user action).
        boot_quiet_until = self._now_fn().timestamp() + self.scan_interval_s
        while not self._stop.is_set():
            try:
                if self._now_fn().timestamp() >= boot_quiet_until:
                    self.run_one_cycle_if_due()
            except Exception:  # noqa: BLE001 -- the loop must never die from one bad cycle
                logger.exception("scheduler: cycle failed")
            self._stop.wait(self.poll_interval_s)

    def _abort_reason(self, recurring: bool, started_mode: Optional[str]) -> Optional[str]:
        """Checked before every unit of provider work in a cycle (S1/S2)."""
        if self._stop.is_set():
            return "scheduler stopping"
        if recurring:
            mode = self.store.get_mode()
            if mode != started_mode:
                return f"mode changed to {mode!r} during the cycle"
        if not self._ensure_lease(self._now_fn()):
            return "lease lost during the cycle"
        return None

    # --- the actual decision + one cycle ---------------------------------

    def _is_market_open(self, now: datetime) -> bool:
        info = session_info(now.date())
        if not info.is_trading_day or info.session_open_et is None or info.session_close_et is None:
            return False
        open_utc = info.session_open_et.astimezone(timezone.utc)
        close_utc = info.session_close_et.astimezone(timezone.utc)
        return open_utc <= now <= close_utc

    def _due(self, state: dict, now: datetime) -> bool:
        next_run_at = state.get("next_run_at")
        if not next_run_at:
            return True
        try:
            next_run = datetime.fromisoformat(next_run_at)
        except ValueError:
            return True
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        return now >= next_run

    def run_one_cycle_if_due(self, now: Optional[datetime] = None) -> Optional[dict]:
        """Public, directly test-callable entry point for exactly one
        decision + (maybe) one cycle. Returns a dict describing what
        happened (useful for tests + diagnostics), or None when nothing
        ran this call (disabled / market closed / not due / lease not
        held)."""
        now = now or self._now_fn()
        state = self.store.get_state()
        mode = state["mode"]
        if mode == "disabled":
            return {"ran": False, "reason": "disabled"}
        if not self._is_market_open(now):
            return {"ran": False, "reason": "market_closed"}
        if not self._due(state, now):
            return {"ran": False, "reason": "not_due"}
        acquired = self._cycle_lock.acquire(blocking=False)
        if not acquired:
            return {"ran": False, "reason": "cycle_already_in_progress"}
        try:
            if not self._ensure_lease(now):
                return {"ran": False, "reason": "lease_not_held"}
            return self._run_cycle(evaluate=(mode == "enabled"), now=now, persist_next_run=True, reported_mode=mode)
        finally:
            self._cycle_lock.release()

    def run_once(self, now: Optional[datetime] = None) -> dict:
        """QA remediation D9: an authenticated, bounded, SUPERVISED
        single-cycle validation action -- see app/main.py's
        POST /api/scheduler/run-once. Deliberately bypasses the
        due-interval check (an explicit one-off user action should not
        be refused just because the normal schedule says "not yet"), but
        NEVER bypasses:
          * mode == "disabled" -> refuses, same as the automatic loop.
          * the SAME in-process `_cycle_lock` the automatic loop itself
            uses -- a manual run-once while either the background loop
            OR another run-once call is actively mid-cycle is REJECTED
            outright (`cycle_already_in_progress`), never queued.
          * the SAME cross-process SQLite `scheduler_lease` -- a
            run-once call can never proceed while a genuinely different
            worker/process holds the lease.
        Runs EXACTLY one cycle (discovery only in dry_run; discovery +
        bounded top_n evaluation in enabled -- the existing mode
        distinction, completely unchanged) and then returns to idle --
        there is no new recurring timer started by this call, and
        calling it once never schedules or implies a second call.
        """
        now = now or self._now_fn()
        state = self.store.get_state()
        mode = state["mode"]
        if mode == "disabled":
            return {"ran": False, "reason": "disabled", "triggered_by": "run_once"}
        acquired = self._cycle_lock.acquire(blocking=False)
        if not acquired:
            return {"ran": False, "reason": "cycle_already_in_progress", "triggered_by": "run_once"}
        try:
            if not self._ensure_lease(now):
                return {"ran": False, "reason": "lease_not_held", "triggered_by": "run_once"}
            # FE-F3: a manual run is never treated as a recurring cycle (no historical backfill, no
            # mode-change abort) even though it keeps the legacy next_run_at bookkeeping.
            outcome = self._run_cycle(evaluate=(mode == "enabled"), now=now, persist_next_run=True, reported_mode=mode,
                                      manual=True)
            outcome["triggered_by"] = "run_once"
            return outcome
        finally:
            self._cycle_lock.release()

    def run_once_with_depth(self, depth: str, now: Optional[datetime] = None) -> dict:
        """Final-round item: a one-time run that is FULLY DECOUPLED from
        the persisted recurring `mode` -- this is the actual fix for
        Chad's 'Run Once currently requires arming Dry Run/Enabled
        first' complaint. Unlike the legacy run_once() above (kept
        byte-for-byte unchanged for backward compat / its own existing
        tests), this method:
          * NEVER refuses because mode == 'disabled' -- that is exactly
            the gate this round removes for the one-time path. The
            persisted mode is read ONLY for the `mode` field in the
            returned dict (pure reporting), never for a gating decision.
          * NEVER persists `mode` -- `store.set_mode()` is not called
            anywhere in this method, so the recurring scheduler mode is
            byte-for-byte unchanged before/during/after this call.
          * NEVER persists a future `next_run_at` -- passes
            persist_next_run=False to `_run_cycle`, so this one-time
            run leaves no side effect that could make the (disabled)
            recurring loop think a schedule is armed.
          * Still goes through the EXACT SAME in-process `_cycle_lock`
            and cross-process SQLite `scheduler_lease` as every other
            entry point (`run_once`, `run_one_cycle_if_due`) -- a
            second concurrent one-time call, or a genuinely different
            worker/process (recurring or manual) already mid-cycle,
            is rejected the same way.
          * Applies NO market-hours/RTH gate (same as the legacy
            run_once -- this method never calls `_is_market_open`),
            satisfying the explicit requirement that a one-time run be
            triggerable outside regular market hours on request.
        `depth` selects discovery-only vs discovery+bounded-top_n-
        evaluation; see DEPTH_DISCOVERY_ONLY / DEPTH_DISCOVERY_PLUS_EVALUATION.
        """
        if depth not in RUN_ONCE_DEPTHS:
            raise ValueError(f"depth must be one of {RUN_ONCE_DEPTHS}, got {depth!r}")
        now = now or self._now_fn()
        acquired = self._cycle_lock.acquire(blocking=False)
        if not acquired:
            return {"ran": False, "reason": "cycle_already_in_progress", "triggered_by": "run_once", "depth": depth}
        try:
            if not self._ensure_lease(now):
                return {"ran": False, "reason": "lease_not_held", "triggered_by": "run_once", "depth": depth}
            persisted_mode = self.store.get_mode()  # read-only; reported, never used to gate or re-written
            evaluate = depth == DEPTH_DISCOVERY_PLUS_EVALUATION
            outcome = self._run_cycle(
                evaluate=evaluate, now=now, persist_next_run=False, reported_mode=persisted_mode,
            )
            outcome["triggered_by"] = "run_once"
            outcome["depth"] = depth
            return outcome
        finally:
            self._cycle_lock.release()

    def _ensure_lease(self, now: datetime) -> bool:
        if self._lease_token:
            ok = self.store.heartbeat_lease(self.worker_id, self._lease_token, self.lease_ttl_s, now=now)
            if ok:
                return True
            # Lost the lease (e.g. a long pause let it expire and another
            # worker already claimed it) -- MUST NOT perform any further
            # writes under the assumption we still own it. Drop the
            # stale token and try a clean re-acquire below.
            logger.warning("scheduler %s: lost its lease, will attempt to re-acquire", self.worker_id)
            self._lease_token = None
        token = self.store.try_acquire_lease(self.worker_id, self.lease_ttl_s, now=now)
        if token:
            self._lease_token = token
            return True
        return False

    def _run_cycle(self, evaluate: bool, now: datetime, persist_next_run: bool = True,
                   reported_mode: Optional[str] = None, manual: bool = False) -> dict:
        """The one real cycle body, shared by every trigger path
        (automatic loop, legacy run_once, and the new decoupled
        run_once_with_depth). `evaluate` replaces the old `mode ==
        'enabled'` check so a one-time call can request evaluation
        without that ever being read from (or confused with) the
        persisted recurring `mode`. `persist_next_run=False` (used only
        by run_once_with_depth) skips writing a future `next_run_at` --
        a one-time run must leave no side effect that could make the
        (still-disabled) recurring loop believe a schedule is armed.
        `reported_mode` is pure disclosure for the returned dict's
        `mode` field; it is never used for any gating decision here."""
        self.store.record_run_started(started_at=now.isoformat())
        adapter = self.get_adapter()
        catalog = self.get_catalog()
        candidates_evaluated = 0
        pause_reason = None
        backfill_result = None
        recurring = (not manual) and persist_next_run and reported_mode in ("dry_run", "enabled")
        lease_lost = False
        try:
            with uw_concurrency.background_priority():
                disc = discovery_module.run_discovery(adapter, {})
            candidates = disc.get("candidates") or []
            if disc.get("unavailable"):
                # S3: a failed screener is a failed cycle, never an empty market snapshot
                # J2-4: plain words (never a raw dict), and honest: nothing is paused, the next cycle retries
                why = disc.get("reason")
                if isinstance(why, dict):
                    why = why.get("message") or why.get("status") or "provider failure"
                pause_reason = (f"last cycle failed: market screener unavailable ({str(why or 'provider failure')[:160]}); "
                                "nothing was cataloged and the next scheduled cycle retries")
                candidates = []
            # QA remediation D3: this snapshot persistence is now gated to
            # exclude a real SyntheticDataAdapter (demo mode) -- SAME
            # Demo/Live separation intent app/main.py's
            # _catalog_*_best_effort helpers already apply via their own
            # isinstance(adapter, LiveUWAdapter) checks. Written as a
            # NEGATIVE check here (not SyntheticDataAdapter) rather than
            # a positive LiveUWAdapter check so that scheduler-mechanics
            # tests exercising a plain test-double adapter (never a real
            # SyntheticDataAdapter) keep their existing, already-verified
            # cataloging behavior unaffected by this fix -- only the ONE
            # real adapter class whose data is genuinely fabricated/demo
            # is ever excluded. Before this fix, running the scheduler
            # against TIP_MODE=demo (e.g. a dry_run smoke test before a
            # UW key is ever configured) would have cataloged fabricated
            # synthetic candidates into the SAME discovery_snapshots/
            # discovery_candidates tables Live data relies on --
            # contaminating real historical data with synthetic rows.
            if not isinstance(adapter, SyntheticDataAdapter) and not pause_reason:
                try:
                    catalog.catalog_discovery_snapshot(
                        filters={}, candidates=candidates, snapshot_at=disc.get("snapshot_at") or now.isoformat(),
                        kind="background",
                    )
                except (StorageGuardPausedError, IngestionStorageGuardPausedError) as e:
                    pause_reason = str(e)
                    candidates = []  # storage guard paused -- do not proceed into per-ticker evaluation either

            if evaluate and not pause_reason:
                bounded = [c for c in candidates if c.get("evaluate_supported")][: self.top_n]
                for c in bounded:
                    ticker = c.get("ticker")
                    if not ticker:
                        continue
                    abort = self._abort_reason(recurring, reported_mode)
                    if abort:
                        pause_reason = abort
                        lease_lost = abort.startswith("lease lost")
                        break
                    try:
                        with uw_concurrency.background_priority():
                            self.run_ticker_scan(adapter, ticker)
                        candidates_evaluated += 1
                    except (StorageGuardPausedError, IngestionStorageGuardPausedError) as e:
                        pause_reason = str(e)
                        break  # storage guard paused mid-cycle -- stop evaluating further tickers
                    except Exception:  # noqa: BLE001 -- one bad ticker must never kill the whole cycle
                        logger.exception("scheduler: per-ticker evaluation failed for %s", ticker)
            # FE-F3: the historical backfill runs only in a RECURRING enabled cycle (never from a one-time
            # run) and only when the operator opted in (main.py: TIP_SCHEDULER_BASELINE_BACKFILL)
            if evaluate and recurring and not pause_reason and self.run_baseline_backfill is not None:
                abort = self._abort_reason(recurring, reported_mode)
                if abort:
                    pause_reason = abort
                    lease_lost = abort.startswith("lease lost")
            if evaluate and recurring and not pause_reason and self.run_baseline_backfill is not None:
                try:
                    with uw_concurrency.background_priority():
                        backfill_result = self.run_baseline_backfill(adapter)
                except Exception:  # noqa: BLE001 -- backfill must never kill the cycle
                    logger.exception("scheduler: baseline backfill failed")
        finally:
            if persist_next_run:
                next_run_at = (now.timestamp() + self.scan_interval_s)
                next_run_iso = datetime.fromtimestamp(next_run_at, tz=timezone.utc).isoformat()
            else:
                # One-time run (run_once_with_depth): must not set/clear any
                # future recurring execution time as a side effect -- carry
                # forward whatever next_run_at is ALREADY persisted (None if
                # nothing was ever armed, e.g. mode has always been
                # 'disabled') rather than writing a new value OR nulling out
                # a real one a still-armed recurring schedule depends on.
                next_run_iso = self.store.get_state().get("next_run_at")
            if not lease_lost:                        # S2: another worker owns the state now: never overwrite it
                self.store.record_run_finished(
                    candidates_evaluated=candidates_evaluated, next_run_at=next_run_iso,
                    pause_reason=pause_reason, finished_at=self._now_fn().isoformat(),
                )
        return {
            "ran": True, "mode": reported_mode, "candidates_evaluated": candidates_evaluated,
            "pause_reason": pause_reason, "baseline_backfill": backfill_result,
        }
