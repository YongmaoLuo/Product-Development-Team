"""验证计划的**增量**评估（2026-09-16）。

要解决的问题
------------
VP 清单在验证第一轮生成后就固定了：``generate_verification_plan`` 只要发现
磁盘上有计划就整份复用，而修复轮新增的任务（``repair-r*``）**不会带出任何
新 VP**。于是"修好了一个没人验的东西"这件事在机制上不可见——循环可以带着
未验证的修复通过。

于是需要：每次验证开始前重新评估一次 VP 方案的合理性，决定是否增减。

为什么必须是"增量"而不是"重新生成"
----------------------------------
2026-09-15 的 verdict 出处机制（结论只对产生它的那条命令有效）在这里会
反咬：只要 LLM 顺手改写了某条已有 VP 的 ``test_command``，那条 VP 此前挣到
的 PASSED 结论立刻因指纹失效而作废，**每一轮都要把已经通过的 VP 全部重跑**，
循环永远收敛不了。

所以本模块把动作**限死为两个**：

* ``add`` —— 新增 VP（落 Phase 1；同样要有界命令、同样过护栏）
* ``obsolete`` —— 废弃 VP（标记而非删除，保留审计痕迹）

**修改已有 VP 一律拒绝**，并在结果里记录 ``rejected_modifications`` 以便
操作者从日志回溯 LLM 是否试图越界。不做人工审批、不做数量
上限，只要有日志可回溯。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from verification_phases import PHASE_CORE, PHASE_FINAL_GATE

#: 已废弃 VP 的标记字段（执行器据此跳过，不进任何统计桶）。
OBSOLETE_FIELD = "obsolete"
OBSOLETE_REASON_FIELD = "obsolete_reason"
OBSOLETE_ROUND_FIELD = "obsoleted_round"
#: 新增 VP 的来源标记，便于日志回溯"这条是谁加的、依据是什么"。
ADDED_REASON_FIELD = "added_reason"
ADDED_ROUND_FIELD = "added_round"

_ID_RE = re.compile(r"^VP-(\d+)$")


# ---------------------------------------------------------------------------
# 输入采集
# ---------------------------------------------------------------------------


def load_tasks(plan_dir: Path) -> List[Dict[str, Any]]:
    """读 ``tasks.json`` 的任务列表（读不到就返回空表）。"""
    path = Path(plan_dir) / "tasks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else data
    return [t for t in tasks or [] if isinstance(t, dict)]


def collect_new_tasks(
    plan_dir: Path, seen_task_ids: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """返回 ``(本轮新增的任务, 当前全部任务 id)``。

    "新增"= 不在 ``seen_task_ids`` 里的任务。修复轮追加的 ``repair-r*``
    正是靠这条判据被识别出来。
    """
    seen = set(seen_task_ids or [])
    tasks = load_tasks(plan_dir)
    all_ids = [str(t.get("id")) for t in tasks if t.get("id") is not None]
    fresh = [t for t in tasks if str(t.get("id")) not in seen]
    return fresh, all_ids


def collect_acceptance(plan_dir: Path) -> List[str]:
    """从 ``prd.json`` 收集验收标准条目（供"新增必须有依据"判定）。"""
    path = Path(plan_dir) / "prd.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    acceptance = data.get("acceptance") if isinstance(data, dict) else None
    out: List[str] = []
    if isinstance(acceptance, list):
        for item in acceptance:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("criteria") or item.get("description") or item.get("title")
                if text:
                    out.append(str(text))
    elif isinstance(acceptance, dict):
        out.extend(f"{k}: {v}" for k, v in acceptance.items())
    return out


def next_vp_id(plan_data: Dict[str, Any]) -> str:
    """分配一个不与现有 VP 冲突的新 id（``VP-0NN`` 递增）。"""
    highest = 0
    for vp in plan_data.get("verification_points") or []:
        if not isinstance(vp, dict):
            continue
        match = _ID_RE.match(str(vp.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"VP-{highest + 1:03d}"


# ---------------------------------------------------------------------------
# 结果契约
# ---------------------------------------------------------------------------


@dataclass
class PlanDelta:
    """增量评估结果。``rejected_modifications`` 是审计用：LLM 试图改动
    已有 VP 时在这里留痕（可回溯）。"""

    added: List[Dict[str, Any]] = field(default_factory=list)
    obsoleted: List[Dict[str, Any]] = field(default_factory=list)
    rejected_additions: List[Dict[str, Any]] = field(default_factory=list)
    rejected_obsoletes: List[Dict[str, Any]] = field(default_factory=list)
    rejected_modifications: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.obsoleted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": list(self.added),
            "obsoleted": list(self.obsoleted),
            "rejected_additions": list(self.rejected_additions),
            "rejected_obsoletes": list(self.rejected_obsoletes),
            "rejected_modifications": list(self.rejected_modifications),
        }


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

DELTA_SYSTEM_PROMPT = """你是资深测试架构师，负责维护一份**已经存在**的验证计划（VP 清单）。

