"""Durable pause checkpoints — the on-disk half of ``stop``.

Why this module exists
----------------------
``stop`` used to change memory and nothing else. A plan was stopped, the
executor dropped the task handles it held, and the backend was left with
a plan directory that looked exactly like one that had never run: no
record that task 3 was in flight, no record of the session id a resume
could re-open, no record of *which kind* of interruption happened. Restart
the backend and that knowledge is gone — there is no in-memory registry
to carry it, because the object that held it died with the process.

This module is the file layer that survives that restart. One JSON
document per plan directory, written atomically at stop time and read
once at resume time. It knows nothing about executors, schedulers or
routes: it moves a dict to disk and back, and that is the whole job —
which is also what makes it the piece that can be tested without a plan.

The document
------------
::

    {
      "version": 1,
      "kind": "execution",              # what was interrupted
      "project_dir": "/w/app",          # where the work was happening
      "paused_at": "2026-10-20T12:00:00+00:00",
      "reason": "user_requested",       # why the stop happened
      "interrupted_tasks": [            # tasks caught mid-flight
        {"task_id": "3", "status": "in_progress", "phase": "execution",
         "session_id": "s-1", "paused_at": "...", "strategy": "session_resume",
         "reason": "session_id_available"}
      ],
      "unfinished_tasks": [             # tasks never reached
        {"task_id": "3", "status": "in_progress"}
      ],
      "verification_round": null,       # set when the stop happened mid-review
      "decision_id": "d1"               # optional: the HITL decision that
                                        # caused the pause (absent when the
                                        # pause had no decision behind it)
    }

Both task lists are normalised to a **fixed key set** on the way in. That
is not tidiness for its own sake: a resume path that trusts
``session_id`` must be able to assume the key is present (and
``None`` when absent) rather than discover its absence with a
``KeyError`` at the worst possible moment. A caller that smuggles an
extra key into an entry cannot smuggle it past here, so the on-disk
contract stays the one this module documents.

``decision_id`` is the one **optional** top-level key: it is written
only when the caller supplies it, and is left out entirely otherwise.
A key that is present-and-null says "there was a decision and we lost
it", which is a different claim from the absent key's "no decision was
involved" — and only the second one is true of most pauses.

Degradation is the default, never an exception
----------------------------------------------
Every read here answers ``None`` rather than raising:

* no checkpoint (a plan that was never stopped) → ``None``;
* a truncated, non-JSON, or otherwise torn file → ``None``, logged;
* a file written by a newer schema version → ``None``, because the safe
  direction there is "resume from scratch" rather than "resume from a
  document this code does not understand".

``None`` means the same thing in all three cases — *there is no
checkpoint to honour* — so the caller needs no ``try``/``except`` and no
branching. That is the design: a resume that cannot find a checkpoint
has to be reachable, because it is the path that runs after a crash.

Writes go through :func:`utils.atomic_io.atomic_write_json`, so a stop
interrupted by a kill leaves either the previous checkpoint or the new
one, never a half-written file — and a delete that fails is silent
because a checkpoint that cannot be removed costs one wasted resume,
while a resume path that raised on removal would cost the whole run.

The second half: the session mapping
------------------------------------
The checkpoint above is written *at stop time*, so it can only record a
session that was already bound when the stop happened. The binding
itself is written by the executor, as it runs, into the execution log
(one ``task_session_bound`` event per attempt, session id under
``data``) — and that log is the only durable record of it.

:func:`collect_task_sessions` is the reader for it: it turns the live
log plus its rotated copies into ``{task_id: session_id}``, latest
binding wins, malformed lines skipped, missing log an empty mapping.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from execution_logger import ExecutionLogger
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Name of the per-plan checkpoint file.
PAUSE_CHECKPOINT_FILENAME = "pause_state.json"

#: Bumped only on an incompatible change to the document shape. A
#: checkpoint written by a newer PDT is treated as unreadable by an older
#: one, which costs a resume from scratch rather than a wrong resume.
_SCHEMA_VERSION = 1

#: Exact key set of an ``interrupted_tasks`` entry, in write order.
_INTERRUPTED_TASK_FIELDS = (
    "task_id",
    "status",
    "phase",
    "session_id",
    "paused_at",
    "strategy",
    "reason",
)

#: Exact key set of an ``unfinished_tasks`` entry, in write order.
_UNFINISHED_TASK_FIELDS = ("task_id", "status")

#: Optional top-level key naming the HITL decision that caused the pause.
#: Written only when the caller supplies one, and left out entirely
#: otherwise — see the module docstring for why "absent" and "null" are
#: not the same statement.
_DECISION_ID_FIELD = "decision_id"

#: The log event that carries a task→session binding. The executor
#: writes one per attempt, with the session id under ``data``.
_SESSION_BOUND_EVENT = "task_session_bound"


def checkpoint_path(plan_dir: PathLike) -> Path:
    """Where ``plan_dir``'s checkpoint lives."""
    return Path(plan_dir) / PAUSE_CHECKPOINT_FILENAME


