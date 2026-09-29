"""Feishu card builders — pure functions, moved from ``tools/main.py``.

Why this exists
---------------
The original card builders lived in ``tools/main.py`` because the
bridge daemon owned the Feishu push. After moving push into the backend (event-driven via ``state_events.py``), the builders
need to live somewhere the backend can import — without dragging
the rest of the bridge (Telegram, polling loop, Bitable sync) into
the backend's import graph.

This module is a verbatim copy of the original ``_build_*_card``
functions with the ``self`` parameter dropped — they were always
pure. The audit comments explaining each section's design history
are preserved so future maintainers see the same context the
original authors saw.

The builders take the same dict shapes as the JSON the bridge used
to poll over HTTP — that contract is owned by ``server.py``'s
``/api/plan/{id}/summary``, ``/api/execution/{id}/progress``, and
``/api/verification/{id}/progress`` handlers, which are the data
source the notifier feeds into these builders.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``cards`` deliberately keeps the backend import graph out (see the
    # module docstring), so this is a contract, not a runtime dependency:
    # every renderer here takes a ``PlanStatus`` and reads nothing else.
    # ``from __future__ import annotations`` is on, so the annotations
    # below are never evaluated.
    from plan_status import PlanStatus


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: Origin marker the progress / summary endpoints stamp on task rows that
#: exist only in the ``plan_tasks`` ledger — the plan's static DAG
#: (``tasks.json``) has no such task.
DB_ORPHAN_TERMINAL_ORIGIN = "db_orphan_terminal"


def drop_terminal_db_orphans(
    summary: Optional[Dict[str, Any]],
    execution_progress: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Strip superseded DB-only tasks from a card's render inputs.

    2026-09-17. Rows that exist only in the ``plan_tasks`` ledger are tasks
    the plan has already superseded: the dispatcher refuses to schedule
    them (``agent._load_tasks`` Phase 2 supersedes terminal orphans) and
    the executor logs them as such at startup (``task_orphans_reconciled``).
    Rendering them as ❌ failures is misleading — a superseded task has
    been replaced, so it should not appear on the card at all. On one
    plan, three refiner-split *parents* surfaced as content-free
    "Task X (DB-only)" failures,
    and because the plan had been idle since the split, they were also the
    five most recent "完成活动" entries.

    They were also inflating the progress bar: ``79% (44/56)`` counted ten
    long-dead rows against a 46-task plan.

    Args:
        summary: ``/api/plan/{id}/summary`` payload (or None).
        execution_progress: ``/api/execution/{id}/progress`` payload (or None).

    Returns:
        ``(summary, execution_progress)`` copies with the orphan rows
        removed from the task list and their statuses decremented from the
        counts. Inputs are never mutated — the caller may hold the originals
        (the notifier persists raw snapshots for operator inspection).
    """
    # Which rows are we dropping? Read them from whichever payload carries
    # the task list; both endpoints stamp the same marker.
    dropped_ids: set = set()
    dropped_statuses: Dict[str, int] = {}
    tasks = (execution_progress or {}).get("tasks")
    if isinstance(tasks, list):
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if t.get("_origin") != DB_ORPHAN_TERMINAL_ORIGIN:
                continue
            tid = t.get("id")
            if tid:
                dropped_ids.add(str(tid))
            status = str(t.get("status") or "pending")
            dropped_statuses[status] = dropped_statuses.get(status, 0) + 1

    if not dropped_ids:
        return summary, execution_progress

    new_progress = None
    if isinstance(execution_progress, dict):
        new_progress = dict(execution_progress)
        new_progress["tasks"] = [
            t for t in (tasks or [])
            if not isinstance(t, dict) or t.get("_origin") != DB_ORPHAN_TERMINAL_ORIGIN
        ]
        counts = execution_progress.get("counts")
        if isinstance(counts, dict):
            new_counts = _decrement_counts(counts, dropped_statuses)
            new_progress["counts"] = new_counts

    new_summary = None
    if isinstance(summary, dict):
        new_summary = dict(summary)
        tasks_info = summary.get("tasks")
        if isinstance(tasks_info, dict):
            new_summary["tasks"] = _decrement_counts(tasks_info, dropped_statuses)

    return new_summary, new_progress


def _decrement_counts(
    counts: Dict[str, Any], dropped: Dict[str, int],
) -> Dict[str, Any]:
    """Subtract ``dropped`` statuses from a counts dict, clamped at zero.

    ``total`` moves by the full number of dropped rows; a status key that
    the payload does not carry is ignored rather than invented.
    """
    out = dict(counts)
    for status, n in dropped.items():
        if status in out and isinstance(out[status], int):
            out[status] = max(0, out[status] - n)
    if isinstance(out.get("total"), int):
        out["total"] = max(0, out["total"] - sum(dropped.values()))
    return out


# Verification phases — used to decide which card builder to invoke
# when the notifier receives a state event. (The notifier itself
# routes on event kind, not on phase — this set is kept here so the
# builders can be unit-tested without importing server.py.)
VERIFICATION_PHASES = frozenset({
    "verification",
    "verification_running",
    "verification_repairing",
    "verification_rerunning",
    # 2026-09-10: use the FULLY-qualified verification-* terminal
    # phase names so we do not collide with the generic execution
    # terminologies ``"failed"`` / ``"completed"`` (which are
    # distinct fields: ``plan_execution.current_phase`` carries
    # execution-only states). The previous entry ``"failed"`` here
    # caused any execution-failed plan to be routed through the
    # verification card path, which then rendered ``🔄 验证中``
    # because its header logic did not match any verification
    # sub-state. The bug: ``current_phase="failed"`` matched the
    # generic ``"failed"`` literal even though execution never
    # entered verification.
    "verification_passed",
    "verification_failed",
    "verification_loop_stopped",
})

# Terminal *verification* statuses — determines whether the
# verification card hides its running-view sections (overall VP
# progress, layer progress, in-flight VPs).
_VERIFICATION_TERMINAL_STATUSES = frozenset({
    "passed",
    "failed",
    "loop_stopped",
})

# 2026-09-11: public alias so cross-module
# routing code (notably ``feishu_notifier._rebuild_card``) can import
# without reaching into the underscore-prefixed private name. The
# two names point to the same frozenset — keep them in sync.
VERIFICATION_TERMINAL_STATUSES = _VERIFICATION_TERMINAL_STATUSES

# 2026-09-17 (schema v5): the routing ``stage`` vocabulary is gone from
# ``plan_routing`` — this table used to carry BOTH vocabularies side by
# side (``ready`` AND ``tasks_ready``, ``completed`` AND
# ``terminal_done``, …), which is how the card papered over the split
# between the two state columns. ``current_phase`` is the only
# workflow-state vocabulary now, so only its keys live here.
_PHASE_LABEL = {
    "interview": "📝 需求澄清",
    "prd_generation": "📄 PRD 生成",
    "prd_review": "📄 PRD 审阅",
    "arch_generation": "🏗 架构设计",
    "arch_review": "🏗 架构审阅",
    "test_generation": "🧪 测试设计",
    "test_review": "🧪 测试审阅",
    "tasks_generation": "📋 任务生成",
    "ready": "⏸ 就绪",
    # 2026-09-15: the queued gate — the plan is waiting
    # for the scheduler, which starts one plan at a time per workspace.
    "queued": "🕒 已排队",
    # Executing — single sub-state, but include the execution
    # phase hint so the user can distinguish running vs
    # failed vs completed inside the executing window.
    "executing": "▶️ 执行中",
    # Verification sub-machine — the user wants to see which
    # sub-phase the loop is in (initial verify, repairing,
    # rerunning).
    "verification": "🔍 初次验证",
    "verification_running": "🔍 初次验证",
    "verify_first_pass": "🔍 初次验证",
    "verification_repairing": "🔧 正在生成修复任务",
    "verification_rerunning": "🔁 重跑验证",
    "verify_recheck": "🔁 重跑验证",
    # Terminal vocabulary.
    "verification_passed": "✅ 验证通过",
    "verification_failed": "❌ 验证失败",
    "verification_loop_stopped": "⏹ 循环停止",
    "completed": "✅ 已完成",
    "failed": "❌ 失败",
    "stopped": "⏹ 已停止",
}

#: Phases that mean "the verification chain ended in failure and no
#: worker is running". This is the one-column spelling of the routing
#: value ``terminal_failed``, which projected from exactly these four.
#: Used by the stuck-state block so the operator is told what blocked
#: them and how to recover.
_TERMINAL_FAILURE_PHASES = frozenset({
    "failed",
    "stopped",
    "verification_failed",
    "verification_loop_stopped",
})

# Execution phase vocabulary — the labels for the "what is the plan
# doing right now" sub-line. Only rendered for phases that are not in
# the verification family; see ``_unified_phase_section``.
_EXECUTION_PHASE_LABEL = {
    "executing": "正在跑 tasks",
    "ready": "准备启动",
    # 2026-09-15: queued plans are auto-started by the scheduler.
    "queued": "已排队，等待调度",
    "failed": "执行失败",
    "completed": "执行已完成",
}


