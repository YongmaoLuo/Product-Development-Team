"""The staged-content scan must not be bypassable, and its plumbing must hold.

Why this file exists
--------------------
``staged_content_scan.py`` is the pre-commit stage's privacy gate: the one
of four surfaces that runs *before* the text is in a commit, so the one
where "no" is still free. Its own tests lived in
``tests/integration/test_staged_content_privacy_gate.py``, and the
``static-gates`` CI job runs ``tests/static_gates/`` only -- so the file
the gate depends on most was covered by a suite that does not run on every
pull request. Two things were true at once and neither was visible: the
implementation was the largest untested unit in the gates, and the one
defect that made it bypassable (a leading NUL byte silently skipping a
``.py``) sat in a function no test called.

So this file pins two things, and the split is deliberate:

1. **The protocol.** ``_parse_batch`` is a hand-written parser for
   ``git cat-file --batch`` output -- a length-prefixed binary format
   whose output framing is not a compatibility guarantee. A parser that
   desynchronises does not raise; it returns bodies belonging to the wrong
   paths, and the gate then reports a clean scan over files it never read.
   That is table-driven below, with the desynchronising inputs spelled
   out, because those are exactly the inputs a smoke test does not
   produce.

2. **The gate itself, through its real entry point.** Every behavioural
   test runs ``scripts/scan_staged_privacy.sh`` -- the command the
   pre-commit hook runs -- against a throwaway index. Not ``scan_staged()``:
   an assertion that calls the function this file is trying to police
   cannot notice that function not being wired to the hook, which is the
   failure mode the whole privacy arrangement is built against.

The NUL skip, specifically
--------------------------
This scan used to skip any staged blob holding a NUL byte in the first
kilobyte -- git's definition of binary -- on the reasoning that "a private
name is text, and no encoding turns one into a NUL". The reasoning is
sound and the skip is still right for a real image. Applied to *every*
blob it was not a rule, it was a bypass: a ``.py`` whose first byte is
``\\x00``, staged beside any other file, was skipped while its neighbour
was read, and the run printed ``OK 0 violations``. ``commit_range_scan``
has always decoded such a file rather than skipping it, so the same bytes
were clean in CI and unreadable at commit time.

Two of the tests below are the contract that replaced it, written so that
reverting the fix turns them red:

* ``test_a_leading_nul_in_source_is_still_scanned`` -- the bypass itself;
* ``test_a_binary_in_the_batch_is_reported_not_passed`` -- the honest
  half, so the fix cannot degenerate into "stop skipping binaries".

Rule text is assembled from fragments
-------------------------------------
``test_commit_range_scan.py`` states the convention: a gate's own source is
scanned by its sibling gates, so a fixture holding the forbidden literal
either fails the suite or forces an exemption that blinds the gate to its
own authors. The plan id below is built from pieces at runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import staged_content_scan  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / "scripts" / "scan_staged_privacy.sh"

#: Matches the gate's own shape (``20[0-9]{6}-[A-Za-z][A-Za-z0-9._-]*``) so
#: the rule fires, while naming nothing that exists.
_LEAK_PLAN_ID = "2026" + "0101-synthetic-staged-plan-id"
_CLEAN_SOURCE = 'GREETING = "hello"\nCOUNT = 3\n'


# ---------------------------------------------------------------------------
# Harness A -- a throwaway index against the real checkout
# ---------------------------------------------------------------------------


def _git(env: dict[str, str], *args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        env=env,
        input=stdin,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("utf-8", "replace")


def _staged_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``GIT_INDEX_FILE`` is a throwaway index.

    ``read-tree HEAD`` seeds it from the real commit, so the staged set is
    the tracked tree plus whatever the test adds. Nothing writes to the
    checkout's own index, and no working-tree file is touched either --
    ``hash-object`` plus ``update-index --cacheinfo`` write the blob and
    the index entry only, which is what makes "the index is read, not the
    working tree" testable at all.
    """
    index = tmp_path / "index"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index)
    _git(env, "read-tree", "HEAD")
    return env


def _stage_bytes(env: dict[str, str], path: str, content: bytes) -> None:
    sha = _git(env, "hash-object", "-w", "--stdin", stdin=content).strip()
    _git(env, "update-index", "--add", "--cacheinfo", f"100644,{sha},{path}")


def _stage(env: dict[str, str], path: str, content: str) -> None:
    _stage_bytes(env, path, content.encode("utf-8"))


