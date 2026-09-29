"""No first-party source may carry operator-attribution phrases or external-checkout names.

Why this gate exists
--------------------
The project rule (CLAUDE.md, "Code must not carry information unrelated to
this project") forbids two shapes that are easy to write and hard to spot
in review:

1. Attribution markers that quote a private conversation or person into
   source. Both English and Chinese forms are caught.
2. External-checkout names — directory names of other repositories the
   developer keeps alongside this one. The list is operator-specific and
   cannot be compiled into the repo (writing it down would itself be the
   leak), so it is read at runtime from an env var.

Why a gate (not just review)
----------------------------
Both defects have shipped already. The first export from the private
development repo carried attribution markers in comments and docstrings
across roughly 340 references; the path-of-least-resistance is to let
them silently accumulate. A regex is cheap; the env-var-only contract
for external names is the part that drifts without a gate.

Scoping
-------
The repo root ``CLAUDE.md`` is the rule body itself — it must be excluded,
otherwise the gate would always fire on the words it is defining. The
shared ``source_scan`` walker already excludes it (``SCAN_ROOTS`` does not
include the repo root), but this test pins that fact explicitly.

Why this test file must be phrase-free
--------------------------------------
This file is itself under the scan root (``backend/tests/static_gates/``).
A phrase that appears in its own source — even inside a docstring or the
``ATTRIBUTION_PATTERNS`` tuple — would make the gate flag its own
definition. The phrases are therefore built from string fragments so the
literal pattern never appears whole in this file's source text.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import source_scan

# ---------------------------------------------------------------------------
# Attribution phrase patterns
# ---------------------------------------------------------------------------

#: Attribution markers that name a private conversation or person.
#: Both English and Chinese forms are listed. Case-insensitive matching
#: is applied at lookup time, so the lowercase canonical forms here are
#: the only ones needed. The entries are built from string fragments so
#: this file's own source does not contain the literal patterns the gate
#: catches (see the module docstring's "phrase-free" note).
ATTRIBUTION_PATTERNS: tuple[str, ...] = (
    "user " + "directive",
    "operator " + "report",
    "用户" + "原话",
    "操作员" + "报告",
    "user " + "feedback",
    "operator " + "feedback",
    "用户" + "提出",
)

#: Structural shapes the phrase list cannot express: the giveaway is the
#: *relationship* between words, not any single word. A prompt template
#: that labels the review UI's own feedback field is legitimate; a
#: changelog entry recording what one person demanded is not. Matching on
#: a word would flag both; matching on the shape flags one. The examples
#: live in the tests below, as fragments, for the same self-scan reason
#: as the phrase list.
#:
#:   * shape 1 — the attributor inside a possessive phrase.
#:   * shape 2 — a possessive followed by a noun naming a demand.
#:   * shape 3 — the attributor as the subject of a speech verb.
#:   * shape 4 — a dated changelog bullet whose body opens with a
#:     quotation, i.e. someone's words preserved verbatim. Errors and log
#:     lines are quoted *inline* in prose, so this combination means "a
#:     person said this", not "the program printed this". A changelog
#:     bullet of that shape can be rewritten with the quote inline.
_ATTRIBUTOR = r"(?:user|operator|owner)"

ATTRIBUTION_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bper\s+the\s+" + _ATTRIBUTOR + r"'s\b", re.IGNORECASE),
    re.compile(
        r"\b" + _ATTRIBUTOR + r"'s\s+(?:\d{4}-\d{2}-\d{2}\s+)?"
        r"(?:requirement|request|directive|feedback|preference|ask|words"
        r"|clarification|principle)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bthe\s+" + _ATTRIBUTOR + r"\s+"
        r"(?:asked|said|wrote|reported|requested|wanted|required|demanded"
        r"|told|rejected|chose|preferred|flagged|clarified)\b",
        re.IGNORECASE,
    ),
    # A changelog bullet whose body opens with a quotation — someone's
    # words preserved verbatim. Errors and log lines are quoted *inline*
    # in prose, so (date-prefixed bullet + opening quote) is the shape
    # that means "a person said this", not "the program printed this".
    re.compile(r"^[ \t]*[*\-][ \t]*\d{4}-\d{2}-\d{2}[ \t]*[:：][ \t]*[\"\u201c]"),
)

#: Env var name that lists the operator's other local checkouts. The
#: names themselves never appear in this file, in test data, or in
#: assertions — the gate would be self-invalidating if it did. An
#: unset or empty value disables the external-name check entirely
#: (no violation, no assumption) rather than guessing a list.
EXT_CHECKOUT_ENV_VAR = "PDT_FORBIDDEN_REPO_NAMES"


def find_attribution(
    path: Path, text: str
) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, phrase)`` for every attribution occurrence.

    Each entry records the file path, line number, and the matched
    phrase so a downstream consumer (e.g. the audit-integrity meta-test
    pinned by task 14) can format ``rel:lineno: <snippet>`` without
    re-walking the tree. Matching is case-insensitive and substring
    based — the phrases are short and unambiguous, so a regex buys
    nothing here and a literal search is easier to audit.
    """
    hits: list[tuple[Path, int, str]] = []
    for phrase in ATTRIBUTION_PATTERNS:
        if not phrase:
            continue
        needle = phrase.lower()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                hits.append((path, lineno, phrase))
    for shape in ATTRIBUTION_SHAPES:
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = shape.search(line)
            if m:
                hits.append((path, lineno, m.group(0)))
    return hits


