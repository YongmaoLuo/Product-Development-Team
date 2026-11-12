"""Unit tests for the subject check on a verdict.

Why these exist
---------------
2026-11-07, round 4: VP-028 asked about ``backend/verification_executor.py``
— "the Phase-2 gate recomputation satisfies (1)~(4)" — and the sub-agent
handed back a **PASSED** whose eleven reasons were all about
``backend/routes/verification.py``, a *different* verification point's
subject (VP-038).  Nothing in the framework asked whether the verdict was
answering *this* question: the citations resolved, the artifact was
there, the status was PASSED — and a verdict about the wrong file reached
the final report as a verified pass.

The subject check closes that hole at the evidence layer.  It is
deliberately narrow: it only judges when it can *prove* the answer is
about something else.

  * only the LLM-judged methods (``EVIDENCE_REQUIRED_METHODS``);
  * only when the point's ``expected_result`` names a repo path that
    really exists under ``project_dir`` — no path, no judgement;
  * only when the verdict names paths too — a verdict that names none is
    a pure-text review and is never punished;
  * no path of the verdict is one of the point's paths → the verdict
    answered a different question;
  * it only ever **downgrades**: PASSED → FAILED.  A FAILED stays
    FAILED, a SKIPPED stays SKIPPED, and nothing is ever turned into a
    pass.

The contract pinned here:

  * a mismatch is reported, naming both sides ("本验证点点名 A，而结论
    只涉及 B");
  * a verdict about the right file is not touched;
  * the downgrade appends the reason and keeps the original verdict
    object intact;
  * the framework-executed methods are not judged at all;
  * the check is wired into ``_apply_evidence_check`` — the one place
    that holds both the question and the answer — and it leaves an
    auditable event behind.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    """A project tree with the two files the incident turned on."""
    project = tmp_path / "project"
    (project / "backend" / "routes").mkdir(parents=True, exist_ok=True)
    (project / "backend" / "verification_executor.py").write_text(
        "def gate():\n    return True\n", encoding="utf-8",
    )
    (project / "backend" / "routes" / "verification.py").write_text(
        "def route():\n    return True\n", encoding="utf-8",
    )
    return project


def _vp(expected_result: str = "", method: str = "code_review") -> dict:
    return {
        "id": "VP-028",
        "verification_method": method,
        "expected_result": expected_result,
    }


def _passed(reasons: list) -> dict:
    return {
        "status": "PASSED",
        "reasons": list(reasons),
        "evidence": {"details": "", "raw": []},
    }


# ---------------------------------------------------------------------------
# The pure function — is the verdict about another question?
# ---------------------------------------------------------------------------


def test_mismatch_is_reported_for_another_files_subject(tmp_path):
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert (
        "本验证点点名 backend/verification_executor.py，"
        "而结论只涉及 backend/routes/verification.py"
    ) in reason


def test_no_mismatch_when_the_verdict_cites_the_named_file(tmp_path):
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/verification_executor.py:3732 的短路判定与重算共用同一判据",
    ])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_no_judgement_when_the_point_names_no_existing_path(tmp_path):
    """A path that does not exist names nothing — no judgement."""
    project = _project(tmp_path)
    vp = _vp("backend/never_written.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_no_judgement_when_the_point_names_no_path_at_all(tmp_path):
    project = _project(tmp_path)
    vp = _vp("四条判据都要满足")
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_no_judgement_when_the_verdict_names_no_path(tmp_path):
    """A pure-text review is not evidence of answering the wrong question."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed(["整体看没有问题，四条判据都满足"])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_framework_executed_methods_are_not_judged(tmp_path):
    """``api_test`` / ``full_ci`` never rest on an LLM's word."""
    project = _project(tmp_path)
    vp = _vp(
        "backend/verification_executor.py 的门禁重算要满足 (1)~(4)",
        method="api_test",
    )
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_paths_in_the_evidence_are_counted(tmp_path):
    """The answer is the whole verdict — prose *and* evidence."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = {
        "status": "PASSED",
        "reasons": ["四条判据都满足"],
        "evidence": {
            "details": "",
            "raw": ["backend/routes/verification.py:1700-1701"],
        },
    }

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "backend/routes/verification.py" in reason


def test_cited_files_count_as_the_answer(tmp_path):
    """A review that cited the right file is not flagged, even if the
    prose never repeats the path."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed(["四条判据都满足"])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project, citation_files=["backend/verification_executor.py"],
    ) is None