def _unified_phase_section(
    phase: Optional[str],
    verification_status: Optional[str],
    verification_round: Optional[int] = None,
    verification_max_rounds: Optional[int] = None,
    verification_stop_reason: Optional[str] = None,
    current_task: Optional[Tuple[str, str]] = None,
    current_vp: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build a single, normalized "where am I now" header line.

    Reads the plan's phase and renders one unified line that tells the
    operator:

    * Whether the plan is in the executing window or the
      verification window (top-level).
    * Inside the executing window: which execution sub-state
      (running / ready / failed / completed).
    * Inside the verification window: which sub-phase
      (initial verify / repairing / rerunning / passed / failed /
      loop_stopped).

    The shape is ``**📍 当前状态：<phase> →<sub-state>`` so a glance
    is enough to disambiguate "执行 vs 验证". Without this section
    the operator previously had to mentally compare the "任务执行
    状态" bar (which counts execution tasks) against the "验证状态"
    line (which describes the verification loop) to figure out which
    worker was active — exactly the split-state the user complained
    about.

    2026-09-17 (schema v5): this used to take TWO values — a routing
    ``stage`` and an ``execution_current_phase`` — because the two
    columns could disagree. One column now; the single ``phase``
    argument drives both the primary label and the sub-line.

    Returns ``[]`` when ``phase`` is absent (a brand-new plan before
    any state row was written).
    """
    primary = _PHASE_LABEL.get(phase or "", "")
    if not primary:
        return []

    parts: List[str] = [primary]

    sub_parts: List[str] = []
    if (phase or "").startswith("verification") or phase in {
        "verification", "verify_first_pass", "verify_recheck",
        "verification_running", "verification_repairing",
        "verification_rerunning",
    }:
        round_n = verification_round or 0
        round_max = verification_max_rounds or 0
        if round_max > 0:
            sub_parts.append(f"round {round_n}/{round_max}")
        # 2026-09-09: ``stop_reason='max_rounds_reached'``
        # means the auto-loop hit the cap and is waiting for the
        # operator to call ``/reset_rounds``. Surface that
        # in the unified line so the user does not have to read
        # the round N/M count to figure out the loop is
        # suspended.
        if verification_stop_reason == "max_rounds_reached":
            sub_parts.append("⏹ round 上限,等待 /reset_rounds")
        elif verification_status in ("running", "in_progress") and isinstance(
            current_vp, dict,
        ) and current_vp.get("id"):
            # 2026-09-14: name the VP that is being verified right now, same as
            # the executing window names the running task.
            vp_title = (current_vp.get("title") or "").strip()
            if vp_title:
                sub_parts.append(
                    f"🔍 正在验证 `[{current_vp['id']}]` {vp_title}"
                )
            else:
                sub_parts.append(f"🔍 正在验证 `[{current_vp['id']}]`")
        elif verification_status:
            status_label = {
                "pending": "⏳ 待启动",
                "running": "🔄 验证中",
                "passed": "✅ 通过",
                "failed": "❌ 失败",
                "loop_stopped": "⏹ 循环停止",
                "partial": "⚠️ 部分通过",
            }.get(verification_status, verification_status)
            sub_parts.append(status_label)
    else:
        exec_label = _EXECUTION_PHASE_LABEL.get(phase or "", "")
        # 2026-09-14: a bare "正在跑 tasks" label forces the
        # operator to hunt through the body for the in-progress
        # section. When the caller knows WHICH task is running,
        # name it right here in the unified line.
        if exec_label == "正在跑 tasks" and current_task:
            tid, ttitle = current_task
            if ttitle:
                exec_label = f"正在跑 `[{tid}]` {ttitle}"
            else:
                exec_label = f"正在跑 task `[{tid}]`"
        if exec_label:
            sub_parts.append(exec_label)

    line = "**📍 当前状态：**" + parts[0]
    if sub_parts:
        line += "　→　" + "　·　".join(sub_parts)

    return [{"tag": "div", "text": {"tag": "lark_md", "content": line}}]


def _in_progress_tasks_section(
    task_progress: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Render a "currently running tasks" block with title + id.

    2026-09-11: the previous version rendered
    ONLY ids (e.g. "🔄 正在执行：`RP-1`、`RP-2`") because the requirement
    from 2026-09-09 was "minimal — count and ids". But the user
    reported this is too terse: when only the title is what they
    recognise, the id-only line is unreadable. Each in_progress task
    gets its own line with `[id] title`, and only truncate the title
    if it overflows the 80-char display budget.
    Returns ``[]`` when there are no in-progress tasks so the card
    stays quiet during quiet phases (e.g. between verification
    rounds when the executor is idle).
    """
    if not task_progress:
        return []
    in_progress = [
        t for t in task_progress
        if isinstance(t, dict) and t.get("status") == "in_progress"
        and t.get("id")
    ]
    if not in_progress:
        return []
    n = len(in_progress)
    if n > 1:
        heading = f"🔄 **正在执行（{n} 个并行）：**"
    else:
        heading = "🔄 **正在执行：**"
    lines = [heading]
    for t in in_progress:
        tid = t.get("id", "")
        title = (t.get("title") or "").strip()
        # Truncate very long titles so the card stays compact
        # (Feishu has a 4 KB element cap and a 30-line/element limit).
        if len(title) > 80:
            title = title[:77] + "…"
        if title:
            lines.append(f"- `[{tid}]` {title}")
        else:
            # Fallback for tasks without a title field (e.g. db_orphan
            # rows) — show the id only so the section is never empty.
            lines.append(f"- `{tid}`")
    return [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        {"tag": "hr"},
    ]


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _format_now() -> str:
    """Return a ``MM-DD HH:MM:SS`` display string; frozen by env.

    The freeze hook (``FEISHU_BRIDGE_FROZEN_TIMESTAMP``) is preserved
    from the bridge for parity with the existing log-grep workflows
    that depend on the format being deterministic during replay.
    """
    frozen = os.environ.get("FEISHU_BRIDGE_FROZEN_TIMESTAMP")
    if frozen:
        return frozen
    from datetime import datetime
    return datetime.now().strftime("%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Shared rendering helpers
# ---------------------------------------------------------------------------


def _task_summary_section(tasks_info: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Render task progress bar + non-zero counts as a list of card elements.

    Returns ``[]`` when ``tasks_info`` has no tasks (total == 0).
    Pure function — same input always produces the same output, so
    the notifier's fingerprint dedup can compare cards safely.
    """
    if not tasks_info:
        return []
    total = tasks_info.get("total", 0) or 0
    if total <= 0:
        return []
    completed = tasks_info.get("completed", 0) or 0
    failed = tasks_info.get("failed", 0) or 0
    skipped = tasks_info.get("skipped", 0) or 0
    in_progress = tasks_info.get("in_progress", 0) or 0
    pending = tasks_info.get("pending", 0) or 0
    pct = round(completed * 100 / total)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    line = f"**📋 任务执行状态：**{bar} {pct}% ({completed}/{total})"
    counts = [
        f"✅{completed}", f"⏳{in_progress}", f"📋{pending}",
        f"❌{failed}", f"⏭{skipped}",
    ]
    non_zero = [c for c in counts if not c.endswith("0")]
    if non_zero:
        line += "　" + "　".join(non_zero)
    return [
        {"tag": "div", "text": {"tag": "lark_md", "content": line}},
        {"tag": "hr"},
    ]


def _extract_failure_context(description: Optional[str], max_chars: int = 120) -> str:
    """Return a short snippet of ``description`` for the failure card.

    The DB stores ``failure_reason=None`` for tasks that failed inside
    the executor but did not surface a structured reason — typically
    the LLM died before writing one, or the task was force-failed
    during an R3-1 race condition. The card previously showed
    ``未知原因`` and the operator had no way to know what the task
    was supposed to do. Fall back to the first lines of
    ``description`` so the card shows "任务背景: …" instead.

    Strategy:
      * If description contains a markdown heading ``## 背景`` /
        ``## 目标`` / ``## 上下文`` / ``## 目的``, return the
        paragraph that follows that heading (truncated to
        ``max_chars``).
      * Otherwise return the first ``max_chars`` of description
        (with leading bullets stripped so the snippet reads like a
        sentence instead of dumping the whole spec).
    """
    if not description:
        return ""
    text = description.strip()
    if not text:
        return ""
    import re
    m = re.search(
        r"^#{1,4}\s*(?:背景|目标|上下文|目的)[^\n]*\n+(.+?)(?=\n#{1,4}\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    snippet = (m.group(1) if m else text).strip()
    snippet = re.sub(r"^[-*]\s+", "", snippet, flags=re.MULTILINE)
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rstrip() + "…"
    return snippet


def _blocked_tasks_section(progress: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Render pending tasks that are blocked by upstream failures.

    2026-09-10: the card title shows
    ``⏸ 暂停（上游阻塞）`` but the body never tells the operator
    *which* tasks are blocked and *why*. Without this section the
    operator has to ask "what's actually stuck?" — exactly the
    ambiguity the card was supposed to remove.

    Algorithm:
      1. Collect pending tasks from ``progress.tasks``.
      2. For each pending task, inspect its ``depends_on`` list:
         - if any dep has status ``failed`` → "被 [id] 阻塞(失败)"
         - else if any dep has status ``pending`` or
           ``in_progress`` → "等待 [id, ...]" (transitive cascade
           is NOT expanded — single-level is enough to surface the
           cascade to the operator)
         - else (all deps completed/skipped/superseded) → skip
      3. Render as ``🚧 被阻塞的待执行任务`` + bullet list.

    Returns ``[]`` when there are no blocked tasks — pure function.
    """
    if not progress:
        return []
    prog_tasks = progress.get("tasks") or []
    if not prog_tasks:
        return []
    # Build a status lookup so we can resolve dependency IDs to
    # statuses without re-scanning the list for each pending task.
    status_by_id: Dict[str, str] = {}
    for t in prog_tasks:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if tid:
            status_by_id[str(tid)] = (t.get("status") or "").lower()
    blocked_lines: List[str] = []
    for t in prog_tasks:
        if not isinstance(t, dict):
            continue
        if (t.get("status") or "").lower() != "pending":
            continue
        deps = t.get("depends_on") or []
        if not deps:
            # Pending with no deps is schedulable, not blocked — skip.
            continue
        failed_deps: List[str] = []
        waiting_deps: List[str] = []
        for dep_id in deps:
            dep_status = status_by_id.get(str(dep_id), "")
            if dep_status == "failed":
                failed_deps.append(str(dep_id))
            elif dep_status in ("pending", "in_progress"):
                waiting_deps.append(str(dep_id))
        if not failed_deps and not waiting_deps:
            # All deps are completed / skipped / superseded — not blocked.
            continue
        tid = t.get("id", "?")
        title = t.get("title", "未知任务")
        if failed_deps:
            block_reason = f"被 [{', '.join(f'`{d}`' for d in failed_deps)}] 阻塞（失败）"
        else:
            block_reason = f"等待 [{', '.join(f'`{d}`' for d in waiting_deps)}]"
        blocked_lines.append(f"• `{tid}` {title}　↳ {block_reason}")
    if not blocked_lines:
        return []
    return [
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**🚧 被阻塞的待执行任务：**"}},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(blocked_lines)},
        },
    ]


def _vps_status_map_from_progress(progress: Dict[str, Any]) -> Dict[str, str]:
    """Derive a ``{vp_id: status}`` map from the per-list progress fields."""
    status_map: Dict[str, str] = {}

    def _id(entry: Any) -> Optional[str]:
        if isinstance(entry, dict):
            return entry.get("id")
        return entry

    for entry in (progress.get("completed_vps") or []):
        vid = _id(entry)
        if vid:
            status_map[vid] = "completed"
    for entry in (progress.get("failed_vps") or []):
        vid = _id(entry)
        if vid:
            status_map[vid] = "failed"
    for entry in (progress.get("skipped_vps") or []):
        vid = _id(entry)
        if vid:
            status_map[vid] = "skipped"
    for entry in (progress.get("pending_vps") or []):
        vid = _id(entry)
        if vid:
            status_map[vid] = "pending"

    current_vp = progress.get("current_vp")
    if isinstance(current_vp, dict):
        cid = current_vp.get("id")
        if cid:
            status_map[cid] = "in_progress"
    return status_map


def _compute_overall_vp_progress(
    progress: Dict[str, Any],
) -> Optional[Dict[str, int]]:
    """Compute overall VP progress from ``vps`` (preferred) or ``counts``.

    The verification progress API returns BOTH ``vps`` (a list of VP
    objects with status) AND ``counts`` (an aggregate dict). In
    practice the ``vps`` list is often empty / partial (only the most
    recent VPs are returned to keep the payload small), but ``counts``
    is always populated for the plan-level rollup. We prefer ``vps``
    when populated and fall back to ``counts`` when the list is empty
    so terminal plans (where the vps list is often empty after the
    verification report is flushed) still get a progress bar.

    Returns a dict with counts by status plus ``total``, ``done``
    (completed + failed + skipped), and ``pct``. Returns ``None`` when
    neither ``vps`` nor ``counts`` carries meaningful data.
    """
    vps = progress.get("vps") or []
    counts_obj = progress.get("counts") or {}

    if vps:
        counts: Dict[str, int] = {
            "completed": 0,
            "in_progress": 0,
            "failed": 0,
            "skipped": 0,
            "pending": 0,
        }
        for v in vps:
            if not isinstance(v, dict):
                continue
            s = (v.get("status") or "pending").lower()
            if s == "running":
                s = "in_progress"
            if s in counts:
                counts[s] += 1
        total = sum(counts.values())
        if total == 0:
            return None
    else:
        # Fall back to the counts aggregate. Normalize the keys to
        # the same vocabulary the vps branch uses.
        counts = {
            "completed": int(counts_obj.get("completed", 0) or 0),
            "in_progress": int(counts_obj.get("in_progress", 0) or 0),
            "failed": int(counts_obj.get("failed", 0) or 0),
            "skipped": int(counts_obj.get("skipped", 0) or 0),
            "pending": int(counts_obj.get("pending", 0) or 0),
        }
        total = int(counts_obj.get("total", 0) or 0) or sum(counts.values())
        if total == 0:
            return None
        counts["total"] = total

    # 2026-09-11: the operator's mental model is
    # "完成 = 通过的 VP 数" — failed VPs are NOT "done", they are a
    # separate count the operator needs to see (the bar's ❌N badge).
    # Previously ``done = completed + failed + skipped`` so a plan with
    # 41 passed + 1 failed showed "100% (42/42)" which is misleading
    # — only 41/42 VPs actually passed. Use ``completed`` only so the
    # bar reflects the pass rate.
    done = counts["completed"]
    pct = int(done / total * 100) if total > 0 else 0
    return {**counts, "total": total, "done": done, "pct": pct}


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def _resolve_header(
    status: PlanStatus, tasks_info: Dict[str, Any]
) -> Dict[str, str]:
    """Pick a single header (title + template color) from the data snapshot.

    ``tasks_info`` is passed in rather than read off ``status`` on
    purpose. The counts decide branches in here (``total`` for the
    empty-plan case, ``completed + failed + skipped`` against ``total``
    for the terminal one) and the *body* decides its own wording from
    ``build_card``'s ``tasks_info`` — which is built from the summary,
    falling back to the status. Reading a second, independent copy here
    is how one card came to report two different totals: the header said
    "⚠️ 已完成（1 个失败）" in green off ``status.tasks`` while the body
    printed ``86% (19/22)`` off the per-task list, for a plan that had
    never run verification and still owed two tasks. One snapshot, taken
    once, handed to both.

    2026-09-11 (single-card design): the card is ALWAYS one full
    message overwritten to Feishu, so the header must
    be ONE coherent decision driven by the data, not by which builder
    the caller picked. Priority order (matches what operators read):

      1. Empty plan (total == 0): smoke-test / fixture plan that
         never executed tasks → "⚪ 空 plan" regardless of verification
         status. A verification-status="passed" on a 0-task plan is
         only meaningful as a smoke-test verdict; showing the verbose
         "✅ 验证通过 · {plan_id}" header hides that.
      2. Verification terminal (passed/failed/loop_stopped) →
         "✅/❌ 验证..." — the strongest signal of plan outcome.
      3. Verification active (running/repairing/rerunning/...) →
         "🔧 正在生成修复任务" / "🔁 重跑验证" / "🔍 初次验证" / "🔄 验证中".
      4. Execution terminal (completed/failed/stopped) →
         "✅ 已完成" / "⚠️ 已完成(N 个失败)" / "⏸ 暂停...".
      5. Mid-plan (interview, prd_review, executing, ...) → base phase label.

    Returns ``{"title": str, "color": str}`` — the caller wraps the
    title in the Feishu header JSON. Pure function — no I/O.

    2026-09-23: the seven loose kwargs became one ``PlanStatus``. They
    were assembled by each caller from whatever it had fetched, which is
    how the header came to disagree with the database it was describing
    (`plan_status` documents the incident). Everything read here now
    comes from one snapshot, taken once.
    """
    plan_id = status.plan_id
    phase = status.phase or ""
    verification_status = (status.verification_status or "").lower()
    verification_round = status.verification_round
    max_rounds = status.verification_max_rounds
    stop_reason = status.verification_stop_reason

    # 1. Empty plan — only treat as empty-plan (smoke-test / fixture)
    # when the plan is in a TERMINAL state. Mid-plan with zero tasks
    # is normal (e.g. interview, prd_review haven't generated tasks
    # yet) and must show the base phase label, NOT "⚪ 空 plan".
    total = tasks_info.get("total", 0) or 0
    if total == 0 and phase in ("failed", "completed"):
        if phase == "failed":
            return {"title": f"❌ 失败 · {plan_id}", "color": "red"}
        # completed + (verification passed or not — wrapper may not
        # have verification data) → smoke-test passed.
        return {"title": f"⚪ 空 plan · {plan_id}", "color": "green"}

    # 2. Verification terminal — strongest signal of plan outcome.
    # 2026-09-11 (header staleness): only
    # honor the verification verdict when execution is NOT currently
    # in flight. Previously a plan that had been verified once (so
    # ``verification_status`` was cached as "passed" in
    # ``state.db``) would render "✅ 验证通过" forever even after a
    # new execution round began — the verification verdict is from
    # a PRIOR round and does not reflect the live executor.
    #
    # 2026-09-23: this used to be re-derived here from task counts (any
    # ``in_progress`` task, or any pending task with the plan in an
    # execution-capable phase). That proxy cannot tell "a repair round is
    # running" from "the server died with a task stuck at
    # ``in_progress``" — the ambiguity that forced the card fix of
    # 2026-09-23 to be narrowed to a single branch. It now comes from the
    # snapshot, which reads the same record the watchdog and
    # ``/api/system/active`` read.
    execution_in_flight = status.execution_in_flight
    if verification_status == "passed" and not execution_in_flight:
        return {"title": f"✅ 验证通过 · {plan_id}", "color": "green"}
    # 2026-09-19: the "⏹ 循环停止" check moved ABOVE the generic
    # failed/loop_stopped branch. It used to sit below it, where it was
    # unreachable — a max-rounds stop always reports
    # ``verification_status == "loop_stopped"``, so the generic branch
    # always won and this label never rendered. It only became visible
    # once ``stop_reason`` actually reached the card (see
    # ``/api/verification/{id}/progress``); reaching the more specific
    # label is the point of carrying the reason at all.
    if stop_reason == "max_rounds_reached" and not execution_in_flight:
        return {
            "title": f"⏹ 循环停止(round 已达上限)· {plan_id}",
            "color": "red",
        }
    if verification_status in ("failed", "loop_stopped") and not execution_in_flight:
        return {"title": f"❌ 验证失败 · {plan_id}", "color": "red"}
    if verification_status == "partial" and not execution_in_flight:
        return {"title": f"⚠️ 部分通过 · {plan_id}", "color": "orange"}

    # 3. Verification active — header shows the active sub-stage.
    _effective_max = max_rounds or 0
    _round_suffix = (
        f" round {verification_round}/{_effective_max}"
        if verification_round and _effective_max
        else ""
    )
    if phase == "verification_repairing":
        return {
            "title": f"🔧 正在生成修复任务 · {plan_id}{_round_suffix}",
            "color": "orange",
        }
    if phase == "verification_rerunning":
        return {
            "title": f"🔁 重跑验证 · {plan_id}{_round_suffix}",
            "color": "blue",
        }
    if phase in ("verify_first_pass", "verification"):
        return {
            "title": f"🔍 初次验证 · {plan_id}{_round_suffix}",
            "color": "blue",
        }
    # Verification sub-stage unknown / unset (e.g. legacy callers that
    # pass current_phase=None with verification_status="running").
    # Default to the generic "🔄 验证中" header so the card still
    # carries a verification verdict rather than falling through to a
    # base phase label that says nothing about verification.
    # Only trigger this fallback when verification_status indicates
    # an active or terminal sub-machine — a bare "pending" status with
    # no current_phase must NOT hijack the header (it would mask
    # mid-plan states like "executing").
    if phase in VERIFICATION_PHASES or verification_status in (
        "running", "in_progress",
    ):
        return {
            "title": f"🔄 验证中 · {plan_id}{_round_suffix}",
            "color": "blue",
        }

    # 4. Empty plan in mid-plan state (e.g. interview) — show base
    # phase label, NOT "⚪ 空 plan". This is the regression case
    # pinned by ``test_total_zero_interview_shows_base_phase_label``.
    if total == 0:
        # Fall through to base phase label below.
        pass

    # 5. Execution terminal — decide by counts and stopped-early.
    #
    # 2026-09-17 (schema v5): the condition used to be
    # ``phase in ("failed", "completed") or stage == "terminal_failed"``
    # — the second clause existed because the routing column could read
    # ``terminal_failed`` while ``current_phase`` still said
    # ``verification_running`` (the split-brain this collapse removes).
    # ``terminal_failed`` projected from four phases, so the faithful
    # one-column translation is that four-element set spelled out.
    # ``verification_passed`` is deliberately NOT included: it projected
    # from ``terminal_done``, which this branch never matched, and it has
    # its own base label ("✅ 验证通过").
    if phase in (
        "failed", "completed", "stopped",
        "verification_failed", "verification_loop_stopped",
    ):
        completed_n = tasks_info.get("completed", 0) or 0
        failed_n = tasks_info.get("failed", 0) or 0
        in_progress_n = tasks_info.get("in_progress", 0) or 0
        pending_n = tasks_info.get("pending", 0) or 0
        finished = completed_n + failed_n + (tasks_info.get("skipped", 0) or 0)
        if finished >= total:
            if failed_n == 0:
                return {"title": f"✅ 已完成 · {plan_id}", "color": "green"}
            return {
                "title": f"⚠️ 已完成（{failed_n} 个失败） · {plan_id}",
                "color": "green",
            }
        # 2026-09-23: a live execution outranks a terminal phase — but
        # only on THIS branch. Every branch above already consults
        # ``execution_in_flight``; this one did not, so a plan whose
        # phase had been stamped ``completed`` while its repair round was
        # starting rendered "⏸ 暂停（上游阻塞）" next to the ⏳ for that
        # very round — a live plan showed exactly that split state.
        #
        # The guard applies to the whole branch, ``in_progress``
        # included. The first cut of this fix had to stop short of
        # that: ``execution_in_flight`` was a task-count proxy, so it
        # reduced to ``in_progress > 0`` here, and guarding on it made
        # "⏸ 暂停（执行中断）" unreachable — a label that covers the real
        # case "the server died with tasks stuck at ``in_progress``".
        # With liveness read from the execution record instead of
        # guessed from counts the two cases are finally separable: a
        # dead executor is not in flight and keeps the interrupted
        # label.
        if execution_in_flight:
            return {
                "title": f"🔧 修复执行中 · {plan_id}{_round_suffix}",
                "color": "blue",
            }
        # Pending or in_progress remain → dispatcher exited before
        # draining the DAG.
        if pending_n > 0:
            return {"title": f"⏸ 暂停（上游阻塞） · {plan_id}", "color": "orange"}
        if in_progress_n > 0:
            return {"title": f"⏸ 暂停（执行中断） · {plan_id}", "color": "orange"}
        return {"title": f"❌ 失败 · {plan_id}", "color": "red"}

    # 6. Mid-plan — base phase label.
    base = {
        "new": "新建", "interview": "需求澄清", "interview_complete": "需求完成",
        "prd_generation": "PRD生成", "prd_review": "PRD审阅", "prd_approved": "PRD通过",
        "arch_generation": "架构设计", "arch_review": "架构审阅", "arch_approved": "架构通过",
        "test_generation": "测试设计", "test_review": "测试审阅", "test_approved": "测试通过",
        "tasks_generation": "任务生成", "ready": "就绪", "queued": "已排队",
        "executing": "执行中",
    }.get(phase, phase)
    # Color: red if any failed task; orange if in-flight; else default blue.
    failed = tasks_info.get("failed", 0) or 0
    in_progress = tasks_info.get("in_progress", 0) or 0
    color = "blue"
    if phase == "ready":
        color = "green"
    elif phase == "completed":
        color = "green"
    elif phase == "failed":
        color = "red"
    elif failed > 0:
        color = "red"
    elif in_progress > 0:
        color = "orange"
    return {"title": f"{base} · {plan_id}", "color": color}


def _verification_sections(
    progress: Optional[Dict[str, Any]],
    is_terminal: bool,
    status: PlanStatus = None,
) -> List[Dict[str, Any]]:
    """Render the verification sub-sections (grouped under the "🔍
    verification 状态" header) from ``/api/verification/{id}/progress``.

    Returns ``[]`` when ``progress`` is None — never rendered for
    plans that have never run verification. The caller decides whether
    to render the group header; this helper only emits the body
    sections under it.

    ``status`` is the plan snapshot (``plan_status.PlanStatus``)
   — used to render the operator-action guidance block when the plan
    is in a stuck terminal state (terminal_failed, max_rounds_reached).
    Without this block operators see "❌ 验证失败" but no hint what to
    do next — the card must carry the blocking reason so the operator
    knows what to instruct.
    """
    if not progress:
        return []
    elements: List[Dict[str, Any]] = []

    # 2026-09-11 plan v12: surface the verification round (1/N, 2/N, …)
    # at the top of the verification section so the operator knows which
    # repair-cycle they're on. Before this the round number only
    # appeared inside the header; a separate per-section line makes it
    # impossible to miss when the operator is scanning the body for
    # current activity.
    # 2026-09-12 plan v14 follow-up: the round line also renders when
    # the section is HISTORICAL (is_terminal=True) — operators need to
    # see "round 3/3, status = failed" to understand why the executor
    # is now running RP-* tasks. The label changes to "📋 历史轮次"
    # so the distinction from a live "🔄 当前轮次" is obvious.
    # (The repair_tasks list that used to be rendered alongside this
    # line moved out of the card entirely — see the note below.)
    v_round = progress.get("verification_round") or 0
    v_max_rounds = progress.get("max_rounds") or 0
    v_status_str = (progress.get("verification_status") or "").lower()
    if v_round and v_max_rounds and not is_terminal:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🔄 当前轮次：round {v_round}/{v_max_rounds}**"
                            f"（status = `{v_status_str or 'pending'}`）"}}
        )
        elements.append({"tag": "hr"})
    elif v_round and not is_terminal:
        # Max rounds unknown — fall back to just round N.
        round_line = f"**🔄 当前轮次：round {v_round}**"
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": round_line}}
        )
        elements.append({"tag": "hr"})
    elif v_round and is_terminal:
        # 2026-09-12 plan v14: historical round summary so the
        # operator can see which round concluded with the current
        # verification status. Without this line the historical
        # section was empty for plans where the live endpoint's
        # vps / counts / repair_tasks lists had been flushed.
        if v_max_rounds:
            round_line = (
                f"**📋 历史轮次：round {v_round}/{v_max_rounds}**"
                f"（status = `{v_status_str or 'unknown'}`）"
            )
        else:
            round_line = (
                f"**📋 历史轮次：round {v_round}**"
                f"（status = `{v_status_str or 'unknown'}`）"
            )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": round_line}}
        )
        elements.append({"tag": "hr"})

    # 2026-09-14: surface the
    # VP that is being verified RIGHT NOW at the very top of the
    # verification section — id, title, method and start time — so
    # the operator never has to guess what the progress bar is
    # measuring. ``current_vp`` is populated by the progress endpoint
    # whenever a VP is mid-flight (re-running VPs included).
    current_vp = progress.get("current_vp")
    if isinstance(current_vp, dict) and current_vp.get("id"):
        cv_title = (current_vp.get("title") or "").strip()
        cv_started = (current_vp.get("started_at") or "").strip()
        cv_line = f"**🔄 正在验证：`[{current_vp['id']}]`** {cv_title}"
        if cv_started:
            cv_line += f"（开始于 {cv_started}）"
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": cv_line}}
        )
        elements.append({"tag": "hr"})

    # 2026-09-14: parents the repair-phase judge split
    # into sub-VPs. Rendered ABOVE the repair-task list and even when
    # that list is empty — a pure-split round produces no repair tasks,
    # so without this block the round reads as a silent no-op on the
    # card, with nothing saying which VPs were split or why.
    vp_splits = progress.get("vp_splits") or []
    if isinstance(vp_splits, list) and vp_splits:
        sp_lines: List[str] = [
            f"**🔀 已拆分 VP ({len(vp_splits)})：**"
        ]
        for sp in vp_splits:
            if not isinstance(sp, dict):
                continue
            parent = sp.get("vp_id", "")
            children = [
                str(c) for c in (sp.get("child_vp_ids") or []) if c
            ]
            child_txt = ", ".join(f"`{c}`" for c in children[:6])
            if len(children) > 6:
                child_txt += f" … 共 {len(children)} 个"
            ptitle = sp.get("title", "")
            hint = (sp.get("hint") or "").strip()
            reason = (sp.get("reason") or "").strip()
            detail = "：".join(p for p in (hint, reason) if p)
            line = f"- `{parent}`"
            if ptitle:
                line += f" {ptitle}"
            line += f" → {child_txt or '（无子 VP）'}"
            if detail:
                line += f"（{detail}）"
            sp_lines.append(line)
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "\n".join(sp_lines)}}
        )
        elements.append({"tag": "hr"})

    # 2026-09-18: the "🔧 反思生成修复任务" section used
    # to be rendered here, listing every repair task with its title,
    # test command and failure reason. It is gone on purpose — a
    # generated repair task joins the plan's task list at generation
    # time (``repair_generator.append_to_tasks`` → ``state.db``
    # ``plan_tasks``), so it
    # reaches the operator through the execution area's
    # "📋 等待中的任务" list like any other pending task. Rendering it a
    # second time under verification only made the card longer (up to 3
    # lines per task) without saying anything the pending list did not
    # already say.
    #
    # The ``repair_tasks`` payload itself is unchanged and still feeds
    # ``GET /api/verification/{id}/repair_tasks`` plus the stuck-state
    # guidance block below — this removed a duplicate rendering, not
    # the data.

    # 2026-09-11: the overall VP progress
    # bar is rendered FIRST in the verification section, mirroring
    # the execution task summary bar's position at the top of the
    # execution section. Symmetric layout: both bars sit directly
    # under their section header so the operator reads top-to-bottom:
    # section header → progress bar → detail sections (in-progress /
    # layer / recent VPs / failed VPs). Terminal plans no longer
    # render the redundant "📈 总结" line — the bar already shows
    # completed/failed/skipped counts in the non-zero suffix.

    overall = _compute_overall_vp_progress(progress)
    if overall is not None and overall["total"] > 0:
        pct = overall["pct"]
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        status_bits = [
            f"✅{overall['completed']}",
            f"⏳{overall['in_progress']}",
            f"❌{overall['failed']}",
            f"⏭{overall['skipped']}",
            f"📋{overall['pending']}",
        ]
        # Suppress trailing zeros so the bar reads as
        # "████████░ 97% (41/42) ✅41 ❌1" instead of
        # "... ✅41 ❌1 ⏭0 📋0".
        non_zero = [b for b in status_bits if not b.endswith("0")]
        line = (
            f"**📈 总进度：**{bar} {pct}% "
            f"({overall['done']}/{overall['total']})　"
            f"{'　'.join(non_zero)}"
        )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": line}}
        )
        elements.append({"tag": "hr"})

    if not is_terminal:
        layer_summaries = progress.get("layer_summaries") or {}
        layer_lines: List[str] = []
        for layer_key in ("L1", "L2", "L3"):
            ls = layer_summaries.get(layer_key)
            if not ls:
                continue
            ltotal = ls.get("total", 0) or 0
            lcompleted = ls.get("completed", 0) or 0
            lfailed = ls.get("failed", 0) or 0
            lskipped = ls.get("skipped", 0) or 0
            lin_progress = ls.get("in_progress", 0) or 0
            if ltotal > 0:
                pct = int(lcompleted / ltotal * 100)
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            else:
                bar = "░" * 10
                pct = 0
            bits = [f"✅{lcompleted}", f"❌{lfailed}", f"⏭{lskipped}", f"⏳{lin_progress}"]
            layer_lines.append(
                f"- **{layer_key}**: {bar} {pct}% ({lcompleted}/{ltotal})　"
                f"{'　'.join(b for b in bits if not b.endswith('0'))}"
            )
        if layer_lines:
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "**📊 层级进度：**\n" + "\n".join(layer_lines)}}
            )
            elements.append({"tag": "hr"})

    if not is_terminal:
        vps_list = progress.get("vps") or []
        in_progress_vps = [
            v for v in vps_list
            if isinstance(v, dict) and v.get("status") == "running" and v.get("id")
        ]
        if in_progress_vps:
            if len(in_progress_vps) == 1:
                heading = "🔄 **当前验证 VP：**"
            else:
                heading = f"🔄 **当前并行验证 ({len(in_progress_vps)})：**"
            lines = [heading]
            for v in in_progress_vps:
                vid = v.get("id", "")
                vtitle = v.get("title", "")
                vmethod = v.get("method", "")
                vlayer = v.get("layer", "")
                method_tag = f" `{vmethod}`" if vmethod else ""
                layer_tag = f" ({vlayer})" if vlayer else ""
                lines.append(f"- `[{vid}]` {vtitle}{layer_tag}{method_tag}")
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "\n".join(lines)}}
            )

    completed_vps = progress.get("completed_vps") or []
    vps_index = {
        v.get("id"): v
        for v in (progress.get("vps") or [])
        if isinstance(v, dict) and v.get("id")
    }
    recent_done: List[Dict[str, Any]] = []
    for entry in completed_vps[-3:]:
        if isinstance(entry, dict):
            recent_done.append(entry)
        else:
            vmeta = vps_index.get(entry, {}) if isinstance(entry, str) else {}
            recent_done.append({
                "id": entry,
                "title": vmeta.get("title", ""),
                "method": vmeta.get("method", ""),
                "layer": vmeta.get("layer", ""),
            })
    if recent_done:
        lines = ["📜 **最近完成的 VP：**"]
        for vp in recent_done:
            vid = vp.get("id", "")
            vtitle = vp.get("title", "")
            vmethod = vp.get("method", "")
            vlayer = vp.get("layer", "")
            method_tag = f" `{vmethod}`" if vmethod else ""
            layer_tag = f" ({vlayer})" if vlayer else ""
            lines.append(f"- `[{vid}]` {vtitle}{layer_tag}{method_tag}")
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        )

    failed_vps = progress.get("failed_vps") or []
    vps_list = progress.get("vps") or []
    vps_index_fail = {
        v.get("id"): v
        for v in vps_list
        if isinstance(v, dict) and v.get("id")
    }
    if failed_vps:
        n = len(failed_vps)
        heading = (
            f"**❌ 失败 VP ({n})：**" if n > 1
            else "**❌ 失败 VP：**"
        )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": heading}}
        )
        for entry in failed_vps:
            if isinstance(entry, dict):
                fid = entry.get("id", "")
                ftitle = entry.get("title", "")
                fmethod = entry.get("method", "")
                flayer = entry.get("layer", "")
            else:
                # 2026-09-12: the failed_vps list emitted by
                # /api/verification/{id}/progress contains bare id strings
                # (e.g. ``"VP-006"``), not dicts. The previous code unpacked
                # ``fid, ftitle, fmethod, flayer = entry, "", "", ""`` and
                # rendered ``ftitle=""`` — operators saw a blank line per
                # failure and had to dig into verification_report.json to
                # discover WHICH VP failed and WHAT it was testing. Look up
                # the metadata from the canonical ``vps_index_fail`` dict
                # built above so the rendered line includes the title and
                # (when present) the actual_result reason.
                fid = entry if isinstance(entry, str) else ""
                fmeta = vps_index_fail.get(fid, {})
                ftitle = fmeta.get("title", "")
                fmethod = fmeta.get("method", "")
                flayer = fmeta.get("layer", "")
            reason = (fmeta.get("actual_result") or "").strip() \
                if isinstance(entry, str) else (entry.get("actual_result") or "").strip()
            method_tag = f" `{fmethod}`" if fmethod else ""
            layer_tag = f" · {flayer}" if flayer else ""
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md",
                    "content": f"🔴 **`{fid}`** · {ftitle}{layer_tag}{method_tag}"}}
            )
            if reason:
                short_reason = reason[:400]
                if len(reason) > 400:
                    short_reason += " …(详见 verification_report.json)"
                elements.append(
                    {"tag": "div", "text": {"tag": "lark_md",
                        "content": f"> {short_reason}"}}
                )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "_完整证据见 plans/<plan_id>/verification_report.json_"}}
        )

    skipped_vps = progress.get("skipped_vps") or []
    if skipped_vps:
        vps_index_skip = {
            v.get("id"): v
            for v in (progress.get("vps") or [])
            if isinstance(v, dict) and v.get("id")
        }
        s_lines = ["**⏭ 跳过 VP：**"]
        for entry in skipped_vps:
            if isinstance(entry, dict):
                sid = entry.get("id", "")
                stitle = entry.get("title", "")
                smethod = entry.get("method", "")
                slayer = entry.get("layer", "")
            else:
                vmeta = vps_index_skip.get(entry, {}) if isinstance(entry, str) else {}
                sid = entry
                stitle = vmeta.get("title", "")
                smethod = vmeta.get("method", "")
                slayer = vmeta.get("layer", "")
            method_tag = f" `{smethod}`" if smethod else ""
            layer_tag = f" ({slayer})" if slayer else ""
            s_lines.append(f"- `[{sid}]` {stitle}{layer_tag}{method_tag}")
            sreason = ""
            if isinstance(entry, dict):
                sreason = (entry.get("actual_result") or "").strip()
            else:
                sreason = (vps_index_skip.get(sid, {}).get("actual_result") or "").strip()
            if sreason:
                short_sreason = sreason[:400]
                if len(sreason) > 400:
                    short_sreason += " …(详见 verification_report.json)"
                s_lines.append(f"    ↳ {short_sreason}")
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(s_lines)}}
        )

    counts = progress.get("counts") or {}
    # 2026-09-11: drop the redundant
    # "📈 总结" line on terminal plans. The progress bar (rendered
    # at the top of the verification section) already shows
    # ✅completed / ❌failed / ⏭skipped counts in the non-zero
    # suffix; the bar's `(done/total)` ratio carries the same
    # information as the "通过 X 失败 Y 跳过 Z 总计 N" text line.
    # Symmetric with execution: the execution task summary bar
    # does not have a matching summary text line either.

    # 2026-09-12: when the plan is in a stuck terminal state the operator needs
    # to know WHAT blocked them and WHAT to do next. Without this
    # block the historical verification section just shows the round
    # line + failed VPs — nothing about the recovery path.
    #
    # Two stuck-state patterns we surface:
    #   A. terminal-failure phase after ``verification_status=failed``
    #     — the verification chain has fully terminated and there are
    #      no further auto-iterations. The operator must either
    #      ``POST /api/verification/{id}/reset_rounds`` to retry
    #      a fresh verification cycle (this resets the round **counter**;
    #      the ``max_rounds`` cap is immutable) and re-runs the failed
    #      VPs, or accept the failure and
    #      ``POST /api/plan/{id}/accept_failure`` to close the plan.
    #      Either action unblocks a stuck terminal-failure plan.
    #   B. ``verification_status=loop_stopped`` + ``stop_reason=
    #      max_rounds_reached`` — the loop hit the configured cap
    #      without converging. Operator must call ``reset_rounds``
    #      to hand it a fresh batch of rounds and the next verification
    #      tick will re-trigger round N+1.
    #
    # 2026-09-17 (schema v5): pattern A used to test
    # ``stage == "terminal_failed"`` against the routing column. That
    # value projected from these four phases, so this is the faithful
    # one-column spelling (and it subsumes the old
    # ``current_phase == "failed" and verif_status == "failed"`` clause,
    # which was the half-fix for the same split-brain).
    if is_terminal and (progress or status is not None):
        current_phase = status.phase if status is not None else ""
        # The verdict comes from the section's own payload first. The
        # body is describing the round it is rendering, and on the
        # wrapper path (``build_progress_card`` with an explicit
        # ``verification_history``) the snapshot deliberately carries no
        # verdict — the header must not see the history, but the body
        # *is* the history. Falling back to the snapshot keeps the
        # notifier path working, where both describe the same read.
        verif_status = (
            (progress or {}).get("verification_status")
            or (status.verification_status if status is not None else "")
            or ""
        ).lower()
        verif_stop = (progress or {}).get("stop_reason") or (
            status.verification_stop_reason if status is not None else None
        )
        # Pattern A: terminal failure (no executor subprocess running)
        is_terminal_failed = current_phase in _TERMINAL_FAILURE_PHASES
        # Pattern B: loop stopped at max_rounds (no auto-iteration)
        is_max_rounds_hit = (
            verif_status == "loop_stopped"
            and verif_stop == "max_rounds_reached"
        )
        if is_terminal_failed or is_max_rounds_hit:
            guidance_lines: List[str] = []
            # 2026-09-12: operators
            # saw "round 1" in the card but the operator-guidance
            # block declared "stuck at terminal_failed" — without
            # surfacing WHAT actually happened in round 1 (which VPs
            # failed, whether orchestrator tried to generate repair
            # tasks, what ``stop_reason`` was set), the diagnosis is
            # opaque. Make the cause visible inline.
            #
            # 1. Distinguish "round in progress" from "round finished
            #    and recorded failed" — the round counter alone is
            #    ambiguous because verification_status='failed'
            #    coexists with verification_round=1.
            v_round = (
                progress.get("verification_round")
                or status.verification_round or 0
            )
            v_max = (
                progress.get("max_rounds")
                or status.verification_max_rounds or 0
            )
            if v_round:
                round_phrase = (
                    f"round {v_round}/{v_max}" if v_max else f"round {v_round}"
                )
                guidance_lines.append(
                    f"_📍 当前轮次 {round_phrase} **已结束**（"
                    f"`status={verif_status or 'unknown'}`），"
                    f"不是正在跑——下面是这一轮记录到的失败原因。_"
                )
            # 2. Surface ``stop_reason`` so the operator understands the
            #    branch that triggered terminal_failed.
            if verif_stop:
                guidance_lines.append(
                    f"_📎 停止原因：`{verif_stop}`_"
                )
            elif verif_status == "failed":
                guidance_lines.append(
                    "_📎 停止原因：未配置（参考 `/api/verification/{id}/progress`）_"
                )
            if is_terminal_failed:
                guidance_lines.append(
                    "**⛔ 计划已卡在 `terminal_failed` — 验证链完全停止，**"
                    "需要人工介入。"
                )
            else:
                guidance_lines.append(
                    "**⛔ 验证循环已停止（达到 max_rounds 上限）—**"
                    "需要人工介入。"
                )
            # 3. Pull failed VPs (titles from the canonical vps index,
            #    NOT bare ids — same fix as the ❌ failed VPs section
            #    above) so the operator sees which VPs blocked the
            #    chain without scrolling.
            failed_vps_for_guidance = progress.get("failed_vps") or []
            if failed_vps_for_guidance:
                vps_index_for_guidance = {
                    v.get("id"): v
                    for v in (progress.get("vps") or [])
                    if isinstance(v, dict) and v.get("id")
                }
                fpv_lines: List[str] = ["_📌 本轮失败 VP（标题）：_"]
                for entry in failed_vps_for_guidance:
                    if isinstance(entry, dict):
                        fid = entry.get("id", "")
                        ftitle = entry.get("title", "")
                    else:
                        fid = entry if isinstance(entry, str) else ""
                        ftitle = vps_index_for_guidance.get(fid, {}).get("title", "")
                    line = f"- `{fid}`"
                    if ftitle:
                        line += f" — {ftitle[:60]}"
                    fpv_lines.append(line)
                guidance_lines.append("\n".join(fpv_lines))
            # Repair tasks line: tell the operator what the executor
            # WOULD have run if max_rounds had not been hit. Pull from
            # the synthesised verification_history that the caller
            # passed via ``progress`` (it carries ``repair_tasks``).
            queued_repairs = progress.get("repair_tasks") or []
            if queued_repairs:
                ids = [rt.get("id") for rt in queued_repairs
                       if isinstance(rt, dict) and rt.get("id")]
                if ids:
                    guidance_lines.append(
                        f"已生成的修复任务（待执行）：`{', '.join(ids)}`"
                    )
            # Recovery commands — match the canonical endpoints so
            # operators can copy-paste without guessing the route.
            guidance_lines.append("**🔧 解锁指令（二选一）：**")
            if is_max_rounds_hit:
                guidance_lines.append(
                    "- 重置轮次并重试：`POST /api/verification/{plan_id}/reset_rounds`"
                    " — 把当前轮次计数重置回 1（`max_rounds` 上限不可改），"
                    "下一轮自动重跑"
                )
            else:
                guidance_lines.append(
                    "- 重跑验证：`POST /api/verification/{plan_id}/reset_rounds`"
                    " — 重新跑一次 verification 链"
                )
            guidance_lines.append(
                "- 接受失败：`POST /api/plan/{plan_id}/accept_failure`"
                " — 关闭 plan（仅在确认无需继续修复时使用）"
            )
            guidance_lines.append(
                "_完整 endpoint 见 `/api/system/endpoints` 路由表；"
                "或直接告诉我「重跑验证」/「接受失败」我会代你执行。_"
            )
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "\n".join(guidance_lines)}}
            )

    return elements


