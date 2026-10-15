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

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRE_COMMIT_GATE = REPO_ROOT / "scripts" / "pre_commit_gate.sh"
RANGE_SCRIPT = REPO_ROOT / "scripts" / "scan_commit_range.sh"

#: A needle that names nothing real. It has to be distinctive enough that
#: it cannot occur by accident in the throwaway content below, and
#: ``zz-`` reads as obviously synthetic at a glance.
SYNTHETIC_NEEDLE = "zz-synthetic-forbidden-project"

#: The same idea with a non-ASCII character in it, which is what makes the
#: encoding tests below possible. The list is read as UTF-8, so *this* is
#: ``zz-caf\\xc3\\xa9-synthetic``; :data:`_NON_ASCII_NEEDLE_LATIN1` is the
#: same characters written the way a Western-locale editor would have
#: written them, ``zz-caf\\xe9-synthetic``. Same name, different bytes.
SYNTHETIC_NON_ASCII_NEEDLE = "zz-café-synthetic"
_NON_ASCII_NEEDLE_LATIN1 = b"NAME = 'zz-caf\xe9-synthetic'\n"

#: One complete discovery line, matched whole. The scanner reports
#: ``path:lineno: <redacted> (N chars) [list entry #K]`` and nothing else,
#: so this is the shape a finding has to arrive in.
#:
#: Why the shape and not "the needle appeared somewhere": this scanner
#: exists to stop a private proper name being published, so it must not
#: print one either. An assertion of the form ``needle in output`` cannot
#: be written any more -- and the form that *could* be written loosely,
#: ``"<redacted>" in output``, is satisfied by the HOWTO legend the
#: wrapper prints on every failure. So the path, the line number, the
#: redaction token and the list position are pinned together, and the
#: needle's own absence is pinned alongside.
_DISCOVERY_RE = re.compile(
    r"^(?P<path>\S+):(?P<lineno>\d+): <redacted> \((?P<chars>\d+) chars\)"
    r"(?: \[list entry #(?P<entry>\d+)\])?$"
)


def _discovery_lines(output: str) -> list[re.Match[str]]:
    """Every line of *output* that is a complete finding report."""
    return [
        m
        for m in (_DISCOVERY_RE.match(line.strip()) for line in output.splitlines())
        if m is not None
    ]


def _assert_reported_at(output: str, *, path: str, lineno: int, needle: str) -> None:
    """The finding is reported at *path*:*lineno*, in full, and the needle
    is nowhere in *output*.

    Both halves matter. The first is the only way a reader can go and fix
    the commit, so a redaction that dropped it would have made the gate
    unusable. The second is the reason this gate exists at all: its
    stderr reaches the operator's terminal, and a scanner that repeats the
    private name it just found has published it to everyone watching.
    """
    wanted = [m for m in _discovery_lines(output) if m["path"] == path]
    assert wanted, (
        f"no finding reported as '{path}:<lineno>: <redacted> ...' in the "
        f"output, so the reader is told a path but not which line:\n{output}"
    )
    got = wanted[0]
    assert int(got["lineno"]) == lineno, (
        f"the finding was reported at line {got['lineno']}, not {lineno} -- "
        f"the line number is what a reader pastes into an editor:\n{output}"
    )
    assert got["entry"] == "1", (
        "the report does not name which of the operator's own list entries "
        f"fired, so it cannot be acted on without re-deriving it:\n{output}"
    )
    assert needle not in output, (
        f"the needle was reprinted by the gate that exists to keep it out of "
        f"the commit:\n{output}"
    )


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
    _assert_reported_at(
        got.stderr, path="backend/agent.py", lineno=1, needle=SYNTHETIC_NEEDLE
    )


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
    # The label a ``re:`` hit is recorded under is ``re:`` + the matched
    # text, so the reported length is 17 and not 14. Asserting the exact
    # figure is what keeps this from being satisfied by any old scan that
    # merely said something at that path and line.
    _assert_reported_at(
        got.stderr, path="backend/agent.py", lineno=1, needle="zzshape=000001"
    )
    assert '[list entry #1]' in got.stderr, got.stderr
    assert "zzshape=000001" not in got.stderr, (
        f"the matched text was reprinted:\n{got.stderr}"
    )


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
# A blob that is not UTF-8
# ---------------------------------------------------------------------------
#
# These four are one matrix and the controls are half of it. Before the fix
# the scanner decoded every blob with errors="replace", which rewrites any
# byte it cannot decode into U+FFFD -- so a needle holding a non-ASCII
# character could never be found in a non-UTF-8 file, and the run printed
# ``OK  forbidden-name scan: 0 violations`` over bytes it had not read.
# ASCII needles were unaffected, because errors="replace" only rewrites
# bytes outside ASCII, which is precisely why the hole stayed hidden.
#
# The needle is written out whole, like every other fixture in this file:
# it is synthetic, and a name hidden behind string concatenation is one
# join away from being published.


