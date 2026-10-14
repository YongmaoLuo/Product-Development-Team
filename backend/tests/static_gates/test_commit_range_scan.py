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

import shutil
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


def test_the_tree_scan_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walker must find the same files whatever the cwd is.

    CI runs the suite with ``working-directory: backend``. While
    ``SCAN_ROOTS`` were resolved against the process working directory, no
    ``backend/`` root existed there — the walker fell back to whatever
    relative root happened to exist (``backend/scripts/``) and returned a
    handful of unrelated files. That is enough for every "the scan is not
    empty" assertion in the sibling gates to stay green, so the failure
    mode was *silence*: the tree those gates exist to scan was never read,
    and nothing said so.

    A count floor cannot catch that (a handful is not zero), which is why
    this pins the answer itself: the same set from three different
    working directories, one of them outside the repository entirely.
    """
    root = {
        source_scan.repo_relative(p).as_posix()
        for p in source_scan.iter_first_party_sources()
    }
    assert root, "no first-party sources found from the repository root"

    for cwd in (source_scan.REPO_ROOT / "backend", _REPO_ROOT, Path("/")):
        monkeypatch.chdir(cwd)
        elsewhere = {
            source_scan.repo_relative(p).as_posix()
            for p in source_scan.iter_first_party_sources()
        }
        assert elsewhere == root, (
            f"the walker returned {len(elsewhere)} files with cwd={cwd} and "
            f"{len(root)} from the repository root. The scan surface must not "
            f"depend on where pytest was launched — CI launches it from "
            f"backend/, where a relative root like 'backend' does not resolve."
        )


def test_commit_scan_and_tree_scan_agree_on_scope() -> None:
    """For HEAD, the commit scan sees exactly the disk walker's HEAD files.

    Neither side depends on the working directory: ``git ls-tree`` hands
    back repository-root-relative paths, and ``iter_first_party_sources``
    resolves its ``SCAN_ROOTS`` against ``source_scan.REPO_ROOT`` and
    yields absolute paths. ``repo_relative`` puts both in the same form.

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
    head_names = set(
        subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )

    on_disk = {
        source_scan.repo_relative(p).as_posix()
        for p in source_scan.iter_first_party_sources()
    }
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


def test_the_registry_covers_every_privacy_rule() -> None:
    """A rule silently dropped from the registry is a rule not enforced.

    The set was five from 2026-10-14, when the plan-id and private-name
    rules were added after the 2026-09-29 publication, and went back to
    four the same day when the private-name rule moved out of the
    repository. The assertion is still an equality, so a fifth rule still
    has to be added here deliberately rather than appearing in one scan and
    not the other.
    """
    labels = {label for label, _ in commit_range_scan.RANGE_RULES}
    assert labels == {
        "home path",
        "operator attribution",
        "local measurement",
        "plan id in source",
    }, f"the registry no longer covers every privacy rule: {sorted(labels)}"


def test_the_private_name_rule_is_not_in_the_registry() -> None:
    """It moved out of the repository, and a needle cannot be imported.

    The rule used to be the fifth entry here, calling ``find_private_names``
    from ``test_no_private_names_in_source.py``. Its needles were literals
    in a *tracked* file, so enforcing it meant committing the names it
    exists to keep out -- and it only ran wherever pytest ran, which is
    after the text is already in a commit.

    It is now ``scripts/scanners/scan_forbidden_names.py`` over an
    untracked ``.config/forbidden-names``, run by ``scan_commit_range.sh``
    on the pre-push path only. So the absence here is the point, and this
    is the assertion that stops somebody helpfully re-adding it.
    """
    labels = {label for label, _ in commit_range_scan.RANGE_RULES}
    assert "private name" not in labels, (
        "the private-name rule is back in the in-repo registry. Its needles "
        "are literals; putting them in a tracked module publishes the names "
        "the check exists to keep out. Use "
        "scripts/scanners/scan_forbidden_names.py instead."
    )
    # And the module it used to come from must not exist at all.
    assert not (
        Path(__file__).resolve().parent / "test_no_private_names_in_source.py"
    ).exists(), (
        "test_no_private_names_in_source.py is back. If it holds needles it "
        "is a committed blacklist; if it does not, it is a pytest gate that "
        "checks nothing. Both were removed deliberately."
    )


def test_the_message_registry_is_not_empty() -> None:
    """Commit messages are scanned, and by a rule of their own.

    A message has no ``tests/`` under it, so the plan-id rule applies there
    with no test exemption. It cannot live in ``RANGE_RULES`` -- that table
    is walked over file contents, where the same rule *does* exempt tests.
    """
    assert commit_range_scan.MESSAGE_RULES, "commit messages are not scanned at all"
    labels = {label for label, _ in commit_range_scan.MESSAGE_RULES}
    assert "plan id in commit message" in labels, sorted(labels)


