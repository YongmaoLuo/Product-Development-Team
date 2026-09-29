"""Unit tests for the shared bug-fix mock plan factories.

The factories live in ``backend/tests/conftest.py`` so that the state
machine, migration, API and E2E tiers all build their on-disk fixtures
from one place.  These tests pin the two contracts that matter for
shared fixtures:

  1. **Isolation** — two calls must not share mutable containers, so a
     test that mutates one payload can never leak into another test.
  2. **Round-trip** — ``write_plan_dir`` must land valid JSON on disk
     for both files, and must write only ``plan_state.json`` when no
     report is supplied.

The factories are reached through the fixture wrappers registered in
``conftest.py`` (``mock_plan_state_factory`` /
``mock_verification_report_factory`` / ``plan_dir_writer``) rather than
via a module-level import.  pytest resolves ``conftest.py`` by
directory, so the fixtures work whether pytest is invoked from the
project root (``backend/tests/unit/...``) or from ``backend/``
(``tests/unit/...``) — a plain ``from backend.tests.conftest import ...``
would break in the second case.
"""

import json

import pytest


# ---------------------------------------------------------------------------
# TDD spec 1: two calls return data that shares no mutable containers.
# ---------------------------------------------------------------------------


def test_mock_plan_factories_are_isolated(
    mock_plan_state_factory,
    mock_verification_report_factory,
):
    """Two factory calls must not share any mutable container.

    Mutating ``a`` (top-level dict, nested ``verification`` dict,
    ``completed_phases`` list, ``flags`` dict) must leave ``b``
    untouched — including when both calls were given the SAME caller
    object, which the factory is required to copy rather than alias.
    """
    shared_flags = {"arch_enabled": False, "test_enabled": False}
    shared_phases = ["interview", "prd_generation"]

    a = mock_plan_state_factory(
        "pending",
        "completed",
        flags=shared_flags,
        completed_phases=shared_phases,
    )
    b = mock_plan_state_factory(
        "pending",
        "completed",
        flags=shared_flags,
        completed_phases=shared_phases,
    )

    # Equal by value ...
    assert a == b
    # ... but distinct objects all the way down.
    assert a is not b
    assert a["completed_phases"] is not b["completed_phases"]
    assert a["flags"] is not b["flags"]
    assert a["review_rounds"] is not b["review_rounds"]
    assert a["verification"] is not b["verification"]
    # The caller's objects must not be aliased into the result either.
    assert a["flags"] is not shared_flags
    assert a["completed_phases"] is not shared_phases

    # Mutating a must not be observable through b or through the caller's
    # originals.
    a["completed_phases"].append("MUTATED")
    a["flags"]["arch_enabled"] = True
    a["review_rounds"]["prd"] = 99
    a["verification"]["status"] = "running"
    a["current_phase"] = "executing"

    assert b["completed_phases"] == ["interview", "prd_generation"]
    assert b["flags"] == {"arch_enabled": False, "test_enabled": False}
    assert b["review_rounds"] == {"prd": 0, "arch": 0, "test": 0}
    assert b["verification"]["status"] == "pending"
    assert b["current_phase"] == "completed"
    assert shared_phases == ["interview", "prd_generation"]
    assert shared_flags == {"arch_enabled": False, "test_enabled": False}

    # Boundary condition: completed_phases=None yields an independent
    # default list per call (NOT a shared mutable default argument).
    d1 = mock_plan_state_factory("pending", "completed")
    d2 = mock_plan_state_factory("pending", "completed")
    assert d1["completed_phases"] == []
    assert d2["completed_phases"] == []
    assert d1["completed_phases"] is not d2["completed_phases"]
    d1["completed_phases"].append("leak?")
    assert d2["completed_phases"] == []

    # The report factory carries the same contract.
    points = [{"id": "VP-001", "status": "PASSED"}]
    r1 = mock_verification_report_factory(
        "PASSED", verification_points=points,
    )
    r2 = mock_verification_report_factory(
        "PASSED", verification_points=points,
    )
    assert r1 == r2
    assert r1 is not r2
    assert r1["verification_points"] is not r2["verification_points"]
    assert r1["verification_points"][0] is not r2["verification_points"][0]
    assert r1["requirement_deviations"] is not r2["requirement_deviations"]
    assert r1["summary"] is not r2["summary"]

    r1["verification_points"][0]["status"] = "FAILED"
    r1["requirement_deviations"].append({"type": "missing"})
    assert r2["verification_points"][0]["status"] == "PASSED"
    assert r2["requirement_deviations"] == []
    assert points[0]["status"] == "PASSED"


