#!/usr/bin/env python3
"""Isolated subprocess entry point for real MCP-protocol tool calls against
the official Unusual Whales hosted MCP server.

Runs ONLY under mcp_isolated/venv's interpreter (its own 'mcp' SDK install,
kept fully separate from backend/requirements.txt -- see
mcp_isolated/requirements.txt for why). Invoked by
backend/app/ai/mcp_tool_client.py via subprocess, one call per
investigation, stdin/stdout JSON, no new network port, no long-running
process.

Contract:
  stdin  (JSON): {"calls": [{"tool": "<real mcp tool name>", "args": {...}}, ...]}
                 args must already be the REAL MCP schema's argument names
                 (e.g. "ticker_symbol" for get_flow_alerts/get_dark_pool_trades/
                 get_option_trades, "ticker" for the rest) -- that mapping is
                 the main app's job (mcp_tool_client.py), not this script's.
  stdout (JSON): {"ok": true, "server_info": {"name":..., "version":...},
                  "results": [{"tool":..., "ok": bool, "data": <tool result
                  content or null>, "error": <str or null>}, ...]}
                 or, if the connection/handshake itself fails:
                 {"ok": false, "error": "<str>", "results": []}

Credential handling: the UW API key is read ONLY from the UW_API_KEY
environment variable (set by the parent process's subprocess.run(..., env=...)
call) -- never from argv (would leak into `ps`), never from a file, never
printed. Nothing in this script's stdout/stderr ever includes the raw key.

Bounded: the caller enforces MAX_TOOLS_PER_INVESTIGATION before invoking
this script; this script itself places no additional cap but does nothing
beyond what's listed in "calls" -- no autonomous looping, no tool chaining,
no further LLM calls (this is pure protocol plumbing, no reasoning).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

UW_MCP_URL = os.environ.get("UW_MCP_URL", "https://api.unusualwhales.com/api/mcp")
CONNECT_TIMEOUT_S = 30.0


async def _run(calls: list[dict]) -> dict:
    api_key = os.environ.get("UW_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "UW_API_KEY not set in subprocess environment", "results": []}

    headers = {"Authorization": f"Bearer {api_key}"}
    http_client = httpx2.AsyncClient(headers=headers, timeout=CONNECT_TIMEOUT_S)

    results: list[dict] = []
    try:
        async with http_client:
            async with streamable_http_client(UW_MCP_URL, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    init_result = await session.initialize()
                    server_info = {
                        "name": init_result.server_info.name,
                        "version": init_result.server_info.version,
                    }

                    # Real tools/list capability call -- used as a guard so we
                    # never invoke a tool name the live server doesn't
                    # actually expose (protects against allowlist drift),
                    # not merely assumed from the REST port's allowlist.
                    tools_result = await session.list_tools()
                    live_tool_names = {t.name for t in tools_result.tools}

                    for call in calls:
                        tool_name = call.get("tool")
                        args = call.get("args") or {}
                        if tool_name not in live_tool_names:
                            results.append({
                                "tool": tool_name,
                                "ok": False,
                                "data": None,
                                "error": f"'{tool_name}' is not a tool the live MCP server currently exposes",
                            })
                            continue
                        try:
                            call_result = await session.call_tool(tool_name, args)
                            if call_result.is_error:
                                text = "; ".join(
                                    getattr(c, "text", str(c)) for c in call_result.content
                                )
                                results.append({"tool": tool_name, "ok": False, "data": None, "error": text or "tool reported an error"})
                            else:
                                payload = []
                                for c in call_result.content:
                                    if hasattr(c, "text"):
                                        payload.append(c.text)
                                    else:
                                        payload.append(str(c))
                                results.append({"tool": tool_name, "ok": True, "data": payload, "error": None})
                        except Exception as exc:  # one bad tool call must not take down the batch
                            results.append({"tool": tool_name, "ok": False, "data": None, "error": str(exc)})

                    return {"ok": True, "server_info": server_info, "results": results}
    except Exception as exc:
        return {"ok": False, "error": f"MCP connection/handshake failed: {exc}", "results": results}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {exc}", "results": []}))
        return 1

    calls = payload.get("calls") or []
    out = asyncio.run(_run(calls))
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
