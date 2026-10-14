"""The acceptance-command gate applies to a *first-pass* command too.

Why this file exists
--------------------
:meth:`RepairTaskGenerator.generate_repair_contents` gets its rows from
one content call, and a model that volunteers a ``test_command`` on that
call has it carried through verbatim by
:func:`_parse_repair_contents_response`. The bounded fill loop then skips
those rows — it only re-asks for rows that have *no* command — so the
two checks every model-authored command is supposed to face
(:func:`test_command_quality.inspect_command`, then the RED invariant in
:meth:`_passes_falsifiability`) never ran on it. The assembler's
:meth:`RepairTaskAssembler._resolve_test_command` returns the declared
string as-is, so a command that is structurally broken, or one that
already exits 0 against the unrepaired tree, reaches the executor as a
completion criterion.

The re-asked half of the same contract was already covered in
``tests/unit/test_repair_task_test_command.py``. What was missing is the
other half — the claim the assembler documents, that a declared command
"survived the quality and falsifiability checks, either on the first
content call or in the bounded fill loop". These tests hold the first
clause of that claim.

The three properties, in the order they are load-bearing:

* the RED invariant on a first-pass command — the shape is legal, only
  the outcome is vacuous, and that is the one ``inspect_command``
  cannot see;
* the structural gate on a first-pass command;
* a first-pass command that *is* good still ships as ``llm``, without
  costing an extra re-ask — the gate must not turn every round into a
  fill round.

None of these run a heavy command: the fixtures are one-line
interpreters that return a fixed exit status.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repair_generator as rg  # noqa: E402
from repair_generator import (  # noqa: E402
    RepairTaskAssembler,
    RepairTaskGenerator,
)


#: Structurally broken: the trailing ``echo`` makes the chain's exit
#: status ``echo``'s, whatever the tests did.
STRUCTURALLY_BROKEN = "pytest -q tests/test_x.py; echo done"

#: Legal shape, already true. ``inspect_command`` accepts it; only running
#: it against the unrepaired tree reveals that it certifies nothing.
VACUOUS = 'python3 -c "import sys; sys.exit(0)"'

#: Legal shape, and red. The counterweight that proves the probe really
#: executes: if the interpreter could not start, this would exit 127 and
#: be rejected too, and the test below would fail rather than pass for the
#: wrong reason.
FALSIFIABLE = 'python3 -c "import sys; sys.exit(1)"'


def _is_fill_prompt(prompt: str) -> bool:
    """Identify the fill loop's prompt by its own subject line.

    Dispatching on prompt content rather than on call index keeps the
    tests readable about *which* call is which, and makes a loop that
    stops asking fail loudly instead of quietly passing on an index
    shift.
    """
    return "补验收命令" in prompt


class _ScriptedTool:
    """``coding_tool`` double: one content reply, then scripted fills."""

    def __init__(self, content_response: Dict[str, Any],
                 fill_responses: List[Dict[str, Any]]):
        self.prompts: List[str] = []
        self._content_response = content_response
        self._fill_responses = list(fill_responses)

    def query_json(self, prompt: str, system_instruction: str = "") -> Any:
        self.prompts.append(prompt)
        if _is_fill_prompt(prompt):
            # Exhausting the script is itself an answer the loop must
            # survive: it retries, then gives up and lets the assembler
            # label the row.
            return self._fill_responses.pop(0) if self._fill_responses \
                else {"commands": []}
        return self._content_response

    @property
    def fill_prompt_count(self) -> int:
        return sum(1 for p in self.prompts if _is_fill_prompt(p))


def _content(test_command: str) -> Dict[str, Any]:
    """A content reply that volunteers ``test_command`` on the first call.

    The content prompt disclaims it (rule 5: the acceptance command is
    attached by the system), so this shape is a model going off-script —
    which is exactly the shape that used to reach the assembler
    unchecked, and the reason the parser carries the field at all.
    """
    return {
        "tasks": [{
            "failed_vp_id": "VP-013",
            "title": "修复 VP-013",
            "description": "让失败点真实成立",
            "acceptance_criteria": "断言失败消失",
            "files_to_modify": ["app/core.py"],
            "test_command": test_command,
        }],
    }


def _failed_vp() -> Dict[str, Any]:
    return {
        "id": "VP-013",
        "title": "输入校验失败",
        "priority": "medium",
        "actual_result": "AssertionError: expected 1 == 2",
        "evidence": "assertion failure",
    }


def _generator(tmp_path: Path, tool: Any) -> RepairTaskGenerator:
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": []}', encoding="utf-8",
    )
    return RepairTaskGenerator(
        coding_tool=tool, plan_dir=plan_dir, project_dir=tmp_path,
    )


def _assemble(contents: List[Dict[str, Any]]):
    """``(tasks, sources)`` for a plan carrying no commands of its own.

    ``vp_test_commands={}`` and an absent ``plan_path`` remove both
    derived fallbacks, so ``sources`` reports exactly what the model
    supplied — or that nothing did. The source lives on the assembler
    rather than on the task dict, because there is no
    ``test_command_source`` column in the task store.
    """
    assembler = RepairTaskAssembler(
        round_number=2,
        failed_vps=[_failed_vp()],
        vp_test_commands={},
        plan_path="",
        seq_base=1,
    )
    tasks = assembler.assemble(contents)
    return tasks, assembler.test_command_sources


def test_a_first_pass_command_that_is_already_green_is_not_shipped(
    tmp_path,
):
    """The RED invariant, on the first content call.

    This is the load-bearing one. The shape is legal, so
    ``inspect_command`` waves it through; only the pre-repair probe
    rejects it. Shipped, it would make the task's completion verdict
    vacuous — the empty-diff failure mode the probe's own docstring
    names — because a tree nobody touched already satisfies it.
    """
    tool = _ScriptedTool(_content(VACUOUS), [
        {"commands": [{"failed_vp_id": "VP-013", "test_command": VACUOUS}]},
    ])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert contents[0].get("test_command") != VACUOUS, (
        "a command that already exits 0 against the unrepaired tree was "
        "accepted as the task's acceptance criterion"
    )
    assert not contents[0].get("test_command"), (
        "a rejected command must be left absent, so the row is re-asked "
        "rather than inherited"
    )
    assert not contents[0].get("test_command_source"), (
        "the source label has to go with the command it described"
    )

    tasks, sources = _assemble(contents)
    assert len(tasks) == 1, "the repair itself is still emitted — only its command goes"
    assert tasks[0]["test_command"] == "", (
        "the assembled task shipped the vacuous first-pass command"
    )
    assert sources.get(tasks[0]["id"]) == "missing", (
        "with no command and no derived fallback the honest label is "
        "'missing', not 'llm'"
    )


def test_a_structurally_broken_first_pass_command_is_not_shipped(tmp_path):
    """The structural gate, on the first content call.

    A chain whose exit status is the trailing ``echo``'s reports success
    whatever the repair did — the false-failure / false-success pair
    :func:`inspect_command` exists to keep out of the completion gate.
    """
    tool = _ScriptedTool(_content(STRUCTURALLY_BROKEN), [
        {"commands": [{"failed_vp_id": "VP-013",
                       "test_command": STRUCTURALLY_BROKEN}]},
    ])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert contents[0].get("test_command") != STRUCTURALLY_BROKEN, (
        "a structurally broken first-pass command was accepted"
    )
    assert not contents[0].get("test_command"), (
        "a rejected command must be left absent so the row is re-asked"
    )

    tasks, sources = _assemble(contents)
    assert len(tasks) == 1
    assert tasks[0]["test_command"] == ""
    assert sources.get(tasks[0]["id"]) == "missing"


def test_a_sound_first_pass_command_ships_without_a_second_call(tmp_path):
    """The gate must not cost a good command its round.

    A first-pass command that survives both checks is the best available
    answer — the fill loop re-asks precisely because the derived paths
    have nothing. Turning every row into a pending one would add an LLM
    call to every repair round to re-derive what was already right.
    """
    tool = _ScriptedTool(_content(FALSIFIABLE), [])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert tool.fill_prompt_count == 0, (
        "a sound first-pass command must not send the row through the "
        "fill loop"
    )
    assert contents[0]["test_command"] == FALSIFIABLE
    assert contents[0]["test_command_source"] == "", (
        "the source label is the assembler's to assign; a first-pass "
        "command is the 'llm' source by default there"
    )

    tasks, sources = _assemble(contents)
    assert len(tasks) == 1
    assert tasks[0]["test_command"] == FALSIFIABLE
    assert sources.get(tasks[0]["id"]) == "llm", (
        "a command the model authored and that passed the gate keeps the "
        "'llm' label, not 'llm_retry'"
    )


def test_the_probe_covers_the_first_pass_command_too(tmp_path, monkeypatch):
    """The first-pass command goes through the probe, and is memoised.

    Pins the mechanism rather than the verdict: the same chokepoint runs
    for a first-pass command and for a re-asked one, so the per-command
    cache that keeps a repair round from spawning a subprocess per task
    covers both.

    A fresh generator is used so the count starts from an empty cache —
    the memoisation being exercised here would otherwise hide the call
    this test exists to require.
    """
    probed: List[str] = []
    real = rg.check_falsifiable

    def counting_check(command, **kwargs):
        probed.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(rg, "check_falsifiable", counting_check)

    tool = _ScriptedTool(_content(VACUOUS), [])
    gen = _generator(tmp_path, tool)

    # The content call offers VACUOUS, it is rejected, the row is
    # re-asked, and the fill loop's script is exhausted so it answers
    # with silence twice — two more chances to probe the same string.
    gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert probed.count(VACUOUS) == 1, (
        f"the probe ran {probed.count(VACUOUS)} times for one command "
        f"(full call log: {probed})"
    )


def test_a_rejected_first_pass_row_is_indistinguishable_from_a_command_less_one(
    tmp_path,
):
    """The first-pass verdict must be the *same* verdict, not a parallel one.

    :meth:`_fill_missing_test_commands` clears a row only by asking
    :meth:`_resolve_repair_test_command` about it, which is the one place
    that runs ``inspect_command`` and then the probe. A row the first pass
    had to re-ask therefore lands in exactly the state a row that arrived
    command-less lands in, and the bounded loop cannot tell the two
    apart — which is what keeps this a reuse of the existing contract
    rather than a second implementation of it.
    """
    tool = _ScriptedTool(_content(VACUOUS), [])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)
    row = contents[0]

    assert not row.get("test_command"), (
        "the rejected command is still on the row, so the pending filter "
        "would skip it and the loop would never re-ask"
    )
    assert not row.get("test_command_source"), (
        "the source label has to go with the command it described, or a "
        "later reader cannot tell an unvetted command from a vetted one"
    )
    # What the resolver says about the row as it now stands — the same
    # call the pending filter makes, and the call that cleared it.
    assert gen._resolve_repair_test_command(row, [])[1] == "missing"
