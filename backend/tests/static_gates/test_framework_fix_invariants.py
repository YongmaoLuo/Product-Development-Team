"""Six static invariants of the framework-fix lane.

Background
----------
The framework-fix lane put a repair of the framework itself into the same
process as an ordinary plan: a mechanical fingerprint registers the
defect, a clean-main reproduction proves it, the repair plan is derived
and attached to the plan that hit it, and the scheduler gives the repair
a slot ahead of an ordinary one.

Six of the properties that make that work are not behaviour any single
test can observe -- they are the *shape* of the code, and a later change
can break one of them without breaking a single test that exists today.

The six
-------
1. **append-only** -- ``plan_verification_result`` is written by INSERT
   and by nothing else.  A re-run after a repair is merged appends the
   row for the new tree; the row for the old tree stays, because "this
   failed here" is the reason the repair exists.  A verdict that can be
   rewritten is an evidence that can be made to agree with whatever the
   tree looks like afterwards.
2. **closed vocabulary** -- every reason the scheduler writes is one of
   the words in :data:`plan_scheduler.policy.REASONS`.  The panel draws
   the words, the dry run records them and the API publishes them: a word
   outside the set is a sentence the panel cannot draw.
3. **no shell** -- the lane runs no shell.  A repair runs commands whose
   output is evidence, and a shell turns a command line into a program.
4. **one-way dependency** -- ``framework_fix`` does not import
   ``plan_scheduler.engine``.  The engine is the seam that calls the
   lane; a lane that called the engine back would close a cycle, and the
   cycle would be an import that fails at start-up.
5. **no state JSON** -- the lane writes no ``plan_state.json``.  SQLite
   is the single source of truth; a JSON sidecar would be a second one.
6. **the scan itself** -- the scan reaches the whole repository, and a
   file it cannot parse is a **failure**, never a skip.

How the scan works
------------------
The scan is repository-wide: every ``*.py`` file in the tree is read,
with the vendor trees (``.venv``, ``node_modules`` and the other
non-source directories) pruned by name.  A file is then *parsed* when it
can carry a violation:

* every file of the lane's own packages (always), and
* every file whose text names the result table.

That second rule is sound because the shapes are self-naming: a
mutation of the result table names the table, a shell call names
``shell``, an import of the scheduler engine names the module, a write of
the state JSON names the file.  A file whose text carries none of those
names carries none of the violations either, so leaving it unparsed
leaves nothing unchecked -- and parsing it would cost the gate a parse of
the whole tree on every run.

Three details make the answer trustworthy:

* **A file that does not parse is a failure.**  A gate that cannot read a
  file cannot say the file is clean; the failure is reported with the
  file that caused it, so the gap stays visible instead of turning into a
  silent pass.
* **Docstrings and comments are not code.**  The detectors look at the
  parsed tree, so a module that *documents* ``shell=True`` (several do)
  is not a module that *calls* it, and a docstring that names a table is
  not a statement that rewrites it.
* **Every detector is checked against a planted violation.**  Each case
  below writes a file that breaks its own invariant and asserts that the
  detector says so; a detector that stopped detecting would turn the
  gate green over a broken tree.

What is scanned, and what is not
--------------------------------
The **repository-wide** scan covers every ``*.py`` file in the tree: the
append-only contract is a property of the table, not of a package, and a
mutation written anywhere is a mutation.

The **lane-scoped** invariants -- the closed vocabulary, the shell rule,
the dependency direction and the state-JSON rule -- are about the code
the framework-fix lane owns, which is two packages:
``backend/framework_fix`` and ``backend/plan_scheduler``.  The exception
is the vocabulary, which is checked in full inside the scheduler package
(a reason the scheduler writes is a scheduler word by definition) and by
its *constants* inside the framework-fix package (the lane repeats three
of the scheduler's words there, and those three have to be words the
scheduler knows; its other ``reason=`` arguments are prose -- the
semantic review's sentence for why two failures are not the same defect
-- and a sentence is not a word).

Known limits, stated rather than hidden: a statement whose table name is
built at run time (``f"UPDATE {table} SET ..."`` where ``table`` is a
variable) is not recognised -- the same limit every static gate that
reads the tree has.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterator, Sequence

import source_scan  # noqa: E402  (same directory; stdlib-only walker)

#: ``backend/`` on the import path, so the closed vocabulary is read from
#: the module that owns it rather than copied into this file.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from plan_scheduler.policy import REASONS  # noqa: E402

#: The repository root, derived from this file's location.
REPO_ROOT = source_scan.REPO_ROOT

#: This file.  It carries the planted fixtures of the negative controls,
#: so it is not scanned for violations of the invariants it checks.
THIS_FILE = Path(__file__).resolve()

#: The packages the framework-fix lane owns.
FRAMEWORK_FIX_PACKAGE = "backend/framework_fix"
SCHEDULER_PACKAGE = "backend/plan_scheduler"

#: The table whose rows are appended and never rewritten.
RESULT_TABLE = "plan_verification_result"

#: The legacy JSON sidecar the state machine replaced with SQLite.
STATE_JSON = "plan_state.json"

#: The constant that carries the legacy sidecar's name.
STATE_JSON_CONSTANT = "PLAN_STATE_FILENAME"

#: The names a reason constant may carry.
_REASON_CONSTANT_NAMES = ("REASON", "REASONS")

#: The suffixes a reason constant may carry.
_REASON_CONSTANT_SUFFIXES = ("_REASON", "_REASONS")

#: The dictionary keys and argument names that carry a reason word.
_REASON_SLOTS = ("reason", "pause_reason")

#: The module the framework-fix lane must not import: it is the seam
#: that calls the lane, so the lane calling it back closes a cycle.
_FORBIDDEN_ENGINE_MODULE = "plan_scheduler.engine"

#: The callables that build a set of words out of a literal.
_SET_LIKE = frozenset({"frozenset", "set", "list", "tuple"})

#: The ``os`` members that take a command *string* and run it.
SHELL_ESCAPE_MEMBERS = frozenset({"system", "popen"})

#: The shape of a statement that rewrites a row.
_UPDATE_STATEMENT_RE = re.compile(r"(?is)\bupdate\s+[^\s;]+\s+set\b")
_DELETE_STATEMENT_RE = re.compile(r"(?is)\bdelete\s+from\s+[^\s;]+")

#: A statement that rewrites a row of the result table.  It is spelled in
#: parts so that this file does not contain the statement it forbids: the
#: gate scans the repository, and this file is in the repository.
_MUTATING_UPDATE = "UPDATE " + RESULT_TABLE + " SET verdict = ?"
_MUTATING_DELETE = "DELETE FROM " + RESULT_TABLE + " WHERE plan_id = ?"

#: The shapes the detectors have to catch, planted as negative controls.
_PLANTED_REASON = "PLANTED_REASON = 'a_word_outside_the_closed_set'\n"
#: A shell call written twice: once through a module member
#: (``subprocess.run``) and once through an object member (``ctx.run``).
#: A shape that only the bare name would miss is not a shape the gate
#: may miss.
_PLANTED_SHELL = (
    "import os\n"
    "import subprocess\n"
    "def run(ctx):\n"
    "    subprocess.run(['ls'], shell=True)\n"
    "    ctx.run(['ls'], shell=True)\n"
    "    os.system('ls')\n"
)
_PLANTED_ENGINE_IMPORT = "from " + _FORBIDDEN_ENGINE_MODULE + " import Scheduler\n"
_PLANTED_STATE_JSON = STATE_JSON_CONSTANT + " = '" + STATE_JSON + "'\n"


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _repo_python_files() -> list[Path]:
    """Every first-party ``*.py`` file in the repository.

    The walk starts at the repository root rather than at one package:
    the append-only contract is a property of the table, and a mutation
    of it written in a directory this gate does not know about is still a
    mutation.  The vendor trees are pruned by name, which is the same
    exclusion list the shared walker uses.
    """
    return [
        path
        for path in source_scan.iter_first_party_sources()
        if path.suffix == ".py"
    ]


def _package_files(package: str) -> list[Path]:
    """Every ``*.py`` file in one package of the repository."""
    root = REPO_ROOT / package
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def _lane_files() -> list[Path]:
    """Every ``*.py`` file of the two packages the lane owns."""
    return _package_files(FRAMEWORK_FIX_PACKAGE) + _package_files(SCHEDULER_PACKAGE)


def _read_files(paths: Sequence[Path]) -> tuple[list[tuple[Path, str]], list[str]]:
    """Read every path, returning ``(contents, failures)``.

    A file that cannot be read is a failure, never a skip.
    """
    contents: list[tuple[Path, str]] = []
    failures: list[str] = []
    for path in paths:
        try:
            contents.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            failures.append(f"{_relative(path)}: cannot be read: {exc}")
    return contents, failures


def _parse_files(paths: Sequence[Path]) -> tuple[list[tuple[Path, ast.Module]], list[str]]:
    """Parse every path, returning ``(parsed, failures)``.

    A file that does not parse is a **failure**, never a silent skip: the
    gate cannot certify a file it did not read.  The two lists are
    returned separately on purpose -- a caller that only looked at the
    parsed list would see a shorter list and would not know that files
    were dropped.
    """
    parsed: list[tuple[Path, ast.Module]] = []
    failures: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failures.append(f"{_relative(path)}: cannot be read: {exc}")
            continue
        try:
            parsed.append((path, ast.parse(source)))
        except SyntaxError as exc:
            failures.append(f"{_relative(path)}: does not parse as Python: {exc}")
    return parsed, failures


def _result_table_candidates(
    contents: Sequence[tuple[Path, str]]
) -> list[Path]:
    """The files whose text names the result table.

    A statement that rewrites a row of the table has to name the table,
    so a file whose text does not name it cannot rewrite one.  This file
    is left out: it carries the planted fixtures of the negative
    controls, and a fixture is not a violation.
    """
    return [
        path
        for path, text in contents
        if RESULT_TABLE in text and path.resolve() != THIS_FILE
    ]


def _relative(path: Path) -> str:
    """The path as the repository names it."""
    return source_scan.repo_relative(path).as_posix()


def _docstrings(tree: ast.Module) -> set[ast.Constant]:
    """The string constants that are docstrings.

    A docstring is prose: it describes the code, it does not run it.  The
    detectors skip them, so a module that *documents* a forbidden shape is
    not a module that *uses* one.
    """
    found: set[ast.Constant] = set()

    def take(body: list[ast.stmt]) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(first.value)

    take(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            take(node.body)
    return found


def _code_strings(tree: ast.Module) -> Iterator[tuple[ast.AST, str]]:
    """Every string the module *executes*, as ``(node, text)``.

    Docstrings are left out; comments are not in the tree at all.  An
    f-string yields the literal parts it is built from, joined -- which is
    enough to recognise a statement whose shape is fixed and whose table
    name is written out.
    """
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node not in docs:
                yield node, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = [
                value.value
                for value in node.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            ]
            yield node, "".join(parts)


# ---------------------------------------------------------------------------
# The detectors
# ---------------------------------------------------------------------------


def _find_result_table_mutations(
    parsed: Sequence[tuple[Path, ast.Module]]
) -> list[str]:
    """Every statement that rewrites a row of the result table.

    The statement has to name the table -- SQL says which table it
    touches -- and it has to have the shape of a rewrite.  A statement
    that inserts, selects or creates the table is what the table is for.
    """
    violations: list[str] = []
    for path, tree in parsed:
        for node, text in _code_strings(tree):
            if RESULT_TABLE not in text:
                continue
            if _UPDATE_STATEMENT_RE.search(text) or _DELETE_STATEMENT_RE.search(text):
                violations.append(
                    f"{_relative(path)}:{node.lineno}: rewrites a row of {RESULT_TABLE}"
                )
    return violations


def _is_reason_constant(name: str) -> bool:
    """True when *name* is the name of a reason constant."""
    return name in _REASON_CONSTANT_NAMES or name.endswith(_REASON_CONSTANT_SUFFIXES)


def _string_words(value: ast.expr | None) -> list[str]:
    """The words a value is made of.

    A word is a string literal: the value itself, an element of a set of
    words, the literal parts of an f-string, or the argument of a
    set-like call (``frozenset({...})``).  Anything else -- a call whose
    result is not built out of literals -- contributes no word, because
    the gate reads the code, not what the code computes.
    """
    if value is None:
        return []
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        words: list[str] = []
        for element in value.elts:
            words.extend(_string_words(element))
        return words
    if isinstance(value, ast.JoinedStr):
        return [
            "".join(
                part.value
                for part in value.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        ]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _SET_LIKE
        and len(value.args) == 1
        and not value.keywords
    ):
        return _string_words(value.args[0])
    return []


def _reason_key(node: ast.expr | None) -> str | None:
    """The reason slot a subscript writes to, if it writes to one."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in _REASON_SLOTS
    ):
        return node.value
    return None


