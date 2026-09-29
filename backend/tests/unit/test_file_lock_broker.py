"""Unit tests for the file-lock broker and its wire protocol.

What these pin
--------------
The broker exists so a sub-agent can be *made to wait* for a file another
task holds, including files no plan declared. The behaviours below are
the contract the executor and the Edit/Write hook depend on; each test
names the failure it prevents rather than restating the code.

Two of them are regressions against defects found while building this,
both of which were silent:

* :func:`test_release_from_a_different_thread_frees_the_lock` — the first
  implementation used ``filelock``, whose ``release()`` is thread-scoped:
  called from a different thread than ``acquire()`` it returns
  successfully and frees nothing. Every release in this design crosses
  threads, so every release would have no-opped.
* :func:`test_socket_path_survives_a_deep_checkout` — the socket was
  first placed at ``<project_dir>/.pdt/lock-broker.sock``, which on macOS
  overruns the 104-byte ``AF_UNIX`` path limit for a moderately nested
  checkout and fails at ``bind``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from file_lock_broker import DEFAULT_ACQUIRE_TIMEOUT, FileLockBroker  # noqa: E402
from file_lock_protocol import (  # noqa: E402
    ALREADY_HELD,
    TIMED_OUT,
    UNAVAILABLE,
    lock_file_path,
    locks_dir_for_plan,
    normalize_target,
    socket_path,
)


@pytest.fixture
def plan_dir(tmp_path):
    """A stand-in plan directory: the lock files go in ``<plan>/locks/``."""
    d = tmp_path / "plans" / "20260101-a-plan"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def broker(tmp_path, plan_dir):
    """A running broker on a throwaway project dir, always torn down.

    ``stop()`` is in a ``finally`` because the process-wide test guard
    fails any test whose threads outlive it.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    b = FileLockBroker(project, locks_dir_for_plan(plan_dir))
    b.start()
    try:
        yield b
    finally:
        b.stop()


# ---------------------------------------------------------------------------
# path canonicalisation
# ---------------------------------------------------------------------------
def test_relative_and_absolute_targets_share_one_lock(tmp_path):
    """One real file must be one lock, however it was spelled.

    The executor hands over project-relative paths (from a task's
    ``files_to_modify``); the Edit/Write hook only ever sees absolute ones
    from its tool input. If those hash differently, two sub-agents each
    hold "the lock" for one file and edit it concurrently.
    """
    project = tmp_path / "project"
    relative = normalize_target(project, "backend/agent.py")
    absolute = normalize_target(project, str(project / "backend" / "agent.py"))

    assert relative == absolute == "backend/agent.py"


def test_dot_segments_do_not_create_a_second_key(tmp_path):
    project = tmp_path / "project"
    assert normalize_target(project, "backend/./agent.py") == normalize_target(
        project, "backend/agent.py"
    )
    assert normalize_target(project, "backend/x/../agent.py") == normalize_target(
        project, "backend/agent.py"
    )


def test_lock_files_live_beside_the_plan_not_in_the_workspace(tmp_path, plan_dir):
    """Locks belong to the plan, and must not land in the user's tree.

    They used to be ``<project_dir>/.pdt/locks/``, inside the workspace.
    That tree is a git working tree and the executor checkpoints each task
    with ``git add -A``, so the files were committed; measured on
    2026-09-26, 14 task commits carried 32 of them. The plan directory is
    the server's own (its ``plans/`` is gitignored), so nothing can leak.
    """
    project = tmp_path / "project"
    project.mkdir()
    path = lock_file_path(locks_dir_for_plan(plan_dir), project, "backend/agent.py")

    assert project not in path.parents, f"lock file is inside the workspace: {path}"
    assert plan_dir in path.parents, f"lock file is not beside the plan: {path}"


def test_broker_and_manager_agree_on_the_lock_file(tmp_path, plan_dir):
    """The broker and ``FileLockManager`` must contend on one file.

    They use different primitives (raw ``flock`` vs ``filelock``) but must
    resolve the same target to the same lock file. If the two ever
    disagreed, a broker and a manager inside one process would each hold
    a *different* lock for one file — exclusion that looks like it works
    and is not.
    """
    from file_lock_manager import FileLockManager

    project = tmp_path / "project"
    project.mkdir()
    locks = locks_dir_for_plan(plan_dir)

    assert FileLockManager._lock_file_path(
        "backend/agent.py", str(project), locks
    ) == lock_file_path(locks, project, "backend/agent.py")
    # And an absolute spelling canonicalises to the same file.
    assert lock_file_path(locks, project, "backend/agent.py") == lock_file_path(
        locks, project, str(project / "backend" / "agent.py")
    )


