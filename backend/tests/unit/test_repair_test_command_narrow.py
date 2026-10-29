"""The repair task's criterion must select the cases that failed — not the gate.

Why this file exists
--------------------
A repair task used to inherit the failing verification point's own
command. For a ``full_ci`` point that command is the whole-repository
gate: one repair round then costs 45–70 minutes of wall clock, and a fix
needs 5–10 rounds. The criterion has to name **the cases that failed
this time** — a narrow command is a 2-minute round, a wide one is an
hour.

What the framework can and cannot do
------------------------------------
It cannot build the narrow command itself: it does not know the other
side's runner or venv. So it does exactly two things — it recognises
the failing case ids in the round's run output, and it refuses a
candidate command that does not select them. A refused command is
dropped, which routes the row into the existing bounded re-ask loop
(``_fill_missing_test_commands``), where the model — which does know the
runner — writes the narrow command.

Why a refused command is better than none
------------------------------------------
A structurally-broken command is worse than no command: it guarantees a
FALSE failure every time, whereas no command degrades to the audit
second pass. A *wide* command is the same shape of defect with a
different cause: it guarantees a false failure on every round that
issues it (the gate is red for reasons that have nothing to do with the
fix), at 45–70 minutes a round. So it is refused the same way.

What is pinned here
-------------------
* the two pure helpers — the case ids read off a run output, and whether
  a command selects them;
* that the case ids are read off **both** shapes a run can end in: the
  short summary, and the pytest-timeout line of a run that was killed
  (which prints no summary at all);
* that the resolver refuses a command that does not select them, and is
  **unchanged** when the output names no case at all;
* that the same holds on the live path (through
  ``generate_repair_contents``), not only on the helper;
* that the root-cause implementation task does not inherit the wide
  command either;
* that both prompts carry the rule and the ids.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repair_generator as rg  # noqa: E402
import test_command_quality as tcq  # noqa: E402
from repair_generator import RepairTaskGenerator  # noqa: E402
from root_cause_task import (  # noqa: E402
    ROOT_CAUSE_DELIVERABLE,
    build_implementation_task,
)


#: The whole-repository gate — the command a ``full_ci`` point carries.
#: Shipping it as a repair criterion is the defect this file pins shut.
WIDE_GATE = "bash scripts/run_tests.sh"

#: The narrow command that actually selects the failed case.
NARROW = "pytest backend/tests/a.py::test_b -q"

#: A run output in pytest's own shape: the verbose progress lines put
#: ``FAILED`` at the END of the line (not a summary), and the short
#: summary puts it at the start. Only the second kind names a case.
RUN_OUTPUT = """\
============================= test session starts ==============================
collected 3 items

backend/tests/a.py::test_b FAILED
backend/tests/a.py::test_c PASSED
backend/tests/a.py::test_d ERROR

=================================== FAILURES ===================================
________________________________ test_b __________________________________
    def test_b():
>       assert 1 == 2
E       assert 1 == 2

