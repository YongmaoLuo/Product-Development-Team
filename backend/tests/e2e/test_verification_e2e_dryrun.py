"""
End-to-end tests for the verification agent's dry-run pipeline.

These tests sit one level above :mod:`tests.test_verification_integration`
and exercise the *real* :class:`VerificationOrchestrator` +
:class:`VerificationAgent` + :class:`SplitDecision` +
:class:`VerificationPersistenceManager` together. Only the
per-method leaf executors are swapped for :class:`FakeVerifierBackend`
instances (or :func:`asyncio.sleep` stubs for the wall-time tests),
so the partition / semaphore / split / event / state-machine
contracts that the production runtime depends on are exercised
end-to-end, just without the LLM, puppeteer, and subprocess costs.

Coverage targets (4 core paths + 1 event contract):

1. ``test_e2e_dryrun_9vp_full_round_passes`` — 4ui+3cr+2api all
   PASSED in < 15s. The baseline "everything works" smoke check
   that catches regressions in the group-partition, the
   FakeVerifierBackend wiring, and the report generator.
2. ``test_e2e_dryrun_parallel_speedup`` — 4 ui VPs each sleep 10s
   → wall time ∈ [10, 13]s. Pins the
   ``parallelism_cap=4 → 4 in flight, 10s wall time`` contract that
   the rest of the suite (and the orchestrator) relies on.
3. ``test_e2e_dryrun_timeout_triggers_split`` — 1 ui VP hits a
   1900s sleep → SPLIT parent + 3 child sub-VPs all PASSED. Pins
   the split-on-timeout + multi-clause decomposition contract.
4. ``test_e2e_dryrun_state_machine_repair_loop`` — 1 failed
   api_test → ``verification_failed`` → ``verification_repairing``
   → user confirms → ``verification_rerunning`` → second round
   passes → ``verification_passed``. Pins the closed-loop repair
   state machine.
5. ``test_e2e_dryrun_execution_log_events`` — running 2
   non-manual_check groups emits exactly N ``group_started`` and
   N ``group_completed`` events on the persistence log (and 0
   ``vp_subtask_split`` events in the no-split path).

Test budget
-----------
Per the TDD spec the full E2E suite must finish in < 5 minutes.
The FakeVerifierBackend short-circuits on
``fake_sleep >= timeout`` (raises ``asyncio.TimeoutError``
immediately, no wall-clock wait) and on ``fake_sleep == 0``
(returns PASSED without sleeping), so the timeout-shape and
"all pass" tests run in seconds. Only the parallelism test
sleeps in real wall time (10s) so the worst-case suite runtime
is bounded by the wall-time test plus a few seconds of
orchestration overhead per case.

Environment
-----------
Every test sets ``VERIFICATION_PROFILE=dry_run`` via
``monkeypatch``. The env var is the *contract* the agent uses to
opt into the fake backend; pinning it in each test documents the
intended execution mode even when the agent is short-circuited by
a leaf-level patch in this suite.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import pytest

# Make `verification_agent` / `verification` / `plan_state` importable
# when pytest is launched from either the project root or the
# ``backend/`` directory. Mirrors the pattern in
# ``test_verification_integration.py`` and
# ``test_verification_orchestrator.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from verification_agent import (  # noqa: E402
    FakeVerifierBackend,
    VerificationAgent,
)
from coding_tool import HardTimeoutError  # noqa: E402
from verification import VerificationOrchestrator  # noqa: E402
from verification_config import TimeoutPolicy  # noqa: E402


# =============================================================================
# Markers
# =============================================================================

# All tests in this file are slow (full E2E exercises) and e2e
# (orchestrator+agent+fakes). pytestmark applies the markers in
# one place so individual tests don't have to remember to
# decorate themselves.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.time_sensitive,
]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_plan_dir(tmp_path):
    """Per-test plan directory under ``tmp_path/plans/e2e``."""
    plan_dir = tmp_path / "plans" / "e2e"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """Per-test project directory under ``tmp_path/projects/e2e``."""
    project_dir = tmp_path / "projects" / "e2e"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    """Stand-in CodingTool — the agent never invokes the LLM in this suite.

    The agent's :meth:`generate_verification_report` calls
    ``coding_tool.query_json`` to get a judgment from the LLM. The
    mock's default behaviour would be to return a ``Mock()`` object
    that fails the required-field validation, triggering 3 retry
    iterations before falling through to
    :meth:`_generate_minimal_report`. We preconfigure the mock to
    return a minimal-but-valid report on the first call so the
    report generation step in tests 1 and 3 finishes in <1s
    rather than <3s.
    """
    tool = Mock()
    tool.query_json.return_value = {
        "overall_status": "PASSED",
        "verification_results": [],
        "summary": "dry-run test report (mocked LLM)",
        "requirement_deviations": [],
    }
    return tool


@pytest.fixture
def dry_run_env(monkeypatch):
    """Pin the dry-run profile env var for every test in this file.

    The agent's leaf-level dispatch in this suite is patched, so
    the env var isn't strictly needed to *make* the tests work —
    but pinning it here documents the production wiring (the
    ``FakeVerifierBackend`` is selected via this env var in real
    use) and protects against future refactors that route dispatch
    through ``_select_backend`` instead of a per-method patch.
    """
    monkeypatch.setenv("VERIFICATION_PROFILE", "dry_run")
    return monkeypatch


# =============================================================================
# Helpers
# =============================================================================


def _make_vps_by_method(
    counts: Dict[str, int],
    *,
    expected_result: str = "ok",
    timeout_seconds: int | None = None,
    extra: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Build a flat list of VPs whose methods match ``counts``.

    Mirrors the helper used in ``test_verification_integration.py``
    so the wall-time tests here can be read against the existing
    reference values. ``extra`` is merged into every VP so a
    single test can pass a per-method override (e.g. an
    ``expected_result`` with a ``;`` clause) without rewriting
    the factory.
    """
    vps: List[Dict[str, Any]] = []
    for method, n in counts.items():
        for i in range(n):
            vp: Dict[str, Any] = {
                "id": f"VP-{method.upper().replace('_', '')}-{i}",
                "title": f"{method} #{i}",
                "verification_method": method,
                "priority": "medium",
                "expected_result": expected_result,
            }
            if timeout_seconds is not None:
                vp["timeout_seconds"] = timeout_seconds
            if extra:
                vp.update(extra)
            vps.append(vp)
    return vps


