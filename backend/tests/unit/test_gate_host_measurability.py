"""A busy host must not turn the machine's own slowness into a red lane.

Why this file exists
--------------------
The gate (``scripts/run_tests.sh``) asked the host whether it could
measure wall-clock time at all — but it only asked **before the
wall-clock phase**, and it only used the answer for that one phase.
Every other lane kept its verdict, so a phase-1 failure that said
"the service was not ready after 90 s" was reported as a code failure
even on a host that cannot time a sleep.

The consequence was not cosmetic.  The verdict is what the repair loop
turns into work: a red gate became a repair task against a delivery
that had not failed, and the next round failed the same way.

What is pinned here
-------------------
1. The probe runs **before every phase**, and its verdict reaches the
   test layer as ``PDT_HOST_MEASURABLE`` (``0`` unmeasurable, ``1``
   measurable) — the test layer's own load gate has to stand down on
   the measurement, not on a guess.
2. A phase that failed on an unmeasurable host is **INCONCLUSIVE** and
   the gate exits 3 — but only when every one of that phase's failures
   is a wall-clock shape.
3. **The negative control**: a phase with a deterministic failure (an
   assertion about behaviour) stays **FAILED** and the gate exits 1,
   even on an unmeasurable host.  "Unmeasurable host ⇒ everything is
   inconclusive" is not a fix; it hides real regressions.
4. A measurable host keeps today's verdict, unchanged.
5. The **test layer's own load gate** stands down on the measurement,
   not on the ``CI`` guess: a probe that measured the host beats an
   environment variable that guessed it.

The gate is driven for real: the script under test is the repository's
own ``scripts/run_tests.sh``, and the pytest it runs is a stub that
writes a junit report with the failure message the test wants.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

#: The backend root — this file lives at ``backend/tests/unit/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import _host_load  # noqa: E402
from tests import _readiness  # noqa: E402

#: The repository root — this file lives at ``backend/tests/unit/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The suites that wait for a service to come up, and therefore depend on
#: a deadline the host can actually keep.
SUITES_THAT_WAIT_FOR_A_SERVICE = (
    "tests/unit/test_service_manager.py",
    "tests/unit/test_service_kill_attribution.py",
    "tests/unit/test_verification_service_freshness.py",
    "tests/integration/test_service_spawn_repro.py",
    "tests/e2e/plan_scheduler/test_scheduler_operator_e2e.py",
    "tests/e2e/test_onboarding_and_frontend_e2e.py",
)

#: The gate script under contract.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"

#: A failure that only says the machine was too slow to answer.
WALL_CLOCK_MESSAGE = (
    "subprocess.TimeoutExpired: Command timed out after 120 seconds"
)

#: A failure that says something about the code, not about the clock.
DETERMINISTIC_MESSAGE = "AssertionError: 1 + 1 == 3"

#: The stub pytest: it records the ``PDT_HOST_MEASURABLE`` it was
#: given, writes the junit report the gate asked for, and exits with
#: the code the test wants.
_STUB_SOURCE = '''#!/usr/bin/env python3
import os
import pathlib
import sys

report = ""
for arg in sys.argv[1:]:
    if arg.startswith("--junitxml="):
        report = arg[len("--junitxml="):]

with open(os.environ["PDT_STUB_ENV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(os.environ.get("PDT_HOST_MEASURABLE", "unset") + "|")

if report:
    path = pathlib.Path(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<testsuites><testsuite name="pytest" tests="1" failures="1" '
        'errors="0" skipped="0"><testcase classname="t" name="n">'
        '<failure message="' + os.environ["PDT_STUB_MESSAGE"] + '">trace'
        "</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

sys.exit(int(os.environ.get("PDT_STUB_EXIT", "1")))
'''


def _write_stub(tmp_path: Path) -> Path:
    """Write the fake pytest and return its path."""
    stub = tmp_path / "fake_pytest"
    stub.write_text(
        _STUB_SOURCE.replace("#!/usr/bin/env python3", f"#!{sys.executable}"),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run_gate(
    tmp_path: Path,
    *,
    message: str,
    probe: str,
    exit_code: str = "1",
    lane: str = "all",
) -> Tuple[subprocess.CompletedProcess, List[str]]:
    """Run the real gate against the stub pytest.

    Returns the completed process and the ``PDT_HOST_MEASURABLE`` values
    the stub saw, one per phase, in the order the phases ran.
    """
    stub = _write_stub(tmp_path)
    junit_dir = tmp_path / "junit"
    junit_dir.mkdir(exist_ok=True)
    env_log = tmp_path / "env.log"
    # Each run records its own measurements: the log is the run's
    # evidence, not the test's.
    if env_log.exists():
        env_log.unlink()

    env = dict(os.environ)
    # The ambient environment must not decide what this run does.
    env.pop("PDT_RUN_TESTS_DRY_RUN", None)
    env.pop("PDT_HOST_MEASURABLE", None)
    env.update({
        "PYTEST_BIN": str(stub),
        "PDT_STUB_MESSAGE": message,
        "PDT_STUB_EXIT": exit_code,
        "PDT_STUB_ENV_LOG": str(env_log),
        "PDT_HOST_TIMING_PROBE": probe,
        "PDT_GATE_JUNIT_DIR": str(junit_dir),
    })

    completed = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--lane", lane],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=300,
    )
    observed: List[str] = []
    if env_log.exists():
        observed = [
            value for value in env_log.read_text(encoding="utf-8").split("|")
            if value
        ]
    return completed, observed


def _summary(stdout: str) -> Dict[str, str]:
    """The gate's summary block, as ``{label: verdict}``."""
    lines = stdout.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines)
            if line.strip() == "=== gate summary ==="
        )
    except StopIteration:
        raise AssertionError(
            f"the gate printed no summary block:\n{stdout}"
        ) from None
    verdicts: Dict[str, str] = {}
    for line in lines[start + 1:]:
        if ":" not in line:
            break
        label, _, verdict = line.partition(":")
        verdicts[label.strip()] = verdict.strip()
    return verdicts


