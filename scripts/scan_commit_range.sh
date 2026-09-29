#!/usr/bin/env bash
# scripts/scan_commit_range.sh — scan every commit in a range for private info.
#
# Why this exists
# ---------------
# The privacy gates in ``backend/tests/static_gates/`` read the current
# tree. That answers "is the repository clean now", which is not the same
# question as "did we publish something we should not have". A commit that
# adds an operator's private project name and a later commit that removes
# it leaves a clean tree and a dirty history — and the dirty commit is
# still fetchable by anyone holding its SHA, and through
# ``refs/pull/<N>/head`` even after a squash merge.
#
# So this scans commits, not the tree and not the diff. The rules
# themselves are not restated here: ``commit_range_scan.py`` imports the
# same pure functions the tree gates use (``find_home_paths``,
# ``find_attribution``, ``find_local_measurements``), so the two scans
# cannot disagree about what a violation is.
#
# One entry point, two callers
# ----------------------------
# The same script runs from:
#
#   * the ``commit-range-privacy`` GitHub Actions job (``--ci``);
#   * the local ``pre-push`` hook (``--stdin-pre-push``), installed by
#     ``scripts/install_git_hooks.sh``;
#   * an ad-hoc shell (``--range``).
#
# That mirrors ``scripts/grep_guard.sh``, for the same reason: three
# callers each rediscovering the venv path, the argument shape and the
# exit-code contract is how they drift apart.
#
# The hook is the one that matters
# --------------------------------
# CI runs *after* the push, so by the time it fails, the commits are
# already public. The pre-push hook is the only preventive point — and at
# that moment rewriting the offending commit is free, because nothing has
# been published yet. That asymmetry is why the hook blocks.
#
# Exit contract
# -------------
#   * 0 — scan ran, zero violations
#   * 1 — scan ran, at least one violation (CI / hook fail)
#   * 2 — usage / setup error, or the scan could not run to completion
#
# 2 is deliberately distinct from 1: "the gate found something" and "the
# gate did not run" need different responses from whoever reads the
# message, and collapsing them lets a broken gate look like a clean one.
#
# Usage
# -----
#   bash scripts/scan_commit_range.sh --stdin-pre-push      # from the hook
#   bash scripts/scan_commit_range.sh --ci --max-commits 5  # from CI
#   bash scripts/scan_commit_range.sh --range origin/main..HEAD
#   bash scripts/scan_commit_range.sh --help

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution — ``BASH_SOURCE`` so the script works from any cwd.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
SCANNER_DIR="$BACKEND_DIR/tests/static_gates"
SCANNER="$SCANNER_DIR/commit_range_scan.py"
VENV_PY="$BACKEND_DIR/.venv/bin/python3"

#: Commit count above which a *local* run warns. CI passes an explicit
#: ``--max-commits`` and fails instead; see the note in ``main``.
WARN_COMMIT_THRESHOLD=5

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USE_CI=0
USE_STDIN=0
MAX_COMMITS=""
RANGE_SPECS=()

