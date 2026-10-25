"""Where a plan's verification worktree lives, and what may not be inside it.

Background
----------
A verification run has to keep its hands off the tree the operator is
working in: the whole point of the verification branch is that the fixes
it produces land on their own branch, in their own worktree, without the
operator's uncommitted changes being swept into a verdict. Three things
therefore have to be decided *before* anything is created, and all three
are derived rather than typed:

  * **the branch name** — ``fix/<plan_id>-verification``, derived from
    the plan id so two plans can never land on one branch;
  * **the workspace directory** — ``~/.pdt/verification-workspaces/<plan_id>``,
    a per-user tree that belongs to no repository;
  * **the boundary check** — a workspace whose *resolved* path sits inside
    a repository is refused, because a worktree nested in a checkout is a
    checkout inside a checkout.

The object under verification is somebody else's: it is the head of an
external pull request. Nothing here may switch it, commit to it or
reset it. The worktree is derived from the *frozen* baseline instead —
the commit the review approved — and lands on a branch of our own.

Why the guards exist
--------------------
Each of these is a way to lose a run quietly rather than a way to fail
loudly:

* **A branch name typed by hand** collides with the next plan's run, and
  the collision shows up as "the wrong diff was verified" rather than as
  an error. Deriving it from the plan id removes the collision, and
  routing the plan id through :func:`framework.ids.validate_plan_id`
  removes the path-separator, traversal and control-byte vectors from
  the name that reaches git.
* **A workspace inside a repository** is picked up by that repository's
  own tooling: ``git status`` lists it, a recursive scan walks into it,
  and a ``git clean`` in the wrong directory deletes it. The workspace
  therefore has to sit outside both the PDT checkout and the repository
  under test.
* **A boundary check on the strings it was handed** is not a boundary
  check. ``/tmp/link-to-repo`` and ``/tmp/workspace/../repo`` are both
  outside the repository as written and both inside it once resolved, so
  the comparison happens on ``Path.resolve()`` results — symlinks and
  ``..`` included.

Guards and materialisation
---------------------------
Two halves live here, and they are kept apart on purpose.

The **guards** (:func:`branch_name`, :func:`workspace_root`,
:func:`validate_workspace_path`) create nothing: they declare a
location and answer a question. A module that created directories as a
side effect of a *check* would make the check un-runnable — you could
not ask "is this allowed?" without also creating the thing you were
asking about.

The **materialisation** (:func:`ensure_workspace`) does create
something: a git worktree, derived from the frozen baseline, on a
branch named after the plan. It is the only function here that touches
the filesystem or runs git, and it is the only one that is allowed to.

The **handover** (:func:`register_project_dir`) is the third half. It
creates nothing on disk: it writes the worktree's path into the one
field the execution chain already reads — ``plan_execution.project_dir``
— so the repair generator, the executor and the verification loop run
inside the worktree without any of them changing. It reuses the
repository layer that column already has an owner for, and adds no
table and no column of its own.

The baseline has exactly one source
-----------------------------------
:func:`resolve_baseline` reads the plan state's ``verification.baseline``
and nothing else.  The verification plan document is deliberately *not*
consulted: the document is what the review covered, and a document
edited after the review is a different document that has to go back
through review.  Reading it here would let an edit silently move the
baseline away from the commit somebody approved.

The baseline is frozen
----------------------
``commit_sha`` is the commit the reference resolved to **at review
time**.  A pull request head moves; the reviewed commit does not.  The
worktree is derived from that commit, so a re-run of the verification
verifies the same tree even though the head has moved on.

Reuse is allowed only for a worktree derived from that commit.  A
worktree whose history does not contain it is refused, never reset: a
reset would throw away the fixes a previous round committed, and a
silent reuse would verify a tree nobody approved.

The environment variable :data:`WORKSPACE_ROOT_ENV` relocates the whole
workspace tree, which is the seam a test or an operator with a different
disk layout needs. It wins over the default location, and it is subject
to the same boundary check — an override that points into a repository
is refused exactly like a default that does.

Which root counts as "a repository"
-----------------------------------
The PDT checkout's boundary is :data:`config_paths.PROJECT_ROOT` — the
same directory as ``server._PROJECT_ROOT``, both being the repository
root one level above ``backend/``. It is read from :mod:`config_paths`
rather than from :mod:`server` so that this leaf module never has to
import the FastAPI app to learn where the repository is; the two are
pinned to agree by
``tests/static_gates/test_extracted_modules_use_app_root.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from bounded_subprocess import run_bounded

# ``PROJECT_ROOT`` is the PDT checkout's root — the same directory as
# ``server._PROJECT_ROOT`` (both are the repository root, one level above
# ``backend/``). It is read from :mod:`config_paths` so this leaf module
# never has to import the FastAPI app to learn where the repository is.
from config_paths import PROJECT_ROOT
from framework.ids import InvalidPlanIdError, validate_plan_id

__all__ = [
    "BRANCH_PREFIX",
    "BRANCH_SUFFIX",
    "WORKSPACE_ROOT_ENV",
    "Baseline",
    "BaselineError",
    "RefResolutionError",
    "VerificationWorkspaceError",
    "Workspace",
    "WorkspaceConflictError",
    "WorkspaceRootError",
    "branch_name",
    "ensure_workspace",
    "register_project_dir",
    "resolve_baseline",
    "resolve_ref_commit",
    "validate_workspace_path",
    "workspace_root",
]

#: Environment variable that relocates the verification workspace tree.
#:
#: Read on every call (never cached) so a test that sets it after import
#: still gets the redirected root — the same rule
#: :func:`config_paths.resolve_plans_dir` follows.
WORKSPACE_ROOT_ENV: str = "PDT_VERIFICATION_WORKSPACE_ROOT"

#: The per-user directory the workspaces live under, relative to ``$HOME``.
#:
#: ``.pdt`` is the same dot-directory convention the repository already
#: uses for its own runtime state (``.pdt/`` in the checkout), and it is
#: outside every repository by construction.
_WORKSPACE_HOME_DIR: str = ".pdt"

#: The directory name the workspaces live under.
_WORKSPACE_DIR_NAME: str = "verification-workspaces"

#: The branch-name shape: ``<prefix>/<plan_id>-<suffix>``.
#:
#: The prefix keeps the branch on the ``fix/`` namespace (a verification
#: run produces fixes), and the suffix says what the branch is for — a
#: branch named after the plan id alone would be indistinguishable from
#: the branch that carries the plan's implementation.
BRANCH_PREFIX: str = "fix"
BRANCH_SUFFIX: str = "verification"


class WorkspaceRootError(ValueError):
    """Raised when a verification workspace path is not allowed.

    Subclass of :class:`ValueError` so a caller that already handles bad
    input as a ``ValueError`` keeps working, while a caller that wants to
    tell "this path is not allowed" from "this other argument is wrong"
    can pin the type exactly.

    Attributes:
        path: The offending path, as the guard saw it.
        reason: Why it was refused.
        blocked_root: The repository root the path was found inside, when
            the refusal came from a boundary.
    """

    def __init__(
        self,
        path: object,
        reason: str,
        blocked_root: Optional[Path] = None,
    ) -> None:
        self.path = str(path)
        self.reason = reason
        self.blocked_root = str(blocked_root) if blocked_root is not None else None
        detail = f"{self.path} ({self.reason})"
        if self.blocked_root is not None:
            detail += f"; it is inside {self.blocked_root}"
        super().__init__(f"verification workspace path is not allowed: {detail}")


def _validated_plan_id(plan_id: object) -> str:
    """Return ``plan_id`` after both the path rules and the git-ref rules.

    :func:`framework.ids.validate_plan_id` already refuses everything
    that could escape a path — separators, traversal components, absolute
    paths, control bytes, empty strings and over-long ids.

    One rule is added on top, and it comes from git rather than from the
    filesystem: a git ref may not contain ``..`` anywhere in its name.
    The plan-id byte set permits ``.``, so an id such as ``a..b`` passes
    the path rules and would still be refused by ``git worktree add``.
    Rejecting it here turns "git said no" into "the plan id is not
    usable", at the point where the caller can still do something about
    it.

    The same rule applies to the workspace directory, so both entry
    points share one policy: a plan id that cannot name a branch cannot
    name a workspace either.
    """
    validated = validate_plan_id(plan_id)
    if ".." in validated:
        raise InvalidPlanIdError(
            f"plan_id {validated!r} contains '..'; a git branch name may not"
        )
    return validated


def branch_name(plan_id: str) -> str:
    """Return the verification branch name for ``plan_id``.

    The name is derived, never taken from a caller: the only input is
    the plan id, and the plan id is validated first.

    Args:
        plan_id: The plan the verification run belongs to.

    Returns:
        ``fix/<plan_id>-verification``.

    Raises:
        InvalidPlanIdError: The plan id is not usable as a path component
            or as a git ref.
    """
    validated = _validated_plan_id(plan_id)
    return f"{BRANCH_PREFIX}/{validated}-{BRANCH_SUFFIX}"


def workspace_root(plan_id: Optional[str] = None) -> Path:
    """Return the directory a verification workspace lives in.

    Called without an argument it returns the *base* root — the directory
    the per-plan workspaces hang off. Called with a plan id it returns
    that plan's workspace directory.

    The base root is :data:`WORKSPACE_ROOT_ENV` when that variable is
    set, and ``~/.pdt/verification-workspaces`` otherwise.

    Nothing is created: this function declares a location, and the caller
    decides whether to make the directory.

    Args:
        plan_id: The plan whose workspace directory is wanted. Optional.

    Returns:
        The workspace directory, as a path.

    Raises:
        InvalidPlanIdError: The plan id is not usable as a path component
            or as a git ref.
        WorkspaceRootError: The root resolves inside the PDT checkout, or
            the override is not an absolute path.
    """
    root = _base_workspace_root()
    if plan_id is not None:
        root = root / _validated_plan_id(plan_id)
    _refuse_inside(root, PROJECT_ROOT, "the PDT repository")
    return root


def validate_workspace_path(path: Path, repo_dir: Path) -> Path:
    """Return ``path`` resolved, or refuse it if it sits in a repository.

    ``path`` is the workspace path a caller intends to use and
    ``repo_dir`` is the toplevel of the repository under verification.
    Both are resolved before they are compared, so a symlink or a ``..``
    component that leads back into a repository is caught.

    Args:
        path: The workspace path to check.
        repo_dir: The toplevel of the repository the workspace must not
            be inside.

    Returns:
        The resolved, absolute workspace path.

    Raises:
        WorkspaceRootError: The path resolves inside the repository under
            verification, or inside the PDT checkout.
    """
    resolved = Path(path).expanduser().resolve()
    _refuse_inside(resolved, repo_dir, "the repository under verification")
    _refuse_inside(resolved, PROJECT_ROOT, "the PDT repository")
    return resolved


# ---------------------------------------------------------------------------
# The baseline and the workspace it derives
# ---------------------------------------------------------------------------


class VerificationWorkspaceError(RuntimeError):
    """Base class for the failures this module raises.

    Subclass of :class:`RuntimeError`: every one of these is "the
    verification workspace cannot be materialised as asked", never "the
    caller passed a bad argument" — the arguments are derived, and a
    derived argument that cannot work is a runtime condition.
    """


class BaselineError(VerificationWorkspaceError):
    """Raised when the plan state carries no usable baseline.

    The baseline is a machine fact frozen at review time.  A plan
    without one has nothing to verify *against*, and guessing a
    baseline would verify a tree nobody approved.
    """


class RefResolutionError(VerificationWorkspaceError):
    """Raised when the baseline's repository or ref cannot be resolved.

    Covers a repository that is not there, a directory that is not a git
    repository, and a ref git cannot turn into a commit — including a
    ref that looks like a git option and is therefore never handed to
    git at all.
    """


class WorkspaceConflictError(VerificationWorkspaceError):
    """Raised when an existing worktree may not be reused.

    The reuse rule is one sentence: a worktree may be reused only when
    its history contains the reviewed baseline, i.e. when it was
    derived from it.  Anything else is refused — never reset, because a
    reset would throw away the fixes a previous round committed.
    """


@dataclass(frozen=True)
class Baseline:
    """The frozen baseline a verification run works from.

    ``repo_dir`` is the repository under verification, ``ref`` the
    reference the baseline was read from (``refs/pull/<n>/head`` for a
    pull request head), ``commit_sha`` the commit that reference
    resolved to **at review time**, and ``pr_number`` the pull request
    the head came from (``None`` when the baseline is not a pull
    request head).

    ``commit_sha`` is the load-bearing field: it is what makes the
    baseline *frozen*.  A pull request head moves; the reviewed commit
    does not.
    """

    repo_dir: Path
    ref: str
    commit_sha: str
    pr_number: Optional[int]


@dataclass(frozen=True)
class Workspace:
    """A materialised verification worktree.

    ``path`` is the worktree's directory, ``branch`` the branch it is
    checked out on, and ``base_sha`` the baseline commit it was
    derived from.
    """

    path: Path
    branch: str
    base_sha: str


def resolve_baseline(plan_dir: Path) -> Baseline:
    """Return the baseline the plan state has frozen.

    The plan state is the only source.  The verification plan document
    is not read: it is what the review covered, and a document edited
    afterwards is a different document that has to go back through
    review.  The caller cannot pass a baseline in either — a baseline
    that arrives as an argument is a baseline nobody approved.

    Args:
        plan_dir: The plan's directory.

    Returns:
        The frozen baseline.

    Raises:
        BaselineError: The plan state carries no baseline, or carries
            one whose fields are unusable.
    """
    recorded = _recorded_baseline(Path(plan_dir))
    if recorded is None:
        raise BaselineError(
            f"the plan state of {plan_dir} carries no verification.baseline; "
            "the baseline is frozen when the verification plan is approved"
        )
    return _baseline_from_recorded(recorded)


def resolve_ref_commit(repo_dir: Path, ref: str) -> str:
    """Return the commit ``ref`` names inside ``repo_dir``.

    This is the step that turns a *reference* into the *commit* the
    baseline freezes.  It runs against a real repository, so it is the
    one place where a ref that does not exist becomes an error instead
    of an empty answer.

    Args:
        repo_dir: The repository the ref lives in.
        ref: Any reference git understands — a branch, a tag, a commit
            SHA, ``refs/pull/<n>/head``.

    Returns:
        The full commit SHA the ref names.

    Raises:
        RefResolutionError: The repository does not exist, is not a git
            repository, the ref does not resolve, or the ref looks like
            a git option and is therefore refused before git sees it.
    """
    repo = Path(repo_dir).expanduser()
    if not repo.is_dir():
        raise RefResolutionError(f"the repository does not exist: {repo}")
    if not isinstance(ref, str) or not ref.strip():
        raise RefResolutionError("the ref must be a non-empty string")
    if ref.startswith("-"):
        # A ref that starts with a dash is an *option* to git, not a
        # name.  Handing it over would let a recorded baseline run a
        # command (``--upload-pack=...``); it is refused here instead.
        raise RefResolutionError(
            f"the ref {ref!r} looks like a git option and is refused "
            "before it reaches git"
        )
    sha = _rev_parse_commit(repo, ref)
    if not _FULL_SHA_RE.match(sha):
        raise RefResolutionError(
            f"the ref {ref!r} did not resolve to a commit SHA (got {sha!r})"
        )
    return sha


def ensure_workspace(plan_id: str, baseline: Baseline) -> Workspace:
    """Materialise the verification worktree for ``plan_id``.

    The worktree is derived from ``baseline.commit_sha`` — the commit
    the review approved — on the branch :func:`branch_name` derives from
    the plan id.  The pull request head and the source checkout are
    never switched, committed to or reset: the verified object belongs
    to somebody else.

    The call is idempotent.  Called twice it yields the same path and
    the same branch, and the second call reuses the worktree the first
    one made instead of creating a second one.

    A worktree is reused only when it was *derived* from the baseline —
    when its history contains the baseline commit.  Anything else is
    refused with a :class:`WorkspaceConflictError`: reusing it would
    verify a tree nobody approved, and resetting it would throw away
    the fixes a previous round committed.

    Args:
        plan_id: The plan the verification run belongs to.
        baseline: The frozen baseline to derive from.

    Returns:
        The materialised workspace.

    Raises:
        BaselineError: The baseline's commit is not a full commit SHA.
        RefResolutionError: The baseline's repository is missing or is
            not a git repository.
        WorkspaceRootError: The workspace path resolves inside a
            repository.
        WorkspaceConflictError: An existing worktree may not be reused.
    """
    branch = branch_name(plan_id)
    repo_dir = Path(baseline.repo_dir).expanduser()
    if not repo_dir.is_dir():
        raise RefResolutionError(f"the baseline repository does not exist: {repo_dir}")
    base_sha = baseline.commit_sha
    if not isinstance(base_sha, str) or not _FULL_SHA_RE.match(base_sha):
        raise BaselineError(
            f"the baseline commit_sha {base_sha!r} is not a full commit SHA"
        )
    # The boundary is the *repository*, not the directory the baseline
    # happened to name: a worktree inside any of the repository's
    # worktrees is a checkout inside a checkout.
    toplevel = _repo_toplevel(repo_dir)
    ws_path = validate_workspace_path(workspace_root(plan_id), toplevel)

    with _plan_lock(plan_id):
        entry = _registered_worktree(repo_dir, ws_path)
        if entry is not None:
            registered = Path(entry.path)
            if registered.is_dir():
                return _reuse(repo_dir, ws_path, branch, base_sha, entry)
            if registered.exists():
                raise WorkspaceConflictError(
                    f"the path {registered} is registered as a worktree but is "
                    "not a directory; refusing to touch it"
                )
            # The registration outlived its directory — the workspace was
            # deleted behind the flow's back.  Reusing the registration
            # would hand back a path that does not exist, so the stale
            # registration is dropped and the workspace is materialised
            # again.  Nothing is deleted here: the directory is already
            # gone, and the branch keeps every commit it had.
            _drop_stale_worktree(repo_dir, entry)
        try:
            _materialise(repo_dir, ws_path, branch, base_sha)
        except WorkspaceConflictError:
            # Another materialisation may have won the race for this
            # path.  The registry decides, not the error.
            if _registered_worktree(repo_dir, ws_path) is None:
                raise
        entry = _registered_worktree(repo_dir, ws_path)
        if entry is None:
            raise WorkspaceConflictError(
                f"git worktree add did not register a worktree at {ws_path}"
            )
        return _reuse(repo_dir, ws_path, branch, base_sha, entry)


def register_project_dir(plan_id: str, path: Path) -> None:
    """Register ``path`` as the plan's project directory.

    The execution chain reads exactly one place for "where does this
    plan's work happen": the ``project_dir`` column of
    ``plan_execution``.  The repair generator, the executor and the
    verification loop all resolve it through that one column, so
    writing the verification worktree's path there is what makes them
    run *inside the worktree* — none of them has to learn that a
    verification workspace exists, and none of them changes.

    The write goes through :class:`ExecutionRepository`, the layer every
    other writer of that column uses.  No table and no column is added:
    the worktree takes over a field that was already there, and the
    field survives a restart.

    The server's in-memory execution record is refreshed as well, when
    there is one.  :func:`server._get_project_dir` answers from that
    record before it falls back to the database, so a plan whose
    execution has already started would otherwise keep answering with
    the directory it started in — the column would be right and every
    reader would still see the old tree.

    Idempotent, and safe to call from several threads at once: the
    write is one SQLite statement, so the last writer wins and no
    interleaving can leave a half-written value behind.

    Args:
        plan_id: The plan whose project directory is being registered.
        path: The directory the plan's work happens in — the
            verification worktree's path.

    Raises:
        InvalidPlanIdError: The plan id is not usable as a path
            component or as a git ref.
        WorkspaceRootError: The path is not absolute.
    """
    validated = _validated_plan_id(plan_id)
    target = Path(path).expanduser()
    if not target.is_absolute():
        # A relative directory would resolve against whatever directory
        # the process happens to be in, and the column is read by
        # subprocesses that run somewhere else entirely.
        raise WorkspaceRootError(
            target,
            "the project directory must be an absolute path; "
            "a relative one would depend on the process's directory",
        )
    resolved = str(target.resolve())
    _write_project_dir(validated, resolved)
    _refresh_execution_record(validated, resolved)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

#: A full commit SHA — 40 hex characters (SHA-1) or 64 (SHA-256).
#:
#: The baseline's ``commit_sha`` has to be a *commit*, never a
#: reference: a reference would be re-resolved on every use, and a pull
#: request head that moved between the review and the run would then
#: verify a tree nobody approved.
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

#: How long a git call may take before it is killed.
_GIT_TIMEOUT_SECONDS = 60

#: One lock per plan id, so two threads asking for the same plan's
#: workspace at the same time end up with one worktree rather than two.
_PLAN_LOCKS: Dict[str, threading.Lock] = {}
_PLAN_LOCKS_GUARD = threading.Lock()


def _write_project_dir(plan_id: str, project_dir: str) -> None:
    """Write ``project_dir`` into the plan's ``plan_execution`` row.

    The row is created when it does not exist yet: the worktree is
    materialised before the plan's execution row necessarily does, and a
    registration that refused to write because of a missing row would
    make the caller guess whether the plan is known.

    The repository is reached through the same layer every other writer
    of that column uses — no table and no column of its own.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            project_dir=project_dir,
        )
    finally:
        conn.close()


