"""Unit tests: an evidence artifact older than the round is not this
round's evidence (2026-11-19, repair-37 / VP-043).

Why these exist
---------------
The evidence check reads the artifact a verification point was supposed
to produce (``citations.json`` / ``checkpoints.json``) and compares it
against the **live** tree.  A verification point's artifact directory is
**reused across rounds**, so the file the framework finds may have been
written by an *earlier* attempt — and comparing that file against a tree
that has moved since tells you nothing about the deliverable.  It tells
you the tree moved.

2026-11-19 hit exactly that: the round started at 04:14:34Z, the
``VP-043`` citations were written at 09:14 local — three hours *before*
the round began — and a later commit shifted the cited line by 72 lines.
The framework compared the stale file against the current tree, called
``citations[12]`` a ``snippet_mismatch``, and turned a PASSED into a
FAILED that was then archived as a *deliverable defect*.

That is precisely the class of false FAILED the plan's §1 forbids: a
verdict that could not be judged must not be recorded as a verdict that
was judged and failed.  An unverified PASSED is a smaller lie than a
FAILED nobody can reproduce.

The contract pinned here:

  * an artifact written **before** the round started is never compared
    against the tree — the check reports ``stale_artifact`` instead;
  * a PASSED resting on such an artifact is **not** downgraded: the
    verdict is kept and ``evidence.evidence_check`` records
    ``state='unavailable'`` with the artifact and the round's start;
  * the agent does not even enter ``apply_to_verdict`` on that path —
    it records an ``evidence_check_stale_artifact`` event and returns the
    sub-agent's conclusion untouched;
  * without a round start (or with a stamp in a shape the framework does
    not write) the behaviour is **exactly** what it was before this
    rule existed, and a fresh artifact with a wrong citation is still
    downgraded — the gate is not relaxed;
  * the deferred re-check at round end carries the round's start with it,
    so a stale artifact cannot change the verdict there either.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
import verification_evidence  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_evidence import (  # noqa: E402
    CITATIONS_FILENAME,
    apply_to_verdict,
    artifact_dir_for,
    verify_evidence,
)


VP = {"id": "VP-X", "verification_method": "code_review"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    """The stamp shape the framework writes (``YYYY-MM-DDTHH:MM:SSZ``)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "engine.rs").write_text(
        "fn main() {\n"
        "    let metric = find_metric(&bars);\n"
        "}\n"
    )
    return project


def _write_citations(plan_dir: Path, vp_id: str, payload) -> Path:
    directory = artifact_dir_for(plan_dir, vp_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / CITATIONS_FILENAME
    target.write_text(json.dumps(payload, ensure_ascii=False))
    return target


def _wrong_citations(plan_dir: Path, vp_id: str) -> Path:
    """A citations artifact quoting a line that is not there."""
    return _write_citations(plan_dir, vp_id, {"citations": [{
        "file": "src/engine.rs", "line": 2,
        "snippet": "let manufactured_evidence = true;",
    }]})


def _stale_citations(plan_dir: Path, vp_id: str) -> Path:
    """The same artifact, written an hour ago — an earlier attempt's."""
    target = _wrong_citations(plan_dir, vp_id)
    old = time.time() - 3600
    os.utime(target, (old, old))
    return target


def _agent(tmp_path: Path, events: list, round_started_at=None):
    """An ``Orchestrator`` with only the attributes the evidence check reads.

    ``__init__`` is skipped on purpose (see
    ``test_evidence_check_tree_mutation``): it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which this
    guard needs — and the persistence manager is exactly what
    ``_safe_log_freshness_event`` writes through, so it is stubbed here.
    """
    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = tmp_path / "plan"
    agent.project_dir = tmp_path / "project"
    agent._round_tree_fingerprint = None
    agent._deferred_evidence_checks = []
    agent._round_started_at = round_started_at
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _passed_verdict() -> dict:
    return {"status": "PASSED", "reasons": ["14 条 SATISFIED"], "evidence": {}}


def _event_types(events: list) -> list:
    return [event[1] for event in events]


def _event(events: list, event_type: str) -> dict:
    for _vp_id, kind, data in events:
        if kind == event_type:
            return data
    raise AssertionError(f"no {event_type} event in {_event_types(events)}")


# ---------------------------------------------------------------------------
# apply_to_verdict — the artifact is judged, not the tree it was written for
# ---------------------------------------------------------------------------


def test_a_citations_artifact_from_a_previous_round_keeps_the_passed_verdict(
    tmp_path: Path,
):
    """The artifact is older than the round → the verdict is not touched."""
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)
    target = _stale_citations(plan_dir, "VP-X")
    started = _iso(time.time())
    verdict = _passed_verdict()

    checked = apply_to_verdict(
        verdict, "code_review", plan_dir, project, "VP-X",
        round_started_at=started,
    )

    # The conclusion stands: there was no conclusion to check, so there
    # is nothing to downgrade it to.
    assert checked["status"] == "PASSED"
    assert checked["reasons"] == ["14 条 SATISFIED"]
    assert checked["evidence"]["evidence_check"]["state"] == "unavailable"
    assert checked["evidence"]["evidence_check"]["reason"] == "stale_artifact"
    assert checked["evidence"]["evidence_check"]["artifact"] == str(target)
    assert checked["evidence"]["evidence_check"]["round_started_at"] == started


