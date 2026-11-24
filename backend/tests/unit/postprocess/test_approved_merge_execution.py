"""An approval a human gave is acted on: approve runs it, reject ends it.

What this file witnesses
------------------------
The finishing stage asks a human before it merges a pull request.  What
the human answers has to be **acted on**, and it has to be acted on
exactly once:

1. ``test_approved_command_bypasses_approval`` — a command a human
   approved **runs**.  The executor does not ask a second time: the
   approval it was given *is* the permission, and the argv is spawned;
2. ``test_without_approval_still_needs_approval`` — a command nobody
   approved is still only a question.  The executor refuses to run it
   and hands the question back;
3. ``test_reject_marks_item_failed_without_wait`` — a human who said
   "no" ends the item: it fails, it says why, and no wait is created
   for an answer that was already given;
4. ``test_wait_record_keeps_answer_private`` — the answer stays where
   it was written.  The wait record that goes to the progress file
   carries the shared state model and nothing else;
5. ``test_approved_command_runs_the_merge_and_passes_the_item`` — the
   whole point of the task: a human said yes, so the merge **ran** and
   the requirement behind it passed;
6. ``test_approved_command_failure_is_a_failure_not_a_wait`` — a merge
   that was approved and then **failed** is a failure.  It does not
   fall back to asking a second time;
7. ``test_unknown_answer_keeps_the_item_waiting`` — an answer that is
   neither "approve" nor "reject" is not an answer this stage acts on:
   the command does not run and the item keeps waiting;
8. ``test_late_approve_runs_the_merge_on_the_next_round`` — an answer
   that arrives **after** the wait's window is over is still an
   answer: the next round merges the pull request;
9. ``test_late_reject_ends_the_item_without_running_it`` — a rejection
   that arrives late ends the item, and runs nothing;
10. ``test_parked_wait_without_an_answer_is_not_rerun`` — the negative
    control: a question nobody answered is never run behind the human's
    back.

Why the answer must not land in the record
------------------------------------------
The answer is **private state**: it says what one human decided about
one question, and it is written through the existing HITL service.  The
wait record is the thing a reader of the progress file sees — a panel,
a resumed run, an auditor — and it carries the *state* of the wait, not
the answer to it.  A record that carried the answer would put a
decision into a file that is rewritten after every item, and would make
"the answer was given" indistinguishable from "the answer was read".

Why the approval is read from the decision row
-----------------------------------------------
The approval is not a flag this stage sets: it is a row in the existing
``plan_decisions`` table, written by whoever answers the question.  The
stage reads that row — by the id the registry remembers, and by the
item's own id when the registry does not remember it, because a wait
outlives the process that created it.  An answer that is neither
"approve" nor "reject" is not an answer this stage acts on: the item
keeps waiting.

Why a late answer is still an answer
-------------------------------------
The wait has a window: while it is open, the stage reads the answer
itself, and the item is left **pending** — still to be done — for the
next round.  When the window closes with no answer, the item is
**parked** as waiting, and the question lives on in the decision row.

A human who answers after that is still answering the same question.
The answer sits in the row, and the stage has to pick it up: a pull
request that was approved stays open forever otherwise, and the plan
never finishes.  So a parked item whose question has been answered is
taken back into a round — and **only** that one: a parked item nobody
answered keeps waiting, because running an action behind a human's
back is the one thing a wait forbids.

Test command::

    pytest backend/tests/unit/postprocess/test_approved_merge_execution.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest

from hitl.store import HitlStore
from postprocess import progress as progress_module
from postprocess.executor import CommandRunner, NeedsApproval
from postprocess.orchestrator import (
    REJECTED_EXCERPT,
    REJECTED_REASON,
    PostProcessOrchestrator,
)
from postprocess.progress import load_progress, mark
from postprocess.spec import freeze, load_spec
from postprocess.waits import WaitError, WaitRegistry
from tests.fixtures.postprocess.builders import FakeClock, FakeNotifier, make_item


#: The plan every case runs under.
PLAN_ID = "p1"

#: The moment every case starts from — a fixed point, so a deadline in
#: an assertion is written out rather than computed twice.
T0 = datetime(2026, 11, 20, 0, 0, 0, tzinfo=timezone.utc)

#: The command the human is asked about: a merge into ``main``, which is
#: one of the three actions the executor refuses to run on its own.
MERGE_ARGV: List[str] = ["gh", "pr", "merge", "42", "--squash"]

#: The question the human is asked about that command.
MERGE_QUESTION = "批准合入 PR #42？"

#: The keys one wait record carries — the shared state model of
#: architecture decision point 5, plus the source and the pull request
#: the wait is about.  The **answer** is not one of them.
WAIT_RECORD_KEYS = {
    "item_id",
    "wait_type",
    "question",
    "deadline",
    "extensions",
    "status",
    "source",
    "pr_number",
    "pr_url",
}


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_root(tmp_path, monkeypatch) -> Path:
    """A private plans tree, so no case writes into the live one."""
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    monkeypatch.setattr(progress_module, "PLANS_DIR", root)
    return root


@pytest.fixture
def plan_dir(plans_root) -> Path:
    """The plan's own directory — where the requirement book lives."""
    directory = plans_root / PLAN_ID
    directory.mkdir()
    return directory


