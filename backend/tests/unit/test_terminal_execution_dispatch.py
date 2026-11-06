"""Unit tests for the terminal-decision execution layer.

Background
----------
Task 11 gave the backend a *vocabulary* for the terminal round: an
answer maps to one of four actions.  But a vocabulary is not an
executor.  Nothing reset the round, nothing started it again, nothing
closed a plan as failed and nothing generated repair tasks — the answer
was recorded and then the backend carried on as if nobody had been
asked.

That is the defect this file pins shut.  Two things were wrong with
it, and they are wrong in opposite directions:

* **The chosen option was ignored.**  Every answer — *rerun this
  round*, *accept the failure*, *generate repair tasks* — took the same
  path.  In particular *accept the failure* started the round again,
  which is the opposite of what the human asked for.
* **A failure to execute would have been a failure to answer.**  The
  answer is already in the database when the execution runs; if the
  execution raised, the human's answer would be lost along with the
  work that was about to happen.  The answer must survive whatever the
  execution does.

What is pinned here
-------------------
1. **Each option runs exactly the action it names** — and only that
   action.  *Accept the failure* closes the plan and stops there: no
   ``start``, no ``resume_agent``.
2. **A failure is audited, never raised.**  The function returns a
   ``failed`` result, writes exactly one audit event
   (``terminal_execution_failed``) and returns normally.
3. **The dispatch is a hand-off, not a wait.**  ``spawn`` receives one
   callable; the port is not touched until that callable is called.
4. **The core takes no engine.**  The mechanical actions do not depend
   on the scheduler, and the signature says so.
5. **A missing sidecar degrades to ``None``** rather than raising — the
   same rule the sidecar's own reader follows (arch decision point 5).

The module under test is imported inside the test bodies on purpose: a
module that does not exist (or does not export the contract) then shows
up as a test FAILURE carrying the real reason, instead of a collection
ERROR that hides which contract broke.

Test command::

    pytest backend/tests/unit/test_terminal_execution_dispatch.py -v
"""

from __future__ import annotations

import inspect

import pytest


def _terminal_execution():
    """The module under test.

    Imported inside the test bodies so a missing module is a FAILURE
    with the real reason rather than a collection ERROR.
    """
    try:
        from hitl import terminal_execution
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"hitl.terminal_execution is not importable: {exc}")
    return terminal_execution


class RecordingPort:
    """An execution port that records what was asked of it.

    ``raises`` names the single method that raises ``RuntimeError``;
    every other method behaves normally.
    """

    def __init__(self, raises=None):
        self.calls = []
        self.records = []
        self.notes = []
        self._raises = raises

    # -- the port protocol ---------------------------------------------

    def reset(self, plan_id, *, clear_artifacts=True):
        self.calls.append("reset")
        self._maybe_raise("reset")

    def start(self, plan_id):
        self.calls.append("start")
        self._maybe_raise("start")

    def close_as_failed(self, plan_id):
        self.calls.append("close_as_failed")
        self._maybe_raise("close_as_failed")

    def generate_repair_tasks(self, plan_id, record):
        self.calls.append("generate_repair_tasks")
        self.records.append(record)
        self._maybe_raise("generate_repair_tasks")
        return 2

    def resume_agent(self, plan_id, record, note):
        self.calls.append("resume_agent")
        self.records.append(record)
        self.notes.append(note)
        self._maybe_raise("resume_agent")

    # -- helpers --------------------------------------------------------

    def _maybe_raise(self, name):
        if self._raises == name:
            raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# 1. Each option runs exactly the action it names
# ---------------------------------------------------------------------------


def test_rerun_resets_then_starts():
    """*Rerun this round* resets the round and starts it again."""
    module = _terminal_execution()
    port = RecordingPort()

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="rerun_this_round",
        port=port,
    )

    assert port.calls == ["reset", "start"], (
        "rerun_this_round must reset the round and then start it again; "
        f"the port was asked for {port.calls!r}"
    )
    assert result == {
        "action": "reset_and_start",
        "status": "done",
        "error": None,
    }, f"the result must report what happened; got {result!r}"


