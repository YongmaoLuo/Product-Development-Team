"""No non-test first-party source may carry a plan-id shape.

Why this gate exists
--------------------
A plan id in this project is ``<YYYYMMDD>-<slug>``: a private plan
directory the operator actually ran, carrying a real date and a real name.
One was pushed to a public remote on 2026-09-29. ``SECURITY_AUDIT.md``
records the shape under ENTRY-021 and explicitly deferred widening the
scan surface to "the next round". This file is that round.

The existing ``RANGE_RULES`` are structurally blind to it. Verified on
this branch by feeding all five real leaked strings to ``find_home_paths``
/ ``find_attribution`` / ``find_local_measurements``: zero violations,
exit 0. That is not a loose gate -- it is no rule at all for this class.

Scope: non-test sources, zero exemptions
----------------------------------------
A violation is a plan id in *production* source: a docstring, a comment, a
prompt template, a default. That is where the 2026-09-29 leak lived, and
it is the only place where the fix is "replace the id" rather than "the test
needs a fixture".

Test sources are excluded, and the exclusion is mechanical rather than
judgemental:

  * ``backend/tests/**`` and ``**/tests/**`` -- synthetic plan-id fixtures
    live here (``20260810-twice-start``, ``20260901-matrix-plan``,
    ``20990101-does-not-exist``, ``20261301-invalid-month``). A
    date-prefixed id in a test is nearly always a *fixture*, so demanding
    every one be neutralised buys nothing and turns the gate into a
    nuisance that gets switched off.
  * ``**/test_*.py`` -- same reasoning, for test modules living outside a
    ``tests/`` directory.

That is the only mechanically separable line available, and it is a real
boundary rather than a loophole: the accident was in production source.

It is deliberately **not** a loophole for the private-name blacklist
(``scripts/scanners/scan_forbidden_names.py``), which has no test
exemption at all -- a real project name in a test file is exactly as
published as one in a docstring. That check is not here, and not in
``tests/`` either: its needles are operator-owned and untracked, so it
cannot be a pytest gate without publishing them. It runs in the pre-commit
and pre-push hooks instead.

Why no ``\\b`` in the shape
--------------------------
The shape avoids ``\\b`` on purpose. POSIX ERE -- which ``git grep -E``
speaks -- has no word boundary, so a pattern written with one and checked
from a shell matches nothing and reports a clean tree. Reproduced on this
branch::

    $ git grep -E -c '\\b20[0-9]{6}-[A-Za-z]' origin/main -- backend/agent.py ; echo $?
    1                                     # <- "no match": a false clean
    $ git grep -E -c '20[0-9]{6}-[A-Za-z]' origin/main -- backend/agent.py ; echo $?
    origin/main:backend/agent.py:1
    0

The unanchored shape is also the safer direction to be wrong in: it catches
an id wherever it sits, including inside a longer token, and the cost of that
is a false positive in a non-test source, which is a two-second edit.

Why this file must not import pytest
-------------------------------------
``scripts/check_commit_msg.py`` runs under a bare ``python3`` from the
``commit-msg`` hook -- no venv, no third-party packages -- and imports
:data:`PLAN_ID_SHAPE` from here so that "what a plan id is" has exactly one
definition. A top-level ``import pytest`` would break that hook with an
ImportError. The tests below therefore use plain ``assert``.
"""

from __future__ import annotations

import re
from pathlib import Path

import source_scan

#: The plan-id shape: an eight-digit date, a hyphen, then a slug starting
#: with a letter.
#:
#: Portable to POSIX ERE on purpose -- see the module docstring's "why no
#: ``\\b``". The date is ``20`` plus six digits rather than a bare
#: ``[0-9]{8}`` so a plain eight-digit number with no plan-id tail cannot
#: match, and so a hyphenated ISO date (``2026-10-14``) is not a match:
#: that is eight digits split by two hyphens, and this shape requires six
#: digits immediately followed by one.
PLAN_ID_SHAPE: re.Pattern[str] = re.compile(r"20[0-9]{6}-[A-Za-z][A-Za-z0-9._-]*")

