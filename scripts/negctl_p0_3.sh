#!/usr/bin/env bash
# scripts/negctl_p0_3.sh — the negative control for P0-3.
#
# The bug this control is about
# -----------------------------
# P0-3 (2026-10-22) changed the same-failure loop control in
# ``backend/verification/orchestrator.py``. Before the fix the loop
# ended the chain the moment the same failure repeated:
# ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` was ``1``, and the
# same-failure branch returned ``repair_tasks=[]`` — a terminal verdict
# with no repair path and no reason a human could act on. The loop
# could not tell a broken criterion (a human must fix the check) from
# a wrong deliverable (the work must be redone), so it took the former
# for the latter and killed the chain.
#
# The fix raised the threshold to ``2`` and turned the cap into a
# semantic triage: reaching it buys one ``VPTriageJudge`` call, and
# the triage's verdict decides between stopping and diverting into the
# repair path.
#
# Why a control is needed at all
# ------------------------------
# "The fix works" is only half of the claim, and it is the easy half:
# the behavioural suite (``backend/tests/integration/
# test_same_failure_triage.py``) pins the four contracts that decide
# whether the triage may act at all.
#
# The other half is "the bug was there". Without it the fix could be a
# no-op and the behavioural suite would still be green — a suite that
# passes on both the broken and the fixed code measures nothing about
# the fix. That half is what a negative control is for, and this script
# is it:
#
#   RED   — the PRE-FIX source (read out of git, never out of the
#           working tree) really did carry the threshold ``1``;
#   RED   — …and really did terminate at the cap with an empty
#           ``repair_tasks`` and without dispatching any triage;
#   RED   — …and the REAL judge's malfunctions (the provider blew up,
#           the answer was not a JSON object, the quote did not
#           verify) stopped the loop in the pre-fix conclusion shape;
#   GREEN — the current source carries the threshold ``2`` and does
#           dispatch a triage;
#   GREEN — …and the same three malfunctions leave the loop running,
#           while a triage that ran and could not characterise the
#           failure still stops, with the structured reason;
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
#   1. ``P0_3_PREFIX_REV`` — an explicit revision, used as given. This
#      is how CI pins the control to one commit (see ``.github/workflows/
#      ci.yml``), and it is the only way to point the control at a
#      baseline that is not in this repository's history.
#
#   2. The history of ``backend/verification/orchestrator.py``: the most
#      recent revision whose same-failure branch still stops with an
#      empty ``repair_tasks`` and does not dispatch a triage. That
#      revision is the pre-fix source by definition — it is the last
#      state of the file in which the shape the fix removed was still
#      there.
#
#      A moving ref cannot do this job. ``origin/main`` stops holding
#      the pre-fix source the moment the fix is merged, and a clone
#      that fetched one branch does not have it at all, so a control
#      pinned to it would fail for a reason that has nothing to do with
#      the claim it is making.
#
#   3. The checked-in pre-fix fragment
#      (``scripts/negctl_fixtures/p0_3_prefix_orchestrator.py``), for a
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
#   bash scripts/negctl_p0_3.sh
#   P0_3_PREFIX_REV=<rev> bash scripts/negctl_p0_3.sh
#
# ``P0_3_PREFIX_REV`` names the revision that holds the PRE-FIX source.
# When it is set, that revision is the baseline; when it is not set, the
# script finds one (see "Choosing the baseline" above). When the
# baseline cannot be resolved (no git, unknown ref, the file missing at
# that ref) the script exits non-zero and says why. It never skips a
# check silently: a skipped control reads exactly like a passed one.
#
# Exit contract
# -------------
#   * exit 0 — the control is established (the RED and GREEN halves
#              above, then PASS)
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
# No nested pytest
# ----------------
# The behavioural suite for the fix is normally run by this script — but
# only when this script is not itself running inside a pytest session.
# A pytest session started inside a pytest session is exactly what a
# control must not do: the inner run inherits the outer session's
# plugins, addopts and coverage file, and the two runs report into
# one another's state. So when the script detects that it is nested
# (``PYTEST_CURRENT_TEST`` / ``PYTEST_VERSION`` are set), it leaves the
# behavioural suite to the caller and says so, instead of quietly
# skipping it.

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
TARGET_REL="backend/verification/orchestrator.py"

