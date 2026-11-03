"""Taking back the reaper daemons a test run started.

The problem this module exists for
----------------------------------
A reaper daemon is written to outlive the process that started it — that
is what a daemon *is*.  The test suite drives real servers, and a real
server starts a reaper daemon on the plan root it was given.  When the
test terminates that server, the daemon is still there: it was asked to
survive, and it did.

So every test that starts a server leaves a daemon behind, and the
daemons accumulate for as long as the machine lives.  They hold memory,
they hold the plan root's lock, and they are indistinguishable from the
daemons of the developer's own server — which is the part that makes
the obvious fix wrong.  A cleanup that reclaims *every* reaper daemon
on the machine would take the developer's own services down with it.

The rule this module applies
-----------------------------
A daemon belongs to this run when two things are both true:

1. it was not running when the run took its **snapshot** — the census
   before the first server was started is the record of what the machine
   already had; and
2. it serves a root **this run made** — either a root pytest made, or a
   root inside this session's own temporary tree.

Both halves are necessary.  The first keeps the developer's daemon; the
second keeps the daemon of another test run that happens to have left
one behind on a real plan root.  What is left over is exactly the
daemons this run started, and only those are stopped.

The two kinds of "this run made"
--------------------------------
The rule used to be "a root pytest made", and it missed the daemons the
**backend** starts: they serve the session's own plan root — a
temporary tree, but not one pytest made — and they are started with
``--survive-owner``, so they outlive the server that started them.
Twenty-four of them were walking one machine on 2026-11-02.

So the session's own tree counts as well, and the rule is still narrow:
the tree is named by an environment variable the run itself minted, and
under xdist every worker has one of its own.

The narrowness has to survive xdist.  The workers of one run share a
machine, and a worker that stopped another worker's daemon would be
stopping a service it never asked for — so a pytest tree that belongs
to another worker is left alone, whichever way it was made.

Nothing here raises
-------------------
Every function in this module is called from a fixture's teardown.  A
raise there would take the whole test run down over a cleanup that
failed, so a census that cannot be read and a stopper that cannot be
reached are both logged and stepped over.  The run's own assertions are
what the run is for; the litter is not worth a red build.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

#: The backend package root — this file lives at ``backend/tests/``.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "is_pytest_plans_root",
    "is_session_plans_root",
    "is_another_worker_root",
    "reclaim_new_daemons",
    "snapshot_daemons",
]


#: The marker in the name of the temporary directory pytest creates.
#:
#: pytest builds ``<tmp>/pytest-of-<user>/pytest-<n>/<test>/`` — the
#: ``pytest-of-`` component is the one that says "a test run made this
#: tree", and it is the one component whose name pytest fixes.
PYTEST_ROOT_MARKER = "pytest-of-"

#: The directory pytest gives one run: ``pytest-<n>``.
_PYTEST_RUN_DIR = re.compile(r"^pytest-\d+$")

#: The name xdist gives one worker: ``gw0``, ``gw1``, ...
#:
#: Under xdist every worker gets its own directory under the run's
#: directory, named after the worker.  That name is what says whose
#: tree a daemon is serving.
_WORKER_DIR = re.compile(r"^gw\d+$")

#: The environment variable that names this session's own temporary tree.
#:
#: The suite mints it (see ``backend/_pytest_hermetic_env.py``), so it
#: is the run's own name for the tree it is working in.  Under xdist
#: it is scoped to the worker, so it says *this worker's* tree.
SESSION_ROOT_ENV = "PDT_TEST_SESSION_ROOT"

#: Path separators, both spellings: a root reaches this module as a
#: string the machine wrote, and the machine's spelling is not ours to
#: assume.
_SEPARATORS = re.compile(r"[\\/]+")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _root_key(root: object) -> Optional[str]:
    """Return *root* as one absolute path, or ``None``.

    The key is what two spellings of one directory are compared by: a
    root can be written down through a symlink, relatively, with a
    trailing separator, and all of them name one directory.  A root that
    cannot be resolved names nothing, and names nothing is never the
    root this run is working in.
    """
    if not root:
        return None
    try:
        return str(Path(str(root)).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _daemon_pid(daemon: object) -> Optional[int]:
    """Return the pid of *daemon*, or ``None`` when it has none.

    A pid of one or less is not a process a daemon can be running as —
    that range is the machine's own init — so an entry carrying one is
    not a daemon this module has to reason about.
    """
    pid = getattr(daemon, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if pid <= 1:
        return None
    return pid


def _known_pids(before: object) -> set:
    """Return the pids the snapshot already knew about."""
    known = set()
    if not before:
        return known
    try:
        pids = list(before)  # type: ignore[call-overload]
    except TypeError:
        return known
    for pid in pids:
        try:
            known.add(int(pid))
        except (TypeError, ValueError):
            continue
    return known


def _default_stopper(plans_root: Path) -> List[int]:
    """Stop the daemons of *plans_root* and report the pids stopped.

    This is the module's own helper for stopping a root's daemons; the
    seam exists so a test can replace it without a signal leaving the
    test process.
    """
    return reaper.stop_daemons_for_plans_root(plans_root)


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def snapshot_daemons(
    *,
    lister: Optional[Callable[[], Sequence[object]]] = None,
) -> Dict[int, str]:
    """Return the reaper daemons running right now, as pid to start token.

    The snapshot is the record of what the machine already had.  A run
    takes it before it starts anything, and compares every later census
    against it: a pid that is in the snapshot is a daemon this run did
    not start, whatever root it serves.

    The start token travels with the pid because a pid alone is not an
    identity — the kernel recycles them — so a caller that wants to know
    whether it is looking at the same process it saw before has the
    information to answer that.

    *lister* returns the daemons to read; the machine's own census when
    it is not given.  A census that raises costs the snapshot and
    nothing else: the result is empty, which reads as "nothing was
    running" and costs the run nothing it cannot live without.
    """
    census = reaper.list_reaper_daemons if lister is None else lister

    try:
        daemons = list(census())
    except Exception:  # noqa: BLE001 - a census that fails is an empty one
        logger.warning(
            "reaper daemon guard: could not read the daemon census",
            exc_info=True,
        )
        return {}

    snapshot: Dict[int, str] = {}
    for daemon in daemons:
        pid = _daemon_pid(daemon)
        if pid is None:
            continue
        started_at = getattr(daemon, "started_at", None)
        snapshot[pid] = "" if started_at is None else str(started_at)
    return snapshot


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def is_pytest_plans_root(root: object) -> bool:
    """Return whether *root* names a path pytest made.

    A root is pytest's when one of its directories carries the
    ``pytest-of-`` marker — the component pytest names after the user
    who ran the suite.  That marker is what tells this run's temporary
    tree from every other directory on the machine, and it is the reason
    a root this run started is recognisable as such after the fact.

    A root that is missing, empty, or names no pytest directory is not
    pytest's, and a daemon on such a root is not this run's to stop.
    """
    if not root:
        return False
    try:
        text = str(root)
    except Exception:  # noqa: BLE001 - an unreadable root is not pytest's
        return False
    if not text:
        return False
    return any(
        PYTEST_ROOT_MARKER in part for part in _SEPARATORS.split(text) if part
    )


def is_session_plans_root(root: object) -> bool:
    """Return whether *root* is inside this session's own temporary tree.

    The session root is named by :data:`SESSION_ROOT_ENV`, which the
    suite mints for itself — so a root inside it is a root this run
    made.  Under xdist the variable is scoped to the worker, so the
    answer is "this worker's tree", not "this run's tree".

    This is the rule that catches the daemons the **backend** starts:
    they serve the session's plan root, which is a temporary tree but
    not one pytest made, and they are started with ``--survive-owner``
    so they outlive the server that started them.
    """
    session = os.environ.get(SESSION_ROOT_ENV)
    if not session:
        return False
    root_key = _root_key(root)
    session_key = _root_key(session)
    if root_key is None or session_key is None:
        return False
    if root_key == session_key:
        return True
    try:
        return Path(root_key).is_relative_to(session_key)
    except (AttributeError, ValueError):  # pragma: no cover - an old Python
        return root_key.startswith(session_key + os.sep)


def _worker_of(root: object) -> Optional[str]:
    """Return the xdist worker whose tree *root* is in, if any.

    The worker directory sits directly under the run's directory, so
    the answer is the component that follows ``pytest-<n>``.  A tree
    that has no worker directory belongs to no worker: it is pytest's
    own tree, made by a serial run.
    """
    try:
        parts = [part for part in _SEPARATORS.split(str(root)) if part]
    except Exception:  # noqa: BLE001 - an unreadable root is nobody's
        return None
    for index, part in enumerate(parts):
        if not _PYTEST_RUN_DIR.match(part):
            continue
        if index + 1 < len(parts):
            candidate = parts[index + 1]
            if _WORKER_DIR.match(candidate):
                return candidate
        return None
    return None


def is_another_worker_root(root: object) -> bool:
    """Return whether *root* belongs to another xdist worker.

    The workers of one run share a machine.  A worker that stopped
    another worker's daemon would be stopping a service it never asked
    for — the very mistake the narrow rule exists to prevent.

    A tree that belongs to no worker — pytest's own, made by a serial
    run — belongs to nobody else, and is left alone.
    """
    worker = _worker_of(root)
    if worker is None:
        return False
    mine = os.environ.get("PYTEST_XDIST_WORKER")
    if not mine:
        # A serial run has no workers, so no tree can be another
        # worker's.
        return False
    return worker != mine


def _is_this_runs_root(root: object) -> bool:
    """Return whether *root* is a tree this run made.

    Two ways to qualify: pytest made it, or this session made it.  The
    second is what catches the backend's own daemons, and the first is
    what catches the ones a test started by hand.
    """
    if is_session_plans_root(root):
        return True
    return is_pytest_plans_root(root) and not is_another_worker_root(root)


# ---------------------------------------------------------------------------
# The reclaim
# ---------------------------------------------------------------------------


def reclaim_new_daemons(
    before: Optional[Dict[int, str]],
    *,
    lister: Optional[Callable[[], Sequence[object]]] = None,
    stopper: Optional[Callable[[Path], Iterable[int]]] = None,
) -> List[int]:
    """Stop the daemons this run started, and report the pids stopped.

    *before* is the snapshot taken before the run started anything: the
    pids that were already running are the ones this run did not start,
    and they are never stopped.

    A daemon is reclaimed when it is new — its pid is not in the
    snapshot — and it serves a root this run made.  The two rules are
    applied together, and each one on its own would be wrong: the
    snapshot alone would claim the developer's own daemon on the next
    run, and the root alone would claim the daemon of an earlier run
    that left one behind on the same temporary tree.

    A root that belongs to another xdist worker is not this run's, and
    is left alone whatever else it looks like.

    A root that already served a daemon before the snapshot is left
    entirely alone, even when a new daemon has joined it.  Stopping is
    done per root, so reclaiming that root would take the older daemon
    with it — and the older daemon is somebody else's.

    *lister* returns the daemons to consider; the machine's own census
    when it is not given.  *stopper* is called with the root of the
    daemons to stop and answers with the pids it stopped; the module's
    own root-scoped helper when it is not given.

    The returned list holds the pids that were actually reclaimed.  A
    census that cannot be read and a stopper that raises are both
    logged and stepped over — the run's own assertions matter more than
    its litter.
    """
    census = reaper.list_reaper_daemons if lister is None else lister
    stop = _default_stopper if stopper is None else stopper

    try:
        daemons = list(census())
    except Exception:  # noqa: BLE001 - a census that fails is an empty one
        logger.warning(
            "reaper daemon guard: could not read the daemon census",
            exc_info=True,
        )
        return []

    known = _known_pids(before)
    protected_roots = set()
    candidates: Dict[str, List[int]] = {}
    order: List[str] = []

    for daemon in daemons:
        pid = _daemon_pid(daemon)
        if pid is None:
            continue
        root = getattr(daemon, "plans_root", None)
        key = _root_key(root)
        if pid in known:
            # A daemon of somebody else's: remember the root it serves,
            # because stopping that root would stop it too.
            if key is not None:
                protected_roots.add(key)
            continue
        if key is None or not _is_this_runs_root(root):
            continue
        if pid == os.getpid():
            # The caller is never one of the daemons it stops.
            continue
        if key not in candidates:
            candidates[key] = []
            order.append(key)
        candidates[key].append(pid)
        if getattr(daemon, "survive_owner", False):
            # The batch that outlives its starter: the daemons the
            # backend asks for.  They are named here so that a run that
            # reclaims one can say it reclaimed *that* kind.
            logger.info(
                "reaper daemon guard: reclaiming the detached daemon %s "
                "on %s",
                pid,
                key,
            )

    reclaimed: List[int] = []
    for key in order:
        pids = candidates[key]
        if key in protected_roots:
            logger.warning(
                "reaper daemon guard: root %s already served a daemon before "
                "the snapshot; leaving it alone",
                key,
            )
            continue
        try:
            stopped = stop(Path(key))
        except Exception:  # noqa: BLE001 - one root must not stop the rest
            logger.warning(
                "reaper daemon guard: stopping the daemons of %s failed",
                key,
                exc_info=True,
            )
            continue
        if not stopped:
            continue
        try:
            reported = list(stopped)
        except TypeError:
            reported = []
        for pid in reported:
            if pid in pids and pid not in reclaimed:
                reclaimed.append(pid)
    return reclaimed
