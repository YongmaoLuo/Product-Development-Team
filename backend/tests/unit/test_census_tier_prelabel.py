"""The Tier A pre-label: does a judgment's value reach the verdict?

Why this file exists
--------------------
The material (§4.1) defines a Tier A judgment point as a function whose
*return value* is written into a verdict, a status or a gate result —
and it is explicit that the criterion looks at the **data flow**, not at
the name: ``verification_evidence.verdict_subject_mismatch`` carries no
decision verb at all and still turns a PASSED verdict into a FAILED one.

So the census has to answer two questions for every function it walks:

* does the value this function produces reach a verdict slot
  (:func:`covers_verdict_write`)?
* is this function one of the *confirmed* Tier A points
  (:data:`TIER_A_CONFIRMED` — the material's §8 first batch, eight
  candidates covering nine functions)?

The second question is a frozen list, because that batch was picked by
people who had watched those functions misjudge. The first question is
mechanical, and the manifest carries both, so a reader can see where
the two disagree: ``verification_buckets.derive_index_buckets`` is one
such disagreement, and it is a real one — it derives the buckets *from*
the verdicts instead of writing into them.

The contract is pinned here by name:

* ``test_assignment_to_status_is_covered``
* ``test_verdict_subscript_is_covered``
* ``test_unrelated_call_is_not_covered``
* ``test_confirmed_candidate_gets_tier_a``

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
_MODULE_NAME = "census_judgment_points_tier_prelabel_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
census = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = census
_spec.loader.exec_module(census)


def _backend_module(name: str) -> Path:
    """The path of a backend module."""
    return _REPO_ROOT / "backend" / name


# ---------------------------------------------------------------------------
# The four cases the material names
# ---------------------------------------------------------------------------


def test_assignment_to_status_is_covered():
    """``status = _check(x)`` writes the judgment into a status slot.

    This is the shape the material asks for (§4.1): the value the
    judgment returns lands in a ``status`` slot, so the judgment can
    turn a PASS into a FAIL.
    """
    src = "def f(x):\n    status = _check(x)\n    return status"
    assert census.covers_verdict_write(ast.parse(src), "_check") is True


def test_verdict_subscript_is_covered():
    """``verdict['status'] = _check(x)`` writes into the verdict itself.

    The value does not have to land in a variable: a dict slot counts
    too, because that is where the status a verdict is read from
    actually lives.
    """
    src = "def f(x):\n    verdict['status'] = _check(x)\n    return verdict"
    assert census.covers_verdict_write(ast.parse(src), "_check") is True


def test_unrelated_call_is_not_covered():
    """``print(_check(x))`` hands the value to a printer.

    Nothing records it, so the judgment cannot flip anything — the
    call is real, the value simply goes nowhere that is read back.
    """
    src = "def f(x):\n    print(_check(x))\n    return None"
    assert census.covers_verdict_write(ast.parse(src), "_check") is False


def test_confirmed_candidate_gets_tier_a():
    """A §8 candidate is pre-labelled Tier A by the census.

    ``verdict_subject_mismatch`` is the material's own example of a
    judgment whose name says nothing about judging.
    """
    points = census.census_module(
        _backend_module("verification_evidence.py")
    )
    tiers = {point.function: point.tier for point in points}
    assert census.TIER_A == "A"
    assert tiers["verdict_subject_mismatch"] == census.TIER_A


# ---------------------------------------------------------------------------
# The published interface
# ---------------------------------------------------------------------------


def test_module_publishes_the_prelabel_interface():
    """The pre-label's two published names are there."""
    assert hasattr(census, "covers_verdict_write")
    assert hasattr(census, "TIER_A_CONFIRMED")


