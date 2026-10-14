"""Every top-level directory the repository tracks is inside the scan roots.

Why this gate exists
--------------------
``source_scan.SCAN_ROOTS`` is what the privacy gates read. Nothing checked
that it was *complete*, and it was not: it was written when the repository
had four source directories, and ``tools/``, ``tests/``, ``.claude/``,
``.github/`` and ``docs/`` each arrived afterwards without joining it. All
five were tracked, all five held files the walker's extension filter would
have accepted, and no tree gate read a byte of any of them.

An unlisted directory fails **silently**, which is why this is a gate and
not a note in the walker's docstring. Every gate built on the walker
asserts that its scan is not empty — so the roots that *are* listed keep
producing files, every assertion keeps passing, and the directory nobody
listed is never read by anything. There is no red run to notice, and no
count that moves.

What it pins
------------
1. **Completeness.** Every top-level directory ``git ls-tree`` reports at
   ``HEAD`` appears in :data:`source_scan.SCAN_ROOTS`. A directory added
   in a commit and not added here turns this red.
2. **Reach.** Every root yields at least one file through the same filter
   the gates use. A root that is named but reads nothing satisfies the
   first rule and still checks nothing — which is what ``example/`` did,
   for as long as its ``.example`` templates sat outside
   ``SOURCE_EXTENSIONS``.

What it does not check
----------------------
That the roots are the *right* ones, or that any gate's patterns are any
good. It answers one question — is there a directory here that nothing
reads — and answers it by deriving the set from the repository rather than
restating a list a person has to remember to update.

Why ``HEAD`` and not the index
------------------------------
The same reason ``test_commit_range_scan`` compares against the commit: a
directory that exists on disk but is not committed is not something the
repository publishes, and a gate that fired on it would fail for the
ordinary reason of "a change is in progress". The commit is the moment an
unscanned directory becomes a defect, so it is the moment this checks.
"""

from __future__ import annotations

import subprocess

import source_scan

#: Repository root, taken from the walker rather than recomputed — the two
#: have to agree, and this file sits beside ``source_scan.py``.
_REPO_ROOT = source_scan.REPO_ROOT


def tracked_top_level_directories() -> set[str]:
    """Return the names of the repository's top-level directories at ``HEAD``.

    ``git ls-tree -d`` lists tree entries of type directory only, so a
    top-level *file* (``README.md``, ``mkdocs.yml``, ``.env.example``)
    does not appear. That is what this gate wants: the walker starts at
    directories, so a root has to be one.
    """
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-tree", "-d", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def unscanned(tracked: set[str], roots: set[str]) -> list[str]:
    """Return the tracked directories that are not scan roots, sorted."""
    return sorted(tracked - roots)


def test_the_repository_reports_its_directories() -> None:
    """The derivation must produce something, or both rules pass vacuously.

    A broken invocation, a changed output format, or a run outside a
    checkout would each leave the derived set empty — and an empty set
    satisfies "every member is a scan root" without checking anything.
    """
    tracked = tracked_top_level_directories()
    assert tracked, (
        "git ls-tree reported no top-level directories under HEAD — the "
        "derivation is broken, and the assertions below would pass on an "
        "empty set."
    )


def test_every_tracked_directory_is_a_scan_root() -> None:
    """No tracked top-level directory may sit outside the privacy gates."""
    tracked = tracked_top_level_directories()
    missing = unscanned(tracked, set(source_scan.SCAN_ROOT_NAMES))
    assert not missing, (
        "these top-level directories are tracked but are not listed in "
        "source_scan.SCAN_ROOTS, so no privacy gate reads them and nothing "
        "says so. Add them to SCAN_ROOTS, or remove them:\n  "
        + "\n  ".join(missing)
    )


def test_every_scan_root_yields_at_least_one_file() -> None:
    """A root that reads nothing is a root in name only.

    Checked per root rather than in aggregate: a total that barely moves
    because ``backend/`` grew while one root fell to zero is exactly the
    reading that hides this.
    """
    empty: list[str] = []
    for root in source_scan.SCAN_ROOTS:
        if not list(source_scan.iter_first_party_sources(roots=(root,))):
            empty.append(str(root))

    assert not empty, (
        "these directories are named in source_scan.SCAN_ROOTS but the "
        "walker yields no file from any of them, so listing them checks "
        "nothing. Either their contents fall outside SOURCE_EXTENSIONS — "
        "decide whether that is deliberate — or they no longer hold "
        "source:\n  " + "\n  ".join(empty)
    )


def test_a_tracked_directory_outside_the_roots_is_reported() -> None:
    """The comparison flags a directory that is not a scan root.

    Sensitivity, pinned the way the sibling gates pin theirs: a rule that
    cannot fail is a rule nothing enforces. The sample is assembled here
    rather than read from the repository, so it stays an assertion about
    the comparison rather than about the current tree.
    """
    assert unscanned({"backend", "a-dir-outside-the-roots"}, {"backend"}) == [
        "a-dir-outside-the-roots"
    ]
    assert unscanned({"backend"}, {"backend"}) == []
