"""共存契约：五个调用点在「任务清单 + 计划书」同目录上的答案。

背景
----
一个 plan 目录里可能躺着两类产物：``tasks.json``（开发分支）与
``verification-plan.md``（验证分支）。两类**同时存在**不是冲突：产物判定
（:func:`plan_artifacts.detect`）的输出域是 ``dev`` / ``verification`` /
``empty`` 三个取值，**没有** ``conflict``。共存落在 ``dev`` 上 ——「任务优先」，
计划书是附加产物（PRD 决策点 2：计划书是任务清单之后的一个可选附加动作）。

本文件取代旧的那条契约：「五个调用点统一 409 ``artifact_conflict``」。旧契约
把共存当成冲突，于是每个入口都在替人裁决「这条 plan 到底想干什么」。共存合法
之后，五个调用点在共存目录上的答案各归各位：

============================  ==========================================
入口                          共存目录上的答案
============================  ==========================================
任务生成                       409 ``artifact_conflict``（**反向**生成被拒）
单计划状态                     200：主分支 + 两份产物的相对名
计划书生成                     200：计划书落盘，任务清单一个字节都不动
``/start``                    **dev 语义**：不再被产物门拦，不物化 worktree
计划列表                       200，条目是**普通行**（没有冲突桶）
============================  ==========================================

为什么任务生成还答 409
----------------------
共存是**单向**的：允许「先任务、后补计划书」，不允许把一条纯验证分支的 plan
改造成开发分支。所以目录里已有计划书时，任务生成入口仍然拒绝。错误码字符串
``artifact_conflict`` **保留** —— 它是既有 API 契约的一部分 —— 但它描述的是
「反向生成被拒」，不再是「两类产物互斥」。

钉住的东西
----------
* ``test_five_call_points_return_coexist_semantics`` —— 五个入口逐个参数化，
  答案各归各位；
* ``test_task_generation_still_refused_when_plan_doc_exists`` —— 反向生成
  仍然 409，错误码不变，两个产物都还在；
* ``test_plan_doc_generate_writes_the_doc_and_leaves_the_tasks_alone`` ——
  计划书生成 200，任务清单一个字节都没动；
* ``test_verification_start_is_not_blocked_by_the_artifact_gate`` —— 验证启动
  不再被产物门拦；
* ``test_verification_start_uses_dev_semantics_and_materialises_no_worktree``
  —— 验证启动走**dev 语义**：不物化 worktree，不改写 ``project_dir``；
* ``test_plan_status_gives_the_coexistence_payload`` —— 单计划状态 200，主分支
  是 dev，载荷给出两份产物的相对名；
* ``test_list_has_no_conflict_bucket`` —— 列表条目是普通行，不落冲突桶；
* ``test_list_keeps_other_plans_readable`` —— 共存计划进列表，不影响邻居；
* ``test_artifact_names_are_relative_names_only`` —— 产物名是相对名，plan 目录
  的绝对路径不出现在任何响应里。

测试数据
--------
两个产物都是真的：任务清单是开发任务清单，计划书满足十章节契约（否则生成
端点会把它当非法文档丢掉），基线记在计划状态里（生成端点的门）。模型是替身
（``server.create_coding_tool`` 被换成脚本化工具），所以本文件不碰任何
provider，也不联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from server import app, invalidate_plans_cache  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    PR_NUMBER,
    PR_REF,
    build_machine_plan,
    build_plan_doc,
    no_worker_thread,
)

client = TestClient(app)

#: 开发分支的产物：任务清单（相对名）。
TASKS = "tasks.json"
#: 验证分支的产物：计划书（相对名）。
PLAN_DOC = "verification-plan.md"
#: 机器计划：计划书的**派生物**（相对名）。它不是产物判定的一极 ——
#: 产物判定只看任务清单与计划书两份（见 :func:`plan_artifacts.detect`）。
MACHINE_PLAN = "verification_plan.json"
#: 共存目录里两份产物都在 —— 相对名，不是路径。
BOTH_ARTIFACTS: List[str] = [TASKS, PLAN_DOC]
#: 共存目录的主分支 —— 「任务优先」。
PRIMARY_BRANCH = "dev"
#: 列表里冲突计划的桶 —— 共存之后**不再有**这个桶。
CONFLICT_BUCKET = "conflict"
#: 机器可读错误码（保留：API 契约）。
ERROR_CODE = "artifact_conflict"
#: 共存目录的相位 —— 开发分支的 plan 在开发流程里排队。
DEV_PHASE = "queued"

#: 五个调用点。
CALL_POINTS: Tuple[str, ...] = (
    "tasks_generate",
    "plan_status",
    "plan_doc_generate",
    "verification_start",
    "plans_list",
)

#: 五个调用点在共存目录上的答案（状态码）。
#:
#: 只有任务生成入口还答 409 —— 共存是**单向**的。``verification_start`` 不在
#: 这张表里：它的答案由验证流水线自己的门决定（计划书翻译、等价性、CAS），
#: 本文件只钉「产物门不再拦它」这一条。
COEXISTENCE_STATUS: Dict[str, int] = {
    "tasks_generate": 409,
    "plan_status": 200,
    "plan_doc_generate": 200,
    "plans_list": 200,
}


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cold_plans_cache():
    """列表缓存每次都是冷的。

    列表读的是**每个** plan 目录的产物；本文件每个用例都在同一个目录里现造
    plan，缓存冷不冷会决定列表能不能看见刚造出来的那一条。
    """
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


class _CoexistingPlan:
    """一个共存目录：任务清单 + 合法计划书 + 记录下来的基线。"""

    def __init__(self, plan_id: str, plan_dir: Path, document: str) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.document = document


@pytest.fixture
def coexisting_plan(tmp_path: Path, plan_sqlite_seeder) -> _CoexistingPlan:
    """一个**同时**存在两类产物的计划。

    目录名故意不带日期前缀：``classify_plan`` 只把 ``YYYYMMDD-*`` 认成历史
    归档计划，带日期就意味着这个计划在列表里是「archived」，普通行轮不到它。

    三样东西凑齐才是「一个真的共存目录」：

    * ``tasks.json`` —— 开发分支的产物；
    * 一份**合法**的计划书（十章节契约）—— 生成端点只认合法的文档，非法的那
      份会被丢掉，目录就退化成「只有任务清单」；
    * 计划状态里**记录下来的基线** —— 生成端点的门：缺基线就停在
      ``baseline_missing``，走不到落盘那一步。
    """
    plan_id = "coexist-artifact-plan-1"
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / TASKS).write_text('{"tasks": []}', encoding="utf-8")

    repo_dir = tmp_path / "upstream"
    repo_dir.mkdir(exist_ok=True)
    document = build_plan_doc(repo_dir, pr_number=PR_NUMBER, ref=PR_REF)
    (plan_dir / PLAN_DOC).write_text(document, encoding="utf-8")

    plan_sqlite_seeder(
        plan_dir,
        plan_id,
        phase=DEV_PHASE,
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
            "baseline": {
                "repo_dir": str(repo_dir),
                "ref": PR_REF,
                "commit_sha": "0" * 40,
                "pr_number": PR_NUMBER,
            },
        },
    )
    return _CoexistingPlan(plan_id, plan_dir, document)


class _ScriptedTool:
    """生成工具的替身：每次被问都返回那份合法的计划书。"""

    def __init__(self, document: str = "") -> None:
        self.document = document
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.document


@pytest.fixture(autouse=True)
def scripted_tool(
    monkeypatch: pytest.MonkeyPatch,
    coexisting_plan: _CoexistingPlan,
) -> _ScriptedTool:
    """把 ``server.create_coding_tool`` 换成脚本化替身。

    自动生效：本文件每个用例都可能走到「问模型」那一步（生成端点写计划书，
    验证启动翻计划书），替身没装上就等于把请求发给真的 provider。
    """
    tool = _ScriptedTool(coexisting_plan.document)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)
    return tool


# ---------------------------------------------------------------------------
# 发起调用
# ---------------------------------------------------------------------------


def _call(call_point: str, plan_id: str):
    """调用五个调用点之一，返回响应。"""
    if call_point == "tasks_generate":
        return client.post(f"/api/tasks/{plan_id}/generate")
    if call_point == "plan_status":
        return client.get(f"/api/plan/{plan_id}/status")
    if call_point == "plan_doc_generate":
        return client.post(
            f"/api/verification-plan/{plan_id}/generate",
            json={"feedback": None},
        )
    if call_point == "verification_start":
        return client.post(f"/api/verification/{plan_id}/start")
    if call_point == "plans_list":
        return client.get("/api/plans")
    raise AssertionError(f"未知的调用点：{call_point!r}")


def _entry(payload: Any, plan_id: str) -> Dict[str, Any]:
    """从 ``GET /api/plans`` 的返回里挑出某个计划那一行。"""
    assert isinstance(payload, list), payload
    for item in payload:
        if isinstance(item, dict) and item.get("id") == plan_id:
            return item
    raise AssertionError(f"{plan_id} 不在 /api/plans 的返回里：{payload!r}")


def _mtimes(plan_dir: Path) -> Dict[str, int]:
    """两个产物的 mtime（纳秒）。"""
    return {
        name: (plan_dir / name).stat().st_mtime_ns
        for name in (TASKS, PLAN_DOC)
    }


def _phase_of(plan_dir: Path) -> Optional[str]:
    """计划当前的阶段（走生产读路径）。"""
    from plan_state import PlanState

    return PlanState(plan_dir).get_current_phase()


def _is_artifact_conflict(resp) -> bool:
    """这个响应是不是「产物冲突」那个 409。"""
    if resp.status_code != 409:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == ERROR_CODE


def _assert_no_absolute_path(text: str, *paths: Path) -> None:
    """响应文本里不许出现绝对路径。

    扫两样东西：

    * ``paths`` 里点名的目录（本机的计划目录）—— 逐字比对；
    * ``/Users/`` 与 ``/home/`` 两个**用户目录字面量** —— 抓「回显了别的绝对
      路径」这种逐字比对抓不到的漏法。

    （不写死 ``/tmp/``：macOS 上 pytest 的 ``tmp_path`` 落在
    ``/private/var/folders/...`` 下，写死 ``/tmp/`` 会得到一条恒真断言。）
    """
    for path in paths:
        assert str(path) not in text, (
            "载荷里不许出现本机绝对路径 %s；实际响应：%s" % (path, text)
        )
    for literal in ("/Users/", "/home/"):
        assert literal not in text, (
            "载荷里不许出现用户目录字面量 %s；实际响应：%s" % (literal, text)
        )


# ---------------------------------------------------------------------------
# 1) 五个调用点 → 共存目录上各归各位的答案
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call_point", CALL_POINTS)
def test_five_call_points_return_coexist_semantics(
    call_point: str,
    coexisting_plan: _CoexistingPlan,
) -> None:
    """五个入口逐个参数化：共存目录上它们各自的答案。

    旧契约（「共存 = 409 ``artifact_conflict``」）已经作废：产物判定的输出域
    是 ``dev`` / ``verification`` / ``empty`` 三个取值，**没有** ``conflict``。
    共存落在 ``dev`` 上 —— 主分支是任务优先，计划书是附加产物。

    新契约：

    * 任务生成 → 仍然 409 ``artifact_conflict``。共存是**单向**的：允许「先
      任务、后补计划书」，不允许把一条纯验证分支的 plan 改造成开发分支。
      错误码字符串保留 —— 它是既有 API 契约的一部分。
    * 状态 / 列表 / 计划书生成 → 200：共存合法，框架不替人裁决。
    * 验证启动 → 不再被产物门拦：它的答案由验证流水线自己的门决定。
    """
    resp = _call(call_point, coexisting_plan.plan_id)

    if call_point == "verification_start":
        assert not _is_artifact_conflict(resp), (
            "共存是合法的，验证启动不许被产物门拒；实际响应：%s" % resp.text
        )
        return

    assert resp.status_code == COEXISTENCE_STATUS[call_point], resp.text


# ---------------------------------------------------------------------------
# 2) 反向生成仍然被拒（错误码不变）
# ---------------------------------------------------------------------------


def test_task_generation_still_refused_when_plan_doc_exists(
    coexisting_plan: _CoexistingPlan,
) -> None:
    """共存目录调任务生成仍然 409，错误码**不变**。

    共存是单向的：允许「先任务、后补计划书」，不允许把一条纯验证分支的 plan
    改造成开发分支。所以目录里已有计划书时，任务生成入口仍然拒绝。

    错误码字符串 ``artifact_conflict`` 保留（API 契约）；它描述的是「反向生成
    被拒」，不再是「两类产物互斥」。

    错误载荷里只有**相对名**：本机路径一个字都不许出现（错误响应同样不许
    泄露服务器把 plan 放在哪）。
    """
    plan_id, plan_dir = coexisting_plan.plan_id, coexisting_plan.plan_dir
    mtimes_before = _mtimes(plan_dir)

    resp = _call("tasks_generate", plan_id)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == ERROR_CODE, body
    assert body["artifacts"] == BOTH_ARTIFACTS, (
        "载荷必须给出两个产物的相对名；实际是 %r" % (body.get("artifacts"),)
    )
    _assert_no_absolute_path(resp.text, plan_dir)
    # 被拒绝时两个产物都还在：框架不替人删文件，也不碰它们。
    assert (plan_dir / TASKS).exists()
    assert (plan_dir / PLAN_DOC).exists()
    assert _mtimes(plan_dir) == mtimes_before, (
        "被拒绝时两个产物一个字节都不许被改"
    )


# ---------------------------------------------------------------------------
# 3) 计划书生成：放行，且不动任务清单
# ---------------------------------------------------------------------------


def test_plan_doc_generate_writes_the_doc_and_leaves_the_tasks_alone(
    coexisting_plan: _CoexistingPlan,
    scripted_tool: _ScriptedTool,
) -> None:
    """共存目录生成计划书 → 200，计划书落盘，任务清单一个字节都没动。

    计划书是任务清单之后的**可选附加动作**（PRD 决策点 2）：所以「目录里已有
    ``tasks.json``」不再是拒绝的理由。落盘的那一份是**计划书**，不是任务清单 ——
    附加动作不许改开发分支的产物。
    """
    plan_id, plan_dir = coexisting_plan.plan_id, coexisting_plan.plan_dir
    tasks_before = (plan_dir / TASKS).read_bytes()
    phase_before = _phase_of(plan_dir)

    resp = _call("plan_doc_generate", plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_path"] == PLAN_DOC, (
        "文档路径必须是**相对路径**；实际是 %r" % body["document_path"]
    )
    assert "artifact_conflict" not in resp.text, (
        "产物门已经不存在了，共存是合法的；实际响应：%s" % resp.text
    )
    assert (plan_dir / PLAN_DOC).exists(), "计划书必须落盘"
    assert (plan_dir / TASKS).read_bytes() == tasks_before, (
        "生成计划书不许动任务清单"
    )
    assert _phase_of(plan_dir) == phase_before, (
        "开发分支的相位不许因为生成计划书而改变；实际是 %r" % _phase_of(plan_dir)
    )
    assert scripted_tool.prompts, "模型被问过了（文档是它写的）"


# ---------------------------------------------------------------------------
# 4) 验证启动：产物门不再拦它
# ---------------------------------------------------------------------------


def test_verification_start_is_not_blocked_by_the_artifact_gate(
    coexisting_plan: _CoexistingPlan,
    scripted_tool: _ScriptedTool,
    no_worker_thread: None,
) -> None:
    """共存目录调 ``/start`` 不被产物门拦下。

    产物门（``BRANCH_CONFLICT`` 分支）已经不存在了：共存是合法的，所以这个
    入口不会用 ``artifact_conflict`` 拒一条共存目录的 plan。它的答案来自验证
    流水线自己的门（计划书翻译、等价性、CAS），不是来自产物判定。

    断言两件事：载荷里没有 ``artifact_conflict``；请求真的走进了流水线（模型
    被问过了）—— 后者证明它不是「碰巧没走到那儿」。
    """
    resp = _call("verification_start", coexisting_plan.plan_id)

    assert not _is_artifact_conflict(resp), (
        "共存是合法的，验证启动不许被产物门拒；实际响应：%s" % resp.text
    )
    assert "artifact_conflict" not in resp.text, resp.text
    assert scripted_tool.prompts, (
        "请求得走进验证流水线才轮得到「产物门没拦它」这句话"
    )


def test_verification_start_uses_dev_semantics_and_materialises_no_worktree(
    coexisting_plan: _CoexistingPlan,
    no_worker_thread: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共存目录调 ``/start``：**dev 语义**，**不物化 worktree**。

    决策表（测试设计 §决策点 2）里「两者共存 × 启动」那一格写的是「dev 语义，
    不物化 worktree」。两半各有一个可观察的证据：

    * **dev 语义** —— 开发任务没做完时 ``/start`` 仍然是既有的 409
      ``stage_mismatch``（**不跳过执行**）。共存目录的主分支是开发分支，所以
      它拿不到纯验证分支那张「审核完备即可启动」的入场券。
    * **不物化 worktree** —— 验证对象准备（基线漂移校验 → 物化 worktree →
      注册 ``project_dir``）只属于纯验证分支。本用例的基线指向一个**不是 git
      仓库**的目录，那一段一旦被走进来，答案立刻变成 422
      ``baseline_unresolvable``：答案不同即证明它没被走进来。

    为了让答案落在「开发分支的门」而不是「翻译门」上，盘上先放一份与计划书
    逐条相等的机器计划（计划的派生物）：计划准备这一段因此不需要问模型。
    """
    from verification_workspace import WORKSPACE_ROOT_ENV, workspace_root

    plan_id, plan_dir = coexisting_plan.plan_id, coexisting_plan.plan_dir
    monkeypatch.setenv(
        WORKSPACE_ROOT_ENV, str(tmp_path / "verification-workspaces")
    )
    (plan_dir / MACHINE_PLAN).write_text(
        json.dumps(build_machine_plan(coexisting_plan.document), ensure_ascii=False),
        encoding="utf-8",
    )
    project_dir_before = server._get_project_dir(plan_id)

    resp = _call("verification_start", plan_id)

    # dev 语义：开发任务没做完 → 既有 409 stage_mismatch（不跳过执行）。
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body.get("error") == "conflict", body
    assert body.get("reason") == "stage_mismatch", body
    assert "artifact_conflict" not in resp.text, resp.text

    # 不物化 worktree：project_dir 没被改写，验证工作区也没被建出来。
    assert server._get_project_dir(plan_id) == project_dir_before, (
        "开发分支的验证跑在自己的目录里，不许把 project_dir 改写到 worktree"
    )
    assert not workspace_root(plan_id).exists(), (
        "共存目录是开发分支，不许物化验证 worktree"
    )


