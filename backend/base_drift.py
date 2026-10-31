"""Base-branch drift check — configuration, report shape, event names.

Why this module exists
----------------------
A verification round answers "does the code on this branch still
work?" — but only with respect to the branch it is standing on.  When
the branch has drifted away from its base (``main``) for weeks, the
round verifies a codebase that upstream has already moved past.  The
symptom shows up far away from the cause: a test that used to pass
now fails because the base changed the contract it depends on, and the
round reports "the feature is broken" when the feature was never
touched on this branch.

The real instance that motivated this: a verification branch sat
**53 commits behind** ``main``.  Every round it ran was measuring its
own staleness, not the work under test.

This module holds three layers:

* the **vocabulary** of the check:

  * :class:`BaseDriftConfig` — the three environment knobs, their safe
    defaults, and how a malformed value falls back.
  * :class:`BaseDriftReport` — the JSON-safe shape every outcome is
    reported in, plus the two predicates the caller needs
    (:attr:`~BaseDriftReport.event` and
    :attr:`~BaseDriftReport.blocked`).
  * :data:`EVENT_BY_STATUS` — one stable event name per status.

* the **git plumbing** that measures the drift:
  :func:`run_git`, :func:`resolve_base_ref`, :func:`count_divergence`,
  :func:`check_and_merge_base` and :func:`conflict_files`.

* the **repair task** a conflict turns into:
  :func:`build_conflict_repair_task` — a pure function over the report
  that produces the task dict.  Writing that task belongs to the
  repair channel that already exists; this module only says what the
  task is.

**The vocabulary stays pure; the git calls live below it.**  The
dataclasses and the event map never touch a repository — they can be
imported and exercised without one.  The git plumbing is a separate
layer further down this module: it shells out, it never raises, and it
turns every failure into a report instead of an exception.

Design constraints
------------------
* **A typo must not disable the check.**  Every knob falls back to its
  default instead of raising.  A base-drift check that refuses to
  start because somebody wrote ``PDT_BASE_DRIFT_MODE=repairp`` is a
  check that never runs, and the drift it exists to catch stays
  invisible — the failure mode of a mis-typed knob is "the check is
  quietly off", which is exactly the failure the check was added to
  prevent.
* **The status set is closed.**  Seven statuses, seven event names.
  A status outside the set is reported as ``failed`` — the
  conservative choice, because "something went wrong" is the only
  answer that is never wrong.
* **The report shape is stable.**  ``base_ref`` and ``base_sha`` are
  always present, even when empty.  An absent key and an empty value
  mean different things to whoever reads the event log afterwards: the
  first says "nobody recorded this", the second says "there was
  nothing to record".

Status semantics
----------------
=============  ==========================================================
``none``       The branch is in sync with its base.  Nothing to do.
``merged``     The branch was behind; the base was merged in.
``conflicts``  The merge hit conflicts.  **The only blocking status.**
``dirty_tree`` The working tree had uncommitted changes, so the merge
               was skipped (not an error — the round continues).
``failed``     A git call failed.  ``stderr_excerpt`` says why.
``disabled``   The check was switched off by configuration.
``skipped``    The directory is in no git repository at all.  An
               internal condition, *not* a failure — the round
               continues.
=============  ==========================================================
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

# ── environment knobs ──────────────────────────────────────────────────────
#
# Three knobs, all optional, all with a default that keeps the check
# enabled and read-only.  They are read from the process environment;
# `.env.example` documents them for a deployment.

ENV_ENABLED = "PDT_BASE_DRIFT_ENABLED"
"""Off switch.  ``0`` / ``false`` / ``no`` / ``off`` (any case) turn the
check off; anything else leaves it on."""

ENV_BASE = "PDT_BASE_DRIFT_BASE"
"""The base ref to compare against.  Defaults to ``origin/main``."""

ENV_MODE = "PDT_BASE_DRIFT_MODE"
"""``report`` (read-only, the default) or ``repair`` (act on the
conflicts).  Any other value falls back to ``report``."""

DEFAULT_BASE_REF = "origin/main"
DEFAULT_MODE = "report"
DEFAULT_TIMEOUT_SEC = 120

MODE_REPORT = "report"
MODE_REPAIR = "repair"
VALID_MODES = (MODE_REPORT, MODE_REPAIR)

FALSY_ENABLED_VALUES = frozenset({"0", "false", "no", "off"})
"""The spellings that turn the check off.  Anything else — including a
junk value — leaves it on, because a check that is silently disabled
by a typo is worse than a check that runs with its default
configuration."""

# ── statuses ───────────────────────────────────────────────────────────────

STATUS_NONE = "none"
STATUS_MERGED = "merged"
STATUS_CONFLICTS = "conflicts"
STATUS_DIRTY_TREE = "dirty_tree"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"
STATUS_SKIPPED = "skipped"

ALL_STATUSES = (
    STATUS_NONE,
    STATUS_MERGED,
    STATUS_CONFLICTS,
    STATUS_DIRTY_TREE,
    STATUS_FAILED,
    STATUS_DISABLED,
    STATUS_SKIPPED,
)
"""Every status the check can report.  The set is closed."""

# ── event names ────────────────────────────────────────────────────────────

EVENT_BY_STATUS: Dict[str, str] = {
    STATUS_NONE: "base_drift_none",
    STATUS_MERGED: "base_drift_merged",
    STATUS_CONFLICTS: "base_drift_conflicts",
    STATUS_DIRTY_TREE: "base_drift_dirty_tree",
    STATUS_FAILED: "base_drift_failed",
    STATUS_DISABLED: "base_drift_disabled",
    STATUS_SKIPPED: "base_drift_skipped",
}
"""One stable event name per status.  The event log is grepped and
counted, so a name may not change meaning — ``base_drift_none`` means
"the branch was in sync" and always will."""


def _read_env(environ: Mapping[str, str], name: str) -> str:
    """Read one variable as a stripped string (missing → empty)."""
    raw = environ.get(name)
    if raw is None:
        return ""
    return str(raw).strip()


def _enabled_from_env(environ: Mapping[str, str]) -> bool:
    """``False`` only for the explicit off-switch spellings."""
    value = _read_env(environ, ENV_ENABLED)
    if not value:
        return True
    return value.lower() not in FALSY_ENABLED_VALUES


def _base_ref_from_env(environ: Mapping[str, str]) -> str:
    """The configured base ref, or the default when unset/blank."""
    return _read_env(environ, ENV_BASE) or DEFAULT_BASE_REF


def _mode_from_env(environ: Mapping[str, str]) -> str:
    """The configured mode, falling back to ``report`` when unknown.

    ``repair`` is the only mode that acts on the working tree, so an
    unrecognised value must never be handed one.
    """
    value = _read_env(environ, ENV_MODE).lower()
    return value if value in VALID_MODES else DEFAULT_MODE


@dataclass(frozen=True)
class BaseDriftConfig:
    """How the base-drift check is configured.

    The defaults are the ones a deployment gets by configuring
    nothing: the check runs, it compares against ``origin/main``, and
    it only reports — it never touches the working tree on its own.
    """

    enabled: bool = True
    base_ref: str = DEFAULT_BASE_REF
    mode: str = DEFAULT_MODE
    timeout_sec: int = DEFAULT_TIMEOUT_SEC

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "BaseDriftConfig":
        """Build a config from the environment.

        ``environ`` defaults to :data:`os.environ`; tests pass an
        explicit mapping so the result does not depend on the ambient
        shell.  A malformed value never raises — it falls back to the
        default.
        """
        env: Mapping[str, str] = os.environ if environ is None else environ
        return cls(
            enabled=_enabled_from_env(env),
            base_ref=_base_ref_from_env(env),
            mode=_mode_from_env(env),
        )


@dataclass(frozen=True)
class BaseDriftReport:
    """What one base-drift check found.

    The field order is the order the feature talks about them in:
    the base it looked at, how far the branch drifted from it, what
    happened to the working tree, and what the caller should do about
    it.

    ``conflict_files`` holds one entry per conflicting path, shaped
    ``{"path": str, "conflict_blocks": int}`` — the path alone would
    not say whether the conflict was one line or forty.
    """

    status: str
    base_ref: str = ""
    base_sha: str = ""
    behind: int = 0
    ahead: int = 0
    head_before: str = ""
    head_after: str = ""
    merge_commit: str = ""
    conflict_files: List[Dict[str, Any]] = field(default_factory=list)
    stderr_excerpt: str = ""
    mode: str = DEFAULT_MODE
    detail: str = ""

    @property
    def event(self) -> str:
        """The event name for this report's status.

        A status outside the closed set is reported as ``failed``:
        "something went wrong" is the only answer that cannot be
        wrong.
        """
        return EVENT_BY_STATUS.get(self.status, EVENT_BY_STATUS[STATUS_FAILED])

    @property
    def blocked(self) -> bool:
        """True iff this report must stop the verification round.

        Conflicts are the only blocking outcome.  A failed git call, a
        dirty working tree and a directory that is not a work tree all
        leave the round running — the drift is reported and the round
        continues, because a check that blocks on its own problems
        would block for reasons that have nothing to do with the code
        under test.
        """
        return self.status == STATUS_CONFLICTS

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe payload for the event log.

        ``base_ref`` and ``base_sha`` are always present, even when
        empty — an absent key and an empty value mean different things
        to whoever reads the log afterwards.  The merge metadata
        (``head_before`` / ``head_after`` / ``merge_commit``) is
        included only when it carries something: a report for a branch
        that was never merged has no merge to describe.
        """
        payload: Dict[str, Any] = {
            "status": self.status,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "behind": self.behind,
            "ahead": self.ahead,
            "stderr_excerpt": self.stderr_excerpt,
            "conflict_files": [
                dict(entry) if isinstance(entry, dict) else entry
                for entry in self.conflict_files
            ],
            "mode": self.mode,
            "detail": self.detail,
        }
        if self.head_before:
            payload["head_before"] = self.head_before
        if self.head_after:
            payload["head_after"] = self.head_after
        if self.merge_commit:
            payload["merge_commit"] = self.merge_commit
        return payload


