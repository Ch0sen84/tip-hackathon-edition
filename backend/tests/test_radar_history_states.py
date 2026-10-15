"""Stage 2 (P3) backend addition: /api/radar entries carry a
history_states list, sourced only from the real local scan-history store
(never fabricated), non-empty only when a ticker genuinely has more than
one recorded scan."""
import os
from pathlib import Path

os.environ.setdefault("TIP_MODE", "demo")
# #38: forced override, not setdefault -- see conftest.py's note. Harmless
# no-op when conftest.py (which forces this first) has already run; kept
# here too so this file is still safe if ever executed standalone.
os.environ["TIP_DB_PATH"] = ":memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_radar_entries_carry_history_states_field():
    client = TestClient(app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200
    data = resp.json()
    all_entries = data["highest"] + data["other"]
    assert all_entries, "expected at least one qualifying entry"
    for entry in all_entries:
        assert "history_states" in entry
        assert isinstance(entry["history_states"], list)


def test_radar_entries_carry_a_real_state_field():
    """Pre-existing gap fixed in this pass: /api/radar entries never
    carried a "state" key at all (frontend state tags/filter had nothing
    real to render against)."""
    client = TestClient(app)
    resp = client.get("/api/radar")
    data = resp.json()
    all_entries = data["highest"] + data["other"]
    valid_states = {"NEW", "BUILDING", "CONFIRMED", "STABLE", "FADING", "INVALIDATED"}
    for entry in all_entries:
        assert entry.get("state") in valid_states, entry


def test_prsx_shows_real_multi_entry_history_after_seeding(app_main):
    """PRSX is the one scenario this pass seeded with genuinely distinct
    real scan_date history -- it must be the one showing a multi-entry
    history_states list on a fresh process, not a fabricated one.

    SIGNAL-INTEGRITY RULE CHANGE (2026-10-15): PRSX's fixture is a SINGLE
    independent category (dark_pool_anomaly only -- see
    fixtures/scenario_persistence_progression.json's own description).
    Before this rule change, the live third scan reached CONFIRMED purely
    via the old `qualifying_days_count >= 2` bypass -- exactly the
    single-real-gate-seen-on-two-calendar-dates loophole Chad's rule
    change closes (see signal_engine.py's module docstring). PRSX now
    correctly STAYS BUILDING on the live scan: multi_signal_confirmation
    never passes (only 1 of INDEPENDENT_SIGNAL_GATES fires), so there is
    no genuine 2-category corroboration to promote it to CONFIRMED.
    """
    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    data = resp.json()
    all_entries = {e["ticker"]: e for e in data["highest"] + data["other"]}
    assert "PRSX" in all_entries
    # main.py's _store is a module-level singleton shared across this whole
    # test session (same pattern other tests in this file already rely
    # on), so by the time this test runs PRSX may have been scanned more
    # than once already -- assert the real, seeded trajectory PREFIX
    # rather than an exact length, so this stays robust to test order
    # without weakening what it actually proves (a real seeded multi-day
    # NEW -> BUILDING -> (still BUILDING, no false CONFIRMED) progression).
    history = all_entries["PRSX"]["history_states"]
    assert len(history) >= 3
    assert history[:3] == ["NEW", "BUILDING", "BUILDING"]