def _execution_sections(
    tasks_info: Dict[str, Any],
    execution_progress: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Render the execution sub-sections (under the 📋 task area).

    Includes: 📋 task summary bar, 🔄 in-progress tasks, 📜 recent
    activity, ❌ failed-task detail + 🚧 blocked tasks, ⏭ skipped
    tasks. Each section self-skips when its data is empty/absent.
    Returns ``[]`` for plans with no tasks (e.g. smoke-test / empty).

    2026-09-11: the in-progress filter previously required ``t.get("title")`` to be
    truthy. For RP-* tasks injected by
    ``tools/bootstrap_repair_tasks.py`` the runtime overlay sometimes
    carries only ``status`` / ``failure_reason`` (no ``title`` field),
    so the entire in-progress section was skipped — operators saw
    "任务执行状态 —————— 最近完成活动" with two parallel ``<hr>`` lines
    and no clue which tasks were actually running. Filter now matches
    on ``status == "in_progress"`` alone; the per-task renderer falls
    back to a bare id line when ``title`` is empty so the section is
    never silently dropped.
    """
    elements: List[Dict[str, Any]] = []
    elements.extend(_task_summary_section(tasks_info))
    if not execution_progress:
        return elements

    tasks_list = execution_progress.get("tasks", []) or []
    in_progress_tasks = [
        t for t in tasks_list
        if isinstance(t, dict)
        and t.get("status") == "in_progress"
    ]
    if in_progress_tasks:
        # 2026-09-11 (clarity): the ``_task_summary_section`` helper
        # above already appends a
        # trailing ``<hr>`` after the 任务执行状态 bar, so we do
        # NOT add another ``<hr>`` here — the previous code did and
        # the operator saw "任务执行状态 ——————— 当前任务" with two
        # parallel separators which looked like a duplicate.
        if len(in_progress_tasks) == 1:
            heading = "🔄 **当前任务：**"
        else:
            heading = f"🔄 **当前并行任务 ({len(in_progress_tasks)})：**"
        lines = [heading]
        for t in in_progress_tasks:
            tid = t.get("id", "")
            title = (t.get("title") or "").strip()
            # 2026-09-11 plan v14: render the title when present, fall
            # back to the id only when missing (orphan RP-* / db-only
            # tasks). Without the fallback the entire section was
            # silently dropped (filter required truthy title).
            if len(title) > 80:
                title = title[:77] + "…"
            if title:
                line = f"- `[{tid}]` {title}"
            else:
                line = f"- `{tid}`"
            if t.get("attempts"):
                line += f"　(尝试 {t.get('attempts')}/5)"
            lines.append(line)
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        )

    # 2026-09-12 plan v14 follow-up: surface the pending tasks so
    # operators can see which tasks the executor SHOULD be picking up
    # next. Repair tasks could sit at pending because the executor
    # subprocess never spawned (v13/v14 race); without this section
    # you have to inspect plan_tasks by hand to know
    # what's waiting. Limit to top 5 to keep the layout compact.
    pending_tasks = [
        t for t in tasks_list
        if isinstance(t, dict)
        and t.get("status") == "pending"
        and t.get("id")
    ]
    if pending_tasks:
        elements.append({"tag": "hr"})
        pending_sorted = pending_tasks[:5]
        more_n = len(pending_tasks) - len(pending_sorted)
        if len(pending_sorted) == 1:
            heading = "📋 **等待中的任务：**"
        else:
            heading = f"📋 **等待中的任务 ({len(pending_tasks)})：**"
        lines = [heading]
        for t in pending_sorted:
            tid = t.get("id", "")
            title = (t.get("title") or "").strip()
            if len(title) > 80:
                title = title[:77] + "…"
            if title:
                lines.append(f"- `[{tid}]` {title}")
            else:
                # Orphan RP-* / db-only tasks without a title —
                # always show the id so the section is never empty.
                lines.append(f"- `{tid}`")
        if more_n > 0:
            lines.append(f"- 等 {more_n} 个…")
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        )

    done_tasks = [
        t for t in tasks_list
        if isinstance(t, dict)
        and t.get("end_ts")
        # 2026-09-11 (clarity): exclude
        # ``in_progress`` tasks from "最近完成活动" because they
        # already appear in their own dedicated section above.
        # Defensive: even if the runtime overlay ever sets ``end_ts``
        # on an in_progress task, we keep it out of the completed
        # list so the operator never sees the same task twice in
        # different sections.
        and t.get("status") != "in_progress"
    ]
    done_tasks.sort(key=lambda t: t.get("end_ts") or "", reverse=True)
    done_tasks = done_tasks[:5][::-1]
    if done_tasks:
        elements.append({"tag": "hr"})
        # 2026-09-11 plan v10: renamed to "最近完成活动" so the label
        # accurately reflects the filter (in_progress tasks are
        # surfaced in their own "🔄 当前任务" section above).
        lines = ["📜 **最近完成活动：**"]
        for t in done_tasks:
            status = t.get("status") or "?"
            status_emoji = {
                "completed": "✅", "failed": "❌",
                "skipped": "⏭",
            }.get(status, "•")
            lines.append(
                f"- {status_emoji} `[{t.get('id', '')}]` {t.get('title', '')}"
            )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        )

    failed_n = tasks_info.get("failed", 0) or 0
    if failed_n > 0:
        failed_tasks = [
            t for t in tasks_list
            if isinstance(t, dict) and t.get("status") == "failed"
        ]
        if failed_tasks:
            elements.append({"tag": "hr"})
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": "**❌ 失败任务详情：**"}}
            )
            for t in failed_tasks:
                tid = t.get("id", "?")
                # 2026-09-11 plan v9 (Bug 1 fix): for ``db_orphan_terminal``
                # placeholders there is no static ``title`` field on disk —
                # the orphan row in ``plan_tasks`` only carries status +
                # failure_reason. Show the id + reason text instead of a
                # generic "未知任务" so the operator can correlate with
                # ``plan_tasks`` rows directly.
                # 2026-09-14: the endpoint hydrate resolves the REAL
                # title from the plan_tasks row when available —
                # prefer it and only fall back to the DB-only marker
                # when the row genuinely has no title (the previous
                # code unconditionally overwrote the resolved title,
                # which is why operators saw "Task RP-1 (DB-only)"
                # even for rows with real titles).
                if t.get("_origin") == "db_orphan_terminal":
                    title = t.get("title") or f"Task {tid} (DB-only)"
                else:
                    title = t.get("title") or "未知任务"
                reason = t.get("failure_reason") or _extract_failure_context(t.get("description")) or "未知原因"
                if len(reason) > 200:
                    reason = reason[:200] + "..."
                # 2026-09-11: surface
                # the per-task attempt count from the runtime overlay
                # so the operator can see "tried N times before giving
                # up" at a glance. ``attempt`` is in
                # ``_RUNTIME_OVERLAY_FIELDS`` (``server.py``) so the
                # progress endpoint already inherits it onto the task
                # dict. ``attempts`` is an alias some callers use — we
                # accept either. ``max_retries`` is the executor's stop
                # bound (see ``retry_manager.MAX_RETRIES`` and
                # ``config.max_retries``); if the overlay doesn't carry
                # it we still show the count alone.
                attempts_used = t.get("attempts") or t.get("attempt") or 1
                # ``max_retries`` defaults to 5 (config.py + retry_manager
                # align after 2026-09-11 plan), but the runtime overlay
                # doesn't always carry it; fall back to retry_manager's
                # current class attr so the rendered text stays truthful.
                max_retries_default = 5
                try:
                    from retry_manager import RetryManager as _RM  # type: ignore
                    max_retries_default = getattr(_RM, "MAX_RETRIES", 5)
                except Exception:
                    pass
                # Only render the ``尝试 N/M`` suffix when the count is
                # > 1 (a single-attempt failure is the common case and
                # doesn't need the suffix noise); when ``attempt`` is
                # missing entirely we still show the failure reason
                # without the suffix.
                if attempts_used and int(attempts_used) > 1:
                    attempt_suffix = f"  (尝试 {int(attempts_used)}/{max_retries_default})"
                else:
                    attempt_suffix = ""
                content = f"• `[{tid}]` {title}{attempt_suffix}"
                content += f"\n  └─ {reason}"
                elements.append(
                    {"tag": "div", "text": {"tag": "lark_md", "content": content}}
                )
        elements.extend(_blocked_tasks_section(execution_progress))

    skipped = tasks_info.get("skipped", 0) or 0
    if skipped > 0:
        skipped_tasks = [
            t for t in tasks_list
            if isinstance(t, dict) and t.get("status") == "skipped"
        ]
        if skipped_tasks:
            elements.append({"tag": "hr"})
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": "**⏭ 跳过的任务：**"}}
            )
            for t in skipped_tasks:
                tid = t.get("id", "?")
                title = t.get("title") or "未知任务"
                display_title = title if len(title) <= 80 else title[:80] + "..."
                elements.append(
                    {"tag": "div", "text": {"tag": "lark_md",
                                            "content": f"• `[{tid}]` {display_title}"}}
                )

    return elements


def build_card(
    plan_id: str,
    plan_status: PlanStatus,
    summary: Optional[Dict[str, Any]] = None,
    execution_progress: Optional[Dict[str, Any]] = None,
    verification_progress: Optional[Dict[str, Any]] = None,
    *,
    verification_body_only: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the single, complete Feishu card for a plan.

    2026-09-11 (single-card design): the card
    pushed to Feishu is ALWAYS a full overwrite of the message, so
    this builder renders the COMPLETE card from the data snapshot,
    not "one of two" card types. Each section self-skips when its
    data is empty — execution sections need ``execution_progress``;
    verification sections need ``verification_progress``.

    Arguments:
      * ``plan_status``: the plan snapshot (``plan_status.PlanStatus``).
        **The header is a pure function of this**, and of nothing else —
        2026-09-23: it used to be re-derived from whatever mix of
        ``summary`` / ``verification_progress`` / overrides the caller
        happened to pass, which is how the header came to describe a
        plan that did not exist. Everything the verdict needs (phase,
        verdict, round, real liveness) is on the snapshot.
      * ``summary``: ``/api/plan/{id}/summary`` payload, for the body's
        task counts.
      * ``execution_progress``: ``/api/execution/{id}/progress`` payload,
        or None for plans that never started execution.
      * ``verification_progress``: ``/api/verification/{id}/progress``,
        or None for plans that never ran verification.
      * ``verification_body_only`` (2026-09-11 plan v14): renders a
        verification section in the body WITHOUT contaminating the
        header resolver. Use this when the plan's execution is
        terminal (e.g. completed / failed / paused) but the operator
        still wants to see the historical verification record in the
        body. If both ``verification_progress`` and
        ``verification_body_only`` are provided, the live one wins
        (header gets the live status, body shows live data too).

    Returns a Feishu card dict (the notifier wraps it in
    ``interactive`` message type). Pure function — no I/O.
    """
    # 2026-09-17: superseded DB-only rows are not part of the plan's work
    # (the dispatcher never schedules them) — see
    # ``drop_terminal_db_orphans``. Normalising here means every section
    # below — the progress bar, the waiting / in-flight / blocked / failed
    # lists and "最近完成活动" — agrees on the same task set.
    summary, execution_progress = drop_terminal_db_orphans(
        summary, execution_progress,
    )

    tasks_info = (summary or {}).get("tasks") or dict(plan_status.tasks)

    # Header — one decision, from one snapshot. The body-only history
    # still must not leak into it (2026-09-11 plan v14: a plan with
    # phase=completed AND verification_status=passed should read
    # "✅ 已完成", not "✅ 验证通过"), but that is now structural rather
    # than a caller convention: the header only ever sees ``plan_status``.
    #
    # 2026-09-27: it also sees the SAME ``tasks_info`` the body uses.
    # It used to read ``status.tasks`` — a second source — which is how
    # a single card reported two different totals.
    body_only = verification_body_only
    v_status = (plan_status.verification_status or "").lower()
    v_round = plan_status.verification_round
    v_max_rounds = plan_status.verification_max_rounds
    v_stop_reason = plan_status.verification_stop_reason

    header = _resolve_header(plan_status, tasks_info)

    is_terminal = v_status in _VERIFICATION_TERMINAL_STATUSES

    # 2026-09-17 (schema v5): this used to resolve TWO values —
    # ``unified_stage`` (routing) and ``unified_exec_phase``
    # (execution) — because the notifier fed a routing ``stage`` and a
    # plan-state ``current_phase`` in from separate summary fields that
    # could disagree. There is one workflow-state value now.
    unified_phase = plan_status.phase or None

    # Body — unified "where am I" banner first, then the execution
    # sections, then the verification sections.
    #
    # 2026-09-13: the v5 builder
    # merge (``805aaa1``) replaced the two standalone card builders
    # with this unified one, but deleted the old bodies WITHOUT
    # carrying their ``_unified_phase_section`` call over. The
    # "📍 当前状态" line therefore vanished from every card while the
    # helper and its 19 regression tests stayed behind — the two
    # still pass in isolation (they call the helper directly) and
    # only the card-level tests noticed. Restoring the call is what
    # makes the unified-status contract hold again: execution and
    # verification are separate *processes* but a single *status*, so
    # the card states it once. Per
    # ``test_unified_header_is_first_element_in_both_cards`` it MUST
    # be the first body element.
    # Derive the currently-running task (id + title) so the unified
    # line can name it instead of the bare "正在跑 tasks" label
    # (2026-09-14). First in_progress row wins.
    _current_task: Optional[Tuple[str, str]] = None
    for _t in ((execution_progress or {}).get("tasks") or []):
        if (
            isinstance(_t, dict)
            and _t.get("status") == "in_progress"
            and _t.get("id")
        ):
            _current_task = (str(_t["id"]), (_t.get("title") or "").strip())
            break
    # Same for the verification window: name the VP being verified
    # (2026-09-14). The payload's current_vp
    # carries id + title + started_at once the progress endpoint has
    # a live VP in flight.
    _current_vp: Optional[Dict[str, Any]] = None
    _vp_candidate = (verification_progress or {}).get("current_vp")
    if isinstance(_vp_candidate, dict) and _vp_candidate.get("id"):
        _current_vp = _vp_candidate

    elements: List[Dict[str, Any]] = []
    elements.extend(_unified_phase_section(
        unified_phase,
        verification_status=v_status,
        verification_round=v_round,
        verification_max_rounds=v_max_rounds,
        verification_stop_reason=v_stop_reason,
        current_task=_current_task,
        current_vp=_current_vp,
    ))
    # Separator only when the banner actually rendered — a brand-new
    # plan with no state row yet has no stage, and a dangling ``<hr>``
    # would read as a rendering bug.
    if elements:
        elements.append({"tag": "hr"})
    elements.extend(_execution_sections(tasks_info, execution_progress))

    # Pick the body section's source: live verification_progress wins,
    # fall back to verification_body_only (historical record that
    # must NOT contaminate the header).
    body_verif_progress = verification_progress if verification_progress else body_only
    body_is_terminal = (
        v_status if verification_progress else (
            (body_only.get("verification_status") or "").lower()
            if body_only else ""
        )
    ) in _VERIFICATION_TERMINAL_STATUSES

    if body_verif_progress:
        # Group separator between execution and verification bodies.
        elements.append({"tag": "hr"})
        # 2026-09-11: the previous code rendered the bare
        # "🔍 verification 状态" header which made it ambiguous
        # whether verification was live or just a historical record.
        # Distinguish the two cases so the operator knows whether
        # to look for in-flight VP updates or just a snapshot of
        # the last completed round.
        if body_is_terminal:
            section_heading = "**🔍 verification 状态（历史）**"
        else:
            section_heading = "**🔍 verification 状态**"
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": section_heading}}
        )
        elements.extend(_verification_sections(body_verif_progress, body_is_terminal, plan_status))

    elements.append({"tag": "hr"})
    elements.append(
        {"tag": "note", "elements": [{"tag": "plain_text",
                                      "content": f"🕐 刷新于 {_format_now()}"}]}
    )

    card: Dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header["title"]},
            "template": header["color"],
        },
        "elements": elements,
    }
    card["_heartbeat"] = f"heartbeat:{_format_now()}"
    return card


