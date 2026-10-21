"""Regression tests for the post-v7 hardening cycle: the server-side confirmation contract (FE-F1), the
FE-F3 backfill rule, Investigate's cache-only evidence (FE-F2), security headers and static-file scope
(SEC-2/SEC-9), key fingerprints for unauthorized readers (SEC-6), pairing-code rotation (SEC-3/4/5),
settings-file corruption (PE-P5), the diagnostics ring buffer (PE-P8), catalog write failures (PE-P4),
the lifecycle second-episode refusal (PE-P3) and the request budget the UI discloses (FE-F12).
Every fake provider and row here is CONSTRUCTED; no real request is made."""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import session_collection as sc
from app.ingestion import IngestionCatalog
from tests.synthetic_ledger import LedgerUW
from tests.uw_paginated import NY, session_prints


# --- FE-F1: provider-spending / recurring actions require an explicit confirmation ------------------
def test_scheduler_mode_needs_confirm_equal_to_the_mode_and_nothing_changes_without_it(app_main):
    client = TestClient(app_main.app)
    for mode, bad in (("dry_run", None), ("enabled", None), ("enabled", "dry_run"), ("enabled", True)):
        body = {"mode": mode} if bad is None else {"mode": mode, "confirm": bad}
        res = client.post("/api/scheduler/mode", json=body)
        assert res.status_code in (422, 428), (mode, bad, res.status_code)
        if res.status_code == 428:
            d = res.json()["detail"]
            assert d["confirmation_required"] and d["action"] == f"scheduler_mode:{mode}"
            assert "RECURRING" in d["consequence"]
        assert client.get("/api/scheduler/status").json()["mode"] == "disabled"
    assert client.post("/api/scheduler/mode", json={"mode": "disabled"}).status_code == 200  # no confirm to disarm
    assert client.post("/api/scheduler/mode", json={"mode": "dry_run", "confirm": "dry_run"}).json()["mode"] == "dry_run"


def test_run_once_validates_first_then_requires_confirm_and_runs_nothing_without_it(app_main, monkeypatch):
    client = TestClient(app_main.app)
    runner = app_main.get_scheduler_runner()
    cycles = []
    monkeypatch.setattr(runner, "run_once_with_depth", lambda depth: cycles.append(depth) or {"ran": True})
    monkeypatch.setattr(runner, "run_once", lambda: cycles.append("legacy") or {"ran": False, "reason": "disabled"})
    assert client.post("/api/scheduler/run-once", json={"depth": "bogus"}).status_code == 400
    res = client.post("/api/scheduler/run-once", json={"depth": "discovery_plus_evaluation"})
    assert res.status_code == 428 and res.json()["detail"]["evaluates"] is True
    assert cycles == []
    # the legacy no-depth call while recurring mode is disabled is a refusal (no provider request): as before
    assert client.post("/api/scheduler/run-once", json={}).json()["reason"] == "disabled"
    assert client.post("/api/scheduler/run-once", json={"depth": "discovery_only", "confirm": True}).status_code == 200
    assert cycles == ["legacy", "discovery_only"]


