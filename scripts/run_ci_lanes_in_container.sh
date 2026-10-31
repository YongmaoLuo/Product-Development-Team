#!/usr/bin/env bash
#
# scripts/run_ci_lanes_in_container.sh — run the gate's lanes, one
# throwaway container each.
#
#   bash scripts/run_ci_lanes_in_container.sh <lane>...
#   PDT_CI_IMAGE=pdt-ci:local bash scripts/run_ci_lanes_in_container.sh unit e2e
#
# Why this exists
# ---------------
# The local gate used to run every lane inside one long-lived container
# (``docker run -d ... sleep infinity``).  That shape has two failure
# modes, and both of them are about what a container that outlives its
# run leaves behind:
#
#   * the container stayed behind whenever the driver died, and
#   * lanes that shared one container shared its processes, its ports
#     and its temporary files, so a leak from one lane reached the
#     next one.
#
# So every lane gets its own container here: created for that lane,
# removed with it, and never shared with another lane.  The image is
# the one ``scripts/build_ci_container.sh`` builds; ``PDT_CI_IMAGE``
# names it.
#
# The rules this driver keeps
# ---------------------------
#   * one container per lane, and one lane at a time (concurrency 1) --
#     isolation between lanes is only real if the lanes do not overlap;
#   * ``--rm``, so the container disappears with its lane;
#   * three labels, so a cleanup script can find what this driver left
#     behind: ``pdt-ci=1`` (this is a gate container), ``pdt-owner``
#     (whose it was) and ``pdt-created`` (when it was made);
#   * a lock, so two drivers cannot run the gate on one machine at the
#     same time.
#
# What this driver does not cover
# -------------------------------
# A driver that is SIGKILLed runs no trap: the container it started
# stays.  That leftover belongs to the cleanup script -- it can find
# the container by its ``pdt-ci`` label -- and pretending otherwise
# here would be a promise this shell cannot keep.
#
# Exit codes
# ----------
#   0   every lane passed
#   1   a lane failed, a lane timed out, or no lane was asked for
#   75  another gate run holds the lock
#
# stdout carries the report: one line per lane, then the summary.  The
# lanes' own output goes to stderr, so a caller that captures stdout
# gets the verdict and not a screen of test output.
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The image the lanes run in.  ``scripts/build_ci_container.sh`` builds
# it under this name by default.
IMAGE="${PDT_CI_IMAGE:-pdt-ci:local}"

# How long one lane may take before it is killed and counted as
# failed.  A lane that hangs is a lane that never reports; the timeout
# turns that into a verdict instead of a silence.
LANE_TIMEOUT="${PDT_CI_LANE_TIMEOUT:-3600}"

# The lock.  A fixed path on purpose: the lock is a claim on the
# machine, not on the run, so two drivers that disagreed about where
# it lives would happily run the gate at the same time.
LOCK_DIR="/tmp/pdt-ci-driver.lock"

case "$LANE_TIMEOUT" in
    ''|*[!0-9]*)
        echo "Error: PDT_CI_LANE_TIMEOUT must be a whole number of seconds (got '$LANE_TIMEOUT')" >&2
        exit 1
        ;;
esac
if [ "$LANE_TIMEOUT" -lt 1 ]; then
    echo "Error: PDT_CI_LANE_TIMEOUT must be at least 1 second (got '$LANE_TIMEOUT')" >&2
    exit 1
fi

# The repository root: the script lives in ``scripts/``, one level
# below it.  ``pwd -P`` gives the physical path, so the path docker
# is handed has no ``..`` in it.
_script_dir="${BASH_SOURCE[0]%/*}"
if [[ "$_script_dir" == "${BASH_SOURCE[0]}" ]]; then
    _script_dir="."
fi
REPO_ROOT="$(cd "$_script_dir/.." && pwd -P)"

if [ "$#" -eq 0 ]; then
    echo "usage: bash scripts/run_ci_lanes_in_container.sh <lane>..." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