# ---------------------------------------------------------------------------
# TDD spec 2: write_plan_dir lands valid JSON for both files.
# ---------------------------------------------------------------------------


def test_write_plan_dir_writes_valid_json(
    tmp_path,
    mock_plan_state_factory,
    mock_verification_report_factory,
    plan_dir_writer,
):
    """Both JSON files must land on disk and round-trip to the inputs."""
    state = mock_plan_state_factory(
        "failed",
        "verification_failed",
        plan_id="20260806-bugfix",
        flags={"arch_enabled": False, "test_enabled": False},
        completed_phases=["interview", "prd_generation", "executing"],
        verification_round=1,
        stop_reason=None,
    )
    report = mock_verification_report_factory(
        "FAILED",
        plan_id="20260806-bugfix",
        round=1,
        verification_points=[
            {"id": "VP-001", "status": "PASSED"},
            {"id": "VP-002", "status": "FAILED"},
        ],
        requirement_deviations=[
            {"type": "missing", "severity": "high", "detail": "no auth check"},
        ],
    )

    plan_dir = plan_dir_writer(tmp_path, "20260806-bugfix", state, report)

    assert plan_dir.is_dir()
    assert plan_dir == tmp_path / "20260806-bugfix"

    state_file = plan_dir / "plan_state.json"
    report_file = plan_dir / "verification_report.json"
    assert state_file.is_file()
    assert report_file.is_file()

    loaded_state = json.loads(state_file.read_text(encoding="utf-8"))
    loaded_report = json.loads(report_file.read_text(encoding="utf-8"))

    assert loaded_state == state
    assert loaded_report == report

    # Spot-check the fields the downstream tiers actually read.
    assert loaded_state["current_phase"] == "verification_failed"
    assert loaded_state["verification"]["status"] == "failed"
    assert loaded_state["verification"]["round"] == 1
    assert loaded_state["verification"]["max_rounds"] == 3
    assert loaded_state["flags"] == {
        "arch_enabled": False,
        "test_enabled": False,
    }
    assert loaded_report["overall_status"] == "FAILED"
    assert loaded_report["summary"] == {
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "total": 2,
    }

    # Boundary condition: no report -> only plan_state.json is written.
    only_state = plan_dir_writer(
        tmp_path, "20260806-no-report", mock_plan_state_factory(),
    )
    assert (only_state / "plan_state.json").is_file()
    assert not (only_state / "verification_report.json").exists()
    assert sorted(p.name for p in only_state.iterdir()) == ["plan_state.json"]


# ---------------------------------------------------------------------------
# Boundary condition: a hand-rolled state missing a required key must
# raise AssertionError from the schema helper, not silently write.
# ---------------------------------------------------------------------------


def test_write_plan_dir_rejects_incomplete_state(tmp_path, plan_dir_writer):
    """Missing required keys must raise AssertionError naming the key."""
    bad_state = {"plan_id": "x", "current_phase": "completed"}
    with pytest.raises(AssertionError) as exc:
        plan_dir_writer(tmp_path, "bad-plan", bad_state)
    assert "verification" in str(exc.value)
    assert not (tmp_path / "bad-plan" / "plan_state.json").exists()


def test_write_plan_dir_rejects_incomplete_report(
    tmp_path, mock_plan_state_factory, plan_dir_writer,
):
    """A malformed report must be rejected before anything hits disk."""
    with pytest.raises(AssertionError) as exc:
        plan_dir_writer(
            tmp_path,
            "bad-report",
            mock_plan_state_factory(),
            {"plan_id": "bad-report"},
        )
    assert "overall_status" in str(exc.value)
    assert not (tmp_path / "bad-report" / "verification_report.json").exists()
