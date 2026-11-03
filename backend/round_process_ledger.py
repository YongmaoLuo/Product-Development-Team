"""Round-scoped ledger of the coding-tool processes a verification round owns.

Why this file exists
--------------------
When a verification round is judged dead, the cleanup has to kill
everything that round left behind.  Two populations are covered today:
the registered sub-agent handles (the LLM tool subprocess) and the
orphaned ``pytest``/``bash`` the agent disowned.  The third population
is not covered at all — the coding tool the **orchestrator** itself
spawned.  Its ``Popen`` lives in ``tool._current_process``, and nothing
outside the tool knows about it: the tool is not a registry handle, so
the cleanup never sees it, and the process keeps running long after the
round that started it was declared dead.

The plan directory is not enough to scope the kill.  The rounds of one
plan share one plan directory, so a kill aimed at "the plan" would take
the next round's processes with it.  The scope has to be the *round*,
and a round already has a name: the ``round_token`` the verification
loop takes as its service-owner token.

So this module is the ledger of "which processes belong to which
round": :func:`begin_round` opens a round, :func:`register_tool` puts a
coding tool into the round that is running, :func:`tools_for_round`
reads the round back, :func:`kill_round` kills exactly that round's
processes, and :func:`end_round` forgets the round.

What the ledger is, and what it is not
--------------------------------------
It is **in memory**, and it has to be: what it holds are Python objects
— the coding tools — and a tool's process is reachable only through the
tool.  The durable record of the pids a plan spawned is
:mod:`process_ledger`; this ledger is the *live* view of which of those
processes belong to which round, and it is what a judged-dead round
acts on.

The rules it keeps
------------------
* **A round has to be begun.**  A tool registered outside a round is
  ignored, silently: a registration that cannot be attributed to a
  round is not evidence that anything owns the process.
* **A round owns only its own tools.**  Two rounds of one plan, two
  plans, two tokens — a kill reaches exactly the tools of the round it
  was asked about.
* **An unknown token kills nothing.**  :func:`kill_round` on a token no
  round holds returns an empty list rather than guessing.
* **A dead process records nothing.**  The ledger records the signals it
  *sent*; a process that had already exited was never signalled, so
  there is no event to write down.
* **A tool without a process is skipped.**  A tool that has not started
  a subprocess yet (or has already finished it) has nothing to kill.

The events
----------
:func:`kill_round` returns one dict per **signal it sent**::

    {"pid": 4321, "signal": "SIGTERM", "round_token": "...", "reason": "judged_dead"}

A process that ignores SIGTERM gets two entries — the SIGTERM that was
tried and the SIGKILL that followed — because the escalation is the
part of the story an operator needs.  A process that was already gone
gets none: nothing was sent.

The kill itself is the one the rest of the backend already uses:
``utils.process.kill_process_group``, which signals the process *group*
(the coding tool spawns its child with ``start_new_session=True``, so
the group's own children have to go with it), with the SIGKILL
escalation ``verification_loop._kill_sub_agent_process`` already
performs.
"""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.process import kill_process_group

logger = logging.getLogger(__name__)

__all__ = [
    "begin_round",
    "end_round",
    "register_tool",
    "tools_for_round",
    "kill_round",
    "reset_for_tests",
]


#: How long a SIGTERM is given before the SIGKILL escalation.  The
#: same grace ``verification_loop._kill_sub_agent_process`` allows: the
#: coding tool's child is a wrapper that flushes JSONL on the way out,
#: and a kill that cuts it off mid-flush leaves a torn stream behind.
SIGTERM_GRACE_SECONDS = 5.0


class _Round:
    """One verification round's tools.

    The tools are held strongly, on purpose: a tool's process is
    reachable only through the tool, so dropping the reference would
    drop the process with it.  The lifetime is bounded by the round —
    :func:`end_round` releases the round, and the round is released
    when the verification loop ends.
    """

    __slots__ = ("token", "tools")

    def __init__(self, token: str) -> None:
        self.token = token
        self.tools: List[Any] = []


class _PlanLedger:
    """The rounds of one plan directory.

    ``rounds`` is insertion-ordered, so the last key is the most
    recently begun round — which is where a registration goes.
    """

    __slots__ = ("rounds", "current_token")

    def __init__(self) -> None:
        self.rounds: Dict[str, _Round] = {}
        self.current_token: Optional[str] = None


#: Serialises the ledger's state.  The backend registers tools from
#: several threads (the verification loop, the watchdog, the service
#: managers), and a registration that landed in the wrong round would
#: put a live process out of reach of the kill that has to reach it.
_LOCK = threading.RLock()

