"""No first-party source file may carry a local-machine home path.

Why this gate exists
--------------------
The project rule (see ``CLAUDE.md``, "Code must not carry information
unrelated to this project") forbids absolute home-directory paths that
identify a specific developer or machine. The rule was originally
meant to be enforced by review. It was not.

The first export from the private development repo carried ~340
references to unrelated private checkouts into ``backend/`` — in
comments, docstrings, test names, and one hard-coded list in
``backend/prd_generator.py`` that switched scan behaviour on three
private directory names. None of it was a credential, so no secret
scanner would have caught it; all of it was visible to anyone who
read the source.

A review-time check is too late by the time the commit lands, and
``grep -rE "/(Users|home)/[A-Za-z0-9._-]+"`` is the kind of one-liner
that drifts: someone "fixes" it with a narrower regex, an exclusion
list grows to silence it, and the gate stops catching anything.
This file pins both the shape and the exemption set so that the
check is identical on every machine that runs the suite.

The rule
--------
Every match of ``/Users/<segment>/...`` or ``/home/<segment>/...``
(plus the Windows drive-letter form ``/C:/Users/<segment>/...``)
whose ``<segment>`` is *not* in ``PLACEHOLDER_SEGMENTS`` is a
violation. Placeholders are documented rather than guessed: any
segment that *names* a person — yours, a teammate's, a project
maintainer's — is forbidden. ``me``, ``user``, ``example`` and friends
are placeholders because that is how every docstring and example in
this codebase already refers to such paths.

Why a *gate* and not just the rule
----------------------------------
A regex is cheap; what is not cheap is keeping the allowlist honest
and keeping the scan target honest. ``test_scan_is_non_empty`` is
the second half: a gate whose target has drifted away (the
excluded-dir set accidentally swallowed every scanned file, the
repository was reorganised so the scan root no longer exists)
silently passes. That is how the original defect shipped — the
paths were there, the check was not running, and the export passed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Locate the static_gates module shared with sibling gates; the
# first-party source scan it exposes is what the test uses to build
# the file list.
import source_scan

# ---------------------------------------------------------------------------
# Recognised home-path shapes
# ---------------------------------------------------------------------------

#: Unix, macOS, and Windows drive-letter home prefixes. The regex
#: anchors on the leading slash and the ``Users`` or ``home``
#: literal, so ``/home/<x>/users_home`` (already inside this repo's
#: source tree) does not match. The drive-letter form
#: ``/C:/Users/foo`` — how pip parses a Windows path on a Unix host,
#: and how users paste Windows file URIs back into terminal output
#: — contains the bare ``/Users/<seg>/`` substring as a suffix and
#: is therefore covered by the same pattern, no second regex needed.
_HOME_PATH_RE = re.compile(
    r"/(?:Users|home)/([A-Za-z0-9._-]+)/"
)

#: Home-path segments that do **not** identify a real machine. The
#: set is documented rather than generated because every entry has
#: a real example in the existing source tree, and an entry here is
#: a deliberate exemption: ``me`` is how a per-tenant command example
#: names its user's home, ``someone`` is the standard indefinite
#: pronoun in API docs, and so on. Adding a name here means
#: "this string is a placeholder everywhere in the codebase".
#: ``...`` (three dots) is the standard ellipsis convention used in
#: docstrings to abbreviate an arbitrary path component — ``/Users/.../plans``
#: reads as "wherever the operator's plans directory happens to live"
#: rather than as a directory literally named ``...``.
PLACEHOLDER_SEGMENTS: frozenset[str] = frozenset({
    "a",
    "b",
    "x",
    "y",
    "u",
    "me",
    "user",
    "username",
    "someone",
    "whoami",
    "name",
    "example",
    "your-name",
    "...",
})


def find_home_paths(path: Path, text: str) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, segment)`` for every non-placeholder
    home-path occurrence in ``text``.

    ``path`` is the file the snippet came from and is echoed back in
    every finding so a downstream gate can format
    ``rel:lineno: <snippet>`` without re-walking the tree. A
    placeholder segment is suppressed — see
    :data:`PLACEHOLDER_SEGMENTS`. Empty string and segments that are
    purely digits are not placeholders by default; the allowlist is
    deliberate.
    """
    hits: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _HOME_PATH_RE.finditer(line):
            segment = match.group(1)
            if segment in PLACEHOLDER_SEGMENTS:
                continue
            hits.append((path, lineno, segment))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_is_non_empty() -> None:
    """The scan target must produce at least one file.

    A gate whose scan target has disappeared — the excluded-dir set
    swallowed the tree, the repo was reorganised — passes vacuously.
    This test fails the moment that happens so the regression is
    visible.
    """
    files = list(source_scan.iter_first_party_sources())
    assert files, (
        "source_scan.iter_first_party_sources() returned no files — the "
        "gate would now pass on an empty result. Either the SCAN_ROOTS, "
        "EXCLUDED_DIRS, or the file extension filter has drifted away "
        "from the real first-party tree."
    )


def test_no_non_placeholder_home_path() -> None:
    """Full-tree scan: zero non-placeholder home-path occurrences."""
    offenders: list[str] = []
    for path in source_scan.iter_first_party_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _, lineno, segment in find_home_paths(path, text):
            # ``repo_relative`` renders the offender the way a reader
            # thinks about it (``backend/server.py``) regardless of how the
            # walker reached it; the walker yields absolute paths, so
            # formatting ``path`` directly would print the runner's whole
            # checkout prefix into the failure message.
            rel = source_scan.repo_relative(path)
            offenders.append(f"{rel}:{lineno}: /Users/{segment}/...")

    assert not offenders, (
        "First-party source contains a local-machine home path. "
        "Strip the path, replace it with a placeholder from "
        "PLACEHOLDER_SEGMENTS (e.g. /Users/me/...), or move the value "
        "into a runtime config / env var. The /Users/<segment>/... "
        "shape identifies one developer's machine and must not ship "
        "in the public repository.\n  " + "\n  ".join(offenders)
    )


def test_placeholder_home_path_is_allowed() -> None:
    """``/Users/me/proj`` is a placeholder and is **not** a violation."""
    text = 'p = "/Users/me/proj/.venv/bin/pytest"\n'
    assert find_home_paths(Path("dummy.py"), text) == []


def test_real_home_path_is_flagged() -> None:
    """A non-placeholder home-path segment is reported as a violation.

    The test fixture text is assembled from string fragments so this
    test file's own source does not contain the literal regex match
    the gate forbids. A self-referential scan would either suppress
    the rule for this file (the gate stops catching its own authors)
    or fail the suite on the gate's own definition; the third option
    is to build the fixture text without ever writing it whole.
    """
    text = 'cwd = "/Use' + 'rs/kai/work/repo"\n'
    assert find_home_paths(Path("dummy.py"), text) == [
        (Path("dummy.py"), 1, "kai")
    ]