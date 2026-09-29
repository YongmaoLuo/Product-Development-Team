#!/bin/bash
# PreToolUse hook: log every tool invocation to a per-subagent activity log
# so operators can detect stuck subagents by checking the log mtime.
# Also logs ANTHROPIC_BASE_URL from $CLAUDE_SETTINGS_PATH for provider
# isolation verification.
set -e

LOG_FILE="${PDT_SUBAGENT_LOG_FILE:-/tmp/subagent_activity.log}"
SUBAGENT_UUID="${PDT_SUBAGENT_UUID:-unknown}"
SETTINGS_FILE="${CLAUDE_SETTINGS_PATH:-}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Read the SDK's stdin payload once so we can inspect it and forward it
# unchanged (pre_tool_use hooks may optionally transform the payload).
STDIN_PAYLOAD=$(cat)

# Try to extract the tool name from the SDK stdin JSON. Schema is defensive:
# accept 'tool', 'name', or fall back to 'unknown'.
TOOL_NAME=$(echo "$STDIN_PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool") or d.get("name") or "unknown")
except Exception:
    print("unknown")
' 2>/dev/null || echo "unknown")

# Ensure the log directory exists (usually /tmp, but be defensive).
LOG_DIR=$(dirname "$LOG_FILE")
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Append a structured activity heartbeat. This is the primary signal used
# by stuck-subagent detection: if the mtime of this log is older than N
# minutes, the subagent is likely hung inside an LLM call.
python3 - "$TIMESTAMP" "$SUBAGENT_UUID" "$TOOL_NAME" "$SETTINGS_FILE" "$LOG_FILE" <<'PY' 2>/dev/null || true
import json, sys
ts, uuid, tool, settings, path = sys.argv[1:6]
row = {
    "ts": ts,
    "phase": "PRE",
    "subagent_uuid": uuid,
    "tool": tool,
    "settings_file": settings,
}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY

# Legacy provider isolation logging: emit provider base_url to stderr so
# the backend's execution.log can capture it.
if [ -n "$SETTINGS_FILE" ] && [ -f "$SETTINGS_FILE" ]; then
    BASE_URL=$(python3 - "$SETTINGS_FILE" <<'PY' 2>/dev/null || echo "unknown"
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
print(d.get("env", {}).get("ANTHROPIC_BASE_URL", "unknown"))
PY
)
    echo "[SUBAGENT_PROVIDER] base_url=$BASE_URL timestamp=$TIMESTAMP" >&2
else
    echo "[SUBAGENT_PROVIDER] settings_file=missing timestamp=$TIMESTAMP" >&2
fi

# Forward the original stdin payload unchanged so the SDK can continue
# processing the tool call.
echo "$STDIN_PAYLOAD"
exit 0