def _now_iso() -> str:
    """A timezone-aware ISO-8601 stamp, as written by this module.

    ``datetime.now(timezone.utc)`` rather than the older ``utcnow()`` the
    rest of the tree still uses: same wall clock, no deprecation warning,
    and the offset is explicit in the stored value.
    """
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> Optional[str]:
    """Normalise a scalar checkpoint field to a string (or ``None``)."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _normalise_interrupted(
    entries: Iterable[Any], default_paused_at: str
) -> List[Dict[str, Any]]:
    """Coerce ``entries`` to the fixed interrupted-task shape.

    An entry that omits ``paused_at`` inherits the checkpoint's stamp
    rather than losing it — the resume path needs to know when the task
    was caught, and the checkpoint is a strictly better answer than
    ``None``. Anything that is not a mapping yields a row of ``None``s,
    so the list length still reflects what the caller passed.
    """
    normalised: List[Dict[str, Any]] = []
    for entry in entries or ():
        source = entry if isinstance(entry, dict) else {}
        row = {field: source.get(field) for field in _INTERRUPTED_TASK_FIELDS}
        if not row.get("paused_at"):
            row["paused_at"] = default_paused_at
        normalised.append(row)
    return normalised


def _normalise_unfinished(entries: Iterable[Any]) -> List[Dict[str, Any]]:
    """Coerce ``entries`` to the fixed unfinished-task shape."""
    return [
        {
            field: (entry if isinstance(entry, dict) else {}).get(field)
            for field in _UNFINISHED_TASK_FIELDS
        }
        for entry in entries or ()
    ]


def write_pause_checkpoint(
    plan_dir: PathLike,
    *,
    kind: str,
    project_dir: Optional[str] = None,
    reason: Optional[str] = None,
    interrupted_tasks: Optional[Iterable[Any]] = None,
    unfinished_tasks: Optional[Iterable[Any]] = None,
    verification_round: Optional[Any] = None,
    decision_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record what ``stop`` interrupted, and return what was written.

    The return value is the stored document itself, so a caller can log
    or assert on exactly the fields a later read will see rather than
    on what it hoped it passed.

    ``plan_dir`` is created if it does not exist — a plan being stopped
    is normally expected to have a directory, but "the directory is not
    there" is not a reason to lose the interruption record, and the
    failure would otherwise land as a confusing error much later, during
    the resume this file exists to enable.

    ``decision_id`` names the HITL decision the pause was waiting on —
    a strict-mode verification that hit its deadline passes the id of
    the decision that timed out, so the resume path can find that
    decision again instead of guessing which one it was. It is
    **optional**: a pause with no decision behind it (a user-requested
    stop, a crash) writes no such key at all, rather than a null one.

    The write is atomic (tempfile + ``os.replace``), so a stop killed
    mid-write leaves the previous checkpoint intact rather than a torn
    one. A write that still fails is logged and swallowed by
    :func:`atomic_write_json`: a checkpoint is a resume optimisation,
    and losing it must not take down the stop that was succeeding.
    """
    paused_at = _now_iso()
    checkpoint: Dict[str, Any] = {
        "version": _SCHEMA_VERSION,
        "kind": _text(kind),
        "project_dir": _text(project_dir),
        "paused_at": paused_at,
        "reason": _text(reason),
        "interrupted_tasks": _normalise_interrupted(
            interrupted_tasks, paused_at
        ),
        "unfinished_tasks": _normalise_unfinished(unfinished_tasks),
        "verification_round": verification_round,
    }
    # Written only when the caller names a decision. An absent key and a
    # null one are different statements, and only the absent one is true
    # of a pause that had no decision behind it.
    if decision_id is not None:
        checkpoint[_DECISION_ID_FIELD] = _text(decision_id)

    path = checkpoint_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "pause_state_dir_uncreatable: cannot create %s (%s); "
            "the checkpoint will not be written",
            path.parent, type(exc).__name__,
        )
        return checkpoint

    atomic_write_json(path, checkpoint, logger=logger)
    return checkpoint