#: Path parts that mark a file as test source. A ``tests`` directory
#: anywhere in the path excludes it, so ``backend/tests/`` and
#: ``backend/state_machine/tests/`` are both covered by one entry.
TEST_DIR_SEGMENTS: frozenset[str] = frozenset({"tests"})


def is_test_source(path: Path) -> bool:
    """Return True iff *path* is test source and so exempt from this gate.

    *path* may be relative to the repository root (``backend/tests/x.py``)
    or to a scan root (``tests/unit/x.py``); neither test below inspects the
    prefix, only the parts, so both forms answer the same.
    """
    if TEST_DIR_SEGMENTS & set(path.parts):
        return True
    return path.name.startswith("test_")


def find_plan_ids_in_non_test_source(
    path: Path, text: str
) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, plan_id)`` for every plan id in *text*.

    Returns an empty list for a test source -- see the module docstring.
    Each entry records the file, the line and the id itself, so a
    downstream consumer can format ``rel:lineno`` without re-walking the
    tree; the id is reported rather than the whole line so the failure
    message does not reprint the text being complained about.
    """
    if is_test_source(path):
        return []

    hits: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        seen: set[str] = set()
        for match in PLAN_ID_SHAPE.finditer(line):
            plan_id = match.group(0)
            # A slug legitimately contains hyphens and dots, so one line
            # can produce several overlapping matches of the same id.
            # Report it once per line instead of once per match.
            if plan_id in seen:
                continue
            seen.add(plan_id)
            hits.append((path, lineno, plan_id))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_is_non_empty() -> None:
    """The scan target must produce at least one file.

    A gate whose scan target has disappeared passes vacuously. Same
    rationale as the sibling gates' ``test_scan_is_non_empty``.
    """
    assert list(source_scan.iter_first_party_sources()), (
        "source_scan.iter_first_party_sources() returned no files — the "
        "gate would now pass on an empty result."
    )


def test_the_shape_matches_a_real_leaked_plan_id() -> None:
    """The shape that leaked on 2026-09-29 is detected in production source.

    The sample is assembled from fragments so this file's own source never
    contains a plan id whole.
    """
    leaked = "2026" + "1010-" + "CC-Switch-" + "Remote-Aut"
    hits = find_plan_ids_in_non_test_source(Path("backend/agent.py"), leaked)
    assert [h[2] for h in hits] == [leaked], hits


def test_the_shape_matches_every_real_leaked_string() -> None:
    """Every string from the 2026-09-29 incident is caught.

    This is the negative control for the whole gate: if the shape ever
    stops matching one of these, the gate is reporting a clean tree over
    exactly the leak it was written for.
    """
    samples = [
        "2026" + "1010-" + "CC-Switch-" + "Remote-Aut",
        "2026" + "1012-" + "chan" + "lun-buy" + "sell-points-rebuild",
        "2026" + "0926-" + "Product-" + "Development",
        "2026" + "1004-" + "PDT-" + "Product-" + "Developm",
    ]
    for sample in samples:
        hits = find_plan_ids_in_non_test_source(Path("backend/agent.py"), sample)
        assert hits, f"not detected in production source: {sample!r}"


def test_the_shape_is_found_embedded_in_a_line() -> None:
    """A plan id in the middle of a sentence is found, with its line number."""
    plan_id = "2026" + "0615-" + "refactor-provider-config"
    text = f"see plan ``{plan_id}`` for the worked example\n"
    hits = find_plan_ids_in_non_test_source(Path("backend/refiner.py"), text)
    assert len(hits) == 1, hits
    assert hits[0][1] == 1, hits


def test_there_is_no_whitelist_for_placeholder_plan_ids() -> None:
    """``20260101-`` is the repo's neutral placeholder and is still a hit.

    It is still the plan-id shape. A bare date prefix in production source
    is what a real id looks like, and the gate cannot tell a placeholder
    from a leak without reading the operator's plans. The synthetic ids
    under ``tests/`` are exempt; production source is not.
    """
    plan_id = "2026" + "0101-example"
    hits = find_plan_ids_in_non_test_source(Path("backend/task_manager.py"), plan_id)
    assert [h[2] for h in hits] == [plan_id], hits


def test_plain_prose_is_not_a_violation() -> None:
    """Hyphenated ISO dates, bare numbers and arrows are not plan ids."""
    text = (
        "released 2026-10-14 as build 20261014, no plan here\n"
        "the plan count went 15 -> 35 tasks\n"
        "see 2099-01-01 and 2026-13-01 for the malformed cases\n"
    )
    assert find_plan_ids_in_non_test_source(Path("backend/agent.py"), text) == []


def test_test_sources_are_exempt() -> None:
    """A ``tests`` directory segment and a ``test_`` filename both exempt."""
    fixture = "2026" + "0810-twice-start"
    for path in (
        Path("backend/tests/unit/test_ids.py"),
        Path("backend/state_machine/tests/unit/test_routing.py"),
        Path("backend/tests/test_grep_guard.py"),
        Path("backend/test_helper.py"),
    ):
        assert find_plan_ids_in_non_test_source(path, fixture) == [], path


def test_production_sources_are_not_exempt() -> None:
    """The exemption is a path-segment test, with no holes in it."""
    fixture = "2026" + "0810-twice-start"
    for path in (
        Path("backend/agent.py"),
        Path(".claude/skills/req-clarification/SKILL.md"),
        Path("scripts/scan_commit_range.sh"),
        Path("backend/tests_helper.py"),  # "tests" as a stem, not a segment
        Path("docs/latest.py"),
    ):
        assert find_plan_ids_in_non_test_source(path, fixture), path


def test_the_shape_is_accepted_by_posix_ere(tmp_path) -> None:
    """The shape round-trips through ``git grep -E`` and still matches.

    This pins the "no ``\\b``" claim instead of asserting it in a comment.
    If someone adds a word boundary or a lookbehind to :data:`PLAN_ID_SHAPE`,
    the first assertion below names the reason, and the second shows the
    shell-side check going quiet while Python still reports a hit.
    """
    import subprocess

    pattern = PLAN_ID_SHAPE.pattern
    for python_only in ("(?<", "(?=", "(?!"):
        assert python_only not in pattern, (
            f"{pattern!r} contains {python_only!r}, which POSIX ERE — and so "
            f"`git grep -E` — does not understand. Checking this rule from a "
            f"shell would then report a clean tree."
        )

    # ``git grep --no-index`` only reaches files inside a work tree, so the
    # probe gets one of its own. The point is the ERE dialect, not the repo.
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    plan_id = "2026" + "0810-twice-start"
    (tmp_path / "probe.txt").write_text(f"seen on {plan_id} today\n", encoding="utf-8")

    def grep(expr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "grep", "-E", "-c", "-e", expr, "--no-index", "--", "probe.txt"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    good = grep(pattern)
    assert good.returncode == 0, f"git grep -E rejected or missed the shape: {good.stderr!r}"
    # ``git grep -c`` prints ``<path>:<count>`` when given a path.
    assert good.stdout.strip().endswith(":1"), good.stdout

    # And the shape this rule exists to avoid: it matches in Python and is
    # silently ignored by the very command used above.
    with_boundary = grep("\\b" + pattern)
    assert with_boundary.returncode == 1, (
        "git grep -E now honours \\b; if that ever becomes true the two "
        f"checks agree and this contrast stops proving anything: {with_boundary.stdout!r}"
    )
    assert PLAN_ID_SHAPE.search(plan_id), "Python should still match the same id"
