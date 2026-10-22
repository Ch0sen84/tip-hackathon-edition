"""Live P0 found in real-provider qualification: after a baseline build the catalog holds ~500,000 SPY
observations and the cached chart response drew every one (~58 MB, ~14 s), freezing Ticker Analysis.
The chart now draws only the newest observations per kind and discloses the true totals. Constructed data."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.test_collection_status import live_app, routes  # noqa: F401 (fixture)

T0 = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_cached_chart_draws_only_the_newest_markers_and_discloses_totals(live_app, monkeypatch):
    m = live_app
    m.use_routes(routes())
    monkeypatch.setattr(m, "CHART_MARKERS_PER_KIND", 50)
    cat = m.get_catalog()
    dp = [{"ticker": "SPY", "executed_at": _iso(T0 + timedelta(seconds=i)), "price": "500.00", "size": 10,
           "premium": "5000.00", "tracking_id": f"t{i}", "volume": i} for i in range(120)]
    fl = [{"id": f"a{i}", "ticker": "SPY", "created_at": _iso(T0 + timedelta(seconds=i)), "type": "call",
           "total_premium": 1000} for i in range(130)]
    cat.catalog_darkpool_observations("SPY", dp)
    cat.catalog_options_flow_observations("SPY", fl)
    body = TestClient(m.app).get("/api/ticker/SPY/ohlc").json()
    ms = body["marker_sources"]
    assert ms["origin"] == "catalog"
    assert ms["darkpool"] == {"row_count": 50, "total": 120, "truncated": True}
    assert ms["options_flow"] == {"row_count": 50, "total": 130, "truncated": True}
    assert len(body["markers"]) == 100
    dp_times = sorted(x["time"] for x in body["markers"] if x["kind"] == "darkpool_trade")
    assert dp_times[0] == _iso(T0 + timedelta(seconds=70)) and dp_times[-1] == _iso(T0 + timedelta(seconds=119))


def test_small_catalogs_are_drawn_in_full(live_app):
    m = live_app
    m.use_routes(routes())
    cat = m.get_catalog()
    cat.catalog_darkpool_observations("SPY", [{"ticker": "SPY", "executed_at": _iso(T0), "price": "1", "size": 1,
                                               "premium": "1", "tracking_id": "x", "volume": 1}])
    ms = TestClient(m.app).get("/api/ticker/SPY/ohlc").json()["marker_sources"]
    assert ms["darkpool"] == {"row_count": 1, "total": 1, "truncated": False}
    assert ms["options_flow"] == {"row_count": 0, "total": 0, "truncated": False}
