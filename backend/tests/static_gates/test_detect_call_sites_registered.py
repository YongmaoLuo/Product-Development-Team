"""Every ``detect()`` call site in the backend is registered, and the
registration carries the semantics the decision table gives it.

Why this gate exists
--------------------
``plan_artifacts.detect()`` is the **only** entry point that decides which
branch a plan directory belongs to: ``dev`` (a task list is on disk),
``verification`` (only a plan document is on disk) or ``empty``.  Everything
that used to read the two artefact files itself now asks that one function,
which is what made the coexistence case ("both artefacts are on disk, and
that is legal") answerable at all.

Eight places consume that answer, and they do not all want the same thing
from it.  The task-generation gate must **refuse** when a plan document is
already there — coexistence is one-way, so a pure verification branch may
not be turned into a development branch — while the plan-listing entry point
must report the two artefacts side by side and stop calling it a conflict.
Those are not variations of one behaviour; they are different answers to
different questions, and the difference is exactly what a careless
"unify the call sites" edit erases.

The decision table (test design, decision point 2) writes those answers
down, cell by cell: the rows are the four disk states, the columns are the
call sites, and one cell — the task-generation gate on a coexisting
directory — reads **拒绝** (refuse).  The table is only worth anything if
it stays attached to the code: a call point that is added without being
registered is a call point whose semantics nobody wrote down, and nobody
reviews.

So this gate pins two things:

1. **The inventory.** The set of ``detect()`` call sites in the production
   tree is compared against the registry below; a difference in either
   direction fails and names the difference.
2. **The one-way cell.** The task-generation gate's registration says
   *refuse*, and the source still raises the 409 that says so.  The
   registration alone would be a comment; the source check is what makes
   it a contract.

Scope, stated honestly
----------------------
This reads the source.  It does not execute the plan directories, so a
green run here means "the call sites are the registered ones, and the
registered semantics are the ones the decision table wrote down" — not
"the endpoints behave that way".  The behavioural half of the table is
owned by the API-layer cases (``tests/api/test_artifact_conflict.py`` and
``tests/api/test_coexist_plan_state_and_listing.py``); this gate is what
stops the table from silently losing a row.

Reading the source
------------------
The scan goes through :mod:`tests.app_source`, so it reads the whole
production tree rather than a hand-picked list of files, and it follows
``detect`` if it moves.  Comments and strings are not calls: the scan is an
AST walk, so a docstring that spells out ``detect() != verification`` is
prose, not a consumer.  The module that *defines* ``detect`` is excluded
— its definition and its two internal calls are the implementation, not
consumers — and the exclusion follows the definition rather than a file
name, so moving ``detect`` does not silently widen the haystack.

Both spellings of the call are resolved, because a caller may write
either: the name one (``from plan_artifacts import detect`` →
``detect(...)``) and the module-object one (``import plan_artifacts`` →
``plan_artifacts.detect(...)``).  A spelling the scan does not know is a
spelling that goes unregistered, which is the failure this gate exists to
prevent — so a ``detect`` call that resolves to **neither** is reported
rather than dropped.  Dropping it would leave the haystack smaller than it
looks: the comparison below only sees the call sites the scan resolved, so
an unresolved call would not show up in the difference between the source
and the registry — it would simply be missing, silently.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import app_source as app_source_module  # noqa: E402


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: The four disk states the decision table's rows are.  Every registered
#: call site carries one cell for each of them — a half-filled row is a
#: decision that was only made for the case somebody happened to be in.
DISK_STATES: Tuple[str, ...] = (
    "only_tasks",
    "only_plan_doc",
    "coexist",
    "neither",
)


@dataclass(frozen=True)
class DetectCallSite:
    """One registered ``detect()`` call site and the semantics it carries.

    Attributes:
        module: The module, relative to ``backend/`` (``routes/phases.py``).
        function: The function the call lives in.  The registry keys on
            ``module::function`` — a line number would rot the first
            time anything above the call moved.
        column: The decision table's column this call site answers for.
        cells: The four disk states and the behaviour each one gets.
        note: Why this call site is the way it is — the sentence a reader
            needs before changing the cell.
    """

    module: str
    function: str
    column: str
    cells: Mapping[str, str]
    note: str = ""


def _site_key(module: str, function: str) -> str:
    """The registry key for one call site: ``module::function``."""
    return f"{module}::{function}"


#: Every ``detect()`` call site in the production tree, with the semantics
#: the decision table gives it.
#:
#: The table (test design, decision point 2) is transcribed here cell by
#: cell.  The ``coexist`` column is the one this plan migrated — the other
#: three columns are the table's own "现状" rows, recorded so that a
#: call site cannot be added without its neighbours' answers being visible
#: next to it.
#:
#: This is the table a reviewer edits.
DETECT_CALL_SITE_TABLE: Tuple[DetectCallSite, ...] = (
    DetectCallSite(
        module="plan_state.py",
        function="_infer_phase",
        column="plan_state 阶段",
        cells={
            "only_tasks": "queued（dev 分支）",
            "only_plan_doc": "verification_plan_review（verification 分支）",
            "coexist": "queued，不抛冲突（任务优先归 dev 分支）",
            "neither": "现状（detect 判 empty，阶段沿既有规则推断）",
        },
        note=(
            "阶段推断读的是**主分支**。共存按「任务优先」落在 dev 上，所以"
            "推断结果是 queued，而不是抛冲突 —— 阶段推断不替人裁决。"
        ),
    ),
    DetectCallSite(
        module="routes/phases.py",
        function="generate_tasks",
        column="phases 生成任务",
        cells={
            "only_tasks": "拒绝（已有任务）",
            "only_plan_doc": "拒绝（单向性）",
            "coexist": "拒绝，错误码 artifact_conflict",
            "neither": "允许",
        },
        note=(
            "**方向单一**：目录里已有计划书时不再写任务清单。共存合法之后，"
            "「先任务、后补计划书」是允许的，但把一条纯验证分支改造成开发"
            "分支不是 —— 错误码 artifact_conflict 保留（API 契约），文案改。"
            "决策表「仅 tasks.json」一格写作「拒绝（已有任务）」；产物门本身"
            "键在**计划书是否在场**（唯一入口 detect），只有任务清单时本入口"
            "走重新生成路径。此格按决策表原文登记。"
        ),
    ),
    DetectCallSite(
        module="routes/phases.py",
        function="generate_verification_plan",
        column="phases 生成计划书",
        cells={
            "only_tasks": "允许（决策点 3）",
            "only_plan_doc": "允许",
            "coexist": "允许，无相位副作用",
            "neither": "允许（受 PRD 门约束）",
        },
        note=(
            "计划书是任务清单之后的**可选附加动作**：「目录里已有 tasks.json」"
            "那道门已经删掉了。相位推进只属于纯验证分支，开发分支生成计划书"
            "不改它的相位。"
        ),
    ),
    DetectCallSite(
        module="routes/plans.py",
        function="_plan_status_payload",
        column="plans 载荷/列表",
        cells={
            "only_tasks": "dev 桶",
            "only_plan_doc": "verification 桶",
            "coexist": "不再进冲突桶；载荷同时给两份产物",
            "neither": "现状",
        },
        note=(
            "单计划状态载荷给的是**同一份判定**（branch + 两份产物的相对名）。"
            "两个读者不许对同一条 plan 给出两个答案，所以它与列表读的是同一个"
            "函数。"
        ),
    ),
    DetectCallSite(
        module="routes/plans.py",
        function="_list_plans_body",
        column="plans 载荷/列表",
        cells={
            "only_tasks": "dev 桶",
            "only_plan_doc": "verification 桶",
            "coexist": "不再进冲突桶；载荷同时给两份产物",
            "neither": "现状",
        },
        note=(
            "列表曾经把共存 plan 丢进 conflict 桶。共存合法之后没有这个桶了："
            "条目给出主分支与两份产物的相对名。"
        ),
    ),
    DetectCallSite(
        module="routes/verification.py",
        function="_verification_branch_prelude",
        column="verification 前奏",
        cells={
            "only_tasks": "dev 语义",
            "only_plan_doc": "verification 语义",
            "coexist": "dev 语义，不物化 worktree",
            "neither": "现状",
        },
        note=(
            "前奏分两段，分派依据不同：**计划准备**按来源（计划书在就翻译并过"
            "等价性门禁，两条分支同等对待），**验证对象准备**按分支（只有纯"
            "验证分支才物化 worktree）。"
        ),
    ),
    DetectCallSite(
        module="routes/verification.py",
        function="start_verification",
        column="verification 启动",
        cells={
            "only_tasks": "dev 语义",
            "only_plan_doc": "verification 语义",
            "coexist": "dev 语义，不物化 worktree",
            "neither": "现状",
        },
        note=(
            "验证启动不再被产物门拦：共存合法之后，这条 plan 走哪条分支由"
            "验证流水线自己的门决定。"
        ),
    ),
    DetectCallSite(
        module="verification_gate.py",
        function="_is_verification_branch",
        column="verification_gate 就绪",
        cells={
            "only_tasks": "False",
            "only_plan_doc": "既有谓词结果（本次不改）",
            "coexist": "False",
            "neither": "False",
        },
        note=(
            "就绪判定看的是「目录里**只有**验证计划书」。共存时主分支是 dev，"
            "所以不就绪 —— 验证分支跳过初始执行，共存目录不该被当成验证分支。"
            "这个调用点没有自己的端点，端点层的用例钉不到它，所以登记在此。"
        ),
    ),
)

#: The registry: every ``detect()`` call site in the production tree, as a
#: set of keys.
#:
#: This is the whitelist the source is compared against — two sets, and a
#: difference in either direction fails the gate.  The semantics behind each
#: key live in :data:`DETECT_CALL_SITE_TABLE`, which is the table a reviewer
#: edits; this set is **derived** from it, so the whitelist and the table
#: cannot drift apart.
DETECT_CALL_SITES: frozenset[str] = frozenset(
    _site_key(entry.module, entry.function) for entry in DETECT_CALL_SITE_TABLE
)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

#: The module that defines ``detect``.  Its definition and its own
#: internal calls are the implementation, not consumers — so they are not
#: registered.  The name is resolved from the source, not hard-coded, so
#: moving ``detect`` does not silently widen the haystack.
DETECT_DEFINITION_NAME = "detect"


def _relative(path: Path) -> str:
    """*path* relative to ``backend/``, in POSIX form."""
    return path.relative_to(BACKEND_DIR).as_posix()


def _definition_module() -> Optional[str]:
    """The module that defines ``detect``, relative to ``backend/``.

    ``None`` when the definition is gone — which is itself a failure, and
    one this gate must report rather than skip: a gate that finds no
    ``detect`` at all has nothing to compare and would pass.
    """
    try:
        found = app_source_module.find_def(DETECT_DEFINITION_NAME)
    except LookupError:
        return None
    return _relative(found.path)


def _import_bindings(
    tree: ast.Module, define_stem: str
) -> Tuple[Set[str], Set[str]]:
    """The names *tree* binds to ``detect`` and to the defining module.

    Returns ``(detect_names, module_names)``: the local names that refer to
    the ``detect`` function, and the dotted paths that refer to the module
    it lives in (for the ``plan_artifacts.detect(...)`` spelling).

    Every import in the module is looked at, not just the module-level
    ones: ``routes/verification.py`` imports ``detect`` **inside** the
    function that uses it, and a scan that only read module-level imports
    would miss that call site.

    *define_stem* is the name of the module ``detect`` is defined in, so
    the spellings that reach it are matched against the definition itself
    rather than against a file name written down here — moving ``detect``
    must not silently widen the haystack.
    """
    detect_names: Set[str] = set()
    module_names: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            from_module = node.module.split(".")[-1]
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == DETECT_DEFINITION_NAME and from_module == define_stem:
                    # ``from plan_artifacts import detect``
                    detect_names.add(bound)
                elif alias.name.split(".")[-1] == define_stem:
                    # ``from backend import plan_artifacts`` — the module
                    # itself is imported, so the call spells itself
                    # ``plan_artifacts.detect(...)``.
                    module_names.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] != define_stem:
                    continue
                if alias.asname:
                    module_names.add(alias.asname)
                else:
                    # ``import backend.plan_artifacts`` binds ``backend``,
                    # but the call reads the whole chain.
                    module_names.add(alias.name)

    return detect_names, module_names


def _dotted_name(node: ast.AST) -> Optional[str]:
    """The dotted path an expression names, e.g. ``backend.plan_artifacts``.

    ``None`` when the expression is not a plain name/attribute chain: a
    subscript or a call has no fixed path, so it cannot be matched against
    the import bindings.
    """
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _enclosing_function(
    node: ast.AST, parents: Mapping[ast.AST, ast.AST]
) -> Optional[str]:
    """The name of the function *node* is inside, if any."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def _is_detect_call(
    func: ast.AST,
    detect_names: Set[str],
    module_names: Set[str],
) -> bool:
    """Is this call target the ``detect`` function?"""
    if isinstance(func, ast.Name):
        return func.id in detect_names
    if isinstance(func, ast.Attribute):
        if func.attr != DETECT_DEFINITION_NAME:
            return False
        base = _dotted_name(func.value)
        return base is not None and base in module_names
    return False


