"""Phase 4B: the two new standalone connection-test routes. No real
network call is ever made -- the underlying llm_provider/mcp_tool_client
functions are monkeypatched, same pattern as test_investigate.py.

Covers the user's explicit requirement: "A successful AI provider test
must not be described as proof that MCP works, and a successful MCP test
must not be described as proof of end-to-end agent execution" -- enforced
here by asserting the exact disclosure fields each route always returns.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.ai import llm_provider as ai_llm_provider
from app.ai import mcp_tool_client as ai_mcp_tool_client


def _client(app_main):
    return TestClient(app_main.app)


# --- /api/settings/ai-key/test ------------------------------------------


def test_ai_key_test_requires_explicit_confirm(app_main, monkeypatch):
    client = _client(app_main)
    client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "fake-key"})
    resp = client.post("/api/settings/ai-key/test", json={"confirm": False})
    assert resp.status_code == 400
    assert "confirm=true" in resp.json()["detail"]


def test_ai_key_test_requires_a_saved_key_first(app_main):
    client = _client(app_main)
    resp = client.post("/api/settings/ai-key/test", json={"confirm": True})
    assert resp.status_code == 400
    assert "No AI provider key" in resp.json()["detail"]


def test_ai_key_test_calls_the_real_test_function_exactly_once_and_discloses_scope(app_main, monkeypatch):
    calls = {"n": 0}

    def fake_test(provider, api_key):
        calls["n"] += 1
        assert provider == "anthropic"
        assert api_key == "fake-key"
        return {"ok": True, "model": "claude-sonnet-5-5", "error": None}

    monkeypatch.setattr(ai_llm_provider, "test_provider_connection", fake_test)
    client = _client(app_main)
    client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "fake-key"})
    resp = client.post("/api/settings/ai-key/test", json={"confirm": True})
    assert resp.status_code == 200
    body = resp.json()
    assert calls["n"] == 1
    assert body["ok"] is True
    # Explicit disclosure requirement: a passing AI-provider test must
    # never be presentable as "MCP works too".
    assert body["proves_mcp_works"] is False


def test_ai_key_test_disabled_in_public_demo(app_main):
    client = _client(app_main)
    client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "fake-key"})
    app_main.settings.public_demo = True
    resp = client.post("/api/settings/ai-key/test", json={"confirm": True})
    assert resp.status_code == 403


# --- /api/settings/mcp/test ----------------------------------------------


def test_mcp_test_requires_a_saved_uw_key_first(app_main):
    client = _client(app_main)
    resp = client.post("/api/settings/mcp/test")
    assert resp.status_code == 400
    assert "No UW API key" in resp.json()["detail"]


def test_mcp_test_never_requires_or_touches_the_ai_key(app_main, monkeypatch):
    """The MCP test is UW-only and must work with zero AI provider
    configuration at all -- it is explicitly NOT an LLM call."""
    calls = {"n": 0}

    def fake_test(uw_api_key):
        calls["n"] += 1
        assert uw_api_key == "fake-uw-key"
        return {"ok": True, "server_info": {"name": "unusual-whales-public-api", "version": "1.0.0"}, "tool_ok": True, "error": None}

    monkeypatch.setattr(ai_mcp_tool_client, "test_mcp_connection", fake_test)
    client = _client(app_main)
    client.post("/api/settings/uw-key", json={"api_key": "fake-uw-key"})
    resp = client.post("/api/settings/mcp/test")
    assert resp.status_code == 200
    body = resp.json()
    assert calls["n"] == 1
    assert body["ok"] is True
    assert body["server_info"]["name"] == "unusual-whales-public-api"
    # Explicit disclosure requirement, mirrored from the AI-key test above:
    # a passing MCP test must never be presentable as "the whole AI agent
    # pipeline works".
    assert body["proves_ai_synthesis_works"] is False


def test_mcp_test_disabled_in_public_demo(app_main):
    client = _client(app_main)
    client.post("/api/settings/uw-key", json={"api_key": "fake-uw-key"})
    app_main.settings.public_demo = True
    resp = client.post("/api/settings/mcp/test")
    assert resp.status_code == 403


# --- app/ai/mcp_tool_client.test_mcp_connection (unit-level, subprocess mocked) --


def test_test_mcp_connection_reports_missing_venv_without_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_mcp_tool_client, "MCP_VENV_PYTHON", tmp_path / "does-not-exist")
    result = ai_mcp_tool_client.test_mcp_connection("fake-key")
    assert result["ok"] is False
    assert "venv not found" in result["error"]


# --- app/ai/llm_provider.test_provider_connection (unit-level, requests mocked) --


def test_test_provider_connection_anthropic_success(monkeypatch):
    from unittest.mock import MagicMock

    resp = MagicMock(ok=True, status_code=200)
    monkeypatch.setattr(ai_llm_provider.requests, "post", lambda *a, **k: resp)
    result = ai_llm_provider.test_provider_connection("anthropic", "fake-key")
    assert result == {"ok": True, "model": "claude-sonnet-5-5", "error": None}


def test_test_provider_connection_reports_http_failure(monkeypatch):
    from unittest.mock import MagicMock

    resp = MagicMock(ok=False, status_code=401, text="invalid x-api-key")
    monkeypatch.setattr(ai_llm_provider.requests, "post", lambda *a, **k: resp)
    result = ai_llm_provider.test_provider_connection("anthropic", "bad-key")
    assert result["ok"] is False
    assert "401" in result["error"]
