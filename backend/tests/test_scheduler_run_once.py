"""QA remediation D9: bounded, supervised "run-once" scheduler validation
action -- runs exactly one cycle and returns to idle, a second
concurrent call is rejected (not queued), respects top_n=1, uses the
existing scheduler_lease mechanism, and never imports/calls the AI
orchestrator (reusing the exact zero-AI-import test pattern already
established for the automatic scheduler)."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from app.ingestion import IngestionCatalog
from app.scheduler import SchedulerRunner

MARKET_OPEN_WEDNESDAY = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


class _FakeAdapter:
    def __init__(self, rows):
        self._rows = rows
        self.discover_calls = 0

    def discover_candidates(self, filters=None):
        self.discover_calls += 1
        return {
            "candidates": self._rows, "snapshot_at": "2026-10-15T14:00:00+00:00",
            "screener_requests_used": 1, "source": "fake", "unavailable": False, "reason": None,
        }

    def get_universe(self):
        return []


class _SlowFakeAdapter(_FakeAdapter):
    """Discovery call blocks until released -- used to prove a SECOND
    run_once() call made while the first is genuinely still in flight is
    rejected, not queued."""

    def __init__(self, rows, release_event: threading.Event, started_event: threading.Event):
        super().__init__(rows)
        self._release_event = release_event
        self._started_event = started_event

    def discover_candidates(self, filters=None):
        self._started_event.set()
        self._release_event.wait(timeout=5)
        return super().discover_candidates(filters)


def _runner(tmp_path, adapter, run_ticker_scan_calls, top_n=2, **kwargs):
    catalog = IngestionCatalog(str(tmp_path / "db.sqlite"))

    def run_ticker_scan(adapter_arg, ticker):
        run_ticker_scan_calls.append(ticker)
        return {"ticker": ticker, "state": "WATCHING"}

    return SchedulerRunner(
        db_path=str(tmp_path / "db.sqlite"),
        get_adapter=lambda: adapter,
        get_catalog=lambda: catalog,
        run_ticker_scan=run_ticker_scan,
        top_n=top_n,
        scan_interval_s=kwargs.pop("scan_interval_s", 900),
        lease_ttl_s=kwargs.pop("lease_ttl_s", 90),
        poll_interval_s=1,
    ), catalog


def test_run_once_executes_exactly_one_cycle_and_returns_to_idle(tmp_path):
    rows = [{"ticker": "NVDA", "net_premium": "5000000"}]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls, top_n=2)
    runner.store.set_mode("enabled")

    outcome = runner.run_once(now=MARKET_OPEN_WEDNESDAY)
    assert outcome["ran"] is True
    assert outcome["triggered_by"] == "run_once"
    assert adapter.discover_calls == 1

    # Returns to idle: no second cycle happens on its own, the lock is
    # released, and a later run_once() call can run again (proves this
    # was a ONE-SHOT action, not a timer that was silently started).
    assert runner._cycle_lock.locked() is False  # noqa: SLF001
    outcome2 = runner.run_once(now=MARKET_OPEN_WEDNESDAY)
    assert outcome2["ran"] is True
    assert adapter.discover_calls == 2


def test_run_once_refuses_when_scheduler_mode_is_disabled(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls)
    # Mode defaults to "disabled" -- never explicitly set here.
    outcome = runner.run_once(now=MARKET_OPEN_WEDNESDAY)
    assert outcome == {"ran": False, "reason": "disabled", "triggered_by": "run_once"}
    assert adapter.discover_calls == 0


def test_run_once_respects_top_n_equal_to_one_with_no_off_by_one(tmp_path):
    rows = [
        {"ticker": "A", "net_premium": "1000000"},
        {"ticker": "B", "net_premium": "9000000"},
        {"ticker": "C", "net_premium": "5000000"},
    ]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls, top_n=1)
    runner.store.set_mode("enabled")
    outcome = runner.run_once(now=MARKET_OPEN_WEDNESDAY)
    assert outcome["candidates_evaluated"] == 1
    assert calls == ["B"]  # highest |net_premium| -- exactly one, never zero or two


def test_run_once_dry_run_mode_discovers_only_no_evaluation(tmp_path):
    rows = [{"ticker": "NVDA", "net_premium": "5000000"}]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls, top_n=5)
    runner.store.set_mode("dry_run")
    outcome = runner.run_once(now=MARKET_OPEN_WEDNESDAY)
    assert outcome["candidates_evaluated"] == 0
    assert calls == []


def test_second_concurrent_run_once_call_is_rejected_not_queued(tmp_path):
    """THE hardest D9 case: a second run_once() call made while the
    first is GENUINELY still executing (blocked mid-discovery on another
    thread) must be rejected immediately -- not block/wait/queue for the
    first to finish."""
    release_event = threading.Event()
    started_event = threading.Event()
    adapter = _SlowFakeAdapter([{"ticker": "NVDA", "net_premium": "1"}], release_event, started_event)
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls, top_n=2)
    runner.store.set_mode("dry_run")

    results = {}

    def first_call():
        results["first"] = runner.run_once(now=MARKET_OPEN_WEDNESDAY)

    t = threading.Thread(target=first_call)
    t.start()
    assert started_event.wait(timeout=5), "first call never reached discover_candidates"

    # The first call is now genuinely blocked INSIDE discover_candidates,
    # holding the cycle lock. A second call right now must be rejected
    # immediately (not after waiting for the first to finish).
    second_start = time.monotonic()
    second_outcome = runner.run_once(now=MARKET_OPEN_WEDNESDAY)
    second_elapsed = time.monotonic() - second_start

    assert second_outcome == {"ran": False, "reason": "cycle_already_in_progress", "triggered_by": "run_once"}
    assert second_elapsed < 1.0, "second call blocked/waited instead of being rejected immediately"

    release_event.set()
    t.join(timeout=5)
    assert results["first"]["ran"] is True


def test_run_once_cannot_overlap_a_real_recurring_cycle_via_the_lease(tmp_path):
    """Cross-process safety: run_once() must respect the SAME SQLite
    scheduler_lease a genuinely different worker/process already holds
    -- not just the in-process lock above."""
    adapter1 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls1 = []
    runner1, catalog = _runner(tmp_path, adapter1, calls1, scan_interval_s=1, lease_ttl_s=3600)
    runner1.store.set_mode("dry_run")
    runner1.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)  # runner1 now holds the lease

    adapter2 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls2 = []

    def run_ticker_scan2(adapter_arg, ticker):
        calls2.append(ticker)
        return {"ticker": ticker, "state": "WATCHING"}

    runner2 = SchedulerRunner(
        db_path=str(tmp_path / "db.sqlite"), get_adapter=lambda: adapter2, get_catalog=lambda: catalog,
        run_ticker_scan=run_ticker_scan2, top_n=2, scan_interval_s=1, lease_ttl_s=3600, poll_interval_s=1,
    )
    runner2.store.set_mode("dry_run")
    outcome = runner2.run_once(now=MARKET_OPEN_WEDNESDAY)
    assert outcome == {"ran": False, "reason": "lease_not_held", "triggered_by": "run_once"}
    assert adapter2.discover_calls == 0


def test_run_once_never_imports_or_calls_the_ai_orchestrator(tmp_path):
    """Reuses the exact same subprocess-import-check pattern as
    test_scheduler_no_ai_import.py's existing coverage -- confirms
    calling run_once specifically (not just importing the module) never
    pulls in app.ai.* ."""
    import subprocess
    import sys
    from pathlib import Path

    scheduler_path = Path(__file__).resolve().parents[1] / "app" / "scheduler.py"
    backend_dir = str(scheduler_path.parents[1])
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.scheduler import SchedulerRunner\n"
        "from app.ingestion import IngestionCatalog\n"
        "import tempfile, os\n"
        "tmp = tempfile.mkdtemp()\n"
        "db = os.path.join(tmp, 'db.sqlite')\n"
        "catalog = IngestionCatalog(db)\n"
        "def run_ticker_scan(a, t):\n"
        "    return {'ticker': t, 'state': 'WATCHING'}\n"
        "class FakeAdapter:\n"
        "    def discover_candidates(self, filters=None):\n"
        "        return {'candidates': [], 'snapshot_at': 'x', 'screener_requests_used': 1, "
        "'source': 'fake', 'unavailable': False, 'reason': None}\n"
        "    def get_universe(self):\n"
        "        return []\n"
        "runner = SchedulerRunner(db_path=db, get_adapter=lambda: FakeAdapter(), "
        "get_catalog=lambda: catalog, run_ticker_scan=run_ticker_scan, top_n=1, "
        "scan_interval_s=900, lease_ttl_s=90, poll_interval_s=1)\n"
        "runner.store.set_mode('dry_run')\n"
        "runner.run_once()\n"
        "ai_modules = [m for m in sys.modules if m == 'app.ai' or m.startswith('app.ai.')]\n"
        "print('AI_MODULES=' + ','.join(sorted(ai_modules)))\n"
    ) % backend_dir
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"run_once() failed: {result.stderr}"
    assert "AI_MODULES=\n" in result.stdout or result.stdout.strip().endswith("AI_MODULES="), (
        f"run_once() pulled in app.ai.* modules: {result.stdout}"
    )
