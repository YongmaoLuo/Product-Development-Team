"""The executor takes the single-instance lock before it starts.

Why this suite exists
---------------------
Two executors on one plan contend for the same file locks, and the
loser does not lose *fast*: it waits out the full acquire timeout
(300 s) before it learns the plan is already being worked on. The
guard for that is an ``flock`` on ``<plan>/executor.lock``
(``backend/executor_lock.py``) — but a guard is only a guard if the
process that is supposed to be excluded actually asks for it. So the
CLI has to take the lock itself, at start-up, before it starts the
orchestrator.

Three things are pinned here, and each one has a failure it prevents.

**The refusal happens before the orchestrator starts.** A lock taken
*after* the orchestrator is running has already lost the race it was
meant to prevent: the second executor would have claimed its provider
connections and its task files first, and the refusal would arrive as
a crash halfway through a run instead of as a refusal at the door.

**The refusal says which plan, and who holds it.**
``already_running plan=p1 holder_pid=61791`` is an answer an operator
can act on. "already running" alone sends them looking for the
culprit; the pid sends them straight to it. The pid is read out of the
lock file, which the holder wrote when it took the lock.

**The lock is released when the executor is done, and the lock file
stays.** Releasing is what lets the next run start; leaving the file
is what keeps the next run's lock the *same* lock — deleting it would
leave the next acquirer locking a fresh inode that nobody else is
watching, which is two holders, both convinced they are alone.

The contention in the refusal tests is a real one: the lock is held by
a child *process*, because the guard is the kernel's ``flock`` and the
case that matters is two processes on one file. (The in-process
registry ``executor_lock`` keeps, for platforms whose ``flock`` rules
differ, is exercised in that module's own unit suite.)

Nothing here reaches a network, starts a model, or writes outside
``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cli
import executor_lock

pytestmark = pytest.mark.integration

#: ``backend/`` — resolved from this file, because the checkout lives
#: at a different path on every machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The child that holds the lock while the CLI is asked to start.
#:
#: It is a separate process on purpose: the guard is a kernel lock, so
#: the contention this suite needs is a real one — two processes, one
#: file — rather than the in-process bookkeeping the guard module
#: keeps for platforms whose ``flock`` rules differ.
_HOLDER_CODE = """
import os
import sys
import time

sys.path.insert(0, sys.argv[1])

from executor_lock import acquire

lock = acquire(sys.argv[2])
if lock is None:
    sys.exit(2)

with open(sys.argv[3], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))

