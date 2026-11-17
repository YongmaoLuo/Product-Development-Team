"""The finishing stage after a crash: what a kill leaves behind, and what must not happen twice.

Background
----------
The finishing stage (收尾阶段) runs outward-facing commands — ``git push``,
``gh pr create`` — and those get killed: the machine sleeps, the CI job is
reaped, a deploy dies half way.  When the stage is killed, the next process
must be able to continue it **without repeating what already happened**, and
that decision cannot live in the killed process's memory.  It lives in the
two files on disk:

  * ``postprocess_progress.json`` — rewritten after every item, so it says
    which items are finished, which were killed mid-flight, and which failed;
  * ``postprocess_report.json`` — written once, at the end, so it says what
    the last run concluded.

``PostProcessOrchestrator.resume(plan_id, plan_dir, **kwargs)`` is the entry
point that reads those two files and continues the stage from them.

The properties this file witnesses are the ones that are easy to get wrong
and impossible to notice afterwards:

  * ``test_resume_skips_passed_items`` — a resumed run does not run an item
    the progress file records as passed;
  * ``test_resume_retries_running_item`` — an item that was killed mid-flight
    is run again, exactly once;
  * ``test_no_duplicate_outbound_action_after_restart`` — a restart does not
    repeat the outward action an item already did: the bare repository keeps
    one branch, and the stub log keeps one line;
  * ``test_corrupt_progress_is_not_silently_passed`` — a damaged progress
    file raises ``ProgressError`` instead of being read as an empty progress,
    which would re-run the whole book;
  * ``test_report_file_is_0600_after_resume`` — the report a resumed run
    writes is private (0600) and says each item exactly once, whichever of
    the three crash points the stage died at.

Why the report's mode is asserted here
--------------------------------------
The report is the stage's audit artefact, and the two files on disk are the
*only* context a resumed run has: it repeats nothing that the progress file
records as passed, and it says each item once.  Both properties are worth
pinning where the artefacts really land — the report of a plan that was
killed and continued is written twice, by two different processes, and the
second write must be held to the same contract as the first: private, and
free of duplicated entries.

The three crash points
----------------------
The stage can die in three places, and each leaves a different state behind:

  * **after the judgement, before the progress write** — the item was judged
    but nothing recorded it, so it is unfinished and is run again;
  * **after the progress write, before the report write** — every item is
    finished and the stage finished, but the answer was never written, so
    the resume writes it without running anything;
  * **in the middle of the retry round** — the item that was being retried
    was killed again, so it is unfinished and is run again.

What is real and what is injected
---------------------------------
Real: the requirement book is loaded and frozen by ``postprocess.spec``, the
verdict comes from the real ``postprocess.judge``, the progress file and the
report are written by ``postprocess.progress`` / ``postprocess.report``, and
the failure notification is delivered by ``postprocess.notify``.

Injected: the **executor** (a script that answers per item, records every
call, and can crash) and the notification **sink** (a recorder that sends
nothing).

Test command::

    pytest backend/tests/integration/postprocess/test_crash_and_resume.py -v
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

import pytest

from postprocess import orchestrator as orchestrator_module
from postprocess import progress as progress_module
from postprocess.models import AuditRecord, EvidenceRecord
from postprocess.notify import notify_failure
from postprocess.orchestrator import PostProcessOrchestrator
from postprocess.progress import ProgressError, load_progress, progress_path
from postprocess.report import read_report, report_path
from tests.fixtures.postprocess.builders import FakeNotifier, make_item


#: This file is an integration test — it writes real files (the progress
#: file, the report, a git repository) and reads them back.
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


#: What a scripted executor may answer with: an exit code, or the crash
#: that kills the stage where it stands.
Outcome = Union[int, BaseException]


class ScriptedExecutor:
    """An executor that answers from a script, records every call, and can die.

    The script maps an item id to the outcomes of its attempts: the first
    entry is the first attempt, the second the retry, and a single entry
    repeats for every attempt.  An outcome of ``0`` is a command that
    succeeded, ``1`` one that failed, and an exception is the kill that
    ends the process where it stands.

    Every call is recorded, in order — which is what lets a case say "the
    resumed run called exactly this item" and have it be a fact about the
    run rather than an inference from the report.

    Args:
        script: The outcomes per item id.
        action: The outward action the executor performs before it
            answers.  It is called with the item id, and it is where a
            repeated action would leave a visible trace.
    """

    def __init__(
        self,
        script: Mapping[str, Sequence[Outcome]],
        action: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._script = {key: list(value) for key, value in script.items()}
        self._action = action
        self.calls: List[str] = []

    def run_item(self, item: Any) -> Any:
        """Run one item and answer from the script.

        Args:
            item: The requirement item.

        Returns:
            ``(EvidenceRecord, AuditRecord)``.

        Raises:
            BaseException: Whatever the script names — the kill that ends
                the stage where it stands.
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

        if isinstance(outcome, BaseException):
            raise outcome

        if self._action is not None:
            self._action(item_id)

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