本轮你的唯一职责是判断：这份计划需不需要**新增**或**废弃**验证点。
**只允许这两个动作。**

硬规则（违反即作废）：

1. **绝不修改已有验证点**——标题、断言、方法都不许改。
   已有 VP 的判定依据一旦改动，它此前挣到的通过结论会因出处指纹失效而作废，
   导致整轮重跑。需要改判据时由操作者显式重新生成计划，不在你的职责内。
2. **新增必须有依据**：每条新增都要指出它对应哪条验收标准、哪个本轮新增
   的任务、或哪个本轮改动引入的新行为。没有依据的新增一律不接受。
3. **废弃要谨慎**：只有当该验证点对应的功能/验收标准已不存在或被明确移除
   时才废弃。废弃是标记，不是删除（保留审计痕迹）。
4. 验证点是**黑盒验收**视角：从用户可见行为、公开接口、真实链路出发。
   **不要**新增"再跑一遍单元测试"这类与执行阶段重复的开发自测。
5. 数量以现实为准，不设上限；但宁缺毋滥——没有依据就不要加。
6. **方法与阶段必须配套**。验证点里**没有**"可执行命令"字段 —— 判据由
   方法自己决定，由框架或证据核对来判定：

   | 方法 | 阶段 | 用途 |
   |---|---|---|
   | `api_test` | 1 | 打真实接口、按断言判定（框架执行，无 LLM） |
   | `code_review` | 1 | 代码审查，必须给出可核对的引用（citations） |
   | `ui_validation` | 1 | 浏览器实测单条交互 |
   | `e2e` | **2** | 真实浏览器跑完整链路 |
   | `full_ci` | **2** | 整仓门禁（lint + 全部测试），必须给 `ci_entry` 指向仓库自己的 CI 入口 |

   `e2e` / `full_ci` **只用于 Phase 2**；写在 Phase 1 上会被就地提升到
   Phase 2。**「Nightly CI 全过」这类整仓门禁属于 Phase 2**，不要写成
   Phase 1 —— 写在 Phase 1 会让它在每条子功能都没验完时就开跑，把整轮堵死。
   新增的验证点默认落 Phase 1，只有这类全量关卡才写
   `"verification_phase": 2`。

输出必须是**纯 JSON**（不要 markdown 代码块）：

{
  "add": [
    {
      "title": "验证点标题",
      "related_prd_criteria": "对应的验收标准 / 任务 id / 新行为说明",
      "verification_method": "api_test|code_review|ui_validation|e2e|full_ci",
      "verification_phase": 1,
      "expected_result": "预期结果",
      "reason": "为什么现在需要新增它（依据）"
    }
  ],
  "obsolete": [
    {"id": "VP-0NN", "reason": "为什么不再需要它"}
  ]
}

