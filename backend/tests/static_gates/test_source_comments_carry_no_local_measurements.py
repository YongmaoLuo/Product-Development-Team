"""No first-party source may carry a first-hand measurement or a rewrite scar.

Why this gate exists
--------------------
``SECURITY_AUDIT.md`` states the project rule as "缺陷公开，事故私有": a
defect is something any reader can reproduce from the code, while an
incident is a fact about one machine — a date, a count, a duration, a
plan id. The audit's own gate enforces that for the documents. Source
comments were deliberately left out of it:

    "没有同时把门禁的扫描面扩到源码注释，这是刻意的：门禁现有的「套件
     记录」与「否定结果单元格」两类形状必须限定在文档上，否则会与测试
     夹具里合法的 pytest 输出字符串（形如 ``<n> passed``）大量误报"

That reasoning is sound, and it is scoped to *those* shapes. Two other
shapes are precise enough to scan source for, and both are still in the
tree because nothing was watching:

1. **A measurement verb next to a date.** The evidence verb and a
   calendar date in the same breath — the sentence is reporting what
   happened on one machine, not what the code does. The count, duration
   or plan id usually travels with it.
2. **The scar of a mechanical rewrite.** An earlier pass replaced the
   "dated plan" phrasing with an indefinite description and did not
   handle the determiner in front of it, leaving a determiner glued to
   an indefinite article across six files. That is ungrammatical English
   *and* it kept the numbers it was meant to remove, so the sentence now
   reads like a general statement while still quoting one run's results.

What this gate cannot see
-------------------------
Its green light means "these two shapes are absent", **not** "the source
carries no incident narrative". A first-hand story with no date and no
listed verb — a bare plan id, a "in that run" clause, a repair-task id —
is not machine-detectable at acceptable precision, and the second shape
above proves the point: the rewrite that produced it was itself aimed at
removing incident text and made the problem harder to read instead.

The concrete examples of every shape live in the tests below, assembled
at runtime, rather than in this docstring — see the note that follows.

Why this test file must be phrase-free
--------------------------------------
This file is inside the scan root. Any sample it contains would be
scanned, so every sample is assembled from fragments at runtime — the
date, and for the Chinese form the verb's suffix, are separate literals.
Without that, the gate would flag its own fixtures and would have to be
allowlisted; an allowlisted gate protects nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import source_scan

# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------

#: Verbs that report a measurement taken at a point in time. "Observed" is
#: on the list because the tree's dominant form is "Observed on the
#: <date> ..."; a bare "observed" describing program behaviour ("the
#: scheduler observes the queue") is not followed by a date and so does
#: not match.
_MEASUREMENT_VERBS = r"(?:observed|measured|verified|reproduced)"

#: ``YYYY-MM-DD``. General on purpose — narrowing it to the current
#: decade would miss the shape the moment the project's dates move.
_DATE = r"\d{4}-\d{2}-\d{2}"

#: Chinese measurement verbs. Written with the ``re.escape``-free literal
#: forms because they are unambiguous on their own; only the *adjacency*
#: to a date is the defect.
_CN_VERBS = "(?:实测|观察于|复现于)"

MEASUREMENT_SHAPES: tuple[re.Pattern[str], ...] = (
    # English: a measurement verb, then a date within a short window.
    # The window is bounded by the end of the sentence so a verb in one
    # sentence cannot reach a date in the next.
    re.compile(
        r"\b" + _MEASUREMENT_VERBS + r"\b[^.\n]{0,30}?" + _DATE,
        re.IGNORECASE,
    ),
    # Chinese: the date follows the verb almost immediately ("实测(
    # <date>", "实测来源(<date>"). Kept tight rather than given the
    # English window because Chinese prose packs more meaning per
    # character, so a 30-character window would span a whole clause.
    re.compile(_CN_VERBS + r"[^。\n]{0,4}[（(]?\s*" + _DATE),
    # The rewrite scar: a determiner followed by an indefinite article.
    # English never puts them together, so this cannot be stylistic.
    re.compile(
        r"\bthe (?:an|a) (?:earlier|previous|prior|production|real)\b",
        re.IGNORECASE,
    ),
)


def find_local_measurements(path: Path, text: str) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, match)`` for every offending occurrence.

    One entry per line per shape, so a caller can format
    ``rel:lineno: <match>`` without re-walking the tree.
    """
    hits: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for shape in MEASUREMENT_SHAPES:
            for match in shape.finditer(line):
                hits.append((path, lineno, match.group(0).strip()))
    return hits


