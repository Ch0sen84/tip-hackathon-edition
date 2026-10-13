"""Loads the static, pre-written simulated investigation transcripts from
fixtures/investigate_simulated/*.json. Used whenever the app is NOT in
live mode (demo mode, with or without PUBLIC_DEMO) -- this path NEVER
makes a network call of any kind, real or otherwise; it only reads a local
JSON file. See backend/app/main.py's POST /api/investigate/{ticker} route.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def get_simulated_transcript(fixtures_dir: str, ticker: str) -> dict:
    ticker = (ticker or "").strip().upper()
    base = Path(fixtures_dir) / "investigate_simulated"
    candidate = base / f"{ticker}.json"
    path = candidate if candidate.exists() else base / "_default.json"
    with open(path, "r") as f:
        data = json.load(f)
    data["ticker"] = ticker
    return data