def _refresh_execution_record(plan_id: str, project_dir: str) -> None:
    """Point the server's in-memory execution record at ``project_dir``.

    The server module is looked up in ``sys.modules`` rather than
    imported: this module is a leaf that must not pull the FastAPI
    application in, and when the server is not running there is no
    in-memory record to refresh in the first place.
    """
    server = sys.modules.get("server")
    if server is None:
        return
    records = getattr(server, "_execution_state", None)
    if not isinstance(records, dict):
        return
    record = records.get(plan_id)
    if not isinstance(record, dict):
        return
    lock = getattr(server, "_execution_state_lock", None)
    if lock is None:
        record["project_dir"] = project_dir
        return
    with lock:
        record["project_dir"] = project_dir


@dataclass(frozen=True)
class _WorktreeEntry:
    """One entry of ``git worktree list --porcelain``."""

    path: str
    head: str
    branch: Optional[str]

    @classmethod
    def from_mapping(cls, mapping: Dict[str, str]) -> "_WorktreeEntry":
        branch = mapping.get("branch")
        return cls(
            path=mapping.get("worktree", ""),
            head=mapping.get("HEAD", ""),
            branch=branch.split("refs/heads/", 1)[-1] if branch else None,
        )


def _recorded_baseline(plan_dir: Path) -> Optional[dict]:
    """Return the ``verification.baseline`` mapping the plan state holds.

    :class:`plan_state.PlanState` is imported here rather than at
    module level: this module is a leaf that other code imports for its
    guards alone, and the state layer is not needed to answer "is this
    path allowed?".

    The baseline is read from the plan state and from nowhere else.  The
    verification plan document is not consulted — it is what the review
    covered, and a document edited after the review is a different
    document that has to go back through review.
    """
    from plan_state import PlanState

    state = PlanState(plan_dir)
    verification = state.get_state().get("verification") or {}
    recorded = verification.get("baseline")
    return recorded if isinstance(recorded, dict) else None


