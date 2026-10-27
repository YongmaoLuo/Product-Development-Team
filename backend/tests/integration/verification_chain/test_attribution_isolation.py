"""验证会话的归因绑定与隔离（2026-10-27）。

背景
----
台账（``usage_registry``）早就认 ``vp_id`` / ``round`` 两个归因坐标
（任务 6），但**验证会话发起时没人绑定**：验证的每一次 LLM 往返写进
``llm_sessions.jsonl`` 的行都落进 ``(unattributed)`` 桶。计划级的
``by_scene`` 能说「验证很贵」，却说不出「**哪条**验证点、**哪一轮**贵」——
一个不能点名的桶没法优化。

这一层要钉住两件事：

* **绑定点**：归因在**分派点**绑定 —— 执行器（轮次循环）把轮次号交给
  分派，子 agent 在发起这条验证点的会话时把 ``vp_id`` 绑上；
* **隔离**：绑定是用 ContextVar 做的，而验证会话跑在**派生 worker**
  （``asyncio.to_thread``）里 —— 绑定必须跟着会话进 worker，也必须在会话
  结束后**复位**，不能漏到下一个验证点。漏一次就是一次「A 的花费记在 B
  头上」，而这样的统计比没有统计更坏。

TDD contract — 4 cases
-----------------------
1. ``test_each_row_carries_its_vp`` —— 每行的 ``vp_id`` 与**产生它的那次
   会话**所属的验证点一致。会话 id 是从**提示词**里派生出来的（提示词是
   生产路径上承载验证点号的那条通道），与 ContextVar 无关 —— 所以这是一次
   真正的交叉核对，而不是自己跟自己核对。
2. ``test_no_cross_session_pollution`` —— 跨会话污染计数为 0。
3. ``test_non_verification_scene_unaffected`` —— 非验证场景不新增键值：
   ``vp_id`` 是 ``null``，既有字段一个不少、值不变。
4. ``test_round_propagates`` —— 行里的轮次号与执行轮次一致。

边界条件
--------
* 上下文在会话结束后必须复位：下一条验证点拿不到上一条的 ``vp_id``。
* 派生 worker 必须收尾：``asyncio.run`` 会 join 默认线程池的 worker；
  ``clean_execution_state`` 看到活着的 worker 即判失败。

测试命令::

    pytest backend/tests/integration/verification_chain/test_attribution_isolation.py -v
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import usage_registry  # noqa: E402
from verification_executor import VerificationExecutor  # noqa: E402
from verification_subagent import VerificationSubAgent  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本文件里那个计划的 id —— 台账按 plan id 分目录。
PLAN_ID = "attribution-isolation"

#: 这一轮的轮次号。执行器把它交给分派，行里必须原样出现。
ROUND_NO = 2

#: 这一轮的八条验证点。
VP_IDS: List[str] = [f"VP-{index:03d}" for index in range(1, 9)]

#: 提示词里承载验证点号的那一行（``_build_prompt`` 的第一行）。
_VP_IN_PROMPT = re.compile(
    r"^Verification point:\s*(\S+)\s*$", re.MULTILINE
)

#: 会话 id 的前缀 —— 会话 id 由提示词里的验证点号派生。
_SESSION_PREFIX = "sid-"


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    """这个计划的 LLM 会话台账（``plans/<plan_id>/llm_sessions.jsonl``）。"""
    return usage_registry.repo_plans_dir() / PLAN_ID / "llm_sessions.jsonl"


def _ledger_rows() -> List[Dict[str, Any]]:
    """台账里的每一行（台账不存在 = 空台账）。"""
    path = _ledger_path()
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _vp_of_session(session_id: Optional[str]) -> Optional[str]:
    """这次会话属于哪条验证点（从会话 id 读，**不**问 ContextVar）。"""
    if not session_id or not session_id.startswith(_SESSION_PREFIX):
        return None
    return session_id[len(_SESSION_PREFIX):]


def _vp_id_from_prompt(prompt: Any) -> str:
    """从提示词里读出验证点号（生产提示词的第一行就带着它）。"""
    match = _VP_IN_PROMPT.search(str(prompt or ""))
    return match.group(1) if match else "unknown"


# ---------------------------------------------------------------------------
# 替身：一次调用 = 一次 LLM 会话
# ---------------------------------------------------------------------------


class _RecordingCodingTool:
    """一次调用记一次会话 —— 和真工具同形。

    真工具在 ``ClaudeCodingTool._run_claude_interactive`` 里记账；这个
    替身做同一件事（``usage_registry.record_llm_call``），并且**从提示词**
    里取验证点号来派生会话 id。提示词是生产路径上承载验证点号的那条通道，
    与 ContextVar 无关 —— 所以「行里的 ``vp_id``」和「会话自己的验证点」
    可以互相核对。

    真工具的调用是同步阻塞的，跑在派生 worker 里；这里照抄那份形状
    （``VerificationSubAgent`` 内部用 ``asyncio.to_thread`` 调它）。
    """

    def __init__(self, delay_by_vp: Optional[Dict[str, float]] = None) -> None:
        self._delay_by_vp = dict(delay_by_vp or {})
        #: 每次调用记下「worker 里看见的 vp_id」—— 绑定有没有跟着进 worker。
        self.seen_vp_ids: List[Optional[str]] = []

    def query_json(
        self,
        prompt: Any = None,
        system_instruction: Any = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        vp_id = _vp_id_from_prompt(prompt)
        # 让各验证点的完成顺序**反过来**（延迟随序号递减）：并发交错的
        # 最坏情形就是「后起的先完」，那正是漏绑最容易显形的时候。
        time.sleep(self._delay_by_vp.get(vp_id, 0.0))
        self.seen_vp_ids.append(usage_registry.current_vp_id())
        usage_registry.record_llm_call(
            session_id=f"{_SESSION_PREFIX}{vp_id}",
            scene="verification",
            provider="vendor-a",
            model="Vendor A-M3",
            ok=True,
            usage={"input_tokens": 11, "output_tokens": 7},
            cost_usd="0.002",
            num_turns=1,
            duration_ms=5,
        )
        return {
            "verdict": "PASSED",
            "reasons": [f"{vp_id} 的判据都查过了"],
            "evidence": [f"{vp_id}: 证据行"],
        }


# ---------------------------------------------------------------------------
# 跑一轮
# ---------------------------------------------------------------------------


def _vp(vp_id: str) -> Dict[str, Any]:
    """一条验证点（``code_review`` —— 没有确定性 runner，走 LLM 路径）。"""
    return {
        "id": vp_id,
        "title": f"{vp_id} 的标题",
        "verification_method": "code_review",
        "expected_result": f"{vp_id} 的判据",
    }


def _run_round(
    tmp_path: Path,
    tool: Optional[_RecordingCodingTool] = None,
) -> _RecordingCodingTool:
    """跑一轮八路并发的验证分派，返回那个记账工具。

    生产形状：执行器 ``run()`` → ``asyncio.gather`` → 每条验证点一个
    任务 → 子 agent → ``asyncio.to_thread`` 调工具（派生 worker）。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if tool is None:
        tool = _RecordingCodingTool(
            {
                vp_id: (len(VP_IDS) - index) * 0.005
                for index, vp_id in enumerate(VP_IDS)
            }
        )

    async def _dispatch(vp: Dict[str, Any]) -> Dict[str, Any]:
        """生产形状的一次分派：子 agent 起会话 → 回裁决。"""
        agent = VerificationSubAgent(
            method="code_review",
            max_retries=0,
            model_complexity="simple",
            scene="verification",
            plan_id=PLAN_ID,
            registry=None,
        )
        verdict = await agent.run(
            vp_node=vp,
            coding_tool=tool,
            log_dir=log_dir,
            project_dir=None,
            plan_id=PLAN_ID,
        )
        # 会话结束 → 绑定必须已经复位（下一条验证点拿不到这一条的号）。
        vp_id_after = usage_registry.current_vp_id()
        return {
            "status": verdict.verdict,
            "reasons": list(verdict.reasons),
            "evidence": {
                "vp_id": vp.get("id"),
                "vp_id_after_session": vp_id_after,
            },
        }

    executor = VerificationExecutor(
        verification_plan={"vps": [_vp(vp_id) for vp_id in VP_IDS]},
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=_dispatch,
        round_no=ROUND_NO,
    )

    # 生产形状：整轮跑在一个绑定着 plan 的上下文里（server._run_in_plan_ctx）。
    with usage_registry.plan_usage_context(plan_id=PLAN_ID):
        asyncio.run(executor.run(max_parallel=0))
    return tool


