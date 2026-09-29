"""
Tasks Generator — PRD to tasks.json Converter
===============================================

Generates SubTask-compatible tasks.json from reviewed PRD.
"""

import ast
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coding_tool import CodingTool
from framework.task_output_validator import is_to_be_created_path
from test_command_quality import (
    check_falsifiable,
    commands_of,
    find_interpreter_mismatch,
    find_unreachable_targets,
    inspect_task,
    probe_scope_is_usable,
)
from preflight_review import PreFlightReviewer
from self_review import run_doc_self_review, write_self_review_report
from execution_logger import get_logger
from plan_state import MIGRATION_AUDIT_SCHEMA as _MIGRATION_AUDIT_SCHEMA
from plan_state import migration_audit as _migration_audit


# Re-export the shared schema constant under the local name so
# tests/integration can assert ``tasks_generator.MIGRATION_AUDIT_SCHEMA``
# IS the same object as ``plan_state.MIGRATION_AUDIT_SCHEMA`` (DP7
# part (b) — single source of truth across the three scope files).
MIGRATION_AUDIT_SCHEMA = _MIGRATION_AUDIT_SCHEMA




# Canonical filename for the per-plan state JSON (see plan_state.py).
# A single literal — no string-concat bypass. This is the contract
# pinned by backend/tests/test_plan_state_filename_constant.py.
PLAN_STATE_FILENAME: str = "plan_state.json"

_logger = logging.getLogger(__name__)


