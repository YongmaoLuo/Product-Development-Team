"""Unit tests: an answered terminal decision is *executed*, not resumed.

Background
----------
``hitl.service.answer_decision`` used to record the answer and then,
unconditionally, ask the scheduler to ``resume`` the plan.  That made
the terminal round's three answers indistinguishable downstream — and
one of them is the opposite of a resume: *accept the failure* means the
plan is closed as failed, not put back to work.

What tells the two apart is the plan's **sidecar**: the terminal round
writes a record next to the plan (``verification.terminal_decision_session``)
naming the decision it asked about.  A record that names *this*
decision means the answer is a terminal one, and the answer names an
*action* that the executor carries out.  No record (or a record for
another decision) means an ordinary answer, and the ordinary behaviour
is unchanged: the plan is resumed.

What is pinned here
-------------------
1. **A terminal answer dispatches, and does not resume.**  The
   executor is called once, with the plan, the decision, the option,
   the note, the plan directory and the sidecar record — and the
   scheduler is never asked to resume the plan.
2. **Negative control.**  *Accept the failure* in particular must not
   reach the scheduler: a resume there would put back to work exactly
   what the human decided to stop.
3. **A failed dispatch keeps the answer.**  The answer is already in
   the database; the dispatch that follows may fail, and when it does
   the answer still stands and exactly one ``terminal_execution_failed``
   audit line is written.
4. **Everything else is unchanged.**  No sidecar, a corrupt sidecar
   and a sidecar for another decision all take the ordinary path: the
   scheduler is asked to resume the plan, and nothing raises.
5. **The default hand-off passes no audit callable.**  The audit line
   is written by the background thread, which opens its own store —
   the store the answer's caller holds is about to be closed.

Test command::

    cd backend && pytest tests/unit/test_answer_decision_executes.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.service import answer_decision
from hitl.store import HitlStore

#: The plan id every test here answers under.
PLAN_ID = "p1"

#: The terminal round's own options — the answers that name an action.
TERMINAL_OPTION = "accept_as_failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pending_decision(decision_id: str, plan_id: str = PLAN_ID) -> Decision:
    """A decision nobody has answered yet."""
    request = DecisionRequest(
        plan_id=plan_id,
        source="verification",
        task_id=None,
        question="The verification round failed. How should the plan continue?",
        options=["rerun_this_round", "accept_as_failed", "manual_repair_tasks"],
        default_option="accept_as_failed",
        mode=DecisionMode.AUTO.value,
        timeout_sec=1800,
    )
    return Decision.from_request(request, decision_id=decision_id)


def _write_sidecar(
    plan_dir: Path, *, decision_id: str, plan_id: str = PLAN_ID
) -> Path:
    """Write the terminal sidecar the way the verification round does.

    The writer is the real one, so the record's shape is the one the
    reader in :mod:`hitl.service` will meet in production.
    """
    from verification import terminal_decision_session

    record = terminal_decision_session.build_record(
        decision_id=decision_id,
        plan_id=plan_id,
        analysis={"summary": "the round failed"},
        session_id="session-1",
        failed_points=[{"vp_id": "VP-1"}],
    )
    return terminal_decision_session.save(plan_dir, record)


def _sidecar_json(plan_dir: Path) -> Dict[str, Any]:
    """The sidecar as it is on disk."""
    return json.loads(
        (plan_dir / "terminal_decision_session.json").read_text(encoding="utf-8")
    )


def _audit_lines(store: HitlStore, plan_id: str = PLAN_ID) -> List[Dict[str, Any]]:
    """The plan's audit mirror, as a list of records."""
    path = store.audit_path(plan_id)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _Outcome:
    """What the engine answers with — the two words the hook reads."""

    def __init__(self, outcome: str = "applied", detail: str = "resumed") -> None:
        self.outcome = outcome
        self.detail = detail


class RecordingEngine:
    """An engine that only remembers what it was asked to do.

    ``commands`` holds the **action names**, so ``"resume" not in
    engine.commands`` says what it means: the scheduler was never asked
    to resume the plan.
    """

    def __init__(self) -> None:
        self.commands: List[str] = []
        self.calls: List[Any] = []

    def submit_command(self, action: str, plan_id: str, **kwargs: Any) -> Any:
        self.commands.append(action)
        self.calls.append((action, plan_id, kwargs))
        return _Outcome()


