"""Per-plan HITL hook configuration — the one reader and one writer of
``<plan_dir>/hitl_config.json``.

Why this module exists
-----------------------
A human-in-the-loop decision is only useful if somebody is actually
asked.  *Who* is asked is not a property of the backend — it is a
property of the plan: a plan created from a Claude Code session can ask
the group that session lives in, and a plan created by a ``curl`` call
has no session and therefore nobody to ask.  So the answer is stored
per plan, next to the rest of that plan's state, in
``<plan_dir>/hitl_config.json``.

The file has **exactly one** reader and one writer, and they are the
two functions in this module.  That is not tidiness: a second copy of
this logic anywhere else would drift, and the drift would show up as a
plan whose stored configuration and read-back configuration disagree —
the kind of disagreement that is invisible until a decision is asked
into the void.

There is no inheritance and no global default
---------------------------------------------
A plan's configuration is read from *its own* file or from nowhere.
There is no plan-level default that another plan inherits from, no
global ``hitl_config.json`` at the plans root, and — most importantly
— no environment variable consulted at read time.  A stray
``PDT_HITL_MODE=strict`` in an operator's shell must not change how a
plan behaves: the environment is where configuration goes to be
forgotten, and a plan's HITL behaviour is not something an ambient
process environment gets to decide.

Degradation is the default, never an exception
----------------------------------------------
Every read here answers with a complete configuration:

* no file (a plan that never had one) → ``auto`` / ``1800`` / no hooks;
* a truncated, non-JSON or otherwise torn file → the same, logged;
* a file whose ``mode`` is outside the closed vocabulary → ``auto``;
* a file whose ``timeout_sec`` is not a positive integer → ``1800``.

The absence of a file and the presence of a broken file are the same
answer, because they mean the same thing: *this plan says nothing about
its hooks*, and the safe direction from there is "ask nobody" rather
than "ask somebody the plan never named".

The document
------------
::

    {
      "version": 1,
      "mode": "auto",                    # auto | strict
      "timeout_sec": 1800,               # how long a human has to answer
      "captured_at": "2026-10-21T10:00:00Z",
      "source": "plan_creation",        # where the plan came from
      "hooks": [                         # who gets asked
        {"kind": "cmd",
         "command": "'/path/to/python3' -m backend.hooks.session_send --project grp-1 --session s-1"}
      ]
    }

``mode`` is the vocabulary of :class:`hitl.models.DecisionMode` — the
same two words the decision table stores — so the mode configured here
and the mode recorded with a decision cannot disagree about what
``strict`` means.

A hook element names its transport in ``kind`` and carries the fields
that kind needs: ``destination`` for ``cc_connect`` / ``feishu`` /
``telegram``, ``url`` for ``http``, ``command`` for ``cmd``, and an
optional per-hook ``timeout_sec``.  This module writes the ``cmd``
element a plan's origin implies — the delivery command itself, with
the interpreter that is running and the destination the plan names —
and reads back every kind, a reader that dropped ``url`` would leave
an ``http`` hook with nowhere to POST to.  Which entries are
deliverable is the dispatcher's question; this one only reports what
the plan stored.

``source`` and ``captured_at`` are provenance: they record *where* the
configuration came from and *when* it was captured.  They are written
to the file but are not part of the effective configuration that
:func:`load_for_plan` returns, because the effective configuration is
the answer to "how do I ask" — and provenance is not part of that
answer.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from hitl.models import DecisionMode
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Name of the per-plan HITL configuration file.
HITL_CONFIG_FILENAME = "hitl_config.json"

#: Schema version of the stored document.  Bumped only on an
#: incompatible change to the document shape.
SCHEMA_VERSION = 1

#: The mode a plan has when its file does not declare one — the mode
#: where a missed deadline falls back to the default answer.
DEFAULT_MODE = DecisionMode.AUTO.value

#: The decision timeout (seconds) a plan has when its file does not
#: declare one.
DEFAULT_TIMEOUT_SEC = 1800

#: The transport kind of a hook that delivers to a cc-connect group.
#: It is not written any more — the plan's hook is a command — but a
#: stored document may still carry one, so the reader passes it through
#: rather than dropping it on the floor.
HOOK_KIND_CC_CONNECT = "cc_connect"

#: The transport kind of a hook that runs a command: the delivery
#: entry point, with the plan's destination in its arguments.
HOOK_KIND_CMD = "cmd"

#: The module the delivery command runs.  It is run with ``-m``, so it
#: is importable only where the ``backend`` package is — the project
#: root — and the process that runs it must be started there.
_SESSION_SEND_MODULE = "backend.hooks.session_send"

#: The text fields a hook element may carry, in contract order.  Each
#: kind uses the ones that apply to it: ``destination`` for
#: ``cc_connect`` / ``feishu`` / ``telegram``, ``url`` for ``http``,
#: ``command`` for ``cmd``.  They are all carried through a read —
#: a reader that dropped ``url`` would leave an ``http`` hook with
#: nowhere to POST to.
_HOOK_FIELDS = ("kind", "destination", "session_id", "url", "command")

#: The optional per-hook timeout, in seconds.  The dispatcher applies
#: its own default (10s) when this is absent.
_HOOK_TIMEOUT_FIELD = "timeout_sec"


# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------


def config_path(plan_dir: PathLike) -> Path:
    """Where ``plan_dir``'s HITL configuration lives.

    The file sits directly in the plan directory, next to the rest of
    that plan's state, so deleting a plan directory deletes its hook
    configuration with it — there is no separate registry that could
    outlive the plan it belongs to.
    """
    return Path(plan_dir) / HITL_CONFIG_FILENAME


def default_config() -> Dict[str, Any]:
    """The effective configuration of a plan that says nothing.

    Returned as a fresh dict every time, so a caller that mutates the
    result cannot poison the next reader.
    """
    return {
        "version": SCHEMA_VERSION,
        "mode": DEFAULT_MODE,
        "timeout_sec": DEFAULT_TIMEOUT_SEC,
        "hooks": [],
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_for_plan(
    plan_dir: PathLike,
    origin: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Write ``plan_dir``'s HITL configuration and return what was written.

    ``origin`` describes where the plan came from.  The keys that are
    read are:

    ``source``
        What created the plan (``plan_creation`` for a plan created
        from a session, empty for one created outside one).  An empty
        source means there is no session to ask.
    ``session_id``
        The session the plan was created in, when there was one.  The
        command that delivers a decision is addressed to that session,
        so a plan without one has nowhere to send a reply to.
    ``destination``
        Where the question goes (a cc-connect group id).  A hook
        without a destination is not deliverable, so it is not a hook.
    ``mode`` / ``timeout_sec``
        Optional overrides for the two fields the document carries
        beyond the hook list.  An override outside the vocabulary
        (``mode``) or outside the positive integers (``timeout_sec``)
        is ignored in favour of the default — the write side never
        stores a value the read side would have to reject.

    The returned value is the document itself, so a caller can log or
    assert on exactly the fields a later read will see.

    The write is atomic (tempfile + ``os.replace``), so a plan killed
    mid-write leaves either the previous configuration or the new one,
    never a half-written file.

    A write that fails is **logged, not raised**.  A plan whose hook
    configuration cannot be persisted is still a plan, and a plan with
    no hooks is a state the plan already knows how to be in — its
    decisions are simply unroutable, which notifies nobody and blocks
    nothing.  A failed plan creation, on the other hand, is a plan
    that does not exist.  The failure is therefore reported loudly in
    the log and the document that was intended is returned.
    """
    origin = origin if isinstance(origin, Mapping) else {}

    source = _text(origin.get("source"))
    session_id = _text(origin.get("session_id"))
    destination = _text(origin.get("destination"))

    document: Dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "mode": _mode_of(origin.get("mode")),
        "timeout_sec": _timeout_of(origin.get("timeout_sec")),
        "captured_at": _now_iso(),
        "source": source,
        "hooks": _hooks_for(
            source=source, session_id=session_id, destination=destination
        ),
    }

    path = config_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(
            "hitl_config_dir_uncreatable: cannot create %s (%s); "
            "the configuration was not written",
            path.parent,
            type(exc).__name__,
        )
        return document

    try:
        atomic_write_json(path, document, logger=logger, reraise=True)
    except OSError as exc:
        logger.error(
            "hitl_config_unwritable: cannot write %s (%s); "
            "the plan keeps no hook configuration",
            path,
            type(exc).__name__,
        )
    return document


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load_for_plan(plan_dir: PathLike) -> Dict[str, Any]:
    """The effective HITL configuration of ``plan_dir``.

    The returned dict always has the same four keys — ``version``,
    ``mode``, ``timeout_sec`` and ``hooks`` — whatever the state of
    the file.  A missing file, an unreadable file and a file with
    values outside the vocabulary all produce a complete answer, so
    the caller has no ``None`` to check and no exception to catch.

    Nothing outside ``plan_dir``'s own file is consulted: no
    environment variable, no global configuration, no sibling plan's
    configuration.  That is what makes the answer *this plan's* answer.
    """
    document = _read_document(plan_dir)
    if document is None:
        return default_config()

    return {
        "version": _version_of(document.get("version")),
        "mode": _mode_of(document.get("mode")),
        "timeout_sec": _timeout_of(document.get("timeout_sec")),
        "hooks": _hooks_from(document.get("hooks")),
    }


