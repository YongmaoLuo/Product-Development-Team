"""The scanned set is the set the repository publishes.

Why this gate exists
--------------------
A static gate that exists to catch a private name must read the files
the repository **publishes**.  It used to read more than that: the
walker walked every tracked top-level directory on the filesystem, and
two of those directories are places the application *writes* while it
runs —

* ``tmp/`` — the verification servers' logs, the state databases, the
  per-round plan directories a plan's ``services[].start_cmd`` creates;
* ``.playwright-mcp/`` — the page snapshots the browser-automation
  tooling captures.

Both directories hold a handful of tracked files (a README, a
``.gitignore``, a few sample fixtures) and both are written to while
the suite runs.  A snapshot the browser tooling wrote three minutes ago
is not published: it is untracked, it is gitignored, and no commit will
ever contain it.

That is not hypothetical.  On 2026-11-01 the privacy gate failed on six
home paths that were **all** inside untracked ``.playwright-mcp/``
snapshots; the tracked files of the whole repository contained zero
hits.  The gate was rejecting a batch of files that can never ship, and
it failed on the machine that ran the browser step and only on that
machine — so the failure was not reproducible anywhere else and looked
like a flake.

What this file pins
--------------------
1. **A runtime root contributes only the files git tracks in it.**  The
   tracked set is read from git itself (``git ls-files``), not from a
   list kept beside the walker, so the two cannot drift apart.
2. **A source root still reads the working tree.**  An uncommitted
   source file is exactly what a privacy gate has to catch, so the
   source roots keep their filesystem walk.
3. **A runtime root whose tracked set cannot be read falls back to the
   filesystem walk.**  A gate that silently stopped reading a directory
   because it could not ask git would pass on the very files it exists
   to catch.

What it does not check
----------------------
That the runtime roots are the *right* ones, or that any gate's
patterns are any good.  It answers one question — does the scan read
files that can never be published — and answers it by deriving the
tracked set from the repository rather than restating a list somebody
has to remember to update.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import source_scan


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def runtime_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository whose ``tmp/`` holds one tracked file and one artifact.

    The walker is pointed at the temporary repository rather than at the
    real one: the behaviour under test is "which files does a runtime
    root contribute", and that is answered by the walker's rules, not by
    whatever this machine happens to have written into its ``tmp/``.
    """
    repo = tmp_path / "repo"
    (repo / "tmp").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "gate@example.invalid", cwd=repo)
    _git("config", "user.name", "Gate", cwd=repo)
    (repo / "tmp" / "README.md").write_text("tracked\n", encoding="utf-8")
    _git("add", "tmp/README.md", cwd=repo)
    _git("commit", "-q", "-m", "tracked", cwd=repo)
    # The artifact: written by a tool, untracked, and therefore never
    # publishable.
    (repo / "tmp" / "artifact.md").write_text(
        "/Users/someone/private/notes.md\n", encoding="utf-8"
    )
    monkeypatch.setattr(source_scan, "REPO_ROOT", repo)
    monkeypatch.setattr(
        source_scan,
        "_RUNTIME_ROOT_PATHS",
        frozenset(source_scan.resolve_root(root) for root in source_scan.RUNTIME_ROOTS),
    )
    return repo


def test_a_runtime_root_contributes_only_the_tracked_files(runtime_repo: Path):
    """The artifact a tool wrote is not published, so no gate reads it."""
    scanned = set(source_scan.iter_first_party_sources(roots=(Path("tmp"),)))
    assert scanned == {runtime_repo / "tmp" / "README.md"}


def test_a_source_root_still_reads_the_working_tree(
    runtime_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """An uncommitted source file is exactly what a privacy gate must catch."""
    source_root = runtime_repo / "src"
    source_root.mkdir()
    (source_root / "uncommitted.py").write_text("x = 1\n", encoding="utf-8")
    scanned = set(source_scan.iter_first_party_sources(roots=(Path("src"),)))
    assert scanned == {source_root / "uncommitted.py"}


def test_a_runtime_root_without_a_git_answer_still_reads_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No tracked set available: the walk keeps its teeth rather than going blind."""
    repo = tmp_path / "not-a-checkout"
    (repo / "tmp").mkdir(parents=True)
    (repo / "tmp" / "artifact.md").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(source_scan, "REPO_ROOT", repo)
    monkeypatch.setattr(
        source_scan,
        "_RUNTIME_ROOT_PATHS",
        frozenset(source_scan.resolve_root(root) for root in source_scan.RUNTIME_ROOTS),
    )
    scanned = set(source_scan.iter_first_party_sources(roots=(Path("tmp"),)))
    assert scanned == {repo / "tmp" / "artifact.md"}


def test_a_runtime_root_is_recognised_by_name_and_by_path(runtime_repo: Path):
    """The walker is handed both spellings; both have to be told apart."""
    assert source_scan.is_runtime_root(Path("tmp"))
    assert source_scan.is_runtime_root(source_scan.resolve_root(Path("tmp")))
    assert not source_scan.is_runtime_root(Path("src"))


def test_the_real_runtime_roots_contribute_only_tracked_files():
    """This repository's own runtime roots: nothing untracked is read."""
    for root in source_scan.RUNTIME_ROOTS:
        tracked = {
            path
            for path in source_scan.tracked_files_under(root)
            if source_scan.is_first_party_source(source_scan.repo_relative(path))
        }
        scanned = set(source_scan.iter_first_party_sources(roots=(root,)))
        assert scanned == tracked, (
            f"{root} contributes files git does not track: "
            f"{sorted(str(p) for p in scanned - tracked)}"
        )
