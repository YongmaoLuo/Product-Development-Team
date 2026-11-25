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

The verdict itself needs a second pin, and it is the interesting one.
``scripts/ci_gha.sh`` used to read the *run-level* conclusion only, so a
run whose only failing job never reached a runner -- GitHub's hosted
runner pool was out of capacity -- came back as "red" (exit 1).  That
contradicts the script's own header, which promises exit 3 for
"environment, queueing, network or timeout -- not a code fault", and it
punishes the code for the machine's mood.

The classification now lives in ``scripts/ci_gha_verdict.sh``: a library
of pure functions, sourced by the gate, that read the run's job list and
say whether the red verdict is a *code* failure or an *infrastructure*
failure.  A job GitHub never handed to a runner has zero steps, because
no step ever ran; that is the whole signal.

The same decision is also available in the shape the run conclusion
allows: the pure functions ``classify_run`` and ``decide_verdict`` answer
GREEN / RED / INFRA / UNKNOWN and GREEN / RED / RERUN / INCONCLUSIVE from
the run conclusion and the job counts.  GitHub reports a run as
``failure`` even when the only thing that went wrong was a single job
that was cancelled without ever being picked up by a runner (a macOS
runner that ran out of capacity, for example).  Such a job produced no
evidence at all, so the gate must not read that as "the code is broken"
-- and it must not read it as green either.  Both functions are reachable
offline, through the ``--classify`` and ``--decide`` modes, so the offline
tests can drive them without a repository, without the gh CLI and without
a network.

What this module pins about that split:

* the verdict library is sourceable and runs nothing while being sourced
  -- that is what makes the classification testable offline;
* the classification itself: clean run -> passed, never-started job ->
  infrastructure, a lane that ran and failed -> code;
* the gate's answer to each of those, driven through a fake ``gh`` so
  the whole path runs without a network: infrastructure gets exactly one
  bounded rerun of the failed jobs, a lane that really ran and failed is
  still red, and a run that never got a runner is inconclusive;
* the run-conclusion verdict functions and the offline entry points that
  reach them.

Every assertion here is static, a subprocess running ``bash -n``, or a
subprocess driving the gate against fake binaries; no network, no
docker, no CI run is involved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The shell the bash subprocesses below run.  Resolved once, because one
#: of the tests empties ``PATH`` on purpose.
BASH = shutil.which("bash") or "/bin/bash"

#: This repository's own CI entry point.
SCRIPT = PROJECT_ROOT / "scripts" / "ci_gate.sh"

#: The implementation the entry point delegates to.
CI_GHA = PROJECT_ROOT / "scripts" / "ci_gha.sh"

#: The verdict library the gate sources.
VERDICT = PROJECT_ROOT / "scripts" / "ci_gha_verdict.sh"


# ---------------------------------------------------------------------------
# 1. the entry point
# ---------------------------------------------------------------------------


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"CI adapter entry point is missing: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"CI adapter entry point is not executable: {SCRIPT}"