def crash_before_progress_write(
    patch: pytest.MonkeyPatch,
    item_id: str,
    status: str,
) -> None:
    """Kill the stage between the judgement and the progress write.

    The item was judged, but the file that says so was never updated — so
    the file knows nothing about it, and a resumed run has to treat it as
    unfinished.

    Args:
        patch: The patch context the crash lives in.
        item_id: The item whose progress write is killed.
        status: The status the killed write would have recorded.
    """
    original = orchestrator_module.mark

    def crashing_mark(
        plan_id: str,
        marked_item: str,
        marked_status: str,
        **kwargs: Any,
    ) -> Any:
        if marked_item == item_id and marked_status == status:
            raise RuntimeError("killed after the judgement")
        return original(plan_id, marked_item, marked_status, **kwargs)

    patch.setattr(orchestrator_module, "mark", crashing_mark)


def crash_before_report_write(patch: pytest.MonkeyPatch) -> None:
    """Kill the stage between the progress write and the report write.

    Every item is through the stage and the file says so, but the terminal
    answer was never written.

    Args:
        patch: The patch context the crash lives in.
    """

    def crashing_write_report(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("killed after the progress, before the report")

    patch.setattr(orchestrator_module, "write_report", crashing_write_report)


# ---------------------------------------------------------------------------
# The outward action — a branch in a bare repository
# ---------------------------------------------------------------------------


def bare_repository(path: Path) -> Path:
    """A bare repository with one commit on ``main``.

    The branch count of this repository is the evidence that an outward
    action happened exactly once: a second execution of the same item
    leaves a second branch behind, and a branch that is not there is an
    action that never happened.

    Args:
        path: Where the bare repository is created.

    Returns:
        The path of the bare repository.
    """
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    work = path.parent / f"{path.name}-seed"
    subprocess.run(
        ["git", "init", "-b", "main", str(work)],
        check=True,
        capture_output=True,
    )
    (work / "README.md").write_text("seed\n", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "postprocess test",
            "GIT_AUTHOR_EMAIL": "postprocess@example.invalid",
            "GIT_COMMITTER_NAME": "postprocess test",
            "GIT_COMMITTER_EMAIL": "postprocess@example.invalid",
        }
    )
    subprocess.run(
        ["git", "-C", str(work), "add", "README.md"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", str(path), "main"],
        check=True,
        capture_output=True,
        env=env,
    )
    return path


def branches(repository: Path) -> List[str]:
    """The branches of a repository, by name.

    Args:
        repository: The repository to read.

    Returns:
        The branch names, sorted.
    """
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        line.strip() for line in completed.stdout.splitlines() if line.strip()
    )


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_resume_skips_passed_items(plan_dir: Path) -> None:
    """A resumed run does not run an item the progress file records as passed."""
    write_book(
        plan_dir,
        [book_item("item-1"), book_item("item-2"), book_item("item-3")],
    )

    # The first crash point: item-2 was judged, but the progress write
    # that would have recorded it never happened.
    with pytest.MonkeyPatch.context() as patch:
        crash_before_progress_write(patch, "item-2", "passed")
        crashed = ScriptedExecutor({"item-1": [0], "item-2": [0], "item-3": [0]})
        with pytest.raises(RuntimeError):
            PostProcessOrchestrator(
                PLAN_ID,
                plan_dir=plan_dir,
                executor=crashed,
            ).start()

    progress = load_progress(PLAN_ID)
    assert progress["items"]["item-1"]["status"] == "passed"
    assert progress["items"]["item-2"]["status"] == "running", (
        "the crash was before the progress write, so nothing recorded "
        f"item-2's verdict: {progress['items']['item-2']!r}"
    )

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0], "item-3": [0]})
    report = PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert "item-1" not in resumed.calls, (
        "an item the progress file records as passed must not be run again "
        f"— the executor was called {resumed.calls!r}"
    )
    assert resumed.calls == ["item-2", "item-3"], (
        "the unfinished items are the ones that run: item-2 was killed "
        f"before it was recorded, item-3 never started.  The executor was "
        f"called {resumed.calls!r}"
    )
    assert report["terminal_status"] == "completed"
    assert [item["verdict"] for item in report["items"]] == [
        "PASS",
        "PASS",
        "PASS",
    ]


