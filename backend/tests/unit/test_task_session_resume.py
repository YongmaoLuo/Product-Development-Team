"""The plain-text path creates a session under the id its caller pinned.

What this pins
--------------
``JsonSession`` carries two session ids because the CLI draws that
distinction itself: ``--session-id <id>`` CREATES a session under that
id, ``--resume <id>`` RE-OPENS a session that already exists. The JSON
path honours the pinned id for the CREATE case; the plain-text path
(``query``) did not, so a caller that pinned a deterministic id — one
derived from the plan and the step, so that a ``/start`` after a
``/stop`` re-opens the same conversation — had its pinned id ignored
and got a random uuid instead.

The two tests below pin the two halves of that contract:

1. **A pinned handle creates the session under the pinned id.** The
   dispatch carries ``session_id == pinned`` and ``resume is False`` —
   CREATE, not RE-OPEN, because the session does not exist yet.
2. **A caller that pins nothing still gets a random id every time.**
   Two unbound calls get two different ids.

Why the second test uses two UNBOUND calls
--------------------------------------------
The obvious way to write it — two dispatches through one empty handle —
proves nothing about randomisation: the first dispatch writes its live
id back into the handle, so the second dispatch re-opens that session
and gets the very same id. That is the reuse contract working, not a
failure to randomise. So the negative control binds no handle at all,
and the two ids have to differ because each dispatch draws its own.

Why the assertion is at ``_run_interactive_resilient``
-----------------------------------------------------
Asserting that a caller passed something along proves nothing: the
session could be dropped anywhere between the caller and the CLI. This
mocks the layer ``query`` dispatches through and reads the kwargs that
layer receives, so "created under session id X" is a statement about the
dispatch rather than about the plumbing. No ``claude`` subprocess is
started.

Where ``stable_session_id`` lives, and why two tests below
-----------------------------------------------------------
The deterministic id is a property of the *session*, not of the repair
generator that happened to need it first: the task-execution path needs
the same id and must not import ``repair_generator`` (that would drag
the verification / test-quality stack into every task). So the
function lives in ``plan_sessions`` — the module that owns session ids —
and ``repair_generator`` re-exports it.

Two tests pin that arrangement:

3. **The re-export is the same object, not a copy.** Two modules can
   hold two functions that happen to agree today; they stop agreeing
   the moment one of them is edited. ``is`` is the assertion that
   catches that.
4. **The id is the same in another process.** ``uuid5`` is
   deterministic by construction, but "deterministic" is a property
   worth *observing* from a second interpreter: a plan that was
   ``/stop``-ped in one process and ``/start``-ed in another has to land
   in the same session, and that is exactly what a second process
   proves.

The task session's three-stage recovery
---------------------------------------
The four tests at the bottom of this file cover the *other* half of
the session contract — how the executor decides which session a task
should run in:

1. **The resume mapping wins.** ``PDT_RESUME_SESSIONS`` names the
   session a stopped task was running in; nothing else may override it.
2. **The sidecar comes next.** A session id recorded in the plan's
   sidecar is re-opened (``--resume``), because that session exists.
3. **A deterministic id is the last resort.** With nothing recorded,
   the id derived from the plan and the task is *pinned*: the first
   dispatch CREATES the session under it, and the next process finds
   it in the sidecar and re-opens it.

The stage that mattered most was the third: a task stopped before its
first dispatch returned had no id anywhere, so the ``/start`` that
followed it fell back to a full re-run. Pinning the id and recording
it *before* the dispatch closes that hole.

The round those stages exist for
--------------------------------
Each stage is pinned on its own above; the test at the bottom of this
file pins the *round* they exist for — ``/stop`` arriving before the
first dispatch has returned anything, then ``/start`` again in a fresh
process. Its assertion is at the dispatch, because that is where
"re-open the conversation" and "start an empty one under the same
name" look identical from the outside and differ completely in what
the task gets to keep.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    JsonSession,
    query_in_session,
)
import agent as agent_module  # noqa: E402
from plan_sessions import (  # noqa: E402
    load_session_id,
    save_session_id,
    stable_session_id,
)

#: A canonical dashed UUID — the shape ``--session-id`` accepts.
PINNED_ID = "11111111-2222-3333-4444-555555555555"


def _tool(monkeypatch):
    """A tool whose ``_run_interactive_resilient`` records its kwargs.

    The stand-in answers in the session it was asked for, which is what
    the CLI does in the ordinary case: the id the dispatch asked for is
    the id that exists afterwards.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded: list[dict] = []

    def _spy(prompt, system_instruction, **kwargs):
        # The launcher stamps the provider it selected; the fallback and
        # degradation paths read it back to say which provider missed.
        tool.current_call_provider = "vendor-a-pro"
        recorded.append({"prompt": prompt, **kwargs})
        return ("done", kwargs.get("session_id"))

    monkeypatch.setattr(tool, "_run_interactive_resilient", _spy)
    tool._calls = recorded
    return tool


