"""Phase D: refuse to start (fail safe, loudly) if this process is about
to run under more than one worker.

WHY THIS EXISTS (Chad's review item 2): the UW concurrency ceiling
(core/uw_concurrency.PriorityGate) is a `threading` primitive, CONFIRMED
process-local -- it has zero visibility into any OTHER process. If this
app were ever launched with multiple Uvicorn/Gunicorn worker processes,
each process would get its OWN gate, and N workers x the configured
per-process ceiling could exceed UW's real server-side hard ceiling (3
concurrent) even though every single process individually believes it
is enforcing the limit correctly.

CONFIRMED THIS PHASE: the real Dockerfile's CMD is plain
`uvicorn app.main:app --host 0.0.0.0 --port 8000` -- no `--workers`
flag, i.e. exactly one worker process today. This module's job is to
make sure that stays true by construction: it inspects the real
environment variables that would indicate someone configured more than
one worker (Uvicorn's own `WEB_CONCURRENCY` env var, which it reads
directly; Gunicorn's `--workers`/`GUNICORN_CMD_ARGS`; or an explicit
`UVICORN_WORKERS`-style override some deployments use) and raises
before the app finishes starting if any of them indicate >1.

This is the "simpler, matches current real deployment" choice from the
two options Chad's review offered (the alternative -- real cross-
process coordination via the SQLite lease table for the UW gate itself,
not just the scheduler -- was considered and rejected as unnecessary
extra complexity for a single-process app that has no present need to
run multi-process; see FINAL REPORT for the full justification). If
Chad ever actually needs multiple worker processes, the right fix is
real cross-process semaphore coordination through the SAME SQLite
pattern as the scheduler lease -- not quietly raising this ceiling.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Every env var that, if set to anything other than "1" / unset, means a
# multi-worker launch was configured somewhere in the deployment chain.
_WORKER_COUNT_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")


class MultiWorkerConfigurationError(RuntimeError):
    """Raised at startup when more than one worker process is configured.
    Deliberately a plain RuntimeError (not caught anywhere) so it fails
    the whole process loudly instead of silently degrading into an
    unenforced concurrency ceiling."""


def _parse_worker_count(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def detected_worker_count(env=None) -> int:
    """Best-effort detection of how many worker processes this
    deployment is configured for. Returns 1 when nothing suggests
    otherwise (the honest default -- never guesses a bigger number than
    the evidence supports). `env` is injectable for tests (any
    Mapping[str, str]); defaults to the real process environment."""
    env = env if env is not None else os.environ
    counts = []
    for name in _WORKER_COUNT_ENV_VARS:
        n = _parse_worker_count(env.get(name))
        if n is not None:
            counts.append(n)
    cmd_args = env.get("GUNICORN_CMD_ARGS", "")
    if "--workers" in cmd_args:
        # GUNICORN_CMD_ARGS="--workers 4" style -- extract the integer
        # that follows --workers if present; otherwise just flag >1 by
        # treating an unparsed --workers as "at least 2" (fail safe, not
        # fail open, when we can't parse the exact count).
        parts = cmd_args.split()
        try:
            idx = parts.index("--workers")
            counts.append(_parse_worker_count(parts[idx + 1]) or 2)
        except (ValueError, IndexError):
            counts.append(2)
    if not counts:
        return 1
    return max(counts)


def assert_single_process(env=None) -> None:
    """Raises MultiWorkerConfigurationError when `detected_worker_count`
    is >1. Call this once at app startup (see main.py). Never silently
    clamps the configured value down to 1 -- that would hide a real
    misconfiguration that could cause UW rate-limit (429) storms from
    this app exceeding its own documented concurrency ceiling."""
    n = detected_worker_count(env)
    if n > 1:
        message = (
            f"Refusing to start: {n} worker processes are configured, but this app's UW "
            "concurrency guard (core/uw_concurrency.py) is process-local and NOT safe across "
            "multiple worker processes -- running with >1 worker can let real UW requests "
            "exceed the server-side hard ceiling even though each process believes it is "
            "enforcing its own limit correctly. This app is deployed single-process by design "
            "(see README.md 'Single-process by design'); remove --workers / WEB_CONCURRENCY / "
            "GUNICORN_WORKERS, or implement real cross-process coordination before raising this."
        )
        logger.critical(message)
        raise MultiWorkerConfigurationError(message)
