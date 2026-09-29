"""VP-023 anchor: ``busy_timeout = 5000`` absorbs short contention bursts.

The verification plan calls out:

    "busy_timeout 5000ms 能在常规竞争下吸收冲突"

as a discrete expected result.  This test exercises that contract
under realistic contention: eight independent subprocesses race
to ``try_mark_phase`` the SAME plan row, all using a connection
opened via the public :func:`state_machine.db.connection.open`
factory (so they all carry the pinned ``busy_timeout = 5000``).

What this test guarantees:

  1. **No SQLITE_BUSY leak** -- none of the subprocesses raises
     ``sqlite3.OperationalError("database is locked")``.  The
     subprocesses either see ``ConflictError`` (lost the CAS) or
     ``True`` (won).  busy_timeout has absorbed the short
     contention.
  2. **Exactly one winner** -- bug 2 contract: exactly one of
     the eight subprocesses flips the row from ``ready``
     to ``executing`` and bumps ``version`` from 0 to 1.
  3. **ConflictError is raised on losers** -- the other seven
     subprocesses observe a normalised ``ConflictError`` (not
     an unhandled ``OperationalError``).
  4. **No half-write** -- the post-run row is at
     ``stage='executing'`` and ``version=1``.  No in-between
     state.

The workload is intentionally small (8 workers x 1 stage
transition) so the test runs fast (~1 s) yet exercises the
WAL/BEGIN-IMMEDIATE contention path that the verification plan
calls out.

Test command (from the verification plan)::

    pytest tests/concurrency/test_busy_timeout_absorbs_contention_without_raising.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

# Backend root is two levels up from this file's directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = BACKEND_ROOT / ".venv" / "bin" / "python3"

NUM_WORKERS = 8
NUM_ROUNDS = 3  # Repeat 3 rounds to guard against flakiness on slow CI.
PLAN_ID_PREFIX = "vp023-busy-absorb"


# ---------------------------------------------------------------------------
# Worker subprocess template
# ---------------------------------------------------------------------------


_WORKER_TEMPLATE = textwrap.dedent(
    """
    import json
    import os
    import sys
    import time
    from pathlib import Path

    DB_PATH = Path(r"{db_path}")
    PLAN_ID = r"{plan_id}"
    BARRIER_DIR = Path(r"{barrier_dir}")
    WORKER_ID = {worker_id}
    RESULT_PATH = Path(r"{result_path}")

    # Open the connection via the public factory so the pinned
    # PRAGMAs (journal_mode=WAL, busy_timeout=5000,
    # synchronous=NORMAL, isolation_level=None) are applied.  This
    # is the exact connection profile the production code uses.
    sys.path.insert(0, r"{backend_root}")
    sys.path.insert(0, r"{backend_root_parent}")

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
        ConflictError,
        PlanNotFoundError,
    )

    conn = open_db(DB_PATH)
    routing = RoutingRepository(conn)

    # Wait on a barrier file (the test process writes it, then
    # deletes it; the workers unblock the moment it disappears).
    BARRIER_DIR.mkdir(parents=True, exist_ok=True)
    (BARRIER_DIR / ("ready_%d" % os.getpid())).write_text(
        "ready", encoding="utf-8"
    )

    go_file = BARRIER_DIR / "go"
    deadline = time.time() + 30.0
    while not go_file.exists():
        if time.time() > deadline:
            raise SystemExit("worker %d: barrier timeout" % WORKER_ID)
        time.sleep(0.005)

    outcome = {{"worker_id": WORKER_ID, "ok": None, "reason": None}}
    try:
        ok = routing.try_mark_phase(
            PLAN_ID,
            ("ready", "failed", "completed"),
            "executing",
        )
        outcome["ok"] = bool(ok)
    except ConflictError as exc:
        outcome["reason"] = "ConflictError: " + str(exc)
    except PlanNotFoundError:
        outcome["reason"] = "PlanNotFoundError"
    except Exception as exc:  # noqa: BLE001 -- capture
        outcome["reason"] = "{{exc_type}}: {{exc_repr}}".format(
            exc_type=type(exc).__name__, exc_repr=repr(exc)
        )

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(outcome), encoding="utf-8")
    conn.close()
    """
).strip()


def _build_worker_script(
    backend_root: Path,
    db_path: Path,
    plan_id: str,
    barrier_dir: Path,
    worker_id: int,
    result_path: Path,
) -> str:
    return _WORKER_TEMPLATE.format(
        backend_root=str(backend_root),
        backend_root_parent=str(backend_root.parent),
        db_path=str(db_path),
        plan_id=plan_id,
        barrier_dir=str(barrier_dir),
        worker_id=worker_id,
        result_path=str(result_path),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Yield a fresh tmp_path SQLite database with the four-table schema."""
    path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


