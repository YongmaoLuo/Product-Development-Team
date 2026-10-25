"""Integration tests for :mod:`verification_workspace` — the handover and the race.

What this pins
--------------
A verification run has to end up *inside* its worktree, and the
execution chain is what has to end up there.  Four properties, each one
a way to lose the run rather than a way to fail loudly:

  1. **The handover is one field.**  The repair generator, the executor
     and the verification loop read one place — the ``project_dir``
     column of ``plan_execution`` — and nothing else.  Writing the
     worktree's path there is therefore the whole of the handover: no
     new table, no new column, and not one line changed in any of the
     three consumers.
  2. **A registration is visible.**  The server resolves the plan's
     project directory to the worktree afterwards — through the row
     *and* through the in-memory record it keeps for a plan whose
     execution has already started, because that record is what the
     readers consult first.
  3. **Concurrency produces one worktree.**  Two threads asking for
     the same plan's workspace at the same time end up with one
     directory and one branch.  Git refuses the second ``worktree add``
     for a branch that is already checked out, so the loser of the race
     has to wait for the winner and reuse what it made.
  4. **Two plans never collide.**  Two plans over the same repository
     get two directories, two branches and two registrations, and
     neither overwrites the other.

The repository is built by the fixtures here: a local git repository
with two commits and a ``refs/pull/7/head`` pointing at the second one.
No network, no real remote, no real home directory — the workspace
root is redirected into ``tmp_path`` for every test, and the state
database is the per-test one the suite redirects every reader to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server
from verification_workspace import (
    WORKSPACE_ROOT_ENV,
    Baseline,
    ensure_workspace,
    register_project_dir,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Real git, deterministically
# ---------------------------------------------------------------------------


def _git_env() -> dict:
    """An environment that makes git deterministic and offline.

    The fixed dates and identity make the commit SHAs reproducible, and
    the neutralised configuration keeps the developer's own git config
    (``commit.gpgsign``, ``init.defaultBranch``, hooks) out of the
    result — a test that depends on the machine it runs on is a test
    that fails on somebody else's.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "PDT Verification Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "PDT Verification Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """A local repository whose ``refs/pull/7/head`` points at the second commit.

    The first commit stands for the reviewed baseline's ancestor and the
    second for the pull request head under verification; ``refs/pull/7/head``
    is how a pull request head looks to a client that fetched the pull
    request ref.
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    (repo / "feature.txt").write_text("pr head\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "--quiet", "-m", "pr head")
    _git(repo, "update-ref", "refs/pull/7/head", "HEAD")
    return repo


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the verification workspace tree into ``tmp_path``.

    The default root is the operator's home directory; a test that wrote
    there would litter the machine it runs on.
    """
    root = tmp_path / "workspaces"
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(root))
    return root


def _baseline(repo: Path) -> Baseline:
    """The frozen baseline a review of ``repo`` would have frozen."""
    return Baseline(
        repo_dir=repo,
        ref="refs/pull/7/head",
        commit_sha=_git(repo, "rev-parse", "refs/pull/7/head"),
        pr_number=7,
    )


def _execution_row(plan_id: str) -> dict:
    """The plan's ``plan_execution`` row, read through the repository.

    Read through the same repository the write goes through, so the test
    sees the value the way the execution chain sees it — not through a
    hand-made SQL query that could drift from the repository's shape.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _worktree_directories(root: Path) -> list:
    """The workspace directories that exist under ``root``."""
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _schema_fingerprint() -> tuple:
    """The database's tables and their columns, as a comparable shape.

    The registration is supposed to reuse the column
    ``plan_execution`` already has, so the schema before it and the
    schema after it must be the same shape: no new table, no new
    column.  The migration is applied before the snapshot so the two
    readings are comparable — the first call would otherwise read an
    empty database and the second a migrated one.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        tables = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        columns = {
            table: tuple(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM pragma_table_info(?) ORDER BY cid",
                    (table,),
                )
            )
            for table in tables
        }
    finally:
        conn.close()
    return tables, columns


# ---------------------------------------------------------------------------
# 1. + 2. The handover: one field, and it is visible.
# ---------------------------------------------------------------------------


def test_register_project_dir_writes_plan_execution(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """The worktree's path lands in the one field the execution chain reads.

    ``plan_execution.project_dir`` is the field the repair generator, the
    executor and the verification loop resolve.  Writing the worktree's
    path there is what makes them run inside the worktree — and it is
    visible afterwards, through the server's own resolver.
    """
    server._execution_state.pop("p1", None)
    workspace = ensure_workspace("p1", _baseline(upstream_repo))
    schema_before = _schema_fingerprint()

    register_project_dir("p1", workspace.path)

    row = _execution_row("p1")
    assert row is not None, "the registration did not write a plan_execution row"
    assert row["project_dir"] == str(workspace.path)
    assert server._get_project_dir("p1") == workspace.path
    # The registration reuses the column the table already has: no new
    # table, no new column.
    assert _schema_fingerprint() == schema_before


def test_register_project_dir_refreshes_the_live_execution_record(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """A plan whose execution already started stops answering with the old tree.

    ``server._get_project_dir`` consults the in-memory execution record
    before it falls back to the database.  A registration that only wrote
    the row would therefore leave every reader with the directory the
    execution started in — the column would be right and the answers
    would still be wrong, which is the failure this test exists to catch.
    """
    workspace = ensure_workspace("p1", _baseline(upstream_repo))
    server._execution_state["p1"] = {
        "status": "running",
        "project_dir": str(upstream_repo),
        "pid": None,
        "logs": deque(maxlen=2000),
        "process": None,
    }
    try:
        register_project_dir("p1", workspace.path)

        assert server._get_project_dir("p1") == workspace.path
        assert server._execution_state["p1"]["project_dir"] == str(workspace.path)
    finally:
        server._execution_state.pop("p1", None)


# ---------------------------------------------------------------------------
# 3. Concurrency produces one worktree.
# ---------------------------------------------------------------------------


def test_concurrent_same_plan_single_worktree(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """Two threads asking for the same plan end up with one worktree.

    The directory and the branch are both derived from the plan id, so
    two threads cannot end up with two directories — but they do race on
    the *creation*, and git refuses the second ``worktree add`` for a
    path and a branch that already exist.  The loser of that race has to
    end up with the workspace the winner made rather than with an
    error, and both callers have to receive the very same workspace.
    That is what this pins.
    """
    baseline = _baseline(upstream_repo)
    # The barrier releases both threads at the same moment, so the two
    # calls really do overlap instead of running one after the other.
    barrier = threading.Barrier(2)

    def _materialise():
        barrier.wait(timeout=60)
        return ensure_workspace("p1", baseline)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_materialise) for _ in range(2)]
        workspaces = [future.result(timeout=120) for future in futures]

    assert workspaces[0] == workspaces[1]
    assert _worktree_directories(workspace_root) == ["p1"]
    listing = _git(upstream_repo, "worktree", "list", "--porcelain")
    assert listing.count("branch refs/heads/fix/p1-verification") == 1
    assert listing.count(f"worktree {workspaces[0].path}") == 1


# ---------------------------------------------------------------------------
# 4. Two plans over one repository never collide.
# ---------------------------------------------------------------------------


def test_two_plans_same_repo_do_not_collide(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """Two plans over the same repository get two trees and two branches.

    The directory is derived from the plan id and so is the branch, which
    is what keeps one plan's fixes out of the other plan's worktree.  The
    registrations follow: each plan keeps its own directory, and neither
    overwrites the other's.
    """
    server._execution_state.pop("p1", None)
    server._execution_state.pop("p2", None)
    baseline = _baseline(upstream_repo)

    first = ensure_workspace("p1", baseline)
    second = ensure_workspace("p2", baseline)

    assert first.path == (workspace_root / "p1").resolve()
    assert second.path == (workspace_root / "p2").resolve()
    assert first.path != second.path
    assert first.branch == "fix/p1-verification"
    assert second.branch == "fix/p2-verification"
    assert first.branch != second.branch
    assert _worktree_directories(workspace_root) == ["p1", "p2"]

    register_project_dir("p1", first.path)
    register_project_dir("p2", second.path)

    assert _execution_row("p1")["project_dir"] == str(first.path)
    assert _execution_row("p2")["project_dir"] == str(second.path)
    assert server._get_project_dir("p1") == first.path
    assert server._get_project_dir("p2") == second.path