def _run_name_scan(
    env: dict[str, str], list_path: str
) -> subprocess.CompletedProcess[str]:
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


def test_a_non_ascii_needle_is_found_in_a_non_utf8_file(tmp_path) -> None:
    """The defect itself: a latin-1 blob carrying a latin-1 needle.

    ``errors="replace"`` turns the ``\\xe9`` into U+FFFD, so the needle was
    unreachable and the scan reported zero violations and exit 0.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/agent.py", _NON_ASCII_NEEDLE_LATIN1)
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NON_ASCII_NEEDLE)

    got = _run_name_scan(env, list_path)

    assert got.returncode == 1, (
        f"a latin-1 blob carrying a latin-1 needle was reported clean "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )
    _assert_reported_at(
        got.stderr,
        path="backend/agent.py",
        lineno=1,
        needle=SYNTHETIC_NON_ASCII_NEEDLE,
    )


def test_an_ascii_needle_is_still_found_in_a_non_utf8_file(tmp_path) -> None:
    """The control that was never broken, pinned so it cannot start.

    If the fix for the non-ASCII case ever decoded latin-1 instead of
    searching for the needle's bytes, this is the test that would notice:
    the ASCII needle would arrive mangled and stop matching.
    """
    env = _staged_env(tmp_path)
    _stage_blob(
        env, "backend/agent.py", f"NAME = '{SYNTHETIC_NEEDLE}'\n".encode()
    )
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _run_name_scan(env, list_path)

    assert got.returncode == 1, (
        f"an ASCII needle stopped matching in a non-UTF-8 file "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )
    assert "backend/agent.py:1" in got.stderr, got.stderr


def test_a_non_ascii_needle_is_still_found_in_a_utf8_file(tmp_path) -> None:
    """The control for the ordinary path: encoding loss did not come cheap.

    The scan now makes two passes over a blob it could not decode strictly.
    A UTF-8 blob must still be reported exactly as it was before.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'NAME = "{SYNTHETIC_NON_ASCII_NEEDLE}"\n')
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NON_ASCII_NEEDLE)

    got = _run_name_scan(env, list_path)

    assert got.returncode == 1, (
        f"the ordinary UTF-8 path regressed (exit {got.returncode}):\n"
        f"{got.stdout}\n{got.stderr}"
    )
    assert "backend/agent.py:1" in got.stderr, got.stderr


