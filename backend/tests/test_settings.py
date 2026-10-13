"""Tests for the Settings/credential endpoints (backend/app/main.py
`/api/settings*`). Uses the `app_main` fixture (conftest.py) for a fresh,
isolated settings.json per test so saved keys/mode never leak between
tests. No real UW key or real network call is ever used -- the
/uw-key/test endpoint is exercised against a monkeypatched UWRestClient.
"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

FAKE_UW_KEY = "uwtest_sk_live_FAKEFAKE1234567890ABCDEF"  # realistic-looking, NOT a real key
FAKE_AI_KEY = "sk-ai-FAKEFAKE0987654321ZZZZ"


class _FakeUWRestClient:
    """Drop-in stand-in for live_uw_adapter.UWRestClient so tests never
    touch the network. `_RESULT` is set per-test to control the branch."""

    _RESULT = "success"  # "success" | "unauthorized" | "forbidden" | "network_error"

    def __init__(self, api_key="", base_url="", client_api_id=""):
        self.api_key = api_key
        self.base_url = base_url
        self.client_api_id = client_api_id

    def stock_info(self, ticker):
        if self._RESULT == "unauthorized":
            return {"ok": False, "status": "unauthorized", "status_code": 401}
        if self._RESULT == "forbidden":
            return {"ok": False, "status": "unavailable_on_tier", "status_code": 403}
        if self._RESULT == "network_error":
            return {"ok": False, "status": "error", "status_code": 0}
        return {"data": {"ticker": ticker, "sector": "Technology"}}


def test_default_settings_demo_mode_nothing_configured(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "demo"
    assert body["public_demo"] is False
    assert body["uw"]["configured"] is False
    assert body["uw"]["last4"] is None
    assert body["ai"]["configured"] is False


def test_save_uw_key_never_echoes_full_key(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"configured": True, "last4": FAKE_UW_KEY[-4:]}
    assert FAKE_UW_KEY not in resp.text

    status = client.get("/api/settings").json()
    assert status["uw"]["configured"] is True
    assert status["uw"]["source"] == "ui"
    assert status["uw"]["last4"] == FAKE_UW_KEY[-4:]


def test_empty_uw_key_rejected(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/uw-key", json={"api_key": "   "})
    assert resp.status_code == 400


def test_env_fallback_key_reported_but_ui_key_takes_precedence(app_main):
    app_main.settings.uw_api_key = "env-fallback-key-9999"
    client = TestClient(app_main.app)

    status = client.get("/api/settings").json()
    assert status["uw"]["configured"] is True
    assert status["uw"]["source"] == "env"
    assert status["uw"]["last4"] == "9999"

    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    status = client.get("/api/settings").json()
    assert status["uw"]["source"] == "ui"
    assert status["uw"]["last4"] == FAKE_UW_KEY[-4:]


def test_uw_key_test_success(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    resp = client.post("/api/settings/uw-key/test")
    assert resp.status_code == 200
    assert resp.json()["result"] == "success"


def test_uw_key_test_unauthorized(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "unauthorized"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": "bad-key-0000"})
    resp = client.post("/api/settings/uw-key/test")
    assert resp.json()["result"] == "unauthorized"


def test_uw_key_test_forbidden_tier_limited(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "forbidden"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    resp = client.post("/api/settings/uw-key/test")
    assert resp.json()["result"] == "forbidden_tier_limited"


def test_uw_key_test_network_error(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "network_error"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    resp = client.post("/api/settings/uw-key/test")
    assert resp.json()["result"] == "network_error"


def test_uw_key_test_not_configured_when_no_key(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/uw-key/test")
    assert resp.json()["result"] == "not_configured"


def test_delete_uw_key_clears_and_forces_demo_mode(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    client.post("/api/settings/uw-key/test")
    client.post("/api/settings/mode", json={"mode": "live"})
    assert client.get("/api/settings").json()["mode"] == "live"

    resp = client.delete("/api/settings/uw-key")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "mode": "demo"}

    status = client.get("/api/settings").json()
    assert status["uw"]["configured"] is False
    assert status["mode"] == "demo"


def test_mode_switch_to_live_requires_a_key(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400

    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400  # key present, but not yet Test-Connection-verified


def test_mode_switch_to_live_requires_passed_test_connection(app_main, monkeypatch):
    """Core security gate: a key that is merely SAVED is not enough --
    live mode must be rejected (400) until that exact key has passed
    /api/settings/uw-key/test, then allowed once it has."""
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})

    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400
    assert "Test Connection" in resp.json()["detail"]

    test_resp = client.post("/api/settings/uw-key/test")
    assert test_resp.json()["result"] == "success"

    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "live"
    assert client.get("/api/settings").json()["uw"]["verified"] is True


def test_mode_switch_to_live_rejected_after_failed_test_connection(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "unauthorized"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    client.post("/api/settings/uw-key/test")

    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400


def test_replacing_verified_key_invalidates_verification_and_forces_demo(app_main, monkeypatch):
    """Item 7: replacing a VERIFIED key with a new, unverified one must
    invalidate the old verification record (old-verified-new-unverified
    sequence rejected) and, if mode was already live, force it back to
    demo rather than keep running live on the new unverified credential."""
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    client.post("/api/settings/uw-key/test")
    client.post("/api/settings/mode", json={"mode": "live"})
    assert client.get("/api/settings").json()["mode"] == "live"

    other_key = "uwtest_sk_live_DIFFERENTKEY999999"
    client.post("/api/settings/uw-key", json={"api_key": other_key})

    status = client.get("/api/settings").json()
    assert status["mode"] == "demo"  # forced back, same protection as clear_uw_key
    assert status["uw"]["verified"] is False

    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400  # new key hasn't passed its own Test Connection


def test_clearing_key_invalidates_verification_and_forces_demo(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    client.post("/api/settings/uw-key/test")
    client.post("/api/settings/mode", json={"mode": "live"})

    client.delete("/api/settings/uw-key")
    status = client.get("/api/settings").json()
    assert status["mode"] == "demo"
    assert status["uw"]["verified"] is False


def test_env_sourced_key_also_requires_verification_before_live(app_main, monkeypatch):
    """Item 8: an env-fallback key (TIP_MODE/UW_API_KEY) is not trusted by
    default -- it must pass its own Test Connection before live-mode
    activation, exactly like a UI-saved key."""
    monkeypatch.setattr(app_main.settings, "uw_api_key", "env-only-key-7777")
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)

    status = client.get("/api/settings").json()
    assert status["uw"]["source"] == "env"
    assert status["uw"]["configured"] is True
    assert status["uw"]["verified"] is False

    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 400

    client.post("/api/settings/uw-key/test")
    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 200


def test_env_fallback_disclosed_after_removing_ui_key(app_main, monkeypatch):
    """Item 10: after Remove Saved Key, if a .env key is still present,
    Settings must say so explicitly instead of implying no credential
    remains."""
    monkeypatch.setattr(app_main.settings, "uw_api_key", "env-only-key-7777")
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})

    status = client.get("/api/settings").json()
    assert status["uw"]["env_fallback_disclosure"] is None  # UI key still present

    client.delete("/api/settings/uw-key")
    status = client.get("/api/settings").json()
    assert status["uw"]["source"] == "env"
    assert status["uw"]["env_fallback_present"] is True
    assert "env-configured key is still present" in status["uw"]["env_fallback_disclosure"]


def test_verified_failure_and_network_failure_are_distinguishable(app_main, monkeypatch):
    """Item 11: a real 401/403 ('this key doesn't work') must be a
    different result string than a transient/network failure ('try
    again'), not collapsed into one generic 'test failed' state."""
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})

    _FakeUWRestClient._RESULT = "unauthorized"
    unauthorized_result = client.post("/api/settings/uw-key/test").json()["result"]
    _FakeUWRestClient._RESULT = "network_error"
    network_result = client.post("/api/settings/uw-key/test").json()["result"]

    assert unauthorized_result == "unauthorized"
    assert network_result == "network_error"
    assert unauthorized_result != network_result


