"""Unit tests for the *revise the criterion* door of the default port.

Background
----------
The terminal round asks a human what happens next, and one of the two
answers is *revise the criterion*: the criterion the round failed on
is wrong, not the deliverable.  The failed points are marked
**obsolete**, and the round carries on without them — the new criterion
itself is written by the operator, outside the framework.

That door is
:meth:`hitl.terminal_decision_port.DefaultTerminalDecisionPort.revise_criterion`,
and it is the only place in the codebase that writes the plan file on
the human's say-so.  This file pins what it does:

1. **The failed points are obsoleted in the plan file.**  Not in
   memory: on disk, through the delta machinery that writes every other
   plan change, so the plan keeps its audit trail
   (``obsolete`` / ``obsolete_reason`` / ``obsoleted_round``).
2. **The point comes off the root-cause chain.**  A point that is no
   longer in the plan must not go on stopping the next round.
3. **Nothing obsoleted means nothing happened.**  When the point is not
   in the plan, the delta refuses, the plan file is left byte for byte
   as it was, and the decision is reported as *failed* — a silent zero
   would read as "the criterion was revised" while the plan still
   carries it.
4. **A record naming no failed point is a failure**, not an empty
   success.
5. **A missing chain file is not a failure.**  The chain is a ledger;
   a plan without one has nothing to take off it.

How these are driven
--------------------
The tests go through :func:`hitl.terminal_execution.execute_terminal_decision`
— the function ``hitl.service`` calls when a human answers — with the
**real** default port behind it.  Only two things are stubbed, and
neither is the subject of the test: the scheduler's start route (a real
one would start a real verification run) and the audit sink (which
would otherwise write into the real state database).

Test command::

    pytest backend/tests/unit/test_terminal_decision_port_criterion.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Ensure backend/ is on sys.path so ``import hitl`` works regardless of
# the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The plan file the port reads and rewrites.
PLAN_FILENAME = "verification_plan.json"

#: The chain file the port takes the obsoleted points off.
CHAIN_FILENAME = "verification_root_cause_chain.json"


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


def _execution_module():
    """The executor that calls the port, in production."""
    try:
        from hitl import terminal_execution
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"hitl.terminal_execution is not importable: {exc}")
    return terminal_execution


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    """A plan directory holding a plan file and a root-cause chain."""
    directory = tmp_path / "plans" / "p1"
    directory.mkdir(parents=True)
    (directory / PLAN_FILENAME).write_text(
        json.dumps(
            {
                "verification_points": [
                    {"id": "VP-001", "title": "登录返回 200"},
                    {"id": "VP-002", "title": "下单返回 201"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / CHAIN_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "stage": "MACHINE_EXHAUSTED",
                "vps": {
                    "VP-001": {"stage": "MACHINE_EXHAUSTED"},
                    "VP-002": {"stage": "MACHINE_EXHAUSTED"},
                },
                "head_sha": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def started(monkeypatch):
    """Stub the scheduler's start route; return the plans it was asked for.

    ``import server`` MUST come before ``from routes import ...``:
    ``routes.verification`` imports ``server`` at module level, and
    ``server`` imports the route modules at the bottom of its file — the
    other order is a half-initialised circular import.
    """
    import server  # noqa: F401  (import order is load-bearing)

    from routes import verification as routes_verification

    calls: List[str] = []

    def fake_start_verification(plan_id, req=None):
        calls.append(plan_id)

    monkeypatch.setattr(
        routes_verification, "start_verification", fake_start_verification
    )
    return calls


@pytest.fixture
def audits():
    """A recorder for the audit lines, so no real database is written."""
    lines: List[Dict[str, Any]] = []
    return lines


def _run(plan_dir, audits, option="revise_criterion", record=None):
    """Carry the option out, the way the production path does."""
    module = _port_module()
    execution = _execution_module()

    port = module.DefaultTerminalDecisionPort(plan_dir_of=lambda pid: plan_dir)
    if record is None:
        record = {"failed_points": [{"id": "VP-001"}], "round_number": 3}

    result = execution.execute_terminal_decision(
        plan_id="p1",
        decision_id="d1",
        option=option,
        port=port,
        plan_dir=plan_dir,
        record=record,
        audit=audits.append,
    )
    return result


def _plan_on_disk(plan_dir) -> Dict[str, Dict[str, Any]]:
    """The plan file's points, keyed by id."""
    raw = json.loads(
        (plan_dir / PLAN_FILENAME).read_text(encoding="utf-8")
    )
    return {
        str(point.get("id")): point
        for point in raw.get("verification_points") or []
        if isinstance(point, dict)
    }


