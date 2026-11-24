#!/usr/bin/env bash
set -euo pipefail

# Whole-repo gate, executed by GitHub Actions instead of locally.
#
# Exit codes are the verdict:
#   0 -> gate green   (CI concluded "success")
#   1 -> gate red     (a CI job really failed)
#   3 -> inconclusive (environment, queueing, network or timeout -- not a code fault)
#
# The red verdict needs one more distinction, and that distinction is
# the point of this script.  A run is red because *something* went wrong,
# but "something" has two flavours:
#
#   * a lane ran and failed        -- a code fault, the gate is red;
#   * a job never reached a runner -- the hosted-runner pool was out of
#     capacity, the machine could not answer, the gate is inconclusive.
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
#
# The run-level conclusion cannot tell the two apart -- a run whose only
# failing job sat in the queue for sixteen minutes and was then cancelled
# concludes "failure" all the same.  The job list can: a job GitHub
# never handed to a runner has zero steps, because no step ever ran.
#
# So the classification lives in scripts/ci_gha_verdict.sh, which is
# sourced below and holds nothing but pure functions -- the unit tests
# drive it with recorded job lists, without a network.  An infrastructure
# failure gets exactly one bounded re-run of the failed jobs; a lane
# that ran and failed is still red, and nothing is relaxed.
#
# The run-conclusion shape of the same decision is also available on its
# own, as the pure functions ``classify_run`` and ``decide_verdict``
# below: they take the run conclusion and the job counts and answer
# GREEN / RED / INFRA / UNKNOWN, and they are reachable offline through
# the ``--classify`` and ``--decide`` modes, so the offline tests can
# drive them without a repository, without the gh CLI and without a
# network.

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
# the verdict library
#
# Sourced *after* the offline entry points on purpose: those modes must
# answer without a single external command, and resolving this script's
# own directory needs ``dirname``.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ci_gha_verdict.sh
. "${SCRIPT_DIR}/ci_gha_verdict.sh"

# ---------------------------------------------------------------------------
# 0. run from the repository root
# ---------------------------------------------------------------------------

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "${repo_root}" ]; then
    die_inconclusive "not inside a git repository"
fi
cd "${repo_root}"

# The verdict library falls back to the repository's own virtualenv when
# no interpreter is on PATH; it needs the root to find it.
CI_GHA_REPO_ROOT="${repo_root}"

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

# How often a job that never reached a runner may be retried before the
# gate gives up with "inconclusive".  The retry is not a second chance
# for red: only a job that was cancelled without ever running is retried.
max_infra_reruns="${PDT_CI_GHA_INFRA_RERUNS:-1}"
case "${max_infra_reruns}" in
    ''|*[!0-9]*) max_infra_reruns="1" ;;
esac

infra_reruns_done=0

# The re-run has its own budget, so that it cannot eat the budget the
# first attempt had.
rerun_timeout="${PDT_CI_GHA_RERUN_TIMEOUT:-1800}"
case "${rerun_timeout}" in
    ''|*[!0-9]*) rerun_timeout="1800" ;;
esac

status=""
conclusion=""
attempt=""
failed_jobs=0
cancelled_jobs=0
cancelled_job_ids=""
cancelled_job_names=""

# wait_for_run <run_id> <timeout_s> <min_attempt>
#
# Waits until the run is "completed" *and* its attempt counter reached
# <min_attempt>.  The attempt guard is what keeps a re-run from being
# read as the finished first attempt: right after "gh run rerun" the run
# still reports the old attempt as completed for a moment.
#
# Prints "<status> <conclusion> <attempt>" and returns 0; returns 1 when
# the timeout expired and 2 when the run could not be read at all.
wait_for_run() {
    local wait_run_id="$1"
    local wait_timeout="$2"
    local wait_min_attempt="$3"
    local wait_deadline=$(( $(now_epoch) + wait_timeout ))
    local view_failures=0
    local view_out=""
    local run_status=""
    local run_conclusion=""
    local run_attempt=""
    local run_rest=""

    while :; do
        if ! view_out="$(gh run view "${wait_run_id}" --json status,conclusion,attempt --jq '.status + " " + (.conclusion // "") + " " + ((.attempt // 1) | tostring)' 2>/dev/null)"; then
            view_failures=$(( view_failures + 1 ))
            if [ "${view_failures}" -ge 5 ]; then
                return 2
            fi
        else
            view_failures=0
            run_status="${view_out%% *}"
            run_rest="${view_out#* }"
            run_conclusion="${run_rest%% *}"
            run_attempt="${run_rest##* }"
            case "${run_attempt}" in
                ''|*[!0-9]*) run_attempt=1 ;;
            esac
            if [ "${run_status}" = "completed" ] && [ "${run_attempt}" -ge "${wait_min_attempt}" ]; then
                printf '%s %s %s\n' "${run_status}" "${run_conclusion}" "${run_attempt}"
                return 0
            fi
        fi

        if [ "$(now_epoch)" -ge "${wait_deadline}" ]; then
            return 1
        fi
        sleep 20
    done
}