# ── git plumbing ───────────────────────────────────────────────────────────
#
# Everything below shells out to git.  One rule governs the whole layer:
# **a git call never raises.**  The check exists to *report* drift; a
# check that breaks the round it runs in is worse than no check at all.
# A missing binary, a timeout and a non-zero exit all come back as a
# completed process carrying the reason in ``stderr``, and the report
# above turns that into a status.

STDERR_EXCERPT_LIMIT = 200
"""How much of git's own complaint survives into the report.

200 characters is enough to name the failing command and the ref it
tripped over; more than that is a log, not an event field."""

CONFLICT_MARKER = "<<<<<<< "
"""The first line of a conflict block.

Git writes the marker with a trailing space (``<<<<<<< HEAD``), so the
marker is matched with one — a line that merely starts with seven
angle brackets is not a conflict block.
"""


def run_git(
    project_dir: Any,
    args: Sequence[str],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> subprocess.CompletedProcess:
    """Run ``git`` inside ``project_dir`` and return the completed process.

    ``shell=False`` with an argv list: the arguments come from
    configuration, and a shell would hand them a second grammar to
    escape into.

    Never raises.  A timeout or an OS error becomes a completed
    process with a non-zero return code and the reason in ``stderr``,
    so the caller only ever asks one question — "did this git call
    work?" — and never has to catch anything.
    """
    argv = ["git", "-C", str(project_dir), *[str(arg) for arg in args]]
    env = dict(os.environ)
    # A git call that needs a credential must fail fast instead of
    # blocking on a prompt that nobody is there to answer.
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            argv,
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            128,
            "",
            f"git {' '.join(str(arg) for arg in args)} timed out after {timeout_sec}s",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            argv, 128, "", f"git could not be executed: {exc}"
        )