def test_resume_retries_running_item(plan_dir: Path) -> None:
    """An item that was killed mid-flight is run again — exactly once."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    crashed = ScriptedExecutor(
        {"item-1": [0], "item-2": [RuntimeError("killed mid-flight")]}
    )
    with pytest.raises(RuntimeError):
        PostProcessOrchestrator(
            PLAN_ID,
            plan_dir=plan_dir,
            executor=crashed,
        ).start()

    progress = load_progress(PLAN_ID)
    assert progress["items"]["item-1"]["status"] == "passed"
    assert progress["items"]["item-2"]["status"] == "running", (
        "the item was killed before it was judged, so it is unfinished"
    )

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    report = PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert resumed.calls == ["item-2"], (
        "the crashed item is run again, and nothing else — the executor "
        f"was called {resumed.calls!r}"
    )
    assert report["terminal_status"] == "completed"
    assert [item["verdict"] for item in report["items"]] == ["PASS", "PASS"]

    progress = load_progress(PLAN_ID)
    assert progress["items"]["item-2"]["status"] == "passed"


def test_no_duplicate_outbound_action_after_restart(
    plan_dir: Path,
    tmp_path: Path,
) -> None:
    """A restart does not repeat the outward action an item already did."""
    bare = bare_repository(tmp_path / "remote.git")
    action_log = tmp_path / "actions.log"

    def outward_action(item_id: str) -> None:
        """Create a branch in the bare repository — the action that shows."""
        if item_id != "item-1":
            return
        done = (
            action_log.read_text(encoding="utf-8").splitlines()
            if action_log.exists()
            else []
        )
        action_log.write_text(
            "\n".join(done + [item_id]) + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(bare), "branch", f"feature-{len(done) + 1}"],
            check=True,
            capture_output=True,
        )

    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    crashed = ScriptedExecutor(
        {"item-1": [0], "item-2": [RuntimeError("killed mid-flight")]},
        action=outward_action,
    )
    with pytest.raises(RuntimeError):
        PostProcessOrchestrator(
            PLAN_ID,
            plan_dir=plan_dir,
            executor=crashed,
        ).start()

    assert branches(bare) == ["feature-1", "main"], (
        "the first run created exactly one branch"
    )

    resumed = ScriptedExecutor(
        {"item-1": [0], "item-2": [0]},
        action=outward_action,
    )
    PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert branches(bare) == ["feature-1", "main"], (
        "the branch was created once — a resumed run that repeated the "
        f"outward action would leave a second branch behind: {branches(bare)!r}"
    )
    assert action_log.read_text(encoding="utf-8").splitlines() == ["item-1"], (
        "the outward action was performed once, not twice"
    )
    assert "item-1" not in resumed.calls, (
        f"item-1 had already run — the executor was called {resumed.calls!r}"
    )


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("{not json at all", id="not-json"),
        pytest.param('{"items": []}', id="items-not-an-object"),
        pytest.param('["a list"]', id="not-an-object"),
    ],
)
def test_corrupt_progress_is_not_silently_passed(
    plan_dir: Path,
    document: str,
) -> None:
    """A damaged progress file is reported, never read as an empty progress."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    path = progress_path(PLAN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")

    executor = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    with pytest.raises(ProgressError):
        PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=executor)

    assert executor.calls == [], (
        "a damaged progress file must not be read as an empty progress — "
        f"the executor was called {executor.calls!r}"
    )


def test_resume_writes_the_report_when_only_the_progress_survived(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """The crash between the progress write and the report write is repaired."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    with pytest.MonkeyPatch.context() as patch:
        crash_before_report_write(patch)
        crashed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
        with pytest.raises(RuntimeError):
            PostProcessOrchestrator(
                PLAN_ID,
                plan_dir=plan_dir,
                executor=crashed,
            ).start()

    progress = load_progress(PLAN_ID)
    assert progress["items"]["item-1"]["status"] == "passed"
    assert progress["items"]["item-2"]["status"] == "passed"
    assert read_report(PLAN_ID, plans_dir=plans_root) is None, (
        "the stage was killed before it wrote its answer"
    )

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    report = PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert resumed.calls == [], (
        "every item was through the stage already — the executor was called "
        f"{resumed.calls!r}"
    )
    assert report["terminal_status"] == "completed"
    assert [item["verdict"] for item in report["items"]] == ["PASS", "PASS"]
    assert read_report(PLAN_ID, plans_dir=plans_root) == report


def test_report_without_progress_refuses_to_repeat_actions(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """A plan whose progress was lost still refuses to repeat a finished action."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    first = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    report = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=first,
    ).start()
    assert report["terminal_status"] == "completed"

    progress_path(PLAN_ID).unlink()

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    again = PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert resumed.calls == [], (
        "the report is the terminal truth — an action that already happened "
        f"must not happen again; the executor was called {resumed.calls!r}"
    )
    assert again == report
    assert read_report(PLAN_ID, plans_dir=plans_root) == report


