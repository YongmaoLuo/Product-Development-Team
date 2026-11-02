"""A repair task must be anchored to the cases that actually failed.

Why this file exists
--------------------
The whole-repository gate knows exactly which cases failed — the runner
writes their node ids into
``plans/<plan_id>/vp_artifacts/<vp_id>/ci_output.json`` (the
``junit_failures`` list). But **nothing read that file**: the ids were
on disk and on nobody's path. What reached the repair generator was
the gate's *summary* (``actual_result`` + ``evidence``), and a summary
names no case — so the prompt could say "narrow the criterion to the
cases that failed" without ever saying which cases those were.

This file pins the two ends of the wire:

* the orchestrator turns the artifact's node ids back into pytest
  summary lines and appends them to the attached ``run_output``;
* the repair-content prompt names those cases in their own section, so
  the model knows what has to turn green and that it may not weaken it.

And it pins the thing that is easy to break: **without** the artifact,
the attached output is byte-for-byte what it was before. The artifact
is an audit trail; it must never become a precondition of a round.

What is pinned here
-------------------
* the node ids reach the attached ``run_output``, with pytest's
  ``FAILED`` / ``ERROR`` markers;
* the ids survive the round trip: what the prompt reads back out of the
  attached output is exactly the ids that went in;
* without the artifact (and without ``plan_dir``) nothing changes;
* the repair-content prompt lists the failing cases under their own
  heading.
"""

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repair_generator as rg  # noqa: E402
import test_command_quality as tcq  # noqa: E402
from verification.orchestrator import _attach_round_run_output  # noqa: E402


#: The two node ids the gate's artifact reports.
CASE_ONE = "backend/tests/unit/test_alpha.py::test_first"
CASE_TWO = "backend/tests/unit/test_beta.py::test_second"

#: The artifact of a failed whole-repository gate, in the shape
#: ``verification_ci_runner`` writes it.
CI_OUTPUT = {
    "junit_phases": [{"phase": 1, "tests": 2, "failures": 1, "errors": 1}],
    "junit_failures": [
        {
            "phase": 1,
            "node_id": CASE_ONE,
            "kind": "failure",
            "first_line": "AssertionError: assert 1 == 2",
        },
        {
            "phase": 1,
            "node_id": CASE_TWO,
            "kind": "error",
            "first_line": "RuntimeError: boom",
        },
    ],
}


def _write_artifact(plan_dir: Path, vp_id: str = "VP-1") -> Path:
    """Write ``vp_artifacts/<vp_id>/ci_output.json`` under ``plan_dir``."""
    artifact_dir = plan_dir / "vp_artifacts" / vp_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    target = artifact_dir / "ci_output.json"
    target.write_text(
        json.dumps(CI_OUTPUT, ensure_ascii=False), encoding="utf-8",
    )
    return target


def _candidate() -> dict:
    return {
        "id": "VP-1",
        "title": "整仓门禁失败",
        "priority": "medium",
    }


def _evidence() -> dict:
    return {
        "VP-1": {
            "actual_result": "门禁失败：phase 1 有 2 条失败",
            "evidence": "见 vp_artifacts/VP-1/ci_output.json",
        },
    }


def test_attach_round_run_output_includes_junit_case_ids(tmp_path):
    """The gate's node ids are attached to the failed VP's run output."""
    _write_artifact(tmp_path)

    candidate = _candidate()
    attached = _attach_round_run_output(
        [candidate], _evidence(), plan_dir=tmp_path,
    )

    assert attached == 1
    run_output = candidate["run_output"]
    assert CASE_ONE in run_output
    assert CASE_TWO in run_output
    assert f"FAILED {CASE_ONE} - AssertionError: assert 1 == 2" in run_output
    assert f"ERROR {CASE_TWO} - RuntimeError: boom" in run_output
    assert "vp_artifacts/VP-1/ci_output.json" in run_output


def test_failing_case_ids_finds_the_attached_ids(tmp_path):
    """What the prompt reads back out is exactly what went in."""
    _write_artifact(tmp_path)

    candidate = _candidate()
    _attach_round_run_output([candidate], _evidence(), plan_dir=tmp_path)

    assert tcq.failing_case_ids(candidate["run_output"]) == [CASE_ONE, CASE_TWO]


def test_attach_round_run_output_is_unchanged_without_the_artifact(tmp_path):
    """No artifact (or no ``plan_dir``) ⇒ the output is byte-identical."""
    evidence = {
        "VP-1": {
            "actual_result": "门禁失败：phase 1 有 2 条失败",
            "evidence": "stdout:\nsome captured output\n",
        },
    }

    without_dir = _candidate()
    _attach_round_run_output([without_dir], evidence)
    expected = "门禁失败：phase 1 有 2 条失败\nstdout:\nsome captured output\n"

    # The artifact is missing in this plan directory.
    missing = _candidate()
    _attach_round_run_output([missing], evidence, plan_dir=tmp_path)

    assert without_dir["run_output"] == expected
    assert missing["run_output"] == expected
    assert "ci_output.json" not in missing["run_output"]


def test_repair_contents_prompt_lists_failing_cases(tmp_path):
    """The prompt names the failing cases under their own heading."""
    failed_vps = [
        {
            "id": "VP-1",
            "title": "整仓门禁失败",
            "run_output": "FAILED x::y - boom\nFAILED z::w - bang\n",
        },
    ]

    prompt = rg._build_repair_contents_prompt(
        failed_vps, 3, tmp_path, tmp_path,
    )

    assert rg.FAILING_CASES_HEADING in prompt
    assert "x::y" in prompt
    assert "z::w" in prompt
    # The section lists the cases, numbered, right under the heading.
    heading_index = prompt.index(rg.FAILING_CASES_HEADING)
    tail = prompt[heading_index:heading_index + 200]
    assert "1. x::y" in tail
    assert "2. z::w" in tail
