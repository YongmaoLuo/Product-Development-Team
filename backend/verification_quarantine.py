"""
Verification Quarantine
=======================

When a verification sub-agent writes into the tree it was supposed to
only read, the damage has to be undone — but not by throwing the work
away.  This module does the two halves of that in one place:

  * **quarantine** — the mutation is written out as a git patch under the
    plan directory, where it survives as evidence and can be replayed or
    inspected;
  * **restore** — the working tree goes back to the commit it was on
    before the mutation.

The rule that shapes everything here: *nothing is destroyed before it
has been saved*.  If the patch cannot be produced or written, the working
tree is left exactly as it was.  A dirty tree the caller can still look
at is a far better outcome than a clean tree whose mutation is gone for
good — the second one cannot be diagnosed at all.  For the same reason a
directory that is not a git repository is refused outright: there is no
baseline to restore to and no way to save what is there, so the only
safe move is to change nothing at all.

How the patch is made
---------------------

``git diff`` only knows about files git already tracks, so an untracked
file — the most common thing a stray writer leaves behind — would be
invisible to it.  The fix is the standard one: ``git add -N`` marks the
untracked files *intent to add*, which puts them into the diff as new
files without staging their content.  ``git diff --binary HEAD`` then
carries everything: modified files, deleted files and the untracked
ones, with binary content intact.

``git add -N`` borrows the index to do that, so the index is put back
with ``git reset`` right after the patch has been written.  The order is
deliberate — the patch is taken *before* the reset, so it also contains
whatever was staged before the mutation.

The restore
-----------

Tracked paths are restored with ``git checkout HEAD -- <path>`` (which
brings back a deleted file as well as undoing an edit), untracked paths
are removed with ``git clean -f -- <path>``.  Both are done path by
path, so one path that refuses to be restored is recorded and the rest
are still attempted.

Every git call is bounded by :data:`GIT_TIMEOUT_SECONDS` and never
raises: a repository that is in trouble is *reported*, not allowed to
hang a verification round.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

#: The ceiling every git call in this module gets.  A patch that takes
#: longer than a minute to produce is a repository in trouble; it is not
#: a reason to let the verification round hang.
GIT_TIMEOUT_SECONDS = 60

#: Where the patches land, relative to the plan directory — next to the
#: other write-scope evidence of the same plan.
QUARANTINE_SUBDIR = ("vp_write_scope", "quarantine")

#: The characters a label may keep.  A label ends up in a file name, so
#: anything that could steer that name out of the quarantine directory is
#: replaced rather than trusted.
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class QuarantineResult:
    """What :func:`quarantine_and_restore` did.

    Attributes:
        patch_path: Where the saved patch was written, or ``None`` when
            there was nothing to save (a clean tree) or the quarantine
            was refused before anything was written.
        restored: The paths that were put back — modified files, restored
            deletions and removed untracked files alike.  Paths are
            relative to the repository root.
        failed: The paths that could **not** be restored.  Their
            contents are still in the tree and still in the patch.
        clean: Whether the tree is clean after the operation.  A
            successful quarantine leaves it clean; a refused or partial
            one does not.
        error: Why the quarantine could not be completed as intended.
            Empty when everything worked.
    """

    patch_path: Optional[Path] = None
    restored: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    clean: bool = False
    error: str = ""


@dataclass
class _StatusEntry:
    """One line of ``git status --porcelain``."""

    xy: str
    path: str
    original: Optional[str] = None

    @property
    def untracked(self) -> bool:
        """Is this path untracked — i.e. does it have to be deleted?"""
        return self.xy == "??"


def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` and return the completed process.

    ``shell=False`` with an argv list: the arguments come from the
    repository and the caller, and a shell would hand them a second
    grammar to escape into.

    Never raises.  A timeout or an OS error becomes a completed process
    with a non-zero return code and the reason in ``stderr``, so the
    caller only ever asks one question — "did this git call work?" — and
    never has to catch anything.
    """
    argv = ["git", *[str(arg) for arg in args]]
    env = dict(os.environ)
    # A git call that needs a credential must fail fast instead of
    # blocking on a prompt that nobody is there to answer.
    env["GIT_TERMINAL_PROMPT"] = "0"
    # This probe runs against a worktree other processes may be writing
    # to; taking the optional index lock to refresh the stat cache is
    # exactly the interference it is meant to detect.  It changes what
    # git *writes*, not what it *reports*.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # A path that came out of ``git status`` is a file name, not a
    # pattern.  ``GIT_LITERAL_PATHSPECS`` keeps a file called
    # ``weird[1].py`` from being read as a glob.
    env["GIT_LITERAL_PATHSPECS"] = "1"
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            124,
            b"",
            f"git {' '.join(argv[1:])} timed out after {GIT_TIMEOUT_SECONDS}s".encode(
                "utf-8"
            ),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            argv,
            127,
            b"",
            f"git could not be executed: {exc}".encode("utf-8"),
        )