#: Ledger state, keyed by the plan directory's resolved path.
_LEDGERS: Dict[str, _PlanLedger] = {}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _plan_key(plan_dir: Any) -> Optional[str]:
    """The ledger key of a plan directory — ``None`` when there is none.

    The path is resolved so that the same directory reached by two
    spellings (``/tmp/x`` and ``/private/tmp/x`` on macOS) is one
    ledger, not two.
    """
    if plan_dir is None:
        return None
    try:
        return str(Path(plan_dir).resolve())
    except (OSError, TypeError, ValueError):
        return None


def _token_of(round_token: Any) -> str:
    """The token as a non-empty string, or ``""`` when there is none."""
    try:
        return str(round_token or "").strip()
    except Exception:  # pragma: no cover - a token is a string in practice
        return ""


# ---------------------------------------------------------------------------
# The round lifecycle
# ---------------------------------------------------------------------------


def begin_round(plan_dir: Path, round_token: str) -> None:
    """Open a round, and make it the one registrations belong to.

    Beginning a round that is already open does not empty it: the round
    keeps the tools it has, because the caller that begins a round is
    the caller that ends it, and a second ``begin`` is a refresh rather
    than a reset.

    An empty token opens nothing — a round with no name cannot be
    addressed by :func:`kill_round`, and a round that cannot be
    addressed is a round nobody can clean up.
    """
    key = _plan_key(plan_dir)
    token = _token_of(round_token)
    if key is None or not token:
        return
    with _LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None:
            ledger = _PlanLedger()
            _LEDGERS[key] = ledger
        round_ = ledger.rounds.get(token)
        if round_ is None:
            round_ = _Round(token)
            ledger.rounds[token] = round_
        ledger.current_token = token


def end_round(plan_dir: Path, round_token: str) -> None:
    """Close a round and forget its tools.

    The tools are dropped, not killed: ending a round is what a round
    that *finished* does, and its processes have already finished with
    it.  A round that has to be cleaned up is cleaned up by
    :func:`kill_round`, which is the only function here that signals
    anything.

    Ending a round leaves the other rounds of the plan alone.
    """
    key = _plan_key(plan_dir)
    token = _token_of(round_token)
    if key is None or not token:
        return
    with _LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None:
            return
        ledger.rounds.pop(token, None)
        if ledger.current_token == token:
            # The round that registrations went to is gone; the next
            # most recently begun round takes over, so a late
            # registration still lands in a round that exists.
            ledger.current_token = next(reversed(ledger.rounds), None)
        if not ledger.rounds:
            _LEDGERS.pop(key, None)


def register_tool(plan_dir: Path, tool: Any) -> None:
    """Put a coding tool into the round that is running in ``plan_dir``.

    Silently ignored when no round has been begun for the directory: a
    tool registered outside a round belongs to no round, and a process
    that belongs to no round is a process nobody may kill — the
    registration is dropped rather than remembered somewhere it cannot
    be reached again.

    Registering the same tool twice keeps one entry, so a kill cannot
    report the same pid twice.
    """
    if tool is None:
        return
    key = _plan_key(plan_dir)
    if key is None:
        return
    with _LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None or not ledger.current_token:
            return
        round_ = ledger.rounds.get(ledger.current_token)
        if round_ is None:
            return
        for registered in round_.tools:
            if registered is tool:
                return
        round_.tools.append(tool)


def tools_for_round(plan_dir: Path, round_token: str) -> List[Any]:
    """The tools the round owns — an empty list for a round nobody began.

    A copy: the caller may not add to the round behind the ledger's
    back, and a kill reads the list while the round may be ending.
    """
    key = _plan_key(plan_dir)
    token = _token_of(round_token)
    if key is None or not token:
        return []
    with _LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None:
            return []
        round_ = ledger.rounds.get(token)
        if round_ is None:
            return []
        return list(round_.tools)


# ---------------------------------------------------------------------------
# The kill
# ---------------------------------------------------------------------------


