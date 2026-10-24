"""A deterministic local failure is raised, not retried in a fresh session.

What this pins
--------------
The resume helper degrades a resume that cannot be honoured: it falls
back to a fresh session and re-sends the prompt. That degradation is
right for a *provider* answer ("no such session", "unknown session
id") and wrong for a *local* answer ("no such file or directory",
"not a directory", "permission denied").

A local answer is deterministic: the same call in a fresh session runs
in the same missing directory and fails the same way. So the fallback
buys nothing and costs a whole dispatch plus a second copy of the
prompt — and, worse, it hides the real cause behind a log line that
says "resume failed, retrying in a fresh session".

The contract these tests hold in place:

1. **The classification is one function.** ``is_deterministic_local_error``
   recognises exactly the three system errors that mean "this machine
   cannot run the call at all" and nothing else. A provider error, a
   timeout, a plain ``OSError`` are all still the caller's to handle.
2. **The resume fallback does not retry a deterministic failure.** The
   exception reaches the caller, and exactly ONE dispatch was made.

The production incident this comes from: ``project_dir`` pointed at a
worktree that had been deleted, so ``Popen(cwd=...)`` raised
``FileNotFoundError``; the resume helper did not recognise it, fell
back to a fresh session, and dispatched the same prompt again — into
the same missing directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from coding_tool import (  # noqa: E402
    ApiError,
    ClaudeCodingTool,
    is_deterministic_local_error,
)


# ---------------------------------------------------------------------------
# 1. The classification
# ---------------------------------------------------------------------------


def test_deterministic_errors_are_recognised():
    """Exactly the three local system errors, and nothing else."""

    # The cwd is gone, the cwd is a file, the cwd cannot be entered.
    assert is_deterministic_local_error(
        FileNotFoundError("[Errno 2] No such file or directory: '/gone/wt'")
    )
    assert is_deterministic_local_error(
        NotADirectoryError("[Errno 20] Not a directory: '/gone/wt/file'")
    )
    assert is_deterministic_local_error(
        PermissionError("[Errno 13] Permission denied: '/gone/wt'")
    )

    # Everything else belongs to someone else's error handling: a
    # provider refusal, a timeout, a programming mistake, and the OS
    # errors that are none of the three above.
    assert not is_deterministic_local_error(RuntimeError("boom"))
    assert not is_deterministic_local_error(ValueError("boom"))
    assert not is_deterministic_local_error(OSError("something else went wrong"))
    assert not is_deterministic_local_error(ApiError("rate limited", status="429"))
    assert not is_deterministic_local_error(TimeoutError("slow"))


# ---------------------------------------------------------------------------
# 2. The resume fallback does not retry it
# ---------------------------------------------------------------------------


def test_resume_fallback_is_not_retried_for_missing_cwd(monkeypatch):
    """A missing cwd is raised, not answered with a second dispatch."""

    # A tool-shaped stub with no ``logger`` attribute at all: the
    # classification branch must survive a tool that was never
    # initialised, so it has to ask for the logger defensively.
    tool = ClaudeCodingTool.__new__(ClaudeCodingTool)
    assert not hasattr(tool, "logger"), "the stub must not carry a logger"

    dispatches = []

    def _spy(prompt, system_instruction, **kwargs):
        dispatches.append({"prompt": prompt, **kwargs})
        raise FileNotFoundError(
            "[Errno 2] No such file or directory: '/gone/wt'"
        )

    monkeypatch.setattr(tool, "_run_interactive_resilient", _spy)

    with pytest.raises(FileNotFoundError):
        tool._resume_or_fresh_session(
            prev_session_id="sid-prev",
            resume_prompt="resume prompt",
            fresh_prompt="fresh prompt",
            system_instruction=None,
            excluded_providers=None,
            total_timeout=None,
            allowed_tools=None,
            scene=None,
        )

    assert len(dispatches) == 1, (
        "the fallback dispatched the same prompt again; a missing "
        "directory fails the same way in a fresh session, so the "
        "second dispatch buys nothing and hides the real cause"
    )
    assert dispatches[0]["resume"] is True, (
        "the only dispatch was the resume attempt — the fresh-session "
        "fallback must not have been reached"
    )
