"""Static gate: the per-plan HITL configuration has one reader and one
writer.

Background
----------
The HITL subsystem stores each plan's hook configuration in
``<plan_dir>/hitl_config.json``.  The upstream material used two names
for the module that owns that file — ``backend/hitl/hook_config.py``
and ``backend/hooks/config.py`` — and "the implementer picks one" is
not a decision a gate can leave open.  Two modules that both read and
write the file are two implementations of the same rules, and they
drift.  The drift is silent: nothing raises, each module's own unit
tests stay green, and the disagreement only surfaces as a plan whose
stored configuration and its read-back configuration differ.

The architecture settled the naming.  ``backend/hooks/config.py`` is
the one reader and one writer of the file, ``backend/hooks/dispatcher.py``
consumes the hook list that module returns without parsing the file
itself, and the ``backend/hitl/hook_config.py`` spelling is void.  This
gate is the executable form of that decision.

What the gate checks
--------------------
1. ``backend/hooks/config.py`` is the only module that writes the file.
2. ``backend/hitl/hook_config.py`` does not exist.
3. Only the sanctioned module opens the file's contents.

How the scan works
------------------
The whole application source is read through
:mod:`tests.app_source` — never ``server.py`` on its own, because the
module that owns the file is not in ``server.py``, and a scan that
looked only there would find nothing and pass.

A module counts as a reader or a writer when **both** halves are
present in its executable code:

* the *path literal* — a string constant naming the file
  (``"hitl_config.json"``, or a path ending in the file's stem), and
* the *operation shape* — a call that writes (``open(..., "w")``,
  ``path.write_text(...)``, ``json.dump(...)``, the repository's
  ``atomic_write_json``) or reads (``open(...)`` without a write mode,
  ``path.read_text(...)``, ``json.load(...)``).

Neither half is a function name of the HITL API.  The gate never looks
for ``write_for_plan`` or ``load_for_plan``: a scan by name is defeated
by a rename, which is the failure this gate exists to prevent.

Docstrings and comments are prose, not references, so they do not
count.  Several modules *describe* the file in prose — the sanctioned
module's own docstring is the longest description of it in the tree —
and a module that explains a file is not a module that touches it.

The gate is coarse in one direction: it decides per module, not per
call.  A module that names the file and writes some *other* file is
reported as a writer.  That is deliberate — the alternative is a
dataflow analysis over a codebase where the path is assembled from a
constant, and a gate nobody can afford to keep is a gate that gets
deleted.
"""

from __future__ import annotations

import ast
import re
import sys
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The repository root, one level above ``backend/``.
REPO_ROOT = BACKEND_DIR.parent

from tests import app_source as app_source_module  # noqa: E402


# ---------------------------------------------------------------------------
# What the gate is about
# ---------------------------------------------------------------------------

#: The per-plan configuration file: one per plan, next to the plan's
#: own state.
CONFIG_FILENAME = "hitl_config.json"

#: The same file's stem.  A path that ends in it names the file too.
CONFIG_STEM = "hitl_config"

#: The one module allowed to read or write the file.
SANCTIONED_MODULE = "backend/hooks/config.py"

#: The module paths the architecture pins by name.
SANCTIONED_PATHS: tuple[str, ...] = (
    "backend/hooks/__init__.py",
    "backend/hooks/config.py",
    "backend/hooks/dispatcher.py",
)

#: The spelling the architecture voided.
FORBIDDEN_MODULE = "backend/hitl/hook_config.py"

#: The modules allowed to open the file's contents.
READER_WHITELIST: frozenset[str] = frozenset({SANCTIONED_MODULE})

#: This repository's atomic write helper.  It is a *write shape* — the
#: codebase's spelling of "put this document on disk" — and not a name
#: of the HITL API, so matching it does not make this a scan by name.
_ATOMIC_WRITE_NAMES: frozenset[str] = frozenset(
    {"atomic_write_json", "atomic_write_text", "atomic_write_bytes"}
)


# ---------------------------------------------------------------------------
# Scanning one module
# ---------------------------------------------------------------------------


def _docstring_ids(tree: ast.Module) -> set[int]:
    """The identity of every string constant that is a docstring.

    A docstring is prose.  A module that *describes* the configuration
    file is not a module that *reads* it.
    """
    ids: set[int] = set()
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
            ids.add(id(first.value))
    return ids


def _names_the_config(value: str) -> bool:
    """True when *value* is a path that names the configuration file."""
    if CONFIG_FILENAME in value:
        return True
    return bool(
        re.search(r"(?:^|[/\\])" + re.escape(CONFIG_STEM) + r"$", value)
    )


