"""UX round (2026-10-16), item 1 root-cause regression: Market
Intelligence's SUMMARY count (`candidate_count_in_snapshot`) must never
silently diverge from the number of candidate ROWS this route actually
returns without an explicit, honest disclosure. Before this fix,
GET /api/radar/market-wide defaulted its own `limit` to a plain 20,
completely decoupled from discovery.py's DISCOVERY_LIMIT_CAP (50) that
actually bounds a snapshot's size -- a snapshot with, say, 35
candidates reported "35 discovered" in the summary while only ever
rendering the first 20 rows, with zero indication anything was cut.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _catalog_snapshot_with_n_candidates(app_main, n: int, ticker_prefix="TICK"):
    catalog = app_main.get_catalog()
    candidates = [
        {"ticker": f"{ticker_prefix}{i}", "net_premium": 1_000_000 + i, "is_index": False}
        for i in range(n)
    ]
    snapshot_id = catalog.catalog_discovery_snapshot(
        filters={}, candidates=candidates, snapshot_at="2026-10-16T12:00:00+00:00",
    )
    assert snapshot_id is not None
    return candidates


def test_default_limit_matches_discovery_cap_so_a_normal_scan_is_never_silently_truncated(app_main):
    """A snapshot at exactly the real discovery cap (50) must render ALL
    50 rows with zero default-limit truncation -- this is the common
    case the bug affected (any scan between 21 and 50 candidates)."""
    _catalog_snapshot_with_n_candidates(app_main, 50)
    client = TestClient(app_main.app)
    res = client.get("/api/radar/market-wide")
    assert res.status_code == 200
    body = res.json()
    assert body["candidate_count_in_snapshot"] == 50
    assert body["candidates_shown"] == 50
    assert len(body["candidates"]) == 50
    assert body["rows_truncated_note"] is None


def test_explicit_smaller_limit_still_discloses_the_real_truncation_honestly(app_main):
    """When a caller DOES pass a smaller `limit` (or a future snapshot
    somehow exceeds the cap), the response must say so explicitly --
    never a bare, unexplained mismatch between the two numbers."""
    _catalog_snapshot_with_n_candidates(app_main, 30)
    client = TestClient(app_main.app)
    res = client.get("/api/radar/market-wide?limit=10")
    assert res.status_code == 200
    body = res.json()
    assert body["candidate_count_in_snapshot"] == 30
    assert body["candidates_shown"] == 10
    assert len(body["candidates"]) == 10
    assert body["rows_truncated_note"] is not None
    assert "10" in body["rows_truncated_note"] and "30" in body["rows_truncated_note"]
