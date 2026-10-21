"""Round-2 security review (S2-1..S2-7), converted from the reviewer's reproductions into regression
tests asserting the corrected behaviour; test_ok_* verify the round-1 fixes.

Audit round 2 (security) reproductions @ 339c84f -- private audit clone only, not for merge.

Each test asserts the CORRECT behaviour, so a FAIL means the defect is present. Tests prefixed
`test_ok_` verify a prior-round fix and are expected to PASS. No real provider request is made:
every provider-spending function is replaced by a recorder, and the UW base URL is a dead
loopback port (conftest)."""
import logging
import sys

import pytest
from fastapi.testclient import TestClient

FAKE_UW = "FAKE-UW-KEY-aaaa"
FAKE_AI = "FAKE-AI-KEY-bbbb"


def _client(m, **kw):
    c = TestClient(m.app, **kw)
    c.headers.pop("X-TIP-Auth", None)
    return c


def _authed(m, **kw):
    c = TestClient(m.app, **kw)
    c.headers["X-TIP-Auth"] = m.auth_token
    return c


@pytest.fixture
def live_m(app_main, monkeypatch, tmp_path):
    """Live mode with fake keys and a LiveUWAdapter pointed at a dead loopback port."""
    from app.adapters.live_uw_adapter import LiveUWAdapter

    monkeypatch.setattr(app_main.settings, "db_path", str(tmp_path / "live.db"))
    monkeypatch.setattr(app_main, "_catalog", None)
    monkeypatch.setattr(app_main, "_store", None)
    monkeypatch.setattr(app_main, "_baseline_job_store", None)
    monkeypatch.setattr(app_main, "_baseline_job_runner", None)
    app_main._settings_store.set_uw_key(FAKE_UW)
    app_main._settings_store.set_ai_key("openai", FAKE_AI)
    app_main._settings_store.set_mode("live")
    adapter = LiveUWAdapter(api_key=FAKE_UW, base_url="http://127.0.0.1:9", watchlist=["ABCD"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return app_main


# ---------------------------------------------------------------- S2-1 pairing rotation DoS / log flood
def test_S2_1_attacker_guesses_must_not_invalidate_the_operators_code(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    announced = []
    app_main._pairing_store.on_rotate = announced.append
    operator_code = app_main._pairing_store.current_code(force_new=True)   # what the operator reads from logs
    announced.clear()
    c = _client(app_main)
    for i in range(200):                                                  # unauthenticated LAN client
        assert c.post("/api/auth/pair", json={"pairing_code": f"guess{i:04d}"}).status_code == 403
    print("codes announced (one log line each) during 200 bad guesses:", len(announced))
    r = c.post("/api/auth/pair", json={"pairing_code": operator_code})
    print("operator's code after the guesses ->", r.status_code)
    assert r.status_code == 200, "an unauthenticated client can keep the legitimate code rotating away"
    assert len(announced) <= 1, "each 20 bad guesses writes a fresh live code to the log (log flood)"
    # (fixed by removing failure limits entirely: see auth.PairingCodeStore -- the same-peer case is the
    # Docker Desktop reality, where every client shares the gateway address)


# ---------------------------------------------------------------- S2-2 provider-spending routes with no 428
def test_S2_2a_baseline_backfill_route_spends_without_confirmation(live_m, monkeypatch):
    calls = []
    monkeypatch.setattr(live_m, "_run_baseline_backfill", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(live_m, "_baseline_report", lambda *a, **k: {})
    r = _authed(live_m).post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    print("backfill without confirm ->", r.status_code, "| provider walks started:", len(calls))
    assert r.status_code == 428 and not calls


def test_S2_2b_baseline_job_resume_spends_without_confirmation(live_m, monkeypatch):
    store = live_m.get_baseline_job_store()
    job = store.create(["ABCD"], 200, 20, requested_by="user", target_sessions=20)
    store.transition(job["id"], ("running",), "paused")
    started = []
    runner = live_m.get_baseline_job_runner()
    monkeypatch.setattr(runner, "start", lambda job_id: started.append(job_id) or True)
    r = _authed(live_m).post(f"/api/baseline/jobs/{job['id']}/resume")
    print("resume without confirm ->", r.status_code, "| job threads started:", started)
    assert r.status_code == 428 and not started


def test_S2_2c_live_investigate_spends_ai_without_confirmation(live_m, monkeypatch):
    calls = []

    class _R:
        def model_dump(self):
            return {"ok": True}

    monkeypatch.setattr(live_m.ai_orchestrator, "investigate", lambda **k: calls.append(k["ticker"]) or _R())
    r = _authed(live_m).post("/api/investigate/ABCD")
    print("live investigate without confirm ->", r.status_code, "| billed AI investigations:", calls)
    assert r.status_code == 428 and not calls


# ---------------------------------------------------------------- S2-3 confirm accepts coerced values
@pytest.mark.parametrize("value", ["yes", "on", "1", 1, "t"])
def test_S2_3_run_once_confirm_must_be_a_real_boolean(app_main, monkeypatch, value):
    ran = []
    runner = app_main.get_scheduler_runner()
    monkeypatch.setattr(runner, "run_once_with_depth", lambda d: ran.append(d) or {"ok": True})
    r = _authed(app_main).post("/api/scheduler/run-once", json={"depth": "discovery_only", "confirm": value})
    print(f"confirm={value!r} ->", r.status_code, ran)
    assert r.status_code in (422, 428) and not ran


# ---------------------------------------------------------------- S2-4 static filter bypass (Windows host)
@pytest.mark.parametrize("path", ["/TEST_CHART_EVIDENCE_MARKERS_HARNESS.HTML", "/Test_navigation.MJS",
                                  "/Test_e2e_live_replay_cdp.py.", "/Test_e2e_live_replay_cdp.py::$DATA"])
def test_S2_4_test_harness_files_never_served(app_main, path):
    r = _client(app_main).get(path)
    print(sys.platform, path, "->", r.status_code, len(r.content))
    assert r.status_code == 404


# ---------------------------------------------------------------- S2-5 Network Mode unauthenticated reads
@pytest.mark.parametrize("path", ["/api/radar", "/api/ticker/BRNX", "/api/settings/watchlist",
                                  "/api/investigate/availability", "/api/ticker/BRNX/ohlc", "/api/scenarios"])
def test_S2_5_network_mode_reads_require_token(app_main, monkeypatch, path):
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    r = _client(app_main).get(path)
    print("network mode, no token:", path, "->", r.status_code)
    assert r.status_code == 401


# ---------------------------------------------------------------- S2-6 quarantine keeps secrets forever
def test_S2_6_removed_keys_survive_in_quarantined_copy(app_main, tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"mode": "live", "uw_api_key": "%s", "ai_api_key": "%s", ' % (FAKE_UW, FAKE_AI))  # truncated
    c = _authed(app_main)
    warn = c.get("/api/settings").json()["settings_warning"]
    print("settings_warning:", warn)
    assert c.delete("/api/settings/uw-key").status_code == 200
    assert c.delete("/api/settings/ai-key").status_code == 200
    leftovers = [f.name for f in tmp_path.iterdir() if f.is_file() and FAKE_UW in f.read_text(errors="ignore")]
    print("files still holding the 'removed' UW key:", leftovers)
    assert not leftovers


def test_S2_6b_transient_read_error_must_not_discard_settings(app_main, tmp_path, monkeypatch):
    from app import settings_store as ss
    app_main._settings_store.set_uw_key(FAKE_UW)
    real_open = open
    state = {"n": 0}

    def flaky_open(path, *a, **k):
        if str(path).endswith("settings.json") and state["n"] == 0:
            state["n"] += 1
            raise PermissionError(13, "file is locked by another process")   # e.g. AV scanner on Windows
        return real_open(path, *a, **k)

    monkeypatch.setattr(ss, "open", flaky_open, raising=False)
    app_main._settings_store.get_all()
    monkeypatch.setattr(ss, "open", real_open, raising=False)
    after = app_main._settings_store.get_all()
    print("after one transient PermissionError, configured key:", bool(after["uw_api_key"]),
          "| files:", sorted(f.name for f in tmp_path.iterdir()))
    assert after["uw_api_key"] == FAKE_UW


# ---------------------------------------------------------------- prior-round fixes (expected to PASS)
def test_ok_SEC2_headers(app_main):
    c = _client(app_main)
    for path in ("/", "/api/settings", "/app.core.js", "/nope"):
        h = c.get(path).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "script-src 'self'" in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    r = _client(app_main).get("/", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.headers.get("x-frame-options") == "DENY"


def test_ok_SEC3_SEC4_rotation(app_main, monkeypatch, tmp_path):
    import time as _t
    from app.auth import PairingCodeStore
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    c = _client(app_main)
    code = app_main._pairing_store.current_code(force_new=True)
    for i in range(20):
        c.post("/api/auth/pair", json={"pairing_code": f"bad{i}"})
    # S2-1 superseded SEC-3's rotate-on-lockout: wrong guesses neither lock out nor rotate the code
    assert app_main._pairing_store.current_code() == code
    assert c.post("/api/auth/pair", json={"pairing_code": code}).status_code == 200
    app_main._pairing_store = PairingCodeStore(tmp_path / ".pc", ttl_s=0.2)
    old = app_main._pairing_store.current_code(force_new=True)
    _t.sleep(0.3)
    assert c.post("/api/auth/pair", json={"pairing_code": old}).status_code == 403
    assert (tmp_path / ".pc").read_text() != old


def test_ok_SEC5_non_ascii(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    c = _client(app_main, raise_server_exceptions=False)
    assert c.post("/api/settings/mode", json={"mode": "demo"},
                  headers={"X-TIP-Auth": ("é" * 64).encode("latin-1")}).status_code == 401
    app_main._pairing_store.current_code(force_new=True)
    assert c.post("/api/auth/pair", json={"pairing_code": "éé"}).status_code == 403


def test_ok_SEC6_last4_hidden_without_auth(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    app_main._settings_store.set_uw_key(FAKE_UW)
    # S2-5 superseded the partial SEC-6 fix: in Network Mode the whole settings read needs the token
    assert _client(app_main).get("/api/settings").status_code == 401
    assert _authed(app_main).get("/api/settings").json()["uw"]["last4"] == "aaaa"


def test_ok_SEC7_update_check_is_authed_post(app_main, monkeypatch):
    calls = []
    monkeypatch.setattr(app_main._update_check.requests, "get", lambda *a, **k: calls.append(a))
    c = _client(app_main)
    assert c.get("/api/settings/update-check").status_code in (404, 405)
    assert c.post("/api/settings/update-check", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 401
    assert not calls


def test_ok_local_mode_bypass_conditions(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", True)
    c = _client(app_main)
    for sfs in ("cross-site", "same-site", "none", None):
        h = {"Sec-Fetch-Site": sfs} if sfs else {}
        assert c.post("/api/settings/mode", json={"mode": "demo"}, headers=h).status_code == 401, sfs
    assert c.post("/api/settings/mode", json={"mode": "demo"},
                  headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:3000"}).status_code == 403
    assert c.post("/api/settings/mode", json={"mode": "demo"},
                  headers={"Sec-Fetch-Site": "same-origin", "Origin": "null"}).status_code == 403
    for host in ("evil.example", "localhost.", "127.0.0.1.nip.io", "foo.localhost"):
        assert c.get("/api/settings", headers={"Host": host}).status_code == 400, host


def test_ok_scheduler_mode_confirm_is_exact(app_main):
    c = _authed(app_main)
    for bad in (True, "true", "ENABLED", "enabled ", 1, None):
        r = c.post("/api/scheduler/mode", json={"mode": "enabled", "confirm": bad})
        assert r.status_code in (422, 428), (bad, r.status_code)
    assert app_main.get_scheduler_runner().store.get_mode() == "disabled"
