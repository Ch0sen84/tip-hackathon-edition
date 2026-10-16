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

from typing import Optional

from ..auth import is_loopback

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


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


def assert_local_mode_configuration(bind_host: Optional[str], tip_local_mode_raw: Optional[str]) -> None:
    """Call once at process startup (see main.py's `_on_startup`, same
    place single_process_guard.assert_single_process() is called).
    Raises LocalModeMisconfiguration -- and only that -- when
    TIP_LOCAL_MODE explicitly requests Local Mode against a BIND_HOST
    that is not loopback. Every other combination (including the
    non-loopback+auto case, which just means Network Mode stays active,
    completely unchanged) is a silent no-op."""
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
