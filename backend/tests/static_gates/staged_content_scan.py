"""Apply the privacy rules to what a ``git commit`` is about to record.

Why this module exists
----------------------
The other three surfaces are all after the fact:

  * :mod:`commit_range_scan` runs after the commits exist, in CI or on push.
  * the static gates read the working tree, which can be clean while the
    commit that was just made carries the leak.
  * :mod:`pr_metadata_scan` runs on a pull request, before the merge.

This one runs **before** the commit exists, on the content in the index.
That is the only point at which refusing costs nothing: the offending text
has not entered history, so the fix is "edit the file", not "rewrite a
commit that someone may already have fetched".

What it reads
-------------
The index, not the working tree. They differ exactly when it matters most:
a file can be edited back to clean after being staged, and the commit will
still carry what was staged. Scanning ``HEAD`` or the working directory
would read the tidied version and report a clean commit that is not clean.

The rule here is the same pure function the tree gate and the range scan
use -- :func:`find_plan_ids_in_non_test_source` -- so there is one
definition of a violation and no drift between surfaces. No rule is
restated.

What it deliberately does not check
-----------------------------------
Private proper names. That check used to be the second entry in
:data:`STAGED_RULES`, calling ``find_private_names`` from a pytest gate --
which meant the names themselves were committed to this repository, and the
check only ran wherever pytest ran. Both halves of that were wrong: a
blacklist that is checked in publishes the names, and a check that runs in
CI cannot run at commit time.

It now lives in ``scripts/scanners/scan_forbidden_names.py``, reading the
operator's local ``.config/forbidden-names``, and it runs here as its own
step of ``scripts/pre_commit_gate.sh``. That step is not in
:data:`STAGED_RULES` because its needles are not in this repository and so
cannot be a pure function here.

What it deliberately does not check
-----------------------------------
The commit *message*. That is a different surface with a different rule
(``test_no_plan_ids_in_metadata``, no test exemption) and it is checked by
``scripts/check_commit_msg.py`` from the ``commit-msg`` hook. Keeping them
apart is what lets each run at the stage where it can still stop something.

Binary content
--------------
Git calls a blob binary when it holds a NUL byte in the first kilobyte,
and the other three surfaces do not skip on that basis:
:mod:`commit_range_scan` narrows to ``SOURCE_EXTENSIONS`` under the scan
roots, so it never reaches an image at all, and this module's sibling
rules read whatever the extension filter lets through.

This module used to skip such a blob instead, on the reasoning that "a
private name is text, and no encoding turns one into a NUL". That
reasoning holds and the skip is still right for a genuine binary -- but it
was applied to *every* blob, including ones named ``.py``, and the
pre-commit stage is the only surface of the four that runs before the text
is in a commit. So the one gate whose whole value is stopping the commit
was the one gate a single NUL byte disabled: stage a ``.py`` whose first
byte is ``\0`` next to any other file, and it is skipped while its
neighbour is scanned, and the run reports ``OK 0 violations``.

The split is by *extension*, not by content, and only for the skip side:

  * a source-suffixed blob (anything in
    :data:`source_scan.SOURCE_EXTENSIONS`) is always read, NUL or not,
    exactly as the range scan reads it;
  * any other blob with a NUL in the first kilobyte is skipped, and the
    batch is reported as degraded -- see below.

Failure is loud
---------------
An empty index is reported as an error rather than a pass, for the same
reason :mod:`commit_range_scan` refuses an empty range: a scan that read
nothing must not look like one that found nothing. ``git commit -a`` with
no changes is the case that would otherwise slip through.

The same rule governs a *partial* read. If anything in the batch was
skipped, the scan did not read everything it was asked to read, so it
exits 2 even when it found nothing -- one skipped blob means an unknown
number of unexamined bytes, and "no violations in what I read" is not the
same claim as "no violations in the commit".

Content that is not UTF-8
-------------------------
Staged blobs were decoded with ``errors="replace"``, which decides what a
blob holds without looking at it: an undecodable byte becomes ``U+FFFD``.
A rule whose pattern holds a non-ASCII character can then never match, and
the run still printed ``OK ... 0 violations``. ASCII rules were unaffected
-- ``errors="replace"`` only rewrites bytes outside ASCII -- which is why
the hole hid behind files that happened to be UTF-8.

:class:`~staged_content_scan.StagedScanError` is raised instead, naming
the blob, for the same reason :mod:`commit_range_scan` raises rather than
substitutes. No alternative decoder recovers these rules: ``surrogateescape``
keeps the bytes but not their identity, and none of the non-ASCII
characters in these rules' patterns is representable in latin-1 or
cp1252, so every alternative returned the same verdict in measurement.
The staged rule set today is ASCII-only, so this is a guard rather than a
live hole -- but ``STAGED_RULES`` is a table, the next non-ASCII rule
added to it would be silently blind the moment a latin-1 file was staged
beside it, and the pre-commit stage is the one surface of the four that
can still stop the commit. Refusing costs a re-save; guessing costs a leak.

Unlike a skipped binary this is a hard error rather than an entry on
``skipped``: a latin-1 text file is not "binary content, and its extension
is not one this scan treats as source", and printing that sentence about
it would mislabel the refusal in the one place a reader is deciding what to
do next.

Exit contract
-------------
  * 0 -- scan ran, read everything staged, zero violations
  * 1 -- scan ran, read everything staged, at least one violation
  * 2 -- the scan could not run, or did not read everything staged
        (usage error, unreadable index, a skipped blob, a blob that is not
        valid UTF-8)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_scan  # noqa: E402  (path set above)

from test_no_plan_ids_in_non_test_source import (  # noqa: E402
    find_plan_ids_in_non_test_source,
)

#: Repository root -- this file lives at
#: ``backend/tests/static_gates/staged_content_scan.py``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``(label, pure_rule)`` pairs, same table shape as ``RANGE_RULES``.
#:
#: This rule is here rather than only in the home-path/attribution/
#: measurement gates: those already run over the whole working tree at the
#: ``pre-commit`` stage via ``scripts/grep_guard.sh``'s sibling, and a file
#: can be staged without ever having been walked.
#:
#: The private-name rule is *not* here and never can be. Its needles are
#: operator-owned and untracked, so there is no pure function over them to
#: put in this table; it is a separate step of the hook. See the module
#: docstring.
STAGED_RULES: tuple[tuple[str, object], ...] = (
    ("plan id in source", find_plan_ids_in_non_test_source),
)


#: How many leading bytes decide whether a blob is binary. Git's own
#: threshold, copied so this module's idea of "binary" cannot drift from
#: ``git diff``'s -- which matters, because a file git renders as binary
#: in a review is a file a reviewer will not read either.
_BINARY_PROBE_BYTES = 1024


class StagedScanError(RuntimeError):
    """The scan could not run. Never conflated with "found nothing"."""


@dataclass(frozen=True, repr=False)
class Finding:
    """One rule violation inside one staged blob.

    ``snippet`` holds the matched text and stays in memory; ``format`` is
    what withholds it. A staged blob is arbitrary content the operator is
    about to publish, so the matched span is exactly the material this
    rule exists to keep out of history -- quoting it in the pre-commit
    output puts it on screen and in any log that captures the hook.

    ``repr=False`` because the generated ``__repr__`` interpolates every
    field, ``snippet`` included, and a ``repr`` reaches a traceback and a
    pytest assertion message as readily as ``print`` reaches a terminal.
    """

    label: str
    path: str
    lineno: int
    snippet: str

    def format(self) -> str:
        return (
            f"[{self.label}]  {self.path}:{self.lineno}: "
            f"{source_scan.redact_span(self.snippet)}"
        )

    def __repr__(self) -> str:
        return f"Finding({self.format()})"


@dataclass(frozen=True)
class StagedScan:
    """What one pass over the index actually read, and what it found.

    ``skipped`` is part of the result rather than a diagnostic counter
    because it changes the exit code: a batch with a skipped blob is a
    batch this scan did not read whole, and reporting that as a clean scan
    is the failure this module exists to avoid.
    """

    scanned: int
    skipped: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def degraded(self) -> bool:
        """True iff anything staged was left unread."""
        return bool(self.skipped)


def _git(repo: Path, args: Sequence[str]) -> bytes:
    """Run a git command and return stdout, or raise :class:`StagedScanError`.

    An explicit raise rather than ``check=True`` so the message can name the
    command: a bare ``CalledProcessError`` from inside the scan is not
    actionable at the moment a developer is trying to commit.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise StagedScanError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def staged_paths(repo: Path = REPO_ROOT) -> list[str]:
    """Return the repository-relative path of every staged file.

    ``--diff-filter=ACMR`` drops deletions (there is no content to scan) and
    the copy/rename *sources* (only the destination is staged). ``-z``
    because a path may contain a newline, and a newline-separated list
    would silently split one file into two halves -- one of which may scan
    clean.

    Nothing is filtered by extension here: a plan-id shape in a ``.yaml``
    or a ``.md`` in non-test source is as published as one in a ``.py``.
    """
    out = _git(
        repo,
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
    )
    return [
        chunk.decode("utf-8", "replace")
        for chunk in out.split(b"\0")
        if chunk
    ]


