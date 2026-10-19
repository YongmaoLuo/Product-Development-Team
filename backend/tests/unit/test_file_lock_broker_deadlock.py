"""Wait-for cycle detection in the file-lock broker (2026-10-19).

The production deadlock this pins
---------------------------------
Two sibling tasks each declared one file, the two declarations were
DISJOINT, so the sorted pre-fetch at task start had nothing to order, and
both sub-agents then went on to write files outside their declarations.
Each such write was acquired by the PreToolUse hook one at a time, in the
order the LLM happened to edit — so the acquisition order was whatever the
model did, and two of them landing in opposite orders is AB-BA. The
broker's own docstring admitted the hole: *"a task that holds key A and
waits for key B while another task holds B and waits for A will deadlock
until the acquire timeout expires"*. In production that timeout was 300
seconds, and the incident spun for 26 minutes.

The structural fix is layer 1 (the touched set must be a subset of the
declared set, so there is no mid-task acquisition to interleave). This
file is the BACKSTOP for whatever slips past it — a directory
declaration, a future sub-agent entry point that acquires on its own, or
a path added later that re-introduces incremental acquisition. It cannot
make the deadlock impossible; it can make it stop being a 300-second
mystery.

The contract, precisely
-----------------------
Exactly ONE side of a cycle is told. The task whose new wait closes the
loop gets :data:`DEADLOCK` immediately and the caller fails its task,
which releases everything it holds; the other side is then an ordinary
contention that resolves the moment that release happens. Both sides
being told would be wrong: the second one is waiting on a real holder,
and refusing its (perfectly resolvable) request would fail a task that
was about to succeed. So the tests below assert the asymmetry, and each
test that would otherwise hang waits for the holder's release instead of
the timeout.

Also pinned: no residue in ``_waiting`` / ``_waiting_for`` afterwards. A
wait-for edge left behind makes the next unrelated acquisition walk an
edge that no longer exists, and the detector would report a cycle that is
not there — which is how a backstop becomes a source of false failures.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from file_lock_broker import FileLockBroker  # noqa: E402
from file_lock_protocol import (  # noqa: E402
    ACQUIRED,
    DEADLOCK,
    TIMED_OUT,
    locks_dir_for_plan,
)


@pytest.fixture
def broker(tmp_path):
    """A running broker on a throwaway project dir, always torn down.

    ``stop()`` is in a ``finally`` because the suite fails any test whose
    threads outlive it.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    b = FileLockBroker(project, locks_dir_for_plan(tmp_path / "plans"))
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _wait_until(predicate, timeout=3.0, interval=0.02):
    """Poll ``predicate`` until true, or return False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------
def test_a_two_task_cycle_is_reported_as_deadlock_not_waited_out(broker):
    """A holds a, wants b; B holds b, wants a. One of them gets DEADLOCK.

    ``60.0`` is the per-side acquire timeout: long enough that a missing
    detector shows up as a verdict timeout rather than as a wrong answer,
    so the failure names the real regression instead of a race.
    """
    assert broker.acquire("A", "a.py", 5) == ACQUIRED
    assert broker.acquire("B", "b.py", 5) == ACQUIRED

    got: dict = {}

    def _grab(task, key):
        started = time.monotonic()
        state = broker.acquire(task, key, 60.0)
        # Elapsed is published before the verdict: the poller below keys on
        # the verdict, so a state visible without its elapsed stamp would
        # be a KeyError rather than an assertion.
        got[task + "_elapsed"] = time.monotonic() - started
        got[task] = state

    threads = [
        threading.Thread(target=_grab, args=("A", "b.py")),
        threading.Thread(target=_grab, args=("B", "a.py")),
    ]
    for t in threads:
        t.start()

    # Poll for the verdict rather than joining: a broker WITHOUT cycle
    # detection leaves the detecting side parked until its deadline, and
    # the assertion below fires on the clock instead of on the join.
    try:
        assert _wait_until(
            lambda: DEADLOCK in (got.get("A"), got.get("B")), timeout=2.0
        ), (
            "no side of the cycle was told it is deadlocked within 2s "
            f"(the default acquire timeout is 300s): {got!r}"
        )
        detected = "A" if got.get("A") == DEADLOCK else "B"
        assert got[detected + "_elapsed"] < 2.0, (
            f"the deadlock verdict took {got[detected + '_elapsed']:.1f}s: "
            f"the detector must answer on its first evaluation, not on "
            f"the acquire deadline"
        )
    finally:
        # Stand in for the executor's teardown of the task that was just
        # failed: it releases everything, which is what lets the other
        # side out of the queue.
        broker.release("A")
        broker.release("B")
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), (
                f"a waiter is still parked 30s after both tasks released: "
                f"{got!r}"
            )

    # Exactly one side was told. The other was in an ordinary, resolvable
    # contention, and refusing its request would fail a task that was
    # about to succeed.
    assert sum(1 for v in got.values() if v == DEADLOCK) == 1, got


def test_the_reported_cycle_names_the_chain(broker):
    """The verdict must name the chain, not just announce one.

    "There is a deadlock somewhere" is not actionable; "A waits on a file
    B holds, and B waits on a file A holds" names both tasks and can go
    straight into the run log.
    """
    assert broker.acquire("A", "a.py", 5) == ACQUIRED
    assert broker.acquire("B", "b.py", 5) == ACQUIRED

    emitted: list = []
    broker._emit = lambda event, data: emitted.append((event, data))

    got: dict = {}

    def _walk():
        got["B"] = broker.acquire("B", "a.py", 60.0)

    def _close():
        got["A"] = broker.acquire("A", "b.py", 60.0)

    t1 = threading.Thread(target=_walk)
    t1.start()
    # Let B publish its wait edge before A tries to close the loop, so
    # the chain is deterministic rather than "whoever got there first".
    assert _wait_until(lambda: broker._waiting_for.get("B") == "a.py"), (
        "B never entered the wait state; the fixture is not exercising "
        "what it claims to"
    )
    t2 = threading.Thread(target=_close)
    t2.start()
    try:
        t2.join(timeout=15)
        assert got.get("A") == DEADLOCK, (
            f"A closes the loop and must be the one told: {got!r}"
        )
        assert got.get("B") is None, (
            "B was told too; B is waiting on a real holder and its request "
            "resolves the moment that holder releases"
        )
    finally:
        broker.release("A")
        broker.release("B")
        for t in (t1, t2):
            t.join(timeout=30)

    deadlock_events = [d for e, d in emitted if e == "file_lock_deadlock"]
    assert deadlock_events, f"no file_lock_deadlock event was emitted: {emitted!r}"
    cycle = deadlock_events[0].get("cycle")
    assert cycle, (
        "the deadlock event carries no cycle: an operator reading the run "
        "log learns nothing about which two tasks are stuck"
    )
    assert cycle[0] == cycle[-1], (
        f"a wait-for cycle must return to its start, got {cycle!r}"
    )
    assert set(cycle) == {"A", "B"}, cycle

    # The cycle names the wedged pair; the wait-for graph is what an
    # operator reads to decide whether anything ELSE in the run is
    # merely queued behind them (which unblocks itself the moment A
    # releases) or holding something they need. One of those two is a
    # five-minute wait and the other is a hang, and "file_lock_deadlock
    # on b.py" alone does not say which.
    event = deadlock_events[0]
    assert event.get("held") == {"A": ["a.py"], "B": ["b.py"]}, (
        f"the event does not carry who holds what: {event.get('held')!r}"
    )
    assert event.get("waiting") == {"a.py": ["B"]}, (
        f"the event does not carry who is queued for what: "
        f"{event.get('waiting')!r}"
    )
    # B is still blocked on a live holder at the moment A is refused, so
    # its edge must be visible in the snapshot rather than dropped.
    assert event.get("waiting_for", {}).get("B") == "a.py", (
        f"the surviving waiter's edge is missing from the snapshot: "
        f"{event.get('waiting_for')!r}"
    )
    # It has to survive a JSON round trip: the broker's only way out is
    # the run log, and a graph that cannot be serialised is a graph an
    # operator never sees.
    assert json.loads(json.dumps(event))["waiting_for"]["B"] == "a.py"


def test_a_detected_deadlock_leaves_no_bookkeeping_behind(broker):
    """``_waiting`` / ``_waiting_for`` must both come back empty.

    Checked only after the holder releases, because until it does the
    surviving waiter's edge is REAL — it is genuinely blocked on a live
    lock. The assertion is about what the broker still knows once the
    whole arrangement unwinds, which is where a stale entry would do its
    damage.
    """
    assert broker.acquire("A", "a.py", 5) == ACQUIRED
    assert broker.acquire("B", "b.py", 5) == ACQUIRED

    out: dict = {}

    def _grab(task, key):
        out[task] = broker.acquire(task, key, 60.0)

    t1 = threading.Thread(target=_grab, args=("B", "a.py"))
    t1.start()
    assert _wait_until(lambda: broker._waiting_for.get("B") == "a.py")
    t2 = threading.Thread(target=_grab, args=("A", "b.py"))
    t2.start()
    try:
        t2.join(timeout=15)
        assert out.get("A") == DEADLOCK, out
    finally:
        broker.release("A")
        broker.release("B")
        for t in (t1, t2):
            t.join(timeout=30)
        broker.release("A")
        broker.release("B")

    assert broker._waiting_for == {}, (
        f"wait-for edges survived the arrangement unwinding: "
        f"{broker._waiting_for!r}"
    )
    assert broker._waiting == {}, (
        f"a refused waiter stayed in its queue: {broker._waiting!r}"
    )


def test_the_refusing_task_leaves_no_lock_row_for_the_refused_key(broker):
    """A refused acquire must not half-register.

    The verdict path drops the waiter and pops the edge; if either were
    skipped, the refused key would stay queued for a task that is no
    longer waiting, and every later acquirer of that file would sit behind
    a ghost.
    """
    assert broker.acquire("A", "a.py", 5) == ACQUIRED
    assert broker.acquire("B", "b.py", 5) == ACQUIRED

    out: dict = {}

    def _grab(task, key):
        out[task] = broker.acquire(task, key, 60.0)

    t1 = threading.Thread(target=_grab, args=("B", "a.py"))
    t1.start()
    assert _wait_until(lambda: broker._waiting_for.get("B") == "a.py")
    t2 = threading.Thread(target=_grab, args=("A", "b.py"))
    t2.start()
    try:
        t2.join(timeout=15)
        assert out.get("A") == DEADLOCK, out
        assert broker.waiting("b.py") == [], (
            "the task that was refused on b.py is still queued for it"
        )
        assert broker.held("A") == ["a.py"], (
            "A keeps what it legitimately held; the refusal only applies "
            "to the key it was refused"
        )
        assert broker._waiting_for.get("A") is None, (
            f"the refusing task kept a wait-for edge: "
            f"{broker._waiting_for!r}"
        )
    finally:
        broker.release("A")
        broker.release("B")
        for t in (t1, t2):
            t.join(timeout=30)
        broker.release("A")
        broker.release("B")


# ---------------------------------------------------------------------------
# what must NOT be called a deadlock
# ---------------------------------------------------------------------------
def test_a_contended_file_is_still_queued_not_called_a_deadlock(broker):
    """The ordinary case must be untouched — this is the one that matters.

    Two tasks contending for ONE file is normal, resolvable work: the
    holder finishes, the waiter proceeds. Reporting it as a deadlock would
    fail tasks that were about to succeed, which is far worse than the
    spin it was meant to prevent.
    """
    assert broker.acquire("holder", "a.py", 5) == ACQUIRED
    got: dict = {}

    def _waiter():
        started = time.monotonic()
        got["state"] = broker.acquire("waiter", "a.py", 30.0)
        got["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=_waiter)
    thread.start()
    try:
        assert _wait_until(lambda: broker._waiting_for.get("waiter") == "a.py")
        time.sleep(0.4)
        assert got.get("state") is None, (
            "the waiter gave up early; it should still be queued"
        )
        broker.release("holder")
    finally:
        thread.join(timeout=30)

    assert got.get("state") == ACQUIRED, (
        f"a plain contention must resolve to ACQUIRED, got {got!r}"
    )
    assert broker._waiting_for == {}, broker._waiting_for
    broker.release("waiter")


def test_the_cycle_walk_terminates_on_a_task_waiting_for_nothing(broker):
    """A one-hop chain that ends in an unblocked task is not a cycle.

    The walk's third exit: reaching a task that is holding but not
    waiting means the chain leads to work that WILL end. Treating that as
    a cycle would fail every ordinary queueing pattern the moment the
    second waiter looked at the first.
    """
    assert broker.acquire("holder", "a.py", 5) == ACQUIRED
    assert broker._deadlock_cycle("somebody_else", "a.py") is None


def test_an_unheld_key_is_never_a_deadlock(broker):
    """No holder means no edge, whatever else is queued."""
    assert broker.acquire("solo", "a.py", 5) == ACQUIRED
    # Nothing holds b.py at all, so waiting for it cannot close a loop.
    assert broker._deadlock_cycle("other", "b.py") is None
    broker.release("solo")


def test_a_task_waiting_on_its_own_lock_is_already_held_not_deadlocked(broker):
    """Re-editing a file the task already holds is not a cycle.

    The mid-task re-edit case the broker explicitly has to support: a
    sub-agent that edits the same file twice must not block on itself,
    and the walk must not report it as a self-cycle.
    """
    assert broker.acquire("A", "a.py", 5) == ACQUIRED
    assert broker.acquire("A", "a.py", 5) != DEADLOCK
    broker.release("A")


def test_a_timed_out_waiter_drops_its_wait_for_edge(broker):
    """The timeout exit has to clean up too, not just the deadlock exit.

    Three exits leave the wait loop — acquired, timed out, and (from the
    OS-lock probe) unavailable. Missing one would leave a phantom edge
    pointing at a task that is no longer waiting, and the next cycle walk
    would follow it.
    """
    assert broker.acquire("holder", "a.py", 5) == ACQUIRED
    assert broker.acquire("waiter", "a.py", 0.3) == TIMED_OUT
    assert broker._waiting_for == {}, (
        f"the timeout exit left a wait-for edge behind: {broker._waiting_for!r}"
    )
    assert broker.waiting("a.py") == []
    broker.release("holder")


# ---------------------------------------------------------------------------
# the wire: a real cycle must surface as CLI exit 5
# ---------------------------------------------------------------------------
def test_a_real_cycle_becomes_exit_code_5(tmp_path):
    """Broker verdict -> wire reply -> ``EXIT_DEADLOCK``.

    End-to-end through the CLI as a subprocess, because the exit code is
    the entire contract the hook has. Asserting on the broker's return
    value instead would leave the mapping untested, and a mapping that
    silently degraded to ``EXIT_BUSY`` would send the sub-agent back to
    wait on a deadlock — which is the 26-minute spin all over again.

    3 (busy, retry) and 5 (deadlock, do not retry) must stay distinct, so
    both codes are checked here.
    """
    import subprocess as _sp

    from file_lock_cli import EXIT_BUSY, EXIT_DEADLOCK, EXIT_OK
    from file_lock_protocol import locks_dir_for_plan, socket_path

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    locks = locks_dir_for_plan(tmp_path / "plans")
    b = FileLockBroker(project, locks)
    b.start()
    cli = _BACKEND_DIR / "file_lock_cli.py"

    def _acquire(task, path, timeout=30.0):
        return _sp.run(
            [sys.executable, str(cli), "acquire", "--task", task,
             "--path", str(project / path),
             "--timeout", str(timeout)],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "PDT_LOCK_BROKER": str(socket_path(project))},
            timeout=60,
        )

    try:
        # Build the cycle in-process (the two holders), then let ONE real
        # CLI invocation close it — that invocation is what must report 5.
        assert b.acquire("A", "a.py", 5) == ACQUIRED
        assert b.acquire("B", "b.py", 5) == ACQUIRED

        result: dict = {}

        def _park():
            # B waits for a.py; this thread supplies the edge the CLI then
            # follows back to A.
            result["B"] = b.acquire("B", "a.py", 30.0)

        t = threading.Thread(target=_park)
        t.start()
        try:
            assert _wait_until(lambda: b._waiting_for.get("B") == "a.py")
            proc = _acquire("A", "b.py", timeout=20.0)
        finally:
            b.release("A")
            b.release("B")
            t.join(timeout=30)
            b.release("A")
            b.release("B")

        assert proc.returncode == EXIT_DEADLOCK, (
            f"a closed wait-for cycle must exit {EXIT_DEADLOCK}, got "
            f"{proc.returncode}: {proc.stdout!r} {proc.stderr!r}"
        )
        assert proc.returncode != EXIT_BUSY, (
            "a deadlock reported as busy sends the sub-agent back to wait "
            "on a condition that can never clear"
        )
    finally:
        b.stop()

    # And the codes the CLI promises are still distinct.
    assert EXIT_OK == 0 and EXIT_BUSY == 3 and EXIT_DEADLOCK == 5
    assert len({EXIT_OK, EXIT_BUSY, EXIT_DEADLOCK}) == 3
