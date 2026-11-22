#!/usr/bin/env python3
"""Attempt-level rollback: capture the evidence, then put the tree back.

Why this module exists
----------------------
A sub agent that oversteps its declared file scope sometimes **commits** its
own work.  The attempt then ends with an empty diff (the change already sits
in a commit), the failure gets attributed to the wrong cause, and the next
attempt starts from a tree that still carries the overstep.  Undoing that
needs three things: the list of paths the attempt touched, the **content**
of what it touched, and a restore that puts those paths back.

The content has to be captured **before** the rollback.  A patch is the
difference between two states; once the tree is put back, that difference no
longer exists and cannot be reconstructed.  So the order is: capture →
roll back → account.

What this module provides
-------------------------
* :func:`write_rollback_patch` — writes the patch of the given paths,
  relative to the attempt baseline, and reports the paths it could not
  capture.
* :func:`rollback_attempt` — captures that evidence and then restores the
  tree file by file, against the attempt's baseline.
* :func:`format_rollback_failure` — turns the outcome into the text the
  executor hands to the next attempt.
* :class:`RollbackResult` — the record one rollback produces (what was
  rolled back, where the evidence lives, what failed).

The patch is a **real** git patch: ``git apply --check`` accepts it and
``git apply`` restores the content.  That is what makes it evidence rather
than a description.

How the patch is built
----------------------
For every requested path:

* **the path is in the baseline** → ``git diff <baseline> --binary --
  <path>``.  This covers an edited file, a deleted file, and a file the
  attempt **committed** (the baseline is the commit the attempt started
  from, not HEAD — so a committed overstep still shows up as a difference).
* **the path is not in the baseline** → it is new.  A new file that was
  committed since the baseline is captured by ``git diff --no-index
  --binary /dev/null <path>`` just the same, and so is one that was never
  committed at all (``git diff`` alone would not show an untracked file).
  The ``--no-index`` form exits **1** when there is a difference — that is
  not an error, it is the answer.

How the tree is put back
-------------------------
:func:`rollback_attempt` restores the tree **per file**, against the
attempt's baseline, and only after the evidence has been written:

* **the path is in the baseline** → ``git checkout <baseline> -- <path>``.
  The content — and the index entry — go back to what the attempt started
  from, whether the attempt edited the file, deleted it, or committed it.
* **the path is not in the baseline** → it is new, and a new file is
  removed: ``git rm --cached --ignore-unmatch`` takes it out of the index
  and the file itself is unlinked.  A git-ignored file is **left alone**
  and reported instead: an ignored file is not the attempt's to delete.
* **the path is declared** → it is dropped before anything happens.  The
  declaration is what the task was allowed to write; a rollback that
  removed it would throw away the task's own work.  A declaration names
  a *file*: when the declared path is a symlink, the file behind it is
  the declared file as well, so a write **through** the link is not
  mistaken for an overstep.

Three rules keep the rollback from becoming a second way to write to disk:

* a path that escapes the work tree (absolute, ``..``), or that points into
  ``.git/``, is **refused** — reported, and not touched;
* a path the evidence could not capture is **not restored**: without the
  patch, a restore would destroy content nobody could get back;
* nothing is ever **committed**.  The restore leaves the difference visible
  in the work tree, where the next attempt and the next reviewer can see
  it.

The first rule is decided **once**, in one place (:func:`_has_git_segment`
and the checks around it), and both halves of the rollback ask it the same
question.  The evidence half decides what the patch may talk about; the
restore half decides what may be touched.  Two copies of the rule would be
two rules — and the drift between them shows up as a patch that carries
the repository's own configuration and a restore that then deletes it.

What is *not* captured
----------------------
A path that escapes the working tree, or that points into ``.git/``, is
**reported** in the returned list and never raises: one bad path must not
cost the evidence for the good ones, and a rollback must not crash the
executor that asked for it.  "Reported" means reported — a path the
rollback cannot handle is named in ``failed``, including one that is empty
or made of nothing but blanks.  Silently dropping it would claim that
nothing went wrong.

The same holds for the tree and the baseline themselves.  A directory that
is not a repository, and a baseline that names no commit, leave nothing to
diff against — every path is then reported as failed.  Guessing "the file
must be new" would write a patch that *claims* the file did not exist at
the baseline, and a false statement about an attempt is worse than no
evidence at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence, Set

__all__ = [
    "RollbackResult",
    "format_rollback_failure",
    "rollback_attempt",
    "write_rollback_patch",
]

#: How long one git call may take.  A hung git must not hang the executor
#: that is trying to record what an attempt did.
GIT_TIMEOUT_SEC = 60


@dataclass
class RollbackResult:
    """What one attempt-level rollback did.

    Attributes:
        rolled_back: The paths that were put back, in the order they were
            handled.
        patch_path: Where the evidence patch was written.
        failed: The paths that could **not** be rolled back or captured.
    """

    rolled_back: List[str] = field(default_factory=list)
    patch_path: str = ""
    failed: List[str] = field(default_factory=list)


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess:
    """Run ``git`` inside ``root``.

    The exit code is not judged here — every caller decides what a non-zero
    code means for it.
    """
    return subprocess.run(
        ["git", "-C", str(root), *[str(arg) for arg in args]],
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_SEC,
    )


def _git_text(proc: subprocess.CompletedProcess) -> str:
    """The stdout of a git call, decoded."""
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _git_failed(proc: subprocess.CompletedProcess) -> bool:
    """True when git reported a hard error.

    ``git diff --no-index`` exits **1** both for "there is a difference" and
    for "I could not read that file" — the two are told apart by stderr, not
    by the exit code.
    """
    for line in _git_text_stderr(proc).splitlines():
        stripped = line.strip()
        if stripped.startswith("error:") or stripped.startswith("fatal:"):
            return True
    return False


def _git_text_stderr(proc: subprocess.CompletedProcess) -> str:
    """The stderr of a git call, decoded."""
    return (proc.stderr or b"").decode("utf-8", errors="replace")


def _git_root(root: Path) -> Path:
    """The root of the work tree ``root`` belongs to.

    Falls back to ``root`` itself when git cannot answer — the caller then
    treats every path as uncapturable, which is the honest answer for a
    directory that is not a repository.
    """
    try:
        proc = _git(root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return root
    if proc.returncode != 0:
        return root
    text = _git_text(proc).strip()
    if not text:
        return root
    return Path(text)


def _has_git_segment(parts: Iterable[str]) -> bool:
    """True when one of ``parts`` is the repository's own ``.git`` directory.

    The comparison is case-insensitive.  On a case-insensitive filesystem
    ``.GIT`` and ``.git`` name the very same directory, so a spelling
    that differs only in case still points into the repository's metadata
    — and a rollback that treated it as ordinary content would delete
    the repository's own configuration.  The judgement is made on the
    **segment** and never on whether the path happens to exist, so a
    case-sensitive filesystem refuses it for the same reason.

    Both halves of the rollback ask this question, and they ask it
    *here*: the evidence half decides what the patch may talk about, the
    restore half decides what may be touched, and one rule leaves no
    room for the two to drift apart.
    """
    return any(str(part).casefold() == ".git" for part in parts)


def _safe_target(root: Path, raw: str) -> Optional[Path]:
    """The path ``raw`` names inside ``root``, or ``None`` when it is unsafe.

    Unsafe means: empty, absolute, escaping the working tree, or pointing
    into ``.git/``.  The check is lexical — a symlink inside the tree that
    points outside it is a different question, and answering it here would
    make the result depend on the state of the filesystem rather than on
    the path that was asked about.

    This is the evidence half's judgement, and it is the same judgement
    the restore half makes (:func:`_safe_rel_path`): both refuse a
    ``.git`` segment however it is spelled.
    """
    text = str(raw).strip()
    if not text:
        return None
    if os.path.isabs(text) or text.startswith("~"):
        return None
    try:
        parts = PurePosixPath(text.replace(os.sep, "/")).parts
    except (TypeError, ValueError):
        return None
    if not parts:
        return None
    if any(part == ".." for part in parts):
        return None
    if _has_git_segment(parts):
        return None
    base = Path(os.path.normpath(str(root)))
    target = Path(os.path.normpath(str(base / text)))
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if target == base:
        return None
    return target


def _repo_relative(git_root: Path, target: Path) -> Optional[str]:
    """``target`` as a POSIX path relative to the work tree root.

    ``None`` when the target is outside the work tree — a patch may only
    talk about paths the work tree owns.
    """
    try:
        relative = os.path.relpath(str(target), str(git_root))
    except ValueError:
        return None
    text = relative.replace(os.sep, "/")
    if text == "." or text.startswith("../"):
        return None
    return text


def _in_baseline(git_root: Path, baseline: str, repo_rel: str) -> bool:
    """True when ``repo_rel`` existed in the baseline commit."""
    try:
        proc = _git(git_root, ["cat-file", "-e", f"{baseline}:{repo_rel}"])
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _is_repository(git_root: Path) -> bool:
    """True when ``git_root`` is inside a git repository."""
    try:
        proc = _git(git_root, ["rev-parse", "--git-dir"])
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _resolve_baseline(git_root: Path, baseline: str) -> Optional[str]:
    """The commit the baseline names, or ``None`` when it names nothing.

    A baseline that does not resolve is not a small problem: without it
    there is no "before" to diff against, and pretending the files are new
    would write a patch that *claims* they did not exist at the baseline.
    A false statement about the attempt is worse than no evidence.
    """
    text = str(baseline).strip()
    if not text:
        return None
    try:
        proc = _git(git_root, ["rev-parse", "--verify", f"{text}^{{commit}}"])
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    resolved = _git_text(proc).strip()
    return resolved or None


def _diff_bytes(
    git_root: Path,
    baseline: str,
    repo_rel: str,
    target: Path,
) -> Optional[bytes]:
    """The patch of one path, relative to the baseline.

    Returns ``None`` when the difference could not be produced.
    """
    if _in_baseline(git_root, baseline, repo_rel):
        try:
            proc = _git(git_root, ["diff", baseline, "--binary", "--", repo_rel])
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0 or _git_failed(proc):
            return None
        return proc.stdout or b""

    # Not in the baseline: the path is new.  It may have been committed
    # since the baseline or never committed at all — the ``--no-index`` form
    # captures both, because it compares the file against nothing.
    if not target.exists():
        # Neither in the baseline nor on disk: there is nothing to capture.
        return b""
    try:
        proc = _git(
            git_root, ["diff", "--no-index", "--binary", "--", "/dev/null", repo_rel]
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Exit code 1 means "there is a difference"; the error lines on stderr
    # are what tell a real failure apart from that answer.
    if proc.returncode not in (0, 1) or _git_failed(proc):
        return None
    return proc.stdout or b""


def _header(baseline: str, captured: Sequence[str], failed: Sequence[str]) -> bytes:
    """The provenance block written above the patch."""
    lines = [
        "# attempt rollback evidence\n",
        f"# baseline: {baseline}\n",
        f"# captured: {len(captured)} path(s)\n",
        "# failed: " + (", ".join(failed) if failed else "(none)") + "\n",
    ]
    return "".join(lines).encode("utf-8")


def write_rollback_patch(
    project_dir,
    baseline: str,
    paths: Iterable[str],
    patch_path,
) -> List[str]:
    """Write the patch of ``paths`` relative to ``baseline``.

    Args:
        project_dir: The tree the attempt worked in.  Paths are read as
            relative to it.
        baseline: The commit the attempt started from.  The patch is the
            difference between that commit and the tree as it stands now —
            which is why a change the attempt **committed** is still
            captured.  A baseline that names no commit captures nothing.
        paths: The paths to capture.
        patch_path: Where the patch is written.  The parent directories are
            created; the file is written even when there is no difference
            at all, because "the attempt changed nothing" is also a fact
            worth keeping.

    Returns:
        The paths that could **not** be captured — the ones that escape
        the working tree, point into ``.git/``, or that git could not
        diff.  An empty list means every path was captured.
    """
    root = Path(str(project_dir)).expanduser().resolve()
    out = Path(str(patch_path)).expanduser()
    git_root = _git_root(root)

    # A tree that is not a repository, or a baseline that names no commit,
    # leaves nothing to diff against — every path is reported as failed.
    usable = _is_repository(git_root)
    resolved = _resolve_baseline(git_root, baseline) if usable else None
    if resolved is None:
        usable = False

    failed: List[str] = []
    captured: List[str] = []
    chunks: List[bytes] = []

    seen = set()
    for raw in paths or []:
        text = str(raw)
        if text in seen:
            continue
        seen.add(text)
        if not usable:
            failed.append(text)
            continue
        target = _safe_target(root, text)
        if target is None:
            failed.append(text)
            continue
        repo_rel = _repo_relative(git_root, target)
        if repo_rel is None:
            failed.append(text)
            continue
        chunk = _diff_bytes(git_root, resolved, repo_rel, target)
        if chunk is None:
            failed.append(text)
            continue
        captured.append(text)
        if chunk:
            chunks.append(chunk)

    out.parent.mkdir(parents=True, exist_ok=True)
    payload = _header(str(baseline), captured, failed) + b"".join(chunks)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, out)
    return failed


# ---------------------------------------------------------------------------
# The restore half
# ---------------------------------------------------------------------------


class _RollbackUnavailable(Exception):
    """The rollback cannot run at all — no repository, or no baseline.

    Raised only for the two conditions that make *every* path unrestorable.
    It never leaves this module: the caller is handed a report of the
    failure, not an exception.
    """


def _normalise_path(text) -> str:
    """The repo-relative POSIX spelling of ``text``.

    ``./a.py`` and ``a.py`` are the same file, so both spellings have to
    land on the same key — otherwise the comparison against the
    declaration compares strings instead of files.
    """
    norm = str(text).strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _safe_rel_path(root: Path, raw: str) -> Optional[str]:
    """The path ``raw`` names inside ``root``, as a relative POSIX path.

    ``None`` when the path is not one the rollback may touch: empty,
    absolute, escaping the working tree, or pointing into ``.git/``.

    The check is lexical — a symlink inside the tree that points outside it
    is a different question, and answering it here would make the result
    depend on the state of the filesystem rather than on the path that was
    asked about.

    This is the restore half's judgement, and it is the same judgement
    the evidence half makes (:func:`_safe_target`): both refuse a
    ``.git`` segment however it is spelled.
    """
    text = str(raw).strip()
    if not text:
        return None
    if os.path.isabs(text) or text.startswith("~"):
        return None
    normalised = text.replace(os.sep, "/")
    parts = PurePosixPath(normalised).parts
    if not parts:
        return None
    if any(part == ".." for part in parts):
        return None
    if _has_git_segment(parts):
        return None
    base = Path(os.path.normpath(str(root)))
    target = Path(os.path.normpath(str(base / normalised)))
    try:
        relative = target.relative_to(base)
    except ValueError:
        return None
    rel = relative.as_posix()
    if not rel or rel == ".":
        return None
    return rel


def _identity_spellings(root: Path, git_root: Path, target: Path) -> Set[str]:
    """The spellings that name the *file* ``target`` points at.

    A symlink inside the tree is one file with two names: ``link.py`` and
    the file it points at.  Resolving the link turns both spellings into
    one key, so the comparison asks about the file rather than about the
    way the path happened to be spelled.

    Only files inside the tree are answered.  A link that leaves the tree
    names a file the rollback does not own, and answering for it would
    make two unrelated paths look like the same file.

    The resolution serves the **matching** only: the paths the rollback
    writes to stay the lexical ones (:func:`_safe_rel_path`).
    """
    base = Path(os.path.normpath(os.path.realpath(str(root))))
    resolved = Path(os.path.normpath(os.path.realpath(str(target))))
    try:
        inside = resolved.relative_to(base).as_posix()
    except ValueError:
        return set()
    spellings = {inside}
    repo_rel = _repo_relative(git_root, resolved)
    if repo_rel:
        spellings.add(repo_rel)
    return spellings


def _declaration_bases(root: Path, git_root: Path) -> List[Path]:
    """The directories a relative declaration may be written against.

    A declaration is written by the task, and the task may spell it
    relative to the project directory it worked in **or** relative to the
    root of the work tree.  The two differ exactly when the project
    directory is a subdirectory of the repository — and a declaration
    read against the wrong base names a file that does not exist, so the
    comparison would quietly protect nothing.

    The flat layout — the project directory *is* the work tree — has one
    base, and behaves exactly as it did before.
    """
    bases = [root]
    try:
        same = os.path.realpath(str(root)) == os.path.realpath(str(git_root))
    except OSError:  # pragma: no cover - realpath does not raise on POSIX
        same = str(root) == str(git_root)
    if not same:
        bases.append(git_root)
    return bases


def _record_declaration(
    keys: Set[str], root: Path, git_root: Path, base: Path, candidate: str
) -> None:
    """Record every spelling that names the file ``base/candidate``.

    Three names are recorded for one file: the spelling relative to the
    base it was read against, the spelling relative to the work tree
    root, and the spellings the file answers to as a file — the name
    behind a symlink and the name of the link itself.
    """
    keys.add(_normalise_path(candidate))
    repo_rel = _repo_relative(git_root, base / candidate)
    if repo_rel:
        keys.add(_normalise_path(repo_rel))
    for spelling in _identity_spellings(root, git_root, base / candidate):
        keys.add(_normalise_path(spelling))


def _declared_keys(
    root: Path, git_root: Path, declared_paths: Iterable[str]
) -> Set[str]:
    """The spellings that count as "declared" for this rollback.

    A declared path may be written relative to the project directory or
    relative to the work tree root, and the two differ when the project
    directory is a subdirectory of the repository.  Both spellings are
    recorded, so the comparison asks about the *file* and not about the
    way the caller happened to spell it.

    The file behind a symlink is recorded as well: a declaration names a
    file, and the link and the file it points at are one file with two
    names.  Without it, an attempt that wrote *through* a declared link
    would have its target rolled back — the deliverable destroyed by a
    rollback that was meant to undo the overstep.

    Every relative declaration is therefore read against **each** base it
    could have been written against (:func:`_declaration_bases`).  Reading
    it against the wrong one names a file that does not exist, and the
    identity comparison then protects nothing at all.

    A declaration is judged like any other path.  It is only another way
    of naming a file, and the one thing a rollback must never touch is
    the repository's own directory — so a declaration naming ``.git/`` is
    refused here as well, whether it is spelled relatively or reaches the
    same file by an absolute route.  A declaration that protected the
    repository's metadata would be a declaration the task never made.
    """
    keys: Set[str] = set()
    for raw in declared_paths or []:
        text = _normalise_path(raw)
        if not text:
            continue
        if os.path.isabs(str(raw).strip()):
            # An absolute declaration is legal as long as it names a file
            # inside the tree the rollback owns.  Both sides are resolved
            # before they are compared: a path that reaches the tree
            # through a symlink (``/tmp`` -> ``/private/tmp``) still names
            # the same file, and the comparison is about the file.
            base = Path(os.path.normpath(os.path.realpath(str(root))))
            target = Path(os.path.normpath(os.path.realpath(str(raw).strip())))
            try:
                candidate = target.relative_to(base).as_posix()
            except ValueError:
                candidate = None
            if candidate is None:
                continue
            # The same judgement the request goes through: a declaration
            # may not name the repository's own directory, however it
            # was spelled.
            if _safe_rel_path(root, candidate) is None:
                continue
            _record_declaration(keys, root, git_root, root, candidate)
            continue
        for base in _declaration_bases(root, git_root):
            candidate = _safe_rel_path(base, text)
            if candidate is None:
                continue
            _record_declaration(keys, root, git_root, base, candidate)
    return keys


def _is_declared(rel: str, root: Path, git_root: Path, keys: Set[str]) -> bool:
    """Whether ``rel`` names a file the task declared."""
    if not keys:
        return False
    if _normalise_path(rel) in keys:
        return True
    repo_rel = _repo_relative(git_root, root / rel)
    if repo_rel and _normalise_path(repo_rel) in keys:
        return True
    # The same file may be spelled two ways — ``link.py`` and the file it
    # points at.  Comparing the identities answers both directions: a
    # declared link protects its target, and a declared target protects
    # the link that names it.
    for spelling in _identity_spellings(root, git_root, root / rel):
        if _normalise_path(spelling) in keys:
            return True
    return False


def _safe_component(text) -> str:
    """``text`` reduced to one harmless path component.

    The task id and the attempt number become directory and file names
    under the evidence directory, so they may not carry a separator, a
    ``..`` or anything else that could point somewhere else on disk.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text).strip())
    if not cleaned or cleaned in (".", ".."):
        return "unknown"
    return cleaned