def _open_mode(node: ast.Call) -> str | None:
    """The mode *node* opens with, or ``None`` when it is not a literal.

    A mode computed at run time is not a shape this gate can read, so
    it is never counted as a write — the gate prefers a missed hit over
    a hit it cannot justify.
    """
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value
    return None


def _operation(node: ast.Call) -> tuple[str, str] | None:
    """``(kind, label)`` for a file operation, or ``None``.

    ``kind`` is ``"write"`` or ``"read"``.  ``label`` names the shape
    without quoting the source line, so a report says what happened
    without republishing the line that happened.
    """
    func = node.func
    if isinstance(func, ast.Name):
        if func.id == "open":
            mode = _open_mode(node)
            if mode is None:
                # ``open(path)`` reads; a computed mode is not a shape
                # this gate can read, so it is not counted as a write.
                return ("read", "open(...)")
            if any(char in mode for char in "wax+"):
                return ("write", f"open(..., {mode!r})")
            if "r" in mode:
                return ("read", f"open(..., {mode!r})")
            return None
        if func.id in _ATOMIC_WRITE_NAMES:
            return ("write", f"{func.id}(...)")
        return None
    if isinstance(func, ast.Attribute):
        if func.attr in ("write_text", "write_bytes", "writelines"):
            return ("write", f"{func.attr}(...)")
        if func.attr in ("read_text", "read_bytes"):
            return ("read", f"{func.attr}(...)")
        if func.attr == "dump" and isinstance(func.value, ast.Name):
            if func.value.id == "json":
                return ("write", "json.dump(...)")
        if func.attr == "load" and isinstance(func.value, ast.Name):
            if func.value.id == "json":
                return ("read", "json.load(...)")
    return None


@dataclass(frozen=True)
class ModuleFacts:
    """What one module does with the configuration file."""

    path: str
    names: tuple[str, ...]
    writes: tuple[str, ...]
    reads: tuple[str, ...]

    @property
    def is_writer(self) -> bool:
        """Names the file *and* writes something."""
        return bool(self.names) and bool(self.writes)

    @property
    def is_reader(self) -> bool:
        """Names the file *and* opens it for reading."""
        return bool(self.names) and bool(self.reads)


def _classify(path: str, source: str) -> ModuleFacts:
    """What *source* does with the configuration file."""
    with warnings.catch_warnings():
        # A module this gate does not own may carry an invalid escape
        # sequence.  That is that module's business, and the warning
        # would otherwise be printed under this gate's name — as if the
        # gate had something to say about it.
        warnings.simplefilter("ignore")
        tree = ast.parse(source, filename=path)
    docstrings = _docstring_ids(tree)

    names: list[str] = []
    writes: list[str] = []
    reads: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if _names_the_config(node.value):
                names.append(node.value)
            continue
        if isinstance(node, ast.Call):
            operation = _operation(node)
            if operation is None:
                continue
            kind, label = operation
            entry = f"line {node.lineno}: {label}"
            if kind == "write":
                writes.append(entry)
            else:
                reads.append(entry)

    return ModuleFacts(
        path=path,
        names=tuple(names),
        writes=tuple(writes),
        reads=tuple(reads),
    )


# ---------------------------------------------------------------------------
# Scanning the application
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _scan() -> dict[str, ModuleFacts]:
    """Classify every application module, keyed by repository path.

    The scan is cached: the tree does not change while the suite runs,
    and four gates asking the same question should not parse the same
    hundred and eighty modules four times.  Callers that add to the
    result copy it first.
    """
    facts: dict[str, ModuleFacts] = {}
    for path, source in app_source_module.app_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        facts[rel] = _classify(rel, source)
    return facts


def _report(facts: Mapping[str, ModuleFacts], kind: str) -> str:
    """The modules that read or write the file, one per line."""
    lines: list[str] = []
    for rel in sorted(facts):
        module = facts[rel]
        if kind == "write" and module.is_writer:
            lines.append(f"  {rel} — {', '.join(module.writes)}")
        elif kind == "read" and module.is_reader:
            lines.append(f"  {rel} — {', '.join(module.reads)}")
    return "\n".join(lines) if lines else "  (none)"


def _writers(facts: Mapping[str, ModuleFacts]) -> set[str]:
    """The modules that write the configuration file."""
    return {rel for rel, module in facts.items() if module.is_writer}


def _readers(facts: Mapping[str, ModuleFacts]) -> set[str]:
    """The modules that open the configuration file for reading."""
    return {rel for rel, module in facts.items() if module.is_reader}


