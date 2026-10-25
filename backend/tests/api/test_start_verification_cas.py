"""``POST /api/verification/{id}/start`` 的源阶段集合：验证分支的放行判定。

背景
----
``/start`` 的**唯一**放行判定是 :meth:`RoutingRepository.try_mark_phase` 的源阶段
集合：计划当前的阶段不在集合里 → ``409 {"error": "conflict", "reason":
"stage_mismatch"}``。阶段词汇表变大**不会**让新阶段自动被接纳——所以「验证分支
已就绪」这个事实必须由 :func:`verification_gate.verification_branch_ready` 回答，
而它的答案只以「多一个源阶段」的形式进入 CAS。

本文件钉住五件事：

* **验证分支六维全真** → 200，阶段推进到 ``verification_running``；
* **开发分支的源阶段元组逐项不变**（冻结值断言）——验证分支的放行不得收窄
  开发分支的既有可达性；
* **审核不完备 / 基线漂移** → 既有 409，**不新增错误码**；
* **既有的 409 用例**（阶段不匹配）保持绿；
* ``verification_plan_review`` **不在** ``begin_verification`` 的「已是验证态」
  集合里——从该阶段调 ``begin_verification`` 仍然进入 ``verification``。

测试数据
--------
真实 git 仓库（两个提交 + ``refs/pull/7/head``）与一份 canonical 验证计划书。
真实 git 是刻意的：基线维度问的就是「ref 当前解析到哪个 commit」，mock 掉它等于
把要测的维度测没了。审核条目用 :func:`record_action` 落盘，与生产路径同一条。

线程
----
``/start`` 在 CAS 之后会 spawn 一个验证 worker 线程。这些用例测的是**线程启动
之前**的那次 CAS，所以线程被替换成不执行 target 的空壳——worker 真的会去驱动
整条验证回路。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
import verification_gate  # noqa: E402
from routes.verification import MACHINE_PLAN_FILENAME  # noqa: E402
from verification_plan_review import record_action  # noqa: E402

client = TestClient(server.app)


#: 一份合法的计划书：十个章节按模板顺序排列，两个 VP 块。
CANONICAL_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为 upstream 仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡

## 排除范围

- 前端交互（本次未改动前端）

## 验证方法

- 以单元测试为主，必要时做一次手工核验

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：单元测试
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
- 预期结果：判据全部满足，无超时

### VP-002 报告把 skip 条目标注为未验证

- 范围：backend/verification_report.py
- 验证方法：单元测试
- 通过判据：
  - C1: skip 条目显示为「未验证」
- 证据要求：
  - E1: tests/unit/test_report_render.py 的 pytest 输出
- 预期结果：渲染结果与期望一致

## 预期结果

- 全部验证点通过，未发现需要修复的缺陷

## 证据要求

- 每个验证点给出可复跑的命令与原始输出

## 通过判据

- 所有验证点的 C 编号判据全部满足

## 环境与依赖

- Python 3.11 与 pytest 可用

## 风险面

- 上游 PR head 可能移动，导致基线漂移
"""

#: canonical 文档的审核条目数：5 个计划级条目 + 2 个验证点条目。
ITEM_COUNT = 7