def test_accept_as_failed_never_starts_or_resumes():
    """*Accept the failure* closes the plan — and stops there.

    This is the regression for the defect itself: *accept the failure*
    used to start the round again, which is the opposite of what the
    human asked for.
    """
    module = _terminal_execution()
    port = RecordingPort()

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="accept_as_failed",
        port=port,
    )

    assert port.calls == ["close_as_failed"], (
        "accept_as_failed must close the plan and do nothing else; the "
        f"port was asked for {port.calls!r}"
    )
    assert "start" not in port.calls, (
        "accepting the failure must not start the round again"
    )
    assert "resume_agent" not in port.calls, (
        "accepting the failure must not hand the round to the agent"
    )
    assert result == {
        "action": "close_as_failed",
        "status": "done",
        "error": None,
    }, f"the result must report what happened; got {result!r}"


def test_manual_repair_generates_then_hands_off():
    """*Generate repair tasks* generates them, then hands off to the run."""
    module = _terminal_execution()
    port = RecordingPort()

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="manual_repair_tasks",
        port=port,
    )

    assert port.calls == ["generate_repair_tasks", "start"], (
        "manual_repair_tasks must generate the tasks and then start the "
        f"run; the port was asked for {port.calls!r}"
    )
    assert result["count"] == 2, (
        "the number of generated repair tasks must be reported; got "
        f"{result!r}"
    )
    assert result["action"] == "generate_repair_tasks"
    assert result["status"] == "done"
    assert result["error"] is None


def test_free_text_enters_agent_path():
    """Free text is not an option — the agent reads it, with the record."""
    module = _terminal_execution()
    port = RecordingPort()
    record = {"session_id": "s-1", "analysis": {"summary": "环境问题"}}

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="先别重跑，看看是不是环境问题",
        note="先别重跑，看看是不是环境问题",
        port=port,
        record=record,
    )

    assert port.calls == ["resume_agent"], (
        "free text must hand the round to the agent and nothing else; the "
        f"port was asked for {port.calls!r}"
    )
    assert port.records == [record], (
        "the analysis record must reach the agent unchanged; got "
        f"{port.records!r}"
    )
    assert port.notes == ["先别重跑，看看是不是环境问题"], (
        "the human's words must reach the agent unchanged; got "
        f"{port.notes!r}"
    )
    assert result["action"] == "agent_interpret"
    assert result["status"] == "done"
    assert result["error"] is None


# ---------------------------------------------------------------------------
# 2. A failure is audited, never raised
# ---------------------------------------------------------------------------


def test_error_is_audited_not_raised():
    """A step that raises degrades to a ``failed`` result — and is audited."""
    module = _terminal_execution()
    port = RecordingPort(raises="reset")
    audits = []

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="rerun_this_round",
        port=port,
        audit=audits.append,
    )

    assert result["status"] == "failed", (
        "a step that raises must produce a failed result; got "
        f"{result!r}"
    )
    assert result["error"] == "RuntimeError: boom", (
        "the failure must be reported as it happened; got "
        f"{result.get('error')!r}"
    )
    assert len(audits) == 1, (
        f"the failure must be audited exactly once; got {audits!r}"
    )
    assert audits[0]["event"] == "terminal_execution_failed", (
        "the audit event must say what happened; got "
        f"{audits[0].get('event')!r}"
    )


def test_audit_failure_does_not_escape():
    """Even an audit that raises cannot make the core raise."""
    module = _terminal_execution()
    port = RecordingPort(raises="reset")

    def exploding_audit(payload):
        raise RuntimeError("audit is down too")

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="rerun_this_round",
        port=port,
        audit=exploding_audit,
    )

    assert result["status"] == "failed"
    assert result["error"] == "RuntimeError: boom"


