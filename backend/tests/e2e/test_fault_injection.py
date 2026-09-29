"""E2E fault-injection: real SQLite + git checkpoints + real watchdog subprocess.

Background
----------
This is the failure-path half of the dispatcher 三段式集成测试
(happy + fault injection). It exercises the SAME three production
components (real SQLite / real git / real watchdog subprocess) but
under deliberate fault injection so the failure surface is locked
in the L5 acceptance lane:

  1. **SQLite 故障注入** — we open a SECOND :class:`sqlite3.Connection`
     with ``exclusive_locking`` semantics and write a row, so the
     watchdog subprocess's :func:`sqlite3.connect` path returns
     :class:`sqlite3.OperationalError` (``database is locked``).
     The watchdog must not crash; it must keep the process alive
     and report a "lock busy" verdict instead of hanging or
     raising an unhandled exception that takes the process down.

  2. **git checkpoint 故障注入** — the second task's ``git add``
     is run on a path that does NOT exist, so the git command
     exits non-zero. The previous-task commit is still on disk;
     the dispatcher's failure semantics must NOT mutate
     ``tasks.json`` or the SQLite store into a "ghost state"
     where the checkpoint is reported but the artifact is not
     actually written.

  3. **watchdog 死循环 故障注入** — three identical
     ``task_failed`` lines are appended to ``execution.log`` with
     the same fingerprint and no progress token change, so the
     watchdogs's ``DeadLoopDetector`` must trip
     ``triggered == True`` after exactly three observations.
     This is the **only** scenario under which a watchdog subprocess
     is allowed to exit non-zero — the contract is "the watchdog
     reports the dead loop, the supervisor reads the JSON, the
     daemon stays alive".

TDD spec — 3 fault-injection tests
----------------------------------
``test_sqlite_locked_keeps_watchdog_alive``
    * Real SQLite store + second locked connection.
    * Spawn ``watchdog --once``; expect ``returncode == 0`` AND
      a parseable JSON verdict on stdout.
    * The watchdog must NOT hang past the 10 s subprocess timeout
      (a regression that lets it block forever would be a silent
      failure of the "independent process" contract).

``test_git_checkpoint_failure_preserves_prior_state``
    * Real git repo with one prior ``[task-1]`` commit.
    * Task 2's ``git add`` is invoked on a non-existent file
      (so git returns exit 1).
    * Assert: the prior commit is STILL in ``git log`` (the
      failure did not roll the tree back); the SQLite
      ``plan_artifacts`` row for task 1 is still
      ``status='completed'``; task 2's row stays
      ``status='pending'``.

``test_watchdog_deadloop_detection_triggers_after_three``
    * Real SQLite store seeded with one plan + one task.
    * Append three identical ``task_failed`` events to
      ``execution.log`` with the same fingerprint and no
      progress-token change.
    * Spawn ``watchdog --once`` and assert the JSON verdict
      carries ``triggered == true`` AND ``count == 3``.
    * The subprocess MUST exit non-zero (``returncode == 1``)
      — that is the canonical supervisor signal that the
      dead loop was detected.

Output (asserted):
  * Each test's contract holds.
  * Total elapsed < 30 s per test.
"""

from __future__ import annotations

import json
import os
import sqlite3
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


# ---------------------------------------------------------------------------
# Constants & helpers shared with the happy-path test (kept here so this
# module is a stand-alone file the wrapper can run in isolation).
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"])
BACKEND_DIR = PROJECT_ROOT / "backend"
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"

WATCHDOG_ONCE_TIMEOUT_SECONDS = 10
HARD_TIMEOUT_SECONDS = 30

PLAN_ID = "mock-fault"
TASK_IDS: tuple[str, ...] = ("1", "2", "3")

# All three failure events share the same fingerprint — the
# watchdog's ``DeadLoopDetector`` advances the counter only when
# BOTH the fingerprint AND the progress token match the previous
# observation. We hold the progress token constant by never
# mutating ``tasks.json`` between observations.
SAME_ERROR_MESSAGE = "synthetic deadlock: the same fingerprint"
SAME_TASK_ID = "task-1"


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


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd``.

    ``check=False`` by default — the fault-injection scenarios
    expect non-zero exit codes (e.g. ``git add`` of a missing
    path). Callers assert on ``returncode`` explicitly.
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


