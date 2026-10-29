"""TDD spec for the plan-id scrubber in :mod:`framework.ids`.

Why the scrubber exists
-----------------------

A plan id names a private project. The repository's gates forbid it in
commit messages, in branch names and in pull-request text, because
those surfaces are permanent public history: GitHub keeps
``refs/pull/<n>/head`` forever, so a force-push cannot take a leaked id
back, and deleting a ref does not delete objects.

The executor writes commit messages on its own -- one checkpoint per
completed task -- and the message is built from the task's free-text
description, which routinely names the plan the task belongs to (the
report path it has to write lives under the plan directory). The
scrubber is how that message is cleaned before it reaches git.

What this file pins down:

1. The production shape is the SAME shape the static gate uses. The two
   live in different layers -- the gate is test source that has to stay
   importable by a bare ``python3`` from the commit-msg hook, the
   scrubber is production code -- so the copy is pinned by a test rather
   than left to good intentions.
2. ``find_plan_ids`` reports every id-shaped span, in order.
3. ``scrub_plan_ids`` replaces an id with a neutral placeholder and
   leaves clean text byte-identical.
4. The scrubbed text carries no plan-id shape any more -- the property
   the gate actually checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
GATES_DIR = BACKEND_DIR / "tests" / "static_gates"
for _path in (str(BACKEND_DIR), str(GATES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The gate's own pattern, used as the oracle for "does this text still
# carry a plan id". Imported rather than restated so the two definitions
# cannot drift apart silently.
from test_no_plan_ids_in_non_test_source import PLAN_ID_SHAPE  # noqa: E402


def _plan_id(topic: str) -> str:
    """A synthetic plan id, assembled so this file never holds one whole."""
    return "2026" + f"0101-{topic}"


def test_the_production_shape_matches_the_gate_shape():
    """One rule, two layers -- pinned so the copies cannot drift.

    The gate is the enforcement point; the scrubber is the prevention.
    If the prevention used a narrower shape than the gate, an id-shaped
    string could slip through the scrubber and still trip the gate.
    """
    from framework import ids

    assert ids.PLAN_ID_SHAPE.pattern == PLAN_ID_SHAPE.pattern


def test_find_plan_ids_reports_every_id_in_order():
    """The scanner sees all of them, and sees them in the order they appear."""
    from framework import ids

    first = _plan_id("alpha")
    second = _plan_id("beta-topic")
    text = f"first {first} then {second} then a plan without a shape"

    found = ids.find_plan_ids(text)

    assert found == [first, second], found


def test_find_plan_ids_returns_nothing_for_clean_text():
    """Clean text has no ids -- the no-op case the scrub depends on."""
    from framework import ids

    assert ids.find_plan_ids("a description that names no plan") == []


def test_scrub_plan_ids_replaces_ids_with_a_neutral_placeholder():
    """The id is replaced, not merely deleted.

    Deleting it would leave a hole where the reader used to be told
    something; a placeholder says "there was a private id here" without
    saying what it was.
    """
    from framework import ids

    plan_id = _plan_id("alpha")
    scrubbed = ids.scrub_plan_ids(f"write the report under plans/{plan_id}/")

    assert plan_id not in scrubbed, scrubbed
    assert ids.PLAN_ID_PLACEHOLDER in scrubbed, scrubbed


def test_scrub_plan_ids_leaves_clean_text_byte_identical():
    """A no-op must be a no-op, character for character.

    Every checkpoint commit in the repository that has nothing to hide
    must produce exactly the message it produced before the scrubber
    existed.
    """
    from framework import ids

    original = "[task-x] do a thing\n\nDescription: nothing private here\n"

    assert ids.scrub_plan_ids(original) == original


def test_scrubbed_text_carries_no_plan_id_shape():
    """The property the gate checks, asserted on the gate's own pattern."""
    from framework import ids

    plan_id = _plan_id("alpha")
    scrubbed = ids.scrub_plan_ids(f"{plan_id} and {_plan_id('beta')}")

    assert not PLAN_ID_SHAPE.search(scrubbed), scrubbed
