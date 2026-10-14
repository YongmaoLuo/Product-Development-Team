"""The forbidden-name check must block at the two hooks, or nowhere.

Why an integration test
-----------------------
The needles live in an operator-owned file that is *not* in this
repository, so there is no rule table and no pure function for a unit test
to exercise. The only thing that can be tested honestly is the thing a
developer actually runs: ``scripts/pre_commit_gate.sh`` for the index and
``scripts/scan_commit_range.sh --stdin-pre-push`` for the commits.

Testing the entry points rather than the scanner is the point. A scanner
whose tests all pass while nothing calls it is the exact failure this
repository already shipped twice -- once with ``PDT_FORBIDDEN_REPO_NAMES``,
which was never set on any machine and reported a pass forever, and once
with a blacklist committed into a pytest gate, where the check ran only
after the text was already in a commit.

The list is synthetic
---------------------
Every test writes its own ``.config/forbidden-names`` under ``tmp_path``
and passes it with ``--list``. Nothing here names anything real, so this
file can be written out whole -- no fragment assembly, because the rule it
tests is no longer reading this file's own source. That is deliberate:
a name hidden behind ``"a" + "b"`` is one concatenation away from being
published, and calling that a protection is how the old list survived
review in the first place.

Index and repository isolation
------------------------------
Nothing here reads or writes the developer's real staging area. Commits
for the range mode are built in a throwaway repository under ``tmp_path``;
the index mode uses ``GIT_INDEX_FILE`` pointed at a temporary index built
with ``git read-tree``.

The pre-push test is the one exception, and it is two exceptions that
were both stated here rather than left implicit. It writes
``.config/forbidden-names``, because the hook reads the list from that
path and passes no ``--list``, so it borrows the operator's own file and
puts the bytes back afterwards. And it mints one commit object, through
:func:`_unpublished_tip`, because the shape a pre-push hook needs is "a
commit about to be published over something already on the remote" and a
checkout holding one commit has no such thing to borrow. What that
deliberately does *not* do is move a ref: ``git commit-tree`` writes an
object and stops, so nothing can be pushed by accident, nothing appears
in ``git log``, and the operator's commit-graph is unchanged. That is
the part of "no commit is created in the real repository" worth keeping,
and it is the part the object does not give up.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PRE_COMMIT_GATE = REPO_ROOT / "scripts" / "pre_commit_gate.sh"
RANGE_SCRIPT = REPO_ROOT / "scripts" / "scan_commit_range.sh"

#: A needle that names nothing real. It has to be distinctive enough that
#: it cannot occur by accident in the throwaway content below, and
#: ``zz-`` reads as obviously synthetic at a glance.
SYNTHETIC_NEEDLE = "zz-synthetic-forbidden-project"


# ---------------------------------------------------------------------------
# A throwaway repository, for the pre-push path
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> Path:
    """Create a repository with one commit, and return its root."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-q", "-b", "main"],
        check=True,
        capture_output=True,
    )
    for key, value in (
        ("user.email", "test@example.invalid"),
        ("user.name", "Test"),
    ):
        subprocess.run(
            ["git", "-C", str(path), "config", key, value],
            check=True,
            capture_output=True,
        )
    (path / "README.md").write_text("nothing here\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return path


def _commit_file(repo: Path, rel: str, content: str) -> str:
    """Commit *content* at *rel* in the throwaway repo; return the SHA."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", rel], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", f"add {rel}"],
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def _write_list(path: Path, *lines: str) -> str:
    """Write a blacklist file and return its path as a string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        check=False,
    )


