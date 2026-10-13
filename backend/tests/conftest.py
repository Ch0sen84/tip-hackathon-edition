import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("TIP_MODE", "demo")
os.environ.setdefault("TIP_DB_PATH", ":memory:")


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
