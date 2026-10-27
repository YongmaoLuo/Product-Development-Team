"""Static gate: the verification kernel's dependency set is pinned.

Background
----------
The verification architecture stacks two layers on top of each other:

* **kernel** — ``backend/verification/topic_relevance.py``,
  ``backend/verification/failure_fingerprint.py`` and
  ``backend/verification/scan.py``.  These are the *decisions*: "is this
  VP relevant to the plan's topic", "is this failure the same failure as
  last round", "how many VPs are deterministically executable".  The
  architecture states they are **pure functions**: same input, same
  output, in any process, on any machine.
* **orchestration** — the executor / sub-agent / agent / ``coding_tool``
  modules that actually run things: they shell out, read the environment,
  talk to the network, call models.

The kernel's promise was written down as prose.  ``scan.py``'s docstring
lists "no environment variable, no network request, no model call" as three
non-negotiable constraints, and ``failure_fingerprint.py``'s docstring
promises "no file read, no request, no clock, no ``hash()`` seed".  Prose
is not a gate.  It is checked by whoever happens to read it, it decays
the moment the next edit is made under time pressure, and nothing in the
suite fails when it does.

So this file turns the promise into assertions.  It reads the three kernel
modules and pins three claims:

1. :func:`test_imports_are_stdlib_only` — the kernel's imports are the
   standard library (plus the pinned same-layer primitives in
   :data:`ALLOWED_FIRST_PARTY`, and nothing else).
2. :func:`test_no_forbidden_calls` — the kernel makes none of the
   forbidden calls: ``open()``, ``os.environ``, ``subprocess``,
   ``requests``, ``coding_tool``.
3. :func:`test_no_orchestration_imports` — the kernel imports nothing from
   the orchestration layer.

Why an AST and not a substring scan
-----------------------------------
A substring scan for ``"open("`` flags ``_fail_open(`` in
``topic_relevance.py`` — a helper whose *name* ends in ``open`` and which
opens nothing.  A gate that cries wolf over a naming coincidence gets
worked around (the substring grows a deny-list, the deny-list grows an
exemption, the gate stops catching anything).  So the checks below work
on the parsed tree: a *call* is a call, an *import* is an import, and a
docstring that merely mentions ``subprocess`` is prose.

The same reasoning runs the other way: a keyword in a comment is not a
call, but a keyword in a *docstring* is also not a call, so neither is
reported.  What is reported is what the module would actually execute.

What the gate does **not** claim
--------------------------------
* It is a **direct-import** gate.  It sees the three kernel files, not
  their transitive closure.  The two first-party modules the kernel is
  allowed to import (:data:`ALLOWED_FIRST_PARTY`) do pull in more
  machinery of their own — that is why the allowlist pins *which symbols*
  the kernel may reach for (``validate_vp``; the masking primitives), not
  merely which modules it may name.  A kernel that started calling
  ``verification_ci_runner.run_ci_verification`` would be flagged; a
  kernel that keeps calling ``validate_vp`` would not.
* ``failure_fingerprint.py`` reaches ``tempfile.gettempdir()`` to learn
  the machine's temporary root, and that call does consult ``TMPDIR``.
  The temporary root is normalised to ``<tmp>`` before it reaches the
  hash, so the fingerprint stays machine-independent; the gate's rule is
  the narrower one the architecture actually states — *the kernel does not
  read the environment itself*.

Missing files fail, they never skip
------------------------------------
A gate whose target disappeared passes vacuously.  Every helper below
raises :class:`AssertionError` when a kernel module is missing or
unparseable, and :func:`test_kernel_modules_are_present_on_disk` says so
out loud.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, FrozenSet, List, NamedTuple, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

#: Repository root.  ``backend/tests/static_gates/`` is three levels deep.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The kernel modules this gate covers, relative to the repository root.
KERNEL_MODULES: Tuple[str, ...] = (
    "backend/verification/topic_relevance.py",
    "backend/verification/failure_fingerprint.py",
    "backend/verification/scan.py",
)


# ---------------------------------------------------------------------------
# Rule 1 -- the allowed import set
# ---------------------------------------------------------------------------

#: Every module the standard library provides.  ``dataclasses`` is listed
#: explicitly because the architecture decision that produced this gate
#: named it: the kernel's data carriers are dataclasses, and that is the
#: only shape they are allowed to take.
STDLIB_MODULES: FrozenSet[str] = frozenset(sys.stdlib_module_names) | {"dataclasses"}

#: The first-party imports each kernel module may make, mapped to the
#: symbols it may reach for in each of them.
#:
#: These three entries are the *only* places where the kernel reaches
#: outside the standard library, and each one is a deliberate, documented
#: reuse rather than a second implementation:
#:
#: * ``failure_classification`` already owns the path / hex / number
#:   masking primitives.  ``failure_fingerprint.py``'s docstring says so
#:   in as many words — "掩码**不另造第二套**" — and the pinned symbols are
#:   exactly the six primitives it borrows.  Copying them into the
#:   kernel would create the second set the docstring forbids.
#: * ``verification_api_runner.validate_vp`` and
#:   ``verification_ci_runner.validate_vp`` are the framework's own
#:   predicates.  ``scan.py``'s docstring explains why it calls them
#:   instead of writing its own: "口径**直接调用**那两个 runner 的
#:   ``validate_vp``" — the scan and the runner must not be able to drift
#:   apart.  Only the predicate is pinned; the runners' execution entry
#:   points are not reachable from the kernel.
#:
#: A first-party import that is not in this table, or a symbol that is not
#: in the pinned set, fails :func:`test_imports_are_stdlib_only`.  So does
#: a plain ``import <first-party module>`` — the kernel may name a module
#: only to take one specific symbol out of it.
ALLOWED_FIRST_PARTY: Dict[str, Dict[str, FrozenSet[str]]] = {
    "backend/verification/topic_relevance.py": {},
    "backend/verification/failure_fingerprint.py": {
        "failure_classification": frozenset({
            "_HEX_RUN_RE",
            "_NUMBER_RE",
            "_PATH_TOKEN_RE",
            "_TOKEN_SEP",
            "_mask_hex",
            "_normalize_path_token",
        }),
    },
    "backend/verification/scan.py": {
        "verification_api_runner": frozenset({"validate_vp"}),
        "verification_ci_runner": frozenset({"validate_vp"}),
    },
}


# ---------------------------------------------------------------------------
# Rule 2 -- the forbidden calls
# ---------------------------------------------------------------------------

#: Modules the kernel must never import or reference.  ``subprocess``
#: shells out, ``requests`` talks to the network, ``coding_tool`` calls a
#: model — the three things that make a decision non-reproducible.
FORBIDDEN_MODULES: Tuple[str, ...] = ("subprocess", "requests", "coding_tool")

#: The ways the process environment can be read or written through the
#: ``os`` module.  ``os.environ`` is the obvious one; ``os.getenv`` and
#: ``os.putenv`` are the same dependency wearing a different hat.
FORBIDDEN_ENVIRONMENT_MEMBERS: Tuple[str, ...] = ("environ", "getenv", "putenv")


# ---------------------------------------------------------------------------
# Rule 3 -- the orchestration layer
# ---------------------------------------------------------------------------

#: Modules that run things, drive models or schedule work.  A kernel that
#: imports one of them has stopped being a pure function: whatever that
#: module decides — which model, which environment, which moment — leaks
#: into the decision the kernel is supposed to make.
#:
#: ``orchestrator`` is listed because ``backend/verification/orchestrator.py``
#: is the verification package's own orchestration module and is reachable
#: from a kernel through a *relative* import, which carries no package
#: name of its own.
FORBIDDEN_ORCHESTRATION_MODULES: Tuple[str, ...] = (
    "agent",
    "base_executor",
    "bounded_subprocess",
    "ci_process_guard",
    "coding_tool",
    "executor",
    "orchestrator",
    "request_guard",
    "service_restart_agent",
    "verification_agent",
    "verification_executor",
    "verification_loop",
    "verification_phases",
    "verification_plan_delta",
    "verification_subagent",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Import(NamedTuple):
    """One ``import`` / ``from ... import ...`` statement."""

    #: The dotted module name (empty for ``from . import x``).
    module: str
    #: The name taken out of the module (``*`` for ``import *``).
    symbol: str
    #: The line the statement is on.
    lineno: int
    #: The relative-import level; ``0`` means an absolute import.
    level: int
    #: True for a plain ``import x`` — the whole module is bound, not one
    #: symbol out of it.
    plain: bool
    #: The local name the import binds (``import os as _os`` -> ``_os``).
    bound: str

    @property
    def is_relative(self) -> bool:
        """True for ``from . import x`` — an import with no package name."""
        return self.level > 0

    @property
    def top_level(self) -> str:
        """The package the import names (``os.path`` -> ``os``)."""
        return self.module.split(".")[0] if self.module else ""

    @property
    def last_component(self) -> str:
        """The module's own name (``verification.orchestrator`` -> ``orchestrator``)."""
        return self.module.split(".")[-1] if self.module else ""


