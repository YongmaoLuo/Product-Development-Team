#!/usr/bin/env bash
# scripts/check_e2e_module_intact.sh — end-of-plan survival check for the
# whole-delivery E2E module.
#
# Purpose
# -------
# ``backend/tests/e2e/test_keychain_full_delivery_macos.py`` is the largest
# file in the suite, and it has been destroyed three separate times by a work
# report being written into it in place of its source. Each time a handful of
# lines of prose stood where several thousand lines of test had been:
#
#     34 lines replacing 3458
#     20 lines replacing ~2908
#      5 lines replacing 2368
#
# Where those readings come from: they are not the output of a command that
# can be re-run, and no committed file records them. Each is what
# ``git diff HEAD`` showed for this module while it was clobbered — the
# working tree held 34, 20, and 5 lines where the commit held 3458, ~2908, and
# 2368. They are quoted here as the shape of the failure, not as a measurement
# anything can reproduce; nothing downstream of this comment depends on their
# exact values, and the thresholds the checks actually use are floors derived
# from HEAD at run time.
#
# The module was restored each time. Restoring is a repair, not a check: it
# says nothing about whether the *next* edit in the same subtree destroys the
# file again. This script is the cheap check that runs after every other task
# in that subtree has written to the module, and it answers one question —
# did the restored-and-edited file survive to the end of the plan?
#
# Why this is a command and not a third static gate
# -------------------------------------------------
# Two of the properties this script asserts are already pinned by committed
# gates:
#
#   * "the module parses" — ``tests/static_gates/test_e2e_docstring_labels_
#     its_evidence.py`` and ``tests/static_gates/test_keychain_macos_job_is_
#     a_merge_gate.py`` both AST-parse this module to read
#     ``_STRICT_SWITCH_ENV`` from it;
#   * "the file is complete" — the E2E tests themselves assert it, once the
#     module is collectible at all.
#
# A third committed gate would re-assert a property the suite already holds
# twice, and would move the static-gate counts without adding coverage. So
# this is deliberately command-level: something the plan runs last, not a
# file under ``tests/static_gates/``.
#
# What it checks
# --------------
#   1. the module's first statement is a docstring, and the docstring is
#      substantial (not a one-line stub);
#   2. none of the corruption-signature markers appear in the first 5 lines;
#   3. the line count is above a floor — both an absolute one and one derived
#      from the committed copy, because a legitimate docstring edit moves the
#      number and a check that broke on those would be a check people switch
#      off;
#   4. no *other* tracked file in the tree lost a block of lines against HEAD,
#      because the same failure has been seen in more than one file;
#   5. ``pytest --collect-only -q`` exits 0 and collects the same number of
#      cases as the committed copy — an edit that silently dropped a test
#      would otherwise leave the module parseable and this guard green.
#
# Checks 1-4 live in ``scripts/scanners/scan_e2e_module_intact.py``; check 5
# needs two pytest runs and lives here. Every check prints the command it ran
# and the output it got. A guard whose result is asserted but not quoted is
# not evidence.
#
# Exit contract
# -------------
#   * exit 0 — all checks ran, none fired
#   * exit 1 — at least one check fired (the plan is not finished)
#   * exit 2 — a check could not run (no git, no venv python, no HEAD commit)
#
# Boundary: read-only
# --------------------
# This script never writes to the module and never repairs it. If a check
# fires, the correct response is to restore the module and report — editing
# it until the guard goes quiet would destroy exactly the evidence the guard
# exists to preserve. The only file this script writes is a scratch copy of
# the HEAD blob under ``backend/tests/``, used to establish the collected-case
# baseline; it is removed by a trap on every exit path.
#
# Usage
# -----
#   bash scripts/check_e2e_module_intact.sh
#   bash scripts/check_e2e_module_intact.sh --target <module.py>
#   bash scripts/check_e2e_module_intact.sh --help
#
# ``--target`` exists so the guard's red path can be rehearsed against a
# deliberately corrupted copy. The default target is the one module this plan
# keeps destroying, and a guard that can only ever be pointed there cannot be
# shown to go red without first destroying the thing it is guarding.
#
# Conforms to the same shape as ``scripts/grep_guard.sh``: root resolved from
# BASH_SOURCE so it works from any cwd, venv Python preferred over system
# Python per CLAUDE.md, and a three-way exit code shared with any caller.

set -uo pipefail

# ---------------------------------------------------------------------------
# Path resolution — script lives at ``<project_root>/scripts/`` so the project
# root is the parent of the directory this script is in. BASH_SOURCE rather
# than ``$0`` so an absolute or relative invocation both work.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
VENV_PY="$BACKEND_DIR/.venv/bin/python3"

