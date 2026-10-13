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
        return _mock_response({"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    llm_provider.AnthropicProvider().complete("sys", "user", "fake-key")
    assert captured["json"]["max_tokens"] == 1500
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