def resolve_base_ref(base_ref: str) -> str:
    """Normalise a configured base ref into the ref git should compare with.

    A ref that already names a remote (``origin/main``,
    ``upstream/main``) is used as it stands.  A bare name (``main``) is
    normalised to ``origin/main``.

    The normalisation is deliberately one-way: a bare ``main`` must
    never fall back to the *local* branch ``main``, because the local
    branch is the thing whose drift is being measured.  Comparing HEAD
    with the local ``main`` reports "no drift" on a branch that never
    fetched — exactly the blindness this check was added to remove.
    """
    ref = (base_ref or "").strip()
    if not ref:
        return DEFAULT_BASE_REF
    if "/" in ref:
        return ref
    return f"origin/{ref}"


def _excerpt(text: str, limit: int = STDERR_EXCERPT_LIMIT) -> str:
    """Git's own complaint, stripped and cut to ``limit`` characters."""
    return (text or "").strip()[:limit]


def _never_raise(runner: Any) -> Any:
    """Wrap a git runner so that a failing subprocess never escapes.

    :func:`run_git` already turns a timeout or an OS error into a
    completed process; a runner injected by a caller — a test, or a
    caller with its own transport — need not.  The check must not
    raise either way: a drift check that breaks the round it runs in
    is worse than a drift check that reports nothing at all.
    """

    def safe(
        project_dir: Any, args: Sequence[str], timeout_sec: int
    ) -> subprocess.CompletedProcess:
        try:
            return runner(project_dir, args, timeout_sec)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(
                ["git", *[str(arg) for arg in args]],
                128,
                "",
                f"git {' '.join(str(arg) for arg in args)} failed: {exc}",
            )

    return safe


def _git_metadata_present(path: Path) -> bool:
    """True when ``path`` carries the markers of a git repository.

    A normal clone keeps them in a ``.git`` *directory*; a linked
    worktree keeps a ``.git`` *file* that points at the real git
    directory; a bare repository — and the ``.git`` directory itself —
    carries ``HEAD``, ``objects`` and ``refs`` directly.

    The ``.git`` entry is looked up with ``lexists``: a *broken*
    symlink still says "this is meant to be a repository", and git
    will say the same thing.
    """
    if os.path.lexists(path / ".git"):
        return True
    return (
        (path / "HEAD").is_file()
        and (path / "objects").is_dir()
        and (path / "refs").is_dir()
    )


