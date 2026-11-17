"""The finishing stage's waits, bridged onto the existing HITL machinery.

Background
----------
The finishing stage can wait for a **human** (a merge needs an
approval) or for **something outside** (a CI run on another platform).
Architecture decision point 5 settles how the human half is stored:
the question goes into the ``plan_decisions`` table that already
exists, it is delivered through the channel the plan is already bound
to, and the wait registers the ``hitl_wait`` heartbeat so the watchdog
does not call a waiting plan dead.  The finishing stage therefore grows
**no** second decision store, no second delivery channel and no second
timeout vocabulary — it *uses* the ones that are already there, and
the existing ``auto``/``strict`` semantics stay exactly as they were.

The other half is the opposite: a wait for something outside asks
**nobody** anything, so it writes **no** decision row and delivers
nothing.  What it registers is the recheck — the call that says whether
the condition is satisfied yet.

What is asserted here
---------------------
The six properties the bridge is made of:

  * ``test_human_wait_creates_exactly_one_pending_decision`` — one row,
    ``pending``, and the id the wait reports is that row's id;
  * ``test_human_wait_question_verbatim`` — the question in the
    database is the question that was asked, character for character;
  * ``test_human_wait_delivers_once`` — the question is delivered
    exactly once;
  * ``test_external_wait_creates_no_decision_row`` — an external wait
    writes nothing into ``plan_decisions``;
  * ``test_approval_request_creates_pending_decision`` — an action the
    executor refused becomes a pending row that nothing answers by
    itself;
  * ``test_answered_decision_resumes_wait`` — an answer landed by the
    *existing* HITL service settles the wait;
  * ``test_terminate_rejects_late_answer`` — a wait that was given up
    on refuses a late answer with a conflict (409), not with an
    overwrite.

and the boundaries around them: the heartbeat is registered, a second
wait for the same item is refused without a second row or a second
delivery, a recheck that raises counts as "not satisfied", and the two
HITL vocabularies still carry exactly the members they carried before.

Two further sections cover the parts that only exist in production:
the **wiring** — the registry the finishing stage builds for itself,
with the real store and the plan's own binding — and the **drive**,
where a wait settles itself at the waiting point instead of waiting for
somebody else to come and settle it.  The last section is the negative
control: the ``auto``/``strict`` expiry semantics of the existing HITL
are pinned on both sides.

The database is read twice
--------------------------
Once through the store's own API and once through a plain SQL query
against the same file.  The store's API is what the backend uses; the
plain query is what an auditor would run, and it is the only way to
show that the *bytes* on disk carry the question verbatim rather than a
round trip through the model.

Test command::

    pytest backend/tests/integration/postprocess/test_waits_hitl_bridge.py -v
"""

from __future__ import annotations

import json
import shlex
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.service import answer_decision, expire_due
from hitl.store import HitlStore
from postprocess import waits as waits_module
from postprocess.executor import CommandRunner, NeedsApproval
from postprocess.orchestrator import PostProcessOrchestrator
from postprocess.progress import load_progress
from postprocess.report import TERMINAL_STATUS_FAILED
from postprocess.spec import freeze, load_spec
from postprocess.waits import (
    DEFAULT_WAIT_SECONDS,
    WaitConflict,
    WaitError,
    WaitRegistry,
    production_registry,
)
from tests.fixtures.postprocess.builders import FakeClock, make_item


#: The moment every case starts from — a fixed point, so the deadlines
#: in the assertions are written out rather than computed twice.
T0 = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)

#: The members the two HITL vocabularies carried before this bridge
#: existed.  The finishing stage reads them; it must never add to them.
DECISION_STATUS_MEMBERS = {
    "pending",
    "answered",
    "defaulted",
    "paused",
    "unroutable",
}
DECISION_MODE_MEMBERS = {"auto", "strict"}


