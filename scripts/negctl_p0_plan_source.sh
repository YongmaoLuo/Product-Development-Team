#!/usr/bin/env bash
# scripts/negctl_p0_plan_source.sh — the negative control for "the plan
# document has to be translated before the verification round consumes it".
#
# The claim this control is about
# -------------------------------
# ``/api/verification/{plan_id}/start`` runs a **prelude** before the phase
# CAS.  One step of that prelude is the translation of the reviewed plan
# document into the machine plan the round actually consumes, and the
# boundary test ``backend/tests/integration/test_verification_prelude_
# boundaries.py::test_dev_branch_with_plan_doc_prepares_machine_plan_only``
# pins it: on a development branch that holds a plan document, the
# translation happens **once**, and the machine plan lands on disk.
#
# A suite of statements like that measures nothing if the same suite would
# also be green when the product did none of it — "the machine plan landed"
# has to be a fact about the round, not the absence of a complaint.
#
# How the control proves it
# -------------------------
# The switch is ``scripts/negctl_fixtures/p0_plan_source_none.py``: loaded
# as a pytest plugin it replaces ``plan_artifacts.resolve_plan_source``
# with a function that always answers ``PlanSource.NONE`` — the source that
# says "there is no reviewed plan in this directory".  With the source
# pinned that way the reviewed document cannot be translated, so the
# machine plan cannot land, so the suite must turn **red**.
#
# The control therefore runs the same suite twice:
#
#   GREEN — without the switch, the suite passes.  (If it does not, the
#           claim is broken before the control even starts.)
#   RED   — with the switch, the suite fails, and it fails **on the
#           translation assertion**.  (If it still passed, the assertions
#           are not load-bearing and the suite measures nothing.)
#
# A control that cannot fail is not a control.  So the script refuses to
# report a vacuous PASS: it requires the pinned run to fail *for the right
# reason* (pytest exit 1 — a test failure — and the translation assertion
# in the output), and it requires the switch to have taken effect at all
# (the plugin leaves a marker file behind, and that marker names the
# function it replaced).
#
# Two controls, one switch
# -------------------------
# The same claim is pinned twice, at two different levels, and each level
# has its own way of going wrong — so each has its own control:
#
#   (default)  the in-process boundary test.  Fast, and it sees the
#              prelude's own side effects (how many times the translator
#              was asked, whether the machine plan landed).
#   ``--e2e``  the real-process coexistence end-to-end test.  Slow, and it
#              sees what the round *actually* consumed — a document the
#              round read is not the same thing as a document the round
#              used.
#
# Both are the same switch on the same function, so both are the same
# control run against a different suite.  They are separate steps in CI so
# that a failure in one does not hide the other.
#
# Usage
# -----
#   bash scripts/negctl_p0_plan_source.sh          # the prelude control
#   bash scripts/negctl_p0_plan_source.sh --e2e    # the end-to-end control
#
# Exit contract
# -------------
#   * exit 0 — the control holds (GREEN, RED, PASS)
#   * exit 1 — a check fired (the control is vacuous, or the claim broke)
#   * exit 2 — the control could not run (no venv / no python / no suite)
#
# Boundary: read-only
# --------------------
# The script writes nothing into the working tree: the marker file and the
# two pytest logs live inside its own ``mktemp -d`` directory, which the
# exit trap removes on every exit path.  ``PYTHONDONTWRITEBYTECODE=1``
# keeps the interpreter from dropping a ``__pycache__`` next to the
# plugin either.

set -uo pipefail

# ---------------------------------------------------------------------------
# Path resolution — the script lives at ``<project_root>/scripts/`` so the
# project root is the parent of its own directory.  ``BASH_SOURCE`` rather
# than ``$0`` so an absolute, a relative and an ``sh``-style invocation all
# resolve the same way.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
VENV_PY="$BACKEND_DIR/.venv/bin/python3"

#: The switch, relative to the project root.  It is loaded as a pytest
#: plugin, so the directory holding it goes on ``PYTHONPATH``.
PLUGIN_REL="scripts/negctl_fixtures/p0_plan_source_none.py"
PLUGIN_MODULE="p0_plan_source_none"
PLUGIN_DIR="$PROJECT_ROOT/scripts/negctl_fixtures"

#: The workflow that has to run this control.
CI_REL=".github/workflows/ci.yml"

# ---------------------------------------------------------------------------
# Which control to run.  The default is the prelude boundary control; the
# end-to-end control is asked for explicitly, so that a reader of the CI
# step can tell which claim the step is making.
# ---------------------------------------------------------------------------

MODE="prelude"
case "${1:-}" in
    "")
        MODE="prelude"
        ;;
    --e2e)
        MODE="e2e"
        ;;
    -h|--help)
        sed -n '2,80p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "ERROR: unknown argument: $1" >&2
        echo "       usage: bash scripts/negctl_p0_plan_source.sh [--e2e]" >&2
        exit 2
        ;;
