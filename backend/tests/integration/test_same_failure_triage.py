"""Integration tests for the same-failure triage (同因循环控制).

Why this file exists
====================

The loop control used to end the chain the moment the same failure set
repeated a second time: no repair tasks, and a bare string
(``same_failure_repeated_after_max_attempts``) as the reason. A human
reading that verdict learns *that* the loop stopped and nothing about
*why* — and the loop had no way to tell a broken criterion (a human
must fix the check) from a wrong deliverable (the work must be redone).

The triage changed that. The same failure repeating for
``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` rounds now buys **one**
semantic triage call, and the triage's verdict decides:

* a **criterion defect** → stop (a human has to fix the criterion);
* a **deliverable defect** → divert into the repair path, with a fresh
  budget for that failure;
* **a triage that ran and could not characterise the failure** → stop,
  with a *structured* reason naming the point, the failure class and
  the triage's words;
* **a triage that malfunctioned** → do not stop at all — a broken triage
  is not evidence about the failure, so the repair path gets its chance
  and the same-cause counter is left alone.

Four contracts are pinned here, and they are the ones that decide
whether the triage may act at all:

1. **The threshold buys exactly one triage call.** Not one per failed
   point, not one per round.
2. **A triage that cannot conclude stops the loop with a structured
   reason** — never with a bare string, never silently.
3. **A triage that malfunctioned does not stop the loop.** The repair
   tasks are still generated and the same-cause counter is left alone.
   The shipped ``VPTriageJudge`` never raises — it answers with a
   conclusion whatever went wrong — so the malfunctions that matter
   are its own three: the provider blew up, the answer was not a JSON
   object, the quote did not verify against the recorded failure. The
   tests below drive the real judge through them, with only the
   provider doubled.
4. **Infrastructure failures never reach the threshold.** They do not
   count, so two rounds of them buy no triage call — and a failure whose
   *nature changed* is two different failures, each counting once.

Three more pin what the verdict *is*, and they are what keeps the
structured reason from breaking the machine that reads it:

5. **The reason is a code to the machine and a verdict to a human.**
   The state machine's vocabulary is a closed set of strings and the
   verification stack compares a stop reason against it; a mapping
   that reached that stack would raise (``dict`` is unhashable) and
   strand the plan inside the verification family. So the reason is
   the code, and the verdict travels with it — readable by subscript
   (``reason["vp_id"]``) and by comparison (``reason == {...}``).
6. **The verdict outlives the round.** The chain stops in a process
   that ends right there, so the verdict is written to the plan's
   directory — otherwise the human who opens the plan finds only a
   bare code that says *that* the chain gave up.
7. **The seam never hands a mapping across.** Whatever shape a round's
   payload has, the code a consumer reads is a string.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Make ``backend/`` importable regardless of the test runner's entry
# point (``verification`` is a package under ``backend/``).
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import VerificationOrchestrator  # noqa: E402


#: The verification point every scenario fails on.
VP_ID = "VP-007"

#: The failure class the report records — the counter counts per
#: ``(point, class)`` pair, so the class is part of the failure's
#: identity.
FAILURE_CLASS = "criterion_defect"

#: The recorded failure text. The judge may only quote from text that is
#: really there, so it is quotable verbatim.
ACTUAL_RESULT = "AssertionError: the response schema was rejected"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point the state machine at a hermetic per-test SQLite file.

    Without the override the orchestrator would write the ``test-plan``
    row into the repo's real ``state.db``.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def repair_mocks():
    """Mock the repair path's collaborators.

    The repair path (report → repair contents → assembled tasks) is
    mocked so the divert-to-repair outcome can be observed without an
    LLM call.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator, \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": VP_ID, "title": "Fix VP-007", "priority": "high"},
        ]
        mock_generator.return_value.generate_repair_contents.return_value = [
            {"failed_vp_id": VP_ID, "title": "Fix VP-007"},
        ]
        mock_assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "failed_vp_id": VP_ID, "title": "Fix VP-007"},
        ]
        yield mock_extract


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the way a real plan carries it."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase not in ("interview",) else [],
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


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class _FakeJudge:
    """A triage judge that records its calls and answers with a verdict.

    ``raises`` makes the judge malfunction — the "the provider blew up"
    path, which the real judge turns into an inconclusive conclusion and
    a broken judge turns into an exception.
    """

    def __init__(self, conclusion=None, raises=None):
        self._conclusion = conclusion
        self._raises = raises
        self.calls = []

    def judge(self, vp, failure_history, round_number=0):
        self.calls.append({
            "vp": dict(vp) if isinstance(vp, dict) else vp,
            "failure_history": list(failure_history or []),
            "round_number": round_number,
        })
        if self._raises is not None:
            raise self._raises
        return self._conclusion


