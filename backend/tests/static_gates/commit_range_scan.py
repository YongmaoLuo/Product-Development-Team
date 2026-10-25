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

Content that is not UTF-8
-------------------------
Blobs were decoded with ``errors="replace"``, and that decided the
question of what is in a blob without looking at it: an undecodable byte
becomes ``U+FFFD``. Two of the rules -- the Chinese measurement shape
(实测/观察于/复现于) and the dated changelog-bullet attribution shape --
hold non-ASCII characters, so against a blob that is not valid UTF-8 they
could never match, and the run still printed ``OK ... 0 violations``.
ASCII rules were unaffected, because ``errors="replace"`` only rewrites
bytes outside ASCII: that is why this stayed invisible, and why the same
hole in a file that had been UTF-8 all along would have shown up as a
failure instead.

A second decode does not fix it and is worth recording why. ``surrogateescape``
keeps the bytes but not their identity, so a rule pattern still does not
match; and neither does latin-1 or cp1252, because *none* of the non-ASCII
characters in these rules' patterns -- ``。``, ``（``, ``：``, ``实测`` --
is even representable in latin-1. Measured over every rule and every
encoding, no decoder other than strict UTF-8 ever returned a different
verdict. So the honest move for a rule set that is not this module's to
change is to refuse rather than to guess: a blob that is not valid UTF-8
now raises :class:`RangeScanError` naming it, which is exit 2 and the
"unreadable blob" case above.

