"""Stage 1 (P4) scenario-expansion tests. Every assertion here runs the
REAL signal_engine/scan_service end-to-end on the new fixtures (never a
hardcoded gate/confidence/state) -- see each fixture's own "description"
field in fixtures/scenario_*.json for the full rationale per scenario.
"""
from pathlib import Path

from app.adapters.synthetic_adapter import SyntheticDataAdapter
from app.core import scan_service
from app.storage import QualifyingDaysStore

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _scan(ticker, current_state=None, qualifying_days_count=0, price_move_pct=None):
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    stock_state = adapter.get_stock_state(ticker)
    if price_move_pct is None and stock_state:
        price_move_pct = stock_state.get("change_percent")
    return scan_service.evaluate_ticker_scan(
        ticker=ticker,
        flow_alerts=adapter.get_flow_alerts(ticker),
        darkpool_trades=adapter.get_darkpool_trades(ticker),
        current_state=current_state,
        qualifying_days_count=qualifying_days_count,
        price_move_pct=price_move_pct,
        catalyst_identified=adapter.get_catalyst_identified(ticker),
    )


class TestConflictingPriceConfirmation:
    def test_confl_price_confirmation_is_genuinely_conflicting(self):
        """CONFL: bullish-lean options flow, but the stock fell -- the real
        engine must compute metrics.confirmed === False (not None/True)."""
        scan = _scan("CONFL")
        pc = scan["gates"]["price_confirmation"]
        assert pc["metrics"]["direction_heuristic"] == "bullish-lean"
        assert pc["metrics"]["price_move_pct"] == -1.8
        assert pc["metrics"]["confirmed"] is False
        assert pc["passed"] is False
        # not accidentally invalidated -- this must stay a live, visible
        # CONFLICTING case, not fall into the separate INVALIDATED state.
        assert scan["state"] != "INVALIDATED"

    def test_confl_options_flow_anomaly_itself_passed(self):
        scan = _scan("CONFL")
        assert scan["gates"]["options_flow_anomaly"]["passed"] is True


class TestPersistenceProgression:
    def test_seed_then_live_scan_stays_building_not_falsely_confirmed(self):
        """Full, real, multi-day trajectory: seed two backdated real scans
        (NEW then BUILDING), then a third live scan.

        SIGNAL-INTEGRITY RULE CHANGE (2026-10-15): PRSX's fixture is a
        SINGLE independent category (dark_pool_anomaly only -- see
        fixtures/scenario_persistence_progression.json's own description).
        Before this rule change, the old `qualifying_days_count >= 2`
        bypass in determine_transition promoted this straight to CONFIRMED
        on the live scan -- exactly the "single real gate seen on two
        different calendar dates masquerading as 2 independent categories"
        loophole Chad's rule change closes. The live scan must now stay
        BUILDING: multi_signal_confirmation never passes for PRSX (only
        1 of INDEPENDENT_SIGNAL_GATES fires), so there is no genuine
        corroboration to promote it."""
        from app.seed_demo_history import seed_persistence_progression

        store = QualifyingDaysStore(":memory:")
        seeded_state = seed_persistence_progression(store, str(FIXTURES_DIR))
        assert seeded_state == "BUILDING"
        history = store.history("PRSX")
        assert len(history) == 2
        assert {h["scan_date"] for h in history} == {
            h["scan_date"] for h in history
        }  # sanity: two rows
        assert len({h["scan_date"] for h in history}) == 2  # two DISTINCT dates
        assert [h["state"] for h in history] == ["NEW", "BUILDING"]

        # Now the live third scan, exactly as main.py._run_scan does it.
        current_state = store.current_state("PRSX")
        qualifying_days = store.qualifying_days_count("PRSX")
        assert current_state == "BUILDING"
        assert qualifying_days == 2
        live_scan = _scan("PRSX", current_state=current_state,
                           qualifying_days_count=qualifying_days)
        assert live_scan["gates"]["multi_signal_confirmation"]["passed"] is False
        assert live_scan["state"] == "BUILDING"
        assert live_scan["transition_rule"] == "still_single_signal"

    def test_seeding_is_idempotent(self):
        from app.seed_demo_history import seed_persistence_progression

        store = QualifyingDaysStore(":memory:")
        first = seed_persistence_progression(store, str(FIXTURES_DIR))
        second = seed_persistence_progression(store, str(FIXTURES_DIR))
        assert first == second == "BUILDING"
        assert len(store.history("PRSX")) == 2  # not seeded twice

    def test_volx_also_gets_genuine_seeded_multi_entry_history(self):
        """2026-10-15 (Chad's Phase B final verification, item #2):
        test_p5_p6_p7.mjs's VOLX multi-entry-history check was previously
        only passing by accident, against a backend that had accumulated
        real repeated scans -- a fresh :memory: store never had any VOLX
        history at all. This locks in the fix: VOLX is now in
        SEED_TICKERS and gets the same real backdated-scan treatment as
        PRSX, deterministically, on every fresh store."""
        from app.seed_demo_history import seed_persistence_progression

        store = QualifyingDaysStore(":memory:")
        seed_persistence_progression(store, str(FIXTURES_DIR))
        history = store.history("VOLX")
        assert len(history) == 2, "VOLX should have 2 genuinely distinct seeded scan_date rows"
        assert len({h["scan_date"] for h in history}) == 2

    def test_volx_seeding_is_also_idempotent(self):
        from app.seed_demo_history import seed_persistence_progression

        store = QualifyingDaysStore(":memory:")
        seed_persistence_progression(store, str(FIXTURES_DIR))
        seed_persistence_progression(store, str(FIXTURES_DIR))
        assert len(store.history("VOLX")) == 2  # not seeded twice


