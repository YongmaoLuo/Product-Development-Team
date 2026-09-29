"""Wire protocol and path canonicalisation for the file-lock broker.

Why this module is separate
---------------------------
Two very different processes speak this protocol:

* the **broker** (:mod:`file_lock_broker`), which runs inside the
  long-lived executor and actually holds the OS locks, and
* the **Edit/Write hook** (:mod:`file_lock_cli`), which is a short-lived
  child of the *sub-agent* and runs under whatever ``python3`` is on
  ``PATH`` — not necessarily this project's virtualenv.

The hook's interpreter therefore cannot be assumed to have ``filelock``
(or any third-party package) available. Keeping the protocol here, with
**standard library imports only**, is what lets both ends share a single
definition of the wire format instead of the hook carrying its own copy
of it. :mod:`file_lock_broker` imports this module and adds ``filelock``
on top; nothing in here may import back.

Why canonicalisation matters
----------------------------
``FileLockManager`` keys a lock file by ``sha256(normpath(path))`` of
whatever string it was handed. The executor hands it *project-relative*
paths (``backend/agent.py``, from a task's ``files_to_modify``), while
the Edit/Write hook only ever sees *absolute* ones from its tool input.
Hashing those raw strings yields two different lock files for one real
file — two sub-agents that each believe they hold the lock, and then
edit the same file concurrently. That is precisely the outcome the lock
exists to prevent, so every path is canonicalised to a project-relative
form *before* it is hashed and both ends go through
:func:`normalize_target`.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

PathLike = Union[str, Path]

#: Environment variable through which the socket location is published to
#: the hook. Set by ``coding_tool`` when it renders the sub-agent's
#: settings, read by ``file_lock_cli``.
SOCKET_ENV_VAR = "PDT_LOCK_BROKER"

#: Root the derived lock directory and socket are placed under, overriding
#: the system temp root when set.
#:
#: The derived locations are keyed by **workspace digest** — one lock
#: directory per workspace the machine has ever run against. That is what
#: the protocol wants (every process agrees without being told), and it is
#: also why the default root accumulates: a test suite that gives every
#: case its own ``tmp_path`` mints one directory per case, in the machine's
#: temp root, for workspaces that were deleted seconds later.
#:
#: This override is the way a harness keeps its cases' lock state inside
#: the case. Same shape as ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``: the
#: default stays machine-wide for production, and the suite points it at
#: something it owns and cleans up.
LOCK_ROOT_ENV_VAR = "PDT_LOCK_ROOT"


def _derived_root() -> Path:
    """Root for workspace-derived lock state: the override, else temp.

    Read on each call rather than cached at import, so a harness that sets
    the variable in a fixture still takes effect for code imported earlier.
    """
    override = os.environ.get(LOCK_ROOT_ENV_VAR, "").strip()
    return Path(override) if override else Path(tempfile.gettempdir())

#: Name of the directory, inside a plan directory, that holds lock files.
LOCKS_SUBDIR = "locks"

#: Basename prefix for the derived socket path.
_SOCKET_PREFIX = "pdt-lock-"

#: Verdicts returned by :func:`acquire`. Strings rather than an enum so
#: the value survives a JSON round-trip without a decoder table.
ACQUIRED = "acquired"
ALREADY_HELD = "already"
TIMED_OUT = "timeout"
#: The broker is up but cannot do its job for this path (the lock file
#: cannot be opened, or the platform has no ``fcntl``). Deliberately
#: distinct from :data:`TIMED_OUT`: "nobody is competing for this file,
#: the mechanism is broken" and "someone else holds it" demand opposite
#: responses, and folding them together would block edits for a reason
#: the sub-agent could never resolve by waiting.
UNAVAILABLE = "unavailable"


class LockBrokerUnavailable(RuntimeError):
    """The broker could not be reached, or answered something unusable.

    Callers must treat this as *"the lock layer is down"*, not as
    *"the file is busy"* — the two demand opposite responses (fail open
    with a warning vs. refuse the edit). Conflating them would either
    brick every sub-agent when the executor dies, or silently drop the
    lock guarantee.
    """


def normalize_target(project_dir: PathLike, target: PathLike) -> str:
    """Return the canonical, project-relative lock key for ``target``.

    Both spellings of the same file — ``backend/agent.py`` and
    ``/abs/path/to/project/backend/agent.py`` — must produce one key, or
    two holders appear for one file (see the module docstring).

    ``realpath`` is applied to both sides so a symlinked checkout (on
    macOS ``/tmp`` and ``/var`` are themselves symlinks) does not split a
    single file into two keys. It resolves as far as it can for a path
    that does not exist yet, which is the common case for a new file.

    A target outside ``project_dir`` is keyed by its absolute path. It is
    not rejected here: containment is the Edit/Write guard's job, and
    conflating the two would make this function's contract depend on a
    policy that belongs elsewhere.
    """
    root = Path(os.path.realpath(str(project_dir)))
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = Path(os.path.realpath(str(candidate)))
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(resolved)


def _workspace_digest(project_dir: PathLike) -> str:
    """Short, stable digest identifying one workspace.

    Shared by the socket and the locks directory so both are derived the
    same way from the same single input (``project_dir``), which is all
    the executor and a sub-agent are guaranteed to agree on.
    """
    root = os.path.realpath(str(project_dir))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]


def locks_dir_for_plan(plan_dir: PathLike) -> Path:
    """Return the lock directory belonging to one plan.

    ``<plan_dir>/locks/`` — beside the plan's own artifacts (``prd.json``,
    ``tasks.json``, ``execution.log``), for three reasons:

    * **A plan's locks are per-plan runtime state.** A fresh run acquires
      its own set, and the set is meaningless once the plan is done, so
      it belongs with the plan's other runtime state rather than in a
      shared or hidden location.
    * **It is inside PDT's own tree, not the user's workspace.** The plan
      directory comes from the server's plans root, never from
      ``project_dir``, so nothing is written into the repository being
      worked on. That is the property that matters: the workspace is
      someone else's git tree, and a tool must not need it to add an
      ignore rule to stay clean.
    * **PDT already ignores it.** ``plans/`` is gitignored in the server's
      own repo, so the lock files can never be committed by anything.

    What this placement does *not* give you is arbitration between two
    **different** plans pointed at one workspace: each plan has its own
    directory, so their lock files never meet. Two executors on the same
    plan still contend correctly (same plan dir), and the scheduler
    starts one plan at a time per workspace; a manual start of a second
    plan against a busy workspace is therefore outside the lock's scope.
    """
    return Path(plan_dir) / LOCKS_SUBDIR


def fallback_locks_dir(project_dir: PathLike) -> Path:
    """Lock directory for callers that have no plan directory.

    The legacy executor layout pointed ``--tasks-file`` at
    ``<project_dir>/tasks.json``, so there is no plan directory to sit
    beside — and putting ``locks/`` into the workspace is the one thing
    this must never do. Derived from the workspace instead, so every
    process on the machine agrees without being told.

    Under the system temp root unless :data:`LOCK_ROOT_ENV_VAR` names
    another one. The directory is created by the writer
    (``file_lock_manager``) with ``exist_ok=True``, so an empty one that
    the boot-time sweep removes is recreated on next use rather than
    becoming a missing-path failure.
    """
    return (
        _derived_root()
        / f"pdt-ws-locks-{_workspace_digest(project_dir)}"
    )


def lock_file_path(locks_dir: PathLike, project_dir: PathLike, target: PathLike) -> Path:
    """Return the lock file guarding ``target`` in ``project_dir``.

    ``target`` is canonicalised against ``project_dir`` first, so a
    relative and an absolute spelling of one file land on one lock file
    (see :func:`normalize_target`). Every caller — the broker and
    ``FileLockManager`` alike — goes through here, because two
    implementations that disagree by a byte produce two lock files for
    one file, i.e. exclusion that looks like it works and is not.
    """
    key = normalize_target(project_dir, target)
    digest = hashlib.sha256(os.path.normpath(key).encode("utf-8")).hexdigest()
    return Path(locks_dir) / f"{digest}.lock"


def socket_path(project_dir: PathLike) -> Path:
    """Return the broker socket path for ``project_dir``.

    Deliberately **not** ``<project_dir>/.pdt/lock-broker.sock``, which is
    where it obviously belongs. ``AF_UNIX`` paths are capped (104 bytes on
    macOS, 108 on Linux) and the cap applies to the whole path, so a
    checkout nested a few directories deep would push the socket past it
    and ``bind`` would fail with ``OSError: AF_UNIX path too long`` — the
    lock layer silently gone on exactly the machines least likely to be
    inspected. Measured: a temp-dir checkout with a descriptive name was
    already over the limit.

    The path is therefore derived into the per-user temp dir as
    ``pdt-lock-<sha256(realpath)[:16]>.sock`` — short, and deterministic
    from the project dir, so the executor and every sub-agent compute the
    same value without a discovery protocol.

    ``PDT_LOCK_BROKER`` overrides the derivation outright. That is also
    how a caller opts **out**: leaving the variable empty while setting it
    is not a thing, but a caller that wants no broker simply never starts
    one, and the hook finds no socket to talk to.
    """
    override = os.environ.get(SOCKET_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return (
        _derived_root()
        / f"{_SOCKET_PREFIX}{_workspace_digest(project_dir)}.sock"
    )


def request(
    sock_path: PathLike,
    payload: Dict[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """Send one newline-delimited JSON request and read one reply.

    One connection per request, closed immediately: the broker serves
    each connection on its own thread, and an ``acquire`` can block for
    minutes waiting on a queue. Reusing a connection would tie up a
    client-side fd for that whole window for no benefit — the callers
    here issue one request per process invocation anyway.

    Raises:
        LockBrokerUnavailable: on any connection, timeout, or framing
            failure. Never leaks a partial reply as if it were valid.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (OSError, socket.timeout) as exc:
        raise LockBrokerUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass

    line = buf.split(b"\n", 1)[0]
    if not line:
        raise LockBrokerUnavailable("broker closed the connection without replying")
    try:
        reply = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LockBrokerUnavailable(f"malformed reply: {exc}") from exc
    if not isinstance(reply, dict):
        raise LockBrokerUnavailable(f"reply is not an object: {type(reply).__name__}")
    return reply