def test_resume_continues_a_failed_item(plan_dir: Path) -> None:
    """An item the progress file records as failed is continued, not skipped."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    sink = FakeNotifier()
    crashed = ScriptedExecutor({"item-1": [0], "item-2": [1, 1]})
    report = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=crashed,
        notify=notify_failure,
        sink=sink,
    ).start()
    assert report["terminal_status"] == "postprocess_failed"

    progress = load_progress(PLAN_ID)
    assert progress["items"]["item-2"]["status"] == "failed"

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    again = PostProcessOrchestrator.resume(
        PLAN_ID,
        plan_dir,
        executor=resumed,
        notify=notify_failure,
        sink=sink,
    )

    assert resumed.calls == ["item-2"], (
        "the failed item is continued, and the passed one is left alone — "
        f"the executor was called {resumed.calls!r}"
    )
    assert again["terminal_status"] == "completed"
    assert [item["verdict"] for item in again["items"]] == ["PASS", "PASS"]
    assert again["items"][0]["evidence_excerpt"] == (
        report["items"][0]["evidence_excerpt"]
    ), (
        "the evidence of the first run's item is still visible in the "
        "resumed report — the work was not repeated, so its evidence is "
        "still the evidence"
    )
    assert again["items"][0]["evidence_excerpt"], (
        "the carried entry keeps the evidence excerpt, not an empty string"
    )


def test_resume_after_a_crash_in_the_retry_round(plan_dir: Path) -> None:
    """The crash in the middle of the retry round leaves an unfinished item."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    crashed = ScriptedExecutor(
        {"item-1": [0], "item-2": [1, RuntimeError("killed in the retry")]}
    )
    with pytest.raises(RuntimeError):
        PostProcessOrchestrator(
            PLAN_ID,
            plan_dir=plan_dir,
            executor=crashed,
        ).start()

    progress = load_progress(PLAN_ID)
    assert progress["items"]["item-2"]["status"] == "running", (
        "the retry round was killed mid-flight, so the item it was retrying "
        f"is unfinished: {progress['items']['item-2']!r}"
    )

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    report = PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert resumed.calls == ["item-2"], (
        "the item that was being retried is run again, and nothing else — "
        f"the executor was called {resumed.calls!r}"
    )
    assert report["terminal_status"] == "completed"
    assert [item["verdict"] for item in report["items"]] == ["PASS", "PASS"]


