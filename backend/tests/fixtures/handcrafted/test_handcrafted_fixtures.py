"""Handcrafted fixtures for SplitDecision.should_split.

These 6 test cases are the 手工构造 (handcrafted) fixtures requested by
verification point VP-016. They cover all 5 early-return branches of
``should_split`` plus the happy path. See ``coverage.md`` for the
branch → test mapping.

The fixtures live in this directory so the verification command

    source venv1/bin/activate && pytest tests/fixtures/handcrafted/ -v

collects exactly these 6 cases (and only these — we keep the package
deliberately free of shared I/O / state).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the backend package importable when pytest is run from
# ``backend/`` (the project default) or from the repo root.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from verification_split import SplitDecision  # noqa: E402


# ---------------------------------------------------------------------------
# Branch 1: non-Mapping input → return None
# ---------------------------------------------------------------------------

def test_branch1_non_mapping_inputs_return_none() -> None:
    """Branch 1: ``not isinstance(vp, Mapping) or not isinstance(result, Mapping)``.

    Passing a list (or any non-Mapping) for either argument must short-circuit
    before the splitter inspects the timeout status. The function is defensive
    here because callers upstream may serialize via JSON and lose dict-ness.
    """
    # A list is a valid non-Mapping input that must trip the guard.
    assert SplitDecision.should_split(["VP-001"], {"status": "timeout"}) is None
    assert SplitDecision.should_split({"id": "VP-001", "expected_result": "A; B"}, "not-a-dict") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Branch 2: status != "timeout" → return None
# ---------------------------------------------------------------------------

def test_branch2_non_timeout_status_returns_none() -> None:
    """Branch 2: ``result.get("status") != "timeout"``.

    Only timed-out VPs are eligible for sub-task decomposition. A
    ``failed`` VP is the repair-task generator's job, not the splitter's.
    """
    vp = {
        "id": "VP-002",
        "verification_method": "automated_test",
        "expected_result": "登录成功; 凭据校验通过",
    }
    result = {"id": "VP-002", "status": "failed"}
    assert SplitDecision.should_split(vp, result) is None


# ---------------------------------------------------------------------------
# Branch 3: expected_result has no ';' separator → return None
# ---------------------------------------------------------------------------

def test_branch3_expected_result_without_separator_returns_none() -> None:
    """Branch 3: ``";" not in expected_result`` (or expected_result not a str).

    The splitter needs at least one ``;`` to chunk along. A single-clause
    expectation such as ``"登录成功"`` is atomic and must not be decomposed.
    """
    vp = {
        "id": "VP-003",
        "verification_method": "automated_test",
        "expected_result": "登录成功后返回 JWT token",  # no ';' at all
    }
    result = {"id": "VP-003", "status": "timeout"}
    assert SplitDecision.should_split(vp, result) is None

    # And an explicitly non-string expected_result must also trip the guard.
    vp_numeric = {
        "id": "VP-003b",
        "verification_method": "automated_test",
        "expected_result": 42,  # type: ignore[typeddict-item]
    }
    assert SplitDecision.should_split(vp_numeric, result) is None


# ---------------------------------------------------------------------------
# Branch 4: < 2 non-empty clauses after split/strip → return None
# ---------------------------------------------------------------------------

def test_branch4_empty_clauses_return_none() -> None:
    """Branch 4: ``len(clauses) < 2`` after strip+drop-empty.

    A trailing ``;`` (e.g. ``"登录成功;"``) or stray ``";;`` yields 0
    real clauses, so there is nothing meaningful to split along.
    """
    vp = {
        "id": "VP-004",
        "verification_method": "ui_validation",
        "expected_result": "登录成功;",  # one real clause + trailing sep
    }
    result = {"id": "VP-004", "status": "timeout"}
    assert SplitDecision.should_split(vp, result) is None

    # Multiple semicolons but no real content must also short-circuit.
    vp2 = {
        "id": "VP-004b",
        "verification_method": "ui_validation",
        "expected_result": ";;;",  # all-empty clauses
    }
    assert SplitDecision.should_split(vp2, result) is None


# ---------------------------------------------------------------------------
# Branch 5: empty / missing parent id → return None
# ---------------------------------------------------------------------------

def test_branch5_missing_parent_id_returns_none() -> None:
    """Branch 5: ``not parent_id`` (empty / missing ``id`` field).

    Sub-VP ids are derived as ``f"{parent_id}-{index}"``; an empty parent
    id would yield ``"-1"`` and create collisions across splits, so the
    splitter refuses rather than risk that.
    """
    vp_no_id = {
        # no "id" key at all
        "verification_method": "automated_test",
        "expected_result": "A 通过; B 通过",
    }
    result = {"id": "VP-005", "status": "timeout"}
    assert SplitDecision.should_split(vp_no_id, result) is None

    # And an explicit empty-string id must trip the same guard.
    vp_empty_id = {
        "id": "",
        "verification_method": "automated_test",
        "expected_result": "A 通过; B 通过",
    }
    assert SplitDecision.should_split(vp_empty_id, result) is None


# ---------------------------------------------------------------------------
# Happy path: all rules hold → list of sub-VPs
# ---------------------------------------------------------------------------

def test_happy_path_produces_sub_vps() -> None:
    """Happy path: timeout + multi-clause + non-empty parent id → chunks.

    The spec example for this happy path: 3 ``;``-separated clauses must
    yield 3 sub-VPs, each carrying ``parent_vp_id``, the original
    ``verification_method``, the per-clause ``expected_result`` (stripped),
    and a per-VP ``timeout_seconds`` (default 3600).
    """
    vp = {
        "id": "VP-016",
        "verification_method": "automated_test",
        "expected_result": "登录成功; 会话已创建; JWT 已签发",
    }
    result = {"id": "VP-016", "status": "timeout"}

    chunks = SplitDecision.should_split(vp, result)

    assert chunks is not None
    assert len(chunks) == 3
    assert [c["id"] for c in chunks] == ["VP-016-1", "VP-016-2", "VP-016-3"]
    for chunk in chunks:
        assert chunk["parent_vp_id"] == "VP-016"
        assert chunk["verification_method"] == "automated_test"
        # 2026-09-13: production default is 3600s
        # (DEFAULT_SUBTASK_TIMEOUT_SECONDS in verification_split.py /
        # verification_split_llm.py), raised from the original 1800.
        assert chunk["timeout_seconds"] == 3600
    assert [c["expected_result"] for c in chunks] == [
        "登录成功",
        "会话已创建",
        "JWT 已签发",
    ]
