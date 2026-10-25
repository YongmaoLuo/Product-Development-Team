"""复用路径上的**增量落盘**（读-改-写闭环）。

背景
----
``verification_agent._maybe_evaluate_plan_delta`` 以前在**复用路径**上跑完就
返回：增量只在内存里生效，磁盘上的 ``verification_plan.json`` 一个字节都没动。
唯一写盘点在"首次生成"那条路径上（``generate_verification_plan`` 的生成分支）。

后果是"作废"下一轮必然消失：判据被标记作废、替换 VP 被加进来，但两样都只
活在那一轮的内存里。下一轮从磁盘读回来的还是旧计划 —— 判据复活，替换 VP
消失，修复任务照旧围绕一条已经不成立的判据生成。

本模块钉住的是**复用路径**上的读-改-写闭环：

* 增量必须**落盘**（``apply_delta_persisted``）—— 换一个新实例重读磁盘，
  作废标记与替换指针都还在；
* 写失败必须**不落盘也不带标记**：磁盘一个字节不变，内存里没有作废标记，
  验证继续用现有计划（增量评估是增益，不是门禁）；
* 失败必须**留痕**：``plan_delta_persist_failed`` 事件，而不是一条
  ``plan_delta_evaluated``（那等于谎报"评估成功且已生效"）。

负控
----
``test_bare_apply_delta_regresses`` 直接调裸 ``apply_delta``（只改内存、不
落盘），钉住"不落盘就一定复活"这件事。没有它，"落盘了"就只是"内存里看见
了"：哪天有人把入口换回裸 ``apply_delta``，正控会一起变红，而这一条会先指出
原因。

术语对照
--------
任务书里的 ``VP-A`` / ``VP-B`` 在本模块里是 ``VP-001`` / ``VP-002`` ——
框架的 VP id 约定是 ``VP-<三位数字>``（见 ``verification_plan_delta
.next_vp_id``），断言按这个约定写，语义一一对应：``VP-001`` 是被判据缺陷而
作废的那条（旧 ``VP-A``），``VP-002`` 是接替它的那条（新 ``VP-B``）。

测试命令::

    cd backend && .venv/bin/python3 -m pytest \
        tests/integration/test_plan_delta_agent_reuse.py -q
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Make ``verification_agent`` / ``verification_plan_delta`` importable when
# pytest is invoked from the project root (``backend/`` is the package root
# for these modules).
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vp(vp_id: str, **extra: Any) -> Dict[str, Any]:
    """一条最小可用的 VP。

    方法用 ``code_review``：它在 ``SUPPORTED_METHODS`` 里，但不像 ``api_test``
    那样要过 request/assertions 的 schema —— 于是本模块测的是"增量能不能
    落盘"，而不是"增量会不会被 api schema 门禁挑掉"。
    """
    vp: Dict[str, Any] = {
        "id": vp_id,
        "title": f"验证点 {vp_id}",
        "verification_method": "code_review",
        "related_prd_criteria": "验收标准 1",
        "expected_result": "审查通过",
        "verification_phase": 1,
    }
    vp.update(extra)
    return vp


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan_file(plan_dir: Path) -> Path:
    return plan_dir / "verification_plan.json"


def _write_plan(plan_dir: Path, plan: Dict[str, Any]) -> str:
    path = _plan_file(plan_dir)
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return _sha256(path)


def _read_plan(plan_dir: Path) -> Dict[str, Any]:
    return json.loads(_plan_file(plan_dir).read_text(encoding="utf-8"))


def _by_id(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(vp.get("id")): vp
        for vp in plan.get("verification_points") or []
        if isinstance(vp, dict)
    }


def _write_tasks(plan_dir: Path, tasks: List[Dict[str, Any]]) -> None:
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_prd(plan_dir: Path) -> None:
    (plan_dir / "prd.json").write_text(
        json.dumps({"acceptance": ["验收标准 1"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _replacement_payload(replacement_for: str = "VP-001") -> Dict[str, Any]:
    """一次"作废旧判据 + 新增一条替换判据"的增量评估结果。"""
    return {
        "add": [{
            "title": "替换判据：审查新接口的鉴权分支",
            "verification_method": "code_review",
            "related_prd_criteria": "验收标准 1",
            "expected_result": "审查通过",
            "reason": f"替换 {replacement_for}：原判据与实现不一致",
            "replacement_for": replacement_for,
        }],
        "obsolete": [{
            "id": replacement_for,
            "reason": "判据缺陷：断言与实现不一致",
        }],
    }


def _empty_payload() -> Dict[str, Any]:
    return {"add": [], "obsolete": []}


class _FakeCodingTool:
    """只回放增量评估的 JSON；不做任何真实调用。"""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def query_json(self, prompt=None, system_instruction=None, scene=None, **kwargs):
        self.calls.append({"scene": scene, "prompt": prompt})
        return json.loads(json.dumps(self.payload))


class _RewritingCodingTool(_FakeCodingTool):
    """在增量评估期间把计划文件换成另一份内容（模拟外部写者）。

    改写发生在 LLM 调用那一步 —— 也就是"读入计划"与"写回计划"之间，
    正好是写前摘要要守住的那个窗口。
    """

    def __init__(
        self,
        payload: Dict[str, Any],
        plan_file: Path,
        external_plan: Dict[str, Any],
    ) -> None:
        super().__init__(payload)
        self.plan_file = plan_file
        self.external_plan = external_plan

    def query_json(self, prompt=None, system_instruction=None, scene=None, **kwargs):
        result = super().query_json(
            prompt=prompt, system_instruction=system_instruction,
            scene=scene, **kwargs,
        )
        self.plan_file.write_text(
            json.dumps(self.external_plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result


def _agent(
    plan_dir: Path,
    project_dir: Path,
    tool: _FakeCodingTool,
    round_number: int,
):
    """造一个"新实例"的 agent（复用路径每轮都是新实例）。"""
    from verification_agent import VerificationAgent

    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=tool,
    )
    agent._current_round = round_number
    # 打开本轮的审计日志 —— ``plan_delta_persist_failed`` 事件要能在
    # 真实的日志文件里被翻到，而不是只在内存里。
    agent.persistence.start_round(round_number)
    return agent


def _events(plan_dir: Path, event_type: str) -> List[Dict[str, Any]]:
    """本计划目录的轮次日志里，某一类事件的全部条目。"""
    found: List[Dict[str, Any]] = []
    logs_dir = plan_dir / "logs"
    if not logs_dir.is_dir():
        return found
    for log_file in sorted(logs_dir.glob("*.log")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event_type") == event_type:
                found.append(entry)
    return found


def _seed_plan_dir(tmp_path: Path) -> tuple[Path, Path]:
    """铺一个"第 1 轮已有计划"的环境，返回 ``(plan_dir, project_dir)``。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    _write_plan(plan_dir, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001", title="老判据")],
    })
    _write_tasks(plan_dir, [{"id": "T-1", "title": "实现功能"}])
    _write_prd(plan_dir)
    return plan_dir, project_dir


