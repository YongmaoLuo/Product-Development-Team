"""Static gate: the ``postprocess`` package's module boundaries.

Background
----------
The post-processing (收尾) stage is split into modules, and the
architecture wrote the split down as a **direction of dependency**:

* ``spec.py`` loads and freezes the requirement book;
* ``executor.py`` runs the commands and produces evidence;
* ``judge.py`` decides PASS/FAIL from the predicate and the evidence;
* ``waits.py`` owns the waiting lifecycle;
* ``progress.py`` / ``report.py`` own the two on-disk artefacts; and
* ``orchestrator.py`` drives the loop that ties them together.

The decision says, in as many words, "模块依赖方向单向：``orchestrator →
(spec, executor, judge, waits, progress, report)``，``judge`` 与
``executor`` 互不依赖" — the dependency runs one way, and the judge and
the executor do not depend on each other.  The reason is structural
rather than stylistic: "执行器只产出数据对象…判定器只接受数据对象、不
import 执行器内部实现" — an executor that can reach the judge can grade
its own homework, and a judge that can reach the executor can be talked
into a verdict by the code that produced the evidence.

The same decision pins the tool boundary: "所有外部命令经唯一入口执行"
— every external command goes through one entry point — and the failure
notification goes out "经 plan 已绑定的 HITL 渠道投递", through the route
the plan already has, so the finishing stage grows no channel of its own.

Prose is not a gate
-------------------
Every one of those promises is a sentence in a design document, and a
sentence is checked by whoever happens to read it.  It decays the moment
the next edit is made under time pressure, and nothing in the suite goes
red when it does.  A behaviour test cannot close the gap either: it can
show that *today's* judge does not import *today's* executor, but it
cannot stop the next person from adding the import — and a test written
against today's judge would have to be rewritten to notice.

So this file turns the boundaries into **structure**.  It parses the
package with :mod:`ast` and pins:

1. :func:`test_judge_does_not_import_executor` — the judge reaches no
   executor, orchestrator or waits module;
2. :func:`test_executor_does_not_import_judge` — and the dependency does
   not point back either;
3. :func:`test_judge_does_not_read_conclusion_fields` — the judge reads
   no self-reported field (``conclusion`` / ``agent_says`` / ``summary``);
4. :func:`test_no_shell_true_or_os_system` — no shell escape anywhere in
   the package;
5. :func:`test_no_new_network_client_in_postprocess` — no HTTP client in
   the package;
6. :func:`test_no_direct_feishu_or_telegram_delivery` — no second
   delivery channel;
7. :func:`test_only_the_executor_spawns_subprocess` — one command entry,
   and it is the executor's.

Why an AST and not a substring scan
-----------------------------------
A substring scan for ``"shell=True"`` cannot tell a call from a docstring
that quotes one, and a scan for ``"os.system"`` matches a helper whose
*name* ends in ``system``.  A gate that cries wolf over a naming
coincidence gets worked around — the deny-list grows an exemption, the
exemption grows a hole — and a gate that is worked around protects
nothing.  So the checks below work on the parsed tree: a *call* is a
call, an *import* is an import, and a sentence that merely mentions a
forbidden name is prose.

What the gate does **not** claim
--------------------------------
* It is a **direct-import** gate.  It sees the package's own modules,
  not their transitive closure.
* The banned-field rule matches a field name **exactly**.  A docstring
  that says "no summary field" is prose and is not flagged; a subscript
  that says ``record["summary"]`` is a read and is.
* ``shell=False`` is not a violation — it is the shape the executor is
  expected to use.  Only a shell that is not provably off is flagged.

Missing files fail, they never skip
------------------------------------
A gate whose target disappeared passes vacuously, which is the one
outcome a boundary gate must never have.  Every helper here raises
:class:`AssertionError` when a module is missing or unparseable, and
:func:`test_the_governed_modules_are_present_on_disk` says so out loud.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import FrozenSet, List, NamedTuple, Set, Tuple


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

#: The backend directory — this file lives two levels below it, in
#: ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The package this gate governs.  Only the modules directly in it are
#: scanned: the boundary is about the package's own structure, and the
#: tests that exercise it are not part of the package.
POSTPROCESS_DIR = BACKEND_DIR / "postprocess"

#: The two modules the dependency-direction rules are about.
JUDGE_MODULE = POSTPROCESS_DIR / "judge.py"
EXECUTOR_MODULE = POSTPROCESS_DIR / "executor.py"


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: The modules the judge may not reach.  ``executor`` is the one the
#: architecture names ("judge 与 executor 互不依赖"); ``orchestrator`` and
#: ``waits`` are the other two edges that would let a verdict be reached
#: from outside the evidence.
#:
#: Leaf names, so a relative import (``from .executor import ...``) and
#: an absolute one (``from postprocess.executor import ...``) are caught
#: by the same rule.
FORBIDDEN_JUDGE_IMPORTS: FrozenSet[str] = frozenset({
    "executor",
    "orchestrator",
    "waits",
})

#: The modules the executor may not reach.  The executor produces
#: evidence; a verdict is the judge's to make.
FORBIDDEN_EXECUTOR_IMPORTS: FrozenSet[str] = frozenset({
    "judge",
})

#: The fields a self-reported verdict would arrive in.  The judge may
#: read the evidence — the exit code and the two output streams — and
#: the fields it may not read are the ones a participant fills in about
#: its own work.
#:
#: These names are matched **exactly** against a string constant, a
#: subscript key, an attribute name or a keyword name.  A sentence that
#: merely mentions one of them is prose.
BANNED_SELF_REPORTED_FIELDS: FrozenSet[str] = frozenset({
    "conclusion",
    "agent_says",
    "summary",
})

#: The evidence fields the judge is supposed to read.  Pinned so the
#: ban above cannot be satisfied by a judge that reads nothing at all.
ALLOWED_EVIDENCE_FIELDS: FrozenSet[str] = frozenset({
    "exit_code",
    "stdout",
    "stderr",
    "external_status",
})

#: The shell escape hatches.  ``shell=True`` hands a command line to a
#: shell, and ``os.system`` / ``os.popen`` do the same without even
#: pretending to be an argv.
SHELL_ESCAPE_MEMBERS: FrozenSet[str] = frozenset({"system", "popen"})

#: The HTTP clients the package may not bring in.  The finishing stage
#: reads and writes files; it does not talk to the network.
NETWORK_CLIENT_ROOTS: FrozenSet[str] = frozenset({
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
})

#: The import roots that would mean the package grew a delivery channel
#: of its own.  ``notifications`` is this repository's existing
#: Feishu/Telegram client package (``backend/notifications/``); the
#: others are the SDKs those clients use.
DELIVERY_IMPORT_ROOTS: FrozenSet[str] = frozenset({
    "notifications",
    "feishu",
    "feishu_client",
    "feishu_notifier",
    "lark",
    "lark_oapi",
    "telegram",
    "telegram_client",
    "aiogram",
    "telebot",
})

#: The hosts a direct delivery would talk to.  A URL literal is how a
#: hand-rolled client looks before anyone imports an SDK.
DELIVERY_HOST_MARKERS: Tuple[str, ...] = (
    "open.feishu.cn",
    "openapi.feishu.cn",
    "api.telegram.org",
)

#: The one module allowed to spawn a process.  The architecture says
#: "所有外部命令经唯一入口执行" — one entry, and it is the executor's
#: ``CommandRunner``.
COMMAND_ENTRY_MODULE = "executor.py"


# ---------------------------------------------------------------------------
# Import records
# ---------------------------------------------------------------------------


class _Import(NamedTuple):
    """One ``import`` / ``from ... import ...`` statement."""

    #: The dotted module name (empty for ``from . import x``).
    module: str
    #: The name taken out of the module.
    symbol: str
    #: The line the statement is on.
    lineno: int
    #: The relative-import level; ``0`` means an absolute import.
    level: int
    #: True for a plain ``import x`` — the whole module is bound.
    plain: bool

    @property
    def leaf(self) -> str:
        """The module's own name (``postprocess.executor`` -> ``executor``)."""
        return self.module.rsplit(".", 1)[-1] if self.module else ""

    @property
    def root(self) -> str:
        """The package the import names (``urllib.request`` -> ``urllib``)."""
        return self.module.split(".")[0] if self.module else ""