def _stderr(proc: subprocess.CompletedProcess) -> str:
    """The error text of a git call, as a plain string."""
    raw = proc.stderr or b""
    if isinstance(raw, str):
        return raw.strip()
    return raw.decode("utf-8", "replace").strip()


def _parse_status(raw: bytes) -> List[_StatusEntry]:
    """Parse ``git status --porcelain -z`` output.

    The ``-z`` format is one NUL-terminated record per entry:
    ``XY<space>path``.  A rename or a copy is followed by a second
    record holding the original path.

    Paths are decoded with ``surrogateescape`` so that a file name which
    is not valid UTF-8 survives the round trip back into git unchanged.
    """
    entries: List[_StatusEntry] = []
    tokens = raw.split(b"\0")
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if len(record) < 4:
            continue
        xy = record[:2].decode("utf-8", "replace")
        path = record[3:].decode("utf-8", "surrogateescape")
        original = None
        if "R" in xy or "C" in xy:
            if index < len(tokens):
                original = tokens[index].decode("utf-8", "surrogateescape")
                index += 1
        entries.append(_StatusEntry(xy=xy, path=path, original=original))
    return entries


def _scope(project_dir: Path, repo_root: Path) -> str:
    """The pathspec that covers ``project_dir`` inside ``repo_root``.

    ``.`` when the directory *is* the repository root, the relative path
    below it otherwise.  Everything this module does is limited to this
    pathspec, so a caller that points at a subdirectory never has the
    rest of the repository touched.
    """
    relative = os.path.relpath(str(project_dir), str(repo_root))
    if relative == ".":
        return "."
    return relative.replace(os.sep, "/")


def quarantine_dir(plan_dir: Path) -> Path:
    """The directory the patches of ``plan_dir`` live in."""
    return Path(plan_dir).expanduser().resolve().joinpath(*QUARANTINE_SUBDIR)


