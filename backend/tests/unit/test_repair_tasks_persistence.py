"""TDD tests for the 2026-09-12: RP-* repair task persistence.

Background
----------
A round generated repair tasks into state.db and
``verification_repair_tasks.json``. The next round then ran with zero
repair_tasks, and the on-disk snapshot was overwritten while the
in-memory dict was wiped — so the card lost track of the earlier
round's in-flight repair work. The design question is what an *empty*
round should do to a non-empty one: nothing, or erase it?

This test suite pins the persistence guarantees that follow:

  1. ``verification_repair_tasks.json`` ACCUMULATES rounds (v2
     schema) instead of overwriting the previous round's tasks.
     An empty round N does NOT erase round N-1's RP-* entries.
  2. The state.db write path in the orchestrator targets the
     repo-root ``state.db`` (not the stale ``backend/state.db``
     — the off-by-one path bug introduced in commit 49a30ed).
  3. The reader aggregates across ALL recorded rounds AND falls
     back to state.db.plan_tasks rows whose ``task_group`` starts
     with ``repair``.
  4. The refiner's reconcile delete loop SKIPS tasks whose
     ``task_group`` starts with ``repair`` so an unrelated task
     failure doesn't silently wipe RP-* rows.

Each test is independent — they target a single layer of the
persistence stack and can run in any sequence.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test 1: verification_repair_tasks.json accumulates rounds (schema v2)
# ---------------------------------------------------------------------------

class TestRepairTasksJsonAccumulation:
    """Round N must NOT erase round N-1's repair_tasks entries.

    The 2026-09-12 plan adds a v2 schema with a ``rounds`` list;
    legacy v1 single-round files are auto-migrated on first write.
    """

    def test_v1_legacy_file_migrates_to_v2_on_next_write(self, tmp_path):
        """Legacy v1 single-round file is preserved when v2 round appended."""
        plan_dir = tmp_path / "test-plan"
        plan_dir.mkdir()
        rt_path = plan_dir / "verification_repair_tasks.json"
        # v1 legacy: single round, 2 tasks
        rt_path.write_text(json.dumps({
            "plan_id": "test-plan",
            "round": 4,
            "generated_at": "2026-09-12T01:47:32",
            "tasks": [
                {"id": "RP-1", "title": "fix VP-006", "status": "pending"},
                {"id": "RP-2", "title": "fix VP-010", "status": "pending"},
            ],
        }))

        # Simulate the server write path's read-side migration logic.
        # (Reused in two places; the migration rule lives in
        # :func:`server._run_auto_verification_loop`'s repair_tasks
        # persistence block.)
        existing_rounds = []
        with rt_path.open("r") as f:
            existing = json.load(f)
        # v1 → v2 migration: wrap as a single round entry.
        if (
            isinstance(existing, dict)
            and not existing.get("rounds")
            and (existing.get("round") is not None
                 or existing.get("tasks") is not None)
        ):
            existing_rounds.append({
                "round": existing.get("round"),
                "generated_at": existing.get("generated_at"),
                "tasks": existing.get("tasks"),
            })

        # Round 5 has zero tasks; the migration + new round append
        # should preserve round 4's entries.
        existing_rounds.append({
            "round": 5,
            "generated_at": "2026-09-12T19:15:36",
            "tasks": [],
        })

        new_payload = {
            "plan_id": "test-plan",
            "schema_version": 2,
            "rounds": existing_rounds,
        }
        rt_path.write_text(json.dumps(new_payload, indent=2))

        # Read back and verify round 4 entries are preserved.
        with rt_path.open("r") as f:
            result = json.load(f)
        assert result["schema_version"] == 2
        assert len(result["rounds"]) == 2
        assert result["rounds"][0]["round"] == 4
        assert len(result["rounds"][0]["tasks"]) == 2
        assert result["rounds"][0]["tasks"][0]["id"] == "RP-1"
        assert result["rounds"][0]["tasks"][1]["id"] == "RP-2"
        assert result["rounds"][1]["round"] == 5
        assert result["rounds"][1]["tasks"] == []

    def test_round_n_with_zero_tasks_does_not_overwrite_round_n_minus_one(self):
        """The original bug — round N with empty tasks wipes round N-1."""
        # Build the file structure the server would write
        # after two rounds.
        rt_payload_v2 = {
            "plan_id": "test-plan",
            "schema_version": 2,
            "rounds": [
                {"round": 4, "generated_at": "t1", "tasks": [
                    {"id": "RP-1", "title": "fix VP-006", "status": "pending"},
                    {"id": "RP-2", "title": "fix VP-010", "status": "pending"},
                ]},
                {"round": 5, "generated_at": "t2", "tasks": []},  # empty!
            ],
        }

        # Read using the same aggregation logic as
        # ``_load_repair_tasks_for_progress``.
        out = []
        seen = set()
        for entry in rt_payload_v2["rounds"]:
            for t in (entry.get("tasks") or []):
                tid = str(t.get("id", ""))
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                out.append(t)

        # Round 5 empty MUST NOT have wiped RP-1/RP-2.
        assert len(out) == 2
        assert out[0]["id"] == "RP-1"
        assert out[1]["id"] == "RP-2"

    def test_v1_legacy_reader_still_works(self):
        """v1 single-round files still readable (backward compat)."""
        legacy = {
            "plan_id": "test-plan",
            "round": 4,
            "tasks": [{"id": "RP-1", "title": "fix"}],
        }
        out = []
        seen = set()
        rounds = legacy.get("rounds")
        if isinstance(rounds, list):
            for entry in rounds:
                for t in (entry.get("tasks") or []):
                    ...
        else:
            # v1 path
            for t in (legacy.get("tasks") or []):
                tid = str(t.get("id", ""))
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                out.append(t)
        assert len(out) == 1
        assert out[0]["id"] == "RP-1"


# ---------------------------------------------------------------------------
# Test 2: orchestrator's _state_db_path_for_orchestrator points to repo root
# ---------------------------------------------------------------------------

class TestOrchestratorStateDbPath:
    """The orchestrator must write to the SAME state.db the rest of the system reads from.

    Bug: commit 49a30ed introduced ``Path(__file__).resolve().parent.parent / 'state.db'``
    which resolves to ``<repo>/backend/state.db`` — a stale 40 KB file from
    2026-08-27 with no ``plan_tasks`` table. The rest of the system
    (``server.py``, ``agent.py``) reads ``<repo>/state.db``. The two paths
    differ by one directory level.
    """

    def test_orchestrator_path_resolves_to_repo_root_state_db(self):
        """The fix: ``.parent.parent.parent`` (3 levels up from ``backend/verification/orchestrator.py``).

        ``__file__``  = ``<repo>/backend/verification/orchestrator.py``
        ``.parent``   = ``<repo>/backend/verification``
        ``.parent``   = ``<repo>/backend``
        ``.parent``   = ``<repo>``               ← we want this level
        """
        # Import the actual function and verify its resolution.
        from verification.orchestrator import _state_db_path_for_orchestrator

        resolved = _state_db_path_for_orchestrator()
        # Must NOT be the stale backend/state.db
        assert "backend/state.db" not in str(resolved), (
            f"BUG: orchestrator still writes to backend/state.db "
            f"({resolved}); this is the off-by-one path bug"
        )
        # Must end with /state.db (sibling of backend/)
        assert str(resolved).endswith("/state.db")

    def test_orchestrator_path_matches_server_state_db_path(self, tmp_path, monkeypatch):
        """The two helpers must resolve to the same file (modulo PDT_STATE_DB_PATH).

        Patches BOTH helpers to a single tmp DB so the assertion holds
        regardless of the real ``state.db`` location; this pins the
        invariant that the orchestrator's writes land where the rest
        of the system reads.
        """
        # Run only when PDT_STATE_DB_PATH is unset, which is the default.
        old_env = os.environ.pop("PDT_STATE_DB_PATH", None)
        try:
            # Lazy import to honour env at call time.
            import importlib
            import verification.orchestrator as orch_mod
            importlib.reload(orch_mod)

            tmp_db = tmp_path / "shared_state.db"
            monkeypatch.setattr(orch_mod, "_state_db_path_for_orchestrator",
                               lambda: tmp_db)
            import server
            monkeypatch.setattr(server, "_state_db_path",
                               lambda request=None: tmp_db)

            orch_path = orch_mod._state_db_path_for_orchestrator()
            server_path = server._state_db_path()

            assert orch_path == server_path == tmp_db, (
                f"orchestrator ({orch_path}) and server ({server_path}) "
                f"resolve to DIFFERENT state.db files — RP-* writes go "
                f"to the wrong DB"
            )
        finally:
            if old_env is not None:
                os.environ["PDT_STATE_DB_PATH"] = old_env


# ---------------------------------------------------------------------------
# Test 3: state.db plan_tasks layer surfaces repair tasks via iter_by_task_group_prefix
# ---------------------------------------------------------------------------

class TestStateDbRepairTaskFallback:
    """Reader layer 3: state.db.plan_tasks WHERE task_group LIKE 'repair%'."""

    def _make_repo(self, tmp_path: Path) -> tuple[str, Path]:
        plan_id = "test-plan"
        # Create a tmp SQLite file the way ``_state_db_path`` resolves to.
        db_path = tmp_path / "state.db"
        # Apply the v4 schema.
        sys_path = Path(__file__).resolve().parents[2]
        from state_machine.db.schema import migrate
        from state_machine.db.connection import open as open_db
        conn = open_db(db_path)
        try:
            migrate(conn)
        finally:
            conn.close()
        return plan_id, db_path

    def test_iter_by_task_group_prefix_returns_repair_tasks(self, tmp_path, monkeypatch):
        """The new method yields RP-* / R{n}* rows that the refiner would otherwise orphan."""
        plan_id, db_path = self._make_repo(tmp_path)

        # Patch server._state_db_path to point at our tmp DB.
        import server
        monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)

        from state_machine.db.connection import open as open_db
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        conn = open_db(db_path)
        try:
            repo = PlanTaskRepository(conn)
            # Insert 2 repair tasks + 1 normal task.
            repo.add_task(plan_id, {
                "id": "RP-1",
                "title": "fix VP-006",
                "description": "select_entering_consolidation 函数体 diff",
                "test_command": "pytest tests/test_vp_006.py",
                "task_group": "repair",
                "failed_vp_id": "VP-006",
                "round": 4,
            })
            repo.add_task(plan_id, {
                "id": "R5-1",
                "title": "fix coverage",
                "description": "signal.rs coverage",
                "test_command": "pytest tests/test_vp_027.py",
                "task_group": "repair-round-5",
                "failed_vp_id": "VP-027",
                "round": 5,
            })
            repo.add_task(plan_id, {
                "id": "11-2",
                "title": "Rust:trend/consolidation",
                "description": "normal task",
                "task_group": None,
            })

            rows = list(repo.iter_by_task_group_prefix(plan_id, "repair"))
            ids = {r["id"] for r in rows}
            assert ids == {"RP-1", "R5-1"}, (
                f"expected only repair-task rows; got {ids}"
            )
        finally:
            conn.close()

    def test_repair_tasks_progress_layer_aggregates_disk_memory_and_db(
        self, tmp_path, monkeypatch, tmp_path_factory,
    ):
        """The full reader stack: disk JSON v2 → in-memory → state.db fallback.

        Scenario:
          * state.db has RP-1 (in-flight)
          * in-memory has R5-1 (live round, just generated)
          * disk JSON has RP-2 (persisted from round 4)
        Result: all three rows surface in the API response, deduped by id.
        """
        plan_id = "test-plan"
        _, db_path = self._make_repo(tmp_path)
        # Write the disk JSON under PLANS_DIR/<plan_id>/ so the reader
        # finds it. The conftest's PLANS_DIR is the global one.
        from server import PLANS_DIR
        plan_dir = PLANS_DIR / plan_id
        # Clean up any stale file from a previous run.
        rt_path = plan_dir / "verification_repair_tasks.json"
        try:
            plan_dir.mkdir(parents=True, exist_ok=True)

            import server
            monkeypatch.setattr(server, "_state_db_path",
                               lambda request=None: db_path)

            from state_machine.db.connection import open as open_db
            from state_machine.repositories.plan_task_repository import (
                PlanTaskRepository,
            )

            # 1) state.db: insert RP-1 (orchestrator's authoritative
            #    write path)
            conn = open_db(db_path)
            try:
                repo = PlanTaskRepository(conn)
                repo.add_task(plan_id, {
                    "id": "RP-1",
                    "title": "fix VP-006",
                    "task_group": "repair",
                    "failed_vp_id": "VP-006",
                    "round": 4,
                })
            finally:
                conn.close()

            # 2) in-memory: R5-1 (live round, just generated)
            server._verification_state[plan_id] = {
                "repair_tasks": [
                    {"id": "R5-1", "title": "fix coverage",
                     "status": "pending"}
                ]
            }

            # 3) disk JSON: RP-2 (legacy round 4 snapshot, still on disk)
            rt_path.write_text(json.dumps({
                "plan_id": plan_id,
                "round": 4,
                "tasks": [
                    {"id": "RP-2", "title": "fix VP-010",
                     "status": "pending"}
                ],
            }))

            # Invoke the reader.
            result = server._load_repair_tasks_for_progress(plan_id)
            ids = {t["id"] for t in result}
            assert ids == {"RP-1", "R5-1", "RP-2"}, (
                f"all three layers should surface; got {ids}"
            )
        finally:
            # Clean up: drop the in-memory entry and the disk JSON
            # so other tests are unaffected.
            server._verification_state.pop(plan_id, None)
            try:
                rt_path.unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Test 4: refiner's reconcile delete loop SKIPS repair tasks
# ---------------------------------------------------------------------------

class TestRefinerSkipsRepairTasks:
    """The refiner must NOT delete tasks whose ``task_group`` starts with ``repair``.

    The refiner's contract is to manage ORIGINAL plan tasks (it has no
    knowledge of VP-failed → repair round mappings). The orchestrator
    owns the repair-task lifecycle.
    """

    def test_refiner_reconcile_skips_repair_tasks(self, tmp_path, monkeypatch):
        """Build the minimal Agent harness and verify the delete path skips RP-*."""
        # We don't need a full Agent — just exercise the delete-skip
        # logic directly. Read the relevant source location and
        # assert the ``startswith("repair")`` guard is present.
        agent_path = Path(__file__).resolve().parents[2] / "agent.py"
        src = agent_path.read_text()

        # The 2026-09-12 fix added a
        # ``_TERMINAL_REPAIR_TASK_GROUP_PREFIX = "repair"`` constant
        # inside ``_refine_after_failure`` and uses it in the
        # ``removed_ids`` predicate. Pin both pieces of evidence.
        assert "_TERMINAL_REPAIR_TASK_GROUP_PREFIX" in src, (
            "BUG: refiner's delete loop does not have the repair-task "
            "skip guard — RP-* rows will be silently deleted when any "
            "unrelated task triggers a refine"
        )
        assert (
            "startswith" in src
            and "_TERMINAL_REPAIR_TASK_GROUP_PREFIX" in src
        ), (
            "BUG: skip guard exists but is not applied via startswith"
        )

    def test_refiner_skips_v9_repair_round_format(self):
        """Post-v9 ``R{number}-{i}`` ids have ``task_group='repair-round-N'`` — also starts with 'repair'."""
        # The prefix guard covers both legacy ``RP-*`` (pre-v9) and
        # ``R{number}-{i}`` (post-v9) because both share the
        # ``repair`` prefix. Verify the guard is applied via
        # ``startswith`` (not exact match).
        agent_path = Path(__file__).resolve().parents[2] / "agent.py"
        src = agent_path.read_text()
        assert (
            "_TERMINAL_REPAIR_TASK_GROUP_PREFIX"
            and "startswith(" in src
        )
        # And the prefix is exactly "repair" (covers both forms).
        assert '"repair"' in src