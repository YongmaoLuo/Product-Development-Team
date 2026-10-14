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

Failure is loud
---------------
An empty index is reported as an error rather than a pass, for the same
reason :mod:`commit_range_scan` refuses an empty range: a scan that read
nothing must not look like one that found nothing. ``git commit -a`` with
no changes is the case that would otherwise slip through.

Exit contract
-------------
  * 0 -- scan ran, zero violations
  * 1 -- scan ran, at least one violation
  * 2 -- the scan could not run (usage error, unreadable index)
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


class StagedScanError(RuntimeError):
    """The scan could not run. Never conflated with "found nothing"."""


@dataclass(frozen=True)
class Finding:
    """One rule violation inside one staged blob."""

    label: str
    path: str
    lineno: int
    snippet: str

    def format(self) -> str:
        return f"[{self.label}]  {self.path}:{self.lineno}: {self.snippet}"


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


def _is_scannable(content: bytes) -> bool:
    """Return True iff *content* is text this scan should read.

    A NUL byte in the first kilobyte is git's own definition of binary, and
    decoding one as UTF-8 with ``errors="replace"`` would manufacture
    findings out of replacement characters. Skipping is correct rather than
    a hole: a private name is text, and no encoding turns one into a NUL.
    """
    return b"\0" not in content[:1024]


def scan_staged(repo: Path = REPO_ROOT) -> tuple[int, list[Finding]]:
    """Apply :data:`STAGED_RULES` to every staged file.

    Returns ``(scanned, findings)``. Raises :class:`StagedScanError` when
    the index holds nothing scannable, so that "I staged nothing" cannot be
    reported as "I staged something clean".
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
    scanned = 0
    for path, content in iter_staged_blobs(repo, paths):
        if content is None:
            raise StagedScanError(
                f"{path} is in the index but git could not read its blob; "
                f"refusing to report a clean scan over a partial read"
            )
        if not _is_scannable(content):
            continue
        rel = Path(path)
        text = content.decode("utf-8", "replace")
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
    return scanned, findings


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
        scanned, findings = scan_staged(Path(args.repo))
    except StagedScanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"scanned {scanned} staged file(s)")
    if not findings:
        print("OK  staged-content privacy scan: 0 violations")
        return 0

    print(f"FAIL staged-content privacy scan: {len(findings)} violation(s)")
    for finding in findings:
        print(f"  {finding.format()}")
    return 1


if __name__ == "__main__":  # pragma: no cover  (exercised via the hook)
    sys.exit(main())
