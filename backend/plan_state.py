"""
Plan State — Central Workflow State Manager
============================================

Tracks workflow progression across all phases including optional
architecture and test design phases. Single source of truth for plan state.

As of task #3.7, the per-plan state is persisted to the SQLite
``plan_routing`` table (columns ``current_phase`` / ``completed_phases``
/ ``review_rounds`` / ``flags`` / ``verification`` / ``last_updated``).
The legacy ``plan_state.json`` file is no longer written by
:class:`PlanState`; on first read of a plan whose file exists but
whose ``plan_routing`` row does not, the contents are migrated to
SQLite (one-shot, atomic) and the file is ignored thereafter.

Two further columns carry the framework-fix lane's state (added by the
2026-11-23 migration): ``plan_kind`` (``normal`` / ``framework_fix``) and
``framework_fix`` (a JSON block, ``{}`` meaning "no framework-fix state").
:class:`PlanState` reads them via :meth:`PlanState.get_plan_kind` /
:meth:`PlanState.get_framework_fix` and writes them via
:meth:`PlanState.enter_block` / :meth:`PlanState.exit_block`. They are
**not** phase values: the phase vocabulary stays untouched, and the
framework-fix display states are projected by
``backend/framework_fix/read_model.py``.
"""

import hashlib
import importlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, List, Dict, Any

from framework import clock as _framework_clock
from config_paths import resolve_plans_dir, resolve_state_db_path
from phase_vocabulary import log_deprecated_ready, normalize_waiting_phase
from plan_artifacts import (
    BRANCH_DEV,
    BRANCH_VERIFICATION,
    PLAN_DOC_FILENAME,
    detect,
)
from plan_mode import MODE_PRODUCTION, mode_from_flags, resolve_mode
from verification_gate import verification_branch_ready
from verification_plan_review import doc_sha256, prd_is_resolved




# Canonical filename for the per-plan state JSON. A single literal —
# no string-concat bypass. This is the contract pinned by
# backend/tests/test_plan_state_filename_constant.py.
PLAN_STATE_FILENAME: str = "plan_state.json"

#: The default verification round budget.
#:
#: It mirrors ``server.DEFAULT_MAX_VERIFICATION_ROUNDS`` — the constant
#: ``GET /api/verification/{id}/status`` answers with when a plan has no
#: ``plan_verification`` row.  ``plan_state`` cannot import ``server``
#: (``server`` imports ``plan_state`` — a circular import), so the value
#: is repeated here; the two MUST agree, and the test suite pins that
#: (``backend/tests/unit/test_plan_state_verification_read.py::
#: test_default_max_rounds_matches_the_server_constant``).
DEFAULT_MAX_VERIFICATION_ROUNDS: int = 4

#: ``plan_routing.plan_kind`` 的默认值 —— 一个普通业务计划。
#:
#: 2026-11-23: ``plan_kind`` 区分「普通计划」（``normal``）与「框架修复
#: 计划」（``framework_fix``，由框架修复 lane 为框架缺陷建的修复计划）。
#: 这一列是任务 1 的迁移加上去的，列的 DEFAULT 就是这个值，所以旧行读出来
#: 一律是它 —— 框架修复特性上线之前写下的计划，本来就不是修复计划。
DEFAULT_PLAN_KIND: str = "normal"

#: 模块级 logger。
#:
#: 等待相位的弃用日志走它：``source`` 固定为 ``plan_state``，于是「谁在读
#: 旧的 ``ready``」这个问题在日志里有一个稳定的来源名可检索。
logger = logging.getLogger(__name__)


def _state_db_path() -> Path:
    """Return the path of the state-machine SQLite file.

    Delegates to :func:`config_paths.resolve_state_db_path` so
    ``PlanState`` persists to exactly the database the API layer reads.
    It used to *mirror* ``server._state_db_path``'s two-step resolution
    instead of calling it, which is how ``watchdog`` and the
    verification orchestrator each drifted to a different file without
    anything failing loudly (see the resolver's docstring).
    """
    return resolve_state_db_path()


