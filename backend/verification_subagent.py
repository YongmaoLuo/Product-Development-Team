"""Verification sub-agent skeleton and method template registry.

This module provides two cooperating classes:

  * ``VerificationSubAgent`` — a thin dataclass that pins the
    parameters of one sub-agent invocation: which ``verification_method``
    it will use, how many retries it gets on transient failure, and
    what model complexity tier it should be routed to.

  * ``MethodTemplateRegistry`` — a class-level registry that maps a
    ``verification_method`` string to a system-prompt template with a
    short "Preferred tools" soft suggestion. Templates are kept short
    (the "Preferred tools" line is ≤ 30 characters) so they fit
    comfortably into a sub-agent context window.

A sub-agent is only used for the methods that genuinely need one:
``code_review``, ``ui_validation`` and ``e2e``. ``api_test`` and
``full_ci`` have no live sub-agent path — the framework executes them
directly (``verification_api_runner`` / ``verification_ci_runner``) —
and ``automated_test`` / ``manual_check`` were retired on 2026-09-18.
See ``SUPPORTED_METHODS`` below.

``code_review``, ``ui_validation`` and ``e2e`` must additionally produce a
checkable artifact (citations / checkpoints); ``_evidence_contract``
puts that requirement in the prompt and ``verification_evidence`` verifies
it before a PASSED is honoured.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

# 2026-09-08: HardTimeoutError is raised by coding_tool when
# the inner adaptive timer SIGKILLs a silent subprocess. The
# subagent must re-raise (NOT swallow into a FAILED verdict) so
# the verification_agent layer can route to _split_vp_on_timeout.
#
# ProviderSlotWaitExpired is raised by the same module when a
# saturated provider pool was queued for longer than the slot wait.
# It is a signal, not a verdict, for the same reason: nothing was
# dispatched, nothing failed, and the two generic ``except Exception``
# arms below would each turn it into a FAILED verdict / a retry. The
# verification layer above turns it into a re-queue instead.
from coding_tool import (
    HardTimeoutError,
    JsonSession,
    ProviderSlotWaitExpired,
    WorkClock,
    query_json_in_session,
)
from utils.secret_files import private_dir, write_private_json
# 2026-11-09（VP-023）：浏览器方法的证据产物文件名与
# ``verification_evidence`` 读的是同一个常量 —— 同一个定义，不复制字符串。
from verification_evidence import CHECKPOINTS_FILENAME
# 2026-10-23（缺陷 4 要求 2）：BLOCKED 留在失败桶，但 reason 必须带
# ``BLOCKED — `` 前缀 —— 读报告的人要一眼看出「验不下去」而不是「验了没过」。
# 前缀由 :mod:`verification_buckets` 统一给出，四个 BLOCKED 产出点共用同一份。
from verification_buckets import blocked_reason
# 2026-11-08（依据契约自愈）：子 agent 自己也要核对本轮写出的依据产物 ——
# 框架侧的核对（``verification_agent._apply_evidence_check``）发生在子 agent
# 退出之后，那时已经没有人能补那份产物了，所以一次读取就能补齐的缺陷会把
# 整条验证点判死。
#
# 模块级导入是刻意的：核对要走与框架侧**同一份**判据
# （``verification_evidence``）。两边各写一套，早晚漂成两个答案。
from verification_evidence import (
    EVIDENCE_REQUIRED_METHODS,
    verify_evidence,
)
from config_paths import resolve_plans_dir
# 2026-10-27（归因绑定点 · 验证点侧）：台账认 ``vp_id`` / ``round`` 两个
# 归因坐标（``usage_registry``）。子 agent 是**验证会话的发起者** —— 它
# 知道这次会话属于哪条验证点，所以验证点号由它绑。
from usage_registry import (
    current_plan_id,
    current_round,
    current_task_id,
    plan_usage_context,
)


# ---------------------------------------------------------------------------
# The context-pack reference contract (shared with the execution brief)
# ---------------------------------------------------------------------------
#
# 2026-10-27 — A5.  The eight environment facts a round runs on live in
# ONE file per round: ``<plan_dir>/context/round-<n>.json``, written by
# :mod:`context_pack`.  Both briefs — the verification brief built below
# and the execution brief in ``agent.py`` — hand out that file's ABSOLUTE
# PATH and nothing else, so a sub-agent that would otherwise spend its
# tool calls on ``ls`` / ``find`` / ``git log`` reads one JSON file
# instead.
#
# The contract lives in this leaf module and ``agent.py`` imports it: a
# leaf must not import the orchestrator (that is how import cycles are
# born), and the two briefs have to say exactly the same thing — two
# copies of the sentence drift, and a drifted instruction is one
# sub-agent that still goes exploring.

#: The fixed instruction.  ``{pack_path}`` is the pack's absolute path.
CONTEXT_PACK_REFERENCE_INSTRUCTION = (
    "环境事实见 {pack_path}，不要再运行 ls / find / git log 重新探索环境"
)

#: The directory the packs live in, under the plan's own directory.
CONTEXT_PACK_DIRNAME = "context"

#: The pack file name for one round.
CONTEXT_PACK_FILENAME_TEMPLATE = "round-{round_no}.json"

#: The shape a file name has to have to count as a pack.
_CONTEXT_PACK_FILENAME_RE = re.compile(r"^round-(\d+)\.json$")


def absolute_path(path: Path) -> Path:
    """The path as an absolute one, WITHOUT resolving symlinks.

    ``Path.resolve()`` follows symlinks, and on macOS ``/tmp`` is a
    symlink to ``/private/tmp`` — so a resolved path and an unresolved
    one are two different strings for the same file.  The two briefs
    have to name the SAME file, so neither of them may resolve: they
    only make the path absolute.
    """
    path = Path(path).expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def context_pack_dir(plan_dir: Path) -> Path:
    """The directory this plan's packs live in."""
    return Path(plan_dir) / CONTEXT_PACK_DIRNAME


def context_pack_path(plan_dir: Path, round_no: int) -> Path:
    """Where the pack for ``round_no`` lives."""
    return context_pack_dir(plan_dir) / CONTEXT_PACK_FILENAME_TEMPLATE.format(
        round_no=round_no
    )


def existing_context_pack_rounds(plan_dir: Path) -> List[int]:
    """The rounds whose pack is already on disk, ascending.

    A missing directory is not an error: it means the round has not
    written its pack yet.
    """
    try:
        names = os.listdir(context_pack_dir(plan_dir))
    except OSError:
        return []
    rounds: List[int] = []
    for name in names:
        match = _CONTEXT_PACK_FILENAME_RE.match(name)
        if match and context_pack_path(plan_dir, int(match.group(1))).is_file():
            rounds.append(int(match.group(1)))
    return sorted(rounds)


def latest_context_pack(plan_dir: Path) -> Optional[Path]:
    """The newest pack in the plan's context directory, or ``None``.

    "Newest" is the highest ROUND NUMBER, not the newest mtime: the
    number is what the file is named after, so it is the one ordering
    two processes agree on.
    """
    rounds = existing_context_pack_rounds(plan_dir)
    if not rounds:
        return None
    return context_pack_path(plan_dir, rounds[-1])


def context_pack_reference_line(pack_path: Path) -> str:
    """The one line a brief uses to point at the context pack."""
    return CONTEXT_PACK_REFERENCE_INSTRUCTION.format(pack_path=str(pack_path))


def plan_dir_of_artifact_dir(artifact_dir: Any) -> str:
    """The plan directory that owns ``artifact_dir``.

    2026-11-02: the orchestrator sets ``evidence_artifact_dir`` to
    ``<plan_dir>/vp_artifacts/<vp_id>`` (``verification_agent``), so the
    plan directory — the verification sub-agent's one writable root — is
    two levels up. A layout without the ``vp_artifacts`` level falls back
    to the directory that holds the artifacts.

    Empty string when there is nothing to derive: the caller then names
    the plan directory *without* a path rather than naming the wrong one.
    """
    text = str(artifact_dir or "").strip()
    if not text:
        return ""
    parent = Path(text).parent
    candidate = parent.parent if parent.name == "vp_artifacts" else parent
    result = str(candidate)
    if not result or result == os.sep:
        return ""
    return result


def _marshal_safe_on_worker_thread(func: Any) -> Any:
    """Wrap ``func`` so every failure it can have can cross back to the loop.

    :func:`asyncio.to_thread` hands the callable's outcome back through
    ``asyncio.futures._chain_future``, which ends in
    ``asyncio.Future.set_exception``. That setter rejects ``StopIteration``
    outright — it cannot tell a generator that ran dry from a real error —
    and instead of setting anything it raises ``TypeError`` *inside the
    loop's own callback*. The callback dies before the future is
    resolved, so the exception is never set, the awaiting task stays
    pending forever, and :func:`_await_under_work_clock` — whose only
    bound is the work clock — sits on it for the whole
    ``HARD_WALL_CLOCK_CAP_SECONDS`` budget with the output pump thread
    still parked on ``stop_event.wait``. The observable shape of that is
    a test worker that dies on its own ``--timeout`` mid-attempt with a
    ``vp-output-pump-*`` thread at the top of the faulthandler dump.

    An exhausted ``Mock.side_effect`` list raises ``StopIteration`` in
    whatever code runs inside the worker, so "the mock ran out" was
    enough to trigger it; ``GeneratorExit`` is refused the same way and
    is handled here for the same reason.

    The normalisation belongs on this side of the thread rather than at
    each call site: what does the damage is the *awaiting* side's
    unbounded wait, not the worker, so any callable that can raise one
    of these reaches the same hang whatever it is. The result is a
    plain ``RuntimeError`` — the type ``asyncio`` itself uses for a
    ``StopIteration`` that lands on a coroutine — which the caller's
    ``except Exception`` can turn into a verdict instead of a wait that
    never ends.
    """

    @functools.wraps(func)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (StopIteration, GeneratorExit) as exc:
            name = getattr(func, "__name__", repr(func))
            raise RuntimeError(
                f"{name} ended with {type(exc).__name__}({exc!r}) on a "
                f"worker thread; it cannot be set on an asyncio.Future, "
                f"so it is normalised to RuntimeError here"
            ) from exc

    return _wrapper


def _verification_point_anchor(vp_node: Dict[str, Any]) -> str:
    """The one line that says which verification point a session is judging.

    2026-11-08（VP-028 归因 · 层二）。续跑的那一轮拿到的不是任务书，
    只有 :data:`coding_tool.RESUME_CONTINUATION_PROMPT`（"上一轮被中断，
    继续"）—— 它不点名任何验证点，而记录本身也点不了名：被续跑的会话
    可能根本不是这个验证点的会话。所以身份只能由派发方写进去。

    每次 attempt 同一形状：它是给模型看的锚，不是给人看的日志。
    """
    vp_id = str(vp_node.get("id", "unknown") or "unknown")
    title = str(vp_node.get("title", "") or "")
    return (
        f"本会话正在判定验证点 {vp_id}：{title}，"
        "最终回复必须是该验证点的裁决 JSON 并引用它点名的源码。"
    )


def _new_attempt_session(vp_node: Dict[str, Any]) -> JsonSession:
    """The session handle ONE attempt owns.

    2026-11-08（VP-028 归因 · 层一）。一个 attempt 一个句柄：id 由本次
    attempt 现生成（所以别的 attempt、别的验证点都不可能撞上它），
    ``stage`` 说明这个 id 是"要被创建"而不是"要被重开"，锚说明这个会话
    在判什么。

    不复用、不共享：同一验证点的第 2 次 attempt 也不复用第 1 次的会话
    —— 重试存在的理由就是上一份答案不能用，让它接着上一份不能用的话
    继续，等于把不能用的话又读了一遍。
    """
    return JsonSession(
        pinned_session_id=str(uuid.uuid4()),
        stage="pinned",
        resume_anchor=_verification_point_anchor(vp_node),
    )


def _session_capable(tool: Any) -> bool:
    """Whether ``tool`` owns the session machinery at all.

    归因规则针对的是**应该有却没有**的会话。一个根本没有会话机制的工具
    （测试替身、脚本化夹具）从来就没有会话可建，"没有会话产出回复"对它
    不是缺陷而是前提 —— 所以规则只适用于真正拥有这套机制的工具。
    """
    from coding_tool import ClaudeCodingTool
    return isinstance(tool, ClaudeCodingTool)


