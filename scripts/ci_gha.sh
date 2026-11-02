#!/usr/bin/env bash
set -euo pipefail

# Whole-repo gate, executed by GitHub Actions instead of locally.
#
# Exit codes are the verdict:
#   0 -> gate green   (CI concluded "success")
#   1 -> gate red     (CI concluded failure / timed_out / action_required)
#   3 -> inconclusive (environment, queueing, network or timeout -- not a code fault)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

emit_gate() {
    # emit_gate <PASSED|FAILED|INCONCLUSIVE> <exit-code>
    printf 'gate: %s (exit %s)\n' "$1" "$2"
}

die_inconclusive() {
    # die_inconclusive <message>
    printf 'ci_gha: %s\n' "$1" >&2
    emit_gate "INCONCLUSIVE" "3"
    exit 3
}

now_epoch() {
    date -u +%s
}

# ---------------------------------------------------------------------------
# 0. run from the repository root
# ---------------------------------------------------------------------------

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "${repo_root}" ]; then
    die_inconclusive "not inside a git repository"
fi
cd "${repo_root}"

command -v gh >/dev/null 2>&1 || die_inconclusive "gh CLI is not installed"

# ---------------------------------------------------------------------------
# 1. current branch
# ---------------------------------------------------------------------------

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -z "${branch}" ]; then
    die_inconclusive "cannot determine the current branch"
fi
if [ "${branch}" = "HEAD" ]; then
    # Detached HEAD: there is no branch name to derive a slug from.
    branch="detached"
fi
printf 'ci_gha: branch=%s\n' "${branch}"

# ---------------------------------------------------------------------------
# 2. push HEAD to a throw-away probe branch
# ---------------------------------------------------------------------------

# The probe branch carries the 8-character short SHA, so two concurrent
# gate runs never share (and never delete) each other's probe branch.
sha8="$(git rev-parse --short=8 HEAD 2>/dev/null || true)"
if [ -z "${sha8}" ]; then
    die_inconclusive "cannot resolve the short SHA of HEAD"
fi
branch_slug="${branch//\//-}"
probe="ci-probe/${branch_slug}-${sha8}"
printf 'ci_gha: probe branch=%s\n' "${probe}"

# Idempotent clean-up: drop a leftover probe branch of the same name
# (same HEAD, e.g. a re-run) before creating it again.
git push origin --delete "${probe}" >/dev/null 2>&1 || true

push_out=""
if ! push_out="$(git push origin "HEAD:refs/heads/${probe}" 2>&1)"; then
    printf 'ci_gha: push of the probe branch failed:\n%s\n' "${push_out}" >&2
    emit_gate "FAILED" "1"
    exit 1
fi

# From here on the probe branch exists on the remote, so it must be
# removed again no matter how this script ends. Every command inside the
# trap is guarded with ``|| true`` so the trap can never change the
# exit status of the script.
# shellcheck disable=SC2329  # invoked through the EXIT trap below
cleanup_probe() {
    git push origin --delete "${probe}" >/dev/null 2>&1 || true
}
trap cleanup_probe EXIT

# ---------------------------------------------------------------------------
# 3. dispatch the workflow
# ---------------------------------------------------------------------------

# The dispatch timestamp is compared against GitHub's ``createdAt``, which
# comes from the server clock. Back the timestamp off by 90 seconds so a
# small local-clock skew can never filter out the run we just created.
since_epoch=$(( $(now_epoch) - 90 ))
since="$(date -u -r "${since_epoch}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || date -u -d "@${since_epoch}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || true)"
if [ -z "${since}" ]; then
    die_inconclusive "cannot format the dispatch timestamp"
fi
printf 'ci_gha: dispatching ci.yml at %s\n' "${since}"

layer="${PDT_CI_LAYER:-all}"
dispatch_err=""
if ! dispatch_err="$(gh workflow run ci.yml --ref "${probe}" -f "layer=${layer}" 2>&1 >/dev/null)"; then
    printf 'ci_gha: gh workflow run failed:\n%s\n' "${dispatch_err}" >&2
    die_inconclusive "workflow dispatch failed"
fi

# ---------------------------------------------------------------------------
# 4. locate the run that was just created
# ---------------------------------------------------------------------------

since_iso="${since}"
# Keep the runs created at/after the dispatch timestamp; prefer the one
# triggered by our own ``workflow_dispatch``; take the newest of them.
jq_program=". as \$all | (\$all | map(select(.createdAt >= \"${since_iso}\"))) as \$recent | (\$recent | map(select(.event == \"workflow_dispatch\"))) as \$disp | (if (\$disp | length) > 0 then \$disp else \$recent end) | sort_by(.createdAt) | last | .databaseId // empty"

run_id=""
deadline=$(( $(now_epoch) + 120 ))
while [ "$(now_epoch)" -lt "${deadline}" ]; do
    candidate="$(gh run list --workflow=ci.yml --branch "${probe}" --limit 50 --json databaseId,createdAt,event --jq "${jq_program}" 2>/dev/null || true)"
    candidate="$(printf '%s' "${candidate}" | tr -d '[:space:]')"
    if [ -n "${candidate}" ]; then
        run_id="${candidate}"
        break
    fi
    sleep 5
done

if [ -z "${run_id}" ]; then
    die_inconclusive "no ci.yml run for branch '${probe}' appeared within 120s"
fi
printf 'ci_gha: run_id=%s\n' "${run_id}"

# ---------------------------------------------------------------------------
# 5. wait for the run to finish
# ---------------------------------------------------------------------------

timeout_s="${PDT_CI_GHA_TIMEOUT:-3000}"
case "${timeout_s}" in
    ''|*[!0-9]*) timeout_s="3000" ;;
esac

watch_deadline=$(( $(now_epoch) + timeout_s ))
status=""
conclusion=""
view_failures=0

while :; do
    if ! view_out="$(gh run view "${run_id}" --json status,conclusion --jq '.status + " " + (.conclusion // "")' 2>/dev/null)"; then
        view_failures=$(( view_failures + 1 ))
        if [ "${view_failures}" -ge 5 ]; then
            die_inconclusive "cannot read the status of run ${run_id}"
        fi
    else
        view_failures=0
        status="${view_out%% *}"
        conclusion="${view_out##* }"
        if [ "${status}" = "completed" ]; then
            break
        fi
    fi

    if [ "$(now_epoch)" -ge "${watch_deadline}" ]; then
        die_inconclusive "run ${run_id} did not finish within ${timeout_s}s"
    fi
    sleep 20
done

# ---------------------------------------------------------------------------
# 6. map the conclusion to the verdict
# ---------------------------------------------------------------------------

case "${conclusion}" in
    success)
        emit_gate "PASSED" "0"
        exit 0
        ;;
    failure|timed_out|action_required)
        emit_gate "FAILED" "1"
        exit 1
        ;;
    *)
        die_inconclusive "run ${run_id} concluded '${conclusion:-unknown}'"
        ;;
esac