def test_a_fresh_artifact_with_a_wrong_citation_still_downgrades(
    tmp_path: Path,
):
    """The negative control: the gate is not relaxed.  An artifact the
    round *did* write is judged as before — a wrong citation still
    downgrades the verdict."""
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)
    _wrong_citations(plan_dir, "VP-X")

    checked = apply_to_verdict(
        _passed_verdict(), "code_review", plan_dir, project, "VP-X",
        round_started_at=_iso(time.time() - 7200),
    )

    assert checked["status"] == "FAILED"
    assert any("snippet_mismatch" in r for r in checked["reasons"])


def test_without_a_round_start_the_check_is_unchanged(tmp_path: Path):
    """The negative control: no cutoff → no staleness rule → exactly what
    the check did before."""
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)
    _stale_citations(plan_dir, "VP-X")

    checked = apply_to_verdict(
        _passed_verdict(), "code_review", plan_dir, project, "VP-X",
    )

    assert checked["status"] == "FAILED"
    assert any("snippet_mismatch" in r for r in checked["reasons"])


# ---------------------------------------------------------------------------
# verify_evidence — the tree is not consulted for a stale artifact
# ---------------------------------------------------------------------------


def test_an_artifact_older_than_the_round_is_not_compared_against_the_tree(
    tmp_path: Path,
):
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)
    _stale_citations(plan_dir, "VP-X")

    issues = verify_evidence(
        "code_review", plan_dir, project, "VP-X",
        round_started_at=_iso(time.time()),
    )

    # The stale artifact is reported as stale — the tree is never
    # consulted, so a moved line cannot be read as a broken deliverable.
    assert [i.kind for i in issues] == ["stale_artifact"]
    assert "citations.json" in issues[0].detail


def test_an_artifact_written_after_the_round_is_judged_as_before(
    tmp_path: Path,
):
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)
    _stale_citations(plan_dir, "VP-X")

    issues = verify_evidence(
        "code_review", plan_dir, project, "VP-X",
        round_started_at=_iso(time.time() - 7200),
    )

    # The artifact is newer than the round → the ordinary check runs.
    assert [i.kind for i in issues] == ["snippet_mismatch"]


def test_a_round_start_in_an_unreadable_shape_is_ignored(tmp_path: Path):
    """A stamp the framework does not write is not a cutoff — dropping
    evidence on the strength of a cutoff nobody can parse would be
    dropping evidence without grounds."""
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)
    _stale_citations(plan_dir, "VP-X")

    issues = verify_evidence(
        "code_review", plan_dir, project, "VP-X",
        round_started_at="2026-11-19",
    )

    assert [i.kind for i in issues] == ["snippet_mismatch"]