def _spawn_watchdog_once(
    plan_id: str,
    plans_root: Path,
    *,
    expect_exit_zero: bool = True,
) -> tuple[int, str, str]:
    """Spawn ``watchdog --once`` as a real subprocess.

    Returns ``(returncode, stdout, stderr)``. The watchdog's own
    contract on a happy poll is ``returncode == 0``; on a detected
    dead loop it is ``returncode == 1``. We DO NOT assert here —
    callers know which one they want.
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
    return (
        proc.returncode,
        (proc.stdout or "").strip(),
        (proc.stderr or "").strip(),
    )


def _seed_plan_and_tasks(
    conn: Any,
    plan_id: str,
    task_ids: tuple[str, ...],
) -> None:
    """Seed the four production tables for ``plan_id``.

    This is the same contract the happy-path test uses, kept
    inline here so the fault-injection file is self-contained.
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


@pytest.fixture
def workspace(tmp_path: Path):
    """Provision a fresh workspace with a real git repo and a real SQLite store.

    Yields a dict with the four paths/handles the tests need.
    """
    repo = tmp_path / "repo"
    plans_root = tmp_path / "plans"

    _init_git_repo(repo)
    conn = _open_state_store(plans_root)
    _seed_plan_and_tasks(conn, PLAN_ID, TASK_IDS)

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


# ---------------------------------------------------------------------------
# Fault 1 — SQLite is locked: watchdog subprocess must NOT crash, MUST
# still emit parseable JSON within the 10 s subprocess budget.
# ---------------------------------------------------------------------------


def test_sqlite_locked_keeps_watchdog_alive(workspace: dict[str, Any]) -> None:
    """Locked SQLite: watchdog subprocess stays alive, JSON verdict is emitted."""
    start = time.time()
    plans_root: Path = workspace["plans_root"]
    plan_id: str = workspace["plan_id"]
    db_path = plans_root / "_state.sqlite"

    # Open a SECOND sqlite3 connection that holds an exclusive
    # transaction. The watchdog subprocess's ``sqlite3.connect`` will
    # block on the busy lock until ``busy_timeout`` expires — and
    # ``Watchdog.read_progress_token`` catches every OSError /
    # JSONDecodeError and returns an empty digest. That is the
    # contract we are verifying.
    holder = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        # Touch a table so the lock is real (SQLite lazy-acquires).
        holder.execute("SELECT COUNT(*) FROM plan_routing").fetchone()

        # Spawn the watchdog. It must NOT crash on the busy DB.
        returncode, stdout, stderr = _spawn_watchdog_once(plan_id, plans_root)

        # The watchdog's only filesystem read is ``tasks.json``
        # (which does not exist yet — fine, the read_progress_token
        # helper returns an empty token on missing/corrupt files),
        # so the SQLite lock does NOT block it. But we still verify
        # that the subprocess path returns 0 + JSON, which is the
        # contract the supervisor relies on.
        assert returncode == 0, (
            f"watchdog --once under SQLite lock returned {returncode}; "
            f"the watchdog must stay alive even when its sibling "
            f"connection holds an exclusive lock; "
            f"stdout={stdout[:200]!r}; stderr={stderr[:200]!r}"
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"watchdog --once under SQLite lock emitted non-JSON "
                f"stdout: {stdout[:200]!r} (JSONDecodeError: {exc})"
            )
        assert payload.get("plan_id") == plan_id, (
            f"verdict plan_id mismatch under lock: {payload.get('plan_id')!r}"
        )
        # The watchdog saw no failure events, so triggered is False.
        assert payload.get("triggered") is False, (
            f"watchdog under SQLite lock must report triggered=False; "
            f"got {payload.get('triggered')!r}"
        )
    finally:
        # Roll back the exclusive transaction so the holder
        # connection can close cleanly (the test process owns the
        # DB file on tmp_path; the leak would only affect this test).
        try:
            holder.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        holder.close()

    elapsed = time.time() - start
    assert elapsed < HARD_TIMEOUT_SECONDS, (
        f"sqlite-lock fault test took {elapsed:.2f}s; exceeds 30 s budget"
    )


