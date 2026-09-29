#!/bin/bash
# post_tool_use.sh — PostToolUse hook: record subagent token usage and
# append a per-subagent activity log entry so operators can detect stuck
# subagents by log mtime.
#
# Reads a single JSON object from stdin with fields:
#   task_type (str, required), task_summary (str, optional),
#   input_tokens (int, required), output_tokens (int, required),
#   started_at (str ISO8601, required), ended_at (str ISO8601, required)
#
# Inserts one row into $HOME/.pdt/subagent_metrics.db subagent_token_usage
# (SQLite at $HOME chosen so per-plan /tmp jsonl doesn't get cleaned up and
# metrics aggregate across plans).
#
# Also appends a JSON line to $PDT_SUBAGENT_LOG_FILE (fallback
# /tmp/subagent_activity.log) so external watchdogs can monitor liveness.
#
# Boundary cases (decision-4 acceptance):
#   - sqlite3 CLI missing → warn to stderr, exit 0 (do not block agent)
#   - $HOME/.pdt missing → mkdir -p
#   - DB file missing → auto-initialize via init_metrics_db.sh (idempotent)
#   - stdin JSON missing required field → reject INSERT, log to stderr
#   - stdin JSON malformed → reject INSERT, log to stderr
#   - any failure (sqlite3 error, mkdir failure) → log, exit 0 (do not block)

set -u

DB_PATH="${HOME}/.pdt/subagent_metrics.db"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Per-subagent activity log (injected by coding_tool.py _run_claude_interactive).
LOG_FILE="${PDT_SUBAGENT_LOG_FILE:-/tmp/subagent_activity.log}"
SUBAGENT_UUID="${PDT_SUBAGENT_UUID:-unknown}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Boundary: sqlite3 CLI missing → warn + exit 0 (never block the agent)
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "[post_tool_use] WARNING: sqlite3 not found in PATH, skipping insert" >&2
    exit 0
fi

# Boundary: ~/.pdt missing → mkdir -p (idempotent). If mkdir fails (read-only
# HOME) we exit 0 to avoid blocking the agent.
if ! mkdir -p "${HOME}/.pdt" 2>/dev/null; then
    echo "[post_tool_use] WARNING: cannot create ${HOME}/.pdt, skipping insert" >&2
    exit 0
fi

# Ensure log directory exists (usually /tmp, but be defensive).
LOG_DIR=$(dirname "$LOG_FILE")
mkdir -p "$LOG_DIR" 2>/dev/null || true

# First-run convenience: if the DB file does not exist yet, initialize the
# schema via the sibling init script (idempotent — safe even if it was
# already initialized by another process).
if [ ! -f "$DB_PATH" ] && [ -f "$SCRIPT_DIR/init_metrics_db.sh" ]; then
    bash "$SCRIPT_DIR/init_metrics_db.sh" >/dev/null 2>&1 || true
fi

# Read stdin to a temp file. We need this because the python heredoc below
# also uses stdin (as the script source), which would conflict with
# forwarding the JSON via stdin.
TMPFILE=$(mktemp)
TMPOUT=$(mktemp)
trap 'rm -f "$TMPFILE" "$TMPOUT"' EXIT

if ! cat > "$TMPFILE"; then
    echo "[post_tool_use] WARNING: failed to read stdin" >&2
    exit 0
fi

# Parse JSON + validate required fields + generate the INSERT SQL.
# Exit 0 on success with SQL on stdout; exit non-zero on validation failure
# with error message on stderr.
python3 - "$TMPFILE" >"$TMPOUT" 2>&1 <<'PY'
import json
import sys

input_path = sys.argv[1]

try:
    with open(input_path) as f:
        data = json.load(f)
except Exception as e:
    print(f"invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

if not isinstance(data, dict):
    print(f"top-level JSON must be an object, got {type(data).__name__}", file=sys.stderr)
    sys.exit(1)

required = ["task_type", "input_tokens", "output_tokens", "started_at", "ended_at"]
missing = [f for f in required if f not in data]
if missing:
    print(f"missing required fields: {missing}", file=sys.stderr)
    sys.exit(1)

try:
    task_type = str(data["task_type"])
    task_summary = str(data.get("task_summary", ""))
    input_tokens = int(data["input_tokens"])
    output_tokens = int(data["output_tokens"])
    started_at = str(data["started_at"])
    ended_at = str(data["ended_at"])
except (TypeError, ValueError) as e:
    print(f"type coercion failed: {e}", file=sys.stderr)
    sys.exit(1)

# SQL escape: double single quotes (SQL standard string-literal escape).
# Tokens are ints, so no escaping needed for them; timestamps and task_type
# are trusted strings from the LLM but escaping defends against quotes.
def esc(s: str) -> str:
    return s.replace("'", "''")

sql = (
    "INSERT INTO subagent_token_usage "
    "(task_type, task_summary, input_tokens, output_tokens, started_at, ended_at) "
    f"VALUES ('{esc(task_type)}', '{esc(task_summary)}', {input_tokens}, "
    f"{output_tokens}, '{esc(started_at)}', '{esc(ended_at)}');"
)
print(sql)
PY
PY_EXIT=$?

if [ "$PY_EXIT" -ne 0 ]; then
    # python's error message is in TMPOUT; log it to stderr and exit 0
    err_msg=$(cat "$TMPOUT")
    echo "[post_tool_use] rejected: $err_msg" >&2
    exit 0
fi

SQL=$(cat "$TMPOUT")

# Execute the INSERT via sqlite3 CLI. Failure is non-fatal — log and exit 0
# so a transient DB issue never blocks the agent.
if ! sqlite3 "$DB_PATH" "$SQL" 2>/dev/null; then
    echo "[post_tool_use] WARNING: sqlite3 insert failed for task_type='$task_type'" >&2
fi

# Append activity-log heartbeat so watchdogs can see this subagent is alive.
python3 - "$TIMESTAMP" "$SUBAGENT_UUID" "$LOG_FILE" <<'PY' 2>/dev/null || true
import json, sys
ts, uuid, path = sys.argv[1:4]
row = {"ts": ts, "phase": "POST", "subagent_uuid": uuid}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY

exit 0
