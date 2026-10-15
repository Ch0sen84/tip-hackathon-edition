"""Tests for backend/app/update_check.py and backend/app/version.py --
ALL HTTP to GitHub is mocked; no real network call happens in this suite.
Covers the four user-facing states: up_to_date, update_available,
unable_to_check (network failure AND rate limit), and unknown_version.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from app import update_check, version


def _mock_response(status_code, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


# --- check_for_updates: the four states -------------------------------------

def test_unknown_version_when_installed_commit_is_not_commit_shaped():
    for bad in (None, "", "unknown", "v1.2.3-not-a-sha", "zzzzzzz"):
        result = update_check.check_for_updates(bad)
        assert result["status"] == update_check.STATUS_UNKNOWN_VERSION
        assert result["installed_commit"] == bad


def test_up_to_date_when_github_reports_identical():
    sha = "a" * 40
    with patch.object(update_check.requests, "get",
                       return_value=_mock_response(200, {"status": "identical", "commits": [], "ahead_by": 0})):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UP_TO_DATE
    assert result["installed_commit"] == sha


def test_update_available_when_github_reports_ahead():
    sha = "b" * 40
    body = {"status": "ahead", "ahead_by": 3, "commits": [{"sha": "c" * 40}]}
    with patch.object(update_check.requests, "get", return_value=_mock_response(200, body)):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UPDATE_AVAILABLE
    assert result["latest_commit"] == "c" * 40
    assert "3 new commit" in result["message"]


def test_up_to_date_when_installed_commit_is_ahead_of_branch():
    """gh_status == "behind" means the installed commit has commits the
    branch doesn't -- nothing to update, never reported as update_available."""
    sha = "d" * 40
    body = {"status": "behind", "commits": []}
    with patch.object(update_check.requests, "get", return_value=_mock_response(200, body)):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UP_TO_DATE


def test_unable_to_check_on_diverged_history_never_guessed_as_update_available():
    sha = "e" * 40
    body = {"status": "diverged", "commits": []}
    with patch.object(update_check.requests, "get", return_value=_mock_response(200, body)):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UNABLE_TO_CHECK
    assert result["status"] != update_check.STATUS_UPDATE_AVAILABLE


def test_unable_to_check_on_network_failure():
    sha = "f" * 40
    with patch.object(update_check.requests, "get", side_effect=requests.ConnectionError("no network")):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UNABLE_TO_CHECK
    assert "Could not reach GitHub" in result["message"]


def test_unable_to_check_on_rate_limit():
    sha = "1" * 40
    with patch.object(update_check.requests, "get", return_value=_mock_response(403)):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UNABLE_TO_CHECK
    assert "rate limit" in result["message"].lower()


def test_unable_to_check_on_404_unknown_commit():
    sha = "2" * 40
    with patch.object(update_check.requests, "get", return_value=_mock_response(404)):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UNABLE_TO_CHECK


def test_unable_to_check_on_unexpected_response_shape():
    sha = "3" * 40
    with patch.object(update_check.requests, "get", return_value=_mock_response(200, {"unexpected": "shape"})):
        result = update_check.check_for_updates(sha)
    assert result["status"] == update_check.STATUS_UNABLE_TO_CHECK


def test_never_raises_on_any_branch():
    """Never-crash guarantee -- every branch above must return a dict."""
    with patch.object(update_check.requests, "get", side_effect=requests.Timeout()):
        result = update_check.check_for_updates("a" * 40)
    assert isinstance(result, dict)
    assert "status" in result


# --- version.get_build_info() ----------------------------------------------

def test_get_build_info_reads_version_file(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("BUILD_COMMIT=abc1234\nBUILD_VERSION=v0.1.0\nBUILD_DATE=2026-10-15T00:00:00Z\n")
    monkeypatch.setenv("TIP_VERSION_FILE", str(f))
    info = version.get_build_info()
    assert info["commit"] == "abc1234"
    assert info["version"] == "v0.1.0"
    assert info["built_at"] == "2026-10-15T00:00:00Z"


def test_get_build_info_treats_unknown_as_none(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("BUILD_COMMIT=unknown\nBUILD_VERSION=unknown\nBUILD_DATE=unknown\n")
    monkeypatch.setenv("TIP_VERSION_FILE", str(f))
    info = version.get_build_info()
    assert info == {"commit": None, "version": None, "built_at": None}


def test_get_build_info_missing_file_returns_all_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TIP_VERSION_FILE", str(tmp_path / "does_not_exist"))
    monkeypatch.delenv("TIP_BUILD_COMMIT", raising=False)
    monkeypatch.delenv("TIP_BUILD_VERSION", raising=False)
    monkeypatch.delenv("TIP_BUILD_DATE", raising=False)
    info = version.get_build_info()
    assert info == {"commit": None, "version": None, "built_at": None}


# --- /api/settings/version and /api/settings/update-check routes ----------

def test_version_route_no_network_call(app_main, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    f = tmp_path / "VERSION"
    f.write_text("BUILD_COMMIT=deadbee\nBUILD_VERSION=v9.9.9\nBUILD_DATE=2026-10-15T00:00:00Z\n")
    monkeypatch.setenv("TIP_VERSION_FILE", str(f))
    client = TestClient(app_main.app)
    resp = client.get("/api/settings/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit"] == "deadbee"
    assert body["repo_url"].endswith("tip-hackathon-edition")


def test_update_check_route_returns_unable_to_check_on_mocked_failure(app_main, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    f = tmp_path / "VERSION"
    f.write_text("BUILD_COMMIT=" + "a" * 40 + "\n")
    monkeypatch.setenv("TIP_VERSION_FILE", str(f))
    client = TestClient(app_main.app)
    with patch.object(update_check.requests, "get", side_effect=requests.ConnectionError("down")):
        resp = client.get("/api/settings/update-check")
    assert resp.status_code == 200
    assert resp.json()["status"] == update_check.STATUS_UNABLE_TO_CHECK


def test_update_check_route_unknown_version_when_nothing_baked_in(app_main, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("TIP_VERSION_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("TIP_BUILD_COMMIT", raising=False)
    client = TestClient(app_main.app)
    resp = client.get("/api/settings/update-check")
    assert resp.status_code == 200
    assert resp.json()["status"] == update_check.STATUS_UNKNOWN_VERSION
