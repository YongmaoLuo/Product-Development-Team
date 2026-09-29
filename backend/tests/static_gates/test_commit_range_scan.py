"""The commit-range scan must catch what the tree scan cannot.

Why this gate exists
--------------------
Every privacy gate in this directory reads the current tree. That answers
"is the repository clean now", not "did we publish something we should not
have". A commit that adds a private identifier and a later commit that
removes it leaves a clean tree and a dirty history -- and the dirty commit
stays fetchable by anyone holding its SHA, and through
``refs/pull/<N>/head`` even after a squash merge.

So this file pins the second scan, and pins two properties of it that are
easy to lose:

1. **It catches the shape it was written for.** The scenario below is the
   one that actually happened: identifier added, identifier removed, tree
   clean. ``test_the_tree_scan_misses_what_the_range_scan_catches`` asserts
   *both* halves -- if the tree scan ever starts catching it, the
   comparison stops being evidence and the test says so.

2. **It scans the same files the tree scan does.** ``is_first_party_source``
   is now shared by an on-disk walker (which sees paths relative to a scan
   root) and a commit-tree walker (which sees paths relative to the
   repository root). Getting that wrong is not theoretical: the first
   version of the commit scanner scanned ``SECURITY_AUDIT.md`` -- a file no
   other gate has ever checked -- and reported twelve violations there.
   The mirror-image mistake silently shrank the *tree* scan to three files
   while ``test_scan_is_non_empty`` stayed green, because three files are
   not zero. A count floor cannot catch that; set equality can.

Rule text is assembled from fragments
-------------------------------------
``test_no_local_home_path_in_first_party.py`` explains the convention: a
gate's own source is scanned by its sibling gates, so a fixture containing
the forbidden literal would either fail the suite or force an exemption
that blinds the gate to its own authors. Every offending string below is
built at runtime from pieces.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import commit_range_scan  # noqa: E402
import source_scan  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: A home path whose segment is not a documented placeholder. Split so this
#: file does not contain the literal it tests for.
_LEAKY_PATH = "/Use" + "rs/kai/private/probe.py"
_CLEAN_PATH = "/Use" + "rs/me/probe.py"


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(repo: Path) -> None:
    """A scratch repository with committing configured locally.

    ``-c`` flags rather than global config: the suite must not depend on,
    or mutate, whatever identity the machine happens to have. ``gpgsign``
    is forced off for the same reason -- a signing key that needs a
    passphrase would hang the suite.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "--quiet")
    _run_git(repo, "config", "user.email", "gate@example.invalid")
    _run_git(repo, "config", "user.name", "gate")
    _run_git(repo, "config", "commit.gpgsign", "false")


