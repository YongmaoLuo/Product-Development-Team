"""The unified waiting lifecycle — one state model for both kinds of wait.

What this file witnesses
------------------------
The finishing stage (收尾阶段) can wait for a **human** (a merge needs an
approval) or for **something outside** (a CI run on another platform).
Those two waits are different in what they wait *for* and identical in
what they need from the operator: a deadline, a way to see how much time
is left, a way to push the deadline out, and a way to give up.  The
architecture (decision point 5) therefore gives them **one** state model::

    {item_id, wait_type, question, deadline, extensions[]}

and this file pins the six behaviours that model is made of:

1. a new wait is due exactly one week after it was created;
2. the remaining time is right at the three boundaries — the whole week,
   the exact deadline, and one second past it (where it clamps to zero
   rather than going negative);
3. an extension is only legal while the wait is still **pending** — a
   wait that was answered or terminated refuses it;
4. every extension appends an audit entry and lengthens the remaining
   time;
5. a termination is final: the status changes and a late answer is
   refused;
6. a wait that outlives its deadline **fails** — unless it was
   terminated, which is why a terminated wait is never expired.

Why the clock is injected
-------------------------
A week is not a thing a test can wait for, and patching ``datetime.now``
globally leaks into every other case in the same worker — the failure
then shows up somewhere else, minutes later, in a case nobody was
looking at.  So the registry takes its clock as an argument and the
cases drive a :class:`FakeClock`: the deadline, the remaining time and
the expiry are all decided by an injected moment.

Where the deadline is kept
--------------------------
The deadline lives in the finishing stage's own progress file, under the
item's ``wait`` field (architecture decision point 5) — not in the HITL
timeout configuration, whose ``auto``/``strict`` semantics this stage
must not touch.  The cases therefore read the record back out of
``postprocess_progress.json``: a deadline that only existed in memory
would not survive the process that created it, and a week-long wait
outlives every process that might hold it.

The plans tree every case writes into is redirected with
``PDT_PLANS_DIR``, so no case touches the operator's live plans
directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from postprocess.progress import progress_path
from postprocess.waits import (
    DEFAULT_WAIT_SECONDS,
    WaitConflict,
    WaitError,
    WaitRegistry,
)
from tests.fixtures.postprocess.builders import FakeClock, make_item


#: The moment every case starts from — a fixed point, so the deadlines
#: in the assertions are written out rather than computed twice.
T0 = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)

#: The keys one wait record carries — the shared state model of
#: architecture decision point 5.
WAIT_RECORD_KEYS = {
    "item_id",
    "wait_type",
    "question",
    "deadline",
    "extensions",
    "status",
}

#: The keys one extension audit entry carries.
EXTENSION_KEYS = {"operator", "when", "from", "to"}


@pytest.fixture
def plans_root(tmp_path, monkeypatch) -> Path:
    """A private plans tree, so no case writes into the live one."""
    root = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


@pytest.fixture
def clock() -> FakeClock:
    """A clock the case moves instead of waiting for."""
    return FakeClock(T0)


@pytest.fixture
def registry(plans_root, clock) -> WaitRegistry:
    """A registry over a private plans tree and an injected clock."""
    return WaitRegistry("p1", clock=clock)


def human_item(item_id: str = "approve") -> dict:
    """A requirement item that waits for a human."""
    return make_item(id=item_id, wait={"type": "human"})


def read_progress_item(plan_id: str, item_id: str) -> dict:
    """One item's record, as it sits in the progress file on disk."""
    document = json.loads(progress_path(plan_id).read_text(encoding="utf-8"))
    return document["items"][item_id]


def test_default_deadline_is_seven_days(registry, clock):
    """A new wait is due exactly one week after it was created."""
    item = human_item("approve")

    record = registry.create_wait(item, question="批准合入？")

    created = clock.now()
    deadline = datetime.fromisoformat(record["deadline"])

    assert DEFAULT_WAIT_SECONDS == 604800, (
        "the default wait limit is one week, in seconds"
    )
    assert (deadline - created).total_seconds() == 604800
    assert record["deadline"] == (
        created + timedelta(seconds=DEFAULT_WAIT_SECONDS)
    ).isoformat()
    assert record["deadline"] == "2026-11-21T00:00:00+00:00", (
        "the deadline is written in UTC, with the offset spelled out"
    )


def test_remaining_seconds_at_boundaries(registry, clock):
    """The remaining time is right at the whole week, the deadline, and past it."""
    registry.create_wait(human_item("approve"), question="批准合入？")

    assert registry.remaining_seconds("approve") == 604800, (
        "a fresh wait has the whole week left"
    )

    clock.advance(DEFAULT_WAIT_SECONDS)

    assert registry.remaining_seconds("approve") == 0, (
        "on the deadline there is no time left"
    )

    clock.advance(1)

    assert registry.remaining_seconds("approve") == 0, (
        "past the deadline the remaining time clamps to zero — it never "
        "goes negative"
    )


def test_extend_only_pending(registry):
    """Only a pending wait can be extended — an answered or terminated one refuses."""
    registry.create_wait(human_item("approve"), question="批准合入？")
    registry.answer("approve", "批准")

    with pytest.raises(WaitConflict):
        registry.extend_deadline("approve", 3600, operator="op")

    registry.create_wait(human_item("push"), question="推送？")
    registry.terminate("push", operator="op")

    with pytest.raises(WaitConflict):
        registry.extend_deadline("push", 3600, operator="op")