def _baseline_from_recorded(recorded: dict) -> Baseline:
    """Turn a recorded baseline mapping into a :class:`Baseline`."""
    repo_dir = recorded.get("repo_dir")
    if not isinstance(repo_dir, str) or not repo_dir.strip():
        raise BaselineError("the recorded baseline has no repo_dir")
    repo_path = Path(repo_dir).expanduser()
    if not repo_path.is_absolute():
        raise BaselineError(
            f"the recorded baseline repo_dir {repo_dir!r} is not an absolute path"
        )
    ref = recorded.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise BaselineError("the recorded baseline has no ref")
    commit_sha = recorded.get("commit_sha")
    if not isinstance(commit_sha, str) or not _FULL_SHA_RE.match(commit_sha):
        raise BaselineError(
            f"the recorded baseline commit_sha {commit_sha!r} is not a full commit SHA"
        )
    return Baseline(
        repo_dir=repo_path,
        ref=ref,
        commit_sha=commit_sha,
        pr_number=_pr_number(recorded.get("pr_number")),
    )


def _pr_number(value: object) -> Optional[int]:
    """Return the recorded pull request number, or ``None``.

    ``None`` means the baseline is not a pull request head.  Anything
    that is neither a number nor null is a malformed baseline, not a
    number to be guessed at.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise BaselineError(
            f"the recorded baseline pr_number {value!r} is neither a number nor null"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise BaselineError(
        f"the recorded baseline pr_number {value!r} is neither a number nor null"
    )


def _run_git(repo_dir: Path, args: List[str]) -> subprocess.CompletedProcess:
    """Run ``git`` inside ``repo_dir`` and return the completed process.

    ``shell=False`` with an argv list: the arguments come from the plan
    state, and a shell would hand them a second grammar to escape into.
    """
    try:
        return run_bounded(
            ["git", "-C", str(repo_dir), *args],
            shell=False,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RefResolutionError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s "
            f"in {repo_dir}"
        ) from exc


def _git_error(completed: subprocess.CompletedProcess) -> str:
    """Return git's own complaint, for an error message."""
    return (
        (completed.stderr or completed.stdout or "").strip()
        or f"git exited with {completed.returncode}"
    )