def _machine_plan_for_canonical_md() -> Dict[str, Any]:
    """由 :data:`CANONICAL_MD` 推出一份与之**等价**的机器计划。

    为什么夹具要写这份东西
    ----------------------
    ``/start`` 的管线只在 ``plan_dir / MACHINE_PLAN_FILENAME`` **不存在**时
    才现译，而现译是要**在请求线程里同步问一次模型**的。测试既不该问模型
    （那依赖宿主机上的 provider，CI 上没有），也不该把一次网络调用堵在
    请求线程里（那是卡死的形状）。所以盘上必须先有一份：管线直接读盘。

    为什么**推**出来而不是另写一份常量
    --------------------------------
    等价性门禁（``validate_equivalence``）比四个维度：验证点 id 集合、
    判据编号集合、执行范围、证据编号集合。推出来的那份与计划书同源，
    计划书改了它跟着改 —— 另写一份常量早晚会与计划书漂移，而漂移的表现
    是 ``422 verification_plan_equivalence_failed``，很难一眼看出是
    夹具的机器计划过期了。

    ``expected_result`` 里的 ``E<n> | `` 前缀是机器格式引用证据编号的
    规范形式：计划书的「预期结果」那一行自己不引用任何证据编号。
    """
    from verification_plan_doc import parse_plan_doc
    from verification_plan_translator import validate_equivalence

    parsed = parse_plan_doc(CANONICAL_MD)
    verification_points: List[Dict[str, Any]] = []
    for item in parsed.items:
        verification_points.append(
            {
                "id": item.id,
                "title": item.title,
                "verification_method": item.method,
                "related_prd_criteria": "；".join(
                    f"{cid}: {text}" for cid, text in item.criteria.items()
                ),
                "expected_result": (
                    f"{', '.join(item.evidence.keys())} | {item.expected_result}"
                ),
            }
        )
    machine_plan: Dict[str, Any] = {
        "services": [],
        "verification_points": verification_points,
    }

    divergences = validate_equivalence(parsed, machine_plan)
    assert not divergences, (
        "夹具推出的机器计划与计划书不等价："
        + "；".join(
            f"{d.dimension}/{d.item_id}: {d.detail}" for d in divergences
        )
    )
    return machine_plan


#: 机器计划：与 :data:`CANONICAL_MD` 等价的那一份（推导一次，全局复用）。
MACHINE_PLAN: Dict[str, Any] = _machine_plan_for_canonical_md()

#: 开发分支的源阶段集合 —— **冻结值**。
#:
#: 验证分支多一个源阶段是这次改动唯一允许的形状，开发分支的这条元组必须
#: 逐项不变：多一项就意味着开发分支的既有可达性被悄悄收窄了。
DEV_SOURCE_PHASES: Tuple[str, ...] = (
    "executing",
    "verification",
    "verification_failed",
    "verification_loop_stopped",
    "verification_passed",
    "completed",
    "failed",
    "stopped",
    "verification_repairing",
)

#: 验证分支的新源阶段——只有在「就绪」成立时才进入 CAS 的源集合。
REVIEW_PHASE = "verification_plan_review"


# ---------------------------------------------------------------------------
# 真实 git，确定性
# ---------------------------------------------------------------------------


def _git_env() -> dict:
    """An environment that makes git deterministic and offline."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "PDT Verification Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "PDT Verification Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A local repository whose ``refs/pull/7/head`` points at the second commit."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    (repo / "feature.txt").write_text("pr head\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "--quiet", "-m", "pr head")
    _git(repo, "update-ref", "refs/pull/7/head", "HEAD")
    return repo


# ---------------------------------------------------------------------------
# 隔离的环境：plans 目录与状态库都在 tmp_path 里
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both the plans tree and the state DB at ``tmp_path``.

    ``PDT_PLANS_DIR`` gives every resolver the same view; ``server.PLANS_DIR``
    is patched as well because ``server._plan_dir`` reads the module global.

    ``PDT_VERIFICATION_WORKSPACE_ROOT`` 也指向 ``tmp_path``：验证工作区
    （worktree）默认落在 ``~/.pdt/verification-workspaces/<plan_id>`` ——
    那是**整个会话共享**的一处。同一个 plan_id 的两个用例于是撞上同一个
    worktree 路径，第二个用例报 ``409``（工作区冲突）而不是走完管线。
    每个用例一份自己的工作区根，撞车就不存在了。
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = tmp_path / "verification-workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("PDT_VERIFICATION_WORKSPACE_ROOT", str(workspace_root))
    return plans_dir


def _record_routing_row(
    plan_id: str,
    *,
    phase: str,
    verification: Dict[str, Any],
) -> None:
    """Write the ``plan_routing`` row the way production does.

    The baseline lives in the routing row's ``verification`` column, so the
    fixture writes it through the repository layer rather than through a
    hand-made file — the reader under test must find it where the freeze
    step puts it.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

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
    """Read the plan's routing phase straight out of the state DB."""
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


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


class _Plan:
    """一个计划：id 与目录。"""

    def __init__(self, plan_id: str, plan_dir: Path):
        self.plan_id = plan_id
        self.plan_dir = plan_dir