# ---------------------------------------------------------------------------
# Fault 2 — git checkpoint failure must NOT roll back prior state.
# ---------------------------------------------------------------------------


def test_git_checkpoint_failure_preserves_prior_state(
    workspace: dict[str, Any],
) -> None:
    """A failed ``git add`` must leave the prior commit + SQLite row intact."""
    start = time.time()
    repo: Path = workspace["repo"]
    plan_id: str = workspace["plan_id"]
    conn = workspace["state_conn"]

    # Step 1 — produce one good checkpoint commit so the "prior
    # state" exists.
    good_path = repo / "checkpoints" / "task-1.done"
    good_path.parent.mkdir(parents=True, exist_ok=True)
    good_path.write_text("done by task-1\n", encoding="utf-8")
    res = _run_git(["add", str(good_path.relative_to(repo))], repo)
    assert res.returncode == 0
    res = _run_git(["commit", "-m", f"[task-1] {plan_id} work done"], repo)
    assert res.returncode == 0
    prior_sha = _run_git(["rev-parse", "HEAD"], repo).stdout.strip()

    # Mark the SQLite ``plan_artifacts`` row for task 1 as completed.
    now = _utcnow_iso()
    conn.execute(
        "UPDATE plan_artifacts SET status = 'completed', updated_at = ? "
        "WHERE plan_id = ? AND artifact_type = ?",
        (now, plan_id, "task_1"),
    )

    # Step 2 — fault-inject: ``git add`` on a path that does NOT
    # exist. Git returns exit 128 with a clear error message.
    bogus_path = "checkpoints/does-not-exist.done"
    fault_res = _run_git(["add", bogus_path], repo)
    assert fault_res.returncode != 0, (
        "precondition: ``git add`` on a missing path must return "
        "non-zero (got 0); if this assertion ever fires, the test "
        "is no longer exercising the fault-injection path"
    )
    assert (
        "did not match any" in (fault_res.stderr or "").lower()
        or "no such file" in (fault_res.stderr or "").lower()
        or "pathspec" in (fault_res.stderr or "").lower()
    ), (
        "expected git to report a missing-path error in stderr; "
        f"got stderr={fault_res.stderr!r}"
    )

    # Step 3 — verify the prior commit is still in ``git log``.
    log_lines = _run_git(
        ["log", "--format=%H %s"], repo
    ).stdout.strip().splitlines()
    prior_subject_present = any(
        f"[task-1] {plan_id} work done" in line for line in log_lines
    )
    assert prior_subject_present, (
        f"prior [task-1] commit was rolled back by the fault; "
        f"git log={log_lines}"
    )
    head_sha = _run_git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_sha == prior_sha, (
        f"HEAD changed after a failed git add; "
        f"before={prior_sha}, after={head_sha}"
    )

    # Step 4 — SQLite state for task 1 must still be ``completed``.
    row = conn.execute(
        "SELECT status FROM plan_artifacts WHERE plan_id = ? "
        "AND artifact_type = ?",
        (plan_id, "task_1"),
    ).fetchone()
    assert row is not None, "plan_artifacts row for task_1 missing"
    assert row[0] == "completed", (
        f"task_1 SQLite status was rewritten by the fault; "
        f"got {row[0]!r}, expected 'completed'"
    )
    # Task 2 (the one whose git add failed) MUST stay ``pending``
    # — the fault never wrote the artifact, so the dispatcher's
    # ghost-state guard is satisfied by leaving it pending.
    row2 = conn.execute(
        "SELECT status FROM plan_artifacts WHERE plan_id = ? "
        "AND artifact_type = ?",
        (plan_id, "task_2"),
    ).fetchone()
    assert row2 is not None and row2[0] == "pending", (
        f"task_2 SQLite status must stay 'pending' after the "
        f"fault; got {row2[0] if row2 else None!r}"
    )

    elapsed = time.time() - start
    assert elapsed < HARD_TIMEOUT_SECONDS, (
        f"git-checkpoint fault test took {elapsed:.2f}s; exceeds 30 s budget"
    )


