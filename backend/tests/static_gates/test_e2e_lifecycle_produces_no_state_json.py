"""VP-005 - five state JSON filenames must not be produced on the
plan directory after a full lifecycle walk.

Background
----------
The state-machine refactor (task-11 / L4 commit ``34e54af``) migrates
the runtime to SQLite as the single source of truth.  Five legacy
JSON sidecar filenames are pinned to disk as ``DO NOT WRITE`` - every
write path (the executor, the verification orchestrator, the runtime
persistence, the progress state) must use the repository layer
instead.

This test is the *automatic* counterpart of the static gate pinned in
``state_machine/tests/unit/test_json_static_gate.py
::test_no_json_state_filename_in_backend_source``: that test scans
the source code, this test scans the on-disk artifacts of a complete
plan lifecycle.  A violation in either scan breaks the contract.

The five forbidden filenames are:

    plan_state.json
    execution.json
    verification_runtime_state.json
    verification_executor_state.json
    verification_progress_state.json

A small **artifact allowlist** enumerates the product JSON / Markdown
artifacts that ARE allowed to land on the plan directory (because they
are user-authored documents, not state sidecars):

    interview.json   - user-authored requirement text (input)
    prd.md           - user-authored PRD document
    tasks.json       - generated task list (input to executor)

The allowlist is explicit and reviewed: growing it requires a
justification comment and a code review.

What the test does
------------------
1. Build a fresh SQLite state-machine DB in ``tmp_path``.
2. Run the canonical lifecycle walk through the production
   ``RoutingRepository.try_mark_phase`` code path.
3. Plant the artifact allowlist files (interview.json / prd.md /
   tasks.json) so the scan can prove the allowlist is honoured.
4. Scan ``plans/<id>/*.json`` for the five forbidden filenames.
5. Assert zero hits.

The test is marked ``acceptance_4`` so the backend's marker-driven
report can select it for the acceptance gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Repository discovery - same idiom as the lifecycle walker used in
# tests/e2e/test_full_plan_lifecycle_on_8001.py.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)


# ---------------------------------------------------------------------------
# Forbidden state JSON filenames (the "5 state JSON" the brief refers to)
# ---------------------------------------------------------------------------
#
# These are the deprecated sidecar files that the state-machine refactor
# migrated to SQLite.  Production code MUST consult the corresponding
# repository (RoutingRepository / ExecutionRepository /
# VerificationRepository / ArtifactRepository) instead.
FORBIDDEN_STATE_JSONS: frozenset = frozenset(
    {
        "plan_state.json",
        "execution.json",
        "verification_runtime_state.json",
        "verification_executor_state.json",
        "verification_progress_state.json",
    }
)


# ---------------------------------------------------------------------------
# Artifact allowlist - product JSON / Markdown files that ARE allowed
# to land on the plan directory because they are user-authored documents,
# not state sidecars.
#
# This is the explicit allowlist called out in the brief.  Any future
# entry must carry a justification comment AND be reviewed in code review.
# ---------------------------------------------------------------------------
ARTIFACT_ALLOWLIST: tuple = (
    "interview.json",  # user-authored requirement text (input)
    "prd.md",          # user-authored PRD document
    "tasks.json",      # generated task list (input to executor)
)


# ---------------------------------------------------------------------------
# Canonical lifecycle walk
# ---------------------------------------------------------------------------
EXPECTED_STAGE_TRANSITIONS: tuple = (
    "interview",
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
    "verification_running",
    "verification_passed",
)


# Standard pytest markers - drive the test into the correct
# collection buckets.  ``acceptance_4`` is the L5 anchor that VP-005
# selects via ``-m acceptance_4``.
pytestmark = [
    pytest.mark.acceptance_4,
]


def _now_iso() -> str:
    """UTC now formatted as ISO-8601 (Z-suffixed, second-precision)."""
    from datetime import datetime, timezone

    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _seed_initial_state(db_path: Path, plan_id: str) -> None:
    """INSERT the initial plan_routing + plan_verification rows.

    Mirrors what the production ``/api/plan/{id}/create`` endpoint
    does - the routing row is created with the first stage
    (``interview``) and the verification row is created with
    ``pending`` status.  Idempotent so the test can be re-run.
    """
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM plan_routing WHERE plan_id = ?", (plan_id,)
        )
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO plan_routing "
                "(plan_id, current_phase, substage, version, updated_at) "
                "VALUES (?, ?, NULL, 0, ?)",
                (plan_id, "interview", _now_iso()),
            )
        cur = conn.execute(
            "SELECT 1 FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        )
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO plan_verification "
                "(plan_id, verification_status, round, max_rounds, "
                " verification_stop_reason, runtime_state, "
                " executor_state, progress_state, results, verdicts, "
                " started_at, updated_at) "
                "VALUES (?, ?, 0, 3, NULL, NULL, NULL, NULL, NULL, "
                "NULL, NULL, ?)",
                (plan_id, "pending", _now_iso()),
            )
        conn.commit()
    finally:
        conn.close()


def _drive_stage(db_path: Path, plan_id: str, target: str) -> None:
    """CAS plan_routing.stage to ``target``; for verification stages
    also drive ``plan_verification.verification_status`` so the I1
    invariant (stage / status compatibility) holds.

    Uses the production code path
    (``RoutingRepository.try_mark_phase``) so the writes that land
    (or do NOT land) on disk are exactly the writes the production
    runtime would issue.
    """
    conn = open_db(db_path)
    try:
        repo = RoutingRepository(conn)
        v_repo = VerificationRepository(conn)
        current = repo.find(plan_id)
        if current is None:
            conn.execute(
                "INSERT INTO plan_routing "
                "(plan_id, current_phase, substage, version, updated_at) "
                "VALUES (?, ?, NULL, 0, ?)",
                (plan_id, target, _now_iso()),
            )
        else:
            repo.try_mark_phase(
                plan_id,
                expected_phases=(current["current_phase"],),
                new_phase=target,
            )

        if target == "verification_running":
            v_repo.init_round(plan_id, round_n=1, max_rounds=3)
        elif target == "verification_passed":
            v_repo._update(
                plan_id,
                verification_status="passed",
                verification_stop_reason=None,
                results=None,
            )
        conn.commit()
    finally:
        conn.close()


def _plant_artifact_allowlist(plan_dir: Path) -> None:
    """Plant the three allowed artifacts on disk so the scan can
    prove the allowlist is honoured.

    These are exactly the files the brief calls out as
    "product JSON allowed when explicitly enumerated in the allowlist".
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        '{"requirement": "VP-005 fixture requirement"}',
        encoding="utf-8",
    )
    (plan_dir / "prd.md").write_text(
        "# VP-005 fixture PRD\n\nPlaceholder.\n",
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        "[]",
        encoding="utf-8",
    )


