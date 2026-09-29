"""Credential-bearing temp files must be written private (2026-09-27).

The defect this pins
--------------------
The subagent ``--settings`` payload carries the routed provider's
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``. It was written to a flat
``/tmp/subagent_settings_<uuid>.json`` with the default file mode and was
never removed: ``/tmp`` is ``1777``, so the sticky bit stopped other local
accounts *deleting* those files but not *reading* them, and a ``0644`` file
is readable by every account on the box. Nothing bounded the population —
every dispatch added one. The name is discoverable too — it is in the
process table, and it is handed to the subagent itself as
``CLAUDE_SETTINGS_PATH``.

Why a static gate and not only the unit tests
---------------------------------------------
``test_secret_files.py`` proves the *helper* is private;
``test_subagent_config.py`` and ``test_coding_tool_secret_redaction.py``
prove the two current writers use it. Neither notices a **new** writer
that goes back to a literal ``/tmp/<something>settings.json``. This gate
scans the source for that shape, so reintroducing it fails here even if
the new code path has no test of its own.

Scope, stated honestly: the scan keys on a literal ``/tmp/`` path whose
name contains ``settings``. A future writer that picks a different name
slips past it — which is why the second test pins the *mechanism*
(the writers import ``utils.secret_files``) rather than only the shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ``static_gates/`` is on sys.path for this directory's modules (see the
# sibling gates, which import ``source_scan`` the same way).
import source_scan


#: A string literal that is a flat ``/tmp`` path naming a settings file.
_FLAT_TMP_SETTINGS_RE = re.compile(
    r"""["']/tmp/[^"']*settings[^"']*\.json["']""",
    re.IGNORECASE,
)


