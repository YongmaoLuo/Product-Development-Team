#!/usr/bin/env bash
# scripts/negctl_group_signal_trap.sh — the negative control behind VP-014's
# third piece of evidence.
#
# What the previous round could not answer
# -----------------------------------------
# On 2026-10-29 the CI gate died of a SIGTERM that nobody inside the run
# sent.  The control that was supposed to say where the signal came from
# installed a TERM trap OUTSIDE ``run_tests.sh`` and appended the group
# census to ``group-trap.txt`` when that trap fired.  The round ended with
# exit 1 — pytest's own exit code — and ``group-trap.txt`` was never
# created: no signal ever reached the session, so the trap never ran.
# The absence of the file was therefore not evidence about the sender, it
# was evidence that the round never reached the point where the question
# could be asked at all.
#
# The question, and the only honest way to ask it
# ------------------------------------------------
# "Did the WHOLE GROUP get the signal, or did one pid in it?"
#
# A control that only ever sees one of the two cannot answer it, and a
# control that hard-codes the answer cannot answer it either — it would
# report the same verdict whether the signal reached the group or one
# process.  So this control runs the signal round TWICE, in the same
# shape, and lets the difference between the two runs BE the answer:
#
#   group      — the signal goes to the whole group; a sentinel that
#                shares the group and carries NO trap dies with it;
#   single-pid — the signal goes to one pid; the sentinel in the same
#                group is untouched.
#
# The sentinel is the witness.  It has no trap, so it cannot report
# anything itself; what it can do is die.  "The sentinel was alive before
# the signal and gone after it" is a fact about the run, and it is the
# fact that separates "the group got it" from "one pid got it".
#
# The safety boundary
# -------------------
# The shell that installs the trap must lead a process group of its OWN:
# ``kill -- -<pgid>`` is only a signal to a group this control created
# if the shell's pgid IS its pid.  A shell that merely *lives* in
# somebody else's group (the pytest's group, the CI runner's group)
# REFUSES to run rather than signal a group it does not own — the very
# failure mode repair-r1-01 closed.  The refusal is the point: the
# control must not be able to reproduce the bug it exists to document.
#
# Nothing outside this control is ever signalled.  The only pids this
# script can send a signal to are: its own pid, the group it created
# itself (and only when it is that group's leader), and the sentinel it
# started itself.  There is no code path in here that reaches a pid or a
# pgid it did not create.
#
# Usage
# -----
#   bash scripts/negctl_group_signal_trap.sh <group|single-pid|gate> <out-dir>
#
# ``group`` / ``single-pid`` run ONE round.  The caller must start this
# shell in a session of its own — ``subprocess.Popen(...,
# start_new_session=True)`` on the Python side, which is what the
# ``gate`` mode and the behavioural suite do.  A shell that is not a
# group leader exits 2 without signalling anything.
#
# ``gate`` runs the whole control: both rounds (each in its own session),
# the check that the two rounds disagree, and the gate command for the
# change this evidence belongs to.
#
# Exit contract
# -------------
#   * exit 0 — the control is established
#   * exit 1 — a check fired (the control is vacuous, or the fix regressed)
#   * exit 2 — the control could not run (not a group leader, no python)

set -uo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"

#: The suite the gate half runs, through the unmodified runner.  It is
#: the suite that pins the narrowed group signal (repair-r1-01), and it
#: is an existing path — ``scripts/run_tests.sh`` treats a path that
#: exists as a target and everything else as a pytest argument.
GATE_TARGET="${GATE_TARGET:-backend/tests/unit/test_service_manager.py}"

#: The python used to start a round in a session of its own.  The
#: venv's python is preferred because ``scripts/run_tests.sh`` needs it
#: anyway; the system python3 is the fallback.
SESSION_PYTHON=""
if [ -x "$REPO_ROOT/backend/.venv/bin/python" ]; then
    SESSION_PYTHON="$REPO_ROOT/backend/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    SESSION_PYTHON="$(command -v python3)"
fi

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

