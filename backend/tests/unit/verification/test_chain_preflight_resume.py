"""TDD spec —— ``/start`` 前置的根因链预检（任务 10）。

背景
----
``reset_rounds`` **保留**根因链与它的报告，但下一次 ``/start`` 先跑一整轮
验证 —— 而链要等**再次失败**才被读（``check_cycle_conditions`` 才会问它欠
着什么）。于是一份「诊断已交付、该做的实现还没派」的链，在 ``/start`` 之后
先烧掉一整轮 token 与一轮 ``max_rounds``，才走到它本该走的那一步。

本任务交付的是 ``/start`` **之前**的那次问话：预检（preflight）。

四分支表（arch 决策点 6）
------------------------
==========================  ===================  ==============================
链的状态                     预检的回答             之后走哪条路
==========================  ===================  ==============================
空链 / 没有点                ``handled=False``    正常验证轮
老记录（没有 ``head_sha``）   ``handled=False``    正常验证轮
HEAD 变了（链陈旧）           ``handled=False``    正常验证轮
全部 ``BLOCKED_ON_USER``     ``handled=True``     不写 repair、不推进轮次
链欠着活（动作非空）           ``handled=True``     派活；**不跑**验证轮
本次刚 demote 成 blocked     ``handled=False``    正常验证轮（证据在轮里）
==========================  ===================  ==============================

为什么「刚 demote」要落回正常轮
------------------------------
链的 ``implementation`` 阶段表示「实现任务派出去了」。它**仍然失败**这件事
只有一轮验证能证明 —— 所以 demote 的判据在轮里，不在预检里。预检把这一轮
原样交回去（``handled=False``），由轮自己的链判定去 demote、去写台账、去
问人。

边界条件
--------
* **链为空 / 没有动作** → ``{"handled": False}``，继续原路径
  （``run_full_verification`` + ``verification_running``）。
* **全部 ``BLOCKED_ON_USER``** → ``{"handled": True, "blocked": True,
  "reason": "root_cause_blocked_on_user"}``，**不写 repair**、不推进轮次。
* **HEAD 变化 / 老记录没有 ``head_sha``** → ``handled=False``（走正常验证轮）。
* **动作非空** → 不调 ``run_full_verification``、不推进
  ``plan_verification.round``。
* 生成的任务一律带 ``origin="root_cause_preflight"``，只带 ``report_path``
  与 scope，**不带报告正文**。
* 不改 ``max_rounds`` 语义、不改 ``reset_rounds`` 的
  ``clear_root_cause_chain`` 默认值。

TDD contract —— 5 cases
-----------------------
1. ``test_preflight_dispatches_implementation`` —— 链欠实现 → 派活。
2. ``test_preflight_refuses_blocked_chain`` —— 链在人手里 → 不派活。
3. ``test_preflight_stale_head_falls_through`` —— HEAD 变了 → 落回正常轮。
4. ``test_preflight_empty_chain_keeps_normal_round`` —— 空链 → 正常轮。
5. ``test_preflight_tasks_carry_origin_marker`` —— 任务带 origin 标记。

测试命令::

    pytest backend/tests/unit/verification/test_chain_preflight_resume.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import (  # noqa: E402
    _CHAIN_STAGE_IMPLEMENTATION,
    _CHAIN_STAGE_ROOT_CAUSE,
    CHAIN_STAGE_BLOCKED_ON_USER,
    VerificationOrchestrator,
)


VP_ID = "VP-3"

#: The scope the delivered root-cause report declares.
SCOPE = ["backend/app/service.py"]

#: A string that only ever appears in the report's **body** — the task
#: must reference the report, not carry it.
REPORT_BODY_MARKER = "REPORT-BODY-MARKER-9f2c"

#: The marker every task the preflight generates carries.
PREFLIGHT_ORIGIN = "root_cause_preflight"

#: The reason the preflight hands the plan back to a human.
BLOCKED_REASON = "root_cause_blocked_on_user"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo_with_one_commit(root: Path) -> tuple[Path, str]:
    """A workspace that is a git repository with exactly one commit."""
    repo = root / "workspace"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c", "user.email=t@example.com",
        "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "init",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def _plan_dir(tmp_path: Path, plan_id: str) -> Path:
    plan = tmp_path / "plans" / plan_id
    plan.mkdir(parents=True)
    return plan


def _orchestrator(plan_dir: Path, project_dir: Path) -> VerificationOrchestrator:
    """An orchestrator with only the two directories.

    The preflight touches nothing else: it reads the chain's record,
    asks whether the point's report is there, reads the workspace's
    HEAD, builds the chain's tasks and writes them to ``state.db``.
    """
    orch = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orch.plan_dir = plan_dir
    orch.project_dir = project_dir
    return orch


def _write_chain_record(plan_dir: Path, record: dict) -> None:
    (plan_dir / "verification_root_cause_chain.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8",
    )


def _write_report(
    orch: VerificationOrchestrator, vp_id: str, scope: list,
) -> Path:
    """A delivered root-cause report that declares ``scope``."""
    report = orch._root_cause_report_file(vp_id)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {"paths": list(scope), "root_cause": REPORT_BODY_MARKER},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return report


def _chain_record(
    vp_id: str, head_sha: str, stage: str = _CHAIN_STAGE_ROOT_CAUSE,
) -> dict:
    """A chain record that owns exactly one point."""
    return {
        "version": 1,
        "stage": stage,
        "head_sha": head_sha,
        "vps": {
            vp_id: {
                "stage": stage,
                "round": 1,
                "report_path": f"plans/p1/reports/root-cause-{vp_id}.md",
            },
        },
    }


def _rows(plan_id: str) -> dict:
    """Every ``plan_tasks`` row of the plan, read back from state.db."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        return PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 链欠实现 → 预检派活，不跑验证轮