def _run_first_round(plan_dir: Path, project_dir: Path) -> _FakeCodingTool:
    """第 1 轮：只记基线（没有"新增任务"，不调 LLM，也不写盘）。"""
    tool = _FakeCodingTool(_empty_payload())
    agent = _agent(plan_dir, project_dir, tool, 1)
    agent.generate_verification_plan()
    return tool


# ---------------------------------------------------------------------------
# 正控：增量落盘，换新实例仍然作废
# ---------------------------------------------------------------------------


def test_second_round_sees_obsolete(tmp_path: Path) -> None:
    """第 2 轮落盘的作废标记，换一个新实例重读磁盘依然在。

    第 1 轮建立基线（``verification_plan_delta_state.json`` 记下"看过哪些
    任务"），第 2 轮来了一个新增任务（修复轮追加的任务），于是增量评估真的
    跑了一次：作废 ``VP-001``、新增接替它的 ``VP-002``。

    断言分两层：

    * **本轮内存**：第 2 轮返回的计划里，``VP-001`` 已作废、``VP-002`` 记着
      ``replacement_for = "VP-001"``；
    * **磁盘 + 下一个新实例**：第 3 轮换一个全新的 agent 从磁盘读回来，
      ``VP-001`` 依然作废 —— 这才是"作废下一轮不会复活"。

    少了落盘，第二层必然失败：磁盘上还是最初那份计划，``VP-001`` 会活过来。
    """
    plan_dir, project_dir = _seed_plan_dir(tmp_path)

    # 第 1 轮：基线。没有新增任务 → 不调 LLM。
    first_tool = _run_first_round(plan_dir, project_dir)
    assert first_tool.calls == []

    # 修复轮追加了一个任务 → 第 2 轮有"新增任务"，增量评估真的跑。
    _write_tasks(plan_dir, [
        {"id": "T-1", "title": "实现功能"},
        {"id": "repair-r1", "title": "修复鉴权分支"},
    ])

    second_tool = _FakeCodingTool(_replacement_payload("VP-001"))
    second_agent = _agent(plan_dir, project_dir, second_tool, 2)
    second_plan = second_agent.generate_verification_plan()

    # 本轮内存：作废与替换都生效了。
    second_by_id = _by_id(second_plan)
    assert second_by_id["VP-001"]["obsolete"] is True
    assert second_by_id["VP-001"]["obsolete_reason"] == "判据缺陷：断言与实现不一致"
    assert second_by_id["VP-002"]["replacement_for"] == "VP-001"

    # 磁盘：这一轮真的写了盘。
    on_disk = _by_id(_read_plan(plan_dir))
    assert on_disk["VP-001"]["obsolete"] is True
    assert on_disk["VP-001"]["obsoleted_round"] == 2
    assert on_disk["VP-002"]["replacement_for"] == "VP-001"

    # 第 3 轮：换一个全新实例从磁盘读 —— 作废没有复活。
    third_tool = _FakeCodingTool(_empty_payload())
    third_agent = _agent(plan_dir, project_dir, third_tool, 3)
    third_plan = third_agent.generate_verification_plan()

    third_by_id = _by_id(third_plan)
    assert third_by_id["VP-001"]["obsolete"] is True
    assert third_by_id["VP-002"]["replacement_for"] == "VP-001"
    # 第 3 轮没有新增任务 → 没有再调 LLM：读回来的就是磁盘上那份。
    assert third_tool.calls == []


