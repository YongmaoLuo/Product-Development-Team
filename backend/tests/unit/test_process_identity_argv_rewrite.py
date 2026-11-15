"""A re-exec rewrites ``argv[0]``; that is not a change of identity.

What is under test
------------------
:func:`process_identity.matches` -- the check that decides whether a
recorded pid is still the process the caller recorded, and therefore
whether a kill may be aimed at it.

Why the check has to survive a re-exec
--------------------------------------
On macOS the framework build of Python is a *launcher*: it starts, and
then re-executes the real interpreter with a rewritten ``argv[0]`` --
the resolved path inside ``Python.app`` rather than the path the caller
asked for.  The kernel reports the rewritten one from that moment on.

So the same process is reported two ways:

    recorded : '<repo>/backend/.venv/bin/python', '-c', '<program>'
    live     : '/opt/homebrew/.../Python.app/Contents/MacOS/Python', '-c', '<program>'

The two name **one** process -- the start time is identical to the
microsecond -- but they do not compare equal character for character.
The old rule ("the first two entries have to be equal") therefore said
*no* for a process that was very much alive, and the caller's answer
was "this is somebody else's pid":

* the executor the scheduler had admitted was never adopted after a
  backend restart, so the read model never settled
  (``used 2, queue_length 0``) and the scheduler e2e timed out;
* the next ``start_execution`` found the pid still in the ledger and
  refused with ``409 already_running``.

What the rule may and may not become
------------------------------------
The tolerance is for ``argv[0]`` alone, and only when the two
spellings name **the same executable**:

* the same file (``os.path.samefile``), or the same file after
  symlinks are resolved (``realpath``) -- a venv's ``bin/python`` and
  the interpreter it points at;
* the same macOS *framework bundle* -- ``Python.framework`` holds one
  interpreter per version, and the launcher that re-execs it lives in
  the same bundle.  The version is part of the comparison, so 3.11 and
  3.12 are not the same interpreter.

Everything else still says no.  A different program, a different first
argument, a different length -- all still a mismatch, and a mismatch is
the safe direction: the caller refuses to signal.

Run them with:

    cd backend && .venv/bin/python3 -m pytest \
        tests/unit/test_process_identity_argv_rewrite.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from process_identity import ProcessIdentity, matches  # noqa: E402


#: The start time both readings of one process carry.  It never changes
#: while the process lives, so a difference here means a different
#: process.
_CREATE_TIME = 1_700_000_000.5

#: A pid that is neither 1 nor this process -- ``matches`` refuses both.
_PID = 424242


def _identity(cmdline: Sequence[str]) -> ProcessIdentity:
    """The identity the kernel reports for the live side."""
    return ProcessIdentity(
        pid=_PID, create_time=_CREATE_TIME, cmdline=tuple(cmdline)
    )


def _matches(live: Sequence[str], recorded: Sequence[str]) -> bool:
    """Do the live and the recorded command line name the same process?"""
    return matches(
        _identity(live),
        create_time=_CREATE_TIME,
        cmdline=tuple(recorded),
    )


def _framework_bundle() -> Optional[tuple[Path, str]]:
    """The macOS framework bundle the running interpreter lives in.

    Returns the bundle's root and the version inside it, or ``None``
    when this interpreter is not a framework build -- which is the case
    on every platform but macOS.
    """
    resolved = Path(os.path.realpath(sys.executable))
    parts = resolved.parts
    for index, part in enumerate(parts):
        if not part.endswith(".framework"):
            continue
        root = Path(*parts[:index + 1])
        versions = root / "Versions"
        if not versions.is_dir():
            return None
        versions_on_disk = sorted(
            child.name for child in versions.iterdir() if child.is_dir()
        )
        for version in versions_on_disk:
            if (versions / version / "bin").is_dir():
                return root, version
    return None


# ---------------------------------------------------------------------------
# 1. The same executable under two spellings
# ---------------------------------------------------------------------------


def test_the_same_executable_under_two_spellings_is_the_same_process(tmp_path):
    """A symlink to the interpreter and the interpreter are one program.

    This is the ordinary case, and it is not macOS-specific: a virtual
    environment's ``bin/python`` is a link to the interpreter it was
    built against, and a process that is started through the link is
    reported through the interpreter's own path.
    """
    link = tmp_path / "python"
    link.symlink_to(sys.executable)

    recorded = (str(link), "-c", "import time; time.sleep(1)")
    live = (os.path.realpath(str(link)), "-c", "import time; time.sleep(1)")

    assert _matches(live, recorded), (
        "the same executable, reached through a symlink, was reported as "
        "a different process"
    )


def test_the_same_path_spelled_the_same_way_is_the_same_process():
    """The unchanged case still holds -- nothing about it was relaxed."""
    argv = ("/usr/bin/python3", "-c", "import time; time.sleep(1)")

    assert _matches(argv, argv)


# ---------------------------------------------------------------------------
# 2. The framework launcher that rewrites argv[0]
# ---------------------------------------------------------------------------


def test_a_framework_launcher_rewrite_is_not_a_change_of_identity():
    """The macOS launcher rewriting ``argv[0]`` does not change the process.

    The launcher that re-execs a framework build of Python lives in the
    same bundle as the interpreter it starts, and it reports itself
    under its own path.  The two paths are different files -- the
    launcher is not the interpreter -- but they are the same
    *interpreter*, and the version inside the bundle is what says so.
    """
    bundle = _framework_bundle()
    if bundle is None:
        pytest.skip(
            "this interpreter is not a macOS framework build, so there "
            "is no launcher rewrite to reproduce"
        )
    root, version = bundle
    launcher = (
        root / "Versions" / version / "Resources" / "Python.app"
        / "Contents" / "MacOS" / "Python"
    )
    if not launcher.exists():
        pytest.skip(f"the framework launcher {launcher} is not on this machine")

    recorded = (sys.executable, "-c", "import time; time.sleep(1)")
    live = (str(launcher), "-c", "import time; time.sleep(1)")

    assert _matches(live, recorded), (
        f"the launcher {launcher} re-exec'd the interpreter it lives "
        f"with, and the two were reported as different processes"
    )


# ---------------------------------------------------------------------------
# 3. What the tolerance must not let through
# ---------------------------------------------------------------------------


def test_a_different_program_is_still_a_mismatch():
    """Two different programs never match, however they are spelled."""
    recorded = ("/bin/sh", "-c", "import time; time.sleep(1)")
    live = ("/bin/bash", "-c", "import time; time.sleep(1)")

    assert not _matches(live, recorded), (
        "two different programs were reported as the same process"
    )


def test_a_different_first_argument_is_still_a_mismatch():
    """The first argument is part of the identity and is not relaxed."""
    recorded = ("/usr/bin/python3", "-m", "pytest")
    live = ("/usr/bin/python3", "-c", "import time; time.sleep(1)")

    assert not _matches(live, recorded), (
        "two different invocations of one program were reported as the "
        "same process"
    )


def test_a_different_length_is_still_a_mismatch():
    """A wrapper that added an argument is still a different invocation."""
    recorded = ("/usr/bin/python3", "-c")
    live = ("/usr/bin/python3", "-c", "import time; time.sleep(1)")

    assert not _matches(live, recorded), (
        "a command line with an extra argument was reported as the same "
        "process"
    )


def test_no_identity_is_still_no_match():
    """An unreadable pid is never a match -- the rule was not relaxed."""
    assert not matches(
        None, create_time=_CREATE_TIME, cmdline=("/usr/bin/python3", "-c"),
    ), "a process that could not be read was reported as a match"


def test_a_different_start_time_is_still_a_mismatch():
    """A recycled pid reports a start time from another life."""
    assert not matches(
        _identity(("/usr/bin/python3", "-c")),
        create_time=_CREATE_TIME + 60.0,
        cmdline=("/usr/bin/python3", "-c"),
    ), "a pid that had been recycled was reported as the same process"
