"""Episode-scoped, NYSE-session-counted lifecycle (signal-integrity
remediation, audit B5/B6/B7/E7/E8)."""
from datetime import datetime, timezone

import pytest

from app.core import lifecycle, signal_engine as se
from app.core import trading_calendar as tc
from app.storage import QualifyingDaysStore


def gates(dp=None, of=None, price_ok=None, mode="live"):
    """dp/of: True=pass, False=fail, None=insufficient (no baseline)."""
    lean = "bullish-lean"
    return se.evaluate_gates(
        "TST", mode=mode,
        tier1_bootstrap=dp is None, tier1_hit=bool(dp),
        options_flow_status=("insufficient" if of is None else ("pass" if of else "fail")),
        options_flow_hit=bool(of),
        price_move_pct=(1.0 if price_ok else -1.0) if price_ok is not None else None,
        direction_heuristic=lean if of else None,
    )


class Runner:
    def __init__(self, tmp_path):
        self.store = QualifyingDaysStore(str(tmp_path / "lc.db"))

    def scan(self, when, g, fp="fp-default", price_last=100.0, daily=0.0, lean=None,
             complete=True, ticker="TST"):
        as_of = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
        sd = tc.evidence_session_date(as_of)
        ctx = self.store.context(ticker, sd.isoformat())
        d = lifecycle.decide(ctx, g, session_date=sd, collection_complete=complete, lean=lean,
                             price_last=price_last, price_move_pct=daily, fingerprint=fp)
        self.store.apply(ticker, d, scanned_at=as_of.isoformat())
        return d


@pytest.fixture
def run(tmp_path):
    return Runner(tmp_path)


# Fri 2026-10-16 is a trading day; Sat/Sun are not; Mon 2026-10-19 is.
FRI = "2026-10-16T15:00:00"
FRI_LATER = "2026-10-16T19:00:00"
SAT = "2026-10-17T23:03:42"
MON_PREOPEN = "2026-10-19T13:00:00"   # 09:00 ET
MON = "2026-10-19T15:00:00"
TUE = "2026-10-20T15:00:00"
WED = "2026-10-21T15:00:00"


def test_zero_evidence_never_opens_an_episode(run):
    d = run.scan(FRI, gates(dp=None, of=False))
    assert d.state_after is None and d.rule == "no_qualifying_evidence_yet"
    assert run.store.current_state("TST") is None
    assert run.store.episodes("TST") == []


def test_repeated_scans_of_one_session_do_not_advance(run):
    assert run.scan(FRI, gates(of=True), fp="a").state_after == "NEW"
    assert run.scan(FRI_LATER, gates(of=True), fp="b").state_after == "NEW"
    assert run.scan(SAT, gates(of=True), fp="b").rule == "same_session_no_advance"   # Sat -> Fri session
    assert run.scan(MON_PREOPEN, gates(of=True), fp="b").state_after == "NEW"         # pre-open -> Fri
    assert run.store.qualifying_days_count("TST") == 1
    assert {h["scan_date"] for h in run.store.history("TST")} == {"2026-10-16"}


def test_qualifying_evidence_on_a_later_session_advances_to_building(run):
    run.scan(FRI, gates(of=True), fp="a")
    d = run.scan(MON, gates(of=True), fp="b")
    assert (d.state_after, d.rule) == ("BUILDING", "qualifying_evidence_on_a_later_session")
    assert run.store.qualifying_days_count("TST") == 2


def test_identical_evidence_on_a_later_session_is_a_stale_reread_and_is_held(run):
    run.scan(FRI, gates(of=True), fp="same")
    d = run.scan(MON, gates(of=True), fp="same")
    assert d.action == "hold" and d.rule == "held_identical_evidence_from_an_earlier_session"
    assert run.store.current_state("TST") == "NEW"
    assert run.store.qualifying_days_count("TST") == 1


def test_holiday_scan_belongs_to_the_previous_session():
    thanksgiving = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    assert tc.evidence_session_date(thanksgiving).isoformat() == "2026-11-25"


def test_stable_needs_confirmation_on_three_distinct_sessions(run):
    both = gates(dp=True, of=True, price_ok=True)
    assert run.scan(FRI, both, fp="1").state_after == "CONFIRMED"
    assert run.scan(FRI_LATER, both, fp="2").state_after == "CONFIRMED"   # same session: no STABLE
    assert run.scan(MON, both, fp="3").state_after == "CONFIRMED"         # 2 sessions
    d = run.scan(TUE, both, fp="4")
    assert d.state_after == "STABLE" and d.confirmed_sessions == 3


