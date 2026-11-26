"""快速通道的 plan 也能用同一个生成入口拿到验证计划书。

背景
----
PRD 决策点 2：验证计划书是**任务清单生成之后**的一个可选附加动作，正式流程
与快速通道共用同一个 generate 入口。快速通道（``POST /api/plan/import``）只
产出 ``tasks.json``，**没有 PRD 审核面** —— 所以门 2（PRD 审核）必须分四态，
并在端点上落成三种结果：

* 审核面**不存在**（``absent``）→ 这道门不适用，放行；
* 审核面存在、读得懂、但还有没解决的决策点（``pending``）→ 409
  ``prd_review_incomplete``，载荷说清「读得懂但没审完」
  （``review_readable=true`` + 非空 ``pending``）；
* 审核面存在、读得懂、且都审完了（``resolved``）→ 放行；
* 审核面存在但**读不出来**（``unreadable``）→ 409 ``prd_review_incomplete`` +
  ``review_readable=false``，**不得**降级成「不适用」——那会变成绕过审阅的后门。

四态由四个**互斥且形状不同**的目录构造覆盖：「文件是否存在」本身就是判据的
一部分，同一个 fixture 加参数覆盖不到。

测试数据
--------
模型是替身：``server.create_coding_tool`` 被换成脚本化工具，所以本文件不碰
任何 provider，也不联网。快速通道那条用例走的是**真实的**
``POST /api/plan/import``（模型同样被替身顶掉），所以它断言的是生产链路，
而不是直接造一个目录。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from routes import phases  # noqa: E402
from tests.fixtures.verification_mode import PR_NUMBER, PR_REF, build_plan_doc  # noqa: E402

client = TestClient(server.app)

#: 验证计划书的文件名（相对路径）。
PLAN_DOC = "verification-plan.md"
#: 任务清单的文件名（相对路径）。
TASKS = "tasks.json"
#: PRD 审核面的两个文件。
PRD = "prd.json"
REVIEW = "review.json"


# ---------------------------------------------------------------------------
# 脚本化的模型替身
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """既能当任务生成器、又能当计划书生成工具的替身。

    * ``query_json`` —— 任务生成器的合成调用，返回任务清单；
    * ``query`` —— 任务生成器的自审调用，回一份「没问题」的改写；
    * ``__call__`` —— 计划书生成工具的调用方式，返回文档原文。

    ``prompts`` 留着被问过的提示词：「被拒绝的请求不许调用模型」这条契约就是
    拿它来断的。
    """

    def __init__(self, document: str = "", tasks: Optional[Dict[str, Any]] = None):
        self.document = document
        self.tasks = tasks
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.document

    def query_json(self, prompt: str, system_instruction: str = "") -> Dict[str, Any]:
        self.prompts.append(prompt)
        return self.tasks or {}

    def query(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {"findings": [], "fixed_content": self.tasks or {}},
            ensure_ascii=False,
        )


@pytest.fixture(autouse=True)
def no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED 探针会真的去跑生成出来的测试命令。"""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """基线里记着的那个仓库（生成端点不校验它，但基线行要写它的路径）。"""
    repo = tmp_path / "upstream"
    repo.mkdir()
    return repo


# ---------------------------------------------------------------------------
# 快速通道的导入
# ---------------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """一个真实存在的检出目录 —— 导入的工作区闸门要它存在。"""
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    adapter = path / "scripts" / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)
    return path


def _tasks_payload() -> Dict[str, Any]:
    """一份模型会给出、而且能通过导入闸门的任务清单。"""
    return {
        "requirement": "add a daily grouped bar chart to the reporting view",
        "tasks": [
            {
                "id": "T1",
                "title": "add the chart renderer",
                "description": "## 背景\nthe reporting view has no chart",
                "files_to_modify": ["backend/chart.py"],
                "test_command": "pytest -q backend/tests/test_chart.py",
            },
            {
                "id": "T2",
                "title": "wire the renderer into the view",
                "description": "## 目标\nmount the renderer",
                "files_to_modify": ["backend/reporting_view.py"],
                "test_command": "pytest -q",
                "depends_on": ["T1"],
            },
        ],
    }


