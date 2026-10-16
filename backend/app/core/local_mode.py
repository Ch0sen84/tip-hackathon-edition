"""Local Mode eligibility + fail-loud startup guard.

Design (Chad's refinement #3 of the Fetch-Metadata auth redesign):
BIND_HOST is read as the operator's STATED INTENT, never independently
verified fact -- this process has no way to confirm from inside a
container what is actually listening on the host machine's real network
interfaces, so a loopback-looking BIND_HOST value is treated as a claim,
not a proof. Two separate things follow from that:

  (a) `effective_local_mode()` derives whether the zero-token Local Mode
      bypass is active purely from BIND_HOST (plus an optional explicit
      TIP_LOCAL_MODE override) -- this is a *trust decision*, never a
      verification.
  (b) `assert_local_mode_configuration()` is the fail-closed backstop:
      if an operator explicitly asks for Local Mode (TIP_LOCAL_MODE=1)
      while BIND_HOST says anything other than loopback, this is a
      direct contradiction of the trust model Local Mode depends on --
      refuse to start loudly (mirrors
      core/single_process_guard.py's MultiWorkerConfigurationError
      pattern) rather than silently downgrading to Network Mode or,
      worse, silently granting the bypass anyway.

This module intentionally has NO socket/network code of its own -- see
module docstring above for why that would be a false sense of security
(a container's loopback interface is not a proof of host-level
isolation; see README.md / INSTALL_AGENT.md for the explicit
unsupported-topologies list: custom Compose overrides, reverse
proxies, tunnels, or any other external port publishing in front of a
Local-Mode-enabled instance).
"""
from __future__ import annotations

from typing import List, Optional

from ..auth import is_loopback

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# Deliberately the SAME loopback-alias set app/main.py's TrustedHostMiddleware
# config (_LOOPBACK_HOST_ALIASES) already uses for the Host header -- see
# compute_allowed_origins below (finding B3): Origin validation must accept
# exactly the hosts Host validation already does, never a broader or
# narrower set.
LOOPBACK_HOST_ALIASES: frozenset[str] = frozenset({"127.0.0.1", "localhost"})


def compute_allowed_origins(
    allowed_host: str,
    bind_port: str,
    allowed_origin_override: Optional[str] = None,
) -> List[str]:
    """Pure function (Windows Docker Desktop QA round 2, findings B1/B3):
    the set of `Origin` header values this deployment must accept, given
    its real configured host/port.

    * An explicit ALLOWED_ORIGIN override wins outright -- exactly one
      origin, never widened (unchanged behavior for anyone fronting this
      with a reverse proxy under a different hostname).
    * Otherwise, when `allowed_host` is one of the loopback aliases
      (127.0.0.1/localhost -- the exact same set already accepted for the
      Host header), EVERY alias on the real configured port is accepted
      (fixes B3: `http://localhost:<port>` and `http://127.0.0.1:<port>`
      for the SAME port are both valid, never just one of them).
    * Otherwise (a genuine non-loopback/custom ALLOWED_HOST, e.g. a
      reverse-proxy hostname with no explicit ALLOWED_ORIGIN set), exactly
      one origin for that host:port -- no widening.

    `bind_port` must be the REAL externally-published port (fixes B1) --
    see docker-compose.yml's BIND_HOST/BIND_PORT environment entries for
    why this is now genuinely forwarded into the container instead of
    silently falling back to the 8000 default regardless of what was
    actually published.

    Always returns a small, exact, bounded list -- never a wildcard.
    """
    if allowed_origin_override:
        return [allowed_origin_override]
    if allowed_host in LOOPBACK_HOST_ALIASES:
        return sorted(f"http://{h}:{bind_port}" for h in LOOPBACK_HOST_ALIASES)
    return [f"http://{allowed_host}:{bind_port}"]


class LocalModeMisconfiguration(RuntimeError):
    """Raised at startup when TIP_LOCAL_MODE explicitly requests Local
    Mode against a non-loopback BIND_HOST. Deliberately a plain
    RuntimeError, never caught anywhere in this app, so it fails the
    whole process loudly instead of silently running with either a
    downgraded mode or an unsafe bypass."""


def _parse_explicit_intent(raw: Optional[str]) -> Optional[bool]:
    """None means "auto" (operator expressed no explicit preference
    either way) -- deliberately distinct from an explicit True/False.
    An unrecognized value (a typo) is also treated as "auto" rather than
    raising here -- only a recognized, explicit TRUE value against a
    non-loopback BIND_HOST is a hard startup failure; see
    assert_local_mode_configuration below."""
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("", "auto"):
        return None
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return None


