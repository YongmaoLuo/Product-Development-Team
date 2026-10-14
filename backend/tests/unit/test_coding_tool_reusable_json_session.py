"""An iteration loop re-asks in the session it just used.

What this pins
--------------
Four call sites re-ask the same question instead of moving on to a new
one: the refiner's re-ask after an unparseable reply, the refiner's
correction round after the validator objects, the repair generator's
test-command fill loop, and repair-content generation across a plan's
verification rounds. Each of those used to pay for a fresh session,
which means the sub-agent re-reads every file its prompt names — and
these prompts name files by path rather than inlining their bodies.

Where each loop's handle lives is the loop's own business, and the three
loops here each own their handle for the length of one call. The fourth
is the odd one out: a plan's repair rounds are not one call, and neither
the ``RepairTaskGenerator`` nor the ``VerificationOrchestrator`` that
owns it survives from one round to the next — the repair-complete
callback builds a fresh orchestrator for every post-repair round. So its
handle is keyed by plan rather than hung on either object, which is what
the L4 test below pins by driving the second round through a **new**
orchestrator. A handle on the generator would pass a test that reused one
generator and reuse nothing at all in production.

The contract:

1. **The second round resumes.** The dispatch carries ``resume=True``
   and the session id the first round *actually used* — not the id the
   caller picked, because the caller has none to pick on round one.
2. **The id comes back out of the dispatch.** The CLI names the session
   it ran in; that is what the handle carries, and what round two
   resumes. This is what makes the reuse reachable at all: a caller that
   had to guess the id would resume a session that never existed, fail,
   degrade, and silently never reuse anything.
3. **A reply that failed to parse still counts.** The session exists,
   and re-asking inside it is the entire point of the loop.
4. **A resume that cannot be honoured degrades.** Same rule as the
   provider switch: fall back to a fresh session, log it, return a
   result, and leave the handle pointing at the session that exists.
5. **Everyone else is untouched.** A caller that does not ask for a
   session gets the fresh-session-per-call behaviour it has today, and
   an explicit provider-fallback ``resume_session`` still outranks the
   loop's handle.

Why the assertion is at ``_run_interactive_resilient``
-----------------------------------------------------
Asserting that a caller passed something along proves nothing: the
session could be dropped anywhere between the caller and the CLI. This
mocks the layer ``query_json`` dispatches through and reads the kwargs
that layer receives, so "resume=True, under session id X" is a statement
about the dispatch rather than about the plumbing. The end-of-chain
argv is covered separately by the provider-switch suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import refiner as refiner_module  # noqa: E402
from coding_tool import (  # noqa: E402
    ApiError,
    ClaudeCodingTool,
    JsonSession,
    query_json_in_session,
)


def _tool(monkeypatch, side_effect):
    """A tool whose ``_run_interactive_resilient`` is driven by ``side_effect``.

    ``side_effect(n, requested, recorded)`` gets the 1-based dispatch
    index, the session id the dispatch asked for, and the dispatch log so
    far; it returns either ``(text, session_id)`` — the pair the real
    method returns — or an exception instance to raise. Every dispatch is
    recorded with its kwargs, which is what the assertions below read.
    """
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    recorded: list[dict] = []

    def _spy(prompt, system_instruction, **kwargs):
        # The launcher stamps the provider it selected; the fallback and
        # degradation paths read it back to say which provider missed.
        tool.current_call_provider = "vendor-a-pro"
        recorded.append({"prompt": prompt, **kwargs})
        result = side_effect(
            len(recorded), kwargs.get("session_id"), recorded
        )
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tool, "_run_interactive_resilient", _spy)
    tool._calls = recorded
    return tool


def _ok(text, sid=None):
    """A dispatch that answers with ``text`` in the session it was asked for.

    That echo is what the CLI does in the ordinary case, and it is what
    lets a test say "the second round resumed the first round's session"
    without hard-coding a uuid. ``sid`` overrides it, which is how a CLI
    that re-keys a conversation is modelled.
    """

    def _side(n, requested, recorded):
        return (text, requested if sid is None else sid)

    return _side


# ---------------------------------------------------------------------------
# 1. The loop's second round resumes the session the first round used
# ---------------------------------------------------------------------------


def test_the_second_round_resumes_the_session_the_first_round_used(monkeypatch):
    tool = _tool(monkeypatch, _ok(json.dumps({"tasks": []})))

    session = JsonSession()
    query_json_in_session(tool, session, prompt="round one")
    query_json_in_session(tool, session, prompt="round two again")

    first, second = tool._calls
    assert first["resume"] is False, "round one opens the session"
    assert second["resume"] is True, (
        "round two must re-open it; a fresh session re-reads every file "
        "the prompt names, which is the cost this exists to avoid"
    )
    assert second["session_id"] == first["session_id"]
    assert session.session_id == first["session_id"], (
        "the handle names the session the dispatch ran in"
    )


def test_the_first_round_has_no_session_to_resume_and_does_not_try(monkeypatch):
    """The chicken-and-egg, on the round where it bites.

    A handle starts empty. Resuming it would aim ``--resume`` at a
    session that was never created, fail, degrade — and look like reuse
    that quietly never happened.
    """
    tool = _tool(monkeypatch, _ok(json.dumps({}), "sid-1"))
    assert JsonSession().session_id is None

    query_json_in_session(tool, JsonSession(), prompt="round one")

    assert len(tool._calls) == 1
    assert tool._calls[0]["resume"] is False


def test_the_reask_carries_the_new_request_not_only_a_continuation_cue(
    monkeypatch,
):
    """The restored conversation does not contain what changed.

    The provider-switch cue describes a turn cut off mid-task and asks
    the model to pick that task back up. A loop's next round is a
    different ask — a validator's objection, the reason the last answer
    was rejected — and that text exists nowhere in the transcript.
    Sending the cue alone would make round two answer round one's
    question again, which is the one outcome reuse must never produce.
    """
    tool = _tool(monkeypatch, _ok(json.dumps({})))
    session = JsonSession()

    query_json_in_session(tool, session, prompt="the original ask")
    query_json_in_session(
        tool, session, prompt="the original ask, and this objection: it is wrong",
    )

    resumed = tool._calls[1]
    assert resumed["resume"] is True
    assert "it is wrong" in resumed["prompt"], (
        "what changed since the last round has to travel with the turn"
    )
    assert "Return ONLY the JSON object" in resumed["prompt"], (
        "a resumed turn still needs the format — the restored context "
        "ends on the previous answer, not on the instruction"
    )
    assert "cut off" not in resumed["prompt"], (
        "nothing was cut off; this turn is a re-ask, not a resumption"
    )


def test_the_provider_switch_keeps_its_own_turn_text(monkeypatch):
    """The two resume reasons must not be confused for one another.

    A provider switch is the same ask on another provider, so the cue
    plus the format instruction is right there and the prompt must not
    be sent twice.
    """
    tool = _tool(monkeypatch, _ok(json.dumps({"ok": True})))

    tool._query_json_internal("the ask", None, None, resume_session="sid-explicit")

    resumed = tool._calls[0]
    assert resumed["resume"] is True
    assert "cut off" in resumed["prompt"]
    assert "the ask" not in resumed["prompt"]


# ---------------------------------------------------------------------------
# 2. The real session id travels back to the caller
# ---------------------------------------------------------------------------


def test_the_handle_carries_the_id_the_dispatch_reported_not_the_one_requested(
    monkeypatch,
):
    """The caller cannot guess this id, so it must be handed back.

    The dispatch is launched under a pinned uuid, and the CLI answers
    with the session it actually ran in. Those differ whenever the CLI
    re-keys the conversation. Resuming the *requested* id would aim at a
    session with no transcript; the reported id is the live one.
    """

    def _side(n, requested, recorded):
        return (json.dumps({}), "sid-reported-by-cli")

    tool = _tool(monkeypatch, _side)
    session = JsonSession()
    query_json_in_session(tool, session, prompt="round one")
    query_json_in_session(tool, session, prompt="round two")

    assert session.session_id == "sid-reported-by-cli"
    assert tool._calls[1]["session_id"] == "sid-reported-by-cli"
    assert tool._calls[0]["session_id"] != "sid-reported-by-cli", (
        "the fixture only proves this if the two ids actually differ"
    )


def test_the_handle_is_empty_until_a_dispatch_has_answered(monkeypatch):
    tool = _tool(monkeypatch, _ok(json.dumps({})))
    session = JsonSession()
    query_json_in_session(tool, session, prompt="round one")
    assert session.session_id == tool._calls[0]["session_id"]


# ---------------------------------------------------------------------------
# 3. A reply too broken to parse is still a session worth re-asking in
# ---------------------------------------------------------------------------


def test_a_reply_that_failed_to_parse_still_hands_back_its_session(monkeypatch):
    """The refiner's re-ask loop lives exactly here."""

    def _side(n, requested, recorded):
        # Dispatches 1 and 2 are the ask and its internal format retry.
        if n <= 2:
            return ("not json at all", requested)
        return (json.dumps({"tasks": []}), requested)

    tool = _tool(monkeypatch, _side)
    session = JsonSession()
    with pytest.raises(json.JSONDecodeError):
        query_json_in_session(tool, session, prompt="round one")

    # Round one: the ask, then its own format retry inside the session.
    assert tool._calls[0]["resume"] is False
    assert tool._calls[1]["resume"] is True
    assert tool._calls[1]["session_id"] == tool._calls[0]["session_id"]
    assert session.session_id == tool._calls[0]["session_id"], (
        "the session answered, it just answered badly; the next round "
        "must re-ask in it"
    )

    query_json_in_session(tool, session, prompt="round one, with a nudge")
    reask = tool._calls[2]
    assert reask["resume"] is True
    assert reask["session_id"] == tool._calls[0]["session_id"]


