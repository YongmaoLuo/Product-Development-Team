"""Apply the privacy rules to every commit in a range, not just the tree.

Why this module exists
----------------------
Every privacy gate in ``static_gates/`` reads the **current tree**: it walks
``source_scan.iter_first_party_sources()`` and inspects what is on disk now.
That answers "is the repository clean today", which is not the same question
as "did we publish something we should not have".

A commit that adds a private identifier and a later commit that removes it
leaves the tree clean and the history dirty. The tree gates pass. And the
dirty commit is not gone: git keeps it, ``refs/pull/<N>/head`` keeps it, and
anyone can fetch it by SHA without authenticating. Squash-merging the pull
request does not remove it either -- squash changes what lands on the
default branch, not what stays reachable through the pull request ref.

So the unit of scanning here is the **commit**, not the diff and not the
tree. For a range ``A..B`` every commit in it is inspected at its own
snapshot. A violation anywhere in the range is a failure, even when a later
commit in the same range fixed it -- because the intermediate snapshot is
still fetchable.

Fixing a failure means **rewriting the offending commit**, not adding a
fixup: ``git commit --amend`` / ``git rebase -i`` and force-push the branch.
While the branch is unmerged that costs nothing, which is exactly why the
scan runs before the push.

How a commit's files are read
-----------------------------
No checkout, no temporary directory. Per commit:

1. ``git ls-tree -r <sha>`` once, to enumerate the tree;
2. filter with :func:`source_scan.is_first_party_path` -- the same scope
   the on-disk walker expresses by starting at the scan roots, so the two
   scans agree on which files are in play;
3. ``git cat-file --batch`` once, streaming every blob back in a single
   subprocess. Feeding the specs on stdin and reading the responses in
   order avoids one process per file.

The rules themselves are not re-implemented here. They are the *pure*
functions the tree gates already export -- ``find_home_paths``,
``find_attribution``, ``find_local_measurements`` -- each of which takes
``(path, text)`` and returns findings. Feeding them a commit's text costs
nothing and guarantees the two scans cannot disagree about what a violation
is.

Failure is loud
---------------
An empty commit list, an unreadable blob, or a git command that fails is a
hard error, never a clean result. A range scanner that silently reports
"0 violations" when it in fact scanned nothing is the failure mode this
whole module exists to prevent.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_scan  # noqa: E402  (path set above)

from test_no_local_home_path_in_first_party import (  # noqa: E402
    find_home_paths,
)
from test_no_operator_attribution_in_source import (  # noqa: E402
    find_attribution,
)
from test_source_comments_carry_no_local_measurements import (  # noqa: E402
    find_local_measurements,
)

#: Repository root -- this file lives at
#: ``backend/tests/static_gates/commit_range_scan.py``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``(label, pure_rule)`` pairs. Every rule has the signature
#: ``(path: Path, text: str) -> list[tuple[Path, int, str]]``, which is why
#: this is data and not code. Adding a rule here is the whole change; there
#: is no second place to teach about it.
RANGE_RULES: tuple[tuple[str, object], ...] = (
    ("home path", find_home_paths),
    ("operator attribution", find_attribution),
    ("local measurement", find_local_measurements),
)


class RangeScanError(RuntimeError):
    """The scan could not run. Never conflated with "found nothing"."""


@dataclass(frozen=True)
class Finding:
    """One rule violation inside one commit's snapshot."""

    commit: str
    subject: str
    label: str
    path: str
    lineno: int
    snippet: str

    def format(self) -> str:
        return (
            f"{self.commit[:9]}  [{self.label}]  "
            f"{self.path}:{self.lineno}: {self.snippet}"
        )


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(repo: Path, args: Sequence[str]) -> str:
    """Run a git command and return stdout, or raise :class:`RangeScanError`.

    ``check=False`` plus an explicit raise (rather than ``check=True``) so
    the error message can name the command. A bare ``CalledProcessError``
    from deep inside the scan is not actionable.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RangeScanError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout.decode("utf-8", "replace")


def iter_range_commits(repo: Path, range_spec: str) -> list[str]:
    """Return the SHAs in *range_spec*, oldest first.

    *range_spec* is any revision expression git understands -- ``A..B``,
    ``HEAD~3..HEAD`` -- and may contain spaces (``<sha> --not --remotes``,
    which is how a branch new to the remote is described). ``--reverse``
    so the reported order matches the order the commits were written,
    which is the order a reader wants when fixing them.
    """
    spec = shlex.split(range_spec)
    if not spec:
        raise RangeScanError("empty range specification")
    out = _git(repo, ["rev-list", "--reverse", *spec])
    return [line.strip() for line in out.splitlines() if line.strip()]


def iter_commit_sources(repo: Path, sha: str) -> Iterator[tuple[str, str]]:
    """Yield ``(relative_path, text)`` for every first-party file in *sha*.

    Blobs are read in one ``git cat-file --batch`` invocation. ``ls-tree``
    is parsed rather than asked for names only, so that non-blob entries --
    submodule commits, nested trees -- are dropped by *type* instead of
    being fed to ``cat-file`` and failing.
    """
    tree = _git(repo, ["ls-tree", "-r", sha])
    paths: list[Path] = []
    for line in tree.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or fields[1] != "blob":
            continue
        rel = Path(path)
        # ``is_first_party_path`` -- the composition that also requires the
        # path to sit under a scan root. ``ls-tree`` lists the whole tree,
        # so without the root test this scan would reach files no other
        # gate checks (``SECURITY_AUDIT.md``, ``docs/``) and report
        # violations the rest of the suite does not recognise.
        if not source_scan.is_first_party_path(rel):
            continue
        paths.append(rel)

    if not paths:
        return

    specs = [f"{sha}:{rel.as_posix()}" for rel in paths]
    blobs = _read_blob_batch(repo, specs)

    for rel, blob in zip(paths, blobs):
        # ``None`` means git answered "<spec> missing". Inside a tree that
        # ``ls-tree`` just listed that cannot legitimately happen, so it is
        # treated as an error rather than skipped -- a silently skipped
        # file is an unscanned file.
        if blob is None:
            raise RangeScanError(
                f"{sha[:9]}:{rel.as_posix()} is listed by ls-tree but has no "
                f"blob; refusing to report a clean scan over a partial read"
            )
        yield rel.as_posix(), blob.decode("utf-8", "replace")


def _read_blob_batch(repo: Path, specs: list[str]) -> list[bytes | None]:
    """Read *specs* through one ``git cat-file --batch``.

    The protocol is, per input line, either::

        <oid> SP <type> SP <size> LF <size bytes> LF

    or, when the object is absent::

        <input> SP missing LF

    Responses come back in input order, so the result is positional. Parsing
    stops only when the output is exhausted; anything else -- a truncated
    header, a size that runs past the end of the buffer -- raises rather
    than yielding a short list, because a short list would silently reduce
    the number of files scanned.
    """
    if not specs:
        return []
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        # Bytes, not str: stdout is consumed as bytes and decoded per blob
        # (a file that is not valid UTF-8 must not abort the whole scan),
        # and mixing a text stdin with a binary stdout is a TypeError.
        input=("\n".join(specs) + "\n").encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RangeScanError(
            "git cat-file --batch failed (exit "
            f"{result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )

    out = result.stdout
    blobs: list[bytes | None] = []
    pos = 0
    total = len(out)
    while pos < total and len(blobs) < len(specs):
        newline = out.find(b"\n", pos)
        if newline == -1:
            raise RangeScanError("git cat-file --batch: unterminated header")
        header = out[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        if header.endswith(" missing"):
            blobs.append(None)
            continue
        fields = header.rsplit(" ", 2)
        if len(fields) != 3 or not fields[2].isdigit():
            raise RangeScanError(
                f"git cat-file --batch: unrecognised header {header!r}"
            )
        size = int(fields[2])
        end = pos + size
        if end > total:
            raise RangeScanError(
                f"git cat-file --batch: blob at offset {pos} claims {size} "
                f"bytes but only {total - pos} remain"
            )
        blobs.append(out[pos:end])
        pos = end + 1  # skip the trailing LF

    if len(blobs) != len(specs):
        raise RangeScanError(
            f"git cat-file --batch returned {len(blobs)} objects for "
            f"{len(specs)} requests; refusing to scan a partial read"
        )
    return blobs


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_commit(repo: Path, sha: str, subject: str = "") -> list[Finding]:
    """Apply every rule in :data:`RANGE_RULES` to one commit's snapshot."""
    findings: list[Finding] = []
    for rel, text in iter_commit_sources(repo, sha):
        for label, rule in RANGE_RULES:
            for _, lineno, snippet in rule(Path(rel), text):  # type: ignore[operator]
                findings.append(
                    Finding(
                        commit=sha,
                        subject=subject,
                        label=label,
                        path=rel,
                        lineno=lineno,
                        snippet=snippet,
                    )
                )
    return findings


