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

REQUEST_TIMEOUT_S = 15


class LLMCallError(RuntimeError):
    pass


class _Provider(Protocol):
    def complete(self, system: str, user: str, api_key: str) -> str: ...


class AnthropicProvider:
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"
    model = "claude-sonnet-5"

    def complete(self, system: str, user: str, api_key: str) -> str:
        resp = requests.post(
            self.url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1500,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=REQUEST_TIMEOUT_S,
        )
        if not resp.ok:
            raise LLMCallError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        parts = body.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text:
            raise LLMCallError("Anthropic response contained no text content")
        return text


class OpenAIProvider:
    name = "openai"
    url = "https://api.openai.com/v1/chat/completions"
    model = "gpt-5"

    def complete(self, system: str, user: str, api_key: str) -> str:
        resp = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
            },
            timeout=REQUEST_TIMEOUT_S,
        )
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