def _end_of_options_unsupported(stderr: str) -> bool:
    """Whether git is too old to know ``--end-of-options``.

    The separator arrived in git 2.24.  On an older git the flag is
    itself an unknown option, and the call has to be retried without
    it — the ref was already refused if it looked like an option, so
    the plain call carries no injection risk.
    """
    text = (stderr or "").lower()
    return "end-of-options" in text and (
        "unknown option" in text or "unknown switch" in text
    )


def _rev_parse_commit(repo: Path, ref: str) -> str:
    """Return the commit ``ref`` names, or raise."""
    target = f"{ref}^{{commit}}"
    completed = _run_git(repo, ["rev-parse", "--verify", "--end-of-options", target])
    if completed.returncode != 0 and _end_of_options_unsupported(completed.stderr):
        completed = _run_git(repo, ["rev-parse", "--verify", target])
    if completed.returncode != 0:
        raise RefResolutionError(
            f"the ref {ref!r} does not resolve to a commit in {repo}: "
            f"{_git_error(completed)}"
        )
    return (completed.stdout or "").strip()


def _repo_toplevel(repo_dir: Path) -> Path:
    """Return the toplevel of the repository ``repo_dir`` belongs to."""
    completed = _run_git(repo_dir, ["rev-parse", "--show-toplevel"])
    if completed.returncode != 0:
        raise RefResolutionError(
            f"{repo_dir} is not a git repository: {_git_error(completed)}"
        )
    toplevel = (completed.stdout or "").strip()
    if not toplevel:
        raise RefResolutionError(f"{repo_dir} is not a git repository")
    return Path(toplevel)


