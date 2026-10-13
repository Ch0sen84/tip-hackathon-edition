"""Environment-driven configuration. No secrets file reads (that was a
single-machine production convention) -- this app reads everything from
plain env vars (optionally via a local .env file) so it runs the same in
Docker, CI, or a laptop. See .env.example at the repo root for the full list.

Phase 2.5 note: `self.uw_api_key` here is ONLY the .env/environment
fallback value, read once at process startup. It is NOT the source of
truth for what the running app actually uses -- backend/app/settings_store.py
holds a UI-saved key (if any) which takes precedence over this env value at
request time. See README "Credential precedence" section.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a .env file in the working dir, if present; no-op otherwise
except ImportError:
    pass  # python-dotenv not installed -- env vars set another way (Docker, shell) still work


def _bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        self.mode = os.environ.get("TIP_MODE", "demo").strip().lower()  # "demo" | "live" (initial seed only; see settings_store)
        self.uw_api_key = os.environ.get("UW_API_KEY", "")  # .env/env fallback only -- see module docstring
        self.public_demo = _bool(os.environ.get("PUBLIC_DEMO"))
        self.uw_base_url = os.environ.get("UW_BASE_URL", "https://api.unusualwhales.com")
        self.uw_client_api_id = os.environ.get("UW_CLIENT_API_ID", "")
        # "rest" (default) -> REST-ported tool calls via UWRestClient, the
        # documented fallback transport. "mcp" -> real MCP-protocol calls
        # against UW's hosted https://api.unusualwhales.com/api/mcp server
        # via the isolated subprocess in app/ai/mcp_tool_client.py (see
        # mcp_isolated/ at the repo root). Any other value falls back to
        # "rest" rather than crashing on a typo.
        _transport = os.environ.get("INVESTIGATE_TRANSPORT", "rest").strip().lower()
        self.investigate_transport = _transport if _transport in ("rest", "mcp") else "rest"
        self.db_path = os.environ.get("TIP_DB_PATH", "./data/tip_hackathon.db")
        self.fixtures_dir = os.environ.get(
            "TIP_FIXTURES_DIR",
            # backend/app/config.py -> backend/app -> backend -> repo root, then /fixtures.
            # (Fixed an extra stray ".." here that silently never mattered before because
            # every caller that needed this path -- Dockerfile, test fixtures -- set
            # TIP_FIXTURES_DIR explicitly; the new Investigate-with-AI demo-mode route is
            # the first thing that relies on this default actually resolving correctly.)
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fixtures"),
        )
        watchlist = os.environ.get("TIP_LIVE_WATCHLIST", "SPY,QQQ,NVDA,TSLA")
        self.live_watchlist = [t.strip().upper() for t in watchlist.split(",") if t.strip()]

    @property
    def is_demo(self) -> bool:
        return self.mode != "live"


settings = Settings()