def test_audit_e8_back_to_back_scans_can_no_longer_reach_stable(run):
    """05c47ca reached STABLE in two scans seconds apart using SPY's all-time
    legacy qualifying days (incl. a Saturday). Legacy rows are not read."""
    import sqlite3
    conn = sqlite3.connect(run.store.db_path)
    conn.executemany("INSERT INTO ticker_scan_log (ticker, scan_date, any_gate_passed, state) VALUES (?,?,?,?)",
                     [("SPY", d, 1, "BUILDING") for d in ("2026-10-15", "2026-10-16", "2026-10-17")])
    conn.execute("INSERT INTO ticker_current_state (ticker, state) VALUES ('SPY', 'BUILDING')")
    conn.commit()
    conn.close()
    assert run.store.current_state("SPY") is None  # legacy state is not inherited
    both = gates(dp=True, of=True, price_ok=True)
    assert run.scan("2026-10-19T15:00:00", both, fp="x", ticker="SPY").state_after == "CONFIRMED"
    assert run.scan("2026-10-19T15:00:05", both, fp="y", ticker="SPY").state_after == "CONFIRMED"


def test_building_fades_without_evidence_and_reheats_on_new_evidence(run):
    run.scan(FRI, gates(of=True), fp="a")
    run.scan(MON, gates(of=True), fp="b")
    assert run.scan(TUE, gates(of=False), fp="c").state_after == "FADING"
    d = run.scan(WED, gates(of=True), fp="d")
    assert (d.state_after, d.rule) == ("BUILDING", "reheated_single_signal")


def test_insufficient_evidence_and_failed_collection_hold_state(run):
    run.scan(FRI, gates(of=True), fp="a")
    run.scan(MON, gates(of=True), fp="b")
    rows = len(run.store.history("TST"))
    d = run.scan(TUE, gates(dp=None, of=None), fp="c")
    assert d.rule == "held_insufficient_evidence" and run.store.current_state("TST") == "BUILDING"
    d = run.scan(WED, gates(of=False), fp="d", complete=False)
    assert d.rule == "held_incomplete_collection" and run.store.current_state("TST") == "BUILDING"
    assert len(run.store.history("TST")) == rows + 1   # the failed collection writes no row


def test_invalidation_is_terminal_for_the_episode_and_reopening_is_explicit(run):
    run.scan(FRI, gates(of=True), fp="a", price_last=100.0)
    d = run.scan(MON, gates(of=True), fp="b", price_last=96.5, daily=-0.2, lean="bullish-lean")
    assert d.state_after == "INVALIDATED"
    assert d.rule.startswith("adverse_price_move_since_episode_open")
    assert run.store.current_state("TST") == "INVALIDATED"
    same_session = run.scan("2026-10-19T19:00:00", gates(of=True), fp="c", price_last=97.0)
    assert same_session.rule == "invalidated_this_session_no_reopen"
    reopened = run.scan(TUE, gates(of=True), fp="d", price_last=97.0)
    assert (reopened.state_after, reopened.rule) == ("NEW", "new_episode_after_invalidation")
    eps = run.store.episodes("TST")
    assert [e["close_reason"] for e in eps] == ["invalidated", None]
    assert run.store.qualifying_days_count("TST") == 1  # counts are per episode


def test_invalidation_without_a_reference_price_uses_the_daily_move(run):
    run.scan(FRI, gates(of=True), fp="a", price_last=None)
    d = run.scan(MON, gates(of=True), fp="b", price_last=None, daily=-4.0, lean="bullish-lean")
    assert d.state_after == "INVALIDATED" and "daily_change" in d.rule


def test_episode_expires_after_the_gap_limit(run):
    run.scan(FRI, gates(of=True), fp="a")
    late = "2026-10-27T15:00:00"   # Fri 10-16 -> Tue 10-27 = 7 sessions later
    d = run.scan(late, gates(of=False), fp="b")
    assert d.rule == "episode_expired_no_recent_evidence" and d.state_after is None
    assert run.store.current_state("TST") is None
    d = run.scan("2026-10-28T15:00:00", gates(of=True), fp="c")
    assert d.state_after == "NEW"
    assert [e["close_reason"] for e in run.store.episodes("TST")] == ["expired", None]


def test_demo_scans_skip_fingerprint_checks(run):
    run.scan(FRI, gates(of=True, mode="demo"), fp=None)
    assert run.scan(MON, gates(of=True, mode="demo"), fp=None).state_after == "BUILDING"


def test_upgrading_an_existing_db_to_the_lifecycle_migration_takes_a_backup(tmp_path, monkeypatch):
    import sqlite3

    from app import migrations

    db = tmp_path / "existing.db"
    conn = sqlite3.connect(db)
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in migrations.MIGRATIONS if m.version < 7])
    migrations.run_migrations(conn, str(db))          # an install from before this change
    conn.execute("CREATE TABLE IF NOT EXISTS ticker_scan_log (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.execute("INSERT INTO ticker_scan_log (ticker) VALUES ('SPY')")
    conn.commit()
    before = {p.name for p in tmp_path.iterdir()}
    monkeypatch.undo()
    applied = migrations.run_migrations(conn, str(db))
    conn.close()
    assert 7 in applied
    backups = {p.name for p in tmp_path.iterdir()} - before
    assert any(".pre-migration-" in name for name in backups)
    check = sqlite3.connect(db)
    assert check.execute("SELECT COUNT(*) FROM ticker_scan_log").fetchone()[0] == 1   # legacy rows untouched
    check.close()
