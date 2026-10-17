"""UX remediation item 4: after an explicit ticker refresh completes,
immediately show the actual latest evaluation timestamp -- fix the
contradictory simultaneous "Current" and "Not yet evaluated" labels bug.

Real finding: GET/POST /api/ticker/{ticker}(/refresh) hardcoded
`last_evaluated_at = None` on every real live fetch, while simultaneously
reporting `eval_freshness = "current"`. The frontend renders a "Current"
freshness tag next to a "Not yet evaluated" text line driven by
last_evaluated_at being falsy -- a plainly contradictory pair of labels
on the exact same response, visible to a real user immediately after
clicking Refresh.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter


def _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist):
    mock_session = MagicMock()
    adapter = LiveUWAdapter(api_key="fake-key", base_url="https://api.unusualwhales.com",
                             client_api_id="fake", watchlist=list(watchlist))
    adapter.client.session = mock_session
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter, mock_session


def test_explicit_refresh_never_returns_current_with_null_last_evaluated_at(app_main, monkeypatch):
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    client = TestClient(app_main.app)
    resp = client.post(f"/api/ticker/{ticker}/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["eval_freshness"] == "current"
    assert body["last_evaluated_at"] is not None, (
        "A 'current' freshness response had last_evaluated_at=None -- this is exactly the "
        "contradictory 'Current' + 'Not yet evaluated' labels bug."
    )


def test_immediately_following_cache_only_get_reflects_the_real_refresh_timestamp(app_main, monkeypatch):
    """A plain GET right after a real refresh must read back a
    consistent, real timestamp -- not regress to None either."""
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    client = TestClient(app_main.app)
    refresh_resp = client.post(f"/api/ticker/{ticker}/refresh")
    assert refresh_resp.status_code == 200

    get_resp = client.get(f"/api/ticker/{ticker}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["served_from_cache"] is True
    assert body["last_evaluated_at"] is not None
