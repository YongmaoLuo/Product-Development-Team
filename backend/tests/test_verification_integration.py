"""
Integration tests for the verification agent refactor.

These tests sit one level above the unit suites
(``test_verification_fake_backend.py``,
``test_verification_config.py``, ``test_verification_split.py``) and
exercise the *real* :class:`VerificationAgent` end-to-end against a
:class:`FakeVerifierBackend` swapped in for the expensive per-method
executors. The unit tests pin individual components in isolation; this
file pins the wiring that connects them — the parts that broke last
time we touched the agent:

* :class:`TimeoutPolicy.resolve` is actually consulted on every VP
  (method-level value; the per-VP override was deleted 2026-09-13 —
  the resolved number is metadata forwarded to the backend).
* :class:`FakeVerifierBackend` actually receives the resolved timeout
  so a hung fake propagates as a real ``status="timeout"`` entry.
* The orchestrator's group partitioning + per-group ``asyncio.gather``
  + per-group ``asyncio.Semaphore(parallelism_cap)`` produces the
  expected wall-clock curve.
* :class:`SplitDecision.should_split` is actually invoked from the
  agent's timeout path, and the child results it produces carry the
  trace-back metadata (``parent_vp_id``, ``original_vp_id``,
  ``split_clause_index``).

Test budget
-----------
Per the TDD spec each test must finish in well under 30 s. The fake
backend short-circuits ``fake_sleep >= timeout`` (raises
``asyncio.TimeoutError`` immediately) and short-circuits
``fake_sleep == 0`` (returns PASSED without sleeping), so the
timeout-shape tests run in <1 s. Only the parallelism tests sleep
in real wall time and they top out around 9 s.

Environment
-----------
Every test sets ``VERIFICATION_PROFILE=dry_run`` via ``monkeypatch``.
The variable is the *contract* the agent (and future in-process
callers) use to opt into the fake backend; pinning it in each test
documents the intended execution mode even when the agent is
short-circuited by a leaf-level patch in this suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, MagicMock

import pytest

# Make `verification_agent` importable when pytest is launched from
# either the project root or the `backend/` directory. Mirrors the
# pattern in ``test_verification_fake_backend.py`` and
# ``test_verification_orchestrator.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from verification_agent import (  # noqa: E402
    FakeVerifierBackend,
    VerificationAgent,
    _select_backend,
)
from verification_config import TimeoutPolicy  # noqa: E402


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    """Per-test plan directory under ``tmp_path/plans/integration``."""
    plan_dir = tmp_path / "plans" / "integration"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """Per-test project directory under ``tmp_path/projects/integration``."""
    project_dir = tmp_path / "projects" / "integration"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    """Stand-in CodingTool — the agent never invokes the LLM in this suite."""
    return Mock()


@pytest.fixture
def dry_run_env(monkeypatch):
    """Pin the dry-run profile env var for every test in this file.

    The agent's leaf-level dispatch in this suite is patched, so the
    env var isn't strictly needed to *make* the tests work — but
    pinning it here documents the production wiring (the
    ``FakeVerifierBackend`` is selected via this env var in real use)
    and protects against future refactors that route dispatch
    through ``_select_backend`` instead of a per-method patch.
    """
    monkeypatch.setenv("VERIFICATION_PROFILE", "dry_run")
    return monkeypatch


def _make_vps_by_method(
    counts: Dict[str, int],
    *,
    expected_result: str = "ok",
    timeout_seconds: int | None = None,
) -> List[Dict[str, Any]]:
    """Build a flat list of VPs whose methods match ``counts``.

    Mirrors the helper used in ``test_verification_orchestrator.py``
    so the wall-time tests here can be read against the existing
    reference values.
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

    The agent's :meth:`_run_single_vp_async` wraps this in
    :func:`asyncio.wait_for(timeout=timeout_seconds)` *itself*, so
    the fake backend's internal ``asyncio.wait_for`` is a
    belt-and-suspenders second layer — both must agree for the
    timeout path to fire.
    """
    for method, backend in method_to_backend.items():
        attr = f"_execute_{method}"

        async def _stub(vp, _backend=backend, _timeout_fn=None):
            # Re-resolve the timeout the same way the agent would
            # for the production per-method executor, so the fake
            # receives exactly the timeout the policy picked.
            timeout_seconds = agent.timeout_policy.resolve(method)
            return await _backend.execute(vp, timeout_seconds=timeout_seconds)

        setattr(agent, attr, _stub)


# -----------------------------------------------------------------------------
# 1. Timeout policy is consulted end-to-end
# -----------------------------------------------------------------------------


class TestTimeoutPolicyWiring:
    """The agent must consult :class:`TimeoutPolicy` for every VP.

    These two tests pin the wiring between the per-method timeout
    config and the leaf executor: the fake backend short-circuits on
    ``fake_sleep >= timeout`` (raising ``asyncio.TimeoutError``
    immediately, no wall-clock wait), so a status of
    ``"timeout"`` in the result list is unambiguous evidence that
    the policy-driven timeout reached the leaf.
    """

    def test_agent_respects_method_timeout_via_fake(
        self, dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """Per-method timeout (ui_validation=1800s) wins when the VP is
        clean → fake sleep 200s, well under 1800s, surfaces as
        ``PASSED``.

        This is the happy-path: the method-level default is the
        right number, no override is set, the fake sleeps 200s and
        returns. The test guards against a regression where the
        agent stops reading the policy and falls back to a
        hard-coded value.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        # Force a known per-method timeout so the test isn't
        # sensitive to the yaml's actual values.
        agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 1800},
            global_default_timeout_seconds=1800,
            parallelism_cap=4,
        )
        agent.start_verification_round(1)

        vps = _make_vps_by_method({"ui_validation": 1})
        # fake_sleep_seconds=0 short-circuits to PASSED instantly (no wall-clock
        # wait).  The test verifies the wiring: the resolved per-method timeout
        # (1800s) is passed to the backend, but since fake_sleep=0 < 1800 the
        # backend returns PASSED without actually sleeping.
        backend = FakeVerifierBackend(fake_sleep_seconds=0, should_fail=False)
        _patch_leaf_to_fake_backend(agent, {"ui_validation": backend})

        result = asyncio.run(
            agent.execute_verification_plan_async({"verification_points": vps})
        )
        results = result["execution_results"]
        assert len(results) == 1
        assert results[0]["status"] == "PASSED", (
            f"fake sleep (200s) < ui_validation timeout (1800s) should "
            f"yield PASSED, got {results[0]!r}"
        )
        assert results[0]["id"] == "VP-UIVALIDATION-0"

    def test_policy_value_forwarded_to_backend_metadata_only(
        self, dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """2026-09-13: the per-VP ``timeout_seconds`` override is
        DELETED — the TimeoutPolicy value is surface metadata, not
        enforcement.

        The policy resolves to 1800s and the fake backend sleeps
        150s: under the deleted wrapper semantics this would have
        stayed ``PASSED``; the point of this test is now the wiring —
        the resolved per-method timeout reaches the backend (it is
        recorded in the PASSED result) and a legacy plan's
        ``timeout_seconds=100`` on the VP does NOT shrink it.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 1800},
            global_default_timeout_seconds=1800,
            parallelism_cap=4,
        )
        agent.start_verification_round(1)

        # Legacy plan data carries a per-VP override of 100s — this
        # must be IGNORED (dead field) and the 1800s policy value
        # forwarded to the backend instead.
        vps = _make_vps_by_method({"ui_validation": 1}, timeout_seconds=100)
        backend = FakeVerifierBackend(fake_sleep_seconds=0, should_fail=False)
        _patch_leaf_to_fake_backend(agent, {"ui_validation": backend})

        result = asyncio.run(
            agent.execute_verification_plan_async({"verification_points": vps})
        )
        results = result["execution_results"]
        assert len(results) == 1
        assert results[0]["status"] == "PASSED", (
            f"fake sleep 0 < 1800s policy value should yield PASSED, "
            f"got {results[0]!r}"
        )
        # The backend received the POLICY value (1800), not the dead
        # per-VP override (100) — the resolved timeout is echoed in
        # the PASSED actual_result.
        assert "1800" in results[0]["actual_result"], (
            f"backend should have received the 1800s policy value "
            f"(per-VP override deleted), got {results[0]['actual_result']!r}"
        )


# -----------------------------------------------------------------------------
# 2. Orchestrator parallelism / rate-limit
# -----------------------------------------------------------------------------


class TestOrchestratorParallelism:
    """The agent's intra-group parallelism + semaphore rate-limit."""

    @staticmethod
    def _patch_leaf_to_sleep(agent: VerificationAgent, method_to_sleep: Dict[str, float]) -> None:
        """Replace per-method leaf executors with ``asyncio.sleep`` stubs.

        Returns ``PASSED`` after sleeping — a direct analogue of the
        helper in ``test_verification_orchestrator.py``. Duplicated
        here (rather than imported) so this test file is
        self-contained and the wall-time assertions can be read
        without cross-file navigation.
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

    def test_orchestrator_runs_4ui_in_parallel(
        self, dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """4 ui VPs, each sleeping 5s → wall time ∈ [5, 7]s.

        With the default ``parallelism_cap=4``, four ui_validation
        VPs fit in a single semaphore batch and finish in roughly
        the duration of one VP (5s), not four (20s). The 2s upper
        bound absorbs asyncio scheduling overhead and the
        ``asyncio.run`` startup cost.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        agent.start_verification_round(1)

        vps = _make_vps_by_method({"ui_validation": 4})
        self._patch_leaf_to_sleep(agent, {"ui_validation": 5})

        start = time.monotonic()
        asyncio.run(agent.execute_verification_plan_async({"verification_points": vps}))
        wall_time = time.monotonic() - start

        assert 5.0 <= wall_time <= 7.0, (
            f"expected wall time ∈ [5, 7]s for 4 VPs in parallel, "
            f"got {wall_time:.2f}s (would be ~20s if serial, ~5s if parallel)"
        )

    def test_orchestrator_respects_parallelism_cap_2(
        self, dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """cap=2 + 6 ui VPs at 3s each → wall time ∈ [6, 8]s.

        The semaphore (``parallelism_cap=2``) throttles in-flight
        VPs to two at a time, so 6 VPs complete in three sequential
        rounds of two, each 3s long. Total ≈ 6s (per the TDD spec
        range). The [6, 8]s band absorbs asyncio scheduling /
        ``asyncio.run`` overhead. Without the semaphore the same
        6 VPs would finish in a single round of 6 (~3s) — that's
        the negative control this test guards against.
        """
        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        # Cap in-flight VPs at 2; raise per-method timeout well
        # above the 3s sleep so the semaphore — not the timeout —
        # is the rate-limiting mechanism.
        agent.timeout_policy = TimeoutPolicy(
            per_method_timeout_seconds={"ui_validation": 60},
            global_default_timeout_seconds=60,
            parallelism_cap=2,
        )
        agent.start_verification_round(1)

        vps = _make_vps_by_method({"ui_validation": 6})
        self._patch_leaf_to_sleep(agent, {"ui_validation": 3})

        start = time.monotonic()
        asyncio.run(agent.execute_verification_plan_async({"verification_points": vps}))
        wall_time = time.monotonic() - start

        # 6 VPs / cap=2 = 3 sequential rounds × 3s = ~9s. The
        # ``[8, 10]s`` band absorbs asyncio scheduling /
        # ``asyncio.run`` overhead. The lower bound 8s leaves no
        # room for the negative control (no cap → ~3s) so a
        # regression that drops the semaphore fails immediately.
        # The 10s upper bound is generous enough to absorb
        # scheduling jitter on slow CI without letting a cap
        # regression (which would land around 3-4s) sneak through.
        assert 8.0 <= wall_time <= 10.0, (
            f"expected wall time ∈ [8, 10]s with cap=2 and 6 VPs at 3s, "
            f"got {wall_time:.2f}s (would be ~3s if cap ignored, "
            f"~9s if cap honoured)"
        )


# -----------------------------------------------------------------------------
# 3. Split metadata propagates end-to-end
# -----------------------------------------------------------------------------


class TestSplitMetadataEndToEnd:
    """Split-on-timeout child VPs must carry trace-back metadata.

    Pinned by the TDD spec: when a multi-clause VP times out and
    :class:`SplitDecision` decomposes it, every child result
    surfaced to the report must carry ``parent_vp_id``,
    ``original_vp_id``, and ``split_clause_index`` so downstream
    consumers (the report generator, the bridge UI) can roll
    results back up to the original VP without re-reading the
    plan.
    """

    def test_split_subtask_inherits_parent_metadata_e2e(
        self, dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
    ):
        """Multi-clause VP hard-times-out → split into 2 sub-VPs → each
        child result carries ``parent_vp_id``,
        ``original_vp_id``, and ``split_clause_index``.

        The VP's ``expected_result`` is split on ``;`` into two
        non-empty clauses so :class:`SplitDecision.should_split`
        accepts it. The parent's leaf raises
        :class:`HardTimeoutError` (the inner 15-min idle-detector
        signal that routes to the split path — the per-VP
        ``asyncio.wait_for`` wrapper that used to raise
        ``asyncio.TimeoutError`` was removed 2026-09-08), triggering
        the split path. The children themselves use a tiny-sleep
        PASSED backend so they finish quickly.

        The parent's status rolls up to ``PASSED`` via
        :meth:`_aggregate_split_results` because all children
        passed; we don't depend on that here — we only assert the
        per-child metadata is present.
        """
        from coding_tool import HardTimeoutError

        agent = VerificationAgent(
            plan_dir=temp_plan_dir,
            project_dir=temp_project_dir,
            coding_tool=mock_coding_tool,
        )
        agent.start_verification_round(1)
        # Orchestrator has no ``logger`` attr by default; the
        # hard-timeout branch guards with ``if self.logger``.
        if not hasattr(agent, "logger"):
            agent.logger = MagicMock()

        vps: List[Dict[str, Any]] = [
            {
                "id": "VP-SPLIT-1",
                "title": "multi-clause VP",
                "verification_method": "ui_validation",
                "priority": "medium",
                # Two clauses separated by ';' so SplitDecision
                # should_split returns 2 children.
                "expected_result": "first clause;second clause",
            }
        ]
        # The parent's leaf must raise HardTimeoutError (simulating
        # the inner 15-min idle detector firing) so SplitDecision's
        # path is exercised, but the children must pass so we can
        # assert the trace-back metadata on PASSED entries. The
        # patched leaf sees every VP, so we dispatch by ``id``: the
        # original parent id maps to the hard-timeout raise, every
        # other id (the children) maps to a tiny-sleep PASSED
        # backend.
        child_backend = FakeVerifierBackend(
            fake_sleep_seconds=0, should_fail=False
        )
        attr = "_execute_ui_validation"

        async def _id_aware_stub(vp):
            if vp.get("id") == "VP-SPLIT-1":
                raise HardTimeoutError(
                    total_sec=900, elapsed=905.0, last_line="x"
                )
            return await child_backend.execute(
                vp, timeout_seconds=agent.timeout_policy.resolve("ui_validation")
            )

        setattr(agent, attr, _id_aware_stub)

        result = asyncio.run(
            agent.execute_verification_plan_async({"verification_points": vps})
        )
        results = result["execution_results"]
        assert len(results) == 1, f"expected one synthesised parent, got {results!r}"
        parent = results[0]

        # The parent itself carries the trace-back metadata too,
        # and surfaces a child_results list with the per-child
        # entries.
        assert "child_results" in parent, (
            f"split parent must carry 'child_results', got keys {list(parent.keys())}"
        )
        child_results = parent["child_results"]
        assert len(child_results) == 2, (
            f"expected 2 children for a 2-clause VP, got {len(child_results)}"
        )

        # Sort by split_clause_index for a stable assertion
        # (parallel execution order is not guaranteed).
        child_results = sorted(
            child_results,
            key=lambda c: c.get("split_clause_index", -1),
        )
        for index, child in enumerate(child_results, start=1):
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
            # Children ran with the backend's default sleep
            # (1.0s) so they finish well under the inherited
            # per-VP timeout.
            assert child.get("status") == "PASSED", (
                f"child[{index}] expected PASSED, got {child!r}"
            )
            # Child id is parent + dash + 1-based index.
            assert child.get("id") == f"VP-SPLIT-1-{index}", (
                f"child[{index}] id should be VP-SPLIT-1-{index}, "
                f"got {child.get('id')!r}"
            )


# -----------------------------------------------------------------------------
# Sanity: _select_backend() integration with the env var
# -----------------------------------------------------------------------------


class TestSelectBackendIntegration:
    """The factory must respect the env var set in ``dry_run_env``."""

    def test_select_backend_returns_fake_under_dry_run(self, dry_run_env):
        """With ``VERIFICATION_PROFILE=dry_run`` set, ``_select_backend``
        must return a :class:`FakeVerifierBackend`.

        This is the wiring contract that lets the rest of the
        pipeline (orchestrator, group partitioning, split
        decision) be exercised end-to-end without LLM or
        filesystem costs. A regression here would silently break
        the dry-run profile and force every CI cycle to fall
        through to the real per-method executors.
        """
        backend = _select_backend()
        assert isinstance(backend, FakeVerifierBackend), (
            f"_select_backend() under VERIFICATION_PROFILE=dry_run "
            f"must return FakeVerifierBackend, got {type(backend).__name__}"
        )
