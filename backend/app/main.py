"""FastAPI app wiring Market Radar / Ticker Analysis routes to the ported
core logic via whichever adapter TIP_MODE selects.

TIP_MODE=demo (default) -> SyntheticDataAdapter over fixtures/*.json
TIP_MODE=live            -> LiveUWAdapter over the real UW API (needs
                             UW_API_KEY; see .env.example)

Routes are intentionally thin: they fetch raw data from the adapter, run it
through backend/app/core/scan_service.py (which is where the real gate /
confidence / state-transition decision happens), persist the scan result to
the local SQLite store for qualifying-days counting, and format it with
backend/app/core/tip_evidence.py for display. No business logic lives here.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

from pathlib import Path

# REAL BUG FIX (2026-10-14): discovered while diagnosing the NVDA/PLTR
# synthesis-truncation report -- app.ai.orchestrator's and
# app.ai.llm_provider's logger.info(...) diagnostic lines (tool-selection
# round completed, tool execution N/M succeeded, etc.) were being
# silently swallowed. Nothing in this app ever called logging.basicConfig,
# so those loggers had no handler and fell back to Python's "last resort"
# handler, which only shows WARNING and above -- the exact INFO-level
# lines needed to diagnose which round/stage failed were invisible in
# `docker logs`. uvicorn's own request-logging keeps working either way;
# this just makes our OWN app loggers actually emit INFO to the same
# stream uvicorn already writes to stdout.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adapters.base import UWDataAdapter
from .adapters.live_uw_adapter import ERROR, UNAUTHORIZED, UNAVAILABLE, LiveUWAdapter, UWRestClient
from .adapters.synthetic_adapter import SyntheticDataAdapter
from .ai import orchestrator as ai_orchestrator
from .ai import llm_provider as ai_llm_provider
from .ai import mcp_tool_client as ai_mcp_tool_client
from .ai.llm_provider import LLMCallError, LLMTruncatedOutputError
from .ai.mcp_tool_client import MCPSubprocessError
from .ai.schemas import MalformedAIResponse
from .ai.simulated import get_simulated_transcript
from .ai.validation import ValidationError as AiValidationError
from .config import settings
from .core import scan_service, tip_evidence
from .core.candles import build_markers
from .settings_store import MAX_WATCHLIST_SIZE, SettingsStore, last4
from .storage import QualifyingDaysStore

app = FastAPI(title="TIP Hackathon Edition", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_adapter: Optional[UWDataAdapter] = None
_adapter_sig: Optional[tuple] = None
_store: Optional[QualifyingDaysStore] = None
_settings_store = SettingsStore(os.environ.get("TIP_SETTINGS_PATH", "./data/settings.json"))
_last_uw_test_result: Optional[str] = None  # in-memory only, informational; never persisted


def _effective_uw_key(store_data: dict) -> tuple[Optional[str], str]:
    """A UI-saved key always wins over the .env/environment fallback value
    (see config.py docstring + README). Returns (key_or_None, source)."""
    ui_key = store_data.get("uw_api_key")
    if ui_key:
        return ui_key, "ui"
    if settings.uw_api_key:
        return settings.uw_api_key, "env"
    return None, "none"


def _reset_adapter_cache() -> None:
    global _adapter, _adapter_sig
    _adapter = None
    _adapter_sig = None


def get_adapter() -> UWDataAdapter:
    global _adapter, _adapter_sig
    data = _settings_store.get_all()
    mode = data["mode"]
    key, _source = _effective_uw_key(data)
    # Market Radar (2026-10-14, Phase A): the watchlist is now user-
    # editable and persisted (SettingsStore.get_watchlist(), falling back
    # to TIP_LIVE_WATCHLIST only as the seed default) -- included in the
    # adapter cache signature so saving a new watchlist actually takes
    # effect on the NEXT /api/radar call instead of silently continuing
    # to scan the old one until some unrelated cache-busting event.
    watchlist = tuple(_settings_store.get_watchlist(default=settings.live_watchlist))
    sig = (mode, key, watchlist)
    if _adapter is None or _adapter_sig != sig:
        if mode != "live":
            _adapter = SyntheticDataAdapter(settings.fixtures_dir)
        else:
            _adapter = LiveUWAdapter(
                api_key=key or "",
                base_url=settings.uw_base_url,
                client_api_id=settings.uw_client_api_id,
                watchlist=list(watchlist),
            )
        _adapter_sig = sig
    return _adapter


def get_store() -> QualifyingDaysStore:
    global _store
    if _store is None:
        _store = QualifyingDaysStore(settings.db_path)
        # Backfill the one demo scenario (PRSX) that needs genuinely
        # distinct historical scan_date rows to show a real multi-day
        # BUILDING -> CONFIRMED trajectory -- see seed_demo_history.py's
        # module docstring. Idempotent, non-fatal (demo-only convenience;
        # never block app startup on it), and runs on real fixture data
        # through the real scan_service, not a hardcoded state.
        try:
            from .seed_demo_history import seed_persistence_progression
            seed_persistence_progression(_store, settings.fixtures_dir)
        except Exception:
            pass
    return _store


def _price_move_pct(stock_state: Optional[dict]) -> Optional[float]:
    if not stock_state or stock_state.get("unavailable"):
        return None
    for key in ("change_percent", "chg_pct", "percent_change"):
        if stock_state.get(key) is not None:
            return float(stock_state[key])
    return None


def _run_scan(adapter: UWDataAdapter, store: QualifyingDaysStore, ticker: str) -> dict:
    flow_alerts = adapter.get_flow_alerts(ticker)
    darkpool_trades = adapter.get_darkpool_trades(ticker)
    stock_state = adapter.get_stock_state(ticker)
    current_state = store.current_state(ticker)
    qualifying_days = store.qualifying_days_count(ticker)
    result = scan_service.evaluate_ticker_scan(
        ticker=ticker,
        flow_alerts=flow_alerts,
        darkpool_trades=darkpool_trades,
        current_state=current_state,
        qualifying_days_count=qualifying_days,
        price_move_pct=_price_move_pct(stock_state),
        catalyst_identified=adapter.get_catalyst_identified(ticker),
    )
    any_gate_passed = len(result["gates_passed"]) > 0
    store.record_scan(ticker, any_gate_passed, result["state"])
    result["stock_state"] = stock_state
    result["raw"] = {"flow_alerts": flow_alerts, "darkpool_trades": darkpool_trades}
    return result


def _to_evidence_item(scan: dict) -> dict:
    """Shape one scan result into the dict backend/app/core/tip_evidence.py
    expects (see its module docstring for the schema)."""
    stock_state = scan.get("stock_state") or {}
    chg = _price_move_pct(stock_state)
    dp_metrics = scan["gates"]["dark_pool_anomaly"]["metrics"]
    return {
        "ticker": scan["ticker"],
        "state": scan["state"],
        "gates_passed": scan["gates_passed"],
        "dp": {"ratio": dp_metrics.get("ratio"), "hist": None, "prints": None} if dp_metrics.get("ratio") else None,
        "options": None,
        "options_partial": False,
        "price": {"chg_pct": chg, "last": stock_state.get("last_price")} if stock_state else None,
        "alerts": None,
        "context": None,
        "next_earnings_date": None,
        "research": [],
    }


@app.get("/api/health")
def health():
    adapter = get_adapter()
    return {"ok": True, "mode": _settings_store.get_mode(), "adapter": adapter.health()}


@app.get("/api/mode")
def mode():
    return {"mode": _settings_store.get_mode()}


# --- Settings / credentials -------------------------------------------
#
# These routes are the only place UW/AI API keys are read or written.
# Keys are NEVER included in a response body -- only a `configured`
# boolean and the key's last 4 characters (for user recognition) are ever
# returned. See settings_store.py for the on-disk format and
# README "Credential precedence" for the .env-vs-UI-key precedence rule.


class ModeBody(BaseModel):
    mode: str


class UwKeyBody(BaseModel):
    api_key: str


class AiKeyBody(BaseModel):
    provider: str
    api_key: str


class ConfirmBody(BaseModel):
    # Phase 4B: AI provider test is a REAL, individually-billed API call
    # (unlike MCP test, which is UW-quota-only) -- confirm=true is a
    # structural guard against it ever firing from anything but an
    # explicit user click that has already seen the cost disclosure in
    # the UI, not just a disabled-by-default frontend button.
    confirm: bool = False


@app.get("/api/settings")
def get_settings():
    data = _settings_store.get_all()
    uw_key, uw_source = _effective_uw_key(data)
    ai_key = data.get("ai_api_key")
    # Disclosure for item 10: if the UI-saved key was removed but a
    # .env-level key is still configured, the effective key silently
    # becomes that env key -- say so explicitly rather than letting the
    # user believe "Remove Saved Key" cleared all credentials.
    env_key_present = bool(settings.uw_api_key)
    ui_key_present = bool(data.get("uw_api_key"))
    return {
        "mode": data["mode"],
        "public_demo": settings.public_demo,
        "uw": {
            "configured": bool(uw_key),
            "source": uw_source,  # "ui" | "env" | "none"
            "last4": last4(uw_key),
            "last_test_result": _last_uw_test_result,
            "verified": _settings_store.is_key_verified(uw_key),
            "env_fallback_present": env_key_present,
            "env_fallback_disclosure": (
                "UI-saved key removed. A .env-configured key is still present and will be used."
                if env_key_present and not ui_key_present
                else None
            ),
        },
        "ai": {
            "configured": bool(ai_key),
            "provider": data.get("ai_provider"),
            "last4": last4(ai_key),
        },
        # Market Radar (2026-10-14, Phase A): included here too (not just
        # the dedicated /api/settings/watchlist GET) so the Settings page
        # can render the watchlist editor from the same /api/settings
        # call it already makes, without a second round-trip.
        "watchlist": {
            "tickers": _settings_store.get_watchlist(default=settings.live_watchlist),
            "max_size": MAX_WATCHLIST_SIZE,
        },
    }


class WatchlistBody(BaseModel):
    watchlist: list[str]


@app.get("/api/settings/watchlist")
def get_watchlist():
    """Market Radar (2026-10-14, Phase A): the persisted watchlist (or
    the env-seeded default if never saved), plus the size cap so the UI
    can disclose it rather than discovering it only on a rejected save."""
    wl = _settings_store.get_watchlist(default=settings.live_watchlist)
    return {"watchlist": wl, "max_size": MAX_WATCHLIST_SIZE, "default": settings.live_watchlist}


@app.put("/api/settings/watchlist")
def set_watchlist(body: WatchlistBody):
    """Whole-list replace (not incremental add/remove) -- simpler and
    atomic; the frontend maintains the current list client-side and PUTs
    the full updated array on every add/remove click. Resets the adapter
    cache immediately so the very next /api/radar scan reflects it."""
    try:
        data = _settings_store.set_watchlist(body.watchlist)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _reset_adapter_cache()
    return {"watchlist": data["watchlist"], "max_size": MAX_WATCHLIST_SIZE}


@app.post("/api/settings/mode")
def set_mode(body: ModeBody):
    requested = body.mode.strip().lower()
    if requested not in ("demo", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'demo' or 'live'")
    if requested == "live":
        if settings.public_demo:
            raise HTTPException(
                status_code=403,
                detail="Live mode is not available in the public demo build.",
            )
        key, _source = _effective_uw_key(_settings_store.get_all())
        if not key:
            raise HTTPException(
                status_code=400,
                detail="Cannot switch to live mode: no UW API key is configured yet.",
            )
        # Real server-side gate (not just a disabled UI button): live mode
        # may only be activated for a key that has ITSELF passed
        # /api/settings/uw-key/test -- applies identically whether the key
        # came from the UI store or the .env fallback (no source is
        # "trusted by default"). A key merely being present is not enough.
        if not _settings_store.is_key_verified(key):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot switch to live mode: this UW API key has not passed "
                    "Test Connection yet. Use Test Connection in Settings first."
                ),
            )
    data = _settings_store.set_mode(requested)
    _reset_adapter_cache()
    return {"mode": data["mode"]}


@app.post("/api/settings/uw-key")
def save_uw_key(body: UwKeyBody):
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Credential entry is not available in the public demo build.")
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    _settings_store.set_uw_key(key)
    _reset_adapter_cache()
    global _last_uw_test_result
    _last_uw_test_result = None
    return {"configured": True, "last4": last4(key)}


@app.post("/api/settings/uw-key/test")
def test_uw_key():
    """Real live test against the UW API using whichever key is currently
    effective (UI-saved, else .env fallback) -- not a stub. Never raises on
    a UW-side error; every branch returns a structured, keyless result."""
    global _last_uw_test_result
    data = _settings_store.get_all()
    key, source = _effective_uw_key(data)
    if not key:
        _last_uw_test_result = "not_configured"
        return {"result": "not_configured", "message": "No UW API key is configured.", "source": source}

    client = UWRestClient(
        api_key=key,
        base_url=settings.uw_base_url,
        client_api_id=settings.uw_client_api_id,
    )
    body = client.stock_info("SPY")  # cheap, cache-friendly endpoint used purely as a connectivity probe
    status = body.get("status") if isinstance(body, dict) else None
    if status == UNAUTHORIZED:
        result, message = "unauthorized", "Unusual Whales rejected this API key (401)."
    elif status == UNAVAILABLE:
        result, message = "forbidden_tier_limited", "This endpoint isn't available on the configured UW tier (403)."
    elif status == ERROR:
        result, message = "network_error", "Could not reach the Unusual Whales API."
    else:
        result, message = "success", "Connected to the Unusual Whales API."
    _last_uw_test_result = result
    if result == "success":
        # Persist a hash of THIS exact key as the one verified-live-capable
        # credential. This is the only place that ever sets the record --
        # a mere "key is present" never does. See settings_store.record_
        # verified_uw_key / is_key_verified and the live-mode gate above.
        _settings_store.record_verified_uw_key(key)
    return {"result": result, "message": message, "source": source}


@app.delete("/api/settings/uw-key")
def delete_uw_key():
    data = _settings_store.clear_uw_key()
    _reset_adapter_cache()
    global _last_uw_test_result
    _last_uw_test_result = None
    return {"configured": False, "mode": data["mode"]}


@app.post("/api/settings/ai-key")
def save_ai_key(body: AiKeyBody):
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Credential entry is not available in the public demo build.")
    provider = body.provider.strip().lower()
    if provider not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic' or 'openai'")
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    _settings_store.set_ai_key(provider, key)
    return {"configured": True, "provider": provider, "last4": last4(key)}


@app.delete("/api/settings/ai-key")
def delete_ai_key():
    _settings_store.clear_ai_key()
    return {"configured": False}


@app.post("/api/settings/ai-key/test")
def test_ai_key(body: ConfirmBody):
    """Phase 4B: minimal, bounded connectivity test for the SAVED AI
    provider key -- one short request, tiny token cap, no tool-selection
    or synthesis prompt. This is a REAL, individually-billed API call
    (unlike the UW/MCP test below), so it requires confirm=true from an
    explicit user action that has already seen the cost disclosure --
    never fired automatically. A success here proves ONLY that the
    credential+model answer a request; it is NOT evidence that MCP or any
    tool execution works (see /api/settings/mcp/test, which is separate
    and LLM-free) -- the response is intentionally plain so the frontend
    cannot accidentally conflate the two."""
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Credential testing is not available in the public demo build.")
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required -- this makes one real, individually-billed request to the configured AI provider.",
        )
    data = _settings_store.get_all()
    provider = data.get("ai_provider")
    key = data.get("ai_api_key")
    if not provider or not key:
        raise HTTPException(status_code=400, detail="No AI provider key is configured yet. Save one first.")
    result = ai_llm_provider.test_provider_connection(provider, key)
    return {**result, "provider": provider, "proves_mcp_works": False}


@app.post("/api/settings/mcp/test")
def test_mcp():
    """Phase 4B: standalone genuine MCP-protocol connectivity test
    (connect, authenticate, tool discovery, ONE bounded read-only tool
    call against a fixed test ticker) -- NO LLM call of any kind, so this
    does not touch your AI provider key or incur any AI provider cost; it
    does use one real, bounded UW API call against your existing UW key.
    A success here proves ONLY that MCP transport + UW auth work; it is
    NOT evidence that AI-directed tool selection/synthesis works (see
    /api/settings/ai-key/test, which is separate) -- the response is
    intentionally plain so the frontend cannot accidentally conflate the
    two."""
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Connectivity testing is not available in the public demo build.")
    data = _settings_store.get_all()
    uw_key, _source = _effective_uw_key(data)
    if not uw_key:
        raise HTTPException(status_code=400, detail="No UW API key is configured yet. Save and test one first.")
    result = ai_mcp_tool_client.test_mcp_connection(uw_key)
    return {**result, "proves_ai_synthesis_works": False}


@app.get("/api/radar")
def radar():
    """Market Radar: scan every ticker in the adapter's universe and return
    the ones that qualify (tip_evidence.qualifies), highest-attention first.

    Market Radar (2026-10-14, Phase A): records when this scan ran and
    how many tickers it covered (persisted -- survives container
    recreation), and explicitly discloses `scan_scope: "watchlist"` so
    the UI never implies broader-market discovery that isn't actually
    happening. UW's REST API does offer a genuine stock/options screener
    capable of real market-wide discovery (confirmed, not assumed) --
    that is NOT wired up here; this remains honest watchlist-only
    scanning until that's a separately-scoped feature."""
    adapter = get_adapter()
    store = get_store()
    universe = adapter.get_universe()
    if not universe:
        return {"highest": [], "other": [], "scanned": 0, "scan_scope": "watchlist", "last_scan_at": None}

    items = []
    scans_by_ticker = {}
    for ticker in universe:
        scan = _run_scan(adapter, store, ticker)
        scans_by_ticker[ticker] = scan
        items.append(_to_evidence_item(scan))

    highest, other = tip_evidence.present(items, next_session=date.today() + timedelta(days=1))

    def _attach(entry: dict) -> dict:
        s = scans_by_ticker[entry["ticker"]]
        # Pre-existing gap found while building the Stage 2 signal-state
        # filter: tip_evidence.present()'s entry dict has never carried a
        # "state" key at all (it only tracks state internally for
        # ordering/STATE_RANK) -- frontend/app.core.js's renderCard()/
        # renderRow() have always rendered `state-${entry.state}` against
        # an undefined value, so the state tag on every Market Radar
        # card/row has always been visually blank. Fixing here (real data
        # already computed in `scan`, just never attached) since the new
        # client-side state filter needs a real field to filter on.
        entry["state"] = s["state"]
        entry["confidence"] = s["confidence"]
        entry["signal_type"] = s["signal_type"]
        entry["qualifying_days_count"] = s["qualifying_days_count"]
        # Compact signal-history indicator (Stage 2 / P3): only non-empty
        # when this ticker genuinely has more than one real recorded scan
        # (store.history() -- never fabricated). On a freshly started
        # process that's only PRSX (seeded + one live scan = 3 real
        # entries); every other ticker stays [] until it's actually been
        # scanned more than once. The frontend only renders a chip when
        # this list has 2+ entries.
        hist = store.history(entry["ticker"])
        entry["history_states"] = [h["state"] for h in hist] if len(hist) > 1 else []
        return entry

    scan_record = _settings_store.record_scan(len(universe))
    return {
        "highest": [_attach(e) for e in highest],
        "other": [_attach(e) for e in other],
        "scanned": len(universe),
        # Honest disclosure (2026-10-14, Phase A): this is a scan of the
        # user's configured watchlist, never broader-market discovery --
        # see this function's docstring for why that distinction matters.
        "scan_scope": "watchlist",
        "last_scan_at": scan_record["last_scan_at"],
    }