def test_socket_path_survives_a_deep_checkout(tmp_path):
    """The socket must not be derived from the project dir path itself.

    ``AF_UNIX`` caps the whole path (104 bytes on macOS) and the obvious
    ``<project_dir>/.pdt/lock-broker.sock`` blows it for a nested
    checkout — ``bind`` raises and the lock layer disappears on exactly
    the machines least likely to be noticed.
    """
    deep = tmp_path / ("nested-" + "d" * 40) / ("e" * 40) / "project"
    path = socket_path(deep)

    assert len(str(path)) < 100, f"AF_UNIX path too long: {len(str(path))}"
    # Deterministic: the executor and every sub-agent derive it separately.
    assert socket_path(deep) == path
    assert socket_path(tmp_path / "other") != path


# ---------------------------------------------------------------------------
# acquisition semantics
# ---------------------------------------------------------------------------
def test_acquire_then_reacquire_by_the_same_task_is_not_a_deadlock(broker):
    """A task that edits a file twice must not block on its own lock."""
    assert broker.acquire("t1", "a.py", 5) == "acquired"
    assert broker.acquire("t1", "a.py", 5) == ALREADY_HELD


def test_second_task_waits_and_then_gets_it(broker):
    """The core promise: contended means *queued*, not *refused*."""
    assert broker.acquire("t1", "a.py", 5) == "acquired"
    got = {}

    def waiter():
        start = time.monotonic()
        got["state"] = broker.acquire("t2", "a.py", 10)
        got["waited"] = time.monotonic() - start

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        time.sleep(0.6)
        assert broker.waiting("a.py") == ["t2"], "t2 should be queued, not done"
        broker.release("t1")
    finally:
        thread.join(timeout=15)

    assert got.get("state") == "acquired"
    assert got["waited"] > 0.5, "t2 must have waited for the release"


def test_waiters_are_served_first_come_first_served(broker):
    """A file contended by several tasks is edited in request order.

    FIFO rather than retry-races, so a task that has been waiting cannot
    be starved by one that arrived later.
    """
    order: list = []
    assert broker.acquire("holder", "a.py", 5) == "acquired"

    threads = []
    for n in (1, 2, 3):
        thread = threading.Thread(
            target=lambda n=n: (broker.acquire(f"q{n}", "a.py", 15), order.append(n))
        )
        thread.start()
        threads.append(thread)
        time.sleep(0.15)

    try:
        time.sleep(0.4)
        assert broker.waiting("a.py") == ["q1", "q2", "q3"]
        for task in ("holder", "q1", "q2"):
            broker.release(task)
            time.sleep(0.35)
    finally:
        for thread in threads:
            thread.join(timeout=15)

    assert order == [1, 2, 3]


def test_acquire_gives_up_and_reports_a_timeout(broker):
    assert broker.acquire("hog", "a.py", 5) == "acquired"
    start = time.monotonic()
    assert broker.acquire("late", "a.py", 0.8) == TIMED_OUT
    assert time.monotonic() - start < 5, "must stop waiting at its deadline"


def test_timed_out_waiter_leaves_the_queue(broker):
    """A give-up must not strand the queue behind a task that left.

    If the timed-out task stayed at the head, every later waiter would
    block until its own deadline even though the file was free.
    """
    assert broker.acquire("hog", "a.py", 5) == "acquired"
    assert broker.acquire("late", "a.py", 0.5) == TIMED_OUT
    broker.release("hog")

    assert broker.waiting("a.py") == []
    assert broker.acquire("next", "a.py", 5) == "acquired"


def test_release_all_for_a_task_frees_every_key_it_holds(broker):
    """The executor's single release point must clear the whole task.

    Both the files declared at task start and the ones the hook acquired
    mid-task are keyed to the task id, so one call at the end reclaims
    them; anything left behind would block the next run.
    """
    broker.acquire("t1", "a.py", 5)
    broker.acquire("t1", "backend/b.py", 5)
    broker.acquire("t2", "c.py", 5)

    assert broker.release("t1") == 2
    assert broker.held("t1") == []
    assert broker.held("t2") == ["c.py"]


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------
def test_release_from_a_different_thread_frees_the_lock(broker):
    """Regression: ``filelock`` releases are thread-scoped.

    Acquire happens on the broker's per-connection thread; release happens
    on the executor's task thread. With ``filelock`` the cross-thread
    ``release()`` returns success and frees nothing — measured, not
    assumed — so every lock would have been held until the executor
    exited. The broker uses raw ``flock`` precisely to avoid that.
    """
    acquired_in = {}

    def acquire_on_another_thread():
        acquired_in["state"] = broker.acquire("t1", "a.py", 5)

    thread = threading.Thread(target=acquire_on_another_thread)
    thread.start()
    thread.join(timeout=10)
    assert acquired_in["state"] == "acquired"

    # Released from THIS thread, not the one that acquired.
    assert broker.release("t1") == 1
    assert broker.acquire("t2", "a.py", 2) == "acquired", (
        "the lock was not actually freed by the cross-thread release"
    )


