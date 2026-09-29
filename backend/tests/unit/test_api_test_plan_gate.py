"""Plan-phase gate for the api_test vocabulary (2026-09-27).

The plan-generation gate that calls
:func:`verification_api_runner.validate_vp` must report any VP whose
assertions reach for a key the runner does not know, so the
regeneration loop can rewrite it before the VP is persisted and burned
through a round.

The regression this pins is VP-007 on the 2026-09-26 plan. It declared
``body_not_contains`` — the natural negation of ``body_contains`` — at a
time when the runner's vocabulary was asymmetric and had no such
subject. Schema validation rejected the **whole VP**, the valid
``{"status": 404}`` assertion included, and the VP sent no request at
all for four consecutive rounds. Two things were wrong and both are
fixed here:

  1. the vocabulary had no way to say "the raw body must not contain
     X" — every leak check had to be written against a parsed field
     instead, which cannot see a non-JSON body (see
     ``test_api_test_vocabulary_equivalence.py``);
  2. when the retry budget ran out with the defect still present, the
     plan was persisted anyway under the "least-bad plan" fallback.
     For a service-reference violation or a missing Phase-2 gate that
     is defensible — those plans still *run*. For an unparseable
     ``api_test`` VP it is not: the VP is guaranteed FAILED no matter
     what the service does, so the fallback laundered a plan defect
     into what looked like a product failure. It now raises
     :class:`~verification_agent.ApiTestSchemaViolation` instead.

This file pins both halves of the contract:

  * :func:`test_out_of_vocabulary_assertion_triggers_regeneration` —
    a VP carrying an out-of-vocabulary subject shows up in
    :meth:`VerificationAgent._api_schema_report`.
  * :func:`test_valid_rewrite_passes_the_gate` — the rewrite the
    guidance teaches is not reported AND survives ``validate_vp``
    clean (a gate that flags the rewrite it teaches is a fixed point).
  * :func:`test_fix_guidance_teaches_only_vocabulary_keys` — every
    assertion key mentioned in the api_test paragraph of
    :meth:`VerificationAgent._violation_fix_guidance` is one of
    ``SUBJECT_KEYS`` or ``COMPARATOR_KEYS``, and the negative-contract
    spelling is taught explicitly.
  * :func:`test_non_api_test_vps_are_unaffected` —
    ``verification_method in {"ci_test", "full_ci"}`` is not even
    inspected. The gate is api_test-only; the other families keep
    their own discipline.

No part of this module exercises the network. ``validate_vp`` and
``_api_schema_report`` are pure functions over the VP / plan object,
so the tests stay deterministic without any live service.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_agent import VerificationAgent  # noqa: E402
from verification_api_runner import (  # noqa: E402
    COMPARATOR_KEYS,
    SUBJECT_KEYS,
    validate_vp,
)


#: The criterion this whole module is about. Pulled into a constant so
#: every test refers to the same string and a future tweak of the
#: criterion does not silently leave a test pinned to the old wording.
LEAK_TOKEN = "root:x:0:0"


def _make_agent(tmp_path: Path) -> VerificationAgent:
    """Build a VerificationAgent wired to a throwaway plan/project dir.

    The agent's ``_api_schema_report`` and ``_violation_fix_guidance``
    are pure methods over their inputs — they do not need a real
    project layout, only the constructor's surface (which writes
    venv-PATH prepends). Empty tmp dirs are enough.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _bad_vp(vp_id: str = "vp-bad") -> dict:
    """A VP whose only assertion names a key the vocabulary does not have.

    ``body_excludes`` is a plausible-looking synonym that no code path
    implements — which is the point. The gate's job is to catch *any*
    out-of-vocabulary key, not one specific historical spelling: the
    original incident happened because the planner reached for a
    symmetric name nobody had implemented, and the next one will be a
    different name.
    """
    return {
        "id": vp_id,
        "verification_method": "api_test",
        "title": "traversal kill",
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/api/plan/x/status",
        },
        "assertions": [{"name": "n", "body_excludes": LEAK_TOKEN}],
    }


