"""The gate writes one junit report per phase — and only when asked to.

Why this file exists
--------------------
The gate (``scripts/run_tests.sh``) runs three phases back to back, and
the verdict keeps only the tail of the whole run's output.  A failure in
phase 1 is therefore gone from the evidence by the time phase 3 ends.

The fix is a report per phase: each phase writes its own junit XML into
the directory ``PDT_GATE_JUNIT_DIR`` names, and the report names the
cases that failed instead of leaving them in a scrolled-off stream.

What is pinned here
-------------------
1. **With** ``PDT_GATE_JUNIT_DIR`` set, every phase that *runs* gets
   ``--junitxml=<dir>/phase-<n>.xml`` on its pytest command line, with
   ``<n>`` the phase's own number.
2. **Without** it, no phase carries ``--junitxml`` at all — and the run
   writes no file, so a run that is not read as evidence stays exactly
   as it was.

The gate script resolves its virtualenv and its suite relative to its
own location, so the script under test is a *copy* of the repository's
own ``scripts/run_tests.sh`` placed in a throwaway project root that
has both a stub ``backend/.venv/bin/activate`` and a
``backend/tests`` directory.  The copy is byte-for-byte the script the
gate really runs, and nothing in the repository is touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

import pytest

#: The repository root — this file lives at ``backend/tests/unit/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The gate script under contract.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"

#: The environment variable that turns the per-phase reports on.
JUNIT_DIR_ENV = "PDT_GATE_JUNIT_DIR"

#: The stub pytest: it records the argv it was handed, one argument per
#: line, and the calls are separated by a marker line.
_STUB_PYTEST_SOURCE = """#!/bin/sh
for arg in "$@"; do
    printf '%s\\n' "$arg" >> "$PDT_STUB_LOG"
done
printf -- '--pdt-call--\\n' >> "$PDT_STUB_LOG"
exit "${PDT_STUB_EXIT:-0}"
"""

#: The marker that separates two recorded calls.
_CALL_SEPARATOR = "--pdt-call--"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not installed on this host",
)


def _synthetic_gate(tmp_path: Path) -> tuple[Path, Path]:
    """A copy of the gate script in a throwaway project root.

    Returns the root and the copied script.  The root carries the two
    things the script resolves relative to its own location: a stub
    virtualenv to activate, and a ``backend/tests`` tree for the default
    target.
    """
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    gate = scripts / "run_tests.sh"
    shutil.copy2(GATE_SCRIPT, gate)

    venv_bin = root / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text(
        "# stub activate: the gate only needs the file to exist\n",
        encoding="utf-8",
    )

    tests = root / "backend" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_stub.py").write_text("def test_stub():\n    pass\n",
                                        encoding="utf-8")
    return root, gate


def _stub_pytest(tmp_path: Path) -> Path:
    """The fake pytest, and the log it writes its calls into."""
    stub = tmp_path / "fake_pytest"
    stub.write_text(_STUB_PYTEST_SOURCE, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run_gate(
    root: Path,
    gate: Path,
    stub: Path,
    log: Path,
    *,
    lane: str = "all",
    junit_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the gate against the fake pytest."""
    env = dict(os.environ)
    # The ambient environment must not decide what this run does.
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env.pop(JUNIT_DIR_ENV, None)
    env["PYTEST_BIN"] = str(stub)
    env["PDT_STUB_LOG"] = str(log)
    # The wall-clock phase asks the host whether it can measure time;
    # the answer is forced so the run does not depend on the machine.
    env["PDT_HOST_TIMING_PROBE"] = "pass"
    if junit_dir is not None:
        env[JUNIT_DIR_ENV] = str(junit_dir)
    return subprocess.run(
        ["bash", str(gate), "--lane", lane],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=120,
    )