def _parameters_with_defaults(
    args: ast.arguments,
) -> Iterator[tuple[ast.arg, ast.expr]]:
    """Every parameter of a function that has a default."""
    positional = list(args.posonlyargs) + list(args.args)
    for argument, default in zip(positional, args.defaults):
        yield argument, default
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            yield argument, default


def _reason_words(tree: ast.Module, *, strict: bool) -> list[tuple[int, str, str]]:
    """Every reason word the module writes, as ``(lineno, source, word)``.

    A *word* is a string literal that reaches a reason slot:

    * a constant whose name says it holds a reason -- ``REASONS``,
      ``WAITING_FOR_SLOT_REASON``, ``PROTECTED_REASONS``;
    * (strict only) a dictionary value under the key ``reason`` or
      ``pause_reason``;
    * (strict only) a keyword argument named ``reason`` or
      ``pause_reason``;
    * (strict only) a parameter default named ``reason`` or
      ``pause_reason``;
    * (strict only) a subscript write to ``entry["reason"]``.

    ``strict`` is on for the scheduler package, where a reason is a
    scheduler word by definition, and off for the framework-fix package,
    whose other ``reason=`` arguments are prose -- the semantic review's
    sentence for why two failures are not the same defect.  A sentence is
    not a word.
    """
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue

        for target in targets:
            if isinstance(target, ast.Name) and _is_reason_constant(target.id):
                for word in _string_words(value):
                    found.append((node.lineno, target.id, word))
            elif strict and isinstance(target, ast.Subscript):
                if _reason_key(target.slice) is not None:
                    for word in _string_words(value):
                        found.append((node.lineno, "a reason slot", word))

    if not strict:
        return found

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if _reason_key(key) is not None:
                    for word in _string_words(value):
                        found.append((node.lineno, "a reason slot", word))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _REASON_SLOTS:
                    for word in _string_words(keyword.value):
                        found.append((node.lineno, f"{keyword.arg}=", word))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument, default in _parameters_with_defaults(node.args):
                if argument.arg in _REASON_SLOTS:
                    for word in _string_words(default):
                        found.append((node.lineno, f"{argument.arg}=", word))
    return found