def find_flat_tmp_settings_paths(path: Path, text: str) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, literal)`` for every flat-/tmp settings path.

    Line numbers are 1-based so the failure message can be pasted into an
    editor.
    """
    found: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _FLAT_TMP_SETTINGS_RE.finditer(line):
            found.append((path, lineno, match.group(0)))
    return found


#: Modules that write a settings file into the temp root. Every one of
#: them must route through :mod:`utils.secret_files` for the directory
#: mode, the file mode and the redaction helper.
#: Repository-root-relative names of the modules that may write a settings
#: file. Kept as relative names because that is how they read in a failure
#: message and how a reviewer thinks about them; resolved against
#: ``source_scan.REPO_ROOT`` at the point of use. Written as bare
#: ``Path(...)`` they were resolved against the *working directory*, so
#: under CI's ``working-directory: backend`` every one of them reported
#: "module is gone" while sitting right there on disk.
_SETTINGS_WRITERS = (
    Path("backend/coding_tool.py"),
    Path("backend/subagent_config.py"),
    Path("backend/verification_subagent.py"),
)


def test_scan_is_non_empty() -> None:
    """A gate that scans nothing passes vacuously.

    The walker now resolves its roots against ``source_scan.REPO_ROOT``,
    so the answer no longer moves with the working directory (it used to,
    and this docstring used to say so). What can still empty the scan is a
    filter that has drifted: an excluded-directory name that swallows the
    tree, an extension list that matches nothing. An empty walk is
    silent without this assertion, which is why it stays.
    """
    files = list(source_scan.iter_first_party_sources())
    assert files, (
        "iter_first_party_sources() returned no files — the gate would "
        "pass on an empty result. Check that SCAN_ROOTS / EXCLUDED_DIRS / "
        "SOURCE_EXTENSIONS still describe the real first-party tree."
    )
    production = _production_sources()
    assert production, (
        "the production filter matched nothing, so "
        "test_no_settings_tmpfile_is_built_from_a_flat_tmp_literal would "
        "pass vacuously. Check _is_production()."
    )


def test_production_filter_excludes_tests_and_keeps_source() -> None:
    """``_is_production`` is the one line that decides what gets scanned.

    It is pinned because getting it wrong is silent in one direction: too
    narrow and the gate stops looking at the code it exists to protect.
    """
    assert _is_production(Path("backend/coding_tool.py"))
    assert _is_production(Path("backend/utils/secret_files.py"))
    assert not _is_production(Path("backend/tests/unit/test_secret_files.py"))
    assert not _is_production(Path("backend/tests/test_server.py"))
    assert not _is_production(Path("backend/tests/conftest.py"))
    # A production module whose *name* merely starts with "test_".
    assert not _is_production(Path("backend/test_helpers.py"))


def _is_production(path: Path) -> bool:
    """True for first-party **production** source, not test code.

    Test fixtures legitimately name ``/tmp/fake_settings.json`` to point
    a mock at something; they are not writers that ship. Only the code
    that runs in the server can leak a credential, so the scan is scoped
    to it — and this predicate is itself covered by a test below, because
    a typo here would silently return "everything passes".
    """
    return "tests" not in path.parts and not path.name.startswith("test_")


def _production_sources() -> list[Path]:
    return [p for p in source_scan.iter_first_party_sources() if _is_production(p)]


def test_no_settings_tmpfile_is_built_from_a_flat_tmp_literal() -> None:
    """No production module may build a settings path under flat ``/tmp``.

    The path has to come from ``private_dir()`` so it lands in a ``0700``
    directory; a literal ``/tmp/...`` is the exact shape that leaked.
    """
    offenders: list[str] = []
    for path in _production_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _, lineno, literal in find_flat_tmp_settings_paths(path, text):
            offenders.append(f"{path}:L{lineno}: {literal}")

    assert not offenders, (
        "a settings file is built from a literal path under /tmp. Those "
        "files carry provider credentials and /tmp is mode 1777 — the "
        "sticky bit stops other accounts deleting them, not reading them. "
        "Use utils.secret_files.private_dir() for the directory and "
        "write_private_json() for the file:\n  " + "\n  ".join(offenders)
    )


def test_settings_writers_route_through_secret_files() -> None:
    """Pin the mechanism, not just the shape.

    The regex above keys on the name ``settings``; a future writer could
    dodge it by naming the file something else. This test does not care
    what the file is called — it asserts that every module in the
    settings-writing set actually uses the private-directory helper.
    """
    missing: list[str] = []
    for rel in _SETTINGS_WRITERS:
        path = source_scan.REPO_ROOT / rel
        if not path.exists():  # pragma: no cover - module moved or renamed
            missing.append(f"{rel} (module is gone — update _SETTINGS_WRITERS)")
            continue
        text = path.read_text(encoding="utf-8")
        for needed in ("utils.secret_files", "private_dir"):
            if needed not in text:
                missing.append(f"{rel} does not reference {needed!r}")

    assert not missing, (
        "every module that writes a settings file into the temp root must "
        "go through utils.secret_files (0700 directory, 0600 file, "
        "redaction):\n  " + "\n  ".join(missing)
    )


# ---------------------------------------------------------------------------
# Sensitivity: the scanner must flag the shape it exists to catch, and
# must not flag the innocuous neighbours.
# ---------------------------------------------------------------------------


def test_scanner_flags_the_exact_shape_that_leaked() -> None:
    leaked = 'path = Path(f"/tmp/subagent_settings_{file_uuid}.json")'
    found = find_flat_tmp_settings_paths(Path("x.py"), leaked)
    assert found, (
        "the scanner no longer matches the literal that caused the "
        "2026-09-27 leak; the gate is now decorative"
    )
    assert found[0][1] == 1
    assert "/tmp/subagent_settings_" in found[0][2]


def test_scanner_flags_a_renamed_settings_file_too() -> None:
    """A different name under /tmp is still the same defect."""
    assert find_flat_tmp_settings_paths(
        Path("x.py"), 'p = "/tmp/whatever_settings_v2.json"'
    )


@pytest.mark.parametrize(
    "line",
    [
        # The private-directory form the fix uses.
        'path = private_dir() / f"subagent_settings_{u}.json"',
        # Flat /tmp paths that are NOT settings files: these are
        # legitimate (progress logs, the lock broker socket, test
        # fixtures) and must not be swept up.
        'output_file = f"/tmp/vp_{vp_id}_progress.log"',
        'sock = tempfile.gettempdir() + "/pdt-lock-broker.sock"',
        'tmp = Path("/tmp")',
    ],
)
def test_scanner_leaves_innocuous_paths_alone(line: str) -> None:
    assert not find_flat_tmp_settings_paths(Path("x.py"), line), (
        f"false positive on {line!r}; the gate would be noisy enough to "
        f"get deleted"
    )
