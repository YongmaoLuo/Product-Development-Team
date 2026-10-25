"""Workflow-phase HTTP routes: interview → PRD → arch → test → tasks.

Extracted from ``server.py`` on 2026-09-25 (that module had grown to ~13.8k
lines and 71 routes; this band is the largest self-contained slice of it).

Why the routes live in their own module, and why they call back into
``server``
------------------------------------------------------------------------
Everything here still belongs to the same application, so this module
*late-binds* into it: shared helpers, request models and module globals are
reached as ``_server.<name>`` rather than imported by value. That is not
style — it is what keeps the test suite honest. Tests monkeypatch things like
``server.PLANS_DIR``, ``server._state_db_path`` and ``server.PlanState``;
a by-value import would snapshot the old object and the patch would silently
stop applying. Late binding keeps every one of those working.

Two mechanical consequences:

  * ``server`` imports this module at the BOTTOM of the file, once every
    name referenced here exists, and it seeds ``sys.modules["server"]``
    first — so ``python server.py`` (where that file is ``__main__``) does
    not execute a second copy of it;
  * handlers keep their original names and are also re-exported from
    ``server`` where tests still import them (``review_arch_item``,
    ``review_test_item``).
"""

from __future__ import annotations

from fastapi import APIRouter

from typing import Callable, Dict, List, Literal, Optional, Sequence, Tuple, Any, Union
from decision_point_adder import ArchDecisionPointAdder, PRDDecisionPointAdder, TestDecisionPointAdder
from arch_generator import ArchGenerator, ArchHardGateError
from arch_refiner import ArchRefiner
from arch_reviewer import ArchReviewer
from fastapi import FastAPI, HTTPException, Request
from interviewer import Interviewer
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from prd_generator import PRDGenerator
from prd_refiner import PRDRefiner
from prd_review import PRDReviewer
from pathlib import Path
from preflight_review import PreFlightReviewer
from self_review import SelfReviewUnavailableError
from tasks_generator import TasksGenerator, TasksGenerationError
from test_design_generator import TestDesignGenerator
from test_design_refiner import TestDesignRefiner
from test_design_reviewer import TestDesignReviewer
from framework.ids import InvalidPlanIdError, derive_plan_id, validate_plan_id
from plan_artifacts import PLAN_DOC_FILENAME, detect
from plan_state import (
    DEDICATED_PHASE_TRANSITIONS,
    PHASE_TRANSITIONS,
    VERIFICATION_PHASE_TRANSITIONS,
)
from verification_plan_doc import PlanDocError
from verification_plan_doc import generate as generate_plan_doc
from verification_plan_doc import refine as refine_plan_doc
from verification_plan_doc import (
    OBJECT_SECTION,
    BaselineDoc,
    StructureError,
    parse_baseline,
    parse_plan_doc,
    validate_structure,
)
from verification_plan_review import (
    PRD_FILENAME,
    PRD_REVIEW_FILENAME,
    RESOLVED_STATUSES,
    STATUS_PENDING,
    ReviewItem,
    ReviewState,
    doc_sha256,
    extract_items,
    is_resolved,
    record_action,
    review_path,
)
from verification_plan_review import load as load_plan_review_state
from verification_workspace import RefResolutionError, resolve_ref_commit
import json
import re
import sqlite3

router = APIRouter()


import server as _server  # noqa: E402  (late binding — see the module docstring)


class StartInterviewRequest(_server.StartInterviewRequest):
    """``POST /api/interview/start``'s body, plus the plan's hook binding.

    The two extra fields say which session created the plan and where a
    human-in-the-loop question about it should be delivered.  Both are
    optional and mean exactly what they mean on ``POST /api/plan/import``:
    a plan started without them asks nobody, which is the right answer
    for a plan started by a bare ``curl`` rather than a failure.
    """

    hook_destination: Optional[str] = None
    hook_session_id: Optional[str] = None


@router.post("/api/interview/start")
def start_interview(req: StartInterviewRequest):
    """Start a new interview."""
    from datetime import datetime

    _DANGEROUS_REQUIREMENT_PATTERN = re.compile(
        r"(<script|</script>|javascript:|on\w+\s*=|--\s|#\s|;\s*drop\s+table|union\s+select)",
        re.IGNORECASE,
    )

    requirement = req.requirement
    if not isinstance(requirement, str) or not requirement.strip():
        raise HTTPException(
            400,
            {
                "error": "Invalid requirement",
                "detail": "requirement must be a non-empty string",
            },
        )
    if len(requirement) > 10000:
        raise HTTPException(
            400,
            {
                "error": "Invalid requirement",
                "detail": "requirement must be at most 10000 characters",
            },
        )
    if "\x00" in requirement:
        raise HTTPException(
            400,
            {
                "error": "Invalid requirement",
                "detail": "requirement must not contain null bytes",
            },
        )
    if _DANGEROUS_REQUIREMENT_PATTERN.search(requirement):
        raise HTTPException(
            400,
            {
                "error": "Invalid requirement",
                "detail": "requirement contains potentially dangerous content",
            },
        )

    # 2026-09-14: the derived id must satisfy ``framework.ids.
    # validate_plan_id`` ([A-Za-z0-9._-]), because every code path that
    # touches ``plans/{plan_id}`` — including the dispatcher's watchdog
    # signal — funnels through it. The old inline form only replaced
    # SPACES, so a CJK requirement
    # produced a directory name the project's own validator rejects:
    # the plan then ran with no watchdog signal ever written and no
    # alert raised.
    plan_id = req.plan_id or derive_plan_id(requirement, max_len=20)
    plan_dir = _server._plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The plan's hook binding: the session it was created in, and where a
    # human-in-the-loop question about it goes.  The helper is the fast
    # lane's, so both plan-creation doors record the origin the same way
    # — one rule, one implementation.  It is imported here rather than at
    # the top of the module because ``routes.fast_lane`` late-binds into
    # ``server``, which imports this module: a module-level import would
    # be a cycle.
    from routes.fast_lane import bind_hook_for_plan

    # Read with ``getattr`` so a caller that hands this handler the older
    # request model (which has no hook fields) still creates a plan — one
    # that asks nobody.
    bind_hook_for_plan(
        plan_dir,
        getattr(req, "hook_destination", None),
        getattr(req, "hook_session_id", None),
    )

    # Task #3.9: seed a ``plan_routing`` row with ``stage='interview'``
    # at plan creation so the interview phase is tracked in SQLite
    # rather than derived from ``interview.json`` file-existence via
    # ``PlanState._infer_phase``. Without this row, ``_load_state``
    # falls through to ``_default_state`` → ``_infer_phase`` and
    # returns ``interview_complete`` the moment ``interview.json`` is
    # written (which happens inside ``interviewer.start`` below),
    # making a fresh interview indistinguishable from a completed one
    # to anyone reading the routing table. ``INSERT OR IGNORE`` keeps
    # the call idempotent — a restart that re-runs ``start`` does
    # not overwrite an existing ``plan_routing`` row, so a plan that
    # has already advanced past ``interview`` (e.g. crashed mid-PRD)
    # is not silently regressed.
    _server._seed_plan_routing_phase(plan_id, phase="interview")

    coding_tool = _server.create_coding_tool(scene="interview")
    interviewer = Interviewer(coding_tool, plan_dir)
    try:
        questions = interviewer.start(req.requirement)
    except TimeoutError as exc:
        _server.logger.warning("Interview start timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Interview start timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    return {
        "plan_id": plan_id,
        "questions": questions,
        "complete": interviewer.is_complete(),
        "dimensions": interviewer.get_dimensions(),
    }