def _install(monkeypatch: pytest.MonkeyPatch, tool: _ScriptedTool) -> _ScriptedTool:
    """把 ``server.create_coding_tool`` 指向脚本化替身。"""
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prd_payload(status: str) -> Dict[str, Any]:
    """一份只有一条决策点的 PRD 及其审核状态。"""
    return {
        "prd": {"title": "fixture PRD", "decision_points": [{"index": 0}]},
        "review": {
            "items": [{"index": 0, "status": status, "note": ""}],
            "total": 1,
        },
    }


#: 审核面形态 → 审核条目里真正写下的状态。
#:
#: 「已解决」在审核面里的取值是 ``accepted`` / ``skipped``（这两个才算已解决），
#: 所以「已审完」这一态必须写成 ``accepted`` —— 写成别的字符串（比如
#: ``resolved``）会被当成「还没审」。
_REVIEW_STATUS_BY_FACE = {
    "pending": "pending",
    "resolved": "accepted",
}


@pytest.fixture
def make_plan(tmp_path: Path, upstream_repo: Path, plan_sqlite_seeder):
    """种一个 plan 目录，并写好它那一行路由状态。

    ``prd_face`` 决定审核面长什么样，四种形态互斥且形状不同：

    * ``None`` —— 盘上**没有**审核面（快速通道的 plan 就是这种）；
    * ``"pending"`` —— 审核面存在且读得懂，但决策点还没审完；
    * ``"resolved"`` —— 审核面存在且读得懂，且决策点都审完了；
    * ``"corrupt"`` —— 审核面存在，但审核文件**读不出来**（字节非法）。
    """
    counter = {"n": 0}

    def _make(
        *,
        tasks: bool = False,
        doc: bool = False,
        prd_face: Optional[str] = None,
        phase: str = "queued",
        with_baseline: bool = True,
    ):
        counter["n"] += 1
        plan_id = f"vp-fastpath-{counter['n']}"
        plan_dir = server.PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True)

        if tasks:
            _write_json(
                plan_dir / TASKS,
                {"tasks": [{"id": "1", "title": "共存 fixture 任务"}]},
            )
        if doc:
            (plan_dir / PLAN_DOC).write_text(
                build_plan_doc(upstream_repo, pr_number=PR_NUMBER, ref=PR_REF),
                encoding="utf-8",
            )
        if prd_face is not None:
            payload = _prd_payload(
                "pending"
                if prd_face == "corrupt"
                else _REVIEW_STATUS_BY_FACE[prd_face]
            )
            _write_json(plan_dir / PRD, payload["prd"])
            if prd_face == "corrupt":
                # 审核面在盘上，但审核文件读不出来 —— 第四态。
                (plan_dir / REVIEW).write_bytes(b"{not json at all")
            else:
                _write_json(plan_dir / REVIEW, payload["review"])

        verification: Dict[str, Any] = {}
        if with_baseline:
            verification["baseline"] = {
                "repo_dir": str(upstream_repo),
                "ref": PR_REF,
                "commit_sha": "0" * 40,
                "pr_number": PR_NUMBER,
            }
        plan_sqlite_seeder(
            plan_dir,
            plan_id,
            phase=phase,
            verification=verification,
        )
        return plan_id, plan_dir

    return _make


def _generate(plan_id: str):
    return client.post(
        f"/api/verification-plan/{plan_id}/generate",
        json={"feedback": None},
    )


# ---------------------------------------------------------------------------
# 1. 快速通道的 plan：没有审核面 → 门 2 不适用 → 200
# ---------------------------------------------------------------------------


