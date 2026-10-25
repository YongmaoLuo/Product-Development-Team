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

落盘：copy-then-swap
--------------------
:func:`apply_delta` 只改内存。增量评估在复用路径上跑完就返回，磁盘上的
``verification_plan.json`` 一个字节都没动 —— 于是"作废"下一轮就复活，替换
VP 也跟着消失，修复任务照旧围绕一条已经不成立的判据生成。

:func:`apply_delta_persisted` 是"应用增量 + 让增量留下来"合成的一步：
先在深拷贝上应用，校验通过后原子替换磁盘上的计划文件，**只有写成功了**
才让调用方手里的 ``plan_data`` 换内容。任何一步失败，磁盘与内存都保持
调用前的原样 —— **内存永远不领先磁盘**。

配套的两个异常把两种失败分开：

* :class:`PlanDeltaPersistError` —— 这次增量**没有**落盘（写失败、护栏
  抛错、往返校验不过）；
* :class:`PlanChangedDuringDelta` —— 这次增量**不该**落盘（写前摘要复核
  发现计划文件已被外部改写，写回去会覆盖掉别人的改动）。

写前摘要用 :func:`plan_file_digest` 算：读入计划时记下它，写回前再算
一次，不一致就放弃这一轮增量。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from utils import atomic_io
from verification_phases import PHASE_CORE, PHASE_FINAL_GATE

#: 已废弃 VP 的标记字段（执行器据此跳过，不进任何统计桶）。
OBSOLETE_FIELD = "obsolete"
OBSOLETE_REASON_FIELD = "obsolete_reason"
OBSOLETE_ROUND_FIELD = "obsoleted_round"
#: 新增 VP 的来源标记，便于日志回溯"这条是谁加的、依据是什么"。
ADDED_REASON_FIELD = "added_reason"
ADDED_ROUND_FIELD = "added_round"
#: 替换 VP 指向"被它替换掉的那条 VP"的字段名。
#:
#: 2026-10-21：作废与替换必须**互指**才查得清"这条判据为什么不见了、换成了
#: 哪条"。方向固定为「新的指向旧的」：替换 VP 带
#: ``replacement_for = <原 VP id>``；原 VP 那一侧记的是作废痕迹
#: （``obsolete`` / ``obsolete_reason`` / ``obsoleted_round``）。
REPLACEMENT_FOR_FIELD = "replacement_for"

#: delta state 里记录"历轮分配出去的 VP id"的键。
#:
#: 2026-10-24：基础计划（磁盘上的 ``verification_plan.json``）每轮被**整份
#: 复用**，增量条目只活在内存里 —— 于是按基础计划求最大值的 id 分配器每轮
#: 都从同一个数重新开始，**同一个 id 被分了两轮**（第 3 轮的 VP-021/VP-022
#: 在第 4 轮被又分了一次，标题与判据完全不同）。而这两个 id 上一轮已经是
#: PASSED，resume 路径按 id 跳过执行，报告里留下的就是上一轮的
#: actual_result，被拿去和新 expected_result 比对后判成「验证对象错位」。
#:
#: 占位必须落在 delta state 里：**基础计划不能被改写** —— 它是「这一轮判据
#: 是什么」的唯一权威，注入占位条目会让判据与实际执行的东西对不上。
ALLOCATED_IDS_KEY = "allocated_vp_ids"

_ID_RE = re.compile(r"^VP-(\d+)$")

#: 增量新增的条目里，**原样**搬进新 VP 的可选字段（骨架之外的判据载荷）。
#:
#: 2026-10-14（VP-009 退休）：add 分支曾经只搬 id/title/related_prd_criteria/
#: verification_method/priority/expected_result/verification_phase 与审计三件套，
#: ``request`` / ``assertions`` / ``target_url`` / ``timeout_seconds`` /
#: ``execution_group`` 从不被读取 —— 落地即消失。于是新增的 ``api_test`` VP 因缺
#: ``request`` 被 :func:`verification_api_runner.validate_vp` 判为不可运行，又被
#: ``verification_agent._maybe_evaluate_plan_delta`` 的护栏**整条丢弃**。
#: 后果不是「少一条 VP」，而是「作废旧 VP + 新增一条表达正确的」这条路在框架层
#: 根本不存在 —— 只能眼看着旧判据继续失败。
#:
#: 只在 delta 条目**确实写了**该键时才搬，不给每条新增 VP 凭空塞五个键。
#:
#: 2026-10-21：``replacement_for`` 也在这里 —— 替换 VP 与被它替换掉的那条
#: VP 之间的指向关系必须落到计划文件里，否则"作废"在审计时是断的链：磁盘上
#: 只看得到一条被作废的判据，看不到接替它的是哪条。
ADD_THROUGH_FIELDS: Tuple[str, ...] = (
    "request",
    "assertions",
    "target_url",
    "timeout_seconds",
    "execution_group",
    REPLACEMENT_FOR_FIELD,
)