def _read_verification_row(plan_id: Optional[str]) -> Optional[dict]:
    """Read the authoritative ``plan_verification`` row for ``plan_id``.

    The verification lifecycle state lives in exactly one place: the
    ``plan_verification`` table (arch decision point 1).  This is the
    module-level door to it for the callers that do not already hold a
    connection.

    Returns ``None`` when the plan has no verification row, when the
    state store cannot be opened, or when the read fails for any other
    reason — an unreadable row is "no row", never an exception.  A plan
    without a row has never run a verification round, and the callers
    answer that with the documented defaults.
    """
    if not plan_id:
        return None
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.verification_repository import (
            VerificationRepository,
        )
    except ImportError:
        return None
    try:
        conn = _open_db(_state_db_path())
    except Exception:  # noqa: BLE001
        return None
    try:
        _migrate(conn)
        return VerificationRepository(conn).current(plan_id)
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def _verification_row_exists(
    plan_id: Optional[str],
    conn: Optional[Any] = None,
) -> Optional[bool]:
    """Probe whether an authoritative ``plan_verification`` row exists.

    This is the **write guard's** existence check, and it must not be
    :func:`_read_verification_row`.  That helper answers "no row" for a
    row it cannot decode — the ``plan_verification`` JSON columns
    (``results`` / ``verdicts`` / …) go through ``json.loads`` and one
    corrupted blob makes the whole read raise, which the helper
    swallows.  For a *read* that answer is right (an unreadable row
    answers with the documented defaults).  For a *guard* it is wrong
    in the unsafe direction: a row that exists but cannot be decoded is
    still a row, and overwriting it destroys the authoritative record.

    So the probe is a bare ``SELECT 1`` — the same shape the migration
    script uses (``scripts/migrate_legacy_verification_stop_reason.py``)
    — and it never decodes a column.

    Args:
        plan_id: the plan to probe.
        conn: an open connection to use.  When ``None`` the default
            state store is opened (and closed again).

    Returns:
        ``True`` when the row exists, ``False`` when it does not, and
        ``None`` when the probe cannot answer (the state store cannot
        be opened, the schema cannot be created, the query fails).  A
        caller that guards a write must treat ``None`` as "do not
        write": overwriting an authoritative row cannot be undone,
        while a missing seeding can be redone by running the migration
        again.
    """
    if not plan_id:
        return False
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
    except ImportError:  # pragma: no cover - 打包环境兜底
        return None
    owned_connection = conn is None
    if owned_connection:
        try:
            conn = _open_db(_state_db_path())
        except Exception:  # noqa: BLE001
            return None
    try:
        _migrate(conn)
        row = conn.execute(
            "SELECT 1 FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if owned_connection:
            conn.close()


def _decode_json_column(value: Any, default: Any) -> Any:
    """Decode a JSON column, tolerating a corrupted blob.

    A corrupted blob means "no value" — never a crash, and never a
    reason to doubt the authoritative row.
    """
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 串（``...Z``）。

    与 ``RoutingRepository._now_iso`` 同一口径 —— ``plan_routing.updated_at``
    是那一列的写者，两边格式不同会让按字符串排序的消费者看到错乱的顺序。
    """
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_framework_fix_columns(
    conn: Any,
    plan_id: str,
) -> tuple[str, Dict[str, Any]]:
    """从一条已打开的连接上读 ``plan_kind`` / ``framework_fix`` 两列。

    返回 ``(plan_kind, framework_fix)``。读不到（表没有这两列、没有这一行、
    值坏掉）一律给文档化的默认值：普通计划 + 空块。**不抛错** —— 一个框架
    修复特性上线之前写下的计划，本来就没有框架修复状态；把它读成异常等于
    让一次旧数据的存在变成一次崩溃。
    """
    try:
        row = conn.execute(
            "SELECT plan_kind, framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return DEFAULT_PLAN_KIND, {}
    if row is None:
        return DEFAULT_PLAN_KIND, {}
    kind = row[0] or DEFAULT_PLAN_KIND
    block = _decode_json_column(row[1], {})
    if not isinstance(block, dict):
        block = {}
    return str(kind), block


def _read_routing_framework_fix(
    plan_id: str,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """读 ``plan_routing`` 的两列；**读不到**时返回 ``None``。

    ``None`` 与「读到空块」是两件不同的事：前者是数据库读不了（调用方该
    知道这个答案不可信），后者是「这个计划确实没有框架修复状态」。

    这一条是**纯读**：不迁移、不建行、不写任何东西。列不存在时读路径给出
    默认值就够了 —— 那正是旧计划该有的答案。
    """
    try:
        from state_machine.db.connection import open as _open_db
    except ImportError:
        return None
    try:
        conn = _open_db(_state_db_path())
    except Exception:  # noqa: BLE001
        return None
    try:
        return _read_framework_fix_columns(conn, plan_id)
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def _write_routing_framework_fix(plan_id: str, block: Dict[str, Any]) -> None:
    """把 ``framework_fix`` 块写进 ``plan_routing`` 行。

    **写失败抛错**（不吞）：挂起写不进去却报告成功，比写失败更糟 ——
    读模型会按「没挂起」渲染，而计划其实停在被阻塞的状态上。抛错让调用方
    能重试。

    行不存在时先建一行：``insert_if_missing`` 只写 ``plan_id`` /
    ``current_phase`` / ``version`` / ``updated_at``，不碰别人拥有的列
    （相位 CAS 的 ``version`` / ``substage`` 尤其不能被顺手动）。
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_db(_state_db_path())
    try:
        # 迁移是幂等的补列步骤 —— 列不存在时写不进去。
        _migrate(conn)
        RoutingRepository(conn).insert_if_missing(plan_id, "interview")
        conn.execute(
            "UPDATE plan_routing SET framework_fix = ?, updated_at = ? "
            "WHERE plan_id = ?",
            (
                json.dumps(block, ensure_ascii=False),
                _utc_now_iso(),
                plan_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _verification_status_for_phase(phase: Optional[str]) -> Optional[str]:
    """The verification status a phase implies (``None`` if it implies none).

    The status itself lives in the authoritative ``plan_verification``
    row (arch decision point 1) — this mapping only says what a
    transition INTO a phase means.  Two callers need it: the phase
    machine (:meth:`PlanState._transition_to`) and the legacy migration
    that reconstructs the status of a plan that stopped its verification
    loop before the write path moved to the authoritative table.
    """
    if phase == "verification":
        return "pending"
    if phase in ("verification_running", "verify_first_pass", "verify_recheck"):
        # Do NOT auto-increment the round here. ``verification_round``
        # is the number of completed rounds (0-based). It is bumped
        # by the repair path (``start_repair_execution``) when the
        # user confirms repair tasks and the orchestrator moves to
        # the next round. Auto-incrementing on entry would break the
        # 0-based contract expected by ``VerificationOrchestrator``
        # tests.
        return "running"
    if phase == "verification_passed":
        return "passed"
    if phase == "verification_failed":
        return "failed"
    if phase == "verification_loop_stopped":
        return "loop_stopped"
    return None


def migrate_mirror_stop_reason(
    plan_id: str,
    routing_row: dict,
    conn: Optional[Any] = None,
) -> Optional[dict]:
    """Carry ONLY the stop reason from the retired routing mirror into
    the authoritative ``plan_verification`` row.

    Background
    ----------
    Before the verification writes moved to ``plan_verification``
    (arch decision point 1), the only home of the stop reason was the
    ``plan_routing.verification`` mirror column — the pre-2026-11-04
    ``backend/verification_loop.py`` wrote it there with a bare
    ``UPDATE plan_routing SET verification = …``.  A plan that stopped
    its verification loop back then has no authoritative row, so the
    reason would be lost the moment the reads stop consulting the
    column — and the "first reason wins" rule (the reason of the first
    stop IS the cause) would start overwriting it.

    What it carries
    ---------------
    Only the **reason**: it is a one-time record of a past event, like
    the baseline.  The mirror's status / round / max_rounds are NOT a
    source (arch decision point 3), so the row is created with the
    documented defaults — ``status="not_started"``, ``round=0`` and
    ``max_rounds=DEFAULT_MAX_VERIFICATION_ROUNDS`` — the very values a
    plan without an authoritative row gets everywhere else.  The reason
    survives; no lifecycle value is imported from the mirror.

    The plan's **phase** is deliberately NOT consulted either.  It used
    to be, back when this carry-over ran as a read-path fallback, and
    that made the routing phase a second source of the verification
    status — the very thing arch decision point 3 retires.  The one
    authoritative row is now written from the documented defaults plus
    the reason, so nothing but the reason crosses over.

    Who calls it
    ------------
    **Nobody on a read path.**  This used to be the fallback of
    :meth:`PlanState._load_state_from_sqlite`, and that made a read a
    write: the answer depended on the order the readers ran in, and
    the mirror column was a state source again (VP-015 (c)).  The
    one-time carry-over is now operator-triggered —
    ``scripts/migrate_legacy_verification_stop_reason.py``.

    It only ever **seeds**: when the plan already has an authoritative
    row, that row is the newer record and wins, and nothing is written
    (VP-019 (a)).  The existence check is a bare ``SELECT 1`` — see
    :func:`_verification_row_exists` — so a row that exists but cannot
    be decoded still counts as existing.

    Args:
        plan_id: the plan the reason belongs to.
        routing_row: the ``plan_routing`` row carrying the mirror column.
        conn: an open connection to use.  When ``None`` the default
            state store is opened (and closed again).  The migration
            script passes its own connection so it controls the
            transaction.

    Returns:
        The authoritative row after the migration, or ``None`` when
        there was nothing to carry — including when the plan already
        had an authoritative row that could not be read back.
    """
    mirror = _decode_json_column(routing_row.get("verification"), {})
    if not isinstance(mirror, dict):
        return None
    stop_reason = mirror.get("stop_reason")
    if not stop_reason:
        return None
    owned_connection = conn is None
    if owned_connection:
        try:
            from state_machine.db.connection import open as _open_db
            conn = _open_db(_state_db_path())
        except Exception:  # noqa: BLE001
            return None
    try:
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.verification_repository import (
            VerificationRepository,
        )
        _migrate(conn)
        repo = VerificationRepository(conn)
        if _verification_row_exists(plan_id, conn=conn) is not False:
            # The authoritative row already exists: it is the newer
            # record and wins.  The carry-over only ever *seeds* a plan
            # that has no row yet — it must never overwrite one
            # (VP-019 (a)).  The guard lives HERE, in the migration
            # itself, not only in the caller: the script's
            # classification skips plans that already have a row, but
            # every other caller (a test, a future migration) would
            # otherwise walk straight past it.
            #
            # A probe that cannot answer fails closed for the same
            # reason: overwriting an authoritative row cannot be undone,
            # while a missing seeding can be redone by running the
            # migration again.
            try:
                return repo.current(plan_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "verification_state_migration_skipped plan=%s reason=%s",
                    plan_id, exc,
                )
                return None
        repo.set_state(
            plan_id,
            status="not_started",
            round_n=0,
            max_rounds=DEFAULT_MAX_VERIFICATION_ROUNDS,
            stop_reason=str(stop_reason),
        )
        return repo.current(plan_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "verification_state_migration_failed plan=%s reason=%s",
            plan_id, exc,
        )
        return None
    finally:
        if owned_connection:
            conn.close()


# Module-level singleton pointing at the project's ``plans/`` directory.
# Mirrors the ``PLANS_DIR`` constant in ``backend/server.py``. Defined
# here so the module-level reader helpers (``get_migration_audit``)
# have a stable anchor; tests may ``monkeypatch.setattr`` it to a
# ``tmp_path`` sandbox without touching the filesystem singleton.
#
# 2026-09-13: resolved via ``config_paths.resolve_plans_dir`` so an
# ``PDT_PLANS_DIR`` override (set by the test conftests) is honoured at
# import time. Tests that keep the module-level anchor MUST NOT create
# ``<repo>/plans/<fixture-id>/`` in the operator's live tree.
plans_dir: Path = resolve_plans_dir()

#: 「被框架缺陷阻塞」相位（架构决策点 4，2026-11-25）。
#:
#: 命中框架缺陷之后原计划要停下来等修复。**部分阻塞**时计划还有别的活要干，
#: 相位不动；**全部阻塞**时（一点活都不剩了）计划进这个相位：让出执行/验证
#: 槽位，回调度队列排队等修复合入。
#:
#: 它不是终态（释放之后还要再跑一遍），也不是失败（被挡住不是做砸了）——
#: 所以进它的那条路绝不写失败裁决、绝不消耗重试预算。
BLOCKED_PHASE = "blocked_on_framework_fix"

VALID_PHASES = [
    "interview",
    "interview_complete",
    "prd_generation",
    "prd_review",
    "prd_refining",
    "prd_approved",
    "arch_generation",
    "arch_review",
    "arch_refining",
    "arch_approved",
    "test_generation",
    "test_review",
    "test_refining",
    "test_approved",
    "preflight_review",
    "tasks_generation",
    # 2026-10-25 (R10): ``queued`` 是**唯一**的等待相位。
    #
    # 历史上「任务已生成、等人决定何时开始」这件事有两个名字：``ready``
    # （人在等）与 ``queued``（交给调度器）。两套词汇说的是同一件事，于是
    # 每处读路径都得各判一次「这是不是 ready」—— 判漏一处，就是一个卡在
    # 旧相位里出不来、也没人认领的计划。
    #
    # ``ready`` 仍然留在 ``VALID_PHASES`` 里，但只作**输入别名**：既有数据
    # 库里存着它，而 :meth:`PlanState.transition_to` 会把它归一化成
    # ``queued``（``phase_vocabulary.normalize_waiting_phase``）。写路径不再
    # 产生 ``ready``。
    #
    # 留着它的另一个理由：``_load_state_from_legacy_file`` 拿
    # ``VALID_PHASES`` 判断旧文件里的相位是否合法 —— 从表里删掉它，旧文件里
    # 的 ``ready`` 会被当成非法值，整条计划掉回 ``interview``。
    #
    # 2026-09-15 的调度器语义不变：``queued`` 是调度器的收件箱，只有停在
    # 这个相位的计划会被自动启动（同一工作区一次一个，两条共享工作树的
    # 计划因此不会同时跑、不会互相踩）。
    "ready",
    "queued",
    "executing",
    # 2026-10-24: ``waiting_for_quota`` — the plan is parked because
    # every provider is out of capacity (HTTP 429). It is an
    # execution-time state, not a workflow one: the plan has work left
    # to do, but it cannot do it right now. Without a name for this the
    # plan either stayed in ``executing`` (reading as live work that had
    # silently stalled) or was pushed into a terminal phase (which
    # throws away the work).
    #
    # It is NOT terminal — a parked plan can be restarted once capacity
    # comes back — and it is deliberately NOT part of the verification
    # sub-machine (see ``VERIFICATION_PHASE_TRANSITIONS`` below).
    #
    # 2026-10-26 (merge): 本分支的容量等待相位落进 main 的新词表。
    # 它是**独立**相位，不是「等待相位」的一种 —— 「等人」在新词表里只有
    # ``queued`` 一个名字（``ready`` 是历史别名，见 ``phase_vocabulary``），
    # 而「等配额」说的是另一件事：工作没做完，但眼下没有 provider 有余量。
    # 把它并进 ``queued`` 会让调度器把一个还欠着活的计划当成「等人开始」，
    # 于是自动启动它 —— 而它一启动就会再撞一次 429。
    "waiting_for_quota",
    # 2026-10-25: the verification branch's plan-doc review gate.
    #
    # A verification-branch plan does not run the dev task list first: it
    # ships a ``verification-plan.md`` (the human review object) and waits
    # for every item to be accepted / skipped. This phase is that wait.
    #
    # It is a *non-terminal* phase: the operator can send the plan back to
    # ``tasks_generation`` (regenerate the doc) or stop it. The only way
    # out towards verification is the review gate itself.
    "verification_plan_review",
    "verification",
    "verification_running",
    "verify_first_pass",
    "verify_recheck",
    "verification_passed",
    "verification_failed",
    "verification_repairing",
    # 2026-11-25: ``blocked_on_framework_fix`` —— 「被框架缺陷阻塞」
    # （架构决策点 4）。
    #
    # 命中框架缺陷之后，原计划**停下来**等修复。停下来有两种：部分阻塞
    # （还有别的活要干 —— 计划继续做那些）与全部阻塞（一点活都不剩了）。
    # 只有后一种才需要一个**相位**：它把计划从「跑着的活」里摘出来，让出
    # 执行/验证槽位，回调度队列排队等修复合入。
    #
    # 它**不是**终态：挂起被释放之后，计划还要回队列再跑一遍（出边
    # ``queued``）。它也**不是**失败：被框架缺陷挡住不是这条计划做砸了，
    # 所以绝不写失败裁决、绝不消耗重试预算。
    #
    # 五个可见状态里只有它落在相位上，其余四个
    # （``waiting_on_user_merge`` / ``framework_fix_exhausted`` /
    # ``scheduler_deadlock_suspected`` / ``reopened``）住在 ``framework_fix``
    # JSON 块里 —— 展示由读模型（``backend/framework_fix/read_model.py``）
    # 投影。
    "blocked_on_framework_fix",
    # 2026-11-20: ``user_stopping`` —— 「正在停止」（**plan 级**）。
    #
    # 过去 stop 只有一条边：从 ``verification_running`` /
    # ``verification_repairing`` 直接落到 ``verification`` + ``stopped``。
    # 一条边同时表达了「停的动作已经发生」与「停的结果已经落地」，中间那段
    # 没有名字 —— 于是事故里出现了「显示已停、后台仍在派发」：相位说停了，
    # 验证链还在跑。
    #
    # 它是**中间相位**：不是终态（停了还要能落到 ``stopped`` 等终态出口），
    # 也不是「已经停了」的结论。
    #
    # 名字是 **plan 级**的（架构决策 1）：execution / verification / repair
    # 三处的停止共用这一个相位，不按阶段各起一个名字 —— 否则「停」在词表里
    # 就有三个名字，读路径要各判一次，判漏一处就是一个「显示已停、后台仍在
    # 派发」的计划。
    #
    # 正因为是 plan 级，它**不**带 ``verification`` 前缀，于是执行门禁
    # （``_is_gated_verification_phase``）不再管着它 —— 执行到一半被喊停的
    # 计划本来就没有「执行已完成」这条记录，让它落在门禁之后等于把这条路
    # 堵死。同一处判定还有第二个后果：门禁后的相位会**自动**给
    # ``completed_phases`` 补一条 ``execution``，停到一半的计划补上这条等于
    # 在相位历史里记下一件它没做过的事。
    #
    # 旧名 ``verification_stopping``（按阶段命名）已从词表里移除，但既有
    # ``plan_routing`` 行里还存着它 —— 读路径把它归一化成新名（见
    # :data:`LEGACY_PHASE_ALIASES`）。
    "user_stopping",
    # 2026-10-22: ``verification_rerunning`` was removed from the
    # vocabulary. It was a DEAD state — its only writer,
    # ``start_verification_rerun``, had no callers anywhere — so no
    # plan can reach it any more. Rows written before the removal
    # still carry the old value; they are read through
    # :func:`normalize_phase` (historical value → ``verification_repairing``).
    "verification_loop_stopped",
    # 2026-11-04: ``import_failed`` — the plan the fast lane could not
    # turn into a runnable plan.
    #
    # The import lane refuses material it cannot run (the audit said no,
    # or one of the executor gates threw the list away).  Before this
    # name existed, the refusal left the plan's *directory* on disk and
    # no row anywhere: every reader that goes through ``plan_routing``
    # answered "no such plan", and the directory was a folder nobody
    # could account for.  Registering the refusal in a phase of its own
    # is what makes the plan findable — "it is there, and it failed"
    # instead of "it does not exist".
    #
    # It is deliberately NOT ``queued`` (the scheduler collects only
    # ``queued``, so a failed import is never pulled into the line) and
    # deliberately NOT terminal (a terminal phase is filtered out of the
    # listings — and a failed import is exactly the plan an operator
    # needs to see).
    "import_failed",
    "completed",
    "failed",
    "stopped",
    # 2026-11-09: ``archived`` —— 「我们主动归档、永远别再跑」。
    #
    # 一批历史测试计划（**从没跑过**）之前一律被置成 ``stopped``，但 ``stopped``
    # 在调度面板里是**可操作**相位（``resume`` / ``start``），于是它们被展示成
    # 「可恢复/可启动」—— 与「主动归档、永远别再跑」的意图正好相反。
    #
    # ``archived`` 与 ``completed`` 是**两个**相位：``completed`` 表示「真的做
    # 完了」，``archived`` 表示「我们把它收起来了，别再跑它」。两者都留在词表
    # 里，不合并 —— 合并就抹掉了「做完」和「收起来」的区别。
    "archived",
    # 2026-11-14: ``post_processing`` / ``postprocess_failed`` —— 可选的收尾
    # 阶段（post-processing）。
    #
    # 验证通过之后，计划原先直接置 ``completed`` 就算结束；可选收尾阶段给
    # 「验证通过」与「真的收完」之间留出一个位置：只有声明了结构化收尾要求书
    # 的计划才会进 ``post_processing``，没有要求书的计划走的还是原来那条
    # ``completed`` 路径（行为不变）。
    #
    # 两个相位都**不在** :data:`TERMINAL_PHASES` 里：``post_processing`` 还在
    # 干活，``postprocess_failed`` 还要等人决定是否续跑。
    #
    # 相位名字由 ``backend/postprocess/state.py`` 拥有（那个包是收尾子系统的
    # 家），这里登记它们进入词表；两处由测试钉住同步。
    "post_processing",
    "postprocess_failed",
]

TERMINAL_PHASES = {"completed", "failed", "stopped", "archived"}

#: 稳定标签映射：阶段名 → 展示用的稳定标签。
#:
#: 2026-10-25: 新增 ``verification_plan_review``（计划书待审）。展示面此前按
#: 文档存在性**猜**状态，会把这个阶段误标成 ``reviewed``；现在标签集中定义在
#: 这一处，状态接口与列表接口共用同一份映射 —— 同一阶段的标签不可能在两个
#: 接口里对不上。
#:
#: 未命中时回退为阶段名本身：标签是**补充**，不是阶段名的替身。往这张表里加
#: 一行就多一个稳定标签，不加也只是回退，不会让任何阶段没有名字。
PHASE_LABELS: Dict[str, str] = {
    "verification_plan_review": "计划书待审",
    # 2026-10-25 (R10): 等待相位合并成 ``queued`` 之后，等待中的计划在
    # 列表接口里有一个**显式桶**（「排队中」），不再靠启发式猜它属于哪一桶。
    "queued": "排队中",
    # 2026-11-09: ``archived``（已归档）—— 主动归档、永远别再跑的计划。
    "archived": "已归档",
    # 2026-11-20: ``user_stopping``（正在停止）—— plan 级的「正在停止」。
    #
    # 未命中时回退为阶段名本身，于是这个相位会以裸英文 ``user_stopping``
    # 的样子出现在卡片与页面上 —— 那是**词表内部**的名词，读者认不出来。
    "user_stopping": "正在停止",
    # 2026-11-25: ``blocked_on_framework_fix``（被框架缺陷阻塞）—— 架构
    # 决策点 4 的那个相位。字面与前端徽章表（``frontend/app.js`` 的
    # ``FRAMEWORK_FIX_BADGE_LABELS``）一致：读者在卡片上看到的词与后端
    # 词表里的词是同一个。
    "blocked_on_framework_fix": "被框架缺陷阻塞",
}

#: Historical phase values that are no longer written anywhere, mapped
#: onto the phase that replaced them.
#:
#: 2026-10-22: ``verification_rerunning`` was a DEAD state — its only
#: writer, :meth:`PlanState.start_verification_rerun`, had no callers
#: anywhere in the repository — so it was removed from the phase
#: vocabulary (``VALID_PHASES`` / ``VERIFICATION_PHASE_TRANSITIONS``).
#: A ``plan_routing`` row written before the removal still carries the
#: old value; the READ path maps it back onto the phase it stood for
#: (``verification_repairing``) so such a plan neither crashes nor is
#: stranded in a phase nothing can move it out of.
LEGACY_PHASE_ALIASES: Dict[str, str] = {
    "verification_rerunning": "verification_repairing",
    # 2026-11-20: ``verification_stopping``（按阶段命名）→
    # ``user_stopping``（plan 级，架构决策 1）。
    #
    # 旧值只出现在既有 ``plan_routing`` 行里。读路径把它归一化成新相位，
    # 那些计划因此不会停在一个词表里已经没有的相位上 —— 没人能推进它，
    # 也没人认领它。
    "verification_stopping": "user_stopping",
}


def normalize_phase(phase: Optional[str]) -> Optional[str]:
    """Map a historical phase value onto the phase that replaced it.

    Unknown / non-string values are returned unchanged — this is a
    compatibility shim for values that were written by an older
    version, not a validator.
    """
    if not isinstance(phase, str):
        return phase
    return LEGACY_PHASE_ALIASES.get(phase, phase)

# 2026-09-17 (schema v5): the ``_PLAN_PHASE_TO_ROUTING_STAGE``
# translation table that used to live here is DELETED, along with the
# ``plan_routing.stage`` column it fed. ``plan_routing`` now carries
# exactly one workflow-state column — ``current_phase`` — so there is no
# second vocabulary to translate into.
#
# The table existed because ``stage`` and ``current_phase`` were
# different words for the same thing (``ready`` vs ``tasks_ready``,
# ``queued`` vs ``tasks_queued``, ``completed`` vs ``terminal_done``),
# and only ONE writer remembered to apply it. The ~23 raw
# ``UPDATE plan_routing SET stage`` sites wrote phase vocabulary
# straight into the stage column, so a single row could hold either
# vocabulary depending on which writer touched it last. On top of that
# the mapping was lossy — ``failed`` / ``stopped`` /
# ``verification_failed`` / ``verification_loop_stopped`` all collapsed
# to ``terminal_failed`` — which is why ``stage`` could never have been
# the surviving column.
#
# The scheduler's two stage-valued predicates are now phase-valued
# (see ``state_machine.services.scheduler_support`` and
# ``notifications.plan_dir_resolver``). If you find yourself wanting to
# add a translation table back, that is the signal you are about to
# re-create a second state column.

PHASE_TRANSITIONS = {
    "interview": ["interview_complete"],
    "interview_complete": ["prd_generation"],
    "prd_generation": ["prd_review"],
    "prd_review": ["prd_refining", "prd_approved"],
    "prd_refining": ["prd_review"],
    "prd_approved": ["prd_refining", "arch_generation", "test_generation", "preflight_review", "tasks_generation", "executing"],
    "arch_generation": ["arch_review"],
    "arch_review": ["arch_refining", "arch_approved"],
    "arch_refining": ["arch_review"],
    # Backward edge: when the user adds a new decision point after a
    # phase was already approved, we roll the plan back to the matching
    # ``*_review`` phase so the new content goes through the same
    # review-correction loop. Mirrors ``prd_approved -> prd_refining``
    # / ``test_approved -> test_refining`` below.
    "arch_approved": ["arch_refining", "test_generation", "preflight_review", "tasks_generation", "executing"],
    "test_generation": ["test_review", "test_approved"],
    "test_review": ["test_refining", "test_approved"],
    "test_refining": ["test_review"],
    "test_approved": ["test_refining", "preflight_review", "tasks_generation", "executing"],
    "preflight_review": ["tasks_generation", "prd_review", "arch_review", "test_review"],
    # 2026-10-25: the verification branch generates a *plan doc* instead
    # of a task list. The doc lands on disk first, then the plan parks in
    # ``verification_plan_review`` waiting for a human to accept / skip
    # every item.
    "tasks_generation": ["ready", "queued", "executing", "verification_plan_review"],
    # The review phase's own exits. Three meanings:
    #   * ``verification`` — the review is complete and the baseline is
    #     frozen; the plan enters the verification sub-machine;
    #   * ``tasks_generation`` — regenerate the plan doc (the review found
    #     something wrong with it);
    #   * ``stopped`` — the operator parks the plan.
    #
    # The row lives in the MAIN table, next to the edge that enters the
    # phase: ``transition_to`` unions both tables before it looks a target
    # up, so which table holds a row is not a behavioural difference — and
    # the review phase is a top-level workflow phase anyway (it is entered
    # from ``tasks_generation`` and it sits in ``VALID_PHASES`` next to the
    # other workflow phases). The verification sub-table's row set is
    # frozen by ``tests/unit/test_state_machine_completed_transition.py``.
    "verification_plan_review": ["verification", "tasks_generation", "stopped"],
    # 2026-10-23: ``stopped`` is reachable from every phase the
    # operator can stop a plan in. A stop that cannot park the phase
    # leaves a plan that still reads as live — the stop does not hold.
    "ready": ["executing", "queued", "stopped"],
    # 2026-09-15: ``queued`` is the auto-scheduler's inbox. The operator
    # can pull a plan back to ``ready`` (and take it off the queue), or
    # the scheduler/operator can start it directly.
    #
    # 2026-10-25 (R10): ``queued`` 是唯一的等待相位，``ready`` 只作输入
    # 别名 —— 表里两个名字都在，但 ``transition_to`` 会把目标归一化成
    # ``queued``，所以「ready → queued」这条边实际由 ``ready`` 这一行接住。
    "queued": ["ready", "executing", "stopped"],
    # 2026-10-25 (R10): ``executing`` / ``failed`` / ``stopped`` 三行都加
    # 上 ``queued`` —— 执行完、失败、停下之后都要能回到「等人」这个相位。
    # ``ready`` 仍然留着（磁盘上的历史状态照旧可读）。
    #
    # 2026-10-26 (merge): 加上 ``waiting_for_quota`` —— 执行期发现所有
    # provider 都没余量了，计划停在这个相位上等人（容量回来再启动）。
    "executing": ["completed", "failed", "stopped", "ready", "queued", "waiting_for_quota", "blocked_on_framework_fix"],
    # 2026-10-24: the parked-for-capacity state. Three ways out:
    # ``executing`` (capacity came back — restart the work), ``ready``
    # (pull it back to the manual gate), ``stopped`` (park it for
    # good). No edge INTO it from ``ready`` — a plan that never started
    # executing has no quota to wait for, and letting it claim the
    # state would be a lie about what happened.
    #
    # 2026-10-26 (merge): 新词表里「等人」只有 ``queued`` 一个名字，``ready``
    # 是历史别名，而 ``transition_to`` 会把目标归一化成 ``queued`` —— 所以
    # 这里必须**同时**列出 ``ready`` 和 ``queued``：只列 ``ready`` 的话，
    # 归一化后的目标 ``queued`` 不在允许集合里，「从等待配额退回人工闸门」
    # 这条边会被当成非法转移而炸掉。语义与本分支原版一致（一条退到人工
    # 闸门、一条重启、一条彻底停掉）。
    "waiting_for_quota": ["executing", "ready", "queued", "stopped"],
    "completed": ["verification"],
    # 2026-10-23: ``failed`` → ``stopped`` — a plan whose executor
    # stamped ``failed`` can still be stopped by the operator, and the
    # stop has to be able to park it.
    "failed": ["executing", "ready", "stopped", "queued"],
    "stopped": ["executing", "ready", "queued"],
    # 2026-11-09: ``archived`` 的**唯一出边**是 ``queued``。
    #
    # 归档是终态：归档的计划永远不会被调度器捡起来，所以出边只有一条，而且
    # 它的作用是**显式恢复** —— 误归档了，操作者把它放回 ``queued``（等待相位）
    # 重来一次。出边不写成「归档之后还能继续跑」的样子（``executing`` 之类），
    # 因为那就等于归档没有归档住。
    "archived": ["queued"],
    # 2026-11-14: 可选收尾阶段的三条边。
    #
    # 相位模型（架构决策 1）：``verification_passed → post_processing →
    # completed``，失败分支 ``post_processing → postprocess_failed``。
    #
    # 为什么这一行在**主表**而不是验证子表：验证子表的 ``verification_passed``
    # 那一行是冻结的安全边界（通过判定不得被顺手洗成终态），而收尾阶段是
    # **工作流层**的相位 —— 它由编排器的终态出口进入，不是验证子机器的一步。
    # ``transition_to`` 取两张表的并集，所以放哪张表不影响可走性；这里选主表
    # 是为了让那张冻结的子表一个字节都不动。
    #
    # 注意：``verification_passed → completed`` 这条**直连**边仍然不存在 ——
    # 收尾阶段是可选的，进 ``post_processing`` 由收尾要求书的存在性判定把关。
    "verification_passed": ["post_processing"],
    # 收尾跑完 → 真的做完了；收尾有条目没通过 → 停在失败相位等人决定是否续跑。
    #
    # 2026-11-17: 加上 ``queued`` —— 收尾阶段**后来新增**的相位，而边表是在
    # 它之前写好的，所以「被人回答之后回到队列」这条边当时并不存在。
    # PRD A9 要求「等待期间不占执行槽位；唤醒后重新排队」：等待中的计划
    # 不占槽位成立，但收尾中的计划被人工回答之后若回不到队列，就永远再也
    # 拿不到槽位 —— 唤醒后重新排队就没有实现。
    #
    # 这条边**不改**其它相位：``post_processing`` 仍然不是终态，仍然要靠
    # ``completed`` / ``postprocess_failed`` 那两条边收尾；``queued`` 只是
    # 给了它一条「先回队列等一次调度」的出路。
    "post_processing": ["completed", "postprocess_failed", "queued"],
    # 续跑：从失败相位回到收尾阶段本身（同相位重入是幂等的 no-op）。
    "postprocess_failed": ["post_processing"],
    # 2026-11-25: 阻塞相位（架构决策点 4）。
    #
    # **进边写在主表里** —— 验证子表是冻结的安全边界
    # （``verification_passed`` 那一行必须还是空的），而 ``transition_to``
    # 取两张表的并集，所以放哪张表都不影响可走性；这里选主表是为了让那张
    # 冻结的子表一个字节都不动。同 ``post_processing`` 的做法 ——
    # ``PHASE_TRANSITIONS["verification_passed"]`` 就是这么加的。
    #
    # 进边有两条来路：
    #
    # * 执行期的计划（``executing``）：执行撞上框架缺陷，全部剩余工作都被
    #   挡住时也要让出执行槽位；
    # * 验证系列的计划（``verification`` / ``verification_running`` /
    #   ``verify_first_pass`` / ``verify_recheck`` / ``verification_failed`` /
    #   ``verification_repairing``）：这一轮验证的活全被挡住了。
    #
    # 出边只有两条：``queued``（挂起被释放后回调度队列，让它重新被调度一
    # 次）与 ``stopped``（留给操作者）。它**不是**终态 —— 释放之后计划还要
    # 再跑一遍，所以出边里没有终态。
    "verification": ["blocked_on_framework_fix"],
    "verification_running": ["blocked_on_framework_fix"],
    "verify_first_pass": ["blocked_on_framework_fix"],
    "verify_recheck": ["blocked_on_framework_fix"],
    "verification_failed": ["blocked_on_framework_fix"],
    "verification_repairing": ["blocked_on_framework_fix"],
    "blocked_on_framework_fix": ["queued", "stopped"],
}

#: 相位 ``archived`` 的**通用目标**规则里，唯一不能作为源相位的那些。
#:
#: 2026-11-09: ``completed``（真的做完了）不能被改写成 ``archived`` —— 那会把
#: 「它完成过」这个事实抹掉，而「完成」与「归档」正是本相位要分开的两件事。
#: ``stopped`` / ``failed`` 是「没做成」，把它们收起来正是本相位的用途。
#:
#: 这张表只管**源**相位；``archived`` 自己的出边只有 ``queued``（见
#: :data:`PHASE_TRANSITIONS`）。
_ARCHIVE_EXCLUDED_SOURCES = frozenset({"completed"})

VERIFICATION_PHASE_TRANSITIONS = {
    "ready": ["verification", "verification_running"],
    # 2026-10-25 (R10): ``queued`` 是唯一的等待相位 —— 验证子机器里的
    # ``executing`` / ``failed`` / ``stopped`` 同样要能回到它。表里的
    # ``ready`` 保留（磁盘上的历史状态照旧可读），而 ``transition_to`` 会把
    # 目标归一化成 ``queued``。
    # 2026-11-20: 加上 ``user_stopping`` —— 执行刚结束、验证链还没
    # 起跑的时候，操作者也可能喊停。那种计划没有「跑着的验证」要等，但它
    # 仍然要经过「正在停止」这一档，否则「停」在这条路径上又是一次没有名字
    # 的动作。
    #
    # 这一条边是「执行停止与验证停止共用同一状态」的落点（G1）：执行侧与
    # 验证侧指向**同一个**相位，而不是两个按阶段命名的相位。
    "executing": ["verification", "completed", "failed", "stopped", "ready", "queued", "user_stopping"],
    # 2026-10-23: ``stopped`` is reachable from every verification
    # phase. A plan stopped in the middle of a round (or in the middle
    # of a repair) has to be able to park — otherwise the stop leaves
    # the phase saying "verification is live" and the chain keeps
    # running behind the operator's back.
    #
    # ``verification_passed`` stays empty (it is the security boundary:
    # a passing verdict may not be laundered into a terminal phase),
    # and ``verification_loop_stopped`` is untouched.
    "verification": ["verification_running", "verification_passed", "verification_failed", "verification_loop_stopped", "stopped"],
    # 2026-11-20: ``verification_running`` 多了 ``user_stopping`` 这条边。
    # 停的动作发生在这里（验证链正在跑），停的结果落在 ``user_stopping``
    # 之后 —— 两者之间那一段必须有名字，否则停就又是一次「显示已停、后台仍在
    # 派发」。
    "verification_running": ["verify_first_pass", "verification_passed", "verification_failed", "user_stopping", "stopped"],
    "verify_first_pass": ["verify_recheck", "verification_passed", "verification_failed", "verification_loop_stopped", "stopped"],
    "verify_recheck": ["verification_passed", "verification_failed", "verification_loop_stopped", "stopped"],
    "verification_passed": [],  # terminal: security boundary — no forward edge to ``completed``
    # 2026-10-22: ``verification_rerunning`` left this table with the
    # phase it named (it was a dead state). A row that still holds the
    # historical value is read as ``verification_repairing`` — see
    # :data:`LEGACY_PHASE_ALIASES`.
    "verification_failed": ["verification_repairing", "verification_passed", "verification_loop_stopped", "stopped"],
    # 2026-11-20: ``verification_repairing`` 同样有 ``user_stopping``
    # 这条边 —— 修复轮里跑的验证链也是「跑着的验证」，停它也要先经过「正在
    # 停止」这一档。
    "verification_repairing": ["verification", "verification_passed", "verification_loop_stopped", "verification_failed", "executing", "user_stopping", "stopped"],
    # 2026-11-20: ``user_stopping`` —— 「正在停止」这一档自己的出边。
    #
    # 落点有三个：``stopped``（停的**结果**：计划停住了）、``verification``
    # （这一轮验证已经收干净，停在待验证状态）与 ``verification_repairing``
    # （修复轮还在外面，要回修复路径）。任何**未列出**的相位一律拒绝 ——
    # 尤其是 ``verification_running``：停了还能重新开跑，停就没停。
    #
    # ``stopped`` 必须在列表里：执行停止这条路**永远**过不了执行门禁（执行
    # 没完成才需要停），所以验证侧那两个落点在执行停止路径上是死路 ——
    # 没有这条边，「执行到一半被喊停」会把计划关在这个相位里出不来。
    #
    # 这一行是**验证子表**的，不是主表的：``transition_to`` 取两张表的并集，
    # 所以放哪张表都不影响可走性；放子表是因为这个相位属于验证子机器。
    "user_stopping": ["stopped", "verification", "verification_repairing"],
    "verification_loop_stopped": ["failed", "verification_passed"],

    # ``completed`` is NOT a dead end for the verification sub-machine.
    # Execution ends in ``completed``; the verification round that follows
    # must be able to converge on a terminal verdict from there. Without
    # these two edges a passing OR failing verification report raises
    # ``ValueError`` in ``transition_to`` and the plan is stranded.
    # Note: ``completed -> completed`` stays illegal (self-loops are
    # handled as an idempotent no-op earlier in ``transition_to``, and the
    # table itself must not declare one).
    "completed": ["verification_passed", "verification_failed"],
    "failed": ["executing", "ready", "queued"],
    "stopped": ["executing", "ready", "queued"],
}


#: 专用入口才承认的转移边。
#:
#: 2026-10-25: 验证分支的最小 PRD 允许整条 skip，所以 PRD 审完之后需要一条
#: ``prd_review → tasks_generation`` 的边 —— 跳过 ``prd_approved``（没有一条
#: 决策点被 accept 的 PRD 进不了那个阶段）直接进计划书生成。
#:
#: 这条边**不进** :data:`PHASE_TRANSITIONS`，也不进
#: :data:`VERIFICATION_PHASE_TRANSITIONS`：通用入口
#: （``POST /api/plan/{id}/phase``）走的是 :meth:`PlanState.transition_to`，
#: 它只认那两张表。PRD skip 路径因此只有专用入口
#: :meth:`PlanState.start_verification_plan_generation` 能走 —— 通用入口能走
#: 的话，「这条边存在」就不再意味着「验证分支的最小 PRD 被整条 skip 掉了」。
DEDICATED_PHASE_TRANSITIONS: Dict[str, List[str]] = {
    "prd_review": ["tasks_generation"],
}


def _is_gated_verification_phase(phase: str) -> bool:
    """Is this phase behind the "execution must be complete" gate?

    The gate exists so a plan cannot start verifying before its dev tasks
    have run. The verification branch has no dev tasks — it ships a plan
    doc instead — so ``verification_plan_review`` (the human review gate
    for that doc) is NOT behind the gate.

    2026-10-25: the criterion used to be a bare
    ``phase.startswith("verification")``. Adding a phase whose name starts
    with the same prefix, without excluding it, would have made the phase
    block itself: the doc is written during ``tasks_generation`` and the
    plan would raise "Cannot transition ... without 'execution' in
    completed_phases" at the exact moment the doc lands on disk.

    The exclusion is deliberately narrow — every other ``verification*``
    phase keeps the criterion word for word (and the two ``verify_*``
    phases never matched the prefix to begin with).
    """
    return phase.startswith("verification") and phase != "verification_plan_review"


class PlanState:
    """Central plan state manager for the spec-driven development workflow."""

    def __init__(self, plan_dir: Path):
        self.plan_dir = Path(plan_dir)
        self.state_file = self.plan_dir / PLAN_STATE_FILENAME
        self._lock = threading.Lock()
        #: The phase we last saw ON DISK. ``_save_state_to_sqlite`` writes
        #: ``current_phase`` ONLY when the in-memory value differs from
        #: this — see the note on that method. A ``PlanState`` caches the
        #: row it loaded, so a metadata-only write must not re-state a
        #: phase the cache may be stale on.
        #:
        #: MUST be initialised BEFORE ``_load_state()``.  The order is
        #: kept deliberately: ``_load_state()`` used to run a one-shot
        #: migration that called ``_save_state_to_sqlite``, and that
        #: method reads ``_persisted_phase``.  The migration branch is
        #: gone (see ``_load_state`` below), but the initialisation
        #: still happens first so the attribute can never be read
        #: before it exists — the failure mode there was an
        #: AttributeError swallowed by a blanket ``except`` and a
        #: silently missing routing row, which is exactly the kind of
        #: silent write this class must not do any more.
        #: ``None`` is also the correct starting value for the
        #: legacy/default branches: nothing is on disk yet, so the
        #: first save really does own the column.
        self._persisted_phase: Optional[str] = None
        self._state = self._load_state()
        # The SQLite branch has already put the on-disk value in
        # ``_state``; the legacy/default branches just read it.  In
        # those two branches there is no row on disk yet, so the cache
        # holds the phase the first real write will establish.
        self._persisted_phase = self._state.get("current_phase")

    def _load_state(self) -> dict:
        # task #3.7: read from ``plan_routing`` first; fall back to
        # the legacy ``plan_state.json`` file only when no SQLite row
        # exists. SQLite wins thereafter.
        #
        # 2026-11-11: this method is a PURE READ.  It used to end with
        # a one-shot migration that INSERTed the legacy/default state
        # into ``plan_routing``, so merely *constructing* a
        # ``PlanState`` wrote a row.  That turned every read-only
        # use (``get_current_phase()`` / ``get_state()``) into a
        # write — e.g. ``VerificationOrchestrator.__init__`` builds a
        # ``PlanState``, so every probe registered a plan.  The row is
        # now created by the first real write (see
        # ``_save_state_to_sqlite``); ``write_plan_state`` is an
        # ``INSERT ... ON CONFLICT DO UPDATE``, so the row appears on
        # that first write without any extra code here.
        sqlite_row = self._load_state_from_sqlite()
        if sqlite_row is not None:
            return sqlite_row
        legacy_row = self._load_state_from_legacy_file()
        migrated_from_file = legacy_row is not None
        if legacy_row is None:
            legacy_row = self._default_state()
        # The one-shot migration into ``plan_routing`` that used to
        # live here is gone (2026-11-11): constructing a ``PlanState``
        # must write nothing, or every read-only use of this class
        # becomes a write — e.g. ``VerificationOrchestrator.__init__``
        # builds a ``PlanState``, so every probe registered a plan.
        # The row is created by the first real write, or on demand via
        # ``ensure_row()`` below.
        #
        # What survives from this branch is the *verification* half of
        # the migration.  The legacy file also carried the verification
        # state, and the authoritative table has to receive it too, or
        # the plan loses its round / stop reason once the reads stop
        # consulting the mirror column.  It is guarded twice: it runs
        # only when a legacy file actually existed, and only when the
        # authoritative row does not exist yet.
        if migrated_from_file:
            self._migrate_legacy_verification_state(legacy_row)
        return legacy_row

    def _migrate_legacy_verification_state(self, legacy_row: dict) -> None:
        """Carry the legacy file's verification state into the authoritative row.

        Before the SQLite tables existed, ``plan_state.json`` was the only
        home of the verification state.  The migration copies it into
        ``plan_routing`` (the mirror column), but the mirror column is
        being retired as the source — the authoritative
        ``plan_verification`` row has to be seeded as well, or the plan
        would report ``not_started`` / round 0 the moment the reads stop
        consulting the column.

        Only when the authoritative row **does not exist**: an existing
        ``plan_verification`` row is the newer record and wins.  "Does
        not exist" is answered by :func:`_verification_row_exists` — a
        bare ``SELECT 1`` — and NOT by :func:`_read_verification_row`,
        whose contract is "unreadable = no row".  A row that exists but
        cannot be decoded (one corrupted JSON column) is still an
        existing row, and overwriting it would destroy the
        authoritative record irrecoverably.

        The guard fails closed: when the existence probe cannot answer,
        nothing is migrated.  Overwriting an authoritative row cannot be
        undone; a missing seeding can be redone by running the migration
        again.
        """
        if _verification_row_exists(self.plan_dir.name) is not False:
            return
        verification = legacy_row.get("verification")
        if not isinstance(verification, dict):
            return
        self._write_verification_state(
            status=verification.get("status"),
            round_n=verification.get("round"),
            max_rounds=verification.get("max_rounds"),
            stop_reason=verification.get("stop_reason"),
        )


    def ensure_row(self) -> None:
        """Create the ``plan_routing`` row for this plan if it is missing.

        Explicit on purpose.  Constructing a ``PlanState`` writes
        nothing (2026-11-11), so a caller that needs the row to
        **exist** — e.g. a routing CAS that would otherwise raise
        ``PlanNotFoundError`` because there is no row to move — has to
        ask for it here, by name.

        The row is written from the state this instance holds, with the
        phase that state says, which is exactly the row the constructor
        used to create as a side effect.

        Idempotent: when the row already exists this is a metadata
        write that leaves the phase column alone (the guard in
        :meth:`_save_state_to_sqlite`).
        """
        self._save_state()


    def reload(self) -> None:
        """Re-read the plan state from the authoritative SQLite store.

        Why this exists (2026-08-19 audit): the verification
        orchestrator, the ``/start`` handler, and the auto-loop each
        construct their own ``PlanState`` instance. When any of them
        calls ``transition_to``, the change is persisted to SQLite
        but the OTHER instances' in-memory ``_state`` cache is stale.
        Without a reload, a downstream ``transition_to`` (e.g. on
        the verification pass path) raises ``Illegal transition``
        because the cached ``current_phase`` is out of date, and the
        blanket ``except Exception`` in the callers silently swallows
        the failure — leaving the plan stranded in
        ``verification_running`` with no observable error.

        Callers must invoke ``reload()`` BEFORE any ``transition_to``
        that depends on the latest persisted state.
        """
        fresh = self._load_state()
        with self._lock:
            self._state = fresh
            # Re-sync the write guard: after a reload the cache agrees
            # with disk by construction, so the next metadata write is
            # a no-op for the phase column.
            self._persisted_phase = fresh.get("current_phase")

    def _load_state_from_sqlite(self) -> Optional[dict]:
        """Read the ``plan_routing`` row for this plan_id.

        Returns ``None`` when SQLite is unavailable, the table is
        missing, or no row exists for this plan (e.g. fresh plan
        created before the row was seeded by ``begin_*`` callers).

        The authoritative verification row (``plan_verification``) is
        read from the **same connection** — one snapshot, one close —
        and handed to :meth:`_sqlite_row_to_state`, which is where the
        verification lifecycle keys come from (arch decision point 2).

        This is a **pure read** (VP-015 (c)).  It writes nothing: the
        mirror column is not the source of the verification state, and a
        plan without an authoritative row answers with the documented
        defaults — the same ones ``GET /api/verification/{id}/status``
        gives.  A read that writes made the answer depend on the order
        the readers ran in; the one-time carry-over of a legacy mirror
        stop reason is operator-triggered instead
        (``scripts/migrate_legacy_verification_stop_reason.py``).
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )
            from state_machine.repositories.verification_repository import (
                VerificationRepository,
            )
        except ImportError:
            return None
        try:
            conn = _open_db(_state_db_path())
        except Exception:  # noqa: BLE001
            return None
        try:
            _migrate(conn)
        except Exception:  # noqa: BLE001
            conn.close()
            return None
        try:
            repo = RoutingRepository(conn)
            row = repo.current(self.plan_dir.name)
        except Exception:  # noqa: BLE001
            row = None
        # The verification row is authoritative for the verification
        # lifecycle keys.  A plan without one has never run a round;
        # an unreadable table is treated the same way (no row) rather
        # than as an error — the state read must not break on it.
        try:
            verification_row = VerificationRepository(conn).current(
                self.plan_dir.name
            )
        except Exception:  # noqa: BLE001
            verification_row = None
        try:
            plan_kind, framework_fix = _read_framework_fix_columns(
                conn, self.plan_dir.name
            )
        except Exception:  # noqa: BLE001
            plan_kind, framework_fix = DEFAULT_PLAN_KIND, {}
        finally:
            conn.close()
        if row is None:
            return None
        # Map SQLite columns back to the legacy ``_state`` shape.
        state = self._sqlite_row_to_state(row, verification_row)
        # The framework-fix columns travel with the rest of the state so
        # that ``get_state()`` is a complete picture of the plan — the
        # read model (``backend/framework_fix/read_model.py``) reads the
        # block straight out of it.
        state["plan_kind"] = plan_kind
        state["framework_fix"] = framework_fix
        return state

    def _migrate_mirror_stop_reason(self, row: dict) -> Optional[dict]:
        """Carry a legacy mirror-column stop reason into the authoritative row.

        Only the **reason** is carried: it is a one-time record of a
        past event, like the baseline.  The mirror's status / round /
        max_rounds are NOT a source (arch decision point 3), so the row
        is created with the documented defaults — ``status="not_started"``,
        ``round=0``, ``max_rounds=DEFAULT_MAX_VERIFICATION_ROUNDS``.  The
        row lands in ``plan_verification`` — the authoritative table.

        **Not reachable from any read path** (VP-015 (c)).  This used
        to be the fallback of :meth:`_load_state_from_sqlite`, which
        made a read a write: the same plan answered differently
        depending on the order the readers ran in, and the mirror
        column was a state source again.  The one-time carry-over is
        operator-triggered now —
        ``scripts/migrate_legacy_verification_stop_reason.py`` — and
        this method is the ``PlanState``-scoped door onto it.

        Returns the authoritative row after the migration, or ``None``
        when there was nothing to carry.
        """
        return migrate_mirror_stop_reason(self.plan_dir.name, row)

    @staticmethod
    def _sqlite_row_to_state(row: dict, verification_row: Optional[dict] = None) -> dict:
        """Translate a ``plan_routing`` row into the legacy
        ``PlanState._state`` dict shape so getters/setters keep
        working unchanged.

        The verification lifecycle keys — ``status`` / ``round`` /
        ``max_rounds`` / ``stop_reason`` — come from the **authoritative**
        ``plan_verification`` row (``verification_row``), not from the
        ``plan_routing.verification`` mirror column (arch decision
        point 2: the value a consumer reads is the authoritative one;
        arch decision point 3: the mirror column is no longer the source
        of the verification state).

        The mirror column keeps ONE job: it is where the frozen
        ``baseline`` / ``baseline_doc_sha256`` are recorded.  They are
        not verification state — ``plan_verification`` has no such
        columns — so they are decoded from the column and nothing else.

        ``verification_row`` is optional so the single-argument call
        (:meth:`_sqlite_row_to_state(row)`, used by several test
        helpers) keeps working: it then reads the authoritative row for
        the plan itself, and falls back to the documented defaults — the
        same ones ``GET /api/verification/{id}/status`` gives for a plan
        without a verification row — when there is none.
        """
        # The mirror column: only the baseline keys are read from it.
        # A corrupted JSON blob means "no baseline" — never a crash,
        # and never a reason to doubt the authoritative row.
        mirror = _decode_json_column(row.get("verification"), {})
        if not isinstance(mirror, dict):
            mirror = {}

        if verification_row is None:
            # No row was handed in: read the authoritative one for this
            # plan.  The mirror column is NEVER a fallback — it is not
            # the source of the verification state.
            verification_row = _read_verification_row(row.get("plan_id"))

        if verification_row is None:
            status = "not_started"
            round_n = 0
            max_rounds = DEFAULT_MAX_VERIFICATION_ROUNDS
            stop_reason: Optional[str] = None
        else:
            status = verification_row.get("verification_status") or "not_started"
            round_n = verification_row.get("round") or 0
            # ``0`` / ``None`` means "unset" — a row created by a narrow
            # write carries no budget.  The /status endpoint answers
            # the same way (``row.get("max_rounds") or DEFAULT``).
            max_rounds = (
                verification_row.get("max_rounds")
                or DEFAULT_MAX_VERIFICATION_ROUNDS
            )
            stop_reason = verification_row.get("verification_stop_reason")

        verification = {
            "status": status,
            "round": round_n,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        }
        # The baseline is NOT verification state: it is a frozen fact
        # about the document that was reviewed, recorded in the mirror
        # column (the only place it lives).  Carry it over untouched.
        if "baseline" in mirror:
            verification["baseline"] = mirror["baseline"]
        if "baseline_doc_sha256" in mirror:
            verification["baseline_doc_sha256"] = mirror["baseline_doc_sha256"]

        return {
            "plan_id": row.get("plan_id"),
            # 2026-10-22: the read path maps the historical
            # ``verification_rerunning`` value onto the phase it stood
            # for, so a row written before that dead state was removed
            # is read as ``verification_repairing`` instead of a phase
            # that is no longer in the vocabulary.
            "current_phase": normalize_phase(
                row.get("current_phase") or "interview"
            ),
            "completed_phases": _decode_json_column(row.get("completed_phases"), []),
            "review_rounds": _decode_json_column(row.get("review_rounds"), {"prd": 0, "arch": 0, "test": 0}),
            "flags": _decode_json_column(row.get("flags"), {}),
            "verification": verification,
            "last_updated": row.get("last_updated") or row.get("updated_at"),
        }

    def _load_state_from_legacy_file(self) -> Optional[dict]:
        """Read the legacy ``plan_state.json`` file (pre-task #3.7)."""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return self._default_state()
        completed_phases = data.get("completed_phases") or []
        review_rounds = data.get("review_rounds") or {"prd": 0, "arch": 0, "test": 0}
        flags = data.get("flags") or {}
        # 2026-11-20: 历史别名先归一化再判合法性 —— 旧文件里存着
        # ``verification_stopping``（旧名），归一化成 ``user_stopping`` 之后
        # 它是合法相位；不归一化的话这份文件会掉回 ``interview``，一条停到
        # 一半的计划就被重置掉了。
        #
        # SQLite 那条读路径（:meth:`_sqlite_row_to_state`）同样做归一化 ——
        # 两条读路径必须给出同一个答案，同一个计划落在哪种存储上不该决定
        # 它被读成什么。
        current_phase = normalize_phase(data.get("current_phase"))
        if current_phase is None or current_phase not in VALID_PHASES:
            current_phase = "interview"
        verification = data.get("verification", {}) or {}
        return {
            "plan_id": data.get("plan_id", self.plan_dir.name),
            "current_phase": current_phase,
            "completed_phases": completed_phases,
            "review_rounds": review_rounds,
            "flags": flags,
            "verification": {
                "status": verification.get("status", "pending"),
                "round": verification.get("round", 0),
                "max_rounds": verification.get("max_rounds", 3),
                "stop_reason": verification.get("stop_reason", None),
            },
            "last_updated": data.get("last_updated", datetime.utcnow().isoformat() + "Z"),
        }

    def _default_state(self) -> dict:
        phase = self._infer_phase()
        return {
            "plan_id": self.plan_dir.name,
            "current_phase": phase,
            "completed_phases": [],
            "review_rounds": {
                "prd": 0,
                "arch": 0,
                "test": 0,
            },
            "flags": {
                "arch_enabled": (self.plan_dir / "arch-design.md").exists(),
                "test_enabled": (self.plan_dir / "test-design.md").exists(),
                "preflight_enabled": True,
                "self_review_enabled": True,
                # A plan that says nothing runs for real.  The mode is
                # stored next to the other flags so it travels with the
                # routing row -- the cost gate, the executor and the
                # verification sub-agent are three processes, and they
                # have to agree on it.
                "mode": MODE_PRODUCTION,
            },
            "verification": {
                # A plan without a ``plan_verification`` row has not
                # started verifying — the same defaults
                # ``GET /api/verification/{id}/status`` answers with.
                # The mirror column used to say ``pending`` here, which
                # made the first read of a fresh plan disagree with
                # every later read (arch decision point 2).
                "status": "not_started",
                "round": 0,
                "max_rounds": DEFAULT_MAX_VERIFICATION_ROUNDS,
                "stop_reason": None,
            },
            "last_updated": datetime.utcnow().isoformat() + "Z",
        }

    def _infer_phase(self) -> str:
        """Infer the phase from the artifacts sitting in the plan directory.

        The artifact verdict comes from :func:`plan_artifacts.detect` — the
        ONE place that decides "which branch is this plan on". This method
        used to stat the files itself, which is how the branch verdict ended
        up computed in three places and drifted.

        Order matters and is fixed:

        1. ``tasks.json`` (dev branch) → ``queued``;
        2. ``verification-plan.md`` (verification branch) →
           ``verification_plan_review`` — the doc exists, so the plan is
           waiting for a human to accept / skip its items. It must NOT fall
           back to ``prd_review`` / ``queued``;
        3. the legacy chain (``test-design.md`` / ``arch-design.md`` /
           ``review.json`` / ``prd.json`` / ``interview.json``) — unchanged,
           word for word.

        The VP manifest (``verification_plan.json``) is **not** the plan doc:
        it is the machine translation of the doc, and it never puts a plan
        into the review phase.

        2026-10-25 (R10): 分支 1 的结果从 ``ready`` 改成 ``queued`` ——
        ``queued`` 是唯一的等待相位（``ready`` 只作输入别名）。检测器是**读**
        路径：它给出的必须是现行词汇，否则一条刚建出来、还没落 routing 行的
        计划会带着旧词进入系统。

        2026-11-09: 共存（``tasks.json`` 与 ``verification-plan.md`` 同时存在）
        **不是冲突** —— 按「任务优先」的主分支语义，它就是一条开发分支的
        计划，只不过额外带着一份计划书（附加产物）。产物判定的输出域因此是
        ``dev`` / ``verification`` / ``empty`` 三个取值，共存落在 ``dev``
        上，走第 1 条规则推断为 ``queued``。这里曾经有一条「两者都在就抛
        ``ArtifactConflictError``」的分支：它要求人先裁决「这条 plan 到底想
        干什么」，而共存合法之后，裁决已经在「任务优先」这条规则里了 ——
        留着它只会让读者以为框架还在替人择一。
        """
        artifacts = detect(self.plan_dir)
        if artifacts.branch == BRANCH_DEV:
            return "queued"
        if artifacts.branch == BRANCH_VERIFICATION:
            return "verification_plan_review"
        if (self.plan_dir / "test-design.md").exists():
            return "test_approved"
        if (self.plan_dir / "arch-design.md").exists():
            return "arch_approved"
        if (self.plan_dir / "review.json").exists() and self._is_prd_review_complete():
            return "prd_approved"
        if (self.plan_dir / "prd.json").exists() or (self.plan_dir / "prd.md").exists():
            return "prd_review"
        if (self.plan_dir / "interview.json").exists():
            return "interview_complete"
        return "interview"

    def _is_prd_review_complete(self) -> bool:
        """
        Check whether every PRD decision point has been accepted in review.json.

        Returns:
            True if all decision_points in prd.json have a corresponding
            review.json item with status "accepted" (or if prd.json has
            zero decision points, for backward compatibility).
        """
        prd_file = self.plan_dir / "prd.json"
        if not prd_file.exists():
            return True

        try:
            with open(prd_file, "r", encoding="utf-8") as f:
                prd_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False

        decision_points = prd_data.get("decision_points", []) or []
        if not decision_points:
            return True

        review_file = self.plan_dir / "review.json"
        if not review_file.exists():
            return False

        try:
            with open(review_file, "r", encoding="utf-8") as f:
                review_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False

        review_items = review_data.get("items", []) or []
        accepted_indices = {
            item.get("index")
            for item in review_items
            if item.get("status") == "accepted"
        }
        required_indices = {dp.get("index") for dp in decision_points}
        return required_indices.issubset(accepted_indices)

    def _save_state(self, completed_phases_mode: str = "merge"):
        # task #3.7: persist to ``plan_routing`` instead of writing
        # the legacy ``plan_state.json`` file. Falls back to no-op
        # when SQLite is unavailable (the legacy file is preserved
        # as a read-only cache and will be picked up by ``_load_state``
        # on the next access).
        #
        # ``completed_phases_mode`` is forwarded verbatim to
        # :meth:`_save_state_to_sqlite`.  The default ``"merge"`` keeps
        # the phase history append-only; only :meth:`reset_to` asks
        # for ``"replace"``, because rewinding a plan is exactly the
        # one case where history must be dropped.
        self._state["last_updated"] = datetime.utcnow().isoformat() + "Z"
        self._save_state_to_sqlite(
            self._state, completed_phases_mode=completed_phases_mode,
        )

    def _save_state_to_sqlite(
        self,
        state: dict,
        completed_phases_mode: str = "merge",
    ) -> None:
        """Write the plan-state fields into ``plan_routing``.

        Delegates the column ownership to
        :meth:`RoutingRepository.write_plan_state`, which touches ONLY
        the ``PlanState``-owned columns and leaves the CAS's
        ``version`` / ``substage`` alone.

        The ``verification`` column is **not** among them: it is the
        mirror of the verification state, and the mirror no longer
        carries that state (arch decision point 3).  The frozen
        baseline is the only thing left in it, and it has its own
        narrow writer — :meth:`_write_verification_baseline`.

        The phase column is written **only when this instance actually
        moved it** — i.e. when the in-memory value differs from the one
        we last saw on disk (:attr:`_persisted_phase`).  That guard is
        the load-bearing part.

        ``PlanState`` caches the row it loaded, and every setter
        (``set_verification_max_rounds``, ``enable_arch``, …) funnels
        through :meth:`_save_state`, so without the guard a harmless
        metadata write would re-state a *cached* phase — silently undoing
        a phase another writer had CAS'd in the meantime.  That is not
        hypothetical: ``POST /api/verification/{id}/reset_rounds`` ends
        with ``set_verification_max_rounds``, which used to snap the
        routing value back and make the very next ``/start`` answer
        ``409 stage_mismatch`` (reproduced twice on a production plan).

        Before schema v5 the protection lived in the *caller* as a
        ~30-line special case that preserved an existing ``stage`` when
        ``current_phase`` was unchanged — because ``stage`` was the other
        column and this class owned only ``current_phase``.  With one
        column the guard has to be here, and stated as the honest rule:
        write the phase when you changed it, otherwise leave it alone.

        The same honesty rule applies to ``completed_phases``, and for
        the same reason: this instance's ``state`` is a *cached
        snapshot* of the row, so it may be older than an append another
        writer made in the meantime.  ``completed_phases_mode``
        therefore defaults to ``"merge"`` — the write adds the phases
        this instance knows about and keeps the ones already recorded
        on the row (the routing CAS maintains the very same ledger, so
        the two must not fight).  Only :meth:`reset_to` passes
        ``"replace"``: rewinding the plan is the one operation whose
        purpose is to *forget* the phases after the target.

        **Who creates the row (2026-11-11).**  The constructor no
        longer creates it — ``_load_state`` is a pure read, so merely
        constructing a ``PlanState`` writes nothing.  The row appears
        on the FIRST real write, through the repository's
        ``INSERT ... ON CONFLICT DO UPDATE``.  Because that INSERT arm
        is the one that CREATES the row, it has to carry the phase:
        this method therefore passes the phase whenever the row does
        not exist yet, and only falls back to the "did I move it?"
        rule when the row is already there.  See the inline comment
        below.
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )
        except ImportError:
            return
        try:
            conn = _open_db(_state_db_path())
        except Exception:  # noqa: BLE001
            return
        current_phase = state.get("current_phase", "interview")
        try:
            _migrate(conn)
            repo = RoutingRepository(conn)
            # 2026-11-11: the routing row no longer exists by the time
            # we get here — the constructor stopped creating it (see
            # ``_load_state``).  Two cases, two different rules for
            # the phase column:
            #
            # * the row IS there → today's rule: write the phase only
            #   when this instance moved it, otherwise leave the column
            #   alone (a cached phase must never drag the live value
            #   back over a phase another writer CAS'd in).
            # * the row is NOT there → this write CREATES it, so the
            #   INSERT arm must carry the phase we hold.  Without it
            #   the column would fall back to its schema DEFAULT
            #   ('interview') and the fresh routing row would
            #   contradict the state we just read — e.g. a legacy plan
            #   whose ``plan_state.json`` says ``prd_review`` would
            #   get a row saying ``interview``.
            try:
                row_exists = repo.current(self.plan_dir.name) is not None
            except Exception:  # noqa: BLE001
                # Unknown → keep the conservative rule and never
                # restate a phase we did not move.
                row_exists = True
            if row_exists:
                # ``None`` tells the repository to leave the column
                # untouched.
                phase_arg = (
                    current_phase
                    if current_phase != self._persisted_phase else None
                )
            else:
                phase_arg = current_phase
            repo.write_plan_state(
                self.plan_dir.name,
                phase=phase_arg,
                completed_phases=state.get("completed_phases", []),
                review_rounds=state.get("review_rounds", {}),
                flags=state.get("flags", {}),
                last_updated=state.get("last_updated"),
                completed_phases_mode=completed_phases_mode,
            )
            self._persisted_phase = current_phase
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()

    # --- Getters ---

    def get_state(self) -> dict:
        return dict(self._state)

    def get_current_phase(self) -> str:
        return self._state.get("current_phase", "interview")

    def get_completed_phases(self) -> List[str]:
        return list(self._state.get("completed_phases", []))

    def get_review_round(self, phase: str) -> int:
        return self._state.get("review_rounds", {}).get(phase, 0)

    def is_arch_enabled(self) -> bool:
        return self._state.get("flags", {}).get("arch_enabled", False)

    def is_test_enabled(self) -> bool:
        return self._state.get("flags", {}).get("test_enabled", False)

    def is_preflight_enabled(self) -> bool:
        """Return whether the preflight cross-doc check runs before tasks gen.

        Defaults to ``True`` for new plans (DP2 contract). Existing plans
        that pre-date the flag also see ``True`` because the field is
        populated by ``_default_state`` and is read with ``get(..., True)``
        to be robust to partial JSON written by older callers.
        """
        return self._state.get("flags", {}).get("preflight_enabled", True)

    def is_self_review_enabled(self) -> bool:
        """Return whether the post-emit self-review audit trail runs.

        Defaults to ``True`` for new plans (DP1 contract). Plans
        written before the flag landed (``plan-state sidecar`` missing
        ``flags.self_review_enabled``) also see ``True`` so the
        audit trail keeps flowing without manual migration.
        """
        return self._state.get("flags", {}).get("self_review_enabled", True)

    def get_mode(self) -> str:
        """Return the plan's mode -- ``"production"`` or ``"test"``.

        The mode is read from ``flags["mode"]`` through
        :func:`plan_mode.mode_from_flags`, so this method and every other
        reader of the mode get the same answer for the same row.

        A plan written before modes existed has no ``mode`` key at all.
        That is a **missing** mode, not an error: the reader falls back
        to ``production`` and says so in the log (``plan_mode_missing``).
        The fallback is ``production`` because that is the mode that must
        be safe to assume -- falling back to ``test`` would silently
        turn real work into a rehearsal.
        """
        mode, _is_fallback = mode_from_flags(self._state.get("flags", {}))
        return mode

    # --- 框架修复：plan_kind 与 framework_fix 块 --------------------------
    #
    # 架构决策点 4：五种可见状态里只有 ``blocked_on_framework_fix`` 落在
    # **相位**上（且只在「全部剩余工作被阻塞」时），其余四种
    # （``waiting_on_user_merge`` / ``framework_fix_exhausted`` /
    # ``scheduler_deadlock_suspected`` / ``reopened``）住在 ``framework_fix``
    # JSON 块里 —— 展示由读模型（``backend/framework_fix/read_model.py``）
    # 投影。
    #
    # 所以这一节改的是**两列**：``plan_kind`` 说这个 plan 是什么计划，
    # ``framework_fix`` 说它此刻被框架缺陷挡成了什么样。
    #
    # 2026-11-25：相位词表**多了一个相位** —— ``blocked_on_framework_fix``
    # 进了 ``VALID_PHASES`` / ``PHASE_TRANSITIONS`` / ``PHASE_LABELS``
    # （架构决策点 4 要求它有常量与合法迁移）。它只在「全部剩余工作被阻塞」
    # 时被写入 —— 那个判定由 ``backend/framework_fix/lifecycle.py`` 做，
    # 这里提供的是**相位机制**（写进去 / 从里面出来）。

    def get_plan_kind(self) -> str:
        """这个 plan 是什么计划 —— ``normal`` 或 ``framework_fix``。

        读的是**活库里的那一列**，不是构造时的快照：计划种类是别人写的，
        读的人要看到最新的。

        读不出来时（数据库不可用）退回构造时缓存的值，再退回
        :data:`DEFAULT_PLAN_KIND` —— 旧计划没有这个概念，读成普通计划。
        """
        read = _read_routing_framework_fix(self.plan_dir.name)
        if read is not None:
            return read[0]
        cached = self._state.get("plan_kind")
        return str(cached) if cached else DEFAULT_PLAN_KIND

    def get_framework_fix(self) -> dict:
        """这个 plan 的框架修复块 —— ``{}`` 表示没有框架修复状态。

        读的是**活库里的那一块**：挂起是别人写的，读模型要看到最新的。

        读不出来时（数据库不可用）退回构造时缓存的值，再退回 ``{}``。
        """
        read = _read_routing_framework_fix(self.plan_dir.name)
        if read is not None:
            return dict(read[1])
        cached = self._state.get("framework_fix")
        return dict(cached) if isinstance(cached, dict) else {}

    def enter_block(
        self,
        plan_id: str,
        *,
        fingerprint: str,
        fix_plan_id: str,
    ) -> None:
        """把这个 plan 挂进框架修复块。

        块里记着三件事：本计划被挂住了（``hold``）、挂它的是哪个框架缺陷
        （``fingerprint``）、以及替它修的计划是谁（``fix_plan_id``）。第四个
        键 ``unclassified_rounds`` 是**别的写者**维护的计数（分类器判不出
        这一轮属于哪个缺陷时递增），挂起动作不碰它。

        幂等：重复调用**不重置** ``unclassified_rounds``。把它清零等于把
        「判不出来」的历史抹掉了 —— 而「判不出来几轮」正是决定这个签名
        是否耗尽的依据。

        写失败抛错（见 :func:`_write_routing_framework_fix`）：挂起写不进去
        却报告成功，比写失败更糟。
        """
        read = _read_routing_framework_fix(plan_id)
        current = dict(read[1]) if read is not None else {}
        rounds = current.get("unclassified_rounds")
        if not isinstance(rounds, int) or isinstance(rounds, bool):
            rounds = 0
        block = dict(current)
        block["hold"] = True
        block["fingerprint"] = fingerprint
        block["fix_plan_id"] = fix_plan_id
        block["unclassified_rounds"] = rounds
        _write_routing_framework_fix(plan_id, block)
        if plan_id == self.plan_dir.name:
            self._state["framework_fix"] = dict(block)

    def exit_block(self, plan_id: str) -> None:
        """把这个 plan 从框架修复块里放出来。

        没有块 → **无操作**：不写库，连行都不建。退块不是一次写，它只是
        「把挂起撤掉」—— 没有挂起可撤的时候，它什么都不做。

        有块 → 把块清空，列回到 ``{}`` —— 也就是「这个计划没有框架修复
        状态」这个文档化的默认值。块里的历史（哪个指纹、哪个修复计划）
        由注册表的挂接记录保存，不靠这块 JSON 记。
        """
        read = _read_routing_framework_fix(plan_id)
        if read is not None and not read[1]:
            return
        _write_routing_framework_fix(plan_id, {})
        if plan_id == self.plan_dir.name:
            self._state["framework_fix"] = {}

    # --- 框架修复：阻塞相位 -----------------------------------------------
    def enter_blocked_phase(self) -> bool:
        """把这条计划移进「被框架缺陷阻塞」相位。

        这个方法只做**相位机制**：判断当前相位能不能走到阻塞相位，然后走。
        「该不该阻塞」（全部剩余工作是不是都被挡住了）是**业务判定** ——
        由 ``backend/framework_fix/lifecycle.py`` 拿着挂起事实做完了才调
        这里的。

        走不动就**不动**：当前相位没有到阻塞相位的那条边时返回 ``False``，
        相位留在原处。一个「该阻塞却没阻塞」的计划仍然是能看见、能操作的
        计划；一个「被硬推进去」的计划则落在没人能解释的相位上。

        Args:
            无 —— 作用对象是这条计划自己（``self.plan_dir.name``）。

        Returns:
            计划此刻是否停在阻塞相位上（已经在上面也算）。
        """
        self.reload()
        current = self._state.get("current_phase", "")
        if current == BLOCKED_PHASE:
            return True
        allowed = set(PHASE_TRANSITIONS.get(current, []))
        allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(current, []))
        if BLOCKED_PHASE not in allowed:
            return False
        self.transition_to(BLOCKED_PHASE)
        return True

    def leave_blocked_phase(self) -> bool:
        """把这条计划从阻塞相位带回调度队列。

        挂起被释放之后，计划要回队列重新被调度一次 —— 它不是终态：被框架
        缺陷挡住的那段时间不算「做砸了」，计划还要把那件事做完。

        不在阻塞相位上 → 返回 ``False``，什么都不做。

        Returns:
            是否真的移出去了。
        """
        self.reload()
        if self._state.get("current_phase", "") != BLOCKED_PHASE:
            return False
        self.transition_to("queued")
        return True

    # --- Phase Transitions ---

    def force_set_phase(self, phase: str):
        """Set phase directly, bypassing transition rules.

        Two callers, both of which know the current phase cannot be
        walked forward through a declared edge:

        * crash recovery (_mark_failed_dead), where the server crashed
          between writing execution-state sidecar and transitioning
          plan_state;
        * ``ensure_verification_repairing`` (2026-11-08), which forces the
          two terminal verification phases (``verification_passed`` and
          ``verification_loop_stopped``) back into
          ``verification_repairing`` when the pending repair queue says
          the chain has not converged.  Neither terminal phase has a
          forward edge, so the repair dispatch has no legal road out of
          them — the force is what makes the road exist, and it is logged.
        """
        if phase not in VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}. Must be one of {VALID_PHASES}")
        current = self._state.get("current_phase", "")
        if current and current not in self._state["completed_phases"]:
            self._state["completed_phases"].append(current)
        self._state["current_phase"] = phase
        self._save_state()

    def transition_to(self, phase: str):
        """Move the plan to ``phase``.

        The legal targets come from :data:`PHASE_TRANSITIONS` and
        :data:`VERIFICATION_PHASE_TRANSITIONS`. The **dedicated** edges —
        the ones only :meth:`start_verification_plan_generation` may take —
        are deliberately NOT part of this entry: the generic
        ``POST /api/plan/{id}/phase`` route goes through here, and the PRD
        skip path must not be reachable from it.

        2026-10-25 (R10): 入口先做**等待相位归一化**。``ready`` 是
        ``queued`` 的历史别名 —— 既有数据库行里存着它，调用方也还在传它，
        但写路径不再产生它。命中别名时：

        * 打一条弃用日志（带 plan_id 与来源 ``plan_state``）：旧数据要能清得
          掉，先得知道它在哪儿；
        * 用归一化后的值继续 —— 归一化后与当前相位相同就是幂等 no-op，
          不抛异常。

        归一化在**合法性检查之前**：否则 ``transition_to('ready')`` 会拿旧词
        去查表，而表里的等待相位写的是 ``queued``，一条本来合法的转移会以
        「非法转移」的形式炸掉。
        """
        normalized, aliased = normalize_waiting_phase(phase)
        if aliased:
            log_deprecated_ready(
                logger,
                self._state.get("plan_id") or self.plan_dir.name,
                "plan_state",
            )
        self._transition_to(normalized)

    def _transition_to(
        self,
        phase: str,
        *,
        extra_allowed: Optional[List[str]] = None,
    ):
        """The transition machinery, with an optional extra set of targets.

        ``extra_allowed`` is how the dedicated edges get in: the caller
        names them explicitly, so they can never leak into the generic
        entry point.
        """
        if phase not in VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}. Must be one of {VALID_PHASES}")

        current = self._state.get("current_phase", "")

        # Idempotent: same phase is a no-op
        if current == phase:
            return

        # Validate transition legality (check both main and verification tables)
        allowed = set(PHASE_TRANSITIONS.get(current, []))
        allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(current, []))
        if extra_allowed:
            allowed.update(extra_allowed)
        # 2026-11-09: ``archived`` 是**通用目标** —— 任何相位都可以直接被
        # 归档，所以这里补一条判断，而不是往三十多条相位边里逐条加
        # ``"archived"``。
        #
        # 为什么是通用目标：归档是一个**操作者的动作**（把一条计划收起来，
        # 永远别再跑它），不是工作流的一个步骤。它要能从任何地方发生 ——
        # 访谈中就黄了的历史计划、排队中等着的计划、停下了的计划、跑挂了的
        # 计划，全都可能被归档。逐条加边的话，任何一条新相位都会**漏掉**这条
        # 边，于是「归档一条新相位的计划」会炸成「非法转移」。
        #
        # 唯一的例外是 ``completed``：它表示「真的做完了」，把它改写成
        # ``archived`` 会抹掉「它完成过」这个事实 —— 而「完成」与「归档」正是
        # 本相位要与 ``completed`` 区分开的两件事。``stopped`` / ``failed``
        # 则是「没做成」，把它们收起来正是本相位存在的意义（历史上那批从没跑过
        # 的旧计划就是这么从 ``stopped`` 迁过来的）。
        if phase == "archived" and current not in _ARCHIVE_EXCLUDED_SOURCES:
            allowed.add("archived")
        if current and phase not in allowed:
            raise ValueError(
                f"Illegal transition from '{current}' to '{phase}'. "
                f"Allowed: {sorted(allowed)}"
            )

        # Enforce execution completion before verification
        # Special case: allow transition from 'executing' directly to verification phases
        #
        # 2026-10-25: the prefix criterion now EXCLUDES
        # ``verification_plan_review``. That phase is the verification
        # branch's plan-doc review gate: a verification-branch plan never
        # runs the dev task list, so there is no ``execution`` to wait for.
        # Without the exclusion the phase would block itself — the doc is
        # written during ``tasks_generation`` and the plan would raise
        # "Cannot transition ... without 'execution' in completed_phases"
        # at the exact moment the doc lands on disk.
        #
        # The exclusion is deliberately narrow: every other
        # ``verification*`` phase (and the two ``verify_*`` phases, which
        # never matched the prefix to begin with) keeps the criterion
        # word for word.
        #
        # 2026-10-25: the criterion itself widened by one disjunct — the
        # verification branch has no execution output at all, so
        # "``execution`` in completed_phases" is permanently false there
        # and the plan could never enter verification. The gate is now
        # "``execution`` in completed_phases **or** the verification
        # branch is ready" (the one predicate both gates ask). On the dev
        # branch the right-hand side is permanently false, so the original
        # verdict applies word for word.
        if (
            _is_gated_verification_phase(phase)
            and current != "executing"
            and not self._execution_gate_open()
        ):
            raise ValueError(
                f"Cannot transition to '{phase}' without 'execution' in completed_phases "
                f"and without a ready verification branch. "
                f"Current completed_phases: {self._state.get('completed_phases', [])}"
            )

        if current and current not in self._state["completed_phases"]:
            self._state["completed_phases"].append(current)

        self._state["current_phase"] = phase

        # Auto-append 'execution' to completed_phases when leaving 'executing' for verification (idempotent)
        if current == "executing" and _is_gated_verification_phase(phase) and "execution" not in self._state["completed_phases"]:
            self._state["completed_phases"].append("execution")

        # Auto-append 'execution' to completed_phases for terminal states (idempotent)
        #
        # 2026-11-09: ``archived`` 被排除在外。归档的对象恰恰是**从没跑过**的
        # 计划 —— 给它补一条 ``execution`` 等于在相位历史里记下一件它没做过
        # 的事。``completed`` / ``failed`` / ``stopped`` 的行为一个字没动。
        # （routing_repository 里同一条规则必须一模一样 —— 两层的相位历史要
        # 对得上，见那里的注释。）
        if (
            phase in TERMINAL_PHASES
            and phase != "archived"
            and "execution" not in self._state["completed_phases"]
        ):
            self._state["completed_phases"].append("execution")

        # Update verification status when transitioning to verification phases
        verification_status = _verification_status_for_phase(phase)

        if verification_status is not None:
            self._state["verification"]["status"] = verification_status
            # 2026-11-04: the status belongs to the authoritative
            # ``plan_verification`` row (arch decision point 1) — the
            # read path answers from that row (arch decision point 2),
            # so a phase transition that only touched the in-memory copy
            # would leave the getter reporting a status nobody
            # recorded.  The write is best-effort: an unreachable state
            # store must not break the phase machine.
            self._write_verification_state(status=verification_status)

        self._save_state()

    def mark_phase_complete(self, phase: str):
        if phase not in self._state["completed_phases"]:
            self._state["completed_phases"].append(phase)
        self._save_state()

    def reset_to(self, phase: str):
        """Reset state backward to a given phase, removing all subsequent progress."""
        if phase not in VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}")

        # 2026-10-25 (R10): ``queued`` 进 ``phase_order`` —— 等待相位合并成
        # ``queued`` 之后，回退到「等人」也必须是一个合法的落点。
        phase_order = [
            "interview", "interview_complete", "prd_generation",
            "prd_review", "prd_refining", "prd_approved",
            "arch_generation", "arch_review", "arch_refining", "arch_approved",
            "test_generation", "test_review", "test_refining", "test_approved",
            "tasks_generation", "ready", "queued", "executing",
        ]
        phase_idx = phase_order.index(phase)

        # Keep only completed phases up to (not including) the target
        self._state["completed_phases"] = [
            p for p in self._state["completed_phases"]
            if p in phase_order and phase_order.index(p) < phase_idx
        ]

        self._state["current_phase"] = phase
        # ``replace`` — rewinding the plan is the ONE operation whose
        # purpose is to forget the history after the target phase.
        # Every other write merges (see ``_save_state_to_sqlite``).
        self._save_state(completed_phases_mode="replace")

    # --- Review Rounds ---

    def increment_review_round(self, phase: str):
        rounds = self._state.get("review_rounds", {})
        rounds[phase] = rounds.get(phase, 0) + 1
        self._state["review_rounds"] = rounds
        self._save_state()

    def reset_review_round(self, phase: str):
        rounds = self._state.get("review_rounds", {})
        rounds[phase] = 0
        self._state["review_rounds"] = rounds
        self._save_state()

    # --- Flags ---

    def enable_arch(self, enabled: bool = True):
        self._state["flags"]["arch_enabled"] = enabled
        self._save_state()

    def enable_test(self, enabled: bool = True):
        self._state["flags"]["test_enabled"] = enabled
        self._save_state()

    def enable_preflight(self, enabled: bool = True):
        """Toggle the preflight gate (DP2). When False, tasks generation
        skips the cross-document reviewer entirely.
        """
        self._state["flags"]["preflight_enabled"] = enabled
        self._save_state()

    def enable_self_review(self, enabled: bool = True):
        """Toggle the post-emit self-review audit trail (DP1). When False,
        the prd/arch/test generators skip ``run_doc_self_review``, do NOT
        write ``{doc_type}_self_review.json`` audit-trail reports, and
        do NOT emit the corresponding ``prd_self_review`` /
        ``arch_self_review`` / ``test_self_review`` log events.
        """
        self._state["flags"]["self_review_enabled"] = enabled
        self._save_state()

    def set_mode(self, mode: Any) -> str:
        """Set the plan's mode and persist it.  Returns what was stored.

        The value is **resolved** first (:func:`plan_mode.resolve_mode`),
        so what lands in ``flags["mode"]`` is always one of the known
        modes.  An unrecognised value falls back to ``production``, is
        logged (``plan_mode_invalid=``), and the fallback is what gets
        stored -- so the row can never carry a mode that some other
        reader would have to guess about.

        The write follows the same shape as the other flag setters
        (:meth:`enable_arch` and friends): change ``self._state["flags"]``,
        then :meth:`_save_state`.  The write is immediate -- the mode is
        read by other processes, so it cannot wait for a later save.
        """
        resolved, _is_fallback = resolve_mode(mode)
        self._state["flags"]["mode"] = resolved
        self._save_state()
        return resolved

    # --- Convenience Helpers ---

    def is_interview_complete(self) -> bool:
        return self.get_current_phase() in [
            "interview_complete", "prd_generation", "prd_review",
            "prd_refining", "prd_approved", "arch_generation",
            "arch_review", "arch_refining", "arch_approved",
            "test_generation", "test_review", "test_refining",
            "test_approved", "tasks_generation", "ready", "queued", "executing",
        ]

    def is_prd_approved(self) -> bool:
        return self.get_current_phase() in [
            "prd_approved", "arch_generation", "arch_review",
            "arch_refining", "arch_approved", "test_generation",
            "test_review", "test_refining", "test_approved",
            "tasks_generation", "ready", "queued", "executing",
        ]

    def is_arch_approved(self) -> bool:
        return self.get_current_phase() in [
            "arch_approved", "test_generation", "test_review",
            "test_refining", "test_approved", "tasks_generation",
            "ready", "queued", "executing",
        ]

    def is_test_approved(self) -> bool:
        return self.get_current_phase() in [
            "test_approved", "tasks_generation", "ready", "queued", "executing",
        ]

    def is_ready_for_tasks(self) -> bool:
        current = self.get_current_phase()
        if current == "prd_approved" and not self.is_arch_enabled():
            return True
        if current == "arch_approved" and not self.is_test_enabled():
            return True
        if current == "test_approved":
            return True
        return False

    def can_enable_arch(self) -> bool:
        return self.is_prd_approved()

    def can_enable_test(self) -> bool:
        return self.is_arch_approved() or (
            self.is_arch_enabled() and self.get_current_phase() == "arch_approved"
        )

    # --- Verification Phase Methods ---
    #
    # 2026-11-04: the four lifecycle values are the authoritative
    # ``plan_verification`` row's, read by :meth:`_sqlite_row_to_state`
    # (arch decision point 2).  The fallbacks below are the documented
    # defaults for a plan that has no verification row — the same ones
    # ``GET /api/verification/{id}/status`` answers with.

    def get_verification_status(self) -> str:
        """Get current verification status (not_started/running/passed/failed/loop_stopped)."""
        return self._state.get("verification", {}).get("status", "not_started")

    def get_verification_round(self) -> int:
        """Get current verification round number."""
        return self._state.get("verification", {}).get("round", 0)

    def get_verification_max_rounds(self) -> int:
        """Get maximum verification rounds allowed."""
        return self._state.get("verification", {}).get(
            "max_rounds", DEFAULT_MAX_VERIFICATION_ROUNDS,
        )

    def get_verification_stop_reason(self) -> Optional[str]:
        """Get the reason why verification stopped."""
        return self._state.get("verification", {}).get("stop_reason")

    def _execution_gate_open(self) -> bool:
        """Is the "execution must be complete" gate open?

        Two ways through, one per branch:

        * **开发分支**：``execution`` 必须在 ``completed_phases`` 里。这条判定
          逐字不变。
        * **验证分支**：没有执行产出，所以它问的是另一个事实——基线已冻结且
          审核完备（:func:`verification_gate.verification_branch_ready`）。

        开发分支上右半边恒假（目录里有 ``tasks.json``，产物判定就是 dev 分支），
        所以原判定逐字生效；验证分支上左半边恒假，右半边才是它能走的那条路。
        两道门禁（阶段门禁与 :meth:`start_verification`）问的是同一个事实，
        所以两处调用的是这一个方法。
        """
        if "execution" in self._state.get("completed_phases", []):
            return True
        return verification_branch_ready(self.plan_dir)

    def start_verification(self):
        """Start verification phase after execution completes.

        The dev branch reaches verification after its tasks ran; the
        verification branch reaches it straight from the plan-doc review
        (see :meth:`_execution_gate_open`).
        """
        if not self._execution_gate_open():
            raise ValueError(
                "Cannot start verification: 'execution' not in completed_phases "
                "and the verification branch is not ready"
            )
        self.transition_to("verification")

    def start_verification_plan_generation(self) -> None:
        """验证分支的专用入口：PRD 审完之后进入**计划书生成**。

        验证分支的最小 PRD 允许整条 skip，所以 PRD 审完之后不一定进得了
        ``prd_approved``（那要求每一条决策点都被 accept）。这条入口给的是另一条
        路：PRD 的审核**已解决**就直接进 ``tasks_generation``（计划书在那里生成）。

        「已解决」由 :func:`verification_plan_review.prd_is_resolved` 回答，它按
        分支区分 skip 的语义：验证分支的 ``skipped`` 算数，开发分支只认
        ``accepted``——开发分支的既有门禁逐字不变。

        这条边（``prd_review → tasks_generation``）**不在**通用转移表里：通用
        入口（``POST /api/plan/{id}/phase``）不许走它，只有这个方法能走。

        Raises:
            ValueError: PRD 审核未解决；或者当前阶段没有通向
                ``tasks_generation`` 的边。
        """
        if not prd_is_resolved(self.plan_dir):
            raise ValueError(
                "Cannot start verification plan generation: the PRD review is "
                "not resolved (every decision point must be accepted, or "
                "skipped on the verification branch)"
            )
        current = self._state.get("current_phase", "")
        self._transition_to(
            "tasks_generation",
            extra_allowed=DEDICATED_PHASE_TRANSITIONS.get(current, []),
        )

    def verification_passed(self):
        """Mark verification as passed."""
        self.transition_to("verification_passed")

    def verification_failed(self):
        """Mark verification as failed (triggers repair or rerun)."""
        self.transition_to("verification_failed")

    def increment_verification_round(self):
        """Increment verification round counter.

        The counter is written to the authoritative ``plan_verification``
        row (arch decision point 1); the in-memory copy follows so the
        next increment reads the value that was just persisted. No
        :meth:`_save_state` — the phase columns are none of this
        method's business.
        """
        round_n = self.get_verification_round() + 1
        self._state.setdefault("verification", {})["round"] = round_n
        self._write_verification_state(round_n=round_n)

    def start_verification_repair(self):
        """Transition to repair tasks phase after verification failure."""
        current = self.get_current_phase()
        if current != "verification_failed":
            raise ValueError(
                f"Cannot start repair from '{current}'. "
                f"Must be in 'verification_failed' phase."
            )
        self.transition_to("verification_repairing")

    def ensure_verification_repairing(self):
        """Establish ``verification_repairing`` before dispatching repair work.

        The auto-verification loop may decide to run pending RP-* tasks
        after a round that ended ``verification_passed`` or
        ``verification_loop_stopped``.  Those exits never walked the plan
        into ``verification_repairing``: only the ordinary failure branch
        of ``check_cycle_conditions`` calls ``start_verification_repair``.
        ``verification_failed`` has a legal edge into the repair phase;
        the two terminal phases do not, and the pending repair queue is
        the authority that the chain has not converged — so the phase is
        corrected, loudly.  Any other phase is a caller bug and raises.
        """
        current = self.get_current_phase()
        if current == "verification_repairing":
            return
        if current == "verification_failed":
            self.start_verification_repair()
            return
        if current in ("verification_passed", "verification_loop_stopped"):
            logger.warning(
                "plan_repair_phase_forced plan=%s from=%s to=verification_repairing",
                self._state.get("plan_id") or self.plan_dir.name,
                current,
            )
            self.force_set_phase("verification_repairing")
            return
        raise ValueError(
            f"Cannot enter verification_repairing from '{current}'."
        )

    # 2026-10-22: ``start_verification_rerun`` is GONE. It was the only
    # writer of ``verification_rerunning``, and it had no callers
    # anywhere in the repository — a dead state with a dead writer. The
    # phase left the vocabulary with it (see :data:`LEGACY_PHASE_ALIASES`
    # for the read-path compatibility).

    def start_repair_execution(self):
        """Transition from ``verification_repairing`` into ``executing``.

        2026-09-12: the state machine is a closed loop — verification
        failure flows through
        ``verification_repairing → executing → verification →
        verification_running`` rather than the shortcut
        ``verification_repairing → verification_rerunning`` which
        bypasses the ``executing`` phase. The vocabulary now matches
        reality: the executor subprocess running RP-* tasks IS in
        ``executing``, not still inside the verification sub-machine.

        The corresponding edge is already declared in
        ``VERIFICATION_PHASE_TRANSITIONS["verification_repairing"]``
        (the only "verification_*" source that may transition to
        ``executing``), but no method previously invoked it.

        Side effects:
          * Increment ``verification.round`` so the post-repair
            verification cycle starts on round N+1, not the same N.
            This mirrors the round bookkeeping the removed
            ``start_verification_rerun`` used to do, so the round
            counter monotonically increases across all path types.
        """
        current = self.get_current_phase()
        if current != "verification_repairing":
            raise ValueError(
                f"Cannot start repair execution from '{current}'. "
                f"Must be in 'verification_repairing' phase."
            )
        # Increment round counter first so post-repair verification
        # cycle starts on the next round number (the round bookkeeping
        # the removed ``start_verification_rerun`` used to do).
        self.increment_verification_round()
        self.transition_to("executing")

    def stop_verification_loop(self, reason: str):
        """
        Stop verification loop with a reason.

        Args:
            reason: One of 'max_rounds_reached', 'user_stopped',
                'same_failure_repeated',
                'same_failure_repeated_after_max_attempts' (2026-09-12: used when the consecutive-same-failure cap fires
                after MAX attempts),
                'repair_execution_failed', 'exception'.

        The **first** reason wins (2026-11-03). A verification cycle that
        is already terminated keeps the reason it was given first; a
        later attempt only logs. Overwriting erased the real cause of
        the termination — one plan ended its round with
        ``base_drift_conflicts`` and a later stop rewrote the field to
        ``user_stopped``, so the operator read the second reason as the
        cause.

        The vocabulary check runs **before** the keep-or-write
        decision: an invalid reason is an error whether or not a reason
        is already recorded.
        """
        valid_reasons = {
            "max_rounds_reached", "user_stopped",
            "same_failure_repeated",
            "same_failure_repeated_after_max_attempts",
            "repair_execution_failed", "exception",
        }
        if reason not in valid_reasons:
            raise ValueError(f"Invalid stop reason: {reason}. Must be one of {valid_reasons}")

        existing = (self._state.get("verification") or {}).get("stop_reason")
        if existing:
            # The reason is already there — keep it, change nothing else,
            # and leave a trail for whoever tried to overwrite it.
            logger.info(
                "plan_stop_reason_kept plan=%s kept=%s attempted=%s",
                self._state.get("plan_id") or self.plan_dir.name,
                existing,
                reason,
            )
            return

        self._state["verification"]["stop_reason"] = reason
        self.transition_to("verification_loop_stopped")
        # The reason and the loop-stopped status belong to the
        # authoritative ``plan_verification`` row (arch decision point 1);
        # the ``plan_routing.verification`` mirror no longer carries
        # verification writes (arch decision point 3). The in-memory copy
        # is updated first, so a transition that refuses the move still
        # leaves the reason visible to the caller — but the row is
        # written only after the move succeeded, so the authoritative row
        # never claims a stop the plan did not make.
        self._write_verification_state(
            status="loop_stopped", stop_reason=reason,
        )

    def can_rerun_verification(self) -> bool:
        """Check if verification can be rerun (round < max_rounds)."""
        return self.get_verification_round() < self.get_verification_max_rounds()

    def is_verification_complete(self) -> bool:
        """Check if verification completed successfully."""
        return self.get_verification_status() == "passed"

    def is_verification_loop_stopped(self) -> bool:
        """Check if verification loop stopped (max rounds or user stopped)."""
        return self.get_verification_status() == "loop_stopped"

    def set_verification_max_rounds(self, max_rounds: int):
        """Set maximum verification rounds allowed.

        The budget is the plan's own; it lives in the authoritative
        ``plan_verification`` row (arch decision point 1). The
        ``plan_routing.verification`` mirror is NOT rewritten: this
        method used to end in :meth:`_save_state`, which re-stated the
        routing row from this instance's cached snapshot — the
        ``POST /api/verification/{id}/reset_rounds`` → next ``/start``
        answers ``409 stage_mismatch`` incident.

        The routing ROW, though, is created when it does not exist yet:
        the first real write is what puts the plan on the routing
        table, and the row carries the phase this instance holds (the
        upstream ``aabce578`` contract — a legacy plan's fresh row must
        not fall back to the column DEFAULT ``interview``).  The
        creation is create-only, so an existing row is not touched: no
        mirror column is rewritten, no already-recorded phase is
        restated, and ``completed_phases`` / ``flags`` /
        ``review_rounds`` are left alone.
        """
        self._state.setdefault("verification", {})["max_rounds"] = max_rounds
        self._ensure_routing_row_exists()
        self._write_verification_state(max_rounds=max_rounds)

    # --- 基线（verification.baseline）---
    #
    # 基线是**机器事实**：它只有一个来源——验证计划书「被验证对象」章节里
    # 那一行 ``baseline:``，在审核完备的那一刻被解析、写入这里。工作区与
    # 门禁只消费这一份记录，不再读文档。
    #
    # 基线是**为某一份文档**冻结的：文档被改写（``doc_sha256`` 变化）之后，
    # 旧基线不再对应当前文档。因此基线连同「它被冻结时那份文档的字节哈希」
    # 一起记录，哈希对不上即为失效。

    def get_verification_baseline(self) -> Optional[Dict[str, Any]]:
        """读回冻结的基线。

        返回 ``None`` 的两种情形：

        * 计划状态里没有记录过基线；
        * 记录的基线属于**另一份**文档（文档被改写后它已失效）——失效的基线
          不得继续用于进入验证，也不得挡住下一次审核通过时按新文档的基线行
          重写。

        失效的基线仍留在状态里（``verification.baseline``）：「被审过的基线」
        是一次批准的事实，不因为文档改写而被抹掉；只是它不再有效。

        Returns:
            基线的四个字段（``repo_dir`` / ``ref`` / ``commit_sha`` /
            ``pr_number``），没有有效基线时为 ``None``。
        """
        verification = self._state.get("verification") or {}
        baseline = verification.get("baseline")
        if not isinstance(baseline, dict):
            return None
        if not self._verification_baseline_is_current(verification):
            return None
        return dict(baseline)

    def set_verification_baseline(
        self, baseline: Dict[str, Any], doc_sha256: str = ""
    ) -> None:
        """把冻结的基线写进计划状态。

        基线只落在 ``plan_routing.verification`` 这一列的两个键上
        （``baseline`` / ``baseline_doc_sha256``）—— 那是它唯一的存放处
        （``plan_verification`` 没有这两列，而验证状态早已搬去权威表）。
        写入走 :meth:`RoutingRepository.write_verification_baseline` 这一个
        窄入口：它在同一个事务里读-改-写，只覆盖那两个键，列里其它键原样
        保留。

        它**不**调用 :meth:`_save_state`：基线不是 ``current_phase`` /
        ``completed_phases`` 的业务，用本实例缓存的快照去重写那些列，正是
        ``POST /api/verification/{id}/reset_rounds`` 之后紧跟一个 ``409
        stage_mismatch`` 的成因（见 :meth:`_save_state_to_sqlite` 的注释）。

        Args:
            baseline: 基线的四个字段（``repo_dir`` / ``ref`` / ``commit_sha`` /
                ``pr_number``）。
            doc_sha256: 这份基线被冻结时**那一份文档**的字节哈希。基线因此
                有了归属：文档被改写后哈希对不上，旧基线即失效。不给时基线
                没有归属（照写照用，不做失效判定）。
        """
        verification = self._state.setdefault("verification", {})
        verification["baseline"] = dict(baseline)
        verification["baseline_doc_sha256"] = str(doc_sha256 or "")
        self._write_verification_baseline(baseline, doc_sha256)

    def _write_verification_baseline(
        self, baseline: Dict[str, Any], doc_sha256: str
    ) -> bool:
        """把冻结的基线写进镜像列（窄写）。

        与 :meth:`_write_verification_state` 同一套失败语义：状态库打不开
        或写不进去时记 warning、返回 ``False``，不抛 —— 一次记账写不进去，
        不该让计划的相位机死掉。

        Returns:
            写成功返回 ``True``；状态库打不开或写失败返回 ``False``。
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )
        except ImportError as exc:  # pragma: no cover - 打包环境兜底
            logger.warning(
                "verification_baseline_write_skipped plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        try:
            conn = _open_db(_state_db_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verification_baseline_write_skipped plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        try:
            _migrate(conn)
            RoutingRepository(conn).write_verification_baseline(
                self.plan_dir.name, baseline, doc_sha256,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verification_baseline_write_failed plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        finally:
            conn.close()
        return True

    def _verification_baseline_is_current(
        self, verification: Dict[str, Any]
    ) -> bool:
        """已记录的基线是否仍对应当前文档。

        没有归属（没记录过 ``baseline_doc_sha256``）的基线照用：它是别的写
        路径留下的，没有任何依据说它失效——失效判定只认「文档被改写」这一
        个事实。
        """
        recorded_sha = str(verification.get("baseline_doc_sha256") or "")
        if not recorded_sha:
            return True
        return recorded_sha == self._plan_doc_sha256()

    def _plan_doc_sha256(self) -> str:
        """当前验证计划书的字节哈希；文件不在时返回空串。"""
        try:
            return doc_sha256((self.plan_dir / PLAN_DOC_FILENAME).read_bytes())
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # 验证状态的权威写入口
    # ------------------------------------------------------------------
    #
    # 验证状态只有一个权威存储：``plan_verification`` 表（arch 决策点 1）。
    # ``plan_routing.verification`` 是镜像列，不再承担写入（arch 决策点 3）。
    # 下面这些写入方法因此走 :class:`VerificationRepository`，并同步更新内存里的
    # ``_state["verification"]`` —— 写完立刻读到的必须是刚写下去的那份。
    #
    # 它们**不**再调用 :meth:`_save_state`：改一次验证状态不该顺手用本实例缓存的
    # 快照去重写 ``current_phase`` / ``completed_phases``。那正是
    # ``POST /api/verification/{id}/reset_rounds`` 之后紧跟一个 ``409
    # stage_mismatch`` 的成因（见 :meth:`_save_state_to_sqlite` 的注释）。

    def _ensure_routing_row_exists(self) -> bool:
        """Create the ``plan_routing`` row for this plan if it is missing.

        The row is created by the FIRST real write, and it must carry
        the phase this instance holds: a plan whose state says
        ``prd_review`` (read from the legacy ``plan_state.json``) must
        not get a row saying ``interview`` — the column DEFAULT — or
        the routing row contradicts the state it was created from.

        **Create-only, on purpose.**  The write goes through
        :meth:`RoutingRepository.insert_if_missing`, which is a no-op
        when the row is already there: not one column is touched.  That
        is what keeps the 2026-09-15 incident dead — a phase another
        writer CAS'd in (``reset_rounds`` → next ``/start`` answering
        ``409 stage_mismatch``) must not be dragged back by a metadata
        write.  This method deliberately does NOT go through
        :meth:`_save_state`: that one re-states the whole
        ``PlanState``-owned row from this instance's cached snapshot.

        Best-effort, like :meth:`_write_verification_state`: a state
        database that cannot be opened must not raise — a bookkeeping
        write that does not make it must not freeze the plan's phase
        machine.  The failure is logged and swallowed.

        Returns:
            ``True`` when the row exists afterwards (it was created, or
            it was already there); ``False`` when the state database
            could not be reached.
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )
        except ImportError as exc:  # pragma: no cover - 打包环境兜底
            logger.warning(
                "routing_row_create_skipped plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        try:
            conn = _open_db(_state_db_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "routing_row_create_skipped plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        try:
            _migrate(conn)
            RoutingRepository(conn).insert_if_missing(
                self.plan_dir.name,
                self._state.get("current_phase") or "interview",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "routing_row_create_failed plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        finally:
            conn.close()
        return True

    def _write_verification_state(self, **fields: Any) -> bool:
        """把验证状态字段写进权威行。

        Args:
            **fields: :meth:`VerificationRepository.set_state` 的窄写参数 ——
                ``status`` / ``round_n`` / ``max_rounds`` / ``stop_reason``。
                值为 ``None`` 表示「这一列别动」，绝不是「清空」。

        Returns:
            写成功返回 ``True``；状态库打不开返回 ``False``。失败是 best-effort：
            调用方保留内存副本、流程继续 —— 一次记账写不进去，不该让计划的相位
            机死掉。
        """
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.verification_repository import (
                VerificationRepository,
            )
        except ImportError as exc:  # pragma: no cover - 打包环境兜底
            logger.warning(
                "verification_state_write_skipped plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        try:
            conn = _open_db(_state_db_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verification_state_write_skipped plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        try:
            _migrate(conn)
            VerificationRepository(conn).set_state(
                self.plan_dir.name, **fields
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verification_state_write_failed plan=%s reason=%s",
                self.plan_dir.name, exc,
            )
            return False
        finally:
            conn.close()
        return True

    def set_verification_status(
        self,
        status: str,
        stop_reason: Optional[str] = None,
        round_n: Optional[int] = None,
    ) -> None:
        """Update verification sub-state (status / stop_reason / round).

        The authoritative home of the verification state is the
        ``plan_verification`` table (arch decision point 1); the
        ``plan_routing.verification`` mirror no longer carries writes
        (arch decision point 3). This method therefore writes through
        :class:`VerificationRepository` and keeps the in-memory copy in
        sync, so a read right after the write agrees with the row.

        It does NOT call :meth:`_save_state`: changing the verification
        status is none of ``current_phase`` / ``completed_phases``'s
        business, and re-stating them from this instance's cached
        snapshot can undo a phase another writer has moved meanwhile.

        Args:
            status: One of ``"pending"``, ``"running"``, ``"passed"``,
                ``"failed"``, ``"loop_stopped"``. Mirrors the SQLite
                ``plan_verification.verification_status`` enum.
            stop_reason: Optional reason for stopping the verification
                loop (``"max_rounds_reached"``, ``"user_stopped"``,
                ``"same_failure_repeated"``, ``"no_repair_tasks"``,
                etc.). Pass ``None`` to leave the existing value alone.
            round_n: Optional round number to stamp. Pass ``None`` to
                leave the existing value alone.

        Why this exists (2026-09-07 plan): the previous direct-SQL
        writer wrote the ``verification`` column directly via a side
        SQLite UPDATE, in parallel with ``PlanState.transition_to``
        writing ``current_phase`` to the same row. Last-writer-wins
        ordering meant the verification_status could race against phase
        transitions. 2026-11-04: the mirror column itself is gone from
        the write path — the state lives in ``plan_verification`` and
        nowhere else.
        """
        v = self._state.setdefault("verification", {})
        v["status"] = status
        if stop_reason is not None:
            v["stop_reason"] = stop_reason
        if round_n is not None:
            v["round"] = round_n
        self._write_verification_state(
            status=status, round_n=round_n, stop_reason=stop_reason,
        )

    def set_verification_round(self, round_n: int) -> None:
        """Stamp the verification round counter.

        The narrow door for "this plan is now on round N". The counter
        is written to the authoritative ``plan_verification`` row (arch
        decision point 1) and the in-memory copy follows, so the next
        increment reads the value that was just persisted.
        """
        self._state.setdefault("verification", {})["round"] = round_n
        self._write_verification_state(round_n=round_n)

    def set_verification_stop_reason(self, reason: str) -> None:
        """Record why the verification loop stopped.

        ``reason`` is the same vocabulary :meth:`stop_verification_loop`
        accepts. The reason lands in the authoritative row; the
        in-memory copy follows so a read right after the write agrees
        with it.
        """
        self._state.setdefault("verification", {})["stop_reason"] = reason
        self._write_verification_state(stop_reason=reason)

    def begin_verification(self, round_n: Optional[int] = None) -> None:
        """Route the plan into the verification phase from any source.

        Audit 2026-07-16: ``completed`` is in ``TERMINAL_PHASES``, so
        ``transition_to("verification")`` refused to fire when the
        executor started a verification cycle on a post-execution
        plan — leaving ``plan-state sidecar``'s ``verification.status``
        permanently stuck at ``"pending"``.

        ``begin_verification`` mirrors the side-effects of
        ``transition_to("verification")`` but skips the
        ``TERMINAL_PHASES`` guard, because routing a completed / failed
        / stopped plan back into verification is a legitimate
        re-entry point. If the plan is already in any verification
        state, the call is a no-op so a resumed round (e.g. a
        crashed-then-restarted thread) doesn't clobber an in-flight
        verification.

        2026-09-12 (state machine closed-loop fix): added
        optional ``round_n`` parameter so the post-repair callback
        can preserve the bumped round (the executor completed round N;
        the next verification must start at round N+1, not round 0).
        Behavior unchanged when ``round_n=None`` (default).
        """
        current = self._state["current_phase"]
        verification_states = {
            "verification", "verification_running", "verify_first_pass",
            "verify_recheck", "verification_repairing",
            "verification_passed",
            "verification_failed", "verification_loop_stopped",
        }
        if current in verification_states:
            # Idempotent: plan is already in a verification state.
            return
        # Append the source phase to completed_phases so PHASE_TRANSITIONS
        # semantics stay consistent (transition_to does this too).
        if current and current not in self._state["completed_phases"]:
            self._state["completed_phases"].append(current)
        # Re-entry from ``executing`` also marks ``execution`` as
        # completed so downstream phase-routing checks stay coherent.
        if current == "executing" and "execution" not in self._state["completed_phases"]:
            self._state["completed_phases"].append("execution")
        self._state["current_phase"] = "verification"
        # Reset round counter and re-initialize status so a new run
        # starts from a clean slate (the executor's first-round
        # increment_rev brings round to 1).
        # 2026-09-12: when round_n is provided (post-repair
        # callback), preserve the bumped value instead of clobbering
        # to 0. Without this, the executor's RP-* run for round N
        # would be followed by a verification round that thinks it is
        # round 0 — destroying the round counter's monotonic
        # invariant.
        round_value = round_n if round_n is not None else 0
        self._state["verification"]["status"] = "pending"
        self._state["verification"]["round"] = round_value
        self._state["verification"]["stop_reason"] = None
        # The verification state itself belongs to the authoritative
        # ``plan_verification`` row (arch decision point 1); the
        # ``plan_routing.verification`` mirror no longer carries
        # writes (arch decision point 3). The stop reason is cleared
        # with the explicit empty string — ``set_state`` reads
        # ``None`` as "leave this column alone", so a clear has to be
        # spelled out: a fresh cycle must not inherit the previous
        # cycle's reason.
        self._write_verification_state(
            status="pending", round_n=round_value, stop_reason="",
        )
        self._state["last_updated"] = datetime.utcnow().isoformat() + "Z"
        # The phase move itself still goes through ``_save_state`` —
        # that is the point of this method, not an incidental rewrite.
        self._save_state()


# ---------------------------------------------------------------------------
# Module-level reader helpers
# ---------------------------------------------------------------------------


def read_plan_mode(plan_dir: Path) -> str:
    """Read a plan's mode without constructing a writer for it.

    The one-line version of ``PlanState(plan_dir).get_mode()``, for
    callers that only want to *read* the mode and have no business
    touching the plan's state.

    This is a **pure read**.  Constructing a ``PlanState`` writes
    nothing (2026-11-11) -- the routing row appears on the first real
    write -- so calling this never creates a row for a plan that has
    none.

    Returns ``"production"`` or ``"test"``.  A plan with no ``mode`` in
    its flags -- one written before modes existed -- reads as
    ``production``, with a warning in the log.  See
    :meth:`PlanState.get_mode` for why the fallback is ``production``.
    """
    return PlanState(Path(plan_dir)).get_mode()


def get_migration_audit(plan_id: str) -> Optional[Dict[str, Any]]:
    """Read the top-level ``migration_audit`` field from a plan_state.json.

    Returns the field's value (a dict) when present, or ``None`` when
    the field is absent. NEVER raises ``KeyError`` — the legacy
    pre-retrofit plan_state.json files do not carry the field, and
    crashing on them would make every historical plan unreadable.

    Contract pinned by ``tests/test_plan_state_migration_audit.py``:

      * Missing field -> ``None`` (not ``KeyError``).
      * Partial sub-fields (e.g. only ``timestamp``) -> returns the
        partial dict verbatim. Sub-field schema validation is the
        writer's responsibility, not the reader's.
      * Reads from the module-level ``plans_dir`` singleton, so tests
        can ``monkeypatch.setattr(plan_state, "plans_dir", tmp_path)``
        for hermetic fixtures.

    The reader deliberately tolerates the legacy file shape. New
    writers (the migration-audit appender) emit a full four-sub-field
    dict; older writers emit no field at all. Both shapes must round-
    trip through this reader without raising.
    """
    plan_state_path = plans_dir / plan_id / PLAN_STATE_FILENAME
    with open(plan_state_path, "r", encoding="utf-8") as _f:
        data = json.load(_f)
    # ``dict.get`` returns None for the missing-key case — this is
    # the entire reason the reader cannot be replaced with
    # ``data["migration_audit"]`` (which raises ``KeyError`` on legacy
    # plans and breaks the historical-compat contract).
    return data.get("migration_audit")


# ---------------------------------------------------------------------------
# Migration-audit schema + writer (DP7 part (b))
# ---------------------------------------------------------------------------


#: JSON-schema-flavoured declaration for the ``migration_audit`` top-level
#: field. The shape is locked at four contract-pinned keys:
#:
#:   - ``migrated``     : bool          — flag set when the migration ran
#:   - ``migrated_at``  : ISO-8601 str  — UTC timestamp (from framework.clock)
#:   - ``migrator``     : str           — subsystem that recorded the audit
#:   - ``source_hash``  : sha256 hex    — fingerprint of the migrated source
#:
#: ``tasks_generator`` and ``preflight_review`` import this same object
#: (no per-module copy) so a refactor of the contract propagates to all
#: three call sites in lock-step. ``tests/unit/test_m6_cleanup.py`` and
#: ``tests/integration/test_migration_audit_schema.py`` assert the
#: ``is`` identity and the JSON-type correctness.
MIGRATION_AUDIT_SCHEMA: Dict[str, Dict[str, str]] = {
    "migrated":    {"type": "boolean"},
    "migrated_at": {"type": "string", "format": "iso8601"},
    "migrator":    {"type": "string"},
    "source_hash": {"type": "string", "format": "sha256-hex"},
}


def _read_plan_state_json(plan_state_path: Path) -> Dict[str, Any]:
    """Read + parse the plan_state.json, returning a fresh dict."""
    with open(plan_state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_plan_state_json(plan_state_path: Path, data: Dict[str, Any]) -> None:
    """Atomic write of plan_state.json (tmp + os.replace)."""
    plan_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = plan_state_path.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, plan_state_path)


def _hash_payload(source_payload: bytes) -> str:
    """Return the lowercase sha256 hex digest of ``source_payload``."""
    return hashlib.sha256(source_payload).hexdigest()


class MigrationAudit:
    """Top-level operations on ``plan_state.json["migration_audit"]``.

    Architecture decision point 7 part (b) specifies the contract:

      * ``record(...)`` writes a four-key audit record. When the
        record is already present, the call is a no-op (idempotent
        at the key level). This protects ``source_hash`` from
        drift across retry loops.
      * ``backfill(...)`` walks every ``plans/20260805-*`` plan
        directory under ``plans_dir`` and calls ``record(...)`` on
        each — also idempotent. Plans that already carry the
        field are untouched.
      * The timestamp source is ``framework.clock.utcnow_iso()``
        (architecture decision point 5) — never ``datetime.now()``.

    The class is exposed under :data:`plan_state.migration_audit`
    so callers (``tasks_generator`` / ``preflight_review``) reach
    the surface via a single import. The constant
    :data:`MIGRATION_AUDIT_SCHEMA` is shared with both callers so
    schema drift propagates to all three modules in lock-step.
    """

    def __init__(self, plans_dir_path: Path) -> None:
        self._plans_dir = Path(plans_dir_path)

    def record(
        self,
        *,
        plan_id: str,
        migrator: str,
        source_payload: bytes,
    ) -> Optional[Dict[str, Any]]:
        """Write ``migration_audit`` to ``plans/<plan_id>/plan_state.json``.

        Returns the on-disk record (a copy) when the writer mutated
        the file, or ``None`` when an existing record was preserved
        (idempotent path).

        ``source_payload`` is hashed with sha256; the digest is
        stored in ``source_hash``. The migrator must pass the
        bytes of whatever it wants the audit to attest to — for
        tasks_generator that's the bytes of ``tasks.json`` before
        emission; for preflight_review that's the bytes of the
        assembled cross-document check.

        A "no plan_state.json" or "malformed JSON" condition is
        intentionally a no-op: the audit must never corrupt a
        file it cannot read. The caller is expected to gate on
        the file's presence (e.g. "only record after tasks.json
        was successfully written").
        """
        plan_state_path = self._plans_dir / plan_id / PLAN_STATE_FILENAME
        if not plan_state_path.exists():
            return None
        try:
            data = _read_plan_state_json(plan_state_path)
        except (OSError, json.JSONDecodeError):
            return None

        existing = data.get("migration_audit")
        if isinstance(existing, dict) and existing:
            # Idempotent: do not overwrite an existing record.
            # This is the security-relevant contract from
            # test_idempotent_record_preserves_source_hash.
            return None

        record_value: Dict[str, Any] = {
            "migrated":    True,
            "migrated_at": _framework_clock.utcnow_iso(),
            "migrator":    migrator,
            "source_hash": _hash_payload(source_payload),
        }
        # Validate against the schema (cheap JSON-schema-style guard).
        for key, entry in MIGRATION_AUDIT_SCHEMA.items():
            assert key in record_value, (
                f"record_value missing schema key {key!r}"
            )
            # Type checks are deliberately minimal — full schema
            # validation lives in the test suite.
            expected = entry.get("type")
            if expected == "boolean" and not isinstance(record_value[key], bool):
                raise TypeError(f"{key!r} must be a boolean")
            if expected == "string" and not isinstance(record_value[key], str):
                raise TypeError(f"{key!r} must be a string")

        data["migration_audit"] = record_value
        try:
            _write_plan_state_json(plan_state_path, data)
        except OSError:
            return None
        return dict(record_value)

    def backfill(
        self,
        *,
        plans_dir: Optional[Path] = None,
        migrator: str = "backfill-20260805",
    ) -> Dict[str, Any]:
        """Idempotently back-fill ``migration_audit`` on every
        ``plans/20260805-*`` plan directory under ``plans_dir``.

        Returns a summary dict ``{scanned, written, skipped,
        errors}``. Each plan is processed independently: a single
        unreadable plan_state.json does NOT abort the batch.

        The default ``migrator`` string is the back-fill script's
        name; callers can override (e.g. when the integration
        test invokes ``backfill``).
        """
        root = Path(plans_dir) if plans_dir is not None else self._plans_dir
        summary = {"scanned": 0, "written": 0, "skipped": 0, "errors": 0}
        if not root.exists():
            return summary
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not child.name.startswith("20260805"):
                continue
            summary["scanned"] += 1
            plan_state_path = child / PLAN_STATE_FILENAME
            if not plan_state_path.exists():
                summary["skipped"] += 1
                continue
            try:
                data = _read_plan_state_json(plan_state_path)
            except (OSError, json.JSONDecodeError):
                summary["errors"] += 1
                continue
            if isinstance(data.get("migration_audit"), dict) and data["migration_audit"]:
                summary["skipped"] += 1
                continue
            # Hash the entire file as the source_payload — the
            # back-fill migration does not have a logical payload
            # to attest to, but the audit must still carry a
            # non-empty sha256 hex so its shape validates.
            try:
                payload = plan_state_path.read_bytes()
            except OSError:
                summary["errors"] += 1
                continue
            data["migration_audit"] = {
                "migrated":    True,
                "migrated_at": _framework_clock.utcnow_iso(),
                "migrator":    migrator,
                "source_hash": _hash_payload(payload),
            }
            try:
                _write_plan_state_json(plan_state_path, data)
            except OSError:
                summary["errors"] += 1
                continue
            summary["written"] += 1
        return summary


#: Module-level handle exposed as ``plan_state.migration_audit``.
#:
#: ``tasks_generator`` and ``preflight_review`` call
#: ``plan_state.migration_audit.record(...)`` from their write
#: paths. The bound attribute is created lazily in
#: :func:`_bind_migration_audit` so the integration tests can
#: ``monkeypatch.setattr(plan_state, "plans_dir", tmp_path)``
#: BEFORE the first call and have the audit write to the
#: hermetic sandbox.
_migration_audit: Optional[MigrationAudit] = None


def _get_migration_audit() -> MigrationAudit:
    """Return (lazily binding) the module-level ``MigrationAudit``.

    The singleton is rebuilt whenever ``plans_dir`` is reassigned
    by a test, so ``monkeypatch.setattr`` stays the supported
    way to redirect the audit to a sandbox.
    """
    global _migration_audit
    if _migration_audit is None:
        _migration_audit = MigrationAudit(plans_dir)
    return _migration_audit


def _record_migration_audit(
    *,
    plan_id: str,
    migrator: str,
    source_payload: bytes,
) -> Optional[Dict[str, Any]]:
    """Module-level shim that always reads the current ``plans_dir``.

    Defined as a plain function (not a bound method) so the call
    site ``plan_state.migration_audit.record(...)`` resolves to a
    function whose ``plans_dir`` lookup happens *at call time*,
    not at import time. This is the difference between a fresh
    test sandbox (``monkeypatch.setattr`` on ``plans_dir``) and a
    permanent module-level singleton built at import.
    """
    # Force re-resolution: if the singleton is stale (e.g. tests
    # reassigned plans_dir), discard it.
    global _migration_audit
    if _migration_audit is None or _migration_audit._plans_dir != plans_dir:
        _migration_audit = MigrationAudit(plans_dir)
    return _migration_audit.record(
        plan_id=plan_id,
        migrator=migrator,
        source_payload=source_payload,
    )


def _backfill_migration_audit(
    *,
    plans_dir_arg: Optional[Path] = None,
    plans_dir: Optional[Path] = None,
    migrator: str = "backfill-20260805",
) -> Dict[str, Any]:
    """Module-level shim for ``MigrationAudit.backfill``.

    Accepts both ``plans_dir=`` and ``plans_dir_arg=`` keyword forms
    so callers can pick whichever reads more naturally. If neither
    is provided, the module-level ``plans_dir`` singleton is used.
    """
    target = Path(plans_dir_arg) if plans_dir_arg is not None else (
        Path(plans_dir) if plans_dir is not None else plans_dir
    )
    global _migration_audit
    if _migration_audit is None or _migration_audit._plans_dir != target:
        _migration_audit = MigrationAudit(target)
    return _migration_audit.backfill(
        plans_dir=target,
        migrator=migrator,
    )


# Pre-bind the module attribute so the dot-access
# ``plan_state.migration_audit.record(...)`` works without an
# extra import dance. The shims look up ``plans_dir`` at call
# time, so monkeypatching the module attribute later still
# re-routes writes.
migration_audit = SimpleNamespace(
    record=_record_migration_audit,
    backfill=_backfill_migration_audit,
)  # type: ignore[assignment]