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

The pause intent
----------------
The checkpoint records what a stop interrupted. The **pause intent**
records the request itself — "a pause was asked for, and here is why" —
and it is a separate document (``pause_intent.json``) written *before*
anything acts on the request, because the thing that acts on it may be
killed right after. A repair round that finds no intent on disk has no
evidence to give an exit code anything but "failed".

::

    {
      "schema_version": 1,
      "reason": "user_pause",                     # why it was asked for
      "requested_at": "2026-11-20T08:42:50+00:00"  # when it was asked
    }

It follows the same degradation rule as the checkpoint — absent,
torn, or reasonless all read as ``None`` — with one difference: a
**write** that fails is raised rather than swallowed. Storing the
intent is this layer's whole job; deciding what a failed store means
belongs to the caller that asked for the pause.

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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple, Union

from config_paths import resolve_plans_dir
from execution_logger import ExecutionLogger
from plan_scheduler.policy import REASONS
from utils.atomic_io import atomic_write_json

try:  # pragma: no cover - the two paths are the same module
    from framework_fix.scheduling import PREEMPTED_REASON
except ImportError:  # pragma: no cover
    from backend.framework_fix.scheduling import (  # type: ignore
        PREEMPTED_REASON,
    )

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Name of the per-plan checkpoint file.
PAUSE_CHECKPOINT_FILENAME = "pause_state.json"

#: Name of the per-plan pause-intent file. A separate document from the
#: checkpoint on purpose: the intent is written *before* the stop
#: happens, the checkpoint *because of* it.
PAUSE_INTENT_FILENAME = "pause_intent.json"

#: Bumped only on an incompatible change to the intent document shape.
_INTENT_SCHEMA_VERSION = 1

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
# ---------------------------------------------------------------------------
# The pause intent — why a plan is not running
# ---------------------------------------------------------------------------
#
# The checkpoint above records what a ``stop`` interrupted.  The **intent**
# is a different thing: it says the plan is *not supposed to be running*,
# and why.  A plan the framework-fix lane preempted is stopped, but the
# stop is not the point — the point is that the scheduler decided the plan
# must wait, and that decision has to outlive the process that made it.
#
# Without a durable record of it, a restart brings the plan back as if
# nothing had happened: the plan runs, the lane's decision does not, and
# the two disagree about what the scheduler is doing.  So the intent is a
# small document of its own, written next to the plan's other records and
# read back by whatever recovers after a restart.
#
# Three functions carry it, and they are the whole contract:
#
# * :func:`write_pause` — record the intent, and answer what was recorded;
# * :func:`read_pause` — read it back, or ``None`` when there is none;
# * :func:`clear_pause` — take it away (the plan is back; it is not paused).
#
# The reason is a word from the scheduler's own closed vocabulary
# (:data:`plan_scheduler.policy.REASONS`) — the same words the panel, the
# dry run and the API draw.  A word outside that set is **refused** rather
# than written: an intent nobody can draw is not a record, it is a piece
# of noise that hides the plans that do have a reason.
#
# The lifecycle keeps its own three names for the same thing — they are the
# ones that write the intent just before the plan is stopped:
#
# * :func:`write_pause_intent` — record that a pause was asked for;
# * :func:`read_pause_intent` — read it back, or ``None`` when there is none;
# * :func:`clear_pause_intent` — take it away.
#
# **One file, one document.**  Both writers write ``pause_intent.json`` into
# the plan's own directory, so they write the same shape: the two version
# fields (:data:`_PAUSE_INTENT_VERSION` and :data:`_INTENT_SCHEMA_VERSION`)
# are one number under two names, and every field either side knows is
# written by both.  The record is deliberately small:
#
#     {
#       "version": 1,
#       "schema_version": 1,
#       "plan_id": "p-20261123-120000-a",          # whose plan it is
#       "reason": "user_pause",                    # the lifecycle's own reason
#       "scheduler_reason": "preempted",           # the scheduler's own reason
#       "source": "framework_fix_preemption",      # who decided it must wait
#       "fix_plan_id": "p-20261123-121500-b",      # the repair plan that took
#                                                  # the slot
#       "paused_at": "2026-11-20T08:42:50+00:00",    # when the intent landed
#       "requested_at": "2026-11-20T08:42:50+00:00", # when it was asked for
#       "persisted": true
#     }
#
# **Two reasons, two fields.**  The two layers answer "why is this plan not
# running" with words from their own vocabularies, and the file keeps both:
# the lifecycle's reason is ``reason`` (the one the repair round reads — it
# only asks "was a pause asked for?"), and the scheduler's word from its
# closed vocabulary is ``scheduler_reason`` (the one the panel draws).  Each
# reader answers with its own: :func:`read_pause_intent` hands the document
# over as it is, and :func:`read_pause` presents the scheduler's word as
# ``reason``.  Neither writer overwrites the other's word.
#
# The file is written atomically and cleared once the pause has been
# honoured.