# ---------------------------------------------------------------------------
# A blob in a commit that is not UTF-8
# ---------------------------------------------------------------------------
#
# This is the one of the three surfaces where the defect had a *live* needle.
# ``find_local_measurements`` and ``find_attribution`` both hold non-ASCII
# characters in their patterns, so against a blob that is not valid UTF-8
# they could never match -- and the run still printed ``0 violations``.
#
# The harness is a temporary tree rather than a throwaway repo passed with
# ``--repo``, because the brief for this is the production entry point.
# ``scan_commit_range.sh`` resolves ``PROJECT_ROOT`` from its own location
# and hard-wires ``--repo "$PROJECT_ROOT"``; there is no environment hook
# for pointing it elsewhere. So the tree is rebuilt under ``tmp_path`` from
# the *working tree* -- not from a clone of HEAD, which would test whatever
# happens to be committed rather than what is in front of the reviewer, and
# would leave the test passing against code that has been reverted.


def _range_harness(tmp_path: Path) -> Path:
    """A checkout-shaped tree holding the scripts and gates under test.

    Copies ``scripts/scan_commit_range.sh``, the whole ``static_gates``
    directory (the scanner imports its rule modules and ``source_scan``
    from beside itself) and ``.gitleaks.toml`` (the credential step reads
    it, and a missing one makes gitleaks exit non-zero and fail the run for
    a reason that has nothing to do with what is under test).
    """
    tree = tmp_path / "checkout"
    (tree / "scripts").mkdir(parents=True)
    (tree / "backend" / "tests").mkdir(parents=True)
    shutil.copy(
        _REPO_ROOT / "scripts" / "scan_commit_range.sh", tree / "scripts"
    )
    shutil.copytree(
        _REPO_ROOT / "backend" / "tests" / "static_gates",
        tree / "backend" / "tests" / "static_gates",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(_REPO_ROOT / ".gitleaks.toml", tree / ".gitleaks.toml")

    _run_git(tree, "init", "--quiet", "-b", "main")
    for key, value in (
        ("user.email", "gate@example.invalid"),
        ("user.name", "gate"),
        ("commit.gpgsign", "false"),
    ):
        _run_git(tree, "config", key, value)
    return tree


def _commit_bytes(repo: Path, rel: str, data: bytes, message: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    _run_git(repo, "add", "--", rel)
    _run_git(repo, "commit", "--quiet", "-m", message)


def _run_range_scan(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "scripts" / "scan_commit_range.sh"),
         "--range", "HEAD~1..HEAD"],
        capture_output=True,
        text=True,
        cwd=str(tree),
        check=False,
    )


#: A source line carrying the Chinese measurement shape and a date. Built
#: from escapes, because ``test_source_comments_carry_no_local_measurements``
#: walks every first-party source including this directory and would
#: otherwise find the shape spelled out here.
_CJK_MEASUREMENT_LINE = "# " + "\u5b9e\u6d4b\uff082026-01-02\uff09\n"


def test_a_non_utf8_blob_in_a_commit_is_refused_not_reported_clean(
    tmp_path: Path,
) -> None:
    """The bypass, end to end through ``scan_commit_range.sh --range``.

    Before the fix this run printed ``OK  commit-range privacy scan: 0
    violations`` and exited 0 over a commit whose measurement rule could
    not see it. Now it exits 2 and names the blob.
    """
    tree = _range_harness(tmp_path)
    _commit_bytes(tree, "backend/base.py", b"X = 0\n", "base")
    _commit_bytes(
        tree,
        "backend/probe.py",
        _CJK_MEASUREMENT_LINE.encode("gb2312"),
        "add probe",
    )

    got = _run_range_scan(tree)
    combined = got.stdout + got.stderr
    assert got.returncode == 2, (
        "a commit holding a blob this scan cannot decode must report 'did "
        f"not run' (exit 2), not a clean scan (exit 0):\n{combined}"
    )
    assert "probe.py" in combined, f"the unreadable path must be named:\n{combined}"
    assert "UTF-8" in combined, (
        f"the refusal must say what is wrong with the blob:\n{combined}"
    )
    assert "0 violations" not in got.stdout, (
        f"a clean result was printed over an unread blob:\n{got.stdout}"
    )


