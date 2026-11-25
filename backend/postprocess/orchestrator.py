"""The finishing stage's main loop — execute, judge, retry the failures once, terminal.

What this module is
-------------------
The one component that drives the finishing stage (收尾阶段).  It does not
execute anything itself, does not decide anything itself, and does not
write the requirement book: it **sequences** the modules that do, in the
order the architecture fixed:

    freeze the book → execute each item → let the judge decide →
    retry the items that failed (once, and only those) → terminal

The retry scope is read from the **progress file**, not from the
orchestrator's memory of what it just did.  That is the whole point of
the split: the file is what survives a crash, so a resumed run repeats
exactly what the file says failed — and an item that passed is never run
again even if the process died right after it was judged.

Why the orchestrator is allowed to see everything
-------------------------------------------------
The dependency direction is one way: ``orchestrator → (spec, executor,
judge, waits, progress, report)``, and the judge and the executor do not
depend on each other.  The orchestrator is the component that has to see
all of them — it is the seam — so it is also the component a boundary
gate has to watch.  It runs no command (the executor is the only entry
that does), it decides nothing (the judge is the only component that
says PASS or FAIL), and it reaches no channel of its own (the failure
notification goes out through the sink the plan already has).

What the loop guarantees
------------------------
* **One round when nothing fails.**  Every item is executed once and
  judged once.
* **Exactly one retry, and only of the failures.**  The second round
  picks up the items whose progress status is ``failed`` — nothing else.
  An item that passed is not in that set, and an item that is *waiting*
  is not in it either: a wait is not a failure, and a failure that is
  waiting for a human must not be retried behind the human's back.
* **A waiting item is neither a pass nor a fail.**  The executor refused
  to run an action without a human, so the item becomes ``waiting``: the
  progress file records the wait, the item is not judged, and the stage
  does not claim to be finished.  A stage with an item still waiting has
  **no terminal report** — the report is the terminal truth source, and
  the plan is suspended, not finished.
* **An optional item may fail.**  The book says which items are
  ``required``.  An optional item that does not pass is reported as
  ``SKIPPED`` — the verdict the report module reserves for "an optional
  item that was not required to run" — and the stage still completes.
  The progress file keeps the truth: the item's status is ``failed``.
* **One notification.**  A stage that ends at ``postprocess_failed``
  sends exactly one failure notification, naming the plan, the items
  that failed and the file a human resumes from.
* **The product code is never touched.**  Every item runs inside the
  product-code guard, which reads the working tree before and after
  the command.  A product code that was touched fails that item — its
  own criterion may have passed, a command that exits 0 can still have
  rewritten the code — stops the stage there, and sends the one
  failure notification.  The items after it are not run, and the
  offending action is not repeated in the retry round: A5 says the
  finishing stage changes no product code, so a stage that found one
  changed stops and says so.  It does not repair anything and it does
  not roll anything back — undoing a product-code change is a
  destructive act, and a human approves that in person.

Where the artefacts land
------------------------
``plan_dir`` is the plan's own directory, where the requirement book
(``postprocess.json``) lives.  The two artefacts the stage writes — the
progress file and the terminal report — land **side by side** under the
plans root the progress module resolves, so the report's
``failure_context_ref`` always points at a file that sits next to it.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from postprocess.executor import (
    NeedsApproval,
    ProductCodeModified,
    product_diff_guard,
    product_guard_root,
)
from postprocess.judge import judge
from postprocess.models import AuditRecord, EvidenceRecord, Verdict
from postprocess.notify import notify_failure
from postprocess.progress import (
    failed_item_ids,
    load_progress,
    mark,
    progress_path,
)
from postprocess.report import (
    AUDIT_KEYS,
    TERMINAL_STATUS_COMPLETED,
    TERMINAL_STATUS_FAILED,
    read_report,
    write_report,
)
from postprocess.pr_ref import extract_pr_ref
from postprocess.spec import SpecItem, freeze, load_spec
from postprocess.waits import (
    ITEM_STATUS_BY_WAIT_STATUS,
    POSTPROCESS_APPROVAL_SOURCE,
    WaitError,
    WaitRegistry,
    production_registry,
)


logger = logging.getLogger(__name__)


#: The repository root — ``backend/postprocess/orchestrator.py`` lives two
#: levels below it.  A reference into the plans tree is written relative
#: to it when the plans tree lives inside the repository, so the report
#: carries ``plans/<id>/postprocess_progress.json`` rather than a path
#: that only means something on the machine that wrote it.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: The verdict of an item the judge passed.
VERDICT_PASS: str = "PASS"

#: The verdict of an item that did not pass and was required to.
VERDICT_FAIL: str = "FAIL"

#: The verdict of an item that did not pass and was **not** required to.
#: It is the report module's own word for "an optional item that was not
#: required to run" — the one verdict besides ``PASS`` that does not
#: mean the stage failed.
VERDICT_SKIPPED: str = "SKIPPED"

#: The verdict of an item that is waiting for a human.
VERDICT_WAITING: str = "WAITING"

#: The answer a human gives when the action may run.  It is one of the
#: two answers an approval request offers (:data:`postprocess.waits.
#: APPROVAL_OPTIONS`).
APPROVE_ANSWER: str = "approve"

#: The answer a human gives when the action may not run.
REJECT_ANSWER: str = "reject"

#: Why an item whose approval was rejected failed.  It is a marker, not
#: a sentence: the report carries it so a reader can tell a failure a
#: human decided from a failure a command had.
REJECTED_REASON: str = "rejected_by_user"

#: What the report says about an item a human rejected.  It names the
#: marker, so the report and the verdict say the same thing.
REJECTED_EXCERPT: str = (
    "rejected_by_user: the approval was rejected by the user, so the "
    "command was not run"
)

#: The progress statuses that mean "this item is not to be executed
#: again in the first round": it is done, or it is waiting.
SETTLED_STATUSES: frozenset[str] = frozenset({"passed", "waiting"})

#: The criterion A5 puts on an item that modified the product code: the
#: product code must be untouched.  It is the judge's own vocabulary —
#: the verdict is still the judge's, this is only the question asked.
PRODUCT_CODE_UNTOUCHED: Dict[str, Any] = {"kind": "git_diff_empty"}


class _class_or_instance:
    """A method that answers to the class and to an instance alike.

    ``PostProcessOrchestrator.resume(plan_id, plan_dir, **kwargs)`` and
    ``orchestrator.resume()`` are two spellings of one question — "go on
    with this plan" — and whoever holds one of the two should not have to
    build the other.  A plain method would answer to the instance only,
    and a classmethod cannot see the plan an instance is bound to.

    The class spelling builds the orchestrator and delegates to the
    instance spelling, so both end in the same place: one implementation,
    one behaviour, no second code path to keep in step.

    Args:
        function: The function the descriptor stands for.
    """

    def __init__(self, function: Any) -> None:
        self._function = function
        functools.update_wrapper(self, function)

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        """The bound method, or the class-level entry point.

        Args:
            instance: The instance the attribute was reached through, or
                ``None`` when it was reached through the class.
            owner: The class the attribute belongs to.

        Returns:
            The instance's own bound method, or a function that builds the
            orchestrator and continues its plan.
        """
        if instance is not None:
            return self._function.__get__(instance, owner)

        def entry(plan_id: str, plan_dir: Any, **kwargs: Any) -> Any:
            orchestrator = owner(plan_id, plan_dir=plan_dir, **kwargs)
            return orchestrator.resume()

        functools.update_wrapper(entry, self._function)
        return entry


def _accepts_keyword(function: Any, name: str) -> bool:
    """Whether ``function`` accepts the keyword argument ``name``.

    The judge takes an optional ``root`` and the notifier an optional
    ``sink``; a test double usually takes neither.  Asking the callable
    once, here, is cheaper than calling it and catching the ``TypeError``
    — and a ``TypeError`` raised *inside* the callable would be swallowed
    by such a catch, which is the worst possible way to find out that a
    call went wrong.

    Args:
        function: The callable.
        name: The keyword argument's name.

    Returns:
        Whether the callable accepts it, directly or through
        ``**kwargs``.
    """
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class PostProcessOrchestrator:
    """The finishing stage's main loop.

    Args:
        plan_id: The plan the stage runs for.
        plan_dir: The plan's own directory — where the requirement book
            (``postprocess.json``) lives.
        executor: The command runner.  Anything with a ``run_item`` (or
            an ``execute``) method that answers with an evidence record
            and an audit triple, or with a
            :class:`~postprocess.executor.NeedsApproval`.
        judge: The judge.  It takes ``(item, evidence_records)`` and
            answers with a verdict.  The real judge is the default; a
            test injects its own.
        notify: The failure notifier.  It takes
            ``(plan_id, failed_items, evidence_path)`` and — when it
            accepts a ``sink`` keyword — the orchestrator's own sink with
            it.  The real notifier is the default.
        clock: The clock the wait registry reads.  Anything with a
            ``now()`` that returns a ``datetime``.
        sink: The channel the failure notification is delivered through.
            It is the sink the plan is already bound to; the package
            grows no channel of its own.
        waits: The registry the waiting items are registered with.  When
            none is given, one is created for this plan — and without a
            HITL store behind it, a question cannot be stored, so the
            wait is recorded in the progress file instead.
    """

    def __init__(
        self,
        plan_id: str,
        *,
        plan_dir: Any,
        executor: Any,
        judge: Any = judge,
        notify: Any = notify_failure,
        clock: Any = None,
        sink: Any = None,
        waits: Optional[WaitRegistry] = None,
    ) -> None:
        self._plan_id = str(plan_id)
        self._plan_dir = Path(plan_dir)
        self._executor = executor
        self._judge = judge
        self._notify = notify
        self._clock = clock
        self._sink = sink
        self._waits = waits

        self._frozen: Any = None
        self._evidence: Dict[str, List[EvidenceRecord]] = {}
        self._audits: Dict[str, Dict[str, Any]] = {}
        self._verdicts: Dict[str, Verdict] = {}
        self._settled: Dict[str, str] = {}
        self._recorded: Dict[str, str] = {}
        self._waiting: Dict[str, Dict[str, Any]] = {}
        self._carried: Dict[str, Dict[str, Any]] = {}
        # The item that modified the product code, and the fact that the
        # stage stopped because of it.  A violation ends the round: the
        # items after it are not run, and the retry round does not repeat
        # the offending action behind a human's back.
        self._violation: Optional[Dict[str, Any]] = None
        self._halted: bool = False

    # ------------------------------------------------------------------
    # The public surface
    # ------------------------------------------------------------------

    def start(self) -> Dict[str, Any]:
        """Run the stage and return what it decided.

        Returns:
            The terminal report, when the stage reached a terminal state:
            ``spec_sha256``, ``terminal_status``, ``items`` and
            ``failure_context_ref``.  When an item is still waiting, the
            stage is suspended rather than finished, so the same four
            keys are returned with ``terminal_status`` of ``None`` and no
            report is written — the report is the terminal truth source,
            and this stage has not reached one.
        """
        self._frozen = freeze(load_spec(self._plan_dir))
        self._evidence = {}
        self._audits = {}
        self._verdicts = {}
        self._waiting = {}
        self._carried = {}
        self._violation = None
        self._halted = False

        items = list(self._frozen.spec.items)

        # What the progress file already holds.  It is read once: the
        # file is the state this run continues from, and reading it
        # twice could read two different states.
        self._recorded = self._recorded_statuses()

        # What an earlier run settled: its verdict is the progress
        # file's, and this run does not repeat the work.
        self._settled = self._settled_verdicts(items)

        # The first round runs what has not been settled yet.  An item
        # the progress file already records as failed is left to the
        # retry round: this run gives it one more attempt, not two.
        self._run_round(self._first_round_scope(items), attempt=1)

        # The retry round takes its scope from the progress file — the
        # file that survives a crash — and not from what this process
        # remembers doing.  A stage that stopped on a product-code
        # violation does not get there: repeating the offending action
        # behind a human's back is exactly what A5 forbids.
        if not self._halted:
            self._run_round(self._retry_scope(items), attempt=2)

        return self._finish(items)

    @_class_or_instance
    def resume(self) -> Dict[str, Any]:
        """Continue the finishing stage from what survived the crash.

        The finishing stage may be killed at any moment — the commands it
        runs are outward-facing ones, and those get killed.  What the next
        process may do is decided by **the files on disk**, never by what
        the killed process remembers: the progress file says which items
        are finished and which are not, and the terminal report says what
        the last run concluded.

        So a resumed run repeats exactly the unfinished items:

        * an item the progress file records as ``passed`` is **not** run
          again — its outward action already happened, and running it again
          would repeat that action;
        * an item recorded as ``running`` was killed mid-flight and never
          judged, so it is run again from the beginning;
        * an item recorded as ``failed`` is continued — the retry round
          picks it up, which is the one place a failure is allowed to be
          repeated;
        * an item the **report** records as passed is not run again either,
          even when the progress file is missing: the report is the
          terminal truth, and a plan whose progress was lost must not pay
          for it by repeating an action that already succeeded.

        A damaged progress file raises
        :class:`~postprocess.progress.ProgressError` rather than being read
        as an empty progress: an empty progress means "nothing to repeat",
        and mistaking damage for emptiness would re-run the whole book —
        outward actions included.

        Args:
            plan_id: The plan to continue.  Only the class-level spelling
                takes one; the instance-level spelling continues the plan
                the instance is bound to.
            plan_dir: The plan's own directory.  Only the class-level
                spelling takes one.
            **kwargs: The rest of the orchestrator's arguments — the
                executor above all.  Only the class-level spelling takes
                them.

        Returns:
            The terminal report of the resumed run — the same four keys
            :meth:`start` returns.  When the previous report is already
            this plan's terminal answer and nothing is left unfinished,
            that report is returned unchanged: the stage had already
            finished, and finishing it again would repeat what it did.
        """
        self._frozen = freeze(load_spec(self._plan_dir))
        self._evidence = {}
        self._audits = {}
        self._verdicts = {}
        self._waiting = {}
        self._carried = {}
        self._violation = None
        self._halted = False

        items = list(self._frozen.spec.items)

        # The progress file is read first, and its damage is not swallowed.
        # A file that cannot be read is reported, never read as "no
        # progress yet" — that reading would re-run the whole book.
        progress = load_progress(self._plan_id)

        report = read_report(self._plan_id, plans_dir=self._plans_dir())

        # What the two files together say about each item.
        self._recorded = self._reconcile(progress, report)
        self._carried = self._carried_entries(report)
        self._settled = self._settled_verdicts(items)

        # The stage had already finished, and nothing is left to continue:
        # the report that says so stays as it is.
        if self._report_is_final(report) and self._all_settled(items):
            return dict(report)

        # The report's word goes into the progress file, so that the retry
        # scope — a query over the file — sees the failures the file never
        # heard of.
        self._repair_progress(items, progress)

        # The first round runs what is not settled yet; the retry round
        # takes its scope from the progress file — the file that survives
        # a crash — and not from what this process remembers doing.  A
        # stage that stopped on a product-code violation does not get
        # to the retry round: the offending action is not repeated
        # behind a human's back.
        self._run_round(self._first_round_scope(items), attempt=1)
        if not self._halted:
            self._run_round(self._retry_scope(items), attempt=2)

        return self._finish(items)

    def status(self) -> Dict[str, Any]:
        """The stage's state, read from the artefacts on disk.

        Returns:
            The terminal report, when the plan has one; otherwise what
            the progress file says: the fingerprint, a terminal status of
            ``None``, the per-item state and no failure context.
        """
        report = read_report(self._plan_id, plans_dir=self._plans_dir())
        if report is not None:
            return dict(report)

        progress = load_progress(self._plan_id)
        recorded = progress.get("items") or {}
        return {
            "spec_sha256": progress.get("spec_sha256", ""),
            "terminal_status": None,
            "items": [
                {
                    "id": item_id,
                    "status": record.get("status"),
                    "attempts": record.get("attempts"),
                }
                for item_id, record in recorded.items()
            ],
            "failure_context_ref": None,
        }

    # ------------------------------------------------------------------
    # The two rounds
    # ------------------------------------------------------------------

    def _first_round_scope(self, items: Sequence[SpecItem]) -> List[SpecItem]:
        """The items the first round runs.

        An item the progress file already settles — passed, waiting, or
        failed by an earlier run — is not run again in this round.  The
        failed ones are picked up by the retry round, which is the one
        place a failure is allowed to be repeated.

        A **waiting** item is settled too, with one exception: a wait a
        human has since answered is not a question any more.  The answer
        lives in the decision row, and the row outlives the wait's
        window — so an answer that came in late is still an answer, and
        the item is taken back into this round to be acted on.  Without
        that, an approved pull request stays open for ever and the plan
        never finishes.

        Only the two answers this stage acts on count: "approve" runs the
        declared command, "reject" ends the item.  A wait nobody
        answered — or one answered with something else — keeps waiting:
        running an action behind a human's back is exactly what a wait
        forbids.
        """
        scope = []
        for item in items:
            status = self._recorded.get(item.id, "")
            if status == "waiting":
                if self._answered_approval(item):
                    scope.append(item)
                continue
            if status in SETTLED_STATUSES or status == "failed":
                continue
            scope.append(item)
        return scope

    def _answered_approval(self, item: SpecItem) -> bool:
        """Whether a human answered this item's approval question.

        Only the two answers this stage acts on count: "approve" runs the
        declared command, "reject" ends the item.  Any other answer — and
        no answer at all — leaves the item waiting.

        Args:
            item: The item the approval is about.

        Returns:
            ``True`` when the answer that was given is one of the two.
        """
        return self._approval_answer_for(item) in (APPROVE_ANSWER, REJECT_ANSWER)

    def _retry_scope(self, items: Sequence[SpecItem]) -> List[SpecItem]:
        """The items the retry round runs: the ones the progress file failed.

        The scope comes from :func:`postprocess.progress.failed_item_ids`
        — from the file, not from this process's memory.  An item that is
        waiting is not in it: a wait is not a failure, and repeating the
        action behind the human's back is exactly what a wait forbids.
        """
        failed = set(failed_item_ids(load_progress(self._plan_id)))
        return [item for item in items if item.id in failed]

    def _run_round(self, scope: Sequence[SpecItem], *, attempt: int) -> None:
        """Run one round over the items of its scope, in the book's order.

        The round stops at the first item that modified the product
        code: the items after it are not run at all.  A stage that
        stopped because of a violation does not go on to act outward
        while the product code is in an unknown state.
        """
        for item in scope:
            if self._halted:
                break
            self._run_item(item, attempt=attempt)

    # ------------------------------------------------------------------
    # What survived the crash
    # ------------------------------------------------------------------

    def _reconcile(
        self,
        progress: Mapping[str, Any],
        report: Optional[Mapping[str, Any]],
    ) -> Dict[str, str]:
        """The per-item status the resumed run continues from.

        Two files speak about the items.  The **progress file** is the live
        record — it is rewritten after every item — so where it speaks, it
        wins.  The **terminal report** is written once, at the end of a
        run, and it fills in what the progress file does not say.

        One verdict is never overturned: an item that either file records
        as passed stays passed.  A passed item has already done its
        outward action, and running it again would repeat that action —
        which is the one thing a resumed run must never do.
        """
        recorded = self._progress_statuses(progress)
        for item_id, verdict in self._report_verdicts(report).items():
            status = recorded.get(item_id, "")
            if status == "passed":
                continue
            if verdict == VERDICT_PASS:
                recorded[item_id] = "passed"
                continue
            if status:
                # The progress file speaks about this item — it is the
                # newer of the two, and the report does not overturn it.
                continue
            if verdict in (VERDICT_FAIL, VERDICT_SKIPPED):
                recorded[item_id] = "failed"
            elif verdict == VERDICT_WAITING:
                recorded[item_id] = "waiting"
        return recorded

    def _progress_statuses(
        self,
        progress: Mapping[str, Any],
    ) -> Dict[str, str]:
        """The status the progress file records for each item.

        A progress file written for **another** requirement book is
        ignored: its verdicts were judged against criteria that are no
        longer the ones this round is bound to, so they say nothing about
        this round.  A record without a status says nothing either, and is
        left out rather than counted as a status nobody can act on.
        """
        recorded_sha = str(progress.get("spec_sha256") or "")
        if recorded_sha and recorded_sha != self._frozen.spec_sha256:
            return {}

        recorded = progress.get("items") or {}
        statuses: Dict[str, str] = {}
        for item_id, record in recorded.items():
            if not isinstance(record, Mapping):
                continue
            status = str(record.get("status") or "")
            if status:
                statuses[item_id] = status
        return statuses

    def _report_verdicts(
        self,
        report: Optional[Mapping[str, Any]],
    ) -> Dict[str, str]:
        """The verdict the terminal report records for each item.

        A report written for another requirement book is ignored, for the
        same reason the progress file's other-book entries are.
        """
        if not report:
            return {}
        if str(report.get("spec_sha256") or "") != self._frozen.spec_sha256:
            return {}

        verdicts: Dict[str, str] = {}
        for entry in report.get("items") or []:
            if not isinstance(entry, Mapping):
                continue
            item_id = str(entry.get("id") or "")
            if item_id:
                verdicts[item_id] = str(entry.get("verdict") or "")
        return verdicts

    def _carried_entries(
        self,
        report: Optional[Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """The earlier report's own entries, for the items this run does not redo.

        An item an earlier run settled is not run again, so this run has
        no fresh evidence to show for it.  The earlier report's entry —
        its evidence excerpt and its audit triple included — is what the
        new report carries for that item: the work was not repeated, and
        the evidence of the run that did it is still the evidence.
        """
        if not report:
            return {}
        if str(report.get("spec_sha256") or "") != self._frozen.spec_sha256:
            return {}

        carried: Dict[str, Dict[str, Any]] = {}
        for entry in report.get("items") or []:
            if not isinstance(entry, Mapping):
                continue
            item_id = str(entry.get("id") or "")
            if item_id:
                carried[item_id] = dict(entry)
        return carried

    def _carried_entry(
        self,
        item: SpecItem,
        carried: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """One earlier run's report entry, carried into this run's report."""
        audit = carried.get("audit")
        if not isinstance(audit, Mapping) or set(audit) != set(AUDIT_KEYS):
            audit = _empty_audit(item)
        command = audit.get("command")
        if not isinstance(command, (list, tuple)):
            command = []

        return {
            "id": item.id,
            "verdict": self._verdict_of(item),
            "evidence_excerpt": str(carried.get("evidence_excerpt") or ""),
            "audit": {
                "action": str(audit.get("action") or ""),
                "command": [str(argument) for argument in command],
                "target": str(audit.get("target") or ""),
            },
        }

    def _report_is_final(
        self,
        report: Optional[Mapping[str, Any]],
    ) -> bool:
        """Whether the report is this plan's terminal answer for this book."""
        if not report:
            return False
        if report.get("terminal_status") not in (
            TERMINAL_STATUS_COMPLETED,
            TERMINAL_STATUS_FAILED,
        ):
            return False
        return str(report.get("spec_sha256") or "") == self._frozen.spec_sha256

    def _all_settled(self, items: Sequence[SpecItem]) -> bool:
        """Whether every item of the book is settled — none of them owes work.

        A failed item is **not** settled: it is the one item that still
        owes a retry.
        """
        return all(
            self._recorded.get(item.id, "") in SETTLED_STATUSES
            for item in items
        )

    def _repair_progress(
        self,
        items: Sequence[SpecItem],
        progress: Mapping[str, Any],
    ) -> None:
        """Write the report's knowledge into the progress file.

        The retry scope is a query over the progress file — that is the
        whole point of the file.  So an item the report says failed, but
        the progress file never heard of, would be skipped by the retry
        round and the failure would never be continued.  The report's word
        goes into the file, and the file stays the single source of truth.

        The attempt count the repair records is the one the report
        implies — an item the report knows about went through the stage at
        least once — and not a number nobody can vouch for.

        Args:
            items: The items of the requirement book.
            progress: The progress document as it was read for this run.
                It is the document this run continues from, and it is read
                once: reading it twice could read two different states.
        """
        recorded = progress.get("items") or {}
        for item in items:
            existing = recorded.get(item.id)
            existing_status = (
                str(existing.get("status") or "")
                if isinstance(existing, Mapping)
                else ""
            )
            if existing_status:
                continue
            status = self._recorded.get(item.id, "")
            if status == "passed":
                mark(
                    self._plan_id,
                    item.id,
                    "passed",
                    attempts=1,
                    spec_sha256=self._frozen.spec_sha256,
                )
            elif status == "failed":
                mark(
                    self._plan_id,
                    item.id,
                    "failed",
                    attempts=1,
                    spec_sha256=self._frozen.spec_sha256,
                )

    # ------------------------------------------------------------------
    # One item
    # ------------------------------------------------------------------

    def _run_item(self, item: SpecItem, *, attempt: int) -> None:
        """Execute one item, judge it, and record what became of it."""
        if self._rejected_answer_for(item):
            # A human said no: the action is not run, the item fails,
            # and no wait is created for an answer that was already
            # given.
            self._record_rejection(item, attempt=attempt)
            return

        # The command a human approved, if they approved one.  It is read
        # before the guard goes up: reading an answer changes nothing in
        # the working tree.
        approved_command = self._approved_command_for(item)

        # The in-flight mark states no attempt count: the counter counts
        # the times an item was **through** the stage, and the item is
        # not through it until it has been judged.
        mark(
            self._plan_id,
            item.id,
            "running",
            spec_sha256=self._frozen.spec_sha256,
        )

        # The product-code guard goes around the execution: it reads the
        # working tree before and after, and a product code that was
        # touched raises.  The outcome is kept even when the guard
        # raises, so the evidence of the command that did it is not
        # lost with the exception.
        outcome: Any = None
        try:
            with self._product_guard(item):
                outcome = self._execute(item, approved_command=approved_command)
        except ProductCodeModified as violation:
            self._record_violation(item, violation, outcome, attempt=attempt)
            self._halted = True
            return

        if isinstance(outcome, NeedsApproval):
            self._wait_for(item, outcome, attempt=attempt)
            return

        evidence, audit = outcome
        # The diff observed around the command is the evidence of the
        # product code being untouched — an empty one says so, and a
        # criterion that asks for it (``git_diff_empty``) can be
        # decided from the record rather than from a promise.
        evidence.git_diff = ""
        self._evidence.setdefault(item.id, []).append(evidence)
        self._audits[item.id] = _audit_triple(audit, item)

        verdict = self._judge_item(item)
        self._verdicts[item.id] = verdict
        mark(
            self._plan_id,
            item.id,
            "passed" if verdict.passed else "failed",
            attempts=attempt,
            spec_sha256=self._frozen.spec_sha256,
        )

    # ------------------------------------------------------------------
    # The answer a human gave
    # ------------------------------------------------------------------

    def _approved_command_for(self, item: SpecItem) -> Optional[List[str]]:
        """The command a human approved for this item, if they did.

        An approval is a **permission**, not another command: the argv
        that runs is the one the requirement book declares, and it runs
        only because a human said yes.  Without that answer the command
        is still a question, and the item keeps waiting.

        Args:
            item: The item the approval is about.

        Returns:
            The argv to run, or ``None`` when nobody approved this item
            — which is the case for every item that was never asked
            about, and for every answer that is not an approval.
        """
        if self._approval_answer_for(item) != APPROVE_ANSWER:
            return None
        command = item.command
        if not command:
            return None
        return list(command)

    def _rejected_answer_for(self, item: SpecItem) -> bool:
        """Whether a human rejected this item's approval.

        Args:
            item: The item the approval is about.

        Returns:
            ``True`` when the answer that was given is a rejection.
        """
        return self._approval_answer_for(item) == REJECT_ANSWER

    def _approval_answer_for(self, item: SpecItem) -> Optional[str]:
        """The answer a human gave for this item's approval request.

        Read-only, and never a guess.  The answer lives in the decision
        row the human answered: the registry remembers which row that
        is, and a registry that does not remember it — a run that
        outlived the one that asked — looks the row up by the item's own
        id.

        An answer that is neither "approve" nor "reject" is not an
        answer this stage acts on: the item keeps waiting, exactly as
        it did before.

        Args:
            item: The item the approval is about.

        Returns:
            The answer that was given, or ``None`` when nobody answered.
        """
        registry = self._registry()
        decision_id = registry.decision_id_of(item.id)
        if not decision_id:
            return None

        decision = registry.decision_by_id(decision_id)
        if decision is None:
            return None
        if str(getattr(decision, "source", "")) != POSTPROCESS_APPROVAL_SOURCE:
            # Not an approval question.  An ordinary human question has
            # its own answers ("proceed" / "stop"), and they say nothing
            # about running a command.
            return None

        answer = getattr(decision, "answer", None)
        if answer is None:
            return None
        return str(answer)

    def _record_rejection(self, item: SpecItem, *, attempt: int) -> None:
        """Record an item a human rejected.

        The command is not run, and no wait is created: a "no" is an
        answer, not a question.  The item fails — the requirement was
        not met — and the report says *why*, so a reader knows the
        failure was a decision and not a broken command.

        Args:
            item: The item that was rejected.
            attempt: The attempt the item was on.
        """
        self._verdicts[item.id] = Verdict(
            item_id=item.id,
            passed=False,
            evidence_excerpt=REJECTED_EXCERPT,
            reason=REJECTED_REASON,
        )
        self._audits[item.id] = _empty_audit(item)
        mark(
            self._plan_id,
            item.id,
            "failed",
            attempts=attempt,
            spec_sha256=self._frozen.spec_sha256,
        )

    def _product_guard(self, item: SpecItem) -> Any:
        """The guard around one item's execution.

        The guard watches the repository the executor works in.  An
        executor that has no repository — a test's stub, or a plan with
        no project — has no product code to protect, so there is nothing
        to guard and the item runs unguarded.  The guard is never
        switched off by a flag: it is off only where there is nothing it
        could watch.
        """
        root = self._guard_root()
        if root is None:
            return contextlib.nullcontext()
        return product_diff_guard(item, repo=root, plan_id=self._plan_id)

    def _guard_root(self) -> Optional[Path]:
        """The repository the product-code guard watches.

        The executor's workspace is the repository the plan's commands
        act in, so that is what the guard reads.  ``None`` when the
        executor has no workspace, or when the workspace is not inside a
        git repository at all.
        """
        workspace = getattr(self._executor, "workspace", None)
        if workspace is None:
            return None
        return product_guard_root(workspace)

    def _record_violation(
        self,
        item: SpecItem,
        violation: ProductCodeModified,
        outcome: Any,
        *,
        attempt: int,
    ) -> None:
        """Record an item that modified the product code.

        The item **fails**: its own criterion may well have passed — a
        command that exits 0 can still have rewritten the product code —
        but A5 says such an item is a failure and the stage stops there.
        The evidence is the diff itself, in the raw text of the two
        read-only git commands, so a reader can check the claim against
        the repository instead of trusting this summary of it.

        Nothing is repaired and nothing is rolled back: undoing a
        product-code change is a destructive act, and a human has to
        approve it in person.

        Args:
            item: The item that modified the product code.
            violation: What the guard raised.
            outcome: What the executor answered, when it got that far.
            attempt: The attempt the item was on.
        """
        original: Optional[EvidenceRecord] = None
        audit: Any = None
        if outcome is not None and not isinstance(outcome, NeedsApproval):
            original, audit = outcome

        diff = str(getattr(violation, "diff", "") or "")
        if not diff:
            # Fail closed: an empty diff would read as "the product code
            # was not touched", which is the one reading this record
            # must not be able to carry.
            paths = ", ".join(getattr(violation, "paths", ()) or ())
            diff = f"the product code was modified: {paths}\n"

        evidence = EvidenceRecord(
            item_id=item.id,
            command=list(item.command or []),
            exit_code=original.exit_code if original is not None else None,
            stdout=diff,
            stderr="",
            git_diff=diff,
        )
        self._evidence.setdefault(item.id, []).append(evidence)
        if audit is not None:
            self._audits[item.id] = _audit_triple(audit, item)
        else:
            self._audits[item.id] = _empty_audit(item)

        self._violation = {
            "item_id": item.id,
            "paths": list(getattr(violation, "paths", ()) or ()),
            "diff": diff,
        }
        self._verdicts[item.id] = self._judge_violation(item, evidence)
        mark(
            self._plan_id,
            item.id,
            "failed",
            attempts=attempt,
            spec_sha256=self._frozen.spec_sha256,
        )

    def _judge_violation(self, item: SpecItem, evidence: EvidenceRecord) -> Verdict:
        """The verdict of an item that modified the product code.

        The question asked is A5's own — *is the product code
        untouched?* — and the judge is still the only component that
        answers PASS or FAIL.  It answers FAIL, because the diff the
        evidence carries is not empty.
        """
        kwargs: Dict[str, Any] = {}
        if _accepts_keyword(self._judge, "root"):
            workspace = getattr(self._executor, "workspace", None)
            if workspace is not None:
                kwargs["root"] = Path(workspace)
        asked: Dict[str, Any] = {
            "id": item.id,
            "acceptance_predicate": dict(PRODUCT_CODE_UNTOUCHED),
        }
        return self._judge(asked, [evidence], **kwargs)

    def _execute(
        self,
        item: SpecItem,
        *,
        approved_command: Optional[List[str]] = None,
    ) -> Any:
        """Run one item through the executor.

        The executor is the only component that runs a command, and it
        answers in one of two ways: with the evidence and the audit
        triple, or with a :class:`NeedsApproval` — an action that is
        legal but needs a human before it may run.

        The approval a human gave is handed over when there is one: the
        executor does not ask a second time for an action that was
        already approved.  An executor that does not take the keyword —
        a test double, mostly — is called the way it always was.
        """
        runner = getattr(self._executor, "run_item", None)
        if callable(runner):
            if approved_command and _accepts_keyword(runner, "approved_command"):
                return runner(item, approved_command=approved_command)
            return runner(item)
        execute = getattr(self._executor, "execute", None)
        if callable(execute):
            if approved_command and _accepts_keyword(execute, "approved_command"):
                return execute(item, approved_command=approved_command)
            return execute(item)
        raise TypeError(
            "the executor must offer run_item(item) or execute(item); "
            f"got {type(self._executor).__name__}"
        )

    def _judge_item(self, item: SpecItem) -> Verdict:
        """Have the judge decide one item from the evidence it produced.

        Every attempt's evidence is handed over, oldest first: the judge
        decides from the newest usable record, and the older ones are
        the history of the earlier attempts.
        """
        records = list(self._evidence.get(item.id, []))
        kwargs: Dict[str, Any] = {}
        if _accepts_keyword(self._judge, "root"):
            workspace = getattr(self._executor, "workspace", None)
            if workspace is not None:
                kwargs["root"] = Path(workspace)
        return self._judge(item, records, **kwargs)

    def _wait_for(
        self,
        item: SpecItem,
        approval: NeedsApproval,
        *,
        attempt: int,
    ) -> None:
        """Turn an action that needs a human into a waiting item.

        The question is asked through the registry — the production one
        when the stage was not given one: the existing HITL store takes
        the row, the plan's own binding takes the delivery.  A registry
        that cannot store the question leaves it in the progress file
        instead — the item waits either way, and a question that cannot
        be stored must not become a question nobody is told about.

        The wait is then **driven where it waits**: the stage reads the
        answer that was given, rechecks what is outside, and fails the
        wait when its deadline passes.  It never answers: a question
        nobody answered stays unanswered, and the stage parks the item
        rather than deciding it.

        The waiting row names the pull request the request is about —
        its number and its link — and says that it is an approval, so
        a human reading the waiting rows is told which pull request
        they are being asked about (architecture decision points R5
        and R6).  A command that names no pull request leaves both of
        them empty; the wait is created either way.
        """
        reference = extract_pr_ref(approval.command)
        pr_number = reference["pr_number"] if reference is not None else None
        pr_url = reference["pr_url"] if reference is not None else None
        record: Dict[str, Any] = {
            "type": "approval",
            "question": approval.question,
            "command": list(approval.command),
            "source": POSTPROCESS_APPROVAL_SOURCE,
            "pr_number": pr_number,
            "pr_url": pr_url,
        }
        registry = self._registry()
        try:
            registry.create_approval_request(
                _as_mapping(item),
                approval,
                pr_number=pr_number,
                pr_url=pr_url,
            )
        except WaitError:
            # No store behind the registry: the wait lives in the
            # progress file, which is where the panel reads it from.
            pass
        try:
            record = dict(registry.get(item.id))
        except WaitError:
            pass

        settled = self._drive(registry, item.id)
        if settled is not None:
            record = dict(settled)

        status = ITEM_STATUS_BY_WAIT_STATUS.get(str(record.get("status", "")), "waiting")
        if status == "failed":
            # A wait that ran out of time is a failure of the item
            # behind it — the requirement was not met, and a human is
            # told about it.
            self._settled[item.id] = (
                VERDICT_FAIL if item.required else VERDICT_SKIPPED
            )

        self._waiting[item.id] = record
        mark(
            self._plan_id,
            item.id,
            status,
            attempts=attempt,
            wait=record,
            spec_sha256=self._frozen.spec_sha256,
        )

    def _registry(self) -> WaitRegistry:
        """The registry this stage asks through.

        The stage is usually built without one — the verification loop
        does not inject a registry — so it builds the **production** one
        itself: the existing HITL store on the state database and the
        plan's own ``hitl_config.json`` binding.  That is what makes
        the question land in the table the answer route reads, and in
        the channel the plan is bound to.

        A registry that cannot be built — a state database that cannot
        be opened — degrades to one without a store: the question is
        then recorded in the progress file alone, which is where the
        panel reads it from.
        """
        if self._waits is not None:
            return self._waits
        try:
            self._waits = production_registry(
                self._plan_id,
                self._plan_dir,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 — 没有 store 也能等
            logger.warning(
                "[postprocess] plan=%s: the production wait registry could "
                "not be built (%s); the question is recorded in the "
                "progress file alone",
                self._plan_id,
                exc,
            )
            self._waits = WaitRegistry(self._plan_id, clock=self._clock)
        return self._waits

    def _drive(self, registry: WaitRegistry, item_id: str) -> Optional[Dict[str, Any]]:
        """Drive one wait to its end, at the waiting point itself.

        The drive blocks in the thread that is already running this
        stage — it starts no thread and no timer — and it comes back on
        its own: a wait nobody answered is still pending when the
        window is over, and the stage parks the item.

        Nothing the drive does is raised to the stage: a wait it cannot
        read or recheck is a wait that is not settled yet.
        """
        try:
            return registry.drive(item_id)
        except WaitError as exc:
            logger.warning(
                "[postprocess] plan=%s item=%s: the wait could not be "
                "driven (%s)",
                self._plan_id,
                item_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # The terminal state
    # ------------------------------------------------------------------

    def _finish(self, items: Sequence[SpecItem]) -> Dict[str, Any]:
        """Decide the terminal status, write the report, notify once."""
        report_items = self._report_items(items)
        failed = self._failed_items(items)
        required_failed = [item.id for item in failed if item.required]
        waiting = [item for item in items if self._verdict_of(item) == VERDICT_WAITING]

        if required_failed or self._violation is not None:
            # A product-code violation fails the stage even when the item
            # that caused it was optional: the finishing stage is not
            # allowed to change the product code, and that is a fact
            # about the stage, not about one item's requirement.
            terminal: Optional[str] = TERMINAL_STATUS_FAILED
        elif waiting:
            # The stage is suspended, not finished: a report written now
            # would claim a terminal status the stage has not reached.
            terminal = None
        else:
            terminal = TERMINAL_STATUS_COMPLETED

        if terminal is None:
            return {
                "spec_sha256": self._frozen.spec_sha256,
                "terminal_status": None,
                "items": report_items,
                "failure_context_ref": None,
            }

        failure_context_ref = (
            self._progress_reference() if terminal == TERMINAL_STATUS_FAILED else None
        )
        report = write_report(
            self._plan_id,
            frozen_spec=self._frozen,
            item_verdicts=report_items,
            failure_context_ref=failure_context_ref,
            terminal_status=terminal,
            plans_dir=self._plans_dir(),
        )

        if terminal == TERMINAL_STATUS_FAILED:
            self._notify_failure(
                [item.id for item in failed], failure_context_ref
            )
        return report

    def _report_items(self, items: Sequence[SpecItem]) -> List[Dict[str, Any]]:
        """The report's items, in the order the requirement book declared.

        Every item of the book appears exactly once — the item that is
        waiting appears too, marked as waiting.

        An item an earlier run settled keeps that run's own entry — its
        evidence excerpt and its audit triple included.  The work was not
        repeated, so the evidence of the run that did it is still this
        report's evidence.
        """
        report_items = []
        for item in items:
            verdict = self._verdicts.get(item.id)
            carried = self._carried.get(item.id)
            if verdict is None and carried is not None:
                report_items.append(self._carried_entry(item, carried))
                continue
            report_items.append(
                {
                    "id": item.id,
                    "verdict": self._verdict_of(item),
                    "evidence_excerpt": (
                        verdict.evidence_excerpt if verdict is not None else ""
                    ),
                    "audit": self._audits.get(item.id) or _empty_audit(item),
                }
            )
        return report_items

    def _verdict_of(self, item: SpecItem) -> str:
        """The verdict the report carries for one item.

        An item that did not pass and was not required to is reported as
        ``SKIPPED``: the stage went on without it, and the report says
        so.  The progress file keeps the harder truth — the item's
        status is ``failed``.

        An item that modified the product code is reported as ``FAIL``
        whether it was required or not: the report's ``SKIPPED`` means
        "an optional item that was not required to run", and this item
        did run — it ran into a violation, and hiding that behind
        ``SKIPPED`` would say less than what happened.

        An item an earlier run settled keeps that run's verdict — this
        run does not repeat the work, so it has no fresh evidence to
        show for it.
        """
        verdict = self._verdicts.get(item.id)
        if verdict is None:
            return self._settled.get(item.id, VERDICT_WAITING)
        if verdict.passed:
            return VERDICT_PASS
        if item.required or self._violated_item() == item.id:
            return VERDICT_FAIL
        return VERDICT_SKIPPED

    def _violated_item(self) -> str:
        """The id of the item that modified the product code.

        An empty string when no item did — the stage has not seen a
        violation.
        """
        if not self._violation:
            return ""
        return str(self._violation.get("item_id") or "")

    def _failed_items(self, items: Sequence[SpecItem]) -> List[SpecItem]:
        """The items that did not pass — required ones and optional ones."""
        return [
            item
            for item in items
            if self._verdict_of(item) in (VERDICT_FAIL, VERDICT_SKIPPED)
        ]

    def _notify_failure(
        self,
        failed_items: Sequence[str],
        evidence_path: Optional[str],
    ) -> None:
        """Send the failure notification — once.

        The notifier is the real one by default, and it delivers through
        the sink the plan is already bound to.  A notifier without a
        channel says so loudly rather than swallowing the delivery: a
        failure nobody was told about is a failure nobody will fix.
        """
        kwargs: Dict[str, Any] = {}
        if _accepts_keyword(self._notify, "sink"):
            kwargs["sink"] = self._sink
        self._notify(self._plan_id, list(failed_items), str(evidence_path), **kwargs)

    # ------------------------------------------------------------------
    # The files
    # ------------------------------------------------------------------

    def _plans_dir(self) -> Path:
        """The plans root the artefacts are written under.

        It is taken from the progress module's own resolution, so the
        progress file and the report are always written side by side —
        and the report's ``failure_context_ref`` always points at a file
        that sits next to it.
        """
        return progress_path(self._plan_id).parent.parent

    def _progress_reference(self) -> str:
        """Where a human finds the context of the failure.

        The reference is relative to the repository root when the plans
        tree lives inside the repository — ``plans/<id>/…`` is what a
        reader can open — and absolute otherwise.
        """
        resolved = progress_path(self._plan_id).resolve()
        try:
            return resolved.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return str(resolved)

    def _recorded_statuses(self) -> Dict[str, str]:
        """The status the progress file records for each item.

        A progress file written for **another** requirement book is
        ignored: its verdicts were judged against criteria that are no
        longer the ones this round is bound to, so they say nothing
        about this round.
        """
        return self._progress_statuses(load_progress(self._plan_id))

    def _settled_verdicts(self, items: Sequence[SpecItem]) -> Dict[str, str]:
        """The verdicts an earlier run settled, keyed by item id.

        They are the progress file's word, not this run's: the items
        behind them are not run again, so this run has no fresh evidence
        to show for them.
        """
        settled: Dict[str, str] = {}
        for item in items:
            status = self._recorded.get(item.id, "")
            if status == "passed":
                settled[item.id] = VERDICT_PASS
            elif status == "waiting":
                settled[item.id] = VERDICT_WAITING
            elif status == "failed":
                settled[item.id] = VERDICT_FAIL if item.required else VERDICT_SKIPPED
        return settled


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _audit_triple(audit: Any, item: SpecItem) -> Dict[str, Any]:
    """The audit triple of one executed action.

    What was done, the argv verbatim, and what it acted on — the three
    facts a reader needs to check the action against the item it
    belongs to.
    """
    action = _field(audit, "action", "")
    command = _field(audit, "command", [])
    target = _field(audit, "target", "")
    return {
        "action": str(action or item.action),
        "command": [str(argument) for argument in list(command or [])],
        "target": str(target or item.target or item.id),
    }


def _empty_audit(item: SpecItem) -> Dict[str, Any]:
    """The audit triple of an action that never ran."""
    return {
        "action": item.action,
        "command": [],
        "target": item.target or item.id,
    }


def _as_mapping(item: SpecItem) -> Dict[str, Any]:
    """One requirement item as a plain mapping.

    The wait registry reads the item as a mapping; the requirement book
    is a dataclass.
    """
    return {
        "id": item.id,
        "action": item.action,
        "command": list(item.command or []),
        "external": item.external,
        "target": item.target,
        "required": item.required,
        "wait": dict(item.wait or {}),
    }


def _field(source: Any, name: str, default: Any = None) -> Any:
    """One field of a record or an item — by key, or by attribute."""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)