@pytest.fixture
def clock() -> FakeClock:
    """A clock the case moves instead of waiting for."""
    return FakeClock(T0)


@pytest.fixture
def store(plans_root, tmp_path):
    """A HITL store on a private database."""
    opened = HitlStore(tmp_path / "state.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def registry(clock, store) -> WaitRegistry:
    """A registry bridged onto the private store."""
    return WaitRegistry(PLAN_ID, clock=clock, store=store)


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------


def merge_item(item_id: str = "merge") -> Dict[str, Any]:
    """The requirement item the human is asked about."""
    item = make_item(
        id=item_id,
        action="merge the pull request",
        acceptance_predicate={"kind": "exit_code", "equals": 0},
        external=True,
        wait={"type": "human"},
    )
    item["command"] = list(MERGE_ARGV)
    item["target"] = "o/r#42"
    return item


def write_book(plan_dir: Path, item_id: str = "merge") -> Path:
    """Write the requirement book of one plan.

    Args:
        plan_dir: The plan's directory.
        item_id: The id of the one entry the book declares.

    Returns:
        The path of the book that was written.
    """
    path = plan_dir / "postprocess.json"
    path.write_text(
        json.dumps({"items": [merge_item(item_id)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def answered_approval(
    plan_dir: Path,
    store: HitlStore,
    clock: FakeClock,
    answer: str,
) -> None:
    """The previous round asked the question, and a human answered it.

    The wait settles on the answer that was given: the item is left
    **pending** — still to be done — and the next round is the one that
    acts on the answer.

    Args:
        plan_dir: The plan's directory — where the requirement book is.
        store: The store the question is asked in.
        clock: The clock the wait measures its deadline with.
        answer: What the human answered.
    """
    asking = WaitRegistry(PLAN_ID, clock=clock, store=store)
    asking.create_approval_request(
        merge_item(),
        NeedsApproval(question=MERGE_QUESTION, command=list(MERGE_ARGV)),
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )
    decision_id = asking.decision_id_of("merge")
    assert decision_id is not None, "the question was stored, so it has an id"
    assert store.answer_if_pending(decision_id, answer) is True

    asking.drive("merge", window=0.0)
    frozen = freeze(load_spec(plan_dir))
    mark(PLAN_ID, "merge", "pending", spec_sha256=frozen.spec_sha256)
    assert load_progress(PLAN_ID)["items"]["merge"]["status"] == "pending"


def parked_approval(
    plan_dir: Path,
    store: HitlStore,
    clock: FakeClock,
) -> str:
    """The previous round asked the question, and its window closed.

    The wait is driven with a window of zero: it comes back at once, the
    question is still unanswered, and the item behind it is **parked** as
    waiting — the state a question lands in when nobody answered it in
    time.  The question itself lives on in the decision row.

    Args:
        plan_dir: The plan's directory — where the requirement book is.
        store: The store the question is asked in.
        clock: The clock the wait measures its deadline with.

    Returns:
        The id of the decision row the question was stored as.
    """
    asking = WaitRegistry(PLAN_ID, clock=clock, store=store)
    asking.create_approval_request(
        merge_item(),
        NeedsApproval(question=MERGE_QUESTION, command=list(MERGE_ARGV)),
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )
    decision_id = asking.decision_id_of("merge")
    assert decision_id is not None, "the question was stored, so it has an id"

    # The window is over and nobody answered: the wait is still pending,
    # and the item behind it is parked as waiting.
    asking.drive("merge", window=0.0)
    frozen = freeze(load_spec(plan_dir))
    mark(
        PLAN_ID,
        "merge",
        "waiting",
        attempts=1,
        wait=asking.get("merge"),
        spec_sha256=frozen.spec_sha256,
    )
    assert load_progress(PLAN_ID)["items"]["merge"]["status"] == "waiting"
    return decision_id


def late_answer(store: HitlStore, decision_id: str, answer: str) -> None:
    """A human answers a question whose window is already over.

    The answer goes into the decision row — where every answer goes.  The
    wait record the progress file carries says nothing about it.

    Args:
        store: The store the question was asked in.
        decision_id: The id of the decision row the question was stored as.
        answer: What the human answered.
    """
    assert store.answer_if_pending(decision_id, answer) is True, (
        "the late answer was accepted by the decision store"
    )


def next_round(
    plan_dir: Path,
    store: HitlStore,
    clock: FakeClock,
    tmp_path: Path,
    run: RecordingRun,
) -> PostProcessOrchestrator:
    """The next round's orchestrator — a new process over the same store.

    The registry is new too: the question was asked by an earlier
    process, so the answer is found the way a real late answer is found
    — in the decision row, not in the memory of the process that asked.

    Args:
        plan_dir: The plan's directory — where the requirement book is.
        store: The store the question was asked in.
        clock: The clock the wait measures its deadline with.
        tmp_path: The workspace the executor works in.
        run: The stand-in that records the argv it was given.

    Returns:
        The orchestrator of the next round.
    """
    return PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=CommandRunner({"items": [merge_item()]}, workspace=tmp_path, run=run),
        notify=RecordingNotifier(),
        sink=FakeNotifier(),
        waits=WaitRegistry(PLAN_ID, clock=clock, store=store),
    )


class RecordingRun:
    """A ``subprocess.run`` stand-in that records the argv it was given.

    The point of the stand-in is that "the command was run" is a fact
    about a recorded call, not an inference from the absence of an
    error.
    """

    def __init__(self, returncode: int = 0) -> None:
        self.calls: List[List[str]] = []
        self._returncode = returncode

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        """Record the argv and answer as a finished process would."""
        self.calls.append(list(argv))
        return SimpleNamespace(
            returncode=self._returncode,
            stdout="merged",
            stderr="",
        )


class RecordingExecutor:
    """An executor that records every item it was asked to run.

    It answers with the approval question the real executor would raise
    for a merge — so a case can tell "the stage never got here" from
    "the stage got here and asked again".
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def run_item(self, item: Any) -> Any:
        """Record the item and answer with the approval question."""
        item_id = item["id"] if isinstance(item, dict) else item.id
        self.calls.append(item_id)
        return NeedsApproval(question=MERGE_QUESTION, command=list(MERGE_ARGV))


class RecordingNotifier:
    """A notifier that records the call and sends nothing."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        plan_id: str,
        failed_items: Sequence[str],
        evidence_path: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Record the failure notification."""
        self.calls.append(
            {
                "plan_id": plan_id,
                "failed_items": list(failed_items),
                "evidence_path": evidence_path,
            }
        )
        return {"plan_id": plan_id, "failed_items": list(failed_items)}


# ---------------------------------------------------------------------------
# The executor: an approval is the permission
# ---------------------------------------------------------------------------


def test_approved_command_bypasses_approval(tmp_path):
    """A command a human approved runs — no second question."""
    run = RecordingRun()
    runner = CommandRunner(
        {"items": [merge_item()]},
        workspace=tmp_path,
        run=run,
    )

    outcome = runner.run_item(merge_item(), approved_command=list(MERGE_ARGV))

    assert not isinstance(outcome, NeedsApproval), (
        "a command a human approved is not a question any more — asking a "
        "second time is what kept the merge from ever happening"
    )
    evidence, audit = outcome
    assert evidence.exit_code == 0
    assert evidence.command == list(MERGE_ARGV)
    assert audit.command == list(MERGE_ARGV)
    assert run.calls == [list(MERGE_ARGV)], (
        "the approved argv is the one that ran"
    )


def test_without_approval_still_needs_approval(tmp_path):
    """A command nobody approved is still only a question."""
    run = RecordingRun()
    runner = CommandRunner(
        {"items": [merge_item()]},
        workspace=tmp_path,
        run=run,
    )

    outcome = runner.run_item(merge_item())

    assert isinstance(outcome, NeedsApproval), (
        "without an approval the merge is a question, not a command"
    )
    assert outcome.command == list(MERGE_ARGV)
    assert run.calls == [], "a command that needs approval is not spawned"


# ---------------------------------------------------------------------------
# The stage: a rejection ends the item
# ---------------------------------------------------------------------------


def test_reject_marks_item_failed_without_wait(plan_dir, registry, store, clock):
    """A human who said no ends the item — it fails, and nobody waits."""
    write_book(plan_dir)

    # The previous round asked, and the human answered "no".
    answered_approval(plan_dir, store, clock, "reject")

    # The next round: a new process, a new registry over the same store.
    executor = RecordingExecutor()
    notifier = RecordingNotifier()
    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        waits=registry,
        sink=FakeNotifier(),
    )
    report = orchestrator.start()

    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] == "failed", (
        "a rejected action fails the item behind it — the requirement was "
        "not met"
    )
    assert [entry["verdict"] for entry in report["items"]] == ["FAIL"], (
        "a rejected action is a failure of the item, and the report says so"
    )
    assert "rejected_by_user" in report["items"][0]["evidence_excerpt"], (
        "the report says **why** the item failed: a human said no"
    )
    assert executor.calls == [], (
        "a rejected command is not run — not now, not on the retry"
    )
    assert len(store.list_by_plan(PLAN_ID)) == 1, (
        "the question was asked once; a rejection is an answer, not a new "
        "question"
    )
    with pytest.raises(WaitError):
        registry.get("merge")
    assert notifier.calls, "a failure a human caused is told to the human"


# ---------------------------------------------------------------------------
# The registry: the answer stays private
# ---------------------------------------------------------------------------


def test_wait_record_keeps_answer_private(registry, store):
    """The answer is the registry's own state — the record does not carry it."""
    approval = NeedsApproval(question=MERGE_QUESTION, command=list(MERGE_ARGV))
    registry.create_approval_request(
        merge_item(),
        approval,
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )
    registry.resolve_human_wait("merge", "approve")

    record = registry.get("merge")

    assert "answer" not in record, (
        "the answer is the registry's own state — it never lands in the "
        "record that goes to the progress file"
    )
    assert set(record) == WAIT_RECORD_KEYS, (
        f"a wait record carries exactly {sorted(WAIT_RECORD_KEYS)}, got "
        f"{sorted(record)}"
    )

    # The answer was written: it is in the decision row, which is where
    # the human put it.
    decision_id = registry.decision_id_of("merge")
    assert decision_id is not None
    assert store.get(decision_id).answer == "approve"


# ---------------------------------------------------------------------------
# The boundaries
# ---------------------------------------------------------------------------


def test_approved_command_failure_is_a_failure_not_a_wait(
    plan_dir,
    registry,
    store,
    clock,
    tmp_path,
):
    """A merge that was approved and failed is a failure — not a wait."""
    write_book(plan_dir)

    # The previous round asked, and the human said yes.
    answered_approval(plan_dir, store, clock, "approve")

    run = RecordingRun(returncode=1)
    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=CommandRunner({"items": [merge_item()]}, workspace=tmp_path, run=run),
        notify=RecordingNotifier(),
        sink=FakeNotifier(),
        waits=registry,
    )
    report = orchestrator.start()

    assert run.calls == [list(MERGE_ARGV), list(MERGE_ARGV)], (
        "the approved command ran — and the retry ran it again.  A merge "
        "that failed is judged like any other failed command; it does not "
        "fall back to asking a second time"
    )
    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] == "failed", (
        "a command that was approved and failed fails the item behind it"
    )
    assert [entry["verdict"] for entry in report["items"]] == ["FAIL"]
    assert report["terminal_status"] == "postprocess_failed"
    with pytest.raises(WaitError):
        registry.get("merge"), "the item never went back to waiting"
    assert len(store.list_by_plan(PLAN_ID)) == 1, (
        "a second question was not asked: the human already answered this one"
    )


def test_approved_command_runs_the_merge_and_passes_the_item(
    plan_dir,
    registry,
    store,
    clock,
    tmp_path,
):
    """The whole point of the task: a human said yes, and the merge ran."""
    write_book(plan_dir)

    # The previous round asked, and the human said yes.
    answered_approval(plan_dir, store, clock, "approve")

    run = RecordingRun()
    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=CommandRunner({"items": [merge_item()]}, workspace=tmp_path, run=run),
        notify=RecordingNotifier(),
        sink=FakeNotifier(),
        waits=registry,
    )
    report = orchestrator.start()

    assert run.calls == [list(MERGE_ARGV)], (
        "the approved merge ran — once, and it was the declared argv"
    )
    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] == "passed", (
        "the requirement behind the merge is met, so the item passed"
    )
    assert [entry["verdict"] for entry in report["items"]] == ["PASS"]
    assert report["terminal_status"] == "completed", (
        "the stage finished: nothing is waiting any more"
    )
    assert len(store.list_by_plan(PLAN_ID)) == 1, (
        "the question was asked once — the answer did not ask it again"
    )