def _evidence_path(plan_dir, root: Path, task_id: str, attempt) -> Path:
    """Where the evidence patch of one attempt belongs.

    ``<plan>/.pdt/evidence/rollbacks/<task id>/<attempt>.patch``.  The
    ``.pdt/`` directory is runtime territory (and is ignored by the
    repository), so the evidence never shows up as a change of the task's
    own.  Without a plan directory the project directory holds it.
    """
    base = Path(str(plan_dir)).expanduser() if plan_dir else root
    return (
        base
        / ".pdt"
        / "evidence"
        / "rollbacks"
        / _safe_component(task_id)
        / (_safe_component(attempt) + ".patch")
    )


def _is_ignored(git_root: Path, repo_rel: str) -> Optional[bool]:
    """Whether git ignores ``repo_rel``.

    ``True`` when the path is ignored, ``False`` when it is not, and
    ``None`` when git could not tell — the third answer is a refusal, not
    a guess: a file nobody can classify must not be deleted.
    """
    try:
        proc = _git(git_root, ["check-ignore", "-q", "--", repo_rel])
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _remove_from_index(git_root: Path, repo_rel: str) -> bool:
    """Take ``repo_rel`` out of the index, leaving the file on disk.

    ``--ignore-unmatch`` keeps a path that was never staged from being an
    error.  git refuses the plain form when the index holds content that
    differs from both the file and HEAD — which is exactly the shape of an
    attempt that staged its own overstep — so the forced form is tried
    after it.  Forcing is safe here: ``--cached`` never deletes anything
    from the work tree, and the unlink that follows is ours.
    """
    try:
        proc = _git(git_root, ["rm", "--cached", "--ignore-unmatch", "--", repo_rel])
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode == 0:
        return True
    try:
        forced = _git(
            git_root, ["rm", "--cached", "-f", "--ignore-unmatch", "--", repo_rel]
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return forced.returncode == 0


def _restore_one(git_root: Path, baseline: str, root: Path, rel: str) -> bool:
    """Put one path back to the baseline.  ``True`` when it worked.

    A path that was in the baseline is checked out from it; a path that
    was not is new, and is removed from the index and from the disk —
    unless git ignores it, in which case it is left alone.
    """
    repo_rel = _repo_relative(git_root, root / rel)
    if repo_rel is None:
        return False
    if _in_baseline(git_root, baseline, repo_rel):
        try:
            proc = _git(git_root, ["checkout", baseline, "--", repo_rel])
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    # Not in the baseline: the path is new.  An ignored file is not the
    # attempt's to delete, and a path git cannot classify is not ours to
    # guess about.
    if _is_ignored(git_root, repo_rel) is not False:
        return False
    if not _remove_from_index(git_root, repo_rel):
        return False
    target = root / rel
    try:
        if os.path.lexists(str(target)):
            target.unlink()
    except OSError:
        return False
    return True


def rollback_attempt(
    project_dir,
    baseline: str,
    paths: Iterable[str],
    task_id: str,
    attempt,
    plan_dir=None,
    declared_paths: Iterable[str] = (),
) -> RollbackResult:
    """Put the paths an attempt overstepped back to the attempt's baseline.

    The order is the point: the evidence is captured **first**, because a
    patch is the difference between two states and once the tree is put
    back that difference no longer exists.

    Args:
        project_dir: The tree the attempt worked in.  Paths are read as
            relative to it.
        baseline: The commit the attempt started from.  A path that was in
            that commit is restored from it; a path that was not is new,
            and is removed.
        paths: The paths the attempt touched.
        task_id: The task the attempt belongs to.  It names the evidence
            directory.
        attempt: The attempt number.  It names the evidence file.
        plan_dir: Where the evidence patch is written.  Without it, the
            project directory holds it.
        declared_paths: The paths the task declared.  They are never
            touched — the declaration is what the task was allowed to
            write.

    Returns:
        A :class:`RollbackResult` naming what was rolled back, where the
        evidence patch is, and what could not be rolled back.  Nothing
        here raises: a rollback that cannot run reports every path as
        failed and leaves ``patch_path`` empty.
    """
    root = Path(str(project_dir)).expanduser().resolve()
    # Every requested path is kept, including one that is empty or made
    # of nothing but blanks.  Such a path names nothing the rollback can
    # touch, and it is therefore **reported** as failed — dropping it
    # would turn "the attempt named a path I cannot handle" into "the
    # attempt named no path at all", which is a claim nobody could check.
    requested = [str(p) for p in (paths or [])]

    try:
        git_root = _git_root(root)
        if not _is_repository(git_root):
            raise _RollbackUnavailable("not a git repository")
        resolved = _resolve_baseline(git_root, baseline)
        if resolved is None:
            raise _RollbackUnavailable("baseline names no commit")
        declared = _declared_keys(root, git_root, declared_paths)

        # Declared paths are dropped before anything else happens: they
        # are the task's own work, not an overstep.
        candidates: List[tuple] = []
        failed: List[str] = []
        for raw in requested:
            rel = _safe_rel_path(root, raw)
            if rel is None:
                failed.append(raw)
                continue
            if _is_declared(rel, root, git_root, declared):
                continue
            candidates.append((raw, rel))

        # Evidence first: a restore without it destroys content nobody
        # could get back.
        patch_path = _evidence_path(plan_dir, root, task_id, attempt)
        capture_failed = set(
            write_rollback_patch(root, str(baseline), [rel for _, rel in candidates], patch_path)
        )

        rolled_back: List[str] = []
        for raw, rel in candidates:
            if rel in capture_failed:
                failed.append(raw)
                continue
            if not _restore_one(git_root, resolved, root, rel):
                failed.append(raw)
                continue
            rolled_back.append(raw)

        return RollbackResult(
            rolled_back=rolled_back, patch_path=str(patch_path), failed=failed
        )
    except Exception:  # noqa: BLE001 - a rollback never takes the executor down
        # Without a repository or a baseline there is nothing to restore
        # against, so nothing is touched and every path is reported.
        return RollbackResult(
            rolled_back=[], patch_path="", failed=list(requested)
        )


def format_rollback_failure(undeclared_paths: Iterable[str], result) -> str:
    """The failure text of a rollback that hit an undeclared write.

    The first line is the executor's own spelling — ``undeclared_write:
    <paths>`` — so the failure keeps the identity it had before the
    rollback ran.  The lines after it carry the outcome: where the
    evidence is, what was rolled back, and what could not be.

    Args:
        undeclared_paths: The paths the attempt wrote without declaring.
        result: The :class:`RollbackResult` of the rollback.

    Returns:
        The text, one fact per line.
    """
    paths = [p for p in (_normalise_path(p) for p in (undeclared_paths or [])) if p]
    lines = [
        "undeclared_write: " + (", ".join(paths) if paths else "(none)"),
        "rollback_patch: " + (str(getattr(result, "patch_path", "") or "") or "(none)"),
        "rollback_rolled_back: "
        + _join_paths(getattr(result, "rolled_back", None) or []),
        "rollback_failed: " + _join_paths(getattr(result, "failed", None) or []),
    ]
    return "\n".join(lines)


def _join_paths(paths: Sequence[str]) -> str:
    """The paths as one comma-separated line, or ``(none)``."""
    items = [str(p) for p in paths if str(p).strip()]
    return ", ".join(items) if items else "(none)"
