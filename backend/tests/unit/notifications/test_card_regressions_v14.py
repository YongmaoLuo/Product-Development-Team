"""TDD tests for the 2026-09-11 plan v14 Feishu card regressions.

Two defects appeared on the Feishu card after the prior v12 ``cards.py``
work: task titles stopped rendering, and the verification section
vanished while only the execution part was still shown.

**Regression A — task title missing**
  ``_execution_sections`` filtered ``in_progress_tasks`` with
  ``t.get("title")`` as a truthy requirement. For RP-* tasks injected
  by ``tools/bootstrap_repair_tasks.py`` the runtime overlay sometimes
  carries only ``status`` / ``failure_reason`` (no ``title`` field),
  so the entire in-progress section was silently skipped. Operators
  saw "任务执行状态 —————— 最近完成活动" with two parallel ``<hr>``
  lines and no clue which tasks were running.

**Regression B — verification section disappears on execution iteration**
  ``build_progress_card`` shim passed ``verification_progress=None``
  to ``build_card``, which made the unified builder skip the entire
  "🔍 verification 状态" group. When the executor restarted to run
  RP-* tasks (auto-loop chain v14), operators lost the verification
  round history at the moment they most needed it.
"""

from unittest.mock import MagicMock, patch

import pytest

from status_payload import status_from  # noqa: E402


# ---------------------------------------------------------------------------
# Regression A — task title fallback
# ---------------------------------------------------------------------------


def test_in_progress_section_renders_when_title_missing():
    """RP-* / db_orphan tasks without a ``title`` field must still
    appear in the in-progress section. The previous
    ``t.get("title")`` filter dropped them entirely.
    """
    from notifications.cards import _execution_sections

    tasks_info = {"total": 2, "completed": 0, "failed": 0,
                  "in_progress": 1, "pending": 1, "skipped": 0}
    # RP-* task with NO title field (matches bootstrap_repair_tasks.py
    # output where runtime overlay only carries status / failure_reason).
    execution_progress = {
        "tasks": [
            {"id": "RP-1", "status": "in_progress"},
        ],
    }

    elements = _execution_sections(tasks_info, execution_progress)
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "RP-1" in combined
    # The fallback line should still mention the id even without title.
    assert "当前任务" in combined


def test_in_progress_section_renders_with_title_when_present():
    """When title IS present, render `[id] title` line."""
    from notifications.cards import _execution_sections

    tasks_info = {"total": 2, "completed": 0, "failed": 0,
                  "in_progress": 1, "pending": 1, "skipped": 0}
    execution_progress = {
        "tasks": [
            {"id": "RP-1", "status": "in_progress",
             "title": "修复合盘整信号 ratio 阈值 (VP-006)"},
        ],
    }

    elements = _execution_sections(tasks_info, execution_progress)
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "RP-1" in combined
    assert "修复合盘整信号 ratio 阈值" in combined


# ---------------------------------------------------------------------------
# Regression A.2 — pending task visibility (2026-09-12 follow-up)
# ---------------------------------------------------------------------------


def test_pending_section_renders_titles():
    """2026-09-12 follow-up: operators need to see which tasks are
    WAITING for the executor. Repair tasks had been stuck at
    pending because the executor subprocess never spawned — without
    a '📋 等待中的任务' section the only signal was the
    '📋{pending_count}' chip in the bar, which doesn't show titles.
    """
    from notifications.cards import _execution_sections

    tasks_info = {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 0, "pending": 2, "skipped": 0}
    execution_progress = {
        "tasks": [
            {"id": "RP-1", "status": "pending",
             "title": "修复 signal.rs ratio 阈值 (VP-006)"},
            {"id": "RP-2", "status": "pending"},
        ],
    }

    elements = _execution_sections(tasks_info, execution_progress)
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "等待中的任务" in combined
    assert "[RP-1]" in combined
    assert "修复 signal.rs ratio 阈值" in combined
    # Orphan RP-2 without title still shows the id
    assert "RP-2" in combined


def test_pending_section_silent_when_no_pending():
    """When no tasks are pending, no '等待中的任务' section renders."""
    from notifications.cards import _execution_sections

    tasks_info = {"total": 5, "completed": 5, "failed": 0,
                  "in_progress": 0, "pending": 0, "skipped": 0}
    execution_progress = {"tasks": []}

    elements = _execution_sections(tasks_info, execution_progress)
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "等待中的任务" not in combined


# ---------------------------------------------------------------------------
# Regression B.2 — historical round summary in body verification section
# ---------------------------------------------------------------------------


