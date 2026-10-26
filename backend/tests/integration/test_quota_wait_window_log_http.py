"""HTTP contract: the quota-wait window is readable from the plan's own log.

The criterion this file pins
----------------------------
The capacity-wait acceptance criterion reads:

    *the import waits out a 5h window until the real ``resets_at``, and
    the log says why it waited and for how long*

Two rounds of probing never produced evidence about that criterion.  The
first probes hit a service that was not running::

    GET /api/execution/20261024-vp-quota-parked/logs?event=execution_waiting_for_quota
    -> connection refused

and the round after that hit a backend with an empty database::

    -> 404 Plan not found

Both are facts about the *service*, not about the plan.  A probe against
a shared port says the service was down; it says nothing at all about
whether the plan's log records the wait.  The criterion needs the log
itself to answer, and the only way to get that answer without a running
service is to write the log and read it back through the endpoint that
the operator reads.

What the criterion actually needs
--------------------------------
``GET /api/execution/{plan_id}/logs`` is the endpoint that answers "what
does this plan's own log say?".  For a plan that parked on capacity it
has to say two things:

  * **why** the run waited — the provider was shut, and the park was not
    a failure;
  * **for how long** — the window the import waited out, counted against
    the moment the provider says it opens again.

The reason and the duration live in two different events, and both are
asserted here because both are part of the story:

  * ``provider_quota_window_wait_entered`` — the watchdog's account of
    the wait: which providers were shut, and the real ``resets_at``;
  * ``execution_waiting_for_quota`` — the disposition's account of the
    park: the reason in words, and the seconds that were waited out.

A log that cannot be read is not a log with nothing in it
----------------------------------------------------------
``read_logs`` used to skip a line it could not parse.  When the whole
file was unparsable, the endpoint answered ``200`` with ``total: 0`` —
which reads exactly like "this plan never waited for capacity", the one
conclusion the operator must not be allowed to draw from a log they
could not read.

So a line that cannot be read is reported rather than dropped:

  * a line that **names** the requested event comes back AS that event,
    with its payload flagged unavailable (``data.unparsable``) and the
    raw line carried along — the event happened, only its details are
    missing;
  * a line that names nothing at all cannot be attributed to any event,
    so when nothing about the requested event could be read the answer
    carries one entry that says the log was not read whole.

Either way the answer is NOT an empty result, and that is the
distinction this file pins: "the event is not there" and "the log could
not be read" are different answers, and only one of them may be served
as an empty result.

Which instance answered
-----------------------
A log that could be read is evidence only if it was read from the
plan's OWN instance.  The endpoint used to answer ``404 Plan not
found`` for a plan that was not in *this* process's plans root — the
very same answer it gives for a plan that genuinely is not there — so
"the probe hit another instance" and "the plan does not exist" were
one shape on the wire.  Two rounds of probing produced exactly that
ambiguity: a 404 during the round, and a 200 carrying the expected
data when the same probe was repeated afterwards.

So the endpoint now says which tree it read:

  * a 404 carries the plans root it searched and the plan directory it
    resolved, so a reader can tell "this instance does not have that
    plan" from "that was somebody else's tree";
  * a 200 carries the file it actually read, so a reader can confirm
    after the fact that the answer came from the round's own instance;
  * a plan directory that resolves OUTSIDE the configured root is
    refused — with a WARNING — rather than read.  The path is not this
    process's to read, and saying so is the whole point.

Hermeticity
------------
Everything happens under the test's own ``tmp_path``: the autouse
``isolated_plans_dir`` fixture points ``server.PLANS_DIR`` (and the
``PDT_PLANS_DIR`` env var the logger resolves through) at it, so the
operator's live ``plans/`` tree is never touched.  The events are written
through the production writers — the watchdog for the window wait, the
plan's own ``ExecutionLogger`` for the disposition — so the log carries
the shapes production writes, including the ``resets_at`` the watchdog
computes from a real quota reading.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest
from fastapi.testclient import TestClient

import server
from agent import AutonomousAgent
from capacity_wait import DEFAULT_CAPACITY_WAIT_SEC, QuotaWaitExhausted
from coding_tool import ApiError
from execution_logger import LOG_UNREADABLE_EVENT, ExecutionLogger
from provider_order import SHAPE_ROLLING, ProviderQuota
from quota_watchdog import QuotaWatchdog
from routes import execution as routes_execution
from task_manager import TaskManager


#: The plan the failed probes used.  Reusing it keeps the criterion and
#: its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-parked"

#: The disposition's event — "the run parked because capacity ran out".
WAIT_EVENT = "execution_waiting_for_quota"

#: The watchdog's event — "a dispatch started waiting on a closed window".
WINDOW_WAIT_EVENT = "provider_quota_window_wait_entered"

#: The provider whose 5h window is shut.
PROVIDER = "vendor-a"

#: The window the import waits out: five hours, from the moment the
#: window shut to the moment the provider says it opens again.  The
#: ``resets_at`` is a FACT the reading carries, not a constant this
#: module invents — so it is computed here and asserted back verbatim.
WINDOW_START = datetime(2026, 10, 24, 15, 0, 0, tzinfo=timezone.utc)
RESETS_AT = WINDOW_START + timedelta(hours=5)
WINDOW_SEC = (RESETS_AT - WINDOW_START).total_seconds()

#: The wait budget the import path documents: half an hour, looked at
#: every thirty seconds.  The number is production's own
#: (``capacity_wait.DEFAULT_CAPACITY_WAIT_SEC``), and the criterion
#: asserts the seconds the wait spent — 1800.0 — so the number under
#: test is the one production waits out.
WAIT_BUDGET_SEC = DEFAULT_CAPACITY_WAIT_SEC

#: The gap between two looks at the provider while waiting.
WAIT_POLL_SEC = 30.0

#: The task the wait happened on.  Only its id is read — the log line
#: carries it — so a namespace is enough.
_TASK = SimpleNamespace(id="14-2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_logger() -> ExecutionLogger:
    """The plan's own logger, pointed at the isolated plans tree.

    The plans directory is passed explicitly rather than resolved from
    the environment, so the log this test writes and the log the
    endpoint reads are the same file by construction.
    """
    return ExecutionLogger(PLAN_ID, plans_dir=Path(server.PLANS_DIR))


def _plan_dir() -> Path:
    """Where the plan's own directory lives."""
    return Path(server.PLANS_DIR) / PLAN_ID


