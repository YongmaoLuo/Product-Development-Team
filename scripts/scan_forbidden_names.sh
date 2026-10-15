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
#
# What it scans
# -------------
# Four surfaces, not one. A private name leaks through a file's contents, a
# file's *name*, a commit's *message*, and a branch or tag *name*, and only
# the first was checked before. The last two are the ones that survive
# review: a commit message is prose nobody reads line by line, and a ref
# name is published the moment it exists on a remote.
#
# Each is reported with its own tag so a reader can tell them apart:
#
#   path:lineno: needle               content of a staged/committed file
#   <path> [path-name]: needle        the path itself
#   <sha> [commit-message]:N: needle  a commit message, N lines in
#   refs/... [ref-name]: needle       a branch or tag name
#
# --index has no commit-message surface: nothing is committed yet, so there
# is no message to read, and the line says so rather than passing quietly.
#
# What --refs-local does and does not cover
# ------------------------------------------
# It reads every local branch and tag, which is what an operator can act on
# before pushing. It CANNOT see a ref that already exists on a remote: no
# local command enumerates what a remote holds. The pre-push path covers
# what it can see for real -- the ref lines git feeds it, one --ref per line
# -- which is every name *this push* is about to publish. A name already on
# a remote, pushed from another machine or before this check existed, is
# outside the reach of any local gate and is not claimed by this one.

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
  --ref NAME     Also scan this branch/tag name (repeatable)
  --refs-local   Also scan every local branch and tag name
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
        --ref)
            # A ref name is not a mode: it selects the ref-name dimension
            # alongside --index or --range, which is what pre-push needs
            # (it has both a range and the names being published).
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "ERROR: --ref requires a non-empty branch or tag name" >&2
                exit 2
            fi
            PY_ARGS+=(--ref "$2")
            shift 2
            ;;
        --refs-local)
            PY_ARGS+=(--refs-local)
            shift
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

Each line above names where the name was found:

  relative/path:lineno: needle        inside a staged or committed file
  relative/path [path-name]: needle   in the path itself
  <sha> [commit-message]:N: needle   in a commit message, line N
  refs/heads/... [ref-name]: needle   in a branch or tag name

There is no per-file allowlist and no `tests/` exemption: a private name is
as published in a fixture or a docstring as in a secret.

To fix it, delete the name from the line — or, if the file genuinely has to
name something the list flags, narrow the list entry rather than the
check. For a `[ref-name]` line the fix is `git branch -m` / `git tag -d`
and a fresh push: the old name stays in every clone's reflog and in every
`git ls-remote` that already saw it, so a rename is the only remedy there
is. The list is yours (.config/forbidden-names); the check is not optional
once you have one.
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