# ---------------------------------------------------------------------------
# 5) 读路径：状态与列表给出共存载荷
# ---------------------------------------------------------------------------


def test_plan_status_gives_the_coexistence_payload(
    coexisting_plan: _CoexistingPlan,
) -> None:
    """单计划状态 → 200，给出主分支与两份产物的相对名。

    状态与列表读的是同一份判定（:func:`plan_artifacts.detect`），所以两个读者
    不许对同一条 plan 给出两个答案。
    """
    resp = _call("plan_status", coexisting_plan.plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("branch") == PRIMARY_BRANCH, body
    assert body.get("artifacts") == BOTH_ARTIFACTS, body
    assert "artifact_conflict" not in body, body


def test_list_has_no_conflict_bucket(
    coexisting_plan: _CoexistingPlan,
) -> None:
    """列表不崩，且这个计划**不进**冲突桶。

    共存是合法的：一份任务清单加一份计划书，主分支是 dev（任务优先），计划书
    只是附加产物。没有什么需要人裁决 —— 所以列表里没有冲突桶，条目上也不挂
    ``artifact_conflict`` 载荷，它就是一条普通的行。
    """
    resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), coexisting_plan.plan_id)
    assert entry["status"] != CONFLICT_BUCKET, (
        "共存的计划不许落进冲突桶；实际是 %r" % (entry.get("status"),)
    )
    assert "artifact_conflict" not in entry, entry
    assert entry.get("branch") == PRIMARY_BRANCH, entry
    assert entry.get("artifacts") == BOTH_ARTIFACTS, entry


