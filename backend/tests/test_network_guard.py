"""The suite-wide network guard (conftest._block_external_network) must
refuse external hosts while leaving loopback usable."""
import socket

import pytest
import requests

from tests.conftest import ExternalNetworkBlocked


def test_external_http_request_is_blocked():
    with pytest.raises((ExternalNetworkBlocked, requests.exceptions.ConnectionError)) as exc:
        requests.get("https://api.unusualwhales.com/api/stock/SPY/info", timeout=2)
    assert "external" in str(exc.value).lower()


def test_external_dns_lookup_is_blocked():
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("api.anthropic.com", 443)


def test_loopback_socket_still_allowed():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        cli = socket.create_connection(srv.getsockname(), timeout=2)
        cli.close()
    finally:
        srv.close()


def test_settings_disclose_whether_uw_base_url_is_the_public_default(app_main, monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(app_main.app)
    monkeypatch.setattr(app_main.settings, "uw_base_url", "https://api.unusualwhales.com")
    assert client.get("/api/settings").json()["uw"]["base_url_is_default"] is True
    monkeypatch.setattr(app_main.settings, "uw_base_url", "http://127.0.0.1:9")
    assert client.get("/api/settings").json()["uw"]["base_url_is_default"] is False