没有需要增减的，就输出 {"add": [], "obsolete": []} —— 这是最常见的答案。
"""


def build_delta_prompt(
    plan_data: Dict[str, Any],
    new_tasks: List[Dict[str, Any]],
    acceptance: List[str],
    round_number: int,
) -> str:
    """构造增量评估的用户提示词。"""
    lines: List[str] = [f"当前验证轮次：第 {round_number} 轮", ""]

    lines.append("## 现有验证点清单（禁止修改其中任何一条）")
    for vp in plan_data.get("verification_points") or []:
        if not isinstance(vp, dict):
            continue
        flag = " [已废弃]" if vp.get(OBSOLETE_FIELD) else ""
        lines.append(
            f"- {vp.get('id')}{flag}｜{vp.get('verification_method', '')}"
            f"｜{str(vp.get('title', ''))[:80]}"
            f"｜依据：{str(vp.get('related_prd_criteria', ''))[:120]}"
        )
    lines.append("")

    lines.append("## 本轮新增的任务（执行阶段新加的工作，可能缺少覆盖）")
    if new_tasks:
        for task in new_tasks:
            lines.append(
                f"- {task.get('id')}｜{str(task.get('title', ''))[:80]}"
                f"｜{str(task.get('description', ''))[:200]}"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## PRD 验收标准")
    if acceptance:
        for item in acceptance:
            lines.append(f"- {str(item)[:200]}")
    else:
        lines.append("（未读取到验收标准）")
    lines.append("")

    lines.append(
        "请判断是否需要新增或废弃验证点。记住：修改已有验证点是被禁止的；"
        "新增必须给出依据；没有需要增减时返回空的 add / obsolete。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 解析与应用
# ---------------------------------------------------------------------------


def parse_delta_payload(
    raw: Any, plan_data: Dict[str, Any],
) -> PlanDelta:
    """把 LLM 返回的 JSON 规整成 :class:`PlanDelta`，并在此处执行硬约束。

    就地执行的约束：

    * ``add`` 里带 ``id`` 且该 id 已存在 → 视为"试图修改已有 VP"，拒绝并记入
      ``rejected_modifications``。
    * ``add`` 缺 ``reason`` → 拒绝（"新增必须有依据"）。
    * ``obsolete`` 指向不存在的 id／已废弃的 id → 记入 ``rejected_additions``
      之外的拒绝清单（复用 ``rejected_modifications`` 会混淆语义，这里同样
      归入 ``rejected_additions`` 之外的记录，但保留原始项）。
    """
    delta = PlanDelta(raw=raw if isinstance(raw, dict) else {})
    if not isinstance(raw, dict):
        return delta

    existing_ids = {
        str(vp.get("id"))
        for vp in plan_data.get("verification_points") or []
        if isinstance(vp, dict)
    }
    already_obsolete = {
        str(vp.get("id"))
        for vp in plan_data.get("verification_points") or []
        if isinstance(vp, dict) and vp.get(OBSOLETE_FIELD)
    }

    for item in raw.get("add") or []:
        if not isinstance(item, dict):
            continue
        if not str(item.get("title", "")).strip():
            delta.rejected_additions.append(
                {"item": item, "why": "缺少 title"}
            )
            continue
        item_id = item.get("id")
        if item_id and str(item_id) in existing_ids:
            # 用户硬约束：不允许修改已有 VP。
            delta.rejected_modifications.append(
                {"id": str(item_id), "item": item,
                 "why": "试图复用已有 VP id（等价于修改已有验证点）"}
            )
            continue
        if not str(item.get("reason", "")).strip():
            delta.rejected_additions.append(
                {"item": item, "why": "缺少 reason（新增必须有依据）"}
            )
            continue
        delta.added.append(dict(item))

    for item in raw.get("obsolete") or []:
        if not isinstance(item, dict):
            continue
        vp_id = str(item.get("id", ""))
        if not vp_id or vp_id not in existing_ids:
            delta.rejected_obsoletes.append(
                {"item": item, "why": f"obsolete 指向不存在的 VP：{vp_id!r}"}
            )
            continue
        if vp_id in already_obsolete:
            continue  # 幂等：已经废弃过的不重复记
        if not str(item.get("reason", "")).strip():
            delta.rejected_obsoletes.append(
                {"item": item, "why": "obsolete 缺少 reason"}
            )
            continue
        delta.obsoleted.append({"id": vp_id, "reason": str(item["reason"])})

    return delta


def _declared_phase(item: Dict[str, Any]) -> int:
    """Read an added item's ``verification_phase``; illegal → Phase 1.

    The planner owns phase assignment (see the ``verification_phases``
    module docstring); this only carries its declaration through.
    """
    try:
        phase = int(item.get("verification_phase", PHASE_CORE))
    except (TypeError, ValueError):
        return PHASE_CORE
    return PHASE_FINAL_GATE if phase == PHASE_FINAL_GATE else PHASE_CORE


def apply_delta(
    plan_data: Dict[str, Any],
    delta: PlanDelta,
    round_number: int,
) -> Dict[str, Any]:
    """就地应用增量，返回可写日志的摘要。

    新增的 VP 默认落 **Phase 1**，但**尊重条目自己声明的**
    ``verification_phase``：增量评估确实可能覆盖到全量关卡（例如本轮新增了
    一条 Nightly CI 要求），而把这种关卡塞进 Phase 1 会让它在每条子功能都
    还没验完时就开跑，白白阻塞整轮 —— that round就出现过一条
    「Nightly CI 全过」被按 Phase 1 排进去、整轮只能 SKIPPED 的情况
    （2026-09-19）。阶段判定权仍在规划 LLM 手里，这里只负责把它的声明带过去。
    """
    points = plan_data.setdefault("verification_points", [])
    summary: Dict[str, Any] = {
        "added": [],
        "obsoleted": [],
        "rejected_additions": len(delta.rejected_additions),
        "rejected_obsoletes": len(delta.rejected_obsoletes),
        "rejected_modifications": len(delta.rejected_modifications),
    }

    for item in delta.added:
        new_id = next_vp_id(plan_data)
        vp = {
            "id": new_id,
            "title": str(item.get("title", "")).strip(),
            "related_prd_criteria": str(item.get("related_prd_criteria", "")),
            # 2026-09-19: 默认值不再是退休的 ``automated_test``。空串会被
            # ``_maybe_evaluate_plan_delta`` 的护栏整条丢掉 —— "宁可少一条
            # VP，也不要一条永远跑不出结论的验收点"，而不是凭空造一个已经
            # 不存在的 method 塞进计划。
            "verification_method": str(item.get("verification_method", "")),
            "priority": str(item.get("priority", "medium")),
            "expected_result": str(item.get("expected_result", "验证通过")),
            # 2026-09-19: VP 早已没有 ``test_command``（C8 删掉了它）。这里
            # 曾经把 delta 里的 ``test_command`` 透传进来，等于给新 VP 重新
            # 塞一个已经退休的字段，让下游按"有命令可跑"去理解它。
            "verification_phase": _declared_phase(item),
            ADDED_REASON_FIELD: str(item.get("reason", "")),
            ADDED_ROUND_FIELD: round_number,
        }
        points.append(vp)
        summary["added"].append(
            {"id": new_id, "title": vp["title"], "reason": vp[ADDED_REASON_FIELD]}
        )

    by_id = {
        str(vp.get("id")): vp
        for vp in points
        if isinstance(vp, dict)
    }
    for item in delta.obsoleted:
        vp = by_id.get(item["id"])
        if vp is None:
            continue
        vp[OBSOLETE_FIELD] = True
        vp[OBSOLETE_REASON_FIELD] = item["reason"]
        vp[OBSOLETE_ROUND_FIELD] = round_number
        summary["obsoleted"].append(
            {"id": item["id"], "reason": item["reason"]}
        )

    return summary


# ---------------------------------------------------------------------------
# 评估状态（跨轮记忆"哪些任务已经看过"）
# ---------------------------------------------------------------------------

_DELTA_STATE_FILENAME = "verification_plan_delta_state.json"


def load_delta_state(plan_dir: Path) -> Dict[str, Any]:
    path = Path(plan_dir) / _DELTA_STATE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_delta_state(plan_dir: Path, state: Dict[str, Any]) -> None:
    path = Path(plan_dir) / _DELTA_STATE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass  # 记忆文件写不进去不影响本轮评估