esac
if [ "$#" -gt 1 ]; then
    echo "ERROR: this control takes at most one argument" >&2
    echo "       usage: bash scripts/negctl_p0_plan_source.sh [--e2e]" >&2
    exit 2
fi

if [ "$MODE" = "prelude" ]; then
    LABEL="the verification prelude boundary"
    TEST_FILE="tests/integration/test_verification_prelude_boundaries.py"
    TEST_NODE="::test_dev_branch_with_plan_doc_prepares_machine_plan_only"
    # The assertion that has to be the one that fails.  It is the statement
    # "the plan document was translated once" — the claim this control is
    # about.  Any other failure is a different problem and must not be
    # reported as a control.
    RED_MARKER='assert probes.count("prepare") == 1'
    RED_TEST_NAME="test_dev_branch_with_plan_doc_prepares_machine_plan_only"
    CI_STEP_PATTERN='^[[:space:]]*run:[[:space:]]*bash[[:space:]]+scripts/negctl_p0_plan_source\.sh[[:space:]]*$'
    CI_STEP_HINT="run: bash scripts/negctl_p0_plan_source.sh"
else
    LABEL="the coexistence end-to-end test"
    TEST_FILE="tests/e2e/test_verification_plan_coexist_e2e.py"
    TEST_NODE=""
    # The message the end-to-end suite fails with when the machine plan did
    # not land.  The control checks for it so that a pinned run which
    # failed for some other reason does not pass as a control.
    RED_MARKER="the machine plan did not land"
    RED_TEST_NAME="test_coexist_full_user_flow"
    CI_STEP_PATTERN='^[[:space:]]*run:[[:space:]]*bash[[:space:]]+scripts/negctl_p0_plan_source\.sh[[:space:]]+--e2e[[:space:]]*$'
    CI_STEP_HINT="run: bash scripts/negctl_p0_plan_source.sh --e2e"
fi

TEST_ARGS=("$TEST_FILE$TEST_NODE")

# ---------------------------------------------------------------------------
# Scratch space — everything this script writes lives here, and the trap
# removes it on every exit path (including the failure paths).
# ---------------------------------------------------------------------------

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/negctl_p0_plan_source.XXXXXX")" || {
    echo "ERROR: could not create a temporary directory" >&2
    exit 2
}

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Pre-flight — exit 2 rather than 1, so a caller can tell "the control
# found a problem" apart from "the control could not run".
# ---------------------------------------------------------------------------

# The behavioural suite must run on the project venv (CLAUDE.md: never run
# pytest on the system interpreter), so a missing venv is a setup error and
# not a fallback.
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: the project venv python is missing: $VENV_PY" >&2
    echo "       create it per CLAUDE.md — the suite must not run on the system interpreter" >&2
    exit 2
fi

if [ ! -f "$BACKEND_DIR/$TEST_FILE" ]; then
    echo "ERROR: the suite this control is about is missing: backend/$TEST_FILE" >&2
    exit 2
fi

if [ ! -f "$PROJECT_ROOT/$PLUGIN_REL" ]; then
    echo "ERROR: the negative control's switch is missing: $PLUGIN_REL" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# The wiring self-check — a control that nothing runs proves nothing.  The
# workflow has to run this control, so the control checks that it does.
# ---------------------------------------------------------------------------

CI_FILE="$PROJECT_ROOT/$CI_REL"
if [ ! -f "$CI_FILE" ]; then
    echo "ERROR: the workflow file is missing: $CI_REL" >&2
    exit 2
fi

if ! grep -Eq "$CI_STEP_PATTERN" "$CI_FILE"; then
    echo "FAIL: $CI_REL does not run this control" >&2
    echo "      it needs a step whose run line is exactly:" >&2
    echo "        $CI_STEP_HINT" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# The control.
# ---------------------------------------------------------------------------

echo "=== negctl P0 plan source — negative control for $LABEL"
echo

# --- GREEN: the suite passes on its own ------------------------------------

GREEN_LOG="$WORK_DIR/green.log"
(
    cd "$BACKEND_DIR" || exit 2
    # The outer pytest session's environment must not leak into this one:
    # PYTEST_ADDOPTS would inject options, and COVERAGE_FILE would point
    # two pytest runs at one coverage file.
    unset PYTEST_ADDOPTS PYTEST_CURRENT_TEST PYTEST_PLUGINS COVERAGE_FILE
    PYTHONDONTWRITEBYTECODE=1 \
    "$VENV_PY" -m pytest "${TEST_ARGS[@]}" -p no:cacheprovider --tb=short -q
) > "$GREEN_LOG" 2>&1
GREEN_RC=$?

