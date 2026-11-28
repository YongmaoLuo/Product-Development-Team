"""真实服务主链路 E2E —— 框架修复 lane 从失败到重跑的一条整链。

这份文件证明什么
----------------
单元测试与集成测试都**替换了组件**：注册表是内存里的替身，生命周期带着
假的状态机，快照是从字典现搭的。于是「整条链是通的」这件事一次也没被证明
过 —— 每段各自成立，拼起来可能断在缝里。

所以这一份起一个**真的** ``backend.server`` 进程（真的 uvicorn、真的
HTTP、真的 SQLite 文件），把框架修复 lane 的整条链跑一遍：

    失败 → 复现 → 登记 → 生成修复 → hold → 合并 → 重跑

链的两头都用**真的 HTTP** 验收：

* **hold 那一刻**，真实服务对 ``POST /api/verification/p1/start`` 回答
  **409 ``blocked_on_framework_fix``**，并且说得出是哪个缺陷的指纹 ——
  守卫在真实进程里读到了真实库里的挂起块；
* **重跑之后**，真实服务的 ``GET /api/verification/p1/status`` 给出**非空**
  的 ``vp_results``，里面是那个验证点的新结论。

中间那一段（复现 / 登记 / 生成修复 / hold / 合并 / 重跑）走的是**生产模块**
—— ``framework_fix.repro`` / ``framework_fix.registry`` /
``framework_fix.spawner`` / ``framework_fix.lifecycle`` —— 没有替身：
它们写的就是真实服务读的那份 SQLite、就在真实服务看的那棵计划树下。
它们不是 HTTP 接口，所以只能由测试进程调用；**可观测的两端一律走 HTTP**，
这样「真实服务读得到真实事实」这件事才是被证明的，而不是被假设的。

三条判据
--------
* ``test_main_chain`` —— 整条链的每一段都留下了它的痕迹（注册表行、命中
  行、修复计划目录、路由行、挂起块、审计链、结果行），而且**两端的 HTTP
  答案**都对得上；
* ``test_snapshot_partition`` —— 真实 HTTP 下，框架修复 lane 的计划只出现在
  它自己的那一列（``kind=framework_fix``），不出现在普通计划那一列
  （``kind=plans``）；
* ``test_no_real_db_written`` —— 真实库（``<repo>/.pdt/state.db``）一个字节
  都没被动过：存在性、mtime、大小三项都不变。

边界条件
--------
* **端口**：随机取一个空闲端口，**跳过 8000** —— 8000 上跑着别的服务，
  抢它会把别的进程踢掉；
* **状态库**：隔离到本次的 ``tmp_path``（``PDT_STATE_DB_PATH``），
  真实库一个字节都不写；
* **子进程退出码非 0**：子进程自己死了（不是我们叫它停的）→ 测试失败，
  并把服务端自己的输出（stdout + stderr 合成一路）打出来；
* **等待**：有界轮询（``_wait_ready`` 的 90 秒上限），不 sleep 死等。

复用说明
--------
起服务的脚手架（``_free_port`` / ``_boot`` / ``_wait_ready`` / ``_teardown`` /
``_http_json`` / ``_server_output``）按**文件路径** importlib 加载
``backend/tests/e2e/test_execution_status_restart_e2e.py`` 里的那一份用 ——
本仓库的 e2e 目录没有 ``__init__.py``，跨模块 import 要靠 pytest 的
rootdir gymnastics，太脆；按路径加载不需要它，也不依赖收集顺序。
那个文件**不被修改**。
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 路径 + 借来的脚手架
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
E2E_DIR = Path(__file__).resolve().parent

# ``backend/`` 必须可导入：生产模块之间按平铺名字互相 import
# （``from config_paths import ...``）。
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_sibling(module_name: str, filename: str):
    """按**文件路径**加载一个兄弟 e2e 模块。

    ``importlib.util.spec_from_file_location`` 读文件、以私有名义执行它。
    兄弟文件一个字节都不改，这里也不依赖它能作为包被导入。
    """
    path = E2E_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load the sibling module at {path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RESTART_E2E = _load_sibling(
    "_pdt_e2e_execution_status_restart", "test_execution_status_restart_e2e.py"
)

_boot = _RESTART_E2E._boot
_wait_ready = _RESTART_E2E._wait_ready
_teardown = _RESTART_E2E._teardown
_http_json = _RESTART_E2E._http_json
_server_output = _RESTART_E2E._server_output

#: 绝不抢的端口。8000 上跑着这台机器上别的服务。
_RESERVED_PORT = 8000


def _free_port_excluding(reserved: int) -> int:
    """一个空闲端口，且不是 ``reserved``。

    做法与兄弟文件相同：让内核挑一个端口，那一刻它必然是空的。挑到
    ``reserved`` 就再挑一次 —— 概率约等于零，但「约等于零」不是「不会」。
    """
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != reserved:
            return port
    raise AssertionError(f"could not find a free port other than {reserved}")


# ---------------------------------------------------------------------------
# 这条链的常量
# ---------------------------------------------------------------------------

#: 等着修复的那条计划。
PLAN_ID = "p1"

#: 撞上框架缺陷的那个验证点。
VP_ID = "VP-1"

#: 负控用的第二条计划（不挂起，但产物自相矛盾）。
CONTROL_PLAN_ID = "p2"

#: 修复计划所在的仓（运营字段：复现排在哪、合并串行到哪）。
REPO_NAME = "repo-a"

#: 缺陷签名的材料 —— 框架文件集、错误类别、首错行。
FRAMEWORK_FILE = "backend/framework_fix/registry.py"
ERROR_CLASS = "AssertionError"
FIRST_ERROR_LINE = "AssertionError: expected 1, got 2"

#: 复现结论 —— 五分类闭集里的那个「确定性框架缺陷」。
FRAMEWORK_DETERMINISTIC = "framework_deterministic"

#: 守卫拒绝时的那个词。
BLOCKED_ON_FRAMEWORK_FIX = "blocked_on_framework_fix"

#: 修复计划的五段任务模板（架构决策点 4）。
TASK_TEMPLATE_STEPS = ("locate", "fix", "targeted_test", "gate", "open_pr")

#: 修复计划在路由表里的那一列。
FRAMEWORK_FIX_KIND = "framework_fix"


# ---------------------------------------------------------------------------
# 真实库的指纹 —— 「真实库一个字节都没动」的判据
# ---------------------------------------------------------------------------
def _real_db_fingerprint() -> Dict[str, Any]:
    """真实状态库的指纹：存在性、mtime、大小。

    ``config_paths.STATE_DB`` 是**运营者的真库**（``<repo>/.pdt/state.db``）。
    判据是三项一起比：文件不在（压根没被创建）、mtime 没变、大小没变。
    """
    from config_paths import STATE_DB

    try:
        stat = STATE_DB.stat()
    except OSError:
        return {
            "path": str(STATE_DB),
            "exists": False,
            "mtime_ns": None,
            "size": None,
        }
    return {
        "path": str(STATE_DB),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


# ---------------------------------------------------------------------------
# 夹具：真的服务 + 真的整条链
# ---------------------------------------------------------------------------
@dataclass
class Lane:
    """这一轮跑出来的事实。

    Attributes:
        base_url: 真实服务的根地址。
        port: 它听的端口。
        plans_dir: 计划树（隔离在 ``tmp_path`` 里）。
        db_path: 状态库（隔离在 ``tmp_path`` 里）。
        fingerprint: 缺陷指纹。
        fix_plan_id: 派生出来的修复计划号。
        attach_id: 挂接 id。
        released: 合并那一步释放了几条挂接。
        rerun: 重跑每一段的结论。
        hold_refusal: **挂起进行中**时真实服务对「开验证」的回答。
        real_db_before: 跑之前真实库的指纹。
    """

    base_url: str
    port: int
    plans_dir: Path
    db_path: Path
    fingerprint: str
    fix_plan_id: str
    attach_id: str
    released: int
    rerun: List[Dict[str, Any]]
    hold_refusal: Tuple[int, Any] = (0, None)
    real_db_before: Dict[str, Any] = field(default_factory=dict)


def _seed_plan(plans_dir: Path, plan_id: str) -> Path:
    """把一条计划种出来。

    生产路径：计划目录 + ``plan_routing`` 行（``PlanState`` 自己写）。
    没有路由行的话，守卫读不到挂起状态，会回答 500（读不到就当没挂起 =
    把守卫变成摆设），而不是 409。
    """
    from plan_state import PlanState

    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {"plan_id": plan_id, "current_phase": "verification_running"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    PlanState(plan_dir).force_set_phase("verification_running")
    return plan_dir


def _seed_conflicting_plan(plans_dir: Path, plan_id: str) -> Path:
    """种一条**产物自相矛盾**的计划 —— 负控用。

    两类产物同时存在时，产物门禁会先答 409（artifact conflict），
    根本走不到分支流水线：所以这条负控**不会**去物化一个 worktree，
    更不会去跑一轮真验证。

    它要证明的是：``POST /api/verification/{plan_id}/start`` **也会**答 409，
    但那是另一种 409 —— 端点的 409 不是框架缺陷挂起造成的。
    """
    from plan_artifacts import PLAN_DOC_FILENAME, TASKS_FILENAME
    from plan_state import PlanState

    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {"plan_id": plan_id, "current_phase": "verification_running"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / TASKS_FILENAME).write_text(
        json.dumps({"tasks": []}, ensure_ascii=False), encoding="utf-8"
    )
    (plan_dir / PLAN_DOC_FILENAME).write_text(
        "# verification plan\n", encoding="utf-8"
    )
    PlanState(plan_dir).force_set_phase("verification_running")
    return plan_dir


def _reproduction() -> Any:
    """一次「干净 main 上确定性复现」的结论。

    两次 attempt 都失败、首错行一样 —— 这是 ``framework_deterministic`` 的
    判据（2/2 同因）。
    """
    from framework_fix.repro import ReproAttempt, ReproductionResult

    return ReproductionResult(
        classification=FRAMEWORK_DETERMINISTIC,
        attempts=(
            ReproAttempt(
                run=1,
                exit_code=1,
                timed_out=False,
                first_error_line=FIRST_ERROR_LINE,
                duration_s=0.5,
            ),
            ReproAttempt(
                run=2,
                exit_code=1,
                timed_out=False,
                first_error_line=FIRST_ERROR_LINE,
                duration_s=0.4,
            ),
        ),
        clean_commit="0" * 40,
        tree_fingerprint="sha256:" + "1" * 64,
    )


def _failure_hit() -> Any:
    """那次失败命中 —— 判定公式的全部输入事实。"""
    from framework_fix.classifier import FailureHit

    return FailureHit(
        vp_id=VP_ID,
        plan_id=PLAN_ID,
        error_class=ERROR_CLASS,
        first_error_line=FIRST_ERROR_LINE,
        framework_files=[FRAMEWORK_FILE],
        acceptance_critical=True,
        out_of_scope_change=False,
    )


def _run_chain(plans_dir: Path, db_path: Path, base_url: str) -> Dict[str, Any]:
    """跑完整条链，把每一段的结果交出来。

    顺序就是生产顺序：

    1. **登记** —— ``register_hit``：注册表行、命中行、首条事件，同一事务；
    2. **生成修复** —— ``build_envelope`` + ``spawn``：派生修复计划，写下它
       的目录、任务与路由行；
    3. **hold** —— ``hold``：挂接行 + 审计链 + 计划状态里的挂起块；
       **就在这一刻**，用真实 HTTP 问一次真实服务「能开验证吗」；
    4. **合并** —— ``release_hits``：合并那一步已经放开了挂起；
    5. **重跑** —— ``rerun_after_merge``：只重跑挂接的那个验证点，落一行
       结果。

    ``hold`` 之前要把「重跑之后验证会给出的结论」写成计划的验证报告 ——
    默认重跑执行器（``_default_rerun``）读的就是它，那是验证路径的产物。
    """
    from framework_fix.lifecycle import FrameworkFixLifecycle
    from framework_fix.registry import FrameworkFixRegistry
    from framework_fix.spawner import build_envelope, spawn

    registry = FrameworkFixRegistry(db_path=db_path)

    # 1. 失败 → 登记
    fingerprint = registry.register_hit(
        {
            "plan_id": PLAN_ID,
            "vp_id": VP_ID,
            "framework_files": [FRAMEWORK_FILE],
            "error_class": ERROR_CLASS,
            "first_error_line": FIRST_ERROR_LINE,
            "classification": FRAMEWORK_DETERMINISTIC,
            "blocking": True,
            "repo": REPO_NAME,
        }
    )

    # 2. 复现 → 生成修复
    envelope = build_envelope(
        fingerprint=fingerprint,
        hit=_failure_hit(),
        repro=_reproduction(),
        repo=REPO_NAME,
    )
    fix_plan_id = spawn(envelope)

    # 3. hold
    lifecycle = FrameworkFixLifecycle(registry=registry)
    receipt = lifecycle.hold(
        plan_id=PLAN_ID,
        vp_id=VP_ID,
        fingerprint=fingerprint,
        round_no=1,
        source="clean_main_repro",
    )

    # 3b. 挂起进行中 —— 问一次真实服务「能开验证吗」。这是链上唯一一处
    #     「真实服务拒绝干活」的地方，必须在挂起**还在**的时候问。
    hold_refusal = _http_json(
        "POST", f"{base_url}/api/verification/{PLAN_ID}/start", {}
    )

    # 4. 合并（修复合入 → 放开挂起）
    released = lifecycle.release_hits(fingerprint)

    # 5. 重跑：验证路径在修复合入之后又跑了一遍，结论写进验证报告。
    (plans_dir / PLAN_ID / "verification_report.json").write_text(
        json.dumps(
            {"verification_points": [{"id": VP_ID, "status": "passed"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rerun = lifecycle.rerun_after_merge(fix_plan_id)

    return {
        "fingerprint": fingerprint,
        "fix_plan_id": fix_plan_id,
        "attach_id": str(receipt["attach_id"]),
        "released": released,
        "rerun": rerun,
        "hold_refusal": hold_refusal,
    }


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lane:
    """起一个真的后端，把整条链跑一遍。

    状态库与计划树都隔离在本次的 ``tmp_path`` 里：``tmp_path`` 是绝对路径，
    所以 ``config_paths`` 的相对路径锚定规则不会把它们拽到仓库里。

    真实库在**起服务之前**就被记下指纹 —— 「真库没被写」这条判据要比较
    的是「跑之前」和「跑之后」，而跑之前的那一刻只有夹具知道。
    """
    real_db_before = _real_db_fingerprint()

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.db"

    # 显式指一遍（而不是指望某个 autouse 夹具替我们指）：生产模块按
    # ``resolve_state_db_path()`` / ``resolve_plans_dir()`` 找这两个位置，
    # 环境变量是它们唯一的入口。
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))

    from config_paths import resolve_plans_dir, resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    assert resolve_state_db_path().resolve() == db_path.resolve(), (
        "the state database must be redirected into tmp_path; "
        f"the application resolved {resolve_state_db_path()}"
    )
    assert resolve_plans_dir().resolve() == plans_dir.resolve(), (
        "the plans tree must be redirected into tmp_path; "
        f"the application resolved {resolve_plans_dir()}"
    )

    # 生产迁移 —— 框架修复的那四张表在第一条链跑之前就在。
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    port = _free_port_excluding(_RESERVED_PORT)
    base_url = f"http://127.0.0.1:{port}"

    proc = _boot(tmp_path, port)
    try:
        _wait_ready(base_url, proc)

        _seed_plan(plans_dir, PLAN_ID)
        chain = _run_chain(plans_dir, db_path, base_url)

        yield Lane(
            base_url=base_url,
            port=port,
            plans_dir=plans_dir,
            db_path=db_path,
            fingerprint=chain["fingerprint"],
            fix_plan_id=chain["fix_plan_id"],
            attach_id=chain["attach_id"],
            released=chain["released"],
            rerun=chain["rerun"],
            hold_refusal=chain["hold_refusal"],
            real_db_before=real_db_before,
        )
    finally:
        # 子进程自己死了（不是我们叫它停的）→ 失败，并把服务端自己的输出
        # 打出来（stdout 与 stderr 在子进程里合成了一路）。
        died = proc.poll()
        _teardown(proc)
        if died is not None and died != 0:
            output = _server_output(proc)
            if sys.exc_info()[0] is None:
                pytest.fail(
                    f"the backend exited with {died} before the test "
                    f"finished:\n{output}"
                )
            print(output, file=sys.stderr)


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------
def _routing_row(db_path: Path, plan_id: str) -> Optional[Dict[str, Any]]:
    """``plan_routing`` 的那一行 —— 读的是真实库。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT plan_id, current_phase, plan_kind, framework_fix "
            "FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _framework_fix_block(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """计划的框架修复块 —— 读的是真实库。"""
    raw = (_routing_row(db_path, plan_id) or {}).get("framework_fix")
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _event_types(db_path: Path) -> List[str]:
    """审计链上的事件类型（按插入顺序）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT event_type FROM framework_fix_event ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def _get(base_url: str, path: str) -> Dict[str, Any]:
    """一次 GET，答案必须是 200 且是一个文档。"""
    status, body = _http_json("GET", f"{base_url}{path}")
    assert status == 200, f"GET {path} answered HTTP {status}: {body!r}"
    assert isinstance(body, dict), f"GET {path} answered {body!r}"
    return body


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_main_chain(lane: Lane) -> None:
    """失败→复现→登记→生成修复→hold→合并→重跑：每一段都留下痕迹，两端都对得上。"""
    # --- 登记：注册表一行、命中一行 ---------------------------------------
    from framework_fix.registry import FrameworkFixRegistry

    registry = FrameworkFixRegistry(db_path=lane.db_path)
    row = registry.lookup(lane.fingerprint)
    assert row is not None, (
        f"登记之后注册表里必须有这一行；实际 {registry.list_registry()!r}"
    )
    assert str(row["fix_plan_id"]) == lane.fix_plan_id, (
        "注册表行必须指着那个修复计划；"
        f"实际 {row['fix_plan_id']!r} != {lane.fix_plan_id!r}"
    )

    hits = registry.list_hits(lane.fingerprint)
    assert len(hits) == 1, f"一次命中一行；实际 {hits!r}"
    assert str(hits[0]["plan_id"]) == PLAN_ID, f"命中来自哪条计划；实际 {hits!r}"
    assert str(hits[0]["classification"]) == FRAMEWORK_DETERMINISTIC, (
        f"复现结论要留在命中行上；实际 {hits[0]!r}"
    )

    # --- 生成修复：目录、任务、路由行 -------------------------------------
    fix_dir = lane.plans_dir / lane.fix_plan_id
    assert fix_dir.is_dir(), f"修复计划必须有目录；实际 {fix_dir} 不存在"

    tasks = json.loads((fix_dir / "tasks.json").read_text(encoding="utf-8"))
    titles = [str(task.get("title") or "") for task in tasks.get("tasks") or []]
    assert tuple(titles) == TASK_TEMPLATE_STEPS, (
        f"修复计划是固定五段；实际 {titles!r}"
    )

    fix_routing = _routing_row(lane.db_path, lane.fix_plan_id)
    assert fix_routing is not None, "修复计划必须在路由表里"
    assert fix_routing["plan_kind"] == FRAMEWORK_FIX_KIND, (
        f"修复计划在路由表里是 lane 自己的那种计划；实际 {fix_routing!r}"
    )

    # --- hold：挂接行（谁曾经等过这个修复） --------------------------------
    attaches = registry.list_attach(lane.fingerprint)
    assert len(attaches) == 1, f"一次挂起一条挂接行；实际 {attaches!r}"
    assert str(attaches[0]["attach_id"]) == lane.attach_id, (
        "回执里的挂接 id 就是那一行的；"
        f"实际 {attaches[0]['attach_id']!r} != {lane.attach_id!r}"
    )
    assert str(attaches[0]["plan_id"]) == PLAN_ID, (
        f"挂接行要记着被挂住的是哪条计划；实际 {attaches[0]!r}"
    )
    assert str(attaches[0]["vp_id"]) == VP_ID, (
        f"挂接行要记着被挂住的是哪个验证点；实际 {attaches[0]!r}"
    )

    # --- hold：审计链上留着「挂起」与「放开」 ------------------------------
    events = _event_types(lane.db_path)
    assert "held" in events, (
        f"挂起必须在审计链上留一条 held；实际 {events!r}"
    )
    assert "released" in events, (
        f"合并（放开挂起）必须在审计链上留一条 released；实际 {events!r}"
    )

    # --- hold 那一刻，真实服务拒绝了「开验证」 ----------------------------
    status, body = lane.hold_refusal
    assert status == 409, (
        f"挂起期间真实服务必须拒绝开验证（409）；实际 HTTP {status}: {body!r}"
    )
    assert isinstance(body, dict), f"拒绝的答案必须是一个文档；实际 {body!r}"
    assert body.get("error") == BLOCKED_ON_FRAMEWORK_FIX, (
        f"拒绝的答案要说出是被框架缺陷挂住的；实际 {body!r}"
    )
    assert body.get("fingerprint") == lane.fingerprint, (
        f"拒绝的答案要说出是哪个缺陷；实际 {body!r}"
    )

    # 负控：这条端点**也会**答 409，但那是另一种 409 —— 没挂起的计划不会
    # 得到「被框架缺陷挂住」这个答复。所以上面那个 409 是挂起造成的。
    _seed_conflicting_plan(lane.plans_dir, CONTROL_PLAN_ID)
    control_status, control_body = _http_json(
        "POST", f"{lane.base_url}/api/verification/{CONTROL_PLAN_ID}/start", {}
    )
    assert isinstance(control_body, dict), (
        f"负控的答案必须是一个文档；实际 {control_body!r}"
    )
    assert control_body.get("error") != BLOCKED_ON_FRAMEWORK_FIX, (
        "没挂起的计划不该拿到「被框架缺陷挂住」的答复；"
        f"实际 HTTP {control_status}: {control_body!r}"
    )
    assert control_body.get("fingerprint") != lane.fingerprint, (
        f"负控的答案不该带着这一条链的指纹；实际 {control_body!r}"
    )

    # --- 合并之后：挂起块被放开了 -----------------------------------------
    block = _framework_fix_block(lane.db_path, PLAN_ID)
    assert not block.get("hold"), (
        f"修复合入之后挂起必须被放开；实际 {block!r}"
    )

    # --- 重跑：结论落了行 --------------------------------------------------
    assert lane.released >= 1, (
        f"合并那一步必须释放挂接；实际 {lane.released}"
    )
    assert len(lane.rerun) == 1, (
        f"只重跑挂接的那一个验证点；实际 {lane.rerun!r}"
    )
    outcome = lane.rerun[0]
    assert outcome["plan_id"] == PLAN_ID, f"重跑的是哪条计划；实际 {outcome!r}"
    assert outcome["vp_id"] == VP_ID, f"重跑的是哪个验证点；实际 {outcome!r}"
    assert outcome["new_status"] == "passed", (
        f"重跑之后的结论；实际 {outcome!r}"
    )

    # --- 两端的 HTTP 验收 ---------------------------------------------------
    status_body = _get(lane.base_url, f"/api/verification/{PLAN_ID}/status")
    results = status_body.get("vp_results")
    assert isinstance(results, list), f"vp_results 必须是列表；实际 {status_body!r}"
    assert results, (
        f"重跑之后真实服务必须给出非空的 vp_results；实际 {results!r}"
    )
    mine = [entry for entry in results if entry.get("vp_id") == VP_ID]
    assert len(mine) == 1, f"那个验证点得有一条结论；实际 {results!r}"
    assert mine[0].get("status") == "passed", (
        f"结论本身要带出来；实际 {mine[0]!r}"
    )