def test_failure_notification_is_not_duplicated_after_a_resume(
    plan_dir: Path,
) -> None:
    """A resumed run does not deliver the failure notification twice."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    sink = FakeNotifier()
    crashed = ScriptedExecutor({"item-1": [0], "item-2": [1, 1]})
    PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=crashed,
        notify=notify_failure,
        sink=sink,
    ).start()
    assert sink.count == 1, "the failed stage notified once"

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [1]})
    PostProcessOrchestrator.resume(
        PLAN_ID,
        plan_dir,
        executor=resumed,
        notify=notify_failure,
        sink=sink,
    )

    assert sink.count == 1, (
        "the failure notification is delivered once — the sink was called "
        f"{sink.count} times"
    )


# ---------------------------------------------------------------------------
# The report the resumed run writes
# ---------------------------------------------------------------------------


def crash_the_stage(
    crash_point: str,
    patch: pytest.MonkeyPatch,
) -> ScriptedExecutor:
    """Run the stage until it dies at ``crash_point``.

    Args:
        crash_point: Which of the three places the stage dies at:
            ``"before_progress_write"`` (after the judgement, before the
            progress write), ``"before_report_write"`` (after the progress
            write, before the report write) or ``"in_retry_round"`` (the
            retry of a failing item is killed).
        patch: The patch context the crash lives in.

    Returns:
        The executor that was killed.
    """
    if crash_point == "before_progress_write":
        crash_before_progress_write(patch, "item-2", "passed")
        return ScriptedExecutor(
            {"item-1": [0], "item-2": [0], "item-3": [0]}
        )
    if crash_point == "before_report_write":
        crash_before_report_write(patch)
        return ScriptedExecutor(
            {"item-1": [0], "item-2": [0], "item-3": [0]}
        )
    if crash_point == "in_retry_round":
        return ScriptedExecutor(
            {"item-1": [0], "item-2": [1, RuntimeError("killed in the retry")]}
        )
    raise ValueError(f"unknown crash point: {crash_point!r}")


@pytest.mark.parametrize(
    ("crash_point", "expected_calls", "expected_ids"),
    [
        pytest.param(
            "before_progress_write",
            ["item-2", "item-3"],
            ["item-1", "item-2", "item-3"],
            id="before-progress-write",
        ),
        pytest.param(
            "before_report_write",
            [],
            ["item-1", "item-2", "item-3"],
            id="before-report-write",
        ),
        pytest.param(
            "in_retry_round",
            ["item-2"],
            ["item-1", "item-2", "item-3"],
            id="in-retry-round",
        ),
    ],
)
def test_report_file_is_0600_after_resume(
    plan_dir: Path,
    plans_root: Path,
    crash_point: str,
    expected_calls: List[str],
    expected_ids: List[str],
) -> None:
    """The report of a resumed run is 0600 and says each item once."""
    write_book(
        plan_dir,
        [book_item("item-1"), book_item("item-2"), book_item("item-3")],
    )

    with pytest.MonkeyPatch.context() as patch:
        crashed = crash_the_stage(crash_point, patch)
        with pytest.raises(RuntimeError):
            PostProcessOrchestrator(
                PLAN_ID,
                plan_dir=plan_dir,
                executor=crashed,
            ).start()

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0], "item-3": [0]})
    report = PostProcessOrchestrator.resume(PLAN_ID, plan_dir, executor=resumed)

    assert resumed.calls == expected_calls, (
        "a resumed run repeats exactly the unfinished items — an item the "
        f"progress file records as passed must not run again.  The executor "
        f"was called {resumed.calls!r}"
    )

    ids = [item["id"] for item in report["items"]]
    assert ids == expected_ids, (
        "the report says every item of the book, once, in the book's order — "
        f"it says {ids!r}"
    )
    assert len(set(ids)) == len(ids), (
        f"the report repeats an item — it says {ids!r}"
    )
    assert report["terminal_status"] == "completed"

    path = report_path(PLAN_ID, plans_root)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, (
        "the report of a resumed run is an audit artefact and is written "
        f"private — the mode is {oct(mode)}"
    )

    progress_mode = stat.S_IMODE(progress_path(PLAN_ID).stat().st_mode)
    assert progress_mode == 0o600, (
        "the progress file is the resumed run's only context, and it is "
        f"private too — the mode is {oct(progress_mode)}"
    )


def test_report_file_is_0600_after_a_resume_rewrites_it(
    plan_dir: Path,
    plans_root: Path,
) -> None:
    """The report a resumed run rewrites is 0600 and says each item once."""
    write_book(plan_dir, [book_item("item-1"), book_item("item-2")])

    sink = FakeNotifier()
    first = ScriptedExecutor({"item-1": [0], "item-2": [1, 1]})
    report = PostProcessOrchestrator(
        PLAN_ID,
        plan_dir=plan_dir,
        executor=first,
        notify=notify_failure,
        sink=sink,
    ).start()
    assert report["terminal_status"] == "postprocess_failed"

    path = report_path(PLAN_ID, plans_root)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, (
        "the first report is written private — the mode is "
        f"{oct(mode)}"
    )

    resumed = ScriptedExecutor({"item-1": [0], "item-2": [0]})
    again = PostProcessOrchestrator.resume(
        PLAN_ID,
        plan_dir,
        executor=resumed,
        notify=notify_failure,
        sink=sink,
    )

    assert resumed.calls == ["item-2"], (
        "only the failed item is continued — the passed one is left alone, "
        f"the executor was called {resumed.calls!r}"
    )

    ids = [item["id"] for item in again["items"]]
    assert ids == ["item-1", "item-2"], (
        f"the rewritten report says every item of the book, once — it says "
        f"{ids!r}"
    )
    assert len(set(ids)) == len(ids), (
        f"the rewritten report repeats an item — it says {ids!r}"
    )
    assert again["terminal_status"] == "completed"

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, (
        "the rewritten report is private too — the mode is "
        f"{oct(mode)}"
    )