def kill_round(
    plan_dir: Path,
    round_token: str,
    *,
    reason: str,
    plan_id: str = "",
    logger: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Kill the processes of one round, and report the signals sent.

    The scope is the round: the tools of *that* round are killed, and
    nothing else is touched.  Another round's tools, another plan's
    tools, and the tools of a round that has already ended are all left
    exactly as they were.

    Each tool's process is taken out of the tool under the tool's own
    lock — the same dance ``verification_loop._kill_sub_agent_process``
    performs, so a kill cannot race the tool's own shutdown into
    killing the same process twice.

    The escalation is SIGTERM, then SIGKILL: a process that ignores
    SIGTERM is not left running, and a process that does not is not
    killed twice.

    Returns one dict per signal sent, each carrying ``pid``,
    ``signal``, ``round_token`` and ``reason``.  A tool with no
    process, and a process that had already exited, contribute nothing
    — nothing was signalled, so there is nothing to report.

    Never raises: a kill that fails is a process that survives, and the
    caller has to be able to carry on with the rest of the cleanup.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    key = _plan_key(plan_dir)
    token = _token_of(round_token)
    if key is None or not token:
        return []
    with _LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None:
            return []
        round_ = ledger.rounds.get(token)
        if round_ is None:
            return []
        # Copied out from under the lock: a kill waits for processes to
        # die, and the lock must not be held for that long.
        tools = list(round_.tools)

    events: List[Dict[str, Any]] = []
    for tool in tools:
        proc = _take_process(tool)
        if proc is None:
            continue
        pid = _pid_of(proc)
        if pid is None:
            # Not a process the kernel can be asked about: there is
            # nothing to signal and nothing to name.
            continue
        if not _is_alive(proc):
            # Already gone — no signal was sent, so no event.
            continue
        events.append(_signal(proc, pid, signal.SIGTERM, token, reason))
        if _is_alive(proc):
            events.append(_signal(proc, pid, signal.SIGKILL, token, reason))

    if events:
        try:
            log.warning(
                "[round_process_ledger] killed round processes round=%s "
                "plan=%s reason=%s pids=%s",
                token, plan_id or "-", reason,
                sorted({event["pid"] for event in events}),
            )
        except Exception:  # pragma: no cover - a logger must not break a kill
            pass
    return events


def _signal(
    proc: Any, pid: int, sig: int, round_token: str, reason: str,
) -> Dict[str, Any]:
    """Send one signal and return the event that records it."""
    try:
        kill_process_group(proc, sig=sig, wait_timeout=SIGTERM_GRACE_SECONDS)
    except Exception:  # noqa: BLE001 — a failed signal must not end the kill
        pass
    return {
        "pid": pid,
        "signal": _signal_name(sig),
        "round_token": round_token,
        "reason": reason,
    }


def _signal_name(sig: int) -> str:
    """``SIGTERM`` for 15, ``SIGKILL`` for 9 — and something sane otherwise."""
    try:
        return signal.Signals(int(sig)).name
    except (TypeError, ValueError):
        return f"SIG{sig}"


def _take_process(tool: Any) -> Optional[Any]:
    """The tool's current process, taken out of the tool.

    Taken under the tool's own lock when it has one: the coding tool
    sets and clears ``_current_process`` under that lock, and reading
    it without the lock is a race with the tool's own shutdown.

    The tool is left without a process afterwards.  The process is
    being killed, and a tool that still holds a handle to a process
    somebody else is about to kill would ask its own shutdown path to
    kill it a second time.
    """
    if tool is None:
        return None
    process_lock = getattr(tool, "_process_lock", None)
    if process_lock is not None:
        try:
            with process_lock:
                proc = getattr(tool, "_current_process", None)
                if proc is not None:
                    try:
                        tool._current_process = None
                    except Exception:  # noqa: BLE001 — a read-only tool is fine
                        pass
                return proc
        except Exception:  # noqa: BLE001 — fall back to the unlocked read
            pass
    return getattr(tool, "_current_process", None)


def _pid_of(proc: Any) -> Optional[int]:
    """The process' pid, or ``None`` when it is not one.

    ``bool`` is excluded because ``bool`` is an ``int`` and ``True`` is
    pid 1 — the same guard ``utils.process._child_pid`` applies.
    """
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid if pid > 0 else None


def _is_alive(proc: Any) -> bool:
    """True when the process is still running.

    A process whose ``poll()`` cannot be asked is treated as gone: it
    is not known to be alive, and signalling it would be signalling
    something the ledger cannot name.
    """
    poll = getattr(proc, "poll", None)
    if poll is None:
        return False
    try:
        return poll() is None
    except Exception:  # noqa: BLE001 — an unusable handle is not alive
        return False


def reset_for_tests() -> None:
    """Forget every round.

    For tests only: the ledger is process-wide state, and a test that
    leaves a round behind would lend its tools to the next one.
    """
    with _LOCK:
        _LEDGERS.clear()
