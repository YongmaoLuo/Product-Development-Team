#!/usr/bin/env bash
# scripts/negctl_p0_1.sh — the negative control for P0-1.
#
# The bug this control is about
# -----------------------------
# P0-1 (2026-10-22) removed the inline ``Verdict(verdict="FAILED")``
# that ``VerificationSubAgent._execute_attempt`` used to build when the
# sub-agent's payload could not be parsed. The bug it fixed was a
# *reachability* bug: the inline verdict answered the caller's question,
# so the L2 retry branch in ``_self_heal_loop`` — the branch that exists
# exactly for a transient protocol garble (a truncated stream, stray
# prose around the JSON object) — was never taken. One garbled response
# failed the verification point for good, and the verdict it produced
# claimed the deliverable was broken when nothing was known about the
# deliverable at all.
#
# Why a control is needed at all
# ------------------------------
# "The fix works" is only half of the claim, and it is the easy half:
# the behavioural suite (``backend/tests/unit/test_verdict_parse_retry.py``)
# shows that a garbled payload is retried and that the retry's verdict
# is the one that counts.
#
# The other half is "the bug was there". Without it the fix could be a
# no-op and the behavioural suite would still be green — a suite that
# passes on both the broken and the fixed code measures nothing about
# the fix. That half is what a negative control is for, and this script
# is it:
#
#   RED   — the PRE-FIX source (read out of git, never out of the
#           working tree) really does build an inline FAILED verdict in
#           its ``VerdictParseError`` handler;
#   GREEN — the current source has no such inline verdict;
#   GREEN — the current source re-raises, so the L2 retry is reachable;
#   GREEN — the behavioural suite for the fix passes.
#
# A control that cannot fail is not a control. If the baseline no
# longer shows the pre-fix shape, this script exits non-zero and says
# so instead of reporting a vacuous PASS — and the same holds for a
# baseline it cannot read at all.
#
# Choosing the baseline
# ----------------------
# The baseline is the PRE-FIX source, and the control is only worth
# anything if that source really is the pre-fix source. There are three
# ways to get one, in the order they are tried:
#
#   1. ``P0_1_PREFIX_REV`` — an explicit revision, used as given. This
#      is how CI pins the control to one commit (see ``.github/workflows/
#      ci.yml``), and it is the only way to point the control at a
#      baseline that is not in this repository's history.
#
#   2. The history of ``backend/verification_subagent.py``: the most
#      recent revision whose ``except VerdictParseError`` arm in
#      ``_execute_attempt`` still builds the inline
#      ``Verdict(verdict="FAILED")``. That revision is the pre-fix
#      source by definition — it is the last state of the file in which
#      the shape the fix removed was still there.
#
#      A moving ref cannot do this job. ``origin/main`` stops holding
#      the pre-fix source the moment the fix is merged, so a control
#      pinned to it would start failing on merge day for a reason that
#      has nothing to do with the claim it is making.
#
#   3. The checked-in pre-fix fragment
#      (``scripts/negctl_fixtures/p0_1_prefix_verdict_arm.py``), for a
#      repository whose history no longer holds the pre-fix shape at
#      all: a shallow clone has one commit in it, and a rewritten
#      history may have none. The fragment is analysed by the same
#      analyser and held to the same assertions as a revision, so a
#      fixture that stopped carrying the pre-fix shape fails the
#      control rather than passing it.
#
# There is no fourth way. A control that cannot find a baseline cannot
# run, and it says so and exits non-zero. A control that quietly fell
# back to "there was nothing to check" would read exactly like a
# control that passed.
#
# Usage
# -----
#   bash scripts/negctl_p0_1.sh
#   P0_1_PREFIX_REV=<rev> bash scripts/negctl_p0_1.sh
#
# ``P0_1_PREFIX_REV`` names the revision that holds the PRE-FIX source.
# When it is set, that revision is the baseline; when it is not set, the
# script finds one (see "Choosing the baseline" above). When the
# baseline cannot be resolved (no git, unknown ref, the file missing at
# that ref) the script exits non-zero and says why. It never skips a
# check silently: a skipped control reads exactly like a passed one.
#
# Exit contract
# -------------
#   * exit 0 — the control is established (RED, three GREENs, PASS)
#   * exit 1 — a check fired (the control is vacuous, or the fix regressed)
#   * exit 2 — the control could not run (no git / unknown rev / no python)
#
# Boundary: read-only
# --------------------
# The baseline is read with ``git show <rev>:<path>`` into a temporary
# directory, and that directory is removed on every exit path. The
# working tree is never written to and no ref is created or moved. The
# only thing this script writes is inside its own ``mktemp -d``
# directory.
#
# How the block is located
# ------------------------
# The CURRENT file's block is located by the marker pair
# ``# NEGCTL-P0-1-BEGIN`` / ``# NEGCTL-P0-1-END`` — the same contract the
# fix's own test uses, and the script fails if the markers are missing.
#
# The BASELINE's block is located structurally (the ``except
# VerdictParseError`` arm of ``_execute_attempt``, found by AST), and
# the reason is worth stating: the marker pair was introduced BY the
# P0-1 fix, so the pre-fix source does not contain it. A marker-only
# locator cannot find the very arm this control is about. When the
# baseline does carry the markers, the marker block and the AST arm are
# cross-checked against each other, so the two locators can never
# quietly disagree.