# The module under guard, relative to the project root, so every message and
# every pytest invocation below reads the same way regardless of cwd.
#
# ``--target`` repoints it. The guard's default target is the module this plan
# keeps destroying, and a check that can only ever be pointed at that one file
# cannot be rehearsed against a deliberately corrupted copy — so pointing it at
# a throwaway copy is how the red path gets tested without touching the real
# module. Nothing about the checks changes with the target.
TARGET_REL="backend/tests/e2e/test_keychain_full_delivery_macos.py"
SCANNER="$SCRIPT_DIR/scanners/scan_e2e_module_intact.py"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'USAGE'
scripts/check_e2e_module_intact.sh — end-of-plan survival check

Usage:
  bash scripts/check_e2e_module_intact.sh
  bash scripts/check_e2e_module_intact.sh --target <module.py>
  bash scripts/check_e2e_module_intact.sh --help

Checks that the whole-delivery E2E module was not overwritten by a work
report during the plan. Read-only: it never edits or repairs the file.

  --target <module.py>   check a module other than the plan's usual one.
                         Used to rehearse the guard against a deliberately
                         corrupted copy; the checks are unchanged.

Exit codes:
  0  all checks ran, none fired
  1  at least one check fired
  2  a check could not run (setup error)
USAGE
    exit 0
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --target)
            if [ $# -lt 2 ]; then
                echo "ERROR: --target requires a module path" >&2
                exit 2
            fi
            TARGET_REL="$2"
            shift 2
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 2
            ;;
    esac
done

# Scratch copy of the HEAD blob, and the pytest path to the module under
# guard. Both are derived from the target's location rather than hardcoded, so
# ``--target`` moves the whole check and not just the structural half.
#
# The scratch copy sits in the target's own directory so pytest resolves the
# same conftest chain it would for the real module — which is what makes the
# two collected-case counts comparable. Its directory name starts with a dot
# and does not match ``test_*.py``, so a directory-wide collection run
# alongside it will not pick the scratch copy up as a test module of its own.
TARGET_ABS="$PROJECT_ROOT/$TARGET_REL"
TARGET_PYTEST_REL="${TARGET_REL#"$PROJECT_ROOT/"}"
TARGET_PYTEST_REL="${TARGET_PYTEST_REL#backend/}"
TARGET_DIR="$(dirname "$TARGET_REL")"
SCRATCH_DIR="$PROJECT_ROOT/$TARGET_DIR/.e2e_intact_headref"
# Built from the pytest-relative path, not from the project-root-relative one:
# pytest is invoked from ``backend/``, so the ``backend/`` prefix that
# ``TARGET_REL`` carries would make it look for a file one directory too high.
SCRATCH_PYTEST_REL="$(dirname "$TARGET_PYTEST_REL")/.e2e_intact_headref/head_ref_copy.py"

# ---------------------------------------------------------------------------
# Pre-flight — exit 2 rather than 1, so a caller can tell "the guard found
# corruption" apart from "the guard could not run".
# ---------------------------------------------------------------------------

if [ ! -d "$BACKEND_DIR" ]; then
    echo "ERROR: backend dir not found: $BACKEND_DIR" >&2
    exit 2
fi
if [ ! -f "$TARGET_ABS" ]; then
    echo "ERROR: module under guard not found: $TARGET_REL" >&2
    exit 2
fi
if [ ! -f "$SCANNER" ]; then
    echo "ERROR: scanner not found: $SCANNER" >&2
    exit 2
fi
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git not on PATH" >&2
    exit 2
fi
if ! git -C "$PROJECT_ROOT" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "ERROR: no HEAD commit in the project root — no baseline to compare against" >&2
    exit 2
fi

PYTHON_CMD=""
if [ -x "$VENV_PY" ]; then
    PYTHON_CMD="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    echo "WARNING: venv python not found; falling back to system python3" >&2
    echo "WARNING: results may differ from CI — please create the venv per CLAUDE.md" >&2
    PYTHON_CMD="$(command -v python3)"
else
    echo "ERROR: neither venv python ($VENV_PY) nor system python3 found" >&2
    exit 2
fi

cleanup() { rm -rf "$SCRATCH_DIR"; }
trap cleanup EXIT

FIRED=0

echo "=== module under guard: $TARGET_REL"
echo "=== HEAD: $(git -C "$PROJECT_ROOT" rev-parse --short HEAD)"
echo

# ---------------------------------------------------------------------------
# Checks 1-4 — docstring intact, header clean, line count above the floor, and
# no mass deletion anywhere else in the tree. Delegated to the scanner so the
# structural rules live in exactly one place.
# ---------------------------------------------------------------------------

echo "--- [1-4] module structure and tree-wide mass-deletion sweep"
echo "\$ python3 scripts/scanners/scan_e2e_module_intact.py --target $TARGET_REL"

"$PYTHON_CMD" "$SCANNER" --target "$TARGET_REL"
SCAN_RC=$?

if [ "$SCAN_RC" -eq 2 ]; then
    echo "ERROR: the structural scan could not run" >&2
    exit 2
elif [ "$SCAN_RC" -ne 0 ]; then
    echo "FAIL  structural scan fired — see the problems above" >&2
    FIRED=1
else
    echo "      OK"
fi
echo

# ---------------------------------------------------------------------------
# Check 5 — the module collects, and collects the same number of cases as the
# committed copy.
#
# The baseline is measured by collecting a scratch copy of the HEAD blob, not
# by a hardcoded number. A hardcoded count would itself be a claim nobody
# re-measures, and it would break the first time a test was legitimately
# added.
# ---------------------------------------------------------------------------

echo "--- [5] module collects, case count matches the committed copy"

# Collect a module in isolation, print the pytest summary line, then print the
# case count on its own line. The two callers differ in how they strip it:
# one wants the prose, one wants the number.
collect() {
    local target="$1"
    local out rc count
    out="$(cd "$BACKEND_DIR" && "$PYTHON_CMD" -m pytest "$target" --collect-only -q 2>&1)"
    rc=$?
    printf '%s\n' "$out" | tail -1
    # "N tests collected" / "1 test collected". A collection error leaves no
    # such line, and the count comes back empty — which the caller treats as
    # a failure rather than as zero.
    count="$(printf '%s\n' "$out" | grep -oE '[0-9]+ tests? collected' | grep -oE '[0-9]+' | tail -1)"
    printf '%s\n' "$count"
    return "$rc"
}

WORKTREE_OUT="$(collect "$TARGET_PYTEST_REL")"
WORKTREE_COLLECT_RC=$?
WORKTREE_COUNT="$(printf '%s\n' "$WORKTREE_OUT" | tail -1)"
# Drop the count line to leave the pytest summary. ``sed '$d'`` rather than
# ``head -n -1``: the negative line count is a GNU extension, and macOS ships
# a BSD head that rejects it.
WORKTREE_SUMMARY="$(printf '%s\n' "$WORKTREE_OUT" | sed '$d' | tail -1)"

rm -rf "$SCRATCH_DIR"
mkdir -p "$SCRATCH_DIR"
if ! git -C "$PROJECT_ROOT" show "HEAD:$TARGET_REL" > "$SCRATCH_DIR/head_ref_copy.py"; then
    echo "ERROR: could not read HEAD:$TARGET_REL" >&2
    exit 2
fi

HEAD_OUT="$(collect "$SCRATCH_PYTEST_REL")"
HEAD_COLLECT_RC=$?
HEAD_COUNT="$(printf '%s\n' "$HEAD_OUT" | tail -1)"
HEAD_SUMMARY="$(printf '%s\n' "$HEAD_OUT" | sed '$d' | tail -1)"

echo "    working tree : ${WORKTREE_SUMMARY:-<no summary>}  [pytest exit $WORKTREE_COLLECT_RC]"
echo "    HEAD blob    : ${HEAD_SUMMARY:-<no summary>}  [pytest exit $HEAD_COLLECT_RC]"

if [ "$WORKTREE_COLLECT_RC" -ne 0 ]; then
    echo "FAIL  pytest --collect-only exited $WORKTREE_COLLECT_RC on the working tree" >&2
    FIRED=1
elif [ -z "$WORKTREE_COUNT" ]; then
    echo "FAIL  pytest --collect-only collected no cases on the working tree" >&2
    FIRED=1
elif [ -z "$HEAD_COUNT" ]; then
    echo "FAIL  could not establish the HEAD baseline case count — nothing to compare against" >&2
    FIRED=1
elif [ "$WORKTREE_COUNT" != "$HEAD_COUNT" ]; then
    echo "FAIL  case count changed: working tree $WORKTREE_COUNT vs HEAD $HEAD_COUNT" >&2
    FIRED=1
else
    echo "    OK — $WORKTREE_COUNT cases, unchanged"
fi
echo

# ---------------------------------------------------------------------------

if [ "$FIRED" -ne 0 ]; then
    echo "=== GUARD FIRED"
    echo "=== Do not edit $TARGET_REL to satisfy these checks. Restore the"
    echo "=== module from HEAD and report; the guard exists to preserve the"
    echo "=== evidence that the file was overwritten."
    exit 1
fi

echo "=== GUARD CLEAN — $TARGET_REL survived intact"
exit 0
