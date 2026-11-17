"""Post-processing progress — the finishing stage's persisted per-item state.

What this module owns
---------------------
The finishing stage (收尾阶段) may run twice: once now, and once more
after a human has dealt with whatever failed.  **Which items the
second run repeats must be decided by something that outlived the
first run.**  An in-memory list dies with the process; a flag on the
orchestrator (``had_failures = True``) dies with it too, and it cannot
say *which* items failed; an agent's recollection of what it already
tried is not a record at all.  So the state lives in one file —
``<plan>/postprocess_progress.json`` — and this module is the only
thing that reads or writes it.

The file's shape is fixed by the architecture (decision point 3)::

    {
      "spec_sha256": "<the frozen requirement book's fingerprint>",
      "started_at": "<when the stage started>",
      "items": {
        "<item id>": {
          "status": "pending|running|passed|failed|waiting",
          "attempts": <int>,
          "last_evidence_ref": "<path, or null>",
          "wait": {...}          # only while the item waits
        }
      }
    }

Three decisions this module makes, and why
-------------------------------------------

**The retry scope is a query over the file, not a flag.**
:func:`failed_item_ids` computes the scope from the persisted record.
The orchestrator therefore cannot widen it by accident: the only way
to retry an item is for the file to say the item failed.

**A write is atomic.**  The document is written to a temporary file in
the *same directory* and then renamed over the target with
``os.replace``.  A process killed between the two leaves the previous
version complete and parseable — a half-written progress file would
be read as "nothing failed" and the retry round would silently skip
the items it was supposed to repeat.

**A damaged file is reported, never guessed.**  A corrupt progress
file raises :class:`ProgressError` instead of being read as an empty
progress.  The same reasoning, one level up: an empty progress means
"nothing to retry", so treating damage as emptiness would turn a
crash into a silent skip of the very work the file exists to protect.

The progress file is separate from the terminal report on purpose
(architecture decision point 3): the progress is *mutable* — it is
rewritten after every item — while the report is written once, at the
end, and holds the verdicts a human reads.  Mixing them would mean a
progress update overwrites a verdict.

Where the file lives
--------------------
``<plans root>/<plan_id>/postprocess_progress.json``.  The plans root
comes from the repository's own resolver
(:func:`config_paths.resolve_plans_dir`), so a test that redirects
``PDT_PLANS_DIR`` gets its own tree and no case ever writes into the
operator's live plans directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from config_paths import resolve_plans_dir
from framework.ids import validate_plan_id


__all__ = [
    "PROGRESS_FILENAME",
    "PROGRESS_STATUSES",
    "SPEC_FILENAME",
    "ProgressError",
    "failed_item_ids",
    "load_progress",
    "mark",
    "progress_path",
]


#: The file the progress lives in, inside the plan's own directory.
PROGRESS_FILENAME: str = "postprocess_progress.json"

#: The requirement book the progress is measured against.
SPEC_FILENAME: str = "postprocess.json"

#: The closed status vocabulary.  A status outside it is a bug in the
#: caller, not a state the file may hold: the retry round reads
#: ``failed``, the report reads the rest, and a typo that lands
#: somewhere else would be a status nobody looks at.
PROGRESS_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "passed",
    "failed",
    "waiting",
)

#: The status whose items a retry round picks up.
FAILED_STATUS: str = "failed"


class ProgressError(ValueError):
    """Raised when the progress file cannot be read as a progress file.

    A subclass of :class:`ValueError` so that a caller which already
    treats bad input as a value error keeps working, while a caller
    that wants to tell "this file is damaged" from "this argument is
    wrong" can pin the type exactly.

    Raised for a file that is not JSON, for a document that is not an
    object, for a document whose ``items`` is not an object, and for a
    status outside :data:`PROGRESS_STATUSES`.
    """


#: The plans root captured at import time.  :data:`PLANS_DIR` below is
#: the in-process seam: a caller that patches the module attribute gets
#: that root instead of the ambient one.
_DEFAULT_PLANS_DIR: Path = resolve_plans_dir()

#: The plans root this module reads and writes.
PLANS_DIR: Path = _DEFAULT_PLANS_DIR


def _plans_dir() -> Path:
    """The plans root to use right now.

    :func:`config_paths.resolve_plans_dir` re-reads ``PDT_PLANS_DIR``
    on every call — never cached — so a test that redirects the plans
    tree after import is honoured.  That is the same escape hatch every
    other plan-artifact writer in this backend uses.

    :data:`PLANS_DIR` wins when it differs from the value captured at
    import: an explicit in-process override is more specific than the
    ambient environment variable, and a test that patches the module
    attribute means it.
    """
    if PLANS_DIR != _DEFAULT_PLANS_DIR:
        return PLANS_DIR
    return resolve_plans_dir()


def _plan_dir(plan_id: str) -> Path:
    """The directory of one plan, with the plan id validated as a leaf.

    The plan id becomes a filesystem path component, so it goes through
    the repository's shared validator (:func:`framework.ids.
    validate_plan_id`) — the same one the HTTP routes use.  An id like
    ``../escape`` is refused here rather than becoming a path.
    """
    return _plans_dir() / validate_plan_id(plan_id)


def progress_path(plan_id: str) -> Path:
    """The path of one plan's progress file.

    Args:
        plan_id: The plan whose progress file is wanted.

    Returns:
        ``<plans root>/<plan_id>/postprocess_progress.json``.  The file
        need not exist — the path is a location, not a claim.

    Raises:
        ValueError: When the plan id is not a safe filesystem leaf.
    """
    return _plan_dir(plan_id) / PROGRESS_FILENAME


def _empty_progress() -> Dict[str, Any]:
    """The skeleton of a progress that does not exist yet.

    The three keys are the file's whole shape, so a caller that reads
    a progress for a plan that never ran gets the same document it
    would get from a file with no items — with empty values rather
    than a missing key.
    """
    return {"spec_sha256": "", "started_at": "", "items": {}}


def load_progress(plan_id: str) -> Dict[str, Any]:
    """Read one plan's progress.

    Args:
        plan_id: The plan whose progress is wanted.

    Returns:
        The progress document — ``spec_sha256``, ``started_at`` and
        ``items`` — as a fresh dict.  A plan that has never run yields
        the empty skeleton: *no progress yet* is a normal state, not a
        failure.

    Raises:
        ProgressError: When the file exists but cannot be read as a
            progress document (not JSON, not an object, or ``items``
            that is not an object).  Damage is reported, never read as
            an empty progress — an empty progress means "nothing to
            retry", so mistaking damage for emptiness would skip the
            very work the file exists to protect.
        ValueError: When the plan id is not a safe filesystem leaf.
    """
    path = progress_path(plan_id)

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_progress()
    except OSError as exc:
        raise ProgressError(f"cannot read progress file {path}: {exc}") from exc

    try:
        document = json.loads(raw)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ProgressError(f"progress file {path} is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise ProgressError(
            f"progress file {path} must contain a JSON object, "
            f"got {type(document).__name__}"
        )

    items = document.get("items")
    if not isinstance(items, dict):
        raise ProgressError(
            f"progress file {path} must carry an 'items' object, "
            f"got {type(items).__name__}"
        )

    document["items"] = items
    document.setdefault("spec_sha256", "")
    document.setdefault("started_at", "")
    return document


def _spec_fingerprint(plan_dir: Path) -> str:
    """The digest of the requirement book as it stands on disk.

    The fingerprint answers "which version of the book is this progress
    about?".  It is the sha256 of the requirement book file's bytes;
    a plan with no book yet gets the digest of no bytes at all, which
    is still a real sha256 — an empty string would be a value that
    looks like nothing was recorded.

    A caller that holds the *canonical* fingerprint of a frozen book
    (the one :func:`postprocess.spec.freeze` computes) passes it to
    :func:`mark` instead, and this fallback is not used.
    """
    try:
        payload = (plan_dir / SPEC_FILENAME).read_bytes()
    except OSError:
        payload = b""
    return hashlib.sha256(payload).hexdigest()


def _next_attempts(recorded: Any, stated: Any) -> int:
    """The attempt count after one more mark.

    ``attempts`` counts how many times the item has been through the
    stage.  A caller states the attempt it is marking (``attempts=1``
    for the first try, ``attempts=2`` for the retry).  The recorded
    count never moves backwards and never sits below the caller's
    number; a statement that does not advance the counter still counts
    as an attempt, so the recorded count is
    ``max(recorded + 1, stated)``.

    A mark that states no count leaves the counter alone: only the
    caller knows which execution a mark belongs to, so only the caller
    may state it.
    """
    current = int(recorded or 0)
    if stated is None:
        return current
    return max(current + 1, int(stated))


def _write_progress(path: Path, document: Mapping[str, Any]) -> None:
    """Write the progress document atomically.

    The document goes to a temporary file **in the same directory** and
    is then renamed over the target with ``os.replace``.  Two things
    follow from that, and both are the point:

    * the rename is atomic on every filesystem this backend runs on, so
      a reader sees either the previous version or the new one — never
      a half-written document;
    * the temporary file lives next to the target, so the rename never
      crosses a filesystem boundary (where ``os.replace`` is not
      atomic).

    If the rename fails, the temporary file is removed and the
    exception is re-raised unchanged — the previous version of the
    progress file is still the live one, complete and parseable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # The rename never happened: the previous file is untouched.
        # The temporary file is best-effort cleanup — its absence must
        # not mask the failure that got us here.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def mark(
    plan_id: str,
    item_id: str,
    status: str,
    *,
    attempts: Optional[int] = None,
    last_evidence_ref: Optional[str] = None,
    wait: Optional[Mapping[str, Any]] = None,
    spec_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one item's state and persist the progress file.

    Args:
        plan_id: The plan the item belongs to.
        item_id: The item's identifier, unique inside the plan's book.
        status: One of :data:`PROGRESS_STATUSES`.
        attempts: The attempt count the caller states for this mark.
            ``None`` leaves the recorded count alone.
        last_evidence_ref: Where this item's latest evidence lives.
            ``None`` keeps the reference already recorded — the *last*
            evidence seen is not erased by a mark that names none.
        wait: The wait record for an item that is waiting.  ``None``
            keeps the record already there, and an item that never
            waited carries no ``wait`` key at all.
        spec_sha256: The frozen requirement book's fingerprint.  When
            it is not given, the fingerprint of the requirement book on
            disk is used — and, once written, it never moves again.

    Returns:
        The progress document as it now stands on disk.

    Raises:
        ProgressError: When the status is outside the closed
            vocabulary, or when the existing progress file cannot be
            read (see :func:`load_progress`).
        ValueError: When the plan id is not a safe filesystem leaf.
    """
    if status not in PROGRESS_STATUSES:
        raise ProgressError(
            f"Invalid status: {status}. Must be one of {PROGRESS_STATUSES}"
        )
    if not isinstance(item_id, str) or not item_id:
        raise ProgressError(f"Invalid item id: {item_id!r}. Must be a non-empty string")

    plan_dir = _plan_dir(plan_id)
    progress = load_progress(plan_id)
    items = progress["items"]

    record = dict(items.get(item_id) or {})
    record["status"] = status
    record["attempts"] = _next_attempts(record.get("attempts"), attempts)
    if last_evidence_ref is not None:
        record["last_evidence_ref"] = last_evidence_ref
    else:
        record.setdefault("last_evidence_ref", None)
    if wait is not None:
        # Copied, not captured: the caller keeps its own wait record and
        # may go on changing it, and a document that aliased it would
        # change under the reader's feet.
        record["wait"] = copy.deepcopy(wait)
    items[item_id] = record

    if spec_sha256 is not None:
        progress["spec_sha256"] = spec_sha256
    if not progress.get("spec_sha256"):
        progress["spec_sha256"] = _spec_fingerprint(plan_dir)
    if not progress.get("started_at"):
        progress["started_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )

    _write_progress(progress_path(plan_id), progress)
    return progress


def failed_item_ids(progress: Mapping[str, Any]) -> List[str]:
    """The ids of the items a retry round must repeat.

    Args:
        progress: A progress document, as :func:`load_progress` returns
            it (or any document with the same ``items`` shape).

    Returns:
        The ids whose status is ``failed``, in the order the document
        lists them.  Nothing else is in scope: a waiting item is not a
        failed one, and a running item has not been judged yet.
    """
    if not isinstance(progress, Mapping):
        return []
    items = progress.get("items")
    if not isinstance(items, Mapping):
        return []
    return [
        item_id
        for item_id, record in items.items()
        if isinstance(record, Mapping) and record.get("status") == FAILED_STATUS
    ]