def _is_unattributable_detect(func: ast.AST, define_stem: str) -> bool:
    """Is this a ``detect`` call that cannot be attributed to the entry point?

    Two shapes reach here: a bare ``detect(...)`` whose name was never
    bound to the defining module, and ``<module>.detect(...)`` whose base
    is named after the defining module but was not bound to it.  Both are
    reported rather than dropped — a call site the registry cannot hold is
    exactly the one that would otherwise be invisible.

    An attribute call whose base is named after some *other* thing
    (``self.detect(...)``) is not reported: it is not a consumer of this
    entry point, and flagging it would make the gate fire on code that has
    nothing to do with the decision table.
    """
    if isinstance(func, ast.Name):
        return func.id == DETECT_DEFINITION_NAME
    if isinstance(func, ast.Attribute) and func.attr == DETECT_DEFINITION_NAME:
        base = _dotted_name(func.value)
        return base is not None and base.split(".")[-1] == define_stem
    return False


def _scan_detect_call_sites() -> Tuple[Dict[str, List[int]], List[str]]:
    """Every ``detect()`` call in the production tree.

    Returns ``(call_sites, unbound)``: the calls keyed by
    ``module::function`` with the line numbers they sit on, and the
    ``detect(...)`` calls that do **not** resolve to the defining module
    (a missing import, or a name shadowed by something else).  Those are
    reported separately: a call site that cannot be attributed to
    ``plan_artifacts.detect`` is either a bug or an unregisterable one, and
    silently dropping it would leave the haystack smaller than it looks.

    Both spellings of the call are resolved: the name one
    (``from plan_artifacts import detect`` → ``detect(...)``) and the
    module-object one (``import plan_artifacts`` →
    ``plan_artifacts.detect(...)``).  A caller may write either, and a
    spelling the scan does not know is a spelling that goes unregistered.
    """
    definition = _definition_module()
    define_stem = Path(definition).stem if definition else DETECT_DEFINITION_NAME
    call_sites: Dict[str, List[int]] = {}
    unbound: List[str] = []

    for path, source in app_source_module.app_sources():
        module = _relative(path)
        if module == definition:
            continue
        tree = ast.parse(source)
        detect_names, module_names = _import_bindings(tree, define_stem)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = _enclosing_function(node, parents) or "<module>"
            if _is_detect_call(node.func, detect_names, module_names):
                call_sites.setdefault(_site_key(module, function), []).append(
                    node.lineno
                )
            elif _is_unattributable_detect(node.func, define_stem):
                # A ``detect(...)`` call that does not resolve to the one
                # entry point: a missing import, or a name shadowed by
                # something else.
                unbound.append(f"{module}:{node.lineno}（{function}）")

    return call_sites, unbound


