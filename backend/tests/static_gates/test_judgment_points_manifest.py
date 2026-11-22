"""The judgment-point manifest has to describe the source that is here.

Why this file exists
--------------------

``docs/judgment_points.json`` is the manifest the judgment-surface gates
read: it names every function that might decide PASS/FAIL, where it
lives, and what shape of value it returns. It is a **checked-in
artifact**, so it can drift away from the source it describes — and a
drifted manifest is worse than no manifest at all, because a gate that
reads it still goes green while checking a file that stopped describing
the tree.

§12.2 asks for a static gate that keeps the manifest honest. A gate that
only says "the file exists" is a gate that cannot fail, and a gate that
cannot fail is the very thing §12.2 is aimed at. So this one checks the
manifest **against the source**, row by row:

* every module the manifest names is a file under ``backend/``;
* every function the manifest names is defined in that module's AST;
* the ``return_shape`` the manifest records is the shape that function's
  definition actually has.

What it pins
------------

1. ``test_manifest_is_non_empty_and_sorted`` — the manifest says
   something, and it says it in a fixed order.
2. ``test_every_module_file_exists`` — every module it names is on disk.
3. ``test_every_function_resolves_in_ast`` — every function it names is
   really defined there.
4. ``test_return_shape_matches_source`` — every shape it records is the
   shape the source has.

The four cases below those are the **negative controls**: each takes a
manifest that has been broken on purpose and requires the checks above
to *name the offender*. Without them, a check that reported nothing
would pass the four cases as happily as it passes a correct manifest —
and the gate would be green over a manifest that names nothing at all.
A gate that cannot go red is not a gate.

Scope, stated honestly: this gate **reads** the manifest and the source.
It never writes either.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

#: The repository root, three parents up from ``backend/tests/static_gates/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The manifest under gate.
_MANIFEST = _REPO_ROOT / "docs" / "judgment_points.json"
#: Where the judgment modules live, relative to the repository root.
_BACKEND_DIR = "backend"
#: The script that writes the manifest.
_SCRIPT = _REPO_ROOT / "scripts" / "census_judgment_points.py"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``. It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs. The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "census_judgment_points_static_gate_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
census = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = census
_spec.loader.exec_module(census)

#: Parsed modules, cached by path. The gate reads the same ten modules
#: once per check, and parsing them again for every row would be waste.
_TREES: dict[Path, ast.Module] = {}


def _module_path(root: Path, module: str) -> Path:
    """Where the module ``module`` lives under ``root``."""
    return root / _BACKEND_DIR / module


def _definitions(path: Path) -> dict[str, ast.AST]:
    """The functions ``path`` defines, keyed by qualified name.

    The census's own walker decides what a name is — that is the
    definition the manifest was written with, so it is the definition
    the manifest has to agree with. A manifest naming a function the
    walker does not report (a nested function, a dunder, a write path)
    names something the census would never have written down.
    """
    tree = _TREES.get(path)
    if tree is None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _TREES[path] = tree
    return dict(census.iter_functions(tree))


def _read_manifest(manifest_path: Path) -> dict:
    """The manifest at ``manifest_path``, parsed."""
    assert manifest_path.is_file(), (
        f"{manifest_path} is missing: there is no manifest to check"
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _points(manifest: dict) -> list[object]:
    """The manifest's rows, or an empty list when it has none."""
    points = manifest.get("points")
    return list(points) if isinstance(points, list) else []


