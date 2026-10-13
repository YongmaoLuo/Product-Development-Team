"""A verification point with no ``test_command`` must not be discarded.

The defect this pins
--------------------
A repair task whose acceptance command resolves to ``""`` used to be
refused outright at generation time (``repair_task_dropped_empty_test_
command``). The refusal looked like a safety gate and was in fact
a dead end:

* A verification point written as a declarative API test carries no
  ``test_command`` string at all — its basis is ``request`` +
  ``assertions``. So *no* repair derived from such a point could ever
  carry one.
* The content prompt never asks the model for a command (its rule 5
  disclaims the field), so there was nothing to ask for either.
* The assembler resolves commands from the VP's own ``evidence_command``
  or from an ``api_test`` replay, and a plan built on declarative points
  has neither on most rows.

Every derived task then died at the gate, the round repaired nothing,
and the generation step reported a plausible count of refusals rather
than an error. A rule that reads as "don't ship an unverifiable task"
was in practice "don't ship any task".

The two ways out, both exercised below
--------------------------------------
**A — regenerate.** ``RepairTaskGenerator.generate_repair_contents``
re-asks the model, up to ``MAX_TEST_COMMAND_FILL_ATTEMPTS`` times,
carrying the whole verification point *and* the reason the previous
answer was unusable. A command found that way is adopted and its source
is recorded as ``llm_retry``.

**B — accept by category.** What remains is a task whose deliverable is
a findings document rather than a replayable command, and it is emitted
under the ``audit_style`` source instead of being discarded.

What B is *not*
---------------
B is not a licence to trust the sub-agent. ``_second_pass_gates_a_task_
with_no_runnable_command`` pins the property that makes emitting these
rows safe: ``agent._audit_task_second_pass`` routes every task without
a runnable command through an independent adversarial review, and that
review can veto the completion. Without it B would be exactly the
single-signal mode the dual-criterion rule exists to prevent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import repair_generator as rg  # noqa: E402
from repair_generator import (  # noqa: E402
    RepairTaskAssembler,
    RepairTaskGenerator,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _is_fill_prompt(prompt: str) -> bool:
    """Identify the fill loop's prompt by its own subject line.

    Dispatching on prompt content rather than on call index keeps each
    test readable about *which* call is which, and makes a test fail
    loudly if the loop stops asking rather than quietly passing on an
    index shift.
    """
    return "补验收命令" in prompt


class _ScriptedCodingTool:
    """A ``coding_tool`` that answers by inspecting the prompt."""

    def __init__(self, fill_responses: List[Any]):
        self._fill_responses = list(fill_responses)
        self.prompts: List[str] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> Any:
        self.prompts.append(prompt)
        if _is_fill_prompt(prompt):
            return self._fill_responses.pop(0)
        return {
            "tasks": [{
                "failed_vp_id": "VP-009",
                "title": "修复 VP-009",
                "description": "让窗口语义成为契约内的真实行为",
                "acceptance_criteria": "空窗口返回 200 + 空集合",
                "files_to_modify": ["app/signals.py"],
            }],
        }

    @property
    def fill_prompt_count(self) -> int:
        return sum(1 for p in self.prompts if _is_fill_prompt(p))


def _write_plan(plan_dir: Path, points: List[Dict[str, Any]]) -> Path:
    path = plan_dir / "verification_plan.json"
    path.write_text(
        json.dumps({"verification_points": points}), encoding="utf-8",
    )
    return path


def _generator(tmp_path: Path, coding_tool: Any) -> RepairTaskGenerator:
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    gen = RepairTaskGenerator(
        coding_tool=coding_tool,
        plan_dir=plan_dir,
        project_dir=tmp_path,
    )
    _write_plan(plan_dir, [_API_VP])
    return gen


#: A command the quality gate rejects on every fill attempt, so the
#: loop has something to keep rejecting. ``tee`` swallows pytest's exit
#: code, so the command exits 0 whether or not the repair took — it can
#: never report the difference.
_UNUSABLE_COMMAND = "pytest -q tests/test_signals_window.py | tee out.log"

#: The shape that broke: a declarative point with no command anywhere.
_API_VP = {
    "id": "VP-009",
    "title": "窗口内无信号返回 200 + 空集合",
    "verification_method": "api_test",
    "verification_phase": "1",
    "request": {
        "method": "GET",
        "url": "{{svc.api.url}}/api/signals?symbol=EXAMPLE&period=30min",
    },
    "assertions": [
        {"name": "状态码 200", "status": 200},
        {"name": "响应体是空数组", "json_path": "$", "length_equals": 0},
    ],
    "execution_group": "0",
}


def _failed_vp(**extra: Any) -> Dict[str, Any]:
    """A report-side failed-VP entry.

    Shaped like what ``extract_failed_vps_with_paths`` returns — which
    notably does *not* include ``verification_method``. Reading the
    method off this dict classifies nothing, and the assembler would
    then file an ``api_test`` point as though nothing could decide it.
    """
    entry = {
        "id": "VP-009",
        "title": "窗口内无信号返回 200 + 空集合",
        "priority": "medium",
        "actual_result": "[响应体是空数组] json_path $: expected length 5 == 0",
        "evidence": "assertion failure",
    }
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Path A — regenerate
# ---------------------------------------------------------------------------


def test_a_command_the_first_round_could_not_give_is_retried_and_adopted(
    tmp_path, monkeypatch,
):
    """The direct evidence for path A: a retry happened, and its answer won.

    Round 1 is the content call, which by design asks for no command, so
    the content dict arrives with none and no derivation path can supply
    one. Round 2 is the fill loop, and the command it returns is the one
    the assembler adopts.
    """
    # The probe runs each candidate as a subprocess; the fill loop's own
    # gates are what is under test here, not the probe.
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    coding_tool = _ScriptedCodingTool([
        {"commands": [{
            "failed_vp_id": "VP-009",
            "test_command": "pytest -q tests/test_signals_window.py",
        }]},
    ])
    gen = _generator(tmp_path, coding_tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    # A retry actually occurred: one content call plus one fill call.
    assert coding_tool.fill_prompt_count == 1, (
        "a command-less row must be re-asked before the assembler "
        "falls back to a label"
    )
    assert len(coding_tool.prompts) == 2

    # The second round's command was adopted, and the source says so.
    assert contents[0]["test_command"] == (
        "pytest -q tests/test_signals_window.py"
    )
    assert contents[0]["test_command_source"] == "llm_retry"

    # And it reaches the emitted task, labelled as the retry's product
    # rather than as a plan-derived command.
    assembler = RepairTaskAssembler(
        round_number=2,
        failed_vps=[_failed_vp()],
        vp_test_commands={},
        plan_path=str(gen.plan_dir / "verification_plan.json"),
    )
    tasks = assembler.assemble(contents)
    assert len(tasks) == 1
    assert tasks[0]["test_command"] == "pytest -q tests/test_signals_window.py"
    assert assembler.test_command_sources["repair-r2-01"] == "llm_retry"
    assert assembler.unverifiable_no_test_command == 0


def test_the_retry_carries_the_reason_the_first_answer_was_unusable(
    tmp_path, monkeypatch,
):
    """A retry is a correction, so it must be told what was wrong.

    Without the feedback the second call is a reroll of the first: the
    same prompt, the same distribution, and a model that produced a
    ``grep`` probe produces a ``grep`` probe again. This asserts the
    rejection reason reaches the next prompt.
    """
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    coding_tool = _ScriptedCodingTool([
        # First fill answer: a real test piped through ``tee``, which
        # swallows the exit code — the shape
        # ``test_command_quality`` rejects as structurally unpassable,
        # because it exits 0 whether or not the repair took.
        {"commands": [{
            "failed_vp_id": "VP-009",
            "test_command": "pytest -q tests/test_signals_window.py | tee out.log",
        }]},
        {"commands": [{
            "failed_vp_id": "VP-009",
            "test_command": "pytest -q tests/test_signals_window.py",
        }]},
    ])
    gen = _generator(tmp_path, coding_tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert coding_tool.fill_prompt_count == 2
    second_prompt = coding_tool.prompts[-1]
    assert "上一次为什么不可用" in second_prompt
    # The whole point is named, not just its id — a retry that cannot
    # see the request and the assertions cannot write a check against
    # them, and would produce the same unusable answer again.
    assert "/api/signals" in second_prompt
    assert "length_equals" in second_prompt
    assert "expected length 5 == 0" in second_prompt
    assert contents[0]["test_command"] == (
        "pytest -q tests/test_signals_window.py"
    )


def test_the_fill_loop_is_bounded_and_does_not_hang_on_a_silent_model(
    tmp_path, monkeypatch,
):
    """An unbounded re-ask would trade a dead round for a hung one.

    The cap exists for that reason, and it has to hold in the case a
    limit is actually written for: the model answers every time with
    something unusable.
    """
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    coding_tool = _ScriptedCodingTool([
        {"commands": [
            {"failed_vp_id": "VP-009", "test_command": _UNUSABLE_COMMAND},
        ]},
        {"commands": [
            {"failed_vp_id": "VP-009", "test_command": _UNUSABLE_COMMAND},
        ]},
        {"commands": [
            {"failed_vp_id": "VP-009", "test_command": _UNUSABLE_COMMAND},
        ]},
    ])
    gen = _generator(tmp_path, coding_tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=1)

    assert coding_tool.fill_prompt_count == (
        rg.RepairTaskGenerator.MAX_TEST_COMMAND_FILL_ATTEMPTS
    )
    assert not contents[0].get("test_command")


def test_a_row_the_model_still_cannot_rescue_is_still_emitted(
    tmp_path, monkeypatch,
):
    """The fill loop is an improvement, never a precondition.

    Exhausting it must leave the repair dispatchable: the row falls back
    to a label and the executor's completion gate does the rest. This is
    the assertion that the change did not reintroduce the refusal under
    a different name.
    """
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    coding_tool = _ScriptedCodingTool([
        {"commands": []},
        {"commands": []},
    ])
    gen = _generator(tmp_path, coding_tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=1)

    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=[_failed_vp()],
        vp_test_commands={},
        plan_path=str(gen.plan_dir / "verification_plan.json"),
    )
    tasks = assembler.assemble(contents)

    assert len(tasks) == 1, "an unrescuable row is still a real repair"
    # This fixture's point is ``api_test``, and that is decidable by
    # replaying its own request + assertions — so the assembler reaches
    # for the replay command rather than settling for a label. The row
    # is unverifiable only in the sense that the fill loop found nothing
    # extra to add; the plan itself supplied an objective criterion.
    assert "verification_api_runner" in tasks[0]["test_command"]
    assert assembler.test_command_sources["repair-r1-01"] == "api_replay"
    assert assembler.unverifiable_no_test_command == 0


def test_a_point_with_no_replayable_basis_is_emitted_even_when_uncued(
    tmp_path, monkeypatch,
):
    """The fill loop is an improvement, never a precondition.

    Exhausting it must leave the repair dispatchable: the row falls back
    to a label and the executor's completion gate does the rest. This is
    the assertion that the change did not reintroduce the refusal under
    a different name.

    The method here is unrecognised, so there is no replay to fall back
    on — which is what makes ``missing`` the honest label. ``api_test``
    is covered by the test above.
    """
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")

    coding_tool = _ScriptedCodingTool([
        {"commands": [
            {"failed_vp_id": "VP-009", "test_command": _UNUSABLE_COMMAND},
        ]},
        {"commands": [
            {"failed_vp_id": "VP-009", "test_command": _UNUSABLE_COMMAND},
        ]},
    ])
    gen = _generator(tmp_path, coding_tool)
    # Written after the generator so it is the plan on disk: an
    # unrecognised method, which no derivation path can serve.
    plan_path = _write_plan(gen.plan_dir, [{
        "id": "VP-009",
        "verification_method": "unrecognised_method",
    }])

    contents = gen.generate_repair_contents([_failed_vp()], round_number=1)

    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=[_failed_vp()],
        vp_test_commands={},
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble(contents)

    assert len(tasks) == 1, "an unrescuable row is still a real repair"
    assert tasks[0]["test_command"] == ""
    # Not the audit-style label: "we could not find one" and "this class
    # needs none" are different claims, and merging them is the mistake
    # this whole split exists to avoid.
    assert assembler.test_command_sources["repair-r1-01"] == "missing"
    assert assembler.unverifiable_no_test_command == 1


# ---------------------------------------------------------------------------
# Path B — accept by category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["code_review", "ui_validation"])
def test_an_artifact_deliverable_is_emitted_and_labelled_audit_style(
    tmp_path, method,
):
    """Path B: emit under the category label instead of discarding.

    ``code_review`` is judged from a ``citations.json`` and
    ``ui_validation`` from a ``checkpoints.json`` — both are findings a
    person reads, and neither has a shell command that reproduces it.
    That is the class :data:`AUDIT_STYLE_VERIFICATION_METHODS` names.
    """
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = _write_plan(plan_dir, [{
        "id": "VP-004",
        "verification_method": method,
    }])
    vp = {
        "id": "VP-004",
        "title": "审查 remote 模块的并发安全",
        "actual_result": "citations 指向不存在的行",
        "evidence": "citations.json",
    }

    assembler = RepairTaskAssembler(
        round_number=3,
        failed_vps=[vp],
        vp_test_commands={},
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble([{
        "failed_vp_id": "VP-004",
        "title": "修复 VP-004",
        "description": "d",
        "files_to_modify": ["src/remote.rs"],
    }])

    assert len(tasks) == 1, (
        "a findings-document deliverable is still a repair that must run"
    )
    assert tasks[0]["test_command"] == ""
    assert assembler.test_command_sources["repair-r3-01"] == "audit_style"
    assert assembler.unverifiable_no_test_command == 1
    assert assembler.dropped_empty_files_to_modify == 0


def test_the_two_absences_are_not_conflated(tmp_path):
    """``audit_style`` and ``missing`` must stay distinguishable.

    This is the same split ``backend/task.py`` makes between
    ``NO_FILE_CHANGES_SENTINEL`` and ``UNKNOWN_MODIFICATIONS_SENTINEL``,
    for the same reason: merging "this needs no check" with "nobody
    found one" is what makes the weaker case invisible.
    """
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = _write_plan(plan_dir, [
        {"id": "VP-004", "verification_method": "code_review"},
        {"id": "VP-005", "verification_method": "perf_benchmark_v2"},
    ])
    vps = [
        {"id": "VP-004", "title": "a", "actual_result": "x"},
        {"id": "VP-005", "title": "b", "actual_result": "y"},
    ]

    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=vps,
        vp_test_commands={},
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble([
        {"failed_vp_id": "VP-004", "title": "a", "description": "d",
         "files_to_modify": ["src/a.rs"]},
        {"failed_vp_id": "VP-005", "title": "b", "description": "d",
         "files_to_modify": ["src/b.rs"]},
    ])

    assert len(tasks) == 2
    assert assembler.test_command_sources["repair-r1-01"] == "audit_style"
    # An unrecognised method is "unknown", never "needs no command" — the
    # whitelist is positive on purpose.
    assert assembler.test_command_sources["repair-r1-02"] == "missing"
    assert len(set(assembler.test_command_sources.values())) == 2


def test_an_api_test_point_still_gets_its_replay_command(tmp_path):
    """The replay path must not regress into the empty-command branch.

    ``api_test`` is decidable by replaying the point's own request and
    assertions, which is a strictly better outcome than either label. It
    is also the case whose method lives in the plan rather than in the
    report-side entry, so it is exercised end to end here: reading the
    method off the failed-VP dict instead returns "" for every VP and
    silently files ``api_test`` under "needs no command".
    """
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = _write_plan(plan_dir, [_API_VP])

    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=[_failed_vp()],
        vp_test_commands={},
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble([{
        "failed_vp_id": "VP-009",
        "title": "t",
        "description": "d",
        "files_to_modify": ["app/signals.py"],
    }])

    assert tasks[0]["test_command"]
    assert "verification_api_runner" in tasks[0]["test_command"]
    assert assembler.test_command_sources["repair-r1-01"] == "api_replay"
    assert assembler.unverifiable_no_test_command == 0


# ---------------------------------------------------------------------------
# The downstream property path B depends on
# ---------------------------------------------------------------------------


class _StubCodingTool:
    def __init__(self, response: str):
        self._response = response
        self.calls: List[str] = []

    def query(self, prompt, system_instruction="", **kwargs):
        self.calls.append(prompt)
        return self._response


def _stub_agent(coding_tool) -> Any:
    """The smallest object ``_audit_task_second_pass`` will accept.

    The unbound method is exercised directly rather than through
    ``AutonomousAgent.__init__`` for the same reason the audit
    classifier is a module-level function: this is about the decision
    the method makes, not about agent construction.
    """
    from agent import AutonomousAgent

    class _Stub:
        logger = None
        project_dir = None
        _looks_like_audit_task = AutonomousAgent._looks_like_audit_task
        _audit_task_second_pass = AutonomousAgent._audit_task_second_pass
        # Read off the class as well: the second pass passes it to
        # ``coding_tool.query``, so omitting it raises before the call
        # and the "did the review run?" assertion would read as a silent
        # skip rather than as a broken stub.
        _AUDIT_SECOND_PASS_SYSTEM_INSTRUCTION = (
            AutonomousAgent._AUDIT_SECOND_PASS_SYSTEM_INSTRUCTION
        )

        def __init__(self):
            self.coding_tool = coding_tool

    return _Stub()


def _subtask(test_command: str):
    from task import SubTask

    return SubTask(
        id="repair-r1-01",
        title="修复 VP-009",
        description="让窗口语义成为契约内的真实行为",
        test_command=test_command,
    )


def test_the_second_pass_gates_a_task_with_no_runnable_command():
    """The behaviour that makes emitting an empty command acceptable.

    ``_cross_verify_test_result`` returns the sub-agent's own claim when
    there is no command to run (``test_cross_verify_unverified``). If
    nothing further checked that claim, path B would be exactly the
    single-signal completion the dual-criterion rule forbids.

    It is not: ``_audit_task_second_pass`` computes ``is_audit`` as
    ``... or not has_runnable_command``, so a command-less task is sent
    to an independent adversarial review. This pins both halves — that
    the review *runs*, and that its verdict can veto completion.
    """
    task = _subtask("")
    coding_tool = _StubCodingTool("AUDIT_VERDICT: FAILED\nREASON: no evidence")
    agent = _stub_agent(coding_tool)

    passed, reason = agent._audit_task_second_pass(task, "I fixed it.")

    assert coding_tool.calls, (
        "a task with no runnable command must reach the adversarial "
        "review; returning early would leave the verdict resting on the "
        "sub-agent's word alone"
    )
    assert passed is False
    assert reason.startswith("audit_second_pass_failed")


def test_the_second_pass_lets_a_sustained_answer_through():
    """The other half: a command-less task is not unwinnable.

    The review is a second signal, not a veto — so an answer that
    satisfies the spec still completes. Emitting these rows must not
    turn into a repair loop that can never drain.
    """
    task = _subtask("")
    coding_tool = _StubCodingTool(
        "Evidence: ran the suite, output pasted below.\nAUDIT_VERDICT: PASSED"
    )
    agent = _stub_agent(coding_tool)

    passed, reason = agent._audit_task_second_pass(task, "I fixed it.")

    assert passed is True
    assert reason == ""


def test_a_task_with_a_runnable_command_skips_the_second_pass():
    """The control: the second pass is not unconditional.

    Without this the two assertions above would still hold if the method
    simply reviewed everything, and would prove nothing about what an
    empty command changes.
    """
    task = _subtask("pytest -q tests/test_signals_window.py")
    coding_tool = _StubCodingTool("AUDIT_VERDICT: FAILED\nREASON: n/a")
    agent = _stub_agent(coding_tool)

    passed, reason = agent._audit_task_second_pass(task, "I fixed it.")

    assert coding_tool.calls == []
    assert (passed, reason) == (True, "")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_every_source_the_assembler_emits_is_in_its_declared_vocabulary(
    tmp_path,
):
    """The label is the contract an operator greps, so it must be closed.

    ``test_command_sources`` is what tells "this needs no command" from
    "nobody found one". A source string outside the declared tuple would
    make that tally silently incomplete.
    """
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = _write_plan(plan_dir, [
        {"id": "VP-001", "verification_method": "api_test"},
        {"id": "VP-002", "verification_method": "code_review"},
        {"id": "VP-003", "verification_method": "mystery_method"},
    ])
    vps = [
        {"id": "VP-001", "title": "a", "actual_result": "x"},
        {"id": "VP-002", "title": "b", "actual_result": "y"},
        {"id": "VP-003", "title": "c", "actual_result": "z"},
    ]
    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=vps,
        vp_test_commands={"VP-003": "pytest -q tests/test_v3.py"},
        plan_path=str(plan_path),
    )
    assembler.assemble([
        {"failed_vp_id": "VP-001", "title": "a", "description": "d",
         "files_to_modify": ["src/a.rs"]},
        {"failed_vp_id": "VP-002", "title": "b", "description": "d",
         "files_to_modify": ["src/b.rs"]},
        {"failed_vp_id": "VP-003", "title": "c", "description": "d",
         "files_to_modify": ["src/c.rs"]},
    ])

    declared = set(RepairTaskAssembler.ASSEMBLER_TEST_COMMAND_SOURCES)
    assert set(assembler.test_command_sources.values()) <= declared
    # The VP's own command still wins over every derived label.
    assert assembler.test_command_sources["repair-r1-03"] == "vp"


def test_the_empty_scope_gate_is_untouched(tmp_path):
    """The sibling gate keeps refusing; only ``test_command`` was relaxed.

    Scope and command are separate decisions, and relaxing one must not
    have loosened the other.
    """
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = _write_plan(plan_dir, [
        {"id": "VP-001", "verification_method": "code_review"},
    ])
    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=[{"id": "VP-001", "title": "a", "actual_result": "x"}],
        vp_test_commands={},
        plan_path=str(plan_path),
    )
    tasks = assembler.assemble([{
        "failed_vp_id": "VP-001", "title": "a", "description": "d",
    }])

    assert tasks == []
    assert assembler.dropped_empty_files_to_modify == 1
