"""Shared rules for reasoning about a task's ``test_command``.

Why this exists
---------------
Two callers need the same three answers and neither of them can work
them out alone:

  * the **refiner**, deciding whether a failed task is still worth
    keeping and what the replacement should touch;
  * the **repair generator**, writing the replacement.

All three questions were being answered twice — once on each side —
and the two copies drifted:

1. *Which files did the failed attempt touch?*  Answered from the
   failure's stack on one side and from the diff on the other, so a
   repair was handed a file list that named the failing test but not
   the file the failure actually came from (or the other way round).
2. *Does the target command collect any test at all?*  Answered by
   running ``pytest --collect-only`` and reading the count, with the
   process handling written into the middle of the rule.
3. *Is this test a vacuous shell?*  Answered by eye on one side and by
   a different standard on the other, so a repair whose only test was
   ``assert True`` counted as a working repair on one side and not on
   the other.

The rules therefore live here, once, and both sides call them.

What "pure" means here
----------------------
Every function in this module takes its inputs as arguments and
returns an answer.  The one impure thing in the neighbourhood — the
process that actually runs ``pytest --collect-only`` — is injected as
a ``runner`` callable by the caller
(:func:`collect_only_probe`).  That has two consequences worth the
injection:

  * the rule is exercisable without starting a process, so its
    behaviour can be pinned by a unit test instead of by a slow one;
  * the caller keeps ownership of timeouts, sandboxing, working
    directory and output parsing, which is where a ``pytest`` run
    actually goes wrong.

The three boundaries
--------------------
**No stack and no diff → no files.**  :func:`derive_files_to_modify`
never invents a path.  ``files_to_modify`` is a hard gate downstream
(``backend/task.py`` rejects an empty list), so a plausible-looking
wrong path is worse than an empty list: it does not merely fail to
help, it sends the repair at a file the failure never touched.  When
there is nothing to derive, the answer is the empty list and the
caller decides what to do about it.

**``collected == 0`` is not an answer.**  :func:`collect_only_probe`
reports the count the runner measured, including a zero.  Zero
collected means the runner could not tell — the target does not exist,
the module fails to import, the command is not a pytest command at
all — and the caller decides whether to treat that as "undecidable"
or as something else.  The rule does not quietly upgrade a zero into
"these tests do not exist" or "these tests are vacuous"; both of
those are conclusions, and conclusions are the caller's job.

**A shell test asserts nothing.**  :func:`is_empty_shell_test` asks
whether the test can fail for a reason that has anything to do with
the code under test.  ``assert True``, ``pass`` and
``pytest.skip(...)`` cannot, so they are shells.  A test that calls
``assertEqual``, compares a computed value, or wraps a call in
``pytest.raises`` can, so it is not.

Paths
-----
The path syntax recognised here is POSIX-shaped and relative
(``tests/a.py``).  An absolute path is reported as it was found: the
rule has no project root to make it relative to, and inventing one
would be a guess.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from typing import Any, Callable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "collect_only_probe",
    "derive_files_to_modify",
    "is_empty_shell_test",
]


# ---------------------------------------------------------------------------
# derive_files_to_modify
# ---------------------------------------------------------------------------

#: Keys of a failure record that hold a text blob a file path can be
#: read out of.  ``stack`` is the one the failure record always has;
#: the others are the aliases a different producer might use for the
#: same thing.
_FAILURE_TEXT_KEYS: Tuple[str, ...] = (
    "stack",
    "traceback",
    "stack_trace",
    "stderr",
    "stdout",
    "output",
)

#: Keys of a failure record that name the failing file outright.
_FAILURE_PATH_KEYS: Tuple[str, ...] = ("file", "filename", "file_path", "path")

#: Suffixes a path must carry to count as a source file.  A stack
#: trace is prose with a path in it, and prose is full of dotted words
#: (``self.assertEqual``, ``example.com``, ``0.5``) that would
#: otherwise be mined for something that looks like a file name.
_SOURCE_SUFFIXES = frozenset(
    {
        # python
        ".py", ".pyi", ".pyx",
        # javascript / typescript
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
        # systems
        ".rs", ".go", ".java", ".kt", ".kts", ".scala", ".c", ".h", ".cc",
        ".cpp", ".cxx", ".hpp", ".hh", ".m", ".mm", ".swift", ".rb", ".php",
        ".cs", ".fs", ".fsx", ".dart", ".lua", ".pl", ".pm", ".r", ".jl",
        ".ex", ".exs", ".erl", ".hs", ".ml", ".clj", ".zig",
        # shell and config
        ".sh", ".bash", ".zsh", ".sql", ".css", ".scss", ".less", ".html",
        ".htm", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".md", ".txt", ".proto", ".tf", ".gradle",
    }
)

#: A run of characters that can be part of a path.  ``:`` is excluded on
#: purpose — it is what separates a path from a line number
#: (``tests/a.py:3``) and a node id from its test name
#: (``tests/a.py::test_x``), so excluding it splits both correctly.
_PATH_RUN_RE = re.compile(r"[A-Za-z0-9_.\-/\\]+")

#: Punctuation that can sit in front of a path in prose, in a diff header
#: or in a hand-written failure record.  A leading "." is deliberately
#: not in it: "./" is another way of writing the same path, and a name
#: that starts with a dot is dropped by the suffix check anyway.
_PATH_LEADING_NOISE = "\"'`([{)]},:; "

#: Punctuation that can sit after a path.  A trailing "." belongs to the
#: sentence around the path, not to the path.
_PATH_TRAILING_NOISE = "\"'`)]},:;. "


def derive_files_to_modify(failure: Any, diff: Any) -> List[str]:
    """The files a failed attempt most likely touched.

    Two independent sources, merged and de-duplicated, first source
    first:

    * ``failure`` — the failure record.  Its text fields (the stack
      above all) are scanned for source-file paths, and a field that
      names the failing file outright is taken as it stands.
    * ``diff`` — the paths the failed attempt changed.  A plain list of
      paths, or a diff body, whose file headers are read.

    The result keeps the order the sources were read in, and a path
    named by both sources appears once.

    Returns an empty list when neither source names a file.  No path is
    ever invented: the caller is better placed to decide what an empty
    answer means than this rule is.
    """
    candidates: List[str] = []
    candidates.extend(_paths_from_failure(failure))
    candidates.extend(_paths_from_diff(diff))

    ordered: List[str] = []
    seen: Set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def _paths_from_failure(failure: Any) -> List[str]:
    """The paths named by a failure record."""
    if failure is None:
        return []
    if isinstance(failure, str):
        return _paths_from_text(failure)
    if not isinstance(failure, Mapping):
        return []

    paths: List[str] = []
    for key in _FAILURE_PATH_KEYS:
        value = failure.get(key)
        if not isinstance(value, str):
            continue
        candidate = _normalise_path(value)
        if candidate is not None:
            paths.append(candidate)

    for key in _FAILURE_TEXT_KEYS:
        text = _as_text(failure.get(key))
        if not text:
            continue
        paths.extend(_paths_from_text(text))
    return paths


def _paths_from_text(text: str) -> List[str]:
    """The source-file paths mentioned in a blob of text."""
    paths: List[str] = []
    for match in _PATH_RUN_RE.finditer(text):
        candidate = _normalise_path(match.group(0))
        if candidate is not None:
            paths.append(candidate)
    return paths


def _paths_from_diff(diff: Any) -> List[str]:
    """The paths named by a diff — a list of paths, or a diff body."""
    paths: List[str] = []
    for entry in _string_entries(diff):
        text = entry.strip()
        if not text:
            continue
        if "\n" in text:
            paths.extend(_paths_from_diff_text(text))
            continue
        candidate = _normalise_path(text)
        if candidate is not None:
            paths.append(candidate)
    return paths


def _paths_from_diff_text(text: str) -> List[str]:
    """The paths named by the file headers of a diff body."""
    paths: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("diff --git "):
            parts = stripped.split()
            if len(parts) >= 4:
                paths.append(_strip_diff_prefix(parts[-1]))
            continue
        if stripped.startswith(("+++ ", "--- ")):
            paths.append(_strip_diff_prefix(stripped[4:].split("\t", 1)[0].strip()))
            continue
        if stripped.startswith("rename to "):
            paths.append(_strip_diff_prefix(stripped[len("rename to "):].strip()))
    return [
        candidate
        for candidate in (_normalise_path(path) for path in paths)
        if candidate is not None
    ]


def _strip_diff_prefix(path: str) -> str:
    """Drop the ``a/`` / ``b/`` prefix git puts on a diff header path."""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _normalise_path(raw: str) -> Optional[str]:
    """The path a candidate names, or ``None`` when it names no file.

    The prose around a path — quotes, brackets, a trailing colon — is
    not part of the path, and stripping it is not a guess.  A path a
    tool shortened (``.../backend/agent.py``) is dropped instead: it
    has no project root, so the rule cannot say which file it names.

    Rejects anything that is not a source file: the suffix allow-list
    is what keeps ``self.assertEqual`` and ``example.com`` out of the
    answer.  A candidate carrying a backslash is rejected too — the
    POSIX path syntax this rule reads does not use them, and a
    Windows-shaped fragment would otherwise be reported as a truncated
    POSIX path.
    """
    text = raw.strip()
    if text.startswith("..."):
        return None
    text = _strip_path_noise(text)
    if not text or "\\" in text:
        return None
    if not _is_source_path(text):
        return None
    if text.startswith("./"):
        text = text[2:]
    return text or None


def _strip_path_noise(text: str) -> str:
    """Strip the punctuation that surrounds a path in prose."""
    return text.lstrip(_PATH_LEADING_NOISE).rstrip(_PATH_TRAILING_NOISE)


def _is_source_path(text: str) -> bool:
    """True when ``text`` names a file with a source-file suffix."""
    name = text.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        return False
    return name[dot:].lower() in _SOURCE_SUFFIXES


def _as_text(value: Any) -> str:
    """A text blob out of a failure field — a string, or a list of them."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(item for item in value if isinstance(item, str))
    return ""