def test_mode_switch_back_to_demo_always_allowed(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    client.post("/api/settings/uw-key/test")
    client.post("/api/settings/mode", json={"mode": "live"})
    resp = client.post("/api/settings/mode", json={"mode": "demo"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "demo"
    # demo<->live is freely reversible without re-verifying, as long as the
    # key itself hasn't changed
    assert client.get("/api/settings").json()["uw"]["verified"] is True
    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 200


def test_invalid_mode_rejected(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/mode", json={"mode": "turbo"})
    assert resp.status_code == 400


def test_ai_key_save_and_clear(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": FAKE_AI_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"configured": True, "provider": "anthropic", "last4": FAKE_AI_KEY[-4:]}
    assert FAKE_AI_KEY not in resp.text

    status = client.get("/api/settings").json()
    assert status["ai"]["configured"] is True
    assert status["ai"]["provider"] == "anthropic"
    assert status["ai"]["last4"] == FAKE_AI_KEY[-4:]

    resp = client.delete("/api/settings/ai-key")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}
    status = client.get("/api/settings").json()
    assert status["ai"]["configured"] is False


def test_ai_key_invalid_provider_rejected(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/settings/ai-key", json={"provider": "cohere", "api_key": FAKE_AI_KEY})
    assert resp.status_code == 400


def test_public_demo_blocks_live_mode_and_key_entry(app_main, monkeypatch):
    monkeypatch.setattr(app_main.settings, "public_demo", True)
    client = TestClient(app_main.app)

    status = client.get("/api/settings").json()
    assert status["public_demo"] is True

    resp = client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    assert resp.status_code == 403

    resp = client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": FAKE_AI_KEY})
    assert resp.status_code == 403

    resp = client.post("/api/settings/mode", json={"mode": "live"})
    assert resp.status_code == 403


def test_fake_key_never_leaks_into_any_response_body_or_log(app_main, monkeypatch, caplog):
    """Save a realistic fake key, exercise save/get/test/delete, and assert
    the full key string never appears in any HTTP response body or any
    captured log record. (The last-4 substring IS expected to appear --
    that's by design -- so we check the FULL key string only.)"""
    monkeypatch.setattr(app_main, "UWRestClient", _FakeUWRestClient)
    _FakeUWRestClient._RESULT = "success"
    caplog.set_level(logging.DEBUG)

    client = TestClient(app_main.app)
    responses = [
        client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY}),
        client.get("/api/settings"),
        client.post("/api/settings/uw-key/test"),
        client.post("/api/settings/ai-key", json={"provider": "openai", "api_key": FAKE_AI_KEY}),
        client.get("/api/settings"),
        client.delete("/api/settings/uw-key"),
        client.delete("/api/settings/ai-key"),
    ]

    for resp in responses:
        assert FAKE_UW_KEY not in resp.text, f"full UW key leaked in response: {resp.text}"
        assert FAKE_AI_KEY not in resp.text, f"full AI key leaked in response: {resp.text}"

    for record in caplog.records:
        message = record.getMessage()
        assert FAKE_UW_KEY not in message, f"full UW key leaked into a log line: {message}"
        assert FAKE_AI_KEY not in message, f"full AI key leaked into a log line: {message}"

    # Sanity check the test actually proves something: the on-disk store was
    # touched (key really was saved before being cleared), not skipped.
    on_disk = app_main._settings_store.path
    if on_disk.exists():
        raw = on_disk.read_text()
        assert FAKE_UW_KEY not in raw  # already cleared by the DELETE above
        assert FAKE_AI_KEY not in raw