def _read_document(plan_dir: PathLike) -> Optional[Dict[str, Any]]:
    """The stored document, or ``None`` when there is nothing to read.

    ``None`` covers every unusable case — absent, unreadable, not JSON,
    JSON but not an object — and every one of them means the same
    thing to the caller: *this plan stores no usable configuration*.
    """
    path = config_path(plan_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # ``ValueError`` covers ``json.JSONDecodeError`` (and
        # ``UnicodeDecodeError``, which is a subclass of it).  A file
        # torn by a crash mid-write, or written by something else,
        # costs the defaults and nothing else.
        logger.warning(
            "hitl_config_unreadable: ignoring unreadable %s (%s); "
            "using the defaults",
            path,
            type(exc).__name__,
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "hitl_config_not_an_object: ignoring %s; using the defaults", path
        )
        return None
    return data


# ---------------------------------------------------------------------------
# Field normalisation
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """A UTC ISO-8601 stamp at second precision, ``Z``-suffixed."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    """A scalar field as text; anything missing becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _mode_of(mode: Any) -> str:
    """The canonical mode string, or the default when it is not one.

    The vocabulary is :class:`hitl.models.DecisionMode` — the same two
    words the decision table stores — so a mode configured here and a
    mode recorded with a decision cannot mean different things.  An
    unknown mode is not a mode: it degrades to ``auto``, the direction
    in which a missed deadline has a fallback answer.
    """
    if mode is None:
        return DEFAULT_MODE
    try:
        return DecisionMode(mode).value
    except ValueError:
        logger.warning(
            "hitl_config_unknown_mode: ignoring mode %r; using %r",
            mode,
            DEFAULT_MODE,
        )
        return DEFAULT_MODE


def _timeout_of(timeout_sec: Any) -> int:
    """A positive integer timeout, or the default.

    ``bool`` is excluded explicitly: ``True`` is an ``int`` in Python,
    and a timeout of one second is not what anybody meant.
    """
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int):
        return DEFAULT_TIMEOUT_SEC
    if timeout_sec <= 0:
        return DEFAULT_TIMEOUT_SEC
    return timeout_sec


