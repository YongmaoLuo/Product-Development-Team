"""共存 plan 的生成入口：三道门的分流结论要逐条可见。

背景
----
PRD 验收①与架构决策点 2 把 ``POST /api/verification-plan/{id}/generate``
拆成三道门 + 一道基线门：

* **门 1（产物冲突）** —— **已经删掉**：任务清单与计划书共存是合法的
  （PRD 决策点 2：计划书是任务清单之后的一个可选附加动作）。
* **门 2（PRD 审核面）** —— 三态判定：盘上根本没有审核面 → 这道门**不适用**，
  放行；读得懂但还有没解决的决策点 → 409 ``prd_review_incomplete``；读不出来
  → 409 ``prd_review_incomplete`` + ``review_readable=false``。
* **门 3（相位门）与相位推进** —— **只属于纯验证分支**：开发分支生成计划书
  是附加动作，不改它的相位。
* **基线门** —— **两条分支都保留**：基线是计划书的内容契约（文档里的
  ``baseline:`` 行必须与计划状态里记录的那份一致），缺基线 → 422
  ``baseline_missing``，且**不落盘**。

本文件钉住的是**共存 plan** 这条路径：目录里同时有 ``tasks.json`` 与
``verification-plan.md``，所以「分支判定」与「审核面判定」必须同时给出正确
答案。请求要一路穿过三道门走到基线门，而三道门各自的结论都要能从响应里看
出来 —— 没被哪道门拦，载荷里就不该出现那道门的错误码。

为什么单独一个文件
------------------
``test_verification_plan_generate.py`` 钉的是纯验证分支（PRD 审完之后那条
路），``test_verification_plan_generate_fastpath.py`` 钉的是快速通道导入的
plan（只有任务清单、没有审核面）。**共存**是第三种形状：两类产物都在盘上，
少一条断言就多一种走错分支的方式漏网。

测试数据
--------
模型是替身（``server.create_coding_tool`` 被换成脚本化工具），所以本文件不碰
任何 provider，也不联网。基线通过 ``plan_sqlite_seeder`` 写进 ``plan_routing``
的 ``verification.baseline`` —— 与生产路径同一条，读侧找到的就是写入侧放进去
的那份。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from plan_state import PlanState  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    PR_NUMBER,
    PR_REF,
    build_plan_doc,
)

client = TestClient(server.app)

#: 验证计划书的文件名（相对路径）。
PLAN_DOC = "verification-plan.md"
#: 任务清单的文件名（相对路径）。
TASKS = "tasks.json"


# ---------------------------------------------------------------------------
# 脚本化的模型替身
# ---------------------------------------------------------------------------


class _ScriptedTool:
    """计划书生成工具的替身：问一次，返回一份文档。

    ``prompts`` 留着被问过的提示词：「被拒绝的请求不许调用模型」这条契约就
    是拿它来断的。
    """

    def __init__(self, document: str = ""):
        self.document = document
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.document


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


@pytest.fixture
def scripted_tool(monkeypatch: pytest.MonkeyPatch) -> _ScriptedTool:
    """把 ``server.create_coding_tool`` 换成脚本化替身。"""
    tool = _ScriptedTool()
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)
    return tool


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def make_plan(tmp_path: Path, upstream_repo: Path, plan_sqlite_seeder):
    """种一个 plan 目录，并写好它那一行路由状态。

    两个产物各自独立：``tasks=True`` 放任务清单（开发分支的标记），
    ``doc=True`` 放计划书。**两个都放**就是「共存 plan」——
    ``plan_artifacts.detect`` 按「任务优先」把它归成开发分支。

    ``prd=True`` 额外放一份**已解决**的审核面（``prd.json`` + ``review.json``，
    唯一那条决策点 ``accepted``）。审核面缺席时门 2 不适用（放行），审核面
    在而没解决时门 2 拦人（409）—— 所以「纯验证分支要推进到计划书待审」这条
    路径必须给一份**审完的**审核面：相位边
    :meth:`plan_state.PlanState.start_verification_plan_generation` 要求 PRD 已解决。
    """
    counter = {"n": 0}

    def _make(
        *,
        tasks: bool = False,
        doc: bool = False,
        phase: str = "queued",
        with_baseline: bool = True,
        prd: bool = False,
    ) -> Tuple[str, Path]:
        counter["n"] += 1
        plan_id = f"vp-coexist-{counter['n']}"
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
        if prd:
            _write_json(
                plan_dir / "prd.json",
                {"decision_points": [{"index": 0, "title": "凭据校验方式"}]},
            )
            _write_json(
                plan_dir / "review.json",
                {
                    "items": [
                        {
                            "index": 0,
                            "title": "凭据校验方式",
                            "status": "accepted",
                            "note": "理由",
                        }
                    ],
                    "total": 1,
                },
            )

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


def _phase_of(plan_dir: Path) -> str:
    """计划当前的阶段（走生产读路径）。"""
    return PlanState(plan_dir).get_current_phase()


def _assert_payload_has_no_local_path(text: str, *paths: Path) -> None:
    """响应载荷里不许出现本机绝对路径。

    扫四样东西：

    * ``paths`` 里点名的目录（本机的计划目录、基线仓库目录）—— 逐字比对；
    * ``/Users/`` 与 ``/home/`` 两个**用户目录字面量** —— 抓「回显了别的绝对
      路径」这种逐字比对抓不到的漏法。

    为什么**不**写死 ``/tmp/``：macOS 上 pytest 的 ``tmp_path`` 落在
    ``/private/var/folders/...`` 下，写死 ``/tmp/`` 会得到一条恒真断言 ——
    恒真的断言等于没钉。
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
# 1. 共存 plan 缺基线 → 停在基线门（422），三道门的结论逐条可见
# ---------------------------------------------------------------------------