def _string_entries(value: Any) -> List[str]:
    """The string entries of a list-like argument."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in value if isinstance(item, str)]
    return []


# ---------------------------------------------------------------------------
# collect_only_probe
# ---------------------------------------------------------------------------

#: The pytest flag that turns an invocation into a collection-only probe.
_COLLECT_ONLY_FLAG = "--collect-only"

#: The pytest program name as it appears on a command line.  The
#: lookbehind keeps ``mypytest`` and ``foo.pytest`` out; the lookahead
#: keeps ``pytest-xdist`` out.
_PYTEST_TOKEN_RE = re.compile(r"(?<![\w.])pytest(?![\w\-])")


def collect_only_probe(command: str, runner: Callable[[str], int]) -> int:
    """How many tests ``command`` would collect.

    The command is rewritten into a collection-only probe and handed to
    ``runner``, which runs it and returns the number of test items it
    collected.  The rewrite puts ``--collect-only`` after the pytest
    program token, so the flag lands on the test invocation and not on
    a later link of a ``&&`` chain; a command that already carries the
    flag is passed through unchanged.

    ``runner`` is the caller's process.  This function starts none: it
    builds the command, calls ``runner`` once, and reports the count.
    The caller keeps ownership of the timeout, the working directory
    and the parsing of the runner's output.

    **A count of zero is not an answer.**  It means the runner could
    not tell — the target does not exist, the module fails to import,
    the command is not a pytest command at all — and this function
    reports it as the zero it is.  Deciding what an undecidable probe
    means is the caller's job; a rule that turned the zero into "no
    tests exist" would be making a claim it cannot support.

    Raises whatever ``runner`` raises: a probe that timed out is a
    fact about the run, and flattening it into a zero would dress a
    broken probe up as a settled answer.
    """
    probe_command = _collect_only_command(command)
    if not probe_command:
        return 0
    result = runner(probe_command)
    try:
        return int(result)
    except (TypeError, ValueError):
        # A runner that answered with something unmeasurable has not
        # measured anything, which is the same situation as a zero.
        return 0


def _collect_only_command(command: str) -> str:
    """The collection-only form of ``command``."""
    text = (command or "").strip()
    if not text or _COLLECT_ONLY_FLAG in text:
        return text
    end = _pytest_token_end(text)
    if end is None:
        # No pytest program in the command: there is nothing to make
        # collect-only, and the runner's answer decides what that means.
        return text
    return f"{text[:end]} {_COLLECT_ONLY_FLAG}{text[end:]}"


def _pytest_token_end(text: str) -> Optional[int]:
    """The index just after the last pytest program token in ``text``.

    The last one, not the first: in ``pytest --version && pytest
    tests/`` the first is a version query and the second is the run.
    A token inside a quoted argument is not a program token, and a
    token that is a path component (``tests/pytest/``) is a directory.
    """
    quoted = _quoted_positions(text)
    end: Optional[int] = None
    for match in _PYTEST_TOKEN_RE.finditer(text):
        start = match.start()
        if start in quoted:
            continue
        before = text[start - 1] if start else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if before in ("/", "\\") and after in ("/", "\\"):
            continue
        end = match.end()
    return end


def _quoted_positions(text: str) -> Set[int]:
    """The indices of ``text`` that sit inside a quoted span."""
    inside: Set[int] = set()
    quote: Optional[str] = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote is None:
            if char in ("'", '"'):
                quote = char
                inside.add(index)
            index += 1
            continue
        inside.add(index)
        if char == "\\" and quote == '"' and index + 1 < length:
            inside.add(index + 1)
            index += 2
            continue
        if char == quote:
            quote = None
        index += 1
    return inside


# ---------------------------------------------------------------------------
# is_empty_shell_test
# ---------------------------------------------------------------------------


def is_empty_shell_test(source: str) -> bool:
    """True when the test source asserts nothing about the code.

    A shell test is one that passes without checking anything:
    ``assert True``, ``pass``, ``pytest.skip(...)``, a body that is
    only a docstring.  Such a test is green before the repair runs and
    green after it, so it certifies nothing — a task whose only test is
    a shell has verified nothing.

    The test is a shell when **no** statement in it can fail for a
    reason that has to do with the code under test.  An ``assert`` on a
    computed value can, an ``assertEqual``-style call can, and a
    ``pytest.raises`` block can; ``assert True`` cannot, because it is
    true before the code runs.

    A source with several tests is a shell only when every one of them
    is.  A source with no test at all is judged on its own body, so a
    module that merely asserts something at import time is not called a
    shell test.

    A source that does not parse is not called a shell either: it is
    broken, which is a different finding.
    """
    text = source if isinstance(source, str) else ""
    if not text.strip():
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    tests = _test_functions(tree)
    if tests:
        return all(_body_asserts_nothing(node.body) for node in tests)
    return _body_asserts_nothing(tree.body)


def _test_functions(tree: ast.AST) -> List[ast.AST]:
    """The test functions in a parsed source."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]


