"""Windows Docker Desktop QA round 2 -- finding B2 (SECURITY-CRITICAL):
BIND_HOST=0.0.0.0 must genuinely turn off Local Mode's zero-token bypass.

This file proves the DECISION LOGIC end-to-end (effective_local_mode +
the real require_auth dependency, forced to the state a genuine
BIND_HOST=0.0.0.0 deployment would be in). What this file does NOT, and
cannot, prove by itself is that docker-compose.yml actually forwards a
real operator's BIND_HOST=0.0.0.0 into the container in the first place
-- that structural wiring is covered separately by
test_compose_config_rendering.py (T2), which renders the real
docker-compose.yml with BIND_HOST=0.0.0.0 and asserts the forwarded
container environment reflects it. Together, the two files are the
regression pair that would have caught B2 before it ever reached real
Windows QA: one proves the app's logic is correct GIVEN a real
BIND_HOST value, the other proves the real file actually produces that
value.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.local_mode import LocalModeMisconfiguration, effective_local_mode


def test_effective_local_mode_is_false_when_bind_host_is_0_0_0_0_auto():
    """The zero-config default (TIP_LOCAL_MODE unset/"auto") against a
    LAN-exposed BIND_HOST must resolve to Network Mode, never Local
    Mode's zero-token bypass."""
    assert effective_local_mode("0.0.0.0", None) is False
    assert effective_local_mode("0.0.0.0", "auto") is False


def test_explicit_local_mode_true_against_0_0_0_0_fails_startup_loud():
    """An operator who ALSO explicitly forces TIP_LOCAL_MODE=1 on top of
    a LAN-exposed BIND_HOST gets a hard startup failure, never a silent
    LAN-exposed zero-token bypass."""
    from app.core.local_mode import assert_local_mode_configuration

    with pytest.raises(LocalModeMisconfiguration) as excinfo:
        assert_local_mode_configuration(bind_host="0.0.0.0", tip_local_mode_raw="1")
    assert "0.0.0.0" in str(excinfo.value)


def test_lan_exposed_deployment_requires_token_end_to_end(app_main, monkeypatch):
    """The exact gap B2 describes, proven through the REAL require_auth
    dependency every sensitive route uses: force the process into the
    state a genuine BIND_HOST=0.0.0.0 deployment is in (LOCAL_MODE_ACTIVE
    False, exactly what effective_local_mode("0.0.0.0", ...) computes),
    and confirm a same-origin-looking browser request with NO token is
    rejected -- i.e. the LAN-exposed Settings/credential-write endpoints
    genuinely cannot be reached with zero authentication."""
    assert effective_local_mode("0.0.0.0", None) is False
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    from app.main import ALLOWED_ORIGIN

    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "same-origin", "Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 401, (
        "a LAN-exposed (BIND_HOST=0.0.0.0) deployment must require the Network Mode "
        "token for every sensitive route -- B2 regression if this ever passes with "
        "zero authentication"
    )


# --- the new narrow self-consistency guard (allowed_host_override) -----


def test_loopback_bind_host_with_non_loopback_allowed_host_override_fails_startup_loud():
    """Narrow, genuinely-detectable contradiction: BIND_HOST says
    loopback (Local Mode would activate) but an explicit ALLOWED_HOST
    override points somewhere non-loopback. Both are operator-supplied
    env vars visible to this process -- refuse to silently pick one."""
    from app.core.local_mode import assert_local_mode_configuration

    with pytest.raises(LocalModeMisconfiguration) as excinfo:
        assert_local_mode_configuration(
            bind_host="127.0.0.1", tip_local_mode_raw=None, allowed_host_override="10.10.10.2"
        )
    assert "ALLOWED_HOST" in str(excinfo.value)
    assert "10.10.10.2" in str(excinfo.value)


def test_loopback_bind_host_with_no_allowed_host_override_does_not_raise():
    """The default, zero-config case (ALLOWED_HOST never set explicitly,
    main.py passes None) must stay a complete no-op."""
    from app.core.local_mode import assert_local_mode_configuration

    assert_local_mode_configuration(bind_host="127.0.0.1", tip_local_mode_raw=None, allowed_host_override=None) is None


def test_loopback_bind_host_with_matching_loopback_allowed_host_override_does_not_raise():
    from app.core.local_mode import assert_local_mode_configuration

    assert_local_mode_configuration(
        bind_host="127.0.0.1", tip_local_mode_raw=None, allowed_host_override="localhost"
    ) is None


def test_non_loopback_bind_host_with_non_loopback_allowed_host_override_does_not_raise():
    """Both already agree on Network Mode -- no contradiction, no raise."""
    from app.core.local_mode import assert_local_mode_configuration

    assert_local_mode_configuration(
        bind_host="0.0.0.0", tip_local_mode_raw=None, allowed_host_override="10.10.10.2"
    ) is None