def iter_staged_blobs(repo: Path, paths: Sequence[str]) -> list[tuple[str, bytes | None]]:
    """Return ``(path, content)`` for each staged path, via one batch read.

    ``<path>::<blob sha>`` rather than ``:<path>``: a path beginning with a
    colon would otherwise be read as a rev. The NUL-terminated list keeps
    one shell-free batch instead of one subprocess per file, which is the
    difference between a pre-commit hook that is invisible and one that is
    not.

    ``None`` means git answered "missing", i.e. the index entry vanished
    between listing and reading. That is treated as an error rather than a
    skip: a silently skipped file is an unscanned file.
    """
    if not paths:
        return []

    specs = "\0".join(f":{path}" for path in paths) + "\0"
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch", "-z", "--buffer"],
        input=specs.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise StagedScanError(
            f"git cat-file --batch failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )

    blobs = _parse_batch(result.stdout)
    if len(blobs) != len(paths):
        raise StagedScanError(
            f"git cat-file --batch returned {len(blobs)} objects for "
            f"{len(paths)} requests; refusing to scan a partial read"
        )
    return list(zip(paths, blobs))


def _parse_batch(out: bytes) -> list[bytes | None]:
    """Parse ``cat-file --batch`` output into blob bodies.

    Measured on this git, not assumed: ``-z`` changes the **input**
    delimiter to NUL and leaves the **output** exactly as it is without it
    -- a header line ``<oid> SP <type> SP <size> LF``, then ``<size>``
    raw bytes, then one LF. So the payload is read by length, never by
    splitting on a delimiter, and the terminators are LFs.

    Getting this wrong is silent in the dangerous direction: a parser that
    expects NULs where git emits LFs reads the header as the whole record
    and every later path desynchronises.
    """
    blobs: list[bytes | None] = []
    pos = 0
    total = len(out)
    while pos < total:
        newline = out.find(b"\n", pos)
        if newline == -1:
            raise StagedScanError("git cat-file --batch: unterminated header")
        header = out[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        if header.endswith(" missing"):
            blobs.append(None)
            continue
        fields = header.rsplit(" ", 2)
        if len(fields) != 3 or not fields[2].isdigit():
            raise StagedScanError(
                f"git cat-file --batch: unrecognised header {header!r}"
            )
        size = int(fields[2])
        end = pos + size
        if end > total:
            raise StagedScanError(
                f"git cat-file --batch: blob at offset {pos} claims {size} "
                f"bytes but only {total - pos} remain"
            )
        blobs.append(out[pos:end])
        pos = end + 1  # skip the record's trailing LF
    return blobs


def is_scannable(rel: Path, content: bytes) -> bool:
    """Return True iff this blob must be read by the scan.

    The extension decides, and the content only ever decides the *skip*
    side. Two halves, and the order is the fix:

    1. A source-suffixed blob (:data:`source_scan.SOURCE_EXTENSIONS`) is
       always read. A NUL byte in it does not make the private name in it
       stop being text -- it makes this module the one gate of four that a
       leading ``\\x00`` disables, and the pre-commit stage is the only
       one of the four that can still stop the commit. The replacement
       characters decoding produces are a cost; skipping the file is a
       hole. :mod:`commit_range_scan` has always made this trade the other
       way, and a gate that treats the same bytes as clean in CI and as
       unreadable at commit time is not two gates, it is one gate with a
       blind spot in it.
    2. Anything else with a NUL in the first kilobyte is git's own
       definition of binary, and is skipped. This half is legitimate --
       decoding a PNG with ``errors="replace"`` manufactures findings out
       of replacement characters -- so the cost of getting it wrong is
       paid by :attr:`StagedScan.degraded` instead: a skipped blob makes
       the whole batch exit 2.
    """
    if rel.suffix in source_scan.SOURCE_EXTENSIONS:
        return True
    return b"\0" not in content[:_BINARY_PROBE_BYTES]


def _undecodable_message(path: str, exc: UnicodeDecodeError) -> str:
    """The refusal text for a staged blob that is not valid UTF-8.

    Deliberately *not* a line on the ``skipped`` list. That list's entries
    say "binary content, and its extension is not one this scan treats as
    source", and a latin-1 ``.txt`` is neither binary nor unrecognised --
    printing that sentence about it would be the same wrong-label failure
    the exit codes exist to prevent, one layer up.

    The wording is shared verbatim with
    :mod:`commit_range_scan._undecodable_message`, for the reason the two
    modules already share the binary rule: a reader who has seen one has
    seen both.
    """
    return (
        f"{path} is not valid UTF-8 and this scan cannot read it: {exc}\n"
        f"The privacy rules are patterns over text, and running them over a "
        f"blob whose encoding this scan would have to guess is not reading "
        f"it. It used to be decoded with errors=\"replace\", which rewrites "
        f"every byte it cannot decode into U+FFFD -- and a rule whose shape "
        f"holds a non-ASCII character (the Chinese measurement and "
        f"attribution shapes do) can then never match, while the run still "
        f"reported a clean result over bytes it had not examined.\n"
        f"Re-save the file as UTF-8, `git add` it again, and commit again. "
        f"Nothing has been scanned and nothing has been cleared."
    )


def scan_staged(repo: Path = REPO_ROOT) -> StagedScan:
    """Apply :data:`STAGED_RULES` to every staged file.

    Raises :class:`StagedScanError` when the index holds nothing
    scannable, so that "I staged nothing" cannot be reported as "I staged
    something clean".
    """
    paths = staged_paths(repo)
    if not paths:
        raise StagedScanError(
            "nothing is staged. If this is a `git commit -a` with no "
            "changes, or a merge, that is fine and this scan has nothing "
            "to do -- but it is not a clean scan, and reporting it as one "
            "would be indistinguishable from having read the index."
        )

    findings: list[Finding] = []
    skipped: list[str] = []
    scanned = 0
    for path, content in iter_staged_blobs(repo, paths):
        if content is None:
            raise StagedScanError(
                f"{path} is in the index but git could not read its blob; "
                f"refusing to report a clean scan over a partial read"
            )
        rel = Path(path)
        if not is_scannable(rel, content):
            skipped.append(path)
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StagedScanError(_undecodable_message(path, exc)) from exc
        scanned += 1
        for label, rule in STAGED_RULES:
            for _, lineno, snippet in rule(rel, text):  # type: ignore[operator]
                findings.append(
                    Finding(label=label, path=path, lineno=lineno, snippet=snippet)
                )

    if scanned == 0:
        raise StagedScanError(
            f"{len(paths)} staged file(s), none of them text; refusing to "
            f"report a clean scan over an empty read"
        )
    return StagedScan(scanned=scanned, skipped=tuple(skipped), findings=tuple(findings))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="staged_content_scan",
        description=(
            "Apply the privacy rules to the content staged for the next "
            "commit. Exits 0 clean, 1 on violations, 2 when the scan could "
            "not run."
        ),
    )
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT),
        help="repository whose index to scan (default: this checkout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = scan_staged(Path(args.repo))
    except StagedScanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    detail = f"scanned {result.scanned} staged file(s)"
    if result.skipped:
        detail += (
            f" ({len(result.skipped)} binary blob(s) skipped -- "
            f"the commit was NOT fully read)"
        )
    print(detail)

    if result.findings:
        print(
            f"FAIL staged-content privacy scan: "
            f"{len(result.findings)} violation(s)"
        )
        for finding in result.findings:
            print(f"  {finding.format()}")

    if result.degraded:
        # Findings first, then the degradation: the violations are real and
        # were found, and a reader who sees only "the scan could not run"
        # will go looking for a git problem instead of the plan id.
        print(
            "ERROR: this scan did not read the whole commit, so it cannot "
            "report a clean result.",
            file=sys.stderr,
        )
        for path in result.skipped:
            print(
                f"ERROR:   not scanned: {path} "
                f"(binary content, and its extension is not one this scan "
                f"treats as source)",
                file=sys.stderr,
            )
        print(
            "ERROR: Unstage the file(s) above, or run the commit with only "
            "text staged, and commit again. If one of them really is a "
            "binary that must be committed, commit it on its own -- a "
            "batch with nothing else in it still cannot be reported clean, "
            "so `--no-verify` is the honest answer for a deliberate binary "
            "and only for that.",
            file=sys.stderr,
        )
        return 2

    if not result.findings:
        print("OK  staged-content privacy scan: 0 violations")
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover  (exercised via the hook)
    sys.exit(main())
