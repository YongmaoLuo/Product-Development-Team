"""The repair round writes down the scene of its own bad end.

Background
----------
The backend launches the repair executor and then waits for it.  When
the round ends badly — a non-zero exit, a SIGKILL from the OOM killer,
a container that ran out of memory — the backend is the only process
still alive to say what happened.

The main execution path already writes that scene down: the parent
writes the fallback report
(``routes.execution._finalize_executor_forensics``) and records it in
the plan's flags.  The repair path did not, so a repair round that
died left nothing but a word — "repair_non_zero_exit" — and no
evidence to go with it.  A repair round that was killed and a repair
round that failed its last task looked identical from the outside, and
neither of them left a report.

What is pinned
--------------
* a repair round killed by a signal writes the fallback report:
  ``signal=9``, ``killed_by='signal'``, ``source='parent'``;
* a round that exited 0 writes nothing at all — a clean exit is not a
  crash;
* a round that ended without a return code (``repair_aborted``) writes
  nothing — there is no end to describe;
* one repair round writes one scene: the writer is one-shot, so the
  second call is a no-op;
* the stop reason says HOW the round ended: an exit code and a signal
  are two different facts, and the reason now carries which one it
  was.

TDD spec -- 5 contract tests
-----------------------------
1. ``test_repair_writer_writes_fallback_report`` -- rc=-9 -> a file
   with ``signal=9`` / ``killed_by='signal'`` / ``source='parent'``.
2. ``test_repair_writer_silent_on_clean_exit`` -- rc=0 -> no file.
3. ``test_repair_writer_skips_none_exit_code`` -- exit_code=None ->
   no file.
4. ``test_repair_writer_is_one_shot`` -- two calls -> one file.
5. ``test_repair_stop_reason_refined`` -- 7 ->
   ``repair_non_zero_exit:7``; -9 -> ``repair_signal:9``.
6. ``test_sync_repair_path_writes_the_same_scene`` -- the synchronous
   repair path leaves the same scene behind.

The boundary that matters most: the writer is shared by the
synchronous and the asynchronous repair paths, so a scene written here
is the scene both paths leave behind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` has to be imported before ``routes.execution``: the server
# module wires the router at import time and ``routes.execution``
# late-binds back into it, so the other order is a circular import.
import server  # noqa: E402,F401
import verification_loop  # noqa: E402

PLAN_ID = "repair-exit-forensics-plan"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProcess:
    """The handle the backend holds on the repair executor.

    Only three things are read off it: the pid, the return code and
    the command line.  That is what the parent can know about a
    process it did not get to watch closely.
    """

    def __init__(
        self,
        returncode: Optional[int],
        *,
        pid: int = 4242,
        args: Optional[List[str]] = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.args = list(args) if args is not None else [
            "/venv/bin/python3",
            "backend/cli.py",
            "--recover",
        ]

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self) -> Optional[int]:
        return self.returncode


def _crash_files(plan_dir: Path) -> List[Path]:
    """The crash reports written under ``plan_dir``, oldest name first."""
    logs = plan_dir / "logs"
    if not logs.is_dir():
        return []
    return sorted(logs.glob("executor_crash_*.json"), key=lambda item: item.name)


def _read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A plan directory for the repair round to be written under."""
    directory = tmp_path / "plans" / PLAN_ID
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# 1. the fallback report
# ---------------------------------------------------------------------------


def test_repair_writer_writes_fallback_report(plan_dir: Path) -> None:
    """A repair round killed by a signal leaves the scene behind.

    The executor had no chance to write anything — it was SIGKILLed
    between one instruction and the next — so the parent writes the
    report from the facts it holds.  The report says it came from the
    parent (``source='parent'``), so a reader can tell it from the
    executor's own report, and it names the signal rather than a bare
    number.
    """
    write_exit = verification_loop._repair_exit_writer(PLAN_ID, plan_dir)

    write_exit(_FakeProcess(-9), -9, "repair_finished")

    files = _crash_files(plan_dir)
    assert len(files) == 1, "the parent wrote no report (or more than one)"
    report = _read_report(files[0])
    assert report["signal"] == 9
    assert report["signal_name"] == "SIGKILL"
    assert report["killed_by"] == "signal"
    assert report["exit_code"] is None
    assert report["source"] == "parent"
    assert report["pid"] == 4242
    assert report["plan_id"] == PLAN_ID


# ---------------------------------------------------------------------------
# 2. a clean exit is not a crash
# ---------------------------------------------------------------------------


def test_repair_writer_silent_on_clean_exit(plan_dir: Path) -> None:
    """rc=0 writes no report: a round that ended well has no scene.

    A report here would be a lie — it would say the run died, and the
    operator would go looking for a crash that never happened.
    """
    write_exit = verification_loop._repair_exit_writer(PLAN_ID, plan_dir)

    write_exit(_FakeProcess(0), 0, "repair_finished")

    assert _crash_files(plan_dir) == [], (
        "a clean exit wrote a crash report — the run did not crash"
    )


