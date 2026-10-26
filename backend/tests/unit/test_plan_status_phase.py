"""Where a plan's phase comes from — one rule, three sources.

The criterion this file pins
----------------------------
The capacity-wait PRD says:

    *import 要求 1：等待配额必须由真实的容量事实触发，不能是全局默认状态*
    ("waiting for quota must be triggered by a real capacity fact — it
    must not be a global default state")

The previous round of this repair proved that criterion by asserting
what a *normal* plan's answer looks like.  It never said **where the
phase came from**, so the rule stayed inside the route: the phase was
whatever ``_build_plan_status`` happened to be holding when the
routing row was missing and a read had failed.  A default that lives
in the implementation is a default nobody can point at — and the one
default that must never exist is the parked one, because it is
*global*: every plan in the system would claim to be waiting for
capacity, and the operator would be sent off to fix a wall that only
one plan ran into.

:func:`plan_status.derive_phase` is that rule, as a pure function::

    routing row  >  durable execution row  >  "ready"

and every answer says which tier produced it (``phase_source``), so a
reader can tell a *recorded* phase from a *fallback*.

The parked state is allowed only on a conjunction, never on a
preference:

* the routing row says ``waiting_for_quota`` — the recorded workflow
  phase, written by the executor when it parked the plan; **or**
* the durable execution row's ``stop_reason`` says
  ``waiting_for_quota`` **and** the plan's ``tasks.json`` carries a
  capacity marker that parses.

Every other shape — a missing routing row, a missing execution row, an
unreadable marker, a malformed marker, a row that says something else —
lands on the resting default.  Never on the parked phase.

What the empty answer means
----------------------------
One shape is deliberately *not* answered with a phase: a plan with no
records at all (no routing row, no execution row, no ``tasks.json``).
There is no plan there to sit at the manual gate, so the honest answer
is the empty phase — which is what the snapshot has always said for
that case, and what the endpoint's ``404`` is about.  It is not the
parked phase either: a plan that does not exist cannot be waiting for
capacity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


#: The marker the executor writes when a run parks because every
#: provider had no capacity left (``agent._stop_reason_for_quota_wait``).
CAPACITY_MARKER = {
    "providers": ["MiniMax"],
    "needs_manual_topup": False,
    "waited_sec": 1800.0,
    "at": "2026-10-25T00:00:00",
}

#: The durable execution row of a parked run.
PARKED_ROW = {
    "plan_id": "plan-x",
    "exec_status": "waiting_for_quota",
    "stop_reason": "waiting_for_quota",
}


def _derive(routing_phase, execution_row=None, tasks_payload=None):
    from plan_status import derive_phase

    return derive_phase(routing_phase, execution_row, tasks_payload)


def _tasks_with_marker(marker=CAPACITY_MARKER, *, as_json=True):
    """``tasks.json`` carrying the capacity marker the executor wrote."""
    detail = json.dumps(marker) if as_json else marker
    return {
        "tasks": [{"id": "VP-001", "title": "do it"}],
        "stop_reason": "waiting_for_quota",
        "reason_detail": detail,
    }


# ---------------------------------------------------------------------------
# Tier 1 — the routing row
# ---------------------------------------------------------------------------


def test_the_routing_row_answers_the_phase():
    """The routing row is the phase authority, and it says where it came from."""
    from plan_status import PHASE_SOURCE_ROUTING

    derived = _derive("executing")

    assert derived.phase == "executing"
    assert derived.source == PHASE_SOURCE_ROUTING


def test_the_routing_row_wins_over_a_parked_execution_row():
    """A recorded phase is the phase — even when a durable row disagrees.

    The routing row is what ``/execution/start`` CASes and what every
    card renders.  When it is there, it is the answer, and the execution
    row is not allowed to overrule it.
    """
    from plan_status import PHASE_SOURCE_ROUTING

    derived = _derive("ready", PARKED_ROW, _tasks_with_marker())

    assert derived.phase == "ready"
    assert derived.source == PHASE_SOURCE_ROUTING


def test_a_parked_routing_row_is_allowed_to_say_so():
    """The recorded phase is allowed to be the parked one.

    The executor parks the plan by moving its workflow phase
    (``executing -> waiting_for_quota``), so the routing row carries the
    verdict directly.  This is the one shape where the parked phase
    needs no second witness.
    """
    from plan_status import PHASE_SOURCE_ROUTING, QUOTA_WAIT_PHASE

    derived = _derive("waiting_for_quota")

    assert derived.phase == QUOTA_WAIT_PHASE
    assert derived.source == PHASE_SOURCE_ROUTING


def test_a_blank_routing_phase_is_not_a_routing_answer():
    """An empty phase is not a recorded phase — it is the routing row
    saying nothing, which is the case the other tiers exist for."""
    from plan_status import PHASE_SOURCE_DEFAULT

    derived = _derive("   ", None, _tasks_with_marker())

    assert derived.source == PHASE_SOURCE_DEFAULT


# ---------------------------------------------------------------------------
# Tier 2 — the durable execution row
# ---------------------------------------------------------------------------


def test_a_parked_execution_row_with_a_capacity_marker_parks_the_plan():
    """Both halves of the conjunction, and the phase is the parked one.

    The row's ``stop_reason`` says the run stopped for capacity; the
    plan's ``tasks.json`` carries the capacity marker that says *why*.
    Neither half alone is enough.
    """
    from plan_status import PHASE_SOURCE_EXECUTION, QUOTA_WAIT_PHASE

    derived = _derive("", PARKED_ROW, _tasks_with_marker())

    assert derived.phase == QUOTA_WAIT_PHASE
    assert derived.source == PHASE_SOURCE_EXECUTION


def test_the_capacity_marker_may_already_be_parsed():
    """``reason_detail`` is written as a JSON string, but a reader that
    hands over a dict must get the same answer."""
    from plan_status import PHASE_SOURCE_EXECUTION, QUOTA_WAIT_PHASE

    derived = _derive("", PARKED_ROW, _tasks_with_marker(as_json=False))

    assert derived.phase == QUOTA_WAIT_PHASE
    assert derived.source == PHASE_SOURCE_EXECUTION


def test_a_parked_row_without_the_marker_does_not_park_the_plan():
    """The row says the run stopped; nothing says it stopped for capacity.

    A missing marker is missing information, and missing information
    lands on the resting default — never on the parked phase.
    """
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive("", PARKED_ROW, {"tasks": [{"id": "VP-001"}]})

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


def test_a_parked_row_with_an_unreadable_marker_does_not_park_the_plan():
    """A broken marker is not a marker."""
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive(
        "", PARKED_ROW,
        {"tasks": [], "reason_detail": "{not json at all"},
    )

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


def test_a_parked_row_with_an_empty_marker_does_not_park_the_plan():
    """An empty detail object carries no capacity fact."""
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive("", PARKED_ROW, _tasks_with_marker({}))

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


def test_a_marker_for_another_reason_does_not_park_the_plan():
    """The marker has to be a *capacity* marker.

    ``reason_detail`` is a free-form "why did this stop" field; a
    marker-shaped detail for some other reason must not be laundered
    into a capacity fact.
    """
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive(
        "", PARKED_ROW,
        _tasks_with_marker({"reason": "operator stopped the run"}),
    )

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


def test_an_execution_row_that_is_not_parked_never_parks_the_plan():
    """The row says something else entirely."""
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive(
        "", {"exec_status": "not_started", "stop_reason": None},
        _tasks_with_marker(),
    )

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


def test_the_stop_reason_wins_over_the_exec_status():
    """A row whose own reason says "stopped" is not a parked row.

    The row's ``stop_reason`` is the field that carries the verdict;
    the status column alone does not promote a plan to the parked
    phase.
    """
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive(
        "",
        {"exec_status": "waiting_for_quota", "stop_reason": "operator_stop"},
        _tasks_with_marker(),
    )

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


# ---------------------------------------------------------------------------
# Tier 3 — the default
# ---------------------------------------------------------------------------


def test_a_missing_routing_row_lands_on_the_resting_default():
    """The routing row is gone; the plan still rests at the manual gate.

    This is the branch the whole change exists for: the previous round
    could not say what the phase would be when the routing row was
    missing, so the phase was whatever the implementation held.
    """
    from plan_status import DEFAULT_PHASE, PHASE_SOURCE_DEFAULT

    derived = _derive("", {"exec_status": "not_started"}, None)

    assert derived.phase == DEFAULT_PHASE
    assert derived.source == PHASE_SOURCE_DEFAULT


def test_a_plan_with_no_records_at_all_has_no_phase():
    """There is no plan here, so there is no phase to report.

    The empty phase is the honest answer — and it is emphatically not
    the parked phase.  ``GET /api/plan/{id}/status`` answers ``404`` for
    such a plan, so this shape is only reachable by a direct reader.
    """
    from plan_status import PHASE_SOURCE_DEFAULT

    derived = _derive(None, None, None)

    assert derived.phase == ""
    assert derived.source == PHASE_SOURCE_DEFAULT


# ---------------------------------------------------------------------------
# The invariant that matters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "routing_phase, execution_row, tasks_payload",
    [
        # Nothing at all.
        (None, None, None),
        ("", None, None),
        # A routing row that says nothing.
        ("", None, {"tasks": []}),
        # An execution row that says nothing.
        (None, {}, None),
        # A parked row without the marker, and with a broken one.
        ("", PARKED_ROW, None),
        ("", PARKED_ROW, {"tasks": []}),
        ("", PARKED_ROW, {"reason_detail": "not json"}),
        ("", PARKED_ROW, {"reason_detail": json.dumps({"reason": "other"})}),
        # A row that says something else.
        ("", {"exec_status": "failed", "stop_reason": "task_failed"},
         _tasks_with_marker()),
    ],
)
def test_no_shape_without_a_witness_parks_the_plan(
    routing_phase, execution_row, tasks_payload,
):
    """The negative control, as a matrix.

    Every shape here is missing something the parked verdict needs.
    None of them may produce the parked phase — a default for it is
    global, and it would send the operator after a capacity wall that
    only one plan ever hit.
    """
    from plan_status import QUOTA_WAIT_PHASE

    derived = _derive(routing_phase, execution_row, tasks_payload)

    assert derived.phase != QUOTA_WAIT_PHASE, (
        "a shape with no witness for the capacity fact must not read as "
        f"parked; got {derived.phase!r}"
    )


# ---------------------------------------------------------------------------
# The answer carries its own provenance
# ---------------------------------------------------------------------------


def test_every_source_names_the_tier_that_answered():
    """``phase_source`` is what makes a fallback visible.

    A reader that cannot tell a recorded phase from a default has to
    guess — and the guess is where the "the card says X but the plan
    is doing Y" bugs come from.
    """
    from plan_status import (
        PHASE_SOURCE_DEFAULT,
        PHASE_SOURCE_EXECUTION,
        PHASE_SOURCE_ROUTING,
    )

    assert _derive("executing").source == PHASE_SOURCE_ROUTING
    assert _derive("", PARKED_ROW, _tasks_with_marker()).source == (
        PHASE_SOURCE_EXECUTION
    )
    assert _derive("", {"exec_status": "not_started"}, None).source == (
        PHASE_SOURCE_DEFAULT
    )


def test_the_source_survives_the_payload_round_trip():
    """``PlanStatus`` carries the source, and the payload keeps it.

    The notifier rebuilds its view from the server's payload, so a
    source that is dropped on the way out is a source nobody can read.
    """
    from plan_status import PlanStatus, from_payload

    status = PlanStatus(
        plan_id="plan-x",
        phase="ready",
        phase_source="routing",
    )

    payload = status.to_dict()
    assert payload["phase_source"] == "routing"

    rebuilt = from_payload(payload)
    assert rebuilt.phase_source == "routing"
    assert rebuilt.phase == "ready"
