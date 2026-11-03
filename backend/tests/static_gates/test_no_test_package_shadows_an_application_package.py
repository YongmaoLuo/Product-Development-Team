"""Static gate: no test module may put a shadowing package on ``sys.path``.

Why this gate exists
--------------------
The application packages live under ``backend/``: ``plan_scheduler``,
``routes``, ``verification``, ``state_machine`` and the rest.  The test
tree lives under ``backend/tests/`` — and it uses some of the *same
names*: ``backend/tests/integration/plan_scheduler/``,
``backend/tests/unit/plan_scheduler/``,
``backend/tests/security/plan_scheduler/`` and so on, one directory per
CI lane, because the repository's lane convention gives every lane its own
``plan_scheduler/`` subdirectory.

Two directories with the same name are only a problem when one of them
reaches ``sys.path`` *ahead of* the other.  A test module that inserts
``backend/tests/integration`` at the front of ``sys.path`` makes
``import plan_scheduler`` resolve to the **test** directory
``backend/tests/integration/plan_scheduler/`` instead of the application
package ``backend/plan_scheduler/``.  That test package is an empty
``__init__.py`` with no ``pkgutil.extend_path`` fallback, so the
application's own submodules become unreachable:

    ModuleNotFoundError: No module named 'plan_scheduler.engine'

and *every* case in the collected module that imports the server dies with
it.  In the run this gate was written for, one such insertion turned a
CI shard red with **222 collection errors** across 39 files — and it did
so only because that shard happened to collect the offending module
before the modules that import the application.  The shard order is
re-shuffled on every run, so the same defect showed up as a different
fingerprint every time: an order-dependent defect that looks like a flaky
one.

The rule
--------
**A test module must not put a directory that shadows an application
package onto ``sys.path``.**

A *shadow* is a package that has the same name as an application package,
is not that application package, and has no ``pkgutil.extend_path``
fallback.  The fallback matters: ``backend/tests/security/plan_scheduler/``
and its siblings carry one, so while they shadow the application package
the application modules still import — the shadow is survivable, and the
repository depends on that convention.  A shadow *without* the fallback is
fatal, and only the fatal ones are reported here.

What this gate checks
---------------------
1. **No fatal shadow reaches ``sys.path``.**  Every ``sys.path.insert``
   and ``sys.path.append`` in ``backend/tests/`` and
   ``backend/state_machine/tests/`` is resolved to a concrete directory,
   and every package inside that directory is checked against the
   application packages.  A fatal shadow is reported with the file and
   the line that caused it.
2. **The inserted directory is not itself a shadow.**  Putting
   ``backend/state_machine/tests`` on ``sys.path`` would shadow the
   application package ``backend/tests`` in exactly the same way, and it
   is fatal for the same reason.
3. **The scan is not vacuous.**  A gate that cannot see the tree passes
   by saying nothing, so this file also pins that the scan reaches the
   insertions it claims to reach.

Scope, stated honestly
-----------------------
This gate reads the *source* of the test tree; it does not import it, and
it does not run the suite.  A ``sys.path`` insertion whose target cannot
be decided statically — a path built at runtime, from an environment
variable or from a function-local variable — is **skipped, not reported**.
That is deliberate: the gate must never report a violation it cannot
prove, and a false accusation in a gate every lane depends on is worse
than a missed insertion.  The price is that an insertion the resolver
cannot read is invisible here; the resolver is deliberately conservative
and is pinned by the case below.

What the resolver reads
-----------------------
The resolver reads the shapes this tree actually writes, because a blind
spot in the resolver is a blind spot in the gate:

* a module-level constant, however it is written — ``_X = ...`` or the
  annotated ``_X: Path = ...``;
* a module-level ``for`` loop variable, which names *every* directory
  its iterable can hold;
* a name that a function binds again is **not** read as the module-level
  constant: inside that function the local binding is what runs, so such
  an insertion is left undecided rather than accused.

Every shape the resolver *cannot* read is still skipped, and the case
below pins that the readable ones never fall back into it.
"""

from __future__ import annotations

import ast
import functools
import warnings
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

#: ``backend/`` -- this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The repository root -- the directory holding ``backend/``.
REPO_ROOT = BACKEND_DIR.parent