# ---------------------------------------------------------------------------
# 3. no return code, no end to describe
# ---------------------------------------------------------------------------


def test_repair_writer_skips_none_exit_code(plan_dir: Path) -> None:
    """A round that ended without a return code writes nothing.

    ``repair_aborted`` is the exit of a round that blew up before or
    during the wait: there is no return code to describe, so there is
    no scene to write.  A report built from a missing fact is a
    report full of guesses.
    """
    # The strict case first: a "finished" round that carries no return
    # code.  Only the missing code can exclude it — the detail is the
    # one a real round would carry.
    write_exit = verification_loop._repair_exit_writer(PLAN_ID, plan_dir)
    write_exit(_FakeProcess(None), None, "repair_finished")
    assert _crash_files(plan_dir) == [], (
        "a round with no return code wrote a report — there was no end "
        "to describe"
    )

    # And the shape the round actually takes when it blows up mid-wait.
    aborted = verification_loop._repair_exit_writer(PLAN_ID, plan_dir)
    aborted(_FakeProcess(None), None, "repair_aborted")
    assert _crash_files(plan_dir) == [], (
        "an aborted round wrote a report — there was no end to describe"
    )


# ---------------------------------------------------------------------------
# 4. one round, one scene
# ---------------------------------------------------------------------------


def test_repair_writer_is_one_shot(plan_dir: Path) -> None:
    """The writer writes the scene once, however often it is called.

    The repair paths call the writer twice by design: once with the
    real exit code, and once from the ``finally`` with the aborted
    exit.  The second call has to be a no-op — a round that leaves
    two reports behind leaves the reader with two answers.

    The two calls carry **different pids** on purpose: a second call
    with the same pid would find the first report and reuse it, and
    the file count would stay at one even if the one-shot rule were
    broken.  A different pid cannot be mistaken for the first run, so
    the count really measures the rule.
    """
    write_exit = verification_loop._repair_exit_writer(PLAN_ID, plan_dir)

    write_exit(_FakeProcess(-9, pid=1111), -9, "repair_finished")
    write_exit(_FakeProcess(-9, pid=2222), -9, "repair_finished")

    files = _crash_files(plan_dir)
    assert len(files) == 1, (
        "the writer wrote %d scenes for one repair round — a round that "
        "leaves two reports behind leaves the reader with two answers"
        % len(files)
    )
    assert _read_report(files[0])["pid"] == 1111, (
        "the scene that survived is not the one the first call wrote"
    )


# ---------------------------------------------------------------------------
# 5. the stop reason says how the round ended
# ---------------------------------------------------------------------------


def test_repair_stop_reason_refined() -> None:
    """An exit code and a signal are two different facts.

    ``repair_non_zero_exit`` said one thing about two different
    events: a round that exited 3 and a round that was SIGKILLed (the
    OOM killer, a stray ``kill -9``, a container that ran out of
    memory) both wrote the same word.  The killing is exactly the
    case where the operator wants to know what happened, so the
    reason now carries the fact.

    The verdict does not change — the status is still ``failed``; only
    the description of it is refined.
    """
    assert verification_loop._repair_stop_reason(7) == "repair_non_zero_exit:7"
    assert verification_loop._repair_stop_reason(-9) == "repair_signal:9"


# ---------------------------------------------------------------------------
# 6. the synchronous path writes the same scene
# ---------------------------------------------------------------------------


def test_sync_repair_path_writes_the_same_scene(
    monkeypatch, tmp_path, plan_dir,
):
    """The synchronous repair path leaves the scene behind too.

    Two functions launch the repair executor — the synchronous
    :func:`_run_repair_execution` and the asynchronous
    :func:`_run_repair_execution_async` — and both close the pid
    through the same writer.  A scene written for one path and not the
    other would make the round's outcome depend on which path ran, so
    the synchronous path is pinned here: it is the one no other test
    drives end to end.

    The round is killed by a signal, so the scene says a signal, and
    the round reports failure — a round that was killed did not
    succeed.
    """
    process = _FakeProcess(-9)
    monkeypatch.setattr(server, "_plan_dir", lambda plan_id: plan_dir)
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (process, tmp_path / "repair.log"),
    )

    assert verification_loop._run_repair_execution(
        PLAN_ID, tmp_path
    ) is False, "a round killed by a signal reported success"

    files = _crash_files(plan_dir)
    assert len(files) == 1, (
        "the synchronous repair path wrote %d scenes — the round's "
        "evidence must not depend on which path ran" % len(files)
    )
    report = _read_report(files[0])
    assert report["signal"] == 9
    assert report["killed_by"] == "signal"
    assert report["source"] == "parent"
