"""VP-010 / bug_2 anchor: 8-process concurrent execution-start CAS.

This file is the multi-process companion to
``tests/unit/cas/test_start_cas_decision_table.py``.  The
verification harness expects it under
``tests/concurrency/test_concurrent_execution_start_only_one_wins.py``
with the ``bug_2`` marker.

The contract under test (PRD DP-2 / arch-design DP-3):

  Two concurrent ``start execution`` calls on the SAME plan MUST
  resolve to one winner — exactly ONE caller succeeds (rows updated
  and version +1) and the remaining N-1 callers MUST observe a CAS
  conflict (so they can be surfaced as HTTP 409).

We exercise this with **eight independent OS subprocesses** (the
layout the executor uses when 8 verification VPs run in parallel).
Each subprocess opens the same SQLite file in autocommit mode, runs
its own BEGIN IMMEDIATE / COMMIT, and the WAL file coordinates the
write lock between them.  We then read the post-run database back
in the test process and assert:

  1. Exactly 1 subprocess wins (``try_mark_phase`` returns ``True``).
  2. Exactly N-1 = 7 subprocesses lose with a
     :class:`state_machine.repositories.ConflictError`.
  3. The post-run ``plan_routing`` row has ``stage='executing'`` and
     ``version`` is exactly 1 (i.e. incremented by exactly ONE
     successful CAS — NOT 8 times the version field, NOT 0).
  4. No partial business writes (plan_execution / plan_verification
     / plan_artifacts) exist for our plan_id (the cross-table
     isolation contract — bug 1 anchor invariants hold here too).

We repeat the whole case for 5 rounds to guard against flaky
synchronisation.  If any round produces a different (success_count,
conflict_count) tuple, the test fails.

The pytest.ini ``bug_2:`` line names this file as the regression
lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Backend root is two levels up from this file's directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = BACKEND_ROOT / ".venv" / "bin" / "python3"


NUM_WORKERS = 8
NUM_ROUNDS = 5
EXPECTED_PLAN_ID_SUFFIX = "vp010-cas-contention"


# ---------------------------------------------------------------------------
# Subprocess worker script
# ---------------------------------------------------------------------------


_WORKER_SCRIPT = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, r"{backend_root}")
    sys.path.insert(0, r"{backend_root_parent}")

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
        ConflictError,
        PlanNotFoundError,
    )

    plan_id = r"{plan_id}"
    db_path = Path(r"{db_path}")
    barrier_token = r"{barrier_token}"
    barrier_dir = Path(r"{barrier_dir}")

    # Each worker opens its OWN connection — distinct file handle,
    # distinct WAL reader position.  This is the multi-process
    # simulation the anchor contract relies on.
    conn = open_db(db_path)
    routing = RoutingRepository(conn)

    # Wait on a multi-process barrier file.  The barrier protocol
    # is: the test process writes ``barrier_token`` then deletes it;
    # every worker polls for the token's absence and fires the CAS
    # the moment it disappears.  We use a file rather than
    # threading primitives because the workers are in different
    # PROCESSES, not different threads.
    while barrier_dir.exists() and (barrier_dir / barrier_token).exists():
        try:
            # Yield to the scheduler so the test process can
            # rm the token.
            import time
            time.sleep(0.001)
        except Exception:
            break

    try:
        ok = routing.try_mark_phase(
            plan_id,
            ("ready", "failed", "completed"),
            "executing",
        )
        outcome = {{"worker_id": {worker_id}, "ok": ok, "reason": None}}
    except ConflictError as exc:
        outcome = {{
            "worker_id": {worker_id},
            "ok": False,
            "reason": "predicate mismatch" if "predicate mismatch" in str(exc)
                       else "version mismatch",
        }}
    except PlanNotFoundError:
        outcome = {{"worker_id": {worker_id}, "ok": False, "reason": "plan_not_found"}}
    except Exception as exc:
        outcome = {{"worker_id": {worker_id}, "ok": False, "reason": "other: {{exc!r}}"}}

    Path(r"{result_path}").write_text(json.dumps(outcome), encoding="utf-8")
    conn.close()
    """
).strip()


