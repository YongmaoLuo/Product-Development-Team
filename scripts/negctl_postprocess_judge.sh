#!/usr/bin/env bash
# scripts/negctl_postprocess_judge.sh — the negative control for the judge.
#
# What this control is about
# --------------------------
# The judge (``backend/postprocess/judge.py``) is the only component in the
# finishing stage that says PASS or FAIL, and its truth table is pinned by
# ``backend/tests/unit/postprocess/test_judge.py``.  That suite being green
# is only half of the claim.  The other half is "the suite would have caught
# a judge that decides wrongly": a suite that passes just as happily
# against a judge that answers PASS to everything measures nothing about
# the judge, and the whole point of the split between the executor and the
# judge is that the judge cannot be trusted to be right.
#
# So this control runs the truth-table suite TWICE against the same
# baseline and lets the difference between the two runs be the answer:
#
#   RED   — the judge whose "evidence that cannot decide" branch answers
#           PASS instead of FAIL makes the suite fail (the evidence
#           sufficiency assertions are load-bearing);
#   GREEN — the very same suite, run against the unmodified judge, passes.
#
# A control that cannot fail is not a control.  If the mutated judge does
# not break the suite, this script exits non-zero and says so instead of
# reporting a vacuous PASS — and the same holds for a baseline it cannot
# read at all, or a mutation it cannot apply.
#
# The mutation
# ------------
# One branch, chosen because it is the judge's core semantic: the case
# where the evidence cannot decide anything (no record carries both an
# exit code and command output) must decide FAIL.  The mutation flips
# that verdict to PASS.  It is applied to a COPY, never to the working
# tree.
#
# The baseline
# ------------
# The judge's source is read out of git (``git show HEAD:...``), never out
# of the working tree: the control measures the committed code, and a
# source that cannot be read at HEAD is a baseline the control cannot be
# built on — in that case the script exits non-zero and says why.
#
# Usage
# -----
#   bash scripts/negctl_postprocess_judge.sh
#
# Exit contract
# -------------
#   * exit 0 — the control is established (RED with at least three
#               failures, GREEN with none)
#   * exit 1 — a check fired (the mutation did not break the suite, the
#               pristine baseline is not green, or the probe could not be
#               built)
#   * exit 2 — the control could not run (no git, no python, no HEAD)
#
# Boundary: read-only
# --------------------
# The working tree is never written to.  The baseline is read with
# ``git show``, the backend tree is copied into the script's own
# ``mktemp -d`` directory, the mutation is applied to that copy, and the
# copy is removed on every exit path.  No ref is created or moved, no
# network is used, and no dependency is added.

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

#: The judge source the control reads out of git — the committed code.
JUDGE_REL="backend/postprocess/judge.py"

#: The suite the probe runs, relative to ``backend/`` (pytest is invoked
#: with ``cwd=backend/``, which is where ``backend/pytest.ini`` and the
#: venv live).
JUDGE_TEST_REL="tests/unit/postprocess/test_judge.py"

#: How many failures the mutated judge has to produce.  The test design
#: sets three as the floor: below that, the mutation may have broken the
#: suite for a reason that has nothing to do with the assertions under
#: test (a broken import, a syntax error), and the control would be
#: measuring the wrong thing.
MIN_RED_FAILURES=3

# ---------------------------------------------------------------------------
# Preflight — a control that cannot run says so instead of going quiet.
# ---------------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is not on PATH — the baseline cannot be read" >&2
    exit 2
fi

PY=""
for candidate in "$BACKEND_DIR/.venv/bin/python3" "$BACKEND_DIR/.venv/bin/python"; do
    if [ -x "$candidate" ]; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PY="$(command -v python3)"
    fi
fi
if [ -z "$PY" ]; then
    echo "ERROR: no python3 found — the control cannot run" >&2
    exit 2
fi

if ! git -C "$PROJECT_ROOT" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "ERROR: HEAD does not resolve in $PROJECT_ROOT — no baseline to measure" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# The work directory — the only thing this script writes to.
# ---------------------------------------------------------------------------

WORK="$(mktemp -d "${TMPDIR:-/tmp}/negctl-postprocess-judge.XXXXXX")"
cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

#: The committed judge source, read out of git.
if ! git -C "$PROJECT_ROOT" show "HEAD:$JUDGE_REL" > "$WORK/judge_pristine.py" 2>/dev/null; then
    echo "ERROR: cannot read HEAD:$JUDGE_REL — the baseline cannot be built" >&2
    exit 2
fi
if [ ! -s "$WORK/judge_pristine.py" ]; then
    echo "ERROR: HEAD:$JUDGE_REL is empty — the baseline cannot be built" >&2
    exit 2
fi

#: The backend tree, copied.  The suite that runs against the copy is the
#: repository's own test file, byte for byte: the control changes the
#: judge, never the cases.
mkdir -p "$WORK/backend"
if ! tar -cf - -C "$BACKEND_DIR" \
        --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' . \
        | tar -xf - -C "$WORK/backend"; then
    echo "ERROR: cannot copy the backend tree into the work directory" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Copy a source file into the copied tree, overwriting it.
install_source() {
    cp "$1" "$WORK/backend/$2"
}

