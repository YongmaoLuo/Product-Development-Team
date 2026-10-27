"""The pinned key → real session id mapping.

Why this module exists
======================

Two ids answer to the same question — "which conversation is this?" —
and they are not the same id.

The **pinned** id is the caller's own stable handle on the work: a
uuid5 derived from the plan and the task, so the same task always
looks up the same entry. It is written down *before* the run starts,
because that is the only way a later process can find the work again
after the first one died.

The **observed** id is the conversation the CLI really created. A CLI
that can be asked to create a conversation under a chosen id hands
that id back unchanged; a CLI that cannot (``mcode exec`` has no
``--session-id``) mints an id of its own and reports it in the run's
events. Only the CLI knows it.

The mapping between the two is what makes a resume possible at all,
and it has to outlive the process that created it: the run that
creates the conversation and the later run that resumes it are not the
same process.

Why it is not in the adapter
============================

The mapping is a *production* concern, not an adapter concern. The
layer that creates a session and the layer that resumes it are both
above the adapters, and the claude path must not have to import the
mcode adapter to get there. So the mapping lives here, in a module
that knows nothing about any adapter, and :mod:`harness.mcode`
re-exports it for the callers that already import from there.

What the mapping is not
=======================

It is not a cache and not a registry: it is a file, and the file is
the whole observable. The caller gives the path — the mapping belongs
next to the task's own persisted session, the same boundary, and only
the caller knows where that is.

The order of the two operations is the contract:

* **register first, hand the id out second.** A crash *after* the
  registration can only leave a conversation nobody references — an
  orphan, which costs a little money and nothing else. The other
  order leaves an observed id that no later process can resolve back
  to a conversation, and that is unrecoverable.
* **a miss is a miss.** ``lookup`` answers ``None`` for "this mapping
  does not exist"; the caller turns that into an explicit create. It
  must never resolve the miss to some *other* conversation: a resume
  that silently continues the wrong session is worse than one that
  starts a new one.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional, Union

from config_paths import resolve_plans_dir
from harness.contract import HarnessConfigError
from utils.atomic_io import atomic_write_json

__all__ = [
    "MCODE_SESSION_ID_RE",
    "SESSION_MAP_FILENAME",
    "SessionIdNotResumable",
    "SessionMap",
    "plan_session_map",
    "plan_session_map_path",
]

logger = logging.getLogger(__name__)


class SessionIdNotResumable(HarnessConfigError):
    """The id names no conversation this backend can re-open.

    A backend that can be asked to create a conversation under a chosen
    id honours that id when it is asked to re-open one too. A backend
    that cannot — it mints the id of every conversation itself — can
    only re-open a conversation whose id it minted, and handing it
    anything else is a request it cannot answer.

    The answer is this error rather than a guess. A resume that quietly
    started a *different* conversation would be worse than one that
    failed: the caller would believe it is continuing the work and would
    be wrong.

    It is a :class:`HarnessConfigError` because that is what it is — the
    dispatch asked for something this backend cannot do — and it is its
    own type so the layer above can tell it apart from the other
    configuration problems (a model that names no provider, an MCP
    configuration that is not JSON). Those are not about the session at
    all, and answering them with a fresh session would hide a
    misconfiguration behind a second dispatch.
    """


#: A path, spelled the way a caller spells it.
PathLike = Union[str, Path]

#: The shape of a real MiniMax Code session id, as measured on the
#: CLI: the ``mvs_`` prefix and 32 hex digits — a uuid without its
#: dashes. An id that does not have this shape was not minted by the
#: CLI, and ``--session`` cannot be given one.
#:
#: It lives here rather than in the adapter because the mapping
#: validates what it stores, and the mapping is used by the layer
#: above the adapters. :mod:`harness.mcode` re-exports it, so the
#: name still resolves where it always did.
MCODE_SESSION_ID_RE = re.compile(r"^mvs_[0-9a-f]{32}$")

#: Name of the per-plan mapping sidecar.
#:
#: It sits beside the plan's own session sidecar (``.pdt-sessions.json``)
#: because it answers the same question about the same boundary: which
#: conversation is this task's? The leading dot keeps it out of every
#: directory listing that walks a plan directory.
SESSION_MAP_FILENAME = ".pdt-session-map.json"


def plan_session_map_path(plan_id: str) -> Path:
    """Where the mapping for ``plan_id``'s sessions lives.

    The path is *derived*, not configured: a plan's sessions belong to
    the plan, and the plan's directory is where its other per-plan
    state already lives.

    Args:
        plan_id: The plan the mapping belongs to.

    Returns:
        The path of the mapping file.
    """
    return resolve_plans_dir() / str(plan_id) / SESSION_MAP_FILENAME


def plan_session_map(plan_id: Optional[str]) -> Optional["SessionMap"]:
    """The mapping for ``plan_id``, or ``None`` when there is nowhere to put it.

    ``None`` means "no mapping is possible here": the dispatch belongs
    to no plan, or the plan's directory does not exist yet. A mapping
    file is never a reason to *create* a plan directory — a directory
    that only holds a mapping is a plan the operator never started,
    and the sweep that lists plan directories would report it as one.

    Args:
        plan_id: The plan the mapping belongs to, if any.

    Returns:
        The mapping, or ``None`` when there is nowhere to keep it.
    """
    if not plan_id:
        return None
    path = plan_session_map_path(plan_id)
    if not path.parent.is_dir():
        return None
    return SessionMap(path)


def _write_mapping(path: Path, mapping: Dict[str, str]) -> None:
    """Write the mapping to ``path``, atomically and privately.

    The write goes to a temporary file in the same directory and is
    moved into place with ``os.replace``, so a reader never sees half
    a mapping — a run that reads the file while another one writes it
    sees the old mapping or the new one, never a torn one. The
    temporary file is created with mode 0600 and the replacement keeps
    it: the mapping names the conversations a task belongs to, and it
    is not a file to share.

    Args:
        path: Where the mapping lives.
        mapping: The mapping to write.

    Raises:
        OSError: The mapping could not be written. The failure is
            raised rather than swallowed: a caller that cannot record
            the mapping must not go on to hand out an id that no later
            process could resolve back to a conversation.
    """
    atomic_write_json(path, mapping, reraise=True, logger=logger)


class SessionMap:
    """The mapping from a pinned session key to the real session id.

    A CLI that cannot be asked to create a conversation under an id the
    caller chose makes the two ids different things. The **pinned**
    key is the caller's own stable handle on the task — a uuid5
    derived from the plan and the task, so the same task always looks
    up the same entry. The **observed** id is the conversation the CLI
    really created, and only the CLI knows it.

    This class is the seam between the two, and it is a file rather
    than a dictionary because the two ids are needed in different
    processes: the run that creates the conversation and the later run
    that resumes it are not the same process.

    The caller gives the path. The mapping belongs next to the task's
    own persisted session — the same boundary — and only the caller
    knows where that is.

    Attributes:
        path: The file the mapping lives in.
    """

    def __init__(self, path: PathLike) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file the mapping lives in."""
        return self._path

    def register(self, pinned: str, observed: str) -> None:
        """Record ``observed`` as the real session behind ``pinned``.

        The mapping is on disk before this call returns, so the caller
        may hand the observed id on as soon as the call is done. The
        order is the whole point: a crash *after* the registration can
        only leave a conversation nobody references — an orphan, which
        costs a little money and nothing else — while the other order,
        handing the id out before it was recorded, leaves an observed
        id that no later process can resolve back to a conversation.

        A later registration for the same key replaces the earlier
        one: the most recent successful create is the conversation the
        task is in, and the conversation before it becomes an orphan.

        Args:
            pinned: The key the caller pinned.
            observed: The real session id the CLI minted.

        Raises:
            ValueError: The pinned key is empty, or the observed id is
                not shaped like a real MiniMax Code session id.
            OSError: The mapping could not be written.
        """
        if not isinstance(pinned, str) or not pinned.strip():
            raise ValueError(
                f"a pinned session key must be a non-empty string, got {pinned!r}"
            )
        if (
            not isinstance(observed, str)
            or not MCODE_SESSION_ID_RE.fullmatch(observed)
        ):
            raise ValueError(
                f"{observed!r} is not a MiniMax Code session id; the CLI "
                f"mints them as {MCODE_SESSION_ID_RE.pattern}"
            )

        mapping = self._read()
        mapping[pinned] = observed
        self._write(mapping)

    def _write(self, mapping: Dict[str, str]) -> None:
        """Put the mapping on disk.

        The write goes through a method rather than straight to the
        module's helper so that the adapter which owns the id shape
        can install its own writer without this module having to know
        what an adapter is. The installed writer is resolved when the
        write happens, so a caller that replaces it sees its own
        function called.

        Args:
            mapping: The mapping to write.

        Raises:
            OSError: The mapping could not be written.
        """
        _write_mapping(self._path, mapping)

    def lookup(self, pinned: str) -> Optional[str]:
        """The real session id behind ``pinned``, or ``None``.

        ``None`` means "this mapping does not exist" — the task has
        never created a conversation, or the mapping was lost. The
        caller above turns that into an explicit create; it must never
        resolve the miss to some *other* conversation, because a resume
        that silently continues the wrong session is worse than one
        that starts a new one.

        Args:
            pinned: The key the caller pinned.

        Returns:
            The real session id, or ``None`` when there is no mapping.
        """
        if not isinstance(pinned, str) or not pinned:
            return None
        return self._read().get(pinned)

    def _read(self) -> Dict[str, str]:
        """The mapping on disk, or ``{}`` when there is nothing usable.

        Every way of not having a mapping reads the same: an absent
        file, an unreadable one, one that is not JSON, one that is not
        an object. A file that *was* written and then became unreadable
        is not the same thing as one that was never written, so it is
        logged rather than passed over in silence.

        A value that is not a real session id is dropped for the same
        reason: it could not be given to ``--session``, so resolving to
        it would be resolving to a conversation that does not exist.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "session_map_unreadable: ignoring unreadable mapping %s (%s)",
                self._path,
                type(exc).__name__,
            )
            return {}

        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning(
                "session_map_unparsable: ignoring mapping that is not JSON: %s",
                self._path,
            )
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "session_map_malformed: ignoring mapping that is not an "
                "object: %s",
                self._path,
            )
            return {}

        mapping: Dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if not MCODE_SESSION_ID_RE.fullmatch(value):
                logger.warning(
                    "session_map_entry_ignored: %s maps a key to something "
                    "that is not a session id",
                    self._path,
                )
                continue
            mapping[key] = value
        return mapping
