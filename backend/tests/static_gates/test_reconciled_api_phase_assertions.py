"""The reconciled API tests must speak the merged waiting phase: ``queued``.

Why this gate exists
--------------------
The waiting phase used to be called ``ready``. It is now called
``queued`` — one word for one situation, decided in
``backend/phase_vocabulary.py`` and written by the two sides that own
the phase:

* the fast lane's import route parks a new plan in ``queued``
  (``backend/routes/fast_lane.py::_mark_queued``), and
* the state write entry point normalises the historical alias ``ready``
  into ``queued`` (``backend/routes/phases.py::update_plan_state``).

Three API test files still asserted the old word:

* ``backend/tests/api/test_plan_import_api.py`` — the import lands in
  ``ready``; the refused imports must leave no ``ready`` row behind;
* ``backend/tests/api/test_plan_revise_api.py`` — a revision leaves the
  plan in the phase the import gave it;
* ``backend/tests/test_data_flow.py`` — the state entry point answers
  with the phase it stored.

A test that asserts the old word is not a test that fails loudly; it is
a test that asserts the *old* behaviour is still correct. Left alone,
those three files would have turned the merge into a green lie: the
product writes ``queued``, the tests insist on ``ready``, and the only
thing that fails is the test run — for a reason that reads like "the
feature is broken" when in fact the test is out of date. The worse
failure is the one where somebody "fixes" it by widening the assertion
to ``in ("ready", "queued")``: the test then passes under either
vocabulary, so it no longer pins anything at all, and the merge can be
undone by the next commit without a single test noticing.

The rule
--------
The three reconciled files assert the waiting phase as ``queued``, and
nothing in them asserts it as ``ready`` any more. The negative control
says "not ``queued``" rather than "not ``ready``", because the question
it answers is "did the refused import leave a plan behind?", and the
phase a plan is parked in is now ``queued`` — a control written against
the old word would pass for the wrong reason.

What is deliberately *not* checked
----------------------------------
``ready`` is still a legal **input** to the state endpoint: it is the
historical alias the normalisation exists to absorb. ``test_data_flow.py``
still posts ``{"current_phase": "ready"}`` — that is the compatibility
path being tested, and rewriting it to ``queued`` would make the test
assert that a name reads back unchanged instead of that the old name is
normalised. So this gate looks at *assertions*, never at request
bodies: a ``current_phase`` value that is compared for equality must
never be ``ready``.

The same word is also still a status *bucket* (「就绪」), which is a
different concept and lives in a different table. Nothing here touches
the buckets.

Scan target
-----------
The three test files, read as source. This is a shape gate, like the
rest of ``static_gates/``: it pins the contract in the text rather than
executing the tests it inspects, so the alignment cannot rot back into
a stale assertion without a gate noticing. The behaviour itself is
pinned by those files' own runs.
"""

from __future__ import annotations

import re
from pathlib import Path


#: ``backend/tests/`` — the directory the three reconciled files live in.
_TESTS_DIR = Path(__file__).resolve().parents[1]

#: The files this gate reconciles. The key is what the file is *for*;
#: the value is where it lives relative to ``backend/tests/``.
_RECONCILED_FILES: dict[str, Path] = {
    "the import lane (backend/tests/api/test_plan_import_api.py)": (
        _TESTS_DIR / "api" / "test_plan_import_api.py"
    ),
    "the revise lane (backend/tests/api/test_plan_revise_api.py)": (
        _TESTS_DIR / "api" / "test_plan_revise_api.py"
    ),
    "the state entry point (backend/tests/test_data_flow.py)": (
        _TESTS_DIR / "test_data_flow.py"
    ),
}

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: An assertion that compares a ``current_phase`` to ``queued``.
#: ``[^\n]*`` keeps the match inside one line, so a match cannot be
#: stitched together out of two unrelated lines.
_QUEUED_ASSERTION = re.compile(
    r"""assert\b[^\n]*\[["']current_phase["']\]\s*==\s*["']queued["']"""
)

#: An assertion that compares a ``current_phase`` to ``ready``. This is
#: the shape the merge removes.
_READY_ASSERTION = re.compile(
    r"""assert\b[^\n]*\[["']current_phase["']\]\s*==\s*["']ready["']"""
)

#: Any assertion at all that mentions ``current_phase`` and ``ready`` —
#: a weaker net than the one above, catching ``!=`` and ``in`` forms.
_READY_ASSERTION_LOOSE = re.compile(
    r"""assert\b[^\n]*current_phase[^\n]*["']ready["']"""
)

#: A comparison that accepts *either* word — the "widened" assertion
#: that stops pinning anything.
_BOTH_WORDS_ACCEPTED = re.compile(
    r"""\bin\s*\(\s*[^)\]]*["']ready["'][^)\]]*["']queued["'][^)\]]*\)"""
    r"""|\bin\s*\(\s*[^)\]]*["']queued["'][^)\]]*["']ready["'][^)\]]*\)"""
)