# ---------------------------------------------------------------------------
# 4. A resume that cannot be honoured degrades, it does not fail
# ---------------------------------------------------------------------------


def _dead_session_on_the_second_round(monkeypatch, *, fail_with):
    """Round one answers; the re-open of its session is refused."""
    def _side(n, requested, recorded):
        if n == 1:
            return (json.dumps({"ok": True}), "sid-1")
        if recorded[n - 1]["resume"]:
            return fail_with
        return (json.dumps({"ok": True}), "sid-fresh")

    return _tool(monkeypatch, _side)


def test_a_session_that_cannot_be_reopened_falls_back_and_still_answers(
    monkeypatch,
):
    tool = _dead_session_on_the_second_round(
        monkeypatch,
        fail_with=ApiError("No conversation found with session ID", status="unknown"),
    )
    session = JsonSession()
    query_json_in_session(tool, session, prompt="round one")
    assert session.session_id == "sid-1"

    result = query_json_in_session(tool, session, prompt="round two")

    assert result == {"ok": True}, "the loop still gets an answer"
    assert tool._calls[1]["resume"] is True, "it did try to re-open first"
    assert tool._calls[2]["resume"] is False, "then fell back to a fresh session"
    assert tool._calls[2]["session_id"] != "sid-1"


def test_the_degradation_is_logged_rather_than_silent(monkeypatch):
    tool = _dead_session_on_the_second_round(
        monkeypatch,
        fail_with=ApiError("No conversation found with session ID", status="unknown"),
    )
    session = JsonSession()
    query_json_in_session(tool, session, prompt="round one")
    query_json_in_session(tool, session, prompt="round two")

    events = [
        call for call in tool.logger.warning.call_args_list
        if call[0] and call[0][0] == "coding_resume_fallback"
    ]
    assert events, (
        "a silent degradation is indistinguishable from reuse that worked"
    )
    data = events[0][1]["data"]
    assert data["fallback"] == "fresh_session"
    assert data["session_id"] == "sid-1"
    assert data["error"]