if [ "$GREEN_RC" -ne 0 ]; then
    echo "FAIL: $LABEL does not pass on its own (pytest exit $GREEN_RC)" >&2
    echo "      the claim this control is about is already broken" >&2
    tail -n 40 "$GREEN_LOG" >&2
    exit 1
fi

GREEN_SUMMARY="$(grep -E '[0-9]+ (passed|failed|error)' "$GREEN_LOG" | tail -n 1)"
echo "=== pytest $TEST_FILE$TEST_NODE -> ${GREEN_SUMMARY:-no summary line} (exit $GREEN_RC)"
echo "GREEN: $LABEL passes without the switch"

# --- RED: with the source pinned to NONE the same suite fails --------------

RED_LOG="$WORK_DIR/red.log"
MARKER_FILE="$WORK_DIR/switch-marker.txt"

(
    cd "$BACKEND_DIR" || exit 2
    unset PYTEST_ADDOPTS PYTEST_CURRENT_TEST PYTEST_PLUGINS COVERAGE_FILE
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    PDT_NEGCTL_MARKER_FILE="$MARKER_FILE" \
    "$VENV_PY" -m pytest "${TEST_ARGS[@]}" -p no:cacheprovider --tb=short -q \
    -p "$PLUGIN_MODULE"
) > "$RED_LOG" 2>&1
RED_RC=$?

# A plugin that pytest could not import ends the session with the same
# exit code as a test failure (1), so the exit code alone cannot tell
# "the suite turned red" from "the switch never ran".  The reason is in
# the output, so it is read from there.
if grep -qiE "error importing plugin .*${PLUGIN_MODULE}|no module named ['\"]?${PLUGIN_MODULE}" "$RED_LOG"; then
    echo "FAIL: the negative control's switch was not loaded by pytest (-p $PLUGIN_MODULE)" >&2
    echo "      the pinned run below is not a pinned run:" >&2
    grep -iE "error importing plugin|no module named" "$RED_LOG" | head -n 5 >&2
    exit 1
fi

# The switch has to have taken effect: a plugin that failed to load, or
# one that did not manage to replace the target function, would leave the
# suite green — and the control would then be reporting a difference it
# did not cause.
if [ ! -s "$MARKER_FILE" ]; then
    echo "FAIL: the negative control's switch never took effect (no marker file)" >&2
    echo "      the pinned run below is not a pinned run" >&2
    tail -n 40 "$RED_LOG" >&2
    exit 1
fi

if ! grep -q "target=plan_artifacts.resolve_plan_source" "$MARKER_FILE"; then
    echo "FAIL: the negative control's switch did not replace the target function" >&2
    echo "      the marker does not name plan_artifacts.resolve_plan_source:" >&2
    cat "$MARKER_FILE" >&2
    exit 1
fi

if ! grep -q "returns=PlanSource.NONE" "$MARKER_FILE"; then
    echo "FAIL: the negative control's switch does not answer PlanSource.NONE" >&2
    echo "      the marker says:" >&2
    cat "$MARKER_FILE" >&2
    exit 1
fi

if [ "$RED_RC" -eq 0 ]; then
    echo "FAIL: $LABEL passed with the plan source pinned to NONE" >&2
    echo "      the assertions this control is about are not load-bearing:" >&2
    echo "      the suite would be green whether or not the product worked" >&2
    tail -n 40 "$RED_LOG" >&2
    exit 1
fi

if [ "$RED_RC" -ne 1 ]; then
    echo "FAIL: the pinned run ended with pytest exit $RED_RC (1 is a test failure)" >&2
    echo "      an INTERNALERROR (3) means the switch itself broke" >&2
    tail -n 40 "$RED_LOG" >&2
    exit 1
fi

# The failure has to be the one this control is about: the assertion that
# says "the plan document was translated", in the test that says so.
if ! grep -q "$RED_TEST_NAME" "$RED_LOG"; then
    echo "FAIL: the pinned run failed, but not in the test this control is about" >&2
    echo "      expected the failure to be in: $RED_TEST_NAME" >&2
    tail -n 40 "$RED_LOG" >&2
    exit 1
fi

if ! grep -qF "$RED_MARKER" "$RED_LOG"; then
    echo "FAIL: the pinned run failed, but not on the assertion this control is about" >&2
    echo "      expected the failure to mention: $RED_MARKER" >&2
    tail -n 40 "$RED_LOG" >&2
    exit 1
fi

RED_SUMMARY="$(grep -E '[0-9]+ (passed|failed|error)' "$RED_LOG" | tail -n 1)"
echo "=== pytest $TEST_FILE$TEST_NODE (source pinned to NONE) -> ${RED_SUMMARY:-no summary line} (exit $RED_RC)"
echo "RED: with the plan source pinned to NONE the machine plan does not land, and the suite says so"

echo
echo "PASS: negctl-p0-plan-source ($MODE)"
exit 0
