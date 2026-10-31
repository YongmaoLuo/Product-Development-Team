"""Unit tests: one decision's failure does not take the batch with it.

What this file holds down
-------------------------
The execute sink hands every decision of a tick to an executor.  The
decisions of a tick are independent of each other — "admit this plan"
and "stop that one" have nothing to do with each other — so a decision
the executor could not carry out says nothing about the decisions
around it.

Yet the sink used to hand the whole batch to the executor in one
sweeping expression.  The first decision that raised took every
decision behind it with it: the plans after the failing one were never
handed to the executor at all, and the caller got no per-decision
result to say which ones had been carried out and which had not.

The contracts this file pins:

1. **A failure is isolated.**  The decision after a failing one is
   still handed to the executor.
2. **Every decision says how it went.**  Each entry carries ``ok`` —
   true or false — and ``error``: the exception, for a failure, and
   ``None`` for a success.
3. **Recording is unchanged.**  The record sink still writes the four
   canonical fields and nothing else: the record is the comparable
   core of a decision, and ``ok`` / ``error`` are about this run's
   execution, not about the decision itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from plan_scheduler.dryrun import ExecuteSink, RecordSink
from state_machine.repositories.routing_repository import PlanNotFoundError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The batch handed to the executor.  The first plan does not exist;
#: the second one does.  The order matters: the failure is in front of
#: the decision that has to survive it.
_DECISIONS: List[Dict[str, Any]] = [
    {
        "ts": "2026-10-29T01:00:00Z",
        "action": "admit",
        "plan_id": "ghost",
        "reason": "waiting_for_slot",
    },
    {
        "ts": "2026-10-29T01:00:05Z",
        "action": "admit",
        "plan_id": "p1",
        "reason": "capacity_available",
    },
]


def _decisions() -> List[Dict[str, Any]]:
    """A fresh copy of the shared decision list."""
    return [dict(decision) for decision in _DECISIONS]


def _executor(seen: List[str]) -> Any:
    """An executor that fails on the ghost plan and remembers the rest.

    The failure is the plan lifecycle's own: a plan that does not
    exist cannot be started, and the lifecycle says so with
    :class:`PlanNotFoundError`.
    """

    def executor(decision: Dict[str, Any]) -> Dict[str, Any]:
        plan_id = str(decision.get("plan_id"))
        seen.append(plan_id)
        if plan_id == "ghost":
            raise PlanNotFoundError(plan_id)
        return dict(decision)

    return executor


# ---------------------------------------------------------------------------
# 1. A failure is isolated
# ---------------------------------------------------------------------------


def test_first_failure_does_not_skip_second() -> None:
    """A decision that fails does not stop the ones behind it."""
    seen: List[str] = []

    results = ExecuteSink(_executor(seen)).apply(_decisions())

    # Both decisions were handed to the executor — the failing one did
    # not cut the batch short.
    assert seen == ["ghost", "p1"]

    # And the caller got one entry per decision, not just the ones
    # that made it through.
    assert len(results) == 2

    assert results[0]["plan_id"] == "ghost"
    assert results[0]["ok"] is False
    assert isinstance(results[0]["error"], PlanNotFoundError)

    assert results[1]["plan_id"] == "p1"
    assert results[1]["ok"] is True


# ---------------------------------------------------------------------------
# 2. Every decision says how it went
# ---------------------------------------------------------------------------


def test_success_entry_carries_ok_true() -> None:
    """A decision that was carried out says so, and says nothing went wrong."""
    seen: List[str] = []

    results = ExecuteSink(_executor(seen)).apply(_decisions())

    success = results[1]
    assert success["ok"] is True
    assert success["error"] is None

    # The decision's own fields stay at the top level: the caller reads
    # them by name, and the executable result is merged *into* the
    # decision rather than replacing it.
    assert success["action"] == "admit"
    assert success["plan_id"] == "p1"
    assert success["reason"] == "capacity_available"


def test_failure_entry_keeps_the_decision_fields() -> None:
    """A decision that failed still says which decision it was."""
    seen: List[str] = []

    results = ExecuteSink(_executor(seen)).apply(_decisions())

    failure = results[0]
    assert failure["action"] == "admit"
    assert failure["plan_id"] == "ghost"
    assert failure["reason"] == "waiting_for_slot"
    assert failure["ok"] is False
    assert failure["error"] is not None


def test_executor_result_is_merged_into_the_decision() -> None:
    """What the executor returned comes back on the entry as well."""
    seen: List[str] = []

    def executor(decision: Dict[str, Any]) -> Dict[str, Any]:
        seen.append(str(decision.get("plan_id")))
        return {"action": decision["action"], "plan_id": decision["plan_id"], "started": True}

    results = ExecuteSink(executor).apply(_decisions())

    assert results[1]["started"] is True
    assert results[1]["ok"] is True
    assert results[1]["error"] is None


def test_non_dict_executor_result_keeps_the_decision_fields() -> None:
    """An executor that returns something else than a mapping still works."""
    seen: List[str] = []

    def executor(decision: Dict[str, Any]) -> Any:
        seen.append(str(decision.get("plan_id")))
        return "started"

    results = ExecuteSink(executor).apply(_decisions())

    success = results[1]
    assert success["action"] == "admit"
    assert success["plan_id"] == "p1"
    assert success["reason"] == "capacity_available"
    assert success["ok"] is True
    assert success["error"] is None


def test_empty_decisions_return_an_empty_list() -> None:
    """A tick with nothing to say has nothing to report."""
    seen: List[str] = []

    assert ExecuteSink(_executor(seen)).apply([]) == []
    assert seen == []


# ---------------------------------------------------------------------------
# 3. Recording is unchanged
# ---------------------------------------------------------------------------


def test_record_sink_shape_unchanged(tmp_path: Path) -> None:
    """A record is still the four canonical fields — and nothing else."""
    sink = RecordSink(tmp_path / "out")

    records = sink.apply(_decisions())

    assert len(records) == 2
    for record, decision in zip(records, _decisions()):
        assert record == {
            "ts": decision["ts"],
            "action": decision["action"],
            "plan_id": decision["plan_id"],
            "reason": decision["reason"],
        }
        # The execution's own verdict is not part of the record: the
        # record is the comparable core of a decision.
        assert "ok" not in record
        assert "error" not in record

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line, decision in zip(lines, _decisions()):
        written = json.loads(line)
        assert written["action"] == decision["action"]
        assert written["plan_id"] == decision["plan_id"]
        assert "ok" not in written