def _run_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the hook's entry point, exactly as the hook does."""
    return subprocess.run(
        ["bash", str(_WRAPPER)],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        check=False,
    )


def _nul_prefixed(text: str) -> bytes:
    """*text* with a NUL as its first byte -- git's definition of binary."""
    return b"\0" + text.encode("utf-8")


def _cjk_line() -> str:
    """A source line carrying the Chinese measurement shape and a date.

    The Chinese measurement verb followed by a date is the shape
    ``MEASUREMENT_SHAPES`` matches and a latin-1 file cannot express, so it
    is the line whose visibility the encoding changes.

    Written with ``\\u`` escapes rather than literal characters, and for a
    reason beyond tidiness: ``test_source_comments_carry_no_local_measurements``
    walks every first-party source *including this directory*, so spelling
    the shape out here would be this file tripping the rule it exists to
    test -- exactly the self-reference that file's own convention of
    assembling forbidden strings from pieces exists to avoid.
    """
    return "# " + "\u5b9e\u6d4b\uff082026-01-02\uff09\n"


def _latin1_cjk_line() -> bytes:
    """The same line in gb2312 -- a non-ASCII encoding of those characters.

    gb2312 rather than latin-1 because the characters here are not
    representable in latin-1 at all; the point is only that the bytes are
    not valid UTF-8.
    """
    return _cjk_line().encode("gb2312")


# ---------------------------------------------------------------------------
# Harness B -- a scratch repository, for the git plumbing
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    """A scratch repository with committing configured locally.

    ``config`` rather than global config: the suite must not depend on,
    or mutate, whatever identity the machine happens to have. ``gpgsign``
    is forced off for the same reason -- a signing key that wants a
    passphrase would hang the run.
    """
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "init", "--quiet"],
        check=True,
        capture_output=True,
    )
    for key, value in (
        ("user.email", "gate@example.invalid"),
        ("user.name", "gate"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value],
            check=True,
            capture_output=True,
        )


def _scratch_stage(repo: Path, rel: str, content: bytes) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", rel],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# The cat-file protocol -- table-driven, because a desync is silent
# ---------------------------------------------------------------------------


def _record(oid: str, body: bytes) -> bytes:
    """One ``cat-file --batch`` response: header line, body, trailing LF."""
    return f"{oid} blob {len(body)}\n".encode("ascii") + body + b"\n"


#: ``(id, raw stdout, expected bodies)``. Every raw buffer is shaped the
#: way this git produces it. Malformed inputs live in
#: :data:`_PARSE_ERROR_CASES` rather than here, because "returns these" and
#: "raises" are different contracts and one parameter column cannot hold
#: both without the reader working out which one it is looking at.
_PARSE_CASES: tuple[tuple[str, bytes, list[bytes | None]], ...] = (
    ("empty output", b"", []),
    ("one blob", _record("a" * 40, b"x = 1\n"), [b"x = 1\n"]),
    (
        "several blobs, in request order",
        _record("a" * 40, b"first\n")
        + _record("b" * 40, b"second\n")
        + _record("c" * 40, b""),
        [b"first\n", b"second\n", b""],
    ),
    ("a missing object", b":backend/gone.py missing\n", [None]),
    (
        "a missing object keeps its position",
        _record("a" * 40, b"first\n")
        + b":backend/gone.py missing\n"
        + _record("c" * 40, b"third\n"),
        [b"first\n", None, b"third\n"],
    ),
    ("an empty blob is a body, not an absence", _record("a" * 40, b""), [b""]),
    (
        "a NUL inside the body does not end the record",
        # The reason this parser reads by length rather than by delimiter:
        # every record ends in LF, but neither the header nor the body is
        # free of NULs, and a payload holding one must not be mistaken for
        # the end of the stream.
        _record("a" * 40, b"a = '\0'\nb = 2\n") + _record("b" * 40, b"next\n"),
        [b"a = '\0'\nb = 2\n", b"next\n"],
    ),
    (
        "invalid UTF-8 is left for the caller to decode",
        # Decoding happens one level up, with errors="replace". The parser
        # slices bytes and must not refuse a blob it was not asked to
        # validate.
        _record("a" * 40, b"\xff\xfe = 1\n"),
        [b"\xff\xfe = 1\n"],
    ),
)

