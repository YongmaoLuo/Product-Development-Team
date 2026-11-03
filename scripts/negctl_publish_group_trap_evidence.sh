#!/usr/bin/env bash
# scripts/negctl_publish_group_trap_evidence.sh — publish the group-signal
# control's evidence to every face a citation can name.
#
# The gap this closes
# -------------------
# The control (``scripts/negctl_group_signal_trap.sh``) writes its product
# into the plan directory: ``vp_artifacts/<vp_id>/group-trap.txt`` and
# ``vp_artifacts/<vp_id>/group-trap-single-pid.txt``.  A ``code_review``
# citation, on the other hand, is resolved against ``project_dir`` --
# ``target = (project_dir / rel)`` -- and the plan directory is usually
# another checkout.  So a citation naming the product resolves against a
# STAGED COPY of it, and the staged copy has to be the same file.
#
# The staging was a one-off snapshot taken when a round was dispatched, by
# a dispatcher running a different checkout from the one that holds the
# current control.  The citable face therefore stayed one generation
# behind: the archive held the current control's product, while the
# staged copy the citations named still held the output of the control as
# it was before the fix.  A citation that resolves against a file nobody
# can reproduce is not evidence, and a snapshot that is never refreshed is
# a snapshot that keeps answering for a control that no longer exists.
#
# What this entry point does
# --------------------------
# It fixes the ORDER, and it fixes it in one place:
#
#   1. run the current control, both rounds, each in a session of its own;
#   2. file the two products in the plan directory under the names a
#      citation uses;
#   3. refresh the citable face with the repository's own staging
#      implementation, so the staging root is computed by the same
#      function the framework uses and the two faces cannot drift apart;
#   4. check that the two faces are byte-for-byte equal and that what
#      they hold is the current control's product.
#
# "Run first, publish second" is the whole point: a publisher that copied
# an old product into the citable tree would be the same defect wearing a
# different hat.
#
# What it will not do
# -------------------
# It signals nothing.  The control owns the group it signals, owns the
# witness it starts, and refuses to run at all unless it leads the group
# it is about to signal.  This script starts the control and copies
# files; the only pids this path can reach are the ones the control
# creates for itself.
#
# Usage
# -----
#   bash scripts/negctl_publish_group_trap_evidence.sh \
#       --plan-dir <plan-dir> --project-root <project-root> \
#       [--vp-id VP-014] [--plan-id <plan-id>] [--plans-dir <plans-dir>]
#
# Exit contract
# -------------
#   * exit 0 — both faces hold the current control's product
#   * exit 1 — a check fired (a round misbehaved, or a face disagrees)
#   * exit 2 — the publisher could not run (bad arguments, no python)

set -uo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"

#: The control whose product is published.
CONTROL_SCRIPT="$REPO_ROOT/scripts/negctl_group_signal_trap.sh"

PLAN_DIR=""
PROJECT_ROOT=""
VP_ID="VP-014"
PLAN_ID=""
PLANS_DIR=""

usage() {
    cat >&2 <<'EOF'
usage: bash scripts/negctl_publish_group_trap_evidence.sh \
    --plan-dir <plan-dir> --project-root <project-root> \
    [--vp-id <VP-ID>] [--plan-id <plan-id>] [--plans-dir <plans-dir>]

  --plan-dir      the plan directory that holds vp_artifacts/<vp-id>/
  --project-root  the tree a citation is resolved against
  --vp-id         the verification point whose evidence is published
                  (default: VP-014)
  --plan-id       the id the staged tree is filed under
                  (default: the plan directory's own name)
  --plans-dir     the tree the api service serves; the staged tree must
                  not land inside it (default: the backend's own
                  resolve_plans_dir())
EOF
    exit "${1:-2}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --plan-dir) PLAN_DIR="${2:-}"; shift 2 ;;
        --project-root) PROJECT_ROOT="${2:-}"; shift 2 ;;
        --vp-id) VP_ID="${2:-}"; shift 2 ;;
        --plan-id) PLAN_ID="${2:-}"; shift 2 ;;
        --plans-dir) PLANS_DIR="${2:-}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *)
            echo "unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ -z "$PLAN_DIR" ] || [ -z "$PROJECT_ROOT" ]; then
    echo "FAIL: --plan-dir and --project-root are both required" >&2
    usage
fi
if [ ! -d "$PLAN_DIR" ]; then
    echo "FAIL: the plan directory does not exist: $PLAN_DIR" >&2
    exit 2
fi
if [ ! -d "$PROJECT_ROOT" ]; then
    echo "FAIL: the project root does not exist: $PROJECT_ROOT" >&2
    exit 2
fi
if [ ! -f "$CONTROL_SCRIPT" ]; then
    echo "FAIL: the control is not in the repository: $CONTROL_SCRIPT" >&2
    exit 2
fi
if [ -z "$PLAN_ID" ]; then
    PLAN_ID="$(basename "$PLAN_DIR")"
fi

