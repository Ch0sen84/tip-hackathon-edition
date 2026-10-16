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

from .core import discovery as discovery_module
from .core import uw_concurrency
from .core.scheduler_store import SchedulerStore, new_worker_id
from .core.trading_calendar import session_info
from .ingestion import StorageGuardPausedError as IngestionStorageGuardPausedError
from .storage import StorageGuardPausedError

logger = logging.getLogger(__name__)

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
        while not self._stop.is_set():
            try:
                self.run_one_cycle_if_due()
            except Exception:  # noqa: BLE001 -- the loop must never die from one bad cycle
                logger.exception("scheduler: cycle failed")
            self._stop.wait(self.poll_interval_s)

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
        if not self._ensure_lease(now):
            return {"ran": False, "reason": "lease_not_held"}
        return self._run_cycle(mode, now)

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

    def _run_cycle(self, mode: str, now: datetime) -> dict:
        self.store.record_run_started(started_at=now.isoformat())
        adapter = self.get_adapter()
        catalog = self.get_catalog()
        candidates_evaluated = 0
        pause_reason = None
        try:
            with uw_concurrency.background_priority():
                disc = discovery_module.run_discovery(adapter, {})
            candidates = disc.get("candidates") or []
            try:
                catalog.catalog_discovery_snapshot(
                    filters={}, candidates=candidates, snapshot_at=disc.get("snapshot_at") or now.isoformat(),
                    kind="background",
                )
            except (StorageGuardPausedError, IngestionStorageGuardPausedError) as e:
                pause_reason = str(e)
                candidates = []  # storage guard paused -- do not proceed into per-ticker evaluation either

            if mode == "enabled" and not pause_reason:
                bounded = [c for c in candidates if c.get("evaluate_supported")][: self.top_n]
                for c in bounded:
                    ticker = c.get("ticker")
                    if not ticker:
                        continue
                    try:
                        with uw_concurrency.background_priority():
                            self.run_ticker_scan(adapter, ticker)
                        candidates_evaluated += 1
                    except (StorageGuardPausedError, IngestionStorageGuardPausedError) as e:
                        pause_reason = str(e)
                        break  # storage guard paused mid-cycle -- stop evaluating further tickers
                    except Exception:  # noqa: BLE001 -- one bad ticker must never kill the whole cycle
                        logger.exception("scheduler: per-ticker evaluation failed for %s", ticker)
        finally:
            next_run_at = (now.timestamp() + self.scan_interval_s)
            next_run_iso = datetime.fromtimestamp(next_run_at, tz=timezone.utc).isoformat()
            self.store.record_run_finished(
                candidates_evaluated=candidates_evaluated, next_run_at=next_run_iso,
                pause_reason=pause_reason, finished_at=self._now_fn().isoformat(),
            )
        return {
            "ran": True, "mode": mode, "candidates_evaluated": candidates_evaluated,
            "pause_reason": pause_reason,
        }
