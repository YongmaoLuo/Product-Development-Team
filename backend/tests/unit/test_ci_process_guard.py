"""The CI process guard must see leaks, name them, and reap them.

These are the tests that make `ci_process_guard` trustworthy rather than
merely present: a guard that silently reports nothing looks identical to
a clean run, which is the failure mode it exists to remove.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import ci_process_guard as guard  # noqa: E402


@pytest.fixture
def sleeper():
    """A child in *our* process group, as a leaked subprocess would be."""
    procs = []

    def _spawn(new_session=False, seconds=60):
        proc = subprocess.Popen(
            ["sleep", str(seconds)],
            start_new_session=new_session,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    yield _spawn

    for proc in procs:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def _wait_for_group(pgid, pid, present=True, timeout=5.0):
    """`_pids_in_group` shells out on the fallback path, so the child may
    take a moment to appear in a sampling of the process table."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = pid in guard._pids_in_group(pgid)
        if found is present:
            return True
        time.sleep(0.05)
    return False


class TestItSeesWhatItShould:
    def test_a_child_in_our_group_is_visible(self, sleeper):
        proc = sleeper()
        assert _wait_for_group(os.getpgrp(), proc.pid), (
            "a subprocess that inherits our process group must be visible; "
            "this is the population the guard reasons about"
        )

    def test_a_child_in_its_own_group_is_invisible(self, sleeper):
        """The safety property, not a nicety.

        The guard *kills* what it sees. Anything it can see outside its
        own group is something it could kill outside its own group — an
        operator's shell, another job, anything. Scoping is what makes
        the reaping defensible.
        """
        proc = sleeper(new_session=True)
        time.sleep(0.3)
        assert proc.pid not in guard._pids_in_group(os.getpgrp()), (
            "a process in a different group must never be visible to the "
            "guard, or the guard could kill outside its own group"
        )


class TestItReaps:
    def test_reap_kills_a_child_in_our_group(self, sleeper):
        proc = sleeper()
        assert _wait_for_group(os.getpgrp(), proc.pid)

        stubborn = guard._reap([proc.pid])

        assert not stubborn, f"reap reported these as unkillable: {stubborn}"
        proc.wait(timeout=5)
        assert proc.poll() is not None, (
            "the survivor must actually be dead, otherwise the next step "
            "in the job inherits the leak"
        )

    def test_reap_ignores_a_pid_that_already_exited(self, sleeper):
        """Racing an ordinary exit must not raise — the guard runs at
        session end, exactly when children are dying."""
        proc = sleeper()
        proc.kill()
        proc.wait(timeout=5)

        assert guard._reap([proc.pid]) == []


class TestTheFallbackProbe:
    """Linux uses a `/proc` walk, which forks nothing. Everywhere else
    the guard shells out to `ps` — and `ps -A` lists *itself*, a child in
    our own group, so without filtering the guard reports its own probe
    as a leak on every single test."""

    @pytest.fixture
    def forced_ps_path(self, monkeypatch):
        real_listdir = os.listdir

        def _no_proc(path="."):
            if str(path) == "/proc":
                raise FileNotFoundError(2, "No such file or directory")
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", _no_proc)

    def test_the_probe_does_not_report_itself(self, forced_ps_path):
        found = guard._pids_in_group(os.getpgrp())
        assert not any(
            "pgid=,command=" in cmd for cmd in found.values()
        ), f"the guard reported its own `ps` probe as a leak: {found}"

    def test_the_fallback_still_sees_real_children(self, forced_ps_path, sleeper):
        proc = sleeper()
        assert _wait_for_group(os.getpgrp(), proc.pid), (
            "the `ps` fallback must still work; it is the only path on "
            "macOS, where developers run this"
        )

    def test_it_returns_empty_instead_of_raising_when_ps_is_unusable(
        self, monkeypatch
    ):
        """A diagnostic that breaks the run it is diagnosing is worse
        than no diagnostic."""
        monkeypatch.setattr(
            os,
            "listdir",
            lambda path=".": (_ for _ in ()).throw(FileNotFoundError(2, "no /proc")),
        )

        def _boom(*args, **kwargs):
            raise OSError("ps not available")

        monkeypatch.setattr(subprocess, "run", _boom)

        assert guard._pids_in_group(os.getpgrp()) == {}


@pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1"), reason="SIGUSR1 is POSIX-only"
)
class TestTheStackDump:
    """The dump is the only artefact that survives an uninterruptible hang.

    ``pytest-timeout``'s thread method raises into the main thread through
    ``PyThreadState_SetAsyncExc``, which is delivered at a bytecode
    boundary — so a test parked in ``waitpid`` never sees it, the run
    never ends, and the log stops at the last test that STARTED. That is
    the shape every 45-minute reap of the ``unit`` shard has had.

    These tests pin the two things that make the dump usable, both of
    which were wrong in the first implementation and caught by running it
    against a deliberately wedged process (see the module docstring):
    the sink must be a real file rather than stderr, and the signal must
    go to the leader rather than the group.
    """

    @pytest.fixture
    def armed_dump(self, tmp_path, monkeypatch):
        """Arm the dump against a scratch file and disarm on teardown.

        A registered signal handler is process-global, so the fixture
        hands the process back the way it found it.
        """
        path = tmp_path / "stack-dump.txt"
        monkeypatch.setenv("CI_STACK_DUMP_PATH", str(path))
        assert guard._arm_stack_dump(), "the handler must arm on POSIX"
        yield path
        guard._disarm_stack_dump()

    def test_sigusr1_names_the_blocked_frame(self, armed_dump):
        """The dump must name *this* test — that is its whole job."""
        os.kill(os.getpid(), signal.SIGUSR1)
        text = armed_dump.read_text(encoding="utf-8")
        assert "test_sigusr1_names_the_blocked_frame" in text, (
            "the SIGUSR1 stack dump does not name the running test; a "
            "wedge would still be unattributable. Got:\n" + text[:2000]
        )
        assert "Current thread" in text, (
            "the dump must include the current thread's stack, not only "
            "the background ones:\n" + text[:2000]
        )

    def test_it_writes_to_the_configured_file_not_stderr(
        self, armed_dump, capfd
    ):
        """Why a file and not stderr — the first implementation's bug.

        pytest's default ``fd``-level capture replaces fd 2 for the
        duration of a test, so a dump written to stderr lands in a buffer
        that is discarded when the process is killed. That is precisely
        the case the dump exists for, so the sink has to be a file the
        guard owns.
        """
        os.kill(os.getpid(), signal.SIGUSR1)
        captured = capfd.readouterr()
        assert armed_dump.read_text(encoding="utf-8").strip(), (
            "nothing was written to the configured dump file"
        )
        assert "Current thread" not in (captured.out + captured.err), (
            "the dump went to stderr, where pytest's per-test capture "
            "will discard it the moment the shard is killed"
        )

    def test_disarm_leaves_no_handler_and_no_open_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CI_STACK_DUMP_PATH", str(tmp_path / "d.txt"))
        assert guard._arm_stack_dump()
        guard._disarm_stack_dump()
        assert guard._STACK_DUMP_FILE is None, (
            "disarming must release the file object, not just the handler"
        )
        assert guard._STATE["stack_dump_path"] == ""
        # Arming after disarming must work again — this is what makes the
        # call idempotent rather than one-shot.
        assert guard._arm_stack_dump(), "re-arming after a disarm failed"
        guard._disarm_stack_dump()

    def test_an_unopenable_path_is_not_armed_and_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        """A bolt-on diagnostic must never be why a session fails."""
        monkeypatch.setenv(
            "CI_STACK_DUMP_PATH", str(tmp_path / "no-such-dir" / "d.txt")
        )
        assert guard._arm_stack_dump() is False
        assert guard._STATE["stack_dump_path"] == ""
