"""Shared adapter interface. SyntheticDataAdapter and LiveUWAdapter both
implement this so backend/app/main.py (and backend/app/core/scan_service.py)
run identically regardless of TIP_MODE. Field names returned by every method
match the real Unusual Whales API response shapes documented in each
adapter, not a simplified version of them -- that's what lets the same
core/signal_engine.py gate logic run unmodified on either one.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class UWDataAdapter(ABC):
    @abstractmethod
    def get_universe(self) -> list:
        """Tickers this adapter currently has data for / is watching."""
        raise NotImplementedError

    @abstractmethod
    def get_flow_alerts(self, ticker: str) -> list:
        """UW option-trades/flow-alerts rows for `ticker`. Each row shaped
        like the real UW API: total_premium, total_ask_premium,
        total_bid_premium, has_sweep, has_multileg, underlying_price, ..."""
        raise NotImplementedError

    @abstractmethod
    def get_darkpool_trades(self, ticker: str) -> list:
        """UW darkpool/{ticker} rows: premium, size, price, executed_at, ..."""
        raise NotImplementedError

    @abstractmethod
    def get_stock_state(self, ticker: str) -> Optional[dict]:
        """UW stock-state snapshot (last price, change %), or None/unavailable
        structure if the adapter couldn't get one."""
        raise NotImplementedError

    def health(self) -> dict:
        return {"mode": getattr(self, "mode", "unknown")}