def _chain_on_disk(plan_dir) -> Dict[str, Any]:
    """The chain file's record."""
    return json.loads((plan_dir / CHAIN_FILENAME).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The failed points are obsoleted in the plan file
# ---------------------------------------------------------------------------


def test_revise_criterion_obsoletes_the_failed_point(plan_dir, started, audits):
    """The point the round failed on is obsoleted, on disk."""
    result = _run(plan_dir, audits)

    assert result["status"] == "done", (
        f"revising the criterion must succeed; got {result!r}"
    )
    assert result["count"] == 1, (
        f"the number of obsoleted points must be reported; got {result!r}"
    )

    points = _plan_on_disk(plan_dir)
    assert points["VP-001"].get("obsolete") is True, (
        "the failed point must be marked obsolete in the plan file; got "
        f"{points['VP-001']!r}"
    )
    assert points["VP-001"].get("obsolete_reason"), (
        f"the obsoletion must say why; got {points['VP-001']!r}"
    )
    assert points["VP-002"].get("obsolete") is not True, (
        "a point the round did not fail on must be left alone; got "
        f"{points['VP-002']!r}"
    )


def test_revise_criterion_starts_the_round_again(plan_dir, started, audits):
    """The round carries on without the obsoleted points."""
    _run(plan_dir, audits)

    assert started == ["p1"], (
        "the round must be started again after the criterion is revised; "
        f"the start route was asked for {started!r}"
    )


# ---------------------------------------------------------------------------
# 2. The point comes off the root-cause chain
# ---------------------------------------------------------------------------


def test_revise_criterion_takes_the_point_off_the_chain(
    plan_dir, started, audits
):
    """An obsoleted point must not go on stopping the next round."""
    _run(plan_dir, audits)

    chain = _chain_on_disk(plan_dir)
    entries = chain.get("vps") or {}
    assert "VP-001" not in entries, (
        f"the obsoleted point must come off the chain; the chain still "
        f"holds {entries!r}"
    )
    assert "VP-002" in entries, (
        "a point that was not revised must stay on the chain; the chain "
        f"holds {entries!r}"
    )


# ---------------------------------------------------------------------------
# 3. Nothing obsoleted means nothing happened
# ---------------------------------------------------------------------------


def test_revise_criterion_fails_when_the_point_is_not_in_the_plan(
    plan_dir, started, audits
):
    """A point the plan does not carry cannot be obsoleted — and says so."""
    before = (plan_dir / PLAN_FILENAME).read_bytes()

    result = _run(
        plan_dir,
        audits,
        record={"failed_points": [{"id": "VP-404"}], "round_number": 3},
    )

    assert result["action"] == "revise_criterion", (
        "the decision must have been carried out as the criterion revision; "
        f"got {result!r}"
    )
    assert result["status"] == "failed", (
        "a point that is not in the plan cannot be revised; the decision "
        f"must be reported as failed; got {result!r}"
    )
    assert "VP-404" in result["error"], (
        "the failure must name the point that could not be revised; got "
        f"{result!r}"
    )
    after = (plan_dir / PLAN_FILENAME).read_bytes()
    assert after == before, (
        "a refused revision must leave the plan file exactly as it was"
    )


def test_revise_criterion_fails_when_the_record_names_no_failed_point(
    plan_dir, started, audits
):
    """A record with no failed point is a failure, not an empty success."""
    before = (plan_dir / PLAN_FILENAME).read_bytes()

    result = _run(plan_dir, audits, record={"round_number": 3})

    assert result["action"] == "revise_criterion", (
        "the decision must have been carried out as the criterion revision; "
        f"got {result!r}"
    )
    assert result["status"] == "failed", (
        "a record that names no failed point cannot be revised; got "
        f"{result!r}"
    )
    assert "no failed verification point" in result["error"], (
        "the failure must say that the record named no point; got "
        f"{result!r}"
    )
    after = (plan_dir / PLAN_FILENAME).read_bytes()
    assert after == before, (
        "a revision that named nothing must leave the plan file alone"
    )


# ---------------------------------------------------------------------------
# 4. A missing chain file is not a failure
# ---------------------------------------------------------------------------


def test_revise_criterion_survives_a_missing_chain_file(
    plan_dir, started, audits
):
    """The chain is a ledger; a plan without one still gets revised."""
    (plan_dir / CHAIN_FILENAME).unlink()

    result = _run(plan_dir, audits)

    assert result["status"] == "done", (
        f"a missing chain file must not fail the revision; got {result!r}"
    )
    points = _plan_on_disk(plan_dir)
    assert points["VP-001"].get("obsolete") is True, (
        f"the failed point must still be obsoleted; got {points['VP-001']!r}"
    )
