"""计划书生成端点：``POST /api/verification-plan/{plan_id}/generate``。

背景
----
验证分支在 PRD 审完之后产出的是**计划书**（``verification-plan.md``），不是
任务清单（``tasks.json``）。这个端点是那条路上的唯一入口，它得自己守住三道门：

* **产物不冲突** —— 目录里已经有 ``tasks.json`` 时，这个 plan 要走的是开发
  分支。框架不替人猜意图，也不替人删文件：409 ``artifact_conflict`` 把两个
  产物的**相对路径**交回去，由操作者裁决。
* **PRD 已审完** —— 还有 pending 条目时 409 ``prd_review_incomplete``：没审完
  的 PRD 不值得花一次模型调用。
* **文档合法** —— 模型给的文档不满足十章节契约时 422，且**不落盘**：非法文档
  留在磁盘上只会变成下一个读它的人踩的坑。

契约之外还钉住两条：计划不存在 → 404；PRD 整条 skip 也算审完（验证分支的
最小 PRD 允许 skip —— 这条边正是任务 15 的专用入口存在的理由）。

基线从哪来
----------
基线**只**从计划状态里读（``verification.baseline``）。这个端点没有基线参数：
请求体里塞 ``baseline`` / ``repo_dir`` / ``ref`` 都不改变任何事（架构决策点 3：
生成与启动接口都不接受基线参数）。

测试数据
--------
模型是替身：``server.create_coding_tool`` 被换成脚本化工具，每次被问都返回同一
份文档。所以本文件不碰任何 provider，也不联网。
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
from plan_state import PlanState  # noqa: E402
from tests.fixtures.verification_mode import build_plan_doc  # noqa: E402

client = TestClient(server.app)

#: 验证分支的产物：计划书（相对路径）。
PLAN_DOC = "verification-plan.md"
#: 开发分支的产物：任务清单（相对路径）。
TASKS = "tasks.json"

#: 生成之后计划停在这里等人审。
REVIEW_PHASE = "verification_plan_review"
#: 生成之前的阶段：PRD 审完，还没生成计划书。
PRD_REVIEW_PHASE = "prd_review"

#: 基线里的 ref 与 PR 号（与样本文档的基线行一致）。
REF = "refs/pull/7/head"
PR_NUMBER = 7


# ---------------------------------------------------------------------------
# 脚本化的生成工具
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """生成工具的替身：每次被问都返回同一份文档。

    ``prompts`` 留着被问过的提示词 —— 「被拒绝的请求不许调用模型」这条契约
    就是拿它来断的。
    """

    def __init__(self, document: str = "") -> None:
        self.document = document
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.document


@pytest.fixture
def scripted_tool(monkeypatch: pytest.MonkeyPatch) -> _ScriptedTool:
    """把 ``server.create_coding_tool`` 换成脚本化替身。"""
    tool = _ScriptedTool()
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)
    return tool


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


def _write_routing_row(
    plan_id: str,
    *,
    phase: str,
    baseline: Optional[Dict[str, Any]],
) -> None:
    """写一行 ``plan_routing``：阶段 + 记录下来的基线。

    基线冻在 routing 行的 ``verification`` 段里 —— 与生产路径同一条，所以
    读侧找到的就是写入侧放进去的那份。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    verification: Dict[str, Any] = {
        "status": "pending",
        "round": 0,
        "max_rounds": 3,
        "stop_reason": None,
    }
    if baseline is not None:
        verification["baseline"] = baseline

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


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """基线里记着的那个仓库。

    生成端点不校验仓库（那是 ``/start`` 的门禁），这里只需要一个**存在
    的目录** —— 基线行里要写它的路径。
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    return repo


@pytest.fixture
def make_plan(tmp_path: Path, upstream_repo: Path):
    """种一个「PRD 审完、还没生成计划书」的计划。"""
    counter = {"n": 0}

    def _make(
        *,
        prd_status: str = "accepted",
        with_tasks: bool = False,
        with_baseline: bool = True,
        phase: str = PRD_REVIEW_PHASE,
    ) -> Tuple[str, Path]:
        counter["n"] += 1
        plan_id = f"vp-gen-{counter['n']}"
        plan_dir = server.PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True)

        (plan_dir / "prd.json").write_text(
            json.dumps(
                {"decision_points": [{"index": 0, "title": "凭据校验方式"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (plan_dir / "review.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "index": 0,
                            "title": "凭据校验方式",
                            "status": prd_status,
                            "note": "理由",
                        }
                    ],
                    "total": 1,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if with_tasks:
            (plan_dir / TASKS).write_text('{"tasks": []}', encoding="utf-8")

        baseline: Optional[Dict[str, Any]] = {
            "repo_dir": str(upstream_repo),
            "ref": REF,
            "commit_sha": "0" * 40,
            "pr_number": PR_NUMBER,
        }
        _write_routing_row(
            plan_id,
            phase=phase,
            baseline=baseline if with_baseline else None,
        )
        return plan_id, plan_dir

    return _make


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


def _valid_doc(repo_dir: Path) -> str:
    """一份满足十章节契约的计划书。"""
    return build_plan_doc(repo_dir, pr_number=PR_NUMBER, ref=REF)


def _invalid_doc(valid: str) -> str:
    """砍掉「风险面」这一节 —— 十章节契约少一节。"""
    head, marker, _ = valid.partition("## 风险面")
    assert marker, "样本文档里没有「风险面」这一节"
    return head


def _phase_of(plan_dir: Path) -> str:
    """计划当前的阶段（走生产读路径）。"""
    return PlanState(plan_dir).get_current_phase()


def _baseline_of(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """计划状态里记录下来的基线。"""
    verification = PlanState(plan_dir).get_state().get("verification") or {}
    recorded = verification.get("baseline")
    return recorded if isinstance(recorded, dict) else None


# ---------------------------------------------------------------------------
# 1. 合法上下文 → 200，落盘，进「计划书待审」
# ---------------------------------------------------------------------------


def test_generate_creates_doc_and_enters_review(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """合法上下文 → 200，文档落盘，阶段推进到 ``verification_plan_review``。"""
    plan_id, plan_dir = make_plan()
    scripted_tool.document = _valid_doc(upstream_repo)

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan_id
    assert body["document_path"] == PLAN_DOC, (
        "文档路径必须是**相对路径**；实际是 %r" % body["document_path"]
    )
    assert body["current_phase"] == REVIEW_PHASE

    doc_path = plan_dir / PLAN_DOC
    assert doc_path.exists(), "计划书必须落盘"
    assert doc_path.read_text(encoding="utf-8") == scripted_tool.document
    assert _phase_of(plan_dir) == REVIEW_PHASE


# ---------------------------------------------------------------------------
# 2. 产物冲突 → 409，不删文件、不写盘、不迁移阶段
# ---------------------------------------------------------------------------


def test_generate_rejects_tasks_json_conflict(
    make_plan,
    scripted_tool: _ScriptedTool,
) -> None:
    """目录里已有 ``tasks.json`` → 409 ``artifact_conflict``。

    响应里给出**两个**产物的相对路径，文件一个都不删，mtime 不变；被拒绝的
    请求不写盘、不迁移阶段、也不调用模型。
    """
    plan_id, plan_dir = make_plan(with_tasks=True)
    tasks_path = plan_dir / TASKS
    mtime_before = tasks_path.stat().st_mtime_ns

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "artifact_conflict"
    assert body["artifacts"] == [TASKS, PLAN_DOC], (
        "响应里必须给出两个产物的相对路径；实际是 %r" % (body.get("artifacts"),)
    )
    assert TASKS in body["existing_artifacts"]
    assert str(plan_dir) not in resp.text, (
        "响应不许泄露本机绝对路径；实际响应：%s" % resp.text
    )

    # 文件没被删，mtime 没变。
    assert tasks_path.exists()
    assert tasks_path.stat().st_mtime_ns == mtime_before

    # 没写盘、没迁移阶段、没调用模型。
    assert not (plan_dir / PLAN_DOC).exists()
    assert _phase_of(plan_dir) == PRD_REVIEW_PHASE
    assert scripted_tool.prompts == []


# ---------------------------------------------------------------------------
# 3. PRD 没审完 → 409
# ---------------------------------------------------------------------------


def test_generate_rejects_pending_prd(
    make_plan,
    scripted_tool: _ScriptedTool,
) -> None:
    """PRD 还有 pending 条目 → 409 ``prd_review_incomplete``。

    没审完的 PRD 不值得花一次模型调用：模型一次都没被问过。
    """
    plan_id, plan_dir = make_plan(prd_status="pending")

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "prd_review_incomplete"

    assert not (plan_dir / PLAN_DOC).exists()
    assert _phase_of(plan_dir) == PRD_REVIEW_PHASE
    assert scripted_tool.prompts == []


# ---------------------------------------------------------------------------
# 4. 文档非法 → 422，且不落盘
# ---------------------------------------------------------------------------


def test_generate_invalid_doc_returns_422_without_write(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """模型给的文档缺一节 → 422，磁盘上不留这份文档。"""
    plan_id, plan_dir = make_plan()
    scripted_tool.document = _invalid_doc(_valid_doc(upstream_repo))

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "verification_plan_invalid"
    assert body["errors"], "必须逐条给出违规"

    assert not (plan_dir / PLAN_DOC).exists(), "非法文档不许落盘"
    assert _phase_of(plan_dir) == PRD_REVIEW_PHASE


# ---------------------------------------------------------------------------
# 5. 计划状态里没有记录基线 → 422
# ---------------------------------------------------------------------------


def test_generate_without_recorded_baseline_returns_422(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """计划状态里没有记录基线 → 422，且不写盘。

    基线只有**一个**来源：计划状态。这个端点没有基线参数，所以没有记录时
    没有第二条路可走 —— 只能拒绝，不能就地编一个。
    """
    plan_id, plan_dir = make_plan(with_baseline=False)
    scripted_tool.document = _valid_doc(upstream_repo)

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "baseline_missing"
    assert not (plan_dir / PLAN_DOC).exists()
    assert _phase_of(plan_dir) == PRD_REVIEW_PHASE
    assert scripted_tool.prompts == []


# ---------------------------------------------------------------------------
# 6. 计划不存在 → 404
# ---------------------------------------------------------------------------


def test_generate_unknown_plan_returns_404() -> None:
    """计划不存在 → 404。"""
    resp = client.post(
        "/api/verification-plan/there-is-no-such-plan/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 7. PRD 整条 skip 也算审完（验证分支的最小 PRD 允许 skip）
# ---------------------------------------------------------------------------


def test_generate_accepts_fully_skipped_prd(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """PRD 的决策点被整条 skip → 照样能生成计划书。

    负控的对面：验证分支的最小 PRD 允许整条 skip，所以「skip 也算数」这条
    口径必须在这个入口上生效 —— 否则任务 15 的专用入口永远走不到。
    """
    plan_id, plan_dir = make_plan(prd_status="skipped")
    scripted_tool.document = _valid_doc(upstream_repo)

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["current_phase"] == REVIEW_PHASE
    assert (plan_dir / PLAN_DOC).exists()


# ---------------------------------------------------------------------------
# 8. 「没有基线参数」这条负向契约
# ---------------------------------------------------------------------------


def test_injected_baseline_changes_nothing(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """请求里塞基线：不改响应，也不改计划状态里记录的基线。

    基线只有**一个**来源：计划状态。端点签名与请求模型里都没有基线字段，
    所以注入的 ``baseline`` / ``repo_dir`` / ``ref`` 都会被丢掉。
    """
    plan_id, plan_dir = make_plan()
    scripted_tool.document = _valid_doc(upstream_repo)

    resp = client.post(
        f"/api/verification-plan/{plan_id}/generate"
        "?baseline=repo%3D%2Fnonexistent%2Finjected-repo&ref=evil&commit_sha=deadbeef",
        json={
            "feedback": None,
            "baseline": {"repo": "/nonexistent/injected-repo", "ref": "evil", "pr": 1},
            "repo_dir": "/nonexistent/injected-repo",
            "ref": "evil",
        },
    )

    assert resp.status_code == 200, resp.text

    # 记录在计划状态里的基线没被动过。
    recorded = _baseline_of(plan_dir)
    assert recorded is not None
    assert recorded["repo_dir"] == str(upstream_repo)
    assert recorded["ref"] == REF
    assert recorded["pr_number"] == PR_NUMBER

    # 落盘的文档里是**记录的那条**基线，不是注入的那条。
    doc_text = (plan_dir / PLAN_DOC).read_text(encoding="utf-8")
    assert f"baseline: repo={upstream_repo} ref={REF} pr={PR_NUMBER}" in doc_text
    assert "/nonexistent/injected-repo" not in doc_text
