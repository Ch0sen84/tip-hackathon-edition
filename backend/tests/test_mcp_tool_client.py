"""Tests for the real-MCP-protocol transport (app/ai/mcp_tool_client.py).
No network access and no real `mcp` SDK/venv are ever required here --
subprocess.run is monkeypatched to a fake isolated-subprocess response,
exactly the same way test_investigate.py monkeypatches execute_tool for
the REST transport. These confirm the argument-name/schema reconciliation
(ticker -> ticker_symbol for 3 tools, no `limit` for tools whose real
schema doesn't accept one) and the crash-proof contract (one bad call, a
subprocess connection failure, and a missing venv all degrade gracefully
or raise the one documented exception type).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from app.ai import mcp_tool_client
from app.ai.mcp_tool_client import MCP_ARG_BUILDERS, MCPSubprocessError, execute_tools_mcp


class _FakePath:
    """Minimal stand-in for mcp_tool_client.MCP_VENV_PYTHON: only needs to
    answer .exists() and str() the same way execute_tools_mcp uses the real
    Path, without mutating the real (immutable) Path object."""

    def __init__(self, exists: bool, path: str = "/fake/mcp_isolated/venv/bin/python"):
        self._exists = exists
        self._path = path

    def exists(self) -> bool:
        return self._exists

    def __str__(self) -> str:
        return self._path


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _ok_subprocess_response(results: list[dict]) -> str:
    return json.dumps({
        "ok": True,
        "server_info": {"name": "unusual-whales-public-api", "version": "1.0.0"},
        "results": results,
    })


# --- Argument reconciliation (real MCP schema, not the REST assumption) --


def test_ticker_symbol_tools_use_real_arg_name():
    for tool in ("get_flow_alerts", "get_dark_pool_trades", "get_option_trades"):
        args = MCP_ARG_BUILDERS[tool]("ZYNC", 20)
        assert args == {"ticker_symbol": "ZYNC", "limit": 20}
        assert "ticker" not in args


def test_ticker_only_tools_never_send_limit():
    for tool in ("get_insider_activity_by_ticker", "get_short_data_by_ticker", "get_company_info", "get_max_pain", "get_greek_exposure_by_ticker"):
        args = MCP_ARG_BUILDERS[tool]("ZYNC", 999)
        assert args == {"ticker": "ZYNC"}
        assert "limit" not in args


def test_institutional_ownership_clamps_to_live_schema_max_of_50():
    args = MCP_ARG_BUILDERS["get_institutional_ownership_by_ticker"]("ZYNC", 999)
    assert args == {"ticker": "ZYNC", "limit": 50}


def test_analyst_ratings_clamps_to_live_schema_max_of_500():
    args = MCP_ARG_BUILDERS["get_analyst_ratings"]("ZYNC", 999999)
    assert args == {"ticker": "ZYNC", "limit": 500}


def test_arg_builders_cover_exactly_the_allowlist():
    from app.ai.allowlist import ALLOWED_TOOL_NAMES
    assert set(MCP_ARG_BUILDERS.keys()) == set(ALLOWED_TOOL_NAMES)


# --- execute_tools_mcp: happy path, mocked subprocess --------------------


def test_execute_tools_mcp_happy_path(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=True))

    captured = {}

    def fake_run(cmd, input, capture_output, text, env, timeout):  # noqa: A002
        captured["cmd"] = cmd
        captured["input"] = json.loads(input)
        captured["env"] = env
        return FakeCompletedProcess(_ok_subprocess_response([
            {"tool": "get_company_info", "ok": True, "data": [json.dumps({"data": {"symbol": "ZYNC"}})], "error": None},
        ]))

    monkeypatch.setattr(subprocess, "run", fake_run)

    calls = [{"tool": "get_company_info", "args": {"ticker": "ZYNC", "limit": 20}}]
    results = execute_tools_mcp(calls, uw_api_key="fake-key-not-real")

    assert len(results) == 1
    assert results[0]["tool"] == "get_company_info"
    assert results[0]["ok"] is True
    assert results[0]["data"] == {"data": {"symbol": "ZYNC"}}

    # Real schema arg name was used, not the REST "ticker" assumption left as-is for this tool (it IS "ticker" here, but confirm no "limit" leaked through)
    assert captured["input"]["calls"] == [{"tool": "get_company_info", "args": {"ticker": "ZYNC"}}]

    # Credential never goes in argv -- only in the subprocess env mapping.
    assert "fake-key-not-real" not in captured["cmd"]
    assert captured["env"]["UW_API_KEY"] == "fake-key-not-real"


def test_execute_tools_mcp_maps_ticker_symbol_tools(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=True))
    captured = {}

    def fake_run(cmd, input, capture_output, text, env, timeout):  # noqa: A002
        captured["input"] = json.loads(input)
        return FakeCompletedProcess(_ok_subprocess_response([
            {"tool": "get_flow_alerts", "ok": True, "data": [json.dumps([])], "error": None},
        ]))

    monkeypatch.setattr(subprocess, "run", fake_run)

    calls = [{"tool": "get_flow_alerts", "args": {"ticker": "ZYNC", "limit": 15}}]
    execute_tools_mcp(calls, uw_api_key="fake-key-not-real")

    assert captured["input"]["calls"] == [{"tool": "get_flow_alerts", "args": {"ticker_symbol": "ZYNC", "limit": 15}}]


def test_execute_tools_mcp_empty_calls_short_circuits(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("subprocess.run should never be invoked for an empty call list")

    monkeypatch.setattr(subprocess, "run", fail_if_called)
    assert execute_tools_mcp([], uw_api_key="fake-key-not-real") == []


# --- Crash-proof behavior: one bad call never takes down the batch -------


def test_execute_tools_mcp_one_tool_error_does_not_fail_others(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=True))

    def fake_run(cmd, input, capture_output, text, env, timeout):  # noqa: A002
        return FakeCompletedProcess(_ok_subprocess_response([
            {"tool": "get_company_info", "ok": True, "data": [json.dumps({"data": {}})], "error": None},
            {"tool": "get_max_pain", "ok": False, "data": None, "error": "upstream 403"},
        ]))

    monkeypatch.setattr(subprocess, "run", fake_run)

    calls = [
        {"tool": "get_company_info", "args": {"ticker": "ZYNC", "limit": 20}},
        {"tool": "get_max_pain", "args": {"ticker": "ZYNC", "limit": 20}},
    ]
    results = execute_tools_mcp(calls, uw_api_key="fake-key-not-real")

    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["error"] == "upstream 403"


def test_execute_tools_mcp_connection_failure_degrades_all_calls(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=True))

    def fake_run(cmd, input, capture_output, text, env, timeout):  # noqa: A002
        return FakeCompletedProcess(json.dumps({"ok": False, "error": "MCP connection/handshake failed: bad key", "results": []}), returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    calls = [{"tool": "get_company_info", "args": {"ticker": "ZYNC", "limit": 20}}]
    results = execute_tools_mcp(calls, uw_api_key="fake-key-not-real")

    assert results[0]["ok"] is False
    assert "bad key" in results[0]["error"]


def test_execute_tools_mcp_timeout_raises_subprocess_error(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=True))

    def fake_run(cmd, input, capture_output, text, env, timeout):  # noqa: A002
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MCPSubprocessError):
        execute_tools_mcp([{"tool": "get_company_info", "args": {"ticker": "ZYNC"}}], uw_api_key="fake-key-not-real")


def test_execute_tools_mcp_unparseable_stdout_raises_subprocess_error(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=True))

    def fake_run(cmd, input, capture_output, text, env, timeout):  # noqa: A002
        return FakeCompletedProcess("not json at all", returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MCPSubprocessError):
        execute_tools_mcp([{"tool": "get_company_info", "args": {"ticker": "ZYNC"}}], uw_api_key="fake-key-not-real")


def test_execute_tools_mcp_missing_venv_raises_subprocess_error(monkeypatch):
    monkeypatch.setattr(mcp_tool_client, "MCP_VENV_PYTHON", _FakePath(exists=False))
    with pytest.raises(MCPSubprocessError, match="venv not found"):
        execute_tools_mcp([{"tool": "get_company_info", "args": {"ticker": "ZYNC"}}], uw_api_key="fake-key-not-real")