#: Bumped only on an incompatible change to the intent's shape.  An intent
#: written by a newer PDT is ignored by an older one: the safe direction
#: there is "this plan is not paused" rather than "this plan is paused for
#: a reason this code cannot read".
_PAUSE_INTENT_VERSION = 1

#: The closed vocabulary of pause reasons.  It is the scheduler's own
#: closed set — not a copy of it — so a reason the scheduler can say is a
#: reason this layer accepts, and a word outside the set is refused.
PAUSE_REASONS: FrozenSet[str] = frozenset(REASONS)

#: The pause was asked for by a person at the panel.
OPERATOR_SOURCE = "operator"

#: The pause was forced by the framework-fix lane: the plan gave its slot
#: to a repair plan.
FRAMEWORK_FIX_SOURCE = "framework_fix_preemption"

#: Every source a pause may have.  The set is closed on purpose: a new
#: source is a new situation, and it needs a decision nobody has made yet.
PAUSE_SOURCES: FrozenSet[str] = frozenset(
    {OPERATOR_SOURCE, FRAMEWORK_FIX_SOURCE}
)

#: The reason a pause carries when the caller named none: the panel was
#: used and nothing else was said.
DEFAULT_PAUSE_REASON = "paused"


class PauseReasonError(ValueError):
    """A reason (or a source) that is not in the closed vocabulary.

    It is raised **before** anything is written: a pause intent nobody can
    draw is worse than no intent at all, because the plan would come back
    after a restart believing it may run.

    Attributes:
        kind: what was wrong — ``reason`` or ``source``.
        value: the word that was refused.
    """

    def __init__(self, message: str, *, kind: str = "reason", value: Any = None) -> None:
        """Hold the refusal and the word it is about.

        Args:
            message: the sentence the caller is answered with.
            kind: what was wrong — ``reason`` or ``source``.
            value: the word that was refused.
        """
        super().__init__(message)
        self.kind = kind
        self.value = value


def checked_reason(reason: Optional[str]) -> str:
    """The reason to record, checked against the closed vocabulary.

    ``None`` (or an empty word) means "the caller named no reason": the
    operator's pause, which is :data:`DEFAULT_PAUSE_REASON`.

    Raises:
        PauseReasonError: the word is not in :data:`PAUSE_REASONS`.
    """
    if reason is None:
        return DEFAULT_PAUSE_REASON
    text = _text(reason)
    if text is None or not text.strip():
        return DEFAULT_PAUSE_REASON
    if text not in PAUSE_REASONS:
        raise PauseReasonError(
            "unknown pause reason: {0!r} (the vocabulary is {1!r})".format(
                text, sorted(PAUSE_REASONS),
            ),
            kind="reason",
            value=text,
        )
    return text


def checked_source(reason: str, source: Optional[str]) -> str:
    """Where the pause came from — the caller's word, or the reason's.

    The caller may say outright; when it does not, the reason decides: the
    framework-fix lane's preemption is the lane's, everything else is the
    operator's.

    Raises:
        PauseReasonError: the word is not in :data:`PAUSE_SOURCES`.
    """
    if source is None:
        if reason == PREEMPTED_REASON:
            return FRAMEWORK_FIX_SOURCE
        return OPERATOR_SOURCE
    text = _text(source)
    if text not in PAUSE_SOURCES:
        raise PauseReasonError(
            "unknown pause source: {0!r} (the vocabulary is {1!r})".format(
                text, sorted(PAUSE_SOURCES),
            ),
            kind="source",
            value=text,
        )
    return text


def _checked_plan_id(plan_id: Any) -> str:
    """The plan id, checked before it becomes a path.

    A plan id names a plan; it does not name a place.  A word carrying a
    separator would let a caller write outside the plans tree, so it is
    refused here rather than trusted further down.

    Raises:
        ValueError: the id is empty or names a path.
    """
    text = str(plan_id or "").strip()
    if not text:
        raise ValueError("a pause intent needs a plan id")
    if text in (".", "..") or "/" in text or "\\" in text or os.sep in text:
        raise ValueError(
            "a plan id may not name a path: {0!r}".format(plan_id)
        )
    return text


