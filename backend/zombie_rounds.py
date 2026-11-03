"""The record of a verification round that was declared dead.

Why this file exists
--------------------
Declaring a verification round dead does not make it dead.  The
watchdog can look at a round whose log went silent past the hard
ceiling, see that no VP is alive, and conclude that the round is
finished -- and then the round wakes up.  The LLM call that was in
flight returns.  The sub-process exits.  The round's code continues
down its normal path and writes a terminal state, changes the plan's
phase, and pushes a card for a round that was already declared dead.

That is not a hypothetical.  It is what "the round was released but
something was still running" looks like from the outside: the plan
reports a failure that the plan has already moved past, and two
different rounds both believe they own the plan's terminal state.

The fix is a mark the round itself can consult, and the mark has to
be **durable** -- the process that wakes up is frequently not the
process that died, so an in-memory flag would not be there to be
found.

The identity is the round's own ``round_token``
------------------------------------------------
This module deliberately invents no new identity system.  The round
already carries a ``round_token`` -- the same token the plan's service
registry, the process ledger and the workflow lease all use -- and
that token is what identifies the round everywhere else.  A second
identity would have to be minted, distributed, correlated and expired;
the existing one is already in every place that needs it.

The file
--------
One JSON document per plan directory::

    {
      "version": 1,
      "rounds": {
        "verification_loop-1-ab": {
          "round_token": "verification_loop-1-ab",
          "reason": "verification_log_stale",
          "actor": "verification_watchdog",
          "at": "2026-11-01T03:40:00.123456+00:00"
        }
      }
    }

The file is a hidden (dot-prefixed) runtime artefact: it is machinery,
not plan content, and an operator browsing the plan directory should
see the plan's own artefacts rather than this.

Several rounds may be listed at once.  The store is keyed by token
precisely so that ``clear`` can remove *one* round's mark without
touching the others -- a plan can have an old dead round and a new
one, and retiring the old one must not un-kill the new one.

Degradation is the default, never an exception
----------------------------------------------
Every read here answers "nothing is invalidated" rather than raising:

* no file (a plan that never had a round declared dead) → nothing
  invalidated;
* a truncated, non-JSON, or otherwise torn file → nothing invalidated;
* a file whose shape is not the one this code wrote → nothing
  invalidated.

The reason is the module's purpose.  This file exists to stop a dead
round from acting.  If the file cannot be read, the *safe* answer is
still "this round is not known to be dead" -- but it must be an ANSWER,
not an exception: a reader that raised here would turn a damaged
bookkeeping file into a plan that cannot proceed, which is a far
worse outcome than a zombie round that gets one extra turn.  So the
reads degrade, and the writes are atomic (tempfile + ``os.replace``)
so a crash mid-write leaves the previous document rather than a torn
one.

The same reasoning governs the writes: a write that fails is logged
and swallowed.  The watchdog's death sequence continues to the terminal
state whether or not this bookkeeping succeeded -- an unreachable
marker must not be the reason the plan never learns that its round
died.

Scope
-----
This module is the marker: the write, the read, the lookup and the
clear.  It does not kill anything, does not know what a verification
round is, and does not read any plan state.  The watchdog decides a
round is dead and calls :func:`invalidate`; the code that might wake
up calls :func:`is_invalidated` before it acts.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Name of the per-plan zombie-round file inside a plan directory.
ZOMBIE_ROUNDS_FILENAME = ".verification_zombie_rounds.json"

#: Bumped only on an incompatible change to the document shape.  A
#: document written by a newer PDT is treated as unreadable by an older
#: one, which costs "nothing is invalidated" rather than a misread.
_SCHEMA_VERSION = 1

#: Serialises the read-modify-write sequences within this process.  The
#: watchdog thread and the round thread can both touch the file, and a
#: lost update would resurrect a mark that was just written.
_LOCK = threading.RLock()


def zombie_rounds_path(plan_dir: PathLike) -> Path:
    """Where ``plan_dir``'s zombie-round file lives."""
    return Path(plan_dir) / ZOMBIE_ROUNDS_FILENAME


def _now_iso() -> str:
    """A timezone-aware ISO-8601 stamp, as written by this module.

    ``datetime.now(timezone.utc)`` rather than the older ``utcnow()``:
    same wall clock, no deprecation warning, and the offset is explicit
    in the stored value.
    """
    return datetime.now(timezone.utc).isoformat()


def _read_rounds(plan_dir: PathLike) -> Dict[str, Dict[str, Any]]:
    """Return ``{round_token: record}`` -- ``{}`` when nothing is usable.

    Every failure mode -- absent file, unreadable file, non-JSON file,
    JSON that is not an object, a document from a newer schema -- lands
    on ``{}``.  See the module docstring: an unreadable marker must
    never be the reason a plan cannot proceed.
    """
    path = zombie_rounds_path(plan_dir)
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError``.  A file torn by
        # a crash, or written by something else, means "no known dead
        # round" -- never an exception.
        logger.warning(
            "zombie_rounds_unreadable: ignoring unreadable marker file %s "
            "(%s); treating every round as live",
            path, type(exc).__name__,
        )
        return {}
    if not isinstance(document, dict):
        logger.warning(
            "zombie_rounds_malformed: marker file %s is not an object; "
            "treating every round as live", path,
        )
        return {}
    if document.get("version") != _SCHEMA_VERSION:
        logger.warning(
            "zombie_rounds_version_mismatch: ignoring marker file %s with "
            "version %r; treating every round as live",
            path, document.get("version"),
        )
        return {}
    rounds = document.get("rounds")
    if not isinstance(rounds, dict):
        logger.warning(
            "zombie_rounds_malformed: marker file %s has no rounds mapping; "
            "treating every round as live", path,
        )
        return {}
    return _normalise_rounds(rounds)


