"""The finishing stage's main loop: execute → judge → retry the failures once → terminal.

Background
----------
``PostProcessOrchestrator`` is the one component that drives the finishing
stage (收尾阶段).  It loads the requirement book, freezes it, runs every
item, has the **judge** decide each one from the evidence the **executor**
produced, retries the items that failed — once, and only those — and then
writes the terminal report and, when the stage failed, sends exactly one
notification.

The properties this file witnesses are the ones that are easy to get wrong
and impossible to notice afterwards:

  * ``test_all_pass_single_round`` — three items that pass end the stage
    in one round; the executor is never asked to run anything twice;
  * ``test_retry_runs_only_failed_item`` — the second round contains the
    failed item **and nothing else**: a passed item is not run again, and
    the retry scope comes from the progress file, not from the
    orchestrator's memory of what it just did;
  * ``test_second_failure_sets_postprocess_failed_and_notifies_once`` — an
    item that fails twice ends the stage at ``postprocess_failed``, the
    failure notification goes out **once**, and the report points at the
    progress file a human resumes from;
  * ``test_optional_failure_does_not_block_terminal`` — an item the book
    marks ``required: false`` may fail without failing the stage, and the
    report says so;
  * ``test_approval_required_item_becomes_waiting`` — an action the
    executor refuses to run without a human becomes a **waiting** item:
    it is neither a pass nor a fail, it is not retried, and the stage
    does not claim to be finished;
  * ``test_report_lists_every_item_in_order`` — the report lists every
    item of the book, in the order the book declared them, and lists
    each of them once.

What is real and what is injected
---------------------------------
Real: the requirement book is loaded and frozen by ``postprocess.spec``,
the verdict comes from the real ``postprocess.judge``, the progress file
and the report are written by ``postprocess.progress`` /
``postprocess.report``, and the notification is delivered by
``postprocess.notify``.

Injected: the **executor** (a script that answers per item and records
every call — so "the item was run again" is countable rather than
inferred) and the notification **sink** (a recorder that sends nothing).

Test command::

    pytest backend/tests/integration/postprocess/test_orchestrator_pipeline.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import pytest

from postprocess import progress as progress_module
from postprocess.executor import NeedsApproval
from postprocess.models import AuditRecord, EvidenceRecord
from postprocess.notify import notify_failure
from postprocess.orchestrator import PostProcessOrchestrator
from postprocess.progress import load_progress
from postprocess.report import read_report
from tests.fixtures.postprocess.builders import FakeNotifier, make_item


#: This file is an integration test — it writes real files (the progress
#: file, the report, the notification log) and reads them back.
pytestmark = pytest.mark.integration


#: The plan every case runs under.
PLAN_ID = "p1"


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated plans root — the artefacts land here, not in the repo.

    Two escape hatches are set, because two modules resolve the plans
    root: ``postprocess.progress`` honours the module attribute, and
    ``postprocess.notify`` reads the environment variable.  Pointing both
    at the same temporary tree is what keeps the case's files out of the
    operator's live plan tree.
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    monkeypatch.setattr(progress_module, "PLANS_DIR", root)
    return root


@pytest.fixture
def plan_dir(plans_root: Path) -> Path:
    """The plan's own directory — where the requirement book lives."""
    directory = plans_root / PLAN_ID
    directory.mkdir()
    return directory


