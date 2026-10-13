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

    def __init__(self, fixtures_dir: str):
        self.fixtures_dir = Path(fixtures_dir)
        self._scenarios = {}
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

    def reload(self):
        self._scenarios = {}
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

    def health(self) -> dict:
        return {"mode": self.mode, "scenarios_loaded": len(self._scenarios),
                "fixtures_dir": str(self.fixtures_dir)}