# ---------------------------------------------------------------------------
# Backwards-compatible thin wrappers
# ---------------------------------------------------------------------------
#
# 2026-09-11 plan v5: ``build_progress_card`` and
# ``build_verification_card`` are kept as thin shims so existing
# test files (which call them directly) keep working without import
# changes. The notifier (``feishu_notifier._rebuild_card``) calls
# ``build_card`` directly. Future cleanup may remove these shims.


def _status_from_summary(
    plan_id: str,
    summary: Optional[Dict[str, Any]],
    *,
    max_rounds_override: Optional[int] = None,
    stop_reason_override: Optional[str] = None,
) -> "PlanStatus":
    """A **degraded** ``PlanStatus`` built from a summary payload.

    Compatibility path for the two ``build_*_card`` shims below and the
    tests written against them — callers that only hold the JSON the
    legacy endpoints returned. The production notifier passes a real
    ``PlanStatus`` assembled server-side by ``GET /api/plan/{id}/status``.

    Degraded in exactly one respect: ``execution_in_flight`` is **not
    knowable** out here (it reads a per-process record), so it defaults
    to ``False``. That is the safe direction — a header may then fall
    back to "执行中断" / "暂停" where a live round would have read
    "修复执行中", never the reverse (claiming work is running when it is
    not). See :mod:`plan_status` for why the flag cannot be guessed from
    a payload.
    """
    from plan_status import PlanStatus

    state = (summary or {}).get("state") or {}
    verif = state.get("verification") or {}
    max_rounds = (
        max_rounds_override
        if max_rounds_override is not None
        else verif.get("max_rounds")
    )
    stop_reason = (
        stop_reason_override
        if stop_reason_override is not None
        else verif.get("stop_reason")
    )
    try:
        round_n = int(verif.get("round") or 0)
    except (TypeError, ValueError):
        round_n = 0
    try:
        max_n = int(max_rounds or 0)
    except (TypeError, ValueError):
        max_n = 0
    return PlanStatus(
        plan_id=plan_id,
        phase=state.get("current_phase") or "",
        verification_status=verif.get("status") or "",
        verification_round=round_n,
        verification_max_rounds=max_n,
        verification_stop_reason=stop_reason,
        execution_in_flight=False,
        verification_in_flight=False,
        tasks=dict((summary or {}).get("tasks") or {}),
    )


