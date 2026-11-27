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

On top of that this module pins what the gate *publishes*.  The gate
runs on a real CI runner by pushing a throw-away probe branch, so the
tree under test has to reach GitHub somehow -- but only the tree may
travel.  Pushing ``HEAD`` would publish the whole local history of the
checkout, including the historical snapshots that the commit-range
privacy scan exists to reject, and it would break the repository's own
rule that a push to the public remote carries one or two commits.  So
the probe branch carries a synthetic commit instead: ``HEAD``'s tree,
byte for byte, with a single parent.  The module pins that shape both
offline (``--probe-commit``) and end to end, against a local bare
remote -- no network, no docker, no CI run is involved.

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

#: A fake ``git``: the gate asks it for the repository root, the branch,
#: the short SHA, the tree of HEAD and the remote-tracking base commit,
#: builds the probe commit with ``commit-tree`` and pushes the probe
#: branch it later drops.  The two log files record what the gate
#: actually asked for, so a test can read the refspec off the push
#: instead of guessing it.
FAKE_GIT = """#!/usr/bin/env bash
case "${1:-}" in
    rev-parse)
        # The gate asks for several different things through rev-parse and
        # the flags sit in front of the argument (``--verify --quiet
        # origin/main^{commit}``), so every argument is looked at.
        for arg in "$@"; do
            case "${arg}" in
                --show-toplevel) printf '%s\\n' "${FAKE_GIT_ROOT}"; exit 0 ;;
                --abbrev-ref) printf 'probe-branch\\n'; exit 0 ;;
                'HEAD^{tree}') printf 'faketree0001\\n'; exit 0 ;;
                'origin/main^{commit}') printf 'basecommit0001\\n'; exit 0 ;;
                'origin/HEAD^{commit}') printf 'basecommit0001\\n'; exit 0 ;;
            esac
        done
        printf 'deadbeef\\n'
        ;;
    commit-tree)
        printf '%s\\n' "$*" >> "${FAKE_GIT_TREE_LOG}"
        printf 'probecommit0001\\n'
        ;;
    push)
        printf '%s\\n' "$*" >> "${FAKE_GIT_PUSH_LOG}"
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

    #: What the fake ``git`` was asked to push, one line per push.
    push_log = tmp_path / "git-push.log"
    #: What the fake ``git`` was asked to build, one line per commit-tree.
    tree_log = tmp_path / "git-tree.log"

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
    env["FAKE_GIT_PUSH_LOG"] = str(push_log)
    env["FAKE_GIT_TREE_LOG"] = str(tree_log)

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


# ---------------------------------------------------------------------------
# what the gate publishes: the tree under test, not the local history
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in ``repo`` and return its trimmed stdout."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in {repo} (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )
    return result.stdout.strip()


def _git_rc(repo: Path, *args: str) -> int:
    """Run ``git`` in ``repo`` and return its exit code."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=60,
    ).returncode


def _make_repo_with_history(repo: Path) -> tuple[str, str]:
    """Create a repository with two commits; return ``(base_sha, head_sha)``.

    The second commit is the work in progress: it is what the gate is
    asked to check, and it is the commit that must *not* travel to the
    remote.
    """
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    # The hooks directory is named explicitly: a machine that points
    # ``core.hooksPath`` somewhere else would otherwise run a hook this
    # test did not install.
    _git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))
    identity = (
        "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false",
    )
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *identity, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *identity, "commit", "-qm", "wip")
    head = _git(repo, "rev-parse", "HEAD")
    return base, head