def _conclusion(failure_class="deliverable_defect", conclusive=True,
                rationale="the deliverable is wrong"):
    """A real :class:`TriageConclusion`, so the shape is the shipped one."""
    from verification.triage_agent import TriageConclusion

    return TriageConclusion(
        vp_id=VP_ID,
        failure_class=failure_class,
        conclusive=conclusive,
        evidence_quote=ACTUAL_RESULT,
        rationale=rationale,
    )


class _BrokenProvider:
    """A coding tool whose triage call fails in one specific way.

    The three ways are the ones the shipped judge meets in production:
    the provider blows up, the answer is not a JSON object, and the
    answer carries a quote that appears nowhere in the recorded
    failure. Each of them used to be swallowed into an ordinary
    inconclusive conclusion — indistinguishable from "the judge ran and
    could not characterise the failure" — and the loop stopped on it.

    Only the *provider* is a double here: the judge in front of it is
    the shipped :class:`VPTriageJudge`.
    """

    def __init__(self, mode):
        self.mode = mode

    def query_json(self, **kwargs):
        if self.mode == "raises":
            raise RuntimeError("provider exploded")
        if self.mode == "not_json":
            return "this is not a json object at all"
        if self.mode == "bad_quote":
            return {
                "failure_class": "criterion_defect",
                "evidence_quote": "a quote that appears nowhere in the record",
                "rationale": "the criterion is broken",
            }
        if self.mode == "outside_vocabulary":
            return {
                "failure_class": "environment_bug",
                "evidence_quote": ACTUAL_RESULT,
                "rationale": "the machine was busy",
            }
        raise AssertionError(f"unknown provider mode: {self.mode}")


class _VerifiableProvider:
    """A coding tool whose triage call *succeeds*.

    The control for the tests above: the judge answers with a class it
    can act on and a quote that really is in the recorded failure. The
    triage worked, so the loop has to act on what it said — which is
    what separates "the triage malfunctioned" from "the triage ran and
    had something to say".
    """

    def query_json(self, **kwargs):
        return {
            "failure_class": "deliverable_defect",
            "evidence_quote": ACTUAL_RESULT,
            "rationale": "the deliverable does not do what the point asks",
        }


def _real_judge_orchestrator(plan_dir, project_dir, coding_tool, provider):
    """The orchestrator under test, with the *shipped* judge in front.

    The judge is the real :class:`VPTriageJudge`; only the provider it
    calls is a double. A fake judge that raises exercises the
    orchestrator's own error path and says nothing about the judge that
    ships — and the shipped judge never raises: it answers with a
    conclusion, whatever went wrong.
    """
    from verification.triage_agent import VPTriageJudge

    return VerificationOrchestrator(
        plan_dir, project_dir, coding_tool,
        triage_judge=VPTriageJudge(coding_tool=provider),
    )