def test_the_handle_follows_the_degradation_instead_of_the_dead_session(
    monkeypatch,
):
    """Otherwise round three aims at the session that just failed."""
    tool = _dead_session_on_the_second_round(
        monkeypatch,
        fail_with=ApiError("No conversation found with session ID", status="unknown"),
    )
    session = JsonSession()
    query_json_in_session(tool, session, prompt="round one")
    query_json_in_session(tool, session, prompt="round two")

    assert session.session_id == "sid-fresh", (
        "the handle must name the session that exists, so the loop stops "
        "re-asking a session that cannot be reopened"
    )


# ---------------------------------------------------------------------------
# 5. Reverse controls — the paths that must not move
# ---------------------------------------------------------------------------


def test_a_caller_that_asks_for_nothing_gets_a_fresh_session_every_time(
    monkeypatch,
):
    """The whole reason the opt-in rides on a context variable.

    Nothing here passes a session, so the dispatch must be identical to
    what it was before reusable sessions existed: ``resume=False`` and a
    new id each time.
    """
    tool = _tool(monkeypatch, _ok(json.dumps({"ok": True})))

    tool.query_json("first unrelated question")
    tool.query_json("second unrelated question")

    assert len(tool._calls) == 2
    assert all(c["resume"] is False for c in tool._calls)
    assert tool._calls[0]["session_id"] != tool._calls[1]["session_id"]