def _belongs_to_repository(project_dir: Any) -> bool:
    """True when ``project_dir`` sits inside — or is — a git repository.

    This is what separates "there is no repository here" from "git
    could not answer": both make ``git rev-parse --is-inside-work-tree``
    exit non-zero, and only the first is a fact about the directory.

    The search walks *up* from the directory, because a repository is
    reached through its ancestors — a subdirectory of a clone belongs
    to that clone.
    """
    try:
        start = Path(str(project_dir)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for candidate in (start, *start.parents):
        if _git_metadata_present(candidate):
            return True
    return False


def _parse_status_paths(stdout: str) -> List[str]:
    """The paths in a ``git status --porcelain`` listing.

    Porcelain v1 prints two status characters, a space and the path:
    ``"?? untracked.txt"``, ``" M changed.txt"``.  Everything after the
    status columns is kept as it stands — a rename is printed as
    ``"R  old -> new"``, and splitting that further would throw away
    information the caller may want.
    """
    paths: List[str] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        paths.append(line[3:] if len(line) > 3 else line.strip())
    return paths


class _DirtyPaths(List[str]):
    """The paths that make the working tree dirty.

    A plain list, with one addition: ``stderr`` carries git's own
    complaint when the **status command itself** failed.  An empty list
    then means "the working tree is clean" only while ``stderr`` is
    empty — a status command that failed must never be read as "clean",
    because the next step would merge into a tree nobody inspected.
    """

    def __init__(self, paths: Sequence[str] = (), stderr: str = "") -> None:
        super().__init__(paths)
        self.stderr = stderr


def _dirty_paths(
    project_dir: Any,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    git_runner: Any = None,
) -> List[str]:
    """The paths that make the working tree dirty.

    An empty list means the working tree is clean — *unless* the
    returned list carries a ``stderr``: then the status command failed
    and the tree was never inspected at all.  The two cases look the
    same to a caller that only looks at the paths, which is exactly
    why they must not be the same value.
    """
    runner = run_git if git_runner is None else git_runner
    completed = _never_raise(runner)(
        project_dir, ["status", "--porcelain"], timeout_sec
    )
    if completed.returncode != 0:
        return _DirtyPaths(
            stderr=_excerpt(
                completed.stderr or completed.stdout or "git status failed"
            )
        )
    return _DirtyPaths(_parse_status_paths(completed.stdout))


def _split_ref(resolved_ref: str) -> tuple:
    """Split a resolved base ref into ``(remote, branch)``.

    ``origin/main`` is fetched as ``git fetch origin main``: everything
    before the first slash names the remote, everything after it names
    the branch.  A resolved ref always carries a slash —
    :func:`resolve_base_ref` guarantees it — but a ref that somehow
    does not is passed on whole, which git rejects loudly instead of
    quietly fetching the wrong thing.
    """
    ref = (resolved_ref or "").strip()
    if "/" not in ref:
        return ref, ""
    remote, branch = ref.split("/", 1)
    return remote, branch


def _head_sha(
    project_dir: Any,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    git_runner: Any = None,
) -> str:
    """The SHA ``HEAD`` points at, or ``""`` when it cannot be read.

    It is the anchor of the merge: the commit before it and the commit
    after it are what makes "the base was merged in" a statement about
    the repository rather than about the check's own bookkeeping.
    """
    runner = run_git if git_runner is None else git_runner
    completed = _never_raise(runner)(project_dir, ["rev-parse", "HEAD"], timeout_sec)
    if completed.returncode != 0:
        return ""
    lines = (completed.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _current_branch(
    project_dir: Any,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    git_runner: Any = None,
) -> str:
    """The branch the working tree is on, or ``HEAD`` when unknown.

    The name is only used to label the merge commit.  When it cannot
    be read the merge still happens — a merge commit that says
    ``HEAD`` is a smaller problem than no merge at all.
    """
    runner = run_git if git_runner is None else git_runner
    completed = _never_raise(runner)(
        project_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout_sec
    )
    if completed.returncode != 0:
        return "HEAD"
    return (completed.stdout or "").strip() or "HEAD"


def _resolve_base_commit(
    project_dir: Any,
    resolved_ref: str,
    runner: Any,
    timeout_sec: int,
) -> tuple:
    """Return ``(sha, stderr)`` for the resolved base ref.

    An empty sha with a non-empty stderr means "the ref does not
    resolve".  The ``^{commit}`` suffix asks git for a commit
    specifically, so a tag pointing at a tree is not mistaken for one.
    """
    completed = runner(
        project_dir,
        ["rev-parse", "--verify", f"{resolved_ref}^{{commit}}"],
        timeout_sec,
    )
    if completed.returncode != 0:
        return "", _excerpt(completed.stderr or completed.stdout or "")
    lines = (completed.stdout or "").strip().splitlines()
    sha = lines[0].strip() if lines else ""
    if not sha:
        return "", _excerpt(
            completed.stderr or f"git rev-parse returned no commit for {resolved_ref}"
        )
    return sha, ""


def _parse_counts(stdout: str) -> Optional[tuple]:
    """Parse the two numbers ``rev-list --left-right --count`` prints."""
    parts = (stdout or "").strip().split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _divergence(
    project_dir: Any,
    resolved_ref: str,
    runner: Any,
    timeout_sec: int,
) -> tuple:
    """Return ``(behind, ahead, base_sha, stderr)`` for the base ref.

    The base is resolved first: without a commit to compare against
    there is no count to report, and a count of zero would read as
    "no drift" — the one answer that must never be guessed.
    """
    base_sha, stderr = _resolve_base_commit(
        project_dir, resolved_ref, runner, timeout_sec
    )
    if stderr:
        return 0, 0, "", stderr

    completed = runner(
        project_dir,
        ["rev-list", "--left-right", "--count", f"HEAD...{resolved_ref}"],
        timeout_sec,
    )
    if completed.returncode != 0:
        return (
            0,
            0,
            base_sha,
            _excerpt(completed.stderr or completed.stdout or ""),
        )

    counts = _parse_counts(completed.stdout)
    if counts is None:
        return (
            0,
            0,
            base_sha,
            _excerpt(
                completed.stdout
                or f"unexpected rev-list output for {resolved_ref}"
            ),
        )

    # ``git rev-list --left-right --count HEAD...<base>`` prints the
    # LEFT side first.  The left side of ``HEAD...<base>`` is HEAD, so
    # the first column counts the commits only HEAD has — that is
    # *ahead* — and the second column counts the commits only the base
    # has — that is *behind*.
    ahead, behind = counts
    return behind, ahead, base_sha, ""


def count_divergence(
    project_dir: Any,
    base_ref: str,
    *,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    git_runner: Any = None,
) -> tuple:
    """How far ``HEAD`` has drifted from ``base_ref``.

    Returns ``(behind, ahead, stderr)``.  The base ref is resolved
    first, so a bare name is compared against its remote-tracking ref.

    ``stderr`` is empty when the counts are meaningful and carries
    git's complaint (cut to 200 characters) when they are not; a failed
    count answers ``(0, 0)`` rather than a number nobody should trust.
    """
    runner = run_git if git_runner is None else git_runner
    resolved = resolve_base_ref(base_ref)
    behind, ahead, _base_sha, stderr = _divergence(
        project_dir, resolved, runner, timeout_sec
    )
    return behind, ahead, stderr


def _repo_toplevel(
    project_dir: Any,
    timeout_sec: int,
    runner: Any,
) -> str:
    """The root of the work tree, or ``""`` when it cannot be read.

    Git prints the paths of a diff relative to the top of the work
    tree, so that is where a conflicted file has to be read from.  An
    empty answer leaves the caller with the project directory as the
    only other candidate — the paths are then looked for in both
    places.
    """
    completed = runner(project_dir, ["rev-parse", "--show-toplevel"], timeout_sec)
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _count_conflict_blocks(project_dir: Any, toplevel: str, path: str) -> int:
    """How many conflict blocks one conflicted file holds.

    A conflict block starts at a ``<<<<<<< `` marker line.  The count
    is taken from the working tree, where the markers are — which is
    why it has to be taken *before* the merge is aborted: afterwards
    the markers are gone and every file would count as zero.

    A file that cannot be read — a binary one, or one that was deleted
    on one side — counts as zero blocks.  The path is still reported:
    the file is still unmerged, and hiding it would hide a conflict the
    operator still has to resolve.
    """
    candidates: List[Path] = []
    if toplevel:
        candidates.append(Path(toplevel) / path)
    candidates.append(Path(project_dir) / path)
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        return sum(
            1 for line in text.splitlines() if line.startswith(CONFLICT_MARKER)
        )
    return 0


def conflict_files(
    project_dir: Any,
    *,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    git_runner: Any = None,
) -> List[Dict[str, Any]]:
    """The paths a conflicted merge left unmerged, with their block counts.

    One entry per path, shaped ``{"path": str, "conflict_blocks": int}``.
    The path alone would not say whether the conflict was one line or
    forty.

    The paths are read **NUL separated** (``git diff --name-only
    --diff-filter=U -z``): a path with a space in it has to survive
    the trip in one piece, and a line-separated listing cannot promise
    that.

    Never raises.  A listing that cannot be read answers an empty list:
    "no unmerged path was found" and "the unmerged paths could not be
    found" must not look alike to the caller — the caller decides what
    an empty list means, it never guesses.
    """
    runner = _never_raise(run_git if git_runner is None else git_runner)
    completed = runner(
        project_dir,
        ["diff", "--name-only", "--diff-filter=U", "-z"],
        timeout_sec,
    )
    if completed.returncode != 0:
        return []
    paths = [path for path in (completed.stdout or "").split("\0") if path]
    if not paths:
        return []
    toplevel = _repo_toplevel(project_dir, timeout_sec, runner)
    return [
        {
            "path": path,
            "conflict_blocks": _count_conflict_blocks(project_dir, toplevel, path),
        }
        for path in paths
    ]


def _conflict_complaint(completed: subprocess.CompletedProcess) -> str:
    """Git's complaint about a conflicted merge, without the progress.

    Git prints ``Auto-merging <path>`` while it works and the complaint
    itself — ``CONFLICT (content): Merge conflict in <path>`` — when it
    cannot finish.  The excerpt exists to name the failure, so the
    progress lines are dropped and the complaint is kept, cut to 200
    characters.
    """
    lines: List[str] = []
    for part in (completed.stderr or "", completed.stdout or ""):
        for line in part.splitlines():
            if not line.strip():
                continue
            if line.startswith("Auto-merging"):
                continue
            lines.append(line)
    return _excerpt("\n".join(lines))


def _work_tree_report(
    project_dir: Any,
    inside: subprocess.CompletedProcess,
    inside_stdout: str,
    resolved: str,
    mode: str,
) -> Optional[BaseDriftReport]:
    """The report for a ``rev-parse`` that did not say "true".

    Two very different things hide behind one non-zero exit: the
    directory is not in a repository (a fact about the directory) and
    the git call itself did not work (a fact about the machine).  Only
    the second one is a failure, and only the second one has a
    complaint worth quoting.

    The two used to be the same report — ``skipped``, with an empty
    excerpt and a fixed sentence standing in for git's own words.  A
    checkout whose git was missing, timing out or broken then looked
    exactly like a directory with nothing to check, and the event log
    said "nothing to do" where the truth was "the check could not
    run".

    ``None`` means "this *is* a work tree — carry on".
    """
    if inside.returncode == 0:
        # git answered, and the answer was "not a work tree".  git
        # knows the directory — it says the directory is not a work
        # tree (a bare repository, for instance).  That is not "there
        # is nothing to check": the check merges a base *into a work
        # tree*, and there is none here.
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            mode=mode,
            stderr_excerpt=(
                _excerpt(inside.stderr)
                or (
                    "git rev-parse --is-inside-work-tree reported "
                    f"'{inside_stdout}' instead of 'true'"
                )
            ),
            detail=(
                "git rev-parse --is-inside-work-tree did not report a "
                f"work tree for {project_dir}"
            ),
        )

    if _belongs_to_repository(project_dir):
        # git could not answer, and there *is* a repository here: the
        # git call failed.  A missing binary, a timeout and a broken
        # ``.git`` all end up here — and all of them used to be
        # reported as "not a work tree", with git's complaint thrown
        # away.  The complaint is the whole point of the event: it is
        # what says what is actually wrong.
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            mode=mode,
            stderr_excerpt=(
                _excerpt(inside.stderr)
                or _excerpt(inside.stdout)
                or (
                    "git rev-parse --is-inside-work-tree exited with "
                    f"code {inside.returncode}"
                )
            ),
            detail=(
                "git rev-parse --is-inside-work-tree failed for "
                f"{project_dir}"
            ),
        )

    # No repository anywhere up the tree: there is nothing to check
    # here, and that is a fact about the directory rather than a
    # failure.  The round continues.
    return BaseDriftReport(
        status=STATUS_SKIPPED,
        base_ref=resolved,
        mode=mode,
        detail=f"{project_dir} is not a git work tree",
    )