class DiscoverFiltersBody(BaseModel):
    # Market Discovery (C2b/C2c, 2026-10-15): all optional named filters
    # over UW's real /api/screener/stocks fields -- no raw UW query DSL
    # exposed to the end user. `limit` is clamped server-side regardless
    # of what's sent here (see live_uw_adapter.DISCOVERY_LIMIT_CAP).
    min_premium: Optional[float] = None
    min_call_volume: Optional[int] = None
    min_put_volume: Optional[int] = None
    min_iv_rank: Optional[float] = None
    min_change: Optional[float] = None
    max_change: Optional[float] = None
    limit: int = 20  # also hard-clamped server-side by the adapter regardless of this value


def _rank_discovery_candidates(candidates: list) -> list:
    """Rank by |net_premium| magnitude primarily, repeated_hits as a
    tiebreaker -- a sensible, documented, non-mysterious ranking. Never
    alters the raw candidate dicts; only reorders them."""
    def _key(c: dict):
        return (abs(float(c.get("net_premium") or 0)), int(c.get("repeated_hits") or 0))
    return sorted(candidates, key=_key, reverse=True)


@app.post("/api/radar/discover")
def radar_discover(body: DiscoverFiltersBody):
    """Market Discovery (C2b/C2c): EXACTLY ONE bounded call to the
    adapter's discover_candidates per invocation -- no automatic
    pagination, no automatic per-ticker follow-up scan. Returns raw
    screener 'candidates' (state = zero gate evaluation has happened),
    ranked, plus a snapshot timestamp and an explicit quota-cost
    disclosure (screener_requests_used). A candidate only becomes
    'evaluated' when the user explicitly calls GET /api/ticker/{ticker}
    (the 'Evaluate in TIP' action) -- never automatically here."""
    adapter = get_adapter()
    filters = {
        "min_premium": body.min_premium,
        "min_call_volume": body.min_call_volume,
        "min_put_volume": body.min_put_volume,
        "min_iv_rank": body.min_iv_rank,
        "min_change": body.min_change,
        "max_change": body.max_change,
        "limit": body.limit,
    }
    result = adapter.discover_candidates(filters)
    candidates = _rank_discovery_candidates(result.get("candidates") or [])
    for c in candidates:
        # Explicit state model (item 1c of the Market Discovery spec):
        # a raw screener row is ALWAYS "candidate" here -- never
        # "evaluating"/"evaluated"/a signal_engine state. Those only
        # ever come from a real GET /api/ticker/{ticker} call.
        c["discovery_state"] = "candidate"
    return {
        "candidates": candidates,
        "count": len(candidates),
        "snapshot_at": result.get("snapshot_at"),
        "screener_requests_used": result.get("screener_requests_used", 1),
        "source": result.get("source"),
        "unavailable": result.get("unavailable", False),
        "reason": result.get("reason"),
        "scan_scope": "market_wide_screener",
        "limit_applied": body.limit,
        "limit_cap": 50,
    }


