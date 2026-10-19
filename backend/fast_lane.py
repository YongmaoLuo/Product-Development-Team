"""Fast lane - upstream material from an external caller into a PDT plan.

The rest of the product reaches ``tasks.json`` by generating it: interview ->
PRD -> architecture -> test design -> ``TasksGenerator``. That is the right
path for a requirement nobody has thought through yet.

This module is the second path, for the caller who arrives with the thinking
already done. The imported material is **an upstream input, not a finished
document**: it is the caller's requirement, their notes, or their own rough
task list, in whatever shape their system already had it in. So the lane
does not try to *parse* that material into ``tasks.json`` -- parsing it
would mean deciding, mechanically and in this file, that a caller's field
named ``name`` is ``title`` and a field named ``files`` is
``files_to_modify``, which is a schema nobody agreed to and a mapping that
silently drops whatever it does not know. Instead the material is written
to ``plans/<plan_id>/import.json`` verbatim and handed to the same high-tier
model that ``tasks_generation`` uses, which produces a task list that
already obeys every PDT constraint. This module keeps only the two things
that survive that change:

1. :func:`store_import_payload` - write any JSON value to ``import.json``
   atomically, so the model reads exactly what the caller sent and a crash
   mid-write cannot leave it half a document;
2. :func:`validate_tasks` - the mechanical audit run **after** generation,
   refusing lists the executor would later choke on, one readable sentence
   per problem.

Invariants this module keeps, because both have a failure mode invisible in
a passing test run:

* **The caller's material is never rewritten.** No field is renamed, no id
  is invented, no key is dropped. What arrives is what is stored, byte for
  byte; the interpretation is the model's job and the review of it is the
  caller's.
* **No re-implementation of the graph rules.** Cycle and dangling-edge
  detection go through :mod:`framework.task_graph` - the same helpers
  ``task_manager._ensure_acyclic`` calls - because a second implementation
  is how the two drift and one of them starts calling a blocked node
  "cyclic" again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from framework.task_graph import find_cycle_groups, find_dangling_references
from task import (
    NO_FILE_CHANGES_SENTINEL,
    UNKNOWN_MODIFICATIONS_SENTINEL,
    SubTask,
)
from utils.atomic_io import atomic_write_json


__all__ = [
    "IMPORT_PAYLOAD_FILENAME",
    "import_payload_summary",
    "store_import_payload",
    "validate_tasks",
]


#: The plan-directory member the imported material is written to, and the
#: only path the import prompt names. Same "path-only" contract the other
#: generators use for ``interview.json`` -- the model reads it itself.
IMPORT_PAYLOAD_FILENAME = "import.json"

#: ``NO_FILE_CHANGES_SENTINEL`` means "read-only by author intent": a code
#: task that claims it is declaring nothing it will change, so it fails.
#: ``UNKNOWN_MODIFICATIONS_SENTINEL`` means "intended a change, never named
#: the files" -- a gap the executor can still work around, and one
#: ``TasksGenerator.harden_for_execution`` already partitions out and
#: reports under ``harden_warnings``. Only the first is a hard problem here,
#: so the audit and the executor agree on which is which.
_HARD_FAIL_SENTINEL = NO_FILE_CHANGES_SENTINEL


def store_import_payload(plan_dir: Path, payload: Any) -> Path:
    """Write ``payload`` to ``plans/<plan_id>/import.json``; return the path.

    ``payload`` is whatever JSON value the caller sent: a string, a bare
    array, an object, a number. No shape is required and none is imposed --
    this function writes what it is handed and nothing else, because the
    model downstream is the thing that reads it, and a parser here would be
    a second opinion about a document nobody has seen yet.

    Atomic (``tempfile`` + ``os.replace``, via :func:`atomic_write_json`)
    with ``reraise=True``: a partial write would leave the model reading a
    truncated document and blaming the payload, so a failure to store has
    to surface rather than be swallowed.
    """
    target = Path(plan_dir) / IMPORT_PAYLOAD_FILENAME
    # ``json.dump`` cannot serialise every object; failing here with the
    # caller's own value in hand beats a half-written file later.
    json.dumps(payload, ensure_ascii=False)
    atomic_write_json(target, payload, reraise=True)
    return target


def import_payload_summary(payload: Any, limit: int = 60) -> str:
    """A short, single-line rendering of ``payload`` for the plan id.

    Only ever a *seed* for ``framework.ids.derive_plan_id``, and only
    consulted when the caller gave no ``title``. It is deliberately lossy:
    the string form of a payload is the one reading every shape has, so a
    string, an array and an object all produce something usable without
    this module having an opinion about their structure.
    """
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(payload)
    text = " ".join(str(text).split())
    return text[:limit] if text else ""


def _one_line(exc: Exception) -> str:
    """Pydantic's complaint as one sentence, without the rendered input.

    ``str(ValidationError)`` interpolates the whole offending dict and a
    documentation URL into the message. Echoing a generated task back to
    the operator inside an error about that task adds noise to the one
    place they are being asked to look, so the structured ``errors()``
    payload is preferred and the rendered form is only the fallback.
    """
    parts: List[str] = []
    structured = getattr(exc, "errors", None)
    if callable(structured):
        try:
            for item in structured() or []:
                location = ".".join(str(piece) for piece in (item.get("loc") or ()))
                message = str(item.get("msg") or "")
                parts.append(f"{location}: {message}" if location else message)
        except Exception:  # noqa: BLE001 - a non-pydantic exception, handled below
            parts = []
    if not parts:
        parts = [" ".join(str(exc).split())]

    cleaned = []
    for part in parts:
        for marker in ("Value error, ", "Assertion failed, "):
            if part.startswith(marker):
                part = part[len(marker):]
        cleaned.append(part.strip())
    return "; ".join(p for p in cleaned if p)


def _declares_concrete_files(files: Sequence[str]) -> bool:
    """True iff ``files`` names at least one real, non-sentinel path.

    A one-element list naming ``__UNKNOWN_MODIFICATIONS__`` is not a
    declaration of anything either, but it is not this function's job to
    refuse it: :meth:`TasksGenerator.harden_for_execution` partitions those
    tasks out and reports them as ``unresolved_files_to_modify``, and a
    hard failure here would refuse a plan the executor will run.
    """
    if not files:
        return False
    listed = list(files)
    if listed == list(UNKNOWN_MODIFICATIONS_SENTINEL):
        return True
    return listed != list(_HARD_FAIL_SENTINEL)


def validate_tasks(
    tasks_data: Any,
    project_dir: Optional[str] = None,
) -> List[str]:
    """Every reason this task list cannot become a plan. Empty = valid.

    The checks, in the order they run:

    * each entry parses as a ``SubTask`` (the model's own field rules and
      its ``deliverable`` / ``report_path`` pairing answer here);
    * ids are unique;
    * a ``code`` task declares real files to modify and a test command;
    * a ``report`` task names its report path;
    * every task points at the same project directory;
    * ``depends_on`` entries resolve, and the graph has no cycle.

    This runs **after** generation, on the model's own output -- it is the
    audit, not the parse. Nothing here maps an incoming field onto a
    ``SubTask`` field: a task that fails to parse is reported, never
    rewritten, because the material it came from is the caller's and the
    interpretation of it is the model's.
    """
    items = tasks_data.get("tasks") if isinstance(tasks_data, Mapping) else tasks_data
    if not isinstance(items, list):
        return ["tasks: expected an object with a 'tasks' list"]

    problems: List[str] = []
    parsed: List[SubTask] = []
    seen_ids: Dict[str, int] = {}

    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            problems.append(
                f"tasks[{index}]: expected an object, got {type(item).__name__}"
            )
            continue
        try:
            subtask = SubTask(**dict(item))
        except Exception as exc:  # noqa: BLE001 - any model failure is a problem to report
            label = item.get("id") or item.get("task_id") or f"tasks[{index}]"
            problems.append(
                f"task {label!r}: is not a valid task ({_one_line(exc)})"
            )
            continue

        parsed.append(subtask)
        label = f"task {subtask.id!r}"

        if subtask.id in seen_ids:
            problems.append(
                f"{label}: duplicate id, already used by "
                f"tasks[{seen_ids[subtask.id]}]"
            )
        else:
            seen_ids[subtask.id] = index

        if subtask.deliverable == "report":
            # Redundant with ``SubTask._validate_report_pair`` while that
            # validator exists, and kept on purpose: this is the gate the
            # fast lane promises its caller, and a relaxation of the model
            # must not silently hand the executor a report task it can
            # never complete.
            if not (subtask.report_path or "").strip():
                problems.append(
                    f"{label}: deliverable='report' needs a non-empty report_path"
                )
        else:
            if not _declares_concrete_files(subtask.files_to_modify):
                problems.append(
                    f"{label}: deliverable='code' needs files_to_modify naming "
                    "at least one file the task will change"
                )
            if not subtask.get_test_commands():
                problems.append(
                    f"{label}: deliverable='code' needs a test_command (or "
                    "test_commands) so the change can be verified"
                )

        if project_dir and subtask.project_dir and subtask.project_dir != project_dir:
            problems.append(
                f"{label}: project_dir {subtask.project_dir!r} differs from the "
                f"request's {project_dir!r}; every task must target one checkout"
            )

    # Graph rules come after the per-task ones so the first sentence the
    # caller reads is about the task they wrote, not about an edge in it.
    for task_id, missing in find_dangling_references(parsed):
        problems.append(
            f"task {task_id!r}: depends_on names {missing!r}, which is not a "
            "task in this list"
        )
    for group in find_cycle_groups(parsed):
        problems.append(
            "dependency cycle between tasks: " + ", ".join(sorted(group))
        )
    return problems
