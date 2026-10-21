"""Demo and Live lifecycle data are isolated at the storage level
(signal-integrity remediation, audit E10): a real ticker that also exists as
a demo fixture (FLUX) must never inherit Demo state/history in Live Mode."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.uw_fakes import fake_live_adapter, ok


@pytest.fixture
def file_backed(app_main, tmp_path, monkeypatch):
    monkeypatch.setattr(app_main.settings, "db_path", str(tmp_path / "tip.db"))
    monkeypatch.setattr(app_main.settings, "demo_db_path", str(tmp_path / "tip.demo.db"))
    app_main._store = None
    app_main._demo_store = None
    app_main._catalog = None
    return app_main


def _quiet_live_routes():
    return {
        "flow-alerts": ok([]),
        "/api/darkpool/": ok([]),
        "stock-state": ok({"close": "10.00", "prev_close": "10.10"}),
    }


def _rows(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def test_demo_and_live_use_different_database_files(file_backed):
    m = file_backed
    assert m.get_store("live").db_path != m.get_store("demo").db_path
    assert m.get_store("demo").db_path.endswith("tip.demo.db")


def test_demo_activity_writes_nothing_into_the_live_database(file_backed):
    m = file_backed
    client = TestClient(m.app)
    for _ in range(3):
        assert client.get("/api/ticker/FLUX").status_code == 200  # demo GET runs a demo scan
    assert client.get("/api/radar").status_code == 200
    demo = m.get_store("demo")
    assert demo.current_state("FLUX") is not None
    assert demo.history("PRSX"), "demo seeding still happens -- in the demo store"
    live_path = m.settings.db_path
    assert m.get_store("live").current_state("FLUX") is None
    assert m.get_store("live").history("PRSX") == []
    assert _rows(live_path, "ticker_scan_log") == 0


def test_live_scan_of_a_ticker_that_is_also_a_demo_fixture_starts_clean(file_backed, monkeypatch):
    m = file_backed
    client = TestClient(m.app)
    for _ in range(3):
        client.get("/api/ticker/FLUX")
    demo_state = m.get_store("demo").current_state("FLUX")
    assert demo_state in ("CONFIRMED", "BUILDING", "NEW")

    adapter = fake_live_adapter(_quiet_live_routes(), watchlist=["FLUX"])
    monkeypatch.setattr(m, "get_adapter", lambda: adapter)
    body = client.post("/api/ticker/FLUX/refresh", json={"confirm": True}).json()
    assert body["previous_state"] is None, "Live must never inherit the Demo lifecycle state"
    assert m.get_store("live").qualifying_days_count("FLUX") == 0
    # and the Live scan did not touch the Demo store either
    assert m.get_store("demo").current_state("FLUX") == demo_state


def test_same_file_for_demo_and_live_is_refused(file_backed, monkeypatch):
    m = file_backed
    monkeypatch.setattr(m.settings, "demo_db_path", m.settings.db_path)
    with pytest.raises(m.StorageIsolationError):
        m.get_store("demo")


def test_derived_demo_path():
    from app.config import derive_demo_db_path

    assert derive_demo_db_path("./data/tip_hackathon.db") == "./data/tip_hackathon.demo.db"
    assert derive_demo_db_path(":memory:") == ":memory:"


def test_contamination_audit_tool_is_read_only_and_attributes_fixture_rows(tmp_path):
    import hashlib
    import importlib.util
    from pathlib import Path

    from app.storage import QualifyingDaysStore

    import sqlite3

    db = tmp_path / "legacy.db"
    QualifyingDaysStore(str(db))  # creates the legacy + current schema
    conn = sqlite3.connect(db)
    # pre-isolation rows as the old single-store code wrote them
    conn.execute("INSERT INTO ticker_scan_log (ticker, scan_date, any_gate_passed, state) "
                 "VALUES ('FLUX', '2026-10-15', 1, 'CONFIRMED'), ('SPY', '2026-10-15', 1, 'NEW')")
    conn.execute("INSERT INTO ticker_current_state (ticker, state) VALUES ('FLUX', 'CONFIRMED'), ('SPY', 'NEW')")
    conn.commit()
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("audit_tool", root / "tools" / "audit_demo_live_contamination.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    report = tool.audit(str(db), str(root / "fixtures"))

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert [r["ticker"] for r in report["scan_log"]] == ["FLUX"]
    assert report["summary"]["scan_log_rows_demo_attributed"] == 1
    assert {r["ticker"] for r in report["current_state"]} == {"FLUX"}