def _normalise_rounds(rounds: Dict[Any, Any]) -> Dict[str, Dict[str, Any]]:
    """Reduce the stored mapping to the fixed four-key record shape.

    A record that is not an object, or a token that is not a non-empty
    string, is dropped: it cannot be matched against a round's token,
    so keeping it would only make the file harder to reason about.
    """
    normalised: Dict[str, Dict[str, Any]] = {}
    for token, record in rounds.items():
        if not isinstance(token, str) or not token.strip():
            continue
        if not isinstance(record, dict):
            continue
        normalised[token] = {
            "round_token": str(record.get("round_token") or token),
            "reason": str(record.get("reason") or ""),
            "actor": str(record.get("actor") or ""),
            "at": str(record.get("at") or ""),
        }
    return normalised


def _write_rounds(plan_dir: PathLike, rounds: Dict[str, Dict[str, Any]]) -> None:
    """Write the document atomically.

    The write goes through :func:`utils.atomic_io.atomic_write_json`
    (tempfile + ``os.replace``), so a process killed mid-write leaves
    the previous document intact rather than a torn one.

    A write that fails is logged and swallowed: the caller is usually a
    watchdog on its way to writing a terminal state, and an
    unreachable bookkeeping file must not stop that.  A caller that
    needs to know whether the mark stuck asks
    :func:`is_invalidated` afterwards.
    """
    path = zombie_rounds_path(plan_dir)
    document = {"version": _SCHEMA_VERSION, "rounds": rounds}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(
            "zombie_rounds_unwritable: cannot create %s (%s); the round was "
            "not marked as dead", path.parent, type(exc).__name__,
        )
        return
    try:
        atomic_write_json(path, document, logger=logger, reraise=True)
    except OSError as exc:
        logger.error(
            "zombie_rounds_write_failed: could not write %s (%s); the round "
            "was not marked as dead", path, type(exc).__name__,
        )


def invalidate(
    plan_dir: PathLike,
    round_token: str,
    *,
    reason: str,
    actor: str,
) -> None:
    """Declare ``round_token`` dead, and remember why and who said so.

    The mark is the round's identity (``round_token``) plus the
    ``reason`` it was declared dead for and the ``actor`` that declared
    it -- a watchdog, an operator, a lease steal.  Those two fields are
    not decoration: when a plan later reports a state nobody expected,
    "which round was killed, and by what rule" is the first question,
    and the answer has to survive the process that wrote it.

    Declaring the same round dead twice is a **no-op**: the second call
    changes nothing, not even the timestamp, so the record keeps
    saying when the death was first observed.

    An empty token names no round, so there is nothing to declare
    dead; the call is ignored (with a warning) rather than raising,
    because the caller is normally a watchdog on a death path and must
    not be stopped by a missing token.

    This call never raises, not even when the mark cannot be written:
    a watchdog that is on its way to recording the round's death must
    not be stopped by a bookkeeping file it could not write.  A caller
    that needs to know whether the mark stuck asks
    :func:`is_invalidated` afterwards.
    """
    token = str(round_token or "").strip()
    if not token:
        logger.warning(
            "zombie_rounds_no_token: ignoring invalidation with an empty "
            "round token (actor=%s reason=%s)", actor, reason,
        )
        return
    with _LOCK:
        rounds = _read_rounds(plan_dir)
        if token in rounds:
            # Already dead.  Idempotent on purpose: the first record is
            # the one that describes the death.
            return
        rounds[token] = {
            "round_token": token,
            "reason": str(reason or ""),
            "actor": str(actor or ""),
            "at": _now_iso(),
        }
        _write_rounds(plan_dir, rounds)


def is_invalidated(plan_dir: PathLike, round_token: str) -> bool:
    """Has ``round_token`` been declared dead?

    ``False`` for a token that was never declared dead, for an empty
    token, and for a plan whose marker file cannot be read.  The last
    one is the important one: an unreadable marker means "not known to
    be dead", not "the caller must stop".
    """
    token = str(round_token or "").strip()
    if not token:
        return False
    with _LOCK:
        rounds = _read_rounds(plan_dir)
    return token in rounds


def invalidated_round(plan_dir: PathLike) -> Optional[Dict[str, Any]]:
    """The most recently invalidated round, or ``None``.

    The record carries ``round_token``, ``reason``, ``actor`` and
    ``at``.  ``None`` means nothing is known to be dead -- an empty
    plan, a plan whose marker file is unreadable, or a plan whose only
    marks have been cleared.

    When several rounds are listed, the most recent one wins: it is the
    death the plan is currently dealing with.  The comparison is on the
    stored ISO-8601 stamp, with the later-listed record breaking a
    tie.
    """
    with _LOCK:
        rounds = _read_rounds(plan_dir)
    if not rounds:
        return None
    latest: Optional[Dict[str, Any]] = None
    latest_at = ""
    for record in rounds.values():
        at = str(record.get("at") or "")
        if latest is None or at >= latest_at:
            latest = record
            latest_at = at
    return dict(latest) if latest is not None else None


def clear(plan_dir: PathLike, round_token: str) -> None:
    """Remove the mark of ``round_token`` -- and only that one.

    Used when a mark has done its job: the plan has moved on and the
    dead round is no longer interesting.  Clearing a token that was
    never marked is a no-op, and clearing one round never touches
    another round's mark -- a plan can hold the marks of several dead
    rounds at once, and retiring the old one must not un-kill the new
    one.
    """
    token = str(round_token or "").strip()
    if not token:
        return
    with _LOCK:
        rounds = _read_rounds(plan_dir)
        if token not in rounds:
            return
        rounds.pop(token)
        _write_rounds(plan_dir, rounds)