"""
Plan State — Central Workflow State Manager
============================================

Tracks workflow progression across all phases including optional
architecture and test design phases. Single source of truth for plan state.

As of task #3.7, the per-plan state is persisted to the SQLite
``plan_routing`` table (columns ``current_phase`` / ``completed_phases``
/ ``review_rounds`` / ``flags`` / ``verification`` / ``last_updated``).
The legacy ``plan_state.json`` file is no longer written by
:class:`PlanState`; on first read of a plan whose file exists but
whose ``plan_routing`` row does not, the contents are migrated to
SQLite (one-shot, atomic) and the file is ignored thereafter.
"""

import hashlib
import importlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, List, Dict, Any

from framework import clock as _framework_clock
from config_paths import resolve_plans_dir, resolve_state_db_path




# Canonical filename for the per-plan state JSON. A single literal —
# no string-concat bypass. This is the contract pinned by
# backend/tests/test_plan_state_filename_constant.py.
PLAN_STATE_FILENAME: str = "plan_state.json"


def _state_db_path() -> Path:
    """Return the path of the state-machine SQLite file.

    Delegates to :func:`config_paths.resolve_state_db_path` so
    ``PlanState`` persists to exactly the database the API layer reads.
    It used to *mirror* ``server._state_db_path``'s two-step resolution
    instead of calling it, which is how ``watchdog`` and the
    verification orchestrator each drifted to a different file without
    anything failing loudly (see the resolver's docstring).
    """
    return resolve_state_db_path()

# Module-level singleton pointing at the project's ``plans/`` directory.
# Mirrors the ``PLANS_DIR`` constant in ``backend/server.py``. Defined
# here so the module-level reader helpers (``get_migration_audit``)
# have a stable anchor; tests may ``monkeypatch.setattr`` it to a
# ``tmp_path`` sandbox without touching the filesystem singleton.
#
# 2026-09-13: resolved via ``config_paths.resolve_plans_dir`` so an
# ``PDT_PLANS_DIR`` override (set by the test conftests) is honoured at
# import time. Tests that keep the module-level anchor MUST NOT create
# ``<repo>/plans/<fixture-id>/`` in the operator's live tree.
plans_dir: Path = resolve_plans_dir()

VALID_PHASES = [
    "interview",
    "interview_complete",
    "prd_generation",
    "prd_review",
    "prd_refining",
    "prd_approved",
    "arch_generation",
    "arch_review",
    "arch_refining",
    "arch_approved",
    "test_generation",
    "test_review",
    "test_refining",
    "test_approved",
    "preflight_review",
    "tasks_generation",
    "ready",
    # 2026-09-15: the auto-scheduling gate.
    #
    # ``ready`` means "tasks are generated and approved — a human decides
    # when to start" (the operator asks the assistant, the assistant
    # reports, the operator authorizes execution). ``queued`` is the
    # explicit hand-off to the scheduler: only plans in this phase are
    # auto-started, one at a time per workspace, so two plans sharing a
    # working tree can never run concurrently and clobber each other.
    #
    # Transition graph: ready → queued (operator queues it),
    # queued → ready (operator pulls it back out), queued → executing
    # (the scheduler starts it). ``ready → executing`` is unchanged, so
    # the manual path still works.
    "queued",
    "executing",
    "verification",
    "verification_running",
    "verify_first_pass",
    "verify_recheck",
    "verification_passed",
    "verification_failed",
    "verification_repairing",
    "verification_rerunning",
    "verification_loop_stopped",
    "completed",
    "failed",
    "stopped",
]

TERMINAL_PHASES = {"completed", "failed", "stopped"}

# 2026-09-17 (schema v5): the ``_PLAN_PHASE_TO_ROUTING_STAGE``
# translation table that used to live here is DELETED, along with the
# ``plan_routing.stage`` column it fed. ``plan_routing`` now carries
# exactly one workflow-state column — ``current_phase`` — so there is no
# second vocabulary to translate into.
#
# The table existed because ``stage`` and ``current_phase`` were
# different words for the same thing (``ready`` vs ``tasks_ready``,
# ``queued`` vs ``tasks_queued``, ``completed`` vs ``terminal_done``),
# and only ONE writer remembered to apply it. The ~23 raw
# ``UPDATE plan_routing SET stage`` sites wrote phase vocabulary
# straight into the stage column, so a single row could hold either
# vocabulary depending on which writer touched it last. On top of that
# the mapping was lossy — ``failed`` / ``stopped`` /
# ``verification_failed`` / ``verification_loop_stopped`` all collapsed
# to ``terminal_failed`` — which is why ``stage`` could never have been
# the surviving column.
#
# The scheduler's two stage-valued predicates are now phase-valued
# (see ``state_machine.services.scheduler_support`` and
# ``notifications.plan_dir_resolver``). If you find yourself wanting to
# add a translation table back, that is the signal you are about to
# re-create a second state column.

PHASE_TRANSITIONS = {
    "interview": ["interview_complete"],
    "interview_complete": ["prd_generation"],
    "prd_generation": ["prd_review"],
    "prd_review": ["prd_refining", "prd_approved"],
    "prd_refining": ["prd_review"],
    "prd_approved": ["prd_refining", "arch_generation", "test_generation", "preflight_review", "tasks_generation", "executing"],
    "arch_generation": ["arch_review"],
    "arch_review": ["arch_refining", "arch_approved"],
    "arch_refining": ["arch_review"],
    # Backward edge: when the user adds a new decision point after a
    # phase was already approved, we roll the plan back to the matching
    # ``*_review`` phase so the new content goes through the same
    # review-correction loop. Mirrors ``prd_approved -> prd_refining``
    # / ``test_approved -> test_refining`` below.
    "arch_approved": ["arch_refining", "test_generation", "preflight_review", "tasks_generation", "executing"],
    "test_generation": ["test_review", "test_approved"],
    "test_review": ["test_refining", "test_approved"],
    "test_refining": ["test_review"],
    "test_approved": ["test_refining", "preflight_review", "tasks_generation", "executing"],
    "preflight_review": ["tasks_generation", "prd_review", "arch_review", "test_review"],
    "tasks_generation": ["ready", "queued", "executing"],
    "ready": ["executing", "queued"],
    # 2026-09-15: ``queued`` is the auto-scheduler's inbox. The operator
    # can pull a plan back to ``ready`` (and take it off the queue), or
    # the scheduler/operator can start it directly.
    "queued": ["ready", "executing"],
    "executing": ["completed", "failed", "stopped", "ready"],
    "completed": ["verification"],
    "failed": ["executing", "ready"],
    "stopped": ["executing", "ready"],
}

