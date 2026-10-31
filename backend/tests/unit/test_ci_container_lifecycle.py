"""Every gate lane runs in its own throwaway container.

Why this file exists
--------------------
The local gate used to run every lane inside one long-lived container
(``docker run -d ... sleep infinity``).  That shape has two failure
modes, and both of them are about what a container that outlives its
run leaves behind:

* the container stayed behind whenever the driver died, and
* lanes that shared one container shared its processes, its ports and
  its temporary files, so a leak from one lane reached the next one.

``scripts/run_ci_lanes_in_container.sh`` replaces that shape: one
container per lane, one lane at a time, ``--rm`` so the container
disappears with its lane, three labels so a cleanup script can find
what a driver left behind, and a lock so two drivers cannot run the
gate on one machine at the same time.

Every assertion here runs the driver against a stand-in ``docker`` on
``PATH``.  No docker daemon is needed: the stand-in records each call,
can be told to fail one lane, and can be told to hang so the driver's
own cleanup can be watched.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

#: The repository root, resolved from this file rather than from the
#: working directory: pytest is often launched with cwd=``backend/``.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The driver under test.
DRIVER = PROJECT_ROOT / "scripts" / "run_ci_lanes_in_container.sh"

#: The lock the driver takes, so a second driver refuses to start.
#: It is a fixed path on purpose -- the lock is a machine-wide claim,
#: not a per-run one, so two drivers that disagree about where the
#: lock lives would happily run the gate at the same time.
LOCK_DIR = Path("/tmp/pdt-ci-driver.lock")


# ---------------------------------------------------------------------------
# The stand-in docker
# ---------------------------------------------------------------------------

#: A stand-in for ``docker``.
#:
#: It records every call it receives, and for a ``run`` it records when
#: the lane started and when it ended -- those two events are what
#: proves the lanes ran one after another rather than side by side.
#:
#: It reads three environment variables:
#:
#:   ``PDT_CI_TEST_DOCKER_LOG``  every call is appended to this file
#:   ``PDT_CI_TEST_EVENTS``      the lane start / end events go here
#:   ``PDT_CI_TEST_EXIT_MAP``    ``<lane> <exit code>`` lines: the
#:                               exit code a named lane reports
#:   ``PDT_CI_TEST_HANG``        when ``1``, a ``run`` never returns
_FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$PDT_CI_TEST_DOCKER_LOG"

if [ "$1" != "run" ]; then
    exit 0
fi

# The lane is the value that follows ``--lane``.
lane=""
previous=""
for argument in "$@"; do
    if [ "$previous" = "--lane" ]; then
        lane="$argument"
    fi
    previous="$argument"
done

printf 'start %s\\n' "$lane" >> "$PDT_CI_TEST_EVENTS"

if [ "${PDT_CI_TEST_HANG:-0}" = "1" ]; then
    sleep 3600
fi

printf 'end %s\\n' "$lane" >> "$PDT_CI_TEST_EVENTS"

status=0
if [ -f "$PDT_CI_TEST_EXIT_MAP" ]; then
    while read -r map_lane map_status; do
        if [ "$map_lane" = "$lane" ]; then
            status="$map_status"
        fi
    done < "$PDT_CI_TEST_EXIT_MAP"
fi
exit "$status"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_lock_left_behind():
    """The lock is machine-wide state; no test may leave one behind.

    The driver removes the lock when it exits, so this only matters
    when a driver was killed mid-run -- but that is exactly the case
    the lock has to survive, and a leftover lock would make every
    later test in this file fail with "another gate run is in
    progress".
    """
    shutil.rmtree(LOCK_DIR, ignore_errors=True)
    yield
    shutil.rmtree(LOCK_DIR, ignore_errors=True)


def _environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """The environment the driver runs with: a stand-in docker on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)

    exit_map = tmp_path / "exit-map"
    exit_map.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PDT_CI_TEST_DOCKER_LOG"] = str(tmp_path / "docker.log")
    env["PDT_CI_TEST_EVENTS"] = str(tmp_path / "events.log")
    env["PDT_CI_TEST_EXIT_MAP"] = str(exit_map)
    env.update(overrides)
    return env


def _read_log(path: str) -> str:
    """The contents of a log the stand-in wrote, or ``""`` if there is none."""
    log = Path(path)
    if not log.exists():
        return ""
    return log.read_text(encoding="utf-8").strip()