def test_a_clean_non_utf8_file_still_exits_0(tmp_path) -> None:
    """Zero still means "read it whole and found nothing".

    A second decode over a non-UTF-8 blob must not manufacture a violation
    out of it. ``U+FFFD`` and a mangled ``café`` are not a needle, and a
    check that cried wolf on a clean file would be bypassed the same week.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/agent.py", b"GREETING = 'hello'\n")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NON_ASCII_NEEDLE)

    got = _run_name_scan(env, list_path)

    assert got.returncode == 0, (
        f"a clean non-UTF-8 file was reported as a violation "
        f"(exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )


def test_a_regex_match_over_a_mangled_byte_is_still_reported(tmp_path) -> None:
    """The decoder that makes the fix lossless is also the one that can crash.

    ``surrogateescape`` keeps an undecodable byte as a lone surrogate, which
    is a real code point in ``str`` and has no ``utf-8`` representation --
    so ``print`` refuses it. A ``re:`` needle whose match spanned such a
    byte took the scanner down with ``UnicodeEncodeError`` while reporting a
    finding it had already made, and a gate that dies on the way to printing
    the leak it found is worse than the mismatched decode it replaced.

    This is the regression pin for that, and it is also the pin for ``re:``
    shapes on the byte pass: the pattern is matched over raw bytes in every
    encoding that can represent it.
    """
    env = _staged_env(tmp_path)
    _stage_blob(env, "backend/agent.py", b"NAME = 'zz-caf\xe9-synthetic'\n")
    list_path = _write_list(tmp_path / "forbidden-names", r"re:zz-caf.")

    got = _run_name_scan(env, list_path)
    combined = got.stdout + got.stderr

    assert got.returncode == 1, (
        "a re: shape was not matched over a non-UTF-8 blob "
        f"(exit {got.returncode}):\n{combined}"
    )
    assert "Traceback" not in combined, (
        f"the scanner crashed while printing its own finding:\n{combined}"
    )
    assert "UnicodeEncodeError" not in combined, combined
    # The finding is reported at the right place, in the full shape.
    # This used to assert that the escape ``\udce9`` appeared in the
    # report, because the report used to print the matched span and
    # ``print`` cannot encode a lone surrogate. It prints no span now, so
    # the crash this test was written for is impossible by construction
    # rather than by escaping -- and the way to pin that is to assert no
    # surrogate escaped into the output either way.
    _assert_reported_at(
        combined, path="backend/agent.py", lineno=1, needle="zz-caf"
    )
    assert "\\udce9" not in combined, (
        f"a lone surrogate reached the report, so the undecodable byte is "
        f"still being rendered somewhere:\n{combined}"
    )
    assert "\ufffd" not in combined, (
        f"a replacement character reached the report -- the byte pass is "
        f"printing the decoded match rather than its length:\n{combined}"
    )


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


def test_a_list_that_is_not_utf8_exits_2_not_1(tmp_path) -> None:
    """A list that cannot be decoded is an unread list, not a violation.

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so
    ``load_needles`` did not catch it: it escaped ``main``'s ``try`` and
    took the process down with exit 1 -- which the wrapper reads as "the
    check ran and found violations" and answers with a HOWTO telling the
    reader to go and delete each ``path:lineno: needle`` listed above it.
    There was no needle and no line; the only thing above the HOWTO was a
    traceback.

    The commit was still refused, so this was never a hole in the check.
    It was the wrong label on a refusal, which is its own kind of lie
    about what was examined -- and a reader who believes it goes looking
    for a leak that is not in their index.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", 'GREETING = "hello"\n')
    list_path = tmp_path / "forbidden-names"
    list_path.write_bytes(b"zz-caf\xe9-synthetic\n")

    got = _run_name_scan(env, str(list_path))

    assert got.returncode == 2, (
        f"an unreadable list reported {got.returncode}; it must be 2 -- the "
        f"check did not run, so it did not find a violation:\n"
        f"{got.stdout}\n{got.stderr}"
    )
    assert "Each line above is" not in got.stderr, (
        "the violation HOWTO was printed for a list that was never read, so "
        f"there are no lines above it to act on:\n{got.stderr}"
    )
    assert "Traceback" not in got.stdout + got.stderr, (
        f"an undecodable list still crashes the scanner:\n{got.stdout}{got.stderr}"
    )
    assert "UTF-8" in got.stderr, (
        f"the refusal does not say what is wrong with the list:\n{got.stderr}"
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
    _assert_reported_at(
        _said(got),
        path="backend/nul_probe.py",
        lineno=1,
        needle=SYNTHETIC_NEEDLE,
    )
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
    _assert_reported_at(
        got.stderr, path="backend/nul_probe.py", lineno=1, needle=SYNTHETIC_NEEDLE
    )


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
    # This is the operator's own pre-push hook, on their terminal, over a
    # real range in this checkout -- the single most-read output this
    # repository produces. The finding must be locatable from the report
    # alone, and the needle must not be in it.
    combined = got.stdout + got.stderr
    reported = _discovery_lines(combined)
    assert reported, (
        f"the pre-push report named no file:line for a listed name:\n{combined}"
    )
    assert any(m["path"] == "README.md" for m in reported), (
        f"README.md carries the needle at HEAD and is not named in the "
        f"report:\n{combined}"
    )
    assert needle not in combined, (
        f"the pre-push hook reprinted the needle it exists to block:\n{combined}"
    )


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
    _assert_reported_at(
        got.stdout, path="backend/agent.py", lineno=1, needle=SYNTHETIC_NEEDLE
    )


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


# ---------------------------------------------------------------------------
# The other three surfaces
# ---------------------------------------------------------------------------
#
# The three above all put a name in file *contents*. These put one somewhere
# else -- a commit's message, a file's name, a branch's name -- where content
# scanning does not reach and where the other gates in this repository do not
# look either. Each test below is built so that removing the detection makes
# exactly that test go red and no other, which is what makes the negative
# control a measurement rather than a claim.
#
# The needles stay synthetic (:data:`SYNTHETIC_NEEDLE`) for the reason given
# in this file's docstring: a name written here to test a check for names is
# a name published.


def _scan_repo(repo: Path, list_path: Path, *args: str):
    """Run the scanner module against a throwaway repository."""
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"),
            "--repo", str(repo),
            "--list", str(list_path),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_with_message(repo: Path, subject: str, body: str = "") -> str:
    """Commit an empty tree change carrying *subject* and *body*."""
    args = ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
            "-m", subject]
    if body:
        args += ["-m", body]
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit_file_under(repo: Path, rel: str, content: str, message: str) -> str:
    """Commit *content* at *rel* under an explicit *message*.

    :func:`_commit_file` derives its message from the path -- ``add <rel>``
    -- which is right for the tests that predate the commit-message
    dimension and wrong for the ones that isolate the *path* dimension: a
    message naming the file names the needle a second time, so the path
    test would pass for two reasons and prove only one. This takes the
    message so the path test can hold the other dimension still.
    """
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", rel], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", message],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_a_name_in_a_commit_message_is_reported(tmp_path) -> None:
    """The message is a surface, and the surface is reachable.

    ``--range`` scans every commit's message, and reports it tagged so a
    reader can tell it from a name in a file. The name here is in the
    *body*, not the subject: ``%s`` would miss it, and a leak note, a
    rebase rationale or an audit line is body text.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_with_message(
        repo,
        "docs: record what this branch is for",
        f"noted while investigating: {SYNTHETIC_NEEDLE} is the other one",
    )
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD~1..HEAD")

    assert got.returncode == 1, f"a name in a commit message was not blocked:\n{_said(got)}"
    assert "[commit-message]" in _said(got), (
        f"the finding is not tagged as a commit message, so it cannot be "
        f"told from a name in a file:\n{_said(got)}"
    )
    assert "1 violation" in _said(got), (
        f"expected exactly one violation from one message:\n{_said(got)}"
    )


def test_a_clean_commit_message_does_not_report(tmp_path) -> None:
    """The control for the test above: nothing to find, nothing reported.

    Without this, "the message dimension reports" and "the message
    dimension reports something on every commit" are indistinguishable.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_with_message(repo, "docs: a perfectly ordinary note", "nothing here at all")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD~1..HEAD")

    assert got.returncode == 0, _said(got)
    assert "1 commit message(s)" in got.stdout, (
        f"the message dimension did not run, so this proves nothing:\n{got.stdout}"
    )


