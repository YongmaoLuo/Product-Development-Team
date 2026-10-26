"""Fast lane HTTP route: import external upstream material as a new plan.

``POST /api/plan/import`` is the short way in. The caller already did the
research and already has *something* — a requirement, notes, a rough task
list — in whatever shape their own system produced it. This writes that
material into the plan directory untouched, asks the same high-tier model
``POST /api/tasks/{plan_id}/generate`` uses to turn it into a
``SubTask`` list, audits the answer, and then **stops**. There is no
``/start`` call here and nothing queued: the plan lands in ``ready``,
which is the phase the product defines as "tasks exist, a human decides
when to run them" (``plan_state.VALID_PHASES``).

Why generation rather than normalisation
----------------------------------------
The earlier version of this route mapped the caller's fields onto
``SubTask`` fields itself — ``name`` → ``title``, ``files`` →
``files_to_modify``, and so on. That made this file a second, private
schema that no caller had agreed to: a field the mapping did not know was
dropped without a word, and the imported task list was a *finished
document* that happened to be shaped like the executor's. The imported
list is the same kind of thing PRD and architecture design are — an
upstream input whose format the caller owns — so it gets the same
treatment they do: a path the model reads, and a model that produces
something that already obeys PDT's constraints. What is left in
``fast_lane.py`` is the audit, not the parser.

Why the file exists rather than another handler in ``routes/plans.py``
------------------------------------------------------------------------
``routes/plans.py`` reads plans; every route it owns is a ``GET``. The
one route here that WRITES a plan would have made that module's stated
scope ("plan inventory: listing, status, summary, usage, task rows") a
lie, and the module docstring is the only thing telling the next reader
which file owns which phase.

Everything it touches is an existing, already-owned mechanism:

* ``framework.ids.derive_plan_id`` - the repository's one plan-id rule
  (``<YYYYMMDD>-<slug>``). Not re-derived here.
* ``TasksGenerator`` in import mode - the same model, gates and self-review
  as every generated plan.
* ``TaskManager.save_tasks`` - the writer every other task-producing path
  uses, including its cycle guard.
* ``_server._seed_plan_routing_phase`` + ``_server.PlanState`` - the
  plan-state store. Fast lane writes one phase and reuses the same
  ``ready`` vocabulary the generated path lands on.

The request model lives here rather than in ``server.py`` because it is
this feature's contract alone; ``server`` keeps the models the pre-existing
routes share, and adding a fourth import site for one route's body would
have made the diff reach a file the fast-lane change does not need to
touch.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import HTTPException
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json

from pydantic import BaseModel

from fast_lane import (
    import_payload_summary,
    read_import_session,
    store_import_payload,
    store_import_session,
    validate_tasks,
)
from coding_tool import JsonSession
from framework.ids import derive_plan_id
from framework.task_graph import DanglingTaskDependency
from hooks.config import write_for_plan
from task import SubTask
from task_manager import CycleInTaskGraph, TaskManager
from tasks_generator import TasksGenerationError, TasksGenerator

# Late binding into the application module: ``server`` owns the shared
# helpers and module globals, and the suite monkeypatches them as
# ``server.<name>``. See ``routes/phases.py`` for the rule.
import server as _server


router = APIRouter()


class ImportPlanRequest(BaseModel):
    """Body of ``POST /api/plan/import``.

    ``project_dir`` is required and ``content`` is declared as ``Any`` on
    purpose. The imported material is an upstream input the caller owns, so
    nothing about its shape is this route's business: a string, a bare
    array, an object with keys nobody here has heard of — all three are
    accepted and all three are stored verbatim. The three aliases are the
    same slot under different names, because which one a caller reaches for
    is a habit, not a requirement; exactly one of them must be present.
    """

    project_dir: Optional[str] = None
    title: Optional[str] = None
    #: ``content`` is the documented spelling. ``tasks`` / ``raw`` /
    #: ``requirement`` are accepted aliases for the same slot — the last
    #: because it is what the field was called before the lane stopped
    #: parsing a task list, and keeping it costs one line.
    content: Any = None
    tasks: Any = None
    raw: Any = None
    requirement: Any = None
    #: Where a human-in-the-loop question about this plan is delivered, and
    #: the session the plan was created in.  ``hook_destination`` is the
    #: cc-connect project the question is delivered to and
    #: ``hook_session_id`` is the cc-connect session key inside it — the
    #: two values the delivery command is given, and the whole of what
    #: this body decides about the delivery.  Both are optional, and a
    #: caller that sends neither is a caller with no session to ask —
    #: which is the normal case for a ``curl``.  See
    #: :func:`bind_hook_for_plan`.
    hook_destination: Optional[str] = None
    hook_session_id: Optional[str] = None


class RevisePlanRequest(BaseModel):
    """Body of ``POST /api/plan/{plan_id}/revise``.

    ``feedback`` is the caller's note about the task list as a whole —
    the reason this door exists. It is required and must not be empty:
    a revision with nothing to say is not a revision, and treating it as
    one would ask the model to redo the list for no reason and charge
    the caller for the answer.

    ``content`` optionally replaces the imported material, so a caller
    who also has new upstream material hands it over in the same round
    trip. It is stored verbatim, exactly as the import stores its own.
    """

    feedback: Optional[str] = None
    content: Any = None


#: Order in which the aliases are consulted. First one that is not ``None``
#: wins, so a caller sending both ``content`` and ``raw`` gets ``content``.
_CONTENT_ALIASES = ("content", "tasks", "raw", "requirement")


def _payload_of(req: ImportPlanRequest) -> Tuple[Any, str]:
    """``(payload, field_name)`` for the alias the caller actually used.

    ``None`` is the "not supplied" answer, which is why the aliases are
    read positionally rather than through a default: a caller who
    explicitly sends ``content: null`` has said nothing to import, and a
    caller who sends a list has said something.
    """
    for name in _CONTENT_ALIASES:
        value = getattr(req, name, None)
        if value is not None:
            return value, name
    return None, ""


def _seed_text(title: Optional[str], payload: Any) -> str:
    """The free text the plan id is derived from.

    ``title`` wins because that is the short label the caller typed.
    Without one, the payload's own string form is used: it is the one
    rendering every shape has, so a caller who sent a bare array still
    gets a readable id. Neither is required — a caller who sent something
    with no printable content at all still gets a valid, unique plan id
    from the last-resort slug.
    """
    return (title or "").strip() or import_payload_summary(payload)


def _allocate_plan_id(seed: str) -> Tuple[str, Optional[str]]:
    """A free ``<YYYYMMDD>-<slug>`` id, and a warning when it had to move.

    Re-importing the same title on the same day derives the same id, and
    the existing directory holds a plan somebody may already have
    started. Appending ``-2`` keeps the derivation rule (still a valid
    leaf for ``framework.ids.validate_plan_id``) and refuses to overwrite
    anything; the caller is told which directory it actually got.
    """
    base = derive_plan_id(seed, max_len=20)
    plan_id = base
    suffix = 2
    while _server._plan_dir(plan_id).exists():
        plan_id = f"{base}-{suffix}"
        suffix += 1
    warning = None
    if plan_id != base:
        warning = (
            f"a plan directory named {base} already exists; "
            f"this import was written to {plan_id}"
        )
    return plan_id, warning


def _persist_tasks(plan_dir: Path, requirement: str, tasks_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Write ``plans/<id>/tasks.json`` through the existing writer.

    ``TaskManager``'s first argument is the plan directory, not the
    checkout the tasks modify - the tasks carry that in their own
    ``project_dir`` field. Using the class rather than a hand-rolled
    ``json.dump`` is what keeps the envelope (``requirement`` /
    ``stop_reason`` / ``reason_detail`` / ``tasks``), the atomic replace
    and the pre-write cycle guard identical to the generated path.

    The response payload is read back off disk rather than rebuilt from
    the in-memory models, because ``save_tasks`` strips the runtime
    fields on its way out; rebuilding that strip list here would be a
    second copy of a rule that already lives in the writer.
    """
    manager = TaskManager(plan_dir)
    manager.requirement = requirement
    manager.tasks = [SubTask(**item) for item in tasks_data.get("tasks", [])]
    manager.save_tasks()
    saved = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    return list(saved.get("tasks", []))


