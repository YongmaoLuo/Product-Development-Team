"""Contract tests for the gate script's lanes, partition and exit codes.

The gate (``scripts/run_tests.sh``) is what a verification point runs when it
asks for "the whole repository". Its verdict is a single exit code, so that
code has to carry three meanings:

* ``0`` — every phase that ran passed;
* ``1`` — a phase failed: a defect in the code under test;
* ``3`` — the run could not decide: the host cannot be measured, so the
  wall-clock phase's verdict would be noise about the machine rather than
  about the code.

The third state is what this file exists for.  Without it, a busy host and
a broken build produced the same exit code, and the busy host's noise was
reported as a code failure — which is a statement about the code that
nobody measured.

What is pinned here:

1. the three phases' marker expressions **partition** the suite: every case
   is selected by exactly one of them, so every case runs once and none
   runs twice;
2. each ``--lane`` entry point runs exactly the phase it names, and prints
   the argv it will execute;
3. a host that cannot be measured produces exit code **3**, not 1;
4. the phase exit codes are still combined with AND — one failing phase
   makes the gate fail, and a later phase never overwrites an earlier
   failure.

The partition is checked against a real collection rather than against the
expressions' text: two expressions can look disjoint and still overlap on
the cases that carry both markers.  The collection is done once per module
(the whole suite takes a while to import) and the markers of every case are
read out of it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

# pytest's own marker-expression engine.  This is the exact class the
# ``-m`` option uses (``_pytest.mark._parse_expression`` compiles the
# expression with it), so evaluating an expression here answers the same
# question pytest answers when it selects cases.  It is a private module,
# and a future pytest that renames it fails this file loudly rather than
# quietly changing what "the lane expression" means.
from _pytest.mark.expression import Expression

#: The repository root — this file lives at ``backend/tests/contract/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The gate script under contract.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"

#: The tree the gate runs.
SUITE_ROOT = REPO_ROOT / "backend" / "tests"

#: The three phases' marker expressions, as the gate declares them.
PHASE1_EXPRESSION = "not time_sensitive and not perf and not e2e and not integration"
PHASE2_EXPRESSION = "(e2e or integration) and not time_sensitive"
PHASE3_EXPRESSION = "time_sensitive or perf"

#: The argv each phase appends after the target, in the gate's own order.
PHASE_ARGUMENTS: Dict[int, List[str]] = {
    1: ["-n", "auto", "-m", PHASE1_EXPRESSION],
    2: ["-m", PHASE2_EXPRESSION, "--timeout=600"],
    3: ["-m", PHASE3_EXPRESSION, "--timeout=1800"],
}

#: Which phases each ``--lane`` name runs.
LANE_PHASES: Dict[str, Tuple[int, ...]] = {
    "unit": (1,),
    "e2e": (2,),
    "wallclock": (3,),
    "all": (1, 2, 3),
}

_MARKER_ARGUMENT_RE = re.compile(r'-m "([^"]+)"')

# pytest's own summary line, which is the only *independent* source for the
# number of cases the suite holds:
#
#   10436 tests collected in 25.01s
#   10403/10436 tests collected (33 deselected) in 26.29s
#
# The line is anchored to the start of a line on purpose: a parametrised case
# can carry the words "0 tests collected" inside its id (a failure-classification
# fixture quotes exactly that sentence), and an unanchored search matches the
# node-id list first and reports "0".  The count is the *total*, so the second
# number of the "selected/total" form wins when it is there.
_COLLECTED_COUNT_RE = re.compile(r"^(\d+)(?:/(\d+))? tests collected", re.MULTILINE)


# ---------------------------------------------------------------------------
# The gate script
# ---------------------------------------------------------------------------


def _gate_text() -> str:
    return GATE_SCRIPT.read_text(encoding="utf-8")


def _gate_marker_expressions() -> List[str]:
    """The marker expressions the gate script passes to pytest, in order.

    Comment lines are dropped first: the script documents the marker
    expressions in prose, and a search that reads the comments would
    pick up the *documented* selection sets as if they were the ones
    the gate runs.
    """
    code = "\n".join(
        line for line in _gate_text().splitlines() if not line.lstrip().startswith("#")
    )
    return _MARKER_ARGUMENT_RE.findall(code)


def _run_gate(
    args: Sequence[str],
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run the gate script and return the completed process."""
    merged = dict(os.environ)
    merged.pop("PDT_RUN_TESTS_DRY_RUN", None)
    merged.update(env or {})
    return subprocess.run(
        ["bash", str(GATE_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=merged,
        timeout=300,
    )


# ---------------------------------------------------------------------------
# A fake pytest
# ---------------------------------------------------------------------------

#: A stand-in for pytest: it records the argv it was handed and then exits
#: with the code the test asked for.  ``PDT_STUB_CODES`` holds one exit
#: code per line — the Nth line is used for the Nth call — which is how a
#: test makes one particular phase fail.
_STUB_SOURCE = """#!/bin/sh
printf '%s\\n' "$*" >> "$PDT_STUB_LOG"
if [ -n "$PDT_STUB_CODES" ]; then
    n=$(wc -l < "$PDT_STUB_LOG" | tr -d ' ')
    code=$(sed -n "${n}p" "$PDT_STUB_CODES")
    if [ -n "$code" ]; then
        exit "$code"
    fi
fi
exit "${PDT_STUB_EXIT:-0}"
"""


def _write_stub(tmp_path: Path) -> Path:
    """Write the fake pytest and return its path."""
    stub = tmp_path / "fake_pytest"
    stub.write_text(_STUB_SOURCE, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _stub_env(
    tmp_path: Path,
    stub: Path,
    log: Path,
    exit_code: str = "0",
    codes: Optional[str] = None,
    probe: Optional[str] = None,
) -> Dict[str, str]:
    """The environment that makes the gate run against the fake pytest."""
    env = {
        "PYTEST_BIN": str(stub),
        "PDT_STUB_LOG": str(log),
        "PDT_STUB_EXIT": exit_code,
    }
    if codes is not None:
        codes_path = tmp_path / "codes.txt"
        codes_path.write_text(codes, encoding="utf-8")
        env["PDT_STUB_CODES"] = str(codes_path)
    if probe is not None:
        env["PDT_HOST_TIMING_PROBE"] = probe
    return env


# ---------------------------------------------------------------------------
# The suite, collected once
# ---------------------------------------------------------------------------

_COLLECTION_PLUGIN_NAME = "pdt_contract_collection_dump"

_COLLECTION_PLUGIN_SOURCE = '''\
"""Dump every collected case's node id and markers.

Loaded with ``-p`` so the dump happens while pytest still holds every
collected item — after the ``-m`` filter has run, the deselected cases are
gone and the partition cannot be checked.
"""

import json
import os


def pytest_collection_modifyitems(items):
    payload = [
        {
            "nodeid": item.nodeid,
            "markers": sorted({mark.name for mark in item.iter_markers()}),
        }
        for item in items
    ]
    with open(os.environ["PDT_CONTRACT_COLLECTION_DUMP"], "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
'''


@pytest.fixture(scope="module")
def collected_suite(tmp_path_factory: pytest.TempPathFactory) -> List[Dict[str, Any]]:
    """Every case in the suite, with the markers it carries."""
    workdir = tmp_path_factory.mktemp("gate-collection")
    plugin_dir = workdir / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / (_COLLECTION_PLUGIN_NAME + ".py")).write_text(
        _COLLECTION_PLUGIN_SOURCE, encoding="utf-8"
    )
    dump_path = workdir / "collection.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(plugin_dir), env.get("PYTHONPATH", "")) if part
    )
    env["PDT_CONTRACT_COLLECTION_DUMP"] = str(dump_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(SUITE_ROOT),
            "--collect-only",
            "-q",
            "-q",
            "-p",
            _COLLECTION_PLUGIN_NAME,
            # A marker nothing carries: the expression selects every case,
            # so the dump holds the whole suite rather than one lane.
            "-m",
            "not __pdt_contract_never_used_marker__",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=900,
    )
    assert completed.returncode == 0, (
        "collecting the suite for the partition check failed:\n"
        f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
    )
    assert dump_path.exists(), (
        "the collection plugin did not write its dump; the partition check "
        "would be comparing against nothing"
    )

    payload = json.loads(dump_path.read_text(encoding="utf-8"))
    counts = _COLLECTED_COUNT_RE.findall(completed.stdout)
    assert counts, (
        "could not read pytest's collected count from its output:\n"
        f"{completed.stdout[-2000:]}"
    )
    selected, total = counts[-1]
    collected_total = int(total) if total else int(selected)
    assert len(payload) == collected_total, (
        f"the dump holds {len(payload)} cases but pytest collected "
        f"{collected_total} — the dump was taken after the marker filter had "
        "already deselected cases, so the partition cannot be checked"
    )
    return payload


class _MarkerMatcher:
    """Answer "does this identifier name one of the case's markers?".

    This is what pytest's own ``MarkMatcher`` reduces to for the
    expressions the gate uses: they name markers and never pass marker
    arguments, so membership in the marker-name set is the whole rule.
    """

    def __init__(self, names: Sequence[str]) -> None:
        self._names = set(names)

    def __call__(self, name: str, **kwargs: Any) -> bool:
        return name in self._names


def _expression_selects(expression: str, markers: Sequence[str]) -> bool:
    """Whether ``expression`` selects a case carrying ``markers``."""
    return bool(Expression.compile(expression).evaluate(_MarkerMatcher(markers)))


# ---------------------------------------------------------------------------
# 1. The three phases partition the suite
# ---------------------------------------------------------------------------


@pytest.mark.timeout(900)
def test_the_three_lane_expressions_partition_the_whole_suite(
    collected_suite: List[Dict[str, Any]],
) -> None:
    """Every case is selected by exactly one of the three expressions.

    A filter that lets a case through twice runs it twice — and a filter
    that lets a case through zero times never runs it at all, which looks
    exactly like a green run over an empty room.  Both are silent, so the
    partition is checked against the real collection.
    """
    expressions = _gate_marker_expressions()
    assert expressions == [
        PHASE1_EXPRESSION,
        PHASE2_EXPRESSION,
        PHASE3_EXPRESSION,
    ], (
        "the gate's marker expressions changed; this file pins the partition "
        f"they describe and now sees {expressions!r}"
    )

    counts = {1: 0, 2: 0, 3: 0}
    for item in collected_suite:
        lanes = [
            index
            for index, expression in enumerate(expressions, start=1)
            if _expression_selects(expression, item["markers"])
        ]
        assert len(lanes) == 1, (
            f"{item['nodeid']} carries {item['markers']} and is selected by "
            f"{len(lanes)} of the three phases ({lanes}); the phases must "
            "partition the suite — one phase per case, no case twice"
        )
        counts[lanes[0]] += 1

    assert sum(counts.values()) == len(collected_suite)
    for index, count in counts.items():
        assert count > 0, (
            f"phase {index} selects no case at all; an empty phase is a hole "
            "in the coverage, not a lane that happens to be quiet"
        )
    print({"phase_counts": counts, "total": len(collected_suite)})


@pytest.mark.timeout(900)
def test_the_partition_is_the_one_pytest_itself_would_apply(
    collected_suite: List[Dict[str, Any]],
) -> None:
    """The expressions select the same cases pytest's own ``-m`` does.

    The partition above is computed with pytest's expression engine, so it
    is the same rule pytest applies — but only if the engine is fed the
    same thing pytest feeds it.  This test re-collects one subtree with
    the real ``-m`` option and compares the two answers case by case.

    The subtree is ``backend/tests/integration`` because it is the one
    that carries cases in all three lanes: a subtree that only ever
    lands in lane 1 would compare "selected" against "selected" for the
    other two lanes and prove nothing about them.
    """
    subtree = REPO_ROOT / "backend" / "tests" / "integration"
    expressions = _gate_marker_expressions()
    for index, expression in enumerate(expressions, start=1):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(subtree),
                "--collect-only",
                "-q",
                "-q",
                "-m",
                expression,
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=300,
        )
        # pytest exits 5 when the expression selects nothing; that is a
        # legitimate answer, not a failure of the collection.
        assert completed.returncode in (0, 5), (
            f"collecting {subtree} with -m {expression!r} failed:\n"
            f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
        )
        # ``-q -q`` prints one node id per line; the summary and the
        # warnings do not start with the rootdir-relative test path.
        selected = {
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().startswith("tests/") and "::" in line
        }
        mine = {
            item["nodeid"]
            for item in collected_suite
            if item["nodeid"].startswith("tests/integration/")
            and _expression_selects(expression, item["markers"])
        }
        assert selected == mine, (
            f"phase {index} selects a different set of cases under pytest's "
            f"own -m than under this file's evaluation "
            f"(pytest: {len(selected)}, here: {len(mine)}); "
            f"difference: {sorted(selected ^ mine)[:5]}"
        )
        assert mine, (
            f"phase {index} selects nothing in {subtree.name}; the "
            f"comparison above would have been vacuous for this lane"
        )


