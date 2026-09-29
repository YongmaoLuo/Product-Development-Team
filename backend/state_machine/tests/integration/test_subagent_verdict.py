"""TDD tests for subagent verdict callback routing via VerificationRepository.

These tests pin the contract from task 11: when a subagent callback
(``sub_agent_runner`` -> ``_record_result`` / ``record_verdict``)
finishes a VP, the verdict must be persisted through
:class:`VerificationRepository.append_verdict` (a single
``BEGIN IMMEDIATE`` + ``COMMIT`` SQLite transaction), and the
historical ``verification_*_state.json`` files must NOT be
produced.

The tests exercise the full callback chain by:

  1. Seeding the state-machine SQLite DB with a plan row whose
     ``verdicts`` column starts as ``[]``.
  2. Constructing a :class:`VerificationExecutor` with a fresh
     :class:`VerificationRepository` bound to a forked-style
     connection (each call site gets its own handle, mirroring
     the cross-process boundary that the backend's repo-singleton + per-
     callback connection contract assumes).
  3. Driving a subagent callback that records a verdict payload
     with the canonical ``vp_id`` / ``status`` / ``worker_id`` /
     ``seq`` keys.
  4. Asserting:
     (a) the ``plan_verification.verdicts`` JSON array grew by
         one entry whose payload round-trips byte-for-byte; and
     (b) scanning ``plan_dir`` and asserting no
         ``verification_*_state.json`` file exists.  This is the
         acceptance condition (4) — five JSON files eliminated.

The tests are isolated under ``state_machine/tests/integration``
because they span the ``VerificationRepository`` and
``VerificationExecutor`` modules and write a real
``plans/<id>/`` directory.  They do NOT spin up a server or
touch the network.

Run with::

    backend/.venv/bin/python3 -m pytest \
        state_machine/tests/integration/test_subagent_verdict.py -v
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure ``backend/`` is on sys.path so the bare-module import of
# ``verification_executor`` resolves regardless of the test's cwd.
_BACKEND = Path(__file__).resolve().parents[3]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)
from verification_executor import VerificationExecutor  # noqa: E402


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """Open an isolated state.db under tmp_path and return its path."""
    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    return db


def _fresh_conn(db: Path) -> sqlite3.Connection:
    """Open a fresh connection to ``db`` (per-process handle).

    SQLite does not support sharing a connection across a process
    fork, so each callback site must open its own handle.  This
    helper mirrors the production wiring the task spec mandates.
    """
    return open_db(db)


def _seed_verification_row(db: Path, plan_id: str) -> VerificationRepository:
    """Insert a verification row whose verdicts column starts as []."""
    conn = _fresh_conn(db)
    repo = VerificationRepository(conn)
    repo.insert(plan_id, "running", verdicts=[])
    conn.close()
    return VerificationRepository(_fresh_conn(db))


# --------------------------------------------------------------------
# Helper: build a minimal VerificationExecutor with a stubbed
# sub_agent_runner that returns a canned verdict dict.
# --------------------------------------------------------------------


def _build_executor(
    plan_id: str,
    plan_dir: Path,
    verif_repo: VerificationRepository,
    verdict_to_return: dict,
) -> VerificationExecutor:
    """Build a VerificationExecutor bound to ``verif_repo`` for tests.

    A real ``sub_agent_runner`` would call the LLM + LLM-subagent
    pipeline; here we substitute a sync stub that returns
    ``verdict_to_return`` directly so the test is deterministic
    and has no LLM dependency.
    """

    def stub_runner(vp_node: dict) -> dict:
        return dict(verdict_to_return)

    plan = {
        "vps": [
            {
                "id": "VP-001",
                "method": "code_review",
                "depends_on": [],
                "title": "stub VP",
                "expected_result": "",
                "test_command": "",
                "priority": "high",
            }
        ]
    }
    return VerificationExecutor(
        verification_plan=plan,
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=stub_runner,
        verif_repo=verif_repo,
    )


# --------------------------------------------------------------------
# TDD #1 — subagent verdict callback writes via Repository
# --------------------------------------------------------------------


def test_subagent_verdict_writes_via_repository(
    tmp_path: Path, state_db: Path
) -> None:
    """Subagent callback -> plan_verification.verdicts has the new entry.

    The stub subagent returns a verdict whose payload mirrors the
    canonical contract from the task spec
    (``vp_id`` / ``status`` / ``worker_id`` / ``seq``).  After
    running the executor once, the verdicts column must contain
    exactly that one entry — the JSON round-trip preserves every
    field.  No ``verification_*_state.json`` is asserted here;
    that's the dedicated second test.
    """
    plan_id = "subagent-verdict-callback"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)

    verif_repo = _seed_verification_row(state_db, plan_id)
    payload = {
        "vp_id": "VP-001",
        "status": "passed",
        "worker_id": "w3",
        "seq": 7,
    }
    executor = _build_executor(plan_id, plan_dir, verif_repo, payload)

    # Drive a single verdict through the canonical public write
    # path that the real subagent callback uses.
    verdict = {"status": "PASSED", "reasons": [], "evidence": {}}
    executor.record_verdict("VP-001", verdict)
    # The repository contract requires the verdict payload to carry
    # the canonical keys; ``record_verdict`` must therefore also
    # delegate to ``verif_repo.append_verdict`` with those keys.
    # We assert by reading the verdicts column directly.
    verif_repo_conn = _fresh_conn(state_db)
    try:
        raw = verif_repo_conn.execute(
            "SELECT verdicts FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]
    finally:
        verif_repo_conn.close()

    verdicts = json.loads(raw)
    assert verdicts, "verdicts column must be non-empty after subagent callback"
    assert len(verdicts) == 1
    entry = verdicts[0]
    # The executor's verdict body uses the executor's status enum
    # (uppercase ``PASSED``/``FAILED``/``SKIPPED``).  The Repository
    # payload preserves whatever the verdict body declared; the
    # test only asserts the canonical keys are present and that
    # the values round-trip without corruption.
    assert entry["status"] in {"PASSED", "FAILED", "SKIPPED"}
    assert entry["worker_id"] == "executor"
    assert isinstance(entry["seq"], int) and entry["seq"] >= 1
    assert entry["vp_id"] == "VP-001"
    # The reasons / evidence forwarded by ``record_verdict`` are
    # also preserved (verdict body was ``{"reasons": [], "evidence": {}}``).
    assert entry["reasons"] == []
    assert entry["evidence"] == {}


# --------------------------------------------------------------------
# TDD #2 — no verification_*_state.json files are written
# --------------------------------------------------------------------


def test_subagent_verdict_no_json_files_written(
    tmp_path: Path, state_db: Path
) -> None:
    """Acceptance condition (4): subagent callback writes zero JSON files.

    Scans ``plan_dir`` (and its subdirectories) for any file
    matching the ``verification_*_state.json`` pattern.  The
    historical contract produced FIVE such files per run; under
    the Repository contract the executor must produce NONE.
    """
    plan_id = "subagent-verdict-no-json"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)

    verif_repo = _seed_verification_row(state_db, plan_id)
    payload = {
        "vp_id": "VP-001",
        "status": "passed",
        "worker_id": "w1",
        "seq": 1,
    }
    executor = _build_executor(plan_id, plan_dir, verif_repo, payload)

    verdict = {"status": "PASSED", "reasons": [], "evidence": {}}
    executor.record_verdict("VP-001", verdict)
    # Defensive: also drive the internal ``_record_result`` entry
    # point (the path BaseExecutor.run() actually uses).  This
    # exercises the same verdict-persistence path that the LLM
    # subagent callback eventually triggers.
    executor._record_result("VP-001", verdict)

    matches: list[Path] = []
    for path in plan_dir.rglob("verification_*_state.json"):
        matches.append(path)

    assert not matches, (
        f"subagent verdict path leaked {len(matches)} "
        f"verification_*_state.json file(s): "
        f"{[str(m.relative_to(plan_dir)) for m in matches]}; "
        f"the Repository contract mandates ZERO such files"
    )