def _find_reason_violations(
    parsed: Sequence[tuple[Path, ast.Module]], *, strict: bool
) -> list[str]:
    """Every reason word written outside the closed vocabulary."""
    violations: list[str] = []
    for path, tree in parsed:
        for lineno, source, word in _reason_words(tree, strict=strict):
            if word in REASONS:
                continue
            violations.append(
                f"{_relative(path)}:{lineno}: {source} writes the reason word {word!r}"
            )
    return violations


def _shell_is_off(value: ast.expr) -> bool:
    """True when the value is provably *not* a shell.

    ``shell=False`` and ``shell=None`` are off.  Anything else -- a
    literal ``True``, a flag, an attribute -- is a shell the gate cannot
    prove is closed, and the lane is not allowed to open one.
    """
    if isinstance(value, ast.Constant):
        return value.value is False or value.value is None
    return False


def _find_shell_violations(
    parsed: Sequence[tuple[Path, ast.Module]]
) -> list[str]:
    """Every call that hands its command line to a shell.

    Two shapes are a shell: the ``shell=`` keyword, which turns an argv
    list into a program, and the ``os`` members, which take a command
    *string* in the first place.  The gate does not run the module, so a
    shell whose value cannot be read off the tree is a shell it has to
    report.
    """
    violations: list[str] = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg != "shell":
                        continue
                    if _shell_is_off(keyword.value):
                        continue
                    violations.append(
                        f"{_relative(path)}:{node.lineno}: a call passes shell="
                    )
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in SHELL_ESCAPE_MEMBERS
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                violations.append(
                    f"{_relative(path)}:{node.lineno}: calls os.{node.attr}"
                )
    return violations


