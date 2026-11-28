"""The semantic review — the model pass that catches what the fingerprint misses.

What this file pins
-------------------
* a review that finds the same root cause says so, names the candidate it
  matched, and flags that an alias should be written;
* a review with **no candidates** never calls the model at all and never
  asks for an alias — there is nothing to merge into, and paying a model
  to be told "no" is money for nothing;
* a model that answers with something that is not JSON does **not** blow
  up the caller: the review degrades to "different defect" and says why;
* the same for a model that times out or is not there at all;
* a model that names a fingerprint which is **not** among the candidates
  gets no alias — otherwise a hallucinated fingerprint would send a real
  defect's history to a stranger;
* the review itself writes nothing.  The alias is the caller's to write.

Why these tests are *integration* tests
---------------------------------------
The candidate list a caller passes in is not a hand-written dictionary in
production — it is a row read out of the registry, and the alias that
follows a verdict lands in the same database.  Both facts only hold
against a real SQLite file: the fingerprint is recomputed from the
registry's own material, and the alias row is resolved by the registry's
own alias chain.  A double would prove nothing about the round trip.

The database each test gets is the shared ``state_db`` fixture — a
migrated file inside that test's own ``tmp_path``, never the operator's
live ``state.db``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from framework_fix.classifier import (
    FailureHit,
    SemanticVerdict,
    semantic_review,
)
from framework_fix.fingerprint import compute_fingerprint
from framework_fix.registry import FrameworkFixRegistry
from framework_fix.repro import ReproAttempt, ReproductionResult
from framework_fix.spawner import build_envelope, spawn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hit(**overrides: Any) -> FailureHit:
    """The failure the review is asked about."""
    fields: Dict[str, Any] = {
        "vp_id": "VP-3",
        "plan_id": "plan-b",
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
        "framework_files": ["backend/framework_fix/registry.py"],
        "acceptance_critical": True,
        "out_of_scope_change": False,
    }
    fields.update(overrides)
    return FailureHit(**fields)


def _hit_material(**overrides: Any) -> Dict[str, Any]:
    """The same failure, in the shape the registry registers."""
    material: Dict[str, Any] = {
        "plan_id": "plan-a",
        "vp_id": "VP-1",
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
    }
    material.update(overrides)
    return material


class _FakeModel:
    """A model that answers with a canned text and records its prompt."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


def _json_answer(
    *, same: bool, matched: str = "", reason: str = "同一条断言"
) -> str:
    """A model answer in the shape the review asks for."""
    return json.dumps(
        {
            "same": same,
            "matched_fingerprint": matched,
            "reason": reason,
        },
        ensure_ascii=False,
    )


def _alias_rows(db_path: Path) -> List[tuple]:
    """Every alias row, read through a plain connection."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT alias_fingerprint, canonical_fingerprint "
            "FROM framework_fix_alias ORDER BY alias_fingerprint"
        ).fetchall()
    finally:
        conn.close()


def _write_alias(
    db_path: Path, alias_fingerprint: str, canonical_fingerprint: str
) -> None:
    """The caller's side of the contract: write the alias row.

    The registry owns the alias table; this stands in for the registry's
    own alias writer so that the test can prove the *round trip* — a
    verdict, an alias row, and a lookup that resolves through it — before
    the two are wired together inside the registry.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO framework_fix_alias ("
            "  alias_fingerprint, canonical_fingerprint, created_at"
            ") VALUES (?, ?, datetime('now'))",
            (alias_fingerprint, canonical_fingerprint),
        )
        conn.commit()
    finally:
        conn.close()


