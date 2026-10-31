"""A **real** base-drift conflict, end to end: BLOCKED report + one merge task.

Why this file exists
--------------------
The unit tests around the base-drift preflight hand it a *stubbed* report:
a dataclass with ``status="conflicts"`` and a conflict file nobody ever
produced.  That is enough to test the decision tree, and it is exactly
what makes the tree untestable at its edge — a stub cannot disagree with
the code that reads it.

What has to be true in production, and is not visible in a stubbed test:

* a **real** merge of a real base into a real branch really does leave
  unmerged paths behind, and the check really does list them;
* the merge attempt is **aborted**, so the branch is left where it was —
  same ``HEAD``, same clean working tree, no merge in progress;
* the conflict really does become **one** task row in the state database,
  with a title and a probe command that an executor can run;
* and a world that has **no** conflict writes **no** task at all.

So this file builds the world with real git — a repository, a local bare
remote, and a linked work tree — and lets the production code do
everything else.  Nothing here stubs the check.

The world
---------
``build_conflict_world`` builds two branches that changed **the same
region of the same file**:

* ``main``   changes ``shared.py`` to return ``"main"``;
* ``feat``   changes ``shared.py`` to return ``"feat"``.

Both start from the same base commit, so neither side can be merged into
the other without a conflict — and git says so on the same line the two
sides changed.  The branches are published to a bare remote and the work
tree fetches it, so the drift the check measures is real.

``build_conflict_free_world`` is the same world with one difference:
``main`` changes a *different* file.  The merge then succeeds, the check
reports ``merged``, and **no** task may be written.  That is the negative
control: without it, a test that asserts "a task was written" would also
pass against a preflight that writes a task for every branch, every time.

Identity
--------
Every commit carries an identity passed with ``-c`` and configured
**locally** in the repository under test.  The suite runs with ``$HOME``
pointed at a scratch directory where no global git identity exists, and
the merge commit the check creates needs one — a work tree shares the
repository's common config, so the local configuration covers it.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
from base_drift import (  # noqa: E402
    BASE_DRIFT_VP_ID,
    STATUS_CONFLICTS,
    STATUS_MERGED,
)
from config_paths import resolve_state_db_path  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate as _migrate_state_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)

pytestmark = pytest.mark.integration


GIT_IDENTITY = (
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
)
"""The identity every commit in these tests carries.

It is passed with ``-c`` instead of being configured globally: the suite
runs with ``$HOME`` pointed at a scratch directory, where ``user.email``
is not set, and a commit without an identity fails outright.
"""

SHARED_FILE = "shared.py"
"""The file both sides of the conflict change."""

OTHER_FILE = "other.txt"
"""The file ``main`` changes in the conflict-free world."""

BASE_CONTENT = 'def shared():\n    return "base"\n'
FEAT_CONTENT = 'def shared():\n    return "feat"\n'
MAIN_CONTENT = 'def shared():\n    return "main"\n'
"""Three versions of the same line — the line both sides change."""

PLAN_ID = "plan-20261031"
"""The plan the preflight files the merge task under."""

ROUND = 1
TASK_ID = f"repair-r{ROUND}-01"
"""The task id the round number gives the merge task."""


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo`` and fail loudly if it errors."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        text=True,
        capture_output=True,
    )


def _git_quiet(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command and report the outcome instead of raising.

    Used for the questions whose answer *is* a non-zero exit code —
    "is a merge in progress?" is asked that way.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=False,
        text=True,
        capture_output=True,
    )


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    """Write a file, commit it, and return the new commit's SHA."""
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _porcelain(repo: Path) -> str:
    """The working tree's ``git status --porcelain`` listing."""
    return _git(repo, "status", "--porcelain").stdout


