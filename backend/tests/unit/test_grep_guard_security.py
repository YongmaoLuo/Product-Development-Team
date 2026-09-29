"""Security / bypass-mode test for the grep-guard scanner.

Background
----------
The grep-guard scanner (``backend/tests/grep_guard.py::run_grep_guard``)
applies 5 pattern classes against the production source tree:

    1. direct_literal     — ``"<forbidden>.json"``
    2. string_concat      — ``"..." + "...".json`` (AST walk)
    3. variable_reference — ``_RT_FN_*`` / ``progress_state_file`` / etc.
    4. fstring_format     — ``f"...{x}...json"`` / ``.format(...json)``
    5. pathlib_join       — ``Path(...) / "<name>.json"``

The original 5 grep-gate tests in
``backend/tests/test_server_json_cleanup.py`` only pin Defense 1
against the production tree — i.e. "no hits → PASS".  They do not
exercise the scanner against synthetic bypass patterns, so a
regression that produces FALSE NEGATIVES on adversarial inputs
would not surface from those tests alone.

This file pins the scanner's **detection** contract by feeding it
the exact bypass patterns a developer might try when trying to
evade the gate, and asserting the scanner catches every variant.

TDD contract
------------
- ``test_grep_guard_catches_concat_variants``: build three synthetic
  fixtures, each planting a DIFFERENT string-concat shape that
  builds a forbidden filename when joined.  The scanner must return
  a ``string_concat`` violation for each fixture.

The three string-concat shapes exercised here cover the bypass
modes the test brief calls out:

  Variant 1 (binary split) — ``"executi" + "on.json"``
    Two adjacent literals on a single line; the joined value is
    ``"execution.json"`` which is in the forbidden basename list.

  Variant 2 (variable + literal) — ``prefix + ".json"``
    A binary ``Add`` whose left operand is a non-literal expression
    and right operand is the literal ``".json"``.  The joined
    value of the literal operands is ``"<prefix>.json"`` which the
    scanner collects via its AST walk — if the joined string
    contains a forbidden basename the gate fires.  This shape
    frequently appears when a developer splits the suffix from the
    basename to bypass naive regex-based detectors; the AST-based
    walker in ``grep_guard`` must catch it.

  Variant 3 (3-way concat) — ``"plan_" + "state" + ".json"``
    Three adjacent ``+`` operations on a single line; the joined
    value is ``"plan_state.json"``.  This shape pins the AST
    walker's left-chain traversal: a naive detector that only
    looked at the right operand would miss the middle literal
    ``"state"``.

Boundary conditions enforced:

  * every detected violation carries ``pattern_type == "string_concat"``;
  * the scanner does NOT misattribute a string-concat hit to
    ``direct_literal`` (or any other class) — pattern_type
    discrimination is part of the contract;
  * the matched_text field carries the joined forbidden basename
    (not just the literal segments) so the failure message is
    useful for the developer who triggered the gate;
  * each violation's ``file`` field points to the fixture
    basename (the scanner normalises paths via ``root``).

This test is a unit test (no subprocess / no live server) so it
runs in the default pytest layer (no marker needed).  Marking it
``@pytest.mark.unit`` keeps the layer filter explicit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure ``backend/tests/`` is on sys.path so the standalone
# ``grep_guard`` module is importable.
_BACKEND_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_TESTS_DIR))


from grep_guard import run_grep_guard  # noqa: E402


# Variant 1 — the canonical "binary split" bypass: two adjacent
# literals on a single line whose concatenation produces
# ``"execution.json"``.  This is the shape the production
# ``_pattern_string_concat`` helper explicitly targets.
FIXTURE_CONCAT_BINARY_SPLIT = (
    '"""Synthetic fixture — string_concat variant 1."""\n'
    'EXEC_FILE = "executi" + "on.json"\n'
)


# Variant 2 — variable on the LEFT + literal suffix on the RIGHT.
# The right operand is a literal whose value contains
# ``".json"``; the scanner's AST walker must collect it.  The
# joined literal segment contains ``".json"`` but does NOT contain
# a forbidden basename on its own — so the walker must rely on
# the whole-context ``Add`` chain (the variable-named ``prefix``
# is what gives the file its meaning at runtime).  To exercise
# the gate positively we plant the literal ``"plan_state"`` on
# the LEFT and ``".json"`` on the RIGHT — the joined literal
# value ``"plan_state.json"`` IS a forbidden basename.
FIXTURE_CONCAT_VAR_PLUS_LITERAL = (
    '"""Synthetic fixture — string_concat variant 2 (var + literal)."""\n'
    'PREFIX_LITERAL = "plan_state"\n'
    'PLAN_STATE_FILE = PREFIX_LITERAL + ".json"\n'
)


# Variant 3 — three-way concat: ``"plan_" + "state" + ".json"``.
# The naive detector that only looks at adjacent pairs would catch
# the first two literals but miss the trailing ``".json"`` because
# the BinOp chain on the left of ``"state" + ".json"`` wraps the
# earlier ``"plan_" + "state"`` segment.  The scanner's left-chain
# AST walker must visit ``"plan_"``, ``"state"``, and ``".json"``
# in order and join them into ``"plan_state.json"`` for the gate
# to fire.
FIXTURE_CONCAT_THREE_WAY = (
    '"""Synthetic fixture — string_concat variant 3 (3-way)."""\n'
    'PLAN_STATE_FILE = "plan_" + "state" + ".json"\n'
)


# The three fixtures, paired with the forbidden basename that must
# appear in the joined literal value.  Each is a distinct bypass
# shape; the scanner must catch all three.
CONCAT_VARIANTS: list[tuple[str, str, str]] = [
    # (filename, fixture_content, forbidden_basename_in_joined_value)
    ("concat_binary_split.py", FIXTURE_CONCAT_BINARY_SPLIT, "execution.json"),
    ("concat_var_plus_literal.py", FIXTURE_CONCAT_VAR_PLUS_LITERAL, "plan_state.json"),
    ("concat_three_way.py", FIXTURE_CONCAT_THREE_WAY, "plan_state.json"),
]


def _write_fixture(tmp_path: Path, filename: str, content: str) -> Path:
    """Write ``content`` to ``tmp_path/filename`` and return the path."""
    target = tmp_path / filename
    target.write_text(content, encoding="utf-8")
    return target


@pytest.mark.unit
def test_grep_guard_catches_concat_variants(tmp_path: Path) -> None:
    """Three string-concat bypass shapes → all reported as string_concat.

    Builds three synthetic fixtures, each planting a DIFFERENT
    string-concat shape that, when joined, produces a forbidden
    filename.  Invokes ``run_grep_guard`` against the tmp tree and
    asserts:

      1. every fixture's planted violation is reported with
         ``pattern_type == "string_concat"``;
      2. the matched_text field carries the joined forbidden
         basename (or a substring that contains it);
      3. every violation's file path resolves to the planted
         fixture's basename (the scanner normalises paths via the
         ``root`` parameter);
      4. the scanner does NOT misattribute the string-concat hits
         to ``direct_literal`` (or any other class) — pattern_type
         discrimination is part of the contract;
      5. exactly one ``string_concat`` violation per fixture is
         reported (the scanner does not double-count adjacent
         literal pairs on the same line — that would inflate the
         downstream repair-task count).

    A regression in any of these contracts (e.g. a naive regex
    rewrite that only catches binary-split shapes, or a pattern-
    type label swap) would fail this test.

    This is the canonical "the scanner's AST walker covers all
    three string-concat bypass modes" contract — a downstream
    developer who tries to evade the gate by splitting the
    forbidden basename across multiple ``+`` operations must still
    be caught.
    """
    written_paths = [
        _write_fixture(tmp_path, filename, content)
        for filename, content, _ in CONCAT_VARIANTS
    ]

    violations = run_grep_guard(
        root=tmp_path,
        target_files=None,
        target_dirs=[tmp_path],
    )

    # ---- Shape: every violation is a properly-keyed dict ------------
    assert isinstance(violations, list)
    for v in violations:
        assert isinstance(v, dict)
        assert set(v.keys()) == {"file", "line", "pattern_type", "matched_text"}
        assert isinstance(v["file"], str)
        assert isinstance(v["line"], int) and v["line"] >= 1
        assert v["pattern_type"] in {
            "direct_literal",
            "string_concat",
            "variable_reference",
            "fstring_format",
            "pathlib_join",
        }, f"unknown pattern_type {v['pattern_type']!r}"
        assert isinstance(v["matched_text"], str) and v["matched_text"]

    # ---- Per-fixture: exactly one string_concat hit per fixture -----
    # Group violations by fixture basename.
    by_fixture: dict[str, list[dict]] = {}
    for v in violations:
        by_fixture.setdefault(Path(v["file"]).name, []).append(v)

    for fixture_path in written_paths:
        fixture_basename = fixture_path.name
        fixture_violations = by_fixture.get(fixture_basename, [])
        assert fixture_violations, (
            f"run_grep_guard did NOT report any violation for concat "
            f"fixture {fixture_basename!r} — the scanner silently "
            f"skipped this bypass mode. Observed fixture files: "
            f"{sorted(by_fixture.keys())!r}"
        )

        # Filter for string_concat only — the scanner may have
        # multiple violations per fixture (e.g. a variable_reference
        # on a helper line), so we narrow the assertion to the
        # class we care about.
        string_concat_hits = [
            v for v in fixture_violations if v["pattern_type"] == "string_concat"
        ]
        assert string_concat_hits, (
            f"fixture {fixture_basename!r} did NOT produce a "
            f"string_concat violation — the bypass mode was missed. "
            f"Observed violations for this fixture: "
            f"{[v['pattern_type'] for v in fixture_violations]!r}"
        )

        # Exactly ONE string_concat violation per fixture: the
        # AST walker must not double-count adjacent literal pairs
        # on the same line.  Multiple hits on the SAME line for
        # the same fixture inflate the downstream repair-task
        # count and confuse the developer.
        assert len(string_concat_hits) == 1, (
            f"fixture {fixture_basename!r} produced "
            f"{len(string_concat_hits)} string_concat hits; "
            f"the AST walker must coalesce adjacent literals into a "
            f"single violation per Add-chain. Hits: {string_concat_hits!r}"
        )

        # The matched_text field must contain the joined forbidden
        # basename (or at least a substring that contains the
        # forbidden name).  We check for substring containment so
        # the scanner has freedom over exactly how it formats the
        # joined value, as long as the developer-facing message is
        # useful.
        forbidden_basename = next(
            basename
            for filename, _, basename in CONCAT_VARIANTS
            if filename == fixture_basename
        )
        joined = string_concat_hits[0]["matched_text"]
        assert forbidden_basename in joined, (
            f"fixture {fixture_basename!r}: string_concat hit's "
            f"matched_text {joined!r} does NOT contain the expected "
            f"forbidden basename {forbidden_basename!r}. The joined "
            f"value must include the forbidden filename so the "
            f"failure message is useful for the developer."
        )

    # ---- Total string_concat count across all 3 fixtures = 3 -------
    all_string_concat_hits = [
        v for v in violations if v["pattern_type"] == "string_concat"
    ]
    assert len(all_string_concat_hits) == len(CONCAT_VARIANTS), (
        f"expected exactly {len(CONCAT_VARIANTS)} string_concat "
        f"violations (one per fixture), got "
        f"{len(all_string_concat_hits)}; mismatched count means the "
        f"AST walker either over- or under-counts. All hits: "
        f"{all_string_concat_hits!r}"
    )

    # ---- No pattern_type discrimination regression ------------------
    # The string_concat fixtures must NOT be misattributed to
    # direct_literal.  This is the cleanest way to lock in the
    # pattern_type labelling: every string_concat fixture's hit
    # carries exactly pattern_type=string_concat.
    for fixture_path in written_paths:
        fixture_basename = fixture_path.name
        hits = by_fixture.get(fixture_basename, [])
        assert all(
            v["pattern_type"] == "string_concat" for v in hits
            if "json" in v["matched_text"]
        ), (
            f"fixture {fixture_basename!r}: scanner misattributed a "
            f"string-concat bypass to a different pattern class. Hits: "
            f"{hits!r}"
        )