@app.get("/api/ticker/{ticker}")
def ticker_analysis(ticker: str):
    """Ticker Analysis: full evidence drill-down for one ticker -- gates,
    confidence, state transition, and the tip_evidence explanation.

    REAL BUG FIX (2026-10-14): this used to reject ANY ticker not already
    in adapter.get_universe() -- for LiveUWAdapter that's exactly
    TIP_LIVE_WATCHLIST (a short fixed list), so a live user could only
    ever analyze whatever 4 tickers happened to be configured there, with
    no way to type in a different real ticker. That watchlist exists to
    scope Market Radar's proactive scanning, not to gate direct lookup --
    UW's REST API will answer for any real ticker. Fix: the fixed-universe
    check now applies ONLY to the synthetic/demo adapter (where it's
    correct -- fixtures genuinely only exist for the 11 named scenarios);
    Live Mode instead validates the SYMBOL FORMAT (same TICKER_RE already
    used for AI Investigation) and lets the real UW call itself be the
    source of truth on whether that ticker actually exists/has data."""
    adapter = get_adapter()
    store = get_store()
    ticker = ticker.upper()
    if isinstance(adapter, SyntheticDataAdapter):
        if ticker not in adapter.get_universe():
            raise HTTPException(status_code=404, detail=f"{ticker} is not one of the demo scenarios")
    else:
        try:
            ticker = _validate_ticker_for_route(ticker)
        except AiValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    scan = _run_scan(adapter, store, ticker)
    item = _to_evidence_item(scan)
    return {
        "ticker": ticker,
        "state": scan["state"],
        "previous_state": scan["previous_state"],
        "transition_rule": scan["transition_rule"],
        "confidence": scan["confidence"],
        "signal_type": scan["signal_type"],
        "confidence_reason": scan["confidence_reason"],
        "qualifies": scan["qualifies"],
        "qualifying_days_count": scan["qualifying_days_count"],
        "gates": scan["gates"],
        "headline": tip_evidence.headline(item),
        "current_read": tip_evidence.current_read(item),
        "evidence": tip_evidence.evidence(item),
        "stock_state": scan.get("stock_state"),
        "history": store.history(ticker),
        "raw": scan["raw"],
    }