#: The value of a ``key=value`` line in an evidence file.
fact() {
    sed -n "s/^$2=//p" "$1" 2>/dev/null | head -n 1
}

#: The pids in ``$1``'s group that are neither this shell nor a child of
#: it.  A group this control created holds nothing else; anything else
#: in it means the group is not ours to signal.  The answer is left in
#: ``RC_GROUP_STRANGERS`` rather than printed, because a command
#: substitution would put the loop in a subshell — and then the ``ps``
#: it runs would be a child of that subshell instead of of this shell,
#: and every transient process would look like a stranger.
#:
#: The roster is read from a file rather than a pipeline for the same
#: reason: a pipeline would run the loop in a subshell too, and ``$$``
#: would no longer be the pid the roster's ``ppid`` column points at.
#:
#: The group is selected by *reading the ``pgid`` column* rather than by
#: ``ps -g``.  ``ps -g`` does not mean the same thing everywhere: on
#: Linux it selects by process group, but on macOS it selects by session
#: and ignores the number it is given -- ``ps -g <pgid>`` there answers
#: with *every* process on the host.  A roster that lists the whole host
#: makes every process a stranger, so the control refuses every round
#: and never produces the evidence it exists to produce.  Reading the
#: table and keeping the rows whose ``pgid`` is the one in question says
#: the same thing on both platforms.
RC_GROUP_STRANGERS=""
group_strangers() {
    local pgid="$1"
    local roster
    RC_GROUP_STRANGERS=""
    roster="$(mktemp "${TMPDIR:-/tmp}/negctl-group-roster.XXXXXX")" || return 0
    ps -axo pid=,ppid=,pgid= > "$roster" 2>/dev/null
    while read -r pid ppid row_pgid; do
        [ -n "$pid" ] || continue
        [ "$row_pgid" = "$pgid" ] || continue
        if [ "$pid" != "$$" ] && [ "$ppid" != "$$" ]; then
            RC_GROUP_STRANGERS="$RC_GROUP_STRANGERS $pid"
        fi
    done < "$roster"
    rm -f "$roster"
}

#: Whether the sentinel is still running.  A zombie still answers
#: ``kill -0``, so the state has to be read: ``Z`` means the process is
#: gone and only its corpse is left.
#:
#: The state is read from the process table rather than from a ``stat``
#: column, because macOS's ``ps`` does not answer that column at all --
#: it says "this stand-in does not answer the column stat" and answers
#: nothing, so every process would read as a corpse and the control
#: would report a sentinel that was never alive.  Both platforms report
#: a corpse as ``<defunct>``, and a process that is gone is not in the
#: table at all, so the table answers the same question everywhere.
#:
#: ``awk`` reads the whole table rather than stopping at the first
#: match: a pipeline that ends early leaves ``ps`` with a broken pipe,
#: and this script runs with ``pipefail``.
sentinel_alive() {
    local row
    row="$(ps -axo pid=,command= 2>/dev/null \
        | awk -v p="$rc_sentinel" '$1 == p { print }')"
    [ -n "$row" ] || return 1
    case "$row" in
        *"<defunct>"*) return 1 ;;
    esac
    return 0
}

#: Start the given command in a session of its own.  The child calls
#: setsid(2) before exec, so it leads a process group that holds
#: nothing but itself — the same thing
#: ``subprocess.Popen(..., start_new_session=True)`` does in Python.
spawn_in_own_session() {
    "$SESSION_PYTHON" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
        bash "$SCRIPT_PATH" "$@"
}

# ---------------------------------------------------------------------------
# The round: one signal, and what the control saw because of it.
# ---------------------------------------------------------------------------

