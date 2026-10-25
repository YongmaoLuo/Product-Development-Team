"""Integration tests for :mod:`verification_workspace` against a real git repository.

What this pins
--------------
A verification run derives its worktree from a *frozen* baseline — the
commit the reviewer approved — and never from the moving PR head.  Each
property below is a way to lose a run rather than a way to fail loudly,
so each one is driven against real ``git`` instead of a mock:

  1. **The baseline has one source.**  :func:`resolve_baseline` reads the
     plan state and nothing else.  A plan document rewritten after the
     review must not move the baseline: the review covered the bytes that
     were approved, and a rewritten document is a *new* document that has
     to go back through review.
  2. **The worktree is derived from the baseline commit.**  The branch is
     created at the baseline commit, and the PR head and the source
     checkout are left exactly as they were — the verified object belongs
     to somebody else.
  3. **Materialising twice is one worktree.**  The second call reuses
     what the first one made; it never creates a second worktree and
     never resets the branch the first one produced.
  4. **Reuse requires derivation.**  A worktree that was *not* derived
     from the baseline commit is a conflict, refused explicitly — never
     silently reset, because a reset would destroy the fixes a previous
     round committed.

The repository is built by the fixtures here: a local git repository with
two commits and a ``refs/pull/7/head`` pointing at the second one.  No
network, no real remote, no real home directory — the workspace root is
redirected into ``tmp_path`` for every test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_plan_doc import PLAN_DOC_FILENAME
from verification_workspace import (
    WORKSPACE_ROOT_ENV,
    Baseline,
    BaselineError,
    RefResolutionError,
    WorkspaceConflictError,
    WorkspaceRootError,
    ensure_workspace,
    resolve_baseline,
    resolve_ref_commit,
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


def _worktree_paths(repo: Path) -> list:
    """The paths git has registered as worktrees of ``repo``."""
    out = _git(repo, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """A local repository whose ``refs/pull/7/head`` points at the second commit.

    The first commit stands for the reviewed baseline's ancestor and the
    second for the PR head under verification; ``refs/pull/7/head`` is
    how a pull request head looks to a client that fetched the pull
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


