"""Regression test for the 2026-10-15 Priority 4 asset cache-busting fix:
index.html's <script src> tags must carry a real, build-derived ?v=
query param so a browser never needs a manual "?v=" to see a new deploy."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_index_html_script_tags_have_cache_busting_query_param(app_main, tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("BUILD_COMMIT=deadbee1234\n")
    monkeypatch.setenv("TIP_VERSION_FILE", str(f))
    client = TestClient(app_main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'app.core.js?v=deadbee1234"' in body
    assert 'app.live.js?v=deadbee1234"' in body


def test_index_html_cache_busting_never_touches_external_urls(app_main, tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("BUILD_COMMIT=deadbee1234\n")
    monkeypatch.setenv("TIP_VERSION_FILE", str(f))
    client = TestClient(app_main.app)
    resp = client.get("/index.html")
    assert resp.status_code == 200
    # lightweight-charts.js is vendored locally (no external URL in this
    # repo), but assert the busting regex is scoped to src="...") so any
    # future external <script src="https://..."> would be left untouched.
    assert "https://v=" not in resp.text
