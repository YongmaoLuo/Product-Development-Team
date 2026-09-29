"""Expressing the traversal criterion in the canonical api_test vocabulary.

Why this module exists
-----------------------
The traversal refusal criterion has two halves:

  * the route refuses the request, and
  * the response body does not hand back the bytes ``root:x:0:0``.

Between 2026-09-26 and 2026-09-27 the second half had no subject to name.
The vocabulary was asymmetric — ``json_path`` and ``header`` both accept
the ``not_contains`` comparator, but the raw body accepted only
``body_contains`` — so a planner who reached for the symmetric spelling
``body_not_contains`` had the **whole VP** rejected at schema validation,
valid assertions included, and VP-007 sent no request at all for four
consecutive rounds.

There *was* a workaround, and it is worth stating plainly because an
earlier revision of this module mistook it for an equivalence::

    {"json_path": "$.detail", "not_contains": "root:x:0:0"}

That is **not** equivalent. It asserts something about a *field of a
parsed JSON document*; the criterion is about the *raw response body*.
They diverge exactly where it matters — a leak that is not valid JSON,
or a leak outside the one path named, is invisible to the
``json_path`` form and caught by the raw-body form.
:func:`test_json_path_substitute_cannot_see_a_non_json_body` is that
divergence, executed rather than asserted in prose.

``body_not_contains`` (added 2026-09-27) closes the asymmetry. This
module pins four properties of the result:

  1. the criterion is expressible in the canonical vocabulary;
  2. the ``json_path`` substitute is strictly weaker, and we keep the
     demonstration so the substitution is not re-adopted;
  3. a key outside the vocabulary is still rejected, loudly and with
     the legal set named;
  4. the body subjects are symmetric — both polarities exist, both are
     self-contained.

No part of this module exercises the network. ``validate_vp``,
``_subject_value`` and ``_comparator_and_expected`` are pure functions
over the VP object and a synthetic response, which is the same
isolation the schema gate relies on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_api_runner import (  # noqa: E402
    COMPARATOR_KEYS,
    SELF_CONTAINED_SUBJECTS,
    SUBJECT_KEYS,
    _comparator_and_expected,
    _evaluate_comparator,
    _subject_value,
    validate_vp,
)


#: The criterion this whole module is about. Pulled into a constant so
#: every test refers to the same string and a future tweak of the
#: criterion does not silently leave a test pinned to the old wording.
LEAK_TOKEN = "root:x:0:0"

#: A response that leaks, in the shape the traversal route would return
#: if the guard were missing. Plain text on purpose — ``/etc/passwd``
#: contents are not JSON, which is precisely the case the substitute
#: cannot see.
LEAKING_BODY = f"root:x:0:0:root:/root:/bin/sh\n{LEAK_TOKEN}\n"


def _vp(assertions: list, *, vp_id: str = "vp-eq") -> dict:
    """Wrap ``assertions`` in the minimum VP shell ``validate_vp`` reads.

    The request shape is deliberately trivial — ``validate_vp`` only
    inspects ``request.method`` / ``request.url`` and refuses to run
    the network anyway, so a placeholder URL is enough.
    """
    return {
        "id": vp_id,
        "verification_method": "api_test",
        "request": {"method": "GET", "url": "http://test/api/plan/x/status"},
        "assertions": list(assertions),
    }


def _decide(assertion: dict, *, status: int, body: str) -> tuple:
    """Run one assertion against a synthetic response, as the runner does.

    Returns ``(passed, explanation)`` — the explanation matters here
    because the whole point of the test below is that two spellings can
    both report "not passed" for *different reasons*, and only one of
    those reasons is a decision about the leak.
    """
    found, actual, label = _subject_value(assertion, status, {}, body)
    comparator, expected = _comparator_and_expected(assertion)
    passed, why = _evaluate_comparator(comparator, expected, found, actual)
    return passed, f"{label}: {why}"


def _evaluate(assertion: dict, *, status: int, body: str) -> bool:
    return _decide(assertion, status=status, body=body)[0]


# ---------------------------------------------------------------------------
# 1. The criterion is expressible in the canonical vocabulary.
# ---------------------------------------------------------------------------


def test_criterion_is_accepted():
    """The two-line declaration passes ``validate_vp`` with zero issues.

    Status half → ``{"status": 404}`` (self-contained: the value IS the
    expectation).

    Body half → ``{"body_not_contains": "root:x:0:0"}`` (self-contained:
    the raw body must NOT contain this substring).
    """
    declaration = [
        {"name": "穿越序列被拒为 404", "status": 404},
        {"name": "响应体不是系统文件内容", "body_not_contains": LEAK_TOKEN},
    ]

    issues = validate_vp(_vp(declaration))

    assert issues == [], (
        f"validate_vp returned issues for the canonical declaration "
        f"{declaration!r}: {[i.to_dict() for i in issues]!r}; this is the "
        f"2026-09-27 regression — if it fires, VP-007's entire assertion "
        f"list is voided again and no request is ever sent."
    )


def test_criterion_actually_decides_both_halves():
    """Anti-vacuity: the accepted declaration passes on a clean response
    and fails on a leaking one.

    Without this, "the vocabulary accepts the spelling" could be true of
    an assertion that decides nothing.
    """
    declaration = [
        {"name": "no leak", "body_not_contains": LEAK_TOKEN},
    ]

    assert _evaluate(declaration[0], status=404, body='{"detail":"Not found"}')
    assert not _evaluate(declaration[0], status=404, body=LEAKING_BODY), (
        "body_not_contains passed against a body that literally contains "
        "the leak token; the negative comparator is not being applied"
    )


# ---------------------------------------------------------------------------
# 2. The ``json_path`` substitute is weaker — kept as a regression guard so
#    it is not re-adopted as an "equivalent".
# ---------------------------------------------------------------------------


def test_json_path_substitute_cannot_see_a_non_json_body():
    """Why ``json_path`` + ``not_contains`` was never an equivalence.

    The substitute is schema-legal (it uses only canonical keys), so it
    passes every shape check. It also behaves identically to the
    canonical spelling on the happy path — which is exactly what made
    it look like an adequate workaround. The divergence shows up where
    the criterion actually matters:

      * a leaking body that is **not JSON** — the substitute resolves
        nothing, so it reports "not passed" for a reason that has
        nothing to do with leaks (and would have reported the same on a
        body that leaks nowhere);
      * a leaking body that is JSON but carries the leak **outside the
        one path named** — the substitute passes it, silently.

    Measured here rather than argued. The second case is the one that
    would have shipped: a partial-pass body leaking in a field nobody
    thought to name reads as PASSED.
    """
    substitute = {"json_path": "$.detail", "not_contains": LEAK_TOKEN}
    canonical = {"body_not_contains": LEAK_TOKEN}

    # The substitute is accepted by the schema — this is what made it
    # look like a workaround rather than a defect.
    assert validate_vp(_vp([substitute])) == []

    # On the happy path the two agree.
    assert _evaluate(substitute, status=404, body='{"detail":"Not found"}')
    assert _evaluate(canonical, status=404, body='{"detail":"Not found"}')

    # Divergence 1 — the leak is in the body but not in the named JSON
    # path. The substitute PASSES; the canonical spelling catches it.
    leak_elsewhere = '{"detail":"Not found","debug":"root:x:0:0"}'
    assert _evaluate(substitute, status=404, body=leak_elsewhere), (
        "the substitute was expected to PASS this body — that is the "
        "defect being demonstrated; if it now fails, the demonstration "
        "no longer holds and the equivalence argument needs revisiting"
    )
    assert not _evaluate(canonical, status=404, body=leak_elsewhere), (
        "body_not_contains passed a body that literally contains the "
        "leak token"
    )

    # Divergence 2 — the body is not JSON at all, so the substitute has
    # nothing to resolve. Both report "not passed", but for different
    # reasons, and only the canonical one is a statement about leaks.
    sub_passed, sub_why = _decide(substitute, status=404, body=LEAKING_BODY)
    can_passed, can_why = _decide(canonical, status=404, body=LEAKING_BODY)

    assert not sub_passed and not can_passed
    assert "root:x:0:0" not in sub_why or "not found" in sub_why.lower(), (
        f"expected the substitute to fail because the subject could not "
        f"be resolved, not because it found the leak; got {sub_why!r}"
    )
    assert "NOT to contain" in can_why, (
        f"expected the canonical spelling to fail *because it saw the "
        f"leak*; got {can_why!r}"
    )


# ---------------------------------------------------------------------------
# 3. A key outside the vocabulary is still rejected — loudly, and naming
#    the legal set so a rejected plan can be regenerated.
# ---------------------------------------------------------------------------


def test_unknown_subject_is_rejected_and_names_the_legal_set():
    """An invented spelling is an unknown key, not a silent pass.

    The rejection must be the *unknown-subject* shape (so the message
    tells the planner what to rewrite to), not "missing a comparator"
    or anything else — that distinction is what makes the
    regeneration guidance actionable.
    """
    vp = _vp([{"name": "no leak", "body_excludes": LEAK_TOKEN}], vp_id="vp-bad")

    issues = validate_vp(vp)

    assert issues, (
        f"validate_vp({vp!r}) returned no issues but the only assertion "
        f"names a key that is not in SUBJECT_KEYS; it must be rejected, "
        f"not silently accepted."
    )
    joined = ", ".join(repr(s) for s in SUBJECT_KEYS)
    assert any(joined in i.detail for i in issues), (
        f"the rejection did not name the legal subjects {SUBJECT_KEYS!r}; "
        f"details={[i.to_dict() for i in issues]!r}"
    )
    assert any("has no subject" in i.detail for i in issues), (
        f"the rejection did not use the unknown-subject shape; "
        f"details={[i.to_dict() for i in issues]!r}"
    )


def test_the_body_subjects_are_symmetric():
    """Both polarities of the raw-body check exist, and are self-contained.

    This is the property whose absence caused the whole incident: the
    raw body could be asserted *positively* but not *negatively*, so
    every leak check had to be written against some parsed field
    instead. Pin both halves so a future vocabulary trim cannot
    silently restore the asymmetry.
    """
    assert "body_contains" in SUBJECT_KEYS
    assert "body_not_contains" in SUBJECT_KEYS

    assert "body_contains" in SELF_CONTAINED_SUBJECTS
    assert "body_not_contains" in SELF_CONTAINED_SUBJECTS

    # Self-contained means the value IS the expectation: adding a
    # comparator on top is a schema error, same as for ``status``.
    issues = validate_vp(_vp([{"body_not_contains": "x", "equals": "x"}]))
    assert any("self-contained subject" in i.detail for i in issues), (
        f"body_not_contains accepted a comparator; it carries its own "
        f"expectation and must reject one. details="
        f"{[i.to_dict() for i in issues]!r}"
    )


# ---------------------------------------------------------------------------
# 4. The declaration covers BOTH halves of the criterion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_half",
    [
        # Status half dropped: the body assertion alone cannot stand in
        # for "the route refused", because the body could be a 200 with
        # a clean body — the runner would still grade it PASSED on the
        # body check, missing the refusal entirely.
        [{"name": "no leak", "body_not_contains": LEAK_TOKEN}],
        # Body half dropped: the status assertion alone cannot stand in
        # for "no leak", because the route could answer 404 with the
        # contents of /etc/passwd in the body — the status check would
        # still PASS and the leak would slip past.
        [{"name": "state is 404", "status": 404}],
    ],
    ids=["no-status", "no-body"],
)
def test_criterion_covers_both_halves(missing_half):
    """The full declaration is two lines; dropping either half is not the
    criterion.

    Each half is independently bypassable, so a one-line declaration is
    not a weaker version of the criterion — it is a different (and
    insufficient) one. The schema accepts the half-only forms, because
    one-assertion VPs are legal in general; it is *coverage* this test
    pins, not shape.
    """
    # The premise: ``missing_half`` is schema-clean. Without this the
    # negative below could be "schema rejected it" rather than "schema
    # accepted it but the criterion is incomplete".
    issues_for_half = validate_vp(_vp(missing_half))
    assert issues_for_half == [], (
        f"parametrisation error: the half-only VP {missing_half!r} was "
        f"itself rejected by validate_vp: "
        f"{[i.to_dict() for i in issues_for_half]!r}"
    )

    full = [
        {"name": "state is 404", "status": 404},
        {"name": "no leak", "body_not_contains": LEAK_TOKEN},
    ]

    assert len(full) == 2
    assert len(missing_half) == 1

    subjects_in_full = [next(s for s in SUBJECT_KEYS if s in a) for a in full]
    assert "status" in subjects_in_full, (
        f"the full declaration {full!r} does not name 'status'; that is "
        f"the refusal half of the criterion"
    )
    assert "body_not_contains" in subjects_in_full, (
        f"the full declaration {full!r} does not name 'body_not_contains'; "
        f"that is the leak-check half. A positive 'body_contains' cannot "
        f"say 'must NOT contain', and 'json_path' examines a parsed field "
        f"rather than the raw body"
    )

    # Every subject used is canonical — the declaration introduces no
    # fifth spelling.
    assert set(subjects_in_full).issubset(set(SUBJECT_KEYS))

    # The body half folds into the ``not_contains`` comparator, i.e. the
    # negative semantics is the existing one, not a new invention.
    body_half = next(a for a in full if "body_not_contains" in a)
    comparator, expected = _comparator_and_expected(body_half)
    assert comparator == "not_contains", (
        f"body_not_contains must fold into the existing 'not_contains' "
        f"comparator; got {comparator!r}"
    )
    assert expected == LEAK_TOKEN
    assert "not_contains" in set(COMPARATOR_KEYS), (
        f"not_contains is no longer in COMPARATOR_KEYS="
        f"{list(COMPARATOR_KEYS)!r}; the body half depends on it"
    )