def test_list_keeps_other_plans_readable(
    coexisting_plan: _CoexistingPlan,
    plan_sqlite_seeder,
) -> None:
    """一个 plan 共存，不许把别的 plan 挤出列表。

    列表读的是**每个** plan 目录的产物；共存 plan 自己以普通行的身份出现，
    其它 plan 的行一个都不许少，谁也不许挂上冲突载荷。
    """
    plan_id = coexisting_plan.plan_id
    other_id = "coexist-artifact-neighbour"
    other_dir = server.PLANS_DIR / other_id
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "prd.md").write_text("# prd", encoding="utf-8")
    # 列表的行来自路由表：没有那一行，这个 plan 压根不在列表里。
    plan_sqlite_seeder(
        other_dir,
        other_id,
        phase="prd_review",
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    )

    resp = client.get("/api/plans")

    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), other_id)
    assert entry["status"] != CONFLICT_BUCKET, (
        "没有共存的 plan 不许落进冲突桶；实际是 %r" % (entry.get("status"),)
    )
    assert "artifact_conflict" not in entry, entry
    # 共存的那一条也在列表里 —— 它是普通行，不是被藏起来的行。
    coexisting = _entry(resp.json(), plan_id)
    assert coexisting.get("branch") == PRIMARY_BRANCH, coexisting