def _kernel_sources() -> Dict[str, str]:
    """Return ``{relative path: source}`` for every kernel module.

    A missing module is a hard failure, never a skip: a gate whose target
    vanished passes vacuously, and the whole point of this gate is that it
    cannot pass vacuously.
    """
    sources: Dict[str, str] = {}
    missing: List[str] = []
    for rel in KERNEL_MODULES:
        path = REPO_ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        sources[rel] = path.read_text(encoding="utf-8")
    assert not missing, (
        "kernel module(s) missing on disk: "
        f"{missing!r}.  This gate cannot verify the dependency set of a "
        "module that is not there — it fails instead of skipping so the "
        "gap stays visible.  If a kernel module was renamed or moved, "
        "update KERNEL_MODULES in this file."
    )
    return sources


def _parse(rel: str, source: str) -> ast.Module:
    """Parse ``source``, failing loudly when it is not valid Python."""
    try:
        return ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - a broken kernel file
        raise AssertionError(
            f"{rel} does not parse as Python: {exc}"
        ) from exc


def _imports(tree: ast.AST) -> List[_Import]:
    """Return every static import in ``tree``.

    Only ``ast.Import`` / ``ast.ImportFrom`` nodes are inspected; a
    dynamic ``__import__("subprocess")`` would have to be found by
    executing the module, and a kernel that has to be executed to be
    checked is not the kernel this gate is about.
    """
    found: List[_Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(
                    _Import(
                        alias.name,
                        alias.name,
                        node.lineno,
                        0,
                        True,
                        alias.asname or alias.name.split(".")[0],
                    )
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
                        alias.asname or alias.name,
                    )
                )
    return found


