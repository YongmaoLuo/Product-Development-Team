#!/usr/bin/env bash
# scripts/negctl_postprocess_gate.sh — the negative control for the
# finishing gate.
#
# What this control is about
# --------------------------
# The gate is the ONE decision point of the finishing stage: a plan that
# declares a requirement book (``plans/{id}/postprocess.json``) enters
# ``post_processing`` and the finishing stage owns its terminal exit; a
# plan that declares nothing walks the old path and goes straight to
# ``completed``.  That decision lives in ``_handle_passed_round`` in
# ``backend/verification_loop.py``, and it is pinned by
# ``backend/tests/unit/postprocess/test_gate.py``.
#
# A green gate suite is only half of the claim.  The other half is "the
# suite would have caught a gate that does not gate": a suite that passes
# just as happily against a verification loop that never asks the
# question measures nothing about the gate.  So this control removes the
# gate — twice, in the two shapes its removal can take — and lets the
# suite's answer be the evidence:
#
#   RED (round 1) — the gate BRANCH deleted: nobody enters the finishing
#                   stage any more.  The cases that pin the gated path
#                   ("a plan with a requirement book enters
#                   ``post_processing``") must go red, and so must the
#                   static contract ("``has_postprocess_spec(`` is called
#                   exactly once in the module") — the decision point is
#                   gone.
#
#   RED (round 2) — the gate's CONDITION deleted: the branch that asks
#                   the predicate is gone, so EVERY plan enters the
#                   finishing stage.  The zero-intrusion case ("a plan
#                   with NO requirement book still ends in ``completed``,
#                   with one terminal record") must go red — that case
#                   is the unit-level twin of the E2E scenario S1.
#
#   GREEN — the same suite, run against the unmodified verification loop,
#           passes.
#
# Both rounds are needed, and neither alone is enough.  Round 1 proves
# the gated path is load-bearing; round 2 proves the zero-intrusion
# baseline is load-bearing.  A gate suite that only pins one of the two
# halves would pass against a loop that does exactly the opposite of the
# gate.
#
# A control that cannot fail is not a control.  If a mutated loop does
# not break the suite, this script exits non-zero and says so instead of
# reporting a vacuous PASS — and the same holds for a baseline it cannot
# read at all, or a mutation it cannot apply.
#
# The baseline
# ------------
# The verification loop's source is read out of git (``git show
# HEAD:...``), never out of the working tree: the control measures the
# committed code, and a source that cannot be read at HEAD is a baseline
# the control cannot be built on — in that case the script exits non-zero
# and says why.
#
# Usage
# -----
#   bash scripts/negctl_postprocess_gate.sh
#
# Exit contract
# -------------
#   * exit 0 — the control is established (both rounds red, pristine green)
#   * exit 1 — a check fired (a mutation did not break the expected case,
#               the pristine baseline is not green, or the probe could not
#               be built)
#   * exit 2 — the control could not run (no git, no python, no HEAD)
#
# Boundary: read-only
# --------------------
# The working tree is never written to.  The baseline is read with
# ``git show``, the backend tree is copied into the script's own
# ``mktemp -d`` directory, the mutations are applied to that copy, and
# the copy is removed on every exit path.  No ref is created or moved, no
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

#: The file the gate lives in — the committed source.
LOOP_REL="backend/verification_loop.py"

#: The suite the probe runs, relative to ``backend/`` (pytest is invoked
#: with ``cwd=backend/``, which is where ``backend/pytest.ini`` and the
#: venv live).
GATE_TEST_REL="tests/unit/postprocess/test_gate.py"

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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/negctl-postprocess-gate.XXXXXX")"
cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

#: The committed verification loop, read out of git.
if ! git -C "$PROJECT_ROOT" show "HEAD:$LOOP_REL" > "$WORK/loop_pristine.py" 2>/dev/null; then
    echo "ERROR: cannot read HEAD:$LOOP_REL — the baseline cannot be built" >&2
    exit 2