#: The trees that are scanned for ``sys.path`` insertions.  Both are test
#: trees: an application module that puts a directory on ``sys.path`` is
#: a different question, and the application has no such insertion.
SCAN_ROOTS: Tuple[Path, ...] = (
    BACKEND_DIR / "tests",
    BACKEND_DIR / "state_machine" / "tests",
)

#: The marker a shadow package has to carry for the shadow to be
#: survivable: it lets the application package's own modules be found
#: past the shadow.
_EXTEND_PATH_MARKER = "pkgutil.extend_path"


# ---------------------------------------------------------------------------
# The application packages
# ---------------------------------------------------------------------------


def application_packages() -> Dict[str, Path]:
    """Return the application packages, keyed by name.

    An application package is a direct child of ``backend/`` that carries
    an ``__init__.py``.  ``backend/`` itself is the top-level package and
    does not take part in the name comparison -- a shadow has to *replace*
    an application package, and nothing replaces ``backend`` itself.
    """
    packages: Dict[str, Path] = {}
    for child in sorted(BACKEND_DIR.iterdir()):
        if child.is_dir() and (child / "__init__.py").is_file():
            packages[child.name] = child.resolve()
    return packages


def _has_extend_path(package_dir: Path) -> bool:
    """Say whether the package carries the ``pkgutil.extend_path`` fallback."""
    init = package_dir / "__init__.py"
    if not init.is_file():
        return False
    return _EXTEND_PATH_MARKER in init.read_text(encoding="utf-8", errors="ignore")


def fatal_shadow_packages(directory: Path, apps: Dict[str, Path]) -> List[Path]:
    """Return the packages inside ``directory`` that fatally shadow an app package.

    A package inside ``directory`` is a fatal shadow when it has the same
    name as an application package, is not that application package, and
    carries no ``pkgutil.extend_path`` fallback.
    """
    shadows: List[Path] = []
    if not directory.is_dir():
        return shadows
    for child in sorted(directory.iterdir()):
        if not (child.is_dir() and (child / "__init__.py").is_file()):
            continue
        application = apps.get(child.name)
        if application is None:
            continue
        if child.resolve() == application:
            # It *is* the application package -- the directory was inserted
            # so that the application could be imported.
            continue
        if _has_extend_path(child):
            # Survivable: the application's own modules are still found.
            continue
        shadows.append(child)
    return shadows


def _is_itself_a_shadow(directory: Path, apps: Dict[str, Path]) -> bool:
    """Say whether the directory itself fatally shadows an application package."""
    application = apps.get(directory.name)
    if application is None:
        return False
    if directory.resolve() == application:
        return False
    return not _has_extend_path(directory)


# ---------------------------------------------------------------------------
# Resolving an inserted path
# ---------------------------------------------------------------------------