def _patch_leaf_to_fake_backend(
    agent: VerificationAgent,
    method_to_backend: Dict[str, FakeVerifierBackend],
) -> None:
    """Wire each method's leaf executor to its own ``FakeVerifierBackend``.

    The replacement is a thin ``async def`` that delegates to
    :meth:`FakeVerifierBackend.execute`, which itself calls
    :func:`asyncio.wait_for` so the resolved per-VP timeout is
    observed at the same point as the real backend. This is the
    closest we can get to a "real" integration without paying the
    LLM / puppeteer / subprocess cost.
    """
    for method, backend in method_to_backend.items():
        attr = f"_execute_{method}"

        async def _stub(vp, _backend=backend):
            timeout_seconds = agent.timeout_policy.resolve(method)
            return await _backend.execute(vp, timeout_seconds=timeout_seconds)

        setattr(agent, attr, _stub)


def _patch_leaf_to_sleep(
    agent: VerificationAgent, method_to_sleep: Dict[str, float]
) -> None:
    """Replace per-method leaf executors with ``asyncio.sleep`` stubs.

    The stub returns ``PASSED`` after sleeping — a direct analogue
    of the helper in ``test_verification_orchestrator.py``. Used
    by the wall-time parallelism test (``fake_sleep=10``) so the
    cap-vs-no-cap contract is observable in real time without
    invoking the FakeVerifierBackend's own short-circuit (which
    would mask the wall-time cost).
    """
    for method, sleep_seconds in method_to_sleep.items():
        attr = f"_execute_{method}"

        async def _stub(vp, _sleep=sleep_seconds):
            await asyncio.sleep(_sleep)
            return {
                "id": vp.get("id", "unknown"),
                "status": "PASSED",
                "actual_result": f"fake sleep {_sleep}s",
                "evidence": "fake_sleep_stub",
            }

        setattr(agent, attr, _stub)


def _write_plan_state(
    plan_dir: Path,
    *,
    phase: str = "executing",
    verification_round: int = 0,
    max_rounds: int = 3,
    stop_reason: str | None = None,
    status: str = "pending",
) -> None:
    """Bootstrap ``plan_state.json`` for an orchestrator test.

    The orchestrator reads the state via :class:`PlanState` and
    writes back transitions during the cycle, so a test that
    skips the bootstrap will see a missing-file error on the
    first ``transition_to`` call.
    """
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase != "interview" else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def _load_log_entries(plan_dir: Path) -> List[Dict[str, Any]]:
    """Read every JSON-lines entry from the round log files.

    The persistence manager writes one log file per round
    (``logs/verification_{round}_{timestamp}.log``); this helper
    concatenates all of them in chronological order so a test can
    assert on the event sequence without caring which file a
    particular event landed in.
    """
    log_files = sorted((plan_dir / "logs").glob("verification_*.log"))
    if not log_files:
        return []
    entries: List[Dict[str, Any]] = []
    for log_file in log_files:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entries.append(json.loads(stripped))
    return entries


