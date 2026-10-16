"""Phase D, items 4/5/6: SchedulerRunner mode behavior (disabled/dry_run/
enabled), bounded top-N evaluation, market-hours-awareness, lease
integration, and reuse of the real discovery/evaluation pipeline (via
injected callables -- never a second pipeline). Every adapter here is a
synthetic in-process fake; zero live UW calls, zero Anthropic calls."""
from __future__ import annotations

from datetime import datetime, timezone

from app.ingestion import IngestionCatalog
from app.scheduler import SchedulerRunner


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


def _runner(tmp_path, adapter, run_ticker_scan_calls, top_n=2, scan_interval_s=900, lease_ttl_s=90):
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
        scan_interval_s=scan_interval_s,
        lease_ttl_s=lease_ttl_s,
        poll_interval_s=1,
    ), catalog


MARKET_OPEN_WEDNESDAY = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 11:00 ET, a real trading day
MARKET_CLOSED_SATURDAY = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
MARKET_CLOSED_OVERNIGHT = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)  # 23:00 ET prior day, closed


def test_disabled_mode_makes_zero_discovery_calls(tmp_path):
    rows = [{"ticker": "NVDA", "net_premium": "5000000"}]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, catalog = _runner(tmp_path, adapter, calls)
    outcome = runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert outcome == {"ran": False, "reason": "disabled"}
    assert adapter.discover_calls == 0
    assert calls == []


def test_dry_run_mode_discovers_but_never_evaluates_tickers(tmp_path):
    rows = [{"ticker": "NVDA", "net_premium": "5000000"}, {"ticker": "TSLA", "net_premium": "3000000"}]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, catalog = _runner(tmp_path, adapter, calls)
    runner.store.set_mode("dry_run")
    outcome = runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert outcome["ran"] is True
    assert outcome["mode"] == "dry_run"
    assert outcome["candidates_evaluated"] == 0
    assert adapter.discover_calls == 1
    assert calls == []  # NO per-ticker evaluation in dry_run -- explicit spec requirement

    # And the snapshot really was cataloged (real discovery_snapshots row).
    with catalog._conn() as conn:  # noqa: SLF001
        count = conn.execute("SELECT COUNT(*) FROM discovery_snapshots").fetchone()[0]
    assert count == 1


def test_enabled_mode_discovers_and_evaluates_bounded_top_n(tmp_path):
    rows = [
        {"ticker": "A", "net_premium": "1000000"},
        {"ticker": "B", "net_premium": "9000000"},
        {"ticker": "C", "net_premium": "5000000"},
        {"ticker": "D", "net_premium": "2000000"},
    ]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, catalog = _runner(tmp_path, adapter, calls, top_n=2)
    runner.store.set_mode("enabled")
    outcome = runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert outcome["ran"] is True
    assert outcome["candidates_evaluated"] == 2  # bounded to top_n, never all 4
    assert len(calls) == 2
    # Ranked by |net_premium| descending -- B (9M) then C (5M) are the real top 2.
    assert calls == ["B", "C"]


def test_enabled_mode_skips_index_instruments_marked_evaluate_unsupported(tmp_path):
    rows = [
        {"ticker": "SPXW", "is_index": True, "net_premium": "9000000"},
        {"ticker": "NVDA", "is_index": False, "net_premium": "5000000"},
    ]
    adapter = _FakeAdapter(rows)
    calls = []
    runner, catalog = _runner(tmp_path, adapter, calls, top_n=5)
    runner.store.set_mode("enabled")
    runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert calls == ["NVDA"]  # SPXW never evaluated -- evaluate_supported=False


def test_market_closed_weekend_never_runs_a_cycle(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls)
    runner.store.set_mode("enabled")
    outcome = runner.run_one_cycle_if_due(now=MARKET_CLOSED_SATURDAY)
    assert outcome == {"ran": False, "reason": "market_closed"}
    assert adapter.discover_calls == 0


def test_market_closed_overnight_never_runs_a_cycle(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls)
    runner.store.set_mode("enabled")
    outcome = runner.run_one_cycle_if_due(now=MARKET_CLOSED_OVERNIGHT)
    assert outcome == {"ran": False, "reason": "market_closed"}


def test_not_due_yet_skips_a_cycle_until_the_interval_elapses(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls, scan_interval_s=900)
    runner.store.set_mode("dry_run")
    first = runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert first["ran"] is True
    from datetime import timedelta
    soon = MARKET_OPEN_WEDNESDAY + timedelta(seconds=60)
    second = runner.run_one_cycle_if_due(now=soon)
    assert second == {"ran": False, "reason": "not_due"}
    assert adapter.discover_calls == 1  # only the first cycle actually called discover


