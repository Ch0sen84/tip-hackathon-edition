import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def no_gap_policy(monkeypatch):
    """For pagination tests whose CONSTRUCTED fixtures cover only part of a day: the coverage
    plausibility checks (core/coverage_policy gap + empty-session) would rightly withhold
    certification once. Tests of those checks do not use this fixture."""
    from app.core import coverage_policy
    monkeypatch.setattr(coverage_policy, "GAP_EXPECTED_ROWS", float("inf"))
    monkeypatch.setattr(coverage_policy, "EMPTY_SESSION_MIN_MEDIAN_ROWS", float("inf"))


CERTIFIED_PROVENANCE = None


def certified_provenance():
    """Provenance for a 'complete' coverage row written directly by a test that models a session
    certified by the CURRENT collector (core/coverage_policy)."""
    from app.core import coverage_policy
    return {"certified_by": coverage_policy.CERTIFIER, "certified_at_utc": "2026-10-20T00:00:00+00:00"}

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("TIP_MODE", "demo")
# SAFETY CRITICAL (#38, 2026-10-15 round 2): this MUST be a forced
# override, never os.environ.setdefault. A shell/container that already
# exports TIP_DB_PATH (e.g. pointed at a real deployed install) would
# otherwise win over this test suite's own isolation, which is exactly the
# bug that let pytest previously write synthetic test records into a real
# deployed SQLite volume. The test suite's own fixture setup must always
# take priority over any inherited env var -- ":memory:" is used here
# (every test using the default db_path gets a private, fully in-process
# DB) with app/storage.py's assert_safe_test_db_path() as a hard fail-
# closed backstop for any test that constructs a Storage with its own
# explicit path instead of the settings default (see
# tests/test_db_isolation.py for the regression coverage).
os.environ["TIP_DB_PATH"] = ":memory:"

# Phase A: EVERY existing test in this suite constructs
# `TestClient(app_main.app)` directly (183+ call sites across many files)
# and was written before any route required authentication. Rather than
# invasively edit every one of those call sites, this patches
# fastapi.testclient.TestClient ONCE, here, at collection time (before any
# test module's own `from fastapi.testclient import TestClient` runs) so
# every TestClient instance automatically carries a valid X-TIP-Auth
# header for whichever app_main fixture instance is currently active (see
# the app_main fixture above, which sets `_main.auth_token` to that
# test's own isolated token). Tests that need to prove UNauthenticated
# access is rejected (test_auth.py) explicitly pop this header back off
# before making the request under test -- this default only ever makes
# the OTHER ~300 pre-existing tests keep passing unmodified; it never
# hides a real auth bug from the new negative tests, which construct
# their requests with the header deliberately removed.
import fastapi.testclient as _fastapi_testclient_mod

_RealTestClient = _fastapi_testclient_mod.TestClient


class _AutoAuthTestClient(_RealTestClient):  # pragma: no cover -- exercised via every test
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from app import main as _m

            # Only attach a header when a test has actually set this up
            # (via the app_main fixture) -- never implicitly creates/reads
            # a real on-disk token file for a test that doesn't use that
            # fixture at all (e.g. a GET-only test constructing
            # `TestClient(app)` directly). Those tests never hit a
            # mutating route, so having no header is harmless; this just
            # avoids ever touching a real default-path token file as a
            # side effect of merely constructing a TestClient.
            token = getattr(_m, "auth_token", None)
            if token:
                self.headers["X-TIP-Auth"] = token
        except Exception:
            pass  # app.main not importable yet -- leave client as-is


_fastapi_testclient_mod.TestClient = _AutoAuthTestClient


