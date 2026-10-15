"""Market Radar (2026-10-14, Phase A): user-editable watchlist, persisted
scan telemetry, and the honest watchlist-vs-market-wide scan_scope
disclosure. Uses the same app_main fixture (conftest.py) every other
settings test uses -- isolated settings.json per test, no leftover
adapter cache."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_default_watchlist_before_any_save_is_the_env_seed(app_main, monkeypatch):
    monkeypatch.setenv("TIP_LIVE_WATCHLIST", "SPY,QQQ,NVDA,TSLA")
    import importlib
    from app import config as config_module
    importlib.reload(config_module)
    monkeypatch.setattr(app_main, "settings", config_module.settings)
    client = TestClient(app_main.app)
    resp = client.get("/api/settings/watchlist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["watchlist"] == ["SPY", "QQQ", "NVDA", "TSLA"]
    assert body["max_size"] >= 1


def test_saving_a_watchlist_persists_normalizes_and_dedupes(app_main):
    client = TestClient(app_main.app)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["amd", "AMD", " msft ", "nvda"]})
    assert resp.status_code == 200, resp.text
    saved = resp.json()["watchlist"]
    assert saved == ["AMD", "MSFT", "NVDA"]  # deduped (case-insensitive), order-preserving, trimmed

    # Persists across a fresh GET (same settings.json)
    resp2 = client.get("/api/settings/watchlist")
    assert resp2.json()["watchlist"] == ["AMD", "MSFT", "NVDA"]


def test_watchlist_rejects_a_format_invalid_ticker_with_400_not_a_silent_drop(app_main):
    client = TestClient(app_main.app)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["NVDA", "NOT-A-TICKER!!"]})
    assert resp.status_code == 400
    assert "not a valid ticker" in resp.json()["detail"]
    # The invalid save must NOT have taken effect -- watchlist stays whatever it was before.
    resp2 = client.get("/api/settings/watchlist")
    assert "NOT-A-TICKER!!" not in resp2.json()["watchlist"]


def test_watchlist_rejects_oversized_list_with_a_clear_quota_reason(app_main, monkeypatch):
    from app import settings_store as settings_store_module
    monkeypatch.setattr(settings_store_module, "MAX_WATCHLIST_SIZE", 3)
    client = TestClient(app_main.app)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["SPY", "QQQ", "NVDA", "TSLA"]})
    assert resp.status_code == 400
    assert "limit" in resp.json()["detail"].lower()
    assert "quota" in resp.json()["detail"].lower()


def test_saved_watchlist_actually_changes_what_radar_scans(app_main):
    """Not just persisted -- the NEXT /api/radar call must reflect it.
    This is the real behavior Chad explicitly asked to verify (adapter
    cache invalidation), not just a settings-file round-trip."""
    client = TestClient(app_main.app)
    # app_main fixture defaults to demo mode -- force live with a fake
    # verified key so get_adapter() actually builds a LiveUWAdapter.
    from unittest.mock import MagicMock
    from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient

    app_main._settings_store.set_uw_key("fake-key")
    app_main._settings_store.record_verified_uw_key("fake-key")
    app_main._settings_store.set_mode("live")

    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AMD", "MSFT"]})
    assert resp.status_code == 200

    adapter = app_main.get_adapter()
    assert isinstance(adapter, LiveUWAdapter)
    assert adapter.get_universe() == ["AMD", "MSFT"]


def test_radar_response_discloses_watchlist_scope_and_last_scan_time(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_scope"] == "watchlist"
    assert body["last_scan_at"] is not None


def test_last_scan_time_persists_across_a_fresh_settings_store_instance():
    """Simulates a container recreation: a NEW SettingsStore pointed at
    the SAME file must still see the last real scan, not reset to None."""
    import tempfile
    from pathlib import Path
    from app.settings_store import SettingsStore

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        store1 = SettingsStore(path)
        store1.record_scan(7)
        store2 = SettingsStore(path)  # fresh instance, same file
        data = store2.get_all()
        assert data["last_scan_count"] == 7
        assert data["last_scan_at"] is not None
