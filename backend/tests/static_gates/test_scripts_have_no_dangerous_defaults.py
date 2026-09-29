"""Static gate: ``scripts/`` has no dangerous defaults.

Why this gate exists
--------------------
The ``scripts/`` tree is first-party shell + Python that gets invoked
from CI (``.github/workflows/ci.yml``), from the developer's shell
(``scripts/run_tests.sh``), and from pre-commit hooks. Every script
in that tree is both an attack surface (whoever changes it owns the
shell that CI runs) and a gate source (the script's verdict becomes
the "all green" badge). The compiler-level checks for Python files
(ruff, mypy) do not cover shell defaults, and there was no static
gate scanning ``scripts/`` for them — until this file.

The rule
--------
A non-compliant default is one of the following, every shape
mechanically detectable by reading the source text:

  * ``.sh`` scripts whose ``set -e`` line is missing ``-u`` or
    ``-o pipefail`` (silent undefined-variable expansion and
    pipe-mask failures both stay hidden until the runtime hits a
    bad branch).
  * ``.sh`` / ``.py`` scripts that call ``eval`` (any caller-supplied
    prefix is one unquoted ``$VAR`` away from code execution).
  * ``.sh`` scripts whose lines contain an unquoted variable
    expansion (``$VAR``, ``${VAR}``, ``$N``) immediately after
    ``rm`` / ``source`` / a shell interpreter call (``bash`` / ``sh``
    / ``python`` / ``python3``).
  * ``.py`` scripts whose shebang is a path-baked Python interpreter
    (``#!/usr/bin/python3``); ``#!/usr/bin/env python3`` is the
    portable form that lets the operator's venv win on ``PATH``
    lookup. Hardcoding ``/usr/bin/python3`` bypasses venv selection
    and runs whatever default interpreter the system installs.
  * ``.sh`` scripts whose default pytest target resolves to a tree
    that contains zero ``test_*.py`` files (the suite always exits
    0; "all green" is a lie).

Why a mechanical gate and not review
------------------------------------
A review-time check is too late by the time the commit lands, and
``:set -e`` / ``:missing -u`` is the kind of one-line shape that
drifts: someone tightens a CI step without renaming the wrapper,
someone refactors a script and drops ``-o pipefail``, and the gate
stops catching anything. This file pins both the rule and the scan
target so that the check is identical on every machine that runs
the suite.

Boundary conditions covered
---------------------------
* Scan target empty (the excluded-dir set swallowed ``scripts/``,
  the directory was renamed) — ``test_scripts_are_scanned`` fails
  the moment that happens.
* Synthetic positive cases (``rm -rf $DIR``) — the unquoted-expansion
  rule fires on the exact shape the brief names.
* The default pytest target check is restricted to
  ``scripts/run_tests.sh`` (the file the brief identifies) rather
  than every script; the broader rule lives in the static scan above.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Locate the shared first-party source walker. ``backend/tests/static_gates``
# is two parents deep from ``backend/``, so the package is colocated.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan  # noqa: E402  (post-sys.path adjustment)


# ---------------------------------------------------------------------------
# Constants — single source of truth shared between tests
# ---------------------------------------------------------------------------

#: File extensions the gate considers scripts. ``.sh`` for shell,
#: ``.py`` for Python. Other extensions (``.yaml`` / ``.json`` / ``.md``)
#: belong to other gates (home-path, grep-guard, hardcoded-provider-id).
SCRIPT_SUFFIXES: frozenset[str] = frozenset({".sh", ".py"})

#: Test-file location is the source of truth for the project root.
#: ``backend/tests/static_gates/test_*.py`` is three parents deep
#: from the project root, so we resolve the root from
#: ``__file__`` instead of from ``os.getcwd()`` — pytest is often
#: launched with cwd=``backend/`` (the backend's local pytest.ini
#: lives there), and a cwd-relative ``scripts/`` there resolves to
#: ``backend/scripts/``, a different tree entirely.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The single root the gate owns. The brief pins the surface as
#: ``scripts/`` specifically — first-party hooks (``backend/``)
#: belong to other gates, and including them here would silently
#: widen the contract. The absolute path form keeps the walker
#: cwd-independent.
SCRIPTS_ROOT: tuple[Path, ...] = (_PROJECT_ROOT / "scripts",)

#: Interpreter invocations whose first unquoted argument travels
#: directly as ``argv[0]`` / ``argv[1]`` into the interpreter. When
#: that argument is ``$VAR`` and the variable contents happen to be
#: code, the script just ran attacker-controlled code. ``python3``
#: is the modern default; ``python`` is kept for older venvs.
_DANGEROUS_INTERPRETERS: tuple[str, ...] = (
    "rm",
    "source",
    "bash",
    "sh",
    "python",
    "python3",
)

#: ``set`` flags a non-trivial script must carry together. ``-e``
#: alone leaves undefined variables and pipe failures invisible;
#: documented together so a future flag (``-E`` for ERR trap, etc.)
#: can be added without rediscovering the trio.
_REQUIRED_SET_FLAGS: tuple[str, ...] = ("u", "o", "pipefail")


# ---------------------------------------------------------------------------
# File walker
# ---------------------------------------------------------------------------


def iter_script_sources(
    roots: tuple[Path, ...] = SCRIPTS_ROOT,
    excluded: frozenset[str] = source_scan.EXCLUDED_DIRS,
    suffixes: frozenset[str] = SCRIPT_SUFFIXES,
) -> Iterator[Path]:
    """Yield every ``.sh`` / ``.py`` file under ``scripts/``.

    Re-uses :func:`source_scan.iter_first_party_sources` for the walk
    so the same exclusions apply (``no .venv``, ``no __pycache__``,
    ``no plans``). Filters to the script suffixes so the gate stays
    tight to the surface it owns — adding ``.yaml`` here would pull
    in every workflow file, which belongs to a different gate.
    Defaults to the ``scripts/`` root regardless of the broader
    scan roots so a future change to ``source_scan.SCAN_ROOTS``
    (e.g. adding ``tools/``) does not silently widen this gate.
    """
    yield from source_scan.iter_first_party_sources(
        roots=roots, excluded=excluded, extensions=suffixes
    )


# ---------------------------------------------------------------------------
# Comment / string stripping — keep prose out of the rule scans
# ---------------------------------------------------------------------------


def _strip_unquoted_comment(line: str) -> str:
    """Return ``line`` truncated at the first unquoted ``#``.

    A ``#`` is *unquoted* when the count of ``'`` and ``"`` before it
    is even on each side. Naive (no escape-sequence awareness) but
    sufficient for the scan — the goal is to filter prose that the
    rules would otherwise false-positive on, not to model a real
    lexer. Used for both ``.sh`` and ``.py`` lines; the shell
    shebang (``#!/bin/bash``) is identified by the line-start check
    in the shebang rules rather than by this helper.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _strip_quoted_strings(line: str) -> str:
    """Replace every quoted substring with spaces (preserving length).

    Tracks single- and double-quote state so the rest of the line
    keeps its column positions; the scan rules use absolute column
    positions only indirectly (line numbers), so replacement with
    spaces is harmless. ``$VAR`` *inside* a quoted string is not an
    unquoted expansion at the caller site, so blanking the quoted
    span filters the rule out — otherwise an ``echo "python
    $SOMETHING"`` line would false-positive as ``unquoted_expansion
    _at_python``.

    Naive (no escape-sequence awareness): the shell would interpolate
    ``$VAR`` inside double quotes, but the *attacker-controlled
    code* shape requires the expansion to drive the next *command*
    token, which an entirely quoted span cannot.
    """
    result = list(line)
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
            result[i] = " "
        elif ch == '"' and not in_single:
            in_double = not in_double
            result[i] = " "
        elif (in_single or in_double) and ch != "\n":
            result[i] = " "
    return "".join(result)


def _strip_python_docstrings(text: str) -> str:
    r"""Blank Python triple-quoted strings to spaces (preserve lines).

    Module / function / class docstrings often mention a rule's
    trigger word in prose (this file's own docstring says
    ``scripts that call ``eval`` ...``). Without blanking
    docstrings the gate would self-flag the moment a doc contains
    the trigger word. Single-line / ordinary string literals are
    preserved so the scanner can still see hardcoded literal
    forms if any rule is later extended to inspect them.
    """
    lines = text.splitlines(keepends=True)
    result: list[list[str]] = [list(line) for line in lines]

    in_docstring = False
    docstring_quote = ""
    for lno, line in enumerate(lines):
        i = 0
        while i < len(line):
            if not in_docstring:
                if line[i] == "#":
                    # same-line comment after code: blank from `#`.
                    for j in range(i, len(line)):
                        if line[j] != "\n":
                            result[lno][j] = " "
                    break
                if line[i : i + 3] in ('"""', "'''"):
                    in_docstring = True
                    docstring_quote = line[i : i + 3]
                    for j in range(i, i + 3):
                        result[lno][j] = " "
                    i += 3
                    continue
                i += 1
            else:
                if line[i : i + 3] == docstring_quote:
                    for j in range(i, i + 3):
                        result[lno][j] = " "
                    in_docstring = False
                    docstring_quote = ""
                    i += 3
                    continue
                if line[i] != "\n":
                    result[lno][i] = " "
                i += 1

    return "".join("".join(row) for row in result)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

# ``set -<flags>`` line. Group 1 captures everything from the first
# flag to the first ``#`` or end-of-line — tokenising is then
# handled by :func:`_normalise_set_flags`. Anchored to start-of-line
# (with optional leading whitespace) so a variable named ``set``
# inside a function body is not pulled in. ``MULTILINE`` so each
# ``set`` line in the file is its own match.
_SET_LINE_RE = re.compile(
    r"^\s*set\s+([^\n#]+)",
    re.MULTILINE,
)

# Bare ``eval``. Requires a non-identifier char (or start of line)
# before ``eval`` so an identifier like ``myeval`` does not
# false-positive. The ``(`` is included so ``(eval ...)`` shells are
# caught too.
_EVAL_RE = re.compile(r"(?:^|(?<=[\s;&|(]))eval\b")

# ``curl ... | sh`` / ``| sudo sh`` — remote-payload-into-shell. The
# pipe-bound shell is the dangerous form; ``curl | bash`` is the
# typical variant.
_CURL_PIPE_SH_RE = re.compile(
    r"\bcurl\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"
)

# ``command [...args] $VAR`` where ``$VAR`` is an unquoted expansion.
# Group 1 = command; group 2 = the unquoted ``$VAR`` form. Stops at
# the first non-quoted shell metacharacter that would terminate the
# argument list (``" ' ; & |``).
_UNQUOTED_EXPANSION_RE = re.compile(
    r"\b(" + "|".join(_DANGEROUS_INTERPRETERS) + r")\b"
    r"(?:[ \t]+[^\s\"';&|]+)*[ \t]+"
    r"(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$[0-9@#])"
)

# Hardcoded Python interpreter shebang. Matches the path-baked
# form (``/usr/bin/python3``); does NOT match ``#!/usr/bin/env
# python3`` (the portable form), and does NOT fire for ``.sh``
# files (where ``/bin/bash`` is the conventional shebang and is
# portable across every supported shell host).
_PYTHON_HARDCODED_SHEBANG_RE = re.compile(
    r"^#!/usr/(?:bin|local/bin)/python"
)


# ---------------------------------------------------------------------------
# Public detection API
# ---------------------------------------------------------------------------


def find_dangerous_defaults(
    path: Path, text: str
) -> list[tuple[Path, int, str]]:
    """Return ``[(path, lineno, rule_name), ...]`` for every dangerous
    default in ``text``.

    Each rule is named after the shape that triggered it so a failing
    test is self-explanatory and a future reader does not have to
    re-derive the categorization from the regex. ``path`` is echoed
    back in every finding so tree-walking tests can format
    ``<rel>:L<line>: <rule>`` without re-walking.

    The function is pure: it does not touch the filesystem; callers
    read the file and pass the text in. The stripping helpers
    (comment-only for shell; docstring-aware for Python) are the
    only preprocessing — both are deliberately tolerant of quoting
    edge cases, and a finding that survives the strip is what the
    gate trusts.
    """
    findings: list[tuple[Path, int, str]] = []
    suffix = path.suffix
    raw_lines = text.splitlines()

    if suffix == ".sh":
        # Build executable-lines view for shell scripts: same line
        # numbers as ``raw_lines``, but with prose blanked and
        # quoted spans blanked so the rules see only real,
        # unquoted shell tokens.
        executable_lines = [
            _strip_quoted_strings(_strip_unquoted_comment(raw))
            for raw in raw_lines
        ]
        findings.extend(
            _find_shell_dangerous_defaults(
                path, text, raw_lines, executable_lines
            )
        )
    elif suffix == ".py":
        # Build the python executable view by blanking comments and
        # triple-quoted docstrings (the latter so a docstring that
        # mentions ``eval`` / ``hardcoded Python interpreter`` does
        # not self-flag the gate file itself).
        cleaned = _strip_python_docstrings(text)
        cleaned_lines = cleaned.splitlines()
        executable_lines: list[str] = []
        for raw, cln in zip(raw_lines, cleaned_lines):
            stripped = raw.lstrip()
            if stripped.startswith("#"):
                executable_lines.append("")
                continue
            # Re-strip inline comments on the cleaned text (the
            # docstring stripper already blanks the ``#`` chars;
            # trimming any trailing prose keeps column positions
            # tidy).
            executable_lines.append(_strip_unquoted_comment(cln))
        findings.extend(
            _find_python_dangerous_defaults(path, raw_lines, executable_lines)
        )

    return findings


def _normalise_set_flags(flags_blob: str) -> set[str]:
    """Expand a ``set -...`` flag list into a flat set.

    Bash allows short flags to be clustered (``-euo``) and long
    flags (``pipefail``) to appear as their own word. Tokenising on
    whitespace then flattening the short-flag clusters keeps the
    comparison against :data:`_REQUIRED_SET_FLAGS` independent of
    the exact form the script's author chose. A single ``-euo
    pipefail`` and a verbose ``-e -u -o pipefail`` end up with the
    same set.
    """
    flags: set[str] = set()
    for token in flags_blob.split():
        if token == "pipefail":
            flags.add("pipefail")
        elif token.startswith("-"):
            for ch in token[1:]:
                if ch.isalpha():
                    flags.add(ch)
    return flags


def _find_shell_dangerous_defaults(
    path: Path,
    text: str,
    raw_lines: list[str],
    executable_lines: list[str],
) -> list[tuple[Path, int, str]]:
    """Shell-script portion of :func:`find_dangerous_defaults`."""
    findings: list[tuple[Path, int, str]] = []

    # Rule: ``set -euo pipefail`` or stronger. A ``set -e`` line
    # missing ``-u`` or ``pipefail`` flags the issue. The flags can
    # appear in any order; a ``set`` line with no flags at all
    # falls outside this rule (a two-line ``rm`` script doesn't
    # need it).
    for match in _SET_LINE_RE.finditer(text):
        flags_blob = match.group(1)
        flat = _normalise_set_flags(flags_blob)
        missing = [flag for flag in _REQUIRED_SET_FLAGS if flag not in flat]
        if missing:
            lineno = text[: match.start()].count("\n") + 1
            findings.append(
                (path, lineno, "set_e_missing_" + "_".join(missing))
            )

    # Rule: bare ``eval``. Every occurrence is reported (the rule
    # is per-occurrence, not per-line — a multi-evil line should
    # show up distinctly).
    for lineno, line in enumerate(executable_lines, start=1):
        if _EVAL_RE.search(line):
            findings.append((path, lineno, "eval_used"))

    # Rule: ``curl | sh`` / ``| sudo sh``. The remote payload runs
    # in the operator's context without review; nothing else in
    # the scripts tree needs this pattern.
    for lineno, line in enumerate(executable_lines, start=1):
        if _CURL_PIPE_SH_RE.search(line):
            findings.append((path, lineno, "curl_pipe_sh"))

    # Rule: unquoted expansion at dangerous position.
    for lineno, line in enumerate(executable_lines, start=1):
        for m in _UNQUOTED_EXPANSION_RE.finditer(line):
            findings.append(
                (path, lineno, f"unquoted_expansion_at_{m.group(1)}")
            )

    return findings


def _find_python_dangerous_defaults(
    path: Path,
    raw_lines: list[str],
    executable_lines: list[str],
) -> list[tuple[Path, int, str]]:
    """Python-script portion of :func:`find_dangerous_defaults`."""
    findings: list[tuple[Path, int, str]] = []

    # Rule: hardcoded Python interpreter shebang at line 1. The
    # portable form is ``#!/usr/bin/env python3`` so the operator's
    # venv wins on ``PATH`` lookup; ``/usr/bin/python3`` is the
    # system interpreter, which on the runner is the bare
    # Python (no venv) — pytest collection fails under that.
    if raw_lines and _PYTHON_HARDCODED_SHEBANG_RE.match(raw_lines[0]):
        findings.append((path, 1, "hardcoded_python_interpreter"))

    # Rule: ``eval(`` invocation. ``eval("literal")`` is safe,
    # ``eval(user_input)`` is a remote-code-execution primitive;
    # every call site is conservatively flagged so the gate is
    # identical across the repo.
    for lineno, line in enumerate(executable_lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("eval(") or " eval(" in stripped:
            findings.append((path, lineno, "eval_used"))

    return findings


# ---------------------------------------------------------------------------
# Default-target helpers (targeted at ``scripts/run_tests.sh``)
# ---------------------------------------------------------------------------

#: The two shapes ``scripts/run_tests.sh`` has carried for "which target
#: does pytest get". The gate accepts both so a legitimate refactor does
#: not have to touch it, while an *unrecognised* refactor still fails
#: loudly (see :func:`_pytest_targets`).
#:
#: ``targets=("$PROJECT_ROOT/backend/tests")`` — the default-assignment
#: form introduced 2026-09-27, which also applies when flags are passed.
_TARGET_ASSIGNMENT_RE = re.compile(
    r"""targets=\(            # the assignment
        \s*['\"]?            # optional opening quote
        (\$\{?PROJECT_ROOT\}?/[^\s'")]+)   # a literal project-relative path
        ['\"]?\s*\)""",
    re.VERBOSE,
)

#: ``pytest "$PROJECT_ROOT/tests"`` — a direct call with a literal path.
_TARGET_CALL_RE = re.compile(
    r"""pytest\s+             # the pytest command
        ['\"]?               # optional opening quote
        (\$\{?PROJECT_ROOT\}?/[^\s'")]+)   # a literal project-relative path
        ['\"]?""",
    re.VERBOSE,
)


def _pytest_targets(text: str) -> list[str]:
    """Every literal ``$PROJECT_ROOT/...`` path ``text`` can hand to pytest.

    Collected from both the default assignment and any direct call, so
    the gate sees the target regardless of which shape the script uses.
    Returns an empty list when the script matches *neither* shape — the
    caller treats that as a failure rather than a pass, so a refactor
    the gate does not understand cannot silently make it vacuous.
    """
    return (
        _TARGET_ASSIGNMENT_RE.findall(text)
        + _TARGET_CALL_RE.findall(text)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scripts_are_scanned() -> None:
    """The scan target must produce at least one ``.sh`` / ``.py`` file.

    A gate whose scan target has disappeared — the excluded-dir set
    swallowed ``scripts/``, the directory was renamed, the gate
    itself was moved out of the project tree — passes vacuously.
    This test fails the moment that happens so the regression is
    visible.
    """
    files = list(iter_script_sources())
    assert files, (
        "iter_script_sources() returned no files — the gate would "
        "pass on an empty result. Either SCAN_ROOTS in source_scan.py "
        "no longer contains scripts/, the EXCLUDED_DIRS set has "
        "swallowed scripts/, or the script suffix set has drifted "
        "away from the real shell / Python surface."
    )


def test_no_dangerous_defaults_in_scripts() -> None:
    """Full-tree scan: zero dangerous defaults across ``scripts/``.

    Every ``.sh`` / ``.py`` file under the shared first-party
    source roots is walked; each is fed through
    :func:`find_dangerous_defaults`; the resulting findings must
    be empty.
    """
    offenders: list[str] = []
    for path in iter_script_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = path.relative_to(_PROJECT_ROOT)
        except ValueError:
            rel = path
        for _, lineno, rule in find_dangerous_defaults(path, text):
            offenders.append(f"{rel}:L{lineno}: {rule}")

    assert not offenders, (
        "scripts/ contains a dangerous default. Each rule is "
        "documented in find_dangerous_defaults' docstring; the fix "
        "is mechanical (add the missing flag, quote the expansion, "
        "or replace the hardcoded shebang with `#!/usr/bin/env "
        "<interpreter>`).\n  " + "\n  ".join(offenders)
    )


def test_run_tests_default_target_is_not_empty() -> None:
    """``scripts/run_tests.sh``'s pytest targets must not include the
    empty ``tests/`` directory at the project root.

    That directory contains only ``fixtures/``; running with no
    arguments collects 0 tests and exits 0 regardless of any
    regression in ``backend/tests/``.

    The gate checks *every* literal target the script declares, not
    just the no-argument branch: since 2026-09-27 the default applies
    whether or not flags are passed, so a regression could now hide in
    either shape. An unrecognised refactor fails on the empty result
    rather than passing vacuously.
    """
    run_tests = Path("scripts/run_tests.sh")
    full = Path(__file__).resolve().parents[3] / run_tests
    text = full.read_text(encoding="utf-8")

    targets = _pytest_targets(text)
    assert targets, (
        f"{run_tests} declares no literal pytest target in any shape "
        "this gate recognises (neither a ``targets=(...)`` assignment "
        "nor a ``pytest \"$PROJECT_ROOT/...\"`` call). The file has been "
        "refactored away from the contract this gate expects — update "
        "_pytest_targets to the new shape rather than deleting the gate; "
        "its whole job is to stop the empty ``tests/`` directory coming "
        "back as a default."
    )

    empty_dir = re.compile(r"\$\{?PROJECT_ROOT\}?/tests$")
    offenders = [t for t in targets if empty_dir.match(t)]
    assert not offenders, (
        f"{run_tests} hands the empty ``$PROJECT_ROOT/tests`` directory "
        f"to pytest (as {offenders!r}). That directory currently contains "
        "only fixtures/, so running it collects 0 tests and always exits "
        "0. Point the target at $PROJECT_ROOT/backend/tests (or another "
        "directory that contains test_*.py files)."
    )


def test_unquoted_expansion_is_detected() -> None:
    """Synthetic ``rm -rf $DIR`` (no quotes) is flagged.

    The test fixture text is assembled from a literal that has
    been split into pieces so this test file's own source does not
    carry the same shape it asserts on (a self-referential scan
    would either suppress the rule for this file or fail on its
    own definition).
    """
    fixture = "rm -" + "rf $DIR\n"
    findings = find_dangerous_defaults(Path("scripts/x.sh"), fixture)
    assert any(
        rule == "unquoted_expansion_at_rm" for _, _, rule in findings
    ), (
        "expected an `unquoted_expansion_at_rm` finding for `rm -rf "
        "$DIR` (no quotes); the rule is documented in the module "
        "docstring. Got: " + ", ".join(r for _, _, r in findings)
    )
