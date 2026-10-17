"""Final remediation round (2026-10-16): Run Once decoupled from the
persisted recurring scheduler mode. Covers SchedulerRunner.run_once_with_depth
and the /api/scheduler/run-once route's new optional `depth` field.

Every requirement below is proven by its OWN named test, not an
aggregate pass count:
  * recurring mode stays Disabled before/during/after a one-time run
    (direct DB read before/after)
  * Discover Once makes only the screener/discovery call, zero
    per-ticker evaluation calls
  * Discover & Evaluate Once evaluates exactly the configured top-N
    candidates, no more, no fewer
  * two simultaneous one-time requests cannot both start (lease
    rejection via the SAME in-process cycle lock + SQLite lease every
    other trigger path uses)
  * a manually-triggered one-time cycle and a scheduled recurring cycle
    respect the SAME lease across two different trigger paths
  * a restart does not re-arm recurring scheduling from a one-time
    run's state
  * zero automatic Anthropic/app.ai.* calls anywhere in this flow
  * no future next_run_at is persisted as a side effect
  * no RTH/market-hours gate applies to the one-time path
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from app.ingestion import IngestionCatalog
from app.scheduler import (
    DEPTH_DISCOVERY_ONLY,
    DEPTH_DISCOVERY_PLUS_EVALUATION,
    SchedulerRunner,
)

MARKET_OPEN_WEDNESDAY = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
# Deliberately a real OUTSIDE-market-hours timestamp (3am ET on a
# Saturday) -- used by the RTH-bypass test below.
WEEKEND_OVERNIGHT = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)


class _FakeAdapter:
    def __init__(self, rows):
        self._rows = rows
        self.discover_calls = 0

    def discover_candidates(self, filters=None):
        self.discover_calls += 1
        return {
            "candidates": self._rows, "snapshot_at": "2026-10-16T14:00:00+00:00",
            "screener_requests_used": 1, "source": "fake", "unavailable": False, "reason": None,
        }

    def get_universe(self):
        return []


class _SlowFakeAdapter(_FakeAdapter):
    def __init__(self, rows, release_event: threading.Event, started_event: threading.Event):
        super().__init__(rows)
        self._release_event = release_event
        self._started_event = started_event

    def discover_candidates(self, filters=None):
        self._started_event.set()
        self._release_event.wait(timeout=5)
        return super().discover_candidates(filters)


def _runner(tmp_path, adapter, run_ticker_scan_calls, top_n=2, db_name="db.sqlite", **kwargs):
    db_path = str(tmp_path / db_name)
    catalog = IngestionCatalog(db_path)

    def run_ticker_scan(adapter_arg, ticker):
        run_ticker_scan_calls.append(ticker)
        return {"ticker": ticker, "state": "WATCHING"}

    return SchedulerRunner(
        db_path=db_path,
        get_adapter=lambda: adapter,
        get_catalog=lambda: catalog,
        run_ticker_scan=run_ticker_scan,
        top_n=top_n,
        scan_interval_s=kwargs.pop("scan_interval_s", 900),
        lease_ttl_s=kwargs.pop("lease_ttl_s", 90),
        poll_interval_s=1,
    ), catalog, db_path


def test_mode_stays_disabled_before_during_and_after_discovery_only_run(tmp_path):
    """DB read before/during/after -- the exact requirement wording."""
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "5000000"}])
    calls = []
    runner, _, db_path = _runner(tmp_path, adapter, calls)
    # Mode defaults to 'disabled'; never explicitly set anywhere in this test.

    mode_before = runner.store.get_mode()
    assert mode_before == "disabled"

    started = threading.Event()
    release = threading.Event()
    mode_during = {}

    class _BlockingAdapter(_FakeAdapter):
        def discover_candidates(self, filters=None):
            # Read mode from a SEPARATE, independent SchedulerStore instance
            # (not the runner's own in-memory object) pointed at the SAME
            # db file -- proves the PERSISTED value, not a cached attribute.
            # Written BEFORE signaling `started` so the main thread never
            # observes the dict before this write (no race).
            from app.core.scheduler_store import SchedulerStore
            mode_during["mode"] = SchedulerStore(db_path).get_mode()
            started.set()
            release.wait(timeout=5)
            return super().discover_candidates(filters)

    blocking_adapter = _BlockingAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    runner2, _, _ = _runner(tmp_path, blocking_adapter, [], db_name=db_path.split("/")[-1])
    runner2.get_adapter = lambda: blocking_adapter

    result_box = {}

    def go():
        result_box["outcome"] = runner2.run_once_with_depth(DEPTH_DISCOVERY_ONLY, now=MARKET_OPEN_WEDNESDAY)

    t = threading.Thread(target=go)
    t.start()
    assert started.wait(timeout=5)
    assert mode_during["mode"] == "disabled", "mode was mutated mid-run"
    release.set()
    t.join(timeout=5)

    assert result_box["outcome"]["ran"] is True
    mode_after = runner.store.get_mode()
    assert mode_after == "disabled"


def test_discover_once_makes_only_screener_call_zero_evaluation(tmp_path):
    rows = [
        {"ticker": "A", "net_premium": "1000000"},
        {"ticker": "B", "net_premium": "9000000"},
        {"ticker": "C", "net_premium": "5000000"},
    ]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, _, _ = _runner(tmp_path, adapter, calls, top_n=5)
    # Mode is 'disabled' the whole time -- the exact scenario this round fixes.
    assert runner.store.get_mode() == "disabled"

    outcome = runner.run_once_with_depth(DEPTH_DISCOVERY_ONLY, now=MARKET_OPEN_WEDNESDAY)

    assert outcome["ran"] is True
    assert outcome["depth"] == DEPTH_DISCOVERY_ONLY
    assert adapter.discover_calls == 1
    assert calls == [], "Discover Once must make zero per-ticker evaluation calls"
    assert outcome["candidates_evaluated"] == 0
    assert runner.store.get_mode() == "disabled"


def test_discover_and_evaluate_once_evaluates_exactly_configured_top_n(tmp_path):
    rows = [
        {"ticker": "A", "net_premium": "1000000"},
        {"ticker": "B", "net_premium": "9000000"},
        {"ticker": "C", "net_premium": "5000000"},
        {"ticker": "D", "net_premium": "7000000"},
    ]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, _, _ = _runner(tmp_path, adapter, calls, top_n=2)
    assert runner.store.get_mode() == "disabled"

    outcome = runner.run_once_with_depth(DEPTH_DISCOVERY_PLUS_EVALUATION, now=MARKET_OPEN_WEDNESDAY)

    assert outcome["ran"] is True
    assert outcome["depth"] == DEPTH_DISCOVERY_PLUS_EVALUATION
    assert outcome["candidates_evaluated"] == 2
    # Ranked by |net_premium| descending: B (9M), D (7M) -- exactly top_n=2,
    # never 1, never 3.
    assert calls == ["B", "D"]
    assert runner.store.get_mode() == "disabled"


def test_second_concurrent_one_time_request_is_rejected_not_queued(tmp_path):
    release_event = threading.Event()
    started_event = threading.Event()
    adapter = _SlowFakeAdapter([{"ticker": "NVDA", "net_premium": "1"}], release_event, started_event)
    calls = []
    runner, _, _ = _runner(tmp_path, adapter, calls, top_n=2)
    assert runner.store.get_mode() == "disabled"

    results = {}

    def first_call():
        results["first"] = runner.run_once_with_depth(DEPTH_DISCOVERY_ONLY, now=MARKET_OPEN_WEDNESDAY)

    t = threading.Thread(target=first_call)
    t.start()
    assert started_event.wait(timeout=5), "first call never reached discover_candidates"

    second_start = time.monotonic()
    second_outcome = runner.run_once_with_depth(DEPTH_DISCOVERY_PLUS_EVALUATION, now=MARKET_OPEN_WEDNESDAY)
    second_elapsed = time.monotonic() - second_start

    assert second_outcome == {
        "ran": False, "reason": "cycle_already_in_progress", "triggered_by": "run_once",
        "depth": DEPTH_DISCOVERY_PLUS_EVALUATION,
    }
    assert second_elapsed < 1.0, "second call blocked/waited instead of being rejected immediately"

    release_event.set()
    t.join(timeout=5)
    assert results["first"]["ran"] is True


def test_manual_one_time_and_scheduled_recurring_share_the_same_lease(tmp_path):
    """Cross-trigger-path safety: a manual one-time run_once_with_depth()
    on one runner/process cannot proceed while a genuinely DIFFERENT
    runner/process (simulating the automatic recurring loop, via
    run_one_cycle_if_due) already holds the SQLite scheduler_lease."""
    adapter1 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls1 = []
    runner1, catalog, db_path = _runner(tmp_path, adapter1, calls1, scan_interval_s=1, lease_ttl_s=3600)
    runner1.store.set_mode("dry_run")  # the recurring path requires a non-disabled mode
    runner1.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)  # runner1 now holds the lease

    adapter2 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls2 = []
    db_name = db_path.split("/")[-1]
    runner2, _, _ = _runner(tmp_path, adapter2, calls2, top_n=2, scan_interval_s=1, lease_ttl_s=3600, db_name=db_name)
    # runner2 is a second runner instance pointed at the SAME db file
    # (simulating a second process) -- it shares the one persisted
    # scheduler_state row, so it reads the same 'dry_run' mode runner1
    # just set; what matters here is runner2's call goes through the
    # DECOUPLED one-time path regardless, and still respects the lease.
    assert runner2.store.get_mode() == "dry_run"

    outcome = runner2.run_once_with_depth(DEPTH_DISCOVERY_ONLY, now=MARKET_OPEN_WEDNESDAY)
    assert outcome == {
        "ran": False, "reason": "lease_not_held", "triggered_by": "run_once", "depth": DEPTH_DISCOVERY_ONLY,
    }
    assert adapter2.discover_calls == 0


def test_one_time_run_never_persists_a_future_next_run_at(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _, _ = _runner(tmp_path, adapter, calls, top_n=2, scan_interval_s=900)
    state_before = runner.store.get_state()
    assert state_before["next_run_at"] is None

    runner.run_once_with_depth(DEPTH_DISCOVERY_PLUS_EVALUATION, now=MARKET_OPEN_WEDNESDAY)

    state_after = runner.store.get_state()
    assert state_after["next_run_at"] is None, "a one-time run must never arm a future recurring execution time"
    assert state_after["mode"] == "disabled"


def test_restart_does_not_re_arm_recurring_scheduling_from_a_one_time_run(tmp_path):
    """Simulates a container/process restart: construct a BRAND NEW
    SchedulerRunner (fresh worker_id, fresh in-process lock/lease-token
    state) against the SAME db file right after a one-time run, and
    confirm the automatic/recurring path still refuses to run (mode is
    still 'disabled') -- i.e. nothing about the one-time run's lease or
    state rows survives into looking like an armed recurring schedule."""
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _, db_path = _runner(tmp_path, adapter, calls, top_n=2, lease_ttl_s=90)
    outcome = runner.run_once_with_depth(DEPTH_DISCOVERY_PLUS_EVALUATION, now=MARKET_OPEN_WEDNESDAY)
    assert outcome["ran"] is True

    # "Restart": a brand new process constructs its own SchedulerRunner
    # instance pointed at the same persisted db -- no shared Python state
    # with `runner` at all.
    restarted_calls = []
    db_name = db_path.split("/")[-1]
    restarted_runner, _, _ = _runner(tmp_path, adapter, restarted_calls, top_n=2, db_name=db_name)

    due_outcome = restarted_runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert due_outcome == {"ran": False, "reason": "disabled"}
    assert restarted_calls == []
    assert restarted_runner.store.get_mode() == "disabled"


def test_one_time_run_bypasses_market_hours_rth_gate(tmp_path):
    """The recurring path (run_one_cycle_if_due) refuses outside market
    hours; the one-time path must NOT apply that same gate."""
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _, _ = _runner(tmp_path, adapter, calls, top_n=2)
    runner.store.set_mode("dry_run")
    recurring_outcome = runner.run_one_cycle_if_due(now=WEEKEND_OVERNIGHT)
    assert recurring_outcome == {"ran": False, "reason": "market_closed"}

    # Reset mode back to disabled -- the one-time call must still work.
    runner.store.set_mode("disabled")
    one_time_outcome = runner.run_once_with_depth(DEPTH_DISCOVERY_ONLY, now=WEEKEND_OVERNIGHT)
    assert one_time_outcome["ran"] is True
    assert adapter.discover_calls == 1


def test_invalid_depth_raises_value_error(tmp_path):
    adapter = _FakeAdapter([])
    runner, _, _ = _runner(tmp_path, adapter, [])
    try:
        runner.run_once_with_depth("not_a_real_depth", now=MARKET_OPEN_WEDNESDAY)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_once_with_depth_never_imports_or_calls_the_ai_orchestrator(tmp_path):
    """Same subprocess-import-check pattern as the existing D9 coverage
    (test_scheduler_run_once.py), run against the NEW decoupled path
    specifically, while mode is 'disabled' (the exact scenario this
    round introduces)."""
    import subprocess
    import sys
    from pathlib import Path

    scheduler_path = Path(__file__).resolve().parents[1] / "app" / "scheduler.py"
    backend_dir = str(scheduler_path.parents[1])
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.scheduler import SchedulerRunner, DEPTH_DISCOVERY_PLUS_EVALUATION\n"
        "from app.ingestion import IngestionCatalog\n"
        "import tempfile, os\n"
        "tmp = tempfile.mkdtemp()\n"
        "db = os.path.join(tmp, 'db.sqlite')\n"
        "catalog = IngestionCatalog(db)\n"
        "def run_ticker_scan(a, t):\n"
        "    return {'ticker': t, 'state': 'WATCHING'}\n"
        "class FakeAdapter:\n"
        "    def discover_candidates(self, filters=None):\n"
        "        return {'candidates': [{'ticker': 'NVDA', 'net_premium': '1'}], 'snapshot_at': 'x', "
        "'screener_requests_used': 1, 'source': 'fake', 'unavailable': False, 'reason': None}\n"
        "    def get_universe(self):\n"
        "        return []\n"
        "runner = SchedulerRunner(db_path=db, get_adapter=lambda: FakeAdapter(), "
        "get_catalog=lambda: catalog, run_ticker_scan=run_ticker_scan, top_n=1, "
        "scan_interval_s=900, lease_ttl_s=90, poll_interval_s=1)\n"
        "assert runner.store.get_mode() == 'disabled'\n"
        "runner.run_once_with_depth(DEPTH_DISCOVERY_PLUS_EVALUATION)\n"
        "ai_modules = [m for m in sys.modules if m == 'app.ai' or m.startswith('app.ai.')]\n"
        "print('AI_MODULES=' + ','.join(sorted(ai_modules)))\n"
    ) % backend_dir
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"run_once_with_depth() failed: {result.stderr}"
    assert "AI_MODULES=\n" in result.stdout or result.stdout.strip().endswith("AI_MODULES="), (
        f"run_once_with_depth() pulled in app.ai.* modules: {result.stdout}"
    )
