"""验证轮的**消费语义由来源决定**：REVIEWED 只读加载，MACHINE_ONLY 缓存复用，NONE 走 LLM。

背景
----
验证轮消费哪一份计划，由**磁盘状态**唯一决定（``plan_artifacts.resolve_plan_source()``）：

* ``REVIEWED`` —— 目录里有已审阅的验证计划书（``verification-plan.md``）。计划书是
  **真源**，机器计划（``verification_plan.json``）是它的派生物。这一条路上验证回路
  对计划的职责只有「读出来并校验」：不调方法归一化、不按 ``tasks.json`` 重算
  ``covers_paths``、不做增量评估，除首次翻译外**不写盘**。
* ``MACHINE_ONLY`` —— 没有计划书、只有机器计划。沿用既有的磁盘复用语义（方法归一化
  + 范围派生 + 增量评估）。
* ``NONE`` —— 两者都没有。走既有的 LLM Phase-1 生成路径。

三者**互斥**，且由磁盘状态唯一决定。

为什么要钉住这件事
------------------
分派的第一条件曾经是 ``is_verification_branch()`` —— 它只认「目录里**只有**计划书」
（``plan_artifacts.detect()`` 的分支判定）。于是**共存目录**（``tasks.json`` +
``verification-plan.md``）落到开发复用路径上：

* 方法归一化把计划书里人写的验证方法（"单元测试"）判成退休方法 → 每一条验证点都被
  标成 ``obsolete``，验证回路于是"跑完了"，一条也没验；
* ``covers_paths`` 按任务清单重算 → 审核过的执行范围被改写；
* 增量评估把计划**原子改写**回盘上的 ``verification_plan.json`` → 审核过的东西在
  这一趟被换掉，而没有任何一步会告诉操作者。

三个判据（来源判定、只读加载、不写盘）必须一起成立才算修好，所以本文件观测的是
**副作用本身**，双向断言：该发生的一次都没少（REVIEWED：零模型调用、零写盘），
不该发生的一次都没多（REVIEWED：零方法归一化、零范围重算、零增量评估）。

测试数据
--------
全部在 tmp 目录里：一个 plan 目录 + 机器计划 JSON + 任务清单 + 一个脚本化 LLM
（记录调用次数）。第二轮那条用例额外用 canonical 计划书与它派生的机器计划
（``tests.fixtures.verification_mode``），因为它要真的跑一遍等价性门禁。
不碰任何 provider，不联网。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
import verification_agent  # noqa: E402
from plan_artifacts import (  # noqa: E402
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    TASKS_FILENAME,
    PlanSource,
    resolve_plan_source,
)
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    MACHINE_PLAN,
)
from verification_agent import (  # noqa: E402
    VerificationAgent,
    VerificationBranchPlanError,
)

#: 增量评估的状态文件（``verification_plan_delta`` 的私有常量，抄一份便于断言）。
DELTA_STATE_FILENAME = "verification_plan_delta_state.json"


# ---------------------------------------------------------------------------
# 脚本化 LLM
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """脚本化 LLM：记录每次调用，回一份固定计划。

    **调用次数是这个文件的核心断言。** 「已审阅的计划不许碰模型」这句话只有靠
    「模型一次都没被叫过」才测得出来 —— 只看返回值的话，一份碰巧长得一样的模型
    输出会把这件事盖过去。

    用在**本来就该问模型**的那两条路上（MACHINE_ONLY / NONE）。
    """

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._payload = payload if payload is not None else {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "模型生成的验证点",
                    "related_prd_criteria": "C1: 与审核无关",
                    "verification_method": "code_review",
                    "priority": "medium",
                    "expected_result": "模型说了算",
                },
            ],
        }

    def query_json(
        self,
        prompt: str = "",
        system_instruction: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            **kwargs,
        })
        # 深拷贝：调用方会就地改这份计划，返回同一个 dict 会让第二次调用看到
        # 第一次的修改。
        return json.loads(json.dumps(self._payload, ensure_ascii=False))


class _ModelCallForbidden(BaseException):
    """「模型被叫了」这件事的信号。

    刻意继承 :class:`BaseException` 而不是 :class:`Exception`：生成路径的
    ``except Exception`` 会把任何普通异常当成「这次生成失败」再试一次，于是
    「模型被叫过」这件事被稀释成一句日志、一份最小计划 —— 用例还是红的，但
    红在别处，看不出是哪一条契约破了。继承 :class:`BaseException` 时它一路
    穿出去，用例当场失败在调用点上。
    """


class _ExplodingTool:
    """「调用即失败」的替身：任何一次模型调用都让用例当场红掉。

    这是**负控**：已审阅的那条路上模型一次都不许被叫。只断言「返回值长得
    对」测不出这件事 —— 一份碰巧长得一样的模型输出会把「模型被叫过」盖过去，
    所以替身自己把这条路堵死：一旦有调用，用例失败，而不是悄悄通过。
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def query_json(
        self,
        prompt: str = "",
        system_instruction: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            **kwargs,
        })
        raise _ModelCallForbidden("已审阅的计划不许问模型")