#: ``(id, raw stdout, message the refusal must carry)``. Each of these is a
#: way the stream can end badly. Returning a short list from any of them
#: would leave paths silently unscanned, which is the one outcome this
#: parser must never produce.
_PARSE_ERROR_CASES: tuple[tuple[str, bytes, str], ...] = (
    (
        "a size larger than the remaining output",
        f"{'a' * 40} blob 4096\n".encode("ascii") + b"short\n",
        "only .* remain",
    ),
    (
        "a header with no LF",
        f"{'a' * 40} blob 4".encode("ascii"),
        "unterminated header",
    ),
    (
        "a header that is not oid/type/size",
        b"not a git header\n",
        "unrecognised header",
    ),
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [pytest.param(raw, expected, id=case_id) for case_id, raw, expected in _PARSE_CASES],
)
def test_parse_batch(raw: bytes, expected: list[bytes | None]) -> None:
    """``_parse_batch`` returns bodies by length, one per request.

    A parser that guessed wrong here would not fail loudly -- it would
    attribute file A's contents to file B and report a clean commit. Note
    the NUL row: the payload of a skipped-then-read blob is exactly the
    input that breaks a delimiter-based parser, and it is also the input
    the bypass fix depends on being handled.
    """
    assert staged_content_scan._parse_batch(raw) == expected  # noqa: SLF001


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param(raw, message, id=case_id)
        for case_id, raw, message in _PARSE_ERROR_CASES
    ],
)
def test_parse_batch_refuses_a_truncated_stream(
    raw: bytes, message: str
) -> None:
    """Every malformed stream raises instead of returning a short list."""
    with pytest.raises(staged_content_scan.StagedScanError, match=message):
        staged_content_scan._parse_batch(raw)  # noqa: SLF001


def test_the_parser_matches_this_git_on_a_real_index(tmp_path: Path) -> None:
    """The table above is hand-written; this runs the real plumbing.

    A fixture can only prove the parser agrees with the author's memory of
    ``cat-file`` output. Reading a scratch repository's actual index also
    pins the parts the table cannot: that ``--batch -z`` NUL-terminates the
    *input* while the *output* stays LF-framed, that bodies come back in
    request order, and that a NUL-carrying blob round-trips byte for byte
    instead of being truncated at the byte the gate used to stop at.
    """
    repo = tmp_path / "scratch"
    _init_repo(repo)
    _scratch_stage(repo, "backend/alpha.py", b"a = 1\n")
    _scratch_stage(repo, "backend/beta.py", b"\0b = 2\n")
    _scratch_stage(repo, "frontend/empty.py", b"")

    paths = staged_content_scan.staged_paths(repo)
    assert len(paths) == 3, paths
    blobs = dict(
        (path, blob)
        for path, blob in staged_content_scan.iter_staged_blobs(repo, paths)
    )
    assert blobs["backend/alpha.py"] == b"a = 1\n"
    assert blobs["backend/beta.py"] == b"\0b = 2\n", (
        "a NUL-carrying blob must come back whole -- this is the byte the "
        "old skip keyed on, and reading it is half the fix"
    )
    assert blobs["frontend/empty.py"] == b""


def test_an_empty_request_reads_nothing_rather_than_failing(tmp_path: Path) -> None:
    """``[]`` in, ``[]`` out -- no subprocess, no error."""
    repo = tmp_path / "scratch"
    _init_repo(repo)
    assert staged_content_scan.iter_staged_blobs(repo, []) == []


# ---------------------------------------------------------------------------
# The scannable policy -- the bypass, pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "content", "expected"),
    [
        pytest.param(
            Path("backend/leak_bin.py"),
            b"\0x = 1\n",
            True,
            id="leading NUL in a .py is still read",
        ),
        pytest.param(
            Path("scripts/tool.sh"),
            b"\0echo hi\n",
            True,
            id="leading NUL in a .sh is still read",
        ),
        pytest.param(
            Path("frontend/icon.png"),
            b"\x89PNG\r\n\x1a\n\x00",
            False,
            id="a real binary is skipped",
        ),
        pytest.param(
            Path("frontend/data.bin"),
            b"\0\x01\x02",
            False,
            id="an unknown extension with a NUL is skipped",
        ),
        pytest.param(
            Path("frontend/notes.png"),
            b"plain text, misleading suffix\n",
            True,
            id="a non-source suffix with no NUL is read anyway",
        ),
    ],
)
def test_is_scannable(rel: Path, content: bytes, expected: bool) -> None:
    """The extension decides; the NUL only ever authorises a *skip*.

    The first two rows are the fix. Reverting the extension test inside
    :func:`staged_content_scan.is_scannable` turns both red, because they
    assert the opposite of what the function used to answer for every blob
    regardless of its name.
    """
    assert staged_content_scan.is_scannable(rel, content) is expected