def _build_worker_script(
    backend_root: Path,
    plan_id: str,
    db_path: Path,
    barrier_token: str,
    barrier_dir: Path,
    worker_id: int,
    result_path: Path,
) -> str:
    return _WORKER_SCRIPT.format(
        backend_root=str(backend_root),
        backend_root_parent=str(backend_root.parent),
        plan_id=plan_id,
        db_path=str(db_path),
        barrier_token=barrier_token,
        barrier_dir=str(barrier_dir),
        worker_id=worker_id,
        result_path=str(result_path),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Fresh tmp_path SQLite with the four-table schema."""
    path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


def _seed_initial_stage(conn, plan_id: str, stage: str) -> None:
    """Insert a plan_routing row at ``stage`` version=0."""
    from state_machine.repositories.routing_repository import RoutingRepository
    RoutingRepository(conn).insert(plan_id, stage)


def _run_round(
    db_path: Path,
    plan_id: str,
    tmp_dir: Path,
    round_idx: int,
) -> list[dict]:
    """Spawn NUM_WORKERS subprocess workers all racing on the same plan.

    Returns the list of per-worker outcomes read from the result files.
    """
    barrier_dir = tmp_dir / f"barrier-{round_idx}"
    barrier_dir.mkdir()
    barrier_token = "go.token"

    # Pre-create the barrier file so workers block before we
    # delete it.  The trick: every worker polls for the file's
    # existence; once we remove it they all unblock within one
    # poll cycle.
    (barrier_dir / barrier_token).touch()

    procs = []
    result_paths = []
    for worker_id in range(NUM_WORKERS):
        result_path = tmp_dir / f"worker-{round_idx}-{worker_id}.json"
        result_paths.append(result_path)
        script = _build_worker_script(
            BACKEND_ROOT,
            plan_id,
            db_path,
            barrier_token,
            barrier_dir,
            worker_id,
            result_path,
        )
        # Each worker is its own PROCESS — this is what makes
        # the test exercise the multi-process CAS path rather
        # than the in-process GIL-serialised thread path.
        p = subprocess.Popen(
            [str(VENV_PYTHON), "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(BACKEND_ROOT),
            env={**os.environ, "PYTHONPATH": f"{BACKEND_ROOT}:{BACKEND_ROOT.parent}"},
        )
        procs.append(p)

    # Tiny grace period so all workers have entered their poll
    # loop before we drop the barrier.
    import time
    time.sleep(0.5)

    # Drop the barrier — all workers race.
    (barrier_dir / barrier_token).unlink()

    for p in procs:
        stdout, stderr = p.communicate(timeout=60)
        if p.returncode != 0:
            raise RuntimeError(
                "worker failed: rc=%s stdout=%s stderr=%s"
                % (p.returncode, stdout[:500], stderr[:500])
            )

    outcomes = []
    for path in result_paths:
        outcomes.append(json.loads(path.read_text(encoding="utf-8")))

    # Cleanup barrier dir.
    import shutil
    shutil.rmtree(barrier_dir, ignore_errors=True)
    return outcomes


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


@pytest.mark.bug_2
@pytest.mark.parametrize("round_idx", list(range(NUM_ROUNDS)))
def test_concurrent_execution_start_only_one_wins(
    db_path: Path, round_idx: int
) -> None:
    """VP-010 anchor: 8 workers racing on the same ``ready`` plan.

    Asserts the bug-2 contract:

      * Exactly ONE worker sees ``ok=True``.
      * Exactly N-1 = 7 workers see ``ok=False`` with
        ``reason="predicate mismatch"`` or ``reason="version mismatch"``
        (i.e. ConflictError).
      * Post-run, ``plan_routing.version == 1`` (only ONE bump).
      * Post-run, no business rows in plan_execution /
        plan_verification / plan_artifacts.
    """
    plan_id = f"{EXPECTED_PLAN_ID_SUFFIX}-{round_idx}"

    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        _seed_initial_stage(conn, plan_id, "ready")
        conn.commit()
    finally:
        conn.close()

    # Run the round in a fresh temp subdir so the result files
    # don't collide with other rounds.
    work_dir = db_path.parent / f"contention-{round_idx}"
    work_dir.mkdir()
    try:
        outcomes = _run_round(db_path, plan_id, work_dir, round_idx)
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)

    success = [o for o in outcomes if o["ok"]]
    conflict = [o for o in outcomes if not o["ok"]]

    # Exactly one winner — bug-2 contract.
    assert len(success) == 1, (
        f"round {round_idx}: expected exactly 1 winner, got "
        f"{len(success)}; outcomes={outcomes}"
    )
    # The remaining N-1 workers all lose.
    assert len(conflict) == NUM_WORKERS - 1, (
        f"round {round_idx}: expected {NUM_WORKERS - 1} losers, got "
        f"{len(conflict)}; outcomes={outcomes}"
    )

    # Every loser must classify as a CAS conflict (predicate or
    # version mismatch).  No ``plan_not_found`` or ``other`` reasons
    # are acceptable in this scenario — every worker sees the same
    # pre-seeded row.
    for o in conflict:
        assert o["reason"] in ("predicate mismatch", "version mismatch"), (
            f"round {round_idx}: unexpected loser reason={o['reason']!r}"
        )

    # Post-run DB state.
    conn = open_db(db_path)
    try:
        from state_machine.repositories.routing_repository import RoutingRepository
        routing = RoutingRepository(conn)
        row = routing.find(plan_id)
        assert row is not None, f"round {round_idx}: plan row missing"
        assert row["current_phase"] == "executing"
        # Exactly one bump — version started at 0.
        assert row["version"] == 1, (
            f"round {round_idx}: expected version=1, got {row['version']!r}"
        )

        # Cross-table isolation — no business rows exist for our
        # plan_id; the routing CAS owns plan_routing exclusively.
        for table in ("plan_execution", "plan_verification", "plan_artifacts"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
            assert n == 0, (
                f"round {round_idx}: CAS-contended plan has {n} rows in {table}"
            )
    finally:
        conn.close()


@pytest.mark.bug_2
def test_concurrent_start_version_increments_exactly_once(db_path: Path) -> None:
    """Bug-2 anchor: under contention, the post-run ``version`` is
    exactly 1 (one CAS succeeded, the rest lost).

    This is a single-round determinism pin — the parametrised
    rounds above already enforce this; this anchor case exists so
    that ``tests/regression/_inclusion_snapshot.json`` and the
    ``test_every_bug_marker_has_at_least_one_anchor_case`` meta-test
    have a named, individually-runnable anchor.
    """
    plan_id = "vp010-version-once"
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        _seed_initial_stage(conn, plan_id, "ready")
        conn.commit()
    finally:
        conn.close()

    work_dir = db_path.parent / "contention-version-once"
    work_dir.mkdir()
    try:
        outcomes = _run_round(db_path, plan_id, work_dir, 0)
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)

    winners = [o for o in outcomes if o["ok"]]
    assert len(winners) == 1

    conn = open_db(db_path)
    try:
        from state_machine.repositories.routing_repository import RoutingRepository
        routing = RoutingRepository(conn)
        row = routing.find(plan_id)
        assert row is not None
        assert row["version"] == 1
    finally:
        conn.close()