def test_extend_appends_audit_entry(registry):
    """Every extension appends one audit entry and lengthens the remaining time."""
    registry.create_wait(human_item("approve"), question="批准合入？")

    first = registry.extend_deadline("approve", 3600, operator="op")
    assert len(first["extensions"]) == 1, (
        "one extension leaves exactly one audit entry"
    )

    remaining_before = registry.remaining_seconds("approve")
    second = registry.extend_deadline("approve", 3600, operator="op2")
    remaining_after = registry.remaining_seconds("approve")

    assert len(second["extensions"]) == 2, (
        "two extensions leave exactly two audit entries"
    )
    assert remaining_after > remaining_before, (
        "extending a wait can only lengthen the time left"
    )
    assert remaining_after - remaining_before == 3600

    for entry in second["extensions"]:
        assert set(entry) == EXTENSION_KEYS, (
            f"an extension audit entry carries exactly {sorted(EXTENSION_KEYS)}, "
            f"got {sorted(entry)}"
        )
        assert entry["operator"]
        assert entry["when"]

    assert [entry["operator"] for entry in second["extensions"]] == ["op", "op2"]
    assert second["extensions"][0]["from"] != second["extensions"][0]["to"], (
        "an audit entry records the deadline it moved away from and the "
        "one it moved to"
    )
    assert second["extensions"][1]["from"] == second["extensions"][0]["to"], (
        "the second extension starts where the first one ended"
    )


def test_terminate_marks_terminated(registry, clock):
    """A termination is final: the status changes and a late answer is refused."""
    registry.create_wait(human_item("approve"), question="批准合入？")

    record = registry.terminate("approve", operator="op")

    assert record["status"] == "terminated"
    assert registry.get("approve")["status"] == "terminated"

    with pytest.raises(WaitConflict):
        registry.answer("approve", "批准")

    clock.advance(DEFAULT_WAIT_SECONDS + 10)

    assert registry.expire_due() == [], (
        "a terminated wait is never expired — the operator already gave up "
        "on it"
    )
    assert registry.get("approve")["status"] == "terminated"


def test_expire_due_marks_failed(registry, clock):
    """A wait that outlives its deadline fails."""
    registry.create_wait(human_item("approve"), question="批准合入？")

    clock.advance(DEFAULT_WAIT_SECONDS + 1)

    expired = registry.expire_due()

    assert [record["item_id"] for record in expired] == ["approve"]
    assert registry.get("approve")["status"] == "failed"


def test_create_wait_record_carries_the_shared_shape(registry):
    """Both kinds of wait produce the same record — only the type differs."""
    human = registry.create_wait(human_item("approve"), question="批准合入？")
    external = registry.create_wait(
        make_item(id="ci", external=True, wait={"type": "external"}),
        question="CI 通过了吗？",
    )

    for record in (human, external):
        assert set(record) == WAIT_RECORD_KEYS, (
            f"a wait record carries exactly {sorted(WAIT_RECORD_KEYS)}, got "
            f"{sorted(record)}"
        )
        assert record["extensions"] == []
        assert record["status"] == "pending"

    assert human["wait_type"] == "human"
    assert external["wait_type"] == "external"
    assert human["question"] == "批准合入？"
    assert human["item_id"] == "approve"


def test_the_two_wait_kinds_share_one_lifecycle(registry, clock):
    """A wait for something outside is extended and expired exactly like a wait for a human."""
    registry.create_wait(
        make_item(id="ci", external=True, wait={"type": "external"}),
        question="CI 通过了吗？",
    )

    registry.extend_deadline("ci", 600, operator="op")
    assert registry.remaining_seconds("ci") == DEFAULT_WAIT_SECONDS + 600

    clock.advance(DEFAULT_WAIT_SECONDS + 601)

    expired = registry.expire_due()

    assert [record["item_id"] for record in expired] == ["ci"]
    assert registry.get("ci")["status"] == "failed"


def test_the_deadline_is_kept_in_the_progress_file(registry):
    """The deadline lives in the progress file, not only in memory."""
    registry.create_wait(human_item("approve"), question="批准合入？")
    registry.extend_deadline("approve", 3600, operator="op")

    record = read_progress_item("p1", "approve")

    assert record["status"] == "waiting", (
        "an item that waits is a waiting item"
    )
    assert record["wait"]["deadline"] == registry.get("approve")["deadline"]
    assert len(record["wait"]["extensions"]) == 1, (
        "the extension audit is persisted with the wait, not only in memory"
    )
    assert record["wait"]["extensions"][0]["operator"] == "op"


def test_an_item_may_only_wait_once(registry):
    """A second wait for the same item is a conflict, not a second record."""
    registry.create_wait(human_item("approve"), question="批准合入？")

    with pytest.raises(WaitConflict):
        registry.create_wait(human_item("approve"), question="再问一次？")

    assert len(registry.waits()) == 1


def test_an_unknown_item_is_an_error(registry):
    """Asking about a wait that was never created is an error, not zero."""
    with pytest.raises(WaitError):
        registry.remaining_seconds("never-created")

    with pytest.raises(WaitError):
        registry.extend_deadline("never-created", 3600, operator="op")


def test_an_extension_must_be_a_positive_number_of_seconds(registry):
    """A negative, fractional or non-numeric extension is refused."""
    registry.create_wait(human_item("approve"), question="批准合入？")

    for bad in (0, -1, 1.5, "3600", None):
        with pytest.raises(WaitError):
            registry.extend_deadline("approve", bad, operator="op")

    assert registry.remaining_seconds("approve") == DEFAULT_WAIT_SECONDS, (
        "a refused extension leaves the deadline where it was"
    )