def test_a_nul_past_the_probe_window_is_still_text() -> None:
    """Git's threshold is 1 KiB and the skip inherits it exactly.

    A file whose first kilobyte is NUL-free is text by git's definition,
    so the scan reads it. Pinning the boundary means a future "just widen
    the probe" edit lands here rather than as a silently narrower skip.
    """
    assert (
        staged_content_scan.is_scannable(Path("frontend/x.png"), b"a" * 1024 + b"\0")
        is True
    )


# ---------------------------------------------------------------------------
# The gate, through the command the pre-commit hook runs
# ---------------------------------------------------------------------------


def test_a_leading_nul_in_source_is_still_scanned(tmp_path: Path) -> None:
    """The bypass, reproduced end to end: exit 0 before, exit 1 after.

    The batch is a NUL-prefixed ``.py`` carrying the plan id *plus* a clean
    text file. That second file is what let the defect survive: with only
    the NUL file staged the run exited 2 ("none of them text"), which
    looks like a gate refusing to work -- so the insurance read as
    load-bearing. Add any ordinary file and the NUL one was skipped, the
    ordinary one was read, and the run reported ``OK 0 violations``. Every
    real commit stages more than one file.
    """
    env = _staged_env(tmp_path)
    _stage_bytes(
        env, "backend/leak_bin.py", _nul_prefixed(f'PLAN = "{_LEAK_PLAN_ID}"\n')
    )
    _stage(env, "backend/innocent.py", _CLEAN_SOURCE)

    got = _run_gate(env)
    combined = got.stdout + got.stderr
    assert got.returncode != 0, (
        "the staged-content scan passed a commit carrying a plan id in a "
        f"NUL-prefixed source file:\n{combined}"
    )
    # The report has to name the file and the line, because that is what
    # the committer acts on -- and it must NOT quote the plan id, because
    # this scan runs from the pre-commit hook and its stderr lands in
    # whatever the operator is looking at. Both halves are asserted here;
    # the second is the half this assertion used to get backwards.
    assert (
        "backend/leak_bin.py:1:" in combined
    ), f"the finding is not reported as path:lineno:\n{combined}"
    assert "<redacted>" in combined, (
        f"the report does not mark the matched text as withheld:\n{combined}"
    )
    assert _LEAK_PLAN_ID not in combined, (
        "the plan id was reprinted by the gate that exists to keep it out "
        f"of the commit:\n{combined}"
    )


def test_a_nul_prefixed_source_file_is_counted_as_scanned(tmp_path: Path) -> None:
    """The count is the evidence the skip is gone.

    ``scanned 1`` is what the old run printed for that batch; ``scanned 2``
    is what a scan that read the NUL file prints. Asserting the count as
    well as the verdict keeps the test from being satisfied by a block that
    happens to exit non-zero for some unrelated reason.
    """
    env = _staged_env(tmp_path)
    _stage_bytes(env, "backend/leak_bin.py", _nul_prefixed(_CLEAN_SOURCE))
    _stage(env, "backend/innocent.py", _CLEAN_SOURCE)

    got = _run_gate(env)
    assert got.returncode == 0, f"clean content blocked:\n{got.stdout}\n{got.stderr}"
    assert "scanned 2 staged file(s)" in got.stdout, got.stdout