def _names_the_engine(name: str) -> bool:
    """True when *name* is the scheduler engine, or a path to it."""
    return name == _FORBIDDEN_ENGINE_MODULE or name.endswith(
        "." + _FORBIDDEN_ENGINE_MODULE
    )


def _find_dependency_violations(
    parsed: Sequence[tuple[Path, ast.Module]]
) -> list[str]:
    """Every import of the scheduler engine from the framework-fix package.

    The static imports are read off the tree.  The dynamic ones --
    ``importlib.import_module("plan_scheduler.engine")`` and
    ``__import__("plan_scheduler.engine")`` -- are read off the tree as
    well, because a dependency that is only written down at run time is
    still a dependency.
    """
    violations: list[str] = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _names_the_engine(alias.name):
                        violations.append(
                            f"{_relative(path)}:{node.lineno}: imports {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _names_the_engine(module):
                    violations.append(
                        f"{_relative(path)}:{node.lineno}: imports from {module}"
                    )
                elif module.rsplit(".", 1)[-1] == "plan_scheduler" and any(
                    alias.name == "engine" for alias in node.names
                ):
                    violations.append(
                        f"{_relative(path)}:{node.lineno}: imports the engine "
                        f"from {module}"
                    )
            elif isinstance(node, ast.Call):
                function = node.func
                dynamic = (
                    isinstance(function, ast.Attribute)
                    and function.attr == "import_module"
                ) or (isinstance(function, ast.Name) and function.id == "__import__")
                if not dynamic or not node.args:
                    continue
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _names_the_engine(first.value)
                ):
                    violations.append(
                        f"{_relative(path)}:{node.lineno}: imports {first.value} "
                        f"at run time"
                    )
    return violations


