#!/usr/bin/env bash
# scripts/pre_commit_gate.sh — the whole pre-commit stage, in one entry point.
#
# Why this exists
# ---------------
# A git hook is one executable. The pre-commit stage has three gates in it —
# the forbidden-filename sweep that has always run there, the privacy scan
# that must run there too, and the forbidden-name check — so one of them
# needs a caller. This file is that caller, and it is the script
# scripts/install_git_hooks.sh points the ``pre-commit`` hook at.
#
# It is a dispatcher, not a reimplementation: each gate keeps its own
# wrapper (scripts/grep_guard.sh, scripts/scan_staged_privacy.sh,
# scripts/scan_forbidden_names.sh), keeps its own exit contract, and stays
# independently runnable from a shell and from the .pre-commit-config.yaml
# stage. Nothing about a rule lives here.
#
# Order matters. The filename sweep runs first because it is the cheapest of
# the three, and a commit with a forbidden filename in it is not worth
# reading the contents of. All three run regardless of whether an earlier
# one passed, so a single commit gets the complete answer in one pass rather
# than a fix-one-then-rerun round trip.
#
# Why the forbidden-name check is its own step
# ---------------------------------------------
# Its needles are not in this repository. They live in an untracked,
# operator-owned .config/forbidden-names, because a blacklist that is
# checked in publishes the names it is meant to keep out. That makes it
# unlike the other two gates in a way the dispatcher has to respect: if the
# list is missing or holds no usable entry, this step exits 2 — "the check
# did not run" — rather than 0. Collapsing that into 0 would make an
# unconfigured gate indistinguishable from a clean one, which is exactly the
# failure this repository already shipped once with an env-var-driven name
# check that was never set on any machine and passed forever.
#
# Exit contract
# -------------
#   * 0 — every gate ran and found nothing
#   * 1 — at least one gate found a violation (commit blocked)
#   * 2 — at least one gate could not run to completion
#
# 2 outranks 1 on purpose. A gate that did not run has not cleared the
# commit, and reporting that as "blocked, something is wrong" would send
# the reader looking for a violation that does not exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREP_GUARD="$SCRIPT_DIR/grep_guard.sh"
STAGED_PRIVACY="$SCRIPT_DIR/scan_staged_privacy.sh"
FORBIDDEN_NAMES="$SCRIPT_DIR/scan_forbidden_names.sh"

for required in "$GREP_GUARD" "$STAGED_PRIVACY" "$FORBIDDEN_NAMES"; do
    if [ ! -f "$required" ]; then
        echo "ERROR: pre-commit gate dependency not found: $required" >&2
        exit 2
    fi
done

STATUS=0
run_gate() {
    local label="$1"
    shift
    local rc=0
    "$@" || rc=$?
    case "$rc" in
        0) ;;
        1) STATUS=1 ;;
        *)
            echo "ERROR: $label could not run to completion (exit $rc)" >&2
            STATUS=2
            ;;
    esac
}

echo "pre-commit: forbidden-filename sweep"
run_gate "grep-guard" bash "$GREP_GUARD"

echo
echo "pre-commit: staged-content privacy scan"
run_gate "staged privacy scan" bash "$STAGED_PRIVACY"

echo
echo "pre-commit: forbidden-name check (local list)"
run_gate "forbidden-name check" bash "$FORBIDDEN_NAMES" --index

if [ "$STATUS" -ne 0 ]; then
    echo
    echo "commit blocked by the pre-commit stage." >&2
fi
exit "$STATUS"