# ---------------------------------------------------------------------------
def test_preflight_dispatches_implementation(tmp_path):
    """诊断已交付、链还欠实现 → 预检直接把实现派出去。"""
    plan_id = "preflight-impl"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(
        plan_dir, _chain_record(VP_ID, head_sha),
    )
    report_file = _write_report(orch, VP_ID, SCOPE)

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["handled"] is True, (
        f"the chain owes work — the preflight must handle it: {decision!r}"
    )
    assert decision["blocked"] is False
    assert decision["actions"], f"the chain owes an action: {decision!r}"
    assert all(
        action["action"] == _CHAIN_STAGE_IMPLEMENTATION
        for action in decision["actions"]
    ), f"the chain owes the implementation: {decision['actions']!r}"
    assert len(decision["tasks"]) == 1, (
        f"one point, one task: {decision['tasks']!r}"
    )

    task = decision["tasks"][0]
    assert task["files_to_modify"] == SCOPE, (
        f"the task changes exactly what the report declared: {task!r}"
    )
    assert task["source_report"] == str(report_file), (
        f"the task points at the report the chain read: {task!r}"
    )
    # The row landed in state.db — the same write path the round uses.
    assert task["id"] in _rows(plan_id), (
        f"the preflight's task must be written: {sorted(_rows(plan_id))!r}"
    )


