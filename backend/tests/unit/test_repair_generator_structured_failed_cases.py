"""The repair generator reads the machine's failing-case ids first.

Why this file exists
--------------------
The repair loop narrows a repair task's acceptance command to the cases
that actually failed.  Until now it found those cases by **scraping the
round's run output** with a regex — reading pytest's short summary out
of free text.  The run output is prose the runner happened to write:
which field carries the summary depends on the runner, a killed run
prints no summary at all, and the text is reworded every round.

Meanwhile the CI runner has been writing the answer down **as data**:
``vp_artifacts/<vp_id>/ci_output.json`` carries ``failed_cases`` — the
node ids of the cases that failed, taken from the junit XML rather than
from a line of text.  That is the machine's own answer to the question
the repair loop is asking, and it is the same field the failure
fingerprint reads, so the repair loop and the fingerprint can no longer
disagree about which defect this round hit.

So the consumer reads the structured ids **first** and falls back to the
text only when the artifact does not answer.

What is pinned here
-------------------
* the structured ids win over the text ids for the same VP;
* every way the artifact can fail to answer falls back to the text
  (missing, unreadable, unparseable, not a dict, no ``failed_cases``);
* the machine's own ids that are **not** node ids (the schema defects,
  ``schema:ci_entry_missing``) are not command targets — a command can
  never select them, so treating them as cases would make every
  candidate command look wide;
* several VPs are merged into one ordered, deduplicated list;
* both prompts carry the structured ids, and the narrowing path reads
  them too.
"""

import inspect
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repair_generator as rg  # noqa: E402
from repair_generator import RepairTaskGenerator  # noqa: E402


#: A run output in pytest's short-summary shape: the id of the case that
#: failed is the token after the marker.
RUN_OUTPUT = "FAILED tests/b.py::test_y - assert 1 == 2"

#: The id the machine wrote down for the case that failed.  It is
#: deliberately a *different* case from the one the text names, so a
#: test that reads the text cannot pass by accident.
MACHINE_ID = "tests/a.py::test_x"

#: The id the text names.
TEXT_ID = "tests/b.py::test_y"

#: The whole-repository gate — the command a ``full_ci`` point carries.
#: Shipping it as a repair criterion is the defect the narrowing exists
#: to prevent.
WIDE_GATE = "bash scripts/run_tests.sh"

#: A narrow command that selects exactly the machine's case.
NARROW = "pytest backend/tests/a.py::test_b -q"


def _write_artifact(plan_dir: Path, vp_id: str, payload: Any) -> Path:
    """Write ``payload`` as ``vp_id``'s CI artifact; return its path.

    ``payload`` is dumped as JSON unless it is already a string — a
    string is written verbatim, which is how the "the file is not JSON
    at all" case is set up.
    """
    directory = plan_dir / "vp_artifacts" / vp_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "ci_output.json"
    target.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return target


def _artifact(plan_dir: Path, vp_id: str, failed_cases: Any) -> Path:
    """Write a contract-shaped artifact carrying ``failed_cases``."""
    return _write_artifact(plan_dir, vp_id, {
        "vp_id": vp_id,
        "verifier_source": {"file": "backend/tests/test_a.py"},
        "failed_cases": failed_cases,
    })


def _item(vp_id: str = "VP-1") -> Dict[str, Any]:
    """A failed VP whose run output names a case the artifact does not."""
    return {
        "id": vp_id,
        "title": "登录校验失败",
        "actual_result": "AssertionError: assert 401 == 200",
        "run_output": RUN_OUTPUT,
    }


# ---------------------------------------------------------------------------
# The structured ids win
# ---------------------------------------------------------------------------


def test_structured_ids_win_over_text(tmp_path):
    """The machine's ids are the answer; the text is the fallback.

    The artifact names ``tests/a.py::test_x`` and the run output names
    ``tests/b.py::test_y``.  Both are "the case that failed", so taking
    both would tell the repair agent to turn green a case that is not
    red.  The machine wrote its answer down, so the machine wins.
    """
    _artifact(tmp_path, "VP-1", [MACHINE_ID])

    assert rg._failing_case_ids_for([_item()], tmp_path) == [MACHINE_ID]


def test_missing_artifact_falls_back_to_text(tmp_path):
    """No artifact on disk: the text is all there is."""
    assert rg._failing_case_ids_for([_item()], tmp_path) == [TEXT_ID]


def test_absent_failed_cases_falls_back_to_text(tmp_path):
    """An artifact that does not carry the field cannot answer."""
    _write_artifact(tmp_path, "VP-1", {
        "vp_id": "VP-1",
        "verifier_source": {"file": "backend/tests/test_a.py"},
    })

    assert rg._failing_case_ids_for([_item()], tmp_path) == [TEXT_ID]


def test_malformed_artifact_falls_back_to_text(tmp_path):
    """Every way the artifact can fail to answer falls back to the text.

    A payload that is not a dict, a file that is not JSON at all, and a
    path that cannot be read are three different faults with one
    consequence: the structured source did not answer.
    """
    item = _item()

    _write_artifact(tmp_path, "VP-1", ["not", "a", "dict"])
    assert rg._failing_case_ids_for([item], tmp_path) == [TEXT_ID]

    _write_artifact(tmp_path, "VP-1", "{ this is not json")
    assert rg._failing_case_ids_for([item], tmp_path) == [TEXT_ID]

    # A directory where the file should be: reading it raises OSError.
    (tmp_path / "vp_artifacts" / "VP-1" / "ci_output.json").unlink()
    (tmp_path / "vp_artifacts" / "VP-1" / "ci_output.json").mkdir()
    assert rg._failing_case_ids_for([item], tmp_path) == [TEXT_ID]