set -uo pipefail

# ---------------------------------------------------------------------------
# Path resolution — the script lives at ``<project_root>/scripts/`` so the
# project root is the parent of its own directory. ``BASH_SOURCE`` rather
# than ``$0`` so an absolute, a relative and a ``sh``-style invocation all
# resolve the same way.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
VENV_PY="$BACKEND_DIR/.venv/bin/python3"

#: The file the control reads — the pre-fix baseline and the working-tree
#: copy are the same path, at two different revisions.
TARGET_REL="backend/verification_subagent.py"

#: The behavioural suite for the fix, relative to ``backend/`` (pytest is
#: invoked with ``cwd=backend/``, which is where ``backend/pytest.ini``
#: and the venv live).
TEST_REL="tests/unit/test_verdict_parse_retry.py"

#: The revision that holds the pre-fix source.  Set from the
#: environment when the caller pins one; empty means "the script finds
#: the baseline itself" (see "Choosing the baseline" in the header).
#: An env var that is set but empty is an error, not an invitation to
#: guess: the caller said which revision it meant and named nothing.
if [ "${P0_1_PREFIX_REV+x}" = "x" ]; then
    PREFIX_REV="$P0_1_PREFIX_REV"
    if [ -z "$PREFIX_REV" ]; then
        echo "ERROR: P0_1_PREFIX_REV is empty — it must name the revision that holds the pre-fix source" >&2
        exit 2
    fi
else
    PREFIX_REV=""
fi

#: The checked-in pre-fix fragment, used when the history holds no
#: revision with the pre-fix shape (a shallow clone, or a rewritten
#: history).  Relative to the project root, like every other path in
#: this script.
PREFIX_FIXTURE_REL="scripts/negctl_fixtures/p0_1_prefix_verdict_arm.py"

#: The marker pair the fix published — the contract the current file's
#: block is located by.
BEGIN_MARKER="# NEGCTL-P0-1-BEGIN"
END_MARKER="# NEGCTL-P0-1-END"

# ---------------------------------------------------------------------------
# Scratch space — everything this script writes lives here, and the trap
# removes it on every exit path (including the failure paths).
# ---------------------------------------------------------------------------

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/negctl_p0_1.XXXXXX")" || {
    echo "ERROR: could not create a temporary directory" >&2
    exit 2
}

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Pre-flight — exit 2 rather than 1, so a caller can tell "the control
# found a problem" apart from "the control could not run".
# ---------------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is not on PATH — the pre-fix baseline cannot be read" >&2
    exit 2
fi

if ! git -C "$PROJECT_ROOT" rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
    echo "ERROR: no HEAD commit in $PROJECT_ROOT — nothing to compare against" >&2
    exit 2
fi

CURRENT_FILE="$PROJECT_ROOT/$TARGET_REL"
if [ ! -f "$CURRENT_FILE" ]; then
    echo "ERROR: the current source is missing: $TARGET_REL" >&2
    exit 2
fi

# The behavioural suite must run on the project venv (CLAUDE.md: never run
# pytest on the system interpreter), so a missing venv is a setup error and
# not a fallback — and since the suite is mandatory, the analyser uses the
# same interpreter rather than carrying a second, unreachable code path.
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: the project venv python is missing: $VENV_PY" >&2
    echo "       create it per CLAUDE.md — the behavioural check must not run on the system interpreter" >&2
    exit 2