# ---------------------------------------------------------------------------
# 盘上的产物
# ---------------------------------------------------------------------------

#: 盘上那份机器计划：``/start`` 管线从计划书翻译出来、且过了等价门禁的那一份。
#:
#: 它的验证方法是**计划书里人写的自由文本**（"单元测试"）—— 与框架的方法词表
#: （``api_test`` / ``code_review`` / …）是两套词汇。磁盘复用路径会跑一趟方法归一化，
#: 把这类方法判成退休方法；已审阅的那一份不许被这样处理。
#:
#: 它**自带** ``covers_paths``（``src/``）：翻译产物里的执行范围是审核过的一部分，
#: 与任务清单派生出来的 ``backend/`` 不是一回事 —— 两者不同，重算这件事才看得见。
#:
#: 第三条验证点**不声明**范围：磁盘复用路径会给它按任务清单派生一个
#: （``backend/``），而已审阅的那条路一个字都不许动它（连这个键都不该有）。
DISK_PLAN: Dict[str, Any] = {
    "services": [],
    "verification_points": [
        {
            "id": "VP-001",
            "title": "配额按真实重开时刻睡眠",
            "verification_method": "单元测试",
            "related_prd_criteria": "C1: 配额耗尽时睡到真实重开时刻",
            "expected_result": "E1 | 两条判据全部满足，无超时",
            "source_task_ids": ["T-1"],
            "covers_paths": ["src/"],
        },
        {
            "id": "VP-002",
            "title": "报告把 skip 条目标注为未验证",
            "verification_method": "以手工核验为主",
            "related_prd_criteria": "C1: skip 条目显示为「未验证」",
            "expected_result": "E1 | 渲染结果与期望一致",
            "source_task_ids": ["T-1"],
            "covers_paths": ["src/"],
        },
        {
            "id": "VP-003",
            "title": "没有声明范围的验证点",
            "verification_method": "code_review",
            "related_prd_criteria": "C1: 没有声明范围",
            "expected_result": "E1 | 人工核验通过",
            "source_task_ids": ["T-1"],
        },
    ],
}

#: 开发分支的任务清单：``files_to_modify`` 派生出**另一份**范围（``backend/``）。
TASKS: Dict[str, Any] = {
    "tasks": [
        {
            "id": "T-1",
            "title": "实现配额重开时刻",
            "files_to_modify": ["backend/feature.py"],
        },
    ],
}


def _write_machine_plan(plan_dir: Path, plan: Dict[str, Any]) -> Path:
    path = plan_dir / MACHINE_PLAN_FILENAME
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _write_tasks(plan_dir: Path) -> Path:
    path = plan_dir / TASKS_FILENAME
    path.write_text(json.dumps(TASKS, ensure_ascii=False), encoding="utf-8")
    return path


def _write_plan_doc(plan_dir: Path, doc: str = "# 验证计划书\n") -> Path:
    path = plan_dir / PLAN_DOC_FILENAME
    path.write_text(doc, encoding="utf-8")
    return path


