#!/usr/bin/env bash
# scripts/scan_staged_privacy.sh — block private info at commit time.
#
# Why this exists
# ---------------
# Every other privacy surface in this repository is *after the fact*:
#
#   * scripts/scan_commit_range.sh runs on commits that already exist —
#     in CI, or from the pre-push hook, which is the earliest of them;
#   * the static gates read the working tree, which can be clean while the
#     commit that was just made carries the leak;
#   * pr_metadata_scan.py runs on a pull request, before the merge.
#
# On 2026-09-29 a `git push` published a private project name, a business
# term and real plan ids, and a force-push could not undo it: deleting a
# ref does not delete objects. Every one of those surfaces would have
# caught it. None of them is the reason it did not happen, because all of
# them run after the text is already in a commit.
#
# This one runs before the commit exists, on the content in the index.
# Refusing here costs nothing: the fix is "edit the file", not "rewrite a
# commit someone may already have fetched".
#
# It reads the index, not the working tree, and that distinction is the
# whole reason it is worth having. A file can be edited back to clean
# after being staged, and the commit still carries what was staged.
#
# One entry point, three callers
# ------------------------------
# The same script runs from:
#
#   * the native pre-commit hook (scripts/install_git_hooks.sh, via
#     scripts/pre_commit_gate.sh);
#   * the .pre-commit-config.yaml pre-commit stage;
#   * an ad-hoc shell, from any cwd.
#
# That mirrors scripts/grep_guard.sh and scripts/scan_commit_range.sh, for
# the same reason: three callers each rediscovering the venv path, the
# argument shape and the exit-code contract is how they drift apart.
#
# The rules are not restated here. backend/tests/static_gates/
# staged_content_scan.py imports the same pure functions the tree gate,
# the range scan and the PR-metadata scan use, so no surface can disagree
# with another about what a violation is.
#
# Exit contract
# -------------
#   * 0 — scan ran, zero violations
#   * 1 — scan ran, at least one violation (commit blocked)
#   * 2 — usage / setup error, or the scan could not run to completion
#
# 2 is deliberately distinct from 1: "the gate found something" and "the
# gate did not run" need different responses, and collapsing them lets a
# broken gate look like a clean one. The scanner returns 2 when the index
# holds nothing scannable, for exactly that reason — an index it did not
# read is not an index it found clean.
#
# Usage
# -----
#   bash scripts/scan_staged_privacy.sh
#   bash scripts/scan_staged_privacy.sh --help

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution — BASH_SOURCE so this works from any cwd.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCANNER="$PROJECT_ROOT/backend/tests/static_gates/staged_content_scan.py"
VENV_PY="$PROJECT_ROOT/backend/.venv/bin/python3"

usage() {
    cat <<'USAGE'
scripts/scan_staged_privacy.sh — block private info in staged content

Usage:
  bash scripts/scan_staged_privacy.sh
  bash scripts/scan_staged_privacy.sh --help

Exit codes:
  0  scan ran, zero violations
  1  scan ran, at least one violation
  2  usage / setup error, or the scan could not run to completion
USAGE
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
fi
if [ $# -gt 0 ]; then
    echo "ERROR: unknown argument: $1" >&2
    usage >&2
    exit 2
fi

if [ ! -f "$SCANNER" ]; then
    echo "ERROR: scanner module not found: $SCANNER" >&2
    exit 2
fi

# Prefer the project venv per CLAUDE.md; fall back to system python3 with a
# loud warning, because silently running on a different interpreter is how a
# gate starts reporting results CI never reproduces.
PYTHON_CMD=""
if [ -x "$VENV_PY" ]; then
    PYTHON_CMD="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    echo "WARNING: venv python not found at $VENV_PY; falling back to system python3" >&2
    echo "WARNING: results may differ from CI — create the venv per CLAUDE.md" >&2
    PYTHON_CMD="$(command -v python3)"
else
    echo "ERROR: neither venv python ($VENV_PY) nor system python3 found" >&2
    exit 2
fi

RC=0
OUTPUT=$("$PYTHON_CMD" "$SCANNER" --repo "$PROJECT_ROOT" 2>&1) || RC=$?

if [ "$RC" -eq 1 ]; then
    printf '%s\n' "$OUTPUT" >&2
    cat >&2 <<'HOWTO'

Nothing has been committed, so this is free to fix: edit the file, `git add`
it again, and commit. The plan-id shape and the private-name blacklist have
no per-file allowlist — a private name is a private name in a docstring as
much as in a secret, and a date-prefixed id in production source is a real
plan until proven otherwise.

If the string is legitimately needed (a test fixture, a doc example),
tests/ is exempt from the plan-id rule but NOT from the private-name
blacklist. Move it, or build it from fragments as the gate modules do.
HOWTO
    exit 1
fi

if [ "$RC" -ne 0 ]; then
    printf '%s\n' "$OUTPUT" >&2
    echo "ERROR: the staged-content scan could not run to completion" >&2
    exit 2
fi

printf '%s\n' "$OUTPUT"
exit 0