class _Resolver:
    """Resolve a path expression to the directories it can name.

    The resolver reads the module's own source: a module-level constant
    is resolved to the expression it was assigned, a ``Path(__file__)``
    chain is resolved against the file's own location, ``/ "name"``
    appends a path segment, and a loop variable is resolved to every
    directory its loop can bind.  Anything else -- a runtime-built path,
    a function-local variable, an environment lookup -- is left
    unresolved, and the caller skips it.

    Two shapes this tree uses constantly are readable, and the first
    version of this resolver could not read either of them:

    * **an annotated assignment** (``BACKEND_DIR: Path = ...``) is a
      module-level constant like any other;
    * **a loop variable** (``for _entry in (BACKEND_DIR, PROJECT_ROOT):``)
      names every directory its loop can bind.

    Between them they accounted for most of the insertions the gate
    could not see -- and a gate that cannot see an insertion cannot
    stop the shadow from coming back.  The blindness was invisible:
    the gate reported no violation, exactly as it does when the tree is
    clean.
    """

    #: How deep a chain of constants may go before the resolver gives up.
    _MAX_DEPTH = 12

    def __init__(self, path: Path, tree: ast.Module) -> None:
        self._path = path
        #: Every module-scope binding of a name, in source order.
        self._constants: Dict[str, List[ast.expr]] = {}
        #: The names bound by an annotated assignment.
        self._annotated: Set[str] = set()
        #: The module-scope ``for`` loops, keyed by the name they bind.
        self._loops: Dict[str, List[ast.For]] = {}
        self._collect(tree.body)

    # -- the module's own bindings ----------------------------------------

    def _collect(self, body: List[ast.stmt]) -> None:
        """Record the bindings a module-scope block makes.

        Functions and classes are **not** descended into: a name bound
        inside a function is local to it, and reading it as a
        module-level constant would put a directory on the gate's
        suspect list that the running code never puts on ``sys.path``.
        """
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._constants.setdefault(target.id, []).append(
                            node.value
                        )
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.value is not None:
                    self._constants.setdefault(node.target.id, []).append(
                        node.value
                    )
                    self._annotated.add(node.target.id)
            elif isinstance(node, ast.For):
                if isinstance(node.target, ast.Name):
                    self._loops.setdefault(node.target.id, []).append(node)
                self._collect(node.body)
                self._collect(node.orelse)
            elif isinstance(node, (ast.If, ast.With)):
                self._collect(node.body)
                self._collect(node.orelse)
            elif isinstance(node, ast.Try):
                self._collect(node.body)
                for handler in node.handlers:
                    self._collect(handler.body)
                self._collect(node.orelse)
                self._collect(node.finalbody)

    def loop_variables(self) -> Set[str]:
        """The names a module-scope ``for`` loop binds."""
        return set(self._loops)

    def annotated_names(self) -> Set[str]:
        """The names bound by an annotated assignment."""
        return set(self._annotated)

    # -- resolution -------------------------------------------------------

    def resolve(self, node: Optional[ast.expr]) -> Optional[Path]:
        """Return the one directory the expression names, or ``None``.

        An expression that names several directories -- a loop variable
        that can bind more than one -- has no single answer, so it is
        reported as unresolved here.  :meth:`resolve_candidates` is the
        form the gate uses.
        """
        candidates = self.resolve_candidates(node)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def resolve_candidates(
        self, node: Optional[ast.expr], depth: int = 0
    ) -> List[Path]:
        """Return every directory the expression can name.

        An empty list means "not decidable from the source" -- the
        caller skips it.  A list with more than one entry means the
        expression names several directories, and every one of them has
        to be safe.
        """
        if node is None or depth > self._MAX_DEPTH:
            return []
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value.startswith("/"):
                return [Path(node.value)]
            return []
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return [self._path]
            if node.id in self._loops:
                return self._loop_candidates(node.id, depth)
            found: List[Path] = []
            for value in self._constants.get(node.id, []):
                found.extend(self.resolve_candidates(value, depth + 1))
            return _unique(found)
        if isinstance(node, ast.Call):
            return self._resolve_call(node, depth)
        if isinstance(node, ast.Attribute):
            if node.attr == "parent":
                return [
                    candidate.parent
                    for candidate in self.resolve_candidates(node.value, depth + 1)
                ]
            return []
        if isinstance(node, ast.Subscript):
            return self._resolve_subscript(node, depth)
        if isinstance(node, ast.BinOp):
            return self._resolve_binop(node, depth)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            found = []
            for element in node.elts:
                found.extend(self.resolve_candidates(element, depth + 1))
            return _unique(found)
        if isinstance(node, ast.IfExp):
            # Both branches have to be readable.  If one of them cannot
            # be decided, the value on disk may be the undecidable one,
            # and reading only the branch that *can* be decided would let
            # a shadow through.
            body = self.resolve_candidates(node.body, depth + 1)
            orelse = self.resolve_candidates(node.orelse, depth + 1)
            if not body or not orelse:
                return []
            return _unique(body + orelse)
        return []

    # -- internals ---------------------------------------------------------

    def _loop_candidates(self, name: str, depth: int) -> List[Path]:
        """Every directory the loop variable can hold.

        A loop that rebinds its own variable is not readable: the value
        the insertion sees is then neither the one the loop started
        with nor the one the iterable held.
        """
        found: List[Path] = []
        for loop in self._loops.get(name, []):
            if _rebinds(loop, name):
                continue
            found.extend(self.resolve_candidates(loop.iter, depth + 1))
        return _unique(found)

    def _resolve_call(self, node: ast.Call, depth: int) -> List[Path]:
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("str", "Path") and node.args:
            return self.resolve_candidates(node.args[0], depth + 1)
        if isinstance(func, ast.Attribute):
            if func.attr == "resolve":
                return [
                    candidate.resolve()
                    for candidate in self.resolve_candidates(func.value, depth + 1)
                ]
            if func.attr == "dirname" and node.args:
                return [
                    candidate.parent
                    for candidate in self.resolve_candidates(node.args[0], depth + 1)
                ]
        return []

    def _resolve_subscript(self, node: ast.Subscript, depth: int) -> List[Path]:
        value = node.value
        if not (isinstance(value, ast.Attribute) and value.attr == "parents"):
            return []
        base = self.resolve_candidates(value.value, depth + 1)
        index = node.slice
        if not (isinstance(index, ast.Constant) and isinstance(index.value, int)):
            return []
        found: List[Path] = []
        for candidate in base:
            try:
                found.append(candidate.parents[index.value])
            except IndexError:
                return []
        return found

    def _resolve_binop(self, node: ast.BinOp, depth: int) -> List[Path]:
        if not isinstance(node.op, ast.Div):
            return []
        left = self.resolve_candidates(node.left, depth + 1)
        right = node.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return []
        return [candidate / right.value for candidate in left]


