"""Real MCP-protocol transport for the bounded 'Investigate with AI'
feature -- an alternate backend for the exact same allowlisted tool calls
that rest_tool_client.py executes over plain REST. Selected via
INVESTIGATE_TRANSPORT=mcp (see backend/app/config.py); REST
(INVESTIGATE_TRANSPORT=rest, the default) remains the documented fallback.

HOW: this process never imports an `mcp` package directly -- the official
MCP Python SDK is NOT a dependency of this app (backend/requirements.txt is
untouched). Instead it shells out to mcp_isolated/venv/bin/python running
mcp_isolated/mcp_investigate.py, a tiny isolated interpreter/venv that owns
the real `mcp` SDK install, pinned in mcp_isolated/requirements.txt. One
subprocess call per investigation (not per tool) connects once to the live
https://api.unusualwhales.com/api/mcp server, lists its real tools as a
safety check, then makes every accepted tool call for this investigation
over that one MCP session before exiting. No new port, no long-running
service.

CREDENTIAL HANDLING: the UW API key is passed to the subprocess ONLY via
the UW_API_KEY entry of its `env=` mapping -- never as a CLI argument
(would be visible in `ps`), never written to a file. It is read here from
the same settings_store-backed value the REST transport already uses
(main.py resolves it once and passes it into orchestrator.investigate(),
same as today).

SCHEMA RECONCILIATION (checked against the live server's real
tools/list output on 2026-10-12 -- see README.md "Investigate with AI ->
MCP transport" for the full note): of the 10 allowlisted tool names, all
10 exist verbatim on the real server (no renaming needed), but 3 use a
different argument name than the REST port assumed, and several do not
accept a `limit` at all (the real schema has `additionalProperties: false`,
so sending an unsupported key would make the whole call fail):

  get_flow_alerts, get_dark_pool_trades, get_option_trades:
      real arg is `ticker_symbol` (string), not `ticker`; both accept a
      `limit`.
  get_insider_activity_by_ticker, get_short_data_by_ticker,
  get_company_info, get_max_pain, get_greek_exposure_by_ticker:
      real arg is `ticker`, matching the REST port's assumption, but NONE
      of these accept a `limit` argument -- it is dropped, not sent.
  get_institutional_ownership_by_ticker:
      real arg is `ticker` + `limit` (max 50, the REST port's clamp of 100
      is narrowed further here to stay within the live schema).
  get_analyst_ratings:
      real arg is `ticker` (optional on the live server, but we always
      supply it) + `limit` (max 500).

MCP_ARG_BUILDERS below is the single place that reconciliation lives.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from .allowlist import ALLOWED_TOOL_NAMES

logger = logging.getLogger(__name__)

# mcp_isolated/ lives at the repo root, two levels up from backend/app/ai/.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MCP_VENV_PYTHON = _REPO_ROOT / "mcp_isolated" / "venv" / "bin" / "python"
DEFAULT_MCP_SCRIPT = _REPO_ROOT / "mcp_isolated" / "mcp_investigate.py"

# Env var overrides exist only so tests (and unusual local layouts) can
# point at a stub interpreter/script without touching the real paths above.
MCP_VENV_PYTHON = Path(os.environ.get("MCP_VENV_PYTHON", str(DEFAULT_MCP_VENV_PYTHON)))
MCP_SCRIPT = Path(os.environ.get("MCP_SCRIPT", str(DEFAULT_MCP_SCRIPT)))

DEFAULT_SUBPROCESS_TIMEOUT_S = 45

_TICKER_SYMBOL_TOOLS = frozenset({"get_flow_alerts", "get_dark_pool_trades", "get_option_trades"})
_TICKER_ONLY_TOOLS = frozenset({
    "get_insider_activity_by_ticker",
    "get_short_data_by_ticker",
    "get_company_info",
    "get_max_pain",
    "get_greek_exposure_by_ticker",
})


def _args_ticker_symbol_and_limit(ticker: str, limit: int) -> dict:
    return {"ticker_symbol": ticker, "limit": limit}


def _args_ticker_only(ticker: str, limit: int) -> dict:  # noqa: ARG001 - limit intentionally unused, see module docstring
    return {"ticker": ticker}


def _args_institutional_ownership(ticker: str, limit: int) -> dict:
    return {"ticker": ticker, "limit": min(limit, 50)}


def _args_analyst_ratings(ticker: str, limit: int) -> dict:
    return {"ticker": ticker, "limit": min(limit, 500)}


MCP_ARG_BUILDERS: dict[str, Callable[[str, int], dict]] = {
    **{name: _args_ticker_symbol_and_limit for name in _TICKER_SYMBOL_TOOLS},
    **{name: _args_ticker_only for name in _TICKER_ONLY_TOOLS},
    "get_institutional_ownership_by_ticker": _args_institutional_ownership,
    "get_analyst_ratings": _args_analyst_ratings,
}

assert set(MCP_ARG_BUILDERS.keys()) == set(ALLOWED_TOOL_NAMES), "MCP_ARG_BUILDERS must cover exactly the allowlist"


class MCPSubprocessError(RuntimeError):
    """Raised when the isolated subprocess itself can't be run at all
    (missing venv, timeout, unparseable stdout) -- NOT raised for an
    individual tool call failing; that's reported per-result instead, same
    as rest_tool_client.execute_tool never raising for a single bad call."""


def execute_tools_mcp(
    calls: list[dict],
    uw_api_key: str,
    timeout_s: int = DEFAULT_SUBPROCESS_TIMEOUT_S,
) -> list[dict]:
    """Batched MCP equivalent of calling rest_tool_client.execute_tool once
    per accepted call. `calls` is the same `accepted_calls` shape
    validation.validate_tool_selection produces:
    [{"tool": str, "args": {"ticker": str, "limit": int}}, ...].

    Returns one {"tool", "ok", "data", "error"} dict per input call, in the
    same order -- the same shape rest_tool_client.execute_tool returns, so
    orchestrator.py's synthesis step and schemas.py's validation don't need
    to know which transport ran.

    Never raises for a single bad tool call (mirrors the REST transport);
    DOES raise MCPSubprocessError if the subprocess/venv itself can't be
    invoked at all (e.g. venv missing -- that's a deployment problem, not a
    per-tool-call failure, and the caller should surface it as a 502, not
    silently return 4 fake failures).
    """
    if not calls:
        return []

    if not MCP_VENV_PYTHON.exists():
        raise MCPSubprocessError(
            f"mcp_isolated venv not found at {MCP_VENV_PYTHON}. "
            "Run: python3 -m venv mcp_isolated/venv && "
            "mcp_isolated/venv/bin/pip install -r mcp_isolated/requirements.txt"
        )

    payload_calls = []
    for call in calls:
        tool = call["tool"]
        args = call.get("args", {})
        ticker = args.get("ticker", "")
        limit = int(args.get("limit", 20))
        builder = MCP_ARG_BUILDERS.get(tool)
        if builder is None:
            # Defensive only; validate_tool_selection already filtered to
            # the allowlist, and MCP_ARG_BUILDERS covers it exactly.
            payload_calls.append({"tool": tool, "args": {}})
            continue
        payload_calls.append({"tool": tool, "args": builder(ticker, limit)})

    env = dict(os.environ)
    env["UW_API_KEY"] = uw_api_key  # subprocess env ONLY -- never argv, never a file

    try:
        proc = subprocess.run(
            [str(MCP_VENV_PYTHON), str(MCP_SCRIPT)],
            input=json.dumps({"calls": payload_calls}),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise MCPSubprocessError(f"mcp_investigate.py subprocess timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise MCPSubprocessError(f"failed to launch mcp_isolated subprocess: {exc}") from exc

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MCPSubprocessError(
            f"mcp_investigate.py produced unparseable stdout (exit {proc.returncode}): "
            f"{proc.stdout[:500]!r} stderr: {proc.stderr[:500]!r}"
        ) from exc

    if not parsed.get("ok", False) and not parsed.get("results"):
        # Connection/handshake-level failure (e.g. bad key, server down) --
        # every call fails the same way; report it per-call rather than
        # raising, same crash-proof contract as the REST transport.
        error_msg = parsed.get("error", "unknown MCP connection failure")
        logger.warning("MCP subprocess connection failure: %s", error_msg)
        return [{"tool": c["tool"], "ok": False, "data": None, "error": error_msg} for c in calls]

    results_by_tool: dict[str, dict] = {}
    for r in parsed.get("results", []):
        results_by_tool.setdefault(r["tool"], r)

    out: list[dict] = []
    for call in calls:
        tool = call["tool"]
        r = results_by_tool.get(tool)
        if r is None:
            out.append({"tool": tool, "ok": False, "data": None, "error": "no result returned for this tool"})
            continue
        data: Any = r.get("data")
        # Each content item from mcp_investigate.py is a JSON text string
        # (MCP tool results are a list of content blocks); unwrap to plain
        # data the same shape rest_tool_client.execute_tool returns.
        if r.get("ok") and isinstance(data, list) and len(data) == 1:
            try:
                data = json.loads(data[0])
            except (json.JSONDecodeError, TypeError):
                pass  # leave as raw text if it wasn't JSON
        out.append({"tool": tool, "ok": r.get("ok", False), "data": data if r.get("ok") else None, "error": r.get("error")})
    return out