def build_progress_card(
    plan_id: str,
    summary: Dict[str, Any],
    progress: Optional[Dict[str, Any]] = None,
    verification_history: Optional[Dict[str, Any]] = None,
    plan_status: Optional["PlanStatus"] = None,
) -> Dict[str, Any]:
    """Thin wrapper preserved for backwards compatibility with tests
    written before the v5 builder merge. Delegates to :func:`build_card`.

    2026-09-11: the previous
    shim always passed ``verification_progress=None`` so the
    unified builder skipped the entire "🔍 verification 状态"
    group the moment the executor restarted. Operators lost the
    round history at the exact time they most needed it. The new
    ``verification_history`` parameter lets the notifier forward a
    synthesised minimal snapshot (status / round / max_rounds /
    stop_reason + repair_tasks) so the historical section persists
    alongside the live execution sections. When ``verification_history``
    is None we fall back to ``summary.state.verification`` so the
    card still has the latest snapshot even when the caller didn't
    synthesise explicitly.

    Critical: ``verification_history`` is passed via the
    ``verification_body_only`` parameter — NOT ``verification_progress``.
    The body's verification section picks it up but the header
    resolver does NOT see the verification status, so a plan with
    phase=completed AND verification_status=passed renders the
    "✅ 已完成" header (operator's preferred signal) plus the
    verification history in the body. Without this separation the
    header would incorrectly flip to "✅ 验证通过" and hide the
    execution outcome.
    """
    # If verification_history wasn't passed, fall back to the
    # summary.state.verification snapshot so plans with prior
    # verification rounds still see the section.
    if verification_history is None:
        state = summary.get("state", {}) or {}
        summary_verif = state.get("verification", {}) or {}
        if summary_verif:
            verification_history = {
                "verification_status": summary_verif.get("status") or "",
                "verification_round": summary_verif.get("round") or 0,
                "max_rounds": summary_verif.get("max_rounds") or 0,
                "stop_reason": summary_verif.get("stop_reason"),
                "repair_tasks": [],
            }
    # The header must not see the verification history (see the
    # docstring): strip it from the summary the status is built from.
    # The body still gets it, through ``verification_body_only``.
    header_summary = dict(summary or {})
    _state = dict(header_summary.get("state") or {})
    _state.pop("verification", None)
    header_summary["state"] = _state

    return build_card(
        plan_id,
        plan_status or _status_from_summary(plan_id, header_summary),
        summary,
        execution_progress=progress,
        verification_body_only=verification_history,
    )


