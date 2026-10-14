"""The pre-repair falsifiability probe, on the live repair path.

Why this file shrank to three tests
-----------------------------------
It used to pin the whole ``test_command`` resolution order — LLM-authored,
derived from the named VP, derived from a single failed point, missing —
driven through ``_generate_repair_tasks``. That was the requirement-driven
chain's own post-processing step, and the chain is gone. On the live path
the resolution order belongs to :class:`RepairTaskAssembler`, which owns
it in ``tests/test_repair_task_assembler.py`` and
``tests/unit/test_repair_no_test_command_paths.py``.

What belongs to :class:`RepairTaskGenerator` and is pinned here is the
probe that decides whether a command is worth shipping at all.
:meth:`_passes_falsifiability` runs a candidate acceptance command
against the current, *pre-repair* tree and rejects one that already exits
0. ``inspect_command`` cannot see that — the shape is legal, only the
outcome is vacuous — and a command that cannot tell a correct fix from no
change at all makes the task's completion verdict meaningless. The live
path reaches the probe through
:meth:`RepairTaskGenerator._fill_missing_test_commands`, which runs it on
every command it re-asks for.

Three properties, each easy to lose and none of them observable from the
assembler's tests: the probe is memoised per command (repair rounds emit
many tasks sharing one command shape, and the probe starts a
subprocess); a vacuous command the model offers *is* rejected on the live
path; and the probe has an escape hatch for a project whose toolchain
cannot execute the command at all.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repair_generator as rg  # noqa: E402
from repair_generator import RepairTaskGenerator  # noqa: E402


#: Well-formed, and already true — the shape the probe exists to catch.
#: ``inspect_command`` accepts it; only running it reveals the problem.
VACUOUS = 'python3 -c "import sys; sys.exit(0)"'


def _is_fill_prompt(prompt: str) -> bool:
    """Identify the fill loop's prompt by its own subject line.

    Dispatching on prompt content rather than on call index keeps the
    test readable about *which* call is which, and makes it fail loudly
    if the loop stops asking rather than quietly passing on an index
    shift.
    """
    return "补验收命令" in prompt


class _ScriptedTool:
    """``coding_tool`` double: a content reply, then scripted fill replies."""

    def __init__(self, fill_responses: List[Dict[str, Any]]):
        self.prompts: List[str] = []
        self._fill_responses = list(fill_responses)

    def query_json(self, prompt: str, system_instruction: str = "") -> Any:
        self.prompts.append(prompt)
        if _is_fill_prompt(prompt):
            # Exhausting the script is itself an answer the loop must
            # survive: it retries, then gives up and labels the row.
            return self._fill_responses.pop(0) if self._fill_responses \
                else {"commands": []}
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
    def fill_prompt_count(self) -> int:
        return sum(1 for p in self.prompts if _is_fill_prompt(p))


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


def _evidence(vp_id: str = "VP-013", test_command: str = "cargo test --test x"):
    return [{
        "vp_id": vp_id,
        "actual_result": "assert 0 == 1",
        "evidence": "tests/test_x.py::test_y",
        "test_command": test_command,
    }]


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def test_the_probe_is_memoised_per_command(tmp_path, monkeypatch):
    """Repair rounds emit many tasks sharing one command shape.

    The probe starts a subprocess, so re-running it per task would add
    minutes to every round for no information.
    """
    calls = []
    real = rg.check_falsifiable

    def counting_check(command, **kwargs):
        calls.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(rg, "check_falsifiable", counting_check)

    gen = _generator(tmp_path, _ScriptedTool([]))
    for _ in range(3):
        gen._resolve_repair_test_command(
            {"test_command": "cargo test --test signal_classification"},
            _evidence(),
        )
    assert len(calls) == 1, f"probe ran {len(calls)} times, expected 1"


def test_a_vacuous_command_is_not_adopted_on_the_live_path(tmp_path, monkeypatch):
    """The content call never asks for a command, so the fill loop is
    where a model-authored one enters — and the probe has to be able to
    refuse it there.

    Without this the loop would stamp a command that exits 0 before any
    repair ran, and the task would be judged done by a change that was
    never made.
    """
    tool = _ScriptedTool([{"commands": [{"failed_vp_id": "VP-013",
                                         "test_command": VACUOUS}]}])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert tool.fill_prompt_count >= 1, (
        "a command-less row must be re-asked before it can be judged"
    )
    assert contents[0].get("test_command") != VACUOUS, (
        "a command that already exits 0 cannot certify the repair"
    )
    assert not contents[0].get("test_command"), (
        "an unusable command is left absent so the assembler labels the "
        "row by category rather than inheriting a vacuous one"
    )


def test_the_probe_can_be_disabled(tmp_path, monkeypatch):
    """An operator whose project cannot execute needs a way out.

    Without this, a missing toolchain would make every honest
    command look vacuous and silently downgrade the whole round to
    unverifiable tasks.
    """
    monkeypatch.setenv(rg.RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")
    tool = _ScriptedTool([{"commands": [{"failed_vp_id": "VP-013",
                                         "test_command": VACUOUS}]}])
    gen = _generator(tmp_path, tool)

    contents = gen.generate_repair_contents([_failed_vp()], round_number=2)

    assert contents[0]["test_command"] == VACUOUS
    assert contents[0]["test_command_source"] == "llm_retry"