fi
PYTHON_CMD="$VENV_PY"

# ---------------------------------------------------------------------------
# The analyser — written into the scratch directory, not into the tree.
#
# It answers two questions about a ``verification_subagent.py`` source:
#
#   * where is the ``except VerdictParseError`` arm of
#     ``_execute_attempt``, and does it build a verdict?
#   * does the block between the negative-control markers build a
#     verdict, and does it re-raise?
#
# It prints ``key=value`` facts; the shell above reads them. The AST
# walk keeps the enclosing function attached to each handler, so the arm
# this control is about (``_execute_attempt``) is not confused with the
# two other ``except VerdictParseError`` arms in the file — the L2 retry
# branch in ``_self_heal_loop`` and the stuck-sub-agent summarizer in
# ``_summarize_vp_result``.
# ---------------------------------------------------------------------------

ANALYSER="$WORK_DIR/analyse_verification_subagent.py"

cat > "$ANALYSER" <<'PYEOF'
#!/usr/bin/env python3
"""Locate the verdict-parse-error arm in a ``verification_subagent.py``.

Prints ``key=value`` facts on stdout; the shell wrapper reads them.

Facts
-----
``markers``
    1 when the source carries the ``# NEGCTL-P0-1-BEGIN`` /
    ``# NEGCTL-P0-1-END`` marker pair.
``arm_source``
    How the arm was located: ``markers`` (the marker block), ``ast``
    (the ``except VerdictParseError`` arm of ``_execute_attempt``), or
    ``none`` (neither was available).
``arm_builds_verdict``
    1 when the located arm builds a ``Verdict`` of its own.
``arm_builds_failed_verdict``
    1 when the located arm builds a ``Verdict`` whose ``verdict`` is
    ``"FAILED"`` — the pre-fix shape.
``arm_raises``
    1 when the located arm re-raises.
``ast_arm_*``
    The same three questions answered about the AST-located arm, so the
    two locators can be cross-checked against each other.
"""

import ast
import re
import sys

BEGIN_MARKER = "# NEGCTL-P0-1-BEGIN"
END_MARKER = "# NEGCTL-P0-1-END"
TARGET_METHOD = "_execute_attempt"

#: A ``Verdict(...)`` construction.
VERDICT_CALL_RE = re.compile(r"\bVerdict\s*\(")
#: A verdict whose value is the literal ``"FAILED"``.
FAILED_VERDICT_RE = re.compile(r"""verdict\s*=\s*["']FAILED["']""")
#: A ``raise`` statement at the start of a line.
RAISE_RE = re.compile(r"^[ \t]*raise\b", re.MULTILINE)


def _is_parse_error(node):
    """True for an ``except VerdictParseError`` type annotation."""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "VerdictParseError"
    if isinstance(node, ast.Attribute):
        return node.attr == "VerdictParseError"
    return False


class _ArmFinder(ast.NodeVisitor):
    """Collect every ``except VerdictParseError`` arm with its function."""

    def __init__(self):
        self.arms = []
        self._stack = []

    def visit_FunctionDef(self, node):
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node):
        if _is_parse_error(node.type):
            owner = self._stack[-1] if self._stack else "<module>"
            self.arms.append((owner, node))
        self.generic_visit(node)


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _verdict_calls(node):
    """Every ``Verdict(...)`` call inside an AST node."""
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and _call_name(sub.func) == "Verdict"
    ]


def _is_failed_verdict(call):
    """True for ``Verdict(verdict="FAILED", ...)`` and its positional form."""
    for keyword in call.keywords:
        if (
            keyword.arg == "verdict"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "FAILED"
        ):
            return True
    if (
        call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "FAILED"
    ):
        return True
    return False


def _has_raise(node):
    return any(isinstance(sub, ast.Raise) for sub in ast.walk(node))


def _marked_block(source):
    """The text between the two markers, or None."""
    if BEGIN_MARKER not in source or END_MARKER not in source:
        return None
    return source.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0]