def _version_of(version: Any) -> int:
    """The stored schema version, or this module's version.

    The stored version is reported as-is rather than rejected: the
    fields this module reads are validated one by one, so a document
    written by a newer version still yields a usable answer for the
    fields this version understands.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        return SCHEMA_VERSION
    return version


def _hooks_for(
    *, source: str, session_id: str, destination: str
) -> List[Dict[str, str]]:
    """The hook list the plan's origin implies.

    A hook is a delivery target: it needs a session the plan came
    from, a place to ask, and the source that says the plan came from
    somewhere at all.  A plan created outside a session (a ``curl``
    call) has none of them, so it gets no hooks — and a plan with no
    hooks asks nobody, which is the correct answer rather than a gap
    to be filled in from somewhere else.

    The hook is the **command** that delivers, not a description of a
    transport: the dispatcher runs what is stored here, and a
    description it would have to interpret is one more thing that can
    disagree with the dispatcher about what a delivery is.  So the
    command is written down whole, once, when the plan is created.
    """
    if not source or not session_id or not destination:
        return []
    return [
        {
            "kind": HOOK_KIND_CMD,
            "command": _session_send_command(
                destination=destination, session_id=session_id
            ),
        }
    ]


def _session_send_command(*, destination: str, session_id: str) -> str:
    """The command that delivers a decision to ``destination``.

    The command is the delivery entry point
    (:mod:`backend.hooks.session_send`) run with the interpreter that
    is running this process.  That interpreter is not written down by
    hand: a path copied from one machine is wrong on every other one,
    and the plan's own file is the only thing that travels between
    machines.

    The two values the plan carries — where to send and in which
    session — are quoted with :func:`shlex.quote`.  A destination is
    somebody else's string, and one that looks like a shell command
    (``grp; touch /tmp/x``) has to reach the delivery CLI as one
    argument rather than become one.
    """
    return " ".join(
        [
            shlex.quote(sys.executable),
            "-m",
            _SESSION_SEND_MODULE,
            "--project",
            shlex.quote(destination),
            "--session",
            shlex.quote(session_id),
        ]
    )


def _hooks_from(hooks: Any) -> List[Dict[str, Any]]:
    """The stored hooks, normalised to the contract's field set.

    The fields an entry actually carries are kept, in contract order;
    the ones it does not carry are not invented.  An entry that is not
    an object carries no fields at all, so there is nothing to report
    about it and it is dropped.

    Which entries are *deliverable* — a known ``kind``, the fields
    that kind needs — is the dispatcher's question, not this reader's.
    This module reports what the plan stored.
    """
    if not isinstance(hooks, list):
        return []
    normalised: List[Dict[str, Any]] = []
    for entry in hooks:
        if not isinstance(entry, dict):
            continue
        hook: Dict[str, Any] = {
            field: _text(entry.get(field))
            for field in _HOOK_FIELDS
            if field in entry
        }
        timeout = _hook_timeout(entry.get(_HOOK_TIMEOUT_FIELD))
        if timeout is not None:
            hook[_HOOK_TIMEOUT_FIELD] = timeout
        normalised.append(hook)
    return normalised


def _hook_timeout(value: Any) -> Optional[int]:
    """A per-hook timeout in seconds, or ``None`` when there is none.

    A value that cannot be read as a positive number of seconds is
    dropped rather than passed on: the dispatcher's default (10s) is a
    better answer than a timeout that would raise when it is compared.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip())
        return seconds if seconds > 0 else None
    return None