class RecordingExecution:
    """A stand-in for the executor's hand-off.

    It records the keyword arguments it was called with — the whole
    contract between the service and the executor — and can be told to
    raise, so the failure path can be pinned.
    """

    def __init__(self, raises: Optional[BaseException] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.kwargs: Dict[str, Any] = {}
        self._raises = raises

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """The plan's directory — where its sidecar lives."""
    path = tmp_path / "plan"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def store(tmp_path: Path):
    """A store on its own database, with its own audit root."""
    opened = HitlStore(tmp_path / "state.db", audit_dir=tmp_path / "audit")
    try:
        yield opened
    finally:
        opened.close()


# ---------------------------------------------------------------------------
# 1. A terminal answer is executed, and the plan is not resumed
# ---------------------------------------------------------------------------


def test_terminal_decision_dispatches_instead_of_resume(
    store: HitlStore, plan_dir: Path
) -> None:
    """A sidecar naming this decision turns the answer into an action.

    The executor is called exactly once, with everything it needs, and
    the scheduler is never asked to resume the plan.
    """
    store.insert(_pending_decision("d1"))
    _write_sidecar(plan_dir, decision_id="d1")

    engine = RecordingEngine()
    recorder = RecordingExecution()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200), (
        "the answer was taken; the action that follows must not undo that"
    )
    assert len(recorder.calls) == 1, (
        f"the executor was called {len(recorder.calls)} times; exactly once "
        "is the contract"
    )
    assert recorder.kwargs == {
        "plan_id": PLAN_ID,
        "decision_id": "d1",
        "option": TERMINAL_OPTION,
        "note": "",
        "plan_dir": plan_dir,
        "record": _sidecar_json(plan_dir),
    }, "the executor was handed the wrong arguments"
    assert engine.commands == [], (
        f"a terminal answer must not resume the plan; the scheduler was "
        f"asked for {engine.commands!r}"
    )


def test_accept_as_failed_never_resumes(
    store: HitlStore, plan_dir: Path
) -> None:
    """Negative control: *accept the failure* must not reach the scheduler.

    A resume here would put back to work exactly the plan the human
    decided to close.
    """
    store.insert(_pending_decision("d1"))
    _write_sidecar(plan_dir, decision_id="d1")

    engine = RecordingEngine()
    recorder = RecordingExecution()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200)
    assert "resume" not in engine.commands, (
        "accept_as_failed must not submit a resume to the scheduler"
    )
    assert recorder.calls, "the action was never dispatched"


# ---------------------------------------------------------------------------
# 2. A failed dispatch keeps the answer
# ---------------------------------------------------------------------------


def test_execution_raises_keeps_answer_and_writes_one_audit(
    store: HitlStore, plan_dir: Path
) -> None:
    """A dispatch that raises loses the work, not the answer.

    The answer is already stored, so it stays stored, and the failure
    is written to the audit mirror — exactly one line.
    """
    store.insert(_pending_decision("d1"))
    _write_sidecar(plan_dir, decision_id="d1")

    before = _audit_lines(store)
    recorder = RecordingExecution(raises=RuntimeError("boom"))
    engine = RecordingEngine()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200), (
        "a failed dispatch must not turn a taken answer into a failure"
    )

    stored = store.get("d1")
    assert stored is not None
    assert stored.status == DecisionStatus.ANSWERED.value
    assert stored.answer == TERMINAL_OPTION

    after = _audit_lines(store)
    before_failures = [
        line for line in before if line.get("event") == "terminal_execution_failed"
    ]
    failures = [
        line for line in after if line.get("event") == "terminal_execution_failed"
    ]
    assert len(failures) == len(before_failures) + 1, (
        f"expected exactly one more terminal_execution_failed line, got "
        f"{failures!r}"
    )
    failure = failures[-1]
    assert failure["decision_id"] == "d1"
    assert failure["plan_id"] == PLAN_ID
    assert failure["status"] == "failed"
    assert failure["answer"] == TERMINAL_OPTION
    assert "boom" in str(failure["note"]), (
        f"the failure line does not say what went wrong: {failure['note']!r}"
    )
    answered = [line for line in after if line.get("event") == "answered"]
    assert len(answered) == 1, (
        f"the answer itself is mirrored as well; got {answered!r}"
    )