def test_a_name_in_a_path_is_reported(tmp_path) -> None:
    """A clean file with a leaky name is still a leak.

    The blob is entirely clean, so the content pass has nothing to say --
    which is the point: the only thing carrying the name is the path.
    """
    repo = _init_repo(tmp_path / "repo")
    # The message is held neutral deliberately: git's own default would name
    # the file, which names the needle, and the message dimension would then
    # fire alongside the path one.
    _commit_file_under(
        repo, f"docs/{SYNTHETIC_NEEDLE}_notes.md",
        "clean, and says nothing.\n", "docs: add notes",
    )
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD~1..HEAD")

    assert got.returncode == 1, f"a name in a path was not blocked:\n{_said(got)}"
    assert "[path-name]" in _said(got), (
        f"the finding is not tagged as a path, so a reader cannot tell it "
        f"from a name in the file's contents:\n{_said(got)}"
    )
    assert "1 violation" in _said(got), (
        f"expected the path to be the only finding:\n{_said(got)}"
    )


def test_a_name_in_a_directory_component_is_reported(tmp_path) -> None:
    """The whole path is searched, not just the file name.

    ``work/<name>/plan.md`` names it in the directory segment exactly as
    ``<name>.md`` names it in the file segment. Searching only the final
    component would miss one and pass the other.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file_under(
        repo, f"work/{SYNTHETIC_NEEDLE}/plan.md", "clean.\n", "docs: add plan",
    )
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD~1..HEAD")

    assert got.returncode == 1, f"a name in a directory component was not blocked:\n{_said(got)}"
    assert "[path-name]" in _said(got), _said(got)
    assert "1 violation" in _said(got), (
        f"expected the directory component to be the only finding:\n{_said(got)}"
    )


def test_a_name_in_a_staged_path_is_reported(tmp_path) -> None:
    """The index checks paths too, not only the range.

    The pre-commit hook runs ``--index``. A path dimension that existed only
    on the range path would leave the earliest and cheapest interception
    point -- before the file is in a commit at all -- checking names in
    contents and nothing else.
    """
    env = _staged_env(tmp_path)
    _stage(env, f"docs/{SYNTHETIC_NEEDLE}_notes.md", "clean.\n")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
         "--index", "--list", str(list_path)],
        capture_output=True, text=True, env=env, check=False,
    )

    assert got.returncode == 1, f"a staged path was not blocked:\n{_said(got)}"
    assert "[path-name]" in _said(got), (
        f"the staged-path finding is not tagged as a path:\n{_said(got)}"
    )


def test_a_name_in_a_local_ref_is_reported(tmp_path) -> None:
    """``--refs-local`` reads branch and tag names.

    Both kinds, because both are published the moment they reach a remote
    and a tag is no less public than a branch.
    """
    repo = _init_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "branch", f"feat/{SYNTHETIC_NEEDLE}"],
        check=True, capture_output=True,
    )
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD", "--refs-local")

    assert got.returncode == 1, f"a name in a branch name was not blocked:\n{_said(got)}"
    assert "[ref-name]" in _said(got), (
        f"the finding is not tagged as a ref name:\n{_said(got)}"
    )
    assert "1 violation" in _said(got), _said(got)


def test_a_name_in_a_local_tag_is_reported(tmp_path) -> None:
    """The tag half of ``--refs-local``, which is a separate ref namespace."""
    repo = _init_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "tag", f"v1.0-{SYNTHETIC_NEEDLE}"],
        check=True, capture_output=True,
    )
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD", "--refs-local")

    assert got.returncode == 1, f"a name in a tag name was not blocked:\n{_said(got)}"
    assert "[ref-name]" in _said(got), _said(got)


def test_ref_names_passed_explicitly_are_scanned(tmp_path) -> None:
    """``--ref`` checks the names a push is publishing.

    This is the pre-push shape, and it is deliberately independent of
    ``--refs-local``: what a push publishes is what its ref lines say, which
    is not necessarily what this repository currently holds. A ref the
    operator has already deleted locally, or one that only exists on a
    remote, still appears in those lines.
    """
    repo = _init_repo(tmp_path / "repo")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(
        repo, list_path, "--range", "HEAD",
        "--ref", f"refs/heads/feat/{SYNTHETIC_NEEDLE}",
        "--ref", "refs/tags/v1.0-release",
    )

    assert got.returncode == 1, f"a pushed ref name was not blocked:\n{_said(got)}"
    assert "[ref-name]" in _said(got), _said(got)
    assert "1 violation" in _said(got), (
        f"the clean tag name alongside it was reported too:\n{_said(got)}"
    )


def test_an_unselected_ref_dimension_says_it_did_not_run(tmp_path) -> None:
    """A dimension that did not run must say so.

    The failure this module exists to prevent is a check that silently did
    not happen reading as a clean pass. When no ref name is selected and
    ``--refs-local`` is absent, the ref dimension has not run -- and the
    output has to name that, because the exit code for it is the same 0
    that a real clean run produces.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", "clean\n")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD")

    assert got.returncode == 0, _said(got)
    assert "ref name(s)" not in got.stdout.split("\n")[0], (
        f"the detail line claims a ref count although none was selected:\n{got.stdout}"
    )
    assert "no ref names selected" in got.stdout, (
        f"the run did not say the ref-name dimension did not run:\n{got.stdout}"
    )