# ---------------------------------------------------------------------------
# The plan state: where a frozen baseline lives
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path) -> Path:
    """A plan directory whose name is unique for this test.

    The plan id is the routing row's key, and the state database is
    shared by the whole worker — two plans with the same name would read
    each other's baseline.
    """
    plan_dir = tmp_path / f"plan-{tmp_path.name}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _record_baseline(plan_dir: Path, baseline: dict) -> None:
    """Write a frozen baseline into the plan state.

    The baseline lives in the ``verification`` column of the plan's
    routing row, so the test writes it through the repository layer
    rather than through a hand-made file — the reader under test must
    find it where the freeze step puts it.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_dir.name,
            phase="verification",
            completed_phases=[],
            review_rounds={},
            flags={},
            verification={
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
                "baseline": baseline,
            },
            last_updated=None,
        )
    finally:
        conn.close()


def _frozen_baseline(repo: Path) -> dict:
    """The baseline a review of ``repo`` would have frozen."""
    return {
        "repo_dir": str(repo),
        "ref": "refs/pull/7/head",
        "commit_sha": _git(repo, "rev-parse", "refs/pull/7/head"),
        "pr_number": 7,
    }


# ---------------------------------------------------------------------------
# 1. The baseline has one source.
# ---------------------------------------------------------------------------


def test_resolve_baseline_single_source(tmp_path: Path, upstream_repo: Path):
    """A rewritten document does not move the frozen baseline.

    The document names another repository and another pull request; the
    plan state still names the ones that were reviewed.  Reading the
    document instead of the plan state would verify a pull request
    nobody approved.
    """
    plan_dir = _plan_dir(tmp_path)
    frozen = _frozen_baseline(upstream_repo)
    _record_baseline(plan_dir, frozen)

    (plan_dir / PLAN_DOC_FILENAME).write_text(
        "## 被验证对象\n\n"
        "baseline: repo=/nonexistent/other-repo ref=refs/pull/9/head pr=9\n\n"
        "另一个被验证对象。\n",
        encoding="utf-8",
    )

    baseline = resolve_baseline(plan_dir)

    assert baseline.repo_dir == Path(frozen["repo_dir"])
    assert baseline.ref == "refs/pull/7/head"
    assert baseline.commit_sha == frozen["commit_sha"]
    assert baseline.pr_number == 7


def test_resolve_baseline_without_recorded_baseline(tmp_path: Path):
    """A plan that never froze a baseline says so instead of guessing one."""
    plan_dir = _plan_dir(tmp_path)

    with pytest.raises(BaselineError):
        resolve_baseline(plan_dir)


# ---------------------------------------------------------------------------
# 2. The worktree is derived from the baseline commit.
# ---------------------------------------------------------------------------


def test_ensure_workspace_creates_from_base_sha(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """The worktree is created at the baseline commit, on the derived branch.

    The PR head and the source checkout are snapshots: the flow may add
    a branch of its own, but it may not move the head under verification
    nor switch the checkout the operator is working in.
    """
    frozen = _frozen_baseline(upstream_repo)
    baseline = Baseline(
        repo_dir=upstream_repo,
        ref=frozen["ref"],
        commit_sha=frozen["commit_sha"],
        pr_number=frozen["pr_number"],
    )
    head_before = _git(upstream_repo, "rev-parse", "HEAD")
    pr_ref_before = _git(upstream_repo, "rev-parse", "refs/pull/7/head")
    branch_before = _git(upstream_repo, "symbolic-ref", "HEAD")

    workspace = ensure_workspace("p1", baseline)

    assert workspace.path == (workspace_root / "p1").resolve()
    assert workspace.branch == "fix/p1-verification"
    assert workspace.base_sha == frozen["commit_sha"]
    assert _git(workspace.path, "rev-parse", "HEAD") == frozen["commit_sha"]
    assert (
        _git(workspace.path, "rev-parse", "--abbrev-ref", "HEAD")
        == "fix/p1-verification"
    )
    # The verified object and the operator's checkout are untouched.
    assert _git(upstream_repo, "rev-parse", "refs/pull/7/head") == pr_ref_before
    assert _git(upstream_repo, "rev-parse", "HEAD") == head_before
    assert _git(upstream_repo, "symbolic-ref", "HEAD") == branch_before


def test_ensure_workspace_refuses_workspace_inside_repository(
    tmp_path: Path,
    upstream_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A workspace inside the repository under verification is refused.

    A worktree nested in a checkout is a checkout inside a checkout:
    the outer repository's own tooling walks into it, and a ``git clean``
    in the wrong directory deletes it.
    """
    inside = upstream_repo / "workspaces"
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(inside))
    frozen = _frozen_baseline(upstream_repo)
    baseline = Baseline(
        repo_dir=upstream_repo,
        ref=frozen["ref"],
        commit_sha=frozen["commit_sha"],
        pr_number=frozen["pr_number"],
    )

    with pytest.raises(WorkspaceRootError):
        ensure_workspace("p1", baseline)

    assert not inside.exists()


# ---------------------------------------------------------------------------
# 3. Materialising twice is one worktree.
# ---------------------------------------------------------------------------