time.sleep(float(sys.argv[4]))
"""


def _plan(tmp_path: Path, name: str = "p1") -> Path:
    """A plan directory with a tasks file in it."""
    plan_dir = tmp_path / name
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    # Resolved, because that is what the CLI resolves the tasks file's
    # parent to: two spellings of one plan directory must be one lock.
    return plan_dir.resolve()


def _spawn_holder(plan_dir: Path, marker: Path) -> subprocess.Popen:
    """Start a child process that holds the plan's lock."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER_CODE,
            str(BACKEND_DIR),
            str(plan_dir),
            str(marker),
            "120",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_holder(marker: Path, proc: subprocess.Popen) -> int:
    """Wait until the child holds the lock; return its pid."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if marker.exists():
            written = marker.read_text(encoding="utf-8").strip()
            if written:
                return int(written)
        if proc.poll() is not None:
            raise AssertionError(
                "the lock holder exited before it took the lock "
                "(exit code {})".format(proc.returncode)
            )
        time.sleep(0.05)
    raise AssertionError("the lock holder never took the lock")


def _reap(proc: subprocess.Popen) -> None:
    """Stop the holder and collect it."""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _must_not_start(**kwargs):
    """Stand-in for the orchestrator that refuses to be started."""
    raise AssertionError(
        "the orchestrator must not start while the plan's lock is held "
        "(called with {})".format(sorted(kwargs))
    )


def _executor_argv(plan_dir: Path, tmp_path: Path, extra=()) -> list:
    """The argv of an executor run against ``plan_dir``."""
    return [
        "cli",
        *extra,
        "-w",
        str(tmp_path),
        "--tasks-file",
        str(plan_dir / "tasks.json"),
    ]


@pytest.mark.parametrize(
    "extra_args",
    [["--recover"], []],
    ids=["recover", "plain"],
)
def test_executor_exits_already_running_when_lock_held(
    tmp_path, monkeypatch, capsys, extra_args
):
    """A second executor on a plan that already has one is refused.

    ``--recover`` and a plain run take the same lock: recovering from a
    crash is not a different kind of executor, it is the same one
    starting again, and it is exactly the run that is most likely to
    overlap with the crashed one it is recovering from.
    """
    plan_dir = _plan(tmp_path)
    marker = tmp_path / "holder.pid"
    holder = _spawn_holder(plan_dir, marker)
    try:
        holder_pid = _wait_for_holder(marker, holder)

        monkeypatch.setattr(cli, "autonomous_coding", _must_not_start)
        monkeypatch.setattr(
            sys, "argv", _executor_argv(plan_dir, tmp_path, extra_args)
        )

        with pytest.raises(SystemExit) as excinfo:
            cli.main()

        assert excinfo.value.code == 3

        err = capsys.readouterr().err
        assert "already_running" in err
        assert "plan=p1" in err
        assert "holder_pid={}".format(holder_pid) in err
    finally:
        _reap(holder)


def test_subcommand_path_does_not_take_lock(tmp_path, monkeypatch):
    """``secrets show`` is not an executor, so it takes no lock.

    Every command on the CLI answers on the same terminal, and most of
    them are asked *while* an executor is running — that is the whole
    point of a read-only ``secrets show``. A guard that every command
    paid would make those commands fail exactly when they are needed.
    """
    taken = []
    real_acquire = executor_lock.acquire

    def _spy(plan_dir):
        taken.append(plan_dir)
        return real_acquire(plan_dir)

    monkeypatch.setattr(cli, "acquire", _spy)
    monkeypatch.setattr(sys, "argv", ["cli", "secrets", "show"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    assert taken == []
    assert list(tmp_path.rglob(executor_lock.LOCK_FILENAME)) == []

    # The control: the spy is wired to the call site, so "no lock taken"
    # above is an observation and not a spy that was never called.
    plan_dir = _plan(tmp_path)
    monkeypatch.setattr(cli, "autonomous_coding", lambda **kwargs: None)
    monkeypatch.setattr(sys, "argv", _executor_argv(plan_dir, tmp_path))

    cli.main()

    assert taken == [plan_dir]


def test_lock_acquired_path_proceeds_to_execution(tmp_path, monkeypatch):
    """The guard is a gate, not a wall: an unheld plan starts.

    And when the run is over the lock is gone while the lock file
    stays — the file is the lock, and deleting it would let the next
    acquirer lock a fresh inode nobody else is watching.
    """
    plan_dir = _plan(tmp_path)
    started = []

    def _recorder(**kwargs):
        started.append(kwargs)

    monkeypatch.setattr(cli, "autonomous_coding", _recorder)
    monkeypatch.setattr(
        sys, "argv", _executor_argv(plan_dir, tmp_path, ["--recover"])
    )

    cli.main()

    assert len(started) == 1
    assert started[0]["project_dir"] == str(tmp_path)
    assert Path(started[0]["tasks_file"]) == plan_dir / "tasks.json"

    assert executor_lock.is_held(plan_dir) is False
    assert (plan_dir / executor_lock.LOCK_FILENAME).exists()


def test_exit_code_is_3_on_already_running(tmp_path, monkeypatch, capsys):
    """The refusal has an exit code of its own.

    Not 1: 1 is "the run started and failed", and this is "the run was
    refused before it started". Whatever watches the exit code treats
    those differently — the first is a failure to retry, the second is
    a duplicate to ignore.
    """
    plan_dir = _plan(tmp_path)
    marker = tmp_path / "holder.pid"
    holder = _spawn_holder(plan_dir, marker)
    try:
        _wait_for_holder(marker, holder)

        monkeypatch.setattr(cli, "autonomous_coding", _must_not_start)
        monkeypatch.setattr(
            sys, "argv", _executor_argv(plan_dir, tmp_path, ["--recover"])
        )

        with pytest.raises(SystemExit) as excinfo:
            cli.main()

        assert excinfo.value.code == cli.EXIT_ALREADY_RUNNING
        assert excinfo.value.code == 3
        assert "already_running" in capsys.readouterr().err
    finally:
        _reap(holder)


def test_lock_is_released_when_the_executor_raises(tmp_path, monkeypatch):
    """The lock is released on the way out, however the executor left.

    A lock held by a process that crashed on its way through would
    need a reaper to clear it. Releasing in a ``finally`` means the
    only way to leave a lock behind is to be killed — which is the
    one case the kernel already covers.
    """
    plan_dir = _plan(tmp_path)

    def _explode(**kwargs):
        raise RuntimeError("the executor blew up")

    monkeypatch.setattr(cli, "autonomous_coding", _explode)
    monkeypatch.setattr(
        sys, "argv", _executor_argv(plan_dir, tmp_path, ["--recover"])
    )

    with pytest.raises(RuntimeError):
        cli.main()

    assert executor_lock.is_held(plan_dir) is False
    assert (plan_dir / executor_lock.LOCK_FILENAME).exists()