def _imports(tree: ast.AST) -> List[_Import]:
    """Return every static import in ``tree``.

    Only ``ast.Import`` / ``ast.ImportFrom`` nodes are inspected.  A
    dynamic ``__import__("subprocess")`` would have to be found by
    executing the module, and a module that has to be executed to be
    checked is not the module this gate is about.
    """
    found: List[_Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(
                    _Import(alias.name, alias.name, node.lineno, 0, True)
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.append(
                    _Import(
                        node.module or "",
                        alias.name,
                        node.lineno,
                        node.level,
                        False,
                    )
                )
    return found


def imported_modules(tree: ast.AST) -> Set[str]:
    """The module names ``tree`` imports, as leaf names.

    A relative import names its target relative to the package, so
    ``from .executor import CommandRunner`` yields ``executor`` — the
    same name an absolute ``from postprocess.executor import ...``
    yields.  That is what lets one deny-list catch both spellings.
    """
    names: Set[str] = set()
    for imp in _imports(tree):
        if imp.module:
            names.add(imp.leaf)
        elif imp.level:
            # ``from . import executor`` — the alias names the module.
            names.add(imp.symbol.rsplit(".", 1)[-1])
    return names


def imported_root_packages(tree: ast.AST) -> Set[str]:
    """The root packages ``tree`` imports.

    Only absolute imports have a root: a relative import names a module
    inside this package, and ``from . import executor`` is the package's
    own module rather than a third-party dependency.
    """
    roots: Set[str] = set()
    for imp in _imports(tree):
        if not imp.level and imp.module:
            roots.add(imp.root)
    return roots


# ---------------------------------------------------------------------------
# Violation collectors — one per rule, each usable on any tree
# ---------------------------------------------------------------------------


def _forbidden_import_violations(
    tree: ast.AST, forbidden: FrozenSet[str]
) -> List[str]:
    """One message per import of a forbidden module."""
    violations: List[str] = []
    for imp in _imports(tree):
        leaf = imp.leaf
        if not leaf and imp.level:
            leaf = imp.symbol.rsplit(".", 1)[-1]
        if leaf in forbidden:
            violations.append(
                f"line {imp.lineno}: imports {leaf!r}, which is forbidden here"
            )
    return violations


def _banned_field_violations(
    tree: ast.AST, banned: FrozenSet[str]
) -> List[str]:
    """One message per read of a banned field.

    Four shapes count as a read: a string constant that *is* the field
    name (a subscript key or a ``getattr`` argument), an attribute of
    that name, and a keyword argument of that name.  Prose is not a
    read, so a docstring that merely mentions a field name is not
    flagged.
    """
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in banned:
                violations.append(
                    f"line {getattr(node, 'lineno', 0)}: reads the field "
                    f"{node.value!r}"
                )
        elif isinstance(node, ast.Attribute) and node.attr in banned:
            violations.append(
                f"line {node.lineno}: reads the attribute {node.attr!r}"
            )
        elif isinstance(node, ast.keyword) and node.arg in banned:
            violations.append(
                f"line {node.lineno}: passes the keyword {node.arg!r}"
            )
    return violations


def _shell_escape_violations(tree: ast.AST) -> List[str]:
    """One message per shell escape hatch.

    ``shell=False`` is fine — it is the shape the executor is expected
    to use.  ``shell=True`` is not, and neither is a shell whose value
    cannot be read off the tree: the gate does not run the module, so
    "provably off" is the only thing it can accept.
    """
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and value.value is False:
                    continue
                violations.append(
                    f"line {node.lineno}: passes shell=... that is not "
                    f"provably False"
                )
        elif isinstance(node, ast.Attribute) and node.attr in SHELL_ESCAPE_MEMBERS:
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                violations.append(
                    f"line {node.lineno}: calls os.{node.attr}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in SHELL_ESCAPE_MEMBERS:
                    violations.append(
                        f"line {node.lineno}: imports os.{alias.name} "
                        f"directly"
                    )
    return violations


def _root_package_violations(
    tree: ast.AST, forbidden_roots: FrozenSet[str]
) -> List[str]:
    """One message per import of a forbidden root package."""
    violations: List[str] = []
    for imp in _imports(tree):
        if imp.level or not imp.module:
            continue
        if imp.root in forbidden_roots:
            violations.append(
                f"line {imp.lineno}: imports {imp.module!r}"
            )
    return violations


def _delivery_url_violations(tree: ast.AST) -> List[str]:
    """One message per URL literal pointing at a delivery host."""
    violations: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for marker in DELIVERY_HOST_MARKERS:
            if marker in node.value:
                violations.append(
                    f"line {getattr(node, 'lineno', 0)}: carries the "
                    f"delivery host {marker!r}"
                )
                break
    return violations


# ---------------------------------------------------------------------------
# Reading the package
# ---------------------------------------------------------------------------


def _module_paths() -> List[Path]:
    """The package's own modules — the files directly in the package.

    A test file is not one of them: the boundary is about the product
    code's structure, and the tests that exercise it live elsewhere.
    """
    return sorted(POSTPROCESS_DIR.glob("*.py"))


def _parse(path: Path) -> ast.Module:
    """Parse ``path``, failing loudly when it is not valid Python."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a broken module
        raise AssertionError(
            f"{path.name} does not parse as Python: {exc}"
        ) from exc


def _tree(path: Path) -> ast.Module:
    """The parsed tree of one module, with the module's existence checked."""
    assert path.is_file(), (
        f"{path.name} is not on disk — this gate cannot check the "
        f"boundaries of a module that is not there.  If it was renamed "
        f"or moved, update this file."
    )
    return _parse(path)


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------


def test_the_governed_modules_are_present_on_disk() -> None:
    """The modules the gate is about exist and parse.

    A gate whose target disappeared passes vacuously, and a vacuous
    boundary gate is worse than no gate: it reports green while the
    boundary it was written to protect has been moved.
    """
    for path in (JUDGE_MODULE, EXECUTOR_MODULE):
        assert path.is_file(), (
            f"{path.name} is missing from {POSTPROCESS_DIR.name}/ — the "
            f"gate fails instead of skipping, so the gap stays visible"
        )
        _parse(path)

    assert _module_paths(), (
        f"{POSTPROCESS_DIR} holds no Python modules — the scan would "
        f"pass on an empty result"
    )


def test_the_scan_covers_the_package_modules_only() -> None:
    """Only the package's own modules are scanned — no tests, no nested dirs."""
    paths = _module_paths()

    assert all(path.parent == POSTPROCESS_DIR for path in paths), (
        "the scan reached outside the package directory"
    )
    assert not any(path.name.startswith("test_") for path in paths), (
        "a test file was picked up by the package scan — the boundary is "
        "about the product code, not about the tests that check it"
    )
    assert {path.name for path in paths} >= {"judge.py", "executor.py"}, (
        "the package scan does not cover both the judge and the executor"
    )


# ---------------------------------------------------------------------------
# Rule 1 -- the dependency direction
# ---------------------------------------------------------------------------


def test_judge_does_not_import_executor() -> None:
    """The judge reaches no executor, orchestrator or waits module.

    The architecture's reason for the separation is structural: the
    executor produces the evidence and the judge decides, and a judge
    that can call the executor is a judge that can be talked into a
    verdict by the code that produced the evidence.
    """
    tree = _tree(JUDGE_MODULE)
    violations = _forbidden_import_violations(tree, FORBIDDEN_JUDGE_IMPORTS)

    assert not violations, (
        "judge.py imports a module it must not depend on — "
        + "; ".join(violations)
        + ".  The judge takes (acceptance_predicate, evidence records) "
        "and nothing else; an executor that can reach the judge can "
        "grade its own homework."
    )


def test_executor_does_not_import_judge() -> None:
    """The dependency does not point back either.

    The two modules are peers: the executor produces evidence records
    and the judge consumes them.  An executor that imports the judge
    can decide, for itself, that its own evidence is good.
    """
    tree = _tree(EXECUTOR_MODULE)
    violations = _forbidden_import_violations(tree, FORBIDDEN_EXECUTOR_IMPORTS)

    assert not violations, (
        "executor.py imports a module it must not depend on — "
        + "; ".join(violations)
        + ".  The executor produces evidence; the verdict is the judge's "
        "to make."
    )


# ---------------------------------------------------------------------------
# Rule 2 -- the judge reads evidence, not conclusions
# ---------------------------------------------------------------------------


def test_judge_does_not_read_conclusion_fields() -> None:
    """The judge reads no self-reported field.

    "agent 自述不算证据" — what an agent says about its own work is not
    evidence.  The judge may read the exit code and the two output
    streams the command printed; it may not read a field where somebody
    wrote down what they believe the outcome was.
    """
    tree = _tree(JUDGE_MODULE)
    violations = _banned_field_violations(tree, BANNED_SELF_REPORTED_FIELDS)

    assert not violations, (
        "judge.py reads a self-reported field — "
        + "; ".join(violations)
        + ".  The judge decides from the acceptance predicate and the "
        "evidence records, never from a field where the executor "
        "described its own success."
    )


def test_judge_reads_the_allowed_evidence_fields() -> None:
    """The judge reads the evidence fields it is allowed to read.

    The rule above bans three names; this one says what the judge is
    there for, so the ban cannot be satisfied by a judge that reads
    nothing at all and calls every item failed.
    """
    constants = {
        node.value
        for node in ast.walk(_tree(JUDGE_MODULE))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    missing = sorted(ALLOWED_EVIDENCE_FIELDS - constants)
    assert not missing, (
        "judge.py no longer reads the evidence field(s) "
        f"{missing!r} — the judge decides from the exit code and the "
        "command output, and a judge that reads neither is not a judge"
    )


# ---------------------------------------------------------------------------
# Rule 3 -- no shell escape
# ---------------------------------------------------------------------------


def test_no_shell_true_or_os_system() -> None:
    """No module in the package hands a command line to a shell.

    The commands are argv arrays, always: the executor passes them to
    the process as a list and never lets a shell re-parse them.  A
    ``shell=True`` anywhere in the package would put that promise back
    in reach of a single keyword argument.
    """
    violations: List[str] = []
    for path in _module_paths():
        for reason in _shell_escape_violations(_tree(path)):
            violations.append(f"{path.name}: {reason}")

    assert not violations, (
        "the postprocess package grew a shell escape — "
        + "; ".join(violations)
        + ".  Commands go to the process as argv arrays; the only entry "
        "is the executor's CommandRunner."
    )


# ---------------------------------------------------------------------------
# Rule 4 -- no network client
# ---------------------------------------------------------------------------


def test_no_new_network_client_in_postprocess() -> None:
    """The package brings in no HTTP client.

    The finishing stage reads files and writes files.  A client in this
    package is either a delivery channel that was not authorised, or a
    decision that depends on what a remote host said.
    """
    violations: List[str] = []
    for path in _module_paths():
        tree = _tree(path)
        for reason in _root_package_violations(tree, NETWORK_CLIENT_ROOTS):
            violations.append(f"{path.name}: {reason}")

    assert not violations, (
        "the postprocess package imports a network client — "
        + "; ".join(violations)
        + ".  The finishing stage talks to the filesystem, not to the "
        "network."
    )


# ---------------------------------------------------------------------------
# Rule 5 -- no second delivery channel
# ---------------------------------------------------------------------------


def test_no_direct_feishu_or_telegram_delivery() -> None:
    """The package grows no delivery channel of its own.

    The failure notification goes out through the route the plan already
    has, handed to the notifier as a sink.  A client in this package
    would be a second set of credentials to leak and a second place for
    a message to go missing.
    """
    violations: List[str] = []
    for path in _module_paths():
        tree = _tree(path)
        for reason in _root_package_violations(tree, DELIVERY_IMPORT_ROOTS):
            violations.append(f"{path.name}: {reason}")
        for reason in _delivery_url_violations(tree):
            violations.append(f"{path.name}: {reason}")

    assert not violations, (
        "the postprocess package grew a direct delivery channel — "
        + "; ".join(violations)
        + ".  The notification is delivered through the sink the plan "
        "already has; this package must not reach Feishu or Telegram "
        "itself."
    )


# ---------------------------------------------------------------------------
# Rule 6 -- one command entry
# ---------------------------------------------------------------------------


def test_only_the_executor_spawns_subprocess() -> None:
    """Only the executor may spawn a process.

    "所有外部命令经唯一入口执行" — every external command goes through
    one entry, and the entry is ``executor.CommandRunner``: it checks the
    argv against the frozen requirement book, decides whether the action
    needs a human, and writes the audit triple.  A second spawn site is
    a command that was never checked, never approved and never audited.
    """
    offenders: List[str] = []
    for path in _module_paths():
        if path.name == COMMAND_ENTRY_MODULE:
            continue
        modules = imported_modules(_tree(path))
        if "subprocess" in modules:
            offenders.append(f"{path.name} imports subprocess")

    assert not offenders, (
        "a module other than the executor spawns a process — "
        + "; ".join(offenders)
        + ".  The command entry is CommandRunner, and it is the only "
        "one: it checks the argv, it routes the approval, it writes the "
        "audit triple."
    )


# ---------------------------------------------------------------------------
# Negative controls -- proof that the gate above is not vacuous
# ---------------------------------------------------------------------------


def test_the_gate_flags_a_judge_importing_the_executor() -> None:
    """A judge that imports the executor must be flagged."""
    tree = ast.parse("from .executor import CommandRunner\n")
    assert _forbidden_import_violations(tree, FORBIDDEN_JUDGE_IMPORTS), (
        "a relative import of the executor was not reported — the "
        "dependency rule is not working"
    )


def test_the_gate_flags_an_absolute_executor_import() -> None:
    """The absolute spelling of the same import must be flagged too."""
    tree = ast.parse("from postprocess.executor import CommandRunner\n")
    assert _forbidden_import_violations(tree, FORBIDDEN_JUDGE_IMPORTS), (
        "an absolute import of the executor was not reported — the "
        "dependency rule can be walked around by spelling the import "
        "differently"
    )


def test_the_gate_flags_an_executor_importing_the_judge() -> None:
    """An executor that imports the judge must be flagged."""
    tree = ast.parse("from postprocess.judge import judge\n")
    assert _forbidden_import_violations(tree, FORBIDDEN_EXECUTOR_IMPORTS), (
        "an import of the judge was not reported — the dependency rule "
        "is not working"
    )


def test_the_gate_flags_a_self_reported_field() -> None:
    """A read of a self-reported field must be flagged."""
    tree = ast.parse('value = record["conclusion"]\n')
    assert _banned_field_violations(tree, BANNED_SELF_REPORTED_FIELDS), (
        "reading the conclusion field was not reported — the field rule "
        "is not working"
    )


def test_the_gate_flags_an_attribute_read_of_a_self_reported_field() -> None:
    """The attribute spelling of the same read must be flagged too."""
    tree = ast.parse("value = record.agent_says\n")
    assert _banned_field_violations(tree, BANNED_SELF_REPORTED_FIELDS), (
        "reading the agent_says attribute was not reported — the field "
        "rule can be walked around by changing the spelling"
    )


def test_the_gate_does_not_flag_prose_about_a_field() -> None:
    """A sentence that mentions a field name is prose, not a read.

    The docstring of a module may well say "no summary field is read
    here".  Flagging that would train whoever edits the file next to
    phrase the documentation differently, and a gate that cries wolf
    over prose stops being a gate.
    """
    tree = ast.parse('"""This module reads no summary field."""\n')
    assert not _banned_field_violations(tree, BANNED_SELF_REPORTED_FIELDS), (
        "a docstring that mentions a field name was reported as a read "
        "of it — the check has degenerated into a substring scan"
    )


def test_the_gate_flags_shell_true() -> None:
    """A command handed to a shell must be flagged."""
    tree = ast.parse('subprocess.run(["ls"], shell=True)\n')
    assert _shell_escape_violations(tree), (
        "shell=True was not reported — the shell rule is not working"
    )


def test_the_gate_flags_an_undecidable_shell_flag() -> None:
    """A shell flag that cannot be read off the tree must be flagged.

    The gate does not run the module, so it cannot know what a variable
    holds.  "Provably off" is the only thing it can accept.
    """
    tree = ast.parse("subprocess.run(argv, shell=use_shell)\n")
    assert _shell_escape_violations(tree), (
        "a shell flag whose value is not a literal was not reported — "
        "the shell rule can be walked around by hiding the value in a "
        "variable"
    )


def test_the_gate_does_not_flag_shell_false() -> None:
    """``shell=False`` is the shape the executor is expected to use."""
    tree = ast.parse('subprocess.run(["ls"], shell=False)\n')
    assert not _shell_escape_violations(tree), (
        "shell=False was reported as a shell escape — the rule must "
        "only flag a shell that is not provably off"
    )


def test_the_gate_flags_os_system() -> None:
    """``os.system`` must be flagged."""
    tree = ast.parse('os.system("ls")\n')
    assert _shell_escape_violations(tree), (
        "os.system was not reported — the shell rule is not working"
    )


def test_the_gate_flags_a_directly_imported_os_system() -> None:
    """``from os import system`` must be flagged too."""
    tree = ast.parse("from os import system\n")
    assert _shell_escape_violations(tree), (
        "importing os.system directly was not reported — the shell rule "
        "can be walked around by changing the spelling"
    )


def test_the_gate_flags_a_network_client() -> None:
    """A network client must be flagged."""
    tree = ast.parse("import requests\n")
    assert _root_package_violations(tree, NETWORK_CLIENT_ROOTS), (
        "importing requests was not reported — the network rule is not "
        "working"
    )


def test_the_gate_flags_a_nested_network_client() -> None:
    """``urllib.request`` must be flagged by its root package."""
    tree = ast.parse("from urllib.request import urlopen\n")
    assert _root_package_violations(tree, NETWORK_CLIENT_ROOTS), (
        "importing urllib.request was not reported — the network rule "
        "can be walked around by importing the nested module"
    )


def test_the_gate_flags_a_delivery_client() -> None:
    """A Feishu or Telegram client must be flagged."""
    tree = ast.parse("from notifications.feishu_client import FeishuClient\n")
    assert _root_package_violations(tree, DELIVERY_IMPORT_ROOTS), (
        "importing the Feishu client was not reported — the delivery "
        "rule is not working"
    )


def test_the_gate_flags_a_delivery_url() -> None:
    """A hand-rolled delivery shows up as a URL literal first."""
    tree = ast.parse('URL = "https://open.feishu.cn/open-apis/im/v1/messages"\n')
    assert _delivery_url_violations(tree), (
        "a delivery URL was not reported — the delivery rule is not "
        "working"
    )


def test_the_gate_does_not_flag_a_plain_argv_call() -> None:
    """An argv call without a shell is what the executor is meant to do."""
    tree = ast.parse('subprocess.run(["ls", "-l"], shell=False)\n')
    assert not _shell_escape_violations(tree), (
        "an argv call was reported as a shell escape — the rule has "
        "degenerated into a substring scan"
    )