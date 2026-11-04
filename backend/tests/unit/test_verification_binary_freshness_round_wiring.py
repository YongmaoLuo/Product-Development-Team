"""TDD: the round-level binary-freshness preflight is wired into the round.

Background
----------
``Orchestrator._run_binary_freshness_round_preflight`` knows how to answer
"may this round verify anything at all?" — it looks at the tree, rebuilds
if needed, looks again, and hands back either ``None`` (the round may go
on) or a BLOCKED report about the *environment*.

Knowing is not enough.  Until the gate is called from
:meth:`run_full_verification`, it never runs: the round spends its
Phase-1 LLM call on a plan, dispatches verification points, and only
then does the environment turn out to be broken.  The round then reports
a pile of failures about a tree nobody could have been verifying — and
the Phase-1 tokens are already spent.

What this pins
--------------

  1. a blocking preflight ends the round: no plan is generated, and the
     blocked report is written down (a BLOCKED round nobody can read is
     a round that looks like it never happened);
  2. a passing preflight lets the round carry on into the plan;
  3. the gate sits **once per round**, after the round's own resets and
     after the base-drift check, and before the plan is generated — a
     check that ran per verification point would rebuild under the
     round's own feet, and a check that ran after the plan has already
     spent the call it was supposed to save.

The verdict is about the environment, not the product: the blocked report
carries an **empty** ``verification_results`` — a round that measured
nothing must not look like a round that measured a failure.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
from tests.app_source import find_def  # noqa: E402


PLAN_DIR_NAME = "plan-20261101"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePersistence:
    """The one persistence surface the round's blocked path touches."""

    def __init__(self) -> None:
        self.reports = []

    def update_report(self, report_data):
        self.reports.append(report_data)


class _PlanReached(Exception):
    """Sentinel: the round got as far as generating a verification plan."""


def _blocked_report() -> dict:
    """The shape ``_run_binary_freshness_round_preflight`` hands back.

    Mirrors the real report: BLOCKED, an environment ``stop_reason``,
    and **no** verification results — the round dispatched nothing.
    """
    return {
        "overall_status": "BLOCKED",
        "stop_reason": "binary_freshness_preflight_failed",
        "verification_results": [],
        "requirement_deviations": [],
        "summary": "binary freshness preflight failed: stale .pyc",
    }


def _orchestrator(tmp_path: Path):
    """An orchestrator carrying only the attributes the round reads.

    Built with ``__new__`` on purpose: the round's own bookkeeping
    (clearing the verdict cache, opening the round, resetting the
    per-round caches) is not what this file tests — it is stubbed so the
    test can look at the one thing that is.
    """
    orch = verification_agent.Orchestrator.__new__(
        verification_agent.Orchestrator
    )
    orch.project_dir = tmp_path
    orch.plan_dir = tmp_path / PLAN_DIR_NAME
    orch.persistence = _FakePersistence()
    orch._current_round = 1
    orch._clear_verification_state_files = lambda: None
    orch.start_verification_round = lambda round_number: None
    orch._freshness_cache = SimpleNamespace(clear=lambda: None)
    orch._rebuild_coordinator = SimpleNamespace(clear=lambda: None)
    # The base-drift gate is a different gate with a different verdict;
    # it must not be what stops this round.
    orch._run_base_drift_preflight = lambda: None
    return orch


def _stub_the_plan(monkeypatch, reached: list) -> None:
    """Make plan generation record that it happened and stop the round.

    The round carries on past the plan into service preflight and the
    executor; this file is not about those.  Raising here ends the round
    at the first step that is not under test.
    """

    def _plan(self, *a, **k):
        reached.append("plan")
        raise _PlanReached("the round generated a verification plan")

    monkeypatch.setattr(
        verification_agent.Orchestrator, "generate_verification_plan", _plan,
    )