# ---------------------------------------------------------------------------
# 2. The lane entry points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", sorted(LANE_PHASES))
def test_each_lane_entry_point_prints_exactly_its_own_argv(lane: str) -> None:
    """``--lane <name>`` runs the phase it names, and says which one."""
    completed = _run_gate(
        ["--lane", lane], env={"PDT_RUN_TESTS_DRY_RUN": "1"}
    )
    assert completed.returncode == 0, (
        f"the dry run of --lane {lane} failed:\n{completed.stdout}\n"
        f"{completed.stderr}"
    )
    printed = [line for line in completed.stdout.splitlines() if line.strip()]
    expected_phases = LANE_PHASES[lane]
    assert len(printed) == len(expected_phases), (
        f"--lane {lane} printed {len(printed)} command(s) for "
        f"{len(expected_phases)} phase(s):\n{completed.stdout}"
    )
    for line, phase in zip(printed, expected_phases):
        tokens = shlex.split(line)
        assert tokens[0].endswith("pytest"), (
            f"the dry run printed {tokens[0]!r} as the command to run"
        )
        assert tokens[1:] == [str(SUITE_ROOT)] + PHASE_ARGUMENTS[phase], (
            f"--lane {lane} printed the wrong argv for phase {phase}:\n{line}"
        )


