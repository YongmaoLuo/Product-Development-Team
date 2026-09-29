"""
TDD tests for the preflight HTTP endpoints (DP2 task — server-side wiring).

Background
----------
``backend/preflight_review.py`` ships a ``PreFlightReviewer`` class that
runs the cross-document consistency check (PRD vs. arch vs. test
design) and persists a ``preflight_report.json`` artifact under
``plans/<plan_id>/``. The pipeline (``TasksGenerator``) calls it
internally before generating tasks, but neither the frontend nor AI
agents have an HTTP surface for it.

This file pins the 5 contracts for the two new endpoints:

  * ``POST /api/preflight/{plan_id}/run``
  * ``GET  /api/preflight/{plan_id}/report``

Contracts:

  1. ``test_post_preflight_run_returns_findings``
     POST on a plan with a PRD + arch + test design + a mocked
     ``CodingTool.query_json`` → 200 with a ``findings`` list,
     ``high_count``, and ``report_path``. The disk artifact
     ``plans/<plan_id>/preflight_report.json`` must also be written
     and be readable.

  2. ``test_get_preflight_report_returns_existing``
     Pre-seed ``plans/<plan_id>/preflight_report.json`` → GET
     returns 200 with the same content (findings / high_count /
     report_path fields preserved verbatim).

  3. ``test_get_preflight_report_404_when_missing``
     Plan exists but ``preflight_report.json`` does NOT → GET
     returns 404.

  4. ``test_post_preflight_run_404_unknown_plan``
     POST against an unknown ``plan_id`` → 404. The endpoint
     must not lazily create the plan dir.

  5. ``test_post_preflight_run_403_when_disabled``
     ``plan_state.flags.preflight_enabled == False`` → POST
     returns 403 with a meaningful message.

The tests are pure unit tests: they use FastAPI's ``TestClient``
against the real ``server.app`` instance. The
``isolated_plans_dir`` autouse fixture in ``tests/conftest.py``
already redirects ``PLANS_DIR`` to a per-test ``tmp_path /
"plans"``, so plan dirs created here stay isolated.

Mocking strategy
----------------
The ``create_coding_tool`` factory in server.py returns a real
``ClaudeCodingTool`` (or whichever provider is configured). To
keep these tests hermetic we monkeypatch ``server.create_coding_tool``
itself with a ``MagicMock`` whose ``query_json`` returns a canned
``{"findings": [...]}`` payload. This is the same pattern used by
``tests/unit/test_preflight_review.py`` for the unit-level tests.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure ``backend/`` is on ``sys.path`` so ``import server`` works
# regardless of which test runner entry point is used. Mirrors the
# pattern in ``test_verification_progress_api.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """A FastAPI TestClient bound to the real ``server.app``.

    The ``isolated_plans_dir`` autouse fixture in
    ``tests/conftest.py`` already redirects ``PLANS_DIR`` to a
    per-test ``tmp_path / "plans"`` so the tests don't leak state
    onto the developer's real plans directory.
    """
    return TestClient(app)


@pytest.fixture
def canned_coding_tool(monkeypatch):
    """Patch ``server.create_coding_tool`` so the endpoint sees a
    predictable ``CodingTool`` whose ``query_json`` returns a
    canned dict. Also stubs ``query`` so any incidental caller
    doesn't trip a MissingMock error.

    Returns the underlying MagicMock so individual tests can
    override ``query_json.return_value`` per scenario.
    """
    canned = {
        "findings": [
            {
                "source_doc": "prd",
                "source_id": "AC-1",
                "target_doc": "arch",
                "target_id": None,
                "severity": "high",
                "finding": "PRD AC-1 has no corresponding arch component.",
                "suggested_fix": "Add an arch module that implements AC-1.",
            },
        ]
    }
    tool = MagicMock()
    tool.query_json.return_value = canned
    tool.query.return_value = json.dumps(canned, ensure_ascii=False)

    import server
    # ``*args, **kwargs``: the scene-routing change (commit 9717073)
    # made callers pass the routing scene, e.g.
    # ``create_coding_tool(scene="tasks_generation")``. The stub is
    # deliberately signature-agnostic so a future routing parameter
    # does not break every endpoint test again.
    monkeypatch.setattr(server, "create_coding_tool", lambda *args, **kwargs: tool)
    return tool


def _write_prd(plan_dir: Path) -> None:
    """Write a minimal PRD with one acceptance item."""
    prd = {
        "title": "Test PRD",
        "overview": "Endpoint test fixture.",
        "acceptance": ["AC-1: must do X"],
        "decision_points": [],
    }
    (plan_dir / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_arch(plan_dir: Path, body: str = "# Architecture\n") -> None:
    (plan_dir / "arch-design.md").write_text(body, encoding="utf-8")


def _write_test_design(plan_dir: Path, body: str = "# Test Design\n") -> None:
    (plan_dir / "test-design.md").write_text(body, encoding="utf-8")


def _write_plan_state(
    plan_dir: Path,
    *,
    preflight_enabled: bool = True,
    arch_enabled: bool = True,
    test_enabled: bool = True,
) -> None:
    """Write a minimal ``plan_state.json`` for the endpoint's flag checks."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": "prd_approved",
        "completed_phases": [],
        "review_rounds": {"prd": 1, "arch": 0, "test": 0},
        "flags": {
            "arch_enabled": arch_enabled,
            "test_enabled": test_enabled,
            "preflight_enabled": preflight_enabled,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_plan(
    plans_root: Path,
    plan_id: str,
    *,
    preflight_enabled: bool = True,
    arch_enabled: bool = True,
    test_enabled: bool = True,
) -> Path:
    """Create a plan directory with all docs needed for a successful
    preflight run."""
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    _write_prd(plan_dir)
    _write_arch(plan_dir)
    _write_test_design(plan_dir)
    _write_plan_state(
        plan_dir,
        preflight_enabled=preflight_enabled,
        arch_enabled=arch_enabled,
        test_enabled=test_enabled,
    )
    return plan_dir


# ---------------------------------------------------------------------------
# Test 1 — POST /api/preflight/{plan_id}/run returns the findings dict
# ---------------------------------------------------------------------------


def test_post_preflight_run_returns_findings(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
    canned_coding_tool,
) -> None:
    """POST against a fully-provisioned plan → 200 with ``findings``,
    ``high_count``, and ``report_path`` in the body. The disk
    artifact ``plans/<plan_id>/preflight_report.json`` must also
    be written and contain the same content as the response.
    """
    plan_id = "20260607-preflight-post"
    _write_plan(isolated_plans_dir, plan_id)

    resp = client.post(f"/api/preflight/{plan_id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Shape pinned: ``findings`` is a list, ``high_count`` is an int
    # (matching the LLM canned response), ``report_path`` references
    # the canonical artifact name.
    assert isinstance(body.get("findings"), list), (
        f"expected 'findings' list, got {type(body.get('findings')).__name__}: {body!r}"
    )
    assert isinstance(body.get("high_count"), int), (
        f"expected int 'high_count', got {type(body.get('high_count')).__name__}: {body!r}"
    )
    assert body["high_count"] >= 1, (
        f"canned response has 1 high-severity finding; expected high_count >= 1, "
        f"got body={body!r}"
    )
    assert "preflight_report.json" in str(body.get("report_path", "")), (
        f"expected report_path to reference 'preflight_report.json', "
        f"got {body.get('report_path')!r}"
    )

    # The artifact must be persisted to disk with content matching
    # the response (so a follow-up GET returns the same findings).
    artifact = isolated_plans_dir / plan_id / "preflight_report.json"
    assert artifact.exists(), f"artifact not written: {artifact}"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["findings"] == body["findings"]
    assert persisted["high_count"] == body["high_count"]


# ---------------------------------------------------------------------------
# Test 2 — GET /api/preflight/{plan_id}/report returns the existing artifact
# ---------------------------------------------------------------------------


def test_get_preflight_report_returns_existing(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """Pre-seed ``plans/<plan_id>/preflight_report.json`` → GET
    returns 200 with the persisted content (findings / high_count /
    report_path preserved verbatim).
    """
    plan_id = "20260607-preflight-get-existing"
    plan_dir = _write_plan(isolated_plans_dir, plan_id)

    report = {
        "findings": [
            {
                "source_doc": "test",
                "source_id": "T-001",
                "target_doc": "task",
                "target_id": None,
                "severity": "medium",
                "finding": "Test scenario T-001 not covered by tasks.json.",
                "suggested_fix": "Add a task for T-001.",
            },
        ],
        "high_count": 0,
        "report_path": str(plan_dir / "preflight_report.json"),
        "passed": True,
    }
    (plan_dir / "preflight_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    resp = client.get(f"/api/preflight/{plan_id}/report")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["findings"] == report["findings"]
    assert body["high_count"] == report["high_count"]
    assert body["report_path"] == report["report_path"]


# ---------------------------------------------------------------------------
# Test 3 — GET returns 404 when the artifact is missing
# ---------------------------------------------------------------------------


def test_get_preflight_report_404_when_missing(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
) -> None:
    """Plan directory exists but no ``preflight_report.json`` → 404."""
    plan_id = "20260607-preflight-get-404"
    _write_plan(isolated_plans_dir, plan_id)
    # Deliberately do NOT write preflight_report.json.

    resp = client.get(f"/api/preflight/{plan_id}/report")

    assert resp.status_code == 404, (
        f"expected 404 for missing report, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Test 4 — POST returns 404 for an unknown plan_id
# ---------------------------------------------------------------------------


def test_post_preflight_run_404_unknown_plan(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
    canned_coding_tool,
) -> None:
    """POST against an unknown ``plan_id`` → 404. The endpoint must
    NOT create the plan directory as a side effect.
    """
    plan_id = "20260607-preflight-no-such-plan"
    plan_dir = isolated_plans_dir / plan_id
    assert not plan_dir.exists(), "test setup must not pre-create the plan dir"

    resp = client.post(f"/api/preflight/{plan_id}/run")

    assert resp.status_code == 404, (
        f"expected 404 for unknown plan, got {resp.status_code}: {resp.text}"
    )
    # Defensive: the endpoint must not silently create the plan dir.
    assert not plan_dir.exists(), (
        "endpoint must not create the plan dir for an unknown plan_id"
    )


# ---------------------------------------------------------------------------
# Test 5 — POST returns 403 when preflight_enabled is False
# ---------------------------------------------------------------------------


def test_post_preflight_run_403_when_disabled(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
    canned_coding_tool,
) -> None:
    """``plan_state.flags.preflight_enabled == False`` → POST
    returns 403 with a meaningful error message. The endpoint
    must NOT call the LLM or write the artifact in this case.
    """
    plan_id = "20260607-preflight-disabled"
    _write_plan(isolated_plans_dir, plan_id, preflight_enabled=False)

    resp = client.post(f"/api/preflight/{plan_id}/run")

    assert resp.status_code == 403, (
        f"expected 403 when preflight_enabled=False, got {resp.status_code}: "
        f"{resp.text}"
    )
    # The artifact must NOT be written when the endpoint refuses
    # to run the reviewer.
    artifact = isolated_plans_dir / plan_id / "preflight_report.json"
    assert not artifact.exists(), (
        "endpoint must not write preflight_report.json when "
        "preflight_enabled=False"
    )
    # The canned coding tool must NOT have been invoked.
    assert not canned_coding_tool.query_json.called, (
        "endpoint must not invoke the LLM when preflight_enabled=False"
    )