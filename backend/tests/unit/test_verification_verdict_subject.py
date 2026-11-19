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

import json
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


# ---------------------------------------------------------------------------
# A command point asks about its own command (VP-022, 2026-11-14)
# ---------------------------------------------------------------------------
#
# 2026-11-14, round 23: VP-022 is an ``e2e``/``command`` point.  Its
# ``expected_result`` mentions ``scripts/run_tests.sh`` — but only as a
# *reference* ("与 scripts/run_tests.sh 的 phase 2 选择一致").  The question
# it asks is one question: does the declared ``entry_command`` exit 0.
#
# The subject check read the referenced path as the **subject** of the
# point, so a verdict that records the command it ran and its exit code
# — which is the whole answer to that question — was downgraded for
# "answering another question", because the log of what ran names the
# test files, never ``scripts/run_tests.sh``.  The gate could not pass:
# the greener the run, the more certain the downgrade.
#
# The carve-out below is mechanical: the verdict records **the very
# command the point declared** together with the exit code the framework
# read.  That is a proof that the answer is about this question, and it
# opens no door for a verdict about another question.


def _command_project(tmp_path: Path) -> Path:
    """A tree with the referenced script and the lane the entry runs."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    (project / "backend" / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
    (project / "scripts" / "run_tests.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8",
    )
    (project / "backend" / "tests" / "e2e" / "test_lane.py").write_text(
        "def test_lane():\n    assert True\n", encoding="utf-8",
    )
    return project


_COMMAND = (
    'python3 -m pytest backend/tests -m "(e2e or integration) '
    'and not time_sensitive" --timeout=600 -q'
)

_EXPECTED = (
    "仓库的 e2e + integration lane 整体跑通（与 scripts/run_tests.sh 的 "
    "phase 2 选择一致）：命令退出码 0 表示全量端到端通过。"
)


def _command_vp(entry: str = _COMMAND, method: str = "e2e") -> dict:
    return {
        "id": "VP-022",
        "verification_method": method,
        "e2e_kind": "command",
        "entry_command": entry,
        "expected_result": _EXPECTED,
    }


def _command_verdict(
    entry: str = _COMMAND, exit_code=0, key: str = "entry_command",
) -> dict:
    """The verdict the CI runner produces for a command point."""
    return {
        "status": "PASSED",
        "reasons": [f"[e2e] `{entry}` 退出码 {exit_code}（用时 1632s）"],
        "evidence": {
            key: entry,
            "exit_code": exit_code,
            "stdout_tail": (
                "backend/tests/e2e/test_lane.py ....\n"
                "= 685 passed, 16 skipped in 1619.77s ="
            ),
        },
    }


def test_a_command_point_that_ran_its_own_entry_is_on_subject(tmp_path):
    """The carve-out: the point's own command, with its exit code."""
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _command_verdict()

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_the_apply_keeps_a_command_point_that_ran_its_own_entry(tmp_path):
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _command_verdict()

    checked = verification_evidence.apply_subject_check_to_verdict(
        verdict, vp, project,
    )

    assert checked["status"] == "PASSED"
    assert "actual_result" not in checked


def test_the_carve_out_does_not_cover_another_command(tmp_path):
    """A verdict that ran a *different* command is still off subject."""
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _command_verdict(
        entry="python3 -m pytest backend/tests/unit/test_other.py -q",
    )

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason


def test_the_carve_out_needs_a_recorded_exit_code(tmp_path):
    """An exit code the framework did not read proves nothing."""
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _command_verdict()
    verdict["evidence"].pop("exit_code")

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason


def test_the_carve_out_reads_the_ci_entry_face_too(tmp_path):
    """``ci_entry`` is the other face of the same question.

    A ``full_ci`` point never reaches the subject check (the method
    gate stops it first), so the two faces are pinned on the helper
    itself: the entry the point declared is the entry the verdict has
    to record.
    """
    vp = _command_vp(entry="bash scripts/run_tests.sh", method="full_ci")
    vp.pop("e2e_kind")
    vp.pop("entry_command")
    vp["ci_entry"] = "bash scripts/run_tests.sh"
    verdict = _command_verdict(
        entry="bash scripts/run_tests.sh", key="ci_entry",
    )

    assert verification_evidence._ran_the_declared_entry(vp, verdict) is True

    other = _command_verdict(entry="bash scripts/other.sh", key="ci_entry")
    assert verification_evidence._ran_the_declared_entry(vp, other) is False