@pytest.fixture
def app_main(tmp_path, monkeypatch):
    """Isolated app.main module state for settings/credential tests: a
    fresh per-test settings.json path, no leftover UW/AI key, no leftover
    adapter cache, and public_demo reset to False. Prevents one test's
    saved key/mode from leaking into the next test.

    Phase A: also gives each test its own isolated auth-token file (never
    the real deployed one) via a fresh AuthTokenStore pointed at this
    test's own tmp_path -- see app/auth.py. `app_main.auth_token` exposes
    the real current token so tests can attach it as X-TIP-Auth without
    ever going through the loopback-only bootstrap route (TestClient's
    simulated peer is "testclient", not a real loopback address -- see
    test_auth.py for why that 403 case is tested directly instead)."""
    from app import main as _main
    from app.auth import AuthTokenStore
    from app.settings_store import SettingsStore

    _main._settings_store = SettingsStore(tmp_path / "settings.json")
    _main._adapter = None
    _main._adapter_sig = None
    _main._last_uw_test_result = None
    _main._auth_store = AuthTokenStore(tmp_path / ".auth_token")
    # QA remediation D1: same isolation for the new pairing-code store --
    # never the real deployed code/file, a fresh one per test.
    from app.auth import PairingCodeStore

    _main._pairing_store = PairingCodeStore(tmp_path / ".pairing_code")
    # Phase D: reset the scheduler runner + its lease/mode store too, and
    # the shared UW concurrency gate, so one test's scheduler mode/lease
    # state (and in-flight counters) can never leak into the next test.
    _main._catalog = None
    _main._store = None
    _main._demo_store = None
    # the reusable Live HTTP client (and its base URL / circuit-breaker
    # state) must not leak from one test into the next
    _main._last_live_client = None
    _main._last_live_client_key_sig = None
    _main._reset_scheduler_runner()
    from app.core import uw_concurrency as _uw_concurrency
    _uw_concurrency.reset_shared_gate_for_tests()
    monkeypatch.setattr(_main.settings, "public_demo", False)
    monkeypatch.setattr(_main.settings, "uw_api_key", "")
    # Never the public UW host: any route that makes a real UW request
    # (e.g. POST /api/settings/uw-key/test) hits a dead loopback port.
    monkeypatch.setattr(_main.settings, "uw_base_url", "http://127.0.0.1:9")
    _main.auth_token = _main._auth_store.get_or_create()
    yield _main


# --- Network guard (signal-integrity remediation) -----------------------
#
# The suite must never reach Unusual Whales, an LLM provider, or any other
# external host by accident (the 05c47ca audit found a test path that could
# send a real request to api.unusualwhales.com). Every socket connect / DNS
# lookup to a non-loopback destination raises instead. In-process
# TestClient traffic never opens a socket, and loopback servers (e.g. the
# recorded-data replay server used by the Live-mode end-to-end tests) stay
# allowed. Set TIP_ALLOW_LIVE_NETWORK_TESTS=1 to opt in deliberately.
import socket as _socket

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "testserver", "testclient"}


class ExternalNetworkBlocked(RuntimeError):
    pass


def _host_of(address):
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return str(address)


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    if os.environ.get("TIP_ALLOW_LIVE_NETWORK_TESTS") == "1":
        yield
        return
    real_connect = _socket.socket.connect
    real_connect_ex = _socket.socket.connect_ex
    real_getaddrinfo = _socket.getaddrinfo

    def guarded_connect(self, address):
        if self.family != getattr(_socket, "AF_UNIX", object()) and not _is_loopback_host(_host_of(address)):
            raise ExternalNetworkBlocked(f"test attempted an external connection to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if self.family != getattr(_socket, "AF_UNIX", object()) and not _is_loopback_host(_host_of(address)):
            raise ExternalNetworkBlocked(f"test attempted an external connection to {address!r}")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback_host(str(host)):
            raise ExternalNetworkBlocked(f"test attempted an external DNS lookup for {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(_socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(_socket, "getaddrinfo", guarded_getaddrinfo)
    yield


# --- Background job-thread isolation (Release 3 test-harness hardening) ---
# A baseline-job runner thread that outlives its test resolves the module
# globals get_catalog()/get_adapter() on its next step and would write into
# the NEXT test's database through that test's provider fake (reproduced
# deterministically). The `live` fixture stops its job threads at teardown;
# this guard turns any remaining leak into an explicit failure of the test
# that would otherwise be silently contaminated.
@pytest.fixture(autouse=True)
def _no_leaked_baseline_job_threads():
    import threading as _threading
    leaked = [t.name for t in _threading.enumerate() if t.name.startswith("baseline-job-") and t.is_alive()]
    assert not leaked, f"baseline job thread(s) leaked from an earlier test: {leaked}"
    yield