@pytest.fixture
def make_verification_plan(
    tmp_path: Path,
    git_repo: Path,
    hermetic_env: Path,
    plan_sqlite_seeder,
):
    """Build a verification-branch plan; every dimension ready unless broken."""
    counter = {"n": 0}

    def _make(
        *,
        review_complete: bool = True,
        baseline_overrides: Optional[Dict[str, Any]] = None,
        ref_moved: bool = False,
    ) -> _Plan:
        counter["n"] += 1
        plan_id = f"verify-plan-cas-{counter['n']}"
        plan_dir = hermetic_env / plan_id
        plan_dir.mkdir(parents=True)
        (plan_dir / "verification-plan.md").write_bytes(
            CANONICAL_MD.encode("utf-8")
        )
        # 机器计划：与计划书等价的那一份。盘上已有它，``/start`` 的管线
        # 就直接读盘，不会去问模型 —— 这一步是「不卡死」的全部要害。
        (plan_dir / MACHINE_PLAN_FILENAME).write_text(
            json.dumps(MACHINE_PLAN, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        for index in range(ITEM_COUNT):
            record_action(plan_dir, index, "accept")
        if not review_complete:
            # 一个条目回到 pending —— 审核不完备。
            record_action(plan_dir, 3, "reset")

        baseline = {
            "repo_dir": str(git_repo),
            "ref": "refs/pull/7/head",
            "commit_sha": _git(git_repo, "rev-parse", "refs/pull/7/head"),
            "pr_number": 7,
        }
        if baseline_overrides:
            baseline.update(baseline_overrides)

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

        if ref_moved:
            # PR head 移动过 —— 基线漂移。
            _git(git_repo, "update-ref", "refs/pull/7/head", "HEAD~1")

        return _Plan(plan_id, plan_dir)

    return _make


@pytest.fixture
def make_dev_plan(hermetic_env: Path, plan_sqlite_seeder):
    """Build a development-branch plan (``tasks.json`` 在场），阶段 ``executing``。"""
    counter = {"n": 0}

    def _make() -> _Plan:
        counter["n"] += 1
        plan_id = f"dev-plan-cas-{counter['n']}"
        plan_dir = hermetic_env / plan_id
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
            plan_dir,
            plan_id,
            phase="executing",
            project_dir=str(project_dir),
        )
        return _Plan(plan_id, plan_dir)

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

    ``/start`` spawns the worker *after* the CAS; these cases are about the
    CAS, and the worker would drive the whole verification loop.
    """
    monkeypatch.setattr(threading, "Thread", _NoOpThread)


# ---------------------------------------------------------------------------
# CAS 间谍：把源阶段元组抓下来
# ---------------------------------------------------------------------------


@pytest.fixture
def cas_spy(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, tuple, str]]:
    """Record every ``try_mark_phase`` call's source-phase tuple."""
    from state_machine.repositories.routing_repository import RoutingRepository

    calls: List[Tuple[str, tuple, str]] = []
    original = RoutingRepository.try_mark_phase

    def _spy(self, plan_id, expected_phases, new_phase, substage=None):
        calls.append((plan_id, tuple(expected_phases), new_phase))
        return original(self, plan_id, expected_phases, new_phase, substage)

    monkeypatch.setattr(RoutingRepository, "try_mark_phase", _spy)
    return calls


# ---------------------------------------------------------------------------
# 1. 验证分支：就绪 → 放行
# ---------------------------------------------------------------------------


def test_verification_branch_start_succeeds(
    make_verification_plan,
    no_worker_thread,
) -> None:
    """六维全真的验证分支 → 200，阶段推进到 ``verification_running``。"""
    plan = make_verification_plan()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "started"
    assert _routing_phase(plan.plan_id) == "verification_running"


def test_verification_branch_source_tuple_gains_the_review_phase(
    make_verification_plan,
    no_worker_thread,
    cas_spy,
) -> None:
    """就绪时，源阶段元组多出 ``verification_plan_review`` 一项。"""
    plan = make_verification_plan()

    client.post(f"/api/verification/{plan.plan_id}/start")

    assert cas_spy, "try_mark_phase was never called"
    _plan_id, sources, new_phase = cas_spy[-1]
    assert new_phase == "verification_running"
    assert REVIEW_PHASE in sources


# ---------------------------------------------------------------------------
# 2. 开发分支：源阶段元组逐项不变
# ---------------------------------------------------------------------------


def test_dev_source_tuple_byte_identical(
    make_dev_plan,
    no_worker_thread,
    cas_spy,
) -> None:
    """开发分支的源阶段元组与冻结值**逐项**相等。

    逐项比而不是整体比：整体比在长度不同时报的是一句「不等」，看不出是
    多了哪一项、少了哪一项。验证分支多一项是这次改动唯一允许的形状。
    """
    plan = make_dev_plan()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert cas_spy, "try_mark_phase was never called"
    _plan_id, sources, _new_phase = cas_spy[-1]
    assert len(sources) == len(DEV_SOURCE_PHASES), (
        f"开发分支的源阶段元组长度变了：{sources}"
    )
    for index, (got, want) in enumerate(zip(sources, DEV_SOURCE_PHASES)):
        assert got == want, (
            f"开发分支的源阶段元组第 {index} 项变了：{got!r} != {want!r}"
        )


# ---------------------------------------------------------------------------
# 3. 谓词为假 → 既有 409，不新增错误码
# ---------------------------------------------------------------------------


def test_incomplete_review_or_baseline_409(
    make_verification_plan,
    no_worker_thread,
) -> None:
    """审核不完备 / 基线漂移 → 409 ``stage_mismatch``。"""
    incomplete = make_verification_plan(review_complete=False)
    resp = client.post(f"/api/verification/{incomplete.plan_id}/start")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict"
    assert body["reason"] == "stage_mismatch"

    drifted = make_verification_plan(ref_moved=True)
    resp = client.post(f"/api/verification/{drifted.plan_id}/start")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict"
    assert body["reason"] == "stage_mismatch"


def test_predicate_exception_is_treated_as_not_ready(
    make_verification_plan,
    no_worker_thread,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """谓词抛异常 → 按假处理：409，而不是 500。

    「读不懂」与「没准备好」对调用方是同一件事：还没到能跑的时候。
    """
    plan = make_verification_plan()

    def _boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(verification_gate, "verification_branch_ready", _boom)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 409, resp.text
    assert resp.json()["reason"] == "stage_mismatch"


# ---------------------------------------------------------------------------
# 4. 既有的 409 用例保持绿
# ---------------------------------------------------------------------------


def test_existing_stage_mismatch_still_green(
    hermetic_env: Path,
    plan_sqlite_seeder,
    no_worker_thread,
) -> None:
    """阶段不在源集合里 → 既有 409 ``stage_mismatch`` 保持不变。"""
    plan_id = "prd-review-plan"
    plan_dir = hermetic_env / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text('{"tasks": []}', encoding="utf-8")

    _record_routing_row(
        plan_id,
        phase="prd_review",
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
        plan_dir, plan_id, phase="prd_review", project_dir=str(project_dir)
    )

    resp = client.post(f"/api/verification/{plan_id}/start")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "conflict"
    assert body["reason"] == "stage_mismatch"


# ---------------------------------------------------------------------------
# 5. ``begin_verification`` 的「已是验证态」集合不含新阶段
# ---------------------------------------------------------------------------


def test_begin_verification_states_exclude_review_phase(
    make_verification_plan,
) -> None:
    """``verification_plan_review`` 不在「已是验证态」集合里。

    ``begin_verification`` 在「已经是某个验证态」时是**幂等空操作**。新阶段
    如果被算进那个集合，从它调 ``begin_verification`` 就什么都不会发生——
    计划会永远卡在「计划书待审」。所以从该阶段调 ``begin_verification``
    仍然必须进入 ``verification``。
    """
    from plan_state import PlanState

    plan = make_verification_plan()

    ps = PlanState(plan.plan_dir)
    ps.reload()
    assert ps.get_current_phase() == REVIEW_PHASE

    ps.begin_verification()

    assert ps.get_current_phase() == "verification"