def test_the_default_lane_runs_all_three_phases() -> None:
    """No ``--lane`` at all means the whole gate, as it always did."""
    completed = _run_gate([], env={"PDT_RUN_TESTS_DRY_RUN": "1"})
    assert completed.returncode == 0
    printed = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(printed) == 3, (
        f"the default invocation printed {len(printed)} command(s):\n"
        f"{completed.stdout}"
    )


def test_an_unknown_lane_is_refused() -> None:
    """A lane name nobody defined must not silently mean "everything"."""
    completed = _run_gate(["--lane", "nonsense"], env={"PDT_RUN_TESTS_DRY_RUN": "1"})
    assert completed.returncode != 0, (
        "an unknown lane was accepted; a typo in a lane name would run the "
        "wrong segment of the gate"
    )


def _gate_summary(stdout: str) -> List[str]:
    """The lines of the gate's ``=== gate summary ===`` block.

    The block is what a reader looks at to find out which lane decided
    what, so its contents are worth asserting on directly rather than
    grepping the whole output for a word.
    """
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "=== gate summary ===":
            return lines[index + 1 :]
    raise AssertionError(
        "the gate printed no summary block:\n" + stdout
    )


# ---------------------------------------------------------------------------
# 3. An unmeasurable host is INCONCLUSIVE, not a failure
# ---------------------------------------------------------------------------


