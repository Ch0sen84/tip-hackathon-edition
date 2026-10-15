"""Bounded, non-autonomous orchestration for the 'Investigate with AI'
feature. Hard structure, no loop beyond this:

  Round 1 (tool selection, ONE call): select_tools()
  Execution (<= MAX_TOOLS_PER_INVESTIGATION real calls): execute_tool() per call
  Round 2 (synthesis, the ONE allowed additional reasoning round): synthesize()

`select_tools`, `execute_tool`, and `synthesize` are imported as plain
names (not called via a class/module attribute chain) specifically so
tests can `monkeypatch.setattr(orchestrator, "select_tools", fake)` etc.
and assert each is invoked exactly once / at most four times -- see
backend/tests/test_investigate.py.
"""
from __future__ import annotations

import logging

from ..adapters.live_uw_adapter import UWRestClient
from .allowlist import MAX_TOOLS_PER_INVESTIGATION
from .llm_provider import LLMCallError, select_tools, synthesize  # noqa: F401  (re-exported for monkeypatching)
from .mcp_tool_client import execute_tools_mcp  # noqa: F401  (re-exported for monkeypatching)
from .rest_tool_client import execute_tool  # noqa: F401  (re-exported for monkeypatching)
from .schemas import InvestigationResult, MalformedAIResponse, validate_investigation_result
from .validation import ValidationError, validate_ticker, validate_tool_selection

logger = logging.getLogger(__name__)


class NoValidToolsSelectedError(ValueError):
    pass


def investigate(
    ticker: str,
    evidence: dict,
    ai_provider: str,
    ai_api_key: str,
    uw_api_key: str,
    uw_base_url: str,
    uw_client_api_id: str = "",
    transport: str = "rest",
) -> InvestigationResult:
    """`transport` selects which backend executes the accepted tool calls:
    "rest" (default -- plain REST calls via UWRestClient, the original
    fallback transport) or "mcp" (real MCP-protocol calls against UW's
    hosted server, via the isolated subprocess in mcp_tool_client.py). Both
    produce the exact same per-tool result shape, so everything from here
    down (synthesis, schema validation, the API response contract) is
    identical regardless of which one ran -- see main.py for how this is
    set from INVESTIGATE_TRANSPORT.
    """
    safe_ticker = validate_ticker(ticker)

    # --- Round 1: tool selection (exactly one LLM call) -------------------
    # Round-boundary logging + error re-raising below exist specifically
    # because of a real diagnosed gap: a failed investigation previously
    # gave NO indication of which of the two rounds (tool selection vs
    # synthesis) actually failed -- it had to be inferred after the fact
    # from the length of the error text. Nothing here logs prompt/response
    # content, only which round and whether it succeeded.
    try:
        raw_selection = select_tools(safe_ticker, evidence, ai_provider, ai_api_key)
    except LLMCallError as e:
        logger.warning("tool-selection round (round 1) failed for %s: %s", safe_ticker, e)
        raise type(e)(f"tool-selection round failed: {e}") from e
    logger.info("tool-selection round (round 1) completed for %s", safe_ticker)

    accepted_calls, rejected_selection = validate_tool_selection(raw_selection, safe_ticker)
    if not accepted_calls:
        raise NoValidToolsSelectedError(
            "The AI did not select any valid read-only tools for this ticker; nothing to investigate."
        )
    accepted_calls = accepted_calls[:MAX_TOOLS_PER_INVESTIGATION]

    # --- Execution: at most MAX_TOOLS_PER_INVESTIGATION real UW calls -----
    if transport == "mcp":
        tool_results = execute_tools_mcp(accepted_calls, uw_api_key=uw_api_key)
    else:
        client = UWRestClient(api_key=uw_api_key, base_url=uw_base_url, client_api_id=uw_client_api_id)
        tool_results = [execute_tool(call["tool"], call["args"]["ticker"], call["args"], client) for call in accepted_calls]
    succeeded = sum(1 for r in tool_results if r.get("ok"))
    logger.info(
        "tool execution (transport=%s) completed for %s: %d/%d calls succeeded",
        transport, safe_ticker, succeeded, len(tool_results),
    )

    # --- Round 2: synthesis (the one allowed additional reasoning round) --
    called_tools = [r["tool"] for r in tool_results]
    try:
        raw_synthesis = synthesize(safe_ticker, tool_results, ai_provider, ai_api_key)
    except LLMCallError as e:
        logger.warning("synthesis round (round 2) failed for %s: %s", safe_ticker, e)
        raise type(e)(f"synthesis round failed: {e}") from e
    logger.info("synthesis round (round 2) completed for %s", safe_ticker)
    if not isinstance(raw_synthesis, dict):
        raise MalformedAIResponse(f"synthesis round did not return a JSON object (got {type(raw_synthesis).__name__})")
    result = validate_investigation_result(raw_synthesis, safe_ticker, called_tools, tool_results)
    result.rejected_tool_selections = rejected_selection
    result.transport = transport
    return result


__all__ = ["investigate", "NoValidToolsSelectedError", "MalformedAIResponse", "ValidationError"]