# ---------------------------------------------------------------------------
# 负控：裸 apply_delta 不落盘 → 下一轮复活
# ---------------------------------------------------------------------------


def test_bare_apply_delta_regresses(tmp_path: Path) -> None:
    """负控：裸 ``apply_delta``（只改内存、不落盘）→ 第 2 轮判据复活。

    这一条钉住"落盘"与"只在内存里生效"的差别。裸 ``apply_delta`` 是本次改动
    之前的复用路径写法：增量在内存里生效，磁盘一个字节没动。下一轮从磁盘
    读回来，判据照旧有效 —— 也就是"作废下一轮必然消失"。

    有了这一条，"落盘了"才不是"内存里看见了"：哪天有人把入口换回裸
    ``apply_delta``，正控会一起变红，而这一条会先指出原因。
    """
    from verification_plan_delta import (
        PlanDelta,
        apply_delta,
        parse_delta_payload,
    )

    plan_dir, project_dir = _seed_plan_dir(tmp_path)
    _run_first_round(plan_dir, project_dir)

    digest_before = _sha256(_plan_file(plan_dir))

    # 裸 apply_delta：和本次改动之前的复用路径一模一样 —— 只改内存。
    plan_data = _read_plan(plan_dir)
    delta = parse_delta_payload(_replacement_payload("VP-001"), plan_data)
    summary = apply_delta(plan_data, delta, 2)

    assert [o["id"] for o in summary["obsoleted"]] == ["VP-001"]
    assert _by_id(plan_data)["VP-001"]["obsolete"] is True

    # 磁盘一个字节都没变 —— 这正是"下一轮会复活"的原因。
    assert _sha256(_plan_file(plan_dir)) == digest_before

    # 下一个新实例从磁盘读回来：判据复活，替换 VP 也不存在。
    tool = _FakeCodingTool(_empty_payload())
    agent = _agent(plan_dir, project_dir, tool, 3)
    plan = agent.generate_verification_plan()

    revived = _by_id(plan)
    assert "obsolete" not in revived["VP-001"]
    assert "VP-002" not in revived


