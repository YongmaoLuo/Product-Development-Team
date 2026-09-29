#!/usr/bin/env bash
# scripts/grep_guard.sh — shared grep-guard wrapper.
#
# Purpose
# -------
# Single entry point for the forbidden-filename static gate. The
# underlying scanner is ``backend/tests/grep_guard.py::run_grep_guard``
# (see also ``backend/tests/test_server_json_cleanup.py`` for the
# pin-down contract). This script wraps it so that:
#
#   * developers can run ``bash scripts/grep_guard.sh`` from any cwd
#     and get the same result CI does;
#   * pre-commit (``.pre-commit-config.yaml``) can call the same
#     script as a local hook;
#   * the GitHub Actions ``grep-guard`` job can call the same
#     script as the legacy inline ``python3 -c`` invocation did.
#
# Exit contract
# -------------
#   * exit 0 — scan ran, zero violations
#   * exit 1 — scan ran, at least one violation (CI / pre-commit fail)
#   * exit 2 — usage / setup error (venv missing, project root not
#     found, scanner import failure)
#
# The output is human-readable on stdout/stderr (one line per
# violation + a summary block) so the failure message points the
# developer straight at the offending file and line.
#
# Boundary conditions
# -------------------
#   * script location is the source of truth for the project root —
#     we resolve ``scripts/`` → project root via BASH_SOURCE so it
#     works from any cwd (the backend launches scripts via
#     ``subprocess.run`` from arbitrary cwds);
#   * the venv Python is preferred (CLAUDE.md: never use system
#     Python to run grep-guard — it has stale urllib3 + missing
#     packages); the script falls back to system Python only when
#     the venv is genuinely missing, and prints a clear warning;
#   * ``set -euo pipefail`` makes any failure surface immediately
#     so a partial scan never hides a violation.
#
# Usage
# -----
#   bash scripts/grep_guard.sh                 # default production scope
#   bash scripts/grep_guard.sh --json          # machine-readable output
#   bash scripts/grep_guard.sh --root <dir>    # scan an alternate root
#   bash scripts/grep_guard.sh --help
#
# Why a shared wrapper (and not just the inline ``python3 -c``)
# -------------------------------------------------------------
# The previous inline call lived in three places (CI yml,
# ``scripts/run_tests.sh`` style helpers, and developer ad-hoc
# shells). Every place had to rediscover the venv path, the right
# ``sys.path.insert`` for the standalone ``grep_guard`` module, and
# the exit-code convention. A single shell script means:
#
#   1. one place to fix when the scanner's default scope changes;
#   2. one place to add ``--json`` / ``--quiet`` flags;
#   3. one exit-code contract shared between CI and pre-commit.
#
# This is the same DRY pattern the project uses for
# ``scripts/run_tests.sh`` (the venv-aware pytest wrapper).

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution — script lives at ``<project_root>/scripts/grep_guard.sh``
# so the project root is the parent of the directory this script is in.
# We use BASH_SOURCE so this works whether the script is invoked via
# ``bash scripts/grep_guard.sh``, ``./scripts/grep_guard.sh``, or
# ``bash /abs/path/to/scripts/grep_guard.sh``.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
VENV_PY="$BACKEND_DIR/.venv/bin/python3"
SCANNER_DIR="$BACKEND_DIR/tests"

# ---------------------------------------------------------------------------
# CLI parsing — minimal, just enough for the CI / pre-commit callers.
# ---------------------------------------------------------------------------

JSON_MODE=0
PRINT_HELP=0
CUSTOM_ROOT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --json)
            JSON_MODE=1
            shift
            ;;
        --root)
            if [ $# -lt 2 ]; then
                echo "ERROR: --root requires a directory argument" >&2
                exit 2
            fi
            CUSTOM_ROOT="$2"
            shift 2
            ;;
        --help|-h)
            PRINT_HELP=1
            shift
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 2
            ;;
    esac
done

if [ "$PRINT_HELP" -eq 1 ]; then
    cat <<'USAGE'
scripts/grep_guard.sh — shared grep-guard wrapper

Usage:
  bash scripts/grep_guard.sh                  # default production scope
  bash scripts/grep_guard.sh --json           # machine-readable JSON output
  bash scripts/grep_guard.sh --root <dir>     # scan an alternate root
  bash scripts/grep_guard.sh --help

Exit codes:
  0  scan ran, zero violations
  1  scan ran, at least one violation (CI / pre-commit fail)
  2  usage / setup error (venv missing, project root not found, scanner import failure)