=========================== short test summary info ============================
FAILED backend/tests/a.py::test_b - AssertionError: assert 1 == 2
FAILED backend/tests/a.py::test_b - AssertionError: assert 1 == 2
ERROR backend/tests/a.py::test_d - RuntimeError: boom
=========================== 2 failed, 1 passed in 3.2s ==========================
"""


#: A run that pytest-timeout killed. The case it killed is named on a
#: line of its own, wrapped in a rule of ``+`` — and because the run was
#: killed, pytest never reaches its short summary, so that line is the
#: only place the case is named.
#:
#: The shape is the one the VP-014 round of the service-spawn repro
#: printed: the node id of the case that hung, then the rule, then the
#: rule again.
TIMEOUT_CASE_LINE = (
    "backend/tests/integration/test_service_spawn_repro.py"
    "::test_three_consecutive_spawns_all_ready "
    "+++++++++++++++++++++++++++++++++++"          # 35
    " Timeout "
    "++++++++++++++++++++++++++++++++++++"         # 36
)

#: The same rule with no node id on it. pytest-timeout prints that one
#: too, at the end of the killed run: it is decoration, not a case, and
#: taking it for one would name a case that does not exist.
BARE_TIMEOUT_RULE = (
    "+++++++++++++++++++++++++++++++++++"          # 35
    " Timeout "
    "++++++++++++++++++++++++++++++++++++"         # 36
)

#: A progress line of a case that passed. It carries a node id, so a
#: reader that only looked for ``::`` would take it for a failure.
PASSED_LINE = (
    "backend/tests/integration/test_service_spawn_end_to_end.py"
    "::test_port_released_after_stop PASSED [ 56%]"
)

#: A killed run, in the shape that run actually printed: the progress
#: lines that passed, the case that hung, the captured output, the bare
#: rule, and the exit code.
TIMEOUT_RUN_OUTPUT = "\n".join([
    PASSED_LINE,
    TIMEOUT_CASE_LINE,
    "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Captured stdout ~~~~~~~~~~~~"
    "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
    "profile=service-manager target=pdt runs=3 ready=3 failed=0",
    BARE_TIMEOUT_RULE,
    "GATE_EXIT=1",
    "",
])


def _helpers():
    """The two narrowing helpers, looked up at call time.

    Read off the module rather than imported by name on purpose: this
    file then *collects* on a tree that does not have them yet, and the
    missing attribute fails the one test that needs it — instead of the
    whole module turning into a single collection error that says
    nothing about which behaviour is missing.
    """
    return tcq.failing_case_ids, tcq.covers_failing_cases


class _ScriptedTool:
    """``coding_tool`` double: a content reply, then scripted fill replies.

    Every prompt is kept, so a test can assert on what the model was
    actually asked — the narrowing rule and the failing case ids have to
    reach the prompt, not just the code.
    """

    def __init__(
        self,
        content_response: Any = None,
        fill_responses: List[Dict[str, Any]] = (),
    ):
        self.prompts: List[str] = []
        self._content_response = content_response
        self._fill_responses = list(fill_responses)

    def query_json(self, prompt: str, system_instruction: str = "") -> Any:
        self.prompts.append(prompt)
        if "补验收命令" in prompt:
            return self._fill_responses.pop(0) if self._fill_responses \
                else {"commands": []}
        if self._content_response is not None:
            return self._content_response
        return {
            "tasks": [{
                "failed_vp_id": "VP-013",
                "title": "修复 VP-013",
                "description": "让失败点真实成立",
                "acceptance_criteria": "断言失败消失",
                "files_to_modify": ["app/core.py"],
            }],
        }

    @property
    def fill_prompts(self) -> List[str]:
        return [p for p in self.prompts if "补验收命令" in p]


def _generator(tmp_path: Path, tool: Any = None) -> RepairTaskGenerator:
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": []}', encoding="utf-8",
    )
    return RepairTaskGenerator(
        coding_tool=tool or _ScriptedTool(), plan_dir=plan_dir,
        project_dir=tmp_path,
    )


def _failed_vp() -> Dict[str, Any]:
    """A failed VP whose own command is the whole-repository gate.

    The evidence is in the shape the runner writes it: a ``stdout:``
    label, then the captured output on its own lines — which is where
    pytest's short summary (and with it the node ids) lives.
    """
    return {
        "id": "VP-013",
        "title": "登录校验失败",
        "priority": "medium",
        "actual_result": "AssertionError: assert 401 == 200",
        "evidence": "stdout:\nFAILED backend/tests/a.py::test_b - AssertionError\n",
        "test_command": WIDE_GATE,
    }


# ---------------------------------------------------------------------------
# The two pure helpers
# ---------------------------------------------------------------------------


def test_failing_case_ids_reads_the_pytest_summary():
    """The short summary names the case; the progress lines do not.

    Order is the order of appearance and a case reported twice is one
    case — the ids go into a prompt, so a duplicate would read as two
    different failures.
    """
    failing_case_ids, _ = _helpers()
    assert failing_case_ids(RUN_OUTPUT) == [
        "backend/tests/a.py::test_b",
        "backend/tests/a.py::test_d",
    ]


def test_failing_case_ids_on_nothing():
    """No output, no ids — and never an exception.

    The caller treats an empty list as "there is nothing to narrow
    towards", so this must be an ordinary answer, not a crash.
    """
    failing_case_ids, _ = _helpers()
    assert failing_case_ids("") == []
    assert failing_case_ids(None) == []
    assert failing_case_ids("everything passed\n") == []


def test_covers_failing_cases():
    """A command covers a case when it names the case or its file.

    The file is enough: a command that runs the whole failing file
    selects the failing case, and it is still a narrow command.
    """
    _, covers_failing_cases = _helpers()
    ids = ["backend/tests/a.py::test_b"]
    assert covers_failing_cases(WIDE_GATE, ids) is False
    assert covers_failing_cases("pytest backend/tests/a.py -q", ids) is True
    assert covers_failing_cases(NARROW, ids) is True
    # Nothing to narrow towards: a wide command is not "wide" here.
    assert covers_failing_cases(WIDE_GATE, []) is True


# ---------------------------------------------------------------------------
# The pytest-timeout marker
# ---------------------------------------------------------------------------
#
# A run killed at pytest-timeout's own deadline prints no short summary:
# the process is gone before pytest writes one. The case it killed is
# named on a line of its own, wrapped in a rule of ``+``. A reader that
# only knows the short summary reads that run as naming no case at all
# — and a run that names no case is a run with nothing to narrow
# towards, so the whole-repository gate survives every round. That is
# the shape the VP-014 round died on.


def test_failing_case_ids_reads_the_pytest_timeout_marker():
    """The killed case is named on the timeout line, and that is enough.

    No short summary, no ``FAILED`` marker, no ``ERROR`` marker — the
    node id is on the line pytest-timeout printed, and it is the only
    place it appears.
    """
    failing_case_ids, _ = _helpers()
    assert failing_case_ids(TIMEOUT_RUN_OUTPUT) == [
        "backend/tests/integration/test_service_spawn_repro.py"
        "::test_three_consecutive_spawns_all_ready",
    ]


def test_failing_case_ids_ignores_the_bare_timeout_rule():
    """The rule with no case on it is decoration, not a case.

    The same killed run prints this line too. Naming it would invent a
    case that does not exist, and the invented id would then be a
    target no command can select — so every command would be refused.
    """
    failing_case_ids, _ = _helpers()
    assert failing_case_ids(BARE_TIMEOUT_RULE) == []
    assert failing_case_ids(TIMEOUT_RUN_OUTPUT) == [
        "backend/tests/integration/test_service_spawn_repro.py"
        "::test_three_consecutive_spawns_all_ready",
    ]


def test_failing_case_ids_ignores_a_passed_progress_line():
    """A case that passed is not a case that failed.

    It carries a node id, so a reader that only looked for ``::`` would
    take it for a failure — and would then refuse every command that
    does not run a passing case. The sample below holds a passing line
    *and* the timeout line, so what is checked is the difference between
    the two, not the absence of any case at all.
    """
    failing_case_ids, _ = _helpers()
    assert failing_case_ids(PASSED_LINE) == []
    output = "\n".join([PASSED_LINE, TIMEOUT_CASE_LINE, ""])
    assert failing_case_ids(output) == [
        "backend/tests/integration/test_service_spawn_repro.py"
        "::test_three_consecutive_spawns_all_ready",
    ]


def test_failing_case_ids_merges_the_two_markers_in_one_order():
    """Both shapes land in one list, in the order the output printed them.

    A run can print a summary *and* a timeout line. The ids go into one
    prompt, so a duplicate would read as two different failures and a
    reshuffled order as two different rounds.
    """
    failing_case_ids, _ = _helpers()
    output = "\n".join([
        "backend/tests/a.py::test_x +++++++++ Timeout +++++++++",
        "=========================== short test summary info =======",
        "FAILED backend/tests/a.py::test_y - AssertionError",
        "FAILED backend/tests/a.py::test_x - Timeout",
        "",
    ])
    assert failing_case_ids(output) == [
        "backend/tests/a.py::test_x",
        "backend/tests/a.py::test_y",
    ]


def test_the_resolver_refuses_the_gate_after_a_timeout_kill(tmp_path, monkeypatch):
    """The killed round must not hand the gate on as the criterion.

    This is the live shape of the defect: the round died on
    ``test_three_consecutive_spawns_all_ready``, so the output names the
    case only on the timeout line. If that line yields no id, the round
    looks like one that named nothing, the gate is "not wide" against
    nothing, and the repair task ships the whole-repository gate as
    its criterion.
    """
    monkeypatch.setenv(
        RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1",
    )
    gen = _generator(tmp_path)
    evidence = [{
        "vp_id": "VP-014",
        "test_command": WIDE_GATE,
        "actual_result": "Timeout",
        "evidence": TIMEOUT_RUN_OUTPUT,
    }]
    command, source = gen._resolve_repair_test_command(
        {"vp_id": "VP-014", "test_command": WIDE_GATE}, evidence,
    )
    assert command != WIDE_GATE, (
        "the whole-repository gate must not be shipped as the criterion "
        "of a round that was killed on a named case"
    )
    assert (command, source) == ("", "missing")


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_the_resolver_refuses_a_command_that_misses_the_failed_cases(
    tmp_path, monkeypatch,
):
    """The whole-repository gate is not a criterion for one failed case.

    The evidence names the case that failed; a command that selects
    neither the case nor its file cannot distinguish "fixed" from
    "unrelated breakage elsewhere in the repo".
    """
    monkeypatch.setenv(
        RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1",
    )
    gen = _generator(tmp_path)
    evidence = [{
        "vp_id": "VP-013",
        "test_command": WIDE_GATE,
        "actual_result": "FAILED backend/tests/a.py::test_b - AssertionError",
        "evidence": "stdout:\nFAILED backend/tests/a.py::test_b\n",
    }]
    command, source = gen._resolve_repair_test_command(
        {"vp_id": "VP-013", "test_command": WIDE_GATE}, evidence,
    )
    assert command != WIDE_GATE, (
        "the whole-repository gate must not be shipped as the criterion"
    )
    assert (command, source) == ("", "missing")


def test_the_resolver_keeps_a_command_that_selects_the_failed_cases(
    tmp_path, monkeypatch,
):
    """The narrowing must not throw the good command away with the wide one."""
    monkeypatch.setenv(
        RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1",
    )
    gen = _generator(tmp_path)
    evidence = [{
        "vp_id": "VP-013",
        "test_command": NARROW,
        "actual_result": "FAILED backend/tests/a.py::test_b - AssertionError",
        "evidence": "stdout:\nFAILED backend/tests/a.py::test_b\n",
    }]
    command, source = gen._resolve_repair_test_command(
        {"vp_id": "VP-013", "test_command": NARROW}, evidence,
    )
    assert command == NARROW
    assert source == "llm"


def test_without_a_failing_case_id_nothing_changes(tmp_path, monkeypatch):
    """Output that names no case leaves the old behaviour alone.

    Not every failure prints a node id (a build error, a service that
    never came up). For those the framework has nothing to narrow
    towards, and refusing commands it cannot judge would take away a
    working criterion.
    """
    failing_case_ids, _ = _helpers()
    monkeypatch.setenv(
        RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1",
    )
    gen = _generator(tmp_path)
    evidence = [{
        "vp_id": "VP-013",
        "test_command": NARROW,
        "actual_result": "AssertionError: assert 1 == 2",
        "evidence": "the suite failed",
    }]
    assert failing_case_ids(evidence[0]["evidence"]) == []
    assert gen._resolve_repair_test_command(
        {"vp_id": "VP-013", "test_command": NARROW}, evidence,
    ) == (NARROW, "llm")


# ---------------------------------------------------------------------------
# The live path
# ---------------------------------------------------------------------------


def test_the_live_path_asks_for_a_narrow_command(
    tmp_path, monkeypatch,
):
    """From ``generate_repair_contents`` down, the gate is refused.

    The row arrives without a command (the content prompt never asks
    for one), so the resolver would fall back to the verification
    point's own command — the gate. With the narrowing in place that
    fallback is refused, the row is re-asked, and the prompt carries
    the failing case ids so the model can write the narrow one.
    """
    monkeypatch.setenv(
        RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1",
    )
    tool = _ScriptedTool(fill_responses=[
        {"commands": [{"failed_vp_id": "VP-013", "test_command": NARROW}]},
    ])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert tool.fill_prompts, (
        "the wide command must be refused, which routes the row into the "
        "existing re-ask loop"
    )
    fill_prompt = tool.fill_prompts[0]
    assert "backend/tests/a.py::test_b" in fill_prompt, (
        "the re-ask must carry the failing case ids"
    )
    assert contents[0].get("test_command") == NARROW


def test_the_round_run_output_reaches_the_generator(tmp_path, monkeypatch):
    """The orchestrator's wiring: the run output → the ids → the prompt.

    The path-based failed-VP records deliberately carry summaries and
    pointers instead of the run output (that is what keeps the prompt
    bounded), so the ids of the failed cases are not in them. The
    orchestrator attaches the output; without that attachment the ids
    never appear, the narrowing has nothing to narrow towards, and the
    gate survives every round.
    """
    from verification.orchestrator import _attach_round_run_output

    monkeypatch.setenv(
        RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1",
    )
    candidate = {
        "id": "VP-013",
        "title": "登录校验失败",
        "priority": "medium",
        "actual_result_summary": "AssertionError: assert 401 == 200",
        "evidence_summary": "stdout: (truncated)",
        "evidence_paths": ["/tmp/logs/vp-013.log"],
        "test_command": WIDE_GATE,
    }
    attached = _attach_round_run_output([candidate], {
        "VP-013": {
            "actual_result": "AssertionError: assert 401 == 200",
            "evidence": "stdout:\nFAILED backend/tests/a.py::test_b - x\n",
        },
    })
    assert attached == 1
    assert "backend/tests/a.py::test_b" in candidate["run_output"]

    tool = _ScriptedTool(fill_responses=[
        {"commands": [{"failed_vp_id": "VP-013", "test_command": NARROW}]},
    ])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([candidate], round_number=2)

    assert tool.fill_prompts, (
        "with the round's run output attached, the gate must be refused"
    )
    assert "backend/tests/a.py::test_b" in tool.fill_prompts[0]
    assert contents[0].get("test_command") == NARROW


# ---------------------------------------------------------------------------
# The root-cause implementation task
# ---------------------------------------------------------------------------


def test_the_implementation_task_does_not_inherit_the_wide_command():
    """The chain's implementation task inherits the point's command too.

    It used to take it verbatim, without even the structural check the
    ordinary repair path runs. A wide command there is the same 45–70
    minute round.
    """
    report = {
        "plan_id": "P-1",
        "vp_id": "VP-013",
        "round": 1,
        "paths": ["app/core.py"],
        "test_command": WIDE_GATE,
    }
    evidence = {
        "actual_result": "FAILED backend/tests/a.py::test_b - AssertionError",
        "evidence": "stdout:\nFAILED backend/tests/a.py::test_b\n",
    }
    task = build_implementation_task(report, evidence=evidence)
    assert task["test_command"] == "", (
        "the whole-repository gate must not be the implementation task's "
        "criterion"
    )


def test_the_implementation_task_keeps_a_narrow_command():
    """A command that selects the failed case is the point of the exercise."""
    report = {
        "plan_id": "P-1",
        "vp_id": "VP-013",
        "round": 1,
        "paths": ["app/core.py"],
        "test_command": NARROW,
    }
    evidence = {
        "actual_result": "FAILED backend/tests/a.py::test_b - AssertionError",
        "evidence": "stdout:\nFAILED backend/tests/a.py::test_b\n",
    }
    task = build_implementation_task(report, evidence=evidence)
    assert task["test_command"] == NARROW


def test_the_root_cause_deliverable_names_the_narrow_scope():
    """The diagnosis is asked for the criterion the repair will use."""
    assert "验收方式（只选中这次失败的用例）" in ROOT_CAUSE_DELIVERABLE


# ---------------------------------------------------------------------------
# The prompts
# ---------------------------------------------------------------------------


def test_the_repair_content_prompt_carries_the_rule_and_the_ids():
    """The rule reaches the prompt that writes the repair task.

    The model is told which cases failed and what a criterion may not
    be; a rule that lives only in the code cannot steer the model, and
    the model is what writes the command.
    """
    failing_case_ids, _ = _helpers()
    prompt = rg._build_repair_contents_prompt(
        [_failed_vp()], 2, Path("/tmp/plan"), Path("/tmp/project"),
    )
    assert "backend/tests/a.py::test_b" in prompt
    assert "不许" in prompt and "整仓门禁" in prompt
    assert failing_case_ids(_failed_vp()["evidence"]) == [
        "backend/tests/a.py::test_b",
    ]


def test_the_fill_prompt_carries_the_rule_and_the_ids():
    """Same rule on the re-ask — that is where the command comes from."""
    prompt = rg._build_test_command_fill_prompt(
        [_failed_vp()], {}, {}, attempt=1, max_attempts=2,
        plan_dir=Path("/tmp/plan"), project_dir=Path("/tmp/project"),
    )
    assert "backend/tests/a.py::test_b" in prompt
    assert "不许" in prompt and "整仓门禁" in prompt