#: The behavioural suite for the fix, relative to ``backend/`` (pytest is
#: invoked with ``cwd=backend/``, which is where ``backend/pytest.ini``
#: and the venv live).
TEST_REL="tests/integration/test_same_failure_triage.py"

#: The revision that holds the pre-fix source.  Set from the
#: environment when the caller pins one; empty means "the script finds
#: the baseline itself" (see "Choosing the baseline" in the header).
#: An env var that is set but empty is an error, not an invitation to
#: guess: the caller said which revision it meant and named nothing.
if [ "${P0_3_PREFIX_REV+x}" = "x" ]; then
    PREFIX_REV="$P0_3_PREFIX_REV"
    if [ -z "$PREFIX_REV" ]; then
        echo "ERROR: P0_3_PREFIX_REV is empty — it must name the revision that holds the pre-fix source" >&2
        exit 2
    fi
else
    PREFIX_REV=""
fi

#: The checked-in pre-fix fragment, used when the history holds no
#: revision with the pre-fix shape (a shallow clone, or a rewritten
#: history).  Relative to the project root, like every other path in
#: this script.
PREFIX_FIXTURE_REL="scripts/negctl_fixtures/p0_3_prefix_orchestrator.py"

# ---------------------------------------------------------------------------
# Scratch space — everything this script writes lives here, and the trap
# removes it on every exit path (including the failure paths).
# ---------------------------------------------------------------------------

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/negctl_p0_3.XXXXXX")" || {
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

# The baseline is resolved after the analyser has been written — the
# history walk asks the analyser which revision still carries the
# pre-fix shape, so the two cannot disagree. See "Choosing the
# baseline" in the header.

CURRENT_FILE="$PROJECT_ROOT/$TARGET_REL"
if [ ! -f "$CURRENT_FILE" ]; then
    echo "ERROR: the current source is missing: $TARGET_REL" >&2
    exit 2
fi

# The analyser runs on the project venv (CLAUDE.md: never run project code
# on the system interpreter), so a missing venv is a setup error and not a
# fallback.
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: the project venv python is missing: $VENV_PY" >&2
    echo "       create it per CLAUDE.md — the analyser must not run on the system interpreter" >&2
    exit 2
fi
PYTHON_CMD="$VENV_PY"

# The behavioural suite the caller runs has to exist — without it there is
# no "the fix works" half to pair with this control's "the bug was there".
if [ ! -f "$BACKEND_DIR/$TEST_REL" ]; then
    echo "ERROR: the behavioural suite for the fix is missing: backend/$TEST_REL" >&2
    echo "       the control pairs the pre-fix shape with the suite that pins the fixed one" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# The analyser — written into the scratch directory, not into the tree.
#
# It answers three questions about an ``orchestrator.py`` source:
#
#   * what is ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` set to;
#   * does a ``return`` hand back a payload whose ``repair_tasks`` is an
#     empty list (the pre-fix stop shape);
#   * does the source call ``self.classify_cycle(...)`` (the triage the
#     fix put between the cap and the stop).
#
# It prints ``key=value`` facts; the shell above reads them. The facts
# are computed on the AST, not on the text, so a comment or a docstring
# that mentions the threshold cannot make a check pass.
# ---------------------------------------------------------------------------

ANALYSER="$WORK_DIR/analyse_orchestrator.py"

cat > "$ANALYSER" <<'PYEOF'
#!/usr/bin/env python3
"""Locate the same-failure stop branch in an ``orchestrator.py``.

Prints ``key=value`` facts on stdout; the shell wrapper reads them.

Facts
-----
``threshold``
    The integer assigned to ``self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS``
    — ``1`` before the fix, ``2`` after it — or ``none`` when the
    assignment is missing.
``empty_repair_tasks_return``
    1 when a ``return`` hands back a payload whose ``repair_tasks`` is
    an empty list — the pre-fix stop shape, where the chain was
    terminated with no repair path.
``triage_dispatch``
    1 when the source calls ``self.classify_cycle(...)`` — the triage
    the fix put between the cap and the stop.
``stop_guarded_by_triage``
    1 when the stop waits for a triage verdict — the shape the fix
    has, where the cap buys one triage call and the chain stops only
    if the triage said stop. A triage that is called and ignored does
    not count, and neither does one the stop never reads.
"""

import ast
import sys

#: The attribute the threshold is stored under.
THRESHOLD_ATTR = "_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS"

#: The method the fix dispatches the triage through.
TRIAGE_METHOD = "classify_cycle"


def _is_empty_repair_tasks_dict(node):
    """True for a dict literal whose ``repair_tasks`` is an empty list."""
    if not isinstance(node, ast.Dict):
        return False
    for key, value in zip(node.keys, node.values):
        if key is None:
            continue
        if isinstance(key, ast.Constant) and key.value == "repair_tasks":
            return isinstance(value, ast.List) and not value.elts
    return False


def _empty_repair_tasks_names(tree):
    """Names bound to a payload that carries an empty ``repair_tasks``."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not _is_empty_repair_tasks_dict(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _stop_returns(tree):
    """Every ``return`` that hands back an empty ``repair_tasks``.

    Both spellings count: the payload built inline in the ``return``
    (the pre-fix source) and the payload built into a variable and
    returned by name (the fixed source, where the return happens only
    after the triage said stop).
    """
    names = _empty_repair_tasks_names(tree)
    stops = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if _is_empty_repair_tasks_dict(value):
            stops.append(node)
        elif isinstance(value, ast.Name) and value.id in names:
            stops.append(node)
    return stops


def _threshold(tree):
    """The value assigned to the threshold, as a string."""
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (
                isinstance(target, ast.Attribute)
                and target.attr == THRESHOLD_ATTR
            ):
                continue
            literal = node.value
            if (
                isinstance(literal, ast.Constant)
                and isinstance(literal.value, int)
                and not isinstance(literal.value, bool)
            ):
                values.append(literal.value)
            else:
                values.append("non-literal")
    distinct = sorted(set(values), key=str)
    if not distinct:
        return "none"
    if len(distinct) > 1:
        return "conflict:" + ",".join(str(value) for value in distinct)
    return str(distinct[0])


def _triage_dispatch(tree):
    """True when the source calls ``self.classify_cycle(...)``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == TRIAGE_METHOD:
            return True
    return False


def _triage_result_names(tree):
    """The names a triage verdict is bound to.

    A verdict that is called and thrown away is not a verdict: only a
    binding can be consulted later, so only a binding counts as the
    triage having spoken.
    """
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute) and func.attr == TRIAGE_METHOD
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _mentions(node, names):
    """True when the subtree under ``node`` names one of ``names``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
    return False


def _parent_map(tree):
    """Every node's parent."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _stop_guarded_by_triage(tree, stops):
    """True when the stop waits for a triage verdict.

    The stop has to sit inside a branch whose condition reads the
    triage's verdict — the shape the fix has: the cap buys one triage
    call, and the chain stops only if the triage said stop. A triage
    that is called and ignored does not guard anything, and neither
    does one that is called somewhere the stop never reads.
    """
    names = _triage_result_names(tree)
    if not names:
        return False
    parents = _parent_map(tree)
    for stop in stops:
        node = parents.get(stop)
        while node is not None:
            if isinstance(node, ast.If) and _mentions(node.test, names):
                return True
            node = parents.get(node)
    return False


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: analyse_orchestrator.py <file>\n")
        return 2

    path = argv[1]
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        sys.stderr.write("could not read %s: %s\n" % (path, exc))
        return 2

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        sys.stderr.write("could not parse %s: %s\n" % (path, exc))
        return 2

    stops = _stop_returns(tree)
    facts = {
        "threshold": _threshold(tree),
        "empty_repair_tasks_return": "1" if stops else "0",
        "triage_dispatch": "1" if _triage_dispatch(tree) else "0",
        "stop_guarded_by_triage": (
            "1" if _stop_guarded_by_triage(tree, stops) else "0"
        ),
    }

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
#   1. ``P0_3_PREFIX_REV`` — an explicit revision.
#   2. The history of the file: the most recent revision whose
#      same-failure branch still stops with an empty ``repair_tasks``
#      and does not dispatch a triage.
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
    local out="$WORK_DIR/baseline_orchestrator.py"

    if ! git -C "$PROJECT_ROOT" rev-parse --verify --quiet "${rev}^{commit}" >/dev/null 2>&1; then
        echo "ERROR: baseline revision not found: $rev" >&2
        echo "       set P0_3_PREFIX_REV to a revision that holds the pre-fix source" >&2
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