#: The python that starts a round in a session of its own, and the one
#: that stages the plan's artifacts.  The venv's python is preferred
#: because the suite needs it anyway; the system python3 is the
#: fallback.
SESSION_PYTHON=""
if [ -x "$REPO_ROOT/backend/.venv/bin/python" ]; then
    SESSION_PYTHON="$REPO_ROOT/backend/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    SESSION_PYTHON="$(command -v python3)"
fi
if [ -z "$SESSION_PYTHON" ]; then
    echo "FAIL: no python is available to start a round in a session of its own" >&2
    exit 2
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/negctl-publish.XXXXXX")" || exit 2
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# The rounds.
# ---------------------------------------------------------------------------

#: Run one round of the control.
#:
#: The control signals a group it created itself, so the shell that
#: installs the trap has to LEAD that group: the round runs in a session
#: of its own.  A shell that merely lives in somebody else's group
#: refuses to run the round at all -- and refusing is the point, because
#: the control must not be able to reproduce the bug it documents.
#:
#: The round runs from the repository root, so the census records the
#: control as the repository-relative path it is: a product that carries
#: one developer's home path into a file other people quote is a product
#: with a leak in it.
run_round() {
    local mode="$1"
    local out_dir="$2"
    mkdir -p "$out_dir" || return 2
    (
        cd "$REPO_ROOT" || exit 2
        "$SESSION_PYTHON" -c \
            'import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:], start_new_session=True).returncode)' \
            bash scripts/negctl_group_signal_trap.sh "$mode" "$out_dir"
    )
}

echo "=== publishing the group-signal control's evidence"
echo "    plan dir : $PLAN_DIR"
echo "    project  : $PROJECT_ROOT"
echo "    vp       : $VP_ID"

run_round group "$WORK_DIR/group"
group_rc=$?
if [ "$group_rc" -ne 143 ]; then
    echo "FAIL: the group round exited $group_rc, not 143 (a real SIGTERM leaves 143)" >&2
    exit 1
fi
echo "GREEN: the group round was terminated by SIGTERM and left 143"

run_round single-pid "$WORK_DIR/single-pid"
single_rc=$?
if [ "$single_rc" -ne 143 ]; then
    echo "FAIL: the single-pid round exited $single_rc, not 143 (a real SIGTERM leaves 143)" >&2
    exit 1
fi
echo "GREEN: the single-pid round was terminated by SIGTERM and left 143"

# ---------------------------------------------------------------------------
# The archive face: the plan directory the citations are written against.
# ---------------------------------------------------------------------------

ARCHIVE_DIR="$PLAN_DIR/vp_artifacts/$VP_ID"
mkdir -p "$ARCHIVE_DIR" || exit 2
cp "$WORK_DIR/group/group-trap.txt" "$ARCHIVE_DIR/group-trap.txt" || exit 2
cp "$WORK_DIR/single-pid/group-trap.txt" "$ARCHIVE_DIR/group-trap-single-pid.txt" || exit 2
echo "GREEN: both products are filed in $ARCHIVE_DIR"

# ---------------------------------------------------------------------------
# The citable face, and the check that the two faces agree.
#
# The staging is the repository's own implementation, so the staging root
# is computed by the same function the framework uses to name it.  A
# publisher that computed its own root would be free to drift away from
# the tree the citations actually resolve against.
# ---------------------------------------------------------------------------

PUBLISH_PLAN_DIR="$PLAN_DIR" \
PUBLISH_PROJECT_ROOT="$PROJECT_ROOT" \
PUBLISH_PLAN_ID="$PLAN_ID" \
PUBLISH_VP_ID="$VP_ID" \
PUBLISH_CONTROL_SCRIPT="$CONTROL_SCRIPT" \
PUBLISH_PLANS_DIR="$PLANS_DIR" \
PYTHONPATH="$REPO_ROOT/backend" \
"$SESSION_PYTHON" - <<'PY'
"""Stage the plan's products and check that both faces agree."""
import os
import re
import sys
from pathlib import Path

plan_dir = Path(os.environ["PUBLISH_PLAN_DIR"])
project_root = Path(os.environ["PUBLISH_PROJECT_ROOT"])
plan_id = os.environ["PUBLISH_PLAN_ID"]
vp_id = os.environ["PUBLISH_VP_ID"]
control_script = Path(os.environ["PUBLISH_CONTROL_SCRIPT"])
plans_dir = os.environ.get("PUBLISH_PLANS_DIR") or ""

from config_paths import resolve_plans_dir
from verification_evidence import stage_plan_artifacts

served = Path(plans_dir) if plans_dir else resolve_plans_dir()
root = stage_plan_artifacts(plan_dir, project_root, plan_id, plans_dir=served)

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def fact(text, key):
    """The value of a ``key=value`` line in an evidence file."""
    prefix = key + "="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    return ""


def census_header(text):
    """The header line of the group census."""
    for line in text.splitlines():
        if line.startswith("--- ps "):
            return line.strip()
    return ""


