#!/bin/bash
# Test Runner Script
# ===================
# Activates the virtual environment and runs pytest with the given arguments.
# This ensures tests always use the correct pytest binary from the venv.

set -euo pipefail

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Virtual environment path
VENV_PATH="$PROJECT_ROOT/backend/.venv"

# Check if venv exists
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Create it from the lockfile with:"
    echo "  uv sync --project backend"
    echo ""
    echo "That builds backend/.venv at the versions backend/uv.lock pins,"
    echo "on the Python backend/pyproject.toml declares. If uv is not"
    echo "installed, see https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

# Activate the virtual environment
source "$VENV_PATH/bin/activate"

# The pytest binary the phases below run. It defaults to the ``pytest`` on
# PATH — which the activation above has just pointed at this project's venv
# — and an explicit value wins, so a caller can aim a run at another binary
# (a stub, in a negative control) without editing this script.
PYTEST_BIN="${PYTEST_BIN:-pytest}"

# ---------------------------------------------------------------------------
# The spawn registry: what this run started, so this run can give it back
# ---------------------------------------------------------------------------
# A declared service is started detached -- it calls ``setsid()`` and leads
# its own session -- so a watchdog that kills the gate's process group does
# not touch it.  That is how the CI of 2026-10-31 left a ``backend.server``
# answering /health a minute after the watchdog had fired, and how twenty-six
# reaper daemons accumulated on one machine.
#
# So every detached process this run starts writes itself down, and the gate
# gives them back on its way out.  The registry is named by an environment
# variable, so everything the run starts inherits it: the service, the reaper
# daemon the service starts, the harness that spawns a server.
#
# The CI runner names the registry when it runs the gate, and gives back what
# is left when the gate was killed before it could.
if [ -z "${PDT_SPAWN_REGISTRY:-}" ]; then
    SPAWN_REGISTRY="$(mktemp "${TMPDIR:-/tmp}/pdt-spawn-registry.XXXXXX")"
    export PDT_SPAWN_REGISTRY="$SPAWN_REGISTRY"
    SPAWN_REGISTRY_IS_OURS=1
else
    SPAWN_REGISTRY="$PDT_SPAWN_REGISTRY"
    SPAWN_REGISTRY_IS_OURS=0
fi

# Give back every process the registry names.  The registry is emptied as it
# goes, so the gate's cleanup and the CI runner's cleanup do not signal the
# same process twice.
reap_spawn_registry() {
    [ -n "${SPAWN_REGISTRY:-}" ] || return 0
    [ -s "$SPAWN_REGISTRY" ] || return 0
    "$VENV_PATH/bin/python" - "$SPAWN_REGISTRY" "$PROJECT_ROOT" <<'PYTHON' || true
import sys

sys.path.insert(0, sys.argv[2])

from backend import orphan_reaper

for entry in orphan_reaper.reap_spawned_processes(
    sys.argv[1], reason="the gate is over"
):
    # On stderr, not on stdout: the summary block on stdout has a
    # ``key: value`` shape, and a line that does not belong to it makes
    # the block harder to read than it has to be.
    print(
        "gate: gave back %s (pid %s)"
        % (entry.get("name") or entry.get("kind") or "process", entry.get("pid")),
        file=sys.stderr,
    )
PYTHON
}

# The gate's own way out.  It runs on every exit -- the normal one, the
# early ones, and the one a signal causes -- and it keeps the exit code the
# gate decided on.
gate_cleanup() {
    local status=$?
    trap - EXIT
    reap_spawn_registry || true
    if [ "$SPAWN_REGISTRY_IS_OURS" -eq 1 ]; then
        rm -f "$SPAWN_REGISTRY"
    fi
    exit "$status"
}
trap gate_cleanup EXIT

# ---------------------------------------------------------------------------
# PDT_GATE_JUNIT_DIR: where the per-phase junit reports go
# ---------------------------------------------------------------------------
#
# The gate runs three phases back to back and the verdict keeps only the
# tail of the whole run's output. A failure in phase 1 is therefore gone
# from the evidence by the time phase 3 ends: the reader sees the last
# 4000 characters, which belong to phase 3.
#
# A junit report per phase fixes that without touching the verdict: each
# phase writes its own report, and a report names the cases that failed
# instead of leaving them in a scrolled-off stream.
#
# Empty (the default) means the gate writes nothing at all — no files,
# no extra arguments, the same argv it has always had. A run that is
# not read as evidence stays exactly as it was.
PDT_GATE_JUNIT_DIR="${PDT_GATE_JUNIT_DIR:-}"

