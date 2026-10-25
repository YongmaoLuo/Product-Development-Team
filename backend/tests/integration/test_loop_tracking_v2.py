"""
Loop tracking v2 — same-cause counting by ``(point, failure class)``.

The v1 tracking file (``verification_loop_tracking.json``) stored the
set of failed verification-point ids and one counter for the whole set.
That made the comparison coarse: a point that kept failing for a
*different* reason was still "the same failure" as the round before.

v2 stores the failed ``(point, failure class)`` pairs and counts each
pair on its own, so "VP-007 failed again — but this time because the
criterion is wrong" no longer counts as the same cause.

The file shapes:

* v2 — ``{"version": 2, "previous_failed_keys": [...],
  "same_failure_counts": {...}, "last_round": N}``
* v1 — ``{"previous_failed_ids": [...],
  "consecutive_same_failure_rounds": N}``

A v1 file is read as an empty state: it names the points that failed
but not *why*, and inventing a class for them would fabricate the very
distinction v2 exists to make. The next write upgrades the file to v2.

``same_failure_counts`` holds, per decision key, the number of rounds
that failure has repeated — one on the round it first failed. Failures
that do not count (``infra``, ``out_of_scope``) have no entry at all:
the environment failing is not evidence that the work is wrong.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Make ``verification`` importable when pytest is launched from either
# the project root or the ``backend/`` directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification.triage_agent import TriageConclusion  # noqa: E402


TRACKING_FILENAME = "verification_loop_tracking.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "loop-tracking-v2"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "loop-tracking-v2"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Keep the orchestrator's state machine out of the real ``state.db``."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _recorded_failure_text(point, failure_history):
    """The recorded failure text the judge was shown, verbatim.

    The shipped judge may only quote text that is really there, so the
    double follows the same rule: the point's ``actual_result`` or
    ``evidence``, or — when the point carries neither — the most recent
    history entry's.
    """
    sources = [point, *reversed(list(failure_history or []))]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for field in ("actual_result", "evidence"):
            text = str(source.get(field) or "").strip()
            if text:
                return text
    return ""


class _StubJudge:
    """A triage judge that ran and could not characterise the failure.

    2026-10-23 (VP-019): reaching the same-failure threshold no longer
    stops the chain by itself — the threshold buys ONE triage call, and
    the triage's verdict decides. The judge injected here is the one
    that ran to completion (``available=True``) but could not name the
    failure (``conclusive=False``), so the chain stops.

    The opposite verdict — a triage that *malfunctioned*
    (``available=False``) — must not stop the chain at all; that
    contract is pinned by ``test_same_failure_triage.py``, which drives
    the shipped judge through its three malfunctions.
    """

    #: What the judge says when it cannot characterise the failure.
    RATIONALE = "the triage could not characterise the failure"

    def __init__(self, failure_class="criterion_defect"):
        self._failure_class = failure_class
        self.calls = []

    def judge(self, vp, failure_history, round_number=0):
        point = vp if isinstance(vp, dict) else {}
        self.calls.append({
            "vp": dict(point),
            "failure_history": list(failure_history or []),
            "round_number": round_number,
        })
        return TriageConclusion(
            vp_id=str(point.get("id") or ""),
            failure_class=self._failure_class,
            conclusive=False,
            evidence_quote=_recorded_failure_text(point, failure_history),
            rationale=self.RATIONALE,
            available=True,
        )


def _orchestrator(plan_dir, project_dir, judge=None):
    """An orchestrator with the agent wiring stubbed out.

    Only the loop-tracking file matters here; the verification agent and
    the repair generator would otherwise reach for a real toolchain.

    ``judge`` is the same-failure triage judge. It is injected rather
    than left to the shipped one because the shipped one talks to a
    provider — and a round that reaches the threshold would then be
    answered by "the triage malfunctioned", which is a verdict about
    the triage and not about the failure.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        return VerificationOrchestrator(
            plan_dir, project_dir, Mock(), triage_judge=judge,
        )


def _read_payload(plan_dir):
    return json.loads(
        (plan_dir / TRACKING_FILENAME).read_text(encoding="utf-8")
    )


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the orchestrator reads on construction."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


