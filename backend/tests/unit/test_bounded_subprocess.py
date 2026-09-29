"""``run_bounded`` must terminate what it times out, not merely stop waiting.

2026-09-23, from the CI wedge investigation
-------------------------------------------
``subprocess.run(cmd, shell=True, timeout=N)`` bounds **how long the
caller waits**, not **how long the command runs**.  On timeout CPython
kills only the direct child; under ``shell=True`` that child is
``/bin/sh``, so the process doing the actual work is a grandchild that
is never signalled and keeps running.  Measured with a bare
``subprocess.run``::

    subprocess.run("sleep 987 & sleep 987", shell=True,
                   capture_output=True, timeout=3)
    -> TimeoutExpired raised at t=3.0s
    -> surviving grandchildren: 2   (PPID=1, still alive afterwards)

That matters here rather than being a curiosity.  ``check_falsifiable``
executes a task's own ``test_command``, and the backend's own fixtures carry
``test_command="pytest tests/ -v"``.  Run with a ``cwd`` inside the backend's checkout
checkout that is a nested pytest over the whole suite, which re-enters
the codepath that spawned it: each timed-out level left its child alive,
so the abandoned tree grew instead of unwinding.  On a 4-vCPU CI runner
the result was a box too starved to schedule the runner agent — the
job's own ``timeout-minutes`` never fired, and no log was ever uploaded.

These tests pin the replacement contract: a timeout leaves **nothing**
running, and the ``subprocess.run`` error shape callers already handle
is preserved.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from bounded_subprocess import (  # noqa: E402
    run_bounded,
    terminate_process_tree,
)


def _marker() -> str:
    return f"ac-bounded-probe-{uuid.uuid4().hex}"


def _survivors(marker: str) -> list:
    """PIDs still alive whose argv carries ``marker``."""
    proc = subprocess.run(
        ["pgrep", "-f", marker],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.split() if line.strip()]


@pytest.fixture
def reap(request):
    """Never leave a probe process behind, even when the test fails."""
    markers: list = []
    request.node._ac_probe_markers = markers
    yield markers
    for marker in markers:
        for pid in _survivors(marker):
            try:
                os.kill(int(pid), 9)
            except (ProcessLookupError, ValueError, OSError):
                pass


def _tree_command(marker: str) -> str:
    """A shell that starts a grandchild and then waits for it.

    The shell is the direct child (so the naive kill reaches it) and the
    interpreter sleeping 120s is the grandchild (which the naive kill
    does not reach).  ``wait`` keeps the shell alive so it is still the
    timeout that ends the call, not an early exit.
    """
    # ``shlex.quote`` is load-bearing, not decoration: ``sys.executable``
    # is an absolute path, and a checkout whose path contains a space
    # (``~/Documents/GitHub/My Projects/...`` — or macOS' own
    # ``~/Library/Application Support/``, where tools routinely live)
    # would otherwise split here: the shell would try to run
    # ``…/GitHub/My`` and the grandchild would never start, so this test
    # would report "the premise changed" when in fact nothing had.
    return (
        f'{shlex.quote(sys.executable)} -c "import time; time.sleep(120)"'
        f" {marker} & wait"
    )


# ---------------------------------------------------------------------------
# The regression this module exists for
# ---------------------------------------------------------------------------


class TestTimeoutKillsTheProcessTree:
    def test_a_timed_out_grandchild_does_not_survive(self, reap):
        marker = _marker()
        reap.append(marker)

        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded(
                _tree_command(marker),
                cwd=str(BACKEND_DIR),
                timeout=3,
            )

        survivors = _survivors(marker)
        assert survivors == [], (
            "the grandchild outlived the timeout — this is the exact leak "
            "that starved the CI runner: the caller believes the command "
            f"was killed while it keeps running (pids={survivors})"
        )

    def test_the_leak_is_real_for_a_bare_subprocess_run(self, reap):
        """Pin *why* ``run_bounded`` exists, so nobody "simplifies" it back.

        This asserts the opposite outcome on ``subprocess.run``: the
        grandchild **does** survive.  If CPython ever changes that, this
        test fails and tells us the wrapper may no longer be needed —
        which is a far better failure than silently keeping a redundant
        layer no one dares delete.
        """
        marker = _marker()
        reap.append(marker)

        try:
            subprocess.run(
                _tree_command(marker),
                shell=True,
                capture_output=True,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            pass

        survivors = _survivors(marker)
        assert survivors, (
            "subprocess.run no longer leaks the grandchild — the premise "
            "of bounded_subprocess has changed; re-evaluate the wrapper"
        )


# ---------------------------------------------------------------------------
# The error contract callers already handle
# ---------------------------------------------------------------------------


class TestRunBoundedKeepsTheRunContract:
    def test_a_normal_command_returns_a_completed_process(self):
        completed = run_bounded("echo hello", cwd=str(BACKEND_DIR), timeout=30)
        assert completed.returncode == 0
        assert "hello" in completed.stdout

    def test_a_failing_command_reports_its_exit_code(self):
        completed = run_bounded("exit 3", cwd=str(BACKEND_DIR), timeout=30)
        assert completed.returncode == 3

    def test_stderr_is_captured(self):
        completed = run_bounded(
            "echo boom >&2; exit 1", cwd=str(BACKEND_DIR), timeout=30,
        )
        assert completed.returncode == 1
        assert "boom" in completed.stderr

    def test_an_empty_command_is_not_an_error(self):
        completed = run_bounded("", cwd=str(BACKEND_DIR), timeout=30)
        assert completed.returncode == 0

    def test_timeout_is_raised_with_the_partial_output_attached(self):
        """``verification_ci_runner`` and ``verification_evidence`` read
        ``exc.stdout`` / ``exc.stderr`` to report what a timed-out gate
        managed to print before it was killed."""
        try:
            run_bounded(
                'echo before-timeout; sleep 120',
                cwd=str(BACKEND_DIR),
                timeout=2,
            )
        except subprocess.TimeoutExpired as exc:
            assert "before-timeout" in (exc.output or "")
            assert exc.timeout == 2
        else:  # pragma: no cover - would mean the timeout did not fire
            pytest.fail("a 120s sleep under a 2s timeout did not time out")

    def test_the_caller_is_not_blocked_past_the_timeout(self):
        """A bounded call costs one timeout's wall time — no more.

        The drain after the kill is itself bounded, so a descendant that
        escaped the group cannot hold the call open.
        """
        import time

        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded("sleep 120", cwd=str(BACKEND_DIR), timeout=2)
        elapsed = time.monotonic() - started

        assert elapsed < 30, (
            f"run_bounded took {elapsed:.1f}s to return after a 2s "
            f"timeout — the post-kill drain is not bounded"
        )


# ---------------------------------------------------------------------------
# Safety: never signal our own process group
# ---------------------------------------------------------------------------


class TestNeverSignalsOurOwnGroup:
    def test_a_child_in_our_group_is_killed_without_killing_us(self):
        """``terminate_process_tree`` must degrade to killing the direct
        child when the child shares our process group.  Signalling the
        group there would take down the backend process — and, under pytest,
        the whole test session."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            # Deliberately NOT start_new_session: this child is in our
            # group, which is the dangerous case.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            terminate_process_tree(proc)
            assert proc.poll() is not None, "the child should be dead"
        finally:
            if proc.poll() is None:  # pragma: no cover - defensive
                proc.kill()

        # Reaching this line at all is the assertion: we are still here.
        assert os.getpid() > 0

    def test_terminating_an_already_dead_process_is_a_no_op(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.wait(timeout=30)

        terminate_process_tree(proc)  # must not raise

        assert proc.poll() == 0
