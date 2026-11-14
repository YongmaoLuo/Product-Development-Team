#!/usr/bin/env bash
set -euo pipefail

# Whole-repo gate, executed by GitHub Actions instead of locally.
#
# Exit codes are the verdict:
#   0 -> gate green   (CI concluded "success")
#   1 -> gate red     (a CI job really failed)
#   3 -> inconclusive (environment, queueing, network or timeout -- not a code fault)
#
# The verdict is decided from *job level* evidence, not from the run level
# conclusion alone.  GitHub reports a run as "failure" even when the only
# thing that went wrong was a single job that was cancelled without ever
# being picked up by a runner (a macOS runner that ran out of capacity, for
# example).  Such a job produced no evidence at all, so it is neither green
# nor red -- it is an infrastructure problem.  The gate therefore retries
# the cancelled jobs and only gives up with "inconclusive" once the retry
# budget is spent.  A cancelled job never becomes a pass: it either runs
# for real on the retry, or the gate ends inconclusive.

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
# pure verdict logic -- no network, no git, no file writes
# ---------------------------------------------------------------------------

as_count() {
    # as_count <value> -- a count that is not a plain number counts as zero
    case "${1:-}" in
        ''|*[!0-9]*) printf '0' ;;
        *) printf '%s' "${1}" ;;
    esac
}

classify_run() {
    # classify_run <run-conclusion> <failed-jobs> <cancelled-jobs>
    #
    # Prints exactly one of GREEN / RED / INFRA / UNKNOWN.
    #
    #   success                                       -> GREEN
    #   failure|timed_out|action_required, failed>0   -> RED    (a job failed)
    #   failure|timed_out|action_required, cancelled>0 -> INFRA (a job never ran)
    #   failure|timed_out|action_required, unknown shape -> RED  (be conservative)
    #   anything else                                 -> UNKNOWN
    local conclusion="${1:-}"
    local failed
    local cancelled
    failed="$(as_count "${2:-}")"
    cancelled="$(as_count "${3:-}")"

    case "${conclusion}" in
        success)
            printf 'GREEN\n'
            ;;
        failure|timed_out|action_required)
            if [ "${failed}" -gt 0 ]; then
                printf 'RED\n'
            elif [ "${cancelled}" -gt 0 ]; then
                printf 'INFRA\n'
            else
                # The run failed but no job explains why -- assume the worst.
                printf 'RED\n'
            fi
            ;;
        *)
            printf 'UNKNOWN\n'
            ;;
    esac
}

decide_verdict() {
    # decide_verdict <verdict> <reruns-done> <max-reruns> <remaining-seconds>
    #
    # Prints exactly one of GREEN / RED / RERUN / INCONCLUSIVE.
    #
    # Only an infrastructure verdict may be retried, only while the retry
    # budget is left, and only while enough of the wall clock budget is
    # left for the retry to have a chance.
    local verdict="${1:-}"
    local reruns_done
    local max_reruns
    local remaining
    reruns_done="$(as_count "${2:-}")"
    max_reruns="$(as_count "${3:-}")"
    remaining="$(as_count "${4:-}")"

    case "${verdict}" in
        GREEN)
            printf 'GREEN\n'
            ;;
        RED)
            printf 'RED\n'
            ;;
        INFRA)
            if [ "${reruns_done}" -lt "${max_reruns}" ] && [ "${remaining}" -ge 600 ]; then
                printf 'RERUN\n'
            else
                printf 'INCONCLUSIVE\n'
            fi
            ;;
        *)
            printf 'INCONCLUSIVE\n'
            ;;
    esac
}

# ---------------------------------------------------------------------------
# offline entry points
#
# These expose the pure verdict logic to the offline tests.  They sit in
# front of every git and gh call, so they work without a repository, without
# the gh CLI and without the network.
# ---------------------------------------------------------------------------

case "${1:-}" in
    --classify)
        shift
        classify_run "${1:-}" "${2:-}" "${3:-}"
        exit 0
        ;;
    --decide)
        shift
        decide_verdict "${1:-}" "${2:-}" "${3:-}" "${4:-}"
        exit 0
        ;;
esac

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
# 5. wait for the run, judge it, retry infrastructure cancellations
# ---------------------------------------------------------------------------

timeout_s="${PDT_CI_GHA_TIMEOUT:-3000}"
case "${timeout_s}" in
    ''|*[!0-9]*) timeout_s="3000" ;;
esac

watch_deadline=$(( $(now_epoch) + timeout_s ))

# How often a cancelled job may be retried before the gate gives up with
# "inconclusive".  The retry is not a second chance for red: only a job
# that was cancelled without ever running is retried.
max_infra_reruns="${PDT_CI_GHA_INFRA_RERUNS:-1}"
case "${max_infra_reruns}" in
    ''|*[!0-9]*) max_infra_reruns="1" ;;
esac

infra_reruns_done=0

status=""
conclusion=""
failed_jobs=0
cancelled_jobs=0
cancelled_job_ids=""
cancelled_job_names=""

wait_for_run_completion() {
    # wait_for_run_completion <run-id>
    # Blocks until the run is completed; sets status and conclusion.
    local rid="$1"
    local view_out=""
    local view_failures=0

    while :; do
        if ! view_out="$(gh run view "${rid}" --json status,conclusion --jq '.status + " " + (.conclusion // "")' 2>/dev/null)"; then
            view_failures=$(( view_failures + 1 ))
            if [ "${view_failures}" -ge 5 ]; then
                die_inconclusive "cannot read the status of run ${rid}"
            fi
        else
            view_failures=0
            status="${view_out%% *}"
            conclusion="${view_out##* }"
            if [ "${status}" = "completed" ]; then
                return 0
            fi
        fi

        if [ "$(now_epoch)" -ge "${watch_deadline}" ]; then
            die_inconclusive "run ${rid} did not finish within ${timeout_s}s"
        fi
        sleep 20
    done
}