#: The most recent revision of the file that still stops with an empty
#: ``repair_tasks`` and does not dispatch a triage.
#:
#: The revisions are visited newest first, and the first one that
#: carries the pre-fix shape is the answer. "Carries the shape" is
#: decided by the analyser — by the AST, which is where the branch is
#: — and not by a text match: a text match would also fire on a
#: threshold mentioned in a comment, which is not the branch this
#: control is about.
find_baseline_in_history() {
    local rev candidate

    while read -r rev; do
        [ -n "$rev" ] || continue
        candidate="$WORK_DIR/history_candidate.py"

        if ! git -C "$PROJECT_ROOT" show "${rev}:${TARGET_REL}" > "$candidate" 2>/dev/null; then
            continue
        fi

        # A revision without the threshold at all cannot carry the
        # pre-fix shape; skipping it keeps the walk cheap without
        # deciding anything.
        if ! grep -q "_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS" "$candidate"; then
            continue
        fi

        if ! analyse "$candidate" 2>/dev/null; then
            continue
        fi

        if [ "$(fact threshold)" = "1" ] \
           && [ "$(fact triage_dispatch)" = "0" ] \
           && [ "$(fact empty_repair_tasks_return)" = "1" ]; then
            read_baseline_from_revision "$rev"
            BASELINE_LABEL="revision $rev ($(git -C "$PROJECT_ROOT" rev-parse --short "${rev}^{commit}")) — the most recent revision of $TARGET_REL that still stops with an empty repair_tasks"
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
        echo "ERROR: no revision in the history of $TARGET_REL stops with an empty repair_tasks," >&2
        echo "       and the checked-in pre-fix fragment is missing: $PREFIX_FIXTURE_REL" >&2
        echo "       the control has no baseline to check — it cannot run" >&2
        exit 2
    fi

    cp "$fixture" "$WORK_DIR/baseline_orchestrator.py"
    BASELINE_FILE="$WORK_DIR/baseline_orchestrator.py"
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

echo "=== negctl P0-3 — negative control for the same-failure triage fix"
echo "=== baseline: $BASELINE_LABEL"
echo

# --- RED: the pre-fix source really did terminate at the first repeat ----

if ! analyse "$BASELINE_FILE"; then
    exit 2
fi

BASE_THRESHOLD="$(fact threshold)"
BASE_EMPTY_REPAIR="$(fact empty_repair_tasks_return)"
BASE_TRIAGE="$(fact triage_dispatch)"
BASE_GUARDED="$(fact stop_guarded_by_triage)"

if [ "$BASE_THRESHOLD" != "1" ]; then
    echo "FAIL: the baseline ($BASELINE_LABEL) does NOT set _MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 1 (it is '$BASE_THRESHOLD')" >&2
    echo "      the pre-fix behaviour this control is meant to prove is not there — the control would be vacuous" >&2
    echo "      pick a baseline revision that predates the P0-3 fix" >&2
    exit 1
fi

echo "RED: pre-fix orchestrator.py has _MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 1"

if [ "$BASE_EMPTY_REPAIR" != "1" ]; then
    echo "FAIL: the baseline ($BASELINE_LABEL) does NOT stop with an empty repair_tasks" >&2
    echo "      the pre-fix stop this control is meant to prove is not there — the control would be vacuous" >&2
    exit 1
fi

if [ "$BASE_TRIAGE" != "0" ]; then
    echo "FAIL: the baseline ($BASELINE_LABEL) already dispatches a triage" >&2
    echo "      the pre-fix source stopped without asking anyone — a baseline that triages is not pre-fix" >&2
    exit 1
fi

if [ "$BASE_GUARDED" != "0" ]; then
    echo "FAIL: the baseline ($BASELINE_LABEL) stops only after a triage verdict" >&2
    echo "      the pre-fix source stopped without asking anyone — a baseline that waits for a verdict is not pre-fix" >&2
    exit 1
fi

echo "RED: pre-fix same-failure branch returns repair_tasks=[]"

# --- GREEN: the current source has the threshold and the triage ----------

if ! analyse "$CURRENT_FILE"; then
    exit 2
fi

CUR_THRESHOLD="$(fact threshold)"
CUR_TRIAGE="$(fact triage_dispatch)"
CUR_GUARDED="$(fact stop_guarded_by_triage)"

if [ "$CUR_THRESHOLD" != "2" ]; then
    echo "FAIL: $TARGET_REL does not set _MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 2 (it is '$CUR_THRESHOLD')" >&2
    echo "      the threshold is what buys the triage a second round to look at" >&2
    exit 1
fi

if [ "$CUR_TRIAGE" != "1" ]; then
    echo "FAIL: $TARGET_REL does not dispatch a triage" >&2
    echo "      without the triage the cap terminates the chain on its own — the fix regressed" >&2
    exit 1
fi

if [ "$CUR_GUARDED" != "1" ]; then
    echo "FAIL: the stop in $TARGET_REL is not guarded by the triage" >&2
    echo "      a triage that does not gate the stop leaves the pre-fix behaviour in place — the fix regressed" >&2
    exit 1
fi

echo "GREEN: current orchestrator.py has threshold 2 and triage dispatch"

# ---------------------------------------------------------------------------
# The real judge — the malfunctions the shipped judge actually has
# ---------------------------------------------------------------------------
#
# Everything above is about the orchestrator's *shape*. The other half
# of the claim is about the judge that ships: the behavioural suite pins
# the contract with judge doubles, and the double that raises exercises
# the orchestrator's own error path — a path the shipped judge never
# takes. The shipped ``VPTriageJudge`` never raises: it answers with a
# conclusion, whatever went wrong.
#
# Its three malfunctions — the provider blew up, the answer was not a
# JSON object, the quote did not verify against the recorded failure —
# used to be swallowed into an ordinary inconclusive conclusion, which
# reads exactly like "the triage ran and could not characterise the
# failure". The loop stopped on that, voiding a criterion over a triage
# that classified nothing.
#
# This half drives the REAL judge through the REAL orchestrator and pins
# both sides of the distinction the fix draws:
#
#   RED (real judge)   — the pre-fix conclusion shape (the judge's own
#                        fields, without the availability marker the fix
#                        added) stopped the loop: the bug was there.
#   GREEN (real judge) — the current judge marks those malfunctions
#                        unavailable and the loop keeps running, while a
#                        triage that RAN and could not characterise the
#                        failure still stops, with the structured reason.
#
# The probe is written into the scratch directory, like the analyser.

REAL_JUDGE_PROBE="$WORK_DIR/real_judge_control.py"

cat > "$REAL_JUDGE_PROBE" <<'PYEOF'
#!/usr/bin/env python3
"""The real-judge half of the negative control.

The orchestrator's own error path is not the shipped judge's path. The
shipped ``VPTriageJudge`` never raises: it answers with a conclusion,
whatever went wrong. Its three malfunctions — the provider blew up, the
answer was not a JSON object, the quote did not verify against the
recorded failure — used to be swallowed into an ordinary inconclusive
conclusion, which reads exactly like "the triage ran and could not
characterise the failure". The loop stopped on that, voiding a
criterion over a triage that classified nothing.

This probe drives the REAL judge through the REAL orchestrator and pins
both sides of the distinction the fix draws:

  * RED   — the pre-fix conclusion shape (the judge's own fields,
            without the availability marker the fix added) stopped the
            loop: the bug was there;
  * GREEN — the current judge marks those malfunctions unavailable and
            the loop keeps running, while a triage that RAN and could
            not characterise the failure still stops, with the
            structured reason a human needs.

Usage: real_judge_control.py <project_root> <scratch_dir>
"""

from __future__ import annotations

import dataclasses
import sys
import types
from pathlib import Path

#: The verification point that is stuck.
VP_ID = "VP-018"

#: The failure class the report records.
FAILURE_CLASS = "deliverable_defect"

#: The recorded failure — quotable text, and the only text the judge
#: may quote from.
ACTUAL_RESULT = "collected 0 items, exit code 5"
EVIDENCE = "pytest --collect-only produced no tests"

#: The round report: one failed point.
REPORT = {
    "verification_results": [
        {
            "id": VP_ID,
            "status": "FAILED",
            "verdict": "FAILED",
            "actual_result": ACTUAL_RESULT,
            "evidence": EVIDENCE,
        },
    ],
}


class ProviderDies:
    """The provider blows up."""

    def query_json(self, **kwargs):
        raise RuntimeError("provider exploded")


class NotJson:
    """The provider answers with something that is not a JSON object."""

    def query_json(self, **kwargs):
        return "this is not a json object at all"


class BadQuote:
    """The provider quotes text that is nowhere in the record."""

    def query_json(self, **kwargs):
        return {
            "failure_class": "criterion_defect",
            "evidence_quote": "a quote that appears nowhere in the record",
            "rationale": "the criterion is broken",
        }


class OutsideVocabulary:
    """The provider answers with a class outside the vocabulary.

    This one is not a malfunction: the judge ran, read the answer and
    found a class it does not know. It is the case the loop is allowed
    to stop on.
    """

    def query_json(self, **kwargs):
        return {
            "failure_class": "environment_bug",
            "evidence_quote": ACTUAL_RESULT,
            "rationale": "the machine was busy",
        }


#: The judge's own malfunctions — the paths it swallows rather than raises.
MALFUNCTIONS = (
    ("the provider blew up", ProviderDies),
    ("the answer was not a JSON object", NotJson),
    ("the quote did not verify", BadQuote),
)


class FixedJudge:
    """A judge that answers with one fixed conclusion."""

    def __init__(self, conclusion):
        self._conclusion = conclusion

    def judge(self, vp, failure_history, round_number=0):
        return self._conclusion


def build_orchestrator(tool, judge, plan_dir):
    """The real orchestrator, wired to the judge under test."""
    from verification.orchestrator import VerificationOrchestrator

    orchestrator = VerificationOrchestrator.__new__(VerificationOrchestrator)
    orchestrator.plan_dir = plan_dir
    orchestrator.project_dir = plan_dir
    orchestrator.coding_tool = tool
    orchestrator._failure_history = {}
    orchestrator._triage_judge = judge
    orchestrator.logger = None
    return orchestrator


def classify(orchestrator):
    """The orchestrator's verdict on the stuck failure."""
    return orchestrator.classify_cycle(
        (VP_ID, FAILURE_CLASS), REPORT, round_number=3,
    )


def pre_fix_shape(conclusion):
    """The conclusion as the judge used to build it.

    Before the fix the conclusion carried no availability marker: a
    malfunctioning triage produced an ordinary inconclusive conclusion,
    and the orchestrator read "no marker" as "the triage spoke". The
    shape is rebuilt from the real judge's own answer, so the content is
    the judge's — only the marker is missing.
    """
    return types.SimpleNamespace(
        **{
            field.name: getattr(conclusion, field.name)
            for field in dataclasses.fields(conclusion)
            if field.name != "available"
        }
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(
            "usage: real_judge_control.py <project_root> <scratch_dir>\n"
        )
        return 2

    # The project root (``backend.framework...``) and the backend package
    # (``verification...``) are both imported by the code under test.
    project_root = Path(argv[1]).resolve()
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(project_root / "backend"))
    plan_dir = Path(argv[2]) / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    from verification.orchestrator import (
        REASON_TRIAGE_UNAVAILABLE,
        STOP_REASON_NO_TRIAGE_CONCLUSION,
    )
    from verification.triage_agent import VPTriageJudge

    def judge_answer(provider_cls):
        """What the real judge answers for one provider."""
        judge = VPTriageJudge(coding_tool=provider_cls())
        return judge.judge(
            vp={
                "id": VP_ID,
                "actual_result": ACTUAL_RESULT,
                "evidence": EVIDENCE,
            },
            failure_history=[],
            round_number=3,
        )

    # --- RED: the pre-fix shape stopped the loop ------------------------
    for label, provider_cls in MALFUNCTIONS:
        legacy = pre_fix_shape(judge_answer(provider_cls))
        if getattr(legacy, "conclusive", False):
            raise AssertionError(
                f"RED (real judge): {label} — the pre-fix shape is not an "
                f"inconclusive conclusion: {legacy!r}"
            )
        decision = classify(
            build_orchestrator(provider_cls(), FixedJudge(legacy), plan_dir)
        )
        if not decision["should_stop"]:
            raise AssertionError(
                f"RED (real judge): {label} — the pre-fix conclusion shape "
                f"did not stop the loop: {decision!r}"
            )
        reason = (decision.get("stop_reason") or {}).get("reason")
        if reason != STOP_REASON_NO_TRIAGE_CONCLUSION:
            raise AssertionError(
                f"RED (real judge): {label} — the pre-fix shape stopped with "
                f"{reason!r}, expected {STOP_REASON_NO_TRIAGE_CONCLUSION!r}"
            )

    print(
        "RED (real judge): a malfunctioning triage stopped the loop in the "
        "pre-fix conclusion shape (provider blew up, answer not JSON, quote "
        "not verifiable)"
    )

    # --- GREEN: the current judge keeps the loop running ----------------
    for label, provider_cls in MALFUNCTIONS:
        conclusion = judge_answer(provider_cls)
        if getattr(conclusion, "available", None) is not False:
            raise AssertionError(
                f"GREEN (real judge): {label} — the judge did not mark the "
                f"malfunction unavailable: {conclusion!r}"
            )
        if conclusion.conclusive is not False:
            raise AssertionError(
                f"GREEN (real judge): {label} — the judge answered "
                f"conclusively about a malfunction: {conclusion!r}"
            )
        decision = classify(
            build_orchestrator(
                provider_cls(), VPTriageJudge(coding_tool=provider_cls()), plan_dir
            )
        )
        if decision["should_stop"] or decision["stop_reason"] is not None:
            raise AssertionError(
                f"GREEN (real judge): {label} — the loop stopped on a "
                f"malfunctioning triage: {decision!r}"
            )
        if decision["void_counter"] is not False:
            raise AssertionError(
                f"GREEN (real judge): {label} — the same-cause counter was "
                f"voided: {decision!r}"
            )
        if decision["reason"] != REASON_TRIAGE_UNAVAILABLE:
            raise AssertionError(
                f"GREEN (real judge): {label} — the verdict is "
                f"{decision['reason']!r}, expected {REASON_TRIAGE_UNAVAILABLE!r}"
            )

    print(
        "GREEN (real judge): the same three malfunctions leave the loop "
        "running (should_stop=False, stop_reason=None, counter untouched)"
    )

    # --- GREEN: a triage that ran and could not characterise still stops
    decision = classify(
        build_orchestrator(
            OutsideVocabulary(),
            VPTriageJudge(coding_tool=OutsideVocabulary()),
            plan_dir,
        )
    )
    if not decision["should_stop"]:
        raise AssertionError(
            f"GREEN (real judge): a triage that ran and could not "
            f"characterise the failure did not stop the loop: {decision!r}"
        )
    stop_reason = decision.get("stop_reason") or {}
    if stop_reason.get("reason") != STOP_REASON_NO_TRIAGE_CONCLUSION:
        raise AssertionError(
            f"GREEN (real judge): the structured stop reason is "
            f"{stop_reason!r}, expected "
            f"{STOP_REASON_NO_TRIAGE_CONCLUSION!r}"
        )
    if (
        stop_reason.get("vp_id") != VP_ID
        or stop_reason.get("failure_class") != FAILURE_CLASS
    ):
        raise AssertionError(
            f"GREEN (real judge): the structured stop reason does not name "
            f"the point and the failure class: {stop_reason!r}"
        )

    print(
        "GREEN (real judge): a triage that ran and could not characterise "
        "the failure still stops, with the structured reason a human needs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PYEOF

echo "--- the real judge: the malfunctions the shipped judge has"

if ! "$PYTHON_CMD" "$REAL_JUDGE_PROBE" "$PROJECT_ROOT" "$WORK_DIR/real-judge" \
    > "$WORK_DIR/real_judge.out" 2> "$WORK_DIR/real_judge.err"; then
    echo "FAIL: the real-judge half of the control did not hold" >&2
    sed 's/^/       /' "$WORK_DIR/real_judge.err" >&2
    exit 1
fi

if ! grep -q '^RED (real judge):' "$WORK_DIR/real_judge.out" \
    || ! grep -q '^GREEN (real judge):' "$WORK_DIR/real_judge.out"; then
    echo "FAIL: the real-judge half of the control did not report both sides" >&2
    sed 's/^/       /' "$WORK_DIR/real_judge.out" >&2
    exit 1
fi

grep -E '^(RED|GREEN) \(real judge\):' "$WORK_DIR/real_judge.out"

# --- GREEN: the behavioural suite for the fix passes ----------------------
#
# The suite is run here only when this script is not itself inside a
# pytest session; a nested pytest run is what this control must not do.
# See the header.

NESTED_IN_PYTEST=0
if [ -n "${PYTEST_CURRENT_TEST:-}" ] || [ -n "${PYTEST_VERSION:-}" ]; then
    NESTED_IN_PYTEST=1
fi

if [ "$NESTED_IN_PYTEST" = "1" ]; then
    echo "=== behavioural suite $TEST_REL is run by the caller — this script is inside a pytest session and starts no nested one"
else
    PYTEST_OUT="$WORK_DIR/pytest.log"
    (
        cd "$BACKEND_DIR" || exit 2
        # The outer pytest session's environment must not leak into this
        # one: PYTEST_ADDOPTS would inject options, and COVERAGE_FILE
        # would point two pytest runs at one coverage file.
        unset PYTEST_ADDOPTS PYTEST_CURRENT_TEST PYTEST_PLUGINS PYTEST_VERSION COVERAGE_FILE
        "$VENV_PY" -m pytest "$TEST_REL" -p no:cacheprovider
    ) > "$PYTEST_OUT" 2>&1
    PYTEST_RC=$?

    if [ "$PYTEST_RC" -ne 0 ]; then
        echo "FAIL: $TEST_REL failed (pytest exit $PYTEST_RC)" >&2
        tail -n 40 "$PYTEST_OUT" >&2
        exit 1
    fi

    PYTEST_SUMMARY="$(grep -Eo '[0-9]+ (passed|failed|errors?)' "$PYTEST_OUT" | tail -n 1)"
    echo "=== pytest $TEST_REL -> ${PYTEST_SUMMARY:-no summary line} (exit $PYTEST_RC)"
    echo "GREEN: $TEST_REL passed"
fi

echo
echo "PASS: negctl-p0-3"
exit 0