def _head(repo: Path) -> str:
    """The commit ``HEAD`` points at."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# the world
# ---------------------------------------------------------------------------


@dataclass
class _World:
    """The three directories one scenario needs, plus what they hold.

    ``repo``     the main checkout — ``main`` is checked out here,
    ``bare``     ``origin.git`` — what the work tree fetches from,
    ``work``     the work tree — the branch under test.
    """

    repo: Path
    bare: Path
    work: Path
    base_sha: str
    upstream_sha: str
    feat_sha: str

    def close(self) -> None:
        """Take the work tree down again.

        ``git worktree remove --force`` is what the scenario owes the
        machine it runs on: a work tree left behind keeps its branch
        checked out, and the next scenario would meet a repository that
        is not the one it built.
        """
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "remove",
                "--force",
                str(self.work),
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            check=False,
            text=True,
            capture_output=True,
        )


def _build_world(root: Path, *, conflict: bool) -> _World:
    """Build a repository with a real work tree on a branch of its own.

    ``conflict`` decides whether ``main`` and ``feat`` change the same
    region of the same file.  Everything else about the two worlds is
    deliberately identical, so the difference between them is exactly
    the thing the tests are about.
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo),
        check=True,
        text=True,
        capture_output=True,
    )
    # The identity is configured **locally**, in the repository under
    # test: the merge the check performs creates a commit, and the suite
    # runs with $HOME pointed at a scratch directory where no global
    # identity exists.  A work tree shares the repository's common
    # config, so this covers the work tree too.
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")

    base_sha = _commit(repo, SHARED_FILE, BASE_CONTENT, "base commit")

    # The remote stands in for the network: a bare repository on disk.
    bare = root / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main")

    # The branch under test: a real work tree, a real branch of its own.
    work = root / "wt"
    _git(repo, "worktree", "add", "-b", "feat", str(work))

    # The branch moves: it changes the shared line in its own direction.
    feat_sha = _commit(work, SHARED_FILE, FEAT_CONTENT, "feat changed shared.py")

    # The base moves.  In the conflicting world it changes the *same*
    # region of the *same* file — so neither side can be merged into the
    # other without git reporting a conflict.  In the conflict-free
    # world it changes a different file, so the merge succeeds.
    if conflict:
        upstream_sha = _commit(
            repo, SHARED_FILE, MAIN_CONTENT, "main changed shared.py"
        )
    else:
        upstream_sha = _commit(
            repo, OTHER_FILE, "other\n", "main changed other.txt"
        )
    _git(repo, "push", "origin", "main")
    # And the work tree learns about it — this is the fetch a developer
    # working in the work tree would run.  Without it the
    # remote-tracking ref is stale and the drift is invisible.
    _git(work, "fetch", "origin", "main")

    return _World(
        repo=repo,
        bare=bare,
        work=work,
        base_sha=base_sha,
        upstream_sha=upstream_sha,
        feat_sha=feat_sha,
    )


def build_conflict_world(root: Path) -> _World:
    """A world in which the base and the branch really conflict.

    ``main`` and ``feat`` both changed the same line of ``shared.py``,
    both published, and the work tree has fetched the base.
    """
    return _build_world(root, conflict=True)


def build_conflict_free_world(root: Path) -> _World:
    """A world in which the base and the branch do **not** conflict.

    The same shape as :func:`build_conflict_world`, except that ``main``
    changed a different file — so the merge succeeds and the check has
    nothing to report.
    """
    return _build_world(root, conflict=False)


# ---------------------------------------------------------------------------
# the round
# ---------------------------------------------------------------------------


class _FakePersistence:
    """The persistence surface the preflight touches."""

    def __init__(self) -> None:
        self.events: List[Any] = []
        self.reports: List[Any] = []

    def write_verification_point_log(self, vp_id, event_type, data):
        self.events.append((vp_id, event_type, data))

    def update_report(self, report_data):
        self.reports.append(report_data)


def _orchestrator(tmp_path: Path, world: _World):
    """An orchestrator carrying only the attributes the preflight reads."""
    orch = verification_agent.Orchestrator.__new__(
        verification_agent.Orchestrator
    )
    orch.project_dir = world.work
    orch.plan_dir = tmp_path / PLAN_ID
    orch.persistence = _FakePersistence()
    orch._current_round = ROUND
    orch._last_base_drift_report = None
    return orch


