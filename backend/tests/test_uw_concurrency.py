"""Phase D, item 2 + 5: the shared process-wide UW concurrency gate
actually enforces its ceiling, and interactive traffic is never
starved by background traffic competing for the same budget."""
from __future__ import annotations

import threading
import time

from app.core import uw_concurrency
from app.core.uw_concurrency import BACKGROUND, INTERACTIVE, PriorityGate


def test_ceiling_is_actually_enforced_across_many_concurrent_callers():
    """Launches N=12 concurrent acquires against a gate sized for 2 and
    confirms AT MOST 2 are ever in flight simultaneously -- not just
    that the total eventually completes."""
    gate = PriorityGate(max_concurrent=2)
    in_flight = {"current": 0, "max_seen": 0}
    lock = threading.Lock()

    def worker():
        with gate.acquire(priority=INTERACTIVE):
            with lock:
                in_flight["current"] += 1
                in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
            time.sleep(0.03)
            with lock:
                in_flight["current"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert in_flight["max_seen"] <= 2
    assert in_flight["max_seen"] >= 1  # sanity: something actually ran concurrently/at all


def test_ceiling_enforced_with_mixed_interactive_and_background_callers():
    gate = PriorityGate(max_concurrent=2)
    in_flight = {"current": 0, "max_seen": 0}
    lock = threading.Lock()

    def worker(priority):
        with gate.acquire(priority=priority):
            with lock:
                in_flight["current"] += 1
                in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
            time.sleep(0.03)
            with lock:
                in_flight["current"] -= 1

    threads = []
    for i in range(10):
        p = INTERACTIVE if i % 2 == 0 else BACKGROUND
        threads.append(threading.Thread(target=worker, args=(p,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert in_flight["max_seen"] <= 2


def test_interactive_request_not_starved_by_background_loop_holding_budget():
    """A background caller holds BOTH slots in a tight re-acquire loop
    (simulating a busy scheduler). A single interactive call arrives
    shortly after and must still complete within a bounded time -- i.e.
    the background loop backs off for it rather than racing it evenly
    for every freed slot."""
    gate = PriorityGate(max_concurrent=2)
    stop = threading.Event()
    background_iterations = {"count": 0}

    def background_loop():
        while not stop.is_set():
            with gate.acquire(priority=BACKGROUND):
                background_iterations["count"] += 1
                time.sleep(0.005)

    bg_threads = [threading.Thread(target=background_loop, daemon=True) for _ in range(2)]
    for t in bg_threads:
        t.start()
    time.sleep(0.05)  # let the background loop get going and occupy both slots

    interactive_done = threading.Event()
    interactive_started_at = {"t": None}
    interactive_acquired_at = {"t": None}

    def interactive_call():
        interactive_started_at["t"] = time.monotonic()
        with gate.acquire(priority=INTERACTIVE):
            interactive_acquired_at["t"] = time.monotonic()
            time.sleep(0.01)
        interactive_done.set()

    it = threading.Thread(target=interactive_call)
    it.start()
    it.join(timeout=2.0)

    stop.set()
    for t in bg_threads:
        t.join(timeout=1.0)

    assert interactive_done.is_set(), "interactive call never completed -- starved by background traffic"
    wait_time = interactive_acquired_at["t"] - interactive_started_at["t"]
    # Bounded wait: well under the background's own 0.005s hold time
    # repeated many times over -- proves it got priority rather than
    # waiting out an indefinite number of background cycles.
    assert wait_time < 0.3, f"interactive call waited {wait_time:.3f}s for a slot -- looks starved"


def test_background_backs_off_rather_than_racing_waiting_interactive_caller():
    """Direct proof of the mechanism (not just the outcome above): while
    an interactive caller is marked as WAITING for a slot, a background
    acquire() must not succeed in grabbing a just-freed slot ahead of
    it."""
    gate = PriorityGate(max_concurrent=1)
    order = []
    order_lock = threading.Lock()

    # Occupy the one slot so both the interactive and background callers
    # below have to wait.
    first_holder_released = threading.Event()

    def hold_then_release():
        with gate.acquire(priority=INTERACTIVE):
            time.sleep(0.05)
        first_holder_released.set()

    holder = threading.Thread(target=hold_then_release)
    holder.start()
    time.sleep(0.01)  # ensure holder has the slot first

    def interactive_waiter():
        with gate.acquire(priority=INTERACTIVE):
            with order_lock:
                order.append("interactive")
            time.sleep(0.02)

    def background_waiter():
        with gate.acquire(priority=BACKGROUND):
            with order_lock:
                order.append("background")

    interactive_thread = threading.Thread(target=interactive_waiter)
    background_thread = threading.Thread(target=background_waiter)
    interactive_thread.start()
    time.sleep(0.005)  # let the interactive waiter register as waiting first
    background_thread.start()

    interactive_thread.join(timeout=2.0)
    background_thread.join(timeout=2.0)
    holder.join(timeout=2.0)

    assert order[0] == "interactive", f"expected interactive to acquire first, got order={order}"


def test_shared_gate_sizes_to_configured_capacity():
    uw_concurrency.reset_shared_gate_for_tests()
    gate = uw_concurrency.get_shared_gate(2)
    assert gate.max_concurrent == 2
    gate_again = uw_concurrency.get_shared_gate(2)
    assert gate_again is gate  # same instance reused, not rebuilt every call


def test_background_priority_context_manager_tags_current_thread():
    assert uw_concurrency.current_priority() == INTERACTIVE
    with uw_concurrency.background_priority():
        assert uw_concurrency.current_priority() == BACKGROUND
    assert uw_concurrency.current_priority() == INTERACTIVE  # restored after the context exits


def test_background_priority_is_thread_local_not_global():
    """Flipping priority on one thread must never affect another thread
    -- proves this is a real thread-local, not a module-global flag that
    would let a background scheduler thread accidentally downgrade an
    interactive request handled on a different thread."""
    results = {}

    def other_thread():
        results["other"] = uw_concurrency.current_priority()

    with uw_concurrency.background_priority():
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        results["this"] = uw_concurrency.current_priority()

    assert results["this"] == BACKGROUND
    assert results["other"] == INTERACTIVE
