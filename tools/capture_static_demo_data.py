#!/usr/bin/env python3
"""Captures GET /api/radar, /api/scenarios, /api/investigate/availability,
GET /api/ticker/{t}, and POST /api/investigate/{t} for every demo-mode
scenario straight from the real backend (FastAPI TestClient over
SyntheticDataAdapter, same app.main code path the Docker/local edition
runs) into static-demo/data/*.json -- same "run the real app, snapshot
real output" convention as tools/capture_static_ohlc.py and
static-demo/data/README.md's documented provenance rule. Never
hand-authors the JSON directly.

Run via tools/build_static_demo.sh (after capture_static_ohlc.py) any
time a new scenario fixture is added or scan_service/signal_engine/
tip_evidence change.
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
os.environ["TIP_SETTINGS_PATH"] = str(REPO_ROOT / ".capture_settings_tmp2.json")
os.environ["TIP_FIXTURES_DIR"] = str(REPO_ROOT / "fixtures")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.adapters.synthetic_adapter import SyntheticDataAdapter  # noqa: E402


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def main():
    client = TestClient(app)
    adapter = SyntheticDataAdapter(str(REPO_ROOT / "fixtures"))
    data_dir = REPO_ROOT / "static-demo" / "data"

    scenarios_resp = client.get("/api/scenarios")
    scenarios_resp.raise_for_status()
    _write(data_dir / "scenarios.json", scenarios_resp.json())

    avail_resp = client.get("/api/investigate/availability")
    avail_resp.raise_for_status()
    _write(data_dir / "investigate-availability.json", avail_resp.json())

    radar_resp = client.get("/api/radar")
    radar_resp.raise_for_status()
    _write(data_dir / "radar.json", radar_resp.json())

    written_tickers = []
    for ticker in adapter.get_universe():
        t_resp = client.get(f"/api/ticker/{ticker}")
        t_resp.raise_for_status()
        _write(data_dir / "ticker" / f"{ticker}.json", t_resp.json())

        inv_resp = client.post(f"/api/investigate/{ticker}")
        inv_resp.raise_for_status()
        _write(data_dir / "investigate" / f"{ticker}.json", inv_resp.json())
        written_tickers.append(ticker)

    settings_tmp = Path(os.environ["TIP_SETTINGS_PATH"])
    if settings_tmp.exists():
        settings_tmp.unlink()
    print(f"Captured radar.json, scenarios.json, investigate-availability.json, "
          f"and ticker/+investigate/ for: {', '.join(written_tickers)} -> {data_dir}")


if __name__ == "__main__":
    sys.exit(main())
