"""Constant contract tests for ``backend/verification_executor.py``.

Background
----------
The :class:`VerificationExecutor` (in ``backend/verification_executor.py``)
persists its verdict map to a state file under ``plan_dir``. The
filename is exported as the module-level constant
``DEFAULT_STATE_FILENAME``. A historical constant used the
``sidecar`` token (``verification_executor_sidecar.json``), which
drifted from the canonical ``verification_executor_state.json``
filename that the rest of the codebase and the state-machine
repository actually use.

This drift silently produced a state file with a different name than
the one callers expected. The two tests below pin the canonical
contract so future refactors cannot re-introduce the
``sidecar`` token without breaking the contract:

  1. ``test_default_state_filename_is_state_not_sidecar``
     Imports ``DEFAULT_STATE_FILENAME`` from
     ``backend.verification_executor`` and asserts it equals the
     authoritative filename (``verification_executor_state.json``).

  2. ``test_default_state_filename_has_no_sidecar_token``
     Same import, but asserts the constant does NOT contain the
     substring ``sidecar`` (case-insensitive). This catches the
     specific class of regression where the filename gets the
     ``sidecar`` token re-introduced — even if the rest of the
     filename changes for unrelated reasons.

The tests do NOT touch any real LLM / network. They are pure
attribute checks against a module constant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Ensure ``backend/`` is on ``sys.path`` so ``import verification_executor``
# works regardless of which test runner entry point is used. Mirrors
# the pattern in ``test_verification_executor.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_executor import DEFAULT_STATE_FILENAME  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The authoritative state filename. This is the single source of
#: truth for the on-disk file name; every test in this module must
#: compare against this exact string.
EXPECTED_DEFAULT_STATE_FILENAME = "verification_executor_state.json"

#: The forbidden token. Any occurrence of this substring in
#: ``DEFAULT_STATE_FILENAME`` (case-insensitive) means the constant
#: has drifted from the canonical name and the state file will not
#: match what callers expect.
FORBIDDEN_TOKEN = "sidecar"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_default_state_filename_is_state_not_sidecar() -> None:
    """``DEFAULT_STATE_FILENAME`` must equal the authoritative name.

    Pins the contract: importing the constant must yield exactly
    ``"verification_executor_state.json"`` (the filename the
    state-machine repository and the rest of the codebase use).

    Any other value — including the historical
    ``"verification_executor_sidecar.json"`` — fails this test, so
    a silent rename back to the sidecar form would be caught.
    """
    assert DEFAULT_STATE_FILENAME == EXPECTED_DEFAULT_STATE_FILENAME, (
        f"DEFAULT_STATE_FILENAME must be exactly "
        f"{EXPECTED_DEFAULT_STATE_FILENAME!r} (the authoritative state "
        f"filename), got {DEFAULT_STATE_FILENAME!r}. The constant has "
        f"drifted from the canonical name; callers will write/read a "
        f"file that the rest of the codebase does not recognise."
    )


def test_default_state_filename_has_no_sidecar_token() -> None:
    """``DEFAULT_STATE_FILENAME`` must not contain the ``sidecar`` token.

    Case-insensitive substring check. Catches the specific class of
    regression where the ``sidecar`` token is re-introduced into the
    filename — even if the rest of the filename changes for unrelated
    reasons — so the test suite is robust against partial drift.
    """
    assert FORBIDDEN_TOKEN.lower() not in DEFAULT_STATE_FILENAME.lower(), (
        f"DEFAULT_STATE_FILENAME must NOT contain the {FORBIDDEN_TOKEN!r} "
        f"token (case-insensitive), got {DEFAULT_STATE_FILENAME!r}. The "
        f"canonical filename is {EXPECTED_DEFAULT_STATE_FILENAME!r} — "
        f"callers and the state-machine repository both expect that name."
    )