def _bound_names(imports: Sequence[_Import], module: str) -> Set[str]:
    """Return the local names that bind ``module``.

    ``import os`` binds ``os``; ``import os as _os`` binds ``_os``.  Both
    have to be known, or the rule can be walked around by renaming the
    import — the kind of bypass a gate is supposed to make expensive.
    """
    names: Set[str] = set()
    for imp in imports:
        if imp.is_relative or not imp.plain:
            continue
        if imp.top_level == module:
            names.add(imp.bound)
    return names


def _forbidden_reasons(tree: ast.AST) -> List[str]:
    """Return one message per forbidden call or import found in ``tree``.

    The rules are the ones the architecture names: no ``open()``, no
    ``os.environ``, no ``subprocess``, no ``requests``, no ``coding_tool``.
    """
    reasons: List[str] = []
    imports = _imports(tree)

    for imp in imports:
        if imp.is_relative:
            continue
        if imp.top_level in FORBIDDEN_MODULES:
            reasons.append(
                f"line {imp.lineno}: imports the forbidden module "
                f"{imp.module!r}"
            )
        if (
            imp.top_level == "os"
            and imp.symbol in FORBIDDEN_ENVIRONMENT_MEMBERS
        ):
            reasons.append(
                f"line {imp.lineno}: imports the environment member "
                f"os.{imp.symbol}"
            )

    # The names the environment and the forbidden modules are reachable
    # under: the plain ones, plus whatever alias the import bound them to.
    os_names = _bound_names(imports, "os") | {"os"}
    forbidden_names: Dict[str, str] = {}
    for module in FORBIDDEN_MODULES:
        for alias in _bound_names(imports, module) | {module}:
            forbidden_names[alias] = module

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                reasons.append(f"line {node.lineno}: calls open(...)")
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                reasons.append(
                    f"line {node.lineno}: calls {func.attr}(...) on "
                    f"{ast.dump(func.value)[:60]}"
                )
        elif isinstance(node, ast.Attribute):
            value = node.value
            if not isinstance(value, ast.Name):
                continue
            if (
                value.id in os_names
                and node.attr in FORBIDDEN_ENVIRONMENT_MEMBERS
            ):
                reasons.append(
                    f"line {node.lineno}: reads os.{node.attr}"
                )
            if value.id in forbidden_names:
                reasons.append(
                    f"line {node.lineno}: calls "
                    f"{forbidden_names[value.id]}.{node.attr}(...)"
                )
        elif isinstance(node, ast.Name) and node.id in forbidden_names:
            reasons.append(
                f"line {node.lineno}: references "
                f"{forbidden_names[node.id]}"
            )

    return sorted(set(reasons))