def _digest(path: Path) -> str:
    """文件的 sha256 —— 「盘上那份没被动过」的判据。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mtime_ns(path: Path) -> int:
    """文件的修改时间（纳秒）—— 「这一趟没写盘」的判据。"""
    return os.stat(path).st_mtime_ns


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def coexist_plan_dir(tmp_path: Path) -> Path:
    """共存目录：任务清单 + 验证计划书 + 机器计划。

    这是**来源判定为 REVIEWED**、而**分支判定为 dev** 的那种目录 —— 两种判定
    正交，消费语义必须听**来源**的。
    """
    plan_dir = tmp_path / "coexist-plan"
    plan_dir.mkdir()
    _write_tasks(plan_dir)
    _write_plan_doc(plan_dir)
    _write_machine_plan(plan_dir, DISK_PLAN)
    return plan_dir


@pytest.fixture
def machine_only_plan_dir(tmp_path: Path) -> Path:
    """只有机器计划、没有计划书：来源判定为 MACHINE_ONLY。"""
    plan_dir = tmp_path / "machine-only-plan"
    plan_dir.mkdir()
    _write_tasks(plan_dir)
    _write_machine_plan(plan_dir, DISK_PLAN)
    return plan_dir


@pytest.fixture
def no_plan_dir(tmp_path: Path) -> Path:
    """既没有计划书、也没有机器计划：来源判定为 NONE。"""
    plan_dir = tmp_path / "no-plan"
    plan_dir.mkdir()
    _write_tasks(plan_dir)
    return plan_dir


class _Probes:
    """消费语义的可计数探针。

    Attributes:
        calls: 事件名的时间线（``loader`` / ``normalize`` / ``covers`` /
            ``delta``）。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def count(self, name: str) -> int:
        """某个事件发生了几次。"""
        return self.calls.count(name)


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> _Probes:
    """把消费语义钉成**可计数**的探针。

    每个探针**仍然调用原函数** —— 探针只负责计数，不负责把行为改掉。
    """
    recorder = _Probes()

    original_covers = verification_agent._attach_covers_paths

    def _covers(plan: Any, task_rows: Any, **kwargs: Any) -> Any:
        recorder.calls.append("covers")
        return original_covers(plan, task_rows, **kwargs)

    monkeypatch.setattr(verification_agent, "_attach_covers_paths", _covers)

    return recorder


@pytest.fixture
def translation_guard(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """翻译器被问了几次 —— 负控：**问一次就让用例红掉**。

    机器计划已在盘上时，``_prepare_machine_plan()`` 走的是「读盘上那一份」那条
    路，一次模型都不该问。它是通过 ``server.create_coding_tool`` 拿工具的，
    所以这里把它换成「调用即失败」的替身：一旦发生重译，用例失败，而不是
    悄悄通过。

    注意这与 agent 那一侧不是同一个口子：agent 用的是 ``self.coding_tool``，
    由 :class:`_ExplodingTool` 堵住。两个口子各堵各的。
    """
    calls: List[str] = []

    def _tool(*args: Any, **kwargs: Any) -> Any:
        calls.append("translate")
        raise AssertionError("机器计划已在盘上，不该再问模型")

    monkeypatch.setattr(server, "create_coding_tool", _tool)
    return calls


def _agent(
    plan_dir: Path,
    tool: Any,
    probes: _Probes,
) -> VerificationAgent:
    """造一个 agent：模型替身 + 一个空的项目目录 + 装好探针。

    ``tool`` 是 :class:`_ExplodingTool`（调用即失败）还是 :class:`_ScriptedTool`
    （记录次数）由用例自己选 —— 负控要用前者，否则「模型一次都没被叫过」这件事
    就没有被真的堵住。
    """
    project_dir = plan_dir.parent / f"{plan_dir.name}-project"
    project_dir.mkdir(exist_ok=True)
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=tool,
    )

    original_normalize = agent._normalize_legacy_methods
    original_delta = agent._maybe_evaluate_plan_delta
    original_loader = agent._load_verification_branch_plan

    def _normalize(plan_data: dict) -> dict:
        probes.calls.append("normalize")
        return original_normalize(plan_data)

    def _delta(plan_data: dict, expected_digest: Any = None) -> dict:
        probes.calls.append("delta")
        return original_delta(plan_data, expected_digest=expected_digest)

    def _loader() -> dict:
        probes.calls.append("loader")
        return original_loader()

    agent._normalize_legacy_methods = _normalize  # type: ignore[method-assign]
    agent._maybe_evaluate_plan_delta = _delta  # type: ignore[method-assign]
    agent._load_verification_branch_plan = _loader  # type: ignore[method-assign]

    return agent


