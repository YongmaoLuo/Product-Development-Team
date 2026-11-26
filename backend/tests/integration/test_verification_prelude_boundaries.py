"""``/start`` 前奏的分支边界：计划准备 vs 验证对象准备。

背景
----
``_verification_branch_prelude()``（``backend/routes/verification.py``）在
``/start`` 的相位 CAS **之前**跑完，是验证轮启动的第一段管线。它有五件事可做：

* **翻译计划书**（``_prepare_machine_plan()`` 里的翻译那一步）；
* **等价性门禁**（``_prepare_machine_plan()`` 里的比对那一步）；
* **基线漂移校验**；
* **物化 worktree**；
* **注册 ``project_dir``**。

后三件是「验证一个 PR 对象」专属的；前两件不是 —— 审过的计划书在哪条分支上
都要变成可消费的机器计划，并且要与盘上那一份逐条相等。开发分支以前**整段**
跳过前奏，于是计划书在开发分支上永远不会被消费，也不会与盘上的机器计划比过
一次：盘上那份与当前计划书对不上时，启动会一路走到相位拒绝（409
``stage_mismatch``），把「审过的东西和要验的东西不是同一批」这件事藏起来。

分派依据
--------
**按磁盘状态分派，不按分支分派**：

* 来源判定（:func:`plan_artifacts.resolve_plan_source`）回答「验证轮该消费哪
  一份计划」：计划书在 → ``REVIEWED``（真源是人审的计划书，机器计划是它的派
  生物，两者必须逐条等价）；
* 分支判定（:func:`plan_artifacts.detect`）回答「这条 plan 走哪条路」。

两个判定正交，所以前奏拆成两段：**计划准备**由来源决定（``REVIEWED`` 时两条
分支同等对待），**验证对象准备**由分支决定（只有纯验证分支、且审核完备时才
做）。

双向断言
--------
每条用例都**双向**断言：该发生的发生了，不该发生的一次都没发生。只断言「验证
轮跑起来了」区分不了这两条路径 —— 它们都会跑起来。所以本文件观测的是**副作
用本身**：

* 翻译器被问了几次（``create_coding_tool`` 的替身计数）；
* 基线漂移校验被调了几次（``resolve_ref_commit`` 探针计数）；
* worktree 物化被调了几次（``ensure_workspace`` 探针计数）；
* ``project_dir`` 有没有被改写（``plan_execution`` 那一列 + 注册探针）；
* 相位 CAS 有没有发生（``cas_spy``）。

两条失败方向相反，都要有断言：该翻译的没翻译（用户的审阅结果静默失效），与
不该翻译的翻译了（开发分支被当成验证对象，执行落在一个不该存在的工作副本
上）。

门禁失败
--------
盘上的机器计划与当前计划书不等价时**硬失败**：422
``verification_plan_equivalence_failed`` + ``divergences``，不落盘、不回退到
LLM 自动生成、不改写盘上的机器计划，而且失败发生在相位 CAS **之前** —— 阶段
不动。

``/start`` 的产物冲突门禁
-------------------------
``/start`` 原来还有一道门禁：产物判定给出 ``conflict`` 时 409
``artifact_conflict``，不许进 CAS。共存合法之后（:func:`plan_artifacts.detect`
按「任务优先」把「任务清单 + 计划书」归入 ``dev`` 分支），判定结果里**没有**
``conflict`` 了 —— 那道门禁永远不会触发。留着它，下一个读代码的人会以为「验证
启动仍会被产物状态拦下」，而真相是这条 plan 走哪条路由**验证流水线自己的门**
决定。

所以门禁被清掉了，``detect()`` 的判定改为直接决定「验证分支的额外源阶段只属于
纯验证分支」：共存目录的主分支是开发分支，它拿不到 ``verification_plan_review``，
开发任务没做完时 ``/start`` 仍然是 409 ``stage_mismatch`` —— **不跳过执行**。

这条清理只有白盒才钉得住（生产里那个分支不可达），所以用例把那次判定换成**一个
会产出 ``conflict`` 的替身**：门禁还在的话它会 409 拦下这次启动。

测试数据
--------
canonical 验证计划书（``tests.fixtures.verification_mode``）与它派生的机器计划。
计划目录由共享夹具造（``make_dev_plan`` / ``make_verification_plan``），目录
落在用例自己的 tmp 树里，数据库行由既有的 ``plan_sqlite_seeder`` 注入。模型
是替身：本文件不碰任何 provider，也不联网。
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
from plan_artifacts import MACHINE_PLAN_FILENAME, PLAN_DOC_FILENAME  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    MACHINE_PLAN,
    REVIEW_PHASE,
    cas_spy,
    make_dev_plan,
    make_verification_plan,
    no_worker_thread,
)

client = TestClient(server.app)


# ---------------------------------------------------------------------------
# 替身与探针
# ---------------------------------------------------------------------------


class _ScriptedTranslator:
    """翻译工具的替身：按计划书里的验证点集合回答。

    只给**执行细节**（``test_command``），身份与内容字段一律以文档为准 ——
    翻译器本来就只允许模型补执行细节，多给反而会被判成漂移。
    """

    def __init__(self, doc: str = CANONICAL_PLAN_DOC) -> None:
        from verification_plan_doc import parse_plan_doc

        parsed = parse_plan_doc(doc)
        self.points: List[Dict[str, Any]] = [
            {"id": item.id, "test_command": "pytest -q"} for item in parsed.items
        ]
        self.prompts: List[str] = []

    def query_json(
        self,
        prompt: str,
        system_instruction: str = "",
        timeout: int = 0,
    ) -> Dict[str, Any]:
        self.prompts.append(prompt)
        return {"verification_points": self.points}


class _Probes:
    """前奏做过的五件事：按发生顺序记下来。

    Attributes:
        calls: 事件名的时间线（``prepare`` / ``gate`` / ``drift`` /
            ``materialise`` / ``register``）。
        translator: 翻译工具的替身 —— ``prompts`` 为空即「没问过模型」。
    """

    def __init__(self, translator: _ScriptedTranslator) -> None:
        self.calls: List[str] = []
        self.translator = translator

    def count(self, name: str) -> int:
        """某个事件发生了几次。"""
        return self.calls.count(name)


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> _Probes:
    """把前奏的五个副作用都变成**可计数**的探针。

    计数用替身与探针，不用墙钟：前奏里没有等待，多等一会儿不会改变结果。
    每个探针都**仍然调用原函数** —— 探针只负责计数，不负责把行为改掉。

    ``gate`` 那一项是等价性门禁本身：它被调用过，就说明「机器计划与计划书
    逐条相等」这件事在这条分支上**真的被查过**，而不是被跳过了。
    """
    import verification_plan_translator
    import verification_workspace

    recorder = _Probes(_ScriptedTranslator())

    original_tool = server.create_coding_tool

    def _tool(*args: Any, **kwargs: Any) -> Any:
        recorder.calls.append("prepare")
        return recorder.translator

    monkeypatch.setattr(server, "create_coding_tool", _tool)

    original_gate = verification_plan_translator.validate_equivalence

    def _gate(parsed: Any, plan_json: Any) -> Any:
        recorder.calls.append("gate")
        return original_gate(parsed, plan_json)

    monkeypatch.setattr(
        verification_plan_translator, "validate_equivalence", _gate
    )

    for name, attribute in (
        ("drift", "resolve_ref_commit"),
        ("materialise", "ensure_workspace"),
        ("register", "register_project_dir"),
    ):
        original = getattr(verification_workspace, attribute)

        def _wrapper(_original: Any = original, _name: str = name) -> Any:
            def _call(*args: Any, **kwargs: Any) -> Any:
                recorder.calls.append(_name)
                return _original(*args, **kwargs)

            return _call

        monkeypatch.setattr(verification_workspace, attribute, _wrapper())

    return recorder


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


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


def _write_plan_doc(plan_dir: Path, doc: str = CANONICAL_PLAN_DOC) -> Path:
    """把计划书放进一个已有的 plan 目录（造共存目录用）。"""
    path = plan_dir / PLAN_DOC_FILENAME
    path.write_bytes(doc.encode("utf-8"))
    return path


def _stale_machine_plan() -> Dict[str, Any]:
    """一份**与计划书对不上**的机器计划。

    对不上的地方是证据引用串：机器计划少引用了 ``E1`` —— 等价性门禁在
    ``evidence`` 维度上会报出这一条。
    """
    stale = json.loads(json.dumps(MACHINE_PLAN))
    stale["verification_points"][0]["expected_result"] = "解析出一个 40 位十六进制提交号"
    return stale


# ---------------------------------------------------------------------------
# 1. 开发分支 + 计划书：只做计划准备
# ---------------------------------------------------------------------------


def test_dev_branch_with_plan_doc_prepares_machine_plan_only(
    make_dev_plan,
    probes: _Probes,
    cas_spy,
    no_worker_thread,
) -> None:
    """开发分支 + 计划书 → 翻译一次、等价门禁跑一次、机器计划落盘，其余三步零次。

    双向断言的后一半：worktree 物化、``project_dir`` 注册、基线漂移校验在开发
    分支上**一次都不该发生** —— 开发分支的验证跑在自己的目录里，不是跑在被验
    证对象的 worktree 里。
    """
    plan = make_dev_plan()
    _write_plan_doc(plan.plan_dir)
    project_dir_before = _project_dir(plan.plan_id)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text

    # 该发生的：翻译了一次，等价性门禁跑过一次，机器计划落盘。
    assert probes.count("prepare") == 1, probes.calls
    assert probes.count("gate") == 1, probes.calls
    assert probes.translator.prompts, "翻译这一步真的问过模型了吗"
    machine_plan_path = plan.plan_dir / MACHINE_PLAN_FILENAME
    assert machine_plan_path.exists(), "机器计划落盘了"

    # 不该发生的：另外三步一次都没有。
    assert probes.count("drift") == 0, probes.calls
    assert probes.count("materialise") == 0, probes.calls
    assert probes.count("register") == 0, probes.calls
    assert _project_dir(plan.plan_id) == project_dir_before, (
        "project_dir 没被改写"
    )

    # 前奏之后是既有 CAS 与验证循环：相位推进了。
    assert cas_spy, "相位 CAS 发生了"
    assert _routing_phase(plan.plan_id) == "verification_running"


# ---------------------------------------------------------------------------
# 2. 开发分支无计划书：前奏完全跳过
# ---------------------------------------------------------------------------


def test_dev_branch_without_plan_doc_skips_prelude_entirely(
    make_dev_plan,
    probes: _Probes,
    cas_spy,
    no_worker_thread,
) -> None:
    """开发分支无计划书 → 五步全都不发生，源阶段集合与开发分支一致。

    「行为与改动前逐字一致」这条断言有两个面：探针计数全零（没有翻译、没有
    门禁、没有物化），以及**源阶段集合没有多出验证分支那一项** —— 共存不得
    悄悄改变开发分支的可达性。
    """
    plan = make_dev_plan()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert probes.calls == [], probes.calls
    assert cas_spy, "相位 CAS 发生了"
    _plan_id, sources, new_phase = cas_spy[-1]
    assert REVIEW_PHASE not in sources, (
        f"开发分支拿到了验证分支的额外源阶段：{sources}"
    )
    assert "executing" in sources, sources
    assert new_phase == "verification_running"


# ---------------------------------------------------------------------------
# 3. 门禁失败：硬失败、报差异、不落盘、不回退
# ---------------------------------------------------------------------------


def test_prepare_machine_plan_failure_aborts_before_cas(
    make_dev_plan,
    probes: _Probes,
    cas_spy,
    no_worker_thread,
) -> None:
    """盘上的机器计划与计划书对不上 → 422 + differences，且什么都没发生。

    这条是「既不静默回退到 LLM 自动生成，也不在门禁失败时改写盘上的机器计划」
    的钉子：翻译器没被问过（没有回退），盘上那一份逐字节未变（没有改写），
    相位 CAS 没发生（阶段没动）。
    """
    plan = make_dev_plan()
    _write_plan_doc(plan.plan_dir)
    machine_plan_path = plan.plan_dir / MACHINE_PLAN_FILENAME
    machine_plan_path.write_text(
        json.dumps(_stale_machine_plan(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    before = machine_plan_path.read_bytes()
    phase_before = _routing_phase(plan.plan_id)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "verification_plan_equivalence_failed", body
    assert body["divergences"], body
    assert "stage_mismatch" not in resp.text, (
        "门禁失败必须报出差异，而不是回落到相位拒绝"
    )

    assert not probes.translator.prompts, "没有回退到 LLM 自动生成"
    assert probes.calls == ["gate"], probes.calls
    assert machine_plan_path.read_bytes() == before, (
        "门禁失败不改写盘上的机器计划"
    )
    assert cas_spy == [], "相位 CAS 没有发生"
    assert _routing_phase(plan.plan_id) == phase_before, "阶段没动"


# ---------------------------------------------------------------------------
# 4. 纯验证分支：顺序不变
# ---------------------------------------------------------------------------


def test_verification_branch_prelude_order_unchanged(
    make_verification_plan,
    probes: _Probes,
    cas_spy,
    no_worker_thread,
) -> None:
    """纯验证分支：计划准备 → 漂移校验 → 物化 worktree → 注册 ``project_dir``。

    顺序本身就是契约：漂移校验排在翻译**之后**，因为翻译要问一次模型，模型
    思考期间 PR head 移动是完全现实的事 —— 放行前那次校验只是不白花一次模型
    调用，物化前那次才是权威的那次。
    """
    plan = make_verification_plan()
    # 机器计划不在盘上 → 「计划准备」这一步真的问了一次模型，顺序才看得见。
    (plan.plan_dir / MACHINE_PLAN_FILENAME).unlink()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert probes.calls == [
        "prepare",
        "gate",
        "drift",
        "materialise",
        "register",
    ], probes.calls


# ---------------------------------------------------------------------------
# 5. 共存目录：验证启动不跳过开发执行
# ---------------------------------------------------------------------------


def test_start_after_coexist_still_executes_tasks_first(
    make_dev_plan,
    probes: _Probes,
    cas_spy,
    no_worker_thread,
) -> None:
    """共存目录 → 验证启动不跳过开发执行，源阶段集合与开发分支一致。

    共存只多了一份产物，不改变这条 plan 走哪条路：开发任务照跑，验证轮照样
    从开发完成的那一阶段起，**不拿**验证分支的额外源阶段。
    """
    plan = make_dev_plan()
    _write_plan_doc(plan.plan_dir)
    tasks_before = (plan.plan_dir / "tasks.json").read_bytes()

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert cas_spy, "相位 CAS 发生了"
    _plan_id, sources, new_phase = cas_spy[-1]
    assert REVIEW_PHASE not in sources, (
        f"共存 plan 拿到了验证分支的额外源阶段：{sources}"
    )
    assert new_phase == "verification_running"
    assert (plan.plan_dir / "tasks.json").read_bytes() == tasks_before, (
        "任务清单没被动过：共存不跳过开发执行"
    )


# ---------------------------------------------------------------------------
# 6. ``/start`` 不再有产物冲突门禁
# ---------------------------------------------------------------------------


def test_start_has_no_artifact_conflict_gate(
    make_dev_plan,
    probes: _Probes,
    cas_spy,
    no_worker_thread,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/start`` 不再被「产物冲突」拦下。

    共存合法之后，:func:`plan_artifacts.detect` 的输出域里**没有**
    ``conflict`` —— 两类产物同时存在时按「任务优先」归入 ``dev`` 分支。
    ``/start`` 里那道「``branch == conflict`` → 409 ``artifact_conflict``」
    的门禁于是**永远不会触发**：留着它，下一个读代码的人会以为「验证启动
    仍会被产物状态拦下」，而真相是这条 plan 走哪条路由**验证流水线自己的
    门**决定。

    这条用例把那次判定换成一个**会产出 ``conflict`` 的替身**：门禁还在的话，
    它会 409 拦下这次启动；门禁清理掉之后，这次启动走的是正常的开发分支
    路径（计划准备 → CAS → 验证循环）。
    """
    import routes.verification as verification_route
    from plan_artifacts import ArtifactSet

    plan = make_dev_plan()
    _write_plan_doc(plan.plan_dir)

    original_detect = verification_route.detect

    def _conflict_detect(plan_dir: Path) -> Any:
        artifacts = original_detect(plan_dir)
        return ArtifactSet(
            tasks_path=artifacts.tasks_path,
            plan_doc_path=artifacts.plan_doc_path,
            branch="conflict",
        )

    monkeypatch.setattr(verification_route, "detect", _conflict_detect)

    resp = client.post(f"/api/verification/{plan.plan_id}/start")

    assert resp.status_code == 200, resp.text
    assert "artifact_conflict" not in resp.text, resp.text
    assert cas_spy, "相位 CAS 发生了"