#: The negative control's comparison: "this plan is *not* parked".
_NOT_QUEUED_ASSERTION = re.compile(
    r"""assert\b[^\n]*\[["']current_phase["']\]\s*!=\s*["']queued["']"""
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError`` — a gate that dies on a
    malformed byte reports nothing useful about the contract it was
    written to protect.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _matches(pattern: re.Pattern[str], source: str) -> list[str]:
    """Return the source lines that ``pattern`` matches, trimmed.

    Only the line is returned, never the matched span's neighbours: the
    failure message says *where* to look, and the reader opens the file.
    """
    return [
        line.strip()
        for line in source.splitlines()
        if pattern.search(line)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconciled_api_tests_assert_queued() -> None:
    """Every reconciled file must assert the waiting phase as ``queued``.

    Not "some file somewhere says ``queued``": each of the three has to
    carry its own queued-phase assertion, because each one is a
    different claim about a different entry point — the import lane's
    landing phase, the revise lane's "the phase did not move" claim,
    and the state entry point's answer.
    """
    for description, path in _RECONCILED_FILES.items():
        source = _source(path)

        matched = _matches(_QUEUED_ASSERTION, source)
        assert matched, (
            f"{description} asserts no `queued` phase. The waiting phase "
            f"is `queued` now (the import lane parks a plan there, and "
            f"the state entry point normalises `ready` into it), so the "
            f"file's phase assertion has to say so."
        )


def test_reconciled_api_tests_have_no_ready_waiting_assertion() -> None:
    """No reconciled file may assert the waiting phase as ``ready``.

    The file that still asserts ``ready`` is a file that says the
    merge did not happen. It does not merely describe the old
    behaviour — it *requires* it, so the product can only satisfy it by
    going back.

    The loose net (any assertion mentioning both ``current_phase`` and
    ``ready``) is checked as well, so that a ``!=`` or an ``in`` cannot
    smuggle the old word back into an assertion. Request bodies are
    out of scope: posting ``ready`` is the compatibility path, and the
    normalisation is what makes it work.

    The widening is called out by name, because it is the shape that
    looks like a fix and is not one: ``in ("ready", "queued")`` passes
    under the old vocabulary and the new one, so it pins nothing, and
    the merge can be undone by the next commit without a single test
    noticing. A reconciled file asserts one word — the word the product
    writes.
    """
    for description, path in _RECONCILED_FILES.items():
        source = _source(path)

        strict = _matches(_READY_ASSERTION, source)
        assert not strict, (
            f"{description} still asserts the waiting phase as `ready`. "
            f"The waiting phase is `queued` now; an assertion on `ready` "
            f"requires the merge to be undone."
        )

        loose = _matches(_READY_ASSERTION_LOOSE, source)
        assert not loose, (
            f"{description} still mentions `ready` inside an assertion "
            f"about `current_phase`. The waiting phase is `queued` now; "
            f"only the *input* side may still say `ready`."
        )

        widened = _matches(_BOTH_WORDS_ACCEPTED, source)
        assert not widened, (
            f"{description} accepts both `ready` and `queued` for the "
            f"same comparison. That assertion passes under either "
            f"vocabulary, so it pins nothing."
        )


def test_import_test_name_mentions_queued() -> None:
    """The import test's name must name the phase it pins.

    A test called ``..._lands_as_a_ready_plan`` that asserts ``queued``
    is a test whose name contradicts its body, and the name is what a
    reader (or a CI log) reads first. Renaming it is what keeps the
    two from drifting apart again.
    """
    source = _source(_RECONCILED_FILES["the import lane (backend/tests/api/test_plan_import_api.py)"])

    assert "def test_material_of_any_shape_lands_as_a_queued_plan(" in source, (
        "the import lane's test is not named "
        "`test_material_of_any_shape_lands_as_a_queued_plan` any more. "
        "The name has to name the phase the test pins."
    )

    assert "lands_as_a_ready_plan" not in source, (
        "the import lane's test still carries the old name "
        "`..._lands_as_a_ready_plan`, which names the phase the merge "
        "removed."
    )


def test_import_negative_control_is_not_queued() -> None:
    """The refused-import control must say "not ``queued``".

    The control's question is "did the refused import leave a plan
    behind?". Answering it against ``ready`` would be answering it
    about a phase the product no longer parks anything in: the control
    would pass for a plan that was wrongly parked in ``queued``, which
    is exactly the failure it exists to catch.
    """
    source = _source(_RECONCILED_FILES["the import lane (backend/tests/api/test_plan_import_api.py)"])

    assert "def _assert_not_queued(" in source, (
        "the import lane's negative control is not named "
        "`_assert_not_queued` any more; it still answers a question "
        "about the phase the merge removed."
    )

    assert "_assert_not_ready" not in source, (
        "the import lane still refers to `_assert_not_ready`. The "
        "control's name and the phase it names must both be `queued`."
    )

    matched = _matches(_NOT_QUEUED_ASSERTION, source)
    assert matched, (
        "the import lane's negative control does not compare "
        "`current_phase` against `queued`. A refused import must leave "
        "no plan parked in `queued` behind."
    )