def _mark_ready(plan_id: str, plan_dir: Path) -> None:
    """Put the new plan in ``ready``, through the existing state store.

    ``_seed_plan_routing_phase`` is the idempotent ``INSERT OR IGNORE``
    the interview path uses; ``transition_to`` then resolves the phase
    through the same legality check every other transition goes through
    and is a no-op when the seed already landed. If SQLite is
    unavailable both fall back to the on-disk inference, which reads
    ``tasks.json``'s existence as ``ready`` - the same answer.
    """
    _server._seed_plan_routing_phase(plan_id, phase="ready")
    try:
        _server.PlanState(plan_dir).transition_to("ready")
    except ValueError as exc:  # noqa: BLE001 - surfaced, never swallowed
        raise HTTPException(
            500,
            {
                "error": "Plan imported but its state could not be set to ready",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )


def bind_hook_for_plan(
    plan_dir: Path,
    destination: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record who a human-in-the-loop question about this plan goes to.

    *Who* is asked is a property of the plan, not of the backend: a plan
    created from a Claude Code session asks the group that session lives
    in, and a plan created by a bare ``curl`` has no session and
    therefore nobody to ask.  So the origin is stored next to the plan,
    in ``<plan_dir>/hitl_config.json``.

    The reader and the writer of that file both live in
    ``hooks.config`` and nowhere else; this function is only the caller
    that hands the origin over, and it is shared by both plan-creation
    doors — this lane and the interview in ``routes/phases.py``.

    **How** the question is delivered is decided by the same writer, and
    it is a command rather than a description of a transport.  The hook
    this origin implies is a ``cmd`` hook: the command runs
    ``backend.hooks.session_send`` with the plan's destination and its
    session as its arguments, and that module is what turns a
    cc-connect project and a cc-connect session key into a delivered
    message.  So ``destination`` is the cc-connect project the question
    goes to and ``session_id`` is the cc-connect session key inside it —
    the two values the command is given, and nothing else about the
    delivery is decided here.

    A plan created without a session gets an **empty** hook list, not an
    error: that is the documented degradation, and it is written down
    rather than left implicit, so a later reader can tell "nobody was
    asked" from "nobody ever wrote this down".

    The write is **not** allowed to fail the creation.  A plan whose
    hook configuration cannot be persisted is still a plan, and a plan
    with no hooks is a state a plan already knows how to be in — its
    decisions are simply unroutable, which notifies nobody and blocks
    nothing.  A failed plan creation, on the other hand, is a plan that
    does not exist.  The failure is therefore logged loudly and the
    creation continues.
    """
    origin = {
        # An empty source is how ``hooks.config`` spells "this plan did
        # not come from a session"; a hook needs a session to have come
        # from as well as a place to be delivered to.
        "source": "plan_creation" if (session_id or "").strip() else "",
        "session_id": (session_id or "").strip(),
        "destination": (destination or "").strip(),
    }
    try:
        return write_for_plan(plan_dir, origin)
    except Exception as exc:  # noqa: BLE001 - logged, never swallowed silently
        _server.logger.error(
            "hitl_hook_binding_failed: cannot bind the hook of plan %s (%s); "
            "the plan keeps no hook configuration",
            plan_dir,
            type(exc).__name__,
        )
        return {}


@router.post("/api/plan/import")
def import_plan(req: ImportPlanRequest):
    """Import external upstream material as a new plan, parked in ``ready``.

    Skips interview / PRD / architecture / test-design generation — the
    caller's material stands in for all four. It does not skip validation:
    the generated list is audited, and one that would break at dispatch
    time is refused with one sentence per problem, without the plan ever
    reaching ``ready``.
    """
    project_dir = (req.project_dir or "").strip()
    if not project_dir:
        raise HTTPException(
            400,
            {
                "error": "Invalid import request",
                "problems": [
                    "project_dir is required: give the absolute path of the "
                    "checkout these tasks will modify"
                ],
            },
        )

    payload, payload_field = _payload_of(req)
    if payload is None:
        raise HTTPException(
            400,
            {
                "error": "Invalid import request",
                "problems": [
                    "content is required: send the upstream material under "
                    "'content' (aliases: tasks / raw / requirement); any JSON "
                    "value is accepted"
                ],
            },
        )

    seed = _seed_text(req.title, payload)
    if not seed:
        seed = "fast-lane-import"
    plan_id, id_warning = _allocate_plan_id(seed)
    plan_dir = _server._plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)

    # Stored before the model is asked anything: import.json is what the
    # prompt names, and the model reads it from disk like every other
    # upstream doc in this repository (see the v15 path-only contract).
    store_import_payload(plan_dir, payload)

    warnings: List[str] = [id_warning] if id_warning else []

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    # ``self_review_enabled`` is DP1's default (``True``) rather than a
    # read from the state store, and that is not a shortcut: ``_allocate_plan_id``
    # hands back a directory that did not exist a line ago, so there is
    # no prior state to read. Constructing a ``PlanState`` to ask would
    # *create* the routing row this route is supposed to seed at ``ready``
    # later, at whatever phase an empty directory infers (``interview``),
    # and ``_seed_plan_routing_phase`` is INSERT OR IGNORE — so the read
    # would make the write a no-op and strand the plan in ``interview``.
    generator = TasksGenerator(
        coding_tool, plan_dir,
        self_review_enabled=True,
        import_mode=True,
        import_project_dir=project_dir,
    )
    # The conversation this import runs in. Created here rather than
    # inside the generator because the lane has to remember it: a later
    # revision of this plan is a follow-up question in the same
    # conversation, not a fresh interview of the same material.
    session = JsonSession()
    try:
        tasks_data = generator.generate(session=session)
    except TimeoutError as exc:
        _server.logger.warning("Fast lane import timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Fast lane import timed out",
                "detail": str(exc),
                "plan_id": plan_id,
                "retry_after": 60,
            },
        )
    except TasksGenerationError as exc:
        # Same mapping as POST /api/tasks/{plan_id}/generate: the LLM
        # mis-fired (0 tasks) or a post-generation gate rejected the list.
        # The generator already wrote the payload to tasks.json; surface it
        # so the caller can decide whether to retry.
        _server.logger.warning(
            "Fast lane import generation failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            422,
            {
                "error": "Tasks generation failed",
                "detail": str(exc),
                "error_payload": exc.error_payload,
                "plan_id": plan_id,
                "source_field": payload_field,
                "retry_hint": (
                    "Re-run POST /api/plan/import with the same material; "
                    "the LLM call will be retried against the same "
                    "import.json."
                ),
            },
        )

    problems = validate_tasks(tasks_data, project_dir)
    if problems:
        raise HTTPException(
            422,
            {
                "error": "Generated task list is not a runnable plan",
                "problems": problems,
                "plan_id": plan_id,
            },
        )

    # The two gates the generated path runs before it will let a plan be
    # started. Reused rather than reimplemented: they answer "can the
    # executor run this" against each task's own project_dir, which is a
    # rule this route has no copy of and must not grow one.
    try:
        tasks_data = generator.validate_workspace(
            tasks_data, search_dirs=None,
            formal_repo_dir=_server._PROJECT_ROOT,
        )
        tasks_data = generator.harden_for_execution(tasks_data)
    except TasksGenerationError as exc:
        _server.logger.warning(
            "Fast lane import hardening failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            422,
            {
                "error": "Generated tasks are not runnable",
                "detail": str(exc),
                "error_payload": exc.error_payload,
                "plan_id": plan_id,
            },
        )

    warnings.extend(_harden_warnings(tasks_data))

    written = _persist_tasks(plan_dir, seed, tasks_data)
    _mark_ready(plan_id, plan_dir)
    # The plan's hook binding: the session it was created in, and where a
    # human-in-the-loop question about it goes.  Recorded once the plan
    # exists — a plan that never reached ``ready`` is not a plan, and
    # has nobody to ask.
    bind_hook_for_plan(plan_dir, req.hook_destination, req.hook_session_id)
    # Remember the conversation the import ran in, so a later revision
    # of this plan resumes it instead of starting over. Written last:
    # a plan that never reached ``ready`` has nothing to revise.
    store_import_session(plan_dir, session.session_id)

    return {
        "plan_id": plan_id,
        "source_field": payload_field,
        "tasks": written,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# The lane's second door: revise an imported plan's whole task list
# ---------------------------------------------------------------------------


def _read_tasks_file(tasks_file: Path) -> Dict[str, Any]:
    """The plan's stored task list, as the revision's starting point.

    Read defensively: a file that is not a JSON object has no list to
    revise, and the caller is told the same thing a plan without a
    ``tasks.json`` is told — there is nothing here to revise.
    """
    try:
        data = json.loads(tasks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            409,
            {
                "error": "this plan has no tasks to revise",
                "detail": str(exc),
            },
        )
    if not isinstance(data, dict):
        raise HTTPException(
            409,
            {"error": "this plan has no tasks to revise"},
        )
    return data


def _plan_project_dir(tasks_data: Dict[str, Any]) -> str:
    """The checkout this plan's tasks target, read off the stored list.

    The revise request carries no ``project_dir``: the plan already has
    one, and a revision that retargeted the plan at a different checkout
    would be a change nobody asked for. The audit below then checks the
    revised list against exactly this directory, so a task that points
    somewhere else is reported rather than quietly accepted.
    """
    tasks = tasks_data.get("tasks")
    if not isinstance(tasks, list):
        return ""
    for task in tasks:
        if isinstance(task, dict):
            own = str(task.get("project_dir") or "").strip()
            if own:
                return own
    return ""


def _persist_revised_tasks(
    plan_dir: Path,
    tasks_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Write the revised list back through the writer the import used.

    ``TaskManager`` is built on the plan directory, so it loads the
    envelope the plan already has — ``requirement`` / ``stop_reason`` /
    ``reason_detail`` — and only the task list is replaced. The caller
    gave feedback about the tasks, not about the plan's identity, so
    the requirement stays what the import wrote.

    The response payload is read back off disk rather than rebuilt from
    the in-memory models, for the same reason the import route reads it
    back: ``save_tasks`` strips the runtime fields on its way out, and
    rebuilding that strip list here would be a second copy of a rule
    that already lives in the writer.
    """
    manager = TaskManager(plan_dir)
    manager.tasks = [SubTask(**item) for item in tasks_data.get("tasks", [])]
    manager.save_tasks()
    saved = json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))
    return list(saved.get("tasks", []))


