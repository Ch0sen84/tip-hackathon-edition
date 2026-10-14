"""Minimal provider-agnostic LLM wrapper (simple strategy pattern) for the
two allowed reasoning rounds: tool selection (round 1) and synthesis
(round 2). Supports exactly the two providers Settings lets a user save a
key for -- Anthropic and OpenAI -- via plain REST calls (`requests`, same
library the rest of this repo already depends on; no new SDK dependency).

Each provider's `complete()` makes ONE HTTP call with a short timeout and
returns the raw text response. Callers (orchestrator.py) are responsible
for calling select_tools()/synthesize() exactly once each per
investigation -- this module does not loop.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

import requests

from .allowlist import ALLOWLIST, MAX_TOOLS_PER_INVESTIGATION

logger = logging.getLogger(__name__)

# ROOT-CAUSE FIX (2026-10-14, real NVDA HTTP 500 after enabling genuine
# MCP transport): the actual exception was
# urllib3.exceptions.ReadTimeoutError -> requests.exceptions.ReadTimeout
# at api.anthropic.com after exactly 15s -- NOT a recurrence of the
# earlier truncation bug. Two real problems, both fixed here:
#   1. 15s was too tight. A real synthesis call fed by actual MCP-executed
#      tool results (larger payload than any REST-fallback test used) plus
#      Sonnet 5.5's adaptive thinking overhead can legitimately take longer
#      than 15s to finish. Raised to 60s -- generous but still bounded
#      (this is one call, not a retry loop).
#   2. complete() had NO try/except around requests.post() at all, unlike
#      test_provider_connection() below which already correctly catches
#      requests.RequestException. A timeout (or any other connection
#      failure) therefore propagated as a raw unhandled exception -> FastAPI
#      500, instead of the structured LLMCallError -> HTTP 502 path every
#      other failure mode already uses. Fixed in both complete() methods.
REQUEST_TIMEOUT_S = 60


class LLMCallError(RuntimeError):
    pass


class LLMTruncatedOutputError(LLMCallError):
    """Raised specifically when the provider's own stop_reason confirms the
    response was cut off before finishing (Anthropic stop_reason ==
    "max_tokens", OpenAI finish_reason == "length") -- as opposed to any
    other parse failure. Callers can give a much more specific, actionable
    message for this case than a generic "not valid JSON"."""


class _Provider(Protocol):
    name: str
    url: str
    model: str

    def complete(self, system: str, user: str, api_key: str) -> str: ...


class AnthropicProvider:
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"
    # Verified against platform.claude.com/docs/en/models/overview on
    # 2026-10-13 (not assumed): claude-sonnet-5 is still a valid, undeprecated
    # model ID, but claude-sonnet-5-5 is the current generation at the same
    # "efficient model for coding and agents" tier -- $2/$10 per MTok
    # in/out, well inside this feature's 2-call bound. Pin it explicitly
    # here (not an alias) so behavior/pricing doesn't drift silently.
    model = "claude-sonnet-5-5"
    # ROOT-CAUSE FIX (2026-10-14, real NVDA investigation failure: "LLM
    # response was not valid JSON: Unterminated string..."): per Anthropic's
    # own migration guide for this model (platform.claude.com/docs/en/
    # models/sonnet-5-5/migration-guide, fetched 2026-10-13), Sonnet 5.5 has
    # ADAPTIVE THINKING ON BY DEFAULT, and `thinking: {"type": "disabled"}`
    # is explicitly documented to return a 400 error for this model -- the
    # vendor-supported way to minimize it is `{"type": "between_tools"}`.
    # Anthropic's `max_tokens` is a hard ceiling over the ENTIRE response
    # (thinking tokens + visible text), so leaving thinking fully adaptive
    # with no token headroom for it is the most likely explanation for a
    # JSON string getting cut off mid-way through a verbose synthesis
    # response: thinking consumed part of the 1500-token budget before the
    # model even started writing the user-visible JSON.
    thinking_mode = "between_tools"
    # Raised from 1500 -> 3000: still cheap ($2/$10 per MTok -> <=$0.03 for
    # this one call) but gives real headroom for a live, multi-tool-result
    # synthesis response (several evidence strings + an interpretation
    # paragraph) on top of whatever thinking tokens still occur. This is a
    # justified, bounded increase tied to the above root cause -- not an
    # unexamined bump.
    max_output_tokens = 3000

    def complete(self, system: str, user: str, api_key: str) -> str:
        try:
            resp = requests.post(
                self.url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": self.max_output_tokens,
                    "thinking": {"type": self.thinking_mode},
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            # FIX (2026-10-14): previously unhandled -- a timeout/connection
            # failure here used to propagate as a raw exception -> HTTP 500.
            raise LLMCallError(f"network error reaching Anthropic: {e}") from e
        if not resp.ok:
            raise LLMCallError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        # Diagnostic capture (2026-10-14 fix): previously NOTHING from this
        # response besides the text content was ever inspected, so a real
        # truncation failure could only be guessed at from the shape of the
        # resulting JSONDecodeError message, never confirmed. stop_reason
        # and usage contain no prompt/response content -- safe to log.
        stop_reason = body.get("stop_reason")
        usage = body.get("usage") or {}
        logger.info(
            "Anthropic call finished: stop_reason=%s input_tokens=%s output_tokens=%s",
            stop_reason, usage.get("input_tokens"), usage.get("output_tokens"),
        )
        parts = body.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
        if not text:
            raise LLMCallError("Anthropic response contained no text content")
        if stop_reason == "max_tokens":
            raise LLMTruncatedOutputError(
                f"Anthropic cut off its response at the {self.max_output_tokens}-token limit "
                f"before finishing (stop_reason=max_tokens) -- the output was truncated, not malformed."
            )
        return text


class OpenAIProvider:
    name = "openai"
    url = "https://api.openai.com/v1/chat/completions"
    # Verified against developers.openai.com/api/docs/models/gpt-5 on
    # 2026-10-13 (not assumed): gpt-5 is now explicitly marked Deprecated
    # there, with gpt-6-astra named as its replacement ($10/$50 per MTok
    # in/out -- still trivial for this feature's 2-call bound). Pin the
    # current, non-deprecated ID explicitly.
    model = "gpt-6-astra"
    # Mirrors AnthropicProvider's max_tokens=1500 bound -- previously this
    # request had NO output-token cap at all, a real cost/runaway-output
    # gap for a feature whose whole design goal is a tightly bounded call
    # count. chat/completions uses `max_tokens` (not the Responses API's
    # `max_output_tokens`).
    max_output_tokens = 1500

    def complete(self, system: str, user: str, api_key: str) -> str:
        try:
            resp = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": self.max_output_tokens,
                },
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            # FIX (2026-10-14): same missing-try/except bug as Anthropic's
            # complete() above -- previously unhandled, propagated as 500.
            raise LLMCallError(f"network error reaching OpenAI: {e}") from e
        if not resp.ok:
            raise LLMCallError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        choices = body.get("choices") or []
        if not choices:
            raise LLMCallError("OpenAI response contained no choices")
        text = choices[0].get("message", {}).get("content", "")
        if not text:
            raise LLMCallError("OpenAI response contained no message content")
        return text


PROVIDERS: dict[str, _Provider] = {
    "anthropic": AnthropicProvider(),
    "openai": OpenAIProvider(),
}


def _get_provider(provider: str) -> _Provider:
    p = PROVIDERS.get((provider or "").strip().lower())
    if p is None:
        raise LLMCallError(f"Unsupported AI provider '{provider}' (expected 'anthropic' or 'openai')")
    return p


def _parse_json_response(text: str) -> object:
    """LLM text responses sometimes wrap JSON in a ```json fence despite
    instructions not to -- strip that defensively before parsing. Never
    executes or evals anything; this is a pure JSON parse."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMCallError(f"LLM response was not valid JSON: {e}") from e


TOOL_SELECTION_SYSTEM_PROMPT = f"""You are a tool-selection assistant for a read-only market research tool.
You may select AT MOST {MAX_TOOLS_PER_INVESTIGATION} tools from this exact allowlist (no other tool exists):
{json.dumps(ALLOWLIST, indent=2)}

Respond with ONLY a JSON array (no prose, no markdown fence) of objects shaped:
[{{"tool": "<one of the allowlisted names>", "args": {{"ticker": "<TICKER>", "limit": <int>}}}}, ...]

Rules:
- Every "tool" value MUST be exactly one of the allowlisted names above.
- Every "ticker" value MUST be exactly the ticker you were given -- never a different symbol.
- Pick the smallest set of tools (1 to {MAX_TOOLS_PER_INVESTIGATION}) that would give a human analyst the most
  relevant read-only context for the ticker and evidence described. Do not select tools unrelated to the evidence.
- You are selecting tools only. You are not answering the question yet."""


def build_tool_selection_user_prompt(ticker: str, evidence: dict) -> str:
    return (
        f"Ticker: {ticker}\n"
        f"Market Radar evidence that triggered this investigation (already computed, read-only):\n"
        f"{json.dumps(evidence, indent=2, default=str)}\n\n"
        "Select which allowlisted tools (and args) would help a human analyst understand this ticker's "
        "current options/dark-pool/ownership picture. Respond with the JSON array only."
    )


SYNTHESIS_SYSTEM_PROMPT = """You are a research-synthesis assistant for a read-only market research tool.
You are given the results of a small number of read-only Unusual Whales data lookups for one ticker.

You MUST respond with ONLY a JSON object (no prose, no markdown fence) shaped exactly:
{
  "observed_facts": [{"fact": "<a single concrete, literal observation from the tool data>", "source_tool": "<exact tool name that produced it>"}, ...],
  "supporting_evidence": ["<short string tying 2+ observed facts together, still just description>", ...],
  "conflicting_observations": ["<short string noting where different tools/data points disagree or seem inconsistent>", ...],
  "limitations": ["<short string on what this data does NOT tell you -- sample size, time window, missing context, etc.>", ...],
  "ai_interpretation": "<your own reasoning / read on what this pattern of observations could mean, clearly framed as interpretation, not fact>"
}

Hard rules, no exceptions:
- "observed_facts" entries must be literal descriptions of what the tool data actually shows (numbers, flags,
  dates, counts). They must NEVER predict future price direction, NEVER state or imply what will happen next,
  and NEVER state or infer a specific trader's/institution's intent ("they are trying to...", "this suggests
  someone knows...") unless you are directly quoting a field the data itself provides (e.g. a flagged
  "is_call"/"has_sweep" boolean) -- inferred intent belongs in ai_interpretation, not observed_facts.
- Do NOT recommend any trade action (buy/sell/hold/short/etc.) anywhere in this response, including in
  ai_interpretation.
- Do NOT predict price direction or a price target anywhere in this response, including in ai_interpretation.
  ai_interpretation may describe what a pattern COULD indicate in general terms, but must not forecast an
  outcome or give a recommendation.
- Every "source_tool" value must be one of the tools that were actually called (listed in the user message).
- If the tool data is sparse, empty, or inconclusive, say so plainly in "limitations" -- do not invent facts."""


def build_synthesis_user_prompt(ticker: str, tool_results: list[dict]) -> str:
    called_tools = [r["tool"] for r in tool_results]
    return (
        f"Ticker: {ticker}\n"
        f"Tools actually called this investigation: {called_tools}\n"
        f"Raw tool results:\n{json.dumps(tool_results, indent=2, default=str)}\n\n"
        "Produce the JSON object described in the system prompt. Respond with the JSON object only."
    )


def test_provider_connection(provider: str, api_key: str) -> dict:
    """Minimal, bounded AI-provider connectivity test (Phase 4B): exactly
    ONE short call, tiny token cap, a trivial prompt with no tool-
    selection/synthesis system prompt at all -- NOT part of the 2-call
    investigation budget, and never called automatically; only from the
    explicit, user-clicked /api/settings/ai-key/test route (main.py),
    which itself requires confirm=true. A successful result here proves
    ONLY that the credential+model answer a request -- it is not evidence
    that MCP or any tool execution works; callers must not conflate the
    two (see /api/settings/mcp/test's separate, LLM-free test).
    Returns {"ok": bool, "model": str, "error": str|None}. Never raises --
    every failure mode (bad key, network, rate limit) comes back as
    ok=False with a message, same crash-proof contract as the rest of this
    module's provider calls.
    """
    p = _get_provider(provider)
    try:
        if p.name == "anthropic":
            resp = requests.post(
                p.url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": p.model,
                    "max_tokens": 16,
                    "thinking": {"type": "between_tools"},
                    "messages": [{"role": "user", "content": "Reply with exactly one word: OK"}],
                },
                timeout=REQUEST_TIMEOUT_S,
            )
        else:
            resp = requests.post(
                p.url,
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json={
                    "model": p.model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply with exactly one word: OK"}],
                },
                timeout=REQUEST_TIMEOUT_S,
            )
    except requests.RequestException as e:
        return {"ok": False, "model": p.model, "error": f"network error reaching {p.name}: {e}"}
    if not resp.ok:
        return {"ok": False, "model": p.model, "error": f"{p.name} API error {resp.status_code}: {resp.text[:200]}"}
    return {"ok": True, "model": p.model, "error": None}


def select_tools(ticker: str, evidence: dict, provider: str, api_key: str) -> object:
    """Round 1. Calls the provider exactly once. Returns the parsed JSON
    (validation.validate_tool_selection is responsible for checking its
    shape/contents -- this function only parses JSON, it does not trust
    the contents)."""
    p = _get_provider(provider)
    text = p.complete(TOOL_SELECTION_SYSTEM_PROMPT, build_tool_selection_user_prompt(ticker, evidence), api_key)
    return _parse_json_response(text)


def synthesize(ticker: str, tool_results: list[dict], provider: str, api_key: str) -> object:
    """Round 2, the one allowed additional reasoning round. Calls the
    provider exactly once."""
    p = _get_provider(provider)
    text = p.complete(SYNTHESIS_SYSTEM_PROMPT, build_synthesis_user_prompt(ticker, tool_results), api_key)
    return _parse_json_response(text)
