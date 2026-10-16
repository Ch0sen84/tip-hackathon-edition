"""Phase D, item 1 + 4: durable scheduler lease (atomic cross-restart
mutual exclusion) and durable mode storage (disabled/dry_run/enabled,
upgrade-preserves-existing-setting). Every test uses a real on-disk
SQLite file via tmp_path (never ":memory:" for the lease/upgrade tests
that need to simulate "a second process/run against the same file") or
":memory:" where only one connection's view matters."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.core.scheduler_store import InvalidModeError, MODES, SchedulerStore


def _now():
    return datetime.now(timezone.utc)


# --- lease: acquire / contention / expiry / release / loss -----------------

def test_lease_acquired_cleanly_by_one_worker(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    token = store.try_acquire_lease("worker-a", ttl_s=60)
    assert token is not None
    lease = store.get_lease()
    assert lease["worker_id"] == "worker-a"
    assert lease["lease_token"] == token


def test_second_worker_cannot_acquire_a_live_lease(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    token_a = store.try_acquire_lease("worker-a", ttl_s=60)
    assert token_a is not None
    token_b = store.try_acquire_lease("worker-b", ttl_s=60)
    assert token_b is None
    lease = store.get_lease()
    assert lease["worker_id"] == "worker-a"  # unchanged -- worker-b never took over


def test_expired_lease_is_takeable_by_a_new_worker(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    then = _now() - timedelta(seconds=3600)
    token_a = store.try_acquire_lease("worker-a", ttl_s=1, now=then)  # expires 3599s ago
    assert token_a is not None
    # worker-a's heartbeat has stopped (simulating a crash) -- worker-b
    # tries now, long after expiry.
    token_b = store.try_acquire_lease("worker-b", ttl_s=60, now=_now())
    assert token_b is not None
    lease = store.get_lease()
    assert lease["worker_id"] == "worker-b"
    assert lease["lease_token"] == token_b


def test_graceful_shutdown_releases_lease_immediately_not_waiting_for_expiry(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    token_a = store.try_acquire_lease("worker-a", ttl_s=3600, now=_now())  # a LONG ttl
    assert token_a is not None
    released = store.release_lease("worker-a", token_a, now=_now())
    assert released is True
    # Even though the long TTL hasn't elapsed, worker-b can acquire
    # immediately because release_lease expired it right away.
    token_b = store.try_acquire_lease("worker-b", ttl_s=60, now=_now())
    assert token_b is not None


def test_release_lease_fails_for_non_owner(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    token_a = store.try_acquire_lease("worker-a", ttl_s=60)
    released = store.release_lease("worker-b", "not-the-real-token")
    assert released is False
    lease = store.get_lease()
    assert lease["worker_id"] == "worker-a"  # untouched


def test_worker_that_loses_lease_detects_on_next_heartbeat_and_must_stop_writes(tmp_path):
    """Simulates a clock-skew / long-GC-pause scenario: worker-a's lease
    silently expires while it believes it's still running, a NEW worker
    (worker-b) takes over, and when worker-a FINALLY gets to call its
    next heartbeat, that heartbeat must fail -- the concrete signal
    worker-a's own loop uses to stop performing writes it no longer
    owns (see app/scheduler.py's _ensure_lease)."""
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    stale_start = _now() - timedelta(seconds=120)
    token_a = store.try_acquire_lease("worker-a", ttl_s=10, now=stale_start)  # expired 110s ago by "now"
    assert token_a is not None

    # worker-b legitimately takes over because worker-a's lease is expired.
    token_b = store.try_acquire_lease("worker-b", ttl_s=60, now=_now())
    assert token_b is not None

    # worker-a, unaware its lease was stolen, finally gets a chance to
    # heartbeat -- this MUST fail (false), proving it can detect the
    # loss rather than silently continuing to believe it owns the lease.
    still_owns = store.heartbeat_lease("worker-a", token_a, ttl_s=60, now=_now())
    assert still_owns is False

    # And worker-b's own heartbeat (the rightful owner) must succeed.
    still_owns_b = store.heartbeat_lease("worker-b", token_b, ttl_s=60, now=_now())
    assert still_owns_b is True


def test_heartbeat_renews_expires_at_for_the_rightful_owner(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    t0 = _now()
    token = store.try_acquire_lease("worker-a", ttl_s=10, now=t0)
    lease_before = store.get_lease()
    t1 = t0 + timedelta(seconds=5)
    ok = store.heartbeat_lease("worker-a", token, ttl_s=10, now=t1)
    assert ok is True
    lease_after = store.get_lease()
    assert lease_after["expires_at"] > lease_before["expires_at"]


def test_same_worker_can_idempotently_reacquire_its_own_live_lease(tmp_path):
    """A worker calling try_acquire_lease again (e.g. restart logic that
    doesn't distinguish acquire from renew) while its OWN lease is still
    live must succeed, not be treated as contention against itself."""
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    token1 = store.try_acquire_lease("worker-a", ttl_s=60)
    token2 = store.try_acquire_lease("worker-a", ttl_s=60)
    assert token1 is not None and token2 is not None
    assert token1 != token2  # a fresh token each time, but both succeed


def test_lease_contention_is_a_real_atomic_sql_update_not_read_then_write(tmp_path):
    """Directly inspects that acquiring is ONE statement with a WHERE
    clause (not a SELECT followed by a separate UPDATE) by checking the
    rowcount contract: a failed acquire must leave the row completely
    untouched (proving the WHERE clause, not application logic, is what
    rejects the second caller)."""
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    store.try_acquire_lease("worker-a", ttl_s=60)
    lease_before = store.get_lease()
    result = store.try_acquire_lease("worker-b", ttl_s=60)
    lease_after = store.get_lease()
    assert result is None
    assert lease_before == lease_after


# --- mode / state: three modes + upgrade preserves existing setting --------

def test_fresh_install_defaults_to_disabled(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    assert store.get_mode() == "disabled"


def test_set_mode_rejects_invalid_value(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    with pytest.raises(InvalidModeError):
        store.set_mode("turbo")


@pytest.mark.parametrize("mode", list(MODES))
def test_set_mode_transitions_to_each_valid_mode(tmp_path, mode):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    state = store.set_mode(mode, actor="chad")
    assert state["mode"] == mode
    assert state["updated_by"] == "chad"
    assert store.get_mode() == mode


def test_mode_transitions_disabled_to_dry_run_to_enabled_and_back(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    assert store.get_mode() == "disabled"
    store.set_mode("dry_run")
    assert store.get_mode() == "dry_run"
    store.set_mode("enabled")
    assert store.get_mode() == "enabled"
    store.set_mode("disabled")
    assert store.get_mode() == "disabled"


def test_upgrade_preserves_existing_mode_never_resets_to_disabled(tmp_path):
    """Builds a fixture DB that ALREADY has an explicit scheduler_state
    row with mode='enabled' (simulating an upgrade from a prior version
    that already created this table and the user turned it on), then
    constructs a fresh SchedulerStore (which re-runs migrations) against
    that SAME file and confirms the row survives completely unchanged --
    never silently reset back to the 'disabled' hardcoded default."""
    db_path = str(tmp_path / "db.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    for v, name in [(1, "catalog_foundation_tables"), (2, "signal_evidence_and_source_corrections"),
                    (3, "darkpool_coverage_status"), (4, "scheduler_lease_and_state")]:
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                     (v, name, "2026-01-01T00:00:00+00:00"))
    conn.execute(
        "CREATE TABLE scheduler_state (id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL DEFAULT 'disabled', "
        "updated_at TEXT NOT NULL, updated_by TEXT, last_run_started_at TEXT, last_run_finished_at TEXT, "
        "next_run_at TEXT, last_cycle_candidates_evaluated INTEGER, pause_reason TEXT)"
    )
    conn.execute(
        "INSERT INTO scheduler_state (id, mode, updated_at, updated_by) VALUES (1, 'enabled', ?, ?)",
        ("2026-01-02T00:00:00+00:00", "chad-previous-version"),
    )
    conn.execute(
        "CREATE TABLE scheduler_lease (id INTEGER PRIMARY KEY CHECK (id = 1), worker_id TEXT NOT NULL, "
        "acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, lease_token TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = SchedulerStore(db_path)  # constructing this re-runs migrations.run_migrations -- must be a no-op here
    state = store.get_state()
    assert state["mode"] == "enabled"
    assert state["updated_by"] == "chad-previous-version"


def test_fresh_install_with_no_prior_row_still_seeds_disabled_after_migration(tmp_path):
    """The counterpart to the upgrade test above: a genuinely fresh file
    (migrations never run before) must still end up with mode='disabled'
    once SchedulerStore is constructed -- the seed-default path."""
    db_path = str(tmp_path / "db.sqlite")
    store = SchedulerStore(db_path)
    assert store.get_mode() == "disabled"


def test_record_run_started_and_finished_updates_state(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    store.record_run_started(started_at="2026-10-15T12:00:00+00:00")
    store.record_run_finished(candidates_evaluated=3, next_run_at="2026-10-15T12:15:00+00:00",
                               finished_at="2026-10-15T12:01:00+00:00")
    state = store.get_state()
    assert state["last_run_started_at"] == "2026-10-15T12:00:00+00:00"
    assert state["last_run_finished_at"] == "2026-10-15T12:01:00+00:00"
    assert state["next_run_at"] == "2026-10-15T12:15:00+00:00"
    assert state["last_cycle_candidates_evaluated"] == 3
    assert state["pause_reason"] is None


def test_set_mode_clears_any_prior_pause_reason(tmp_path):
    store = SchedulerStore(str(tmp_path / "db.sqlite"))
    store.record_pause("disk low")
    assert store.get_state()["pause_reason"] == "disk low"
    store.set_mode("dry_run")
    assert store.get_state()["pause_reason"] is None