def _failed_report(vp_id, failure_class):
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": vp_id,
                "status": "FAILED",
                "failure_class": failure_class,
                # The recorded failure text a real report carries — and
                # the only text a triage judge may quote from.
                "actual_result": "AssertionError: expected 200, got 401",
            },
        ],
        "requirement_deviations": [],
    }


def _run_round(plan_dir, project_dir, report, round_number, judge=None):
    """Run one verification round through a *fresh* orchestrator.

    A fresh instance per round is how production runs it: the auto-loop
    rebuilds the orchestrator after every repair, so anything the
    counter remembers has to come back from the file.
    """
    with patch(
        "verification.verification_report_reader.extract_failed_vps_with_paths",
        return_value=[],
    ):
        orch = _orchestrator(plan_dir, project_dir, judge=judge)
        result = orch.check_cycle_conditions(report, round_number=round_number)
    return orch, result


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def test_v2_roundtrip(plan_dir, project_dir):
    """What v2 writes is what v2 reads back.

    The tracking file has to outlive the orchestrator instance — the
    post-repair round builds a fresh one — so the round trip through
    the file is the whole contract.
    """
    orch = _orchestrator(plan_dir, project_dir)
    orch._previous_failed_keys = {("VP-007", "criterion_defect")}
    orch._same_failure_counts = {"VP-007|criterion_defect": 2}
    orch._last_round = 4
    orch._save_loop_tracking()

    payload = _read_payload(plan_dir)
    assert payload == {
        "version": 2,
        "previous_failed_keys": ["VP-007|criterion_defect"],
        "same_failure_counts": {"VP-007|criterion_defect": 2},
        "last_round": 4,
    }, "the file must carry the v2 structure, not the v1 one"

    fresh = _orchestrator(plan_dir, project_dir)
    keys, counts, last_round = fresh._load_loop_tracking()
    assert keys == {("VP-007", "criterion_defect")}
    assert counts == {"VP-007|criterion_defect": 2}
    assert last_round == 4


def test_v1_compat(plan_dir, project_dir):
    """A v1 file loads as an empty state and is written back as v2.

    v1 names the points that failed but not the reason. Reading them
    as "deliverable_defect" would be a guess, and a wrong guess is
    exactly the mistake v2 was written to stop: a point whose class
    changed must not be counted as the same cause.
    """
    (plan_dir / TRACKING_FILENAME).write_text(
        json.dumps({
            "previous_failed_ids": ["VP-007"],
            "consecutive_same_failure_rounds": 1,
        }),
        encoding="utf-8",
    )

    orch = _orchestrator(plan_dir, project_dir)
    keys, counts, last_round = orch._load_loop_tracking()
    assert keys == set(), "a v1 file carries no failure class — none is invented"
    assert counts == {}, "a v1 file carries no per-class counts"
    assert last_round == 0

    # The upgrade: the next write is a v2 file.
    orch._save_loop_tracking()
    payload = _read_payload(plan_dir)
    assert payload["version"] == 2
    assert payload["previous_failed_keys"] == []
    assert payload["same_failure_counts"] == {}


def test_missing_failure_class_defaults(plan_dir, project_dir):
    """A report that names no failure class reads as a deliverable defect.

    It is the only class whose verdict is safe to act on when nobody
    has said otherwise — the other three either do not count or demand
    a human, and reaching one of those by accident is the expensive
    mistake the default exists to prevent.
    """
    orch = _orchestrator(plan_dir, project_dir)

    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-007", "status": "FAILED"},
        ],
        "requirement_deviations": [],
    }
    assert orch._extract_failed_keys(report) == {
        ("VP-007", "deliverable_defect"),
    }

    # A report that names a class keeps it.
    report["verification_results"][0]["failure_class"] = "criterion_defect"
    assert orch._extract_failed_keys(report) == {
        ("VP-007", "criterion_defect"),
    }

    # An unknown class is read as the default, not as itself.
    report["verification_results"][0]["failure_class"] = "not_a_class"
    assert orch._extract_failed_keys(report) == {
        ("VP-007", "deliverable_defect"),
    }