def effective_local_mode(bind_host: Optional[str], tip_local_mode_raw: Optional[str]) -> bool:
    """Whether the Sec-Fetch-Site+Origin zero-token bypass is active for
    this process.

      * TIP_LOCAL_MODE explicitly false -> OFF, regardless of BIND_HOST
        (an operator who wants the existing Network Mode token model
        enforced everywhere, even on a loopback-bound install, gets
        exactly that -- Local Mode is strictly opt-out-able).
      * TIP_LOCAL_MODE explicitly true -> ON only if BIND_HOST is
        loopback. The non-loopback+true combination is a startup-time
        hard failure (assert_local_mode_configuration) and this
        function is never actually evaluated for that combination in a
        real run -- it returns False here purely so this pure function
        has a safe, defined answer if ever called in isolation (e.g. a
        test) without the guard around it.
      * TIP_LOCAL_MODE unset/"auto"/unrecognized -> ON iff BIND_HOST is
        loopback, OFF otherwise. This is the zero-config default every
        existing install (this round's new default) falls into, and is
        exactly today's documented safe-by-default BIND_HOST behavior.
    """
    intent = _parse_explicit_intent(tip_local_mode_raw)
    loopback = is_loopback(bind_host)
    if intent is False:
        return False
    if intent is True:
        return loopback
    return loopback


def assert_local_mode_configuration(
    bind_host: Optional[str],
    tip_local_mode_raw: Optional[str],
    allowed_host_override: Optional[str] = None,
) -> None:
    """Call once at process startup (see main.py's `_on_startup`, same
    place single_process_guard.assert_single_process() is called).
    Raises LocalModeMisconfiguration -- and only that -- when
    TIP_LOCAL_MODE explicitly requests Local Mode against a BIND_HOST
    that is not loopback. Every other combination (including the
    non-loopback+auto case, which just means Network Mode stays active,
    completely unchanged) is a silent no-op.

    `allowed_host_override` (Windows Docker Desktop QA round 2, finding
    B2's secondary ask): pass the RAW `ALLOWED_HOST` env var here only
    when an operator has set it explicitly to something other than
    BIND_HOST's own value (main.py passes None whenever ALLOWED_HOST was
    never set, i.e. it just defaults to BIND_HOST and cannot diverge from
    it by construction). This is a narrow, genuinely detectable
    self-consistency check: both BIND_HOST and ALLOWED_HOST are operator-
    supplied env vars visible to this same process, so if BIND_HOST claims
    loopback (Local Mode would activate) while the operator has ALSO
    explicitly pointed ALLOWED_HOST somewhere non-loopback, that is an
    internal contradiction this process genuinely CAN see and must refuse
    to silently resolve one way or the other.

    This is NOT, and cannot be, a general detector of "is BIND_HOST
    actually telling the truth about what's reachable from outside this
    container" -- that depends on real host-level network/Compose
    behavior (what `docker compose` actually published this container on)
    which is fundamentally unobservable from inside the container itself.
    That class of divergence is closed structurally instead, by making
    docker-compose.yml forward the SAME ${BIND_HOST}/${BIND_PORT} shell
    variables into both the `ports:` publish and this process's own
    environment (see docker-compose.yml's BIND_HOST/BIND_PORT environment
    entries and their comment) -- not by trying to verify it from here,
    which honestly cannot be done.
    """
    intent = _parse_explicit_intent(tip_local_mode_raw)
    if intent is True and not is_loopback(bind_host):
        message = (
            f"Refusing to start: TIP_LOCAL_MODE explicitly requests Local Mode, but "
            f"BIND_HOST={bind_host!r} is not a loopback address (127.0.0.1/::1/localhost). "
            "Local Mode's zero-token same-origin bypass is only safe when this deployment is "
            "genuinely reachable from nowhere but this machine -- BIND_HOST is read here as your "
            "STATED intent only, never independently verified from inside this container, so a "
            "non-loopback value directly contradicts the one thing Local Mode depends on. Fix: "
            "either unset TIP_LOCAL_MODE (or set TIP_LOCAL_MODE=0) to run in Network Mode with "
            "the existing X-TIP-Auth token model, or set BIND_HOST back to 127.0.0.1 if this "
            "deployment really is loopback-only. See README.md 'Local Mode / Network Mode' for "
            "the full design and the explicitly UNSUPPORTED topologies (custom Compose port "
            "overrides, reverse proxies, tunnels) that must never be combined with Local Mode."
        )
        raise LocalModeMisconfiguration(message)
    if (
        is_loopback(bind_host)
        and allowed_host_override is not None
        and not is_loopback(allowed_host_override)
    ):
        message = (
            f"Refusing to start: BIND_HOST={bind_host!r} says this deployment is loopback-only "
            f"(which would activate Local Mode's zero-token bypass), but ALLOWED_HOST has been "
            f"explicitly set to {allowed_host_override!r}, a non-loopback value -- these two "
            "operator-supplied settings directly contradict each other. This process cannot "
            "verify from inside a container what is truly reachable on the host's real network "
            "interfaces (see this module's docstring), but it CAN see that its own two config "
            "values disagree, and refuses to silently pick one. Fix: either remove the "
            "ALLOWED_HOST override so it defaults to BIND_HOST, or set BIND_HOST to match the "
            "real non-loopback address you intend (which will correctly fall back to Network "
            "Mode) -- never leave BIND_HOST and ALLOWED_HOST pointing at different trust levels."
        )
        raise LocalModeMisconfiguration(message)
