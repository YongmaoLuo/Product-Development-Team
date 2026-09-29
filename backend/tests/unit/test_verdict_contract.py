"""The verdict contract after the cross-verify layer was removed (2026-09-18).

What this file replaces
-----------------------
``tests/unit/test_cross_verify_zero_tests.py`` tested the old
**cross-verify** layer: the sub-agent reported ``pytest_exit_code`` and
``tests_run`` *about itself*, and ``parse_verdict`` let those numbers
override its verdict.

That layer is gone, and the reason is worth stating where its tests used
to live: a signal that one agent both produces and interprets is not a
second source of truth. It produced *opposite* verdicts for an unchanged
artifact — ``tests_run=0`` in one round (→ FAILED) and "Exit code 0
confirms successful execution" in a later one (→ PASSED). The
failed-VP set therefore differed every round, ``same_failure_repeated``
never fired, and the loop always ran out to ``max_rounds`` while
generating repair tasks against phantom failures.

Each method now carries a basis the *framework* can check:

  * ``api_test``        — executed and graded by ``verification_api_runner``;
  * ``code_review``     — must produce ``citations.json``;
  * ``ui_validation``   — must produce ``checkpoints.json``.

The invariant pinned here is the absence itself: no entry point in the
verdict path may accept an exit code or a test count again.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent as vs  # noqa: E402
from verification_subagent import (  # noqa: E402
    VerdictParseError,
    parse_verdict,
    parse_verdict_to_dataclass,
)


# ---------------------------------------------------------------------------
# The layer is gone — structurally, not just behaviourally
# ---------------------------------------------------------------------------


def test_parse_verdict_takes_no_exit_code_arguments():
    """A signature lock, not a style preference: if an exit code can be
    passed in, an agent's self-report can override its own verdict again,
    and that is the exact bug this rework removed."""
    params = list(inspect.signature(parse_verdict).parameters)

    assert params == ["subagent_output"], params


def test_parse_verdict_to_dataclass_takes_only_raw_output():
    params = list(inspect.signature(parse_verdict_to_dataclass).parameters)

    assert params == ["raw_output"], params


def test_the_tests_run_coercion_is_gone():
    assert not hasattr(vs, "_coerce_tests_run")


def test_verdict_path_source_has_no_cross_verify_machinery():
    """A *compiled*-code lock, so a docstring that merely explains the
    removal cannot satisfy it (nor trip it)."""
    code = parse_verdict.__code__
    identifiers = set(code.co_names) | set(code.co_varnames)
    constants = {c for c in code.co_consts if isinstance(c, str)}

    for identifier in ("pytest_exit_code", "tests_run", "cross_verify_status"):
        assert identifier not in identifiers, (
            f"{identifier!r} is referenced by the verdict-parsing code again"
        )
    assert "enforced" not in constants, (
        "'enforced' — the old override marker — is back in parse_verdict"
    )
    assert "skipped" not in constants, (
        "'skipped' — the old cross-verify status — is back in parse_verdict"
    )


# ---------------------------------------------------------------------------
# What parse_verdict still does: validate the shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["PASSED", "FAILED"])
def test_a_well_formed_verdict_is_returned(verdict: str):
    assert parse_verdict({"verdict": verdict}) == verdict


def test_skipped_is_coerced_to_failed():
    """PRD decision point 3 — zero tolerance for skipping."""
    assert parse_verdict({"verdict": "SKIPPED"}) == "FAILED"


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ("not-a-dict", "must be a dict"),
        ({}, "missing required field"),
        ({"verdict": 1}, "must be a str"),
        ({"verdict": "MAYBE"}, "must be 'PASSED' or 'FAILED'"),
        ({"verdict": "passed"}, "must be 'PASSED' or 'FAILED'"),
    ],
)
def test_malformed_verdicts_raise(payload, fragment: str):
    with pytest.raises(VerdictParseError) as exc:
        parse_verdict(payload)

    assert fragment in str(exc.value)


def test_a_self_reported_exit_code_is_not_even_read():
    """Extra fields are ignored — there is nothing that consults them."""
    assert parse_verdict({
        "verdict": "PASSED",
        "pytest_exit_code": 1,      # would have forced FAILED before
        "tests_run": 0,             # would have forced FAILED before
    }) == "PASSED"


# ---------------------------------------------------------------------------
# The dataclass wrapper keeps its richer contract
# ---------------------------------------------------------------------------


def test_dataclass_wrapper_carries_the_fields_through():
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "PASSED", "reasons": ["ok"], "evidence": ["out"],'
        ' "provider": "p", "chosen_model": "m"}'
    )

    assert verdict.verdict == "PASSED"
    assert verdict.reasons == ["ok"]
    assert verdict.evidence == ["out"]
    assert verdict.provider == "p"


def test_dataclass_wrapper_requires_evidence():
    """Decision point 4 — stricter than ``parse_verdict`` on purpose."""
    with pytest.raises(VerdictParseError) as exc:
        parse_verdict_to_dataclass('{"verdict": "PASSED", "reasons": []}')

    assert "evidence" in str(exc.value)


def test_dataclass_wrapper_coerces_skipped():
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "SKIPPED", "reasons": [], "evidence": []}'
    )

    assert verdict.verdict == "FAILED"


def test_dataclass_wrapper_rejects_malformed_json():
    with pytest.raises(VerdictParseError):
        parse_verdict_to_dataclass("{not json")
