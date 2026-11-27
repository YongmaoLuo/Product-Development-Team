"""No merge resolution may leave a helper that nothing calls.

Why this gate exists
--------------------
A merge resolves a conflict by **keeping one side's version of a file**
and, when the two sides disagreed about more than the lines in the
hunk, by keeping a *mixture*: a function from one side, a body from the
other, a caller from neither.  The mixture compiles.  The mixture is
even plausible.  And the half that lost its caller is dead code that
reads like a contract: ``_park_needs_finishing`` said, in a long
docstring, that a restart has to finish a park the dying server left
half done -- and nothing in the tree ever asked it to.

Dead code is not the worst of it.  The worst of it is the reader: a
helper whose docstring states a rule looks like the rule, and the
behaviour it described is the behaviour a reader will believe the
system has.  The behaviour it described was **not** there -- the
mechanism that carried it was resolved to the other side of the merge,
and the helper was left behind as its ghost.

So this gate says the narrow, checkable thing: in the modules the merge
touched, a private module-level helper may not exist that nothing
calls.

What counts as a call
---------------------
A name counts as called when it is **used**: read as a name, or reached
as an attribute (``module._helper``).  Uses in the whole production
tree count, so a helper called from another module is not flagged.

The test tree does **not** count.  A helper that only the tests reach is
dead production code -- the tests are exercising a code path the
application never takes -- and that is exactly the residue this gate is
here for.

One reading is deliberately not a call: a helper that only calls
**itself** has no caller either.  Recursion is not wiring.

And one shape is deliberately not a call either: ``self._helper`` is a
method call on an object.  Two modules can each have a
``_current_phase``; the method in one of them says nothing about the
function in the other, and counting it would hide a real orphan
behind a namesake.  A cross-module call -- ``other_module._helper()``
-- is a call, and the gate counts it.

What the gate does not say
--------------------------
It says a helper has no caller.  It does not say the behaviour that
helper described is covered elsewhere -- that is a judgement about the
merge, made by a reader, and the two helpers this gate was written for
were read before they were deleted.  The gate is the cheap half of the
check; it is the half that cannot rot, because it is re-derived from
the source every run.

Why these two modules
---------------------
The rule is scoped to the modules the merge rewrote.  The rest of the
tree has private helpers that only the tests reach
(``_reset_for_tests`` and its cousins) -- a deliberate, separate
question, and not this gate's.  Widening the rule to the whole tree
would make it a rule about the tree rather than about the merge, and a
rule that fires everywhere is a rule nobody reads.

The decision logic is a pure function over (source, reference sites),
so the rule can be exercised with synthetic inputs; the gate that reads
the real tree is one call away from them.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

#: ``backend/`` -- this file lives at ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The modules the rule applies to, relative to :data:`BACKEND_DIR`.
#:
#: These are the two modules the merge rewrote.  The list is written
#: down rather than discovered, because a rule whose scope is computed
#: from the tree it judges answers a different question every time the
#: tree changes -- and a gate that silently widened its scope would
#: start failing on code this merge never touched.
RULE_MODULES: Tuple[str, ...] = (
    "routes/execution.py",
    "service_manager.py",
)

#: Directory names under ``backend/`` that never hold application
#: source: the test tree, the interpreter's caches and the virtualenv.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", "__pycache__", ".venv", ".pytest_cache", ".git", "node_modules"}
)

#: One reference site: the name that was used, and whether the use was
#: the name referring to itself.
Reference = Tuple[str, bool]


def _production_modules() -> List[Path]:
    """Every production module under ``backend/``, sorted.

    The walk prunes the excluded directories rather than filtering them
    out afterwards, so a virtualenv in the tree does not cost a full
    traversal of its thousands of files.
    """
    modules: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(BACKEND_DIR):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
        for name in sorted(filenames):
            if name.endswith(".py"):
                modules.append(Path(dirpath) / name)
    return modules


class _ReferenceCollector(ast.NodeVisitor):
    """Collect every name a module *uses*.

    A use is a name read (``ast.Name`` in load context) or an attribute
    reached (``ast.Attribute``).  A use that happens inside a definition
    of the **same** name is recorded as a self-reference: a helper that
    only calls itself has no caller, and recursion is not wiring.
    """

    def __init__(self) -> None:
        self.sites: List[Reference] = []
        self._scope: List[str] = []

    # -- scopes ---------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    # -- uses -----------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.sites.append((node.id, bool(self._scope) and self._scope[-1] == node.id))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # ``self._helper`` is a method call on an object -- it says
        # nothing about a module-level helper of the same name in
        # another module.  Two modules can each have a
        # ``_current_phase``, and the attribute access proves nothing
        # about the other one; counting it would hide a real orphan
        # behind a namesake.
        #
        # An attribute on anything else -- a module alias, an object --
        # is a reference to *something* with that name, and the gate
        # cannot tell which, so it counts.
        base = node.value
        if isinstance(base, ast.Name) and base.id in ("self", "cls"):
            self.generic_visit(node)
            return
        self.sites.append((node.attr, False))
        self.generic_visit(node)


def _reference_sites(modules: Iterable[Path]) -> List[Reference]:
    """Every name use across *modules*, with the self-references marked."""
    sites: List[Reference] = []
    for path in modules:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            # A module that does not parse is a broken module, and the
            # gate cannot read it.  Say which one, rather than letting
            # the traceback point at a line in a file nobody opened.
            raise AssertionError(
                "%s does not parse, so the gate cannot read it: %s"
                % (path, exc)
            ) from exc
        collector = _ReferenceCollector()
        collector.visit(tree)
        sites.extend(collector.sites)
    return sites


def _declared_private_names(source: str) -> List[Tuple[str, int]]:
    """The private module-level names *source* declares.

    Both kinds of declaration count: a function (or async function) and
    a module-level constant.  A constant that only the helper it was
    written for used is the same residue as the helper itself -- one
    level of indirection is not a difference in kind.
    """
    tree = ast.parse(source)
    declared: List[Tuple[str, int]] = []
    for node in tree.body:
        candidates: List[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates.append(node.name)
        elif isinstance(node, ast.Assign):
            candidates.extend(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            candidates.append(node.target.id)
        for name in candidates:
            if not name.startswith("_"):
                continue
            # A dunder is part of the language's own vocabulary
            # (``__all__``, ``__future__``), not a private helper.
            if name.startswith("__") and name.endswith("__"):
                continue
            declared.append((name, node.lineno))
    return declared


def _orphans(
    source: str, sites: Sequence[Reference]
) -> List[Tuple[str, int]]:
    """The private module-level names *source* declares that nothing calls.

    A name is called when it is used somewhere that is not a
    self-reference.
    """
    called: Set[str] = {name for name, is_self in sites if not is_self}
    return [
        (name, lineno)
        for name, lineno in _declared_private_names(source)
        if name not in called
    ]


def _rule_module_paths() -> List[Path]:
    """The modules the rule applies to, in the order they are written."""
    return [BACKEND_DIR / relative for relative in RULE_MODULES]


def _orphan_report() -> List[str]:
    """One line per orphaned private name, across the rule's modules."""
    sites = _reference_sites(_production_modules())
    report: List[str] = []
    for path in _rule_module_paths():
        source = path.read_text(encoding="utf-8")
        for name, lineno in _orphans(source, sites):
            report.append(
                "%s:%d: %s is defined here and nothing calls it"
                % (path.relative_to(BACKEND_DIR), lineno, name)
            )
    return report