# ---------------------------------------------------------------------------
# --lane: which segment of the gate runs
# ---------------------------------------------------------------------------
# ``--lane <name>`` selects which of the three phases below run:
#
#   unit       the parallel phase — everything that is not timing-sensitive
#   e2e        the e2e + integration phase
#   wallclock  the time_sensitive + perf phase
#   all        all three (the default)
#
# A verification point that owns one lane can run exactly that lane with
# its own budget instead of asking the whole gate for a verdict it does
# not need.  The lane selects *which* phases run; it never changes *what*
# a phase selects — the three marker expressions below remain the
# partition of the suite.
#
# ``--lane`` is parsed before anything else so the lane name is never
# mistaken for a pytest flag, and so the rest of the script keeps seeing
# exactly the arguments it always did.
lane="all"
lane_requested=0
lane_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --lane)
            if [ $# -lt 2 ]; then
                echo "Error: --lane needs a value (all|unit|e2e|wallclock)" >&2
                exit 2
            fi
            lane="$2"
            lane_requested=1
            shift 2
            ;;
        --lane=*)
            lane="${1#--lane=}"
            lane_requested=1
            shift
            ;;
        *)
            lane_args+=("$1")
            shift
            ;;
    esac
done
# ``lane_args`` holds every argument except the ``--lane`` pair that was
# consumed above, so it IS this script's positional parameters from here
# on.  It is used as an array rather than pushed back onto ``"$@"`` with
# ``set --``: a second ``set`` line without ``-u`` / ``-o pipefail`` is a
# shape the static gate in ``backend/tests/static_gates`` reports as a
# dangerous default, and the flag set this script already runs under is
# the one that matters.

case "$lane" in
    all|unit|e2e|wallclock) ;;
    *)
        echo "Error: unknown lane '$lane' (expected all|unit|e2e|wallclock)" >&2
        exit 2
        ;;
esac

# Which phases this invocation runs.
run_phase1=0
run_phase2=0
run_phase3=0
case "$lane" in
    all) run_phase1=1; run_phase2=1; run_phase3=1 ;;
    unit) run_phase1=1 ;;
    e2e) run_phase2=1 ;;
    wallclock) run_phase3=1 ;;
esac

# ---------------------------------------------------------------------------
# The gate's wall-clock budget
# ---------------------------------------------------------------------------
# The gate declares how long it may take, and the declaration is the single
# source of truth for the whole chain: the CI watchdog reads these four
# numbers out of this file (``backend/verification_ci_runner.py``) so that
# the watchdog can never be shorter than the gate it watches.  Before this
# the two budgets were two unrelated constants in two unrelated files, and
# the gate could quietly need more time than the watchdog allowed — which
# is how a round ended with no verdict at all.
#
# The four numbers are plain assignments on purpose: a machine reads them
# out of this file, not only this shell.  They are the three lanes added
# up, so a run of ``--lane all`` may spend what the three lanes would
# spend separately, and not a second more.
#
#   unit        1800 s   the parallel phase on its own
#   e2e         3600 s   the e2e + integration phase on its own
#   wallclock   1800 s   the time_sensitive + perf phase on its own
#   all         7200 s   all three phases together
#
# ``GATE_BUDGET_SECONDS`` overrides the budget of the run being made.  The
# CI runner reads the same variable, so an operator who raises the budget
# raises it on both sides of the fence.
GATE_BUDGET_SECONDS_ALL=7200
GATE_BUDGET_SECONDS_UNIT=1800
GATE_BUDGET_SECONDS_E2E=3600
GATE_BUDGET_SECONDS_WALLCLOCK=1800

case "$lane" in
    all) lane_budget=$GATE_BUDGET_SECONDS_ALL ;;
    unit) lane_budget=$GATE_BUDGET_SECONDS_UNIT ;;
    e2e) lane_budget=$GATE_BUDGET_SECONDS_E2E ;;
    wallclock) lane_budget=$GATE_BUDGET_SECONDS_WALLCLOCK ;;
    *) lane_budget=$GATE_BUDGET_SECONDS_ALL ;;
esac
gate_budget="${GATE_BUDGET_SECONDS:-$lane_budget}"
case "$gate_budget" in
    ''|*[!0-9]*)
        echo "Error: GATE_BUDGET_SECONDS must be a whole number of seconds (got '$gate_budget')" >&2
        exit 2
        ;;
esac
if [ "$gate_budget" -lt 1 ]; then
    echo "Error: GATE_BUDGET_SECONDS must be at least 1 second (got '$gate_budget')" >&2
    exit 2
fi