# ---------------------------------------------------------------------------
# Sensitivity — each shape must hit, and legitimate prose must not
# ---------------------------------------------------------------------------


def test_scan_is_non_empty() -> None:
    """The scan target must produce at least one file.

    A gate whose walk has drifted away from the real tree passes
    vacuously. Same rationale as the sibling gates' ``test_scan_is_non_empty``.
    """
    assert list(source_scan.iter_first_party_sources()), (
        "source_scan.iter_first_party_sources() returned no files — the "
        "gate would now pass on an empty result."
    )


def test_the_english_measurement_shape_is_detected() -> None:
    """A verb plus a date, in both orders the tree actually uses.

    Samples are built from fragments so this file's own source does not
    contain the shape it forbids.
    """
    date = "2026-09-20"
    fragments = (
        "regenerated; " + "Observed on the " + date + " that the hint was wrong",
        "the cap never bound (" + "verified " + date + " during a rewrite)",
    )
    for sample in fragments:
        assert find_local_measurements(Path("dummy.py"), sample), (
            f"synthesized measurement not detected: {sample!r}"
        )


def test_the_chinese_measurement_shape_is_detected() -> None:
    """``实测``/``观察于`` immediately followed by a date."""
    date = "2026-09-16"
    verb = "实" + "测"
    samples = (
        verb + "(" + date + ", 计划 VP-027）类节点永远找不到",
        verb + "来源(" + date + ", VP-027)",
    )
    for sample in samples:
        assert find_local_measurements(Path("dummy.py"), sample), (
            f"synthesized measurement not detected: {sample!r}"
        )


def test_the_rewrite_scar_is_detected() -> None:
    """A determiner glued to an indefinite article."""
    samples = (
        "The " + "an" + " earlier plan's round-2 repair died that way.",
        "shipped anyway on the " + "a" + " production plan",
    )
    for sample in samples:
        assert find_local_measurements(Path("dummy.py"), sample), (
            f"rewrite scar not detected: {sample!r}"
        )


def test_legitimate_prose_is_left_alone() -> None:
    """The counterweight: none of these is an incident report.

    Every sample is modelled on real first-party text. A gate that flags
    them would be allowlisted within a week.
    """
    samples = (
        # A measurement verb with no date — the code describes its own
        # behaviour, which is exactly what a comment is for.
        "the scheduler observes the queue and records what it saw",
        "the fake key is verified against the loader before use",
        # A date with no measurement verb — a changelog or contract note.
        "2026-09-28: the state family moved into `.pdt/`",
        "reproducible: the command exits 0 against the current tree",
        # The Chinese verb without a date.
        "实测不需要日期也能描述行为",
        # Correct English determiners, including one that a careless
        # pattern would catch.
        "the earlier plan's rows are gone",
        "the previous behaviour is preserved",
        "the real provider is selected from the contract file",
        "there is a production plan in every fixture",
    )
    for sample in samples:
        hits = find_local_measurements(Path("dummy.py"), sample)
        assert not hits, (
            f"legitimate text flagged: {sample!r} -> {hits}"
        )


# ---------------------------------------------------------------------------
# The full-tree scan
# ---------------------------------------------------------------------------


def test_no_first_party_source_carries_a_local_measurement() -> None:
    """Full-tree scan: no measurement-with-date and no rewrite scar.

    Reproduce the rule rather than the code: keep the observation, drop
    the date, the count, and the plan id that travelled with it. Where
    the sentence only made sense as a report of one run, describe what
    the code does instead — and if it cannot be described that way, that
    is the signal the comment was never about the code.
    """
    offenders: list[str] = []

    for path in source_scan.iter_first_party_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _, lineno, matched in find_local_measurements(path, text):
            offenders.append(f"{path}:{lineno}: {matched}")

    assert not offenders, (
        "First-party source carries a dated first-hand measurement or the "
        "scar of a mechanical rewrite. Keep the observation and the "
        "reasoning; drop the date, the counts, and the plan/task ids.\n  "
        + "\n  ".join(offenders)
    )