class DeliverySink:
    """A delivery that records instead of sending.

    The production delivery is the plan's own ``hitl_config.json``
    binding, carried by the existing ``session_send`` hook.  A test
    must not send a message to anybody, so the sink records what would
    have been sent and sends nothing.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Record one delivery and return it."""
        self.calls.append(dict(payload))
        return payload

    @property
    def count(self) -> int:
        """How many deliveries were recorded."""
        return len(self.calls)


@pytest.fixture
def plans_root(tmp_path, monkeypatch) -> Path:
    """A private plans tree, so no case writes into the live one."""
    root = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


@pytest.fixture
def state_db(tmp_path, monkeypatch) -> Path:
    """A private state database, so no case writes into the live one."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def store(state_db: Path):
    """A HITL store on the private database."""
    opened = HitlStore(state_db)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def sink() -> DeliverySink:
    """A delivery sink that records instead of sending."""
    return DeliverySink()


@pytest.fixture
def clock() -> FakeClock:
    """A clock the case moves instead of waiting for."""
    return FakeClock(T0)


@pytest.fixture
def registry(plans_root, clock, store, sink) -> WaitRegistry:
    """A registry bridged onto the private store and the recording sink."""
    return WaitRegistry("p1", clock=clock, store=store, deliver=sink)


def decision_rows(db_path: Path) -> List[sqlite3.Row]:
    """The ``plan_decisions`` rows on disk, read with plain SQL."""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM plan_decisions ORDER BY created_at, decision_id"
        ).fetchall()
    finally:
        connection.close()


def human_item(item_id: str = "approve") -> dict:
    """A requirement item that waits for a human."""
    return make_item(id=item_id, wait={"type": "human"})


def external_item(item_id: str = "ci") -> dict:
    """A requirement item that waits for something outside."""
    return make_item(id=item_id, external=True, wait={"type": "external"})


# ---------------------------------------------------------------------------
# The human wait
# ---------------------------------------------------------------------------


def test_human_wait_creates_exactly_one_pending_decision(registry, state_db):
    """A human wait is one pending row, and the wait reports its id."""
    record = registry.create_human_wait(human_item(), question="批准合入？")

    rows = decision_rows(state_db)

    assert len(rows) == 1, (
        "a human wait is stored as exactly one decision row — a second row "
        "would be a second question"
    )
    assert rows[0]["status"] == "pending"
    assert rows[0]["plan_id"] == "p1"
    assert record["status"] == "pending"
    assert record["decision_id"] == rows[0]["decision_id"], (
        "the wait reports the id of the row the question was stored as"
    )
    assert record["wait_type"] == "human"


def test_human_wait_question_verbatim(registry, state_db):
    """The question in the database is the question that was asked."""
    question = "批准合入 main？ —— 逐字，不改一个字符"

    registry.create_human_wait(human_item(), question=question)

    rows = decision_rows(state_db)

    assert len(rows) == 1
    assert rows[0]["question"] == question, (
        "the question is stored verbatim — a reworded question is a "
        "different question"
    )


def test_human_wait_delivers_once(registry, sink):
    """The question is delivered exactly once."""
    record = registry.create_human_wait(human_item(), question="批准合入？")

    assert sink.count == 1, (
        "the question is delivered once — a second delivery is a second "
        "question asked of the same human"
    )
    assert sink.calls[0]["question"] == "批准合入？"
    assert sink.calls[0]["decision_id"] == record["decision_id"]
    assert sink.calls[0]["plan_id"] == "p1"


def test_human_wait_registers_the_heartbeat(registry, state_db):
    """The wait registers the heartbeat that keeps the watchdog off it."""
    registry.create_human_wait(human_item(), question="批准合入？")

    rows = decision_rows(state_db)

    assert rows[0]["heartbeat_at"], (
        "a question somebody is waiting for carries a liveness stamp — "
        "without one the watchdog reads the waiting plan as a dead one"
    )


def test_human_wait_leaves_nothing_to_answer_itself(registry, state_db, store):
    """The row carries no default answer, so nothing may answer it."""
    registry.create_human_wait(human_item(), question="批准合入？")

    rows = decision_rows(state_db)

    assert rows[0]["default_option"] is None, (
        "a question with a default answer is a question the system may "
        "answer on the human's behalf — this one has none"
    )
    assert rows[0]["mode"] == "strict", (
        "the row is strict — the existing HITL's own word for 'there is "
        "no substitute for a human'.  An auto question is a question "
        "the system may answer on the human's behalf"
    )
    assert rows[0]["deadline_at"] is None, (
        "the wait's deadline lives in the wait record; the HITL row "
        "carries no deadline of its own, so the existing sweep has "
        "nothing of ours to settle"
    )

    assert expire_due(store=store) == 0

    assert decision_rows(state_db)[0]["status"] == "pending", (
        "the existing auto/strict sweep must leave our question alone"
    )


# ---------------------------------------------------------------------------
# The external wait
# ---------------------------------------------------------------------------


def test_external_wait_creates_no_decision_row(registry, state_db, sink):
    """An external wait asks nobody, so it writes no row and sends nothing."""
    record = registry.create_external_wait(
        external_item(), check=lambda: True, question="CI 通过了吗？"
    )

    assert decision_rows(state_db) == [], (
        "nothing outside is asked a question, so there is no question to "
        "store — a row here would be a question nobody was asked"
    )
    assert sink.count == 0, (
        "an external wait has nobody to tell"
    )
    assert record["wait_type"] == "external"
    assert record["status"] == "pending"
    assert record["decision_id"] is None


def test_external_recheck_that_raises_is_not_satisfied(registry):
    """A recheck that raises is a recheck that did not satisfy anything."""
    registry.create_external_wait(
        external_item(),
        check=lambda: (_ for _ in ()).throw(RuntimeError("the CI page is down")),
    )

    outcome = registry.check_external_wait("ci")

    assert outcome["satisfied"] is False, (
        "an exception is not evidence that the condition is satisfied"
    )
    assert outcome["status"] == "pending", (
        "the wait goes on: it is not satisfied, but it has not failed either"
    )
    assert "the CI page is down" in (outcome["error"] or "")


def test_external_recheck_that_says_yes_ends_the_wait(registry):
    """A satisfied condition ends the wait — the same lifecycle as a human's."""
    registry.create_external_wait(external_item(), check=lambda: True)

    outcome = registry.check_external_wait("ci")

    assert outcome["satisfied"] is True
    assert outcome["status"] == "answered"
    assert registry.get("ci")["status"] == "answered"


