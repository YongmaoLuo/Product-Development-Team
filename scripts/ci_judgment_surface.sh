#!/usr/bin/env bash
#
# The three judgment-surface consistency checks, behind one entry point.
#
# Why this script exists
# ----------------------
# The judgment surface is described by three artifacts that are allowed
# to drift apart:
#
#   * the manifest — which functions decide PASS/FAIL, and what pins
#     each of them;
#   * the decision tables the manifest's Tier A rows name — a table that
#     was renamed or emptied is a claim about a table that is not there;
#   * the behavioural goldens — what those functions actually return,
#     frozen byte for byte.
#
# Each of the three has its own checker, and each checker is a script or
# a test file — which means that until something runs it, it is a file
# in a repository and nothing else.  Three separate entry points are
# also three ways for the chain to rot: a link can stop running while
# the other two keep reporting green, and the reader of a CI log has to
# know which of the three to believe.
#
# So this script is the one entry point.  It runs all three, in order,
# and answers with one number.
#
# The three checks, in order
# --------------------------
#   1. the judgment-point census — does the manifest still describe the
#      tree it was generated from?
#   2. the three judgment static gates — the manifest's own rows, the
#      decision tables those rows name, and the coverage report;
#   3. the behavioural goldens — do the frozen outputs still match what
#      the code produces today?
#
# Each one describes the tree the next one reads, which is why the
# order is part of what this script means.
#
# Which of the three block
# -------------------------
# The census check is **advisory** and the other two are **blocking**,
# and the reason is the rollout decision the judgment-surface work
# started from: a manifest that does not match the code warns in the
# first version and does not block.  A new judgment function that
# nobody registered would turn every pull request that touches a
# judgment module red, and regenerating the manifest is not yet part of
# a reviewer's normal loop — so the drift is *reported*, here and in
# the static-gates job, and the thing that blocks instead is the static
# gate that says the tables the manifest names are really there.
#
# That is why the drift and the blocking checks cannot be confused with
# one another: a drift says "the manifest may be out of date", and a
# red table gate says "the manifest names a table that is not there" —
# the second one is a defect in the tree, and defects block.
#
# `--strict` turns the advisory check into a blocking one, for a
# rollout that has decided the drift may block from now on.
#
# Usage
# -----
#   PYTHON=<interpreter> bash scripts/ci_judgment_surface.sh [--root DIR] [--strict]
#
# `--root` is the tree to check; it defaults to the repository this
# script lives in.  ``PYTHON`` names the interpreter the checkers run
# under and defaults to ``python3``; a relative name is looked up
# beside the root and beside the root's ``backend/`` directory, which is
# where this repository's virtual environment lives.
#
# Exit status
# -----------
#   0 — no blocking check failed.  An advisory drift is reported in the
#       log and does not change this.
#   1 — a blocking check failed.
#   2 — the script could not run at all (a bad argument, a missing root,
#       no interpreter).
#
# The failing check's own status is in the log; the script's status is
# one number, because one number is what the CI job and the shell read.
#
# The summary line
# ----------------
# On a tree whose three checks all pass, the last line is
#
#     JUDGMENT SURFACE: OK (3/3 checks)
#
# A tree whose only failure is the advisory one says
#
#     JUDGMENT SURFACE: WARN (2/3 checks passed, 1 advisory drift)
#
# and a tree with a blocking failure says
#
#     JUDGMENT SURFACE: FAILED (1/3 checks passed)
#
# The counts are the number of checks that ran and the number of them
# that passed, so they cannot claim a check passed when it did not.

set -euo pipefail

#: The directory this script lives in — ``<root>/scripts``.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

#: The tree the three checks read.  ``--root`` overrides it; it
#: defaults to the repository this script lives in.
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

#: The interpreter the checkers run under.
PYTHON="${PYTHON:-python3}"

#: Whether the advisory check blocks too.
STRICT=0

usage() {
    printf '%s\n' \
        "Usage: bash scripts/ci_judgment_surface.sh [--root DIR] [--strict]" \
        "" \
        "Runs the judgment-point census, the three judgment static gates" \
        "and the behavioural goldens against the tree at DIR (default: the" \
        "repository this script lives in), and exits 1 if a blocking check" \
        "failed." \
        "" \
        "The census check is advisory by default — a manifest that does not" \
        "match the code warns and does not block. --strict makes it block" \
        "too." \
        "" \
        "The interpreter comes from \$PYTHON and defaults to python3."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --root)
            if [ "$#" -lt 2 ]; then
                printf '%s\n' \
                    "ci_judgment_surface: --root needs a directory" >&2
                exit 2
            fi
            ROOT="$2"
            shift 2
            ;;
        --root=*)
            ROOT="${1#*=}"
            shift
            ;;
        --strict)
            STRICT=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'ci_judgment_surface: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ ! -d "$ROOT" ]; then
    printf 'ci_judgment_surface: no such directory: %s\n' "$ROOT" >&2
    exit 2