def test_snapshot_partition(lane: Lane) -> None:
    """真实 HTTP 下，修复计划只出现在 lane 自己那一列。"""
    fix_view = _get(lane.base_url, "/api/scheduler/state?kind=framework_fix")
    fixes = fix_view.get("framework_fixes")
    assert isinstance(fixes, list), (
        f"framework_fixes 必须是列表；实际 {fix_view!r}"
    )
    ids = [str(entry.get("plan_id")) for entry in fixes]
    assert lane.fix_plan_id in ids, (
        f"修复计划必须在 lane 自己的列里；实际 {ids!r}"
    )
    entry = next(
        item for item in fixes if str(item.get("plan_id")) == lane.fix_plan_id
    )
    assert entry.get("kind") == FRAMEWORK_FIX_KIND, (
        f"列里的行要认出自己的 kind；实际 {entry!r}"
    )
    assert str(entry.get("source", {}).get("plan_id")) == PLAN_ID, (
        f"这一行要指出缺陷是从哪条计划来的；实际 {entry!r}"
    )

    # lane 自己的列里没有普通计划的位置
    for name in ("queue", "running", "waiting", "parked"):
        assert fix_view.get(name) == [], (
            f"kind=framework_fix 的 {name} 必须是空的；实际 {fix_view.get(name)!r}"
        )
    assert fix_view.get("queue_length") == 0, (
        f"kind=framework_fix 的 queue_length 必须是 0；实际 {fix_view!r}"
    )

    # 普通计划那一列里没有修复计划
    plans_view = _get(lane.base_url, "/api/scheduler/state?kind=plans")
    assert plans_view.get("framework_fixes") == [], (
        f"kind=plans 的 framework_fixes 必须是空的；实际 {plans_view!r}"
    )

    # 全量视图里两者都在 —— 与上面那行放在一起看：**同一列**在 ``all``
    # 下装得下修复计划、在 ``plans`` 下装不下，所以那个过滤是干活了的，
    # 不是「那一列本来就是空的」。
    everything = _get(lane.base_url, "/api/scheduler/state")
    assert lane.fix_plan_id in [
        str(item.get("plan_id"))
        for item in everything.get("framework_fixes") or []
    ], f"全量视图里必须有这一列；实际 {everything!r}"


