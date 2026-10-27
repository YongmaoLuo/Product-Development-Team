"""The stop path reads the binding the executor wrote before the dispatch.

Background
----------
2026-10-26. The executor records a task's session binding twice: once
*before* the dispatch starts (the id the dispatch was asked for) and
once after the dispatch returns (the id it actually used — a provider
fallback may have switched it). The first record is the one that
matters when the stop lands while a dispatch is still blocked: without
it the stop can only answer ``no_session_id``, and the whole plan is
re-run from scratch instead of continuing in the session it was in.

The two ends of that record have to agree:

* the writer is ``agent._log_task_session_bound`` — the one place that
  writes the shape;
* the reader is ``pause_state.collect_task_sessions``, reached through
  ``routes.execution._stop_interruption`` — the stop path that decides
  how a resume treats each interrupted task.

Nothing pinned the two together: the writer's own test asserts the
shape of the line it writes, and the reader's own test asserts the
lines it reads — but neither proves that a line the *writer* produced
is a line the *reader* understands. This file is that proof. The log
is produced by the real writer and consumed by the real stop path, so
a change to either shape fails here instead of silently turning a
resumable plan into a full re-run.

What is pinned
--------------
* **one pinned binding** -- the task is interrupted with
  ``session_resume`` and that session id;
* **two bindings** -- the later one wins (the reader's rule: the newest
  binding for a task is the one a resume must use);
* **a hard kill** -- ``full_rerun``, whatever the log says: a tree that
  was SIGKILLed may have been cut mid-write, so it cannot be trusted to
  continue in its session;
* **no log at all** -- no exception, and the task falls back to a full
  re-run.

TDD spec -- 3 contract tests
----------------------------
1. ``test_predispatch_binding_yields_session_resume`` -- one pinned
   binding -> ``session_resume`` + the same id.
2. ``test_later_binding_wins`` -- two bindings -> the later one.
3. ``test_hard_kill_still_full_rerun`` -- ``hard_killed=True`` ->
   ``full_rerun``.

Boundary case (outside the three): ``test_missing_log_is_not_an_error``.
"""

import json
import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so ``import agent`` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import agent as agent_module  # noqa: E402
from coding_tool import JsonSession  # noqa: E402
from execution_logger import ExecutionLogger  # noqa: E402

# ``server`` is imported BEFORE ``routes.execution`` on purpose: the
# application module seeds ``sys.modules['server']`` and then imports
# the route modules, so importing a route module first would make it
# import a half-initialised ``server`` back.
import server  # noqa: E402,F401
from routes.execution import _stop_interruption  # noqa: E402

PLAN_ID = "vp001-predispatch-binding-read"
TASK_ID = "2"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """The plan directory the executor writes its log into.

    ``PDT_PLANS_DIR`` is pointed at this test's own tree here rather
    than left to the suite-wide redirect, so the file states the
    directory it reads and writes. The executor's logger resolves the
    same variable, so the writer and the reader land on one log.
    """
    plans_dir = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    # The task is marked ``in_progress`` before the binding is written
    # (the executor sets the status, then binds, then dispatches), so
    # that is the status a stop finds on disk.
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": TASK_ID, "status": "in_progress"}]}),
        encoding="utf-8",
    )
    return plan_dir


def _bind(session, attempt: int = 1) -> ExecutionLogger:
    """Write one binding with the real writer, into the real log."""
    logger = ExecutionLogger(PLAN_ID)
    agent_module._log_task_session_bound(logger, TASK_ID, session, attempt)
    return logger


def _answered(interrupted):
    """What the stop decided, per task: ``(task_id, strategy, session_id)``."""
    return [
        (entry["task_id"], entry["strategy"], entry["session_id"])
        for entry in interrupted
    ]


def _log_events(plan_dir: Path):
    """The event names the writer actually put on the log."""
    log = plan_dir / "execution.log"
    events = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line).get("event"))
        except ValueError:
            continue
    return events


def test_predispatch_binding_yields_session_resume(plan_dir):
    """One pinned binding -> the stop resumes exactly that session.

    The binding was written before the dispatch started, so it names
    the id the dispatch was *asked* for. The stop has to read it as a
    binding — not as an unknown line — or the task falls back to a full
    re-run and the conversation the plan paid for is lost.
    """
    _bind(JsonSession(pinned_session_id="sid-a", stage="pinned"))

    assert "task_session_bound" in _log_events(plan_dir), (
        "the writer must have put a binding on the log; got "
        f"{_log_events(plan_dir)!r}"
    )

    interrupted, _ = _stop_interruption(PLAN_ID, plan_dir, hard_killed=False)

    assert _answered(interrupted) == [(TASK_ID, "session_resume", "sid-a")]


def test_later_binding_wins(plan_dir):
    """Two bindings for one task -> the later one is the session to use.

    The executor binds a task before the dispatch and again after it
    returns, and a provider fallback may have switched the id in
    between. The reader takes the LAST binding, so the answer is the id
    the dispatch actually used.
    """
    _bind(JsonSession(pinned_session_id="sid-a", stage="pinned"), attempt=1)
    _bind(JsonSession(session_id="sid-b", stage="resumed"), attempt=1)

    interrupted, _ = _stop_interruption(PLAN_ID, plan_dir, hard_killed=False)

    assert _answered(interrupted) == [(TASK_ID, "session_resume", "sid-b")]


def test_hard_kill_still_full_rerun(plan_dir):
    """A hard kill -> ``full_rerun``, whatever the log says.

    A tree that had to be SIGKILLed may have been cut mid-write, so its
    session cannot be trusted to continue. The binding is still read
    and still reported — but the strategy stays ``full_rerun``.
    """
    _bind(JsonSession(pinned_session_id="sid-a", stage="pinned"))

    interrupted, _ = _stop_interruption(PLAN_ID, plan_dir, hard_killed=True)

    assert [
        (entry["task_id"], entry["strategy"], entry["reason"])
        for entry in interrupted
    ] == [(TASK_ID, "full_rerun", "sigkill_after_grace")]


def test_missing_log_is_not_an_error(plan_dir):
    """No execution log at all -> no binding, and no exception.

    A plan that never ran has no log. The stop has to treat that as
    "nothing to resume" rather than as a failure of the stop itself.
    """
    interrupted, _ = _stop_interruption(PLAN_ID, plan_dir, hard_killed=False)

    assert _answered(interrupted) == [(TASK_ID, "full_rerun", None)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))