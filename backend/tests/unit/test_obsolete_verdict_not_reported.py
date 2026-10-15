"""``obsolete`` must be symmetric: execution side AND verdict side.

2026-10-14 production incident (``a private plan``,
round 4). The plan marked VP-009 ``obsolete: true`` / ``obsoleted_round: 3``.
The execution side honoured that — ``_build_items_from_plan`` drops retired
VPs, and round 4's log contains **no** execution event for VP-009, only one
``judgment_heartbeat`` from the Phase-3 supplement review.

The verdict side did not. The round-3 verdict map lives in SQLite
(``plan_verification.verdicts``), and ``VerificationAgent.
_clear_verification_state_files`` only deletes the two legacy JSON sidecars —
so ``_load_or_init_state`` re-hydrates round 3's ``VP-009: FAILED`` into
``_verdicts``. ``collect_verdicts()`` then handed it straight to
``_build_execution_results_from_executor``, the Phase-3 judgment prompt
showed the LLM a FAILED row whose ``actual_result`` was round 3's text, and
the round-4 report shipped ``VP-009: FAILED`` → ``overall_status = FAILED``
→ repair loop that can never converge (VP-028 was DEFERRED *because* of it).

A VP the plan no longer requires must not manufacture failures. These tests
pin both halves of that contract:

  * **retired (``obsolete``)** — never runs, never reported, never counted
    in ``failed_vps``, never shown to the Phase-3 judgment LLM;
  * **superseded (``superseded_by``)** — the split-parent semantics must
    survive: the parent's ``SPLIT`` verdict stays terminal-skipped so the
    parent never re-runs while its children do, and a FAILED verdict on the
    parent is retired out of the failure set instead of re-running the very
    VP the split replaced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)
from verification_agent import VerificationAgent
from verification_executor import VerificationExecutor


PLAN_ID = "plan-obsolete-20261014"

# The plan as it looked at round 4: VP-009 retired by the per-round plan
# delta, VP-028 the Phase-2 full gate that gets deferred behind it.
PLAN_DATA: Dict[str, Any] = {
    "verification_points": [
        {
            "id": "VP-001",
            "title": "健康检查",
            "verification_method": "code_review",
            "priority": "high",
            "expected_result": "PASSED",
        },
        {
            "id": "VP-009",
            "title": "API 边界：窗口内无信号返回 200 + 空集合",
            "verification_method": "api_test",
            "priority": "high",
            "expected_result": "PASSED",
            "obsolete": True,
            "obsolete_reason": "前提失效 —— 判据锚在一个源码里已经死掉的性质上",
            "obsoleted_round": 3,
        },
        {
            "id": "VP-028",
            "title": "端到端链路贯通核验",
            "verification_method": "code_review",
            "priority": "medium",
            "expected_result": "PASSED",
            "verification_phase": 2,
            "phase_order": 1,
        },
    ],
}

# The executor schema the agent converts PLAN_DATA into. Note the live
# converter (``_convert_plan_to_executor_schema``) carries ``obsolete`` but
# drops ``superseded_by`` — see the superseded tests below, which build the
# executor plan directly.
EXECUTOR_PLAN: Dict[str, Any] = {
    "vps": PLAN_DATA["verification_points"]
}

# Round 3's persisted verdict map, exactly as it sits in SQLite today.
ROUND3_VERDICTS: List[Dict[str, Any]] = [
    {
        "vp_id": "VP-001",
        "status": "PASSED",
        "reasons": ["ok"],
        "evidence": {},
    },
    {
        "vp_id": "VP-009",
        "status": "FAILED",
        "reasons": ["[响应体是空数组] json_path $: expected length 7 == 0"],
        "evidence": {"status_code": 200},
        "actual_result": (
            "状态码 200（非 5xx）与 content-type 为 JSON 两条通过，但"
            "「响应体是空数组」断言失败：000001 30min 实际返回 7 条信号"
        ),
    },
]


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    d = tmp_path / PLAN_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def verif_repo(tmp_path: Path) -> VerificationRepository:
    """A real SQLite repo holding round 3's verdict map.

    Not a mock: the bug lives on the ``verif_repo`` hydration branch of
    ``_load_or_init_state``, and a mock would happily agree with whatever
    the fix does.
    """
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(PLAN_ID, verification_status="running")
    for verdict in ROUND3_VERDICTS:
        repo.append_verdict(PLAN_ID, verdict)
    conn.commit()
    return repo


def _executor(plan_dir: Path, runner, repo=None, plan=None) -> VerificationExecutor:
    return VerificationExecutor(
        verification_plan=plan or EXECUTOR_PLAN,
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=runner,
        verif_repo=repo,
    )


def _agent(plan_dir: Path, repo=None) -> VerificationAgent:
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=plan_dir.parent,
        coding_tool=Mock(),
        verif_repo=repo,
    )


# ---------------------------------------------------------------------------
# The execution half — already correct, pinned so the symmetry is visible.
# ---------------------------------------------------------------------------


def test_obsolete_vp_is_not_executed(plan_dir: Path):
    """Execution side already honours ``obsolete`` — this is the other half."""
    executor = _executor(plan_dir, Mock(return_value={"status": "PASSED"}))
    ids = [vp["id"] for vp in executor._build_items_from_plan()]
    assert ids == ["VP-001", "VP-028"], (
        "a retired VP must not be executed — running it manufactures a "
        "failure for something the plan no longer requires"
    )


# ---------------------------------------------------------------------------
# The verdict half — the defect.
# ---------------------------------------------------------------------------


def test_obsolete_failed_verdict_is_not_in_the_failed_set(plan_dir: Path, verif_repo):
    """``_failed_vps`` is the executor's failure set (dashboard, progress
    state, the repair loop's input). A retired VP's stale verdict must not
    be in it — and must not be *reclassified* as passed either."""
    executor = _executor(plan_dir, Mock(return_value={"status": "PASSED"}), verif_repo)

    assert executor.failed_vps == [], (
        f"retired VP-009 leaked into the failure set: {executor.failed_vps!r}"
    )
    assert "VP-009" not in executor.completed_vps
    assert "VP-009" not in executor.skipped_vps


def test_obsolete_failed_verdict_is_not_collected_for_the_report(plan_dir: Path, verif_repo):
    """``collect_verdicts`` is the ONLY thing Phase 3 sees. If the retired
    VP's round-3 verdict survives here, the judgment LLM is handed a FAILED
    row and copies it into the report verbatim."""
    executor = _executor(plan_dir, Mock(return_value={"status": "PASSED"}), verif_repo)

    collected = [v["vp_id"] for v in executor.collect_verdicts()]
    assert "VP-009" not in collected, (
        "the retired VP's stale FAILED verdict reached the report side: "
        f"{collected!r}"
    )
    assert collected == ["VP-001"]


def test_retired_vp_is_absent_from_the_phase3_envelope_and_prompt(plan_dir: Path, verif_repo):
    """End of the chain: the Phase-3 execution envelope and the judgment
    prompt it renders must not mention the retired VP at all."""
    executor = _executor(plan_dir, Mock(return_value={"status": "PASSED"}), verif_repo)
    agent = _agent(plan_dir, verif_repo)

    envelope = agent._build_execution_results_from_executor(PLAN_DATA, executor)

    result_ids = [r["id"] for r in envelope["execution_results"]]
    assert "VP-009" not in result_ids, result_ids
    point_ids = [p["id"] for p in envelope["verification_points"]]
    assert "VP-009" not in point_ids, (
        "the judgment prompt lists every plan VP; a retired VP listed there "
        "with no execution result is an invitation for the LLM to invent "
        "a verdict for it"
    )

    prompt = agent._build_judgment_prompt(envelope)
    assert "VP-009" not in prompt, (
        "the Phase-3 judgment prompt still describes the retired VP"
    )


def test_full_round_resume_does_not_resurrect_the_retired_failure(
    plan_dir: Path, verif_repo
):
    """Production entry: hydrate from SQLite, drive ``executor.run()`` the
    way ``run_full_verification`` does, project into the report envelope.

    This is the round-4 incident end to end, minus the LLM judgment call.
    """
    ran: List[str] = []

    async def _runner(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        # Everything that runs is green: in round 4 the Phase-2 gate (VP-028)
        # deferred *because* VP-009 was FAILED; with the retired VP out of
        # the failure set there is nothing left to defer behind.
        ran.append(vp_node["id"])
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    executor = _executor(plan_dir, _runner, verif_repo)
    agent = _agent(plan_dir, verif_repo)

    asyncio.run(executor.run(max_parallel=1))

    assert "VP-009" not in ran, f"the retired VP ran: {ran!r}"

    envelope = agent._build_execution_results_from_executor(PLAN_DATA, executor)
    statuses = {r["id"]: r["status"] for r in envelope["execution_results"]}
    assert "VP-009" not in statuses, statuses
    assert not [r for r in envelope["execution_results"] if r["status"] == "FAILED"], (
        "a retired VP's stale failure still reaches overall_status"
    )
    assert executor.failed_vps == [], executor.failed_vps


# ---------------------------------------------------------------------------
# superseded_by — the split-parent contract must not regress.
# ---------------------------------------------------------------------------


SPLIT_PLAN: Dict[str, Any] = {
    "vps": [
        {"id": "VP-006", "title": "函数体 diff"},
        {
            "id": "VP-023",
            "title": "Nightly CI 全过",
            "superseded_by": ["VP-023-1", "VP-023-2"],
        },
        {"id": "VP-023-1", "title": "Nightly CI 全过（拆分1）", "parent_vp_id": "VP-023"},
        {"id": "VP-023-2", "title": "Nightly CI 全过（拆分2）", "parent_vp_id": "VP-023"},
    ]
}


def test_superseded_parent_split_verdict_stays_terminal_skipped(plan_dir: Path):
    """The 2026-09-14 contract, unchanged: SPLIT lands in the skipped
    bucket so the parent joins ``completed_set`` and the resumed round runs
    only the children. Re-running the parent is the whole cost the split
    exists to avoid."""
    executor = _executor(
        plan_dir, Mock(), plan={"vps": [{"id": "VP-006"}]}
    )
    executor._verdicts = {
        "VP-006": {"status": "PASSED"},
        "VP-023": {"status": "SPLIT", "reasons": ["split"], "evidence": {}},
    }
    executor._backfill_index_lists_from_verdicts()
    assert "VP-023" in executor.skipped_vps

    # Same, but with the parent actually stamped in the plan.
    executor2 = _executor(plan_dir, Mock(), plan=SPLIT_PLAN)
    executor2._verdicts = {
        "VP-006": {"status": "PASSED"},
        "VP-023": {"status": "SPLIT", "reasons": ["split"], "evidence": {}},
    }
    executor2._backfill_index_lists_from_verdicts()
    assert "VP-023" in executor2.skipped_vps, (
        "a split parent must stay terminal-skipped — otherwise the resume "
        "path re-runs the giant VP the split was meant to replace"
    )
    assert "VP-023" not in executor2.failed_vps


def test_superseded_parent_failed_verdict_neither_fails_nor_re_runs(plan_dir: Path):
    """A stale FAILED on a split parent is not a live failure: the children
    carry the real verdicts. It must leave the failure set AND stay out of
    the re-run set (the parent is replaced by its children, not run beside
    them) — the "父不重跑、子重跑" half of the contract."""
    ran: List[str] = []

    async def _runner(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        ran.append(vp_node["id"])
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    executor = _executor(plan_dir, _runner, plan=SPLIT_PLAN)
    executor._verdicts = {
        "VP-023": {
            "status": "FAILED",
            "reasons": ["round 1 failed"],
            "evidence": {},
        }
    }
    executor._backfill_index_lists_from_verdicts()

    assert "VP-023" not in executor.failed_vps, (
        "a superseded parent must not be counted as a failure — its "
        "children carry the verdict"
    )
    assert "VP-023" in executor.skipped_vps, (
        "and it must stay terminal, or the next round re-runs the exact VP "
        "the split replaced"
    )

    asyncio.run(executor.run(max_parallel=1))
    assert "VP-023" not in ran, f"the superseded parent re-ran: {ran!r}"
    assert set(ran) == {"VP-006", "VP-023-1", "VP-023-2"}, ran


def test_a_clean_plan_is_untouched_by_the_retirement_filter(plan_dir: Path):
    """No marker → nothing is filtered. Guards against the fix being an
    over-broad 'drop anything without a verdict' filter."""
    plan = {"vps": [{"id": "VP-001"}, {"id": "VP-002"}]}
    executor = _executor(plan_dir, Mock(), plan=plan)
    executor._verdicts = {
        "VP-001": {"status": "PASSED"},
        "VP-002": {"status": "FAILED"},
    }
    executor._backfill_index_lists_from_verdicts()

    assert executor.failed_vps == ["VP-002"]
    assert [v["vp_id"] for v in executor.collect_verdicts()] == [
        "VP-001",
        "VP-002",
    ]