def test_historical_verification_section_shows_round_line():
    """2026-09-12 follow-up: the historical verification section
    must show the round number + status, otherwise the section
    is empty (no vps / counts / repair_tasks to render).
    """
    from notifications.cards import _verification_sections, build_card
    from status_payload import status_from

    summary = {
        "state": {
            "current_phase": "executing",
            "verification": {
                "status": "failed",
                "round": 3,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 0, "pending": 2, "skipped": 0},
        "execution": {},
    }
    # Minimal synthesised history (no vps / counts / repair_tasks)
    body_only = {
        "verification_status": "failed",
        "verification_round": 3,
        "max_rounds": 3,
        "stop_reason": None,
    }
    card = build_card(
        "plan-x",
        status_from(summary, verification=body_only, plan_id="plan-x"),
        summary,
        execution_progress=None,
        verification_progress=None,
        verification_body_only=body_only,
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in card["elements"]
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "历史轮次" in combined
    assert "round 3/3" in combined
    assert "failed" in combined


# ---------------------------------------------------------------------------
# Regression B — verification section persists in execution card
# ---------------------------------------------------------------------------


def test_build_progress_card_renders_verification_history():
    """When ``build_progress_card`` is called with verification_history,
    the verification section must appear in the output even though the
    plan is in execution phase.

    Pre-v14 the shim passed ``verification_progress=None`` so the
    "🔍 verification 状态" group was silently dropped.
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "executing",
            "verification": {
                "status": "failed",
                "round": 1,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 1, "pending": 1, "skipped": 0},
        "execution": {},
    }
    verification_history = {
        "verification_status": "failed",
        "verification_round": 1,
        "max_rounds": 3,
        "stop_reason": None,
        "repair_tasks": [{"id": "RP-1", "title": "fix divergence ratio"}],
    }
    progress = {
        "tasks": [
            {"id": "RP-1", "status": "in_progress",
             "title": "fix divergence ratio"},
        ],
    }

    card = build_progress_card(
        "plan-xyz", summary,
        progress=progress,
        verification_history=verification_history,
    )

    elements = card.get("elements", [])
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    # Verification section MUST appear
    assert "verification" in combined.lower() or "验证" in combined
    # The historical label distinguishes from live
    assert "历史" in combined


def test_build_progress_card_falls_back_to_summary_state_verification():
    """If verification_history is not passed but summary.state.verification
    has data, the section still renders (no data loss on plans that ran
    verification at least once).
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "executing",
            "verification": {
                "status": "passed",
                "round": 2,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {"total": 30, "completed": 30, "failed": 0,
                  "in_progress": 0, "pending": 0, "skipped": 0},
        "execution": {},
    }
    progress = {"tasks": []}

    card = build_progress_card(
        "plan-abc", summary, progress=progress,
        # No verification_history kwarg — must fall back to summary
    )

    elements = card.get("elements", [])
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    # Verification section header is the historical-style "🔍 verification 状态（历史）"
    assert "verification" in combined.lower()
    assert "历史" in combined


def test_build_progress_card_no_verification_for_fresh_plan():
    """Plan with empty summary.state.verification and no verification_history
    renders NO verification section (smoke-test / never-verified plan).
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "executing",
            "verification": {},  # empty — never ran verification
        },
        "tasks": {"total": 5, "completed": 2, "failed": 0,
                  "in_progress": 1, "pending": 2, "skipped": 0},
        "execution": {},
    }
    progress = {"tasks": [{"id": "T-1", "status": "in_progress",
                            "title": "doing the thing"}]}

    card = build_progress_card(
        "plan-fresh", summary, progress=progress,
    )

    elements = card.get("elements", [])
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    # Verification section should NOT appear
    assert "🔍 verification" not in combined
    assert "verification 状态" not in combined


# ---------------------------------------------------------------------------
# Regression C — orphan RP-* tasks render real titles (2026-09-12 follow-up)
# ---------------------------------------------------------------------------


def test_pending_section_renders_real_title_for_orphan_with_runtime_title():
    """2026-09-12 follow-up: when the /api/execution/{id}/progress
    endpoint hydrates an orphan task (``db_orphan`` placeholder), it
    must prefer the runtime overlay's ``title`` over the placeholder
    "Task <tid> (DB-only)" so the operator sees the real task identity.

    Without this fix the card rendered no task titles at all, so the
    reader could not tell which task it referred to. RP-* tasks (which are
    injected by ``tools/bootstrap_repair_tasks.py`` and exist only in
    ``plan_tasks`` SQLite, not on disk) showed up as bare id labels in
    the "📋 等待中的任务" section.
    """
    from notifications.cards import _execution_sections

    tasks_info = {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 0, "pending": 2, "skipped": 0}
    execution_progress = {
        "tasks": [
            {"id": "RP-1", "status": "pending",
             "title": "修复 VP-006 函数体 diff (select_entering_consolidation)"},
            {"id": "RP-2", "status": "pending",
             "title": "修正 VP-010 测试命令 filter 拼写"},
        ],
    }

    elements = _execution_sections(tasks_info, execution_progress)
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "等待中的任务" in combined
    # Real title from runtime overlay must appear (not the placeholder)
    assert "修复 VP-006 函数体 diff" in combined, (
        "RP-1 real title missing — endpoint is still using "
        "'Task <tid> (DB-only)' placeholder. Re-check server.py "
        "orphan hydration in /api/execution/{id}/progress."
    )
    assert "修正 VP-010 测试命令 filter 拼写" in combined, (
        "RP-2 real title missing."
    )
    # Placeholder should NOT appear when real title is present
    assert "Task RP-1 (DB-only)" not in combined
    assert "Task RP-2 (DB-only)" not in combined


def test_pending_section_falls_back_to_placeholder_when_runtime_title_empty():
    """When runtime overlay has neither ``title`` nor ``description``
    for the orphan (true DB-only task with empty static fields — legacy
    case), the placeholder fallback must still kick in so the operator
    can at least see the task id.
    """
    from notifications.cards import _execution_sections

    tasks_info = {"total": 5, "completed": 3, "failed": 0,
                  "in_progress": 0, "pending": 2, "skipped": 0}
    execution_progress = {
        "tasks": [
            # Both runtime title and description are missing/empty —
            # the endpoint would still emit a placeholder dict with
            # ``title`` synthesized to ``"Task <tid> (DB-only)"``.
            {"id": "RP-7", "status": "pending",
             "title": "Task RP-7 (DB-only)",
             "description": "recovered from plan_tasks DB"},
        ],
    }

    elements = _execution_sections(tasks_info, execution_progress)
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    assert "等待中的任务" in combined
    assert "[RP-7]" in combined
    # Placeholder preserved as fallback
    assert "Task RP-7 (DB-only)" in combined


# ---------------------------------------------------------------------------
# Regression D — operator-action guidance for stuck terminal plans
# (2026-09-12: a blocked plan must say what blocked it and what
# to do next)
# ---------------------------------------------------------------------------


def test_failed_block_renders_unlock_instructions():
    """A plan stuck at ``stage=failed`` with
    ``verification_status=failed`` must surface an explicit operator
    guidance block — without it the card just shows the failed round
    + repair task list but no recovery path.

    The guidance block must contain:
    - The "⛔ 计划已卡在 failed" warning
    - The two recovery endpoints (``/reset_rounds`` and
      ``/accept_failure``)
    - A copy-paste hint so the operator doesn't have to guess
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "failed",
            "stage": "failed",
            "verification": {
                "status": "failed",
                "round": 3,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 0, "pending": 2, "skipped": 0},
        "execution": {},
    }
    progress = {"tasks": [
        {"id": "RP-1", "status": "pending",
         "title": "修复 VP-006 函数体 diff"},
        {"id": "RP-2", "status": "pending",
         "title": "修正 VP-010 测试命令 filter 拼写"},
    ]}
    verification_history = {
        "verification_status": "failed",
        "verification_round": 3,
        "max_rounds": 3,
        "stop_reason": None,
        "repair_tasks": [{"id": "RP-1", "title": "fix VP-006"},
                         {"id": "RP-2", "title": "fix VP-010"}],
    }

    card = build_progress_card(
        "test-terminal-failed", summary, progress=progress,
        verification_history=verification_history,
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in card["elements"]
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)

    # Header warning
    assert "failed" in combined, (
        "Plan stage 'failed' must be surfaced in body. "
        "Operators cannot tell from the header alone what action to take."
    )
    # Queued repair tasks surfaced
    assert "RP-1" in combined
    assert "RP-2" in combined
    # Recovery endpoints
    assert "/api/verification/{plan_id}/reset_rounds" in combined, (
        "Reset-max-rounds endpoint must appear in guidance block."
    )
    assert "/api/plan/{plan_id}/accept_failure" in combined, (
        "Accept-failure endpoint must appear in guidance block."
    )
    # Copy-paste hint
    assert "重跑验证" in combined or "接受失败" in combined, (
        "Natural-language shortcut hints must appear so the operator "
        "knows they can just tell the assistant."
    )


def test_max_rounds_reached_block_renders_extend_instruction():
    """A plan at ``verification_status=loop_stopped`` with
    ``stop_reason=max_rounds_reached`` must surface an explicit
    "reset_rounds" hint instead of just showing "循环停止".
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "verification_loop_stopped",
            "stage": "verification_loop_stopped",
            "verification": {
                "status": "loop_stopped",
                "round": 3,
                "max_rounds": 3,
                "stop_reason": "max_rounds_reached",
            },
        },
        "tasks": {"total": 30, "completed": 28, "failed": 2,
                  "in_progress": 0, "pending": 0, "skipped": 0},
        "execution": {},
    }
    progress = {"tasks": []}
    verification_history = {
        "verification_status": "loop_stopped",
        "verification_round": 3,
        "max_rounds": 3,
        "stop_reason": "max_rounds_reached",
        "repair_tasks": [],
    }

    card = build_progress_card(
        "test-max-rounds", summary, progress=progress,
        verification_history=verification_history,
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in card["elements"]
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)

    assert "max_rounds" in combined
    assert "/api/verification/{plan_id}/reset_rounds" in combined


def test_unstuck_plan_does_not_render_guidance_block():
    """Sanity test: a plan that's actively executing (NOT stuck at a
    terminal state) must NOT render the unlock-instructions block.
    The block is only for stuck terminal states where the operator
    must intervene.
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "executing",
            "stage": "executing",
            "verification": {
                "status": "failed",
                "round": 1,
                "max_rounds": 3,
                "stop_reason": None,
            },
        },
        "tasks": {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 2, "pending": 0, "skipped": 0},
        "execution": {},
    }
    progress = {"tasks": [{"id": "RP-1", "status": "in_progress",
                            "title": "fix VP-006"}]}
    verification_history = {
        "verification_status": "failed",
        "verification_round": 1,
        "max_rounds": 3,
        "stop_reason": None,
        "repair_tasks": [],
    }

    card = build_progress_card(
        "test-active", summary, progress=progress,
        verification_history=verification_history,
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in card["elements"]
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)

    # Active plan — no unlock-instructions block
    assert "/api/plan/{plan_id}/accept_failure" not in combined


