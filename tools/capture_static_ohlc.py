#!/usr/bin/env python3
"""Captures GET /api/ticker/{t}/ohlc for every demo-mode scenario straight
from the real backend (FastAPI TestClient over SyntheticDataAdapter, same
app.main code path the Docker/local edition runs) into
static-demo/data/ohlc/<TICKER>.json -- same "run the real app, snapshot
real output" convention as the rest of static-demo/data/*.json. Never
hand-authors the JSON directly.

Run via tools/build_static_demo.sh (or directly) after any change to the
OHLC route, candles.py, or fixtures/*.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

os.environ["TIP_MODE"] = "demo"
os.environ["TIP_DB_PATH"] = ":memory:"
os.environ["TIP_SETTINGS_PATH"] = str(REPO_ROOT / ".capture_settings_tmp.json")
os.environ["TIP_FIXTURES_DIR"] = str(REPO_ROOT / "fixtures")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.adapters.synthetic_adapter import SyntheticDataAdapter  # noqa: E402


def main():
    client = TestClient(app)
    adapter = SyntheticDataAdapter(str(REPO_ROOT / "fixtures"))
    out_dir = REPO_ROOT / "static-demo" / "data" / "ohlc"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for ticker in adapter.get_universe():
        resp = client.get(f"/api/ticker/{ticker}/ohlc")
        resp.raise_for_status()
        (out_dir / f"{ticker}.json").write_text(json.dumps(resp.json(), indent=2) + "\n")
        written.append(ticker)
    settings_tmp = REPO_ROOT / ".capture_settings_tmp.json"
    if settings_tmp.exists():
        settings_tmp.unlink()
    print(f"Captured OHLC+markers for: {', '.join(written)} -> {out_dir}")


if __name__ == "__main__":
    sys.exit(main())