def acquire(
    sock_path: PathLike,
    task_id: str,
    target: PathLike,
    timeout: float,
) -> str:
    """Ask the broker for ``target`` on behalf of ``task_id``.

    Returns one of :data:`ACQUIRED`, :data:`ALREADY_HELD`,
    :data:`TIMED_OUT`.

    The socket timeout is ``timeout`` plus a margin: the broker holds the
    connection open for the whole queue wait, so a client that gave up
    exactly when the broker was about to grant would report a timeout
    while the lock was in fact taken — and then never release it.
    """
    reply = request(
        sock_path,
        {"op": "acquire", "task_id": task_id, "path": str(target),
         "timeout": float(timeout)},
        timeout=float(timeout) + 30.0,
    )
    if not reply.get("ok"):
        reason = reply.get("reason") or "unknown"
        if reason == TIMED_OUT:
            return TIMED_OUT
        if reason == UNAVAILABLE:
            detail = reply.get("detail") or "broker cannot lock this path"
            raise LockBrokerUnavailable(str(detail))
        raise LockBrokerUnavailable(f"acquire rejected: {reason}")
    return ACQUIRED if reply.get("state") == ACQUIRED else ALREADY_HELD


def release(
    sock_path: PathLike,
    task_id: str,
    targets: Optional[Iterable[PathLike]] = None,
) -> int:
    """Release ``targets`` for ``task_id``; ``None`` releases everything.

    Returns the number of locks actually released, so callers can tell
    "released nothing because the task held nothing" apart from "the
    broker never heard about this task" (a silent leak otherwise).
    """
    reply = request(
        sock_path,
        {
            "op": "release",
            "task_id": task_id,
            "paths": None if targets is None else [str(t) for t in targets],
        },
        timeout=30.0,
    )
    if not reply.get("ok"):
        raise LockBrokerUnavailable(f"release rejected: {reply.get('reason')}")
    return int(reply.get("released", 0))
