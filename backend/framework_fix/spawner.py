"""框架修复计划的生成器 —— 信封与五段任务模板。

这个模块要解决什么
------------------
PRD 要求「判定为 blocking 之后立即创建独立的框架修复计划」。要做到这一点，
需要一个地方把「失败签名 + 干净 main 上的复现证据 + 挂接清单 + 影响面」收成
一份**固定形状的信封**，再由它派生出**固定五段**的任务清单::

    定位 → 修复 → 针对性测试 → 门禁 → 提交 PR

信封同时是**生成输入**和**验收契约**（架构决策点 4）：它既是创建计划时要读的
那份材料，也是事后核对「这个计划是不是按模板生成的」时读的那份材料。两份
材料分开写，就会有一个跑在前面、一个落在后面，验收时读到的是旧的那份。

四条不变式
----------
1. **一个签名最多一个修复计划。** 已经有 open 的修复计划时，:func:`spawn`
   返回它的计划号，不新建、不重跑。
2. **生成失败不留半截计划目录。** 目录、任务文件、信封、路由行，任何一步失败
   都把已经写下的东西撤掉，错误照原样抛出去 —— 半截计划比没有计划更坏：
   读的人看见一个目录，以为修复已经在跑。
3. **任务文本里没有未落实的占位。** 五段模板的每一段都写死了它要碰的文件、
   它要跑的命令、它凭什么算做完。一句「按需补充」会让子 agent 去猜，猜出来
   的东西没人能核对。
4. **同一个签名两次生成得到同一个计划号。** 计划号来自指纹，不是来自时钟或
   计数器；已经存在时直接返回旧的那个。

还有两条在入口上接起来的不变式
------------------------------
5. **一个仓库同时只有一个未合入的修复 PR。** 建计划之前先占这个仓库的合并槽
   （:meth:`FrameworkFixRegistry.acquire_merge_lease`）：占到了的修复计划才有
   资格开 PR，占不到的那个计划把自己的等待写进框架修复块
   （``merge_slot='waiting'`` / ``merge_slot_reason='waiting_merge_slot'``），
   容量账本据此不接纳它 —— 于是第二个同仓修复 PR 开不出来。
6. **语义复核判为同因就并进同一个缺陷。** 机械指纹认不出的「症状不同、根因
   相同」在这里被问一次模型；判为同因就写成别名（:meth:`write_alias`），
   于是「同一个缺陷一个修复计划」这条不变式对**语义**上的重复也成立。

模型是**外面递进来的**
----------------------
:func:`spawn` 收一个 ``model_call``（收一段提示词、回一段文本）来做语义复核。
本模块**不** import agent / server：内核不知道模型长什么样、也不知道它连不
得上 —— 那是调用方的事。没给 ``model_call`` 时不做复核，也不留痕：没有模型
可问就不判，审计链上记一条没问过任何人的判断比不记更糟。

与快速通道的关系
----------------
修复计划**复用**快速通道的导入路径产出 ``tasks.json``：同一个写入器
(:class:`backend.task_manager.TaskManager`)、同一份审计
(:func:`backend.fast_lane.validate_tasks`)、同一个材料文件 ``import.json``。
但它**不调用模型**：模板是写死的五段，模型只会改写任务文本，不会改写判定
（测试设计决策点 4 的「dry-run 任务模板」）。自建一条任务生成链路会变成第二套
执行机制 —— 那是 PRD 明确不想要的。

计划落地之后落在 ``queued`` 相位上，交给调度器收；「创建即开跑」的那道接缝在
执行器侧（它读的是同一个 ``plan_kind`` 列），不在这里。

包形态
------
与同包的 ``classifier.py`` / ``registry.py`` 一致：
``backend/framework_fix/`` 是 PEP 420 的隐式命名空间包，没有 ``__init__.py``。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from config_paths import PROJECT_ROOT, resolve_plans_dir, resolve_state_db_path
except ImportError:  # pragma: no cover
    from backend.config_paths import (  # type: ignore
        PROJECT_ROOT,
        resolve_plans_dir,
        resolve_state_db_path,
    )

try:  # pragma: no cover
    from fast_lane import store_import_payload, validate_tasks
except ImportError:  # pragma: no cover
    from backend.fast_lane import store_import_payload, validate_tasks  # type: ignore

try:  # pragma: no cover
    from framework.clock import utcnow_iso
except ImportError:  # pragma: no cover
    from backend.framework.clock import utcnow_iso  # type: ignore

try:  # pragma: no cover
    from framework.ids import derive_plan_id
except ImportError:  # pragma: no cover
    from backend.framework.ids import derive_plan_id  # type: ignore

try:  # pragma: no cover
    from framework_fix.classifier import (
        REPRO_CLASSIFICATIONS,
        FailureDecision,
        FailureHit,
        decide,
        semantic_review,
    )
except ImportError:  # pragma: no cover
    from backend.framework_fix.classifier import (  # type: ignore
        REPRO_CLASSIFICATIONS,
        FailureDecision,
        FailureHit,
        decide,
        semantic_review,
    )

try:  # pragma: no cover
    from framework_fix.fingerprint import canonical_key, compute_fingerprint
except ImportError:  # pragma: no cover
    from backend.framework_fix.fingerprint import (  # type: ignore
        canonical_key,
        compute_fingerprint,
    )

try:  # pragma: no cover
    from framework_fix.registry import (
        DEFAULT_CLASSIFICATION,
        DISPOSITION_ATTACH,
        DISPOSITION_FIX_PLAN,
        DISPOSITION_HITL,
        DISPOSITION_LOCAL_CONTINUE,
        DISPOSITION_REGISTER_FLAKE,
        DISPOSITION_REGISTER_SIGNATURE,
        REGISTRY_STATUS_FIXED,
        FrameworkFixRegistry,
    )
except ImportError:  # pragma: no cover
    from backend.framework_fix.registry import (  # type: ignore
        DEFAULT_CLASSIFICATION,
        DISPOSITION_ATTACH,
        DISPOSITION_FIX_PLAN,
        DISPOSITION_HITL,
        DISPOSITION_LOCAL_CONTINUE,
        DISPOSITION_REGISTER_FLAKE,
        DISPOSITION_REGISTER_SIGNATURE,
        REGISTRY_STATUS_FIXED,
        FrameworkFixRegistry,
    )

try:  # pragma: no cover
    from framework_fix.lifecycle import FrameworkFixLifecycle
except ImportError:  # pragma: no cover
    from backend.framework_fix.lifecycle import (  # type: ignore
        FrameworkFixLifecycle,
    )

try:  # pragma: no cover
    from framework_fix.repro import ReproductionResult
except ImportError:  # pragma: no cover
    from backend.framework_fix.repro import ReproductionResult  # type: ignore

try:  # pragma: no cover
    from framework_fix.scheduling import (
        FRAMEWORK_FIX_KIND,
        WAITING_MERGE_SLOT_REASON,
    )
except ImportError:  # pragma: no cover
    from backend.framework_fix.scheduling import (  # type: ignore
        FRAMEWORK_FIX_KIND,
        WAITING_MERGE_SLOT_REASON,
    )

try:  # pragma: no cover
    from state_machine.db.connection import open as open_db
except ImportError:  # pragma: no cover
    from backend.state_machine.db.connection import open as open_db  # type: ignore

try:  # pragma: no cover
    from state_machine.db.schema import migrate
except ImportError:  # pragma: no cover
    from backend.state_machine.db.schema import migrate  # type: ignore

try:  # pragma: no cover
    from state_machine.repositories.routing_repository import RoutingRepository
except ImportError:  # pragma: no cover
    from backend.state_machine.repositories.routing_repository import (  # type: ignore
        RoutingRepository,
    )

try:  # pragma: no cover
    from task import SubTask
except ImportError:  # pragma: no cover
    from backend.task import SubTask  # type: ignore

try:  # pragma: no cover
    from task_manager import TaskManager
except ImportError:  # pragma: no cover
    from backend.task_manager import TaskManager  # type: ignore


__all__ = [
    "TASK_TEMPLATE_STEPS",
    "build_envelope",
    "dispatch_hit",
    "spawn",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 词表与常量
# ---------------------------------------------------------------------------

#: 修复计划的任务模板 —— 固定五段，顺序也是固定的（架构决策点 4）。
#:
#: 这五个词既是任务标题，也是 ``envelope["steps"]`` / ``task_template`` 的
#: 取值：标题用这个词，验收时才能机械地核对「这个计划是不是这五段」，而不必
#: 去读描述里的措辞。
TASK_TEMPLATE_STEPS: Tuple[str, ...] = (
    "locate",
    "fix",
    "targeted_test",
    "gate",
    "open_pr",
)

#: 报告交付物的落点。相对 ``project_dir``（检出根），落在 ``.pdt/`` 下面 ——
#: 那是被 gitignore 的运行期目录（见仓库根的 ``.gitignore``），报告不会污染
#: 检出，也不会被误当成一次代码改动。
_REPORT_DIR = ".pdt/framework_fix"

#: 复现 / 针对性测试 / 门禁 / 套件的命令。写法取自
#: ``docs/development/testing.md`` 里记录的本仓约定：单文件用
#: ``backend/.venv/bin/python3 -m pytest``，整棵树用
#: ``scripts/run_tests.sh``（它是规范入口，会自己找 venv）。
_PYTEST = "backend/.venv/bin/python3 -m pytest"
_TEST_RUNNER = "bash scripts/run_tests.sh"
_GATE_RUNNER = "bash scripts/ci_gate.sh"

#: 既有测试套件的范围：单元 + 集成。
_SUITE_PATHS = ("backend/tests/unit", "backend/tests/integration")

#: 基准分支。复现是在干净 main 上做的，验收也拿 main 当基准。
_BASE_REF = "origin/main"

#: 合并槽的两个词 —— 这个计划自己占着这个仓库的合并槽，还是在等别人释放。
#:
#: 同一批词也由 :func:`framework_fix.scheduling.decorate_plans` 投影到读模型
#: 上（它从租约表**推导**这两个词）；这里写的是计划自己那一侧的事实。两侧
#: 必须一致，否则面板上会出现「块里说等着、面板说拿着」这种自相矛盾。
MERGE_SLOT_HELD = "held"
MERGE_SLOT_WAITING = "waiting"


#: 判定结论 → 登记动作。
#:
#: 登记动作说的是「登记这一行时做了什么」，判定结论说的是「这是什么性质
#: 的失败」。两者不是一回事：同一个 ``not_blocking``，flake 登记成
#: ``register_flake``，只在本地复现的登记成 ``local_continue``，其余的
#: （比如不是验收关键的命中）只登记签名。
#:
#: 这张表与 ``backend/tests/e2e/test_framework_fix_lane_e2e_negative.py``
#: 里负控 E2E 自己写的映射是同一张 —— 判定公式只有一个，映射也只能有一份。
_NON_BLOCKING_DISPOSITIONS: Dict[str, str] = {
    "flake": DISPOSITION_REGISTER_FLAKE,
    "not_reproducible": DISPOSITION_LOCAL_CONTINUE,
}

#: 上面那张表没写到的非阻塞分类：只登记签名。
DEFAULT_NON_BLOCKING_DISPOSITION = DISPOSITION_REGISTER_SIGNATURE

#: 判定入口的动作词 —— 一次命中处置之后发生了什么。
ACTION_SPAWNED = "spawned"
ACTION_ATTACHED = "attached"
ACTION_REGISTERED = "registered"
ACTION_NOT_BLOCKING = "not_blocking"
ACTION_HITL = "hitl"
ACTION_NEEDS_REPRO = "needs_repro"
ACTION_SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------
def build_envelope(
    *,
    fingerprint: str,
    hit: FailureHit,
    repro: ReproductionResult,
    repo: str,
) -> Dict[str, Any]:
    """把一次阻塞命中收成修复计划的信封。

    Args:
        fingerprint: 缺陷指纹。**必填** —— 没有它就没有去重的键，「同一签名
            只产生一个修复计划」这条不变式就没有依据。
        hit: 这次命中（任务 5 的 :class:`FailureHit`）。它给出签名与影响面：
            框架文件集、错误类别、首错行。
        repro: 干净 main 上的复现结论（任务 15 的
            :class:`ReproductionResult`）。它给出**证据**：复现分类、基准
            commit、树的指纹、两次 attempt 的观测。没有它，这个计划就没有
            理由相信自己修的是个框架缺陷。
        repo: 仓库标识（复现排在哪、合并串行到哪）。它同时用来定这次修复在
            哪个检出里做 —— 给的是一个存在的目录就用它，否则用本仓。

    Returns:
        信封字典。它同时是生成输入和验收契约，形状见模块文档。

    Raises:
        ValueError: 指纹为空；或签名的材料不足（没有框架文件 / 错误类别）。
    """
    fp = _require_text(fingerprint, "fingerprint")
    signature = _signature_of(hit)
    reproduction = _reproduction_of(repro, hit)
    root = _checkout_root(repo)

    envelope: Dict[str, Any] = {
        # 同一个值写两遍：``kind`` 是架构契约里的叫法，``plan_kind`` 是
        # ``plan_routing`` 那一列的叫法。读的人不必猜哪一个才是权威的。
        "kind": FRAMEWORK_FIX_KIND,
        "plan_kind": FRAMEWORK_FIX_KIND,
        "fingerprint": fp,
        "signature": signature,
        "reproduction": reproduction,
        # 挂接清单在生成时是空的：真正的清单要问注册表，而注册表要等到
        # 计划建出来才知道挂到谁身上。:func:`spawn` 把它补上。
        "attached_plans": [],
        "impact_scope": {
            "framework_files": list(signature["framework_files"]),
            "affected_plan_count": 0,
        },
        # 判定公式里的两个事实。它们**不在指纹里** —— 指纹认的是「哪个缺陷」，
        # 判定问的是「这次失败算不算阻塞」；把两者混进指纹会让同一个缺陷在
        # 不同判定下变成两个缺陷。
        "blocking_inputs": {
            "acceptance_critical": bool(
                getattr(hit, "acceptance_critical", False)
            ),
            "out_of_scope_change": bool(
                getattr(hit, "out_of_scope_change", False)
            ),
        },
        "acceptance": [],
        "task_template": list(TASK_TEMPLATE_STEPS),
        "steps": TASK_TEMPLATE_STEPS,
        "tasks": len(TASK_TEMPLATE_STEPS),
        "task_specs": [],
        "repo": str(repo or ""),
        "source_plan_id": str(getattr(hit, "plan_id", "") or ""),
        "source_vp_id": str(getattr(hit, "vp_id", "") or ""),
        "project_dir": str(root),
        "branch": _branch_name(fp),
        "covering_tests": _covering_test_paths(
            signature["framework_files"], root
        ),
    }
    envelope["reproduction"]["command"] = _repro_command(envelope["covering_tests"])
    envelope["acceptance"] = _acceptance(envelope, 0)
    envelope["task_specs"] = _render_task_specs(envelope)
    return envelope


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------
def _decision_of(
    envelope: Dict[str, Any],
    *,
    unclassified_rounds: int = 0,
) -> FailureDecision:
    """这份信封对应的判定结论。

    判定要读的是命中本身的事实（验收关键与否、改动是否越界）与复现结论；
    信封把两者都带着（``blocking_inputs`` 与 ``reproduction``），所以这里
    不必回头找调用方 —— 这也是 :func:`spawn` 能自己再校验一遍的原因。

    判定公式只有一份（:func:`framework_fix.classifier.decide`）：这里**不**
    重写它，只把它的结论用起来。
    """
    signature = envelope.get("signature") or {}
    inputs = envelope.get("blocking_inputs") or {}
    reproduction = envelope.get("reproduction") or {}
    hit = FailureHit(
        vp_id=str(envelope.get("source_vp_id") or "") or None,
        plan_id=str(envelope.get("source_plan_id") or "") or None,
        error_class=str(signature.get("error_class") or ""),
        first_error_line=str(signature.get("first_error_line") or ""),
        framework_files=list(signature.get("framework_files") or []),
        acceptance_critical=bool(inputs.get("acceptance_critical")),
        out_of_scope_change=bool(inputs.get("out_of_scope_change")),
    )
    classification = str(reproduction.get("classification") or "") or None
    return decide(
        hit,
        reproduced=classification,
        unclassified_rounds=unclassified_rounds,
    )


def _disposition_of(
    classification: str, decision: FailureDecision,
) -> str:
    """登记动作 —— 判定结论翻译成「登记这一行时做了什么」。

    阻塞的命中建计划（``fix_plan``）；不阻塞的按分类登记：flake 登记成
    ``register_flake``，只在本地复现的登记成 ``local_continue``，其余的
    （归不了类的、不是验收关键的）只登记签名。连续多轮归不了类的那一轮
    登记成 ``hitl`` —— 那一轮问过人了。
    """
    if decision.disposition == "exhausted":
        return DISPOSITION_HITL
    if decision.blocking:
        return DISPOSITION_FIX_PLAN
    return _NON_BLOCKING_DISPOSITIONS.get(
        classification, DEFAULT_NON_BLOCKING_DISPOSITION
    )


def _unclassified_rounds(
    registry: FrameworkFixRegistry, fingerprint: str,
) -> int:
    """这个签名**连续**多少轮归不了类。

    从最新的登记往回数，数到分类不再是 ``unclassified`` 为止。只数连续的：
    归过一次类之后又坏了，那是从头开始的又一轮，不该把上一轮的疲惫算进来。

    数不出来就当零轮：轮数只决定「什么时候升级问人」，猜多了会白问一次人，
    猜少了会多熬两轮 —— 后者更糟，但也只是多熬两轮。
    """
    try:
        hits = registry.list_hits(fingerprint)
    except Exception as exc:  # noqa: BLE001 - 数不出来就当没数过
        logger.warning(
            "framework_fix_unclassified_rounds_unreadable: fingerprint=%s "
            "error=%s",
            fingerprint,
            exc,
        )
        return 0
    rounds = 0
    for row in reversed(hits):
        if str(row.get("classification") or "") != "unclassified":
            break
        rounds += 1
    return rounds


def _already_asked(
    registry: FrameworkFixRegistry, fingerprint: str,
) -> bool:
    """这个签名是不是已经升级问过人了？

    问过一次就够了 —— 同一个问题问第二遍不会得到第二个答案，只会多一张
    等着人的卡片。
    """
    try:
        hits = registry.list_hits(fingerprint)
    except Exception as exc:  # noqa: BLE001 - 读不到就当没问过
        logger.warning(
            "framework_fix_already_asked_unreadable: fingerprint=%s error=%s",
            fingerprint,
            exc,
        )
        return False
    return any(
        str(row.get("disposition") or "") == DISPOSITION_HITL for row in hits
    )


def _escalate_unclassified(
    registry: FrameworkFixRegistry,
    *,
    fingerprint: str,
    plan_id: str,
    vp_id: str = "",
) -> Optional[str]:
    """连续多轮归不了类 —— 问人。返回决策 id（问不成就是 ``None``）。

    问在**命中所在的那条计划**上：那是被这件事挡住验收的计划。决策落在
    注册表那本库里 —— 缺陷的登记与它的决策必须同库，否则读的人看到「问过
    人了」却找不到该问谁。
    """
    owner = str(plan_id or "").strip()
    if not owner:
        logger.warning(
            "framework_fix_unclassified_not_asked: fingerprint=%s error=%s",
            fingerprint,
            "the hit belongs to no plan, so there is nobody to ask",
        )
        return None
    if _already_asked(registry, fingerprint):
        return None
    try:
        lifecycle = FrameworkFixLifecycle(registry=registry)
        return lifecycle.ask_for_unclassified_escalation(
            fingerprint=fingerprint,
            plan_id=owner,
            vp_id=vp_id,
        )
    except Exception as exc:  # noqa: BLE001 - 问不到人不掀翻已发生的事
        logger.warning(
            "framework_fix_unclassified_not_asked: fingerprint=%s plan=%s "
            "error=%s",
            fingerprint,
            owner,
            exc,
        )
        return None


def _register_hit(
    registry: FrameworkFixRegistry,
    *,
    fingerprint: str,
    signature: Dict[str, Any],
    classification: str,
    decision: FailureDecision,
    disposition: str,
    plan_id: str,
    vp_id: str = "",
    repo: str = "",
    fix_plan_id: str = "",
) -> Optional[str]:
    """把这次命中登记进注册表 —— 写的是判定的事实，不是常量。

    ``blocking`` 写的是 :attr:`FailureDecision.blocking` 的**真实值**；登记
    动作写的是这一轮做了什么（建了计划 / 挂上了 / 只登记了签名 / 登记成
    flake / 本地继续 / 问过人了）。

    指纹是别名时（语义复核合并过）不另起一行：同一个缺陷登记成两个，会让
    「这个缺陷被登记过吗」给出两个答案。

    Returns:
        登记的指纹；登记不了就是 ``None``。
    """
    canonical = compute_fingerprint(
        framework_files=signature["framework_files"],
        error_class=signature["error_class"],
        first_error_line=signature["first_error_line"],
    )
    if canonical != fingerprint:
        return None
    plan = str(plan_id or "").strip() or fingerprint
    return registry.register_hit(
        {
            "plan_id": plan,
            "vp_id": vp_id,
            "framework_files": list(signature["framework_files"]),
            "error_class": signature["error_class"],
            "first_error_line": signature["first_error_line"],
            "classification": classification,
            "blocking": bool(decision.blocking),
            "disposition": disposition,
            "repo": repo,
            "fix_plan_id": fix_plan_id or plan,
        }
    )


def spawn(
    envelope: Dict[str, Any],
    *,
    model_call: Optional[Callable[[str], str]] = None,
) -> str:
    """按信封创建修复计划，返回它的计划号。

    这个函数是「框架修复命中」的**处置入口**：它先把机械指纹认不出的重复
    并掉（语义复核），再占住这个仓库的合并槽，最后才建计划。

    Args:
        envelope: :func:`build_envelope` 产出的那份信封。
        model_call: 语义复核时调模型的函数（收一段提示词，回一段文本）。
            **注入**进来的 —— 本模块不知道模型长什么样，也不该知道：内核
            不 import agent / server。没给 ``model_call`` 时不做语义复核。

    Returns:
        新建的（或已存在的）修复计划号。

    Raises:
        ValueError: 信封缺关键字段。
        Exception: 生成过程中的任何失败都会照原样抛出去，并且**不留半截计划
            目录** —— 已经写下的目录与路由行会被撤掉。
    """
    # 拷一份再动手：这一轮要往信封上补合并槽与复核结论，调用方手里的那份
    # 是它的材料，不该被入口改写。
    env = dict(_validated_envelope(envelope))
    fingerprint = env["fingerprint"]

    registry = FrameworkFixRegistry()

    # 闸门。只有「验收关键」且「已复现为框架缺陷 / 改动越界」的命中才允许
    # 建修复计划 —— 判据是判定公式本身（:func:`framework_fix.classifier.
    # decide`），这里不重写它，只是把它的结论用起来。
    #
    # 这一步在语义复核**之前**：复核只影响指纹归谁，不影响这次失败算不算
    # 阻塞。先判后动，省掉一次模型调用。
    decision = _decision_of(
        env,
        unclassified_rounds=_unclassified_rounds(registry, fingerprint),
    )
    if not decision.blocking:
        # 不阻塞：登记这一行，**不建计划**。
        #
        # 建了计划就是「一次 flake 换来一个框架修复计划」—— 那正是这条闸门
        # 要挡住的事。登记动作写的是判定的事实（flake / 只在本地复现 / 只
        # 登记签名），不是一句「都归到框架那边」。
        classification = str(env["reproduction"]["classification"])
        disposition = _disposition_of(classification, decision)
        if decision.disposition == "exhausted":
            # 连续多轮归不了类 —— 问人，而不是继续猜。
            _escalate_unclassified(
                registry,
                fingerprint=fingerprint,
                plan_id=str(env.get("source_plan_id") or ""),
                vp_id=str(env.get("source_vp_id") or ""),
            )
        try:
            _register_hit(
                registry,
                fingerprint=fingerprint,
                signature=env["signature"],
                classification=classification,
                decision=decision,
                disposition=disposition,
                plan_id=str(env.get("source_plan_id") or ""),
                vp_id=str(env.get("source_vp_id") or ""),
                repo=str(env.get("repo") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 登记失败不该掀翻这一轮
            logger.warning(
                "framework_fix_hit_not_registered: fingerprint=%s error=%s",
                fingerprint,
                exc,
            )
        return ""

    # 语义复核先做：它可能把这一行并进别人的规范指纹名下，而「已经有没有
    # 修复计划」这个问题要在并完之后才问得对。
    conclusion = _semantic_review(registry, env, model_call=model_call)
    canonical = registry.resolve_fingerprint(fingerprint)
    if canonical != fingerprint:
        # 这一行现在是别人的别名了 —— 修的是那个规范缺陷，不是这个别名。
        env["fingerprint"] = canonical
        env["branch"] = _branch_name(canonical)
    if conclusion is not None:
        env["semantic_review"] = conclusion

    # 已经修好的签名又被撞上 —— 延迟命中。
    #
    # **不新建修复计划**：这个签名已经有主人了 —— 那个修复计划合入过了。
    # 再建一个，等于同一个缺陷有两条修复线，两条线的结论互相矛盾。走的是
    # 同一条路径：挂接到已修复的记录上，在含修复的树上重跑，只追加结果。
    #
    # 这一步在「已经有修复计划」**之前** —— 已修复的签名一定有一个修复计划
    # （就是修好它的那一个），先问「有没有计划」就永远问不到延迟命中。
    registry_row = registry.lookup(canonical)
    if registry_row is not None and str(
        registry_row.get("status") or ""
    ) == REGISTRY_STATUS_FIXED:
        return _late_hit(registry, env, canonical, registry_row)

    existing = _existing_fix_plan(canonical)
    if existing:
        # 一个签名一个修复计划。已经有人在修，就不新建、不重跑。
        return existing

    # 补挂接（架构决策点 4）：此前只登记了签名的命中，在这里挂到这个即将
    # 创建的修复计划上。它们不持有执行、不计入阻塞数，但修复合入后必须重验，
    # 漏掉它们就等于漏掉一批「修好了但没人发现自己被修好了」的计划。
    registry.attach_registered_hits(canonical)

    plan_id = _allocate_plan_id(canonical)
    repo = str(env.get("repo") or "")
    merge_slot = _take_merge_slot(registry, repo, plan_id)
    env["merge_slot"] = merge_slot
    env["merge_slot_reason"] = (
        None if merge_slot != MERGE_SLOT_WAITING else WAITING_MERGE_SLOT_REASON
    )

    plan_dir = resolve_plans_dir() / plan_id

    plan_dir.mkdir(parents=True, exist_ok=False)
    try:
        env = _with_attach_list(env, registry)
        _write_envelope(plan_dir, env)
        store_import_payload(plan_dir, env)
        _write_tasks(plan_dir, env)
        _write_routing(plan_id, env)
        _ensure_registered(registry, env, plan_id, decision)
        _point_registry_at_fix_plan(registry, env, plan_id)
    except BaseException:
        # 半截计划比没有计划更坏：读的人看见一个目录，以为修复已经在跑。
        _discard_plan(plan_dir, plan_id)
        if merge_slot == MERGE_SLOT_HELD:
            # 计划没了，槽也还回去 —— 否则这个仓库的合并槽会被一个**不存在**
            # 的计划占住，直到有人手工清理。
            registry.release_merge_lease(repo, plan_id)
        raise
    return plan_id


def dispatch_hit(
    *,
    hit: FailureHit,
    reproduction: Optional[ReproductionResult] = None,
    repo: str = "",
    model_call: Optional[Callable[[str], str]] = None,
    registry: Optional[FrameworkFixRegistry] = None,
) -> Dict[str, Any]:
    """判定入口 —— 一次命中该被谁管，答一遍。

    这是生产链路上**唯一**的判定入口。调用方（修复轮）把一次失败交给它，
    它按 :func:`framework_fix.classifier.decide` 的结论决定：

    * 阻塞且已复现为框架缺陷 → 建修复计划（一个签名一个）；
    * 阻塞但已经有人建过 → 挂到既有计划上（不产生第二个计划）；
    * flake → 登记成 flake，本地继续；
    * 只在本地复现 → 登记成「本地继续」；
    * 归不了类 → 登记签名，本轮不建计划；连续多轮归不了类 → 问人；
    * 还没复现 → 本轮判不了（``needs_repro``），不登记也不建计划。

    Args:
        hit: 这次命中 —— 判定公式的全部输入事实。
        reproduction: 干净 main 上的复现结论。``None`` 表示还没复现：本轮
            判不了，不登记也不建计划。
        repo: 仓库标识（复现排在哪、合并串行到哪）。
        model_call: 语义复核时调模型的函数（收一段提示词，回一段文本）。
        registry: 注册表。``None`` 表示用默认的那本库。

    Returns:
        一个字典：

        * ``action`` —— ``spawned`` / ``attached`` / ``not_blocking`` /
          ``hitl`` / ``needs_repro`` / ``skipped``；
        * ``blocking`` —— 判定结论；
        * ``disposition`` —— 登记动作（没登记就是空字符串）；
        * ``fingerprint`` —— 这次命中的指纹；
        * ``fix_plan_id`` —— 建了或挂上的修复计划（没有就是空字符串）；
        * ``decision`` —— 判定结论本身（判不了就是 ``None``）。
    """
    reg = registry if registry is not None else FrameworkFixRegistry()

    try:
        signature = _signature_of(hit)
        fingerprint = compute_fingerprint(
            framework_files=signature["framework_files"],
            error_class=signature["error_class"],
            first_error_line=signature["first_error_line"],
        )
    except ValueError as exc:
        # 材料不足：这不是一次「判下来不阻塞」的命中，是一次**判不了**的
        # 命中。调用方要能分清这两者 —— 前者有结论，后者没有。
        logger.warning("framework_fix_hit_not_dispatched: error=%s", exc)
        return {
            "action": ACTION_SKIPPED,
            "blocking": False,
            "disposition": "",
            "fingerprint": "",
            "fix_plan_id": "",
            "decision": None,
        }

    classification = ""
    if reproduction is not None:
        classification = str(
            _reproduction_of(reproduction, hit)["classification"]
        )

    decision = decide(
        hit,
        reproduced=classification or None,
        unclassified_rounds=_unclassified_rounds(reg, fingerprint),
    )
    disposition = _disposition_of(classification, decision)
    plan_id = str(getattr(hit, "plan_id", "") or "")
    vp_id = str(getattr(hit, "vp_id", "") or "")

    if decision.disposition == "needs_repro":
        # 还没复现 —— 本轮判不了。不登记（还不知道这是不是框架缺陷），不建
        # 计划，也不挂起：这一轮照旧由本计划自己修。
        return {
            "action": ACTION_NEEDS_REPRO,
            "blocking": False,
            "disposition": "",
            "fingerprint": fingerprint,
            "fix_plan_id": "",
            "decision": decision,
        }

    if not decision.blocking:
        # 不阻塞：登记，但**不建计划**。
        #
        # 建了计划就是「一次 flake 换来一个框架修复计划」—— 那正是这条闸门
        # 要挡住的事。
        action = {
            "unclassified": ACTION_REGISTERED,
            "exhausted": ACTION_HITL,
        }.get(decision.disposition, ACTION_NOT_BLOCKING)
        if decision.disposition == "exhausted":
            # 连续多轮归不了类 —— 问人，而不是继续猜。
            _escalate_unclassified(
                reg,
                fingerprint=fingerprint,
                plan_id=plan_id,
                vp_id=vp_id,
            )
        _register_or_warn(
            reg,
            fingerprint=fingerprint,
            signature=signature,
            classification=classification or DEFAULT_CLASSIFICATION,
            decision=decision,
            disposition=disposition,
            plan_id=plan_id,
            vp_id=vp_id,
            repo=repo,
        )
        return {
            "action": action,
            "blocking": False,
            "disposition": disposition,
            "fingerprint": fingerprint,
            "fix_plan_id": "",
            "decision": decision,
        }

    # 阻塞：建计划，或者挂到已经建好的那个上。
    #
    # 「已经建好的那个」要先问 —— 挂上去不产生第二个计划，这是「一个签名一个
    # 修复计划」这条不变式在命中这一侧的样子。
    canonical = reg.resolve_fingerprint(fingerprint)
    existing = _existing_fix_plan(canonical)
    if existing:
        _register_or_warn(
            reg,
            fingerprint=fingerprint,
            signature=signature,
            classification=classification or DEFAULT_CLASSIFICATION,
            decision=decision,
            disposition=DISPOSITION_ATTACH,
            plan_id=plan_id,
            vp_id=vp_id,
            repo=repo,
            fix_plan_id=existing,
        )
        return {
            "action": ACTION_ATTACHED,
            "blocking": True,
            "disposition": DISPOSITION_ATTACH,
            "fingerprint": fingerprint,
            "fix_plan_id": existing,
            "decision": decision,
        }

    envelope = build_envelope(
        fingerprint=fingerprint,
        hit=hit,
        repro=reproduction if reproduction is not None else _no_reproduction(hit),
        repo=repo,
    )
    spawned = spawn(envelope, model_call=model_call)
    return {
        "action": ACTION_SPAWNED,
        "blocking": True,
        "disposition": DISPOSITION_FIX_PLAN,
        "fingerprint": fingerprint,
        "fix_plan_id": spawned,
        "decision": decision,
    }


def _no_reproduction(hit: FailureHit) -> ReproductionResult:
    """一份「还没有复现结论」的复现。

    只有一种情况会用到它：判定说阻塞，但阻塞的理由是**改动越界**而不是
    「已复现为框架缺陷」—— 那条理由与复现无关，不需要先跑一次复现。计划
    的信封要一份复现块，这里给它一份**明说「没有结论」**的：分类是
    ``unclassified``，两次 attempt 都没有。
    """
    return ReproductionResult(
        classification="unclassified",
        attempts=(),
        clean_commit="",
        tree_fingerprint="",
    )


def _register_or_warn(
    registry: FrameworkFixRegistry,
    *,
    fingerprint: str,
    signature: Dict[str, Any],
    classification: str,
    decision: FailureDecision,
    disposition: str,
    plan_id: str,
    vp_id: str = "",
    repo: str = "",
    fix_plan_id: str = "",
) -> Optional[str]:
    """登记这次命中 —— 登记不了就记一笔，不掀翻这一轮。

    登记失败不该把修复轮带崩：这一轮不登记，最坏的结果是下一轮再登记一次；
    掀翻这一轮，整轮修复就白跑了。
    """
    try:
        return _register_hit(
            registry,
            fingerprint=fingerprint,
            signature=signature,
            classification=classification,
            decision=decision,
            disposition=disposition,
            plan_id=plan_id,
            vp_id=vp_id,
            repo=repo,
            fix_plan_id=fix_plan_id,
        )
    except Exception as exc:  # noqa: BLE001 - 登记失败不该掀翻这一轮
        logger.warning(
            "framework_fix_hit_not_registered: fingerprint=%s error=%s",
            fingerprint,
            exc,
        )
        return None


def _late_hit(
    registry: FrameworkFixRegistry,
    env: Dict[str, Any],
    fingerprint: str,
    registry_row: Dict[str, Any],
) -> str:
    """延迟命中：挂到已修复的记录上，在含修复的树上重跑，返回那个修复计划。

    **不新建修复计划。** 这个签名已经修好过一次 —— 它的修复计划就是那个主人。
    这次命中挂到它名下，在**含修复**的那棵树上重跑，只追加结果。

    重跑用的树是那个修复计划干活的树：修复是在那里做的，也只有那里带着修复。

    Returns:
        那个修复计划的计划号。
    """
    fix_plan_id = str(registry_row.get("fix_plan_id") or "").strip()
    hit_plan_id = str(env.get("source_plan_id") or "").strip()
    vp_id = str(env.get("source_vp_id") or "").strip()
    if not hit_plan_id or not vp_id:
        # 这一次命中不属于任何 (计划, 验证点) —— 记不下也重跑不了。
        # 但**仍然不新建修复计划**：这个签名已经有主人了。
        logger.warning(
            "framework_fix_late_hit_without_a_source: the hit on %s names "
            "no plan or no verification point; it is not rerun",
            fingerprint,
        )
        return fix_plan_id
    lifecycle = FrameworkFixLifecycle(registry)
    lifecycle.handle_late_hit(
        hit_plan_id=hit_plan_id,
        fingerprint=fingerprint,
        vp_id=vp_id,
        worktree=_project_dir_of(fix_plan_id),
    )
    return fix_plan_id


def _project_dir_of(plan_id: str) -> Optional[Path]:
    """这个计划干活的树 —— 它的修复就带着那棵树。

    执行行是这件事的权威：它记着这个计划究竟在哪里干的活，所以合入之后的
    修复就在那棵树上。没有执行行就没有树 —— 那时重跑记下的指纹是
    ``unknown``，而不是猜一个。
    """
    if not plan_id:
        return None
    try:
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
    except ImportError:  # pragma: no cover - 两条路径等价
        from backend.state_machine.repositories.execution_repository import (  # type: ignore
            ExecutionRepository,
        )
    try:
        conn = open_db(resolve_state_db_path())
    except Exception:  # noqa: BLE001 - 没有库就没有树
        return None
    try:
        summary = ExecutionRepository(conn).summary(plan_id)
    except Exception:  # noqa: BLE001 - 读不到执行行就没有树
        summary = None
    finally:
        conn.close()
    if not summary:
        return None
    project_dir = str(summary.get("project_dir") or "").strip()
    if not project_dir:
        return None
    return Path(project_dir)


def _take_merge_slot(
    registry: FrameworkFixRegistry, repo: str, plan_id: str
) -> Optional[str]:
    """占住这个仓库的合并槽；占不到就是在等。

    没有仓库就没有合并串行可言 —— 这时既不占槽，也不等（``None``）。

    占到了是 :data:`MERGE_SLOT_HELD`；被别人占着是
    :data:`MERGE_SLOT_WAITING` —— 那个计划**不**能开 PR：容量账本看到槽在
    别人手里就不接纳它（理由词 ``waiting_merge_slot``），于是第二个同仓修复
    PR 开不出来。
    """
    if not repo:
        return None
    if registry.acquire_merge_lease(repo, plan_id):
        return MERGE_SLOT_HELD
    return MERGE_SLOT_WAITING


def _semantic_review(
    registry: FrameworkFixRegistry,
    envelope: Dict[str, Any],
    *,
    model_call: Optional[Callable[[str], str]],
) -> Optional[Dict[str, Any]]:
    """问模型「这次失败与候选是不是同一个根因」，判为同因就写成别名。

    这是**入口**上的复核：机械指纹认不出的「症状不同但根因相同」在这里被
    判掉，判掉之后两个签名归到同一行注册表下，「同一个缺陷一个修复计划」
    这条不变式才真的成立。

    判为同因才写别名；**同因与不同因都**往审计链上追加一条 ``agent_judged``
    —— 「问过模型、模型怎么说」本身就是要留痕的事实，否则下一次没人知道这个
    签名为什么没有并进别人。

    没有 ``model_call`` 时**不**复核：没有模型可问就不判，也不留痕。

    Returns:
        复核结论（能直接放进信封的普通字典），没复核时是 ``None``。
    """
    if model_call is None:
        return None

    fingerprint = str(envelope["fingerprint"])
    signature = envelope["signature"]
    hit = FailureHit(
        vp_id=str(envelope.get("source_vp_id") or "") or None,
        plan_id=str(envelope.get("source_plan_id") or "") or None,
        error_class=str(signature["error_class"]),
        first_error_line=str(signature["first_error_line"]),
        framework_files=list(signature["framework_files"]),
        acceptance_critical=True,
        out_of_scope_change=False,
    )
    candidates = [
        row
        for row in registry.list_registry()
        if str(row.get("fingerprint") or "") != fingerprint
    ]
    verdict = semantic_review(hit, candidates, model_call)
    conclusion: Dict[str, Any] = {
        "same": bool(verdict.same),
        "matched_fingerprint": verdict.matched_fingerprint,
        "alias": bool(verdict.alias),
        "reason": str(verdict.reason),
    }
    registry.append_event(
        "agent_judged",
        {
            "same": conclusion["same"],
            "reason": conclusion["reason"],
            "matched_fingerprint": conclusion["matched_fingerprint"],
            "fingerprint": fingerprint,
        },
        fingerprint=fingerprint,
        fix_plan_id=None,
    )
    if verdict.same and verdict.alias and verdict.matched_fingerprint:
        registry.write_alias(fingerprint, verdict.matched_fingerprint)
    return conclusion


# ---------------------------------------------------------------------------
# 信封的构造
# ---------------------------------------------------------------------------
def _require_text(value: Any, name: str) -> str:
    """一段必须有的文字。"""
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    return text


def _signature_of(hit: FailureHit) -> Dict[str, Any]:
    """签名 —— 指纹的原料，也是「修哪里」的答案。

    校验交给 :func:`framework_fix.fingerprint.canonical_key`（材料不足时它
    抛错），但**留存的**是原文：文件集按它排好序去重（那是无损的），错误
    类别与首错行按原文留存（归一化会把 ``AssertionError`` 写成
    ``assertionerror``，那是**有损**的，而任务文本要给人看）。

    指纹由这同一份材料算出（归一化在算指纹的那一步发生），所以信封里的
    签名与注册表里的签名算出来是同一个指纹。
    """
    files = list(getattr(hit, "framework_files", None) or [])
    error_class = str(getattr(hit, "error_class", "") or "")
    first_error_line = str(getattr(hit, "first_error_line", "") or "")
    key = canonical_key(
        framework_files=files,
        error_class=error_class,
        first_error_line=first_error_line,
    )
    return {
        "framework_files": list(key["framework_files"]),
        "error_class": error_class.strip(),
        "first_error_line": first_error_line.strip(),
    }


def _reproduction_of(
    repro: ReproductionResult, hit: FailureHit
) -> Dict[str, Any]:
    """复现证据摘要 —— 没有它，计划没有理由相信自己修的是个框架缺陷。"""
    attempts = list(getattr(repro, "attempts", None) or [])
    classification = str(getattr(repro, "classification", "") or "")
    if classification not in REPRO_CLASSIFICATIONS:
        # 分类词表是闭集：不在里面的词说明这份复现结论不是本模块认得的结论。
        # 早在这里拒绝，比让它一路走到注册表那一步才炸要好 —— 那时计划目录
        # 已经建出来了。
        raise ValueError(
            "the reproduction's classification must be one of "
            f"{REPRO_CLASSIFICATIONS}; got {classification!r}"
        )
    first_error_line = ""
    for attempt in attempts:
        line = str(getattr(attempt, "first_error_line", "") or "")
        if line:
            first_error_line = line
            break
    if not first_error_line:
        first_error_line = str(getattr(hit, "first_error_line", "") or "")
    return {
        "classification": classification,
        "base_commit": str(getattr(repro, "clean_commit", "") or ""),
        "tree_fingerprint": str(getattr(repro, "tree_fingerprint", "") or ""),
        "node_id": str(getattr(hit, "vp_id", "") or getattr(hit, "plan_id", "") or ""),
        "first_error_line": first_error_line,
        # 证据文件由本模块在计划目录里写下（见 ``_write_envelope``），所以这里
        # 记的是**计划目录内**的相对路径 —— 它不认得机器上的绝对路径。
        "evidence_path": "framework_fix/reproduction.json",
        "attempts": [
            {
                "run": int(getattr(attempt, "run", 0) or 0),
                "exit_code": getattr(attempt, "exit_code", None),
                "timed_out": bool(getattr(attempt, "timed_out", False)),
                "first_error_line": str(
                    getattr(attempt, "first_error_line", "") or ""
                ),
                "duration_s": float(getattr(attempt, "duration_s", 0.0) or 0.0),
            }
            for attempt in attempts
        ],
    }


def _checkout_root(repo: str) -> Path:
    """这次修复在哪个检出里做。

    ``repo`` 是仓库标识；它给的是一个存在的目录时就用那个目录，否则用本仓
    根目录（框架缺陷修的就是本仓的框架）。
    """
    text = str(repo or "").strip()
    if text:
        candidate = Path(text).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    return Path(PROJECT_ROOT).resolve()


def _branch_name(fingerprint: str) -> str:
    """修复分支名。指纹短码 + 一个固定前缀，猜不出来也认不错。"""
    digest = fingerprint.split(":")[-1][:12]
    safe = "".join(ch for ch in digest if ch.isalnum())
    return f"fix/framework-fix-{safe or 'unknown'}"


def _covering_test_paths(
    framework_files: Sequence[str], root: Path
) -> List[str]:
    """覆盖这些框架文件的测试模块（检出相对路径）。

    找的是「覆盖这个框架文件的那个测试模块」：同一个包下的同名模块优先，
    找不到就找 ``test_<名字>_*`` 的兄弟模块，再找不到就全树找同名模块。
    一个都找不到时退回该包自己的测试子树，再退回单元测试子树 —— 退回值也
    必须是真路径，不能是一句「相关的测试」。
    """
    tests_root = root / "backend" / "tests"
    found: List[str] = []
    packages: List[str] = []
    for raw in framework_files:
        rel = Path(str(raw))
        stem = rel.stem
        package = rel.parent.name
        if package and package not in packages:
            packages.append(package)
        chosen: Optional[Path] = None
        for base in ("unit", "integration"):
            candidate = tests_root / base / package / f"test_{stem}.py"
            if candidate.is_file():
                chosen = candidate
                break
        if chosen is None:
            for base in ("unit", "integration"):
                directory = tests_root / base / package
                if not directory.is_dir():
                    continue
                siblings = sorted(directory.glob(f"test_{stem}_*.py"))
                if siblings:
                    chosen = siblings[0]
                    break
        if chosen is None:
            matches = sorted(tests_root.rglob(f"test_{stem}.py"))
            if matches:
                chosen = matches[0]
        if chosen is None:
            continue
        relative = chosen.relative_to(root).as_posix()
        if relative not in found:
            found.append(relative)
    if found:
        return found
    for package in packages:
        for base in ("unit", "integration"):
            candidate = tests_root / base / package
            if candidate.is_dir():
                relative = candidate.relative_to(root).as_posix()
                if relative not in found:
                    found.append(relative)
    if not found and (tests_root / "unit").is_dir():
        found.append("backend/tests/unit")
    return found


def _repro_command(covering: Sequence[str]) -> str:
    """复现命令 —— 干净 main 上跑出来的那条。

    :class:`ReproductionResult` 只带结论，不带命令（命令是复现队列那一侧
    的事实），所以命令在这里由「覆盖这些框架文件的测试模块」推导出来：
    缺陷在这些模块里复现，命令就是跑这些模块。
    """
    if not covering:
        covering = ["backend/tests/unit"]
    return f"{_PYTEST} {' '.join(covering)} -xvs"


def _targeted_command(
    covering: Sequence[str], framework_files: Sequence[str], root: Path
) -> str:
    """针对性测试命令 —— 覆盖清单 + 框架文件所在包的整棵测试子树。

    范围限定在**这个缺陷所在的包**：把整个测试树拉进来跑一遍，不是针对性
    测试，是重跑门禁（门禁那一步已经在做那件事了）。
    """
    paths = list(covering)
    for package_dir in _package_test_dirs(framework_files, root):
        if package_dir not in paths:
            paths.append(package_dir)
    if not paths:
        paths = ["backend/tests/unit"]
    return f"{_PYTEST} {' '.join(paths)} -xvs"


def _package_test_dirs(
    framework_files: Sequence[str], root: Path
) -> List[str]:
    """框架文件所在包在测试树里的对应子树。"""
    dirs: List[str] = []
    tests_root = root / "backend" / "tests"
    packages: List[str] = []
    for raw in framework_files:
        package = Path(str(raw)).parent.name
        if package and package not in packages:
            packages.append(package)
    for package in packages:
        for base in ("unit", "integration"):
            candidate = tests_root / base / package
            if candidate.is_dir():
                relative = candidate.relative_to(root).as_posix()
                if relative not in dirs:
                    dirs.append(relative)
    return dirs


def _suite_command() -> str:
    """既有测试套件的命令。"""
    return f"{_TEST_RUNNER} {' '.join(_SUITE_PATHS)}"


def _gate_command() -> str:
    """门禁命令。"""
    return _GATE_RUNNER


def _acceptance(envelope: Dict[str, Any], attach_count: int) -> List[Dict[str, Any]]:
    """验收清单 —— 「修好了」在这份材料里是什么意思。"""
    reproduction = envelope["reproduction"]
    return [
        {
            "type": "clean_main_repro_green",
            "command": reproduction["command"],
            "base_ref": _BASE_REF,
        },
        {
            "type": "attached_vps_pass_on_merged_tree",
            "attach_count": attach_count,
        },
        {
            "type": "no_new_regression",
            "command": _gate_command(),
        },
    ]


# ---------------------------------------------------------------------------
# 五段任务模板
# ---------------------------------------------------------------------------
def _render_task_specs(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把信封渲染成五段任务。

    每一段都写死了三件事：**碰哪些文件**、**跑哪条命令**、**凭什么算做完**。
    少写任何一件，子 agent 就得自己猜，猜出来的东西没有人能核对。
    """
    signature = envelope["signature"]
    reproduction = envelope["reproduction"]
    files = list(signature["framework_files"])
    covering = list(envelope.get("covering_tests") or [])
    attached = list(envelope.get("attached_plans") or [])
    project_dir = str(envelope["project_dir"])
    fingerprint = str(envelope["fingerprint"])
    branch = str(envelope["branch"])

    file_block = "\n".join(f"  {path}" for path in files)
    covering_block = (
        "\n".join(f"  {path}" for path in covering) if covering else "  backend/tests/unit"
    )
    attach_block = "\n".join(
        "  计划 {plan_id} / 验证点 {vp_id} / 第 {round} 轮 / 来源 {source}".format(
            plan_id=item["plan_id"],
            vp_id=item["vp_id"],
            round=item["round"],
            source=item["source"],
        )
        for item in attached
    )
    if not attach_block:
        attach_block = "  没有人在等这个缺陷的修复"

    repro_command = reproduction["command"]
    exit_code = _exit_code_text(reproduction["attempts"])
    header = "\n".join(
        [
            f"缺陷指纹：{fingerprint}",
            f"错误类别：{signature['error_class']}",
            f"首错行：{signature['first_error_line']}",
            "涉及的框架文件：",
            file_block,
            f"复现基准：干净 main，commit {reproduction['base_commit']}",
            f"复现命令（在干净 main 上跑，退出码 {exit_code}）：",
            f"  {repro_command}",
        ]
    )

    locate_description = "\n".join(
        [
            "定位这个框架缺陷落在框架文件的哪一行、哪个函数。",
            "",
            header,
            "",
            "定位要求：",
            "1. 对上面列出的每一个框架文件，给出「路径:行号」与出错的函数名。",
            "2. 报告里原样抄录复现命令的首错行，与上面的首错行逐字相同。",
            f"3. 报告里写明复现是在 commit {reproduction['base_commit']} 上跑出来的。",
            "",
            f"完成判据：报告 {_REPORT_DIR}/locate.md 存在且非空，上面三条每一条"
            "都能在报告里逐字找到。",
        ]
    )

    fix_description = "\n".join(
        [
            "只改上面列出的框架文件，把这个框架缺陷修掉。",
            "",
            header,
            "",
            "可以改的文件（超出这份清单的改动一律不许）：",
            file_block,
            "",
            f"修之前的观测：复现命令退出码 {exit_code}，"
            f"首错行 {signature['first_error_line']}。",
            "",
            "完成判据：",
            "1. 改完之后同一条复现命令的退出码是 0，首错行不再出现。",
            "2. 本任务实际改动的文件集合与上面的清单完全一致"
            "（用 git diff --name-only origin/main..HEAD 核对）。",
        ]
    )

    targeted_description = "\n".join(
        [
            "跑覆盖这个缺陷的针对性测试，确认它真的被修好了。",
            "",
            header,
            "",
            "覆盖这个缺陷的测试模块：",
            covering_block,
            "",
            "针对性测试命令：",
            f"  {_targeted_command(covering, files, Path(project_dir))}",
            "",
            "完成判据：",
            "1. 针对性测试命令退出码 0，0 failed。",
            "2. 上面那份覆盖清单里的每一个测试模块都被这条命令跑到。",
            f"3. 报告 {_REPORT_DIR}/targeted_test.md 里记录了命令原文、退出码与"
            "失败用例数（失败用例数必须是 0）。",
        ]
    )

    gate_description = "\n".join(
        [
            "跑门禁与既有测试套件，确认这次修复没有引入回归。",
            "",
            header,
            "",
            "门禁命令：",
            f"  {_gate_command()}",
            "既有测试套件命令：",
            f"  {_suite_command()}",
            "",
            "完成判据：",
            "1. 门禁命令退出码 0。",
            "2. 既有测试套件命令退出码 0，0 failed。",
            f"3. 报告 {_REPORT_DIR}/gate.md 里记录了两条命令的原文、退出码与失败数"
            "（失败数必须是 0）。",
        ]
    )

    open_pr_description = "\n".join(
        [
            "把这个框架修复开成 PR。",
            "",
            header,
            "",
            f"分支：{branch}",
            "创建 PR 的命令：",
            f"  {_pr_create_command(branch, fingerprint, attached)}",
            "查看 PR 的命令：",
            f"  gh pr view {branch} --json url,body,state",
            "查看 CI 结论的命令：",
            f"  gh pr checks {branch}",
            "",
            f"待挂接清单（{len(attached)} 条，修复合入后它们要按树重演）：",
            attach_block,
            "",
            "完成判据：",
            f"1. PR 已创建，PR 描述里逐字包含缺陷指纹 {fingerprint}。",
            "2. PR 描述里逐字包含上面待挂接清单里的每一条（计划号与验证点号）。",
            "3. git diff --name-only origin/main..HEAD 的输出只包含上面列出的框架文件。",
            f"4. 报告 {_REPORT_DIR}/open_pr.md 里记录了 PR 地址与分支名。",
            f"5. 报告 {_REPORT_DIR}/open_pr.md 里有下面这两行**机器可读**字段，"
            "各占一行、冒号后只写值：",
            "   pr_url: 刚开出来的那个 PR 的完整地址（形如 "
            "https://github.com/OWNER/REPO/pull/NUMBER，OWNER/REPO/NUMBER 换成真实值）",
            "   ci_conclusion: gh pr checks 的结论（全绿写 passed，没绿写 failed）",
        ]
    )

    specs = [
        {
            "id": "1",
            "title": "locate",
            "description": locate_description,
            "test_command": repro_command,
            "project_dir": project_dir,
            "model_type": "medium",
            "depends_on": [],
            "deliverable": "report",
            "report_path": f"{_REPORT_DIR}/locate.md",
        },
        {
            "id": "2",
            "title": "fix",
            "description": fix_description,
            "test_command": repro_command,
            "project_dir": project_dir,
            "model_type": "complex",
            "depends_on": ["1"],
            "deliverable": "code",
            "files_to_modify": list(files),
        },
        {
            "id": "3",
            "title": "targeted_test",
            "description": targeted_description,
            "test_command": _targeted_command(covering, files, Path(project_dir)),
            "project_dir": project_dir,
            "model_type": "medium",
            "depends_on": ["2"],
            "deliverable": "report",
            "report_path": f"{_REPORT_DIR}/targeted_test.md",
        },
        {
            "id": "4",
            "title": "gate",
            "description": gate_description,
            "test_command": _gate_command(),
            "project_dir": project_dir,
            "model_type": "medium",
            "depends_on": ["3"],
            "deliverable": "report",
            "report_path": f"{_REPORT_DIR}/gate.md",
        },
        {
            "id": "5",
            "title": "open_pr",
            "description": open_pr_description,
            "test_command": f"gh pr view {branch} --json url,body,state",
            "project_dir": project_dir,
            "model_type": "medium",
            "depends_on": ["4"],
            "deliverable": "report",
            "report_path": f"{_REPORT_DIR}/open_pr.md",
        },
    ]
    return specs