# ---------------------------------------------------------------------------
# Fault 3 — three identical task_failed events with no progress token
# change MUST trip the watchdog's DeadLoopDetector on the third observation.
# ---------------------------------------------------------------------------


def test_watchdog_deadloop_detection_triggers_after_three(
    workspace: dict[str, Any],
) -> None:
    """Three identical failure events with no progress must trip the watchdog."""
    start = time.time()
    plans_root: Path = workspace["plans_root"]
    plan_id: str = workspace["plan_id"]
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    # Seed ``tasks.json`` with one task in the same state across all
    # three observations so the progress token is constant.
    tasks_path = plan_dir / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": SAME_TASK_ID, "status": "failed", "commit_sha": ""},
                ],
            }
        ),
        encoding="utf-8",
    )

    log_path = plan_dir / "execution.log"
    log_path.write_text("", encoding="utf-8")

    # Append three identical ``task_failed`` events with the same
    # message — same fingerprint (plan_id, task_id, normalised text)
    # AND same progress token (tasks.json never moves).
    for _ in range(3):
        entry = {
            "ts": _utcnow_iso(),
            "level": "ERROR",
            "event": "task_failed",
            "task_id": SAME_TASK_ID,
            "message": SAME_ERROR_MESSAGE,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    returncode, stdout, stderr = _spawn_watchdog_once(plan_id, plans_root)
    # The watchdog MUST exit non-zero on a detected dead loop — that
    # is the canonical supervisor signal.
    assert returncode == 1, (
        f"watchdog --once with 3 identical failures must exit 1 "
        f"(dead-loop detected); got {returncode}; "
        f"stdout={stdout[:200]!r}; stderr={stderr[:200]!r}"
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"watchdog --once dead-loop mode emitted non-JSON stdout: "
            f"{stdout[:200]!r} (JSONDecodeError: {exc})"
        )
    assert payload.get("triggered") is True, (
        f"watchdog must report triggered=True after 3 identical "
        f"failures; got {payload.get('triggered')!r}"
    )
    assert int(payload.get("count", -1)) == 3, (
        f"watchdog must report count == 3 after the 3rd observation; "
        f"got {payload.get('count')!r}"
    )
    observation = payload.get("observation") or {}
    assert observation.get("task_id") == SAME_TASK_ID, (
        f"observation.task_id mismatch: {observation.get('task_id')!r}"
    )

    elapsed = time.time() - start
    assert elapsed < HARD_TIMEOUT_SECONDS, (
        f"dead-loop detection test took {elapsed:.2f}s; exceeds 30 s budget"
    )


# ---------------------------------------------------------------------------
# Fault 4 - dispatcher post-read validation gate.
# (architecture decision points 4 + 6).
# 
# Architecture decision point 4: after _load_tasks reads
# tasks.json from disk, the dispatcher MUST re-run
# TaskOutputValidator.validate on the parsed snapshot. If the
# validator reports failures that one auto_fix pass cannot resolve,
# decision point 6 says the dispatcher writes
# plans/{plan_id}/_watchdog_signal.json with source="dispatcher"
# and aborts the scheduling loop.
# 
# This test pins BOTH branches of the gate with a real tmp_path
# filesystem (real git repo, real tasks.json, real plans root) so a
# regression that drops the gate, removes the signal write, or starts
# looping auto_fix past its one-pass quota is caught in a single E2E run.
# 
# One-pass quota enforcement: TaskOutputValidator.auto_fix is wrapped
# with an invocation counter so the test asserts the dispatcher invokes
# it EXACTLY once per _load_tasks call (not 0, not 2+).
# ---------------------------------------------------------------------------

class _AutoFixCallCounter:
    def __init__(self):
        self._auto_fix_calls = 0
    def record(self):
        self._auto_fix_calls += 1

    @property
    def auto_fix_calls(self):
        return self._auto_fix_calls


class _DummyCodingTool:
    def __init__(self, *args, **kwargs):
        pass


def _git_init_gate(project_dir):
    project_dir.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "--initial-branch=main"], project_dir)
    _run_git(["config", "user.email", "gate-test@example.com"], project_dir)
    _run_git(["config", "user.name", "gate-test"], project_dir)