# ---------------------------------------------------------------------------
# 写失败：磁盘不变、内存不带标记、留痕
# ---------------------------------------------------------------------------


def test_persist_failure_keeps_plan(tmp_path: Path, monkeypatch) -> None:
    """写盘失败 → 磁盘不变、内存不带标记、验证继续、失败留痕。

    写失败必须让这一轮增量**整体作废**：``apply_delta_persisted`` 先在深拷贝
    上应用，落盘成功之后才让调用方手里的计划换内容，所以失败时内存里连
    作废标记都没有 —— 内存不领先磁盘。

    同时，失败必须**留痕**：``plan_delta_persist_failed`` 事件，而不是一条
    ``plan_delta_evaluated``（那等于谎报"评估成功且已生效"）。
    """
    from utils import atomic_io

    plan_dir, project_dir = _seed_plan_dir(tmp_path)
    _run_first_round(plan_dir, project_dir)

    _write_tasks(plan_dir, [
        {"id": "T-1", "title": "实现功能"},
        {"id": "repair-r1", "title": "修复鉴权分支"},
    ])
    digest_before = _sha256(_plan_file(plan_dir))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io, "atomic_write_json", boom)

    tool = _FakeCodingTool(_replacement_payload("VP-001"))
    agent = _agent(plan_dir, project_dir, tool, 2)

    # 写失败不阻断验证：增量评估是增益，不是门禁。
    plan = agent.generate_verification_plan()

    # 磁盘一个字节都没变。
    assert _sha256(_plan_file(plan_dir)) == digest_before

    # 内存里不带标记：没有作废，也没有多出来的替换 VP。
    by_id = _by_id(plan)
    assert "obsolete" not in by_id["VP-001"]
    assert set(by_id) == {"VP-001"}

    # 失败留痕：一条 plan_delta_persist_failed，没有 plan_delta_evaluated。
    failed = _events(plan_dir, "plan_delta_persist_failed")
    assert len(failed) == 1
    assert "PlanDeltaPersistError" in failed[0]["data"]["error"]
    assert _events(plan_dir, "plan_delta_evaluated") == []


# ---------------------------------------------------------------------------
# 写前摘要复核：计划被外部改写 → 这一轮增量不落盘
# ---------------------------------------------------------------------------


def test_digest_guard_rejects_external_rewrite(tmp_path: Path) -> None:
    """写前摘要复核：计划在评估期间被外部改写 → 这一轮增量不落盘。

    摘要在**读入计划时**记下（sha256），写回前再算一次：两次不一致说明
    计划在增量评估期间被别人改过，写回去会覆盖掉别人的改动 —— 宁可放弃
    这一轮增量。

    这里的"外部改写"由假的 coding tool 代劳：它在 LLM 调用那一步把计划
    文件换成了另一份内容，正好落在"读入"与"写回"之间的那个窗口里。

    没有摘要复核（``expected_digest`` 没透传下去）时，这次增量会**盖掉**
    外部改动 —— 所以这一条钉住的是摘要真的被用上了，而不只是"算了个摘要"。
    """
    plan_dir, project_dir = _seed_plan_dir(tmp_path)
    _run_first_round(plan_dir, project_dir)

    _write_tasks(plan_dir, [
        {"id": "T-1", "title": "实现功能"},
        {"id": "repair-r1", "title": "修复鉴权分支"},
    ])

    external_plan = {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001", title="被外部改写")],
    }
    tool = _RewritingCodingTool(
        _replacement_payload("VP-001"), _plan_file(plan_dir), external_plan,
    )
    agent = _agent(plan_dir, project_dir, tool, 2)

    plan = agent.generate_verification_plan()

    # 外部改动没被覆盖，也没被半途截断。
    assert _read_plan(plan_dir) == external_plan

    # 这一轮增量整体作废：内存里没有作废标记，也没有多出来的替换 VP。
    by_id = _by_id(plan)
    assert "obsolete" not in by_id["VP-001"]
    assert set(by_id) == {"VP-001"}

    # 失败留痕，原因写清楚是"计划被改写"而不是"写失败"。
    failed = _events(plan_dir, "plan_delta_persist_failed")
    assert len(failed) == 1
    assert "PlanChangedDuringDelta" in failed[0]["data"]["error"]
