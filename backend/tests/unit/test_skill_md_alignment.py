"""TDD tests for ``.claude/skills/req-clarification/SKILL.md`` alignment.

These tests pin the contract that the ``req-clarification`` SKILL.md
documents three methodology details introduced by DP8 so that the LUI
(Language User Interface) mode of Claude Code can stay aligned with the
backend ``interviewer.py`` runtime behaviour.

DP8 adds three pieces of behaviour to ``backend/interviewer.py``:

  1. ``scope_precheck`` — a single LLM call at the start of an
     interview that classifies the user's intent into a 4-class
     uppercase ``product_form`` enum
     (``SOFTWARE`` / ``SKILL`` / ``AGENT`` / ``WORKFLOW``) and
     proposes draft ``in_scope`` / ``out_of_scope`` lists.
  2. ``next_question`` — a one-question-per-turn loop. Each call to
     ``next_question`` returns AT MOST one question, advancing the
     state machine one step at a time (replacing the old
     batch-of-3-questions approach).
  3. ``product_form`` field on the interview JSON, with the
     4-class uppercase enum.

For LUI mode (Claude Code invoking the skill directly via natural
language) to behave consistently with the backend, the SKILL.md MUST
document all three pieces of behaviour.

TDD spec (3 tests):

  - test_skill_md_has_scope_precheck_section:
        grep SKILL.md for the literal substring ``Scope Pre-check``
        (the section header introduced by this task).

  - test_skill_md_has_one_question_per_turn:
        grep SKILL.md for the literal substring ``一次一个问题`` (the
        one-question-per-turn contract that the backend now enforces).

  - test_skill_md_has_product_form_enum:
        grep SKILL.md for the 4-class product_form enum
        ``SOFTWARE`` / ``SKILL`` / ``AGENT`` / ``WORKFLOW`` — at least
        all 4 tokens must appear somewhere in the file (case-sensitive).

These tests are deliberately grep-based (not AST-based). The skill
file is documentation, not code; the contract is "the SKILL.md must
mention these strings", and a grep is the simplest reliable way to
enforce it. They also tolerate any future refactor that keeps the
strings present (e.g. moving them between sections).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_MD_PATH = (
    Path(__file__).resolve().parents[3]
    / ".claude"
    / "skills"
    / "req-clarification"
    / "SKILL.md"
)

# 4-class product_form enum (must be uppercase, per backend
# ``interviewer.SCOPE_PRECHECK_VALID_FORMS``).
EXPECTED_PRODUCT_FORM_ENUM = ("SOFTWARE", "SKILL", "AGENT", "WORKFLOW")


@pytest.fixture(scope="module")
def skill_md_text() -> str:
    """Read SKILL.md once per test module (avoid re-reading per test).

    The file is small (< 200 lines), so reading it once is plenty.
    If the file is missing, raise a clear error (the test for that
    is implicit — every test in this module will fail with the
    same FileNotFoundError, surfacing the root cause immediately).
    """
    if not SKILL_MD_PATH.exists():
        raise FileNotFoundError(
            f"SKILL.md not found at expected path: {SKILL_MD_PATH}"
        )
    return SKILL_MD_PATH.read_text(encoding="utf-8")


def test_skill_md_has_scope_precheck_section(skill_md_text: str) -> None:
    """SKILL.md must document the ``Scope Pre-check`` first-round
    methodology introduced by DP8-1.

    The backend ``interviewer.scope_precheck`` runs BEFORE the
    6-dimension framework is iterated, classifying the user's
    intent into a 4-class ``product_form`` enum and proposing draft
    in/out-of-scope lists. LUI mode (Claude Code calling the skill)
    must follow the same flow — otherwise the LUI interview
    diverges from the backend and downstream phases (PRD,
    architecture) consume inconsistent inputs.

    The literal section header ``## Scope Pre-check（首轮）`` is the
    canonical name. We match the ASCII substring ``Scope Pre-check``
    (case-sensitive, no whitespace) so the test passes regardless of
    the exact punctuation / fullwidth characters used around the
    header.
    """
    assert "Scope Pre-check" in skill_md_text, (
        f"SKILL.md ({SKILL_MD_PATH}) is missing the "
        f"'Scope Pre-check' section header; DP8-1 introduced "
        f"interviewer.scope_precheck as the first round of "
        f"requirement clarification, and the LUI mode must "
        f"document this so Claude Code can mirror the backend flow. "
        f"Expected header: '## Scope Pre-check（首轮）'."
    )


def test_skill_md_has_one_question_per_turn(skill_md_text: str) -> None:
    """SKILL.md must document the ``一次一个问题`` (one-question-per-
    turn) contract introduced by DP8-2.

    The backend ``interviewer.next_question`` returns AT MOST one
    question per call (replacing the prior batch-of-3 approach).
    This keeps the LLM state machine easy to audit and makes the
    interview JSON schema ``chat_history`` clean (one
    user-message / one assistant-question per row).

    LUI mode must not regress to "ask 3 questions per turn" — the
    SKILL.md must explicitly call out the one-question rule. The
    literal substring ``一次一个问题`` is the canonical contract
    name in the project's documentation.
    """
    assert "一次一个问题" in skill_md_text, (
        f"SKILL.md ({SKILL_MD_PATH}) is missing the '一次一个问题' "
        f"contract; DP8-2 changed interviewer.next_question to "
        f"return one question per turn (replacing batch-of-3), "
        f"and the LUI mode must follow the same rule. Expected "
        f"section header: '## 一次一个问题（默认）'."
    )


def test_skill_md_has_product_form_enum(skill_md_text: str) -> None:
    """SKILL.md must document the 4-class uppercase ``product_form``
    enum introduced by DP8-3.

    The backend ``interviewer.SCOPE_PRECHECK_VALID_FORMS`` constrains
    ``product_form`` to one of 4 uppercase strings:
    ``SOFTWARE``, ``SKILL``, ``AGENT``, ``WORKFLOW``. The SKILL.md
    must surface all 4 tokens (case-sensitive) so LUI mode writes
    the correct enum value into ``interview.json.product_form``.

    We check ``re.search`` of each token individually and aggregate
    missing tokens into a single failure message — easier to debug
    than 4 separate ``assert`` calls that stop at the first miss.
    """
    missing = [
        form for form in EXPECTED_PRODUCT_FORM_ENUM
        if not re.search(rf"\b{re.escape(form)}\b", skill_md_text)
    ]
    assert not missing, (
        f"SKILL.md ({SKILL_MD_PATH}) is missing one or more "
        f"product_form enum tokens: {missing}. DP8-3 constrains "
        f"product_form to {{SOFTWARE, SKILL, AGENT, WORKFLOW}} "
        f"(uppercase, 4-class), and LUI mode must use the same "
        f"enum so the resulting interview.json is consistent with "
        f"backend-generated interviews. Expected section header: "
        f"'## Product Form 字段（SOFTWARE/SKILL/AGENT/WORKFLOW）'."
    )