# ---------------------------------------------------------------------------
# 1. 每行带着它自己的验证点
# ---------------------------------------------------------------------------


def test_each_row_carries_its_vp(tmp_path: Path) -> None:
    """每行的 ``vp_id`` 与产生它的那次会话所属的验证点一致。

    会话 id 从**提示词**派生，与 ContextVar 无关 —— 所以这一条是真的
    交叉核对：行里的 ``vp_id`` 必须等于会话自己的验证点。
    """
    _run_round(tmp_path)
    rows = _ledger_rows()

    assert len(rows) == len(VP_IDS), rows
    for row in rows:
        session_vp = _vp_of_session(row.get("session_id"))
        assert session_vp is not None, row
        assert row.get("vp_id") == session_vp, (
            "the row's vp_id is not the VP that produced the session: "
            f"{row}"
        )
    assert {row["vp_id"] for row in rows} == set(VP_IDS), rows


# ---------------------------------------------------------------------------
# 2. 跨会话污染 = 0
# ---------------------------------------------------------------------------


def test_no_cross_session_pollution(tmp_path: Path) -> None:
    """跨会话污染计数为 0。

    污染有两种：**记到别人头上**（A 的会话写着 B 的号）和**记不成**
    （谁的号都没有，掉进 ``(unattributed)`` 桶）。两种都算。
    """
    _run_round(tmp_path)
    rows = _ledger_rows()

    pollution = sum(
        1
        for row in rows
        if row.get("vp_id") != _vp_of_session(row.get("session_id"))
    )
    assert pollution == 0, rows
    assert all(row.get("vp_id") is not None for row in rows), (
        "a verification session landed in the (unattributed) bucket: "
        f"{rows}"
    )