def check_and_merge_base(
    project_dir: Any,
    *,
    config: Optional[BaseDriftConfig] = None,
    git_runner: Any = None,
) -> BaseDriftReport:
    """Compare ``HEAD`` with the configured base and report the drift.

    Never raises.  The outcomes, in the order they are decided:

    ``disabled``   the check is switched off — no git call at all,
    ``skipped``    the directory is in no git repository at all — a
                   fact about the directory, not a failure,
    ``failed``     the base ref does not resolve, or a git call failed
                   (a missing binary, a timeout, a broken ``.git``).
                   Git's own complaint is in ``stderr_excerpt``,
    ``none``       the branch is in sync with its base — nothing to do.
                   The base is read again after the fetch, so a base
                   that caught up with ``HEAD`` while the fetch was
                   running lands here too,
    ``dirty_tree`` the branch is behind, but the working tree has
                   uncommitted changes — the merge is skipped and the
                   round continues,
    ``conflicts``  the merge left unmerged paths behind — the only
                   blocking outcome.  The conflicting files are listed
                   with their block counts, and the merge attempt is
                   aborted, so the working tree is clean again,
    ``merged``     the base was merged into the branch; the report
                   carries the merge commit and the HEAD before and
                   after it.

    A branch that is *behind* its base is measured here; merging it in
    takes a clean working tree, a fetch that works and a merge that
    works.  Every one of those three can fail, and every failure is
    reported with git's own complaint rather than guessed at.
    """
    cfg = BaseDriftConfig.from_env() if config is None else config
    runner = _never_raise(run_git if git_runner is None else git_runner)
    timeout_sec = cfg.timeout_sec

    if not cfg.enabled:
        return BaseDriftReport(
            status=STATUS_DISABLED,
            base_ref=cfg.base_ref,
            mode=cfg.mode,
            detail="the base-drift check is switched off by configuration",
        )

    resolved = resolve_base_ref(cfg.base_ref)

    inside = runner(
        project_dir, ["rev-parse", "--is-inside-work-tree"], timeout_sec
    )
    inside_stdout = (inside.stdout or "").strip()
    if inside.returncode != 0 or inside_stdout != "true":
        work_tree = _work_tree_report(
            project_dir, inside, inside_stdout, resolved, cfg.mode
        )
        if work_tree is not None:
            return work_tree

    behind, ahead, base_sha, stderr = _divergence(
        project_dir, resolved, runner, timeout_sec
    )
    if stderr:
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            base_sha=base_sha,
            behind=behind,
            ahead=ahead,
            stderr_excerpt=stderr,
            mode=cfg.mode,
            detail=f"could not compare HEAD with {resolved}",
        )

    if behind == 0:
        return BaseDriftReport(
            status=STATUS_NONE,
            base_ref=resolved,
            base_sha=base_sha,
            behind=0,
            ahead=ahead,
            mode=cfg.mode,
            detail=f"HEAD is in sync with {resolved}",
        )

    # ── the branch is behind its base ──
    #
    # Before anything is merged the working tree has to be clean.
    # Merging into a tree with uncommitted work folds that work into
    # the merge: the result is neither the branch nor the base, and
    # the operator's changes end up inside a merge commit they never
    # saw.  A dirty tree is therefore reported and skipped — it is a
    # normal state of a working repository, not a failure, and it does
    # not block the round.

    dirty = _dirty_paths(project_dir, timeout_sec, runner)
    if dirty.stderr:
        # The status command itself failed.  This is *not* "the tree is
        # clean": nobody looked at the tree, so nothing may be merged.
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            base_sha=base_sha,
            behind=behind,
            ahead=ahead,
            stderr_excerpt=dirty.stderr,
            mode=cfg.mode,
            detail="could not read the working tree status",
        )

    if dirty:
        return BaseDriftReport(
            status=STATUS_DIRTY_TREE,
            base_ref=resolved,
            base_sha=base_sha,
            behind=behind,
            ahead=ahead,
            mode=cfg.mode,
            detail=(
                f"the working tree has {len(dirty)} uncommitted change(s) "
                f"({', '.join(dirty[:3])}); {resolved} was not merged"
            ),
        )

    # ── fetch the base ──
    #
    # The remote and the branch come from the *resolved* ref, so a
    # bare ``main`` is fetched from ``origin`` — never from a local
    # branch of the same name, which is the very thing whose drift is
    # being measured.

    remote, branch = _split_ref(resolved)
    fetched = runner(project_dir, ["fetch", remote, branch], timeout_sec)
    if fetched.returncode != 0:
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            base_sha=base_sha,
            behind=behind,
            ahead=ahead,
            stderr_excerpt=_excerpt(fetched.stderr or fetched.stdout or ""),
            mode=cfg.mode,
            detail=f"could not fetch {branch} from {remote}",
        )

    # ── read the base again ──
    #
    # The fetch may have moved the base, and the drift measured before
    # it is then a statement about a commit that is no longer the base.
    # So the base is resolved *again* — with the same normalisation, so
    # a bare ``main`` still means ``origin/main`` and never the local
    # branch of the same name — and the drift is counted again.
    #
    # A base that caught up with HEAD leaves nothing to merge: the
    # branch is in sync after all, and merging anyway would put a merge
    # commit into a branch that had nothing to merge.

    resolved = resolve_base_ref(cfg.base_ref)
    behind, ahead, base_sha, stderr = _divergence(
        project_dir, resolved, runner, timeout_sec
    )
    if stderr:
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            base_sha=base_sha,
            behind=behind,
            ahead=ahead,
            stderr_excerpt=stderr,
            mode=cfg.mode,
            detail=f"could not compare HEAD with {resolved} after the fetch",
        )

    if behind == 0:
        return BaseDriftReport(
            status=STATUS_NONE,
            base_ref=resolved,
            base_sha=base_sha,
            behind=0,
            ahead=ahead,
            mode=cfg.mode,
            detail=f"HEAD is in sync with {resolved}",
        )

    # ── merge the base in ──
    #
    # The merge names the base *commit*, not the ref: a ref that moves
    # under a concurrent fetch would merge something else than the
    # base this report is about.

    head_before = _head_sha(project_dir, timeout_sec, runner)
    merge_message = (
        f"Merge {resolved} into {_current_branch(project_dir, timeout_sec, runner)}"
    )
    merged = runner(
        project_dir,
        ["merge", "--no-ff", "-m", merge_message, base_sha],
        timeout_sec,
    )
    if merged.returncode != 0:
        # A failed merge comes in two kinds.  Only one of them leaves
        # unmerged paths behind; the others leave nothing behind and
        # must not be dressed up as a conflict — a conflict stops the
        # round, a failed merge that is not a conflict does not.
        conflicts = conflict_files(
            project_dir, timeout_sec=timeout_sec, git_runner=runner
        )
        if conflicts:
            # ── the merge conflicts ──
            #
            # The conflicting files are listed, and the merge is then
            # aborted: the abort rolls back *this* merge attempt and
            # nothing else — the branch keeps its own commits and stays
            # where it was, so the next round runs into the same
            # conflict again until somebody resolves it.  The check
            # keeps no memory of the attempt: a conflict that was
            # reported once is still a conflict.
            #
            # The block counts were taken *before* the abort, while
            # the markers were still in the files.
            aborted = runner(project_dir, ["merge", "--abort"], timeout_sec)
            detail = (
                f"{resolved} could not be merged: {len(conflicts)} file(s) "
                f"conflict"
            )
            if aborted.returncode != 0:
                detail += (
                    "; the merge could not be aborted: "
                    + _excerpt(aborted.stderr or aborted.stdout or "")
                )
            return BaseDriftReport(
                status=STATUS_CONFLICTS,
                base_ref=resolved,
                base_sha=base_sha,
                behind=behind,
                ahead=ahead,
                head_before=head_before,
                conflict_files=conflicts,
                stderr_excerpt=_conflict_complaint(merged),
                mode=cfg.mode,
                detail=detail,
            )
        return BaseDriftReport(
            status=STATUS_FAILED,
            base_ref=resolved,
            base_sha=base_sha,
            behind=behind,
            ahead=ahead,
            stderr_excerpt=_excerpt(merged.stderr or merged.stdout or ""),
            mode=cfg.mode,
            detail=f"{resolved} could not be merged",
        )

    # The base was merged in.  The merge commit is the HEAD the merge
    # left behind — unless the merge reported success without moving
    # HEAD, in which case there is no merge commit to name and the
    # field stays empty.
    head_after = _head_sha(project_dir, timeout_sec, runner)
    merge_commit = head_after if head_after and head_after != head_before else ""

    return BaseDriftReport(
        status=STATUS_MERGED,
        base_ref=resolved,
        base_sha=base_sha,
        behind=behind,
        ahead=ahead,
        head_before=head_before,
        head_after=head_after,
        merge_commit=merge_commit,
        mode=cfg.mode,
        detail=f"{resolved} was merged into the branch",
    )