def test_index_says_it_has_no_commit_message_dimension(tmp_path) -> None:
    """``--index`` has no message surface, and says so rather than passing.

    Nothing is committed yet, so there is no message to read. The failure
    this guards against is the ``--index`` equivalent of a green-but-dead
    check: a caller reading exit 0 and concluding the message was checked
    when no message existed.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", "clean\n")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "scan_forbidden_names.sh"),
         "--index", "--list", str(list_path)],
        capture_output=True, text=True, env=env, check=False,
    )

    assert got.returncode == 0, _said(got)
    assert "no commit-message dimension" in got.stdout, (
        f"--index did not say the message dimension does not run here:\n{got.stdout}"
    )


def test_a_missing_message_object_is_exit_2_not_0(tmp_path) -> None:
    """A partial read of the messages is exit 2, never a clean pass.

    This is the fail-closed half of the message dimension and it is the
    reason the count is checked rather than the output being scanned for
    what happens to be there. An empty message -- real, and produced by
    ``--allow-empty-message`` -- is a *read* of zero bytes and exits 0; an
    unreadable object is a read that did not happen and exits 2.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_with_message(repo, "chore: an empty message", "")
    list_path = _write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE)

    got = _scan_repo(repo, list_path, "--range", "HEAD~1..HEAD")

    assert got.returncode == 0, (
        f"a commit with an empty message is a complete read of zero bytes, "
        f"not a failed one:\n{_said(got)}"
    )
    assert "1 commit message(s)" in got.stdout, got.stdout