# ---------------------------------------------------------------------------
# 3. 非验证场景不受影响
# ---------------------------------------------------------------------------


def test_non_verification_scene_unaffected(tmp_path: Path) -> None:
    """别的 scene 不因这次绑定多出键值：``vp_id`` 是 ``null``，其余不变。

    台账的契约是「没绑定就写 ``null``，不是少一个键」（任务 6），所以
    这里断言的是**值**：非验证场景的调用拿不到任何验证点号；而
    ``plan_id`` / ``task_id`` / ``scene`` / ``usage`` 这些既有字段一个不少、
    值也不变。
    """
    with usage_registry.plan_usage_context(plan_id=PLAN_ID):
        usage_registry.record_llm_call(
            session_id="sid-scene-other",
            scene="prd_generation",
            provider="vendor-b",
            model="Vendor B-M3",
            ok=True,
            usage={"input_tokens": 3, "output_tokens": 2},
            cost_usd="0.001",
            num_turns=2,
            duration_ms=11,
        )

    rows = _ledger_rows()
    assert len(rows) == 1, rows
    row = rows[0]

    # 新坐标是空的 —— 没有验证点号的调用不许借一个。
    assert row["vp_id"] is None
    assert row["round"] is None

    # 既有字段一个不少、值不变。
    assert row["plan_id"] == PLAN_ID
    assert row["task_id"] is None
    assert row["session_id"] == "sid-scene-other"
    assert row["scene"] == "prd_generation"
    assert row["provider"] == "vendor-b"
    assert row["model"] == "Vendor B-M3"
    assert row["ok"] is True
    assert row["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert row["cost_usd"] == "0.001"
    assert row["num_turns"] == 2
    assert row["duration_ms"] == 11
    assert row["error"] is None
    assert row["ts"]


# ---------------------------------------------------------------------------
# 4. 轮次号跟着分派走
# ---------------------------------------------------------------------------


def test_round_propagates(tmp_path: Path) -> None:
    """行里的轮次号与执行轮次一致。"""
    _run_round(tmp_path)
    rows = _ledger_rows()

    assert len(rows) == len(VP_IDS), rows
    assert {row.get("round") for row in rows} == {ROUND_NO}, rows


# ---------------------------------------------------------------------------
# 边界条件：绑定不漏到下一个验证点
# ---------------------------------------------------------------------------


def test_context_is_reset_after_each_session(tmp_path: Path) -> None:
    """会话结束 → 绑定复位；worker 里看见的号就是这条验证点的号。

    两条断言，一条在**里面**（worker 看见的号 = 这条验证点的号），一条在
    **外面**（会话结束后 ContextVar 复位）。少了任何一条，漏绑都能蒙混
    过关：只在 worker 里看，测不到「漏到下一个验证点」；只在外面看，
    测不到「绑定根本没进 worker」。
    """
    tool = _run_round(tmp_path)
    rows = _ledger_rows()

    # worker 里看见的号 = 提示词里的号（一次不多一次不少）。
    assert len(tool.seen_vp_ids) == len(VP_IDS), tool.seen_vp_ids
    assert set(tool.seen_vp_ids) == set(VP_IDS), tool.seen_vp_ids

    # 会话结束后，这一条验证点的号不再留在上下文里。
    assert usage_registry.current_vp_id() is None
    assert usage_registry.current_round() is None