def test_preflight_dispatches_without_running_a_round(tmp_path):
    """预检接手时，验证轮**不跑**（``run_full_verification`` 不被调用）。"""
    plan_id = "preflight-no-round"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(plan_dir, _chain_record(VP_ID, head_sha))
    _write_report(orch, VP_ID, SCOPE)

    calls: list = []

    class _Agent:
        def run_full_verification(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("the round must not run")

    orch.verification_agent = _Agent()
    orch.plan_state = _StubPlanState()

    report = orch.start_verification_cycle(round_number=2)

    assert calls == [], "the verification round must not run"
    assert report["root_cause_preflight"]["handled"] is True, (
        f"the report must carry the preflight's decision: {report!r}"
    )


class _StubPlanState:
    """A plan state that records the phase calls it receives."""

    def __init__(self) -> None:
        self.calls: list = []

    def reload(self) -> None:
        self.calls.append("reload")

    def get_current_phase(self) -> str:
        return "verification"

    def transition_to(self, phase: str) -> None:
        self.calls.append(("transition_to", phase))

    def force_set_phase(self, phase: str) -> None:
        self.calls.append(("force_set_phase", phase))

    def verification_failed(self) -> None:
        self.calls.append("verification_failed")


# ---------------------------------------------------------------------------
# 2. 链在人手里 → 预检不派活
# ---------------------------------------------------------------------------
def test_preflight_refuses_blocked_chain(tmp_path):
    """链上的点全在人手里 → 不写 repair、不推进轮次。"""
    plan_id = "preflight-blocked"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(
        plan_dir,
        _chain_record(VP_ID, head_sha, stage=CHAIN_STAGE_BLOCKED_ON_USER),
    )

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["handled"] is True, (
        f"the chain is with a human — the preflight must handle it: {decision!r}"
    )
    assert decision["blocked"] is True
    assert decision["reason"] == BLOCKED_REASON
    assert decision["actions"] == []
    assert decision["tasks"] == []
    assert _rows(plan_id) == {}, "a blocked chain must not write a repair task"


# ---------------------------------------------------------------------------
# 3. HEAD 变了 → 落回正常验证轮
# ---------------------------------------------------------------------------
def test_preflight_stale_head_falls_through(tmp_path):
    """链记着的 HEAD 与当前 HEAD 不一样 → 这条链不作数，走正常验证轮。"""
    plan_id = "preflight-stale"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(plan_dir, _chain_record(VP_ID, "0" * 40))
    _write_report(orch, VP_ID, SCOPE)
    assert head_sha != "0" * 40

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["handled"] is False, (
        f"a stale chain must fall through to the ordinary round: {decision!r}"
    )
    assert decision["tasks"] == []
    assert _rows(plan_id) == {}, "a stale chain must not write a repair task"


def test_preflight_legacy_chain_without_head_sha_falls_through(tmp_path):
    """老记录不知道它描述的是哪一次失败 → 保守，落回正常验证轮。"""
    plan_id = "preflight-legacy"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, _head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    record = _chain_record(VP_ID, "aaa")
    record.pop("head_sha")
    _write_chain_record(plan_dir, record)
    _write_report(orch, VP_ID, SCOPE)

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["handled"] is False
    assert decision["tasks"] == []


# ---------------------------------------------------------------------------
# 4. 空链 → 正常验证轮
# ---------------------------------------------------------------------------
def test_preflight_empty_chain_keeps_normal_round(tmp_path):
    """没有链 → 预检什么都不做，正常验证轮照跑。"""
    plan_id = "preflight-empty"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, _head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["handled"] is False, (
        f"an empty chain is not a chain: {decision!r}"
    )
    assert decision["blocked"] is False
    assert decision["actions"] == []
    assert decision["tasks"] == []


def test_preflight_demoted_chain_falls_through(tmp_path):
    """本次刚 demote 成 blocked → 落回正常轮（那个判据归轮里）。"""
    plan_id = "preflight-demote"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(
        plan_dir,
        _chain_record(VP_ID, head_sha, stage=_CHAIN_STAGE_IMPLEMENTATION),
    )

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["handled"] is False, (
        f"a demotion needs the round's own evidence: {decision!r}"
    )
    assert decision["tasks"] == []


# ---------------------------------------------------------------------------
# 5. 任务带 origin 标记
# ---------------------------------------------------------------------------
def test_preflight_tasks_carry_origin_marker(tmp_path):
    """预检派出的任务一律带 ``origin="root_cause_preflight"``。"""
    plan_id = "preflight-origin"
    plan_dir = _plan_dir(tmp_path, plan_id)
    repo, head_sha = _repo_with_one_commit(tmp_path)
    orch = _orchestrator(plan_dir, repo)
    _write_chain_record(plan_dir, _chain_record(VP_ID, head_sha))
    _write_report(orch, VP_ID, SCOPE)

    decision = orch._root_cause_preflight(round_number=2)

    assert decision["tasks"], f"the preflight must dispatch: {decision!r}"
    for task in decision["tasks"]:
        assert task.get("origin") == PREFLIGHT_ORIGIN, (
            f"every task says where it came from: {task!r}"
        )
    # The task references the report — it does not carry its body.
    payload = json.dumps(decision["tasks"], ensure_ascii=False)
    assert REPORT_BODY_MARKER not in payload, (
        "the report's body must not be inlined into the task"
    )