def test_stop_releases_everything_and_removes_the_socket(tmp_path, plan_dir):
    """A finished run must leave neither held locks nor a stale socket.

    An orphaned socket would be *adopted* by the next executor, which
    would then route its locks to a process that is not serving.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True)
    broker = FileLockBroker(project, locks_dir_for_plan(plan_dir))
    sock = broker.start()
    broker.acquire("t1", "a.py", 5)

    try:
        assert Path(sock).exists()
    finally:
        broker.stop()

    assert broker.held("t1") == []
    assert not sock_path_exists(sock), "stale socket left for the next run to adopt"


def sock_path_exists(path: str) -> bool:
    return Path(path).exists()


def test_stop_ends_the_serving_thread_even_when_close_cannot_wake_it(
    tmp_path, plan_dir, monkeypatch
):
    """``stop()`` must not depend on ``close()`` to end the accept loop.

    The defect this pins was found on the CI runner, not here. On Linux a
    thread already blocked in ``accept()`` keeps the file description
    alive through the pending syscall, so closing the listening socket
    does NOT wake it: ``_serve`` never sees the ``OSError`` it waits for,
    the thread outlives ``stop()``'s join, and the conftest leak guard
    fails the test with ``pdt-lock-broker ... outlived their test``.
    macOS *does* wake it (the blocked accept fails ECONNABORTED), so the
    bug is invisible on a developer machine and reproducible only on the
    runner.

    So the platform is simulated rather than relied on: ``close()`` is
    neutralised, which is exactly the Linux behaviour. A broker that
    ends its thread by polling a stop flag still shuts down; one that
    waits for ``close()`` to deliver an error hangs here.
    """

    class _CloseInertSocket:
        """Everything the broker does to its server socket, minus close.

        ``socket.close`` is read-only on the C type, so the attribute has
        to be intercepted by delegation rather than patched in place.
        Only ``stop()`` ever sees this — ``_serve`` captured the real
        socket when its thread started, which is the point: the loop is
        still blocked in ``accept()`` on a socket nothing can close out
        from under it.
        """

        def __init__(self, sock):
            self._sock = sock

        def __getattr__(self, name):
            return getattr(self._sock, name)

        def close(self):
            pass

    project = tmp_path / "project"
    project.mkdir(parents=True)
    broker = FileLockBroker(project, locks_dir_for_plan(plan_dir))
    sock = broker.start()
    # Captured up front: ``stop()`` clears ``_thread`` on its way out, so
    # the assertion below has to hold a reference of its own.
    server_thread = broker._thread
    assert server_thread is not None and server_thread.is_alive()
    real_server = broker._server

    monkeypatch.setattr(broker, "_server", _CloseInertSocket(real_server))
    started = time.monotonic()
    broker.stop()
    elapsed = time.monotonic() - started

    # Whatever the platform, the socket and its file must not leak into
    # the next broker this process starts.
    real_server.close()
    Path(sock).unlink(missing_ok=True)

    assert not server_thread.is_alive(), (
        "the serving thread is still blocked in accept() after stop(); it "
        "must be ended by the stop flag, not by the socket teardown"
    )
    assert elapsed < 5.0, (
        f"stop() took {elapsed:.1f}s — it burned the whole join timeout "
        f"waiting for a thread that was never going to wake"
    )


def test_an_unopenable_lock_file_is_unavailable_not_busy(tmp_path, plan_dir):
    """An I/O failure must not be reported as contention.

    "Someone else holds it" makes the caller wait or block the edit;
    "the mechanism is broken" makes it proceed with a warning. Reporting
    the second as the first blocks an edit nobody is competing for.

    Induced by pointing the lock directory at a path that cannot exist,
    rather than by making the workspace read-only: the lock directory is
    no longer inside the workspace, so a broken project dir says nothing
    about whether locks can be taken.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    broker = FileLockBroker(project, blocked / "locks")
    broker.start()
    try:
        assert broker.acquire("t1", "a.py", 2) == UNAVAILABLE
    finally:
        broker.stop()


def test_default_timeout_is_the_documented_value():
    """Pinned because the hook's own wait budget is derived from it."""
    assert DEFAULT_ACQUIRE_TIMEOUT == 300.0
