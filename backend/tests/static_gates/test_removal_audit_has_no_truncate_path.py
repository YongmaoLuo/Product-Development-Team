"""Static gate: the VP removal audit has no truncate / overwrite path.

Why this gate exists
--------------------
主题外 VP 的删除顺序被钉死为「先归档、后移除」，而审计文件是这次删除
**唯一**的持久化真相。That makes the audit file a tamper target: if any
code path can truncate it, rewrite it, or delete it, then "the removal is
archived" stops being a fact and becomes a claim — and a claim that can be
erased is not a reason to remove a VP.

This gate reads the **shape** of the module rather than its behaviour (the
behaviour lives in
``backend/tests/unit/test_vp_removal_audit_is_append_only.py``). It is a
source-shape gate because the failure it prevents is a *capability* — a
truncate path does not have to be taken to be a defect.

What it pins
------------
1. **One write mode.** The only file mode the module opens the audit with
   is ``"a"``. ``"w"`` / ``"w+"`` / ``"r+"`` / ``"x"`` are all rejected.
2. **No destructive call.** No ``truncate`` / ``remove`` / ``unlink`` /
   ``rmtree``, no ``shutil``, no whole-file overwrite helper
   (``write_text`` / ``write_bytes``), no atomic-replace path
   (``os.replace`` / ``os.rename``).
3. **A pinned public surface.** The module exports exactly
   ``archive_removal`` / ``load_removed_vp_ids`` / ``AUDIT_FILENAME`` — no
   public helper that could grow into a truncate path later.
4. **Durable before durable.** ``archive_removal`` writes, flushes and
   fsyncs, in that order: the record must be on disk before the function
   returns, or the caller must not remove the VP.
5. **The file name is a contract.** ``AUDIT_FILENAME`` is
   ``"vp_removal_audit.jsonl"``, and no other production module in
   ``backend/`` mentions that name — so nobody else can open the audit
   behind the module's back.

Docstrings and ``#`` comments are blanked before the text scan, so prose
about truncation is not a violation, and prose cannot hide one.
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

import pytest

#: ``backend/`` is two levels up from ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The module under gate.
MODULE_PATH = BACKEND_DIR / "verification" / "vp_removal_audit.py"

#: The public surface the interface contract promises — and nothing else.
EXPECTED_PUBLIC_SURFACE: frozenset[str] = frozenset(
    {"AUDIT_FILENAME", "archive_removal", "load_removed_vp_ids"}
)

#: The audit file's on-disk name. Part of the contract: anything reading
#: the audit outside this module has to agree on it.
EXPECTED_AUDIT_FILENAME = "vp_removal_audit.jsonl"

#: Directories that never hold production source.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", ".venv", "__pycache__", ".pytest_cache", ".git"}
)

#: Forbidden shapes, as ``(pattern, why)`` pairs. Matched against the
#: code-only text (docstrings and comments blanked).
_FORBIDDEN_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"""['"]w\+?['"]""", "write mode — truncates the audit"),
    (r"""['"]r\+['"]""", "read/write mode — rewrites the audit"),
    (r"""['"]x\+?['"]""", "exclusive-create mode — refuses to append"),
    (r"\btruncate\b", "truncate call"),
    (r"\.truncate\(", "truncate call"),
    (r"\bos\.remove\b", "os.remove"),
    (r"\bos\.unlink\b", "os.unlink"),
    (r"\bos\.rmdir\b", "os.rmdir"),
    (r"\bunlink\(", "unlink call"),
    (r"\brmtree\b", "shutil.rmtree"),
    (r"\bshutil\b", "shutil import"),
    (r"\bwrite_text\b", "whole-file overwrite"),
    (r"\bwrite_bytes\b", "whole-file overwrite"),
    (r"\bos\.replace\b", "atomic replace — an overwrite path"),
    (r"\bos\.rename\b", "rename — an overwrite path"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


def _code_only_text(source: str) -> str:
    """Return ``source`` with docstrings and ``#`` comments blanked out.

    Prose about truncation is not a truncate path, and prose must not be
    able to hide one either — so both the docstrings and the comments are
    replaced by spaces (newlines kept) before the text scan.
    """
    lines = source.splitlines(keepends=True)
    offsets: List[int] = []
    total = 0
    for line in lines:
        offsets.append(total)
        total += len(line)

    def _abs(row: int, col: int) -> int:
        # tokenize / AST rows are 1-based, columns are 0-based.
        return offsets[row - 1] + col

    spans: List[Tuple[int, int]] = []

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc = first.value
            spans.append(
                (_abs(doc.lineno, doc.col_offset), _abs(doc.end_lineno, doc.end_col_offset))
            )

    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                spans.append((_abs(*token.start), _abs(*token.end)))
    except tokenize.TokenError:  # pragma: no cover — malformed source
        pass

    chars = list(source)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _public_module_names(tree: ast.AST) -> Set[str]:
    """Public module-level names (functions, classes, assignments)."""
    names: Set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
    return names


def _module_level_constant(tree: ast.AST, name: str) -> Optional[str]:
    """The literal value of a module-level string constant, if any."""
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != name:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def _function(tree: ast.AST, name: str) -> Optional[ast.FunctionDef]:
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _call_sequence(func: ast.FunctionDef) -> List[str]:
    """The names called inside ``func``, in source order."""
    calls: List[Tuple[int, int, str]] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            calls.append((node.lineno, node.col_offset, target.id))
        elif isinstance(target, ast.Attribute):
            calls.append((node.lineno, node.col_offset, target.attr))
    calls.sort(key=lambda item: (item[0], item[1]))
    return [name for _, _, name in calls]


def _contains_subsequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True when ``needle`` appears in ``haystack`` in order."""
    iterator = iter(haystack)
    return all(any(item == wanted for item in iterator) for wanted in needle)


def _iter_production_modules() -> List[Path]:
    """Every production ``*.py`` under ``backend/`` (tests excluded)."""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(BACKEND_DIR):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return found


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_audit_module_has_no_truncate_path() -> None:
    """No truncate / overwrite / delete shape survives in the audit module."""
    source = _module_source()
    code_only = _code_only_text(source)

    violations: List[str] = []
    for pattern, why in _FORBIDDEN_PATTERNS:
        for match in re.finditer(pattern, code_only):
            line_no = code_only.count("\n", 0, match.start()) + 1
            violations.append(f"line {line_no}: {why} ({match.group(0)!r})")

    assert not violations, (
        f"{MODULE_PATH.name} exposes a truncate / overwrite / delete path "
        "for the removal audit. The audit is append-only and is the only "
        "persistent truth behind a VP removal, so no code path may "
        "truncate, rewrite or delete it.\n  "
        + "\n  ".join(violations)
    )


def test_audit_module_opens_only_in_append_mode() -> None:
    """The audit file is opened with ``"a"`` and nothing else."""
    tree = ast.parse(_module_source())

    modes: List[Tuple[int, Optional[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == "open":
            index = 1
        elif isinstance(target, ast.Attribute) and target.attr == "open":
            index = 0
        else:
            continue
        argument: Optional[ast.AST] = None
        if len(node.args) > index:
            argument = node.args[index]
        else:
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    argument = keyword.value
                    break
        if argument is None:
            modes.append((node.lineno, None))
        elif isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            modes.append((node.lineno, argument.value))
        else:
            modes.append((node.lineno, "<dynamic>"))

    assert modes, f"{MODULE_PATH.name} never opens the audit file"

    offenders = [
        f"line {line_no}: mode={mode!r}" for line_no, mode in modes if mode != "a"
    ]
    assert not offenders, (
        f"{MODULE_PATH.name} opens the audit file with a mode other than 'a' "
        f"(append): {offenders!r}"
    )


def test_audit_module_public_surface_is_pinned() -> None:
    """The module exports the contracted surface and nothing destructive."""
    tree = ast.parse(_module_source())
    public = _public_module_names(tree)

    assert public == set(EXPECTED_PUBLIC_SURFACE), (
        f"{MODULE_PATH.name} public surface drifted. Expected exactly "
        f"{sorted(EXPECTED_PUBLIC_SURFACE)!r}, found {sorted(public)!r}. "
        "A public helper is where a truncate path grows next."
    )

    destructive = sorted(
        name
        for name in public
        if any(word in name.lower() for word in ("truncate", "overwrite", "delete", "unlink", "purge", "rewrite"))
    )
    assert not destructive, (
        f"{MODULE_PATH.name} exports destructive-sounding names {destructive!r}. "
        "The audit module must not offer a way to erase the audit."
    )


def test_audit_filename_is_part_of_the_contract() -> None:
    """``AUDIT_FILENAME`` is pinned, and only this module mentions it."""
    tree = ast.parse(_module_source())
    assert _module_level_constant(tree, "AUDIT_FILENAME") == EXPECTED_AUDIT_FILENAME, (
        f"{MODULE_PATH.name}: AUDIT_FILENAME must be "
        f"{EXPECTED_AUDIT_FILENAME!r} — the name is how anything else "
        "finds the audit."
    )

    others: List[str] = []
    for module in _iter_production_modules():
        if module == MODULE_PATH:
            continue
        try:
            text = module.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if EXPECTED_AUDIT_FILENAME in text:
            others.append(str(module.relative_to(BACKEND_DIR)))
    assert not others, (
        f"Production module(s) {others!r} mention {EXPECTED_AUDIT_FILENAME!r}. "
        "Only the audit module may touch the audit file; a second writer "
        "is how the append-only guarantee is lost."
    )


def test_archive_removal_is_durable_before_it_returns() -> None:
    """``archive_removal`` writes, flushes and fsyncs — in that order.

    The caller removes the VP only after this function returned, so the
    record has to be on disk by then.  A flush alone would leave the
    bytes in the page cache; the fsync is what makes "archived" a fact.
    """
    tree = ast.parse(_module_source())
    func = _function(tree, "archive_removal")
    assert func is not None, f"{MODULE_PATH.name} must define archive_removal"

    sequence = _call_sequence(func)
    assert _contains_subsequence(sequence, ["write", "flush", "fsync"]), (
        f"archive_removal must write → flush → fsync, in that order, before "
        f"it returns. Actual call sequence: {sequence!r}"
    )