# ---------------------------------------------------------------------------
# The approval request
# ---------------------------------------------------------------------------


def test_approval_request_creates_pending_decision(registry, state_db, store):
    """An action the executor refused becomes a pending row — and only that."""
    approval = NeedsApproval(
        question="批准合入 main？", command=["git", "push", "origin", "main"]
    )

    record = registry.create_approval_request(make_item(id="merge"), approval)

    rows = decision_rows(state_db)

    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["question"] == "批准合入 main？"
    assert rows[0]["answer"] is None, (
        "nobody answered the request yet — and the system did not answer "
        "it on the human's behalf"
    )
    assert rows[0]["answered_by"] is None
    assert rows[0]["answered_at"] is None
    assert record["decision_id"] == rows[0]["decision_id"]

    assert expire_due(store=store) == 0, (
        "the sweep must not settle an approval request either"
    )
    assert decision_rows(state_db)[0]["status"] == "pending"


def test_approval_request_is_not_answered_in_bulk(registry, state_db):
    """One request is one row — the system does not approve a batch."""
    for index in range(3):
        registry.create_approval_request(
            make_item(id=f"merge-{index}"),
            NeedsApproval(
                question=f"批准合入 main？ #{index}",
                command=["git", "push", "origin", "main"],
            ),
        )

    rows = decision_rows(state_db)

    assert len(rows) == 3, (
        "three requests are three rows — one row each, never a batch "
        "answered on the human's behalf"
    )
    assert [row["status"] for row in rows] == ["pending", "pending", "pending"]
    assert all(row["answer"] is None for row in rows)


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def test_answered_decision_resumes_wait(registry, store):
    """An answer landed by the existing HITL service settles the wait."""
    record = registry.create_human_wait(human_item(), question="批准合入？")

    status, code = answer_decision(
        record["decision_id"], "proceed", answered_by="operator", store=store
    )

    assert (status, code) == ("answered", 200), (
        "the existing HITL service is what answers the question — the "
        "bridge does not answer it a second time"
    )

    resolved = registry.resolve_human_wait("approve", "proceed")

    assert resolved["status"] == "answered", (
        "the wait follows the decision: the question was answered, so the "
        "wait is over"
    )
    assert registry.get("approve")["status"] == "answered"