def _plans_root(root: Optional[PathLike] = None) -> Path:
    """The plans root the intents live under.

    The root is resolved on **every** call and never cached:
    ``PDT_PLANS_DIR`` is the harness's escape hatch, and a resolver that
    answered once would answer with the tree that was current when the
    module was imported.
    """
    if root is not None:
        return Path(root)
    return resolve_plans_dir()


def _plan_dir_candidates(root: Path, plan_id: str) -> Tuple[Path, Path]:
    """The two places this plan's directory may be.

    The root may be the plans tree itself or a repository root that holds
    one, so both are tried — in that order, because the first is where a
    plan of this tree really lives.  It is the same pair the scheduler's
    facts reader uses, so a plan's pause intent lands in the very
    directory the rest of the backend reads.
    """
    return (root / plan_id, root / "plans" / plan_id)


def _plan_dir_of(plan_id: str, root: Optional[PathLike] = None) -> Path:
    """The plan's own directory under the plans root."""
    candidates = _plan_dir_candidates(_plans_root(root), plan_id)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def pause_intent_path(plan_id: str, *, root: Optional[PathLike] = None) -> Path:
    """Where the pause intent of ``plan_id`` lives."""
    return _plan_dir_of(_checked_plan_id(plan_id), root) / PAUSE_INTENT_FILENAME


def _stamp_of(clock: Optional[Callable[[], float]] = None) -> str:
    """The moment the intent was written, as an aware UTC stamp.

    The clock is a parameter so that a case can move time by hand; the
    real clock is what the running backend uses.
    """
    if clock is None:
        return _now_iso()
    try:
        moment = float(clock())
    except (TypeError, ValueError):
        return _now_iso()
    try:
        return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return _now_iso()


def _intent_version_known(data: Dict[str, Any]) -> bool:
    """Whether the document on disk is a shape this code understands.

    Two shapes are in the wild and both are honoured: this layer's own
    (``version``) and the lifecycle's (``schema_version``) — the two writers
    of this one file each carry their own name for the same number.  A
    document that carries neither is **not** read: an intent nobody can
    read is safer to ignore than to half-believe.
    """
    if data.get("version") == _PAUSE_INTENT_VERSION:
        return True
    return data.get("schema_version") == _INTENT_SCHEMA_VERSION