def commit_subjects(repo: Path, shas: Iterable[str]) -> dict[str, str]:
    """Map each SHA to its one-line subject, for readable failure output."""
    subjects: dict[str, str] = {}
    for sha in shas:
        out = _git(repo, ["log", "-1", "--format=%s", sha])
        subjects[sha] = out.strip()
    return subjects


def scan_ranges(
    range_specs: Sequence[str], repo: Path = REPO_ROOT
) -> tuple[list[str], list[Finding]]:
    """Scan every commit in every spec, de-duplicating across specs.

    Returns ``(shas, findings)``. Raises :class:`RangeScanError` when a spec
    resolves to no commits -- an empty range means the scan proved nothing,
    and reporting that as success is the vacuous pass this module is built
    to avoid. A caller that legitimately has nothing to scan (an up-to-date
    push) should not call this at all.

    A spec may cover commits another spec already covered (pushing two refs
    that share history); each commit is scanned once.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for spec in range_specs:
        shas = iter_range_commits(repo, spec)
        if not shas:
            raise RangeScanError(
                f"range {spec!r} resolves to no commits; nothing to scan. "
                f"This is reported as an error rather than a pass, because a "
                f"scan that inspected nothing must not look like a clean one."
            )
        for sha in shas:
            if sha not in seen:
                seen.add(sha)
                ordered.append(sha)

    subjects = commit_subjects(repo, ordered)
    findings: list[Finding] = []
    for sha in ordered:
        findings.extend(scan_commit(repo, sha, subjects.get(sha, "")))
    return ordered, findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commit_range_scan",
        description=(
            "Apply the privacy rules to every commit in a range. Exits 0 "
            "clean, 1 on violations, 2 when the scan could not run."
        ),
    )
    parser.add_argument(
        "--range",
        dest="ranges",
        action="append",
        default=[],
        metavar="SPEC",
        help="revision range (repeatable), e.g. origin/main..HEAD",
    )
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT),
        help="repository to scan (default: this checkout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.ranges:
        print("ERROR: at least one --range is required", file=sys.stderr)
        return 2
    try:
        shas, findings = scan_ranges(args.ranges, Path(args.repo))
    except RangeScanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"scanned {len(shas)} commit(s)")
    if not findings:
        print("OK  commit-range privacy scan: 0 violations")
        return 0

    print(f"FAIL commit-range privacy scan: {len(findings)} violation(s)")
    for finding in findings:
        print(f"  {finding.format()}")
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the shell script
    sys.exit(main())