def test_the_handle_does_not_leak_into_the_next_calling_scope(monkeypatch):
    tool = _tool(monkeypatch, _ok(json.dumps({})))
    query_json_in_session(tool, JsonSession(), prompt="inside the scope")

    tool.query_json("outside the scope")

    assert tool._calls[-1]["resume"] is False, (
        "the scope is per call: a later, unrelated caller must not be "
        "handed a conversation it had no part in"
    )


def test_an_explicit_provider_fallback_session_outranks_the_loop_handle(
    monkeypatch,
):
    """The recursion that switches providers mid-answer.

    It resumes the session that just timed out. Reading the handle
    instead would resume whatever the loop was holding and send the
    retry off to answer a different question.
    """
    tool = _tool(monkeypatch, _ok(json.dumps({})))
    session = JsonSession()
    session.session_id = "sid-from-the-loop"

    query_json_in_session(tool, session, prompt="anything")
    tool._query_json_internal("a prompt", None, None, resume_session="sid-explicit")

    assert tool._calls[-1]["session_id"] == "sid-explicit"
    assert tool._calls[-1]["resume"] is True


def test_a_hard_timeout_is_not_read_as_a_reuse_failure(monkeypatch):
    """Same rule as the provider switch: slow is not gone."""
    import coding_tool as ct

    def _side(n, requested, recorded):
        if n == 1:
            return (json.dumps({}), requested)
        raise ct.HardTimeoutError(total_sec=10, elapsed=11.0, last_line="")

    tool = _tool(monkeypatch, _side)
    session = JsonSession()

    query_json_in_session(tool, session, prompt="round one")
    with pytest.raises(ct.HardTimeoutError):
        query_json_in_session(tool, session, prompt="round two")

    assert len([c for c in tool._calls if not c["resume"]]) == 1, (
        "the slow provider keeps its own routing; it must not be retried "
        "here as a fresh session"
    )


# ---------------------------------------------------------------------------
# 6. The loops themselves — end to end, caller down to the dispatch
# ---------------------------------------------------------------------------


def _refiner(tool):
    from refiner import TaskRefiner

    config = MagicMock()
    config.refiner_system_prompt = "refine!"
    config.domain_knowledge = ""
    return TaskRefiner(tool, config, project_dir=".")


