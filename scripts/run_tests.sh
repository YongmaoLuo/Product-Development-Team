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
run_phase() {
    local phase_no="$1"
    local label="$2"
    shift 2
    printf '=== phase %s ===\n' "$label"
    # errexit off for the call below, so a failing phase is *captured*
    # instead of aborting the script; ``-u`` and ``pipefail`` stay on.
    set -euo pipefail +e
    if [ -n "$PDT_GATE_JUNIT_DIR" ]; then
        # The report of *this* phase, written next to the reports of the
        # other phases. The phase number is part of the file name, so the
        # three reports of one run never overwrite each other.
        "$PYTEST_BIN" "$@" "--junitxml=${PDT_GATE_JUNIT_DIR}/phase-${phase_no}.xml"
    else
        "$PYTEST_BIN" "$@"
    fi
    local rc=$?
    set -euo pipefail
    return "$rc"
}

if [ "$run_phase1" -eq 1 ]; then
    run_phase 1 "1/3 (parallel)" "${phase1[@]}" || rc1=$?
fi
if [ "$run_phase2" -eq 1 ]; then
    run_phase 2 "2/3 (e2e + integration)" "${phase2[@]}" || rc2=$?
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
    run_phase 3 "3/3 (time_sensitive + perf)" "${phase3[@]}" || rc3=$?
fi

# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
# Three states, three exit codes:
#
#   0  every phase that ran passed
#   1  a phase failed — a defect in the code under test
#   3  the run could not decide — the host could not be measured, or a
#      phase reported "inconclusive"
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
    if [ "$phase_rc" -eq 3 ]; then
        inconclusive=1
    else
        status=1
    fi
done
if [ "$rc3" -ne 0 ]; then
    if [ "$rc3" -eq 3 ] || [ "$host_measurable" -eq 0 ]; then
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

# ``describe_phase <selected> <rc> <host_measurable>`` — the verdict for
# one phase.
#
# Three states, and the third one matters: a phase that was *selected*
# but never *ran* has no verdict at all.  Reporting it as "passed"
# would be a claim about a lane that was never executed, and the exit
# code does not repair that claim — the summary is what a reader reads.
describe_phase() {
    if [ "$1" -eq 0 ]; then
        printf 'not selected'
        return
    fi
    if [ "$2" -eq 0 ]; then
        printf 'passed'
        return
    fi
    if [ "$2" -eq 3 ]; then
        printf 'INCONCLUSIVE (exit 3)'
        return
    fi
    if [ "$3" -eq 0 ]; then
        printf 'INCONCLUSIVE (the host could not be measured)'
        return
    fi
    printf 'FAILED (exit %s)' "$2"
}

printf '=== gate summary ===\n'
printf 'phase 1/3 (parallel):             %s\n' "$(describe_phase "$run_phase1" "$rc1" 1)"
printf 'phase 2/3 (e2e + integration):    %s\n' "$(describe_phase "$run_phase2" "$rc2" 1)"
printf 'phase 3/3 (time_sensitive + perf): %s\n' "$(describe_phase "$run_phase3" "$rc3" "$host_measurable")"
printf 'host measurability:               %s\n' "$host_probe_note"
case "$status" in
    0)
        printf 'gate: PASS (exit 0)\n'
        ;;
    1)
        printf 'gate: FAIL (exit 1) — a phase failed\n'
        ;;
    3)
        printf 'gate: INCONCLUSIVE (exit 3) — the run could not decide (the host could not be measured, or a phase reported itself inconclusive); this is not a code failure\n'
        ;;
esac
exit "$status"