#
# Taken before anything else runs, and given back on the way out.  A
# driver that finds the lock taken says so and starts nothing: a gate
# run that shares the machine with another one reports the other run's
# failures as its own.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -d "$LOCK_DIR" ]; then
        echo "another gate run is in progress" >&2
        exit 75
    fi
    echo "Error: could not create the driver lock at $LOCK_DIR" >&2
    exit 1
fi

# The container of the lane that is running right now, if any.  The
# trap below removes it; an empty name means there is nothing to
# remove.
CURRENT_CONTAINER=""

cleanup() {
    local status=$?
    trap - EXIT
    if [ -n "$CURRENT_CONTAINER" ]; then
        # The container is created with --rm, so by the time a lane
        # ends there is usually nothing left to remove.  When the
        # driver is killed mid-lane there is, and this is what takes
        # it away.
        docker rm -f "$CURRENT_CONTAINER" >&2 || true
    fi
    rm -rf "$LOCK_DIR"
    exit "$status"
}
trap cleanup EXIT
# A signal ends the driver; the EXIT trap then does the cleanup the
# signal interrupted.
trap 'exit 143' TERM
trap 'exit 130' INT

# ---------------------------------------------------------------------------
# The lanes
# ---------------------------------------------------------------------------

# One id for the whole run: the containers it starts share it, so the
# ones belonging to a run can be told apart from the ones belonging to
# another.
RUN_ID="$(date +%s)-$$"
OWNER_UID="$(id -u)"

passed=0
total=0
overall=0

for lane in "$@"; do
    total=$((total + 1))

    # The container name carries the lane and the run.  A lane name
    # that is not a legal container name is flattened into one that
    # is, rather than passed on and rejected by the daemon.
    lane_slug="$(printf '%s' "$lane" | sed 's/[^A-Za-z0-9_.-]/-/g')"
    container="pdt-ci-${lane_slug}-${RUN_ID}"
    CURRENT_CONTAINER="$container"

    started="$(date +%s)"

    # The lane runs in the background so the driver can watch how long
    # it takes.  The job's exit status is the lane's own status: the
    # driver waits for the job and takes what it reports.
    docker run --rm \
        --name "$container" \
        --label "pdt-ci=1" \
        --label "pdt-owner=${OWNER_UID}" \
        --label "pdt-created=$(date +%s)" \
        --volume "${REPO_ROOT}:/src" \
        --workdir /src \
        "$IMAGE" \
        bash scripts/run_tests.sh --lane "$lane" >&2 &
    lane_pid=$!

    # Watch the lane until it ends or it runs out of time.
    timed_out=0
    while kill -0 "$lane_pid" 2>/dev/null; do
        elapsed=$(( $(date +%s) - started ))
        if [ "$elapsed" -ge "$LANE_TIMEOUT" ]; then
            timed_out=1
            break
        fi
        sleep 1
    done

    if [ "$timed_out" -eq 1 ]; then
        # The lane gets no more time than it was given: the container
        # goes, the client that was waiting for it goes with it, and
        # the lane is recorded as failed.
        echo "[$lane] timed out after ${LANE_TIMEOUT}s" >&2
        docker rm -f "$container" >&2 || true
        kill "$lane_pid" 2>/dev/null || true
    fi

    lane_status=0
    wait "$lane_pid" 2>/dev/null || lane_status=$?
    if [ "$timed_out" -eq 1 ]; then
        lane_status=124
    fi
    CURRENT_CONTAINER=""

    if [ "$lane_status" -eq 0 ]; then
        passed=$((passed + 1))
    else
        # A failed lane does not cancel the lanes after it: every lane
        # runs, and the run fails at the end.
        overall=1
    fi
    printf '[%s] %s -> exit %s\n' "$lane" "$container" "$lane_status"
done

printf 'lanes: %s/%s passed\n' "$passed" "$total"
exit "$overall"