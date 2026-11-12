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

The merged gate runs every phase through the bounded runner, which is
``backend/.venv/bin/python -c <source> <budget> <pytest> <args>``.  The
synthetic virtualenv therefore also carries a ``bin/python`` symlink to
the interpreter running this test: the stub pytest is still what the
phase invokes, but the wall-clock guard around it needs a real Python.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

#: The repository root — this file lives at ``backend/tests/unit/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The backend, so the runner that reads the reports back is importable.
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification_ci_runner import run_ci_verification  # noqa: E402

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
    # The bounded runner is ``"$VENV_PATH/bin/python" -c ...``; the stub
    # venv needs a working interpreter for it, not a stub of its own.
    (venv_bin / "python").symlink_to(sys.executable)

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


# ---------------------------------------------------------------------------
# Who may read the reports
# ---------------------------------------------------------------------------
#
# The directory of a VP is reused across rounds: it holds the audit
# copy of the verdict as well as the reports the gate wrote.  So the
# reports of the last round are still there when the next round starts
# -- and read as if they belonged to this round, they name cases this
# round never ran.  That is how a whole repair round was built out of
# reports six days old (2026-11-08): the round fixed a failure list
# that no run of that day had produced.
#
# So the reports go before the gate runs, and a gate that runs the
# whole-repo gate *elsewhere* says so instead of reading them.


def _vp(entry: str) -> dict:
    """A ``full_ci`` verification point that runs *entry*."""
    return {
        "id": "VP-014",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": entry,
    }


#: A report of an earlier round: it names a case this round never ran.
_STALE_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" \
time="0.001" timestamp="2026-11-02T00:00:00" hostname="an-older-round">
    <testcase classname="tests.unit.test_older" \
name="test_of_an_earlier_round" time="0.001">\
<failure message="assert 1 == 2">E       assert 1 == 2</failure>\
</testcase>
  </testsuite>
</testsuites>
"""


def _stale_report_in(artifact_dir: Path) -> Path:
    """A report of an earlier round, in the artifact directory of a VP."""
    vp_dir = artifact_dir / "VP-014"
    vp_dir.mkdir(parents=True, exist_ok=True)
    report = vp_dir / "phase-2.xml"
    report.write_text(_STALE_REPORT, encoding="utf-8")
    return report


def test_stale_phase_reports_from_a_previous_run_are_not_read(tmp_path):
    """The reports in the directory belong to the run that wrote them.

    The report of the last round is still in the directory when this
    round starts.  Read as if it were this round's, it names a case
    this round never ran -- and the failing set is what the repair
    loop goes and fixes.

    A gate that runs the whole-repo gate on GitHub Actions has no
    reports here at all: ``scripts/ci_gha.sh`` dispatches the CI
    workflow, the lanes run on the runner and the reports they write
    stay there.  The evidence says so, instead of reading what this
    machine happens to be carrying and calling it this round's
    failures.
    """
    artifact_dir = tmp_path / "arts"
    report = _stale_report_in(artifact_dir)

    result = run_ci_verification(
        _vp("exit 1"), project_dir=tmp_path, artifact_dir=artifact_dir,
    )

    assert result.evidence["junit_phases"] == [], (
        "the report of an earlier round was read as this round's: "
        f"{result.evidence['junit_phases']!r}"
    )
    assert result.evidence["junit_failures"] == [], (
        "the failures of an earlier round were read as this round's: "
        f"{result.evidence['junit_failures']!r}"
    )
    assert result.evidence["failed_cases"] == [], (
        "the failing set names cases this round never ran: "
        f"{result.evidence['failed_cases']!r}"
    )
    assert not report.exists(), (
        "the report of the earlier round is still in the directory, so "
        "the next round would read it as its own"
    )

    # The same report, in front of a gate that runs the whole-repo
    # gate elsewhere: the reports are not available this run, and the
    # evidence has to say so.
    _stale_report_in(artifact_dir)

    # The script is here so the entry passes the path preflight; the
    # entry itself runs ``true``, because the real script pushes a
    # branch and waits for a workflow to finish.
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "ci_gha.sh").write_text("exit 0\n", encoding="utf-8")

    remote = run_ci_verification(
        _vp("true scripts/ci_gha.sh"), project_dir=tmp_path,
        artifact_dir=artifact_dir,
    )

    assert remote.evidence["junit_available"] is False, (
        "the evidence does not say that the reports are not available "
        "this run"
    )
    assert remote.evidence["junit_unavailable_reason"], (
        "the evidence does not say why the reports are not available"
    )
    assert remote.evidence["junit_phases"] == [], (
        "the phases of an earlier round were read as this round's: "
        f"{remote.evidence['junit_phases']!r}"
    )
    assert remote.evidence["junit_failures"] == []
    assert remote.evidence["failed_cases"] == [], (
        "the failing set names cases this round never ran: "
        f"{remote.evidence['failed_cases']!r}"
    )


def test_junit_availability_follows_the_run_not_the_entry_name(tmp_path):
    """Whether the reports are available is a fact about the run.

    The framework does not know what the project's gate scripts are
    called -- the entry is the project's to choose -- so it cannot
    decide "the reports are elsewhere" by recognising a name.  It looks
    at the directory the run started from empty.

    So an entry whose name says nothing to the framework and which
    writes no report gets the same answer as one that runs the gate
    somewhere else: not available, with a reason.  The same entry
    writing a report gets the opposite answer.  The answer follows
    what the run did.
    """
    artifact_dir = tmp_path / "arts"
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)

    # An entry with a name that means nothing to the framework, and
    # that writes no report at all.
    (scripts / "run_the_gate.sh").write_text("exit 0\n", encoding="utf-8")
    silent = run_ci_verification(
        _vp("bash scripts/run_the_gate.sh"), project_dir=tmp_path,
        artifact_dir=artifact_dir,
    )

    assert silent.evidence["junit_available"] is False, (
        "a run that wrote no report says the reports are available: "
        f"{silent.evidence['junit_available']!r}"
    )
    assert silent.evidence["junit_unavailable_reason"], (
        "the evidence does not say why the reports are not available"
    )
    assert silent.evidence["junit_phases"] == [], (
        "phases were read out of a run that wrote no report: "
        f"{silent.evidence['junit_phases']!r}"
    )

    # The same kind of entry, writing a report into the directory the
    # runner named for this run: now the reports are available.
    (scripts / "writes_a_report.sh").write_text(
        "#!/bin/sh\n"
        "cat > \"$PDT_GATE_JUNIT_DIR/phase-1.xml\" <<'XML'\n"
        "<testsuites name=\"pytest tests\">\n"
        "  <testsuite name=\"pytest\" errors=\"0\" failures=\"0\" "
        "skipped=\"0\" tests=\"1\" time=\"0.001\">\n"
        "    <testcase classname=\"tests.unit.test_anything\" "
        "name=\"test_ok\" time=\"0.001\"/>\n"
        "  </testsuite>\n"
        "</testsuites>\n"
        "XML\n"
        "exit 0\n",
        encoding="utf-8",
    )
    written = run_ci_verification(
        _vp("bash scripts/writes_a_report.sh"), project_dir=tmp_path,
        artifact_dir=artifact_dir,
    )

    assert written.evidence["junit_available"] is True, (
        "a run that wrote a report says the reports are not available: "
        f"{written.evidence['junit_available']!r}"
    )
    assert written.evidence["junit_phases"], (
        "the report this run wrote was not read: "
        f"{written.evidence['junit_phases']!r}"
    )