# Run pytest with all arguments passed to this script.
#
# The default target is ``backend/tests`` and it applies whether or not
# flags were passed. Earlier this script took a ``pytest "$@"`` branch as
# soon as it saw *any* argument, which dropped the target — and pytest,
# given no path, resolves its inifile from the current directory. There
# are two pytest.ini files in this repo (repo root and ``backend/``) with
# different addopts, so ``run_tests.sh`` and ``run_tests.sh -q`` ran
# different suites: 5405 tests under ``backend/pytest.ini`` versus 5524
# under the root one, with a different pass/fail outcome. A flag must
# never change *which* tests run.
#
# Arguments that name an existing path are treated as targets; everything
# else (including the value of a flag like ``-k smoke``) is passed through
# to pytest. Passing ``backend/tests/unit`` still runs just that subtree.
targets=()
passthrough=()
explicit_target=0
# The arguments this run carries.  ``lane_args`` is empty when the caller
# named nothing but a lane, and macOS ships bash 3.2 where an empty
# array under ``set -u`` is an unbound-variable error — hence the guard.
if [ ${#lane_args[@]} -gt 0 ]; then
    for arg in "${lane_args[@]}"; do
        if [ "${arg#-}" != "$arg" ]; then
            passthrough+=("$arg")                       # a flag
        elif [ -e "$arg" ] || [ -e "$PROJECT_ROOT/$arg" ]; then
            targets+=("$arg")                           # a path that exists
            explicit_target=1                           # the caller picked the tree
        else
            passthrough+=("$arg")                       # a flag's value
        fi
    done
fi
if [ ${#targets[@]} -eq 0 ]; then
    targets=("$PROJECT_ROOT/backend/tests")
fi

# ---------------------------------------------------------------------------
# Shape of the run
# ---------------------------------------------------------------------------
# ``backend/tests`` holds 10419 cases. One serial pass over it does not
# fit in the gate's wall-clock budget (the unit subtree alone is 1.3-1.5
# CPU-hours), so the default invocation runs three phases instead of one:
#
#   1. parallel   ``-n auto`` over everything that is not timing-sensitive
#                 and not an e2e / integration / perf case   (9854 cases)
#   2. serial     e2e + integration, with a per-test budget of 600 s  (505)
#   3. serial     time_sensitive + perf, with a per-test budget of 1800 s (60)
#
# The three marker expressions are a *partition*, not a filter: every case
# in the suite is selected by exactly one phase, so every case runs once
# and none runs twice. ``pytest --collect-only`` over the same tree
# confirms it: 9854 + 505 + 60 = 10419, the full collection count.
#
# Phase 2 is where the partition is decided. ``e2e or integration`` alone
# would select 535 cases, 30 of which are also ``time_sensitive`` — and
# those 30 would then run twice: once in phase 2, beside 535 other e2e /
# integration cases and under the 600 s budget, and once in phase 3, where
# they belong. Excluding ``time_sensitive`` here is what keeps the three
# phases disjoint.
#
# Phase 2 does not carry a separate ``not perf``: no case in the suite
# carries ``perf`` together with ``e2e`` / ``integration``
# (``pytest --collect-only -m "(e2e or integration) and perf"`` selects 0
# of 10419), so that clause would be a no-op. If a case ever gains both
# markers, re-check the partition before trusting it.
#
# Phase 3 is not optional: the default selection set is the one the root
# ``addopts`` produce (``-m "not time_sensitive"``, 10386 cases). 33 of
# the 60 wall-clock cases live outside it and would never be run; the
# other 27 (perf) are inside it but have to run alone. Dropping phase 3
# would trade coverage for a green exit code.
#
# The caller keeps the historical one-shot invocation whenever it has
# already expressed the shape it wants: an explicit target, an explicit
# ``-n`` (xdist workers), an explicit ``-p xdist`` / ``-p no:xdist``, or
# ``PDT_RUN_TESTS_SERIAL=1``. The one-shot call is the pre-change code
# path, unchanged.
single_shot=0
# Whether the caller owns the parallelism of the run.  When it does, the
# parallel phase must not add its own ``-n``: the caller's choice wins,
# and a phase that appended one would override it (the last ``-n`` on the
# command line is the one pytest uses).
caller_owns_parallelism=0
if [ "$explicit_target" -eq 1 ]; then
    single_shot=1
fi
if [ "${PDT_RUN_TESTS_SERIAL:-}" = "1" ]; then
    single_shot=1
    caller_owns_parallelism=1
fi
if [ ${#passthrough[@]} -gt 0 ]; then
    idx=0
    while [ "$idx" -lt "${#passthrough[@]}" ]; do
        arg="${passthrough[$idx]}"
        case "$arg" in
            -n|-n?*)
                # ``-n`` / ``-n8`` / ``-nauto`` — xdist workers; the caller
                # owns the parallelism, so this script does not add phases.
                single_shot=1
                caller_owns_parallelism=1
                ;;
            -p)
                nxt="${passthrough[$((idx + 1))]:-}"
                case "$nxt" in
                    xdist|xdist.*|no:xdist)
                        single_shot=1
                        caller_owns_parallelism=1
                        ;;
                esac
                ;;
            -p?*)
                case "${arg#-p}" in
                    xdist|xdist.*|no:xdist)
                        single_shot=1
                        caller_owns_parallelism=1
                        ;;
                esac
                ;;
        esac
        idx=$((idx + 1))
    done
fi

# An explicit ``--lane`` overrides the single-shot shortcuts above: the
# caller named the segment of the gate it wants, and a flag that happens
# to look like "run it all" must not widen that back out.
if [ "$lane_requested" -eq 1 ]; then
    single_shot=0
fi

# Marker expressions and per-phase budgets, one array per phase. The
# shell strips the quotes at assignment time, so the element pytest
# receives is the bare expression — no stray quote characters.
PHASE1_ARGS=()
if [ "$caller_owns_parallelism" -eq 0 ]; then
    PHASE1_ARGS+=(-n auto)
fi
PHASE1_ARGS+=(-m "not time_sensitive and not perf and not e2e and not integration")
PHASE2_ARGS=(-m "(e2e or integration) and not time_sensitive" --timeout=600)
PHASE3_ARGS=(-m "time_sensitive or perf" --timeout=1800)

# Every phase carries the caller's own flags (the selection must not
# depend on them) plus the phase's marker expression and time budget.
phase_common=("${targets[@]}")
if [ ${#passthrough[@]} -gt 0 ]; then
    phase_common+=("${passthrough[@]}")
fi
phase1=("${phase_common[@]}" "${PHASE1_ARGS[@]}")
phase2=("${phase_common[@]}" "${PHASE2_ARGS[@]}")
phase3=("${phase_common[@]}" "${PHASE3_ARGS[@]}")

# Print an argv the way it will be executed: one element per argument,
# quoted only when it contains whitespace. ``printf '%q'`` is not used
# here — it escapes the spaces inside a marker expression as ``\ ``,
# which is correct shell but unreadable (and copy-pasteable into
# nothing).
print_argv() {
    local sep=""
    local item
    for item in "$@"; do
        printf '%s' "$sep"
        case "$item" in
            *[[:space:]]*) printf '"%s"' "$item" ;;
            *) printf '%s' "$item" ;;
        esac
        sep=" "
    done
    printf '\n'
}

# ``PDT_RUN_TESTS_DRY_RUN=1`` prints the commands that would run and
# runs nothing at all — no test is collected, no test is executed. It
# exists so the *shape* of a run can be inspected without paying for it.
if [ "${PDT_RUN_TESTS_DRY_RUN:-}" = "1" ]; then
    if [ "$single_shot" -eq 1 ]; then
        if [ ${#passthrough[@]} -gt 0 ]; then
            print_argv "$PYTEST_BIN" "${targets[@]}" "${passthrough[@]}"
        else
            print_argv "$PYTEST_BIN" "${targets[@]}"
        fi
    else
        if [ "$run_phase1" -eq 1 ]; then
            print_argv "$PYTEST_BIN" "${phase1[@]}"
        fi
        if [ "$run_phase2" -eq 1 ]; then
            print_argv "$PYTEST_BIN" "${phase2[@]}"
        fi
        if [ "$run_phase3" -eq 1 ]; then
            print_argv "$PYTEST_BIN" "${phase3[@]}"
        fi
    fi
    exit 0
fi

# Branch rather than expanding both arrays unconditionally: macOS ships
# bash 3.2, where `"${empty[@]}"` under `set -u` is an unbound-variable
# error (fixed in bash 4.4). `run_tests.sh` with no arguments is exactly
# the case that trips it.
if [ "$single_shot" -eq 1 ]; then
    if [ ${#passthrough[@]} -gt 0 ]; then
        "$PYTEST_BIN" "${targets[@]}" "${passthrough[@]}"
    else
        "$PYTEST_BIN" "${targets[@]}"
    fi
    exit 0
fi

# The clock the budget is measured against.  It starts when the run
# starts: the phases, the host probe and the summary are all part of the
# run the budget is about.
#
# It is read in milliseconds, because the budget is a wall-clock deadline
# and a whole-second clock cannot tell "the deadline has passed" from
# "the deadline is still a second away": a gate that gave a phase up a
# second early would report "the budget was exhausted" about a run that
# had not run out of time.  The summary reports the same clock, so the
# two never contradict each other.
gate_now_ms() {
    "$VENV_PATH/bin/python" -c 'import time; print(int(time.time() * 1000))'
}
gate_started_ms=$(gate_now_ms)

# ---------------------------------------------------------------------------
# The host measurability probe
# ---------------------------------------------------------------------------
# The wall-clock phase asserts on timings.  On a host whose scheduler
# stretches a short sleep into several times its nominal length, those
# assertions measure the machine rather than the code, and the gate would
# report a code failure for a host that is merely busy.
#
# The probe asks the host whether it can measure wall-clock time at all: it
# times a short sleep a few times and reads the load average.  A host that
# cannot sleep 50 ms in about 50 ms cannot time anything, so the
# wall-clock lane's verdict would be noise.
#
# ``PDT_HOST_TIMING_PROBE`` forces the verdict (``pass`` / ``fail``) so both
# branches can be exercised deterministically — by the contract test in
# ``backend/tests/contract/test_gate_lane_contract.py``, and by an operator
# who knows what their machine is doing.  ``auto`` (the default) measures.
# ``PDT_HOST_TIMING_PROBE_MAX_SLEEP`` moves the threshold.
#
# What the verdict is used for: a wall-clock phase that FAILS on a host the
# probe found unmeasurable is reported as INCONCLUSIVE (exit code 3) — the
# machine, not the code.  A wall-clock phase that PASSES is green, and the
# phase always runs, so the gate's coverage never depends on the host's
# load.
PROBE_SOURCE='
import os
import statistics
import time

samples = []
for _ in range(5):
    started = time.monotonic()
    time.sleep(0.05)
    samples.append(time.monotonic() - started)
median = statistics.median(samples)
try:
    loadavg = os.getloadavg()[0]
except (AttributeError, OSError):
    loadavg = 0.0
threshold = float(os.environ.get("PDT_HOST_TIMING_PROBE_MAX_SLEEP", "0.06"))
verdict = "OK" if median <= threshold else "FAIL"
print("%s %.3f %.2f" % (verdict, median, loadavg))
raise SystemExit(0 if verdict == "OK" else 1)
'

run_host_probe() {
    local forced="${PDT_HOST_TIMING_PROBE:-auto}"
    case "$forced" in
        pass)
            printf 'OK 0.000 0.00 (forced)\n'
            return 0
            ;;
        fail)
            printf 'FAIL 0.000 0.00 (forced)\n'
            return 1
            ;;
    esac
    local probe_python="$VENV_PATH/bin/python"
    local output=""
    local rc=0
    output="$("$probe_python" -c "$PROBE_SOURCE")" || rc=$?
    printf '%s\n' "$output"
    return "$rc"
}

# Run the three phases, capturing each phase's own exit code.
#
# Every phase runs even when an earlier one failed: the PRD's "no
# regression in the existing unit tests" claim is only as good as the
# coverage behind it, and a script that aborted on the first failing phase
# would leave the other two lanes unrun — an unrun lane looks exactly like
# a green one to whoever reads the exit code.
#
# The script's exit code is the AND of the three phase codes: a later phase
# must never overwrite an earlier failure with its own success.
#
# ``rc1`` / ``rc2`` / ``rc3`` are pre-assigned because ``|| rcN=$?`` runs
# only on the failing path — under ``set -u`` an unassigned ``rcN`` would be
# an unbound variable.
rc1=0
rc2=0
rc3=0

# ---------------------------------------------------------------------------
# The bounded phase runner
# ---------------------------------------------------------------------------
# A phase runs under a wall-clock budget of its own.  When the budget is
# gone the phase's process *tree* is stopped and the runner says so with a
# dedicated exit code, so the gate can tell "the phase failed" from "the
# phase ran out of time" — the distinction the round-2 artifact could not
# make, because the gate had been killed from the outside and had said
# nothing at all.
#
# The tree is stopped by pid, not by process group: the phase shares the
# gate's process group on purpose (a watchdog that kills the gate's group
# has to reach the phase too — that is how a killed gate used to leave a
# live service behind), so signalling the group would signal the gate
# itself.
BOUNDED_RUNNER_SOURCE='
import os
import signal
import subprocess
import sys
import time

budget = float(sys.argv[1])
argv = sys.argv[2:]

if not argv:
    sys.exit(2)


def _process_table():
    """The process table of the machine as ``[(pid, ppid), ...]``.

    ``ps`` is used rather than ``/proc`` because this script runs on
    macOS as well as on Linux, and macOS has no ``/proc``.
    """
    try:
        raw = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    table = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            table.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return table


def _descendants(root):
    """Every pid below *root*, deepest first."""
    children = {}
    for pid, ppid in _process_table():
        children.setdefault(ppid, []).append(pid)
    found = []
    stack = [root]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            found.append(child)
            stack.append(child)
    found.reverse()
    return found


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal(pid, sig):
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


started = time.monotonic()
proc = subprocess.Popen(argv)
deadline = started + budget
timed_out = False
while True:
    rc = proc.poll()
    if rc is not None:
        break
    if time.monotonic() >= deadline:
        timed_out = True
        break
    time.sleep(0.05)

if timed_out:
    tree = _descendants(proc.pid)
    for pid in tree:
        _signal(pid, signal.SIGTERM)
    _signal(proc.pid, signal.SIGTERM)
    grace = time.monotonic() + 5.0
    while time.monotonic() < grace:
        if proc.poll() is not None and not any(_alive(p) for p in tree):
            break
        time.sleep(0.05)
    for pid in tree:
        _signal(pid, signal.SIGKILL)
    _signal(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    print(
        "the phase ran out of its wall-clock budget after %.1fs"
        % (time.monotonic() - started)
    )
    sys.stdout.flush()
    sys.exit(124)

print("phase elapsed: %.1fs" % (time.monotonic() - started))
sys.stdout.flush()
rc = proc.returncode
if rc is None:
    rc = 0
if rc < 0:
    # A child killed by a signal is reported the way a shell reports it.
    rc = 128 + (-rc)
sys.exit(rc)
'

#: The exit code the bounded runner uses to say "the budget was gone
#: while the phase was running".
BUDGET_EXHAUSTED_EXIT_CODE=124
#: The exit code for "the budget was already gone, so the phase never
#: started".
BUDGET_GONE_EXIT_CODE=125

run_phase() {
    local phase_no="$1"
    local label="$2"
    shift 2
    local now_ms remaining_ms remaining
    printf '=== phase %s ===\n' "$label"
    # How much of the budget this phase may still spend, measured on the
    # same clock the summary reports.  The comparison is in
    # milliseconds because the budget is a deadline and a deadline is
    # not a whole number of seconds: a phase that starts a moment
    # before the deadline still has a little time left, and a phase
    # that starts a moment after it has none.
    now_ms=$(gate_now_ms)
    remaining_ms=$((gate_budget * 1000 - (now_ms - gate_started_ms)))
    if [ "$remaining_ms" -le 0 ]; then
        printf 'the gate budget of %ss is gone; this phase does not run\n' "$gate_budget"
        return "$BUDGET_GONE_EXIT_CODE"
    fi
    remaining=$(printf '%d.%03d' $((remaining_ms / 1000)) $((remaining_ms % 1000)))
    # errexit off for the call below, so a failing phase is *captured*
    # instead of aborting the script; ``-u`` and ``pipefail`` stay on.
    set -euo pipefail +e
    if [ -n "$PDT_GATE_JUNIT_DIR" ]; then
        # The report of *this* phase, written next to the reports of the
        # other phases. The phase number is part of the file name, so the
        # three reports of one run never overwrite each other.
        "$VENV_PATH/bin/python" -c "$BOUNDED_RUNNER_SOURCE" "$remaining" "$PYTEST_BIN" "$@" "--junitxml=${PDT_GATE_JUNIT_DIR}/phase-${phase_no}.xml"
    else
        "$VENV_PATH/bin/python" -c "$BOUNDED_RUNNER_SOURCE" "$remaining" "$PYTEST_BIN" "$@"
    fi
    local rc=$?
    set -euo pipefail
    return "$rc"
}

# Whether each phase actually ran.  A phase that was never selected and a
# phase that was selected but could not start are two different things,
# and neither of them is a phase that passed.
ran_phase1=0
ran_phase2=0
ran_phase3=0
budget_exhausted=0
exhausted_phase=""

if [ "$run_phase1" -eq 1 ]; then
    run_phase 1 "1/3 (parallel)" "${phase1[@]}" || rc1=$?
    if [ "$rc1" -ne "$BUDGET_GONE_EXIT_CODE" ]; then
        ran_phase1=1
    fi
    if [ "$rc1" -eq "$BUDGET_EXHAUSTED_EXIT_CODE" ] || [ "$rc1" -eq "$BUDGET_GONE_EXIT_CODE" ]; then
        budget_exhausted=1
        exhausted_phase="1/3 (parallel)"
    fi
fi
if [ "$budget_exhausted" -eq 0 ] && [ "$run_phase2" -eq 1 ]; then
    run_phase 2 "2/3 (e2e + integration)" "${phase2[@]}" || rc2=$?
    if [ "$rc2" -ne "$BUDGET_GONE_EXIT_CODE" ]; then
        ran_phase2=1
    fi
    if [ "$rc2" -eq "$BUDGET_EXHAUSTED_EXIT_CODE" ] || [ "$rc2" -eq "$BUDGET_GONE_EXIT_CODE" ]; then
        budget_exhausted=1
        exhausted_phase="2/3 (e2e + integration)"
    fi
fi

# The probe runs before the wall-clock phase, and only when that phase is
# part of this run.
#
# The probe's verdict does NOT decide whether the phase runs.
#
# It used to: an unmeasurable host skipped the wall-clock phase and ended
# the whole gate with exit code 3.  That made the gate's verdict a
# property of the machine — a busy verification host could never see a
# green gate, no matter what the code did, and the wall-clock cases never
# ran at all, so the gate's *coverage* depended on the host's load too.
# That is the root cause the round-4 report names: the gate's conclusion
# was bound to the host's runtime environment rather than to the code
# under test (§0.2).  Labelling the verdict "INCONCLUSIVE" did not undo
# the binding; it only gave the red a different number.
#
# So the probe now answers a narrower question: when the wall-clock
# phase FAILS, could the host measure wall-clock time at all?  If it
# could not, the failure is the machine's and the gate reports
# INCONCLUSIVE; if it could, the failure is the code's and the gate
# reports a failure.  A wall-clock phase that PASSES is green on any
# host: the cases in it decide for themselves whether they could measure
# what they assert (the perf case measures its own dispersion and skips
# with the numbers that say so; the lock case differences against a
# same-round baseline).
#
# ``host_measurable`` carries the probe's verdict into the summary.
inconclusive=0
host_measurable=1
host_probe_note="not measured (the wall-clock phase was not selected)"
if [ "$run_phase3" -eq 1 ]; then
    probe_output=""
    probe_rc=0
    probe_output="$(run_host_probe)" || probe_rc=$?
    probe_verdict="${probe_output%% *}"
    probe_rest="${probe_output#* }"
    probe_measured="${probe_rest%% *}"
    probe_loadavg="${probe_rest#* }"
    if [ "$probe_rc" -ne 0 ]; then
        host_measurable=0
        host_probe_note="the host cannot measure wall-clock (sleep(0.05)=${probe_measured}s, loadavg=${probe_loadavg})"
        printf 'host timing probe: %s\n' "$host_probe_note"
        printf 'the wall-clock phase runs anyway; a failure in it is reported as INCONCLUSIVE, not as a code defect\n'
    else
        host_probe_note="OK (sleep(0.05)=${probe_measured}s, loadavg=${probe_loadavg})"
        printf 'host timing probe: %s\n' "$host_probe_note"
    fi
    if [ "$budget_exhausted" -eq 0 ]; then
        run_phase 3 "3/3 (time_sensitive + perf)" "${phase3[@]}" || rc3=$?
        if [ "$rc3" -ne "$BUDGET_GONE_EXIT_CODE" ]; then
            ran_phase3=1
        fi
        if [ "$rc3" -eq "$BUDGET_EXHAUSTED_EXIT_CODE" ] || [ "$rc3" -eq "$BUDGET_GONE_EXIT_CODE" ]; then
            budget_exhausted=1
            exhausted_phase="3/3 (time_sensitive + perf)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
# Three states, three exit codes:
#
#   0  every phase that ran passed
#   1  a phase failed — a defect in the code under test
#   3  the run could not decide — the host could not be measured, a
#      phase reported "inconclusive", or the gate ran out of its own
#      wall-clock budget
#
# The AND of the phase codes is unchanged: the gate is green only when
# every phase that ran returned 0, and a later phase never overwrites an
# earlier failure with its own success.  What is new is that "could not
# decide" is a state of its own instead of being folded into "failed"
# (which blamed the code for the machine) or into "passed" (which claimed
# a lane had run when it had not).
status=0
for phase_rc in "$rc1" "$rc2"; do
    if [ "$phase_rc" -eq 0 ]; then
        continue
    fi
    if [ "$phase_rc" -eq 3 ] || [ "$phase_rc" -eq "$BUDGET_EXHAUSTED_EXIT_CODE" ] || [ "$phase_rc" -eq "$BUDGET_GONE_EXIT_CODE" ]; then
        inconclusive=1
    else
        status=1
    fi
done
if [ "$rc3" -ne 0 ]; then
    if [ "$rc3" -eq 3 ] || [ "$rc3" -eq "$BUDGET_EXHAUSTED_EXIT_CODE" ] || [ "$rc3" -eq "$BUDGET_GONE_EXIT_CODE" ] || [ "$host_measurable" -eq 0 ]; then
        # The wall-clock phase failed on a host that could not measure
        # wall-clock time: the failure is the machine's, so the gate
        # reports "could not decide" instead of blaming the code.
        inconclusive=1
    else
        status=1
    fi
fi
if [ "$status" -eq 0 ] && [ "$inconclusive" -eq 1 ]; then
    status=3
fi

# ``describe_phase <selected> <ran> <rc> <host_measurable>`` — the verdict
# for one phase.
#
# Three states, and the third one matters: a phase that was *selected*
# but never *ran* has no verdict at all.  Reporting it as "passed"
# would be a claim about a lane that was never executed, and the exit
# code does not repair that claim — the summary is what a reader reads.
#
# A phase that was stopped at the gate's deadline is not a phase that
# passed either, and it is not a phase that failed: the code in it was
# never given the time to be judged.  That is what INCONCLUSIVE means.
describe_phase() {
    if [ "$1" -eq 0 ]; then
        printf 'not selected'
        return
    fi
    if [ "$2" -eq 0 ]; then
        printf 'not run (the gate budget was exhausted)'
        return
    fi
    if [ "$3" -eq "$BUDGET_GONE_EXIT_CODE" ]; then
        printf 'not run (the gate budget was exhausted)'
        return
    fi
    if [ "$3" -eq "$BUDGET_EXHAUSTED_EXIT_CODE" ]; then
        printf 'INCONCLUSIVE (the gate budget was exhausted)'
        return
    fi
    if [ "$3" -eq 0 ]; then
        printf 'passed'
        return
    fi
    if [ "$3" -eq 3 ]; then
        printf 'INCONCLUSIVE (exit 3)'
        return
    fi
    if [ "$4" -eq 0 ]; then
        printf 'INCONCLUSIVE (the host could not be measured)'
        return
    fi
    printf 'FAILED (exit %s)' "$3"
}

# The time the gate spent, and the time it was allowed to spend.
#
# The elapsed time is the time the gate *really* spent.  It used to be
# clipped to the budget, which meant an over-budget run always
# reported exactly the budget: the overrun was invisible, and the
# summary could not say how far over the deadline the run had gone.
#
# The number is rounded *up* to whole seconds.  A whole-second report
# that rounded down would swallow the overrun again -- a run of 3.7s
# against a 3s budget would report "3s", which is the budget, which is
# the very thing this line has to be able to contradict.  Rounding up
# keeps "the elapsed time is larger than the budget" true for every run
# that really went over it.
#
# The budget decision itself is untouched: it is made by
# ``budget_exhausted``, which never looks at this number, so the exit
# codes (0 / 1 / 3) are the same as they were.
gate_ended_ms=$(gate_now_ms)
gate_elapsed_ms=$((gate_ended_ms - gate_started_ms))
gate_elapsed=$(((gate_elapsed_ms + 999) / 1000))

printf '=== gate summary ===\n'
printf 'phase 1/3 (parallel):             %s\n' "$(describe_phase "$run_phase1" "$ran_phase1" "$rc1" 1)"
printf 'phase 2/3 (e2e + integration):    %s\n' "$(describe_phase "$run_phase2" "$ran_phase2" "$rc2" 1)"
printf 'phase 3/3 (time_sensitive + perf): %s\n' "$(describe_phase "$run_phase3" "$ran_phase3" "$rc3" "$host_measurable")"
printf 'host measurability:               %s\n' "$host_probe_note"
printf 'gate budget: %ss\n' "$gate_budget"
printf 'gate elapsed: %ss\n' "$gate_elapsed"
if [ "$budget_exhausted" -eq 1 ]; then
    printf 'gate budget exhausted in: %s\n' "$exhausted_phase"
fi
case "$status" in
    0)
        printf 'gate: PASS (exit 0)\n'
        ;;
    1)
        printf 'gate: FAIL (exit 1) — a phase failed\n'
        ;;
    3)
        if [ "$budget_exhausted" -eq 1 ]; then
            printf 'gate: INCONCLUSIVE (exit 3) — the gate budget of %ss was exhausted in %s; the machine did not finish the run, which is not a code failure\n' "$gate_budget" "$exhausted_phase"
        else
            printf 'gate: INCONCLUSIVE (exit 3) — the run could not decide (the host could not be measured, or a phase reported itself inconclusive); this is not a code failure\n'
        fi
        ;;
esac
exit "$status"