def test_the_tree_can_come_from_the_point_itself(tmp_path):
    """The dispatch hands the point the tree it has to be read against."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    vp["project_root"] = str(project)
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    reason = verification_evidence.verdict_subject_mismatch(vp, verdict)

    assert reason is not None
    assert "本验证点点名" in reason


# ---------------------------------------------------------------------------
# The applier — only ever a downgrade
# ---------------------------------------------------------------------------


def test_apply_downgrades_the_passed_and_appends_the_reason(tmp_path):
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked["status"] == "FAILED"
    assert len(checked["reasons"]) == 2
    assert "本验证点点名" in checked["reasons"][-1]
    # The caller's verdict is left alone.
    assert verdict["status"] == "PASSED"
    assert len(verdict["reasons"]) == 1


def test_apply_leaves_a_failed_verdict_alone(tmp_path):
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = {
        "status": "FAILED",
        "reasons": ["原有的失败理由"],
        "evidence": {},
    }

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked is verdict
    assert checked["status"] == "FAILED"
    assert checked["reasons"] == ["原有的失败理由"]


def test_apply_leaves_a_skipped_verdict_alone(tmp_path):
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = {"status": "SKIPPED", "reasons": ["环境不具备"], "evidence": {}}

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked is verdict
    assert checked["status"] == "SKIPPED"


def test_apply_never_upgrades_a_failure(tmp_path):
    """The check refuses a pass; it never hands one out."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = {
        "status": "FAILED",
        "reasons": ["backend/routes/verification.py 的调用方没同步"],
        "evidence": {},
    }

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked["status"] == "FAILED"


def test_apply_keeps_a_passed_that_is_on_subject(tmp_path):
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/verification_executor.py:3732 的短路判定与重算共用同一判据",
    ])

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked is verdict
    assert checked["status"] == "PASSED"


# ---------------------------------------------------------------------------
# A bare file name *is* the same path (VP-039, judge (5))
# ---------------------------------------------------------------------------
#
# 2026-11-09, round 35: the path words of an answer dropped every token
# without a slash, so a verdict that named the checked file by its bare
# name (``verification_executor.py:3732``) had that name thrown away.
# When the verdict *also* mentioned another file by path (the changed
# test file), the answer set held only the other file — and the verdict
# was downgraded to FAILED for answering the wrong question, even though
# it had answered it.
#
# The rule is narrow on purpose: a bare name counts as the file it
# names **only when the verification point named that file**, so the
# artifact names a verdict mentions in passing (``checkpoints.json``,
# ``ci_output.json``, ``verdict.json``) never become paths of their own
# and never manufacture a mismatch.


def test_a_bare_file_name_counts_as_the_named_file(tmp_path):
    """Judging (5): a bare file name is the same path."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "verification_executor.py:3732 的短路判定与重算共用同一判据",
        "backend/tests/unit/test_verification_phase_gate.py:174 仍绿",
    ])

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_apply_keeps_a_passed_that_names_the_file_by_its_bare_name(tmp_path):
    """The downgrade must not fire on a verdict that answered the point."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "verification_executor.py:3732 的短路判定与重算共用同一判据",
        "backend/tests/unit/test_verification_phase_gate.py:174 仍绿",
    ])

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked["status"] == "PASSED"
    assert "actual_result" not in checked


def test_a_bare_name_of_another_file_does_not_answer_the_point(tmp_path):
    """Only the point's own file may be answered by its bare name."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "verification.py:1700-1701 的报告地址是对的",
        "backend/tests/unit/test_verification_phase_gate.py:174 仍绿",
    ])

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason


def test_artifact_file_names_never_become_paths_of_their_own(tmp_path):
    """``checkpoints.json`` and friends are not sources of a mismatch."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "checkpoints.json 记录了每一次观察",
        "ci_output.json 与 verdict.json 都在",
        "backend/routes/verification.py:120 路由返回 200",
    ])

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    for artifact in ("checkpoints.json", "ci_output.json", "verdict.json"):
        assert artifact not in reason


def test_a_bare_name_in_the_evidence_counts_as_the_named_file(tmp_path):
    """The answer is the whole verdict — the bare name may sit in the
    evidence rather than in the reasons."""
    project = _project(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = {
        "status": "PASSED",
        "reasons": ["四条判据都满足"],
        "evidence": {
            "details": "",
            "raw": [
                "verification_executor.py:3732 的短路判定与重算共用同一判据",
                "backend/tests/unit/test_verification_phase_gate.py:174 仍绿",
            ],
        },
    }

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_the_same_bare_name_under_another_directory_is_not_the_answer(
    tmp_path,
):
    """``backend/routes/verification_executor.py`` is a *different* file.

    It ends in the same name, so a rule that matched on "the basename
    occurs somewhere in the answer" would rescue it — and wave through a
    verdict about another file.  A bare name is the name **standing
    alone**; a path that merely ends in it is a path, and it is judged as
    one.
    """
    project = _project(tmp_path)
    (project / "backend" / "routes" / "verification_executor.py").write_text(
        "def other():\n    return True\n", encoding="utf-8",
    )
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/routes/verification_executor.py:120 是另一个文件",
        "backend/tests/unit/test_verification_phase_gate.py:174 仍绿",
    ])

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason


# ---------------------------------------------------------------------------
# The agent wiring — the one place that sees question *and* answer
# ---------------------------------------------------------------------------


def _agent(tmp_path: Path, project: Path, events: list):
    """An ``Orchestrator`` with only the attributes the check reads."""
    agent = Orchestrator.__new__(Orchestrator)
    agent.plan_dir = tmp_path / "plan"
    agent.project_dir = project
    agent._round_tree_fingerprint = "eb64e8a:aaa"
    agent._deferred_evidence_checks = []
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _agent_test(tmp_path, monkeypatch):
    """An agent whose *evidence* check passes — the subject check is what
    is under test here."""
    project = _project(tmp_path)
    events: list = []
    agent = _agent(tmp_path, project, events)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: args[0],
    )
    return agent, events, project


def test_agent_downgrades_a_verdict_about_another_question(
    tmp_path, monkeypatch,
):
    agent, events, project = _agent_test(tmp_path, monkeypatch)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    checked = agent._apply_evidence_check(vp, verdict)

    assert checked["status"] == "FAILED"
    assert any("本验证点点名" in reason for reason in checked["reasons"])
    # And the downgrade is auditable.
    assert "subject_check_downgraded" in [event[1] for event in events]


def test_agent_keeps_a_verdict_on_subject(tmp_path, monkeypatch):
    agent, events, project = _agent_test(tmp_path, monkeypatch)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/verification_executor.py:3732 的短路判定与重算共用同一判据",
    ])

    checked = agent._apply_evidence_check(vp, verdict)

    assert checked["status"] == "PASSED"
    assert "subject_check_downgraded" not in [event[1] for event in events]


def test_agent_leaves_a_failed_verdict_alone(tmp_path, monkeypatch):
    agent, events, project = _agent_test(tmp_path, monkeypatch)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = {
        "status": "FAILED",
        "reasons": ["backend/routes/verification.py 的调用方没同步"],
        "evidence": {},
    }

    checked = agent._apply_evidence_check(vp, verdict)

    assert checked["status"] == "FAILED"
    assert checked["reasons"] == ["backend/routes/verification.py 的调用方没同步"]
    assert "subject_check_downgraded" not in [event[1] for event in events]


def test_agent_leaves_other_methods_alone(tmp_path, monkeypatch):
    agent, events, project = _agent_test(tmp_path, monkeypatch)
    vp = _vp(
        "backend/verification_executor.py 的门禁重算要满足 (1)~(4)",
        method="api_test",
    )
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])

    checked = agent._apply_evidence_check(vp, verdict)

    assert checked is verdict
    assert checked["status"] == "PASSED"


# ---------------------------------------------------------------------------
# The deferred check — a deferred check carries the whole gate
# ---------------------------------------------------------------------------


def _executor(tmp_path: Path):
    """A real executor holding one VP, with a PASSED verdict recorded."""
    from verification_executor import VerificationExecutor

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    async def _runner(vp: dict) -> dict:  # pragma: no cover - never called
        return {"status": "PASSED", "reasons": [], "evidence": {}}

    executor = VerificationExecutor(
        verification_plan={"vps": [{
            "id": "VP-028", "layer": "L1", "method": "code_review", "title": "t",
        }]},
        plan_id="20261107-test",
        plan_dir=plan_dir,
        sub_agent_runner=_runner,
    )
    executor.record_verdict(
        "VP-028", {"status": "PASSED", "reasons": [], "evidence": {}},
    )
    return executor


def test_the_deferred_check_also_judges_the_subject(tmp_path, monkeypatch):
    """A deferred check is this round's debt — the subject check is part
    of it, so a deferred verdict about another question is downgraded
    when the tree comes back."""
    project = _project(tmp_path)
    events: list = []
    agent = _agent(tmp_path, project, events)
    monkeypatch.setattr(
        verification_agent, "tree_mutated",
        lambda project_dir, baseline: False,
    )
    monkeypatch.setattr(
        verification_evidence, "apply_to_verdict",
        lambda *args, **kwargs: args[0],
    )
    executor = _executor(tmp_path)
    vp = _vp("backend/verification_executor.py 的门禁重算要满足 (1)~(4)")
    verdict = _passed([
        "backend/routes/verification.py:1700-1701 的报告地址是对的",
    ])
    agent._deferred_evidence_checks = [("VP-028", vp, verdict)]

    applied = agent._flush_deferred_evidence_checks(executor)

    assert applied == ["VP-028"]
    assert executor._verdicts["VP-028"]["status"] == "FAILED"
    assert "subject_check_downgraded" in [event[1] for event in events]
