"""The executor's log is bounded, and the bound holds on the real spawn.

Background
----------
``logs/repair_*.log`` and ``logs/executor_*.log`` were bare files: the
executor subprocess's stdout was redirected into one of them and
nothing ever bounded it, so one long run grew a single log past
gigabytes.  :mod:`bounded_log` gave the plan directory a bounded,
rotating writer; this file pins that the *executor spawn* goes through
it, because a bounded writer nobody uses is a bounded writer that does
not bound the one file that grew.

The spawn is the interesting place, because it is the only place the
executor's output enters the plan directory at all.  Redirecting it
into a file (``stdout=log_file``) is what made the 64KB pipe deadlock
impossible, and it is also what made the log unbounded: the child
wrote into a file nobody could rotate.  The way out is a pipe again —
but a pipe that is *drained*, by a pump, into a writer that bounds
what it writes.

What the tests below pin
------------------------
* the executor's output lands in bounded files: the live log and every
  rotated copy stay within the bound, and a run that writes more than
  the bound rotates;
* the log is complete when ``wait()`` returns — the pump has been
  joined, so the tail a reader takes is the tail the child wrote;
* a small run writes one file: nothing is rotated that does not need
  to be, and a run that writes nothing still leaves an (empty) live
  log behind;
* a spawn that cannot start closes the sink and raises;
* ``wait(timeout)`` raises ``TimeoutExpired`` and does *not* wait for
  the pump: the child is still writing, so its output is still coming;
* a grandchild that keeps the pipe open does not make ``wait()`` hang.

The executor is a real subprocess in every case here.  A fake would
prove that the *test* wrote the file, not that the spawn did — and the
spawn is the thing under test.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

import bounded_log

# ``server`` is imported BEFORE ``routes.execution`` on purpose: the route
# module late-binds into the application module, and ``server`` seeds
# ``sys.modules['server']`` before importing it.
import server
from routes import execution as routes_execution


#: The plan id every spawn in this file uses.  It only has to be a legal
#: plan id — the log path is given explicitly, so the plan directory is
#: never read.
PLAN_ID = "p-bounded-log"

#: The bound the rotation test shrinks the writer to.  Small enough that
#: 200KB of output cannot fit in one file, small enough that a mistake
#: in the bound shows up as a size, not as a slow test.
TEST_MAX_BYTES = 4096


#: A writer that produces 2000 lines of 100 bytes: 200KB, which is more
#: than the 64KB a pipe holds, so the child blocks unless somebody
#: drains it.
WRITER_200KB = """
import sys

line = "y" * 99 + "\\n"
for _ in range(2000):
    sys.stdout.write(line)
sys.stdout.flush()
"""

#: A writer whose last line is a marker, so a reader can tell where the
#: output ends.
WRITER_MARKER = """
import sys

sys.stdout.write("first line\\n")
sys.stdout.write("second line\\n")
sys.stdout.write("the tail MARKER\\n")
sys.stdout.flush()
"""

#: A writer that says nothing at all.
WRITER_SILENT = """
pass
"""

#: A writer that leaves a grandchild behind holding the pipe open.  The
#: pipe is inherited, so the pump never sees EOF while the grandchild
#: lives — the case ``wait()`` must not wait for.
WRITER_WITH_GRANDCHILD = """
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
sys.stdout.write("child done\\n")
sys.stdout.flush()
"""

#: A writer that never finishes, so ``wait(timeout=...)`` has something
#: to time out on.
WRITER_HANGS = """
import time

time.sleep(30)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _writer_script(tmp_path: Path, body: str) -> Path:
    """Write ``body`` to a python script and return its path."""
    script = tmp_path / "writer.py"
    script.write_text(body, encoding="utf-8")
    return script


def _command_for(script: Path) -> str:
    """The command line that runs ``script`` — quoted for :func:`shlex.split`."""
    return " ".join(shlex.quote(part) for part in (sys.executable, str(script)))


