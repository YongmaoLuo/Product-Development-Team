"""Derived repair commands must pass the shape check.

*Which commands may leave this module without being judged?*

**The gate gap.** ``_resolve_repair_test_command`` ran
:func:`test_command_quality.inspect_command` on the model-authored
command and on nothing else. The two derived paths handed back whatever
string the plan author had written into ``verification_plan.json`` --
and an author is a person with a shell. A chain that ends in a
mid-chain ``grep``/``ls`` exits non-zero whenever that verb finds
nothing, so the repair fails *even when it worked*.
``RepairTaskAssembler._resolve_test_command`` took a declared command as
already vetted, so nothing downstream would have caught it.

The RED probe is deliberately **not** extended to these paths: it runs
the command, and the command belongs to a verification point that has
just failed, so the probe answers "fails as it should" while costing a
subprocess. :func:`inspect_command` executes nothing, and the
false-failure argument applies exactly as well to an author-declared
command.
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


class _CountingCodingTool:
    """Answers the content call; counts it so "was the model asked?" is a fact."""

    def __init__(self, tasks: List[Dict[str, Any]]):
        self._tasks = list(tasks)
        self.content_prompts: List[str] = []
        self.fill_prompts: List[str] = []

    def query_json(self, prompt: str, system_instruction: str = "") -> Any:
        if "补验收命令" in prompt:
            self.fill_prompts.append(prompt)
            return {"commands": []}
        self.content_prompts.append(prompt)
        return {"tasks": [dict(t) for t in self._tasks]}

    @property
    def content_call_count(self) -> int:
        return len(self.content_prompts)


class _ExplodingCodingTool:
    """Any call is a failure -- used to prove no call is made at all."""

    def query_json(self, prompt: str, system_instruction: str = "") -> Any:
        raise AssertionError("the model must not be asked on this round")


def _generator(
    tmp_path: Path,
    coding_tool: Any,
    plan_points: List[Dict[str, Any]] = (),
) -> RepairTaskGenerator:
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": list(plan_points)}),
        encoding="utf-8",
    )
    return RepairTaskGenerator(
        coding_tool=coding_tool,
        plan_dir=plan_dir,
        project_dir=project_dir,
    )


# ---------------------------------------------------------------------------
# Part 1 -- the structural check on the derived paths
# ---------------------------------------------------------------------------

#: Structurally broken by construction: the chain ends in ``tee``, so a
#: failing run reports success. The same defect the fill loop rejects a
#: model answer for, arriving here from ``verification_plan.json``.
_BROKEN_COMMAND = "pytest -q tests/test_api.py | tee out.log"

#: Broken the other way: a mid-chain ``ls`` on a path that is not there
#: exits non-zero and short-circuits the chain behind it. ``inspect_command``
#: deliberately clears inspection links once the chain runs a test runner
#: (inspecting *on the way to* verification is fine), so this one is a
#: build chain -- the shape a binary-stale verification point ships.
_PROBE_CHAIN_COMMAND = (
    "cd /proj && ls -la build/libext.dylib && cargo build --release"
)

#: A semicolon ends the chain and ``echo`` reports success, so the run
#: before it can never fail. The other direction of the same defect: a
#: command that cannot report failure certifies work nobody did.
_SEMICOLON_COMMAND = "cd /proj && pytest -q tests/test_api.py; echo done"

_HEALTHY_COMMAND = "pytest -q tests/test_api.py"


def _resolve(gen, task, evidence):
    return gen._resolve_repair_test_command(task, evidence)


def test_vp_path_refuses_a_structurally_broken_command(tmp_path):
    """The ``vp`` derivation used to return the plan's string verbatim.

    A command that can only exit non-zero becomes ``missing`` -- the
    assembler's own "we could not find one" label. The task is then
    emitted, routed through the adversarial second pass, and counted,
    rather than shipped as a false-failure generator.
    """
    gen = _generator(tmp_path, _ExplodingCodingTool())
    evidence = [{
        "id": "VP-007",
        "vp_id": "VP-007",
        "test_command": _BROKEN_COMMAND,
        "actual_result": "3 failed",
        "evidence": "pytest output",
    }]
    assert _resolve(gen, {"vp_id": "VP-007"}, evidence) == ("", "missing")


@pytest.mark.parametrize(
    "command", [_PROBE_CHAIN_COMMAND, _SEMICOLON_COMMAND],
)
def test_vp_path_refuses_the_other_broken_shapes(tmp_path, command):
    """The mid-chain probe and the semicolon, both on the ``vp`` path.

    Parametrised because they fail in opposite directions -- one cannot
    report success, the other cannot report failure -- and a gate that
    only caught one would leave the other as a false-pass generator on
    the completion gate.
    """
    gen = _generator(tmp_path, _ExplodingCodingTool())
    evidence = [{
        "id": "VP-007",
        "vp_id": "VP-007",
        "test_command": command,
        "actual_result": "3 failed",
        "evidence": "pytest output",
    }]
    assert _resolve(gen, {"vp_id": "VP-007"}, evidence) == ("", "missing")


def test_single_evidence_path_refuses_a_structurally_broken_command(tmp_path):
    """The same rule on the second derivation.

    It fires when the task names no ``vp_id``, so the round's only usable
    evidence command is the candidate -- and that candidate is no more
    trustworthy than the plan author's.
    """
    gen = _generator(tmp_path, _ExplodingCodingTool())
    evidence = [{
        "id": "VP-007",
        "vp_id": "VP-007",
        "test_command": _BROKEN_COMMAND,
        "actual_result": "3 failed",
        "evidence": "pytest output",
    }]
    assert _resolve(gen, {}, evidence) == ("", "missing")


def test_a_rejected_derivation_continues_to_the_next_one(tmp_path):
    """A rejection is "this source had nothing", not a third outcome.

    The task names ``VP-001``, whose command is broken, while
    ``VP-002``'s is sound. Resolution continues instead of stopping: one
    unusable author-declared command must not cost the row the command
    the very next source could have supplied.
    """
    gen = _generator(tmp_path, _ExplodingCodingTool())
    healthy = "pytest -q tests/test_b.py"
    evidence = [
        {"id": "VP-001", "vp_id": "VP-001", "test_command": _BROKEN_COMMAND,
         "actual_result": "", "evidence": ""},
        {"id": "VP-002", "vp_id": "VP-002", "test_command": healthy,
         "actual_result": "", "evidence": ""},
    ]
    # Two non-empty commands, so ``single_evidence`` does not fire --
    # that is the pre-existing rule. What is pinned is that the broken
    # one is not returned by the path that owns it.
    command, source = _resolve(gen, {"vp_id": "VP-001"}, evidence)
    assert command != _BROKEN_COMMAND
    assert source == "missing"


def test_structurally_sound_derived_commands_are_untouched(tmp_path):
    """The counterweight: the gate rejects shapes, not commands.

    Both derivations still return the plan's string byte for byte, under
    the same ``source``. A gate that flagged these would make every
    repair of a declarative plan unverifiable, which is a strictly worse
    outcome than the one it prevents.
    """
    gen = _generator(tmp_path, _ExplodingCodingTool())
    one = [{"id": "VP-001", "vp_id": "VP-001",
            "test_command": _HEALTHY_COMMAND,
            "actual_result": "", "evidence": ""}]

    assert _resolve(gen, {"vp_id": "VP-001"}, one) == (
        _HEALTHY_COMMAND, "vp",
    )
    assert _resolve(gen, {}, one) == (_HEALTHY_COMMAND, "single_evidence")


def test_the_red_probe_stays_off_the_derived_paths(tmp_path, monkeypatch):
    """Structural check yes, RED probe no -- and that is deliberate.

    ``check_falsifiable`` spawns the command. These candidates belong to
    verification points that have just failed, so the probe would nearly
    always answer "fails as it should" while still costing a subprocess
    per distinct command. The structural check executes nothing, which is
    why it extends to these paths and the probe does not.
    """
    gen = _generator(tmp_path, _ExplodingCodingTool())
    calls: List[str] = []

    def _record(command, **kwargs):
        calls.append(command)
        return True, "recorded"

    monkeypatch.setattr(rg, "check_falsifiable", _record)

    evidence = [{"id": "VP-001", "vp_id": "VP-001",
                 "test_command": _HEALTHY_COMMAND,
                 "actual_result": "", "evidence": ""}]
    assert _resolve(gen, {"vp_id": "VP-001"}, evidence) == (
        _HEALTHY_COMMAND, "vp",
    )
    assert _resolve(gen, {}, evidence) == (_HEALTHY_COMMAND, "single_evidence")
    assert calls == [], "a derived command must not be executed"


def test_a_model_command_still_has_to_survive_both_checks(
    tmp_path, monkeypatch,
):
    """Extending the structural rule cost the RED probe nothing.

    The probe is disabled for the passing half rather than monkeypatched
    away, so the test also covers the documented kill switch.
    """
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")
    gen = _generator(tmp_path, _ExplodingCodingTool())

    # Structurally sound + (probe disabled, i.e. "fails as it should").
    assert _resolve(gen, {"test_command": _HEALTHY_COMMAND}, []) == (
        _HEALTHY_COMMAND, "llm",
    )
    # Structurally broken: discarded before the probe is even consulted.
    assert _resolve(gen, {"test_command": _BROKEN_COMMAND}, []) == (
        "", "missing",
    )


def test_api_replay_survives_its_own_shape_check(tmp_path):
    """The command ``_api_replay_command`` builds is checked, and passes.

    Everything interpolated into it is single-quoted and its own literals
    hold no inspection verb, so the check costs nothing on the normal
    shape. It is there so that stays a property rather than an accident
    of today's plan paths.
    """
    gen = _generator(
        tmp_path, _ExplodingCodingTool(),
        plan_points=[{"id": "VP-009", "verification_method": "api_test"}],
    )
    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=[{"id": "VP-009"}],
        plan_path=str(gen.plan_dir / "verification_plan.json"),
    )
    command, source = assembler._resolve_test_command("VP-009", {})
    assert source == "api_replay"
    assert rg.inspect_command(command) == []


def test_a_structurally_broken_api_replay_is_not_shipped(
    tmp_path, monkeypatch,
):
    """Same rule on the assembler-side derivation.

    The negative control for the check above: a replay command that
    cannot exit 0 must not reach the completion gate, and the row falls
    through to the assembler's own labelling rather than acquiring a
    third outcome.
    """
    gen = _generator(
        tmp_path, _ExplodingCodingTool(),
        plan_points=[{"id": "VP-009", "verification_method": "api_test"}],
    )
    assembler = RepairTaskAssembler(
        round_number=1,
        failed_vps=[{"id": "VP-009"}],
        plan_path=str(gen.plan_dir / "verification_plan.json"),
    )
    monkeypatch.setattr(
        assembler, "_api_replay_command", lambda vp_id: _BROKEN_COMMAND,
    )
    assert assembler._resolve_test_command("VP-009", {}) == ("", "missing")


# ---------------------------------------------------------------------------
# Part 3 -- the closing summary must not report a point that was answered
# ---------------------------------------------------------------------------


def test_the_closing_summary_excludes_points_a_derivation_answered(
    tmp_path, monkeypatch, capsys,
):
    """A point the loop skipped is not a point the loop failed.

    ``reasons`` is fed back into the retry prompt *and* counted by the
    closing log line. Recording a reason for a row the pending filter
    skips makes the log claim a point went unanswered when the assembler
    labelled it from a derivation -- the log contradicts the task it is
    describing.
    """
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")
    tool = _CountingCodingTool([])
    gen = _generator(tmp_path, tool)
    contents = [{
        "failed_vp_id": "VP-777",
        "title": "修复 VP-777",
        "description": "d",
        "test_command": _BROKEN_COMMAND,
    }]
    # ``single_evidence`` answers for this row, so the loop must skip it.
    failed_vps = [{
        "id": "VP-777",
        "test_command": _HEALTHY_COMMAND,
        "actual_result": "1 failed",
        "evidence": "pytest output",
    }]

    gen._fill_missing_test_commands(contents, failed_vps, 1)
    out = capsys.readouterr().out

    assert tool.fill_prompts == [], "the row was never pending"
    assert "no acceptance command obtained" not in out


def test_the_closing_summary_still_reports_a_point_it_could_not_rescue(
    tmp_path, monkeypatch, capsys,
):
    """The other half: the summary must not go quiet when it should speak.

    With no usable command anywhere, the row stays pending, the loop
    spends its attempts, and the closing line names it.
    """
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")
    tool = _CountingCodingTool([])
    gen = _generator(tmp_path, tool)
    contents = [{
        "failed_vp_id": "VP-888",
        "title": "修复 VP-888",
        "description": "d",
        "test_command": _BROKEN_COMMAND,
    }]

    gen._fill_missing_test_commands(contents, [], 1)
    out = capsys.readouterr().out

    assert tool.fill_prompts, "the row really was pending"
    assert "no acceptance command obtained" in out
    assert "VP(s)" in out