def _ordering_problems(manifest_path: Path, manifest: dict) -> list[str]:
    """Is the manifest's own content sane? Empty, keyed, sorted, unique.

    The order is the census's (§12.5): sorted by ``(module, function)``.
    A repeated run has to diff to nothing, and an unsorted manifest
    cannot — the order would be the order the walker happened to meet
    the functions in, which is the source layout, not the content.
    """
    points = _points(manifest)
    if not points:
        return [
            f"{manifest_path.name} lists no judgment points at all: "
            "an empty manifest is not a census"
        ]

    problems: list[str] = []
    keys: list[tuple[str, str]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            problems.append(
                f"{manifest_path.name} point #{index} is not a row: {point!r}"
            )
            continue
        module = point.get("module")
        function = point.get("function")
        if not isinstance(module, str) or not isinstance(function, str):
            problems.append(
                f"{manifest_path.name} point #{index} does not name a module "
                f"and a function: {point!r}"
            )
            continue
        keys.append((module, function))

    if keys != sorted(keys):
        problems.append(
            f"{manifest_path.name} is not sorted by (module, function)"
        )
    duplicates = sorted(
        key for key, count in Counter(keys).items() if count > 1
    )
    if duplicates:
        problems.append(
            f"{manifest_path.name} names the same function more than once: "
            f"{duplicates}"
        )
    return problems


def _missing_modules(
    manifest_path: Path, manifest: dict, root: Path
) -> list[str]:
    """The modules the manifest names that are not on disk."""
    problems: list[str] = []
    modules = sorted(
        {
            point["module"]
            for point in _points(manifest)
            if isinstance(point, dict) and isinstance(point.get("module"), str)
        }
    )
    for module in modules:
        if not _module_path(root, module).is_file():
            problems.append(
                f"{manifest_path.name} names module {module!r}, which is not "
                f"a file under {_BACKEND_DIR}/"
            )
    return problems


def _unresolved_functions(
    manifest_path: Path, manifest: dict, root: Path
) -> list[str]:
    """The functions the manifest names that no module defines."""
    problems: list[str] = []
    for point in _points(manifest):
        if not isinstance(point, dict):
            continue
        module = point.get("module")
        function = point.get("function")
        if not isinstance(module, str) or not isinstance(function, str):
            continue
        module_path = _module_path(root, module)
        if not module_path.is_file():
            continue  # named by the module check
        if function not in _definitions(module_path):
            problems.append(
                f"{manifest_path.name} names {module}::{function}, which no "
                "function in that module defines"
            )
    return problems


def _mismatched_shapes(
    manifest_path: Path, manifest: dict, root: Path
) -> list[str]:
    """The rows whose recorded shape is not the shape the source has."""
    problems: list[str] = []
    for point in _points(manifest):
        if not isinstance(point, dict):
            continue
        module = point.get("module")
        function = point.get("function")
        if not isinstance(module, str) or not isinstance(function, str):
            continue
        module_path = _module_path(root, module)
        if not module_path.is_file():
            continue  # named by the module check
        node = _definitions(module_path).get(function)
        if node is None:
            continue  # named by the resolution check
        actual = census.classify_return_shape(node)
        recorded = point.get("return_shape")
        if recorded != actual:
            problems.append(
                f"{manifest_path.name} records {module}::{function} as "
                f"return_shape={recorded!r}, but its definition returns "
                f"{actual!r}"
            )
    return problems


def _write(tmp_path: Path, manifest: dict) -> Path:
    """Write ``manifest`` into ``tmp_path`` and return where it landed."""
    path = tmp_path / "judgment_points.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# The four cases the material names
# ---------------------------------------------------------------------------


def test_manifest_is_non_empty_and_sorted() -> None:
    """The manifest says something, and it says it in a fixed order.

    An empty manifest is not a manifest — it is a file that says the
    census found nothing, and every gate that reads it would go green
    over an empty room. The order matters for the same reason the
    census sorts (§12.5): a re-run has to diff to nothing, and an
    unsorted manifest cannot.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _ordering_problems(_MANIFEST, manifest)

    assert not problems, "\n".join(problems)


def test_every_module_file_exists() -> None:
    """Every module the manifest names is a file under ``backend/``.

    A module that is not on disk cannot be read, so every claim the
    manifest makes about it — how many functions it has, what shape they
    return — is a claim about a file that is not there.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _missing_modules(_MANIFEST, manifest, _REPO_ROOT)

    assert not problems, "\n".join(problems)


def test_every_function_resolves_in_ast() -> None:
    """Every function the manifest names is defined in that module.

    The manifest is a set of addresses. An address that does not resolve
    names nothing, and a reader who follows it finds no function — so
    the row is a claim about a function that is not there.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _unresolved_functions(_MANIFEST, manifest, _REPO_ROOT)

    assert not problems, "\n".join(problems)


def test_return_shape_matches_source() -> None:
    """Every recorded shape is the shape the function's definition has.

    The shape decides what happens to a function downstream: a ``bool``
    is a judgment, a ``reason_or_none`` says why, an ``other`` is
    neither. A manifest that records the wrong shape puts a function in
    the wrong bucket — silently, because the manifest is what the
    downstream gates read.
    """
    manifest = _read_manifest(_MANIFEST)

    problems = _mismatched_shapes(_MANIFEST, manifest, _REPO_ROOT)

    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The negative controls: each check has to be able to go red
# ---------------------------------------------------------------------------


def _first_resolvable_point(manifest: dict, root: Path) -> dict:
    """The first row of ``manifest`` that the source really has.

    The negative controls corrupt a row, so they need a row that is
    sound to begin with — one whose module is on disk and whose function
    is defined there. Picking it by search rather than by position keeps
    the controls honest when the manifest is *already* broken: they
    still prove what they claim, instead of failing for a reason that
    has nothing to do with what they are testing.
    """
    for point in _points(manifest):
        if not isinstance(point, dict):
            continue
        module = point.get("module")
        function = point.get("function")
        if not isinstance(module, str) or not isinstance(function, str):
            continue
        module_path = _module_path(root, module)
        if not module_path.is_file():
            continue
        if function not in _definitions(module_path):
            continue
        return point
    raise AssertionError(
        "the manifest has no row that resolves in the source, so there is "
        "nothing to corrupt: the gate above is already red"
    )


def test_a_module_that_is_not_on_disk_is_named(tmp_path: Path) -> None:
    """A manifest naming a module that does not exist is reported."""
    manifest = _read_manifest(_MANIFEST)
    point = _first_resolvable_point(manifest, _REPO_ROOT)
    point["module"] = "not_a_module_in_this_repo.py"
    path = _write(tmp_path, manifest)

    problems = _missing_modules(path, manifest, _REPO_ROOT)

    assert problems, (
        "a manifest naming a module that is not on disk passed the module check"
    )
    named = [p for p in problems if "not_a_module_in_this_repo.py" in p]
    assert named, f"the module check did not name the missing module: {problems}"


def test_a_function_that_is_not_in_the_ast_is_named(tmp_path: Path) -> None:
    """A manifest naming a function no module defines is reported."""
    manifest = _read_manifest(_MANIFEST)
    point = _first_resolvable_point(manifest, _REPO_ROOT)
    point["function"] = "not_a_function_in_this_repo"
    path = _write(tmp_path, manifest)

    problems = _unresolved_functions(path, manifest, _REPO_ROOT)

    assert problems, (
        "a manifest naming a function that is not defined passed the resolution check"
    )
    named = [p for p in problems if "not_a_function_in_this_repo" in p]
    assert named, f"the resolution check did not name the missing function: {problems}"


def test_a_return_shape_that_does_not_match_is_named(tmp_path: Path) -> None:
    """A manifest recording the wrong shape is reported, with both shapes."""
    manifest = _read_manifest(_MANIFEST)
    point = _first_resolvable_point(manifest, _REPO_ROOT)
    module_path = _module_path(_REPO_ROOT, point["module"])
    node = _definitions(module_path)[point["function"]]
    actual = census.classify_return_shape(node)
    wrong = "other" if actual != "other" else "bool"
    point["return_shape"] = wrong
    path = _write(tmp_path, manifest)

    problems = _mismatched_shapes(path, manifest, _REPO_ROOT)

    assert problems, (
        "a manifest recording the wrong return shape passed the shape check"
    )
    named = [p for p in problems if point["function"] in p]
    assert named, f"the shape check did not name the mismatched row: {problems}"
    assert wrong in named[0]
    assert actual in named[0]


def test_an_empty_manifest_is_rejected(tmp_path: Path) -> None:
    """A manifest with no rows at all is reported, not accepted."""
    manifest = {"schema_version": 1, "points": []}
    path = _write(tmp_path, manifest)

    problems = _ordering_problems(path, manifest)

    assert problems, "a manifest listing no judgment points was accepted"
    assert "no judgment points" in problems[0]