def test_unknown_answer_keeps_the_item_waiting(
    plan_dir,
    registry,
    store,
    clock,
    tmp_path,
):
    """An answer that is neither approve nor reject is not an answer."""
    write_book(plan_dir)

    # The previous round asked, and the human answered something this
    # stage does not act on.
    answered_approval(plan_dir, store, clock, "maybe")

    run = RecordingRun()
    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=CommandRunner({"items": [merge_item()]}, workspace=tmp_path, run=run),
        notify=RecordingNotifier(),
        sink=FakeNotifier(),
        waits=registry,
    )
    report = orchestrator.start()

    assert run.calls == [], (
        "an answer that is not an approval does not run the command"
    )
    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] != "failed", (
        "an answer that is not a rejection does not fail the item either"
    )
    assert progress["items"]["merge"]["wait"], "the item is still waiting"
    assert [entry["verdict"] for entry in report["items"]] == ["WAITING"], (
        "the item is neither passed nor failed: the stage goes on waiting "
        "for an answer it can act on"
    )
    assert report["terminal_status"] is None
    assert len(store.list_by_plan(PLAN_ID)) == 1, (
        "the same question, not a second one — nobody was asked twice"
    )


# ---------------------------------------------------------------------------
# A late answer: the question outlived its window
# ---------------------------------------------------------------------------


