"""The verification-branch fixtures and the canonical plan document have one
home, and every later case imports them from there.

Why this file exists
--------------------
``make_dev_plan``, ``make_verification_plan``, ``no_worker_thread`` and
``cas_spy`` used to be module-level fixtures in
``tests/api/test_start_verification_cas.py``.  pytest resolves a fixture in
the namespace of the module that *asks* for it, so a case in another module
could not ask for those four at all — the only way to get them was to copy
them, and a copy is a copy that drifts.  They now live in
``tests/fixtures/verification_mode.py``, the module that already owns the
fixtures an end-to-end verification run is handed, and this file pins the
three properties the move has to keep.

What this pins
--------------
1. **The four names are importable, and they are still fixtures.**  A
   fixture that degraded into a plain function would stop being injected:
   the case that asks for it would fail with a missing argument instead of
   a clear error, and the marker is what says "this is a fixture".
2. **The factories they hand back keep their signatures.**  The keyword
   arguments are the contract the cases call them with.
3. **The canonical plan document is byte-stable.**  Two calls must produce
   the same bytes and the exported constant must be exactly those bytes: a
   document that drifts between two calls cannot be compared with anything,
   and a comparison that silently compares two different documents is worse
   than no comparison at all.
4. **The shared module is their only home, and everybody names it.**  The
   move deletes the local copies in the CAS suite; a deleted copy that a
   sibling module still imports breaks the whole lane's collection, not one
   case.  So this pins three more things: the CAS suite does not define the
   promoted helpers again; no API module imports a name a sibling test
   module no longer defines; and the end-to-end lane really consumes one of
   the promoted names from the shared module.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import List, Optional, Set

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests.fixtures import verification_mode  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    build_plan_doc,
    cas_spy,
    make_dev_plan,
    make_verification_plan,
    no_worker_thread,
)

#: The four fixtures the move put into the shared module.
FIXTURE_NAMES = (
    "make_dev_plan",
    "make_verification_plan",
    "no_worker_thread",
    "cas_spy",
)

#: The ten sections a verification plan document has to carry, in the order
#: the template asks for them.
REQUIRED_SECTIONS = (
    "## 被验证对象",
    "## 验证范围",
    "## 排除范围",
    "## 验证方法",
    "## 验证点清单",
    "## 预期结果",
    "## 证据要求",
    "## 通过判据",
    "## 环境与依赖",
    "## 风险面",
)

#: The module that owns the promoted fixtures — and the only place a later
#: case may import them from.
SHARED_MODULE = "tests.fixtures.verification_mode"

#: The names that used to be module-level helpers in the CAS suite.  The
#: shared module is now their only home: a second copy is a copy that
#: drifts, and a *deleted* copy is a name a sibling module still imports.
CAS_MODULE_ONLY_NAMES = ("git_repo", "_record_routing_row")

#: The API lane's own directory.
API_TESTS_DIR = _BACKEND_DIR / "tests" / "api"

#: The end-to-end lane's own directory.
E2E_TESTS_DIR = _BACKEND_DIR / "tests" / "e2e"


def _is_pytest_fixture(candidate: object) -> bool:
    """Whether ``candidate`` is a pytest fixture rather than a plain function.

    pytest has carried the marker under two names: ``_pytestfixturefunction``
    up to 8.3 and ``_fixture_function_marker`` from 8.4 on.  Both are
    checked so this file keeps working across the versions the suite runs on.
    """
    return (
        hasattr(candidate, "_pytestfixturefunction")
        or hasattr(candidate, "_fixture_function_marker")
    )


def test_four_fixtures_importable() -> None:
    """The four fixture names are importable from the shared module."""
    for name in FIXTURE_NAMES:
        assert callable(globals()[name]), f"{name} is not importable"


def test_fixture_signatures_unchanged(
    make_verification_plan,
    make_dev_plan,
) -> None:
    """The two plan builders are still pytest fixtures, with the same factories."""
    assert _is_pytest_fixture(verification_mode.make_verification_plan)
    assert _is_pytest_fixture(verification_mode.make_dev_plan)

    assert list(inspect.signature(make_verification_plan).parameters) == [
        "review_complete",
        "baseline_overrides",
        "ref_moved",
    ]
    assert list(inspect.signature(make_dev_plan).parameters) == []


def test_canonical_plan_doc_is_exported() -> None:
    """``build_plan_doc()`` is byte-stable and carries every required section."""
    first = build_plan_doc()
    second = build_plan_doc()

    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
    assert CANONICAL_PLAN_DOC == first

    positions = [first.index(section) for section in REQUIRED_SECTIONS]
    assert positions == sorted(positions), (
        "the canonical plan document's sections are out of order: "
        f"{list(zip(REQUIRED_SECTIONS, positions))}"
    )


# ---------------------------------------------------------------------------
# 4. 共享模块是这几个构造的唯一一份
# ---------------------------------------------------------------------------


def _module_file(dotted: str) -> Optional[Path]:
    """The file a dotted module name lives in, or ``None`` when it is not one.

    ``backend.tests.api.test_x`` and ``tests.api.test_x`` are the same file
    seen through two roots; the suite uses both spellings.
    """
    if dotted.startswith("backend."):
        dotted = dotted[len("backend."):]
    if not dotted.startswith("tests."):
        return None
    return _BACKEND_DIR.joinpath(*dotted.split(".")).with_suffix(".py")


def _module_level_definitions(tree: ast.Module) -> Set[str]:
    """The names a module *defines* at module level.

    An import does not define anything: ``from … import record_routing_row``
    binds a name, ``def record_routing_row`` defines one.  Only the second
    kind makes a module a second home of a constructor.
    """
    names: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _module_level_bindings(tree: ast.Module) -> Set[str]:
    """Every name a module binds at module level, imports included."""
    names = _module_level_definitions(tree)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _imported_from_shared_module(tree: ast.Module) -> Set[str]:
    """The promoted names a module imports from the shared module."""
    imported: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module not in (SHARED_MODULE, f"backend.{SHARED_MODULE}"):
            continue
        for alias in node.names:
            if alias.name in FIXTURE_NAMES:
                imported.add(alias.name)
    return imported


def _names_used(tree: ast.Module) -> Set[str]:
    """The names a module reads.

    A fixture is asked for by name in a signature, and that name is an
    :class:`ast.arg` rather than a :class:`ast.Name` — so both are counted.
    """
    used: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.arg):
            used.add(node.arg)
    return used


def test_the_four_names_are_still_pytest_fixtures() -> None:
    """All four promoted names are still fixtures, not plain functions."""
    for name in FIXTURE_NAMES:
        candidate = getattr(verification_mode, name)
        assert _is_pytest_fixture(candidate), (
            f"{name} degraded into a plain function: it would stop being "
            "injected, and the case asking for it would fail with a missing "
            "argument instead of a clear error"
        )


def test_the_shared_module_is_the_only_home_of_the_promoted_names() -> None:
    """The CAS suite does not define the promoted helpers any more."""
    cas_path = API_TESTS_DIR / "test_start_verification_cas.py"
    tree = ast.parse(cas_path.read_text(encoding="utf-8"))
    defined = _module_level_definitions(tree)

    for name in CAS_MODULE_ONLY_NAMES:
        assert name not in defined, (
            f"{name} is defined in {cas_path.name} again — the shared module "
            "is its only home, and a second copy is a copy that drifts"
        )


def test_no_api_module_imports_a_name_its_sibling_module_does_not_define() -> (
    None
):
    """An API module may not import a name a sibling test module lost.

    pytest resolves a fixture in the namespace of the module that *asks* for
    it, so a sibling module's fixture stops being importable the moment it
    moves.  An import that names a fixture the sibling no longer defines
    breaks the whole lane's collection — not one case, the lane.
    """
    problems: List[str] = []

    for path in sorted(API_TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            sibling = _module_file(node.module)
            if sibling is None or not sibling.is_file():
                continue
            bound = _module_level_bindings(
                ast.parse(sibling.read_text(encoding="utf-8"))
            )
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name not in bound:
                    problems.append(
                        f"{path.name}:{node.lineno} imports {alias.name!r} from "
                        f"{node.module}, which does not define it any more"
                    )

    assert problems == [], (
        "the API lane imports names that are gone:\n" + "\n".join(problems)
    )


def test_the_e2e_lane_consumes_the_promoted_fixtures_by_name() -> None:
    """The end-to-end lane imports a promoted fixture — and really uses it.

    A shared module nobody imports is a shared module nothing is pinned by.
    The pin is that the end-to-end lane asks for one of the promoted names
    **and uses it**: an import that is never read is not consumption.
    """
    consumers: List[str] = []

    for path in sorted(E2E_TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_from_shared_module(tree)
        if not imported:
            continue
        used = _names_used(tree)
        for name in sorted(imported):
            if name in used:
                consumers.append(f"{path.name}: {name}")

    assert consumers, (
        "no module under backend/tests/e2e/ imports and uses one of the "
        f"promoted fixtures {FIXTURE_NAMES}: the end-to-end lane has to "
        "consume the shared module by name"
    )