That is a real refusal, not a formality. This module reads
``SOURCE_EXTENSIONS`` files under the scan roots, so the shape is a source
file saved in the wrong encoding rather than an image -- and the fix is to
re-save it as UTF-8 and rewrite the commit, which at pre-push time is
still free. The alternative was to keep reporting a clean result over bytes
nobody read, which is the thing this module was written to stop.
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
from test_no_plan_ids_in_metadata import (  # noqa: E402
    find_plan_ids_in_metadata,
)
from test_no_plan_ids_in_non_test_source import (  # noqa: E402
    find_plan_ids_in_non_test_source,
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
#:
#: The private-name rule used to be the fifth entry, and what it cost is
#: worth recording. Its needles were literals in
#: ``test_no_private_names_in_source.py`` -- a *tracked* file -- so the rule
#: could only be enforced by committing the names it was meant to keep out.
#: It has moved to ``scripts/scanners/scan_forbidden_names.py``, which reads
#: the operator's untracked ``.config/forbidden-names``; ``scan_commit_range.sh``
#: runs it as a separate step of the pre-push hook, and it deliberately does
#: not run in ``--ci`` mode, because a CI runner has no local list and could
#: only ever report "the check did not run".
RANGE_RULES: tuple[tuple[str, object], ...] = (
    ("home path", find_home_paths),
    ("operator attribution", find_attribution),
    ("local measurement", find_local_measurements),
    # Added after the 2026-09-29 publication. Structurally absent from the
    # three rules above: feeding the real leaked plan-id strings to them
    # returns zero findings, because a plan id is neither a path, an
    # attribution, nor a measurement.
    ("plan id in source", find_plan_ids_in_non_test_source),
)

#: ``(label, pure_rule)`` pairs applied to each commit's *message*, not to
#: its tree. Separate from :data:`RANGE_RULES` on purpose: the plan-id
#: shape is checked in source files only where the file is not a test,
#: because a date-prefixed id in a test is a fixture. On a commit message
#: there are no fixtures -- nothing under ``tests/`` is reachable from
#: one -- so the rule applies with no exemptions at all, and is a different
#: function with the same ``(path, text)`` signature. Rule 3 therefore
#: cannot simply be appended to :data:`RANGE_RULES`: doing so would flag
#: every synthetic fixture in the tree.
MESSAGE_RULES: tuple[tuple[str, object], ...] = (
    ("plan id in commit message", find_plan_ids_in_metadata),
)

#: The synthetic path recorded for a message finding. A message has no file
#: behind it; naming the surface is what lets a reader tell a message
#: violation from a tree violation in the same report.
COMMIT_MESSAGE_PATH = Path("commit message")


class RangeScanError(RuntimeError):
    """The scan could not run. Never conflated with "found nothing"."""


@dataclass(frozen=True, repr=False)
class Finding:
    """One rule violation inside one commit's snapshot.

    ``snippet`` is the matched text and it stays in memory -- the rule
    functions return it, the hit count is computed from the list length,
    and the exit code follows it. :meth:`format` is what withholds it.

    A commit is the one surface where a matched span is most likely to
    *be* the private thing: a plan id is a quoted identifier by
    definition, and a home path's username is a proper name. Printing
    either here republishes it into a terminal and a CI log.

    ``repr=False`` because the generated one interpolates every field,
    ``snippet`` included, and a ``repr`` reaches a traceback and a pytest
    assertion message as readily as ``print`` reaches a terminal.
    """

    commit: str
    subject: str
    label: str
    path: str
    lineno: int
    snippet: str

    def format(self) -> str:
        return (
            f"{self.commit[:9]}  [{self.label}]  "
            f"{self.path}:{self.lineno}: {source_scan.redact_span(self.snippet)}"
        )

    def __repr__(self) -> str:
        return f"Finding({self.format()})"


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


def _undecodable_message(path: str, exc: UnicodeDecodeError) -> str:
    """The refusal text for a blob that is not valid UTF-8.

    A hard error, and that is this module's existing contract rather than a
    new one: "an empty commit list, an unreadable blob, or a git command
    that fails is a hard error, never a clean result." A blob this module
    cannot decode is unreadable, so it belongs there.

    The wording is shared verbatim with
    :func:`staged_content_scan._undecodable_message`, for the reason the
    two modules already share the binary rule: a reader who has seen one
    has seen both.
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
        f"Re-save the file as UTF-8 and rewrite the commit that introduced "
        f"it. Nothing has been scanned and nothing has been cleared."
    )


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

    The listing is read with ``-z``. Without it git *quotes* a path that
    holds a character it considers unusual -- an em dash, a quote, a
    backslash -- and the quoted spelling is not the path: the blob spec
    built from it names nothing, ``cat-file`` answers "missing", and the
    scan refuses to report a clean result over a partial read. The NUL
    form is the raw path, with the entries separated by a byte that cannot
    occur in a name, so nothing has to be unescaped and nothing can be
    truncated at a space.
    """
    tree = _git(repo, ["ls-tree", "-r", "-z", sha])
    paths: list[Path] = []
    for entry in tree.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
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
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RangeScanError(
                _undecodable_message(f"{sha[:9]}:{rel.as_posix()}", exc)
            ) from exc
        yield rel.as_posix(), text


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


def scan_commit_message(
    repo: Path, sha: str, message: str, subject: str = ""
) -> list[Finding]:
    """Apply every rule in :data:`MESSAGE_RULES` to one commit message.

    The message is a separate surface from the tree and is scanned
    separately: it survives into ``main`` verbatim through GitHub's merge
    commit, and no tree rule reads it.
    """
    findings: list[Finding] = []
    for label, rule in MESSAGE_RULES:
        for _, lineno, snippet in rule(COMMIT_MESSAGE_PATH, message):  # type: ignore[operator]
            findings.append(
                Finding(
                    commit=sha,
                    subject=subject,
                    label=label,
                    path=COMMIT_MESSAGE_PATH.as_posix(),
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


def commit_messages(repo: Path, shas: Iterable[str]) -> dict[str, str]:
    """Map each SHA to its whole message, subject and body.

    ``%B`` rather than ``%s``: a plan id quoted in the body is as permanent
    as one in the subject, and the body is where a long incident note ends
    up. ``commit_subjects`` reads ``%s`` for display only.
    """
    messages: dict[str, str] = {}
    for sha in shas:
        out = _git(repo, ["log", "-1", "--format=%B", sha])
        messages[sha] = out
    return messages


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
    messages = commit_messages(repo, ordered)
    findings: list[Finding] = []
    for sha in ordered:
        subject = subjects.get(sha, "")
        findings.extend(scan_commit(repo, sha, subject))
        findings.extend(scan_commit_message(repo, sha, messages.get(sha, ""), subject))
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
