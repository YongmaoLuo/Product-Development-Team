"""The retired verdict format must not come back.

The terminal question changed shape on 2026-11-08: when the judge
produces no analysis the question is the **fallback card**, not the
bare sentence of facts.  The sentence wrote a failed point as
``VP-3=FAILED``; the card writes it as ``VP-3 FAILED`` and it says
**why** there is no analysis — the ``analysis_error`` key — instead of
silently dropping the analysis.

This file is the guard for that rewrite.  It reads the four test
modules that used to assert the retired format and pins two things:

* none of them still writes the retired verdict format.  A test that
  quotes it keeps the old shape alive in the suite even after the
  production code has moved on — and the old shape is the one a reader
  of the card would never see.
* the two modules that assert the payload's shape name
  ``analysis_error``.  That key is what says why there is no analysis;
  a payload test that asserts the shape without naming the key would
  let it be dropped silently, and the round's own record would go
  quiet about the missing analysis.

The guard covers exactly the four modules named below.  A module it
does not name is not covered by it.
"""

from pathlib import Path


#: Where ``tests/`` is — this file lives in ``tests/unit/``.
_TESTS_DIR = Path(__file__).resolve().parents[1]

#: The modules that used to carry the retired verdict format.
_GUARDED_MODULES = (
    "unit/test_terminal_decision_question.py",
    "unit/test_terminal_decision_brief_wiring.py",
    "unit/test_terminal_failed_vp_selection.py",
    "integration/verification_chain/test_terminal_operator_decision.py",
)

#: The modules that assert the payload's shape.
_PAYLOAD_MODULES = (
    "unit/test_terminal_decision_question.py",
    "unit/test_terminal_decision_brief_wiring.py",
)


def _read(relative_path: str) -> str:
    """One module's text, read relative to ``tests/``."""
    return (_TESTS_DIR / relative_path).read_text(encoding="utf-8")


def test_no_guarded_module_keeps_the_retired_verdict_format():
    """None of the four modules still writes ``VP-3=FAILED``."""
    for relative_path in _GUARDED_MODULES:
        text = _read(relative_path)
        assert "=FAILED" not in text, (
            f"{relative_path} still writes the retired verdict format "
            f"(=FAILED); the fallback card writes 'VP-3 FAILED'"
        )


def test_payload_assertions_name_the_analysis_error_key():
    """Both payload modules name the fifth key — ``analysis_error``."""
    for relative_path in _PAYLOAD_MODULES:
        text = _read(relative_path)
        assert "analysis_error" in text, (
            f"{relative_path} asserts the payload's shape without naming "
            f"analysis_error — the key that says why there is no analysis"
        )