def read_pause_checkpoint(plan_dir: PathLike) -> Optional[Dict[str, Any]]:
    """The stored checkpoint, or ``None`` if there is nothing to honour.

    ``None`` covers every unusable case — absent, unreadable, not JSON,
    JSON but not an object, or a schema version this code does not know.
    Callers therefore have one "resume from scratch" answer to handle and
    no exception to catch.
    """
    path = checkpoint_path(plan_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError``. A checkpoint
        # torn by a crash mid-write, or written by something else, costs
        # a resume from scratch and nothing else.
        logger.warning(
            "pause_state_unreadable: ignoring unreadable checkpoint %s (%s); "
            "resuming from scratch",
            path, type(exc).__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != _SCHEMA_VERSION:
        # A checkpoint whose shape is not the one this code wrote.
        # ``_SCHEMA_VERSION`` has only ever been 1, so in practice this is
        # a *newer* PDT's file, and "no checkpoint" costs a full resume
        # rather than a misread one.
        logger.warning(
            "pause_state_version_mismatch: ignoring checkpoint %s with "
            "version %r; resuming from scratch",
            path, data.get("version"),
        )
        return None
    if not isinstance(data.get("interrupted_tasks"), list):
        data["interrupted_tasks"] = []
    if not isinstance(data.get("unfinished_tasks"), list):
        data["unfinished_tasks"] = []
    return data


def consume_pause_checkpoint(plan_dir: PathLike) -> Optional[Dict[str, Any]]:
    """Read the checkpoint, then delete it — at most once per stop.

    Delete-after-read is what makes a resume single-shot: without it, a
    restart loop would re-consume the same interruption on every attempt
    and a plan would never actually move past it.

    The delete happens only for a checkpoint that was **successfully
    read**. A corrupt file is left alone: it is evidence of a crash,
    not a checkpoint, and removing it would destroy the only trace of
    what the previous process was doing when something can still
    diagnose that. It costs nothing to leave it — every read of it
    answers ``None``.

    A delete that fails is logged and swallowed. The caller has already
    been handed the checkpoint, which is what it came for; failing the
    resume over a file it can no longer remove would be a strictly worse
    outcome than one wasted resume attempt.
    """
    checkpoint = read_pause_checkpoint(plan_dir)
    if checkpoint is None:
        return None
    clear_pause_checkpoint(plan_dir)
    return checkpoint


def clear_pause_checkpoint(plan_dir: PathLike) -> None:
    """Remove the checkpoint if it exists; do nothing (loudly) if it does not.

    Idempotent by design — the "resume finished cleanly" path calls this
    unconditionally, and making it conditional on an existing file would
    only move the ``FileNotFoundError`` to the caller.
    """
    path = checkpoint_path(plan_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "pause_state_clear_failed: could not remove %s (%s); "
            "a stale checkpoint may be re-consumed",
            path, type(exc).__name__,
        )


def checkpoint_summary(plan_dir: PathLike) -> Optional[Dict[str, Any]]:
    """The projection a listing needs: what stopped, when, how much.

    Returns ``None`` when there is no checkpoint, and otherwise only the
    scalar fields — never the task bodies. A summary that carries the
    task list is a second way to leak a plan's internals into a view
    that has no use for them.

    ``decision_id`` is projected only when the checkpoint carries one,
    for the same reason the writer omits it: a summary that always had
    the key would claim a decision was behind every pause.
    """
    checkpoint = read_pause_checkpoint(plan_dir)
    if checkpoint is None:
        return None
    summary = {
        "paused_at": checkpoint.get("paused_at"),
        "kind": checkpoint.get("kind"),
        "project_dir": checkpoint.get("project_dir"),
        "interrupted_task_count": len(checkpoint.get("interrupted_tasks") or []),
        "verification_round": checkpoint.get("verification_round"),
    }
    decision_id = checkpoint.get(_DECISION_ID_FIELD)
    if decision_id is not None:
        summary[_DECISION_ID_FIELD] = decision_id
    return summary


# ---------------------------------------------------------------------------
# The task→session mapping
# ---------------------------------------------------------------------------
#
# A resume needs the session id of the task it is about to re-open, and
# the checkpoint above is written *at stop time* — it can only record a
# session that was already bound when the stop happened. The binding
# itself is written by the executor, as it happens, into the execution
# log: one ``task_session_bound`` event per attempt, with the session
# id under ``data``.
#
# That log is the only durable record of the binding, so it is the
# place a resume must read the ids from. Two properties of it shape
# the reader:
#
# * **It rotates.** At 10 MB the live log becomes ``execution.log.1``
#   and the previous ``.1`` becomes ``.2``. A binding a resume needs
#   may have been rotated aside, so the reader looks at the rotated
#   copies too — oldest first, so the live log's bindings overwrite
#   them, which is the correct order: a rotation only moves *old* lines
#   out of the way.
# * **It is written while the plan runs.** A line can be torn by a kill
#   mid-write, and the log holds entries for every event the executor
#   emits, not just the bindings. So every line is judged on its own:
#   a line that is not JSON, or that is not a binding, is skipped, and
#   the lines around it are unaffected.

def _rotated_log_paths(log_path: Path) -> List[Path]:
    """The rotated copies of ``log_path``, oldest first.

    The highest index holds the oldest lines and ``.1`` the newest of
    the rotated ones, so a descending index is a chronological order.

    The depth is read from ``ExecutionLogger`` — the writer that
    produces these copies — rather than repeated here. A binding can
    only be found if the reader reaches the copy it was rotated into,
    so a reader that kept its own copy of the number would silently
    stop finding the oldest bindings the day the writer's rotation
    depth changed.
    """
    return [
        log_path.parent / f"{log_path.name}.{index}"
        for index in range(ExecutionLogger.BACKUP_COUNT, 0, -1)
    ]


def _session_binding(entry: Any) -> Optional[Tuple[str, str]]:
    """The ``(task_id, session_id)`` an entry binds, or ``None``.

    ``None`` for every line that is not a binding: a payload that is
    not an object, an event that is not ``task_session_bound`` (a
    missing event name included — the log holds an entry for every
    event the executor emits, and only a named binding is one), a
    missing or empty ``task_id``, a missing or empty ``session_id``.

    The session id is read from ``data``, where the executor puts it
    (``agent.py`` writes ``data={"session_id": ...}`` alongside
    ``event="task_session_bound"``). A session id anywhere else is not
    read: a binding is exactly the shape the executor writes, and a
    reader that accepts other shapes will eventually accept a line that
    means something else.
    """
    if not isinstance(entry, dict):
        return None

    if entry.get("event") != _SESSION_BOUND_EVENT:
        return None

    task_id = entry.get("task_id")
    if task_id is None or (isinstance(task_id, str) and not task_id.strip()):
        return None

    data = entry.get("data")
    session_id = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    return str(task_id), session_id


def _merge_log_bindings(log_path: Path, bindings: Dict[str, str]) -> None:
    """Merge the bindings found in one log file into ``bindings``.

    Later lines win: the executor re-binds a task on every attempt,
    and the newest binding is the one a resume must use.

    A file that cannot be read is logged and skipped. A log that is
    missing, unreadable or torn costs the mapping one file — never the
    whole call, and never an exception: a resume that cannot find a
    binding has to be reachable, because it is the path that runs after
    a crash.

    A *missing* file is not a problem to report: most plans never
    rotate their log, so most of the rotated copies a reader looks for
    are not there. Only a file that exists and cannot be read is worth
    a warning.
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    # ``json.JSONDecodeError`` is a ``ValueError``: a
                    # torn line, or a line that is not JSON at all.
                    continue
                binding = _session_binding(entry)
                if binding is None:
                    continue
                task_id, session_id = binding
                bindings[task_id] = session_id
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "pause_state_log_unreadable: ignoring unreadable log %s (%s)",
            log_path, type(exc).__name__,
        )


def collect_task_sessions(log_path: PathLike) -> Dict[str, str]:
    """The task→session mapping recorded in an execution log.

    ``log_path`` is the *live* log (``<plan_dir>/execution.log``); its
    rotated copies are found next to it. The result maps a task id to
    the session id that task was last bound to:

    ::

        {"3": "9f2c…"}

    The rotated copies are read first, oldest first, and the live log
    last — so a binding written after a rotation wins over the one it
    replaced. Within a file, the last binding for a task wins too: the
    executor re-binds on every attempt, and the newest attempt is the
    one a resume can re-open.

    An empty mapping means "no session to resume", which is the answer
    for a plan that never ran, and the answer a caller gets when the
    log holds no usable binding. Nothing here raises.
    """
    path = Path(log_path)
    bindings: Dict[str, str] = {}
    for candidate in [*_rotated_log_paths(path), path]:
        _merge_log_bindings(candidate, bindings)
    return bindings