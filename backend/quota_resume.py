"""Is the quota window open again, and what does that revive?
... (module docstring: why the criterion lives in one place, why an empty
reading is not a recovery, why the marker promotes and the status filters,
and why missing fields degrade instead of raising)

The module has three halves, and they are kept apart on purpose.

The **decision** — which rows count as parked — is pure: it takes rows
somebody else hands it and answers.  Every reader shares it (the
heartbeat that sweeps the table, the death latch that asks about one
plan, the resumer that starts the work again), and the only way three
readers can be kept from drifting apart is for them to share one
function.

The **read** — where the rows come from — is the other half, and it is
the one that touches the durable state database.  Its defining property
is that it never raises: a heartbeat thread that dies on one malformed
row stops the resumption loop for every plan, and a death latch that
raises on a row it cannot read declares a plan dead when it is only
unreadable.  So every failure mode answers the empty answer.

The **callers** — how a parked plan is put back to work — are the
third half, and they are deliberately the thinnest part of the module.
A start is a chain of guards (the ledger's single-instance check, the
routing CAS, the project-directory check) and a chain of answers (409
for a plan that is already running, 410 for an archived one, 400 for
one without a usable working directory).  Re-implementing that chain
here would be a second opinion on the same question, and the two
copies would drift — which is how a plan ends up with two executors
writing the same task rows.  So the callers name a plan, call the
start path that already exists, and read the answer.

The **sweep** — the pass that puts the three halves together — is the
fourth.  It reads the parked plans, asks whether the quota window is
open again, and revives what it can.  Its defining property is the
*order* of its two questions: the table is read first and the probe is
sent second, because a sweep that probed on every heartbeat tick would
ask the provider quota endpoint every thirty seconds on a machine with
nothing parked at all.  Nothing parked means no probe is sent at all.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

from fastapi import HTTPException

__all__ = [
    "MIN_RETRY_SEC",
    "ParkedPlan",
    "QUOTA_PARK_REASON",
    "TERMINAL_STATUSES",
    "plan_parked",
    "quota_recovered",
    "read_parked_plans",
    "resume_execution",
    "resume_verification",
    "run_resume_sweep",
    "select_parked_plans",
]

log = logging.getLogger("quota_resume")

QUOTA_PARK_REASON = "waiting_for_quota"
KIND_EXECUTION = "execution"
KIND_VERIFICATION = "verification"
TERMINAL_STATUSES = frozenset({"completed", "failed", "loop_stopped", "passed", "stopped"})

_EXECUTION_STATUS_KEYS = ("exec_status", "status")
_VERIFICATION_STATUS_KEYS = ("verification_status", "status")
_EXECUTION_REASON_KEYS = ("stop_reason", "reason")
_VERIFICATION_REASON_KEYS = ("verification_stop_reason", "stop_reason", "reason")
_VERIFICATION_ROUND_KEYS = ("round",)

#: The shortest interval between two revival attempts for the *same*
#: plan.
#:
#: The heartbeat sweeps every thirty seconds, and R2 asks for a
#: revival within two minutes.  Without a floor, a plan whose revival
#: is refused would be retried on every tick and would write a line on
#: every tick — the log fills with noise and the noise hides the one
#: line that matters.  Two minutes is the number R2 itself names, so
#: the retry window and the requirement are the same window.
MIN_RETRY_SEC = 120

#: When each plan was last *attempted*, by plan id.
#:
#: The ledger is module state on purpose: the sweep runs from a
#: heartbeat thread that has no other place to keep it, and the
#: alternative — a column in the state database — would mean writing
#: to the database from a reader that is documented never to write.
#:
#: An attempt is recorded whether it succeeded or not.  Recording only
#: the successes would let a plan that always fails be retried every
#: thirty seconds, which is the very thing the window exists to stop.
_LAST_ATTEMPT: dict = {}


@dataclass(frozen=True)
class ParkedPlan:
    plan_id: str
    kind: str
    round: int
    reason: str


def quota_recovered(quota_map: Optional[Mapping[str, Any]]) -> bool:
    if not quota_map:
        return False
    for entry in quota_map.values():
        if not getattr(entry, "exhausted", False):
            return True
    return False


def _read(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        pass
    return getattr(row, key, None)


def _text(row: Any, keys: Sequence[str]) -> str:
    for key in keys:
        value = _read(row, key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _number(row: Any, keys: Sequence[str]) -> int:
    for key in keys:
        value = _read(row, key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_parked(row, status_keys, reason_keys) -> bool:
    status = _norm(_text(row, status_keys))
    if status in TERMINAL_STATUSES:
        return False
    reason = _norm(_text(row, reason_keys))
    if reason == QUOTA_PARK_REASON:
        return True
    return status == QUOTA_PARK_REASON


def select_parked_plans(execution_rows=None, verification_rows=None) -> List[ParkedPlan]:
    selected: dict = {}
    for row in execution_rows or ():
        if not _is_parked(row, _EXECUTION_STATUS_KEYS, _EXECUTION_REASON_KEYS):
            continue
        plan_id = _text(row, ("plan_id",))
        if not plan_id or plan_id in selected:
            continue
        selected[plan_id] = ParkedPlan(plan_id, KIND_EXECUTION, 0, QUOTA_PARK_REASON)
    for row in verification_rows or ():
        if not _is_parked(row, _VERIFICATION_STATUS_KEYS, _VERIFICATION_REASON_KEYS):
            continue
        plan_id = _text(row, ("plan_id",))
        if not plan_id or plan_id in selected:
            continue
        selected[plan_id] = ParkedPlan(
            plan_id, KIND_VERIFICATION, _number(row, _VERIFICATION_ROUND_KEYS), QUOTA_PARK_REASON
        )
    return sorted(selected.values(), key=lambda entry: entry.plan_id)


# ---------------------------------------------------------------------------
# The durable read
# ---------------------------------------------------------------------------
#
# Everything above is pure: it decides what a row *means*.  What follows
# is where the rows come from — the state database the whole product
# writes its plan facts into.  The decision is not re-implemented here;
# the rows are handed to it.

#: The columns of ``plan_execution`` the reader reads.  The park lives
#: in two of them: ``exec_status`` says the run is waiting for quota
#: and ``stop_reason`` says why it stopped (``plan_status.py`` reads the
#: same pair, and calls ``stop_reason`` the only place the parked
#: verdict survives a restart).
_EXECUTION_COLUMNS = ("plan_id", "exec_status", "stop_reason")

#: The columns of ``plan_verification`` the reader reads.  The
#: verification table carries its park in its own vocabulary —
#: ``verification_status`` / ``verification_stop_reason`` — and the
#: round number travels with them, because the resumed round *is* the
#: round that stopped.
_VERIFICATION_COLUMNS = (
    "plan_id",
    "verification_status",
    "verification_stop_reason",
    "round",
)

#: The whole-table read of the executions.
_EXECUTION_SQL = "SELECT plan_id, exec_status, stop_reason FROM plan_execution"

#: The whole-table read of the verification rounds.
_VERIFICATION_SQL = (
    "SELECT plan_id, verification_status, verification_stop_reason, round "
    "FROM plan_verification"
)


def _open_state_db():
    """Open a connection to the state database, with the schema applied.

    The connection is the caller's to close: every caller of this
    function opens it inside a ``try`` and closes it in the matching
    ``finally``.  A reader that runs every few seconds and never closes
    its connection leaks a file descriptor per tick, and the symptom of
    that leak — "too many open files" — arrives hours later, in a
    process nobody suspects.

    The path comes from ``server._state_db_path()``: it is the one
    place that knows how the state database's location is resolved (the
    environment override, the live-database guard), so the reader
    follows the same override the rest of the product follows.

    **The sweep never creates the database it sweeps.**  A server whose
    ``state.db`` is absent has nothing parked, so there is nothing to
    look for — and a heartbeat tick that materialises an empty database
    as a side effect of looking is a write nobody asked for.  It also
    hides the state from every reader that asks the filesystem:
    ``/api/plan/{id}/state-from-db`` answers 503 while the file is
    missing and 404 the moment a tick has created it, so "the state
    database is gone" becomes unobservable.  This is the same rule
    ``hitl.service.expire_due`` follows, for the same endpoint.

    So a missing file answers ``None`` — "there is nothing to read" —
    and no connection is opened.  A file that exists but is empty still
    gets the schema applied below, exactly as before.

    The schema is applied on the way in, so the tables the reader reads
    exist even on a database nobody has written to yet.  A migration
    that cannot be applied is not fatal here — the read answers
    "nothing is parked" on its own, which is the same answer a fresh
    database gives.
    """
    import server as _server
    from state_machine.db import schema as _schema
    from state_machine.db.connection import open as _open_connection

    path = _server._state_db_path()
    if not path.exists():
        # There is no database to read.  Opening one here would create
        # it — see the docstring for why the sweep must not do that.
        return None

    conn = _open_connection(path)
    try:
        _schema.migrate(conn)
    except Exception:  # noqa: BLE001
        # The read below answers the empty answer on a database it
        # cannot read; a migration that failed is one of the ways to
        # get there, and it is not a reason to raise on the way in.
        pass
    return conn


def _close_quietly(conn) -> None:
    """Close a connection, whatever the close itself does."""
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _fetch_rows(conn, sql: str, columns: Sequence[str], params: Sequence[Any] = ()) -> List[dict]:
    """Run ``sql`` and turn its rows into plain dicts.

    The columns are named here rather than taken from the row, so the
    result does not depend on the connection's ``row_factory``: the
    production connection hands out plain tuples, a test's connection
    may hand out :class:`sqlite3.Row`, and both are read the same way.
    """
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(zip(columns, tuple(row))) for row in rows]


def read_parked_plans(conn=None) -> List[ParkedPlan]:
    """Read the quota-parked plans out of the state database.

    This is the whole-table read: it answers "who is parked?", which is
    what the heartbeat asks on every tick.

    The rows are handed to :func:`select_parked_plans`, which owns the
    decision — a plan parked in both tables is de-duplicated there, not
    here, and the criterion "parked because of quota" is not re-stated
    in this function.

    **The read never raises.**  A missing table, a column an older
    build did not have, a corrupt file, a locked database: every one of
    them answers the empty answer.  The reason is the callers — a
    heartbeat thread that dies on one bad row stops the resumption loop
    for every plan, and a death latch that raises on a row it cannot
    read declares a plan dead when it is only unreadable.  The cost of
    the empty answer is a park that goes unnoticed until the next tick;
    the cost of raising is a loop that never runs again.

    Args:
        conn: the connection to read on.  ``None`` means "open your own"
            — the reader resolves the state database's path, opens it,
            and closes it again before returning.

    Returns:
        The parked plans, sorted by plan id.  Empty when nothing is
        parked *and* when the read could not be done — the two are the
        same answer, because neither of them licenses a revival.
    """
    if conn is not None:
        return _read_parked_plans(conn)

    try:
        owned = _open_state_db()
    except Exception:  # noqa: BLE001
        return []
    if owned is None:
        # No state database: nothing is parked.  The reader does not
        # create one just to look (see ``_open_state_db``).
        return []
    try:
        return _read_parked_plans(owned)
    finally:
        _close_quietly(owned)


def _read_parked_plans(conn) -> List[ParkedPlan]:
    """The whole-table read, on a connection the caller owns."""
    try:
        execution_rows = _fetch_rows(conn, _EXECUTION_SQL, _EXECUTION_COLUMNS)
        verification_rows = _fetch_rows(conn, _VERIFICATION_SQL, _VERIFICATION_COLUMNS)
    except Exception:  # noqa: BLE001
        return []
    return select_parked_plans(execution_rows, verification_rows)


def plan_parked(conn, plan_id) -> bool:
    """Is this one plan parked because of quota?

    This is the single-plan read: the death latch asks it before it
    declares a plan dead, because a plan parked for capacity is not
    dead — it is waiting, and reviving it is exactly the point.

    The answer is ``True`` only for a plan the durable rows prove to be
    parked.  Everything else — a plan the human stopped, a plan whose
    round failed, a plan with no rows, a row that could not be read —
    answers ``False``.  The asymmetry is deliberate: the latch uses a
    ``False`` to leave a plan alone, so a doubt must resolve to
    ``False`` and never to ``True``.

    Args:
        conn: the connection to read on.  ``None`` means "open your own"
            — the reader resolves the state database's path, opens it,
            and closes it again before returning.
        plan_id: the plan to ask about.

    Returns:
        Whether the plan is parked because of quota.
    """
    if conn is not None:
        return _plan_parked(conn, plan_id)

    try:
        owned = _open_state_db()
    except Exception:  # noqa: BLE001
        return False
    if owned is None:
        # No state database: no row proves the plan is parked.  The
        # reader does not create one just to look (see
        # ``_open_state_db``).
        return False
    try:
        return _plan_parked(owned, plan_id)
    finally:
        _close_quietly(owned)


def _plan_parked(conn, plan_id) -> bool:
    """The single-plan read, on a connection the caller owns."""
    target = plan_id if isinstance(plan_id, str) else str(plan_id)
    try:
        execution_rows = _fetch_rows(
            conn, _EXECUTION_SQL + " WHERE plan_id = ?", _EXECUTION_COLUMNS, (target,)
        )
        verification_rows = _fetch_rows(
            conn, _VERIFICATION_SQL + " WHERE plan_id = ?", _VERIFICATION_COLUMNS, (target,)
        )
    except Exception:  # noqa: BLE001
        return False
    selected = select_parked_plans(execution_rows, verification_rows)
    return any(entry.plan_id == target for entry in selected)


# ---------------------------------------------------------------------------
# The resume callers
# ---------------------------------------------------------------------------
#
# Requirement R2 says the revived work goes back out through the paths
# that already exist: an execution is started the way a human starts
# it, a verification round is continued the way a human continues it.
# So this half of the module starts nothing of its own — it names a
# plan, calls the start path, and reads the answer.
#
# The routes are imported **inside** the callers, not at module scope:
# the route modules import the application module (``server``), which
# imports the routers back at its own module scope.  A module-level
# import here would close that loop, and the failure would surface as
# a circular import in whatever imported this module first.
#
# The start path is injectable (``starter=``) so that the contract can
# be pinned without a server, a database or a plan directory.

#: The status codes that mean "the start path refused this plan".
#:
#: 409 — the plan already has a live executor (the ledger's
#: single-instance guard) or its phase does not permit a start;
#: 410 — the plan is archived;
#: 400 — the request is not well-formed for this plan (the working
#: directory is missing or unusable).
#:
#: All three are ordinary outcomes of a sweep over many plans, not
#: errors: "this plan cannot be started right now" is an answer.
_REFUSAL_STATUSES = (400, 409, 410)


def _response_status(response: Any) -> Optional[int]:
    """The HTTP status an answer carries, or ``None`` when it carries none.

    The start paths answer in two shapes: a plain mapping on success
    (no status at all) and a ``JSONResponse`` on a refusal (a status
    and an error body).  Both have to be read, because a resumer that
    only caught raised exceptions would call a *returned* refusal a
    success — and would report a plan as running when it is not.
    """
    status = getattr(response, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _start_outcome(plan: ParkedPlan, starter: Callable[[str], Any], kind: str) -> bool:
    """Call ``starter`` for ``plan`` and read the answer.

    Args:
        plan: the parked plan to start.
        starter: the start path to call — it takes the plan id and
            nothing else.
        kind: what is being started, for the log line.

    Returns:
        Whether the plan is now started.  Every failure answers
        ``False``; none of them is raised.
    """
    try:
        response = starter(plan.plan_id)
    except HTTPException as exc:
        if exc.status_code in _REFUSAL_STATUSES:
            # An ordinary refusal: the plan is running, or archived,
            # or not startable as it stands.  The sweep moves on.
            log.warning(
                "quota_resume %s start refused plan=%s status=%s detail=%s",
                kind,
                plan.plan_id,
                exc.status_code,
                exc.detail,
            )
            return False
        # Anything else is an anomaly — a 404 on a plan that has a
        # parked row, say.  It is still not worth stopping the sweep
        # for, but it is worth being loud about.
        log.exception(
            "quota_resume %s start failed plan=%s status=%s",
            kind,
            plan.plan_id,
            exc.status_code,
        )
        return False
    except Exception:  # noqa: BLE001
        # The caller is a heartbeat thread that sweeps every parked
        # plan.  An exception raised here would unwind that thread and
        # stop the sweep for every plan, so it is absorbed — and
        # logged, because a start that blows up is not ordinary.
        log.exception("quota_resume %s start raised plan=%s", kind, plan.plan_id)
        return False

    status = _response_status(response)
    if status is not None and status >= 400:
        log.warning(
            "quota_resume %s start refused plan=%s status=%s",
            kind,
            plan.plan_id,
            status,
        )
        return False
    return True


def resume_execution(plan: ParkedPlan, starter: Optional[Callable[[str], Any]] = None) -> bool:
    """Put a parked execution back to work, through the existing start.

    The start path is ``routes.execution.start_execution`` — the same
    one a human ``POST /start`` reaches.  It is that path, and not a
    private one, because the guards it owns are the ones that keep a
    plan single-instance: the ledger's live-executor check (409), the
    routing CAS, the project-directory check (400).  A resumer with a
    second copy of those guards would be free to disagree with them,
    and the disagreement shows up as two executors on one plan.

    The single-instance rule is therefore *not* re-decided here: the
    ledger is asked inside the start path, and its 409 is what this
    function reports as "not started".

    Args:
        plan: the parked execution to revive.
        starter: the start path to call.  ``None`` means the real
            route — injectable so that the contract can be pinned
            without a server.

    Returns:
        Whether the execution was started.
    """
    if starter is None:
        from routes.execution import start_execution

        starter = start_execution
    return _start_outcome(plan, starter, KIND_EXECUTION)


def resume_verification(plan: ParkedPlan, starter: Optional[Callable[[str], Any]] = None) -> bool:
    """Continue a parked verification round, through the existing start.

    The start path is ``routes.verification.start_verification`` — the
    same one a human ``POST /start`` reaches, and the one that knows
    how to pick a round back up (the parked round continues *as
    itself*: same round number, verdicts untouched).  Only the plan id
    is passed, so the resumer cannot ask for a new round and throw
    away what the stopped one had already decided.

    Args:
        plan: the parked verification round to continue.
        starter: the start path to call.  ``None`` means the real
            route — injectable so that the contract can be pinned
            without a server.

    Returns:
        Whether the verification round was started.
    """
    if starter is None:
        from routes.verification import start_verification

        starter = start_verification
    return _start_outcome(plan, starter, KIND_VERIFICATION)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
#
# The sweep is the pass that ties the three halves together: it reads
# the parked plans, asks whether the quota window is open again, and
# revives what it can.
#
# Two things about it are worth stating before the code, because both
# are easy to get wrong in the direction of "more work".
#
# **The table is read before the probe is sent.**  The heartbeat that
# drives this sweep runs every thirty seconds.  A sweep that probed
# first would ask the provider quota endpoint on every tick — forever,
# on a machine with nothing parked.  Nothing parked means no probe is
# sent at all.
#
# **Nothing escapes.**  The sweep runs inside a heartbeat thread: an
# exception raised out of it would stop the revival of *every* parked
# plan, not just the one that failed.  So every failure — a probe that
# blows up, a revival that raises — is absorbed here, and the sweep
# carries on.

#: The environment variable that points the probe at a file instead of
#: at the provider quota endpoint.
#:
#: It exists so that an end-to-end case can *change* the quota
#: reading — write the file, sweep, watch the plan start — with no
#: provider endpoint involved.  The file is read as a JSON object; a
#: file that is missing, unreadable, corrupt, or not an object at all
#: is the empty reading, which is not a recovery.
_QUOTA_FIXTURE_ENV = "PDT_QUOTA_FIXTURE"

#: The scene the sweep's probe is logged under, next to the other
#: reads of the same payload.
_PROBE_SCENE = "quota_resume_sweep"


def _default_probe() -> Mapping[str, Any]:
    """Read the quota window: the fixture file, or the shared watchdog.

    The watchdog is the one place in the product that reads the
    provider quota payload, and it merges concurrent readers into one
    request.  A second reader would be a second opinion about the same
    window, and the window is a fact rather than an opinion — so the
    sweep asks the watchdog.

    The fixture file is the seam for the end-to-end case: it lets that
    case *change* the reading by writing a file, with no provider
    endpoint in the picture.  The file is a JSON object; an object
    with an ``exhausted`` key is one reading, and an object of
    provider names is one reading per provider.

    **This never raises.**  The caller is a heartbeat thread: a raise
    here would kill that thread — and with it the revival of every
    parked plan — over a truncated file.  So every way the file can be
    wrong answers the empty reading, which means "no reading", which
    means "no recovery", which means the next tick asks again.
    """
    fixture = os.environ.get(_QUOTA_FIXTURE_ENV)
    if fixture:
        return _fixture_reading(fixture)

    try:
        from quota_watchdog import get_shared_quota_watchdog

        return get_shared_quota_watchdog().probe(scene=_PROBE_SCENE)
    except Exception:  # noqa: BLE001
        # The watchdog's own probe does not raise; this is the
        # wrapper around it (an import that fails, a watchdog that
        # cannot be built).  A sweep that cannot read the window
        # revives nothing, and says so.
        log.exception("quota_resume the quota reading could not be taken")
        return {}


def _fixture_reading(path: str) -> Mapping[str, Any]:
    """Read the quota reading out of the fixture file.

    A missing file, an unreadable file, a file that is not JSON, and a
    file whose top level is not an object all answer the empty
    reading.  So does an object whose entries carry no reading at all.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict):
        return {}

    if "exhausted" in payload:
        return {"fixture": SimpleNamespace(exhausted=bool(payload["exhausted"]))}

    reading: dict = {}
    for name, value in payload.items():
        if isinstance(value, bool):
            reading[name] = SimpleNamespace(exhausted=value)
        elif isinstance(value, dict):
            reading[name] = SimpleNamespace(exhausted=bool(value.get("exhausted", False)))
    return reading