# ---------------------------------------------------------------------------
# 仓库强制的请求头（提示词要教给增量 LLM 的那一条）
# ---------------------------------------------------------------------------
#
# 2026-10-24：本仓库对 ``/api/*`` 强制要求一个自定义请求头
# （``backend/request_guard.py``）。不带它的 ``/api/*`` 请求一律 403，响应体
# 是错误信封而不是业务数据 —— 于是「漏了这个头」写出来的 ``api_test``，
# 它的断言**永远无法满足**。
#
# 提示词不教这件事，增量 LLM 就会再造一条同样漏头的验证点（本轮 VP-004 就是
# 这么来的）。所以这里从 ``request_guard`` 直接取名字与取值，而不是把字面量
# 抄进提示词 —— 抄一份就会漂。
try:  # pragma: no cover - 兜底分支只在 request_guard 不可导入时走到
    from request_guard import (
        GUARDED_PREFIX as _GUARDED_PREFIX,
        REQUEST_HEADER as _REQUEST_HEADER,
        REQUEST_HEADER_VALUE as _REQUEST_HEADER_VALUE,
    )
except Exception:  # noqa: BLE001 - 文案不该因为一次导入失败就整个模块不可用
    _GUARDED_PREFIX = "/api/"
    _REQUEST_HEADER = "X-PDT-Request"
    _REQUEST_HEADER_VALUE = "1"

#: 提示词里说「带上这个头」时用的写法：``X-PDT-Request: 1``。
GUARD_HEADER_SPEC = f"{_REQUEST_HEADER}: {_REQUEST_HEADER_VALUE}"


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