VERIFICATION_PHASE_TRANSITIONS = {
    "ready": ["verification", "verification_running"],
    "executing": ["verification", "completed", "failed", "stopped", "ready"],
    "verification": ["verification_running", "verification_passed", "verification_failed", "verification_loop_stopped"],
    "verification_running": ["verify_first_pass", "verification_passed", "verification_failed"],
    "verify_first_pass": ["verify_recheck", "verification_passed", "verification_failed", "verification_loop_stopped"],
    "verify_recheck": ["verification_passed", "verification_failed", "verification_loop_stopped"],
    "verification_passed": [],  # terminal: security boundary — no forward edge to ``completed``
    "verification_failed": ["verification_repairing", "verification_rerunning", "verification_passed", "verification_loop_stopped"],
    "verification_repairing": ["verification", "verification_rerunning", "verification_passed", "verification_loop_stopped", "verification_failed", "executing"],
    "verification_rerunning": ["verification_running", "verification_passed", "verification_failed", "verification_loop_stopped"],
    "verification_loop_stopped": ["failed", "verification_passed"],

    # ``completed`` is NOT a dead end for the verification sub-machine.
    # Execution ends in ``completed``; the verification round that follows
    # must be able to converge on a terminal verdict from there. Without
    # these two edges a passing OR failing verification report raises
    # ``ValueError`` in ``transition_to`` and the plan is stranded.
    # Note: ``completed -> completed`` stays illegal (self-loops are
    # handled as an idempotent no-op earlier in ``transition_to``, and the
    # table itself must not declare one).
    "completed": ["verification_passed", "verification_failed"],
    "failed": ["executing", "ready"],
    "stopped": ["executing", "ready"],
}