def external_names() -> tuple[str, ...]:
    """Read the operator's forbidden checkout names from the env var.

    Returns an empty tuple when the env var is unset, empty, or
    whitespace-only — in which case the external-name check is a no-op.
    When set, the value is split on commas and each segment is
    whitespace-stripped; empty segments are dropped. Names are echoed
    back verbatim (no case folding) so the caller can format them in
    the operator's own spelling.
    """
    raw = os.environ.get(EXT_CHECKOUT_ENV_VAR, "")
    if not raw.strip():
        return ()
    return tuple(
        segment.strip()
        for segment in raw.split(",")
        if segment.strip()
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_is_non_empty() -> None:
    """The scan target must produce at least one file.

    A gate whose scan target has disappeared — the excluded-dir set
    swallowed the tree, the repo was reorganised — passes vacuously.
    Same rationale as the home-path gate's ``test_scan_is_non_empty``.
    """
    files = list(source_scan.iter_first_party_sources())
    assert files, (
        "source_scan.iter_first_party_sources() returned no files — the "
        "gate would now pass on an empty result. Either the SCAN_ROOTS, "
        "EXCLUDED_DIRS, or the file extension filter has drifted away "
        "from the real first-party tree."
    )


def test_attribution_phrase_is_detected() -> None:
    """A synthesized snippet containing the canonical phrase must hit.

    The fixture text is assembled from string fragments so this test
    file's own source does not contain the literal phrase the gate
    forbids (a self-referential scan would either suppress the rule for
    this file or fail the suite on its own definition).
    """
    # Build the phrase from two halves so this file's own source does
    # not contain the literal pattern that the gate catches.
    phrase = "user " + "directive"
    text = "WARNING: results may differ from CI — " + phrase + "\n"
    hits = find_attribution(Path("dummy.py"), text)
    expected = "user " + "directive"
    assert hits, "synthesized attribution snippet was not detected"
    assert any(matched == expected for _, _, matched in hits)


def test_attribution_shapes_are_detected() -> None:
    """The structural shapes hit, one sample per pattern.

    The phrase list only catches markers that spell the attribution out
    (one of the two-word markers above). The forms that actually recurred
    in this tree were relational — a possessive phrase, a possessive plus
    a demand noun, a speech verb, a dated bullet quoting someone — and
    none of them contains a listed phrase. Each sample is assembled from
    fragments at runtime so this file's own source stays free of the text
    it forbids.
    """
    date = "2026-09-15"
    who = "user"
    samples = (
        "Governing rule, per the " + who + "'s request: do not inline.",
        "This makes the " + who + "'s " + date + " requirement hold again.",
        "Clearing them would hide it; the " + who + " asked"
        + " for the ability to void a task.",
        "* " + date + ": \u201c" + "just retry it twice" + "\u201d",
    )
    for sample in samples:
        hits = find_attribution(Path("dummy.py"), sample)
        assert hits, f"shape not detected: {sample!r}"


def test_the_shapes_leave_legitimate_text_alone() -> None:
    """The counterweight: none of these is attribution.

    Every sample here appears in (or is modelled on) real first-party
    text. A gate that flags them would be allowlisted within a week,
    and an allowlisted gate protects nothing — so the shapes are pinned
    against the false positives that motivated them.
    """
    samples = (
        # A prompt template showing the review UI's own feedback field.
        "用户反馈：{feedback}",
        # Product prose about the review flow itself.
        "Revise a decision point from the review feedback.",
        # An error or log line quoted *inline* in prose.
        "the run died with \u201cconnection refused\u201d after 30 s",
        # The software's own operator — generic, not a private person.
        "so the operator does not mistake a suspended loop for a "
        "still-running round",
        # A dated bullet that does NOT open with a quotation.
        "* " + "2026-09-15" + ": retries are now for malfunctions only.",
    )
    for sample in samples:
        hits = find_attribution(Path("dummy.py"), sample)
        assert not hits, (
            f"legitimate text flagged as attribution: {sample!r} -> {hits}"
        )


def test_no_operator_attribution_outside_claude_md() -> None:
    """Full-tree scan: zero attribution or external-name violations.

    Walks every first-party source file and looks for both attribution
    phrases and external-checkout names. The repo root ``CLAUDE.md`` is
    the rule body and must be excluded; this test pins that exclusion
    explicitly so a future change to ``SCAN_ROOTS`` that pulls it in
    fails loudly instead of always-red.
    """
    # ``backend/tests/static_gates/test_no_operator_attribution_in_source.py``
    # -> backend/tests/static_gates -> backend/tests -> backend -> repo.
    repo_root = Path(__file__).resolve().parents[3]

    files = list(source_scan.iter_first_party_sources())

    # Pin the exclusion: CLAUDE.md must not appear in the scan set.
    # SCAN_ROOTS does not include the repo root, so this should hold
    # naturally — the assertion is here so a future change to SCAN_ROOTS
    # that brings it in fails loudly.
    claude_md = repo_root / "CLAUDE.md"
    if claude_md.exists():
        for path in files:
            try:
                same = path.resolve() == claude_md.resolve()
            except OSError:
                continue
            assert not same, (
                f"CLAUDE.md must not be scanned (it is the rule body "
                f"itself): {path}"
            )

    ext_names = external_names()
    offenders: list[str] = []

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Attribution phrases
        for _, lineno, phrase in find_attribution(path, text):
            offenders.append(
                f"{path}:{lineno}: attribution phrase {phrase!r}"
            )

        # External-checkout names. Match as a path-segment token to
        # avoid false positives on substring overlaps with English
        # words (e.g. a project literally named "to" inside "total").
        # ``PDT_FORBIDDEN_REPO_NAMES`` unset/empty -> skip entirely.
        if ext_names:
            for name in ext_names:
                if not name:
                    continue
                pattern = re.compile(
                    r"(?<![A-Za-z0-9._-])"
                    + re.escape(name)
                    + r"(?![A-Za-z0-9._-])",
                    re.IGNORECASE,
                )
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        offenders.append(
                            f"{path}:{lineno}: "
                            f"external-checkout name {name!r}"
                        )

    assert not offenders, (
        "First-party source contains an attribution marker or a "
        "reference to a forbidden local checkout. Strip the phrase, "
        "remove the name, or move the value to runtime config. The "
        "rule body lives in CLAUDE.md.\n  "
        + "\n  ".join(offenders)
    )


def test_external_names_come_from_env_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The external-name list is sourced from the env var at runtime.

    When ``PDT_FORBIDDEN_REPO_NAMES`` is unset or empty,
    ``external_names()`` returns ``()`` and the external-name check is
    skipped (no violation, no assumption). When set, the value is split
    on commas with whitespace stripped; empty segments are dropped. The
    fixture values here are abstract placeholders, not real checkout
    names — adding a real name to this test would be the leak the gate
    is built to prevent.
    """
    # Unset: empty tuple.
    monkeypatch.delenv(EXT_CHECKOUT_ENV_VAR, raising=False)
    assert external_names() == ()

    # Empty string: also empty tuple.
    monkeypatch.setenv(EXT_CHECKOUT_ENV_VAR, "")
    assert external_names() == ()

    # Whitespace-only: also empty tuple.
    monkeypatch.setenv(EXT_CHECKOUT_ENV_VAR, "   ")
    assert external_names() == ()

    # Set: comma split + whitespace strip.
    monkeypatch.setenv(EXT_CHECKOUT_ENV_VAR, "alpha,beta , gamma")
    assert external_names() == ("alpha", "beta", "gamma")

    # Single value: still a one-tuple.
    monkeypatch.setenv(EXT_CHECKOUT_ENV_VAR, "delta")
    assert external_names() == ("delta",)

    # Empty segments (trailing/leading commas) are dropped.
    monkeypatch.setenv(EXT_CHECKOUT_ENV_VAR, "epsilon,,zeta,")
    assert external_names() == ("epsilon", "zeta")