def test_broken_audit_lookup_still_keeps_the_answer(
    store: HitlStore, plan_dir: Path, monkeypatch
) -> None:
    """A store that cannot be read does not take the answer down with it.

    Writing the failure line needs to look the decision up first — to
    read the sequence number the answer left it at.  When even that
    lookup fails, the audit degrades to a warning: the answer is
    already committed, and a broken mirror is not a failed answer.
    """
    store.insert(_pending_decision("d1"))
    _write_sidecar(plan_dir, decision_id="d1")

    real_get = store.get
    calls = {"n": 0}

    def _flaky_get(decision_id: str):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("the store is gone")
        return real_get(decision_id)

    monkeypatch.setattr(store, "get", _flaky_get)

    recorder = RecordingExecution(raises=RuntimeError("boom"))

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=RecordingEngine(),
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200), (
        "a broken mirror must not turn a taken answer into a failure"
    )


# ---------------------------------------------------------------------------
# 3. Everything else is unchanged
# ---------------------------------------------------------------------------


def test_other_decisions_still_resume(store: HitlStore, plan_dir: Path) -> None:
    """A plan with no sidecar is an ordinary plan: the answer resumes it."""
    store.insert(_pending_decision("d2"))

    engine = RecordingEngine()
    recorder = RecordingExecution()

    status, code = answer_decision(
        "d2",
        "accept",
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200)
    assert engine.commands == ["resume"], (
        f"an ordinary answer must resume the plan; the scheduler was asked "
        f"for {engine.commands!r}"
    )
    assert recorder.calls == [], (
        "an ordinary answer must not carry out a terminal action"
    )


def test_corrupt_sidecar_falls_back_to_resume(
    store: HitlStore, plan_dir: Path
) -> None:
    """A sidecar that cannot be read is no sidecar: the answer resumes."""
    store.insert(_pending_decision("d1"))
    (plan_dir / "terminal_decision_session.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    engine = RecordingEngine()
    recorder = RecordingExecution()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200), (
        "a corrupt sidecar must not fail the answer"
    )
    assert engine.commands == ["resume"], (
        f"a corrupt sidecar falls back to the ordinary path; the scheduler "
        f"was asked for {engine.commands!r}"
    )
    assert recorder.calls == []


def test_decision_id_mismatch_falls_back_to_resume(
    store: HitlStore, plan_dir: Path
) -> None:
    """A sidecar for another question does not answer this one."""
    store.insert(_pending_decision("d1"))
    _write_sidecar(plan_dir, decision_id="some-other-decision")

    engine = RecordingEngine()
    recorder = RecordingExecution()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200)
    assert engine.commands == ["resume"], (
        f"a sidecar for another decision falls back to the ordinary path; "
        f"the scheduler was asked for {engine.commands!r}"
    )
    assert recorder.calls == [], (
        "another question's record must not carry out this answer's action"
    )


def test_without_plan_dir_still_resumes(store: HitlStore) -> None:
    """No plan directory means no sidecar, so the answer resumes the plan."""
    store.insert(_pending_decision("d1"))

    engine = RecordingEngine()
    recorder = RecordingExecution()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        execution=recorder,
    )

    assert (status, code) == ("answered", 200)
    assert engine.commands == ["resume"]
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# 4. The default hand-off
# ---------------------------------------------------------------------------


def test_default_execution_hands_off_without_an_audit_callable(
    store: HitlStore, plan_dir: Path, monkeypatch
) -> None:
    """Without an injected executor the real hand-off is used.

    The audit is left to the background thread — which opens its own
    store — because the store the answer's caller holds is about to be
    closed.
    """
    store.insert(_pending_decision("d1"))
    _write_sidecar(plan_dir, decision_id="d1")

    seen: Dict[str, Any] = {}

    def _fake_dispatch(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(
        "hitl.service.dispatch_terminal_execution", _fake_dispatch
    )

    engine = RecordingEngine()

    status, code = answer_decision(
        "d1",
        TERMINAL_OPTION,
        "alice",
        "",
        plan_id=PLAN_ID,
        store=store,
        engine=engine,
        plan_dir=plan_dir,
    )

    assert (status, code) == ("answered", 200)
    assert seen.get("plan_id") == PLAN_ID
    assert seen.get("decision_id") == "d1"
    assert seen.get("option") == TERMINAL_OPTION
    assert seen.get("note") == ""
    assert seen.get("plan_dir") == plan_dir
    assert seen.get("record") == _sidecar_json(plan_dir)
    assert "audit" in seen, "the hand-off must say what to do about the audit"
    assert seen["audit"] is None, (
        "the background thread opens its own store; the closing one must "
        "not be handed to it"
    )
    assert seen.get("port") is not None, (
        "the hand-off needs a port to carry the action out with"
    )
    assert engine.commands == [], "a terminal answer must not resume the plan"