# ---------------------------------------------------------------------------
# The projected shape — what the gate really receives (VP-022, 2026-11-18)
# ---------------------------------------------------------------------------
#
# 2026-11-18, round 30: the carve-out above reads a **flat** evidence dict
# (``{"entry_command": ..., "exit_code": 0}``).  The verdict that really
# reaches the gate is the **projection** the agent builds in
# ``_verdict_to_result`` —— ``{"details": str, "raw": list[str]}`` —— and
# the runner's evidence dict survives inside ``raw`` as one JSON string
# (``verification_subagent._verdict_from_outcome`` dumps it there).
#
# So on the real path the carve-out never fired: a command point whose
# declared entry had just exited 0 was downgraded for "answering another
# question", because the log of what ran names the test files and never
# names ``scripts/run_tests.sh``.  The greener the lane, the more certain
# the downgrade — and the lane *was* green (769 passed, exit code 0).
#
# The projection has to be read as what it is: the runner's own record of
# the command it ran and the exit code it read.  Nothing else in the
# projection (a non-empty ``raw``, a prose line) buys the exemption.

_OTHER_COMMAND = "python3 -m pytest backend/tests/unit/test_other.py -q"


def _projected_command_verdict(
    entry: str = _COMMAND, exit_code: int = 0, with_exit_code: bool = True,
) -> dict:
    """The verdict the agent's projection produces for a command point.

    ``raw`` holds the runner's evidence dict as one JSON string — the
    only place the entry command and the exit code survive the
    projection.
    """
    record = {
        "vp_id": "VP-022",
        "entry_command": entry,
        "timed_out": False,
        "duration_seconds": 1996.53,
        "stdout_head": "backend/tests/e2e/test_lane.py ....\n",
        "stdout_tail": "= 769 passed, 16 skipped in 1994.10s =\n",
    }
    if with_exit_code:
        record["exit_code"] = exit_code
    raw = [
        f"exit_code: {exit_code if with_exit_code else 'unknown'}",
        "stdout: backend/tests/e2e/test_lane.py ....\n"
        "= 769 passed, 16 skipped, 14868 deselected in 1994.10s =",
        json.dumps(record, ensure_ascii=False),
    ]
    return {
        "status": "PASSED",
        "reasons": [f"[e2e] `{entry}` 退出码 {exit_code}（用时 1996s）"],
        "evidence": {"details": "", "raw": raw},
    }


def test_a_command_point_whose_verdict_carries_the_runner_artifact_is_on_subject(
    tmp_path,
):
    """The carve-out on the real shape: the projection of the runner's record.

    The declared entry ran and exited 0 — that *is* the answer to a
    command point's one question.  The file names in the log are the
    record of what ran, not the subject of the question.
    """
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _projected_command_verdict()

    assert verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    ) is None


def test_a_projected_command_verdict_that_ran_another_command_is_off_subject(
    tmp_path,
):
    """A record of a *different* command answers a different question."""
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _projected_command_verdict(entry=_OTHER_COMMAND)

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason


def test_a_projected_command_verdict_without_an_exit_code_is_off_subject(
    tmp_path,
):
    """The command alone proves nothing — no exit code, no answer."""
    project = _command_project(tmp_path)
    vp = _command_vp()
    verdict = _projected_command_verdict(with_exit_code=False)

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason


def test_a_point_that_is_not_a_command_point_is_not_exempted_by_the_raw_record(
    tmp_path,
):
    """Only the framework's own command points get the projection carve-out.

    A browser point's evidence is the sub-agent's prose, not a runner
    artifact — so a raw record that *would* exempt a command point buys
    nothing here.  (The point still declares an entry on purpose: the
    only reason it is not exempt is the sub-class, not a missing entry.)
    """
    project = _command_project(tmp_path)
    vp = _command_vp()
    vp["e2e_kind"] = "browser"
    vp["target_url"] = "http://127.0.0.1:8000/plan"
    verdict = _projected_command_verdict()

    reason = verification_evidence.verdict_subject_mismatch(
        vp, verdict, project,
    )

    assert reason is not None
    assert "本验证点点名" in reason
