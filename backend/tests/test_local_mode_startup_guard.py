"""Refinement #3(a): TIP_LOCAL_MODE explicitly requested against a
non-loopback BIND_HOST must fail startup loud, never silently run.

Same direct-unit-test pattern as test_single_process_guard.py (that
module's own analogous fail-closed startup guard) -- calls the pure
function directly rather than depending on FastAPI TestClient's
lifespan-event quirks, which is the standard, already-established way
this repo tests a startup guard.
"""
from __future__ import annotations

import pytest

from app.core.local_mode import (
    LocalModeMisconfiguration,
    assert_local_mode_configuration,
    effective_local_mode,
)


# --- assert_local_mode_configuration: the hard startup-failure case ------


def test_explicit_local_mode_against_non_loopback_bind_host_fails_startup_loud():
    with pytest.raises(LocalModeMisconfiguration) as excinfo:
        assert_local_mode_configuration(bind_host="10.10.10.2", tip_local_mode_raw="1")
    assert "10.10.10.2" in str(excinfo.value)
    assert "Refusing to start" in str(excinfo.value)


@pytest.mark.parametrize("truthy", ["1", "true", "True", "YES", "on"])
def test_every_truthy_spelling_of_explicit_local_mode_fails_against_non_loopback(truthy):
    with pytest.raises(LocalModeMisconfiguration):
        assert_local_mode_configuration(bind_host="0.0.0.0", tip_local_mode_raw=truthy)


def test_explicit_local_mode_against_loopback_bind_host_does_not_raise():
    for host in ("127.0.0.1", "::1", "localhost"):
        assert_local_mode_configuration(bind_host=host, tip_local_mode_raw="1") is None


def test_auto_against_non_loopback_bind_host_does_not_raise():
    """The non-loopback+auto case is just Network Mode, completely
    unchanged -- never a startup failure, only the explicit-true case
    is."""
    assert_local_mode_configuration(bind_host="203.0.113.5", tip_local_mode_raw=None) is None
    assert_local_mode_configuration(bind_host="203.0.113.5", tip_local_mode_raw="auto") is None


def test_explicit_false_against_non_loopback_bind_host_does_not_raise():
    assert_local_mode_configuration(bind_host="203.0.113.5", tip_local_mode_raw="0") is None


# --- effective_local_mode: the trust-decision pure function ---------------


def test_effective_local_mode_auto_on_loopback_is_true():
    assert effective_local_mode("127.0.0.1", None) is True
    assert effective_local_mode("localhost", "auto") is True
    assert effective_local_mode("::1", "") is True


def test_effective_local_mode_auto_on_non_loopback_is_false():
    assert effective_local_mode("10.10.10.2", None) is False
    assert effective_local_mode("0.0.0.0", "auto") is False


def test_effective_local_mode_explicit_false_always_off():
    assert effective_local_mode("127.0.0.1", "0") is False
    assert effective_local_mode("10.10.10.2", "false") is False


def test_effective_local_mode_explicit_true_on_loopback_is_true():
    assert effective_local_mode("127.0.0.1", "1") is True


def test_effective_local_mode_unrecognized_value_treated_as_auto():
    """A typo (e.g. TIP_LOCAL_MODE=maybe) must never crash the pure
    function or silently grant Local Mode against a non-loopback host
    -- it falls back to the same 'auto' behavior as unset."""
    assert effective_local_mode("10.10.10.2", "maybe") is False
    assert effective_local_mode("127.0.0.1", "maybe") is True
    assert_local_mode_configuration(bind_host="10.10.10.2", tip_local_mode_raw="maybe") is None
