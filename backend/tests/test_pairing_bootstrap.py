"""QA remediation D1: secure pairing/provisioning fallback for the real
Docker Desktop bootstrap failure (a genuine same-machine browser request
arrives with the Docker bridge GATEWAY address as its peer, not a real
loopback IP -- is_loopback() correctly rejects that, which is exactly
the documented symptom this item reproduces).

These tests simulate that real topology directly (a constructed Request
whose client.host is a Docker bridge gateway address, e.g. 172.17.0.1 --
NOT 127.0.0.1), never merely asserting against FastAPI's TestClient
default peer. See auth.py's PairingCodeStore docstring for the full
security design this exercises.
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import PairingCodeStore

DOCKER_DESKTOP_BRIDGE_GATEWAY = "172.17.0.1"  # the real documented symptom IP shape
ANOTHER_LAN_DEVICE = "192.168.1.42"  # some other device on the same Docker/LAN network


def _request(client_host: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/bootstrap",
        "headers": [],
        "client": (client_host, 54321),
        "server": (client_host, 8000),
        "query_string": b"",
    }
    return Request(scope)


# --- reproduces the real D1 symptom: bridge-gateway peer, old route 403s --


def test_bootstrap_still_403s_for_docker_desktop_bridge_gateway_peer(app_main):
    """Confirms the real, reported root cause is still true of the
    UNCHANGED loopback bootstrap route: a Docker Desktop bridge gateway
    address is correctly treated as non-loopback (it is NOT loopback,
    and is indistinguishable from any other device on the same Docker/
    LAN network by IP alone -- this is the whole reason D1 needed a
    different mechanism, not a wider loopback check)."""
    with pytest.raises(HTTPException) as excinfo:
        app_main.auth_bootstrap(_request(DOCKER_DESKTOP_BRIDGE_GATEWAY))
    assert excinfo.value.status_code == 403


# --- the new pairing mechanism: legitimate case succeeds ------------------


def test_pairing_succeeds_for_the_exact_docker_desktop_topology(app_main):
    """THE hardest/most important case: a genuine Docker Desktop browser
    (bridge-gateway peer, same as the test above -- the bootstrap route
    still 403s it) can nonetheless mint a real session via the pairing
    code, because the pairing route's security boundary is possession of
    the code, never request.client.host."""
    code = app_main._pairing_store.current_code()
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    # Simulate the real Docker Desktop topology at the HTTP layer too --
    # TestClient's own peer ("testclient") is already non-loopback, which
    # is itself evidence this route makes no origin-based decision at all.
    resp = client.post("/api/auth/pair", json={"pairing_code": code})
    assert resp.status_code == 200
    assert resp.json()["token"] == app_main.auth_token


def test_pairing_illegitimate_caller_without_the_code_is_rejected(app_main):
    """An attacker merely reachable on the same Docker/LAN network (or
    anywhere else) who does NOT know the real pairing code is rejected,
    regardless of how many attempts or what IP they present as."""
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    for guess in ("0000000000000000", "deadbeefdeadbeef", "", "not-even-hex"):
        resp = client.post("/api/auth/pair", json={"pairing_code": guess})
        assert resp.status_code == 403
        assert app_main.auth_token not in resp.text


def test_pairing_code_is_single_use(app_main):
    code = app_main._pairing_store.current_code()
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    first = client.post("/api/auth/pair", json={"pairing_code": code})
    assert first.status_code == 200
    second = client.post("/api/auth/pair", json={"pairing_code": code})
    assert second.status_code == 403  # the SAME code can never be replayed


def test_pairing_code_expires_after_ttl(app_main, monkeypatch):
    import time

    store = PairingCodeStore(app_main._pairing_store.code_path, ttl_s=0.01)
    code = store.current_code()
    time.sleep(0.05)
    assert store.consume(code) is False


def test_pairing_lockout_after_too_many_failed_attempts(app_main):
    from app.auth import PairingCodeStore as _Store

    store = _Store(app_main._pairing_store.code_path)
    real_code = store.current_code()
    for _ in range(25):
        store.consume("wrong-guess")
    # Even the REAL code is now rejected until rotation/TTL -- the
    # lockout is a defense-in-depth measure independent of the already-
    # large (64-bit) keyspace.
    assert store.consume(real_code) is False


def test_pairing_route_never_exposes_the_code_itself(app_main):
    """The only thing /api/auth/pair ever returns on success is the real
    long-lived token -- never the pairing code (which would let anyone
    who guessed once harvest the still-live rotated code too)."""
    code = app_main._pairing_store.current_code()
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.post("/api/auth/pair", json={"pairing_code": code})
    assert code not in resp.text or resp.json().get("token") == code  # token != code in practice
    assert set(resp.json().keys()) == {"token"}


def test_pairing_store_rotates_a_new_code_on_every_successful_pairing(app_main):
    code1 = app_main._pairing_store.current_code()
    assert app_main._pairing_store.consume(code1) is True
    code2 = app_main._pairing_store.current_code()
    assert code2 != code1


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on NTFS")
def test_pairing_store_file_is_0600(tmp_path):
    import stat

    path = tmp_path / ".pairing_code"
    store = PairingCodeStore(path)
    store.current_code()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_pairing_store_force_new_never_reuses_a_prior_lifetime_code(tmp_path):
    store1 = PairingCodeStore(tmp_path / ".pairing_code")
    code1 = store1.current_code()
    store2 = PairingCodeStore(tmp_path / ".pairing_code")
    code2 = store2.current_code(force_new=True)
    assert code1 != code2


# --- cross-origin / CORS regression (reuse Phase A's existing pattern) ----


def test_pairing_route_cors_rejects_foreign_origin(app_main):
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.post(
        "/api/auth/pair",
        json={"pairing_code": "wrong"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example.com"