def _recorded_calls(log: Path) -> List[List[str]]:
    """The argv of every call the fake pytest received."""
    if not log.exists():
        return []
    calls: List[List[str]] = []
    current: List[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == _CALL_SEPARATOR:
            calls.append(current)
            current = []
        else:
            current.append(line)
    if current:
        calls.append(current)
    return calls


def _junit_arguments(call: List[str]) -> List[str]:
    """The ``--junitxml`` arguments of one recorded call."""
    return [arg for arg in call if arg.startswith("--junitxml")]


class TestGateJunitDir:
    def test_every_phase_that_runs_gets_its_own_report(self, tmp_path):
        root, gate = _synthetic_gate(tmp_path)
        stub = _stub_pytest(tmp_path)
        log = tmp_path / "calls.log"
        junit_dir = tmp_path / "junit"

        completed = _run_gate(
            root, gate, stub, log, lane="all", junit_dir=junit_dir,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

        calls = _recorded_calls(log)
        assert len(calls) == 3, (
            f"the gate made {len(calls)} pytest call(s), expected one per "
            f"phase:\n{completed.stdout}"
        )
        for number, call in enumerate(calls, start=1):
            assert _junit_arguments(call) == [
                f"--junitxml={junit_dir}/phase-{number}.xml"
            ], (
                f"phase {number} was asked for "
                f"{_junit_arguments(call)}, expected "
                f"--junitxml={junit_dir}/phase-{number}.xml"
            )

    def test_a_lane_runs_only_its_own_phase_and_its_own_report(
        self, tmp_path,
    ):
        root, gate = _synthetic_gate(tmp_path)
        stub = _stub_pytest(tmp_path)
        log = tmp_path / "calls.log"
        junit_dir = tmp_path / "junit"

        completed = _run_gate(
            root, gate, stub, log, lane="unit", junit_dir=junit_dir,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

        calls = _recorded_calls(log)
        assert len(calls) == 1, (
            f"the unit lane made {len(calls)} pytest call(s), expected one:\n"
            f"{completed.stdout}"
        )
        assert _junit_arguments(calls[0]) == [
            f"--junitxml={junit_dir}/phase-1.xml"
        ]

    def test_without_the_variable_no_phase_is_asked_for_a_report(
        self, tmp_path,
    ):
        root, gate = _synthetic_gate(tmp_path)
        stub = _stub_pytest(tmp_path)
        log = tmp_path / "calls.log"

        completed = _run_gate(root, gate, stub, log, lane="all")
        assert completed.returncode == 0, completed.stdout + completed.stderr

        calls = _recorded_calls(log)
        assert len(calls) == 3, (
            f"the gate made {len(calls)} pytest call(s), expected one per "
            f"phase:\n{completed.stdout}"
        )
        for number, call in enumerate(calls, start=1):
            assert _junit_arguments(call) == [], (
                f"phase {number} was asked for a junit report even though "
                f"{JUNIT_DIR_ENV} was not set: {_junit_arguments(call)}"
            )

    def test_without_the_variable_the_run_writes_no_file(self, tmp_path):
        root, gate = _synthetic_gate(tmp_path)
        stub = _stub_pytest(tmp_path)
        log = tmp_path / "calls.log"

        before = sorted(path.name for path in root.rglob("*"))
        completed = _run_gate(root, gate, stub, log, lane="all")
        assert completed.returncode == 0, completed.stdout + completed.stderr
        after = sorted(path.name for path in root.rglob("*"))

        assert before == after, (
            "the gate changed the tree even though no report was asked "
            f"for:\n  before: {before}\n  after:  {after}"
        )

    def test_the_reports_are_named_after_the_phase_and_not_the_lane(
        self, tmp_path,
    ):
        root, gate = _synthetic_gate(tmp_path)
        stub = _stub_pytest(tmp_path)
        log = tmp_path / "calls.log"
        junit_dir = tmp_path / "junit"

        completed = _run_gate(
            root, gate, stub, log, lane="e2e", junit_dir=junit_dir,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

        calls = _recorded_calls(log)
        assert len(calls) == 1
        # The e2e lane is the *second* phase of the gate, and its report
        # says so: the number is the phase's, not the lane's.
        assert _junit_arguments(calls[0]) == [
            f"--junitxml={junit_dir}/phase-2.xml"
        ]