def test_text_path_creates_session_under_pinned_id(monkeypatch):
    """A handle that pins an id gets a session CREATED under it.

    ``resume is False``: the session does not exist yet, so the
    dispatch creates it (``--session-id``) rather than re-opening one
    (``--resume``), which would fail.
    """
    tool = _tool(monkeypatch)
    handle = JsonSession(pinned_session_id=PINNED_ID)

    query_in_session(tool, handle, "do work")

    assert len(tool._calls) == 1, tool._calls
    assert tool._calls[0]["session_id"] == PINNED_ID
    assert tool._calls[0]["resume"] is False


def test_text_path_without_pinned_id_still_randomises(monkeypatch):
    """A caller that pins nothing gets a different random id each time.

    Both dispatches CREATE a session (``resume is False``) — this path
    has no live session to re-open — and neither id repeats.
    """
    tool = _tool(monkeypatch)

    tool.query("first")
    tool.query("second")

    assert len(tool._calls) == 2, tool._calls
    first, second = tool._calls
    assert first["resume"] is False
    assert second["resume"] is False
    assert first["session_id"]
    assert second["session_id"]
    assert first["session_id"] != second["session_id"], (
        "a caller that pins nothing must get a fresh id per dispatch; "
        f"both dispatches used {first['session_id']!r}"
    )


def test_stable_session_id_is_reexported_from_repair_generator():
    """``repair_generator`` re-exports the one ``plan_sessions`` owns.

    ``is``, not ``==``: two functions that merely agree today drift the
    first time one of them is edited, and the drift is invisible until
    a plan resumes the wrong session.
    """
    import plan_sessions
    import repair_generator

    assert repair_generator.stable_session_id is plan_sessions.stable_session_id, (
        "repair_generator must re-export plan_sessions.stable_session_id "
        "itself, not a copy of it"
    )


def test_stable_session_id_is_deterministic_across_processes():
    """A second interpreter derives the same id from the same inputs.

    This is what makes a ``/stop``-ped round resumable by the ``/start``
    that follows it in a different process: both have to ask for the
    same session, not two sessions that merely look alike.
    """
    from plan_sessions import stable_session_id

    plan_id = "20261021-PDT"
    step = "task:9"
    local = stable_session_id(plan_id, step)

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_BACKEND_DIR)] + ([existing] if existing else [])
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from plan_sessions import stable_session_id;"
            f"print(stable_session_id({plan_id!r}, {step!r}))",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert proc.returncode == 0, (
        f"the second process failed to compute the id: {proc.stderr!r}"
    )
    assert proc.stdout.strip() == local, (
        "the id must not depend on the process that computes it; the "
        f"second process got {proc.stdout.strip()!r}, this one {local!r}"
    )
    # Different plan, different step → different id.
    assert stable_session_id("20261021-PDT", "task:10") != local
    assert stable_session_id("20261022-PDT", step) != local


# ---------------------------------------------------------------------------
# The task session's three-stage recovery
# ---------------------------------------------------------------------------
#
# Every test below deletes ``PDT_RESUME_SESSIONS`` first: the mapping is
# read from the process environment, so a mapping left behind by another
# test in this module would decide the answer before the test's own
# arrangement could.

PLAN_ID = "20261021-PDT"
TASK_ID = "9"


def test_recorded_sidecar_session_is_resumed(tmp_path, monkeypatch):
    """A session id recorded in the sidecar is re-opened, not recomputed.

    The recorded id names a session that *exists* — the previous round
    created it — so the handle carries it as the live id and the first
    dispatch runs ``--resume``. No id is pinned: there is nothing left
    to pin.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)
    save_session_id(tmp_path, f"task:{TASK_ID}", "session-from-sidecar")

    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert handle.session_id == "session-from-sidecar", (
        "the sidecar's id must be the session the task re-opens; got "
        f"{handle.session_id!r}"
    )
    assert handle.pinned_session_id is None, (
        "a recorded id needs no pin — the session already exists"
    )


def test_missing_sidecar_pins_and_records_deterministic_id(tmp_path, monkeypatch):
    """With nothing recorded, the id is derived — and written down.

    The pinned id is what the first dispatch CREATES the session under;
    writing it into the sidecar *before* the dispatch is what lets the
    next process re-open that session, even though the round that
    created it was stopped before it could report an id.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)

    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    expected = stable_session_id(PLAN_ID, f"task:{TASK_ID}")
    assert handle.session_id is None, (
        "nothing was recorded, so there is no session to re-open yet"
    )
    assert handle.pinned_session_id == expected, (
        "the pinned id must be the deterministic one; got "
        f"{handle.pinned_session_id!r}, expected {expected!r}"
    )
    assert load_session_id(tmp_path, f"task:{TASK_ID}") == expected, (
        "the pinned id must be recorded before the first dispatch, so a "
        "later process can find it"
    )

    # The next process — the one that reads what this one wrote — finds
    # the id and re-opens the session rather than pinning it again.
    restarted = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)
    assert restarted.session_id == expected, (
        "a restart must re-open the recorded session, not create a new one"
    )
    assert restarted.pinned_session_id is None