def test_the_pre_push_hook_receives_the_ref_names_it_is_pushing(tmp_path) -> None:
    """The ref dimension is wired to the hook, not merely available.

    ``--ref`` existing proves nothing on its own: a flag nothing passes is
    the dead-code failure this repository has shipped twice. This drives the
    pre-push entry point with a ref line whose name carries a needle and
    requires the push to be blocked for it.

    The needle is :data:`_CLEAN_TREE_NEEDLE`, which occurs nowhere in this
    repository, so the *only* thing that can block this push is the ref
    name. A needle that also occurs in the tree would let the content scan
    block it, and the test would then pass with the ref dimension dead --
    which is exactly what happened when this test was first written, and
    which the negative control below caught. Likewise the assertion is on a
    *finding line*, not on the string ``[ref-name]``: the wrapper's HOWTO
    legend contains that tag on every blocked push, so asserting on the
    bare tag passes whether or not the dimension ran.
    """
    needle = _CLEAN_TREE_NEEDLE
    ref = f"refs/heads/feat/{needle}-work"
    _write_list(tmp_path / "forbidden-names", needle)
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    tip = _unpublished_tip(head)

    real_list = REPO_ROOT / ".config" / "forbidden-names"
    real_existed = real_list.exists()
    real_bytes = real_list.read_bytes() if real_existed else None
    try:
        real_list.parent.mkdir(parents=True, exist_ok=True)
        real_list.write_text(needle + "\n", encoding="utf-8")
        got = subprocess.run(
            ["bash", str(RANGE_SCRIPT), "--stdin-pre-push"],
            input=f"{ref} {tip} {ref} {head}\n",
            capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
        )
    finally:
        if real_existed:
            real_list.write_bytes(real_bytes)
        else:
            real_list.unlink(missing_ok=True)

    if real_existed:
        assert real_list.read_bytes() == real_bytes, (
            "the test did not restore .config/forbidden-names byte-for-byte"
        )

    said = got.stdout + got.stderr
    assert got.returncode == 1, (
        f"the pre-push hook did not block a push publishing a forbidden "
        f"ref name (exit {got.returncode}):\n{said}"
    )
    # A finding line: the two-space indent the scanner gives each reported
    # violation, followed by this ref and its tag. Asserting on the tag
    # alone would match the HOWTO legend instead.
    assert f"  {ref} [ref-name]:" in said, (
        f"the push was not blocked by a ref-name *finding*; the only way it "
        f"can have been blocked is by something else:\n{said}"
    )


