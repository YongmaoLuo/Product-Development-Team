"""
Interviewer — Requirement Gathering Module
===========================================

Multi-turn dialogue to collect structured requirements and produce interview.json.
"""

import json
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

from coding_tool import CodingTool


REQUIRED_DIMENSIONS = ["background", "goals", "scope", "acceptance"]
OPTIONAL_DIMENSIONS = ["constraints"]

INTERVIEWER_SYSTEM_PROMPT = """你是一位资深的需求分析师。你的任务是通过多轮对话收集用户的真实需求，确保信息完备。

你必须覆盖以下维度：
- background（背景）：解决什么问题、面向谁、当前痛点
- goals（目标）：成功的定义，可量化或可验收
- scope（范围）：做什么、不做什么，明确边界
- constraints（约束）：技术栈、时间、资源限制（选填）
- acceptance（验收）：如何判断完成，可执行的验收条件

规则：
1. 每轮最多问 3 个问题
2. 问题要具体，避免笼统（如"你想要什么功能"）
3. 基于已有信息推导，不要重复问
4. 当 5 个维度全部有具体可操作的描述后，输出 JSON

输出格式（所有维度完备后）：
```json
{
  "dimensions": {
    "background": "...",
    "goals": "...",
    "scope": {
      "in": ["..."],
      "out": ["..."]
    },
    "constraints": {
      "tech_stack": "...",
      "deadline": "..."
    },
    "acceptance": "..."
  },
  "complete": true
}
```

如果信息不完备，输出：
```json
{
  "dimensions": {已收集的信息},
  "questions": ["问题1", "问题2", "问题3"],
  "complete": false
}
```
"""

SCOPE_PRECHECK_SYSTEM_PROMPT = """你是一位产品形态分类专家。根据用户的初始需求，判断用户想要交付的产品形态（product_form），
并初步划分 in_scope / out_of_scope。

product_form 取值（4 类枚举，必须小写）：
- "software" — 通用软件 / 库 / SDK / 服务 / daemon / CLI 工具
- "skill" — Claude / Agent 技能包（如 .claude/skills/ 下的 SKILL.md + scripts/）
- "agent" — 自主代理 / agent loop / 长期运行的 agent 流程
- "workflow" — 多步骤工作流编排 / pipeline

要求：
1. 只输出严格 JSON，禁止任何解释或前后缀文字
2. product_form 必须是上述 4 类之一；信息不足以判断时设为 "software"
3. in_scope / out_of_scope 是字符串数组；实在无法判断就放 []

输出格式（严格 JSON）：
{
  "product_form": "software" | "skill" | "agent" | "workflow",
  "in_scope": ["..."],
  "out_of_scope": ["..."],
  "open_questions": ["..."]
}
"""


# DP8 4-class lowercase enum. Anything outside this set defaults to
# "software" per the spec's boundary conditions.
SCOPE_PRECHECK_VALID_FORMS = frozenset({"software", "skill", "agent", "workflow"})