def _events(db_path: Path) -> List[Dict[str, Any]]:
    """Every audit-chain row, oldest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, fingerprint, fix_plan_id, payload_json "
            "FROM framework_fix_event ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _judgements(db_path: Path) -> List[Dict[str, Any]]:
    """The agent judgements on the audit chain."""
    return [
        event for event in _events(db_path)
        if event["event_type"] == "agent_judged"
    ]


def _repro() -> ReproductionResult:
    """A reproduction that failed twice, the same way."""
    attempts = (
        ReproAttempt(
            run=1,
            exit_code=1,
            timed_out=False,
            first_error_line="AssertionError: expected 1, got 2",
            duration_s=1.5,
        ),
        ReproAttempt(
            run=2,
            exit_code=1,
            timed_out=False,
            first_error_line="AssertionError: expected 1, got 2",
            duration_s=1.4,
        ),
    )
    return ReproductionResult(
        classification="framework_deterministic",
        attempts=attempts,
        clean_commit="a" * 40,
        tree_fingerprint="sha256:" + "b" * 64,
    )


def _envelope_for(hit: FailureHit) -> Dict[str, Any]:
    """An envelope for this failure, with the fingerprint its material gives."""
    fingerprint = compute_fingerprint(
        framework_files=list(hit.framework_files),
        error_class=hit.error_class,
        first_error_line=hit.first_error_line,
    )
    return build_envelope(
        fingerprint=fingerprint,
        hit=hit,
        repro=_repro(),
        repo="repo-a",
    )


def _routing_block(plan_id: str) -> Dict[str, Any]:
    """The framework-fix block the routing row carries for this plan."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"the routing row of {plan_id!r} is missing"
    return json.loads(row[0])


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_same_writes_alias(state_db: Path) -> None:
    """Same root cause: the verdict names the candidate and asks for an alias."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit_material())

    candidate = registry.lookup(canonical)
    assert candidate is not None, "the candidate must come from the registry"

    # A different wording of the same assertion — a different mechanical
    # fingerprint, which is exactly the case the review exists for.
    hit = _hit(first_error_line="AssertionError: expected 1, got 2 (wording)")
    model = _FakeModel(
        _json_answer(same=True, matched=canonical, reason="同一条断言")
    )

    verdict = semantic_review(hit, [candidate], model)

    assert isinstance(verdict, SemanticVerdict)
    assert verdict.same is True, "the model found the same root cause"
    assert verdict.matched_fingerprint == canonical, (
        "the verdict must name the candidate it matched; got "
        f"{verdict.matched_fingerprint!r}"
    )
    assert verdict.alias is True, (
        "a same-cause verdict must ask the caller to write the alias"
    )
    assert verdict.reason == "同一条断言", (
        "the model's own reason is the most useful one to keep; got "
        f"{verdict.reason!r}"
    )

    # The review writes nothing: the alias is the caller's to write.
    assert _alias_rows(state_db) == [], (
        "the review must not write to the registry — that is the caller's "
        "job, and the caller's transaction"
    )

    # ... and when the caller writes it, the new fingerprint resolves to
    # the canonical one through the registry's alias chain.
    new_fingerprint = registry.register_hit(
        _hit_material(
            plan_id="plan-b",
            first_error_line="AssertionError: expected 1, got 2 (wording)",
        )
    )
    assert new_fingerprint != canonical, (
        "the two wordings must be different mechanical fingerprints, "
        "otherwise this test proves nothing"
    )
    _write_alias(state_db, new_fingerprint, canonical)

    resolved = registry.lookup(new_fingerprint)
    assert resolved is not None, "the aliased fingerprint must resolve"
    assert resolved["fingerprint"] == canonical, (
        "the alias must point at the canonical fingerprint; got "
        f"{resolved['fingerprint']!r}"
    )


def test_no_candidates(state_db: Path) -> None:
    """No candidates: no verdict, no model call, no alias."""
    model = _FakeModel(_json_answer(same=True, matched="sha256:whatever"))

    verdict = semantic_review(_hit(), [], model)

    assert verdict.same is False, "nothing to merge into means not the same"
    assert verdict.matched_fingerprint is None
    assert verdict.alias is False, "an alias needs a target; there is none"
    assert model.prompts == [], (
        "the model must not be called when there is no candidate to judge "
        f"against; it was called {len(model.prompts)} time(s)"
    )
    assert _alias_rows(state_db) == []


# ---------------------------------------------------------------------------
# The model fails
# ---------------------------------------------------------------------------
def test_bad_json(state_db: Path) -> None:
    """A model that answers with something else is not allowed to raise."""
    model = _FakeModel("这不是 JSON，只是一段模型的自言自语。")

    verdict = semantic_review(_hit(), [{"fingerprint": "sha256:a"}], model)

    assert verdict.same is False, (
        "an unparseable answer is not a verdict; the review degrades to "
        "'different defect'"
    )
    assert verdict.matched_fingerprint is None
    assert verdict.alias is False
    assert "JSON" in verdict.reason, (
        "the reason must say what went wrong; got " f"{verdict.reason!r}"
    )
    assert _alias_rows(state_db) == []


def test_timeout_degrades_to_not_same(state_db: Path) -> None:
    """A model that times out degrades to 'different defect', quietly."""

    def timeout(prompt: str) -> str:
        raise TimeoutError("model timed out")

    verdict = semantic_review(_hit(), [{"fingerprint": "sha256:a"}], timeout)

    assert verdict.same is False
    assert verdict.matched_fingerprint is None
    assert verdict.alias is False
    assert "超时" in verdict.reason, (
        "the reason must record the timeout; got " f"{verdict.reason!r}"
    )
    assert _alias_rows(state_db) == []


def test_model_unavailable_degrades_to_not_same(state_db: Path) -> None:
    """A model that is not there at all degrades the same way."""

    def unavailable(prompt: str) -> str:
        raise RuntimeError("agent unavailable")

    verdict = semantic_review(_hit(), [{"fingerprint": "sha256:a"}], unavailable)

    assert verdict.same is False
    assert verdict.alias is False
    assert "失败" in verdict.reason, (
        "the reason must record the failure; got " f"{verdict.reason!r}"
    )


def test_unknown_fingerprint_gets_no_alias(state_db: Path) -> None:
    """A fingerprint the model invents is not a target an alias may point at."""
    model = _FakeModel(
        _json_answer(same=True, matched="sha256:" + "f" * 64)
    )

    verdict = semantic_review(
        _hit(), [{"fingerprint": "sha256:a"}], model
    )

    assert verdict.same is False, (
        "the model named a fingerprint nobody offered — that is not a "
        "verdict about the candidates"
    )
    assert verdict.matched_fingerprint is None
    assert verdict.alias is False
    assert _alias_rows(state_db) == []


def test_fenced_json_is_accepted(state_db: Path) -> None:
    """Models wrap JSON in code fences; that is still an answer."""
    model = _FakeModel(
        "```json\n"
        + _json_answer(same=True, matched="sha256:a", reason="同一条断言")
        + "\n```"
    )

    verdict = semantic_review(_hit(), [{"fingerprint": "sha256:a"}], model)

    assert verdict.same is True
    assert verdict.matched_fingerprint == "sha256:a"
    assert verdict.alias is True


# ---------------------------------------------------------------------------
# What the model is shown
# ---------------------------------------------------------------------------
def test_prompt_carries_the_evidence(state_db: Path) -> None:
    """The model is shown the failure and the candidates it must judge."""
    model = _FakeModel(_json_answer(same=False))

    semantic_review(
        _hit(
            error_class="KeyError",
            first_error_line="KeyError: 'registry'",
            framework_files=["backend/framework_fix/registry.py"],
        ),
        [{"fingerprint": "sha256:a", "first_error_line": "assert 1 == 2"}],
        model,
    )

    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert "KeyError" in prompt, "the error class must reach the model"
    assert "KeyError: 'registry'" in prompt, (
        "the first error line must reach the model — it is the evidence"
    )
    assert "sha256:a" in prompt, "the candidate fingerprints must reach the model"
    assert "assert 1 == 2" in prompt, (
        "the candidate's own evidence must reach the model, or it cannot "
        "compare the two"
    )


def test_candidate_without_fingerprint_is_not_offered(state_db: Path) -> None:
    """A candidate with no fingerprint cannot be matched, so it is dropped."""
    model = _FakeModel(_json_answer(same=True, matched=""))

    verdict = semantic_review(
        _hit(),
        [{"first_error_line": "assert 1 == 2"}],
        model,
    )

    assert model.prompts == [], (
        "a candidate without a fingerprint is not a candidate"
    )
    assert verdict.same is False
    assert verdict.alias is False


def test_verdict_can_be_recorded_on_the_hit(state_db: Path) -> None:
    """The verdict is storable on the registry row it belongs to."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit_material())
    candidate = registry.lookup(canonical)
    assert candidate is not None

    model = _FakeModel(_json_answer(same=True, matched=canonical))
    verdict = semantic_review(_hit(), [candidate], model)

    fingerprint = registry.register_hit(
        _hit_material(
            plan_id="plan-b",
            first_error_line="AssertionError: expected 1, got 2 (wording)",
            semantic_review={
                "same": verdict.same,
                "matched_fingerprint": verdict.matched_fingerprint,
                "alias": verdict.alias,
                "reason": verdict.reason,
            },
        )
    )

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["semantic_review"]["same"] is True
    assert row["semantic_review"]["matched_fingerprint"] == canonical