def test_fastpath_plan_can_generate(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    upstream_repo: Path,
    plan_sqlite_seeder,
) -> None:
    """真实导入的 plan 也能生成计划书，且相位不变。

    走的是生产的 ``POST /api/plan/import``：导入出来的目录里只有任务清单，
    **没有** ``prd.json`` / ``review.json`` —— 审核面根本不存在，所以门 2
    不适用。生成是**附加动作**：相位保持 ``queued``，任务清单一个字节都不动。
    """
    tool = _install(
        monkeypatch,
        _ScriptedTool(
            document=build_plan_doc(
                upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
            ),
            tasks=_tasks_payload(),
        ),
    )

    imported = client.post(
        "/api/plan/import",
        json={
            "project_dir": str(checkout),
            "content": "把 CSV 报表加上按天分组的柱状图",
            "title": "Fast lane reachability",
        },
    )
    assert imported.status_code == 200, imported.text
    plan_id = imported.json()["plan_id"]
    plan_dir = server.PLANS_DIR / plan_id

    # 审核面确实不存在 —— 这条用例的前提。
    assert not (plan_dir / PRD).exists()
    assert not (plan_dir / REVIEW).exists()

    # 基线是生成端点唯一的输入源，而导入不写它：这里补上（生产里由操作者
    # 冻结基线时写入同一处）。
    plan_sqlite_seeder(
        plan_dir,
        plan_id,
        phase="queued",
        verification={
            "baseline": {
                "repo_dir": str(upstream_repo),
                "ref": PR_REF,
                "commit_sha": "0" * 40,
                "pr_number": PR_NUMBER,
            }
        },
    )

    tasks_before = (plan_dir / TASKS).read_bytes()

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan_id
    assert body["document_path"] == PLAN_DOC
    assert body["current_phase"] == "queued", (
        "生成是附加动作，开发分支的相位不许被改；实际是 %r"
        % body["current_phase"]
    )
    assert (plan_dir / PLAN_DOC).exists(), "计划书必须落盘"
    assert (plan_dir / TASKS).read_bytes() == tasks_before, (
        "生成计划书不许动任务清单"
    )
    assert tool.prompts, "模型被问过了（文档是它写的）"


# ---------------------------------------------------------------------------
# 2. 审核面读得懂但没审完 → 409，且说清「读得懂但没审完」
# ---------------------------------------------------------------------------


def test_formal_flow_prd_pending_still_409(
    make_plan,
    monkeypatch: pytest.MonkeyPatch,
    upstream_repo: Path,
) -> None:
    """审核面存在、读得懂、但还有没解决的决策点 → 409。

    载荷必须区分「读得懂但没审完」：``review_readable=true`` 与非空的
    ``pending``。模型一次都没被问过 —— 没审完的 PRD 不值得花一次模型调用。
    """
    plan_id, plan_dir = make_plan(prd_face="pending")
    tool = _install(monkeypatch, _ScriptedTool(document=""))

    resp = _generate(plan_id)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "prd_review_incomplete"
    assert body["review_readable"] is True, (
        "读得懂就必须说读得懂；实际载荷：%r" % body
    )
    assert body["pending"], "还有没解决的决策点时 pending 必须非空"
    assert "artifact_conflict" not in resp.text, (
        "产物冲突那道门已经删掉了；实际响应：%s" % resp.text
    )
    assert not (plan_dir / PLAN_DOC).exists(), "被拒绝的请求不许写盘"
    assert tool.prompts == []


# ---------------------------------------------------------------------------
# 3. 审核面存在但读不出来 → 409，且说清「读不出来」
# ---------------------------------------------------------------------------


def test_formal_flow_prd_unreadable_still_409(
    make_plan,
    monkeypatch: pytest.MonkeyPatch,
    upstream_repo: Path,
) -> None:
    """审核面在盘上但读不出来 → 409 ``prd_review_incomplete``。

    这一态**不得**降级成「不适用」：一份损坏的审核文件如果被当成「没有审核
    面」，就成了绕过审阅的后门。载荷说清 ``review_readable=false``。
    """
    plan_id, plan_dir = make_plan(prd_face="corrupt")
    tool = _install(monkeypatch, _ScriptedTool(document=""))

    resp = _generate(plan_id)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "prd_review_incomplete"
    assert body["review_readable"] is False, (
        "读不出来就必须说读不出来；实际载荷：%r" % body
    )
    assert body["pending"] == [], (
        "读不出来的时候没有「还有几条没审」这回事；实际载荷：%r" % body
    )
    assert "artifact_conflict" not in resp.text, (
        "产物冲突那道门已经删掉了；实际响应：%s" % resp.text
    )
    assert not (plan_dir / PLAN_DOC).exists(), "被拒绝的请求不许写盘"
    assert tool.prompts == []


# ---------------------------------------------------------------------------
# 4. 审核面存在且都审完了 → 放行（resolved 与 absent 是两回事）
# ---------------------------------------------------------------------------