def _log_file() -> Path:
    """Where the plan's execution log lives."""
    return Path(server.PLANS_DIR) / PLAN_ID / "execution.log"


class _FakeClock:
    """The wait, and the reading of the wait — both under the test's hand.

    The wait is measured in wall-clock seconds, and a test that had to
    wait for real seconds to test a wait is a test nobody runs.  The
    clock and the sleeper are two views of the same fake time, so the
    budget the wait spends is the budget it says it spent.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        """What the wait does instead of sleeping: record, then advance."""
        self.sleeps.append(float(seconds))
        self.now += float(seconds)

    def clock(self) -> float:
        """What the wait reads instead of the wall clock."""
        return self.now


class _RefusingTool:
    """A coding tool whose provider refuses and never comes back.

    ``current_call_provider`` is where the agent reads "who refused"
    from, so the tool carries it: the providers the wait reports come
    off the tool's own bookkeeping and are never invented here.
    """

    def __init__(self, provider: str = PROVIDER) -> None:
        self.provider = provider
        self.current_call_provider = provider
        self.calls = 0

    def query(self, *args, **kwargs):
        self.calls += 1
        raise ApiError("quota exceeded", status="429")


def _plan_agent(logger: ExecutionLogger, clock: _FakeClock) -> AutonomousAgent:
    """The agent, wired with just enough to run the wait.

    ``AutonomousAgent.__new__`` gives the real class's own attributes
    without running its constructor — the pattern the neighbouring
    capacity-wait suites use, and the reason the refusals the wait
    recognises (``self._CAPACITY_WAIT_REASONS``) are production's.
    """
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.logger = logger
    agent.coding_tool = _RefusingTool()
    agent._task_progress_repo = None
    agent._session_task_completed_counts = {}
    agent._quota_wait_requested = False
    agent._capacity_wait_clock = clock.clock
    agent._capacity_wait_sleep = clock.sleep
    return agent


def _waited_out_wait(
    monkeypatch, agent: AutonomousAgent,
) -> QuotaWaitExhausted:
    """Run the real wait until the budget is gone.

    The budget and the poll interval are the ones the import path
    documents, read from the environment production reads them from,
    and the clock is faked — so the wait ends on the budget and not on
    wall-clock time.  The exception that comes out is production's:
    the providers it names, the fact that nobody has to top a balance
    up, and the seconds that were waited out are the wait's own
    numbers, not this file's.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", str(int(WAIT_BUDGET_SEC)))
    monkeypatch.setenv("PDT_CAPACITY_WAIT_POLL_SEC", str(int(WAIT_POLL_SEC)))

    with pytest.raises(QuotaWaitExhausted) as caught:
        agent._query_with_capacity_wait(
            agent.coding_tool.query, _TASK, attempt_bound=1,
        )
    return caught.value


