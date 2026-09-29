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
import json
import re
import sqlite3

router = APIRouter()


import server as _server  # noqa: E402  (late binding — see the module docstring)


@router.post("/api/interview/start")
def start_interview(req: _server.StartInterviewRequest):
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
