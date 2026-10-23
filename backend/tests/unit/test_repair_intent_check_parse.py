"""Unit tests for the ``intent_check`` verdict carried by a repair task.

Why this exists
---------------
The repair prompt tells the model about the plan's own hard constraints
("不碰 Rust") and asks it to report a conflict instead of silently
crossing it. But a *narrative* conflict report is indistinguishable
from a model that never saw the constraint at all: both arrive as an
ordinary task description. Downstream code therefore cannot tell
"the model judged this out of scope" apart from "the model did not
notice" — and the two need different handling.

The structured ``intent_check`` block is what makes them separable: the
model answers with ``violates / constraint / reason`` and the parser
carries that answer through verbatim, so a downstream reader sees
``violates=true`` as a machine-checkable fact rather than as prose.

What is pinned here
-------------------
* a well-formed ``intent_check`` survives the parse unchanged;
* a missing block falls back to "no violation" (not an exception);
* a string ``"true"`` counts as a violation — models emit booleans as
  strings often enough that only accepting real ``True`` would lose the
  signal;
* a malformed block (not a dict) falls back to "no violation" instead
  of raising.

No network / database — pure in-process unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import repair_generator as rg  # noqa: E402


#: The fallback every malformed / missing block degrades to.
DEFAULT_INTENT_CHECK = {"violates": False, "constraint": "", "reason": ""}


def _failed_vp() -> dict:
    """One failed verification point, in the legacy inlined shape."""
    return {
        "id": "VP-1",
        "title": "x",
        "priority": "medium",
        "actual_result": "boom",
        "evidence": "trace",
    }


def _parse(task: dict) -> dict:
    """Run one task through the response parser."""
    contents = rg._parse_repair_contents_response({"tasks": [task]}, [_failed_vp()])
    assert len(contents) == 1
    return contents[0]


# ---------------------------------------------------------------------------
# parse-level behaviour
# ---------------------------------------------------------------------------


def test_intent_check_carried_through():
    """A well-formed block reaches the parsed content unchanged."""
    intent_check = {
        "violates": True,
        "constraint": "不碰 Rust",
        "reason": "必须改 engine_rs",
    }

    content = _parse({
        "failed_vp_id": "VP-1",
        "title": "t",
        "description": "d",
        "intent_check": intent_check,
    })

    assert content["intent_check"] == {
        "violates": True,
        "constraint": "不碰 Rust",
        "reason": "必须改 engine_rs",
    }


def test_missing_intent_check_defaults_to_false():
    """A model that says nothing is not treated as a violation."""
    content = _parse({
        "failed_vp_id": "VP-1",
        "title": "t",
        "description": "d",
    })

    assert content["intent_check"] == DEFAULT_INTENT_CHECK


def test_string_violates_true_normalised():
    """``"true"`` (in any case) counts as a violation."""
    content = _parse({
        "failed_vp_id": "VP-1",
        "title": "t",
        "description": "d",
        "intent_check": {
            "violates": "true",
            "constraint": " 不碰 Rust ",
            "reason": " 必须改 engine_rs ",
        },
    })

    assert content["intent_check"]["violates"] is True
    assert content["intent_check"]["constraint"] == "不碰 Rust"
    assert content["intent_check"]["reason"] == "必须改 engine_rs"


def test_malformed_intent_check_defaults():
    """A non-dict block degrades to the default instead of raising."""
    content = _parse({
        "failed_vp_id": "VP-1",
        "title": "t",
        "description": "d",
        "intent_check": "violates",
    })

    assert content["intent_check"] == DEFAULT_INTENT_CHECK


# ---------------------------------------------------------------------------
# normaliser
# ---------------------------------------------------------------------------


def test_normalise_missing_value_returns_default():
    """``None`` is the normal "the model said nothing" case."""
    assert rg._normalise_intent_check(None) == DEFAULT_INTENT_CHECK


def test_normalise_accepts_yes():
    """``"yes"`` is another spelling of a violation."""
    normalised = rg._normalise_intent_check({"violates": "YES"})

    assert normalised["violates"] is True


def test_normalise_rejects_other_truthy_values():
    """Only the documented spellings count as a violation."""
    for value in ("no", "false", 1, 0, [], {}, "true story"):
        normalised = rg._normalise_intent_check({"violates": value})

        assert normalised["violates"] is False, value


def test_normalise_keeps_only_the_three_known_keys():
    """Extra keys from the model do not leak into the parsed content."""
    normalised = rg._normalise_intent_check({
        "violates": True,
        "constraint": "c",
        "reason": "r",
        "surprise": "extra",
    })

    assert set(normalised) == {"violates", "constraint", "reason"}


def test_normalise_stringifies_and_strips_text_fields():
    """A non-string constraint / reason is rendered as stripped text."""
    normalised = rg._normalise_intent_check({
        "violates": False,
        "constraint": 123,
        "reason": "  理由  ",
    })

    assert normalised["constraint"] == "123"
    assert normalised["reason"] == "理由"


# ---------------------------------------------------------------------------
# system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_shows_the_intent_check_shape():
    """The model is told the exact block it has to produce."""
    assert '"intent_check"' in rg.REPAIR_CONTENT_SYSTEM_PROMPT
    assert '"violates"' in rg.REPAIR_CONTENT_SYSTEM_PROMPT
    assert '"constraint"' in rg.REPAIR_CONTENT_SYSTEM_PROMPT
    assert '"reason"' in rg.REPAIR_CONTENT_SYSTEM_PROMPT


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))