def test_baseline_job_without_confirm_states_its_total_page_budget_and_creates_nothing(app_main, monkeypatch):
    from tests.test_baseline_jobs import AS_OF, build_uw

    uw = build_uw()
    adapter = uw.adapter(["ABCD", "EFGH"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    client = TestClient(app_main.app)
    res = client.post("/api/baseline/jobs", json={"tickers": ["ABCD", "EFGH"], "max_pages_per_ticker": 50})
    assert res.status_code == 428
    d = res.json()["detail"]
    assert d["total_pages"] == 100 and d["tickers"] == ["ABCD", "EFGH"]
    assert "100 pages in total" in d["consequence"]
    assert uw.calls == []
    assert client.get("/api/baseline/jobs/current").json().get("job") is None


def test_reconcile_requires_confirm_after_validation_and_makes_no_request_without_it(app_main, monkeypatch):
    from tests.test_baseline_jobs import AS_OF, build_uw

    uw = build_uw()
    adapter = uw.adapter(["ABCD"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    client = TestClient(app_main.app)
    url = "/api/ticker/ABCD/baseline/reconcile"
    assert client.post(url, json={"dataset": "nope", "session_date": "2026-10-15"}).status_code == 400
    assert client.post(url, json={"dataset": "darkpool", "session_date": "2026-10-16"}).status_code == 400  # not prior
    res = client.post(url, json={"dataset": "darkpool", "session_date": "2026-10-15", "max_pages": 9999})
    assert res.status_code == 428 and res.json()["detail"]["max_pages"] == app_main.RECONCILE_MAX_PAGES
    assert uw.calls == []


# --- FE-F3: a one-time run never backfills history ---------------------------------------------------
def test_run_once_with_evaluation_never_runs_the_historical_backfill(tmp_path):
    from app.scheduler import SchedulerRunner

    backfills = []

    class _Adapter:
        def discover_candidates(self, filters=None):
            return {"candidates": [], "snapshot_at": None}

    class _Catalog:
        def catalog_discovery_snapshot(self, **kw):
            return None

    runner = SchedulerRunner(db_path=str(tmp_path / "s.db"), get_adapter=_Adapter, get_catalog=_Catalog,
                             run_ticker_scan=lambda a, t: {}, run_baseline_backfill=lambda a: backfills.append(1))
    runner.store.set_mode("enabled", actor="test")      # even with the recurring mode armed
    runner.run_once_with_depth("discovery_plus_evaluation")
    runner.run_once()
    assert backfills == []


def test_scheduler_baseline_backfill_is_opt_in_and_off_by_default():
    import inspect

    from app import main
    src = inspect.getsource(main.get_scheduler_runner)
    assert 'TIP_SCHEDULER_BASELINE_BACKFILL' in src and '== "1"' in src


def _live_with_keys(m):
    m._settings_store.set_uw_key("test-uw-key-0000")
    m._settings_store.set_ai_key("anthropic", "test-ai-key-0000")
    m._settings_store.set_mode("live")              # last: saving a key returns the app to Demo Mode
    assert m._settings_store.get_mode() == "live"


# --- FE-F2: Investigate reads the cached evaluation; it never collects or saves one -------------------
def test_live_investigate_makes_no_adapter_call_and_saves_no_evaluation_even_when_ai_fails(app_main, monkeypatch):
    client = TestClient(app_main.app)
    _live_with_keys(app_main)

    class _NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"Investigate touched the adapter: {name}")

    monkeypatch.setattr(app_main, "get_adapter", lambda: _NoCalls())
    monkeypatch.setattr(app_main, "_mode_of", lambda adapter: "live")
    monkeypatch.setattr(app_main, "_run_scan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_run_scan")))

    def _fail(**kw):
        raise app_main.LLMCallError("AI provider down")

    monkeypatch.setattr(app_main.ai_orchestrator, "investigate", _fail)
    res = client.post("/api/investigate/AMD", json={"confirm": True})
    assert res.status_code == 502
    with app_main.get_catalog()._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM signal_evaluations WHERE ticker = 'AMD'").fetchone()[0]
    assert n == 0


def test_a_second_concurrent_investigation_of_the_same_ticker_is_refused(app_main, monkeypatch):
    client = TestClient(app_main.app)
    _live_with_keys(app_main)
    monkeypatch.setattr(app_main, "_cached_scan_for_ticker", lambda *a: (None, None))
    lock = app_main._investigate_lock("AMD")
    assert lock.acquire(blocking=False)
    try:
        assert client.post("/api/investigate/AMD", json={"confirm": True}).status_code == 409
    finally:
        lock.release()


# --- SEC-2 / SEC-9 / SEC-6 --------------------------------------------------------------------------
def test_every_response_carries_the_browser_hardening_headers(app_main):
    client = TestClient(app_main.app)
    for path in ("/api/health", "/api/settings"):
        h = client.get(path).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"].split("script-src")[1].split(";")[0]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"


def test_static_files_serve_the_app_but_never_test_harnesses_or_sources(app_main):
    client = TestClient(app_main.app)
    if client.get("/index.html").status_code != 200:
        pytest.skip("frontend directory not mounted in this environment")
    assert client.get("/app.live.js").status_code == 200
    for p in ("/test_navigation.mjs", "/test_lib_auth.mjs", "/test_cycle_fe_guards.mjs", "/fe_shim.py", "/NOTES.md"):
        assert client.get(p).status_code == 404, p


def test_index_html_has_no_inline_handlers_under_the_csp():
    from pathlib import Path
    import re
    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handlers are blocked by script-src 'self'"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I), "inline <script> is blocked by the CSP"


def test_key_fingerprints_are_withheld_from_an_unauthorized_network_mode_reader(app_main, monkeypatch):
    client = TestClient(app_main.app)
    assert client.post("/api/settings/uw-key", json={"api_key": "uwtest_FAKE_0000_WXYZ"}).status_code == 200
    assert client.get("/api/settings").json()["uw"]["last4"] == "WXYZ"
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    anon = TestClient(app_main.app)
    anon.headers.pop("X-TIP-Auth", None)
    assert anon.get("/api/settings").status_code == 401          # S2-5: Network Mode reads need the token
    assert client.get("/api/settings").json()["uw"]["last4"] == "WXYZ"   # the token holder still sees it


def test_network_mode_reads_need_the_token_except_health_and_pairing(app_main, monkeypatch):
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    anon = TestClient(app_main.app)
    anon.headers.pop("X-TIP-Auth", None)
    for path in ("/api/radar", "/api/ticker/FLUX", "/api/ticker/FLUX/ohlc", "/api/settings/watchlist",
                 "/api/investigate/availability", "/api/scenarios", "/api/mode", "/api/settings/version"):
        assert anon.get(path).status_code == 401, path
    assert anon.get("/api/health").status_code == 200
    assert anon.get("/api/auth/mode").status_code == 200
    authed = TestClient(app_main.app)
    assert authed.get("/api/radar").status_code == 200
    # Local Mode is unchanged: same-origin browser reads need no token
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", True)
    assert anon.get("/api/radar").status_code == 200


def test_confirm_fields_are_strict_booleans(app_main, monkeypatch):
    """S2-3: "yes", "1", 1 or "on" never count as a confirmation."""
    client = TestClient(app_main.app)
    runner = app_main.get_scheduler_runner()
    ran = []
    monkeypatch.setattr(runner, "run_once_with_depth", lambda depth: ran.append(depth) or {"ran": True})
    for v in ("yes", "1", 1, "on", "true", "t"):
        r = client.post("/api/scheduler/run-once", json={"depth": "discovery_only", "confirm": v})
        assert r.status_code == 422, (v, r.status_code)
    assert ran == []


def test_resume_states_the_remaining_budget_before_spending(app_main, monkeypatch):
    from tests.test_baseline_jobs import AS_OF, build_uw

    uw = build_uw()
    adapter = uw.adapter(["ABCD"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    store = app_main.get_baseline_job_store()
    job = store.create(["ABCD"], max_pages_per_ticker=40, step_pages=10, requested_by="test")
    store.transition(job["id"], ("running",), "paused")
    client = TestClient(app_main.app)
    r = client.post(f"/api/baseline/jobs/{job['id']}/resume")
    assert r.status_code == 428 and r.json()["detail"]["remaining_pages"] == 40
    assert uw.calls == [] and store.get(job["id"])["status"] == "paused"


def test_backfill_route_requires_confirmation(app_main, monkeypatch):
    from tests.test_baseline_jobs import AS_OF, build_uw

    uw = build_uw()
    adapter = uw.adapter(["ABCD"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    r = TestClient(app_main.app).post("/api/ticker/ABCD/baseline/backfill?max_pages=999")
    assert r.status_code == 428 and r.json()["detail"]["max_pages"] == app_main.BACKFILL_MAX_PAGES_PER_REQUEST
    assert uw.calls == []


def test_live_investigate_requires_confirmation(app_main, monkeypatch):
    _live_with_keys(app_main)
    called = []
    monkeypatch.setattr(app_main, "_investigate_run", lambda *a: called.append(a) or {})
    r = TestClient(app_main.app).post("/api/investigate/AMD")
    assert r.status_code == 428 and "billed" in r.json()["detail"]["consequence"]
    assert called == []


def test_static_files_are_an_exact_allowlist(app_main):
    client = TestClient(app_main.app)
    if client.get("/index.html").status_code != 200:
        pytest.skip("frontend directory not mounted in this environment")
    for p in ("/APP.LIVE.JS", "/Test_navigation.MJS", "/app.live.js.", "/app.live.js::$DATA", "/test_lib_auth.mjs"):
        assert client.get(p).status_code == 404, p
    assert client.get("/styles.css").status_code == 200


def test_update_check_is_a_post_now(app_main):
    client = TestClient(app_main.app)
    assert client.get("/api/settings/update-check").status_code in (404, 405)   # no GET route any more
    anon = TestClient(app_main.app)
    anon.headers.pop("X-TIP-Auth", None)
    import app.main as _m
    _m.LOCAL_MODE_ACTIVE, saved = False, _m.LOCAL_MODE_ACTIVE
    try:
        assert anon.post("/api/settings/update-check").status_code == 401
    finally:
        _m.LOCAL_MODE_ACTIVE = saved


# --- FE-F12: the request budget the UI states comes from the collector's real limits ------------------
def test_settings_report_the_per_ticker_request_budget(app_main, monkeypatch):
    client = TestClient(app_main.app)
    monkeypatch.setattr(sc, "DEFAULT_SCAN_PAGES", 4)
    monkeypatch.setenv("TIP_UW_MAX_RETRIES", "2")
    b = client.get("/api/settings").json()["request_budget"]
    assert b == {"per_ticker_scan_requests": 9, "attempts_per_request": 3}


# --- SEC-3/4/5: pairing codes ------------------------------------------------------------------------
def test_another_clients_wrong_guesses_never_lock_out_or_rotate_the_operators_code(tmp_path):
    """S2-1 (replaces the SEC-3 rotate-on-failure fix): failures are limited per client and never rotate."""
    from app import auth

    store = auth.PairingCodeStore(tmp_path / "pairing_code")
    announced = []
    store.on_rotate = lambda code, reason: announced.append((code, reason))
    code = store.current_code(force_new=True)
    for _ in range(10 * auth.PAIRING_CODE_MAX_FAILED_ATTEMPTS):
        assert store.consume("wrong", client="172.17.0.1") is False
    assert store.current_code() == code and announced == [(code, "startup")]   # no rotation, nothing logged
    # behind Docker Desktop the operator arrives from the SAME gateway address as the guesser: still pairs
    assert store.consume(code, client="172.17.0.1") is True
    assert announced[-1][1] == "used" and announced[-1][0] != code


def test_an_expired_pairing_code_is_replaced_and_non_ascii_input_never_raises(tmp_path, monkeypatch):
    from app import auth

    store = auth.PairingCodeStore(tmp_path / "pairing_code", ttl_s=60)
    announced = []
    store.on_rotate = lambda code, reason: announced.append((code, reason))
    old = store.current_code(force_new=True)
    assert store.consume("ééé") is False   # SEC-5: compared as bytes, no TypeError
    import time as _time
    t = _time.monotonic() + 3600
    monkeypatch.setattr(_time, "monotonic", lambda: t)
    assert store.consume(old) is False
    assert announced[-1][1] == "expired" and announced[-1][0] != old


def test_secret_files_never_reuse_or_follow_a_stale_temp_file(tmp_path):
    """S2-7: a crash-leftover temp file (world-readable) is replaced, not written through."""
    import os

    from app import auth

    target = tmp_path / "token"
    stale = tmp_path / "token.tmp"
    stale.write_text("stale")
    os.chmod(stale, 0o644)
    auth._write_secret_file(target, "secret-value")
    assert target.read_text(encoding="utf-8") == "secret-value" and not stale.exists()


# --- PE-P5: a corrupt settings file is preserved and reported, never silently wiped ------------------
def test_corrupt_settings_file_is_quarantined_and_reported(tmp_path):
    from app.settings_store import SettingsStore

    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff\xfe{ not json")
    store = SettingsStore(path)
    data = store.get_all()
    assert isinstance(data, dict)
    assert store.recovery_notice and "settings.json.corrupt-" in store.recovery_notice
    aside = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == b"\xff\xfe{ not json"


def test_deleting_a_key_also_removes_preserved_copies_that_may_contain_it(app_main):
    from pathlib import Path

    client = TestClient(app_main.app)
    assert client.post("/api/settings/uw-key", json={"api_key": "uwtest_FAKE_0000_WXYZ"}).status_code == 200
    path = Path(app_main._settings_store.path)
    path.write_text('{"uw_api_key": "uwtest_FAKE_0000_WXYZ", ', encoding="utf-8")    # truncated: quarantined
    app_main._settings_store.get_all()
    assert app_main._settings_store.preserved_copies()
    body = client.delete("/api/settings/uw-key").json()
    assert body["removed_preserved_copies"] and app_main._settings_store.preserved_copies() == []
    assert not any("WXYZ" in p.read_text(errors="ignore") for p in path.parent.glob(path.name + "*") if p.is_file())


def test_settings_route_surfaces_the_recovery_notice(app_main, monkeypatch):
    monkeypatch.setattr(app_main._settings_store, "recovery_notice", "settings file was unreadable (test)")
    assert TestClient(app_main.app).get("/api/settings").json()["settings_warning"] == \
        "settings file was unreadable (test)"


# --- PE-P8: diagnostics are a ring buffer, never a permanent stop ----------------------------------
def test_diagnostics_keep_the_newest_rows_once_full(tmp_path, monkeypatch):
    cat = IngestionCatalog(str(tmp_path / "d.db"))
    monkeypatch.setattr(IngestionCatalog, "DIAGNOSTICS_MAX_ROWS", 5)
    for i in range(12):
        assert cat.record_collection_diagnostic({"dataset": "darkpool", "ticker": "ABCD", "session_date": "2026-10-15",
                                                 "reason": f"r{i}"}) is True
    rows = cat.list_collection_diagnostics()
    assert [r["reason"] for r in rows] == ["r11", "r10", "r9", "r8", "r7"]


# --- PE-P4: a local catalog write failure never erases proven coverage ---------------------------------
def test_catalog_write_failure_keeps_proven_coverage_and_reports_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)
    day = date(2026, 10, 15)
    open_utc = datetime.combine(day, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
    cat = IngestionCatalog(str(tmp_path / "c.db"))
    uw = LedgerUW(darkpool=session_prints("ABCD", day, 1000.0))
    uw.now = open_utc + timedelta(hours=2)
    u1 = sc.update_session_coverage(uw.adapter().client, cat, "darkpool", "ABCD", day, now_utc=uw.now, max_pages=200)
    assert u1.coverage["status"] == "partial" and u1.coverage["covered_through_utc"]
    before = cat.get_session_coverage("darkpool", "ABCD", day.isoformat())

    def _refuse(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(IngestionCatalog, "catalog_darkpool_observations", _refuse)
    uw.now = open_utc + timedelta(hours=4)
    u2 = sc.update_session_coverage(uw.adapter().client, cat, "darkpool", "ABCD", day, now_utc=uw.now, max_pages=200)
    assert u2.collection_status == "error" and "catalog_write_failed" in u2.detail
    assert cat.get_session_coverage("darkpool", "ABCD", day.isoformat()) == before


# --- PE-P3: never a second open lifecycle episode from a stale decision -------------------------------
def test_a_stale_open_decision_never_creates_a_second_open_episode(tmp_path):
    import sqlite3

    from app.core.lifecycle import LifecycleDecision
    from app.storage import QualifyingDaysStore

    db = str(tmp_path / "l.db")
    store = QualifyingDaysStore(db)
    d = LifecycleDecision(state_before=None, state_after="EMERGING", rule="test", action="open",
                          session_date="2026-10-15", qualifying=True, reference_price=10.0)
    store.apply("ABCD", d)
    store.apply("ABCD", d)                                # a concurrent scan decided from the same stale context
    with sqlite3.connect(db) as conn:
        open_eps = conn.execute("SELECT COUNT(*) FROM lifecycle_episodes WHERE ticker = 'ABCD' AND closed_at IS NULL"
                                ).fetchone()[0]
    assert open_eps == 1


def test_live_refresh_budget_counts_the_current_watchlist(app_main, monkeypatch):
    """F2-3 / F2-3b: a Live refresh is confirmed by the server, with a budget for the CURRENT watchlist."""
    from tests.test_baseline_jobs import AS_OF, build_uw

    uw = build_uw()
    adapter = uw.adapter(["ABCD"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    app_main._settings_store.set_watchlist([f"T{c}" for c in "ABCDEFGHIJKL"])
    client = TestClient(app_main.app)
    r = client.post("/api/radar/refresh")
    assert r.status_code == 428 and r.json()["detail"]["tickers"] == 12
    r = client.post("/api/ticker/ABCD/refresh")
    assert r.status_code == 428 and r.json()["detail"]["tickers"] == 1
    assert uw.calls == []
    # Demo Mode (synthetic data) is never interrupted
    monkeypatch.setattr(app_main, "get_adapter", lambda: app_main.SyntheticDataAdapter(app_main.settings.fixtures_dir))
    assert client.post("/api/radar/refresh").status_code != 428