def test_formal_flow_prd_resolved_is_allowed(
    make_plan,
    monkeypatch: pytest.MonkeyPatch,
    upstream_repo: Path,
) -> None:
    """审核面存在、读得懂、且每条决策点都 accept 了 → 200。

    ``resolved`` 这一态必须与 ``absent`` 分得开：``absent`` 是「没有审核面」，
    ``resolved`` 是「审核面在，而且审完了」。两者都放行，但只有后者说明审阅
    真的走完了 —— 所以状态本身必须能区分。
    """
    plan_id, plan_dir = make_plan(tasks=True, prd_face="resolved")
    tool = _install(
        monkeypatch,
        _ScriptedTool(
            document=build_plan_doc(
                upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
            )
        ),
    )

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan_id
    assert body["document_path"] == PLAN_DOC
    assert body["current_phase"] == "queued", (
        "生成是附加动作，相位不许被改；实际是 %r" % body["current_phase"]
    )
    assert (plan_dir / PLAN_DOC).exists(), "计划书必须落盘"
    assert tool.prompts, "模型被问过了（文档是它写的）"


# ---------------------------------------------------------------------------
# 5. 共存 plan（任务清单 + 审核面未解决）→ 拒的是 PRD 门，不是产物冲突
# ---------------------------------------------------------------------------


def test_coexisting_plan_with_pending_prd_is_rejected_by_the_prd_gate(
    make_plan,
    monkeypatch: pytest.MonkeyPatch,
    upstream_repo: Path,
) -> None:
    """目录里已有任务清单时，拒的仍然是 PRD 门。

    「产物冲突」那道门已经删掉了（共存是合法的）：所以共存 plan 上没审完的
    PRD 必须被报成 ``prd_review_incomplete``，而不是 ``artifact_conflict`` ——
    后者会让操作者以为要去裁决两个产物，而真正没做完的是审 PRD。
    """
    plan_id, plan_dir = make_plan(tasks=True, doc=True, prd_face="pending")
    tool = _install(monkeypatch, _ScriptedTool(document=""))
    doc_before = (plan_dir / PLAN_DOC).read_bytes()

    resp = _generate(plan_id)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "prd_review_incomplete"
    assert body["review_readable"] is True
    assert len(body["pending"]) >= 1
    assert "artifact_conflict" not in resp.text, (
        "产物冲突那道门已经删掉了；实际响应：%s" % resp.text
    )
    assert (plan_dir / PLAN_DOC).read_bytes() == doc_before, (
        "被拒绝的请求不许改盘上的任何东西"
    )
    assert tool.prompts == []


# ---------------------------------------------------------------------------
# 6. 门 2 的四态：辅助函数层面也必须能区分
# ---------------------------------------------------------------------------


def test_prd_gate_distinguishes_absent_from_corrupt(make_plan) -> None:
    """四态的返回值互不相同，且与调用点的映射一致。

    这一条是给**辅助函数**的：只有 API 层的用例，「读不懂」与「没有」在
    载荷上可能长得一样（都是 409），函数层的四态才是判据本身。

    契约：``(state, pending)``，``state`` 取值互斥 —— ``absent``（没有审核面）
    / ``pending``（读得懂但没审完）/ ``resolved``（读得懂且审完了）/
    ``unreadable``（读不出来）。其中 ``absent`` 与 ``unreadable`` 必须可区分：
    「没有审核面」这道门不适用，「审核面读不出来」按未解决处理 —— 损坏不得
    降级成「不适用」。
    """
    _, absent_dir = make_plan(tasks=True)
    _, pending_dir = make_plan(tasks=True, prd_face="pending")
    _, resolved_dir = make_plan(tasks=True, prd_face="resolved")
    _, corrupt_dir = make_plan(tasks=True, prd_face="corrupt")

    absent = phases._verification_prd_state(absent_dir)
    pending = phases._verification_prd_state(pending_dir)
    resolved = phases._verification_prd_state(resolved_dir)
    corrupt = phases._verification_prd_state(corrupt_dir)

    assert absent[0] == phases.PRD_GATE_ABSENT
    assert pending[0] == phases.PRD_GATE_PENDING
    assert resolved[0] == phases.PRD_GATE_RESOLVED
    assert corrupt[0] == phases.PRD_GATE_UNREADABLE

    assert absent[0] != pending[0] != corrupt[0] != resolved[0]
    assert absent[1] == []
    assert pending[1], "没审完的那一态必须给出没解决的决策点"
    assert resolved[1] == [], "审完了的那一态没有没解决的决策点"
    assert corrupt[1] == [], "读不出来的时候没有「还有几条没审」这回事"

    # 返回值就是一个二元组：既能按下标取，也能解包。
    state, pending_indices = pending
    assert state == phases.PRD_GATE_PENDING
    assert pending_indices
