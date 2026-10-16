"""Windows Docker Desktop QA round 2 -- findings B1 (custom-port Origin
mismatch) and B3 (localhost vs 127.0.0.1 Origin mismatch).

Tests the pure `compute_allowed_origins()` helper directly (same
established pattern as test_local_mode_startup_guard.py calling
`effective_local_mode`/`assert_local_mode_configuration` directly) plus
one end-to-end TestClient proof for B3 that the real require_auth
dependency genuinely accepts both loopback spellings on the SAME
configured port.

B1 (custom BIND_PORT) cannot be proven end-to-end inside this same
pytest process, because ALLOWED_HOST/_BIND_PORT/_ALLOWED_ORIGINS are
computed once at `app.main` import time from the process environment --
see test_compose_config_rendering.py for the regression coverage that a
custom BIND_PORT is genuinely forwarded into that environment in the
first place, and FINAL_REPORT.md / the task's real-browser section for
the real-port end-to-end proof (a fresh process bound to a real custom
port, hit by a real browser).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.local_mode import compute_allowed_origins


# --- B1: the computed origin set must use the REAL configured port, not
# a hardcoded 8000 -----------------------------------------------------


def test_custom_port_is_reflected_in_the_computed_origin_set():
    origins = compute_allowed_origins("127.0.0.1", "18000")
    assert "http://127.0.0.1:18000" in origins
    assert "http://127.0.0.1:8000" not in origins, "must not silently fall back to port 8000"


def test_default_port_still_works_unchanged():
    origins = compute_allowed_origins("127.0.0.1", "8000")
    assert "http://127.0.0.1:8000" in origins


# --- B3: both loopback spellings must be accepted on the SAME port -----


def test_localhost_and_127_0_0_1_both_accepted_on_the_same_custom_port():
    origins = compute_allowed_origins("127.0.0.1", "18000")
    assert set(origins) == {"http://127.0.0.1:18000", "http://localhost:18000"}


def test_localhost_as_the_configured_allowed_host_widens_identically():
    """ALLOWED_HOST defaults to BIND_HOST, which could itself be the
    string 'localhost' (an operator's .env literally says
    BIND_HOST=localhost) -- must widen exactly the same as 127.0.0.1
    does, never treat the two loopback spellings asymmetrically."""
    origins = compute_allowed_origins("localhost", "8000")
    assert set(origins) == {"http://127.0.0.1:8000", "http://localhost:8000"}


# --- explicit overrides / non-loopback hosts must NOT be widened -------


def test_explicit_allowed_origin_override_is_not_widened():
    origins = compute_allowed_origins("proxy.example.com", "443", allowed_origin_override="https://proxy.example.com")
    assert origins == ["https://proxy.example.com"]


def test_non_loopback_allowed_host_is_not_widened():
    origins = compute_allowed_origins("10.10.10.2", "8000")
    assert origins == ["http://10.10.10.2:8000"]


def test_computed_origins_never_contain_a_wildcard():
    for host, port in [("127.0.0.1", "8000"), ("localhost", "18000"), ("10.10.10.2", "8000")]:
        for o in compute_allowed_origins(host, port):
            assert o != "*"
            assert "*" not in o


# --- end-to-end proof: the real require_auth dependency accepts BOTH
# loopback spellings for the process's actual configured port ----------


def test_require_auth_end_to_end_accepts_both_loopback_origin_spellings(app_main):
    """Proves the wiring, not just the pure helper: a real mutating
    request through the real app with Origin=http://localhost:<port>
    must be treated identically to Origin=http://127.0.0.1:<port> for
    whatever port this test process's app.main actually computed (the
    default 8000 under pytest's own env -- see conftest.py)."""
    from app.main import _BIND_PORT

    localhost_origin = f"http://localhost:{_BIND_PORT}"
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "same-origin", "Origin": localhost_origin},
    )
    assert resp.status_code == 200, (
        f"http://localhost:{_BIND_PORT} Origin was rejected even though "
        "TrustedHostMiddleware already accepts the 'localhost' Host alias -- "
        "B3 regression"
    )