def test_the_gate_reports_inconclusive_when_the_host_cannot_be_measured(
    tmp_path: Path,
) -> None:
    """A wall-clock failure on an unmeasurable host is INCONCLUSIVE.

    The host cannot time a sleep, so a failing wall-clock case does not
    tell us anything about the code.  Reporting 1 would blame the code for
    the machine.

    The wall-clock phase still RUNS: skipping it would make the gate's
    coverage a property of the host's load, which is the root cause the
    round-4 report names (§0.2) — the gate's conclusion was bound to the
    host's runtime environment rather than to the code under test.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "all"],
        env=_stub_env(
            tmp_path,
            stub,
            log,
            codes="0\n0\n1\n",
            probe="fail"),
    )
    assert completed.returncode == 3, (
        f"a wall-clock failure on an unmeasurable host produced exit code "
        f"{completed.returncode}, not 3:\n" + completed.stdout
        + completed.stderr
    )
    assert "INCONCLUSIVE" in completed.stdout, (
        "the gate exited 3 without saying why:\n" + completed.stdout
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3, (
        "the wall-clock phase did not run; the stub was called "
        f"{len(calls)} time(s) instead of 3. A phase that is skipped is a "
        "phase whose cases never ran — the gate's coverage would depend on "
        "the host's load\n" + completed.stdout
    )


def test_a_green_wall_clock_phase_is_green_on_a_busy_host(
    tmp_path: Path,
) -> None:
    """The host's noise does not make a passing lane red.

    This is the other half of the same contract: the gate's verdict has to
    be a property of the code.  A host that cannot time a sleep is still
    able to run cases that pass, and the gate has to say so — otherwise no
    verification host that is busy can ever see a green gate, and the
    wall-clock cases never run at all.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "all"],
        env=_stub_env(
            tmp_path,
            stub,
            log,
            probe="fail"),
    )
    assert completed.returncode == 0, (
        f"a run whose every phase passed produced exit code "
        f"{completed.returncode}, not 0, on a host the probe found "
        "unmeasurable:\n" + completed.stdout + completed.stderr
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3, (
        f"the stub was called {len(calls)} time(s) instead of 3 — a phase "
        "was skipped because of the host's load\n" + completed.stdout
    )
    assert "host timing probe" in completed.stdout, (
        "the gate did not say what the probe measured:\n" + completed.stdout
    )


def test_the_summary_does_not_claim_a_phase_that_never_ran_passed(
    tmp_path: Path,
) -> None:
    """A phase that did not run must not be reported as passed.

    A lane that was not selected has no verdict at all.  A line reading
    "passed" is a claim about a lane that was never executed, and the exit
    code alone does not repair it — whoever reads the summary sees a green
    lane.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "unit"],
        env=_stub_env(
            tmp_path,
            stub,
            log),
    )
    assert completed.returncode == 0, (
        f"the unit lane produced exit code {completed.returncode}:\n"
        + completed.stdout + completed.stderr
    )
    summary = _gate_summary(completed.stdout)
    for phase in ("phase 2/3", "phase 3/3"):
        lines = [line for line in summary if phase in line]
        assert lines, (
            f"the summary does not mention {phase} at all:\n"
            + completed.stdout
        )
        verdict = lines[0].split(":", 1)[-1].strip()
        assert verdict != "passed", (
            f"the summary reports {phase} as passed, but that lane was not "
            "selected and never ran:\n" + completed.stdout
        )
        assert "not selected" in verdict, (
            f"the summary does not say that {phase} was not selected:\n"
            + completed.stdout
        )


def test_the_gate_reports_a_failure_when_a_phase_fails(tmp_path: Path) -> None:
    """A failing phase is a code failure: exit code 1."""
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "all"],
        env=_stub_env(
            tmp_path,
            stub,
            log, exit_code="1", probe="pass"),
    )
    assert completed.returncode == 1, (
        f"a failing phase produced exit code {completed.returncode}, not 1:\n"
        + completed.stdout
        + completed.stderr
    )
    assert "INCONCLUSIVE" not in completed.stdout, (
        "a code failure was reported as inconclusive:\n" + completed.stdout
    )


def test_a_phase_that_reports_inconclusive_does_not_fail_the_gate(
    tmp_path: Path,
) -> None:
    """Exit code 3 from a phase means "undecided", not "failed"."""
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "all"],
        env=_stub_env(
            tmp_path,
            stub,
            log, exit_code="3", probe="pass"),
    )
    assert completed.returncode == 3, (
        f"a phase reporting exit code 3 produced gate exit "
        f"{completed.returncode}:\n" + completed.stdout
    )
    assert "INCONCLUSIVE" in completed.stdout


# ---------------------------------------------------------------------------
# 4. The phase codes are still combined with AND
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "codes,expected",
    [
        ("1\n0\n0\n", 1),
        ("0\n1\n0\n", 1),
        ("0\n0\n1\n", 1),
        ("0\n0\n0\n", 0),
    ],
)
def test_the_gate_exit_code_is_the_logical_and_of_the_phases(
    tmp_path: Path, codes: str, expected: int
) -> None:
    """One failing phase fails the gate; no failing phase leaves it green.

    The AND is what keeps a later phase from overwriting an earlier
    failure with its own success — a gate that reported the last phase's
    code would report "green" for a run whose first phase failed.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "all"],
        env=_stub_env(
            tmp_path,
            stub,
            log, codes=codes, probe="pass"),
    )
    assert completed.returncode == expected, (
        f"phase codes {codes!r} produced gate exit {completed.returncode}, "
        f"expected {expected}:\n" + completed.stdout
    )


def test_the_host_probe_reports_a_verdict_on_this_host(tmp_path: Path) -> None:
    """The probe measures this host and says what it found.

    No verdict is forced here: the probe's own measurement decides whether
    the wall-clock phase runs.  Either outcome is a legitimate verdict —
    what is not legitimate is silence.
    """
    stub = _write_stub(tmp_path)
    log = tmp_path / "calls.log"
    completed = _run_gate(
        ["--lane", "wallclock"],
        env=_stub_env(
            tmp_path,
            stub,
            log),
    )
    assert completed.returncode in (0, 3), (
        f"the wall-clock lane produced exit code {completed.returncode}; "
        "the only verdicts it can give are 0 (measured and green) and 3 "
        "(the host could not be measured):\n" + completed.stdout
    )
    assert (
        "host timing probe" in completed.stdout
        or "INCONCLUSIVE" in completed.stdout
    ), (
        "the gate ran the wall-clock lane without saying whether the host "
        "could be measured:\n" + completed.stdout
    )