def test_the_rule_modules_are_the_ones_the_merge_rewrote() -> None:
    """The scope of the rule is written down, not computed.

    A gate whose scope is derived from the tree it judges changes its
    mind every time the tree changes, and a reviewer cannot tell what
    it now covers.  The two modules are named here on purpose.
    """
    assert RULE_MODULES == (
        "routes/execution.py",
        "service_manager.py",
    )


def test_the_rule_modules_are_on_disk() -> None:
    """A rule that points at a file that moved is a rule that reads nothing."""
    for path in _rule_module_paths():
        assert path.exists(), "%s does not exist" % path
        assert path.is_file(), "%s is not a file" % path


def test_the_production_scan_is_not_empty() -> None:
    """The scan has to reach real files.

    A gate whose walk has drifted away from the tree passes vacuously.
    """
    modules = _production_modules()
    assert modules, "no production modules were found under %s" % BACKEND_DIR
    assert len(modules) > 1, "the production scan found a single file"


def test_no_private_helper_in_the_rule_modules_is_only_defined() -> None:
    """No private helper in the merge's modules is defined and never called.

    This is the gate.  A helper that only exists is the ghost of a
    contract: its docstring states a rule that nothing enforces.
    """
    report = _orphan_report()
    assert not report, (
        "these private names are defined and nothing calls them:\n%s\n"
        "A merge that resolved a call site away from its helper leaves the "
        "helper behind, and the helper's docstring then reads like a "
        "contract the system does not have. Wire it up or delete it."
        % "\n".join(report)
    )


