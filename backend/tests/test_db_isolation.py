"""Regression tests for #38 (SAFETY CRITICAL, 2026-10-15 round 2): pytest
must never open/write the real deployed SQLite volume
(./data/tip_hackathon.db), regardless of what TIP_DB_PATH the shell or
container the test happens to run inside has exported. See
app/storage.py's assert_safe_test_db_path() for the fail-closed guard this
covers, and conftest.py's forced (not setdefault) TIP_DB_PATH override.
"""
from __future__ import annotations

import os

import pytest

from app.storage import (
    REAL_DEFAULT_DB_PATH,
    QualifyingDaysStore,
    assert_safe_test_db_path,
)


def test_memory_path_always_accepted():
    """':memory:' is always safe -- it never touches disk at all."""
    store = QualifyingDaysStore(db_path=":memory:")
    assert store.db_path == ":memory:"
    # usable immediately
    store.record_scan("TESTX", True, "NEW")
    assert store.qualifying_days_count("TESTX") >= 1


def test_tmp_path_fixture_db_is_accepted(tmp_path):
    """A pytest tmp_path-backed file is a real isolated temp path -- must
    be accepted, never refused."""
    db_file = tmp_path / "isolated_test.db"
    store = QualifyingDaysStore(db_path=str(db_file))
    assert db_file.exists()
    store.record_scan("TESTY", True, "NEW")
    assert store.qualifying_days_count("TESTY") >= 1


def test_tempfile_mkdtemp_db_is_accepted():
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="tip_test_db_")
    db_path = os.path.join(tmp_dir, "scan.db")
    store = QualifyingDaysStore(db_path=db_path)
    assert os.path.exists(db_path)


def test_real_deployed_default_path_is_refused_under_pytest():
    """The exact real default path (what a deployed install actually uses)
    must be refused outright during a pytest run."""
    with pytest.raises(RuntimeError, match="REFUSING"):
        QualifyingDaysStore(db_path=REAL_DEFAULT_DB_PATH)


def test_path_outside_any_tempdir_is_refused_even_if_not_the_exact_default():
    """Any path that is neither ':memory:' nor inside the OS temp dir is
    refused -- not just the one specific default string. A relative path
    resembling a real deployed install (as if TIP_DB_PATH had been
    inherited from a real container/shell) must be rejected the same way."""
    fake_deployed_path = "./data/some_other_deployed_name.db"
    with pytest.raises(RuntimeError, match="REFUSING"):
        QualifyingDaysStore(db_path=fake_deployed_path)


def test_deliberately_hostile_tip_db_path_env_var_does_not_leak_into_a_real_open(monkeypatch):
    """Direct regression for #38's root cause: deliberately set TIP_DB_PATH
    to something resembling the real deployed path (simulating an
    inherited shell/container env var), rebuild Settings from that
    environment, and assert the test harness's own safety boundary -- not
    the app's Settings object -- refuses to let it actually open a DB
    during a pytest run. Settings() itself is intentionally dumb (it just
    reads env, by design -- see app/config.py's docstring); the enforced
    boundary lives at the point the DB is actually opened, which is what
    this test exercises end-to-end."""
    monkeypatch.setenv("TIP_DB_PATH", REAL_DEFAULT_DB_PATH)
    from app import config as _config

    hostile_settings = _config.Settings()
    assert hostile_settings.db_path == REAL_DEFAULT_DB_PATH  # env read correctly...
    with pytest.raises(RuntimeError, match="REFUSING"):
        # ...but the storage layer must still refuse to actually use it.
        QualifyingDaysStore(db_path=hostile_settings.db_path)


def test_assert_safe_test_db_path_is_a_noop_outside_pytest(monkeypatch):
    """The guard must never fire for the real running app (uvicorn/Docker)
    -- only ever during an actual pytest run. Simulate "not running under
    pytest" and confirm the real default path is accepted without error."""
    import app.storage as storage_mod

    monkeypatch.setattr(storage_mod, "_running_under_pytest", lambda: False)
    # Must not raise -- this call simulates the real deployed app, which
    # must keep working exactly as before this change.
    assert_safe_test_db_path(REAL_DEFAULT_DB_PATH)


def test_ambient_tip_db_path_env_var_from_the_real_test_session_is_already_isolated():
    """End-to-end confirmation that THIS test session's own effective
    TIP_DB_PATH (as conftest.py forces it) is itself safe -- i.e. the fix
    is actually wired up, not just unit-testable in isolation."""
    assert os.environ.get("TIP_DB_PATH") == ":memory:"