class TestMultiSignalAltCombination:
    def test_volx_multi_signal_without_dark_pool(self):
        """SIGNAL-INTEGRITY RULE CHANGE (2026-10-15): VOLX's fixture was
        tuned to trip options_flow_anomaly + historical_percentile (the
        SYNTHETIC gate). historical_percentile is now EXCLUDED from
        multi_signal_confirmation's tally (zero weight in any decision --
        see signal_engine.py docstring), so VOLX is correctly only a
        single real category now (options_flow_anomaly alone) -- medium
        confidence, not high/multi-signal. This is the intended behavior
        change, not a regression: a synthetic-gate-backed "multi-signal"
        reading must not survive this rule change."""
        scan = _scan("VOLX")
        assert scan["gates"]["historical_percentile"]["passed"] is True  # still computed/shown
        assert scan["gates"]["dark_pool_anomaly"]["passed"] is False
        assert scan["gates"]["options_flow_anomaly"]["passed"] is True
        assert scan["gates"]["multi_signal_confirmation"]["passed"] is False
        assert scan["signal_type"] == "single-signal-options-flow"
        assert scan["confidence"] == "medium"
        assert scan["qualifies"] is True


class TestOptionsOnlyBearish:
    def test_brnx_matches_zync_shaped_single_signal_behavior(self):
        """BRNX is options_flow_anomaly + a confirmed price_confirmation,
        no dark pool. price_confirmation is excluded from
        INDEPENDENT_SIGNAL_GATES (its direction input is sourced from the
        same options-flow feed -- see signal_engine.py docstring), so this
        correctly stays single-category: options-flow alone never
        independently confers CONFIRMED/high confidence (same real-engine
        finding as ZYNC), even when price corroborates its own direction."""
        scan = _scan("BRNX")
        assert scan["gates"]["options_flow_anomaly"]["passed"] is True
        # Signal-integrity remediation (direction): BRNX's fixture flow is
        # $2.2M put premium at the BID (put SELLING) vs $0.2M at the ask.
        # The old combined-ask-share heuristic called that "bearish"; put
        # selling is not bearish, and selling-dominated flow has no
        # reliable lean -- see core/flow_direction.py.
        assert scan["gates"]["price_confirmation"]["metrics"]["direction_heuristic"] is None
        assert "selling" in scan["gates"]["price_confirmation"]["metrics"]["direction_ambiguity"]
        assert scan["gates"]["price_confirmation"]["metrics"]["confirmed"] is None
        assert scan["gates"]["multi_signal_confirmation"]["passed"] is False
        assert scan["confidence"] == "medium"
        assert scan["signal_type"] == "single-signal-options-flow"
        assert scan["qualifies"] is True