def test_resume_mapping_wins_over_sidecar_and_deterministic_id(
    tmp_path, monkeypatch
):
    """The recovery mapping beats both other stages.

    The mapping is what the ``/start`` continuation path carries: it
    names the session the interrupted task was *actually* running in.
    A sidecar entry and a derived id are both guesses by comparison, so
    neither may override it.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)
    monkeypatch.setenv(
        "PDT_RESUME_SESSIONS", json.dumps({TASK_ID: "session-from-mapping"})
    )
    save_session_id(tmp_path, f"task:{TASK_ID}", "session-from-sidecar")

    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert handle.session_id == "session-from-mapping", (
        "the mapping names the session that was really running; got "
        f"{handle.session_id!r}"
    )
    assert handle.pinned_session_id is None, (
        "the mapping needs no pin — it is a session that already exists"
    )


def test_sidecar_write_failure_does_not_break_task_session(tmp_path, monkeypatch):
    """A sidecar that cannot be written costs the record, not the task.

    The write happens before the first dispatch, so an unwritable plan
    directory would abort every task in the plan if it were allowed to
    raise. The pinned id is still handed back: the session the first
    dispatch creates is the same one either way.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)

    def _boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(agent_module, "save_session_id", _boom)

    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert handle.session_id is None
    assert handle.pinned_session_id == stable_session_id(
        PLAN_ID, f"task:{TASK_ID}"
    ), "a failed write must not cost the task its pinned session id"


# ---------------------------------------------------------------------------
# Writing the LIVE session id back to the sidecar
# ---------------------------------------------------------------------------
#
# The pinned id is what the FIRST dispatch creates the session under, but
# it is not necessarily the id the dispatch ends up *using*: a provider
# fallback may switch it, and a resume that fell back to a fresh session
# reports the new one. The next process has to be told which id is live
# or it re-opens a session that never answered anything.
#
# The write-back is best-effort by construction. It happens AFTER the
# dispatch has already produced an answer, so letting it raise would fail
# a task that had already succeeded — a sidecar that cannot be written
# costs the *record* of the id, never the task.


def test_persist_task_session_records_live_session_id(tmp_path):
    """The id the dispatch used is written back under the task's own key.

    The sidecar is the only thing that carries the live id across
    processes, so a write-back that lands is the difference between the
    next process re-opening this session and opening a fresh one.
    """
    session = JsonSession(session_id="live-abc")

    agent_module._persist_task_session(tmp_path, TASK_ID, session, MagicMock())

    assert load_session_id(tmp_path, f"task:{TASK_ID}") == "live-abc", (
        "the id the dispatch actually used must be recorded under the "
        "task's own key"
    )


def test_persist_task_session_swallows_sidecar_write_failure(tmp_path, monkeypatch):
    """A sidecar that cannot be written costs the record, not the task.

    The write-back runs after the dispatch has already produced an
    answer, so an unwritable plan directory must not be allowed to fail
    the task that just succeeded.
    """
    def _boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(agent_module, "persist_session", _boom)

    session = JsonSession(session_id="live-abc")

    agent_module._persist_task_session(tmp_path, TASK_ID, session, MagicMock())

    assert load_session_id(tmp_path, f"task:{TASK_ID}") is None, (
        "a failed write leaves the sidecar alone"
    )