# Phase B (2026-10-14): maps a user-selectable chart range to an actual
# number of DAILY bars to request. UW's stock_ohlc endpoint takes a bar
# COUNT, not a calendar date range, so "1Y" means "request ~252 trading
# days of daily bars" -- these are standard US trading-day approximations
# (not exact, since holidays vary year to year), used only to decide how
# many bars to ask for; actual coverage is verified from what's returned
# (see range_fully_satisfied below), never assumed from this table alone.
RANGE_TO_TRADING_DAYS = {"1M": 22, "3M": 65, "6M": 130, "1Y": 252}

# 2026-10-15 (Chad-approved, item #3 -- 5D explicitly NOT included this
# pass): genuine intraday range. candle_size="5m" (distinct from the
# daily ranges' "1d") + UW's own timeframe=1D query param -- live-
# verified to return ~191 real 5-minute bars spanning pre-market through
# post-market for one real trading day (NOT a handful of daily bars
# relabeled). No raw-limit multiplier needed here: unlike daily bars,
# each intraday bucket already has its own distinct start_time, so there
# is no multi-session-per-timestamp collision to over-fetch around.
INTRADAY_RANGES = {"1D": {"candle_size": "5m", "limit": 200}}


@app.get("/api/ticker/{ticker}/ohlc")
def ticker_ohlc(ticker: str, timeframe: str = "1d", limit: int = 180, range: Optional[str] = None):
    """Candlestick bars + evidence markers for the Ticker Analysis chart.
    Markers come from the SAME flow_alerts/darkpool_trades the detection
    engine already uses -- never a new data source, never an invented
    price (see backend/app/core/candles.py::build_markers). `unavailable`
    is a structured, non-crashing result (e.g. an unentitled live key, or
    a demo scenario with no captured OHLC fixture) -- the frontend must
    show an explicit "chart data unavailable" state, not silently render
    nothing.

    Phase B (2026-10-14): optional `range` (1M/3M/6M/1Y) is a convenience
    that maps to `limit` via RANGE_TO_TRADING_DAYS and forces daily bars
    -- this is a REAL re-fetch requesting a different bar count, not a
    client-side zoom of a fixed dataset. `timeframe`/`limit` remain
    directly usable for any other caller (e.g. the marker-building logic
    elsewhere in this route already relies on the existing defaults)."""
    if range is not None:
        range = range.strip().upper()
        all_ranges = set(RANGE_TO_TRADING_DAYS) | set(INTRADAY_RANGES)
        if range not in all_ranges:
            raise HTTPException(status_code=400, detail=f"range must be one of {sorted(all_ranges)}")
        if range in INTRADAY_RANGES:
            timeframe = INTRADAY_RANGES[range]["candle_size"]
            limit = INTRADAY_RANGES[range]["limit"]
        else:
            timeframe = "1d"
            limit = RANGE_TO_TRADING_DAYS[range]
    adapter = get_adapter()
    ticker = ticker.upper()
    # Same real fix as ticker_analysis() above (2026-10-14): only the
    # demo/synthetic adapter enforces a fixed ticker universe (fixtures
    # genuinely only exist for the 11 named scenarios); Live Mode accepts
    # any format-valid ticker and lets the real UW call decide if it has
    # data, instead of hard-gating on TIP_LIVE_WATCHLIST.
    if isinstance(adapter, SyntheticDataAdapter):
        if ticker not in adapter.get_universe():
            raise HTTPException(status_code=404, detail=f"{ticker} is not one of the demo scenarios")
    else:
        try:
            ticker = _validate_ticker_for_route(ticker)
        except AiValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # `window` (Phase B final fix, 2026-10-14): UW's REAL date-window
    # selector -- our `range` strings ("1M"/"3M"/"6M"/"1Y") are already in
    # UW's own vocabulary for this parameter, so pass it straight through.
    # `limit` stays as a sane safety cap (RANGE_TO_TRADING_DAYS), not the
    # primary selector -- see live_uw_adapter.stock_ohlc()'s docstring for
    # the full bug writeup.
    ohlc = adapter.get_ohlc(ticker, timeframe=timeframe, limit=limit, window=range)
    flow_alerts = adapter.get_flow_alerts(ticker)
    darkpool_trades = adapter.get_darkpool_trades(ticker)
    markers = build_markers(flow_alerts, darkpool_trades)
    candles = ohlc.get("candles", [])
    # Phase B (2026-10-14) honest coverage disclosure -- production-
    # inspired (its own range_fully_satisfied/available_span_days
    # pattern): never claim a range is fully covered when UW simply
    # doesn't have that much history for this ticker (recent IPO, etc.).
    # `available_span_days` is measured from the REAL returned candles,
    # not assumed from the request -- a short-history ticker is reported
    # honestly rather than silently padded or mislabeled.
    earliest_candle = candles[0]["t"] if candles else None
    latest_candle = candles[-1]["t"] if candles else None
    available_span_days = None
    range_fully_satisfied = None
    if range is not None and range in INTRADAY_RANGES:
        # Intraday coverage is "did we get a real continuous session's
        # worth of 5m bars" (live-verified ~191 for a full day), not a
        # trading-day count -- a much looser floor (a half-day/holiday
        # genuinely has fewer bars; never treat that as a bug).
        range_fully_satisfied = len(candles) >= 50
    elif range is not None:
        expected_bars = RANGE_TO_TRADING_DAYS[range]
        # Allow a small tolerance for holidays/early closes -- 90% of the
        # expected trading-day COUNT, not a calendar-day comparison,
        # since that's what was actually requested (a bar count).
        range_fully_satisfied = len(candles) >= int(expected_bars * 0.9)
    if range is not None:
        if candles:
            try:
                from datetime import datetime
                d0 = datetime.fromisoformat(str(earliest_candle)[:10])
                d1 = datetime.fromisoformat(str(latest_candle)[:10])
                available_span_days = (d1 - d0).days
            except (ValueError, TypeError):
                available_span_days = None
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "range": range,
        "candles": candles,
        "source": ohlc.get("source"),
        "negative_volume_bars_clamped": ohlc.get("negative_volume_bars_clamped", 0),
        "unavailable": ohlc.get("unavailable", False),
        "reason": ohlc.get("reason"),
        "markers": markers,
        "candle_count": len(candles),
        "earliest_candle": earliest_candle,
        "latest_candle": latest_candle,
        "range_fully_satisfied": range_fully_satisfied,
        "available_span_days": available_span_days,
        # 2026-10-15 session-handling fix: real coverage disclosure, not
        # just the final candle count -- lets a caller verify (not just
        # trust) that the raw-record-to-unique-trading-day math actually
        # worked, per Chad's requirement to show this explicitly.
        "raw_records_requested": ohlc.get("raw_records_requested"),
        "raw_records_returned": ohlc.get("raw_records_returned"),
        "regular_session_records_retained": ohlc.get("regular_session_records_retained"),
    }