def _as_clock(value: Any) -> float:
    """The moment the sweep is running at, as a number of seconds.

    A caller that hands over something that is not a number gets the
    wall clock instead of an exception: the sweep's promise is that
    nothing escapes it, and the clock is the least important thing it
    could fail on.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _in_cooldown(plan_id: str, now: float) -> bool:
    """Was this plan attempted too recently to be tried again?

    A plan is in its cooldown while less than :data:`MIN_RETRY_SEC`
    has passed since it was last attempted.  The boundary belongs to
    the requirement: at exactly the retry window the plan may be
    tried again, so R2's "within two minutes" is met and not missed.
    """
    last = _LAST_ATTEMPT.get(plan_id)
    if last is None:
        return False
    try:
        elapsed = float(now) - float(last)
    except (TypeError, ValueError):
        return False
    return elapsed < MIN_RETRY_SEC


def run_resume_sweep(
    conn=None,
    probe=None,
    now=None,
    resume_execution_fn=None,
    resume_verification_fn=None,
) -> List[str]:
    """Sweep the parked plans and revive the ones the quota lets through.

    This is the pass the heartbeat drives.  It answers three questions
    in one go:

    1. *Who is parked?*  The table is read first, and an empty table
       ends the sweep — no probe is sent, nothing is started.
    2. *Is the quota window open again?*  The probe answers that.  A
       reading that is empty, or one where every provider is still
       exhausted, is the answer "still shut", and nothing is started.
    3. *Who can be revived?*  Each parked plan that is not inside its
       retry window is handed to the start path that owns its kind of
       work, and the answer is read.

    The two lines R4 asks for are written here, and they are kept
    apart: ``quota_recovered_resume`` says a revival happened, and
    ``quota_resume_skipped`` says one did not.  A sweep that reported
    a refusal in the success vocabulary would tell an operator that a
    plan was running when it is not.

    Args:
        conn: the connection to read the parked plans on.  ``None``
            means "open your own" — the reader resolves the state
            database's path, opens it and closes it again.
        probe: the quota reading to consult.  ``None`` means the
            default probe — the shared quota watchdog, or the fixture
            file when one is pointed at.
        now: the moment the sweep is running at, as a number of
            seconds.  ``None`` means the wall clock.  It is the clock
            the retry window is measured against.
        resume_execution_fn: the caller that revives a parked
            execution.  ``None`` means :func:`resume_execution`.
        resume_verification_fn: the caller that continues a parked
            verification round.  ``None`` means
            :func:`resume_verification`.

    Returns:
        The plan ids that were revived, in the order they were
        revived.  Empty when nothing was parked, when the window is
        still shut, or when every candidate was inside its retry
        window.
    """
    parked = read_parked_plans(conn)
    if not parked:
        # Nothing parked: the probe is not sent.  A machine with
        # nothing to revive must not be made to ask the provider
        # quota endpoint every thirty seconds.
        return []

    if probe is None:
        probe = _default_probe
    try:
        reading = probe() or {}
    except Exception:  # noqa: BLE001
        # A probe that blows up is a reading that could not be taken.
        # The sweep revives nothing and the next tick asks again.
        log.exception("quota_resume the quota probe failed")
        return []
    if not isinstance(reading, Mapping):
        reading = {}
    if not quota_recovered(reading):
        return []

    stamp = time.time() if now is None else _as_clock(now)
    revived: List[str] = []
    for plan in parked:
        if _in_cooldown(plan.plan_id, stamp):
            # Attempted too recently.  The window exists so that a
            # plan whose revival is refused does not fill the log
            # with a line every thirty seconds.
            continue
        # The attempt is recorded before it is made, so that a
        # revival that raises still puts the plan inside its window.
        _LAST_ATTEMPT[plan.plan_id] = stamp
        revived_plan = _revive_one(plan, resume_execution_fn, resume_verification_fn)
        if revived_plan:
            log.info(
                "quota_recovered_resume plan=%s round=%s reason=quota_recovered",
                plan.plan_id,
                plan.round,
            )
            revived.append(plan.plan_id)
        else:
            log.warning(
                "quota_resume_skipped plan=%s kind=%s reason=start_refused",
                plan.plan_id,
                plan.kind,
            )
    return revived


def _revive_one(
    plan: ParkedPlan,
    resume_execution_fn: Optional[Callable[[ParkedPlan], Any]],
    resume_verification_fn: Optional[Callable[[ParkedPlan], Any]],
) -> bool:
    """Revive one parked plan, and absorb anything it throws.

    The caller is a heartbeat thread: a raise here would unwind that
    thread and stop the sweep for every plan, not just this one.  So
    the failure is absorbed, logged, and reported as "not revived" —
    and the sweep moves on to the next plan.
    """
    if plan.kind == KIND_VERIFICATION:
        caller = resume_verification_fn
        if caller is None:
            caller = resume_verification
    else:
        caller = resume_execution_fn
        if caller is None:
            caller = resume_execution
    try:
        return bool(caller(plan))
    except Exception:  # noqa: BLE001
        log.exception(
            "quota_resume the revival failed plan=%s kind=%s",
            plan.plan_id,
            plan.kind,
        )
        return False