def _make_agent(plan_dir):
    """An ``AutonomousAgent`` skeleton wired so ``_execute_task_with_retry``
    reaches the completion decision with everything around it neutralised.

    The attribute list is copied from
    ``backend/tests/unit/test_audit_veto_gates_completion.py`` — the two
    skeletons drive the same method, and importing one from the other
    would couple the two files' fates.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = plan_dir
    agent.subagent_cfg = None
    agent.logger = None
    agent.config = MagicMock()
    agent.config.executor_system_prompt = "fake prompt"
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout.return_value = False
    agent.retry_manager = MagicMock()
    agent.retry_manager.get_retry_prompt_modifier.return_value = ""
    agent.task_manager = MagicMock()
    # The executor's per-task session is recovered from the plan
    # directory and the plan id, so both have to name something real
    # here — a mock-shaped tasks file would name no plan at all.
    agent.task_manager.tasks_file = plan_dir / "tasks.json"
    agent.plan_id = PLAN_ID
    agent.rollback_manager = MagicMock()
    agent._session_task_completed_counts = {}

    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files.return_value = []
    agent.git_manager.get_diff.return_value = ""

    agent.coding_tool = MagicMock()
    agent.coding_tool.query.return_value = (
        "Inspected the workspace; the prior round already applied this.\n"
        "TEST_RESULT: PASSED"
    )

    agent._preflight_test_command_skip = MagicMock(return_value=False)
    agent._parse_test_result = MagicMock(return_value=(True, ""))
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))
    agent._task_declared_files_exist = MagicMock(return_value=True)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._refine_after_failure = MagicMock()
    agent._persist_task_status = MagicMock()
    agent._commit_task_changes = MagicMock()
    agent._audit_task_second_pass = MagicMock(return_value=(True, ""))
    return agent


def test_execution_persists_session_after_dispatch(tmp_path, monkeypatch):
    """The live session id reaches the sidecar through the real executor.

    The dispatch is what decides the live id: a provider fallback may
    switch it, and a resume that fell back to a fresh session reports the
    new one. Only the dispatch knows, so the write-back has to happen
    there and nowhere else.

    The agent here has NO logger, and that is the point: the write-back
    must not be tucked inside the ``if task_session.session_id and
    self.logger:`` block that reports the bound session, or a run
    without an execution log would never record the id it just used.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)

    from task import SubTask

    task = SubTask(
        id=TASK_ID,
        title="恢复任务会话",
        description="把 live 会话 id 写回 sidecar",
        test_command="",
        files_to_modify=[],
        verification_only=True,
    )
    agent = _make_agent(tmp_path)

    def _fake_query_in_session(tool, session, *args, **kwargs):
        # What the real dispatch does: the handle carries whichever
        # session actually produced the reply.
        session.session_id = "live-abc"
        return "Inspected the workspace.\nTEST_RESULT: PASSED"

    monkeypatch.setattr(
        agent_module, "query_in_session", _fake_query_in_session
    )

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, "the task must still succeed — the write-back is best-effort"
    assert load_session_id(tmp_path, f"task:{TASK_ID}") == "live-abc", (
        "the id the dispatch actually used must reach the sidecar, so the "
        "next process re-opens that session instead of guessing"
    )


# ---------------------------------------------------------------------------
# The whole round: /stop before the first dispatch, then /start
# ---------------------------------------------------------------------------
#
# The stages above are each pinned on their own. This one pins the round
# they exist for: the operator stops the plan while the first dispatch is
# still in flight, and starts it again in a fresh process. Nothing had
# come back yet, so there was no live session id to report — the only
# thing that could carry the conversation across was the id the first
# round had already pinned and recorded.
#
# The assertion that matters is at the DISPATCH. The restarted round has
# to RE-OPEN that session (``--resume``); a "create" would give the task
# an empty conversation under the very same id, which from the outside
# looks exactly like a successful resume.

def test_stop_before_first_dispatch_restart_resumes_same_session(
    tmp_path, monkeypatch
):
    """A ``/stop`` before the first dispatch still leaves a resumable task.

    The two rounds below are two processes. The recovery mapping is
    process-local state that a real restart would not carry, so it is
    cleared between them; what DOES carry across is the plan
    directory's sidecar, and what the sidecar holds is the id the first
    round pinned before it dispatched anything.

    No ``claude`` subprocess is started: the dispatch is mocked at
    ``_run_interactive_resilient`` and the kwargs that layer receives
    are read back.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)

    # Process 1: /start binds the task's session, then /stop arrives
    # before the first dispatch has returned anything.
    first = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    pinned = first.pinned_session_id
    assert first.session_id is None, (
        "nothing was dispatched yet, so there is no live session to re-open"
    )
    assert pinned == stable_session_id(PLAN_ID, f"task:{TASK_ID}"), (
        "the stopped round has only the derived id to go on; got "
        f"{pinned!r}"
    )

    # Process 2: /start again. The mapping died with the process, so
    # only the sidecar can carry the id across.
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)
    second = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert second.session_id == pinned, (
        "the restarted round must re-open the session the stopped round "
        f"pinned; got {second.session_id!r}, expected {pinned!r}"
    )
    assert second.pinned_session_id is None, (
        "the id names a session that exists now — pinning it again would "
        "CREATE an empty session under the same name instead of re-"
        "opening the one that was there"
    )

    tool = _tool(monkeypatch)
    query_in_session(tool, second, "carry on with the task")

    assert len(tool._calls) == 1, tool._calls
    assert tool._calls[0]["resume"] is True, (
        "the restarted round must re-open the session, not create a new one"
    )
    assert tool._calls[0]["session_id"] == pinned, (
        "the dispatch must name the session the stopped round pinned; got "
        f"{tool._calls[0]['session_id']!r}, expected {pinned!r}"
    )
