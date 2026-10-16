import os
import sys
from pathlib import Path

import pytest

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
    _main._reset_scheduler_runner()
    from app.core import uw_concurrency as _uw_concurrency
    _uw_concurrency.reset_shared_gate_for_tests()
    monkeypatch.setattr(_main.settings, "public_demo", False)
    monkeypatch.setattr(_main.settings, "uw_api_key", "")
    _main.auth_token = _main._auth_store.get_or_create()
    yield _main
