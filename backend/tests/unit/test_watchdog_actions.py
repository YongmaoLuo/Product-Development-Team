"""TDD spec for the watchdog action sequence (architecture decision point 6).

Architecture decision point 6 mandates a five-step action sequence
fired by the watchdog's ``on_trigger`` seam when a dead loop is
detected::

    1. 调研    (research)    — collect failure context (fingerprint,
                                progress token, recent log lines)
    2. auto-fix            — invoke the LLM-driven fix path to repair
                                the failure cause automatically
    3. validate-through    — re-run the validator to confirm the fix
                                landed (no regression)
    4. 重启                 — restart the affected subprocess so the
                                fix takes effect at runtime
    5. 报告                 — write a structured report describing
                                the action sequence outcome

This module pins the public surface of that sequence:

* :class:`WatchdogActionContext` — frozen dataclass carrying every
  input the action sequence needs (plan_id, plans_root, fingerprint,
  progress_token, recent log entries).
* :class:`ActionStep` — the enum naming each of the five steps in
  the canonical order.
* :class:`ActionReport` — frozen dataclass returned by
  :func:`run_action_sequence` summarising the outcome.
* :func:`run_action_sequence` — the orchestrator. It calls each
  step's callable in order, short-circuits on failure, and never
  raises (so the watchdog process can stay alive across an
  unrecoverable auto-fix attempt).

The five steps are *pluggable*: callers pass a dict of callables
(one per step) so tests can substitute deterministic stubs that
record the call order and return canned results. The default
implementations are the production code paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from framework.clock import utcnow_iso
from framework.ids import InvalidPlanIdError
from watchdog_actions import (
    ActionReport,
    ActionStep,
    MAX_ACTIONS_PER_TRIGGER,
    WatchdogActionContext,
    run_action_sequence,
)


PLAN_ID = "20260807-actions-spec"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    plans_root: Path,
    plan_id: str = PLAN_ID,
    fingerprint: str = "fp-abc",
    progress_token: str = "tok-xyz",
    recent_failures: list[dict[str, Any]] | None = None,
) -> WatchdogActionContext:
    """Build a :class:`WatchdogActionContext` for hermetic tests."""
    if recent_failures is None:
        recent_failures = [
            {
                "task_id": "12",
                "level": "ERROR",
                "event": "task_failed",
                "message": "boom",
            }
        ]
    return WatchdogActionContext(
        plan_id=plan_id,
        plans_root=plans_root,
        fingerprint=fingerprint,
        progress_token=progress_token,
        recent_failures=tuple(recent_failures),
        triggered_at=utcnow_iso(),
    )


@pytest.fixture
def plans_root(tmp_path: Path) -> Path:
    """Hermetic plans root — every test gets a fresh empty dir."""
    root = tmp_path / "plans"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _reset_action_counter() -> None:
    """Reset the module-level action counter before every test.

    The orchestrator uses a persistent counter to enforce the
    per-trigger budget; without an autouse reset, the budget test
    would pollute every subsequent test.
    """
    from watchdog_actions import reset_action_counter
    reset_action_counter()
    yield
    reset_action_counter()


def _stub_steps() -> tuple[
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
    list[list[str]],
]:
    """Return 5 stub step callables plus a shared call-recorder.

    Each stub appends its :class:`ActionStep` name to ``recorder[i]``
    and returns a tiny dict marking success. The orchestrator sees a
    clean (steps_completed=5, succeeded=True) report.
    """
    recorder: list[list[str]] = [[], [], [], [], []]

    def make_recorder(idx: int):
        def _stub(ctx: WatchdogActionContext, **kwargs) -> dict[str, Any]:
            recorder[idx].append(ActionStep(idx).name)
            return {"step": ActionStep(idx).name, "ok": True}
        return _stub

    steps = (
        make_recorder(0),  # 调研
        make_recorder(1),  # auto-fix
        make_recorder(2),  # validate-through
        make_recorder(3),  # 重启
        make_recorder(4),  # 报告
    )
    return steps + (recorder,)


# ---------------------------------------------------------------------------
# 1. WatchdogActionContext — frozen dataclass with safety guards
# ---------------------------------------------------------------------------


def test_context_rejects_unsafe_plan_id(plans_root: Path) -> None:
    with pytest.raises(InvalidPlanIdError):
        WatchdogActionContext(
            plan_id="../../etc",
            plans_root=plans_root,
            fingerprint="fp",
            progress_token="tok",
            recent_failures=(),
            triggered_at=utcnow_iso(),
        )


def test_context_requires_non_empty_fingerprint(plans_root: Path) -> None:
    with pytest.raises(ValueError):
        WatchdogActionContext(
            plan_id=PLAN_ID,
            plans_root=plans_root,
            fingerprint="",
            progress_token="tok",
            recent_failures=(),
            triggered_at=utcnow_iso(),
        )


def test_context_requires_non_empty_progress_token(plans_root: Path) -> None:
    with pytest.raises(ValueError):
        WatchdogActionContext(
            plan_id=PLAN_ID,
            plans_root=plans_root,
            fingerprint="fp",
            progress_token="",
            recent_failures=(),
            triggered_at=utcnow_iso(),
        )


def test_context_is_frozen(plans_root: Path) -> None:
    ctx = _make_context(plans_root)
    with pytest.raises(Exception):
        ctx.plan_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. ActionStep — canonical 5-step enum, frozen order
# ---------------------------------------------------------------------------


def test_action_step_has_exactly_five_members_in_canonical_order() -> None:
    expected = ("RESEARCH", "AUTO_FIX", "VALIDATE_THROUGH", "RESTART", "REPORT")
    actual = tuple(s.name for s in ActionStep)
    assert actual == expected
    assert len(actual) == 5


def test_action_step_values_are_stable_ints() -> None:
    """Enum values must be 0..4 (index-based) so dicts iterate in order."""
    assert int(ActionStep.RESEARCH) == 0
    assert int(ActionStep.AUTO_FIX) == 1
    assert int(ActionStep.VALIDATE_THROUGH) == 2
    assert int(ActionStep.RESTART) == 3
    assert int(ActionStep.REPORT) == 4


# ---------------------------------------------------------------------------
# 3. run_action_sequence — calls every step in canonical order
# ---------------------------------------------------------------------------


def test_run_action_sequence_calls_all_five_steps_in_order(
    plans_root: Path,
) -> None:
    ctx = _make_context(plans_root)
    s0, s1, s2, s3, s4, recorder = _stub_steps()
    steps = {
        ActionStep.RESEARCH: s0,
        ActionStep.AUTO_FIX: s1,
        ActionStep.VALIDATE_THROUGH: s2,
        ActionStep.RESTART: s3,
        ActionStep.REPORT: s4,
    }
    report = run_action_sequence(ctx, steps=steps)
    # Each stub was called exactly once.
    assert [r[0] for r in recorder] == [
        "RESEARCH",
        "AUTO_FIX",
        "VALIDATE_THROUGH",
        "RESTART",
        "REPORT",
    ]
    # Report reflects success.
    assert report.succeeded is True
    assert report.steps_completed == 5
    assert report.failed_step is None
    # The canonical order is also reflected in report.step_results.
    assert tuple(r["step"] for r in report.step_results) == (
        "RESEARCH",
        "AUTO_FIX",
        "VALIDATE_THROUGH",
        "RESTART",
        "REPORT",
    )


def test_run_action_sequence_requires_all_five_steps(
    plans_root: Path,
) -> None:
    """A missing callable for any step is a programmer error → ValueError."""
    ctx = _make_context(plans_root)
    s0, s1, s2, s3, _s4, _ = _stub_steps()
    incomplete = {
        ActionStep.RESEARCH: s0,
        ActionStep.AUTO_FIX: s1,
        ActionStep.VALIDATE_THROUGH: s2,
        ActionStep.RESTART: s3,
        # REPORT intentionally missing
    }
    with pytest.raises(ValueError):
        run_action_sequence(ctx, steps=incomplete)


# ---------------------------------------------------------------------------
# 4. run_action_sequence — short-circuits on step failure
# ---------------------------------------------------------------------------


def test_run_action_sequence_short_circuits_on_step_exception(
    plans_root: Path,
) -> None:
    ctx = _make_context(plans_root)
    s0, s1, _s2, s3, s4, recorder = _stub_steps()

    def failing_validate(ctx, **kwargs):
        recorder[2].append("VALIDATE_THROUGH")
        raise RuntimeError("validator crashed")

    steps = {
        ActionStep.RESEARCH: s0,
        ActionStep.AUTO_FIX: s1,
        ActionStep.VALIDATE_THROUGH: failing_validate,
        ActionStep.RESTART: s3,
        ActionStep.REPORT: s4,
    }
    report = run_action_sequence(ctx, steps=steps)
    # 3 steps ran (research + auto_fix + the failing validate).
    assert report.succeeded is False
    assert report.failed_step == ActionStep.VALIDATE_THROUGH
    assert report.steps_completed == 2  # only the ones that succeeded
    # RESTART and REPORT were NOT called.
    assert recorder[3] == []
    assert recorder[4] == []


def test_run_action_sequence_returns_false_on_validate_failure_dict(
    plans_root: Path,
) -> None:
    """A step returning ``{"ok": False}`` also short-circuits the rest."""
    ctx = _make_context(plans_root)
    s0, _s1, s2, s3, s4, recorder = _stub_steps()

    def auto_fix_returns_false(ctx, **kwargs):
        recorder[1].append("AUTO_FIX")
        return {"step": "AUTO_FIX", "ok": False, "reason": "no LLM"}

    steps = {
        ActionStep.RESEARCH: s0,
        ActionStep.AUTO_FIX: auto_fix_returns_false,
        ActionStep.VALIDATE_THROUGH: s2,
        ActionStep.RESTART: s3,
        ActionStep.REPORT: s4,
    }
    report = run_action_sequence(ctx, steps=steps)
    assert report.succeeded is False
    assert report.failed_step == ActionStep.AUTO_FIX
    assert report.steps_completed == 1
    assert recorder[2] == []  # validate never ran
    assert recorder[3] == []  # restart never ran
    assert recorder[4] == []  # report never ran


# ---------------------------------------------------------------------------
# 5. run_action_sequence — never raises out of the orchestrator
# ---------------------------------------------------------------------------


def test_run_action_sequence_does_not_raise_on_catastrophic_step_failure(
    plans_root: Path,
) -> None:
    """Even if RESEARCH blows up, the report must come back cleanly."""
    ctx = _make_context(plans_root)
    _s0, s1, s2, s3, s4, recorder = _stub_steps()

    def boom(ctx, **kwargs):
        recorder[0].append("RESEARCH")
        raise MemoryError("synthetic catastrophic failure")

    steps = {
        ActionStep.RESEARCH: boom,
        ActionStep.AUTO_FIX: s1,
        ActionStep.VALIDATE_THROUGH: s2,
        ActionStep.RESTART: s3,
        ActionStep.REPORT: s4,
    }
    # Must NOT raise. The orchestrator catches and packages it.
    report = run_action_sequence(ctx, steps=steps)
    assert report.succeeded is False
    assert report.failed_step == ActionStep.RESEARCH
    assert report.steps_completed == 0
    assert report.error is not None
    assert "MemoryError" in report.error


# ---------------------------------------------------------------------------
# 6. ActionReport — report.json contract
# ---------------------------------------------------------------------------


def test_action_report_serializes_to_json_with_required_fields(
    plans_root: Path,
) -> None:
    ctx = _make_context(plans_root)
    s0, s1, s2, s3, s4, _ = _stub_steps()
    steps = {
        ActionStep.RESEARCH: s0,
        ActionStep.AUTO_FIX: s1,
        ActionStep.VALIDATE_THROUGH: s2,
        ActionStep.RESTART: s3,
        ActionStep.REPORT: s4,
    }
    report = run_action_sequence(ctx, steps=steps)
    blob = json.loads(report.to_json())
    # All 5 canonical keys present.
    for key in (
        "plan_id",
        "fingerprint",
        "progress_token",
        "triggered_at",
        "succeeded",
        "steps_completed",
        "failed_step",
        "error",
        "step_results",
        "finished_at",
    ):
        assert key in blob, f"missing key: {key}"
    assert blob["plan_id"] == PLAN_ID
    assert blob["fingerprint"] == "fp-abc"
    assert blob["progress_token"] == "tok-xyz"
    assert blob["succeeded"] is True
    assert blob["failed_step"] is None


def test_action_report_writes_to_plan_dir(plans_root: Path) -> None:
    """The orchestrator persists the report to plans/{plan_id}/."""
    ctx = _make_context(plans_root)
    s0, s1, s2, s3, s4, _ = _stub_steps()
    steps = {
        ActionStep.RESEARCH: s0,
        ActionStep.AUTO_FIX: s1,
        ActionStep.VALIDATE_THROUGH: s2,
        ActionStep.RESTART: s3,
        ActionStep.REPORT: s4,
    }
    report = run_action_sequence(ctx, steps=steps)
    report_path = report.write(plans_root)
    assert report_path.exists()
    assert report_path.parent == plans_root / PLAN_ID
    blob = json.loads(report_path.read_text(encoding="utf-8"))
    assert blob["plan_id"] == PLAN_ID
    assert blob["succeeded"] is True


# ---------------------------------------------------------------------------
# 7. Action budget — bounded retries
# ---------------------------------------------------------------------------


def test_max_actions_per_trigger_is_a_positive_integer() -> None:
    """Defensive: the budget must be a small positive integer."""
    assert isinstance(MAX_ACTIONS_PER_TRIGGER, int)
    assert MAX_ACTIONS_PER_TRIGGER > 0
    # The exact value is implementation-tunable, but it MUST stay
    # small enough that one plan can't consume the watchdog forever.
    assert MAX_ACTIONS_PER_TRIGGER <= 10


def test_run_action_sequence_rejects_more_than_max_actions(
    plans_root: Path,
) -> None:
    """One trigger must not be allowed to invoke > MAX_ACTIONS_PER_TRIGGER
    actions in a single window — that would be an infinite-loop vector
    at the orchestrator level.
    """
    # Build a stub that *would* run forever if the budget were not
    # enforced. The orchestrator should refuse to start after the
    # cumulative action count reaches MAX_ACTIONS_PER_TRIGGER.
    ctx = _make_context(plans_root)
    call_count = {"n": 0}

    def counting_research(ctx, **kwargs):
        call_count["n"] += 1
        return {"step": "RESEARCH", "ok": True}

    steps = {
        ActionStep.RESEARCH: counting_research,
        ActionStep.AUTO_FIX: counting_research,
        ActionStep.VALIDATE_THROUGH: counting_research,
        ActionStep.RESTART: counting_research,
        ActionStep.REPORT: counting_research,
    }
    # Calling the orchestrator MAX_ACTIONS_PER_TRIGGER // 5 + 1 times
    # should trip the budget before it would otherwise run forever.
    iterations = (MAX_ACTIONS_PER_TRIGGER // 5) + 2
    last_report = None
    for _ in range(iterations):
        last_report = run_action_sequence(ctx, steps=steps)
        if not last_report.succeeded:
            break
    # The orchestrator MUST have stopped before spinning forever —
    # either by short-circuiting (succeeded=False) or by raising a
    # BudgetExceeded error surfaced as a failed report. We accept
    # either shape.
    assert last_report is not None
    assert call_count["n"] <= MAX_ACTIONS_PER_TRIGGER + 5


# ---------------------------------------------------------------------------
# 8. Structural guards — the module must stay decoupled
# ---------------------------------------------------------------------------


def test_module_does_not_import_agent_or_dispatcher() -> None:
    """The watchdog action sequence must not pull in daemon code."""
    source = (
        Path(__file__).resolve().parents[2] / "watchdog_actions.py"
    ).read_text(encoding="utf-8")
    assert "import agent" not in source
    assert "from agent import" not in source
    assert "import dispatcher" not in source
    assert "from dispatcher import" not in source


def test_module_uses_framework_clock_not_datetime_now() -> None:
    """Decision point 5: every timestamp flows through framework.clock."""
    source = (
        Path(__file__).resolve().parents[2] / "watchdog_actions.py"
    ).read_text(encoding="utf-8")
    assert "datetime.now(" not in source
    assert "utcnow_iso" in source