class Interviewer:
    """Collects structured requirements through multi-turn dialogue."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = plan_dir
        self.interview_file = plan_dir / "interview.json"
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if self.interview_file.exists():
            with open(self.interview_file, "r") as f:
                return json.load(f)
        return {
            "plan_id": self.plan_dir.name,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "in_progress",
            "dimensions": {},
            "chat_history": [],
        }

    def _save_state(self):
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        with open(self.interview_file, "w") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def scope_precheck(self, requirement: str) -> dict:
        """First-round scope pre-check (DP8 contract).

        Asks the LLM to classify the user's initial requirement into one
        of the 4-class lowercase product_form enum
        ``{software, skill, agent, workflow}`` and propose a draft
        in_scope / out_of_scope split.

        Contract:

          * LLM succeeds with a valid 4-class form → returned verbatim
            (case + value preserved).
          * LLM succeeds but the form is outside the 4-class enum
            (e.g. ``"ROBOT"``, ``"SERVICE"``, ``"SKILL"`` uppercase) →
            defaults to ``"software"``.
          * LLM fails (timeout / network / malformed payload / non-dict)
            → fallback ``{"product_form": "software", "in_scope": [],
            "out_of_scope": [], "open_questions": [requirement]}``. The
            implementation never propagates the LLM-side exception.
          * Empty input → ``open_questions == [""]`` (the
            ``[requirement_text]`` echo of an empty string).

        The returned dict is also mirrored onto ``self.state`` as
        top-level keys (``product_form`` / ``in_scope`` / ``out_of_scope`` /
        ``scope_open_questions``) so downstream consumers (PRDGenerator)
        can read them without traversing ``state["dimensions"]``.
        """
        # LLM-failure fallback echoes the requirement text in
        # open_questions so the user still sees what they asked.
        # When ``requirement`` is the empty string, this naturally
        # produces ``open_questions == [""]`` per the DP8 spec.
        fallback = {
            "product_form": "software",
            "in_scope": [],
            "out_of_scope": [],
            "open_questions": [requirement],
        }
        try:
            raw = self.coding_tool.query_json(
                prompt=requirement,
                system_instruction=SCOPE_PRECHECK_SYSTEM_PROMPT,
            )
        except Exception:
            self._persist_scope_precheck(fallback)
            return fallback

        if not isinstance(raw, dict):
            self._persist_scope_precheck(fallback)
            return fallback

        try:
            product_form = str(raw.get("product_form", "software"))
            in_scope = list(raw.get("in_scope", []) or [])
            out_scope = list(raw.get("out_of_scope", []) or [])
            open_questions = list(raw.get("open_questions", []) or [])
        except Exception:
            self._persist_scope_precheck(fallback)
            return fallback

        # Enforce the 4-class lowercase enum. Anything outside the
        # canonical set (including uppercase variants and unsupported
        # tokens like "ROBOT"/"service") defaults to "software" per
        # the DP8 boundary conditions.
        if product_form not in SCOPE_PRECHECK_VALID_FORMS:
            product_form = "software"

        result = {
            "product_form": product_form,
            "in_scope": in_scope,
            "out_of_scope": out_scope,
            "open_questions": open_questions,
        }
        self._persist_scope_precheck(result)
        return result

    def _persist_scope_precheck(self, result: dict) -> None:
        """Persist the scope_precheck result onto ``self.state`` as
        TOP-LEVEL keys (per the DP8 contract: ``interview.json`` must
        carry ``product_form`` / ``in_scope`` / ``out_of_scope`` at
        the top level, not nested under ``dimensions``).

        We do NOT call ``_save_state`` here — the caller (typically
        ``start`` / ``start_interview``) is responsible for the final
        ``_save_state()`` so the scope_precheck + dimension-update are
        persisted atomically.
        """
        self.state["product_form"] = result.get("product_form", "software")
        self.state["in_scope"] = list(result.get("in_scope", []) or [])
        self.state["out_of_scope"] = list(result.get("out_of_scope", []) or [])
        self.state["scope_open_questions"] = list(
            result.get("open_questions", []) or []
        )

    def start(self, initial_requirement: str) -> List[str]:
        """Start interview with initial requirement. Returns questions to ask user.

        This is the "first round" entry point.  Per the DP8 contract,
        the first round runs ``scope_precheck`` BEFORE the regular
        dimension-gathering query — this lets downstream phases
        (notably PRDGenerator) know the product_form early so they
        can adapt their templates (e.g. a "skill" plan is structured
        very differently from a "service" plan).
        """
        # Run scope_precheck first; it persists the result onto
        # self.state at top level (product_form / in_scope /
        # out_of_scope / scope_open_questions).  ``scope_precheck``
        # is fail-safe and never raises, so we can call it without
        # a try/except here.
        self.scope_precheck(initial_requirement)

        self.state["chat_history"].append({
            "role": "user",
            "content": initial_requirement,
        })

        response = self._query_interviewer()
        self.state["chat_history"].append({
            "role": "interviewer",
            "content": response,
        })

        parsed = self._parse_response(response)
        if parsed.get("complete"):
            self.state["dimensions"] = parsed["dimensions"]
            self.state["status"] = "complete"
            self._save_state()
            return []

        self.state["dimensions"] = parsed.get("dimensions", self.state["dimensions"])
        self._save_state()
        return parsed.get("questions", [])

    def continue_interview(self, user_reply: str) -> List[str]:
        """Continue interview with user's reply. Returns next questions or empty if complete."""
        self.state["chat_history"].append({
            "role": "user",
            "content": user_reply,
        })

        response = self._query_interviewer()
        self.state["chat_history"].append({
            "role": "interviewer",
            "content": response,
        })

        parsed = self._parse_response(response)
        if parsed.get("complete"):
            self.state["dimensions"] = parsed["dimensions"]
            self.state["status"] = "complete"
            self._save_state()
            return []

        self.state["dimensions"] = parsed.get("dimensions", self.state["dimensions"])
        self._save_state()
        return parsed.get("questions", [])

    # ------------------------------------------------------------------
    # DP8-2: single-question loop contract
    # ------------------------------------------------------------------
    #
    # Default interview cadence is ONE question per turn. The user must
    # explicitly opt into batch mode (``allow_batch=True``) to receive
    # multiple questions in a single round. This matches the DP8 spec
    # rule: "后续轮每次只 prompt 1 个问题（除非用户显式批量）".
    #
    # ``next_question`` is a pure transform over ``dimensions_state``
    # — it does NOT call the LLM. The LLM-driven question generation
    # is the job of ``start`` / ``continue_interview``; this method
    # gives callers (UI flows, tests, replay tools) a deterministic
    # way to surface the next question for the first missing
    # dimension, plus the four top-level DP8 fields so the caller can
    # render the surrounding context (product form chip, scope chips,
    # completion state) in a single round-trip.

    # Default per-dimension question templates. Kept deterministic so
    # tests / replay tools can assert on the exact string without
    # involving the LLM. Production callers who want LLM-tuned wording
    # can still call ``start`` / ``continue_interview`` directly.
    _DIMENSION_QUESTION_TEMPLATES: Dict[str, str] = {
        "background": "请描述一下项目背景：要解决什么问题？面向哪些用户？当前的痛点是什么？",
        "goals": "成功的目标是什么？哪些是可以量化的指标？怎样算完成？",
        "scope": "项目范围内做什么、不做什么？请列出 in_scope 和 out_of_scope 的功能点。",
        "acceptance": "如何验收？请给出可执行的验收标准（例如测试命令、人工抽样规则）。",
        "constraints": "有什么约束？技术栈、时间、资源限制等（选填）。",
    }

    def next_question(
        self,
        dimensions_state: dict,
        allow_batch: bool = False,
    ) -> dict:
        """Return the next question(s) to ask, based on ``dimensions_state``.

        Contract (DP8-2):

          * Default (``allow_batch=False``): ``next_question`` is a
            ``str`` — exactly ONE question for the first missing
            required dimension.
          * Explicit batch (``allow_batch=True``): ``next_question``
            is a ``list[str]`` — one question per missing required
            dimension.
          * All required dimensions covered: ``next_question=None``
            and ``interview_complete=True``.

        The returned dict always carries the four DP8 top-level
        fields (``product_form``, ``in_scope``, ``out_of_scope``,
        ``interview_complete``) so the caller can render the
        surrounding UI without a separate lookup.

        Args:
            dimensions_state: A dict mapping dimension names
                (``background`` / ``goals`` / ``scope`` /
                ``acceptance`` / ``constraints``) to their current
                content. A falsy value (empty string, empty dict,
                ``None``) counts as "not yet covered".
            allow_batch: ``False`` (default) → return a single
                question ``str``; ``True`` → return a ``list[str]``
                of questions for every missing required dimension.

        Returns:
            dict with keys ``next_question``, ``interview_complete``,
            ``product_form``, ``in_scope``, ``out_of_scope``.
        """
        # Defensive: callers may pass ``None`` for an unset dimension.
        # Normalise to a dict so ``.get(d)`` always works.
        if dimensions_state is None:
            dimensions_state = {}

        # Required-dimension coverage check. ``scope`` may be a dict
        # (``{"in": [...], "out": [...]}``) or a string; truthiness
        # handles both uniformly.
        missing_required = [
            d for d in REQUIRED_DIMENSIONS if not dimensions_state.get(d)
        ]
        interview_complete = not missing_required

        result = {
            "next_question": None,
            "interview_complete": interview_complete,
            "product_form": self.state.get("product_form"),
            "in_scope": list(self.state.get("in_scope", []) or []),
            "out_of_scope": list(self.state.get("out_of_scope", []) or []),
        }

        if interview_complete:
            return result

        if allow_batch:
            # Batch mode: one question per missing required dimension.
            result["next_question"] = [
                self._question_for_dimension(d) for d in missing_required
            ]
        else:
            # Default single-question mode: question for the FIRST
            # missing required dimension only.
            result["next_question"] = self._question_for_dimension(
                missing_required[0]
            )
        return result

    def _question_for_dimension(self, dimension: str) -> str:
        """Deterministic per-dimension question template.

        Used by ``next_question`` so callers (tests, replay tools,
        UIs) get a stable string without involving the LLM. The
        templates cover the 5 dimensions declared in
        ``REQUIRED_DIMENSIONS`` + ``OPTIONAL_DIMENSIONS``; an unknown
        dimension falls back to a generic prompt so the method never
        raises on a typo.
        """
        return self._DIMENSION_QUESTION_TEMPLATES.get(
            dimension, f"请补充 {dimension} 维度的信息"
        )

    def is_complete(self) -> bool:
        return self.state.get("status") == "complete"

    def get_dimensions(self) -> dict:
        return self.state.get("dimensions", {})

    def _query_interviewer(self) -> str:
        context = json.dumps({
            "dimensions": self.state["dimensions"],
            "chat_history": self.state["chat_history"][-10:],
        }, ensure_ascii=False)

        return self.coding_tool.query(
            prompt=context,
            system_instruction=INTERVIEWER_SYSTEM_PROMPT,
        )

    def _parse_response(self, response: str) -> dict:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start != -1 and end > start:
                return json.loads(response[start:end])
        except json.JSONDecodeError:
            pass
        return {"complete": False, "questions": [response]}


