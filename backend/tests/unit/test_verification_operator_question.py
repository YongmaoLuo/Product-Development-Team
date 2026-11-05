"""The question the verification loop puts to a human.

A verification point that asks for something the plan never scoped
cannot be repaired — only a human can answer it.  The question is asked
through :func:`hitl.service.create_decision`, the only writer of
``plan_decisions``, so it lands in the table the verification loop waits
on.

Two things this module pins down:

* **The default question never changes.**  The out-of-scope wording, the
  two options, the default answer and the point's id are the contract
  the operator already knows; a refactor that makes the question
  configurable has to leave the default *byte for byte* the same.
* **A caller may bring its own question.**  The terminal branch of the
  chain asks about the plan rather than about one point, so it has to
  be able to say what it means — while the "one question per point"
  de-duplication still applies to the calls that do name a point.

The orchestrator is built with its two heavy collaborators replaced: the
method under test reads ``self.plan_dir.name`` and nothing else, so a
real agent or repair generator would only add noise.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from verification.orchestrator import (
    OUT_OF_SCOPE_DEFAULT_OPTION,
    OUT_OF_SCOPE_OPTIONS,
    VERIFICATION_DECISION_SOURCE,
    VerificationOrchestrator,
)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """A verification orchestrator with its heavy collaborators stubbed out."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(plan_dir=plan_dir, project_dir=tmp_path)


class _DecisionSpy:
    """Stands in for ``hitl.service.create_decision``.

    Records every request it is handed, and answers with a decision that
    carries a known id — so a test can assert both *that* the question
    was asked and *what* it said.
    """

    def __init__(self, decision_id="DEC-1"):
        self.requests = []
        self._decision_id = decision_id

    def __call__(self, request, plan_dir, *args, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(decision_id=self._decision_id)


def test_out_of_scope_question_is_unchanged(orchestrator, monkeypatch):
    """The default call still asks the out-of-scope question, verbatim."""
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)

    decision_id = orchestrator._ask_a_human_about(
        {"id": "VP-1", "actual_result_summary": "env missing"},
        "out_of_scope",
        2,
    )

    assert decision_id == "DEC-1"
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.plan_id == orchestrator.plan_dir.name
    assert request.source == VERIFICATION_DECISION_SOURCE
    assert request.vp_id == "VP-1"
    assert request.failure_class == "out_of_scope"
    assert request.round == 2
    assert request.question == (
        "the verification check VP-1 asks for something this plan "
        "never scoped: env missing"
    )
    assert request.options == ["skip_the_check", "bring_it_into_scope"]
    assert request.options == list(OUT_OF_SCOPE_OPTIONS)
    assert request.default_option == "skip_the_check"
    assert request.default_option == OUT_OF_SCOPE_DEFAULT_OPTION


def test_caller_supplied_question_and_options_are_used(orchestrator, monkeypatch):
    """A caller may bring its own question, options and default."""
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)
    monkeypatch.setattr(
        orchestrator, "_open_decision_for", lambda *args, **kwargs: True,
    )

    decision_id = orchestrator._ask_a_human_about(
        {"id": "VP-1"},
        "terminal",
        3,
        question="the plan cannot continue without your answer",
        options=["continue", "stop"],
        default_option="stop",
        vp_id=None,
        check_open_question=False,
    )

    assert decision_id == "DEC-1"
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.question == "the plan cannot continue without your answer"
    assert request.options == ["continue", "stop"]
    assert request.default_option == "stop"
    assert request.vp_id is None
    assert request.failure_class == "terminal"
    assert request.round == 3


def test_open_question_for_the_point_suppresses_a_second_ask(
    orchestrator, monkeypatch,
):
    """A point with a question already waiting is not asked again."""
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)
    monkeypatch.setattr(
        orchestrator, "_open_decision_for", lambda *args, **kwargs: True,
    )

    decision_id = orchestrator._ask_a_human_about(
        {"id": "VP-1", "actual_result_summary": "env missing"},
        "out_of_scope",
        2,
    )

    assert decision_id == ""
    assert spy.requests == []


def test_decision_creation_failure_is_audited_and_returns_empty(
    orchestrator, monkeypatch,
):
    """A question that could not be created does not take the round down."""
    events = []

    def _boom(request, plan_dir, *args, **kwargs):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr("hitl.service.create_decision", _boom)
    monkeypatch.setattr(
        orchestrator,
        "_log_audit",
        lambda event, message, data=None: events.append((event, message, data)),
    )

    decision_id = orchestrator._ask_a_human_about(
        {"id": "VP-1", "actual_result_summary": "env missing"},
        "out_of_scope",
        2,
    )

    assert decision_id == ""
    assert len(events) == 1
    event, message, data = events[0]
    assert event == "verification_out_of_scope_decision_failed"
    assert "VP-1" in message
    assert data["vp_id"] == "VP-1"
    assert data["failure_class"] == "out_of_scope"
    assert data["round"] == 2


def test_explicit_none_vp_id_skips_the_open_question_lookup(
    orchestrator, monkeypatch,
):
    """``vp_id=None`` means "this question is not about a point"."""
    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)
    lookups = []

    def _lookup(plan_id, vp_id):
        lookups.append((plan_id, vp_id))
        return True

    monkeypatch.setattr(orchestrator, "_open_decision_for", _lookup)

    decision_id = orchestrator._ask_a_human_about(
        {"id": "VP-1"},
        "out_of_scope",
        2,
        vp_id=None,
        check_open_question=False,
    )

    assert decision_id == "DEC-1"
    assert lookups == []
    assert spy.requests[0].vp_id is None


def test_a_class_held_across_a_module_reload_still_asks_its_question(
    orchestrator, monkeypatch,
):
    """A reload must not make the "not given" marker leak into the request.

    The module's sentinel is rebound by a reload, while the class
    imported before it keeps the older one as the default of
    ``vp_id``.  A class that only knew the module's *current* sentinel
    would read that default as a point id and build a request whose
    ``vp_id`` is a marker object instead of a name.
    """
    import importlib

    import verification.orchestrator as orch_mod

    spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", spy)

    # The orchestrator under test was built before the reload; the
    # module's sentinel is a different object afterwards.
    importlib.reload(orch_mod)

    decision_id = orchestrator._ask_a_human_about(
        {"id": "VP-1", "actual_result_summary": "env missing"},
        "out_of_scope",
        2,
    )

    assert decision_id == "DEC-1"
    assert len(spy.requests) == 1
    assert spy.requests[0].vp_id == "VP-1"
    assert spy.requests[0].question == (
        "the verification check VP-1 asks for something this plan "
        "never scoped: env missing"
    )
