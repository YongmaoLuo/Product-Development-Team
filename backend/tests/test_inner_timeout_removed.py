"""Regression tests for the 2026-09-08 inner-asyncio.wait_for removal.

Background: the per-VP ``asyncio.wait_for`` wrapper inside
``VerificationAgent._run_single_vp_async`` was a 60-300s inner cap
that fired long before the 1-hour outer cap in
``VerificationSubAgent._execute_attempt``. That inner cap routed to
a recursive LLM-split pipeline that ran away 8 levels deep on
VP-034 (2026-09-08 incident). The per-VP hard timeout was 1 hour, with
a 15-minute silence timeout; the outer 1-hour + 15-minute caps already
covered both, so the inner wrappers were pure redundancy.

The fix:
  * Remove all three ``asyncio.wait_for(..., timeout=timeout_seconds)``
    wrappers in ``_run_single_vp_async`` (executor_async,
    executor_sync, _delegate_to_sub_agent paths).
  * Remove the corresponding ``except asyncio.TimeoutError`` branch
    (the outer 1-hour cap raises ``StuckAgentError`` and routes to
    the summarizer fallback, NOT to ``_run_single_vp_async``).
  * Bump defensive defaults in ``verification.yaml`` and
    ``DEFAULT_PER_METHOD_TIMEOUT_SECONDS`` from 120/60/1800 to 3600
    so ``vp_start`` log lines surface the correct 1-hour budget.

2026-09-13 update: the per-VP timeout interface was
DELETED entirely — ``_resolve_vp_timeout_seconds`` no longer exists
and the outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600``.

These tests pin the contract so a future refactor can't silently
re-introduce the inner cap.

Contract pinned:
  1. ``_run_single_vp_async`` does NOT wrap any ``await`` in
     ``asyncio.wait_for(timeout=...)``. The 1-hour outer cap is the
     sole wall-clock ceiling.
  2. ``_run_single_vp_async`` does NOT catch ``asyncio.TimeoutError``
     anywhere — that exception would mean the outer cap fired
     prematurely, which is the very bug we just removed.
  3. ``_resolve_vp_timeout_seconds`` does NOT exist (deleted
     2026-09-13; flat cap only).
  4. ``DEFAULT_PER_METHOD_TIMEOUT_SECONDS`` is uniformly 3600s.
  5. ``verification.yaml`` declares 3600s for all four methods.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


REPO_ROOT = Path(__file__).parent.parent.parent
VERIFICATION_AGENT_PY = REPO_ROOT / "backend" / "verification_agent.py"
VERIFICATION_SUBAGENT_PY = REPO_ROOT / "backend" / "verification_subagent.py"
VERIFICATION_CONFIG_PY = REPO_ROOT / "backend" / "verification_config.py"
VERIFICATION_YAML = REPO_ROOT / "backend" / "configs" / "verification.yaml"


# ---------------------------------------------------------------------------
# 1. Source code shape — pin the structural contract
# ---------------------------------------------------------------------------


def _find_run_single_vp_async(tree: ast.AST) -> ast.AsyncFunctionDef:
    """Locate ``_run_single_vp_async`` in the verification_agent AST.

    The agent file is large (>3000 lines), so we walk the top-level
    and nested function defs to find it by name. We don't trust
    line numbers in source-code grep tests because the line numbers
    drift across commits.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_single_vp_async"
        ):
            return node
    raise AssertionError(
        "_run_single_vp_async not found in verification_agent.py"
    )