def _verdict(summary: Dict[str, str], prefix: str) -> str:
    """The verdict of the summary line whose label starts with ``prefix``."""
    for label, verdict in summary.items():
        if label.startswith(prefix):
            return verdict
    raise AssertionError(
        f"the summary has no line for {prefix!r}: {sorted(summary)}"
    )


# ---------------------------------------------------------------------------
# 1. An unmeasurable host does not produce a red lane
# ---------------------------------------------------------------------------


def test_a_wall_clock_failure_on_an_unmeasurable_host_is_inconclusive(
    tmp_path: Path,
) -> None:
    """The machine was too slow to answer — that is not the code's fault.

    Every failure in the phase says the same thing ("it timed out"), so
    the phase's verdict is the machine's, and the gate says so: exit 3.
    """
    completed, _ = _run_gate(
        tmp_path, message=WALL_CLOCK_MESSAGE, probe="fail",
    )

    assert completed.returncode == 3, (
        f"a wall-clock failure on an unmeasurable host produced exit code "
        f"{completed.returncode}, not 3:\n{completed.stdout}\n{completed.stderr}"
    )
    verdicts = _summary(completed.stdout)
    for phase in ("phase 1/3", "phase 2/3", "phase 3/3"):
        verdict = _verdict(verdicts, phase)
        assert "INCONCLUSIVE" in verdict, (
            f"{phase} is reported as {verdict!r} on a host that "
            f"cannot measure wall-clock time:\n{completed.stdout}"
        )
        assert "FAILED" not in verdict, (
            f"{phase} is reported as {verdict!r} on a host that "
            f"cannot measure wall-clock time:\n{completed.stdout}"
        )


