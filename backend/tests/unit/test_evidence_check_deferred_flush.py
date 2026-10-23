"""Unit tests for flushing the deferred evidence checks at round end.

Why these exist
---------------
``_apply_evidence_check`` holds an evidence check back when the working
tree moved under it (a concurrent "变异负控" writing into the shared
worktree).  The check is *deferred*, not dropped — but a deferred check
that never runs leaves the round's verdict table holding an unverified
PASSED, and the operator never learns about it.

At the end of the round the tree is supposed to be back where it was,
so the deferred checks can be re-run then:

  * tree restored → re-run the check; if it now says the basis does not
    hold, the verdict is downgraded and **written back** into the
    executor's verdict table (the buckets move with it);
  * tree still different → the check stays inconclusive; the original
    conclusion is kept (an unverified PASSED is better than a false
    FAILED) and an ``evidence_check_inconclusive`` event is recorded.

The contract pinned here:

  * ``apply_verdict_updates`` re-records through ``record_verdict``, so
    the index buckets follow the verdict (a VP that becomes FAILED
    leaves ``completed`` and joins ``failed``);
  * a VP the executor never saw is skipped, not an error;
  * an empty deferred table is a no-op — no check runs, no event is
    written;
  * a check that raises leaves the original conclusion alone and never
    takes the round down with it;
  * the flush runs even when ``executor.run`` raised — it is in a
    ``try/finally``, and the flush never masks the original exception.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
import verification_evidence  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_executor import VerificationExecutor  # noqa: E402


VP = {"id": "VP-008", "verification_method": "code_review"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passed_verdict() -> dict:
    return {
        "status": "PASSED",
        "reasons": ["14 条 SATISFIED"],
        "evidence": {},
    }


def _failed_verdict() -> dict:
    return {
        "status": "FAILED",
        "reasons": ["[citation] citations[8] 指向 backend/routes/execution.py:785 "
                    "但该行不存在"],
        "evidence": {
            "evidence_check": {
                "method": "code_review",
                "issues": [],
            },
        },
    }


def _executor(tmp_path: Path) -> VerificationExecutor:
    """A real executor holding one VP, with a PASSED verdict recorded."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    async def _runner(vp: dict) -> dict:  # pragma: no cover - never called
        return _passed_verdict()

    executor = VerificationExecutor(
        verification_plan={
            "vps": [
                {
                    "id": "VP-008",
                    "layer": "L1",
                    "method": "code_review",
                    "title": "登录密码哈希实现审查",
                },
            ],
        },
        plan_id="20261023-test",
        plan_dir=plan_dir,
        sub_agent_runner=_runner,
    )
    executor.record_verdict("VP-008", _passed_verdict())
    return executor


def _agent(tmp_path: Path, fingerprint: Any, events: list) -> Orchestrator:
    """An ``Orchestrator`` with only the attributes the flush reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which this
    needs — and the persistence manager is exactly what
    ``_safe_log_freshness_event`` writes through, so it is stubbed here.
    """
    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = tmp_path / "plan"
    agent.project_dir = tmp_path / "project"
    agent._round_tree_fingerprint = fingerprint
    agent._deferred_evidence_checks = []
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _event_types(events: list) -> list:
    return [event[1] for event in events]


# ---------------------------------------------------------------------------
# Executor side — the write-back moves the VP between the buckets
# ---------------------------------------------------------------------------


def test_apply_verdict_updates_moves_bucket(tmp_path):
    executor = _executor(tmp_path)
    assert executor.completed_vps == ["VP-008"]
    assert executor.failed_vps == []

    applied = executor.apply_verdict_updates(
        {"VP-008": _failed_verdict()},
    )

    assert applied == ["VP-008"]
    assert executor._verdicts["VP-008"]["status"] == "FAILED"
    # The buckets are mutually exclusive: a VP that turns FAILED leaves
    # ``completed`` and joins ``failed``.
    assert "VP-008" not in executor.completed_vps
    assert executor.failed_vps == ["VP-008"]


def test_apply_verdict_updates_skips_unknown_vp(tmp_path):
    executor = _executor(tmp_path)

    applied = executor.apply_verdict_updates(
        {"VP-999": _failed_verdict()},
    )

    assert applied == []
    assert "VP-999" not in executor._verdicts
    assert executor.failed_vps == []