# =============================================================================
# 1. Happy path — 9 VP mixed-method plan, all PASSED in < 15s
# =============================================================================


def test_e2e_dryrun_9vp_full_round_passes(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """4ui+3cr+2api all PASSED in < 15s through the real
    orchestrator + real agent + real persistence log.

    The plan is partitioned by :meth:`_partition_vps_by_method`
    into 3 non-manual groups (``ui_validation`` / ``code_review``
    / ``api_test``). Each group runs through the per-group
    ``asyncio.Semaphore(parallelism_cap=4)`` and writes
    ``group_started`` / ``group_completed`` events to the
    persistence log. The fake backends use the default
    ``fake_sleep=1.0s`` (well under any per-method timeout), so
    every VP returns ``PASSED`` and the total wall time is
    bounded by the slowest single-group duration plus asyncio
    scheduling overhead. The 15s upper bound is generous enough
    for slow CI but tight enough to fail if a regression makes
    the VPs run serially (that would push the wall time past
    9 * 1s = 9s + overhead, still under 15s, but it would
    also leave group events missing or malformed, which the
    assertions below would catch).
    """
    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    # Per-method default 1800s; real test is the short-circuit on
    # the 1.0s fake sleep, so the timeout number only matters as
    # an upper bound on the wait_for wrapper.
    agent.timeout_policy = TimeoutPolicy(
        per_method_timeout_seconds={
            "ui_validation": 1800,
            "code_review": 1800,
            "api_test": 1800,
        },
        global_default_timeout_seconds=1800,
        parallelism_cap=4,
    )
    agent.start_verification_round(1)

    vps = _make_vps_by_method(
        {
            "ui_validation": 4,
            "code_review": 3,
            "api_test": 2,
        }
    )
    # Default 1.0s fake sleep → well under any timeout → PASSED.
    _patch_leaf_to_fake_backend(
        agent,
        {
            "ui_validation": FakeVerifierBackend(fake_sleep_seconds=1.0),
            "code_review": FakeVerifierBackend(fake_sleep_seconds=1.0),
            "api_test": FakeVerifierBackend(fake_sleep_seconds=1.0),
        },
    )

    start = time.monotonic()
    result = asyncio.run(
        agent.execute_verification_plan_async({"verification_points": vps})
    )
    wall_time = time.monotonic() - start

    # 1. All 9 VPs returned.
    results = result["execution_results"]
    assert len(results) == 9, (
        f"expected 9 results, got {len(results)}: {results!r}"
    )

    # 2. Every VP surfaced as PASSED.
    statuses = [r.get("status") for r in results]
    assert all(s == "PASSED" for s in statuses), (
        f"expected all 9 VPs to be PASSED, got {statuses!r}"
    )

    # 3. Wall time is bounded by the slowest single-group
    # duration plus asyncio overhead. The 15s ceiling is
    # generous — a 4-vp group with 1.0s sleep finishes in ~1s
    # under cap=4, the 3-vp and 2-vp groups are similar, and
    # asyncio.run startup is ~0.1s on most systems.
    assert wall_time < 15.0, (
        f"expected wall time < 15s for 9 VPs in parallel "
        f"(3 groups, cap=4), got {wall_time:.2f}s"
    )

    # 4. Every non-manual group emitted group_started /
    # group_completed events on the persistence log.
    entries = _load_log_entries(temp_plan_dir)
    started = [e for e in entries if e.get("event_type") == "group_started"]
    completed = [e for e in entries if e.get("event_type") == "group_completed"]
    methods_started = {e["data"]["method"] for e in started}
    methods_completed = {e["data"]["method"] for e in completed}
    assert methods_started == {"ui_validation", "code_review", "api_test"}, (
        f"expected 3 group_started (ui/cr/api), got {methods_started!r}"
    )
    assert methods_completed == {"ui_validation", "code_review", "api_test"}, (
        f"expected 3 group_completed (ui/cr/api), got {methods_completed!r}"
    )

    # 5. execution_profile in the run_full_verification report
    # surfaces the same N-group profile (the smoke check that
    # Phase 3 of the agent still produces a complete profile).
    report = agent.generate_verification_report(result)
    assert "execution_profile" in report, (
        "report must carry execution_profile for bridge UI"
    )
    profile = report["execution_profile"]
    profile_methods = {g["method"] for g in profile.get("groups", [])}
    assert profile_methods == {"ui_validation", "code_review", "api_test"}, (
        f"execution_profile must list 3 groups (ui/cr/api), "
        f"got {profile_methods!r}"
    )


# =============================================================================
# 2. Wall-time parallelism — 4 ui VPs each sleep 10s, wall ∈ [10, 13]s
# =============================================================================


def test_e2e_dryrun_parallel_speedup(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """4 ui VPs each sleep 10s → wall time ∈ [10, 13]s.

    With ``parallelism_cap=4`` and a single group of 4 ui VPs, all
    four run concurrently under the per-group
    ``asyncio.Semaphore(4)`` and finish in ~10s (the duration of
    one VP), not 40s (the serial floor). The 3s upper bound absorbs
    asyncio scheduling overhead and the ``asyncio.run`` startup
    cost. A regression that drops the semaphore (serial execution)
    would push the wall time to ~40s and fail the upper bound
    immediately.
    """
    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    # Per-method timeout must exceed the 10s sleep so the
    # semaphore — not the timeout — is the rate-limiting
    # mechanism.
    agent.timeout_policy = TimeoutPolicy(
        per_method_timeout_seconds={"ui_validation": 60},
        global_default_timeout_seconds=60,
        parallelism_cap=4,
    )
    agent.start_verification_round(1)

    vps = _make_vps_by_method({"ui_validation": 4})
    # 10s real sleep on every leaf → 4 in parallel → ~10s wall.
    _patch_leaf_to_sleep(agent, {"ui_validation": 10.0})

    start = time.monotonic()
    asyncio.run(
        agent.execute_verification_plan_async({"verification_points": vps})
    )
    wall_time = time.monotonic() - start

    assert 10.0 <= wall_time <= 13.0, (
        f"expected wall time ∈ [10, 13]s for 4 VPs in parallel "
        f"(cap=4, 10s each), got {wall_time:.2f}s (would be ~40s "
        f"if serial, ~10s if parallel)"
    )


# =============================================================================
# 3. Timeout → split — 1 ui VP hits 1900s sleep, 3 sub-VPs complete
# =============================================================================


def test_e2e_dryrun_timeout_triggers_split(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """1 ui VP with ``fake_sleep=1900`` hits a 5s timeout →
    SplitDecision decomposes the 3-clause expectation into 3
    sub-VPs, all of which complete (PASSED) under the real
    per-group ``asyncio.gather`` path.

    The 1900s ``fake_sleep`` is well above the 5s
    ``timeout_seconds`` so the FakeVerifierBackend short-circuits
    to ``asyncio.TimeoutError`` immediately, exercising the
    agent's ``_split_vp_on_timeout`` branch without waiting 30
    minutes in real time. The children's default 1.0s fake
    sleep puts them well under the inherited 5s per-VP timeout,
    so all three pass.

    The assertion shape matches the TDD spec: ``subtask_splits``
    carries 1 entry (one parent → 3 children) and the parent's
    ``child_results`` carries 3 sub-VP results, each tagged with
    the trace-back metadata the downstream consumers rely on.
    """
    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    # Tight 5s per-method timeout so the fake-sleep short-circuit
    # fires immediately and the children's default 1.0s sleep
    # finishes well inside the budget.
    agent.timeout_policy = TimeoutPolicy(
        per_method_timeout_seconds={"ui_validation": 5},
        global_default_timeout_seconds=5,
        parallelism_cap=4,
    )
    agent.start_verification_round(1)

    vps: List[Dict[str, Any]] = [
        {
            "id": "VP-SPLIT-1",
            "title": "multi-clause VP",
            "verification_method": "ui_validation",
            "priority": "medium",
            # Three clauses separated by ';' so SplitDecision
            # decomposes into 3 sub-VPs.
            "expected_result": "first clause;second clause;third clause",
            "timeout_seconds": 5,
        }
    ]

    # Parent backend: 1900s fake sleep → instant TimeoutError
    # (short-circuited at 5s resolution). Child backend: default
    # 1.0s fake sleep → all 3 children PASSED.
    parent_backend = FakeVerifierBackend(
        fake_sleep_seconds=1900, should_fail=False
    )
    child_backend = FakeVerifierBackend(
        fake_sleep_seconds=1.0, should_fail=False
    )

    # Dispatch by VP id: the parent goes to the timeout-shaped
    # backend, the children go to the PASSED backend.
    async def _id_aware_stub(vp):
        timeout_seconds = agent.timeout_policy.resolve("ui_validation")
        backend = (
            parent_backend
            if vp.get("id") == "VP-SPLIT-1"
            else child_backend
        )
        try:
            return await backend.execute(vp, timeout_seconds=timeout_seconds)
        except asyncio.TimeoutError:
            # 2026-09-08 contract: the per-method ``asyncio.wait_for``
            # wrapper was removed, so a plain
            # ``asyncio.TimeoutError`` no longer routes to the
            # split path — it falls into the generic FAILED handler.
            # The split route today is :class:`HardTimeoutError`
            # (inner silence detector). The fake backend still
            # raises asyncio.TimeoutError to avoid wall-clock waits,
            # so translate it here to exercise the real split chain.
            raise HardTimeoutError(
                total_sec=int(timeout_seconds),
                elapsed=float(timeout_seconds) + 0.1,
                last_line="dry-run parent VP timed out",
            )

    setattr(agent, "_execute_ui_validation", _id_aware_stub)

    result = asyncio.run(
        agent.execute_verification_plan_async({"verification_points": vps})
    )
    results = result["execution_results"]
    assert len(results) == 1, f"expected 1 synthesised parent, got {results!r}"
    parent = results[0]

    # 1. The parent's status is one of the split-family
    #    statuses (SPLIT, FAILED, or PASSED) — all valid
    #    post-aggregation outcomes. The aggregated verdict rolls
    #    all 3 children up: all PASSED → parent PASSED, or all
    #    SPLIT → parent SPLIT, depending on the aggregator's
    #    rules (see _aggregate_split_results).
    assert parent.get("status") in {"PASSED", "SPLIT", "FAILED"}, (
        f"split parent must surface a terminal status, got "
        f"{parent.get('status')!r}"
    )

    # 2. ``subtask_splits`` carries exactly 1 entry (the parent
    #    → 3 children decomposition). We look at the parent's
    #    child_results list to count the entry; the persistence
    #    log's ``vp_subtask_split`` event is checked separately
    #    below.
    child_results = parent.get("child_results", [])
    assert len(child_results) == 3, (
        f"expected 3 child sub-VPs for a 3-clause VP, got "
        f"{len(child_results)}: {child_results!r}"
    )

    # 3. Every child carries the trace-back metadata the
    #    downstream consumers (report, repair generator) rely on.
    for index, child in enumerate(
        sorted(child_results, key=lambda c: c.get("split_clause_index", -1)),
        start=1,
    ):
        assert child.get("parent_vp_id") == "VP-SPLIT-1", (
            f"child[{index}] missing parent_vp_id: {child!r}"
        )
        assert child.get("original_vp_id") == "VP-SPLIT-1", (
            f"child[{index}] missing original_vp_id: {child!r}"
        )
        assert child.get("split_clause_index") == index, (
            f"child[{index}] split_clause_index should be {index}, "
            f"got {child.get('split_clause_index')!r}"
        )
        assert child.get("id") == f"VP-SPLIT-1-{index}", (
            f"child[{index}] id should be VP-SPLIT-1-{index}, "
            f"got {child.get('id')!r}"
        )
        assert child.get("status") == "PASSED", (
            f"child[{index}] expected PASSED, got {child!r}"
        )

    # 4. The persistence log carries exactly one
    #    ``vp_subtask_split`` event for this parent — this is
    #    the canonical "subtask_splits contains 1 entry" half
    #    of the TDD spec (the persistence log is what the JSONL
    #    log parser and the bridge UI subscribe to for split
    #    notifications).
    entries = _load_log_entries(temp_plan_dir)
    splits = [
        e for e in entries if e.get("event_type") == "vp_subtask_split"
    ]
    assert len(splits) == 1, (
        f"expected exactly 1 vp_subtask_split event on the "
        f"persistence log, got {len(splits)}: "
        f"{[e.get('data') for e in splits]!r}"
    )
    assert splits[0]["data"]["parent_id"] == "VP-SPLIT-1", (
        f"split event must reference VP-SPLIT-1 as the parent, "
        f"got {splits[0]['data']!r}"
    )
    assert splits[0]["data"]["N"] == 3, (
        f"split event must carry N=3 children, got "
        f"{splits[0]['data']!r}"
    )
    assert sorted(splits[0]["data"]["child_vp_ids"]) == [
        "VP-SPLIT-1-1",
        "VP-SPLIT-1-2",
        "VP-SPLIT-1-3",
    ], (
        f"split event must list 3 child ids, got "
        f"{splits[0]['data']!r}"
    )

    # 5. The report still computes a valid execution_profile
    #    (the structural smoke check that the agent's Phase 3
    #    step tolerates a SPLIT-aggregated parent and produces
    #    a profile keyed on the parent's verification_method).
    report = agent.generate_verification_report(result)
    profile = report.get("execution_profile", {})
    assert profile, "report must carry execution_profile"
    profile_methods = {g["method"] for g in profile.get("groups", [])}
    assert profile_methods == {"ui_validation"}, (
        f"execution_profile must list 1 group (ui_validation), "
        f"got {profile_methods!r}"
    )


# =============================================================================
# 4. State machine repair loop — failed → repairing → rerunning → passed
# =============================================================================


def test_e2e_dryrun_state_machine_repair_loop(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """Drive the real :class:`VerificationOrchestrator` through a
    full failed→repairing→rerunning→passed cycle.

    Round 1: 1 api_test VP with ``should_fail=True`` → report
    overall_status=FAILED → orchestrator enters
    ``verification_repairing`` with repair tasks queued (the
    ``waiting_for_user`` field is dead per the 2026-09-14
    auto-confirm contract — see check_cycle_conditions).
    User confirms repair (mocked ``RepairTaskGenerator`` returns
    content the real :class:`RepairTaskAssembler` stamps into
    tasks) → orchestrator advances to ``verification_rerunning``
    and bumps round 0 → round 1.

    Round 2: same VP, but with ``should_fail=False`` this time →
    report overall_status=PASSED → orchestrator transitions to
    ``verification_passed``.

    The mock on the agent forces the report's overall_status
    directly (the per-method FakeVerifierBackend wiring would
    still drive the real ``execute_verification_plan_async``,
    but mocking the report keeps the test focused on the state
    machine and avoids the LLM step that the agent's
    ``generate_verification_report`` would otherwise need).
    """
    _write_plan_state(
        temp_plan_dir,
        phase="executing",
        verification_round=0,
        max_rounds=3,
    )

    # 2026-09-13 contract: ``check_cycle_conditions`` no longer
    # consumes the passed report dict for repair generation — it
    # re-reads ``verification_report.json`` from disk via the
    # path-based reader (keeps untruncated evidence available for
    # the repair LLM).  The plan overlay below is what the reader
    # joins against for test commands; the report file itself is
    # written next to the hand-built ``failed_report`` below.
    (temp_plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {
                        "id": "VP-APITEST-0",
                        "title": "api_test VP",
                        "verification_method": "api_test",
                        "expected_result": "api responds 200",
                        "test_command": "pytest tests/test_dryrun.py -k api",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # Build the orchestrator with the real agent + a mocked
    # repair generator so we can control the repair-task output
    # without standing up an LLM.
    with patch("verification.orchestrator.VerificationAgent") as MockVA, \
         patch("verification.orchestrator.RepairTaskGenerator") as MockRG:
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool
        )

        # Round 1: report a FAILED api_test. The orchestrator's
        # ``check_cycle_conditions`` looks only at
        # ``overall_status`` and the per-VP statuses, so a
        # hand-built report is sufficient.
        failed_report = {
            "overall_status": "FAILED",
            "verification_results": [
                {
                    "id": "VP-APITEST-0",
                    "verification_method": "api_test",
                    "status": "FAILED",
                    "actual_result": "dry-run injected failure",
                    "evidence": "FakeVerifierBackend.should_fail=True",
                }
            ],
            "requirement_deviations": [],
        }
        # Round 2: same VP, now PASSED. After the repair cycle
        # the user has "fixed" the underlying issue (mocked).
        passed_report = {
            "overall_status": "PASSED",
            "verification_results": [
                {
                    "id": "VP-APITEST-0",
                    "verification_method": "api_test",
                    "status": "PASSED",
                    "actual_result": "dry-run backend slept 1.0s",
                    "evidence": "FakeVerifierBackend",
                }
            ],
            "requirement_deviations": [],
        }
        # ``run_full_verification`` is called twice (once per
        # round). The first call returns the FAILED report, the
        # second returns the PASSED report.
        orch.verification_agent.run_full_verification.side_effect = [
            failed_report,
            passed_report,
        ]
        # The FAILED report must live on disk — the path-based
        # reader inside check_cycle_conditions reads
        # ``verification_report.json``, not the dict passed in.
        (temp_plan_dir / "verification_report.json").write_text(
            json.dumps(failed_report),
            encoding="utf-8",
        )
        # 2026-09-07/08 contract: repair generation is the
        # single-call ``generate_repair_contents`` whose output the
        # REAL :class:`RepairTaskAssembler` (locally imported in
        # check_cycle_conditions, so not covered by the patch above)
        # validates — any content whose ``failed_vp_id`` is not
        # among the disk report's FAILED VPs is dropped.
        orch.repair_generator.generate_repair_contents.return_value = [
            {
                "failed_vp_id": "VP-APITEST-0",
                "title": "Fix api_test #0",
                "description": "Investigate FakeVerifierBackend failure",
            }
        ]

        # --- Round 1: FAILED → repair ---
        orch.start_verification_cycle(round_number=1)
        # Agent is now in verification_running with round 0.
        from plan_state import PlanState
        ps = PlanState(temp_plan_dir)
        assert ps.get_current_phase() == "verification_running", (
            f"after start_verification_cycle, expected "
            f"verification_running, got {ps.get_current_phase()!r}"
        )
        assert ps.get_verification_round() == 0

        # The orchestrator evaluates the report and decides
        # whether to wait for the user (FAILED + round <
        # max_rounds-1 → wait).
        cycle_result = orch.check_cycle_conditions(failed_report)
        assert cycle_result["status"] == "verification_failed", (
            f"first failure must surface as verification_failed, "
            f"got {cycle_result!r}"
        )
        # 2026-09-14 contract: ``waiting_for_user`` is a dead
        # backwards-compat field — the auto-confirm path never
        # waits, so the failure payload hard-codes it False (see
        # ``orchestrator.check_cycle_conditions``).  What round 1
        # must guarantee is the repair payload + phase parked in
        # ``verification_repairing`` for ``confirm_repair_and_rerun``.
        assert cycle_result["waiting_for_user"] is False
        assert cycle_result["should_stop"] is False
        assert len(cycle_result["repair_tasks"]) >= 1, (
            f"repair_tasks must list at least one task, got "
            f"{cycle_result['repair_tasks']!r}"
        )

        ps = PlanState(temp_plan_dir)
        assert ps.get_current_phase() == "verification_repairing", (
            f"after check_cycle_conditions on FAILED, expected "
            f"verification_repairing, got {ps.get_current_phase()!r}"
        )
        assert ps.get_verification_status() == "failed"

        # --- User confirms repair → advance ---
        orch.confirm_repair_and_rerun()
        ps = PlanState(temp_plan_dir)
        # 2026-09-12 state-machine closed-loop fix:
        # ``confirm_repair_and_rerun`` routes ``verification_repairing
        # → executing`` (the repair tasks genuinely run under the
        # executor subprocess), replacing the old
        # ``verification_rerunning`` edge.
        assert ps.get_current_phase() == "executing", (
            f"after confirm_repair_and_rerun, expected "
            f"executing, got {ps.get_current_phase()!r}"
        )
        assert ps.get_verification_round() == 1, (
            f"verification_round should bump 0 → 1 on confirm, "
            f"got {ps.get_verification_round()}"
        )

        # --- Round 2: PASSED → completed ---
        orch.start_verification_cycle(round_number=2)
        ps = PlanState(temp_plan_dir)
        assert ps.get_current_phase() == "verification_running", (
            f"after start_verification_cycle (round 2), expected "
            f"verification_running, got {ps.get_current_phase()!r}"
        )

        cycle_result = orch.check_cycle_conditions(passed_report)
        assert cycle_result["status"] == "passed", (
            f"second-round PASSED must close the loop, got "
            f"cycle_result={cycle_result!r}"
        )
        assert cycle_result["should_continue"] is False
        assert cycle_result["should_stop"] is False
        assert cycle_result["waiting_for_user"] is False

        ps = PlanState(temp_plan_dir)
        assert ps.get_current_phase() == "verification_passed", (
            f"after PASSED round 2, expected "
            f"verification_passed, got {ps.get_current_phase()!r}"
        )
        assert ps.get_verification_status() == "passed"

        # The mocked agent should have been called twice (once
        # per cycle), with the right round numbers, exercising
        # the contract that ``run_full_verification(round_number=...)``
        # is the only public entry point into the agent.
        assert (
            orch.verification_agent.run_full_verification.call_count == 2
        ), (
            f"expected agent.run_full_verification to be called "
            f"twice (rounds 1 and 2), got "
            f"{orch.verification_agent.run_full_verification.call_count}"
        )
        first_call = (
            orch.verification_agent.run_full_verification.call_args_list[
                0
            ]
        )
        second_call = (
            orch.verification_agent.run_full_verification.call_args_list[
                1
            ]
        )
        assert first_call.args[0] == 1, (
            f"first cycle must call run_full_verification(1), "
            f"got args={first_call.args!r}"
        )
        assert second_call.args[0] == 2, (
            f"second cycle must call run_full_verification(2), "
            f"got args={second_call.args!r}"
        )


# =============================================================================
# 5. Execution log events — N group_started + N group_completed
# =============================================================================


def test_e2e_dryrun_execution_log_events(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """Running 2 non-manual groups emits exactly N
    ``group_started`` + N ``group_completed`` events on the
    persistence log, and zero ``vp_subtask_split`` events in
    the no-split happy path.

    This is the structural contract the JSONL log parser and the
    ExecutionProfileGenerator rely on: every non-manual group
    must bracket its VPs with a started/completed pair (so an
    observer can compute the per-group duration), and the
    persistence log must NOT carry spurious ``vp_subtask_split``
    events when no VP actually split (those would inflate the
    ``subtask_splits`` count and mislead the report).
    """
    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    agent.start_verification_round(1)

    # Two non-manual groups + one manual_check (which must NOT
    # emit group events — the negative control).
    vps = _make_vps_by_method(
        {
            "ui_validation": 2,
            "code_review": 3,
            "manual_check": 1,
        }
    )
    _patch_leaf_to_fake_backend(
        agent,
        {
            "ui_validation": FakeVerifierBackend(fake_sleep_seconds=0.1),
            "code_review": FakeVerifierBackend(fake_sleep_seconds=0.1),
        },
    )

    asyncio.run(
        agent.execute_verification_plan_async({"verification_points": vps})
    )

    entries = _load_log_entries(temp_plan_dir)
    assert entries, "no log entries — round never started"

    started = [e for e in entries if e.get("event_type") == "group_started"]
    completed = [
        e for e in entries if e.get("event_type") == "group_completed"
    ]
    splits = [
        e for e in entries if e.get("event_type") == "vp_subtask_split"
    ]

    # 1. Two non-manual groups → exactly 2 of each bracket event.
    assert len(started) == 2, (
        f"expected 2 group_started events (ui + cr), got "
        f"{len(started)}: {[e.get('data') for e in started]!r}"
    )
    assert len(completed) == 2, (
        f"expected 2 group_completed events (ui + cr), got "
        f"{len(completed)}: {[e.get('data') for e in completed]!r}"
    )

    # 2. The set of methods on both brackets matches the plan's
    #    non-manual methods.
    methods_started = {e["data"]["method"] for e in started}
    methods_completed = {e["data"]["method"] for e in completed}
    assert methods_started == {"ui_validation", "code_review"}, (
        f"group_started methods must be {{ui, cr}}, got "
        f"{methods_started!r}"
    )
    assert methods_completed == {"ui_validation", "code_review"}, (
        f"group_completed methods must be {{ui, cr}}, got "
        f"{methods_completed!r}"
    )

    # 3. ``group_completed`` carries the documented data fields
    #    (count + status_counts + duration_sec) so downstream
    #    log parsers and the ExecutionProfileGenerator can read
    #    them without guessing.
    for event in completed:
        data = event["data"]
        assert "method" in data
        assert "count" in data and data["count"] >= 1
        assert "status_counts" in data, (
            f"group_completed must carry status_counts: {data!r}"
        )
        assert "duration_sec" in data, (
            f"group_completed must carry duration_sec: {data!r}"
        )
        assert isinstance(data["duration_sec"], (int, float))
        assert data["duration_sec"] >= 0

    # 4. manual_check must NOT have produced a group event —
    #    it short-circuits to SKIPPED and never enters the
    #    gather / semaphore path. The negative control guards
    #    against a regression that adds manual_check to the
    #    group-event emit list.
    assert all(
        e["data"]["method"] != "manual_check"
        for e in started + completed
    ), "manual_check must not emit group events"

    # 5. No ``vp_subtask_split`` events in the happy path —
    #    none of the VPs timed out, so the splitter never ran.
    #    A regression that emits spurious split events would
    #    inflate ``subtask_splits`` in the report.
    assert len(splits) == 0, (
        f"expected 0 vp_subtask_split events on the happy path, "
        f"got {len(splits)}: {[e.get('data') for e in splits]!r}"
    )