def _commit_file(repo: Path, rel: str, text: str, message: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _run_git(repo, "add", "--", rel)
    _run_git(repo, "commit", "--quiet", "-m", message)
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Sensitivity -- the range scan catches an identifier the tree scan does not
# ---------------------------------------------------------------------------


def test_the_tree_scan_misses_what_the_range_scan_catches(tmp_path: Path) -> None:
    """The incident, reproduced: add the identifier, then remove it.

    Commit 1 carries a real-looking home path, commit 2 deletes it. The
    working tree at the end is clean, so a tree scan finds nothing -- and
    that is exactly why the identifier shipped. The range scan must find
    it, and must name commit 1 rather than commit 2.
    """
    repo = tmp_path / "scratch"
    _init_repo(repo)

    # A base commit first, so the range below has a real ``A..B`` shape
    # rather than starting at the root commit (where ``A~1`` does not
    # resolve).
    base = _commit_file(repo, "backend/base.py", "X = 0\n", "base")
    leaky = _commit_file(
        repo,
        "backend/probe.py",
        f'CWD = "{_LEAKY_PATH}"\n',
        "add probe",
    )
    clean = _commit_file(
        repo,
        "backend/probe.py",
        f'CWD = "{_CLEAN_PATH}"\n',
        "scrub probe",
    )

    # Half one: the current tree is clean, so the tree-shaped rules see
    # nothing. This is the half that makes the defect invisible today.
    final_text = (repo / "backend/probe.py").read_text(encoding="utf-8")
    assert commit_range_scan.find_home_paths(  # type: ignore[attr-defined]
        Path("backend/probe.py"), final_text
    ) == [], (
        "the scenario is no longer the one this gate was written for: the "
        "final tree is supposed to be clean, which is what makes the "
        "history scan necessary"
    )

    # Half two: the range scan sees commit 1 anyway.
    shas, findings = commit_range_scan.scan_ranges([f"{base}..{clean}"], repo)
    assert shas == [leaky, clean], (
        "the range must resolve to both commits, oldest first -- if it "
        f"collapsed to the tip the scan would prove nothing; got {shas}"
    )
    home_path_findings = [f for f in findings if f.label == "home path"]
    assert home_path_findings, (
        "the range scan found no home-path violation in a range whose "
        "first commit introduces one; the scan is not reading history"
    )
    assert {f.commit for f in home_path_findings} == {leaky}, (
        "the violation must be attributed to the commit that introduced it "
        f"({leaky[:9]}), not to the one that removed it ({clean[:9]})"
    )


def test_a_clean_range_reports_clean(tmp_path: Path) -> None:
    """The counterweight: the gate must not fire on legitimate content."""
    repo = tmp_path / "scratch"
    _init_repo(repo)
    base = _commit_file(repo, "backend/base.py", "X = 0\n", "base")
    _commit_file(repo, "backend/probe.py", f'CWD = "{_CLEAN_PATH}"\n', "clean")
    tip = _commit_file(repo, "backend/other.py", "X = 1\n", "more clean")
    shas, findings = commit_range_scan.scan_ranges([f"{base}..{tip}"], repo)
    assert len(shas) == 2
    assert findings == [], f"false positives on clean content: {findings}"


# ---------------------------------------------------------------------------
# Scope parity -- the two scans must see the same files
# ---------------------------------------------------------------------------


def test_commit_scan_and_tree_scan_agree_on_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For HEAD, the commit scan sees exactly the disk walker's HEAD files.

    ``monkeypatch.chdir`` first: ``iter_first_party_sources`` resolves its
    ``SCAN_ROOTS`` against the process working directory, and the suite runs
    from either the repository root or ``backend/`` depending on the caller.
    The commit scan is not cwd-sensitive at all (``git ls-tree`` hands back
    repository-root-relative paths), so without the chdir this test would
    compare a cwd-independent walk against a cwd-dependent one and fail for
    a reason that has nothing to do with scope agreement.

    That cwd-sensitivity is a real, separate defect, not something this
    chdir papers over as *fixed*: under `working-directory: backend` no
    ``backend/`` root resolves, and the walker falls back to returning only
    the files under ``backend/scripts/`` — enough for a non-empty assertion
    to pass while the tree it is supposed to scan is never read. It is
    reported rather than repaired here, because the repair changes the
    meaning of every gate that shares this walker.

    Both sides are restricted to what **HEAD's tree** contains, rather than
    to ``git ls-files``. The index is not the commit: a file that is staged
    or untracked exists on disk and in ``ls-files`` but not in HEAD, and
    comparing against the index would make this test fail for the ordinary
    reason of "you have uncommitted work" instead of for scope drift.

    Restricting to HEAD's tree also drops the disk walker's untracked
    runtime noise -- ``.pytest_cache/`` ships a ``README.md`` that no commit
    contains -- while keeping the invariant sharp: any first-party file in
    HEAD must be scanned here, and the commit scan must not reach anything
    the tree gates never check.
    """
    monkeypatch.chdir(_REPO_ROOT)

    head_names = set(
        subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )

    # ``iter_first_party_sources`` yields paths relative to the scan roots
    # (its ``SCAN_ROOTS`` are relative), so with the cwd anchored at the
    # repository root these are repository-root-relative.
    on_disk = {p.as_posix() for p in source_scan.iter_first_party_sources()}
    in_head = {rel for rel, _ in commit_range_scan.iter_commit_sources(
        _REPO_ROOT, "HEAD"
    )}

    expected = on_disk & head_names
    assert expected, "no HEAD-tracked first-party files found -- the walks are broken"
    assert in_head == expected, (
        "the commit scan and the tree scan disagree about which files are "
        "in scope. Missing from the commit scan (the tree gates check these "
        f"and history would not): {sorted(expected - in_head)[:10]}. Past "
        f"the end of the tree gates' scope: {sorted(in_head - expected)[:10]}."
    )


# ---------------------------------------------------------------------------
# No vacuous pass
# ---------------------------------------------------------------------------


def test_an_empty_range_is_an_error_not_a_pass() -> None:
    """A scan that inspected nothing must not report a clean result.

    ``HEAD..HEAD`` is the realistic shape of this: a push where the remote
    is already up to date, or a base that resolved to the tip. Returning
    "0 violations" there would be indistinguishable from a real pass.
    """
    with pytest.raises(commit_range_scan.RangeScanError):
        commit_range_scan.scan_ranges(["HEAD..HEAD"], _REPO_ROOT)


def test_an_unresolvable_range_is_an_error() -> None:
    """A bogus revision must raise, not silently scan nothing."""
    with pytest.raises(commit_range_scan.RangeScanError):
        commit_range_scan.scan_ranges(
            ["refs/heads/definitely-not-a-branch..HEAD"], _REPO_ROOT
        )


def test_a_missing_blob_is_reported_rather_than_skipped() -> None:
    """``cat-file --batch`` echoes "<spec> missing"; that becomes ``None``.

    ``iter_commit_sources`` turns ``None`` into an error, because a file
    ``ls-tree`` just listed cannot legitimately be missing -- and skipping
    it would silently scan less than the caller believes.
    """
    blobs = commit_range_scan._read_blob_batch(
        _REPO_ROOT, ["HEAD:backend/__init__.py", "HEAD:no/such/file.py"]
    )
    assert blobs[0] is not None and b"__version__" in blobs[0]
    assert blobs[1] is None


# ---------------------------------------------------------------------------
# The rule registry
# ---------------------------------------------------------------------------


def test_every_rule_has_the_shared_signature() -> None:
    """Rules are data; a rule with another signature must fail here.

    ``RANGE_RULES`` works only because all three rules take ``(path, text)``
    and return ``(path, lineno, snippet)`` triples. Adding a fourth rule of
    a different shape would break the loop in ``scan_commit`` at runtime,
    on whichever commit happened to hit it.
    """
    path = Path("backend/probe.py")
    text = f'CWD = "{_LEAKY_PATH}"\n'
    assert commit_range_scan.RANGE_RULES, "the rule registry is empty"
    for label, rule in commit_range_scan.RANGE_RULES:
        findings = rule(path, text)
        assert isinstance(findings, list), f"{label} did not return a list"
        for entry in findings:
            assert len(entry) == 3, f"{label} returned a {len(entry)}-tuple"
            _, lineno, snippet = entry
            assert isinstance(lineno, int)
            assert isinstance(snippet, str)


def test_the_registry_covers_the_three_privacy_rules() -> None:
    """A rule silently dropped from the registry is a rule not enforced."""
    labels = {label for label, _ in commit_range_scan.RANGE_RULES}
    assert labels == {
        "home path",
        "operator attribution",
        "local measurement",
    }, f"the registry no longer covers every privacy rule: {sorted(labels)}"