def next_vp_id(
    plan_data: Dict[str, Any],
    reserved_ids: Optional[Iterable[str]] = None,
    *,
    avoid_ids: Iterable[str] = (),
) -> str:
    """分配一个不与现有 VP、也不与历轮已分配 id 冲突的新 id（``VP-0NN`` 递增）。

    ``reserved_ids`` 是**历轮已经分配出去、但不在 ``plan_data`` 里**的 id：
    增量条目只活在内存里，基础计划看不到它们（见
    :data:`ALLOCATED_IDS_KEY` 的说明）。不跳过它们就会出现「同一个 id 分了
    两轮」—— 上一轮已 PASSED 的 VP 被新一轮的同名条目顶掉，而 resume 路径
    按 id 跳过执行，报告里留下的是上一轮的结果。

    ``avoid_ids`` 是同一个东西的另一个名字：**发号账本**
    （``allocated_vp_ids``）里已经发出去的号。跳过它们的理由不只是"撞号"，
    而是**继承**：那个号在 SQLite 里可能还留着上一轮的裁决（VP-031 的
    FAILED、VP-033 的 PASSED）。重新发出去同一个号，新验证点就会继承旧结论
    —— 一次都没跑就记成失败，或者一次都没跑就被当成"验证通过"而跳过
    （2026-10-25）。

    两个参数等价，取并集；跳过是**向前**跳的：id 单调递增，宁可留空号也
    不复用。

    **只有能产出完整定义的条目才配调用它**（见 :func:`vp_definition_gaps`
    与 :func:`apply_delta`）：发出去的号必须有定义跟在后面，否则分配账本上
    会留下一个"有 id、无定义"的占位 —— 而执行器对这种条目没有分派前校验，
    会按缺省方法把它派出去（2026-10-24 的 VP-031）。
    """
    highest = 0
    for vp in plan_data.get("verification_points") or []:
        if not isinstance(vp, dict):
            continue
        match = _ID_RE.match(str(vp.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    avoided = {
        str(vp_id).strip().upper()
        for vp_id in list(reserved_ids or ()) + list(avoid_ids or ())
        if str(vp_id or "").strip()
    }
    candidate = highest + 1
    while f"VP-{candidate:03d}".upper() in avoided:
        candidate += 1
    return f"VP-{candidate:03d}"


def vp_definition_gaps(vp: Dict[str, Any]) -> List[str]:
    """这条 VP 还差什么才能算一条**完整**的定义（空列表 = 完整）。

    "完整"是执行器能判它的最低要求：**有标题**（说清要断言什么），
    **方法在 ``SUPPORTED_METHODS`` 里**（框架会照它跑）。缺任何一样，
    这条 VP 就跑不出结论 —— 重做交付物也救不了它，只能由操作者重新表达。

    2026-10-24（VP-031）：这个判据以前只存在于护栏
    （``verification_agent._plan_delta_guard``），也就是**发号之后**才检查：
    号已经发出去了，条目却被整条丢掉，于是分配账本上留下一个没有定义的
    占位 id。把判据提到发号之前，缺定义的条目连号都拿不到。

    只读词汇表，不改任何东西 —— VP 的声明字段不可变：判据写错或缺失时由
    操作者重新表达，执行器不替它发明。
    """
    from verification_subagent import SUPPORTED_METHODS

    gaps: List[str] = []
    title = str(vp.get("title") or "").strip()
    if not title:
        gaps.append("缺少 title（没有标题就没有判据）")
    method = str(vp.get("verification_method") or "").strip()
    if method not in SUPPORTED_METHODS:
        gaps.append(
            f"verification_method {method or '(missing)'} 不在 "
            f"{list(SUPPORTED_METHODS)} 里"
        )
    return gaps


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

DELTA_SYSTEM_PROMPT = f"""你是资深测试架构师，负责维护一份**已经存在**的验证计划（VP 清单）。

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
   `"verification_phase": 2。

7. **`api_test` 必须自带判据载荷**。只给标题和预期结果的 `api_test` 等于
   没给：`request` / `assertions` 缺失时它跑不出任何结论，会被框架整条丢弃。
   每条新增的 `api_test` 必须写全：
   - `request`：`{{"method": "GET", "url": "{{{{svc.<name>.url}}}}/<path>",
     "headers": {{...}}, "body": null, "timeout_seconds": 30}}`。服务地址用
     占位符 `{{{{svc.<name>.url}}}}`，不要写死端口。
   - `assertions`：非空数组。每条断言**恰好一个**主体，主体只能是
     `status` / `header` / `json_path` / `body_contains` /
     `body_not_contains`；`header` 与 `json_path` 还必须再给一个比较符
     （`equals` / `contains` / `not_contains` / `exists` / `length_gte` / …），
     而 `status` / `body_contains` / `body_not_contains` 自带期望值，不要再
     加比较符。

8. **打 `{_GUARDED_PREFIX}` 的请求必须带上仓库强制的守卫头**：
   `{GUARD_HEADER_SPEC}`（见 `backend/request_guard.py`）。不带这个头的
   `{_GUARDED_PREFIX}` 请求一律 403，响应体是错误信封而不是业务数据 ——
   那样写出来的断言永远不可能满足。
   反过来，「缺该头 → 403」本身可以是**另一条**验证点的判据；两条互不影响，
   不要因为要给新验证点补这个头就去改那条守卫验证点。

9. **已有验证点的请求规格写错了怎么办**：**作废它 + 新增一条正确的**，
   不要改它（硬规则 1）。最常见的场景就是硬规则 8：已有 `api_test` 漏了那个
   请求头，它的断言因此永远无法满足。这时在 `obsolete` 里写明原因把它标记
   掉，同时在 `add` 里给出表达正确的那一条（`request` 与 `assertions` 写全）。
   作废是标记不是删除，审计痕迹要留着。

输出必须是**纯 JSON**（不要 markdown 代码块）：

{{
  "add": [
    {{
      "title": "验证点标题",
      "related_prd_criteria": "对应的验收标准 / 任务 id / 新行为说明",
      "verification_method": "api_test|code_review|ui_validation|e2e|full_ci",
      "verification_phase": 1,
      "priority": "medium",
      "expected_result": "预期结果",
      "reason": "为什么现在需要新增它（依据）",
      "request": {{                      // api_test 必填
        "method": "GET",
        "url": "{{{{svc.api.url}}}}/api/items",
        "headers": {{"Accept": "application/json", "{_REQUEST_HEADER}": "{_REQUEST_HEADER_VALUE}"}},
        "body": null,
        "timeout_seconds": 30
      }},
      "assertions": [                    // api_test 必填
        {{"name": "状态码", "status": 200}},
        {{"name": "响应类型是 JSON", "header": "content-type", "contains": "json"}}
      ]
    }}
  ],
  "obsolete": [
    {{"id": "VP-0NN", "reason": "为什么不再需要它"}}
  ]
}}

没有需要增减的，就输出 {{"add": [], "obsolete": []}} —— 这是最常见的答案。
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
        # 请求规格也要进提示词。增量 LLM 只有看得见那条请求长什么样，才发现
        # 得了「它的请求规格漏了仓库强制的请求头」——而漏头会让断言永远无法
        # 满足。没有这一段，漏头这件事**不可见**，也就无从作废它。
        request = vp.get("request")
        if isinstance(request, dict):
            headers = request.get("headers") or {}
            header_text = "、".join(
                f"{name}: {value}" for name, value in headers.items()
            ) or "（无）"
            lines.append(
                f"  └ 请求：{request.get('method', '')} "
                f"{request.get('url', '')}｜请求头：{header_text}"
            )
    lines.append("")

    lines.append("## 本轮新增的任务（执行阶段新加的工作，可能缺少覆盖）")
    if new_tasks:
        for task in new_tasks:
            # 2026-10-24：描述**不再截断**。描述是「新增必须有依据」的依据
            # 来源：截断到 200 字，增量 LLM 只能猜一个请求规格出来，而不是照
            # 着任务真正要求的接口写 —— 于是新增的 api_test 拿不到判据载荷。
            lines.append(
                f"- {task.get('id')}｜{str(task.get('title', ''))[:80]}"
                f"｜{str(task.get('description', ''))}"
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
    lines.append(
        "如果某条已有验证点的**请求规格写错了**（最常见的是打 "
        f"{_GUARDED_PREFIX} 却漏了 {GUARD_HEADER_SPEC} 这个必需请求头，"
        "断言因此永远无法满足），**不要改它**：在 obsolete 里写明原因把它作废掉，"
        "同时在 add 里新增一条表达正确的（request 与 assertions 都要写全）。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 解析与应用
# ---------------------------------------------------------------------------


def parse_delta_payload(
    raw: Any, plan_data: Dict[str, Any],
    reserved_ids: Optional[Iterable[str]] = None,
) -> PlanDelta:
    """把 LLM 返回的 JSON 规整成 :class:`PlanDelta`，并在此处执行硬约束。

    就地执行的约束：

    * ``add`` 里带 ``id`` 且该 id 已存在 → 视为"试图修改已有 VP"，拒绝并记入
      ``rejected_modifications``。
    * ``add`` 缺 ``reason`` → 拒绝（"新增必须有依据"）。
    * ``obsolete`` 指向不存在的 id／已废弃的 id → 记入 ``rejected_additions``
      之外的拒绝清单（复用 ``rejected_modifications`` 会混淆语义，这里同样
      归入 ``rejected_additions`` 之外的记录，但保留原始项）。

    ``reserved_ids`` 是历轮分配出去、但不在 ``plan_data`` 里的 id（增量条目
    只活在内存里）。复用它们同样等于「顶掉一条已经跑过/判过的验证点」，所以
    与复用基础计划里的 id 一样按"试图修改已有 VP"拒绝。
    """
    delta = PlanDelta(raw=raw if isinstance(raw, dict) else {})
    if not isinstance(raw, dict):
        return delta

    existing_ids = {
        str(vp.get("id"))
        for vp in plan_data.get("verification_points") or []
        if isinstance(vp, dict)
    }
    reserved = {str(rid) for rid in (reserved_ids or ())}
    taken_ids = existing_ids | reserved
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
        if item_id and str(item_id) in taken_ids:
            # 用户硬约束：不允许修改已有 VP。
            delta.rejected_modifications.append(
                {"id": str(item_id), "item": item,
                 "why": (
                     "试图复用已有 VP id（等价于修改已有验证点）"
                     if str(item_id) in existing_ids
                     else "试图复用历轮已分配的 VP id（增量 id 必须跨轮单调）"
                 )}
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
    reserved_ids: Optional[Iterable[str]] = None,
    *,
    avoid_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """就地应用增量，返回可写日志的摘要。

    新增的 VP 默认落 **Phase 1**，但**尊重条目自己声明的**
    ``verification_phase``：增量评估确实可能覆盖到全量关卡（例如本轮新增了
    一条 Nightly CI 要求），而把这种关卡塞进 Phase 1 会让它在每条子功能都
    还没验完时就开跑，白白阻塞整轮 —— that round就出现过一条
    「Nightly CI 全过」被按 Phase 1 排进去、整轮只能 SKIPPED 的情况
    （2026-09-19）。阶段判定权仍在规划 LLM 手里，这里只负责把它的声明带过去。

    ``reserved_ids`` 是历轮已经分配出去的 id（见 :data:`ALLOCATED_IDS_KEY`）：
    新条目必须跳过它们，id 才跨轮单调。``avoid_ids`` 是同一个东西的另一个
    名字（**发号账本**），两个参数取并集。摘要里的 ``allocated_vp_ids`` 记下
    本轮**实际分配出去**的 id（含随后被护栏丢弃的那些）—— 丢掉的 id 同样
    不能被下一轮复用。

    **发号之前先看定义**（2026-10-24，VP-031）：产不出完整定义的条目
    （没有标题，或方法不在 ``SUPPORTED_METHODS`` 里）连号都拿不到 —— 它既
    不落进计划，也不占掉一个 id。理由见 :func:`vp_definition_gaps`：号已经
    发出去了、条目却被整条丢掉，会在分配账本上留下一个"有 id、无定义"的
    占位，而执行器对这种条目没有分派前校验，会按缺省方法把它派出去。
    """
    points = plan_data.setdefault("verification_points", [])
    summary: Dict[str, Any] = {
        "added": [],
        "obsoleted": [],
        "rejected_additions": len(delta.rejected_additions),
        "rejected_obsoletes": len(delta.rejected_obsoletes),
        "rejected_modifications": len(delta.rejected_modifications),
        # 2026-10-24（VP-031）：发号之前被判"产不出完整定义"的条目。留痕
        # 是因为它们是被**丢掉的** —— 静默丢弃的话，审计里看不出"这一轮
        # 评估新增了几条、丢了几条、丢在哪儿"。
        "rejected_definitionless": [],
        # 2026-10-25（VP-031）：本次**发出去过**的号。
        #
        # 它与 ``added`` 不是一回事：``added`` 是"落进计划"的条目，而这里
        # 是"发出去过"的号 —— 护栏后来整条剔除的那些也在内（号已经发出
        # 去了，只是没落进计划）。发号账本记的是后者：这个号已经被用过，
        # 重新发出去会让新验证点继承旧号的裁决。
        "allocated_vp_ids": [],
    }
    reserved = {
        str(rid).strip().upper()
        for rid in list(reserved_ids or ()) + list(avoid_ids or ())
        if str(rid or "").strip()
    }

    for item in delta.added:
        vp = {
            # id 在下面发 —— 只在定义完整时才发（见本函数末尾的注释）。
            "id": "",
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
        # 2026-10-14: 判据载荷原样透传。见 ADD_THROUGH_FIELDS。
        for key in ADD_THROUGH_FIELDS:
            if key in item:
                vp[key] = item[key]
        # 2026-10-24（VP-031）：**先看定义，再发号**。
        #
        # 以前是"先发号、再由护栏整条丢掉"。于是分配账本上留下了一个没有
        # 定义的占位 id（VP-031），而执行器对"有 id、无定义"的条目没有
        # 分派前校验 —— 它按缺省方法把这条空壳派给了 coding tool，整轮取消
        # 后被记成 FAILED。缺定义的条目现在连号都拿不到：它既不落进计划，
        # 也不占掉一个 id。
        gaps = vp_definition_gaps(vp)
        if gaps:
            summary["rejected_definitionless"].append({
                "title": vp["title"],
                "gaps": gaps,
            })
            continue
        new_id = next_vp_id(plan_data, reserved_ids=reserved)
        reserved.add(new_id.upper())
        vp["id"] = new_id
        summary["allocated_vp_ids"].append(new_id)
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
# 原子持久化（copy-then-swap）
# ---------------------------------------------------------------------------
#
# 2026-10-21：``apply_delta`` 只改内存。增量评估在复用路径上跑完就返回，
# 磁盘上的 ``verification_plan.json`` 一个字节都没动 —— 于是"作废"下一轮就
# 复活，替换 VP 也跟着消失，修复任务照旧围绕一条已经不成立的判据生成。
#
# 修法不是"再写一次文件"，而是把"应用增量"和"让增量留下来"合成一步：
# 先在深拷贝上应用，校验通过后原子替换磁盘上的计划文件，**只有写成功了**
# 才让调用方手里的 ``plan_data`` 换内容。任何一步失败，磁盘与内存都保持
# 调用前的原样 —— 内存永远不领先磁盘。


class PlanDeltaPersistError(RuntimeError):
    """增量没有落盘。

    抛出这个异常时，**磁盘上的计划文件与调用方持有的 ``plan_data`` 都保持
    调用前的原样** —— 内存不会领先磁盘（copy-then-swap 的另一半）。
    """


class PlanChangedDuringDelta(PlanDeltaPersistError):
    """写前摘要复核发现计划文件已被外部改写。

    它是 :class:`PlanDeltaPersistError` 的子类：同样意味着"这次增量没有
    落盘"，只是原因不同 —— 不是写失败，而是**不该**写。磁盘上的计划已经
    不是调用方读入时那一份了，写回会覆盖掉别人的改动，所以宁可放弃这一轮
    增量。
    """


def plan_file_digest(plan_file: Path) -> Optional[str]:
    """计划文件内容的 sha256（十六进制）；文件不存在时返回 ``None``。

    这就是 :func:`apply_delta_persisted` 的 ``expected_digest`` 用的那个摘要：
    读入计划时记下它，写回前再算一次；不一致说明计划在增量评估期间被外部
    改过。
    """
    target = Path(plan_file)
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanDeltaPersistError(f"读计划文件失败：{target}（{exc}）") from exc


def _vp_markers(plan: Dict[str, Any]) -> Dict[str, Tuple[bool, Any]]:
    """VP id → ``(是否作废, replacement_for)``，用于往返一致性核对。

    只取这两样：它们是"作废与替换"这条链上**唯一不能丢**的标记。id 集合
    也顺带被钉住了（字典的键就是 id 集合）。
    """
    markers: Dict[str, Tuple[bool, Any]] = {}
    for vp in plan.get("verification_points") or []:
        if not isinstance(vp, dict):
            continue
        markers[str(vp.get("id"))] = (
            bool(vp.get(OBSOLETE_FIELD)),
            vp.get(REPLACEMENT_FOR_FIELD),
        )
    return markers


def _prune_summary(candidate: Dict[str, Any], summary: Dict[str, Any]) -> None:
    """把摘要裁剪到**落盘后**还站得住的那些条目。

    护栏可能把不合格的新增 VP 整条剔除。那条 VP 根本没进计划文件，摘要里
    再留着它就是撒谎 —— 而"摘要为空"正是"不写盘"的判据（见
    :func:`apply_delta_persisted` 第 4 步），留着会让一次全被拒的增量照样
    落盘。

    作废那一侧要多看一层：**撤回**作废标记时（护栏否掉了替换 VP → 原 VP
    不能先死），那条 VP 还在计划里，但落盘后它并不作废。摘要里再留着
    "已作废 VP-0NN"同样是撒谎，而且会让一次**什么都没落盘**的增量白白写
    一次盘（写出来的内容与原来一字不差）。

    所以作废的判据不是"那条 VP 还在不在"，而是"那条 VP 落盘后到底作不
    作废"—— 摘要描述的是落盘后的内容。
    """
    points = [
        vp for vp in candidate.get("verification_points") or []
        if isinstance(vp, dict)
    ]
    present = {str(vp.get("id")) for vp in points}
    obsolete_after_write = {
        str(vp.get("id")) for vp in points if vp.get(OBSOLETE_FIELD)
    }
    summary["added"] = [
        entry for entry in summary.get("added") or []
        if str(entry.get("id")) in present
    ]
    summary["obsoleted"] = [
        entry for entry in summary.get("obsoleted") or []
        if str(entry.get("id")) in obsolete_after_write
    ]


def apply_delta_persisted(
    plan_file: Path,
    plan_data: Dict[str, Any],
    delta: PlanDelta,
    round_number: int,
    *,
    post_apply: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
    expected_digest: Optional[str] = None,
    avoid_ids: Iterable[str] = (),
    reserved_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """把一次增量**原子地**落到 ``plan_file``；失败时内存不领先磁盘。

    契约（九步）：

    1. ``candidate = copy.deepcopy(plan_data)``；
    2. ``summary = apply_delta(candidate, delta, round_number)``；
    3. ``post_apply(candidate, summary)`` —— 护栏在**落盘之前**生效；
    4. 本次**发出去过**的号并进发号账本（状态文件的 ``allocated_vp_ids``）
       —— 护栏把新增整条剔除时计划一个字节都不动，但号已经发出去，必须
       记下来；
    5. ``summary["added"]`` 与 ``summary["obsoleted"]`` 都为空 → 不写盘，
       直接返回；
    6. ``payload = json.loads(json.dumps(candidate, ensure_ascii=False))`` 做
       一次 JSON 往返校验（往返后整份内容必须与 candidate 相等），并核对 VP
       id 集合与 ``obsolete`` / ``replacement_for`` 标记和 candidate 一致 ——
       **写下去的就是校验过的这一份**；
    7. ``expected_digest`` 与 ``plan_file`` 当前 sha256 不一致 → 抛
       :class:`PlanChangedDuringDelta`，不写；
    8. ``atomic_write_json(plan_file, payload, indent=2, ensure_ascii=False,
       reraise=True)``；
    9. 第 8 步成功之后才 ``plan_data.clear(); plan_data.update(payload)``
       （copy-then-swap，调用方持有的对象引用不变），并返回 ``summary``。

    第 2/3/6/7/8 步任一异常 → 抛 :class:`PlanDeltaPersistError`（第 7 步抛
    的是它的子类 :class:`PlanChangedDuringDelta`），**磁盘与 ``plan_data``
    都保持原样**。

    Args:
        plan_file: 计划文件（``verification_plan.json``）的路径。
        plan_data: 调用方持有的计划对象；落盘成功后它的内容被换成磁盘上的
            那一份（对象本身不变，引用不变）。
        delta: 本轮增量。
        round_number: 当前验证轮次（写进 ``obsoleted_round`` /
            ``added_round``）。
        post_apply: 护栏。签名 ``(candidate, summary) -> None``：它在候选
            计划上做判定（"新增的 VP 必须过 method / schema 检查"），不合格
            的整条剔除，并把 ``summary`` 改成与剔除后一致。护栏在写盘之前
            跑，所以"不合格的 VP"不会先落盘再被撤。
        expected_digest: 读入计划时记下的 sha256。写盘前复核一次：不一致
            说明计划在增量评估期间被外部改过，放弃这一轮增量而不是覆盖它。
        avoid_ids: **发号账本**里已经发出去的号（状态文件的
            ``allocated_vp_ids``）。新号必须避开它们 —— 同一个号重新发出去
            会让新验证点继承旧号的裁决（2026-10-25 的 VP-031）。
        reserved_ids: 历轮分配出去、但不在计划里的 id（占位集合）。与
            ``avoid_ids`` 取并集 —— 两者说的是同一件事的两种读法：占位用来
            拒绝"复用旧号"，账本用来避开"发过的号"。

    Returns:
        可写日志的摘要（与 :func:`apply_delta` 同一个形状）：``added`` /
        ``obsoleted`` 描述的是**落盘后**的内容。
    """
    target = Path(plan_file)

    # 1) copy-then-swap 的前半段：先在深拷贝上做，内存此刻还没被碰过。
    candidate = copy.deepcopy(plan_data)

    # 1.5) 发号账本里的号一律避开。
    #
    # 2026-10-25（VP-031）：账本是"这个号已经发出去过"的唯一记录，而它
    # 就写在**这个**状态文件里 —— 所以落盘这一步自己就知道该避开哪些号，
    # 不必指望调用方记得传。调用方传的 ``avoid_ids`` 只是额外的约束，
    # 两边取并集。
    avoid = list(avoid_ids or ()) + list(reserved_ids or ())
    for allocated_id in load_delta_state(target.parent).get("allocated_vp_ids") or []:
        key = str(allocated_id or "").strip()
        if key and key not in avoid:
            avoid.append(key)

    # 2) 应用增量。
    try:
        summary = apply_delta(
            candidate, delta, round_number, avoid_ids=avoid,
            reserved_ids=reserved_ids,
        )
    except Exception as exc:
        raise PlanDeltaPersistError(f"应用增量失败：{exc}") from exc

    # 3) 护栏：新增的 VP 必须在落盘前过完检查。
    if post_apply is not None:
        try:
            post_apply(candidate, summary)
        except Exception as exc:
            raise PlanDeltaPersistError(f"增量护栏失败：{exc}") from exc

    # 3.5) 摘要只描述落盘后还站得住的条目（护栏剔除掉的不算"已新增"）。
    _prune_summary(candidate, summary)

    # 4) 发号账本：本次发出的号必须记进 ``allocated_vp_ids``。
    #
    # 2026-10-25（VP-031）：这个键磁盘上早就有（就在这个状态文件里），却
    # 从来没有写者 —— 全仓没有任何代码读写它。于是"这个号已经发出去过"
    # 无处可查，下一次发号会重新拿到同一个号（计划最高 VP-030 → 又发
    # VP-031），新验证点于是继承旧号的裁决。
    #
    # 这一步**在"写盘"之前**，因为它记的是"号发出去了"，不是"条目落盘
    # 了"：护栏把新增整条剔除时（VP-031 的来历）计划一个字节都不会动，
    # 但号已经发出去 —— 那个号必须被记下来，不能再发第二次。
    #
    # 记的是**发出去过**的号（``summary["allocated_vp_ids"]``），不是
    # "落进计划"的号（``summary["added"]``）。
    #
    # 只往**已经存在**的状态文件里并：那个文件归调用方所有（agent 每轮都
    # 写它），本函数不创建它 —— 计划落盘这一步的契约是"计划目录里除计划
    # 文件外不留下本函数产生的文件"
    # （``tests/integration/test_plan_delta_persisted.py`` 钉的就是这一条）。
    # 文件不存在时本次并账被跳过，号仍在摘要里，调用方可以自己落盘。
    issued = [
        str(vp_id).strip()
        for vp_id in (summary.get("allocated_vp_ids") or [])
        if str(vp_id or "").strip()
    ]
    if issued and (target.parent / _DELTA_STATE_FILENAME).is_file():
        record_allocated_vp_ids(target.parent, issued)

    # 5) 全部被拒 → 不写盘，内存也不动。
    if not summary["added"] and not summary["obsoleted"]:
        return summary

    # 5) JSON 往返校验：写下去的就是校验过的这一份。
    #    ``json.dumps`` 除了 ``TypeError``（不可序列化） / ``ValueError``
    #    （循环引用）还会抛 ``RecursionError``（结构深到编码器自己撑不住）。
    #    契约是"这一步任一异常 → PlanDeltaPersistError" —— 漏出去一个裸异常，
    #    调用方拿到的就不是"这次增量没有落盘"这个可处理的信号。
    try:
        payload = json.loads(json.dumps(candidate, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise PlanDeltaPersistError(
            f"计划内容无法通过 JSON 往返校验：{exc}"
        ) from exc
    if payload != candidate:
        # 往返之后内容变了（元组变列表、整型键变字符串键……）：写下去的就不是
        # 调用方手里那份计划了，落盘等于悄悄换了一份计划。
        raise PlanDeltaPersistError(
            "计划内容 JSON 往返后与原内容不一致，拒绝落盘"
        )
    if _vp_markers(payload) != _vp_markers(candidate):
        raise PlanDeltaPersistError(
            "计划内容 JSON 往返后发生变化（VP 集合或作废/替换标记漂了），"
            "拒绝落盘"
        )

    # 6) 写前摘要复核：计划在增量评估期间被别人改过 → 放弃这一轮。
    if expected_digest is not None:
        current = plan_file_digest(target)
        if current != expected_digest:
            raise PlanChangedDuringDelta(
                f"计划文件在增量评估期间被改写：期望摘要 {expected_digest}，"
                f"实际 {current}"
            )

    # 7) 原子替换：临时文件 + fsync + os.replace，读者不会看到半个文件。
    try:
        atomic_io.atomic_write_json(
            target, payload, indent=2, ensure_ascii=False, reraise=True,
        )
    except OSError as exc:
        raise PlanDeltaPersistError(f"计划文件落盘失败：{exc}") from exc
    except (TypeError, ValueError) as exc:
        raise PlanDeltaPersistError(f"计划文件落盘失败：{exc}") from exc

    # 8) 只有写成功了才让内存生效（调用方持有的对象引用不变）。
    plan_data.clear()
    plan_data.update(payload)

    # 9) 摘要：描述落盘后的内容。
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
    """写增量评估状态。

    ``allocated_vp_ids``（**发号账本**）与"看过哪些任务"住在同一个文件
    里，而写这个文件的调用方各写各的：``verification_agent`` 写的是"看过
    哪些任务"，发号账本由 :func:`record_allocated_vp_ids` 写。谁后写谁
    覆盖整份文件 —— 账本会被"看过哪些任务"那次写冲掉，"已经发出去过"这个
    事实于是只活在一轮里。

    所以这里做**合并**：调用方没带这个键时，保留磁盘上已有的账本。
    """
    path = Path(plan_dir) / _DELTA_STATE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        if "allocated_vp_ids" not in payload:
            existing = load_delta_state(plan_dir).get("allocated_vp_ids")
            if existing:
                payload["allocated_vp_ids"] = list(existing)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass  # 记忆文件写不进去不影响本轮评估


# ---------------------------------------------------------------------------
# 历轮分配过的 VP id（跨轮占位）
# ---------------------------------------------------------------------------


def _vp_id_sort_key(vp_id: str) -> Tuple[int, str]:
    """按数字段排序（``VP-999`` 必须排在 ``VP-1000`` 前面）。"""
    match = _ID_RE.match(str(vp_id))
    number = int(match.group(1)) if match else 0
    return (number, str(vp_id))


def allocated_vp_ids(state: Dict[str, Any]) -> Set[str]:
    """读出 state 里记着的"历轮分配过的 VP id"。

    认不出的 id（格式不对、不是 VP-0NN）直接丢掉 —— 占位集合宁可少一个，
    也不能把乱七八糟的字符串当成 id。
    """
    raw = (state or {}).get(ALLOCATED_IDS_KEY) or []
    if not isinstance(raw, (list, tuple, set)):
        return set()
    out: Set[str] = set()
    for item in raw:
        text = str(item)
        if _ID_RE.match(text):
            out.add(text)
    return out


def merge_allocated_ids(
    state: Dict[str, Any],
    summary: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """把本轮分配出去的 id 累加进 state 的 ``allocated_vp_ids``。

    返回累加后的完整列表（按数字段升序）。**不就地修改** ``state`` ——
    落盘由调用方决定（:func:`save_delta_state`）。
    """
    ids = allocated_vp_ids(state)
    for extra in ((summary or {}).get("allocated_vp_ids") or []):
        text = str(extra)
        if _ID_RE.match(text):
            ids.add(text)
    return sorted(ids, key=_vp_id_sort_key)


def record_allocated_vp_ids(
    plan_dir: Path, vp_ids: Iterable[str],
) -> List[str]:
    """把发出去的号并进发号账本，返回合并后的账本。

    这个键是"这个号已经发出去过"的**唯一**记录。下一次发号必须避开它
    （见 :func:`next_vp_id` 的 ``avoid_ids``）：一个被跳过、从未落进计划的
    号（VP-031 那样）如果被重新发出去，新验证点会继承它在 SQLite 里的旧
    结论 —— 一次都没跑就记成失败，或者一次都没跑就被当成"验证通过"。

    账本是**累加**的：这一轮发出的号并进去，之前发出去的号不会被抹掉。

    **只往已经存在的状态文件里并**（见
    :func:`apply_delta_persisted` 第 4 步的说明）：增量状态文件归调用方
    所有，本函数不创建它。文件不存在时账本只存在于返回值里。
    """
    state = load_delta_state(plan_dir)
    allocated: List[str] = []
    for item in state.get("allocated_vp_ids") or []:
        key = str(item or "").strip()
        if key and key not in allocated:
            allocated.append(key)
    for vp_id in vp_ids or ():
        key = str(vp_id or "").strip()
        if key and key not in allocated:
            allocated.append(key)
    path = Path(plan_dir) / _DELTA_STATE_FILENAME
    if path.is_file():
        state["allocated_vp_ids"] = allocated
        save_delta_state(plan_dir, state)
    return allocated