def test_probe_commit_carries_the_head_tree_and_one_parent(tmp_path):
    """``--probe-commit`` builds the commit the probe branch publishes.

    The probe has to carry the tree under test and nothing else: one
    commit, one parent, and none of the local work-in-progress history.
    The mode is offline -- it touches no remote and needs no ``gh``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    base, head = _make_repo_with_history(repo)

    result = subprocess.run(
        ["bash", str(CI_GHA), "--probe-commit", base],
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"--probe-commit exited {result.returncode}: {result.stderr.strip()}"
    )
    probe = result.stdout.strip()
    assert len(probe) == 40 and set(probe) <= set("0123456789abcdef"), (
        f"--probe-commit did not print a commit SHA: {probe!r}"
    )

    # the tree under test travels unchanged
    assert _git(repo, "rev-parse", f"{probe}^{{tree}}") == _git(
        repo, "rev-parse", "HEAD^{tree}"
    )
    # the probe has exactly one parent: the base that was named
    assert _git(repo, "rev-parse", f"{probe}^") == base
    # it publishes exactly one commit ...
    assert _git(repo, "rev-list", "--count", probe, "--not", base) == "1"
    # ... and the work-in-progress commit does not travel with it
    assert probe != head
    assert _git_rc(repo, "merge-base", "--is-ancestor", head, probe) == 1


#: A stand-in for the ``gh`` CLI: enough of it for the gate to reach a
#: verdict without a network.  ``gh workflow run`` succeeds, ``gh run
#: list`` reports one run, and ``gh run view`` reports that run as
#: completed and successful.
#:
#: 2026-11-24（合入 origin/main@deb9b009）：这个名字原先与 base 侧的同名
#: 常量撞车 —— base 侧也有一份 ``FAKE_GH``（Python 写的、按状态文件驱动，
#: 用来把「一条 lane 真跑了并失败」「一个 job 从未被 runner 领走」这些
#: 情形喂给 gate）。两侧各加各的，Git 的文本合并看不见「同名顶层常量」
#: 这种冲突，于是合并结果里出现了**两个** ``FAKE_GH``：后定义的那份（就是
#: 这一份）会把前一份整个遮住，base 侧的三条 gate 用例因此读到的是这个
#: 只知道「completed success」的替身，而不是它们自己那份能演失败情形的
#: 替身 —— 判据被悄悄换掉了（合并前 base 侧 24 条全绿，合并后 3 条红）。
#: 两侧的意图都保留，但名字必须分开：base 那份仍叫 ``FAKE_GH``（它定义在
#: 前面，base 侧的代码一个字未动），这一份改名 ``FAKE_GH_PROBE`` —— 它只
#: 服务于下面那条「推的是 commit、不是历史」的用例，而那条用例根本不看
#: CI 判决，只要 gate 跑完并给出结论即可。
FAKE_GH_PROBE = """#!/usr/bin/env bash
case "${1:-}" in
    workflow)
        exit 0
        ;;
    run)
        case "${2:-}" in
            list)
                echo "424242"
                exit 0
                ;;
            view)
                case "$*" in
                    *"--json jobs"*) exit 0 ;;
                    *"--json status,conclusion"*) echo "completed success"; exit 0 ;;
                esac
                exit 0
                ;;
        esac
        exit 0
        ;;
