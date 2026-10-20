"""The task→session mapping is recoverable from the execution log.

What this pins
--------------
A resume needs two things about an interrupted task: *which* task it
was, and *which* session it was running in. The first is what the
executor holds in memory; the second is written to the execution log
as a ``task_session_bound`` event, and that log is the only place the
id survives a restart of the backend.

``pause_state.collect_task_sessions`` is the reader for that log. These
tests pin the contract that matters to the resume path:

* **last write wins** -- the executor re-binds a task on every attempt,
  so the newest binding for a task is the one a resume must use;
* **missing log** -- a plan that never ran has no sessions, and that is
  an empty mapping rather than an error;
* **malformed lines** -- the log is written while the plan runs, so it
  can hold a torn line or a line that is not JSON at all; one bad line
  must not cost the good ones;
* **rotation** -- the log rotates at 10 MB, so the binding a resume
  needs may have been rotated into ``execution.log.1``; the live log
  still wins, because a rotation only moves *old* lines aside;
* **rotation depth** -- the reader looks at as many rotated copies as
  the logger keeps, read from the logger rather than copied into the
  reader, so the two cannot drift apart.

A binding is a line the executor wrote: ``event`` is
``task_session_bound`` and the session id is under ``data``. The reader
accepts that shape and no other, because the log holds an entry for
every event the executor emits -- a reader loose enough to accept
"something that looks like a binding" will eventually accept a line
that means something else.

Note the module is imported as a module rather than by name: the
attribute is looked up at call time, so a missing
``collect_task_sessions`` fails these tests as a plain failure rather
than as a collection error that hides whether the assertions hold.
"""

import json
from pathlib import Path

import execution_logger
import pause_state


def _write_log(path: Path, entries) -> None:
    """Write ``entries`` to ``path`` as JSON lines.

    A string entry is written verbatim, so a test can put on the log
    exactly what a killed writer would leave there: a line that is not
    JSON at all.
    """
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bound(task_id, session_id, event="task_session_bound"):
    """One ``task_session_bound`` entry, shaped as the executor writes it."""
    return {
        "ts": "2026-10-20T12:00:00.000000",
        "level": "INFO",
        "event": event,
        "message": f"Task [{task_id}] ran in session {session_id}",
        "task_id": task_id,
        "data": {"attempt": 1, "session_id": session_id, "reused": False},
    }


def test_collects_last_session_per_task(tmp_path):
    """Two bindings for one task: the later one is the session to resume."""
    log = tmp_path / "execution.log"
    _write_log(
        log,
        [
            _bound("3", "session-old"),
            _bound("3", "session-new"),
            _bound("4", "session-4"),
        ],
    )

    assert pause_state.collect_task_sessions(log) == {
        "3": "session-new",
        "4": "session-4",
    }


def test_missing_log_returns_empty(tmp_path):
    """A plan directory with no execution log has no sessions to resume."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    assert pause_state.collect_task_sessions(plan_dir / "execution.log") == {}


def test_skips_malformed_lines(tmp_path):
    """One unreadable line does not cost the bindings around it."""
    log = tmp_path / "execution.log"
    _write_log(
        log,
        [
            "this line is not json at all",
            _bound("1", "session-1"),
            {"event": "task_session_bound", "data": {"session_id": "no-task"}},
            {"event": "task_session_bound", "task_id": "2", "data": {}},
            {"event": "task_session_bound", "task_id": "3", "data": {"session_id": ""}},
            _bound("5", "session-5", event="task_started"),
            _bound("6", "session-6"),
            # No event name at all: not a binding, even though the
            # payload looks like one.
            {"task_id": "7", "data": {"session_id": "session-7"}},
            # The session id sits at the top level rather than under
            # ``data``: not where the executor writes it, so not a
            # binding.
            {"event": "task_session_bound", "task_id": "8", "session_id": "session-8"},
        ],
    )

    assert pause_state.collect_task_sessions(log) == {
        "1": "session-1",
        "6": "session-6",
    }


def test_reads_rotated_backups_oldest_first(tmp_path):
    """Rotated logs are read oldest first, and the live log still wins."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    log = plan_dir / "execution.log"

    _write_log(plan_dir / "execution.log.3", [_bound("3", "session-oldest")])
    _write_log(plan_dir / "execution.log.2", [_bound("3", "session-middle"), _bound("9", "session-9")])
    _write_log(plan_dir / "execution.log.1", [_bound("3", "session-newer")])
    _write_log(log, [_bound("3", "session-current")])

    assert pause_state.collect_task_sessions(log) == {
        "3": "session-current",
        "9": "session-9",
    }


def test_rotation_depth_follows_the_logger(monkeypatch, tmp_path):
    """The reader looks at as many rotated copies as the writer keeps.

    A binding can only be found if the reader reaches the copy it was
    rotated into, so the depth the reader looks at is the depth the
    logger writes. It is read from the logger rather than copied into
    the reader, because a copy is a number that goes stale the moment
    the rotation depth changes.
    """
    monkeypatch.setattr(execution_logger.ExecutionLogger, "BACKUP_COUNT", 5)

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    log = plan_dir / "execution.log"

    _write_log(plan_dir / "execution.log.5", [_bound("3", "session-deep")])

    assert pause_state.collect_task_sessions(log) == {"3": "session-deep"}
