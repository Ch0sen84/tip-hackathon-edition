"""Environment-driven configuration. No secrets file reads (that was a
single-machine production convention) -- this app reads everything from
plain env vars so it runs the same in Docker, CI, or a laptop. See
.env.example at the repo root for the full list."""
import os


def _bool(val: str, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        self.mode = os.environ.get("TIP_MODE", "demo").strip().lower()  # "demo" | "live"
        self.uw_api_key = os.environ.get("UW_API_KEY", "")
        self.uw_base_url = os.environ.get("UW_BASE_URL", "https://api.unusualwhales.com")
        self.uw_client_api_id = os.environ.get("UW_CLIENT_API_ID", "")
        self.db_path = os.environ.get("TIP_DB_PATH", "./data/tip_hackathon.db")
        self.fixtures_dir = os.environ.get(
            "TIP_FIXTURES_DIR",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "..", "fixtures"),
        )
        watchlist = os.environ.get("TIP_LIVE_WATCHLIST", "SPY,QQQ,NVDA,TSLA")
        self.live_watchlist = [t.strip().upper() for t in watchlist.split(",") if t.strip()]

    @property
    def is_demo(self) -> bool:
        return self.mode != "live"


settings = Settings()