def _dotted(node) -> str:
    """``self.foo.bar`` for the call target of a call expression."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# ---------------------------------------------------------------------------
# The round that stops
# ---------------------------------------------------------------------------


def test_blocked_round_persists_report_and_skips_plan(
    monkeypatch, tmp_path,
):
    """A blocked round is written down, and the round stops there.

    The report has to reach the persistence layer — a BLOCKED round that
    nobody can read is a round that looks like it never happened.  And
    the round must not carry on: no verification plan, no verification
    point dispatched against a tree the environment has already refused.
    """
    orch = _orchestrator(tmp_path)
    blocked = _blocked_report()
    orch._run_binary_freshness_round_preflight = lambda: blocked
    reached: list = []
    _stub_the_plan(monkeypatch, reached)

    try:
        report = orch.run_full_verification(round_number=1)
    except _PlanReached:
        pytest.fail(
            "the round generated a verification plan despite a blocked "
            "binary-freshness preflight"
        )

    assert report is blocked, (
        f"the round must return the blocked report; got {report!r}"
    )
    assert report["stop_reason"] == "binary_freshness_preflight_failed", (
        f"the blocked report must say why the round stopped; got "
        f"{report.get('stop_reason')!r}"
    )
    assert report["verification_results"] == [], (
        f"a round that dispatched nothing must carry no verification "
        f"results; got {report.get('verification_results')!r}"
    )
    assert reached == [], (
        f"a blocked round must not generate a verification plan; got "
        f"{reached!r}"
    )
    assert len(orch.persistence.reports) == 1, orch.persistence.reports
    assert orch.persistence.reports[0]["stop_reason"] == (
        "binary_freshness_preflight_failed"
    ), orch.persistence.reports[0]


def test_blocked_round_survives_a_failing_persistence(
    monkeypatch, tmp_path,
):
    """The report is a nicety: a broken persistence layer must not turn a
    blocked round into a crashed round.

    The verdict is already known — the environment refused the round.
    Failing to write it down is worth a warning, not an exception that
    hides the verdict.
    """
    orch = _orchestrator(tmp_path)
    blocked = _blocked_report()
    orch._run_binary_freshness_round_preflight = lambda: blocked

    def boom(report_data):
        raise RuntimeError("the state database is down")

    orch.persistence.update_report = boom
    reached: list = []
    _stub_the_plan(monkeypatch, reached)

    report = orch.run_full_verification(round_number=1)

    assert report is blocked, (
        f"the round must still return the blocked report; got {report!r}"
    )
    assert reached == [], (
        f"a blocked round must not generate a verification plan; got "
        f"{reached!r}"
    )


# ---------------------------------------------------------------------------
# The round that goes on
# ---------------------------------------------------------------------------


def test_passing_preflight_keeps_the_round_flow(monkeypatch, tmp_path):
    """A preflight that lets the round go on must not stop it.

    The sentinel exception raised by the stubbed plan proves the flow
    reached plan generation: the gate is a gate, not a wall.
    """
    orch = _orchestrator(tmp_path)
    calls: list = []
    orch._run_binary_freshness_round_preflight = lambda: calls.append(
        "preflight"
    )
    reached: list = []
    _stub_the_plan(monkeypatch, reached)

    with pytest.raises(_PlanReached):
        orch.run_full_verification(round_number=1)

    assert calls == ["preflight"], (
        f"the freshness preflight must run once per round; got {calls!r}"
    )
    assert reached == ["plan"], (
        f"a passing preflight must let the round reach the plan; got "
        f"{reached!r}"
    )
    assert orch.persistence.reports == [], (
        f"a passing preflight must not write a blocked report; got "
        f"{orch.persistence.reports!r}"
    )


# ---------------------------------------------------------------------------
# The call site
# ---------------------------------------------------------------------------


def test_call_site_is_once_per_round_before_plan():
    """The gate sits once per round, between the round's reset and the
    plan.

    *Once*: a check that ran per verification point would rebuild the
    tree under the round's own feet, and the round's report would depend
    on how many verification points it happened to have.

    *After the resets and after the base-drift gate*: the resets are what
    make the round's own bookkeeping per-round, and the base-drift gate
    is the earlier of the two environment gates — a round blocked on
    drift must not spend a rebuild on the way out.

    *Before the plan*: the plan is what the round executes; a round that
    discovers the stale tree after the plan exists has already spent the
    LLM call that produced it, and a stale tree is knowable before
    anything is planned.
    """
    found = find_def("run_full_verification")
    calls = [
        node for node in ast.walk(found.node) if isinstance(node, ast.Call)
    ]
    preflight = [
        c for c in calls
        if _dotted(c.func) == "self._run_binary_freshness_round_preflight"
    ]
    plan = [
        c for c in calls
        if _dotted(c.func) == "self.generate_verification_plan"
    ]
    reset = [
        c for c in calls
        if _dotted(c.func) == "self._rebuild_coordinator.clear"
    ]
    base_drift = [
        c for c in calls
        if _dotted(c.func) == "self._run_base_drift_preflight"
    ]

    assert len(preflight) == 1, [
        _dotted(c.func) for c in calls
    ]
    assert plan, "the round no longer generates a verification plan"
    assert reset, "the round no longer resets the rebuild coordinator"
    assert base_drift, "the round no longer runs the base-drift gate"
    assert (
        reset[0].lineno
        < base_drift[0].lineno
        < preflight[0].lineno
        < plan[0].lineno
    ), (
        f"the freshness preflight must run after the round's reset "
        f"({reset[0].lineno}) and after the base-drift gate "
        f"({base_drift[0].lineno}), and before the plan ({plan[0].lineno}); "
        f"it sits at {preflight[0].lineno}"
    )


def test_blocked_report_has_its_own_variable():
    """The blocked report is not the base-drift report.

    Both branches end a round with a report called "blocked", and both
    are returned from the same function.  Reusing one name would make
    the two read like the same event, and a reader could not tell a
    drifted branch from a stale tree without reading the whole method.
    """
    found = find_def("run_full_verification")
    assignments = [
        node for node in ast.walk(found.node)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "binary_blocked"
            for target in node.targets
        )
    ]

    assert len(assignments) == 1, (
        f"the binary-freshness gate must bind its own report; found "
        f"{len(assignments)} assignments to 'binary_blocked'"
    )
    value = assignments[0].value
    assert isinstance(value, ast.Call), (
        f"'binary_blocked' must hold the preflight's return value; got "
        f"{type(value).__name__}"
    )
    assert _dotted(value.func) == "self._run_binary_freshness_round_preflight", (
        f"'binary_blocked' must be the preflight's verdict; got "
        f"{_dotted(value.func)!r}"
    )