def _unique(paths: List[Path]) -> List[Path]:
    """Drop duplicates, keeping the order."""
    seen = set()
    found: List[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _rebinds(loop: ast.For, name: str) -> bool:
    """Say whether the loop body rebinds its own loop variable."""
    for block in (loop.body, loop.orelse):
        for statement in block:
            for node in ast.walk(statement):
                if isinstance(node, ast.Assign):
                    targets: List[ast.expr] = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    targets = [node.target]
                else:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return True
    return False


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _is_sys_path_call(node: ast.AST) -> bool:
    """Say whether the node is a ``sys.path.insert`` / ``sys.path.append`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("insert", "append")):
        return False
    owner = func.value
    return (
        isinstance(owner, ast.Attribute)
        and owner.attr == "path"
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "sys"
    )


def _path_argument(node: ast.Call) -> Optional[ast.expr]:
    """Return the argument that names the path being inserted.

    ``sys.path.insert(0, D)`` names it in the second argument;
    ``sys.path.append(D)`` names it in the first.
    """
    index = 1 if node.func.attr == "insert" else 0  # type: ignore[union-attr]
    if len(node.args) <= index:
        return None
    return node.args[index]


def scan_file(path: Path) -> Iterator[Tuple[int, Tuple[Path, ...]]]:
    """Yield ``(line number, candidate directories)`` for every insertion.

    The line number is the line of the insertion; the candidates are
    empty when the expression could not be decided statically.  More
    than one candidate means the expression names several directories
    (a loop variable), and every one of them has to be safe.
    """
    try:
        source = path.read_text(encoding="utf-8")
        with warnings.catch_warnings():
            # Some test modules carry docstrings with invalid escape
            # sequences.  Parsing them is not this gate's business, and
            # the warning would drown the report.
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return
    resolver = _Resolver(path, tree)
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if not _is_sys_path_call(node):
            continue
        argument = _path_argument(node)
        if _shadowed_by_a_local(tree, parents, node, argument):
            # The name the insertion reads is bound again inside the
            # function that contains it, so the module-level constant is
            # not what the running code uses.  Undecidable: skipped.
            yield node.lineno, ()
            continue
        yield node.lineno, tuple(resolver.resolve_candidates(argument))


def _parent_map(tree: ast.AST) -> Dict[int, ast.AST]:
    """Map the identity of every node to its parent."""
    parents: Dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _scope_bindings(scope: ast.AST) -> Set[str]:
    """The names a function scope binds.

    Nested functions and classes are not descended into: their names
    belong to their own scope and shadow nothing at the insertion's
    level.
    """
    names: Set[str] = set()
    arguments = getattr(scope, "args", None)
    if arguments is not None:
        for argument in (
            list(arguments.posonlyargs)
            + list(arguments.args)
            + list(arguments.kwonlyargs)
        ):
            names.add(argument.arg)
        if arguments.vararg is not None:
            names.add(arguments.vararg.arg)
        if arguments.kwarg is not None:
            names.add(arguments.kwarg.arg)
    stack: List[ast.AST] = list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _names_read_by(node: Optional[ast.expr]) -> Set[str]:
    """The names an expression reads."""
    if node is None:
        return set()
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _shadowed_by_a_local(
    tree: ast.AST,
    parents: Dict[int, ast.AST],
    node: ast.AST,
    argument: Optional[ast.expr],
) -> bool:
    """Say whether a local binding shadows the module-level one.

    A module-level constant that a function binds again is not what the
    code inside that function reads.  Reading it as the constant would
    put a directory on the suspect list that the running code never
    puts on ``sys.path`` -- an accusation the gate cannot prove.
    """
    read = _names_read_by(argument)
    if not read:
        return False
    current: Optional[ast.AST] = node
    while current is not None:
        parent = parents.get(id(current))
        if parent is None:
            return False
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if read & _scope_bindings(parent):
                return True
        current = parent
    return False


@functools.lru_cache(maxsize=None)
def scanned_files() -> Tuple[Path, ...]:
    """Return every file the scan reads."""
    found: List[Path] = []
    for root in SCAN_ROOTS:
        if root.is_dir():
            found.extend(sorted(root.rglob("*.py")))
    return tuple(found)


@functools.lru_cache(maxsize=None)
def insertions() -> Tuple[Tuple[Path, int, Tuple[Path, ...]], ...]:
    """Return every ``sys.path`` insertion in the scanned trees.

    The result is cached: the scan reads the whole test tree, and two
    cases ask for it.
    """
    found: List[Tuple[Path, int, Tuple[Path, ...]]] = []
    for path in scanned_files():
        for lineno, candidates in scan_file(path):
            found.append((path, lineno, candidates))
    return tuple(found)


def _undecided_shape(path: Path, lineno: int) -> Optional[str]:
    """Say which readable shape an undecided insertion is written in.

    Returns ``"loop variable"``, ``"annotated constant"``, or ``None``
    when the insertion is written in a shape that is not decidable from
    the source -- an environment lookup, a function-local variable, a
    conditional expression.  Those are skipped on purpose; the two
    shapes above are not, and an insertion in one of them that the
    resolver cannot read is a hole in the gate.
    """
    try:
        source = path.read_text(encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return None
    resolver = _Resolver(path, tree)
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno != lineno:
            continue
        if not _is_sys_path_call(node):
            continue
        argument = _path_argument(node)
        if _shadowed_by_a_local(tree, parents, node, argument):
            return None
        name: Optional[str] = None
        if isinstance(argument, ast.Name):
            name = argument.id
        elif (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Name)
            and argument.func.id in ("str", "Path")
            and argument.args
            and isinstance(argument.args[0], ast.Name)
        ):
            name = argument.args[0].id
        if name is None:
            return None
        if name in resolver.loop_variables():
            return "loop variable"
        if name in resolver.annotated_names():
            return "annotated constant"
        return None
    return None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_test_module_puts_a_shadowing_package_on_sys_path() -> None:
    """No test module may insert a directory that shadows an application package.

    The failure this pins is the one that cost a CI shard 222 collection
    errors: a test module that put ``backend/tests/integration`` at the
    front of ``sys.path`` made ``import plan_scheduler`` resolve to the
    lane's own empty ``plan_scheduler/`` package, and every case that
    imported the server died with ``ModuleNotFoundError: No module named
    'plan_scheduler.engine'``.

    The report names the file and the line, because "some module does
    this" is not actionable: the insertion is one line, and the fix is to
    import through the package path instead.
    """
    apps = application_packages()
    violations: List[str] = []
    for path, lineno, candidates in insertions():
        for directory in candidates:
            if not directory.is_dir():
                # Undecidable statically: skipped on purpose.  See the module
                # docstring -- a gate must not accuse what it cannot prove.
                continue
            shadows = fatal_shadow_packages(directory, apps)
            for shadow in shadows:
                violations.append(
                    "{0}:{1} puts {2} on sys.path, and {3} inside it shadows the "
                    "application package {4} (no pkgutil.extend_path fallback)".format(
                        path.relative_to(REPO_ROOT),
                        lineno,
                        directory.relative_to(REPO_ROOT),
                        shadow.relative_to(REPO_ROOT),
                        apps[shadow.name].relative_to(REPO_ROOT),
                    )
                )
            if _is_itself_a_shadow(directory, apps):
                violations.append(
                    "{0}:{1} puts {2} on sys.path, and that directory shadows the "
                    "application package {3} (no pkgutil.extend_path fallback)".format(
                        path.relative_to(REPO_ROOT),
                        lineno,
                        directory.relative_to(REPO_ROOT),
                        apps[directory.name].relative_to(REPO_ROOT),
                    )
            )

    assert not violations, (
        "a test module puts a shadowing package on sys.path:\n"
        + "\n".join("  - " + violation for violation in violations)
        + "\n\nThe application package is then unreachable in that process: "
        "'from plan_scheduler.engine import ...' raises "
        "ModuleNotFoundError. Import through the package path "
        "(from tests.integration.resume_test import restart) instead of "
        "inserting the parent directory onto sys.path."
    )


def test_the_scan_reaches_the_insertions_it_claims_to_reach() -> None:
    """The scan must actually see the tree -- a silent gate is no gate.

    A resolver that stopped understanding the source would report no
    violations at all, and this file would go green while the defect it
    exists for came back.  So the scan pins what it reaches: both test
    trees, a substantial number of insertions, and at least one directory
    that the resolver reads correctly.
    """
    for root in SCAN_ROOTS:
        assert root.is_dir(), "the scan root {0} does not exist".format(root)

    found = insertions()
    assert len(found) > 100, (
        "the scan found only {0} sys.path insertion(s) in {1} file(s) -- it "
        "is not reaching the tree it is meant to guard".format(
            len(found), len(scanned_files())
        )
    )

    resolved = {directory for _, _, candidates in found for directory in candidates}
    assert BACKEND_DIR.resolve() in resolved, (
        "the scan did not resolve the backend directory itself, so its "
        "resolver is not reading the module-level constants it is supposed "
        "to read"
    )


def test_the_resolver_reads_the_path_shapes_the_tree_uses() -> None:
    """The resolver must understand the shapes the tree actually writes.

    Each case below is one of the shapes that appear in the repository:
    a ``Path(__file__)`` chain, a ``.parents[N]`` chain, a constant that
    is built from another constant, and a ``/ "segment"`` append.  The
    resolver is what makes the gate able to see anything at all, so its
    own blind spots are pinned here rather than discovered later as a
    silently passing gate.
    """
    here = Path(__file__).resolve()
    cases = (
        ("_X = Path(__file__).resolve().parent\nstr(_X)", here.parent),
        ("_X = Path(__file__).resolve().parents[2]\nstr(_X)", here.parents[2]),
        ("_X = Path(__file__).parent.parent\nstr(_X)", here.parent.parent),
        ("_ROOT = Path(__file__).resolve().parents[3]\n_ROOT", here.parents[3]),
        (
            '_ROOT = Path(__file__).resolve().parents[3]\n'
            '_ROOT / "backend" / "tests"',
            here.parents[3] / "backend" / "tests",
        ),
        ("import os\nos.path.dirname(__file__)", here.parent),
    )
    for source, expected in cases:
        tree = ast.parse(source)
        resolver = _Resolver(Path(__file__).resolve(), tree)
        resolved = resolver.resolve(tree.body[-1].value)
        assert resolved is not None, "the resolver could not read {0!r}".format(source)
        assert resolved == expected, (
            "the resolver read {0!r} as {1}, not {2}".format(source, resolved, expected)
        )


def test_the_resolver_reads_an_annotated_constant() -> None:
    """``BACKEND_DIR: Path = ...`` is a module-level constant like any other.

    The tree writes this shape constantly -- an annotated assignment
    reads as a declaration, but to a resolver that only knows
    ``ast.Assign`` it does not exist at all, and every insertion that
    names it falls out of the gate's sight.
    """
    here = Path(__file__).resolve()
    source = "_X: Path = Path(__file__).resolve().parents[2]\nstr(_X)"
    tree = ast.parse(source)
    resolver = _Resolver(here, tree)
    resolved = resolver.resolve(tree.body[-1].value)
    assert resolved == here.parents[2], (
        "the resolver read the annotated constant as {0!r}".format(resolved)
    )
    assert "_X" in resolver.annotated_names(), (
        "the resolver did not record the annotated constant at all"
    )


def test_the_resolver_reads_a_loop_variable() -> None:
    """A loop variable names every directory its loop can bind.

    ``for _entry in (BACKEND_DIR, PROJECT_ROOT):`` is how a third of
    this tree's insertions are written.  A resolver that cannot read
    the loop sees neither the directories nor the loop, and the
    insertion stays invisible to the gate.
    """
    here = Path(__file__).resolve()
    source = (
        "_A = Path(__file__).resolve().parents[2]\n"
        "_B = _A.parent\n"
        "for _entry in (_A, _B):\n"
        "    sys.path.insert(0, str(_entry))\n"
    )
    tree = ast.parse(source)
    resolver = _Resolver(here, tree)
    candidates = resolver.resolve_candidates(
        _path_argument(tree.body[-1].body[0].value)
    )
    assert set(candidates) == {here.parents[2], here.parents[2].parent}, (
        "the resolver read the loop variable as {0!r}".format(candidates)
    )
    assert "_entry" in resolver.loop_variables(), (
        "the resolver did not record the loop variable at all"
    )


def test_the_resolver_refuses_a_loop_that_rebinds_its_own_variable() -> None:
    """A loop that rebinds its variable is not readable, and must be skipped.

    ``for _p in (...):`` followed by ``_p = something_else`` means the
    value the insertion sees is neither the one the loop started with
    nor the one the iterable held.  Guessing would put a directory on
    the suspect list that never reaches ``sys.path`` -- a false
    accusation, which is worse than a missed insertion.
    """
    here = Path(__file__).resolve()
    source = (
        "_A = Path(__file__).resolve().parents[2]\n"
        "for _p in (_A,):\n"
        "    _p = _A.parent\n"
        "    sys.path.insert(0, str(_p))\n"
    )
    tree = ast.parse(source)
    resolver = _Resolver(here, tree)
    assert resolver.resolve_candidates(
        _path_argument(tree.body[-1].body[1].value)
    ) == [], (
        "the resolver read a loop that rebinds its own variable"
    )


def test_the_scan_skips_an_insertion_that_a_local_binding_shadows(
    tmp_path: Path,
) -> None:
    """A local binding wins: the module-level constant is not what runs.

    ``_X = ...`` at module level and ``_X = ...`` again inside a
    function are two different directories.  The insertion inside the
    function puts the *local* one on ``sys.path``, so reading the
    module-level one would put a directory on the suspect list that the
    running code never puts there -- an accusation the gate cannot
    prove.  Such an insertion is therefore left undecided.
    """
    shadowed = tmp_path / "test_shadowed_by_a_local.py"
    shadowed.write_text(
        "_X = Path('/opt/module_level')\n"
        "def use():\n"
        "    _X = Path('/opt/local_level')\n"
        "    sys.path.insert(0, str(_X))\n",
        encoding="utf-8",
    )
    assert list(scan_file(shadowed)) == [(4, ())], (
        "the scan read a name that a local binding shadows: {0!r}".format(
            list(scan_file(shadowed))
        )
    )

    plain = tmp_path / "test_not_shadowed.py"
    plain.write_text(
        "_X = Path('/opt/module_level')\nsys.path.insert(0, str(_X))\n",
        encoding="utf-8",
    )
    assert list(scan_file(plain)) == [(2, (Path("/opt/module_level"),))], (
        "the scan stopped reading a module-level constant: {0!r}".format(
            list(scan_file(plain))
        )
    )


def test_the_resolver_reads_the_annotated_and_loop_shapes_the_tree_uses() -> None:
    """No insertion the tree writes in a readable shape may stay undecided.

    This is the gate's own blind spot, pinned.  An insertion the
    resolver cannot decide is skipped rather than reported -- that is
    the rule that keeps the gate from accusing what it cannot prove --
    but "skipped" must not become "unnoticed".  A *readable* shape
    (an annotated constant, a loop variable) that the resolver fails to
    read is a hole in the gate, and a hole in the gate is exactly how
    this defect comes back.

    The test is self-maintaining: it looks at the tree as it is, and
    fails when an insertion in one of these two shapes is left
    undecided -- whether because the resolver regressed or because
    somebody wrote a new one.
    """
    undecided: List[str] = []
    for path, lineno, candidates in insertions():
        if candidates:
            continue
        shape = _undecided_shape(path, lineno)
        if shape:
            undecided.append(
                "{0}:{1} writes a {2} the resolver cannot read".format(
                    path.relative_to(REPO_ROOT), lineno, shape
                )
            )
    assert not undecided, (
        "these insertions are written in a shape the resolver must be able "
        "to read, and are therefore invisible to the gate:\n"
        + "\n".join("  - " + item for item in undecided)
    )