# ── repair tasks ───────────────────────────────────────────────────────────
#
# In ``repair`` mode a conflict is not merely reported: it is turned
# into one executable task.  The builder below is a **pure function** —
# a report goes in, a task dict comes out, and nothing is written.  The
# repair channel that already exists owns persistence; a second owner
# for one row is a second set of rules for it.

BASE_DRIFT_VP_ID = "__base_drift__"
"""The verification point a base-drift conflict is filed under.

The drift is not a verification point of its own — it is a property of
the branch the round stands on.  The repair task still has to name the
point it repairs, and ``__base_drift__`` is the name that says "the
base moved, not the code under test".
"""

REPAIR_TASK_GROUP = "repair"
"""The group a base-drift repair task belongs to.

The executor recognises a repair task by its ``repair-r<round>-<seq>``
id shape and, as a fallback, by a group that starts with ``repair``.
A task under any other group is a task the executor does not pick up.
"""

REPAIR_TASK_PRIORITY = "P0"
"""A conflict with the base blocks the whole round, so its repair is
the most urgent task in the queue."""

BASE_DRIFT_MERGE_SCENE = "base_drift_merge"
"""The provider-routing scene a base-drift merge runs under.

Merging a base is a different kind of work from fixing product code:
it wants a different budget of attention and, when it fails, a
different response.  Giving it its own scene is what makes that
possible.

Only the **name** lives here.  Which tier the scene resolves to is a
configuration decision (``provider_routing.yaml``), so re-tiering the
merge is a config change and not a code change — a tier literal in this
module would silently override the operator's configuration.
"""