def _find_state_json_violations(
    parsed: Sequence[tuple[Path, ast.Module]]
) -> list[str]:
    """Every mention of the legacy state JSON in the lane's own code.

    Naming the file is the violation: the lane has no business with the
    sidecar, whether it writes it, reads it or merely knows its name.
    The pinned constant that carries the name is caught as well, so the
    lane cannot reach the file through the module that owns it.
    """
    violations: list[str] = []
    for path, tree in parsed:
        for node, text in _code_strings(tree):
            if STATE_JSON in text:
                violations.append(
                    f"{_relative(path)}:{node.lineno}: names the legacy state JSON"
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == STATE_JSON_CONSTANT:
                violations.append(
                    f"{_relative(path)}:{node.lineno}: uses the legacy state "
                    f"filename constant"
                )
    return violations


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def test_results_table_append_only(tmp_path: Path) -> None:
    """No code rewrites a row of ``plan_verification_result``.

    The table holds one row per (plan, verification point, tree).  A
    re-run after a repair is merged appends the row for the new tree; the
    row for the old tree stays, because "this failed here" is the reason
    the repair exists.  A verdict that can be updated in place is a
    verdict that can be made to agree with whatever the tree looks like
    afterwards.

    The scan is repository-wide: the contract belongs to the table, and a
    mutation written outside the lane's packages is still a mutation.
    """
    contents, read_failures = _read_files(_repo_python_files())
    assert not read_failures, (
        "the repository holds Python files that cannot be read; a gate "
        "that cannot read a file cannot say the file is clean:\n"
        + "\n".join(read_failures)
    )

    parsed, parse_failures = _parse_files(_result_table_candidates(contents))
    assert not parse_failures, (
        "a file that names the result table does not parse; a gate that "
        "cannot read a file cannot say the file is clean:\n"
        + "\n".join(parse_failures)
    )

    violations = _find_result_table_mutations(parsed)
    assert not violations, (
        "a plan_verification_result row is rewritten. The table is "
        "append-only: a re-run appends a row, it never updates or "
        "deletes one.\n" + "\n".join(violations)
    )

    # The negative control: the check has to fire on a statement that
    # really does rewrite a row.
    for name, statement in (
        ("planted_update.py", _MUTATING_UPDATE),
        ("planted_delete.py", _MUTATING_DELETE),
    ):
        planted = tmp_path / name
        planted.write_text(
            "conn.execute(\n    " + repr(statement) + "\n)\n", encoding="utf-8"
        )
        planted_parsed, planted_failures = _parse_files([planted])
        assert not planted_failures
        assert _find_result_table_mutations(planted_parsed), (
            f"the append-only check did not flag a planted {statement.split()[0]}; "
            "the check is not looking at the statements it claims to look at"
        )

    # And a file that names the table but does not parse is a failure,
    # not a skip.
    broken = tmp_path / "broken_candidate.py"
    broken.write_text(
        "TABLE = '" + RESULT_TABLE + "'\ndef oops(:\n    pass\n", encoding="utf-8"
    )
    broken_parsed, broken_failures = _parse_files([broken])
    assert not broken_parsed
    assert broken_failures, (
        "a file that does not parse was skipped instead of reported; the "
        "gate cannot certify a file it did not read"
    )


def test_reasons_closed(tmp_path: Path) -> None:
    """Every reason the scheduler writes is a word of the closed set.

    The set is closed on purpose: the panel, the dry run and the API all
    draw the same words, and a word outside the set is a sentence none of
    them can draw.  The lane adds words to the set deliberately (see
    ``plan_scheduler.policy.REASONS``); it does not write words outside
    it.
    """
    scheduler = _package_files(SCHEDULER_PACKAGE)
    framework_fix = _package_files(FRAMEWORK_FIX_PACKAGE)
    parsed, parse_failures = _parse_files(scheduler + framework_fix)
    assert not parse_failures, (
        "a file of the lane's own code does not parse:\n"
        + "\n".join(parse_failures)
    )

    scheduler_set = set(scheduler)
    violations = _find_reason_violations(
        [item for item in parsed if item[0] in scheduler_set], strict=True
    )
    violations += _find_reason_violations(
        [item for item in parsed if item[0] not in scheduler_set], strict=False
    )
    assert not violations, (
        "a reason outside the closed vocabulary was written. The panel, "
        "the dry run and the API draw the words of "
        "plan_scheduler.policy.REASONS and nothing else.\n"
        + "\n".join(violations)
    )

    # The negative control: a word outside the set has to be caught.
    planted = tmp_path / "planted_reason.py"
    planted.write_text(_PLANTED_REASON, encoding="utf-8")
    planted_parsed, planted_failures = _parse_files([planted])
    assert not planted_failures
    assert _find_reason_violations(planted_parsed, strict=True), (
        "the vocabulary check did not flag a planted reason word; the "
        "check is not looking at the words it claims to look at"
    )


def test_no_shell_true(tmp_path: Path) -> None:
    """No call in the lane's own code hands its command line to a shell.

    The lane runs commands whose output is the evidence for a verdict, so
    the command line is data.  A shell turns a command line into a
    program -- the command line decides what else runs.

    The check reads the parsed tree, so a module that *documents*
    ``shell=True`` is not a module that *calls* it, and the member-prefixed
    form (``subprocess.run(...)``) is caught like any other call.
    """
    parsed, parse_failures = _parse_files(_lane_files())
    assert not parse_failures, (
        "a file of the lane's own code does not parse:\n"
        + "\n".join(parse_failures)
    )

    violations = _find_shell_violations(parsed)
    assert not violations, (
        "the lane's code opens a shell. A repair's command line is data "
        "and must not be turned into a program.\n" + "\n".join(violations)
    )

    # The negative control: a shell call has to be caught.
    planted = tmp_path / "planted_shell.py"
    planted.write_text(_PLANTED_SHELL, encoding="utf-8")
    planted_parsed, planted_failures = _parse_files([planted])
    assert not planted_failures
    assert _find_shell_violations(planted_parsed), (
        "the shell check did not flag a planted subprocess.run(..., "
        "shell=True); the check is not looking at the calls it claims to "
        "look at"
    )


def test_dependency_direction(tmp_path: Path) -> None:
    """``framework_fix`` does not import the scheduler engine.

    The engine is the seam that calls the lane (it admits, preempts and
    escalates on its behalf).  The lane calling the engine back would
    close a cycle, and a cycle between the two packages is an import
    that fails at start-up.
    """
    parsed, parse_failures = _parse_files(_package_files(FRAMEWORK_FIX_PACKAGE))
    assert not parse_failures, (
        "a file of the framework-fix package does not parse:\n"
        + "\n".join(parse_failures)
    )

    violations = _find_dependency_violations(parsed)
    assert not violations, (
        "the framework-fix package imports the scheduler engine; the "
        "dependency runs one way only (the engine calls the lane).\n"
        + "\n".join(violations)
    )

    # The negative control: the import has to be caught.
    planted = tmp_path / "planted_engine_import.py"
    planted.write_text(_PLANTED_ENGINE_IMPORT, encoding="utf-8")
    planted_parsed, planted_failures = _parse_files([planted])
    assert not planted_failures
    assert _find_dependency_violations(planted_parsed), (
        "the dependency check did not flag a planted import of "
        "plan_scheduler.engine; the check is not looking at the imports it "
        "claims to look at"
    )


def test_no_plan_state_json(tmp_path: Path) -> None:
    """The lane's own code does not write the legacy state JSON.

    SQLite is the single source of truth.  A ``plan_state.json`` written
    beside it would be a second one, and the two would disagree the
    moment one of them is written twice.
    """
    parsed, parse_failures = _parse_files(_lane_files())
    assert not parse_failures, (
        "a file of the lane's own code does not parse:\n"
        + "\n".join(parse_failures)
    )

    violations = _find_state_json_violations(parsed)
    assert not violations, (
        "the lane's code names the legacy state JSON. The state machine "
        "keeps the plan's state in SQLite; a JSON sidecar would be a "
        "second source of truth.\n" + "\n".join(violations)
    )

    # The negative control: naming the file has to be caught.
    planted = tmp_path / "planted_state_json.py"
    planted.write_text(_PLANTED_STATE_JSON, encoding="utf-8")
    planted_parsed, planted_failures = _parse_files([planted])
    assert not planted_failures
    assert _find_state_json_violations(planted_parsed), (
        "the state-JSON check did not flag a planted plan_state.json; the "
        "check is not looking at the code it claims to look at"
    )


def test_scan_surface_covers_the_repository(tmp_path: Path) -> None:
    """The scan reaches the whole repository, and reads every file it lists.

    Two things are pinned here, and both of them are about the gate
    rather than about the lane:

    1. **the surface is the repository.**  The file list holds the lane's
       own packages, the modules around them (``backend/agent.py``) and
       the scripts outside ``backend/`` as well -- a scan that only read
       one directory would leave the rest of the tree unchecked, and
       nothing in the result would say so.  The vendor trees (``.venv``,
       ``node_modules``) are not in it: they are not source.

    2. **a file that does not parse is a failure.**  A gate that skips
       what it cannot read passes over exactly the file it was written
       to catch, and the skip is invisible in the result.
    """
    files = _repo_python_files()
    names = {_relative(path) for path in files}

    assert "backend/framework_fix/registry.py" in names, (
        "the scan does not reach the framework-fix package; the gate is "
        "not looking at the code it was written for"
    )
    assert "backend/plan_scheduler/engine.py" in names, (
        "the scan does not reach the scheduler package"
    )
    assert "backend/agent.py" in names, (
        "the scan does not reach the executor"
    )
    assert any(name.startswith("scripts/") for name in names), (
        "the scan only reaches backend/; the surface is the repository"
    )

    vendor = [
        name
        for name in names
        if ".venv" in name.split("/") or "node_modules" in name.split("/")
    ]
    assert not vendor, (
        "the scan reads the vendor trees:\n" + "\n".join(sorted(vendor))
    )

    # Every file the scan lists is read -- none of them is dropped.
    contents, read_failures = _read_files(files)
    assert not read_failures, (
        "the scan could not read every file it listed:\n"
        + "\n".join(read_failures)
    )
    assert len(contents) == len(files), (
        "the scan listed "
        f"{len(files)} files and read {len(contents)} of them"
    )

    # A file that does not parse is reported, not skipped.
    broken = tmp_path / "broken.py"
    broken.write_text("def oops(:\n    pass\n", encoding="utf-8")
    broken_parsed, broken_failures = _parse_files([broken])
    assert not broken_parsed, "a file that does not parse was parsed anyway"
    assert broken_failures, (
        "a file that does not parse was skipped instead of reported; the "
        "gate cannot certify a file it did not read"
    )