class PreflightFailedError(Exception):
    """Raised when the preflight cross-doc reviewer reports ≥1 high-severity
    finding and tasks generation must abort.

    The ``findings`` attribute exposes the high-severity items so the
    server / UI layer can render them back to the user.
    """

    def __init__(self, message: str, findings: Optional[list] = None,
                 report: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.findings = findings or []
        self.report = report or {}


#: Constraint keys that name a workspace, in priority order. The first
#: match is the plan's primary target; all matches together form the set
#: a task is allowed to live in. A key that is not listed here still
#: counts as a workspace as long as it ends in ``_project_dir`` (see
#: :func:`_declared_workspaces`); it simply ranks below the listed ones.
WORKSPACE_CONSTRAINT_KEYS: Tuple[str, ...] = (
    "target_project_dir",
    "primary_project_dir",
    # Bare ``project_dir`` is the natural key the interview/PRD LLM
    # reaches for when the user names a single target directory
    # (observed live in smoke v4).
    "project_dir",
)


def _constraint_texts(constraints: Any) -> List[str]:
    """Flatten a constraints block into a list of ``key=value``-ish strings.

    Three shapes occur in the wild, all emitted by the interview/PRD
    LLMs:

      * a **dict** — ``{"target_project_dir": "/x"}``;
      * a ``";"``-delimited ``key=value`` string
        (observed live in smoke v11);
      * a **list**, whose elements are either ``key=value`` strings or
        bare prose (observed live on a production plan,
        whose PRD emitted a list of six prose constraints).

    Anything unrecognised yields an empty list. Callers decide what an
    empty result means — for workspaces it means "declared nothing",
    which is a real answer, not a lookup failure.
    """
    out: List[str] = []
    if isinstance(constraints, dict):
        return [f"{k}={v}" for k, v in constraints.items()]
    if isinstance(constraints, str):
        out.extend(part for part in constraints.split(";") if "=" in part)
        return out
    if isinstance(constraints, (list, tuple)):
        for item in constraints:
            if isinstance(item, str) and "=" in item:
                out.append(item)
            elif isinstance(item, dict):
                out.extend(f"{k}={v}" for k, v in item.items())
    return out


def _declared_workspace_pairs(constraints: Any) -> List[Tuple[str, str]]:
    """Extract ``[(key, path), ...]`` for every workspace key in *constraints*.

    Ordered by :data:`WORKSPACE_CONSTRAINT_KEYS` (declared priority),
    then by any other ``*_project_dir`` key. Only the FIRST occurrence
    of each key is kept.
    """
    pairs: List[Tuple[str, str]] = []
    seen: set = set()
    for text in _constraint_texts(constraints):
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip()
        if not value or key in seen:
            continue
        if key in WORKSPACE_CONSTRAINT_KEYS or key.endswith("_project_dir"):
            pairs.append((key, value))
            seen.add(key)

    def _rank(pair: Tuple[str, str]) -> int:
        try:
            return WORKSPACE_CONSTRAINT_KEYS.index(pair[0])
        except ValueError:
            return len(WORKSPACE_CONSTRAINT_KEYS)

    return sorted(pairs, key=_rank)


def declared_workspaces_for_plan(plan_dir: Path) -> List[Tuple[str, str]]:
    """``[(key, path), ...]`` the plan at *plan_dir* declared, priority order.

    Reads ``prd.json`` first, then ``interview.json`` — the same order and
    the same constraint shapes :func:`_declared_workspace_pairs` handles,
    but without needing a generator instance. ``POST
    /api/execution/{id}/start`` uses it to decide whether the plan named a
    workspace at all, and therefore whether the run's ``project_dir`` is
    the only authority.

    Empty list ⇒ the plan declared nothing.
    """
    def _from(data: Any) -> List[Tuple[str, str]]:
        if not isinstance(data, dict):
            return []
        pairs = _declared_workspace_pairs(data.get("constraints", {}))
        if pairs:
            return pairs
        dims = data.get("dimensions")
        if isinstance(dims, dict):
            return _declared_workspace_pairs(dims.get("constraints", {}))
        return []

    for path in (Path(plan_dir) / "prd.json", Path(plan_dir) / "interview.json"):
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                pairs = _from(json.load(f))
        except Exception:
            continue
        if pairs:
            return pairs
    return []


class TasksGenerationError(Exception):
    """Raised when tasks generation fails for a recoverable reason.

    Two cases this catches:

      1. **LLM returned 0 tasks**: the LLM mis-fired (hallucinated
         'no PRD provided', returned a bare list, etc.). The error
         payload is written to ``tasks.json`` with the raw LLM
         response preview so the user can decide to retry the
         endpoint vs. investigate the upstream docs.

      2. **Post-processing rejected all tasks**: every task emitted
         by the LLM was filtered out (e.g. all items were non-dict
         strings). Same recovery: retry the endpoint.

    The previous behaviour was to synthesise a 1-task "fallback"
    plan with the user-stated requirement inlined. That was a
    misleading safety net — the synthesised task had no DP
    structure, no TDD specs, no real implementation guidance, and
    frequently caused the executor to write code that diverged
    from the approved PRD/arch/test docs. Failing loudly is the
    correct behaviour.
    """

    def __init__(self, message: str, error_payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.error_payload = error_payload or {}


def _is_path_within(child: Path, parent: Path) -> bool:
    """True iff ``child`` resolves to a path equal to or under ``parent``.

    Used by ``TasksGenerator.validate_workspace`` to decide whether a
    task's emitted ``project_dir`` is inside one of the directories the
    operator declared (the places the plan is allowed to work in) or has
    leaked into the repository that hosts this server, which must never
    be a task's workspace and therefore needs to be force-overridden.

    Resolves both sides before comparing so trailing slashes, symlinks,
    and relative paths are normalized.
    """
    try:
        child_resolved = child.resolve()
        parent_resolved = parent.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if child_resolved == parent_resolved:
        return True
    try:
        child_resolved.relative_to(parent_resolved)
        return True
    except ValueError:
        return False


def _extract_decision_index(text: str, kind: str) -> str:
    """Return a numbered list of decisions from the upstream text.

    ``kind`` is "arch" or "test". The upstream loader concatenates
    arch + test design markdown, so both share the same heading
    format ``### 决策点 N: ...``. We differentiate by the source
    file label the caller passed in (``"arch 决策点 N"`` or
    ``"test 决策点 N"``).

    The function scans for headings ``### 决策点 N`` and yields
    a string like::

        arch 决策点 1: <title>
        arch 决策点 2: <title>
        ...
    """
    if not text:
        return ""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("### "):
            continue
        rest = line[4:].strip()
        if not rest.startswith("决策点 "):
            continue
        rest = rest[len("决策点 "):]
        # `1: title` or `1 ： title`
        n, _, title = rest.partition(":")
        if not n.strip().isdigit():
            n, _, title = rest.partition("：")
        if not n.strip().isdigit():
            continue
        lines.append(f"{kind} 决策点 {n.strip()}: {title.strip()}")
    return "\n".join(lines)


def _probe_problem_count(report: Optional[Dict[str, Any]]) -> int:
    """Unusable commands/paths in a probe report — the iteration's metric.

    Both categories count. A command that can never pass is exactly as
    unusable as one that always passes, and the loop must keep going
    while either is present.
    """
    if not isinstance(report, dict):
        return 0
    return len(report.get("vacuous") or []) + len(report.get("unreachable") or [])


def _render_falsifiability_block(report: Optional[Dict[str, Any]]) -> str:
    """Render RED-probe results as a self-review prompt section.

    Returns ``""`` when the probe was skipped or found nothing, so the
    prompt stays byte-identical to the pre-2026-09-20 one in the common
    case.

    This is the feedback half of the iteration loop: the probe *measures*
    that a command already passes before its task runs, and this block
    tells the rewriting agent what that means, why the obvious "fix"
    (narrow the name filter) does not work, and what does.
    """
    if not isinstance(report, dict):
        return ""
    vacuous = report.get("vacuous") or []
    unreachable = report.get("unreachable") or []
    if not vacuous and not unreachable:
        return ""

    parts: List[str] = []
    if vacuous:
        parts.append("\n".join([
            "",
            "## 命令可失败性（RED）实测结果 —— 必须全部修掉",
            "",
            "下面这些命令在**任务开工前**（所有代码改动都还没做的时候）就已经 exit 0。",
            "它们在任务做完之后也一定 exit 0，因此**无法证明任务做了任何事** ——",
            "任务会「完成」却什么都不改，然后被验证阶段判为失败，形成不收敛的循环。",
            "",
            "对每一条，请改写它的 `test_commands`，使其满足下列之一：",
            "",
            "1. 指向一个**尚不存在的测试产物**：新建的测试文件、新的 test target",
            "   （`cargo test --test <新集成测试文件>`、`pytest tests/<新文件>.py`、",
            "   `npx vitest run <新 spec 文件>`、`npx playwright test <新 spec 文件>`）；",
            "2. 指向一个**当前正在失败**的测试（复现既有 bug 的回归任务）。",
            "",
            "**禁止**改成「在已有测试集合里按名字过滤」的形式 —— 目标测试还没写出来时",
            "它们会空跑通过并退 0：",
            "",
            "- `cargo test --lib <模块>`：实测过滤到 0 个用例仍退 0",
            "- `npx vitest run <已存在的文件>`：实测已存在的用例全过，退 0",
            "- `npx vitest -t <不存在的用例名>`：实测 5 skipped，退 0",
            "- `pytest -k <关键字>`：无匹配时的退出码不要依赖",
            "",
            "在 `findings` 中为每条报 `type: \"violation\"`、`severity: \"high\"`，",
            "并在 `fixed_content` 里把命令**真的换掉**（不是只记 finding）。",
            "",
            "实测结果：",
            "```json",
            json.dumps(vacuous, ensure_ascii=False, indent=2),
            "```",
            "",
        ]))

    if unreachable:
        parts.append("\n".join([
            "",
            "## 不可达目标（REACHABLE）实测结果 —— 必须全部修掉",
            "",
            "下面的命令或路径**永远不会成功**。它们确实在开工前就失败（满足了上面那条），",
            "但任务做完之后**也**会失败 —— 任务永远无法完成，是另一种死循环。",
            "",
            "四种形态：",
            "",
            "- `doubled_cd_prefix`：命令写了 `cd X`，参数却又以 `X/` 开头，",
            "  解析成 `X/X/…`。**相对路径不要再带 `cd` 的那一层目录。**",
            "- `subproject_outside_cwd`：命令引用了子项目（自带 manifest，如",
            "  `frontend-app/`）里的文件，却没有 `cd` 进去。**加 `cd <子项目> && `，",
            "  并把路径里的子项目前缀去掉** —— 从仓库根跑时 runner 用的是另一套",
            "  配置，找不到那个文件（实测 vitest 报 `No test files found`）。",
            "- `malformed_declared_path`：`files_to_modify` 里的文件名被截断了",
            "  （词干以 `-`/`_` 结尾）。",
            "- `duplicate_declared_path`：同一条路径写了两遍，只差一个前导点",
            "  （`github/…` 与 `.github/…`）。只保留真实存在的那个。",
            "",
            "在 `findings` 中为每条报 `type: \"violation\"`、`severity: \"high\"`，",
            "并在 `fixed_content` 里**真的改掉**。",
            "",
            "实测结果：",
            "```json",
            json.dumps(unreachable, ensure_ascii=False, indent=2),
            "```",
            "",
        ]))

    return "\n".join(parts)


def _build_tasks_self_review_prompt(
    original_json: str,
    original_markdown: str,
    upstream: str,
    falsifiability: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the second-pass LLM prompt for tasks self-review.

    The LLM is told to return the **complete** post-fix tasks.json
    (same id set, every task re-emitted) with corrections baked in.
    Returning only a findings list is rejected — every finding must
    come with the diff applied to the relevant task's fields. The
    generator adopts the LLM's rewrite so the final tasks.json
    reflects the fix, not just the diagnosis.

    The prompt enumerates every ``arch 决策点`` and ``test 决策点``
    discovered in ``upstream`` so the LLM is forced to cross-check
    the candidate task list against the source-of-truth decisions.
    """
    arch_index = _extract_decision_index(upstream, "arch")
    test_index = _extract_decision_index(upstream, "test")
    falsifiability_block = _render_falsifiability_block(falsifiability)
    upstream_block = (
        f"\n\n## 上游文档（架构 / 测试设计）\n{upstream}\n"
        if upstream
        else ""
    )
    decision_index_block = ""
    if arch_index or test_index:
        decision_index_block = (
            "\n## 决策点索引（必须用于 cross-check）\n"
            "上游文档里所有决策点都列在这里。"
            "你的任务是核对每个决策点是否被至少一个 task 覆盖。\n\n"
            f"### arch 决策点\n{arch_index or '(未在 upstream 中发现)'}\n\n"
            f"### test 决策点\n{test_index or '(未在 upstream 中发现)'}\n\n"
            "对每条决策点：\n"
            "- 至少一个 task 在 description / depends_on 中显式引用并实施 → covered；\n"
            "- 没有 task 引用 → missing_coverage；\n"
            "- task 描述里出现 ``决策点 N`` 但上游文档中**没有**第 N 条（编号错位、\n"
            "  把 test 决策点编号当 arch 决策点引用）→ violation；\n"
            "- 多个 task 描述内容几乎完全相同（重复决策点 / 重复任务）→ duplication；\n"
            "在 ``findings`` 中报为对应 type。\n"
        )
    return (
        "你是一名资深任务列表审校员 / 第二轮生成器。\n\n"
        "刚刚第一轮生成了下面的 tasks.json。这一轮你的任务**不**只是列出问题——\n"
        "你需要把修复**直接写进**新的 tasks.json，并把完整的新版作为 **fixed_content** 返回。\n\n"
        "严格要求：\n"
        "1. **id 一致**：必须保留所有原任务 id（新增任务允许，但不要删除或重命名任何 id）。\n"
        "2. **结构性字段保留（硬约束，违反 executor 将直接拒跑）**：\n"
        "   - **files_to_modify**（list[str]）：必须**保留原文件路径**，**禁止返回空列表 []**；新建文件可追加但不得清空原路径。\n"
        "     **但「保留」不等于保留垃圾**：畸形条目必须就地修正或删除，这不算违规 ——\n"
        "     重复条目（`.github/x.yml` 与 `github/x.yml` 是同一条，只留带点的那个）、\n"
        "     被截断的文件名（词干以 `-`／`_` 结尾，如 `signal-type-.py`）。\n"
        "     判据：这条路径对应的文件，这个任务做完之后真的会存在吗？不会就不要写。\n"
        "   - **test_commands**（数组形式，唯一形式）：必须存在且非空。原命令可用时保留原值；\n"
        "     **若为空、或出现在下面「RED 实测结果」中**，必须换一条满足该节规则的命令。\n"
        "     空字符串或缺失视为 spec 违规。不要写 `test_command` 单数字段。\n"
        "   - **project_dir / status / depends_on**：必须**保留**（status 默认为 \"pending\"）。\n"
        "   违反以上任一项 → findings 报 violation severity=high，并在 fixed_content 中**显式补回**。\n"
        "3. **决策点覆盖 cross-check（最重要）**：用下面「决策点索引」中的每一条\n"
        "   ``arch 决策点 N`` 与 ``test 决策点 N`` 反向搜索 task 描述与 depends_on。\n"
        "   - 每条 arch 决策点必须至少被一个 task 显式实施；\n"
        "   - 每条 test 决策点必须至少被一个 test 任务覆盖；\n"
        "   - task 描述中出现 ``决策点 N`` 但上游文档中**没有**第 N 条（编号错位、\n"
        "     把 test 决策点编号当 arch 决策点引用）→ violation；\n"
        "   - 多个 task 描述内容几乎完全一致（重复决策点 / 重复任务）→ duplication；\n"
        "   在 findings 中按 type 字段记录。\n"
        "4. **针对每条 finding 真正修复字段**：例如「Task 13 端点归属错误」就把 task 13 的\n"
        "   ``files_to_modify`` / 接口契约从 frontend-app/app/api 改到 backend/routers；\n"
        "   「Task 7 缺 RSI/KDJ 任务」就新增一个 task 并把它插入到 depends_on 链中；\n"
        "   「重复任务」就合并 description 并调整 depends_on。\n"
        "5. **新增任务必须自洽**：插入位置（id 排序与 depends_on）必须闭环，不允许循环依赖。\n"
        "6. **fixed_content 必须是完整 JSON**（与原 tasks.json 顶层结构相同），不是 markdown，\n"
        "   不是只标记问题；下游执行器只会采用 fixed_content 的内容。\n"
        "7. **findings 仍要输出**，方便审计记录；每条 finding 引用 task id 与 location。\n"
        "8. **不在 fixed_content 中放 TBD / TODO / 待补充** 之类占位符；若是发现占位符，\n"
        "   应当场补全而不是仅记入 finding。\n\n"
        f"{falsifiability_block}"
        f"## 原始 tasks.json（JSON）\n{original_json}\n"
        f"{upstream_block}"
        f"{decision_index_block}"
        "## 原始 Markdown 渲染（仅作辅助参考，权威源是上面的 JSON）\n"
        f"{original_markdown}\n\n"
        "## 输出要求\n"
        "直接输出一个 JSON 对象（不要 markdown 代码块、不要解释、不要对话前缀），结构为：\n\n"
        "{\n"
        "  \"findings\": [\n"
        "    {\n"
        "      \"severity\": \"high|medium|low\",\n"
        "      \"type\": \"placeholder|consistency|scope|ambiguity|missing_coverage|violation|duplication|unclear\",\n"
        "      \"location\": \"<task id 或决策点>\",\n"
        "      \"finding\": \"<1-2 句中文描述>\",\n"
        "      \"action\": \"<具体修复了什么>\"\n"
        "    }\n"
        "  ],\n"
        "  \"fixed_content\": <完整的 tasks.json 对象，结构与原始一致，"
        "所有 id 与 status / project_dir / depends_on / test_command(s) 字段保留>\n"
        "}\n"
    )


def _extract_first_json_object(text: str) -> object:
    """Return the first balanced JSON object in ``text`` (or ``None``).

    Strips code fences and walks braces to find the first complete
    object — useful for LLM replies that prefix or suffix the JSON
    with prose.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    decoder = json.JSONDecoder()
    idx = cleaned.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
            return obj
        except json.JSONDecodeError:
            idx = cleaned.find("{", idx + 1)
    return None


def _compute_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(
        (text or "").encode("utf-8")
    ).hexdigest()


def _topological_sort_task_ids(tasks: List[Dict[str, Any]]) -> List[str]:
    """Return task ids in topological order (depends_on first).

    Stable Kahn's algorithm. Falls back to the input order on any
    cycle or missing-dependency, so the caller always gets *some*
    ordered list (the E2E gate uses this to identify the "last"
    task; a wrong last task simply fails the gate loudly rather
    than silently passing).
    """
    by_id = {t.get("id"): t for t in tasks if t.get("id") is not None}
    if not by_id:
        return [t.get("id") for t in tasks]
    indegree = {tid: 0 for tid in by_id}
    edges = {tid: [] for tid in by_id}
    for tid, t in by_id.items():
        for dep in (t.get("depends_on") or []):
            if dep in by_id and dep != tid:
                edges[dep].append(tid)
                indegree[tid] += 1
    # Stable: process ids in input order
    order = [tid for tid in by_id.keys() if indegree[tid] == 0]
    result = []
    while order:
        # pop the first to keep input order stable
        nxt = order.pop(0)
        result.append(nxt)
        for child in edges[nxt]:
            indegree[child] -= 1
            if indegree[child] == 0:
                order.append(child)
    if len(result) != len(by_id):
        # Cycle or dangling dep — fall back to input order.
        return [t.get("id") for t in tasks]
    return result


#: Appended to the tasks prompt ONLY when the plan declared workspaces.
#:
#: Before 2026-09-22 this block lived inside ``TASKS_SYSTEM_PROMPT``
#: unconditionally, with two placeholders — ``{primary_project_dir}`` and
#: ``{secondary_project_dir}`` — that nothing ever substituted (there is no
#: ``.format()`` call on the system prompt anywhere). A plan that declared
#: no workspace was therefore told "MUST 为每个任务指定 project_dir" and
#: handed a literal ``{primary_project_dir}`` as the example value, with no
#: candidate list. The model either copied the braces or fabricated a path.
#: Both are guesses, and guessing here is the bug (2026-09-22): a fabricated path may happen to work on one machine and
#: be nonsense on every other.
#:
#: Now the workspace list is substituted from what the plan actually
#: declared, and this block is omitted entirely when it declared nothing.
MULTI_WORKSPACE_PROMPT_TEMPLATE = """
## 工作区归属（CRITICAL）
本项目在 PRD/访谈里明确声明了以下工作区。每个任务 MUST 指定 `project_dir` 字段，
取值 MUST 是下列路径之一 —— 原样复制，不要改写、不要自己编路径：

{workspaces}

- 任务属于哪个工作区，就填哪个路径
- 不跨项目的独立任务，也必须明确归属其中一个
- 确实无法归入上述任何一个工作区的任务，把它写进 description 说明，不要编路径

**并行执行原则**：不同 project_dir 的任务可以并行执行。只有在最终集成测试时才需要等待所有前置任务完成。
"""

#: Appended instead when the plan declared no workspace. The framework has
#: no basis for a directory in that case, so it asks for none: the run's
#: working directory comes from ``POST /api/execution/{id}/start``, which
#: the operator fills in explicitly.
NO_WORKSPACE_PROMPT = """
## 工作区（CRITICAL）
本项目未声明任何工作区路径。因此：

- **不要输出 `project_dir` 字段**（省略它），不要猜测或编造任何目录
- 工作目录由操作者在启动执行时指定
"""


TASKS_SYSTEM_PROMPT = """你是一位资深技术架构师。根据 PRD 文档生成开发任务列表。
## 时间预算规则（CRITICAL）
- 每个任务 MUST 在 15 分钟内由中等能力模型（如 B-PRO-4.7）完成
- 如果估算超过 15 分钟，拆分为更小的子任务
- 每个任务必须包含 "estimated_duration" 字段（单位：分钟，整数）
- 如果任务涉及复杂重构或多个文件修改，必须拆分为多个 ≤10 分钟的任务

## Description 格式（CRITICAL — Markdown 结构化，通用模型兼容）
每个任务的 "description" 使用 Markdown 结构化格式（所有主流模型通用）：

```markdown
## 背景
本任务需要实现 XXX 功能。前置条件/依赖：已完成 YYY（如任务 1、任务 2）。
当前代码在 ZZZ 处缺少该功能，导致 ... 问题。

## 目标
一句话说明要实现什么（≤30字）

## 修改位置
- 文件：`path/to/file.ext`
- 函数：`function_name()`（新增或修改）

## 输入示例
```python
input_data = {"key": "value"}
```

## 输出示例
```python
expected_output = {"result": "value"}
```

## 边界条件
- 条件X → 处理方式Y
- 空输入 → 返回空/抛异常

## TDD 规格
- `test_xxx`: 输入条件 → 预期结果（对应 `tests/unit/test_xxx.py::test_xxx`）
- `test_yyy`: 边界条件 → 预期结果（对应 `tests/unit/test_xxx.py::test_yyy`）
```

Description 规则：
1. 总长度 ≤ 500 中文字符
2. **背景**：必须说明上下文、前置条件、当前缺失的功能
3. **修改位置**：必须指定确切的文件路径和函数名
4. **输入/输出示例**：必须包含具体的数据格式（不只是描述）
5. **边界条件**：必须明确列出异常处理和边界情况
6. **TDD 规格**：
   - 每项规格必须对应一个具体的测试函数
   - 格式：`test_name: 条件 → 预期结果（对应 test_file.py::test_name）`
7. **test_commands（唯一形式，永远用数组）**：
   - 一律写 `"test_commands": [...]`。**不要**写 `test_command` 单数字段——
     同一个概念两种 key（一会儿带 s 一会儿不带）会让读取方静默漏检。
   - 只有一个命令时也写成单元素数组：`"test_commands": ["pytest tests/x.py -v"]`
   - 多命令按顺序执行，全部通过才算成功
   - **【RED 硬规则】命令必须在任务开工前失败。**
     一条开工前就已经通过的命令，无法证明这个任务做了任何事——它会让该任务的
     完成判定恒为真，任务"完成"了却什么都没改。
     所以：
     - ✅ 指向**尚不存在的测试产物**：新建的测试文件 / 新的 test target
       （`cargo test --test <新集成测试文件>`、`npx vitest run <新 spec 文件>`、
       `npx playwright test <新 spec 文件>`、`pytest tests/<新文件>.py`）
     - ✅ 或者指向**当前就在失败**的测试（复现既有 bug 的回归任务）
     - ❌ **不要**用「在已有测试集合里按名字过滤」的形式：
       `cargo test --lib <模块>`、`pytest -k <关键字>`、`npx vitest run <已存在的文件>`。
       目标测试还没写出来时它们会**空跑通过**——实测
       `cargo test --lib no_such_test` 退 0、`npx vitest -t <不存在的用例名>` 退 0。
     - 判断方法：如果这个任务的所有代码改动都不做，你写的这条命令现在会怎么样？
       如果它会通过，就必须换一条。
   - **【路径一致性】命令引用的路径必须真的能指向任务要创建的文件**，
     否则命令**永远**不会成功（另一种死循环）：
     - 写了 `cd X` 之后，参数**不要**再带 `X/` 前缀：
       `cd frontend-app && npx vitest run tests/unit/x.spec.ts` ✅
       `cd frontend-app && npx vitest run frontend-app/tests/unit/x.spec.ts` ❌
       （后者解析成 `frontend-app/frontend-app/tests/unit/x.spec.ts`）
     - **引用子项目里的文件时，要先 `cd` 进去，路径里不要再带那一层目录名**：
       `cd frontend-app && npx vitest run lib/signals/x.spec.ts` ✅
       `npx vitest run frontend-app/lib/signals/x.spec.ts` ❌
       （仓库根的 vitest 用的是另一套 include 配置，实测对该路径报
       `No test files found` 并退 1 —— 任务做完之后也一样找不到）
     - `files_to_modify` 里**不要出现重复路径**：`.github/workflows/x.yml` 与
       `github/workflows/x.yml` 是同一条，只写带点的那条。
     - `files_to_modify` 里**不要有截断的文件名**（词干以 `-` / `_` 结尾，如
       `signal-type-.py`）—— 那是名字被截断了，不是文件。
   - **【解释器一致性】Python 命令必须用项目自带解释器的绝对路径，禁止
     `uv run` / `poetry run` / `pipenv run` / `pdm run` / `hatch run` /
     `conda run`。**
     这些 runner 会切到**它们自己**的环境，而不是项目仓库里的虚拟环境。
     结果：subagent 跑的时候用 A 解释器，框架独立复跑时用 B 解释器，
     同一条命令一边过一边不过 —— 表现为 `test-report mismatch`，任务被
     判为 FAILED，而根因只是一条命令的写法。
     - ✅ `"<该项目虚拟环境的 bin 目录>/pytest tests/x.py -v"` ——
       确切的绝对路径**见「工作区配置」里标明的「Python 解释器 / pytest」**
       （每个项目的虚拟环境目录名各不相同，以配置为准，不要猜）
     - ❌ `"uv run pytest tests/x.py -v"`
     - ❌ `"poetry run pytest tests/x.py -v"`
     工作区配置里若标明了「Python 解释器」路径，一律照它写。
8. 不要包含长篇解释性段落
9. 不要包含可轻易发现的文件结构信息
10. 使用 Markdown 标题（##）分隔信息块，所有模型都能理解

## 对应上游决策点（CRITICAL — 覆盖检查强约束）

每个任务的 description 必须包含一节 `## 对应上游决策点`，逐条列出该任务实施的
上游决策点，格式**必须**是：

```
## 对应上游决策点
- arch 决策点 3：四阶段 taxonomy 的配置化定义与扩展机制
- test 决策点 4：混合分类管线的可注入测试
```

【硬约束】

1. **必须带 kind 前缀**：`arch 决策点 N` 或 `test 决策点 N`。
   **禁止**只写 `决策点 N` —— arch-design.md 与 test-design.md 的编号空间彼此独立，
   省略前缀会让覆盖检查把 arch 的引用误判成 test 的覆盖（反之亦然），
   从而使真实的覆盖缺口被静默掩盖。
2. **编号取自文档标题**：arch-design.md / test-design.md 中的 `### 决策点 N:` 编号，
   从 1 开始。编号必须真实存在，错位引用（把 test 的编号当 arch 引用）视为 violation。
3. **每条上游决策点都必须至少被一个任务引用**，没有决策点可以被省略。
   一个任务实施多条决策点时全部列出；一条决策点被多个任务实施时各自列出。
4. 该节放在 description 内即可，不影响其他小节；不要用表格或变体格式。

## files_to_modify 字段（CRITICAL — validator step-3 强约束）

`framework/task_output_validator.py` step-3 会强校验 `files_to_modify`：
- 空列表 `[]` → `TaskValidationError("files_to_modify is empty")`，直接 abort dispatcher
- 路径不在磁盘上 → `TaskValidationError("<path> not found")`，直接 abort
- `auto_fix` **不会**修复 step-3（参见 validator line 235 "Steps 3 and 4 are not auto-fixable"）
- post-read gate 因此报 `unfixable failure`，整个 plan 终止

每个任务的 `files_to_modify` 必须满足以下条件之一：

1. **要新建/修改已存在文件**：填**相对项目根目录**的路径（不含绝对前缀、URL、不含 `../`）
   ```
   "files_to_modify": ["backend/agent.py", "backend/tests/unit/test_xxx.py"]
   ```

2. **完全不修改任何文件（纯文档/纯调研/未列出的新建）**：填 sentinel
   ```
   "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"]
   ```
   **禁止返回 `[]`**——会让 validator abort。

3. **【后处理自动补全】**：你在 description 里提到的相对路径会被自动扫描并 merge 到 `files_to_modify`（见下方 `_backfill_files_to_modify_from_description`）。如果你忘了列具体文件但 description 写了路径，后处理会自动补全。但**不要依赖**这条路径——优先按规则 1/2 显式填。

【硬约束】每个任务的 `files_to_modify` 数量 ≤ 5 个。超过会被后处理 `_enforce_files_to_modify_limit` 自动拆为子任务；**不要在首板自己写 `1-1`、`1-2` 这种 id**。

【并行提示】含 sentinel 或不与他人重叠的文件路径时，dispatcher 识别为"无文件冲突"，会和同 layer 其他 task 并发跑。

示例 1（修改单文件）：
```json
{
  "id": "1",
  "title": "实现 register 函数",
  "description": "...",
  "test_command": "pytest tests/unit/test_register.py -v",
  "files_to_modify": ["src/auth.py"]
}
```

示例 2（修改多文件）：
```json
{
  "id": "2",
  "title": "重构登录模块",
  "description": "...",
  "test_commands": ["pytest tests/unit/test_login.py -v", "pytest tests/integration/test_login_flow.py -v"],
  "files_to_modify": ["src/auth/login.py", "src/auth/session.py", "tests/unit/test_login.py"]
}
```

示例 3（不修改任何文件——纯调研/文档任务）：
```json
{
  "id": "3",
  "title": "编写 README",
  "description": "...",
  "test_command": "echo",
  "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"]
}
```

## depends_on 字段（CRITICAL）
每个任务 MUST 输出 `depends_on` 数组，列出该任务依赖的前置任务 id 列表：
- 如果 desc 写了 '前置条件：任务 X 已完成'，则 `depends_on: ["X"]`
- **如果无依赖**，**只有真正"零前置"的入口任务**（例如：第一步新建一个全新的独立模块、新建空的 fixture 目录、新建 CI workflow 文件、独立工具脚本等无需依赖任何上游任务产出的工作）才允许 `depends_on: []`。判断准则：**任务执行时是否需要读取任何其他任务产出物？**——如果需要，必须有依赖。
- 引用其他任务产出物（文件路径、函数名、类型、配置等）时，必须把该任务 id 写进 `depends_on`
- **新建模块若位于某任务创建的包/目录内**（例如某任务先建 `backend/failure_miner/` 包骨架，本任务在其中新建模块），必须把创建该包的任务 id 写进 `depends_on` —— 这类「包内归属」依赖不会出现在「前置条件」prose 里，不写就会从依赖图中丢失
示例：`"depends_on": ["1"]`

**生成指南（强烈推荐）**：
1. **入口任务（零前置）数量应最小化**——通常 1-3 个。如果你输出了 5+ 个入口任务，回头检查是否漏写了依赖（特别是测试、集成、CI 类任务通常依赖先行的实现任务）。
2. **多个可并行的基础组件任务**（例如：三个独立的 API 路由扩展、独立的 fixture 目录初始化）允许 `depends_on: []`——这是正常的并行起点。
3. **测试 / 集成 / 端到端任务**几乎总有上游：写某模块的单测 → 依赖该模块的实现任务；端到端集成 → 依赖整条链路的所有上游。

## 端到端验收 Task 强制项（CRITICAL — 数据源接入 / UI 改动类 plan 必须包含）

**触发条件**：当需求涉及以下任一类型时：
- 接入新数据源（ETF / 港股 / 美股 / 期货 / 任何新数据源）
- 修改/扩展/重构用户可见的 UI 功能（图表 / 页面 / 表单 / 设置项）
- 修改 API 端点字段结构 / 数据落库 schema

**强制要求**：任务列表的**最后一个任务**（按 id 升序的最后一个 `task`，或 `depends_on` 拓扑排序的汇点）必须是一个端到端验收任务，其 description 必须包含**全部**以下关键词：
- "端到端" 或 "end-to-end" 或 "E2E"
- "Playwright" 或 "Puppeteer" 或 "mcp__playwright__" 或 "mcp__puppeteer__"
- "模拟用户输入" 或 "模拟用户" 或 "user input"
- "断言" 或 "assert"

**端到端 task 的 description 必须包含**：
1. 启动前后端 server 的具体命令（必须列出实际命令，包括端口、venv 路径、cd 路径）
2. Playwright/Puppeteer 模拟用户操作的步骤序列（每步：点击 / 输入 / 等待 / 截图）
3. 断言语句（每条断言对应一个 KPI：K 线非空 / 实时价格有数字 / 错误信息可读 / 截图存档路径）

**为什么这条是铁律**：历史教训（一次复盘）—— 缺端到端验收 task 导致完成后用户还得手工补"启动 server → 输入 symbol → 看到 K 线"这条链。框架会在生成后强制检查这个任务存在；若缺失则拒绝 tasks.json 并要求重生成。

## 严禁的 Placeholder 模式（CRITICAL — No Placeholders）

任务 description 中**绝不能**出现以下任何模式。每条都是计划失败，不是文档问题：

- `TBD` / `TODO` / `FIXME` / `TBA` —— 未确定的占位符
- "implement later" / "fill in details" / "to be decided"
- "add appropriate error handling" / "add validation" / "handle edge cases"（不指明哪些）
- "write tests for the above"（不带具体 test 名称）
- "similar to Task N" / "same as Task N" / "类似 Task N"（implementer 可能读 out of order）
- 任何只描述做什么、不写怎么做（缺代码块）的代码步骤
- 引用未在任何 task 中定义的类型、函数、方法

**如果你写了这些，executor 会看到 "相似 Task N"，但读到的 task 顺序可能完全不同——必须把代码完整重复。**

## Interface Block（CRITICAL — 强制项）

每个任务 description 必须包含 Interface Block，明确告诉 implementer 接口契约：

```markdown
## 接口契约
- **Consumes**（来自前置任务）：[列出来自前置任务的具体函数名/类型/字段]
- **Produces**（给后续任务）：[列出本任务产出的具体函数签名/类型/返回值]
```

举例：
```markdown
## 接口契约
- Consumes: Task 1-1 的 `User` 模型（字段: id, email, password_hash, created_at）
- Produces: `POST /api/auth/login(request: LoginRequest) -> LoginResponse`
  其中 `LoginRequest = {email: str, password: str}`,
  `LoginResponse = {token: str, expires_at: datetime}`
```

Implementer 只看自己 task 的 description——这是它学习接口的唯一渠道。**Interfaces 块是 cross-task 通信的载体，不能省略。**

## Self-Review（生成后必须执行 — 不要跳过）

在生成完整 task 列表后、写 JSON 前，自检一遍：

1. **Spec coverage**：每个 PRD 验收标准都能在 ≥1 个 task 里实现？没有就加 task。
2. **Placeholder scan**：扫一遍所有 description，找上面"严禁的 placeholder 模式"。命中就改写——不能直接说"应该不会触发"。
3. **Type consistency**：Task 5 用的 `clearLayers()` 和 Task 7 用的 `clearFullLayers()` 是不是同一个？是 bug。统一。
4. **Test command validity**：每个 `test_command` / `test_commands` 都能直接执行吗？文件路径有效吗？测试文件存在吗？
5. **files_to_modify 完整性**：被引用的所有新文件都在某个 task 的 `files_to_modify` 里？
6. **依赖顺序**：task X 的 `consumes` 来自 task Y，那 Y 在 X 之前吗？

命中问题就**就地修复**，不要 re-ask user——他们不应该看到你第一稿的粗糙边角。

## 输出格式
{
  "requirement": "原始需求描述",
  "tasks": [
    {
      "id": "1",
      "title": "简短标题",
      "description": "结构化描述（含 ## 接口契约 块）",
      "test_commands": ["pytest tests/unit/test_xxx.py -v"],
      "estimated_duration": 8,
      "files_to_modify": ["backend/agent.py", "backend/tests/unit/test_xxx.py"],
      "depends_on": [],
      "project_dir": "/path/to/project"
    },
    {
      "id": "2",
      "title": "纯文档任务（无文件修改）",
      "description": "结构化描述",
      "test_commands": ["pytest tests/unit/test_xxx.py -v"],
      "estimated_duration": 4,
      "files_to_modify": [],
      "depends_on": ["1"],
      "project_dir": "/path/to/project"
    }
  ]
}

IMPORTANT: Return ONLY the JSON object, no markdown fencing.
"""


def _workspace_line(key: str, path: str) -> str:
    """One workspace bullet, annotated with the project's Python interpreter.

    The annotation is what stops the task generator reaching for
    ``uv run`` / ``poetry run``. Without it the LLM has no idea which
    environment the project actually uses, so it falls back on the modern
    Python default — and a foreign runner resolves an interpreter the
    project does not control. The subagent then passes on one interpreter
    while the framework's independent re-run fails on another: a
    test-report mismatch whose root cause is a single command's phrasing.

    A project that ships ``venv1/`` still gets generated tasks saying
    ``uv run pytest``, so the framework's independent re-run fails on an
    interpreter the project never uses.

    Best-effort — a missing or unreadable path simply yields no
    annotation rather than failing generation.
    """
    line = f"- {key}: {path}"
    try:
        from workspace_utils import detect_venv

        activate = detect_venv(Path(path))
    except Exception:
        return line
    if not activate:
        return line
    bin_dir = Path(activate).parent
    return (
        f"{line}  （Python 解释器：{bin_dir / 'python'}；"
        f"pytest：{bin_dir / 'pytest'}；"
        f"本项目的 Python 测试命令必须用这些路径，不要用 uv/poetry/pipenv run）"
    )


class TasksGenerator:
    """Generates SubTask-compatible tasks.json from reviewed PRD."""

    def __init__(
        self,
        coding_tool: CodingTool,
        plan_dir: Path,
        self_review_enabled: bool = True,
    ):
        self.coding_tool = coding_tool
        self.plan_dir = plan_dir
        self.self_review_enabled = self_review_enabled
        self.prd_json = plan_dir / "prd.json"
        self.prd_md = plan_dir / "prd.md"
        self.review_file = plan_dir / "review.json"
        self.tasks_file = plan_dir / "tasks.json"

    def generate(self) -> dict:
        """Generate tasks.json from PRD + review results + optional arch/test design.

        Writes the result to ``self.tasks_file`` (i.e.
        ``plans/{plan_id}/tasks.json``). This is the SINGLE source of
        truth — there is no longer a copy at ``<project_dir>/tasks.json``.
        The executor is launched with ``--tasks-file <plan_dir>/tasks.json``
        (see ``server.py::start_execution`` and ``cli.py``), so any
        copy at ``project_dir/tasks.json`` would diverge from canonical
        state and re-introduce the bug fixed in
        ``_persist_task_status`` (see agent.py).

        Returns:
            Generated tasks dict.

        Raises:
            PreflightFailedError: when the cross-document preflight
                reviewer (DP2) reports ≥1 high-severity finding. The
                tasks file is NOT written in that case — callers must
                surface the findings back to the user and re-run after
                fixing the underlying doc mismatches.
        """
        # DP2 — preflight cross-document consistency check. Runs before
        # any LLM call so we don't burn tokens generating tasks from a
        # broken spec. ``preflight_enabled`` defaults to True for new
        # plans; toggle via ``plan_state.flags.preflight_enabled = False``.
        self._run_preflight_gate()

        prd_content = self._summarise_prd()
        review_context = self._load_review_context()
        domain_knowledge = self._load_domain_knowledge()
        arch_content = self._summarise_arch_design()
        test_content = self._summarise_test_design()
        workspace_context = self._load_workspace_context()
        # Always include the user's original requirement (from
        # interview.json's dimensions) as a leading, unambiguous
        # requirement block. Smoke v11/v12 surfaced a non-determinism
        # where the LLM sometimes hallucinated "no PRD provided" even
        # with the PRD/arch/test docs inlined (12 KB+ of upstream
        # content). The fix was to pin the user-stated requirement
        # at the very top of the prompt as a JSON block.
        #
        # v15 (path-only with M1) reverts this: interview.json is
        # also referenced by path, not inlined. The LLM in
        # interactive mode can Read it itself. (interview.json is
        # the smallest upstream doc — ~3 KB — so this is a
        # negligible cost.) All three upstream docs + interview
        # follow the same path-only contract.
        interview_path = self.plan_dir / "interview.json"
        prompt_parts = []
        if interview_path.exists():
            prompt_parts.append(self._format_doc_reference(
                "interview.json (用户原始需求)",
                interview_path,
            ))
        if workspace_context:
            prompt_parts.append(f"工作区配置（CRITICAL）：\n{workspace_context}")
        if domain_knowledge:
            prompt_parts.append(f"领域知识：\n{domain_knowledge}")
        if test_content:
            prompt_parts.append(f"测试设计文档（最高优先级）：\n{test_content}")
        if arch_content:
            prompt_parts.append(f"架构设计文档：\n{arch_content}")
        if prd_content:
            prompt_parts.append(prd_content)
        if review_context:
            prompt_parts.append(f"审阅批注（rejected 的决策点需调整方案）：\n{review_context}")
        skip_notice = self._load_skip_notice()
        if skip_notice:
            prompt_parts.append(skip_notice)

        prompt = "\n\n".join(prompt_parts)
        # v15.2 minimal user prompt: the giant footer was confusing
        # the LLM into thinking I wanted test commands or step-by-step
        # instructions. Smoke v15.1 emitted `["python3 -c ..."]`
        # consistently. The fix is to KEEP THE PROMPT SMALL — the
        # TASKS_SYSTEM_PROMPT (passed via system_instruction) already
        # has the full JSON schema + rules. Repeating it in the user
        # prompt just gives the LLM more text to misread.
        #
        # The user prompt's only responsibility is now: tell the LLM
        # WHERE the upstream docs are and WHICH ONE to start with.
        prompt += (
            "\n\nRead the upstream docs in the order listed above and emit a valid JSON task plan."
        )

        # Append the JSON output contract to the system prompt so
        # the LLM sees "your reply must be a JSON object" as the
        # very last instruction. Smoke v5 surfaced silent empty
        # replies from tasks_generator — this is the lowest-cost
        # mitigation.
        #
        from llm_prompts import append_json_output_contract

        system_instruction = TASKS_SYSTEM_PROMPT
        # Workspace ownership is declared by the plan, never guessed by the
        # framework: ask for `project_dir` only when the plan actually named
        # a workspace, and give the model exactly those paths. A plan that
        # named none is told to omit the field — see the constants' docs.
        declared_dirs = self._declared_workspace_dirs()
        if declared_dirs:
            system_instruction = (
                f"{system_instruction}\n\n"
                + MULTI_WORKSPACE_PROMPT_TEMPLATE.format(
                    workspaces="\n".join(f"- `{d}`" for d in declared_dirs)
                )
            )
        else:
            system_instruction = f"{system_instruction}\n\n{NO_WORKSPACE_PROMPT}"
        system_instruction = append_json_output_contract(system_instruction)

        tasks_data = self.coding_tool.query_json(
            prompt=prompt,
            system_instruction=system_instruction,
        )

        # LLM JSON output is structurally unreliable. The prompt
        # asks for ``{"tasks": [...]}`` but the LLM sometimes
        # returns a bare list ``[...]`` or wraps the list in some
        # other shape. Coerce any of the common variants into
        # ``{"tasks": [...]}`` so the downstream code can use a
        # single consistent schema. (See commit 8b4d921 history.)
        if isinstance(tasks_data, list):
            tasks_data = {"tasks": tasks_data}
        elif not isinstance(tasks_data, dict):
            # Last-resort: wrap any other shape so ``.get("tasks")``
            # below doesn't crash. An empty tasks list means the
            # caller will see a plan with zero work items.
            tasks_data = {"tasks": []}
        elif "tasks" not in tasks_data:
            # The LLM may have wrapped the list under a different
            # key. We don't want to silently lose the work items,
            # so sniff for any list-typed value as a best-effort.
            list_under_other_key = next(
                (v for v in tasks_data.values() if isinstance(v, list)),
                None,
            )
            if list_under_other_key is not None:
                tasks_data = {"tasks": list_under_other_key}
            else:
                tasks_data = {"tasks": []}

        # Ensure tasks have required fields. LLM occasionally
        # emits non-dict items in the tasks list (e.g. a stray
        # string or number). Filter them out once at the top so
        # the rest of the per-task code (which assumes a dict)
        # does not crash.
        all_tasks = tasks_data.get("tasks", [])
        tasks = [t for t in all_tasks if isinstance(t, dict)]
        if not tasks and all_tasks:
            # All items were non-dict (rare, but possible). Wrap
            # the raw list so downstream code can still iterate
            # without crashing.
            tasks_data["tasks"] = []
            tasks = []

        for task in tasks:
            task.setdefault("status", "pending")
            task.setdefault("updated_time", None)
            task.setdefault("failure_reason", None)

            # Post-processing for ``depends_on`` (3-tier fallback).
            #
            # The prompt explicitly asks the LLM to emit ``depends_on``,
            # but LLM output formatting is unreliable: the field may be
            # missing, empty, or fail JSON parsing. To guarantee the
            # downstream executor (``agent._build_layers``) always sees
            # a well-formed ``depends_on`` array, we run a 3-tier
            # fallback on every task:
            #
            #   1. If the LLM emitted a non-empty ``depends_on`` list,
            #      keep it as-is.
            #   2. Otherwise try to extract ids from the description's
            #      "前置条件/依赖" hint via
            #      ``_postprocess_extract_from_desc``.
            #   3. Otherwise infer from the task id (e.g. "1-2" -> ["1"])
            #      via ``_postprocess_infer_from_id``.
            #
            # ``setdefault('depends_on', [])`` at the bottom guarantees
            # the field is always present, even when both extraction
            # and inference yield an empty list (top-level tasks).
            existing = task.get("depends_on")
            if not existing or not isinstance(existing, list) or len(existing) == 0:
                desc = task.get("description", "") or ""
                extracted = self._postprocess_extract_from_desc(desc)
                if extracted:
                    task["depends_on"] = extracted
                else:
                    task["depends_on"] = self._postprocess_infer_from_id(
                        task.get("id", "")
                    )
            task.setdefault("depends_on", [])

        # Enforce ``files_to_modify`` count limit (max 5 per task).
        #
        # Audit 2026-07-18: LLM sometimes emits one task with 10+ files
        # (prior plan's Stage 2 had 30 files; a single test failure rolled back
        # 30 file changes together). To guarantee each task is small enough
        # to fail/retry independently, we post-process: any task with > 5
        # entries in ``files_to_modify`` is split into child tasks using the
        # existing ``<parent>-N`` id convention. The parent's ``depends_on``
        # is preserved on each child; each child inherits the parent's other
        # fields (title prefix, description, test_command, etc.).
        #
        # Runs AFTER the per-task depends_on loop above so that:
        #   1. The parent's depends_on has already been resolved by the
        #      3-tier fallback, and we just copy that resolved list onto
        #      every child.
        #   2. The new children don't need a second pass through the
        #      depends_on post-processor — their id is hierarchical
        #      (``<parent>-N``) so ``_postprocess_infer_from_id`` would
        #      anyway want to set ``depends_on = [parent_id]``, which
        #      is wrong: children run in parallel with their siblings,
        #      so they must depend on the parent's upstream deps, not
        #      on the parent itself.
        # Principle 2 (user audit 2026-08-19): BEFORE the limit split,
        # backfill any empty ``files_to_modify`` with sentinel and
        # merge description-extracted paths. Without this step,
        # ``framework/task_output_validator.py`` step-3 rejects the
        # whole plan with ``unfixable failure`` and aborts the
        # dispatcher abort trace).
        tasks = self._backfill_files_to_modify_from_description(
            tasks, project_dir=str(self.plan_dir)
        )
        # 2026-09-15: wire semantic "package ownership" dependencies that
        # prose never states (a module created inside a package depends
        # on the task that creates the package). Runs AFTER the file
        # backfill so real paths exist to match against, and BEFORE
        # the files-limit split so children inherit the resolved deps.
        tasks = self._infer_package_dependencies(tasks)
        tasks_data["tasks"] = self._enforce_files_to_modify_limit(
            tasks, max_per_task=5
        )
        # 2026-09-20: run the RED step for the whole plan. A command that
        # already exits 0 on the untouched tree cannot certify a fix, and
        # until now nothing asked the question: `inspect_command` reads
        # shape only, and `_preflight_test_command_skip` (agent.py) turns
        # a pre-passing command into a *skipped task* rather than a
        # suspect one. Runs after the split/limit passes so every command
        # in the final list is probed.
        #
        # The result is not just recorded — it is fed back into the
        # self-review pass below, which rewrites the offenders, and the
        # probe is re-run afterwards. See `_iterate_on_falsifiability`.
        falsifiability = self.probe_falsifiability(tasks_data["tasks"])

        # LLM returned no tasks: this is a hard failure. The previous
        # behaviour synthesised a "fallback" task with the user-stated
        # requirement inlined, but that was a misleading safety net —
        # the synthesised task had no Decision-Point structure, no TDD
        # specs, no real implementation guidance, and frequently
        # caused the executor to write code that diverged from the
        # approved PRD/arch/test docs.
        #
        # The correct behaviour is to fail loudly: surface the LLM
        # response to the user, write the failure to disk as a
        # well-formed error JSON so the caller (server.py / FastAPI
        # endpoint) can surface a clear error message, and let the
        # user re-run the endpoint. Smoke v13 showed the LLM
        # rejection rate at ~33% — acceptable as a transient failure
        # that the user retries, NOT acceptable as a "synthesise
        # something anyway" path.
        if not tasks_data["tasks"]:
            error_payload = {
                "error": (
                    "tasks_generator: LLM returned 0 tasks. "
                    "Re-run /api/tasks/{plan_id}/generate; the "
                    "synthesis will retry."
                ),
                "error_type": "llm_returned_zero_tasks",
                "raw_llm_response_preview": str(tasks_data)[:2000],
                "tasks": [],
            }
            self.plan_dir.mkdir(parents=True, exist_ok=True)
            with open(self.tasks_file, "w") as f:
                json.dump(error_payload, f, indent=2, ensure_ascii=False)
            raise TasksGenerationError(
                "LLM returned 0 tasks; re-run the endpoint to retry. "
                "See tasks.json's `error` field for the raw LLM "
                "response preview."
            )

        # Save to plan directory (CANONICAL — single source of truth)
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        with open(self.tasks_file, "w") as f:
            json.dump(tasks_data, f, indent=2, ensure_ascii=False)

        # -- DP0: end-to-end verification task gate (CRITICAL) ----------
        # Trigger: this plan touches data sources, UI, or API surface
        # (per the prompt's "端到端验收 Task 强制项" section). Enforce
        # that the **last** task (by id) is an E2E verification task
        # whose description includes Playwright/Puppeteer + assertion
        # keywords. Without this gate the omission recurs (no E2E task →
        # executor stops at code-only completion → someone runs the
        # server and smoke-tests by hand → bug).
        try:
            self._enforce_e2e_task_gate(tasks_data)
        except TasksGenerationError as e:
            # Replace the just-written tasks.json with an error payload
            # so the UI surfaces the gate failure instead of silently
            # accepting a plan without an E2E task.
            error_payload = {
                "requirement": tasks_data.get("requirement", ""),
                "error": str(e),
                "error_type": "missing_e2e_verification_task",
                "hint": (
                    "The last task's description must contain 端到端 (or "
                    "end-to-end/E2E) + Playwright/Puppeteer + 断言/assert "
                    "+ 模拟用户输入. Re-run /api/tasks/{plan_id}/generate; "
                    "the synthesis will retry."
                ),
                "raw_llm_response_preview": json.dumps(tasks_data, ensure_ascii=False)[:2000],
                "tasks": tasks_data.get("tasks", []),
            }
            with open(self.tasks_file, "w") as f:
                json.dump(error_payload, f, indent=2, ensure_ascii=False)
            raise

        # -- DP1: mandatory second-pass self-review on tasks.json ---------
        # Render the canonical tasks.json into a Markdown form, run the
        # shared second LLM pass, then parse the rewrite back to JSON.
        # Failures abort the generator so the executor never sees a
        # tasks.json that the second LLM has not reviewed.
        if self.self_review_enabled:
            self._run_tasks_self_review(
                tasks_data, falsifiability=falsifiability,
            )
            falsifiability = self._iterate_on_falsifiability(
                tasks_data, falsifiability,
            )

        return tasks_data

    def _iterate_on_falsifiability(
        self,
        tasks_data: Dict[str, Any],
        falsifiability: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Re-run the self-review while commands still pass before their task.

        The first self-review gets the probe results in its prompt. This
        checks whether that landed and, if not, hands the *new* results
        back for another pass — the same diagnose → retry shape the task
        executor uses on failure, applied one level up.

        Bounded on three sides:

        * ``FALSIFIABILITY_MAX_ROUNDS`` caps the total number of
          self-review passes (the one already run counts).
        * The loop stops early when a round does not reduce the count of
          vacuous commands — repeating a rewrite that did not help only
          burns tokens.
        * Any probe failure degrades to "stop", never to "raise": the
          plan is still usable, and the surviving findings are in
          ``falsifiability_report.json`` for an operator.

        Returns the last probe report.
        """
        for _ in range(max(0, self.FALSIFIABILITY_MAX_ROUNDS - 1)):
            if falsifiability.get("skipped") or not _probe_problem_count(falsifiability):
                return falsifiability

            before = _probe_problem_count(falsifiability)
            try:
                self._run_tasks_self_review(
                    tasks_data, falsifiability=falsifiability,
                )
            except Exception as exc:  # noqa: BLE001 — must not abort generation
                print(
                    f"[TasksGenerator] falsifiability iteration stopped: "
                    f"{type(exc).__name__}: {exc}"
                )
                return falsifiability

            falsifiability = self.probe_falsifiability(tasks_data["tasks"])
            after = _probe_problem_count(falsifiability)
            print(
                f"[TasksGenerator] falsifiability iteration: "
                f"{before} -> {after} command(s)/path(s) still unusable"
            )
            if after >= before:
                return falsifiability

        return falsifiability

    def _enforce_e2e_task_gate(self, tasks_data: Dict[str, Any]) -> None:
        """Gate: ensure the LAST task is an end-to-end verification task.

        Trigger condition (the plan likely touches data sources / UI / API):
            * Plan requirement mentions 接入 / 数据源 / API / 端点 / 前端 /
              UI / 页面 / 图表 / 路由 / K 线 / 信号 / 数据 — any of these
              signals an externally-visible surface that the user must be
              able to exercise.
            * OR: any task's files_to_modify touches frontend/, api/, routes,
              page.tsx, app.py, api_server, routes/, etc.

        When triggered, the last task (by id) must be an E2E task whose
        description contains ALL of these keyword groups:
            * "端到端" / "end-to-end" / "E2E"
            * "Playwright" / "Puppeteer" / "mcp__playwright__" /
              "mcp__puppeteer__"
            * "断言" / "assert"
            * "模拟用户" / "user input" / "输入"

        Raises TasksGenerationError when any of these is missing.
        """
        tasks = tasks_data.get("tasks") or []
        if not tasks:
            # Already caught earlier (zero-task guard); nothing to gate.
            return

        requirement_text = (tasks_data.get("requirement") or "").lower()
        # Use word-boundary-aware matching via regex so short keywords
        # like 'ui' / 'api' don't trigger on substrings inside unrelated
        # English words (e.g. "require*ment*" — wait, that's not the case,
        # but "requ*ui*rement" / "m*api*" must not trigger). Chinese
        # keywords (e.g. '接入', '数据源') have no word boundary issue.
        trigger_keywords = (
            "接入", "数据源", "端点", "前端", "页面",
            "图表", "信号", "数据",
            "frontend", "endpoint",
        )
        plan_touches_surface = any(
            kw in requirement_text for kw in trigger_keywords
        )
        if not plan_touches_surface:
            # Word-boundary regex for short ambiguous English keywords.
            import re
            short_kws = ("api", "ui", "路由", "route")
            for kw in short_kws:
                if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", requirement_text):
                    plan_touches_surface = True
                    break
        if not plan_touches_surface:
            # Heuristic fallback: any task writes to a UI/API file.
            surface_path_hints = (
                "frontend/", "/api/", "routes.py", "page.tsx",
                "app.py", "api_server", "handlers/", "routers/",
            )
            for t in tasks:
                for f in (t.get("files_to_modify") or []):
                    fl = str(f).lower()
                    if any(h in fl for h in surface_path_hints):
                        plan_touches_surface = True
                        break
                if plan_touches_surface:
                    break
        if not plan_touches_surface:
            # Pure backend / data layer plan: E2E gate does not apply.
            return

        # Identify the "last" task. Prefer topological sort by depends_on;
        # fall back to id order.
        try:
            sorted_ids = _topological_sort_task_ids(tasks)
        except Exception:
            sorted_ids = [t.get("id") for t in tasks]
        last_id = sorted_ids[-1] if sorted_ids else None
        last_task = next(
            (t for t in tasks if t.get("id") == last_id),
            tasks[-1],
        )
        desc = (last_task.get("description") or "") + " " + (
            last_task.get("title") or ""
        )

        e2e_kw = ("端到端", "end-to-end", "e2e")
        browser_kw = (
            "playwright", "puppeteer",
            "mcp__playwright__", "mcp__puppeteer__",
        )
        assert_kw = ("断言", "assert")
        user_kw = ("模拟用户", "user input", "输入", "type(", "fill(", "click(")

        missing = []
        if not any(k in desc.lower() for k in e2e_kw):
            missing.append("端到端/end-to-end/E2E")
        if not any(k in desc.lower() for k in browser_kw):
            missing.append("Playwright/Puppeteer")
        if not any(k in desc.lower() for k in assert_kw):
            missing.append("断言/assert")
        if not any(k in desc.lower() for k in user_kw):
            missing.append("模拟用户输入 (user input / fill / click / 输入)")

        if missing:
            raise TasksGenerationError(
                "E2E verification task gate failed: last task (id="
                f"{last_id}, title={last_task.get('title','')!r}) is "
                "not a valid end-to-end verification task. Missing "
                f"keyword groups: {missing}. The plan touches a "
                "user-facing surface (data source / UI / frontend / "
                "API), so its last task MUST be an E2E task with "
                "Playwright/Puppeteer steps + assertions + simulated "
                "user input. See the '端到端验收 Task 强制项' section "
                "in the TASKS_SYSTEM_PROMPT for the required "
                "description structure."
            )

    def _run_tasks_self_review(
        self,
        tasks_data: Dict[str, Any],
        falsifiability: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Run the mandatory second LLM pass on tasks.json.

        The first pass emitted a canonical ``tasks.json``. The second
        pass reads the JSON **and** the upstream architecture / test
        design, then returns the **complete post-fix tasks.json** so
        the generator can apply the rewrite to disk — not just a
        findings list. The LLM is told to return the *whole* JSON
        (same id set, same task count) with corrections baked in.

        We bypass the generic ``run_doc_self_review`` here because
        the tasks contract needs the LLM to return JSON
        (not markdown), so we drive the coding tool directly with a
        custom prompt and run our own parse / apply path.
        """
        # Round-trip JSON so the LLM sees the same canonical shape
        # we plan to write back. ``ensure_ascii=False`` keeps
        # Chinese characters readable.
        original_json_text = json.dumps(
            tasks_data, ensure_ascii=False, indent=2,
        )
        original_markdown = self._tasks_to_markdown(tasks_data)
        upstream = self._load_tasks_upstream_block()

        prompt = _build_tasks_self_review_prompt(
            original_json=original_json_text,
            original_markdown=original_markdown,
            upstream=upstream,
            falsifiability=falsifiability,
        )

        # Drive the coding tool directly. ``mandatory=True``
        # semantics: any failure here must surface; the generator
        # cannot promote an unaudited draft.
        try:
            llm_reply = self.coding_tool.query(prompt=prompt)
        except Exception as exc:  # noqa: BLE001 — mandatory path
            raise RuntimeError(
                f"tasks second-pass LLM call failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # Findings live on the *wrapper*, the tasks dict it carries does
        # not have that key — so read them off the raw reply. Losing them
        # was the other half of the 2026-09-21 bug: a reply that followed
        # the prompt schema reported `findings: []`, which is why the
        # discarded rewrite left no trace anywhere.
        wrapper_findings: List[Any] = []
        reply_object = _extract_first_json_object(llm_reply)
        if isinstance(reply_object, dict) and isinstance(
            reply_object.get("findings"), list
        ):
            wrapper_findings = list(reply_object["findings"])

        rewritten = self._extract_rewritten_tasks(
            llm_reply,
            fallback=tasks_data,
        )

        # Programmatic cross-check: the LLM keeps returning
        # ``findings: []`` for real coverage gaps, so we emit the
        # missing_coverage findings ourselves and merge them with
        # whatever the LLM returned. This guarantees the audit
        # surfaces real decision-vs-task gaps even when the LLM
        # is lazy.
        programmatic_findings = self._compute_decision_coverage_findings(
            tasks_data
        )
        llm_findings = wrapper_findings or (
            rewritten.get("findings")
            if isinstance(rewritten, dict)
            else None
        )
        if not isinstance(llm_findings, list):
            llm_findings = []
        merged_findings = list(llm_findings) + list(programmatic_findings)
        rewritten = dict(rewritten) if isinstance(rewritten, dict) else {}
        rewritten["findings"] = merged_findings

        report: Dict[str, Any] = {
            "doc_type": "tasks",
            "attempted": True,
            "succeeded": True,
            "rewrote": rewritten != tasks_data,
            "findings": merged_findings,
            "input_content_hash": _compute_hash(original_json_text),
            "fixed_content_hash": _compute_hash(
                json.dumps(rewritten, ensure_ascii=False)
            ),
            "error": None,
            "mandatory": True,
        }

        applied = self._apply_tasks_rewrite(
            tasks_data=tasks_data,
            rewritten=rewritten,
            original_markdown=original_markdown,
            report=report,
        )
        report["applied"] = applied
        report["fixed_tasks"] = rewritten if applied else None

        try:
            write_self_review_report(self.plan_dir, "tasks", report)
        except Exception as exc:  # noqa: BLE001 — best-effort degrade
            _logger.warning(
                "tasks_generator self-review audit write failed: %s: %s",
                type(exc).__name__, exc,
            )

        logger = get_logger(plan_id=self.plan_dir.name)
        if logger is not None:
            logger.info(
                "tasks_self_review",
                f"tasks self-review applied={bool(applied)}; "
                f"tasks={len((rewritten or {}).get('tasks', []))}",
                phase="tasks_generation",
                data={
                    "doc_type": "tasks",
                    "findings_count": 0,
                    "rewrote": bool(report.get("rewrote")),
                    "applied": bool(applied),
                    "succeeded": True,
                },
            )

        if not applied:
            raise RuntimeError(
                "tasks self-review did not produce an applicable "
                "rewrite — refusing to promote the unaudited draft"
            )

    def _load_tasks_upstream_block(self) -> str:
        """Load architecture / test design as one upstream context.

        The LLM uses the upstream to verify task 13's API endpoint
        ownership, task 7's indicator scope, etc. Each section is
        optional — missing files are skipped silently.
        """
        chunks: List[str] = []
        for label, path in (
            ("架构设计 arch-design.md", self.plan_dir / "arch-design.md"),
            ("测试设计 test-design.md", self.plan_dir / "test-design.md"),
        ):
            if not path.exists():
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            chunks.append(
                f"## {label}\n{body[:6000]}\n"
            )
        return "\n".join(chunks)

    def _collect_upstream_decisions(self) -> List[Tuple[str, int, str]]:
        """Instance method that walks the plan dir for arch / test
        design markdown and returns ``(kind, n, title)`` tuples.
        """
        results: List[Tuple[str, int, str]] = []
        for kind, name in (("arch", "arch-design.md"),
                            ("test", "test-design.md")):
            path = self.plan_dir / name
            if not path.exists():
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            pat = re.compile(
                r"^###\s*决策点\s*(\d+)\s*[:：]\s*(.+?)$", re.M
            )
            for n, title in pat.findall(body):
                try:
                    results.append((kind, int(n), title.strip()))
                except ValueError:
                    continue
        return results

    def _compute_decision_coverage_findings(
        self, tasks_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Programmatically detect decision points that no task
        references and emit missing_coverage findings.

        The LLM self-review keeps returning 0 findings even when
        there are real coverage gaps; this deterministic check is
        the trustworthy fallback. The findings are merged into the
        LLM's own report so the operator sees the union.
        """
        decisions = self._collect_upstream_decisions()
        if not decisions:
            return []

        decision_to_tasks: Dict[Tuple[str, int], List[str]] = {}
        for t in tasks_data.get("tasks", []) or []:
            if not isinstance(t, dict):
                continue
            desc = t.get("description", "") or ""
            for kind, n, _title in decisions:
                # 2026-09-15: match on the kind prefix.
                #
                # The original bare pattern ``决策点\s*{n}`` did not
                # distinguish the two upstream documents, which number
                # their decision points independently. An ``arch 决策点 3``
                # citation therefore also satisfied ``test 决策点 3``, so a
                # genuine gap on the test side could be silently masked by
                # an unrelated arch reference — the check reported coverage
                # it had not actually verified. The generation prompt now
                # requires the kind-prefixed citation form
                # (``arch 决策点 N`` / ``test 决策点 N``), so this regex
                # matches the same contract the generator is told to honour.
                if re.search(rf"{kind}\s*决策点\s*{n}\b", desc):
                    decision_to_tasks.setdefault(
                        (kind, n), []
                    ).append(str(t.get("id")))

        findings: List[Dict[str, Any]] = []
        for kind, n, title in decisions:
            covered = decision_to_tasks.get((kind, n), [])
            if covered:
                continue
            findings.append({
                "severity": "high",
                "type": "missing_coverage",
                "location": f"{kind} 决策点 {n}",
                "finding": (
                    f"上游文档的 {kind} 决策点 {n}（{title}）没有任何 task 引用或实施。"
                    "second-pass LLM 持续漏报此类 missing_coverage，"
                    "本 finding 由 generator 程序化检测补全。"
                ),
                "action": (
                    f"新增至少一个 task 描述 {kind} 决策点 {n} 的实施，"
                    "并接到 depends_on 链。"
                ),
            })
        return findings

    @staticmethod
    def _extract_rewritten_tasks(
        llm_reply: str,
        fallback: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Pull the post-fix tasks dict out of the LLM reply.

        Accepted shapes (in priority order):
          1. A wrapper carrying the fix under ``fixed_content`` — **what
             the prompt actually asks for** — or the older ``fixed_tasks``.
          2. The reply is itself a valid tasks dict.
          3. The reply is markdown rendered by the LLM — fall back
             to ``_markdown_to_tasks`` (legacy contract).

        Shape 1 was missing until 2026-09-21: the extractor looked for
        ``tasks`` / ``fixed_tasks`` only, so a reply that followed the
        documented schema (``{"findings": […], "fixed_content": {…}}``)
        fell through to the ``"findings" in candidate`` branch and
        returned the *input unchanged*. The self-review then looked like
        a clean pass — ``rewrote: False``, ``findings: []`` — while
        silently discarding both the fix and the findings. Every
        iteration of the falsifiability loop was a no-op because of it.
        """
        if not isinstance(llm_reply, str) or not llm_reply.strip():
            return fallback

        candidate = _extract_first_json_object(llm_reply)
        if isinstance(candidate, dict):
            for key in ("fixed_content", "fixed_tasks"):
                inner = candidate.get(key)
                if isinstance(inner, dict) and isinstance(
                    inner.get("tasks"), list
                ):
                    return inner
            if "tasks" in candidate and isinstance(candidate["tasks"], list):
                # Either {"tasks":[...]} or {"requirement":..., "tasks":[...]}
                return candidate
            if "fixed_tasks" in candidate and isinstance(
                candidate["fixed_tasks"], dict
            ):
                return candidate["fixed_tasks"]
            if "findings" in candidate:
                # Wrapper without a fix — nothing to apply.
                return fallback

        # Fall back to the legacy markdown parser so we still pick
        # up older second-pass replies.
        try:
            return TasksGenerator._markdown_to_tasks(llm_reply, fallback)
        except Exception:
            return fallback

    def _apply_tasks_rewrite(
        self,
        tasks_data: Dict[str, Any],
        rewritten: Dict[str, Any],
        original_markdown: str,
        report: Dict[str, Any],
    ) -> bool:
        """Validate the rewrite and persist it.

        Returns True when tasks.json was actually overwritten with
        a valid rewrite (so the audit / log can record the apply
        step). Returns True for an idempotent "no changes" rewrite
        as well — the LLM reviewed the doc and found it clean.
        Raises on a structurally broken rewrite (no ``tasks`` key,
        id-set mismatch).
        """
        if not isinstance(rewritten, dict):
            raise RuntimeError(
                "tasks self-review rewrite is not a JSON object"
            )
        if "tasks" not in rewritten or not isinstance(rewritten["tasks"], list):
            raise RuntimeError(
                "tasks self-review rewrite is missing the tasks[] "
                "array — refusing to overwrite tasks.json"
            )

        # 1:1 id-set preservation. The LLM may add new tasks, but
        # we never accept a rewrite that drops an existing task —
        # the executor depends on the id set.
        original_ids = {
            str(t.get("id"))
            for t in tasks_data.get("tasks", [])
            if isinstance(t, dict) and t.get("id") is not None
        }
        rewritten_ids = {
            str(t.get("id"))
            for t in rewritten["tasks"]
            if isinstance(t, dict) and t.get("id") is not None
        }
        if original_ids and not original_ids.issubset(rewritten_ids):
            missing = sorted(original_ids - rewritten_ids)
            raise RuntimeError(
                f"tasks self-review rewrite dropped task ids: {missing!r}"
            )

        # Idempotent rewrite: same id set, same task count. The
        # LLM reviewed the draft and found it clean. Treat the
        # ``apply`` step as successful but a no-op so the generator
        # does not raise. This also covers the legacy passthrough
        # path where the test fixture's first-pass emit produces an
        # empty task list (e.g. ``{"tasks": []}``) — the second
        # pass through, with no changes, should still succeed.
        if (
            rewritten.get("tasks") == tasks_data.get("tasks")
            and (rewritten.get("requirement") or tasks_data.get("requirement"))
            == (tasks_data.get("requirement") or rewritten.get("requirement"))
        ):
            report["rewrote"] = False
            report["fixed_content_hash"] = report.get(
                "input_content_hash"
            )
            return True

        # Sanity: rewritten has the same top-level shape so callers
        # of tasks_data (e.g. _run_tasks_self_review's caller) keep
        # their expectations.
        merged: Dict[str, Any] = dict(tasks_data)
        merged["tasks"] = list(rewritten["tasks"])
        if "requirement" in rewritten and rewritten["requirement"]:
            merged["requirement"] = rewritten["requirement"]

        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)

        # Reflect the rewrite in the in-memory dict so the caller
        # of ``generate()`` sees the post-fix tasks.
        tasks_data.clear()
        tasks_data.update(merged)
        report["rewrote"] = True
        return True

    @staticmethod
    def _tasks_to_markdown(tasks_data: Dict[str, Any]) -> str:
        """Render tasks.json into a Markdown form for the second LLM pass."""
        lines = [f"# 任务列表 — {tasks_data.get('requirement', '未命名需求')}", ""]
        for idx, task in enumerate(tasks_data.get("tasks", []) or []):
            if not isinstance(task, dict):
                continue
            task_id = task.get("id", str(idx + 1))
            title = task.get("title", "未命名任务")
            lines.append(f"### Task {task_id}: {title}")
            lines.append("")
            description = task.get("description", "")
            if description:
                lines.append(description)
                lines.append("")
            commands = task.get("test_commands") or (
                [task["test_command"]] if task.get("test_command") else []
            )
            if commands:
                lines.append(
                    "**Test commands:** " + "; ".join(commands)
                )
            files = task.get("files_to_modify") or []
            if files:
                lines.append(
                    "**Files to modify:** " + ", ".join(files)
                )
            depends = task.get("depends_on") or []
            if depends:
                lines.append(
                    "**Depends on:** " + ", ".join(depends)
                )
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _markdown_to_tasks(
        markdown_text: str,
        original_tasks: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Parse a rewritten Markdown block back into tasks.json.

        Splits on ``### Task N`` headings, then matches each block to
        the original task by ``id`` and patches editable fields. Task
        order is preserved (we copy fields into the original dict
        in-place, never reorder). New tasks that the LLM introduces
        are dropped — the executor matches by id and an id we did
        not generate is unschedulable.
        """
        if not isinstance(markdown_text, str) or not markdown_text.strip():
            raise ValueError("self-review rewrite returned empty content")

        # Split on `### Task <id>: <title>` headings.
        blocks = re.split(r"(?m)^###\s+Task\s+([^:\n]+):\s*([^\n]*)\n", markdown_text)
        # blocks layout: [preamble, id1, title1, body1, id2, title2, body2, ...]
        parsed_tasks: Dict[str, Dict[str, Any]] = {}
        if len(blocks) >= 4:
            iterator = iter(blocks[1:])
            for task_id_raw, _title, body in zip(iterator, iterator, iterator):
                task_id = task_id_raw.strip()
                parsed_tasks[task_id] = TasksGenerator._parse_task_block(
                    task_id, body
                )

        patched_tasks: List[Dict[str, Any]] = []
        for original in original_tasks.get("tasks", []) or []:
            if not isinstance(original, dict):
                continue
            original_id = str(original.get("id", "")).strip()
            if original_id in parsed_tasks:
                merged = dict(original)
                merged.update(parsed_tasks[original_id])
                patched_tasks.append(merged)
            else:
                patched_tasks.append(original)

        if not patched_tasks:
            raise ValueError(
                "self-review rewrite lost every task — refusing to "
                "overwrite tasks.json"
            )

        patched = dict(original_tasks)
        patched["tasks"] = patched_tasks
        return patched

    @staticmethod
    def _parse_task_block(task_id: str, body: str) -> Dict[str, Any]:
        """Extract editable fields from a single task block.

        Captures ``test_commands`` / ``test_command`` and
        ``files_to_modify`` from the ``**Key:** value`` lines. The
        description block is taken verbatim from everything that is
        not a recognised metadata line. Structural fields (``id``,
        ``priority``, ``category``, ``status``, ``depends_on``,
        ``project_dir``) are intentionally preserved from the
        original tasks.json — the second LLM is not allowed to
        mutate them.
        """
        if not isinstance(body, str):
            return {}

        editable: Dict[str, Any] = {"id": task_id}

        # Extract structured "Key: value" lines.
        for key in ("test_commands", "test_command", "files_to_modify",
                    "depends_on"):
            pattern = re.compile(
                rf"(?m)^\s*\*\*{re.escape(key)}\*\*:\s*(.+?)\s*$"
            )
            match = pattern.search(body)
            if not match:
                continue
            raw = match.group(1).strip()
            if key == "test_commands":
                commands = [
                    part.strip().rstrip(";")
                    for part in re.split(r"[;,]\s*", raw)
                    if part.strip()
                ]
                editable["test_commands"] = commands
                editable.pop("test_command", None)
            elif key == "test_command":
                editable["test_command"] = raw
                editable.pop("test_commands", None)
            elif key == "files_to_modify":
                files = [
                    part.strip().rstrip(",")
                    for part in re.split(r"[;,]\s*", raw)
                    if part.strip()
                ]
                editable["files_to_modify"] = files
            elif key == "depends_on":
                deps = [
                    part.strip().rstrip(",")
                    for part in re.split(r"[;,]\s*", raw)
                    if part.strip()
                ]
                editable["depends_on"] = deps

        # Description: every line NOT matching a metadata key.
        description_lines: List[str] = []
        _meta_re = re.compile(
            r"^\s*\*\*(?:test_commands?|test_command|"
            r"files_to_modify|depends_on)\*\*:"
        )
        for line in body.splitlines():
            if _meta_re.match(line):
                continue
            description_lines.append(line.rstrip())
        description = "\n".join(description_lines).strip()
        if description:
            editable["description"] = description

        return editable

    def _run_preflight_gate(self) -> None:
        """Run the DP2 preflight cross-document consistency check.

        Behavior:
          * ``plan_state.flags.preflight_enabled == False`` → skip
            entirely (no LLM call, no report written).
          * Otherwise, invoke ``PreFlightReviewer.run`` with the plan
            id resolved from ``plan_dir.name``.
          * If the reviewer reports ``high_count >= 1`` → raise
            ``PreflightFailedError`` and DO NOT write tasks.json.
          * If the reviewer itself raises → log a warning and treat
            as degraded pass (do NOT block task generation, per the
            spec boundary condition).

        The reviewer's ``plans_root`` is set to ``plan_dir.parent`` so
        the canonical ``plans/<plan_id>/preflight_report.json`` path
        continues to work whether the caller passes a ``plans/<id>``
        directory or a project-relative plan directory.
        """
        if not self._is_preflight_enabled():
            return

        plans_root = self.plan_dir.parent
        plan_id = self.plan_dir.name

        reviewer = PreFlightReviewer(self.coding_tool, plans_root=plans_root)
        try:
            report = reviewer.run(plan_id)
        except Exception as exc:
            # Per spec: preflight throwing → log warning, treat as
            # degraded pass. Do NOT block task generation.
            _logger.warning(
                "Preflight reviewer raised %s for plan %s; "
                "treating as degraded pass: %s",
                type(exc).__name__, plan_id, exc,
            )
            return

        high_count = int(report.get("high_count", 0)) if isinstance(report, dict) else 0
        if high_count > 0:
            findings = report.get("findings", []) if isinstance(report, dict) else []
            high_findings = [f for f in findings if isinstance(f, dict) and f.get("severity") == "high"]
            raise PreflightFailedError(
                f"Preflight cross-document review found {high_count} high-severity "
                f"mismatch(es); aborting tasks generation. "
                f"Fix the highlighted gaps in PRD / arch / test design and re-run.",
                findings=high_findings,
                report=report,
            )

    def _is_preflight_enabled(self) -> bool:
        """Read the ``preflight_enabled`` flag from ``plan-state sidecar``.

        Defaults to ``True`` when the file is missing or the flag is
        absent, matching the DP2 contract that preflight is opt-out
        (the gate runs unless explicitly disabled).
        """
        state_file = self.plan_dir / PLAN_STATE_FILENAME
        if not state_file.exists():
            return True
        try:
            with open(state_file, "r", encoding="utf-8") as fp:
                state = json.load(fp)
        except (OSError, ValueError):
            # Corrupt or unreadable state — fall back to the safe default
            # (preflight runs). This matches the ``PreFlightReviewer``'s
            # own tolerance for missing plan-state sidecar.
            return True
        if not isinstance(state, dict):
            return True
        flags = state.get("flags", {})
        if not isinstance(flags, dict):
            return True
        return bool(flags.get("preflight_enabled", True))

    def _load_prd(self) -> str:
        if self.prd_json.exists():
            from prd_generator import PRDGenerator
            with open(self.prd_json, "r", encoding="utf-8") as f:
                prd_data = json.load(f)
            return PRDGenerator.prd_to_markdown(prd_data)
        with open(self.prd_md, "r") as f:
            return f.read()

    # ---- Upstream-doc references (path-only, v15 with M1) ---------
    # Governing principle: pass the path, not the content — let the
    # agent Read one decision point (DP) at a time.
    #
    # v11 (commit 19a5c30) tried this with --print mode but
    # failed — --print does NOT expose tool definitions, so the
    # LLM cannot actually Read the file. The LLM saw "PRD at
    # /path" + "请根据以上内容生成开发任务列表" and returned
    # ``{tasks: []}`` because it had no requirement information.
    #
    # v12 (commit bfb2587) re-introduced inline bodies so the LLM
    # had the upstream content directly.
    #
    # M1 (commit 3fb6517) migrated query_json to interactive mode,
    # which exposes the full Read/Bash/Edit tool set. With M1, the
    # original v11 path-only approach works: the LLM can now
    # ``Read`` the upstream docs and emit informed tasks.
    #
    # v15 contract: every upstream doc is referenced by PATH ONLY.
    # We provide a structured hint (doc_label, file path) plus an
    # explicit instruction to Read one DP at a time. No body
    # content is inlined. The LLM must use Read tool to fetch
    # the actual content; this is the whole point of M1.
    @staticmethod
    def _format_doc_reference(doc_label: str, path: Path) -> str:
        """Format a one-line upstream-doc reference.

        Emits an explicit hint that the LLM should Read the path
        one DP at a time, so the LLM never has to load the full
        doc into its context window at once.
        """
        return (
            f"{doc_label}（路径：{path}）：\n"
            f"  请使用 Read 工具**逐步**阅读本文档，"
            f"一次只读一个决策点（DP），不要一次性 Read 全文。"
        )

    def _summarise_prd(self) -> str:
        """Path-only reference for the PRD.

        Governing rule: do not inline the PRD text. Tell
        the LLM where the file is and to Read one DP at a time so
        the LLM never has to load the full PRD into its context
        window at once. Works because M1's interactive mode
        exposes the Read tool to the LLM.
        """
        if self.prd_json.exists():
            path = self.prd_json
        elif self.prd_md.exists():
            path = self.prd_md
        else:
            return ""  # No PRD on disk; let the LLM fall back to interview.
        return self._format_doc_reference("PRD 文档", path)

    def _summarise_arch_design(self) -> Optional[str]:
        """Path-only reference for the architecture design."""
        arch_file = self.plan_dir / "arch-design.md"
        if not arch_file.exists():
            return None
        return self._format_doc_reference("架构设计文档", arch_file)

    def _summarise_test_design(self) -> Optional[str]:
        """Path-only reference for the test design."""
        test_file = self.plan_dir / "test-design.md"
        if not test_file.exists():
            return None
        return self._format_doc_reference("测试设计文档", test_file)

    def _load_review_context(self) -> Optional[str]:
        if not self.review_file.exists():
            return None
        with open(self.review_file, "r") as f:
            review = json.load(f)

        rejected = [item for item in review.get("items", []) if item.get("status") == "rejected"]
        if not rejected:
            return None

        lines = []
        for item in rejected:
            lines.append(f"- [{item['title']}] 拒绝原因: {item.get('note', '无')}")
        return "\n".join(lines)

    def _load_domain_knowledge(self) -> Optional[str]:
        """Load domain knowledge from config if available."""
        try:
            from config_loader import load_config_by_name
            config = load_config_by_name("coding")
            if config and config.domain_knowledge:
                return config.domain_knowledge
        except Exception:
            pass
        return None

    def _load_arch_design(self) -> Optional[str]:
        """Load architecture design if available."""
        arch_file = self.plan_dir / "arch-design.md"
        if arch_file.exists():
            with open(arch_file, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def _load_test_design(self) -> Optional[str]:
        """Load test design if available."""
        test_file = self.plan_dir / "test-design.md"
        if test_file.exists():
            with open(test_file, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def _load_skip_notice(self) -> Optional[str]:
        """Build a skip notice from BOTH PRD review and test review, if any.

        Aggregation strategy:
        - PRD review status=skipped → user explicitly said this PRD decision point
          should not propagate to arch / test / tasks.
        - Test review status=skipped → user explicitly said this test decision point
          should not become a task.
        - Both lists are surfaced to the LLM so it doesn't generate tasks implementing
          or testing the skipped topics.
        """
        import json
        sections = []

        # PRD review skips
        if self.review_file.exists():
            try:
                with open(self.review_file, "r", encoding="utf-8") as f:
                    review = json.load(f)
                prd_skipped = [
                    i for i in review.get("items", []) if i.get("status") == "skipped"
                ]
                if prd_skipped:
                    bullet = "\n".join(
                        f"  - 决策点 {i['index']}: {i.get('title', '') or '(无标题)'}"
                        for i in prd_skipped
                    )
                    sections.append(
                        f"以下 {len(prd_skipped)} 个 PRD 决策点已被用户 SKIP(在 PRD review 阶段):\n"
                        f"{bullet}\n不得为这些主题生成任何开发任务(包括代码修改、CLI 工具、"
                        f"回测脚本、报告生成等)。"
                    )
            except Exception:
                pass

        # Test review skips
        test_review_file = self.plan_dir / "test-review.json"
        if test_review_file.exists():
            try:
                with open(test_review_file, "r", encoding="utf-8") as f:
                    test_review = json.load(f)
                test_skipped = [
                    i for i in test_review.get("items", []) if i.get("status") == "skipped"
                ]
                if test_skipped:
                    bullet = "\n".join(
                        f"  - {i.get('title', '') or '(无标题)'}"
                        for i in test_skipped
                    )
                    sections.append(
                        f"以下 {len(test_skipped)} 个测试决策点已被用户 SKIP(在 test review 阶段):\n"
                        f"{bullet}\n不得为这些被 SKIP 的测试生成任何实现任务。"
                    )
            except Exception:
                pass

        if not sections:
            return None
        return (
            "## ⚠️ 重要:用户已 SKIP 的决策点(必须遵守)\n"
            + "\n\n".join(sections)
        )

    @staticmethod
    def _postprocess_extract_from_desc(description: str) -> list:
        """Extract ``depends_on`` task ids from a task description.

        Recognised patterns (in priority order):

          - ``前置条件：任务 1 已完成。``
          - ``前置条件：任务 1-2 已完成。``
          - ``依赖：任务 1 已完成``
          - ``前置条件/依赖：已完成 任务 1-2。``
          - ``前置条件：任务 1, 2, 3 已完成``

        The function returns the list of task ids parsed from the
        description, e.g. ``['1-2']`` for ``"前置条件：任务 1-2 已完成"``.
        Returns ``[]`` when no recognised trigger word is found or no
        task id can be parsed.

        Boundary cases:

          - ``description`` is empty / ``None`` / non-string -> ``[]``
          - description has the trigger word but no ``任务`` keyword after
            it -> ``[]``
          - description has the trigger + ``任务`` keyword but no parseable
            id (e.g. just trailing punctuation) -> ``[]``

        Implementation note: the trigger regex matches any of the three
        Chinese phrasings ("前置条件", "依赖", "前置条件/依赖"). The id
        matcher is deliberately permissive — ``\d+(?:-\d+)*`` — to
        accept both flat ("1") and hierarchical ("1-2", "1-2-3") task
        ids, matching the prompt's id convention.
        """
        if not description or not isinstance(description, str):
            return []

        # Locate the trigger phrase (first occurrence wins).
        trigger_match = re.search(
            r"(?:前置条件\s*/\s*依赖|前置条件|依赖)\s*[：:]",
            description,
        )
        if not trigger_match:
            return []

        # Bound the search to the trigger's sentence: stop at the
        # first sentence terminator or newline after the trigger so
        # we don't pick up ids from unrelated later sentences.
        after_trigger = description[trigger_match.end():]
        boundary = re.search(r"[。\n;；]", after_trigger)
        if boundary:
            after_trigger = after_trigger[:boundary.start()]

        # The "任务" keyword (preceded by nothing specific) is where
        # the id list starts. If absent, no id can be parsed.
        task_kw_idx = after_trigger.find("任务")
        if task_kw_idx < 0:
            return []
        ids_section = after_trigger[task_kw_idx + len("任务"):]

        # Extract all valid task ids. The regex is greedy on the
        # `(?:\-\d+)*` tail so it captures full hierarchical ids
        # ("1-2-3") rather than splitting them.
        id_pattern = re.compile(r"\d+(?:-\d+)*")
        matches = id_pattern.findall(ids_section)
        return matches

    @staticmethod
    def _postprocess_infer_from_id(task_id: str) -> list:
        """Infer ``depends_on`` from a hierarchical task id.

        Convention: a task whose id is a flat number ("1", "2") has
        no parent — returns ``[]``. A hierarchical id like "1-2"
        depends on its direct parent ("1"). Deeper hierarchies like
        "1-2-3" depend on the immediate parent ("1-2").

        Boundary cases:

          - empty / ``None`` task id -> ``[]``
          - flat id "1" / "2" -> ``[]`` (no parent)
          - "1-2" -> ``["1"]``
          - "1-2-3" -> ``["1-2"]``
          - "2-3" -> ``["2"]``

        The function deliberately returns only the **direct** parent
        (not the full ancestor chain) because (a) the prompt asks the
        LLM to keep ``depends_on`` minimal, and (b) the executor's
        layer builder walks transitive closures anyway.
        """
        if not task_id or not isinstance(task_id, str):
            return []
        if "-" not in task_id:
            return []
        parts = task_id.split("-")
        if len(parts) < 2:
            return []
        return ["-".join(parts[:-1])]

    # Regex for backfilling files_to_modify from description text.
    # Mirrors ``backend/agent.py:_extract_target_files`` so behaviour
    # stays consistent across generator (plan-load) and dispatcher
    # (runtime file-lock detection).
    _PATH_REGEX = re.compile(
        r"\b([a-zA-Z0-9_./-]+\.(?:py|js|ts|jsx|tsx|java|kt|go|rs|"
        r"c|cpp|h|hpp|cs|rb|php|swift|m|mm|sh|bash|"
        r"yaml|yml|json|toml|md))\b"
    )

    # 2026-09-15: extensionless module paths (``backend/failure_miner/
    # normalizer``) written by the generator in its 修改位置 sections.
    # ``_PATH_REGEX`` above requires a file extension, so module-style
    # references extracted 0 paths and every task degraded to the
    # ``__UNKNOWN_MODIFICATIONS__`` sentinel.
    #
    # The ``(?![./])`` lookahead rejects two shapes the caller must not
    # mistake for a module:
    #   * a trailing ``/`` (``backend/failure_miner/``) — the directory
    #     itself, not a file inside it;
    #   * a trailing ``.<ext>`` (``scripts/runners/run_x.sh``) — a real
    #     file whose extension ``_PATH_REGEX`` already extracted, so
    #     guessing ``.py`` would invent a path that does not exist (and
    #     would still pass validator step-3 via the "parent exists →
    #     file to be created" rule, so the executor would build the
    #     wrong artefact).
    _MODULE_PATH_REGEX = re.compile(
        r"\b((?:backend|frontend|tools|scripts|tests|docs)"
        r"(?:/[A-Za-z0-9_-]+)+)\b(?![./])"
    )

    # 2026-09-15: "creates a package / package directory" statements in
    # the 修改位置 section, e.g. ``新建包目录 `backend/failure_miner/```.
    # Used by ``_infer_package_dependencies`` to wire the semantic
    # dependency "modules inside the package depend on the task that
    # creates the package" — a dependency prose never states (task
    # descriptions say 前置条件：无 for such tasks).
    _PKG_DIR_REGEX = re.compile(
        r"新建包(?:目录)?\s*[`'\"]?([A-Za-z0-9_./-]+?)[`'\"]?(?=[，,。；;\s)]|$)"
    )

    # Test-file candidates for the test_command backfill: any path
    # under a ``tests/`` directory, with or without the ``.py``
    # suffix (the generator writes them extensionless).
    _TEST_FILE_REGEX = re.compile(
        r"\b((?:[A-Za-z0-9_.-]+/)*tests?/[A-Za-z0-9_./-]*)\b(?!/)"
    )

    # Body of the ``## 修改位置`` section, up to the next ``##`` heading.
    _MOD_SECTION_RE = re.compile(
        r"^##\s*修改位置\s*$(.*?)(?=^##\s|\Z)", re.M | re.S
    )

    def _extraction_scope(self, description: str) -> str:
        """Return the text the path / module / test-file extractors scan.

        The ``## 修改位置`` section is the authoritative declaration of
        what a task creates and modifies. Prose elsewhere in the
        description (背景 / 输入示例 / 边界条件) routinely mentions files
        that are NOT deliverables — an operator's absolute path, a shared
        cron file that lives outside the repo — and scanning the whole
        description turned those into ``files_to_modify`` entries. An
        absolute path was the worst case: the leading ``/`` is not part
        of the regex match, so ``/Users/x/y.py`` came back as the bogus
        repo-relative ``Users/x/y.py``.

        Falls back to the whole description when the section is absent,
        preserving the behaviour earlier callers rely on.
        """
        m = self._MOD_SECTION_RE.search(description or "")
        return m.group(1) if m else (description or "")

    #: Wall-clock ceiling for one command's pre-work probe. Short on
    #: purpose: a vacuous command returns in milliseconds — it only reads
    #: a file — so anything still running after a minute is a real test
    #: and the probe already has its answer. The probe never waits for a
    #: real suite to finish, so its cost is bounded by "distinct commands
    #: x timeout", not by the suite's runtime.
    FALSIFIABILITY_TIMEOUT_S = 60

    #: Set to a non-empty value to skip the probe. Needed when the target
    #: project cannot execute in the current environment (missing
    #: toolchain, offline): every command would otherwise look vacuous
    #: and the whole plan would be reported as unverifiable.
    FALSIFIABILITY_ENV = "PDT_DISABLE_FALSIFIABILITY_PROBE"

    #: Total number of self-review passes allowed while trying to make
    #: every command falsifiable, *including* the mandatory first one.
    #: Three is enough for the rewrite to land in practice; each extra
    #: round is a full LLM round-trip over the whole tasks.json, so this
    #: is a cost cap as much as a quality one.
    FALSIFIABILITY_MAX_ROUNDS = 3

    #: Path-looking tokens inside a command. Used only for the
    #: "does the command point at something that already exists?"
    #: discriminator below — not for deciding anything on its own.
    _PATH_TOKEN_RE = re.compile(
        r"[\w][\w./-]*\.(?:py|rs|ts|tsx|js|jsx|mjs|cjs|json|toml|ya?ml|css|sh)"
    )

    _CD_RE = re.compile(r"^\s*cd\s+(?P<dir>[^\s&;|]+)")

    def _effective_cwd(self, command: str, project_dir: Path) -> Path:
        """Directory ``command`` actually runs in, honouring a leading ``cd``.

        Path tokens in ``cd frontend-app && npx vitest run a/b.spec.ts``
        resolve against ``frontend-app``, not the project root. Without
        this the existence hint is wrong for every command that changes
        directory — which is most of them. The hint then reports "no such
        file" for spec files that *do* exist, which flips the task's
        ``likely`` label.
        """
        match = self._CD_RE.match(command)
        if match is None:
            return project_dir
        target = match.group("dir").strip("\"'")
        home = str(Path.home())
        if target == "$HOME" or target.startswith("$HOME/"):
            target = home + target[len("$HOME"):]
        candidate = Path(target)
        return candidate if candidate.is_absolute() else project_dir / candidate

    def _existing_paths_in(self, command: str, project_dir: Path) -> List[str]:
        """Path-looking tokens of ``command`` that exist under its cwd."""
        cwd = self._effective_cwd(command, project_dir)
        found: List[str] = []
        for token in self._PATH_TOKEN_RE.findall(command):
            if token in found:
                continue
            try:
                if (cwd / token).exists():
                    found.append(token)
            except OSError:
                continue
        return found

    def probe_falsifiability(
        self, tasks: list, timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Report which task commands already pass *before* the work runs.

        A task's completion verdict cross-checks the subagent's
        ``TEST_RESULT`` claim against the command's exit code. That
        second signal is worth nothing unless the command can fail:
        if it already exits 0 on the untouched tree, it will exit 0
        whatever the subagent does. This is the RED step of TDD,
        applied to generated tasks.

        **This reports, it does not reject.** Three different situations
        produce the same "exit 0 before the work", and they need
        different resolutions:

        * **A — the feature already exists.** The task is redundant and
          should be dropped. (Either an earlier task implemented it, or
          it predates the plan — in which case the plan was generated
          without reconciling against the current codebase.)
        * **B — the command targets an artifact that already exists and
          does not cover this task's change.** The classic shape is
          "append cases to an existing spec file": that file passes
          before and after, so its exit code never moves. The task is
          real; the command is wrong.
        * **C — the command is vacuous by construction.** Self-referential
          probes, ``echo pass``, reading a file and comparing strings.

        The probe cannot tell them apart on its own — every one of them
        looks like ``exit 0``. What it can add is the mechanical half of
        the discriminator: whether the command's path-like arguments
        already exist. A command that points *only* at pre-existing
        artifacts is B or C; one that points at something not yet on
        disk and still exits 0 is more likely A (or a runner that
        passed with nothing to run). ``likely`` in the report is that
        heuristic, labelled as such — the resolution stays a decision,
        not a silent edit.

        Results are written to ``plan_dir/falsifiability_report.json``
        and returned.
        """
        report: Dict[str, Any] = {
            "plan_id": self.plan_dir.name,
            "timeout_seconds": timeout or self.FALSIFIABILITY_TIMEOUT_S,
            "skipped": False,
            "checked": 0,
            "vacuous": [],
            "unreachable": [],
            # Commands the probe refused to execute because no workspace
            # was declared for them. See the guard in the task loop.
            "not_probed": [],
        }
        if os.environ.get(self.FALSIFIABILITY_ENV) not in (None, "", "0"):
            report["skipped"] = True
            report["skip_reason"] = f"{self.FALSIFIABILITY_ENV} is set"
            return report

        probe_timeout = timeout or self.FALSIFIABILITY_TIMEOUT_S
        cache: Dict[str, Tuple[bool, str]] = {}

        for task in tasks:
            if not isinstance(task, dict):
                continue
            project_dir = task.get("project_dir") or ""
            cwd = str(project_dir) if project_dir else None

            for reference, issue in find_unreachable_targets(task):
                report["unreachable"].append({
                    "task_id": str(task.get("id", "<no-id>")),
                    "title": str(task.get("title", ""))[:120],
                    "code": issue.code,
                    "detail": issue.detail,
                    "reference": str(reference)[:200],
                })

            for command in commands_of(task):
                # 2026-09-22 — never execute with an inherited cwd.
                #
                # With no declared workspace, ``cwd`` was ``None``, which
                # ``subprocess.run`` reads as "inherit the current
                # working directory" — i.e. the backend's own checkout. Two things
                # are wrong with that:
                #
                #   1. The verdict describes nothing. A command's exit
                #      code only means something relative to the tree it
                #      runs in; running it in the backend's cwd answers a question
                #      nobody asked.
                #   2. It EXECUTES an LLM-authored shell command inside
                #      the this repositorysitory.
                #
                # (2) is not hypothetical. the backend's own unit fixture emits
                # ``test_command="pytest tests/ -v"``; with no
                # ``project_dir`` declared, the probe ran it in
                # ``backend/`` — a nested pytest over the entire suite,
                # which re-entered this very test. The outer runner's
                # 60s timeout killed its own process but left the child
                # pytest running, and the recursive orphans starved the
                # GitHub runner until it lost communication with the
                # server 48 minutes later (runs 35725371222 / 35077046987
                # — identical signature, no logs uploaded).
                #
                # 2026-09-23: the rule now lives in the shared
                # ``probe_scope_is_usable`` predicate so the repair-side
                # probe (``RepairTaskGenerator._passes_falsifiability``)
                # cannot drift away from it — which is exactly what it
                # did when this guard was first added here alone.
                usable, unusable_reason = probe_scope_is_usable(project_dir)
                if not usable:
                    report["not_probed"].append({
                        "task_id": str(task.get("id", "<no-id>")),
                        "title": str(task.get("title", ""))[:120],
                        "command": command,
                        "detail": (
                            f"refusing to execute this command: "
                            f"{unusable_reason}"
                        ),
                    })
                    continue
                if command not in cache:
                    cache[command] = check_falsifiable(
                        command, cwd=cwd, timeout=probe_timeout,
                    )
                ok, detail = cache[command]
                report["checked"] += 1
                if ok:
                    continue
                existing = (
                    self._existing_paths_in(command, Path(project_dir))
                    if project_dir else []
                )
                report["vacuous"].append({
                    "task_id": str(task.get("id", "<no-id>")),
                    "title": str(task.get("title", ""))[:120],
                    "command": command,
                    "detail": detail,
                    "existing_paths": existing,
                    # Observed fact, not a verdict. Both values describe
                    # *why the command cannot express this task's delta*:
                    # it either points at something already on disk, or
                    # names no artifact at all — the shape of a
                    # module-wide runner filter (`cargo test --lib x`,
                    # `pytest -k y`) that passes with zero tests
                    # collected: `cargo test --lib no_such_test` exits 0.
                    "likely": (
                        "references_existing_artifact" if existing
                        else "references_no_new_artifact"
                    ),
                })

        if report["vacuous"]:
            print(
                f"[TasksGenerator] {len(report['vacuous'])} command(s) already "
                f"exit 0 BEFORE the work is done — they cannot certify a fix:"
            )
            for item in report["vacuous"]:
                print(
                    f"[TasksGenerator]   task {item['task_id']} "
                    f"({item['likely']}): {item['command'][:140]}"
                )
                if item["existing_paths"]:
                    print(
                        f"[TasksGenerator]     points at existing artifact(s): "
                        f"{', '.join(item['existing_paths'][:3])}"
                    )

        if report["unreachable"]:
            print(
                f"[TasksGenerator] {len(report['unreachable'])} unreachable "
                f"target(s) — the command or the declared path can never work:"
            )
            for item in report["unreachable"]:
                print(
                    f"[TasksGenerator]   task {item['task_id']} "
                    f"({item['code']}): {item['reference'][:140]}"
                )

        try:
            out = Path(self.plan_dir) / "falsifiability_report.json"
            out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - disk full / perms
            print(f"[TasksGenerator] could not write falsifiability report: {exc}")

        return report

    def _backfill_files_to_modify_from_description(
        self, tasks: list, project_dir: Optional[str] = None
    ) -> list:
        """Replace empty ``files_to_modify`` with sentinel and merge
        description-extracted paths so the framework validator's step-3
        never rejects.

        Two passes per task:
          1. ``files_to_modify=[]`` or missing → sentinel
             ``["__UNKNOWN_MODIFICATIONS__"]`` so validator step-3
             accepts the task.
          2. Scan the task's description for relative source-file
             paths (e.g. ``backend/agent.py``, ``tests/test_foo.py``)
             and merge into ``files_to_modify``. Salvages LLM output
             that forgot to list files explicitly but did write them
             in the description.

        Path hygiene (each candidate is skipped if):
          - empty
          - contains ``://`` (URL)
          - starts with ``/`` (absolute path — never valid here)
          - equals the sentinel itself
          - already in the existing set

        Hard cap: sentinel + at most 5 paths per task. Tasks that
        exceed this are split downstream by
        ``_enforce_files_to_modify_limit``.

        Args:
          tasks: Raw task dicts from the LLM (pre-validator).
          project_dir: Optional project root. Currently unused —
            kept for future per-workspace backfill (e.g. skipping
            paths that do not exist on disk).

        Returns:
          The same list, mutated in place.
        """
        sentinel = "__UNKNOWN_MODIFICATIONS__"
        # 2026-09-15: repo root for the pass-2b parent-exists guard.
        repo_root = self._find_repo_root()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            existing = task.get("files_to_modify")
            # Pass 1: empty / missing → sentinel.
            if not existing:
                task["files_to_modify"] = [sentinel]
                existing = [sentinel]
            # Pass 2: scan description for paths.
            description = task.get("description") or ""
            if not description:
                continue
            # 2026-09-15: restrict all three extraction passes to the
            # 修改位置 section (whole description when absent) — see
            # ``_extraction_scope``.
            scope = self._extraction_scope(description)
            seen: set = set(existing)
            merged = list(existing)
            extracted_paths: list[str] = []
            for match in self._PATH_REGEX.finditer(scope):
                path = match.group(1).strip()
                if (
                    not path
                    or "://" in path
                    or path.startswith("/")
                    or path == sentinel
                    or path in seen
                ):
                    continue
                # ``/Users/x/y.py`` matches from ``Users`` onward (no word
                # boundary before ``/``), so the leading-slash guard above
                # misses it. A match preceded by ``/`` is the tail of an
                # absolute path, not a repo-relative one.
                if match.start() > 0 and scope[match.start() - 1] == "/":
                    continue
                seen.add(path)
                extracted_paths.append(path)
            # Pass 2b (2026-09-15): extensionless module paths → ``.py``,
            # but only when the parent directory already exists in the
            # repo at generation time. A speculative path whose parent
            # does not exist yet would fail validator step-3 at dispatch
            # time (steps 3/4 are not auto-fixable — an abort, not a
            # heal), so for not-yet-created packages the safe fallback
            # is to keep the sentinel and let the dispatcher's fill loop
            # determine the real files.
            if repo_root is not None:
                for match in self._MODULE_PATH_REGEX.finditer(scope):
                    mod = match.group(1).strip()
                    if (
                        not mod
                        or any(seg.startswith(".") for seg in mod.split("/"))
                        or mod in seen
                    ):
                        continue
                    # A path introduced as a directory ("新建包目录 X",
                    # with or without a trailing slash) is not a module
                    # file — guessing ``X.py`` would fabricate a path.
                    line_start = scope.rfind("\n", 0, match.start()) + 1
                    if "目录" in scope[line_start : match.start()]:
                        continue
                    py_path = f"{mod}.py"
                    if py_path in seen:
                        continue
                    if not (repo_root / py_path).parent.is_dir():
                        continue
                    seen.add(py_path)
                    extracted_paths.append(py_path)
            # Cap ONLY the extracted paths (defensive — LLM may have
            # written 50 paths in description). Already-explicit
            # ``files_to_modify`` entries are NEVER truncated by this
            # helper; ``_enforce_files_to_modify_limit`` downstream
            # splits tasks with >5 explicit files into children.
            if len(extracted_paths) > 5:
                extracted_paths = extracted_paths[:5]
            merged.extend(extracted_paths)
            task["files_to_modify"] = merged

        # Pass 3 (2026-09-15): backfill a missing test_command.
        #
        # A task can ship with no test command at all, which silently
        # disables the command-line half of the dual-signal completion
        # rule. The generator's prompt requires the field, but nothing
        # enforced it: ``SubTask.test_command`` defaults to "" (so
        # validator step-1's ``is None`` check can never fire) and
        # validator step-4 no-ops on empty input. When the task's
        # TDD 规格 / 修改位置 sections name the test files, synthesising
        # ``pytest <files> -v`` is a pure mechanical step — the mirror
        # of pass 2 above. Runs BEFORE the verification_only tagging
        # loop below so the tag sees the backfilled command.
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if (task.get("test_command") or "").strip():
                continue
            if task.get("test_commands"):
                continue
            test_files = self._extract_test_files_from_description(
                task.get("description") or ""
            )
            if test_files:
                task["test_command"] = (
                    "backend/.venv/bin/python3 -m pytest "
                    + " ".join(test_files)
                    + " -v"
                )

        # Pass 4 (2026-09-16): detect structurally-unpassable test
        # commands. Detection moved to pass 5 below, which also acts on
        # the result; the history of why this check exists is kept
        # there. See ``test_command_quality`` for what is and is not
        # flagged.

        # 2026-09-15 audit: when a task's ``files_to_modify`` is purely
        # the sentinel (no concrete paths were extracted from the
        # description) and the task carries a non-empty test command,
        # the subagent is expected to validate runtime behaviour without
        # checking in any file diff. Tag the task ``verification_only``
        # so the empty-output gate (agent.py) does not block the
        # completion path. Perf-benchmark / smoke / harness tasks
        # generated by prior LLM rounds (e.g. an earlier plan
        # task #26 ``test_micro_bench.py``) used to fail with
        # ``task_empty_output_blocked`` because the sentinel-only
        # declaration triggered the gate despite a passing test run.
        for task in tasks:
            if not isinstance(task, dict):
                continue
            files = task.get("files_to_modify") or []
            if files == [sentinel]:
                test_cmds = task.get("test_commands") or []
                test_cmd = task.get("test_command") or ""
                if test_cmds or test_cmd:
                    if not task.get("verification_only"):
                        task["verification_only"] = True

        # Pass 5 (2026-09-20): clear — not merely warn about — the
        # commands the detector flags.
        #
        # Why the check exists (2026-09-16): a generated task can ship
        # with
        #
        #   cd … && grep -n 'TODO' … && ls -la … 2>/dev/null \
        #     && ls -la …/*.so 2>/dev/null && source … && python3 -c "…"
        #
        # ``ls``/``grep`` return non-zero when they find nothing —
        # ``2>/dev/null`` hides the message, not the status — and ``&&``
        # short-circuits, so the command could never exit 0 no matter how
        # good the code was. The gate then scored the subagent's
        # ``TEST_RESULT: PASSED`` as a lie and the task burned two retry
        # cycles plus a refiner pass before anyone looked at the command.
        #
        # Why clear rather than warn (2026-09-20): the warning-only
        # stance was argued from "blocking here would strand a plan
        # whose author could have fixed the command". That argument is
        # about *blocking the plan*, and it still holds — generation
        # must produce a task list. It is not an argument for shipping
        # the command. ``repair_generator`` already treats a flagged
        # command as absent, for the reason its own docstring gives: a
        # structurally-unpassable command is worse than none, because it
        # scores every honest ``TEST_RESULT: PASSED`` as a lie, whereas
        # an empty command degrades to the audit second pass — which can
        # actually pass. Same trade here.
        #
        # Deliberately a separate loop, after the ``verification_only``
        # tagging above: that tag keys on the task *having* a command, so
        # clearing first would silently un-tag sentinel-only tasks and
        # let the empty-output gate block them.
        cleared: List[str] = []
        # Interpreter contract (2026-09-21). A command that delegates to a
        # foreign env manager while its project ships its own virtualenv
        # is the test-report-mismatch shape: the runner resolves an
        # interpreter the project does not control, so the subagent and
        # the framework's re-run can disagree. ``_rewrite_cmd`` already
        # rewrites those onto the venv binaries; anything still carrying a
        # runner reached this point by a path that skipped that rewrite,
        # so clear it rather than ship a command that resolves the wrong
        # interpreter. See ``test_command_quality.find_foreign_env_runner``.
        _venv_by_dir: Dict[str, Optional[str]] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            bad = inspect_task(task)
            project_dir = str(task.get("project_dir") or "")
            if project_dir:
                if project_dir not in _venv_by_dir:
                    try:
                        from workspace_utils import detect_venv

                        _venv_by_dir[project_dir] = detect_venv(Path(project_dir))
                    except Exception:
                        _venv_by_dir[project_dir] = None
                bad = bad + find_interpreter_mismatch(task, _venv_by_dir[project_dir])
            if not bad:
                continue
            task_id = str(task.get("id", "<no-id>"))
            offenders = {command for command, _ in bad}
            codes = sorted({issue.code for _, issue in bad})
            cleared.append(f"{task_id}:{','.join(codes)}")
            for command, issue in bad:
                print(
                    f"[TasksGenerator] Clearing a {issue.code} "
                    f"test_command on task {task_id} — {issue.detail}"
                )
                print(f"[TasksGenerator]   was: {command[:200]}")
            # Drop the offenders from both schema forms. A task may
            # carry `test_commands` (list, canonical) and/or
            # `test_command` (string, legacy) and the two are written by
            # different code paths, so clearing only one leaves the other
            # to be run.
            if task.get("test_commands"):
                task["test_commands"] = [
                    c for c in (task.get("test_commands") or [])
                    if str(c or "").strip() not in offenders
                ]
            if str(task.get("test_command") or "").strip() in offenders:
                task["test_command"] = ""
        if cleared:
            print(
                f"[TasksGenerator] {len(cleared)} task(s) had a "
                f"test_command cleared and will self-verify instead: "
                f"{cleared}"
            )
        return tasks

    # ------------------------------------------------------------------
    # 2026-09-15 — repo-root / test-file / package-dependency helpers
    # ------------------------------------------------------------------

    def _find_repo_root(self) -> Optional[Path]:
        """Best-effort repo root for generation-time existence checks.

        ``plan_dir`` is ``<repo>/plans/<plan_id>``; the repo root is the
        first ancestor that looks like one (has ``.git`` or a ``backend/``
        directory). Returns ``None`` when neither candidate qualifies —
        callers then skip the parent-exists guard and keep the sentinel.

        ``getattr``-defensive: unit tests construct the generator via
        ``TasksGenerator.__new__`` without ``__init__``.
        """
        plan_dir = getattr(self, "plan_dir", None)
        if plan_dir is None:
            return None
        for cand in (plan_dir.parent, plan_dir.parent.parent):
            if (cand / ".git").exists() or (cand / "backend").is_dir():
                return cand
        return None

    def _extract_test_files_from_description(self, description: str) -> List[str]:
        """Ordered, deduped, ``.py``-normalised test files from a task
        description (修改位置 / TDD 规格 sections).

        The generator writes test files extensionless
        (``backend/tests/unit/test_foo``); normalise to ``.py``.

        Only real pytest modules qualify: this repo's ``pytest.ini``
        declares ``python_files = test_*.py``, so the last path segment
        must start with ``test_`` (or be ``conftest``). That keeps
        fixture directories (``backend/tests/fixtures/failure_miner/``)
        and helper modules (``backend/tests/e2e/failure_miner_helpers``)
        out of the synthesized command — pytest would collect nothing
        from them and the task's command-line signal would be useless.
        """
        files: List[str] = []
        seen: set = set()
        scope = self._extraction_scope(description)
        for match in self._TEST_FILE_REGEX.finditer(scope):
            raw = match.group(1).strip().strip("`'\"")
            if not raw or raw in seen:
                continue
            last = raw.rsplit("/", 1)[-1]
            if not last.startswith("test_") and last not in ("conftest",):
                continue
            # Directory mentions (``…/tests/fixtures/x/`` 目录) are not files.
            line_start = scope.rfind("\n", 0, match.start()) + 1
            if "目录" in scope[line_start : match.start()]:
                continue
            path = raw if "." in last else f"{raw}.py"
            if path in seen:
                continue
            seen.add(path)
            files.append(path)
        return files[:5]

    def _infer_package_dependencies(self, tasks: list) -> list:
        """Wire the semantic dependency "modules inside a package depend
        on the task that creates the package" (2026-09-15).

        Motivation: dependency inference is prose-driven (LLM field →
        前置条件 extraction → id fallback), so a dependency the prose
        never states is lost. Task descriptions routinely say
        前置条件：无 for tasks that create modules inside a package
        another task creates (包骨架 first, then normalizer.py,
        taxonomy.py, …). Under the sequential executor this is masked
        (ids run in order); under parallel dispatch or re-planning such
        tasks can run before the package exists.

        Rule: a task that *creates a package* — its description matches
        新建包[目录] <dir>, or its ``files_to_modify`` contains
        ``<pkg>/__init__.py`` — is the provider of <dir>; every other
        task whose ``files_to_modify`` contains a file under ``<dir>/``
        gets the provider appended to its ``depends_on`` (cycle-guarded).
        """
        if not tasks:
            return tasks
        by_id = {
            str(t.get("id")): t
            for t in tasks
            if isinstance(t, dict) and t.get("id")
        }
        # (dir, provider task id), deduped.
        providers: List[Tuple[str, str]] = []
        seen_prov: set = set()
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id"))
            desc = t.get("description") or ""
            dirs: List[str] = []
            for m in self._PKG_DIR_REGEX.finditer(desc):
                d = m.group(1).strip().strip("`'\"").strip("/").strip()
                if d:
                    dirs.append(d)
            for f in t.get("files_to_modify") or []:
                if isinstance(f, str) and f.endswith("/__init__.py"):
                    dirs.append(f[: -len("/__init__.py")])
            for d in dirs:
                if (d, tid) not in seen_prov:
                    seen_prov.add((d, tid))
                    providers.append((d, tid))
        if not providers:
            return tasks
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id"))
            files = [
                f for f in (t.get("files_to_modify") or [])
                if isinstance(f, str)
            ]
            # Consumers are matched on declared files AND on module paths
            # mentioned in the 修改位置 section: the pass-2b parent guard
            # keeps not-yet-created package files OUT of
            # ``files_to_modify``, but the dependency "my module lives
            # inside that package" is real regardless of whether the
            # directory exists yet.
            desc = self._extraction_scope(t.get("description") or "")
            for m in self._MODULE_PATH_REGEX.finditer(desc):
                mod = m.group(1).strip()
                if not mod or any(
                    seg.startswith(".") for seg in mod.split("/")
                ):
                    continue
                py_path = f"{mod}.py"
                if py_path not in files:
                    files.append(py_path)
            if not files:
                continue
            deps = t.setdefault("depends_on", [])
            for d, pid in providers:
                if pid == tid or pid in deps:
                    continue
                if not any(f.startswith(d + "/") for f in files):
                    continue
                if self._depends_on_would_cycle(by_id, tid, pid):
                    continue
                deps.append(pid)
        return tasks

    @staticmethod
    def _depends_on_would_cycle(
        by_id: Dict[str, dict], child_id: str, new_dep_id: str
    ) -> bool:
        """True when making ``child_id`` depend on ``new_dep_id`` closes a
        cycle (``new_dep_id`` already transitively depends on
        ``child_id``)."""
        stack = [new_dep_id]
        visited: set = set()
        while stack:
            cur = stack.pop()
            if cur == child_id:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            deps = (by_id.get(cur) or {}).get("depends_on") or []
            stack.extend(d for d in deps if isinstance(d, str))
        return False


    def _enforce_files_to_modify_limit(
        self, tasks: list, max_per_task: int = 5
    ) -> list:
        """Split any task with > ``max_per_task`` files into child tasks.

        Audit 2026-07-18: an LLM emitted a single task with 30 files
        in ``files_to_modify``; a test failure on that one task rolled
        back all 30 file changes together, and the recovery was
        painful because the diff had to be manually partitioned. To
        guarantee each task is small enough to fail/retry
        independently, this method post-processes the LLM output:

          - A task whose ``files_to_modify`` has ≤ ``max_per_task``
            entries is passed through unchanged.
          - A task whose ``files_to_modify`` has > ``max_per_task``
            entries is split into ⌈N/max⌉ children, each carrying a
            chunk of files. Children are appended in place of the
            parent; their position in the returned list is the same
            as the parent's position so downstream consumers (agent
            layer-builder, verification DAG) don't see ordering
            shifts.

        Id convention: a parent with id ``"2-3"`` produces children
        ``"2-3-1"`` and ``"2-3-2"``. A parent with id ``"5"`` produces
        ``"5-1"`` and ``"5-2"``. This matches the existing hierarchical
        id scheme used throughout the executor (see
        ``_postprocess_infer_from_id``) so the layer builder keeps
        working without changes.

        Field inheritance: every child task inherits ALL other fields
        from the parent (``title``, ``description``, ``test_command``,
        ``depends_on``, ``project_dir``, etc.). The child's title is
        rewritten to ``"<parent title> [part N/M]"`` so the operator
        can see the split at a glance. The child's ``depends_on`` is
        a copy of the parent's — children of the same parent are
        siblings and run in parallel, so they do NOT depend on each
        other.

        Boundary cases:

          - ``tasks`` is empty or ``None`` → returns ``[]`` or the
            input as-is.
          - Task has no ``files_to_modify`` key or its value is
            ``None`` → treated as ``[]``, task passes through.
          - Task's ``files_to_modify`` is not a list (e.g. a single
            string) → passed through unchanged; the field validation
            layer (``agent._load_tasks``) is responsible for raising
            a structured error in that case.
          - Task's ``files_to_modify`` has ≤ ``max_per_task`` entries
            → passed through unchanged (the common case).
          - ``max_per_task <= 0`` → treated as 1 (defensive: never
            produce an empty-child degenerate case).
        """
        if not tasks:
            return list(tasks) if tasks is not None else []
        if max_per_task <= 0:
            max_per_task = 1

        result: list = []
        for task in tasks:
            files = task.get("files_to_modify")
            # No field, None, non-list, or within budget: pass through.
            if not isinstance(files, list) or len(files) <= max_per_task:
                result.append(task)
                continue

            # Split into chunks of max_per_task. The parent task is
            # replaced by ⌈len(files) / max_per_task⌉ children.
            parent_id = str(task.get("id", ""))
            parent_title = str(task.get("title", ""))
            num_children = (len(files) + max_per_task - 1) // max_per_task
            for chunk_idx in range(num_children):
                start = chunk_idx * max_per_task
                end = start + max_per_task
                chunk = files[start:end]
                # ``copy()`` so each child is independent; a later
                # refactor that mutates a child must not bleed into
                # the parent or the next sibling.
                child = task.copy()
                child["id"] = f"{parent_id}-{chunk_idx + 1}"
                child["title"] = f"{parent_title} [part {chunk_idx + 1}/{num_children}]"
                child["files_to_modify"] = list(chunk)
                result.append(child)
        return result

    def _declared_workspaces(self) -> List[Tuple[str, str]]:
        """``[(key, path), ...]`` declared by the plan, in priority order.

        Reads PRD constraints first, then interview constraints. An empty
        list means the plan declared **no** workspace — which is a real,
        actionable answer (see :meth:`validate_workspace`), not a lookup
        that failed.
        """
        return declared_workspaces_for_plan(self.plan_dir)

    def _load_workspace_context(self) -> Optional[str]:
        """Load workspace configuration from PRD or interview.json.

        Returns a prompt block naming every declared workspace, or
        ``None`` when the plan declared none. ``None`` is deliberate: the
        caller must NOT synthesise a workspace for a plan that has not
        named one (see :meth:`validate_workspace`).

        Constraints we look for (in priority order):
          * ``target_project_dir`` — explicit user target (smoke-test / new-project)
          * ``primary_project_dir`` — main development workspace
          * any other ``*_project_dir`` field, including keys older plans
            used before ``target_project_dir`` was introduced
        """
        pairs = self._declared_workspaces()
        if not pairs:
            return None
        lines = [_workspace_line(key, value) for key, value in pairs]
        return (
            "本项目涉及以下工作区，任务必须明确归属其中一个：\n"
            + "\n".join(lines)
        )

    def _declared_workspace_dirs(self) -> List[str]:
        """Resolved paths of every workspace the plan declared, priority order.

        Duplicates collapse; unparseable values are skipped. Empty list ⇒
        the plan declared nothing.
        """
        out: List[str] = []
        for _key, value in self._declared_workspaces():
            value = value.strip() if isinstance(value, str) else ""
            if not value:
                continue
            try:
                resolved = str(Path(value).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved not in out:
                out.append(resolved)
        return out

    def validate_workspace(
        self,
        tasks_data: dict,
        search_dirs: list = None,
        formal_repo_dir: Optional[Path] = None,
    ) -> dict:
        """Bind every task to a workspace the PLAN declared — never a guess.

        Contract
        ------------------------------------
        Where work happens is the plan's call, not the framework's:

          1. **The plan declared workspace(s)** — ``target_project_dir`` /
             ``primary_project_dir`` / any other ``*_project_dir`` in the
             PRD's or interview's ``constraints``.
             That declaration is the ONLY authority:
               * one declared workspace → every task is placed in it;
               * several → each task must sit inside one of them (the
                 generation prompt lists exactly those, so the LLM's own
                 choice IS the routing decision — it is only verified);
                 a task that cannot be placed fails generation rather than
                 silently landing somewhere else.
          2. **The plan declared nothing** → do not guess. No filesystem
             scan, no candidate router, no ``-dev`` sibling heuristic.
             Tasks keep whatever ``project_dir`` they carry (usually none)
             and ``POST /api/execution/{id}/start`` supplies the working
             directory for the run.

        Why (2026-09-22): this project is meant to be published and used
        by other people, so a rule must not depend on this machine. A rule
        that reads the local filesystem —
        ``~/work``, ``-dev`` siblings — is meaningful on exactly
        one machine; for everyone else the "adaptive" behaviour is an
        inexplicable one.

        History this replaces: the method used to scan the user's home for
        marker files (``Cargo.toml`` / ``setup.py`` / ...) and hand the hits
        to an LLM router, then run a ``map_to_dev_repo`` pass that rewrote
        any path with a ``-dev`` / ``-exec`` / ``-workspace`` sibling. On
        a production plan that machinery put all 14
        tasks on the production checkout ``the production checkout``
        — the plan declared nothing, so the router picked from whatever
        happened to be on disk. Guessing is the bug, not the fix.

        The one thing the framework still refuses is targeting
        ``formal_repo_dir`` — the checkout running the backend server. That is a
        property of the framework itself, not of the user's layout, so it
        holds on every machine, and it is expressed as an EXCLUSION (never
        a redirect to some sibling directory).

        Args:
            tasks_data: tasks dict as produced by generate(), shape
                {"requirement": str, "tasks": [list of task dicts]}.
            search_dirs: accepted for backwards compatibility; unused. The
                candidate set is the plan's declaration, not a filesystem
                scan.
            formal_repo_dir: the checkout running the orchestration
                server. Never a valid target.

        Returns:
            Updated tasks_data (same dict, mutated in place and returned).

        Raises:
            TasksGenerationError: when workspaces were declared and at
                least one task cannot be placed inside any of them.
        """
        tasks = tasks_data.get("tasks", [])
        if not isinstance(tasks, list):
            return tasks_data

        declared = self._declared_workspace_dirs()
        if not declared:
            self._drop_formal_repo_targets(tasks, formal_repo_dir)
            return tasks_data

        primary = declared[0]
        if len(declared) == 1:
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                old_dir = (task.get("project_dir") or "").strip()
                # A task already inside the declared tree keeps its sub-path
                # (``declared/native_ext`` is a real place inside the
                # declared repo); everything else is placed at the root.
                if not old_dir or not _is_path_within(Path(old_dir), Path(primary)):
                    task["project_dir"] = primary
                    task["_workspace_validated"] = True
                    task["_workspace_reason"] = (
                        f"declared workspace: {primary}"
                        + (f" (was {old_dir})" if old_dir else "")
                    )
                self._ensure_project_venv(task, task["project_dir"])
            return tasks_data

        # Several declared workspaces — verify, do not re-route.
        unplaceable: List[Dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            old_dir = (task.get("project_dir") or "").strip()
            if not old_dir:
                task["project_dir"] = primary
                task["_workspace_validated"] = True
                task["_workspace_reason"] = (
                    "no project_dir emitted; defaulted to the primary "
                    f"declared workspace: {primary}"
                )
            elif not any(_is_path_within(Path(old_dir), Path(d)) for d in declared):
                unplaceable.append({"id": task.get("id"), "project_dir": old_dir})
                continue
            else:
                task["_workspace_validated"] = True
            self._ensure_project_venv(task, task["project_dir"])

        if unplaceable:
            raise TasksGenerationError(
                f"{len(unplaceable)} task(s) point outside the declared "
                f"workspaces {declared}",
                error_payload={
                    "failures": unplaceable,
                    "declared_workspaces": declared,
                    "hint": (
                        "Every task must live inside one of the plan's "
                        "declared workspaces. Fix the constraint or the "
                        "task, then re-run "
                        "POST /api/tasks/{plan_id}/generate."
                    ),
                },
            )
        return tasks_data

    def _drop_formal_repo_targets(self, tasks: list, formal_repo_dir) -> None:
        """Clear ``project_dir`` on tasks that point into the backend server checkout.

        With no declared workspace the framework has no opinion about where
        work happens — except that it will not write into the repository
        that is running the server. An empty ``project_dir`` means "whatever
        the run was started with", which is the operator's explicit choice,
        not a framework guess.
        """
        if not formal_repo_dir:
            return
        try:
            formal = Path(formal_repo_dir).resolve()
        except (OSError, RuntimeError, ValueError):
            return
        for task in tasks:
            if not isinstance(task, dict):
                continue
            old_dir = (task.get("project_dir") or "").strip()
            if not old_dir or not _is_path_within(Path(old_dir), formal):
                continue
            task["project_dir"] = ""
            task["_workspace_validated"] = True
            task["_workspace_reason"] = (
                f"refused formal-repo target {old_dir}; falling back to the "
                f"project_dir the run was started with"
            )

    def _ensure_project_venv(self, task: dict, project_dir: str) -> None:
        """Point the task's test commands at ``project_dir``'s own virtualenv.

        This is not a guess about *where* to work — the directory is already
        decided. It is a fact about the directory that was chosen: when it
        ships a virtualenv, a bare ``pytest`` resolves some other
        interpreter, and the subagent and the framework's independent re-run
        then disagree about whether the command passed. Observed live
        2026-09-21 on a production plan: the project ships
        ``venv1/``, six generated tasks said ``uv run pytest``.

        Best-effort: an unreadable or absent directory simply yields no
        annotation rather than failing generation.
        """
        if not project_dir:
            return
        try:
            from workspace_utils import detect_venv

            venv_path = detect_venv(Path(project_dir))
        except Exception:
            return
        if not venv_path:
            return
        for key in ("test_commands", "test_command"):
            value = task.get(key)
            if isinstance(value, list):
                task[key] = [
                    self._rewrite_cmd(c, project_dir, project_dir, venv_path)
                    if isinstance(c, str)
                    else c
                    for c in value
                ]
            elif isinstance(value, str) and value:
                task[key] = self._rewrite_cmd(
                    value, project_dir, project_dir, venv_path
                )
        task["_venv_injected"] = venv_path

    #: Sentinel the LLM emits when it intended a file modification but
    #: never listed the files. ``framework.task_output_validator`` step-3
    #: rejects it by design, so :meth:`harden_for_execution` reports it as
    #: a warning rather than a hard failure (see the method).
    UNRESOLVED_FILES_SENTINEL = "__UNKNOWN_MODIFICATIONS__"

    def harden_for_execution(self, tasks_data: dict) -> Dict[str, Any]:
        """Hard-validate every task against ITS OWN ``project_dir``.

        This is the ONLY place the 4-step ``TaskOutputValidator`` runs
        (2026-09-21). The execution side used to run it too — a "pre-run
        gate" in ``agent._load_tasks`` that aborted the whole run when it
        failed. That gate had three problems: it fired *after* the
        operator had been told the run started; it validated the whole
        snapshot against a single run-level ``project_dir`` while every
        task carries its own; and it therefore rejected perfectly good
        plans (a production plan: five tasks creating the
        first file in a directory that did not exist yet). Everything must be validated at task-generation time: once a plan is
executable, starting it should succeed immediately.

        Steps:

          1. Partition out tasks whose ``files_to_modify`` is the
             unresolved sentinel — they cannot be validated (and cannot
             be resolved without repo inspection, which no longer happens
             at execution time). They are returned as a warning.
          2. Group the rest by their declared ``project_dir`` (falling
             back to the single declared directory when the plan has
             exactly one). A task with no resolvable directory is
             reported, not silently dropped.
          3. Run ``framework.task_output_validator`` per group:
             ``auto_fix`` then ``validate``. Only ``depends_on`` is
             auto-fixable, and it is copied back surgically so the
             caller's dicts keep every field the validator does not
             model (``_workspace_validated``, ``_venv_injected``, ...).
          4. Any group still failing raises
             :class:`TasksGenerationError`, which the endpoint surfaces
             as HTTP 422 — the operator fixes the cause and re-generates.
             A ``tasks.json`` is never written in a state the executor
             would refuse.

        Args:
            tasks_data: The shape returned by :meth:`generate`
                (``{"requirement": ..., "tasks": [...]}``), already
                workspace-validated.

        Returns:
            The same ``tasks_data``, with ``depends_on`` auto-fixes
            applied and, when applicable, a ``harden_warnings`` entry.

        Raises:
            TasksGenerationError: When at least one task cannot be made
                runnable. ``error_payload["failures"]`` carries the
                validator's per-project_dir reasons.
        """
        from framework.task_output_validator import TaskOutputValidator
        from task import SubTask, is_unknown_modifications

        tasks = tasks_data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return tasks_data

        declared: list = []
        unresolved: list = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if is_unknown_modifications(task.get("files_to_modify") or []):
                unresolved.append(task.get("id"))
            else:
                declared.append(task)

        declared_dirs = {
            str(t["project_dir"]) for t in declared if t.get("project_dir")
        }
        # A plan with exactly one declared workspace may have tasks that
        # did not repeat it; inherit rather than skip them.
        inherited = next(iter(declared_dirs)) if len(declared_dirs) == 1 else None

        groups: Dict[str, list] = {}
        no_dir: list = []
        for task in declared:
            pd = task.get("project_dir") or inherited
            if not pd:
                no_dir.append(task.get("id"))
                continue
            groups.setdefault(str(pd), []).append(task)

        failures: List[Dict[str, Any]] = []
        for project_dir, group in groups.items():
            root = Path(project_dir)
            if not root.is_dir():
                failures.append({
                    "project_dir": project_dir,
                    "task_ids": [t.get("id") for t in group],
                    "reasons": [f"project_dir does not exist: {project_dir}"],
                })
                continue

            validator = TaskOutputValidator(root)
            fixed = validator.auto_fix([SubTask(**t) for t in group])
            report = validator.validate(fixed)
            if report.status != "passed":
                failures.append({
                    "project_dir": project_dir,
                    "task_ids": sorted(set(report.failed_task_ids)),
                    "reasons": list(report.reasons),
                    "failed_steps": list(report.failed_steps),
                })
                continue

            # Surgical copy-back: ``auto_fix`` only rewrites
            # ``depends_on``, and re-serialising the SubTask would drop
            # the bookkeeping keys the caller added.
            for original, subtask in zip(group, fixed):
                fixed_deps = list(subtask.depends_on or [])
                if fixed_deps != list(original.get("depends_on") or []):
                    original["depends_on"] = fixed_deps

        if failures:
            payload = {
                "error": (
                    "Generated tasks are not runnable; fix the cause and "
                    "re-generate. Validation runs at generation time so "
                    "that POST /api/execution/{id}/start always succeeds."
                ),
                "error_type": "tasks_not_runnable",
                "failures": failures,
                "unresolved_files_to_modify": unresolved,
                "tasks_without_project_dir": no_dir,
            }
            raise TasksGenerationError(
                (
                    f"tasks_generator: {len(failures)} project workspace(s) "
                    f"produced tasks the executor would refuse "
                    f"({sum(len(f['task_ids']) for f in failures)} task(s)); "
                    f"see tasks.json's `failures` field"
                ),
                error_payload=payload,
            )

        warnings: Dict[str, Any] = {}
        if unresolved:
            warnings["unresolved_files_to_modify"] = unresolved
        if no_dir:
            warnings["tasks_without_project_dir"] = no_dir
        if warnings:
            tasks_data["harden_warnings"] = warnings

        return tasks_data

    def _extract_user_target_dir(self, _tasks_data=None) -> Optional[str]:
        """The plan's primary declared workspace, resolved, or ``None``.

        Thin wrapper over :meth:`_declared_workspaces` — the highest
        priority declared key wins (``target_project_dir`` >
        ``primary_project_dir`` > any other ``*_project_dir`` >
        bare ``project_dir``).

        Returns ``None`` when the plan declared no workspace at all. We
        deliberately do NOT fall back to a scan of the user's filesystem
        or to the formal repo: for a plan that named nothing, guessing a
        directory is worse than leaving it unset. See
        :meth:`validate_workspace`.
        """
        for _key, value in self._declared_workspaces():
            value = value.strip() if isinstance(value, str) else ""
            if not value:
                continue
            try:
                return str(Path(value).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        return None

    def _extract_user_requirement(self) -> Optional[str]:
        """Best-effort extract of the user's stated requirement from interview.json.

        Smoke v12 surfaced a non-determinism where the LLM sometimes
        hallucinated "no PRD provided" even with 12 KB+ of inlined PRD/arch/test
        content. The fix pins the user-stated requirement (extracted from
        interview.json's ``dimensions`` field) as the leading block of the
        tasks prompt — the LLM cannot misread it as "missing".

        Contract (see tests/unit/test_tasks_generator_user_requirement.py):
          * Missing interview.json            → return None
          * interview.json without dimensions → return None
          * interview.json with dimensions    → return JSON, capped at
            4000 chars + ``…`` ellipsis if longer
          * Malformed interview.json          → return None (graceful degrade)
        """
        interview_path = self.plan_dir / "interview.json"
        if not interview_path.exists():
            return None
        try:
            with open(interview_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        dims = data.get("dimensions")
        if not isinstance(dims, dict) or not dims:
            return None
        try:
            rendered = json.dumps(dims, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return None
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "…"
        return rendered

    @staticmethod
    def _rewrite_cmd(cmd: str, old_dir: str, new_dir: str, venv_path: str = None) -> str:
        """If old_dir appears anywhere in the command, replace with new_dir.

        If the command targets a Python venv-less project and a venv_path
        is provided, inject `source <venv_path> && ` at the front so pytest
        / flask / etc. resolve without manual activation.

        A foreign env runner (``uv run`` / ``poetry run`` / …) is
        rewritten onto the project venv's own binaries *before* the
        injection check — see :meth:`_normalize_python_runner` for why
        leaving it in place is a defect rather than a convenience.

        Handles `cd /old && ...` and `/old/...` style references.
        """
        if not cmd:
            return cmd
        if old_dir and old_dir != new_dir and old_dir in cmd:
            cmd = cmd.replace(old_dir, new_dir)
        if venv_path:
            cmd = TasksGenerator._normalize_python_runner(cmd, venv_path)
        if (
            venv_path
            and not TasksGenerator._already_uses_venv(cmd, venv_path)
            and TasksGenerator._needs_venv(cmd, project_has_venv=True)
        ):
            cmd = f"source {venv_path} && {cmd}"
        return cmd

    @staticmethod
    def _already_uses_venv(cmd: str, venv_path: str) -> bool:
        """True when ``cmd`` already names a binary inside ``venv_path``'s bin/.

        Such a command resolves the project interpreter by absolute path,
        so prefixing ``source <venv>/bin/activate`` adds nothing but
        noise — and noise in a command is what makes the next reader
        misjudge which interpreter actually runs.
        """
        try:
            bin_dir = str(Path(venv_path).parent)
        except Exception:  # pragma: no cover - venv_path is a str in practice
            return False
        return bool(bin_dir) and bin_dir in cmd

    #: Env managers that run a command inside an environment *they* own.
    #: Mirrors ``test_command_quality.FOREIGN_ENV_RUNNERS``; kept local so
    #: this module does not import the validator at import time.
    _FOREIGN_ENV_RUNNERS = (
        "uv run", "poetry run", "pipenv run", "pdm run",
        "hatch run", "rye run", "conda run",
    )

    @staticmethod
    def _normalize_python_runner(cmd: str, venv_path: Optional[str]) -> str:
        """Rewrite ``<foreign runner> run <tool> …`` to ``<venv>/bin/<tool> …``.

        Only applied when the project ships its own virtualenv. Left
        alone, the runner resolves an interpreter *it* owns rather than
        the project's, so the subagent and the framework's independent
        re-run can disagree about whether the command passes — a
        test-report mismatch whose root cause is one command's phrasing.

        Returns ``cmd`` unchanged when there is no project venv (a repo
        that owns none may legitimately use ``uv``), when no runner
        prefix is present, or when the token after the runner is a flag
        (``uv run --python 3.11 pytest``) — that shape is left to the
        validator to flag rather than guessed at here.
        """
        if not cmd or not venv_path:
            return cmd
        stripped = cmd.lstrip()
        lead = cmd[: len(cmd) - len(stripped)]
        for runner in TasksGenerator._FOREIGN_ENV_RUNNERS:
            if not stripped.startswith(runner + " "):
                continue
            parts = stripped[len(runner) + 1:].split(None, 1)
            if not parts or parts[0].startswith("-"):
                return cmd
            tool, tail = parts[0], (parts[1] if len(parts) > 1 else "")
            resolved = str(Path(venv_path).parent / tool)
            return f"{lead}{resolved} {tail}".rstrip()
        return cmd

    @staticmethod
    def _needs_venv(cmd: str, project_has_venv: bool = False) -> bool:
        """Heuristic: does this command need Python venv activation?

        Returns True if the command runs pytest / python / pip / flask /
        django-admin / sphinx / etc. AND does NOT already have a `source`
        or `conda activate` prefix (which would already handle env
        activation).

        ``project_has_venv`` — when the project ships its own virtualenv,
        a foreign env manager (``uv run`` / ``poetry run`` / …) is NOT a
        valid "env already handled" marker: it swaps in an interpreter
        the project does not control. Those prefixes therefore keep the
        venv requirement set instead of clearing it.

        Defaults to ``False`` so callers with no project context keep the
        pre-existing generic behaviour.
        """
        if not cmd:
            return False
        stripped = cmd.lstrip()
        # Foreign env managers only count as "already activated" when the
        # project has no venv of its own. With one, they are the defect.
        if any(stripped.startswith(p) for p in TasksGenerator._FOREIGN_ENV_RUNNERS):
            return bool(project_has_venv)
        # Already-activated patterns → skip
        skip_prefixes = ("source ", "conda activate", "venv ")
        if any(stripped.startswith(p) for p in skip_prefixes):
            return False
        # Python tooling that lives in venv
        py_tools = ("pytest", "python ", "python3", "py.test", "pip ",
                    "flask ", "django-admin", "sphinx-", "sphinx ", "tox ",
                    "mypy ", "black ", "ruff ", "isort ", "coverage ")
        return any(t in cmd for t in py_tools)

    @staticmethod
    def _rewrite_python_cmd_with_venv(cmd: str, venv_path: Optional[str]) -> str:
        """Public hook: normalize a foreign runner, then add venv prefix."""
        if venv_path:
            cmd = TasksGenerator._normalize_python_runner(cmd, venv_path)
        if (
            venv_path
            and not TasksGenerator._already_uses_venv(cmd, venv_path)
            and TasksGenerator._needs_venv(cmd, project_has_venv=True)
        ):
            return f"source {venv_path} && {cmd}"
        return cmd