def _plan_lock(plan_id: str) -> threading.Lock:
    """Return the in-process lock for ``plan_id``.

    Two threads asking for the same plan's workspace at the same time
    must end up with one worktree, not two: git refuses the second
    ``worktree add`` for the same branch, and the caller would see an
    error instead of the workspace that already exists.
    """
    with _PLAN_LOCKS_GUARD:
        lock = _PLAN_LOCKS.get(plan_id)
        if lock is None:
            lock = threading.Lock()
            _PLAN_LOCKS[plan_id] = lock
        return lock


def _worktree_entries(repo_dir: Path) -> List[_WorktreeEntry]:
    """Return the worktrees git has registered for ``repo_dir``."""
    completed = _run_git(repo_dir, ["worktree", "list", "--porcelain"])
    if completed.returncode != 0:
        return []
    entries: List[_WorktreeEntry] = []
    current: Dict[str, str] = {}
    for raw in (completed.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            if current:
                entries.append(_WorktreeEntry.from_mapping(current))
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(_WorktreeEntry.from_mapping(current))
    return entries


def _registered_worktree(repo_dir: Path, ws_path: Path) -> Optional[_WorktreeEntry]:
    """Return the worktree registered at ``ws_path``, if there is one."""
    target = ws_path.resolve()
    for entry in _worktree_entries(repo_dir):
        try:
            path = Path(entry.path).resolve()
        except OSError:  # pragma: no cover - an unreadable path is not ours
            continue
        if path == target:
            return entry
    return None


def _branch_tip(repo_dir: Path, branch: str) -> Optional[str]:
    """Return the commit the branch points at, or ``None`` if it is absent."""
    completed = _run_git(
        repo_dir, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    if completed.returncode != 0:
        return None
    tip = (completed.stdout or "").strip()
    return tip or None


def _branch_checked_out_elsewhere(repo_dir: Path, branch: str, ws_path: Path) -> bool:
    """Whether ``branch`` is checked out in a worktree other than ``ws_path``."""
    target = ws_path.resolve()
    for entry in _worktree_entries(repo_dir):
        if entry.branch != branch:
            continue
        try:
            same_path = Path(entry.path).resolve() == target
        except OSError:  # pragma: no cover - an unreadable path is not ours
            same_path = False
        if not same_path:
            return True
    return False


def _history_contains(repo_dir: Path, base_sha: str, head: str) -> bool:
    """Whether ``head``'s history contains ``base_sha``.

    ``head`` is passed explicitly rather than as ``HEAD``: the source
    checkout's ``HEAD`` is the operator's branch, and asking whether
    *it* descends from the baseline would answer a question about the
    wrong tree.
    """
    if not head:
        return False
    completed = _run_git(repo_dir, ["merge-base", "--is-ancestor", base_sha, head])
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise WorkspaceConflictError(
        f"could not compare the baseline {base_sha} with {head} in {repo_dir} "
        f"— is the baseline commit in this repository? "
        f"{_git_error(completed)}"
    )


def _reuse(
    repo_dir: Path,
    ws_path: Path,
    branch: str,
    base_sha: str,
    entry: _WorktreeEntry,
) -> Workspace:
    """Reuse an existing worktree, or refuse it.

    The rule is one sentence: a worktree may be reused only when it was
    derived from the reviewed baseline.  A worktree on another branch,
    or one whose history does not contain the baseline, is a conflict —
    never reset, because a reset would throw away the fixes a previous
    round committed.
    """
    if entry.branch is not None and entry.branch != branch:
        raise WorkspaceConflictError(
            f"the worktree at {ws_path} is checked out on {entry.branch!r}, "
            f"not on {branch!r}; refusing to reuse it"
        )
    if not _history_contains(repo_dir, base_sha, entry.head):
        raise WorkspaceConflictError(
            f"the worktree at {ws_path} was not derived from the reviewed "
            f"baseline {base_sha}; refusing to reuse or reset it"
        )
    return Workspace(path=ws_path, branch=branch, base_sha=base_sha)


def _drop_stale_worktree(repo_dir: Path, entry: _WorktreeEntry) -> None:
    """Remove a worktree registration whose directory is gone.

    Only the registration goes: the directory is already gone, and the
    branch keeps every commit it had.  The removal is aimed at this one
    path, so another plan's worktree is never touched.
    """
    completed = _run_git(repo_dir, ["worktree", "remove", "--force", entry.path])
    if completed.returncode != 0:
        raise WorkspaceConflictError(
            f"the worktree registered at {entry.path} is gone but git would "
            f"not drop the registration: {_git_error(completed)}"
        )


def _materialise(repo_dir: Path, ws_path: Path, branch: str, base_sha: str) -> None:
    """Create the worktree at ``ws_path`` from the baseline commit."""
    ws_path.parent.mkdir(parents=True, exist_ok=True)
    tip = _branch_tip(repo_dir, branch)
    if tip is None:
        args = ["worktree", "add", "-b", branch, str(ws_path), base_sha]
    else:
        # The branch outlived its worktree directory — a previous round
        # that was cleaned up by hand rather than by the flow.  It is
        # re-attached only when it still descends from the reviewed
        # baseline; a branch that does not is somebody else's work and
        # is never taken over.
        if _branch_checked_out_elsewhere(repo_dir, branch, ws_path):
            raise WorkspaceConflictError(
                f"the branch {branch!r} is already checked out in another "
                "worktree; refusing to take it over"
            )
        if not _history_contains(repo_dir, base_sha, tip):
            raise WorkspaceConflictError(
                f"the branch {branch!r} exists but does not descend from the "
                f"reviewed baseline {base_sha}; refusing to reset it"
            )
        args = ["worktree", "add", str(ws_path), branch]
    completed = _run_git(repo_dir, args)
    if completed.returncode != 0:
        raise WorkspaceConflictError(
            f"git worktree add failed for {ws_path}: {_git_error(completed)}"
        )


def _base_workspace_root() -> Path:
    """Return the directory the per-plan workspaces hang off.

    The environment variable wins over the default; it is read on every
    call so a test can set it after import.
    """
    override = os.environ.get(WORKSPACE_ROOT_ENV)
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceRootError(
                candidate,
                "the workspace root must be an absolute path; "
                "a relative one would depend on the process's directory",
            )
        return candidate
    return Path.home() / _WORKSPACE_HOME_DIR / _WORKSPACE_DIR_NAME


def _is_within(child: Path, parent: Path) -> bool:
    """Return whether ``child`` is ``parent`` or sits under it."""
    return child == parent or parent in child.parents


def _refuse_inside(path: Path, root: Path, label: str) -> None:
    """Raise :class:`WorkspaceRootError` when ``path`` sits inside ``root``.

    Both sides are resolved first, so the comparison sees the location
    the path really points at rather than the way it was spelled.
    """
    resolved = Path(path).expanduser().resolve()
    resolved_root = Path(root).expanduser().resolve()
    if _is_within(resolved, resolved_root):
        raise WorkspaceRootError(resolved, f"it is inside {label}", resolved_root)