# ---------------------------------------------------------------------------
# The review in the production path
# ---------------------------------------------------------------------------
# The review only earns its place if something calls it.  The spawner is
# the entry point that turns a blocking hit into a repair plan, so it is
# where the review belongs: it asks the model whether this failure is the
# same defect as one that is already registered, writes the alias when the
# answer is yes, and records the judgement either way.
def test_the_spawner_writes_the_alias_the_review_asks_for(
    state_db: Path,
) -> None:
    """Same root cause: the alias lands and the judgement is audited."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit_material())

    hit = _hit(first_error_line="AssertionError: expected 1, got 2 (wording)")
    envelope = _envelope_for(hit)
    new_fingerprint = str(envelope["fingerprint"])
    assert new_fingerprint != canonical, (
        "the two wordings must be different mechanical fingerprints, "
        "otherwise this test proves nothing"
    )

    model = _FakeModel(
        _json_answer(same=True, matched=canonical, reason="同一条断言")
    )
    spawn(envelope, model_call=model)

    assert _alias_rows(state_db) == [(new_fingerprint, canonical)], (
        "the verdict asks for an alias and the entry point must write it; "
        f"got {_alias_rows(state_db)!r}"
    )

    judgements = _judgements(state_db)
    assert len(judgements) == 1, (
        "the judgement is an agent decision and belongs on the audit "
        f"chain; got {judgements!r}"
    )
    payload = json.loads(judgements[0]["payload_json"])
    assert payload["same"] is True
    assert payload["reason"] == "同一条断言"
    assert payload["matched_fingerprint"] == canonical
    assert judgements[0]["fingerprint"] == new_fingerprint


def test_a_different_cause_is_judged_and_writes_no_alias(
    state_db: Path,
) -> None:
    """Different root cause: no alias, but the judgement is still audited."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit_material())

    hit = _hit(first_error_line="AssertionError: expected 1, got 2 (wording)")
    envelope = _envelope_for(hit)

    model = _FakeModel(_json_answer(same=False, reason="不同的缺陷"))
    spawn(envelope, model_call=model)

    assert _alias_rows(state_db) == [], (
        "a different defect must not be merged into another one; got "
        f"{_alias_rows(state_db)!r}"
    )

    judgements = _judgements(state_db)
    assert len(judgements) == 1, (
        "the judgement is recorded whether it says same or different; got "
        f"{judgements!r}"
    )
    payload = json.loads(judgements[0]["payload_json"])
    assert payload["same"] is False
    assert payload["reason"] == "不同的缺陷"
    assert payload["matched_fingerprint"] is None


def test_the_same_defect_gets_one_repair_plan(state_db: Path) -> None:
    """A signature judged to be the same defect repairs the canonical one."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit_material())

    hit = _hit(first_error_line="AssertionError: expected 1, got 2 (wording)")
    envelope = _envelope_for(hit)

    model = _FakeModel(_json_answer(same=True, matched=canonical))
    plan_id = spawn(envelope, model_call=model)

    block = _routing_block(plan_id)
    assert block["fingerprint"] == canonical, (
        "the repair plan must repair the canonical defect, not open a "
        f"second plan for the same one; got {block.get('fingerprint')!r}"
    )
    row = registry.lookup(canonical)
    assert row is not None
    assert row["fix_plan_id"] == plan_id, (
        "the canonical defect's row must point at the repair plan; got "
        f"{row['fix_plan_id']!r}"
    )


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    raise SystemExit(
        "run this file with pytest: "
        "backend/.venv/bin/python3 -m pytest backend/tests/integration/"
        "framework_fix/test_semantic_review.py -v"
    )