def _report(failure_class=FAILURE_CLASS, vp_id=VP_ID):
    """A round report with one failed verification point."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": vp_id,
                "status": "FAILED",
                "failure_class": failure_class,
                "actual_result": ACTUAL_RESULT,
                "evidence": ACTUAL_RESULT,
            },
        ],
    }


def _orchestrator(plan_dir, project_dir, coding_tool, judge):
    """The orchestrator under test, with the judge injected."""
    return VerificationOrchestrator(
        plan_dir, project_dir, coding_tool, triage_judge=judge,
    )


def _run_round(orchestrator, report, round_number):
    """One verification round: start the cycle, then classify it."""
    orchestrator.start_verification_cycle(round_number=round_number)
    return orchestrator.check_cycle_conditions(report, round_number=round_number)


# ---------------------------------------------------------------------------
# 1. The threshold buys exactly one triage
# ---------------------------------------------------------------------------


def test_threshold_triggers_triage(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """Two rounds of the same failure buy exactly one triage call.

    The first round is not a repeat yet, so it buys nothing. The second
    round repeats the same failure — same point, same class — and that
    is the threshold: exactly one triage call, no more.
    """
    _write_plan_state(temp_plan_dir)
    judge = _FakeJudge(conclusion=_conclusion(failure_class="deliverable_defect"))
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )

    first = _run_round(orchestrator, _report(), round_number=1)
    assert judge.calls == [], (
        "the first failure is not a repeat yet — no triage is due"
    )
    assert first["should_stop"] is False

    second = _run_round(orchestrator, _report(), round_number=2)
    assert len(judge.calls) == 1, (
        "the threshold buys exactly one triage call, not one per point "
        "and not one per round"
    )
    # The triage named a deliverable defect: the loop keeps working on
    # it instead of stopping.
    assert second["should_stop"] is False
    assert second["repair_tasks"], (
        "a triage that says 'the work is wrong' must divert into repair"
    )


# ---------------------------------------------------------------------------
# 2. A triage that cannot conclude stops the loop, structurally
# ---------------------------------------------------------------------------


def test_completed_inconclusive_stops(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """No conclusion the judge could check → a structured stop reason.

    The judge *ran* and answered, and the answer says the failure is
    none of the four classes the loop can act on. That is the only
    "no answer" the loop stops on: the triage spoke, so a human has to
    look — and the reason says so, with the point, the failure class
    and the triage's own words.
    """
    _write_plan_state(temp_plan_dir)
    judge = _FakeJudge(
        conclusion=_conclusion(
            failure_class="deliverable_defect",
            conclusive=False,
            rationale="triage LLM output is not a JSON object",
        ),
    )
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    assert result["should_stop"] is True, (
        "a triage that could not conclude must not leave the loop running"
    )
    assert result["status"] == "loop_stopped"
    assert result["repair_tasks"] == []
    assert result["stop_reason"] == {
        "reason": "same_failure_no_triage_conclusion",
        "vp_id": VP_ID,
        "failure_class": FAILURE_CLASS,
        "triage_rationale": "triage LLM output is not a JSON object",
    }, "the stop reason must be structured, not a bare string"


# ---------------------------------------------------------------------------
# 2b. The structured verdict travels with a machine-facing code
# ---------------------------------------------------------------------------


def test_stop_reason_is_a_code_to_the_machine_and_a_verdict_to_a_human(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The reason must satisfy both contracts at once.

    The state machine's vocabulary is a closed set of strings, and the
    verification stack compares a stop reason against it: a verdict
    that reached that stack as a *mapping* would raise on the first
    membership test (``dict`` is unhashable, so ``reason in frozenset``
    is a ``TypeError``) and the plan it belonged to would be stranded
    inside the verification family.

    So the reason is the code — hashable, bindable, serialisable — and
    the triage's verdict travels with it.
    """
    _write_plan_state(temp_plan_dir)
    judge = _FakeJudge(
        conclusion=_conclusion(
            failure_class="deliverable_defect",
            conclusive=False,
            rationale="triage LLM output is not a JSON object",
        ),
    )
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    reason = result["stop_reason"]

    # (a) The machine's half: a string the vocabulary already knows.
    assert isinstance(reason, str), (
        "the state machine's vocabulary is a closed set of strings — a "
        "mapping here raises on the first membership test"
    )
    assert reason == "same_failure_repeated_after_max_attempts", (
        "the machine-facing code must be one the vocabulary knows, or "
        "the chain-ending decision changes with the verdict"
    )
    assert reason in {
        "same_failure_repeated",
        "same_failure_repeated_after_max_attempts",
    }, "the code must be found by the vocabulary's own membership test"

    # (b) The human's half: the triage's verdict, and the words in it.
    assert reason == {
        "reason": "same_failure_no_triage_conclusion",
        "vp_id": VP_ID,
        "failure_class": FAILURE_CLASS,
        "triage_rationale": "triage LLM output is not a JSON object",
    }, "the verdict must be readable off the reason"
    assert reason.verdict["vp_id"] == VP_ID
    assert reason.verdict["triage_rationale"] == (
        "triage LLM output is not a JSON object"
    )

    # (c) The verdict also *reads* like the mapping the brief's
    # expected output shows, so a caller that reaches for a key gets
    # the verdict's key rather than a TypeError.
    assert reason["vp_id"] == VP_ID
    assert reason["failure_class"] == FAILURE_CLASS
    assert reason["triage_rationale"] == (
        "triage LLM output is not a JSON object"
    )
    assert reason.get("triage_rationale") == (
        "triage LLM output is not a JSON object"
    )
    assert reason.get("no_such_key") is None


