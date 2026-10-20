"""Shared adapter interface. SyntheticDataAdapter and LiveUWAdapter both
implement this so backend/app/main.py (and backend/app/core/scan_service.py)
run identically regardless of TIP_MODE. Field names returned by every method
match the real Unusual Whales API response shapes documented in each
adapter, not a simplified version of them -- that's what lets the same
core/signal_engine.py gate logic run unmodified on either one.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

# --- Collection status (signal-integrity remediation, audit B4/E9) ---------
#
# A provider failure (HTTP 429/5xx, timeout, malformed body, unentitled or
# unauthorized endpoint, open circuit breaker) must NEVER be indistinguishable
# from "UW answered and there was genuinely nothing". Every collect_* method
# returns one of these dicts; only status == "ok" means the rows are a real,
# complete answer from the provider.
COLLECTION_OK = "ok"
COLLECTION_FAILURE_STATUSES = (
    "error", "rate_limited", "unauthorized", "unavailable_on_tier", "malformed", "circuit_open",
)


def collection_result(status: str, rows: Optional[list] = None, record: Optional[dict] = None,
                      detail: Optional[str] = None, http_status: Optional[int] = None,
                      source: Optional[str] = None, dropped_rows: int = 0) -> dict:
    return {
        "status": status,
        "ok": status == COLLECTION_OK,
        "rows": list(rows or []),
        "record": record,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
        "http_status": http_status,
        "source": source,
        "dropped_rows": dropped_rows,
    }


def collection_summary(result: dict) -> dict:
    """The persisted/API-facing view of one collection: never the rows."""
    return {
        "status": result.get("status"),
        "ok": bool(result.get("ok")),
        "retrieved_at": result.get("retrieved_at"),
        "row_count": len(result.get("rows") or []) if result.get("record") is None else (1 if result.get("record") else 0),
        "detail": result.get("detail"),
        "http_status": result.get("http_status"),
        "source": result.get("source"),
        "dropped_rows": result.get("dropped_rows", 0),
        **({"coverage": result["coverage"], "pages": result.get("pages")} if "coverage" in result else {}),
    }


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

    @abstractmethod
    def get_ohlc(self, ticker: str, timeframe: str = "1d", limit: int = 180,
                  window: Optional[str] = None) -> dict:
        """Candlestick bars for `ticker`. `window` (Phase B, 2026-10-14) is
        UW's own date-window selector ("1M"/"3M"/"6M"/"1Y" etc, their real
        "timeframe" query parameter -- distinct from the `timeframe`
        candle-size argument above) -- None means "adapter default", a
        synthetic adapter can ignore it entirely. Returns
        {"candles": [{"t","o","h","l","c","v"[,"v_raw"]}], "source": str,
        "negative_volume_bars_clamped": int, "unavailable": bool,
        "reason": Optional[dict]}. `unavailable` is True (never an
        exception) when the adapter genuinely has no OHLC data -- an
        unentitled live key, or a synthetic scenario with no captured
        series -- so callers degrade gracefully instead of guessing."""
        raise NotImplementedError

    def discover_candidates(self, filters: Optional[dict] = None) -> dict:
        """Market Discovery (C2b/C2c, 2026-10-15): ONE bounded, user-
        triggered stock-screener call -- never auto-paginated, never
        looped, never a per-ticker follow-up. `filters` accepts a subset
        of UW's real `/api/screener/stocks` filter vocabulary
        (min_premium, min_call_volume, min_put_volume, min_iv_rank,
        min_change, max_change, limit) -- a caller-supplied `limit` is
        ALWAYS clamped server-side to DISCOVERY_LIMIT_CAP by the
        implementation, never trusted as-is. Returns
        {"candidates": [raw screener row dicts, real UW field names],
        "snapshot_at": ISO str, "screener_requests_used": 1,
        "source": str}. A 'candidate' here is a RAW screener row --
        zero gate evaluation has happened; it must never be labeled
        'evaluated' or carry a signal_engine state. Evaluating one
        candidate is a SEPARATE, explicit action that reuses the
        existing get_flow_alerts/get_darkpool_trades/get_stock_state +
        scan_service pipeline for that one ticker -- this method itself
        does none of that."""
        raise NotImplementedError

    # --- status-carrying collection (preferred by the scan pipeline) -----
    # Defaults wrap the list-returning methods above for adapters (the
    # synthetic/demo one) whose data source cannot fail transiently.
    collection_source = "adapter"

    def collect_flow_alerts(self, ticker: str) -> dict:
        return collection_result(COLLECTION_OK, rows=self.get_flow_alerts(ticker), source=self.collection_source)

    def collect_darkpool_trades(self, ticker: str) -> dict:
        return collection_result(COLLECTION_OK, rows=self.get_darkpool_trades(ticker), source=self.collection_source)

    def collect_stock_state(self, ticker: str) -> dict:
        record = self.get_stock_state(ticker)
        if isinstance(record, dict) and record.get("unavailable"):
            return collection_result("error", detail="stock state unavailable", source=self.collection_source)
        return collection_result(COLLECTION_OK, record=record, source=self.collection_source)

    def get_catalyst_identified(self, ticker: str) -> bool:
        """Whether a real catalyst has been explicitly identified for this
        ticker. NOT abstract -- defaults to False for every adapter,
        because signal_engine.py's catalyst_identified gate is, by its own
        docstring, 'not auto-computed -- set explicitly when a real
        catalyst is found'. Neither SyntheticDataAdapter's fixtures nor
        LiveUWAdapter's real UW calls can auto-detect a catalyst; only
        SyntheticDataAdapter overrides this (reading an optional,
        manually-curated fixture field) to demonstrate the gate at all.
        LiveUWAdapter inherits this default (always False) until/unless a
        real editorial catalyst-flagging workflow is added -- never fabricate
        auto-detection here."""
        return False

    def health(self) -> dict:
        return {"mode": getattr(self, "mode", "unknown")}
