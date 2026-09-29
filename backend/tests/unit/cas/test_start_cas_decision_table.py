"""VP-010 / bug_2 anchor: full stage × operation decision table.

This file is the VP-010 anchor. The verification harness expects it
under ``tests/unit/cas/test_start_cas_decision_table.py`` with the
``bug_2`` marker; see ``pytest.ini`` and
``tests/test_bug_2_cas_409_anchor.py``.

The contract under test (PRD decision point 2):

  * ``start execution``       CAS stage in {ready,
                                   failed, completed}
  * ``start verification``    CAS stage in {executing,
                                   failed, completed}
  * ``stop verification``     CAS stage == verification_running

All three operations are expected to advance the row on success, and
to raise :class:`state_machine.repositories.ConflictError` on any
predicate mismatch — with ``reason`` text distinguishing the two
failure modes ("predicate mismatch" vs "version mismatch").

The test below parametrises all known stages × the three operations
and asserts that every combination matches the decision table.

The pytest.ini ``bug_2:`` line names this file as the regression
lock for the contract; if you remove these markers the
``test_every_bug_marker_has_at_least_one_anchor_case`` meta-test
fails.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import (
    ConflictError,
    PlanNotFoundError,
    RoutingRepository,
)


# Decision table — frozen snapshot of the contract from PRD.json DP-2
# and arch-design.md DP-3. Every stage × every operation pair lives
# here so the parametrization below stays declarative.
ALL_STAGES: tuple = (
    "interview",
    "interview_complete",
    "prd_generation",
    "prd_review",
    "prd_approved",
    "arch_generation",
    "arch_review",
    "arch_approved",
    "test_generation",
    "test_review",
    "test_approved",
    "tasks_generation",
    "ready",
    "executing",
    "verification",
    "verification_running",
    "verification_failed",
    "verification_passed",
    "verification_loop_stopped",
    "completed",
    "failed",
    "completed",
    "failed",
)


# The three operations that go through ``try_mark_phase``. The
# ``expected_phases`` and ``new_phase`` tuple pin the contract.
EXEC_START = (
    ("ready", "failed", "completed"),
    "executing",
)
VERIFY_START = (
    ("executing", "failed", "completed"),
    "verification_running",
)
VERIFY_STOP = (
    ("verification_running",),
    "verification",
)


def _expect_success(stage, op):
    expected, _new = op
    return stage in expected


@pytest.fixture
def db_path(tmp_path):
    """Yield a fresh ``state.db`` with the four-table schema migrated."""
    path = tmp_path / "state.db"
    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


def _seed_stage(conn, plan_id, stage):
    """Insert a fresh plan_routing row in ``stage`` (version=0)."""
    RoutingRepository(conn).insert(plan_id, stage)


@pytest.mark.bug_2
@pytest.mark.parametrize("stage", ALL_STAGES)
@pytest.mark.parametrize(
    "op_name,op",
    [
        ("exec_start", EXEC_START),
        ("verify_start", VERIFY_START),
        ("verify_stop", VERIFY_STOP),
    ],
)
def test_start_cas_decision_table(db_path, stage, op_name, op):
    """VP-010 anchor: full stage × operation decision table.

    For every stage listed in ``ALL_STAGES`` and every operation
    (``exec_start``, ``verify_start``, ``verify_stop``):

      * If ``stage in expected_phases``: ``try_mark_phase`` must
        succeed (row advances to new_phase, version +1).
      * Otherwise: ``try_mark_phase`` must raise ConflictError with
        ``reason`` containing ``"predicate mismatch"`` (i.e. the
        API surfaces ``stage_mismatch``).
    """
    expected_phases, new_phase = op
    conn = open_db(db_path)
    try:
        plan_id = f"vp010-{op_name}-{stage}"
        _seed_stage(conn, plan_id, stage)
        repo = RoutingRepository(conn)
        expect = _expect_success(stage, op)

        if expect:
            ok = repo.try_mark_phase(plan_id, tuple(expected_phases), new_phase)
            assert ok is True, f"{op_name} from {stage!r} should succeed"
            row = repo.find(plan_id)
            assert row is not None
            assert row["current_phase"] == new_phase
            # Successful CAS bumps version by 1 (from 0 to 1).
            assert row["version"] == 1
        else:
            with pytest.raises(ConflictError) as excinfo:
                repo.try_mark_phase(plan_id, tuple(expected_phases), new_phase)
            assert "predicate mismatch" in str(excinfo.value)
            # Row must be untouched on failure (version still 0).
            row = repo.find(plan_id)
            assert row is not None
            assert row["current_phase"] == stage
            assert row["version"] == 0
    finally:
        conn.close()


@pytest.mark.bug_2
def test_start_on_executing_plan_returns_409():
    """Bug-2 anchor: a second execution start on an already-executing plan
    must raise :class:`ConflictError`, which the API surfaces as HTTP 409.

    The plan starts at ``ready``; the first ``start_execution``
    succeeds (advances to ``executing``); the second is the bug-2
    regression: ``executing`` is NOT in
    ``expected_phases=(ready, failed, completed)``,
    so the call must raise :class:`ConflictError` whose ``reason``
    contains ``"predicate mismatch"``.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        conn = open_db(path)
        try:
            migrate(conn)
            routing = RoutingRepository(conn)
            plan_id = "vp010-bug2-anchor-exec"
            routing.insert(plan_id, "ready")

            assert routing.try_mark_phase(
                plan_id,
                ("ready", "failed", "completed"),
                "executing",
            ) is True

            with pytest.raises(ConflictError) as excinfo:
                routing.try_mark_phase(
                    plan_id,
                    ("ready", "failed", "completed"),
                    "executing",
                )
            assert "predicate mismatch" in str(excinfo.value)
        finally:
            conn.close()