def test_confirmed_set_is_the_materials_eight_candidates():
    """§8 names eight candidates — the phase-2 gate is two of them.

    Frozen here as literals so that a change to the constant has to be
    a change to *this* test, not a silent redefinition.
    """
    assert census.TIER_A_CONFIRMED == frozenset(
        {
            (
                "agent.py",
                "AutonomousAgent._task_declared_files_exist",
            ),
            ("agent.py", "excess_declared_paths"),
            ("verification_buckets.py", "derive_index_buckets"),
            (
                "verification_evidence.py",
                "_projected_ran_the_declared_entry",
            ),
            ("verification_evidence.py", "_ran_the_declared_entry"),
            ("verification_evidence.py", "apply_subject_check_to_verdict"),
            ("verification_evidence.py", "verdict_subject_mismatch"),
            (
                "verification_executor.py",
                "VerificationExecutor._gate_block_reason_for",
            ),
            (
                "verification_executor.py",
                "VerificationExecutor.rerun_deferred_gate_layers",
            ),
        }
    )
    assert len(census.TIER_A_CONFIRMED) == 9


def test_confirmed_candidates_exist_in_their_modules():
    """Every confirmed candidate is a real function in the module named.

    A confirmed entry that no longer resolves would silently become a
    label nothing carries.
    """
    for module, function in census.TIER_A_CONFIRMED:
        path = _backend_module(module)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {name for name, _node in census.iter_functions(tree)}
        assert function in names, f"{module}::{function} is gone"


def test_prelabel_adds_a_tier_without_changing_the_function_set(tmp_path: Path):
    """The pre-label labels rows; it does not add or drop any.

    The census is a survey of the code as it is. A row that only exists
    to carry a tier would be an invented judgment point, and a function
    dropped for lack of a tier would be a hidden one.
    """
    module = tmp_path / "sample_module.py"
    module.write_text(
        "def alpha() -> bool:\n    return True\n", encoding="utf-8"
    )
    points = census.census_module(module)
    assert [point.function for point in points] == ["alpha"]
    assert points[0].tier == census.TIER_B
    assert points[0].covers_verdict_write is False


def test_only_confirmed_functions_are_tier_a():
    """Tier A is the confirmed set — no more, no less.

    A Tier A row is a promise: a decision table will be written for it
    (§4.3). Promising a table for a function nobody confirmed would
    make the promise hollow.
    """
    points = census.census_module(
        _backend_module("verification_evidence.py")
    )
    tier_a = sorted(
        point.function for point in points if point.tier == census.TIER_A
    )
    assert tier_a == [
        "_projected_ran_the_declared_entry",
        "_ran_the_declared_entry",
        "apply_subject_check_to_verdict",
        "verdict_subject_mismatch",
    ]
    assert all(
        point.tier in (census.TIER_A, census.TIER_B) for point in points
    )


def test_judgment_that_decides_the_caller_reaches_the_verdict():
    """A judgment used as a condition reaches the verdict through it.

    ``_inner`` is never assigned to a status: it decides whether
    ``_outer`` returns a reason, and that reason is written into the
    verdict. The value reaches the verdict through the call chain, which
    is why the criterion has to follow the chain and not stop at the
    assignment.
    """
    src = (
        "def _inner():\n"
        "    return True\n"
        "\n"
        "def _outer():\n"
        "    if _inner():\n"
        "        return None\n"
        "    return 'blocked'\n"
        "\n"
        "def _apply():\n"
        "    reason = _outer()\n"
        "    verdict['reasons'] = [reason]\n"
        "    return verdict\n"
    )
    assert census.covers_verdict_write(ast.parse(src), "_inner") is True


def test_value_hopped_through_a_local_reaches_the_verdict():
    """A value carried by a local into a verdict slot still reaches it.

    The intermediate name says nothing about verdicts; only where the
    value ends up does.
    """
    src = (
        "def f(x):\n"
        "    candidate = _check(x)\n"
        "    verdict['status'] = candidate\n"
        "    return verdict\n"
    )
    assert census.covers_verdict_write(ast.parse(src), "_check") is True


def test_function_that_writes_the_verdict_itself_is_covered():
    """A function that writes the verdict covers the verdict write.

    ``apply_subject_check_to_verdict`` is called from another module,
    so no call site inside its own module stores its value — but the
    function is the one that puts ``status = "FAILED"`` into the
    verdict, and that is a verdict write whoever calls it.
    """
    src = (
        "def apply(verdict):\n"
        "    checked = dict(verdict)\n"
        "    checked['status'] = 'FAILED'\n"
        "    return checked\n"
    )
    assert census.covers_verdict_write(ast.parse(src), "apply") is True