#: The TERM handler.  Its shape is the one the 2026-10-29 control used;
#: what is new is that this one actually runs, because the signal is
#: really delivered to the group this control created.
_on_term() {
    # The default disposition goes back first: a second signal must
    # kill this shell outright instead of re-entering the handler.
    trap - TERM INT HUP

    # Give the witness a moment to die.  The kernel hands the group
    # signal to every member at once, but this shell may reach the
    # handler a hair before the sentinel's corpse shows up.
    local waited=0
    while [ "$waited" -lt 40 ]; do
        if ! sentinel_alive; then
            break
        fi
        sleep 0.05
        waited=$((waited + 1))
    done

    if sentinel_alive; then
        rc_after="yes"
        rc_received_by="single-pid"
    else
        rc_after="no"
        rc_received_by="group"
    fi

    {
        echo "=== $(date '+%Y-%m-%dT%H:%M:%S') the control shell was signalled ==="
        echo "mode=$rc_mode"
        echo "signal_target=$rc_signal_target"
        echo "shell_pid=$$"
        echo "shell_pgid=$rc_own_pgid"
        echo "sentinel_pid=$rc_sentinel"
        echo "sentinel_alive_before_the_signal=$rc_before"
        echo "sentinel_alive_after_the_signal=$rc_after"
        echo "received_by=$rc_received_by"
        echo "--- ps -axo pid,ppid,pgid,command | awk '\$3 == $rc_own_pgid' (the group at the moment of the signal) ---"
        ps -axo pid,ppid,pgid,command 2>/dev/null \
            | awk -v g="$rc_own_pgid" 'NR == 1 || $3 == g'
        echo "--- end of the census ---"
    } >> "$rc_trap_file" 2>&1

    # The witness is this control's own child; it is cleaned up here so
    # that nothing is left running.  The state above was recorded
    # BEFORE this line.
    kill "$rc_sentinel" 2>/dev/null
    wait "$rc_sentinel" 2>/dev/null

    # 143 is what a shell that was terminated by SIGTERM leaves with,
    # and this shell really was terminated by SIGTERM — the handler only
    # runs because the signal arrived.  Nothing in this script swallows
    # it and nothing turns it into an exit 1.
    exit 143
}

run_round() {
    rc_mode="$1"
    rc_out_dir="$2"
    rc_trap_file="$rc_out_dir/group-trap.txt"

    mkdir -p "$rc_out_dir" || return 2

    rc_own_pid=$$
    rc_own_pgid="$(ps -o pgid= -p "$rc_own_pid" 2>/dev/null | tr -d '[:space:]')"

    # The safety gate: this shell must lead its OWN group.  Without
    # this check, `kill -- -$rc_own_pgid` would signal whatever group
    # the shell merely happens to live in — the pytest's group, the CI
    # runner's group — which is the failure this control exists to
    # document.
    if [ -z "$rc_own_pgid" ] || [ "$rc_own_pgid" != "$rc_own_pid" ]; then
        {
            echo "refused: this shell does not lead its own process group"
            echo "  own_pid=$rc_own_pid own_pgid=$rc_own_pgid"
            echo "  a group signal is only allowed to a group this control created;"
            echo "  start this control with start_new_session=True"
        } >&2
        return 2
    fi

    # The group must hold nothing but this shell and what it started.
    # The check runs before the sentinel exists, so the roster it reads
    # is the group at its emptiest: this shell, and the ``ps`` that
    # reads the roster.
    group_strangers "$rc_own_pgid"
    if [ -n "$RC_GROUP_STRANGERS" ]; then
        echo "refused: the group holds processes this control did not create:" >&2
        echo "$RC_GROUP_STRANGERS" >&2
        return 2
    fi

    # The witness: a plain sleep, in the same group, with NO trap.  It
    # can only die by being signalled.
    sleep 30 &
    rc_sentinel=$!

    rc_before="no"
    if sentinel_alive; then
        rc_before="yes"
    fi

    trap _on_term TERM INT HUP

    case "$rc_mode" in
        group)
            rc_signal_target="-$rc_own_pgid"
            kill -TERM -- "-$rc_own_pgid"
            ;;
        single-pid)
            rc_signal_target="$rc_own_pid"
            kill -TERM "$rc_own_pid"
            ;;
        *)
            echo "refused: unknown mode '$rc_mode'" >&2
            return 2
            ;;
    esac

    # The trap did not fire: no signal reached this shell at all.  Say
    # so, and leave with a status that is NOT 143 — 143 would claim a
    # termination that did not happen.
    echo "FAIL: the trap did not fire; no signal reached this shell" >&2
    return 1
}