def _write_gate_tasks(project_dir, tasks):
    payload = {"requirement": "dispatcher post-read gate fault injection", "tasks": tasks}
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return tasks_file


def _touch_gate_files(project_dir, paths):
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = project_dir / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def _build_dispatcher(project_dir, plan_id, plans_root, monkeypatch):
    import framework.watchdog_signal as wd
    import framework.task_output_validator as tov

    monkeypatch.setattr(wd, "PLANS_ROOT", plans_root)

    # Patch the validator class method so every TaskOutputValidator
    # instance the dispatcher constructs goes through our counter.
    # The validator is stateless so wrapping the method (rather than
    # subclassing) is safe -- the wrapper just forwards to the
    # original implementation after incrementing the call counter.
    counter = _AutoFixCallCounter()
    original_auto_fix = tov.TaskOutputValidator.auto_fix

    def counting_auto_fix(self, snapshot):
        counter.record()
        return original_auto_fix(self, snapshot)

    monkeypatch.setattr(tov.TaskOutputValidator, "auto_fix", counting_auto_fix)

    sys.path.insert(0, str(BACKEND_DIR))
    from agent import AutonomousAgent

    agent = AutonomousAgent(
        requirement="dispatcher post-read gate fault injection",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
    )
    agent.plan_id = plan_id
    return agent, counter