def _load_module(path: Path):
    """Import the scanner module at *path*, which is not a package.

    Only for the two assertions above that are about a *constant* rather
    than a behaviour: those cannot be observed from the outside without
    contriving a file type that happens to disagree with the shared set.
    Every behavioural case in this file goes through the wrapper script,
    which is the thing a developer actually runs.
    """
    spec = importlib.util.spec_from_file_location(f"_probe_{path.stem}", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: both modules define ``@dataclass`` classes
    # whose annotations are strings, and ``dataclasses`` resolves those
    # through ``sys.modules[cls.__module__]`` -- which is not populated yet
    # if the exec happens first.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


# ---------------------------------------------------------------------------
# pre-commit: the staged index
# ---------------------------------------------------------------------------


def _staged_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``GIT_INDEX_FILE`` is a throwaway index."""
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(tmp_path / "index")
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "read-tree", "HEAD"],
        check=True,
        capture_output=True,
        env=env,
    )
    return env


def _stage(env: dict[str, str], path: str, content: str) -> None:
    """Put *content* at *path* in the throwaway index, touching no file."""
    _stage_blob(env, path, content.encode("utf-8"))


def _stage_blob(env: dict[str, str], path: str, data: bytes) -> None:
    """Stage raw *data*, so a NUL byte survives to the index.

    ``_stage`` goes through UTF-8, which cannot express the leading
    ``\\x00`` these tests are about; the blob is built here instead so the
    byte under test is the byte the scanner sees.
    """
    sha = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "hash-object", "-w", "--stdin"],
        input=data,
        capture_output=True,
        check=True,
        env=env,
    ).stdout.decode().strip()
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "update-index", "--add", "--cacheinfo",
         f"100644,{sha},{path}"],
        check=True,
        capture_output=True,
        env=env,
    )


def test_pre_commit_blocks_a_staged_forbidden_name(tmp_path) -> None:
    """A needle in staged content must stop the commit, with file:line."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'NAME = "{SYNTHETIC_NEEDLE}"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    # The dispatcher runs three gates; drive the name check's own wrapper
    # through the same path the dispatcher's third step uses, so a failure
    # here is attributable.
    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )

    assert got.returncode == 1, (
        f"a staged forbidden name was not blocked (exit {got.returncode}):\n"
        f"{got.stdout}\n{got.stderr}"
    )
    assert "backend/agent.py:1" in got.stderr, (
        f"the violation was not reported as path:lineno:\n{got.stderr}"
    )
    assert SYNTHETIC_NEEDLE in got.stderr, got.stderr


def test_pre_commit_passes_when_the_staged_content_is_clean(tmp_path) -> None:
    """The ordinary commit must not cost anything."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'GREETING = "hello"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 0, f"clean content blocked:\n{got.stdout}\n{got.stderr}"


def test_pre_commit_matches_case_insensitively(tmp_path) -> None:
    """A name shouted in capitals is the same leak."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'NAME = "{SYNTHETIC_NEEDLE.upper()}"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 1, f"case variant not blocked:\n{got.stdout}\n{got.stderr}"


def test_pre_commit_matches_a_name_glued_to_a_prefix(tmp_path) -> None:
    """The reason matching is substring and not a boundary regex.

    The rule this replaced used ``(?<![A-Za-z0-9._-])NAME(?![A-Za-z0-9._-])``.
    A hyphen counts as a word character in that class, so the pattern
    cannot match a name glued to a prefix -- and a build artefact that
    glues one is a perfectly ordinary way a private name reaches a file.
    This test is the regression pin for the replacement's actual rationale.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'LIB = "lib{SYNTHETIC_NEEDLE}_rs.dylib"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 1, (
        f"a name glued to a prefix was not matched:\n{got.stdout}\n{got.stderr}"
    )


def test_pre_commit_applies_the_re_prefixed_shape(tmp_path) -> None:
    """A ``re:`` entry matches a shape, not a word.

    The shape is synthetic rather than a realistic one. That is not
    cosmetic: this file is scanned by the operator's real local list every
    time they commit, the list has no test-directory exemption, and a
    realistic quote-identifier shape here would be a needle this file
    carries -- which is the failure the check exists to catch, aimed at
    the test that proves the check works.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'QUOTE = "zzshape=000001"\n')
    list_path = _write_list(tmp_path / "forbidden-names", r"re:zzshape=[0-9]{6}")

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 1, f"a re: shape was not matched:\n{got.stdout}\n{got.stderr}"
    assert "zzshape=000001" in got.stderr, got.stderr