# ---------------------------------------------------------------------------
# 1. 来源判定：三种来源互斥，由磁盘状态唯一决定
# ---------------------------------------------------------------------------


def test_three_sources_are_mutually_exclusive(
    coexist_plan_dir: Path,
    machine_only_plan_dir: Path,
    no_plan_dir: Path,
) -> None:
    """共存目录是 REVIEWED（不是 MACHINE_ONLY，也不是 NONE）。

    这是整份文件的**前提**：判定只认「计划书在不在」，与「任务清单在不在」无关。
    判定错了，后面几条断言全都无从谈起。
    """
    assert resolve_plan_source(coexist_plan_dir) is PlanSource.REVIEWED
    assert resolve_plan_source(machine_only_plan_dir) is PlanSource.MACHINE_ONLY
    assert resolve_plan_source(no_plan_dir) is PlanSource.NONE


# ---------------------------------------------------------------------------
# 2. REVIEWED：只读加载语义
# ---------------------------------------------------------------------------


def test_reviewed_plan_skips_method_normalisation(
    coexist_plan_dir: Path,
    probes: _Probes,
) -> None:
    """REVIEWED：方法归一化一次都不发生，``obsolete`` 零次。

    归一化那一趟是为「模型生成的计划」准备的：模型用的是框架的方法词表，词表外的
    方法说明它是早就退休的方法。而已审阅的机器计划里，方法字段是**计划书里人写的
    自由文本**（"单元测试"）—— 拿退休检查去处理它，会把审核过的每一条验证点都标成
    ``obsolete``，验证回路于是"跑完了"，一条也没验。

    规格要求的是「**三个函数 0 次**」—— 方法归一化、范围重算、增量评估，
    在已审阅的那条路上一个都不许发生。三条断言一起钉：只断言归一化的话，
    「范围被按任务清单重算」与「增量把计划改写回盘上」这两件事仍然能溜过去
    （下面两条用例各自钉住它们，这里钉住的是「一次都不许发生」这个整体）。
    """
    tool = _ExplodingTool()
    agent = _agent(coexist_plan_dir, tool, probes)

    plan = agent.generate_verification_plan()

    assert probes.count("normalize") == 0, probes.calls
    assert probes.count("covers") == 0, probes.calls
    assert probes.count("delta") == 0, probes.calls
    assert tool.calls == [], "已审阅的计划不许问模型"

    for vp in plan["verification_points"]:
        assert "obsolete" not in vp, vp
        assert "obsolete_reason" not in vp, vp
    assert plan["verification_points"][0]["verification_method"] == "单元测试"

    # 产物里也不许出现 ``obsolete``：审核过的机器计划原样留着。
    raw = (coexist_plan_dir / MACHINE_PLAN_FILENAME).read_text(encoding="utf-8")
    assert "obsolete" not in raw


def test_reviewed_plan_skips_covers_paths_recompute(
    coexist_plan_dir: Path,
    probes: _Probes,
) -> None:
    """REVIEWED：``covers_paths`` 不按 ``tasks.json`` 重算，与翻译结果逐字一致。

    盘上那两条自带 ``covers_paths: ["src/"]``（审核过的执行范围）；任务清单派生出来
    的是 ``["backend/"]``。两者不同，所以「有没有被重算」看得见 —— 消费到的那份
    必须与盘上那份**逐字一致**（连尾斜杠都在）。

    第三条**没有声明**范围：磁盘复用路径会按任务清单给它派生一个，已审阅的那条
    路连这个键都不许有。
    """
    tool = _ExplodingTool()
    agent = _agent(coexist_plan_dir, tool, probes)

    plan = agent.generate_verification_plan()

    assert probes.count("covers") == 0, probes.calls
    assert plan["verification_points"][0]["covers_paths"] == ["src/"]
    assert plan["verification_points"][1]["covers_paths"] == ["src/"]
    assert "covers_paths" not in plan["verification_points"][2]

    # 盘上那份逐字未变。
    on_disk = json.loads(
        (coexist_plan_dir / MACHINE_PLAN_FILENAME).read_text(encoding="utf-8")
    )
    assert [vp["covers_paths"] for vp in on_disk["verification_points"][:2]] == [
        ["src/"],
        ["src/"],
    ]
    assert "covers_paths" not in on_disk["verification_points"][2]