def _patched_validator(reports):
    """A validator that returns ``reports`` in order, then repeats the last."""
    validator = MagicMock()
    validator.auto_fix.side_effect = lambda snapshot: snapshot
    seen = {"n": 0}

    def _validate(_snapshot):
        report = reports[min(seen["n"], len(reports) - 1)]
        seen["n"] += 1
        return report

    validator.validate.side_effect = _validate
    return validator


def _report(status, reasons=()):
    report = MagicMock()
    report.status = status
    report.reasons = list(reasons)
    report.failed_steps = []
    report.failed_task_ids = []
    return report


def _task():
    return {"id": "1", "title": "t", "description": "d",
            "test_command": "true", "status": "pending"}


def test_the_refiner_correction_round_resumes_the_refiner_s_own_session(
    monkeypatch,
):
    """L2: round N+1 is round N plus the validator's objection.

    Same subject, same files, same plan — so round two re-reads nothing.
    """
    task = _task()

    def _side(n, requested, recorded):
        return (json.dumps({"tasks": [dict(task)]}), requested)

    tool = _tool(monkeypatch, _side)
    validator = _patched_validator(
        [_report("failed", ["description is empty"]), _report("passed")]
    )
    monkeypatch.setattr(refiner_module, "TaskOutputValidator", lambda *_: validator)
    monkeypatch.setattr(refiner_module, "watchdog_signal", MagicMock())

    tasks = _refiner(tool).refine(
        requirement="demo", tasks=[dict(task)],
        last_coder_response="tried something", last_result="tests failed",
        exit_code=1, last_task_id="1",
    )

    assert len(tasks) == 1
    assert len(tool._calls) == 2, "one dispatch per round"
    assert tool._calls[0]["resume"] is False
    assert tool._calls[1]["resume"] is True, (
        "the correction round is the same subject asked again"
    )
    assert tool._calls[1]["session_id"] == tool._calls[0]["session_id"]
    assert "description is empty" in tool._calls[1]["prompt"], (
        "and it really is the correction round, not some other call"
    )


def test_the_refiner_reask_after_an_unparseable_reply_resumes(monkeypatch):
    """L1: the format re-ask, moments later, same prompt plus a nudge."""
    task = _task()

    def _side(n, requested, recorded):
        # Dispatches 1 and 2 are round one's ask and its internal format
        # retry; dispatch 3 is the refiner's own re-ask.
        if n <= 2:
            return ("prose, not json", requested)
        return (json.dumps({"tasks": [dict(task)]}), requested)

    tool = _tool(monkeypatch, _side)
    validator = _patched_validator([_report("passed")])
    monkeypatch.setattr(refiner_module, "TaskOutputValidator", lambda *_: validator)
    monkeypatch.setattr(refiner_module, "watchdog_signal", MagicMock())

    _refiner(tool).refine(
        requirement="demo", tasks=[dict(task)],
        last_coder_response="tried something", last_result="tests failed",
        exit_code=1, last_task_id="1",
    )

    assert len(tool._calls) == 3
    assert tool._calls[2]["resume"] is True
    assert tool._calls[2]["session_id"] == tool._calls[0]["session_id"]


def test_a_second_refine_call_starts_a_fresh_session(monkeypatch):
    """The handle is scoped to one ``refine`` call.

    A different task is a different subject; sharing a session would
    hand the model the previous task's files as if they were context
    for this one.
    """
    task = _task()
    tool = _tool(monkeypatch, _ok(json.dumps({"tasks": [dict(task)]}), "sid-refiner"))
    validator = _patched_validator([_report("passed")])
    monkeypatch.setattr(refiner_module, "TaskOutputValidator", lambda *_: validator)
    monkeypatch.setattr(refiner_module, "watchdog_signal", MagicMock())

    refiner = _refiner(tool)
    for task_id in ("1", "2"):
        refiner.refine(
            requirement="demo", tasks=[dict(task)],
            last_coder_response="tried something", last_result="tests failed",
            exit_code=1, last_task_id=task_id,
        )

    assert all(c["resume"] is False for c in tool._calls)


