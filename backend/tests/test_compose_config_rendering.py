"""Windows Docker Desktop QA round 2 -- finding T2 / brief item 5: a
regression test that actually renders docker-compose.yml with real env
var substitution and asserts the forwarded container environment is
self-consistent with what gets published, for the default case, a
custom BIND_PORT case, and a BIND_HOST=0.0.0.0 case. This is the test
that would have caught B1/B2 before they ever reached real Windows QA
(the prior round's tests only exercised local_mode.py's pure functions
directly -- never the actual rendered Compose file those functions
depend on being fed correct values from).

HONESTY NOTE (see FINAL REPORT for the full accounting): this sandbox
has no `docker` or `docker compose` binary at all (verified: `which
docker` / `which docker-compose` / `which podman` all come back empty).
Per the task brief ("an equivalent config-parsing approach if `docker
compose` isn't available in this environment -- check and use whatever's
actually available"), this renders docker-compose.yml with PyYAML plus a
small, deliberately narrow implementation of Compose's own
`${VAR}` / `${VAR:-default}` interpolation syntax (the only two forms
this file actually uses), applied as a text pre-pass exactly like the
real `docker compose config` does, then parses the result with
yaml.safe_load. It is NOT a substitute for actually running `docker
compose config` and diffing its output -- that genuine Docker-side
verification could not be performed in this environment and must happen
in Claude Code's Windows Docker Desktop re-test pass before merge.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-(?P<default>[^}]*))?\}")


def render_compose(env: Optional[Dict[str, str]] = None) -> dict:
    """Render docker-compose.yml's `${VAR}` / `${VAR:-default}` forms
    against the given env overrides (anything not in `env` is treated as
    unset, exactly like a real shell/`.env` with that var absent) and
    parse the result as YAML -- a deliberately narrow stand-in for
    `docker compose config` (see module docstring for why that real
    command could not be run in this sandbox)."""
    env = env or {}
    raw = COMPOSE_PATH.read_text()

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        default = match.group("default")
        if name in env:
            return env[name]
        return default if default is not None else ""

    rendered_text = _VAR_PATTERN.sub(_sub, raw)
    return yaml.safe_load(rendered_text)


def _service(config: dict) -> dict:
    return config["services"]["tip-hackathon"]


def _port_mapping(config: dict) -> str:
    ports = _service(config)["ports"]
    assert len(ports) == 1
    return ports[0]


def _container_env(config: dict) -> Dict[str, str]:
    """Container env vars are always strings at runtime regardless of
    what scalar type bare YAML would infer (e.g. `BIND_PORT: 8000`
    parses as a YAML int) -- coerce here exactly like Docker itself does
    when it actually sets a container's environment."""
    return {k: ("" if v is None else str(v)) for k, v in _service(config)["environment"].items()}


# --- sanity: the file is genuinely YAML + has the fields this test cares
# about, so a renamed/restructured key fails loudly instead of the
# scenarios below silently asserting against stale defaults -----------


def test_compose_file_parses_and_has_expected_shape():
    config = render_compose({})
    svc = _service(config)
    assert "ports" in svc
    assert "environment" in svc
    assert "BIND_HOST" in svc["environment"], (
        "BIND_HOST must be forwarded into the container's own environment "
        "(finding B2) -- not just used in the `ports:` line"
    )
    assert "BIND_PORT" in svc["environment"], (
        "BIND_PORT must be forwarded into the container's own environment "
        "(finding B1) -- not just used in the `ports:` line"
    )


# --- scenario 1: default case (no .env overrides at all) ---------------


def test_default_case_publishes_and_forwards_loopback_8000():
    config = render_compose({})
    assert _port_mapping(config) == "127.0.0.1:8000:8000"
    env = _container_env(config)
    assert env["BIND_HOST"] == "127.0.0.1"
    assert env["BIND_PORT"] == "8000"
    # self-consistency: the host-side address/port in the publish mapping
    # must exactly equal what got forwarded into the container's own env --
    # the structural fix for B1/B2 (single shared source, see
    # docker-compose.yml's BIND_HOST/BIND_PORT comment).
    published_host, published_port, _container_port = _port_mapping(config).split(":")
    assert published_host == env["BIND_HOST"]
    assert published_port == env["BIND_PORT"]


# --- scenario 2: custom BIND_PORT override (finding B1) ----------------


def test_custom_bind_port_is_forwarded_consistently_with_the_publish_mapping():
    config = render_compose({"BIND_PORT": "18000"})
    assert _port_mapping(config) == "127.0.0.1:18000:8000"
    env = _container_env(config)
    assert env["BIND_PORT"] == "18000", (
        "B1 regression: a custom BIND_PORT override must reach the container's "
        "own environment, not silently stay at the 8000 default"
    )
    published_host, published_port, _container_port = _port_mapping(config).split(":")
    assert published_port == env["BIND_PORT"]
    assert published_host == env["BIND_HOST"]


# --- scenario 3: BIND_HOST=0.0.0.0 (finding B2 -- SECURITY-CRITICAL) ---


def test_bind_host_0_0_0_0_is_forwarded_consistently_with_the_publish_mapping():
    config = render_compose({"BIND_HOST": "0.0.0.0"})
    assert _port_mapping(config) == "0.0.0.0:8000:8000"
    env = _container_env(config)
    assert env["BIND_HOST"] == "0.0.0.0", (
        "B2 regression: BIND_HOST=0.0.0.0 (publishing to the LAN) must reach the "
        "container's own environment -- a container that cannot see this would "
        "wrongly keep activating Local Mode's zero-token bypass"
    )
    published_host, published_port, _container_port = _port_mapping(config).split(":")
    assert published_host == env["BIND_HOST"]
    assert published_port == env["BIND_PORT"]

    # The actual safety claim this round closes: FEED the real rendered
    # value into the real decision function and confirm it resolves to
    # Network Mode, not Local Mode -- i.e. the rendered config and the
    # app's own logic agree end to end, not just "the string got copied".
    from app.core.local_mode import effective_local_mode

    tip_local_mode_raw = env.get("TIP_LOCAL_MODE")
    if tip_local_mode_raw == "auto":
        tip_local_mode_raw = None
    assert effective_local_mode(env["BIND_HOST"], tip_local_mode_raw) is False


# --- combined scenario: custom port AND LAN bind together --------------


def test_custom_port_and_lan_bind_combined_still_self_consistent():
    config = render_compose({"BIND_HOST": "0.0.0.0", "BIND_PORT": "9999"})
    env = _container_env(config)
    published_host, published_port, _container_port = _port_mapping(config).split(":")
    assert published_host == env["BIND_HOST"] == "0.0.0.0"
    assert published_port == env["BIND_PORT"] == "9999"


# --- explicit TIP_LOCAL_MODE=1 against BIND_HOST=0.0.0.0, rendered from
# the real file, must be the combination the startup guard refuses -----


def test_rendered_explicit_local_mode_against_lan_bind_fails_the_real_startup_guard():
    config = render_compose({"BIND_HOST": "0.0.0.0", "TIP_LOCAL_MODE": "1"})
    env = _container_env(config)
    from app.core.local_mode import LocalModeMisconfiguration, assert_local_mode_configuration

    with pytest.raises(LocalModeMisconfiguration):
        assert_local_mode_configuration(bind_host=env["BIND_HOST"], tip_local_mode_raw=env["TIP_LOCAL_MODE"])