def test_apply_verdict_updates_empty_is_noop(tmp_path):
    executor = _executor(tmp_path)

    assert executor.apply_verdict_updates({}) == []
    assert executor._verdicts["VP-008"]["status"] == "PASSED"
    assert executor.failed_vps == []


def test_apply_verdict_updates_accepts_a_partial_correction(tmp_path):
    """A correction carries what changed — it need not repeat the verdict.

    The stored verdict is schema-valid, so it supplies the fields the
    correction does not mention (``evidence`` here).  A partial
    correction that was rejected for a missing field would silently
    keep the old — and now wrong — conclusion.
    """
    executor = _executor(tmp_path)

    updates = {
        "VP-008": {
            "status": "FAILED",
            "reasons": ["[snippet_mismatch] citations[8] ..."],
        },
    }
    applied = executor.apply_verdict_updates(updates)

    assert applied == ["VP-008"]
    assert executor._verdicts["VP-008"]["status"] == "FAILED"
    assert executor.completed_vps == []
    assert executor.failed_vps == ["VP-008"]


# ---------------------------------------------------------------------------
# Flush side — tree restored → downgrade and write back
# ---------------------------------------------------------------------------


def test_flush_downgrades_when_tree_restored(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: _failed_verdict(),
    )
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == ["VP-008"]
    # The verdict table now holds the downgraded verdict ...
    assert executor._verdicts["VP-008"]["status"] == "FAILED"
    assert executor.failed_vps == ["VP-008"]
    assert "VP-008" not in executor.completed_vps
    # ... and the downgrade is auditable.
    assert "evidence_check_downgraded" in _event_types(events)
    assert "evidence_check_inconclusive" not in _event_types(events)


def test_flush_resolves_apply_to_verdict_through_this_module(
    tmp_path, monkeypatch,
):
    """The stub may sit on ``verification_agent`` instead of the source."""
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_agent, "apply_to_verdict",
        lambda *args, **kwargs: _failed_verdict(),
        raising=False,
    )
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == ["VP-008"]
    assert executor._verdicts["VP-008"]["status"] == "FAILED"


# ---------------------------------------------------------------------------
# Flush side — tree still moved → inconclusive, original conclusion kept
# ---------------------------------------------------------------------------


def test_flush_inconclusive_when_tree_still_mutated(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: True,
    )
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == []
    # An unverified PASSED beats a false FAILED: the conclusion stands.
    assert executor._verdicts["VP-008"]["status"] == "PASSED"
    assert executor.completed_vps == ["VP-008"]
    assert executor.failed_vps == []
    assert "evidence_check_inconclusive" in _event_types(events)
    assert "evidence_check_downgraded" not in _event_types(events)


# ---------------------------------------------------------------------------
# Flush side — no deferred checks is a no-op
# ---------------------------------------------------------------------------


def test_flush_noop_when_no_deferred(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return _failed_verdict()

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _spy)

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == []
    assert calls == [], "no deferred check means no check runs"
    assert events == [], "no deferred check means no event is written"
    assert executor._verdicts["VP-008"]["status"] == "PASSED"
    assert executor.failed_vps == []


# ---------------------------------------------------------------------------
# Flush side — the boundary conditions
# ---------------------------------------------------------------------------


def test_flush_keeps_original_when_check_raises(tmp_path, monkeypatch):
    """A crashing check must not take the round down with it."""
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("evidence check crashed")

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _boom)
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == []
    assert executor._verdicts["VP-008"]["status"] == "PASSED"
    assert executor.failed_vps == []
    assert "evidence_check_downgraded" not in _event_types(events)


def test_flush_does_not_write_back_when_status_unchanged(
    tmp_path, monkeypatch,
):
    """A check that changes nothing must not write anything back."""
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: _passed_verdict(),
    )
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == []
    assert executor._verdicts["VP-008"]["status"] == "PASSED"
    assert events == []


def test_flush_skips_vp_the_executor_never_saw(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: _failed_verdict(),
    )
    agent._deferred_evidence_checks = [("VP-999", VP, _passed_verdict())]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == []
    assert "VP-999" not in executor._verdicts
    assert executor.failed_vps == []


