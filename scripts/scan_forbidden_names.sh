#!/usr/bin/env bash
# scripts/scan_forbidden_names.sh — block forbidden proper names from the operator's local list.
#
# Why this exists
# ---------------
# The list of private proper names cannot live in this repository: writing a
# name down in order to check for it publishes the name, which is the exact
# thing the list exists to prevent. So the list is a *local* file,
# .config/forbidden-names, and this wrapper is the entry point that reads it.
#
# It runs from the two hooks where refusing is still free:
#
#   * scripts/pre_commit_gate.sh (--index), as the third gate of the
#     pre-commit stage: the text is not in a commit yet, so the fix is
#     "edit the file";
#   * scripts/scan_commit_range.sh (--range), for the commits a push is
#     about to publish, where rewriting the commit is still free.
#
# It deliberately does not run in CI. A CI runner has no
# .config/forbidden-names -- it is gitignored, operator-owned, and by
# definition not on a remote -- so a CI invocation could only ever be exit 2.
#
# One entry point, three callers
# ------------------------------
# The same script runs from the pre-commit hook, from the pre-push hook, and
# from an ad-hoc shell in any cwd. That mirrors scripts/grep_guard.sh,
# scripts/scan_staged_privacy.sh and scripts/scan_commit_range.sh, for the
# same reason: three callers each rediscovering the venv path, the argument
# shape and the exit-code contract is how they drift apart.
#
# The rule is not restated here: scripts/scanners/scan_forbidden_names.py
# owns it, and nothing about a needle or a match lives in this file.
#
# Exit contract
# -------------
#   * 0 — the list was read, the content was read, zero needles matched
#   * 1 — the list was read, the content was read, at least one needle
#         matched (commit / push blocked)
#   * 2 — the check did not run: no list, an empty list, an uncompilable
#         pattern, an unreadable index, or a range with no commits
#
# 2 is deliberately distinct from 0 and from 1, and it is the reason this
# script exists rather than a "no list, nothing to do" branch. A check that
# did not run has not cleared the commit. Reporting it as 0 would make an
# unconfigured gate indistinguishable from a clean one -- which is the
# failure this whole arrangement was built to stop, and which the dead
# PDT_FORBIDDEN_REPO_NAMES env-var path demonstrated: it was never set on any
# machine, so it reported a pass forever and checked nothing.
#
# Usage
# -----
#   bash scripts/scan_forbidden_names.sh --index
#   bash scripts/scan_forbidden_names.sh --range origin/main..HEAD
#   bash scripts/scan_forbidden_names.sh --index --list /tmp/forbidden-names
#   bash scripts/scan_forbidden_names.sh --help

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution — BASH_SOURCE so this works from any cwd.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCANNER="$SCRIPT_DIR/scanners/scan_forbidden_names.py"
LIST_PATH="$PROJECT_ROOT/.config/forbidden-names"
VENV_PY="$PROJECT_ROOT/backend/.venv/bin/python3"

usage() {
    cat <<'USAGE'
scripts/scan_forbidden_names.sh — block forbidden proper names from the local list

Usage:
  bash scripts/scan_forbidden_names.sh --index
  bash scripts/scan_forbidden_names.sh --range SPEC [--range SPEC ...]
  bash scripts/scan_forbidden_names.sh --index --list PATH
  bash scripts/scan_forbidden_names.sh --help

Options:
  --index        Scan the content staged for the next commit
  --range SPEC   Scan every commit in SPEC (repeatable)
  --list PATH    Read the list from PATH (default: <repo>/.config/forbidden-names)
  --help         This text

Exit codes:
  0  the list was read, the content was read, zero needles matched
  1  the list was read, the content was read, at least one needle matched
  2  the check did not run (no list / empty list / unreadable content)
USAGE
}

MODE=""
PY_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --index)
            MODE="index"
            PY_ARGS+=(--index)
            shift
            ;;
        --range)
            if [ $# -lt 2 ]; then
                echo "ERROR: --range requires a revision range" >&2
                exit 2
            fi
            if [ "$MODE" = "index" ]; then
                echo "ERROR: --index and --range are mutually exclusive" >&2
                exit 2
            fi
            MODE="range"
            PY_ARGS+=(--range "$2")
            shift 2
            ;;
        --list)
            if [ $# -lt 2 ]; then
                echo "ERROR: --list requires a path" >&2
                exit 2
            fi
            LIST_PATH="$2"
            PY_ARGS+=(--list "$2")
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "ERROR: pass --index or --range SPEC — nothing was selected to scan" >&2
    usage >&2
    exit 2
fi

if [ ! -f "$SCANNER" ]; then
    echo "ERROR: scanner module not found: $SCANNER" >&2
    exit 2
fi

# Prefer the project venv per CLAUDE.md; fall back to system python3 with a
# loud warning, because silently running on a different interpreter is how a
# gate starts reporting results nobody else reproduces. The scanner is
# stdlib-only, so the fallback is genuinely equivalent -- but the warning
# still fires, because "it happened to be fine" is not the same guarantee.
PYTHON_CMD=""
if [ -x "$VENV_PY" ]; then
    PYTHON_CMD="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    echo "WARNING: venv python not found at $VENV_PY; falling back to system python3" >&2
    PYTHON_CMD="$(command -v python3)"
else
    echo "ERROR: neither venv python ($VENV_PY) nor system python3 found" >&2
    exit 2
fi

RC=0
OUTPUT=$("$PYTHON_CMD" "$SCANNER" --repo "$PROJECT_ROOT" "${PY_ARGS[@]}" 2>&1) || RC=$?

if [ "$RC" -eq 1 ]; then
    printf '%s\n' "$OUTPUT" >&2
    cat >&2 <<'HOWTO'

Each line above is `relative/path:lineno: <redacted> (N chars) [list entry
#K]`. The name itself is not printed — a report that repeats the private
proper name it exists to catch is the leak with extra steps, and this
output ends up in terminals, CI logs and pasted issues. `#K` is the
position of the matching entry in your own .config/forbidden-names, so you
can look it up without this script telling you what it says. There is no
per-file allowlist and no `tests/` exemption: a private name is as
published in a fixture or a docstring as in a secret.

To fix it, open the file at that line and delete the name — or, if the file
genuinely has to name something the list flags, narrow the list entry
rather than the check. The list is yours (.config/forbidden-names); the
check is not optional once you have one.
HOWTO
    exit 1
fi

if [ "$RC" -ne 0 ]; then
    printf '%s\n' "$OUTPUT" >&2
    echo >&2
    echo "ERROR: the forbidden-name check did not run. Nothing has been" >&2
    echo "ERROR: cleared — an absent or empty list is an unconfigured check," >&2
    echo "ERROR: not a clean one. Create the list and run this again." >&2
    exit 2
fi

printf '%s\n' "$OUTPUT"
exit 0
