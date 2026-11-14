"""This repository's own CI entry point, ``scripts/ci_gate.sh``.

The framework does not look for this file -- which script is a project's
CI entry is the project's own decision, and the framework only runs the
entry the plan names and reads its exit code.  What the framework does
not do is parse the script, so the contract has to be pinned here
instead.

This repository already carries the implementation -- ``scripts/ci_gha.sh``
dispatches the ``ci.yml`` workflow and turns its conclusion into the
verdict (0 green / 1 red / 3 inconclusive). What was missing was the
uniform entry point this repository's own plans point at, so this module
pins the three properties that entry point has to keep:

* it exists and is executable,
* it is valid bash (a syntax error would only surface at gate time),
* it hands over to ``ci_gha.sh`` -- the file that owns the verdict and the
  exit codes.

On top of that this module pins the *verdict semantics* of
``scripts/ci_gha.sh``.  GitHub reports a run as ``failure`` even when the
only thing that went wrong was a single job that was cancelled without
ever being picked up by a runner (a macOS runner that ran out of capacity,
for example).  Such a job produced no evidence at all, so the gate must
not read that as "the code is broken" -- and it must not read it as green
either.  The script therefore decides from *job level* evidence and
exposes that decision through two pure functions, ``classify_run`` and
``decide_verdict``, which this module exercises offline (no network, no
CI run, no docker).

Every assertion here is static or a subprocess running ``bash``; no
network, no docker, no CI run is involved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: This repository's own CI entry point.
SCRIPT = PROJECT_ROOT / "scripts" / "ci_gate.sh"

#: The implementation the entry point delegates to.
CI_GHA = PROJECT_ROOT / "scripts" / "ci_gha.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"CI adapter entry point is missing: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"CI adapter entry point is not executable: {SCRIPT}"


def test_script_is_bash_syntax_valid():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n rejected {SCRIPT}: {result.stderr.strip()}"


def test_script_execs_ci_gha():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "exec bash" in content, "the adapter must hand over to the gate, not fork it"
    assert "ci_gha.sh" in content, "the adapter must delegate to scripts/ci_gha.sh"
    assert CI_GHA.is_file(), f"the delegated script is missing: {CI_GHA}"
    assert os.access(CI_GHA, os.X_OK), f"the delegated script is not executable: {CI_GHA}"


# ---------------------------------------------------------------------------
# the verdict semantics of scripts/ci_gha.sh
# ---------------------------------------------------------------------------


def _run_ci_gha(*args: str, env: dict | None = None) -> str:
    """Run ``scripts/ci_gha.sh`` in one of its offline modes.

    Returns the trimmed stdout.  The script is expected to exit 0 and to
    print exactly one word.
    """
    result = subprocess.run(
        ["bash", str(CI_GHA), *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, (
        f"ci_gha.sh {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}"
    )
    return result.stdout.strip()


def test_ci_gha_is_bash_syntax_valid():
    result = subprocess.run(
        ["bash", "-n", str(CI_GHA)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n rejected {CI_GHA}: {result.stderr.strip()}"


#: ``classify_run <run-conclusion> <failed-jobs> <cancelled-jobs>``.
#:
#: A run that failed with at least one genuinely failed job is RED.  A run
#: that failed while a job was *cancelled* -- a job that never ran, because
#: no runner picked it up -- is INFRA: neither green nor red, it is an
#: infrastructure problem that can be retried.  A failed run whose shape
#: is unknown (no failed and no cancelled job explains it) stays RED, so
#: the gate never turns a failure into a pass by accident.
#:
#: The function classifies the *run conclusion*; it is the gate that
#: additionally refuses to pass a run that still carries a cancelled job
#: (see ``test_ci_gha_reads_job_level_evidence`` and the script itself) --
#: a cancelled job never produced evidence, so it can never make the gate
#: green.
CLASSIFY_CASES = [
    ("success", 0, 0, "GREEN"),
    ("success", 0, 3, "GREEN"),
    ("failure", 1, 0, "RED"),
    ("failure", 3, 2, "RED"),
    ("failure", 0, 1, "INFRA"),
    ("timed_out", 0, 1, "INFRA"),
    ("action_required", 0, 2, "INFRA"),
    ("failure", 0, 0, "RED"),
    ("cancelled", 0, 1, "UNKNOWN"),
    ("skipped", 0, 0, "UNKNOWN"),
    ("", 0, 0, "UNKNOWN"),
    # A count that is not a number counts as zero.
    ("failure", "x", "y", "RED"),
    ("failure", 0, "abc", "RED"),
]


def test_classify_run_verdicts():
    for conclusion, failed, cancelled, expected in CLASSIFY_CASES:
        actual = _run_ci_gha("--classify", str(conclusion), str(failed), str(cancelled))
        assert actual == expected, (
            f"classify_run({conclusion!r}, {failed!r}, {cancelled!r}) "
            f"returned {actual!r}, expected {expected!r}"
        )


#: ``decide_verdict <verdict> <reruns-done> <max-reruns> <remaining-seconds>``.
#:
#: Only an infrastructure verdict may be retried, only while the retry
#: budget is left, and only while enough of the wall clock budget is left
#: for the retry to have a chance.  Everything else ends in INCONCLUSIVE --
#: never in a pass.
DECIDE_CASES = [
    ("GREEN", 0, 1, 1000, "GREEN"),
    ("GREEN", 3, 1, 1000, "GREEN"),
    ("RED", 0, 1, 1000, "RED"),
    ("RED", 0, 1, 0, "RED"),
    ("INFRA", 0, 1, 1000, "RERUN"),
    ("INFRA", 0, 1, 600, "RERUN"),
    ("INFRA", 0, 1, 599, "INCONCLUSIVE"),
    ("INFRA", 1, 1, 1000, "INCONCLUSIVE"),
    ("INFRA", 0, 0, 1000, "INCONCLUSIVE"),
    ("UNKNOWN", 0, 1, 1000, "INCONCLUSIVE"),
    ("", 0, 1, 1000, "INCONCLUSIVE"),
]


def test_decide_verdict_outcomes():
    for verdict, done, maximum, remaining, expected in DECIDE_CASES:
        actual = _run_ci_gha(
            "--decide", str(verdict), str(done), str(maximum), str(remaining)
        )
        assert actual == expected, (
            f"decide_verdict({verdict!r}, {done!r}, {maximum!r}, {remaining!r}) "
            f"returned {actual!r}, expected {expected!r}"
        )


def test_offline_verdict_modes_need_neither_git_nor_gh():
    """The offline modes must be reachable without git, gh or a repository.

    The verdict logic is pure, so it must be decidable before the script
    touches git or the network.  Running it with an empty PATH proves it:
    if the entry point were placed after the git/gh calls, the script would
    die with "not inside a git repository" instead of answering.
    """
    bash = shutil.which("bash")
    assert bash, "bash is required to run the offline verdict modes"
    result = subprocess.run(
        [bash, str(CI_GHA), "--classify", "failure", "0", "1"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        env={"PATH": "/nonexistent", "HOME": str(PROJECT_ROOT)},
    )
    assert result.returncode == 0, (
        f"the offline mode needs git or gh: exit {result.returncode}, "
        f"stderr={result.stderr.strip()}"
    )
    assert result.stdout.strip() == "INFRA"


def test_ci_gha_reads_job_level_evidence():
    """The verdict must be based on the jobs, not on the run conclusion alone."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert "--json jobs" in content, (
        "the gate must read the job list of the run, not only the run conclusion"
    )


def test_ci_gha_retries_cancelled_jobs():
    """An infrastructure cancellation is retried, not turned into a pass."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert "rerun" in content, "the gate must retry the cancelled jobs"
    assert "--job" in content, "the retry must address the cancelled job itself"