def _spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    script: Path,
    log_path: Path,
):
    """Spawn the executor through the production helper.

    The executor is replaced by ``script`` through the documented
    ``PDT_EXECUTOR_CMD`` port, so the *spawn* is the production one —
    only the body of the child changes.
    """
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "worktree"
    project_dir.mkdir(exist_ok=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    monkeypatch.setenv("PDT_EXECUTOR_CMD", _command_for(script))

    return routes_execution._spawn_executor_subprocess(
        plan_id=PLAN_ID,
        backend_dir=backend_dir,
        project_dir=project_dir,
        tasks_file=tasks_file,
        tool=None,
        extra_args=None,
        log_path=log_path,
    )


def _log_files(log_path: Path):
    """The live log and every rotated copy of it."""
    return sorted(log_path.parent.glob(f"{log_path.name}*"))


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


def test_spawned_output_rotates_under_cap(monkeypatch, tmp_path):
    """200KB of executor output lands in files that are all within the cap.

    The output is more than a pipe holds, so the child cannot finish
    unless somebody drains the pipe — and the drain is what writes the
    log, so this is also the test that the spawn does not deadlock.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", TEST_MAX_BYTES)

    log_path = tmp_path / "logs" / "repair_x.log"
    script = _writer_script(tmp_path, WRITER_200KB)

    started = time.monotonic()
    process, returned_log = _spawn(
        tmp_path, monkeypatch, script=script, log_path=log_path
    )
    assert returned_log == log_path, (
        "the spawn must hand back the log path it was given"
    )
    assert process.wait() == 0, "the writer must finish cleanly"
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"the spawn took {elapsed:.1f}s — the pipe was not drained"

    files = _log_files(log_path)
    assert log_path in files, f"the live log is missing: {files!r}"
    assert any(path.name.endswith(".1") for path in files), (
        f"200KB of output did not rotate under a {TEST_MAX_BYTES}B cap: {files!r}"
    )
    for path in files:
        size = path.stat().st_size
        assert size <= TEST_MAX_BYTES, f"{path.name} is {size} bytes"


def test_single_oversized_chunk_is_capped(monkeypatch, tmp_path):
    """One line bigger than the cap is truncated to it, not written whole.

    The bound is what keeps the disk from filling up; a single write
    that overshoots would defeat it on its own.
    """
    monkeypatch.setattr(bounded_log, "MAX_PLAN_LOG_BYTES", TEST_MAX_BYTES)

    log_path = tmp_path / "logs" / "big.log"
    script = _writer_script(
        tmp_path,
        'import sys\nsys.stdout.write("z" * 100000 + "\\n")\nsys.stdout.flush()\n',
    )

    process, _ = _spawn(tmp_path, monkeypatch, script=script, log_path=log_path)
    assert process.wait() == 0

    for path in _log_files(log_path):
        size = path.stat().st_size
        assert size <= TEST_MAX_BYTES, f"{path.name} is {size} bytes"


# ---------------------------------------------------------------------------
# The log a reader gets
# ---------------------------------------------------------------------------


def test_spawned_log_tail_is_readable(monkeypatch, tmp_path):
    """After ``wait()`` returns the live log ends with what the child wrote.

    ``wait()`` joins the pump before it returns, so the tail a reader
    takes is the tail the child wrote — not a log that is still being
    filled in the background.
    """
    log_path = tmp_path / "logs" / "executor.log"
    script = _writer_script(tmp_path, WRITER_MARKER)

    process, _ = _spawn(tmp_path, monkeypatch, script=script, log_path=log_path)
    assert process.wait() == 0

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines, "the log is empty — the output never reached it"
    assert lines[-1].endswith("MARKER"), (
        f"the live log does not end with what the child wrote: {lines[-1]!r}"
    )


def test_small_output_writes_single_file(monkeypatch, tmp_path):
    """A run that fits in one file does not rotate.

    The rotation is a visible side effect: a reader that finds a
    ``.1`` next to a log that never needed one has to work out whether
    it is looking at an old run.
    """
    log_path = tmp_path / "logs" / "executor.log"
    script = _writer_script(tmp_path, WRITER_MARKER)

    process, _ = _spawn(tmp_path, monkeypatch, script=script, log_path=log_path)
    assert process.wait() == 0

    assert log_path.exists(), "the live log is missing"
    assert not (log_path.parent / f"{log_path.name}.1").exists(), (
        "a run that fits in one file must not rotate"
    )


def test_no_output_leaves_an_empty_live_log(monkeypatch, tmp_path):
    """A run that writes nothing still leaves the log file behind.

    The log is created before the child starts, so a reader that asks
    for the log of a run that has not written anything yet finds an
    empty file rather than a missing path.
    """
    log_path = tmp_path / "logs" / "executor.log"
    script = _writer_script(tmp_path, WRITER_SILENT)

    process, _ = _spawn(tmp_path, monkeypatch, script=script, log_path=log_path)
    assert process.wait() == 0

    assert log_path.exists(), "the live log is missing"
    assert log_path.stat().st_size == 0, "the live log is not empty"
    assert not (log_path.parent / f"{log_path.name}.1").exists(), (
        "an empty run must not rotate"
    )


# ---------------------------------------------------------------------------
# The failure paths
# ---------------------------------------------------------------------------


def test_spawn_failure_closes_the_sink_and_raises(monkeypatch, tmp_path):
    """A spawn that cannot start closes the sink and re-raises.

    The sink is opened before the child is, so a spawn that fails
    leaves it open unless the failure path closes it.  The error
    itself is not swallowed: the caller records the run as failed.
    """
    closed: list[bool] = []

    class _RecordingSink(routes_execution.RotatingLogSink):
        def close(self) -> None:
            closed.append(True)
            super().close()

    monkeypatch.setattr(routes_execution, "RotatingLogSink", _RecordingSink)
    monkeypatch.setenv(
        "PDT_EXECUTOR_CMD", str(tmp_path / "no-such-interpreter")
    )

    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "worktree"
    project_dir.mkdir(exist_ok=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    with pytest.raises(OSError):
        routes_execution._spawn_executor_subprocess(
            plan_id=PLAN_ID,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=None,
            extra_args=None,
            log_path=tmp_path / "logs" / "executor.log",
        )

    assert closed, "the sink was left open when the spawn failed"


def test_wait_timeout_raises_without_joining_the_pump(monkeypatch, tmp_path):
    """``wait(timeout)`` raises ``TimeoutExpired`` and leaves the pump alone.

    The child is still running when the timeout expires, so its output
    is still coming: joining the pump here would wait for a pipe that
    is still being written to.
    """
    log_path = tmp_path / "logs" / "executor.log"
    script = _writer_script(tmp_path, WRITER_HANGS)

    process, _ = _spawn(tmp_path, monkeypatch, script=script, log_path=log_path)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        assert process._pump_thread.is_alive(), (
            "the pump must still be running: the child is still writing"
        )
    finally:
        process.kill()
        process.wait()


def test_grandchild_holding_the_pipe_does_not_block_wait(monkeypatch, tmp_path):
    """A grandchild that keeps the pipe open does not make ``wait()`` hang.

    The pipe is inherited, so the pump never sees EOF while the
    grandchild lives.  ``wait()`` waits for the child, then for the
    pump — but the wait for the pump is bounded, because a descendant
    that outlives the run must not hold the run's own bookkeeping open.
    """
    monkeypatch.setattr(routes_execution, "PUMP_JOIN_TIMEOUT", 0.5)

    log_path = tmp_path / "logs" / "executor.log"
    script = _writer_script(tmp_path, WRITER_WITH_GRANDCHILD)

    process, _ = _spawn(tmp_path, monkeypatch, script=script, log_path=log_path)

    started = time.monotonic()
    assert process.wait() == 0
    elapsed = time.monotonic() - started

    assert elapsed < 3, (
        f"wait() took {elapsed:.1f}s — it waited for a pipe nobody was going to close"
    )
    assert log_path.read_text(encoding="utf-8").strip() == "child done"