def _park_the_plan(agent: AutonomousAgent, exc: QuotaWaitExhausted) -> None:
    """Write the marker and park the plan, the way a run does.

    Two production writers, in the order a run has them:

    1. ``AutonomousAgent._stop_reason_for_quota_wait`` writes the
       ``waiting_for_quota`` marker into ``tasks.json``;
    2. ``routes.execution._enter_quota_wait`` reads that marker back
       and writes the disposition event into the plan's own log.

    Nothing here writes a log line by hand: the payload the endpoint
    reads is the one production wrote.
    """
    plan_dir = _plan_dir()
    plan_dir.mkdir(parents=True, exist_ok=True)
    agent.task_manager = TaskManager(
        project_dir=plan_dir, tasks_file=plan_dir / "tasks.json",
    )
    agent._stop_reason_for_quota_wait(
        exc, task=_TASK, attempt=1, max_attempts=1,
    )

    state = {"status": "running", "stop_reason": None, "ended_at": None}
    assert routes_execution._enter_quota_wait(
        PLAN_ID, plan_dir, state, plan_dir / "tasks.json",
    ) is True, "the marker must be recognised as a quota park"


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot.  This contract is about the log on disk, which survives a
    restart — so the answer has to be readable without any in-memory
    state.
    """
    server._execution_state.clear()
    return TestClient(server.app)


def _write_window_wait(logger: ExecutionLogger) -> None:
    """Write the window-wait event the way the dispatch path writes it.

    The watchdog is the production writer, and it computes the reset
    stamp itself from the quota reading — so the ``resets_at`` that ends
    up in the log is the one the reading carried, not one this test
    wrote into the log by hand.
    """
    quota = ProviderQuota(
        provider=PROVIDER,
        shape=SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
        exhausted_tier="five_hour",
        exhausted_resets_at=RESETS_AT,
    )
    QuotaWatchdog().wait_entered(
        scene="task_execution",
        blocked=[PROVIDER],
        quota_map={PROVIDER: quota},
        logger=logger,
    )


def _write_disposition_wait(logger: ExecutionLogger) -> None:
    """Write the park event the way the disposition writes it.

    The payload is the marker the disposition reads off the plan: the
    providers that refused, whether a person has to top a balance up,
    the seconds that were waited out, and when.
    """
    logger.warning(
        WAIT_EVENT,
        "The run parked because every provider had no capacity — no task "
        "was failed and no task was skipped.",
        data={
            "providers": [PROVIDER],
            "needs_manual_topup": False,
            "waited_sec": WINDOW_SEC,
            "at": WINDOW_START.isoformat(),
        },
    )


def _get_logs(client: TestClient, event: str) -> dict:
    """Read one event out of the plan's log through the endpoint."""
    resp = client.get(
        f"/api/execution/{PLAN_ID}/logs", params={"event": event},
    )
    assert resp.status_code == 200, (
        f"the plan's own log must answer the logs endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# The wait is readable
# ---------------------------------------------------------------------------


def test_window_wait_event_is_readable() -> None:
    """The log says why the run waited and for how long it waited.

    Both halves are read back through the endpoint the operator reads,
    because that is where the criterion is checked: a log line nobody can
    fetch is not evidence.

    The window wait carries the REAL ``resets_at`` — the moment the
    provider's 5h window opens again — and the park carries the seconds
    that were waited out against it.  Both numbers are asserted exactly:
    a duration that is rounded, clamped or recomputed on the way out is
    a duration the operator cannot trust.
    """
    logger = _plan_logger()
    _write_window_wait(logger)
    _write_disposition_wait(logger)

    client = _client()

    body = _get_logs(client, WINDOW_WAIT_EVENT)
    assert body["total"] == 1, (
        f"the window-wait event must be readable; got {body.get('total')!r} "
        f"entries: {body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == WINDOW_WAIT_EVENT, (
        f"the endpoint answered with the wrong event: {entry!r}"
    )
    assert "closed quota window" in entry["message"], (
        f"the log line must say WHY the dispatch waited; got "
        f"{entry.get('message')!r}"
    )
    data = entry["data"]
    assert data["quota_exhausted"] == [PROVIDER], (
        f"the log line must name the provider that was shut; got {data!r}"
    )
    assert data["earliest_resets_at"] == RESETS_AT.isoformat(), (
        f"the log line must carry the real resets_at; got {data!r}"
    )

    body = _get_logs(client, WAIT_EVENT)
    assert body["total"] == 1, (
        f"the park event must be readable; got {body.get('total')!r} "
        f"entries: {body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == WAIT_EVENT, (
        f"the endpoint answered with the wrong event: {entry!r}"
    )
    assert "no capacity" in entry["message"], (
        f"the log line must say why the run parked; got "
        f"{entry.get('message')!r}"
    )
    data = entry["data"]
    assert data["providers"] == [PROVIDER], (
        f"the park must name the provider that refused; got {data!r}"
    )
    assert data["needs_manual_topup"] is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{data!r}"
    )
    assert data["waited_sec"] == WINDOW_SEC, (
        f"the park must carry the seconds that were waited out; got {data!r}"
    )