def test_the_negative_control_keeps_a_deterministic_failure_red(
    tmp_path: Path,
) -> None:
    """A deterministic failure stays FAILED, whatever the host is doing.

    "The host is busy" is an explanation for a *timing* failure.  It is
    not an explanation for ``1 + 1 == 3``, and a fix that turns every
    failure on a busy host into "inconclusive" hides real regressions
    behind the machine's noise.
    """
    completed, _ = _run_gate(
        tmp_path, message=DETERMINISTIC_MESSAGE, probe="fail",
    )

    assert completed.returncode == 1, (
        f"a deterministic failure on an unmeasurable host produced exit "
        f"code {completed.returncode}, not 1:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    verdicts = _summary(completed.stdout)
    assert "FAILED" in _verdict(verdicts, "phase 1/3"), (
        f"phase 1 is reported as {verdicts['phase 1/3']!r} although its "
        f"failure is not a wall-clock shape:\n{completed.stdout}"
    )


# ---------------------------------------------------------------------------
# 2. The measurement reaches the test layer
# ---------------------------------------------------------------------------


def test_the_measurement_is_exported_to_the_phases(tmp_path: Path) -> None:
    """The test layer has to learn what the probe measured.

    The test layer's own load gate stands down on ``CI=true`` — a guess.
    The probe is a measurement.  The gate has to hand the measurement
    down, or the layers keep disagreeing about the same host.
    """
    completed, observed = _run_gate(
        tmp_path, message=WALL_CLOCK_MESSAGE, probe="fail",
    )
    assert observed, (
        "no phase ran, so nothing could see the measurement:\n"
        f"{completed.stdout}"
    )
    assert observed[0] == "0", (
        f"the phases were told the host is measurable (PDT_HOST_MEASURABLE="
        f"{observed[0]!r}) although the probe said it is not:\n"
        f"{completed.stdout}"
    )

    completed, observed = _run_gate(
        tmp_path, message=DETERMINISTIC_MESSAGE, probe="pass",
    )
    assert observed and observed[0] == "1", (
        f"the phases were told the host is unmeasurable "
        f"(PDT_HOST_MEASURABLE={observed[0]!r}) although the probe said it "
        f"is measurable:\n{completed.stdout}"
    )


def test_a_measurable_host_keeps_todays_verdict(tmp_path: Path) -> None:
    """A measurable host is judged by the code, as it always was."""
    completed, observed = _run_gate(
        tmp_path, message=DETERMINISTIC_MESSAGE, probe="pass",
    )

    assert completed.returncode == 1, (
        f"a deterministic failure on a measurable host produced exit code "
        f"{completed.returncode}, not 1:\n{completed.stdout}\n{completed.stderr}"
    )
    verdicts = _summary(completed.stdout)
    assert "FAILED" in _verdict(verdicts, "phase 1/3"), (
        f"phase 1 is reported as {verdicts['phase 1/3']!r}:\n{completed.stdout}"
    )


def test_the_summary_does_not_claim_the_host_was_measurable(
    tmp_path: Path,
) -> None:
    """Every lane reports the same host — the one that was measured.

    The summary used to say "measurable" for phases 1 and 2 no matter
    what the probe had found, so the screen contradicted itself: one
    line said the machine could not be measured, the next two said it
    could.
    """
    completed, _ = _run_gate(
        tmp_path, message=WALL_CLOCK_MESSAGE, probe="fail",
    )

    assert "host measurability:" in completed.stdout, (
        f"the gate did not say what the probe measured:\n{completed.stdout}"
    )
    for line in completed.stdout.splitlines():
        if line.startswith("host measurability:"):
            note = line.split(":", 1)[1].strip()
            break
    else:  # pragma: no cover - the assertion above fires first
        raise AssertionError("no host measurability line")
    assert "cannot measure" in note, (
        f"the summary says the host is measurable ({note!r}) although the "
        f"probe was forced to fail:\n{completed.stdout}"
    )


# ---------------------------------------------------------------------------
# 3. The measurement reaches the test layer's own load gate
# ---------------------------------------------------------------------------


def test_the_host_fact_is_read_as_three_valued() -> None:
    """Measured / not measured / never measured — three different things.

    Only the middle one makes the machine's verdict worth less.  A run
    that never measured has not declared the host untrustworthy, so it
    must leave every gate exactly where it was.
    """
    assert _host_load.host_measurable(env={"PDT_HOST_MEASURABLE": "1"}) is True
    assert _host_load.host_measurable(env={"PDT_HOST_MEASURABLE": "0"}) is False
    assert _host_load.host_measurable(env={}) is None
    assert _host_load.host_measurable(env={"PDT_HOST_MEASURABLE": ""}) is None
    assert _host_load.host_measurable(env={"PDT_HOST_MEASURABLE": "maybe"}) is None


def test_a_measurement_beats_the_ci_guess() -> None:
    """``CI=true`` is a guess; the probe is a measurement.

    The load gate used to stand down on ``CI`` before it looked at
    anything else — so on a CI runner the gate asked nothing at all,
    and a host that cannot time a sleep produced failures nobody could
    explain.  A measurement outranks a guess, in both directions.
    """
    reason = _host_load.skip_reason(
        load=1.0, limit=20.0,
        env={"CI": "true", "PDT_HOST_MEASURABLE": "0"},
    )

    assert reason is not None, (
        "a host that was measured to be unmeasurable was not skipped, "
        "because CI was believed instead of the measurement"
    )
    assert "cannot measure" in reason, (
        f"the skip reason does not say why: {reason!r}"
    )


def test_a_measurable_host_keeps_the_ci_exception() -> None:
    """A measured host that can keep time keeps the CI exception."""
    assert _host_load.skip_reason(
        load=55.0, limit=20.0,
        env={"CI": "true", "PDT_HOST_MEASURABLE": "1"},
    ) is None


def test_an_unmeasured_host_changes_nothing() -> None:
    """No measurement, no change: the historical behaviour stands.

    The gate is only allowed to stand down on something that was
    actually measured.  A run that never measured leaves the load gate
    exactly as it was — including the CI exception, which is still
    there for the hosted runners that have never had a probe.
    """
    assert _host_load.skip_reason(load=55.0, limit=20.0, env={}) == (
        "skipped: host load 55.0 >= 20"
    )
    assert _host_load.skip_reason(
        load=55.0, limit=20.0, env={"CI": "true"},
    ) is None
    assert _host_load.skip_reason(load=3.0, limit=20.0, env={}) is None


# ---------------------------------------------------------------------------
# 4. The shared readiness budget lives in one place
# ---------------------------------------------------------------------------


def test_the_shared_budget_follows_the_host_load() -> None:
    """One formula, in one place — the copies had drifted.

    The budget a spawned process is given follows the host's load,
    because both the child's own start-up and the cost of one probe
    get slower when the machine is busy.  A literal deadline measures
    the host instead of the service.
    """
    assert _readiness.readiness_budget_seconds(
        20, limit=20, load=0,
    ) == 20.0, "a quiet host gets the base budget"
    assert _readiness.readiness_budget_seconds(
        20, limit=20, load=20,
    ) == 40.0, "a host at the limit gets twice the base budget"


def test_a_scaled_budget_is_still_a_budget() -> None:
    """The ceiling is what keeps a scaled budget a budget."""
    assert _readiness.readiness_budget_seconds(
        120, ceiling=90, limit=20, load=200,
    ) == 90.0, "a scaled budget must not outlast its ceiling"


def test_the_shared_decision_skips_an_unmeasurable_host(monkeypatch) -> None:
    """A host that cannot keep time is skipped, not failed.

    The deadline is meaningless on a host whose clock does not work:
    waiting for a service that is up, and calling it a regression
    because the machine could not count, is how a delivery that never
    failed turns into a repair task.
    """
    monkeypatch.setenv(_host_load.HOST_MEASURABLE_ENV, "0")

    with pytest.raises(pytest.skip.Exception):
        _readiness.skip_if_host_unmeasurable()


def test_the_shared_decision_leaves_a_measurable_host_alone(monkeypatch) -> None:
    """A host that can keep time runs the case, as it always did."""
    monkeypatch.setenv(_host_load.HOST_MEASURABLE_ENV, "1")

    _readiness.skip_if_host_unmeasurable()


def test_the_suites_that_wait_for_a_service_use_the_shared_decision() -> None:
    """Six suites wait for a service; they all ask the same question.

    The decision was copied into each suite once already, and the
    copies drifted.  One shared call in each of them is what keeps the
    next copy from being written by hand.
    """
    missing = []
    for relative in SUITES_THAT_WAIT_FOR_A_SERVICE:
        source = (REPO_ROOT / "backend" / relative).read_text(encoding="utf-8")
        if "_readiness.skip_if_host_unmeasurable()" not in source:
            missing.append(relative)

    assert not missing, (
        "these suites wait for a service to come up but do not ask the "
        f"shared decision what to do on a host that cannot keep time: "
        f"{missing}"
    )