def test_script_is_bash_syntax_valid():
    result = subprocess.run(
        [BASH, "-n", str(SCRIPT)],
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
# 2. the verdict library is sourceable and inert
# ---------------------------------------------------------------------------

#: A bash function definition, as this repository writes them.
_FUNCTION_HEADER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(\)\s*\{")


def _child_env(**extra: str) -> dict[str, str]:
    """Environment for the bash subprocesses below.

    ``PDT_CI_GHA_PYTHON`` pins the interpreter the verdict library uses
    to the one running the tests, so the classification never depends on
    what happens to be on ``PATH``.
    """
    env = dict(os.environ)
    env["PDT_CI_GHA_PYTHON"] = sys.executable
    env.update(extra)
    return env


def _top_level_command_lines(text: str) -> list[tuple[int, str]]:
    """Return the lines of a shell file that run a command at top level.

    A sourced library may hold function definitions and nothing else:
    every other top-level statement would run the moment the file is
    sourced.  The scan tracks function bodies by their closing brace at
    column 0, which is how this repository writes them.
    """
    findings: list[tuple[int, str]] = []
    inside = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if inside:
            if line == "}":
                inside = False
            continue
        if not line or line.startswith("#"):
            continue
        if _FUNCTION_HEADER_RE.match(line):
            inside = True
            continue
        findings.append((lineno, line))
    return findings


def test_verdict_helper_is_sourced_without_running_anything():
    """Sourcing the library must define the functions and print nothing.

    A library that ran something on the way in would make every caller
    of it (and every test that sources it) depend on that side effect.
    """
    result = subprocess.run(
        [
            BASH,
            "-c",
            'set -euo pipefail; . "$1"; '
            "type -t ci_gha_classify_jobs >/dev/null; "
            "type -t ci_gha_describe_jobs >/dev/null",
            "bash",
            str(VERDICT),
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        f"sourcing {VERDICT} failed: {result.stderr.strip()}"
    )
    assert result.stdout == "", f"sourcing {VERDICT} printed: {result.stdout!r}"
    assert result.stderr == "", f"sourcing {VERDICT} wrote: {result.stderr!r}"


def test_verdict_helper_survives_a_path_without_any_tool():
    """Sourcing must not need a single external command.

    With an empty ``PATH`` any top-level command would fail, so a clean
    sourcing proves the file holds definitions only.
    """
    result = subprocess.run(
        [BASH, "-c", 'set -euo pipefail; . "$1"', "bash", str(VERDICT)],
        capture_output=True,
        text=True,
        env=_child_env(PATH=str(PROJECT_ROOT / "no-such-bin")),
    )
    assert result.returncode == 0, (
        f"sourcing {VERDICT} needed a tool: {result.stderr.strip()}"
    )


def test_verdict_helper_top_level_holds_only_function_definitions():
    """Nothing but function definitions may live at the top level."""
    content = VERDICT.read_text(encoding="utf-8")
    findings = _top_level_command_lines(content)
    assert findings == [], (
        "the verdict library must hold function definitions only, "
        f"but these top-level statements would run on source: {findings}"
    )


# ---------------------------------------------------------------------------
# 3. the classification
# ---------------------------------------------------------------------------


def _classify(payload: object) -> str:
    """Run the pure classifier over a job payload and return its verdict."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        [
            BASH,
            "-c",
            'set -euo pipefail; . "$1"; ci_gha_classify_jobs "$2"',
            "bash",
            str(VERDICT),
            text,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        f"the classifier failed: {result.stderr.strip()}"
    )
    return result.stdout.strip()


def _job(name: str, conclusion: str | None, steps: int) -> dict:
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "steps": [{"name": f"step {index}"} for index in range(steps)],
    }


def test_classify_reports_a_clean_run_as_passed():
    jobs = {
        "jobs": [
            _job("Unit tests (unit-00)", "success", 13),
            _job("Nightly regression", "skipped", 0),
            _job("Merge gate", "neutral", 0),
        ]
    }
    assert _classify(jobs) == "passed"


def test_classify_reports_a_never_started_job_as_infrastructure():
    """A cancelled job with zero steps never reached a runner."""
    jobs = {
        "jobs": [
            _job("Unit tests (unit-00)", "success", 13),
            _job("Keychain E2E (macOS runner, real keychain path)", "cancelled", 0),
        ]
    }
    assert _classify(jobs) == "infrastructure"


def test_classify_reports_a_ran_and_failed_job_as_code():
    """A job that executed steps and failed is a lane that went red."""
    jobs = {
        "jobs": [
            _job("Unit tests (unit-00)", "success", 13),
            _job("Static gates (contracts)", "failure", 10),
        ]
    }
    assert _classify(jobs) == "code"


def test_classify_reports_a_mixed_job_list_as_code():
    """One lane that really ran is enough to keep the gate red."""
    jobs = {
        "jobs": [
            _job("Keychain E2E (macOS runner, real keychain path)", "cancelled", 0),
            _job("Static gates (contracts)", "failure", 10),
        ]
    }
    assert _classify(jobs) == "code"


def test_classify_reports_an_unreadable_payload_as_unknown():
    """An unreadable job list must not be mistaken for a green run."""
    assert _classify("not json at all") == "unknown"
    assert _classify({"no_jobs_key": []}) == "unknown"


def test_classify_accepts_the_gh_run_view_payload_shape():
    """``gh`` answers with a wrapper; a bare job list reads the same."""
    jobs = [_job("Keychain E2E (macOS runner, real keychain path)", "cancelled", 0)]
    assert _classify({"jobs": jobs}) == "infrastructure"
    assert _classify(jobs) == "infrastructure"


# ---------------------------------------------------------------------------
# 4. the gate's answer, driven through a fake ``gh``
# ---------------------------------------------------------------------------

#: A fake ``git``: the gate only asks it for the repository root, the
#: branch and the short SHA, and pushes a probe branch it later drops.
FAKE_GIT = """#!/usr/bin/env bash
case "${1:-}" in
    rev-parse)
        case "${2:-}" in
            --show-toplevel) printf '%s\\n' "${FAKE_GIT_ROOT}" ;;
            --abbrev-ref) printf 'probe-branch\\n' ;;
            *) printf 'deadbeef\\n' ;;
        esac
        ;;
    push)
        ;;