def _run_calls(log_text: str) -> list[list[str]]:
    """The ``docker run`` calls in *log_text*, as token lists."""
    return [
        line.split()
        for line in log_text.splitlines()
        if line.startswith("run ")
    ]


def _flag_value(tokens: list[str], flag: str) -> str | None:
    """The value that follows *flag* in *tokens*, or ``None``."""
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _flag_values(tokens: list[str], flag: str) -> list[str]:
    """Every value that follows *flag* in *tokens*."""
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            values.append(tokens[index + 1])
    return values


def _run_driver(env: dict[str, str], *lanes: str) -> subprocess.CompletedProcess[str]:
    """Run the driver over *lanes* with the stand-in docker on PATH."""
    return subprocess.run(
        ["bash", str(DRIVER), *lanes],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _wait_for_container_name(log_path: str, timeout: float = 30.0) -> str:
    """The name of the container the driver started, once it has started one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        calls = _run_calls(_read_log(log_path))
        if calls:
            name = _flag_value(calls[0], "--name")
            if name:
                return name
        time.sleep(0.05)
    raise AssertionError("the driver never started a container")


# ---------------------------------------------------------------------------
# One container per lane, in order
# ---------------------------------------------------------------------------


def test_one_container_per_lane_in_order(tmp_path: Path) -> None:
    """Each lane gets its own container, and the lanes run one at a time.

    One container for all lanes is what the driver replaced: a lane
    that inherited the previous lane's processes, ports and temporary
    files was testing the previous lane's leftovers as much as its own
    code.

    Serial execution is the other half of the same property -- a
    shared container cannot be shared by two lanes that run at the same
    time, so "one at a time" is what makes the isolation real rather
    than nominal.  The stand-in records when each lane started and
    when it ended; the second lane may only start after the first one
    ended.
    """
    env = _environment(tmp_path)
    result = _run_driver(env, "unit", "e2e")

    assert result.returncode == 0, (
        f"the driver failed: {result.stdout!r} / {result.stderr!r}"
    )

    calls = _run_calls(_read_log(env["PDT_CI_TEST_DOCKER_LOG"]))
    assert len(calls) == 2, (
        "the driver did not run one container per lane: "
        f"{_read_log(env['PDT_CI_TEST_DOCKER_LOG'])!r}"
    )

    events = _read_log(env["PDT_CI_TEST_EVENTS"]).splitlines()
    assert events == ["start unit", "end unit", "start e2e", "end e2e"], (
        "the lanes did not run one after another; a lane that starts "
        f"while another one is running shares its container: {events!r}"
    )

    names = [_flag_value(call, "--name") for call in calls]
    assert all(names), f"a container ran without a name: {names!r}"
    assert names[0] != names[1], (
        f"both lanes ran in the same container: {names!r}"
    )

    # The names carry the lane they ran and the id of the run they
    # belong to: ``pdt-ci-<lane>-<run-id>``.
    suffixes = [name.rsplit("-", 1)[-1] for name in names]
    assert suffixes[0] == suffixes[1], (
        f"the two containers belong to different runs: {names!r}"
    )
    assert names[0].startswith("pdt-ci-unit-"), names[0]
    assert names[1].startswith("pdt-ci-e2e-"), names[1]


# ---------------------------------------------------------------------------
# Every run is removable and labelled
# ---------------------------------------------------------------------------


def test_every_run_is_removable_and_labelled(tmp_path: Path) -> None:
    """Every container is created to be removed, and carries three labels.

    ``--rm`` is what makes the container disappear with its lane.  The
    labels are what makes a leftover findable: a driver that was
    SIGKILLed runs no trap, so the container it started stays -- and the
    cleanup script that has to deal with that can only find it by
    label.  ``pdt-ci=1`` says "this is a gate container", ``pdt-owner``
    says whose it was, and ``pdt-created`` says when it was made.
    """
    env = _environment(tmp_path)
    result = _run_driver(env, "unit", "e2e")

    assert result.returncode == 0, (
        f"the driver failed: {result.stdout!r} / {result.stderr!r}"
    )

    calls = _run_calls(_read_log(env["PDT_CI_TEST_DOCKER_LOG"]))
    assert len(calls) == 2, (
        "the driver did not run one container per lane: "
        f"{_read_log(env['PDT_CI_TEST_DOCKER_LOG'])!r}"
    )

    for call in calls:
        assert "--rm" in call, f"a container was not created to be removed: {call!r}"

        labels = _flag_values(call, "--label")
        assert "pdt-ci=1" in labels, f"the pdt-ci label is missing: {labels!r}"

        owners = [label for label in labels if label.startswith("pdt-owner=")]
        assert len(owners) == 1, f"the pdt-owner label is missing: {labels!r}"
        assert owners[0].split("=", 1)[1].isdigit(), (
            f"the pdt-owner label does not carry a user id: {owners[0]!r}"
        )

        created = [label for label in labels if label.startswith("pdt-created=")]
        assert len(created) == 1, f"the pdt-created label is missing: {labels!r}"
        assert created[0].split("=", 1)[1].isdigit(), (
            f"the pdt-created label does not carry a timestamp: {created[0]!r}"
        )


# ---------------------------------------------------------------------------
# A failed lane does not stop the next one
# ---------------------------------------------------------------------------


def test_a_failed_lane_does_not_stop_the_next(tmp_path: Path) -> None:
    """A failing lane fails the run; it does not cancel the lanes after it.

    Stopping at the first failure would hide the rest of the gate
    behind it -- and a gate that reports one failure at a time is a
    gate that needs one run per failure.  Every lane runs; the run
    fails at the end.
    """
    env = _environment(tmp_path)
    Path(env["PDT_CI_TEST_EXIT_MAP"]).write_text("unit 1\n", encoding="utf-8")

    result = _run_driver(env, "unit", "e2e")

    assert result.returncode == 1, (
        "the driver did not report the failed lane: "
        f"{result.stdout!r} / {result.stderr!r}"
    )

    events = _read_log(env["PDT_CI_TEST_EVENTS"]).splitlines()
    assert events == ["start unit", "end unit", "start e2e", "end e2e"], (
        "the lane after the failed one did not run: " f"{events!r}"
    )

    assert "[unit] " in result.stdout, result.stdout
    assert "-> exit 1" in result.stdout, result.stdout
    assert "[e2e] " in result.stdout, result.stdout
    assert "-> exit 0" in result.stdout, result.stdout
    assert "lanes: 1/2 passed" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# The trap removes the container the driver started
# ---------------------------------------------------------------------------


def test_the_trap_removes_the_container_the_driver_started(tmp_path: Path) -> None:
    """A driver that is killed takes its container with it.

    The container is started detached from the driver's own lifetime:
    when the driver is gone, nothing is left to stop it.  The trap is
    what closes that gap -- on a signal, and on any other way out of
    the script, the container the driver started is removed.

    The stand-in hangs inside ``run``, so the driver is sitting in its
    lane when the signal arrives.
    """
    env = _environment(tmp_path, PDT_CI_TEST_HANG="1")
    stdout_path = tmp_path / "driver.out"
    stderr_path = tmp_path / "driver.err"

    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        process = subprocess.Popen(
            ["bash", str(DRIVER), "unit"],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            container = _wait_for_container_name(env["PDT_CI_TEST_DOCKER_LOG"])
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=60)
        finally:
            # The stand-in is still sleeping inside the container it
            # pretended to start; the process group goes with it.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

    assert process.returncode not in (0, None), (
        "the driver ignored the signal that was sent to it"
    )

    log = _read_log(env["PDT_CI_TEST_DOCKER_LOG"])
    removals = [
        line
        for line in log.splitlines()
        if "rm -f" in line and container in line
    ]
    assert removals, (
        f"the driver did not remove the container it started ({container}); "
        f"the calls it made were: {log!r}"
    )


# ---------------------------------------------------------------------------
# A second driver is refused
# ---------------------------------------------------------------------------


def test_a_second_driver_is_refused(tmp_path: Path) -> None:
    """Two drivers do not run the gate on one machine at the same time.

    The lanes share ports, the machine's CPU and its temporary
    directories; two drivers running at once is how a gate run starts
    reporting failures that belong to the other run.  The second driver
    says so and starts nothing at all.
    """
    env = _environment(tmp_path)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = _run_driver(env, "unit")
    finally:
        shutil.rmtree(LOCK_DIR, ignore_errors=True)

    assert result.returncode == 75, (
        "the second driver was not refused: "
        f"{result.stdout!r} / {result.stderr!r}"
    )
    assert "another gate run is in progress" in result.stdout + result.stderr, (
        "the second driver did not say why it refused: "
        f"{result.stdout!r} / {result.stderr!r}"
    )
    assert not _run_calls(_read_log(env["PDT_CI_TEST_DOCKER_LOG"])), (
        "the refused driver started a container anyway: "
        f"{_read_log(env['PDT_CI_TEST_DOCKER_LOG'])!r}"
    )