def test_the_rule_flags_a_helper_nobody_calls() -> None:
    """The shape of the residue is flagged, and the report names it.

    The gate's own control: a rule that cannot fail is not a gate.
    """
    source = (
        "def _never_called() -> None:\n"
        "    return None\n"
        "\n"
        "\n"
        "def _called() -> None:\n"
        "    return None\n"
        "\n"
        "\n"
        "def caller() -> None:\n"
        "    _called()\n"
    )
    orphans = _orphans(source, _reference_sites_from_source(source))
    assert [name for name, _ in orphans] == ["_never_called"], (
        "the residue was not flagged, or the live helper was: %r" % orphans
    )


def test_the_rule_accepts_a_helper_that_is_called() -> None:
    """The counterweight: a helper with a caller is not flagged.

    Without this the gate would say "delete every private helper",
    which is not the rule -- the rule is about the ones with no
    caller.
    """
    source = (
        "def _called() -> None:\n"
        "    return None\n"
        "\n"
        "\n"
        "def caller() -> None:\n"
        "    _called()\n"
    )
    assert _orphans(source, _reference_sites_from_source(source)) == []


def test_a_helper_that_only_calls_itself_is_flagged() -> None:
    """Recursion is not wiring.

    A helper whose only use is its own body has no caller, and a gate
    that counted the recursion would let the residue through.
    """
    source = (
        "def _recursive(n: int) -> int:\n"
        "    if n <= 0:\n"
        "        return 0\n"
        "    return _recursive(n - 1)\n"
    )
    orphans = _orphans(source, _reference_sites_from_source(source))
    assert [name for name, _ in orphans] == ["_recursive"], (
        "a helper that only calls itself was not flagged: %r" % orphans
    )


def test_a_namesake_method_does_not_count_as_a_call() -> None:
    """``self._helper`` is a method call, not a call to a module helper.

    Two modules can each have a ``_current_phase``.  When the one that
    is *not* called is masked by the other's method, the gate would
    pass on a tree that carries an orphan -- and the orphan it misses
    is exactly the kind this gate was written for.
    """
    module = (
        "def _current_phase() -> str:\n"
        "    return 'phase'\n"
        "\n"
        "\n"
        "def caller() -> str:\n"
        "    return 'x'\n"
    )
    namesake = (
        "class Handler:\n"
        "    def _current_phase(self) -> str:\n"
        "        return 'phase'\n"
        "\n"
        "    def run(self) -> str:\n"
        "        return self._current_phase()\n"
    )
    sites = (
        _reference_sites_from_source(module)
        + _reference_sites_from_source(namesake)
    )
    orphans = _orphans(module, sites)
    assert [name for name, _ in orphans] == ["_current_phase"], (
        "a namesake method masked an orphan: %r" % orphans
    )


def test_a_module_alias_attribute_counts_as_a_call() -> None:
    """The counterweight to the rule above.

    ``other_module._helper()`` **is** a call to a module-level helper:
    the base is a module, not an object.  The rule that excludes
    ``self._helper`` must not exclude this one, or every cross-module
    call would be invisible and the gate would flag live code.
    """
    module = (
        "def _helper() -> None:\n"
        "    return None\n"
        "\n"
        "\n"
        "def caller() -> None:\n"
        "    other_module._helper()\n"
    )
    sites = _reference_sites_from_source(module)
    assert _orphans(module, sites) == [], (
        "a cross-module call was not counted as a call: %r"
        % _orphans(module, sites)
    )


def _reference_sites_from_source(source: str) -> List[Reference]:
    """The reference sites of one source text, without touching the disk."""
    collector = _ReferenceCollector()
    collector.visit(ast.parse(source))
    return collector.sites
