"""负控 E2E —— 证明框架修复的判据不是恒真的。

这份文件证明什么
----------------
任务 38 的主链路 E2E 证明的是**正例**：失败 → 复现 → 登记 → 生成修复 →
hold → 合并 → 重跑，一条链走通，两端的 HTTP 答案都对得上。

但正例证明不了另一件事：**判据本身**。如果「只要有失败就阻塞」是一条
恒真的规则，那么上面那条链照样走通 —— 它只是永远走通。真正要证的是
**反方向**：不是框架问题的失败（复现结论是 flake，或者命中根本不是验收
关键的）**不得**触发阻塞、**不得**生成修复计划。

所以这一份起同样的真服务（真的 uvicorn、真的 HTTP、真的 SQLite 文件），
喂进两种「不该阻塞」的失败，观察三件事：

* **框架修复分区**里没有它们（没有修复计划）；
* **守卫没有挂起它们**（``POST /api/verification/{id}/start`` 不答
  ``blocked_on_framework_fix``）；
* 命中本身按生产契约留在注册表里（``blocking=0`` + 登记动作），但那只是
  **签名登记**，不是 lane 里的条目。

判据在哪
--------
判定公式是**生产代码**：``framework_fix.classifier.decide``。这一份不重写
它，也不替它下结论 —— 它把生产判定接上一个最小形状的**调用方**（把判定
结果翻译成「建不建修复计划 / 挂不挂起」的动作），然后用真服务验收这个
调用方的后果。生产链路上这一段还没有接线，所以调用方的形状写在这里；
它写错了，这一份就会红。

每个负例里都带一个**阳性对照**：同一台服务、同一份库、同一段代码，先喂
一个不该阻塞的，再喂一个该阻塞的。阳性对照绿了，才说明观察通道是通的 ——
否则「分区是空的」可能只是因为分区根本没被读过。

三条判据
--------
* ``test_flake_not_blocking`` —— flake → 无框架分区条目；
* ``test_non_critical_not_blocking`` —— 非关键 → 不 hold；
* ``test_no_residual_state`` —— 负控之后主链路仍能跑通，结束无残留 running。

边界条件
--------
* **端口**：随机取一个空闲端口，**跳过 8000**（那上面跑着别的服务）；
* **状态库与计划树**：隔离在本次的 ``tmp_path`` 里；
* **调度器**：这一份**不开**调度器（自带的 ``scheduler.yaml`` 里
  ``enabled: false``）—— 负控要观察的是判据，不是调度器；让调度器去跑
  计划只会引入与判据无关的副作用（真跑一轮验证、物化 worktree）；
* **子进程退出码非 0**：子进程自己死了（不是我们叫它停的）→ 测试失败，
  并把服务端自己的输出打出来；
* **真实服务进程必须被终止**：夹具退出后断言进程已经不在了、端口已经还回来。

复用说明
--------
起服务的脚手架（``_boot`` / ``_wait_ready`` / ``_teardown`` / ``_http_json`` /
``_server_output``）与计划种子的那个助手按**文件路径** importlib 加载任务 38
的那一份（``test_framework_fix_lane_e2e.py``）—— 那个文件一个字节都不改，
这里也不依赖它能作为包被导入。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import socket
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

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

    加载之后要**登记进 ``sys.modules``**：被加载的模块里有 ``@dataclass``
    定义，而 ``dataclasses`` 回头要按 ``cls.__module__`` 去 ``sys.modules``
    里找那个模块的命名空间 —— 没登记的话，那一步拿到的是 ``None``，
    收集阶段就炸了（``AttributeError: 'NoneType' object has no attribute
    '__dict__'``）。
    """
    path = E2E_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load the sibling module at {path}"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_LANE_E2E = _load_sibling(
    "_pdt_e2e_framework_fix_lane", "test_framework_fix_lane_e2e.py"
)