def test_the_verdict_is_written_to_the_plan_directory(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The verdict outlives the round that produced it.

    The chain stops in a process that ends right there, so an
    in-memory verdict dies with the round. The plan's own directory is
    where the other round-scoped records live, so the verdict goes
    there too — otherwise the human who opens the plan finds only a
    bare code that says *that* the chain gave up.
    """
    _write_plan_state(temp_plan_dir)
    judge = _FakeJudge(
        conclusion=_conclusion(
            failure_class="deliverable_defect",
            conclusive=False,
            rationale="triage LLM output is not a JSON object",
        ),
    )
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )

    _run_round(orchestrator, _report(), round_number=1)
    _run_round(orchestrator, _report(), round_number=2)

    path = temp_plan_dir / "verification_triage_verdict.json"
    assert path.exists(), (
        "the verdict must be recorded where a human can find it"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["round"] == 2
    assert payload["verdict"] == {
        "reason": "same_failure_no_triage_conclusion",
        "vp_id": VP_ID,
        "failure_class": FAILURE_CLASS,
        "triage_rationale": "triage LLM output is not a JSON object",
    }


def test_stop_reason_code_never_returns_a_mapping():
    """The helper's whole job: a string, whatever the payload holds.

    This is the seam between the orchestrator's verdict and the
    string-only vocabulary downstream. If it ever hands a dict across,
    the membership test raises and the plan is stranded — so every
    shape the payload can take is pinned here.
    """
    from verification.orchestrator import stop_reason_code

    assert stop_reason_code({
        "stop_reason_code": "a", "stop_reason": {"reason": "b"},
    }) == "a", "the code wins over the verdict when both are present"
    assert stop_reason_code({"stop_reason": "b"}) == "b", (
        "a producer that still ships a bare string keeps working"
    )
    assert stop_reason_code({"stop_reason": {"reason": "b"}}) == "b", (
        "a verdict without a code still yields a string, never a dict"
    )
    assert stop_reason_code({}) == ""


# ---------------------------------------------------------------------------
# 3. A triage that malfunctioned falls back to repair
# ---------------------------------------------------------------------------


def test_judge_exception_falls_back_to_repair(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """A judge that raises must not stop the loop.

    A broken triage is not evidence about the failure: the loop does not
    know what the failure is, so it must not act as if it did. The
    repair path gets its chance and the same-cause counter is left
    alone.
    """
    _write_plan_state(temp_plan_dir)
    judge = _FakeJudge(raises=RuntimeError("provider exploded"))
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    assert len(judge.calls) == 1, "the threshold was reached — the judge was asked"
    assert result["should_stop"] is False, (
        "a judge that raised is not a verdict — the loop must not stop on it"
    )
    assert result["stop_reason"] is None
    assert result["repair_tasks"], (
        "the fallback for a broken judge is the repair path, not a stop"
    )


# ---------------------------------------------------------------------------
# 4. Infrastructure failures never reach the threshold
# ---------------------------------------------------------------------------


def test_infra_not_counted(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """Two rounds of an infrastructure failure buy no triage call.

    An environment blip is not evidence that the work is wrong, so it
    does not move the same-cause counter — and a failure whose nature
    changed is a different failure, which counts once on its own.
    """
    _write_plan_state(temp_plan_dir)
    judge = _FakeJudge(conclusion=_conclusion())
    orchestrator = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )

    first = _run_round(
        orchestrator, _report(failure_class="infra"), round_number=1,
    )
    second = _run_round(
        orchestrator, _report(failure_class="infra"), round_number=2,
    )

    assert judge.calls == [], (
        "infrastructure failures do not count — two rounds of them must "
        "not reach the threshold"
    )
    assert first["should_stop"] is False
    assert second["should_stop"] is False

    # A failure whose nature changed is a different failure: it counts
    # once, so it does not reach the threshold either.
    orchestrator2 = _orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool, judge,
    )
    _run_round(
        orchestrator2, _report(failure_class="deliverable_defect"),
        round_number=1,
    )
    _run_round(
        orchestrator2, _report(failure_class="criterion_defect"),
        round_number=2,
    )
    assert judge.calls == [], (
        "a failure whose class changed counts once — the two are "
        "different failures and neither reaches the threshold"
    )


# ---------------------------------------------------------------------------
# 5. The real judge: a triage that malfunctioned does not stop the loop
# ---------------------------------------------------------------------------
#
# Everything above drives the loop with a *fake* judge — one that
# raises, or one that hands back a conclusion it built itself. That
# covers the orchestrator's own error path, and it says nothing about
# the judge that actually ships.
#
# The shipped judge never raises. Whatever goes wrong — the provider
# blows up, the answer is not a JSON object, the quote the answer
# carries appears nowhere in the recorded failure — it answers with a
# conclusion. And that conclusion used to be indistinguishable from
# "the judge ran and could not characterise the failure", so the loop
# stopped on it: the chain died with no repair path, over a triage
# that never classified anything.
#
# The two situations are different and must stay different:
#
# * the triage **malfunctioned** → the loop does *not* stop: the
#   failure was never classified, so the repair path gets its chance
#   and the same-cause counter is left alone;
# * the triage **ran and could not characterise** → the loop stops
#   with the structured reason, because the failure really is
#   something only a human can look at.


def _assert_malfunction_continues_with_repair(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks, mode,
):
    """The body the three malfunction tests share.

    A triage that malfunctioned is not evidence about the failure: the
    provider blew up, the answer was not readable, or the quote it
    carried could not be verified against the recorded failure. None of
    those is a statement about the failure itself, so the loop must not
    stop on it — the repair path gets its chance and the round still
    produces repair tasks.
    """
    _write_plan_state(temp_plan_dir)
    orchestrator = _real_judge_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
        _BrokenProvider(mode),
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    assert result["should_stop"] is False, (
        "a triage that malfunctioned must not stop the loop — the "
        "failure was never classified"
    )
    assert result["stop_reason"] is None, (
        "a triage that malfunctioned carries no stop reason — there is "
        "nothing for a human to act on yet"
    )
    assert result["repair_tasks"], (
        "the fallback for a malfunctioning triage is the repair path, "
        "not a stop"
    )


def test_provider_exception_continues_with_repair(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The provider blew up → the triage is unavailable → the loop runs on."""
    _assert_malfunction_continues_with_repair(
        temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
        "raises",
    )


def test_non_json_output_continues_with_repair(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The answer was not a JSON object → the triage is unavailable.

    The provider answered, but what it answered is not something the
    judge could read, so the judge classified nothing — which says
    nothing about the failure either.
    """
    _assert_malfunction_continues_with_repair(
        temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
        "not_json",
    )


def test_unverifiable_quote_continues_with_repair(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The quote does not verify → the triage is unavailable.

    The judge named a class and pointed at text that is not in the
    recorded failure. That is a failure of the *triage*, not a
    statement about the failure, so the loop must not stop on it.
    """
    _assert_malfunction_continues_with_repair(
        temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
        "bad_quote",
    )


@pytest.mark.parametrize("mode", ["raises", "not_json", "bad_quote"])
def test_a_malfunctioning_triage_leaves_the_counter_alone(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks, mode,
):
    """The same-cause counter survives a triage that malfunctioned.

    The counter is a tally of what the *failure* has cost. A triage
    that never classified the failure has not changed anything about
    it, so resetting the counter would hand the failure a fresh budget
    on the strength of a triage that said nothing.
    """
    _write_plan_state(temp_plan_dir)
    orchestrator = _real_judge_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
        _BrokenProvider(mode),
    )

    decision = orchestrator.classify_cycle(
        (VP_ID, FAILURE_CLASS), _report(), round_number=2,
    )

    assert decision["should_stop"] is False
    assert decision["stop_reason"] is None
    assert decision["void_counter"] is False, (
        "the triage decided nothing, so the counter must stand"
    )
    assert decision["reason"] == "triage_unavailable", (
        "the round has to say why the triage did not stop it"
    )


def test_a_triage_that_could_not_characterise_still_stops(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The judge ran, and could not name the failure → the loop stops.

    This is the one case where stopping is right: the triage delivered
    an answer and the answer says the failure is none of the four
    classes. The loop genuinely cannot tell what to do with it, so a
    human has to look — and the reason says so, with the point, the
    failure class and the triage's own words.
    """
    _write_plan_state(temp_plan_dir)
    orchestrator = _real_judge_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
        _BrokenProvider("outside_vocabulary"),
    )

    _run_round(orchestrator, _report(), round_number=1)
    result = _run_round(orchestrator, _report(), round_number=2)

    assert result["should_stop"] is True, (
        "a triage that ran and could not characterise the failure must "
        "stop the loop — there is nothing the loop can do about it"
    )
    assert result["status"] == "loop_stopped"
    assert result["repair_tasks"] == []
    assert result["stop_reason"] == {
        "reason": "same_failure_no_triage_conclusion",
        "vp_id": VP_ID,
        "failure_class": FAILURE_CLASS,
        "triage_rationale": (
            "triage returned a failure_class outside the four-class "
            "vocabulary: 'environment_bug'"
        ),
    }, "the stop reason must be structured, not a bare string"


def test_verifiable_conclusion_still_diverts_to_repair(
    temp_plan_dir, temp_project_dir, mock_coding_tool, repair_mocks,
):
    """The control: a triage that worked → the loop repairs, it does not stop.

    The three tests above say what a *malfunction* does. This one says
    what a working triage does, so the two cannot be confused: a
    conclusion the judge could check is a statement about the failure,
    and the loop acts on it — here, by diverting into the repair path
    with a fresh budget for that failure.
    """
    _write_plan_state(temp_plan_dir)
    orchestrator = _real_judge_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
        _VerifiableProvider(),
    )

    decision = orchestrator.classify_cycle(
        (VP_ID, FAILURE_CLASS), _report(), round_number=2,
    )

    assert decision["should_stop"] is False, (
        "a triage that named a class is not a reason to stop"
    )
    assert decision["stop_reason"] is None
    assert decision["reason"] == "deliverable_defect", (
        "the reason is the class the triage named"
    )
    assert decision["void_counter"] is True, (
        "the triage decided what this failure is, so the counter starts "
        "over for it"
    )


def test_the_judge_marks_a_malfunction_as_unavailable():
    """The judge says which kind of "no answer" it has.

    The conclusion carries the difference the loop acts on: a triage
    that malfunctioned is *unavailable*, and one that ran without
    naming a class is *available* but not conclusive. Without the
    distinction the loop cannot tell "the triage broke" from "the
    failure is unclassifiable" — and it stops on both.
    """
    from verification.triage_agent import VPTriageJudge

    def _judge(mode):
        return VPTriageJudge(coding_tool=_BrokenProvider(mode))

    for mode in ("raises", "not_json", "bad_quote"):
        conclusion = _judge(mode).judge(
            vp={"id": VP_ID, "actual_result": ACTUAL_RESULT},
            failure_history=[{"round": 2, "actual_result": ACTUAL_RESULT}],
            round_number=2,
        )
        assert conclusion.available is False, (
            f"{mode}: a triage that malfunctioned is not available"
        )
        assert conclusion.conclusive is False

    conclusion = _judge("outside_vocabulary").judge(
        vp={"id": VP_ID, "actual_result": ACTUAL_RESULT},
        failure_history=[{"round": 2, "actual_result": ACTUAL_RESULT}],
        round_number=2,
    )
    assert conclusion.available is True, (
        "the triage ran and answered — it is available, even though the "
        "answer names no class the loop can act on"
    )
    assert conclusion.conclusive is False