def _report_field(report: Any, name: str, default: Any = None) -> Any:
    """Read one field of a report — an object or a mapping."""
    if isinstance(report, Mapping):
        return report.get(name, default)
    return getattr(report, name, default)


def _conflict_entries(report: Any) -> List[Dict[str, Any]]:
    """The conflicting files of a report, normalised to path + blocks.

    The report's own shape is ``{"path": str, "conflict_blocks": int}``,
    but a bare path is accepted too: a file is still a file to resolve
    even when nobody counted its blocks.
    """
    entries: List[Dict[str, Any]] = []
    for raw in _report_field(report, "conflict_files", []) or []:
        if isinstance(raw, Mapping):
            path = str(raw.get("path") or "").strip()
            try:
                blocks = int(raw.get("conflict_blocks") or 0)
            except (TypeError, ValueError):
                blocks = 0
        else:
            path = str(raw or "").strip()
            blocks = 0
        if not path:
            continue
        entries.append({"path": path, "conflict_blocks": blocks})
    return entries


def _conflict_repair_title(
    base_ref: str, base_sha: str, file_count: int
) -> str:
    """The one-line name of the task.

    A task without a title is not a task the executor runs: the loader
    that reconciles orphan rows merges a row's real content only when a
    title *and* a description are both there
    (``orphan_rules.MERGE_REQUIRED_FIELDS``), and substitutes a
    content-free placeholder otherwise.  So the title is not decoration
    — it is the difference between the merge running and the merge being
    silently skipped while the round reports the repair batch drained.

    It names the base and how many files are in conflict, because that
    is what tells a reader of the queue that this row is a merge rather
    than any other repair.  The ref is the friendlier name; a report
    that only carries a commit still names the base by that commit, and
    a report that carries neither still says "base" rather than nothing.
    """
    base = (base_ref or "").strip() or (base_sha or "").strip() or "base"
    return f"合并 {base}：解决 {file_count} 个文件的冲突"