# ---------------------------------------------------------------------------
# 6) 产物名是相对名，plan 目录的绝对路径不出现在任何响应里
# ---------------------------------------------------------------------------


def _artifact_names_in(resp, call_point: str, plan_id: str) -> List[str]:
    """这个响应里带出来的产物名。"""
    body = resp.json()
    if call_point == "plans_list":
        return list(_entry(body, plan_id).get("artifacts") or [])
    if call_point == "plan_doc_generate":
        return [str(body.get("document_path"))]
    return list(body.get("artifacts") or [])


@pytest.mark.parametrize(
    "call_point",
    [cp for cp in CALL_POINTS if cp != "verification_start"],
)
def test_artifact_names_are_relative_names_only(
    call_point: str,
    coexisting_plan: _CoexistingPlan,
) -> None:
    """产物名是 **plan 相对名**，plan 目录的绝对路径不出现在响应里。

    调用方拿产物名去自己的 plan 目录里找文件，不需要知道服务器把 plan 放在哪。

    （列表的条目里另有 ``plans_dir`` 这个来源标记 —— 它说的是「这份列表是从哪
    读出来的」，不是产物名，所以不在本条的断言范围内。）
    """
    plan_id, plan_dir = coexisting_plan.plan_id, coexisting_plan.plan_dir

    resp = _call(call_point, plan_id)

    assert str(plan_dir) not in resp.text, (
        "响应不许出现 plan 目录的绝对路径；实际响应：%s" % resp.text
    )
    names = _artifact_names_in(resp, call_point, plan_id)
    assert names, "响应必须带出产物名；实际响应：%s" % resp.text
    for name in names:
        assert "/" not in name and "\\" not in name, (
            "产物名必须是相对名；实际是 %r" % (name,)
        )