def _good_vp(vp_id: str = "vp-good") -> dict:
    """The canonical form of the traversal-refusal criterion.

    Two assertions: a status half (``{"status": 404}``) and a body half
    (``{"body_not_contains": "..."}``). Both halves are required — a
    single-assertion VP that drops either one bypasses the other half of
    the criterion (see ``test_api_test_vocabulary_equivalence.py`` for
    the argument, including why the ``json_path`` substitute is not
    equivalent).
    """
    return {
        "id": vp_id,
        "verification_method": "api_test",
        "title": "traversal kill",
        "request": {
            "method": "GET",
            "url": "{{svc.api.url}}/api/plan/x/status",
        },
        "assertions": [
            {"name": "state is 404", "status": 404},
            {"name": "no leak", "body_not_contains": LEAK_TOKEN},
        ],
    }


# ---------------------------------------------------------------------------
# 1. The gate surfaces the withdrawn spelling — the regression the brief
# pins. Without this, VP-007 walks past the gate again.
# ---------------------------------------------------------------------------


def test_out_of_vocabulary_assertion_triggers_regeneration(tmp_path: Path):
    """A VP that names an unknown key as a subject is reported.

    The key is not in ``SUBJECT_KEYS``, so using it as a subject
    triggers the "has no subject" issue :func:`validate_vp` emits —
    which the gate must surface so the regeneration loop has something
    to feed back to the planner. This is exactly the regression VP-007
    walked into: the gate existed but the downstream consumer walked
    past it.
    """
    agent = _make_agent(tmp_path)

    report = agent._api_schema_report(
        {"verification_points": [_bad_vp("vp-bad")]}
    )

    assert [v["id"] for v in report] == ["vp-bad"], (
        f"_api_schema_report should flag the VP carrying the unknown "
        f"subject; got {[v['id'] for v in report]}"
    )
    # The issue must name the unknown-key shape — a gate that fires but
    # does not tell the planner what to fix is a gate that does not
    # close the loop. ``validate_vp``'s "has no subject" message
    # identifies the regression by name.
    issues = report[0]["issues"]
    assert any("subject" in d for d in issues), (
        f"_api_schema_report issues should mention 'subject' so the "
        f"regeneration loop knows the bad key is a subject, not a "
        f"comparator; got {issues!r}"
    )
    assert SUBJECT_KEYS, (
        f"sanity: SUBJECT_KEYS should not be empty; got {list(SUBJECT_KEYS)!r}"
    )


# ---------------------------------------------------------------------------
# 2. The gate accepts the rewrite it teaches AND that rewrite is valid.
# A gate that flags the rewrite it teaches is a fixed point.
# ---------------------------------------------------------------------------


def test_valid_rewrite_passes_the_gate(tmp_path: Path):
    """The canonical rewrite is invisible to the gate AND valid.

    The gate must NOT report this VP — it is the canonical "what the
    planner should write instead" shape, and a gate that flags the
    rewrite would be self-contradictory. Then :func:`validate_vp` must
    also pass it, so the guidance the gate feeds back is something the
    gate itself accepts on the next round.
    """
    agent = _make_agent(tmp_path)
    good = _good_vp()

    assert agent._api_schema_report(
        {"verification_points": [good]}
    ) == [], (
        "_api_schema_report flagged a VP that uses only canonical "
        "vocabulary keys — the gate must accept the rewrite it teaches, "
        "otherwise regeneration is a fixed point."
    )
    assert validate_vp(good) == [], (
        f"validate_vp returned issues for the canonical rewrite; "
        f"the gate teaches a spelling the gate rejects: "
        f"{[i.to_dict() for i in validate_vp(good)]!r}"
    )


# ---------------------------------------------------------------------------
# 3. The guidance teaches only vocabulary keys — no out-of-vocabulary
# spelling sneaks in via the prompt. The negative-contract subject must be
# taught explicitly.
# ---------------------------------------------------------------------------