def test_atomic_write_on_error(plan_dir, project_dir, monkeypatch):
    """A failed write leaves the previous file whole.

    The tracking file is read by the next round's fresh orchestrator.
    A half-written JSON would be read as a corrupt file, which resets
    the counter — the loop would then run forever on a failure that
    never changes.
    """
    orch = _orchestrator(plan_dir, project_dir)
    orch._previous_failed_keys = {("VP-007", "criterion_defect")}
    orch._same_failure_counts = {"VP-007|criterion_defect": 1}
    orch._last_round = 2
    orch._save_loop_tracking()
    before = (plan_dir / TRACKING_FILENAME).read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", _boom)
    orch._same_failure_counts = {"VP-007|criterion_defect": 7}
    orch._last_round = 3
    orch._save_loop_tracking()

    after = (plan_dir / TRACKING_FILENAME).read_text(encoding="utf-8")
    assert after == before, "the old file must survive a failed write"
    assert json.loads(after)["same_failure_counts"] == {
        "VP-007|criterion_defect": 1,
    }, "the old content must still be a complete, readable JSON"
    assert not (plan_dir / (TRACKING_FILENAME + ".tmp")).exists(), (
        "the half-written temporary file must not be left behind"
    )


def test_same_class_counts_up_to_two(plan_dir, project_dir):
    """The same failure twice counts up to two and stops the loop.

    This is the case v2 has to keep working: the counting is only
    useful if an unchanged failure still accumulates.

    2026-10-23 (VP-019): the threshold no longer stops the chain by
    itself — it buys one triage call, and the triage's verdict decides.
    The judge injected here ran and could not name the failure, so the
    chain stops; the tally the loop keeps is still the one v2 promises.
    """
    _write_plan_state(plan_dir)
    report = _failed_report("VP-007", "criterion_defect")

    _, r1 = _run_round(
        plan_dir, project_dir, report, 1, judge=_StubJudge(),
    )
    assert r1["status"] != "loop_stopped", "the first failure is not a repeat"

    orch, r2 = _run_round(
        plan_dir, project_dir, report, 2, judge=_StubJudge(),
    )
    assert r2["status"] == "loop_stopped", (
        "the same failure for a second round is the documented stop "
        "condition"
    )
    assert orch._same_failure_counts == {"VP-007|criterion_defect": 2}


def test_class_change_resets_count(plan_dir, project_dir):
    """A point that changed its failure class starts a new count.

    The same point failing for a *different* reason is a different
    failure. Counting it as the same cause is the mistake v2 exists to
    stop, so each class carries its own count: the new one starts at
    one, and the loop is not stopped by a change of nature.
    """
    _write_plan_state(plan_dir)

    _, r1 = _run_round(
        plan_dir, project_dir, _failed_report("VP-007", "deliverable_defect"), 1
    )
    assert r1["status"] != "loop_stopped"

    _, r2 = _run_round(
        plan_dir, project_dir, _failed_report("VP-007", "criterion_defect"), 2
    )
    assert r2["status"] != "loop_stopped", (
        "a change of failure class is a different failure — it must not "
        "be counted as the same cause"
    )

    payload = _read_payload(plan_dir)
    assert payload["same_failure_counts"] == {
        "VP-007|deliverable_defect": 1,
        "VP-007|criterion_defect": 1,
    }, "each class carries its own count"


def test_infra_not_counted(plan_dir, project_dir):
    """An infrastructure failure does not move the same-cause counter.

    The environment failing is not evidence that the work is wrong.
    Counting it would escalate a healthy run to a human and would make
    the counter report "same cause, twice" about two failures that have
    nothing in common.
    """
    _write_plan_state(plan_dir)
    report = _failed_report("VP-007", "infra")

    _, r1 = _run_round(plan_dir, project_dir, report, 1)
    assert r1["status"] != "loop_stopped"

    _, r2 = _run_round(plan_dir, project_dir, report, 2)
    assert r2["status"] != "loop_stopped", (
        "two infrastructure failures are not the same cause"
    )

    payload = _read_payload(plan_dir)
    assert payload["same_failure_counts"] == {}, (
        "an infrastructure failure must not be counted at all"
    )
