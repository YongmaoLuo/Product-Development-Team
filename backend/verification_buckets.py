"""verdicts → 四个索引桶的**唯一**派生规则。

背景
----
「这个 VP 算完成 / 失败 / 跳过 / 延后」这条规则以前只长在写侧的实例方法
``VerificationExecutor._backfill_index_lists_from_verdicts`` 里。读侧（报告、
卡片、API）要回答同一个问题时只能另写一份，而两份规则必然漂移 —— 2026-10-14
那轮「计划第 4 轮自己 ``overall_status = FAILED``」就是漂移的产物。

本模块把规则抽成两个**纯函数**，写侧和读侧都从这里取：

    bucket_for_status(status, vp_id=None, retired=None) -> str | None
    derive_index_buckets(verdicts, *, retired=None) -> dict[str, list[str]]

纯函数的含义（与「读侧能不能复用」直接相关）：

  * 不碰 I/O、不碰全局状态、不改入参；
  * 输出只由入参决定 —— 同一个裁决表永远派生出同一份桶。

桶的语义（与写侧现状逐条对齐）
--------------------------------
  * ``completed_vps`` = PASSED（终态成功）
  * ``failed_vps``    = FAILED / BLOCKED（要重跑）
  * ``skipped_vps``   = SKIPPED / SPLIT（终态成功，重跑也只会再跳一次）
  * ``deferred_vps``  = DEFERRED（**刻意不进任何终态桶**）

``BaseExecutor.run`` 的 ``completed_set`` 是 completed ∪ skipped，两者都不在
里面才会在下一轮重跑 —— 这正是 DEFERRED 的语义：Phase 1 修好之后要把全量
关卡真正跑一遍。

计划作废（retired）的两条规则：

  * ``obsolete``（计划自己不要这个判据了）的 FAILED **不进任何桶** ——
    为一个没人要的判据生产失败，会把失败统计和修复任务的输入一起污染。
  * ``superseded_by``（父 VP 被子 VP 取代）的 FAILED 进 **skipped** ——
    否则下一轮会把拆分过的那条大 VP 重跑一遍，那正是拆分要避免的代价。

重复裁决（同一个 VP 先 FAILED 后 PASSED）由**调用方**归一：裁决表是 dict，
后写覆盖，函数只读一遍表格，自然只看到最后那条。这个模块**不修改入参**。

常量值就是 ``derive_index_buckets`` 返回的键名，因此两个函数可以直接互相
索引：``buckets[bucket_for_status(status, vp_id=vp_id, retired=retired)]``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

#: 四个索引桶的名字。**值即返回 dict 的键名** —— 见模块 docstring 末段。
BUCKET_COMPLETED = "completed_vps"
BUCKET_FAILED = "failed_vps"
BUCKET_SKIPPED = "skipped_vps"
BUCKET_DEFERRED = "deferred_vps"

#: 计划作废的两种标记（与 ``VerificationExecutor._vp_retired_marker`` 同名）。
RETIRED_OBSOLETE = "obsolete"
RETIRED_SUPERSEDED_BY = "superseded_by"


def _retired_marker(vp_id: Any, retired: Optional[Mapping[str, str]]) -> Optional[str]:
    """这个 vp_id 被计划作废的标记（``obsolete`` / ``superseded_by`` / ``None``）。

    ``retired`` 的键是 **字符串** 形式的 VP id（``VerificationExecutor.
    _plan_retired_vps`` 就是这么建的），所以查表前先 ``str()``。
    """
    if vp_id is None or not retired:
        return None
    try:
        return retired.get(str(vp_id))
    except AttributeError:  # pragma: no cover — 传了不是 mapping 的东西
        return None


def bucket_for_status(
    status: Any,
    vp_id: Any = None,
    retired: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """一条裁决属于哪个桶；不属于任何桶时返回 ``None``。

    :param status: 裁决的 ``status`` 字段（``"PASSED"`` / ``"FAILED"`` /
        ``"BLOCKED"`` / ``"SKIPPED"`` / ``"SPLIT"`` / ``"DEFERRED"``）。
    :param vp_id: 该裁决所属的 VP id —— 只有它能去 ``retired`` 里查作废标记。
    :param retired: ``{vp_id: "obsolete" | "superseded_by"}``，即「计划自己
        作废了哪些 VP」。``None`` 表示没有 VP 被作废。
    :return: 桶名（``BUCKET_*`` 之一）或 ``None``（不进任何桶）。

    未知 / 缺失的 status 返回 ``None`` —— **不猜、不兜底成 failed**。把
    一条看不懂的裁决报成失败，比让它不进任何桶更糟：前者会凭空制造一个修复
    任务，后者只是让这个 VP 再跑一轮。
    """
    if not isinstance(status, str):
        return None

    if status == "FAILED":
        # 计划作废的两条规则只作用在 FAILED 上（与写侧现状一致）。
        marker = _retired_marker(vp_id, retired)
        if marker is not None:
            if marker == RETIRED_SUPERSEDED_BY:
                # 父 VP 已被子 VP 取代，结论由子 VP 承担。仍进 skipped
                # （= ``completed_set``），否则下一轮会把拆分过的那条大 VP 重跑。
                return BUCKET_SKIPPED
            # ``obsolete``（以及将来可能出现的其它作废标记）：计划不再要求
            # 这个判据 —— 报成失败是凭空制造失败。
            return None
        return BUCKET_FAILED

    if status == "BLOCKED":
        # BLOCKED 是「验不下去」（例如二进制过期），不是「验过了但没过」。
        # 它必须以失败的身份出现在失败桶里，否则没人会去修它。
        return BUCKET_FAILED

    if status == "PASSED":
        return BUCKET_COMPLETED

    if status in ("SKIPPED", "SPLIT"):
        # SPLIT：修复阶段的裁判把这个 VP 拆成了子 VP。父 VP 不能再跑 ——
        # 子 VP 已经在计划里，真实的结论在子 VP 身上。归到 SKIPPED 就是让它
        # 进 ``completed_set``，续跑时只跑子 VP。
        return BUCKET_SKIPPED

    if status == "DEFERRED":
        # Phase 2 关卡被门禁延后。**刻意不进任何终态成功桶**。
        return BUCKET_DEFERRED

    # 未知 / None status：不进任何桶。
    return None


def derive_index_buckets(
    verdicts: Optional[Mapping[str, Any]],
    *,
    retired: Optional[Mapping[str, str]] = None,
) -> Dict[str, List[str]]:
    """整张裁决表 → 四个索引桶。

    :param verdicts: ``{vp_id: verdict}``。verdict 是 dict（含 ``status``）
        才对；不是 dict 的条目按「看不懂」处理，不进任何桶。
    :param retired: ``{vp_id: "obsolete" | "superseded_by"}``，即「计划自己
        作废了哪些 VP」。``None`` 表示没有 VP 被作废。
    :return: ``{"completed_vps": [...], "failed_vps": [...],
        "skipped_vps": [...], "deferred_vps": [...]}``，四个键恒定存在。

    两条与调用方有关的约定：

      * **不改入参** —— 裁决表和 ``retired`` 都是只读；
      * **顺序保留** —— 桶内顺序跟裁决表的写入顺序一致，仪表盘的失败 / 跳过
        计数才能跟逐条裁决表逐行对齐。
    """
    buckets: Dict[str, List[str]] = {
        BUCKET_COMPLETED: [],
        BUCKET_FAILED: [],
        BUCKET_SKIPPED: [],
        BUCKET_DEFERRED: [],
    }
    if not verdicts:
        return buckets

    for vp_id, verdict in verdicts.items():
        status = verdict.get("status") if isinstance(verdict, dict) else None
        bucket = bucket_for_status(status, vp_id=vp_id, retired=retired)
        if bucket is None:
            continue
        buckets[bucket].append(vp_id)

    return buckets


__all__ = [
    "BUCKET_COMPLETED",
    "BUCKET_FAILED",
    "BUCKET_SKIPPED",
    "BUCKET_DEFERRED",
    "RETIRED_OBSOLETE",
    "RETIRED_SUPERSEDED_BY",
    "bucket_for_status",
    "derive_index_buckets",
]