def _generator(tool, tmp_path, name="P-1"):
    from repair_generator import RepairTaskGenerator

    plan_dir = tmp_path / "plans" / name
    plan_dir.mkdir(parents=True, exist_ok=True)
    return RepairTaskGenerator(
        coding_tool=tool, plan_dir=plan_dir, project_dir=tmp_path,
    )


def test_the_test_command_fill_loop_resumes_its_own_attempts(monkeypatch, tmp_path):
    """L3: one session for the loop, not one per outstanding point.

    The fill call covers every outstanding point at once, so the unit of
    reuse is the loop. Splitting it per point would multiply the number
    of calls, which is the opposite of what reuse is for.
    """
    def _side(n, requested, recorded):
        # The first answer carries no command for the point, so the loop
        # asks again.
        if n == 1:
            return (json.dumps({"VP-009": ""}), requested)
        return (json.dumps({"VP-009": "pytest tests/test_x.py -k y"}), requested)

    tool = _tool(monkeypatch, _side)
    gen = _generator(tool, tmp_path)
    monkeypatch.setattr(gen, "_passes_falsifiability", lambda command: True)
    monkeypatch.setattr(gen, "_verification_plan_points", lambda: {})

    contents = [{"failed_vp_id": "VP-009", "title": "t", "description": "d"}]
    failed_vps = [{"id": "VP-009", "request": "r", "assertions": ["a"]}]
    filled = gen._fill_missing_test_commands(contents, failed_vps, 1)

    assert filled == {"VP-009": "pytest tests/test_x.py -k y"}
    assert len(tool._calls) == 2, "one call per attempt, not per point"
    assert tool._calls[0]["resume"] is False
    assert tool._calls[1]["resume"] is True, (
        "the second attempt carries the reason the first answer was "
        "unusable — that is a correction of the same answer"
    )
    assert tool._calls[1]["session_id"] == tool._calls[0]["session_id"]


def _failed_vp():
    return {
        "id": "VP-009",
        "title": "窗口内无信号返回 200 + 空集合",
        "priority": "high",
        "actual_result": "AssertionError: expected length 5 == 0",
        "evidence": "assertion failure",
    }


def _failed_report():
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-009", "status": "FAILED", "actual_result": "boom"},
        ],
        "requirement_deviations": [],
    }


def _orchestrator(monkeypatch, tmp_path, tool, plan_id="P-1"):
    """A real orchestrator — and therefore a real repair generator — whose
    only stub is the verification agent.
    """
    from unittest.mock import patch

    from verification.orchestrator import VerificationOrchestrator

    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": []}', encoding="utf-8",
    )
    with patch("verification.orchestrator.VerificationAgent"):
        orch = VerificationOrchestrator(plan_dir, tmp_path, coding_tool=tool)
    # Stub the two collaborators that stand between the orchestrator and
    # the repair-content LLM call, so every dispatch recorded below is
    # that call and nothing else.
    monkeypatch.setattr(
        "verification.verification_report_reader.extract_failed_vps_with_paths",
        lambda *a, **k: [_failed_vp()],
    )
    monkeypatch.setattr(
        VerificationOrchestrator, "_apply_vp_split_decisions",
        lambda self, failed_vps, round_number: (failed_vps, []),
    )
    monkeypatch.setattr(
        VerificationOrchestrator, "_resolve_repair_vp_commands",
        lambda self, *a, **k: {}, raising=False,
    )
    return orch


