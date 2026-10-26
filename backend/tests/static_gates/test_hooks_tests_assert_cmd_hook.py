"""Static gate: the hook suites assert the ``cmd`` hook, not the old shape.

Background
----------
A plan's HITL hook used to be stored as a *description* of a transport::

    {"kind": "cc_connect", "destination": "grp-1", "session_id": "s-1"}

The dispatcher does not interpret that description — its transports are
``http`` and ``cmd`` — so the stored shape was rewritten.  What a plan
stores now is the delivery command itself::

    {"kind": "cmd", "command": "<python> -m backend.hooks.session_send --project grp-1 --session s-1"}

The two suites that pin the hook configuration went stale with it: they
still expected the old element on the write path, so they no longer
pinned the write path at all.  A regression that put the write path back
to the old shape would have been invisible — the assertions that should
have caught it were the ones that had stopped matching the code.

What the gate pins
------------------
1. **No stale write-path expectation.**  Neither file still expects the
   old ``cc_connect`` element as what a plan is written with.
2. **The write path is asserted in the new shape.**  Both files assert
   the ``cmd`` kind, and they read the command's argv back with
   ``shlex.split`` — the argv, not a second implementation of the
   quoting.
3. **The aligned suites pass.**  Both files are run as a subprocess, so
   the gate reports what the suites *do*, not what their text looks like.

Scope, stated honestly
----------------------
The gate does not forbid the string ``cc_connect`` from appearing in
those files: the reader still passes a stored ``cc_connect`` element
through unchanged, and that pass-through case is legitimate — a plan
written before the rewrite must still load.  What the gate forbids is
the old element as a **write-path expectation**.

For the same reason the scan collapses whitespace before looking for the
old element: the pass-through case spells it across several lines, and
collapsing turns it into a shape the stale-write check cannot match.
The write-path expectations spelled it on one line, which is why the
stale check finds them.

The subprocess run is why this file carries the ``integration`` marker:
the unit lanes exclude that marker and bound a test at 60 s, which does
not fit a cold interpreter start plus a backend import.  The
``static-gates`` job filters no marker, so the gate runs there anyway.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# This gate drives pytest as a subprocess, so it declares the
# ``integration`` marker — the same contract
# ``tests/static_gates/test_ci_lanes_match_marker_contracts.py`` pins for
# the other subprocess drivers: the unit lanes exclude the marker and
# bound a test at 60 s, which does not fit a cold interpreter start plus
# a backend import.
pytestmark = [pytest.mark.integration]


#: ``backend/`` — this file lives at ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The two suites whose write-path expectations this gate governs.
TARGET_FILES: tuple[Path, ...] = (
    BACKEND_DIR / "tests" / "unit" / "test_hooks_config.py",
    BACKEND_DIR / "tests" / "api" / "test_plan_creation_hook_binding.py",
)

#: The old write-path element, in the two spellings the suites used: one
#: with a session, one without.
STALE_WRITE_SHAPES: tuple[str, ...] = (
    '{"kind": "cc_connect", "destination": "grp-1", "session_id": "s-1"}',
    '{"kind": "cc_connect", "destination": "grp-1", "session_id": ""}',
)


def _squashed(path: Path) -> str:
    """The file's text with every run of whitespace collapsed to one space."""
    return " ".join(path.read_text(encoding="utf-8").split())


def _text(path: Path) -> str:
    """The file's text, read from disk."""
    assert path.exists(), f"{path} is missing: the gate is scanning a tree that moved."
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} is empty: a gate that reads nothing proves nothing."
    return text


def test_no_stale_write_path_expectation() -> None:
    """Neither file still expects the old element as what a plan is written with.

    The old element is legal on the *read* path — a plan written before
    the rewrite still loads — so this is about the write path only: what
    ``write_for_plan`` produces is a ``cmd`` hook now, and an assertion
    that expects the old element is asserting a contract the writer no
    longer implements.
    """
    for path in TARGET_FILES:
        squashed = _squashed(path)
        for shape in STALE_WRITE_SHAPES:
            assert shape not in squashed, (
                f"{path} still expects the old cc_connect hook on the write "
                f"path: {shape!r} appears in it.\n"
                f"A plan's hook is now the delivery command itself — a 'cmd' "
                f"hook whose command runs backend.hooks.session_send with "
                f"the plan's destination and session. Assert that shape; the "
                f"read path may still pass a stored cc_connect element "
                f"through, but the write path never writes one."
            )


def test_write_path_expectations_use_the_cmd_shape() -> None:
    """Both files assert the ``cmd`` hook, and read its argv back.

    The kind is asserted by name, and the command is read back with
    ``shlex.split`` — the argv the dispatcher will run.  A suite that
    re-implements the quoting instead would break the day the quoting
    changed, and would say nothing about the argv.
    """
    for path in TARGET_FILES:
        text = _text(path)
        assert '"cmd"' in text, (
            f"{path} does not assert the 'cmd' hook kind.\n"
            f"A plan's hook is now the delivery command itself, so the "
            f"write-path expectation has to name the kind it expects."
        )
        assert "shlex.split" in text, (
            f"{path} does not read the command's argv back with shlex.split.\n"
            f"The hook is a command line; asserting on it means asserting "
            f"on the argv it produces, not on the string it was written as."
        )


def test_aligned_legacy_suites_pass() -> None:
    """The two aligned suites pass, as they are written.

    The other two checks in this file read text; this one runs the
    suites, so the gate fails on an assertion that does not hold and not
    only on a spelling that drifted.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_hooks_config.py",
            "tests/api/test_plan_creation_hook_binding.py",
            "-q",
        ],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, (
        "the aligned hook suites do not pass:\n"
        f"{result.stdout}\n{result.stderr}"
    )
