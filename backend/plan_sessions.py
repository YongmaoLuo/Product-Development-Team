"""Cross-request session reuse for the planning phases.

What this is for
----------------
Reuse inside a single ``generate()`` is already wired: the generation and
its self-review share one :class:`~coding_tool.JsonSession` handle, which
travels on a contextvar. What that handle cannot cover is the turn that
happens *after* the request that created it is gone — a whole-document
revise, which is a separate HTTP request against a separate
:class:`~arch_refiner.ArchRefiner` instance, and an interview turn that
survives a service restart. By the time either runs, the Python object
holding the id has been garbage-collected along with the process that
made it, and the reviser has no choice but to paste the entire document
back into the prompt.

This module is the missing half: a tiny per-plan sidecar that carries the
session id across that boundary. The generation writes it; the refiner
reads it back and re-opens the same conversation.

Why a sidecar and not more in-process state
-------------------------------------------
An in-process registry keyed by plan directory would not fix the bug. The
two turns are not in the same process — the whole point of the case is
that they are not — and a registry survives exactly as long as the handle
it was meant to outlive. The interview already carries such a registry
(:func:`interviewer.interview_session`); it is the right shape for a
loop *inside* one process and the wrong shape for this one.

Where the sidecar lives, and why nothing shows it to the user
------------------------------------------------------------
``<plan_dir>/.pdt-sessions.json``. Three properties of that choice are
load-bearing, each checked against the code that reads plan directories:

1. Every enumeration of ``PLANS_DIR`` filters on ``is_dir()`` before it
   builds a row — ``routes/plans.py`` (the ``/api/plans`` sidebar),
   ``routes/execution.py`` (startup recovery) and
   ``state_machine/db/archive_scan.py`` (the archived-plan listing) all
   skip files outright, so a file inside a plan directory never becomes a
   row in any of them.
2. The artifact status a plan reports comes from
   ``state_machine/db/artifact_scan.py``, which is an explicit allowlist
   of four filenames — an unlisted file is invisible to it by
   construction.
3. The one place a plan directory's files are walked and shown is
   ``/api/execution/{plan_id}/files``, and that walks ``project_dir`` —
   the generated software's own working tree, not the plan directory.

The leading dot is belt and braces on top of those: a file named
``.pdt-sessions.json`` is invisible to every ``ls``-shaped view of a plan
directory even if one is added later. The one contract it must not break
is ``tests/static_gates/test_e2e_lifecycle_produces_no_state_json.py``,
which walks a plan directory and rejects five legacy state-JSON names —
none of which this file is.

Degradation is the default, never an error
------------------------------------------
Every read and write here swallows its failure and answers with the
behaviour the caller had before this module existed:

* no sidecar (a plan generated before it existed, or one whose generation
  never ran) → an empty handle → a fresh session, exactly as before;
* a corrupt, truncated, non-JSON or wrong-shaped sidecar → the same empty
  handle;
* an id that has since been pruned or never existed → the CLI refuses
  the ``--resume``, and ``coding_tool._resume_or_fresh_session`` already
  degrades that to the fresh-session call it replaces.

A revise that cannot resume is no worse than a revise that never tried;
that is the whole design, and it is why none of these paths raise.

The one thing that needs no sidecar
-----------------------------------
:func:`stable_session_id` derives a session id from the plan and the
step alone. There is nothing to persist — the same inputs always give
the same id — so it lives here, next to the sidecar code that shares
its subject, and the modules that need it import it from here.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Union

from coding_tool import JsonSession
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Name of the per-plan sidecar. Leading dot so no directory listing that
#: a human or an agent reads shows it; see the module docstring for the
#: three code paths that would otherwise have to be taught to skip it.
SESSIONS_FILENAME = ".pdt-sessions.json"

#: Bumped only if the on-disk shape changes incompatibly. A sidecar
#: written by a newer PDT is treated as unreadable (and therefore as
#: "no id") by an older one, which is the safe direction: it costs a
#: fresh session, not a wrong session.
_SCHEMA_VERSION = 1

#: Sidecar keys, one per conversation a plan can carry across requests.
KEY_INTERVIEW = "interview"
KEY_PRD = "prd"
KEY_ARCH = "arch"
KEY_TEST_DESIGN = "test_design"

#: One lock per sidecar path, so two phases of the same plan being
#: refined concurrently cannot lose each other's entry. Process-local by
#: construction — the read-modify-write is a single ``json`` file and a
#: second PDT process refining the same plan at the same instant is not a
#: shape this codebase produces. The failure mode if it ever happens is
#: one key reverting to the id it had a moment ago, which the next
#: refine recovers from.
_WRITE_LOCKS: Dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def sessions_path(plan_dir: PathLike) -> Path:
    """Where ``plan_dir``'s sidecar lives."""
    return Path(plan_dir) / SESSIONS_FILENAME


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _read_sessions(plan_dir: PathLike) -> Dict[str, Any]:
    """The sidecar's ``sessions`` mapping, or ``{}`` if unusable.

    Every failure mode — absent, unreadable, not JSON, JSON but not an
    object, an object without a ``sessions`` mapping — lands on ``{}``,
    which reads exactly like "this plan has never been generated".
    """
    path = sessions_path(plan_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError``. A sidecar torn
        # by a crash mid-write, or written by something else, costs a
        # fresh session and nothing else.
        logger.warning(
            "plan_sessions_unreadable: ignoring unreadable session "
            "sidecar %s (%s); this phase runs in a fresh session",
            path, type(exc).__name__,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != _SCHEMA_VERSION:
        # A sidecar whose schema version is not the one this code wrote.
        # ``_SCHEMA_VERSION`` has only ever been 1, so in practice this
        # is a *newer* PDT's file, and answering "no id" costs a fresh
        # session rather than a wrong one.
        return {}
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def load_session_id(plan_dir: PathLike, key: str) -> Optional[str]:
    """The session id ``key`` last recorded for ``plan_dir``, if any.

    ``None`` means "open a fresh one" — which is also what a plan that
    has never been generated, a plan whose sidecar is unreadable and a
    plan whose recorded id is not a non-empty string all answer.
    """
    value = _read_sessions(plan_dir).get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def save_session_id(plan_dir: PathLike, key: str, session_id: Optional[str]) -> None:
    """Record ``session_id`` under ``key``, leaving other keys alone.

    Idempotent: re-recording the id a ``generate()`` already recorded is
    a no-op, not a second write. The write goes through
    :func:`utils.atomic_io.atomic_write_json`, so a crash mid-write
    leaves the previous sidecar intact rather than a truncated one.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return
    session_id = session_id.strip()
    path = sessions_path(plan_dir)
    with _lock_for(path):
        sessions = _read_sessions(plan_dir)
        if sessions.get(key) == session_id:
            return
        sessions[key] = session_id
        atomic_write_json(
            path,
            {"version": _SCHEMA_VERSION, "sessions": sessions},
            logger=logger,
        )


def persisted_session(plan_dir: PathLike, key: str) -> JsonSession:
    """A handle for ``key``, seeded with whatever ``plan_dir`` recorded.

    The returned handle is never ``None``, so a caller can bind it
    unconditionally. An unrecorded (or unreadably recorded) key gives an
    *empty* handle, and an empty handle dispatches exactly the fresh
    session it dispatched before this module existed — that is the whole
    of the degradation path, and it is why a refiner needs no
    ``try``/``except`` around its read.
    """
    return JsonSession(load_session_id(plan_dir, key))


def persist_session(
    plan_dir: PathLike, key: str, session: Optional[JsonSession]
) -> None:
    """Write back whatever ``session``'s last dispatch actually used.

    Called *after* the dispatch rather than before the next one, because
    the id a loop should resume is the one that produced the answer: a
    provider fallback may have switched it, and a resume that fell back to
    a fresh session reports the new id. Persisting on the failure path
    too is deliberate — the tool writes the id back into the handle at
    every dispatch, so a call that raised after dispatching still leaves
    behind a session that exists and is worth re-asking in.
    """
    if session is None:
        return
    save_session_id(plan_dir, key, getattr(session, "session_id", None))


def stable_session_id(plan_id: str, step: str) -> str:
    """A deterministic session id for ``plan_id``'s ``step``.

    2026-10-22. The id has to be *stable* — the same plan and the same
    step must always land on the same session — so that a ``/start``
    after a ``/stop`` can re-open the conversation the stopped round
    was having, instead of starting a new one and paying for the whole
    context again.

    The CLI constrains the shape: ``--session-id`` wants a canonical
    dashed UUID (8-4-4-4-12 hex). A readable ``f"{plan_id}-{step}"`` is
    rejected with ``Error: Invalid session ID``, so the readable part
    is hashed into one — ``uuid.uuid5`` is deterministic (the same name
    always gives the same id), and it produces exactly the shape the
    CLI accepts.

    Why it lives here
    -----------------
    A session id is this module's subject, and the task-execution path
    needs the same id: ``agent`` must be able to ask for "the session
    for this plan's task 9" without importing ``repair_generator``,
    which would drag the verification and test-quality stack into every
    task. So the function lives here and ``repair_generator`` re-exports
    it — one object, two import paths, no drift.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdt:{plan_id}:{step}"))
