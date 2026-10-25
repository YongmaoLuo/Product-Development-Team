"""Only a deliverable defect may become a repair task (P0-2 wiring).

A repair task reworks the DELIVERABLE. That is the right remedy for
``deliverable_defect`` and the wrong remedy for the other three classes
of the four-class vocabulary (``verification.failure_triage``):

``infra``
    The attempt malfunctioned (rate limit, timeout, unparseable answer).
    Another attempt is the remedy; a rework touches a file that was
    never the problem.
``criterion_defect``
    The verification point itself is wrong. No rework of the deliverable
    satisfies it — a human has to fix the criterion.
``out_of_scope``
    Nobody agreed to build this. The remedy is nothing at all.

So the repair generator filters the failed VPs *before* it asks the
model anything, and the refusals are recorded: a failure that got no
repair task is exactly the thing an operator asks about, and "no task"
is not an answer.

The other half of the contract is the historical one: a verdict written
before the classification existed carries no class, and it must keep
being repaired exactly as it used to be. A missing class reads as
``deliverable_defect`` — the class that counts, and the conservative
reading of "nobody said otherwise".

These are pure rule tests: no model call, no subprocess, no filesystem
side-effect beyond pytest's own ``tmp_path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Ensure backend/ is on sys.path so ``import repair_generator`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from repair_generator import (  # noqa: E402
    RepairTaskAssembler,
    RepairTaskGenerator,
    select_repairable_failed_vps,
)


class _StubTool:
    """A coding tool that answers every call with one fixed envelope.

    The generator calls the model once for the content and (at most)
    twice more for the bounded acceptance-command fill; this stub answers
    all of them the same way, which is enough: the fill loop looks for a
    ``commands`` key and finds none, so it simply runs out of attempts.
    """

    def __init__(self, tasks):
        self.tasks = tasks
        self.prompts = []

    def query_json(self, prompt, system_instruction, timeout=None):
        self.prompts.append(prompt)
        return {"tasks": self.tasks}


def _task(vp_id, **extra):
    """One content row, as the model would answer it."""
    row = {
        "failed_vp_id": vp_id,
        "title": f"fix {vp_id}",
        "description": f"repair {vp_id}",
        "acceptance_criteria": f"{vp_id} passes",
    }
    row.update(extra)
    return row


def _generator(tmp_path, tasks):
    """A generator whose model answers with ``tasks``."""
    return RepairTaskGenerator(
        _StubTool(tasks), tmp_path, tmp_path / "proj",
    )


# ---------------------------------------------------------------------------
# The filter itself
# ---------------------------------------------------------------------------

def test_only_deliverable(tmp_path):
    """``out_of_scope`` and ``infra`` get no repair task; the deliverable
    defect does.

    The model is answered with a row for EVERY failed VP — a chatty model
    that volunteers a task for a failure nobody wants repaired. The rows
    for the refused VPs must still not reach the caller: the filter runs
    before the prompt is built, so the model is never asked about them,
    and the parser is handed the filtered list, so a volunteered row
    naming a refused VP is dropped as the hallucination it now is.
    """
    failed_vps = [
        {"id": "VP-1", "failure_class": "out_of_scope", "title": "a"},
        {"id": "VP-2", "failure_class": "infra", "title": "b"},
        {"id": "VP-3", "failure_class": "deliverable_defect", "title": "c"},
    ]
    gen = _generator(tmp_path, [_task("VP-1"), _task("VP-2"), _task("VP-3")])

    contents = gen.generate_repair_contents(failed_vps, round_number=1)

    assert [c["failed_vp_id"] for c in contents] == ["VP-3"], (
        "only the deliverable defect may produce a repair task, got "
        f"{[c['failed_vp_id'] for c in contents]!r}"
    )
    # The refused VPs are named in the prompt the model was given, and
    # the two that must not be repaired are not.
    content_prompt = gen.coding_tool.prompts[0]
    assert "VP-3" in content_prompt
    assert "VP-1" not in content_prompt
    assert "VP-2" not in content_prompt

    # The refusals are recorded, with a reason each.
    refused = {item["id"]: item for item in gen.filtered_vps}
    assert set(refused) == {"VP-1", "VP-2"}
    assert refused["VP-1"]["failure_class"] == "out_of_scope"
    assert refused["VP-2"]["failure_class"] == "infra"
    assert refused["VP-1"]["reason"]
    assert refused["VP-2"]["reason"]


def test_missing_class_defaults(tmp_path):
    """A verdict with no class is repaired, exactly as before.

    The classification is newer than most verdicts, so "no class stated"
    has to keep meaning "repair it" — otherwise every plan whose
    verdicts predate the vocabulary would silently stop being repaired.
    """
    gen = _generator(tmp_path, [_task("VP-7")])

    contents = gen.generate_repair_contents(
        [{"id": "VP-7", "title": "no class stated"}],
        round_number=1,
    )

    assert [c["failed_vp_id"] for c in contents] == ["VP-7"]
    assert gen.filtered_vps == []


def test_unknown_class_is_read_as_deliverable(tmp_path):
    """An unrecognised class spelling is read as ``deliverable_defect``.

    Same rule as a missing class, and the same reason: the conservative
    reading is the one that keeps repairing what it used to repair.
    """
    gen = _generator(tmp_path, [_task("VP-8")])

    contents = gen.generate_repair_contents(
        [{"id": "VP-8", "failure_class": "not-a-class", "title": "t"}],
        round_number=1,
    )

    assert [c["failed_vp_id"] for c in contents] == ["VP-8"]
    assert gen.filtered_vps == []


def test_criterion_defect_is_refused(tmp_path):
    """A criterion defect is not a deliverable defect, so it is refused."""
    gen = _generator(tmp_path, [_task("VP-9")])

    contents = gen.generate_repair_contents(
        [{"id": "VP-9", "failure_class": "criterion_defect", "title": "t"}],
        round_number=1,
    )

    assert contents == []
    assert [item["id"] for item in gen.filtered_vps] == ["VP-9"]


def test_nothing_repairable_returns_no_content_and_no_llm_call(tmp_path):
    """No deliverable defect means no content — and no model call.

    Asking the model for repair tasks when every failure is out of scope
    would burn a call to produce work nobody wants, and would give the
    caller a non-empty answer to "what is there to repair?".
    """
    tool = _StubTool([_task("VP-1")])
    gen = RepairTaskGenerator(tool, tmp_path, tmp_path / "proj")

    contents = gen.generate_repair_contents(
        [{"id": "VP-1", "failure_class": "out_of_scope", "title": "t"}],
        round_number=1,
    )

    assert contents == []
    assert tool.prompts == []
    assert [item["id"] for item in gen.filtered_vps] == ["VP-1"]


# ---------------------------------------------------------------------------
# The pure rule, on its own
# ---------------------------------------------------------------------------

def test_select_repairable_failed_vps_splits_and_explains():
    """The rule itself: what is kept, what is refused, and why."""
    repairable, filtered = select_repairable_failed_vps([
        {"id": "VP-1", "failure_class": "out_of_scope"},
        {"id": "VP-2", "failure_class": "deliverable_defect"},
        {"id": "VP-3", "failure_class": "infra"},
        {"id": "VP-4"},
    ])

    assert [vp["id"] for vp in repairable] == ["VP-2", "VP-4"]
    assert [item["id"] for item in filtered] == ["VP-1", "VP-3"]
    assert all(item["reason"] for item in filtered)


# ---------------------------------------------------------------------------
# The boundaries the change must not move
# ---------------------------------------------------------------------------

def test_filtering_does_not_change_the_task_id_rule(tmp_path):
    """The id rule is untouched: ``repair-r{round}-{seq:02d}``, contiguous.

    Filtering happens upstream of the assembler, so the tasks that ARE
    emitted keep the ids they always had — the same rule, the same
    format — and no gap is left behind by a refused VP.
    """
    gen = _generator(tmp_path, [
        _task("VP-2", files_to_modify=["src/remote.rs"]),
        _task("VP-4", files_to_modify=["src/client.rs"]),
    ])
    contents = gen.generate_repair_contents(
        [
            {"id": "VP-1", "failure_class": "out_of_scope", "title": "a"},
            {"id": "VP-2", "failure_class": "deliverable_defect", "title": "b"},
            {"id": "VP-3", "failure_class": "infra", "title": "c"},
            {"id": "VP-4", "failure_class": "deliverable_defect", "title": "d"},
        ],
        round_number=3,
    )

    tasks = RepairTaskAssembler(
        round_number=3,
        failed_vps=[
            {"id": "VP-1", "title": "a"},
            {"id": "VP-2", "title": "b"},
            {"id": "VP-3", "title": "c"},
            {"id": "VP-4", "title": "d"},
        ],
        vp_test_commands={},
        seq_base=1,
    ).assemble(contents)

    assert [task["id"] for task in tasks] == ["repair-r3-01", "repair-r3-02"]
    assert [task["failed_vp_id"] for task in tasks] == ["VP-2", "VP-4"]


def test_declared_files_to_modify_is_unchanged(tmp_path):
    """A declared edit scope is the model's own answer and it wins.

    The shared scope rule is a fallback for the rows that declared
    nothing; a row that declared something keeps exactly what it
    declared.
    """
    gen = _generator(tmp_path, [
        _task("VP-2", files_to_modify=["src/remote.rs"]),
    ])

    contents = gen.generate_repair_contents(
        [
            {"id": "VP-2", "failure_class": "deliverable_defect", "title": "b",
             "actual_result": "AssertionError in backend/other.py:12"},
        ],
        round_number=1,
    )

    assert contents[0]["files_to_modify"] == ["src/remote.rs"]


def test_missing_files_to_modify_is_derived_by_the_shared_rule(tmp_path):
    """A row that declared no scope gets one from the shared rule.

    The rule is :func:`test_command_rules.derive_files_to_modify` — the
    one the refiner uses — so the two sides cannot drift on what counts
    as a file. Without a scope the assembler refuses the task outright
    (an empty scope reads as "touch nothing"), so this is the difference
    between a repair that runs and one that is dropped.
    """
    gen = _generator(tmp_path, [_task("VP-2")])

    contents = gen.generate_repair_contents(
        [
            {"id": "VP-2", "failure_class": "deliverable_defect", "title": "b",
             "actual_result": "AssertionError: expected 1 == 2 "
                              "(backend/service/handler.py:42)"},
        ],
        round_number=1,
    )

    assert contents[0]["files_to_modify"] == ["backend/service/handler.py"]


def test_the_failure_record_is_handed_to_the_rule_as_it_stands(tmp_path):
    """A record that already carries ``stack`` / ``diff`` is read as it is.

    The rule reads the failure's text and the paths the failed attempt
    changed; a record that carries either of them under its own key
    must not have that key dropped on the way to the rule. The replay
    corpus depends on it: its deliverable samples seed the derivation
    with ``stack`` or ``diff``.
    """
    gen = _generator(tmp_path, [_task("VP-2")])

    contents = gen.generate_repair_contents(
        [
            {"id": "VP-2", "failure_class": "deliverable_defect", "title": "b",
             "stack": '  File "backend/service/handler.py", line 42'},
        ],
        round_number=1,
    )

    assert contents[0]["files_to_modify"] == ["backend/service/handler.py"]


def test_the_changed_paths_seed_the_derived_scope(tmp_path):
    """A ``diff`` on the record is the other half of the rule's answer."""
    gen = _generator(tmp_path, [_task("VP-2")])

    contents = gen.generate_repair_contents(
        [
            {"id": "VP-2", "failure_class": "deliverable_defect", "title": "b",
             "diff": ["backend/service/handler.py"]},
        ],
        round_number=1,
    )

    assert contents[0]["files_to_modify"] == ["backend/service/handler.py"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