usage() {
    cat <<'USAGE'
scripts/scan_commit_range.sh — scan every commit in a range for private info

Usage:
  bash scripts/scan_commit_range.sh --stdin-pre-push       # git pre-push hook
  bash scripts/scan_commit_range.sh --ci --max-commits 5   # GitHub Actions
  bash scripts/scan_commit_range.sh --range origin/main..HEAD
  bash scripts/scan_commit_range.sh --help

Options:
  --ci               Resolve the range from PDT_BASE_SHA (set by the workflow)
  --stdin-pre-push   Resolve the range from the ref lines git feeds a pre-push hook
  --range SPEC       Scan SPEC (repeatable), e.g. origin/main..HEAD
  --max-commits N    Fail when the range holds more than N commits
  --help             This text

Exit codes:
  0  scan ran, zero violations
  1  scan ran, at least one violation
  2  usage / setup error, or the scan could not run to completion
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ci)
            USE_CI=1
            shift
            ;;
        --stdin-pre-push)
            USE_STDIN=1
            shift
            ;;
        --range)
            if [ $# -lt 2 ]; then
                echo "ERROR: --range requires a revision range" >&2
                exit 2
            fi
            RANGE_SPECS+=("$2")
            shift 2
            ;;
        --max-commits)
            if [ $# -lt 2 ] || ! [[ "$2" =~ ^[0-9]+$ ]]; then
                echo "ERROR: --max-commits requires a non-negative integer" >&2
                exit 2
            fi
            MAX_COMMITS="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ ! -f "$SCANNER" ]; then
    echo "ERROR: scanner module not found: $SCANNER" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Range resolution
# ---------------------------------------------------------------------------

if [ "$USE_CI" -eq 1 ]; then
    BASE="${PDT_BASE_SHA:-}"
    if [ -z "$BASE" ]; then
        echo "ERROR: --ci needs PDT_BASE_SHA in the environment" >&2
        exit 2
    fi
    if [[ "$BASE" =~ ^0+$ ]]; then
        # The null SHA means the ref is new, so there is no base to diff
        # against. Everything not already on a remote is what this push
        # publishes.
        echo "note: PDT_BASE_SHA is the null SHA (new ref); scanning commits not on any remote" >&2
        RANGE_SPECS+=("HEAD --not --remotes")
    elif git -C "$PROJECT_ROOT" rev-parse --quiet --verify "${BASE}^{commit}" >/dev/null 2>&1; then
        RANGE_SPECS+=("${BASE}..HEAD")
    elif git -C "$PROJECT_ROOT" rev-parse --quiet --verify "HEAD~1" >/dev/null 2>&1; then
        # Loud, not silent: the narrow range still checks something, but
        # the caller must know the scan is smaller than it looks.
        echo "WARNING: base ${BASE} is not resolvable; scanning HEAD~1..HEAD only" >&2
        RANGE_SPECS+=("HEAD~1..HEAD")
    else
        echo "WARNING: no base and no parent commit; scanning the tip only" >&2
        RANGE_SPECS+=("HEAD")
    fi
fi

if [ "$USE_STDIN" -eq 1 ]; then
    # git feeds a pre-push hook one line per ref:
    #   <local ref> <local sha> <remote ref> <remote sha>
    # The final line may lack a trailing newline, hence the ``|| [ -n ... ]``
    # -- without it the last ref is silently skipped, which is the one case
    # a hook must not get wrong.
    while read -r local_ref local_sha remote_ref remote_sha || [ -n "${local_sha:-}" ]; do
        if [ -z "${local_sha:-}" ]; then
            continue
        fi
        if [[ "$local_sha" =~ ^0+$ ]]; then
            continue   # deleting a ref: nothing to scan
        fi
        if [[ "$remote_sha" =~ ^0+$ ]]; then
            RANGE_SPECS+=("${local_sha} --not --remotes")
        else
            RANGE_SPECS+=("${remote_sha}..${local_sha}")
        fi
    done
fi

if [ "${#RANGE_SPECS[@]}" -eq 0 ]; then
    if [ "$USE_STDIN" -eq 1 ]; then
        # git runs pre-push even when every ref is already up to date, and
        # in that case it feeds no ref lines at all. Nothing is being
        # published, so there is nothing to scan — failing here would
        # reject an ordinary `git push` on a clean branch, which is how a
        # hook earns itself a --no-verify.
        echo "nothing to push — no commits to scan"
        exit 0
    fi
    echo "ERROR: no range to scan — pass --range, --ci or --stdin-pre-push" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Commit budget — before the scan, so an oversized range costs nothing
# ---------------------------------------------------------------------------

# ``while read`` into an array rather than ``mapfile``: macOS ships bash
# 3.2, which has no ``mapfile``, and this script runs from a developer's
# shell as often as from CI (where bash is 5). The ``< <(...)`` form keeps
# the loop in the current shell, so ``RANGE_SHAS`` survives it.
RANGE_SHAS=()
while IFS= read -r sha; do
    RANGE_SHAS+=("$sha")
done < <(
    for spec in "${RANGE_SPECS[@]}"; do
        read -r -a spec_args <<<"$spec"
        git -C "$PROJECT_ROOT" rev-list "${spec_args[@]}" 2>/dev/null || true
    done | awk '!seen[$0]++'
)

COMMIT_COUNT="${#RANGE_SHAS[@]}"

if [ "$COMMIT_COUNT" -eq 0 ]; then
    # Same reasoning as the empty-spec case above: a ref pair whose two
    # sides are equal (``X..X``) is an up-to-date push, not a broken scan.
    # Every other mode asserted that there is something to scan.
    if [ "$USE_STDIN" -eq 1 ]; then
        echo "nothing to push — every ref is already up to date"
        exit 0
    fi
    echo "ERROR: the range resolved to no commits; the scan would prove nothing" >&2
    exit 2
fi

echo "checking ${COMMIT_COUNT} commit(s) in: ${RANGE_SPECS[*]}"

if [ -n "$MAX_COMMITS" ]; then
    if [ "$COMMIT_COUNT" -gt "$MAX_COMMITS" ]; then
        echo "FAIL: ${COMMIT_COUNT} commits in range, limit is ${MAX_COMMITS}" >&2
        echo "  Squash the branch before opening the pull request:" >&2
        echo "    git rebase -i origin/main   # or: git reset --soft origin/main" >&2
        exit 1
    fi
elif [ "$COMMIT_COUNT" -gt "$WARN_COMMIT_THRESHOLD" ]; then
    # Local runs warn rather than fail: a commit count is not a leak, and
    # a long-running work-in-progress branch is a legitimate thing to push.
    # CI is where the budget is enforced, because CI is where the cost is.
    echo "WARNING: ${COMMIT_COUNT} commits in range (over ${WARN_COMMIT_THRESHOLD}); CI will reject this pull request" >&2
fi

# ---------------------------------------------------------------------------
# The privacy scan
# ---------------------------------------------------------------------------

PYTHON_CMD=""
if [ -x "$VENV_PY" ]; then
    PYTHON_CMD="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    echo "WARNING: venv python not found at $VENV_PY; falling back to system python3" >&2
    echo "WARNING: results may differ from CI — please create the venv per CLAUDE.md" >&2
    PYTHON_CMD="$(command -v python3)"
else
    echo "ERROR: neither venv python ($VENV_PY) nor system python3 found" >&2
    exit 2
fi

RANGE_ARGS=()
for spec in "${RANGE_SPECS[@]}"; do
    RANGE_ARGS+=(--range "$spec")
done

PYTHON_RC=0
PYTHON_OUTPUT=$("$PYTHON_CMD" "$SCANNER" --repo "$PROJECT_ROOT" "${RANGE_ARGS[@]}" 2>&1) || PYTHON_RC=$?

# ---------------------------------------------------------------------------
# The secret scan — over the same commits
# ---------------------------------------------------------------------------

# ``.gitleaks.toml`` is committed, so a caller need not supply a config.
SECRET_RC=0
SECRET_OUTPUT=""
if command -v gitleaks >/dev/null 2>&1; then
    for spec in "${RANGE_SPECS[@]}"; do
        out=$(gitleaks detect \
            --no-banner \
            --redact \
            --config "$PROJECT_ROOT/.gitleaks.toml" \
            --log-opts="$spec" 2>&1) || SECRET_RC=$?
        SECRET_OUTPUT="${SECRET_OUTPUT}${out}"$'\n'
    done
else
    # Loud, never silent. A skipped scan must not read as a passing one --
    # that is the failure this whole script exists to prevent, and a
    # quiet skip would reproduce it in the scanner itself.
    echo "WARNING: gitleaks not on PATH — the secret scan over this range was SKIPPED." >&2
    echo "WARNING: the privacy rules above did run; only the credential scan is missing." >&2
    echo "WARNING: install it (brew install gitleaks) to close the gap; CI always runs it." >&2
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

printf '%s\n' "$PYTHON_OUTPUT"
if [ -n "$SECRET_OUTPUT" ]; then
    printf '%s' "$SECRET_OUTPUT"
fi

if [ "$PYTHON_RC" -ne 0 ] || [ "$SECRET_RC" -ne 0 ]; then
    cat >&2 <<'HOWTO'

A violation inside a commit cannot be fixed by adding another commit — the
commit that carries it stays fetchable by its SHA. Rewrite it instead:

    git rebase -i origin/main     # mark the offending commit "edit"
    # ...fix the file...
    git commit --amend --no-edit
    git rebase --continue
    git push --force-with-lease   # the branch, not main

On an unmerged branch this is free: nothing has been published yet.
HOWTO
    # A non-zero python exit is 1 (violations) or 2 (could not run); the
    # script's 2 means the same thing, so pass the distinction through.
    if [ "$PYTHON_RC" -eq 2 ] || [ "$SECRET_RC" -gt 1 ]; then
        exit 2
    fi
    exit 1
fi

echo "OK  commit-range privacy scan: clean"
exit 0
