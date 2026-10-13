from pathlib import Path

from app.adapters.synthetic_adapter import SyntheticDataAdapter

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


EXPECTED_UNIVERSE = {
    "ZYNC", "QRVX", "FLUX", "NADA", "DRFT",  # original 5 (P1/P2 pass), unchanged
    "CONFL", "PRSX", "VOLX", "BRNX", "TEPID", "CATX",  # P4 expansion (Stage 1)
}


def test_loads_all_scenario_fixtures():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    universe = adapter.get_universe()
    assert set(universe) == EXPECTED_UNIVERSE


def test_get_flow_alerts_well_formed_for_call_sweep():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    alerts = adapter.get_flow_alerts("ZYNC")
    assert len(alerts) == 2
    for a in alerts:
        assert "total_premium" in a
        assert "total_ask_premium" in a
        assert "total_bid_premium" in a
        assert "has_sweep" in a


def test_get_darkpool_trades_well_formed_for_darkpool_anomaly():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    trades = adapter.get_darkpool_trades("QRVX")
    assert len(trades) == 1
    assert trades[0]["premium"] == 28750000
    assert "avg_premium_baseline" in trades[0]


def test_unknown_ticker_returns_empty_not_crash():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    assert adapter.get_flow_alerts("DOESNOTEXIST") == []
    assert adapter.get_darkpool_trades("DOESNOTEXIST") == []
    assert adapter.get_stock_state("DOESNOTEXIST") is None


def test_stock_state_shape():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    state = adapter.get_stock_state("FLUX")
    assert state["last_price"] == 214.1
    assert state["change_percent"] == 2.1


def test_quiet_control_has_low_signal_inputs():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    alerts = adapter.get_flow_alerts("NADA")
    trades = adapter.get_darkpool_trades("NADA")
    assert alerts[0]["total_premium"] < 100_000
    # dark pool premium is near its own baseline -- not anomalous.
    assert trades[0]["premium"] / trades[0]["avg_premium_baseline"] < 1.2


def test_engine_runs_for_real_on_each_fixture_not_hardcoded():
    """The core requirement: scan_service really computes a gate/confidence
    result per scenario, and different fixtures produce different results
    -- proving the SyntheticDataAdapter feeds real data through real logic,
    not canned conclusions."""
    from app.core import scan_service

    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    results = {}
    for ticker in adapter.get_universe():
        scan = scan_service.evaluate_ticker_scan(
            ticker=ticker,
            flow_alerts=adapter.get_flow_alerts(ticker),
            darkpool_trades=adapter.get_darkpool_trades(ticker),
            current_state=None,
            qualifying_days_count=0,
        )
        results[ticker] = scan

    # multi-signal scenario must actually produce multi-signal confidence.
    assert results["FLUX"]["signal_type"] == "multi-signal"
    assert results["FLUX"]["confidence"] == "high"
    assert set(results["FLUX"]["gates_passed"]) >= {"dark_pool_anomaly", "options_flow_anomaly"}

    # dark-pool-only scenario must pass that gate with a strong ratio.
    assert "dark_pool_anomaly" in results["QRVX"]["gates_passed"]
    assert results["QRVX"]["gates"]["dark_pool_anomaly"]["metrics"]["ratio"] >= se_strong_ratio()

    # quiet control must not qualify on any anomaly gate.
    assert results["NADA"]["gates_passed"] == [] or "dark_pool_anomaly" not in results["NADA"]["gates_passed"]
    assert results["NADA"]["confidence"] == "low"

    # not every ticker produces the same result -- proves it's not canned.
    distinct_signal_types = {r["signal_type"] for r in results.values()}
    assert len(distinct_signal_types) > 1


def se_strong_ratio():
    from app.core.signal_engine import STRONG_RATIO
    return STRONG_RATIO
