"""Pins the exact Anthropic/OpenAI model IDs and request bounds used by
llm_provider.py, and guards the cost-bounding fix (OpenAI previously had NO
output-token cap at all). No real network call is ever made -- requests.post
is monkeypatched.

Verified (not assumed) against each provider's own docs on 2026-10-13:
  - platform.claude.com/docs/en/models/overview: claude-sonnet-5-5 is the
    current, non-deprecated "efficient model for coding and agents" ID.
  - developers.openai.com/api/docs/models/gpt-5: gpt-5 is explicitly marked
    Deprecated there; gpt-6-astra is the current recommended replacement.

If either provider retires these IDs in the future, this test's hardcoded
expected strings will fail loudly instead of the app silently calling a
dead model -- that's the point of pinning them here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.ai import llm_provider


def _mock_response(json_body):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_body
    return resp


def test_anthropic_model_id_is_current_and_not_a_deprecated_string():
    assert llm_provider.AnthropicProvider.model == "claude-sonnet-5-5"


def test_openai_model_id_is_current_and_not_the_deprecated_gpt5():
    assert llm_provider.OpenAIProvider.model == "gpt-6-astra"
    assert llm_provider.OpenAIProvider.model != "gpt-5"


def test_anthropic_request_has_a_max_tokens_bound(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _mock_response({"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
    # Raised 1500 -> 3000 on 2026-10-14 (see root-cause section below) --
    # still a bound, still cheap, just no longer the value that was too
    # tight once Sonnet 5.5's default adaptive thinking is accounted for.
    assert captured["json"]["max_tokens"] == 3000
    assert captured["json"]["model"] == "claude-sonnet-5-5"


def test_openai_request_now_has_a_max_tokens_bound_regression_guard(monkeypatch):
    """Regression guard: the OpenAI path previously sent NO token cap at
    all, a real unbounded-cost gap for a feature whose entire design goal
    is a tightly bounded call count. This must never silently come back."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _mock_response({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    llm_provider.OpenAIProvider().complete("sys", "user", "fake-key")
    assert "max_tokens" in captured["json"], "OpenAI request must set an output-token cap"
    assert captured["json"]["max_tokens"] == 1500
    assert captured["json"]["model"] == "gpt-6-astra"


# --- Root-cause fix for the real NVDA investigation failure ------------
# ("LLM response was not valid JSON: Unterminated string starting at line
# 1 column 3404") -- Anthropic's Sonnet 5.5 has adaptive thinking ON by
# default, and max_tokens is a hard ceiling over thinking + visible text
# combined, so a verbose synthesis response could get cut off mid-JSON.


def test_anthropic_request_sets_between_tools_thinking_mode(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _mock_response({
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "content": [{"type": "text", "text": "{}"}],
        })

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
    assert captured["json"]["thinking"] == {"type": "between_tools"}
    assert captured["json"]["max_tokens"] == 3000


def test_anthropic_stop_reason_max_tokens_raises_specific_truncation_error(monkeypatch):
    """This is the exact signature the real failed investigation should
    have surfaced: a response that DID come back HTTP-ok, DID contain
    some text, but was cut off by the token limit -- must raise
    LLMTruncatedOutputError, not a generic/ambiguous LLMCallError."""

    def fake_post(url, headers=None, json=None, timeout=None):
        return _mock_response({
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 500, "output_tokens": 3000},
            "content": [{"type": "text", "text": '{"ai_interpretation": "cut off mid-stri'}],
        })

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    try:
        llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
        assert False, "expected LLMTruncatedOutputError"
    except llm_provider.LLMTruncatedOutputError as e:
        assert "max_tokens" in str(e) or "cut off" in str(e)
    except llm_provider.LLMCallError:
        assert False, "must raise the specific LLMTruncatedOutputError subclass, not the generic base class"


def test_anthropic_normal_completion_does_not_raise_truncation_error(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _mock_response({
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "content": [{"type": "text", "text": '{"ai_interpretation": "complete"}'}],
        })

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    text = llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
    assert text == '{"ai_interpretation": "complete"}'


def test_anthropic_network_timeout_raises_llmcallerror_not_a_raw_exception(monkeypatch):
    """REAL BUG FIX regression guard (2026-10-14): the actual NVDA HTTP 500
    was requests.exceptions.ReadTimeout propagating completely unhandled
    out of complete() -- FastAPI turned that into a raw 500 instead of the
    structured LLMCallError -> HTTP 502 path every other failure mode
    already used. complete() must now catch requests.RequestException."""
    import requests as _requests

    def fake_post(*a, **k):
        raise _requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    try:
        llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
        assert False, "expected LLMCallError"
    except llm_provider.LLMCallError as e:
        assert "network error" in str(e) or "timed out" in str(e).lower()
    except Exception as e:  # pragma: no cover
        assert False, f"a raw {type(e).__name__} escaped complete() -- this is the exact original bug"


def test_openai_network_timeout_raises_llmcallerror_not_a_raw_exception(monkeypatch):
    import requests as _requests

    def fake_post(*a, **k):
        raise _requests.exceptions.ConnectionError("connection reset")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    try:
        llm_provider.OpenAIProvider().complete("sys", "user", "fake-key")
        assert False, "expected LLMCallError"
    except llm_provider.LLMCallError:
        pass
    except Exception as e:  # pragma: no cover
        assert False, f"a raw {type(e).__name__} escaped complete() -- this is the exact original bug"


def test_request_timeout_is_generous_enough_for_a_real_mcp_fed_synthesis_call():
    """Regression guard for the other half of the same real bug: 15s was
    too tight for a genuine MCP-executed, multi-tool-result synthesis call
    plus Sonnet 5.5's adaptive thinking overhead."""
    assert llm_provider.REQUEST_TIMEOUT_S >= 45


def test_anthropic_text_extraction_ignores_non_text_blocks():
    """Defensive check: a thinking-type content block (no "text" key, or a
    "text" key some other block type might coincidentally carry) must
    never be concatenated into the parsed output -- only type=="text"
    blocks are real visible output."""
    import types

    fake_body = {
        "stop_reason": "end_turn",
        "usage": {},
        "content": [
            {"type": "thinking", "thinking": "internal reasoning, not the answer"},
            {"type": "text", "text": "the real answer"},
        ],
    }
    resp = types.SimpleNamespace(ok=True, status_code=200, json=lambda: fake_body, text="")
    import unittest.mock as mock
    with mock.patch.object(llm_provider.requests, "post", return_value=resp):
        text = llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
    assert text == "the real answer"
    assert "internal reasoning" not in text