@app.get("/api/investigate/availability")
def investigate_availability():
    """Tells the frontend whether to show/enable the 'Investigate with AI'
    button, and why not, without triggering any network call. Demo mode is
    always available (it only ever serves a static simulated transcript);
    live mode additionally needs both an AI provider key and a UW key."""
    data = _settings_store.get_all()
    mode = data["mode"]
    if settings.public_demo or mode != "live":
        return {"available": True, "simulated": True, "reason": None}
    ai_key = data.get("ai_api_key")
    if not ai_key:
        return {
            "available": False,
            "simulated": False,
            "reason": "No AI provider key is configured. Add one in Settings to enable Investigate with AI.",
        }
    uw_key, _source = _effective_uw_key(data)
    if not uw_key:
        return {
            "available": False,
            "simulated": False,
            "reason": "Live mode requires a Unusual Whales API key (Settings) before Investigate with AI can run.",
        }
    return {"available": True, "simulated": False, "reason": None}


@app.post("/api/investigate/{ticker}")
def investigate_ticker(ticker: str):
    """Bounded 'Investigate with AI' for one ticker. Demo mode / PUBLIC_DEMO
    always returns a pre-written, clearly-labeled simulated transcript and
    NEVER calls a real LLM or UW endpoint. Live mode makes the real bounded
    MCP-allowlist-equivalent REST calls plus two LLM calls described in
    backend/app/ai/orchestrator.py, entirely from this local backend."""
    try:
        safe_ticker = _validate_ticker_for_route(ticker)
    except AiValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = _settings_store.get_all()
    mode = data["mode"]

    if settings.public_demo or mode != "live":
        return get_simulated_transcript(settings.fixtures_dir, safe_ticker)

    ai_provider = data.get("ai_provider")
    ai_key = data.get("ai_api_key")
    if not ai_key or not ai_provider:
        raise HTTPException(
            status_code=400,
            detail="No AI provider key is configured. Add one in Settings to use Investigate with AI.",
        )
    uw_key, _source = _effective_uw_key(data)
    if not uw_key:
        raise HTTPException(
            status_code=400,
            detail="Live mode requires a Unusual Whales API key (Settings) before Investigate with AI can run.",
        )

    adapter = get_adapter()
    store = get_store()
    try:
        scan = _run_scan(adapter, store, safe_ticker)
        evidence = _to_evidence_item(scan)
        evidence["qualifying_days_count"] = scan["qualifying_days_count"]
        evidence["confidence"] = scan["confidence"]
    except Exception:
        evidence = {"ticker": safe_ticker, "note": "Market Radar evidence unavailable; investigating ticker alone."}

    try:
        result = ai_orchestrator.investigate(
            ticker=safe_ticker,
            evidence=evidence,
            ai_provider=ai_provider,
            ai_api_key=ai_key,
            uw_api_key=uw_key,
            uw_base_url=settings.uw_base_url,
            uw_client_api_id=settings.uw_client_api_id,
            transport=settings.investigate_transport,
        )
    except AiValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ai_orchestrator.NoValidToolsSelectedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MalformedAIResponse as e:
        raise HTTPException(status_code=502, detail=f"AI returned a malformed response: {e}")
    except LLMTruncatedOutputError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"AI provider response was cut off before it finished (token limit reached), not malformed: {e} "
                "Try again -- if this keeps happening, the response needed more room than the bounded limit allows."
            ),
        )
    except LLMCallError as e:
        raise HTTPException(status_code=502, detail=f"AI provider call failed: {e}")
    except MCPSubprocessError as e:
        raise HTTPException(status_code=502, detail=f"MCP transport unavailable: {e}")

    return result.model_dump()


def _validate_ticker_for_route(ticker: str):
    from .ai.validation import validate_ticker

    return validate_ticker(ticker)


@app.get("/api/scenarios")
def scenarios():
    """Demo-mode only: lists the synthetic scenarios available, so the
    frontend can show what each fixture is meant to demonstrate."""
    adapter = get_adapter()
    if isinstance(adapter, SyntheticDataAdapter):
        return {"scenarios": adapter.list_scenarios()}
    return {"scenarios": [], "note": "scenario listing only available in TIP_MODE=demo"}


# Serve the plain HTML/CSS/JS frontend at "/" when its directory is present
# (true in the Docker image; optional for local `uvicorn app.main:app` runs
# started from backend/ without the sibling frontend/ dir mounted).
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