def _ast_arm(source):
    """The ``except VerdictParseError`` arm of ``_execute_attempt``."""
    finder = _ArmFinder()
    finder.visit(ast.parse(source))
    for owner, handler in finder.arms:
        if owner == TARGET_METHOD:
            return handler
    return None


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(
            "usage: analyse_verification_subagent.py <file>\n"
        )
        return 2

    path = argv[1]
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        sys.stderr.write("could not read %s: %s\n" % (path, exc))
        return 2

    try:
        arm = _ast_arm(source)
    except SyntaxError as exc:
        sys.stderr.write("could not parse %s: %s\n" % (path, exc))
        return 2

    facts = {}

    block = _marked_block(source)
    facts["markers"] = "1" if block is not None else "0"

    if arm is None:
        facts["ast_arm_found"] = "0"
        facts["ast_arm_builds_verdict"] = "0"
        facts["ast_arm_builds_failed_verdict"] = "0"
        facts["ast_arm_raises"] = "0"
    else:
        calls = _verdict_calls(arm)
        facts["ast_arm_found"] = "1"
        facts["ast_arm_builds_verdict"] = "1" if calls else "0"
        facts["ast_arm_builds_failed_verdict"] = (
            "1" if any(_is_failed_verdict(call) for call in calls) else "0"
        )
        facts["ast_arm_raises"] = "1" if _has_raise(arm) else "0"

    # The located arm: the marker block when the markers are there (they
    # are the contract the fix published), the AST arm otherwise (the
    # pre-fix source predates the markers).
    if block is not None:
        facts["arm_source"] = "markers"
        facts["arm_builds_verdict"] = (
            "1" if VERDICT_CALL_RE.search(block) else "0"
        )
        facts["arm_builds_failed_verdict"] = (
            "1" if FAILED_VERDICT_RE.search(block) else "0"
        )
        facts["arm_raises"] = "1" if RAISE_RE.search(block) else "0"
    elif arm is not None:
        facts["arm_source"] = "ast"
        facts["arm_builds_verdict"] = facts["ast_arm_builds_verdict"]
        facts["arm_builds_failed_verdict"] = facts[
            "ast_arm_builds_failed_verdict"
        ]
        facts["arm_raises"] = facts["ast_arm_raises"]
    else:
        facts["arm_source"] = "none"
        facts["arm_builds_verdict"] = "0"
        facts["arm_builds_failed_verdict"] = "0"
        facts["arm_raises"] = "0"

    for key in sorted(facts):
        sys.stdout.write("%s=%s\n" % (key, facts[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PYEOF

# ---------------------------------------------------------------------------
# Running the analyser. ``analyse`` stores the facts in ANALYZER_FACTS and
# returns the analyser's exit code; ``fact`` reads one of them.
# ---------------------------------------------------------------------------

ANALYZER_FACTS=""

analyse() {
    ANALYZER_FACTS="$("$PYTHON_CMD" "$ANALYSER" "$1" 2> "$WORK_DIR/analyse.err")"
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "ERROR: could not analyse $1" >&2
        sed 's/^/       /' "$WORK_DIR/analyse.err" >&2
    fi
    return "$rc"
}

fact() {
    printf '%s\n' "$ANALYZER_FACTS" | grep -E "^$1=" | head -n 1 | sed "s/^$1=//"
}

# ---------------------------------------------------------------------------
# Choosing the baseline.
#
# Three ways to get one, in the order they are tried (see the header for
# why each of them exists):
#
#   1. ``P0_1_PREFIX_REV`` — an explicit revision.
#   2. The history of the file: the most recent revision whose
#      ``except VerdictParseError`` arm in ``_execute_attempt`` still
#      builds the inline ``Verdict(verdict="FAILED")``.
#   3. The checked-in pre-fix fragment.
#
# Whatever the source, the baseline goes through the same analyser and
# the same assertions — the RED check below is what decides whether the
# baseline really carries the pre-fix shape, and it runs on all three.
# ---------------------------------------------------------------------------

#: The file the baseline was read into, and a description of where it
#: came from (used in the report and in the failure messages).
BASELINE_FILE=""
BASELINE_LABEL=""

read_baseline_from_revision() {
    local rev="$1"
    local out="$WORK_DIR/baseline_verification_subagent.py"

    if ! git -C "$PROJECT_ROOT" rev-parse --verify --quiet "${rev}^{commit}" >/dev/null 2>&1; then
        echo "ERROR: baseline revision not found: $rev" >&2
        echo "       set P0_1_PREFIX_REV to a revision that holds the pre-fix source" >&2
        exit 2
    fi

    if ! git -C "$PROJECT_ROOT" show "${rev}:${TARGET_REL}" > "$out" 2> "$WORK_DIR/show.err"; then
        echo "ERROR: could not read ${rev}:${TARGET_REL}" >&2
        sed 's/^/       /' "$WORK_DIR/show.err" >&2
        exit 2
    fi

    if [ ! -s "$out" ]; then
        echo "ERROR: the baseline ${rev}:${TARGET_REL} is empty" >&2
        exit 2
    fi

    BASELINE_FILE="$out"
    BASELINE_LABEL="revision $rev ($(git -C "$PROJECT_ROOT" rev-parse --short "${rev}^{commit}"))"
}

#: The most recent revision of the file that still builds the inline
#: FAILED verdict in the arm this control is about.
#:
#: The revisions are visited newest first, and the first one that
#: carries the pre-fix shape is the answer. "Carries the shape" is
#: decided by the analyser — by the AST, which is where the arm is —
#: and not by a text match: a text match would also fire on a verdict
#: built somewhere else in the file, which is not the arm this control
#: is about.
find_baseline_in_history() {
    local rev candidate

    while read -r rev; do
        [ -n "$rev" ] || continue
        candidate="$WORK_DIR/history_candidate.py"

        if ! git -C "$PROJECT_ROOT" show "${rev}:${TARGET_REL}" > "$candidate" 2>/dev/null; then
            continue
        fi

        # A revision without the exception at all cannot carry the
        # pre-fix shape; skipping it keeps the walk cheap without
        # deciding anything.
        if ! grep -q "VerdictParseError" "$candidate"; then
            continue
        fi

        if ! analyse "$candidate" 2>/dev/null; then
            continue
        fi

        if [ "$(fact ast_arm_found)" = "1" ] \
           && [ "$(fact ast_arm_builds_failed_verdict)" = "1" ]; then
            read_baseline_from_revision "$rev"
            BASELINE_LABEL="revision $rev ($(git -C "$PROJECT_ROOT" rev-parse --short "${rev}^{commit}")) — the most recent revision of $TARGET_REL that still builds the inline FAILED verdict"
            return 0
        fi
    done < <(git -C "$PROJECT_ROOT" log --format=%H -- "$TARGET_REL")

    return 1
}

#: The checked-in pre-fix fragment — the fallback for a repository whose
#: history no longer holds the pre-fix shape.
use_baseline_fixture() {
    local fixture="$PROJECT_ROOT/$PREFIX_FIXTURE_REL"

    if [ ! -f "$fixture" ]; then
        echo "ERROR: no revision in the history of $TARGET_REL builds the inline FAILED verdict," >&2
        echo "       and the checked-in pre-fix fragment is missing: $PREFIX_FIXTURE_REL" >&2
        echo "       the control has no baseline to check — it cannot run" >&2
        exit 2
    fi

    cp "$fixture" "$WORK_DIR/baseline_verification_subagent.py"
    BASELINE_FILE="$WORK_DIR/baseline_verification_subagent.py"
    BASELINE_LABEL="the checked-in pre-fix fragment $PREFIX_FIXTURE_REL"
}

if [ -n "$PREFIX_REV" ]; then
    read_baseline_from_revision "$PREFIX_REV"
elif ! find_baseline_in_history; then
    use_baseline_fixture
fi

# ---------------------------------------------------------------------------
# The control.
# ---------------------------------------------------------------------------

echo "=== negctl P0-1 — negative control for the verdict-parse retry fix"
echo "=== baseline: $BASELINE_LABEL"
echo

# --- RED: the pre-fix source really did swallow the failure ---------------

if ! analyse "$BASELINE_FILE"; then
    exit 2
fi

BASE_SOURCE="$(fact arm_source)"
BASE_FAILED="$(fact arm_builds_failed_verdict)"
BASE_AST_FOUND="$(fact ast_arm_found)"
BASE_AST_FAILED="$(fact ast_arm_builds_failed_verdict)"

if [ "$BASE_AST_FOUND" != "1" ]; then
    echo "FAIL: no 'except VerdictParseError' arm found in _execute_attempt of the baseline ($BASELINE_LABEL)" >&2
    echo "      the pre-fix behaviour this control is meant to prove cannot be located" >&2
    exit 1
fi

if [ "$BASE_SOURCE" = "markers" ] && [ "$BASE_FAILED" != "$BASE_AST_FAILED" ]; then
    echo "FAIL: the marker block and the AST arm of the baseline disagree" >&2
    echo "      marker block says failed-verdict=$BASE_FAILED, AST arm says $BASE_AST_FAILED" >&2
    echo "      the two locators cannot both be right — the control would be guessing" >&2
    exit 1
fi

if [ "$BASE_FAILED" != "1" ]; then
    echo "FAIL: the baseline ($BASELINE_LABEL) does NOT build an inline Verdict(verdict='FAILED') in its VerdictParseError arm" >&2
    echo "      the pre-fix bug this control is meant to prove is not there — the control would be vacuous" >&2
    echo "      pick a baseline revision that predates the P0-1 fix" >&2
    exit 1
fi

echo "RED: pre-fix verification_subagent.py contains inline Verdict(verdict='FAILED') in VerdictParseError handler"

# --- GREEN 1: the current source has no inline FAILED verdict -------------

if ! analyse "$CURRENT_FILE"; then
    exit 2
fi

CUR_MARKERS="$(fact markers)"
CUR_BLOCK_VERDICT="$(fact arm_builds_verdict)"
CUR_AST_FOUND="$(fact ast_arm_found)"
CUR_AST_VERDICT="$(fact ast_arm_builds_verdict)"

if [ "$CUR_MARKERS" != "1" ]; then
    echo "FAIL: the negative-control markers ($BEGIN_MARKER / $END_MARKER) are missing from $TARGET_REL" >&2
    echo "      the markers are the contract this control locates the block by" >&2
    exit 1
fi

if [ "$CUR_AST_FOUND" != "1" ]; then
    echo "FAIL: no 'except VerdictParseError' arm found in _execute_attempt of $TARGET_REL" >&2
    exit 1
fi

if [ "$CUR_BLOCK_VERDICT" = "1" ] || [ "$CUR_AST_VERDICT" = "1" ]; then
    echo "FAIL: $TARGET_REL still builds a verdict of its own in the VerdictParseError arm" >&2
    echo "      the inline verdict is what made the L2 retry unreachable" >&2
    exit 1
fi

echo "GREEN: current verification_subagent.py has no inline FAILED verdict"

# --- GREEN 2: the current source re-raises, so the L2 retry is reachable --

CUR_BLOCK_RAISES="$(fact arm_raises)"
CUR_AST_RAISES="$(fact ast_arm_raises)"

if [ "$CUR_BLOCK_RAISES" != "1" ] || [ "$CUR_AST_RAISES" != "1" ]; then
    echo "FAIL: $TARGET_REL does not re-raise the parse error out of the VerdictParseError arm" >&2
    echo "      the L2 retry branch stays unreachable without the re-raise" >&2
    exit 1
fi

echo "GREEN: current verification_subagent.py re-raises so the L2 retry is reachable"

# --- GREEN 3: the behavioural suite for the fix passes --------------------

PYTEST_OUT="$WORK_DIR/pytest.log"
(
    cd "$BACKEND_DIR" || exit 2
    # The outer pytest session's environment must not leak into this one:
    # PYTEST_ADDOPTS would inject options, and COVERAGE_FILE would point
    # two pytest runs at one coverage file.
    unset PYTEST_ADDOPTS PYTEST_CURRENT_TEST PYTEST_PLUGINS COVERAGE_FILE
    "$VENV_PY" -m pytest "$TEST_REL" -p no:cacheprovider
) > "$PYTEST_OUT" 2>&1
PYTEST_RC=$?

if [ "$PYTEST_RC" -ne 0 ]; then
    echo "FAIL: $TEST_REL failed (pytest exit $PYTEST_RC)" >&2
    tail -n 40 "$PYTEST_OUT" >&2
    exit 1
fi

PYTEST_SUMMARY="$(grep -E '[0-9]+ (passed|failed|error)' "$PYTEST_OUT" | tail -n 1)"
echo "=== pytest $TEST_REL -> ${PYTEST_SUMMARY:-no summary line} (exit $PYTEST_RC)"
echo "GREEN: tests/unit/test_verdict_parse_retry.py passed"

echo
echo "PASS: negctl-p0-1"
exit 0
