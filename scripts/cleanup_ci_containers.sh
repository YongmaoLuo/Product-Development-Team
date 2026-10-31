#!/usr/bin/env bash
#
# scripts/cleanup_ci_containers.sh — remove the gate containers a killed
# driver left behind.
#
#   bash scripts/cleanup_ci_containers.sh            # dry run (the default)
#   bash scripts/cleanup_ci_containers.sh --apply    # actually remove them
#
# Why this exists
# ---------------
# scripts/run_ci_lanes_in_container.sh starts one container per lane with
# --rm, so a container normally disappears with its lane.  Two things
# defeat that: a driver that is SIGKILLed runs no trap at all, and a
# restarted docker daemon loses the containers it was running.  Either
# way the container stays, holding a slice of the machine's memory and
# disk for as long as nobody removes it.
#
# What it removes
# ---------------
# A container is removed only when all four of these hold:
#
#   * it carries the label pdt-ci=1 -- the docker filter below is what
#     restricts the listing to those, so a container without the label
#     is never in the list to begin with;
#   * it carries the label pdt-owner=<uid> and that uid is this user;
#   * it carries the label pdt-created=<unixtime> and that timestamp is
#     more than two hours old;
#   * it is not running.
#
# Anything else is left alone: somebody else's container, a container
# without the labels, a container that is still running, a container
# that is younger than two hours.
#
# How it removes
# --------------
# A plain `docker rm` first, because the daemon refuses it for a
# container that is running -- and a container that started running
# again between the listing and the removal must not be killed.  The
# daemon refuses a paused or a dead container too, and those are not
# running: they are leftovers like any other, so the state is asked
# for once more and the removal is forced for them.  A container that
# is running (or restarting) at that moment is left alone, with the
# refusal reported.
#
# The default is a dry run
# ------------------------
# The script prints what it would remove and removes nothing.  Removing
# a container is not something to do by accident: the listing may be
# stale, the threshold may be wrong, and the container may be one
# somebody is about to look at.  So the decision to actually do it has
# to be made out loud, with --apply.
#
# Exit codes
# ----------
#   0   the run finished (a dry run that would remove nothing is a
#       success too)
#   1   the container list could not be read, or a removal failed
#
# stdout carries one line per container: "would remove <name> (age <n>s)"
# in a dry run, "removed <name>" when the removal happened.  Everything
# else -- errors, reasons -- goes to stderr.
set -euo pipefail

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

#: Whether the script really removes anything.  A dry run until it is
#: told otherwise.
APPLY=0

usage() {
    cat <<'EOF'
usage: bash scripts/cleanup_ci_containers.sh [--apply]

  (no flag)   print what would be removed, remove nothing
  --apply     actually remove the containers
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply)
            APPLY=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: How old a container has to be before it counts as a leftover.  Two
#: hours is long enough that a container still in use -- a lane that
#: runs for an hour, a driver waiting between lanes -- is never taken
#: away from under itself.
MIN_AGE_SECONDS=7200

#: The user whose containers this script may remove.  The driver labels
#: every container it starts with pdt-owner=<uid>; a container that
#: carries somebody else's id was started by another session on this
#: machine and is not ours to remove.
OWNER_UID="$(id -u)"

#: The columns the listing is read from: name, state, pdt-owner,
#: pdt-created.  The pdt-ci label itself is not a column -- the filter
#: below is what restricts the listing to gate containers, so the
#: unlabelled containers of the machine are never listed.
PS_FORMAT='{{.Names}}\t{{.State}}\t{{.Label "pdt-owner"}}\t{{.Label "pdt-created"}}'

# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is required" >&2
    exit 1
fi

now="$(date +%s)"

# The listing is the whole world this script may act on.  docker's own
# message is merged into the capture so the reason survives when the
# call fails: a daemon that is down must not turn into "there was
# nothing to clean up".
if ! listing="$(docker ps -a --filter label=pdt-ci=1 --format "$PS_FORMAT" 2>&1)"; then
    echo "Error: could not list the gate containers: ${listing}" >&2
    exit 1
fi

status=0

# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

# The listing is fed to the loop through a here-document rather than a
# pipe: a pipe would put the loop in a subshell, and the failure of a
# removal has to reach this script's exit code.
while IFS=$'\t' read -r name state owner created; do
    [ -n "$name" ] || continue

    # A running container belongs to somebody right now.  Its age says
    # nothing: a lane may have been running for hours, and the
    # container is still in use.  It is not a leftover.
    lowered_state="$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')"
    if [ "$lowered_state" = "running" ]; then
        continue
    fi

    # The owner has to be this user.  A container without the label, or
    # with somebody else's id in it, is not ours.
    [ -n "$owner" ] || continue
    [ "$owner" = "$OWNER_UID" ] || continue

    # The creation time has to be a number we can do arithmetic on, and
    # the container has to be old enough.  A missing or malformed
    # timestamp means the age is unknown -- and a container of unknown
    # age is not a leftover this script may remove.
    case "$created" in
        ''|*[!0-9]*) continue ;;
    esac
    age=$(( now - created ))
    [ "$age" -gt "$MIN_AGE_SECONDS" ] || continue

    if [ "$APPLY" -eq 0 ]; then
        printf 'would remove %s (age %ss)\n' "$name" "$age"
        continue
    fi

    # Plain `docker rm` first: it is enough for a container that has
    # exited, and it refuses a container that is running -- which is
    # exactly what is wanted here.
    if docker rm "$name" >/dev/null 2>&1; then
        printf 'removed %s\n' "$name"
        continue
    fi

    # The daemon refuses a container that is not simply exited too -- a
    # paused or a dead one.  Those are not running either, so they are
    # leftovers like any other; but a container that is running (or
    # restarting) must not be killed.  So the state is asked for again,
    # right before anything is forced.
    #
    # The status is asked for rather than "is it running": the daemon
    # calls a paused container running (it is, after all, only paused),
    # and a paused container is exactly the one that may be forced.
    container_state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)"
    case "$container_state" in
        running|restarting)
            # In use: it is not ours to kill.
            ;;
        *)
            if docker rm -f "$name" >/dev/null; then
                printf 'removed %s\n' "$name"
                continue
            fi
            ;;
    esac

    echo "Error: could not remove ${name}" >&2
    status=1
done <<PDT_CLEANUP_LISTING
${listing}
PDT_CLEANUP_LISTING

exit "$status"