class PlanState:
    """Central plan state manager for the spec-driven development workflow."""

    def __init__(self, plan_dir: Path):
        self.plan_dir = Path(plan_dir)
        self.state_file = self.plan_dir / PLAN_STATE_FILENAME
        self._lock = threading.Lock()
        #: The phase we last saw ON DISK. ``_save_state_to_sqlite`` writes
        #: ``current_phase`` ONLY when the in-memory value differs from
        #: this — see the note on that method. A ``PlanState`` caches the
        #: row it loaded, so a metadata-only write must not re-state a
        #: phase the cache may be stale on.
        #:
        #: MUST be initialised BEFORE ``_load_state()``: the legacy-file
        #: migration branch at the bottom of that method calls
        #: ``_save_state_to_sqlite``, and if the attribute does not exist
        #: yet the AttributeError is swallowed by that branch's blanket
        #: ``except Exception`` — the migration silently never happens and
        #: the routing row is never created. ``None`` is also the correct
        #: starting value for the legacy/default branches: nothing is on
        #: disk yet, so the first save really does own the column.
        self._persisted_phase: Optional[str] = None
        self._state = self._load_state()
        # The SQLite branch has already put the on-disk value in
        # ``_state``; the legacy/default branches just wrote it. Either
        # way the cache and disk now agree.
        self._persisted_phase = self._state.get("current_phase")

    def _load_state(self) -> dict:
        # task #3.7: read from ``plan_routing`` first; fall back to
        # the legacy ``plan_state.json`` file (and migrate it) only
        # when no SQLite row exists. SQLite wins thereafter.
        sqlite_row = self._load_state_from_sqlite()
        if sqlite_row is not None:
            return sqlite_row
        legacy_row = self._load_state_from_legacy_file()
        if legacy_row is None:
            legacy_row = self._default_state()
        # One-shot migration: persist the legacy row to SQLite so
        # subsequent reads short-circuit at the SQLite branch.
        try:
            self._save_state_to_sqlite(legacy_row)
        except Exception:  # noqa: BLE001
            # Migration failures must not break state reads; the
            # next ``_save_state`` will retry the migration.
            pass
        return legacy_row

    def reload(self) -> None:
        """Re-read the plan state from the authoritative SQLite store.

        Why this exists (2026-08-19 audit): the verification
        orchestrator, the ``/start`` handler, and the auto-loop each
        construct their own ``PlanState`` instance. When any of them
        calls ``transition_to``, the change is persisted to SQLite
        but the OTHER instances' in-memory ``_state`` cache is stale.
        Without a reload, a downstream ``transition_to`` (e.g. on
        the verification pass path) raises ``Illegal transition``
        because the cached ``current_phase`` is out of date, and the
        blanket ``except Exception`` in the callers silently swallows
        the failure — leaving the plan stranded in
        ``verification_running`` with no observable error.

        Callers must invoke ``reload()`` BEFORE any ``transition_to``
        that depends on the latest persisted state.
        """
        fresh = self._load_state()
        with self._lock:
            self._state = fresh
            # Re-sync the write guard: after a reload the cache agrees
            # with disk by construction, so the next metadata write is
            # a no-op for the phase column.
            self._persisted_phase = fresh.get("current_phase")

    def _load_state_from_sqlite(self) -> Optional[dict]:
        """Read the ``plan_routing`` row for this plan_id.

        Returns ``None`` when SQLite is unavailable, the table is
        missing, or no row exists for this plan (e.g. fresh plan
        created before the row was seeded by ``begin_*`` callers).
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )
        except ImportError:
            return None
        try:
            conn = _open_db(_state_db_path())
        except Exception:  # noqa: BLE001
            return None
        try:
            _migrate(conn)
        except Exception:  # noqa: BLE001
            conn.close()
            return None
        try:
            repo = RoutingRepository(conn)
            row = repo.current(self.plan_dir.name)
        except Exception:  # noqa: BLE001
            row = None
        finally:
            conn.close()
        if row is None:
            return None
        # Map SQLite columns back to the legacy ``_state`` shape.
        return self._sqlite_row_to_state(row)

    @staticmethod
    def _sqlite_row_to_state(row: dict) -> dict:
        """Translate a ``plan_routing`` row into the legacy
        ``PlanState._state`` dict shape so getters/setters keep
        working unchanged.
        """
        def _decode_json(value, default):
            if value is None:
                return default
            try:
                import json as _json
                return _json.loads(value)
            except (TypeError, ValueError):
                return default
        return {
            "plan_id": row.get("plan_id"),
            "current_phase": row.get("current_phase") or "interview",
            "completed_phases": _decode_json(row.get("completed_phases"), []),
            "review_rounds": _decode_json(row.get("review_rounds"), {"prd": 0, "arch": 0, "test": 0}),
            "flags": _decode_json(row.get("flags"), {}),
            "verification": _decode_json(row.get("verification"), {
                "status": "pending", "round": 0, "max_rounds": 3, "stop_reason": None,
            }),
            "last_updated": row.get("last_updated") or row.get("updated_at"),
        }

    def _load_state_from_legacy_file(self) -> Optional[dict]:
        """Read the legacy ``plan_state.json`` file (pre-task #3.7)."""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return self._default_state()
        completed_phases = data.get("completed_phases") or []
        review_rounds = data.get("review_rounds") or {"prd": 0, "arch": 0, "test": 0}
        flags = data.get("flags") or {}
        current_phase = data.get("current_phase")
        if current_phase is None or current_phase not in VALID_PHASES:
            current_phase = "interview"
        verification = data.get("verification", {}) or {}
        return {
            "plan_id": data.get("plan_id", self.plan_dir.name),
            "current_phase": current_phase,
            "completed_phases": completed_phases,
            "review_rounds": review_rounds,
            "flags": flags,
            "verification": {
                "status": verification.get("status", "pending"),
                "round": verification.get("round", 0),
                "max_rounds": verification.get("max_rounds", 3),
                "stop_reason": verification.get("stop_reason", None),
            },
            "last_updated": data.get("last_updated", datetime.utcnow().isoformat() + "Z"),
        }

    def _default_state(self) -> dict:
        phase = self._infer_phase()
        return {
            "plan_id": self.plan_dir.name,
            "current_phase": phase,
            "completed_phases": [],
            "review_rounds": {
                "prd": 0,
                "arch": 0,
                "test": 0,
            },
            "flags": {
                "arch_enabled": (self.plan_dir / "arch-design.md").exists(),
                "test_enabled": (self.plan_dir / "test-design.md").exists(),
                "preflight_enabled": True,
                "self_review_enabled": True,
            },
            "verification": {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
            "last_updated": datetime.utcnow().isoformat() + "Z",
        }

    def _infer_phase(self) -> str:
        if (self.plan_dir / "tasks.json").exists():
            return "ready"
        if (self.plan_dir / "test-design.md").exists():
            return "test_approved"
        if (self.plan_dir / "arch-design.md").exists():
            return "arch_approved"
        if (self.plan_dir / "review.json").exists() and self._is_prd_review_complete():
            return "prd_approved"
        if (self.plan_dir / "prd.json").exists() or (self.plan_dir / "prd.md").exists():
            return "prd_review"
        if (self.plan_dir / "interview.json").exists():
            return "interview_complete"
        return "interview"

    def _is_prd_review_complete(self) -> bool:
        """
        Check whether every PRD decision point has been accepted in review.json.

        Returns:
            True if all decision_points in prd.json have a corresponding
            review.json item with status "accepted" (or if prd.json has
            zero decision points, for backward compatibility).
        """
        prd_file = self.plan_dir / "prd.json"
        if not prd_file.exists():
            return True

        try:
            with open(prd_file, "r", encoding="utf-8") as f:
                prd_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False

        decision_points = prd_data.get("decision_points", []) or []
        if not decision_points:
            return True

        review_file = self.plan_dir / "review.json"
        if not review_file.exists():
            return False

        try:
            with open(review_file, "r", encoding="utf-8") as f:
                review_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False

        review_items = review_data.get("items", []) or []
        accepted_indices = {
            item.get("index")
            for item in review_items
            if item.get("status") == "accepted"
        }
        required_indices = {dp.get("index") for dp in decision_points}
        return required_indices.issubset(accepted_indices)

    def _save_state(self):
        # task #3.7: persist to ``plan_routing`` instead of writing
        # the legacy ``plan_state.json`` file. Falls back to no-op
        # when SQLite is unavailable (the legacy file is preserved
        # as a read-only cache and will be picked up by ``_load_state``
        # on the next access).
        self._state["last_updated"] = datetime.utcnow().isoformat() + "Z"
        self._save_state_to_sqlite(self._state)

    def _save_state_to_sqlite(self, state: dict) -> None:
        """Write the plan-state fields into ``plan_routing``.

        Delegates the column ownership to
        :meth:`RoutingRepository.write_plan_state`, which touches ONLY
        the ``PlanState``-owned columns and leaves the CAS's
        ``version`` / ``substage`` alone.

        The phase column is written **only when this instance actually
        moved it** — i.e. when the in-memory value differs from the one
        we last saw on disk (:attr:`_persisted_phase`).  That guard is
        the load-bearing part.

        ``PlanState`` caches the row it loaded, and every setter
        (``set_verification_max_rounds``, ``enable_arch``, …) funnels
        through :meth:`_save_state`, so without the guard a harmless
        metadata write would re-state a *cached* phase — silently undoing
        a phase another writer had CAS'd in the meantime.  That is not
        hypothetical: ``POST /api/verification/{id}/reset_rounds`` ends
        with ``set_verification_max_rounds``, which used to snap the
        routing value back and make the very next ``/start`` answer
        ``409 stage_mismatch`` (reproduced twice on a production plan).

        Before schema v5 the protection lived in the *caller* as a
        ~30-line special case that preserved an existing ``stage`` when
        ``current_phase`` was unchanged — because ``stage`` was the other
        column and this class owned only ``current_phase``.  With one
        column the guard has to be here, and stated as the honest rule:
        write the phase when you changed it, otherwise leave it alone.
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )
        except ImportError:
            return
        try:
            conn = _open_db(_state_db_path())
        except Exception:  # noqa: BLE001
            return
        current_phase = state.get("current_phase", "interview")
        # ``None`` tells the repository to leave the column untouched.
        phase_arg = (
            current_phase if current_phase != self._persisted_phase else None
        )
        try:
            _migrate(conn)
            RoutingRepository(conn).write_plan_state(
                self.plan_dir.name,
                phase=phase_arg,
                completed_phases=state.get("completed_phases", []),
                review_rounds=state.get("review_rounds", {}),
                flags=state.get("flags", {}),
                verification=state.get("verification", {}),
                last_updated=state.get("last_updated"),
            )
            self._persisted_phase = current_phase
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()

    # --- Getters ---

    def get_state(self) -> dict:
        return dict(self._state)

    def get_current_phase(self) -> str:
        return self._state.get("current_phase", "interview")

    def get_completed_phases(self) -> List[str]:
        return list(self._state.get("completed_phases", []))

    def get_review_round(self, phase: str) -> int:
        return self._state.get("review_rounds", {}).get(phase, 0)

    def is_arch_enabled(self) -> bool:
        return self._state.get("flags", {}).get("arch_enabled", False)

    def is_test_enabled(self) -> bool:
        return self._state.get("flags", {}).get("test_enabled", False)

    def is_preflight_enabled(self) -> bool:
        """Return whether the preflight cross-doc check runs before tasks gen.

        Defaults to ``True`` for new plans (DP2 contract). Existing plans
        that pre-date the flag also see ``True`` because the field is
        populated by ``_default_state`` and is read with ``get(..., True)``
        to be robust to partial JSON written by older callers.
        """
        return self._state.get("flags", {}).get("preflight_enabled", True)

    def is_self_review_enabled(self) -> bool:
        """Return whether the post-emit self-review audit trail runs.

        Defaults to ``True`` for new plans (DP1 contract). Plans
        written before the flag landed (``plan-state sidecar`` missing
        ``flags.self_review_enabled``) also see ``True`` so the
        audit trail keeps flowing without manual migration.
        """
        return self._state.get("flags", {}).get("self_review_enabled", True)

    # --- Phase Transitions ---

    def force_set_phase(self, phase: str):
        """Set phase directly, bypassing transition rules.

        Only for crash recovery (_mark_failed_dead) where the server
        crashed between writing execution-state sidecar and transitioning plan_state.
        """
        if phase not in VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}. Must be one of {VALID_PHASES}")
        current = self._state.get("current_phase", "")
        if current and current not in self._state["completed_phases"]:
            self._state["completed_phases"].append(current)
        self._state["current_phase"] = phase
        self._save_state()

    def transition_to(self, phase: str):
        if phase not in VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}. Must be one of {VALID_PHASES}")

        current = self._state.get("current_phase", "")

        # Idempotent: same phase is a no-op
        if current == phase:
            return

        # Validate transition legality (check both main and verification tables)
        allowed = set(PHASE_TRANSITIONS.get(current, []))
        allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(current, []))
        if current and phase not in allowed:
            raise ValueError(
                f"Illegal transition from '{current}' to '{phase}'. "
                f"Allowed: {sorted(allowed)}"
            )

        # Enforce execution completion before verification
        # Special case: allow transition from 'executing' directly to verification phases
        if phase.startswith("verification") and current != "executing" and "execution" not in self._state.get("completed_phases", []):
            raise ValueError(
                f"Cannot transition to '{phase}' without 'execution' in completed_phases. "
                f"Current completed_phases: {self._state.get('completed_phases', [])}"
            )

        if current and current not in self._state["completed_phases"]:
            self._state["completed_phases"].append(current)

        self._state["current_phase"] = phase

        # Auto-append 'execution' to completed_phases when leaving 'executing' for verification (idempotent)
        if current == "executing" and phase.startswith("verification") and "execution" not in self._state["completed_phases"]:
            self._state["completed_phases"].append("execution")

        # Auto-append 'execution' to completed_phases for terminal states (idempotent)
        if phase in TERMINAL_PHASES and "execution" not in self._state["completed_phases"]:
            self._state["completed_phases"].append("execution")

        # Update verification status when transitioning to verification phases
        if phase == "verification":
            self._state["verification"]["status"] = "pending"
        elif phase == "verification_running":
            self._state["verification"]["status"] = "running"
            # Do NOT auto-increment the round here. ``verification_round``
            # is the number of completed rounds (0-based). It is bumped
            # only by ``start_verification_rerun`` when the user confirms
            # repair tasks and the orchestrator moves to the next round.
            # Auto-incrementing on entry would break the 0-based contract
            # expected by ``VerificationOrchestrator`` tests.
        elif phase == "verify_first_pass":
            self._state["verification"]["status"] = "running"
        elif phase == "verify_recheck":
            self._state["verification"]["status"] = "running"
        elif phase == "verification_passed":
            self._state["verification"]["status"] = "passed"
        elif phase == "verification_failed":
            self._state["verification"]["status"] = "failed"
        elif phase == "verification_loop_stopped":
            self._state["verification"]["status"] = "loop_stopped"

        self._save_state()

    def mark_phase_complete(self, phase: str):
        if phase not in self._state["completed_phases"]:
            self._state["completed_phases"].append(phase)
        self._save_state()

    def reset_to(self, phase: str):
        """Reset state backward to a given phase, removing all subsequent progress."""
        if phase not in VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}")

        phase_order = [
            "interview", "interview_complete", "prd_generation",
            "prd_review", "prd_refining", "prd_approved",
            "arch_generation", "arch_review", "arch_refining", "arch_approved",
            "test_generation", "test_review", "test_refining", "test_approved",
            "tasks_generation", "ready", "executing",
        ]
        phase_idx = phase_order.index(phase)

        # Keep only completed phases up to (not including) the target
        self._state["completed_phases"] = [
            p for p in self._state["completed_phases"]
            if p in phase_order and phase_order.index(p) < phase_idx
        ]

        self._state["current_phase"] = phase
        self._save_state()

    # --- Review Rounds ---

    def increment_review_round(self, phase: str):
        rounds = self._state.get("review_rounds", {})
        rounds[phase] = rounds.get(phase, 0) + 1
        self._state["review_rounds"] = rounds
        self._save_state()

    def reset_review_round(self, phase: str):
        rounds = self._state.get("review_rounds", {})
        rounds[phase] = 0
        self._state["review_rounds"] = rounds
        self._save_state()

    # --- Flags ---

    def enable_arch(self, enabled: bool = True):
        self._state["flags"]["arch_enabled"] = enabled
        self._save_state()

    def enable_test(self, enabled: bool = True):
        self._state["flags"]["test_enabled"] = enabled
        self._save_state()

    def enable_preflight(self, enabled: bool = True):
        """Toggle the preflight gate (DP2). When False, tasks generation
        skips the cross-document reviewer entirely.
        """
        self._state["flags"]["preflight_enabled"] = enabled
        self._save_state()

    def enable_self_review(self, enabled: bool = True):
        """Toggle the post-emit self-review audit trail (DP1). When False,
        the prd/arch/test generators skip ``run_doc_self_review``, do NOT
        write ``{doc_type}_self_review.json`` audit-trail reports, and
        do NOT emit the corresponding ``prd_self_review`` /
        ``arch_self_review`` / ``test_self_review`` log events.
        """
        self._state["flags"]["self_review_enabled"] = enabled
        self._save_state()

    # --- Convenience Helpers ---

    def is_interview_complete(self) -> bool:
        return self.get_current_phase() in [
            "interview_complete", "prd_generation", "prd_review",
            "prd_refining", "prd_approved", "arch_generation",
            "arch_review", "arch_refining", "arch_approved",
            "test_generation", "test_review", "test_refining",
            "test_approved", "tasks_generation", "ready", "executing",
        ]

    def is_prd_approved(self) -> bool:
        return self.get_current_phase() in [
            "prd_approved", "arch_generation", "arch_review",
            "arch_refining", "arch_approved", "test_generation",
            "test_review", "test_refining", "test_approved",
            "tasks_generation", "ready", "executing",
        ]

    def is_arch_approved(self) -> bool:
        return self.get_current_phase() in [
            "arch_approved", "test_generation", "test_review",
            "test_refining", "test_approved", "tasks_generation",
            "ready", "executing",
        ]

    def is_test_approved(self) -> bool:
        return self.get_current_phase() in [
            "test_approved", "tasks_generation", "ready", "executing",
        ]

    def is_ready_for_tasks(self) -> bool:
        current = self.get_current_phase()
        if current == "prd_approved" and not self.is_arch_enabled():
            return True
        if current == "arch_approved" and not self.is_test_enabled():
            return True
        if current == "test_approved":
            return True
        return False

    def can_enable_arch(self) -> bool:
        return self.is_prd_approved()

    def can_enable_test(self) -> bool:
        return self.is_arch_approved() or (
            self.is_arch_enabled() and self.get_current_phase() == "arch_approved"
        )

    # --- Verification Phase Methods ---

    def get_verification_status(self) -> str:
        """Get current verification status (pending/running/passed/failed/loop_stopped)."""
        return self._state.get("verification", {}).get("status", "pending")

    def get_verification_round(self) -> int:
        """Get current verification round number."""
        return self._state.get("verification", {}).get("round", 0)

    def get_verification_max_rounds(self) -> int:
        """Get maximum verification rounds allowed."""
        return self._state.get("verification", {}).get("max_rounds", 3)

    def get_verification_stop_reason(self) -> Optional[str]:
        """Get the reason why verification stopped."""
        return self._state.get("verification", {}).get("stop_reason")

    def start_verification(self):
        """Start verification phase after execution completes."""
        if "execution" not in self._state.get("completed_phases", []):
            raise ValueError("Cannot start verification: 'execution' not in completed_phases")
        self.transition_to("verification")

    def verification_passed(self):
        """Mark verification as passed."""
        self.transition_to("verification_passed")

    def verification_failed(self):
        """Mark verification as failed (triggers repair or rerun)."""
        self.transition_to("verification_failed")

    def increment_verification_round(self):
        """Increment verification round counter."""
        self._state["verification"]["round"] = self.get_verification_round() + 1
        self._save_state()

    def start_verification_repair(self):
        """Transition to repair tasks phase after verification failure."""
        current = self.get_current_phase()
        if current != "verification_failed":
            raise ValueError(
                f"Cannot start repair from '{current}'. "
                f"Must be in 'verification_failed' phase."
            )
        self.transition_to("verification_repairing")

    def start_verification_rerun(self):
        """Transition to rerun verification after repair."""
        # Increment round before rerunning
        self.increment_verification_round()

        # Check if max rounds reached (stop if round >= max_rounds)
        if self.get_verification_round() >= self.get_verification_max_rounds():
            self.stop_verification_loop("max_rounds_reached")
            return

        self.transition_to("verification_rerunning")

    def start_repair_execution(self):
        """Transition from ``verification_repairing`` into ``executing``.

        2026-09-12: the state machine is a closed loop — verification
        failure flows through
        ``verification_repairing → executing → verification →
        verification_running`` rather than the shortcut
        ``verification_repairing → verification_rerunning`` which
        bypasses the ``executing`` phase. The vocabulary now matches
        reality: the executor subprocess running RP-* tasks IS in
        ``executing``, not still inside the verification sub-machine.

        The corresponding edge is already declared in
        ``VERIFICATION_PHASE_TRANSITIONS["verification_repairing"]``
        (the only "verification_*" source that may transition to
        ``executing``), but no method previously invoked it.

        Side effects:
          * Increment ``verification.round`` so the post-repair
            verification cycle starts on round N+1, not the same N.
            This mirrors ``start_verification_rerun``'s
            ``increment_verification_round`` so the round counter
            monotonically increases across all path types.
        """
        current = self.get_current_phase()
        if current != "verification_repairing":
            raise ValueError(
                f"Cannot start repair execution from '{current}'. "
                f"Must be in 'verification_repairing' phase."
            )
        # Increment round counter first so post-repair verification
        # cycle starts on the next round number (mirrors
        # ``start_verification_rerun``'s increment_verification_round).
        self.increment_verification_round()
        self.transition_to("executing")

    def stop_verification_loop(self, reason: str):
        """
        Stop verification loop with a reason.

        Args:
            reason: One of 'max_rounds_reached', 'user_stopped',
                'same_failure_repeated',
                'same_failure_repeated_after_max_attempts' (2026-09-12: used when the consecutive-same-failure cap fires
                after MAX attempts),
                'repair_execution_failed', 'exception'.
        """
        valid_reasons = {
            "max_rounds_reached", "user_stopped",
            "same_failure_repeated",
            "same_failure_repeated_after_max_attempts",
            "repair_execution_failed", "exception",
        }
        if reason not in valid_reasons:
            raise ValueError(f"Invalid stop reason: {reason}. Must be one of {valid_reasons}")

        self._state["verification"]["stop_reason"] = reason
        self.transition_to("verification_loop_stopped")

    def can_rerun_verification(self) -> bool:
        """Check if verification can be rerun (round < max_rounds)."""
        return self.get_verification_round() < self.get_verification_max_rounds()

    def is_verification_complete(self) -> bool:
        """Check if verification completed successfully."""
        return self.get_verification_status() == "passed"

    def is_verification_loop_stopped(self) -> bool:
        """Check if verification loop stopped (max rounds or user stopped)."""
        return self.get_verification_status() == "loop_stopped"

    def set_verification_max_rounds(self, max_rounds: int):
        """Set maximum verification rounds allowed."""
        self._state["verification"]["max_rounds"] = max_rounds
        self._save_state()

    def set_verification_status(
        self,
        status: str,
        stop_reason: Optional[str] = None,
        round_n: Optional[int] = None,
    ) -> None:
        """Update verification sub-state (status / stop_reason / round).

        Single source of truth for the ``verification`` JSON column on
        ``plan_routing``. Replaces the previous direct-SQL writer
        ``_update_plan_state_to_terminal`` so every ``verification``
        mutation goes through :meth:`_save_state` (atomic with the
        ``current_phase`` update).

        Args:
            status: One of ``"pending"``, ``"running"``, ``"passed"``,
                ``"failed"``, ``"loop_stopped"``. Mirrors the SQLite
                ``plan_verification.verification_status`` enum.
            stop_reason: Optional reason for stopping the verification
                loop (``"max_rounds_reached"``, ``"user_stopped"``,
                ``"same_failure_repeated"``, ``"no_repair_tasks"``,
                etc.). Pass ``None`` to leave the existing value alone.
            round_n: Optional round number to stamp. Pass ``None`` to
                leave the existing value alone.

        Why this exists (2026-09-07 plan): the previous
        ``_update_plan_state_to_terminal`` helper wrote the
        ``verification`` column directly via a side SQLite UPDATE, in
        parallel with ``PlanState.transition_to`` writing
        ``current_phase`` to the same row. Last-writer-wins ordering
        meant the verification_status could race against phase
        transitions; this method funnels both writes through the
        single ``_save_state`` call, with the same atomicity guarantee
        as a phase transition.
        """
        v = self._state.setdefault("verification", {})
        v["status"] = status
        if stop_reason is not None:
            v["stop_reason"] = stop_reason
        if round_n is not None:
            v["round"] = round_n
        self._save_state()

    def begin_verification(self, round_n: Optional[int] = None) -> None:
        """Route the plan into the verification phase from any source.

        Audit 2026-07-16: ``completed`` is in ``TERMINAL_PHASES``, so
        ``transition_to("verification")`` refused to fire when the
        executor started a verification cycle on a post-execution
        plan — leaving ``plan-state sidecar``'s ``verification.status``
        permanently stuck at ``"pending"``.

        ``begin_verification`` mirrors the side-effects of
        ``transition_to("verification")`` but skips the
        ``TERMINAL_PHASES`` guard, because routing a completed / failed
        / stopped plan back into verification is a legitimate
        re-entry point. If the plan is already in any verification
        state, the call is a no-op so a resumed round (e.g. a
        crashed-then-restarted thread) doesn't clobber an in-flight
        verification.

        2026-09-12 (state machine closed-loop fix): added
        optional ``round_n`` parameter so the post-repair callback
        can preserve the bumped round (the executor completed round N;
        the next verification must start at round N+1, not round 0).
        Behavior unchanged when ``round_n=None`` (default).
        """
        current = self._state["current_phase"]
        verification_states = {
            "verification", "verification_running", "verify_first_pass",
            "verify_recheck", "verification_repairing",
            "verification_rerunning", "verification_passed",
            "verification_failed", "verification_loop_stopped",
        }
        if current in verification_states:
            # Idempotent: plan is already in a verification state.
            return
        # Append the source phase to completed_phases so PHASE_TRANSITIONS
        # semantics stay consistent (transition_to does this too).
        if current and current not in self._state["completed_phases"]:
            self._state["completed_phases"].append(current)
        # Re-entry from ``executing`` also marks ``execution`` as
        # completed so downstream phase-routing checks stay coherent.
        if current == "executing" and "execution" not in self._state["completed_phases"]:
            self._state["completed_phases"].append("execution")
        self._state["current_phase"] = "verification"
        # Reset round counter and re-initialize status so a new run
        # starts from a clean slate (the executor's first-round
        # increment_rev brings round to 1).
        # 2026-09-12: when round_n is provided (post-repair
        # callback), preserve the bumped value instead of clobbering
        # to 0. Without this, the executor's RP-* run for round N
        # would be followed by a verification round that thinks it is
        # round 0 — destroying the round counter's monotonic
        # invariant.
        self._state["verification"]["status"] = "pending"
        self._state["verification"]["round"] = (
            round_n if round_n is not None else 0
        )
        self._state["verification"]["stop_reason"] = None
        self._state["last_updated"] = datetime.utcnow().isoformat() + "Z"
        self._save_state()


# ---------------------------------------------------------------------------
# Module-level reader helpers
# ---------------------------------------------------------------------------


def get_migration_audit(plan_id: str) -> Optional[Dict[str, Any]]:
    """Read the top-level ``migration_audit`` field from a plan_state.json.

    Returns the field's value (a dict) when present, or ``None`` when
    the field is absent. NEVER raises ``KeyError`` — the legacy
    pre-retrofit plan_state.json files do not carry the field, and
    crashing on them would make every historical plan unreadable.

    Contract pinned by ``tests/test_plan_state_migration_audit.py``:

      * Missing field -> ``None`` (not ``KeyError``).
      * Partial sub-fields (e.g. only ``timestamp``) -> returns the
        partial dict verbatim. Sub-field schema validation is the
        writer's responsibility, not the reader's.
      * Reads from the module-level ``plans_dir`` singleton, so tests
        can ``monkeypatch.setattr(plan_state, "plans_dir", tmp_path)``
        for hermetic fixtures.

    The reader deliberately tolerates the legacy file shape. New
    writers (the migration-audit appender) emit a full four-sub-field
    dict; older writers emit no field at all. Both shapes must round-
    trip through this reader without raising.
    """
    plan_state_path = plans_dir / plan_id / PLAN_STATE_FILENAME
    with open(plan_state_path, "r", encoding="utf-8") as _f:
        data = json.load(_f)
    # ``dict.get`` returns None for the missing-key case — this is
    # the entire reason the reader cannot be replaced with
    # ``data["migration_audit"]`` (which raises ``KeyError`` on legacy
    # plans and breaks the historical-compat contract).
    return data.get("migration_audit")


# ---------------------------------------------------------------------------
# Migration-audit schema + writer (DP7 part (b))
# ---------------------------------------------------------------------------


#: JSON-schema-flavoured declaration for the ``migration_audit`` top-level
#: field. The shape is locked at four contract-pinned keys:
#:
#:   - ``migrated``     : bool          — flag set when the migration ran
#:   - ``migrated_at``  : ISO-8601 str  — UTC timestamp (from framework.clock)
#:   - ``migrator``     : str           — subsystem that recorded the audit
#:   - ``source_hash``  : sha256 hex    — fingerprint of the migrated source
#:
#: ``tasks_generator`` and ``preflight_review`` import this same object
#: (no per-module copy) so a refactor of the contract propagates to all
#: three call sites in lock-step. ``tests/unit/test_m6_cleanup.py`` and
#: ``tests/integration/test_migration_audit_schema.py`` assert the
#: ``is`` identity and the JSON-type correctness.
MIGRATION_AUDIT_SCHEMA: Dict[str, Dict[str, str]] = {
    "migrated":    {"type": "boolean"},
    "migrated_at": {"type": "string", "format": "iso8601"},
    "migrator":    {"type": "string"},
    "source_hash": {"type": "string", "format": "sha256-hex"},
}


def _read_plan_state_json(plan_state_path: Path) -> Dict[str, Any]:
    """Read + parse the plan_state.json, returning a fresh dict."""
    with open(plan_state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_plan_state_json(plan_state_path: Path, data: Dict[str, Any]) -> None:
    """Atomic write of plan_state.json (tmp + os.replace)."""
    plan_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = plan_state_path.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, plan_state_path)


def _hash_payload(source_payload: bytes) -> str:
    """Return the lowercase sha256 hex digest of ``source_payload``."""
    return hashlib.sha256(source_payload).hexdigest()


class MigrationAudit:
    """Top-level operations on ``plan_state.json["migration_audit"]``.

    Architecture decision point 7 part (b) specifies the contract:

      * ``record(...)`` writes a four-key audit record. When the
        record is already present, the call is a no-op (idempotent
        at the key level). This protects ``source_hash`` from
        drift across retry loops.
      * ``backfill(...)`` walks every ``plans/20260805-*`` plan
        directory under ``plans_dir`` and calls ``record(...)`` on
        each — also idempotent. Plans that already carry the
        field are untouched.
      * The timestamp source is ``framework.clock.utcnow_iso()``
        (architecture decision point 5) — never ``datetime.now()``.

    The class is exposed under :data:`plan_state.migration_audit`
    so callers (``tasks_generator`` / ``preflight_review``) reach
    the surface via a single import. The constant
    :data:`MIGRATION_AUDIT_SCHEMA` is shared with both callers so
    schema drift propagates to all three modules in lock-step.
    """

    def __init__(self, plans_dir_path: Path) -> None:
        self._plans_dir = Path(plans_dir_path)

    def record(
        self,
        *,
        plan_id: str,
        migrator: str,
        source_payload: bytes,
    ) -> Optional[Dict[str, Any]]:
        """Write ``migration_audit`` to ``plans/<plan_id>/plan_state.json``.

        Returns the on-disk record (a copy) when the writer mutated
        the file, or ``None`` when an existing record was preserved
        (idempotent path).

        ``source_payload`` is hashed with sha256; the digest is
        stored in ``source_hash``. The migrator must pass the
        bytes of whatever it wants the audit to attest to — for
        tasks_generator that's the bytes of ``tasks.json`` before
        emission; for preflight_review that's the bytes of the
        assembled cross-document check.

        A "no plan_state.json" or "malformed JSON" condition is
        intentionally a no-op: the audit must never corrupt a
        file it cannot read. The caller is expected to gate on
        the file's presence (e.g. "only record after tasks.json
        was successfully written").
        """
        plan_state_path = self._plans_dir / plan_id / PLAN_STATE_FILENAME
        if not plan_state_path.exists():
            return None
        try:
            data = _read_plan_state_json(plan_state_path)
        except (OSError, json.JSONDecodeError):
            return None

        existing = data.get("migration_audit")
        if isinstance(existing, dict) and existing:
            # Idempotent: do not overwrite an existing record.
            # This is the security-relevant contract from
            # test_idempotent_record_preserves_source_hash.
            return None

        record_value: Dict[str, Any] = {
            "migrated":    True,
            "migrated_at": _framework_clock.utcnow_iso(),
            "migrator":    migrator,
            "source_hash": _hash_payload(source_payload),
        }
        # Validate against the schema (cheap JSON-schema-style guard).
        for key, entry in MIGRATION_AUDIT_SCHEMA.items():
            assert key in record_value, (
                f"record_value missing schema key {key!r}"
            )
            # Type checks are deliberately minimal — full schema
            # validation lives in the test suite.
            expected = entry.get("type")
            if expected == "boolean" and not isinstance(record_value[key], bool):
                raise TypeError(f"{key!r} must be a boolean")
            if expected == "string" and not isinstance(record_value[key], str):
                raise TypeError(f"{key!r} must be a string")

        data["migration_audit"] = record_value
        try:
            _write_plan_state_json(plan_state_path, data)
        except OSError:
            return None
        return dict(record_value)

    def backfill(
        self,
        *,
        plans_dir: Optional[Path] = None,
        migrator: str = "backfill-20260805",
    ) -> Dict[str, Any]:
        """Idempotently back-fill ``migration_audit`` on every
        ``plans/20260805-*`` plan directory under ``plans_dir``.

        Returns a summary dict ``{scanned, written, skipped,
        errors}``. Each plan is processed independently: a single
        unreadable plan_state.json does NOT abort the batch.

        The default ``migrator`` string is the back-fill script's
        name; callers can override (e.g. when the integration
        test invokes ``backfill``).
        """
        root = Path(plans_dir) if plans_dir is not None else self._plans_dir
        summary = {"scanned": 0, "written": 0, "skipped": 0, "errors": 0}
        if not root.exists():
            return summary
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not child.name.startswith("20260805"):
                continue
            summary["scanned"] += 1
            plan_state_path = child / PLAN_STATE_FILENAME
            if not plan_state_path.exists():
                summary["skipped"] += 1
                continue
            try:
                data = _read_plan_state_json(plan_state_path)
            except (OSError, json.JSONDecodeError):
                summary["errors"] += 1
                continue
            if isinstance(data.get("migration_audit"), dict) and data["migration_audit"]:
                summary["skipped"] += 1
                continue
            # Hash the entire file as the source_payload — the
            # back-fill migration does not have a logical payload
            # to attest to, but the audit must still carry a
            # non-empty sha256 hex so its shape validates.
            try:
                payload = plan_state_path.read_bytes()
            except OSError:
                summary["errors"] += 1
                continue
            data["migration_audit"] = {
                "migrated":    True,
                "migrated_at": _framework_clock.utcnow_iso(),
                "migrator":    migrator,
                "source_hash": _hash_payload(payload),
            }
            try:
                _write_plan_state_json(plan_state_path, data)
            except OSError:
                summary["errors"] += 1
                continue
            summary["written"] += 1
        return summary


#: Module-level handle exposed as ``plan_state.migration_audit``.
#:
#: ``tasks_generator`` and ``preflight_review`` call
#: ``plan_state.migration_audit.record(...)`` from their write
#: paths. The bound attribute is created lazily in
#: :func:`_bind_migration_audit` so the integration tests can
#: ``monkeypatch.setattr(plan_state, "plans_dir", tmp_path)``
#: BEFORE the first call and have the audit write to the
#: hermetic sandbox.
_migration_audit: Optional[MigrationAudit] = None


def _get_migration_audit() -> MigrationAudit:
    """Return (lazily binding) the module-level ``MigrationAudit``.

    The singleton is rebuilt whenever ``plans_dir`` is reassigned
    by a test, so ``monkeypatch.setattr`` stays the supported
    way to redirect the audit to a sandbox.
    """
    global _migration_audit
    if _migration_audit is None:
        _migration_audit = MigrationAudit(plans_dir)
    return _migration_audit


def _record_migration_audit(
    *,
    plan_id: str,
    migrator: str,
    source_payload: bytes,
) -> Optional[Dict[str, Any]]:
    """Module-level shim that always reads the current ``plans_dir``.

    Defined as a plain function (not a bound method) so the call
    site ``plan_state.migration_audit.record(...)`` resolves to a
    function whose ``plans_dir`` lookup happens *at call time*,
    not at import time. This is the difference between a fresh
    test sandbox (``monkeypatch.setattr`` on ``plans_dir``) and a
    permanent module-level singleton built at import.
    """
    # Force re-resolution: if the singleton is stale (e.g. tests
    # reassigned plans_dir), discard it.
    global _migration_audit
    if _migration_audit is None or _migration_audit._plans_dir != plans_dir:
        _migration_audit = MigrationAudit(plans_dir)
    return _migration_audit.record(
        plan_id=plan_id,
        migrator=migrator,
        source_payload=source_payload,
    )


def _backfill_migration_audit(
    *,
    plans_dir_arg: Optional[Path] = None,
    plans_dir: Optional[Path] = None,
    migrator: str = "backfill-20260805",
) -> Dict[str, Any]:
    """Module-level shim for ``MigrationAudit.backfill``.

    Accepts both ``plans_dir=`` and ``plans_dir_arg=`` keyword forms
    so callers can pick whichever reads more naturally. If neither
    is provided, the module-level ``plans_dir`` singleton is used.
    """
    target = Path(plans_dir_arg) if plans_dir_arg is not None else (
        Path(plans_dir) if plans_dir is not None else plans_dir
    )
    global _migration_audit
    if _migration_audit is None or _migration_audit._plans_dir != target:
        _migration_audit = MigrationAudit(target)
    return _migration_audit.backfill(
        plans_dir=target,
        migrator=migrator,
    )


# Pre-bind the module attribute so the dot-access
# ``plan_state.migration_audit.record(...)`` works without an
# extra import dance. The shims look up ``plans_dir`` at call
# time, so monkeypatching the module attribute later still
# re-routes writes.
migration_audit = SimpleNamespace(
    record=_record_migration_audit,
    backfill=_backfill_migration_audit,
)  # type: ignore[assignment]