def test_a_binary_in_the_batch_is_reported_not_passed(tmp_path: Path) -> None:
    """The other half: skipping is legitimate only if it is *said*.

    A real image is unreadable as text and has to be skipped -- but a batch
    that skipped one was not read whole, so it cannot be reported clean.
    Exit 2, not 0. This is what stops "stop skipping binaries" from being
    the whole of the fix: that trade alone would read every image as
    replacement characters and manufacture findings out of them.
    """
    env = _staged_env(tmp_path)
    _stage_bytes(
        env, "frontend/icon.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )
    _stage(env, "backend/innocent.py", _CLEAN_SOURCE)

    got = _run_gate(env)
    combined = got.stdout + got.stderr
    assert got.returncode == 2, (
        "a batch holding an unreadable blob must report 'did not run' "
        f"(exit 2), not a clean scan (exit 0):\n{combined}"
    )
    assert "icon.png" in combined, f"the skipped path must be named:\n{combined}"


def test_a_binary_alone_is_still_refused(tmp_path: Path) -> None:
    """Unchanged behaviour: nothing read at all is an error, not a pass."""
    env = _staged_env(tmp_path)
    _stage_bytes(
        env, "frontend/icon.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )

    got = _run_gate(env)
    assert got.returncode == 2, f"expected exit 2:\n{got.stdout}\n{got.stderr}"


def test_a_clean_text_batch_still_passes(tmp_path: Path) -> None:
    """The counterweight. A gate that blocks everything is bypassed."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", _CLEAN_SOURCE)
    _stage(env, "backend/other.py", "VALUE = 2\n")

    got = _run_gate(env)
    assert got.returncode == 0, f"clean content blocked:\n{got.stdout}\n{got.stderr}"
    assert "0 violations" in got.stdout, got.stdout


def test_an_empty_index_is_still_refused(tmp_path: Path) -> None:
    """``git commit -a`` with no changes is not a clean scan."""
    env = _staged_env(tmp_path)
    env["GIT_INDEX_FILE"] = str(tmp_path / "empty-index")

    got = _run_gate(env)
    assert got.returncode == 2, f"expected exit 2:\n{got.stdout}\n{got.stderr}"


# ---------------------------------------------------------------------------
# A staged blob that is not UTF-8
# ---------------------------------------------------------------------------
#
# The staged rule set is ASCII-only today, so unlike the range scan there is
# no live needle here to lose: what this pins is the *shape*, because
# STAGED_RULES is a table and the next non-ASCII rule added to it would be
# silently blind the moment a latin-1 file was staged beside it -- at the
# one surface of the four that can still stop the commit.
#
# The blob body is built from an escaped literal rather than a CJK string
# literal so this file stays pure ASCII on disk. That is not tidiness: a
# non-UTF-8 source file in the tree would make ``commit_range_scan`` refuse
# to read its own repository.


def test_a_non_utf8_staged_blob_is_refused_not_reported_clean(tmp_path: Path) -> None:
    """A blob this scan cannot decode is one it did not read.

    Before: decoded with ``errors="replace"``, so every undecodable byte
    became ``U+FFFD``, a rule whose pattern holds a non-ASCII character
    could never match, and the run still printed ``OK ... 0 violations``.
    """
    env = _staged_env(tmp_path)
    _stage_bytes(env, "backend/probe.py", _latin1_cjk_line())
    _stage(env, "backend/innocent.py", _CLEAN_SOURCE)

    got = _run_gate(env)
    combined = got.stdout + got.stderr
    assert got.returncode == 2, (
        "a staged blob that is not valid UTF-8 must report 'did not run' "
        f"(exit 2), not a clean scan (exit 0):\n{combined}"
    )
    assert "probe.py" in combined, f"the unreadable path must be named:\n{combined}"
    assert "UTF-8" in combined, (
        f"the refusal must say what is wrong with the blob:\n{combined}"
    )
    assert "binary content" not in combined, (
        "the refusal borrowed the skipped-binary wording. A latin-1 text "
        f"file is not binary, and that sentence would mislabel it:\n{combined}"
    )


def test_a_utf8_blob_with_the_same_text_still_passes(tmp_path: Path) -> None:
    """The control. Only the encoding differs, and it must still be read.

    Without this the refusal could pass for a gate that simply stopped
    reading source files.
    """
    env = _staged_env(tmp_path)
    _stage_bytes(env, "backend/probe.py", _cjk_line().encode("utf-8"))
    _stage(env, "backend/innocent.py", _CLEAN_SOURCE)

    got = _run_gate(env)
    assert got.returncode == 0, (
        f"the same text as UTF-8 was refused too:\n{got.stdout}\n{got.stderr}"
    )
    assert "scanned 2 staged file(s)" in got.stdout, got.stdout


# ---------------------------------------------------------------------------
# Wiring -- the module the hook runs is the module these tests import
# ---------------------------------------------------------------------------


def test_the_hook_runs_this_module() -> None:
    """``scan_staged_privacy.sh`` must invoke ``staged_content_scan.py``.

    Every behavioural test above goes through the wrapper, so if the
    wrapper ever started running a second implementation -- or stopped
    running this one -- they would all stay green while the hook stopped
    being the thing they measured.
    """
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    assert "backend/tests/static_gates/staged_content_scan.py" in wrapper, wrapper


def test_the_rule_table_is_not_empty() -> None:
    """An empty table would make every behavioural test pass for free."""
    assert staged_content_scan.STAGED_RULES, "the staged rule table is empty"