def test_the_same_line_as_utf8_is_still_reported(tmp_path: Path) -> None:
    """The control, and the reason the refusal above is not a blanket ban.

    Identical bytes-count-of-content, one encoding different. The rule must
    still fire, so the refusal is refusing to *guess* rather than refusing
    to read.
    """
    tree = _range_harness(tmp_path)
    _commit_bytes(tree, "backend/base.py", b"X = 0\n", "base")
    _commit_bytes(
        tree,
        "backend/probe.py",
        _CJK_MEASUREMENT_LINE.encode("utf-8"),
        "add probe",
    )

    got = _run_range_scan(tree)
    combined = got.stdout + got.stderr
    assert got.returncode == 1, (
        f"the measurement rule stopped firing on a UTF-8 blob "
        f"(exit {got.returncode}):\n{combined}"
    )
    assert "local measurement" in combined, combined


# ---------------------------------------------------------------------------
# Startup -- the scanners must come up on a bare interpreter
# ---------------------------------------------------------------------------
#
# The two tests above are the first in this repository to run the scanner
# through its real entry point from an environment that has no venv, and
# they failed on a machine that happened to have pytest installed and
# passed on a runner that did not. The harness copies the tree without a
# ``backend/.venv``, so ``scan_commit_range.sh`` takes its documented
# fallback to the system ``python3`` -- and ``commit_range_scan.py`` was
# importing ``find_home_paths`` from a gate whose module scope imported
# pytest. The scanner therefore needed a test dependency to start.
#
# That is not a detail of the harness. It is the state of a fresh clone:
# ``scan_commit_range.sh`` is the pre-push hook, it prefers the venv and
# falls back to whatever ``python3`` is on PATH, and on a GitHub runner
# that interpreter has no pytest. The scanner died on
# ``ModuleNotFoundError`` and exited 1, which is the code that means
# "violations found" -- so a hook that could not run reported findings.

#: Every production entry point that imports its rules out of
#: ``static_gates/``. They are listed rather than discovered so that adding
#: a fifth scanner is a visible edit here, and so the assertion below names
#: a file a reader can go and look at.
_SCANNER_ENTRY_POINTS = (
    "commit_range_scan",       # scripts/scan_commit_range.sh
    "staged_content_scan",     # scripts/scan_staged_privacy.sh
    "pr_metadata_scan",        # the PR-metadata CI step
)

#: Runs the imports in a *fresh* interpreter with ``pytest`` made
#: unimportable. The interpreter is this suite's own, which does have
#: pytest: the point is that nothing in the graph below needs it, so the
#: block is what a bare interpreter amounts to, on every platform.
_BLOCKS_PYTEST = """
import importlib, importlib.util, sys
from pathlib import Path


class _NoPytest:
    \"\"\"A meta-path finder that refuses to find pytest, like a runner that
    never installed it. Raising rather than returning None means a module
    that reaches for pytest gets the ModuleNotFoundError it would have got
    there, rather than a confusing success from a later finder.\"\"\"

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pytest" or fullname.startswith("pytest."):
            raise ModuleNotFoundError("No module named %r" % fullname, name=fullname)
        return None


sys.meta_path.insert(0, _NoPytest())

gates, script = sys.argv[1], sys.argv[2]
sys.path.insert(0, gates)

for name in sys.argv[3:]:
    importlib.import_module(name)

spec = importlib.util.spec_from_file_location("check_commit_msg", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

assert "pytest" not in sys.modules, sorted(
    name for name in sys.modules if name.split(".")[0] == "pytest"
)
print("OK", len(sys.argv) - 3 + 1, "entry points")
"""


def test_the_scanners_start_without_pytest(tmp_path: Path) -> None:
    """The four hook entry points import on an interpreter without pytest.

    They live in two places -- three under ``static_gates/`` and
    ``scripts/check_commit_msg.py`` -- and they get their rules by
    importing pytest gate *modules*, which is the only way a production
    script ends up needing a test package. Three of those modules already
    say in their docstrings that they must not import pytest, and one of
    them still did, silently: no test noticed, because every suite that
    exercises these scanners runs under a venv that has pytest.

    This is hermetic rather than environmental on purpose. Skipping a
    test when pytest is importable, or spawning a scanner and hoping the
    PATH ``python3`` is bare, reproduces the original bug's *shape* -- it
    passes on the machine that has the package. Blocking the import pins
    the invariant on every machine.
    """
    gates = _REPO_ROOT / "backend" / "tests" / "static_gates"
    script = _REPO_ROOT / "scripts" / "check_commit_msg.py"
    got = subprocess.run(
        [sys.executable, "-c", _BLOCKS_PYTEST, str(gates), str(script),
         *_SCANNER_ENTRY_POINTS],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert got.returncode == 0, (
        "a scanner imported pytest at module scope, so the pre-push hook "
        "and the PR-metadata CI step cannot start without a venv:\n"
        f"{got.stdout}{got.stderr}"
    )
    assert got.stdout.startswith("OK"), got.stdout
