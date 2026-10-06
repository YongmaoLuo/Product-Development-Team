#!/bin/bash
# Test Runner Script
# ===================
# Activates the virtual environment and runs pytest with the given arguments.
# This ensures tests always use the correct pytest binary from the venv.

set -euo pipefail

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Virtual environment path
VENV_PATH="$PROJECT_ROOT/backend/.venv"

# Check if venv exists
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Create it from the lockfile with:"
    echo "  uv sync --project backend"
    echo ""
    echo "That builds backend/.venv at the versions backend/uv.lock pins,"
    echo "on the Python backend/pyproject.toml declares. If uv is not"
    echo "installed, see https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

# Activate the virtual environment
source "$VENV_PATH/bin/activate"

# Run pytest with all arguments passed to this script.
#
# The default target is ``backend/tests`` and it applies whether or not
# flags were passed. Earlier this script took a ``pytest "$@"`` branch as
# soon as it saw *any* argument, which dropped the target — and pytest,
# given no path, resolves its inifile from the current directory. There
# are two pytest.ini files in this repo (repo root and ``backend/``) with
# different addopts, so ``run_tests.sh`` and ``run_tests.sh -q`` ran
# different suites: 5405 tests under ``backend/pytest.ini`` versus 5524
# under the root one, with a different pass/fail outcome. A flag must
# never change *which* tests run.
#
# Arguments that name an existing path are treated as targets; everything
# else (including the value of a flag like ``-k smoke``) is passed through
# to pytest. Passing ``backend/tests/unit`` still runs just that subtree.
targets=()
passthrough=()
for arg in "$@"; do
    if [ "${arg#-}" != "$arg" ]; then
        passthrough+=("$arg")                       # a flag
    elif [ -e "$arg" ] || [ -e "$PROJECT_ROOT/$arg" ]; then
        targets+=("$arg")                           # a path that exists
    else
        passthrough+=("$arg")                       # a flag's value
    fi
done
if [ ${#targets[@]} -eq 0 ]; then
    targets=("$PROJECT_ROOT/backend/tests")
fi

# Branch rather than expanding both arrays unconditionally: macOS ships
# bash 3.2, where `"${empty[@]}"` under `set -u` is an unbound-variable
# error (fixed in bash 4.4). `run_tests.sh` with no arguments is exactly
# the case that trips it.
if [ ${#passthrough[@]} -gt 0 ]; then
    pytest "${targets[@]}" "${passthrough[@]}"
else
    pytest "${targets[@]}"
fi
