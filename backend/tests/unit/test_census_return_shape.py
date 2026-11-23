"""The return-shape classifier behind the judgment-point census.

Why this file exists
--------------------
"Which functions decide PASS/FAIL?" cannot be answered by grepping for
names: ``verification_evidence.verdict_subject_mismatch`` carries no
decision verb and still turns a PASSED verdict into a FAILED one. The
material (§6.1) therefore asks for an AST census — walk the
judgment-relevant modules, classify every function by the *shape* of
what it returns, and let the shape decide what happens next.

The classifier is the one place in that pipeline where a wrong answer is
silent. A function misclassified as ``other`` drops out of the candidate
pool and nobody notices; a function misclassified as ``bool`` puts a
non-judgment function into a decision table that then has to carry it.
So the contract is pinned here by name:

* ``test_bool_annotated_is_bool``
* ``test_optional_str_is_reason_or_none``
* ``test_unannotated_is_other``
* ``test_nested_and_dunder_skipped``

The remaining cases pin the rest of the published interface
(:data:`JUDGMENT_MODULES`, :func:`census_module`) and the two boundary
conditions the material states alongside the four above: ``async def``
counts, and the module list is the material's eleven modules.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_redact_leaked_secrets.py`` loads
its script that way.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

#: The repository root, four parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
#: The census script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "census_judgment_points.py"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``. It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs. The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "census_judgment_points_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
census = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = census
_spec.loader.exec_module(census)


def _first_function(source: str) -> ast.AST:
    """The first module-level function (sync or async) in ``source``."""
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise AssertionError(f"no module-level function in snippet: {source!r}")


def _function_names(source: str) -> list[str]:
    """The qualified names the census walker reports for ``source``."""
    return [name for name, _node in census.iter_functions(ast.parse(source))]


# ---------------------------------------------------------------------------
# The four cases the material names
# ---------------------------------------------------------------------------


def test_bool_annotated_is_bool():
    """``-> bool`` is the shape of a function that answers yes/no."""
    src = "def f(x) -> bool: return x"
    assert census.classify_return_shape(ast.parse(src).body[0]) == "bool"


def test_optional_str_is_reason_or_none():
    """``-> Optional[str]`` is the shape of "why did this fail, or nothing"."""
    src = "def f(x) -> Optional[str]: return None"
    assert census.classify_return_shape(_first_function(src)) == "reason_or_none"


def test_unannotated_is_other():
    """No annotation means the census refuses to guess."""
    src = "def f(x): return x"
    assert census.classify_return_shape(_first_function(src)) == "other"


def test_nested_and_dunder_skipped():
    """A function defined inside another function, and a dunder, are not
    judgment points: the first is an implementation detail of its
    enclosing function, the second is protocol plumbing."""
    src = """
def outer():
    def inner() -> bool:
        return True
    return inner


class Helper:
    def __init__(self):
        pass

    def method(self) -> bool:
        return True
"""
    assert _function_names(src) == ["outer", "Helper.method"]


# ---------------------------------------------------------------------------
# The rest of the published interface
# ---------------------------------------------------------------------------


def test_union_str_none_is_reason_or_none():
    """``-> str | None`` is the same shape as ``-> Optional[str]``."""
    src = "def f(x) -> str | None: return None"
    assert census.classify_return_shape(_first_function(src)) == "reason_or_none"


def test_quoted_annotation_is_classified():
    """A quoted annotation is still an annotation — it is read, not ignored."""
    src = "def f(x) -> 'bool': return True"
    assert census.classify_return_shape(_first_function(src)) == "bool"


def test_async_def_is_counted():
    """An ``async def`` judges just as much as a ``def`` does."""
    src = "async def f(x) -> bool: return True"
    assert census.classify_return_shape(_first_function(src)) == "bool"
    assert _function_names(src) == ["f"]


def test_judgment_modules_pins_material_section_6_2():
    """The module list is the material's eleven modules, verbatim."""
    assert census.JUDGMENT_MODULES == (
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


def test_census_module_returns_sorted_function_points(tmp_path: Path):
    """``census_module`` reports one point per function, sorted by name.

    The sort is what makes the census reproducible: two runs over the
    same file must produce byte-identical output, and the manifest this
    feeds is compared against a re-run in CI.
    """
    module = tmp_path / "sample_module.py"
    module.write_text(
        "def zulu() -> bool:\n    return True\n\n"
        "def alpha():\n    return None\n",
        encoding="utf-8",
    )
    assert census.census_module(module) == [
        census.FunctionPoint(
            module="sample_module.py", function="alpha", return_shape="other"
        ),
        census.FunctionPoint(
            module="sample_module.py", function="zulu", return_shape="bool"
        ),
    ]


def test_property_setter_is_not_a_census_point():
    """A setter is a write path — nobody reads what it returns.

    ``@value.setter`` is also a *second definition* of the name
    ``value``. Reporting it would both invent a judgment point that
    cannot judge and hide the getter, which is the definition that says
    what reading the attribute returns.
    """
    src = """
class Holder:
    @property
    def value(self) -> Optional[str]:
        return self._value

    @value.setter
    def value(self, new) -> None:
        self._value = new
"""
    reported = [
        (name, census.classify_return_shape(node))
        for name, node in census.iter_functions(ast.parse(src))
    ]
    assert reported == [("Holder.value", "reason_or_none")]


def test_shadowed_definition_is_reported_once():
    """A name defined twice is one function, and the live one is reported.

    The second definition shadows the first, so the first is unreachable
    code — Python would never call it. Reporting both would put two rows
    with the same ``(module, function)`` key into the manifest, and the
    manifest's key has to be unique for a row to name *one* function.
    """
    src = """
class Gate:
    def check(self) -> bool:
        return False

    def check(self) -> Optional[str]:
        return "live"
"""
    reported = [
        (name, census.classify_return_shape(node))
        for name, node in census.iter_functions(ast.parse(src))
    ]
    assert reported == [("Gate.check", "reason_or_none")]


def test_census_module_reports_one_point_per_function(tmp_path: Path):
    """No two rows of a module's census share a function name."""
    module = tmp_path / "shadowed_module.py"
    module.write_text(
        "class Gate:\n"
        "    def check(self) -> bool:\n"
        "        return False\n\n"
        "    def check(self) -> bool:\n"
        "        return True\n",
        encoding="utf-8",
    )
    points = census.census_module(module)
    names = [point.function for point in points]
    assert names == ["Gate.check"]
    assert len(set(names)) == len(names)