@router.post("/api/plan/{plan_id}/revise")
def revise_plan(plan_id: str, req: RevisePlanRequest):
    """Revise an imported plan's whole task list, in place.

    The lane's second door. ``POST /api/plan/import`` is the way in;
    this is the way to say "not quite" about what came out. The plan
    keeps its directory, its phase and its execution state — only the
    task list is replaced, and only after the replacement passes the
    same audit the import applied. A rejected revision leaves the plan
    exactly as it was: the generator's writes are deferred until the
    result has been audited and accepted.
    """
    feedback = (req.feedback or "").strip()
    if not feedback:
        raise HTTPException(
            400,
            {
                "error": "Invalid revise request",
                "problems": [
                    "feedback is required: send a non-empty note about the "
                    "task list you want changed"
                ],
            },
        )

    plan_dir = _server._plan_dir(plan_id)
    if not plan_dir.exists():
        raise HTTPException(
            404,
            {"error": "Plan not found", "plan_id": plan_id},
        )

    tasks_file = plan_dir / "tasks.json"
    if not tasks_file.exists():
        raise HTTPException(
            409,
            {
                "error": "this plan has no tasks to revise",
                "plan_id": plan_id,
            },
        )

    # New upstream material, if the caller sent any: the revision is
    # audited against the same ``import.json`` the import was, so it has
    # to be replaced before the model is asked.
    if req.content is not None:
        store_import_payload(plan_dir, req.content)

    existing = _read_tasks_file(tasks_file)
    project_dir = _plan_project_dir(existing)

    # The conversation the import ran in, if there was one. A revision
    # in the same session is a follow-up question about a list the model
    # has already seen; a revision in a new one is a fresh interview
    # that has to be told what the list looks like.
    session_id = read_import_session(plan_dir)
    session = JsonSession(session_id)
    session_reused = bool(session_id)

    coding_tool = _server.create_coding_tool(scene="tasks_generation")
    generator = TasksGenerator(
        coding_tool, plan_dir,
        self_review_enabled=True,
        import_mode=True,
        import_project_dir=project_dir,
    )
    try:
        tasks_data = generator.revise_from_feedback(session, feedback)
    except TimeoutError as exc:
        _server.logger.warning("Fast lane revise timed out: %s", exc)
        raise HTTPException(
            504,
            {
                "error": "Fast lane revise timed out",
                "detail": str(exc),
                "plan_id": plan_id,
                "retry_after": 60,
            },
        )
    except TasksGenerationError as exc:
        # Same mapping as the import: the model mis-fired (0 tasks) or a
        # post-generation gate rejected the list. The plan's own list is
        # untouched — the generator's writes are deferred.
        _server.logger.warning(
            "Fast lane revise generation failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            422,
            {
                "error": "Tasks revision failed",
                "detail": str(exc),
                "error_payload": exc.error_payload,
                "plan_id": plan_id,
                "retry_hint": (
                    "Re-run POST /api/plan/{plan_id}/revise with the same "
                    "feedback; the plan's task list is unchanged."
                ),
            },
        )
    except RuntimeError as exc:
        # The mandatory self-review could not produce an applicable
        # rewrite. Same class of failure as above, and the same
        # consequence: the plan still runs the list it ran before.
        _server.logger.warning(
            "Fast lane revise self-review failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            422,
            {
                "error": "Tasks revision failed",
                "detail": str(exc),
                "plan_id": plan_id,
            },
        )

    problems = validate_tasks(tasks_data, project_dir)
    if problems:
        raise HTTPException(
            400,
            {
                "error": "Revised task list is not a runnable plan",
                "problems": problems,
                "plan_id": plan_id,
            },
        )

    # The two gates the import route runs, on the same terms: the
    # revised list has to be as runnable as an imported one, or the
    # caller is told why instead of finding out at dispatch time.
    try:
        tasks_data = generator.validate_workspace(
            tasks_data, search_dirs=None,
            formal_repo_dir=_server._PROJECT_ROOT,
        )
        tasks_data = generator.harden_for_execution(tasks_data)
    except TasksGenerationError as exc:
        _server.logger.warning(
            "Fast lane revise hardening failed for plan %s: %s", plan_id, exc
        )
        raise HTTPException(
            400,
            {
                "error": "Revised tasks are not runnable",
                "detail": str(exc),
                "problems": [str(exc)],
                "plan_id": plan_id,
            },
        )

    try:
        written = _persist_revised_tasks(plan_dir, tasks_data)
    except (CycleInTaskGraph, DanglingTaskDependency) as exc:
        # The writer's own graph guard, which the audit above already
        # applies — a disagreement between the two is a bug, and the
        # plan's list is still the old one either way.
        _server.logger.warning(
            "Fast lane revise refused by the task graph guard for plan %s: %s",
            plan_id, exc,
        )
        raise HTTPException(
            400,
            {
                "error": "Revised task list is not a runnable plan",
                "problems": [str(exc)],
                "plan_id": plan_id,
            },
        )

    # The conversation may have moved (a provider fallback can switch
    # sessions mid-answer), so what is remembered is the id the last
    # answer actually came from.
    store_import_session(plan_dir, session.session_id)

    return {
        "plan_id": plan_id,
        "tasks": written,
        "revised": True,
        "session_reused": session_reused,
    }


def _harden_warnings(tasks_data: Dict[str, Any]) -> List[str]:
    """Render ``harden_for_execution``'s warnings as readable sentences.

    Its two entries are the two things this lane refuses to call failures:
    a task whose ``files_to_modify`` is the "intended a change, never named
    the files" sentinel, and a task with no resolvable ``project_dir``.
    Both are runnable; neither is something the caller can discover from
    the response alone.
    """
    harden = tasks_data.get("harden_warnings")
    if not isinstance(harden, dict):
        return []
    out: List[str] = []
    for task_id in harden.get("unresolved_files_to_modify") or []:
        out.append(
            f"task {task_id!r}: files_to_modify was never resolved; the "
            "executor will decide which files to change"
        )
    for task_id in harden.get("tasks_without_project_dir") or []:
        out.append(
            f"task {task_id!r}: no project_dir could be resolved; the run's "
            "working directory will be used"
        )
    return out