# ---------------------------------------------------------------------------
# VP-004: Five-dimension non-empty validation
# ---------------------------------------------------------------------------
# Contract (per VP-004 verification spec):
#   (a) The dimensions argument is an empty object ``{}`` -> REJECT
#   (b) The dimensions argument is a non-object type (str / int / list
#       of non-dict / None ...) -> REJECT
#   (c) A field value is an empty array ``[]`` -> REJECT
#   (d) A field value is an array containing only empty strings
#       (e.g. ``["", "  ", ""]``) -> REJECT
#   (e) All 5 dimensions are non-empty AND every field contains at
#       least one concrete (non-blank) item -> PASS
VP004_REQUIRED_DIMENSIONS = (
    "background",
    "goals",
    "scope",
    "constraints",
    "acceptance",
)


def _value_has_concrete_content(value) -> bool:
    """Return True iff ``value`` is a non-empty container carrying at
    least one concrete (non-blank) item.

    Accepted shapes:
      * ``str``  -> True iff ``value.strip()`` is non-empty
      * ``dict`` -> True iff any nested value passes this same check
        (recursive)
      * ``list``/``tuple`` -> True iff any item is itself concrete
    """
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_value_has_concrete_content(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_has_concrete_content(item) for item in value)
    return False


def validate_five_dimensions_completeness(dimensions) -> tuple:
    """Validate the five-dimension non-empty contract (VP-004).

    Returns a ``(is_valid, failures)`` tuple where ``failures`` is a
    list of human-readable reason strings.  Single source of truth
    for "is this ``dimensions`` dict interview-complete?".

    Boundary conditions (mirrored in
    ``tests/unit/test_interview_completeness.py``):

      * ``dimensions == {}``                       -> (False, [...])
      * ``dimensions`` is a non-object (str/int)  -> (False, [...])
      * ``dimensions`` is ``None``                -> (False, [...])
      * a field is an empty ``{}``                 -> (False, [...])
      * a field is an empty ``[]``                 -> (False, [...])
      * a field is ``["", "  ", ""]``             -> (False, [...])
      * a required dimension key is missing        -> (False, [...])
      * a field is a bare number (e.g. ``42``)    -> (False, [...])
      * a field is a non-blank string              -> (True,  [])
      * a field is a dict with concrete items      -> (True,  [])
      * a field is a list of non-blank strings     -> (True,  [])
      * full 5-dimension dict per the system prompt
        (mixed strings + dicts + non-empty lists) -> (True,  [])
    """
    failures = []

    # Rule (b): non-object type at the top level -> reject
    if not isinstance(dimensions, dict):
        failures.append(
            f"dimensions must be a dict, got {type(dimensions).__name__}"
        )
        return False, failures

    # Rule (a): empty object -> reject
    if not dimensions:
        failures.append("dimensions is an empty object")
        return False, failures

    for dim in VP004_REQUIRED_DIMENSIONS:
        if dim not in dimensions:
            failures.append(f"missing required dimension: {dim}")
            continue

        value = dimensions[dim]

        if value is None:
            failures.append(f"{dim} is null")
            continue

        # Rule (b) applied per-field: numbers / booleans are not
        # concrete descriptions.
        if isinstance(value, (int, float, bool)):
            failures.append(
                f"{dim} has invalid type {type(value).__name__}; "
                f"expected non-empty str / dict / list"
            )
            continue

        if not _value_has_concrete_content(value):
            failures.append(
                f"{dim} has no concrete content (got "
                f"{type(value).__name__}: {value!r})"
            )

    return (len(failures) == 0), failures
