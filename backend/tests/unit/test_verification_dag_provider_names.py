"""TDD guard: verification_dag.py uses no hardcoded legacy provider IDs.

After the migration to kebab-case provider IDs, production code must
not contain quoted string literals that hardcode the old bare IDs
(``'vendor-b'``, ``"vendor-a"``, ``'vendor-c-app'``). This module runs the
static scanner on :mod:`verification_dag` and asserts a clean result.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Project root: this file lives at
# ``backend/tests/unit/test_verification_dag_provider_names.py``.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCANNER = PROJECT_ROOT / "scripts" / "scanners" / "scan_hardcoded_provider_ids.py"
TARGET = PROJECT_ROOT / "backend" / "verification_dag.py"


def test_no_bare_vendor_b_in_verification_dag():
    """Static scan of verification_dag.py exits 0.

    The scanner strips comments and docstrings, then looks for quoted
    string literals whose entire content is a legacy bare provider ID.
    Identifiers like ``_ordered_candidate_records`` and model names like
    ``"vendor-a M2"`` are intentionally NOT flagged.
    """
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--file", str(TARGET)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"scanner found hardcoded legacy provider IDs in "
        f"{TARGET.relative_to(PROJECT_ROOT)}:\n{result.stderr}"
    )