def test_no_real_db_written(lane: Lane) -> None:
    """真实库一个字节都没被动过。"""
    after = _real_db_fingerprint()

    assert after == lane.real_db_before, (
        "真实库必须原封不动："
        f"跑之前 {lane.real_db_before!r}，跑之后 {after!r}"
    )

    # 「没写」不能靠「真库本来就不存在」蒙混过去 —— 先证明这一轮**真的**
    # 写进了隔离的那份库，而且写进去的正是这条链的东西。
    conn = sqlite3.connect(str(lane.db_path))
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        fixes = conn.execute(
            "SELECT COUNT(*) FROM framework_fix_registry"
        ).fetchone()[0]
        results = conn.execute(
            "SELECT COUNT(*) FROM plan_verification_result"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "framework_fix_registry" in tables, (
        f"隔离的那份库必须有框架修复的表；实际 {sorted(tables)!r}"
    )
    assert int(fixes) >= 1, "这一轮必须真的登记过一个缺陷"
    assert int(results) >= 1, "这一轮必须真的落下过一条验证结果行"

    # 而那一份库**不是**真实库 —— 两条路径必须是两个文件。
    assert (
        Path(lane.real_db_before["path"]).resolve() != lane.db_path.resolve()
    ), (
        "隔离的那份库不能就是真实库；"
        f"真实库 {lane.real_db_before['path']!r}，实际写的 {lane.db_path}"
    )
