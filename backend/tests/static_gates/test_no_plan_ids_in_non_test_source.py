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
The shape avoids ``\\b`` on purpose, because POSIX does not define a word
boundary -- ``\\b`` is a GNU extension, and an ERE engine is free to
either honour it or treat it as a literal ``b``. A pattern written with
one means different things to different consumers, and the direction that
hurts is the one where the engine drops it and reports a clean tree.

Which engine that is depends on more than the operating system. Measured
on this branch, same pattern, same command::

    # macOS, git's own regex engine: \\b is dropped -> "no match", exit 1
    $ git grep -E -c '\\b20[0-9]{6}-[A-Za-z]' --no-index -- probe.txt ; echo $?
    1
    # ubuntu-24.04, git grep over GNU grep 3.11: \\b honoured -> exit 0
    $ git grep -E -c '\\b20[0-9]{6}-[A-Za-z]' --no-index -- probe.txt ; echo $?
    probe.txt:1
    0

So the failure mode is real and so is its opposite, and *which* one you
get is a property of the runner rather than of the pattern. A rule
therefore cannot be kept honest by probing one engine for the absence of
an extension: the probe answers a question about the machine. The test
below pins the property the rule actually needs -- that the pattern
contains nothing POSIX leaves undefined, and that ``git grep -E`` and
Python agree on both a hit and a near-miss -- which holds on every
engine.

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

#: Constructs Python's ``re`` accepts and POSIX ERE does not define.
#: Python's are a superset, not an extension: ``\b``, ``\d`` and
#: ``(?=`` all work here, and every ERE engine is entitled to read them as
#: something else -- ``\d`` as the letter ``d``, ``\b`` as the letter
#: ``b``, ``(?=`` as a syntax error. Kept as data so that naming a new
#: construct is the whole change, and so the check below can report what
#: it forbids rather than asserting a dialect it cannot compile.
#:
#: The string anchors are here for the same reason as ``\b``: ``re``
#: matches the whole string, an ERE matches a line, so ``\A`` and ``\z``
#: would not mean the same thing even where they are understood.
NOT_POSIX_ERE: tuple[str, ...] = (
    r"\b", r"\B",
    r"\A", r"\Z", r"\z", r"\G",
    r"\d", r"\D", r"\w", r"\W", r"\s", r"\S",
    r"\Q", r"\E", r"\h", r"\H",
    "(?<", "(?=", "(?!", "(?>", "(?P<", "(?#", "(?(",
    "(?i", "(?m", "(?s", "(?x", "(?a", "(?u", "(?L",
)

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


def test_the_shape_matches_every_leaked_string_shape() -> None:
    """Every id *shape* from the 2026-09-29 incident is caught.

    This is the negative control for the whole gate: if the shape ever
    stops matching one of these, the gate is reporting a clean tree over
    exactly the leak it was written for.

    The second entry is a synthetic stand-in. It used to be two fragments
    that reassembled into a real plan name, and fragments are not a
    protection -- that is the same argument that moved the forbidden-name
    blacklist out of this repository, and it applies to one name just as
    much as to a list of them. The rule keys on :data:`PLAN_ID_SHAPE`, so
    what this row pins is the *slug shape* (a hyphenated run glued to the
    date prefix on both sides), which is the part that could regress.
    """
    samples = [
        "2026" + "1010-" + "CC-Switch-" + "Remote-Aut",
        "2026" + "1012-" + "example-" + "topic-buy" + "sell-points-rebuild",
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
    """The shape means the same thing in Python and in ``git grep -E``.

    :data:`PLAN_ID_SHAPE` is shared with consumers that are not Python --
    ``git grep -E`` stands in for "somebody checking this from a shell" --
    and the failure this gate is written against is the two *disagreeing*
    without anyone noticing. So this pins agreement, in both directions,
    rather than agreement plus a probe of one engine's extensions.

    The first assertion is what the old version got right and kept: a
    construct Python understands and POSIX does not define must not appear
    in the pattern. :data:`NOT_POSIX_ERE` is the full list rather than the
    three tokens it started as, and it is checked on the pattern text
    because that is the only question with one answer on every machine.

    The probe is what the old version got wrong, and the reason is worth
    keeping: it asked whether ``git grep -E`` honours ``\\b`` and asserted
    the answer, which is a property of the runner. Apple's git dropped it,
    GNU grep keeps it, so the assertion passed on a laptop and failed on
    the runner -- a test that could only ever be green on one of the two
    platforms it was supposed to cover. A probe cannot answer it; the
    pattern can. What the probe *can* answer, on any engine, is whether
    the two sides find the same lines, which is the second half.
    """
    import subprocess

    pattern = PLAN_ID_SHAPE.pattern
    for python_only in NOT_POSIX_ERE:
        assert python_only not in pattern, (
            f"{pattern!r} contains {python_only!r}, which POSIX ERE does not "
            f"define — an ERE engine may drop it and report a clean tree "
            f"where Python reports a hit. Remove it, or move the check off "
            f"`git grep -E` and onto a Python consumer."
        )

    # ``git grep --no-index`` only reaches files inside a work tree, so the
    # probe gets one of its own. The point is the ERE dialect, not the repo.
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    plan_id = "2026" + "0810-twice-start"
    (tmp_path / "probe.txt").write_text(f"seen on {plan_id} today\n", encoding="utf-8")
    # The other direction: lines Python calls clean. An ERE engine that
    # matches one of these has drifted from the rule just as surely as one
    # that misses ``probe.txt``, and it does so on the same engines, so it
    # is pinned by the same platform-independent assertion. The last line
    # is the one that carries the weight: widening the single hyphen after
    # the date into ``[_-]`` is the loosening this shape is one edit away
    # from, and it turns that line into a match on both sides at once.
    near_miss = (
        "released 2026-10-14 as build 20261014, no plan here\n"
        "the plan count went 15 -> 35 tasks\n"
        "see 2099-01-01 and 2026-13-01 for the malformed cases\n"
        "artifact 20310405_x86 carries no plan id\n"
    )
    (tmp_path / "near_miss.txt").write_text(near_miss, encoding="utf-8")

    def grep(expr: str, target: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "grep", "-E", "-c", "-e", expr, "--no-index", "--", target],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    good = grep(pattern, "probe.txt")
    assert good.returncode == 0, f"git grep -E rejected or missed the shape: {good.stderr!r}"
    # ``git grep -c`` prints ``<path>:<count>`` when given a path.
    assert good.stdout.strip().endswith(":1"), good.stdout
    assert PLAN_ID_SHAPE.search(plan_id), "Python should still match the same id"

    quiet = grep(pattern, "near_miss.txt")
    assert quiet.returncode == 1, (
        "git grep -E matched a line the rule calls clean, so the two sides "
        f"disagree about what a plan id is: {quiet.stdout!r}"
    )
    assert find_plan_ids_in_non_test_source(Path("backend/agent.py"), near_miss) == []