def test_comments_and_blank_lines_are_ignored(tmp_path) -> None:
    """The documented format has to mean something."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'GREETING = "hello"\n')
    list_path = _write_list(
        tmp_path / "forbidden-names",
        "# a comment",
        "",
        "   ",
        SYNTHETIC_NEEDLE,
    )

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 0, f"a comment or blank line was read as a needle:\n{got.stderr}"


# ---------------------------------------------------------------------------
# "the check did not run" must not read as "clean"
# ---------------------------------------------------------------------------


def test_a_missing_list_exits_2_not_0(tmp_path) -> None:
    """The load-bearing case.

    An absent list and a clean tree have to be distinguishable, or an
    operator who never created the file gets a green hook forever and
    believes the names are being checked. This is the ``
    PDT_FORBIDDEN_REPO_NAMES`` failure, reproduced deliberately so the
    replacement's exit codes pin it shut.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'GREETING = "hello"\n')
    missing = tmp_path / "does-not-exist" / "forbidden-names"

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            str(missing),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 2, (
        f"a missing list reported {got.returncode}; it must be 2, not a "
        f"pass and not a violation:\n{got.stdout}\n{got.stderr}"
    )
    # The message has to say how to fix it, without suggesting --no-verify.
    assert "did not run" in got.stderr.lower(), got.stderr
    assert "--no-verify" not in got.stderr, (
        "the failure message suggests --no-verify, which is the one thing "
        "that must not be offered here"
    )
    assert "forbidden-names.example" in got.stderr, (
        f"the failure message does not point at the template to copy:\n{got.stderr}"
    )


def test_an_empty_list_exits_2_not_0(tmp_path) -> None:
    """Zero usable needles is an unconfigured check, not a clean one."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'GREETING = "hello"\n')
    list_path = _write_list(
        tmp_path / "forbidden-names", "# only a comment", "", "   "
    )

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 2, (
        f"a list with no usable needle reported {got.returncode}, not 2:\n"
        f"{got.stdout}\n{got.stderr}"
    )


def test_an_uncompilable_pattern_exits_2(tmp_path) -> None:
    """A broken pattern is a check that is not running as configured.

    The pattern is an unterminated character set, which ``re`` rejects.
    (An unterminated *quantifier* would not do: ``[0-9]{6`` compiles,
    because Python reads the brace as a literal, so a "broken" pattern of
    that shape would pass silently -- which is the whole reason this test
    exists.)
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'GREETING = "hello"\n')
    list_path = _write_list(tmp_path / "forbidden-names", "re:symbol=[0-9")

    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    assert got.returncode == 2, (
        f"an uncompilable pattern reported {got.returncode}, not 2:\n"
        f"{got.stdout}\n{got.stderr}"
    )


# ---------------------------------------------------------------------------
# A NUL byte must not switch the check off
# ---------------------------------------------------------------------------


def _scan_index(env: dict[str, str], list_path: str) -> subprocess.CompletedProcess[str]:
    """Run the pre-commit name check exactly as the dispatcher runs it."""
    return subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--index",
            "--list",
            list_path,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )


def _said(got: subprocess.CompletedProcess[str]) -> str:
    """Everything the check said, on either stream.

    ``scan_forbidden_names.sh`` captures the scanner's output with
    ``2>&1`` and replays it on **stderr** for any non-zero exit, so on
    every path these tests care about ``stdout`` is empty. Asserting
    against ``stdout`` alone would be asserting against a string that is
    always ``""`` -- which passes no matter what the scanner says.
    """
    return got.stdout + got.stderr


def test_a_nul_prefixed_source_file_is_still_scanned(tmp_path) -> None:
    """The bypass itself: a ``\\x00`` in a ``.py`` must not skip it.

    One leading NUL byte used to make the scanner skip the blob, print a
    reassuring note that changed no exit code, and report ``OK  0
    violations`` -- with a real name sitting in the skipped file. This is
    the same shape as the bypass closed in ``staged_content_scan.py``,
    and this scanner is the *only* place these names are checked: it needs
    an operator-owned list, so it cannot run in CI, and the hooks are the
    whole surface.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/nul_probe.py", f"\x00{SYNTHETIC_NEEDLE}\n".encode())
    _stage(env, "backend/innocent.py", 'GREETING = "hello"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_index(env, list_path)

    assert got.returncode == 1, (
        f"a NUL-prefixed .py carrying a forbidden name was not blocked "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )
    assert "backend/nul_probe.py:1" in got.stderr, (
        f"the skipped file was not reported as a violation:\n{got.stderr}"
    )
    assert SYNTHETIC_NEEDLE in _said(got), _said(got)
    assert "OK  forbidden-name scan" not in _said(got), (
        f"a batch containing a forbidden name reported OK:\n{_said(got)}"
    )


def test_a_nul_prefixed_source_file_alone_is_not_a_clean_scan(tmp_path) -> None:
    """The degenerate batch -- only the NUL file staged -- must still refuse.

    This is the case a "skip binaries, read the rest" fix would leave
    behind if it only fixed the neighbouring-file shape: with nothing else
    in the index there is no other file to scan, so an implementation that
    reported the skip as a clean run would pass here. Any non-zero exit is
    acceptable; 0 is not.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/nul_probe.py", f"\x00{SYNTHETIC_NEEDLE}\n".encode())
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_index(env, list_path)

    assert got.returncode != 0, (
        f"a batch whose only file carries a forbidden name reported "
        f"{got.returncode}:\n{got.stdout}\n{got.stderr}"
    )
    assert SYNTHETIC_NEEDLE in got.stderr, got.stderr


