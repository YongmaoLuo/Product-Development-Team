"""The CI adapter entry point the framework looks for.

The gate framework only knows one thing about a project's CI: it looks for
an *executable* ``scripts/ci_gate.sh`` and runs it. It never parses the
file, so the contract has to be pinned here instead.

This repository already carries the implementation -- ``scripts/ci_gha.sh``
dispatches the ``ci.yml`` workflow and turns its conclusion into the
verdict (0 green / 1 red / 3 inconclusive). What was missing was the
uniform entry point the framework asks for, so this module pins the three
properties that entry point has to keep:

* it exists and is executable (the framework refuses to run anything else),
* it is valid bash (a syntax error would only surface at gate time),
* it hands over to ``ci_gha.sh`` -- the file that owns the verdict and the
  exit codes.

Every assertion here is static or a subprocess running ``bash -n``; no
network, no docker, no CI run is involved.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The uniform entry point the framework looks for.
SCRIPT = PROJECT_ROOT / "scripts" / "ci_gate.sh"

#: The implementation the entry point delegates to.
CI_GHA = PROJECT_ROOT / "scripts" / "ci_gha.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"CI adapter entry point is missing: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"CI adapter entry point is not executable: {SCRIPT}"


def test_script_is_bash_syntax_valid():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n rejected {SCRIPT}: {result.stderr.strip()}"


def test_script_execs_ci_gha():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "exec bash" in content, "the adapter must hand over to the gate, not fork it"
    assert "ci_gha.sh" in content, "the adapter must delegate to scripts/ci_gha.sh"
    assert CI_GHA.is_file(), f"the delegated script is missing: {CI_GHA}"
    assert os.access(CI_GHA, os.X_OK), f"the delegated script is not executable: {CI_GHA}"