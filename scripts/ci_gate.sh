#!/usr/bin/env bash
set -euo pipefail

# Uniform CI entry point for this repository.
#
# The gate framework only looks for an executable ``scripts/ci_gate.sh``
# and runs it -- it never parses the file. This adapter is the one place
# that knows how the repository runs its own gate: it hands over to
# ``scripts/ci_gha.sh``, which dispatches the ``ci.yml`` workflow and
# turns its conclusion into the verdict.
#
# The verdict is the exit code, and ``exec`` is what carries it over: the
# adapter's own exit code *is* the gate's exit code.
#
#   0 -> gate green
#   1 -> gate red
#   3 -> inconclusive (environment / queueing / network, not a code fault)
#
# ``dirname`` locates the sibling script, so the delegation holds no
# matter which directory the caller runs from; ``"$@"`` forwards the
# arguments (an empty argument list is fine -- it expands to nothing).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec bash "${SCRIPT_DIR}/ci_gha.sh" "$@"