def test_a_nul_source_file_with_no_needle_is_read_not_skipped(tmp_path) -> None:
    """Reading a NUL-bearing ``.py`` must not cost anything by itself.

    The counterpart of the case above: if the rule were "any NUL is exit
    2", the ordinary commit would be blocked for no reason and the gate
    would be switched off. A NUL source file with nothing forbidden in it
    is a *clean* scan, because nothing was left unread.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/nul_probe.py", b"\x00GREETING = 'hello'\n")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_index(env, list_path)

    assert got.returncode == 0, (
        f"a NUL-bearing .py with no forbidden name blocked the commit "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )


def test_a_skipped_binary_degrades_the_batch_to_2(tmp_path) -> None:
    """The second half of the fix: a skip degrades the batch, loudly.

    Extension-based skipping means an extension nobody has met yet is not
    in the source set, so it gets skipped. If that were free, this same
    bypass would return with one more file type. So a batch that skipped
    anything exits 2 and names what it did not read -- and in particular
    does not print ``OK``.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/logo.png", b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 40)
    _stage(env, "backend/innocent.py", 'GREETING = "hello"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_index(env, list_path)

    assert got.returncode == 2, (
        f"a batch with a skipped blob reported {got.returncode}, not 2 -- "
        f"one unread blob is an unknown number of unexamined bytes:\n"
        f"{got.stdout}\n{got.stderr}"
    )
    assert "not scanned: backend/logo.png" in got.stderr, (
        f"the skipped file was not named:\n{got.stderr}"
    )
    assert "did not read the whole commit" in got.stderr, got.stderr
    assert "OK  forbidden-name scan" not in _said(got), (
        f"a degraded batch reported a clean scan:\n{_said(got)}"
    )


def test_a_real_binary_alone_still_exits_2(tmp_path) -> None:
    """Staging only a real binary keeps its exit code: 2 before, 2 after.

    The batch of one leaves nothing readable, which is the pre-existing
    "refusing to report a clean scan over an empty read" branch. Pinning
    it stops a later change from turning "I read nothing" into 0 or 1.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/logo.png", b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 40)
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_index(env, list_path)

    assert got.returncode == 2, (
        f"a batch holding only a real binary reported {got.returncode}, "
        f"not 2:\n{got.stdout}\n{got.stderr}"
    )


def test_a_skip_does_not_hide_a_violation_in_the_files_that_were_read(
    tmp_path,
) -> None:
    """Degradation must not swallow findings, and findings must come first.

    The two outputs answer different questions, and a reader who is sent
    to the wrong one is sent looking for a git problem instead of the name
    that was just found in the file next to the PNG.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/logo.png", b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 40)
    _stage(env, "backend/agent.py", f'NAME = "{SYNTHETIC_NEEDLE}"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_index(env, list_path)

    assert got.returncode == 2, (
        f"a degraded batch with a violation reported {got.returncode}:\n"
        f"{got.stdout}\n{got.stderr}"
    )
    # The findings go to stdout and the degradation notice to stderr; the
    # wrapper captures both and replays them on stderr. What has to hold is
    # that neither half is dropped, not how the two streams interleave --
    # stdout is block-buffered through a pipe, so their relative order in
    # the captured text is not the order ``main()`` printed them in.
    assert "FAIL forbidden-name scan: 1 violation(s)" in _said(got), (
        f"the violation was not reported:\n{_said(got)}"
    )
    assert "backend/agent.py:1" in _said(got), _said(got)
    assert "not scanned: backend/logo.png" in _said(got), _said(got)


def test_the_binary_probe_bytes_match_the_staged_content_scan() -> None:
    """Two copies of one constant, held together by a test.

    ``scan_forbidden_names.py`` and ``staged_content_scan.py`` both carry
    ``_BINARY_PROBE_BYTES`` rather than one importing the other: the
    constant is git's, and sharing it would drag this hook through the
    whole import closure of the plan-id gate to save an integer. That is a
    trade only while the two agree, so this pins them. A test may import
    both modules; a pre-commit hook may not.
    """
    forbidden = _load_module(
        REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"
    )
    staged = _load_module(
        REPO_ROOT / "backend" / "tests" / "static_gates" / "staged_content_scan.py"
    )

    assert forbidden._BINARY_PROBE_BYTES == staged._BINARY_PROBE_BYTES, (
        f"the two binary probes disagree: "
        f"{forbidden._BINARY_PROBE_BYTES} != {staged._BINARY_PROBE_BYTES}. "
        f"They are meant to be git's threshold, so one of them has drifted."
    )
    assert forbidden._BINARY_PROBE_BYTES == 1024, (
        f"git's binary threshold changed: "
        f"{forbidden._BINARY_PROBE_BYTES} != 1024"
    )


def test_the_scanner_reads_the_shared_source_extension_set() -> None:
    """The skip decision must not restate which files are source.

    If this scanner carried its own extension set, adding a type to
    ``source_scan.SOURCE_EXTENSIONS`` and not to the copy would re-open
    exactly the bypass these tests close, silently. So the set is the
    imported one, not a literal here.
    """
    forbidden = _load_module(
        REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"
    )

    # Identity, not equality: the point is that the scanner *imports* the
    # set rather than writing its own literal. Loading ``source_scan`` a
    # second time here would produce an equal-but-distinct frozenset and
    # prove nothing about provenance, so this compares against the module
    # object the scanner's own import left in ``sys.modules``.
    shared = sys.modules.get("source_scan")
    assert shared is not None, (
        "loading the scanner did not import source_scan; the extension set "
        "must come from backend/tests/static_gates/source_scan.py"
    )
    assert forbidden.SOURCE_EXTENSIONS is shared.SOURCE_EXTENSIONS, (
        "the scanner holds its own copy of SOURCE_EXTENSIONS instead of "
        "importing the shared one; two copies drift, and this particular "
        "one drifting re-opens the NUL bypass for the new file type"
    )
    assert Path("backend/agent.py").suffix in forbidden.SOURCE_EXTENSIONS
    assert Path("backend/logo.png").suffix not in forbidden.SOURCE_EXTENSIONS


# ---------------------------------------------------------------------------
# pre-push: the commits about to be published
# ---------------------------------------------------------------------------


#: The environment :func:`_unpublished_tip` builds its commit in. Git needs
#: an identity and refuses to commit without one, and where that identity
#: comes from is a property of the machine: a laptop has ``user.name`` in its
#: global config, a CI runner does not. Reading it from the environment is
#: what keeps the test's behaviour identical on both. The dates are pinned
#: for the same reason, and for one more: fixed bytes mean a fixed commit
#: SHA, so a second run finds the object already written and adds nothing.
_SYNTHETIC_TIP_ENV = {
    "GIT_AUTHOR_NAME": "pre-push test",
    "GIT_AUTHOR_EMAIL": "pre-push-test@example.invalid",
    "GIT_COMMITTER_NAME": "pre-push test",
    "GIT_COMMITTER_EMAIL": "pre-push-test@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}


def _unpublished_tip(published: str) -> str:
    """One commit to push over *published*, without moving a ref.

    ``git commit-tree`` writes a single object and stops. No ref, no index
    entry, no worktree file, no reflog: the object is unreachable from the
    moment it is written, and ``git gc`` is what would ever remove it. The
    commit's parent is *published* and its tree is that commit's tree, so
    ``<published>..<tip>`` is exactly one commit -- the new one -- at any
    checkout depth.

    Why the tip and not a synthetic parent. The other shape available is a
    synthetic commit standing where ``HEAD~1`` used to be, and it is
    depth-dependent in exactly the way this fixture exists to stop being:
    ``<synthetic-root>..HEAD`` is every commit reachable from ``HEAD``, which
    is one commit in a ``fetch-depth: 1`` clone and the whole history in a
    full one. Same test, different commits scanned, and the local run would
    drag gitleaks over the entire repository. A synthetic tip has no such
    dependence -- the range is one commit whether the checkout holds one
    commit or five thousand.

    Why a fixture is needed at all. The published side used to be read as
    ``HEAD~1``, and that revision does not exist in the shallow checkout
    every CI job makes: ``rev-parse`` exits 128 and the test dies there,
    while on any laptop with a full clone it is green. The failure read as
    a broken test because nothing in the test said it was borrowing
    something the environment might not have.

    ``commit.gpgsign`` is forced off for the same reason
    ``test_commit_range_scan.py`` forces it off in its throwaway
    repositories: a signing key that wants a passphrase would hang the
    suite on the machine that has one.

    The count assertion is the fixture checking itself. If a future edit
    gave this commit a second parent, or dropped one, the range would stop
    being the single new commit this test says it is -- and it would fail
    quietly, by scanning something else.
    """
    env = dict(os.environ)
    env.update(_SYNTHETIC_TIP_ENV)
    tip = subprocess.run(
        [
            "git", "-C", str(REPO_ROOT),
            "-c", "commit.gpgsign=false",
            "commit-tree", f"{published}^{{tree}}",
            "-p", published,
            "-m", "pre-push test: a commit held by no ref",
        ],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()

    counted = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-list", "--count", f"{published}..{tip}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert counted == "1", (
        f"the synthetic tip does not stand for exactly one new commit "
        f"(rev-list counted {counted} over {published}..{tip}); the range "
        f"this test scans would not be the range it claims to scan"
    )
    return tip


def test_pre_push_blocks_a_forbidden_name_in_a_commit(tmp_path) -> None:
    """End to end through the hook's own entry point, in this checkout.

    ``scan_commit_range.sh --stdin-pre-push`` is what
    ``install_git_hooks.sh`` points the pre-push hook at. This runs that
    script for real, with the ref line git would feed it on stdin, over a
    real range in this repository.

    The ref line is ``<tip> over <HEAD>``: one commit that is not on any
    remote yet, published over the commit it is already based on. That is
    the ordinary shape of a push, and it is built by :func:`_unpublished_tip`
    rather than read out of the history, so the test asks for the same
    thing on a ``--depth 1`` clone and on a full one.

    The needle is the repository's own public name rather than anything
    private, for two reasons: it is definitely present in the blobs at
    ``HEAD`` (README.md, mkdocs.yml), so the control has something real to
    fire on; and the test must not put a private name in a tracked file in
    order to test the check for private names.

    It is also chosen so it cannot collide with the operator's real list:
    the list deliberately spells that project's private plan-id family
    with a leading hyphen, because the bare spelling would match this
    repository's own public identity in README, mkdocs and the changelog.
    A needle without the leading hyphen is therefore a different string
    from anything the real list holds, and this test stays valid whatever
    the operator puts in theirs.
    """
    needle = "Product-Development-Team"
    list_path = _write_list(tmp_path / "forbidden-names", needle)

    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    tip = _unpublished_tip(head)

    # The forbidden-name step reads the list at <repo>/.config/forbidden-names,
    # and the hook passes no --list. So the temporary list has to be at that
    # path for this test, which is why it is backed up and restored.
    real_list = REPO_ROOT / ".config" / "forbidden-names"
    real_existed = real_list.exists()
    real_bytes = real_list.read_bytes() if real_existed else None
    try:
        real_list.parent.mkdir(parents=True, exist_ok=True)
        real_list.write_text(needle + "\n", encoding="utf-8")

        got = subprocess.run(
            [
                "bash", str(RANGE_SCRIPT), "--stdin-pre-push",
            ],
            input=f"refs/heads/main {tip} refs/heads/main {head}\n",
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
    finally:
        if real_existed:
            real_list.write_bytes(real_bytes)
        else:
            real_list.unlink(missing_ok=True)

    # The restore is byte-exact and checked, not assumed: this test borrows
    # the operator's real list path because the hook passes no --list, and
    # silently leaving a different list behind would weaken their hooks
    # without saying so.
    if real_existed:
        assert real_list.read_bytes() == real_bytes, (
            "the test did not restore .config/forbidden-names byte-for-byte"
        )
    else:
        assert not real_list.exists(), (
            "the test left a .config/forbidden-names behind that was not "
            "there before it ran"
        )

    assert got.returncode == 1, (
        f"the pre-push hook did not block a commit carrying a listed name "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )
    assert needle in got.stdout + got.stderr, (
        f"the needle was not named in the report:\n{got.stdout}\n{got.stderr}"
    )
    assert ":" in got.stdout, got.stdout


def test_the_range_mode_finds_a_name_in_this_repository(tmp_path) -> None:
    """The range mode really does read commit contents.

    Rather than assert on a throwaway repository through a script that is
    hard-wired to scan this checkout, this drives the scanner module
    against a throwaway repository directly. It is here because the
    pre-push wiring itself is pinned by
    ``test_pre_push_is_wired_into_the_range_script``.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", f'NAME = "{SYNTHETIC_NEEDLE}"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"),
            "--repo",
            str(repo),
            "--list",
            list_path,
            "--range",
            "HEAD~1..HEAD",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )
    assert got.returncode == 1, (
        f"the range scan missed a forbidden name in a commit "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )
    assert "backend/agent.py:1" in got.stdout, got.stdout
    assert SYNTHETIC_NEEDLE in got.stdout, got.stdout


def test_a_clean_range_exits_0(tmp_path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", 'GREETING = "hello"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"),
            "--repo",
            str(repo),
            "--list",
            list_path,
            "--range",
            "HEAD~1..HEAD",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )
    assert got.returncode == 0, f"a clean range was blocked:\n{got.stdout}\n{got.stderr}"


def test_a_missing_list_exits_2_in_range_mode(tmp_path) -> None:
    """Same contract on the pre-push side as on the pre-commit side."""
    # No throwaway repo here: the wrapper always scans this checkout, which
    # is the behaviour under test -- the list is read and refused before any
    # content is touched, so there is nothing to point it at.
    got = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
            "--range",
            "HEAD~1..HEAD",
            "--list",
            str(tmp_path / "absent" / "forbidden-names"),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert got.returncode == 2, (
        f"a missing list in range mode reported {got.returncode}, not 2:\n"
        f"{got.stdout}\n{got.stderr}"
    )


# ---------------------------------------------------------------------------
# Wiring: the checks are attached to the hooks that must run them
# ---------------------------------------------------------------------------


def test_pre_push_is_wired_into_the_range_script() -> None:
    """The pre-push hook's entry point must invoke the name check.

    And it must do so on the pre-push path *only*. A CI runner has no
    ``.config/forbidden-names``, so a ``--ci`` invocation could only ever
    exit 2 and the job would be red for a file it is not allowed to have.
    """
    text = RANGE_SCRIPT.read_text(encoding="utf-8")
    assert "scan_forbidden_names.sh" in text, (
        "scan_commit_range.sh no longer invokes the forbidden-name check, so "
        "the pre-push hook does not run it"
    )
    assert 'if [ "$USE_STDIN" -eq 1 ]; then' in text, (
        "the forbidden-name step is no longer keyed on the pre-push path; "
        "check that it does not run under --ci, where there is no local list"
    )
    assert "--range" in text, "the forbidden-name step is not given a range"


def test_the_wrapper_is_registered_for_both_hooks() -> None:
    """Both hook entries must reach the new wrapper.

    ``pre_commit_gate.sh`` for the index and ``scan_commit_range.sh`` for
    the commits. One without the other leaves half the surface unchecked,
    and both halves are needed: the index catches it before the commit, the
    range catches it after.
    """
    installer = (REPO_ROOT / "scripts" / "install_git_hooks.sh").read_text(
        encoding="utf-8"
    )
    assert "scripts/pre_commit_gate.sh" in installer, (
        "the pre-commit hook no longer points at the dispatcher"
    )
    assert "scripts/scan_commit_range.sh" in installer, (
        "the pre-push hook no longer points at the range scan"
    )
    dispatcher = PRE_COMMIT_GATE.read_text(encoding="utf-8")
    assert "scan_forbidden_names.sh" in dispatcher, (
        "the dispatcher no longer runs the forbidden-name check"
    )