async def _await_under_work_clock(awaitable: Any, clock: WorkClock) -> Any:
    """Await ``awaitable``, bounded by the clock's *work* time only.

    :func:`asyncio.wait_for` takes one number and spends it on wall
    time, which is the wrong unit here: the thing being bounded can
    park itself for as long as a provider's quota window says, and that
    parked stretch is not work and must not be charged to the budget
    that bounds the work. So the deadline is re-read every pass rather
    than set once, and a timer alone is not enough to notice the budget
    growing again — the caller is asleep until either the work finishes
    or the budget runs out, so the clock is given a way back in to say
    "the work started, the budget is running again". Without that the
    loop would sit on a timer computed before the park and would not
    return until the work had been over for a while.

    The timeout is raised as :class:`asyncio.TimeoutError`, which is
    what the caller already handles, so "the outer cap fired" arrives
    through the same arm it always did. As before, the underlying worker
    thread is abandoned rather than killed: cancelling the wrapper does
    not reach into a thread that is already running.
    """
    task = asyncio.ensure_future(awaitable)
    resumed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal() -> None:
        # Raised on the worker thread, so it has to be handed to the
        # loop's own thread rather than set directly.
        loop.call_soon_threadsafe(resumed.set)

    clock.bind_resume(_signal)
    try:
        while True:
            remaining = clock.remaining()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            resumed.clear()
            waiter = asyncio.ensure_future(resumed.wait())
            try:
                done, _pending = await asyncio.wait(
                    {task, waiter},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                waiter.cancel()
            if task in done:
                return task.result()
    finally:
        clock.bind_resume(None)
        task.cancel()


# The verification methods a plan may use as of 2026-09-18.
#
# ``automated_test`` and ``manual_check`` were removed:
#
#   * ``automated_test`` re-ran the developer's own unit tests — exactly
#     what the 2026-09-16 decision rejects for a VP ("不要把执行阶段已经
#     写好的单元测试原样再跑一遍当验收——那既重复又不黑盒") — and it
#     spent a whole Claude Code sub-agent plus a 1-hour budget to obtain
#     an exit code the framework could have read itself.
#   * ``manual_check`` could never pass: ``parse_verdict`` coerces
#     SKIPPED to FAILED unconditionally, so every manual_check VP was a
#     guaranteed failure that also polluted repair-task generation.
#
# ``api_test`` is graded by :mod:`verification_api_runner` (no LLM);
# ``ui_validation`` and ``code_review`` still use a sub-agent, but must
# produce a checkable artifact (later stage of this rework).
#
# 2026-09-18 (Phase 2 gate completion): ``e2e`` and ``full_ci`` were
# added. They name the two **全量关卡** the 2026-09-16 two-phase decision
# requires to run last, and neither is a mechanism — they are the two
# things Phase 2 verifies:
#
#   * ``e2e``     — 全量端到端：真实链路上把整条用户流程跑一遍。由
#                    子 agent 驱动（真开浏览器、真交互），和
#                    ``ui_validation`` 同一条执行路径，只是作用域是整条
#                    链路而不是某个组件。
#   * ``full_ci`` — 全量 CI 门禁：整仓门禁 + 全部测试。判定依据就是
#                    仓库 CI 总入口那条命令的退出码，由框架执行
#                    (:mod:`verification_ci_runner`)，没有自报环节。
#
# 这两个不是 ``test_command`` / ``automated_test`` 的回归：那两个的罪名
# 是"任意命令 + 在 Phase 1 重复开发自测"。``e2e`` / ``full_ci`` 只允许
# 出现在 Phase 2（由 ``verification_phases`` 的阶段归一化强制）。
SUPPORTED_METHODS: Tuple[str, ...] = (
    "api_test",
    "code_review",
    "ui_validation",
    "e2e",
    "full_ci",
)

#: Methods the framework executes itself — no sub-agent, no LLM.
#: ``full_ci`` mirrors ``api_test``: the verdict is a pure function of a
#: declared input the framework runs, so it is reproducible across rounds.
FRAMEWORK_METHODS: Tuple[str, ...] = (
    "api_test",
    "full_ci",
)

#: Methods that may only appear in Phase 2 (全量关卡).
PHASE_2_ONLY_METHODS: Tuple[str, ...] = (
    "e2e",
    "full_ci",
)

#: Methods that existed before 2026-09-18. Named so the legacy
#: normalizer and its tests agree on the list.
LEGACY_METHODS: Tuple[str, ...] = (
    "automated_test",
    "manual_check",
)


# ---------------------------------------------------------------------------
# The per-method wall-clock budget, and the heartbeat that fires first
# ---------------------------------------------------------------------------
#
# 2026-11-05（arch 决策点 9 / 缺陷 B 第 5 条）。墙钟预算以前是**扁平的
# 一小时** —— ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` 对所有方法一视同仁。
# 其中两个方法付不起这个代价：
#
#   * ``ui_validation`` —— 子 agent 开真浏览器点页面；
#   * ``e2e``          —— 子 agent 跑完整条用户链路。
#
# 这两个的卡死形态是"安静地卡住"：没有异常、没有输出、没有产出，
# 于是一个卡死的子 agent 要把整整一小时预算跑满才有人知道。那一小时
# 里唯一在生效的判据是"总时长到了"—— 而不是"它还活着吗"。
#
# 所以预算改成**按方法**给：这两个降到 900（见
# :meth:`VerificationSubAgent.wall_clock_cap`），其余方法保持扁平一小时。
# 而且 900 必须配一个**更早**的心跳（:data:`NO_PROGRESS_HEARTBEAT_SECONDS`）：
# 心跳等于或晚于墙钟上限时它永远触发不了，"无进展就终止"就退化成
# "总时长兜底" —— 恰好是这次要去掉的那件事。
#
# 心跳的语义是"没有新的工具调用 / 产出"，不是"跑了多久"：一条一直在
# 打印的调用不会被它杀掉（``coding_tool`` 的静默窗口测的是 stdout 静默，
# 不是经过时间）。

#: 墙钟上限（秒），按方法。列在这里的方法用这个数，其余方法用
#: :attr:`VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS`。
METHOD_WALL_CLOCK_CAPS: Dict[str, int] = {
    "ui_validation": 900,
    "e2e": 900,
}

#: 无进展心跳（秒）：多少秒没有新的工具调用 / 产出就算卡死。
#: 必须**严格小于**对应方法的墙钟上限，否则心跳永远触发不了。
NO_PROGRESS_HEARTBEAT_SECONDS: int = 300

# 2026-11-06（VP-015 根因 · 层四「生命周期」）：子 agent 的工作预算用完
# 之后，它**可能还在跑**—— ``_await_under_work_clock`` 放弃的是 worker
# 线程，不是那个线程里跑着的浏览器腿。于是 2026-11-06 那一轮出现了最刺眼
# 的一幕：22:30:28 判决落定「未产出任何结果」，而 22:31:27–22:32:08 证据
# 与 ``checkpoints.json`` 全部落盘，然后没有人读过。
#
# 宽限期就是给这种"判决比产物早到"的情况留的一道门：预算用完之后不立刻判
# 死，先在这段时间里轮询产物是否落盘。落盘了就按产物重建判决（层二），
# 没落盘才终止进程组（层二之外的那一半），并且**必须**把"终止了"写进
# reasons —— 不能把"框架没读"写成"场景没产出"。
#:
#: 宽限期（秒）：预算用完之后等待迟到产物落盘的时间。
#: 120 这个数是按 2026-11-06 那轮实测的 99.8 秒落盘延迟留的余量 ——
#: 它是量出来的，不是推导出来的。
STUCK_GRACE_SECONDS: int = 120

#: 宽限期内轮询产物的间隔（秒）。
STUCK_GRACE_POLL_SECONDS: float = 2.0

#: 浏览器腿真正的产物文件名（``<plan_dir>/vp_artifacts/<vp_id>/`` 下）。
CHECKPOINTS_ARTIFACT_FILENAME = "checkpoints.json"

#: 框架自己写的失败记录（机器写出的失败集合 + 分类 + 原因码）。
FAILURE_RECORD_FILENAME = "failure.json"


# ---------------------------------------------------------------------------
# The artifacts a verification attempt leaves behind
# ---------------------------------------------------------------------------
#
# 2026-11-06（VP-015 根因 · 层二「卡死恢复」）。恢复路径以前只认
# ``/tmp/vp_<id>_progress.log`` —— 那条路径的设计前提是"卡死的 attempt
# 是一个跑 pytest 的进程"。浏览器腿（``e2e`` / ``ui_validation``）永远
# 不写那个文件：它的产出是 ``vp_artifacts/<vp_id>/checkpoints.json`` 加
# 一个 ``evidence/`` 目录。于是框架去一个浏览器腿永远不会写的地方要
# pytest 日志，然后把自己没找到东西写成了"场景没跑"。
#
# 下面这两个读法**永不抛异常**：产物损坏就是"没有产物"，不能让一轮验证
# 因为一个半截 JSON 崩掉。


def _read_json_payload(path: Path) -> Optional[Any]:
    """Read a JSON file, or ``None`` when it is missing or unreadable."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_checkpoints_artifact(
    artifact_dir: Optional[Path],
) -> Optional[List[Dict[str, Any]]]:
    """The checkpoints a browser leg wrote, or ``None``.

    ``None`` means "there is no usable artifact": the directory is
    unknown, the file is missing, it is not JSON, its ``checkpoints`` is
    not a list, or the list is empty.  An empty list is no artifact
    either — there is nothing in it to decide anything with.
    """
    if artifact_dir is None:
        return None
    payload = _read_json_payload(
        Path(artifact_dir) / CHECKPOINTS_ARTIFACT_FILENAME
    )
    if not isinstance(payload, dict):
        return None
    checkpoints = payload.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        return None
    return [item for item in checkpoints if isinstance(item, dict)]


def _checkpoint_identity(checkpoint: Dict[str, Any], index: int) -> str:
    """One checkpoint's machine-written id.

    The browser leg's checkpoint shape is
    ``{selector, expected, actual, passed}`` — no id of its own.  An id
    the producer wrote down wins; otherwise the checkpoint's position
    in the list is the id.  A position is stable (the list is written by
    a fixed script) and, unlike the ``selector``, it is not prose: the
    failing set has to be an identity, and prose is what made the
    fingerprint change every round.
    """
    for key in ("id", "name", "label"):
        value = str(checkpoint.get(key) or "").strip()
        if value:
            return value
    return f"checkpoint[{index}]"


def failed_checkpoint_ids(
    checkpoints: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """The ids of the checkpoints that did not pass.

    The machine-written failing set.  A checkpoint whose ``passed`` is
    not ``True`` did not pass — the shape check lives in
    :mod:`verification_evidence`, and a checkpoint that does not even
    carry a boolean ``passed`` has certainly not passed.
    """
    ids: List[str] = []
    for index, checkpoint in enumerate(checkpoints or []):
        if not isinstance(checkpoint, dict):
            continue
        if checkpoint.get("passed") is True:
            continue
        ids.append(_checkpoint_identity(checkpoint, index))
    return ids


# ---------------------------------------------------------------------------
# The budget an attempt spends, and the part of it reserved for recovery
# ---------------------------------------------------------------------------
#
# 2026-11-06（VP-015 根因 · 层四边界）：宽限期必须**从预算里扣**，不能
# 变成 ``cap + 120`` 的额外占用 —— 一个 attempt 的实际占用仍然只能是
# 它自己的预算。所以工作阶段提前一截收工，那一截留给宽限期。
#
# 扣多少：``STUCK_GRACE_SECONDS`` 是上限，但也只能是预算的一部分 ——
# 一个 0.1 秒的预算不能把 0.1 秒全扣掉，那样 attempt 连工作都没做就
# 直接进宽限期了。所以宽限期最多拿走预算的一半。


def _grace_reserve_seconds(cap: float) -> float:
    """How much of an attempt's budget the stuck-grace window may spend.

    ``min(STUCK_GRACE_SECONDS, cap / 2)``: the recovery window is capped
    by the constant, and it can never take more than half of the
    attempt's own budget — so the work phase always gets the other half
    to work with.
    """
    try:
        budget = max(0.0, float(cap))
    except (TypeError, ValueError):
        return 0.0
    return min(float(STUCK_GRACE_SECONDS), budget / 2.0)


class _GraceReservedClock:
    """A work clock with the stuck-grace window reserved out of it.

    The clock itself is untouched — the dispatch still parks and resumes
    *it*, and the dispatch's own view of the budget is unchanged.  This
    is the **waiting** side's view: it stops asking the clock to wait
    once only the reserved remainder is left, so the grace window has
    exactly that much budget to spend on a late artifact.

    ``_await_under_work_clock`` only ever asks three things of a clock
    (:meth:`remaining`, :meth:`bind_resume` and ``is_parked``), so this
    is the whole of the surface that has to be forwarded.
    """

    def __init__(self, inner: WorkClock, reserved: float) -> None:
        self._inner = inner
        self._reserved = max(0.0, float(reserved))

    def remaining(self) -> float:
        return max(0.0, self._inner.remaining() - self._reserved)

    def bind_resume(self, callback: Optional[Callable[[], None]]) -> None:
        self._inner.bind_resume(callback)

    @property
    def is_parked(self) -> bool:
        return self._inner.is_parked


# Shared rule applied to every verification sub-agent (added 2026-09-07
# after two consecutive rounds were killed by the verification
# watchdog: long pytest runs produced no log writes for 15+ minutes,
# so the staleness watchdog force-terminated the plan).
#
# Rule 1 — 15-minute wall-clock budget per command.  Silent >15 min
#          runs are force-terminated and waste a round.
# Rule 2 — emit observable progress from real subprocess output on
#          a 60-120 s cadence so the watchdog sees activity.
# Anti-cheat — NEVER pad the log with empty heartbeats; every
#              progress event must carry data sourced from the
#              actual subprocess (current item, completed/total
#              counts, last log line, etc.).
_TIME_BUDGET_AND_PROGRESS_BLOCK = (
    "<TIME-BUDGET-AND-PROGRESS>\n"
    "Two rules apply to every command you run, on top of any method-\n"
    "specific guidance below.\n\n"
    "Rule 1 — 15-minute wall-clock budget per command.\n"
    "Do NOT attempt a single command whose expected runtime exceeds\n"
    "~15 minutes *without* observable intermediate progress. A silent\n"
    ">15 min run will be force-terminated by the verification watchdog,\n"
    "which is expensive and wastes an entire round. If the\n"
    "test_command naturally takes that long, do ONE of:\n"
    "  - tighten scope (e.g. `pytest -k <subset>` instead of full suite,\n"
    "    `cargo test <single_name>` instead of `cargo test`)\n"
    "  - split across multiple shorter commands in different VPs\n"
    "  - wrap with an explicit shell timeout (`timeout 900 ...`)\n"
    "  - or run it under a streaming tee so progress is visible in real\n"
    "    time (see Rule 2)\n"
    "Never `fire and forget` a command whose wall-clock you cannot\n"
    "bound.\n\n"
    "Rule 2 — emit observable progress on a 60-120 s cadence.\n"
    "Long-running commands MUST produce visible progress continuously\n"
    "while they run. The orchestrator watches sub-agent activity for\n"
    "freshness — your job is to keep something emitting.\n"
    "  - pytest: always use `-v` AND pipe through `tee` to a file, e.g.\n"
    "      `pytest -v tests/ 2>&1 | tee /tmp/vp_<id>_progress.log`\n"
    "    Each test item then prints live as\n"
    "    `tests/x.py::test_y PASSED` and a small watcher loop can\n"
    "    `tail -n 1` the file every 60-120 s.\n"
    "  - pytest: also consider `--junit-xml=/tmp/vp_<id>.xml` for a\n"
    "    machine-readable side-channel.\n"
    "  - cargo build/test: add `--verbose`; crate-level progress lines\n"
    "    stream continuously.\n"
    "  - For shell pipelines: run the command in the background, then\n"
    "    in a separate `while kill -0 $PID` loop, read NEW output from\n"
    "    the tee'd file and append a `progress` entry. A working\n"
    "    pattern:\n"
    "      pytest -v tests/ 2>&1 | tee /tmp/vp_progress.log &\n"
    "      PID=$!\n"
    "      while kill -0 $PID 2>/dev/null; do\n"
    "        tail -n 1 /tmp/vp_progress.log | grep -E '::' | tail -n 1\n"
    "        sleep 90\n"
    "      done\n"
    "    Each tick produces a `progress` entry whose payload is\n"
    "    derived from the LAST live line of pytest output — real\n"
    "    subprocess state, not made up.\n\n"
    "Anti-cheat — NO empty heartbeats.\n"
    "Every `progress` event MUST carry data you actually observed from\n"
    "the subprocess (current item, completed/total counts, last log\n"
    "line, bytes processed, etc.). Writing\n"
    "  {\"event\":\"progress\"} or {\"event\":\"progress\",\"msg\":\"still running\"}\n"
    "is CHEATING — it pads the log to fool the watchdog without\n"
    "showing real work. If you have nothing real to report, REPORT THE\n"
    "FAILURE honestly (e.g. `command stalled for 10 minutes, killing\n"
    "and marking FAILED`) instead of padding the log.\n"
    "Rule 3 — the Bash tool force-terminates foreground calls around 10\n"
    "minutes. (Observed live: a full-suite pytest was SIGTERMed at the\n"
    "10-minute mark with exit 143 even though it was healthy and would\n"
    "have finished in ~18.) If the test_command's natural runtime may\n"
    "exceed ~8 minutes you MUST run it in the background and poll the\n"
    "tee'd progress file (Rule 2's `kill -0 $PID` pattern) —\n"
    "foreground is reserved for commands that provably finish quickly.\n"
    "A healthy long run killed by the Bash timeout is an INVALID\n"
    "failure, not a real one: avoid it by construction, and never\n"
    "re-run it foreground.\n"
    "</TIME-BUDGET-AND-PROGRESS>\n\n"
)


# The browser methods have no pytest run to tee, so the progress
# channel above has nothing to report for them: a walk that observes
# twenty things prints nothing at all, and the framework cannot tell a
# finished walk from a hung one.
#
# 2026-11-09 (VP-023): the stuck-sub-agent fallback read that empty
# channel and degraded a walk whose every checkpoint had passed into
# "no test output captured" — the observations were thrown away and a
# green walk scored zero. The three rules below give the browser
# methods the same observable-progress contract the other methods get,
# and they are what keeps the verdict coming back inside the budget.
_BROWSER_EVIDENCE_PROGRESS_BLOCK = (
    "<BROWSER-EVIDENCE-AND-PROGRESS>\n"
    "You drive a real browser; there is no pytest run to tee. Three\n"
    "rules give this method the same observable-progress contract the\n"
    "other methods get.\n\n"
    "Rule B1 — write checkpoints.json INCREMENTALLY.\n"
    "The moment you observe one checkpoint, add it to the file and\n"
    "rewrite the file with every checkpoint you have observed SO FAR.\n"
    "Never wait until the whole walk is over: a run that is cut short\n"
    "must still leave the observations it actually made. The path is\n"
    "the one the EVIDENCE CONTRACT below names.\n\n"
    "Rule B2 — append ONE line per checkpoint to the progress log.\n"
    "For every checkpoint you record, append one line to the progress\n"
    "file the framework reads:\n"
    "    echo \"checkpoint <n>/<total> <passed|failed>: <selector> -> "
    "<actual>\" >> /tmp/vp_<id>_progress.log\n"
    "(<id> is this verification point's id — the same path the\n"
    "TIME-BUDGET block above uses.) The generic progress channel is\n"
    "blind for a browser walk: without these lines the framework sees\n"
    "an empty log and cannot tell a finished walk from a hung one.\n\n"
    "Rule B3 — return the verdict the moment the required checkpoints\n"
    "are recorded.\n"
    "As soon as every checkpoint this verification point asks for is\n"
    "in checkpoints.json, return the verdict JSON and STOP. Do NOT\n"
    "spend the remaining budget on optional corroboration (asset\n"
    "hashes, a line-by-line server-log comparison, extra screenshots,\n"
    "extra endpoints). Those extras are worth nothing next to a\n"
    "verdict that never arrives: an unfinished walk scores zero, a\n"
    "finished one scores on its checkpoints.\n"
    "</BROWSER-EVIDENCE-AND-PROGRESS>\n\n"
)


# Each template is a system-prompt string for a verification sub-agent.
# Templates follow Superpowers-style bulletproofing: HARD-GATE block,
# method-specific Rationalization Red Flags, and a Match-the-Form-to-
# the-Failure mapping. The Output JSON contract at the end pins the
# schema the LLM must produce (see parse_verdict for the canonical
# fields).
_TEMPLATES: Dict[str, str] = {
    "code_review": (
        "You are a code review verification agent. Judge whether the code "
        "satisfies the PRD/architecture acceptance criteria for this VP.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT mark PASSED without citing the specific lines that satisfy each criterion.\n"
        "- DO NOT collapse multiple criteria into one verdict — each gets its own line of reasoning.\n"
        "- DO NOT mark a spec deviation as 'minor' — spec compliance is binary (PASSED/FAILED); quality concerns go in evidence.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'Code looks clean, must be right' | Cite the lines. Looks ≠ satisfies spec. |\n"
        "| 'User probably won't care about this edge case' | If the spec lists it, FAILED. |\n"
        "| 'Mostly works, just one small issue' | Each unaddressed criterion = FAILED. |\n\n"
        "## Match the Form to the Failure\n"
        "| Failure Type | Verdict |\n"
        "|---|---|\n"
        "| Missing acceptance criterion | FAILED, cite spec section |\n"
        "| Implementation differs from spec | FAILED, cite both spec and code |\n"
        "| Quality issue (refactor opportunity) | PASSED with quality concern in evidence |\n"
        "| Test coverage below spec threshold | FAILED if spec required a number |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    "ui_validation": (
        "You are a UI validation verification agent. Inspect puppeteer "
        "checkpoints and report whether all of them actually passed.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + _BROWSER_EVIDENCE_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT mark PASSED unless every checkpoint has passed=True.\n"
        "- DO NOT infer UI state from code review — only trust the puppeteer checkpoints.\n"
        "- DO NOT skip checkpoints that 'look unimportant' — if a checkpoint exists, it must pass.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'Most checkpoints passed' | All-or-nothing. 1 fail = FAILED. |\n"
        "| 'The failed checkpoint is just visual polish' | If it's a checkpoint, it's required. |\n"
        "| 'Screenshot looks fine to me' | Trust the puppeteer result, not visual inference. |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    # NOTE (2026-09-18): production never constructs a sub-agent for
    # ``api_test`` — the framework executes it (``verification_api_runner``).
    # The template is kept so the registry stays total over
    # ``SUPPORTED_METHODS`` and so a future caller that does want a
    # sub-agent has something to start from; it is not on any live path.
    "api_test": (
        "You are an API test verification agent. Judge whether the API "
        "responses match the contract specified by the PRD/architecture.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT mark PASSED without verifying status code AND response shape.\n"
        "- DO NOT skip edge cases (4xx, 5xx, malformed input) — if the spec lists them, test them.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'Returns 200, must be fine' | Check the body too. |\n"
        "| 'Edge case test failed but main flow works' | Each spec'd case = its own check. |\n"
        "| 'Skipping rate limit test for now' | If spec lists it, you must test it. |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    "e2e": (
        "You are an end-to-end verification agent for the plan's FINAL "
        "gate. Every earlier verification point has already passed; your "
        "job is to drive the real user-facing flow end to end and report "
        "what you actually observed.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + _BROWSER_EVIDENCE_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT reason about the code — open the real page/service and interact with it.\n"
        "- DO NOT mark PASSED on a flow you did not actually walk through in this run.\n"
        "- DO NOT substitute a unit test, a curl, or a code read for the end-to-end flow.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'The individual pieces all pass, so the flow must work' | Walk the flow. Composition breaks. |\n"
        "| 'It worked when I loaded the page' | Loading is not the flow. Drive it. |\n"
        "| 'This step is probably fine, skip it' | If the flow includes it, it runs. |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    # NOTE (2026-09-18): production never constructs a sub-agent for
    # ``full_ci`` either — the framework runs the repo's CI entry and
    # grades on its exit code (``verification_ci_runner``). Kept for the
    # same reason as the ``api_test`` template above: the registry stays
    # total over ``SUPPORTED_METHODS``.
    "full_ci": (
        "You are a full-CI gate verification agent. The plan's final gate "
        "is the repository's own CI entry point; its exit code decides.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT re-run a narrowed subset of the suite and call it a pass.\n"
        "- DO NOT mark PASSED when any part of the entry failed.\n"
        "</HARD-GATE>\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
}


class MethodTemplateRegistry:
    """Maps a ``verification_method`` string to a system-prompt template.

    The registry is class-level (templates are immutable for the process
    lifetime) and total over ``SUPPORTED_METHODS``. Retired methods
    (``automated_test`` / ``manual_check``) raise ``ValueError`` — see
    ``SUPPORTED_METHODS`` for why they are gone.
    """

    _TEMPLATES: Dict[str, str] = _TEMPLATES

    @classmethod
    def get_template(cls, method: str) -> str:
        """Return the system-prompt template for ``method``.

        Args:
            method: One of the supported verification method names.

        Returns:
            The template string. The template is a multi-line string
            with a soft tool-suggestion line and a JSON output contract.

        Raises:
            ValueError: If ``method`` is not a string or is not in
                the registry. The error message includes the full
                supported-methods list for easy debugging.
        """
        if not isinstance(method, str):
            raise ValueError(
                "verification method must be a string, "
                f"got {type(method).__name__}"
            )
        if method not in cls._TEMPLATES:
            supported = ", ".join(sorted(cls._TEMPLATES.keys()))
            raise ValueError(
                f"Unknown verification method: {method!r}. "
                f"Supported methods: {supported}"
            )
        return cls._TEMPLATES[method]

    @classmethod
    def supported_methods(cls) -> Tuple[str, ...]:
        """Return the tuple of supported method names.

        Useful for callers that want to introspect the registry
        without hardcoding the method list.
        """
        return tuple(cls._TEMPLATES.keys())


@contextmanager
def _session_attribution_scope(vp_id: str) -> Iterator[None]:
    """把这条验证点的**会话**绑定到它自己身上。

    2026-10-27（归因绑定点）：台账早就认 ``vp_id`` / ``round`` 两个归因
    坐标（任务 6），但验证会话发起时没人绑定 —— 于是验证的每一次 LLM 往返
    都落进 ``(unattributed)`` 桶。桶能说「验证很贵」，却说不出「**哪条**
    验证点贵」—— 一个不能点名的桶没法优化。

    绑定点选在**会话发起处**，不是台账写出来之后：写出来再猜，就只能猜。

    三个坐标各自的来源：

    * ``vp_id`` —— 这条验证点自己的号（这里绑定）；
    * ``round`` —— 轮次由执行器在分派处交给分派（``verification_executor``
      的 ``run``），这里**继承**它：``plan_usage_context`` 的继承规则是
      「内层不点名就保住外层的值」，所以这一层不必知道轮次是几；
    * ``plan_id`` / ``task_id`` —— 原样带过来，不因为这次绑定而丢（台账里
      既有字段的值必须与绑定之前一模一样）。

    作用域是**会话级**的：``with`` 退出时 ContextVar 复位，下一条验证点
    拿不到这一条的号。会话跑在派生 worker（``asyncio.to_thread``）里，
    worker 拿到的是这次绑定的**副本** —— 绑定进得去，也出不来：worker 里
    改的值不会流回主线程，主线程的值也不会流进下一个 worker。
    """
    with plan_usage_context(
        plan_id=current_plan_id(),
        task_id=current_task_id(),
        vp_id=vp_id,
        round=current_round(),
    ):
        yield


@dataclass
class VerificationSubAgent:
    """Skeleton for a single verification sub-agent invocation.

    Attributes:
        method: The verification method name. Must be one of the
            values in ``MethodTemplateRegistry.supported_methods()``.
        max_retries: Maximum number of retries on transient failure.
            2026-09-14: default 2 — total attempts
            including the first try is capped at 3. Beyond that the VP
            must go to repair / split, not burn more full-suite runs.
        model_complexity: Hint for which model tier to use. Free-form
            string so downstream model-routing code can interpret it
            (e.g. "simple" / "medium" / "complex"). Default "simple".
        template: System-prompt template. Set in ``__post_init__`` by
            looking up the registry — not part of ``__init__``.
    """

    method: str
    max_retries: int = 2
    model_complexity: str = "simple"
    # 2026-09-13 provider routing: the workflow scene this VP's LLM
    # calls route through (``verification`` — at least medium tier).
    # The legacy model_complexity hint never reached a routing layer;
    # the scene does (see coding_tool's scene-chain dispatch).
    scene: str = "verification"
    template: str = field(init=False, repr=False)
    # 2026-08-25: HB sub-agent watchdog integration. ``plan_id`` and
    # ``registry`` are optional for backward-compat — when ``plan_id``
    # is None the legacy behaviour is preserved (no watchdog tracking).
    plan_id: Optional[str] = None
    registry: Optional[Any] = None
    # 2026-09-13: per-VP timeout interface DELETED.
    #   * Outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` (1
    #     hour) for every VP — no per-VP field, no multiplier, no grace
    #     arithmetic. A legacy plan's ``timeout_seconds: 120`` (VP-023)
    #     used to shrink both the inner watcher AND the outer cap to
    #     ~32 min, stalling round 4 indefinitely.
    #   * Inner layer is the 15-min idle detector in ``coding_tool``
    #     (``DEFAULT_TOTAL_TIMEOUT=900``) — callers now pass
    #     ``timeout=None`` so the coding-tool default applies.
    #
    # 2026-11-05（arch 决策点 9 / 缺陷 B 第 5 条）：这个"扁平一小时"不再是
    # 所有方法的唯一预算。``ui_validation`` / ``e2e`` 收窄到 900 并配一个
    # 300 秒的无进展心跳（见 :data:`METHOD_WALL_CLOCK_CAPS` 与
    # :data:`NO_PROGRESS_HEARTBEAT_SECONDS`）。**实际生效的数是
    # :meth:`wall_clock_cap` 的返回值**，不是这个类属性 —— 这个类属性留下
    # 作为"扁平上限"，并且仍然是可以被单个实例调小的上界。
    HARD_WALL_CLOCK_CAP_SECONDS: int = 3600  # flat outer cap / ceiling
    # 2026-09-08: outer cap fires the **stuck-agent summarizer**
    # fallback (a fresh LLM call with no tools that reads the tee'd
    # log and returns verdict JSON) instead of bouncing up to the
    # generic HardTimeoutError → split path. The first agent is
    # allowed to re-run pytest if it thinks the run was flaky; the
    # summarizer only steps in when the first agent has not produced a
    # verdict at all by the time the outer cap fires. Outer cap is
    # flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` (2026-09-13 plan),
    # narrowed per method since 2026-11-05 (see ``wall_clock_cap()``).
    #
    # 2026-11-08（依据契约自愈）：本轮重试要带进 prompt 的**依据缺陷原文**
    # —— 框架核对产物时给出的每一条 complaint（"citations[49] 缺少 snippet"）。
    # 空列表 = 这一轮不是「依据不合格」触发的重试，于是 prompt 用通用的
    # 重试措辞。它是实例状态而不是参数：``_build_prompt`` 的签名被若干测试
    # 替身覆盖过（``_build_prompt(self, vp_node, attempt)``），加参数会把
    # 那些替身炸掉。
    pending_evidence_issues: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Resolve the template eagerly so construction fails fast for
        # unknown methods. This is more user-friendly than deferring
        # the error to first use.
        self.template = MethodTemplateRegistry.get_template(self.method)
        # stdlib logger for the [HARD TIMEOUT] / [STUCK SUB-AGENT]
        # markers. Without this the ``except HardTimeoutError`` handler
        # raised AttributeError on the *first* hard timeout and the
        # attempt crashed, and every retry burned a full real pytest
        # runtime before failing the same way
        # (``'VerificationSubAgent' object has no attribute 'logger'``).
        self.logger = logging.getLogger(
            f"verification.subagent.{self.method}"
        )
        # 2026-11-06（VP-015 根因 · 层一）：本次 attempt 的 VP 节点。
        # ``wall_clock_cap()`` 读得到它自己声明的 ``timeout_seconds`` ——
        # 调用点不必把节点一路传下去。
        self._active_vp_node: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # wall_clock_cap() — the budget THIS attempt may spend
    # ------------------------------------------------------------------

    def wall_clock_cap(
        self, vp_node: Optional[Dict[str, Any]] = None,
    ) -> float:
        """The wall-clock budget (seconds) one attempt of this method may spend.

        2026-11-05（arch 决策点 9 / 缺陷 B 第 5 条）：预算按方法给。
        ``ui_validation`` 与 ``e2e`` 用 :data:`METHOD_WALL_CLOCK_CAPS` 里的
        900；其余方法用扁平的一小时
        (:attr:`HARD_WALL_CLOCK_CAP_SECONDS`)。

        取的是**方法下限与扁平上限中更小的那个**：按方法的收窄只能把预算
        变紧，不能把一个已经被显式调小的实例预算（测试里把上限压到秒级的
        那些）重新放大回 900。VP 自己声明的 ``timeout_seconds`` 只能把
        预算**往上抬**（见下面 2026-11-06 那段）。

        方法名在比对前会去掉首尾空白并折叠大小写 —— 计划里的
        ``verification_method`` 未必是干净的 ``"e2e"``，而一个带空格或
        大小写不同的名字仍然是同一个方法，不该因此拿到另一份预算。

        2026-11-06（VP-015 根因 · 层一「预算层」）：方法下限是**下限**，
        不是判决。VP-015 自己声明了 ``timeout_seconds: 3600``，而它拿到
        的是 900 —— 于是它在 900.042 秒处被掐断，比自己的产物落盘早了
        整整一百秒。声明必须算数，但只允许**往上抬**：

            cap = min(扁平天花板, max(方法下限, VP 声明的预算))

        * 声明值比方法下限大 → 抬到声明值（VP-015：900 → 3600）；
        * 声明值比方法下限小 → 仍然按方法下限（一份 ``timeout_seconds:
          120`` 的老计划不能把浏览器腿的预算压到 120 秒）；
        * 没有声明 → 行为完全不变；
        * 方法没有下限（``code_review`` 等）→ 扁平天花板就是全部预算，
          声明值不能把它缩小。

        Returns:
            A positive number of seconds.
        """
        # 2026-11-05: 不做 int() 截断 —— 实例上限可以是秒级以下
        # （测试把 ``HARD_WALL_CLOCK_CAP_SECONDS`` 压到 0.1 来钉边界），
        # 截断会把它变成 0，于是每一次尝试都立刻被判成卡死。
        cap = self.HARD_WALL_CLOCK_CAP_SECONDS
        per_method = METHOD_WALL_CLOCK_CAPS.get(self._method_key())
        if per_method is None:
            # 没有方法下限：扁平天花板就是全部预算，声明值不得缩小它。
            return cap
        declared = self._declared_timeout_seconds(vp_node)
        budget = per_method if declared is None else max(per_method, declared)
        return min(cap, budget)

    def _declared_timeout_seconds(
        self, vp_node: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """The budget this VP declared, or ``None`` when it declared none.

        The node is the one the attempt is running; when no node is
        passed the attempt's own node is read (set by
        :meth:`_execute_attempt`), so every call site sees the same
        declared budget without having to thread it through.

        A value that is not a positive number is not a declaration: a
        plan with ``timeout_seconds: 0`` or ``"soon"`` declares nothing,
        and must not be read as "no budget at all".
        """
        node = vp_node
        if node is None:
            node = getattr(self, "_active_vp_node", None)
        if not isinstance(node, dict):
            return None
        try:
            declared = float(node.get("timeout_seconds"))
        except (TypeError, ValueError):
            return None
        if declared <= 0:
            return None
        return declared

    def _method_key(self) -> str:
        """The method name, normalised for lookup.

        Stripped of surrounding whitespace and case-folded — see
        :meth:`wall_clock_cap` for why the lookup cannot assume the
        constructor normalised it.
        """
        return (self.method or "").strip().lower()

    def _silence_window_seconds(self) -> Optional[int]:
        """The inner silence window (seconds) for this attempt's LLM call.

        ``None`` means "the coding tool's own default"
        (``coding_tool.DEFAULT_TOTAL_TIMEOUT`` — 15 minutes of stdout
        silence).  For the two methods whose wall clock is 900 seconds the
        window is narrowed to :data:`NO_PROGRESS_HEARTBEAT_SECONDS`
        (300 seconds), so a call that stops producing output is killed on
        the evidence of its own silence — long before the wall clock runs
        out.

        The heartbeat has to be strictly earlier than the ceiling.  At or
        above it, the ceiling would always win the race and "no progress"
        would never be distinguishable from "still running" — which is
        the same single bound this change exists to remove.

        The window measures SILENCE, not elapsed time: a call that keeps
        printing (thinking deltas, tool events, test output) is not
        killed however long it runs.
        """
        if self._method_key() in METHOD_WALL_CLOCK_CAPS:
            return NO_PROGRESS_HEARTBEAT_SECONDS
        return None

    # ------------------------------------------------------------------
    # write_verdict() — unified verdict JSON on disk (PRD Decision Point 5)
    # ------------------------------------------------------------------
    #
    # The executor (see ``verification_executor.VerificationExecutor``)
    # used to collect verdicts in an in-memory dict keyed by VP id and
    # re-judge them via the LLM at the report-generation stage.  The
    # unified-verdict contract (PRD decision point 5) reverses that:
    # the **sub-agent** writes a self-contained verdict JSON to disk
    # with status computed *objectively* from the sub-process output,
    # so the executor can simply read the file and trust the verdict
    # without re-running the LLM.
    #
    # On-disk layout::
    #
    #     plans/{plan_id}/vps/{vp_id}/verdict.json
    #
    # Schema::
    #
    #     {
    #         "vp_id":   "VP-001",
    #         "status":  "PASSED" | "FAILED",
    #         "reasons":  [str, ...],
    #         "evidence": {
    #             "logs_path": "...",
    #             ... method-specific fields ...
    #         }
    #     }
    #
    # Status computation rules (objective, no LLM re-judging):
    #
    #   * ``automated_test`` (pytest) — ``exit_code == 0`` → PASSED,
    #     else FAILED.
    #   * ``ui_validation`` (puppeteer) — all checkpoints passed →
    #     PASSED, else FAILED.
    #   * ``code_review`` — LLM-provided ``verdict``/``status`` is
    #     passed through unchanged (the LLM is the judge; the executor
    #     trusts the verdict as written).
    #   * ``api_test`` and ``manual_check`` — same pass-through as
    #     ``code_review`` (no objective exit-code signal).
    #
    # The method chosen for status computation defaults to
    # ``self.method`` (the sub-agent's configured verification method),
    # but ``exec_result["method"]`` overrides it when present (useful
    # for tests and for callers that route a single sub-agent through
    # multiple methods).
    #
    # SKIPPED is never produced here: per PRD decision point 3
    # (zero-tolerance for skipping), if the LLM emits SKIPPED for a
    # code_review VP it is coerced to FAILED with an annotation in
    # the reason list (the executor schema still allows SKIPPED at
    # the executor layer, but the sub-agent policy is strict).

    def write_verdict(
        self,
        plan_dir: Path,
        vp_id: str,
        exec_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Write a unified verdict JSON for ``vp_id`` under ``plan_dir``.

        See the class-level block above for the schema and status-
        computation rules.  This method is the *single* public entry
        point for per-VP verdict persistence; the executor must call
        it (or read the resulting file) rather than re-judging the
        verdict itself.

        Args:
            plan_dir: The plan directory (the directory containing
                ``plan_state.json``, NOT the project directory).
            vp_id: Verification point id, e.g. ``"VP-001"``.
            exec_result: Sub-process output.  Required fields vary
                by method:

                  * ``automated_test`` — ``exit_code`` (int).  Optional:
                    ``stdout``, ``stderr``, ``logs_path``.
                  * ``ui_validation`` — ``checkpoints`` (list of
                    ``{"name": str, "passed": bool}``).  Optional:
                    ``logs_path``.
                  * ``code_review`` / ``api_test`` / ``manual_check`` —
                    ``verdict`` (or ``status``) from the LLM.
                    Optional: ``reasons``, ``evidence``, ``logs_path``.

                If the dict contains a ``"method"`` key, it overrides
                ``self.method`` for status computation.

        Returns:
            The verdict payload that was written to disk::

                {
                    "vp_id":   str,
                    "status":  "PASSED" | "FAILED",
                    "reasons": [str, ...],
                    "evidence": {
                        "logs_path": "...",
                        ... method-specific fields ...
                    }
                }

            The same dict is also persisted to
            ``plan_dir/vps/{vp_id}/verdict.json`` (atomic write).
        """
        method = str(exec_result.get("method") or self.method)
        status, reasons, evidence = self._compute_verdict(method, exec_result)

        payload: Dict[str, Any] = {
            "vp_id": str(vp_id),
            "status": status,
            "reasons": list(reasons),
            "evidence": dict(evidence),
        }

        self._atomic_write_verdict(plan_dir, vp_id, payload)
        return payload

    def _compute_verdict(
        self,
        method: str,
        exec_result: Dict[str, Any],
    ) -> tuple:
        """Dispatch to the per-method objective status computer.

        Returns a 3-tuple ``(status, reasons, evidence)``.
        """
        if method in ("ui_validation", "e2e"):
            # ``e2e`` shares the puppeteer path: both are "drive the real
            # UI and report what you observed". The difference is scope
            # (whole flow vs. one component), which the VP node carries —
            # not something the verdict computation needs to know.
            return self._compute_puppeteer_verdict(exec_result)
        # code_review — LLM-judged, pass-through. (api_test never reaches
        # this class: it is graded deterministically by
        # :mod:`verification_api_runner`, which the executor dispatches to
        # before a sub-agent is ever spawned.)
        return self._compute_llm_verdict(exec_result)

    @staticmethod
    def _compute_puppeteer_verdict(
        exec_result: Dict[str, Any],
    ) -> tuple:
        """Compute verdict for puppeteer (ui_validation) runs.

        All checkpoints must be ``passed=True`` for PASSED; any failure
        or a missing/empty checkpoint list forces FAILED.  The reason
        list always states the passed/total count so the on-disk
        artifact is human-readable without re-running the test.
        """
        checkpoints = exec_result.get("checkpoints") or []
        if not isinstance(checkpoints, list):
            checkpoints = []
        total = len(checkpoints)
        passed_count = sum(
            1 for c in checkpoints if isinstance(c, dict) and bool(c.get("passed"))
        )
        all_passed = total > 0 and passed_count == total
        status = "PASSED" if all_passed else "FAILED"
        reasons: List[str] = [
            f"puppeteer checkpoints: {passed_count}/{total} passed"
        ]
        failed_count = total - passed_count
        if failed_count > 0:
            reasons.append(f"{failed_count} checkpoint(s) failed")
        elif total == 0:
            reasons.append("no checkpoints recorded")
        evidence: Dict[str, Any] = {
            "logs_path": str(exec_result.get("logs_path") or ""),
            "checkpoints": checkpoints,
            "passed_count": passed_count,
            "failed_count": failed_count,
        }
        return status, reasons, evidence

    @staticmethod
    def _compute_llm_verdict(
        exec_result: Dict[str, Any],
    ) -> tuple:
        """Compute verdict for LLM-judged methods (code_review etc.).

        The LLM is the source of truth for these methods — we do NOT
        re-judge.  Both ``status`` (the new unified-schema field name)
        and ``verdict`` (the existing template's field name) are
        accepted so callers that produce either form round-trip
        cleanly.

        SKIPPED is coerced to FAILED (PRD decision point 3, zero-
        tolerance for skipping); the coercion is auditable via the
        appended reason.
        """
        raw_status = (
            exec_result.get("status")
            or exec_result.get("verdict")
            or "FAILED"
        )
        if raw_status not in ("PASSED", "FAILED", "SKIPPED"):
            raw_status = "FAILED"
        coerced = raw_status == "SKIPPED"
        status = "FAILED" if coerced else raw_status

        reasons_raw = exec_result.get("reasons") or []
        reasons: List[str] = list(reasons_raw) if isinstance(reasons_raw, list) else []
        if coerced:
            reasons.append("verdict=SKIPPED coerced to FAILED")
        if not reasons:
            reasons.append(f"code_review LLM verdict: {raw_status}")

        raw_evidence = exec_result.get("evidence")
        if isinstance(raw_evidence, dict):
            evidence: Dict[str, Any] = dict(raw_evidence)
        else:
            evidence = {}
        logs_path = exec_result.get("logs_path")
        if logs_path and "logs_path" not in evidence:
            evidence["logs_path"] = str(logs_path)
        return status, reasons, evidence

    @staticmethod
    def _atomic_write_verdict(
        plan_dir: Path,
        vp_id: str,
        payload: Dict[str, Any],
    ) -> Path:
        """Atomic-write ``payload`` to ``vps/{vp_id}/verdict.json``.

        Creates the ``vps/`` and ``vps/{vp_id}/`` directories on
        demand (parents=True).  Writes to a sibling ``.tmp`` file
        first, fsyncs the data, then ``os.replace``-s into place —
        the POSIX guarantee on ``os.replace`` means a crash mid-write
        never leaves a partial JSON on disk, and the executor can
        safely re-read the file as soon as this method returns.
        """
        plan_dir = Path(plan_dir)
        verdict_dir = plan_dir / "vps" / str(vp_id)
        verdict_dir.mkdir(parents=True, exist_ok=True)
        verdict_path = verdict_dir / "verdict.json"
        tmp_path = verdict_path.with_suffix(verdict_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, verdict_path)
        return verdict_path

    # ------------------------------------------------------------------
    # run() / self-heal loop — PRD Decision Point 2
    # ------------------------------------------------------------------
    #
    # The run() method implements the self-heal retry loop pinned by
    # PRD Decision Point 2, AMENDED 2026-09-15:
    #
    #   1. Build a temp settings.json with 9 PreToolUse security hooks
    #      (delegated to SettingsJsonBuilder).
    #   2. Invoke the coding tool to attempt the verification. The
    #      ``max_retries + 1`` total attempts cover the initial try
    #      plus the retry budget — the budget exists for ATTEMPT
    #      MALFUNCTIONS (verdict-parse garble, watchdog kill, transport
    #      error), not for verdicts.
    #   3. On a clean FAILED verdict (the sub-agent produced a
    #      well-formed verdict with real evidence), RETURN IT — no
    #      retry, no self-heal. A verification agent is an observer,
    #      not a repairer: "113 tests failed" is a finding, and
    #      re-running pytest does not change the code under test.
    #      Retrying a clean FAILED burns the whole suite again per VP per
    #      round, and a re-run cannot change the verdict — only the code
    #      under test can. The FAILED verdict is consumed
    #      downstream by the repair/split judge — that is the fix path.
    #   4. On a BLOCKED verdict (security net intercepts an
    #      Edit/Write or Bash tool call), return immediately — the
    #      security net is intentional, not a transient failure to
    #      retry away.
    #   5. When the retry budget is exhausted by malfunctions,
    #      synthesize a FAILED verdict from the last error (never
    #      SKIPPED — PRD decision point 3 zero-tolerance).
    #   6. The ``finally`` clause always cleans up the temp
    #      settings.json so /tmp does not accumulate stale files.

    async def run(
        self,
        vp_node: Dict[str, Any],
        coding_tool: Optional[Any] = None,
        log_dir: Optional[Path] = None,
        project_dir: Optional[Path] = None,
        plan_id: str = "default",
        plan_dir: Optional[Path] = None,
        round_no: int = 0,
        excluded_providers: Optional[Iterable[str]] = None,
    ) -> "Verdict":
        """Run a verification sub-agent invocation with self-heal retry.

        Args:
            vp_node: Verification point dict (id, title,
                verification_method, test_command, expected_result).
            coding_tool: Optional coding tool (defaults to
                ClaudeCodingTool). Exposed as a parameter so tests
                can inject mocks / fake coding tools.
            log_dir: Directory where retry attempt logs are written
                (default: ``Path("logs")`` so the file lives at
                ``logs/vp_attempt_*.log`` — note the ``vp_attempt_``
                prefix avoids collision with the round-level
                ``verification_{round}_*.log`` files).
            project_dir: Project directory (passed for context; not
                used in the default no-op self-heal).
            plan_id: Plan ID, used to name the temp settings.json and
                tag the log entries.
            plan_dir: The plan's own directory — the verification
                sub-agent's single writable root. When it is given,
                the built settings.json declares the write scope (see
                :meth:`SettingsJsonBuilder._scope_env`): the sub-agent
                may write inside this directory (or a temp one) and
                nowhere else — the repository under verification is
                read-only for it. ``None`` (the default) means "no
                scope declared": the two scope guards stay inert.
            round_no: The round this run belongs to. It decides which
                ``round-<n>/`` directory the refusal evidence lands in.
                Only meaningful together with ``plan_dir``.
            excluded_providers: Providers this call must NOT use (any
                iterable of provider names). The exclusion set reaches
                the dispatch, which skips those providers when it picks
                one — that is how a retry gets a *different* provider
                after the previous one failed for infra reasons (arch
                decision point 2). ``None`` (the default) and an empty
                collection both mean "exclude nobody", and neither adds
                the keyword to the dispatch call: a stand-in tool with
                the old narrow ``query_json`` signature must not blow
                up on a keyword it never learned about.

        Returns:
            A :class:`Verdict` with ``verdict`` ∈ ``{"PASSED",
            "FAILED", "BLOCKED"}``. ``BLOCKED`` is set only by the
            security net (the LLM never emits it).
        """
        vp_id = str(vp_node.get("id", "unknown"))

        # 2026-10-27（分派表）：先问分派表。这条验证点的判据如果本来就是
        # 可机械执行的，就**不要**起 LLM 会话 —— 判定住在 runner 里，
        # 跨轮可复跑；子会话的自报不可复跑。分派表说「判不了」时，才走
        # 下面的 LLM 子会话。
        route, mode = resolve(vp_node, project_dir)
        if mode == DETERMINISTIC_MODE:
            return await self._run_deterministic_vp(
                vp_node, log_dir, project_dir,
            )

        # 2026-11-02（写入范围落地）：有 plan 目录才有一份可写范围要声明，
        # 也就才有轮次可言 —— 这时把两个值交给 builder，它据此算出
        # settings.json 里的 scope env。没有 plan_dir 时**不传**这两个
        # kwarg：没有范围要声明，也就没必要多传参数（旧调用方与窄签名的
        # 测试替身不该因为多出来的 kwarg 炸掉）。
        builder_kwargs: Dict[str, Any] = {
            "vp_id": vp_id,
            "plan_id": plan_id or "default",
        }
        if plan_dir is not None:
            builder_kwargs["plan_dir"] = plan_dir
            builder_kwargs["round_no"] = round_no
        builder = SettingsJsonBuilder(**builder_kwargs)
        settings_path: Optional[str] = None
        log_path: Optional[Path] = None
        last_blocked: Optional[BlockedPathError] = None

        try:
            # Step 1 — build temp settings.json with the 11 security hooks
            settings_path = builder.build()

            # Per-VP retry-attempt logs use the ``vp_attempt_`` prefix so
            # they do NOT collide with the round-level
            # ``verification_{round}_*.log`` files used by
            # verification_persistence.start_round. The VP-006 test
            # contract globs ``logs/*.log`` (no prefix filter) and expects
            # the first result to start with ``event=round_start``; on
            # filesystems where ``glob`` order is undefined (e.g. macOS
            # APFS, where Python's ``glob.glob`` returns inode order
            # instead of lexicographic), co-locating both prefixes in
            # ``logs/`` causes ``vp_attempt_*.log`` to land at index 0
            # and trip the assertion. Shelve attempt logs into
            # ``logs/vp_attempts/`` so the unsorted glob at the parent
            # level only sees round logs.
            log_path = self._attempt_log_path(log_dir, vp_id)

            # Step 3 — run the self-heal loop.
            #
            # 2026-10-27（归因绑定点）：这条验证点的会话在这里绑定归因
            # —— 会话属于哪条验证点，只有发起的这一层知道。作用域包住整条
            # 自愈回路：重试发起的还是这条验证点的会话，卡住后那个「读日志
            # 总结」的会话也是。
            with _session_attribution_scope(vp_id):
                return await self._self_heal_loop(
                    vp_node=vp_node,
                    coding_tool=coding_tool,
                    settings_path=settings_path,
                    log_path=log_path,
                    project_dir=project_dir,
                    excluded_providers=excluded_providers,
                    artifact_dir=self._artifact_dir(plan_dir, vp_id),
                )
        except BlockedPathError as e:
            # Security net intercepted an action; surface as a BLOCKED
            # verdict (per spec input example: {verdict: BLOCKED, ...}).
            last_blocked = e
            return Verdict(
                verdict="BLOCKED",
                reasons=[blocked_reason(f"Security net blocked: {e.reason}")],
                evidence=[e.evidence] if e.evidence else [],
                provider="",
                chosen_model="",
                model_complexity=self.model_complexity,
            )
        finally:
            # Step 4 — always clean up the temp settings.json. This
            # block runs even if an unexpected exception escapes the
            # try / except chain (and even on KeyboardInterrupt), so the
            # private temp dir never accumulates stale verif_settings_*
            # files from a crashed / retried run.
            try:
                builder.cleanup()
            except Exception:
                # Cleanup failures must never mask the run() result.
                pass
            # If we hit a BLOCKED path, ensure the log captures it
            # (forensic trail even on the early-return path).
            if last_blocked is not None and log_path is not None:
                self._write_log(log_path, "blocked", {
                    "vp_id": vp_id,
                    "reason": last_blocked.reason,
                    "evidence": last_blocked.evidence,
                })

    @staticmethod
    def _attempt_log_path(log_dir: Optional[Path], vp_id: str) -> Path:
        """One attempt's log path.

        Per-VP retry-attempt logs live in ``<log_dir>/vp_attempts/`` so
        they never collide with the round-level
        ``verification_{round}_*.log`` files (the VP-006 contract globs
        ``logs/*.log`` and expects the first entry to be the round log).
        """
        base = Path("logs") if log_dir is None else Path(log_dir)
        attempts_dir = base / "vp_attempts"
        attempts_dir.mkdir(parents=True, exist_ok=True)
        return attempts_dir / f"vp_attempt_{vp_id}_{int(time.time() * 1000)}.log"

    async def _run_deterministic_vp(
        self,
        vp_node: Dict[str, Any],
        log_dir: Optional[Path],
        project_dir: Optional[Path],
    ) -> "Verdict":
        """走确定性路径 —— 这条路上一次 LLM 会话都不开。

        分派表说这条验证点的判据是可机械执行的，那它就不该起子会话：
        判定住在 runner 里（可复跑），子会话的自报不可复跑。判不了的
        情况在分派层就被拦在外面（见 :func:`resolve`），所以走到这里的
        验证点一定判得了 —— 真跑崩了也只是「判不了」，落回 LLM 路径。
        """
        vp_id = str(vp_node.get("id", "unknown"))
        artifact_dir: Optional[Path] = None
        if log_dir is not None:
            artifact_dir = Path(log_dir).parent / "vp_artifacts"
        outcome = await asyncio.to_thread(
            _marshal_safe_on_worker_thread(run_deterministic),
            vp_node,
            project_dir=project_dir,
            artifact_dir=artifact_dir,
        )
        self._write_log(
            self._attempt_log_path(log_dir, vp_id),
            "deterministic_verdict",
            {
                "vp_id": vp_id,
                "route": outcome.route,
                "status": outcome.status,
                "exit_code": outcome.exit_code,
            },
        )
        return self._verdict_from_outcome(outcome)

    def _verdict_from_outcome(
        self, outcome: DeterministicOutcome,
    ) -> "Verdict":
        """把三态结果落成子 agent 契约里的 :class:`Verdict`。"""
        payload = outcome.verdict or {}
        status = str(payload.get("status") or "")
        evidence_lines: List[str] = []
        if outcome.exit_code is not None:
            evidence_lines.append(f"exit_code: {outcome.exit_code}")
        if outcome.stdout:
            evidence_lines.append(f"stdout: {outcome.stdout[-2000:]}")
        if outcome.stderr:
            evidence_lines.append(f"stderr: {outcome.stderr[-2000:]}")
        if outcome.evidence:
            evidence_lines.append(
                json.dumps(outcome.evidence, ensure_ascii=False, default=str)
            )
        return Verdict(
            verdict="PASSED" if status == "PASSED" else "FAILED",
            reasons=list(outcome.reasons),
            evidence=evidence_lines,
            model_complexity=self.model_complexity,
        )

    def _artifact_dir(
        self, plan_dir: Optional[Path], vp_id: str,
    ) -> Optional[Path]:
        """Where this VP's artifacts live.

        ``<plan_dir>/vp_artifacts/<vp_id>/`` — the browser leg's
        ``checkpoints.json`` and its ``evidence/`` directory live there,
        and that is the only place the stuck path can read them from.
        ``None`` when there is no plan directory: without one there is
        no artifact location to read, and the framework falls back to
        the legacy pytest tee log.
        """
        if plan_dir is None:
            return None
        try:
            return Path(plan_dir) / "vp_artifacts" / str(vp_id)
        except (TypeError, ValueError):
            return None

    async def _self_heal_loop(
        self,
        vp_node: Dict[str, Any],
        coding_tool: Optional[Any],
        settings_path: str,
        log_path: Path,
        project_dir: Optional[Path] = None,
        excluded_providers: Optional[Iterable[str]] = None,
        artifact_dir: Optional[Path] = None,
    ) -> "Verdict":
        """Run the self-heal retry loop, returning the final Verdict.

        Total attempts = ``max_retries + 1`` (one initial try + N
        retries), and the budget exists ONLY for attempt malfunctions
        (verdict-parse garble, watchdog kill, transport error). A clean
        FAILED verdict is returned immediately — no retry, no
        self-heal (2026-09-15 the verifier is an
        observer; a finding is not a malfunction to retry away). The
        loop short-circuits on BLOCKED (no retry) and, when the budget
        is exhausted by malfunctions, synthesizes a FAILED verdict
        from the last error.

        2026-11-08（依据契约自愈）：还有一种情况既不是 finding 也不是
        malfunction —— **依据产物不完整**。子 agent 交了 PASSED，但它写出
        的 citations.json 少一条原文（VP-015 第 1 轮：51 条引用里
        citations[49] 的 snippet 是空串），框架侧核对会把这条 PASSED 改判
        FAILED；可框架侧核对发生在子 agent **退出之后**，那时已经没有人
        能补那份产物了。于是「再读一次就能补齐」的缺陷把整条验证点判死，
        下一轮还得从头重跑一遍 review。

        所以 PASSED 在返回之前先核对一次本轮写出的产物；不合格就给一次
        重试，并把框架自己的 complaint 带进下一次 prompt。那一次重试是
        **独立的预算**：它不挪用 parse-error 的重试（那是给「答案不是
        Verdict」的协议故障用的），自己也只给一次。
        """
        last_error: Optional[str] = None
        # Whether ``last_error`` describes a verdict-parse failure (the
        # sub-agent answered something that is not a Verdict) as
        # opposed to a transport-layer failure (the call itself blew
        # up). The two are different malfunctions and the synthesized
        # verdict at the bottom of the loop says which one happened —
        # an operator must not be sent to the provider when the
        # sub-agent is the one that answered nonsense.
        last_error_is_parse_error = False
        # 2026-09-14: the watchdog-kill bonus slot is
        # REMOVED — total attempts is now a hard ``max_retries + 1``
        # (initial try + 2 retries = 3). The old ``+1`` reserved slot
        # let a VP burn up to 5 full-suite runs (observed live: VP-023
        # burned ~100 min of pytest per round). Watchdog kills now
        # consume the normal retry budget like any other failure; a VP
        # that still fails goes to repair / split instead of retrying.
        total_attempts = self.max_retries + 1
        # 2026-11-08（依据契约自愈）：依据缺陷自带一次重试，**不挪用**
        # parse-error 的重试预算 —— 两者是不同的东西。所以循环上界默认是
        # ``total_attempts``，只有「依据核对判它不合格」这一种情况会 +1，
        # 且只加一次。
        attempt_budget = total_attempts
        evidence_retry_used = False
        # The PASSED whose artifact did not hold up.  When the retry
        # attempt itself malfunctions, this is the verdict that comes
        # back: the framework downgrades it for its evidence exactly as
        # it did before this change, and the operator reads WHICH
        # citations were broken instead of a protocol garble.
        evidence_failed_verdict: Optional[Verdict] = None
        attempt = 0
        # A stale list from an earlier run of this instance would put the
        # wrong complaint into this run's first prompt.
        self.pending_evidence_issues = []

        while attempt < attempt_budget:
            attempt_no = attempt
            attempt += 1
            # 2026-11-08（VP-028 归因 · 层一）：本次 attempt 独占一个会话
            # 句柄。没有它，派发层自己挑会话，续跑时子 agent 只能对着别人
            # 的记录继续 —— round 24 就是这样：判 VP-028 的 attempt 答了
            # VP-038，而那份答卷看起来像一份合法的裁决。
            session = _new_attempt_session(vp_node)
            self._write_log(log_path, "attempt_started", {
                "vp_id": vp_node.get("id", "unknown"),
                "attempt": attempt_no,
                # 循环上界就是本 attempt 的预算：依据契约自愈会给它 +1，
                # 所以这里报的是**本轮实际可用的 attempt 数**。
                "max_attempts": attempt_budget,
                # 这是**申请**的会话（本次 attempt 生成的固定 id）；
                # ``attempt_completed`` 里记的是**作答**的会话 ——
                # provider 切换可能换 id，记录要能回答"哪段对话答的"。
                "session_id": session.pinned_session_id,
            })

            try:
                verdict = await self._execute_attempt(
                    vp_node=vp_node,
                    coding_tool=coding_tool,
                    settings_path=settings_path,
                    log_path=log_path,
                    attempt=attempt_no,
                    project_dir=project_dir,
                    excluded_providers=excluded_providers,
                    artifact_dir=artifact_dir,
                    session=session,
                )
            except BlockedPathError:
                # Propagate to the outer handler in run() so the
                # structured BLOCKED verdict is constructed there.
                raise
            except ProviderSlotWaitExpired:
                # Not a malfunction of this attempt — the provider pool was
                # saturated past the slot wait and the dispatch gave its
                # wait back. Spending a retry on it would report a verdict
                # the verifier never reached for, and burning the retry
                # budget on it would leave nothing for a genuine
                # malfunction. Let it reach run(), which lets it reach the
                # verification layer, which re-queues the verification
                # point instead of failing it.
                raise
            except VerdictParseError as e:
                # L2 (json-parse retry): the sub-agent's response
                # could not be parsed as the Verdict JSON contract.
                # That's usually a transient protocol error (truncated
                # stream, missing bracket, etc.) — retry up to
                # ``max_retries`` so a momentary JSON garble doesn't
                # fail the VP. We record the raw output so the
                # failure is auditable, but we do NOT feed the parse
                # error into _self_heal (that path is for code
                # defects, not for protocol garbles).
                last_error = f"VerdictParseError: {e}"
                last_error_is_parse_error = True
                self._write_log(log_path, "verdict_parse_error", {
                    "attempt": attempt_no,
                    "error": last_error,
                    # The raw payload the sub-agent produced rides along
                    # on the exception (see ``_execute_attempt``'s
                    # parse-error arm); the message itself is the
                    # fallback for an error raised elsewhere.
                    "raw_output_head": str(
                        getattr(e, "raw_output_head", "") or (str(e) or "")
                    )[:400],
                })
                if attempt_no < self.max_retries:
                    # Bounded exponential backoff: 1s, 2s, 4s, ...
                    # so we don't hammer the LLM while still
                    # giving the provider time to recover.
                    #
                    # The condition is the PARSE-ERROR budget, not the
                    # loop's bound: the evidence retry has its own
                    # attempt and must not spend this one (2026-11-08).
                    backoff = 2 ** attempt_no
                    self._write_log(log_path, "verdict_parse_retry", {
                        "attempt": attempt_no,
                        "backoff_sec": backoff,
                    })
                    await asyncio.sleep(backoff)
                continue
            except WatchdogKilledError as e:
                # 2026-09-07: the watchdog killed our subprocess.
                # Mark this run as having already consumed its single
                # watchdog-retry slot, then continue the loop — the
                # next iteration builds a fresh ``scoped_tool`` and
                # registers a fresh watchdog handle.
                #
                # 2026-09-14 bugfix: the old reset block referenced a
                # ``watchdog_handle`` local that does not exist in this
                # scope (it lives inside ``_execute_attempt``), so every
                # real kill raised NameError here instead of retrying.
                # The reset is also unnecessary: ``_execute_attempt``'s
                # ``finally`` ALWAYS unregisters the handle before the
                # exception bubbles up (verification_subagent.py:1228),
                # and ``register_sub_agent`` creates a fresh handle with
                # ``watchdog_kill_count=0`` on the next attempt. The
                # only state that must survive is the retry-used flag.
                self._watchdog_retry_used = True
                self._write_log(log_path, "sub_agent_kill_retry", {
                    "vp_id": vp_node.get("id", "unknown"),
                    "attempt": attempt_no,
                    "kill_count": e.kill_count,
                })
                continue
            except StuckAgentError as e:
                # The original sub-agent ran the test_command (perhaps
                # re-running it; that's allowed) but never returned
                # verdict JSON by the time the outer cap fired. Route
                # to a fresh-LLM-call summarizer that has no tools and
                # just returns verdict JSON based on the tee'd log.
                # This avoids the previous behaviour where
                # the entire round budget was burned by a stuck LLM.
                #
                # Note: the watchdog handle was already unregistered
                # in ``_execute_attempt``'s ``finally`` clause before
                # the exception bubbled up here — we don't need to
                # touch it again.
                self._write_log(log_path, "stuck_sub_agent_routed_to_summarizer", {
                    "vp_id": e.vp_id,
                    "attempt": attempt_no,
                    "elapsed": e.elapsed,
                    "threshold": e.threshold,
                    "output_file": e.output_file,
                    # 2026-11-10: whether the in-flight dispatch was
                    # actually ended when the outer cap fired.
                    "killed": bool(getattr(e, "killed", False)),
                })
                summarizer_verdict = await self._summarize_vp_result(
                    vp_node=vp_node,
                    output_file=e.output_file,
                    coding_tool=coding_tool,
                    log_path=log_path,
                    artifact_dir=e.artifact_dir or artifact_dir,
                    terminated=e.terminated,
                )
                # Whatever the summarizer returns is treated as the
                # final verdict — the stuck agent's retry would just
                # loop again. Don't burn more rounds on this VP.
                return summarizer_verdict
            except Exception as e:
                # Coding tool call itself raised (not a verdict-parse
                # failure, but a transport / API error). Record the
                # failure and continue to the next attempt, unless
                # we're out of budget.
                last_error = str(e)
                last_error_is_parse_error = False
                self._write_log(log_path, "attempt_exception", {
                    "attempt": attempt_no,
                    "error": last_error,
                })
                if attempt_no < self.max_retries:
                    await self._self_heal(
                        vp_node=vp_node,
                        last_verdict=None,
                        last_error=last_error,
                        log_path=log_path,
                        attempt=attempt_no,
                    )
                continue

            self._write_log(log_path, "attempt_completed", {
                "attempt": attempt_no,
                "verdict": verdict.verdict,
                "reasons": verdict.reasons,
                # 作答的会话（句柄最终记下的那个）。provider 切换可能
                # 换 id，所以它未必等于 ``attempt_started`` 里申请的那个。
                "session_id": session.session_id,
            })

            if verdict.verdict == "PASSED":
                # 2026-11-08（依据契约自愈）：PASSED 在返回之前先核对本轮
                # 写出的依据产物。框架侧的核对（``_apply_evidence_check``）
                # 发生在子 agent 退出之后，那时已经没有人能补那份产物了 ——
                # 一次读取就能补齐的缺陷会把整条验证点判死。
                #
                # **只查 PASSED**：FAILED / BLOCKED 本来就不欠引用
                # （``verification_evidence`` 的模块注释：Only PASSED is
                # inspected），不能因为缺引用去重试它们。
                issues = self._evidence_contract_issues(vp_node, project_dir)
                if issues:
                    evidence_failed_verdict = verdict
                    if evidence_retry_used:
                        # The one evidence retry is spent.  The verdict
                        # goes back as it is — the framework downgrades it
                        # downstream, exactly as it did before.  No new
                        # pass path is created here.
                        return verdict
                    evidence_retry_used = True
                    # One extra attempt, and only one: the evidence retry
                    # does not spend the parse-error budget.
                    attempt_budget = total_attempts + 1
                    self.pending_evidence_issues = issues
                    self._write_log(log_path, "evidence_contract_retry", {
                        "vp_id": vp_node.get("id", "unknown"),
                        "attempt": attempt_no,
                        "issues": issues,
                    })
                    continue
                return verdict
            if verdict.verdict == "BLOCKED":
                # Security net is intentional — no retry, no heal.
                return verdict

            # 2026-09-15: a clean FAILED verdict is a
            # FINDING, not a malfunction. The observer reported real
            # evidence; re-running the same checks cannot change the
            # code under test — every retry here burned a full suite
            # run (VP-023: 3 × 21-minute pytest to re-confirm the same
            # 113 failures). Return the verdict as-is so the round can
            # close and the repair/split judge can act on it. No
            # self-heal either: a verification agent has nothing to
            # "heal" — fixing belongs to the repair phase, and letting
            # the verifier touch code would corrupt verification
            # independence.
            return verdict

        # The budget ran out on ATTEMPT MALFUNCTIONS (a clean FAILED
        # verdict never reaches this point — it returns above). Per PRD
        # decision point 3 (zero-tolerance for skipping), we never
        # coerce this to SKIPPED; it's a hard FAILED.
        #
        # 2026-11-08（依据契约自愈）：如果手上已经有一份「依据不合格」的
        # PASSED，而它后面的重试都出了故障，那么交回这一份才是诚实的 ——
        # 框架会照旧按依据缺陷把它改判 FAILED，操作者读到的是「哪几条引用
        # 站不住」，而不是一次协议乱码。
        if evidence_failed_verdict is not None:
            return evidence_failed_verdict
        #
        # 2026-10-23: the wording names WHICH malfunction ate the
        # budget. An unparseable verdict (the sub-agent's answer did
        # not satisfy the Verdict JSON contract) is a different
        # failure from a transport error, and the operator reading the
        # report has to be able to tell them apart — "max retries
        # exhausted" on a parse failure would send them to the
        # provider when the sub-agent is the one that answered
        # nonsense.
        if last_error_is_parse_error:
            exhausted_reason = (
                f"verdict unparseable after {attempt} attempts: "
                f"{last_error or 'unknown error'}"
            )
        else:
            exhausted_reason = (
                f"max retries exhausted: {last_error or 'unknown error'}"
            )
        # 2026-10-22 (P0-1): the class is ``infra`` and the source is
        # ``deterministic``. The attempts malfunctioned (a verdict-parse
        # garble, a watchdog kill, a transport error) and nothing was
        # ever established about the deliverable, so the verdict must
        # not be filed as a broken deliverable — that is the class that
        # counts, and counting it would charge the deliverable for the
        # environment's malfunctions. The source says ``deterministic``
        # because the class was derived by rule here, not read off a
        # claim; a ``defaulted`` class would be degraded downstream to
        # ``deliverable_defect`` and land exactly where the confusion
        # starts.
        return Verdict(
            verdict="FAILED",
            reasons=[exhausted_reason],
            evidence=[],
            model_complexity=self.model_complexity,
            failure_class=INFRA_FAILURE_CLASS,
            failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
        )

    def _evidence_contract_issues(
        self,
        vp_node: Dict[str, Any],
        project_dir: Optional[Path] = None,
    ) -> List[str]:
        """What the framework's evidence check says about this attempt.

        The same check the framework runs downstream
        (``verification_agent._apply_evidence_check`` → ``apply_to_verdict``
        → ``verify_evidence``), run **here**, while the sub-agent that
        wrote the artifact is still here to complete it.

        Returns one string per issue, in the form the prompt quotes
        (``[kind] detail``), or ``[]`` when there is nothing to complain
        about — including every case where the check cannot be run at
        all (a method with no evidence contract, no artifact directory, no
        project directory to resolve the citations against).  A check
        that cannot be run is not a complaint.
        """
        method = str(
            vp_node.get("verification_method") or self.method or ""
        ).strip()
        if method not in EVIDENCE_REQUIRED_METHODS:
            return []

        # The plan directory is derived from the artifact directory the
        # orchestrator set (``<plan_dir>/vp_artifacts/<vp_id>``) — the
        # sub-agent is told where to write, never where the plan lives.
        artifact_dir = str(vp_node.get("evidence_artifact_dir") or "").strip()
        if not artifact_dir:
            return []
        plan_dir_text = plan_dir_of_artifact_dir(artifact_dir)
        if not plan_dir_text:
            return []

        root = project_dir or vp_node.get("project_root")
        if not root:
            return []

        try:
            issues = verify_evidence(
                method,
                Path(plan_dir_text),
                Path(str(root)),
                str(vp_node.get("id", "unknown")),
            )
        except Exception as exc:  # noqa: BLE001 - the check never raises
            self.logger.warning(
                "[evidence_contract] check failed for %s (%s): %s",
                vp_node.get("id", "unknown"), method, exc,
            )
            return []
        return [f"[{issue.kind}] {issue.detail}" for issue in issues]

    async def _execute_attempt(
        self,
        vp_node: Dict[str, Any],
        coding_tool: Optional[Any],
        settings_path: str,
        log_path: Path,
        attempt: int,
        project_dir: Optional[Path] = None,
        excluded_providers: Optional[Iterable[str]] = None,
        artifact_dir: Optional[Path] = None,
        session: Optional[JsonSession] = None,
    ) -> "Verdict":
        """Execute one verification attempt.

        Builds the prompt from the VP, invokes the coding tool, and
        parses the response into a :class:`Verdict`. Raises
        :class:`BlockedPathError` if the security net blocks an
        action; the caller (``_self_heal_loop``) will propagate it
        to :meth:`run` for structured verdict construction.

        ``excluded_providers`` is the set of providers this attempt must
        not use. It is handed to the dispatch, which skips them when it
        picks one — that is the "re-select the provider" half of arch
        decision point 2. The set is only passed on when it is
        non-empty: a stand-in tool with the old narrow ``query_json``
        signature would raise ``TypeError`` on a keyword it never
        learned about, and that error would be swallowed as a transport
        failure.

        ``artifact_dir`` is where this attempt's artifacts live
        (``<plan_dir>/vp_artifacts/<vp_id>/``). It is what the stuck
        path reads when the attempt is cut off: a browser leg's verdict
        is its ``checkpoints.json``, and without this the framework can
        only go looking for a pytest tee log it will never find.

        ``session`` (2026-11-08, VP-028) is the session handle this
        attempt runs in. It is passed in rather than created here so
        that the attempt log can name the session the attempt ASKED for
        before the dispatch starts; a caller that does not supply one
        gets a fresh handle of its own.
        """
        # 2026-11-06（VP-015 根因 · 层一）：本次 attempt 的 VP 节点记在
        # 实例上，``wall_clock_cap()`` 因此读得到它自己声明的
        # ``timeout_seconds`` —— 调用点不必把节点一路传下去。
        self._active_vp_node = vp_node
        # 2026-11-08（VP-028 归因 · 层一）：本次 attempt 的会话句柄。
        # 调用方（``_self_heal_loop``）传进来的是它已经记进 attempt 日志
        # 的那一个；没传就现造一个 —— 两种情况下句柄都只属于本次 attempt。
        if session is None:
            session = _new_attempt_session(vp_node)
        if coding_tool is None:
            # Lazy import so the module remains importable in
            # environments where coding_tool is unavailable (e.g.,
            # the unit test sandbox when no real LLM is wired up).
            try:
                from coding_tool import ClaudeCodingTool
                coding_tool = ClaudeCodingTool(
                    cwd=str(project_dir) if project_dir else None,
                    scene=self.scene,
                )
            except Exception as e:
                return Verdict(
                    verdict="FAILED",
                    reasons=[f"no coding tool available: {e}"],
                    evidence=[],
                    model_complexity=self.model_complexity,
                    failure_class=INFRA_FAILURE_CLASS,
                    failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
                )

        prompt = self._build_prompt(vp_node, attempt=attempt)

        # Build a scoped coding_tool clone that carries the verification
        # sub-agent settings (security hooks + inherited LLM config) so
        # Phase 2 uses the same isolation as task-execution sub-agents.
        from coding_tool import ClaudeCodingTool
        from pathlib import Path as _Path

        scoped_tool = coding_tool
        if (
            settings_path
            and isinstance(coding_tool, ClaudeCodingTool)
            and coding_tool.settings != _Path(settings_path)
        ):
            # Note: we deliberately do NOT forward the parent's
            # ``base_url`` / ``auth_token`` here. The scoped tool
            # must walk the same ``provider_priority`` fallback
            # chain as the parent does — that chain is
            # ``_check_provider_availability`` (CC Switch SQLite DB
            # with a shell-env fallback), and any change in priority
            # order (e.g. user swapping [vendor-a-pro, vendor-b] to
            # [vendor-b, vendor-a-pro]) must be reflected in sub-agents
            # too. Inheriting the parent's resolved creds would
            # bypass the chain and freeze sub-agents on whichever
            # provider the parent happened to pick.
            #
            # See bug 3 fix on coding_tool.py
            # (``_load_provider_from_db`` now reads the CC Switch DB
            # so a missing DB no longer breaks the chain).
            scoped_tool = ClaudeCodingTool(
                model=coding_tool.model,
                model_type=getattr(coding_tool, "model_type", None),
                model_map=getattr(coding_tool, "model_map", None),
                provider_priority=getattr(coding_tool, "provider_priority", None),
                mcp_config=getattr(coding_tool, "mcp_config", None),
                logger=getattr(coding_tool, "logger", None),
                settings=_Path(settings_path),
                cwd=str(project_dir) if project_dir else None,
                scene=self.scene,
            )

        # 2026-08-25: register scoped_tool with the HB watchdog so a
        # hung ``query_json`` call can be killed (otherwise the sync
        # blocking I/O inside the asyncio loop hides indefinitely and
        # HeartbeatMonitor cannot detect the stall via thread.is_alive).
        watchdog_handle = None
        if self.registry is not None and self.plan_id is not None:
            try:
                watchdog_handle = self.registry.register_sub_agent(
                    plan_id=self.plan_id,
                    vp_id=str(vp_node.get("id", "unknown")),
                    attempt=attempt,
                    scoped_tool=scoped_tool,
                    timeout_seconds=self.wall_clock_cap(),
                    max_retries=self.max_retries,
                )
            except Exception:
                # Never let watchdog registration failures break a VP.
                watchdog_handle = None

        # 2026-08-25: master try/finally guarantees watchdog handle is
        # unregistered on every exit path — return success, return FAILED,
        # or reraise (BlockedPathError → caller). Without this the
        # HeartbeatMonitor would keep a stale handle until the next
        # registry sweep and risk killing a Popen that already exited.
        try:
            try:
                # query_json enforces a JSON object response; we re-serialise
                # to a string so parse_verdict applies the canonical contract
                # (reasons list copy, evidence list copy, SKIPPED coercion).
                if watchdog_handle is not None:
                    self.registry.mark_progress(
                        self.plan_id, watchdog_handle, stage="llm_query_start"
                    )
                # 2026-09-07 hang-fix: ``query_json`` is a *synchronous*
                # blocking call (spawns the claude CLI and blocks on its
                # stdout read loop). Awaiting it directly on the event
                # loop froze the whole asyncio loop — every other VP's
                # ``asyncio.wait_for`` timeout, the executor's parallel
                # scheduling and the watchdog's kill path were all dead
                # while one VP's LLM call ran: the frozen VP's own timeout
                # never fired and the remaining VPs starved behind it.
                #
                # Fix: run the blocking call in a worker thread via
                # ``asyncio.to_thread`` so the loop stays live.
                # If the outer timeout wins first, the worker thread is
                # abandoned but the loop is unblocked and the watchdog
                # can still SIGKILL the subprocess via the registry
                # handle.
                #
                # 2026-09-13: per-VP timeout
                # interface DELETED. The timeout contract is now:
                #   * Outer cap: ``self.wall_clock_cap()`` — the per-method
                #     wall-clock budget (900s for ``ui_validation`` / ``e2e``,
                #     the flat 3600s for the rest) — fires the stuck-agent
                #     summarizer.
                #   * Inner layer: the silence detector inside
                #     ``coding_tool``.  For the two narrowed methods this
                #     attempt passes ``NO_PROGRESS_HEARTBEAT_SECONDS``
                #     (300s) — the heartbeat has to be strictly earlier
                #     than the ceiling, or it could never fire; for every
                #     other method ``timeout=None`` lets the coding-tool
                #     default (``DEFAULT_TOTAL_TIMEOUT=900``) apply.  A
                #     legacy plan's per-VP ``timeout_seconds: 120``
                #     (VP-023) can no longer shrink either layer.
                #
                # The outer cap measures *work*. A dispatch parked on a
                # provider's closed quota window is not working, and
                # charging its wait to this budget was wrong twice over:
                # the wait ran long enough to blow the cap on its own,
                # and when it did the answer was not a statement about
                # what happened but a second, tool-less call asked to
                # infer it from the logs. So the cap is a
                # :class:`coding_tool.WorkClock` the coding tool parks
                # while it waits, and the budget below asks it how much
                # work time is left rather than assuming wall time.
                work_clock = WorkClock(self.wall_clock_cap())
                # 2026-11-06（VP-015 根因 · 层四边界）：宽限期**从预算里
                # 扣**，不是额外加 —— 否则一个 attempt 的实际占用会变成
                # ``cap + 120``。所以等待侧看到的预算要先扣掉宽限期那一
                # 截：工作阶段提前收工，宽限期正好有那一截可花。
                # dispatch 拿到的仍然是原始的 clock（它要 park / resume
                # 的就是那个），扣减只发生在等待侧。
                grace_reserve = _grace_reserve_seconds(self.wall_clock_cap())
                wait_clock = _GraceReservedClock(work_clock, grace_reserve)
                try:
                    scoped_tool._work_clock = work_clock
                except Exception:
                    # A stand-in tool that does not accept the attribute
                    # simply does not get a budget, which is the
                    # behaviour it had before there was one to hand out.
                    pass
                # 2026-09-14: a healthy long-running tool call streams
                # stdout continuously — a live pytest prints progress
                # lines (its progress bar, among others) however long
                # it runs — but that liveness only reached
                # ``scoped_tool._last_output_ts`` — never the plan-dir log
                # that the verification watchdog measures by mtime. A
                # 22-minute pytest therefore looked "silent" and got
                # stamped ``verification_log_stale`` (live: the plan
                # round-3 false positive). Pump the streaming activity
                # into the attempt log so health is visible where the
                # watchdog looks — and so operators see real progress.
                _pump_stop = threading.Event()
                _pump_thread = threading.Thread(
                    target=self._pump_tool_output,
                    args=(str(vp_node.get("id", "") or ""), scoped_tool,
                          log_path, watchdog_handle, _pump_stop),
                    daemon=True,
                    name=f"vp-output-pump-{vp_node.get('id', 'vp')}",
                )
                try:
                    _pump_thread.start()
                except Exception:
                    # A thread that cannot start must never block the VP —
                    # the watchdog's second-chance checks still cover it.
                    _pump_thread = None
                # 2026-11-03 (arch decision point 2 — provider
                # re-selection): the provider that just failed must
                # not be picked again. The exclusion set only becomes a
                # keyword when it is non-empty: a stand-in tool with
                # the old narrow ``query_json`` signature would raise
                # ``TypeError`` on a keyword it never learned about,
                # and that error would be swallowed by the
                # transport-error arm below and reported as a FAILED
                # verification point.
                # 2026-11-05（arch 决策点 9 / 缺陷 B 第 5 条）：``timeout``
                # 是这次调用的**静默窗口**（``coding_tool`` 读它作
                # ``total_timeout``：多少秒没有 stdout 就杀掉子进程）。
                # 两个被收窄的方法拿到的是"无进展心跳"
                # （``NO_PROGRESS_HEARTBEAT_SECONDS``），其余方法仍然是
                # ``None`` —— 让 coding_tool 自己的默认
                # （``DEFAULT_TOTAL_TIMEOUT=900``）生效。心跳必须严格早于
                # 墙钟上限，否则它永远触发不了，"无进展就终止"就等于没有。
                query_kwargs: Dict[str, Any] = {
                    "prompt": prompt,
                    "system_instruction": self.template,
                    "timeout": self._silence_window_seconds(),
                }
                excluded: List[str] = [
                    str(p) for p in (excluded_providers or []) if p
                ]
                if excluded:
                    query_kwargs["excluded_providers"] = excluded
                try:
                    # 2026-11-08（VP-028 归因 · 层一）：派发**带着本次
                    # attempt 的会话句柄**。不带句柄时工具自选会话，续跑
                    # 的那一轮就对着别人的记录继续 —— 这正是 round 24 的
                    # 形状。``query_json_in_session`` 把句柄绑进这次调用
                    # 的上下文，工具在 dispatch 里读得到它、并把真正作答
                    # 的会话 id 写回句柄。
                    response_dict = await _await_under_work_clock(
                        asyncio.to_thread(
                            _marshal_safe_on_worker_thread(
                                functools.partial(
                                    query_json_in_session,
                                    scoped_tool,
                                    session,
                                ),
                            ),
                            **query_kwargs,
                        ),
                        wait_clock,
                    )
                finally:
                    _pump_stop.set()
            except BlockedPathError:
                # Let the security-net signal bubble up to run().
                raise
            except ProviderSlotWaitExpired:
                # The slot wait ran out before a provider slot was free. The
                # ``except Exception`` arm below would record this as a
                # transport error, self-heal on it, and — if the budget ran
                # out — synthesise a FAILED verdict for a verification point
                # that never got to be dispatched. Re-raise so it reaches
                # the re-queue above.
                raise
            except HardTimeoutError as hte:
                # 2026-09-08: the inner coding_tool SIGKILL fired
                # (subprocess silent for ``total_sec`` seconds). This is
                # a SIGNAL to auto-split, not a verdict to surface. Re-raise
                # so the verification_agent layer routes to
                # ``_split_vp_on_timeout``. Do NOT swallow into FAILED.
                vp_id_log = vp_node.get("id", "unknown")
                self.logger.warning(
                    "[HARD TIMEOUT] vp_id=%s total_sec=%s elapsed=%.1fs "
                    "last_line=%r — propagating to auto-split",
                    vp_id_log, hte.total_sec, hte.elapsed, hte.last_line[:80],
                )
                raise
            except asyncio.TimeoutError:
                # 2026-09-08: the OUTER cap fired — the flat 1-hour
                # budget elapsed without a verdict. Route to the
                # **stuck-agent summarizer** (a fresh LLM call with no
                # tools that reads /tmp/vp_<id>_progress.log and
                # returns verdict JSON) instead of bouncing up to the
                # generic HardTimeoutError → split path. Splitting a
                # test_command that's already COMPLETE in its log
                # file is wasteful; summarization reads the existing
                # result. The first agent was allowed to re-run
                # pytest if it thought the run was flaky — we only
                # step in if it didn't return a verdict at all.
                vp_id_log = vp_node.get("id", "unknown")
                # Conventional tee path the user-prompt instructs the
                # sub-agent to use. Empty string means pytest never
                # ran — summarizer degrades to FAILED with that note.
                output_file = f"/tmp/vp_{vp_id_log}_progress.log"
                # The budget is the work this call was allowed, not the
                # wall time it happened to span: a call that sat on a
                # closed quota window for a week still only ever gets an
                # hour of work, and that is the number this reports.
                #
                # 2026-11-05: the number is per method now (900 for
                # ``ui_validation`` / ``e2e``) — the log line says which
                # budget actually ran out.
                outer_cap = float(self.wall_clock_cap())
                # 2026-11-10: the outer cap fired, and the dispatch it
                # was watching is STILL RUNNING.  Logging and handing
                # the VP to the summarizer did not end it — and the
                # ``finally`` below unregisters the only handle that
                # could reach the process.  So the in-flight dispatch is
                # ended HERE, by the one action that ends it, BEFORE
                # the exception (and the unregister) travel on.
                #
                # ``getattr`` because a test double need not carry the
                # method: a stand-in without it must not turn the outer
                # cap into a crash.
                kill_fn = getattr(scoped_tool, "kill_in_flight_dispatch", None)
                killed = False
                if kill_fn is not None:
                    try:
                        killed = bool(
                            kill_fn(f"vp_outer_cap:{vp_id_log}"),
                        )
                    except Exception:
                        # The kill is best-effort here: the outer cap
                        # has already fired, and refusing to report the
                        # stuck agent would lose the summarizer route
                        # as well.
                        killed = False
                self.logger.warning(
                    "[STUCK SUB-AGENT] vp_id=%s outer_cap=%.0fs "
                    "(wall_clock_cap=%d) "
                    "— routing to summarizer (will Read %s)",
                    vp_id_log, outer_cap,
                    self.wall_clock_cap(), output_file,
                )
                # 2026-11-06（VP-015 根因 · 层四「生命周期」）：预算用完
                # 不等于产物没写出来。``_await_under_work_clock`` 放弃的是
                # worker 线程，线程里那条浏览器腿**还在跑** —— 2026-11-06
                # 那一轮它的 checkpoints 在判决之后 99.8 秒才落盘，然后
                # 没有人读过。所以先在宽限期内等它落盘。
                #
                # 宽限期能等多久，是这次 attempt 预算里**预留**出来的那
                # 一截（层四边界：宽限期从预算里扣，不是额外加）。
                grace_reserve = _grace_reserve_seconds(
                    self.wall_clock_cap()
                )
                late_checkpoints = await self._await_stuck_artifact(
                    artifact_dir, budget_seconds=grace_reserve,
                )
                if late_checkpoints:
                    # 2026-11-11 (VP-061): 宽限期里等到的迟到产物，判决
                    # 仍然必须披露终止事实 —— 框架在 :2247 已经用
                    # ``kill_in_flight_dispatch`` 结束了在飞的 dispatch，
                    # ``killed`` 就是那个已经算出的事实，复用它即可（不新增
                    # 任何 kill 调用，也不把 ``_terminate_attempt_process``
                    # 提前到宽限期之前：生命周期语义是「先等宽限期产物，
                    # 再收掉还在跑的 attempt」）。``killed`` 为假时框架并
                    # 没有结束这次尝试 —— 不得声称终止。
                    verdict = self._verdict_from_checkpoints(
                        late_checkpoints, vp_node, artifact_dir,
                        terminated=bool(killed),
                    )
                    self._write_log(log_path, "stuck_artifact_recovered", {
                        "vp_id": vp_id_log,
                        "artifact_dir": str(artifact_dir),
                        "checkpoints": len(late_checkpoints),
                        "verdict": verdict.verdict,
                        # 日志与判决必须讲同一件事：这条判决披露了终止
                        # 事实，日志也得记下框架是不是真的结束了这次尝试。
                        "terminated": bool(killed),
                        "killed": bool(killed),
                    })
                    return verdict
                # 宽限期用完还没有产物：这次 attempt 真的白跑了 —— 把它
                # 收掉，别让它继续往一个没人读的目录里写东西。
                #
                # 2026-11-10 (VP-032): the attempt's process group was
                # **already ended** above, by ``kill_in_flight_dispatch`` —
                # and ending it drops the tool's process handle.  So the
                # termination below found nothing left to kill and
                # reported "not terminated", while the attempt was in
                # fact terminated — by the framework.  The fact was
                # already computed: ``killed`` says whether a process
                # group was really killed.  The verdict of the
                # termination is that fact, not a second observation of
                # it.
                #
                # ``or`` short-circuits on purpose: once the dispatch has
                # been ended there is nothing left to terminate, and
                # asking again could reach a *different* dispatch that
                # has since started — a process this branch does not
                # own.  No new kill path is added, and no signal is sent
                # to a process the framework did not start.
                terminated = bool(
                    killed or self._terminate_attempt_process(scoped_tool)
                )
                self._write_log(log_path, "stuck_attempt_terminated", {
                    "vp_id": vp_id_log,
                    "artifact_dir": str(artifact_dir),
                    "terminated": terminated,
                    # The two facts are not the same, and an operator
                    # needs both: ``terminated`` says the attempt's
                    # process group is gone (the reason the verdict
                    # names the termination), ``killed`` says the
                    # in-flight dispatch itself was ended.  The
                    # dispatch kill is what usually ends the attempt,
                    # so a log that carries only ``terminated`` cannot
                    # say which of the two happened.
                    "killed": killed,
                })
                stuck_error = StuckAgentError(
                    elapsed=outer_cap,
                    threshold=outer_cap,
                    vp_id=vp_id_log,
                    output_file=output_file,
                    artifact_dir=artifact_dir,
                    terminated=terminated,
                )
                # Whether the in-flight dispatch was actually ended —
                # the log below says so, so an operator can tell "the
                # CLI was killed" from "there was nothing to kill".
                stuck_error.killed = killed
                raise stuck_error
            except Exception as e:
                # 2026-09-07: distinguish watchdog kills from
                # genuine coding-tool failures. Read the per-handle
                # kill counter under the registry lock so we don't
                # race with the watchdog thread mutating it from the
                # heartbeat loop.
                kc = 0
                if watchdog_handle is not None:
                    with self.registry._lock:
                        kc = getattr(
                            watchdog_handle, "watchdog_kill_count", 0,
                        )

                retry_already_used = getattr(
                    self, "_watchdog_retry_used", False,
                )
                if kc >= 2 or (kc >= 1 and retry_already_used):
                    # Second kill on the same VP (or first kill on the
                    # retry attempt after _watchdog_retry_used was
                    # set) — give up and let the executor route this
                    # into ``_skipped_vps`` via the existing SKIPPED
                    # branch in ``_record_result``
                    # (verification_executor.py:1254-1256).
                    self._write_log(log_path, "sub_agent_skipped_after_kill", {
                        "vp_id": vp_node.get("id", "unknown"),
                        "attempt": attempt,
                        "kill_count": kc,
                        "original_error": str(e)[:200],
                    })
                    return Verdict(
                        verdict="SKIPPED",
                        reasons=[
                            "watchdog_killed_twice",
                            f"coding tool call failed: {e}",
                        ],
                        evidence=[],
                        # The provider that was serving the attempt when
                        # the watchdog killed it — the retry has to know
                        # which provider to skip.
                        provider=getattr(
                            scoped_tool, "current_call_provider", "",
                        ) or "",
                        model_complexity=self.model_complexity,
                        failure_class=INFRA_FAILURE_CLASS,
                        failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
                    )
                if kc >= 1 and not retry_already_used:
                    # First kill on the initial attempt — raise so
                    # ``_self_heal_loop``'s new
                    # ``except WatchdogKilledError`` branch retries
                    # the VP with a fresh ``scoped_tool``. The retry
                    # attempt is what consumes the +1 slot from the
                    # budget extension (Step H).
                    raise WatchdogKilledError(
                        kill_count=kc, original_exc=e,
                    )
                # No watchdog kill involved — genuine coding-tool
                # failure, same as the pre-fix behavior. The verdict
                # value and the reasons are unchanged; what the
                # classification adds is the answer to "whose failure
                # is this?" — the attempt's, not the deliverable's.
                return Verdict(
                    verdict="FAILED",
                    reasons=[f"coding tool call failed: {e}"],
                    evidence=[],
                    # The provider that was serving the attempt when the
                    # call blew up. An infra verdict that does not name
                    # its provider cannot tell the ledger "this provider
                    # is broken" from "this deliverable is broken" —
                    # and the retry has nothing to exclude without it.
                    provider=getattr(
                        scoped_tool, "current_call_provider", "",
                    ) or "",
                    model_complexity=self.model_complexity,
                    failure_class=INFRA_FAILURE_CLASS,
                    failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
                )

            raw = json.dumps(response_dict) if not isinstance(
                response_dict, str
            ) else response_dict

            try:
                # 2026-09-18: no cross-verify here any more. The layer that
                # let the sub-agent's self-reported exit code override its
                # own verdict is gone — see ``parse_verdict``. The verdict
                # is validated for shape, and the *method's* own evidence
                # check (``verification_evidence`` for code_review /
                # ui_validation, ``verification_api_runner`` for api_test)
                # is what decides whether a PASSED is honoured.
                verdict = parse_verdict_to_dataclass(raw)
                # 2026-11-03 (arch decision point 2): the sub-agent
                # does not always declare which provider served it, and
                # the dispatch does know. Fill the gap — but only the
                # gap: a provider the payload DID declare is its own
                # claim and is never overwritten.
                if not verdict.provider:
                    verdict.provider = getattr(
                        scoped_tool, "current_call_provider", "",
                    ) or ""
                # 2026-11-08（VP-028 归因 · 层三）：裁决要能归因才算数。
                # 一个会话都没产出过回复的答案，无法证明它出自本次
                # attempt、这个验证点 —— 它只是一段来路不明的文字。
                # 所以它不能记成 PASSED：这类失败属于 attempt，不属于
                # 被验的交付物（failure_class=infra）。
                if not session.session_id and _session_capable(scoped_tool):
                    self._write_log(log_path, "attempt_unattributed", {
                        "vp_id": vp_node.get("id", "unknown"),
                        "attempt": attempt,
                        "pinned_session_id": session.pinned_session_id,
                        "verdict": verdict.verdict,
                    })
                    return Verdict(
                        verdict="FAILED",
                        reasons=[
                            "no session produced a reply for this attempt; "
                            "the verdict cannot be attributed to this "
                            "verification point",
                        ],
                        evidence=[],
                        provider=verdict.provider,
                        model_complexity=self.model_complexity,
                        failure_class=INFRA_FAILURE_CLASS,
                        failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
                    )
                return verdict
            # NEGCTL-P0-1-BEGIN
            except VerdictParseError as e:
                # 2026-10-22 (P0-1): a payload that cannot be parsed is
                # a MALFUNCTION of this attempt, not a statement about
                # the deliverable under verification. Answering with a
                # FAILED verdict here used to swallow the failure: the
                # caller saw a well-formed verdict, so the L2 retry
                # branch in ``_self_heal_loop`` — the one that exists
                # exactly for a transient protocol garble (truncated
                # stream, stray prose around the JSON object) — was
                # unreachable, and one garbled response failed the
                # verification point for good while claiming the
                # deliverable was broken.
                #
                # 2026-10-23: an unparseable verdict is a MALFUNCTION
                # of the protocol, not a finding about the code under
                # test. It used to be turned into a clean FAILED
                # verdict right here, and that made
                # ``_self_heal_loop``'s ``except VerdictParseError``
                # branch dead code: the loop returns a clean FAILED
                # immediately (2026-09-15 — the verifier is an
                # observer, a finding is not a malfunction to retry
                # away), so a sub-agent that answered ``{}`` was
                # reported as a verification failure and the retry
                # budget never spent a single attempt on it
                # (measured: attempt log held ``attempt_started`` +
                # ``attempt_completed FAILED`` and nothing else).
                #
                # Re-raise so the caller can retry it (bounded
                # exponential backoff) and, once the budget is gone,
                # synthesize a FAILED verdict that says the verdict
                # was UNPARSEABLE rather than dressing it up as an
                # exhausted transport budget.
                #
                # The raw payload rides along on the exception so the
                # attempt log keeps the forensic trail the inline
                # verdict used to carry in ``evidence``.
                e.raw_output_head = raw[:400]
                raise
            # NEGCTL-P0-1-END
        finally:
            if watchdog_handle is not None and self.registry is not None:
                try:
                    self.registry.unregister(self.plan_id, watchdog_handle)
                except Exception:
                    # Unregister failure must never bleed out of VP code.
                    pass

    # ------------------------------------------------------------------
    # The artifacts a cut-off attempt leaves behind
    # ------------------------------------------------------------------

    async def _await_stuck_artifact(
        self,
        artifact_dir: Optional[Path],
        budget_seconds: Optional[float] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Wait out the grace window for a late artifact to land.

        The attempt's budget ran out; the work it was doing did not
        stop.  This waits for the artifact that attempt is still
        writing, and returns it if it lands.

        ``budget_seconds`` is how long the grace window may wait — the
        share of the attempt's own budget that was reserved for it
        (层四边界：宽限期从预算里扣，不是额外加).  It defaults to
        :data:`STUCK_GRACE_SECONDS`.

        ``None`` means "no artifact landed" — the caller then has to
        terminate the attempt and say so.  It never raises: a corrupt
        artifact is "no artifact", not a reason to abort the round.
        """
        if artifact_dir is None:
            return None
        if budget_seconds is None:
            budget_seconds = float(STUCK_GRACE_SECONDS)
        deadline = time.monotonic() + max(0.0, float(budget_seconds))
        while True:
            checkpoints = read_checkpoints_artifact(artifact_dir)
            if checkpoints:
                return checkpoints
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(STUCK_GRACE_POLL_SECONDS, remaining))

    def _list_evidence_dir(
        self, artifact_dir: Optional[Path],
    ) -> List[str]:
        """The attempt's evidence files — the second recovery input.

        A browser leg writes its evidence into
        ``<plan_dir>/vp_artifacts/<vp_id>/evidence/``.  The listing
        itself is not a verdict (only ``checkpoints.json`` is), but it
        is the record of what the attempt *did* produce, and it is
        attached to whatever verdict is produced.

        Never raises: a missing directory is an empty listing.
        """
        if artifact_dir is None:
            return []
        try:
            directory = Path(artifact_dir) / "evidence"
            if not directory.is_dir():
                return []
            names = sorted(
                entry.name for entry in directory.iterdir() if entry.is_file()
            )
        except OSError:
            return []
        return [f"evidence/{name}" for name in names]

    def _verdict_from_checkpoints(
        self,
        checkpoints: List[Dict[str, Any]],
        vp_node: Dict[str, Any],
        artifact_dir: Optional[Path],
        terminated: bool = False,
    ) -> "Verdict":
        """Rebuild a verdict from the checkpoints the attempt wrote.

        The checkpoints are the attempt's own machine-written record of
        what it observed, so they answer the question the framework was
        about to ask a model to guess at.

        * every checkpoint passed → **PASSED**, naming the artifact;
        * some checkpoint did not pass → **FAILED**, naming the failing
          checkpoints, and the machine-written failing set is written to
          ``failure.json`` so the report, the artifact and the failure
          fingerprint all name the same set.

        ``terminated`` says the framework itself ended this attempt
        (2026-11-11, VP-061).  When it is true the verdict **discloses**
        that fact, the way every other branch that returns a verdict
        does: the dispatcher hands every VP it runs an
        ``evidence_artifact_dir``, so for a browser leg this branch — not
        the one that discloses — is the branch production takes, and a
        verdict returned from here without the disclosure hid the
        framework's own decision behind a statement about the
        deliverable.  The disclosure is **added** to the verdict: the
        reasons above, the failing set and the classification are
        unchanged.
        """
        vp_id = str(vp_node.get("id", "unknown"))
        failed = failed_checkpoint_ids(checkpoints)
        artifact = (
            str(Path(artifact_dir) / CHECKPOINTS_ARTIFACT_FILENAME)
            if artifact_dir is not None else CHECKPOINTS_ARTIFACT_FILENAME
        )
        evidence = [
            f"{CHECKPOINTS_ARTIFACT_FILENAME}: {len(checkpoints)} "
            f"checkpoint(s) at {artifact}",
        ]
        if not failed:
            reasons = [
                f"the attempt wrote {len(checkpoints)} checkpoint(s) and "
                f"all of them passed ({artifact})",
            ]
            if terminated:
                reasons.append(self._termination_disclosure(
                    "the checkpoints it did write are the whole of the "
                    "walk's evidence"
                ))
            return Verdict(
                verdict="PASSED",
                reasons=reasons,
                evidence=evidence,
                model_complexity=self.model_complexity,
            )

        evidence.append("failed checkpoints: " + ", ".join(failed))
        self._write_failure_record(
            artifact_dir,
            vp_id=vp_id,
            reason_code="checkpoints_failed",
            failure_class=DEFAULT_FAILURE_CLASS,
            failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
            failed_cases=failed,
            verdict="FAILED",
        )
        reasons = [
            f"the attempt wrote {len(checkpoints)} checkpoint(s) and "
            f"{len(failed)} of them did not pass ({artifact}): "
            + ", ".join(failed),
        ]
        # 2026-11-09（合并 VP-023）：机器写的失败集合是**位置 id**（稳定，
        # 指纹靠它），但只给 id 的理由读不出"哪一条没对上" —— 浏览器腿的
        # 失败还要点名选择器、期望与实际。
        reasons.extend(self._failed_checkpoint_details(checkpoints))
        if terminated:
            # 2026-11-11 (VP-061): 终止事实必须在**每一条**会返回判决的
            # 分支上被披露 —— 这一条是产物优先的那条，浏览器腿在生产上
            # 走的正是它（派发层给每个 VP 都发了 ``evidence_artifact_dir``）。
            reasons.append(self._termination_disclosure(
                "the checkpoints it did write are the only ones this "
                "verdict can speak for"
            ))
        return Verdict(
            verdict="FAILED",
            reasons=reasons,
            evidence=evidence,
            model_complexity=self.model_complexity,
            failure_class=DEFAULT_FAILURE_CLASS,
            failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
        )

    @staticmethod
    def _failed_checkpoint_details(
        checkpoints: List[Dict[str, Any]],
    ) -> List[str]:
        """One line per failed checkpoint, naming what was not met.

        The failing **set** is carried by ids — a position in the
        checkpoint list, so the fingerprint does not move from round to
        round.  The prose a reader needs is the other half: which
        selector was checked, what was expected there and what was
        actually there.  Only the checkpoint itself holds that.
        """
        lines: List[str] = []
        for checkpoint in checkpoints or []:
            if not isinstance(checkpoint, dict):
                continue
            if checkpoint.get("passed") is True:
                continue
            selector = str(checkpoint.get("selector") or "")
            expected = str(checkpoint.get("expected") or "")
            actual = str(checkpoint.get("actual") or "")
            lines.append(
                f"checkpoint failed: selector={selector!r} "
                f"expected={expected!r} actual={actual!r}"
            )
        return lines

    def _write_failure_record(
        self,
        artifact_dir: Optional[Path],
        *,
        vp_id: str,
        reason_code: str,
        failure_class: str,
        failure_class_source: str,
        failed_cases: Optional[List[str]] = None,
        verdict: str = "FAILED",
    ) -> None:
        """Write the machine-written record of a failure next to the artifact.

        ``failure.json`` is what the fingerprint layer reads when the
        report entry carries no failing set of its own: it is the
        machine-written identity of the failing set, so the same defect
        fingerprints the same however the run was worded.
        """
        if artifact_dir is None:
            return
        payload = {
            "vp_id": vp_id,
            "verdict": verdict,
            "reason_code": reason_code,
            "failure_class": failure_class,
            "failure_class_source": failure_class_source,
            "failed_cases": list(failed_cases or []),
            "written_at": datetime.now().isoformat(),
        }
        try:
            directory = Path(artifact_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / FAILURE_RECORD_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # The record is evidence, not a gate: a plan directory that
            # cannot be written must not fail the round.
            pass

    def _terminate_attempt_process(self, scoped_tool: Any) -> bool:
        """Kill the attempt that was cut off. True when it was killed.

        The attempt's worker thread was **abandoned**, not cancelled, so
        the subprocess it drove is still running.  Left alone it keeps
        writing into an artifact directory nobody will read — and on the
        next round it is still there.

        The repo's existing termination path is reused: the tool's own
        ``Popen``, killed through :func:`utils.process.kill_process_group`
        (the same helper the watchdog uses), so the whole process group
        goes and not just the wrapper.
        """
        proc = None
        try:
            lock = getattr(scoped_tool, "_process_lock", None)
            if lock is not None:
                with lock:
                    proc = getattr(scoped_tool, "_current_process", None)
            else:
                proc = getattr(scoped_tool, "_current_process", None)
        except Exception:
            proc = None
        if proc is None:
            return False
        try:
            import signal

            from utils.process import kill_process_group

            kill_process_group(
                proc, sig=signal.SIGTERM, wait_timeout=1.0,
            )
            kill_process_group(
                proc, sig=signal.SIGKILL, wait_timeout=1.0,
            )
        except Exception:
            # A kill that cannot be delivered is not a reason to abort:
            # the attempt is abandoned either way, and the reason says
            # so.
            return False
        return True

    #: The methods whose evidence is a browser walk.  Their evidence
    #: artifact is ``checkpoints.json`` — there is no pytest log behind
    #: them, which is exactly why the stuck fallback used to throw
    #: their observations away (2026-11-09, VP-023).
    _BROWSER_EVIDENCE_METHODS = ("ui_validation", "e2e")

    def _termination_disclosure(self, consequence: str) -> str:
        """The sentence that discloses that the FRAMEWORK ended the attempt.

        2026-11-10 (VP-032): when the outer cap fires and the grace
        window expires without an artifact, the framework terminates the
        attempt's process group **itself**.  Every verdict built in that
        state has to say so: "the walk cannot be graded" reads like a
        statement about the deliverable, and it is false — the framework
        is the one that stopped the walk.  A verdict that omits the
        disclosure blames the deliverable for a decision the framework
        made.

        The disclosure goes **last** among the reasons: this codebase's
        convention is "satisfied criteria first, the failing one last"
        (see :meth:`VerificationAgent._verdict_to_result`), and the
        consumers that read the tail of the reason text must read the
        cause, not the symptom.
        """
        return (
            "stuck sub-agent: the attempt was terminated after its budget "
            f"ran out; the framework ended it, so {consequence}"
        )

    def _verdict_from_checkpoint_artifact(
        self,
        vp_node: Dict[str, Any],
        terminated: bool = False,
    ) -> Optional["Verdict"]:
        """Grade a browser verification point from the artifact it left.

        2026-11-09 (VP-023): a browser walk (``ui_validation`` / ``e2e``)
        leaves its evidence in ``<evidence_artifact_dir>/checkpoints.json``
        — there is no pytest run and therefore no log to summarize.  The
        stuck-sub-agent fallback used to degrade such a walk into
        "no test output captured" and threw the observations away, even
        when every checkpoint had passed.

        The rule here is the same all-or-nothing one the in-band path
        uses (:meth:`_compute_puppeteer_verdict`): every checkpoint must
        be ``passed=true``.  A missing or malformed artifact is a FAILED
        that **names the artifact** — it must never claim that a test log
        was read, because a browser walk does not produce one.

        ``terminated`` says the framework itself ended this attempt.
        When it is true, the verdict **discloses** that fact (2026-11-10,
        VP-032): the dispatching agent gives every VP an
        ``evidence_artifact_dir``, so this branch is the one every
        browser verification point takes, and a verdict that returned
        here without the disclosure hid the framework's own decision
        behind a statement about the deliverable.

        Returns ``None`` for a method that does not keep its evidence in
        a checkpoint artifact (the caller then falls back to the log).
        """
        method = str(
            vp_node.get("verification_method") or self.method or ""
        ).strip()
        if method not in self._BROWSER_EVIDENCE_METHODS:
            return None
        artifact_dir = str(
            vp_node.get("evidence_artifact_dir") or ""
        ).strip()
        if not artifact_dir:
            # No artifact directory was handed to this VP — there is
            # nothing to read, so the log path stays in charge.
            return None

        path = Path(artifact_dir) / CHECKPOINTS_FILENAME
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._checkpoint_artifact_failure(
                path, "the evidence artifact is missing",
                terminated=terminated,
            )
        except OSError as e:
            return self._checkpoint_artifact_failure(
                path, f"the evidence artifact could not be read: {e}",
                terminated=terminated,
            )
        try:
            payload = json.loads(raw)
        except ValueError as e:
            return self._checkpoint_artifact_failure(
                path, f"the evidence artifact is not valid JSON: {e}",
                terminated=terminated,
            )

        checkpoints = (
            payload.get("checkpoints") if isinstance(payload, dict) else None
        )
        if not isinstance(checkpoints, list):
            return self._checkpoint_artifact_failure(
                path, "the evidence artifact carries no 'checkpoints' list",
                terminated=terminated,
            )
        if not checkpoints:
            return self._checkpoint_artifact_failure(
                path, "the evidence artifact carries an empty checkpoint list",
                terminated=terminated,
            )

        failed = [
            c for c in checkpoints
            if not (isinstance(c, dict) and bool(c.get("passed")))
        ]
        passed_count = len(checkpoints) - len(failed)
        summary = f"{passed_count}/{len(checkpoints)} checkpoints passed"
        if not failed:
            reasons = [
                f"stuck sub-agent: every checkpoint in {path} passed "
                f"({summary}) — graded from the evidence artifact, "
                f"not from a test log"
            ]
            if terminated:
                reasons.append(self._termination_disclosure(
                    "the artifact it left behind is the whole of the "
                    "walk's evidence"
                ))
            return Verdict(
                verdict="PASSED",
                reasons=reasons,
                evidence=[f"{path}: {summary}"],
                model_complexity=self.model_complexity,
            )

        reasons = [
            f"stuck sub-agent: {summary} — {len(failed)} checkpoint(s) "
            f"failed in {path}"
        ]
        for checkpoint in failed:
            selector = (
                str(checkpoint.get("selector") or "")
                if isinstance(checkpoint, dict) else ""
            )
            expected = (
                str(checkpoint.get("expected") or "")
                if isinstance(checkpoint, dict) else ""
            )
            actual = (
                str(checkpoint.get("actual") or "")
                if isinstance(checkpoint, dict) else ""
            )
            reasons.append(
                f"checkpoint failed: selector={selector!r} "
                f"expected={expected!r} actual={actual!r}"
            )
        if terminated:
            reasons.append(self._termination_disclosure(
                "the checkpoints it did write are the only ones this "
                "verdict can speak for"
            ))
        return Verdict(
            verdict="FAILED",
            reasons=reasons,
            evidence=[f"{path}: {summary}"],
            model_complexity=self.model_complexity,
        )

    def _checkpoint_artifact_failure(
        self,
        path: Path,
        detail: str,
        terminated: bool = False,
    ) -> "Verdict":
        """A FAILED that names the evidence artifact it could not use.

        ``terminated`` says the framework ended the attempt itself.  The
        artifact statement stays — it is a fact about the walk — but the
        termination is disclosed as well, because without it the verdict
        says nothing about the framework's own decision and reads like a
        verdict on the deliverable (2026-11-10, VP-032).
        """
        reasons = [
            f"stuck sub-agent: {detail} — {path} carries no usable "
            f"checkpoint evidence, so the walk cannot be graded"
        ]
        if terminated:
            reasons.append(self._termination_disclosure(
                "the missing evidence artifact is not a claim that the "
                "deliverable produced no result"
            ))
        return Verdict(
            verdict="FAILED",
            reasons=reasons,
            evidence=[str(path)],
            model_complexity=self.model_complexity,
            # 2026-11-06（层三，合并保留）：这是**框架自己**造的判决 ——
            # 证据产物读不到，框架手里没有可以判决的东西。分类必须是
            # infra（且必须是 deterministic：分类层会按 source 降级一个
            # "defaulted" 的声明），少了它，"框架超时"会被归档成产品缺陷，
            # 开成产品修复任务。
            failure_class=INFRA_FAILURE_CLASS,
            failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
        )

    async def _summarize_vp_result(
        self,
        vp_node: Dict[str, Any],
        output_file: Optional[str],
        coding_tool: Optional[Any],
        log_path: Path,
        artifact_dir: Optional[Path] = None,
        terminated: bool = False,
    ) -> "Verdict":
        """Decide a stuck attempt's verdict — from its artifact first.

        Contract (2026-11-06, VP-015 根因 · 层二/层三/层四):
          * **The artifact comes first.**  A browser leg's verdict is its
            ``checkpoints.json``: the checkpoints the attempt wrote are
            its own machine-written record of what it saw, and they
            answer the question without asking a model to guess.  So
            when they are there, they decide — and no summarizer call is
            made at all.
          * The **evidence directory listing** comes next, and is
            attached to whatever verdict is produced.
          * Only when there is no artifact at all does the old
            fresh-LLM-call summarizer run (2026-09-08 plan): a coding
            tool call with **NO tools allowed** (``allowed_tools=[]``),
            given the **tail** of the tee'd pytest log.  That path is
            kept exactly as it was — a browser leg has no tee log, but a
            pytest leg still does.
          * Every verdict **the framework makes up** — no artifact and
            no log, a summarizer that could not be reached, a summarizer
            that answered something unparseable — carries
            ``failure_class=infra`` and
            ``failure_class_source=deterministic``.  A framework
            timeout is not a broken deliverable, and an unclassified
            one used to be filed as one.

        Why a fresh agent instead of re-running the original:
        ``_self_heal_loop`` could in principle retry the original
        sub-agent, but the original is structured to RUN pytest and
        judge it inline — a re-run is exactly what triggered the
        stuck loop. A second agent with no tools cannot re-run;
        it can only summarize the existing log.

        Why no tools at all (``allowed_tools=[]``):
        a summarizer with even Read access will sometimes try to
        Read other files to "understand context" and get sidetracked.
        Stripping the tool belt is the simplest way to ensure the
        summarizer produces one JSON object and exits.
        """
        vp_id = str(vp_node.get("id", "unknown"))

        # 2026-11-06（层二）：恢复输入按顺序取，先到先得 ——
        # (a) checkpoints.json，(b) evidence/ 目录清单，(c) 旧的 pytest tee
        # 日志。浏览器腿没有 (c)，所以 (a) 必须真的被读。
        checkpoints = read_checkpoints_artifact(artifact_dir)
        if checkpoints:
            # 2026-11-11 (VP-061): 产物优先的分支顺序不动，但它必须**携带**
            # 终止事实 —— 派发层给每个派发的 VP 都发了
            # ``evidence_artifact_dir``，所以浏览器腿在生产上走的正是这条
            # 腿，它返回的判决必须披露框架自己收掉了这次尝试。
            verdict = self._verdict_from_checkpoints(
                checkpoints, vp_node, artifact_dir, terminated=terminated,
            )
            self._write_log(log_path, "summarizer_artifact_verdict", {
                "vp_id": vp_id,
                "artifact_dir": str(artifact_dir),
                "checkpoints": len(checkpoints),
                "verdict": verdict.verdict,
                # 2026-11-11 (VP-061): 日志与判决必须讲同一件事 —— 这条
                # 判决披露了终止事实，日志也得记下框架是不是真的结束了这次
                # 尝试（``summarizer_no_output`` 那条事件早就记了）。
                "terminated": terminated,
            })
            return verdict

        # 2026-11-09 (VP-023): a browser walk leaves its evidence in
        # checkpoints.json, NOT in a pytest log. Decide from the
        # artifact BEFORE reading a log: the no-log branch below used
        # to degrade a walk whose every checkpoint had passed into
        # "no test output captured", and an LLM asked to summarize a
        # log that a browser walk never produces can only disagree
        # with the artifact that actually holds the observations.
        #
        # 2026-11-09（合并）：上面的 ``_verdict_from_checkpoints`` 先读同一
        # 份产物，读到**能读**的产物就用它判决（它还写 failure.json，给
        # 指纹层同一份机器写的失败集合）。它读不到时（缺失／损坏／为空）
        # 才轮到这条腿的严格读法接手 —— 那种情况下判决必须**点名产物**，
        # 不能退回去说 "no test output captured"（浏览器腿没有 pytest 日志）。
        artifact_verdict = self._verdict_from_checkpoint_artifact(
            vp_node, terminated=terminated,
        )
        if artifact_verdict is not None:
            self._write_log(log_path, "summarizer_verdict_from_artifact", {
                "vp_id": vp_id,
                "verdict": artifact_verdict.verdict,
                "reasons": artifact_verdict.reasons,
            })
            return artifact_verdict

        evidence_files = self._list_evidence_dir(artifact_dir)
        if evidence_files:
            self._write_log(log_path, "summarizer_evidence_dir", {
                "vp_id": vp_id,
                "artifact_dir": str(artifact_dir),
                "files": len(evidence_files),
            })

        if coding_tool is None:
            from coding_tool import ClaudeCodingTool
            coding_tool = ClaudeCodingTool(scene="summarizer")

        # Read the tee'd log tail (last ~200 lines is enough for the
        # pytest summary line + the last few FAILED entries).
        log_tail = ""
        log_size = 0
        if output_file:
            try:
                p = Path(output_file)
                if p.exists():
                    log_size = p.stat().st_size
                    with p.open("r", errors="replace") as f:
                        # Slurp the tail efficiently — file may be
                        # hundreds of MB for a full pytest run.
                        # ``deque(maxlen=200)`` keeps memory bounded.
                        from collections import deque
                        tail_lines: deque = deque(maxlen=200)
                        for line in f:
                            tail_lines.append(line)
                        log_tail = "".join(tail_lines)
            except OSError as e:
                self._write_log(log_path, "summarizer_log_unreadable", {
                    "vp_id": vp_id, "output_file": output_file,
                    "error": str(e)[:200],
                })

        if not log_tail:
            # No artifact and no log → can't summarize → return a clear
            # FAILED so the parent split path can take over instead.
            #
            # 2026-11-06（层三）：这是**框架自己**造的判决，分类必须是
            # infra —— 框架的卡死不是交付物坏了。少了这个分类，它会落到
            # ``deliverable_defect`` / ``defaulted`` 上，于是"框架超时"
            # 被归档成产品缺陷，开成产品修复任务。
            self._write_log(log_path, "summarizer_no_output", {
                "vp_id": vp_id, "output_file": output_file,
                "artifact_dir": str(artifact_dir),
                "terminated": terminated,
            })
            if terminated:
                # 层四：attempt 的进程组是被框架**收掉**的，reasons 说的
                # 必须是这件事 —— 不能说成"场景没产出任何结果"，那是对
                # 交付物的判决，而框架只是没等到它。
                return Verdict(
                    verdict="FAILED",
                    reasons=[
                        "stuck sub-agent: the attempt was terminated after "
                        "its budget ran out; it left no artifact behind in "
                        f"{str(artifact_dir)!r} and no test log at "
                        f"{output_file!r}"
                    ],
                    evidence=list(evidence_files),
                    model_complexity=self.model_complexity,
                    failure_class=INFRA_FAILURE_CLASS,
                    failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
                )
            return Verdict(
                verdict="FAILED",
                reasons=[
                    "stuck sub-agent: no test output captured at "
                    f"{output_file!r}; cannot summarize"
                ],
                evidence=list(evidence_files),
                model_complexity=self.model_complexity,
                failure_class=INFRA_FAILURE_CLASS,
                failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
            )

        self._write_log(log_path, "summarizer_started", {
            "vp_id": vp_id,
            "output_file": output_file,
            "log_size_bytes": log_size,
            "tail_lines": log_tail.count("\n"),
        })

        prompt = (
            f"You are a VERDICT SUMMARIZER for a verification point that "
            f"hit a stuck sub-agent. The previous verification agent ran "
            f"the test_command but never returned a verdict JSON. Your "
            f"ONLY job is to read the pytest output below and produce a "
            f"structured verdict.\n\n"
            f"VP id: {vp_id}\n"
            f"Title: {vp_node.get('title', '')}\n"
            f"Expected result: {vp_node.get('expected_result', '')}\n"
            f"Test command: {vp_node.get('test_command', '')}\n\n"
            f"Test output (tail, last 200 lines of {output_file}):\n"
            f"```\n{log_tail}\n```\n\n"
            f"Decide PASSED or FAILED based on the pytest summary line "
            f"and the FAILED entries above. If you see \"= X failed, Y "
            f"passed\" then pytest exited non-zero → FAILED. If \"= Y "
            f"passed\" only (no failures), pytest exited 0 → PASSED.\n\n"
            f"Return ONLY this JSON object, no markdown, no prose:\n"
            f'{{"verdict": "PASSED|FAILED", "reasons": [<one-line '
            f'explanation citing the pytest summary line>], "evidence": '
            f'[<last 3-5 FAILED test names if FAILED, else empty list>]}}'
        )
        system_instruction = (
            "You are a verdict summarizer. Read the pytest output in the "
            "user prompt and return ONLY the JSON object requested. Do "
            "NOT call any tools. Do NOT re-run any tests. Do NOT add "
            "prose. Output is the JSON object and nothing else."
        )

        try:
            response_dict = coding_tool.query_json(
                prompt=prompt,
                system_instruction=system_instruction,
                # 2026-11-05: the summarizer's own budget is the same
                # per-method wall clock the attempt that got stuck was
                # given (``wall_clock_cap()``) — not the flat hour.
                timeout=self.wall_clock_cap(),
                allowed_tools=[],  # summarizer has no tools
            )
        except Exception as e:
            self._write_log(log_path, "summarizer_query_failed", {
                "vp_id": vp_id, "error": str(e)[:300],
            })
            # 2026-11-06（层三）：框架自己造的判决 —— 分类必须是 infra。
            reasons = [
                f"stuck sub-agent: summarizer query failed: "
                f"{type(e).__name__}: {str(e)[:200]}"
            ]
            if terminated:
                # 2026-11-10（VP-032）：终止事实必须在**每一条**会返回
                # 判决的分支上被披露。
                reasons.append(self._termination_disclosure(
                    "the failure above is the framework's own summarizer "
                    "failure, not the deliverable's behaviour"
                ))
            return Verdict(
                verdict="FAILED",
                reasons=reasons,
                evidence=list(evidence_files),
                model_complexity=self.model_complexity,
                failure_class=INFRA_FAILURE_CLASS,
                failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
            )

        raw = json.dumps(response_dict) if not isinstance(
            response_dict, str
        ) else response_dict
        try:
            verdict = parse_verdict_to_dataclass(raw)
        except VerdictParseError as e:
            self._write_log(log_path, "summarizer_verdict_parse_error", {
                "vp_id": vp_id,
                "error": str(e)[:300],
                "raw_head": raw[:400],
            })
            # 2026-11-06（层三）：同上 —— 框架自己造的判决。
            reasons = [
                f"stuck sub-agent: summarizer returned unparseable "
                f"verdict ({type(e).__name__}: {str(e)[:200]})"
            ]
            if terminated:
                # 2026-11-10（VP-032）：同上 —— 终止事实必须被披露。
                reasons.append(self._termination_disclosure(
                    "the failure above is the framework's own parse "
                    "failure, not the deliverable's behaviour"
                ))
            return Verdict(
                verdict="FAILED",
                reasons=reasons,
                evidence=[raw[:500], *evidence_files],
                model_complexity=self.model_complexity,
                failure_class=INFRA_FAILURE_CLASS,
                failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
            )

        self._write_log(log_path, "summarizer_returned_verdict", {
            "vp_id": vp_id,
            "verdict": verdict.verdict,
            "reasons_count": len(verdict.reasons or []),
            "evidence_count": len(verdict.evidence or []),
        })
        if evidence_files:
            # The attempt's own evidence listing rides along with the
            # verdict — it is the record of what the attempt produced,
            # and the verdict is the framework's reading of it.
            verdict.evidence = list(verdict.evidence or []) + list(
                evidence_files
            )
        if terminated:
            # 2026-11-10（VP-032）：终止事实必须在**每一条**会返回判决的
            # 分支上被披露 —— 这一条是模型读的日志尾，框架仍然得说清楚
            # 是**它**收掉了这次尝试。
            verdict.reasons = list(verdict.reasons or []) + [
                self._termination_disclosure(
                    "this verdict is the framework's reading of the log "
                    "tail, not of a walk that ran to completion"
                )
            ]
        return verdict

    def _context_pack_reference_line(self) -> str:
        """The line that points this brief at the round's context pack.

        2026-10-27（A5）: the environment facts live in ONE file per
        round (``<plan_dir>/context/round-<n>.json``), written by the
        execution side.  The verifier reads that file instead of
        exploring the environment itself — which is what it used to do,
        with the same ``ls`` / ``find`` / ``git log`` the coder used.

        Empty when there is no pack to point at: a brief that names a
        file which is not there is worse than a brief that says nothing,
        and a missing pack must never stop a verification.
        """
        plan_id = str(self.plan_id or "").strip()
        if not plan_id:
            return ""
        try:
            plan_dir = absolute_path(resolve_plans_dir() / plan_id)
        except Exception:  # noqa: BLE001 - a missing plan dir is not a failure
            return ""
        pack = latest_context_pack(plan_dir)
        if pack is None:
            return ""
        return context_pack_reference_line(pack)

    def _write_scope_block(self, vp_node: Dict[str, Any]) -> List[str]:
        """The visible write scope — what the sub-agent may not write.

        2026-11-02 (task 8): the mechanical guard
        (``verification_write_guard``) refuses a write outside the plan
        directory and the temp dirs, but it speaks **after** the fact:
        the sub-agent writes, and only then gets refused. That costs one
        retry of the budget (three attempts by default), and the attempt
        itself may already have touched the tree under verification —
        "verification" rewritten by the verifier.

        So the boundary is stated up front, in the prompt: the repository
        under verification is read-only; scripts, logs and evidence go to
        the plan directory or to /tmp.

        The paths come from the VP node: ``project_root`` (the tree under
        verification) and ``evidence_artifact_dir`` (whose parent chain
        leads to the plan directory). A missing path does not remove the
        block — it only makes the wording pathless ("the verified
        repository" / "the plan directory"). A prompt without any boundary
        is exactly the failure mode this block exists to prevent.
        """
        project_root = str(vp_node.get("project_root") or "").strip()
        repository = project_root or "the verified repository"
        plan_dir = plan_dir_of_artifact_dir(
            vp_node.get("evidence_artifact_dir")
        )
        plan = plan_dir or "the plan directory"
        return [
            "",
            f"WRITE SCOPE: {repository} is READ-ONLY. Write scripts, logs "
            f"and evidence only under {plan} or /tmp. Any write inside "
            "the repository is refused by a hook and recorded as "
            "verifier_mutated_tree.",
        ]

    def _build_prompt(self, vp_node: Dict[str, Any], attempt: int) -> str:
        """Build the LLM prompt from a verification point node.

        The prompt is short and structured: VP id, title, method,
        expected result, and — since 2026-09-18 — the ``target_url`` a
        ``ui_validation`` point is required to declare. On retries
        (attempt > 0) a self-heal hint is appended so the LLM knows the
        previous try failed, and ``_evidence_contract`` appends the
        artifact requirement for the LLM-judged methods.

        The prompt explicitly forbids project-wide test runs and
        coverage analysis. Without that constraint the LLM treats the
        verification as a free-form code review and reports a synthetic
        verdict based on the project's overall test state — observed
        once as 21 unrelated test failures plus a 59% coverage number
        reported against a point that verified three specific functions.

        Since 2026-10-27 (A5) it also carries ONE line naming the
        round's context pack — the file the environment facts live in —
        so the verifier reads them instead of re-deriving them with
        ``ls`` / ``find`` / ``git log``.

        Since 2026-11-02 it also carries the WRITE SCOPE declaration:
        the repository under verification is read-only, scripts / logs /
        evidence go to the plan directory or to /tmp. The sub-agent has
        to learn that boundary *before* it writes, not from a hook
        refusal afterwards (see :meth:`_write_scope_block`).
        """
        parts = [
            f"Verification point: {vp_node.get('id', 'unknown')}",
            f"Title: {vp_node.get('title', '')}",
            f"Method: {vp_node.get('verification_method', self.method)}",
            f"Priority: {vp_node.get('priority', 'medium')}",
            f"Expected result: {vp_node.get('expected_result', '')}",
        ]
        # 2026-11-02（任务 8）：可写范围必须在**写之前**就摆在子 agent 眼前。
        # 护栏只在写下去之后才拒绝它 —— 那时候重试预算已经烧掉一次，而被验证
        # 仓库的工作区可能已经被改过。
        parts.extend(self._write_scope_block(vp_node))
        prd_criteria = str(vp_node.get("related_prd_criteria") or "").strip()
        if prd_criteria:
            parts.append(f"PRD criterion: {prd_criteria}")
        # 2026-09-18: a ui_validation VP is *required* to declare a
        # target_url, but it never reached this prompt — the sub-agent had
        # to infer the page from prose. Emit it up front.
        target_url = str(vp_node.get("target_url") or "").strip()
        if target_url:
            parts.append(f"Target URL: {target_url}")
        evidence_command = str(vp_node.get("evidence_command") or "").strip()
        if evidence_command:
            parts.append(
                "Supporting evidence command (optional; it does NOT decide "
                f"the verdict): {evidence_command}"
            )
        service_env = vp_node.get("service_env")
        if isinstance(service_env, dict) and service_env:
            parts.append("")
            parts.append(
                "## Managed services — the framework already started these. "
                "Use these addresses; do NOT start your own copy."
            )
            for key in sorted(service_env):
                parts.append(f"  {key}={service_env[key]}")
        # 2026-10-27（A5）: the environment facts are NOT inlined here and
        # the verifier is NOT sent out to find them — the round's context
        # pack holds them, and this line is where the verifier is told so.
        pack_reference = self._context_pack_reference_line()
        if pack_reference:
            parts.append("")
            parts.append(pack_reference)
        parts.extend([
            "",
            "PROCESS SAFETY (HARD RULE — no exceptions):",
            "1. Never run `killall`, and never run `pkill` (with or without -f). Both select",
            "   processes by name or by a substring of their command line, so they reach every",
            "   process on the host that matches — not just the ones you started. On 2026-10-27",
            "   a `pkill -f \"backend/cli.py\"` issued by a verification sub-agent killed the",
            "   repair executors of three unrelated plans.",
            "2. To stop a process, kill the exact PID you recorded when you started it:",
            "   `kill <pid>` (add `-- -<pgid>` only if you created that process group yourself).",
            "3. If you did not record the PID, discover it first: `lsof -ti tcp:<port>` returns",
            "   the listening PIDs; kill those PIDs explicitly, one per line.",
            "4. Never kill a PID you did not start, and never touch a process outside your own",
            "   fixture. Stopping other people's workloads is out of scope for a verification",
            "   point — report it as an observation instead.",
            "",
            "STRICT SCOPE: Your verdict MUST rest on evidence you actually "
            "gathered for THIS verification point. Do NOT run project-wide "
            "test suites, do NOT compute project-wide coverage, and do NOT "
            "pull in evidence from files this point does not exercise. "
            "Without that constraint the verdict drifts into a synthetic "
            "code review — observed once as 21 unrelated test failures and "
            "a 59% coverage number reported against a point that verified "
            "three specific functions.",
            "",
            "EVIDENCE: Whatever your method, you must be able to show the "
            "basis of your conclusion. For code_review and ui_validation "
            "the framework checks a specific artifact (see the EVIDENCE "
            "CONTRACT below) and will refuse a PASSED it cannot verify.",
            "",
            "ADVERSARIAL SELF-CHECK (MANDATORY — do this before you settle on a verdict):",
            "1. Write down at least two concrete counter-hypotheses for how this "
            "verification point could still be FAILED despite surface appearances "
            "(e.g. the asserted path is dead code, the cited test is skipped or "
            "xfail, the evidence command passes vacuously, a related PRD "
            "requirement is only partially met).",
            "2. Test each counter-hypothesis against evidence you gather yourself "
            "(read the actual lines, run the command, inspect the assertion).",
            "3. Report PASSED only when every counter-hypothesis is refuted by "
            "evidence. If any counter-hypothesis survives, report FAILED and put "
            "the surviving one first in `reasons`, with the evidence that "
            "supports it.",
            "4. In `reasons`, name the counter-hypotheses you tested and how each "
            "was refuted — a bare 'all checks passed' is not acceptable.",
            "",
            "PROGRESS REPORTING (if the work is long-running): The "
            "orchestrator's staleness watchdog force-terminates plans whose "
            "verification log has no fresh writes for >~15 min. If what you "
            "are about to do would exceed ~5 min of silent wall-clock time, "
            "structure it so observable progress is emitted continuously — "
            "pipe through `tee` to a stable path (e.g. `/tmp/"
            "<vp_id>_progress.log`) and, where the framework exposes it, "
            "append `progress` events to the sub-agent log via "
            "`_write_log(log_path, 'progress', {...})`. Each progress event "
            "MUST carry real observed data (current item, completed/total "
            "counts, last log line) — never an empty heartbeat. See the "
            "system prompt's TIME-BUDGET-AND-PROGRESS block for the full "
            "anti-cheat contract.",
        ])

        if attempt > 0:
            parts.append("")
            if self.pending_evidence_issues:
                # 2026-11-08（依据契约自愈）：这一轮的重试是**依据产物不合格**
                # 触发的，不是「分析失败、改代码、重跑 test_command」。那句话
                # 会把验证 agent 送进被验证的仓库 —— 那个方法的作用域是只读，
                # 验证独立性也不许它改代码。所以这里换成「补齐产物」的措辞，
                # 并把框架自己的 complaint 原文带进来：子 agent 知道该补哪一
                # 条引用、补什么原文。
                parts.extend(self._evidence_retry_block(vp_node))
            else:
                parts.append(
                    f"Retry attempt {attempt + 1}/{self.max_retries + 1}: "
                    "the previous attempt failed. Analyse the failure, "
                    "fix the underlying issue (edit code, install a "
                    "missing dependency, or tweak config), then re-run "
                    "the test_command and report the verdict."
                )
        parts.extend(self._evidence_contract(vp_node))
        return "\n".join(parts)

    def _evidence_retry_block(self, vp_node: Dict[str, Any]) -> List[str]:
        """The retry hint for an attempt that failed the evidence check.

        The generic retry wording ("analyse the failure, fix the
        underlying issue (edit code, install a missing dependency, or
        tweak config), then re-run the test_command") is wrong here: the
        verdict was not disputed, only the artifact it shipped with was
        incomplete, and the repository under verification is READ-ONLY
        for a verification agent — sending it in to "fix the issue" would
        corrupt the very independence the method depends on.

        So this block asks for exactly one thing: rewrite the artifact,
        with the framework's own complaints quoted verbatim so the
        sub-agent knows which entry to complete and with what text.
        """
        method = str(
            vp_node.get("verification_method") or self.method or ""
        ).strip()
        artifact_dir = str(vp_node.get("evidence_artifact_dir") or "").strip()
        filename = (
            "citations.json" if method == "code_review" else "checkpoints.json"
        )
        artifact_path = (
            str(Path(artifact_dir) / filename) if artifact_dir else filename
        )
        block = [
            f"Retry attempt — the previous attempt's EVIDENCE ARTIFACT did "
            f"not satisfy the evidence contract. Your conclusion was not "
            f"disputed: only the artifact you shipped with it was "
            f"incomplete.",
            "",
            f"DO NOT change the repository under verification: it is "
            f"READ-ONLY for you, and a verification agent that edits what "
            f"it is verifying is not a verification. Fix ONLY the "
            f"evidence artifact:",
            f"    {artifact_path}",
            "",
            "The framework rejected these entries — each one is a concrete "
            "thing to complete, with the text it expects there:",
        ]
        for issue in self.pending_evidence_issues:
            block.append(f"  - {issue}")
        block.append("")
        block.append(
            f"Rewrite {artifact_path} so every entry carries what the "
            "contract asks for (the quoted text you actually read there, "
            "the real line number, the path relative to the project root), "
            "then report the verdict again."
        )
        return block

    def _evidence_contract(self, vp_node: Dict[str, Any]) -> List[str]:
        """The "prove your conclusion" block for the LLM-judged methods.

        2026-09-18: ``code_review`` and ``ui_validation`` rest on an LLM's
        word, and the old cross-method overlay could not check that word
        against anything — it only compared a *self-reported* exit code,
        which is how the same artifact graded PASSED in one round and
        FAILED in the next. The framework now checks the artifact each
        method was supposed to produce; a PASSED whose artifact is
        missing or does not hold up is downgraded to FAILED.

        The block is generated rather than baked into the template
        constants because the artifact directory is per-VP.
        """
        method = str(
            vp_node.get("verification_method") or self.method or ""
        ).strip()
        artifact_dir = str(vp_node.get("evidence_artifact_dir") or "").strip()
        if not artifact_dir:
            return []

        if method == "code_review":
            # 2026-10-29（引用必须落在计划的 project_dir 上）："项目根"这个词
            # 指代不明 —— 计划目录（citations.json 的所在地）通常在另一个
            # checkout 里，那里同名文件的行号不是本 VP 的证据。VP-010 第 1 轮
            # 的 30 条引用就是这么全被判 unverifiable 的：引用的树不是判据
            # 的树。所以这里点名**唯一**一棵允许引用的树。
            project_root = str(vp_node.get("project_root") or "").strip()
            project_root_rules: List[str] = []
            if project_root:
                project_root_rules = [
                    "- The project root below is the ONLY tree your citations "
                    "may come from:",
                    f"    {project_root}",
                    "- Read every file you cite from THAT tree. The `file` / "
                    "`line` / `snippet` triple must describe a line of the "
                    "file as it is there — not as it is in some other copy "
                    "of the same file name.",
                    "- The plan directory (where citations.json is written) "
                    "is a DIFFERENT checkout: the same file name there "
                    "carries different line numbers, and those numbers are "
                    "not evidence for this VP. The framework resolves your "
                    "citations against the project root above and refuses "
                    "every citation that does not match it.",
                ]
            # 2026-11-01 (the forensic artifacts are staged): the plan
            # directory's forensic products -- the audit ledger, the
            # session census, the two trap rounds -- live in a checkout the
            # framework never resolves citations against.  They are staged
            # under this root: inside the project root (the tree the
            # framework resolves against) and outside the plans tree the
            # api service serves.  A citation naming one of them must use
            # the staged path, relative to the project root.
            staging_root = str(
                vp_node.get("evidence_staging_root") or "").strip()
            staging_rules: List[str] = []
            if staging_root:
                staging_rel = ""
                if project_root:
                    try:
                        staging_rel = str(
                            Path(staging_root).relative_to(Path(project_root))
                        )
                    except ValueError:
                        staging_rel = ""
                staging_rules = [
                    "- The plan directory's own forensic artifacts (the "
                    "audit ledger, the session census, the trap rounds) are "
                    "staged for you under this root:",
                    f"    {staging_root}",
                    "- Cite THOSE artifacts from the staged copy: `file` is "
                    "the staged path relative to the project root"
                    + (
                        f" (e.g. `{staging_rel}/vp_artifacts/VP-014/...`)."
                        if staging_rel else "."
                    ),
                    "- The plan directory is NOT a citable tree: a citation "
                    "that names a path inside it cannot be resolved by the "
                    "framework and is downgraded to unverifiable.",
                ]
            return [
                "",
                "## EVIDENCE CONTRACT — write citations.json (MANDATORY)",
                "Every conclusion you report must rest on specific lines "
                "you actually read. Write exactly this file (create the "
                "directory if needed):",
                f"    {artifact_dir}/citations.json",
                '{"citations": [',
                '  {"file": "<path relative to the project root>",',
                '   "line": <1-based line number>,',
                '   "snippet": "<the exact text you saw on that line>",',
                '   "supports": "<which part of the assertion this proves>"}',
                "]}",
                "Rules:",
                "- At least one citation. Cite every file/line your verdict "
                "depends on.",
                "- `snippet` must be text that is actually on (or within a "
                "couple of lines of) `line`. The framework reads the file "
                "and checks this; a citation it cannot resolve is not "
                "evidence.",
                "- `snippet` must be the verbatim line — do NOT abbreviate "
                "with `...` or other ellipsis markers. The framework's "
                "whitespace-normalised, single-line lookup cannot resolve a "
                "snippet whose middle is hidden; an ellipsis always reads "
                "as \"no real citation here\" and the citation is downgraded "
                "to unverifiable.",
                "- `file` must be a path relative to the project root "
                "(e.g. `backend/foo.py`, `SECURITY_AUDIT.md`). Absolute "
                "paths, paths under `/tmp/`, `/private/tmp/`, or any other "
                "directory outside this repository are NOT project evidence "
                "— the framework rejects them as `outside_project` and the "
                "citation is treated as unverifiable.",
                *project_root_rules,
                *staging_rules,
                "- A verdict of PASSED with no citations, or with citations "
                "that do not resolve, is NOT honoured — it is downgraded to "
                "FAILED.",
            ]

        if method in ("ui_validation", "e2e"):
            observed = (
                "Every end-to-end observation you report must be one you "
                "actually made in this run."
                if method == "e2e" else
                "Every UI observation you report must be one you actually "
                "made."
            )
            return [
                "",
                "## EVIDENCE CONTRACT — write checkpoints.json (MANDATORY)",
                observed + " Write exactly this file (create the directory if "
                "needed):",
                f"    {artifact_dir}/checkpoints.json",
                '{"checkpoints": [',
                '  {"selector": "<the selector you looked at>",',
                '   "expected": "<what the requirement says should be there>",',
                '   "actual": "<what you actually observed>",',
                '   "passed": true|false}',
                "]}",
                "Rules:",
                "- At least one checkpoint. No checkpoint may omit "
                "`selector` or `actual` — \"it looked fine\" is not a "
                "checkpoint.",
                "- `passed` must be a boolean, and your verdict must agree "
                "with it: PASSED requires every checkpoint to have "
                "passed=true.",
                "- A PASSED whose checkpoints.json is missing or malformed "
                "is NOT honoured — it is downgraded to FAILED.",
            ]

        return []

    async def _self_heal(
        self,
        vp_node: Dict[str, Any],
        last_verdict: Optional[Verdict],
        last_error: Optional[str],
        log_path: Path,
        attempt: int,
    ) -> str:
        """Apply a self-heal action based on the last failure.

        The default implementation is a no-op stub: it returns a
        human-readable description of the would-be heal action so
        callers (and tests) have something concrete to assert on,
        but it does NOT actually mutate code or install packages.
        Production deployments can subclass :class:`VerificationSubAgent`
        and override this method to wire in real fix-up logic (e.g.,
        an LLM that analyses the test output and emits a patch).

        Returns:
            A short description of the heal action taken.
        """
        if last_error is not None:
            return f"attempted heal after error: {last_error[:120]}"
        if last_verdict is not None and last_verdict.reasons:
            return (
                f"attempted heal based on reasons: "
                f"{last_verdict.reasons[0][:120]}"
            )
        return "attempted heal (no specific reason recorded)"

    #: How often the tool-output pump writes a liveness line while a
    #: sub-agent's tool call is streaming. Must be comfortably under the
    #: verification watchdog's staleness threshold (3600s in
    #: ``.env``); 60s gives operators a dense trail without flooding the
    #: attempt log.
    OUTPUT_HEARTBEAT_INTERVAL_SEC: int = int(
        os.environ.get("VP_OUTPUT_HEARTBEAT_INTERVAL_SEC", "60")
    )

    def _pump_tool_output(
        self,
        vp_id: str,
        scoped_tool: Any,
        log_path: Optional[Path],
        watchdog_handle: Any,
        stop_event: "threading.Event",
    ) -> None:
        """Mirror a streaming tool call's liveness into the attempt log.

        2026-09-14: a live pytest prints progress lines
        even when it runs far longer than any idle threshold — that is
        exactly the case that was being killed as "silent". The coding
        tool already stamps ``_last_output_ts`` per stdout line
        (``coding_tool`` read loop); this thread samples it and writes a
        ``tool_output_heartbeat`` event whenever it advanced, which:
          * refreshes the attempt-log mtime the watchdog's staleness
            check measures (``logs/vp_attempts/*.log``);
          * refreshes the registry handle so ``find_stale`` does not
            kill the subprocess;
          * leaves an operator-visible trail ("still printing: <line>").

        Runs on a daemon thread and never raises — a missing attribute or
        a closed log must not disturb the VP.
        """
        started = time.monotonic()
        last_ts: Optional[float] = None
        while not stop_event.wait(self.OUTPUT_HEARTBEAT_INTERVAL_SEC):
            try:
                ts = getattr(scoped_tool, "_last_output_ts", None)
                if ts is None or ts == last_ts:
                    # No new stdout since the last sample — staying silent
                    # is the point (a genuinely idle tool produces no
                    # heartbeat, so the watchdog can still catch it).
                    continue
                last_ts = ts
                last_line = str(
                    getattr(scoped_tool, "_last_output_line", "") or ""
                )
                self._write_log(log_path, "tool_output_heartbeat", {
                    "vp_id": vp_id,
                    "elapsed_sec": round(time.monotonic() - started, 1),
                    "last_line": last_line[:200],
                })
                if watchdog_handle is not None and self.registry is not None:
                    try:
                        self.registry.mark_progress(
                            self.plan_id, watchdog_handle,
                            stage="tool_streaming",
                        )
                    except Exception:
                        pass
            except Exception:
                continue

    @staticmethod
    def _write_log(log_path: Path, event: str, data: Dict[str, Any]) -> None:
        """Append a structured JSON-line entry to the run's log file.

        Failures in the logger must never break the run() result, so
        every operation is wrapped in a broad try/except. The file
        format is JSON-lines (one object per line), matching the
        convention used by ``plans/{plan_id}/execution.log``.
        """
        if log_path is None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "event": event,
                "data": data,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Never let logging failures propagate.
            pass


class WatchdogKilledError(Exception):
    """Raised by ``_execute_attempt`` when the sub-agent watchdog killed
    the subprocess (because ``last_progress_ts`` went stale) and the
    per-handle kill count is exactly 1 — i.e. the executor should retry
    once with a fresh subprocess via the existing ``_self_heal_loop``.

    When the kill count is already 2 (a second kill on the same VP),
    ``_execute_attempt`` returns ``Verdict(verdict="SKIPPED", ...)``
    directly without raising — the round should advance to the next
    VP rather than burning the entire retry budget.

    Attributes:
        kill_count: The value of ``SubAgentHandle.watchdog_kill_count``
            at the moment the exception was raised. ``1`` for the
            retry path; ``>= 2`` for the SKIPPED path.
        original_exc: The underlying ``Exception`` raised by
            ``scoped_tool.query_json`` after the SIGKILL broke the
            stdout pipe.
    """

    def __init__(self, kill_count: int, original_exc: BaseException):
        self.kill_count = kill_count
        self.original_exc = original_exc
        super().__init__(
            f"sub-agent subprocess killed by watchdog (kill_count="
            f"{kill_count}): {original_exc}"
        )


class StuckAgentError(Exception):
    """Raised by ``_execute_attempt`` when the outer wall-clock cap
    fires — i.e. the original sub-agent ran the test_command (the
    tee'd log has its summary line) but never returned verdict JSON
    before the outer cap elapsed.

    Per the 2026-09-08: the first sub-agent's context (the
    tee'd log file at the conventional ``/tmp/vp_<id>_progress.log``
    path) is handed to a fresh summarizer LLM call with **no tools**
    that reads the log and returns a structured verdict. The first
    agent is allowed to re-run pytest if it suspects flakiness; the
    summarizer only steps in when the first agent never produced a
    verdict at all. This avoids the previous behaviour where the
    orchestrator killed the round budget on a stuck LLM and the
    parent had to fall through to ``status="timeout"`` with no
    verdict.

    Attributes:
        elapsed: Wall-clock seconds from ``_execute_attempt`` start
            to the outer cap firing.
        threshold: The configured outer-cap threshold that fired.
        vp_id: The VP id (for logging).
        output_file: The tee'd log path the summarizer should Read.
            ``None`` if pytest never produced one (in which case the
            summarizer degrades to ``FAILED, reasons=["no test
            output captured"]``).
        artifact_dir: The artifact directory the attempt was writing
            into (``<plan_dir>/vp_artifacts/<vp_id>/``).  The recovery
            path reads the attempt's own machine-written record from
            here — a browser leg writes no pytest tee log, so this is
            the only place its result can be.
        terminated: Whether the abandoned attempt's process group was
            actually killed — by the in-flight dispatch kill, or by the
            termination that follows it.  The reason the verdict carries
            says "terminated" only when this is true.
        killed: Whether the in-flight dispatch was actually ended when
            the outer cap fired (2026-11-10).  ``False`` means there
            was nothing to kill, or the kill could not be attempted.
    """

    def __init__(
        self,
        elapsed: float,
        threshold: float,
        vp_id: str,
        output_file: Optional[str] = None,
        artifact_dir: Optional[Path] = None,
        terminated: bool = False,
    ):
        self.elapsed = elapsed
        self.threshold = threshold
        self.vp_id = vp_id
        self.output_file = output_file
        self.artifact_dir = artifact_dir
        self.terminated = terminated
        # 2026-11-10 — the outer cap ends the in-flight dispatch
        # before it raises; this carries the answer ("was there
        # something to kill?") to the log that reports the routing.
        self.killed: bool = False
        super().__init__(
            f"stuck sub-agent for vp_id={vp_id}: query_json ran for "
            f"{elapsed:.1f}s (threshold={threshold:.1f}s); routing to "
            f"summarizer with output_file={output_file!r}"
        )


class BlockedPathError(Exception):
    """Raised when a security net hook blocks a sub-agent action.

    The security net lives in the temp settings.json built by
    :class:`SettingsJsonBuilder`. When the sub-agent tries to
    Edit/Write outside its own workspace or run a blacklisted Bash
    command (e.g., ``rm -rf plans/``), the hook returns exit code 2,
    which the SDK reports as a blocked tool call. The sub-agent
    catches that signal and raises this exception so
    :meth:`VerificationSubAgent.run` can return a structured
    :class:`Verdict` with ``verdict="BLOCKED"`` (rather than
    retrying, which would just keep hitting the same blacklist).

    Attributes:
        reason: Human-readable explanation of why the action was
            blocked (e.g. "Edit/Write outside the workspace blocked").
        evidence: Raw signal from the hook (e.g. "hook exit=2 at call 0").
    """

    def __init__(self, reason: str = "", evidence: str = "") -> None:
        self.reason = reason
        self.evidence = evidence
        message = reason or "security net blocked"
        super().__init__(message)


# ---------------------------------------------------------------------------
# SettingsJsonBuilder — temporary settings.json with 11 hard PreToolUse hooks
# ---------------------------------------------------------------------------
#
# PRD Decision Point 2 pins 9 hard PreToolUse hooks for the verification
# sub-agent. The hooks are split into two groups:
#
#   * Basic 6 (Bash): rm -rf /, fork bomb, mkfs, dd if=/dev/zero, force
#     push to main, pkill/killall. These are system-level safety nets that
#     protect against irreversible damage from any sub-agent.
#
#   * Extended 3 (Bash ×2, Edit|Write ×1): plans/ deletion interception,
#     server-restart interception, and Edit/Write outside the
#     sub-agent's own workspace. These guard the project a sub-agent
#     was pointed at against accidental destruction.
#
# 2026-11-02 — the 10th and 11th hook (verifier write scope, Edit|Write
# and Bash) close the last hole in that list: ``exec_path_block`` treats
# the whole project directory as writable, which is right for an
# EXECUTION sub-agent and wrong for a VERIFICATION one — the repository
# under verification is exactly what the latter must not touch (VP-055
# rewrote ``backend/verification_agent.py`` mid-verification on
# 2026-11-02). Those two hooks point at the guard script
# ``backend/verification_write_guard.py``, whose scope is declared
# through the environment: no scope, no enforcement.
#
# The order of the 11 rules is stable: basic 6 in front, extended 3,
# then the two write-scope guards last. Tests pin the order so the
# SDLC audit trail is reproducible.
#
# Each rule's command is a one-liner that reads a JSON payload from
# stdin (the Claude Code SDK supplies ``tool_input`` keyed by tool
# name) and exits 2 to block or 0 to allow. The descriptive name is
# kept ≤ 30 Chinese characters per the PRD's "≤30字" rule.

_BASIC_RM_ROOT_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'rm\\s+-(?=[a-zA-Z]*[rR])(?=[a-zA-Z]*[fF])[a-zA-Z]+',c) "
    "and re.search(r'(/\\s|/$|/ )',c) else 0)\""
)

_BASIC_FORK_BOMB_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r':\\s*\\(\\s*\\)\\s*\\{.*:\\s*\\|.*\\}\\s*;\\s*:',c) else 0)\""
)

_BASIC_MKFS_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bmkfs(\\.|\\b)',c) else 0)\""
)

_BASIC_DD_ZERO_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bdd\\b.*if=/dev/(zero|urandom)',c) else 0)\""
)

_BASIC_FORCE_PUSH_MAIN_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bgit\\b.*\\bpush\\b.*-f',c) "
    "and re.search(r'\\bmain\\b',c) else 0)\""
)

_BASIC_PKILL_KILLALL_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bpkill\\b|\\bkillall\\b',c) else 0)\""
)

_EXT_PLANS_DELETE_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\brm\\b.*plans/',c) else 0)\""
)

_EXT_SERVER_RESTART_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'pkill\\s+.*server\\.py',c) "
    "or re.search(r'\\bkill\\b.*server\\.py',c) else 0)\""
)

_EXT_EXEC_PATH_BLOCK = (
    "python3 -c "
    "\"import json,sys,os,pathlib,tempfile;"
    "d=json.load(sys.stdin);"
    "p=d.get('tool_input',{}).get('file_path','') or '';"
    "R=lambda x: str(pathlib.Path(x).resolve()) if x else '';"
    "inside=lambda x,r: bool(r) and (x==r or x.startswith(r+os.sep));"
    "rp=R(p);pdr=R(os.environ.get('PDT_PROJECT_DIR',''));"
    "pr=R(os.environ.get('PDT_PLANS_DIR',''));tmp=R(tempfile.gettempdir());"
    "scratch=rp.startswith('/tmp/') or rp.startswith('/private/tmp/') "
    "or inside(rp,tmp);"
    "blocked=bool(rp) and bool(pdr) and not "
    "(inside(rp,pdr) or inside(rp,pr) or scratch);"
    "sys.exit(2 if blocked else 0)\""
)


#: The guard script that enforces the verification sub-agent's write scope.
#:
#: Its absolute path is resolved from THIS file's own directory, so the
#: command keeps pointing at the right script no matter which checkout
#: (or worktree) the backend runs from.
_WRITE_GUARD_SCRIPT = (
    Path(__file__).resolve().parent / "verification_write_guard.py"
)

#: The command that runs the guard.
#:
#: ``sys.executable`` rather than a bare ``python3``: the hook must run
#: under the same interpreter as the backend that wrote the settings file.
#: Both halves go through ``shlex.quote`` — a checkout path with a space
#: in it would otherwise split into two arguments.
_WRITE_GUARD_COMMAND = (
    f"{shlex.quote(sys.executable)} {shlex.quote(str(_WRITE_GUARD_SCRIPT))}"
)


#: The script that measures the tree before and after a tool call.
#:
#: Like the write guard above, its absolute path is resolved from THIS
#: file's own directory, so the command keeps pointing at the right
#: script no matter which checkout (or worktree) the backend runs from.
_TREE_GUARD_SCRIPT = (
    Path(__file__).resolve().parent / "verification_tree_guard.py"
)

#: The command that runs the tree guard — the interpreter plus the script.
#:
#: Both phases run *this* program; the phase travels in the environment,
#: in front of it, because Claude Code gives a hook exactly one command
#: per entry and the two halves have to run the same measuring code to
#: be comparable.
_TREE_GUARD_BASE_COMMAND = (
    f"{shlex.quote(sys.executable)} {shlex.quote(str(_TREE_GUARD_SCRIPT))}"
)

#: The command that takes the "before" snapshot (PreToolUse).
_TREE_GUARD_PRE_COMMAND = f"PDT_TREE_GUARD_PHASE=pre {_TREE_GUARD_BASE_COMMAND}"

#: The command that takes the "after" snapshot and compares it (PostToolUse).
_TREE_GUARD_POST_COMMAND = f"PDT_TREE_GUARD_PHASE=post {_TREE_GUARD_BASE_COMMAND}"


_HOOK_RULES: Tuple[Dict[str, str], ...] = (
    # Basic 6 — system-level safety nets
    {
        "matcher": "Bash",
        "name": "rm_root_block",
        "command": _BASIC_RM_ROOT_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "fork_bomb_block",
        "command": _BASIC_FORK_BOMB_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "mkfs_block",
        "command": _BASIC_MKFS_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "dd_zero_block",
        "command": _BASIC_DD_ZERO_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "force_push_main_block",
        "command": _BASIC_FORCE_PUSH_MAIN_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "pkill_killall_block",
        "command": _BASIC_PKILL_KILLALL_BLOCK,
    },
    # Extended 3 — this project's own guards
    {
        "matcher": "Bash",
        "name": "plans_delete_block",
        "command": _EXT_PLANS_DELETE_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "pdt_server_restart_block",
        "command": _EXT_SERVER_RESTART_BLOCK,
    },
    {
        "matcher": "Edit|Write",
        "name": "exec_path_block",
        "command": _EXT_EXEC_PATH_BLOCK,
    },
    # Verifier write scope — 1 rule.
    #
    # The verification sub-agent may write inside the plan directory or a
    # temporary directory; the repository under verification is read-only
    # for it. The scope is declared through the environment
    # (``PDT_VP_WRITE_SCOPE`` and friends — see
    # :meth:`SettingsJsonBuilder._scope_env`), so the very same command
    # stays inert for every caller that does not declare a scope: the
    # execution sub-agents, the refiners, and every other place that
    # writes its own settings.json are unaffected.
    #
    # Only Edit|Write is matched. The Bash matcher that used to sit next
    # to it (``verifier_bash_write_block``) was removed on 2026-11-05
    # (task 17): the guard's Bash branch no longer judges anything — it
    # always allows, because the command parser left the decision path
    # (task 7) — so the rule only cost one Python process per Bash call.
    # Writes that slip through are caught by measurement instead: the
    # tree guard compares the tree before and after the call
    # (``verification_tree_guard.py``).
    {
        "matcher": "Edit|Write",
        "name": "verifier_write_scope_block",
        "command": _WRITE_GUARD_COMMAND,
    },
    # Tree snapshot — 1 rule.
    #
    # The rules above all answer "may this call happen?". None of them
    # answers "what did the tree look like afterwards?" — that is the
    # PostToolUse half of the tree guard, and this is its PreToolUse
    # half: the snapshot the comparison is made against.
    #
    # Like the write-scope guards, it is switched on by the environment
    # (``PDT_VP_WRITE_SCOPE`` — see ``backend/verification_tree_guard.py``),
    # not by the rule table: a caller that declares no scope gets no
    # snapshot at all, so the execution sub-agents, the refiners and
    # every other place that writes its own settings.json are unaffected.
    {
        "matcher": "Bash|Edit|Write",
        "name": "tree_snapshot_pre",
        "command": _TREE_GUARD_PRE_COMMAND,
    },
)


@dataclass
class SettingsJsonBuilder:
    """Build a temporary ``settings.json`` with 11 PreToolUse security hooks.

    The first 9 hooks are pinned by PRD Decision Point 2 of the
    verification sub-agent work-package; the next one (verifier write
    scope) was added on 2026-11-02; the last one (tree snapshot) was
    added on 2026-11-04. Each rule is identified by a short ``name``
    (≤ 30 Chinese characters) and the ``command`` is a one-liner
    shell that blocks (exit 2) or allows (exit 0) a given tool
    invocation.

    The built file also carries one **PostToolUse** entry — the second
    half of the tree guard, which compares the tree after the call with
    the snapshot the PreToolUse hook took before it. It is the same
    script with ``PDT_TREE_GUARD_PHASE=post`` in front of it, and it is
    inert unless a write scope is declared (the tree guard switches
    itself on through ``PDT_VP_WRITE_SCOPE``).

    Attributes:
        vp_id: Verification-point ID, e.g. ``"VP-001"``. Used in the
            temp file path.
        plan_id: Plan ID (see ``framework.ids.validate_plan_id``
            for what one may contain). Also used in the temp file
            path.
        plan_dir: The plan's own directory. When it is given, the built
            settings file **declares the write scope** of the verification
            sub-agent (see :meth:`_scope_env`): it may write inside this
            directory or a temporary one, and nowhere else — the
            repository under verification is read-only for it. When it is
            ``None`` (the default) no scope is declared, the scope guard
            stays inert, and the tree guard takes no snapshot.
        round_no: The round this build belongs to. It decides which
            ``round-<n>/`` directory the refusal evidence lands in.
    """

    vp_id: str
    plan_id: str

    #: The plan's own directory — the one writable root of the verification
    #: sub-agent. ``None`` means "no scope declared" (old behaviour).
    plan_dir: Optional[Path] = None

    #: Which round this build belongs to; part of the refusal evidence path.
    round_no: int = 0

    #: Memoised temp path. ``build()`` writes it and ``cleanup()`` removes
    #: it, so the two must agree — see :meth:`_temp_path`.
    _temp_file_path: Optional[str] = field(
        default=None, repr=False, compare=False,
    )

    def build(self) -> str:
        """Build and write the settings.json into a private temp dir.

        The temp file path follows the pattern
        ``<private tmpdir>/verif_settings_{vp_id}_{plan_id}.json``.

        The ``env`` section carries the write scope (when there is one):
        Claude Code folds a settings file's ``env`` into the process
        environment and the hook subprocesses inherit it — that is how
        the scope guard learns where the writable root is, and how
        the tree guard learns whether it is switched on at all.

        The ``PostToolUse`` section carries the second half of the tree
        guard: the same script as the ``tree_snapshot_pre`` rule, with
        ``PDT_TREE_GUARD_PHASE=post`` in front of it, so the tree can be
        compared before and after the call.

        Returns:
            Absolute path to the temp settings file.
        """
        path = self._temp_path()
        settings: Dict[str, Any] = {
            "hooks": {
                "PreToolUse": [self._rule_to_hook(r) for r in _HOOK_RULES],
                "PostToolUse": [self._post_tool_use_hook()],
            }
        }
        scope_env = self._scope_env()
        if scope_env:
            settings["env"] = scope_env
        write_private_json(Path(path), settings)
        return path

    @staticmethod
    def _post_tool_use_hook() -> Dict:
        """The PostToolUse entry that compares the tree after the call.

        同一个脚本、同一个 matcher，只是前面多了 ``PDT_TREE_GUARD_PHASE=post``
        —— 两个阶段必须跑同一份测量代码，比对才有意义。

        它只在这个 builder 产出的文件里出现：其它子 agent 写自己的
        settings.json（``backend/subagent_config.py``），那里一个字符都没改。
        """
        return {
            "matcher": "Bash|Edit|Write",
            "hooks": [
                {
                    "type": "command",
                    "command": _TREE_GUARD_POST_COMMAND,
                }
            ],
        }

    def _scope_env(self) -> Dict[str, str]:
        """The environment variables that declare the write scope.

        Empty when there is no plan directory: the scope guards then find
        no scope and allow everything, exactly like before they existed.

        The names are the ones the guard script itself reads
        (``backend/verification_write_guard.py``):

        * ``PDT_VP_WRITE_SCOPE`` — the **plan directory**. The guard
          treats the value as the single writable root, so it has to be
          the directory itself and not a flag: a flag would leave the
          guard comparing write targets against ``<cwd>/1``.
        * ``PDT_VP_ID`` — which VP the refusals belong to.
        * ``PDT_VP_ROUND`` — which round they happened in; the guard
          derives the ``round-<n>/`` marker directory from it.

        ``PDT_VP_REFUSAL_DIR`` spells the second fact out for whoever
        reads the evidence back later.  The plan directory itself is
        named once, by ``PDT_VP_WRITE_SCOPE`` — a second copy of it
        under another name is a copy that can drift.
        """
        if self.plan_dir is None:
            return {}

        plan_dir = absolute_path(Path(self.plan_dir))
        try:
            round_no = int(self.round_no)
        except (TypeError, ValueError):
            round_no = 0
        refusal_dir = (
            plan_dir / "vp_write_scope" / "refusals" / f"round-{round_no}"
        )
        return {
            "PDT_VP_WRITE_SCOPE": str(plan_dir),
            "PDT_VP_ID": str(self.vp_id),
            "PDT_VP_ROUND": str(round_no),
            "PDT_VP_REFUSAL_DIR": str(refusal_dir),
        }

    def cleanup(self) -> None:
        """Remove the temp settings file and its private directory.

        Idempotent: a missing file is silently ignored. Calling this on
        an instance that was never built does nothing at all — it must
        not materialise a directory just to remove it.

        The class does not register an ``atexit`` hook — the caller is
        responsible for invoking ``cleanup()`` (typically in a
        ``finally`` block).
        """
        if not self._temp_file_path:
            return
        path = Path(self._temp_file_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
        # The 0700 directory was created for this one file; leaving it
        # behind would accumulate one empty dir per VP per round.
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def hooks(self) -> List[Dict]:
        """Return the in-memory list of PreToolUse hook entries.

        Each entry has the canonical shape::

            {
                "matcher": "Bash" | "Edit|Write",
                "hooks": [{"type": "command", "command": "..."}]
            }

        Useful for callers that want to inspect the rules without
        re-reading the temp file.
        """
        return [self._rule_to_hook(r) for r in _HOOK_RULES]

    @staticmethod
    def _rule_to_hook(rule: Dict[str, str]) -> Dict:
        """Convert an internal rule dict to the canonical hook entry."""
        return {
            "matcher": rule["matcher"],
            "hooks": [
                {
                    "type": "command",
                    "command": rule["command"],
                }
            ],
        }

    def _temp_path(self) -> str:
        """Absolute path of the temp settings file, created on first call.

        Memoised because ``build()`` and ``cleanup()`` both call this and
        must agree on the path — a fresh :func:`private_dir` per call
        would leave ``cleanup()`` hunting for a file ``build()`` never
        wrote.

        A private directory rather than a flat ``/tmp`` name: this
        payload carries hook commands rather than credentials, but the
        flat name handed the plan id and the VP id to every local
        account, and the file can outlive a crashed run.
        """
        if self._temp_file_path is None:
            self._temp_file_path = str(
                private_dir()
                / f"verif_settings_{self.vp_id}_{self.plan_id}.json"
            )
        return self._temp_file_path


# ---------------------------------------------------------------------------
# Verdict + parse_verdict — structured JSON contract for sub-agent output
# ---------------------------------------------------------------------------
#
# PRD Decision Point 4 (zero-tolerance): any malformed JSON, wrong
# top-level type, or missing required field is rejected with
# ``VerdictParseError``. The caller is expected to retry / log / report
# rather than silently absorb a bad payload.
#
# PRD Decision Point 3 (zero-tolerance for skipping): ``verdict="SKIPPED"``
# is coerced to ``"FAILED"`` at parse time. The downstream judgment
# pipeline never sees a SKIPPED value.
#
# 2026-10-22 (P2-2): the verdict text is normalised (stripped, folded
# to upper case) before the vocabulary check, and the canonical value
# is what the caller gets. A value that normalises to nothing known is
# rejected with ``VerdictParseError`` rather than passed through — the
# vocabulary is closed, and a status string the pipeline was never
# told to expect is a defect, not a verdict.


class VerdictParseError(ValueError):
    """Raised when an LLM output cannot be parsed into a ``Verdict``.

    Per PRD decision point 4 (zero-tolerance), any malformed JSON
    payload is rejected — the parse layer never silently coerces
    or drops fields. Per PRD decision point 3 (zero-tolerance for
    skipping), ``verdict="SKIPPED"`` is coerced to ``"FAILED"`` at
    parse time; downstream judgment therefore never has to handle
    a SKIPPED value.
    """


#: The failure class a verdict carries when nothing earned a different
#: one — the class that *counts*. Mirrors
#: ``verification.failure_triage.FailureClass.DELIVERABLE_DEFECT``.
#:
#: Spelled as a literal rather than imported, on purpose:
#: ``verification_subagent`` is imported by ``verification_agent``,
#: which the ``verification`` package imports in turn, so a
#: module-level import of that package here would be a circular
#: import. ``tests/unit/test_verdict_fields.py`` pins this literal
#: against the constant, so a drift is a test failure rather than a
#: silent inconsistency.
DEFAULT_FAILURE_CLASS = "deliverable_defect"

#: The source tag of a failure class that was assumed rather than
#: earned. Mirrors ``verification.failure_triage.SOURCE_DEFAULTED``
#: (same drift guard as :data:`DEFAULT_FAILURE_CLASS`).
DEFAULT_FAILURE_CLASS_SOURCE = "defaulted"

#: The failure class of a verdict whose ATTEMPT malfunctioned — a
#: verdict-parse garble, a watchdog kill, a transport error. The
#: deliverable is not implicated by any of those, so the verdict must
#: not be filed as a broken deliverable.
#:
#: Spelled as a literal for the same reason as
#: :data:`DEFAULT_FAILURE_CLASS` (the import would be circular), and
#: pinned against the constant by
#: ``tests/unit/test_verdict_parse_retry.py``.
INFRA_FAILURE_CLASS = "infra"

#: The source tag of a class this module derived by RULE rather than by
#: reading a claim off a payload. Mirrors
#: ``verification.failure_triage.SOURCE_DETERMINISTIC`` (same drift
#: guard as :data:`DEFAULT_FAILURE_CLASS`).
#:
#: It matters that this is not ``defaulted``: a defaulted claim is
#: degraded downstream to ``deliverable_defect``, so an attempt
#: malfunction would be filed as a broken deliverable — exactly the
#: confusion the four failure classes exist to prevent.
DETERMINISTIC_FAILURE_CLASS_SOURCE = "deterministic"


def _canonical_verdict_text(text: str) -> str:
    """The canonical spelling of a verdict: stripped, upper case.

    One definition, so the parse layer and the dataclass cannot drift
    apart on what "the same verdict" is. This is spelling only — what
    to *do* with a spelling (the ``SKIPPED`` coercion) is a policy
    decision and stays in :func:`parse_verdict`.
    """
    return text.strip().upper()


@dataclass
class Verdict:
    """Structured result from a single verification sub-agent invocation.

    Attributes:
        verdict: One of ``"PASSED"`` or ``"FAILED"``. ``"SKIPPED"``
            is never present — :func:`parse_verdict` coerces it to
            ``"FAILED"`` per PRD decision point 3.
        reasons: Free-form explanations supporting the verdict
            (e.g. "all assertions passed", "missing field X").
        evidence: Concrete observations the LLM cited (e.g. command
            output excerpts, file paths, line numbers).
        provider: Provider key (e.g. ``"vendor-a-pro"``,
            ``"vendor-b"``). Empty string if the LLM did not declare it.
        chosen_model: Concrete model id chosen for this VP
            (e.g. ``"Vendor A-M3"``, ``"B-PRO-5.1"``).
        model_complexity: Tier hint that produced the routing decision
            (e.g. ``"simple"`` / ``"medium"`` / ``"complex"``).
        failure_class: The class of the failure this verdict carries
            (e.g. ``"deliverable_defect"``). Empty for a passed
            verdict — a pass has no failure to classify.
        failure_class_source: Where the class came from
            (e.g. ``"defaulted"``). ``"defaulted"`` says the class was
            assumed, not earned.
        evidence_quote: The fragment of raw output the class is
            anchored in, quoted verbatim. Empty when there is none.
    """

    verdict: str
    reasons: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    provider: str = ""
    chosen_model: str = ""
    model_complexity: str = ""
    failure_class: str = DEFAULT_FAILURE_CLASS
    failure_class_source: str = DEFAULT_FAILURE_CLASS_SOURCE
    evidence_quote: str = ""

    def __post_init__(self) -> None:
        """Give a passed verdict the empty class it has no use for.

        A verdict that passed has no failure to classify, and the
        empty class is the convention the verdict record uses for that
        case — the same one
        :func:`verification.failure_triage.classify_verdict` returns
        for a passed verdict. The construction point may pass the
        empty class explicitly; this is the backstop for the ones
        that do not, so no construction point can end up claiming
        that a passing deliverable is broken.
        """
        if _canonical_verdict_text(str(self.verdict)) == "PASSED":
            self.failure_class = ""


def parse_verdict(subagent_output: Dict[str, Any]) -> str:
    """Validate a sub-agent's self-reported verdict and return it.

    2026-09-18 (VP judgment rework): this used to be a **cross-verify**
    layer. It took the ``pytest_exit_code`` and ``tests_run`` the
    sub-agent reported *about itself* and let them override its verdict.
    That is precisely what made the same artifact grade PASSED in one
    round and FAILED in the next — one run's VP-001 reported ``tests_run=0``
    in round 1 (→ FAILED) and "Exit code 0 confirms successful
    execution" in round 3 (→ PASSED), on an unchanged command. The
    failed-VP set therefore differed every round,
    ``same_failure_repeated`` never fired, and the loop always ran out
    to ``max_rounds``.

    A signal that one agent both produces and interprets is not a second
    source of truth. Each method now carries its own checkable basis
    instead:

      * ``api_test`` — executed and graded by
        :mod:`verification_api_runner`, no LLM involved;
      * ``code_review`` / ``ui_validation`` — must produce an artifact
        (citations / checkpoints) that :mod:`verification_evidence`
        verifies before a PASSED is honoured.

    So this function is back to doing one thing: validate the shape.
    It no longer takes exit-code arguments, and there is no
    ``cross_verify_status`` to report because there is no second
    signal to report *about*.

    ``SKIPPED`` is coerced to ``FAILED`` per PRD decision point 3
    (zero-tolerance for skipping).

    2026-10-22 (P2-2): the verdict text is put into the vocabulary
    before it is used. An LLM asked for ``"verdict": "PASSED"`` answers
    ``"passed"`` about as often as it answers ``"PASSED"``, and the
    whitespace around a value that was quoted out of a sentence is not
    information. The text is therefore stripped and folded to upper
    case, and the *canonical* value is what the caller gets back — no
    downstream consumer ever has to compare against a second spelling
    of the same verdict, and no verdict record ends up holding
    ``"passed"``.

    A value that does not normalise to a known verdict is rejected the
    same way a malformed payload is: with ``VerdictParseError``. That
    includes the empty string and a non-string value — from the
    caller's point of view they are the same situation, there is no
    verdict here to act on, so the only correct move is to retry.

    Raises:
        VerdictParseError: If ``subagent_output`` is not a dict, is
            missing the ``verdict`` field, is not a string, or holds a
            ``verdict`` value that does not normalise to one of
            ``{"PASSED", "FAILED", "SKIPPED"}``.
    """
    if not isinstance(subagent_output, dict):
        raise VerdictParseError(
            f"subagent_output must be a dict, got "
            f"{type(subagent_output).__name__}"
        )

    if "verdict" not in subagent_output:
        raise VerdictParseError("missing required field: 'verdict'")

    verdict = subagent_output["verdict"]
    if not isinstance(verdict, str):
        raise VerdictParseError(
            f"'verdict' must be a str, got {type(verdict).__name__}"
        )

    # Normalise before the vocabulary check, and normalise *only* the
    # case and the surrounding whitespace: " 通过" is a different
    # language, not a different spelling, and the vocabulary is
    # English and closed.
    normalized = _canonical_verdict_text(verdict)

    if normalized == "SKIPPED":
        return "FAILED"
    if normalized not in ("PASSED", "FAILED"):
        raise VerdictParseError(
            "verdict must be 'PASSED' or 'FAILED' (or 'SKIPPED' to be "
            f"coerced to FAILED), got {verdict!r}"
        )
    return normalized


def parse_verdict_to_dataclass(raw_output: str) -> "Verdict":
    """JSON-string entry point returning a :class:`Verdict` dataclass.

    Kept as a distinct function from :func:`parse_verdict` because the
    two have different jobs: ``parse_verdict`` validates the status
    field, while this wrapper additionally enforces the richer contract
    the dataclass consumers expect (``evidence`` must be present, per
    PRD decision point 4).

    Internally: JSON-decode ``raw_output``, validate the status via
    :func:`parse_verdict` (which normalises the status text and
    refuses anything outside the closed vocabulary), then carry
    ``reasons`` / ``evidence`` / ``provider`` / ``chosen_model`` /
    ``model_complexity`` / ``failure_class`` / ``failure_class_source``
    / ``evidence_quote`` through.

    The three classification fields are optional: a payload that
    carries none of them is not malformed, it simply gets the
    conservative defaults (the class that counts, marked as
    defaulted, with no quote). What the payload *does* claim about
    them reaches the dataclass unchanged.

    Raises:
        VerdictParseError: For malformed JSON, missing required
            fields, or invalid verdict values (see
            :func:`parse_verdict`).
    """
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError as e:
        raise VerdictParseError(f"Invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise VerdictParseError(
            f"top-level JSON must be an object, got {type(data).__name__}"
        )

    verdict_str = parse_verdict(data)

    raw_reasons = data.get("reasons", [])
    if not isinstance(raw_reasons, list):
        raise VerdictParseError(
            f"'reasons' must be a list, got {type(raw_reasons).__name__}"
        )

    if "evidence" not in data:
        raise VerdictParseError("missing required field: 'evidence'")
    evidence = data["evidence"]
    if not isinstance(evidence, list):
        raise VerdictParseError(
            f"'evidence' must be a list, got {type(evidence).__name__}"
        )

    # The failure classification the payload claims, and the quote it
    # anchors the claim in. A payload that claims nothing gets the
    # class that counts, marked as defaulted — an unearned claim must
    # not be able to file a broken deliverable as an environment
    # failure. A passed verdict has no failure to classify, so its
    # class is empty by convention.
    if verdict_str == "PASSED":
        failure_class = ""
    else:
        failure_class = str(
            data.get("failure_class") or DEFAULT_FAILURE_CLASS
        )
    failure_class_source = str(
        data.get("failure_class_source") or DEFAULT_FAILURE_CLASS_SOURCE
    )
    evidence_quote = str(data.get("evidence_quote") or "")

    return Verdict(
        verdict=verdict_str,
        reasons=list(raw_reasons),
        evidence=list(evidence),
        provider=str(data.get("provider", "")),
        chosen_model=str(data.get("chosen_model", "")),
        model_complexity=str(data.get("model_complexity", "")),
        failure_class=failure_class,
        failure_class_source=failure_class_source,
        evidence_quote=evidence_quote,
    )


# DETERMINISTIC-DISPATCH-BEGIN
# ---------------------------------------------------------------------------
# 确定性分派（2026-10-27）
# ---------------------------------------------------------------------------
#
# ``api_test`` / ``full_ci`` / ``e2e`` 的判据是**可机械执行的命令与断言**，
# 不是「必须问一次模型才知道答案」的问题。以前这些方法一律起 LLM 子会话，
# 于是同一条判据在两轮里能得出相反结论 —— 判定变成了子 agent 的自报，
# 而自报不可复跑。
#
# 所以「谁执行什么」是一条**显式的契约**，写在这张表里，而不是长在一条
# if 链里的惯例：审计时一眼能看出哪些方法不进 LLM。
#
# 表的**值**是执行该方法的 runner 模块名。两个 runner 都提供同名的
# ``validate_vp``，所以「这条验证点能不能跑」只有一个答案 —— 问那个将要
# 执行它的 runner。分派器自己不读验证点的字段（静态门禁：
# ``tests/static_gates/test_dispatch_reads_no_vp_fields.py``）：判定口径
# 住在 runner 里，分派器改了口径 runner 不会跟着改，两边就会打架。

#: verification_method → 执行它的确定性 runner 模块名。
#:
#: 显式常量，不是运行时拼出来的：这张表是「哪些方法不进 LLM」的契约，
#: 审计时一眼可读。``e2e`` 指向 CI runner，是因为 e2e 的判定同样是
#: 「跑一条命令、按退出码判」，机制与整仓门禁同源。
DETERMINISTIC_RUNNERS: Dict[str, str] = {
    "api_test": "verification_api_runner",
    "full_ci": "verification_ci_runner",
    "e2e": "verification_ci_runner",
}

#: 分派结论的两种模式。
#: ``deterministic`` —— 这条验证点由 runner 判定，**不开 LLM 会话**。
#: ``escalate_path`` —— 这条验证点判不了（或者根本没有对应的 runner），
#: 落回既有的 LLM 子会话。
DETERMINISTIC_MODE = "deterministic"
ESCALATE_PATH_MODE = "escalate_path"

#: LLM 路径的路由名。分派结论的第一项是它时，意思是「走既有的 LLM
#: 子会话」。
LLM_ROUTE = "llm"

#: 三态判定的取值。
OUTCOME_PASS = "PASS"
OUTCOME_FAIL = "FAIL"
OUTCOME_ESCALATE = "ESCALATE"

#: runner 模块名 → 执行函数名。两个 runner 的校验函数都叫 ``validate_vp``，
#: 所以只有执行函数需要一张表。
_RUNNER_EXECUTORS: Dict[str, str] = {
    "verification_api_runner": "run_api_verification",
    "verification_ci_runner": "run_ci_verification",
}


@dataclass(frozen=True)
class DeterministicOutcome:
    """一次确定性判定的结果 —— PASS / FAIL / ESCALATE 三态。

    Attributes:
        status: 三态之一（:data:`OUTCOME_PASS` / :data:`OUTCOME_FAIL` /
            :data:`OUTCOME_ESCALATE`）。
        route: 产生这个结果的路由 —— runner 模块名，或 :data:`LLM_ROUTE`。
        reasons: 人读的原因。ESCALATE 时是**触发原因**。
        stdout: 进程标准输出。API runner 没有进程，它的原始依据在
            ``evidence`` 里。
        stderr: 进程标准错误。
        exit_code: 进程退出码。
        evidence: 机器读的证据。ESCALATE 时是**触发这件事的证据**。
        verdict: runner 自己的裁决。ESCALATE 时是 ``None`` —— 升级不是
            「判失败」，是「换条路走」。
    """

    status: str
    route: str
    reasons: Tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    verdict: Optional[Dict[str, Any]] = None

    @property
    def is_deterministic(self) -> bool:
        """这次判定没有开过 LLM 会话（PASS 或 FAIL）。"""
        return self.status in (OUTCOME_PASS, OUTCOME_FAIL)

    @property
    def needs_llm(self) -> bool:
        """这次判定判不了，必须落回 LLM 路径。"""
        return self.status == OUTCOME_ESCALATE

    def to_verdict_dict(self) -> Dict[str, Any]:
        """runner 自己的裁决（``status`` / ``reasons`` / ``evidence``）。

        Raises:
            ValueError: 当这次判定是升级 —— 升级没有裁决，它要的是
                「换条路走」。
        """
        if self.verdict is None:
            raise ValueError(
                "an escalation carries no verdict — it asks for the LLM path"
            )
        return dict(self.verdict)


def _vp_method(vp: Any) -> str:
    """验证点声明的验证方法（没声明就当没有）。"""
    if not isinstance(vp, dict):
        return ""
    return str(vp.get("verification_method") or "").strip()


def _project_runner_path(name: str, project_dir: Optional[Path]) -> Optional[Path]:
    """被测树自己那份 runner（``<project_dir>/backend/<name>.py``）。

    没有这一层，「跑的是谁的代码」就由 ``sys.path`` 决定 —— 而
    ``backend/__init__.py`` 把**长驻进程自己那份** ``backend/`` 放在
    ``sys.path[0]``，于是判的是别的树。
    """
    if not project_dir:
        return None
    try:
        candidate = Path(project_dir) / "backend" / f"{name}.py"
    except TypeError:
        return None
    return candidate if candidate.is_file() else None


def _runner_module(name: str, project_dir: Optional[Path] = None) -> Any:
    """导入分派表点名的 runner 模块。

    优先用**被测树自己那份**（按路径加载，绝不从 ``sys.path`` 解析）：
    分派表点名的 runner 必须是被测的那个，否则这条验证点判的是另一棵树。
    被测树没有这份模块、或者加载失败时，回落到 harness 那份。

    模块名带路径摘要（``pdt_project_runner_<sha1[:12]>``），不占用
    ``<name>`` 这个名字 —— 否则会把 harness 的模块换掉，正是这个 loader
    要防的混淆。加载期间注册进 ``sys.modules``，加载完 ``pop`` 掉。
    绝不抛异常：加载不了就回落，不能因此掀翻整轮。
    """
    path = _project_runner_path(name, project_dir)
    if path is None:
        import importlib

        return importlib.import_module(name)

    try:
        import hashlib
        import importlib.util

        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
        module_name = f"pdt_project_runner_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
        return module
    except Exception:  # noqa: BLE001 — 回落，不是失败
        import importlib

        return importlib.import_module(name)


def _runner_issues(
    runner_name: str, vp: Any, project_dir: Optional[Path] = None,
) -> List[Any]:
    """runner 的校验器给出的全部问题（空列表 = 判得了）。

    校验器问的是「这条验证点你能不能执行」，答案只有它能给：分派器不替
    runner 做这个判断。抄一份规则进分派器，抄出来的那份一定会和真规则
    漂移，而漂移的方向是「看起来能跑」。

    2026-11-04：``project_dir`` 必须传进去。校验器里「入口引用的路径
    存不存在」这一段的前提就是 ``project_dir`` —— 不传，整段检查被跳过，
    一条写坏了的 ``ci_entry`` 在这条路径上被判成「可执行」。

    CI runner 的「入口必须是单条适配脚本调用」这条**形态契约**在执行期
    不启用（``require_single_entry=False``）—— 历史计划里的内联复合入口
    不重写，它仍然要能跑、仍然归确定性 runner 判。形态契约是**规划期**的
    规则（生成时就退回重写），不是执行期的准入条件。
    """
    module = _runner_module(runner_name, project_dir)
    validator = getattr(module, "validate_vp", None)
    if validator is None:
        return [f"{runner_name} 没有提供校验器，无法判定这条验证点"]
    kwargs: Dict[str, Any] = {}
    if project_dir is not None and _accepts_kwarg(validator, "project_dir"):
        kwargs["project_dir"] = Path(project_dir)
    if runner_name == "verification_ci_runner" and _accepts_kwarg(
        validator, "require_single_entry"
    ):
        kwargs["require_single_entry"] = False
    return list(validator(vp, **kwargs) or [])


def _accepts_kwarg(validator: Any, name: str) -> bool:
    """这个校验器接不接受 ``name`` 这个关键字参数？

    看签名，不靠试错：校验器自己抛 ``TypeError`` 时，签名检查不会把
    那个错误当成「签名不匹配」再跑一遍。
    """
    import inspect

    try:
        signature = inspect.signature(validator)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.name == name:
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _issue_evidence(issues: List[Any]) -> List[Any]:
    """把 runner 给的问题渲染成证据。"""
    out: List[Any] = []
    for issue in issues:
        to_dict = getattr(issue, "to_dict", None)
        out.append(to_dict() if callable(to_dict) else str(issue))
    return out


def _verdict_of(result: Any) -> Dict[str, Any]:
    """把 runner 的返回值取成裁决字典。"""
    to_dict = getattr(result, "to_verdict_dict", None)
    if callable(to_dict):
        payload = to_dict()
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _evidence_text(evidence: Dict[str, Any], *keys: str) -> str:
    """从证据里取一段文本（按候选键的顺序取第一个非空的）。"""
    for key in keys:
        value = evidence.get(key)
        if value:
            return str(value)
    return ""


def _blocking_issues(issues: List[Any]) -> List[Any]:
    """哪些问题让 runner **判不了**这条验证点（要落回 LLM 路径）。

    「入口引用的路径不存在」**不算**：门禁判得了 —— 它会跑起来，会跑出一个
    机器写的失败裁决（退出码、失败用例、artifact、失败身份）。落回 LLM 路径
    反而把它变成一句散文：下一轮就认不出这是同一个缺陷，指纹只能去哈希那句
    散文，于是同一个缺陷每轮都长得不一样（2026-11-04，VP-012）。

    这里读的是 runner **自己**给问题的分类（``issue.kind``），不是分派器自己
    抄一份规则 —— 抄出来的规则一定会和真规则漂移。
    """
    return [
        issue for issue in issues
        if getattr(issue, "kind", "") != "path_missing"
    ]


def resolve(vp: Any, project_dir: Optional[Path] = None) -> Tuple[str, str]:
    """决定一条验证点走哪条路：确定性 runner，还是 LLM 路径。

    Returns:
        ``(route, mode)``：

        * ``("verification_api_runner", "deterministic")`` —— 这条验证点
          由 runner 判定，跑它，**不要**开 LLM 会话。
        * ``("llm", "escalate_path")`` —— 这条验证点判不了，落回既有的
          LLM 子会话。

    「判得了」这件事**只**由那个将要执行它的 runner 的校验器回答。
    分派器自己不读验证点的字段：判定口径住在 runner 里，两边不会打架。

    2026-11-04：``project_dir`` 传进去，判与被判的都是**被测树那份**
    runner —— 校验用的代码必须和执行的代码来自同一棵树。
    """
    method = _vp_method(vp)
    runner_name = DETERMINISTIC_RUNNERS.get(method)
    if runner_name is None:
        return (LLM_ROUTE, ESCALATE_PATH_MODE)
    if _blocking_issues(_runner_issues(runner_name, vp, project_dir)):
        return (LLM_ROUTE, ESCALATE_PATH_MODE)
    return (runner_name, DETERMINISTIC_MODE)


def run_deterministic(
    vp: Any,
    *,
    project_dir: Optional[Path] = None,
    artifact_dir: Optional[Path] = None,
) -> DeterministicOutcome:
    """按分派表判定一条验证点，产出 PASS / FAIL / ESCALATE 三态结果。

    * **PASS** —— runner 跑完了，判过。裁决直接来自 runner
      （``outcome.verdict``），不是任何一次模型往返的自报。
    * **FAIL** —— runner 跑完了，没判过。裁决同样来自 runner，并带上
      进程的 stdout / stderr / 退出码：「没判过」必须能被人复现，只有
      一个 FAILED 是不够的。
    * **ESCALATE** —— 这条验证点判不了：校验器不通过（入口缺失、判据
      不齐…），或者 runner 没给出裁决。结果带上**触发原因**与**证据**，
      交给 LLM 路径接手。

    这条路径**不开任何 LLM 会话**。
    """
    method = _vp_method(vp)
    runner_name = DETERMINISTIC_RUNNERS.get(method)
    if runner_name is None:
        return DeterministicOutcome(
            status=OUTCOME_ESCALATE,
            route=LLM_ROUTE,
            reasons=(
                f"verification_method={method!r} 没有对应的确定性 runner",
            ),
            evidence={"method": method},
        )

    issues = _runner_issues(runner_name, vp, project_dir)
    if _blocking_issues(issues):
        return DeterministicOutcome(
            status=OUTCOME_ESCALATE,
            route=LLM_ROUTE,
            reasons=tuple(
                f"runner {runner_name} 判这条验证点不可执行：{issue}"
                for issue in issues
            ),
            evidence={
                "method": method,
                "runner": runner_name,
                "issues": _issue_evidence(issues),
            },
        )

    module = _runner_module(runner_name, project_dir)
    executor_name = _RUNNER_EXECUTORS.get(runner_name)
    executor = getattr(module, executor_name, None) if executor_name else None
    if executor is None:
        return DeterministicOutcome(
            status=OUTCOME_ESCALATE,
            route=LLM_ROUTE,
            reasons=(
                f"runner {runner_name} 没有提供执行入口，无法确定性判定",
            ),
            evidence={"method": method, "runner": runner_name},
        )

    kwargs: Dict[str, Any] = {}
    if artifact_dir is not None:
        kwargs["artifact_dir"] = artifact_dir
    if runner_name == "verification_ci_runner":
        if project_dir is None:
            return DeterministicOutcome(
                status=OUTCOME_ESCALATE,
                route=LLM_ROUTE,
                reasons=(
                    "整仓门禁要知道仓库在哪（project_dir），没有它判不了",
                ),
                evidence={"method": method, "runner": runner_name},
            )
        kwargs["project_dir"] = Path(project_dir)

    try:
        result = executor(vp, **kwargs)
    except Exception as exc:  # noqa: BLE001 - runner 崩了也是「判不了」
        detail = f"{type(exc).__name__}: {exc}"
        return DeterministicOutcome(
            status=OUTCOME_ESCALATE,
            route=LLM_ROUTE,
            reasons=(f"runner {runner_name} 执行时崩溃：{detail}",),
            evidence={
                "method": method,
                "runner": runner_name,
                "error": detail,
            },
        )

    verdict = _verdict_of(result)
    status_text = str((verdict or {}).get("status") or "")
    evidence = dict((verdict or {}).get("evidence") or {})
    reasons = tuple(str(reason) for reason in ((verdict or {}).get("reasons") or []))

    if status_text not in ("PASSED", "FAILED"):
        # runner 没给出裁决 —— 它没有判定任何东西，落回 LLM 路径。
        return DeterministicOutcome(
            status=OUTCOME_ESCALATE,
            route=LLM_ROUTE,
            reasons=reasons or (f"runner {runner_name} 没有给出裁决",),
            evidence={**evidence, "method": method, "runner": runner_name},
        )

    return DeterministicOutcome(
        status=OUTCOME_PASS if status_text == "PASSED" else OUTCOME_FAIL,
        route=runner_name,
        reasons=reasons,
        stdout=_evidence_text(evidence, "stdout", "stdout_tail"),
        stderr=_evidence_text(evidence, "stderr", "stderr_tail"),
        exit_code=evidence.get("exit_code"),
        evidence={**evidence, "method": method, "runner": runner_name},
        verdict=verdict,
    )
# DETERMINISTIC-DISPATCH-END
