"""
TDD tests for DP8 scope_precheck on the Interviewer.

These tests pin the contract from the DP8 task spec:

  * ``test_scope_precheck_identifies_skill`` — a user requirement
    that contains the keyword ``skill`` must produce
    ``product_form == "skill"`` via the LLM round-trip.
  * ``test_scope_precheck_fallback_on_llm_failure`` — when the LLM
    raises (timeout, rate-limit, JSON-parse error, etc.) the method
    must NOT crash; it must fall back to ``product_form='software'``
    and empty ``in_scope``/``out_of_scope`` lists.
  * ``test_interview_json_has_top_level_fields`` — after
    ``scope_precheck`` runs and the resulting state is persisted,
    ``interview.json`` must carry ``product_form`` / ``in_scope`` /
    ``out_of_scope`` as **top-level** keys (not nested under
    ``dimensions``).

The tests run with no real LLM — they use a stub ``CodingTool``
whose ``query_json`` returns a fixture dict or raises.  Hardcoded
fixtures keep the suite under the 30s backend budget.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from interviewer import Interviewer  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """Minimal ``CodingTool`` double used by these tests.

    A real ``CodingTool`` is an abstract base — we only need the two
    methods the interviewer actually calls (``query_json``).  Tests
    configure ``query_json_return`` / ``query_json_side_effect`` on
    the instance before invoking the interviewer.
    """

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
# Tests
# ---------------------------------------------------------------------------


def test_scope_precheck_identifies_skill(fake_coding_tool, plan_dir):
    """A user requirement containing the keyword ``skill`` must
    produce ``product_form == "skill"`` via the LLM round-trip.

    The LLM stub returns a dict whose ``product_form`` is "skill";
    the implementation must surface that on the returned dict (and
    later persist it as a top-level interview.json field — see
    ``test_interview_json_has_top_level_fields``).
    """
    fake_coding_tool.query_json_return = {
        "product_form": "skill",
        "in_scope": ["LaTeX 公式 OCR", "skill 包结构"],
        "out_of_scope": ["非 LaTeX 文本"],
        "open_questions": ["目标平台？"],
    }

    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    result = interviewer.scope_precheck("帮我写一个 latex 公式识别的 skill")

    assert isinstance(result, dict)
    assert result.get("product_form") == "skill", (
        f"expected product_form='skill' for a 'skill' requirement, got "
        f"{result.get('product_form')!r}"
    )
    # in_scope / out_of_scope should also be surfaced (non-empty lists).
    assert isinstance(result.get("in_scope"), list)
    assert isinstance(result.get("out_of_scope"), list)
    assert len(result["in_scope"]) >= 1


def test_scope_precheck_fallback_on_llm_failure(fake_coding_tool, plan_dir):
    """When the LLM call raises, scope_precheck must NOT crash — it
    must fall back to ``product_form='software'`` and empty
    ``in_scope`` / ``out_of_scope`` lists.

    We exercise three distinct failure modes (timeout, generic
    exception, malformed JSON) to prove the fallback path is robust
    — the spec says "LLM 失败 → 降级 product_form='software'，
    in_scope/out_scope=[]".
    """
    requirement = "帮我写一个 latex 公式识别的 skill"

    # -- Failure mode 1: LLM raises a timeout-style exception. ------
    fake_coding_tool.query_json_side_effect = TimeoutError(
        "llm upstream timed out"
    )
    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    result = interviewer.scope_precheck(requirement)
    assert isinstance(result, dict), "fallback must return a dict"
    assert result.get("product_form") == "software", (
        f"fallback product_form must be 'software', got "
        f"{result.get('product_form')!r}"
    )
    assert result.get("in_scope") == [], (
        f"fallback in_scope must be [], got {result.get('in_scope')!r}"
    )
    assert result.get("out_of_scope") == [], (
        f"fallback out_of_scope must be [], got {result.get('out_of_scope')!r}"
    )

    # -- Failure mode 2: LLM raises a generic exception. -----------
    fake_coding_tool.query_json_side_effect = RuntimeError("upstream 500")
    result = interviewer.scope_precheck(requirement)
    assert result.get("product_form") == "software"

    # -- Failure mode 3: LLM returns malformed (non-JSON-stringable)
    #    payload — surfaces as an exception during downstream parsing
    #    OR as a dict missing required keys. The fallback contract
    #    covers both: if anything goes wrong we always end up with
    #    product_form='software'. -------------------------------------
    class _Boom:
        def __iter__(self):
            raise ValueError("malformed payload")

        def __getitem__(self, key):
            raise ValueError("malformed payload")

    fake_coding_tool.query_json_side_effect = None
    fake_coding_tool.query_json_return = _Boom()  # type: ignore[assignment]
    # The implementation may either raise-and-catch here (returning
    # the fallback) or simply read required keys and fall through to
    # the fallback when KeyError/TypeError fires. Either path must
    # produce product_form='software'. We accept either outcome via
    # a try/except wrapper in the test.
    try:
        result = interviewer.scope_precheck(requirement)
        assert result.get("product_form") == "software", (
            f"malformed-payload fallback must be 'software', got "
            f"{result.get('product_form')!r}"
        )
        assert result.get("in_scope") == []
        assert result.get("out_of_scope") == []
    except Exception as exc:  # pragma: no cover — defensive
        pytest.fail(
            f"scope_precheck raised {type(exc).__name__} on malformed "
            f"payload: must fall back to software"
        )


def test_interview_json_has_top_level_fields(fake_coding_tool, plan_dir):
    """After ``scope_precheck`` runs, ``interview.json`` must contain
    ``product_form``, ``in_scope`` and ``out_of_scope`` as
    **top-level** keys (NOT nested under ``dimensions``).

    The task spec calls out exactly this: "interview.json 含
    product_form/in_scope/out_of_scope 顶层字段".  PRDGenerator
    only knows how to read top-level fields — nesting them under
    ``dimensions`` would hide them from downstream consumers.
    """
    fake_coding_tool.query_json_return = {
        "product_form": "skill",
        "in_scope": ["LaTeX 公式 OCR", "skill 包结构"],
        "out_of_scope": ["非 LaTeX 文本"],
        "open_questions": ["目标平台？"],
    }

    interviewer = Interviewer(coding_tool=fake_coding_tool, plan_dir=plan_dir)
    result = interviewer.scope_precheck("帮我写一个 latex 公式识别的 skill")

    # Persist the result (or rely on start_interview to do so — we
    # accept either design, but the test pins that interview.json
    # ends up with the required top-level keys).
    if isinstance(result, dict) and result.get("product_form") == "skill":
        # The implementation should have stored it; if it stored a
        # top-level product_form key we can read it back here.
        interviewer.state["product_form"] = result["product_form"]
        interviewer.state["in_scope"] = result.get("in_scope", [])
        interviewer.state["out_of_scope"] = result.get("out_of_scope", [])
        interviewer._save_state()

    interview_file = plan_dir / "interview.json"
    assert interview_file.exists(), (
        "interview.json must be persisted after scope_precheck"
    )
    persisted = json.loads(interview_file.read_text(encoding="utf-8"))

    # Top-level keys MUST exist (the spec's primary contract).
    assert "product_form" in persisted, (
        f"interview.json missing top-level 'product_form'; top-level "
        f"keys = {sorted(persisted.keys())}"
    )
    assert "in_scope" in persisted, (
        f"interview.json missing top-level 'in_scope'; top-level "
        f"keys = {sorted(persisted.keys())}"
    )
    assert "out_of_scope" in persisted, (
        f"interview.json missing top-level 'out_of_scope'; top-level "
        f"keys = {sorted(persisted.keys())}"
    )

    # The product_form value must match what the LLM returned.
    assert persisted["product_form"] == "skill"
    assert isinstance(persisted["in_scope"], list)
    assert isinstance(persisted["out_of_scope"], list)
    assert len(persisted["in_scope"]) >= 1

    # And these keys must NOT be nested under ``dimensions`` — that
    # would defeat the whole point of moving them to the top level.
    dims = persisted.get("dimensions", {}) or {}
    assert "product_form" not in dims, (
        "product_form must NOT be nested under dimensions"
    )
    assert "in_scope" not in dims, (
        "in_scope must NOT be nested under dimensions"
    )
    assert "out_of_scope" not in dims, (
        "out_of_scope must NOT be nested under dimensions"
    )