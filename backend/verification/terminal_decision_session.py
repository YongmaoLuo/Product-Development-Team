"""The sidecar file that carries a terminal decision's session and analysis.

Why this module exists
======================

When a verification round stops and asks a human what to do next, the
answer is produced by an **analysis session** — a conversation with a
model that read the failure scene and wrote a recommendation.  Two
things have to survive the round: the **session id** (which conversation
it was, so the session can be resumed) and the **analysis context**
(what the model was shown, so the answer can be re-read next to the
question it answers).

The decision itself lives in the state database.  But a session id and
a free-form analysis context are not decision fields, and adding
columns for them would change the decision storage schema — which this
work does not do (arch decision point 8).  So they are parked in a
**sidecar**: one JSON file next to the plan,
``<plan_dir>/terminal_decision_session.json``.

A sidecar, not a table
======================

The file is deliberately *not* part of the state database:

* **The decision stays where it is.**  Nothing here writes to
  ``state.db``, nothing here changes a table, nothing here changes the
  schema.  A plan whose sidecar is missing behaves exactly as it did
  before this file existed.
* **It is per-plan, not global.**  It sits in the plan directory next
  to the other plan artefacts, so it is copied, archived and inspected
  with the plan it belongs to.

What this module guarantees
===========================

**The write is atomic.**  The bytes are written to a sibling
``.terminal_decision_session.json.tmp`` file, flushed, and then moved
onto the final path with :func:`os.replace`.  A reader of the final
path therefore sees either the previous record or the complete new one
— never a truncated one.  The staging file is removed if anything goes
wrong, so a failed write does not leave litter next to the plan.

**The read never raises.**  A missing file, a corrupt file, a file that
is not a JSON object, and a file written under a schema version this
module does not know all answer the same way: ``None``.  The reader is
trying to *resume* a session; a session it cannot read is a session it
does not have, and that is a normal state to be in (arch decision
point 5: read it or degrade, never raise).

**The record's shape is fixed.**  :func:`build_record` always produces
the same nine keys, with the same defaults.  A reader that knows the
schema can rely on the keys being there; a file written by a future
version is refused rather than misread.

**A missing session id is not a missing context.**  When no session id
could be obtained, the record carries ``null`` for it and keeps the
analysis context intact — the analysis is still worth having, it just
cannot be resumed.

What this module deliberately does NOT do
=========================================

It imports no orchestrator, no routes and no server — only the standard
library.  The orchestrator will import *this* module; an import the
other way round would be a cycle, and a file helper that pulls the
server in cannot be exercised on its own.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "SCHEMA_VERSION",
    "SESSION_FILENAME",
    "build_record",
    "load",
    "save",
    "session_path",
]


#: The sidecar's filename, next to the plan's other artefacts.
SESSION_FILENAME = "terminal_decision_session.json"

#: The record schema this module reads and writes.
#:
#: A file carrying any other version is **refused** rather than read on
#: a best-effort basis: a record whose meaning changed underneath the
#: reader is worse than no record at all.
SCHEMA_VERSION = 1

#: The staging file's suffix.  The staging file lives next to the
#: target and is hidden, so the swap is a rename within one filesystem
#: and the plan directory does not show a second record.
_TMP_SUFFIX = ".tmp"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def session_path(plan_dir: Any) -> Path:
    """The sidecar's path inside ``plan_dir``."""
    return Path(plan_dir) / SESSION_FILENAME


def _tmp_path(target: Path) -> Path:
    """The staging path for ``target`` — a hidden sibling of it."""
    return target.parent / f".{target.name}{_TMP_SUFFIX}"


def _discard(path: Path) -> None:
    """Remove ``path``, ignoring the fact that it may not be there.

    Used on the failure path of the write: the staging file must not
    survive a failed write, but the failure that is being reported is
    the original one, not a failure to clean up.
    """
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def build_record(
    *,
    decision_id: str,
    plan_id: str,
    analysis: Any = None,
    session_id: Optional[str] = None,
    failed_points: Any = None,
    stop_reason: str = "repair_execution_failed",
    round_number: int = 0,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the record this module writes.

    The nine keys are fixed, so a reader can rely on them:

    ``schema_version``
        always :data:`SCHEMA_VERSION`.
    ``decision_id`` / ``plan_id``
        which decision and which plan this belongs to.
    ``session_id``
        the analysis session to resume, or ``None`` when there is none.
    ``analysis``
        the analysis context, stored **as given** — this module does not
        re-shape it and does not summarise it.
    ``failed_points``
        the failed verification points the analysis was read against.
    ``stop_reason``
        why the round stopped.
    ``round_number``
        which round stopped.
    ``created_at``
        when the record was built; the current local time unless the
        caller stamps its own.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "plan_id": plan_id,
        "session_id": session_id,
        # The analysis is kept verbatim; only its absence is normalised
        # to an empty context, so the key's type never changes.
        "analysis": analysis if analysis is not None else {},
        # The list is copied so a caller mutating its own list cannot
        # change a record that was already built.
        "failed_points": list(failed_points) if failed_points is not None else [],
        "stop_reason": stop_reason,
        "round_number": round_number,
        "created_at": (
            created_at if created_at is not None else datetime.now().isoformat()
        ),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save(plan_dir: Any, record: Mapping[str, Any]) -> Path:
    """Write ``record`` to the sidecar in ``plan_dir``; return its path.

    The write is atomic: the bytes go to a hidden staging file next to
    the target and are then renamed onto it.  A reader never sees half
    a record, and the staging file is gone afterwards — whether the
    write succeeded or failed.

    A failure here is **not** swallowed: a record that could not be
    written is a fact the caller has to hear about.  (Only the *read*
    side degrades.)
    """
    target = session_path(plan_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = dict(record)
    # A record built elsewhere may not carry the version; fill it in.
    # An explicitly wrong version is left alone — the reader refuses it.
    payload.setdefault("schema_version", SCHEMA_VERSION)

    # Serialised before the staging file is created: an unserialisable
    # payload then leaves nothing behind at all.
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    staged = _tmp_path(target)
    try:
        with open(staged, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    except BaseException:
        _discard(staged)
        raise

    return target


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load(plan_dir: Any) -> Optional[Dict[str, Any]]:
    """Read the sidecar in ``plan_dir``; return ``None`` if there is none.

    ``None`` answers every "there is nothing usable here" case: the
    file is missing, it is not readable, it is not JSON, it is not a
    JSON object, or it was written under a schema version this module
    does not know.  Nothing here raises.
    """
    path = session_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, Mapping):
        return None

    if data.get("schema_version") != SCHEMA_VERSION:
        return None

    return dict(data)