def test_resolve_human_wait_answers_a_pending_question(registry, store, state_db):
    """The bridge may settle the wait itself, through the existing CAS."""
    record = registry.create_human_wait(human_item(), question="批准合入？")

    resolved = registry.resolve_human_wait("approve", "proceed")

    assert resolved["status"] == "answered"
    rows = decision_rows(state_db)
    assert rows[0]["status"] == "answered"
    assert rows[0]["answer"] == '"proceed"'


def test_terminate_rejects_late_answer(registry, store):
    """A wait that was given up on refuses a late answer."""
    record = registry.create_human_wait(human_item(), question="批准合入？")

    registry.terminate("approve", operator="operator")

    status, code = answer_decision(record["decision_id"], "proceed", store=store)

    assert code == 409, (
        "the question is settled, so a late answer is a conflict — the "
        "answer must not overwrite the decision that was closed"
    )
    assert status == "unroutable"

    with pytest.raises(WaitConflict) as conflict:
        registry.resolve_human_wait("approve", "proceed")

    assert conflict.value.status_code == 409, (
        "the bridge reports the conflict with the status the API layer "
        "answers it with"
    )


# ---------------------------------------------------------------------------
# The boundaries
# ---------------------------------------------------------------------------


def test_a_second_wait_for_the_same_item_is_refused(registry, state_db, sink):
    """Asking twice is a conflict — no second row, no second delivery."""
    registry.create_human_wait(human_item(), question="批准合入？")

    with pytest.raises(WaitConflict):
        registry.create_human_wait(human_item(), question="再问一次？")

    assert len(decision_rows(state_db)) == 1, (
        "a refused second wait leaves no second row"
    )
    assert sink.count == 1, (
        "a refused second wait delivers nothing either"
    )


def test_a_human_wait_without_a_store_is_refused(clock, plans_root):
    """A question that cannot be stored is a question nobody can answer."""
    registry = WaitRegistry("p1", clock=clock)

    with pytest.raises(WaitError):
        registry.create_human_wait(human_item(), question="批准合入？")


def test_the_hitl_vocabularies_are_closed():
    """The finishing stage reads the HITL vocabularies; it never adds to them."""
    assert {status.value for status in DecisionStatus} == DECISION_STATUS_MEMBERS
    assert {mode.value for mode in DecisionMode} == DECISION_MODE_MEMBERS


# ---------------------------------------------------------------------------
# The production wiring
# ---------------------------------------------------------------------------
#
# The module-level contract above is worth nothing if the production path
# does not use it: a registry without a store cannot write a question down,
# and a registry without a channel cannot deliver one.  So the cases below
# build the registry the way the finishing stage builds it — the real HITL
# store, the plan's own ``hitl_config.json`` binding — and read the facts
# off the disk: the row in ``plan_decisions``, the heartbeat on it, and the
# message the plan's channel received.
#
# The channel is bound to a local file rather than to a session: the
# dispatcher runs the destination and writes the payload there, so "the
# question was delivered" is a fact on disk and nothing leaves the machine.


