#!/usr/bin/env python3
"""Census the judgment surface: which functions decide PASS/FAIL.

Why this script exists
----------------------
A verdict is flipped by a function's *return value*, not by its name.
``verification_evidence.verdict_subject_mismatch`` carries no decision
verb at all and still turns a PASSED verdict into a FAILED one — so
"which functions judge?" cannot be answered by grepping for ``_should_``
or ``_is_`` prefixes. The material (§6.1) asks for the opposite: an AST
census over the judgment-relevant modules that produces a list of
*function points*, each carrying the shape of what the function can
return.

The three shapes
----------------
``bool``
    The function answers a yes/no question (``-> bool``). Its return
    value can flip a verdict on its own.

``reason_or_none``
    The function answers "why did this fail", or nothing at all
    (``-> Optional[str]`` / ``-> str | None``). ``None`` means "no
    objection"; a string means "objection, and here it is".

``other``
    Everything else — including a function with no annotation at all. An
    unannotated function is not necessarily uninteresting, but the census
    refuses to guess: the function is recorded, and it is recorded as
    ``other`` rather than dropped.

What this file is, and is not
-----------------------------
This is the classifier, the walker, the tier pre-label **and the manifest
writer**. The manifest (``docs/judgment_points.json``) is generated
here rather than written by hand: a hand-written list of judgment points
rots the first time a function is renamed, and a stale list is worse
than none — it looks authoritative.

The tier pre-label
------------------
The material (§4.1) defines a Tier A judgment point as a function whose
*return value* is written into a verdict, a status or a gate result.
Two questions follow, and this file answers both of them per function:

``covers_verdict_write``
    Does the value this function produces reach a verdict slot? The
    answer is mechanical: the scan follows the **data flow**, never the
    name of the function — ``verdict_subject_mismatch`` carries no
    decision verb and still turns a PASSED verdict into a FAILED one.

``tier``
    Is this function one of the *confirmed* Tier A points? The answer
    is a frozen list, :data:`TIER_A_CONFIRMED`, because the first batch
    (§8) was picked by people who had watched those functions misjudge.

The two answers are kept apart on purpose. The scan is narrower than
the human list: of the nine confirmed candidates it sees seven.
``verification_buckets.derive_index_buckets`` derives the buckets *from*
the verdicts instead of writing into them;
``VerificationExecutor.rerun_deferred_gate_layers`` triggers a write
through a helper instead of returning into one; and
``AutonomousAgent._task_declared_files_exist`` decides whether a task
may complete rather than filling a verdict slot. The manifest carries
both answers, so a reader sees the disagreement instead of inheriting
it.

Scope notes
-----------
* The module list is the material's eleven modules (§6.2), pinned in
  :data:`JUDGMENT_MODULES`. One of them — ``attempt_rollback.py`` — is
  **not on disk in this tree**: the rollback implementation lives in
  ``backend/rollback_manager.py``. The list stays as the material states
  it so that the census and the manifest agree on what is being
  surveyed; a consumer walking the list must skip the modules that are
  not on disk rather than read their absence as a zero-function module.
* :func:`census_module` raises :class:`FileNotFoundError` for a path
  that is not there. "This module has no functions" and "this module is
  not here" are different facts, and only one of them is a census.
* The order of :data:`JUDGMENT_MODULES` is the material's own order (the
  judgment path, not the alphabet), so a caller that concatenates
  several modules gets them in *that* order. :func:`build_manifest`
  sorts the aggregate by ``(module, function)`` — the material's
  determinism rule — before it writes it out; each module's own rows are
  already sorted by function name.

The manifest
------------
``docs/judgment_points.json`` is what the CI gates read. Its shape is
the material's (§6.1 and §4.3):

``schema_version``
    The version of the manifest's own shape.
``points``
    One row per function of every judgment module that is on disk,
    sorted by ``(module, function)``. A row names ``module`` and
    ``function``; it carries ``return_shape`` (what the function can
    return), ``tier`` (whether it is a confirmed judgment point),
    ``covers_verdict_write`` (whether its value reaches a verdict), and
    ``table`` / ``pinned_by`` (what pins it — ``None`` until the
    decision tables land).

The manifest carries **no timestamp**. A timestamp would make every
re-run differ from the file in the repository, and the drift check
(§6.1) would then report a difference that says nothing about the code.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The judgment-relevant modules, pinned to the material (§6.2).
JUDGMENT_MODULES: tuple[str, ...] = (
    "verification_evidence.py",
    "verification_ci_runner.py",
    "verification_buckets.py",
    "verification_executor.py",
    "agent.py",
    "verification_agent.py",
    "verification_loop.py",
    "verification_subagent.py",
    "attempt_rollback.py",
    "retry_manager.py",
    "failure_classification.py",
)

#: The function returns a yes/no answer.
SHAPE_BOOL = "bool"
#: The function returns a reason, or nothing at all.
SHAPE_REASON_OR_NONE = "reason_or_none"
#: Anything else, including an unannotated function.
SHAPE_OTHER = "other"

#: The tier of a function that is a confirmed judgment point.
TIER_A = "A"
#: The tier of everything else: covered by a behavioural golden, not by
#: a decision table.
TIER_B = "B"

#: The words that name a slot a verdict is written into.
#:
#: The material (§4.1) names the slots it means — ``status``, a
#: ``verdict``, a result. A slot is recognised by the words of its
#: name, so ``actual_result`` and ``reasons`` count as well, while
#: ``buckets`` does not: a bucket is derived *from* a verdict, it is not
#: one.
VERDICT_SINK_TOKENS: frozenset[str] = frozenset(
    {"status", "verdict", "result", "outcome", "reason"}
)

#: The material's §8 first batch of Tier A candidates — eight candidates
#: covering nine functions, because the phase-2 gate is two of them
#: (``_gate_block_reason_for`` and ``rerun_deferred_gate_layers``).
#:
#: Each entry is ``(module, function)``, with ``function`` spelled the
#: way the census spells it: the plain name for a module-level function,
#: ``Class.method`` for a method. The material's list writes the two
#: methods by their bare names (``agent._task_declared_files_exist``);
#: the census's spelling is the one that addresses one function, so it
#: is the one used here.
TIER_A_CONFIRMED: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "verification_evidence.py",
            "_ran_the_declared_entry",
        ),
        (
            "verification_evidence.py",
            "_projected_ran_the_declared_entry",
        ),
        (
            "verification_evidence.py",
            "verdict_subject_mismatch",
        ),
        (
            "verification_evidence.py",
            "apply_subject_check_to_verdict",
        ),
        ("agent.py", "excess_declared_paths"),
        ("agent.py", "AutonomousAgent._task_declared_files_exist"),
        (
            "verification_executor.py",
            "VerificationExecutor._gate_block_reason_for",
        ),
        (
            "verification_executor.py",
            "VerificationExecutor.rerun_deferred_gate_layers",
        ),
        ("verification_buckets.py", "derive_index_buckets"),
    }
)


@dataclass(frozen=True)
class FunctionPoint:
    """One function in the census: where it lives and what it can return.

    ``module`` is the file name (``"verification_evidence.py"``) so that
    it matches an entry of :data:`JUDGMENT_MODULES` one-to-one.
    ``function`` is the qualified name: the plain name for a module-level
    function, ``Class.method`` for a method, so that two methods with
    the same name in one module stay distinguishable.

    ``covers_verdict_write`` says whether the value this function
    produces reaches a verdict slot, and ``tier`` says whether the
    function is one of the confirmed Tier A points. Both default to the
    answer for an unremarkable function, so a row can still be built by
    hand without repeating them.
    """

    module: str
    function: str
    return_shape: str
    covers_verdict_write: bool = False
    tier: str = TIER_B


def _is_name(node: ast.AST, name: str) -> bool:
    """``node`` is a reference to the bare name ``name``."""
    return isinstance(node, ast.Name) and node.id == name


def _is_none(node: ast.AST) -> bool:
    """``node`` is the ``None`` literal."""
    return isinstance(node, ast.Constant) and node.value is None


def _is_dunder(name: str) -> bool:
    """``name`` is a dunder — protocol plumbing, not a judgment point."""
    return name.startswith("__") and name.endswith("__")


def _is_write_path(node: ast.AST) -> bool:
    """``node`` is a property setter or deleter — a write path, not a reader.

    ``@value.setter`` is *also* a second definition of the name
    ``value``. Reporting it would invent a judgment point that cannot
    judge (a setter returns nothing anybody reads) and would hide the
    getter, which is the definition that says what reading the attribute
    returns.
    """
    for decorator in getattr(node, "decorator_list", []):
        if (
            isinstance(decorator, ast.Attribute)
            and decorator.attr in ("setter", "deleter")
            and isinstance(decorator.value, ast.Name)
            and decorator.value.id == node.name
        ):
            return True
    return False


def _is_reason_or_none(annotation: ast.AST) -> bool:
    """``annotation`` means "a reason string, or nothing".

    That is ``Optional[str]``, ``str | None`` and ``Union[str, None]`` —
    the three spellings the codebase uses for it.
    """
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left, right = annotation.left, annotation.right
        return (_is_name(left, "str") and _is_none(right)) or (
            _is_none(left) and _is_name(right, "str")
        )
    if isinstance(annotation, ast.Subscript):
        if _is_name(annotation.value, "Optional"):
            return _is_name(annotation.slice, "str")
        if _is_name(annotation.value, "Union") and isinstance(
            annotation.slice, ast.Tuple
        ):
            elements = annotation.slice.elts
            return any(_is_name(el, "str") for el in elements) and any(
                _is_none(el) for el in elements
            )
    return False


def classify_return_shape(node: ast.AST) -> str:
    """Classify what the function ``node`` can return.

    ``node`` is a :class:`ast.FunctionDef` (or :class:`ast.AsyncFunctionDef`);
    the answer is one of :data:`SHAPE_BOOL`, :data:`SHAPE_REASON_OR_NONE`
    and :data:`SHAPE_OTHER`.

    A function with no return annotation is :data:`SHAPE_OTHER`: the
    census records it, but it does not claim the function cannot judge.
    """
    annotation = getattr(node, "returns", None)
    if annotation is None:
        return SHAPE_OTHER
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        # A quoted annotation ("-> 'bool'") states the shape just as well
        # as an unquoted one, so it is read rather than ignored.
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return SHAPE_OTHER
    if _is_name(annotation, "bool"):
        return SHAPE_BOOL
    if _is_reason_or_none(annotation):
        return SHAPE_REASON_OR_NONE
    return SHAPE_OTHER


# ---------------------------------------------------------------------------
# Does the value reach a verdict?
# ---------------------------------------------------------------------------


def _words(identifier: str) -> frozenset[str]:
    """The lower-cased words of ``identifier``.

    ``"actual_result"`` -> ``{"actual", "result"}``. Splitting on ``_``
    keeps ``"statement"`` from reading as ``"state"``.
    """
    return frozenset(
        chunk.strip().lower()
        for chunk in identifier.replace(".", "_").split("_")
        if chunk.strip()
    )


def _is_verdict_sink_name(identifier: str) -> bool:
    """``identifier`` names a slot a verdict is written into.

    A word counts when it is one of :data:`VERDICT_SINK_TOKENS`, or that
    token with an ``s`` on the end — ``"reasons"`` is ``"reason"``.
    """
    return any(
        word in VERDICT_SINK_TOKENS or f"{word}s" in VERDICT_SINK_TOKENS
        for word in _words(identifier)
    )


def _identifier_of(node: ast.AST) -> str:
    """The last identifier of a name chain (``self.verdict`` -> ``verdict``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_verdict_sink_target(target: ast.AST) -> bool:
    """``target`` is a place a verdict is written into.

    Three shapes count: a name (``status = ...``), an attribute
    (``self.verdict = ...``) and a dict slot — where the slot is named
    by its key (``verdict["status"] = ...``) or by the container it
    belongs to (``verdict[...] = ...``).
    """
    if isinstance(target, ast.Name):
        return _is_verdict_sink_name(target.id)
    if isinstance(target, ast.Attribute):
        return _is_verdict_sink_name(target.attr)
    if isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if _is_verdict_sink_name(key.value):
                return True
        return _is_verdict_sink_name(_identifier_of(target.value))
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_is_verdict_sink_target(element) for element in target.elts)
    return False