USAGE
    exit 0
fi

# ---------------------------------------------------------------------------
# Pre-flight checks — fail loudly with exit 2 (not exit 1) so the
# caller can distinguish "the gate found a violation" from "the gate
# could not run at all".
# ---------------------------------------------------------------------------

if [ ! -d "$BACKEND_DIR" ]; then
    echo "ERROR: backend dir not found: $BACKEND_DIR" >&2
    exit 2
fi
if [ ! -f "$SCANNER_DIR/grep_guard.py" ]; then
    echo "ERROR: scanner module not found: $SCANNER_DIR/grep_guard.py" >&2
    exit 2
fi

# Pick the interpreter — prefer the project venv per CLAUDE.md,
# fall back to system Python with a clear warning (exit 2 if neither
# is available, since silently running on system Python would
# produce inconsistent results).
PYTHON_CMD=""
if [ -x "$VENV_PY" ]; then
    PYTHON_CMD="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    echo "WARNING: venv python not found at $VENV_PY; falling back to system python3" >&2
    echo "WARNING: results may differ from CI — please create the venv per CLAUDE.md" >&2
    PYTHON_CMD="$(command -v python3)"
else
    echo "ERROR: neither venv python ($VENV_PY) nor system python3 found" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Build the python one-liner. We delegate to ``run_grep_guard`` so
# the pattern classes (direct_literal, string_concat, variable_reference,
# fstring_format, pathlib_join) are owned in exactly one place
# (backend/tests/grep_guard.py) — the shell script never duplicates
# the regex set.
#
# The python invocation:
#   * prepends ``$SCANNER_DIR`` to ``sys.path`` so the standalone
#     ``grep_guard`` module (which lives outside any package) imports;
#   * imports ``run_grep_guard`` and calls it with the default scope
#     (or the caller-supplied ``--root``);
#   * prints violations either as JSON (``--json``) or as a
#     human-readable table;
#   * exits 0 on clean, 1 on any violation.
# ---------------------------------------------------------------------------

if [ -n "$CUSTOM_ROOT" ]; then
    # The caller passed --root <dir>. Forward it through to the
    # scanner so the relative paths in the output are anchored on
    # that root instead of the inferred backend root.
    ROOT_ARG="root=Path('$CUSTOM_ROOT'),"
else
    ROOT_ARG=""
fi

if [ "$JSON_MODE" -eq 1 ]; then
    # Machine-readable mode: dump the violation list as JSON.
    # Pre-commit and CI both consume stdout; stderr is reserved
    # for warnings / setup errors.
    PYTHON_OUTPUT=$("$PYTHON_CMD" -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '$SCANNER_DIR')
from grep_guard import run_grep_guard
violations = run_grep_guard($ROOT_ARG)
sys.stdout.write(json.dumps(violations, indent=2, sort_keys=True))
sys.stdout.write('\n')
sys.exit(1 if violations else 0)
" 2>&1)
    PYTHON_RC=$?
else
    # Human-readable mode: one violation per line plus a summary.
    PYTHON_OUTPUT=$("$PYTHON_CMD" -c "
import sys
from pathlib import Path
sys.path.insert(0, '$SCANNER_DIR')
from grep_guard import run_grep_guard
violations = run_grep_guard($ROOT_ARG)
if not violations:
    print('OK  grep-guard: 0 violations')
    sys.exit(0)
print(f'FAIL grep-guard: {len(violations)} violation(s) found')
for v in violations:
    file = v.get('file', '?')
    line = v.get('line', '?')
    ptype = v.get('pattern_type', '?')
    matched = v.get('matched_text', '')
    print(f'  {file}:{line}  [{ptype}]  {matched}')
sys.exit(1)
" 2>&1)
    PYTHON_RC=$?
fi

# Echo the python output verbatim so the caller sees the violation
# list. Preserve exit code 1 (violations) vs 2 (setup error) —
# only the python invocation returned 0 or 1 here; if PYTHON_RC is
# neither, surface it as exit 2 so CI / pre-commit can react.
if [ "$PYTHON_RC" -eq 0 ]; then
    printf '%s\n' "$PYTHON_OUTPUT"
    exit 0
elif [ "$PYTHON_RC" -eq 1 ]; then
    printf '%s\n' "$PYTHON_OUTPUT" >&2
    exit 1
else
    printf '%s\n' "$PYTHON_OUTPUT" >&2
    exit 2
fi