def test_repair_content_generation_resumes_across_verification_rounds(
    monkeypatch, tmp_path
):
    """L4: round N+1 of one plan is round N plus the evidence it missed.

    Driven from the orchestrator's ``check_cycle_conditions``, because
    that is where the repair path is entered in production, and with a
    **fresh orchestrator** for the second round. The fresh instance is
    not a detail: ``server.py``'s repair-complete callback builds a new
    ``VerificationOrchestrator`` — and therefore a new
    ``RepairTaskGenerator`` — for every post-repair round, so a handle
    held on either object is empty again on round two and the reuse this
    test asserts would not happen in production. Only a handle that
    outlives both objects makes it real.
    """
    import repair_generator as rg

    # The RED probe is not what this test is about, and leaving it on made
    # the test's answer depend on the machine it ran on. The command above
    # is a stand-in that names ``pytest``, which exists on a developer
    # machine and does not exist on a CI runner (CI invokes
    # ``.venv/bin/python3 -m pytest`` and never puts ``.venv/bin`` on
    # PATH). With no ``pytest`` the probe exits 127, the command is
    # correctly refused as "cannot start", and the generator spends extra
    # calls re-asking for an acceptance command it is never going to get —
    # so the call count this test asserts on was 6 on CI and 2 here.
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    tool = _tool(monkeypatch, _ok(json.dumps({"tasks": [{
        "failed_vp_id": "VP-009",
        "title": "修复 VP-009",
        "description": "让窗口语义成为契约内的真实行为",
        "test_command": "pytest -q tests/test_window.py",
    }]})))

    with patch("repair_generator.RepairTaskAssembler") as MockAssembler:
        MockAssembler.return_value.assemble.return_value = [
            {"id": "repair-r1-01", "title": "修复 VP-009"},
        ]
        first = _orchestrator(monkeypatch, tmp_path, tool)
        first.check_cycle_conditions(_failed_report(), round_number=1)
        # Exactly what ``_on_repair_complete`` does between rounds.
        second = _orchestrator(monkeypatch, tmp_path, tool)
        # Round 2 repeats round 1's failure set, which is exactly the
        # condition that stops the loop. Raising the cap isolates the
        # reuse assertion from that rule, which has its own tests
        second._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 99
        second.check_cycle_conditions(_failed_report(), round_number=2)

    assert len(tool._calls) == 2, (
        f"expected one content call per round, got {len(tool._calls)}"
    )
    assert tool._calls[0]["resume"] is False, (
        "round one opens the plan's session"
    )
    assert tool._calls[1]["resume"] is True, (
        "the same plan's next round asks about the same repository, so it "
        "re-opens the session instead of re-reading every file the prompt "
        "names — this is the assertion that fails if the handle is hung "
        "on the generator"
    )
    assert tool._calls[1]["session_id"] == tool._calls[0]["session_id"], (
        "round two must re-open the session round one actually ran in, "
        "not one the caller had to guess"
    )
    assert rg.repair_content_session(second.plan_dir).session_id == (
        tool._calls[0]["session_id"]
    ), "the handle names the session the dispatch reported"


def test_a_second_plan_starts_a_fresh_session(monkeypatch, tmp_path):
    """The handle is keyed by plan, so one plan's repair history is not
    another plan's context.
    """
    import repair_generator as rg

    # Same reason as the case above: the RED probe is not under test here,
    # and its verdict on a stand-in command that names ``pytest`` depends
    # on whether the machine running this has ``pytest`` on PATH.
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    tool = _tool(monkeypatch, _ok(json.dumps({"tasks": [{
        "failed_vp_id": "VP-009",
        "title": "修复 VP-009",
        "description": "d",
        "test_command": "pytest -q tests/test_window.py",
    }]})))

    seen = []
    for name in ("P-1", "P-2"):
        with patch("repair_generator.RepairTaskAssembler") as MockAssembler:
            MockAssembler.return_value.assemble.return_value = [
                {"id": "repair-r1-01", "title": "修复 VP-009"},
            ]
            orch = _orchestrator(monkeypatch, tmp_path, tool, plan_id=name)
            orch.check_cycle_conditions(_failed_report(), round_number=1)
            seen.append(tool._calls[-1]["session_id"])

    assert all(c["resume"] is False for c in tool._calls), (
        "neither plan has a previous round to resume"
    )
    assert len(set(seen)) == 2, "each plan gets its own live session"
