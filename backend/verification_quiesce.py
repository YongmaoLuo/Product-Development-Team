"""Which of a plan's verification processes are still alive.

2026-11-20 — the ability to answer "does this plan still have live
work?" was scattered across two call sites: ``_cleanup_dead_verification_processes``
(kills what it can enumerate) and ``_surviving_vp_ids`` (reports what
survived a kill).  Both read one source — the registry of in-flight
verification sub-agents — and both therefore share one blind spot: the
**repair executor**.  That process is spawned by the verification
auto-loop's repair path, not by a VP dispatch, so it never appears in
the registry.  A stop that could not see it could not kill it, and the
plan was reported stopped while the repair kept spending.

So the answer lives here, in one place, over two sources:

* the **registry** — ``sub_agent_registry.all_handles(plan_id)``, one
  handle per in-flight verification sub-agent.  The handle's ``pid``
  is the process it owns and the VP id is what makes the report
  actionable ("*which* verification point is still burning tokens").
* the **repair executor** — the plan's ``_execution_state`` record
  whose ``_source`` is ``repair_execution``.  The record is read
  lazily out of the server module, because this module is imported by
  the code that decides whether the server is busy: a module-level
  ``import server`` would be a cycle, and reading the state only when
  a scan actually happens keeps the answer honest — a scan that is
  never asked for never imports the application.

Liveness is decided by ``process_reaper._alive`` — the same predicate
the reaper and the stop path use — and **a pid that cannot be read is
not a survivor**: "I could not look" is not evidence that the plan is
still busy, and refusing to report a plan as busy on a reading failure
would strand plans that did stop.

Nothing here raises.  A source that cannot be read is logged and
skipped: the scan is a report, and the decision (and the timeout) that
follows it belongs to the wait below — or, for a caller that only
wants the picture, to the caller.

2026-11-20 — the scan answers "what is running *now*", and "now" is
not silence: the round can dispatch another verification point a
millisecond after the scan looked empty, which is the race a stop has
to lose.  So the same module also owns the *wait*
(:func:`wait_for_quiescence`) that turns a snapshot into a fact: the
tree has to be empty, stay empty for a window, be seen empty more than
once, and see no new dispatch.  The scan stays a pure report; the wait
is where the verdict lives.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import verification_cancel

logger = logging.getLogger(__name__)


#: A verification sub-agent registered in ``sub_agent_registry``.
KIND_REGISTRY = "registry"

#: The repair executor subprocess recorded in ``server._execution_state``.
KIND_REPAIR_EXECUTOR = "repair_executor"

#: The ``_source`` marker the repair path writes into the execution
#: record.  The main execution path shares the same record and is not
#: a repair executor.
REPAIR_EXECUTION_SOURCE = "repair_execution"


@dataclass(frozen=True)
class Survivor:
    """One live process that belongs to a plan's verification work.

    ``pid`` is the process.  ``kind`` says which source named it
    (``registry`` / ``repair_executor``).  ``detail`` carries that
    source's own label — the VP id for a registry handle, empty for the
    repair executor, which has no verification point of its own.
    """

    pid: int
    kind: str
    detail: str = ""


#: Test seam — a stand-in for the ``server`` module.  ``None`` means
#: "import the real one", which is what production does.
_server_module: Optional[Any] = None


# ---------------------------------------------------------------------------
# The primitives
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    """True when ``pid`` still names a running process.

    The predicate is ``process_reaper._alive`` — the same one the
    reaper and the stop path use, so "alive" means one thing in the
    process.  A probe that cannot be answered is answered "no": an
    unreadable pid is not evidence of a busy plan.
    """
    try:
        import process_reaper

        return bool(process_reaper._alive(pid))
    except Exception:  # noqa: BLE001 — an unreadable pid is not a live one
        logger.warning(
            "[verification_quiesce] liveness probe failed pid=%s",
            pid, exc_info=True,
        )
        return False


def _handle_pid(handle: Any) -> int:
    """The pid a registry handle owns, or ``0`` when it owns none."""
    try:
        return int(getattr(handle, "pid", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _handle_detail(handle: Any) -> str:
    """The label a registry handle carries — its verification point id."""
    try:
        return str(getattr(handle, "vp_id", "") or "")
    except Exception:  # noqa: BLE001 — a label is never worth a failure
        return ""


def _execution_state_map() -> Optional[Dict[str, Any]]:
    """The server's live ``_execution_state`` map, read lazily.

    ``None`` when the server cannot be imported or carries no such
    map — the caller then simply has no repair-executor source, which
    is a smaller report, not a broken scan.
    """
    module = _server_module
    if module is None:
        try:
            import server
        except Exception:  # noqa: BLE001 — the source is skipped, not fatal
            logger.warning(
                "[verification_quiesce] server import failed — the repair "
                "executor source is skipped",
                exc_info=True,
            )
            return None
        module = server
    state = getattr(module, "_execution_state", None)
    if not isinstance(state, dict):
        return None
    return state


def _plan_record(plan_id: str, execution_state: Any) -> Optional[Dict[str, Any]]:
    """The plan's own execution record inside ``execution_state``.

    ``execution_state`` is the whole ``{plan_id: record}`` map.  A
    caller that hands the plan's record directly is accepted too — the
    repair path's record is recognisable by its ``_source`` marker, and
    refusing it would silently hide the very process this scanner
    exists to find.
    """
    if not isinstance(execution_state, dict):
        return None
    record = execution_state.get(plan_id)
    if isinstance(record, dict):
        return record
    if plan_id not in execution_state and "_source" in execution_state:
        return execution_state
    return None


# ---------------------------------------------------------------------------
# The sources
# ---------------------------------------------------------------------------


def _registry_survivors(plan_id: str) -> List[Survivor]:
    """The registry handles of ``plan_id`` whose process is still alive.

    A handle with no process on it is not a survivor: there is nothing
    running that the handle names.
    """
    try:
        from sub_agent_registry import sub_agent_registry as registry

        handles = list(registry.all_handles(plan_id))
    except Exception:  # noqa: BLE001 — the source is skipped, not fatal
        logger.warning(
            "[verification_quiesce] registry read failed plan=%s",
            plan_id, exc_info=True,
        )
        return []

    survivors: List[Survivor] = []
    for handle in handles:
        pid = _handle_pid(handle)
        if pid <= 0:
            continue
        if not _alive(pid):
            continue
        survivors.append(Survivor(
            pid=pid, kind=KIND_REGISTRY, detail=_handle_detail(handle),
        ))
    return survivors


def _repair_executor_survivors(
    plan_id: str, execution_state: Any,
) -> List[Survivor]:
    """The plan's repair executor, if its process is still alive.

    Only the repair path's own marker names a repair executor, and
    only a live process counts: a record that still says ``running``
    after the process died describes nothing that is running.
    """
    record = _plan_record(plan_id, execution_state)
    if not isinstance(record, dict):
        return []
    if record.get("_source") != REPAIR_EXECUTION_SOURCE:
        return []
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return []
    if pid <= 0:
        return []
    if not _alive(pid):
        return []
    return [Survivor(pid=pid, kind=KIND_REPAIR_EXECUTOR, detail="")]


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan_plan_processes(
    plan_id: str, *, execution_state: Optional[Dict[str, Any]] = None,
) -> List[Survivor]:
    """Every live process that belongs to ``plan_id``'s verification work.

    ``execution_state`` is the server's ``{plan_id: record}`` map.  It
    is injectable so a caller (and a test) can read a specific state
    instead of the live one; when it is omitted the map is read
    lazily out of the server module, because the repair executor is
    invisible to the registry and a scanner that could not see it would
    be blind to exactly the process this module exists to find.

    One process is one survivor: the same pid named by both sources is
    reported once, and the registry wins — it knows which verification
    point the pid belongs to, which is the useful half of the answer.
    """
    if not plan_id:
        return []

    survivors: List[Survivor] = []
    seen: Dict[int, Survivor] = {}

    for survivor in _registry_survivors(plan_id):
        if survivor.pid in seen:
            continue
        seen[survivor.pid] = survivor
        survivors.append(survivor)

    state_map = execution_state
    if state_map is None:
        state_map = _execution_state_map()
    for survivor in _repair_executor_survivors(plan_id, state_map):
        if survivor.pid in seen:
            continue
        seen[survivor.pid] = survivor
        survivors.append(survivor)

    return survivors


def survivors_snapshot(plan_id: str) -> List[Dict[str, Any]]:
    """``scan_plan_processes`` as JSON-friendly dicts.

    The shape a card, a log line or an API response wants: plain
    values, one key per field, nothing that needs an encoder.
    """
    return [
        {"pid": s.pid, "kind": s.kind, "detail": s.detail}
        for s in scan_plan_processes(plan_id)
    ]


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuiesceResult:
    """The verdict of a wait for quiescence.

    ``quiet`` — the plan went quiet: the process tree was empty, it
    stayed empty for the whole window, it was observed empty at least
    ``min_scans`` times, and nothing new was dispatched while the
    window was open.

    ``scans`` — how many scans the wait made.  It says how much
    evidence the verdict rests on: a quiet verdict always rests on at
    least ``min_scans`` observations of an empty tree.

    ``survivors`` — the live processes the last scan found.  A quiet
    verdict carries none (quietness *is* an empty tree); a verdict that
    is not quiet carries the report the caller needs to act on.

    ``dispatch_advanced`` — the dispatch ledger moved inside the
    window, so the plan was not quiet even though its tree looked
    empty.
    """

    quiet: bool
    scans: int
    survivors: List[Survivor] = field(default_factory=list)
    dispatch_advanced: bool = False


def wait_for_quiescence(
    plan_id: str,
    *,
    quiet_seconds: float = 5.0,
    min_scans: int = 2,
    max_seconds: float = 60.0,
    poll_interval: float = 0.5,
    dispatch_baseline: Optional[int] = None,
    scan: Optional[Callable[[str], List[Survivor]]] = None,
    execution_state: Optional[Dict[str, Any]] = None,
) -> QuiesceResult:
    """Wait until ``plan_id``'s verification work is really quiet.

    The judgement is the one the architecture decided on: a plan may
    only be treated as silent when **all four** of these hold —

    1. the process tree is empty,
    2. it has stayed empty for at least ``quiet_seconds``,
    3. it was observed empty at least ``min_scans`` times,
    4. nothing was dispatched while the window was open.

    A tree that is empty *at one instant* is not silence: the round
    can dispatch another verification point a millisecond later, and
    that is exactly the race a stop has to lose.  So the wait keeps
    scanning until the emptiness has lasted long enough to be a fact
    rather than a coincidence, and a scan that finds a survivor starts
    the window over.

    Condition 4 is read from the dispatch ledger
    (``verification_cancel.dispatch_total``) — a monotonic per-plan
    counter of dispatched verification points.  The baseline is the
    ledger as it stands when the wait starts, unless the caller
    passes one.  A total that differs from the baseline means new work
    was dispatched inside the window, so the plan is **not** quiet; the
    ledger never goes back, so that verdict stands for the rest of the
    wait.

    ``scan`` replaces the scanner — the seam that lets a caller (and a
    test) decide what "the process tree" means.  It is called with the
    plan id and returns the survivors it finds; ``execution_state`` is
    threaded into the default scanner only.

    The wait does not raise.  When the deadline passes without
    silence it returns ``quiet=False`` together with the survivors the
    last scan found: the caller gets a report, not an exception.
    """
    if scan is None:
        def _default_scan(target_plan_id: str) -> List[Survivor]:
            return scan_plan_processes(
                target_plan_id, execution_state=execution_state,
            )

        scanner = _default_scan
    else:
        scanner = scan

    baseline = (
        verification_cancel.dispatch_total(plan_id)
        if dispatch_baseline is None
        else dispatch_baseline
    )

    started = time.monotonic()
    quiet_since: Optional[float] = None
    empty_scans = 0
    scans = 0
    survivors: List[Survivor] = []
    dispatch_advanced = False

    while True:
        now = time.monotonic()
        found = scanner(plan_id)
        scans += 1
        survivors = list(found or [])

        total = verification_cancel.dispatch_total(plan_id)
        if total != baseline:
            if not dispatch_advanced:
                logger.warning(
                    "[verification_quiesce] dispatch inside the quiet window "
                    "plan=%s total=%s baseline=%s",
                    plan_id, total, baseline,
                )
            dispatch_advanced = True

        if survivors:
            # the tree is not empty — the quiet period starts over
            empty_scans = 0
            quiet_since = None
        else:
            empty_scans += 1
            if quiet_since is None:
                quiet_since = now

        quiet = (
            not dispatch_advanced
            and empty_scans >= min_scans
            and quiet_since is not None
            and (now - quiet_since) >= quiet_seconds
        )
        if quiet:
            logger.info(
                "[verification_quiesce] plan is quiet plan=%s scans=%s",
                plan_id, scans,
            )
            return QuiesceResult(
                quiet=True, scans=scans, survivors=survivors,
                dispatch_advanced=False,
            )

        if (time.monotonic() - started) >= max_seconds:
            logger.warning(
                "[verification_quiesce] quiet window expired plan=%s "
                "scans=%s survivors=%s dispatch_advanced=%s",
                plan_id, scans, len(survivors), dispatch_advanced,
            )
            return QuiesceResult(
                quiet=False, scans=scans, survivors=survivors,
                dispatch_advanced=dispatch_advanced,
            )

        if poll_interval > 0:
            time.sleep(poll_interval)


def _reset_for_tests() -> None:
    """Test hook — drops the stand-in server module.

    The scanner keeps no state of its own; the hook exists so a test
    that swapped the server stand-in gets a clean module back, and so
    a test suite has one obvious place to call when it wants the next
    scan to read the world from scratch.
    """
    global _server_module
    _server_module = None
