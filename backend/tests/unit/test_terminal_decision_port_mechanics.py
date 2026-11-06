"""Unit tests for the mechanics of the default terminal-decision port.

Background
----------
Task 12 gave the backend a *port* — the interface the terminal executor
calls to carry a human's answer out.  The port was an interface and
nothing more: there was no implementation behind it, so the three
mechanical actions the executor names (*reset the round*, *start the
plan*, *close the plan as failed*) had nowhere to go.

This file pins the mechanics of the default implementation, and only
the mechanics:

1. **Reset asks the reset route to clear the round's artefacts.**
   The request that reaches ``reset_verification`` must carry
   ``clear_artifacts`` — and carry it verbatim, because the executor
   does not get a say in whether a stale artefact survives.
2. **Reset asks the reset route to clear the root-cause chain.**  The
   *rerun this round* answer is the one that clears it: a round that
   restarts with the chain still hanging on it walks straight back
   into the terminal branch.
3. **Start goes to the start route**, with no request body.
4. **Close-as-failed goes to the state machine**, as the ``failed``
   phase.
5. **The port constructs with no engine.**  Mechanical actions do not
   depend on the scheduler, and the constructor signature is where
   that is made visible.
6. **Reset and start are two calls, not one.**  The *executor* decides
   the order; the port does not fuse them, or *accept the failure*
   would silently start the round again.

What is deliberately *not* pinned
--------------------------------
The order of the two calls, and the outcome of the routes themselves —
those belong to the executor and to the route layer.  This file is
about the port being a faithful, mechanical adapter.

Test command::

    pytest backend/tests/unit/test_terminal_decision_port_mechanics.py -v
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, List, Tuple

import pytest


def _port_module():
    """The module under test.

    Imported inside the test bodies so a missing module is a FAILURE
    with the real reason rather than a collection ERROR.
    """
    try:
        from hitl import terminal_decision_port
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"hitl.terminal_decision_port is not importable: {exc}")
    return terminal_decision_port


@pytest.fixture
def route_calls(monkeypatch):
    """Replace the two route handlers with recorders; return the calls.

    ``import server`` MUST come before ``from routes import ...``:
    ``routes.verification`` imports ``server`` at module level, and
    ``server`` imports the route modules at the bottom of its file — the
    other order is a half-initialised circular import.
    """
    import server  # noqa: F401  (import order is load-bearing)

    from routes import verification as routes_verification

    calls: List[Tuple[str, str, Any]] = []

    def fake_reset_verification(plan_id, req=None):
        calls.append(("reset", plan_id, req))

    def fake_start_verification(plan_id, req=None):
        calls.append(("start", plan_id, req))

    monkeypatch.setattr(
        routes_verification, "reset_verification", fake_reset_verification
    )
    monkeypatch.setattr(
        routes_verification, "start_verification", fake_start_verification
    )
    return calls


@pytest.fixture
def state_calls(monkeypatch):
    """Replace ``plan_state.PlanState`` with a recorder; return the calls."""
    import plan_state

    calls: List[Tuple[str, Any]] = []

    class RecordingPlanState:
        def __init__(self, plan_dir):
            calls.append(("init", plan_dir))

        def transition_to(self, phase):
            calls.append(("transition_to", phase))
            return phase

    monkeypatch.setattr(plan_state, "PlanState", RecordingPlanState)
    return calls


# ---------------------------------------------------------------------------
# 1. Reset clears the round's artefacts
# ---------------------------------------------------------------------------


def test_reset_passes_clear_artifacts_true(route_calls, tmp_path):
    """Reset hands the reset route a request that clears the artefacts."""
    module = _port_module()

    port = module.DefaultTerminalDecisionPort(plan_dir_of=lambda pid: tmp_path)
    port.reset("p1")

    assert len(route_calls) == 1, (
        "reset must reach the reset route exactly once; it reached "
        f"{route_calls!r}"
    )
    kind, plan_id, request = route_calls[0]
    assert kind == "reset"
    assert plan_id == "p1"
    assert request is not None, "reset must carry a request object"
    assert getattr(request, "clear_artifacts", None) is True, (
        "reset must clear the round's artefacts by default; the request "
        f"carried {getattr(request, 'clear_artifacts', None)!r}"
    )

    # The flag is passed through verbatim, not merely defaulted: an
    # executor that asked to keep the artefacts must get exactly that.
    port.reset("p2", clear_artifacts=False)

    assert len(route_calls) == 2, (
        f"the second reset did not reach the reset route; calls={route_calls!r}"
    )
    _, plan_id_2, request_2 = route_calls[1]
    assert plan_id_2 == "p2"
    assert getattr(request_2, "clear_artifacts", None) is False, (
        "clear_artifacts must be handed to the request object unchanged; "
        f"it arrived as {getattr(request_2, 'clear_artifacts', None)!r}"
    )


def test_reset_clears_the_root_cause_chain_by_default(route_calls, tmp_path):
    """A rerun takes the chain with it — the port asks for that.

    The chain is the ledger that says which points are still standing
    between the plan and the human.  A round that restarts with the
    chain still hanging on it walks straight back into the terminal
    branch — the very state the human asked to get out of.  The route's
    own default is *off*; this path is what turns it on.
    """
    module = _port_module()

    port = module.DefaultTerminalDecisionPort(plan_dir_of=lambda pid: tmp_path)
    port.reset("p1")

    kind, _, request = route_calls[0]
    assert kind == "reset"
    assert getattr(request, "clear_root_cause_chain", None) is True, (
        "the rerun path must ask the route to clear the chain; the request "
        f"carried {getattr(request, 'clear_root_cause_chain', None)!r}"
    )


# ---------------------------------------------------------------------------
# 2. Start goes to the start route
# ---------------------------------------------------------------------------


def test_start_calls_start_verification(route_calls, tmp_path):
    """Start reaches the start route with the plan id and no body."""
    module = _port_module()

    port = module.DefaultTerminalDecisionPort(plan_dir_of=lambda pid: tmp_path)
    port.start("p1")

    assert route_calls == [("start", "p1", None)], (
        "start must reach start_verification as (plan_id, None); it "
        f"reached {route_calls!r}"
    )


# ---------------------------------------------------------------------------
# 3. Close-as-failed goes to the state machine
# ---------------------------------------------------------------------------


def test_close_as_failed_transitions_to_failed(state_calls, tmp_path):
    """Close-as-failed moves the plan to the ``failed`` phase."""
    module = _port_module()

    port = module.DefaultTerminalDecisionPort(plan_dir_of=lambda pid: tmp_path)
    port.close_as_failed("p1")

    assert ("transition_to", "failed") in state_calls, (
        "close_as_failed must move the plan to the 'failed' phase; the "
        f"state machine was asked for {state_calls!r}"
    )
    inits = [call for call in state_calls if call[0] == "init"]
    assert len(inits) == 1, (
        f"close_as_failed must build exactly one PlanState; it built {inits!r}"
    )
    assert Path(inits[0][1]) == tmp_path, (
        "the plan directory must come from the port's own resolver; it "
        f"came from {inits[0][1]!r}"
    )


# ---------------------------------------------------------------------------
# 4. The port needs no engine
# ---------------------------------------------------------------------------


def test_port_constructs_without_engine(route_calls, state_calls, tmp_path):
    """The port constructs with nothing, and its three actions still work."""
    module = _port_module()

    parameters = inspect.signature(module.DefaultTerminalDecisionPort).parameters
    assert "engine" not in parameters, (
        "the port must not take an engine — mechanical actions do not "
        f"depend on the scheduler; the signature is {parameters!r}"
    )

    port = module.DefaultTerminalDecisionPort()

    port.reset("p1")
    port.start("p1")
    port.close_as_failed("p1")

    assert [call[0] for call in route_calls] == ["reset", "start"], (
        "a port built with no engine must still reset and start; the "
        f"routes were asked for {route_calls!r}"
    )
    assert ("transition_to", "failed") in state_calls, (
        "a port built with no engine must still close the plan as failed; "
        f"the state machine was asked for {state_calls!r}"
    )


# ---------------------------------------------------------------------------
# 5. Reset and start are two calls, not one
# ---------------------------------------------------------------------------


def test_reset_and_start_are_separate_calls(route_calls, tmp_path):
    """Reset alone does not start the round; the executor decides that."""
    module = _port_module()

    port = module.DefaultTerminalDecisionPort(plan_dir_of=lambda pid: tmp_path)
    port.reset("p1")

    assert [call[0] for call in route_calls] == ["reset"], (
        "reset must not start the round as well — the executor is what "
        f"decides the order; the routes were asked for {route_calls!r}"
    )