def _seed_initial_stage(db_path: Path, plan_id: str) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(db_path)
    try:
        RoutingRepository(conn).insert(plan_id, "ready")
        conn.commit()
    finally:
        conn.close()


def _wait_for_ready_files(barrier_dir: Path, n: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = list(barrier_dir.glob("ready_*"))
        if len(ready) >= n:
            return
        time.sleep(0.01)
    raise RuntimeError(
        f"only {len(list(barrier_dir.glob('ready_*')))} of {n} workers ready "
        f"after {timeout}s"
    )


def _run_round(
    db_path: Path,
    plan_id: str,
    tmp_dir: Path,
    round_idx: int,
) -> list[dict[str, Any]]:
    """Spawn NUM_WORKERS subprocesses racing on the same plan_id."""
    barrier_dir = tmp_dir / f"barrier-{round_idx}"
    barrier_dir.mkdir()

    procs: list[subprocess.Popen] = []
    result_paths: list[Path] = []
    for worker_id in range(NUM_WORKERS):
        result_path = tmp_dir / f"worker-{round_idx}-{worker_id}.json"
        result_paths.append(result_path)
        script = _build_worker_script(
            BACKEND_ROOT,
            db_path,
            plan_id,
            barrier_dir,
            worker_id,
            result_path,
        )
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(BACKEND_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
        )
        procs.append(subprocess.Popen(
            [str(VENV_PYTHON), "-c", script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ))

    try:
        _wait_for_ready_files(barrier_dir, NUM_WORKERS)
        (barrier_dir / "go").write_text("go", encoding="utf-8")
        for proc in procs:
            rc = proc.wait(timeout=60)
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            assert rc == 0, f"worker exited with rc={rc}; stderr={stderr!r}"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    return [json.loads(p.read_text(encoding="utf-8")) for p in result_paths]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_busy_timeout_absorbs_contention_without_raising(
    db_path: Path, tmp_path: Path
) -> None:
    """8 subprocesses race a CAS -- busy_timeout=5000 absorbs the contention.

    The verification plan calls out:

        "busy_timeout 5000ms 能在常规竞争下吸收冲突"

    With 8 subprocesses all opening ``BEGIN IMMEDIATE`` against the
    same row, contention WILL happen -- but with ``busy_timeout=5000``
    on every connection, the contention is absorbed by SQLite's
    internal retry loop and surfaces to the application layer as a
    CAS conflict (``ConflictError`` on the losers), not as an
    ``OperationalError("database is locked")``.

    This test runs the race for ``NUM_ROUNDS`` rounds so any
    occasional flake (e.g. one round where the contention actually
    exceeds the timeout) is caught -- the test fails on the FIRST
    round that produces an unhandled ``OperationalError``.
    """
    for round_idx in range(NUM_ROUNDS):
        plan_id = f"{PLAN_ID_PREFIX}-r{round_idx}"
        _seed_initial_stage(db_path, plan_id)

        work_dir = db_path.parent / f"contention-busy-{round_idx}"
        work_dir.mkdir()
        try:
            outcomes = _run_round(db_path, plan_id, work_dir, round_idx)
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

        winners = [o for o in outcomes if o["ok"]]
        losers = [o for o in outcomes if not o["ok"]]

        # ---- Contract 1: exactly one winner ----
        assert len(winners) == 1, (
            f"round {round_idx}: expected exactly 1 winner under "
            f"busy_timeout-absorbed contention, got {len(winners)}; "
            f"outcomes={outcomes}"
        )

        # ---- Contract 2: NO SQLITE_BUSY leak ----
        # All 7 losers must classify as a normalised conflict
        # (ConflictError).  An unhandled ``OperationalError`` leak
        # means busy_timeout=5000 was NOT applied, OR the timeout
        # was insufficient for this workload.
        for o in losers:
            reason = o["reason"] or ""
            assert "OperationalError" not in reason, (
                f"round {round_idx}: worker {o['worker_id']} leaked "
                f"OperationalError under contention: {reason!r}.  "
                "busy_timeout=5000 must absorb this contention; a "
                "leak means the timeout is not applied or is too short."
            )
            assert "ConflictError" in reason, (
                f"round {round_idx}: worker {o['worker_id']} lost "
                f"unexpectedly: {reason!r}.  Expected ConflictError "
                "(normalised conflict)."
            )

        # ---- Contract 3: no half-write ----
        from state_machine.db.connection import open as open_db
        c = open_db(db_path)
        try:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT current_phase, version FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            assert row is not None, f"round {round_idx}: plan row missing"
            assert row["current_phase"] == "executing", (
                f"round {round_idx}: expected stage='executing', got "
                f"{row['stage']!r}"
            )
            assert int(row["version"]) == 1, (
                f"round {round_idx}: expected version=1, got "
                f"{row['version']!r}"
            )
        finally:
            c.close()


@pytest.mark.timeout(60)
def test_busy_timeout_pragmas_are_applied_on_every_subprocess(
    db_path: Path,
) -> None:
    """Every subprocess that opens via ``open_db`` reports busy_timeout=5000.

    This is a structural pin for the contention test above: if a
    refactor drops the ``PRAGMA busy_timeout = 5000`` line from the
    connection factory, the contention test will start leaking
    ``OperationalError`` -- but the contention test is expensive,
    so we pin the pragma here as a cheap static check that runs in
    a single subprocess.

    The test asserts the runtime state on a freshly-opened DB by
    running the factory in a subprocess and reading the PRAGMA
    back via the same connection.
    """
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        sys.path.insert(0, r"{backend_root}")
        sys.path.insert(0, r"{backend_root_parent}")

        from state_machine.db.connection import open as open_db

        DB_PATH = Path(r"{db_path}")
        conn = open_db(DB_PATH)
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        isolation = conn.isolation_level
        conn.close()
        import json
        Path(r"{result_path}").write_text(
            json.dumps({{
                "busy_timeout": int(busy),
                "journal_mode": str(journal),
                "synchronous": int(sync) if not isinstance(sync, str) else (
                    1 if sync.upper() == "NORMAL" else (
                        0 if sync.upper() == "OFF" else (
                            2 if sync.upper() == "FULL" else -1
                        )
                    )
                ),
                "isolation_level": isolation,
            }}),
            encoding="utf-8",
        )
        """
    ).strip()

    result_path = db_path.parent / "pragmas_check.json"
    script_filled = script.format(
        backend_root=str(BACKEND_ROOT),
        backend_root_parent=str(BACKEND_ROOT.parent),
        db_path=str(db_path),
        result_path=str(result_path),
    )
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(BACKEND_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
    )
    proc = subprocess.run(
        [str(VENV_PYTHON), "-c", script_filled],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"pragma-check subprocess failed: rc={proc.returncode} "
        f"stderr={proc.stderr.decode('utf-8', errors='replace')!r}"
    )

    pragmas = json.loads(result_path.read_text(encoding="utf-8"))
    assert pragmas["busy_timeout"] == 5000, (
        f"expected busy_timeout=5000, got {pragmas['busy_timeout']!r} -- "
        "without this pragma the contention test would leak OperationalError"
    )
    assert pragmas["journal_mode"].upper() == "WAL", (
        f"expected journal_mode=WAL, got {pragmas['journal_mode']!r}"
    )
    assert pragmas["synchronous"] == 1, (
        f"expected synchronous=NORMAL (1), got {pragmas['synchronous']!r}"
    )
    assert pragmas["isolation_level"] is None, (
        f"expected isolation_level=None, got {pragmas['isolation_level']!r}"
    )