def test_reviewed_plan_skips_delta_and_disk_write(
    coexist_plan_dir: Path,
    probes: _Probes,
) -> None:
    """REVIEWED：增量评估零次，盘上那份的 sha256 与 mtime 都不变。

    增量评估会把计划**原子改写**回盘上的 ``verification_plan.json`` —— 审核过
    的东西在这一趟被换掉，而没有任何一步会告诉操作者。增量条目还是「这轮多了
    什么任务」的产物，而任务清单是开发分支的产物，已审阅的那份凭空加出来的验证点
    不在审核过的计划书里。
    """
    machine_plan_path = coexist_plan_dir / MACHINE_PLAN_FILENAME
    digest_before = _digest(machine_plan_path)
    mtime_before = _mtime_ns(machine_plan_path)

    tool = _ExplodingTool()
    agent = _agent(coexist_plan_dir, tool, probes)

    agent.generate_verification_plan()

    assert probes.count("delta") == 0, probes.calls
    assert _digest(machine_plan_path) == digest_before, "盘上的机器计划被改写了"
    assert _mtime_ns(machine_plan_path) == mtime_before, "这一趟写了盘"
    assert not (coexist_plan_dir / DELTA_STATE_FILENAME).exists(), (
        "增量评估留下了状态文件"
    )


def test_reviewed_plan_never_falls_back_to_llm(
    coexist_plan_dir: Path,
    probes: _Probes,
) -> None:
    """REVIEWED 但机器计划不在盘上 → 抛错，不退回 LLM，也不就地生成一份。

    退回 LLM 等于悄悄换掉审核过的验证点集合；就地生成一份等于把一份没过等价
    门禁的机器计划写进盘上。两条都不许。
    """
    (coexist_plan_dir / MACHINE_PLAN_FILENAME).unlink()

    tool = _ExplodingTool()
    agent = _agent(coexist_plan_dir, tool, probes)

    with pytest.raises(VerificationBranchPlanError):
        agent.generate_verification_plan()

    assert tool.calls == [], "校验失败不许退回 LLM 重新生成"
    assert not (coexist_plan_dir / MACHINE_PLAN_FILENAME).exists(), (
        "不许就地生成一份机器计划"
    )


def test_reviewed_plan_with_unreadable_json_never_falls_back_to_llm(
    coexist_plan_dir: Path,
    probes: _Probes,
) -> None:
    """REVIEWED 且机器计划读不出来 → 抛错，不退回 LLM 重新生成一份。

    开发分支的磁盘复用路径遇到坏 JSON 会打一句「读不出来，重新生成」然后
    问模型 —— 那条路上换掉一份坏计划是安全的（没人审过它）。已审阅的那份不一样：
    坏掉的机器计划是审核过的东西的**派生物**，就地重新生成一份等于拿一批没人
    审过的验证点顶替它。
    """
    (coexist_plan_dir / MACHINE_PLAN_FILENAME).write_text(
        "{ this is not json",
        encoding="utf-8",
    )

    tool = _ExplodingTool()
    agent = _agent(coexist_plan_dir, tool, probes)

    with pytest.raises(VerificationBranchPlanError):
        agent.generate_verification_plan()

    assert tool.calls == [], "机器计划坏了不许退回 LLM 重新生成"
    assert (coexist_plan_dir / MACHINE_PLAN_FILENAME).read_text(
        encoding="utf-8"
    ) == "{ this is not json", "坏掉的那份不许被改写"


# ---------------------------------------------------------------------------
# 3. MACHINE_ONLY：缓存复用语义原样保留
# ---------------------------------------------------------------------------