class TestDarkPoolWithDirection:
    def test_tepid_single_signal_with_evaluated_price_confirmation(self):
        """SIGNAL-INTEGRITY RULE CHANGE: a single real gate -- even a
        strong dark-pool ratio -- now caps at medium confidence, never
        high/CONFIRMED-eligible on its own (price_confirmation passing
        alongside it does not add a second INDEPENDENT category -- see
        signal_engine.py docstring)."""
        scan = _scan("TEPID")
        assert scan["gates"]["dark_pool_anomaly"]["passed"] is True
        assert scan["gates"]["options_flow_anomaly"]["passed"] is False  # below $1M threshold
        assert scan["gates"]["historical_percentile"]["passed"] is False  # stays single-signal
        assert scan["gates"]["multi_signal_confirmation"]["passed"] is False
        assert scan["gates"]["price_confirmation"]["metrics"]["direction_heuristic"] == "bullish-lean"
        # The lean is reported, but price_confirmation is only evaluated for
        # QUALIFYING options flow (TEPID's is below the options threshold).
        assert scan["gates"]["price_confirmation"]["metrics"]["confirmed"] is None
        assert scan["confidence"] == "medium"
        assert scan["signal_type"] == "single-signal-dark-pool"


class TestCatalystIdentified:
    def test_catx_catalyst_flag_flows_from_fixture_through_adapter_to_gate(self):
        """CATX is options_flow_anomaly + a manually-verified catalyst_identified
        -- two genuinely independent real categories (different data sources:
        options flow vs. a curated real-world catalyst), so this is a real
        "2 independent categories CAN reach CONFIRMED/high" demonstration."""
        adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
        assert adapter.get_catalyst_identified("CATX") is True
        # every other scenario defaults to False -- never auto-detected.
        assert adapter.get_catalyst_identified("ZYNC") is False
        assert adapter.get_catalyst_identified("FLUX") is False

        scan = _scan("CATX")
        assert scan["gates"]["catalyst_identified"]["passed"] is True
        assert scan["gates"]["options_flow_anomaly"]["passed"] is True
        assert scan["gates"]["multi_signal_confirmation"]["passed"] is True
        assert set(scan["gates"]["multi_signal_confirmation"]["metrics"]["contributing_gates"]) == {
            "options_flow_anomaly", "catalyst_identified",
        }
        assert scan["confidence"] == "high"
        assert scan["signal_type"] == "multi-signal"


def test_all_eleven_scenarios_run_through_real_engine_without_crashing():
    adapter = SyntheticDataAdapter(str(FIXTURES_DIR))
    universe = adapter.get_universe()
    assert len(universe) == 11
    results = {t: _scan(t) for t in universe}
    distinct_signal_types = {r["signal_type"] for r in results.values()}
    # Real finding under the SIGNAL-INTEGRITY RULE CHANGE (2026-10-15,
    # re-verified against all 11 real fixtures, not assumed): with
    # historical_percentile/persistence/price_confirmation excluded from
    # INDEPENDENT_SIGNAL_GATES, several scenarios that used to read as
    # "multi-signal" (VOLX, BRNX, TEPID) are now correctly single-category.
    assert distinct_signal_types == {
        "multi-signal", "single-signal-dark-pool", "single-signal-options-flow",
        "t1-bootstrap", "none",
    }
