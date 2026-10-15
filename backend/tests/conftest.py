import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("TIP_MODE", "demo")
# SAFETY CRITICAL (#38, 2026-10-15 round 2): this MUST be a forced
# override, never os.environ.setdefault. A shell/container that already
# exports TIP_DB_PATH (e.g. pointed at a real deployed install) would
# otherwise win over this test suite's own isolation, which is exactly the
# bug that let pytest previously write synthetic test records into a real
# deployed SQLite volume. The test suite's own fixture setup must always
# take priority over any inherited env var -- ":memory:" is used here
# (every test using the default db_path gets a private, fully in-process
# DB) with app/storage.py's assert_safe_test_db_path() as a hard fail-
# closed backstop for any test that constructs a Storage with its own
# explicit path instead of the settings default (see
# tests/test_db_isolation.py for the regression coverage).
os.environ["TIP_DB_PATH"] = ":memory:"


@pytest.fixture
def app_main(tmp_path, monkeypatch):
    """Isolated app.main module state for settings/credential tests: a
    fresh per-test settings.json path, no leftover UW/AI key, no leftover
    adapter cache, and public_demo reset to False. Prevents one test's
    saved key/mode from leaking into the next test."""
    from app import main as _main
    from app.settings_store import SettingsStore

    _main._settings_store = SettingsStore(tmp_path / "settings.json")
    _main._adapter = None
    _main._adapter_sig = None
    _main._last_uw_test_result = None
    monkeypatch.setattr(_main.settings, "public_demo", False)
    monkeypatch.setattr(_main.settings, "uw_api_key", "")
    yield _main