def _exit_code_text(attempts: Sequence[Dict[str, Any]]) -> str:
    """复现的退出码。没有退出码（跑不起来 / 超时被带走）就说没有。"""
    for attempt in attempts:
        code = attempt.get("exit_code")
        if code is not None:
            return str(code)
    return "无"


def _pr_create_command(
    branch: str, fingerprint: str, attached: Sequence[Dict[str, Any]]
) -> str:
    """创建 PR 的命令 —— 描述里逐字写死，不留「按需补充」。

    PR 描述里必须带指纹与挂接清单（架构决策点 4）：合入之后，靠这两样东西
    才能知道「哪些计划在等这次修复」。
    """
    attach_text = "；".join(
        "计划 {plan_id} 验证点 {vp_id}".format(
            plan_id=item["plan_id"], vp_id=item["vp_id"]
        )
        for item in attached
    ) or "没有人在等这个缺陷的修复"
    short = fingerprint.split(":")[-1][:12]
    body = f"指纹 {fingerprint}；挂接清单：{attach_text}"
    return (
        "gh pr create --base main "
        f"--head {branch} "
        f'--title "fix: 修复框架缺陷 {short}" '
        f'--body "{body}"'
    )


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def _write_envelope(plan_dir: Path, envelope: Dict[str, Any]) -> None:
    """把信封与复现证据写进计划目录。

    信封落在 ``<plan>/framework_fix/envelope.json``（架构决策点 4 定的位置），
    复现证据落在它旁边的 ``reproduction.json`` —— 信封里 ``evidence_path``
    指的就是那一份。
    """
    target_dir = plan_dir / "framework_fix"
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(json.dumps(envelope, ensure_ascii=False, default=str))
    (target_dir / "envelope.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (target_dir / "reproduction.json").write_text(
        json.dumps(payload["reproduction"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_tasks(plan_dir: Path, envelope: Dict[str, Any]) -> None:
    """写出 ``tasks.json`` —— 走快速通道那一个写入器。

    同一个 :class:`TaskManager`、同一份审计
    (:func:`backend.fast_lane.validate_tasks`)。审计不通过就抛错：一份自己
    都过不了审计的任务清单，不该被写进计划目录。
    """
    specs = list(envelope.get("task_specs") or [])
    tasks_data = {"tasks": specs}
    problems = validate_tasks(tasks_data, str(envelope["project_dir"]))
    if problems:
        raise ValueError(
            "the generated framework-fix task list failed the fast-lane "
            "audit: " + "; ".join(problems)
        )
    manager = TaskManager(plan_dir)
    manager.requirement = _requirement_text(envelope)
    manager.tasks = [SubTask(**spec) for spec in specs]
    manager.save_tasks()


def _requirement_text(envelope: Dict[str, Any]) -> str:
    """计划的一句话说明 —— 写进 ``tasks.json`` 的 ``requirement``。"""
    signature = envelope["signature"]
    files = "、".join(signature["framework_files"])
    return (
        f"修复框架缺陷 {envelope['fingerprint']}："
        f"{signature['error_class']}（{signature['first_error_line']}），"
        f"涉及 {files}"
    )


def _write_routing(plan_id: str, envelope: Dict[str, Any]) -> None:
    """写下路由行 —— ``plan_kind`` 与框架修复块。

    ``plan_kind='framework_fix'`` 就是执行器那道接缝读的那一列：读成
    ``framework_fix`` 的计划自己开跑，不用等普通批次。
    """
    block = _framework_fix_block(envelope)
    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert_if_missing(plan_id, "queued")
        conn.execute(
            "UPDATE plan_routing SET plan_kind = ?, framework_fix = ?, "
            "updated_at = ? WHERE plan_id = ?",
            (
                FRAMEWORK_FIX_KIND,
                json.dumps(block, ensure_ascii=False),
                utcnow_iso(),
                plan_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _framework_fix_block(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """路由行里的框架修复块。"""
    attached = list(envelope.get("attached_plans") or [])
    blocked = sum(1 for item in attached if item.get("status") == "blocked")
    registered = sum(1 for item in attached if item.get("status") == "registered")
    return {
        "fingerprint": str(envelope["fingerprint"]),
        "status": "active",
        "source_plan_id": str(envelope.get("source_plan_id") or ""),
        "source_round": 0,
        "attach_blocked_count": blocked,
        "attach_registered_count": registered,
        "repo": str(envelope.get("repo") or ""),
        "branch": str(envelope.get("branch") or ""),
        # 合并槽：自己占着（``held``）还是在等别人释放（``waiting`` +
        # ``waiting_merge_slot``）。没有仓库就没有槽，两项都是 ``None``。
        "merge_slot": envelope.get("merge_slot"),
        "merge_slot_reason": envelope.get("merge_slot_reason"),
        "created_at": utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def _existing_fix_plan(fingerprint: str) -> Optional[str]:
    """这个签名是不是已经有修复计划了？有就把它的计划号返回。

    两条路：

    * 路由表里 ``plan_kind='framework_fix'`` 的计划，框架修复块里的指纹就是
      它 —— 这是**权威**的一份：本模块创建的每一个计划都在那里留了指纹。
    * 注册表行上的 ``fix_plan_id`` —— 别人（注册表的写入方）指向的那个修复
      计划。它只有在那个计划确实是修复计划时才作数。
    """
    plans = _framework_fix_plans()
    for plan_id, block in plans.items():
        if block.get("fingerprint") == fingerprint:
            return plan_id
    row = FrameworkFixRegistry().lookup(fingerprint)
    if row:
        candidate = str(row.get("fix_plan_id") or "")
        if candidate and candidate in plans:
            return candidate
    return None


def _framework_fix_plans() -> Dict[str, Dict[str, Any]]:
    """路由表里所有修复计划，连同它们的框架修复块。"""
    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        rows = conn.execute(
            "SELECT plan_id, framework_fix FROM plan_routing "
            "WHERE plan_kind = ?",
            (FRAMEWORK_FIX_KIND,),
        ).fetchall()
    finally:
        conn.close()
    plans: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        plans[str(row[0])] = _decode_block(row[1])
    return plans


def _decode_block(raw: Any) -> Dict[str, Any]:
    """框架修复块 —— 解不开就当它没有内容，不当成异常。"""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _with_attach_list(
    envelope: Dict[str, Any], registry: FrameworkFixRegistry
) -> Dict[str, Any]:
    """把挂接清单补进信封，再重渲染五段任务。

    清单要问注册表 —— 注册表知道「谁在等这个修复」，而这不是信封生成时
    手里有的事实（那时计划还没建出来，挂接也还没发生）。
    """
    rows = registry.list_attach(str(envelope["fingerprint"]))
    attached = [
        {
            "plan_id": str(row.get("plan_id") or ""),
            "vp_id": str(row.get("vp_id") or ""),
            "round": int(row.get("round") or 0),
            "source": str(row.get("source") or ""),
            "status": str(row.get("status") or ""),
        }
        for row in rows
    ]
    enriched = dict(envelope)
    enriched["attached_plans"] = attached
    scope = dict(envelope["impact_scope"])
    scope["affected_plan_count"] = len({item["plan_id"] for item in attached})
    enriched["impact_scope"] = scope
    enriched["acceptance"] = _acceptance(enriched, len(attached))
    enriched["task_specs"] = _render_task_specs(enriched)
    return enriched


def _ensure_registered(
    registry: FrameworkFixRegistry,
    envelope: Dict[str, Any],
    plan_id: str,
    decision: Optional[FailureDecision] = None,
) -> None:
    """确保这个缺陷在注册表里有一行，指着这个修复计划。

    注册表是缺陷的唯一登记处。一个**已经**登记过的签名不在这里重写 —— 那
    一行的 ``fix_plan_id`` 由 :func:`_point_registry_at_fix_plan` 改。

    只有「信封给的指纹就是这份材料算出来的指纹」时才登记：指纹是别名时
    （语义复核合并过），另起一行等于把同一个缺陷登记成两个。

    ``blocking`` 写的是判定结论的**真实值**，不是常量 —— 建了计划的那次
    命中阻塞是事实，但写下来的是「判定说了什么」，不是一个常量。
    """
    fingerprint = str(envelope["fingerprint"])
    if registry.lookup(fingerprint) is not None:
        return
    if decision is None:
        decision = _decision_of(envelope)
    _register_hit(
        registry,
        fingerprint=fingerprint,
        signature=envelope["signature"],
        classification=str(envelope["reproduction"]["classification"]),
        decision=decision,
        disposition=DISPOSITION_FIX_PLAN,
        plan_id=plan_id,
        vp_id=str(envelope.get("source_vp_id") or ""),
        repo=str(envelope.get("repo") or ""),
        fix_plan_id=plan_id,
    )


def _point_registry_at_fix_plan(
    registry: FrameworkFixRegistry, envelope: Dict[str, Any], plan_id: str
) -> None:
    """把注册表那一行的 ``fix_plan_id`` 指向这个修复计划。

    注册表的登记是 ``INSERT OR IGNORE``：重复登记**不会**改写已有行的
    ``fix_plan_id``，所以「这一行现在指着哪个修复计划」必须在这里显式改。
    不改的话，注册表会一直指着第一次发现它的那个业务计划 —— 而业务计划不
    是修复计划。

    这一改由**注册表自己**做（:meth:`FrameworkFixRegistry.link_fix_plan`），
    不在这里写裸 SQL：注册表是这一行的唯一写入点，绕过它就等于绕过审计链
    ——「这一行归谁修」会变成一条没有出处的答案。
    """
    registry.link_fix_plan(
        str(envelope["fingerprint"]),
        plan_id,
        repo=str(envelope.get("repo") or ""),
    )


# ---------------------------------------------------------------------------
# 计划号与撤除
# ---------------------------------------------------------------------------
def _allocate_plan_id(fingerprint: str) -> str:
    """给这个签名分配一个计划号。

    计划号来自指纹（不是来自时钟或计数器），所以同一个签名永远落在同一个
    名字上；名字被占用了就加后缀。
    """
    seed = f"framework-fix-{fingerprint.split(':')[-1][:12]}"
    base = derive_plan_id(seed, max_len=20)
    plans_dir = resolve_plans_dir()
    plan_id = base
    suffix = 1
    while (plans_dir / plan_id).exists():
        suffix += 1
        plan_id = f"{base}-{suffix}"
    return plan_id


def _discard_plan(plan_dir: Path, plan_id: str) -> None:
    """撤掉一次失败的生成留下的东西。

    目录与路由行都撤。撤不掉的那部分记一条警告 —— **不吞掉**：原始错误要
    照原样抛出去，但这半截残骸得有人知道。

    路由行由**路由仓库**撤（``RoutingRepository.delete``）：整行写就是相位写
    （那一行带着相位），相位只有一个写入者 —— 路由仓库。这里自己发一条
    ``DELETE`` 就是第二个写入者。
    """
    try:
        shutil.rmtree(plan_dir, ignore_errors=False)
    except OSError as exc:
        logger.warning(
            "framework_fix_spawner: could not remove the half-created plan "
            "directory %s: %s",
            plan_dir,
            exc,
        )
    try:
        conn = open_db(resolve_state_db_path())
        try:
            RoutingRepository(conn).delete(plan_id)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 撤除失败不能盖住原始错误
        logger.warning(
            "framework_fix_spawner: could not remove the routing row of the "
            "half-created plan %s: %s",
            plan_id,
            exc,
        )


def _validated_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """检查信封的关键字段齐不齐。"""
    if not isinstance(envelope, dict):
        raise ValueError("the envelope must be a dict")
    for key in ("fingerprint", "signature", "reproduction", "project_dir"):
        if not envelope.get(key):
            raise ValueError(f"the envelope is missing {key!r}")
    signature = envelope["signature"]
    if not isinstance(signature, dict) or not signature.get("framework_files"):
        raise ValueError(
            "the envelope's signature must name the framework files to change"
        )
    project_dir = Path(str(envelope["project_dir"]))
    if not project_dir.is_dir():
        # 任务全部指向这一个检出；检出不在，计划生成了也跑不起来 —— 而
        # 「跑不起来的修复计划」会一直占着别人的挂接。
        raise ValueError(
            f"the envelope's project_dir does not exist: {project_dir}"
        )
    return envelope
