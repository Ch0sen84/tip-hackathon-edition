"""LiveUWAdapter tests use a mocked requests.Session -- no real network
calls to the UW API are ever made here."""
from unittest.mock import MagicMock

from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient


def _mock_session(status_code, json_body=None, text_body=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text_body
    resp.json.return_value = json_body if json_body is not None else {}
    session = MagicMock()
    session.get.return_value = resp
    return session


def test_successful_call_returns_parsed_json():
    session = _mock_session(200, {"data": [{"ticker": "SPY"}]})
    client = UWRestClient("fake-key", session=session)
    body = client.stock_state("SPY")
    assert body == {"data": [{"ticker": "SPY"}]}
    session.get.assert_called_once()


def test_401_returns_unauthorized_structure_not_exception():
    session = _mock_session(401, text_body='{"code":"invalid_key"}')
    client = UWRestClient("bad-key", session=session)
    body = client.stock_info("SPY")
    assert body["status"] == "unauthorized"
    assert body["status_code"] == 401
    assert "ok" in body and body["ok"] is False


def test_403_returns_unavailable_on_tier_structure_not_exception():
    session = _mock_session(403, text_body='{"code":"futures_access_required"}')
    client = UWRestClient("fake-key", session=session)
    body = client.get("/api/futures/contracts")
    assert body["status"] == "unavailable_on_tier"
    assert body["status_code"] == 403


def test_missing_api_key_never_makes_a_request():
    session = MagicMock()
    client = UWRestClient("", session=session)
    body = client.stock_state("SPY")
    assert body["status"] == "unauthorized"
    session.get.assert_not_called()


def test_network_exception_is_caught_not_raised():
    import requests
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("boom")
    client = UWRestClient("fake-key", session=session)
    body = client.stock_state("SPY")
    assert body["status"] == "error"


def test_adapter_get_flow_alerts_returns_empty_list_on_403_no_crash():
    session = _mock_session(403, text_body="forbidden")
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    alerts = adapter.get_flow_alerts("SPY")
    assert alerts == []


def test_adapter_get_darkpool_trades_returns_empty_list_on_401_no_crash():
    session = _mock_session(401, text_body="unauthorized")
    client = UWRestClient("bad-key", session=session)
    adapter = LiveUWAdapter(api_key="bad-key", watchlist=["SPY"], client=client)
    trades = adapter.get_darkpool_trades("SPY")
    assert trades == []


def test_adapter_get_stock_state_marks_unavailable_without_crashing():
    session = _mock_session(403, text_body="forbidden")
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    state = adapter.get_stock_state("SPY")
    assert state["unavailable"] is True


def test_adapter_get_stock_state_returns_data_on_success():
    session = _mock_session(200, {"data": {"last_price": 450.1, "change_percent": 1.0}})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    state = adapter.get_stock_state("SPY")
    assert state["last_price"] == 450.1


def test_adapter_universe_is_the_configured_watchlist():
    adapter = LiveUWAdapter(api_key="k", watchlist=["spy", "qqq"], client=MagicMock())
    assert adapter.get_universe() == ["SPY", "QQQ"]


def test_health_reports_probe_failure_without_crashing():
    session = _mock_session(401, text_body="unauthorized")
    client = UWRestClient("bad-key", session=session)
    adapter = LiveUWAdapter(api_key="bad-key", watchlist=["SPY"], client=client)
    h = adapter.health()
    assert h["probe_ok"] is False
    assert h["api_key_configured"] is True
