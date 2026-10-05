"""Apply the privacy rules to what a pull request will contribute to history.

Why this module exists
----------------------
The rules in ``static_gates/`` and in :mod:`commit_range_scan` both read
things a *commit* carries: the tree, and the commits in a range. A pull
request contributes a fourth thing, and on this repository it is the one
that survives into the default branch's message:

    Merge pull request #27 from YongmaoLuo/docs/audit-state-the-reason-not-the-history

    docs(audit): 把断言被绕过的经过改成它成立的理由

    <pull request body, when there is one>

GitHub composes that message itself. Every word of it becomes a commit on
``main`` that is permanent, public, and not amendable without rewriting
history. **The branch name and the pull request title are inside that
message, and no other gate in this repository reads either one.** A branch
called ``fix/whatever`` or a description that quotes a local path passes
every tree gate, every range gate, and the AI-attribution check, and then
lands in ``main`` verbatim.

The rules are not re-implemented. They are the same pure functions the tree
and range gates use -- ``find_home_paths``, ``find_attribution``,
``find_local_measurements`` -- each taking ``(path, text)`` and returning
findings. Four surfaces, one definition of "a violation".

What this cannot do
-------------------
It cannot tell that a branch name *refers to another project*. The
identifiers that would make that decidable are the leak: writing
``frontend-echarts`` into this repository to blacklist it publishes the
private project name inside the blacklist, which is the exact shape the
rule exists to prevent. So a branch named after an unrelated project is
caught by a human reading the pull request -- which is why the check
belongs **before** the merge rather than after it, where it is no longer
actionable at any price.

Failure is loud
---------------
An unset environment variable is an error, not a clean result, for the same
reason :mod:`commit_range_scan` refuses an empty range: a scan that read
nothing must not look like one that found nothing. A variable that is *set
and empty* is different -- an empty pull request body genuinely contains
nothing -- and is reported as scanned.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_no_local_home_path_in_first_party import (  # noqa: E402
    find_home_paths,
)
from test_no_operator_attribution_in_source import (  # noqa: E402
    find_attribution,
)
from test_source_comments_carry_no_local_measurements import (  # noqa: E402
    find_local_measurements,
)

#: The pull-request fields that end up in the merge commit message, as
#: ``(env var, synthetic path, required)``. ``required`` distinguishes
#: "the plumbing failed" from "the field was left blank on purpose".
PR_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("PR_HEAD_REF", "pull_request/head.ref", True),
    ("PR_TITLE", "pull_request/title", True),
    ("PR_BODY", "pull_request/body", False),
)

#: ``(label, pure_rule)`` pairs -- the same table shape and the same rules
#: :mod:`commit_range_scan` uses, so the two scans cannot drift apart.
METADATA_RULES: tuple[tuple[str, object], ...] = (
    ("home path", find_home_paths),
    ("operator attribution", find_attribution),
    ("local measurement", find_local_measurements),
)


class MetadataScanError(RuntimeError):
    """The scan could not run. Never conflated with "found nothing"."""


@dataclass(frozen=True)
class Finding:
    """One rule violation in one pull-request field."""

    field: str
    label: str
    lineno: int
    snippet: str

    def format(self) -> str:
        return f"[{self.label}]  {self.field}:{self.lineno}: {self.snippet}"


def scan_text(path: str, text: str) -> list[Finding]:
    """Apply every rule in :data:`METADATA_RULES` to one field's text."""
    findings: list[Finding] = []
    for label, rule in METADATA_RULES:
        for _, lineno, snippet in rule(Path(path), text):  # type: ignore[operator]
            findings.append(
                Finding(field=path, label=label, lineno=lineno, snippet=snippet)
            )
    return findings


def scan_fields(
    fields: Sequence[tuple[str, str]],
) -> tuple[int, list[Finding]]:
    """Scan ``(label, text)`` pairs. Returns ``(scanned, findings)``.

    *fields* is ``(field_label, text)``; a separate argument from the
    env-var table so a caller can scan a payload it built itself.
    """
    findings: list[Finding] = []
    for field, text in fields:
        findings.extend(scan_text(field, text))
    return len(fields), findings


def fields_from_env(environ: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Read the pull-request fields named by :data:`PR_FIELDS`.

    Raises :class:`MetadataScanError` when a *required* variable is
    absent. Unset-and-required is the signature of broken plumbing --
    a renamed workflow step, a dropped ``env:`` block -- and reporting
    that as a clean scan is the vacuous pass this module exists to avoid.
    A variable that is set and empty is legitimate and is not an error.
    """
    env = os.environ if environ is None else environ
    fields: list[tuple[str, str]] = []
    for var, path, required in PR_FIELDS:
        if var not in env:
            if required:
                raise MetadataScanError(
                    f"{var} is not set. The pull request's branch name and "
                    f"title are composed into the merge commit message, so "
                    f"scanning without them would check nothing and report "
                    f"success. Wire the job's `env:` to "
                    f"github.event.pull_request.head.ref / .title / .body."
                )
            continue
        fields.append((path, env[var]))
    return fields


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr_metadata_scan",
        description=(
            "Apply the privacy rules to a pull request's branch name, "
            "title and body. Exits 0 clean, 1 on violations, 2 when the "
            "scan could not run."
        ),
    )
    parser.add_argument(
        "--text",
        dest="pairs",
        action="append",
        default=[],
        metavar="LABEL=TEXT",
        help=(
            "scan an arbitrary label/text pair instead of reading the "
            "environment (repeatable); used by the tests"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.pairs:
        fields = []
        for pair in args.pairs:
            label, sep, text = pair.partition("=")
            if not sep:
                print(
                    f"ERROR: --text expects LABEL=TEXT, got {pair!r}",
                    file=sys.stderr,
                )
                return 2
            fields.append((label, text))
    else:
        try:
            fields = fields_from_env()
        except MetadataScanError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if not fields:
        print(
            "ERROR: nothing to scan; refusing to report a clean result "
            "over an empty input",
            file=sys.stderr,
        )
        return 2

    scanned, findings = scan_fields(fields)
    print(f"scanned {scanned} pull request field(s)")
    if not findings:
        print("OK  pull request metadata privacy scan: 0 violations")
        return 0

    print(f"FAIL pull request metadata privacy scan: {len(findings)} violation(s)")
    for finding in findings:
        print(f"  {finding.format()}")
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the CI job
    sys.exit(main())
