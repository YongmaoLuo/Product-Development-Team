"""The batch acquisition is all-or-nothing (2026-10-19).

This is a REGRESSION PIN for a piece of behaviour that already existed
(PR1's ``except Exception: handle.release()``) and that the new
declaration rule depends on being true.

Why it now matters more than it did
----------------------------------
``_pre_task_lock_hook`` takes a task's whole declared set, sorted, up
front. That ordering is the only ordering a concurrent executor can
apply, and it is what makes the deadlock structurally impossible **once
the touched set is a subset of the declared set** (the hook's new
``PDT_ALLOWED_WRITE_FILES`` check). "Once" is load-bearing: if the batch
were not atomic, a task could hold A while blocked on B, and that is
exactly the state AB-BA is made of — the structural fix would be undone
by a rollback that forgot to run.

Nothing in the tests below could fail loudly if the rollback were
removed. A task that fails to acquire leaves its already-acquired locks
behind; the next task declaring one of those files then queues against a
holder that has already exited. The symptom is a second, unrelated task
hanging to its own timeout, on a file nobody is editing — which is
indistinguishable, from the run log, from the deadlock this whole change
exists to remove.

The assertions are therefore about the BROKER'S STATE after a failed
acquisition, not about the exception: ``broker.held(task)`` must be
empty, the key that was acquired first must be free for the next task,
and the in-memory conflict map must not have kept a claim either.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from file_lock_protocol import ACQUIRED  # noqa: E402
from task import SubTask  # noqa: E402


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(project_dir), capture_output=True, text=True, check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Atomic Test"],
        cwd=str(project_dir), capture_output=True, text=True, check=False,
    )


def _make_agent(project_dir: Path) -> AutonomousAgent:
    return AutonomousAgent(
        requirement=None,
        project_dir=project_dir,
        coding_tool=Mock(),
    )


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    _git_init(p)
    return p


@pytest.fixture
def agent_with_broker(project, monkeypatch):
    """An agent with a LIVE broker — the rollback path only exists there.

    Without a broker the hook falls back to ``FileLockManager``, which is
    a different code path and would make these tests pass without
    exercising the thing they are about.

    ``PDT_LOCK_ACQUIRE_TIMEOUT`` is shortened because these tests put a
    real task behind a real lock on purpose. The production default is
    300s PER KEY, so an unset value turns a rollback assertion into a
    five-minute wait — and a suite that has to be waited out is a suite
    nobody re-runs when the wait grows.
    """
    monkeypatch.setenv("PDT_LOCK_ACQUIRE_TIMEOUT", "0.5")
    agent = _make_agent(project)
    broker = agent._ensure_lock_broker()
    assert broker is not None, "the broker must start on a POSIX host"
    try:
        yield agent, broker
    finally:
        agent._stop_lock_broker()


def _task(task_id: str, files: list[str]) -> SubTask:
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description="## 修改位置\n（见 files_to_modify）\n",
        files_to_modify=list(files),
        test_command="echo ok",
    )


# ---------------------------------------------------------------------------
# the rollback
# ---------------------------------------------------------------------------
def test_a_failed_batch_releases_the_keys_it_already_got(agent_with_broker):
    """``[a, b]`` with ``b`` held: ``a`` must come back.

    ``sorted`` puts ``a`` before ``b``, so ``a`` is genuinely acquired
    before the hook discovers it cannot have ``b``. Without the rollback
    the task holds ``a`` for the rest of the run.
    """
    agent, broker = agent_with_broker
    assert broker.acquire("blocker", "b.py", 5) == ACQUIRED

    task = _task("t1", ["a.py", "b.py"])
    with pytest.raises(RuntimeError) as exc:
        agent._pre_task_lock_hook(task)

    assert "b.py" in str(exc.value), exc.value
    assert broker.held("t1") == [], (
        f"a half-acquired batch leaked {broker.held('t1')!r}: the task "
        f"holds a file nobody is working on and every later acquirer of "
        f"it queues against a holder that already exited"
    )


def test_a_leaked_key_is_really_free_for_the_next_task(agent_with_broker):
    """Not "absent from the table" — actually acquirable.

    ``held()`` reads the broker's own dict. What matters is the OS lock:
    the next task must be able to take the key immediately, with no
    timeout and no help.
    """
    agent, broker = agent_with_broker
    assert broker.acquire("blocker", "b.py", 5) == ACQUIRED

    with pytest.raises(RuntimeError):
        agent._pre_task_lock_hook(_task("t1", ["a.py", "b.py"]))

    broker.release("blocker")
    started = time.monotonic()
    assert broker.acquire("t2", "a.py", 5) == ACQUIRED
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, (
        f"the next task waited {elapsed:.1f}s for a key whose holder is "
        f"gone: the rollback did not release the flock"
    )
    broker.release("t2")


def test_a_failed_batch_leaves_no_in_flight_claim(agent_with_broker):
    """The in-memory half of the rollback matters as much as the OS half.

    ``_in_flight_files`` is the fast-fail same-layer check. A stale entry
    turns every later task that names the same file into "File conflict
    detected" — permanently, with no process holding anything — so the
    plan stalls on a conflict that does not exist.
    """
    agent, broker = agent_with_broker
    assert broker.acquire("blocker", "b.py", 5) == ACQUIRED

    with pytest.raises(RuntimeError):
        agent._pre_task_lock_hook(_task("t1", ["a.py", "b.py"]))

    with agent._in_flight_lock:
        leftover = [f for f in ("a.py", "b.py") if f in agent._in_flight_files]
    assert leftover == [], (
        f"the failed task still claims {leftover!r} in the in-flight map; "
        f"every later task naming them now fails a conflict check that no "
        f"live task is party to"
    )

    # And the fast-fail check really is clean again.
    broker.release("blocker")
    files, handle = agent._pre_task_lock_hook(_task("t2", ["a.py", "b.py"]))
    try:
        assert sorted(files) == ["a.py", "b.py"]
    finally:
        agent._post_task_lock_hook("t2", files, handle)


def test_the_batch_is_taken_in_sorted_order(agent_with_broker):
    """The ordering the whole scheme rests on.

    Two tasks declaring overlapping sets can only avoid AB-BA if both
    acquire in the same global order. ``sorted()`` is that order; taking
    them in declaration order would reintroduce the deadlock the moment
    two tasks happened to list their shared files in opposite sequences.

    The order is read off the acquire CALLS rather than inferred from a
    contention: blocking a real task behind a real lock would make this a
    test of the timeout, and would take seconds to answer a question that
    is a pure ordering decision.
    """
    agent, broker = agent_with_broker

    order: list = []
    real_acquire = broker.acquire

    def _recording(task_id, target, timeout=None):
        order.append(str(target))
        return real_acquire(task_id, target, timeout)

    broker.acquire = _recording
    try:
        # Deliberately declared in the OPPOSITE order.
        files, handle = agent._pre_task_lock_hook(_task("t1", ["b.py", "a.py"]))
    finally:
        broker.acquire = real_acquire

    try:
        assert order == ["a.py", "b.py"], (
            f"the batch must be acquired in sorted order, got {order!r}: "
            f"this is the one global order every participant agrees on"
        )
        assert broker.held("t1") == ["a.py", "b.py"]
    finally:
        agent._post_task_lock_hook("t1", files, handle)


# ---------------------------------------------------------------------------
# the directory guard (1c)
# ---------------------------------------------------------------------------
def test_a_directory_declaration_fails_before_anything_is_locked(agent_with_broker):
    """A directory is neither a lock key nor a permission, so it is refused.

    Locking it would create a lock nobody could ever contend on (every
    task locks the same directory name, while the files inside it stay
    unlocked), and allowing it would grant no write at all, because the
    guard matches declarations exactly. Failing here says the true thing:
    the declaration is not a list of files.
    """
    agent, broker = agent_with_broker
    (agent.project_dir / "backend").mkdir(parents=True, exist_ok=True)

    task = _task("t1", ["backend", "a.py"])
    with pytest.raises(RuntimeError) as exc:
        agent._pre_task_lock_hook(task)

    assert "files_to_modify must name files, not directories" in str(exc.value)
    assert "backend" in str(exc.value)
    assert broker.held("t1") == [], (
        "the directory check must run BEFORE acquisition, or the sibling "
        "file would still have been locked"
    )


def test_a_directory_declaration_is_refused_even_when_it_is_the_only_entry(
    agent_with_broker,
):
    """It is not a mixed-list edge case; on its own it is still wrong."""
    agent, broker = agent_with_broker
    (agent.project_dir / "backend").mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError):
        agent._pre_task_lock_hook(_task("t1", ["backend"]))


# ---------------------------------------------------------------------------
# the executor's reading of a deadlock
# ---------------------------------------------------------------------------
def test_a_broker_deadlock_is_reported_as_a_deadlock_not_a_timeout(
    agent_with_broker,
):
    """``DEADLOCK`` and ``TIMED_OUT`` must not collapse into one message.

    ``lock_conflict`` classifies both, but they are different diagnoses
    with different remedies: a timeout may resolve on its own and the
    right answer is a retry, a deadlock provably cannot and the right
    answer is to report it. A single wording would send the executor down
    the retry path for a task that can never run.
    """
    agent, broker = agent_with_broker
    assert broker.acquire("holder", "a.py", 5) == ACQUIRED

    out: dict = {}

    def _wait():
        out["state"] = broker.acquire("waiter", "a.py", 0.3)

    thread = threading.Thread(target=_wait)
    thread.start()
    thread.join(timeout=15)

    with pytest.raises(RuntimeError) as exc:
        agent._pre_task_lock_hook(_task("t1", ["a.py"]))
    assert "File lock timeout" in str(exc.value)
    assert "deadlock" not in str(exc.value).lower()
    broker.release("holder")


def test_a_deadlock_on_a_declared_file_fails_the_task_with_the_deadlock_word(
    agent_with_broker,
):
    """The message the classifier keys on.

    Built directly rather than through threads: the assertion is about
    the wording the executor produces from a ``DEADLOCK`` verdict, and
    the verdict itself is pinned in ``test_file_lock_broker_deadlock``.
    """
    agent, broker = agent_with_broker
    task = _task("t1", ["a.py"])

    # Stand in for the broker's answer. ``_pre_task_lock_hook`` reads the
    # verdict from ``broker.acquire`` and nothing else, so pinning the
    # verdict pins the message.
    class _DeadlockingBroker:
        def acquire(self, task_id, path, timeout=None):
            from file_lock_protocol import DEADLOCK
            return DEADLOCK

        def held(self, task_id):
            return []

    original = agent._active_lock_broker
    agent._active_lock_broker = lambda: _DeadlockingBroker()
    try:
        with pytest.raises(RuntimeError) as exc:
            agent._pre_task_lock_hook(task)
    finally:
        agent._active_lock_broker = original

    message = str(exc.value)
    assert "File lock deadlock" in message, message
    assert "a.py" in message, message