def test_due_again_after_interval_elapses(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls, scan_interval_s=900)
    runner.store.set_mode("dry_run")
    runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    from datetime import timedelta
    later = MARKET_OPEN_WEDNESDAY + timedelta(seconds=901)
    second = runner.run_one_cycle_if_due(now=later)
    assert second["ran"] is True
    assert adapter.discover_calls == 2


def test_runner_acquires_and_uses_its_own_lease(tmp_path):
    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls)
    runner.store.set_mode("dry_run")
    runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    lease = runner.store.get_lease()
    assert lease["worker_id"] == runner.worker_id


def test_second_runner_cannot_run_while_first_holds_the_lease(tmp_path):
    """Two SchedulerRunner instances pointed at the SAME db file (the
    cross-restart/cross-process scenario) -- only one may actually run a
    cycle at a time."""
    adapter1 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    adapter2 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls1, calls2 = [], []
    # scan_interval_s=1 so runner2's attempt a few seconds later is
    # genuinely "due" again (isolating the LEASE as the reason it's
    # blocked, not an unrelated not-due check) -- lease_ttl_s=3600 keeps
    # runner1's lease alive well past that.
    runner1, catalog = _runner(tmp_path, adapter1, calls1, scan_interval_s=1, lease_ttl_s=3600)
    runner1.store.set_mode("dry_run")

    def run_ticker_scan2(adapter_arg, ticker):
        calls2.append(ticker)
        return {"ticker": ticker, "state": "WATCHING"}

    runner2 = SchedulerRunner(
        db_path=str(tmp_path / "db.sqlite"), get_adapter=lambda: adapter2, get_catalog=lambda: catalog,
        run_ticker_scan=run_ticker_scan2, top_n=2, scan_interval_s=1, lease_ttl_s=3600, poll_interval_s=1,
    )

    first_outcome = runner1.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert first_outcome["ran"] is True
    assert adapter1.discover_calls == 1

    from datetime import timedelta
    second_outcome = runner2.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY + timedelta(seconds=5))
    assert second_outcome == {"ran": False, "reason": "lease_not_held"}
    assert adapter2.discover_calls == 0  # runner2 made ZERO UW calls -- lease genuinely prevented the overlap


def test_graceful_stop_releases_lease_so_another_runner_can_immediately_take_over(tmp_path):
    adapter1 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls1 = []
    # scan_interval_s=1 so the SECOND runner's attempt a moment later is
    # "due" again, isolating lease-release as the thing under test.
    runner1, catalog = _runner(tmp_path, adapter1, calls1, scan_interval_s=1, lease_ttl_s=3600)
    runner1.store.set_mode("dry_run")
    runner1.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    runner1.stop(timeout=1.0)  # never actually started the thread, but exercises release_lease directly

    adapter2 = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls2 = []

    def run_ticker_scan2(adapter_arg, ticker):
        calls2.append(ticker)
        return {"ticker": ticker, "state": "WATCHING"}

    runner2 = SchedulerRunner(
        db_path=str(tmp_path / "db.sqlite"), get_adapter=lambda: adapter2, get_catalog=lambda: catalog,
        run_ticker_scan=run_ticker_scan2, top_n=2, scan_interval_s=1, lease_ttl_s=3600, poll_interval_s=1,
    )
    from datetime import timedelta
    outcome = runner2.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY + timedelta(seconds=2))
    assert outcome["ran"] is True  # immediately takeable -- stop() released the lease, didn't wait for TTL
    assert adapter2.discover_calls == 1


def test_enabled_mode_reuses_core_discovery_module_not_a_second_pipeline(tmp_path, monkeypatch):
    """Direct proof of reuse: patches core.discovery.run_discovery (the
    SAME function app.main's route calls) and confirms the scheduler's
    cycle actually calls through it, rather than having its own
    independent normalize/rank logic."""
    import app.scheduler as scheduler_module

    adapter = _FakeAdapter([{"ticker": "NVDA", "net_premium": "1"}])
    calls = []
    runner, _ = _runner(tmp_path, adapter, calls)
    runner.store.set_mode("dry_run")

    call_log = []
    real_run_discovery = scheduler_module.discovery_module.run_discovery

    def spy(adapter_arg, filters):
        call_log.append((adapter_arg, filters))
        return real_run_discovery(adapter_arg, filters)

    monkeypatch.setattr(scheduler_module.discovery_module, "run_discovery", spy)
    runner.run_one_cycle_if_due(now=MARKET_OPEN_WEDNESDAY)
    assert len(call_log) == 1
    assert call_log[0][0] is adapter