def test_machine_only_keeps_legacy_cache_semantics(
    machine_only_plan_dir: Path,
    probes: _Probes,
) -> None:
    """MACHINE_ONLY：磁盘复用路径原样保留（归一化 + 范围派生 + 增量评估）。

    「保留」是双向的：既不退回 LLM 重新生成，也**不**因为分流改动而丢掉既有
    的复用语义。
    """
    tool = _ScriptedTool()
    agent = _agent(machine_only_plan_dir, tool, probes)

    plan = agent.generate_verification_plan()

    assert probes.count("loader") == 0, probes.calls
    assert probes.count("normalize") == 1, probes.calls
    assert probes.count("covers") == 1, probes.calls
    assert probes.count("delta") == 1, probes.calls
    assert tool.calls == [], "复用路径不重新问模型"

    # 范围那一趟真的跑了：作者自己声明的那两条被**归一化**（尾斜杠去掉），没
    # 声明的那条按任务清单**派生**出 ``backend/``。
    assert plan["verification_points"][0]["covers_paths"] == ["src"]
    assert plan["verification_points"][2]["covers_paths"] == ["backend/"]


# ---------------------------------------------------------------------------
# 4. NONE：LLM Phase-1
# ---------------------------------------------------------------------------


def test_none_keeps_llm_phase1_semantics(
    no_plan_dir: Path,
    probes: _Probes,
) -> None:
    """NONE：走既有的 LLM Phase-1 生成路径，问一次模型。"""
    tool = _ScriptedTool()
    agent = _agent(no_plan_dir, tool, probes)

    plan = agent.generate_verification_plan()

    assert len(tool.calls) == 1, (
        f"没有计划时应当问一次模型，实际调用 {len(tool.calls)} 次"
    )
    assert probes.count("loader") == 0, probes.calls
    assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001"]
    assert (no_plan_dir / MACHINE_PLAN_FILENAME).exists()


# ---------------------------------------------------------------------------
# 5. 连续两轮：第二轮不重译、不写盘、等价性门禁仍通过
# ---------------------------------------------------------------------------


def test_second_verification_round_reuses_json_without_recompute(
    tmp_path: Path,
    probes: _Probes,
    translation_guard: List[str],
) -> None:
    """第二轮：不重译、不写盘、等价性门禁仍然通过。

    这一条把「只读」钉在**跨轮**的尺度上：第一轮之后盘上那份是审核过的产物，
    第二轮读到它就该原样消费 —— 不重译（翻译器一次都不许被问）、不写盘
    （sha256 与 mtime 不变）、等价性门禁仍然通过（机器计划与当前计划书逐条
    相等，门禁的返回值是「放行」）。
    """
    from routes.verification import _prepare_machine_plan

    plan_dir = tmp_path / "two-rounds-plan"
    plan_dir.mkdir()
    _write_tasks(plan_dir)
    _write_plan_doc(plan_dir, CANONICAL_PLAN_DOC)
    machine_plan_path = _write_machine_plan(plan_dir, MACHINE_PLAN)

    digest_before = _digest(machine_plan_path)
    mtime_before = _mtime_ns(machine_plan_path)

    tool = _ExplodingTool()
    agent = _agent(plan_dir, tool, probes)

    for _round in (1, 2):
        # 等价性门禁：机器计划与当前计划书逐条相等 → 放行。
        failure = _prepare_machine_plan("two-rounds-plan", plan_dir)
        assert failure is None, f"第 {_round} 轮的等价性门禁没过：{failure}"

        plan = agent.generate_verification_plan()

        assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001"]

    # 不重译：机器计划已在盘上，翻译器一次都不许被问。
    assert translation_guard == [], "第二轮重译了机器计划"
    assert tool.calls == [], "第二轮问了模型"

    # 不写盘。
    assert _digest(machine_plan_path) == digest_before, "机器计划被改写了"
    assert _mtime_ns(machine_plan_path) == mtime_before, "这一趟写了盘"
    assert probes.count("loader") == 2, probes.calls
    assert probes.count("normalize") == 0, probes.calls
    assert probes.count("covers") == 0, probes.calls
    assert probes.count("delta") == 0, probes.calls