#: A module that writes the configuration file — the shape the gate has
#: to catch.  It is kept as source text rather than as a file so the
#: negative control does not put a second writer into the tree it is
#: scanning.
_SECOND_WRITER_SOURCE = '''
"""A second writer of the per-plan configuration."""

import json
from pathlib import Path

CONFIG_FILENAME = "hitl_config.json"


def write(plan_dir):
    path = Path(plan_dir) / CONFIG_FILENAME
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mode": "auto"}, handle)
'''


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_single_writer() -> None:
    """``backend/hooks/config.py`` is the only module that writes the file.

    A second writer is a second implementation of the same rules.  The
    two drift, and the drift is silent: both modules' own tests stay
    green while the plan's stored configuration and its read-back
    configuration disagree.
    """
    source = app_source_module.app_source()
    assert CONFIG_FILENAME in source, (
        f"{CONFIG_FILENAME!r} does not appear in the application source. "
        f"The gate is scanning the wrong tree — a scan that matches "
        f"nothing passes, and that is not evidence of anything."
    )

    facts = _scan()
    writers = _writers(facts)
    assert writers == {SANCTIONED_MODULE}, (
        f"the per-plan HITL configuration must have exactly one writer, "
        f"{SANCTIONED_MODULE!r}; found {sorted(writers)!r}.\n"
        f"modules that write it:\n{_report(facts, 'write')}\n"
        f"Route the write through hooks.config instead of writing the "
        f"file here."
    )


def test_forbidden_module_absent() -> None:
    """``backend/hitl/hook_config.py`` must not exist.

    The architecture voided that spelling of the module: the hook
    configuration lives in ``backend/hooks/``, next to the dispatcher
    that consumes it.  A second module at the voided path is the
    beginning of the second implementation this gate exists to prevent.
    """
    forbidden = REPO_ROOT / FORBIDDEN_MODULE
    assert not forbidden.exists(), (
        f"{FORBIDDEN_MODULE} exists. The hook configuration belongs to "
        f"backend/hooks/config.py; the hitl/hook_config.py spelling is "
        f"void and a second module at that path is a second "
        f"implementation waiting to drift."
    )

    siblings = sorted(
        path.name
        for path in (REPO_ROOT / "backend" / "hitl").glob("hook_config*.py")
    )
    assert not siblings, (
        f"backend/hitl/ holds {siblings!r}. The hook configuration "
        f"module belongs to backend/hooks/config.py; nothing in "
        f"backend/hitl/ may carry that name."
    )


def test_readers_report() -> None:
    """Only the sanctioned module opens the file's contents.

    A second reader is a second parse of the same document, and a
    second parser is a second set of defaults — a file that is absent
    here and torn there is one plan behaving two ways.
    """
    facts = _scan()
    readers = _readers(facts)
    assert readers == set(READER_WHITELIST), (
        f"only {sorted(READER_WHITELIST)!r} may read the per-plan HITL "
        f"configuration; found {sorted(readers)!r}.\n"
        f"modules that read it:\n{_report(facts, 'read')}\n"
        f"Read it through hooks.config.load_for_plan() instead of "
        f"parsing the file here."
    )


def test_sanctioned_modules_exist() -> None:
    """The module paths the architecture pins are present.

    A gate that pins a path also has to notice the path going away —
    otherwise the pin is a comment.
    """
    missing = [path for path in SANCTIONED_PATHS if not (REPO_ROOT / path).exists()]
    assert not missing, (
        f"the architecture pins {list(SANCTIONED_PATHS)!r}; missing "
        f"{missing!r}."
    )


def test_only_the_sanctioned_module_names_the_config_file() -> None:
    """No other module names the configuration file in executable code.

    The whitelist is about the *name* as much as about the read and
    the write: a module that spells the file out is a module that is
    one edit away from opening it itself.
    """
    facts = _scan()
    namers = {rel for rel, module in facts.items() if module.names}
    unexpected = sorted(namers - set(READER_WHITELIST))
    assert not unexpected, (
        f"only {sorted(READER_WHITELIST)!r} may name the per-plan HITL "
        f"configuration in executable code; {unexpected!r} name it too. "
        f"Go through hooks.config rather than spelling the file out."
    )


def test_a_second_writer_is_named_in_the_report() -> None:
    """A second writer turns the gate red, and the report names it.

    The negative control, kept hermetic: the second writer is source
    text here, not a file in the tree, so the control proves the scan
    is sensitive without leaving a second writer behind.
    """
    facts = dict(_scan())
    second = "backend/hitl/second_writer.py"
    facts[second] = _classify(second, _SECOND_WRITER_SOURCE)

    writers = _writers(facts)
    assert SANCTIONED_MODULE in writers, (
        "the sanctioned module must still be recognised as the writer"
    )
    assert second in writers, (
        f"a module that names the file and writes it must be reported as "
        f"a writer; {second!r} was not. writers={sorted(writers)!r}"
    )
    assert second in _report(facts, "write"), (
        f"the report must name the offending file; {_report(facts, 'write')!r} "
        f"does not mention {second!r}"
    )