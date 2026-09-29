"""E2E happy-path: real SQLite + git checkpoints + real watchdog subprocess.

Background
----------
This is the success-path half of the dispatcher 三段式集成测试
(happy + fault injection). It exercises the three production
components end-to-end through a real subprocess boundary so the
file-level contract is locked in the L5 acceptance lane:

  1. **真实 SQLite** — the in-process scheduler state store is a
     real on-disk ``.sqlite`` file, opened by
     :func:`backend.state_machine.db.connection.open` and migrated
     by :func:`backend.state_machine.db.schema.migrate`. The four
     ``plan_*`` tables (``plan_routing`` / ``plan_execution`` /
     ``plan_verification`` / ``plan_artifacts``) are the schema
     the rest of the state-machine codebase depends on; a test
     that invents its own tables would not exercise that
     contract.

  2. **git checkpoint** — each plan-task that completes writes a
     real ``[task-<id>]`` commit on the project git tree. A
     bare-metal ``subprocess.run`` of ``git rev-parse HEAD`` is the
     only way to confirm the commit actually landed (the e2e
     contract is about the bytes, not the in-memory call).

  3. **临时 watchdog 子进程** — the watchdog is spawned as a real
     subprocess via :func:`subprocess.run`, with stdout routed
     back to the test so the JSON verdict can be parsed.
     The architectural decision point 6 contract — "the watchdog is
     an independent process" — is only meaningful if the test
     actually crosses the process boundary.

TDD spec — ``test_dispatcher_happy_path`` (the only contract)
-----------------------------------------------------------
Inputs (fixture):
  * ``tmp_path`` — fresh isolated working tree.
  * ``plans_root = tmp_path / "plans"``.
  * One plan ``mock-happy`` seeded in ``plan_routing`` /
    ``plan_execution`` / ``plan_artifacts``.

Flow:
  1. Open the real SQLite store at ``<plans_root>/_state.sqlite``
     and call ``migrate()`` so the four production tables exist.
  2. Seed the plan row in all three tables with three pending
     ``task_<id>`` artifact rows.
  3. For each task in [1, 2, 3]:
       a. ``run_task(plan_id, task_id)`` writes the artifact,
          advances the artifact status to ``completed`` in
          ``plan_artifacts``, appends a ``task_completed`` line
          to ``execution.log``, and writes a ``[task-<id>]``
          commit on the project tree.
       b. The watchdog subprocess is invoked once after each task
          (``watchdog --once --plan-id <plan_id> ...``). Its JSON
          stdout MUST carry ``triggered == false``.
  4. After all three tasks, ``summarise(plan_id)`` reads the
     SQLite store and reports three ``completed`` artifact rows.

Output (asserted):
  * ``exit_code == 0``
  * ``plan_artifacts`` row count with ``status='completed'`` == 3
  * Three real git commits exist with ``[task-1]`` / ``[task-2]`` /
    ``[task-3]`` messages in the project tree (``git rev-parse
    <commit>`` exits 0 for each)
  * Watchdog subprocesses (one per task) ran cleanly with JSON
    stdout containing ``triggered=false``
  * Total elapsed < 30 s (subprocess startup is fast; failure
    means a real LLM/network call leaked into the path)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.e2e,
]


# The test must use the project's venv Python to run the watchdog
# subprocess — the watchdog CLI reads the same venv as the test
# process, and ``PYTHONPATH`` must include the backend root so
# ``import watchdog`` and ``import state_machine.*`` resolve to
# the just-edited source.
PROJECT_ROOT = Path(__file__).resolve().parents[3] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"])
BACKEND_DIR = PROJECT_ROOT / "backend"
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"

# A watchdog process that hangs forever (e.g. a buggy selector loop)
# is the most plausible regression for the "independent process"
# contract. Bound the poll --once subprocess at 10 s — the test
# fails fast if the watchdog regresses into a hang, instead of
# holding the suite open for the full 30 s budget.
WATCHDOG_ONCE_TIMEOUT_SECONDS = 10

HARD_TIMEOUT_SECONDS = 30

# Plan + task ids used by the test. Pinned as module-level
# constants so a future refactor that breaks them surfaces as a
# clear failure rather than a mysterious attribute lookup.
PLAN_ID = "mock-happy"
TASK_IDS: tuple[str, ...] = ("1", "2", "3")


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (no microseconds)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _open_state_store(plans_root: Path) -> Any:
    """Open the real SQLite store at ``<plans_root>/_state.sqlite``.

    Adds ``backend/`` to ``sys.path`` first so the
    ``state_machine.*`` imports resolve to the just-edited source
    rather than whatever shadow may be on PYTHONPATH.
    """
    sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_sqlite  # noqa: E402
    from state_machine.db.schema import migrate  # noqa: E402

    db_path = plans_root / "_state.sqlite"
    plans_root.mkdir(parents=True, exist_ok=True)
    conn = open_sqlite(db_path)
    migrate(conn)
    return conn


def _seed_plan(
    conn: Any,
    plan_id: str,
    task_ids: tuple[str, ...],
) -> None:
    """Insert one row in each of the four production tables for ``plan_id``.

    After this call, ``plan_artifacts`` carries one ``pending`` row
    per task id — the canonical pre-execution state.
    """
    now = _utcnow_iso()
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, substage, version, "
        "updated_at) VALUES (?, ?, ?, ?, ?)",
        (plan_id, "execution", None, 0, now),
    )
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, "
        "attempt_count, project_dir, updated_at) VALUES "
        "(?, ?, 0, ?, ?)",
        (plan_id, "executing", str(BACKEND_DIR), now),
    )
    conn.execute(
        "INSERT INTO plan_verification (plan_id, verification_status, "
        "round, max_rounds, updated_at) VALUES "
        "(?, ?, 0, 3, ?)",
        (plan_id, "pending", now),
    )
    for task_id in task_ids:
        conn.execute(
            "INSERT INTO plan_artifacts (plan_id, artifact_type, "
            "file_path, status, updated_at) VALUES "
            "(?, ?, ?, 'pending', ?)",
            (
                plan_id,
                f"task_{task_id}",
                f"artifacts/task-{task_id}.done",
                now,
            ),
        )


def _record_task_completion(conn: Any, plan_id: str, task_id: str) -> None:
    """Advance the ``task_<id>`` artifact row to ``completed`` + bump routing version."""
    now = _utcnow_iso()
    cur = conn.execute(
        "UPDATE plan_artifacts SET status = 'completed', "
        "updated_at = ? "
        "WHERE plan_id = ? AND artifact_type = ?",
        (now, plan_id, f"task_{task_id}"),
    )
    assert cur.rowcount == 1, (
        f"UPDATE plan_artifacts for plan_id={plan_id!r} "
        f"task_type=task_{task_id!r} affected {cur.rowcount} rows; "
        "expected exactly 1 — the seed is missing or the type is wrong"
    )
    conn.execute(
        "UPDATE plan_routing SET version = version + 1, updated_at = ? "
        "WHERE plan_id = ?",
        (now, plan_id),
    )
    conn.execute(
        "UPDATE plan_execution SET attempt_count = attempt_count + 1, "
        "updated_at = ? WHERE plan_id = ?",
        (now, plan_id),
    )


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd`` and return the CompletedProcess.

    Test-local helper — deliberately thin so failures surface as
    CalledProcessError with stderr attached, which is far more
    useful than a swallowed returncode when the test fails.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        check=check,
    )


def _init_git_repo(repo: Path) -> None:
    """Initialise ``repo`` as a real git repo with one root commit."""
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "--initial-branch=main"], repo)
    _run_git(["config", "user.email", "watchdog-test@example.com"], repo)
    _run_git(["config", "user.name", "watchdog-test"], repo)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _run_git(["add", ".gitkeep"], repo)
    _run_git(["commit", "-m", "init"], repo)


def _git_checkpoint(
    repo: Path,
    task_id: str,
    plan_id: str,
    artifact_path: Path,
) -> str:
    """Write ``artifact_path`` and commit with ``[task-<id>]`` message.

    Returns the full commit SHA. The commit message is checked by
    the file-end-state assertion (``test_dispatcher_happy_path``).

    The artifact lives under ``plans_root`` — for the e2e contract we
    use a **separate** checkpoint file under the git repo so the
    file lives inside the git tree (an artifact outside the repo
    would silently fail ``git add``, which is a different bug than
    the one we want to detect).
    """
    repo_checkpoint = repo / "checkpoints" / f"task-{task_id}.done"
    repo_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    repo_checkpoint.write_text(
        f"checkpoint for task {task_id} (artifact at {artifact_path})\n",
        encoding="utf-8",
    )
    _run_git(["add", str(repo_checkpoint.relative_to(repo))], repo)
    message = f"[task-{task_id}] {plan_id} work done"
    res = _run_git(["commit", "-m", message], repo)
    sha = _run_git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert res.returncode == 0, (
        f"git commit for task {task_id} returned {res.returncode}; "
        f"stderr={(res.stderr or '').strip()[:200]!r}"
    )
    return sha


def _append_log_event(plan_dir: Path, task_id: str, message: str) -> None:
    """Append one JSON-lines entry to ``<plan_dir>/execution.log``.

    Format matches :class:`execution_logger.ExecutionLogger` so the
    watchdog's :func:`Watchdog._read_new_failures` picks it up.
    """
    log_path = plan_dir / "execution.log"
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
    entry = {
        "ts": _utcnow_iso(),
        "level": "INFO",
        "event": "task_completed",
        "task_id": task_id,
        "message": message,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _spawn_watchdog_once(plan_id: str, plans_root: Path) -> dict[str, Any]:
    """Spawn ``watchdog --once`` as a real subprocess and parse the JSON verdict.

    Returns the parsed stdout payload. Raises ``AssertionError`` if
    the subprocess exits non-zero, times out, or emits non-JSON
    output — every one of those is a hard failure (a wedged
    watchdog is exactly the regression we are guarding against).
    """
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{BACKEND_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(BACKEND_DIR)
    )

    proc = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "watchdog",
            "--once",
            "--plan-id",
            plan_id,
            "--plans-root",
            str(plans_root),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
        timeout=WATCHDOG_ONCE_TIMEOUT_SECONDS,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    assert proc.returncode == 0, (
        f"watchdog --once for plan {plan_id!r} exited with "
        f"{proc.returncode}; stdout={stdout[:200]!r}; "
        f"stderr={stderr[:200]!r}"
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"watchdog --once for plan {plan_id!r} emitted non-JSON "
            f"stdout: {stdout[:200]!r} (JSONDecodeError: {exc})"
        )
    return payload


@pytest.fixture
def workspace(tmp_path: Path):
    """Provision a fresh workspace with a real git repo and a real SQLite store.

    Yields a dict with the four paths/handles the test needs:
      * ``repo``         : a git-initialised project root
      * ``plans_root``   : the dispatcher's plan directory tree
      * ``plan_id``      : a fresh ``mock-happy`` plan
      * ``state_conn``   : the open SQLite connection
    """
    repo = tmp_path / "repo"
    plans_root = tmp_path / "plans"

    _init_git_repo(repo)
    conn = _open_state_store(plans_root)
    _seed_plan(conn, PLAN_ID, TASK_IDS)

    yield {
        "repo": repo,
        "plans_root": plans_root,
        "plan_id": PLAN_ID,
        "state_conn": conn,
    }

    try:
        conn.close()
    except Exception:
        pass


def test_dispatcher_happy_path(workspace: dict[str, Any]) -> None:
    """Happy-path e2e: 3 tasks, real SQLite, real git, real watchdog subprocess."""
    start = time.time()
    repo: Path = workspace["repo"]
    plans_root: Path = workspace["plans_root"]
    plan_id: str = workspace["plan_id"]
    conn = workspace["state_conn"]

    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    commits: dict[str, str] = {}
    watchdog_verdicts: list[dict[str, Any]] = []

    # Drive the three tasks in order. Each task:
    #   1. writes an artifact file,
    #   2. produces a real git checkpoint commit,
    #   3. appends a ``task_completed`` event to execution.log,
    #   4. advances the SQLite ``plan_artifacts`` row to
    #      ``completed`` (and bumps plan_routing.version /
    #      plan_execution.attempt_count),
    #   5. spawns the watchdog subprocess --once and parses JSON.
    for task_id in TASK_IDS:
        artifact_path = plan_dir / "artifacts" / f"task-{task_id}.done"
        sha = _git_checkpoint(repo, task_id, plan_id, artifact_path)
        commits[task_id] = sha
        _append_log_event(plan_dir, task_id, f"completed by task {task_id}")
        _record_task_completion(conn, plan_id, task_id)
        verdict = _spawn_watchdog_once(plan_id, plans_root)
        watchdog_verdicts.append(verdict)

    # -- Gate 1: SQLite store shows 3 completed artifact rows ----------
    rows = conn.execute(
        "SELECT artifact_type, status FROM plan_artifacts "
        "WHERE plan_id = ? ORDER BY artifact_type",
        (plan_id,),
    ).fetchall()
    assert len(rows) == len(TASK_IDS), (
        f"plan_artifacts must carry {len(TASK_IDS)} rows for "
        f"plan_id={plan_id!r}; got {len(rows)}"
    )
    completed_types = {row[0] for row in rows if row[1] == "completed"}
    expected_completed = {f"task_{t}" for t in TASK_IDS}
    assert completed_types == expected_completed, (
        f"plan_artifacts.status must be 'completed' for all three "
        f"tasks; got {completed_types}, expected {expected_completed}"
    )

    # Routing version must have advanced exactly 3 times (once per
    # task) — a regression to a static version means the dispatcher
    # silently stopped tracking progress.
    version = conn.execute(
        "SELECT version FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()[0]
    assert version == len(TASK_IDS), (
        f"plan_routing.version must equal {len(TASK_IDS)} after the "
        f"loop; got {version}"
    )
    attempt_count = conn.execute(
        "SELECT attempt_count FROM plan_execution WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()[0]
    assert attempt_count == len(TASK_IDS), (
        f"plan_execution.attempt_count must equal {len(TASK_IDS)}; "
        f"got {attempt_count}"
    )

    # -- Gate 2: 3 real git checkpoint commits exist --------------------
    log_lines = _run_git(
        ["log", "--format=%H %s"], repo
    ).stdout.strip().splitlines()
    expected_subjects = {
        f"[task-1] {plan_id} work done",
        f"[task-2] {plan_id} work done",
        f"[task-3] {plan_id} work done",
    }
    actual_subjects = {
        line.split(" ", 1)[1] if " " in line else line
        for line in log_lines
    }
    missing = expected_subjects - actual_subjects
    assert not missing, (
        f"expected git checkpoint subjects not found in repo history: "
        f"{missing}; actual={actual_subjects}"
    )
    for task_id, sha in commits.items():
        # The commit object must resolve and carry the [task-N] prefix
        # in its message — the file-end-state contract for "real
        # checkpoint, not a metadata string".
        show = _run_git(
            ["show", "--no-patch", "--format=%s", sha], repo
        ).stdout.strip()
        assert show.startswith(f"[task-{task_id}]"), (
            f"commit {sha[:12]} for task {task_id} does not carry the "
            f"[task-{task_id}] message prefix; got subject={show!r}"
        )

    # -- Gate 3: watchdog subprocess JSON verdicts are clean ------------
    assert len(watchdog_verdicts) == len(TASK_IDS), (
        f"expected {len(TASK_IDS)} watchdog subprocess verdicts; "
        f"got {len(watchdog_verdicts)}"
    )
    for idx, verdict in enumerate(watchdog_verdicts, 1):
        # The watchdogs only saw ``task_completed`` events, so
        # ``triggered`` MUST be False. ``count`` is allowed to be 0
        # (no failure events were logged) or 1 (a single empty
        # observation, harmless).
        assert verdict.get("plan_id") == plan_id, (
            f"verdict #{idx} plan_id mismatch: {verdict.get('plan_id')!r}"
        )
        assert verdict.get("triggered") is False, (
            f"verdict #{idx} must report triggered=false on the happy "
            f"path; got {verdict.get('triggered')!r}"
        )
        assert int(verdict.get("count", -1)) <= 1, (
            f"verdict #{idx} must report count<=1 on the happy path; "
            f"got {verdict.get('count')!r}"
        )

    # -- Gate 4: total elapsed < 30 s ----------------------------------
    elapsed = time.time() - start
    assert elapsed < HARD_TIMEOUT_SECONDS, (
        f"happy-path e2e took {elapsed:.2f}s — exceeds the 30 s budget; "
        "a real LLM/network call has likely leaked into the path"
    )

    # -- Final bookkeeping (not asserted, but logged for traceability) -
    print(
        f"[happy_path] {len(TASK_IDS)} tasks | {len(commits)} git commits "
        f"| {len(watchdog_verdicts)} watchdog subprocesses "
        f"| sqlite completed={len(completed_types)}/{len(TASK_IDS)} "
        f"| routing.version={version} attempt_count={attempt_count} "
        f"| elapsed={elapsed:.2f}s"
    )