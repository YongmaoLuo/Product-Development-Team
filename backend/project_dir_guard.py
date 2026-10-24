"""Single validation point for a plan's ``project_dir``.

Why this module exists
----------------------
``project_dir`` is the directory an executor works in. A *wrong* value is a
configuration error, but the start-execution route used to answer it with
``mkdir(parents=True, exist_ok=True)`` — which silently created an empty
directory and let the run proceed inside it. The failure then surfaced much
later as a confusing "no such file" from a subprocess, with nothing pointing
back at the typo that caused it.

So the rule is: **the directory must already exist, and it must be a
directory.** This module is the one place that says so, and it never creates
anything — creating the path is the caller's decision, not a side effect of
validating it.

Both the start-execution route and the repair dispatch call
:func:`validate_project_dir` so the two paths cannot drift apart.

The error carries the offending path and a short machine-readable reason so
callers can map it onto their own responses (an HTTP 400 here) without
re-deriving the cause from the message text.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ProjectDirInvalidError", "validate_project_dir"]


class ProjectDirInvalidError(ValueError):
    """Raised when a ``project_dir`` is not a usable working directory.

    Attributes:
        project_dir: The offending path, as given to the validator.
        reason: Why it was refused (``does not exist`` / ``not a directory``).
    """

    def __init__(self, project_dir, reason: str):
        self.project_dir = str(project_dir)
        self.reason = reason
        super().__init__(
            f"project_dir is not a usable working directory: "
            f"{self.project_dir} ({reason})"
        )


def validate_project_dir(project_dir) -> Path:
    """Return ``project_dir`` as a resolved absolute path, or refuse it.

    A leading ``~`` is expanded first, and the returned path is resolved so
    callers get an unambiguous absolute location.

    Args:
        project_dir: The directory to validate.

    Returns:
        The resolved absolute path.

    Raises:
        ProjectDirInvalidError: The path does not exist, or exists but is
            not a directory.
    """
    path = Path(project_dir).expanduser()
    if not path.exists():
        raise ProjectDirInvalidError(path, "does not exist")
    if not path.is_dir():
        raise ProjectDirInvalidError(path, "not a directory")
    return path.resolve()