def _merge_probe_command(merge_target: str) -> str:
    """The command that answers "is the merge done?".

    Two facts have to hold: the base commit is now an ancestor of HEAD
    (the base was really merged in, not merely fetched), and no path is
    left unmerged (the conflicts were resolved, not just staged away).
    The command says exactly that, so a reviewer reading it knows what
    a green result means for this task.
    """
    return (
        f'git merge-base --is-ancestor {merge_target} HEAD '
        '&& test -z "$(git ls-files -u)"'
    )


def _conflict_repair_description(
    *,
    plan_id: str,
    base_ref: str,
    base_sha: str,
    behind: int,
    entries: List[Dict[str, Any]],
    project_dir: Any,
    merge_target: str,
) -> str:
    """The text the fixer reads: the evidence, then the steps.

    The **first line** carries the plan id — it is the only place the
    plan id lands, because the task row has no column for it and a
    fixer that does not know which plan the conflict belongs to cannot
    report the outcome anywhere.

    The **second line** says what kind of task this is: a merge of the
    base, not a fix of product code.  Without it the agent picks the
    task up looking for a bug that does not exist — or "resolves" a
    conflict by dropping one side of it.

    The rest is the evidence a correct resolution needs: which base was
    merged (ref *and* commit), how far the branch drifted, which files
    conflict and how many blocks each of them holds — followed by the
    steps, because "resolve the conflict" is not an instruction.  Which
    commit to merge, how much conflict there is, and what a good
    resolution looks like are all things the fixer would otherwise have
    to guess.
    """
    evidence = [
        f"base_ref: {base_ref}",
        f"base_sha: {base_sha}",
        f"behind: {behind}",
    ]
    if project_dir is not None:
        evidence.append(f"project_dir: {project_dir}")

    lines = [
        f"plan={plan_id}",
        "任务类型: base 合并（不是产品代码修复）",
        "; ".join(evidence),
        "冲突文件："
        + "; ".join(
            f"{entry['path']} 冲突块 {entry['conflict_blocks']}"
            for entry in entries
        ),
        "修复步骤：",
        f"1. git merge {merge_target} —— 把 base 合并进当前分支",
        "2. 逐块解决冲突标记（<<<<<<< / ======= / >>>>>>>）",
        "3. 保留双方意图：两侧的改动都要留下，不要只保留一侧",
        "4. 跑受影响测试，确认合并之后没有回归",
    ]
    return "\n".join(lines)


def build_conflict_repair_task(
    report: Any,
    *,
    plan_id: str,
    project_dir: Any,
    round_number: int,
    seq: int = 1,
) -> Optional[Dict[str, Any]]:
    """Turn a conflicting base-drift report into one executable task.

    The task is what the ``repair`` mode owes the round: a conflict the
    report named, turned into a task that names the files to resolve
    and says how to resolve them.  Nothing is written — the dict is the
    deliverable, and the repair channel that already exists puts it in
    the database.

    Returns ``None`` when the report names no conflicting file: there
    is nothing to repair, and a task without a change scope sends the
    executor off to "fix" files that were never in conflict.

    The id is the shape the repair round writes — ``repair-r<round>-<seq>``
    — because the executor recognises a repair task by that shape (and by
    a group starting with ``repair``).  A task whose id falls outside it
    is a task the executor never picks up: the round would report "the
    repair batch drained" while the repair is still queued.

    The round number is *not* zero-padded, and the sequence number is:
    ``repair-r3-01`` is the shape the rest of the repair chain writes
    and parses back, so a task written here occupies a number the
    sequence allocator can see.

    Every key it emits is on the task table's static allow-list, and
    ``status`` is not among them: the status belongs to whoever writes
    the row, and a payload carrying one is rejected outright.

    The task carries a **title** and a **test command** because it has
    to be executable, not merely writable: the loader that reconciles
    orphan rows merges a row's real content only when a title and a
    description are both present, and the round needs a command that
    says "the merge is done" rather than one that passes by default.
    """
    entries = _conflict_entries(report)
    if not entries:
        return None

    round_no = int(round_number)
    seq_no = int(seq)
    plan = str(plan_id or "").strip()
    base_ref = str(_report_field(report, "base_ref", "") or "")
    base_sha = str(_report_field(report, "base_sha", "") or "")
    try:
        behind = int(_report_field(report, "behind", 0) or 0)
    except (TypeError, ValueError):
        behind = 0

    # The merge target is the commit, not the ref: a ref that moves
    # under a concurrent fetch would merge something else than the base
    # this report is about.  Without a commit the ref is the best
    # answer left.
    merge_target = base_sha or base_ref

    files: List[str] = []
    for entry in entries:
        if entry["path"] not in files:
            files.append(entry["path"])

    description = _conflict_repair_description(
        plan_id=plan,
        base_ref=base_ref,
        base_sha=base_sha,
        behind=behind,
        entries=entries,
        project_dir=project_dir,
        merge_target=merge_target,
    )

    task: Dict[str, Any] = {
        "id": f"repair-r{round_no}-{seq_no:02d}",
        "task_group": REPAIR_TASK_GROUP,
        "failed_vp_id": BASE_DRIFT_VP_ID,
        "round": round_no,
        "priority": REPAIR_TASK_PRIORITY,
        "files_to_modify": files,
        "title": _conflict_repair_title(base_ref, base_sha, len(files)),
        "description": description,
    }

    # The probe is only worth running when there is something to probe:
    # an empty target makes the ancestor check vacuously true, and a
    # command that always passes is worse than no command at all.
    if merge_target:
        task["test_command"] = _merge_probe_command(merge_target)

    return task