# ---------------------------------------------------------------------------
# The gate: both rounds, the disagreement between them, and the suite.
# ---------------------------------------------------------------------------

run_gate() {
    local out_dir="$1"
    local group_dir="$out_dir/group"
    local single_dir="$out_dir/single-pid"
    local group_rc single_rc gate_rc group_value single_value

    if [ -z "$SESSION_PYTHON" ]; then
        echo "FAIL: no python is available to start a round in a session of its own" >&2
        return 2
    fi

    mkdir -p "$group_dir" "$single_dir" || return 2

    echo "=== negctl group-signal-trap — telling the group from the pid"

    # Both rounds run in a session of their own: the shell that
    # installs the trap has to lead a group that holds nothing but
    # itself.
    spawn_in_own_session group "$group_dir"
    group_rc=$?
    if [ "$group_rc" -ne 143 ]; then
        echo "FAIL: the group round exited $group_rc, not 143 (a real SIGTERM leaves 143)" >&2
        return 1
    fi
    echo "GREEN: the group round was terminated by SIGTERM and left 143"

    spawn_in_own_session single-pid "$single_dir"
    single_rc=$?
    if [ "$single_rc" -ne 143 ]; then
        echo "FAIL: the single-pid round exited $single_rc, not 143 (a real SIGTERM leaves 143)" >&2
        return 1
    fi
    echo "GREEN: the single-pid round was terminated by SIGTERM and left 143"

    group_value="$(fact "$group_dir/group-trap.txt" received_by)"
    single_value="$(fact "$single_dir/group-trap.txt" received_by)"

    if [ "$group_value" != "group" ]; then
        echo "FAIL: the group round reports received_by='$group_value', not 'group'" >&2
        return 1
    fi
    if [ "$single_value" != "single-pid" ]; then
        echo "FAIL: the single-pid round reports received_by='$single_value', not 'single-pid'" >&2
        return 1
    fi
    if [ "$group_value" = "$single_value" ]; then
        echo "FAIL: both rounds report '$group_value' — the control cannot tell the group from the pid" >&2
        return 1
    fi
    echo "GREEN: the two rounds disagree (group -> $group_value, single-pid -> $single_value)"

    # The two pieces of evidence, side by side, ready to be filed.
    cp "$group_dir/group-trap.txt" "$out_dir/group-trap.txt"
    cp "$single_dir/group-trap.txt" "$out_dir/group-trap-single-pid.txt"

    # The gate for the change this evidence belongs to: the suite that
    # pins the narrowed group signal, run through the unmodified
    # runner.  ``run_tests.sh`` is untouched — the path is a target it
    # already knows how to treat.
    echo "=== gate: bash scripts/run_tests.sh $GATE_TARGET"
    (
        cd "$REPO_ROOT" || exit 2
        unset PYTEST_ADDOPTS PYTEST_CURRENT_TEST PYTEST_PLUGINS COVERAGE_FILE
        bash scripts/run_tests.sh "$GATE_TARGET"
    ) > "$out_dir/gate.log" 2>&1
    gate_rc=$?
    if [ "$gate_rc" -ne 0 ]; then
        echo "FAIL: the gate failed (exit $gate_rc)" >&2
        tail -n 40 "$out_dir/gate.log" >&2
        return 1
    fi
    echo "GREEN: scripts/run_tests.sh $GATE_TARGET passed"

    echo
    echo "PASS: negctl-group-signal-trap"
    return 0
}

# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

if [ "$#" -ne 2 ]; then
    echo "usage: bash scripts/negctl_group_signal_trap.sh <group|single-pid|gate> <out-dir>" >&2
    exit 2
fi

case "$1" in
    group|single-pid)
        run_round "$1" "$2"
        exit $?
        ;;
    gate)
        run_gate "$2"
        exit $?
        ;;
    *)
        echo "usage: bash scripts/negctl_group_signal_trap.sh <group|single-pid|gate> <out-dir>" >&2
        exit 2
        ;;
esac