esac
exit 0
"""


def test_ci_gha_pushes_the_commit_not_the_history(tmp_path):
    """The probe branch must publish the tree, not the local history.

    The gate gets the tree under test onto a CI runner by pushing a
    throw-away branch.  What that push publishes is the whole point: it
    must be ``HEAD``'s tree on a single synthetic commit, never the local
    history that sits behind ``HEAD``.  This runs the real script against
    a local bare remote -- no network -- and reads back what the push
    actually published.
    """
    work = tmp_path / "work"
    bare = tmp_path / "bare.git"
    work.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    base, head = _make_repo_with_history(work)
    _git(work, "remote", "add", "origin", str(bare))
    # ``origin/main`` exists and is an ancestor of HEAD, so the gate finds
    # a merge base and the probe gets a parent.
    _git(work, "push", "-q", "origin", f"{base}:refs/heads/main")
    _git(work, "fetch", "-q", "origin")

    # The pre-push hook records what the push publishes.  It is installed
    # after the setup push, so the log holds the gate's push and nothing
    # else.
    push_log = tmp_path / "push-refs.txt"
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_text(
        '#!/usr/bin/env bash\ncat >> "$PDT_PROBE_PUSH_LOG"\n', encoding="utf-8"
    )
    hook.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_gh = bindir / "gh"
    fake_gh.write_text(FAKE_GH_PROBE, encoding="utf-8")
    fake_gh.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["PDT_PROBE_PUSH_LOG"] = str(push_log)

    result = subprocess.run(
        ["bash", str(CI_GHA)],
        capture_output=True,
        text=True,
        cwd=str(work),
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"ci_gha.sh exited {result.returncode}: {result.stderr.strip()}"
    )
    assert "gate: PASSED" in result.stdout, result.stdout

    lines = [
        line
        for line in push_log.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("(delete)")
    ]
    assert len(lines) == 1, f"expected exactly one push, got {lines!r}"
    fields = lines[0].split()
    pushed = fields[1]
    remote_ref = fields[2]

    assert remote_ref.startswith("refs/heads/ci-probe/"), remote_ref
    # the published commit is not HEAD: the local history does not travel
    assert pushed != head
    # the tree under test does travel, unchanged
    assert _git(bare, "rev-parse", f"{pushed}^{{tree}}") == _git(
        work, "rev-parse", "HEAD^{tree}"
    )
    # and it is a single commit on top of the merge base
    assert _git(bare, "rev-parse", f"{pushed}^") == base
    assert _git(bare, "rev-list", "--count", pushed, "--not", base) == "1"
    # the work-in-progress commit itself was never published
    assert _git_rc(bare, "cat-file", "-e", f"{head}^{{commit}}") != 0
# ---------------------------------------------------------------------------
# 7. the probe branch carries the tree, not the branch history
# ---------------------------------------------------------------------------
#
# The gate judges the *current tree*.  It used to push HEAD itself, which
# meant the pre-push hook -- the one that scans every commit of the
# pushed ref that is not on a remote yet -- had to scan the whole local
# branch history, and a gate run could be blocked by an old commit that
# the current tree no longer contains.
#
# So the probe branch now points at a single commit built from HEAD's
# tree with ``git commit-tree``, and that commit hangs off a parent
# that is already on the remote.  The branch history is not published,
# every lane runs on the same tree, and the history checks stay where
# they belong: the pre-push hook for the real pushes of a branch, and
# ci.yml's ``commit-range-privacy`` job for pull requests (the gate
# dispatches ``workflow_dispatch``, which never runs that job).
#
# What is pinned here:
#
#   * the push target is a commit-tree commit, not HEAD;
#   * that commit is built from the tree of HEAD and hangs off a parent
#     that is already on the remote (``origin/main``, with
#     ``origin/HEAD`` as the fallback);
#   * the probe branch name is still ``ci-probe/<branch_slug>-<sha8>``;
#   * the red path still leaves the gate through ``emit_gate "FAILED" "1"``
#     (the verdict itself is pinned by the tests of section 4 above).
#
# Why the parent matters: for a ref that is new on the remote the
# pre-push hook scans ``<probe> --not --remotes``, so a parent that is
# reachable from any ``refs/remotes/*`` is excluded and the scan covers
# the probe commit alone -- the net change of the current tree against
# the base.  A *parentless* probe commit is a root commit, and then the
# whole tree reads as added lines: gitleaks hits the test fixtures main
# has been publishing for a long time (14 findings, measured) and the
# hook rejects the push -- the gate could never push its probe branch.


def test_gate_pushes_a_probe_commit_built_from_the_current_tree(tmp_path):
    """The probe branch is a tree-only commit, not the branch history."""
    attempts = {
        "1": _attempt("success", [_job("Unit tests (unit-00)", "success", 13)])
    }
    result, _ = _run_gate(tmp_path, attempts)
    assert result.returncode == 0, (
        f"the gate must be green, got {result.returncode}: {result.stdout}{result.stderr}"
    )

    # The probe commit is built from the tree of HEAD, once.
    tree_calls = (tmp_path / "git-tree.log").read_text(encoding="utf-8").splitlines()
    assert len(tree_calls) == 1, (
        f"the gate must build exactly one probe commit, got {tree_calls}"
    )
    assert tree_calls[0].startswith("commit-tree faketree0001 "), (
        "the probe commit must be built from the tree of HEAD: " + tree_calls[0]
    )

    # And that commit -- not HEAD -- is what the probe branch is pushed to.
    pushes = (tmp_path / "git-push.log").read_text(encoding="utf-8").splitlines()
    creating = [line for line in pushes if "refs/heads/ci-probe/" in line]
    assert len(creating) == 1, f"the gate must push the probe branch once: {pushes}"
    assert "probecommit0001:refs/heads/ci-probe/probe-branch-deadbeef" in creating[0], (
        "the probe branch must be pushed from the tree-only commit: " + creating[0]
    )
    assert "HEAD:refs/heads" not in "\n".join(pushes), (
        f"the probe branch must not be pushed from HEAD: {pushes}"
    )


def test_probe_commit_hangs_off_the_remote_base_commit(tmp_path):
    """The probe commit has the remote base as its parent.

    A parentless probe commit is a root commit, and the hook's scan of
    it reads the whole tree as added lines -- which hits the fixtures
    main has been publishing for a long time and blocks the push.
    """
    attempts = {
        "1": _attempt("success", [_job("Unit tests (unit-00)", "success", 13)])
    }
    result, _ = _run_gate(tmp_path, attempts)
    assert result.returncode == 0, (
        f"the gate must be green, got {result.returncode}: {result.stdout}{result.stderr}"
    )

    tree_calls = (tmp_path / "git-tree.log").read_text(encoding="utf-8").splitlines()
    assert len(tree_calls) == 1, (
        f"the gate must build exactly one probe commit, got {tree_calls}"
    )
    probe_call = tree_calls[0]
    assert " -p basecommit0001 " in probe_call, (
        "the probe commit must hang off the remote base commit: " + probe_call
    )
    # The parent is the remote-tracking base, never HEAD: a HEAD parent
    # would put the whole local branch history back into the scan range.
    assert "-p deadbeef" not in probe_call, (
        "the probe commit must not hang off HEAD: " + probe_call
    )


def test_probe_branch_name_is_unchanged(tmp_path):
    """The probe branch is still ``ci-probe/<branch_slug>-<sha8>``."""
    attempts = {
        "1": _attempt("success", [_job("Unit tests (unit-00)", "success", 13)])
    }
    result, _ = _run_gate(tmp_path, attempts)
    assert result.returncode == 0, (
        f"the gate must be green, got {result.returncode}: {result.stdout}{result.stderr}"
    )
    assert "ci_gha: probe branch=ci-probe/probe-branch-deadbeef" in result.stdout, (
        "the probe branch name must stay ci-probe/<branch_slug>-<sha8>: " + result.stdout
    )
    pushes = (tmp_path / "git-push.log").read_text(encoding="utf-8").splitlines()
    assert any("ci-probe/probe-branch-deadbeef" in line for line in pushes), (
        f"the probe branch must be pushed under its ci-probe name: {pushes}"
    )


def test_ci_gha_builds_the_probe_commit_with_commit_tree():
    """The probe commit comes from ``git commit-tree``, parented on the base."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert "commit-tree" in content, "the probe commit must be built with git commit-tree"
    assert '"HEAD:refs/heads/${probe}"' not in content, (
        "the probe branch must not be pushed from HEAD"
    )
    # The parent is the remote-tracking base, resolved before the commit
    # is built, and HEAD is never used as the parent.
    assert 'origin/main^{commit}' in content, (
        "the probe commit must hang off the remote base commit"
    )
    assert 'origin/HEAD^{commit}' in content, (
        "the probe commit needs a fallback when origin/main is missing"
    )
    for line in content.splitlines():
        stripped = line.strip()
        if "commit-tree" in stripped and not stripped.startswith("#"):
            assert '-p "${probe_parent}"' in stripped, (
                "the probe commit must be built with the base as its parent: " + stripped
            )


def test_ci_gha_fails_inconclusive_when_the_base_commit_is_missing():
    """No remote-tracking base -> the gate says inconclusive, it does not push."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert (
        'die_inconclusive "cannot resolve a remote-tracking base commit for the probe"'
        in content
    ), "the gate must refuse to build a parentless probe commit"


def test_ci_gha_still_drops_the_probe_branch_when_it_fails():
    """The failure path is unchanged: the gate still goes red with exit 1."""
    content = CI_GHA.read_text(encoding="utf-8")
    assert 'emit_gate "FAILED" "1"' in content
    assert 'git push origin --delete "${probe}"' in content, (
        "the probe branch must still be dropped after the run"
    )