def _body_asserts_nothing(body: Sequence[ast.stmt]) -> bool:
    """True when no statement of a body can fail on the code."""
    statements = _without_docstring(body)
    return not any(_is_meaningful_assertion(stmt) for stmt in statements)


def _without_docstring(body: Sequence[ast.stmt]) -> List[ast.stmt]:
    """A body without its leading docstring — a docstring asserts nothing."""
    statements = list(body)
    if not statements:
        return statements
    first = statements[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        statements = statements[1:]
    return statements


def _is_meaningful_assertion(stmt: ast.stmt) -> bool:
    """True when a statement can fail for a reason of its own."""
    if isinstance(stmt, ast.Assert):
        return not _is_trivially_true(stmt.test)
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return _is_assertion_call(stmt.value)
    if isinstance(stmt, ast.With):
        return any(_is_raises_call(item.context_expr) for item in stmt.items)
    return False


def _is_trivially_true(node: ast.expr) -> bool:
    """True for an assertion that is true before the code under test runs."""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
        left = node.left
        right = node.comparators[0]
        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
            return left.value == right.value
    return False


def _is_assertion_call(call: ast.Call) -> bool:
    """True for an assertion written as a call — ``assertEqual``, ``assert_foo``."""
    name = _call_name(call.func)
    return bool(name) and name.startswith("assert")


def _is_raises_call(node: ast.expr) -> bool:
    """True for a ``pytest.raises``-style context manager."""
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node.func)
    return name in ("raises", "warns")


def _call_name(func: ast.expr) -> Optional[str]:
    """The trailing name of a called expression — ``a.b.c`` → ``c``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None