def test_ensure_workspace_idempotent(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """The second call reuses the first one's worktree.

    Idempotency here is not a nicety: the verification entry point may
    be asked for the workspace more than once (a retried start, a second
    round), and a second worktree would leave two checkouts of the same
    branch fighting over the same fixes.
    """
    frozen = _frozen_baseline(upstream_repo)
    baseline = Baseline(
        repo_dir=upstream_repo,
        ref=frozen["ref"],
        commit_sha=frozen["commit_sha"],
        pr_number=frozen["pr_number"],
    )

    first = ensure_workspace("p1", baseline)
    second = ensure_workspace("p1", baseline)

    assert first == second
    registered = [
        path
        for path in _worktree_paths(upstream_repo)
        if Path(path).is_relative_to(workspace_root.resolve())
    ]
    assert registered == [str(first.path)]
    out = _git(upstream_repo, "worktree", "list", "--porcelain")
    assert out.count("branch refs/heads/fix/p1-verification") == 1


# ---------------------------------------------------------------------------
# 4. Reuse requires derivation from the baseline.
# ---------------------------------------------------------------------------


def test_reuse_requires_base_sha_in_history(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """A worktree that is not derived from the baseline is a conflict.

    The existing worktree was created from the *other* commit, so its
    history does not contain the reviewed baseline.  Reusing it would
    verify a tree nobody approved; resetting it would throw away
    whatever a previous round committed.  Both are refused.
    """
    other_sha = _git(upstream_repo, "rev-parse", "HEAD~1")
    existing = workspace_root / "p1"
    _git(
        upstream_repo,
        "worktree",
        "add",
        "-b",
        "fix/p1-verification",
        str(existing),
        other_sha,
    )
    frozen = _frozen_baseline(upstream_repo)
    baseline = Baseline(
        repo_dir=upstream_repo,
        ref=frozen["ref"],
        commit_sha=frozen["commit_sha"],
        pr_number=frozen["pr_number"],
    )

    with pytest.raises(WorkspaceConflictError):
        ensure_workspace("p1", baseline)

    # Refused, not reset: the existing worktree still holds what it had.
    assert _git(existing, "rev-parse", "HEAD") == other_sha


def test_reuse_accepts_workspace_derived_from_the_baseline(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """A worktree whose history contains the baseline is reused as is.

    This is the re-verification path: the fixes of a previous round sit
    on top of the baseline, and the workspace has to come back exactly
    as it was.
    """
    frozen = _frozen_baseline(upstream_repo)
    baseline = Baseline(
        repo_dir=upstream_repo,
        ref=frozen["ref"],
        commit_sha=frozen["commit_sha"],
        pr_number=frozen["pr_number"],
    )
    first = ensure_workspace("p1", baseline)
    # A fix commit on top of the baseline, the way a repair round ends.
    (first.path / "fix.txt").write_text("fixed\n", encoding="utf-8")
    _git(first.path, "add", "fix.txt")
    _git(first.path, "commit", "--quiet", "-m", "fix")
    fix_sha = _git(first.path, "rev-parse", "HEAD")

    again = ensure_workspace("p1", baseline)

    assert again.path == first.path
    assert _git(again.path, "rev-parse", "HEAD") == fix_sha


def test_ensure_workspace_recreates_a_deleted_workspace(
    tmp_path: Path,
    upstream_repo: Path,
    workspace_root: Path,
):
    """A workspace deleted behind the flow's back comes back.

    The registration git keeps outlives the directory when the directory
    is removed by hand.  Handing that registration back as a workspace
    would hand back a path that does not exist, so the stale
    registration is dropped and the workspace is materialised again.
    """
    frozen = _frozen_baseline(upstream_repo)
    baseline = Baseline(
        repo_dir=upstream_repo,
        ref=frozen["ref"],
        commit_sha=frozen["commit_sha"],
        pr_number=frozen["pr_number"],
    )
    first = ensure_workspace("p1", baseline)
    shutil.rmtree(first.path)

    again = ensure_workspace("p1", baseline)

    assert again.path == first.path
    assert again.path.is_dir()
    assert _git(again.path, "rev-parse", "HEAD") == frozen["commit_sha"]
    assert (
        _git(again.path, "rev-parse", "--abbrev-ref", "HEAD")
        == "fix/p1-verification"
    )


# ---------------------------------------------------------------------------
# The ref resolution the freeze step depends on.
# ---------------------------------------------------------------------------


def test_resolve_ref_commit_resolves_the_pr_head(upstream_repo: Path):
    """The PR head ref resolves to the commit it points at."""
    expected = _git(upstream_repo, "rev-parse", "refs/pull/7/head")

    assert resolve_ref_commit(upstream_repo, "refs/pull/7/head") == expected


def test_resolve_ref_commit_rejects_a_missing_repository(tmp_path: Path):
    """A repository that is not there is an explicit error."""
    with pytest.raises(RefResolutionError):
        resolve_ref_commit(tmp_path / "no-such-repo", "refs/pull/7/head")


def test_resolve_ref_commit_rejects_an_unresolvable_ref(upstream_repo: Path):
    """A ref that does not exist is an explicit error, not an empty SHA."""
    with pytest.raises(RefResolutionError):
        resolve_ref_commit(upstream_repo, "refs/pull/999/head")


def test_resolve_ref_commit_refuses_an_option_like_ref(upstream_repo: Path):
    """A ref that looks like a git option never reaches git.

    ``--upload-pack=...`` is not a ref; handed to git as one it is an
    option that runs a command.  The refusal happens before the call.
    """
    with pytest.raises(RefResolutionError) as excinfo:
        resolve_ref_commit(upstream_repo, "--upload-pack=touch /tmp/pwned")

    assert "option" in str(excinfo.value)