fetch_job_evidence() {
    # fetch_job_evidence <run-id>
    # Reads the job list of the run and sets failed_jobs, cancelled_jobs,
    # cancelled_job_ids and cancelled_job_names.
    local rid="$1"
    local jobs_tsv=""
    local job_id
    local job_conclusion
    local job_name

    if ! jobs_tsv="$(gh run view "${rid}" --json jobs --jq '.jobs[] | [.databaseId, .conclusion, .name] | @tsv' 2>/dev/null)"; then
        return 1
    fi

    failed_jobs=0
    cancelled_jobs=0
    cancelled_job_ids=""
    cancelled_job_names=""

    while IFS=$'\t' read -r job_id job_conclusion job_name; do
        [ -n "${job_id}" ] || continue
        case "${job_conclusion}" in
            failure|timed_out|action_required)
                failed_jobs=$(( failed_jobs + 1 ))
                ;;
            cancelled)
                cancelled_jobs=$(( cancelled_jobs + 1 ))
                cancelled_job_ids="${cancelled_job_ids} ${job_id}"
                if [ -n "${cancelled_job_names}" ]; then
                    cancelled_job_names="${cancelled_job_names}; "
                fi
                cancelled_job_names="${cancelled_job_names}${job_name:-job ${job_id}}"
                ;;
        esac
    done <<< "${jobs_tsv}"

    return 0
}

rerun_cancelled_jobs() {
    # rerun_cancelled_jobs <run-id>
    # Re-runs every cancelled job on its own; falls back to a full rerun of
    # the run when the per-job rerun is refused.
    local rid="$1"
    local job_id
    local per_job_ok=1

    for job_id in ${cancelled_job_ids}; do
        if ! gh run rerun "${rid}" --job "${job_id}" >/dev/null 2>&1; then
            per_job_ok=0
            break
        fi
    done

    if [ "${per_job_ok}" -eq 0 ]; then
        printf 'ci_gha: per-job rerun failed; falling back to a full rerun of run %s\n' "${rid}"
        if ! gh run rerun "${rid}" >/dev/null 2>&1; then
            die_inconclusive "cannot rerun run ${rid}"
        fi
    fi
}

wait_for_rerun_to_start() {
    # wait_for_rerun_to_start <run-id>
    # Waits until the run leaves the completed state again, which means the
    # rerun was accepted.  Bounded, so a rerun that never starts cannot
    # block the gate forever.
    local rid="$1"
    local rerun_deadline=$(( $(now_epoch) + 180 ))
    local view_out=""

    while [ "$(now_epoch)" -lt "${rerun_deadline}" ]; do
        if view_out="$(gh run view "${rid}" --json status --jq '.status' 2>/dev/null)"; then
            if [ "${view_out}" != "completed" ]; then
                return 0
            fi
        fi
        sleep 5
    done
    die_inconclusive "run ${rid} did not leave the completed state within 180s of the rerun request"
}

while :; do
    wait_for_run_completion "${run_id}"

    job_fetch_failures=0
    while :; do
        if fetch_job_evidence "${run_id}"; then
            break
        fi
        job_fetch_failures=$(( job_fetch_failures + 1 ))
        if [ "${job_fetch_failures}" -ge 5 ]; then
            die_inconclusive "cannot read the jobs of run ${run_id}"
        fi
        sleep 10
    done

    printf 'ci_gha: run %s concluded %s (failed jobs: %s, cancelled jobs: %s)\n' \
        "${run_id}" "${conclusion:-unknown}" "${failed_jobs}" "${cancelled_jobs}"

    verdict="$(classify_run "${conclusion}" "${failed_jobs}" "${cancelled_jobs}")"

    # A run level "success" that still carries a cancelled job is not a
    # green gate -- that job never produced evidence.  Treat it as
    # infrastructure so it gets retried instead of passed.
    if [ "${verdict}" = "GREEN" ] && [ "${cancelled_jobs}" -gt 0 ]; then
        verdict="INFRA"
    fi

    remaining=$(( watch_deadline - $(now_epoch) ))
    decision="$(decide_verdict "${verdict}" "${infra_reruns_done}" "${max_infra_reruns}" "${remaining}")"

    case "${decision}" in
        GREEN)
            emit_gate "PASSED" "0"
            exit 0
            ;;
        RED)
            emit_gate "FAILED" "1"
            exit 1
            ;;
        RERUN)
            printf 'ci_gha: retrying %s cancelled job(s) of run %s: %s\n' \
                "${cancelled_jobs}" "${run_id}" "${cancelled_job_names:-unknown}"
            rerun_cancelled_jobs "${run_id}"
            infra_reruns_done=$(( infra_reruns_done + 1 ))
            wait_for_rerun_to_start "${run_id}"
            continue
            ;;
        INCONCLUSIVE)
            die_inconclusive "run ${run_id}: ${failed_jobs} failed job(s), ${cancelled_jobs} cancelled job(s), ${infra_reruns_done}/${max_infra_reruns} infrastructure rerun(s) used"
            ;;
        *)
            die_inconclusive "run ${run_id}: unknown verdict '${decision}'"
            ;;
    esac
done