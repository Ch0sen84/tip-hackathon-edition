"""Regression guard for a real deployment bug (2026-10-14): docker-compose.yml
never set TIP_SETTINGS_PATH, so settings_store.py's default
("./data/settings.json", relative to the app's /srv/backend working
directory) resolved OUTSIDE the tip-data volume (mounted at /srv/data) --
saved UW/AI keys and Live Mode activation silently did not survive a
container recreation. Fixed by setting TIP_SETTINGS_PATH explicitly inside
the mounted volume in docker-compose.yml. This test guards the underlying
mechanism (SettingsStore honors whatever path it's given) stays correct;
it cannot test docker-compose.yml's YAML directly, so the compose file
itself must be read when auditing this again.
"""
from __future__ import annotations

from app.settings_store import SettingsStore


def test_settings_store_persists_to_the_exact_path_it_is_given(tmp_path):
    """If this path is inside a mounted volume, state survives a
    container recreation; if it is not (the original bug), it won't --
    this test can only confirm the store itself is path-faithful, which is
    the whole mechanism docker-compose.yml's TIP_SETTINGS_PATH relies on."""
    settings_path = tmp_path / "data" / "settings.json"
    store = SettingsStore(settings_path)
    store.set_uw_key("fake-key")
    store.set_ai_key("anthropic", "fake-ai-key")

    assert settings_path.exists(), "SettingsStore must write to exactly the path it was constructed with"

    # Simulate a container recreation: a FRESH SettingsStore instance
    # pointed at the SAME path (this is exactly what a real redeploy does
    # -- a new process, same path if and only if that path is inside a
    # persistent volume).
    reloaded = SettingsStore(settings_path)
    data = reloaded.get_all()
    assert data["uw_api_key"] == "fake-key"
    assert data["ai_provider"] == "anthropic"
    assert data["ai_api_key"] == "fake-ai-key"