def _plan_dir(plans_root: Path, plan_id: str = "p1") -> Path:
    """The plan's own directory — the book and the HITL binding live there."""
    directory = plans_root / plan_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_channel(plan_dir: Path, delivery_file: Path) -> None:
    """Bind the plan to a local channel, the way a plan is bound in production.

    The binding is the plan's own ``hitl_config.json`` — the file the
    existing hook dispatcher reads.  The destination is a command that
    appends the delivered payload to a file, so the delivery is a fact on
    disk.
    """
    target = shlex.quote(str(delivery_file))
    command = f"cat >> {target}; printf '\\n' >> {target}"
    (plan_dir / "hitl_config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 30,
                "hooks": [{"kind": "cmd", "command": command}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _delivered(delivery_file: Path) -> List[Dict[str, Any]]:
    """The payloads the plan's channel received."""
    if not delivery_file.exists():
        return []
    return [
        json.loads(line)
        for line in delivery_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _merge_book_item(item_id: str = "merge") -> Dict[str, Any]:
    """One entry of a requirement book: an action that needs a human."""
    item = make_item(
        id=item_id,
        action="把改动合入 main",
        acceptance_predicate={"kind": "exit_code", "equals": 0},
        external=True,
        wait={"type": "none"},
    )
    item["command"] = ["gh", "pr", "merge", "123", "--squash"]
    item["target"] = "origin"
    return item


def _write_book(plan_dir: Path, items: List[Dict[str, Any]]) -> Path:
    """Write the requirement book of one plan."""
    path = plan_dir / "postprocess.json"
    path.write_text(
        json.dumps({"items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class _FastClock:
    """A clock that runs a week per reading.

    A week is the wait's own limit, so a clock that jumps that far on
    every reading puts the deadline behind the stage immediately — the
    case for "nobody answered until it was too late" without a case that
    waits a week for it.
    """

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._readings = 0

    def now(self) -> datetime:
        """The current moment — a week further on than the last reading."""
        self._readings += 1
        return self._start + timedelta(seconds=DEFAULT_WAIT_SECONDS * self._readings)


def test_the_production_registry_writes_one_heartbeated_row(plans_root, state_db):
    """The registry production builds writes the question down and keeps it alive."""
    registry = production_registry("p1", _plan_dir(plans_root))

    record = registry.create_human_wait(human_item(), question="批准合入？")

    rows = decision_rows(state_db)

    assert len(rows) == 1, (
        "the production registry asks through the table that already "
        "exists — a second row would be a second question"
    )
    assert rows[0]["status"] == "pending"
    assert rows[0]["heartbeat_at"], (
        "a question somebody is waiting for carries a liveness stamp — "
        "without one the watchdog reads the waiting plan as a dead one"
    )
    assert record["decision_id"] == rows[0]["decision_id"]


def test_the_production_registry_delivers_through_the_plan_binding(
    plans_root, state_db, tmp_path
):
    """The question goes out through the channel the plan is bound to."""
    plan_dir = _plan_dir(plans_root)
    delivery_file = tmp_path / "delivery.jsonl"
    _write_channel(plan_dir, delivery_file)

    registry = production_registry("p1", plan_dir)
    registry.create_human_wait(human_item(), question="批准合入？")

    delivered = _delivered(delivery_file)

    assert len(delivered) == 1, (
        "the question is delivered exactly once — a second delivery is a "
        "second question asked of the same human"
    )
    assert delivered[0]["question"] == "批准合入？"
    assert delivered[0]["event"] == "hitl_decision_created", (
        "the finishing stage asks through the outlet that already exists"
    )


def test_a_plan_without_a_channel_still_asks_the_question(plans_root, state_db):
    """A plan nobody can be told about still has its question written down.

    The delivery is the plan's own business — a plan with no binding has
    nobody to ask — but the question is stored and pending either way: a
    question that exists and was never announced is a fact the audit
    mirror can show.
    """
    registry = production_registry("p1", _plan_dir(plans_root))

    record = registry.create_human_wait(human_item(), question="批准合入？")

    rows = decision_rows(state_db)

    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert record["status"] == "pending"


def test_the_orchestrator_builds_the_production_registry_when_it_has_none(
    plans_root, state_db, tmp_path, monkeypatch
):
    """The stage asks through the real machinery, with no registry injected.

    The orchestrator is built the way the verification loop builds it —
    without a registry — so the question has to find its way into the
    table on its own.  The window is shortened so the case does not sit
    on the rhythm for its full length.
    """
    monkeypatch.setattr(waits_module, "WAIT_SETTLE_SECONDS", 0.2)
    plan_dir = _plan_dir(plans_root)
    delivery_file = tmp_path / "delivery.jsonl"
    _write_channel(plan_dir, delivery_file)
    _write_book(plan_dir, [_merge_book_item()])

    frozen = freeze(load_spec(plan_dir))
    orchestrator = PostProcessOrchestrator(
        "p1",
        plan_dir=plan_dir,
        executor=CommandRunner(frozen, workspace=plan_dir),
    )
    report = orchestrator.start()

    rows = decision_rows(state_db)

    assert len(rows) == 1, (
        "the stage asked nobody — the question never reached the table "
        "that already exists"
    )
    assert rows[0]["status"] == "pending"
    assert rows[0]["heartbeat_at"], (
        "the stored question carries no heartbeat, so the watchdog reads "
        "the waiting plan as a dead one"
    )
    assert "批准" in (rows[0]["question"] or ""), (
        "the stored question is not the approval request: %r"
        % ([row["question"] for row in rows],)
    )
    assert _delivered(delivery_file), (
        "the question was never delivered through the plan's channel"
    )
    assert report["terminal_status"] is None, (
        "a stage with an item still waiting has not finished — it must "
        "not claim a terminal status"
    )
    progress = load_progress("p1")
    assert progress["items"]["merge"]["status"] == "waiting"


# ---------------------------------------------------------------------------
# The drive — the wait settles at the waiting point
# ---------------------------------------------------------------------------
#
# The wait is not a record somebody else has to come and settle: the stage
# drives it where it waits, at the existing two-second rhythm.  A human
# wait is settled by **reading** the answer that was given — the system
# never writes one — and an external wait is settled by **rechecking** the
# condition, where a recheck that raises counts as "not satisfied".


def test_the_drive_settles_a_human_wait_from_the_answer_that_was_given(
    registry, store, state_db
):
    """The wait follows the answer the human gave — read, never written."""
    record = registry.create_human_wait(human_item(), question="批准合入？")

    status, code = answer_decision(
        record["decision_id"], "proceed", answered_by="operator", store=store
    )
    assert (status, code) == ("answered", 200)

    settled = registry.drive("approve", window=0.2, poll=0.05)

    assert settled["status"] == "answered", (
        "the wait is still pending although the question was answered"
    )
    assert registry.get("approve")["status"] == "answered"
    rows = decision_rows(state_db)
    assert rows[0]["status"] == "answered"
    assert rows[0]["answer"] == '"proceed"'
    assert rows[0]["answered_by"] == "operator", (
        "the answer is the human's — the stage read it, it did not write it"
    )


def test_the_drive_never_answers_the_question_itself(registry, state_db):
    """Nobody answered, so nothing is answered: the wait goes on."""
    registry.create_human_wait(human_item(), question="批准合入？")

    settled = registry.drive("approve", window=0.2, poll=0.05)

    assert settled["status"] == "pending"
    rows = decision_rows(state_db)
    assert rows[0]["status"] == "pending"
    assert rows[0]["answer"] is None, (
        "the system answered the question on the human's behalf"
    )
    assert rows[0]["answered_by"] is None


def test_the_drive_rechecks_an_external_wait_at_the_fixed_rhythm(registry, store):
    """An external wait is rechecked — again and again, until it is satisfied."""
    calls: List[int] = []

    def check() -> bool:
        calls.append(1)
        return len(calls) >= 2

    registry.create_external_wait(external_item(), check=check)

    settled = registry.drive("ci", window=1.0, poll=0.05)

    assert settled["status"] == "answered"
    assert len(calls) >= 2, (
        "the wait was rechecked once and never again — a recheck that "
        "happens once is not a rhythm"
    )
    assert store.list_by_plan("p1") == [], (
        "an external wait asks nobody, so it writes no row"
    )


def test_a_raising_recheck_leaves_the_wait_unsatisfied(registry):
    """A recheck that raises is a recheck that did not satisfy anything."""
    registry.create_external_wait(
        external_item(),
        check=lambda: (_ for _ in ()).throw(RuntimeError("the CI page is down")),
    )

    settled = registry.drive("ci", window=0.2, poll=0.05)

    assert settled["status"] == "pending", (
        "an exception is not evidence that the condition is satisfied"
    )
    assert settled["satisfied"] is False
    assert "the CI page is down" in (settled["error"] or "")


def test_an_overdue_wait_fails_at_its_deadline(registry, clock):
    """A wait nobody answered until its deadline is a failure."""
    registry.create_human_wait(human_item(), question="批准合入？")

    clock.advance(DEFAULT_WAIT_SECONDS + 1)
    settled = registry.drive("approve", window=0.2, poll=0.05)

    assert settled["status"] == "failed", (
        "the deadline passed and the wait is still pending — a wait that "
        "ran out of time is a failure"
    )


def test_the_drive_returns_when_its_window_is_over(registry):
    """The stage waits for a bounded window, and then parks the item."""
    registry.create_human_wait(human_item(), question="批准合入？")

    started = time.monotonic()
    settled = registry.drive("approve", window=0.2, poll=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, (
        "the drive took %.1f s — it has to come back on its own" % elapsed
    )
    assert settled["status"] == "pending", (
        "the wait is still live — the stage parked it, it did not settle it"
    )


def test_the_drive_does_not_deliver_the_question_again(registry, sink):
    """The drive settles the wait — it does not ask the question again."""
    registry.create_human_wait(human_item(), question="批准合入？")

    registry.drive("approve", window=0.2, poll=0.05)

    assert sink.count == 1, (
        "the question was delivered %d times — a second delivery is a "
        "second question asked of the same human" % sink.count
    )


def test_a_question_that_was_already_asked_is_not_asked_again(
    registry, store, sink, state_db, clock
):
    """A plan that comes back to an open question finds it, not a new one.

    The finishing stage is resumed when the answer arrives, and a
    resumed stage runs the item again.  The question it finds is the one
    already asked — a second row and a second delivery would be a second
    question asked of the same human.
    """
    registry.create_human_wait(human_item(), question="批准合入？")

    # A second run of the same plan: a new registry, the same store.
    resumed = WaitRegistry("p1", clock=clock, store=store, deliver=sink)
    record = resumed.create_human_wait(human_item(), question="批准合入？")

    assert len(decision_rows(state_db)) == 1, (
        "the question was asked twice — the resumed plan wrote a second row"
    )
    assert sink.count == 1, (
        "the question was delivered twice — the resumed plan asked again"
    )
    assert record["decision_id"] == decision_rows(state_db)[0]["decision_id"], (
        "the resumed plan found the open question instead of asking a new one"
    )


def test_a_wait_that_ran_out_of_time_fails_the_item_and_notifies(
    plans_root, state_db, tmp_path, monkeypatch
):
    """A wait nobody answered until its deadline fails the item — and says so.

    The requirement book says a wait that ran out of time is a failure,
    and a failure is something a human is told about.  So the item fails,
    the stage's terminal status is ``failed`` and exactly one failure
    notification goes out.
    """
    monkeypatch.setattr(waits_module, "WAIT_SETTLE_SECONDS", 0.2)
    plan_dir = _plan_dir(plans_root)
    _write_book(plan_dir, [_merge_book_item()])

    notified: List[Any] = []

    def notify(plan_id, failed_items, evidence_path, **kwargs):
        notified.append((plan_id, list(failed_items)))
        return {}

    frozen = freeze(load_spec(plan_dir))
    orchestrator = PostProcessOrchestrator(
        "p1",
        plan_dir=plan_dir,
        executor=CommandRunner(frozen, workspace=plan_dir),
        notify=notify,
        clock=_FastClock(T0),
    )
    report = orchestrator.start()

    assert report["terminal_status"] == TERMINAL_STATUS_FAILED, (
        "a wait that ran out of time is a failure — the stage must not "
        "claim it completed: %r" % (report,)
    )
    verdicts = {item["id"]: item["verdict"] for item in report["items"]}
    assert verdicts.get("merge") == "FAIL", (
        "the item that waited and was never answered did not fail: %r" % (verdicts,)
    )
    assert notified == [("p1", ["merge"])], (
        "the failure was not notified — a failure nobody was told about is "
        "a failure nobody will fix: %r" % (notified,)
    )
    progress = load_progress("p1")
    assert progress["items"]["merge"]["status"] == "failed"


# ---------------------------------------------------------------------------
# The negative control
# ---------------------------------------------------------------------------
#
# The finishing stage borrows the existing HITL's ``auto``/``strict``
# vocabularies; it does not get to change what they mean.  Its own
# questions are ``strict`` — there is no substitute for a human — and the
# existing sweep leaves them alone; an ``auto`` question still falls back
# to its default when nobody answers it.  Both halves are pinned here, so
# a change that made the finishing stage's questions sweepable — or that
# stopped ``auto`` from defaulting — turns this red.


def test_the_auto_and_strict_expiry_semantics_are_unchanged(
    plans_root, state_db, store, tmp_path
):
    """``auto`` still defaults on a missed deadline; ``strict`` is left alone."""
    plan_dir = _plan_dir(plans_root)
    _write_channel(plan_dir, tmp_path / "delivery.jsonl")

    # The existing HITL's own question, in the mode it carries.
    auto = Decision.from_request(
        DecisionRequest(
            plan_id="p1",
            source="verification",
            task_id="auto-task",
            question="继续吗？",
            options=["yes", "no"],
            default_option="yes",
            mode=DecisionMode.AUTO.value,
            timeout_sec=60,
        ),
        now=T0,
    )
    store.insert(auto)

    # The finishing stage's own question: strict, and with no deadline of its own.
    registry = production_registry("p1", plan_dir, clock=FakeClock(T0))
    registry.create_human_wait(human_item(), question="批准合入？")

    rows = decision_rows(state_db)
    assert len(rows) == 2, (
        "the two rows are the two questions — one asked by the existing "
        "HITL, one asked by the finishing stage"
    )

    # The sweep moves the auto row and leaves the strict one alone.
    assert expire_due(now=T0 + timedelta(days=1), store=store) == 1

    rows = {row["task_id"]: row for row in decision_rows(state_db)}
    assert rows["auto-task"]["status"] == "defaulted", (
        "an auto question nobody answered still falls back to its default — "
        "the finishing stage did not change that: %r"
        % (dict(rows["auto-task"]),)
    )
    assert rows["approve"]["status"] == "pending", (
        "the finishing stage's question is strict — the existing sweep "
        "leaves it alone: %r" % (dict(rows["approve"]),)
    )
    assert rows["approve"]["answer"] is None, (
        "nobody answered it, and the sweep is not an answer"
    )