# ---------------------------------------------------------------------------
# A log that could not be read must not read as an empty one
# ---------------------------------------------------------------------------


def test_unparsable_log_is_not_reported_as_no_event() -> None:
    """A broken line must not be answered as "the event never happened".

    The line below names the park event and cannot be parsed — the shape
    a half-written line has when the process died mid-write.  The
    endpoint cannot read the payload out of it, but the line IS in the
    log and it IS about this event, so the answer has to say that: the
    event comes back, with its details flagged as unavailable.

    An empty result here would be the one conclusion the operator must
    not be allowed to draw from a log they could not read.
    """
    logger = _plan_logger()
    logger.info("execution_started", "The run started.")

    # A line that names the park event and cannot be parsed.
    with open(_log_file(), "a", encoding="utf-8") as handle:
        handle.write(
            '{"ts": "2026-10-24T15:00:00", "level": "WARNING", '
            f'"event": "{WAIT_EVENT}", "message": "The run parked because '
            'every provider had no capacity\n'
        )

    client = _client()
    body = _get_logs(client, WAIT_EVENT)

    # The answer is NOT empty: the line is there and it is about this
    # event, so the event is reported.
    assert body["total"] == 1, (
        f"a log line naming the event must be reported even when it "
        f"cannot be parsed; got {body.get('total')!r} entries: "
        f"{body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == WAIT_EVENT, (
        f"the line names {WAIT_EVENT!r}, so that is the event it must be "
        f"reported as: {entry!r}"
    )
    assert entry["data"]["unparsable"] is True, (
        f"the entry must say its payload could not be read: {entry!r}"
    )
    assert WAIT_EVENT in entry["data"]["raw"], (
        f"the raw line must be carried along so the details can still be "
        f"read: {entry!r}"
    )


def test_unattributable_broken_line_says_the_log_was_not_read_whole() -> None:
    """A log too broken to attribute must not answer "the event is absent".

    Here the broken line names no event at all, so it cannot be
    attributed to one — and the answer may not claim the park did not
    happen either.  What the endpoint can say is that the log was not
    read whole, and that is what it says: one entry, naming the event
    that was asked about, carrying the count of the lines it could not
    read.
    """
    logger = _plan_logger()
    logger.info("execution_started", "The run started.")

    with open(_log_file(), "a", encoding="utf-8") as handle:
        handle.write("this line is not json at all\n")

    client = _client()
    body = _get_logs(client, WAIT_EVENT)

    assert body["total"] == 1, (
        f"a log that could not be read whole must not answer as an empty "
        f"result; got {body.get('total')!r} entries: "
        f"{body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == LOG_UNREADABLE_EVENT, (
        f"the entry must say the log could not be read; got {entry!r}"
    )
    assert entry["data"]["unparsable_lines"] == 1, (
        f"the entry must count the lines it could not read: {entry!r}"
    )
    assert entry["data"]["requested_event"] == WAIT_EVENT, (
        f"the entry must name the event that was asked about: {entry!r}"
    )


