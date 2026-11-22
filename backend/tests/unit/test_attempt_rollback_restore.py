"""Partial rollback: the tree goes back to the baseline — and says why not.

Background
----------
A sub agent that oversteps its declared file scope sometimes **commits** its
own work.  The attempt then ends with an empty diff (the change already sits
in a commit), the failure gets attributed to the wrong cause, and the next
attempt starts from a tree that still carries the overstep.

Rolling that attempt back needs three things, in this order:

1. the list of paths the attempt touched,
2. the **content** of what it touched — captured *before* the rollback,
   because after the rollback the difference no longer exists,
3. the restore itself — per file, against the attempt's baseline.

This file pins the restore half: :func:`rollback_attempt` puts the tree
back file by file, and :func:`format_rollback_failure` turns the outcome
into the text the executor hands to the next attempt.

The rules that make a partial rollback safe
-------------------------------------------
* **Declared paths are never touched.**  The declaration is what the task
  was *allowed* to write; a rollback that removed it would throw away the
  task's own work.  A declaration names a *file*, so a declared symlink
  protects the file behind it — and vice versa.
* **A path that is not in the baseline is new**, and new paths are removed
  — unless git ignores them, in which case they are left alone and
  reported.
* **A path that escapes the work tree is refused**, and nothing is
  touched: a rollback must not become a way to write outside the tree.
* **One failure does not raise.**  A rollback runs while the executor is
  already handling a failure; raising would cost the evidence for the
  good paths and hide the real cause.

TDD contract — 8 test cases
---------------------------
1. ``test_tracked_path_is_restored_to_baseline`` — a tracked file the
   attempt edited and committed: the content goes back to the baseline.
2. ``test_new_file_committed_during_attempt_is_removed`` — a file the
   attempt created and committed: neither the work tree nor the index
   holds it any more.
3. ``test_declared_paths_are_never_touched`` — a declared path survives
   the rollback and is not reported as rolled back.
4. ``test_ignored_untracked_file_is_left_alone`` — a git-ignored file is
   not deleted; it is reported as failed.
5. ``test_path_outside_worktree_is_refused`` — ``../evil`` is refused and
   the file outside the tree is untouched.
6. ``test_patch_written_before_restore`` — the evidence patch still holds
   the reverted content after the tree was put back.
7. ``test_format_rollback_failure`` — the failure text names the paths
   first and then the rollback outcome.
8. ``test_unusable_baseline_fails_every_path`` — a baseline that names no
   commit rolls nothing back, touches nothing, and leaves ``patch_path``
   empty.

The symlink clause
------------------
A declaration names a **file**, and a symlink inside the tree is one file
with two names.  Five cases pin that down:

* ``test_declared_symlink_protects_the_file_it_points_at`` — the attempt
  wrote *through* a declared link, so git reports the **target** as the
  changed path; the target is the declared file and survives.
* ``test_declared_file_protects_the_spelling_of_its_link`` — the other
  direction: a declared file protects the link that names it.
* ``test_declared_symlink_is_recognised_however_it_is_spelled`` —
  ``link.py``, ``./link.py`` and the absolute path are one declaration.
* ``test_declared_symlink_is_recognised_in_a_nested_project_directory`` —
  the same three spellings still name one declaration when the project
  directory is a *subdirectory* of the work tree, where the
  repository-relative spelling (``sub/link.py``) differs from the
  project-relative one (``link.py``).
* ``test_a_nested_declaration_protects_only_its_own_file`` — reading a
  declaration against two bases is not blanket protection: an undeclared
  sibling is still rolled back.
* ``test_a_link_leaving_the_tree_does_not_protect_anything`` — a link
  that points out of the tree names a file the rollback does not own, so
  it groups with nothing.

The spelling of the repository's own directory
----------------------------------------------
``.git`` holds the repository's metadata, not the task's work, and a
rollback may never touch it.  Five cases pin that down:

* ``test_git_internal_path_is_refused`` — the literal spelling is refused
  and reported.
* ``test_git_internal_path_in_another_case_is_refused`` — so is a spelling
  that differs only in case: on a case-insensitive filesystem ``.GIT``
  names the very same directory, and the judgement is made on the
  **segment**, never on whether the path happens to exist.
* ``test_nested_git_internal_path_in_another_case_is_refused`` — the same
  wherever in the path the segment appears.
* ``test_empty_path_is_reported_as_failed`` and
  ``test_whitespace_path_is_reported_as_failed`` — a path that is empty,
  or made of nothing but blanks, is **reported** rather than dropped: a
  rollback that discarded it would claim that nothing went wrong.
* ``test_both_halves_refuse_the_same_unsafe_paths`` — the evidence half
  and the restore half judge a path the same way, so the two cannot drift
  apart.
* ``test_git_internal_declaration_is_not_a_declared_key`` — a *declaration*
  is judged like any other path: it may not name the repository's own
  directory, whether it is spelled relatively or reaches the same file by
  an absolute route.
* ``test_a_declaration_of_ordinary_content_is_still_declared`` — the
  refusal is about the directory, not about the spelling: ``.gitignore``
  and ``.gitmodules`` are ordinary content and stay declarable.

Boundary conditions
-------------------
* Everything happens under ``tmp_path``: a throw-away git repository and a
  throw-away plan directory.  The repository the suite runs in is never
  touched.
* The tests need a real ``git`` binary (they drive a real repository) —
  they skip when there is none rather than pretending to pass.
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

from attempt_rollback import (  # noqa: E402
    RollbackResult,
    _declared_keys,
    _safe_rel_path,
    _safe_target,
    format_rollback_failure,
    rollback_attempt,
)

#: ``git`` refuses to commit without an identity; the identity belongs to
#: the throw-away repository, never to the machine's global config.
_GIT_IDENTITY = (
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=PDT Test",
)


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary is not available")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo``."""
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> str:
    """Create a repository holding one commit; return that commit's sha.

    The sha is the **baseline**: the commit the attempt starts from.
    """
    _require_git()
    repo.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    (repo / "tracked.txt").write_text("line1\nline2\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    commit = _git(repo, "commit", "-m", "initial")
    assert commit.returncode == 0, commit.stderr
    head = _git(repo, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    return head.stdout.strip()


def _commit(repo: Path, message: str) -> None:
    """Commit whatever is staged in ``repo``."""
    commit = _git(repo, "commit", "-m", message)
    assert commit.returncode == 0, commit.stderr


def _commit_symlink(repo: Path, link: str, target: str) -> str:
    """Track a symlink ``link -> target`` in ``repo``; return the new baseline.

    The link is committed like any other file: git stores it as a link
    (mode 120000), not as the file it points at.  The file behind it is
    tracked too — otherwise git would report the write through the link
    as a new file rather than as a change of the target.
    """
    os.symlink(target, repo / link)
    assert _git(repo, "add", link, target).returncode == 0
    _commit(repo, "add the symlink")
    head = _git(repo, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    return head.stdout.strip()


def test_tracked_path_is_restored_to_baseline(tmp_path: Path) -> None:
    """A tracked file the attempt edited goes back to the baseline."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    # The attempt edited a tracked file and committed its own work.
    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert _git(repo, "add", "tracked.txt").returncode == 0
    _commit(repo, "attempt commit")

    result = rollback_attempt(
        repo, baseline, ["tracked.txt"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == ["tracked.txt"]
    assert result.failed == []
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "line1\nline2\n"


def test_new_file_committed_during_attempt_is_removed(tmp_path: Path) -> None:
    """A file the attempt created is gone from the tree *and* the index."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "added.txt").write_text("brand new\n", encoding="utf-8")
    assert _git(repo, "add", "added.txt").returncode == 0
    _commit(repo, "attempt commit")

    result = rollback_attempt(
        repo, baseline, ["added.txt"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == ["added.txt"]
    assert result.failed == []
    assert not (repo / "added.txt").exists()
    assert "added.txt" not in _git(repo, "ls-files").stdout.split()


def test_declared_paths_are_never_touched(tmp_path: Path) -> None:
    """The task's own work is never rolled back."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "ok.py").write_text("declared work\n", encoding="utf-8")
    (repo / "bad.py").write_text("undeclared work\n", encoding="utf-8")

    result = rollback_attempt(
        repo,
        baseline,
        ["ok.py", "bad.py"],
        "repair-03",
        1,
        plan_dir=plan,
        declared_paths=["ok.py"],
    )

    assert (repo / "ok.py").read_text(encoding="utf-8") == "declared work\n"
    assert "ok.py" not in result.rolled_back
    assert "ok.py" not in result.failed
    assert result.rolled_back == ["bad.py"]
    assert not (repo / "bad.py").exists()


def test_declared_path_is_recognised_however_it_is_spelled(tmp_path: Path) -> None:
    """The declaration names a *file* — ``./ok.py`` and the absolute path
    are the same declaration."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "ok.py").write_text("declared work\n", encoding="utf-8")

    for declaration in ("./ok.py", str(repo / "ok.py")):
        result = rollback_attempt(
            repo,
            baseline,
            ["ok.py"],
            "repair-03",
            1,
            plan_dir=plan,
            declared_paths=[declaration],
        )
        assert result.rolled_back == []
        assert result.failed == []
        assert (repo / "ok.py").read_text(encoding="utf-8") == "declared work\n"


def test_declared_symlink_protects_the_file_it_points_at(tmp_path: Path) -> None:
    """A declared symlink protects the file it points at.

    The attempt wrote *through* the link, so git reports the target's
    path as the changed one.  A declaration names a file, and the link
    and its target are one file with two names — so the target is the
    declared file, and rolling it back would throw away the task's own
    work.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "real.py").write_text("original\n", encoding="utf-8")
    baseline = _commit_symlink(repo, "link.py", "real.py")

    # The attempt writes through the declared link: git blames the
    # target's path, not the link's.
    (repo / "link.py").write_text("updated\n", encoding="utf-8")
    assert " M real.py" in _git(repo, "status", "--porcelain").stdout

    result = rollback_attempt(
        repo,
        baseline,
        ["real.py"],
        "repair-03",
        1,
        plan_dir=plan,
        declared_paths=["link.py"],
    )

    assert result.rolled_back == []
    assert result.failed == []
    assert (repo / "real.py").read_text(encoding="utf-8") == "updated\n"
    assert os.readlink(repo / "link.py") == "real.py"


def test_declared_file_protects_the_spelling_of_its_link(tmp_path: Path) -> None:
    """The same file, spelled as its link, is still the declared file.

    The comparison runs in both directions: the declaration may name the
    link or the file behind it, and either spelling protects the file.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "real.py").write_text("original\n", encoding="utf-8")
    baseline = _commit_symlink(repo, "link.py", "real.py")

    (repo / "link.py").write_text("updated\n", encoding="utf-8")

    result = rollback_attempt(
        repo,
        baseline,
        ["link.py"],
        "repair-03",
        1,
        plan_dir=plan,
        declared_paths=["real.py"],
    )

    assert result.rolled_back == []
    assert result.failed == []
    assert (repo / "real.py").read_text(encoding="utf-8") == "updated\n"


def test_declared_symlink_is_recognised_however_it_is_spelled(tmp_path: Path) -> None:
    """``link.py``, ``./link.py`` and the absolute path are one declaration."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "real.py").write_text("original\n", encoding="utf-8")
    baseline = _commit_symlink(repo, "link.py", "real.py")

    for declaration in ("link.py", "./link.py", str(repo / "link.py")):
        (repo / "link.py").write_text("updated\n", encoding="utf-8")

        result = rollback_attempt(
            repo,
            baseline,
            ["real.py"],
            "repair-03",
            1,
            plan_dir=plan,
            declared_paths=[declaration],
        )

        assert result.rolled_back == [], declaration
        assert result.failed == [], declaration
        assert (
            (repo / "real.py").read_text(encoding="utf-8") == "updated\n"
        ), declaration


def test_declared_symlink_is_recognised_in_a_nested_project_directory(
    tmp_path: Path,
) -> None:
    """The same declaration, however the base directory spells it.

    The project directory is a *subdirectory* of the work tree, so the
    two spellings of one file differ: ``link.py`` names it relative to
    the project directory, ``sub/link.py`` relative to the repository
    root.  A declaration written the second way names the declared file
    all the same — the deliverable behind the link survives the
    rollback instead of being thrown away by it.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"

    project = repo / "sub"
    project.mkdir()
    (project / "real.py").write_text("original\n", encoding="utf-8")
    baseline = _commit_symlink(project, "link.py", "real.py")

    for declaration in ("link.py", "sub/link.py", str(project / "link.py")):
        # The attempt writes through the declared link, so git blames
        # the target's path, not the link's.  ``--porcelain`` reports it
        # relative to the repository root.
        (project / "link.py").write_text("updated\n", encoding="utf-8")
        assert " M sub/real.py" in _git(repo, "status", "--porcelain").stdout

        result = rollback_attempt(
            project,
            baseline,
            ["real.py"],
            "repair-03",
            1,
            plan_dir=plan,
            declared_paths=[declaration],
        )

        assert result.rolled_back == [], declaration
        assert result.failed == [], declaration
        assert (
            (project / "real.py").read_text(encoding="utf-8") == "updated\n"
        ), declaration
        assert os.readlink(project / "link.py") == "real.py", declaration


def test_a_nested_declaration_protects_only_its_own_file(tmp_path: Path) -> None:
    """Reading a declaration against two bases is not blanket protection.

    The criterion has two halves: the rollback undoes the overstep **and**
    it leaves the declared work alone.  The second half must not swallow
    the first — a sibling the task never declared is still an overstep,
    and it is still rolled back.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"

    project = repo / "sub"
    project.mkdir()
    (project / "real.py").write_text("original\n", encoding="utf-8")
    (project / "other.py").write_text("other original\n", encoding="utf-8")
    _commit_symlink(project, "link.py", "real.py")
    assert _git(project, "add", "other.py").returncode == 0
    _commit(project, "add the sibling")
    baseline = _git(project, "rev-parse", "HEAD").stdout.strip()

    # The attempt writes through the declared link and oversteps the
    # sibling, which it never declared.
    (project / "link.py").write_text("updated\n", encoding="utf-8")
    (project / "other.py").write_text("overstep\n", encoding="utf-8")

    result = rollback_attempt(
        project,
        baseline,
        ["real.py", "other.py"],
        "repair-03",
        1,
        plan_dir=plan,
        declared_paths=["sub/link.py"],
    )

    assert result.rolled_back == ["other.py"]
    assert result.failed == []
    assert (project / "real.py").read_text(encoding="utf-8") == "updated\n"
    assert (
        (project / "other.py").read_text(encoding="utf-8") == "other original\n"
    )
    assert os.readlink(project / "link.py") == "real.py"


def test_a_link_leaving_the_tree_does_not_protect_anything(tmp_path: Path) -> None:
    """A link that points out of the tree names a file the rollback does not own.

    Two links to the same file outside the tree are two paths, not one
    file: the identity of a path is only answered while it stays inside
    the tree the rollback owns.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"

    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    os.symlink(str(outside), repo / "inside.py")
    assert _git(repo, "add", "inside.py").returncode == 0
    _commit(repo, "add the escaping symlink")
    os.symlink(str(outside), repo / "twin.py")
    assert _git(repo, "add", "twin.py").returncode == 0
    _commit(repo, "add the twin symlink")
    baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = rollback_attempt(
        repo,
        baseline,
        ["twin.py"],
        "repair-03",
        1,
        plan_dir=plan,
        declared_paths=["inside.py"],
    )

    # ``twin.py`` was never declared: the escaping link does not make the
    # two paths the same file.
    assert result.rolled_back == ["twin.py"]
    assert result.failed == []
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_ignored_untracked_file_is_left_alone(tmp_path: Path) -> None:
    """A git-ignored file is not deleted — it is reported, not touched."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    assert _git(repo, "add", ".gitignore").returncode == 0
    _commit(repo, "add the ignore rule")
    (repo / "secret.txt").write_text("do not touch\n", encoding="utf-8")

    result = rollback_attempt(
        repo, baseline, ["secret.txt"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == ["secret.txt"]
    assert (repo / "secret.txt").read_text(encoding="utf-8") == "do not touch\n"


def test_path_outside_worktree_is_refused(tmp_path: Path) -> None:
    """A path that escapes the work tree is refused, not followed."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"
    (tmp_path / "evil.txt").write_text("outside\n", encoding="utf-8")

    result = rollback_attempt(
        repo, baseline, ["../evil.txt"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == ["../evil.txt"]
    assert (tmp_path / "evil.txt").exists()


def test_absolute_path_is_refused(tmp_path: Path) -> None:
    """An absolute path is refused — the rollback works on relative paths."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"
    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = rollback_attempt(
        repo, baseline, [str(repo / "tracked.txt")], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == [str(repo / "tracked.txt")]
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "line1\nline2\nline3\n"


def test_git_internal_path_is_refused(tmp_path: Path) -> None:
    """A path into ``.git/`` is refused: the repository is not the task's."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    result = rollback_attempt(
        repo, baseline, [".git/config"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == [".git/config"]
    assert (repo / ".git" / "config").exists()


def test_git_internal_path_in_another_case_is_refused(tmp_path: Path) -> None:
    """``.GIT/`` is the repository's own directory, whatever it is spelled.

    On a case-insensitive filesystem ``.GIT`` and ``.git`` name the very
    same directory, so a spelling that differs only in case still points
    into the repository's metadata.  The judgement is made on the
    **segment**, never on whether the path happens to exist — a
    case-sensitive filesystem refuses it for the same reason.
    """
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"
    config = repo / ".git" / "config"
    before = config.read_bytes()

    result = rollback_attempt(
        repo, baseline, [".GIT/config"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == [".GIT/config"]
    assert config.read_bytes() == before


def test_nested_git_internal_path_in_another_case_is_refused(tmp_path: Path) -> None:
    """A ``.GIT`` segment is refused wherever it appears in the path."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"
    (repo / "sub").mkdir()
    (repo / "sub" / "hooks").mkdir()

    result = rollback_attempt(
        repo, baseline, ["sub/.GIT/hooks/x"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == ["sub/.GIT/hooks/x"]


def test_empty_path_is_reported_as_failed(tmp_path: Path) -> None:
    """An empty path is reported, not silently dropped.

    Dropping it would hide the fact that the attempt named a path the
    rollback cannot touch: "nothing failed" would be a claim the caller
    has no way to check.
    """
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    result = rollback_attempt(repo, baseline, [""], "repair-03", 1, plan_dir=plan)

    assert result.rolled_back == []
    assert result.failed == [""]


def test_whitespace_path_is_reported_as_failed(tmp_path: Path) -> None:
    """A path made of nothing but blanks is reported as well."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    result = rollback_attempt(repo, baseline, ["   "], "repair-03", 1, plan_dir=plan)

    assert result.rolled_back == []
    assert result.failed == ["   "]


def test_both_halves_refuse_the_same_unsafe_paths(tmp_path: Path) -> None:
    """The evidence half and the restore half judge a path the same way.

    Two copies of the same rule are two rules: the one that lets a path
    through decides what the evidence may talk about, and the one that
    refuses it decides what the restore may touch.  When they drift, a
    path can be written into the evidence patch and then destroyed by the
    restore — or refused by the restore after the evidence already
    captured it.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    unsafe = (
        "",
        "   ",
        "/etc/passwd",
        "~/notes.txt",
        "../evil.txt",
        ".git/config",
        ".GIT/config",
        "sub/.git/hooks/x",
        "sub/.GIT/hooks/x",
    )

    for raw in unsafe:
        assert _safe_rel_path(repo, raw) is None, raw
        assert _safe_target(repo, raw) is None, raw


def test_git_internal_declaration_is_not_a_declared_key(tmp_path: Path) -> None:
    """A declaration may not name the repository's own directory.

    The declaration is the one thing the rollback is forbidden to touch,
    so a declaration that names ``.git/`` would protect the repository's
    own metadata.  The guard that refuses such a path in the *request*
    must refuse it in the *declaration* too: a declaration is only
    another way of naming a path, and it goes through the same
    judgement.

    Both spellings are checked — the relative one and the absolute one,
    which reaches the same file by a different route.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    relative = _declared_keys(repo, repo, [".git/config", ".GIT/config"])
    assert ".git/config" not in relative
    assert ".GIT/config" not in relative

    absolute = _declared_keys(repo, repo, [str(repo / ".git" / "config")])
    assert ".git/config" not in absolute
    assert str(repo / ".git" / "config") not in absolute


def test_a_declaration_of_ordinary_content_is_still_declared(
    tmp_path: Path,
) -> None:
    """The declaration guard refuses the repository's directory only.

    A name that merely *starts* with ``.git`` is ordinary content, and a
    task is allowed to declare it.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    keys = _declared_keys(repo, repo, [".gitignore", ".gitmodules"])
    assert ".gitignore" in keys
    assert ".gitmodules" in keys


def test_patch_written_before_restore(tmp_path: Path) -> None:
    """The evidence still holds the reverted content after the restore."""
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    plan = tmp_path / "plan"

    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = rollback_attempt(
        repo, baseline, ["tracked.txt"], "repair-03", 1, plan_dir=plan
    )

    assert result.patch_path == str(
        plan / ".pdt" / "evidence" / "rollbacks" / "repair-03" / "1.patch"
    )
    patch = Path(result.patch_path)
    assert patch.exists()
    assert "+line3" in patch.read_text(encoding="utf-8")
    # The tree is back at the baseline; the difference is gone from the
    # tree but still readable in the evidence.
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "line1\nline2\n"


def test_format_rollback_failure(tmp_path: Path) -> None:
    """The failure text names the paths first, then the rollback outcome."""
    patch = tmp_path / "plan" / ".pdt" / "evidence" / "rollbacks" / "repair-03" / "1.patch"
    result = RollbackResult(
        rolled_back=["scripts/x.sh"],
        patch_path=str(patch),
        failed=["scripts/y.sh"],
    )

    text = format_rollback_failure(["scripts/x.sh", "scripts/y.sh"], result)

    lines = text.splitlines()
    assert lines[0] == "undeclared_write: scripts/x.sh, scripts/y.sh"
    assert f"rollback_patch: {patch}" in text
    assert "rollback_rolled_back: scripts/x.sh" in text
    assert "rollback_failed: scripts/y.sh" in text


def test_unusable_baseline_fails_every_path(tmp_path: Path) -> None:
    """A baseline that names no commit rolls nothing back and touches nothing.

    Without a baseline there is no "before" to restore, and guessing would
    write a claim about the attempt that is not true.  The whole rollback
    is reported as failed, with no evidence path.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = tmp_path / "plan"
    (repo / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = rollback_attempt(
        repo, "not-a-commit", ["tracked.txt"], "repair-03", 1, plan_dir=plan
    )

    assert result.rolled_back == []
    assert result.failed == ["tracked.txt"]
    assert result.patch_path == ""
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "line1\nline2\nline3\n"