def _reads_verdict(node: ast.AST) -> bool:
    """``node`` reads a verdict out of something.

    ``verdict.get("status")`` reads one; ``_check(x)`` does not. The
    difference decides what an assignment means: a function that lifts
    the status out of a verdict into a local called ``status`` is
    *reading* the verdict, and calling that a verdict write would put
    every reader of a status into the judgment surface.
    """
    return any(
        _is_verdict_sink_name(_identifier_of(sub))
        for sub in ast.walk(node)
        if isinstance(sub, (ast.Name, ast.Attribute))
    )


def _is_verdict_write(target: ast.AST, value: ast.AST) -> bool:
    """``value`` is written into ``target`` as a verdict.

    A container — ``verdict["status"]`` or ``self.verdict`` — is a
    verdict by being one, so writing into it always counts. A plain
    name counts only when the value was not read out of a verdict: the
    material's own shape (``status = _check(x)``) writes a judgment
    into a status, and ``status = verdict.get("status")`` copies one
    out of a verdict.
    """
    if not _is_verdict_sink_target(target):
        return False
    if isinstance(target, (ast.Subscript, ast.Attribute)):
        return True
    return not _reads_verdict(value)


def _call_name(node: ast.AST) -> str:
    """The bare name a call refers to (``self.check()`` -> ``check``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _call_names_in(node: ast.AST) -> frozenset[str]:
    """Every function called inside ``node``."""
    return frozenset(
        name
        for name in (
            _call_name(sub.func)
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
        )
        if name
    )


def _name_ids_in(node: ast.AST) -> frozenset[str]:
    """Every plain name read inside ``node``."""
    return frozenset(
        sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)
    )


def _walk_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Every node of ``node``'s own scope.

    A nested ``def`` opens a scope of its own: its body belongs to that
    function, not to the one that defines it, so the walk stops there.
    The census treats a nested function as an implementation detail of
    its enclosing function, and this keeps the two readings in step.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        yield child
        yield from _walk_scope(child)


@dataclass(frozen=True)
class _ScopeFacts:
    """What one function's own body says about the verdict.

    ``writes_verdict`` — the body writes into a verdict slot itself.
    ``stored_calls`` — the calls whose value the body stores into a
    verdict slot.
    ``returned_calls`` — the calls whose value the body returns.
    ``condition_calls`` — the calls the body uses to decide.
    """

    writes_verdict: bool
    stored_calls: frozenset[str]
    returned_calls: frozenset[str]
    condition_calls: frozenset[str]


def _collect_scope_facts(node: ast.AST) -> _ScopeFacts:
    """Read one function's body for the facts a verdict write leaves."""
    writes_verdict = False
    stored: set[str] = set()
    returned: set[str] = set()
    conditions: set[str] = set()
    #: Calls whose value a local carries, keyed by the local's name.
    local_calls: dict[str, set[str]] = {}
    #: The names read by the value of a verdict write.
    sink_value_names: set[str] = set()

    for child in _walk_scope(node):
        if isinstance(child, ast.Assign):
            calls = _call_names_in(child.value)
            if _is_verdict_write(child.targets[0], child.value):
                writes_verdict = True
                stored |= calls
                sink_value_names |= _name_ids_in(child.value)
            else:
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        local_calls.setdefault(target.id, set()).update(calls)
        elif isinstance(child, ast.AnnAssign):
            if child.value is None:
                continue
            calls = _call_names_in(child.value)
            if _is_verdict_write(child.target, child.value):
                writes_verdict = True
                stored |= calls
                sink_value_names |= _name_ids_in(child.value)
            elif isinstance(child.target, ast.Name):
                local_calls.setdefault(child.target.id, set()).update(calls)
        elif isinstance(child, ast.AugAssign):
            if _is_verdict_write(child.target, child.value):
                writes_verdict = True
                stored |= _call_names_in(child.value)
        elif isinstance(child, ast.Return):
            if child.value is not None:
                returned |= _call_names_in(child.value)
        elif isinstance(child, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            conditions |= _call_names_in(child.test)

    # A value carried by a local into a verdict slot reaches it, however
    # the local is named.
    for local, calls in local_calls.items():
        if local in sink_value_names:
            stored |= calls

    return _ScopeFacts(
        writes_verdict=writes_verdict,
        stored_calls=frozenset(stored),
        returned_calls=frozenset(returned),
        condition_calls=frozenset(conditions),
    )


def _scope_facts(tree: ast.Module) -> dict[str, _ScopeFacts]:
    """The facts of every function in ``tree``, keyed by qualified name."""
    return {
        name: _collect_scope_facts(node)
        for name, node in iter_functions(tree)
    }


def _verdict_reaching_calls(facts: Mapping[str, _ScopeFacts]) -> frozenset[str]:
    """The calls whose value reaches a verdict, followed up the chain.

    A value reaches a verdict in three ways, and the third is why this
    is a chain and not a single step:

    * it is stored into a verdict slot — ``verdict["status"] = check()``;
    * the function that stores it *returns* that call — the value the
      caller receives is the value that lands in the verdict;
    * the function that stores it *decides* on that call — the call
      decides whether the verdict is written at all.

    The last two are why ``_ran_the_declared_entry``, which is never
    assigned to a status anywhere, is still a judgment point: it decides
    whether ``verdict_subject_mismatch`` reports a reason, and that
    reason is written into the verdict.
    """
    reaching: set[str] = set()
    changed = True
    while changed:
        changed = False
        for qualified, scope in facts.items():
            bare = qualified.rsplit(".", 1)[-1]
            for callee in scope.stored_calls:
                if callee not in reaching:
                    reaching.add(callee)
                    changed = True
            if bare in reaching:
                for callee in scope.returned_calls | scope.condition_calls:
                    if callee not in reaching:
                        reaching.add(callee)
                        changed = True
    return frozenset(reaching)


def _covers_verdict_write(
    facts: Mapping[str, _ScopeFacts],
    reaching: frozenset[str],
    name: str,
) -> bool:
    """Does ``name`` cover a verdict write? (see :func:`covers_verdict_write`)"""
    bare = name.rsplit(".", 1)[-1]
    if bare in reaching:
        return True
    for qualified, scope in facts.items():
        if not scope.writes_verdict:
            continue
        if qualified == name or (
            name == bare and qualified.rsplit(".", 1)[-1] == bare
        ):
            return True
    return False


def covers_verdict_write(tree: ast.Module, name: str) -> bool:
    """Does the value ``name`` produces reach a verdict?

    ``name`` is a function name — the bare name (``"_check"``) or the
    qualified one the census uses (``"Class.method"``). The answer is
    read off the data flow, never off the name:

    * the call's value is stored into a verdict slot — a ``status``
      variable, a ``verdict["status"]`` slot, a ``self.verdict`` field;
    * the call's value is carried by a local into such a slot;
    * the call decides the outcome of a function whose value reaches a
      verdict, or is returned by one;
    * the function named ``name`` writes into a verdict slot itself.

    The scan is deliberately local: it reads one module. A judgment
    that only reaches a verdict in another module is not seen from here,
    which is why the confirmed list — :data:`TIER_A_CONFIRMED` — is
    kept beside it rather than derived from it.
    """
    facts = _scope_facts(tree)
    return _covers_verdict_write(facts, _verdict_reaching_calls(facts), name)


def iter_functions(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Yield ``(qualified name, node)`` for every function in ``tree``.

    A name is reported **once**. Three kinds of definition do not become
    a row of their own:

    * a function defined **inside** another function — it is an
      implementation detail of its enclosing function, not a module-level
      judgment point;
    * a **dunder** — ``__init__`` and friends are protocol plumbing;
    * a property **setter or deleter** — a write path, whose return
      value nobody reads.

    When a name is defined more than once (a method redefined later in
    the same class), the later definition shadows the earlier one, so the
    earlier is unreachable code. The live definition is the one reported,
    and it is reported at its own position — which is what makes a
    module's rows address one function each: the manifest's
    ``(module, function)`` key has to be unique for a row to name *one*
    function.

    ``async def`` counts: an async function judges exactly as much as a
    synchronous one does.
    """
    live: dict[str, ast.AST] = {}
    for name, node in _iter_definitions(tree):
        # A later definition of the same name wins and takes the place of
        # the earlier one, so the order of the rows is the order of the
        # live definitions.
        live.pop(name, None)
        live[name] = node
    yield from live.items()


def _iter_definitions(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Every function definition in ``tree``, in source order.

    This is the raw walk; :func:`iter_functions` is the one that
    resolves shadowing.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_write_path(node):
                continue
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _is_dunder(member.name) or _is_write_path(member):
                    continue
                yield f"{node.name}.{member.name}", member


def census_module(path: str | Path) -> list[FunctionPoint]:
    """Census one module: every function it defines, with its return shape.

    The result is sorted by function name, so two runs over the same file
    produce identical output — the manifest this feeds is compared
    against a re-run in CI, and an unsorted walk would make that
    comparison depend on the source layout rather than on the code.

    Every row also carries the tier pre-label: whether the value the
    function produces reaches a verdict (:func:`covers_verdict_write`),
    and whether the function is one of the confirmed Tier A points
    (:data:`TIER_A_CONFIRMED`). The pre-label adds a label to a row; it
    never adds or drops a row.
    """
    module_path = Path(path)
    tree = ast.parse(
        module_path.read_text(encoding="utf-8"), filename=str(module_path)
    )
    facts = _scope_facts(tree)
    reaching = _verdict_reaching_calls(facts)
    points = [
        FunctionPoint(
            module=module_path.name,
            function=name,
            return_shape=classify_return_shape(node),
            covers_verdict_write=_covers_verdict_write(facts, reaching, name),
            tier=(
                TIER_A
                if (module_path.name, name) in TIER_A_CONFIRMED
                else TIER_B
            ),
        )
        for name, node in iter_functions(tree)
    ]
    return sorted(points, key=lambda point: point.function)


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

#: The version of the manifest's own shape.
SCHEMA_VERSION = 1

#: Where the manifest lives, relative to the repository root.
DEFAULT_MANIFEST_PATH = "docs/judgment_points.json"

#: The directory the judgment modules live in, relative to the root.
BACKEND_DIR = "backend"

#: The decision table that pins a Tier A point, keyed by
#: ``(module, function)`` and written as ``"<path>::<NAME>"``.
#:
#: It is empty here: the tables land with the decision-table work, and
#: the manifest then names them. A row whose ``table`` is ``None`` is a
#: point nobody has pinned yet — which is the truth today, and better
#: than a path to a file that does not exist.
DECISION_TABLES: dict[tuple[str, str], str] = {}

#: The test that pins a point, keyed by ``(module, function)``.
#:
#: Empty for the same reason as :data:`DECISION_TABLES`: the pinning
#: tests land later, and the manifest is regenerated when they do.
PINNED_BY: dict[tuple[str, str], str] = {}


def _default_root() -> Path:
    """The repository root, derived from this file's own location.

    ``scripts/census_judgment_points.py`` sits one directory below the
    root, so the root is the parent of the parent of this file. Nothing
    here is written down: a path written down is a path that is wrong
    on somebody else's machine.
    """
    return Path(__file__).resolve().parents[1]


def build_manifest(root: str | Path) -> dict:
    """Build the judgment-point manifest for the tree at ``root``.

    ``root`` is the repository root: the judgment modules are read from
    ``<root>/backend/``. Every module of :data:`JUDGMENT_MODULES` that is
    on disk contributes its rows; a module that is *not* on disk is
    skipped, because "this module is not here" is not the same fact as
    "this module has no functions" (see :func:`census_module`).

    The rows are sorted by ``(module, function)`` — the material's
    determinism rule (§12.5) — so two runs over the same tree produce
    the same manifest, byte for byte.

    Every row carries the fields the material names (§6.1, §4.3):
    ``module``, ``function``, ``tier``, ``table``, ``return_shape``,
    ``pinned_by`` and ``covers_verdict_write``. The last two are the
    pre-label's two answers, kept apart on purpose, so that a reader
    sees where the mechanical scan and the confirmed list disagree.
    """
    root_path = Path(root)
    points: list[dict] = []
    for module in JUDGMENT_MODULES:
        module_path = root_path / BACKEND_DIR / module
        if not module_path.exists():
            continue
        for point in census_module(module_path):
            key = (point.module, point.function)
            points.append(
                {
                    "module": point.module,
                    "function": point.function,
                    "tier": point.tier,
                    "table": DECISION_TABLES.get(key),
                    "return_shape": point.return_shape,
                    "pinned_by": PINNED_BY.get(key),
                    "covers_verdict_write": point.covers_verdict_write,
                }
            )
    points.sort(key=lambda point: (point["module"], point["function"]))
    return {"schema_version": SCHEMA_VERSION, "points": points}


def render_manifest(manifest: Mapping[str, object]) -> str:
    """Render the manifest as the exact text the file holds.

    The text is byte-stable: the same manifest always renders to the
    same string, and the string ends with exactly one newline. Nothing
    that changes between two runs — a timestamp above all — is allowed
    into it, because the drift check compares this text with the file
    in the repository and every difference it reports is supposed to
    mean "the judgment surface changed".
    """
    return (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


def _manifest_path(root: Path, out: str) -> Path:
    """Where the manifest lives for ``root``: ``out``, resolved."""
    out_path = Path(out)
    if out_path.is_absolute():
        return out_path
    return root / out_path


def main(argv: Sequence[str] | None = None) -> int:
    """Write or check the manifest. Returns the process exit code.

    * ``--write`` writes the manifest to ``--out``;
    * ``--check`` compares the manifest in ``--out`` with the census of
      the tree and exits ``1`` when they differ — printing ``DRIFT`` so
      that the reason is in the log, not only in the exit code;
    * neither flag prints the manifest to stdout.

    ``--root`` is the repository root (the directory that holds
    ``backend/`` and ``scripts/``); it defaults to the root this script
    lives in. ``--out`` is relative to that root unless it is absolute.

    The exit codes are the contract: ``0`` — the manifest matches (or was
    written); ``1`` — the manifest drifted, or is missing; ``2`` — the
    census could not be run at all.
    """
    parser = argparse.ArgumentParser(
        prog="census_judgment_points.py",
        description=(
            "Generate or check the judgment-point manifest "
            "(docs/judgment_points.json)."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(_default_root()),
        help="repository root to census (default: the root of this checkout)",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_MANIFEST_PATH,
        help=(
            "manifest path, relative to --root unless absolute "
            f"(default: {DEFAULT_MANIFEST_PATH})"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="write the manifest",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the manifest differs from the census",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root)
    out = _manifest_path(root, args.out)

    manifest = build_manifest(root)
    if not manifest["points"]:
        print(
            f"census: no judgment module found under {root / BACKEND_DIR}",
            file=sys.stderr,
        )
        return 2
    text = render_manifest(manifest)

    if args.write:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
        return 0

    if args.check:
        if not out.exists():
            print(f"DRIFT: manifest is missing: {out}")
            return 1
        if out.read_text(encoding="utf-8") != text:
            print(f"DRIFT: manifest differs from the census: {out}")
            return 1
        print(f"clean: {out}")
        return 0

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
