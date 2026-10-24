"""Pins for the service-spawn root-cause document.

What is under test
------------------
``docs/development/service-spawn-root-cause.zh.md`` -- the Chinese page
of the document that states what the spawn failure is, which candidate
the failure follows, and what shape the fix takes.  The measurement
that backs it is in ``backend/tests/manual/spawn_repro.py``; the numbers
in the document have to be numbers, not adjectives, or the document is
an opinion with a table on it.

The document is published in two files, one per language: the Chinese
one above, and ``service-spawn-root-cause.md`` in English.  The pins
read the Chinese page, because the contract is written in Chinese; the
last pin reads both, because a translation that drifts from the
contract is a contract nobody can check.

Why the document is pinned at all
---------------------------------
A root-cause document is prose, and prose cannot be tested for
truth.  What *can* be tested is that the claims it makes are the
claims it is supposed to make: that a table exists and every cell in
it is a count over a sample, that a root cause is named, that the
mechanism is one of the four shapes the follow-up work knows how to
read, that nothing is left unfinished, and that the document carries
no path off the machine it was written on.

Why the mechanism vocabulary is a closed set
--------------------------------------------
The fix that follows this document is written by other work, and it
reads one word to know what shape to build.  A word outside the set
would leave that work guessing, so the set is pinned here rather than
agreed on later.

The pins
--------
1. The document exists.
2. ``## 候选失败率`` has three candidate rows and every count cell in
   it is ``failed/runs``.
3. ``## 根因`` is not empty and names one candidate -- or names an
   interaction between candidates, which is a different conclusion and
   has to be said out loud.
4. ``- 机制：`` names one of the four shapes.
5. No unfinished-work markers.
6. No home-directory paths.
7. The English page exists, carries the same three sections in English,
   and says the same counts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

#: The repository root -- ``<repo>``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The Chinese page of the document under test -- the contract.
_DOC_PATH = (
    _REPO_ROOT / "docs" / "development" / "service-spawn-root-cause.zh.md"
)

#: The English page of the same document.
_EN_DOC_PATH = (
    _REPO_ROOT / "docs" / "development" / "service-spawn-root-cause.md"
)

#: The candidates the document may name.  The first three are the ones
#: the failure was supposed to be found in; the second three are the
#: ones that are about what the spawn hands over rather than about its
#: shape, and they are what the question became once the first three
#: came back flat.
_CANDIDATES: Tuple[str, ...] = (
    "start_new_session",
    "stdin",
    "parent_threads",
    "clear_env",
    "close_fds",
    "cwd",
)

#: The candidates whose table the document has to carry.
_TABLE_CANDIDATES: Tuple[str, ...] = (
    "start_new_session",
    "stdin",
    "parent_threads",
)

#: The shapes the follow-up work knows how to read.
_MECHANISMS: Tuple[str, ...] = (
    "wrapper",
    "stdin-handle",
    "posix-spawn",
    "unchanged",
)

#: The three contract sections, in English.  The Chinese page carries
#: ``候选失败率``, ``根因`` and ``修复形状``.
_EN_SECTIONS: Tuple[str, ...] = (
    "Candidate failure rate",
    "Root cause",
    "Fix shape",
)

#: A count over a sample: how many failed out of how many ran.
_COUNT_CELL = re.compile(r"^\d+/\d+$")

#: A markdown table separator row.
_SEPARATOR_ROW = re.compile(r"^\|[\s:\-|]+\|$")

#: The mechanism line: ``- 机制：<token>``.
_MECHANISM_LINE = re.compile(r"^\s*-\s*机制[:：]\s*(\S+)\s*$", re.MULTILINE)

#: Markers that mean the document is not finished.
_PLACEHOLDER = re.compile(
    r"(?i)t[b]d|t[o]do|to be decided|待\s*定"
)

#: A path off the machine the document was written on.
_HOME_PATH = re.compile(r"/Users/|/home/")


def _read() -> str:
    """The Chinese page's text."""
    return _DOC_PATH.read_text(encoding="utf-8")