fi
ROOT="$(cd "$ROOT" && pwd)"

#: The absolute path of a file that is already known to be executable.
absolute_path() {
    printf '%s/%s\n' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")"
}

#: The interpreter the checkers run under, as an absolute path.
#:
#: A relative ``PYTHON`` is looked up in three places: beside the caller,
#: beside the root, and beside the root's ``backend/`` directory.  The
#: last one is where this repository's virtual environment lives, so
#: ``PYTHON=.venv/bin/python3`` finds it without the caller having to
#: know where it is.
resolve_interpreter() {
    local candidate
    if [ -x "$PYTHON" ]; then
        absolute_path "$PYTHON"
        return 0
    fi
    for candidate in "${ROOT}/${PYTHON}" "${ROOT}/backend/${PYTHON}"; do
        if [ -x "$candidate" ]; then
            absolute_path "$candidate"
            return 0
        fi
    done
    if command -v "$PYTHON" >/dev/null 2>&1; then
        command -v "$PYTHON"
        return 0
    fi
    printf 'ci_judgment_surface: no interpreter named PYTHON=%s\n' "$PYTHON" >&2
    return 2
}

INTERPRETER="$(resolve_interpreter)" || exit 2

#: How many checks ran, and how many of them passed.
TOTAL=0
PASSED=0
BLOCKING_FAILED=0
ADVISORY_FAILED=0

#: Run one check, in ``$3``, and count it.
#:
#: ``$1`` is the check's severity — ``blocking`` or ``advisory`` — and
#: ``$2`` is what the log calls it.  The check's own status is kept for
#: the log; a failure is counted rather than fatal, so that one CI log
#: carries the verdict of all three checks instead of only the first one
#: that failed.
run_check() {
    local severity="$1"
    local label="$2"
    local dir="$3"
    shift 3
    local code=0
    TOTAL=$((TOTAL + 1))
    printf '\n=== %s ===\n' "$label"
    ( cd "$dir" && "$@" ) || code=$?
    if [ "$code" -eq 0 ]; then
        PASSED=$((PASSED + 1))
        printf '  -> passed\n'
        return 0
    fi
    if [ "$severity" = "blocking" ] || [ "$STRICT" -eq 1 ]; then
        BLOCKING_FAILED=$((BLOCKING_FAILED + 1))
        printf '  -> FAILED (exit %d)\n' "$code" >&2
    else
        ADVISORY_FAILED=$((ADVISORY_FAILED + 1))
        printf '  -> ADVISORY: reported, not blocking (exit %d)\n' "$code" >&2
    fi
    return 0
}

# 1. The manifest against the tree it was generated from.  Advisory: see
#    the header for why a drifting manifest warns rather than blocks.
run_check "advisory" "judgment-point census" \
    "${ROOT}" \
    "${INTERPRETER}" "${ROOT}/scripts/census_judgment_points.py" \
    --check --root "${ROOT}"

# 2. The manifest's own rows, the decision tables those rows name, and
#    the coverage report.  These are test *files*, so they run under
#    pytest — and they resolve the tree they inspect from their own
#    location, which is why they have to be the ones in the tree under
#    test rather than the ones beside this script.
#
#    The per-test bound comes from backend/pytest.ini (timeout = 300), so
#    no timeout flag is repeated here.
run_check "blocking" "judgment static gates (manifest / tables / coverage)" \
    "${ROOT}/backend" \
    "${INTERPRETER}" -m pytest \
    tests/static_gates/test_judgment_points_manifest.py \
    tests/static_gates/test_judgment_points_tables_exist.py \
    tests/static_gates/test_judgment_snapshot_coverage.py \
    -p no:cacheprovider -q

# 3. The frozen outputs against what the code produces today.
run_check "blocking" "behavioural goldens" \
    "${ROOT}" \
    "${INTERPRETER}" "${ROOT}/scripts/judgment_snapshots.py" \
    --check --root "${ROOT}"

if [ "$BLOCKING_FAILED" -ne 0 ]; then
    printf 'JUDGMENT SURFACE: FAILED (%d/%d checks passed)\n' \
        "$PASSED" "$TOTAL" >&2
    exit 1
fi

if [ "$ADVISORY_FAILED" -ne 0 ]; then
    printf 'JUDGMENT SURFACE: WARN (%d/%d checks passed, %d advisory drift)\n' \
        "$PASSED" "$TOTAL" "$ADVISORY_FAILED"
    exit 0
fi

printf 'JUDGMENT SURFACE: OK (%d/%d checks)\n' "$PASSED" "$TOTAL"
