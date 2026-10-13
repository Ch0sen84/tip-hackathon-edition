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

import os
from datetime import date, timedelta
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adapters.base import UWDataAdapter
from .adapters.live_uw_adapter import ERROR, UNAUTHORIZED, UNAVAILABLE, LiveUWAdapter, UWRestClient
from .adapters.synthetic_adapter import SyntheticDataAdapter
from .ai import orchestrator as ai_orchestrator
from .ai.llm_provider import LLMCallError
from .ai.mcp_tool_client import MCPSubprocessError
from .ai.schemas import MalformedAIResponse
from .ai.simulated import get_simulated_transcript
from .ai.validation import ValidationError as AiValidationError
from .config import settings
from .core import scan_service, tip_evidence
from .core.candles import build_markers
from .settings_store import SettingsStore, last4
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
    sig = (mode, key)
    if _adapter is None or _adapter_sig != sig:
        if mode != "live":
            _adapter = SyntheticDataAdapter(settings.fixtures_dir)
        else:
            _adapter = LiveUWAdapter(
                api_key=key or "",
                base_url=settings.uw_base_url,
                client_api_id=settings.uw_client_api_id,
                watchlist=settings.live_watchlist,
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
    }


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


@app.get("/api/radar")
def radar():
    """Market Radar: scan every ticker in the adapter's universe and return
    the ones that qualify (tip_evidence.qualifies), highest-attention first."""
    adapter = get_adapter()
    store = get_store()
    universe = adapter.get_universe()
    if not universe:
        return {"highest": [], "other": [], "scanned": 0}

    items = []
    scans_by_ticker = {}
    for ticker in universe:
        scan = _run_scan(adapter, store, ticker)
        scans_by_ticker[ticker] = scan
        items.append(_to_evidence_item(scan))

    highest, other = tip_evidence.present(items, next_session=date.today() + timedelta(days=1))

    def _attach(entry: dict) -> dict:
        s = scans_by_ticker[entry["ticker"]]
        entry["confidence"] = s["confidence"]
        entry["signal_type"] = s["signal_type"]
        entry["qualifying_days_count"] = s["qualifying_days_count"]
        return entry

    return {
        "highest": [_attach(e) for e in highest],
        "other": [_attach(e) for e in other],
        "scanned": len(universe),
    }


@app.get("/api/ticker/{ticker}")
def ticker_analysis(ticker: str):
    """Ticker Analysis: full evidence drill-down for one ticker -- gates,
    confidence, state transition, and the tip_evidence explanation."""
    adapter = get_adapter()
    store = get_store()
    ticker = ticker.upper()
    if ticker not in adapter.get_universe():
        raise HTTPException(status_code=404, detail=f"{ticker} is not in the current adapter universe")

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


@app.get("/api/ticker/{ticker}/ohlc")
def ticker_ohlc(ticker: str, timeframe: str = "1d", limit: int = 180):
    """Candlestick bars + evidence markers for the Ticker Analysis chart.
    Markers come from the SAME flow_alerts/darkpool_trades the detection
    engine already uses -- never a new data source, never an invented
    price (see backend/app/core/candles.py::build_markers). `unavailable`
    is a structured, non-crashing result (e.g. an unentitled live key, or
    a demo scenario with no captured OHLC fixture) -- the frontend must
    show an explicit "chart data unavailable" state, not silently render
    nothing."""
    adapter = get_adapter()
    ticker = ticker.upper()
    if ticker not in adapter.get_universe():
        raise HTTPException(status_code=404, detail=f"{ticker} is not in the current adapter universe")
    ohlc = adapter.get_ohlc(ticker, timeframe=timeframe, limit=limit)
    flow_alerts = adapter.get_flow_alerts(ticker)
    darkpool_trades = adapter.get_darkpool_trades(ticker)
    markers = build_markers(flow_alerts, darkpool_trades)
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "candles": ohlc.get("candles", []),
        "source": ohlc.get("source"),
        "negative_volume_bars_clamped": ohlc.get("negative_volume_bars_clamped", 0),
        "unavailable": ohlc.get("unavailable", False),
        "reason": ohlc.get("reason"),
        "markers": markers,
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
