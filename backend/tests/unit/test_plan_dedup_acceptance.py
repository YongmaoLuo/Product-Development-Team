"""计划去重的**端到端验收**：R1-R5 串成一条（2026-11-09, 验收任务）。

前面几轮把需求拆成五块分别落地了：

* R1 —— 验证点重复检测（命令相同 / 覆盖面+方法+标题相似）；
* R2 —— 空泛验收判据（``vague_criterion``）；
* R3 —— 接进验证计划生成（落盘前检测 + 写报告 + 开关）；
* R4 —— 接进任务生成（``tasks.json`` 落盘时写报告）；
* R5 —— 三条不得误伤的用例。

每一块都有自己的单元测试，但**单元测试证明的是"每块都对"**；操作员真正
关心的是"合起来对不对" —— 真实案例能不能被抓出来、干净的方案会不会被误
报、报告会不会每次都不一样、关掉开关会不会改动已有产物。这四件事没有
任何一块的单元测试能单独回答，所以这里用一条验收测试把它们串起来。

五个用例与验收项一一对应：

======================================  ====================================
用例                                    钉住的验收项
======================================  ====================================
``test_real_case_groups_vp022_and_...``  真实案例负控（VP-022/024/025）
``test_distinct_plan_writes_no_report``  正向：明显不同的方案零报告
``test_r5_false_positive_battery_...``   R5 三条误伤用例
``test_two_runs_produce_identical_...``  报告确定性（逐字节相同）
``test_disabled_switch_preserves_...``   开关关闭时产物逐字节不变
======================================  ====================================

验收 1 / 4 走**真实钩子**
------------------------
这两条验收项问的是"接进落盘路径之后还对不对"，所以它们**必须**驱动真实的
``VerificationAgent._persist_verification_plan``，并且断言要**读回落盘的文件**
—— 直接调报告层的 writer 只证明了"纯函数算得对"，钩子接错了（传错了计划、
写错了目录、往 ``.txt`` 里掺了时间戳、把 ``expected_result`` 抹掉）一样会绿。

验收 2（干净计划零报告）是唯一直接调 writer 的用例：它要验的恰恰是
"writer 对干净输入什么都不写"，这一层就是 writer 本身。

真实案例（负控）
----------------
需求里的原话：一个真实计划的验证计划里同时存在
三条「整仓门禁」验证点 —— ``VP-022``（Nightly CI）、``VP-024``（本地复刻
容器，``expected_result`` 只有四个字「验证通过」）、``VP-025``（GitHub
Actions）。其中 VP-022 与 VP-025 跑的是同一条命令，VP-024 是旧时代的残留。

这份数据是**真实计划里长出来的**，不是为测试编的 —— 所以它是唯一能证明
"这套规则抓的是真问题"的样本。断言照抄需求：VP-022 与 VP-025 同组，
VP-024 判 ``vague_criterion``。

为什么 VP-024 也进了重复组不算错
--------------------------------
VP-024 跑的是**同一条** ``bash scripts/ci_gate.sh``。需求只要求 VP-022 与
VP-025 同组，并没有要求 VP-024 被排除 —— 事实上 VP-024 就是那条"重复跑
一遍的残留"，把它一起抓出来才是对的。所以这里断言的是"**存在**一个组同时
含 VP-022 与 VP-025"，而不是"这个组恰好只有两条"。

开关那一格（验收第 5 条）
-------------------------
"关掉开关时产物与改动前逐字节相同"这句话有两半：报告文件不出现（前半），
以及**已有产物一个字节都不变**（后半）。后半更容易被忽略 —— 钩子插在落盘
路径上，钩子写错一个字符（多一个 ``\n``、换一个 ``indent``）都会让"关掉
开关"变成"换了种写法"。所以这里两个方向都跑一遍，比对**字节**。
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_dedup import build_verification_report  # noqa: E402
from plan_dedup_report import (  # noqa: E402
    TASKS_REPORT_JSON,
    VERIFICATION_REPORT_JSON,
    VERIFICATION_REPORT_TEXT,
    write_verification_dedup_report,
)


# ---------------------------------------------------------------------------
# 真实案例：VP-022 / VP-024 / VP-025
# ---------------------------------------------------------------------------

#: 全量 Nightly CI 跑整仓门禁。
VP_022 = {
    "id": "VP-022",
    "title": "【全量】Nightly CI：整仓门禁 + 全部测试",
    "verification_method": "full_ci",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "整仓门禁退出码 0，全部测试通过",
}

#: 本地 CI 复刻容器时代的残留 —— 判据只有四个字「验证通过」。
VP_024 = {
    "id": "VP-024",
    "title": "【本地】整仓门禁在 CI 复刻容器内全量跑绿",
    "verification_method": "full_ci",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "验证通过",
}

#: 云端 GitHub Actions 跑整仓门禁 —— 与 VP-022 同一条命令。
VP_025 = {
    "id": "VP-025",
    "title": "【云端】整仓门禁经 GitHub Actions 全量跑绿",
    "verification_method": "full_ci",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "GitHub Actions 全部 job 通过",
}

#: 真实案例的三条验证点（原样，不做删改）。
REAL_CASE_POINTS = [VP_022, VP_024, VP_025]


def _real_case_plan() -> dict:
    return {"verification_points": copy.deepcopy(REAL_CASE_POINTS)}


# ---------------------------------------------------------------------------
# 正向样本：四条明显不同的验证点
# ---------------------------------------------------------------------------

#: 四条**确实不同**的验证点 —— 命令、覆盖面、方法、标题各不相同，判据也都是
#: 描述性长句。这组样本的用处是**负向**的：它们不该被任何一条判据抓到。
DISTINCT_POINTS = [
    {
        "id": "VP-101",
        "title": "验证登录接口返回 200",
        "covers_paths": ["backend/auth/"],
        "verification_method": "api_test",
        "ci_entry": "pytest backend/tests/unit/test_auth.py -v",
        "expected_result": "登录接口返回 200 且响应体包含 token 字段",
    },
    {
        "id": "VP-102",
        "title": "验证注册流程落库正确",
        "covers_paths": ["backend/api/"],
        "verification_method": "integration_test",
        "ci_entry": "pytest backend/tests/unit/test_api.py -v",
        "expected_result": "注册流程落库成功且数据库出现对应用户记录",
    },
    {
        "id": "VP-103",
        "title": "验证任务队列按序消费",
        "covers_paths": ["backend/task/"],
        "verification_method": "unit_test",
        "ci_entry": "pytest backend/tests/unit/test_task.py -v",
        "expected_result": "任务队列按入队顺序逐条消费且无遗漏无重复",
    },
    {
        "id": "VP-104",
        "title": "验证去重报告落在计划目录",
        "covers_paths": ["backend/plan/"],
        "verification_method": "static_check",
        "ci_entry": "pytest backend/tests/unit/test_plan.py -v",
        "expected_result": "去重报告写在计划目录下且能被完整读回",
    },
]


# ---------------------------------------------------------------------------
# R5 三条误伤用例
# ---------------------------------------------------------------------------

#: 同一份命令与验证方法，两条各跑各的场景。
SAME_CMD = {"verification_method": "full_ci", "ci_entry": "bash scripts/ci_gate.sh"}

#: R5 第 1 条：同一入口脚本用于两个不同场景（后端 vs 前端）。
#:
#: 两条都给了描述性判据，所以这一组可以连"零空泛判据"一起断言 —— 判据
#: 写不写跟判重结果无关（``expected_result`` 不参与判重），但写上之后
#: 整组电池的断言口径就统一成"零重复组 + 零空泛判据"。
R5_PAIR_A = [
    dict(
        SAME_CMD,
        id="VP-1",
        title="后端整仓门禁",
        covers_paths=["backend/"],
        expected_result="后端整仓门禁退出码 0 且全部后端测试通过",
    ),
    dict(
        SAME_CMD,
        id="VP-2",
        title="前端整仓门禁",
        covers_paths=["frontend/"],
        expected_result="前端整仓门禁退出码 0 且全部前端测试通过",
    ),
]

#: R5 第 2 条：只有 ``expected_result`` 相同，命令与覆盖面都不同。
R5_PAIR_B = [
    {
        "id": "VP-3",
        "expected_result": "接口返回 200 且字段完整",
        "covers_paths": ["backend/api/"],
        "verification_method": "api_test",
        "ci_entry": "pytest a",
    },
    {
        "id": "VP-4",
        "expected_result": "接口返回 200 且字段完整",
        "covers_paths": ["backend/web/"],
        "verification_method": "e2e",
    },
]

#: R5 第 3 条：覆盖面为空，两条标题互为子串。
R5_PAIR_C = [
    {
        "id": "VP-5",
        "title": "验证登录流程",
        "covers_paths": [],
        "verification_method": "api_test",
        "expected_result": "登录接口返回 200 且响应体包含用户标识",
    },
    {
        "id": "VP-6",
        "title": "验证登录流程完整",
        "covers_paths": [],
        "verification_method": "api_test",
        "expected_result": "登录流程端到端走通且刷新后依然保持会话",
    },
]


# ---------------------------------------------------------------------------
# 钩子装配
# ---------------------------------------------------------------------------


def _verification_agent(plan_file: Path):
    """造一个只带 ``verification_plan_file`` 的 ``VerificationAgent``。

    钩子只碰 ``verification_plan_file``（计划目录从它推出来），因此不走
    ``__init__`` —— ``__init__`` 要 plan_dir / project_dir，钩子一个都不用。
    """
    module = importlib.import_module("verification_agent")
    agent = object.__new__(module.VerificationAgent)
    agent.verification_plan_file = plan_file
    return agent


def _tasks_generator(directory: Path):
    """造一个只带落盘所需属性的 ``TasksGenerator``。

    用 ``object.__new__`` 绕开 ``__init__``：``__init__`` 要一整套依赖
    （coding_tool 等），这里要验的只有落盘这一个动作。
    """
    module = importlib.import_module("tasks_generator")
    generator = object.__new__(module.TasksGenerator)
    generator.tasks_file = directory / "tasks.json"
    generator.plan_dir = directory
    generator._defer_tasks_writes = False
    return generator


#: 两条改了同一批文件、跑了同一条命令的任务 → 判重成立（报告必须出现）。
DUPLICATE_TASKS = {
    "tasks": [
        {
            "id": "1",
            "files_to_modify": ["backend/plan_dedup.py"],
            "test_commands": ["pytest backend/tests/unit/test_plan_dedup.py -v"],
        },
        {
            "id": "2",
            "files_to_modify": ["backend/plan_dedup.py"],
            "test_commands": ["pytest backend/tests/unit/test_plan_dedup.py -v"],
        },
    ]
}


# ---------------------------------------------------------------------------
# 验收 1：真实案例负控
# ---------------------------------------------------------------------------


def test_real_case_groups_vp022_and_vp025_and_flags_vp024(tmp_path):
    """真实案例：VP-022 与 VP-025 同组，VP-024 判空泛判据。

    这是整套规则存在的理由 —— 需求里的那个真实计划
    里躺着三条「整仓门禁」验证点，其中两条跑同一条命令、一条判据只有四个
    字（「验证通过」）。检测器必须把它们指出来。

    VP-024 跑的是同一条命令，所以它也会落进这个组 —— 需求并没有要求排除
    它（它本来就是那条该被处理的残留），因此断言写成"**存在**一个组同时含
    VP-022 与 VP-025"。

    走**真实钩子**（``VerificationAgent._persist_verification_plan``），断言读
    的是**落盘的文件** —— 判定只活在一次调用的返回值里等于没做：钩子传错
    计划、写错目录、抹掉 ``expected_result``，都只有读盘才看得出来。
    """
    plan_data = _real_case_plan()
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    _verification_agent(plan_dir / "verification_plan.json")._persist_verification_plan(
        plan_data
    )

    json_path = plan_dir / VERIFICATION_REPORT_JSON
    txt_path = plan_dir / VERIFICATION_REPORT_TEXT
    assert json_path.exists(), "真实案例必须出报告文件 —— 检测器漏掉了真问题"
    assert txt_path.exists(), "真实案例必须出摘要文件"

    report = json.loads(json_path.read_text(encoding="utf-8"))

    groups = [group["member_ids"] for group in report["duplicate_groups"]]
    assert any(
        "VP-022" in members and "VP-025" in members for members in groups
    ), f"VP-022 与 VP-025 必须同组，实际分组：{groups}"

    vague = report["vague_criteria"]
    assert [item["id"] for item in vague] == ["VP-024"], (
        "只有 VP-024 的判据是空话（「验证通过」），其余两条都是描述性长句"
    )
    assert vague[0]["expected_result"] == "验证通过", (
        "报告必须回显计划里**原文**的判据，不是归一化后的样子"
    )


# ---------------------------------------------------------------------------
# 验收 2：正向 —— 明显不同的方案不产生任何报告
# ---------------------------------------------------------------------------


def test_distinct_plan_writes_no_report(tmp_path):
    """四条明显不同的验证点 → 零重复组、零空泛判据、**不落任何报告文件**。

    计划目录里凭空多一份空报告，会让"目录里有报告"这件事失去意义 ——
    读的人分不清"查过了没问题"与"压根没查"。所以干净的计划**连文件都
    不该有**。
    """
    plan = {"verification_points": copy.deepcopy(DISTINCT_POINTS)}

    report = build_verification_report(DISTINCT_POINTS)
    assert report["duplicate_groups"] == [], "四条不同的验证点不该成组"
    assert report["vague_criteria"] == [], "四条描述性判据不该被标成空泛"

    assert write_verification_dedup_report(plan, tmp_path) is None
    assert sorted(path.name for path in tmp_path.iterdir()) == [], (
        "干净的计划不该在目录里留下任何报告文件"
    )


# ---------------------------------------------------------------------------
# 验收 3：R5 三条误伤用例
# ---------------------------------------------------------------------------


def test_r5_false_positive_battery_stays_clean():
    """R5 点名的三条 → 全部零重复组、零空泛判据。

    **误伤比漏报贵**：把两条其实不同的验证点判成重复，等于让操作员删掉
    其中一条该跑的关卡。这三条用例是需求里逐字写下来的"不得误伤"，必须
    逐条钉死。

    （``expected_result`` 不参与判重，所以给 R5 前两组的判据写成长句只是
    为了让整组电池的断言口径统一成"零重复组 + 零空泛判据"。）
    """
    for name, pair in (
        ("R5-1 同入口不同场景", R5_PAIR_A),
        ("R5-2 只有判据相同", R5_PAIR_B),
        ("R5-3 空覆盖面标题相似", R5_PAIR_C),
    ):
        report = build_verification_report(pair)
        assert report["duplicate_groups"] == [], f"{name} 被误判成重复"
        assert report["vague_criteria"] == [], f"{name} 被误判成空泛判据"


# ---------------------------------------------------------------------------
# 验收 4：报告确定性
# ---------------------------------------------------------------------------


def test_two_runs_produce_identical_report_bytes(tmp_path):
    """同一份输入跑两次 → 两份报告**逐字节相同**。

    报告是要被 diff、被审的产物。里面掺一个时间戳或一个绝对路径，每次
    diff 都脏，"这次改动引入了新的重复"这句话就再也说不清了。

    跑两遍：一次**同一个目录**（覆盖写），一次**另一个目录**（顺带证明
    报告里没有混进路径之类的环境相关字节）。

    同样走**真实钩子** —— 掺时间戳这类缺陷长在写盘的那一段，直接调 writer
    根本照不到。
    """
    plan_data = _real_case_plan()

    same_dir = tmp_path / "same"
    same_dir.mkdir()
    _verification_agent(same_dir / "verification_plan.json")._persist_verification_plan(
        copy.deepcopy(plan_data)
    )
    first_json = (same_dir / VERIFICATION_REPORT_JSON).read_bytes()
    first_txt = (same_dir / VERIFICATION_REPORT_TEXT).read_bytes()

    # 第二次：同一个目录，覆盖写。
    _verification_agent(same_dir / "verification_plan.json")._persist_verification_plan(
        copy.deepcopy(plan_data)
    )
    assert (same_dir / VERIFICATION_REPORT_JSON).read_bytes() == first_json
    assert (same_dir / VERIFICATION_REPORT_TEXT).read_bytes() == first_txt

    # 第三次：另一个目录 —— 报告里不能混进任何环境相关的字节。
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    _verification_agent(other_dir / "verification_plan.json")._persist_verification_plan(
        copy.deepcopy(plan_data)
    )
    assert (other_dir / VERIFICATION_REPORT_JSON).read_bytes() == first_json
    assert (other_dir / VERIFICATION_REPORT_TEXT).read_bytes() == first_txt


# ---------------------------------------------------------------------------
# 验收 5：开关关闭时产物逐字节不变
# ---------------------------------------------------------------------------


def test_disabled_switch_preserves_plan_and_tasks_bytes(tmp_path, monkeypatch):
    """``PDT_PLAN_DEDUP=0`` → 不出报告，且计划与任务列表**逐字节不变**。

    验收第 5 条是"关掉开关时，验证计划与任务列表与改动前逐字节相同"。这里
    把它拆成两半各自钉死：

    * **报告不出现** —— 目录里只有计划与任务列表两个文件；
    * **已有产物一个字节都不变** —— 同一个钩子在开关开启 / 关闭两种状态下
      写出的计划与任务列表，比对的是**字节**而不是解析后的结构（换个
      ``indent``、多写一个 ``\n`` 都是"变了"）。

    样本用的是**有重复**的那份数据（真实案例 + 两条重复任务）：只有存在
    可报告的内容，"没有报告文件"才说明是开关关掉的，而不是"本来就没得报"。
    """
    plan_data = _real_case_plan()
    tasks_payload = copy.deepcopy(DUPLICATE_TASKS)

    # --- 开关开启（默认） --------------------------------------------------
    monkeypatch.delenv("PDT_PLAN_DEDUP", raising=False)
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    _verification_agent(on_dir / "verification_plan.json")._persist_verification_plan(
        plan_data
    )
    _tasks_generator(on_dir)._write_tasks_file(tasks_payload)

    on_plan_bytes = (on_dir / "verification_plan.json").read_bytes()
    on_tasks_bytes = (on_dir / "tasks.json").read_bytes()

    # 开启时报告确实在 —— 否则下面的"关闭时没有报告"证明不了什么。
    assert (on_dir / VERIFICATION_REPORT_JSON).exists()
    assert (on_dir / VERIFICATION_REPORT_TEXT).exists()
    assert (on_dir / TASKS_REPORT_JSON).exists()

    # --- 开关关闭 ----------------------------------------------------------
    monkeypatch.setenv("PDT_PLAN_DEDUP", "0")
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    _verification_agent(off_dir / "verification_plan.json")._persist_verification_plan(
        plan_data
    )
    _tasks_generator(off_dir)._write_tasks_file(tasks_payload)

    off_plan_bytes = (off_dir / "verification_plan.json").read_bytes()
    off_tasks_bytes = (off_dir / "tasks.json").read_bytes()

    # 报告一个都不许出现。
    assert sorted(path.name for path in off_dir.iterdir()) == [
        "tasks.json",
        "verification_plan.json",
    ], "开关关闭时目录里不该有任何报告文件"

    # 计划与任务列表：逐字节不变。
    assert off_plan_bytes == on_plan_bytes, "开关关闭改变了验证计划的字节"
    assert off_tasks_bytes == on_tasks_bytes, "开关关闭改变了任务列表的字节"

    # 计划内容本身也确是那份真实案例（防止"两边都写空了所以相等"）。
    assert json.loads(off_plan_bytes.decode("utf-8")) == plan_data
    assert json.loads(off_tasks_bytes.decode("utf-8")) == tasks_payload