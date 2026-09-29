import json
import os
import threading
from pathlib import Path

import pytest

from plan_state import PlanState


def read_plan_routing_state(plan_id, db_path):
    """Read the legacy ``PlanState`` shape for ``plan_id`` from the
    ``plan_routing`` SQLite row at ``db_path``. (Task #3.7: persistence
    moved off ``plan_state.json`` onto the ``plan_routing`` table.)
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_db(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    if row is None:
        return None
    return PlanState._sqlite_row_to_state(row)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


class TestAtomicWrite:
    def test_atomic_write_fields_complete(self, sample_plan_factory, state_db):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        ps.transition_to("executing")

        data = read_plan_routing_state(plan_dir.name, state_db)
        assert data["plan_id"] == plan_dir.name
        assert data["current_phase"] == "executing"
        assert "completed_phases" in data
        assert "review_rounds" in data
        assert "flags" in data
        assert "last_updated" in data

    def test_no_tmp_residue(self, sample_plan_factory, state_db):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        ps.transition_to("executing")

        tmp_files = list(plan_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_legacy_file_not_rewritten(self, sample_plan_factory, state_db):
        """After a transition, the legacy ``plan_state.json`` (if present)
        is NOT updated — SQLite is now the single source of truth.
        """
        plan_dir = sample_plan_factory(phase="ready")
        original_content = (plan_dir / "plan_state.json").read_text(encoding="utf-8")

        ps = PlanState(plan_dir)
        ps.transition_to("executing")

        # The on-disk legacy file is untouched (still says "ready")...
        assert (plan_dir / "plan_state.json").read_text(encoding="utf-8") == original_content
        # ...while SQLite carries the new phase.
        data = read_plan_routing_state(plan_dir.name, state_db)
        assert data["current_phase"] == "executing"

    def test_no_tmp_residue_after_write(self, sample_plan_factory, state_db):
        """No ``*.tmp`` residue is left behind by the SQLite write path."""
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        ps.transition_to("executing")
        assert list(plan_dir.glob("*.tmp")) == []


class TestConcurrency:
    def test_concurrent_same_plan(self, sample_plan_factory, state_db):
        plan_dir = sample_plan_factory(phase="interview")
        ps = PlanState(plan_dir)
        errors = []

        def worker():
            try:
                ps.transition_to("interview_complete")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        data = read_plan_routing_state(plan_dir.name, state_db)
        assert data["current_phase"] == "interview_complete"
        assert isinstance(data, dict)

    def test_lock_independence(self, sample_plan_factory, tmp_path, state_db):
        plan_dir_a = sample_plan_factory(plan_id="plan-a", phase="ready")
        plan_dir_b = sample_plan_factory(plan_id="plan-b", phase="ready")

        ps_a = PlanState(plan_dir_a)
        ps_b = PlanState(plan_dir_b)

        barrier = threading.Barrier(2)

        def worker_a():
            barrier.wait()
            for _ in range(20):
                ps_a.transition_to("executing")
                ps_a.transition_to("ready")

        def worker_b():
            barrier.wait()
            for _ in range(20):
                ps_b.transition_to("executing")
                ps_b.transition_to("ready")

        t_a = threading.Thread(target=worker_a)
        t_b = threading.Thread(target=worker_b)
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        data_a = read_plan_routing_state("plan-a", state_db)
        data_b = read_plan_routing_state("plan-b", state_db)
        assert data_a["current_phase"] in ("ready", "executing")
        assert data_b["current_phase"] in ("ready", "executing")
        assert isinstance(data_a, dict)
        assert isinstance(data_b, dict)


class TestPhaseSurvivesMetadataWrites:
    """2026-09-15 — a CAS'd phase must not be clobbered by a
    metadata-only PlanState write.

    ``_save_state_to_sqlite`` re-derived the routing value from
    ``current_phase`` on every upsert. The verification layer CASes the
    phase independently (``RoutingRepository.try_mark_phase``), so any
    later ``set_verification_*`` write dragged the live value back to the
    stale one.

    Live symptom: ``POST /api/verification/{id}/reset_rounds`` ends
    with ``set_verification_max_rounds`` → the routing value snapped from
    ``verification`` back to ``verification_running`` → the very
    next ``/start`` failed with ``409 stage_mismatch``. Reproduced twice
    on a production plan before the cause was pinned.

    2026-09-17 (schema v5): the two columns are one, and the fix is
    structural rather than a special case — ``write_plan_state`` uses
    ``ON CONFLICT DO UPDATE`` over an explicit column list, so it
    cannot touch a column it does not name. The test is kept because
    the *behaviour* (a metadata write must not move the phase) is
    still the contract.
    """

    def _routing_row(self, plan_id, db_path):
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = _open_db(str(db_path))
        try:
            migrate(conn)
            return RoutingRepository(conn).current(plan_id)
        finally:
            conn.close()

    def _cas_phase(self, plan_id, db_path, from_phase, to_phase):
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = _open_db(str(db_path))
        try:
            migrate(conn)
            RoutingRepository(conn).try_mark_phase(
                plan_id, (from_phase,), to_phase,
            )
        finally:
            conn.close()

    def test_metadata_write_preserves_the_casd_phase(
        self, sample_plan_factory, state_db,
    ):
        plan_dir = sample_plan_factory(phase="verification_running")
        ps = PlanState(plan_dir)
        # The verification layer parks the plan while an operator resets.
        self._cas_phase(
            plan_dir.name, state_db, "verification_running",
            "verification",
        )

        ps.set_verification_max_rounds(4)

        row = self._routing_row(plan_dir.name, state_db)
        assert row["current_phase"] == "verification", (
            "the CAS'd phase must survive a metadata-only PlanState write; "
            f"got {row['current_phase']!r}"
        )

    def test_a_real_phase_transition_still_moves_the_value(
        self, sample_plan_factory, state_db,
    ):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)

        ps.transition_to("executing")

        row = self._routing_row(plan_dir.name, state_db)
        assert row["current_phase"] == "executing", (
            f"a real phase change must still drive the value, got "
            f"{row['current_phase']!r}"
        )
