#!/bin/bash
# init_metrics_db.sh — Initialize the subagent token usage metrics database.
#
# Creates $HOME/.pdt/subagent_metrics.db with the subagent_token_usage table
# and 2 indexes (task_type, started_at). Idempotent: safe to re-run; CREATE
# TABLE/INDEX use IF NOT EXISTS.
#
# Boundary cases:
#   - $HOME/.pdt missing → mkdir -p
#   - sqlite3 CLI not installed → warn to stderr, exit 0 (do not block)
#   - DB path exists but is empty → schema is still applied
#   - mkdir fails (read-only HOME) → warn, exit 0
#
# TDD spec:
#   - test_post_hook_schema: table + 2 indexes exist after this runs

set -u

PDT_DIR="${HOME}/.pdt"
DB_PATH="${PDT_DIR}/subagent_metrics.db"

# Boundary: sqlite3 CLI missing → warn + exit 0
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "[init_metrics_db] WARNING: sqlite3 not found in PATH, skipping DB init" >&2
    exit 0
fi

# Boundary: ~/.pdt missing → mkdir -p
if ! mkdir -p "$PDT_DIR" 2>/dev/null; then
    echo "[init_metrics_db] WARNING: cannot create $PDT_DIR, skipping DB init" >&2
    exit 0
fi

# Idempotent schema: CREATE TABLE/INDEX use IF NOT EXISTS so re-runs are safe.
sqlite3 "$DB_PATH" <<'SQL'
CREATE TABLE IF NOT EXISTS subagent_token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    task_summary TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_type ON subagent_token_usage(task_type);
CREATE INDEX IF NOT EXISTS idx_started_at ON subagent_token_usage(started_at);
SQL

exit 0