@router.post("/api/interview/{plan_id}/continue")
def continue_interview(plan_id: str, req: _server.ContinueInterviewRequest):
    """Continue an interview."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    coding_tool = _server.create_coding_tool(scene="interview")
    interviewer = Interviewer(coding_tool, plan_dir)
    try:
        questions = interviewer.continue_interview(req.reply)
    except TimeoutError as exc:
        _server.logger.warning("Interview continue timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Interview continue timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    return {
        "plan_id": plan_id,
        "questions": questions,
        "complete": interviewer.is_complete(),
        "dimensions": interviewer.get_dimensions(),
    }


@router.get("/api/interview/{plan_id}")
def get_interview(plan_id: str):
    """Get interview state."""
    plan_dir = _server._plan_dir(plan_id)
    interview_file = plan_dir / "interview.json"
    if not interview_file.exists():
        raise HTTPException(404, "Interview not found")

    with open(interview_file) as f:
        data = json.load(f)
    return data


@router.post("/api/interview/{plan_id}/answer")
def answer_interview(plan_id: str, req: _server.AnswerInterviewRequest):
    """Submit a direct answer for a specific interview dimension.

    This endpoint bypasses the LLM-driven dialogue and stores the
    user's answer directly into interview.json.  When all five
    required dimensions (background, goals, scope, constraints,
    acceptance) have non-empty answers, the interview is marked
    complete and the plan state is advanced to ``interview_complete``.
    """
    import json  # local; safe to shadow the outer stdlib `json`.

    from datetime import datetime

    VALID_DIMENSIONS = {"background", "goals", "scope", "constraints", "acceptance"}

    plan_dir = _server._plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)

    if req.dimension not in VALID_DIMENSIONS:
        raise HTTPException(
            400,
            {
                "error": "Invalid dimension",
                "detail": f"dimension must be one of {sorted(VALID_DIMENSIONS)}",
            },
        )

    answer = req.answer if isinstance(req.answer, str) else ""
    if not answer.strip():
        raise HTTPException(
            400,
            {
                "error": "Invalid answer",
                "detail": "answer must be a non-empty string",
            },
        )

    interview_file = plan_dir / "interview.json"
    if interview_file.exists():
        with open(interview_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "plan_id": plan_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "in_progress",
            "dimensions": {},
            "chat_history": [],
        }

    # Check completeness. The interview JSON file is written by
    # both /interview/start (LLM-driven) and /interview/answer
    # (direct). The LLM path produces dict-shaped values (e.g.
    # ``{"value": "..."}``); the direct path produces strings. The
    # legacy ``.strip()`` call assumed string values and crashed
    # (``'dict' object has no attribute 'strip'``) when the LLM
    # version of the file was loaded. Normalise the value to a
    # string before checking emptiness / saving.
    def _coerce_dimension_value(v) -> str:
        """Coerce a stored dimension value to a flat string.

        Accepts the LLM-interviewer shape (``{"value": "..."}``,
        ``{"reasoning": "..."}``), lists, plain strings, and ``None``.
        Returns the joined text suitable for completeness checks.
        """
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            # Common shapes: {"value": "..."} or {"reasoning": "..."}.
            # Take value first, fall back to reasoning, then any
            # string-typed field.
            for key in ("value", "reasoning", "text", "answer"):
                inner = v.get(key)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
            # Last resort: serialise the dict.
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, (list, tuple)):
            parts = [_coerce_dimension_value(item) for item in v]
            return " ".join(p for p in parts if p)
        return str(v).strip()

    def _dimension_value(d, key):
        """Robust lookup + coercion for a single dimension."""
        return _coerce_dimension_value(d.get(key, ""))

    def _is_complete(dimensions: dict) -> tuple[bool, list[str]]:
        covered = []

        for k in VALID_DIMENSIONS:
            v = _dimension_value(dimensions, k)
            if v:
                covered.append(k)
        return len(covered) == len(VALID_DIMENSIONS), covered

    # Store the answer.
    data["dimensions"] = data.get("dimensions", {})
    data["dimensions"][req.dimension] = answer.strip()

    # Check completeness.
    is_complete, covered = _is_complete(data["dimensions"])
    covered_list = covered  # alias for the return payload

    if is_complete:
        data["status"] = "complete"

    with open(interview_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Update plan state when interview becomes complete.
    state = _server.PlanState(plan_dir)
    if is_complete:
        if state._state.get("current_phase") == "interview":
            state.transition_to("interview_complete")

    return {
        "plan_id": plan_id,
        "current_phase": state._state.get("current_phase"),
        "dimensions_covered": covered_list,
        "complete": is_complete,
    }


@router.post("/api/prd/{plan_id}/generate")
def generate_prd(plan_id: str, req: _server.GeneratePRDRequest = None):
    """Generate PRD from interview.

    Optionally accepts project_dir to scan existing codebase for context.
    Falls back to execution.json project_dir if not provided.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # Resolve project_dir: request body > execution.json > None
    project_dir = None
    if req and req.project_dir:
        project_dir = Path(req.project_dir).expanduser().resolve()
    if project_dir is None:
        project_dir = _server._get_project_dir(plan_id)

    # Set cwd so the Claude agent can use Grep/Glob/Read tools on the project
    cwd = str(project_dir) if project_dir else None
    coding_tool = _server.create_coding_tool(cwd=cwd, scene="prd")
    state = _server.PlanState(plan_dir)
    gen = PRDGenerator(
        coding_tool, plan_dir, project_dir=project_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        content = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("PRD generation timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "PRD generation timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    # Update plan state
    state = _server.PlanState(plan_dir)
    state.transition_to("prd_generation")

    return {"plan_id": plan_id, "prd": content}


@router.post("/api/prd/{plan_id}/regenerate")
def regenerate_prd(plan_id: str, req: _server.GeneratePRDRequest = None):
    """Regenerate PRD — clears existing PRD and review data, re-generates from interview."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # Clear existing PRD and review files
    for f in ["prd.json", "prd.md", "review.json"]:
        p = plan_dir / f
        if p.exists():
            p.unlink()

    # Reset state backward
    state = _server.PlanState(plan_dir)
    state.reset_to("prd_generation")
    state.reset_review_round("prd")

    # Resolve project_dir: request body > execution.json > None
    project_dir = None
    if req and req.project_dir:
        project_dir = Path(req.project_dir).expanduser().resolve()
    if project_dir is None:
        project_dir = _server._get_project_dir(plan_id)

    # Re-generate — set cwd so Claude agent can use tools on the project
    cwd = str(project_dir) if project_dir else None
    coding_tool = _server.create_coding_tool(cwd=cwd, scene="prd")
    state = _server.PlanState(plan_dir)
    gen = PRDGenerator(
        coding_tool, plan_dir, project_dir=project_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        content = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("PRD regeneration timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "PRD regeneration timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    return {"plan_id": plan_id, "prd": content}


@router.get("/api/prd/{plan_id}")
def get_prd(plan_id: str):
    """Get PRD content."""
    plan_dir = _server._plan_dir(plan_id)
    prd_json = plan_dir / "prd.json"
    if prd_json.exists():
        with open(prd_json, "r", encoding="utf-8") as f:
            prd_data = json.load(f)
        # Render to markdown for display
        content = PRDGenerator.prd_to_markdown(prd_data)
        return {"plan_id": plan_id, "prd": content}

    # Fallback to legacy markdown
    prd_file = plan_dir / "prd.md"
    if prd_file.exists():
        with open(prd_file) as f:
            content = f.read()
        return {"plan_id": plan_id, "prd": content}

    raise HTTPException(404, "PRD not found")


@router.get("/api/review/{plan_id}/review/items")
def get_review_items(plan_id: str):
    """Extract and return review items from PRD."""
    plan_dir = _server._plan_dir(plan_id)
    if not (plan_dir / "prd.json").exists() and not (plan_dir / "prd.md").exists():
        raise HTTPException(404, "PRD not found")

    coding_tool = _server.create_coding_tool(scene="prd")
    reviewer = PRDReviewer(coding_tool, plan_dir)
    items = reviewer.extract_decision_points()

    # Transition to prd_review phase so sidebar navigation can resume correctly
    try:
        state = _server.PlanState(plan_dir)
        if state.get_state().get("current_phase", "") != "prd_approved":
            state.transition_to("prd_review")
    except Exception:
        pass

    # Check for existing review
    review_file = plan_dir / "review.json"
    existing = {}
    if review_file.exists():
        with open(review_file) as f:
            existing = json.load(f)
        item_map = {i["index"]: i for i in existing.get("items", [])}
        for item in items:
            if item.index in item_map:
                item.status = item_map[item.index]["status"]
                item.note = item_map[item.index].get("note", "")

    return {
        "plan_id": plan_id,
        "items": [
            {
                "index": i.index,
                "title": i.title,
                "content": i.content,
                "status": i.status,
                "note": i.note,
            }
            for i in items
        ],
        "total": len(items),
    }


@router.post("/api/review/{plan_id}/review/item/{item_index}")
def review_item(plan_id: str, item_index: int, req: _server.ReviewActionRequest):
    """Submit review action for a single item."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # Load existing review or create new
    review_file = plan_dir / "review.json"
    if review_file.exists():
        with open(review_file) as f:
            review_data = json.load(f)
    else:
        review_data = {"items": [], "total": 0}

    # Update or add item
    items_map = {i["index"]: i for i in review_data["items"]}
    if item_index in items_map:
        item = items_map[item_index]
    else:
        item = {"index": item_index, "title": "", "status": "pending", "note": ""}

    if req.action == "accept":
        item["status"] = "accepted"
    elif req.action == "skip":
        item["status"] = "skipped"
    elif req.action == "revise":
        coding_tool = _server.create_coding_tool(scene="prd_refine")
        reviewer = PRDReviewer(coding_tool, plan_dir)
        result = reviewer.submit_action(item_index, "revise", question=req.question)
        return result
    elif req.action == "reset":
        item["status"] = "pending"
        item["note"] = ""

    items_map[item_index] = item
    review_data["items"] = list(items_map.values())
    review_data["total"] = len(review_data["items"])
    review_data["accepted"] = sum(1 for i in review_data["items"] if i.get("status") == "accepted")
    review_data["skipped"] = sum(1 for i in review_data["items"] if i.get("status") == "skipped")

    with open(review_file, "w") as f:
        json.dump(review_data, f, indent=2, ensure_ascii=False)

    # Update plan state based on review completion — full validation synced
    # with plan_state._is_prd_review_complete (plan_state.py:173-174): only
    # transition to prd_approved when *every* decision point in prd.json
    # has a corresponding review item with status="accepted".
    try:
        state = _server.PlanState(plan_dir)
        prd_file = plan_dir / "prd.json"
        if prd_file.exists():
            with open(prd_file, "r", encoding="utf-8") as f:
                prd_data = json.load(f)
        else:
            prd_data = {}
        decision_points = prd_data.get("decision_points", []) or []
        required_indices = {dp.get("index") for dp in decision_points}
        accepted_indices = {
            i.get("index")
            for i in review_data.get("items", [])
            if i.get("status") == "accepted"
        }
        skipped_count = sum(
            1 for i in review_data.get("items", [])
            if i.get("status") == "skipped"
        )

        # Backward compat: empty PRD is trivially approved.
        if not required_indices:
            state.transition_to("prd_approved")
            return {
                "status": item["status"],
                "note": item.get("note", ""),
                "current_phase": state.get_current_phase(),
            }

        if required_indices.issubset(accepted_indices):
            state.transition_to("prd_approved")
            return {
                "status": item["status"],
                "note": item.get("note", ""),
                "current_phase": state.get_current_phase(),
            }

        # Partial accept: stay at prd_review, surface a warning.
        return {
            "status": "warning",
            "current_phase": state.get_current_phase(),
            "accepted": len(accepted_indices & required_indices),
            "skipped": skipped_count,
            "total": len(required_indices),
            "pending": len(required_indices) - len(accepted_indices & required_indices) - skipped_count,
        }
    except Exception:
        pass

    return {"status": item["status"], "note": item.get("note", "")}


@router.post("/api/prd/{plan_id}/refine")
def refine_prd(plan_id: str):
    """Refine PRD based on rejected review items."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    coding_tool = _server.create_coding_tool(scene="prd_refine")
    refiner = PRDRefiner(coding_tool, plan_dir)
    try:
        refined_prd = refiner.refine()
    except TimeoutError as exc:
        _server.logger.warning("PRD refinement timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "PRD refinement timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    # Reset review state for re-review
    reviewer = PRDReviewer(coding_tool, plan_dir)
    reviewer.reset_for_refinement()

    # Update plan state
    state = _server.PlanState(plan_dir)
    state.increment_review_round("prd")
    state.transition_to("prd_review")

    return {"plan_id": plan_id, "prd": refined_prd}


# --- Placeholder-PRD downstream gating (VP-006) ---------------------------
#
# When the interview is empty / missing required dimensions, the PRD
# generator emits a *placeholder* PRD (``prd.json`` with
# ``_placeholder=True``) instead of a real one (see
# ``prd_generator.PRDGenerator.build_placeholder_prd``). A placeholder PRD
# is NOT a real specification: it only tells the user which interview
# dimensions are missing. Downstream phases (architecture, test design,
# task generation, execution) MUST refuse to consume it — otherwise they
# would design/build against a non-existent spec and let the workflow
# silently advance on garbage.
#
# This guard is called at the top of every downstream generate / start
# endpoint. It raises HTTP 409 Conflict (``status="blocked"``) BEFORE any
# generation runs and BEFORE any ``PlanState.transition_to`` call, so
# ``plan_state.json``'s ``current_phase`` can never jump to
# ``arch_generation`` / ``test_generation`` / ``task_generation`` /
# ``executing`` while the PRD is still a placeholder.


def _is_placeholder_prd(plan_dir: Path, plan_id: str = "") -> bool:
    """Return True when the plan's PRD is a placeholder.

    VP-005: the body is loaded through ArtifactRepository.query_prd
    rather than reading the PRD file directly so all artifact
    reads funnel through the repository layer. Falls back to a
    pre-migration legacy open() only when no state-machine DB
    is reachable AND plan_id was not supplied (kept for unit
    tests that exercise the function in isolation).
    """
    prd_file = plan_dir / "prd.json"
    if not prd_file.exists():
        return False
    prd: Any = None
    if plan_id:
        sm_blob = _server._open_state_machine()
        try:
            if sm_blob is not None:
                _, _, _, _, artifact_repo = sm_blob
                prd = artifact_repo.query_prd(plan_id)
        except Exception:
            prd = None
        finally:
            _server._close_state_machine(sm_blob)
    if prd is None and not plan_id:
        with open(prd_file, "r", encoding="utf-8") as f:
            prd = json.load(f)
    return isinstance(prd, dict) and prd.get("_placeholder") is True


def _reject_if_placeholder_prd(plan_dir: Path, plan_id: str) -> None:
    """Raise HTTP 409 when the plan's PRD is still a placeholder.

    VP-006: downstream phases must be blocked while the PRD is a
    placeholder. The 409 body carries ``status="blocked"`` plus the list
    of missing interview dimensions so callers can surface an actionable
    prompt (complete the interview, then re-generate the PRD).
    """
    if not _is_placeholder_prd(plan_dir, plan_id=plan_id):
        return
    missing: list[Any] = []
    prd: Any = None
    sm_blob = _server._open_state_machine()
    try:
        if sm_blob is not None:
            _, _, _, _, artifact_repo = sm_blob
            prd = artifact_repo.query_prd(plan_id)
    except Exception:
        prd = None
    finally:
        _server._close_state_machine(sm_blob)
    if isinstance(prd, dict):
        missing = list(prd.get("_missing_dimensions", []) or [])
    else:
        missing = []
    raise HTTPException(
        409,
        {
            "error": "Placeholder PRD — downstream phases blocked",
            "status": "blocked",
            "blocked": True,
            "plan_id": plan_id,
            "missing_dimensions": missing,
            "detail": (
                "PRD 仍为占位 PRD（_placeholder=True）。请先补全 interview "
                "缺失维度并重新生成真实 PRD，然后才能进入架构 / 测试 / 任务 / 执行阶段。"
            ),
        },
    )


# --- Architecture Design Endpoints ---

@router.get("/api/arch/{plan_id}")
def get_arch(plan_id: str):
    """Get architecture design."""
    plan_dir = _server._plan_dir(plan_id)
    arch_file = plan_dir / "arch-design.md"
    if not arch_file.exists():
        raise HTTPException(404, "Architecture design not found")

    with open(arch_file) as f:
        content = f.read()
    return {"plan_id": plan_id, "arch": content}


@router.post("/api/arch/{plan_id}/generate")
def generate_arch(plan_id: str):
    """Generate architecture design from approved PRD."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # VP-006: block downstream phases while the PRD is a placeholder.
    _reject_if_placeholder_prd(plan_dir, plan_id)

    coding_tool = _server.create_coding_tool(scene="arch")
    state = _server.PlanState(plan_dir)
    gen = ArchGenerator(
        coding_tool, plan_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        content = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("Architecture generation timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Architecture generation timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )
    except ArchHardGateError as exc:
        # Design Principles HARD-GATE failed after retries. The file
        # was NOT written. Caller can show a blocking UI prompt that
        # lists the failed principles and asks the user to revise the
        # PRD / supply a better example, or to relax the gate.
        _server.logger.warning(
            "Architecture HARD-GATE rejected for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            409,
            {
                "error": "Architecture HARD-GATE rejected",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )
    except SelfReviewUnavailableError as exc:
        _server.logger.warning(
            "Architecture self-review unavailable for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            503,
            {
                "error": "Architecture self-review unavailable",
                "detail": str(exc),
                "plan_id": plan_id,
                "status": "self_review_unavailable",
            },
        )

    # Update plan state
    state = _server.PlanState(plan_dir)
    state.transition_to("arch_generation")
    state.enable_arch(True)

    return {"plan_id": plan_id, "arch": content}


@router.post("/api/arch/{plan_id}/regenerate")
def regenerate_arch(plan_id: str):
    """Regenerate architecture design — clears existing arch and review data."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # Clear existing arch and review files
    for f in ["arch-design.md", "arch-review.json"]:
        p = plan_dir / f
        if p.exists():
            p.unlink()

    # Reset state backward
    state = _server.PlanState(plan_dir)
    state.reset_to("arch_generation")
    state.reset_review_round("arch")

    # Re-generate
    coding_tool = _server.create_coding_tool(scene="arch")
    state = _server.PlanState(plan_dir)
    gen = ArchGenerator(
        coding_tool, plan_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        content = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("Architecture regeneration timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Architecture regeneration timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )
    except SelfReviewUnavailableError as exc:
        _server.logger.warning(
            "Architecture self-review unavailable for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            503,
            {
                "error": "Architecture self-review unavailable",
                "detail": str(exc),
                "plan_id": plan_id,
                "status": "self_review_unavailable",
            },
        )

    state.transition_to("arch_generation")
    state.enable_arch(True)

    return {"plan_id": plan_id, "arch": content}


@router.post("/api/arch/{plan_id}/refine")
def refine_arch(plan_id: str, req: _server.RefineRequest):
    """Refine architecture based on rejected review items or direct feedback."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    coding_tool = _server.create_coding_tool(scene="arch_refine")
    refiner = ArchRefiner(coding_tool, plan_dir)
    try:
        refined_arch = refiner.refine(feedback=req.feedback)
    except TimeoutError as exc:
        _server.logger.warning("Architecture refinement timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Architecture refinement timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    # Reset review state
    reviewer = ArchReviewer(coding_tool, plan_dir)
    reviewer.reset_for_refinement()

    # Update plan state
    state = _server.PlanState(plan_dir)
    state.increment_review_round("arch")
    state.transition_to("arch_review")

    return {"plan_id": plan_id, "arch": refined_arch}


@router.get("/api/arch/{plan_id}/review/items")
def get_arch_review_items(plan_id: str):
    """Get architecture review items."""
    plan_dir = _server._plan_dir(plan_id)
    arch_file = plan_dir / "arch-design.md"
    if not arch_file.exists():
        raise HTTPException(404, "Architecture design not found")

    with open(arch_file) as f:
        content = f.read()

    coding_tool = _server.create_coding_tool(scene="arch")
    reviewer = ArchReviewer(coding_tool, plan_dir)
    items = reviewer.get_items(content)

    # Transition to arch_review phase so sidebar navigation can resume correctly
    try:
        state = _server.PlanState(plan_dir)
        if state.get_state().get("current_phase", "") != "arch_approved":
            state.transition_to("arch_review")
    except Exception:
        pass

    return {
        "plan_id": plan_id,
        "items": [
            {
                "index": i.index,
                "title": i.title,
                "content": i.content,
                "status": i.status,
                "note": i.note,
            }
            for i in items
        ],
        "total": len(items),
    }


@router.post("/api/arch/{plan_id}/review/item/{item_index}")
def review_arch_item(plan_id: str, item_index: int, req: _server.ReviewActionRequest):
    """Submit architecture review action."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    coding_tool = _server.create_coding_tool(scene="arch_refine")
    reviewer = ArchReviewer(coding_tool, plan_dir)
    result = reviewer.submit_action(item_index, req.action, req.note, req.question)

    # Update plan state if all reviewed
    summary = reviewer.get_review_summary()
    # Identity stability contract: ``arch_approved`` requires the user
    # to have explicitly *accepted* every decision point. A plan
    # that mixes accepted + skipped (or is fully skipped) must NOT
    # silently advance — otherwise self-review-induced DP renumbering
    # could let one accepted status bind to a different DP than the
    # one the user actually read.
    if summary["total"] > 0 and summary["accepted"] == summary["total"]:
        state = _server.PlanState(plan_dir)
        state.transition_to("arch_approved")

    return result


# --- Test Design Endpoints ---

@router.get("/api/test/{plan_id}")
def get_test_design(plan_id: str):
    """Get test design document."""
    plan_dir = _server._plan_dir(plan_id)
    test_file = plan_dir / "test-design.md"
    if not test_file.exists():
        raise HTTPException(404, "Test design not found")

    with open(test_file) as f:
        content = f.read()
    return {"plan_id": plan_id, "test_design": content}


@router.post("/api/test/{plan_id}/generate")
def generate_test_design(plan_id: str):
    """Generate test design from approved architecture."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # VP-006: block downstream phases while the PRD is a placeholder.
    _reject_if_placeholder_prd(plan_dir, plan_id)

    coding_tool = _server.create_coding_tool(scene="test_design")
    state = _server.PlanState(plan_dir)
    gen = TestDesignGenerator(
        coding_tool, plan_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        content = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("Test design generation timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Test design generation timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )
    except SelfReviewUnavailableError as exc:
        _server.logger.warning(
            "Test design self-review unavailable for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            503,
            {
                "error": "Test design self-review unavailable",
                "detail": str(exc),
                "plan_id": plan_id,
                "status": "self_review_unavailable",
            },
        )

    # Update plan state
    state = _server.PlanState(plan_dir)
    state.transition_to("test_generation")
    state.enable_test(True)

    return {"plan_id": plan_id, "test_design": content}


@router.post("/api/test/{plan_id}/regenerate")
def regenerate_test_design(plan_id: str):
    """Regenerate test design — clears existing test design and review data."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # Clear existing test and review files
    for f in ["test-design.md", "test-review.json"]:
        p = plan_dir / f
        if p.exists():
            p.unlink()

    # Reset state backward
    state = _server.PlanState(plan_dir)
    state.reset_to("test_generation")
    state.reset_review_round("test")

    # Re-generate
    coding_tool = _server.create_coding_tool(scene="test_design")
    state = _server.PlanState(plan_dir)
    gen = TestDesignGenerator(
        coding_tool, plan_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        content = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("Test design regeneration timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Test design regeneration timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )
    except SelfReviewUnavailableError as exc:
        _server.logger.warning(
            "Test design self-review unavailable for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            503,
            {
                "error": "Test design self-review unavailable",
                "detail": str(exc),
                "plan_id": plan_id,
                "status": "self_review_unavailable",
            },
        )

    state.transition_to("test_generation")
    state.enable_test(True)

    return {"plan_id": plan_id, "test_design": content}


@router.post("/api/test/{plan_id}/refine")
def refine_test_design(plan_id: str, req: _server.RefineRequest):
    """Refine test design based on rejected review items or direct feedback."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    coding_tool = _server.create_coding_tool(scene="test_refine")
    refiner = TestDesignRefiner(coding_tool, plan_dir)
    try:
        refined_test = refiner.refine(feedback=req.feedback)
    except TimeoutError as exc:
        _server.logger.warning("Test design refinement timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Test design refinement timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    # Reset review state
    reviewer = TestDesignReviewer(coding_tool, plan_dir)
    reviewer.reset_for_refinement()

    # Update plan state
    state = _server.PlanState(plan_dir)
    state.increment_review_round("test")
    state.transition_to("test_review")

    return {"plan_id": plan_id, "test_design": refined_test}


@router.get("/api/test/{plan_id}/review/items")
def get_test_review_items(plan_id: str):
    """Get test design review items."""
    plan_dir = _server._plan_dir(plan_id)
    test_file = plan_dir / "test-design.md"
    if not test_file.exists():
        raise HTTPException(404, "Test design not found")

    with open(test_file) as f:
        content = f.read()

    coding_tool = _server.create_coding_tool(scene="test_design")
    reviewer = TestDesignReviewer(coding_tool, plan_dir)
    items = reviewer.get_items(content)

    # Transition to test_review phase so sidebar navigation can resume correctly
    try:
        state = _server.PlanState(plan_dir)
        if state.get_state().get("current_phase", "") != "test_approved":
            state.transition_to("test_review")
    except Exception:
        pass

    return {
        "plan_id": plan_id,
        "items": [
            {
                "index": i.index,
                "title": i.title,
                "content": i.content,
                "status": i.status,
                "note": i.note,
            }
            for i in items
        ],
        "total": len(items),
    }


@router.post("/api/test/{plan_id}/review/item/{item_index}")
def review_test_item(plan_id: str, item_index: int, req: _server.ReviewActionRequest):
    """Submit test design review action."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    coding_tool = _server.create_coding_tool(scene="test_refine")
    reviewer = TestDesignReviewer(coding_tool, plan_dir)
    result = reviewer.submit_action(item_index, req.action, req.note, req.question)

    # Update plan state if all reviewed
    summary = reviewer.get_review_summary()
    # Same identity-stability contract as arch: only advance when
    # every decision point was explicitly accepted by the user.
    if summary["total"] > 0 and summary["accepted"] == summary["total"]:
        state = _server.PlanState(plan_dir)
        state.transition_to("test_approved")

    return result


# --- Decision Point Add Endpoints (lightweight append) -------------------
#
# Companion to the per-phase ``*_refine`` endpoints which do full
# rewrites. The add endpoints are surgical: they let the user say
# "this doc is missing DP X" and we append a single CPEA block
# without disturbing anything the user has already accepted.
#
# Workflow review gate: every newly added DP is registered as
# ``pending`` in the review JSON, and if the plan is already past
# ``<x>_approved`` we transition back to ``<x>_review`` so the user
# is forced to look at the new content before it can flow into task
# generation.


@router.post("/api/prd/{plan_id}/decision_point/add")
def add_prd_decision_point(plan_id: str, req: _server.AddDecisionPointRequest):
    """Append a new CPEA decision point to prd.json."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")
    prd_json = plan_dir / "prd.json"
    if not prd_json.exists():
        raise HTTPException(404, "PRD not found")

    coding_tool = _server.create_coding_tool(scene="prd")
    adder = PRDDecisionPointAdder(coding_tool, plan_dir)
    reviewer = PRDReviewer(coding_tool, plan_dir)
    try:
        body = adder.add(req.requirement, req.count)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except TimeoutError as exc:
        raise HTTPException(
            504,
            {
                "error": "Decision-point add timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    if body["added"]:
        reviewer.register_new_items(
            [
                {"index": dp.index, "title": dp.title}
                for dp in body["added"]
            ]
        )
        state = _server.PlanState(plan_dir)
        cur_phase = state.get_state().get("current_phase", "")
        if cur_phase == "prd_approved":
            # Go via prd_refining so the existing prd_refining -> prd_review
            # edge is honoured; the refining window is harmless because no
            # refiner is actually invoked — only the phase label changes.
            state.transition_to("prd_refining")
            state.transition_to("prd_review")

    return {
        "plan_id": plan_id,
        "added": [dp.to_dict() for dp in body["added"]],
        "total": len(body["added"]),
        "no_gap_reason": body["no_gap_reason"],
        "warnings": body["warnings"],
    }


@router.post("/api/arch/{plan_id}/decision_point/add")
def add_arch_decision_point(plan_id: str, req: _server.AddDecisionPointRequest):
    """Append a new CPEA decision point to arch-design.md."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")
    arch_file = plan_dir / "arch-design.md"
    if not arch_file.exists():
        raise HTTPException(404, "Architecture design not found")

    coding_tool = _server.create_coding_tool(scene="arch")
    adder = ArchDecisionPointAdder(coding_tool, plan_dir)
    reviewer = ArchReviewer(coding_tool, plan_dir)
    try:
        body = adder.add(req.requirement, req.count)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except TimeoutError as exc:
        raise HTTPException(
            504,
            {
                "error": "Decision-point add timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    if body["added"]:
        reviewer.register_new_items(
            [
                {"index": dp.index, "title": dp.title}
                for dp in body["added"]
            ]
        )
        state = _server.PlanState(plan_dir)
        cur_phase = state.get_state().get("current_phase", "")
        if cur_phase == "arch_approved":
            state.transition_to("arch_refining")
            state.transition_to("arch_review")

    return {
        "plan_id": plan_id,
        "added": [dp.to_dict() for dp in body["added"]],
        "total": len(body["added"]),
        "no_gap_reason": body["no_gap_reason"],
        "warnings": body["warnings"],
    }


@router.post("/api/test/{plan_id}/decision_point/add")
def add_test_decision_point(plan_id: str, req: _server.AddDecisionPointRequest):
    """Append a new CPEA decision point to test-design.md."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")
    test_file = plan_dir / "test-design.md"
    if not test_file.exists():
        raise HTTPException(404, "Test design not found")

    coding_tool = _server.create_coding_tool(scene="test_design")
    adder = TestDecisionPointAdder(coding_tool, plan_dir)
    reviewer = TestDesignReviewer(coding_tool, plan_dir)
    try:
        body = adder.add(req.requirement, req.count)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except TimeoutError as exc:
        raise HTTPException(
            504,
            {
                "error": "Decision-point add timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    if body["added"]:
        reviewer.register_new_items(
            [
                {"index": dp.index, "title": dp.title}
                for dp in body["added"]
            ]
        )
        state = _server.PlanState(plan_dir)
        cur_phase = state.get_state().get("current_phase", "")
        if cur_phase == "test_approved":
            state.transition_to("test_refining")
            state.transition_to("test_review")

    return {
        "plan_id": plan_id,
        "added": [dp.to_dict() for dp in body["added"]],
        "total": len(body["added"]),
        "no_gap_reason": body["no_gap_reason"],
        "warnings": body["warnings"],
    }


# --- Plan State Endpoints ---

@router.get("/api/plan/{plan_id}/state")
def get_plan_state(plan_id: str):
    """Get plan workflow state."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    state = _server.PlanState(plan_dir)
    return state.get_state()


def read_plan_routing_row(plan_id: str) -> Optional[dict[str, Any]]:
    """Read a single ``plan_routing`` row directly from state.db.

    This helper is the read-only seam used by
    :func:`state_from_db` — it intentionally bypasses every layer
    that the canonical ``/state`` route uses (PlanState, JSON
    fallback, migration logic) so integration tests can pin the
    raw SQLite contract without that machinery in the way.

    Returns a plain dict with exactly the three fields the brief
    pins::

        {
            "plan_id": str,
            "current_phase": Optional[str],
            "completed_phases": list[str],
        }

    ``current_phase`` is returned as ``None`` when the column is
    SQL NULL (not replaced with a default stage string).
    ``completed_phases`` is returned as an empty list when the
    column is NULL, an empty string, or the JSON payload fails
    to decode (the decode-failure branch logs a warning but does
    NOT raise — the endpoint's contract is "best-effort decode
    so the caller gets *something* readable").

    The caller is responsible for the file-existence check; this
    helper raises :class:`sqlite3.Error` (which the FastAPI
    handler translates to HTTP 500 / ``SQLITE_ERROR``) when the
    DB is missing or corrupt.
    """
    db_path = _server._state_db_path()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT plan_id, current_phase, completed_phases "
            "FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    raw_completed = row[2]
    completed: list[Any]
    if raw_completed is None or raw_completed == "":
        completed = []
    else:
        try:
            decoded = json.loads(raw_completed)
        except (TypeError, ValueError):
            _server.logger.warning(
                "state_from_db: plan_routing.completed_phases JSON "
                "decode failed for plan_id=%r (raw=%r); returning []",
                plan_id,
                raw_completed,
            )
            completed = []
        else:
            # The brief pins the decoded shape as a list.  Anything
            # else (a dict, a scalar) is logged and coerced to []
            # so the wire contract stays predictable.
            if isinstance(decoded, list):
                completed = decoded
            else:
                _server.logger.warning(
                    "state_from_db: plan_routing.completed_phases "
                    "decoded to non-list %s for plan_id=%r; "
                    "returning []",
                    type(decoded).__name__,
                    plan_id,
                )
                completed = []

    return {
        "plan_id": row[0],
        "current_phase": row[1],  # may be None (SQL NULL -> JSON null)
        "completed_phases": completed,
    }


@router.get("/api/plan/{plan_id}/state-from-db")
def state_from_db(plan_id: str):
    """Return the routing row straight from the state-machine SQLite.

    This endpoint is the integration-test seam the task brief
    pins: it MUST read ``plan_routing`` directly from
    ``state.db`` and MUST NOT consult the on-disk plan
    directory or the ``plan_state.py`` JSON fallback that
    ``/api/plan/{plan_id}/state`` uses.

    Status codes:

      * **200** — a row exists for ``plan_id``; the body carries
        ``plan_id``, ``current_phase`` (may be ``null``) and
        ``completed_phases`` (decoded list, default ``[]``).
      * **404** — no row in ``plan_routing``; body is
        ``{"error": "plan not found in routing table",
        "error_code": "PLAN_NOT_FOUND"}``.
      * **503** — the state-db file does not exist on disk; body
        is ``{"error": "state db unavailable", "error_code":
        "STATE_DB_MISSING"}``.
      * **500** — sqlite3 raised while reading; body is
        ``{"error": "state db read failed", "error_code":
        "SQLITE_ERROR"}``.
    """
    _server._validated_plan_id(plan_id)   # uniform contract — see that function
    db_path = _server._state_db_path()
    if not db_path.exists():
        return JSONResponse(
            status_code=503,
            content={
                "error": "state db unavailable",
                "error_code": "STATE_DB_MISSING",
            },
        )

    try:
        row = read_plan_routing_row(plan_id)
    except sqlite3.Error as exc:
        _server.logger.exception(
            "state_from_db: sqlite error reading plan_routing for "
            "plan_id=%r: %s",
            plan_id,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "state db read failed",
                "error_code": "SQLITE_ERROR",
            },
        )

    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "plan not found in routing table",
                "error_code": "PLAN_NOT_FOUND",
            },
        )

    return row


@router.post("/api/plan/{plan_id}/state")
def update_plan_state(plan_id: str, req: _server.PlanStateUpdateRequest):
    """Update plan workflow state."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    archived = _server._archived_plan_response(plan_dir)
    if archived is not None:
        return archived

    state = _server.PlanState(plan_dir)
    if req.arch_enabled is not None:
        state.enable_arch(req.arch_enabled)
    if req.test_enabled is not None:
        state.enable_test(req.test_enabled)
    if req.current_phase is not None:
        try:
            state.transition_to(req.current_phase)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return state.get_state()


@router.post("/api/plan/{plan_id}/phase")
def transition_plan_phase(plan_id: str, req: _server.PlanPhaseTransitionRequest):
    """Transition plan to a target phase with input completeness checks.

    VP-005: When transitioning to ``prd_generation`` and ``interview.json``
    is empty (or missing required dimensions), the transition is refused:
    the response carries ``status="failed"`` (alias ``"blocked"``) plus
    ``error_code="PRD_INPUT_INCOMPLETE"``, and ``current_phase`` is rolled
    back to ``interview``.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    archived = _server._archived_plan_response(plan_dir)
    if archived is not None:
        return archived

    target = req.target_phase
    if not target:
        raise HTTPException(400, "target_phase is required")

    state = _server.PlanState(plan_dir)
    current = state.get_state().get("current_phase", "interview")

    if target == "prd_generation":
        interview_file = plan_dir / "interview.json"
        interview_data: Any = {}
        if interview_file.exists():
            try:
                with open(interview_file, "r", encoding="utf-8") as f:
                    interview_data = json.load(f)
            except (OSError, json.JSONDecodeError):
                interview_data = {}

        if not isinstance(interview_data, dict) or not interview_data:
            state.force_set_phase("interview")
            return {
                "status": "failed",
                "error_code": "PRD_INPUT_INCOMPLETE",
                "detail": "interview.json is empty; transition to prd_generation refused.",
                "plan_id": plan_id,
                "current_phase": "interview",
                "requested_phase": target,
                "blocked": True,
            }

        try:
            is_invalid, missing = PRDGenerator.validate_interview(interview_data)
        except Exception:
            is_invalid, missing = True, []

        if is_invalid:
            state.force_set_phase("interview")
            return {
                "status": "failed",
                "error_code": "PRD_INPUT_INCOMPLETE",
                "detail": (
                    "interview.json is empty or missing required dimensions: "
                    + ", ".join(missing)
                ),
                "plan_id": plan_id,
                "current_phase": "interview",
                "requested_phase": target,
                "blocked": True,
                "missing_dimensions": missing,
            }

    try:
        state.transition_to(target)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    result = state.get_state()
    result["status"] = "ok"
    result["requested_phase"] = target
    return result


@router.post("/api/tasks/{plan_id}/generate")
def generate_tasks(plan_id: str):
    """Generate tasks.json from reviewed PRD and auto-validate workspace."""
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # 产物门禁（五个调用点之一）。
    #
    # 目录里已经有计划书（``verification-plan.md``）时，这个 plan 属于**验证
    # 分支** —— 往里写 ``tasks.json`` 就是在替人择一，还会当场造出「两类产物
    # 同时存在」的冲突，让后面每一个入口都 409。框架不替人决定，也不删任何
    # 文件：两个产物的**相对路径**交回去，由操作者裁决。
    #
    # 判定来自 :func:`plan_artifacts.detect`（唯一入口）；错误语义与其它四个
    # 调用点逐字相同。
    #
    # 载荷构造器住在 ``routes.plans``（那里同时是状态接口与列表的所在地）。
    # 这里是**函数内**导入的：路由模块之间在模块级互相导入会绕成环
    # （``server`` 在文件底部才导入它们），函数内导入则是在请求到达时求值，
    # 那时一切都已加载完毕。
    from routes.plans import artifact_conflict_payload

    artifacts = detect(plan_dir)
    if artifacts.plan_doc_path is not None:
        raise HTTPException(
            409,
            artifact_conflict_payload(plan_id, artifacts),
        )

    # VP-006: block downstream phases while the PRD is a placeholder.
    _reject_if_placeholder_prd(plan_dir, plan_id)

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    state = _server.PlanState(plan_dir)
    gen = TasksGenerator(
        coding_tool, plan_dir,
        self_review_enabled=state.is_self_review_enabled(),
    )
    try:
        tasks_data = gen.generate()
    except TimeoutError as exc:
        _server.logger.warning("Tasks generation timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Tasks generation timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )
    except TasksGenerationError as exc:
        # LLM mis-fire (0 tasks returned) or post-processing rejected
        # all tasks. Surface the error payload (which is already
        # written to tasks.json by the generator) so the user can
        # decide to retry. Do NOT synthesise a fallback plan — that
        # was the previous misleading behaviour.
        _server.logger.warning(
            "Tasks generation failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            422,
            {
                "error": "Tasks generation failed",
                "detail": str(exc),
                "error_payload": exc.error_payload,
                "plan_id": plan_id,
                "retry_hint": (
                    "Re-run POST /api/tasks/{plan_id}/generate; the "
                    "LLM call will be retried with the same upstream "
                    "docs."
                ).format(plan_id=plan_id),
            },
        )
    except SelfReviewUnavailableError as exc:
        _server.logger.warning(
            "Tasks self-review unavailable for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            503,
            {
                "error": "Tasks self-review unavailable",
                "detail": str(exc),
                "plan_id": plan_id,
                "status": "self_review_unavailable",
            },
        )

    # Auto-validate workspace after generation: route tasks away from the
    # formal repo (where the backend server runs) toward the matching dev repo.
    # This prevents accidental modification of deploy/production code.
    formal_repo_dir = _server._PROJECT_ROOT
    tasks_data = gen.validate_workspace(
        tasks_data, search_dirs=None, formal_repo_dir=formal_repo_dir
    )
    # 2026-09-21: this is the LAST gate before tasks.json is written, and
    # the only one. Everything the executor used to check at load time
    # (JSON shape, depends_on consistency, files_to_modify existence,
    # test_command function names) is enforced here, against each task's
    # OWN ``project_dir``. A tasks.json that reaches the executor is
    # therefore runnable, and ``POST /api/execution/{id}/start`` always
    # succeeds. See ``TasksGenerator.harden_for_execution``.
    try:
        tasks_data = gen.harden_for_execution(tasks_data)
    except TasksGenerationError as exc:
        _server.logger.warning(
            "Tasks hardening failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            422,
            {
                "error": "Generated tasks are not runnable",
                "detail": str(exc),
                "error_payload": exc.error_payload,
                "plan_id": plan_id,
                "retry_hint": (
                    "Fix the reported tasks (or the target project "
                    "layout) and re-run POST /api/tasks/{plan_id}/generate."
                ),
            },
        )
    # Persist the workspace-validated result back to tasks.json.
    tasks_file = plan_dir / "tasks.json"
    with open(tasks_file, "w", encoding="utf-8") as f:
        json.dump(tasks_data, f, ensure_ascii=False, indent=2)

    return {"plan_id": plan_id, "tasks": tasks_data}


# --- Preflight Endpoints (DP2) ---
#
# The preflight cross-document reviewer (``backend/preflight_review.py``)
# is invoked internally by ``TasksGenerator`` before generating tasks.
# These two endpoints expose the same reviewer to the frontend and to
# AI agents so they can:
#
#   * Trigger a re-run on demand (``POST /api/preflight/{plan_id}/run``)
#   * Read the latest persisted report
#     (``GET /api/preflight/{plan_id}/report``)
#
# Boundary conditions pinned by ``tests/unit/test_preflight_endpoints.py``:
#
#   * Unknown ``plan_id`` → 404 (the endpoint must NOT lazily create
#     the plan directory).
#   * ``plan_state.flags.preflight_enabled == False`` → POST returns
#     403 with a meaningful message, no LLM call, no artifact written.
#   * On the happy path POST writes ``preflight_report.json`` to
#     ``plans/<plan_id>/`` and returns the report dict synchronously.
#   * GET returns 200 with the persisted content when the artifact
#     exists, 404 otherwise.


@router.post("/api/preflight/{plan_id}/run")
def run_preflight(plan_id: str):
    """Run the preflight cross-document reviewer on demand.

    Returns the report dict (``findings`` / ``high_count`` /
    ``report_path``) synchronously. Side effect: writes
    ``plans/<plan_id>/preflight_report.json`` (overwriting any prior
    artifact, per the DP2 contract).
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    # Honour the ``preflight_enabled`` flag exactly as the
    # ``TasksGenerator`` does, so the HTTP surface stays consistent
    # with the internal gate. Reading via ``PlanState`` keeps the
    # default (``True`` for new plans) centralised in one place.
    state = _server.PlanState(plan_dir)
    if not state.is_preflight_enabled():
        raise HTTPException(
            403,
            "Preflight is disabled for this plan "
            "(plan_state.flags.preflight_enabled == False)",
        )

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    reviewer = PreFlightReviewer(coding_tool, plans_root=_server.PLANS_DIR)
    report = reviewer.run(plan_id)
    return report


@router.get("/api/preflight/{plan_id}/report")
def get_preflight_report(plan_id: str):
    """Return the persisted preflight report for ``plan_id``.

    Returns 404 when no ``preflight_report.json`` exists for the plan.
    The body shape matches the POST response so callers can read both
    endpoints interchangeably.
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    report_path = plan_dir / "preflight_report.json"
    if not report_path.exists():
        raise HTTPException(404, "Preflight report not found")

    try:
        with open(report_path, "r", encoding="utf-8") as fp:
            report = json.load(fp)
    except (OSError, ValueError) as exc:
        _server.logger.warning(
            "Failed to read preflight_report.json for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            500,
            f"Failed to read preflight report: {exc}",
        )

    return report


@router.post("/api/tasks/{plan_id}/validate_workspace")
def validate_task_workspace(plan_id: str, req: Optional[dict] = None):
    """Post-generation: validate and fix each task's project_dir + test commands.

    Scans the local filesystem for plausible workspace candidates and asks
    the LLM to assign each generated task to the correct one. Use after
    POST /api/tasks/{plan_id}/generate to fix hallucinated project_dir
    values (e.g. one checkout of the project when the declared
    ``project_dir`` points at a different one).
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(404, "Plan not found")

    tasks_file = plan_dir / "tasks.json"
    if not tasks_file.exists():
        raise HTTPException(404, "tasks.json not found — call /generate first")

    search_dirs = None
    formal_repo_dir = None
    if isinstance(req, dict):
        if req.get("search_dirs"):
            search_dirs = [Path(p) for p in req["search_dirs"]]
        if req.get("formal_repo_dir"):
            formal_repo_dir = Path(req["formal_repo_dir"]).expanduser().resolve()

    with open(tasks_file) as f:
        tasks_data = json.load(f)

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    gen = TasksGenerator(coding_tool, plan_dir)
    # Default: the formal repo is the repository that hosts this
    # server.py. It must never be selected as a dev workspace.
    if formal_repo_dir is None:
        formal_repo_dir = _server._PROJECT_ROOT
    updated = gen.validate_workspace(
        tasks_data, search_dirs=search_dirs, formal_repo_dir=formal_repo_dir
    )

    with open(tasks_file, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)

    return {
        "plan_id": plan_id,
        "tasks": updated,
        "validated_count": sum(
            1 for t in updated.get("tasks", []) if isinstance(t, dict) and t.get("_workspace_validated")
        ),
        "venv_injected_count": sum(
            1 for t in updated.get("tasks", []) if isinstance(t, dict) and t.get("_venv_injected")
        ),
        "needs_user_confirmation_count": sum(
            1 for t in updated.get("tasks", []) if isinstance(t, dict) and t.get("_workspace_ask_user")
        ),
        "needs_user_confirmation_tasks": [
            {"id": t.get("id"), "ask_user": t.get("_workspace_ask_user"), "reason": t.get("_workspace_reason", "")}
            for t in updated.get("tasks", [])
            if isinstance(t, dict) and t.get("_workspace_ask_user")
        ],
        "total_count": len(updated.get("tasks", [])),
    }


@router.get("/api/tasks/{plan_id}")
def get_tasks(plan_id: str):
    """Get generated tasks with runtime status overlay + counts.

    Returns ``{plan_id, tasks, counts}`` where ``tasks`` is the flat
    array of task dicts (id/title/description/test_command/...) with
    runtime fields overlaid from the state-machine SQLite row
    (``status``, ``end_ts``, ``commit_sha``, ``attempt``,
    ``failure_reason``), and ``counts`` is ``{total, completed,
    failed, skipped, in_progress, pending}``.

    Two bugs in the previous contract are fixed:

    1. **Nested-bug.** The previous implementation returned
       ``{"plan_id": ..., "tasks": <whole tasks.json>}`` — the
       whole file (which has shape ``{"tasks": [...]}``) was nested
       one level too deep, so the frontend's ``data.tasks.map(...)``
       saw a dict and crashed. The handler now extracts the array
       first (matching ``/api/execution/{plan_id}/progress``).
    2. **No runtime overlay.** Without it the frontend cannot tell
       a passed task from a failed one — all tasks carried
       ``status=None`` and the task panel showed no state. We now
       overlay runtime fields from ``plan_task_repository`` (the
       same seam ``/api/execution/{plan_id}/progress`` uses).

    Backward compatibility: the response still includes a ``tasks``
    array — every existing field stays. Only nested-shape callers
    (none in this repo) would break.
    """
    plan_dir = _server._plan_dir(plan_id)
    tasks_file = plan_dir / "tasks.json"
    if not tasks_file.exists():
        raise HTTPException(404, "Tasks not found")

    # 1) Load static tasks.json and unwrap the array.
    tasks: list = []
    try:
        data = json.loads(tasks_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Failed to read tasks.json: {exc}")
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []

    # 2) Overlay runtime fields from state-machine SQLite (the
    #    ``PlanTaskRepository`` snapshot — same path as
    #    ``/api/execution/{plan_id}/progress``). This is the
    #    authoritative per-task ``status`` source — ``tasks.json``
    #    is static-only.
    _RUNTIME_OVERLAY_FIELDS = (
        "status",
        "end_ts",
        "commit_sha",
        "attempt",
        "schedule_ts",
        "failure_reason",
    )
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository as _PlanTaskRepo,
        )

        runtime_conn = _open_db(_server._state_db_path())
        try:
            _migrate(runtime_conn)
            runtime = _PlanTaskRepo(runtime_conn).load_all(plan_id)
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                rt = runtime.get(t.get("id"))
                if not rt:
                    continue
                for k in _RUNTIME_OVERLAY_FIELDS:
                    if k in rt:
                        t[k] = rt[k]
        finally:
            runtime_conn.close()
    except (sqlite3.OperationalError, OSError):
        # State-machine not on disk yet — fall through with the
        # static status (``None`` → "pending" in the count loop).
        pass

    # 3) Counts: derive from the (now-overlay-enriched) tasks list.
    #    ``skipped`` is included as a separate bucket so the
    #    frontend can render it without re-deriving.
    counts = {
        "total": len(tasks),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "in_progress": 0,
        "pending": 0,
    }
    for t in tasks:
        st = (t.get("status") if isinstance(t, dict) else None) or "pending"
        if st in counts:
            counts[st] += 1
        elif st not in counts:
            # Unknown status — log so we can spot new ones; don't
            # silently inflate the pending bucket.
            _server.logger.warning(
                "tasks_api_unknown_status plan_id=%s task_id=%s status=%s",
                plan_id, t.get("id") if isinstance(t, dict) else "?", st,
            )

    return {"plan_id": plan_id, "tasks": tasks, "counts": counts}


# ---------------------------------------------------------------------------
# 验证分支：计划书生成入口
# ---------------------------------------------------------------------------
#
# 验证分支在 PRD 审完之后产出的是**计划书**（``verification-plan.md``），不是
# 任务清单（``tasks.json``）。这个端点是那条路上的唯一入口。
#
# 它守住三道门，每道门都在**写盘之前**：
#
#   1. **产物冲突** —— 目录里已有 ``tasks.json`` → 409 ``artifact_conflict``。
#      响应里给出两个产物的**相对路径**（不泄露本机绝对路径），并且不删任何
#      文件：「这个 plan 到底要执行开发任务、还是要按计划书直接验证」只有人
#      能回答，框架不替人猜，也不替人删。
#   2. **PRD 未审完** → 409 ``prd_review_incomplete``。没审完的 PRD 不值得
#      花一次模型调用。
#   3. **阶段不匹配** → 409 ``stage_mismatch``。
#
# 生成的文档不满足十章节契约 → 422，且**不落盘**（生成器先校验、后落盘）。
#
# 基线只有**一个**来源：计划状态里记录的那份（``verification.baseline``）。
# 这个端点**没有基线参数** —— 请求里带进来的基线字段会被丢掉（架构决策点
# 3：生成与启动接口都不接受基线参数）。


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """读一个 JSON 对象文件。

    读不出来（文件缺失 / 不是合法 JSON / 顶层不是对象）时返回 ``None`` ——
    在这里「读不懂」与「没有」是同一件事：都判不出内容。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _verification_required_prd_indices(
    plan_dir: Path, raw_items: List[Any]
) -> Optional[set]:
    """必须被审过的 PRD 决策点序号集合。

    ``prd.json`` 在 → 用它的决策点序号（与既有 PRD 门禁同一口径）；不在 →
    用 ``review.json`` 里的条目序号。返回 ``None`` 表示「判不出来」
    （``prd.json`` 存在但损坏）——按未解决处理。
    """
    prd_path = plan_dir / PRD_FILENAME
    if prd_path.exists():
        prd = _read_json_object(prd_path)
        if prd is None:
            return None
        points = prd.get("decision_points")
        if not isinstance(points, list):
            points = []
        return {
            point.get("index") for point in points if isinstance(point, dict)
        }
    return {raw.get("index") for raw in raw_items if isinstance(raw, dict)}


def _verification_prd_pending(plan_dir: Path) -> Optional[List[Any]]:
    """PRD 审核里还没解决的决策点序号。

    这是 :func:`verification_plan_review.prd_is_resolved` 的**路由侧镜像**，
    唯一的一处不同是 skip 的语义：那个函数按**分支**区分（验证分支认 skip、
    开发分支只认 accepted），而这个端点只服务验证分支，所以这里一律按
    「accepted 或 skipped 算已解决」判断 —— 这正是 prd.md 的要求：最小 PRD
    的每一条决策点 accept **或** skip 之后才能生成计划书。

    为什么要镜像而不是直接调那个函数：它按「目录里有没有计划书」判断分支，
    而计划书正是**这个端点**要生成的 —— 调它的时候分支还没落地，skip 会被
    当成没解决。两处的判据（哪些决策点必须被审过、什么状态算已解决）逐字
    一致，所以镜像不会给出与它不同的答案。

    返回 ``None`` 表示「判不出来」（审核文件缺失 / 损坏）——按未解决处理；
    返回空列表表示全部已解决。
    """
    review = _read_json_object(plan_dir / PRD_REVIEW_FILENAME)
    if review is None:
        return None
    raw_items = review.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    statuses: Dict[Any, str] = {}
    for raw in raw_items:
        if isinstance(raw, dict):
            statuses[raw.get("index")] = str(raw.get("status") or STATUS_PENDING)
    required = _verification_required_prd_indices(plan_dir, raw_items)
    if required is None:
        return None
    pending = [
        index
        for index in required
        if statuses.get(index) not in RESOLVED_STATUSES
    ]
    pending.sort(key=repr)
    return pending


def _plan_generation_target_allowed(phase: str) -> bool:
    """这个阶段能不能进 ``tasks_generation``（计划书生成）。

    与 :meth:`plan_state.PlanState._transition_to` 的合法性判定同一口径：主表
    + 验证子表 + 专用表。为什么要在这里先问一遍：阶段不匹配必须在**写盘
    之前**被拒 ——「被拒绝的请求不写盘」是这条链路的契约，而阶段判定发生
    在文档生成之后。
    """
    allowed = set(PHASE_TRANSITIONS.get(phase, []))
    allowed.update(VERIFICATION_PHASE_TRANSITIONS.get(phase, []))
    allowed.update(DEDICATED_PHASE_TRANSITIONS.get(phase, []))
    return "tasks_generation" in allowed


def _recorded_baseline(state: Any) -> Optional[Dict[str, Any]]:
    """计划状态里记录下来的基线；没有记录时返回 ``None``。

    基线冻在 ``verification.baseline`` 里 —— 与 ``verification_gate`` 读的
    是同一处。计划书文档不在此列：文档是评审覆盖的内容，评审之后被改写的
    文档是另一份文档。
    """
    verification = state.get_state().get("verification") or {}
    recorded = verification.get("baseline")
    return recorded if isinstance(recorded, dict) else None


@router.post("/api/verification-plan/{plan_id}/generate")
def generate_verification_plan(plan_id: str, req: _server.RefineRequest = None):
    """生成验证计划书（验证分支的产物）。

    Args:
        plan_id: 计划 id。
        req: 请求体。目前只有 ``feedback`` 一个字段，而**生成**这一步用不上
            它（提示词只吃 plan_id 与基线）—— 改文档是 refine 端点的事。这里
            仍然接受它，是为了让请求体形状与其它生成端点一致。

    Returns:
        200 ``{"plan_id", "document_path", "current_phase"}``。
        ``document_path`` 是**相对路径**（``verification-plan.md``），不泄露
        本机绝对路径。

    Raises:
        HTTPException: 404 ``plan_not_found``（计划目录不存在）；409
            ``artifact_conflict``（目录里已有 ``tasks.json``）；409
            ``prd_review_incomplete``（PRD 审核还有没解决的决策点）；409
            ``stage_mismatch``（当前阶段不能进计划书生成）；422
            ``baseline_missing``（计划状态里没有记录基线）；422
            ``verification_plan_invalid``（生成的文档不满足十章节契约，且
            没有落盘）；504（生成超时）。
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(
            404,
            {
                "error": "plan_not_found",
                "detail": "Plan not found",
                "plan_id": plan_id,
            },
        )

    # 门 1 —— 产物不冲突。``tasks.json`` 在场说明这个 plan 要走开发分支，
    # 而验证分支的产物是计划书。框架不替人择一，也不删任何文件：两个产物
    # 的**相对路径**交回去，由操作者裁决。
    #
    # 载荷与其它四个调用点逐字相同（共用 ``routes.plans`` 里的构造器）。那里
    # 的导入是**函数内**的：路由模块之间在模块级互相导入会绕成环（``server``
    # 在文件底部才导入它们），函数内导入是在请求到达时求值，那时一切都已加载
    # 完毕。
    from routes.plans import artifact_conflict_payload

    artifacts = detect(plan_dir)
    if artifacts.tasks_path is not None:
        raise HTTPException(
            409,
            artifact_conflict_payload(plan_id, artifacts),
        )

    # 门 2 —— PRD 已审完（accepted 或 skipped 都算已解决）。
    pending = _verification_prd_pending(plan_dir)
    if pending is None or pending:
        raise HTTPException(
            409,
            {
                "error": "prd_review_incomplete",
                "detail": (
                    "PRD 审核还没解决：最小 PRD 的每一条决策点都要 accept 或 "
                    "skip，才能生成验证计划书。"
                ),
                "plan_id": plan_id,
                # 判不出来时是空的 —— 那是「读不懂」，不是「还有几条没审」。
                "pending": list(pending) if pending else [],
                "review_readable": pending is not None,
            },
        )

    # 门 3 —— 阶段能进计划书生成。
    state = _server.PlanState(plan_dir)
    current_phase = state.get_current_phase()
    if not _plan_generation_target_allowed(current_phase):
        raise HTTPException(
            409,
            {
                "error": "stage_mismatch",
                "reason": "stage_mismatch",
                "detail": (
                    f"当前阶段 {current_phase} 不能进计划书生成；PRD 审完之后"
                    "（prd_review）才生成计划书。"
                ),
                "plan_id": plan_id,
                "current_phase": current_phase,
            },
        )

    # 基线：只从计划状态里读。这个端点没有基线参数，注入进来的基线字段
    # 不会、也不允许改变这里读到的那一份。
    baseline = _recorded_baseline(state)
    if baseline is None:
        raise HTTPException(
            422,
            {
                "error": "baseline_missing",
                "detail": (
                    "计划状态里没有记录基线（verification.baseline），无法生成"
                    "计划书：计划书的 baseline: 行必须与已记录的基线一致，而本"
                    "端点不接受基线参数。"
                ),
                "plan_id": plan_id,
            },
        )
    context = {
        "plan_id": plan_id,
        "baseline": {
            "repo": baseline.get("repo_dir"),
            "ref": baseline.get("ref"),
            "pr": baseline.get("pr_number"),
        },
    }

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    try:
        generate_plan_doc(plan_dir, context, coding_tool)
    except PlanDocError as exc:
        # 文档不合法：生成器先校验后落盘，所以磁盘上不会留下这份文档。
        _server.logger.warning(
            "Verification plan generation produced an invalid document for "
            "plan %s: %s",
            plan_id,
            exc,
        )
        raise HTTPException(
            422,
            {
                "error": "verification_plan_invalid",
                "detail": (
                    "生成的验证计划书不满足十章节契约，已丢弃，没有落盘。"
                ),
                "plan_id": plan_id,
                "errors": [
                    {
                        "section": error.section,
                        "line": error.line,
                        "code": error.code,
                        "message": error.message,
                    }
                    for error in exc.errors
                ],
            },
        )
    except TimeoutError as exc:
        _server.logger.warning(
            "Verification plan generation timed out: %s", exc
        )
        raise HTTPException(
            504,
            {
                "error": "Verification plan generation timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    # 阶段推进：专用入口（PRD skip 的那条边）→ 「计划书待审」。
    #
    # 顺序有讲究：文档先落盘，阶段后推进。专用入口判「PRD 是否已解决」时
    # 按分支区分 skip 的语义 —— 目录里有了计划书，验证分支才算落地，skip
    # 这时才算数。文档没落盘之前调它，整条 skip 的 PRD 会被误判成没审完。
    try:
        state.start_verification_plan_generation()
        state.transition_to("verification_plan_review")
    except ValueError as exc:
        raise HTTPException(
            409,
            {
                "error": "stage_mismatch",
                "reason": "stage_mismatch",
                "detail": str(exc),
                "plan_id": plan_id,
                "current_phase": state.get_current_phase(),
            },
        )

    return {
        "plan_id": plan_id,
        "document_path": PLAN_DOC_FILENAME,
        "current_phase": state.get_current_phase(),
    }


# ---------------------------------------------------------------------------
# 验证分支：计划书的只读入口（人工审核用）
# ---------------------------------------------------------------------------
#
# 审核者要看到三样东西：文档原文、结构校验结果、条目列表。两个只读端点各给
# 一份：``GET /api/verification-plan/{plan_id}`` 给文档，
# ``GET /api/verification-plan/{plan_id}/review/items`` 给条目。两者读的是同一份
# 磁盘上的文档 —— 审核界面上看到的原文与被审的原文必须是同一份，否则就成了
# 「审的是 A、看到的是 B」。
#
# 基线只有一个来源：文档里那一行 ``baseline:``。审核通过之后冻结的基线就是它，
# 所以 accept 之前必须让审核者看见它 —— ``PLAN-OBJECT`` 条目带着它（架构决策点
# 3：审核对象与派生视图）。
#
# 结构非法不是接口错误：200 + 非空 ``structure_errors``。审核者要看到的是
# 「这份文档哪里不对」，不是一句 500。


#: 携带基线的审核条目 id（「被验证对象」条目覆盖基线行本身）。
_PLAN_OBJECT_ITEM_ID = "PLAN-OBJECT"


def _verification_plan_bytes(plan_dir: Path) -> Optional[bytes]:
    """计划书的**原始字节**；文件不在时返回 ``None``。

    哈希是按文件字节算的，所以这一层给的是字节而不是解码后的文本：解码会丢掉
    「这份字节不是合法 UTF-8」这件事，而哈希必须与落盘审核状态里记的那一份
    逐字节相同。
    """
    try:
        return (plan_dir / PLAN_DOC_FILENAME).read_bytes()
    except OSError:
        return None


def _verification_plan_content(plan_dir: Path) -> Optional[str]:
    """计划书原文；文件不在时返回 ``None``。

    按**原始字节**读、按 UTF-8 解码：审核状态是按文件字节算哈希的，换行被
    归一化过的文本算出来的哈希与审核状态对不上，审核会被无声地作废。

    字节不是合法 UTF-8 时按替换字符解码而不是抛错：审核者要看到的是那份
    文档此刻的样子，不是 500。
    """
    raw = _verification_plan_bytes(plan_dir)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _verification_plan_baseline_payload(doc: BaselineDoc) -> Dict[str, Any]:
    """基线三字段：仓库目录、ref、PR 号。

    只有这三个字段：``commit_sha`` 与解析后的工作区都**不**在这里 —— 基线是
    「审核覆盖的那一行」，把它变成一个真实的工作区属于冻结阶段的事（那时才
    做，且只做一次）。
    """
    return {
        "repo_dir": str(doc.repo_dir),
        "ref": doc.ref,
        "pr_number": doc.pr_number,
    }


def _verification_plan_baseline(content: str) -> Optional[Dict[str, Any]]:
    """计划书里的基线。

    结构合法时走 :func:`verification_plan_doc.parse_plan_doc`；结构非法时仍
    尽力把「被验证对象」章节里的基线行取出来 —— 审核者恰恰在文档有问题时最
    需要知道「被验证的到底是哪个仓库、哪个 ref」。取不出来才返回 ``None``。
    """
    try:
        parsed = parse_plan_doc(content)
    except StructureError:
        return _verification_plan_baseline_lenient(content)
    return _verification_plan_baseline_payload(parsed.baseline)


def _verification_plan_baseline_lenient(content: str) -> Optional[Dict[str, Any]]:
    """结构非法时的基线提取：只找「被验证对象」章节里的那一行。"""
    body: List[Tuple[int, str]] = []
    in_object = False
    for lineno, raw in enumerate(content.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("## "):
            if in_object:
                break
            in_object = stripped[3:].strip() == OBJECT_SECTION
            continue
        if in_object:
            body.append((lineno, raw))
    if not body:
        return None
    try:
        doc = parse_baseline(
            "\n".join(text for _lineno, text in body),
            line_number=body[0][0],
            section=OBJECT_SECTION,
        )
    except StructureError:
        return None
    return _verification_plan_baseline_payload(doc)


def _verification_plan_review_statuses(
    plan_dir: Path,
) -> Dict[int, Tuple[str, str]]:
    """已落盘的审核状态：条目序号 → (状态, 原因)。

    审核状态是文档的**派生视图**：文档字节变了，
    :func:`verification_plan_review.load` 会把全部条目退回 pending —— 旧审核
    不再覆盖当前文档。
    """
    state = load_plan_review_state(plan_dir)
    if state is None:
        return {}
    return {item.index: (item.status, item.note) for item in state.items}


def _verification_plan_review_payload(
    plan_dir: Path, parsed: Any
) -> List[Dict[str, Any]]:
    """审核条目的负载：形状固定五键，``PLAN-OBJECT`` 多带一份基线。

    条目本身（id / 标题 / 序号）来自**当前文档** —— 审核状态是文档的派生视图，
    文档改了条目集合就跟着改；状态与原因取自已落盘的审核状态。

    两个端点（读条目 / 记动作）共用这一份构造：审核界面上看到的条目与被记的
    条目必须是同一份形状。
    """
    statuses = _verification_plan_review_statuses(plan_dir)
    baseline = _verification_plan_baseline_payload(parsed.baseline)
    payload: List[Dict[str, Any]] = []
    for item in extract_items(parsed):
        entry = item.to_dict()
        status, note = statuses.get(item.index, (entry["status"], entry["note"]))
        entry["status"] = status
        entry["note"] = note
        if item.id == _PLAN_OBJECT_ITEM_ID:
            entry["baseline"] = baseline
        payload.append(entry)
    return payload


@router.get("/api/verification-plan/{plan_id}")
def get_verification_plan(plan_id: str):
    """读取验证计划书：原文 + 结构校验结果 + 基线。

    Args:
        plan_id: 计划 id。

    Returns:
        200 ``{"content", "structure_errors", "baseline"}``。``content`` 是
        磁盘上的原文（逐字）；``structure_errors`` 是全部结构违规（合法时是
        空列表，每条是 ``{section, line, code, message}``）；``baseline`` 是
        文档里那一行基线（``repo_dir`` / ``ref`` / ``pr_number``），取不出来
        时是 ``None``。

        结构非法**不是**接口错误：200 + 非空 ``structure_errors``。审核者要
        看到的是「这份文档哪里不对」，不是一句 500。

    Raises:
        HTTPException: 404 ``plan_not_found``（计划目录不存在）；404
            ``document_not_found``（目录在，计划书不在）。
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(
            404,
            {
                "error": "plan_not_found",
                "detail": "Plan not found",
                "plan_id": plan_id,
            },
        )
    content = _verification_plan_content(plan_dir)
    if content is None:
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )
    return {
        "content": content,
        "structure_errors": [
            {
                "section": error.section,
                "line": error.line,
                "code": error.code,
                "message": error.message,
            }
            for error in validate_structure(content)
        ],
        "baseline": _verification_plan_baseline(content),
    }


@router.get("/api/verification-plan/{plan_id}/review/items")
def get_verification_plan_review_items(plan_id: str):
    """读取计划书的审核条目列表。

    条目是文档的**派生视图**：5 个计划级条目（id 与顺序是契约）+ 文档里每个
    验证点一个条目。条目形状固定 ``{index, id, title, status, note}``；
    ``PLAN-OBJECT`` 多带一份 ``baseline`` —— 它覆盖的正是文档里那行
    ``baseline:``，accept 之前必须让审核者看见将被冻结的基线。

    ``status`` / ``note`` 取自已落盘的审核状态（``verification-plan-review.
    json``）。文档字节变了，那些状态一律作废（全部回到 pending）—— 旧审核
    不再覆盖当前文档。

    Args:
        plan_id: 计划 id。

    Returns:
        200 ``{"items": [...], "doc_sha256": "..."}``。文档结构非法时条目是空
        列表（仍是 200）：条目是从合法文档里提取的，非法文档没有条目可提取 ——
        结构错误由文档端点逐条给出。

        ``doc_sha256`` 是当前计划书的字节哈希。审核者要把这一版**记下来**再
        去提交动作：文档在这期间被改过，动作端点会 409 ``document_changed``
        要求重读条目（审核不能覆盖一份已经变过的文档）。

    Raises:
        HTTPException: 404 ``plan_not_found``（计划目录不存在）；404
            ``document_not_found``（目录在，计划书不在）。
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(
            404,
            {
                "error": "plan_not_found",
                "detail": "Plan not found",
                "plan_id": plan_id,
            },
        )
    raw = _verification_plan_bytes(plan_dir)
    if raw is None:
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )
    content = raw.decode("utf-8", errors="replace")
    try:
        parsed = parse_plan_doc(content)
    except StructureError as exc:
        _server.logger.warning(
            "Verification plan %s has an invalid structure; it has no review "
            "items: %s",
            plan_id,
            exc,
        )
        return {"items": [], "doc_sha256": doc_sha256(raw)}

    return {
        "items": _verification_plan_review_payload(plan_dir, parsed),
        "doc_sha256": doc_sha256(raw),
    }


# ---------------------------------------------------------------------------
# 验证分支：计划书的审核动作（逐条 accept / skip / reset / revise）
# ---------------------------------------------------------------------------
#
# 门禁是**逐条**的：每一条都要被处置过（accept，或者带原因的 skip），pending
# 阻断。所以动作落在条目上，不落在文档上 —— 文档只有一个状态，条目有七个。
#
# 三个动作各有各的语义：
#
#   * ``accept`` —— 这条没问题，审过；
#   * ``skip`` —— 本次不验这条，但**必须带原因**：没有原因的 skip 在报告里无法
#     归因，等于把条目悄悄扔掉；
#   * ``reset`` —— 这条先不算数，退回待审，原因一并清空；
#   * ``revise`` —— 这条要改，退回待审，但**留着** note：那条 note 是后续
#     refine 要用的反馈，丢了就没人知道要改什么。
#
# 审核状态与文档**字节**绑定（``doc_sha256``）：文档被改过之后，之前那份审核
# 针对的是另一份文档。请求带着的哈希对不上当前文档 → 409 ``document_changed``，
# 要求重读条目再操作 —— 审核不能覆盖一份已经变过的文档。


#: 计划书审核动作的合法取值。
_VERIFICATION_PLAN_REVIEW_ACTIONS: Tuple[str, ...] = (
    "accept",
    "skip",
    "reset",
    "revise",
)


class _VerificationPlanReviewActionRequest(BaseModel):
    """计划书审核动作的请求体。

    Attributes:
        action: ``accept`` / ``skip`` / ``reset`` / ``revise``。
        note: 原因。``skip`` 必须给非空原因；``revise`` 的 note 是 refine 反馈。
        doc_sha256: **可选**：审核者读到的那一版文档的字节哈希。不带它时，
            端点只能拿已落盘的审核状态去比「文档是不是被改过」。
    """

    action: str
    note: str = ""
    doc_sha256: Optional[str] = None


def _write_plan_review_note(
    plan_dir: Path, state: Any, item_index: int, note: str
) -> None:
    """把 refine 反馈写回条目（``revise`` 动作）。

    :func:`verification_plan_review.record_action` 的动作词汇里没有 ``revise``
    —— 模块自己的注释说得很清楚：「改文档是另一条链路（改文档会改哈希，审核
    随之失效），不在这里假装能改」。所以这里借 ``reset`` 落状态（退回待审、清空
    原因），再把 note 补回去。

    写出的形状与模块内部一致（``ReviewState.to_dict``），读回仍走模块自己的
    ``load``；落盘走临时文件再替换，避免半截文件。
    """
    for item in state.items:
        if item.index == item_index:
            item.note = note
    path = review_path(plan_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


@router.post("/api/verification-plan/{plan_id}/review/item/{item_index}")
def review_verification_plan_item(
    plan_id: str,
    item_index: int,
    req: _VerificationPlanReviewActionRequest,
):
    """记录一次计划书审核动作（逐条）。

    一次动作只动**一条**：``accept`` / ``skip`` / ``reset`` / ``revise``。动作
    落盘走 :func:`verification_plan_review.record_action` —— 与生产路径同一条。

    Args:
        plan_id: 计划 id。
        item_index: 条目序号（0 起，计划级条目在前、验证点条目按文档顺序在后）。
        req: ``{action, note, doc_sha256}``。``doc_sha256`` 是审核者读到的那一版
            文档的哈希，可选。

    Returns:
        200 ``{"items": [...], "doc_sha256": "...", "resolved": false}``。
        ``items`` 是全量条目（形状与 ``GET .../review/items`` 一致）；``doc_sha256``
        是当前文档的字节哈希；``resolved`` 是这份审核是否已经完备（全部条目
        accepted / skipped）。

    Raises:
        HTTPException: 404 ``plan_not_found``（计划目录不存在）；404
            ``document_not_found``（目录在，计划书不在）；404 ``item_not_found``
            （条目序号不存在）；400 ``unknown_action``（动作不认识）；400
            ``skip_note_required``（skip 缺非空原因）；409
            ``document_changed``（请求带的哈希对不上当前文档，要求重读条目）；
            422 ``verification_plan_invalid``（计划书结构非法，条目无从提取）。
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(
            404,
            {
                "error": "plan_not_found",
                "detail": "Plan not found",
                "plan_id": plan_id,
            },
        )

    raw = _verification_plan_bytes(plan_dir)
    if raw is None:
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )

    action = str(req.action or "").strip().lower()
    if action not in _VERIFICATION_PLAN_REVIEW_ACTIONS:
        raise HTTPException(
            400,
            {
                "error": "unknown_action",
                "detail": (
                    f"未知的审核动作：{req.action!r}"
                    f"（可用：{', '.join(_VERIFICATION_PLAN_REVIEW_ACTIONS)}）"
                ),
                "plan_id": plan_id,
                "item_index": item_index,
            },
        )

    reason = str(req.note or "").strip()
    if action == "skip" and not reason:
        raise HTTPException(
            400,
            {
                "error": "skip_note_required",
                "detail": "skip 必须带非空原因（note）：没有原因的 skip 无法在报告里归因",
                "plan_id": plan_id,
                "item_index": item_index,
            },
        )

    content = raw.decode("utf-8", errors="replace")
    try:
        parsed = parse_plan_doc(content)
    except StructureError as exc:
        _server.logger.warning(
            "Verification plan %s has an invalid structure; it has no review "
            "items to review: %s",
            plan_id,
            exc,
        )
        raise HTTPException(
            422,
            {
                "error": "verification_plan_invalid",
                "detail": (
                    "计划书结构非法，没有可审核的条目；结构错误由文档端点逐条给出"
                ),
                "plan_id": plan_id,
            },
        )

    if not any(item.index == item_index for item in extract_items(parsed)):
        raise HTTPException(
            404,
            {
                "error": "item_not_found",
                "detail": f"审核条目序号不存在：{item_index}",
                "plan_id": plan_id,
                "item_index": item_index,
            },
        )

    # 哈希闸门：审核不能覆盖一份已经变过的文档。
    current_sha = doc_sha256(raw)
    expected = str(req.doc_sha256 or "").strip()
    if expected:
        stale = expected != current_sha
    else:
        # 没带哈希时退一步看已落盘的审核状态：它记着上一次审核针对的那份文档，
        # 对不上就说明文档在这之后被改过。
        stored = load_plan_review_state(plan_dir)
        stale = bool(
            stored is not None
            and stored.doc_sha256
            and stored.doc_sha256 != current_sha
        )
    if stale:
        raise HTTPException(
            409,
            {
                "error": "document_changed",
                "detail": (
                    "计划书在这次审核之后被改过，之前那份审核针对的是另一份文档。"
                    "请重读条目（GET /api/verification-plan/{plan_id}/review/items）"
                    "再操作。"
                ),
                "plan_id": plan_id,
                "item_index": item_index,
                "doc_sha256": current_sha,
            },
        )

    try:
        # ``revise`` 借 ``reset`` 落状态（退回待审），note 由下面补回去。
        state = record_action(
            plan_dir,
            item_index,
            "reset" if action == "revise" else action,
            reason,
        )
    except FileNotFoundError:
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )
    except StructureError:
        raise HTTPException(
            422,
            {
                "error": "verification_plan_invalid",
                "detail": "计划书结构非法，没有可审核的条目",
                "plan_id": plan_id,
            },
        )
    except ValueError as exc:
        # ``record_action`` 自己的校验（动作未知 / 序号不存在 / skip 缺原因）。
        # 上面已经逐条挡过，这里是兜底。
        raise HTTPException(
            400,
            {
                "error": "invalid_review_action",
                "detail": str(exc),
                "plan_id": plan_id,
                "item_index": item_index,
            },
        )

    if action == "revise":
        _write_plan_review_note(plan_dir, state, item_index, reason)

    # 审核完备的那一刻，基线被冻结。冻结不了 = 这次审核不通过（409），
    # 刚落盘的那一条退回待审：审核完备与基线已记录必须同时成立，不能留下
    # 「审核完备却没有基线」的中间态。
    try:
        freeze_baseline_if_resolved(plan_dir)
    except BaselineFreezeError as exc:
        _rollback_plan_review_action(plan_dir, item_index)
        raise HTTPException(
            409,
            {
                "error": exc.code,
                "detail": exc.detail,
                "plan_id": plan_id,
                "item_index": item_index,
                "resolved": False,
            },
        )

    return {
        "items": _verification_plan_review_payload(plan_dir, parsed),
        "doc_sha256": current_sha,
        "resolved": is_resolved(state, raw),
    }


# ---------------------------------------------------------------------------
# 验证分支：审核完备时的基线冻结
# ---------------------------------------------------------------------------
#
# 基线是机器事实，只有一个来源：「被验证对象」章节里那一行 ``baseline:``。
# 它在**审核完备的那次写入**上被冻结：那一刻是唯一确定「这份文档被审过了」
# 的时刻——早一刻还有条目没处置，晚一刻文档就可能被改。
#
# 冻结做三件事：
#
#   1. 重新解析那一行。**不读请求体、不读 query、不读独立文件**——同一份
#      事实只能有一个来源，读别处就等于有第二份基线。
#   2. 把 ``ref`` 解析成 ``commit_sha``（``git rev-parse``）。这一步是文档
#      解析刻意不做的那一步：解析器只做语法与字段校验，不访问 git，因此它
#      能在任何环境重跑；冻结只发生在一个有仓库可用的入口。
#   3. 与任何已记录的基线逐字段比较：不一致就拒绝这次审核通过，旧值不被
#      覆盖；一致（或没有记录）就写进 ``PlanState.verification.baseline``。
#
# 仓库目录不存在、``ref`` 解析不出来，同样阻止审核通过：被验证对象说不清，
# 就不该开始验证。


#: 基线的四个字段。写入与比较都按这四个字段走。
_BASELINE_FIELDS: Tuple[str, ...] = (
    "repo_dir",
    "ref",
    "commit_sha",
    "pr_number",
)


class BaselineFreezeError(RuntimeError):
    """基线冻结失败。

    Attributes:
        code: 机器可读的错误码（``baseline_mismatch`` /
            ``baseline_unresolvable`` / ``baseline_missing``）。
        detail: 给人看的一句话。
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def freeze_baseline_if_resolved(plan_dir: Path) -> Optional[Dict[str, Any]]:
    """审核完备时把计划书里的基线冻结进计划状态。

    审核不完备时什么都不做（返回 ``None``）——基线只在「审核完备的那次写入」
    上被冻结，早一刻冻结的是一份还没被审完的文档。

    Args:
        plan_dir: 计划目录。

    Returns:
        冻结下来的基线（四个字段）；审核不完备、或没有计划书时为 ``None``。

    Raises:
        BaselineFreezeError: 基线冻结不了——
            ``baseline_mismatch``（与已记录的基线逐字段对不上，旧值不被
            覆盖）、``baseline_unresolvable``（仓库目录不存在或 ``ref`` 解析
            不出来）、``baseline_missing``（文档里取不出合法的基线行）。
    """
    plan_dir = Path(plan_dir)
    raw = _verification_plan_bytes(plan_dir)
    if raw is None:
        return None
    current_sha = doc_sha256(raw)

    state = _server.PlanState(plan_dir)
    # 已记录的基线：只有仍对应当前文档的那一份才算数（文档被改写后旧基线
    # 已失效，它不得挡住下一次按新文档的基线行重写）。
    recorded = state.get_verification_baseline()

    if not is_resolved(load_plan_review_state(plan_dir), raw):
        return None

    try:
        parsed = parse_plan_doc(raw.decode("utf-8", errors="replace"))
    except StructureError as exc:
        raise BaselineFreezeError(
            "baseline_missing",
            f"计划书结构非法，取不出合法的基线行：{exc}",
        ) from exc

    doc_baseline = parsed.baseline
    repo_dir = Path(doc_baseline.repo_dir)
    ref = doc_baseline.ref
    try:
        commit_sha = resolve_ref_commit(repo_dir, ref)
    except RefResolutionError as exc:
        raise BaselineFreezeError(
            "baseline_unresolvable",
            f"基线的 ref 解析不出来，审核不通过：{exc}",
        ) from exc

    candidate: Dict[str, Any] = {
        "repo_dir": str(repo_dir),
        "ref": ref,
        "commit_sha": commit_sha,
        "pr_number": doc_baseline.pr_number,
    }

    if recorded is not None:
        mismatched = [
            field
            for field in _BASELINE_FIELDS
            if recorded.get(field) != candidate.get(field)
        ]
        if mismatched:
            raise BaselineFreezeError(
                "baseline_mismatch",
                "已记录的基线与本次冻结的基线不一致（"
                f"字段：{', '.join(mismatched)}），旧值不被覆盖",
            )

    state.set_verification_baseline(candidate, doc_sha256=current_sha)
    return candidate


def _rollback_plan_review_action(plan_dir: Path, item_index: int) -> None:
    """把刚落盘的那一次审核动作退回待审。

    基线冻结失败 = 这次审核不通过：这一条不算数，退回 pending，操作者修好
    仓库或 ref 之后重新提交即可。审核完备与基线已记录必须同时成立——留下
    「审核完备却没有基线」的中间态，计划就会卡在一个谁也推不动的位置。

    回滚是尽力而为：回滚本身失败时只记日志，不掩盖原始的冻结失败。
    """
    try:
        record_action(plan_dir, item_index, "reset", "")
    except Exception as exc:  # noqa: BLE001 - 回滚失败不能盖掉冻结失败
        _server.logger.warning(
            "Verification plan %s: rolling back review item %s failed: %s",
            plan_dir.name,
            item_index,
            exc,
        )


# ---------------------------------------------------------------------------
# 验证分支：计划书的改写入口
# ---------------------------------------------------------------------------
#
# 审核者对计划书的反馈要**改写整篇文档**，不是改一个字段。改写走这个端点：
# ``POST /api/verification-plan/{plan_id}/refine``。
#
# 改写让旧审核失效
# ----------------
# 审核状态与文档**字节**绑定：文档一改，字节就变了，之前那份审核针对的是另
# 一份文档 —— 它既不覆盖新内容，也不再有效。所以改写之后全部条目回到
# ``pending``，一条都不算数。
#
# 基线随之失效
# ------------
# 基线是**为某一份文档**冻结的（它记着自己被冻结时那份文档的字节哈希）。改写
# 之后哈希对不上，旧基线不再有效：它不得继续用于进入验证，验证门禁随之关闭。
# 改写过的计划书必须重新审一遍。
#
# 先校验后落盘
# ------------
# 改写结果不满足十章节契约时 → 422，**旧文档原样保留**。非法文档落到磁盘上只
# 会变成下一个读它的人踩的坑。


def _reset_plan_review_to_pending(
    plan_dir: Path, doc_sha: str, parsed: Any
) -> None:
    """把审核状态整体退回待审，并绑定新文档的哈希。

    文档被改写之后，之前的审核针对的是另一份文档：它既不覆盖新内容，也不再
    有效。所以全部条目回到 ``pending``，原因一并清掉 —— 旧原因说的是旧文档。

    写出的形状与模块内部一致（``ReviewState.to_dict``），读回仍走模块自己的
    ``load``；先写临时文件再替换，避免半截文件。
    """
    items = [
        ReviewItem(index=item.index, id=item.id, title=item.title)
        for item in extract_items(parsed)
    ]
    state = ReviewState(items=items, doc_sha256=doc_sha)
    path = review_path(plan_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


@router.post("/api/verification-plan/{plan_id}/refine")
def refine_verification_plan(plan_id: str, req: _server.RefineRequest = None):
    """按反馈改写验证计划书。

    Args:
        plan_id: 计划 id。
        req: ``{feedback}``。反馈不能为空 —— 没有反馈就没有要改的东西。

    Returns:
        200 ``{"document_path", "doc_sha256", "items"}``。``document_path`` 是
        **相对路径**（``verification-plan.md``），不泄露本机绝对路径；
        ``doc_sha256`` 是改写后那份文档的字节哈希；``items`` 是审核条目 —— 改写
        之后**全部**回到 ``pending``。

    Raises:
        HTTPException: 400 ``feedback_required``（反馈为空）；404
            ``plan_not_found``（计划目录不存在）；404 ``document_not_found``
            （目录在，计划书不在）；422 ``verification_plan_invalid``（改写结果
            不满足十章节契约，旧文档原样保留）；504（改写超时）。
    """
    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(
            404,
            {
                "error": "plan_not_found",
                "detail": "Plan not found",
                "plan_id": plan_id,
            },
        )

    if _verification_plan_bytes(plan_dir) is None:
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )

    feedback = str(getattr(req, "feedback", None) or "").strip()
    if not feedback:
        raise HTTPException(
            400,
            {
                "error": "feedback_required",
                "detail": (
                    "反馈不能为空：没有反馈就没有要改的东西，也不值得花一次模型"
                    "调用。"
                ),
                "plan_id": plan_id,
            },
        )

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    try:
        refine_plan_doc(plan_dir, feedback, coding_tool)
    except PlanDocError as exc:
        # 改写结果不合法：改写器先校验后落盘，所以磁盘上留下的仍是旧文档。
        _server.logger.warning(
            "Verification plan refinement produced an invalid document for "
            "plan %s: %s",
            plan_id,
            exc,
        )
        raise HTTPException(
            422,
            {
                "error": "verification_plan_invalid",
                "detail": (
                    "改写后的验证计划书不满足十章节契约，已丢弃，旧文档原样保留。"
                ),
                "plan_id": plan_id,
                "errors": [
                    {
                        "section": error.section,
                        "line": error.line,
                        "code": error.code,
                        "message": error.message,
                    }
                    for error in exc.errors
                ],
            },
        )
    except FileNotFoundError:
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )
    except TimeoutError as exc:
        _server.logger.warning("Verification plan refinement timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Verification plan refinement timed out",
                "detail": str(exc),
                "retry_after": 60,
            },
        )

    # 文档字节变了 → 旧审核不再覆盖它 → 全部条目回 pending（并绑定新哈希）。
    raw = _verification_plan_bytes(plan_dir)
    if raw is None:  # pragma: no cover — 刚写下的文档不会凭空消失
        raise HTTPException(
            404,
            {
                "error": "document_not_found",
                "detail": "验证计划书不存在",
                "plan_id": plan_id,
            },
        )
    new_sha = doc_sha256(raw)
    try:
        parsed = parse_plan_doc(raw.decode("utf-8", errors="replace"))
    except StructureError as exc:  # pragma: no cover — 落盘前已通过结构校验
        _server.logger.warning(
            "Verification plan %s is invalid right after refinement: %s",
            plan_id,
            exc,
        )
        raise HTTPException(
            422,
            {
                "error": "verification_plan_invalid",
                "detail": "改写后的验证计划书结构非法",
                "plan_id": plan_id,
            },
        )

    _reset_plan_review_to_pending(plan_dir, new_sha, parsed)

    return {
        "document_path": PLAN_DOC_FILENAME,
        "doc_sha256": new_sha,
        "items": _verification_plan_review_payload(plan_dir, parsed),
    }