_boot = _LANE_E2E._boot
_wait_ready = _LANE_E2E._wait_ready
_teardown = _LANE_E2E._teardown
_http_json = _LANE_E2E._http_json
_server_output = _LANE_E2E._server_output
_seed_conflicting_plan = _LANE_E2E._seed_conflicting_plan
_free_port_excluding = _LANE_E2E._free_port_excluding
_RESERVED_PORT = _LANE_E2E._RESERVED_PORT


# ---------------------------------------------------------------------------
# 这条链的常量
# ---------------------------------------------------------------------------

#: 负控的两条计划：复现结论是 flake 的那条、命中不是验收关键的那条。
FLAKE_PLAN_ID = "pflake"
NON_CRITICAL_PLAN_ID = "pnoncrit"

#: 阳性对照的计划：复现结论是确定性框架缺陷、命中是验收关键的。
BLOCKING_PLAN_ID = "pblock"

#: 三条计划各自的验证点。
FLAKE_VP_ID = "VP-FLAKE"
NON_CRITICAL_VP_ID = "VP-NONCRIT"
BLOCKING_VP_ID = "VP-BLOCK"

#: 修复计划所在的仓（运营字段：复现排在哪、合并串行到哪）。
REPO_NAME = "repo-neg"

#: 缺陷签名的材料 —— 框架文件集、错误类别、首错行。三条计划的首错行
#: **必须各不相同**：首错行是指纹的原料之一，三条用同一行就变成了同一个
#: 缺陷，第二条会复用第一条的注册表行。
FRAMEWORK_FILE = "backend/framework_fix/registry.py"
ERROR_CLASS = "AssertionError"
FLAKE_ERROR_LINE = "AssertionError: order dependence, 1 pass of 3"
NON_CRITICAL_ERROR_LINE = "AssertionError: log line assertion, not critical"
BLOCKING_ERROR_LINE = "AssertionError: framework lane dropped the hold"

#: 复现结论 —— 五分类闭集里的两个值。
FLAKE_CLASSIFICATION = "flake"
BLOCKING_CLASSIFICATION = "framework_deterministic"

#: 守卫的两种答复：被框架缺陷挂住 / 计划书不满足契约。
#:
#: 2026-11-13（main）：「产物冲突 → 409 artifact_conflict」那道门禁已被有意移除
#: （两类产物共存合法，``plan_artifacts.detect`` 按「任务优先」归入 dev 分支，
#: detect 的输出域里已经没有 ``conflict``），所以这里不再有冲突这个答复。
BLOCKED_ON_FRAMEWORK_FIX = "blocked_on_framework_fix"
VERIFICATION_PLAN_INVALID = "verification_plan_invalid"

#: 修复计划的五段任务模板（架构决策点 4）。
TASK_TEMPLATE_STEPS = ("locate", "fix", "targeted_test", "gate", "open_pr")

#: 修复计划在路由表里的那一列。
FRAMEWORK_FIX_KIND = "framework_fix"

#: 判定 → 登记动作的翻译。生产契约（架构决策点 2）给的是登记动作的闭集：
#: ``fix_plan`` / ``attach`` / ``register_signature`` / ``register_flake`` /
#: ``local_continue`` / ``hitl``。把复现结论翻译成登记动作是**调用方**的活
#: —— 生产链路上这一段还没有接线，所以它写在这里。
NON_BLOCKING_DISPOSITIONS = {
    "flake": "register_flake",
    "not_reproducible": "local_continue",
}
DEFAULT_NON_BLOCKING_DISPOSITION = "register_signature"


# ---------------------------------------------------------------------------
# 真服务
# ---------------------------------------------------------------------------
@dataclass
class Backend:
    """这一轮跑出来的事实。

    Attributes:
        base_url: 真实服务的根地址。
        port: 它听的端口。
        plans_dir: 计划树（隔离在 ``tmp_path`` 里）。
        db_path: 状态库（隔离在 ``tmp_path`` 里）。
        proc: 真实服务进程。
    """

    base_url: str
    port: int
    plans_dir: Path
    db_path: Path
    proc: Any