def _has_call_to(node: ast.AST, func_name: str) -> bool:
    """True if ``node`` (or any descendant) calls a function whose
    ``id`` or ``attr`` ends with ``func_name``.

    Used to scan for ``asyncio.wait_for(...)`` and
    ``asyncio.TimeoutError`` references without depending on line
    numbers.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            # ``asyncio.wait_for(...)`` → Attribute(attr='wait_for')
            if isinstance(func, ast.Attribute) and func.attr == func_name:
                return True
            # ``asyncio.TimeoutError`` → Name(id='TimeoutError') only
            # if used in an except clause; we ignore Name() here.
    return False


def _except_handlers(node: ast.AsyncFunctionDef) -> list:
    """Return the exception-type AST nodes of every ``except`` clause
    inside ``node``, recursively (covers nested try/except)."""
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.ExceptHandler) and child.type is not None:
            out.append(child.type)
    return out


def test_run_single_vp_async_does_not_use_asyncio_wait_for():
    """_run_single_vp_async MUST NOT call asyncio.wait_for anywhere.

    The 2026-09-08 fix removed all three inner-cap wrappers
    (executor_async, executor_sync, _delegate_to_sub_agent paths).
    A regression that re-adds one would re-introduce the
    60-300s inner cap that triggered the 8-level LLM-split
    runaway on VP-034.
    """
    tree = ast.parse(VERIFICATION_AGENT_PY.read_text(encoding="utf-8"))
    fn = _find_run_single_vp_async(tree)
    assert not _has_call_to(fn, "wait_for"), (
        "_run_single_vp_async still calls asyncio.wait_for — "
        "this re-introduces the per-VP inner cap that fired long "
        "before the 1-hour outer cap and caused the VP-034 "
        "8-level recursive split (2026-09-08)."
    )


def test_run_single_vp_async_does_not_catch_asyncio_timeout_error():
    """_run_single_vp_async MUST NOT have an ``except asyncio.TimeoutError``
    branch — that exception can no longer reach this scope because
    no inner ``asyncio.wait_for`` raises it. Keeping the handler
    would silently swallow real bugs in the future.
    """
    tree = ast.parse(VERIFICATION_AGENT_PY.read_text(encoding="utf-8"))
    fn = _find_run_single_vp_async(tree)

    for handler_type in _except_handlers(fn):
        # Match `asyncio.TimeoutError` (Attribute) and the bare
        # `TimeoutError` (Name) — both are reachable shapes.
        if isinstance(handler_type, ast.Attribute):
            assert handler_type.attr != "TimeoutError", (
                "_run_single_vp_async has an "
                "``except asyncio.TimeoutError`` branch — this was "
                "removed on 2026-09-08 because no inner "
                "``asyncio.wait_for`` raises it anymore."
            )
        elif isinstance(handler_type, ast.Name):
            assert handler_type.id != "TimeoutError", (
                "_run_single_vp_async catches bare TimeoutError — "
                "use HardTimeoutError explicitly instead."
            )


def test_run_single_vp_async_still_catches_hard_timeout_error():
    """Sanity: the inner idle (15 min) HardTimeoutError branch must
    still exist. The 1-hour outer cap + 15-min inner idle is the
    full timeout contract.
    """
    tree = ast.parse(VERIFICATION_AGENT_PY.read_text(encoding="utf-8"))
    fn = _find_run_single_vp_async(tree)

    found_hard_timeout = False
    for handler_type in _except_handlers(fn):
        if isinstance(handler_type, ast.Name) and handler_type.id == "HardTimeoutError":
            found_hard_timeout = True
            break
    assert found_hard_timeout, (
        "_run_single_vp_async must still catch HardTimeoutError — "
        "the 15-min inner idle detector (coding_tool._total_timer) "
        "raises it when subprocess stdout is silent."
    )


# ---------------------------------------------------------------------------
# 2. Defensive defaults — all 3600 (1-hour outer cap)
# ---------------------------------------------------------------------------


def test_default_per_method_timeout_seconds_is_uniformly_3600():
    """DEFAULT_PER_METHOD_TIMEOUT_SECONDS must be 3600 for every method.

    Before 2026-09-08: ``automated_test=120`` and ``api_test=60`` —
    the bug. The values were "fast feedback preferred" defaults,
    not actual caps, but they became the inner asyncio.wait_for
    timeout via TimeoutPolicy.resolve.
    """
    from verification_config import DEFAULT_PER_METHOD_TIMEOUT_SECONDS
    from verification_subagent import SUPPORTED_METHODS

    # 2026-09-18: tied to SUPPORTED_METHODS rather than a hard-coded set,
    # so retiring a method cannot leave a stale timeout entry behind (or
    # a supported method without one).
    expected = set(SUPPORTED_METHODS)
    assert set(DEFAULT_PER_METHOD_TIMEOUT_SECONDS.keys()) == expected, (
        f"Missing/extra methods in defaults: "
        f"{set(DEFAULT_PER_METHOD_TIMEOUT_SECONDS.keys()) ^ expected}"
    )
    for method, value in DEFAULT_PER_METHOD_TIMEOUT_SECONDS.items():
        assert value == 3600, (
            f"DEFAULT_PER_METHOD_TIMEOUT_SECONDS[{method!r}] = {value}, "
            f"expected 3600 (1-hour outer cap)."
        )


def test_default_global_timeout_seconds_is_3600():
    """The global default fallback (for unknown methods) is also 3600."""
    from verification_config import DEFAULT_GLOBAL_TIMEOUT_SECONDS

    assert DEFAULT_GLOBAL_TIMEOUT_SECONDS == 3600, (
        f"DEFAULT_GLOBAL_TIMEOUT_SECONDS = {DEFAULT_GLOBAL_TIMEOUT_SECONDS}, "
        f"expected 3600."
    )


def test_verification_yaml_declares_3600_for_all_methods():
    """The on-disk yaml must match the new defaults. If a future
    refactor forgets to update the yaml, this catches it."""
    import yaml

    from verification_subagent import SUPPORTED_METHODS

    data = yaml.safe_load(VERIFICATION_YAML.read_text(encoding="utf-8"))
    per_method = data["execution"]["per_method_timeout_seconds"]

    # 2026-09-18: tied to SUPPORTED_METHODS. The hard-coded set named
    # ``automated_test`` for a month after it was retired — a stale
    # expectation that no longer guarded anything and would have let a
    # missing new method through.
    expected = set(SUPPORTED_METHODS)
    assert set(per_method.keys()) == expected, (
        f"Missing/extra methods in verification.yaml: "
        f"{set(per_method.keys()) ^ expected}"
    )
    for method, value in per_method.items():
        assert value == 3600, (
            f"verification.yaml {method!r} = {value}, expected 3600."
        )


# ---------------------------------------------------------------------------
# 3. Per-VP timeout interface deleted (2026-09-13 plan)
# ---------------------------------------------------------------------------


def test_resolve_vp_timeout_seconds_deleted():
    """2026-09-13: the per-VP timeout interface
    was deleted. ``_resolve_vp_timeout_seconds`` must NOT exist — the
    outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` and no
    plan-provided ``timeout_seconds`` may shrink it (legacy VP-023
    wrote 120s and stalled round 4).
    """
    src = VERIFICATION_SUBAGENT_PY.read_text(encoding="utf-8")
    assert "_resolve_vp_timeout_seconds" not in src, (
        "_resolve_vp_timeout_seconds was deleted 2026-09-13 (per-VP "
        "timeout interface removed). Do not re-introduce it without an "
        "the contract is explicit."
    )
    assert 'vp_node.get("timeout_seconds"' not in src, (
        "verification_subagent.py must not read a per-VP "
        "timeout_seconds field anymore."
    )


def test_outer_cap_is_flat_3600_constant():
    """End-to-end: the only enforcement constant is the flat 3600s
    HARD_WALL_CLOCK_CAP_SECONDS; the multiplier / grace knobs are
    gone so no arithmetic can shrink the cap.
    """
    from verification_subagent import VerificationSubAgent

    assert VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS == 3600
    assert not hasattr(VerificationSubAgent, "OUTER_TIMEOUT_MULTIPLIER")
    assert not hasattr(VerificationSubAgent, "QUERY_ABANDON_GRACE_SECONDS")


# ---------------------------------------------------------------------------
# 4. Behavioural smoke — _run_single_vp_async with the inner cap removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_single_vp_async_no_inner_timeout_runs_subprocess_to_completion():
    """A sub-agent that "sleeps 1 second" must complete in ~1s, NOT be
    killed by an inner cap. (No inner cap means it runs naturally.)
    """
    from verification_agent import VerificationAgent
    from verification_config import TimeoutPolicy

    agent = VerificationAgent.__new__(VerificationAgent)  # skip __init__

    async def fake_subagent(vp, method, vp_id):
        await asyncio.sleep(0.05)
        return {
            "verdict": "PASSED",
            "reasons": ["ok"],
            "evidence": [],
            "pytest_exit_code": 0,
        }

    vp = {
        "id": "VP-NOINNERTIMEOUT",
        "title": "t",
        "verification_method": "automated_test",
        "expected_result": "x",
        "test_command": "sleep 0.05",
        # Set timeout_seconds=3600 explicitly (matches the new default).
        # If an inner cap were still active with a smaller value,
        # the test would still pass; this test is specifically
        # checking that there's NO inner cap that races ahead of
        # the natural sub-agent completion.
        "timeout_seconds": 3600,
    }
    # Stub out persistence so the test doesn't touch disk.
    agent.persistence = MagicMock()
    agent.persistence.write_verification_point_log = MagicMock()
    agent._delegate_to_sub_agent = fake_subagent  # type: ignore[method-assign]
    agent._verdict_to_result = lambda vp_id, verdict: {  # type: ignore[method-assign]
        "id": vp_id,
        "status": verdict.get("verdict", "UNKNOWN"),
        "actual_result": verdict.get("reasons", [""])[0],
        "reasons": verdict.get("reasons", []),
        "evidence": verdict.get("evidence", []),
    }
    # _run_single_vp_async still calls timeout_policy.resolve() to
    # surface the budget in vp_start logs (no longer used as a cap).
    agent.timeout_policy = TimeoutPolicy.defaults()
    agent._enter_repairing_state = MagicMock()  # type: ignore[method-assign]

    import time
    t0 = time.monotonic()
    result = await agent._run_single_vp_async(vp)
    elapsed = time.monotonic() - t0

    assert result["status"] == "PASSED", (
        f"expected PASSED, got {result}. The inner cap (if any) "
        f"would have killed the sub-agent before it could return "
        f"PASSED."
    )
    assert elapsed < 5.0, (
        f"sub-agent took {elapsed:.2f}s — if an inner cap of 60-300s "
        f"were still active, it would have killed the agent or made "
        f"it return timeout. The test sleeping 0.05s should complete "
        f"well under any sane wall-clock cap."
    )


@pytest.mark.asyncio
async def test_run_single_vp_async_does_not_swallow_real_exceptions():
    """A sub-agent that raises (not times out) must propagate the
    exception, NOT silently return ``status='timeout'``. Before the
    fix, exceptions that looked like ``asyncio.TimeoutError`` could
    get mis-routed to the LLM-split path. After the fix, only true
    timeouts (which now never fire from this layer) trigger that.
    """
    from verification_agent import VerificationAgent
    from verification_config import TimeoutPolicy

    agent = VerificationAgent.__new__(VerificationAgent)

    async def fake_subagent(vp, method, vp_id):
        raise ValueError("real bug, not a timeout")

    vp = {
        "id": "VP-REALEXC",
        "verification_method": "automated_test",
        "expected_result": "x",
        "test_command": "true",
    }
    agent.persistence = MagicMock()
    agent.persistence.write_verification_point_log = MagicMock()
    agent._delegate_to_sub_agent = fake_subagent  # type: ignore[method-assign]
    agent.timeout_policy = TimeoutPolicy.defaults()
    agent._enter_repairing_state = MagicMock()  # type: ignore[method-assign]

    # The agent catches all Exception and returns FAILED (not raises).
    # The point of this test: it must NOT return status="timeout".
    result = await agent._run_single_vp_async(vp)
    assert result["status"] == "FAILED", (
        f"expected FAILED (real exception), got {result['status']!r}. "
        f"If this says 'timeout', the removed asyncio.TimeoutError "
        f"branch is somehow still active."
    )
    # The agent wraps the exception via ``str(e)`` which captures
    # the message but not the type name. Sanity: the message body
    # we raised is present, and the result is NOT routed to the
    # split path (which would set ``status='timeout'`` or
    # ``status='SPLIT'``).
    assert "real bug, not a timeout" in result.get("actual_result", ""), (
        f"FAILED actual_result should carry the exception message: {result}"
    )


# ---------------------------------------------------------------------------
# 5. Cancellation safety — the inner cap removal must not break
#    outer-cap cancellation propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_through_run_single_vp_async():
    """If the outer scope cancels this task (``asyncio.CancelledError``),
    the inner sub-agent must also be cancelled. With the inner
    ``asyncio.wait_for`` removed, ``_run_single_vp_async`` no longer
    swallows cancellation — it propagates naturally.
    """
    from verification_agent import VerificationAgent
    from verification_config import TimeoutPolicy

    agent = VerificationAgent.__new__(VerificationAgent)

    started = asyncio.Event()
    cancel_observed = asyncio.Event()

    async def fake_subagent(vp, method, vp_id):
        started.set()
        try:
            await asyncio.sleep(60)  # longer than any sane test timeout
        except asyncio.CancelledError:
            cancel_observed.set()
            raise
        return {"verdict": "PASSED", "reasons": [], "evidence": []}

    vp = {
        "id": "VP-CANCEL",
        "verification_method": "automated_test",
        "expected_result": "x",
        "test_command": "true",
    }
    agent.persistence = MagicMock()
    agent.persistence.write_verification_point_log = MagicMock()
    agent._delegate_to_sub_agent = fake_subagent  # type: ignore[method-assign]
    agent.timeout_policy = TimeoutPolicy.defaults()
    agent._enter_repairing_state = MagicMock()  # type: ignore[method-assign]

    task = asyncio.create_task(agent._run_single_vp_async(vp))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancel_observed.is_set(), (
        "Outer cancellation did NOT propagate to the sub-agent — "
        "an inner ``asyncio.wait_for`` might be silently catching it."
    )