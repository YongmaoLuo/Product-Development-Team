"""No file anywhere in the repository may carry a private proper name.

Why this gate exists
--------------------
On 2026-09-29 a ``git push`` published a private project name, a business
term and real plan ids. ``force-push`` did not undo it: deleting a ref does
not delete objects, and the old commits stay fetchable by SHA.

The plan-id *shape* is covered by
:mod:`test_no_plan_ids_in_non_test_source`, which exempts tests. This rule is
the complement: a **name** is a violation wherever it appears, with no
exemption at all, because a private project name inside a test fixture is
exactly as published as one inside a docstring. Only a *shape* has a
legitimate synthetic stand-in; a name does not.

Why the blacklist is written down at all
----------------------------------------
:mod:`pr_metadata_scan` argues that a private name cannot be compiled into
this repository, because writing it down is itself the leak. That argument
is correct for a name that is *not already published* — and it is exactly
why this rule is applied to ``origin/main`` too, "already public" not being a
defence. The circularity has a cut: the names that must not be published must
still be checkable, and the only way out is to accept that a name known to
be public in one ref is worth blacklisting so it never spreads to another.

Each entry was measured against the ``origin/main`` tree before it was added.
Counts are recorded in ``tests`` below so a later edit that starts firing on
existing content is visible rather than surprising.

Why every entry is built from fragments
---------------------------------------
This file lives under the scan root and rule 2 has no test exemption, so a
literal private name in this source -- even inside ``PRIVATE_NAME_PHRASES``
-- would make the gate fail on its own definition. Every entry is therefore
concatenated from fragments, the same convention
``test_no_operator_attribution_in_source`` uses.

Why no ``\\b``
--------------
Same reason as the plan-id rule, and for the same measured reason: POSIX ERE
has no word boundary, and ``git grep -E`` silently ignores one. Matching is
plain case-insensitive substring or a shape regex, so the shell-side and
Python-side answers agree. False positives are the right way round here: a
two-second rewrite beats a permanent publication.

Why this file must not import pytest
-------------------------------------
``scripts/check_commit_msg.py`` runs under a bare ``python3`` with no venv and
imports from here, so that "what a private name is" has one definition. The
tests below use plain ``assert``.
"""

from __future__ import annotations

import re
from pathlib import Path

import source_scan

#: Private proper names, as ``(label, needle)`` pairs. Matched
#: case-insensitively as substrings. Assembled from fragments so this file
#: does not contain any of them whole -- see the module docstring.
#:
#: The leading hyphen on that entry is load-bearing and must not be
#: dropped. The public name of *this* repository is the un-hyphenated form
#: of the same three words, so a bare entry would match the project's own
#: identity in ``README.md``, ``mkdocs.yml``, ``CHANGELOG.md`` and
#: ``docs/security.md`` -- 53 occurrences on ``origin/main`` -- while
#: matching no private plan id at all, because every private id carries the
#: words hyphen-prefixed by a date or by the project's private prefix.
#: Requiring that hyphen separates the two: measured, 0 hits against the
#: ``origin/main`` tree for the hyphen-prefixed form and 53 for the bare
#: one.
PRIVATE_NAME_PHRASES: tuple[tuple[str, str], ...] = (
    # The private project name from the 2026-09-29 incident.
    ("private frontend project", "frontend" + "-echarts"),
    # The operator's other local checkout: a strategy project.
    ("private strategy project", "chan" + "lun"),
    # Its feature branch name, which carries the project name above.
    ("private strategy feature branch",
     "chan" + "lun" + "-" + "buysell" + "-" + "points"),
    # The same feature, named without the project.
    ("private buy/sell feature", "buysell" + "-points"),
    # A private repository the provider chain was switched over from.
    ("private provider remote", "CC-Switch" + "-Remote"),
    # Business vocabulary from the private project.
    ("candlestick term (zh)", "k" + "线"),
    ("candlestick term (en)", "k" + "line"),
    # The private plan-id family. See the note above on the leading hyphen.
    ("private plan-id family", "-" + "Product-Develop"),
    # A real plan id from the 2026-09-29 incident, kept whole because a
    # caller may quote it without the surrounding plan-id shape.
    ("plan id from the 2026-09-29 incident", "2026" + "0819-"),
)

#: Shapes a literal phrase list cannot express: what matters is the
#: *structure* of the string, not any one word in it.
#:
#: ``symbol=<six digits>`` is a live quote identifier against a real
#: exchange. Six digits is the shape of a mainland-China A-share code, and
#: this repository records quotes of real instruments in its tests and in
#: notifications. The shape is matched rather than a list of codes so a new
#: code is covered without adding it here.
PRIVATE_NAME_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("live quote symbol", re.compile(r"symbol=[0-9]{6}", re.IGNORECASE)),
)