def _patch_path(directory: Path, label: str) -> Path:
    """A free file name for this quarantine inside ``directory``.

    The label is sanitised, the timestamp is UTC, and a second that
    already has a patch does not overwrite the first one — two rounds
    that quarantine in the same second must not cost one of them its
    evidence.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_label = _LABEL_SAFE.sub("_", label or "quarantine").strip("_") or "quarantine"
    candidate = directory / f"{safe_label}-{stamp}.patch"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{safe_label}-{stamp}-{counter}.patch"
        counter += 1
    return candidate


def quarantine_and_restore(
    project_dir: Path,
    plan_dir: Path,
    label: str,
) -> QuarantineResult:
    """Save the mutation of ``project_dir`` as a patch and put the tree back.

    Args:
        project_dir: The tree that was mutated.  It has to be inside a
            git repository — a directory that is not one is refused
            without anything being deleted.
        plan_dir: The plan directory the patch is written into, under
            ``vp_write_scope/quarantine/``.
        label: A name for this quarantine, e.g. the round it belongs to.
            It ends up in the patch's file name.

    Returns:
        A :class:`QuarantineResult`.  ``error`` is empty when the
        mutation was saved and the tree was put back; otherwise it says
        what stopped it, and ``failed`` names the paths that are still
        dirty.
    """
    project_dir = Path(project_dir).expanduser().resolve()
    plan_dir = Path(plan_dir).expanduser().resolve()

    # --- is there a tree to restore, and a baseline to restore it to? ---
    probe = _git(["rev-parse", "--show-toplevel"], cwd=project_dir)
    if probe.returncode != 0 or not (probe.stdout or b"").strip():
        detail = _stderr(probe)
        return QuarantineResult(
            error=f"not a git repository: {project_dir}"
            + (f" ({detail})" if detail else "")
        )
    repo_root = Path(
        (probe.stdout or b"").decode("utf-8", "replace").strip()
    ).resolve()

    head = _git(["rev-parse", "--verify", "HEAD"], cwd=repo_root)
    if head.returncode != 0:
        return QuarantineResult(
            error=f"repository has no commit to restore to: {repo_root}"
        )

    scope = _scope(project_dir, repo_root)

    status = _git(
        ["status", "--porcelain", "-z", "--untracked-files=all", "--", scope],
        cwd=repo_root,
    )
    if status.returncode != 0:
        return QuarantineResult(
            error=f"git status failed in {repo_root}: {_stderr(status)}"
        )
    if not _parse_status(status.stdout or b""):
        # Nothing was mutated — nothing to save and nothing to restore.
        return QuarantineResult(clean=True)

    # --- save the mutation BEFORE touching the tree ---------------------
    # ``git add -N`` puts the untracked files into the diff as new files
    # without staging their content.  If it fails, the patch would be
    # missing exactly the files that are most likely to be deleted
    # below, so the quarantine stops here rather than destroying
    # something it has not saved.
    add = _git(["add", "-N", "--", scope], cwd=repo_root)
    if add.returncode != 0:
        return QuarantineResult(
            error=f"could not stage the untracked files for the patch: {_stderr(add)}"
        )

    diff = _git(["diff", "--binary", "HEAD", "--", scope], cwd=repo_root)
    if diff.returncode != 0:
        return QuarantineResult(
            error=f"could not produce a patch: {_stderr(diff)}"
        )

    target_dir = quarantine_dir(plan_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        patch_path = _patch_path(target_dir, label)
        patch_path.write_bytes(diff.stdout or b"")
    except OSError as exc:
        return QuarantineResult(error=f"could not write the patch: {exc}")

    # --- put the tree back ----------------------------------------------
    problems: List[str] = []

    # The index was borrowed for ``git add -N``; give it back before the
    # worktree is restored, so that a file which was staged as new is
    # untracked again and can be cleaned rather than checked out.
    reset = _git(["reset", "--", scope], cwd=repo_root)
    if reset.returncode != 0:
        problems.append(f"git reset failed: {_stderr(reset)}")

    after = _git(
        ["status", "--porcelain", "-z", "--untracked-files=all", "--", scope],
        cwd=repo_root,
    )
    entries = _parse_status(after.stdout or b"") if after.returncode == 0 else []

    restored: List[str] = []
    failed: List[str] = []
    for entry in entries:
        if entry.untracked:
            proc = _git(["clean", "-f", "--", entry.path], cwd=repo_root)
        else:
            proc = _git(["checkout", "HEAD", "--", entry.path], cwd=repo_root)
        if proc.returncode == 0:
            restored.append(entry.path)
        else:
            # One path that refuses to be restored must not stop the
            # others — the rest of the tree still has to come back.
            failed.append(entry.path)

    final = _git(
        ["status", "--porcelain", "-z", "--untracked-files=all", "--", scope],
        cwd=repo_root,
    )
    clean = final.returncode == 0 and not _parse_status(final.stdout or b"")

    if failed:
        problems.append("could not restore: " + ", ".join(failed))
    if not clean:
        problems.append("the working tree is still dirty")

    return QuarantineResult(
        patch_path=patch_path,
        restored=restored,
        failed=failed,
        clean=clean,
        error="; ".join(problems),
    )