def _use_db(tmp_path: Path, monkeypatch) -> Path:
    """Point the state database at a throwaway file for this test."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    assert resolve_state_db_path() == db_path
    return db_path


def _load_rows(db_path: Path, plan_id: str) -> Dict[str, Dict[str, Any]]:
    """Every task row of ``plan_id``, read back through the repository.

    The schema is migrated first: a database that never received a row
    has no ``plan_tasks`` table at all, and the negative control reads
    exactly such a database.  Reading it without the migration would
    fail on the missing table instead of answering "no rows".
    """
    conn = open_db(db_path)
    try:
        _migrate_state_db(conn)
        return PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()


@pytest.fixture
def world_factory(tmp_path: Path) -> Callable[..., _World]:
    """Build isolated repositories, and take their work trees down again."""
    worlds: List[_World] = []

    def build(*, conflict: bool = True) -> _World:
        root = tmp_path / ("conflict" if conflict else "clean")
        root.mkdir()
        world = (
            build_conflict_world(root)
            if conflict
            else build_conflict_free_world(root)
        )
        worlds.append(world)
        return world

    yield build

    for world in reversed(worlds):
        world.close()


@pytest.fixture(autouse=True)
def _clear_base_drift_env(monkeypatch):
    """The check reads its configuration from the environment.

    A stray value would change what the check does, so the environment is
    cleared rather than assumed: the check compares against the default
    ``origin/main`` and only reports.
    """
    for name in (
        base_drift.ENV_ENABLED,
        base_drift.ENV_BASE,
        base_drift.ENV_MODE,
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


def test_real_conflict_still_writes_the_blocked_report(
    world_factory, monkeypatch, tmp_path
):
    """A real conflict blocks the round and says what it found.

    The report is what the operator reads when the round refuses to
    start, so it has to carry the evidence: the base that was merged in,
    the commit it was merged from, and the files that are in conflict.
    A stubbed report proves the plumbing; only a real conflict proves
    that the plumbing is fed the truth.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    world = world_factory(conflict=True)
    orch = _orchestrator(tmp_path, world)

    blocked = orch._run_base_drift_preflight()
    assert blocked is not None, "a real conflict must block the round"

    # The round never started, so it has no verdict to give.
    assert blocked["overall_status"] == "BLOCKED", blocked
    assert blocked["stop_reason"] == "base_drift_conflicts", blocked
    assert blocked["verification_results"] == [], blocked

    # The conflict's own evidence travels with the report.
    conflicts = blocked["base_drift_conflicts"]
    assert conflicts["status"] == STATUS_CONFLICTS, conflicts
    assert conflicts["base_ref"] == "origin/main", conflicts
    assert conflicts["base_sha"] == world.upstream_sha, conflicts
    assert conflicts["behind"] == 1, conflicts
    paths = [entry["path"] for entry in conflicts["conflict_files"]]
    assert paths == [SHARED_FILE], conflicts
    assert conflicts["conflict_files"][0]["conflict_blocks"] >= 1, conflicts

    # The base it compared against is the base that was really published:
    # the bare remote carries ``main``'s tip, and that is the commit the
    # check merged.
    assert (
        _git(world.bare, "rev-parse", "main").stdout.strip()
        == world.upstream_sha
    )
    assert world.base_sha != world.upstream_sha

    # And the event log says the same thing, so a reader who never sees
    # the blocked report still learns that the check ran and what it hit.
    events = [
        (event_type, data)
        for _vp_id, event_type, data in orch.persistence.events
    ]
    assert events, orch.persistence.events
    event_type, data = events[-1]
    assert event_type == "base_drift_conflicts", events
    assert data["status"] == STATUS_CONFLICTS, events

    # The merge task is handed back to the loop, under the id the round
    # number gives it.
    assert blocked["base_drift_repair_task_id"] == TASK_ID, blocked

    # The task really is in the database — the report does not invent it.
    rows = _load_rows(db_path, PLAN_ID)
    assert list(rows) == [TASK_ID], rows