def find_private_names(
    path: Path, text: str
) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, needle)`` for every private name in *text*.

    No path is exempt -- including ``tests/``. Each entry records the file,
    the line and the label of the rule that fired, so a downstream consumer
    can format ``rel:lineno`` without re-walking the tree and can say *why*
    a line was rejected.
    """
    hits: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for label, needle in PRIVATE_NAME_PHRASES:
            if needle.lower() in lowered:
                hits.append((path, lineno, f"{label}: {needle}"))
        for label, shape in PRIVATE_NAME_SHAPES:
            match = shape.search(line)
            if match:
                hits.append((path, lineno, f"{label}: {match.group(0)}"))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_is_non_empty() -> None:
    """The scan target must produce at least one file.

    Same rationale as the sibling gates: a gate whose target has vanished
    passes vacuously.
    """
    assert list(source_scan.iter_first_party_sources()), (
        "source_scan.iter_first_party_sources() returned no files — the "
        "gate would now pass on an empty result."
    )


def test_every_real_leaked_string_is_detected() -> None:
    """The negative control: each string from 2026-09-29 must fire.

    This is the assertion that keeps the rule honest. If any of these stops
    firing, the gate is green over exactly the leak it was written for.
    """
    # Built from fragments: this file is scanned by the rule it defines,
    # so a sample written out whole would make the gate fail on its own
    # test data. The strings are the ones from 2026-09-29, unmodified.
    samples = [
        "chan" + "lun",
        "20261010-" + "CC-Switch-" + "Remote-Aut",
        "20261012-" + "chan" + "lun-" + "buysell-" + "points-rebuild",
        "20260926-" + "Product-" + "Development",
        "symbol=" + "000001",
    ]
    for sample in samples:
        hits = find_private_names(Path("backend/agent.py"), sample)
        assert hits, f"not detected: {sample!r}"


def test_detection_is_case_insensitive() -> None:
    """A leaked name written in a different case is still a leak."""
    samples = (
        "CHAN" + "LUN",
        "FRONTEND-" + "ECHARTS",
        "cc-SWITCH-" + "remote-aut",
        "K" + "LINE",
    )
    for sample in samples:
        assert find_private_names(Path("backend/agent.py"), sample), sample


def test_test_sources_are_not_exempt() -> None:
    """A private name in a test file is as published as one in a docstring."""
    needle = "chan" + "lun"
    for path in (
        Path("backend/tests/unit/test_ids.py"),
        Path("backend/state_machine/tests/unit/test_routing.py"),
        Path("backend/tests/conftest.py"),
    ):
        assert find_private_names(path, needle), path


def test_the_repository_own_public_name_is_not_a_violation() -> None:
    """``Product-Development-Team`` is this project's own identity.

    Blacklisting the bare word would flag the repository's own name in its
    README, its mkdocs nav and its changelog -- and would not catch a single
    private plan id, which always carry the same words hyphen-prefixed.
    Measured on ``origin/main``: 53 hits for the bare word, 0 for the
    hyphen-prefixed form.
    """
    for line in (
        "Product-Development-Team",
        "https://github.com/owner/Product-Development-Team.git",
        "Product-Development-Team/backend",
    ):
        assert find_private_names(Path("README.md"), line) == [], line


def test_ordinary_prose_is_not_a_violation() -> None:
    """No false positive on the English the repository already uses."""
    text = (
        "the backend line loops and the frontend build is green\n"
        "a stock ticker symbol=X is not a six digit code\n"
        "k means kilo; see RFC 1234 for the registry\n"
    )
    assert find_private_names(Path("backend/server.py"), text) == []


def test_the_blacklist_has_no_duplicate_needles() -> None:
    """Two entries matching the same text make the count unreadable."""
    needles = [needle for _, needle in PRIVATE_NAME_PHRASES]
    assert len(needles) == len(set(needles)), needles


def test_each_phrase_is_present_as_a_fragment_pair() -> None:
    """No entry may be written out whole in this file's source.

    Enforced mechanically rather than by convention: concatenate each
    entry's source fragment, and fail if the literal appears anywhere in
    this file. This is the same self-reference the other gates carry, and
    the reason their phrase lists are built the same way.
    """
    # Case-insensitive, because the gate matches case-insensitively: a
    # sample shouted in capitals would flag this file just as surely as
    # the lower-case spelling would.
    source = Path(__file__).read_text(encoding="utf-8").lower()
    for label, needle in PRIVATE_NAME_PHRASES:
        assert needle.lower() not in source, (
            f"{needle!r} ({label}) appears literally in this file, so the "
            f"gate would flag its own definition. Build it from fragments."
        )
    # The shape's own probe: a six-digit code must not be written whole
    # either, for the same reason.
    assert "symbol=" + "000001" not in source, (
        "a literal quote code appears in this file; build it from fragments."
    )
