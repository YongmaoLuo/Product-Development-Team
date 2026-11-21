#!/usr/bin/env bash
#
# Verdict helpers for scripts/ci_gha.sh.
#
# This file is *sourced*, never run: it holds function definitions only,
# so that pulling it in executes no command at all.  That is what lets
# the unit tests drive the classification with recorded job lists,
# without a network, without a docker container and without a real CI
# run.
#
# What it decides
# ---------------
# A finished GitHub Actions run is red when *something* went wrong, but
# "something" has two very different flavours:
#
#   * a lane ran and failed -- a code fault, the gate is red (exit 1);
#   * a job never reached a runner -- the hosted-runner pool was out of
#     capacity, the machine could not answer, the gate is inconclusive
#     (exit 3).
#
# The second flavour is recognisable from the job list alone: a job
# GitHub never handed to a runner has *zero* steps, because no step ever
# ran.  The distinction matters because the gate's own contract already
# promises exit 3 for "environment, queueing, network or timeout -- not
# a code fault" (see the header of scripts/ci_gha.sh).  Reading only the
# run-level conclusion cannot tell the two apart, and a queue that never
# drains is not a lane that went red.
#
# ci_gha_classify_jobs answers that question from the job list:
#
#   passed         -- every job concluded success / skipped / neutral
#   infrastructure -- at least one job is not clean, and *every* such job
#                     ran zero steps (never acquired by a runner)
#   code           -- at least one such job actually ran steps
#   unknown        -- the job list could not be read; the caller must
#                     treat this as a code failure (fail safe)
#
# The JSON is parsed with the standard library of a Python 3
# interpreter, because the gate runs on developer machines and in the CI
# container, where Python is guaranteed to be present (this repository
# is a Python project) while ``jq`` is not.  When no interpreter can be
# found the answer is ``unknown`` -- the fail-safe direction, so a
# missing interpreter can never turn a red lane green.

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ci_gha_verdict_python() {
    # Prints the interpreter to use for the JSON classification, or
    # nothing at all when none can be found.  The override comes first
    # so a caller (or a test) can pin an exact interpreter.
    local candidate=""
    local resolved=""

    candidate="${PDT_CI_GHA_PYTHON:-}"
    if [ -n "${candidate}" ]; then
        # The override may be a path or a name on PATH.
        resolved="$(command -v "${candidate}" 2>/dev/null || true)"
        if [ -n "${resolved}" ]; then
            printf '%s\n' "${resolved}"
            return 0
        fi
        if [ -x "${candidate}" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi

    # Last resort: the repository's own virtualenv.  scripts/ci_gha.sh
    # puts CI_GHA_REPO_ROOT into the shell it sources this file from, so
    # the path does not depend on the current directory.
    if [ -n "${CI_GHA_REPO_ROOT:-}" ] \
        && [ -x "${CI_GHA_REPO_ROOT}/backend/.venv/bin/python3" ]; then
        printf '%s\n' "${CI_GHA_REPO_ROOT}/backend/.venv/bin/python3"
        return 0
    fi

    return 0
}

ci_gha_verdict_program() {
    # Prints the Python program that does the classification and the
    # description.  It lives in a function so that sourcing this file
    # runs no command at all.  The job list arrives on stdin, so its
    # size is not bounded by the argument list.
    cat <<'PY'
import json
import sys

#: Conclusions that mean "this job is not the reason the run is red".
CLEAN = ("success", "skipped", "neutral")


def jobs_of(payload):
    """Return the job list of a payload, or None when it cannot be read.

    Accepts both shapes ``gh`` produces: the wrapper of ``--json jobs``
    (a mapping with a ``jobs`` key) and a bare job array.
    """
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if isinstance(data, dict):
        jobs = data.get("jobs")
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = None
    if not isinstance(jobs, list):
        return None
    return jobs


def not_clean(jobs):
    """Return the jobs whose conclusion is not in :data:`CLEAN`.

    Each entry is ``(name, conclusion, step_count)``.  ``step_count`` is
    None when the job list carries no readable step array -- "we cannot
    tell" is never read as "the job never ran".
    """
    out = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        conclusion = job.get("conclusion")
        if isinstance(conclusion, str) and conclusion in CLEAN:
            continue
        steps = job.get("steps")
        count = len(steps) if isinstance(steps, list) else None
        name = job.get("name")
        out.append((name if isinstance(name, str) else "<unnamed>", conclusion, count))
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "classify"
    payload = sys.stdin.read()

    jobs = jobs_of(payload)
    if jobs is None:
        print("unknown")
        return 0

    bad = not_clean(jobs)

    if mode == "describe":
        for name, conclusion, count in bad:
            steps = "unknown" if count is None else str(count)
            print("%s [%s, %s steps]" % (name, conclusion or "unknown", steps))
        return 0

    if not bad:
        print("passed")
        return 0
    if all(count == 0 for _, _, count in bad):
        print("infrastructure")
        return 0
    print("code")
    return 0


main()
PY
}

ci_gha_classify_jobs() {
    # ci_gha_classify_jobs <jobs-json>
    #
    # Prints one of: passed | infrastructure | code | unknown.
    # Anything the classifier does not recognise is reported as
    # ``unknown``, so the caller keeps its fail-safe behaviour.
    local jobs_json="${1-}"
    local py=""
    local out=""

    py="$(ci_gha_verdict_python)"
    if [ -z "${py}" ]; then
        printf 'unknown\n'
        return 0
    fi

    out="$(printf '%s' "${jobs_json}" | "${py}" -c "$(ci_gha_verdict_program)" classify 2>/dev/null)" || out=""
    case "${out}" in
        passed|infrastructure|code|unknown)
            printf '%s\n' "${out}"
            ;;
        *)
            printf 'unknown\n'
            ;;
    esac
    return 0
}

ci_gha_describe_jobs() {
    # ci_gha_describe_jobs <jobs-json>
    #
    # Prints one line per job that is not clean, so the gate's log says
    # *which* job never reached a runner.  Prints nothing when the job
    # list cannot be read.
    local jobs_json="${1-}"
    local py=""

    py="$(ci_gha_verdict_python)"
    if [ -z "${py}" ]; then
        return 0
    fi

    printf '%s' "${jobs_json}" | "${py}" -c "$(ci_gha_verdict_program)" describe 2>/dev/null || true
    return 0
}