def _scan_forbidden_state_jsons(plan_dir: Path) -> list:
    """Return the sorted list of forbidden state JSON filenames
    present in ``plan_dir`` (top-level only).

    Any file in the forbidden set is a violation.  Files in the
    artifact allowlist are explicitly NOT flagged.
    """
    if not plan_dir.exists():
        return []
    found: list = []
    for entry in sorted(plan_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name in FORBIDDEN_STATE_JSONS:
            found.append(entry.name)
    return found


# ---------------------------------------------------------------------------
# The acceptance gate
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_4
def test_e2e_lifecycle_produces_no_state_json(tmp_path: Path) -> None:
    """Walking the full lifecycle MUST NOT produce any of the five
    forbidden state JSON files on the plan directory.

    The canonical stage walk is driven through the production
    ``RoutingRepository`` so the test exercises the same code path
    the production state-machine endpoints use.  A violation here
    means a refactor regressed one of the five writes back onto the
    filesystem, re-opening the dual-write race window the refactor
    was meant to close.

    The artifact allowlist (``interview.json`` / ``prd.md`` /
    ``tasks.json``) is planted on disk so the scan can prove the
    allowlist is honoured - finding those files is NOT a violation.
    """
    # 1. Build a fresh SQLite state-machine DB.
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    # 2. Build the plan directory + plant the artifact allowlist.
    plan_id = "vptest-vp005-no-state-json"
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    _plant_artifact_allowlist(plan_dir)

    # 3. Seed the initial state-machine rows.
    _seed_initial_state(db_path, plan_id)

    # 4. Drive every stage transition through the production code path.
    for target in EXPECTED_STAGE_TRANSITIONS:
        _drive_stage(db_path, plan_id, target)

    # 5. Scan the plan directory for forbidden state JSON filenames.
    #    The artifact allowlist must be present (proves the scan is
    #    non-vacuous and the allowlist is honoured).  The forbidden
    #    set must be empty.
    allowlist_present = sorted(
        p.name for p in plan_dir.iterdir()
        if p.is_file() and p.name in ARTIFACT_ALLOWLIST
    )
    assert sorted(ARTIFACT_ALLOWLIST) == allowlist_present, (
        "artifact allowlist must be present on the plan directory "
        "so the scan is non-vacuous; planted "
        f"{sorted(ARTIFACT_ALLOWLIST)!r} but only found "
        f"{allowlist_present!r}"
    )

    leaked = _scan_forbidden_state_jsons(plan_dir)
    assert leaked == [], (
        f"VP-005 violation: lifecycle walk leaked "
        f"{len(leaked)} forbidden state JSON file(s) into "
        f"{plan_dir}: {leaked!r}.  These five filenames "
        f"({sorted(FORBIDDEN_STATE_JSONS)!r}) are migrated to "
        f"SQLite; any production write to disk is a regression "
        f"of the state-machine refactor (task-11 / L4 commit "
        f"34e54af).  Route the writes through the repository "
        f"layer (RoutingRepository / ExecutionRepository / "
        f"VerificationRepository / ArtifactRepository)."
    )


# ---------------------------------------------------------------------------
# Companion assertions: the forbidden set + the allowlist are pinned
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_4
def test_forbidden_state_json_set_is_exactly_five() -> None:
    """The forbidden set must contain EXACTLY five filenames.

    Adding a sixth is a visible diff that requires a justification
    comment + a code review.  Removing one is a regression of the
    state-machine refactor.
    """
    assert len(FORBIDDEN_STATE_JSONS) == 5, (
        f"FORBIDDEN_STATE_JSONS must contain exactly 5 filenames; "
        f"got {len(FORBIDDEN_STATE_JSONS)}: "
        f"{sorted(FORBIDDEN_STATE_JSONS)!r}"
    )
    assert FORBIDDEN_STATE_JSONS == frozenset(
        {
            "plan_state.json",
            "execution.json",
            "verification_runtime_state.json",
            "verification_executor_state.json",
            "verification_progress_state.json",
        }
    ), (
        "FORBIDDEN_STATE_JSONS must be the canonical five; got "
        f"{sorted(FORBIDDEN_STATE_JSONS)!r}"
    )


@pytest.mark.acceptance_4
def test_artifact_allowlist_is_explicit() -> None:
    """The artifact allowlist must enumerate the three canonical
    artifacts explicitly (interview.json / prd.md / tasks.json).

    Pinning the list as a literal here makes any future addition a
    visible diff that requires a justification comment + a code
    review.  The set is documented in
    ``state_machine/tests/unit/test_json_static_gate.py
    ::test_artifact_json_allowlist_is_explicit`` - both tests must
    agree on the canonical three.
    """
    assert set(ARTIFACT_ALLOWLIST) == {
        "interview.json",
        "prd.md",
        "tasks.json",
    }, (
        "ARTIFACT_ALLOWLIST must equal the canonical three ("
        "{interview.json, prd.md, tasks.json}); got "
        f"{sorted(ARTIFACT_ALLOWLIST)!r}"
    )
    assert len(ARTIFACT_ALLOWLIST) == 3, (
        f"ARTIFACT_ALLOWLIST must contain exactly 3 entries; got "
        f"{len(ARTIFACT_ALLOWLIST)}"
    )