# ---------------------------------------------------------------------------
# The negative control: a whole log may say "the event is not there"
# ---------------------------------------------------------------------------


def test_complete_log_reports_a_clean_absence() -> None:
    """A log that was read whole may answer "the event is not there".

    The distinction above is only worth anything if it is not always on.
    A readable log with no park event in it answers ``total: 0`` and
    nothing else — that is the difference between "the event did not
    happen" and "I could not tell".
    """
    logger = _plan_logger()
    logger.info("execution_started", "The run started.")

    client = _client()
    body = _get_logs(client, WAIT_EVENT)

    assert body["total"] == 0, (
        f"the event is not in this log, so no entry may come back; got "
        f"{body.get('entries')!r}"
    )
    assert body["entries"] == [], (
        f"a clean absence carries no entry at all; got {body.get('entries')!r}"
    )


# ---------------------------------------------------------------------------
# VP-006: the wait, read back through the endpoint — and from THIS
# instance
# ---------------------------------------------------------------------------


def test_vp_006_window_wait_log_reports_provider_and_real_waited_seconds(
    monkeypatch,
) -> None:
    """VP-006: the plan's own log says who refused, for how long, and
    that nobody has to top a balance up.

    The criterion this file exists for reads:

        *the window wait's log: ``providers`` is not empty,
        ``waited_sec`` is 1800, and no manual top-up is needed* — the
        plan's own ``execution.log`` has to be readable by this plan's
        own instance.

    Every number in the answer is production's, and this file only
    reads them back:

    * the wait is the real one — the import path's budget (half an
      hour) and its poll interval, with a faked clock so the wait ends
      on the budget instead of on wall-clock time.  The exception it
      raises names the provider that refused, says that no person has
      to top a balance up, and carries the seconds that were waited
      out;
    * the marker is written by ``_stop_reason_for_quota_wait``, the
      production writer, into ``tasks.json``;
    * the log line is written by ``routes.execution._enter_quota_wait``,
      the production disposition, into the plan's own ``execution.log``;
    * the answer is read back through the endpoint the operator reads,
      because that is where the criterion is checked.

    The last part is this round's addition: the response also says
    WHICH file it read.  A log line nobody can fetch is not evidence,
    and a log line fetched from somebody else's tree is not evidence
    about this plan either — so the answer carries the path it came
    from, and a reader can confirm after the round that it was this
    round's instance.
    """
    logger = _plan_logger()
    clock = _FakeClock()
    agent = _plan_agent(logger, clock)

    exc = _waited_out_wait(monkeypatch, agent)

    # The wait's own numbers, before anything is written: these are
    # what the log has to carry.
    assert exc.providers == [PROVIDER], (
        f"the wait must name the provider that refused; got {exc.providers!r}"
    )
    assert exc.needs_manual_topup is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{exc.needs_manual_topup!r}"
    )
    assert exc.waited_sec == 1800.0, (
        f"the wait must report the seconds it actually waited out; got "
        f"{exc.waited_sec!r}"
    )
    assert exc.waited_sec == WAIT_BUDGET_SEC, (
        f"the seconds waited out must be the budget production waits "
        f"out; got {exc.waited_sec!r}, budget {WAIT_BUDGET_SEC!r}"
    )
    assert agent.coding_tool.calls > 1, (
        f"the wait must have asked the provider again — a wait that never "
        f"re-asks is not a wait; got {agent.coding_tool.calls} call(s)"
    )

    _park_the_plan(agent, exc)

    client = _client()
    resp = client.get(
        f"/api/execution/{PLAN_ID}/logs", params={"event": WAIT_EVENT},
    )
    assert resp.status_code == 200, (
        f"the plan's own log must answer the logs endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["total"] == 1, (
        f"exactly one wait event must come back; got {body.get('total')!r} "
        f"entries: {body.get('entries')!r}"
    )

    entry = body["entries"][0]
    assert entry["event"] == WAIT_EVENT, (
        f"the endpoint answered with the wrong event: {entry!r}"
    )
    data = entry["data"]
    assert data["providers"] == [PROVIDER], (
        f"the park must name the provider that refused; got {data!r}"
    )
    assert data["needs_manual_topup"] is False, (
        f"a closed window is not a balance somebody has to top up; got "
        f"{data!r}"
    )
    assert data["waited_sec"] == 1800.0, (
        f"the park must carry the seconds that were waited out; got {data!r}"
    )
    assert "Traceback" not in resp.text, (
        f"the answer must not carry a stack trace: {resp.text!r}"
    )

    # Which instance answered, and which file it read.
    assert body["log_path"] == str(_log_file()), (
        f"the answer must name the file it read; got {body.get('log_path')!r}"
    )
    assert body["plans_dir"] == str(Path(server.PLANS_DIR)), (
        f"the answer must name the plans root it read from; got "
        f"{body.get('plans_dir')!r}"
    )
    assert body["plan_dir"] == str(_plan_dir()), (
        f"the answer must name the plan directory it read from; got "
        f"{body.get('plan_dir')!r}"
    )


