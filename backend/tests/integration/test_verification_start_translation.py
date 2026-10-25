"""``/start`` 的验证分支前奏：翻译、等价校验、基线漂移与工作区物化。

背景
----
``/start`` 原来只做两件事：CAS（阶段放行）与启动验证循环。验证分支在这两件
事之前还缺一整条管线：

* 机器计划得与审核过的计划书**逐条相等** —— 审过的与实际被验的必须是同一批
  验证点；
* 基线得还是审核时冻结的那个 commit —— PR head 移动过，要验的东西就换了；
* 验证得在**被验证对象自己的 worktree** 里跑，而不是在 plan 的旧目录里；
* ``project_dir`` 得指向那个 worktree —— 执行链只读这一列。

顺序与中止
----------
顺序是固定的，而且顺序本身就是契约：

    产物判定 → 翻译 + 等价校验 → 基线漂移校验 → 物化 worktree → 注册
    project_dir → 既有循环

任何一步失败都**中止**，且中止之前不产生副作用：worktree 没建、project_dir
没改写、循环没启动。

为什么「翻译」排在「漂移校验」前面
--------------------------------
翻译要问一次模型，那是要花时间的，而 PR head 在模型思考期间移动是完全现实的
事。所以漂移校验有两次：放行前那次（不白花一次模型调用）与物化前那次（权威
的那次）。两次之间隔着的就是那次模型调用 —— 这条顺序就是为它准备的。

测试数据
--------
真实 git 仓库（``refs/pull/7/head`` 指向第二个提交）与 canonical 验证计划书。
真实 git 是刻意的：漂移维度问的就是「ref 当前解析到哪个 commit」，mock 掉它
等于把要测的维度测没了。模型是替身：``server.create_coding_tool`` 被换成脚本
化工具 —— 包括「翻译时把 PR head 挪走」这种故障注入。所以本文件不碰任何
provider，也不联网。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from tests.fixtures.verification_mode import register_worktree  # noqa: E402
from tests.unit.test_verification_plan_review import CANONICAL_MD  # noqa: E402
from verification_plan_review import record_action  # noqa: E402

client = TestClient(server.app)

#: 验证分支的机器计划：验证回路读的那一份。
MACHINE_PLAN = "verification_plan.json"

#: canonical 文档的审核条目数：5 个计划级条目 + 2 个验证点条目。
ITEM_COUNT = 7

#: 验证分支的入口阶段：计划书待审。
REVIEW_PHASE = "verification_plan_review"

#: canonical 文档里的基线行（要换成真实的仓库与 ref）。
_BASELINE_LINE = "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7"


# ---------------------------------------------------------------------------
# 机器计划
# ---------------------------------------------------------------------------

#: 与 canonical 文档**逐条相等**的机器格式。
#:
#: 证据引用串是机器格式的规范形式（``E1,E2 | 正文``）：没有这个前缀的
#: ``expected_result`` 等于「一条证据都没引用」，等价性门禁会报出来。
MACHINE_PLAN_JSON: Dict[str, Any] = {
    "services": [],
    "verification_points": [
        {
            "id": "VP-001",
            "title": "配额按真实重开时刻睡眠",
            "verification_method": "单元测试",
            "related_prd_criteria": (
                "C1: 配额耗尽时睡到真实重开时刻；"
                "C2: 双档耗尽时取最晚重开的窗口"
            ),
            "expected_result": "E1 | 两条判据全部满足，无超时",
            "test_command": "pytest backend/verification_loop.py",
        },
        {
            "id": "VP-002",
            "title": "报告把 skip 条目标注为未验证",
            "verification_method": "单元测试",
            "related_prd_criteria": "C1: skip 条目显示为「未验证」",
            "expected_result": "E1 | 渲染结果与期望一致",
            "test_command": "pytest backend/verification_report.py",
        },
    ],
}

#: 翻译时模型只需要给**执行细节**：身份与内容字段一律以文档为准。
MODEL_POINTS: List[Dict[str, Any]] = [
    {"id": "VP-001", "test_command": "pytest backend/verification_loop.py"},
    {"id": "VP-002", "test_command": "pytest backend/verification_report.py"},
]


# ---------------------------------------------------------------------------
# 模型的替身
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """翻译工具的替身：返回一份固定的验证点集合。"""

    def __init__(self, points: Optional[List[Dict[str, Any]]] = None) -> None:
        self.points = MODEL_POINTS if points is None else points
        self.prompts: List[str] = []

    def query_json(
        self,
        prompt: str,
        system_instruction: str = "",
        timeout: int = 0,
    ) -> Dict[str, Any]:
        self.prompts.append(prompt)
        return {"verification_points": self.points}


class _FailingTool:
    """翻译工具的替身：模型不可用。"""

    def __init__(self) -> None:
        self.prompts: List[str] = []

    def query_json(
        self,
        prompt: str,
        system_instruction: str = "",
        timeout: int = 0,
    ) -> Dict[str, Any]:
        self.prompts.append(prompt)
        raise RuntimeError("the provider is down")


class _DriftingTool(_ScriptedTool):
    """翻译工具的替身：模型还在想的时候，上游 PR head 挪走了。

    这是「翻译排在漂移校验前面」那条顺序的**理由**：模型思考期间 ref 移动，
    物化之前那次漂移校验就是靠这个抓到的。
    """

    def __init__(self, repo: Any, ref: str, base_sha: str) -> None:
        super().__init__()
        self._repo = repo
        self._ref = ref
        self._base_sha = base_sha

    def query_json(
        self,
        prompt: str,
        system_instruction: str = "",
        timeout: int = 0,
    ) -> Dict[str, Any]:
        self.prompts.append(prompt)
        self._repo.git("update-ref", self._ref, self._base_sha)
        return {"verification_points": self.points}


# ---------------------------------------------------------------------------
# 状态库
# ---------------------------------------------------------------------------


def _record_routing_row(
    plan_id: str,
    *,
    phase: str,
    verification: Dict[str, Any],
) -> None:
    """写 ``plan_routing`` 行：阶段 + 记录下来的基线。

    基线住在 routing 行的 ``verification`` 段里，所以这里走 repository 层，
    而不是手搓一个文件 —— 读侧找到的必须是冻结那一步放进去的那一份。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_id,
            phase=phase,
            completed_phases=[],
            review_rounds={},
            flags={},
            verification=verification,
            last_updated=None,
        )
    finally:
        conn.close()