def _orchestration_reasons(tree: ast.AST) -> List[str]:
    """Return one message per orchestration-layer import found in ``tree``."""
    reasons: List[str] = []
    for imp in _imports(tree):
        if imp.is_relative:
            # A relative import carries no package name, so the module's
            # own name is what identifies it (``from .orchestrator import
            # ...`` is the verification package's orchestrator).
            names = (imp.last_component,)
        else:
            names = (imp.top_level,)
        for name in names:
            if name in FORBIDDEN_ORCHESTRATION_MODULES:
                reasons.append(
                    f"line {imp.lineno}: imports the orchestration module "
                    f"{imp.module!r}"
                )
                break
    return sorted(set(reasons))


def _import_violations(rel: str, tree: ast.AST) -> List[str]:
    """Return one message per import that is outside the allowed set."""
    violations: List[str] = []
    allowed_first_party = ALLOWED_FIRST_PARTY.get(rel, {})
    for imp in _imports(tree):
        if imp.is_relative:
            violations.append(
                f"line {imp.lineno}: relative import {imp.module!r} — the "
                "kernel reaches its own package only through the "
                "standard library and the pinned first-party primitives"
            )
            continue
        top = imp.top_level
        if top in STDLIB_MODULES:
            continue
        if top not in allowed_first_party:
            violations.append(
                f"line {imp.lineno}: imports {imp.module!r}, which is not "
                "part of the standard library and is not a pinned "
                "same-layer primitive"
            )
            continue
        allowed_symbols = allowed_first_party[top]
        if imp.plain:
            violations.append(
                f"line {imp.lineno}: imports the whole module {top!r} — "
                "the kernel may only take the pinned symbols "
                f"{sorted(allowed_symbols)!r} out of it"
            )
            continue
        if imp.symbol not in allowed_symbols:
            violations.append(
                f"line {imp.lineno}: imports {imp.symbol!r} from {top!r} — "
                f"only {sorted(allowed_symbols)!r} are pinned"
            )
    return sorted(set(violations))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_kernel_modules_are_present_on_disk() -> None:
    """Every kernel module the gate covers must exist.

    The other three tests read these files; if one were missing they
    would fail anyway, but with a message about a parse failure or an
    empty import set.  This test names the actual problem.
    """
    missing = [
        rel for rel in KERNEL_MODULES if not (REPO_ROOT / rel).is_file()
    ]
    assert not missing, (
        f"kernel module(s) missing on disk: {missing!r}.  This gate "
        "fails rather than skips so the gap stays visible; if a kernel "
        "module was renamed or moved, update KERNEL_MODULES in this file."
    )


def test_imports_are_stdlib_only() -> None:
    """The kernel imports the standard library and nothing else.

    The kernel's decisions have to be reproducible in any process on any
    machine.  A third-party package, a sibling module that reads the
    environment, or a package that is not installed on the next machine
    all break that: the same input stops giving the same output.

    The only non-stdlib imports allowed are the pinned same-layer
    primitives in :data:`ALLOWED_FIRST_PARTY` — and only the pinned
    symbols of them.
    """
    violations: List[str] = []
    for rel, source in _kernel_sources().items():
        tree = _parse(rel, source)
        for message in _import_violations(rel, tree):
            violations.append(f"{rel}: {message}")
    assert not violations, (
        "the verification kernel must import the standard library (plus "
        "the pinned same-layer primitives in ALLOWED_FIRST_PARTY) and "
        "nothing else:\n  " + "\n  ".join(violations)
    )


def test_no_forbidden_calls() -> None:
    """The kernel makes none of the forbidden calls.

    ``open()`` — a decision function that reads a file is a decision
    function whose output depends on the file.  ``os.environ`` — the
    machine's configuration is not an argument.  ``subprocess`` — a shell
    out is not a pure function.  ``requests`` — a network call is not
    reproducible.  ``coding_tool`` — a model call is not deterministic.

    The check runs on the parsed tree, so ``_fail_open(...)`` — a helper
    whose name merely ends in ``open`` — is not a violation, and a
    docstring that mentions ``subprocess`` is prose rather than a call.
    """
    violations: List[str] = []
    for rel, source in _kernel_sources().items():
        tree = _parse(rel, source)
        for message in _forbidden_reasons(tree):
            violations.append(f"{rel}: {message}")
    assert not violations, (
        "the verification kernel must not call open(), read os.environ, "
        "or reach for subprocess / requests / coding_tool:\n  "
        + "\n  ".join(violations)
    )


