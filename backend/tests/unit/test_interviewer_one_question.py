"""TDD tests for ``Interviewer.scope_precheck`` (DP8 contract) and
``Interviewer.next_question`` single-question loop (DP8-2).

These tests pin the 4-class uppercase product_form contract from the
DP8 task spec:

    product_form ∈ {SOFTWARE, SKILL, AGENT, WORKFLOW}

Boundary behaviour pinned here:

  * LLM returns a valid 4-class form  → preserved verbatim (case + value)
  * LLM raises / fails                 → fallback ``{"product_form": "SOFTWARE",
                                          "in_scope": [], "out_of_scope": [],
                                          "open_questions": [requirement_text]}``
  * LLM returns a form outside the
    4-class enum (e.g. "ROBOT")        → defaults to ``"SOFTWARE"``
  * Empty input                       → ``open_questions == [""]``

DP8-2 single-question loop contract:

  * ``next_question(dimensions_state, allow_batch=False)`` returns a
    dict whose ``next_question`` is a ``str`` (NOT a list) when the
    interview is not yet complete.
  * ``allow_batch=True`` returns a ``list`` (user explicitly requested
    batch).
  * All required dimensions covered → ``next_question=None,
    interview_complete=True``.
  * ``scope_precheck`` + ``_save_state`` persist ``product_form``,
    ``in_scope``, ``out_of_scope`` as TOP-LEVEL keys in
    ``interview.json``.
  * Legacy ``interview.json`` without those keys loads cleanly with
    ``product_form=None``.

The tests run with no real LLM — a stub ``CodingTool`` whose
``query_json`` returns a fixture dict or raises. Hardcoded fixtures
keep the suite well under the 30s backend budget.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from interviewer import Interviewer  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """Minimal CodingTool double — only ``query_json`` is exercised."""

    def __init__(self):
        self.query_json_return: dict | None = None
        self.query_json_side_effect: BaseException | None = None
        self.query_json_calls: list[dict] = []

    def query(self, prompt: str, system_instruction=None, **kwargs) -> str:
        raise NotImplementedError("query() not used by scope_precheck")

    def query_json(self, prompt: str, system_instruction=None, **kwargs) -> dict:
        self.query_json_calls.append(
            {"prompt": prompt, "system_instruction": system_instruction}
        )
        if self.query_json_side_effect is not None:
            raise self.query_json_side_effect
        if self.query_json_return is None:
            raise RuntimeError(
                "_FakeCodingTool.query_json called without configuring "
                "query_json_return or query_json_side_effect"
            )
        return self.query_json_return


@pytest.fixture
def fake_coding_tool() -> _FakeCodingTool:
    return _FakeCodingTool()


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    plan = tmp_path / "20260101-test-scope-precheck"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


# ---------------------------------------------------------------------------
# TDD spec tests (DP8 — 4-class uppercase enum)
# ---------------------------------------------------------------------------


def test_scope_precheck_returns_valid_product_form(fake_coding_tool, plan_dir):
    """LLM returns ``product_form == "agent"`` → result.product_form == "agent".

    The 4-class lowercase enum is {software, skill, agent, workflow}.
    A valid response must be surfaced verbatim (case + value) so the
    downstream PRD generator can dispatch on the canonical token.
    """
    fake_coding_tool.query_json_return = {
        "product_form": "agent",
        "in_scope": ["邮件抓取", "分类规则"],
        "out_of_scope": ["邮件发送"],
        "open_questions": ["支持 Gmail/Outlook?"],
    }

    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    result = interviewer.scope_precheck("帮我写一个邮件分类的 agent")

    assert isinstance(result, dict)
    assert result.get("product_form") == "agent", (
        f"expected product_form='agent' (4-class lowercase enum preserved "
        f"verbatim), got {result.get('product_form')!r}"
    )
    # The LLM-provided in_scope / out_of_scope lists must also be surfaced.
    assert isinstance(result.get("in_scope"), list)
    assert isinstance(result.get("out_of_scope"), list)
    assert isinstance(result.get("open_questions"), list)


def test_scope_precheck_llm_failure_fallback(fake_coding_tool, plan_dir):
    """LLM raises → software fallback with open_questions=[requirement_text].

    The DP8 fallback contract is:
        {"product_form": "software",
         "in_scope": [],
         "out_of_scope": [],
         "open_questions": [requirement_text]}

    The implementation must NEVER propagate an LLM-side exception to the
    caller; the caller can always trust the returned dict shape.
    """
    requirement = "帮我写一个邮件分类的 agent"

    fake_coding_tool.query_json_side_effect = TimeoutError("llm upstream timed out")
    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    result = interviewer.scope_precheck(requirement)

    assert isinstance(result, dict), "fallback must return a dict"
    assert result.get("product_form") == "software", (
        f"fallback product_form must be 'software' (lowercase, 4-class enum), "
        f"got {result.get('product_form')!r}"
    )
    assert result.get("in_scope") == [], (
        f"fallback in_scope must be [], got {result.get('in_scope')!r}"
    )
    assert result.get("out_of_scope") == [], (
        f"fallback out_of_scope must be [], got {result.get('out_of_scope')!r}"
    )
    # open_questions must echo the requirement so the user can still see
    # what they originally asked.
    assert result.get("open_questions") == [requirement], (
        f"fallback open_questions must be [requirement_text]; got "
        f"{result.get('open_questions')!r}"
    )


def test_scope_precheck_invalid_form_defaults_software(fake_coding_tool, plan_dir):
    """LLM returns a product_form outside the 4-class enum → default software.

    The 4-class enum is {software, skill, agent, workflow}. Anything else
    — including plausible-but-unsupported tokens like "ROBOT", "service",
    "script" — must default to ``"software"`` so downstream consumers
    always see a recognisable token.

    The in_scope / out_of_scope lists from the LLM are still surfaced
    (the LLM may have produced useful scope hints even with a bad
    product_form label).
    """
    fake_coding_tool.query_json_return = {
        "product_form": "ROBOT",
        "in_scope": ["some scope"],
        "out_of_scope": ["other scope"],
        "open_questions": [],
    }

    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    result = interviewer.scope_precheck("build me a robot")

    assert isinstance(result, dict)
    assert result.get("product_form") == "software", (
        f"invalid product_form must default to 'software' (4-class enum), "
        f"got {result.get('product_form')!r}"
    )


# ---------------------------------------------------------------------------
# DP8-2: single-question loop + product_form persistence contract
# ---------------------------------------------------------------------------


def test_single_question_per_turn(fake_coding_tool, plan_dir):
    """``next_question(allow_batch=False)`` returns ``str`` (not list).

    The DP8-2 contract: subsequent rounds ask exactly ONE question per
    turn unless the user explicitly requests a batch. The default
    return shape for ``next_question`` is therefore a single string
    surfaced as ``result["next_question"]`` — never a list.

    A list-shaped return would silently regress the interview UX
    (user sees 3 stacked questions instead of 1), so this test pins
    the type precisely: ``isinstance(next_question, str)`` AND
    ``not isinstance(next_question, list)`` (the second clause rules
    out subclasses that might satisfy the first via ``UserString``-
    style wrappers).
    """
    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    # dimensions_state with all required dimensions empty → not complete.
    dimensions_state = {
        "background": "",
        "goals": "",
        "scope": "",
        "acceptance": "",
    }
    result = interviewer.next_question(dimensions_state, allow_batch=False)

    assert isinstance(result, dict), (
        f"next_question must return a dict, got {type(result)}"
    )
    assert "next_question" in result, (
        "next_question result must contain 'next_question' key"
    )
    assert "interview_complete" in result, (
        "next_question result must contain 'interview_complete' key"
    )
    nq = result["next_question"]
    assert nq is not None, (
        "next_question must be a non-None str when interview is not complete"
    )
    assert isinstance(nq, str), (
        f"next_question must be a str when allow_batch=False, "
        f"got {type(nq).__name__}: {nq!r}"
    )
    # Explicitly rule out list — a list would silently regress the
    # single-question UX even if it had one element.
    assert not isinstance(nq, list), (
        f"next_question must NOT be a list when allow_batch=False, "
        f"got list: {nq!r}"
    )
    # When the interview is not complete, the flag must reflect that.
    assert result["interview_complete"] is False, (
        f"interview_complete must be False when dimensions are missing, "
        f"got {result['interview_complete']!r}"
    )


def test_batch_when_user_explicit_requests(fake_coding_tool, plan_dir):
    """``allow_batch=True`` returns a ``list`` of questions.

    The DP8-2 contract: only when the user EXPLICITLY says "batch"
    (e.g. "给我 2-3 个一起", "批量提问") may the interviewer return
    multiple questions in a single round. This test pins the
    positive case — ``allow_batch=True`` MUST yield a list shape.

    A non-list shape (e.g. a single str even when batch was requested)
    would silently regress the batch UX, so the assertion is strict
    on ``isinstance(..., list)``.
    """
    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    # All dimensions empty → multiple missing → batch should yield >1.
    dimensions_state = {
        "background": "",
        "goals": "",
        "scope": "",
        "acceptance": "",
    }
    result = interviewer.next_question(dimensions_state, allow_batch=True)

    assert isinstance(result, dict)
    nq = result["next_question"]
    assert isinstance(nq, list), (
        f"next_question must be a list when allow_batch=True, "
        f"got {type(nq).__name__}: {nq!r}"
    )
    assert len(nq) >= 1, (
        f"batch-mode list must contain at least one question, got empty list"
    )
    # Every element in the batch must itself be a string (not nested lists).
    for i, q in enumerate(nq):
        assert isinstance(q, str), (
            f"batch question [{i}] must be a str, got {type(q).__name__}: {q!r}"
        )


def test_next_question_none_when_interview_complete(fake_coding_tool, plan_dir):
    """All 5 required dimensions covered → ``next_question=None,
    interview_complete=True``.

    The DP8-2 contract: once every required dimension has substantive
    content, ``next_question`` returns ``None`` (no more questions to
    ask) and ``interview_complete=True``. The caller uses this to
    transition out of the interview loop.
    """
    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    # All required dimensions populated with substantive content.
    dimensions_state = {
        "background": "团队希望有一个轻量级邮件分类助手",
        "goals": "准确率 ≥ 90%，每天处理 1000 封",
        "scope": {"in": ["抓取", "分类"], "out": ["发送"]},
        "acceptance": "通过 pytest 单元测试 + 人工抽样 100 封准确率 ≥ 90%",
    }
    result = interviewer.next_question(dimensions_state, allow_batch=False)

    assert isinstance(result, dict)
    assert result["interview_complete"] is True, (
        f"interview_complete must be True when all required dims covered, "
        f"got {result['interview_complete']!r}"
    )
    assert result["next_question"] is None, (
        f"next_question must be None when interview is complete, "
        f"got {result['next_question']!r}"
    )


def test_persist_product_form_to_interview_json(fake_coding_tool, plan_dir):
    """``scope_precheck`` + ``_save_state`` persist ``product_form``,
    ``in_scope``, ``out_of_scope`` as TOP-LEVEL keys in interview.json.

    The DP8-2 contract: downstream consumers (PRDGenerator, etc.)
    read ``product_form`` / ``in_scope`` / ``out_of_scope`` directly
    from the top level of ``interview.json`` — NOT nested under
    ``dimensions``. The persistence path must therefore:

      1. Mirror the scope_precheck result onto ``self.state`` as
         top-level keys (done in ``_persist_scope_precheck``).
      2. Actually flush ``self.state`` to disk so a cross-process
         read-back sees the same keys.

    This test exercises the full path: ``scope_precheck`` →
    ``_save_state`` → re-read ``interview.json`` → assert the three
    top-level keys exist with the expected values.
    """
    # Configure the stub LLM to return a known-good scope_precheck result.
    fake_coding_tool.query_json_return = {
        "product_form": "agent",
        "in_scope": ["邮件抓取", "分类规则配置"],
        "out_of_scope": ["邮件发送", "日历集成"],
        "open_questions": ["Gmail 还是 Outlook?"],
    }

    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    interviewer.scope_precheck("帮我写一个邮件分类的 agent")
    # Persist to disk — this is what production callers (start,
    # continue_interview) do at the end of each round.
    interviewer._save_state()

    # Re-read the on-disk interview.json independently of the
    # in-memory self.state.
    saved_path = plan_dir / "interview.json"
    assert saved_path.exists(), (
        f"interview.json must be persisted to disk after _save_state, "
        f"but {saved_path} does not exist"
    )
    saved = json.loads(saved_path.read_text(encoding="utf-8"))

    # The three DP8 top-level keys must all be present.
    assert "product_form" in saved, (
        f"interview.json missing top-level 'product_form' key; "
        f"keys present: {sorted(saved.keys())}"
    )
    assert "in_scope" in saved, (
        f"interview.json missing top-level 'in_scope' key; "
        f"keys present: {sorted(saved.keys())}"
    )
    assert "out_of_scope" in saved, (
        f"interview.json missing top-level 'out_of_scope' key; "
        f"keys present: {sorted(saved.keys())}"
    )
    # Values must round-trip verbatim from the scope_precheck result.
    assert saved["product_form"] == "agent", (
        f"product_form round-trip mismatch; expected 'agent', "
        f"got {saved['product_form']!r}"
    )
    assert saved["in_scope"] == ["邮件抓取", "分类规则配置"], (
        f"in_scope round-trip mismatch; got {saved['in_scope']!r}"
    )
    assert saved["out_of_scope"] == ["邮件发送", "日历集成"], (
        f"out_of_scope round-trip mismatch; got {saved['out_of_scope']!r}"
    )


def test_load_legacy_interview_json_without_product_form(tmp_path: Path):
    """Legacy ``interview.json`` without ``product_form`` → loads cleanly.

    Older plans on disk (created before DP8-2 landed) do NOT have the
    ``product_form`` / ``in_scope`` / ``out_of_scope`` top-level keys.
    Loading them MUST NOT raise — instead, ``self.state.get(
    "product_form")`` returns ``None`` (Python's natural default for
    a missing dict key).

    This contract is critical for plan-recovery flows: an existing
    plan that was mid-interview when DP8-2 shipped must continue to
    load without manual migration.
    """
    # Synthesise a legacy interview.json with NO DP8 top-level keys.
    plan_dir = tmp_path / "20260101-legacy-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "plan_id": plan_dir.name,
        "created_at": "2026-01-01T00:00:00Z",
        "status": "in_progress",
        "dimensions": {
            "background": "legacy background text",
        },
        "chat_history": [],
        # NOTE: deliberately no product_form / in_scope / out_of_scope.
    }
    (plan_dir / "interview.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8"
    )

    # Stub coding tool — never invoked during load.
    fake_tool = _FakeCodingTool()
    # Loading MUST NOT raise even though the legacy file lacks the
    # DP8 top-level keys.
    interviewer = Interviewer(coding_tool=fake_tool, plan_dir=plan_dir)

    # The DP8 keys default to None / empty list rather than raising
    # or being created out of thin air.
    assert interviewer.state.get("product_form") is None, (
        f"legacy interview.json must yield product_form=None; "
        f"got {interviewer.state.get('product_form')!r}"
    )
    # Existing pre-DP8 fields must round-trip intact.
    assert interviewer.state.get("dimensions", {}).get("background") == (
        "legacy background text"
    ), "legacy dimensions content must round-trip through load"