def _scheduler_settings(work: Path) -> Path:
    """这一份自带的调度器设置：**不开**调度器。

    负控要观察的是判据，不是调度器。开着调度器，它会去跑计划 —— 那会
    物化 worktree、真跑一轮验证，引入与判据无关的副作用。这一份自带一份
    设置文件（``PDT_SCHEDULER_FILE``），机器上操作者自己的那份不参与。
    """
    path = work / "scheduler.yaml"
    path.write_text(
        "scheduler:\n"
        "  enabled: false\n"
        "  dry_run: false\n"
        "  capacity: 4\n"
        "  min_runtime_seconds: 0\n"
        "  tick_seconds: 5\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )
    return path


@contextlib.contextmanager
def _running_backend(work: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Backend]:
    """起一个真的后端，隔离的状态库与计划树，退出时一定把它停掉。

    状态库与计划树都隔离在本次的 ``tmp_path`` 里：``tmp_path`` 是绝对路径，
    所以 ``config_paths`` 的相对路径锚定规则不会把它们拽到仓库里。

    退出时：子进程自己死了（不是我们叫它停的）→ 失败，并把服务端自己的
    输出（stdout 与 stderr 合成一路）打出来。
    """
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = work / "state.db"

    # 显式指一遍（而不是指望某个 autouse 夹具替我们指）：生产模块按
    # ``resolve_state_db_path()`` / ``resolve_plans_dir()`` 找这两个位置，
    # 环境变量是它们唯一的入口。
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_SCHEDULER_FILE", str(_scheduler_settings(work)))

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

    # 生产迁移 —— 框架修复的那些表在第一条链跑之前就在。
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    port = _free_port_excluding(_RESERVED_PORT)
    base_url = f"http://127.0.0.1:{port}"

    proc = _boot(work, port)
    try:
        _wait_ready(base_url, proc)
        yield Backend(
            base_url=base_url,
            port=port,
            plans_dir=plans_dir,
            db_path=db_path,
            proc=proc,
        )
    finally:
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


def _port_is_free(port: int) -> bool:
    """这个端口现在还能绑吗（服务真的把端口还回来了吗）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------
def _get(base_url: str, path: str) -> Dict[str, Any]:
    """一次 GET，答案必须是 200 且是一个文档。"""
    status, body = _http_json("GET", f"{base_url}{path}")
    assert status == 200, f"GET {path} answered HTTP {status}: {body!r}"
    assert isinstance(body, dict), f"GET {path} answered {body!r}"
    return body


def _partition_plan_ids(base_url: str) -> List[str]:
    """框架修复分区里的计划号。

    分区是**真服务**从真库里读出来的那一列（``plan_routing`` 里
    ``plan_kind='framework_fix'`` 的那些行），不是测试自己拼的。
    """
    document = _get(base_url, "/api/scheduler/state?kind=framework_fix")
    partition = document.get("framework_fixes")
    assert isinstance(partition, list), (
        f"framework_fixes 必须是列表；实际 {document!r}"
    )
    return [str(entry.get("plan_id")) for entry in partition]


def _rows(
    db_path: Path,
    sql: str,
    params: Tuple[Any, ...] = (),
) -> List[Dict[str, Any]]:
    """读真实库里的若干行。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _routing_row(db_path: Path, plan_id: str) -> Optional[Dict[str, Any]]:
    """``plan_routing`` 的那一行 —— 读的是真实库。"""
    rows = _rows(
        db_path,
        "SELECT plan_id, current_phase, plan_kind, framework_fix "
        "FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    )
    return rows[0] if rows else None


def _framework_fix_plan_rows(db_path: Path) -> List[Dict[str, Any]]:
    """路由表里属于框架修复 lane 的那些行。"""
    return _rows(
        db_path,
        "SELECT plan_id, current_phase, plan_kind FROM plan_routing "
        "WHERE plan_kind = ?",
        (FRAMEWORK_FIX_KIND,),
    )


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


def _is_held(db_path: Path, plan_id: str) -> bool:
    """这个计划此刻被框架缺陷挂住了吗（读的是生产读模型）。"""
    from framework_fix.read_model import _is_held as read_model_is_held

    return bool(read_model_is_held({}, _framework_fix_block(db_path, plan_id)))


def _display_state(
    db_path: Path,
    plan_id: str,
    fingerprint: str,
) -> Optional[str]:
    """这个计划此刻对外显示成什么状态（生产读模型）。"""
    from framework_fix.read_model import project_display_state
    from framework_fix.registry import FrameworkFixRegistry

    routing = _routing_row(db_path, plan_id) or {}
    registry_row = FrameworkFixRegistry(db_path=db_path).lookup(fingerprint)
    plan_state = {
        "current_phase": str(routing.get("current_phase") or ""),
        "framework_fix": _framework_fix_block(db_path, plan_id),
    }
    return project_display_state(plan_state, registry_row)


def _plan_dirs(plans_dir: Path) -> List[str]:
    """计划树里的目录名（按名字排）。"""
    return sorted(
        entry.name for entry in plans_dir.iterdir() if entry.is_dir()
    )


def _hit(
    *,
    plan_id: str,
    vp_id: str,
    error_line: str,
    acceptance_critical: bool,
) -> Any:
    """那次失败命中 —— 判定公式的全部输入事实。"""
    from framework_fix.classifier import FailureHit

    return FailureHit(
        vp_id=vp_id,
        plan_id=plan_id,
        error_class=ERROR_CLASS,
        first_error_line=error_line,
        framework_files=[FRAMEWORK_FILE],
        acceptance_critical=acceptance_critical,
        out_of_scope_change=False,
    )


def _reproduction(classification: str, error_line: str) -> Any:
    """一次复现的结论（证据用）。"""
    from framework_fix.repro import ReproAttempt, ReproductionResult

    return ReproductionResult(
        classification=classification,
        attempts=tuple(
            ReproAttempt(
                run=run,
                exit_code=1,
                timed_out=False,
                first_error_line=error_line,
                duration_s=0.5,
            )
            for run in (1, 2)
        ),
        clean_commit="0" * 40,
        tree_fingerprint="sha256:" + "1" * 64,
    )


def _drive_failure(
    *,
    plan_id: str,
    vp_id: str,
    error_line: str,
    classification: str,
    acceptance_critical: bool,
    db_path: Path,
    round_no: int = 1,
) -> Dict[str, Any]:
    """把一次失败走一遍「判定 → 登记 → 生成修复 → 挂起」。

    这一段就是**调用方**：判定公式本身是生产代码
    （``framework_fix.classifier.decide``），但把判定结果翻译成
    「建不建修复计划 / 挂不挂起」的动作是调用方的责任。生产链路上这一段
    还没有接线，所以最小形状写在这里 —— 它写错了，下面三条判据就会红。

    Args:
        plan_id: 命中所在的计划。
        vp_id: 命中所在的验证点。
        error_line: 首错行（指纹的原料之一）。
        classification: 复现结论（五分类闭集里的一个词）。
        acceptance_critical: 命中是否落在验收关键的验证点上。
        db_path: 隔离的那份状态库。
        round_no: 挂起发生在第几轮。

    Returns:
        这一轮的事实：判定结论、指纹、登记动作、修复计划号、挂接 id、
        以及「挂住了没有」。
    """
    from framework_fix.classifier import decide
    from framework_fix.lifecycle import FrameworkFixLifecycle
    from framework_fix.registry import FrameworkFixRegistry
    from framework_fix.spawner import build_envelope, spawn

    hit = _hit(
        plan_id=plan_id,
        vp_id=vp_id,
        error_line=error_line,
        acceptance_critical=acceptance_critical,
    )
    decision = decide(hit, reproduced=classification)

    registry = FrameworkFixRegistry(db_path=db_path)
    disposition = (
        "fix_plan"
        if decision.blocking
        else NON_BLOCKING_DISPOSITIONS.get(
            classification, DEFAULT_NON_BLOCKING_DISPOSITION
        )
    )
    fingerprint = registry.register_hit(
        {
            "plan_id": plan_id,
            "vp_id": vp_id,
            "framework_files": [FRAMEWORK_FILE],
            "error_class": ERROR_CLASS,
            "first_error_line": error_line,
            "classification": classification,
            "blocking": decision.blocking,
            "disposition": disposition,
            "repo": REPO_NAME,
        }
    )

    outcome: Dict[str, Any] = {
        "decision": decision,
        "fingerprint": fingerprint,
        "disposition": disposition,
        "fix_plan_id": "",
        "attach_id": "",
        "held": False,
    }

    if not decision.blocking:
        # 判据说「不阻塞」：**不生成修复计划，也不挂起**。
        #
        # 这一段就是负控要证的东西 —— 少了它，flake 与非关键命中都会被送进
        # lane，负控立刻红（见本文件第一次运行的那三条失败）。
        return outcome

    envelope = build_envelope(
        fingerprint=fingerprint,
        hit=hit,
        repro=_reproduction(classification, error_line),
        repo=REPO_NAME,
    )
    fix_plan_id = spawn(envelope)
    receipt = FrameworkFixLifecycle(registry=registry).hold(
        plan_id=plan_id,
        vp_id=vp_id,
        fingerprint=fingerprint,
        round_no=round_no,
        source="clean_main_repro",
    )
    outcome["fix_plan_id"] = fix_plan_id
    outcome["attach_id"] = str(receipt["attach_id"])
    outcome["held"] = True
    return outcome


def _start_verification(base_url: str, plan_id: str) -> Tuple[int, Any]:
    """问一次真实服务「这条计划能开验证吗」。"""
    return _http_json("POST", f"{base_url}/api/verification/{plan_id}/start", {})


def _registry(db_path: Path) -> Any:
    from framework_fix.registry import FrameworkFixRegistry

    return FrameworkFixRegistry(db_path=db_path)


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_flake_not_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """flake → 无框架分区条目。"""
    with _running_backend(tmp_path, monkeypatch) as backend:
        _seed_conflicting_plan(backend.plans_dir, FLAKE_PLAN_ID)

        flake = _drive_failure(
            plan_id=FLAKE_PLAN_ID,
            vp_id=FLAKE_VP_ID,
            error_line=FLAKE_ERROR_LINE,
            classification=FLAKE_CLASSIFICATION,
            acceptance_critical=True,
            db_path=backend.db_path,
        )

        # 生产判定本身说不阻塞。
        assert flake["decision"].disposition == "not_blocking", (
            f"flake 的判定结论；实际 {flake['decision']!r}"
        )
        assert flake["decision"].blocking is False, (
            f"flake 不得阻塞；实际 {flake['decision']!r}"
        )

        # 框架修复分区里没有它 —— 真服务从真库读出来的那一列。
        assert _partition_plan_ids(backend.base_url) == [], (
            "flake 不得进框架修复分区；实际 "
            f"{_partition_plan_ids(backend.base_url)!r}"
        )

        # 路由表里没有它的修复计划行。
        assert _framework_fix_plan_rows(backend.db_path) == [], (
            "flake 不得留下框架修复的路由行；实际 "
            f"{_framework_fix_plan_rows(backend.db_path)!r}"
        )

        # 计划树里没有多出修复计划的目录。
        assert _plan_dirs(backend.plans_dir) == [FLAKE_PLAN_ID], (
            f"flake 不得生成修复计划；实际目录 {_plan_dirs(backend.plans_dir)!r}"
        )

        # 守卫没有挂起它：端点答的是计划书校验错误，不是「被框架缺陷挂住」。
        status, body = _start_verification(backend.base_url, FLAKE_PLAN_ID)
        assert isinstance(body, dict), f"答复必须是一个文档；实际 {body!r}"
        assert body.get("error") != BLOCKED_ON_FRAMEWORK_FIX, (
            "flake 不得被挂起；实际 HTTP {0}: {1!r}".format(status, body)
        )
        # 2026-11-13（main）：产物冲突不再是 409 —— 共存合法、按「任务优先」
        # 归入 dev 分支，那道门禁已被有意移除。这颗种子计划的计划书故意不满足
        # 十章节契约，所以先被计划书校验挡下（422）。负控要证明的仍然成立：
        # 答案不是「被框架缺陷挂住」。
        assert body.get("error") == VERIFICATION_PLAN_INVALID, (
            "没有挂起的计划答的是计划书校验错误；实际 HTTP {0}: {1!r}".format(
                status, body
            )
        )
        assert _is_held(backend.db_path, FLAKE_PLAN_ID) is False, (
            "flake 的计划状态里不得有挂起块"
        )

        # 命中本身按生产契约留在注册表里 —— 但只是签名登记，不是 lane 的条目。
        hits = _registry(backend.db_path).list_hits(flake["fingerprint"])
        assert len(hits) == 1, f"一次命中一行；实际 {hits!r}"
        assert int(hits[0]["blocking"]) == 0, (
            f"命中的 blocking 必须是 0；实际 {hits[0]!r}"
        )
        assert str(hits[0]["disposition"]) == "register_flake", (
            f"flake 的登记动作；实际 {hits[0]!r}"
        )
        assert _registry(backend.db_path).list_attach(flake["fingerprint"]) == [], (
            "flake 不得有挂接行"
        )

        # --- 阳性对照：同一台服务、同一段代码，该阻塞的确实阻塞了 ------------
        _seed_conflicting_plan(backend.plans_dir, BLOCKING_PLAN_ID)
        blocking = _drive_failure(
            plan_id=BLOCKING_PLAN_ID,
            vp_id=BLOCKING_VP_ID,
            error_line=BLOCKING_ERROR_LINE,
            classification=BLOCKING_CLASSIFICATION,
            acceptance_critical=True,
            db_path=backend.db_path,
        )
        assert blocking["decision"].blocking is True, (
            f"对照的判定结论；实际 {blocking['decision']!r}"
        )
        partition = _partition_plan_ids(backend.base_url)
        assert blocking["fix_plan_id"] in partition, (
            "对照的修复计划必须在分区里（否则上面那些空分区是观察通道哑了）；"
            f"实际 {partition!r}"
        )
        assert FLAKE_PLAN_ID not in partition, (
            f"flake 仍然不得在分区里；实际 {partition!r}"
        )


def test_non_critical_not_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非关键 → 不 hold。"""
    with _running_backend(tmp_path, monkeypatch) as backend:
        _seed_conflicting_plan(backend.plans_dir, NON_CRITICAL_PLAN_ID)

        outcome = _drive_failure(
            plan_id=NON_CRITICAL_PLAN_ID,
            vp_id=NON_CRITICAL_VP_ID,
            error_line=NON_CRITICAL_ERROR_LINE,
            # 复现结论是「确定性框架缺陷」，但这个命中**不是验收关键的** ——
            # 判定公式的第一个合取项为假，结论已经定了：永不阻塞。
            classification=BLOCKING_CLASSIFICATION,
            acceptance_critical=False,
            db_path=backend.db_path,
        )

        assert outcome["decision"].disposition == "not_blocking", (
            f"非关键命中的判定结论；实际 {outcome['decision']!r}"
        )
        assert outcome["decision"].blocking is False, (
            f"非关键命中不得阻塞；实际 {outcome['decision']!r}"
        )

        # 不 hold：守卫不挂起它。
        status, body = _start_verification(backend.base_url, NON_CRITICAL_PLAN_ID)
        assert isinstance(body, dict), f"答复必须是一个文档；实际 {body!r}"
        assert body.get("error") != BLOCKED_ON_FRAMEWORK_FIX, (
            "非关键命中不得被挂起；实际 HTTP {0}: {1!r}".format(status, body)
        )
        # 2026-11-13（main）：产物冲突不再是 409 —— 共存合法、按「任务优先」
        # 归入 dev 分支，那道门禁已被有意移除。这颗种子计划的计划书故意不满足
        # 十章节契约，所以先被计划书校验挡下（422）。负控要证明的仍然成立：
        # 答案不是「被框架缺陷挂住」。
        assert body.get("error") == VERIFICATION_PLAN_INVALID, (
            "没有挂起的计划答的是计划书校验错误；实际 HTTP {0}: {1!r}".format(
                status, body
            )
        )
        assert _is_held(backend.db_path, NON_CRITICAL_PLAN_ID) is False, (
            "非关键命中的计划状态里不得有挂起块"
        )
        assert _registry(backend.db_path).list_attach(
            outcome["fingerprint"]
        ) == [], "非关键命中不得有挂接行"

        # 也不生成修复计划：分区里没有它，树里没有多出目录。
        assert _partition_plan_ids(backend.base_url) == [], (
            "非关键命中不得进框架修复分区；实际 "
            f"{_partition_plan_ids(backend.base_url)!r}"
        )
        assert _plan_dirs(backend.plans_dir) == [NON_CRITICAL_PLAN_ID], (
            "非关键命中不得生成修复计划；实际目录 "
            f"{_plan_dirs(backend.plans_dir)!r}"
        )

        # 显示状态：没有框架修复的可见状态（沿用原来的相位）。
        assert _display_state(
            backend.db_path, NON_CRITICAL_PLAN_ID, outcome["fingerprint"]
        ) is None, (
            "非关键命中不得有框架修复的显示状态；实际 "
            f"{_display_state(backend.db_path, NON_CRITICAL_PLAN_ID, outcome['fingerprint'])!r}"
        )

        # --- 阳性对照：同一台服务、同一段代码，该阻塞的确实被挂住了 ----------
        _seed_conflicting_plan(backend.plans_dir, BLOCKING_PLAN_ID)
        blocking = _drive_failure(
            plan_id=BLOCKING_PLAN_ID,
            vp_id=BLOCKING_VP_ID,
            error_line=BLOCKING_ERROR_LINE,
            classification=BLOCKING_CLASSIFICATION,
            acceptance_critical=True,
            db_path=backend.db_path,
        )
        assert blocking["decision"].blocking is True, (
            f"对照的判定结论；实际 {blocking['decision']!r}"
        )
        status, body = _start_verification(backend.base_url, BLOCKING_PLAN_ID)
        assert status == 409, (
            f"挂起期间真实服务必须拒绝开验证；实际 HTTP {status}: {body!r}"
        )
        assert isinstance(body, dict), f"答复必须是一个文档；实际 {body!r}"
        assert body.get("error") == BLOCKED_ON_FRAMEWORK_FIX, (
            "对照必须拿到「被框架缺陷挂住」的答复；实际 {0!r}".format(body)
        )
        assert _is_held(backend.db_path, BLOCKING_PLAN_ID) is True, (
            "对照的计划状态里必须有挂起块"
        )
        assert _display_state(
            backend.db_path, BLOCKING_PLAN_ID, blocking["fingerprint"]
        ) == BLOCKED_ON_FRAMEWORK_FIX, (
            "对照的显示状态是被挂住；实际 "
            f"{_display_state(backend.db_path, BLOCKING_PLAN_ID, blocking['fingerprint'])!r}"
        )


def test_no_residual_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """负控之后主链路仍能跑通，结束无残留 running。"""
    with _running_backend(tmp_path, monkeypatch) as backend:
        # --- 负控先跑：两个都不该阻塞的 ---------------------------------------
        _seed_conflicting_plan(backend.plans_dir, FLAKE_PLAN_ID)
        _seed_conflicting_plan(backend.plans_dir, NON_CRITICAL_PLAN_ID)

        _drive_failure(
            plan_id=FLAKE_PLAN_ID,
            vp_id=FLAKE_VP_ID,
            error_line=FLAKE_ERROR_LINE,
            classification=FLAKE_CLASSIFICATION,
            acceptance_critical=True,
            db_path=backend.db_path,
        )
        _drive_failure(
            plan_id=NON_CRITICAL_PLAN_ID,
            vp_id=NON_CRITICAL_VP_ID,
            error_line=NON_CRITICAL_ERROR_LINE,
            classification=BLOCKING_CLASSIFICATION,
            acceptance_critical=False,
            db_path=backend.db_path,
        )

        negative_partition = _partition_plan_ids(backend.base_url)
        negative_hold = bool(
            _is_held(backend.db_path, FLAKE_PLAN_ID)
            or _is_held(backend.db_path, NON_CRITICAL_PLAN_ID)
        )

        assert negative_partition == [], (
            f"负控不得进框架修复分区；实际 {negative_partition!r}"
        )
        assert negative_hold is False, "负控不得挂起任何东西"
        assert _plan_dirs(backend.plans_dir) == sorted(
            [FLAKE_PLAN_ID, NON_CRITICAL_PLAN_ID]
        ), (
            "负控不得留下修复计划的目录；实际目录 "
            f"{_plan_dirs(backend.plans_dir)!r}"
        )

        # --- 主链路在负控之后仍然跑得通 -------------------------------------
        _seed_conflicting_plan(backend.plans_dir, BLOCKING_PLAN_ID)
        blocking = _drive_failure(
            plan_id=BLOCKING_PLAN_ID,
            vp_id=BLOCKING_VP_ID,
            error_line=BLOCKING_ERROR_LINE,
            classification=BLOCKING_CLASSIFICATION,
            acceptance_critical=True,
            db_path=backend.db_path,
        )
        assert blocking["fix_plan_id"], "主链路必须生成修复计划"

        fix_dir = backend.plans_dir / blocking["fix_plan_id"]
        assert fix_dir.is_dir(), f"修复计划必须有目录；实际 {fix_dir} 不存在"
        tasks = json.loads((fix_dir / "tasks.json").read_text(encoding="utf-8"))
        titles = [str(task.get("title") or "") for task in tasks.get("tasks") or []]
        assert tuple(titles) == TASK_TEMPLATE_STEPS, (
            f"修复计划是固定五段；实际 {titles!r}"
        )

        status, body = _start_verification(backend.base_url, BLOCKING_PLAN_ID)
        assert status == 409, (
            f"挂起期间真实服务必须拒绝开验证；实际 HTTP {status}: {body!r}"
        )
        assert isinstance(body, dict), f"答复必须是一个文档；实际 {body!r}"
        assert body.get("error") == BLOCKED_ON_FRAMEWORK_FIX, (
            "主链路仍然被挂住；实际 {0!r}".format(body)
        )

        # --- 结束无残留 running ----------------------------------------------
        state = _get(backend.base_url, "/api/scheduler/state")
        assert state.get("running") == [], (
            f"结束不得有残留 running；实际 {state.get('running')!r}"
        )
        assert state.get("waiting") == [], (
            f"结束不得有残留 waiting；实际 {state.get('waiting')!r}"
        )
        assert state.get("parked") == [], (
            f"结束不得有残留 parked；实际 {state.get('parked')!r}"
        )
        assert _rows(backend.db_path, "SELECT * FROM scheduler_slot") == [], (
            "结束不得留下调度槽"
        )
        assert _rows(backend.db_path, "SELECT * FROM scheduler_queue") == [], (
            "结束不得留下调度队列"
        )

        summary = {
            "framework_fixes": negative_partition,
            "hold": negative_hold,
            "repair_tasks_generated": bool(titles),
        }
        # 负控证据摘要 —— ``pytest -s`` 时打出来，好让人核对输出示例。
        print(f"[framework-fix negative control] {summary}")
        assert summary == {
            "framework_fixes": [],
            "hold": False,
            "repair_tasks_generated": True,
        }, f"负控证据摘要；实际 {summary!r}"

    # --- 真实服务进程必须被终止 ----------------------------------------------
    assert backend.proc.poll() is not None, (
        "真实服务进程必须已经被终止；它还在跑"
    )
    assert _port_is_free(backend.port), (
        f"真实服务必须把端口 {backend.port} 还回来"
    )
