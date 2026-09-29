"""Strict string-level safety check for single-segment directory names.

The contract is intentionally narrow: a safe name is a non-empty string that
contains no whitespace-only content, no path separators, no parent-directory
tokens, and no NUL byte. This function performs no filesystem access and
performs no normalization (e.g. it does NOT trim surrounding whitespace).
"""

from __future__ import annotations


def is_safe_dir_name(path: object) -> bool:
    """Return True iff *path* is a single-segment, non-empty, non-whitespace
    directory name that contains no path-separator, parent-traversal token,
    or NUL byte.

    Any non-``str`` input (including ``None``, ``int``, ``bytes``,
    ``os.PathLike``) is rejected with ``False`` rather than raising
    ``TypeError``.
    """
    # Reject any non-string input without raising TypeError.
    if not isinstance(path, str):
        return False

    # Reject empty string and pure-whitespace strings (incl. tab / newline).
    if path == "" or path.isspace():
        return False

    # Reject any character that can be used to escape the single-segment
    # contract: NUL byte, forward slash, or backslash.
    if "\x00" in path or "/" in path or "\\" in path:
        return False

    # Reject parent-directory traversal tokens (any ".." segment anywhere).
    if path == ".." or ".." in path.split("/") or ".." in path.split("\\"):
        return False

    return True