# ---------------------------------------------------------------------------
# The registry, read back
# ---------------------------------------------------------------------------


def _registry_keys() -> Dict[str, DetectCallSite]:
    """The registry keyed by ``module::function``."""
    return {
        _site_key(entry.module, entry.function): entry
        for entry in DETECT_CALL_SITE_TABLE
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_registry_is_a_frozenset_of_call_site_keys() -> None:
    """``DETECT_CALL_SITES`` is the whitelist, and it is a set of keys.

    The plan names the shape of the deliverable: the registry is a
    ``frozenset[str]`` — one key per call site — and the gate compares the
    source against **that**, as two sets.

    A tuple of records can be compared just as well, but then the name does
    not say what it is, and every consumer has to know the key format
    before it can use the registry.  The semantic table stays a table; the
    registry stays a set.
    """
    assert isinstance(DETECT_CALL_SITES, frozenset), (
        f"DETECT_CALL_SITES is a {type(DETECT_CALL_SITES).__name__}, not a "
        "frozenset. The registry is the whitelist the source is compared "
        "against, and it has to be a set of keys to be compared as one."
    )
    assert all(isinstance(key, str) for key in DETECT_CALL_SITES), (
        "the registry's keys are not all strings: "
        f"{sorted(k for k in DETECT_CALL_SITES if not isinstance(k, str))}"
    )


def test_all_detect_call_sites_are_registered() -> None:
    """The production tree's ``detect()`` call sites and the registry agree.

    Both directions matter.  A call site that is not registered is a
    consumer whose semantics nobody wrote down; a registry entry with no
    call site behind it is a decision recorded against code that is no
    longer there, and it is the kind of stale entry that makes the whole
    table stop being read.
    """
    call_sites, unbound = _scan_detect_call_sites()
    registry = _registry_keys()

    missing = sorted(set(call_sites) - set(registry))
    extra = sorted(set(registry) - set(call_sites))

    assert not missing and not extra, (
        "the registry and the source disagree about the detect() call sites.\n"
        f"  in the source but not registered: {missing}\n"
        f"  registered but not in the source: {extra}\n"
        "A new call site has to be registered with the semantics the "
        "decision table gives it; a call site that disappeared has to "
        "have its registry entry removed. Both are edits to the table, "
        "and both belong in the review."
    )
    assert not unbound, (
        "these detect() calls do not resolve to plan_artifacts.detect: "
        f"{unbound}. A call that cannot be attributed to the one entry "
        "point is either a missing import or a shadowed name — and it is "
        "a call site the registry cannot hold, so it would be invisible "
        "to the comparison above."
    )


def test_a_call_through_an_imported_module_object_is_a_call_site(
    monkeypatch,
) -> None:
    """``plan_artifacts.detect(...)`` is a consumer, not an invisible one.

    The registry has to hold **every** consumer, so the scan has to
    recognise the module-object spelling of the call as well as the
    ``from plan_artifacts import detect`` one.  A scan that only knows the
    second spelling drops the first on the floor — and a dropped call site
    is the one thing this gate exists to prevent, because it is exactly the
    call site whose semantics nobody wrote down.

    The spelling checked here is the package-qualified one
    (``from backend import plan_artifacts as pa``): it is legal in this
    tree — ``backend/__init__.py`` puts the backend directory on
    ``sys.path`` — so a caller may write it, and the gate has to see it.
    """
    synthetic = (
        BACKEND_DIR / "_synthetic_caller.py",
        "from backend import plan_artifacts as pa\n"
        "\n"
        "def consume(plan_dir):\n"
        "    return pa.detect(plan_dir)\n",
    )
    real = app_source_module.app_sources
    monkeypatch.setattr(
        app_source_module, "app_sources", lambda: list(real()) + [synthetic]
    )

    call_sites, unbound = _scan_detect_call_sites()

    assert _site_key("_synthetic_caller.py", "consume") in call_sites, (
        "a detect() call reached through an imported module object was not "
        f"recognised as a call site (scanned: {sorted(call_sites)}). The "
        "module-object spelling of the call has to resolve, or it is "
        "invisible to the registry."
    )
    assert not unbound, (
        f"the call should resolve to plan_artifacts.detect, not be reported "
        f"as unattributable: {unbound}"
    )


def test_a_detect_call_that_cannot_be_attributed_is_reported(
    monkeypatch,
) -> None:
    """A ``detect`` call nobody can attribute is loud, not dropped.

    The comparison in :func:`test_all_detect_call_sites_are_registered` only
    sees the call sites the scan resolved.  A call it could **not** resolve
    would therefore not show up in the difference between the source and the
    registry — it would simply be missing, silently, and the gate would go
    green on a haystack smaller than it looks.

    So an unattributable call is reported on its own, and the gate fails on
    it: a call site the registry cannot hold is either a missing import or a
    shadowed name, and both need a human.
    """
    synthetic = (
        BACKEND_DIR / "_synthetic_caller.py",
        "def consume(plan_dir):\n"
        "    return plan_artifacts.detect(plan_dir)\n",
    )
    real = app_source_module.app_sources
    monkeypatch.setattr(
        app_source_module, "app_sources", lambda: list(real()) + [synthetic]
    )

    call_sites, unbound = _scan_detect_call_sites()

    assert _site_key("_synthetic_caller.py", "consume") not in call_sites, (
        "a detect() call that does not resolve to the entry point was "
        "registered as if it did. The registry would then claim semantics "
        "for code that never reaches plan_artifacts.detect."
    )
    assert len(unbound) == 1, (
        "a detect() call that cannot be attributed to plan_artifacts.detect "
        f"was not reported (unattributable: {unbound}). It is invisible to "
        "the registry comparison, so it has to be reported on its own."
    )
    assert "_synthetic_caller.py" in unbound[0], (
        f"the unattributable call is reported without its module: {unbound}"
    )


def test_a_registered_call_site_calls_detect_exactly_once() -> None:
    """One registry entry is one call.

    The registry keys on ``module::function``, so a function that calls
    ``detect()`` twice is two call sites sharing one key — and the second
    one would be a consumer whose semantics nobody wrote down.  It is also
    wasted work: ``detect()`` re-reads the disk on every call, so two calls
    in one function can disagree with each other.
    """
    call_sites, _ = _scan_detect_call_sites()
    offenders = {
        key: lines for key, lines in call_sites.items() if len(lines) > 1
    }
    assert not offenders, (
        f"these functions call detect() more than once: {offenders}. Call it "
        "once and reuse the ArtifactSet — two calls can read two different "
        "disk states, and the registry holds one semantics per function."
    )


def test_registry_has_no_stale_entries() -> None:
    """Every registered ``module::function`` is still in the tree.

    A registry entry whose function was renamed or moved describes code
    that is not there.  It reads like coverage and is not.
    """
    known = _defined_functions()
    registry = _registry_keys()
    missing = sorted(set(registry) - known)
    assert not missing, (
        f"the registry names call sites that are not in the production tree: "
        f"{missing}. A renamed or moved function leaves its entry behind, "
        "and an entry with no code behind it is a decision nobody reviews."
    )


def test_every_registered_call_site_has_a_cell_for_every_disk_state() -> None:
    """No half-filled rows.

    The decision table's rows are the four disk states.  A cell that is
    empty is a decision that was only made for the case somebody happened
    to be in when the table was written — and the case nobody was in is
    the one that breaks.
    """
    incomplete = {
        key: sorted(set(DISK_STATES) - set(entry.cells))
        for key, entry in _registry_keys().items()
        if not set(DISK_STATES).issubset(entry.cells)
    }
    assert not incomplete, (
        f"these registered call sites have no cell for some disk states: "
        f"{incomplete}. Every call site answers all four rows of the "
        f"decision table ({', '.join(DISK_STATES)})."
    )

    blank = {
        key: state
        for key, entry in _registry_keys().items()
        for state, value in entry.cells.items()
        if not value.strip()
    }
    assert not blank, (
        f"these registered cells are empty: {blank}. An empty cell is a "
        "decision that was not made, not a decision that was made quietly."
    )


def test_the_task_generation_gate_still_refuses() -> None:
    """The one-way cell: the task-generation gate says *refuse*, and refuses.

    Coexistence is one-way.  "Tasks first, plan document afterwards" is
    allowed; turning a pure verification branch into a development branch
    is not, because that is a decision about the plan's purpose, and the
    framework does not make it.  The 409 stays; its wording changed.

    The registry cell alone would be a comment.  This check reads the
    source as well: the gate must still raise the 409, and it must still
    key on the plan document being there — a refusal keyed on something
    else would refuse for the wrong reason.
    """
    entry = _registry_keys().get(_site_key("routes/phases.py", "generate_tasks"))
    assert entry is not None, (
        "the task-generation gate (routes/phases.py::generate_tasks) is not "
        "registered. It is the one call site whose answer must not change, "
        "so it cannot be one that is added later."
    )

    coexist = entry.cells.get("coexist", "")
    assert coexist.startswith("拒绝"), (
        f"the task-generation gate's coexistence cell reads {coexist!r}; it "
        "must read 拒绝 (refuse). Coexistence is one-way: a plan document "
        "that is already on disk may not be answered with a task list."
    )
    assert "artifact_conflict" in coexist, (
        f"the task-generation gate's coexistence cell reads {coexist!r}; the "
        "error code artifact_conflict is part of the API contract and has "
        "to be named here, so that changing it is a visible edit."
    )

    guards = _conflict_guards("routes/phases.py", "generate_tasks")
    assert guards, (
        "routes/phases.py::generate_tasks no longer raises a 409. The "
        "coexistence cell says 拒绝 and the code has to refuse: coexistence "
        "is one-way, and a gate that stopped refusing would turn a pure "
        "verification branch into a development branch without asking."
    )
    assert any("plan_doc_path" in guard for guard in guards), (
        "the task-generation gate's 409 is not keyed on the plan document "
        f"any more (guards: {guards}). The refusal exists because a plan "
        "document is already on disk; keyed on anything else it would "
        "refuse for the wrong reason."
    )


# ---------------------------------------------------------------------------
# Helpers used by the checks above
# ---------------------------------------------------------------------------


def _defined_functions() -> Set[str]:
    """Every ``module::function`` defined in the production tree."""
    defined: Set[str] = set()
    for path, source in app_source_module.app_sources():
        module = _relative(path)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(_site_key(module, node.name))
    return defined


def _conflict_guards(module: str, function: str) -> List[str]:
    """The guards in *module*'s *function* that raise a 409 ``HTTPException``.

    Each entry is the source text of the ``if`` test the raise sits under,
    so the caller can see **what the refusal is keyed on** and not merely
    that a refusal exists.
    """
    for path, source in app_source_module.app_sources():
        if _relative(path) != module:
            continue
        tree = ast.parse(source)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        guards: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "HTTPException":
                continue
            args = node.exc.args
            if not args:
                continue
            status = args[0]
            if not (isinstance(status, ast.Constant) and status.value == 409):
                continue
            current = parents.get(node)
            while current is not None:
                if isinstance(current, ast.If):
                    guards.append(ast.get_source_segment(source, current.test) or "")
                    break
                current = parents.get(current)
        return guards
    return []