#: Run the truth-table suite inside the copied tree.
#:
#: $1 — the label the log is filed under.
#: Echoes the pytest exit code; the log lands in ``$WORK/$1.log``.
run_suite() {
    local label="$1"
    local log="$WORK/$label.log"
    local code=0
    (
        cd "$WORK/backend" || exit 2
        "$PY" -m pytest "$JUDGE_TEST_REL" -q --tb=no -rfE -p no:cacheprovider
    ) > "$log" 2>&1 || code=$?
    echo "$code"
}

#: The number of tests the last run reported as failed or errored.
#:
#: The number comes from pytest's own summary line — the one that reads
#: ``4 failed, 14 passed`` — because that line counts every category,
#: including the ones a green run does not mention: a run in which
#: nothing failed has no ``N failed`` in it at all, and a counter that
#: only ever looks for that phrase reports "unknown" for the very run
#: that matters most (the pristine baseline).  The ``FAILED``/``ERROR``
#: node-id lines are the fallback for a run that died before the
#: summary; a run that failed without a single broken case prints
#: ``-1``, because something other than the assertions went wrong (a
#: collection error, a crash) and the caller has to treat that as a
#: broken probe instead of as a green run.
count_failures() {
    "$PY" - "$WORK/$1.log" <<'PYEOF'
import re
import sys
from pathlib import Path

log = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")

count = None
for candidate in reversed(re.findall(r"=+[^\n=]*=+\s*$", log, re.MULTILINE)):
    failed = re.search(r"(\d+)\s+failed", candidate)
    errors = re.search(r"(\d+)\s+errors?\b", candidate)
    passed = re.search(r"(\d+)\s+passed", candidate)
    if failed or errors or passed:
        count = (int(failed.group(1)) if failed else 0) + (
            int(errors.group(1)) if errors else 0
        )
        break

if count is None:
    broken = re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", log, re.MULTILINE)
    count = len(broken) if broken else -1

print(count)
PYEOF
}

#: The node ids the last run reported as failed or errored.
failed_ids() {
    "$PY" - "$WORK/$1.log" <<'PYEOF'
import re
import sys
from pathlib import Path

log = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
for node_id in re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", log, re.MULTILINE):
    print(node_id)
PYEOF
}

# ---------------------------------------------------------------------------
# RED — the mutated judge must break the suite.
# ---------------------------------------------------------------------------

#: Apply the mutation: the branch that answers FAIL for evidence that
#: cannot decide anything answers PASS instead.
#:
#: The anchor is the whole block, not just ``passed=False``: the string
#: ``passed=False`` appears several times in the judge, and a mutation
#: that cannot say WHICH occurrence it changed is a mutation nobody can
#: review.  If the anchor is not present exactly once, the probe cannot
#: be applied — and a probe that silently applies nothing is a vacuous
#: control, so the helper refuses instead.
mutate_judge() {
    "$PY" - "$WORK/judge_pristine.py" "$WORK/judge_mutated.py" <<'PYEOF'
import sys
from pathlib import Path

src_path = Path(sys.argv[1])
dst_path = Path(sys.argv[2])
text = src_path.read_text(encoding="utf-8")

anchor = (
    "    usable = [record for record in records if _is_usable(record)]\n"
    "    if not usable:\n"
    "        return Verdict(\n"
    "            item_id=item_id,\n"
    "            passed=False,\n"
)
found = text.count(anchor)
if found != 1:
    sys.stderr.write(
        "ERROR: the mutation anchor is present {} time(s) in {} — the "
        "judge no longer has the shape this probe mutates, and a probe "
        "that applies nothing is a vacuous control\n".format(found, src_path)
    )
    raise SystemExit(1)

mutated = text.replace(anchor, anchor.replace("passed=False", "passed=True"))
dst_path.write_text(mutated, encoding="utf-8")
PYEOF
}

if ! mutate_judge; then
    echo "ERROR: the mutation could not be applied — the control is vacuous" >&2
    exit 1
fi

install_source "$WORK/judge_mutated.py" "postprocess/judge.py"
red_code="$(run_suite mutated)"
red_failures="$(count_failures mutated)"

if [ "$red_code" -eq 0 ]; then
    echo "ERROR: the mutated judge left the suite green — the probe proves nothing" >&2
    exit 1
fi
if [ "$red_code" -gt 1 ]; then
    echo "ERROR: the mutated run did not finish (pytest exit $red_code) — the probe is broken" >&2
    exit 1
fi
if [ "$red_failures" -lt "$MIN_RED_FAILURES" ]; then
    echo "ERROR: the mutated judge broke only $red_failures case(s)," \
         "fewer than the $MIN_RED_FAILURES the control requires" >&2
    failed_ids mutated >&2
    exit 1
fi

echo "[RED]  mutated judge -> $red_failures failed"

# ---------------------------------------------------------------------------
# GREEN — the unmodified judge must pass the same suite.
# ---------------------------------------------------------------------------

install_source "$WORK/judge_pristine.py" "postprocess/judge.py"
green_code="$(run_suite pristine)"
green_failures="$(count_failures pristine)"

if [ "$green_failures" -ne 0 ] || [ "$green_code" -ne 0 ]; then
    echo "ERROR: the pristine judge did not pass the suite" \
         "($green_failures failed, pytest exit $green_code) — the control" \
         "cannot tell a mutation from a broken baseline" >&2
    failed_ids pristine >&2
    exit 1
fi

echo "[GREEN] pristine judge -> 0 failed"
echo "RESULT: OK"
exit 0