def test_real_conflict_persists_one_merge_task(world_factory, monkeypatch, tmp_path):
    """The conflict becomes exactly one executable merge task.

    One — not one per round and not one per conflicting file.  The id
    carries the round number, so a second task would be a *new* id for
    the *same* conflict, and a merge that keeps failing would then be
    dispatched forever.  The row has to be **executable**: a title (or
    the loader that reconciles orphan rows substitutes a content-free
    placeholder) and a probe command that really answers "is the merge
    done?".
    """
    db_path = _use_db(tmp_path, monkeypatch)
    world = world_factory(conflict=True)
    orch = _orchestrator(tmp_path, world)

    blocked = orch._run_base_drift_preflight()
    assert blocked is not None and blocked["overall_status"] == "BLOCKED", blocked

    rows = _load_rows(db_path, PLAN_ID)
    assert list(rows) == [TASK_ID], rows

    row = rows[TASK_ID]
    assert row["title"], row
    assert row["test_command"].startswith("git merge-base --is-ancestor"), row
    assert row["failed_vp_id"] == BASE_DRIFT_VP_ID, row
    assert row["task_group"] == "repair", row
    assert row["round"] == ROUND, row
    assert row["files_to_modify"] == [SHARED_FILE], row
    assert row["status"] == "pending", row

    # The task names the base it merges and the file it resolves.
    assert world.upstream_sha in row["test_command"], row
    assert SHARED_FILE in row["description"], row

    # The probe really runs against the work tree — and it is red while
    # the conflict is unresolved, which is the whole point of a probe.
    probe = subprocess.run(
        row["test_command"],
        cwd=str(world.work),
        shell=True,
        text=True,
        capture_output=True,
    )
    assert probe.returncode != 0, (probe.stdout, probe.stderr)


def test_worktree_is_clean_after_the_aborted_merge(
    world_factory, monkeypatch, tmp_path
):
    """The aborted merge leaves the work tree exactly as it was.

    The check aborts the merge it could not finish: the branch keeps its
    own commits, the working tree is clean again, and no merge is left in
    progress.  A half-finished merge left behind would poison every
    later round — the next ``git merge`` would refuse to start, and the
    branch would be stuck for a reason nobody could see.
    """
    _use_db(tmp_path, monkeypatch)
    world = world_factory(conflict=True)
    work = world.work
    orch = _orchestrator(tmp_path, world)

    head_before = _head(work)
    assert _porcelain(work) == ""
    assert head_before == world.feat_sha

    blocked = orch._run_base_drift_preflight()
    assert blocked is not None and blocked["overall_status"] == "BLOCKED", blocked

    # The branch did not move: it still sits on its own commit.
    assert _head(work) == head_before, "the aborted merge must not move HEAD"
    assert _head(work) == world.feat_sha

    # The working tree is clean — no conflict markers, no unmerged paths.
    assert _porcelain(work) == "", "the aborted merge must leave a clean tree"
    assert _git_quiet(work, "ls-files", "-u").stdout == "", (
        "no path may be left unmerged"
    )

    # No merge is in progress.
    assert _git_quiet(work, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode != 0, (
        "the merge must have been aborted"
    )

    # The file itself carries the branch's own content again.
    assert (work / SHARED_FILE).read_text(encoding="utf-8") == FEAT_CONTENT


def test_conflict_free_world_writes_no_task(world_factory, monkeypatch, tmp_path):
    """A world without a conflict writes no task — the negative control.

    The merge succeeds, the check reports ``merged``, the round is not
    blocked, and the database carries **no** base-drift row.  Without this
    case, the three tests above would also pass against a preflight that
    files a merge task for every branch it ever sees.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    world = world_factory(conflict=False)
    orch = _orchestrator(tmp_path, world)

    blocked = orch._run_base_drift_preflight()

    # The round is not blocked: there is nothing to block it.
    assert blocked is None, blocked

    # The check really did merge — otherwise "no task" would be trivial.
    report = orch._last_base_drift_report
    assert report.status == STATUS_MERGED, report
    assert report.merge_commit, report
    assert report.merge_commit != report.head_before, report

    # And no task was written.
    assert _load_rows(db_path, PLAN_ID) == {}

    # The base really was merged in: the work tree carries main's file.
    assert (world.work / OTHER_FILE).exists()
    assert _porcelain(world.work) == ""
