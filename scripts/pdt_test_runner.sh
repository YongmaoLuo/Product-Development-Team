#!/usr/bin/env bash
#
# scripts/pdt_test_runner.sh -- the only sanctioned way to start a PDT
# process in test mode.
#
# A plan may declare that it runs in test mode: the pipeline is
# exercised, but the run must not touch anything real.  The mode travels
# to the child process in the environment, as PDT_PLAN_MODE -- and every
# reader of that value (the cost gate, the executor, the verification
# sub-agent) believes it.  A launch that forgot to inject it would
# therefore look like a production launch to all of them: the cost gate
# would spend real LLM quota and the sub-agent would believe it is
# running for real.  "Forgot" is not a bug class this codebase can
# afford, so the launch goes through this script instead.
#
# What it does:
#
#   * exports PDT_PLAN_MODE=test for the command it runs;
#   * refuses to run at all when the caller's own environment already
#     says PDT_PLAN_MODE=production -- a production caller must not be
#     able to start its work in test mode, and a refusal that still ran
#     the command would be a refusal in name only;
#   * exports PDT_VERIFY_ALLOW_LLM_QUOTA=1 when --allow-llm-quota is
#     given, so the cost=llm_quota verification points run instead of
#     being deferred;
#   * hands the command's own exit code back.
#
# The values it injects are the ones the rest of the codebase reads:
# PDT_PLAN_MODE and "test" come from backend/plan_mode.py (PLAN_MODE_ENV
# and MODE_TEST), PDT_VERIFY_ALLOW_LLM_QUOTA comes from
# backend/verification_executor.py (ALLOW_LLM_QUOTA_ENV), where "1" is
# one of the values treated as yes.
#
# Usage:
#
#   scripts/pdt_test_runner.sh [--allow-llm-quota] -- <command> [arg...]
#
# The "--" separator is required: it is what makes it unambiguous which
# arguments belong to the wrapper and which belong to the command, and
# it is what keeps a command's own flags (including its own "--") from
# being read as the wrapper's.

set -uo pipefail

# The environment variable that carries the plan mode, and the mode a
# test launch is allowed to run in.
PLAN_MODE_ENV="PDT_PLAN_MODE"
PLAN_MODE_TEST="test"

# The cost gate's override: it lets the llm_quota verification points
# run instead of being deferred.
ALLOW_LLM_QUOTA_ENV="PDT_VERIFY_ALLOW_LLM_QUOTA"

print_usage() {
    cat <<'EOF'
Usage: pdt_test_runner.sh [--allow-llm-quota] -- <command> [arg...]

Runs <command> with PDT_PLAN_MODE=test in its environment, so a test
launch cannot forget to inject the mode.

  --allow-llm-quota   also export PDT_VERIFY_ALLOW_LLM_QUOTA=1, which lets
                      the cost=llm_quota verification points run instead of
                      being deferred.

The "--" separator is required.  Everything after it is the command and is
passed through unchanged.

If the caller's environment already says PDT_PLAN_MODE=production the
command is NOT run: this launcher starts test-mode processes only, and a
production caller must not be able to start its work in test mode.  The
reason is written to stderr and the exit code is 2.

The exit code is the command's own exit code.
EOF
}

usage_error() {
    printf '%s\n' "pdt_test_runner.sh: $1" >&2
    print_usage >&2
    exit 2
}

# ---------------------------------------------------------------------------
# The arguments
# ---------------------------------------------------------------------------
allow_llm_quota=0
saw_separator=0

while [ $# -gt 0 ]; do
    case "$1" in
        --allow-llm-quota)
            allow_llm_quota=1
            shift
            ;;
        --)
            saw_separator=1
            shift
            break
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            usage_error "unknown argument: $1"
            ;;
    esac
done

if [ "$saw_separator" -ne 1 ]; then
    usage_error "missing '--' before the command"
fi

if [ $# -eq 0 ]; then
    usage_error "no command given after '--'"
fi

# ---------------------------------------------------------------------------
# The caller's own mode
# ---------------------------------------------------------------------------
# The mode is normalised the way backend/plan_mode.py normalises it --
# surrounding whitespace stripped, case folded -- so that " Test " and
# "TEST" are recognised as the test mode rather than being mistaken for
# an unknown mode.  An unknown mode is not the test mode: the plan_mode
# module falls back to production for anything it does not recognise,
# and this launcher follows the same rule.
raw_mode="${!PLAN_MODE_ENV-}"
leading="${raw_mode%%[![:space:]]*}"
trimmed="${raw_mode#"$leading"}"
trailing="${trimmed##*[![:space:]]}"
trimmed="${trimmed%"$trailing"}"
caller_mode="$(printf '%s' "$trimmed" | tr '[:upper:]' '[:lower:]')"

case "$caller_mode" in
    ""|"$PLAN_MODE_TEST")
        # Not set, or already the test mode: nothing to refuse.
        ;;
    *)
        {
            printf '%s\n' "pdt_test_runner.sh: refusing to run: the caller's environment says ${PLAN_MODE_ENV}=${raw_mode}."
            printf '%s\n' "pdt_test_runner.sh: this launcher starts test-mode processes only, and a production caller must not start its work in test mode."
            printf '%s\n' "pdt_test_runner.sh: unset ${PLAN_MODE_ENV} (or set it to '${PLAN_MODE_TEST}') and run again."
        } >&2
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# The launch
# ---------------------------------------------------------------------------
export "${PLAN_MODE_ENV}=${PLAN_MODE_TEST}"

if [ "$allow_llm_quota" -eq 1 ]; then
    export "${ALLOW_LLM_QUOTA_ENV}=1"
fi

# exec, not a plain call: the shell is replaced by the command, so the
# command's own exit code is this script's exit code and a signal aimed
# at this process reaches the command instead of stopping here.
exec "$@"