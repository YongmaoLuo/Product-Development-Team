"""The gate's structured verdict (``pdt-gate/v1``) is what the framework grades.

An exit code says one thing: "did it pass".  It cannot say which stage
went red, how many things failed, or what the first error was — so a
reader of the verdict has to dig through a scrolled-off stdout to find
out.  A gate that writes a verdict file says all three in one
machine-readable place, and the framework grades THAT; the exit code
becomes the transport signal that says the gate ran and how it ended.

Covers:
  * the five ways a verdict is graded (pass / fail / inconclusive, and
    the two ways a verdict can contradict the process it describes);
  * a plan that never declared the format: the file is not read at all,
    and a broken file lying around changes nothing;
  * a verdict file that cannot be parsed: the exit code decides, and the
    evidence says why the verdict was not used;
  * the reason names the failing stage and its first error — "some lane
    went red" is the sentence this feature exists to replace.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402
from ci_contract import CONTRACT_FILENAME  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_PASSED,
    run_ci_verification,
)


def _vp(entry: str, **extra) -> dict:
    """A ``full_ci`` verification point pointing at *entry*."""
    vp = {
        "id": "VP-902",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


def _gate(
    tmp_path: Path,
    exit_code: int,
    verdict: dict | None = None,
    raw: str | None = None,
) -> Path:
    """A project whose gate writes a verdict file and exits *exit_code*.

    ``raw`` writes the file verbatim — that is how a gate writes a file
    this framework cannot read.
    """
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    body = "#!/bin/bash\n"
    if raw is not None or verdict is not None:
        payload = raw if raw is not None else json.dumps(verdict)
        body += (
            'cat > "$PDT_GATE_RESULT_DIR/pdt-gate-result.json" '
            "<<'PDT_GATE_JSON'\n" + payload + "\nPDT_GATE_JSON\n"
        )
    body += f"exit {exit_code}\n"
    (project / "scripts" / "run_tests.sh").write_text(
        body, encoding="utf-8",
    )
    return project


def _plan_dir(tmp_path: Path, **fields) -> Path:
    """A plan directory whose contract declares *fields*."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / CONTRACT_FILENAME).write_text(
        json.dumps(fields), encoding="utf-8",
    )
    return plan_dir


def _run(tmp_path: Path, project: Path, plan_dir: Path):
    """Grade the gate the way the framework does."""
    return run_ci_verification(
        _vp("bash scripts/run_tests.sh"),
        project_dir=project,
        plan_dir=plan_dir,
        artifact_dir=tmp_path / "artifacts",
    )


def _verdict(
    status: str, exit_code: int, stages: dict | None = None,
    failing_stage=None,
) -> dict:
    return {
        "schema": "pdt-gate/v1",
        "status": status,
        "failing_stage": failing_stage,
        "exit_code": exit_code,
        "stages": stages if stages is not None else {},
    }


# --------------------------------------------------------------------------
# The five ways a verdict is graded
# --------------------------------------------------------------------------


def test_a_passing_verdict_passes(tmp_path):
    """``status=pass`` and the process exited 0 -> PASSED."""
    project = _gate(tmp_path, 0, _verdict("pass", 0))
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_PASSED
    assert result.evidence["gate_result_available"] is True
    assert result.evidence["gate_result"]["status"] == "pass"


def test_a_failing_verdict_fails_and_names_the_stage(tmp_path):
    """``status=fail`` -> FAILED, and the reason names the stage.

    "门禁报告某条 lane 红" is the sentence this feature exists to
    replace: it names no stage and no error, so the reader has to go
    and find both.
    """
    stages = {
        "unit": {"status": "pass", "errors": []},
        "e2e": {
            "status": "fail",
            "errors": [
                "tests/e2e/test_x.py::test_y — AssertionError: 1 != 2",
                "tests/e2e/test_x.py::test_z — AssertionError: 3 != 4",
            ],
        },
    }
    project = _gate(tmp_path, 1, _verdict("fail", 1, stages, "e2e"))
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED
    reason = " ".join(result.reasons)
    assert "e2e" in reason
    assert "tests/e2e/test_x.py::test_y" in reason
    assert result.evidence["gate_result"]["failing_stage"] == "e2e"


