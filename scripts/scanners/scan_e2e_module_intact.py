"""Structural survival scan for the whole-delivery E2E module.

Background
----------
``backend/tests/e2e/test_keychain_full_delivery_macos.py`` is the largest
file in the suite, and it has been destroyed three separate times by a work
report being written into it in place of its source. Each time a handful
of lines of prose stood where several thousand lines of test had been::

    34 lines replacing 3458
    20 lines replacing ~2908
     5 lines replacing 2368

Those readings are not the output of a command anyone can re-run, and no
committed file records them. Each is what ``git diff HEAD`` showed for the
module while it was clobbered: the working tree held 34, 20, and 5 lines
where the commit held 3458, ~2908, and 2368. They are quoted as the shape of
the failure rather than as a measurement anything can reproduce. Nothing in
the thresholds below depends on their exact values — those are floors derived
from HEAD at run time, so a module that legitimately grows or shrinks does
not need this paragraph rewritten.

Every occurrence had the same four-part signature, and all four are cheap
to check:

1. the module's first statement is no longer a docstring — report prose
   parses as some other statement, or does not parse at all;
2. the header carries report markers (``Full diff:``, an insertions /
   deletions count, a ``FILE:`` line) where the docstring belongs;
3. the line count collapses far below the committed copy;
4. the module stops collecting the cases it used to.

The first three are static and are what this scanner reports. The fourth
needs pytest and is driven by ``scripts/check_e2e_module_intact.sh``, which
calls this module for the static half and runs the collection comparison
itself.

Why a scanner and not a third static gate
-----------------------------------------
"the module parses" is already pinned twice: two gates under
``tests/static_gates/`` AST-parse this very module to read
``_STRICT_SWITCH_ENV`` from it. "the file is complete" is pinned by the
E2E tests themselves, once the module is collectible. A third committed
gate would re-assert a property the suite already holds twice and would
move the static-gate counts without adding coverage. This is therefore
command-level: something the plan runs at the end, not a file the suite
collects.

The line-count check is a floor, not an exact count
---------------------------------------------------
Legitimate edits to this module's docstring move the line number, and a
check that broke on those would be a check people switch off. So the bar
is a floor well under the committed count. The floor is expressed as an
absolute default *and* cross-checked against the committed copy, so a tree
whose real size has drifted is still caught.

Usage
-----
::

    python3 scripts/scanners/scan_e2e_module_intact.py
    python3 scripts/scanners/scan_e2e_module_intact.py --target path/to.py
    python3 scripts/scanners/scan_e2e_module_intact.py --quiet

Exit codes
----------
* ``0`` — the module looks intact.
* ``1`` — at least one check fired; the module was overwritten.
* ``2`` — the scan could not run (target missing, no git, no HEAD commit).

Read-only
---------
This scanner never writes to the module and never repairs it. If a check
fires, the correct response is to restore the file from ``HEAD`` and report
— editing the module until the scan goes quiet would destroy exactly the
evidence the scan exists to preserve.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: The module under guard, relative to the project root.
TARGET_REL = "backend/tests/e2e/test_keychain_full_delivery_macos.py"

#: The committed docstring runs to tens of thousands of characters. 200 is a
#: deliberately low bar: it still rejects every observed corruption, all of
#: which replaced the docstring with prose short enough to fall under it,
#: while a legitimate edit cannot plausibly shorten it past the mark.
MIN_DOCSTRING_CHARS = 200

#: Floor on the line count. The committed copy is ~3458 lines and each
#: observed corruption replaced it with fewer than 40.
MIN_LINES = 3000

#: A tracked file that lost this many lines against HEAD has been clobbered.
#: No edit in this plan legitimately removes hundreds of lines from one file.
MASS_DELETION_LINES = 200

#: Report markers that appeared where the module docstring belongs. The first
#: line of a ``git diff --stat`` block reads ``N files changed, M
#: insertions(+), K deletions(-)``, and a report quoted verbatim starts
#: ``FILE:``; all four are what the corruption put in the header.
HEADER_MARKERS = ("Full diff:", "insertions,", "deletions(-")
HEADER_LINES = 5

#: Files this plan intentionally rewrites. A mass deletion in one of these
#: still gets reported — the list of exceptions is here so the message can
#: say whether the loss was expected, not so it can be waved through.
KNOWN_HEAVILY_EDITED = (TARGET_REL,)


class ScanError(RuntimeError):
    """The scan could not run — distinct from "the scan found something"."""


def _git(*args: str) -> str:
    """Run a git command against the project root and return its stdout."""
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ScanError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout


def check_docstring(source: str, min_chars: int) -> tuple[list[str], dict[str, object]]:
    """The first statement must be a docstring, and it must be substantial."""
    problems: list[str] = []

    # Report prose standing where the module source belongs does not always
    # parse — ``Full diff:`` is not Python. A SyntaxError here is a
    # corruption finding, not a scanner failure, so it is caught and reported
    # rather than allowed to escape as a traceback: a guard that crashes on
    # the exact input it exists to catch reports nothing about that input.
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return (
            [f"module does not parse: {exc.msg} at line {exc.lineno}"],
            {"first_node": "UNPARSEABLE", "docstring_chars": 0},
        )

    first = tree.body[0] if tree.body else None

    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        docstring = first.value.value
    else:
        docstring = ""
        problems.append(
            "first statement is not a docstring "
            f"(it is a {type(first).__name__ if first is not None else 'missing module body'})"
        )

    if len(docstring) <= min_chars:
        problems.append(
            f"docstring is {len(docstring)} chars, at or below the {min_chars} floor"
        )

    evidence = {
        "first_node": type(first).__name__ if first is not None else "EMPTY",
        "docstring_chars": len(docstring),
    }
    return problems, evidence


def check_header(lines: list[str]) -> list[str]:
    """No report markers in the header, where the docstring belongs."""
    header = "\n".join(lines[:HEADER_LINES])
    found = [marker for marker in HEADER_MARKERS if marker in header]
    if header and lines[0].startswith("FILE:"):
        found.append("FILE:")
    if not found:
        return []
    return [
        "report prose in the first "
        f"{HEADER_LINES} lines: {', '.join(repr(m) for m in found)}"
    ]


def check_line_count(
    lines: list[str], head_lines: int | None, min_lines: int
) -> tuple[list[str], dict[str, object]]:
    """The line count must sit above the floor, not merely be nonzero."""
    count = len(lines)
    evidence: dict[str, object] = {"lines": count, "floor": min_lines}

    if head_lines is not None:
        evidence["head_lines"] = head_lines
        # The committed copy is the scale. A floor fixed at write time goes
        # stale the moment the module legitimately grows or shrinks, so the
        # committed count is checked alongside the absolute floor: neither
        # one alone is enough — an absolute floor misses a tree that shrank
        # wholesale, and a relative check misses a baseline that was itself
        # committed damaged.
        relative_floor = int(head_lines * 0.8)
        if count <= relative_floor:
            problems = [
                f"line count collapsed: {count} lines, at or below 80% of the "
                f"committed {head_lines}"
            ]
            if count <= min_lines:
                problems.append(
                    f"line count is also at or below the absolute floor {min_lines}"
                )
            return problems, evidence
        evidence["relative_floor"] = relative_floor

    if count <= min_lines:
        return [f"line count collapsed: {count} lines (floor {min_lines})"], evidence

    return [], evidence


def check_no_mass_deletion(
    threshold: int, known_heavy: tuple[str, ...] = KNOWN_HEAVILY_EDITED
) -> tuple[list[str], str]:
    """No tracked file in the tree lost a block of lines against HEAD.

    The same clobbering has been observed in files other than the module
    under guard, so a scan that watched only that one file would miss an
    occurrence elsewhere.
    """
    numstat = _git("diff", "--numstat", "HEAD").strip()
    if not numstat:
        return [], ""

    massive: list[str] = []
    for row in numstat.splitlines():
        fields = row.split("\t")
        if len(fields) < 3:
            continue
        _, deleted, path = fields[0], fields[1], fields[2].strip('"')
        if not deleted.isdigit() or int(deleted) < threshold:
            continue
        note = " (a file this plan rewrites)" if path in known_heavy else ""
        massive.append(f"{path} (-{deleted} lines){note}")

    return massive, numstat


def scan(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    if not target.is_file():
        raise ScanError(f"module under guard not found: {target}")

    source = target.read_text(encoding="utf-8")
    lines = source.splitlines()

    # The committed line count, when there is a committed copy to compare to.
    head_lines = None
    try:
        head_lines = len(
            _git("show", f"HEAD:{args.target}").splitlines()
        )
    except ScanError as exc:
        print(f"warning: {exc}", file=sys.stderr)

    problems: list[str] = []

    docstring_problems, docstring_evidence = check_docstring(
        source, args.min_docstring_chars
    )
    problems.extend(docstring_problems)

    problems.extend(check_header(lines))

    count_problems, count_evidence = check_line_count(
        lines, head_lines, args.min_lines
    )
    problems.extend(count_problems)

    massive, numstat = check_no_mass_deletion(args.mass_deletion_lines)

    # Evidence first, verdict second. A guard whose result is asserted but
    # not quoted is not evidence, so every measurement is printed whether or
    # not the scan failed.
    rel = _display_path(target)
    print(f"module: {rel}")
    print(
        "  first_node={first_node} docstring_chars={docstring_chars}".format(
            **docstring_evidence
        )
    )
    print(
        "  lines={lines} floor={floor}".format(**{k: count_evidence[k] for k in ("lines", "floor")})
    )
    if "head_lines" in count_evidence:
        print(
            "  head_lines={head_lines} relative_floor={relative_floor}".format(
                **count_evidence
            )
            if "relative_floor" in count_evidence
            else "  head_lines={head_lines}".format(**count_evidence)
        )

    if numstat:
        print("  git diff --numstat HEAD:")
        for row in numstat.splitlines():
            print(f"    {row}")
    else:
        print("  git diff --numstat HEAD: (working tree identical to HEAD)")

    for problem in problems:
        print(f"  problem: {problem}", file=sys.stderr)

    if problems:
        print(
            f"error: {len(problems)} problem(s) — {rel} does not look like the "
            "committed module",
            file=sys.stderr,
        )

    if massive:
        print(
            f"error: {len(massive)} tracked file(s) lost "
            f"{args.mass_deletion_lines} or more lines against HEAD:",
            file=sys.stderr,
        )
        for entry in massive:
            print(f"  {entry}", file=sys.stderr)

    if problems or massive:
        return 1

    print(f"OK {rel} is intact")
    return 0


def _display_path(path: Path) -> str:
    """Print the module relative to the project root when it is inside it."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that the whole-delivery E2E module was not overwritten "
            "by a work report. Read-only; never repairs the file."
        )
    )
    parser.add_argument(
        "--target",
        default=TARGET_REL,
        help="module to check, relative to the project root (default: %(default)s)",
    )
    parser.add_argument(
        "--min-lines",
        type=int,
        default=MIN_LINES,
        help="absolute floor on the line count (default: %(default)s)",
    )
    parser.add_argument(
        "--min-docstring-chars",
        type=int,
        default=MIN_DOCSTRING_CHARS,
        help="floor on the module docstring length (default: %(default)s)",
    )
    parser.add_argument(
        "--mass-deletion-lines",
        type=int,
        default=MASS_DELETION_LINES,
        help="deletions against HEAD that count as a mass deletion (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return scan(args)
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
