"""SyntheticDataAdapter: feeds hand-built fixture JSON (shaped exactly like
real UW API responses) through the SAME base.UWDataAdapter interface
LiveUWAdapter implements, so backend/app/core/scan_service.py's gate logic
runs for real on this fake data -- it is not a hardcoded conclusion, see
fixtures/README_SYNTHETIC.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .base import UWDataAdapter


class SyntheticDataAdapter(UWDataAdapter):
    mode = "demo"
    collection_source = "synthetic_fixture"

    def __init__(self, fixtures_dir: str):
        self.fixtures_dir = Path(fixtures_dir)
        self._scenarios = {}
        self._discovery_candidates = []
        self._load()

    def _load(self):
        if not self.fixtures_dir.exists():
            raise FileNotFoundError(f"fixtures dir not found: {self.fixtures_dir}")
        for path in sorted(self.fixtures_dir.glob("scenario_*.json")):
            data = json.loads(path.read_text())
            ticker = data.get("ticker")
            if not ticker:
                continue
            self._scenarios[ticker.upper()] = data
        discovery_path = self.fixtures_dir / "discovery_candidates.json"
        if discovery_path.exists():
            self._discovery_fixture = json.loads(discovery_path.read_text())
            self._discovery_candidates = list(self._discovery_fixture.get("candidates", []))
        else:
            self._discovery_fixture = {}
            self._discovery_candidates = []

    def reload(self):
        self._scenarios = {}
        self._discovery_candidates = []
        self._load()

    def get_universe(self) -> list:
        return sorted(self._scenarios.keys())

    def list_scenarios(self) -> list:
        """Extra helper (not part of base interface): scenario metadata for
        the frontend to show e.g. 'call-side sweep' / 'dark-pool anomaly'."""
        return [
            {"ticker": t, "scenario_name": d.get("scenario_name"), "description": d.get("description")}
            for t, d in sorted(self._scenarios.items())
        ]

    def get_flow_alerts(self, ticker: str) -> list:
        scenario = self._scenarios.get(ticker.upper())
        return list(scenario.get("flow_alerts", [])) if scenario else []

    def get_darkpool_trades(self, ticker: str) -> list:
        scenario = self._scenarios.get(ticker.upper())
        return list(scenario.get("darkpool_trades", [])) if scenario else []

    def get_stock_state(self, ticker: str) -> Optional[dict]:
        scenario = self._scenarios.get(ticker.upper())
        return dict(scenario["stock_state"]) if scenario and scenario.get("stock_state") else None

    def get_catalyst_identified(self, ticker: str) -> bool:
        """Reads an optional, manually-curated \"catalyst_identified\" key
        from the fixture (see fixtures/scenario_catalyst_identified.json).
        Absent on every other fixture -> defaults to False, same as the
        base class. This is a fixture-authored demonstration value, never
        computed by this adapter."""
        scenario = self._scenarios.get(ticker.upper())
        return bool(scenario.get("catalyst_identified")) if scenario else False

    def get_ohlc(self, ticker: str, timeframe: str = "1d", limit: int = 180,
                 window: Optional[str] = None) -> dict:
        """Returns the scenario's pre-authored `ohlc` series, if the fixture
        has one. No synthesis happens here -- a scenario fixture with no
        `ohlc` key (or an unknown ticker) honestly reports `unavailable`
        rather than fabricating bars on the fly. `window` is accepted for
        signature parity with LiveUWAdapter (Phase B, 2026-10-14) but
        intentionally unused -- a fixed pre-authored fixture has nothing
        to window into; `limit` already does the real trimming below."""
        scenario = self._scenarios.get(ticker.upper())
        series = scenario.get("ohlc") if scenario else None
        if not series:
            return {
                "candles": [], "source": "synthetic_fixture",
                "negative_volume_bars_clamped": 0, "unavailable": True,
                "reason": {"message": f"No captured OHLC fixture for {ticker.upper()}"},
            }
        from ..core.candles import build_candles
        candles, _neg = build_candles(series, dedupe_daily=(timeframe == "1d"))
        candles = candles[-int(limit or 180):]
        return {
            "candles": candles, "source": "synthetic_fixture",
            "negative_volume_bars_clamped": 0, "unavailable": False, "reason": None,
        }

    def discover_candidates(self, filters: Optional[dict] = None) -> dict:
        """Market Discovery (C2b/C2c, 2026-10-15): serves the fabricated
        fixtures/discovery_candidates.json rows, with the SAME filter
        semantics LiveUWAdapter applies to the real screener response
        (so the frontend's filter form behaves identically in both
        modes) and the same server-side limit cap. Exactly one 'request'
        -- there is no network call to bound here, but
        screener_requests_used is still reported as 1 for UI parity."""
        from .live_uw_adapter import DISCOVERY_LIMIT_CAP

        filters = filters or {}
        requested_limit = filters.get("limit", DISCOVERY_LIMIT_CAP)
        try:
            requested_limit = int(requested_limit)
        except (TypeError, ValueError):
            requested_limit = DISCOVERY_LIMIT_CAP
        limit = max(1, min(requested_limit, DISCOVERY_LIMIT_CAP))

        def _passes(c: dict) -> bool:
            checks = [
                (filters.get("min_premium"), c.get("net_premium"), ">="),
                (filters.get("min_call_volume"), c.get("call_volume"), ">="),
                (filters.get("min_put_volume"), c.get("put_volume"), ">="),
                (filters.get("min_iv_rank"), c.get("iv_rank"), ">="),
                (filters.get("min_change"), c.get("change"), ">="),
                (filters.get("max_change"), c.get("change"), "<="),
            ]
            for threshold, value, op in checks:
                if threshold is None:
                    continue
                if value is None:
                    return False
                if op == ">=" and not (float(value) >= float(threshold)):
                    return False
                if op == "<=" and not (float(value) <= float(threshold)):
                    return False
            return True

        filtered = [c for c in self._discovery_candidates if _passes(c)]
        candidates = filtered[:limit]
        snapshot_at = self._discovery_fixture.get("snapshot_at") if self._discovery_fixture else None
        return {
            "candidates": candidates, "snapshot_at": snapshot_at, "screener_requests_used": 1,
            "source": "synthetic_discovery_fixture", "unavailable": False, "reason": None,
        }

    def health(self) -> dict:
        return {"mode": self.mode, "scenarios_loaded": len(self._scenarios),
                "fixtures_dir": str(self.fixtures_dir)}