fi
if [ ! -s "$WORK/loop_pristine.py" ]; then
    echo "ERROR: HEAD:$LOOP_REL is empty — the baseline cannot be built" >&2
    exit 2
fi

#: The backend tree, copied.  The suite that runs against the copy is the
#: repository's own test file, byte for byte: the control changes the
#: verification loop, never the cases.
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

#: Run the gate suite inside the copied tree.
#:
#: $1 — the label the log is filed under.
#: Echoes the pytest exit code; the log lands in ``$WORK/$1.log``.
run_suite() {
    local label="$1"
    local log="$WORK/$label.log"
    local code=0
    (
        cd "$WORK/backend" || exit 2
        "$PY" -m pytest "$GATE_TEST_REL" -q --tb=no -rfE -p no:cacheprovider
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

#: The names of the tests the last run reported as failed or errored.
#:
#: Only the case name is printed: the node id's path prefix depends on
#: where pytest was started from, and the names are what the control
#: makes its claims about.
failed_names() {
    "$PY" - "$WORK/$1.log" <<'PYEOF'
import re
import sys
from pathlib import Path

log = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
for node_id in re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", log, re.MULTILINE):
    print(node_id.rsplit("::", 1)[-1])
PYEOF
}

# ---------------------------------------------------------------------------
# The mutations
# ---------------------------------------------------------------------------

#: Apply a mutation to the verification loop.
#:
#: $1 — the mutation: ``branch`` deletes the whole gate branch,
#:       ``condition`` deletes the branch's condition (the line that asks
#:       the predicate) and dedents the body, so the branch runs
#:       unconditionally.
#:
#: The source is the committed loop and the destination is a file inside
#: the work directory; the copy in the work tree is what the suite runs
#: against.
#:
#: The gate branch is located by its header line.  The header has to be
#: present exactly once: the static contract in the gate suite says the
#: predicate is called from exactly one place, so a file in which the
#: header appears twice (or not at all) is not the file this probe knows
#: how to measure, and the helper refuses instead of guessing.
mutate_loop() {
    "$PY" - "$WORK/loop_pristine.py" "$WORK/loop_mutated.py" "$1" <<'PYEOF'
import sys
from pathlib import Path

src_path = Path(sys.argv[1])
dst_path = Path(sys.argv[2])
mode = sys.argv[3]

HEADER = "    if has_postprocess_spec(plan_dir):\n"

lines = src_path.read_text(encoding="utf-8").splitlines(keepends=True)
found = [i for i, line in enumerate(lines) if line == HEADER]
if len(found) != 1:
    sys.stderr.write(
        "ERROR: the gate branch header is present {} time(s) in {} — the "
        "verification loop no longer has the shape this probe mutates, and "
        "a probe that applies nothing is a vacuous control\n".format(
            len(found), src_path
        )
    )
    raise SystemExit(1)

start = found[0]
indent = len(HEADER) - len(HEADER.lstrip())
end = start + 1
while end < len(lines):
    line = lines[end]
    if not line.strip():
        end += 1
        continue
    if len(line) - len(line.lstrip()) <= indent:
        break
    end += 1

if mode == "branch":
    mutated = lines[:start] + lines[end:]
elif mode == "condition":
    body = []
    for line in lines[start + 1:end]:
        if line.strip() and not line.startswith(" " * (indent + 4)):
            sys.stderr.write(
                "ERROR: the gate branch body is not indented as expected — "
                "the condition cannot be deleted safely\n"
            )
            raise SystemExit(1)
        body.append(line[4:] if line.strip() else line)
    mutated = lines[:start] + body + lines[end:]
else:
    sys.stderr.write("ERROR: unknown mutation {}\n".format(mode))
    raise SystemExit(1)

dst_path.write_text("".join(mutated), encoding="utf-8")
PYEOF
}

# ---------------------------------------------------------------------------
# RED (round 1) — the gate branch deleted: nobody enters the finishing
# stage any more.  The gated path and the single-predicate contract must
# go red.
# ---------------------------------------------------------------------------

if ! mutate_loop branch; then
    echo "ERROR: the mutation could not be applied — the control is vacuous" >&2
    exit 1
fi

install_source "$WORK/loop_mutated.py" "verification_loop.py"
red_a_code="$(run_suite mutated_branch)"
red_a_failures="$(count_failures mutated_branch)"

if [ "$red_a_code" -eq 0 ]; then
    echo "ERROR: the loop without the gate branch left the suite green — the probe proves nothing" >&2
    exit 1
fi
if [ "$red_a_code" -gt 1 ]; then
    echo "ERROR: the mutated run did not finish (pytest exit $red_a_code) — the probe is broken" >&2
    exit 1
fi
if [ "$red_a_failures" -lt 1 ]; then
    echo "ERROR: the loop without the gate branch broke no case — the probe proves nothing" >&2
    exit 1
fi
if ! failed_names mutated_branch | grep -qx "test_gate_is_single_predicate"; then
    echo "ERROR: removing the gate branch did not break the single-predicate" \
         "contract — the gate's decision point is not what the suite pins" >&2
    failed_names mutated_branch >&2
    exit 1
fi
if ! failed_names mutated_branch | grep -q "^test_spec_path_"; then
    echo "ERROR: removing the gate branch did not break the gated path — the" \
         "gated path is not load-bearing" >&2
    failed_names mutated_branch >&2
    exit 1
fi

echo "[RED]  mutated gate (branch deleted) -> $red_a_failures failed"

# ---------------------------------------------------------------------------
# RED (round 2) — the gate's condition deleted: every plan enters the
# finishing stage.  The zero-intrusion case — the unit-level twin of the
# E2E scenario S1 — must go red.
# ---------------------------------------------------------------------------

if ! mutate_loop condition; then
    echo "ERROR: the mutation could not be applied — the control is vacuous" >&2
    exit 1
fi

install_source "$WORK/loop_mutated.py" "verification_loop.py"
red_b_code="$(run_suite mutated_condition)"
red_b_failures="$(count_failures mutated_condition)"

if [ "$red_b_code" -eq 0 ]; then
    echo "ERROR: the loop with an unconditional gate left the suite green — the probe proves nothing" >&2
    exit 1
fi
if [ "$red_b_code" -gt 1 ]; then
    echo "ERROR: the mutated run did not finish (pytest exit $red_b_code) — the probe is broken" >&2
    exit 1
fi
if [ "$red_b_failures" -lt 1 ]; then
    echo "ERROR: the loop with an unconditional gate broke no case — the probe proves nothing" >&2
    exit 1
fi
if ! failed_names mutated_condition | grep -qx "test_no_spec_path_keeps_completed_and_records_once"; then
    echo "ERROR: an unconditional gate did not break the zero-intrusion case" \
         "(S1) — the baseline is not load-bearing" >&2
    failed_names mutated_condition >&2
    exit 1
fi

echo "[RED]  mutated gate (condition deleted) -> $red_b_failures failed"

# ---------------------------------------------------------------------------
# GREEN — the unmodified verification loop must pass the same suite.
# ---------------------------------------------------------------------------

install_source "$WORK/loop_pristine.py" "verification_loop.py"
green_code="$(run_suite pristine)"
green_failures="$(count_failures pristine)"

if [ "$green_failures" -ne 0 ] || [ "$green_code" -ne 0 ]; then
    echo "ERROR: the pristine verification loop did not pass the suite" \
         "($green_failures failed, pytest exit $green_code) — the control" \
         "cannot tell a mutation from a broken baseline" >&2
    failed_names pristine >&2
    exit 1
fi

echo "[GREEN] pristine gate -> 0 failed"
echo "RESULT: OK"
exit 0