#: A needle for the pre-push control test that occurs nowhere in this
#: repository's tree. The pre-push scan reads every blob in the range, and
#: this file is one of them -- so a needle written here as one literal would
#: be found in this file and every "the push was clean" assertion would fail
#: on the test's own text. The concatenation is not concealment and is not
#: the practice this file's docstring warns about; that warning is about
#: real names, where splitting a string hides a name that must not be
#: written down. Here the opposite is wanted: a string that is definitely
#: not written down, so that "clean" means clean.
_CLEAN_TREE_NEEDLE = "zz-synthetic" + "-clean-control-needle"


def test_the_pre_push_hook_does_not_block_a_clean_ref_name(tmp_path) -> None:
    """The control for the wiring test above.

    A hook that blocked every push would pass that test too. This one pushes
    a ref name that carries no needle and requires the push to be allowed.

    The needle is :data:`_CLEAN_TREE_NEEDLE`, which occurs nowhere in this
    tree, so a clean result is a statement about the whole scan -- every
    blob, the message, the paths and the ref name -- rather than one
    surviving a wall of unrelated findings. That is what leaves "the ref
    name was clean" as the only thing that could have blocked this push.
    """
    needle = _CLEAN_TREE_NEEDLE
    _write_list(tmp_path / "forbidden-names", needle)
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    tip = _unpublished_tip(head)

    real_list = REPO_ROOT / ".config" / "forbidden-names"
    real_existed = real_list.exists()
    real_bytes = real_list.read_bytes() if real_existed else None
    try:
        real_list.parent.mkdir(parents=True, exist_ok=True)
        real_list.write_text(needle + "\n", encoding="utf-8")
        got = subprocess.run(
            ["bash", str(RANGE_SCRIPT), "--stdin-pre-push"],
            input=(
                f"refs/heads/work/an-ordinary-name {tip} "
                f"refs/heads/work/an-ordinary-name {head}\n"
            ),
            capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
        )
    finally:
        if real_existed:
            real_list.write_bytes(real_bytes)
        else:
            real_list.unlink(missing_ok=True)

    if real_existed:
        assert real_list.read_bytes() == real_bytes, (
            "the test did not restore .config/forbidden-names byte-for-byte"
        )

    assert got.returncode == 0, (
        f"the pre-push hook blocked a push whose ref name and commits are "
        f"both clean (exit {got.returncode}):\n{got.stdout}\n{got.stderr}"
    )
    # No finding line of any kind. The bare tag is not the assertion: it
    # appears in the HOWTO legend whenever the check blocks, so testing for
    # it here would pass for a blocked push and mean nothing.
    said = got.stdout + got.stderr
    assert not [l for l in said.splitlines() if l.startswith("  ") and "[ref-name]:" in l], (
        f"a ref-name finding was reported for a clean ref name:\n{said}"
    )