def test_fix_guidance_teaches_only_vocabulary_keys(tmp_path: Path):
    """The api_test paragraph of ``_violation_fix_guidance`` names only
    vocabulary keys, AND teaches the negative-contract spelling.

    Every key mentioned inside a backticked dict-shaped example must
    be either a vocabulary key (``SUBJECT_KEYS`` or
    ``COMPARATOR_KEYS``) or a request-level field (``method`` /
    ``url``, which name the request shape, not an assertion). A future
    refactor that smuggles an out-of-vocabulary key into the guidance
    trips here before it reaches a planner.

    The paragraph must also teach ``body_not_contains`` explicitly. The
    planner reaching for the natural symmetric spelling is exactly how
    VP-007 was born; if the guidance does not show it the canonical
    name, the regeneration loop is a fixed point — every retry emits
    the same dead spelling, the gate keeps flagging it, and the round
    never converges. (An earlier revision taught ``json_path`` +
    ``not_contains`` instead, on the theory that the vocabulary was
    closed at four subjects. That rewrite is schema-legal but weaker:
    it examines a parsed JSON field, not the raw body, so it cannot see
    a leak at all on a non-JSON response.)
    """
    agent = _make_agent(tmp_path)

    text = agent._violation_fix_guidance(
        service_violations=[],
        api_schema_violations=[{"id": "VP-007"}],
        gate_gaps=[],
    )

    # The api_test paragraph is the section that starts with "对「api_test
    # 断言不合规」". The other two families (服务引用违规 / Phase 2 全量关
    # 卡缺失) carry their own discipline; the vocabulary rule only
    # applies to the api_test one, so we narrow before scanning.
    api_paragraph_match = re.search(
        r"对「api_test 断言不合规」.*?(?=对「|\Z)",
        text,
        re.DOTALL,
    )
    assert api_paragraph_match, (
        f"api_test paragraph not found in guidance text: {text!r}"
    )
    api_paragraph = api_paragraph_match.group(0)

    # Every key mentioned inside a backticked ``{...}`` example must be
    # either a vocabulary key or one of the request-level fields. The
    # exclusion list is small and explicit, so a future expansion is
    # visible (add a name → it must be a real addition, not a quiet
    # relaxation of the rule).
    vocab = set(SUBJECT_KEYS) | set(COMPARATOR_KEYS)
    request_level_keys = {"method", "url"}

    examples = re.findall(r"`(\{[^`]+?\})`", api_paragraph)
    out_of_vocab = []
    for example in examples:
        keys = re.findall(r'"([\w_]+)"\s*:', example)
        for key in keys:
            if key not in vocab and key not in request_level_keys:
                out_of_vocab.append((key, example))
    assert out_of_vocab == [], (
        f"api_test guidance paragraph mentions non-vocabulary keys "
        f"{[k for k, _ in out_of_vocab]!r}; only SUBJECT_KEYS, "
        f"COMPARATOR_KEYS, and the request-level fields {{method, url}} "
        f"are allowed in assertion examples. Offending examples: "
        f"{[ex for _, ex in out_of_vocab]!r}"
    )

    # The canonical negative-contract spelling must appear as a concrete
    # ``{body_not_contains: ...}`` example, not just as a bare token
    # somewhere in the paragraph. The planner needs the exact shape to
    # copy; a mention in prose leaves it to guess, and the guess is
    # precisely what went wrong the first time.
    has_negative_contract_example = any(
        '"body_not_contains"' in example for example in examples
    )
    assert has_negative_contract_example, (
        f"api_test guidance paragraph does not show a "
        f"{{body_not_contains: ...}} example; the planner needs a "
        f"concrete negative-contract template to copy rather than "
        f"inventing a spelling. Examples found: {examples!r}"
    )


# ---------------------------------------------------------------------------
# 4. The gate is api_test-only — other methods are not in scope.
# ---------------------------------------------------------------------------


def test_non_api_test_vps_are_unaffected(tmp_path: Path):
    """``ci_test`` / ``full_ci`` VPs do not enter the api_test report.

    The gate inspects ONLY ``verification_method == "api_test"``.
    Other methods (``ci_test``, ``full_ci``, ``code_review``,
    ``ui_validation``) have their own discipline; the api_test schema
    gate does not speak for them, and ``_api_schema_report`` must
    return ``[]`` for plans that contain only non-api_test VPs even
    when those VPs lack the request / assertions shape the api_test
    rule would otherwise complain about.
    """
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [
        # ``ci_test`` is not a real method on this codebase, but the
        # task brief calls it out by name; using it pins that the gate
        # does NOT special-case known methods — it special-cases only
        # ``api_test``.
        {"id": "VP-CI", "verification_method": "ci_test", "title": "ci"},
        # ``full_ci`` IS a real method (Phase-2 framework method). A
        # framework method with no request / assertions must not
        # be flagged by the api_test gate — ``full_ci`` runs a
        # declared command, not an HTTP request.
        {
            "id": "VP-FULLCI",
            "verification_method": "full_ci",
            "title": "full ci",
            "command": "echo ci",
        },
    ]}

    assert agent._api_schema_report(plan) == [], (
        "_api_schema_report must NOT touch non-api_test VPs; got "
        f"{agent._api_schema_report(plan)!r}"
    )