def census_rows(text):
    """The rows of the group census the trap wrote."""
    rows = []
    inside = False
    for line in text.splitlines():
        if line.startswith("--- ps "):
            inside = True
            continue
        if line.startswith("--- end of the census"):
            inside = False
            continue
        if not inside:
            continue
        fields = line.split()
        if fields and fields[0].isdigit():
            rows.append(fields)
    return rows


archive_dir = plan_dir / "vp_artifacts" / vp_id
staged_dir = root / "vp_artifacts" / vp_id

#: The two products, under the names the citations use.
faces = (
    ("group", "group-trap.txt"),
    ("single-pid", "group-trap-single-pid.txt"),
)

texts = {}
for mode, name in faces:
    archive = archive_dir / name
    staged = staged_dir / name
    check(archive.is_file(), "the archive copy %s is missing" % archive)
    check(staged.is_file(), "the staged copy %s is missing" % staged)
    if not (archive.is_file() and staged.is_file()):
        continue
    archived = archive.read_bytes()
    staged_bytes = staged.read_bytes()
    check(
        archived == staged_bytes,
        "%s: the staged copy differs from the archive copy, so a citation's "
        "line numbers do not carry over" % name,
    )
    texts[mode] = staged_bytes.decode("utf-8")

#: The sentinel's lifetime, read out of the control's own source: the
#: evidence has to be checked against the control that produced it, not
#: against a number written down here.
source = control_script.read_text(encoding="utf-8")
match = re.search(r"SENTINEL_LIFETIME_SECONDS:-(\d+)", source)
lifetime = int(match.group(1)) if match else 0
check(lifetime > 0, "the control declares no numeric sentinel lifetime")

group = texts.get("group", "")
single = texts.get("single-pid", "")

check(fact(group, "mode") == "group", "the group round does not report mode=group")
check(
    fact(group, "sentinel_alive_before_the_signal") == "yes",
    "the group round reports no witness alive before the signal",
)
check(
    fact(group, "sentinel_alive_after_the_signal") == "no",
    "the group round reports the witness alive after the signal",
)
check(fact(group, "received_by") == "group", "the group round does not report received_by=group")
check(
    "Terminated" in group,
    "the group round shows no terminated witness, so the text does not say "
    "that the group signal is what killed it",
)
check(
    "SENTINEL_LIFETIME_SECONDS" in group,
    "the group round's job message does not name the witness' lifetime, so "
    "the text cannot tell a witness killed by the signal from one that "
    "expired by itself",
)

check(fact(single, "mode") == "single-pid", "the single-pid round does not report mode=single-pid")
check(
    fact(single, "sentinel_alive_before_the_signal") == "yes",
    "the single-pid round reports no witness alive before the signal",
)
check(
    fact(single, "sentinel_alive_after_the_signal") == "yes",
    "the single-pid round reports the witness dead after the signal",
)
check(
    fact(single, "received_by") == "single-pid",
    "the single-pid round does not report received_by=single-pid",
)

sentinel_pid = fact(single, "sentinel_pid")
rows = census_rows(single)
check(
    any(row[0] == sentinel_pid and row[3:] == ["sleep", str(lifetime)] for row in rows),
    "the single-pid round's census does not show the witness (pid %s) alive "
    "as `sleep %s`" % (sentinel_pid, lifetime),
)

for mode, text in texts.items():
    header = census_header(text)
    check(bool(header), "the %s round wrote no census header" % mode)
    check(
        "ps -axo pid,ppid,pgid,command" in header,
        "the %s round's census header is not the current one: %r" % (mode, header),
    )
    check(
        "ps -o pid,ppid,pgid,command -g" not in text,
        "the %s round's product still carries the superseded census" % mode,
    )
    check(
        "sleep 30" not in text,
        "the %s round's product still carries the superseded witness" % mode,
    )

check(
    fact(group, "received_by") != fact(single, "received_by"),
    "both rounds report the same verdict; the control cannot tell the group "
    "from the pid",
)

# The placement rule, in both halves: the staged tree is inside the tree a
# citation resolves against, and outside the tree the api service serves.
try:
    root.resolve().relative_to(project_root.resolve())
    inside_project = True
except ValueError:
    inside_project = False
check(
    inside_project,
    "the staged tree %s is not inside the project dir %s" % (root, project_root),
)
try:
    root.resolve().relative_to(Path(served).resolve())
    inside_served = True
except ValueError:
    inside_served = False
check(
    not inside_served,
    "the staged tree %s lands inside the served plans tree %s" % (root, served),
)

if failures:
    print("FAIL: the published evidence is not the current control's product")
    for failure in failures:
        print("  - " + failure)
    sys.exit(1)

print("GREEN: the staged tree %s holds the current control's product" % root)
print("GREEN: the archive face and the citable face are byte-for-byte equal")
print("GREEN: the two rounds disagree (group -> %s, single-pid -> %s)"
      % (fact(group, "received_by"), fact(single, "received_by")))
sys.exit(0)
PY
publish_rc=$?
if [ "$publish_rc" -ne 0 ]; then
    exit "$publish_rc"
fi

echo
echo "PASS: the group-signal control's evidence is published on both faces"
exit 0