def test_flush_clears_the_deferred_table(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    executor = _executor(tmp_path)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: _failed_verdict(),
    )
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    agent._flush_deferred_evidence_checks(executor)

    assert agent._deferred_evidence_checks == []


def test_flush_swallows_its_own_exception(tmp_path, monkeypatch):
    """The flush never raises — it runs in a ``finally``."""
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)

    def _boom(*args, **kwargs):
        raise RuntimeError("event log crashed")

    agent._safe_log_freshness_event = _boom
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: True,
    )
    agent._deferred_evidence_checks = [("VP-008", VP, _passed_verdict())]

    agent._flush_deferred_evidence_checks(_executor(tmp_path))


# ---------------------------------------------------------------------------
# Integration — the flush runs at the end of the round, even on failure
# ---------------------------------------------------------------------------


class _Stub:
    """Just enough of the round's collaborators for the wiring test."""

    def clear(self) -> None:
        pass


def _round_agent(tmp_path: Path, executor: Any, calls: List[str]) -> Orchestrator:
    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = tmp_path / "plan"
    agent.project_dir = tmp_path / "project"
    agent._freshness_cache = _Stub()
    agent._rebuild_coordinator = _Stub()
    agent._clear_verification_state_files = lambda: None
    agent.start_verification_round = lambda round_number: None
    agent.generate_verification_plan = lambda: {"verification_points": []}
    agent._preflight_service_freshness = lambda plan_data: None
    agent._resolve_service_placeholders = lambda plan_data: plan_data
    agent._current_tree_fingerprint = lambda: "eb64e8a:aaa"
    agent._build_verification_executor = lambda plan_data: executor
    agent._note_vp_capacity = lambda peak: None
    agent._build_execution_results_from_executor = (
        lambda plan_data, ex: calls.append("build_results") or {
            "verification_points": [], "execution_results": [],
        }
    )
    agent._build_failure_execution_results = (
        lambda plan_data, exc: calls.append("failure_results") or {
            "verification_points": [], "execution_results": [],
        }
    )
    agent.generate_verification_report = (
        lambda results: calls.append("report") or {"overall_status": "FAILED"}
    )
    agent._flush_deferred_evidence_checks = (
        lambda ex: calls.append("flush") or []
    )
    return agent


def test_flush_runs_after_the_round(tmp_path):
    calls: List[str] = []

    class _Executor:
        peak_in_flight = 0

        async def run(self, *args, **kwargs):
            calls.append("run")

    agent = _round_agent(tmp_path, _Executor(), calls)
    agent.run_full_verification()

    # The flush runs after the round and before the results are projected
    # — the report must see the downgraded verdict.
    assert calls.index("run") < calls.index("flush")
    assert calls.index("flush") < calls.index("build_results")


def test_flush_runs_when_the_round_raises(tmp_path):
    calls: List[str] = []

    class _Executor:
        peak_in_flight = 0

        async def run(self, *args, **kwargs):
            calls.append("run")
            raise RuntimeError("round exploded")

    agent = _round_agent(tmp_path, _Executor(), calls)
    agent.run_full_verification()

    # The deferred checks are this round's debt: the flush runs even when
    # the round blew up, and it does not mask the original failure.
    assert "flush" in calls
    assert "failure_results" in calls
    assert "report" in calls


def test_flush_failure_does_not_mask_the_round_failure(tmp_path):
    """A flush that raises must not replace the round's own exception."""
    seen: List[Any] = []

    class _Executor:
        peak_in_flight = 0

        async def run(self, *args, **kwargs):
            raise RuntimeError("round exploded")

    agent = _round_agent(tmp_path, _Executor(), [])
    agent._flush_deferred_evidence_checks = (
        lambda ex: (_ for _ in ()).throw(RuntimeError("flush exploded"))
    )
    agent._build_failure_execution_results = (
        lambda plan_data, exc: seen.append(exc) or {
            "verification_points": [], "execution_results": [],
        }
    )

    agent.run_full_verification()

    assert len(seen) == 1
    assert isinstance(seen[0], RuntimeError)
    assert "round exploded" in str(seen[0])