def test_late_approve_runs_the_merge_on_the_next_round(
    plan_dir,
    store,
    clock,
    tmp_path,
):
    """An approval that came in late still merges the pull request."""
    write_book(plan_dir)

    # The previous round asked, and its window closed with no answer.
    decision_id = parked_approval(plan_dir, store, clock)

    # The human answers now — after the window, after the item was
    # parked as waiting.
    late_answer(store, decision_id, "approve")

    run = RecordingRun()
    orchestrator = next_round(plan_dir, store, clock, tmp_path, run)
    report = orchestrator.start()

    assert run.calls == [list(MERGE_ARGV)], (
        "a late approval is a permission like any other: the declared argv "
        "ran, once"
    )
    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] == "passed", (
        "the merge happened, so the requirement behind it is met"
    )
    assert [entry["verdict"] for entry in report["items"]] == ["PASS"]
    assert report["terminal_status"] == "completed", (
        "the stage finished: nothing is waiting any more"
    )
    assert len(store.list_by_plan(PLAN_ID)) == 1, (
        "the question was asked once — the late answer did not ask it again"
    )


def test_late_reject_ends_the_item_without_running_it(
    plan_dir,
    store,
    clock,
    tmp_path,
):
    """A rejection that came in late ends the item — and runs nothing."""
    write_book(plan_dir)

    # The previous round asked, and its window closed with no answer.
    decision_id = parked_approval(plan_dir, store, clock)

    # The human says no — after the window, after the item was parked.
    late_answer(store, decision_id, "reject")

    run = RecordingRun()
    orchestrator = next_round(plan_dir, store, clock, tmp_path, run)
    report = orchestrator.resume()

    assert run.calls == [], (
        "a rejected command is not run — not now, not on the retry"
    )
    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] == "failed", (
        "a rejected action fails the item behind it — the requirement was "
        "not met"
    )
    assert [entry["verdict"] for entry in report["items"]] == ["FAIL"]
    assert report["items"][0]["evidence_excerpt"] == REJECTED_EXCERPT, (
        "the report says **why** the item failed: a human said no"
    )
    assert orchestrator._verdicts["merge"].reason == REJECTED_REASON, (
        "the stage's own sentence says the same thing"
    )
    assert report["terminal_status"] == "postprocess_failed"


def test_parked_wait_without_an_answer_is_not_rerun(
    plan_dir,
    store,
    clock,
    tmp_path,
):
    """A question nobody answered is never run behind the human's back."""
    write_book(plan_dir)

    # The previous round asked, and its window closed with no answer.
    parked_approval(plan_dir, store, clock)

    run = RecordingRun()
    orchestrator = next_round(plan_dir, store, clock, tmp_path, run)
    report = orchestrator.start()

    assert run.calls == [], (
        "a question nobody answered is not a permission: the merge did not run"
    )
    progress = load_progress(PLAN_ID)
    assert progress["items"]["merge"]["status"] == "waiting", (
        "the item is still waiting — for an answer, not for a command"
    )
    assert [entry["verdict"] for entry in report["items"]] == ["WAITING"], (
        "the item is neither passed nor failed: the stage goes on waiting "
        "for an answer it can act on"
    )
    assert report["terminal_status"] is None, (
        "the stage is suspended, not finished"
    )