# ---------------------------------------------------------------------------
# Fail-closed: a dimension that did not run is exit 2, never a clean pass
# ---------------------------------------------------------------------------
#
# The three tests above the per-surface ones would still pass if a dimension
# silently reported nothing, which is the failure this module was written to
# stop. These pin the other half: when a dimension is *selected* and cannot
# read its input, the answer is exit 2, not 0.


def test_the_ref_dimension_selected_with_no_names_is_exit_2(tmp_path) -> None:
    """Asking for the ref dimension and reading no names is exit 2.

    Found by writing this feature and testing only the happy path: with
    ``--refs-local`` selected and an empty ref list the scan reported
    ``OK  0 violations`` and exited 0 -- the new check, existing only to
    find things, quietly finding nothing and saying so in the voice of a
    clean run.

    Driven through :func:`scan` with the ref reader stubbed, because the
    honest way to reach "selected but empty" is a repository whose refs were
    all deleted, and building one is a fixture whose brittleness would be its
    own test. What is under test is the branch, not the plumbing above it.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", "clean\n")
    list_path = Path(_write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE))

    forbidden = _load_module(
        REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"
    )
    original = forbidden.read_ref_names
    forbidden.read_ref_names = lambda repo: []
    try:
        with pytest.raises(forbidden.ScanError, match="ref-name dimension"):
            forbidden.scan(
                repo, list_path, index=False, range_specs=["HEAD"],
                check_local_refs=True,
            )
    finally:
        forbidden.read_ref_names = original


def test_the_ref_dimension_raises_on_an_empty_ref_name(tmp_path) -> None:
    """An empty ``--ref`` value is exit 2, not a name that matches nothing.

    The same reasoning one level down: an empty string is a name that did
    not arrive. The wrapper rejects it at the argument layer; this pins the
    scanner, which is reachable directly and must not rely on every caller
    being the wrapper.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", "clean\n")
    list_path = Path(_write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE))

    forbidden = _load_module(
        REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"
    )
    with pytest.raises(forbidden.ScanError, match="empty ref name"):
        forbidden.scan(
            repo, list_path, index=False, range_specs=["HEAD"],
            ref_names=[""],
        )


def test_an_unreadable_commit_message_object_is_exit_2(tmp_path) -> None:
    """A commit object that cannot be read is exit 2, not a clean message.

    The message dimension's partial-read guard. Driven with a SHA that
    resolves to no object, which is the shape a shallow or partially
    fetched checkout produces and which the reader has to survive without
    reporting "no messages to scan".
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", "clean\n")
    list_path = Path(_write_list(tmp_path / "forbidden-names", SYNTHETIC_NEEDLE))

    forbidden = _load_module(
        REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"
    )
    with pytest.raises(forbidden.ScanError, match="could not read its commit object"):
        list(forbidden.read_commit_messages(repo, ["0" * 40]))


def test_a_malformed_commit_object_is_exit_2(tmp_path) -> None:
    """A commit object with no message separator is refused, not guessed at.

    Taking ``tree``/``author`` headers for a message would scan the commit's
    authorship under the pretence of scanning its prose -- which for this
    gate is not a harmless over-scan but a different rule from the one the
    operator wrote.

    The object is written straight into the object store: ``hash-object``
    refuses to create it (``fsck: unterminatedHeader``), which is git being
    stricter than this scanner has any need to be -- git will decline to
    *make* the object, and this test needs git to be able to *read* one it
    did not make.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "backend/agent.py", "clean\n")

    forbidden = _load_module(
        REPO_ROOT / "scripts" / "scanners" / "scan_forbidden_names.py"
    )
    body = b"tree 1234\nauthor T <t@e.invalid> 0 +0000"
    raw = b"commit %d\0" % len(body) + body
    sha = hashlib.sha1(raw).hexdigest()
    directory = repo / ".git" / "objects" / sha[:2]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sha[2:]).write_bytes(zlib.compress(raw))

    with pytest.raises(forbidden.ScanError, match="no blank line"):
        list(forbidden.read_commit_messages(repo, [sha]))