def test_coexist_plan_without_baseline_stops_at_the_baseline_gate(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """共存 plan（无审核面、未记录基线）→ 422 ``baseline_missing``。

    这个 plan 的形状是「两类产物都在盘上」：``tasks.json`` 让它落在开发分支，
    盘上又没有 ``prd.json`` / ``review.json``（所以门 2 不适用），开发分支
    又不受门 3 拦（相位门只属于纯验证分支）—— 于是请求一路走到基线门。

    三道门的结论因此要逐条可见：**没被哪道门拦，载荷里就不该出现那道门的
    错误码** —— ``artifact_conflict``（门 1 已删除）、``prd_review_incomplete``
    （门 2 不适用）、``stage_mismatch``（门 3 不拦开发分支）三个都不该出现。

    盘上也不能有任何变化：计划书已经在那儿了，被拒绝的请求不许改它，更不许
    删它；模型一次都不该被问过。
    """
    plan_id, plan_dir = make_plan(tasks=True, doc=True, with_baseline=False)
    doc_before = (plan_dir / PLAN_DOC).read_bytes()
    tasks_before = (plan_dir / TASKS).read_bytes()

    resp = _generate(plan_id)

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "baseline_missing", (
        "共存 plan 缺基线时要停在基线门上；实际载荷：%r" % body
    )
    assert not any("path" in str(key) for key in body), (
        "错误载荷里不许出现路径字段；实际键：%r" % sorted(body)
    )
    _assert_payload_has_no_local_path(resp.text, plan_dir, upstream_repo)
    assert "artifact_conflict" not in resp.text, (
        "门 1（产物冲突）已经删掉了，共存是合法的；实际响应：%s" % resp.text
    )
    assert "prd_review_incomplete" not in resp.text, (
        "门 2 在没有审核面时不适用，不该拦人；实际响应：%s" % resp.text
    )
    assert "stage_mismatch" not in resp.text, (
        "门 3 只属于纯验证分支，不该拦开发分支；实际响应：%s" % resp.text
    )

    assert (plan_dir / PLAN_DOC).read_bytes() == doc_before, (
        "被拒绝的请求不许改盘上的计划书"
    )
    assert (plan_dir / TASKS).read_bytes() == tasks_before, (
        "被拒绝的请求不许动任务清单"
    )
    assert _phase_of(plan_dir) == "queued", "被拒绝的请求不许改相位"
    assert scripted_tool.prompts == [], "缺基线不值得花一次模型调用"


# ---------------------------------------------------------------------------
# 2. 共存 plan 有基线 → 放行：200，落盘，相位不变
# ---------------------------------------------------------------------------


def test_coexist_plan_with_baseline_generates_and_keeps_the_phase(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """同一个 plan，只把基线补上 → 200，计划书重新落盘，相位原样不变。

    这是上一条的**对照**：两份 fixture 只差一个基线，答案就从 422 变成 200 ——
    所以拦住它的确实是基线门，不是别的门。

    相位不变是门 3 的另一半：相位门与相位推进都只属于纯验证分支，开发分支
    生成计划书是**附加动作**，不改它的相位（PRD 决策点 2）。
    """
    plan_id, plan_dir = make_plan(tasks=True, doc=True, with_baseline=True)
    scripted_tool.document = build_plan_doc(
        upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
    )
    tasks_before = (plan_dir / TASKS).read_bytes()

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan_id
    assert body["document_path"] == PLAN_DOC, (
        "文档路径必须是**相对路径**；实际是 %r" % body["document_path"]
    )
    assert body["current_phase"] == "queued", (
        "开发分支的相位不许因为生成计划书而改变；实际是 %r"
        % body["current_phase"]
    )
    assert "artifact_conflict" not in resp.text, resp.text

    assert (plan_dir / PLAN_DOC).read_text(encoding="utf-8") == (
        scripted_tool.document
    ), "计划书必须落盘"
    assert (plan_dir / TASKS).read_bytes() == tasks_before, (
        "生成计划书不许动任务清单"
    )
    assert _phase_of(plan_dir) == "queued"
    assert scripted_tool.prompts, "模型被问过了（文档是它写的）"


# ---------------------------------------------------------------------------
# 3. 基线门在相位门之前判：两条分支上的结论一致
# ---------------------------------------------------------------------------


def test_baseline_gate_is_decided_before_the_phase_gate(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """纯验证分支：缺基线报基线（422），有基线才轮到相位门（409）。

    基线门对**两条分支**都保留，而且是**先**判的一道：基线是计划书的内容
    契约（文档里的 ``baseline:`` 行必须与计划状态里记录的那份一致），这个
    端点不接受基线参数，所以缺基线时没有第二条路可走。

    两条 fixture 只差一个基线，相位都是「不能生成计划书」的那个相位：

    * 缺基线 → 422 ``baseline_missing``（不是 ``stage_mismatch``）；
    * 有基线 → 409 ``stage_mismatch`` —— 门 3 只属于纯验证分支，它还在。

    顺序反过来也说得通（先报相位），但那样「缺基线」的结论在纯验证分支上就
    永远看不见了：基线门在两条分支上的结论必须一致。
    """
    no_base_id, no_base_dir = make_plan(doc=True, with_baseline=False)
    doc_before = (no_base_dir / PLAN_DOC).read_bytes()

    resp = _generate(no_base_id)

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "baseline_missing", (
        "基线门先判：缺基线时先报基线；实际载荷：%r" % body
    )
    assert not any("path" in str(key) for key in body), (
        "错误载荷里不许出现路径字段；实际键：%r" % sorted(body)
    )
    _assert_payload_has_no_local_path(resp.text, no_base_dir, upstream_repo)
    assert "stage_mismatch" not in resp.text, (
        "基线门在相位门之前判；实际响应：%s" % resp.text
    )
    assert (no_base_dir / PLAN_DOC).read_bytes() == doc_before, (
        "被拒绝的请求不许改盘上的计划书"
    )
    assert not scripted_tool.prompts, "缺基线不值得花一次模型调用"

    # 对照：同一个相位，补上基线之后就轮到相位门了 —— 门 3 只属于纯验证分支。
    with_base_id, with_base_dir = make_plan(doc=True, with_baseline=True)
    doc_before = (with_base_dir / PLAN_DOC).read_bytes()

    resp = _generate(with_base_id)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "stage_mismatch", (
        "门 3 只属于纯验证分支，它必须还在；实际载荷：%r" % body
    )
    assert not any("path" in str(key) for key in body), (
        "错误载荷里不许出现路径字段；实际键：%r" % sorted(body)
    )
    _assert_payload_has_no_local_path(resp.text, with_base_dir, upstream_repo)
    assert body["current_phase"] == "queued"
    assert (with_base_dir / PLAN_DOC).read_bytes() == doc_before, (
        "被拒绝的请求不许改盘上的计划书"
    )
    assert not scripted_tool.prompts, "相位不对时不该问模型"


# ---------------------------------------------------------------------------
# 4. 分支分流：门 3（相位门）与相位推进只属于纯验证分支
# ---------------------------------------------------------------------------
#
# 上面三条钉的是「三道门的结论逐条可见」。这一节钉的是**分流本身**：
# 同一个端点，两条分支上的行为必须不同，而且不同的地方必须**恰好**是
# 门 3 与相位推进 —— 其余部分（基线门、门 2、成功载荷的形状）两条分支
# 完全一致。
#
# 「收窄」不是「取消」：纯验证分支那边必须照旧推进到计划书待审，否则纯验证
# 分支的计划书生成完就没有人审了。所以这一节是**成对**的两条用例 —— 一条
# 钉住开发分支**不**推进，一条钉住纯验证分支**照旧**推进。


def test_dev_branch_generate_200_without_phase_change(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """开发分支（目录里有 ``tasks.json``）→ 200，相位原样不变。

    相位门与相位推进都只属于纯验证分支：开发分支生成计划书是一个**可选附加
    动作**，它的相位是开发流程自己的事（PRD 决策点 2）。所以断言的是
    「相位等于**调用前**那个相位」，而不是某个写死的值 —— 后者会在调用前
    相位变化时假通过。

    相位取 ``prd_approved`` 是刻意的：那是**合法**能推进到计划书待审的相位
    （``prd_approved → tasks_generation → verification_plan_review``）。所以
    「相位不变」不能靠「这个相位推不动」蒙混过去 —— 只有**分支分流**那条
    规则能挡住这次推进。这就是这条用例比「挑一个推不动的相位」强的地方。
    """
    plan_id, plan_dir = make_plan(tasks=True, prd=True, phase="prd_approved")
    scripted_tool.document = build_plan_doc(
        upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
    )
    phase_before = _phase_of(plan_dir)
    assert phase_before == "prd_approved"

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan_id
    assert body["document_path"] == PLAN_DOC, (
        "文档路径必须是**相对路径**；实际是 %r" % body["document_path"]
    )
    assert body["current_phase"] == phase_before, (
        "开发分支的相位不许因为生成计划书而改变；实际是 %r"
        % body["current_phase"]
    )
    assert _phase_of(plan_dir) == phase_before, (
        "路由状态里的相位也不许被改写；实际是 %r" % _phase_of(plan_dir)
    )
    assert (plan_dir / PLAN_DOC).exists(), "计划书必须落盘"


def test_dev_branch_generate_does_not_touch_tasks_or_start_execution(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
    state_db_reader,
) -> None:
    """开发分支生成：任务清单逐字节不变，执行器没被拉起来。

    三条断言，三件事：

    * ``tasks.json`` 的 sha256 调用前后相同 —— 生成计划书**不许**改写任务
      清单（它是开发流程的产物，不是计划书的附属品）；
    * 执行器没被拉起来 —— 生成只写一份文档，启动执行是另一个端点的事；
    * 路由行的相位没被改写 —— 与上一条同源，但读的是**写侧**（数据库行），
      而不是 ``PlanState`` 的读路径。

    相位同样取 ``prd_approved``（那条相位**推得动**），所以路由行那条断言
    断的是「有没有推进」，不是「推进会不会失败」。
    """
    plan_id, plan_dir = make_plan(tasks=True, prd=True, phase="prd_approved")
    scripted_tool.document = build_plan_doc(
        upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
    )
    tasks_path = plan_dir / TASKS
    sha_before = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    phase_before = _phase_of(plan_dir)

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    sha_after = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    assert sha_after == sha_before, (
        "生成计划书不许改写任务清单；sha256 %r -> %r" % (sha_before, sha_after)
    )
    assert plan_id not in server._execution_state, "生成计划书不许拉起执行器"
    routing = state_db_reader.routing(plan_id)
    assert routing is not None
    assert routing["current_phase"] == phase_before, (
        "路由行的相位不许被生成动作改写；实际是 %r" % routing["current_phase"]
    )


def test_verification_branch_generate_still_advances_phase(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """纯验证分支（只有计划书、没有 ``tasks.json``）→ 相位推进到 ``verification_plan_review``。

    这是上一条的**对照**：门 3 与相位推进被收进纯验证分支之后，纯验证分支
    那边必须**照旧推进** —— 收窄不是取消。计划书就是这条分支的产物，生成完
    要停在「计划书待审」等人审。

    fixture 的相位是 ``prd_approved``：那是唯一一类「相位门放行、且相位边
    通向 ``tasks_generation``」的相位（``prd_approved → tasks_generation`` 在
    通用转移表里）。审核面已解决，所以那条相位边也放行。
    """
    plan_id, plan_dir = make_plan(doc=True, prd=True, phase="prd_approved")
    scripted_tool.document = build_plan_doc(
        upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
    )
    phase_before = _phase_of(plan_dir)
    assert phase_before == "prd_approved"

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_phase"] == "verification_plan_review", (
        "纯验证分支生成完必须停在「计划书待审」；实际是 %r"
        % body["current_phase"]
    )
    assert _phase_of(plan_dir) == "verification_plan_review", (
        "路由状态里的相位也必须推进；实际是 %r" % _phase_of(plan_dir)
    )


def test_generate_response_schema_is_three_fields(
    make_plan,
    upstream_repo: Path,
    scripted_tool: _ScriptedTool,
) -> None:
    """成功响应的键集合**恰为**三个，且载荷里没有绝对路径。

    「恰为」而不是「至少包含」：多一个键就是契约变了（调用方按固定形状解析
    载荷）。「不含绝对路径」是因为载荷要能直接发给前端 —— 计划目录在本机
    哪里，调用方不需要知道。
    """
    plan_id, plan_dir = make_plan(tasks=True, phase="queued")
    scripted_tool.document = build_plan_doc(
        upstream_repo, pr_number=PR_NUMBER, ref=PR_REF
    )

    resp = _generate(plan_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"plan_id", "document_path", "current_phase"}, (
        "成功响应的键集合必须恰为三个；实际是 %r" % sorted(body)
    )
    assert body["document_path"] == PLAN_DOC
    assert str(plan_dir) not in resp.text, (
        "载荷里不许出现计划目录的绝对路径；实际响应：%s" % resp.text
    )
    assert str(upstream_repo) not in resp.text, (
        "载荷里不许出现上游仓库的绝对路径；实际响应：%s" % resp.text
    )
