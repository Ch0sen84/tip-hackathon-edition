"""Phase D: the ONE shared UW concurrency gate every call path funnels
through, plus interactive-over-background priority.

AUDITED CALL PATHS (every single place that can issue a live UW HTTP
request, confirmed by reading each one this phase):
  * Manual Radar scan            -- GET /api/radar            -> get_adapter()
  * Market Discovery              -- POST /api/radar/discover  -> get_adapter()
  * Ticker Analysis               -- GET /api/ticker/{ticker}  -> get_adapter()
  * Dark-pool pagination backfill (Phase B) -- app/dark_pool_pagination.py,
    DarkPoolPaginator is always constructed from an adapter obtained via
    get_adapter() by its caller (never builds its own UWRestClient).
  * AI Investigation's REST tool client -- app/ai/rest_tool_client.py
    wraps the SAME adapter.client (UWRestClient) passed to it; never
    constructs a second one.
  * Phase D background scheduler (new this phase) -- app/scheduler.py,
    via the injected get_adapter() callable, i.e. the SAME adapter.

Every one of those ultimately calls through exactly one
`LiveUWAdapter.client` (a `UWRestClient`), and that class's `get()`
acquires THIS module's shared gate rather than a semaphore private to
its own instance -- see live_uw_adapter.py's UWRestClient.__init__.
Before this phase each UWRestClient held its OWN
`threading.Semaphore`, which only ever actually mattered because
app.main.get_adapter() happens to cache and reuse one adapter instance
per (mode, key, watchlist) signature; the moment that signature changed
(a mode toggle, a new key, a watchlist edit) a BRAND NEW UWRestClient
with a BRAND NEW semaphore was constructed, silently resetting the
concurrency ceiling's bookkeeping mid-flight for any still-in-progress
request on the old instance. Making the gate itself process-global
closes that gap structurally, not by convention.

PRIORITY: background (scheduler) traffic must never starve an
interactive (user-initiated) request competing for the same bounded
slots. `PriorityGate.acquire(priority=...)` implements this concretely:
an interactive acquire always just takes the next free slot (fair,
uncontested by background callers because of the rule below). A
background acquire first checks whether any interactive caller is
currently waiting for a slot; if so, it backs off and retries rather
than racing evenly for the next release -- i.e. background traffic
voluntarily yields the head of the queue to interactive traffic, never
"hopes the semaphore is fair".
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Optional

INTERACTIVE = "interactive"
BACKGROUND = "background"

_BACKGROUND_BACKOFF_S = float(os.environ.get("TIP_UW_BACKGROUND_BACKOFF_S", "0.05"))


class PriorityGate:
    """A bounded-concurrency gate (like a semaphore) where callers tagged
    `priority="background"` yield to any `priority="interactive"` caller
    currently waiting for a slot."""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max(1, max_concurrent)
        self._slots = threading.Semaphore(self.max_concurrent)
        self._state_lock = threading.Lock()
        self._interactive_waiting = 0
        self.interactive_in_flight = 0
        self.background_in_flight = 0
        self.max_observed_in_flight = 0

    def _note_in_flight(self, delta: int, priority: str) -> None:
        with self._state_lock:
            if priority == INTERACTIVE:
                self.interactive_in_flight += delta
            else:
                self.background_in_flight += delta
            total = self.interactive_in_flight + self.background_in_flight
            if total > self.max_observed_in_flight:
                self.max_observed_in_flight = total

    @contextmanager
    def acquire(self, priority: str = INTERACTIVE, sleep_fn=None):
        sleep_fn = sleep_fn or time.sleep
        if priority == INTERACTIVE:
            with self._state_lock:
                self._interactive_waiting += 1
            try:
                self._slots.acquire()
            finally:
                with self._state_lock:
                    self._interactive_waiting -= 1
        else:
            # Background: never race an interactive caller for the next
            # free slot. Poll non-blockingly, backing off whenever an
            # interactive caller is currently waiting.
            while True:
                with self._state_lock:
                    someone_interactive_waiting = self._interactive_waiting > 0
                if not someone_interactive_waiting and self._slots.acquire(blocking=False):
                    break
                sleep_fn(_BACKGROUND_BACKOFF_S)
        self._note_in_flight(1, priority)
        try:
            yield
        finally:
            self._note_in_flight(-1, priority)
            self._slots.release()


_lock = threading.Lock()
_shared_gate: Optional[PriorityGate] = None
_shared_gate_capacity: Optional[int] = None

# Thread-local priority tag: defaults to "interactive" for every ordinary
# request-handling thread (uvicorn's worker threads, TestClient calls,
# etc). The background scheduler explicitly flips this for the duration
# of its own UW calls via `background_priority()` below -- nothing else
# in the app ever needs to touch this.
_tls = threading.local()


def get_shared_gate(max_concurrent: int) -> PriorityGate:
    """Returns the ONE process-wide PriorityGate, sized to
    `max_concurrent`. Resizing (a changed max_concurrent_requests config)
    replaces the gate -- acceptable because that only ever happens at
    settings-change time, never mid-flight in the real deployment;
    documented here rather than silently assumed."""
    global _shared_gate, _shared_gate_capacity
    with _lock:
        if _shared_gate is None or _shared_gate_capacity != max_concurrent:
            _shared_gate = PriorityGate(max_concurrent)
            _shared_gate_capacity = max_concurrent
        return _shared_gate


def reset_shared_gate_for_tests() -> None:
    """Test-only: forces the next get_shared_gate() call to build a brand
    new gate instead of reusing one left over from a previous test."""
    global _shared_gate, _shared_gate_capacity
    with _lock:
        _shared_gate = None
        _shared_gate_capacity = None


def current_priority() -> str:
    return getattr(_tls, "priority", INTERACTIVE)


@contextmanager
def background_priority():
    """Marks every UW call made on THIS thread, for the duration of this
    context, as background priority. Used exclusively by the Phase D
    scheduler -- every other caller (route handlers, AI investigation,
    on-demand pagination) never calls this and stays interactive by
    default."""
    previous = getattr(_tls, "priority", INTERACTIVE)
    _tls.priority = BACKGROUND
    try:
        yield
    finally:
        _tls.priority = previous