def _en_read() -> str:
    """The English page's text."""
    return _EN_DOC_PATH.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of the ``heading`` section, up to the next heading.

    Returns the empty string when there is no such section, so a
    missing section fails the way a missing requirement should.
    """
    pattern = re.compile(
        r"^##\s+" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    return match.group(1)


def _table_rows(section: str) -> List[List[str]]:
    """The data rows of the first markdown table in *section*.

    The header row and the separator row are not data, so they are
    dropped; what is left is one list of cells per candidate.
    """
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if _SEPARATOR_ROW.match(stripped):
            continue
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows[1:] if rows else []


def _candidate_counts(section: str) -> List[Tuple[str, str, str]]:
    """The candidate table's rows as ``(name, on, off)``.

    The verdict cell is dropped on purpose: it is prose, and the two
    language versions word it differently.  The candidate name and the
    two counts are the same in both, and those are what have to match.
    """
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if _SEPARATOR_ROW.match(stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if _COUNT_CELL.match(cells[1]) and _COUNT_CELL.match(cells[2]):
            rows.append((cells[0], cells[1], cells[2]))
    return rows


def _named_candidates(text: str) -> List[str]:
    """The candidate names *text* mentions, each at most once."""
    named = []
    for candidate in _CANDIDATES:
        if re.search(r"\b" + re.escape(candidate) + r"\b", text):
            named.append(candidate)
    return named


# ---------------------------------------------------------------------------
# 1. The document exists
# ---------------------------------------------------------------------------


def test_doc_exists():
    """The root-cause document is part of the repository."""
    assert _DOC_PATH.is_file(), f"missing { _DOC_PATH.name }"


# ---------------------------------------------------------------------------
# 2. The candidate table carries counts, not adjectives
# ---------------------------------------------------------------------------


def test_candidate_table_has_numeric_cells():
    """Every cell of the candidate table is ``failed/runs``."""
    section = _section(_read(), "候选失败率")
    assert section.strip(), "no '## 候选失败率' section"

    rows = _table_rows(section)
    assert len(rows) == 3, (
        f"the candidate table has {len(rows)} rows, expected three"
    )

    for row, expected in zip(rows, _TABLE_CANDIDATES):
        assert len(row) == 4, f"row {row} is not a four-cell row"
        assert row[0] == expected, (
            f"row names {row[0]!r}, expected {expected!r}"
        )
        for cell in row[1:3]:
            assert _COUNT_CELL.match(cell), (
                f"cell {cell!r} is not a count over a sample"
            )


# ---------------------------------------------------------------------------
# 3. A root cause is named
# ---------------------------------------------------------------------------


def test_root_cause_section_names_one_cause():
    """The root-cause section names one candidate, or an interaction."""
    section = _section(_read(), "根因")
    assert section.strip(), "no '## 根因' section"

    if "交互" in section:
        return

    named = _named_candidates(section)
    assert len(named) == 1, (
        "the root-cause section must name exactly one candidate, "
        f"named {named!r}"
    )


# ---------------------------------------------------------------------------
# 4. The mechanism is one the follow-up work can read
# ---------------------------------------------------------------------------


def test_mechanism_token_in_vocabulary():
    """``- 机制：`` names one of the four known shapes."""
    text = _read()
    assert "修复形状" in text, "no '## 修复形状' section"

    found = _MECHANISM_LINE.findall(text)
    assert found, "no '- 机制：<token>' line"
    for token in found:
        assert token in _MECHANISMS, (
            f"mechanism {token!r} is not one of {_MECHANISMS!r}"
        )


# ---------------------------------------------------------------------------
# 5. Nothing left unfinished
# ---------------------------------------------------------------------------


def test_doc_has_no_placeholder():
    """The document says what it has to say."""
    text = _read()
    match = _PLACEHOLDER.search(text)
    assert match is None, f"unfinished marker {match.group(0)!r}"


# ---------------------------------------------------------------------------
# 6. No path off the machine
# ---------------------------------------------------------------------------


def test_doc_has_no_home_paths():
    """The document carries no home-directory path."""
    text = _read()
    match = _HOME_PATH.search(text)
    assert match is None, f"home-directory path {match.group(0)!r}"


# ---------------------------------------------------------------------------
# 7. The English page carries the same content
# ---------------------------------------------------------------------------


def test_the_english_page_exists():
    """The document is published in English as well as in Chinese."""
    assert _EN_DOC_PATH.is_file(), f"missing { _EN_DOC_PATH.name }"


def test_the_english_page_carries_the_same_sections():
    """The English page has the three contract sections, in English."""
    text = _en_read()
    for heading in _EN_SECTIONS:
        assert re.search(
            r"^##\s+" + re.escape(heading) + r"\s*$", text, re.MULTILINE
        ), f"the English page has no '## {heading}' section"


def test_the_two_versions_carry_the_same_counts():
    """The candidate table says the same numbers in both languages.

    A translation that quietly changes a number is worse than one that
    changes a word: the number is what the follow-up work reasons from.
    """
    chinese = _candidate_counts(_section(_read(), "候选失败率"))
    english = _candidate_counts(_section(_en_read(), "Candidate failure rate"))
    assert chinese, "the Chinese page has no candidate table"
    assert chinese == english, (
        "the two language versions disagree about the candidate counts: "
        f"{chinese!r} vs {english!r}"
    )