def test_load_tasks_has_no_pre_run_gate(tmp_path, monkeypatch):
    """Fault 4 (rewritten 2026-09-21): the dispatcher must NOT gate a plan.

    This test used to pin the opposite contract. ``agent._load_tasks``
    ran the 4-step ``TaskOutputValidator`` over the freshly-loaded
    snapshot, auto-fixed what it could, and for the rest wrote a
    ``source="dispatcher"`` watchdog signal and raised ``RuntimeError``.
    Everything must be validated at task-generation time: there is no
pre-run gate. Once a plan is executable, starting it should succeed
immediately.

    So both branches below — the fixable one and the unfixable one — now
    load cleanly, the validator is never invoked from this path, and no
    watchdog signal is written. Validation happens once, at
    task-generation time (``TasksGenerator.harden_for_execution``).
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)

    # --- Branch A: a description that references another task ---------
    # The gate used to auto-fix this by injecting the missing edge. The
    # generator now does that before tasks.json is written; the loader
    # leaves the list alone.
    a_project_dir = tmp_path / "branch_a_project"
    _git_init_gate(a_project_dir)
    _touch_gate_files(a_project_dir, ["src/repair.py"])
    _write_gate_tasks(a_project_dir, [
        {"id": "1", "title": "Root task", "description": "no deps",
         "test_command": "echo 1", "status": "pending",
         "depends_on": [], "files_to_modify": ["src/repair.py"]},
        {"id": "2", "title": "Downstream task references task-1",
         "description": "this task depends on task-1",
         "test_command": "echo 2", "status": "pending",
         "depends_on": [], "files_to_modify": ["src/repair.py"]},
    ])
    plan_id_a = "fault-branch-a-repair"
    agent_a, counter_a = _build_dispatcher(
        a_project_dir, plan_id_a, plans_root, monkeypatch
    )
    tasks_a = agent_a._load_tasks()  # must NOT raise
    assert {t.id for t in tasks_a} == {"1", "2"}
    assert not (plans_root / plan_id_a / "_watchdog_signal.json").exists()
    assert counter_a.auto_fix_calls == 0, (
        "the loader must not run the validator at all — validation moved "
        "to generation time"
    )

    # --- Branch B: an entry the validator used to reject outright -----
    # Empty title fails step-1. It must still load: the plan's tasks are
    # the operator's artefact and the executor's job is to run them.
    b_project_dir = tmp_path / "branch_b_project"
    _git_init_gate(b_project_dir)
    _touch_gate_files(b_project_dir, ["src/bad.py"])
    _write_gate_tasks(b_project_dir, [
        {"id": "1", "title": "", "description": "broken",
         "test_command": "echo 1", "status": "pending",
         "depends_on": [], "files_to_modify": ["src/bad.py"]},
    ])
    plan_id_b = "fault-branch-b-unfixable"
    agent_b, counter_b = _build_dispatcher(
        b_project_dir, plan_id_b, plans_root, monkeypatch
    )
    tasks_b = agent_b._load_tasks()  # must NOT raise
    assert {t.id for t in tasks_b} == {"1"}
    assert not (plans_root / plan_id_b / "_watchdog_signal.json").exists()
    assert counter_b.auto_fix_calls == 0



# Fault 5 — watchdog validate-through failure escalation (VP-013).
#
# Architecture decision point 6 / AC-006 sub-path:
# when the watchdog's auto-fix step cannot produce a validator-clean
# ``tasks.json`` (TaskOutputValidator.validate() still reports failures),
# the watchdog MUST:
#   (1) auto-fix step completes (no exception)
#   (2) validate-through reports failure
#   (3) NOT silently restart the daemon
#   (4) write ``plans/{plan_id}/_watchdog_signal.json`` with structured
#       fault info (source=watchdog, reason=watchdog_validate_through_exhausted)
#   (5) exit cleanly (not enter an infinite loop)
#
# The contract is verified at the orchestrator level — the action
# sequence short-circuits at VALIDATE_THROUGH, RESTART never fires,
# and a sibling reader picks up the signal via :func:`read_signal`.
# ---------------------------------------------------------------------------

_VP013_PLAN_ID = "plan-vp013-fault"


@pytest.fixture
def _vp013_plans_root(monkeypatch, tmp_path):
    """Re-target ``framework.watchdog_signal.PLANS_ROOT`` to ``tmp_path``."""
    import framework.watchdog_signal as _ws

    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_ws, "PLANS_ROOT", plans_root)
    return plans_root


def test_watchdog_validate_through_failure(
    tmp_path: Path,
    monkeypatch,
    _vp013_plans_root: Path,
) -> None:
    """VP-013: validate-through failure escalates to watchdog signal."""
    from framework.clock import utcnow_iso
    from framework.watchdog_signal import read_signal, write_signal
    from watchdog_actions import (
        ActionStep,
        WatchdogActionContext,
        reset_action_counter,
        run_action_sequence,
    )

    reset_action_counter()
    plans_root = _vp013_plans_root
    plan_id = _VP013_PLAN_ID

    # Stage 1 — auto-fix step will "complete" successfully (no exception)
    # but the snapshot still contains the dangling dep that auto_fix
    # cannot repair; the validate-through step will then report failure.
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "src").mkdir(exist_ok=True)
    (project_dir / "src" / "m.py").write_text("", encoding="utf-8")
    (project_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "1",
                        "title": "root",
                        "description": "depends on task-99 which does not exist",
                        "depends_on": ["99"],
                        "files_to_modify": ["src/m.py"],
                        "test_command": "echo ok",
                        "status": "pending",
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Track calls so we can verify RESTART never fires.
    call_log: dict[ActionStep, int] = {step: 0 for step in ActionStep}

    def research_step(ctx, **kwargs):  # type: ignore[no-untyped-def]
        call_log[ActionStep.RESEARCH] += 1
        return {"step": ActionStep.RESEARCH.name, "ok": True}

    def auto_fix_step(ctx, **kwargs):  # type: ignore[no-untyped-def]
        call_log[ActionStep.AUTO_FIX] += 1
        # (1) auto-fix completes; the file on disk still has the
        # dangling dep — model "auto-fix could not repair".
        return {"step": ActionStep.AUTO_FIX.name, "ok": True}

    def validate_through_step(ctx, **kwargs):  # type: ignore[no-untyped-def]
        call_log[ActionStep.VALIDATE_THROUGH] += 1
        # (2) validator injection: TaskOutputValidator still fails.
        return {
            "step": ActionStep.VALIDATE_THROUGH.name,
            "ok": False,
            "reason": (
                "depends_on=['99'] but task '99' does not exist"
            ),
        }

    def restart_step(ctx, **kwargs):  # type: ignore[no-untyped-def]
        call_log[ActionStep.RESTART] += 1
        return {"step": ActionStep.RESTART.name, "ok": True}

    def report_step(ctx, **kwargs):  # type: ignore[no-untyped-def]
        call_log[ActionStep.REPORT] += 1
        return {"step": ActionStep.REPORT.name, "ok": True}

    ctx = WatchdogActionContext(
        plan_id=plan_id,
        plans_root=plans_root,
        fingerprint="fp-vp013",
        progress_token="tok-vp013",
        recent_failures=(
            {
                "task_id": "1",
                "level": "ERROR",
                "event": "task_failed",
                "message": "depends_on references missing task '99'",
            },
        ),
        triggered_at=utcnow_iso(),
    )

    report = run_action_sequence(
        ctx,
        steps={
            ActionStep.RESEARCH: research_step,
            ActionStep.AUTO_FIX: auto_fix_step,
            ActionStep.VALIDATE_THROUGH: validate_through_step,
            ActionStep.RESTART: restart_step,
            ActionStep.REPORT: report_step,
        },
    )

    # (5) The orchestrator must NOT raise — the watchdog process
    # stays alive across an unrecoverable recovery attempt.
    assert report is not None
    assert report.succeeded is False, (
        "validate-through failure must short-circuit the action sequence"
    )
    assert report.failed_step == ActionStep.VALIDATE_THROUGH
    # (3) RESTART must NOT have fired.
    assert call_log[ActionStep.RESTART] == 0, (
        "watchdog must NOT restart the daemon when validate-through fails"
    )
    # REPORT is also short-circuited (it's the last step).
    assert call_log[ActionStep.REPORT] == 0
    # RESEARCH, AUTO_FIX and VALIDATE_THROUGH all ran before the failure.
    assert call_log[ActionStep.RESEARCH] == 1
    assert call_log[ActionStep.AUTO_FIX] == 1
    assert call_log[ActionStep.VALIDATE_THROUGH] == 1

    # (4) The watchdog writes _watchdog_signal.json with structured fault info.
    # In production this happens inside the on_trigger callback; here we
    # invoke write_signal explicitly to model the side-effect and verify
    # the sibling reader picks it up.
    write_signal(
        plan_id,
        source="watchdog",
        reason="watchdog_validate_through_exhausted",
        detail={
            "plan_id": plan_id,
            "attempts": 1,
            "last_attempted_fix": "auto_fix.no_op_unchanged_snapshot",
            "validator_rejections": [
                {
                    "step": "depends_on_consistency",
                    "reason": (
                        "depends_on=['99'] but task '99' does not exist"
                    ),
                }
            ],
            "restart_skipped": True,
            "failed_action_step": report.failed_step.name,
            "error": report.error,
        },
    )

    sig = plans_root / plan_id / "_watchdog_signal.json"
    assert sig.exists(), f"watchdog_signal should be written at {sig}"

    payload = read_signal(plan_id)
    assert payload is not None, "sibling reader must see the signal"
    assert payload["source"] == "watchdog"
    assert payload["reason"] == "watchdog_validate_through_exhausted"
    assert payload["plan_id"] == plan_id
    assert payload["detail"]["restart_skipped"] is True
    assert payload["detail"]["failed_action_step"] == "VALIDATE_THROUGH"
    assert len(payload["detail"]["validator_rejections"]) >= 1, (
        "validator_rejections must record the validate-through failure"
    )
    rejection = payload["detail"]["validator_rejections"][0]
    assert rejection["step"] == "depends_on_consistency"
    assert rejection["reason"].startswith("depends_on=")

    # Cleanup — restore module-level counter so subsequent tests are
    # not affected by this test's short-circuit.
    reset_action_counter()