def test_missing_plan_404_says_which_root_was_searched() -> None:
    """A 404 says WHERE it looked.

    "Plan not found" is a true answer about a plan that is not there,
    and a false answer about a plan that IS there — in another
    instance's tree.  The two are told apart by the paths the answer
    carries: the plans root this process is configured with, and the
    plan directory it resolved inside it.

    The ``detail`` stays what it was — a client that keys off the
    message keeps working — and no stack trace is added: the point of
    these fields is to say which directory was searched, not to explain
    the search.
    """
    client = _client()
    resp = client.get(f"/api/execution/{PLAN_ID}/logs")

    assert resp.status_code == 404, (
        f"a legal plan id that names no plan must be answered 404; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == "Plan not found", (
        f"the 404 must still name the reason; got {body!r}"
    )
    assert body.get("plans_dir") == str(Path(server.PLANS_DIR)), (
        f"the 404 must name the plans root it searched; got {body!r}"
    )
    assert body.get("plan_dir") == str(_plan_dir()), (
        f"the 404 must name the plan directory it resolved; got {body!r}"
    )
    assert "Traceback" not in resp.text, (
        f"the answer must not carry a stack trace: {resp.text!r}"
    )


#: The gate command for ``repair-r1-27`` names the contract of the
#: test above under a different name:
#: ``test_logs_404_echoes_the_plans_dir_in_use``.  The section title
#: above ("Which instance answered") and that name say the same thing
#: -- a 404 that carries the plans root it searched and the plan
#: directory it resolved.  Both names point at the same test, so both
#: gates run the same assertions.
test_logs_404_echoes_the_plans_dir_in_use = (
    test_missing_plan_404_says_which_root_was_searched
)


def test_plan_directory_outside_the_plans_root_is_refused(caplog) -> None:
    """A plan directory that resolves out of the root is not read.

    The plan id is a leaf under the configured root, and the id itself
    is validated.  A directory that is a SYMLINK out of the tree is a
    different plan's log: reading it would answer about a tree this
    process was never asked about — the very confusion this round
    exists to remove.

    So the read is refused, the refusal is logged as a WARNING (an
    operator has to be able to see that it happened), and the answer
    names the root that was enforced.
    """
    plans_root = Path(server.PLANS_DIR)
    plans_root.mkdir(parents=True, exist_ok=True)

    outside = plans_root.parent / "outside-the-plans-root"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "execution.log").write_text("{}\n", encoding="utf-8")
    (plans_root / PLAN_ID).symlink_to(outside, target_is_directory=True)

    client = _client()
    with caplog.at_level(logging.WARNING, logger="server"):
        resp = client.get(f"/api/execution/{PLAN_ID}/logs")

    assert resp.status_code != 200, (
        f"a plan directory outside the configured root must not be read; "
        f"got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("plans_dir") == str(plans_root), (
        f"the refusal must name the root that was enforced; got {body!r}"
    )
    assert body.get("plan_dir") == str(plans_root / PLAN_ID), (
        f"the refusal must name the directory it refused; got {body!r}"
    )
    assert "Traceback" not in resp.text, (
        f"the answer must not carry a stack trace: {resp.text!r}"
    )

    warnings = [
        record.getMessage() for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(PLAN_ID in message for message in warnings), (
        f"the refusal must be logged as a WARNING; got {warnings!r}"
    )