def test_a_missing_artifact_is_still_missing_not_stale(tmp_path: Path):
    """Nothing was written — that is ``missing_artifact``'s business, and
    it has always been a downgrade."""
    plan_dir = tmp_path / "plan"
    project = _project(tmp_path)

    issues = verify_evidence(
        "code_review", plan_dir, project, "VP-X",
        round_started_at=_iso(time.time()),
    )

    assert [i.kind for i in issues] == ["missing_artifact"]


# ---------------------------------------------------------------------------
# The agent — the guard sits next to the tree-mutation guard
# ---------------------------------------------------------------------------


def test_apply_evidence_check_keeps_a_verdict_whose_artifact_predates_the_round(
    tmp_path: Path,
):
    events: list = []
    agent = _agent(tmp_path, events, round_started_at=_iso(time.time()))
    _stale_citations(agent.plan_dir, "VP-X")
    verdict = _passed_verdict()

    checked = agent._apply_evidence_check(VP, verdict)

    assert checked["status"] == "PASSED"
    assert "evidence_check_stale_artifact" in _event_types(events)


def test_the_agent_records_the_artifact_and_the_round_start(tmp_path: Path):
    events: list = []
    started = _iso(time.time())
    agent = _agent(tmp_path, events, round_started_at=started)
    target = _stale_citations(agent.plan_dir, "VP-X")

    agent._apply_evidence_check(VP, _passed_verdict())

    data = _event(events, "evidence_check_stale_artifact")
    assert data["artifact"] == str(target)
    assert data["round_started_at"] == started
    assert data["method"] == "code_review"


def test_the_agent_does_not_enter_apply_to_verdict_on_a_stale_artifact(
    tmp_path: Path, monkeypatch,
):
    events: list = []
    agent = _agent(tmp_path, events, round_started_at=_iso(time.time()))
    _stale_citations(agent.plan_dir, "VP-X")
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "FAILED", "reasons": [], "evidence": {}}

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _spy)

    checked = agent._apply_evidence_check(VP, _passed_verdict())

    assert calls == [], "a stale artifact must not be judged at all"
    assert checked["status"] == "PASSED"
    # It is not a tree mutation either, so nothing is deferred for review.
    assert agent._deferred_evidence_checks == []


def test_without_a_round_start_the_agent_behaves_exactly_as_before(
    tmp_path: Path,
):
    events: list = []
    agent = _agent(tmp_path, events, round_started_at=None)
    _stale_citations(agent.plan_dir, "VP-X")

    checked = agent._apply_evidence_check(VP, _passed_verdict())

    assert checked["status"] == "FAILED"


# ---------------------------------------------------------------------------
# The deferred re-check carries the round's start with it
# ---------------------------------------------------------------------------


def test_the_deferred_recheck_carries_the_round_start(tmp_path, monkeypatch):
    events: list = []
    started = _iso(time.time())
    agent = _agent(tmp_path, events, round_started_at=started)
    seen: list = []

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return _passed_verdict()

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _spy)

    agent._recheck_deferred_evidence(
        "VP-X", "code_review", _passed_verdict(), VP,
    )

    assert len(seen) == 1
    assert seen[0].get("round_started_at") == started


def test_the_deferred_recheck_does_not_downgrade_on_a_stale_artifact(
    tmp_path: Path,
):
    events: list = []
    agent = _agent(tmp_path, events, round_started_at=_iso(time.time()))
    _stale_citations(agent.plan_dir, "VP-X")

    checked = agent._recheck_deferred_evidence(
        "VP-X", "code_review", _passed_verdict(), VP,
    )

    assert checked is not None
    assert checked["status"] == "PASSED"
    assert "evidence_check_stale_artifact" in _event_types(events)


def test_the_round_end_flush_does_not_downgrade_a_stale_artifact(
    tmp_path: Path,
):
    events: list = []
    agent = _agent(tmp_path, events, round_started_at=_iso(time.time()))
    _stale_citations(agent.plan_dir, "VP-X")
    agent._defer_evidence_check(
        "VP-X", VP, _passed_verdict(), "code_review",
    )

    class _Executor:
        def __init__(self):
            self.updates: dict = {}

        def apply_verdict_updates(self, updates):
            self.updates = dict(updates)
            return list(updates)

    executor = _Executor()
    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == []
    assert executor.updates == {}