def test_no_orchestration_imports() -> None:
    """The kernel imports nothing from the orchestration layer.

    The orchestration modules run commands, call models and read the
    environment.  A kernel that imports one has already given up on being
    a pure function of its arguments — the import is the moment the
    outside world gets a say in the decision.
    """
    violations: List[str] = []
    for rel, source in _kernel_sources().items():
        tree = _parse(rel, source)
        for message in _orchestration_reasons(tree):
            violations.append(f"{rel}: {message}")
    assert not violations, (
        "the verification kernel must not import the orchestration "
        "layer:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Negative controls -- proof that the gate above is not vacuous
# ---------------------------------------------------------------------------


def test_the_gate_flags_a_forbidden_import() -> None:
    """A kernel that imports ``subprocess`` must be flagged."""
    tree = ast.parse("import subprocess\n")
    assert _forbidden_reasons(tree), (
        "importing subprocess was not reported — the forbidden-call rule "
        "is not working"
    )


def test_the_gate_flags_a_forbidden_call() -> None:
    """A kernel that calls ``open()`` must be flagged."""
    tree = ast.parse('with open("plan.json") as handle:\n    pass\n')
    assert _forbidden_reasons(tree), (
        "calling open() was not reported — the forbidden-call rule is not "
        "working"
    )


def test_the_gate_flags_an_environment_read() -> None:
    """A kernel that reads ``os.environ`` must be flagged."""
    tree = ast.parse('root = os.environ["PDT_PROJECT_DIR"]\n')
    assert _forbidden_reasons(tree), (
        "reading os.environ was not reported — the forbidden-call rule is "
        "not working"
    )


def test_the_gate_flags_an_aliased_environment_read() -> None:
    """Renaming the import must not hide the environment read.

    ``import os as _os`` binds the same module under a different name.  A
    rule that only knows the name ``os`` can be walked around by a rename,
    which is exactly what a gate has to make expensive.
    """
    tree = ast.parse(
        'import os as _os\n'
        'root = _os.environ["PDT_PROJECT_DIR"]\n'
    )
    assert _forbidden_reasons(tree), (
        "reading os.environ through an alias was not reported — the "
        "forbidden-call rule can be walked around by renaming the import"
    )


def test_the_gate_flags_an_aliased_forbidden_call() -> None:
    """A renamed ``subprocess`` import must still be flagged."""
    tree = ast.parse(
        'import subprocess as sp\n'
        'sp.run(["ls"])\n'
    )
    assert _forbidden_reasons(tree), (
        "calling subprocess through an alias was not reported — the "
        "forbidden-call rule can be walked around by renaming the import"
    )


def test_the_gate_flags_a_non_stdlib_import() -> None:
    """A kernel that imports a third-party package must be flagged."""
    tree = ast.parse("import requests\n")
    violations = _import_violations(
        "backend/verification/scan.py", tree
    )
    assert violations, (
        "importing requests was not reported — the import rule is not "
        "working"
    )


def test_the_gate_flags_an_orchestration_import() -> None:
    """A kernel that imports the executor must be flagged."""
    tree = ast.parse("from verification_executor import Executor\n")
    assert _orchestration_reasons(tree), (
        "importing verification_executor was not reported — the "
        "orchestration rule is not working"
    )


def test_the_gate_flags_a_relative_orchestration_import() -> None:
    """A kernel that reaches its own package's orchestrator is flagged."""
    tree = ast.parse("from .orchestrator import run_phase\n")
    assert _orchestration_reasons(tree), (
        "a relative import of the orchestrator was not reported — the "
        "orchestration rule is not working"
    )


def test_the_gate_flags_an_unpinned_first_party_symbol() -> None:
    """Taking an unpinned symbol out of a pinned module is flagged."""
    tree = ast.parse("from failure_classification import classify_failure\n")
    violations = _import_violations(
        "backend/verification/failure_fingerprint.py", tree
    )
    assert violations, (
        "an unpinned first-party symbol was not reported — the import "
        "rule is not working"
    )


def test_the_gate_does_not_flag_the_kernels_own_names() -> None:
    """``_fail_open`` is a helper name, not a call to ``open``.

    The substring rule would flag it; the tree rule must not.  If this
    test fails, the forbidden-call check has degenerated into a substring
    scan and the kernel can no longer use an ``..._open`` helper name.
    """
    tree = ast.parse('result = _fail_open("VP-1", "reason")\n')
    assert not _forbidden_reasons(tree), (
        "a helper named _fail_open was reported as a call to open() — the "
        "check has degenerated into a substring scan"
    )