def test_failed_cases_that_is_not_a_list_falls_back_to_text(tmp_path):
    """``failed_cases`` is a list in the contract; anything else is not an answer."""
    _artifact(tmp_path, "VP-1", "tests/a.py::test_x")

    assert rg._failing_case_ids_for([_item()], tmp_path) == [TEXT_ID]


def test_schema_ids_are_skipped(tmp_path):
    """A schema defect is not a case: no command can select it.

    The CI runner names a schema rejection ``schema:ci_entry_missing``
    — the machine's identity of the defect, not the name of a test.
    Measuring a candidate command against it would make every command
    look wide (nothing selects that string), so those ids are not
    command targets.
    """
    _artifact(tmp_path, "VP-1", ["schema:ci_entry_missing", MACHINE_ID])

    assert rg._failing_case_ids_for([_item()], tmp_path) == [MACHINE_ID]


def test_blank_and_non_string_entries_are_ignored(tmp_path):
    """Only real ids are ids."""
    _artifact(tmp_path, "VP-1", ["", "   ", 42, None, MACHINE_ID])

    assert rg._failing_case_ids_for([_item()], tmp_path) == [MACHINE_ID]


def test_ids_of_several_vps_are_merged_in_order(tmp_path):
    """Several VPs are one list: ordered by appearance, without repeats."""
    _artifact(tmp_path, "VP-1", [MACHINE_ID, "tests/a.py::test_dup"])
    _artifact(tmp_path, "VP-2", ["tests/a.py::test_dup", "tests/c.py::test_z"])

    items = [_item("VP-1"), _item("VP-2")]

    assert rg._failing_case_ids_for(items, tmp_path) == [
        MACHINE_ID,
        "tests/a.py::test_dup",
        "tests/c.py::test_z",
    ]


def test_a_vp_without_an_artifact_still_contributes_its_text_ids(tmp_path):
    """The fallback is per VP, not per round.

    One VP's artifact answers and another's does not; the second one
    still contributes what its run output named, because the text is
    per-VP too.
    """
    _artifact(tmp_path, "VP-1", [MACHINE_ID])

    items = [_item("VP-1"), _item("VP-2")]

    assert rg._failing_case_ids_for(items, tmp_path) == [MACHINE_ID, TEXT_ID]


# ---------------------------------------------------------------------------
# The prompts
# ---------------------------------------------------------------------------


def test_prompt_carries_structured_ids(tmp_path):
    """Both prompts carry the machine's ids — and not the text's.

    The repair-content prompt says which cases have to turn green, and
    the fill prompt says which ones the acceptance command has to
    select.  Both read the same ids, so they read the machine's.
    """
    _artifact(tmp_path, "VP-1", [MACHINE_ID])
    failed_vps = [_item()]

    content_prompt = rg._build_repair_contents_prompt(
        failed_vps, 1, tmp_path, tmp_path,
    )
    fill_prompt = rg._build_test_command_fill_prompt(
        failed_vps, {}, {}, attempt=1, max_attempts=2,
        plan_dir=tmp_path, project_dir=tmp_path,
    )

    for prompt in (content_prompt, fill_prompt):
        assert MACHINE_ID in prompt
        assert TEXT_ID not in prompt


# ---------------------------------------------------------------------------
# The narrowing path
# ---------------------------------------------------------------------------


def test_narrowing_path_uses_structured_ids():
    """The resolver must not keep a text-only path alive.

    Three call sites read the failing-case ids, and a call site left on
    the old text-only reading is a call site that narrows against the
    prose rather than against the machine's answer.  The source of the
    resolver is checked rather than its behaviour because the behaviour
    is a subprocess away.
    """
    source = inspect.getsource(RepairTaskGenerator._resolve_repair_test_command)

    assert "_failing_case_ids_for(" in source
    assert "failing_case_ids(" not in source


def test_the_resolver_measures_a_command_against_the_structured_ids(
    tmp_path, monkeypatch,
):
    """The gate is refused because the artifact named a case.

    The run output names no case at all (a run that died before pytest
    wrote its summary), so the text alone leaves the resolver with
    nothing to measure against — and the whole-repository gate sails
    through as the repair's criterion.  The artifact names the case, so
    the gate is refused.
    """
    monkeypatch.setenv(RepairTaskGenerator.FALSIFIABILITY_PROBE_ENV, "1")
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        '{"verification_points": []}', encoding="utf-8",
    )
    _artifact(plan_dir, "VP-013", ["backend/tests/a.py::test_b"])

    generator = RepairTaskGenerator(
        coding_tool=None, plan_dir=plan_dir, project_dir=tmp_path,
    )
    evidence = [{
        "vp_id": "VP-013",
        "test_command": WIDE_GATE,
        "actual_result": "the run was killed",
        "evidence": "the run was killed before the summary",
    }]

    command, source = generator._resolve_repair_test_command(
        {"vp_id": "VP-013", "test_command": WIDE_GATE}, evidence,
    )

    assert command != WIDE_GATE, (
        "the whole-repository gate must not be shipped as the criterion "
        "of a round whose artifact names the case that failed"
    )
    assert (command, source) == ("", "missing")


def test_the_artifact_reader_returns_the_machines_ids(tmp_path):
    """The reader is the one that knows where the artifact lives."""
    _artifact(tmp_path, "VP-1", [MACHINE_ID, "", 7])

    assert rg._artifact_failed_cases(tmp_path, "VP-1") == [MACHINE_ID]
    assert rg._artifact_failed_cases(tmp_path, "VP-404") == []