def write_pause(
    plan_id: str,
    reason: Optional[str] = None,
    *,
    source: Optional[str] = None,
    fix_plan_id: Optional[str] = None,
    root: Optional[PathLike] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Record why a plan is not running; answer what was recorded.

    The answer is the question that was asked — which plan, which reason,
    and whether it landed — so a caller can tell a recorded pause from one
    that only happened in memory.  The record itself (with its timestamp
    and its source) is read back with :func:`read_pause`.

    Args:
        plan_id: the plan that is not running.
        reason: the word from the scheduler's closed vocabulary that says
            why.  A word outside it is refused and nothing is written.
            The operator's own pause is the default when it is omitted.
        source: where the pause came from — the operator or the
            framework-fix lane.  Derived from the reason when omitted.
        fix_plan_id: the repair plan that took the slot, when the pause
            was a preemption.
        root: the plans root to write under.  The configured one when
            omitted.
        clock: the source of "now", in epoch seconds.

    Returns:
        ``{"plan_id": ..., "reason": ..., "persisted": ...}``.

    Raises:
        PauseReasonError: the reason or the source is not in the closed
            vocabulary.  Nothing is written.
        ValueError: the plan id names a path rather than a plan.
    """
    plan = _checked_plan_id(plan_id)
    reason_text = checked_reason(reason)
    source_text = checked_source(reason_text, source)

    path = _plan_dir_of(plan, root) / PAUSE_INTENT_FILENAME
    stamp = _stamp_of(clock)
    document: Dict[str, Any] = {
        "version": _PAUSE_INTENT_VERSION,
        "schema_version": _INTENT_SCHEMA_VERSION,
        "plan_id": plan,
        # The scheduler's word is written to **both** fields: to
        # ``scheduler_reason``, which is where it lives once the lifecycle
        # has written its own reason, and to ``reason``, so that a pause
        # that only the scheduler asked for is still a document the
        # lifecycle's reader can read (it needs a reason to believe).
        "reason": reason_text,
        "scheduler_reason": reason_text,
        "source": source_text,
        "fix_plan_id": _text(fix_plan_id),
        "paused_at": stamp,
        "requested_at": stamp,
        "persisted": True,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, document, logger=logger, reraise=True)
    except OSError as exc:
        logger.warning(
            "pause_intent_unwritable: the pause intent of the plan %s could "
            "not be written (%s: %s); a restart will not honour it",
            plan, type(exc).__name__, exc,
        )
        return {"plan_id": plan, "reason": reason_text, "persisted": False}
    return {"plan_id": plan, "reason": reason_text, "persisted": True}


def read_pause(
    plan_id: str, *, root: Optional[PathLike] = None
) -> Optional[Dict[str, Any]]:
    """The plan's pause intent, or ``None`` when there is none to honour.

    ``None`` covers every unusable case — absent, unreadable, not JSON,
    JSON but not an object, or a version this code does not know — so the
    caller has one answer to handle and no exception to catch.  It is the
    same degradation the checkpoint above is built on: a plan that cannot
    be read back is a plan that is resumed from scratch.
    """
    try:
        path = pause_intent_path(plan_id, root=root)
    except ValueError as exc:
        logger.warning("pause_intent_unreadable: %s", exc)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "pause_intent_unreadable: ignoring unreadable intent %s (%s: %s)",
            path, type(exc).__name__, exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    if not _intent_version_known(data):
        logger.warning(
            "pause_intent_version_mismatch: ignoring the intent %s with "
            "version %r; the plan is treated as not paused",
            path, data.get("version"),
        )
        return None
    return _scheduler_view(data)


def _scheduler_view(data: Dict[str, Any]) -> Dict[str, Any]:
    """The document as **the scheduler** reads it.

    The scheduler's own word lives in ``scheduler_reason`` as soon as the
    lifecycle has written its own reason into ``reason``; an intent that
    only the scheduler wrote carries the same word in both.  The view
    answers with the scheduler's word under the name the scheduler reads,
    and leaves the rest of the document exactly as it is on disk.
    """
    reason = data.get("scheduler_reason") or data.get("reason")
    if reason is None or reason == data.get("reason"):
        return data
    view = dict(data)
    view["reason"] = reason
    return view


def clear_pause(plan_id: str, *, root: Optional[PathLike] = None) -> None:
    """Take the plan's pause intent away; do nothing loudly if it has none.

    Idempotent by design — the resume path calls this unconditionally, and
    making it conditional on an existing file would only move the
    ``FileNotFoundError`` to the caller.

    A delete that fails is logged and swallowed.  The caller has already
    decided the plan is not paused, and failing the resume over a file it
    can no longer remove would be a strictly worse outcome than one
    leftover intent.
    """
    try:
        path = pause_intent_path(plan_id, root=root)
    except ValueError as exc:
        logger.warning("pause_intent_clear_failed: %s", exc)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "pause_intent_clear_failed: could not remove %s (%s); a stale "
            "intent may be honoured after a restart",
            path, type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# The lifecycle's own pause intent
# ---------------------------------------------------------------------------
#
# The three functions below are the lifecycle's names for the same record
# the section above keeps: the intent is written *before* anything acts on
# the request, because the thing that acts on it is a process that may be
# killed right after — and a repair round that cannot find an intent on
# disk has no evidence to give an exit code anything but "failed".
#
# They live on the plan's directory rather than on a plan id, because the
# lifecycle is handed the directory and not the id, and they write the very
# same document the section above writes — see "One file, one document"
# there.


def intent_path(plan_dir: PathLike) -> Path:
    """Where ``plan_dir``'s pause intent lives."""
    return Path(plan_dir) / PAUSE_INTENT_FILENAME


def write_pause_intent(plan_dir: PathLike, *, reason: str) -> Dict[str, Any]:
    """Record that a pause was asked for, and return what was written.

    The return value is the stored document itself, so a caller can log
    or assert on exactly the fields a later read will see rather than
    on what it hoped it passed.

    ``plan_dir`` is created if it does not exist: the intent is the
    first thing written for a pause, and "the directory is not there
    yet" is not a reason to drop the request on the floor.

    The write is atomic (tempfile + ``os.replace``), so a kill
    mid-write leaves no torn file. Unlike the checkpoint, a write that
    still fails is **raised** rather than swallowed: this layer stores
    the intent, it does not get to decide what a failed store means —
    that call belongs to whoever asked for the pause.
    """
    stamp = _now_iso()
    # The record already on disk keeps the fields this layer does not own:
    # the scheduler writes its own reason and its own source **before** the
    # lifecycle stops the plan, and this write must not erase them — the
    # reason from the scheduler's closed vocabulary is the one the panel
    # draws, and the source says who decided that the plan must wait.
    existing = read_pause_intent(plan_dir) or {}
    intent: Dict[str, Any] = {
        "version": _PAUSE_INTENT_VERSION,
        "schema_version": _INTENT_SCHEMA_VERSION,
        "plan_id": existing.get("plan_id") or Path(plan_dir).name,
        # This layer's own reason is the one that goes into ``reason``: it
        # is the reason this call was given, and it is the one the repair
        # round reads when it asks "was a pause asked for?".  The
        # scheduler's word stays where it lives — ``scheduler_reason`` —
        # so that writing this does not erase it.
        "reason": _text(reason),
        "scheduler_reason": existing.get("scheduler_reason"),
        "source": existing.get("source") or OPERATOR_SOURCE,
        "fix_plan_id": existing.get("fix_plan_id"),
        "paused_at": existing.get("paused_at") or stamp,
        "requested_at": stamp,
        "persisted": True,
    }

    path = intent_path(plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, intent, reraise=True, logger=logger)
    return intent


def read_pause_intent(plan_dir: PathLike) -> Optional[Dict[str, Any]]:
    """The stored pause intent, or ``None`` if there is none to honour.

    ``None`` covers every unusable case — absent, unreadable, not JSON,
    JSON but not an object, or a document without a ``reason``. Callers
    therefore have one "no pause was requested" answer to handle and no
    exception to catch.
    """
    path = intent_path(plan_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError``. An intent file
        # torn by a crash mid-write, or written by something else, costs
        # a pause with no recorded reason and nothing else.
        logger.warning(
            "pause_intent_unreadable: ignoring unreadable pause intent %s "
            "(%s); no pause intent on record",
            path, type(exc).__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        # A document without a reason does not say *why* the pause was
        # asked for, which is the whole content of an intent.
        logger.warning(
            "pause_intent_reasonless: ignoring pause intent %s without a "
            "usable reason; no pause intent on record",
            path,
        )
        return None
    return data


def clear_pause_intent(plan_dir: PathLike) -> None:
    """Remove the pause intent if it exists; do nothing if it does not.

    Idempotent by design — the "the pause was honoured" path calls this
    unconditionally, and making it conditional on an existing file would
    only move the ``FileNotFoundError`` to the caller.
    """
    path = intent_path(plan_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "pause_intent_clear_failed: could not remove %s (%s); "
            "a stale pause intent may be re-read",
            path, type(exc).__name__,
        )

def scan_pauses(*, root: Optional[PathLike] = None) -> List[Dict[str, Any]]:
    """Every pause intent under the plans root.

    This is what a restart reads: the plans that must not come back as if
    nothing had happened.  The scan is a walk over the plan directories —
    one file read per plan — and it answers with the intents themselves,
    so the reader sees the same words the writer wrote.
    """
    base = _plans_root(root)
    found: List[Dict[str, Any]] = []
    seen: set = set()
    for container in (base, base / "plans"):
        if not container.is_dir():
            continue
        for candidate in sorted(container.iterdir()):
            if not candidate.is_dir() or candidate.name in seen:
                continue
            intent = read_pause(candidate.name, root=root)
            if intent is None:
                continue
            seen.add(candidate.name)
            found.append(intent)
    return found


class PauseIntentStore:
    """The pause intents of the plans under one root.

    The module-level functions above are the same thing with the
    configured root; this class is the same thing with a root of the
    caller's choosing.  A **new instance** of it reads exactly what the
    old one wrote — that is what a restart is, from the point of view of
    this layer: another reader of the same directory.

    Attributes:
        root: the plans root this store reads and writes.  The configured
            one when it was not named.
    """

    def __init__(self, root: Optional[PathLike] = None) -> None:
        """Bind the store to a root.

        Args:
            root: the plans root to read and write.  The configured one
                when it is omitted.
        """
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        """The plans root this store reads and writes."""
        return _plans_root(self._root)

    def write_pause(
        self,
        plan_id: str,
        reason: Optional[str] = None,
        *,
        source: Optional[str] = None,
        fix_plan_id: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> Dict[str, Any]:
        """Record why a plan is not running; answer what was recorded."""
        return write_pause(
            plan_id,
            reason,
            source=source,
            fix_plan_id=fix_plan_id,
            root=self._root,
            clock=clock,
        )

    def read_pause(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """The plan's pause intent, or ``None``."""
        return read_pause(plan_id, root=self._root)

    def clear_pause(self, plan_id: str) -> None:
        """Take the plan's pause intent away."""
        clear_pause(plan_id, root=self._root)

    def scan(self) -> List[Dict[str, Any]]:
        """Every pause intent under this store's root."""
        return scan_pauses(root=self._root)
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