@pytest.mark.bug_2
def test_verification_start_on_already_running_returns_409():
    """Bug-2 anchor: starting verification when verification is already
    running must raise :class:`ConflictError`.

    Predicates: ``verify_start`` requires stage in {executing,
    failed, completed}; the second call sees
    ``verification_running`` and must conflict.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        conn = open_db(path)
        try:
            migrate(conn)
            routing = RoutingRepository(conn)
            plan_id = "vp010-bug2-anchor-verify"
            routing.insert(plan_id, "executing")

            assert routing.try_mark_phase(
                plan_id,
                ("executing", "failed", "completed"),
                "verification_running",
            ) is True

            with pytest.raises(ConflictError) as excinfo:
                routing.try_mark_phase(
                    plan_id,
                    ("executing", "failed", "completed"),
                    "verification_running",
                )
            assert "predicate mismatch" in str(excinfo.value)
        finally:
            conn.close()


@pytest.mark.bug_2
def test_verification_stop_requires_running_stage():
    """Bug-2 anchor: ``verify_stop`` only succeeds from
    ``verification_running``; any other stage must raise
    :class:`ConflictError`.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        conn = open_db(path)
        try:
            migrate(conn)
            routing = RoutingRepository(conn)
            plan_id = "vp010-bug2-anchor-stop"
            routing.insert(plan_id, "executing")

            with pytest.raises(ConflictError) as excinfo:
                routing.try_mark_phase(
                    plan_id, ("verification_running",), "verification"
                )
            assert "predicate mismatch" in str(excinfo.value)
        finally:
            conn.close()


@pytest.mark.bug_2
def test_try_mark_phase_failure_leaves_no_partial_business_row(db_path):
    """Bug-2 cross-row isolation anchor: a CAS conflict must leave
    ``plan_execution`` / ``plan_verification`` untouched.
    """
    plan_id = "vp010-bug2-isolation"
    conn = open_db(db_path)
    try:
        RoutingRepository(conn).insert(plan_id, "executing")

        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cur.fetchall()}
        assert {
            "plan_routing",
            "plan_execution",
            "plan_verification",
            "plan_artifacts",
        }.issubset(tables)

        for table in ("plan_execution", "plan_verification", "plan_artifacts"):
            pre = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?", (plan_id,)
            ).fetchone()[0]
            assert pre == 0, f"{table} should be empty pre-CAS"

        with pytest.raises(ConflictError):
            RoutingRepository(conn).try_mark_phase(
                plan_id, ("ready",), "executing"
            )

        for table in ("plan_execution", "plan_verification", "plan_artifacts"):
            post = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?", (plan_id,)
            ).fetchone()[0]
            assert post == 0, f"CAS failure must not write to {table}"
    finally:
        conn.close()


@pytest.mark.bug_2
def test_conflict_distinguishes_stage_mismatch_from_version_mismatch(db_path):
    """Bug-2 anchor: ``ConflictError.reason`` distinguishes the two
    CAS failure modes.

      * Stage-mismatch: ``reason`` contains ``"predicate mismatch"``.
      * Version-mismatch: ``reason`` contains ``"version mismatch"``.

    The API layer maps these to ``stage_mismatch`` vs
    ``version_mismatch`` strings in the HTTP-409 body.
    """
    conn = open_db(db_path)
    try:
        routing = RoutingRepository(conn)
        routing.insert("vp010-conflict-stage", "executing")

        with pytest.raises(ConflictError) as stage_exc:
            routing.try_mark_phase(
                "vp010-conflict-stage", ("ready",), "executing"
            )
        assert "predicate mismatch" in str(stage_exc.value)

        # The version-mismatch branch is the ``rowcount == 0`` arm
        # of the implementation — extremely rare under
        # ``BEGIN IMMEDIATE``.  Here we pin the API mapping contract
        # via the public ``_conflict_reason`` helper.
        import importlib
        server_module = importlib.import_module("server")

        assert server_module._conflict_reason(stage_exc.value) == "stage_mismatch"

        class _FakeVersionExc(Exception):
            def __str__(self):
                return "ConflictError(version mismatch: row was bumped ...)"

        assert (
            server_module._conflict_reason(_FakeVersionExc()) == "version_mismatch"
        )
    finally:
        conn.close()


@pytest.mark.bug_2
def test_try_mark_phase_unknown_plan_raises_plan_not_found():
    """Bug-2 anchor: missing plan_id raises :class:`PlanNotFoundError`,
    NOT :class:`ConflictError`.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        conn = open_db(path)
        try:
            migrate(conn)
            routing = RoutingRepository(conn)
            with pytest.raises(PlanNotFoundError):
                routing.try_mark_phase(
                    "vp010-does-not-exist", ("ready",), "executing"
                )
        finally:
            conn.close()


@pytest.mark.bug_2
def test_empty_expected_phases_raises_value_error():
    """Bug-2 anchor: ``expected_phases=()`` is a programmer error.

    The CAS layer refuses to silently match any stage with an empty
    tuple — this is the "bug-2 silent-failure mode" the anchor
    exists to prevent.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        conn = open_db(path)
        try:
            migrate(conn)
            routing = RoutingRepository(conn)
            routing.insert("vp010-empty-tuple", "executing")
            with pytest.raises(ValueError):
                routing.try_mark_phase("vp010-empty-tuple", (), "executing")
        finally:
            conn.close()
