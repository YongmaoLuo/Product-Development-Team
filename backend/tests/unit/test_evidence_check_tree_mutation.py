"""Unit tests for the tree-mutation guard on the evidence check.

Why these exist
---------------
``_apply_evidence_check`` verifies the artifact a LLM-judged VP was
supposed to produce (``citations.json`` / ``checkpoints.json``) against
the **live** working tree.  When several verification processes share
one worktree, a concurrent writer (a "变异负控" running in parallel) can
rewrite a file between the moment a verdict was produced and the moment
its citations are read — and the check then reads a tree that belongs to
nobody.  The 2026-10-23 production round hit exactly that: a
``code_review`` VP handed back PASSED, and 7.5 ms later the framework
downgraded it because ``citations[8]`` pointed at a line that the
concurrent writer had already removed.

A downgrade on that basis is a **false** FAILED: the reviewer never said
anything false, the tree simply moved under it.  So when the round's
baseline fingerprint says the tree moved, the evidence check must not
downgrade — it must hold the verdict for review, and leave an auditable
event behind.

The contract pinned here:

  * a mutated tree defers the check and keeps the sub-agent's original
    verdict **object** (status still PASSED);
  * the deferred check is recorded, not silently dropped;
  * ``apply_to_verdict`` is not called at all on that path — the check
    never ran, so it cannot have produced a downgrade;
  * an unmutated tree still downgrades exactly as before;
  * no baseline (not a git tree) keeps the previous behaviour: the check
    runs and nothing is deferred.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
import verification_evidence  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402


VP = {"id": "VP-008", "verification_method": "code_review"}


def _agent(tmp_path: Path, fingerprint, events: list):
    """An ``Orchestrator`` with only the attributes the evidence check reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which this
    guard needs — and the persistence manager is exactly what
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


def _event_types(events: list) -> list:
    return [event[1] for event in events]


# ---------------------------------------------------------------------------
# Mutated tree — defer, keep the original verdict
# ---------------------------------------------------------------------------


def test_mutated_tree_defers_and_keeps_passed(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: True,
    )
    verdict = _passed_verdict()

    checked = agent._apply_evidence_check(VP, verdict)

    # The sub-agent's conclusion stands: same object, same status.
    assert checked is verdict
    assert checked["status"] == "PASSED"
    # The check is held for review, not dropped.
    assert len(agent._deferred_evidence_checks) == 1
    vp_id, deferred_vp, deferred_verdict = (
        agent._deferred_evidence_checks[0]
    )
    assert vp_id == "VP-008"
    assert deferred_vp is VP
    assert deferred_verdict is verdict
    # And the deferral is auditable.
    assert "evidence_check_deferred_tree_mutated" in _event_types(events)


def test_mutated_tree_does_not_call_apply(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: True,
    )
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return _failed_verdict()

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _spy)
    verdict = _passed_verdict()

    checked = agent._apply_evidence_check(VP, verdict)

    assert calls == [], "the evidence check must not run on a mutated tree"
    assert checked is verdict
    assert checked["status"] == "PASSED"


# ---------------------------------------------------------------------------
# Unmutated tree / no baseline — the previous behaviour, unchanged
# ---------------------------------------------------------------------------


def test_unmutated_tree_still_downgrades(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, "eb64e8a:aaa", events)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    downgraded = _failed_verdict()
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: downgraded,
    )
    verdict = _passed_verdict()

    checked = agent._apply_evidence_check(VP, verdict)

    assert checked is downgraded
    assert checked["status"] == "FAILED"
    assert agent._deferred_evidence_checks == []
    assert "evidence_check_downgraded" in _event_types(events)
    assert "evidence_check_deferred_tree_mutated" not in _event_types(events)


def test_none_baseline_degrades_to_current_behaviour(tmp_path, monkeypatch):
    events: list = []
    agent = _agent(tmp_path, None, events)
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return _failed_verdict()

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _spy)
    verdict = _passed_verdict()

    checked = agent._apply_evidence_check(VP, verdict)

    # No baseline (not a git tree): the check runs exactly as it did
    # before the fingerprint existed.
    assert len(calls) == 1
    assert checked["status"] == "FAILED"
    assert agent._deferred_evidence_checks == []
    assert "evidence_check_deferred_tree_mutated" not in _event_types(events)