def test_an_inconclusive_verdict_is_inconclusive(tmp_path):
    """``status=inconclusive`` -> INCONCLUSIVE, and it says so."""
    project = _gate(tmp_path, 3, _verdict("inconclusive", 3))
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_INCONCLUSIVE
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE


def test_a_verdict_that_contradicts_the_process_is_inconclusive(tmp_path):
    """The verdict says 0, the process exited 1 -> INCONCLUSIVE.

    A self-contradictory verdict is not evidence, it is noise — so the
    framework refuses to grade the code by it and says that the report
    and the process disagree.
    """
    project = _gate(tmp_path, 1, _verdict("pass", 0))
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_INCONCLUSIVE
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE
    reason = " ".join(result.reasons)
    assert "0" in reason and "1" in reason


def test_a_passing_verdict_with_a_failing_process_is_inconclusive(tmp_path):
    """``status=pass`` but the process exited 1 -> INCONCLUSIVE.

    The verdict says the run passed; the process says it ended in
    failure.  One of them is wrong, and the framework cannot tell which
    — so it does not grade the code.
    """
    project = _gate(tmp_path, 1, _verdict("pass", 1))
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_INCONCLUSIVE
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE


# --------------------------------------------------------------------------
# A plan that never declared the format
# --------------------------------------------------------------------------


def test_a_plan_without_the_format_does_not_read_the_file(tmp_path):
    """No declaration -> the file is not read, and a broken one is harmless.

    A gate that never promised a verdict file is not asked for one.  The
    file that happens to be lying around is not this round's verdict, so
    reading it would be reading somebody else's news.
    """
    project = _gate(tmp_path, 1, raw="{not json at all")
    plan_dir = _plan_dir(tmp_path, inconclusive_exit_codes=[])

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED
    assert "gate_result" not in result.evidence
    assert "gate_result_available" not in result.evidence


# --------------------------------------------------------------------------
# A verdict file that cannot be read
# --------------------------------------------------------------------------


def test_a_broken_verdict_file_leaves_the_exit_code_deciding(tmp_path):
    """A verdict that is not JSON -> the exit code decides, and says why.

    The verdict is an **addition** to the evidence, not a new way for a
    broken file to take the gate down.
    """
    project = _gate(tmp_path, 1, raw="{not json at all")
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED
    assert result.evidence["gate_result_available"] is False
    assert result.evidence["gate_result_error"]
    assert "gate_result" not in result.evidence


def test_a_missing_verdict_file_leaves_the_exit_code_deciding(tmp_path):
    """No verdict file at all -> the exit code decides, and says why."""
    project = _gate(tmp_path, 1)
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["gate_result_available"] is False
    assert result.evidence["gate_result_error"]


def test_a_verdict_with_an_unknown_schema_is_not_used(tmp_path):
    """A file that declares another schema is not this framework's verdict."""
    project = _gate(
        tmp_path, 1,
        {
            "schema": "something-else/v1",
            "status": "pass",
            "exit_code": 1,
            "stages": {},
        },
    )
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["gate_result_available"] is False
    assert result.evidence["gate_result_error"]


def test_a_verdict_with_a_bad_stage_is_not_used(tmp_path):
    """A stage with a status the framework does not know is not a verdict."""
    project = _gate(
        tmp_path, 1,
        {
            "schema": "pdt-gate/v1",
            "status": "fail",
            "failing_stage": "unit",
            "exit_code": 1,
            "stages": {"unit": {"status": "exploded"}},
        },
    )
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["gate_result_available"] is False
    assert result.evidence["gate_result_error"]


# --------------------------------------------------------------------------
# The contract that declares no inconclusive exit code
# --------------------------------------------------------------------------


def test_an_empty_inconclusive_code_list_makes_exit_three_a_failure(tmp_path):
    """``inconclusive_exit_codes: []`` -> exit 3 is a real failure.

    This is the accident in one test: a gate that declared "I have no
    'could not decide' exit code" was graded by the framework's default
    3, so a real failure was filed as "the machine could not decide" —
    and nothing was ever repaired.
    """
    project = _gate(tmp_path, 3)
    plan_dir = _plan_dir(
        tmp_path, result_format="pdt-gate/v1", inconclusive_exit_codes=[],
    )

    result = _run(tmp_path, project, plan_dir)

    assert result.status == STATUS_FAILED
    assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED
    assert result.evidence["ci_contract"]["inconclusive_exit_codes"] == []