def test_unknown_action_is_audited_not_guessed(monkeypatch):
    """An action outside the vocabulary is not guessed at — it is audited.

    ``resolve_action`` cannot return one today, so the only way to reach
    this is a resolver that lies.  What matters is the shape of the
    answer: the executor must not fall through to whichever branch
    happens to be last.
    """
    module = _terminal_execution()
    monkeypatch.setattr(module, "resolve_action", lambda option: "teleport")
    port = RecordingPort()
    audits = []

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="rerun_this_round",
        port=port,
        audit=audits.append,
    )

    assert port.calls == [], (
        "an unknown action must not be carried out as some other action; "
        f"the port was asked for {port.calls!r}"
    )
    assert result["status"] == "failed"
    assert len(audits) == 1
    assert audits[0]["event"] == "terminal_execution_failed"


# ---------------------------------------------------------------------------
# 3. The dispatch is a hand-off, not a wait
# ---------------------------------------------------------------------------


def test_dispatch_uses_injected_spawn():
    """``spawn`` receives one callable; the port waits for it to be called."""
    module = _terminal_execution()
    port = RecordingPort()
    captured = []

    module.dispatch_terminal_execution(
        plan_id="p1",
        decision_id="d1",
        option="rerun_this_round",
        port=port,
        spawn=captured.append,
    )

    assert len(captured) == 1, (
        "the dispatch must hand exactly one callable to spawn; got "
        f"{captured!r}"
    )
    assert callable(captured[0]), (
        "what spawn receives must be callable"
    )
    assert port.calls == [], (
        "the port must not be touched before the callable is called; got "
        f"{port.calls!r}"
    )

    captured[0]()

    assert port.calls == ["reset", "start"], (
        "calling the handed-off callable must run the action; the port was "
        f"asked for {port.calls!r}"
    )


# ---------------------------------------------------------------------------
# 4. The core takes no engine
# ---------------------------------------------------------------------------


def test_mechanical_actions_take_no_engine():
    """``execute_terminal_decision`` must not take an ``engine``.

    The mechanical actions — reset, start, close — do not depend on the
    scheduler, and the signature is where that claim is made visible.  A
    core that took an engine would let a caller wire the scheduler in
    again, and the "mechanical" claim would become unverifiable.
    """
    module = _terminal_execution()

    parameters = inspect.signature(module.execute_terminal_decision).parameters

    assert "engine" not in parameters, (
        "the mechanical core must not take an engine; its parameters are "
        f"{sorted(parameters)!r}"
    )


# ---------------------------------------------------------------------------
# 5. A missing sidecar degrades to None
# ---------------------------------------------------------------------------


def test_missing_record_degrades_to_none(tmp_path):
    """No record and no sidecar → the agent is handed ``None``, not an error."""
    module = _terminal_execution()
    port = RecordingPort()

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="先看看情况",
        note="先看看情况",
        port=port,
        plan_dir=tmp_path,
    )

    assert result["status"] == "done", (
        "a missing sidecar must not fail the execution; got " f"{result!r}"
    )
    assert port.calls == ["resume_agent"]
    assert port.records == [None], (
        "a missing sidecar must degrade to None, not to an empty record; "
        f"got {port.records!r}"
    )


def test_sidecar_record_is_loaded_from_plan_dir(tmp_path):
    """A sidecar that *is* there is read and handed on unchanged."""
    module = _terminal_execution()
    port = RecordingPort()

    from verification import terminal_decision_session as tds

    record = tds.build_record(
        decision_id="d1",
        plan_id="p1",
        analysis={"summary": "环境问题"},
        session_id="s-1",
    )
    tds.save(tmp_path, record)

    result = module.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option="先看看情况",
        note="先看看情况",
        port=port,
        plan_dir=tmp_path,
    )

    assert result["status"] == "done", f"got {result!r}"
    assert len(port.records) == 1
    assert port.records[0]["session_id"] == "s-1", (
        "the sidecar must reach the agent; got " f"{port.records[0]!r}"
    )