"""Phase A: storage-guard circuit breaker tests. Mocks shutil.disk_usage
to simulate low-disk scenarios and confirms the pause/throttle thresholds
fire correctly, and that on-demand vs background thresholds genuinely
differ (background pauses earlier/more conservatively)."""
from __future__ import annotations

from collections import namedtuple

import pytest

from app.storage import QualifyingDaysStore, StorageGuardPausedError
from app.storage_guard import BACKGROUND, ONDEMAND, StorageGuard, default_mount_point

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _mb(n: float) -> int:
    return int(n * 1024 * 1024)


def test_background_threshold_is_more_conservative_than_ondemand_by_default():
    guard = StorageGuard("/tmp")
    assert guard.background_min_mb > guard.ondemand_min_mb


def test_constructor_rejects_a_background_threshold_lower_than_ondemand():
    with pytest.raises(ValueError):
        StorageGuard("/tmp", ondemand_min_mb=2048, background_min_mb=500)


def test_ondemand_allowed_when_plenty_of_space(monkeypatch):
    guard = StorageGuard("/tmp", ondemand_min_mb=500, background_min_mb=2048)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: _DiskUsage(total=_mb(100_000), used=_mb(90_000), free=_mb(10_000)),
    )
    result = guard.check_before_write(ONDEMAND)
    assert result.allowed is True


def test_ondemand_paused_below_its_own_threshold(monkeypatch):
    guard = StorageGuard("/tmp", ondemand_min_mb=500, background_min_mb=2048)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: _DiskUsage(total=_mb(10_000), used=_mb(9_900), free=_mb(100)),
    )
    result = guard.check_before_write(ONDEMAND)
    assert result.allowed is False
    assert "on_demand" in result.reason
    assert "No existing data was touched or deleted" in result.reason


def test_background_pauses_earlier_than_ondemand_at_the_same_free_space(monkeypatch):
    """The key design requirement: at a free-space level where on-demand
    is still allowed, background must ALREADY be paused -- so a user
    actively using the app is never blocked by their own background
    collector, but the collector itself backs off long before disk
    actually runs out."""
    guard = StorageGuard("/tmp", ondemand_min_mb=500, background_min_mb=2048)
    # 1000MB free: above the on-demand floor (500MB) but below the
    # background floor (2048MB).
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: _DiskUsage(total=_mb(10_000), used=_mb(9_000), free=_mb(1_000)),
    )
    ondemand_result = guard.check_before_write(ONDEMAND)
    background_result = guard.check_before_write(BACKGROUND)
    assert ondemand_result.allowed is True
    assert background_result.allowed is False


def test_background_also_paused_when_truly_critical(monkeypatch):
    guard = StorageGuard("/tmp", ondemand_min_mb=500, background_min_mb=2048)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: _DiskUsage(total=_mb(10_000), used=_mb(9_999), free=_mb(1)),
    )
    assert guard.check_before_write(ONDEMAND).allowed is False
    assert guard.check_before_write(BACKGROUND).allowed is False


def test_check_before_write_rejects_unknown_kind():
    guard = StorageGuard("/tmp")
    with pytest.raises(ValueError):
        guard.check_before_write("not_a_real_kind")


def test_thresholds_configurable_via_env_vars(monkeypatch):
    monkeypatch.setenv("TIP_STORAGE_GUARD_ONDEMAND_MIN_MB", "111")
    monkeypatch.setenv("TIP_STORAGE_GUARD_BACKGROUND_MIN_MB", "999")
    guard = StorageGuard("/tmp")
    assert guard.ondemand_min_mb == 111
    assert guard.background_min_mb == 999


def test_default_mount_point_is_the_db_files_directory(tmp_path):
    db_path = tmp_path / "sub" / "tip_hackathon.db"
    assert default_mount_point(str(db_path)) == str((tmp_path / "sub").resolve())


def test_qualifying_days_store_pauses_on_demand_writes_when_disk_critical(tmp_path, monkeypatch):
    """End-to-end: QualifyingDaysStore.record_scan (the real on-demand
    writer) actually raises StorageGuardPausedError when the storage
    guard reports critical, WITHOUT deleting or corrupting anything
    already written."""
    db_path = tmp_path / "scan.db"
    store = QualifyingDaysStore(db_path=str(db_path))
    store.record_scan("TESTZ", True, "NEW")  # succeeds while disk is fine
    assert store.qualifying_days_count("TESTZ") >= 1

    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: _DiskUsage(total=_mb(10_000), used=_mb(9_999), free=_mb(1)),
    )
    with pytest.raises(StorageGuardPausedError):
        store.record_scan("TESTZ", True, "BUILDING")

    # The earlier successful write is still intact -- the guard never
    # deletes anything, it only blocks the NEW write.
    assert store.qualifying_days_count("TESTZ") >= 1


def test_qualifying_days_store_skips_guard_for_in_memory_db():
    """':memory:' has no real disk-usage concept -- the guard must be a
    no-op there, never raising a spurious pause."""
    store = QualifyingDaysStore(db_path=":memory:")
    store.record_scan("TESTM", True, "NEW")  # must not raise
    assert store.qualifying_days_count("TESTM") >= 1