# fetch_job_evidence <run-id>
#
# Reads the job list of the run and sets failed_jobs, cancelled_jobs,
# cancelled_job_ids and cancelled_job_names.  The ids are what lets the
# retry address the job that never reached a runner.
fetch_job_evidence() {
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

# rerun_cancelled_jobs <run-id>
#
# Re-runs every cancelled job on its own -- the retry addresses the job
# that never reached a runner.  When the per-job retry is refused, or
# when the job list carried no id to address, it falls back to re-running
# the failed jobs of the run, and then to re-running the whole run.
rerun_cancelled_jobs() {
    local rid="$1"
    local job_id
    local per_job_ok=1
    local per_job_done=0

    for job_id in ${cancelled_job_ids}; do
        case "${job_id}" in
            ''|*[!0-9]*) continue ;;
        esac
        if gh run rerun "${rid}" --job "${job_id}" >/dev/null 2>&1; then
            per_job_done=$(( per_job_done + 1 ))
        else
            per_job_ok=0
            break
        fi
    done

    if [ "${per_job_ok}" -eq 0 ] || [ "${per_job_done}" -eq 0 ]; then
        printf 'ci_gha: re-running the failed jobs of run %s\n' "${rid}"
        if ! gh run rerun "${rid}" --failed; then
            printf 'ci_gha: re-run of the failed jobs refused; re-running the whole run %s\n' "${rid}"
            if ! gh run rerun "${rid}"; then
                die_inconclusive "could not re-run run ${rid}"
            fi
        fi
    fi
}

# ---------------------------------------------------------------------------
# 6. map the conclusion to the verdict
# ---------------------------------------------------------------------------

# The attempt the gate is waiting for, and the budget it may spend on it.
# The first attempt waits on the main budget; a re-run waits on its own.
min_attempt=1
run_budget="${timeout_s}"

while :; do
    wait_rc=0
    wait_out="$(wait_for_run "${run_id}" "${run_budget}" "${min_attempt}")" || wait_rc=$?
    if [ "${wait_rc}" -eq 2 ]; then
        die_inconclusive "cannot read the status of run ${run_id}"
    fi
    if [ "${wait_rc}" -ne 0 ]; then
        die_inconclusive "run ${run_id} did not finish within ${run_budget}s"
    fi

    status="${wait_out%% *}"
    wait_rest="${wait_out#* }"
    conclusion="${wait_rest%% *}"
    attempt="${wait_rest##* }"
    printf 'ci_gha: run finished (status=%s conclusion=%s attempt=%s)\n' \
        "${status}" "${conclusion}" "${attempt}"

    # The job list is the evidence.  The run level conclusion cannot tell
    # a lane that ran and failed from a job that never reached a runner.
    jobs_json="$(gh run view "${run_id}" --json jobs 2>/dev/null || true)"
    verdict="$(ci_gha_classify_jobs "${jobs_json}")"
    printf 'ci_gha: job verdict=%s\n' "${verdict}"

    if [ "${conclusion}" = "success" ]; then
        if [ "${verdict}" != "infrastructure" ]; then
            emit_gate "PASSED" "0"
            exit 0
        fi
        # A run level "success" that still carries a job that never ran
        # is not a green gate -- that job produced no evidence at all.
        printf 'ci_gha: no runner picked up a job of run %s:\n' "${run_id}"
        ci_gha_describe_jobs "${jobs_json}" || true
    else
        case "${conclusion}" in
            failure|timed_out|action_required)
                # Decided below: the job list says whether a lane ran and
                # failed or whether the job never reached a runner.
                ;;
            *)
                die_inconclusive "run ${run_id} concluded '${conclusion:-unknown}'"
                ;;
        esac

        if [ "${verdict}" != "infrastructure" ]; then
            # A lane that ran and failed -- or a job list that could not
            # be read, which is the fail-safe direction.  Nothing is
            # relaxed here.
            #
            # The exit code says *that* the gate is red; the job list says
            # *which* lane is red.  Without the second half the verdict
            # carries no locatable evidence, and whoever reads the run has
            # to go back to the API by hand to find out what failed.  The
            # job list is already in hand here, so the failing job(s) are
            # named before the verdict is emitted.
            printf 'ci_gha: failing job(s) of run %s:\n' "${run_id}"
            ci_gha_describe_jobs "${jobs_json}" || true
            emit_gate "FAILED" "1"
            exit 1
        fi

        printf 'ci_gha: no runner picked up the failing job(s):\n'
        ci_gha_describe_jobs "${jobs_json}" || true
    fi

    # The retry addresses the job that never reached a runner, so the
    # evidence that names it has to be in hand first.  The read is
    # retried a bounded number of times: a transient failure of ``gh``
    # must not silently zero the counters that decide what gets re-run.
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

    # Infrastructure failure: a bounded re-run of the jobs that never ran.
    # The policy is the pure ``decide_verdict``: only an infrastructure
    # verdict may be retried, only while the retry budget is left, and
    # only when the time the retry gets is worth spending.  The time that
    # matters is the budget of the retry itself -- the re-run has its own,
    # so it cannot eat what the first attempt had.
    if [ "${rerun_timeout}" -eq 0 ]; then
        die_inconclusive "the re-run budget is disabled and no runner picked up the failing job(s)"
    fi

    decision="$(decide_verdict "INFRA" "${infra_reruns_done}" "${max_infra_reruns}" "${rerun_timeout}")"
    case "${decision}" in
        RERUN)
            printf 'ci_gha: re-running the failed jobs of run %s (budget %ss)\n' \
                "${run_id}" "${rerun_timeout}"
            rerun_cancelled_jobs "${run_id}"

            infra_reruns_done=$(( infra_reruns_done + 1 ))
            min_attempt=$(( attempt + 1 ))
            run_budget="${rerun_timeout}"
            ;;
        *)
            die_inconclusive "run ${run_id}: ${failed_jobs} failed job(s), ${cancelled_jobs} cancelled job(s), ${infra_reruns_done}/${max_infra_reruns} infrastructure rerun(s) used"
            ;;
    esac
done