def write_book(plan_dir: Path, items: Sequence[Mapping[str, Any]]) -> Path:
    """Write the requirement book of one plan.

    Args:
        plan_dir: The plan's directory.
        items: The items, in the order the book declares them.

    Returns:
        The path of the book that was written.
    """
    path = plan_dir / "postprocess.json"
    path.write_text(
        json.dumps({"items": [dict(item) for item in items]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def book_item(
    item_id: str,
    *,
    required: bool = True,
    command: Optional[Sequence[str]] = ("git", "status"),
) -> Dict[str, Any]:
    """One entry of a requirement book.

    Args:
        item_id: The entry's id.
        required: Whether the entry's failure fails the whole book.
        command: The argv the entry declares.

    Returns:
        The entry, as the requirement book spells it.
    """
    item = make_item(
        id=item_id,
        action=f"finish {item_id}",
        acceptance_predicate={"kind": "exit_code", "equals": 0},
        required=required,
    )
    item["command"] = list(command or ())
    return item


# ---------------------------------------------------------------------------
# The injected boundaries
# ---------------------------------------------------------------------------


#: What a scripted executor may answer with: an exit code, or an action
#: that needs a human before it may run.
Outcome = Union[int, NeedsApproval]


class ScriptedExecutor:
    """An executor that answers from a script and records every call.

    The script maps an item id to the outcomes of its attempts: the first
    entry is the first attempt, the second the retry, and a single entry
    repeats for every attempt.  An outcome of ``0`` is a command that
    succeeded, ``1`` one that failed, and a :class:`NeedsApproval` an
    action the executor refuses to run on its own.

    Every call is recorded, in order — which is what lets a case say
    "the second round contained exactly this item" and have it be a fact
    about the run rather than an inference from the report.
    """

    def __init__(self, script: Mapping[str, Sequence[Outcome]]) -> None:
        self._script = {key: list(value) for key, value in script.items()}
        self.calls: List[str] = []

    def run_item(self, item: Any) -> Any:
        """Run one item and answer from the script.

        Args:
            item: The requirement item.

        Returns:
            ``(EvidenceRecord, AuditRecord)``, or the
            :class:`NeedsApproval` the script names.
        """
        item_id = item["id"] if isinstance(item, Mapping) else item.id
        self.calls.append(item_id)

        outcomes = self._script.get(item_id) or []
        if not outcomes:
            outcome: Outcome = 0
        elif len(outcomes) > 1:
            outcome = outcomes.pop(0)
        else:
            outcome = outcomes[0]

        if isinstance(outcome, NeedsApproval):
            return outcome

        exit_code = int(outcome)
        return (
            EvidenceRecord(
                item_id=item_id,
                command=["fake", item_id],
                exit_code=exit_code,
                stdout="ok" if exit_code == 0 else "",
                stderr="" if exit_code == 0 else "boom",
            ),
            AuditRecord(
                action=f"finish {item_id}",
                command=["fake", item_id],
                target=item_id,
            ),
        )

    @property
    def rounds(self) -> List[List[str]]:
        """The calls, split into the rounds they belong to.

        A round ends when the orchestrator starts the next item of the
        book — so this is derived, not guessed: the orchestrator runs the
        items of a round in the book's order.
        """
        return [list(self.calls)]


class RecordingNotifier:
    """A notifier that records the call and then delivers it for real.

    The real notifier is idempotent: a second call finds its audit line
    already written and delivers nothing.  That is what the module is
    for, but it also means the *sink* alone cannot tell "the orchestrator
    notified once" from "the orchestrator notified twice and the second
    call was swallowed".  So the call is recorded here, before the real
    notifier sees it — the two counts answer two different questions:
    how many times the orchestrator notified, and how many messages were
    delivered.
    """

    def __init__(self, sink: Any) -> None:
        self._sink = sink
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        plan_id: str,
        failed_items: Sequence[str],
        evidence_path: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Record the call, then deliver it through the real notifier."""
        self.calls.append(
            {
                "plan_id": plan_id,
                "failed_items": list(failed_items),
                "evidence_path": evidence_path,
            }
        )
        kwargs.setdefault("sink", self._sink)
        return notify_failure(plan_id, failed_items, evidence_path, **kwargs)

    @property
    def count(self) -> int:
        """How many times the orchestrator notified."""
        return len(self.calls)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_all_pass_single_round(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """Three items that pass end the stage in one round."""
    write_book(plan_dir, [book_item("a"), book_item("b"), book_item("c")])
    executor = ScriptedExecutor({"a": [0], "b": [0], "c": [0]})
    sink = FakeNotifier()
    notifier = RecordingNotifier(sink)

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        sink=sink,
    )
    report = orchestrator.start()

    assert report["terminal_status"] == "completed"
    assert [item["verdict"] for item in report["items"]] == ["PASS", "PASS", "PASS"]
    assert executor.calls == ["a", "b", "c"], (
        "an item that passed must not be run a second time — the stage "
        f"ended in one round, but the executor was called {executor.calls!r}"
    )
    assert notifier.count == 0, "a stage that completed notifies nobody"
    assert sink.count == 0, "a stage that completed delivers no message"
    assert read_report(PLAN_ID, plans_dir=plans_root) == report


def test_retry_runs_only_failed_item(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """The retry round contains the failed item and nothing else."""
    write_book(plan_dir, [book_item("a"), book_item("b"), book_item("c")])
    executor = ScriptedExecutor({"a": [0], "b": [1, 0], "c": [0]})
    sink = FakeNotifier()
    notifier = RecordingNotifier(sink)

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        sink=sink,
    )
    report = orchestrator.start()

    assert report["terminal_status"] == "completed"
    assert executor.calls == ["a", "b", "c", "b"], (
        "the second round must run the failed item again and no other — "
        f"the executor was called {executor.calls!r}"
    )
    assert [item["verdict"] for item in report["items"]] == ["PASS", "PASS", "PASS"]

    progress = load_progress(PLAN_ID)
    assert progress["items"]["b"]["status"] == "passed"
    assert progress["items"]["b"]["attempts"] == 2
    assert progress["items"]["a"]["attempts"] == 1, (
        "an item that passed on the first try was through the stage once"
    )
    assert notifier.count == 0, "a stage that completed notifies nobody"
    assert sink.count == 0, "a stage that completed delivers no message"


def test_second_failure_sets_postprocess_failed_and_notifies_once(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """An item that fails twice ends the stage at ``postprocess_failed``."""
    write_book(plan_dir, [book_item("a"), book_item("b")])
    executor = ScriptedExecutor({"a": [0], "b": [1, 1]})
    sink = FakeNotifier()
    notifier = RecordingNotifier(sink)

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        sink=sink,
    )
    report = orchestrator.start()

    assert report["terminal_status"] == "postprocess_failed"
    assert executor.calls == ["a", "b", "b"], (
        "a failed item is retried exactly once — the executor was called "
        f"{executor.calls!r}"
    )
    assert [item["verdict"] for item in report["items"]] == ["PASS", "FAIL"]

    assert notifier.count == 1, (
        "the orchestrator notifies exactly once — the notifier was called "
        f"{notifier.count} times"
    )
    assert sink.count == 1, (
        "one message was delivered — the sink was called "
        f"{sink.count} times"
    )
    call = notifier.calls[0]
    assert call["plan_id"] == PLAN_ID
    assert call["failed_items"] == ["b"]
    assert sink.calls[0]["payload"]["failed_items"] == ["b"]

    failure_ref = report["failure_context_ref"]
    assert failure_ref, "a failed stage points at the file a human resumes from"
    context = Path(failure_ref)
    if not context.is_absolute():
        context = plans_root / context
    assert context.is_file(), (
        f"failure_context_ref {failure_ref!r} does not point at a file"
    )
    assert context.name == "postprocess_progress.json"
    assert read_report(PLAN_ID, plans_dir=plans_root) == report


def test_optional_failure_does_not_block_terminal(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """An item the book marks optional may fail without failing the stage."""
    write_book(
        plan_dir,
        [book_item("a"), book_item("b", required=False)],
    )
    executor = ScriptedExecutor({"a": [0], "b": [1, 1]})
    sink = FakeNotifier()
    notifier = RecordingNotifier(sink)

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        sink=sink,
    )
    report = orchestrator.start()

    assert report["terminal_status"] == "completed", (
        "an optional item that failed does not fail the whole book"
    )
    assert report["failure_context_ref"] is None
    assert [item["verdict"] for item in report["items"]] == ["PASS", "SKIPPED"], (
        "the report marks the optional item as skipped — the stage went on "
        "without it"
    )
    assert notifier.count == 0, "an optional failure is not a stage failure"
    assert sink.count == 0, "an optional failure delivers no message"

    progress = load_progress(PLAN_ID)
    assert progress["items"]["b"]["status"] == "failed", (
        "the progress file keeps the truth: the optional item did fail"
    )


def test_approval_required_item_becomes_waiting(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """An action that needs a human becomes a waiting item."""
    write_book(plan_dir, [book_item("a"), book_item("b"), book_item("c")])
    approval = NeedsApproval(
        question="May this be merged into main?",
        command=["git", "push", "origin", "main"],
    )
    executor = ScriptedExecutor({"a": [0], "b": [approval], "c": [0]})
    sink = FakeNotifier()
    notifier = RecordingNotifier(sink)

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        sink=sink,
    )
    report = orchestrator.start()

    progress = load_progress(PLAN_ID)
    assert progress["items"]["b"]["status"] == "waiting"
    assert progress["items"]["b"]["wait"], (
        "a waiting item records the wait it is in"
    )
    assert progress["items"]["b"]["attempts"] == 1

    assert executor.calls == ["a", "b", "c"], (
        "a waiting item is not retried — the executor was called "
        f"{executor.calls!r}"
    )
    assert [item["verdict"] for item in report["items"]] == [
        "PASS",
        "WAITING",
        "PASS",
    ], "a waiting item is neither a pass nor a fail"
    assert report["terminal_status"] is None, (
        "a stage with an item still waiting has not finished — it must not "
        "claim a terminal status"
    )
    assert read_report(PLAN_ID, plans_dir=plans_root) is None, (
        "the report is the terminal truth source; a stage that is still "
        "waiting has no terminal report yet"
    )
    assert notifier.count == 0
    assert sink.count == 0, "a waiting item delivers no message"


def test_report_lists_every_item_in_order(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """The report lists every item of the book, in the book's order."""
    write_book(
        plan_dir,
        [book_item("zebra"), book_item("alpha"), book_item("middle")],
    )
    executor = ScriptedExecutor({"zebra": [0], "alpha": [0], "middle": [0]})
    sink = FakeNotifier()
    notifier = RecordingNotifier(sink)

    orchestrator = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=executor,
        notify=notifier,
        sink=sink,
    )
    report = orchestrator.start()

    assert [item["id"] for item in report["items"]] == ["zebra", "alpha", "middle"], (
        "the report follows the order the requirement book declared — "
        "not the alphabetical order of the ids"
    )
    assert len(report["items"]) == 3, "no item is listed twice"