# ---------------------------------------------------------------------------
# Regression C — failed VPs must render titles from vps index
# 2026-09-12: a blocked plan must explain the cause, not just the
# verdict. The progress endpoint emits failed_vps as bare id
# strings (e.g. ["VP-006", "VP-027", "VP-023"]). The previous code
# unpacked ``fid, ftitle, fmethod, flayer = entry, "", "", ""`` and
# rendered an empty title — operators saw three blank "🔴 VP-NNN"
# lines with no clue WHAT failed. Fix: look up the metadata in the
# canonical vps index built earlier in the same loop.
# ---------------------------------------------------------------------------


def test_failed_vps_render_titles_when_progress_uses_bare_id_strings():
    """The progress endpoint emits ``failed_vps`` as a list of bare id
    strings (matches the ``progress_state`` JSON column contract). The
    renderer must look up each id in the canonical ``vps`` index and
    surface the title so the operator sees what failed.
    """
    from notifications.cards import _verification_sections

    progress = {
        "verification_round": 1,
        "max_rounds": 3,
        "verification_status": "failed",
        "stop_reason": "no_repair_tasks",
        # bare id strings (the actual /progress payload shape)
        "failed_vps": ["VP-006", "VP-027", "VP-023"],
        # canonical vps index has titles
        "vps": [
            {"id": "VP-006", "title": "select_entering_consolidation 函数体对 9/2 commit diff 为空",
             "status": "failed", "actual_result": "函数体新增 VP-013 instrumentation"},
            {"id": "VP-027", "title": "Rust 覆盖率门禁", "status": "failed"},
            {"id": "VP-023", "title": "Nightly CI 全过", "status": "failed"},
        ],
    }
    state = {
        "stage": "failed",
        "current_phase": "failed",
        "verification": {
            "status": "failed",
            "round": 1,
            "max_rounds": 3,
            "stop_reason": "no_repair_tasks",
        },
    }

    elements = _verification_sections(
        progress, True,
        status_from({"state": state}, verification=progress,
                    plan_id="plan-x"),
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    # Every failed VP must surface its title — NOT a blank line.
    assert "select_entering_consolidation 函数体对 9/2 commit diff 为空" in combined
    assert "Rust 覆盖率门禁" in combined
    assert "Nightly CI 全过" in combined
    # The actual_result for VP-006 must be quoted as a reason line.
    assert "新增 VP-013 instrumentation" in combined


def test_failed_vps_render_titles_even_when_vps_index_empty():
    """Defensive case: if the ``vps`` index is missing (e.g. server
    crashed mid-load and the index wasn't built) the renderer must
    still render the id — title may be blank but the id line must
    not be silently dropped.
    """
    from notifications.cards import _verification_sections

    progress = {
        "verification_round": 1,
        "verification_status": "failed",
        "stop_reason": "no_repair_tasks",
        "failed_vps": ["VP-006"],   # bare id
        "vps": [],                  # empty index — defensive case
    }
    state = {
        "stage": "failed",
        "current_phase": "failed",
        "verification": {"status": "failed", "round": 1, "max_rounds": 3},
    }

    elements = _verification_sections(
        progress, True,
        status_from({"state": state}, verification=progress,
                    plan_id="plan-x"),
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)
    # Id line must still appear even when title lookup fails.
    assert "`VP-006`" in combined


# ---------------------------------------------------------------------------
# Regression D — operator-guidance block must surface round status +
# stop_reason + failed-VP titles inline so operators can diagnose
# "round=N vs status=failed" without scrolling.
# 2026-09-12: the operator-guidance block said \"stuck at
# failed\" without explaining that round 1 had already
# finished and recorded failed; without the diagnostic context the
# operator could not tell whether the chain was still running.
# ---------------------------------------------------------------------------


def test_operator_guidance_block_surfaces_round_status_and_stop_reason():
    """The operator-guidance block must include:
      1. "round N/M 已结束 (status=failed), 不是正在跑" so the operator
         can see the round has already concluded
      2. "📎 停止原因: <stop_reason>" so the operator knows the
         specific terminal branch (no_repair_tasks / max_rounds /
         same_failure_repeated etc.)
      3. The failed VP titles inline (not just ids) so the operator
         can see WHICH VPs blocked the chain without scrolling
    """
    from notifications.cards import build_progress_card

    summary = {
        "state": {
            "current_phase": "failed",
            "stage": "failed",
            "verification": {
                "status": "failed",
                "round": 1,
                "max_rounds": 3,
                "stop_reason": "no_repair_tasks",
            },
        },
        "tasks": {"total": 30, "completed": 28, "failed": 0,
                  "in_progress": 0, "pending": 0, "skipped": 0},
        "execution": {},
    }
    progress = {"tasks": []}
    verification_history = {
        "verification_status": "failed",
        "verification_round": 1,
        "max_rounds": 3,
        "stop_reason": "no_repair_tasks",
        "failed_vps": ["VP-006", "VP-027", "VP-023"],
        "vps": [
            {"id": "VP-006", "title": "select_entering_consolidation diff 为空",
             "status": "failed"},
            {"id": "VP-027", "title": "Rust 覆盖率门禁", "status": "failed"},
            {"id": "VP-023", "title": "Nightly CI 全过", "status": "failed"},
        ],
        "repair_tasks": [],
    }

    card = build_progress_card(
        "20260101-test", summary, progress=progress,
        verification_history=verification_history,
    )
    text_chunks = [
        e.get("text", {}).get("content", "") for e in card["elements"]
        if isinstance(e, dict) and e.get("tag") == "div"
    ]
    combined = "\n".join(text_chunks)

    # (1) Round-status phrase makes the "finished not running" state obvious
    assert "round 1/3" in combined
    assert "已结束" in combined, (
        "operator-guidance block must say the round has already ended, "
        "not be ambiguous about whether verification is still running"
    )
    # The "已结束，不是正在跑" phrase tells the operator the round has
    # already concluded. The substring "不是正在" appears in this
    # exact phrase so assert on that fragment instead of an
    # exclusion test (which is brittle to phrasing changes).
    assert "不是正在" in combined, (
        "operator-guidance block must explicitly say the round is "
        "NOT currently running so the operator does not mistake "
        "round=1 in the card for an in-progress verification cycle"
    )
    # (2) Stop reason must be inline
    assert "停止原因" in combined
    assert "no_repair_tasks" in combined
    # (3) Failed VP titles must appear in the guidance block
    assert "select_entering_consolidation diff 为空" in combined
    assert "Rust 覆盖率门禁" in combined
    assert "Nightly CI 全过" in combined