def build_verification_card(
    plan_id: str,
    progress: Dict[str, Any],
    tasks_info: Optional[Dict[str, Any]] = None,
    task_progress: Optional[List[Dict[str, Any]]] = None,
    current_phase: Optional[str] = None,
    max_rounds_override: Optional[int] = None,
    stop_reason_override: Optional[str] = None,
    plan_status: Optional["PlanStatus"] = None,
) -> Dict[str, Any]:
    """Thin wrapper preserved for backwards compatibility with tests
    written before the v5 builder merge. Delegates to :func:`build_card`
    by constructing a minimal ``summary`` from the verification-only
    arguments the wrapper receives (the wrapper predates the unified
    snapshot pattern).
    """
    # The original ``build_verification_card`` rendered the verification
    # header directly from ``current_phase`` / verification status,
    # ignoring whether the underlying plan had tasks. Preserve that
    # behavior: if ``tasks_info`` is missing we mark the plan as
    # "has tasks" (total=1, completed=0) so the empty-plan guard in
    # ``_resolve_header`` doesn't kick in and override the verification
    # header with "⚪ 空 plan".
    if tasks_info is None:
        # Synthesize tasks_info that the empty-plan guard will not
        # match (total > 0). Counts are irrelevant for the header
        # decision — only ``total > 0`` matters.
        effective_tasks_info: Dict[str, Any] = {
            "total": 1, "completed": 0, "failed": 0,
            "in_progress": 0, "pending": 0, "skipped": 0,
        }
    else:
        effective_tasks_info = tasks_info
    summary: Dict[str, Any] = {
        "state": {
            "current_phase": current_phase or "",
            "verification": {
                "status": progress.get("verification_status") if progress else "",
                "round": progress.get("verification_round") if progress else 0,
                "max_rounds": progress.get("max_rounds") if progress else None,
                "stop_reason": progress.get("stop_reason") if progress else None,
            },
        },
        "tasks": effective_tasks_info,
        "execution": {},
    }
    return build_card(
        plan_id,
        plan_status or _status_from_summary(
            plan_id, summary,
            max_rounds_override=max_rounds_override,
            stop_reason_override=stop_reason_override,
        ),
        summary,
        execution_progress={"tasks": task_progress or []},
        verification_progress=progress,
    )