def _routing_phase(plan_id: str) -> Optional[str]:
    """计划当前的 routing 阶段。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _project_dir(plan_id: str) -> Optional[str]:
    """``plan_execution.project_dir`` —— 执行链读工作目录的那一列。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT project_dir FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


class _Plan:
    """一个计划：id、目录、原来的 project_dir。"""

    def __init__(self, plan_id: str, plan_dir: Path, project_dir: Path) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.project_dir = project_dir

    @property
    def machine_plan_path(self) -> Path:
        """机器计划在盘上的位置。"""
        return self.plan_dir / MACHINE_PLAN


@pytest.fixture
def make_plan(upstream_repo, workspace_root, plan_sqlite_seeder):
    """造一个验证分支的计划：审核完备、基线已冻结、阶段待审。"""
    counter = {"n": 0}

    def _make(
        *,
        machine_plan: Optional[Dict[str, Any]] = None,
        ref_moved: bool = False,
    ) -> _Plan:
        counter["n"] += 1
        plan_id = f"verify-start-{counter['n']}"
        plan_dir = server.PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True)

        doc = CANONICAL_MD.replace(
            _BASELINE_LINE,
            "baseline: repo={} ref={} pr={}".format(
                upstream_repo.repo,
                upstream_repo.pr_ref,
                upstream_repo.pr_number,
            ),
        )
        (plan_dir / "verification-plan.md").write_bytes(doc.encode("utf-8"))

        for index in range(ITEM_COUNT):
            record_action(plan_dir, index, "accept")

        baseline = {
            "repo_dir": str(upstream_repo.repo),
            "ref": upstream_repo.pr_ref,
            "commit_sha": upstream_repo.head_sha(),
            "pr_number": upstream_repo.pr_number,
        }
        _record_routing_row(
            plan_id,
            phase=REVIEW_PHASE,
            verification={
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
                "baseline": baseline,
            },
        )

        project_dir = plan_dir / "project"
        project_dir.mkdir()
        plan_sqlite_seeder(
            plan_dir,
            plan_id,
            phase=REVIEW_PHASE,
            project_dir=str(project_dir),
        )

        if machine_plan is not None:
            (plan_dir / MACHINE_PLAN).write_text(
                json.dumps(machine_plan, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # worktree 的位置在请求之前就是已知的（工作区根 + plan id）：
        # 交给 teardown 收走，测试不留东西在盘上。
        register_worktree(upstream_repo.repo, workspace_root / plan_id)

        if ref_moved:
            upstream_repo.git(
                "update-ref", upstream_repo.pr_ref, upstream_repo.base_sha()
            )

        return _Plan(plan_id, plan_dir, project_dir)

    return _make


@pytest.fixture
def make_dev_plan(plan_sqlite_seeder):
    """造一个开发分支的计划：``tasks.json`` 在场，阶段 ``executing``。"""
    counter = {"n": 0}

    def _make() -> _Plan:
        counter["n"] += 1
        plan_id = f"dev-start-{counter['n']}"
        plan_dir = server.PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True)
        (plan_dir / "tasks.json").write_text(
            '{"tasks": []}', encoding="utf-8"
        )

        _record_routing_row(
            plan_id,
            phase="executing",
            verification={
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
        )

        project_dir = plan_dir / "project"
        project_dir.mkdir()
        plan_sqlite_seeder(
            plan_dir, plan_id, phase="executing", project_dir=str(project_dir)
        )
        return _Plan(plan_id, plan_dir, project_dir)

    return _make


# ---------------------------------------------------------------------------
# 线程空壳：worker 不会被真的启动
# ---------------------------------------------------------------------------


class _NoOpThread:
    """A thread stand-in that never runs its target."""

    def __init__(self, target=None, daemon=None, *args, **kwargs):
        self._target = target
        self._target_args = kwargs.pop("args", ())
        self._target_kwargs = kwargs.pop("kwargs", {})

    def start(self) -> None:
        return None

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


@pytest.fixture
def no_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the verification worker from running.

    ``/start`` spawns the worker *after* the pipeline; these cases are about
    the pipeline, and the worker would drive the whole verification loop.
    """
    monkeypatch.setattr(threading, "Thread", _NoOpThread)


def _use_tool(monkeypatch: pytest.MonkeyPatch, tool: Any) -> None:
    """把 ``server.create_coding_tool`` 换成脚本化替身。"""
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)


# ---------------------------------------------------------------------------
# 1. 翻译失败 → 中止，且没有副作用
# ---------------------------------------------------------------------------


def test_start_branch_pipeline_order(
    make_plan,
    workspace_root,
    monkeypatch,
    no_worker_thread,
) -> None:
    """翻译失败 → 中止：worktree 没建、project_dir 没改写、循环没启动。

    这条用例钉的是**顺序**：翻译在物化 worktree 之前，所以翻译失败时
    worktree 根本不该存在 —— 这就是「任何一步失败都中止且不产生副作用」。
    """
    plan = make_plan()
    tool = _FailingTool()
    _use_tool(monkeypatch, tool)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 422, resp.text
    assert tool.prompts, "翻译这一步真的问过模型了吗"
    assert not (workspace_root / plan.plan_id).exists(), (
        "翻译失败就不该有 worktree"
    )
    assert _project_dir(plan.plan_id) == str(plan.project_dir), (
        "project_dir 没被改写"
    )
    assert _routing_phase(plan.plan_id) == REVIEW_PHASE, (
        "循环没启动，阶段没推进"
    )


# ---------------------------------------------------------------------------
# 2. 成功路径：机器计划落盘，project_dir 指向 worktree
# ---------------------------------------------------------------------------


def test_start_writes_plan_json_and_registers_worktree(
    make_plan,
    workspace_root,
    monkeypatch,
    no_worker_thread,
) -> None:
    """成功路径 → 机器计划落盘，``project_dir`` 指向 worktree。"""
    plan = make_plan()
    tool = _ScriptedTool()
    _use_tool(monkeypatch, tool)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    worktree = (workspace_root / plan.plan_id).resolve()
    assert worktree.is_dir(), "worktree 被物化了"
    assert resp.json()["project_dir"] == str(worktree), (
        "响应里带着 worktree 的路径"
    )
    assert _project_dir(plan.plan_id) == str(worktree), (
        "project_dir 指向 worktree"
    )

    assert plan.machine_plan_path.exists(), "机器计划落盘了"
    written = json.loads(
        plan.machine_plan_path.read_text(encoding="utf-8")
    )
    assert [vp["id"] for vp in written["verification_points"]] == [
        "VP-001",
        "VP-002",
    ]
    assert written["verification_points"][0]["expected_result"].startswith(
        "E1 | "
    ), "证据引用串是机器格式的规范形式"


# ---------------------------------------------------------------------------
# 3. 等价性失败 → 422，且不落盘
# ---------------------------------------------------------------------------


def test_equivalence_failure_returns_422_without_write(
    make_plan,
    workspace_root,
    monkeypatch,
    no_worker_thread,
) -> None:
    """手工改过的机器计划 → 422，且不落盘、不建 worktree。"""
    broken = json.loads(json.dumps(MACHINE_PLAN_JSON))
    # 证据引用串被抹掉：这条验证点等于「一条证据都没引用」。
    broken["verification_points"][1]["expected_result"] = "渲染结果与期望一致"
    plan = make_plan(machine_plan=broken)
    before = plan.machine_plan_path.read_bytes()
    tool = _ScriptedTool()
    _use_tool(monkeypatch, tool)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "verification_plan_equivalence_failed", body
    assert plan.machine_plan_path.read_bytes() == before, (
        "不落盘：文件没被改写"
    )
    assert not (workspace_root / plan.plan_id).exists(), "没建 worktree"
    assert _project_dir(plan.plan_id) == str(plan.project_dir), (
        "project_dir 没被改写"
    )
    assert not tool.prompts, "盘上已有机器计划时不再问模型"


# ---------------------------------------------------------------------------
# 4. 开发分支：完全走原路径
# ---------------------------------------------------------------------------


def test_dev_branch_does_not_create_worktree(
    make_dev_plan,
    workspace_root,
    monkeypatch,
    no_worker_thread,
) -> None:
    """开发分支 → 不建 worktree，``project_dir`` 不动，也不问模型。"""
    plan = make_dev_plan()
    tool = _ScriptedTool()
    _use_tool(monkeypatch, tool)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert not (workspace_root / plan.plan_id).exists(), (
        "开发分支不建 worktree"
    )
    assert _project_dir(plan.plan_id) == str(plan.project_dir), (
        "project_dir 没被改写"
    )
    assert not tool.prompts, "开发分支不问模型"


# ---------------------------------------------------------------------------
# 5. 基线漂移：模型还在想的时候 PR head 挪走了
# ---------------------------------------------------------------------------


def test_baseline_drift_during_translation_returns_409(
    make_plan,
    workspace_root,
    upstream_repo,
    monkeypatch,
    no_worker_thread,
) -> None:
    """翻译期间基线漂移 → 409，没建 worktree，``project_dir`` 没改写。"""
    plan = make_plan()
    tool = _DriftingTool(
        upstream_repo,
        upstream_repo.pr_ref,
        upstream_repo.base_sha(),
    )
    _use_tool(monkeypatch, tool)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict", body
    assert body["reason"] == "stage_mismatch", body
    assert not (workspace_root / plan.plan_id).exists(), "没建 worktree"
    assert _project_dir(plan.plan_id) == str(plan.project_dir), (
        "project_dir 没被改写"
    )
    assert _routing_phase(plan.plan_id) == REVIEW_PHASE, (
        "循环没启动，阶段没推进"
    )