esac
exit 0
"""

#: A fake ``gh``: it answers the gate's run queries out of a JSON state
#: file, and counts the re-runs it was asked for.
FAKE_GH = '''#!/usr/bin/env python3
"""Fake ``gh`` CLI for the CI adapter's end-to-end tests."""
import json
import os
import sys

STATE = os.environ["FAKE_GH_STATE"]


def load():
    with open(STATE, encoding="utf-8") as handle:
        return json.load(handle)


def save(state):
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def main():
    argv = sys.argv[1:]
    state = load()
    if not argv:
        return 0
    if argv[0] == "workflow":
        return 0
    if argv[0] != "run" or len(argv) < 2:
        return 0

    sub = argv[1]
    if sub == "list":
        print(state["run_id"])
        return 0
    if sub == "view":
        fields = ""
        if "--json" in argv:
            fields = argv[argv.index("--json") + 1]
        attempt = 1 + int(state.get("reruns", 0))
        entry = state["attempts"][str(attempt)]
        if "jobs" in fields.split(","):
            print(json.dumps({"jobs": entry["jobs"]}))
            return 0
        values = []
        for field in fields.split(","):
            if field == "status":
                values.append(entry["status"])
            elif field == "conclusion":
                values.append(entry.get("conclusion") or "")
            elif field == "attempt":
                values.append(str(attempt))
            else:
                values.append("")
        print(" ".join(values))
        return 0
    if sub == "rerun":
        state["reruns"] = int(state.get("reruns", 0)) + 1
        save(state)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _run_gate(tmp_path: Path, attempts: dict[str, dict]) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the gate against fake ``git`` / ``gh`` binaries.

    ``attempts`` maps the attempt number to the run state of that
    attempt.  The returned dictionary is the fake's state after the run,
    so the test can see how many re-runs the gate asked for.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    state_path = tmp_path / "gh-state.json"
    state = {"run_id": "424242", "reruns": 0, "attempts": attempts}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    git_path = bin_dir / "git"
    git_path.write_text(FAKE_GIT, encoding="utf-8")
    git_path.chmod(0o755)
    gh_path = bin_dir / "gh"
    gh_path.write_text(FAKE_GH, encoding="utf-8")
    gh_path.chmod(0o755)

    env = _child_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GIT_ROOT"] = str(repo)
    env["FAKE_GH_STATE"] = str(state_path)

    result = subprocess.run(
        [BASH, str(CI_GHA)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
        timeout=300,
    )
    return result, json.loads(state_path.read_text(encoding="utf-8"))


def _attempt(conclusion: str, jobs: list[dict]) -> dict:
    return {"status": "completed", "conclusion": conclusion, "jobs": jobs}


def test_gate_answers_red_when_a_lane_ran_and_failed(tmp_path):
    """A lane that executed and failed stays red -- and is not re-run."""
    attempts = {
        "1": _attempt(
            "failure",
            [
                _job("Unit tests (unit-00)", "success", 13),
                _job("Static gates (contracts)", "failure", 10),
            ],
        )
    }
    result, state = _run_gate(tmp_path, attempts)
    assert result.returncode == 1, (
        f"the gate must stay red, got {result.returncode}: {result.stdout}{result.stderr}"
    )
    assert "gate: FAILED (exit 1)" in result.stdout
    assert state["reruns"] == 0, "a lane that really failed must not be re-run"
    # The exit code says *that* the gate is red; the job list says *which*
    # lane is red.  A red verdict that names no job leaves the run's
    # artifact without locatable evidence, so the red branch has to print
    # the failing job(s) before it emits the verdict.
    assert "ci_gha: failing job(s) of run 424242:" in result.stdout, (
        "the red verdict must name the failing job(s): " + result.stdout
    )
    assert "Static gates (contracts) [failure, 10 steps]" in result.stdout, (
        "the red verdict must name the failing lane: " + result.stdout
    )
    assert result.stdout.index("ci_gha: failing job(s) of run 424242:") < result.stdout.index(
        "gate: FAILED (exit 1)"
    ), "the failing job(s) must be named before the verdict is emitted"


def test_gate_does_not_name_failing_jobs_when_it_answers_green(tmp_path):
    """A green gate names no failing job -- there is none."""
    attempts = {
        "1": _attempt(
            "success",
            [
                _job("Unit tests (unit-00)", "success", 13),
                _job("Static gates (contracts)", "success", 10),
            ],
        )
    }
    result, state = _run_gate(tmp_path, attempts)
    assert result.returncode == 0, (
        f"the gate must be green, got {result.returncode}: {result.stdout}{result.stderr}"
    )
    assert "gate: PASSED (exit 0)" in result.stdout
    assert "failing job(s)" not in result.stdout, (
        "a green gate must not report failing jobs: " + result.stdout
    )
    assert state["reruns"] == 0, "a green run needs no re-run"


def test_gate_answers_green_after_the_rerun_passes(tmp_path):
    """A run that only lost its runner gets one bounded re-run."""
    attempts = {
        "1": _attempt(
            "failure",
            [
                _job("Unit tests (unit-00)", "success", 13),
                _job("Keychain E2E (macOS runner, real keychain path)", "cancelled", 0),
            ],
        ),
        "2": _attempt(
            "success",
            [
                _job("Unit tests (unit-00)", "success", 13),
                _job("Keychain E2E (macOS runner, real keychain path)", "success", 9),
            ],
        ),
    }
    result, state = _run_gate(tmp_path, attempts)
    assert result.returncode == 0, (
        f"the gate must be green after the re-run, got {result.returncode}: "
        f"{result.stdout}{result.stderr}"
    )
    assert "gate: PASSED (exit 0)" in result.stdout
    assert state["reruns"] == 1, "exactly one bounded re-run, no more"


def test_gate_answers_inconclusive_when_no_job_ever_started(tmp_path):
    """No runner for the failing job twice: the machine could not answer."""
    never_started = _job(
        "Keychain E2E (macOS runner, real keychain path)", "cancelled", 0
    )
    attempts = {
        "1": _attempt("failure", [_job("Unit tests (unit-00)", "success", 13), never_started]),
        "2": _attempt("failure", [_job("Unit tests (unit-00)", "success", 13), never_started]),
    }
    result, state = _run_gate(tmp_path, attempts)
    assert result.returncode == 3, (
        f"the gate must be inconclusive, got {result.returncode}: "
        f"{result.stdout}{result.stderr}"
    )
    assert "gate: INCONCLUSIVE (exit 3)" in result.stdout
    assert state["reruns"] == 1, "the re-run happens once, then the gate gives up"


# ---------------------------------------------------------------------------
# 5. the gate keeps the contract it declares
# ---------------------------------------------------------------------------


def test_ci_gha_pulls_in_the_verdict_helper():
    """The gate has to read the classification from the library."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert "ci_gha_verdict.sh" in content, "the gate must source the verdict library"
    assert "ci_gha_classify_jobs" in content, "the gate must use the classification"


def test_ci_gha_keeps_the_three_exit_codes():
    """The verdict is still the exit code, and the three still mean what they mean."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert 'emit_gate "PASSED" "0"' in content
    assert 'emit_gate "FAILED" "1"' in content
    assert 'emit_gate "INCONCLUSIVE" "3"' in content


def test_ci_gha_offers_one_bounded_rerun_for_infrastructure_failures():
    """The re-run is bounded, configurable and separate from the main timeout."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert "gh run rerun" in content, "the gate must re-run the failed jobs once"
    assert "--failed" in content, "the re-run must be limited to the failed jobs"
    assert "PDT_CI_GHA_RERUN_TIMEOUT" in content, "the re-run needs its own budget"
    assert "PDT_CI_GHA_TIMEOUT" in content, "the main wait keeps its own budget"


# ---------------------------------------------------------------------------
# 6. the verdict semantics of scripts/ci_gha.sh
# ---------------------------------------------------------------------------


def test_ci_gha_is_bash_syntax_valid():
    result = subprocess.run(
        ["bash", "-n", str(CI_GHA)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n rejected {CI_GHA}: {result.stderr.strip()}"


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
