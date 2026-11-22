"""The repair-03 shape, replayed end to end: attempt 1 oversteps, attempt 2 completes.

Why this file exists
--------------------
The pieces below — the undeclared-write gate, the attempt rollback, the
persistent attempt baseline, the retry hint that names the rollback
patch — are each pinned by their own unit file.  What none of them can
pin is the thing that actually matters to an operator: **do the four
pieces work together, in one run, on the shape the accident had?**

The shape is the one ``repair-03`` had: a task that declares exactly one
file, whose sub-agent writes that file *and* one more, and then commits
its own work.  The replay drives it exactly like that:

* **attempt 1** — the sub-agent writes ``docs/readme.md`` (declared) and
  ``scripts/ci_gate.sh`` (never declared), then commits both with
  ``git add -A && git commit -m 'wip'``.  The attempt must fail as an
  undeclared write, the overstep must be rolled back, and the content
  that was rolled back must survive as a patch;
* **attempt 2** — the sub-agent writes only ``docs/readme.md``, with the
  same content as attempt 1.  The attempt must succeed.

What the replay has to prove
----------------------------
1. the task **completes** — the overstep is not allowed to strand the
   task, and the first attempt is not allowed to be excused by the
   rollback;
2. the overstep is **gone** from the tree, while the declared work
   **stays**;
3. the second attempt is judged against the **first attempt's**
   baseline — the sub-agent's own commit moved HEAD, and a baseline
   read afresh from HEAD would swallow that commit and make the change
   set the attempt is judged by empty;
4. the second attempt's prompt **names the patch** and says how to
   restore from it — otherwise the sub-agent's answer to "your work was
   rolled back" is to rewrite it from scratch;
5. the patch **still applies** after the rollback — a patch that
   ``git apply`` refuses is a story, not evidence.

No model is called anywhere in here: ``coding_tool.query`` is the fake
sub-agent, and the three gates around the executor (cross-verify,
commit, second-pass audit) are neutralised so that a failure can only
come from the behaviour under test.

The fake sub-agent is the one from
``backend/tests/unit/test_undeclared_write_gate_enforced.py`` — the same
skeleton, the same real ``GitManager`` over a real (tmp) repository, the
same "the sub-agent wrote the file directly, so no tool guard saw it"
shape.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from retry_manager import RetryManager  # noqa: E402
from tests.unit.test_undeclared_write_gate_enforced import (  # noqa: E402
    DECLARED_PATH,
    UNDECLARED_PATH,
    _git,
    _make_agent,
    _make_task,
)

pytestmark = pytest.mark.integration

#: What the sub-agent writes into the DECLARED file.  Attempt 1 and
#: attempt 2 write the same content: the second attempt is not "try
#: again, write something else", it is "the declared work is still
#: there, keep it".
DECLARED_CONTENT = "updated\n"

#: What the sub-agent writes OUTSIDE its declaration.  The shape from
#: the accident: a shell redirection into a script the task never
#: declared.
UNDECLARED_CONTENT = "#!/bin/sh\nexit 0\n"


class _RecordingRetryManager(RetryManager):
    """The real retry manager, with its attempt history kept for the assertions.

    The *real* one is what renders the ``undeclared_write`` retry hint,
    so a mock here would only test the mock.  This subclass copies what
    the real one records and adds nothing else: the executor keeps
    talking to a genuine ``RetryManager``, and the assertions can look
    at the failure texts after the run reset the state.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[tuple[str, str, bool]] = []

    def record_attempt(self, task_id: str, error: str, success: bool):
        self.attempts.append((task_id, str(error), bool(success)))
        super().record_attempt(task_id, error, success)


@dataclass
class _Replay:
    """Everything the replay produced, for the assertions to read."""

    repo: Path
    plan_dir: Path
    task: object
    agent: object
    ok: bool
    #: The context handed to the fake sub-agent, one entry per attempt.
    contexts: list[str] = field(default_factory=list)
    #: The HEAD after the sub-agent's own commit, recorded mid-replay.
    head_after_first_attempt: str = ""
    #: The HEAD the first attempt started from.
    head_before_first_attempt: str = ""
    #: Whether the task had been marked ``completed`` when each attempt
    #: was dispatched.
    completed_before_attempt: list[bool] = field(default_factory=list)

    @property
    def patch_path(self) -> Path:
        """Where the evidence patch of attempt 1 belongs."""
        return (
            self.plan_dir
            / ".pdt"
            / "evidence"
            / "rollbacks"
            / self.task.id
            / "1.patch"
        )

    @property
    def recorded_failures(self) -> list[str]:
        """Every failure text the executor recorded, in order."""
        return [error for _tid, error, success in self.agent.retry_manager.attempts
                if not success]

    @property
    def stored_baseline(self) -> str:
        """The baseline the store holds for this task."""
        store = self.agent._attempt_baseline_store(self.task)
        return store.get(
            self.agent._lineage_id(self.task.id), self.task.id,
        )


def _rev_parse(repo: Path) -> str:
    """The commit HEAD names in ``repo``."""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _write(repo: Path, rel: str, content: str) -> None:
    """Write ``content`` to ``repo/rel``, creating the parent directory."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _was_completed(agent, task) -> bool:
    """Whether the run had already marked ``task`` ``completed``."""
    return any(
        call.args[:2] == (task.id, "completed")
        for call in agent.task_manager.update_task_status.call_args_list
    )


def _events(agent, name: str) -> list[dict]:
    """The ``data`` of every logged event called ``name``."""
    calls = (
        agent.logger.info.call_args_list
        + agent.logger.warning.call_args_list
        + agent.logger.error.call_args_list
    )
    out: list[dict] = []
    for call in calls:
        if call.args and call.args[0] == name:
            out.append(call.kwargs.get("data") or {})
    return out


@pytest.fixture(scope="module")
def replay(tmp_path_factory) -> _Replay:
    """Run the whole two-attempt replay once, and hand the result over."""
    root = tmp_path_factory.mktemp("attempt-rollback-replay")

    repo = root / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / DECLARED_PATH).write_text("original\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "replay@example.com")
    _git(repo, "config", "user.name", "attempt rollback replay")
    _git(repo, "add", DECLARED_PATH)
    _git(repo, "commit", "-q", "-m", "base")

    plan_dir = root / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    head_before = _rev_parse(repo)

    # The fake sub-agent skeleton, reused from the gate's own file.
    agent = _make_agent(repo, writes={})
    # The plan directory is the state root: the attempt baseline and the
    # rollback evidence both live under it.
    agent.task_manager.tasks_file = plan_dir / "tasks.json"
    # The REAL retry manager — it is what renders the ``undeclared_write``
    # hint, so a mock would hide the very thing under test.
    agent.retry_manager = _RecordingRetryManager()

    task = _make_task()
    # The repair-03 shape: a task id in the refiner's ``repair-*``
    # namespace, so the lineage the baseline is keyed by is ``repair``.
    task.id = f"repair-03-{uuid.uuid4().hex[:8]}"

    contexts: list[str] = []
    completed_before: list[bool] = []
    head_after: list[str] = []

    def _fake_query(prompt, *_args, **_kwargs) -> str:
        contexts.append(prompt)
        completed_before.append(_was_completed(agent, task))
        attempt = len(contexts)
        if attempt == 1:
            # The overstep: the declared file AND an undeclared one, then
            # the sub-agent commits its own work.
            _write(repo, DECLARED_PATH, DECLARED_CONTENT)
            _write(repo, UNDECLARED_PATH, UNDECLARED_CONTENT)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "wip")
            head_after.append(_rev_parse(repo))
        else:
            # Only the declared work, with the same content.
            _write(repo, DECLARED_PATH, DECLARED_CONTENT)
        return "Done.\nTEST_RESULT: PASSED"

    agent.coding_tool.query.side_effect = _fake_query

    ok = agent._execute_task_with_retry(task, max_retries=1)

    return _Replay(
        repo=repo,
        plan_dir=plan_dir,
        task=task,
        agent=agent,
        ok=ok,
        contexts=contexts,
        head_after_first_attempt=head_after[0] if head_after else "",
        head_before_first_attempt=head_before,
        completed_before_attempt=completed_before,
    )


def test_repair_03_shaped_task_completes_on_second_attempt(replay: _Replay):
    """The overstep fails the first attempt — and the task still finishes.

    Two facts have to hold at once, and they pull in opposite
    directions: the undeclared write must NOT be excused by the
    rollback (the first attempt fails), and the failed attempt must NOT
    strand the task (the second attempt completes).
    """
    assert len(replay.contexts) == 2, (
        f"the task dispatched {len(replay.contexts)} time(s) — the replay "
        "expects exactly one attempt per dispatch"
    )
    assert replay.ok is True, (
        "the task did not complete: the undeclared write of the first "
        "attempt stranded it even though the second attempt wrote only "
        "what it declared"
    )
    assert replay.completed_before_attempt == [False, False], (
        "the task was marked completed before the second attempt even "
        f"started: {replay.completed_before_attempt!r} — the first "
        "attempt's undeclared write was excused"
    )
    assert _was_completed(replay.agent, replay.task), (
        "the task was never marked completed"
    )


def test_first_attempt_fails_as_an_undeclared_write(replay: _Replay):
    """The first attempt's failure carries the executor's own spelling.

    ``undeclared_write:`` is what ``failure_classification`` parses, and
    what the retry hint branches on.  A failure that lost the prefix
    would be read as "your code is wrong" — and the sub-agent would
    rewrite working code.
    """
    failures = replay.recorded_failures
    assert failures, (
        "the first attempt recorded no failure at all — the undeclared "
        "write never reached the retry history"
    )
    assert failures[0].startswith("undeclared_write:"), (
        "the first failure lost the executor's own spelling; recorded: "
        f"{failures[0]!r}"
    )
    assert UNDECLARED_PATH in failures[0], (
        f"the first failure does not name the undeclared path; recorded: "
        f"{failures[0]!r}"
    )
    events = _events(replay.agent, "task_undeclared_write")
    assert events, (
        "no task_undeclared_write event was logged — the gate that "
        "judges the change set against the declaration never fired"
    )
    assert events[0].get("attempt") == 1, (
        f"the undeclared write was not attributed to attempt 1: {events[0]!r}"
    )
    # A second, independent carrier of the same fact: the executor's own
    # retry log.  The retry manager's copy could in principle be a
    # different string; this one is what the executor logged.
    retries = _events(replay.agent, "task_retry")
    assert retries, (
        "the executor logged no retry for the failed first attempt"
    )
    logged = str(retries[0].get("test_reason") or "")
    assert logged.startswith("undeclared_write:"), (
        "the executor's retry log does not carry the undeclared_write "
        f"spelling; logged: {logged!r}"
    )


def test_first_attempt_rolls_back_undeclared_file(replay: _Replay):
    """The overstep is gone from the tree; the declared work stays."""
    assert not (replay.repo / UNDECLARED_PATH).exists(), (
        "the undeclared file is still in the working tree — the overstep "
        "was reported but never undone, so the next attempt starts from "
        "a tree that carries it"
    )
    assert (replay.repo / DECLARED_PATH).read_text(encoding="utf-8") == (
        DECLARED_CONTENT
    ), (
        "the declared file was rolled back too — the rollback threw away "
        "the task's own work along with the overstep"
    )


def test_second_attempt_reuses_first_baseline(replay: _Replay):
    """The second attempt is judged against the FIRST attempt's baseline.

    The sub-agent committed its own work, so HEAD moved on.  A baseline
    read afresh from HEAD would be a commit that already contains the
    first attempt's work: the change set the attempt is judged by
    disappears, and the rollback would roll back the wrong thing.
    """
    baseline = replay.stored_baseline
    assert baseline, (
        "no attempt baseline was stored — the second attempt cannot know "
        "which changes are its own"
    )
    assert replay.head_after_first_attempt != replay.head_before_first_attempt, (
        "the sub-agent's own commit did not move HEAD — the replay did "
        "not reproduce the shape it claims to reproduce"
    )
    assert baseline == replay.head_before_first_attempt, (
        f"the stored baseline is {baseline!r}, but the first attempt "
        f"started from {replay.head_before_first_attempt!r} — the "
        "baseline was re-read from HEAD instead of being recorded once"
    )
    assert baseline != replay.head_after_first_attempt, (
        "the stored baseline is the commit the sub-agent made in its "
        "first attempt — the baseline swallowed the attempt's own work, "
        "which is exactly what the recorded-once rule exists to prevent"
    )


def test_second_prompt_carries_patch_and_restore_hint(replay: _Replay):
    """The second attempt is told where the patch is and how to use it.

    Without this the failure reads as "your code is wrong", and the
    sub-agent's answer is to rewrite the work from scratch — throwing
    away the patch that already holds it.
    """
    assert len(replay.contexts) >= 2, (
        "the replay produced fewer than two contexts — there was no "
        "second attempt to carry the hint"
    )
    second = replay.contexts[1]
    assert str(replay.patch_path) in second, (
        "the second attempt's prompt does not name the rollback patch; "
        f"expected {str(replay.patch_path)!r} in the context"
    )
    assert "git apply" in second, (
        "the second attempt's prompt does not say how to restore from "
        "the patch"
    )
    assert "do not rewrite" in second, (
        "the second attempt's prompt does not forbid rewriting the work "
        "from scratch — the patch already holds it"
    )


def test_patch_is_reapplicable_after_rollback(replay: _Replay):
    """The captured patch still applies onto the tree the rollback made.

    A patch ``git apply`` refuses is a story, not evidence: the content
    that was rolled back would be gone for good.
    """
    patch = replay.patch_path
    assert patch.exists(), (
        f"no rollback evidence was written at {patch} — the content of "
        "the overstep is lost once the tree is put back"
    )
    body = patch.read_text(encoding="utf-8")
    assert UNDECLARED_PATH in body, (
        f"the evidence patch does not mention the rolled-back path; "
        f"content: {body!r}"
    )
    check = subprocess.run(
        ["git", "apply", "--check", str(patch)],
        cwd=str(replay.repo),
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, (
        "the evidence patch does not apply onto the rolled-back tree: "
        f"{check.stderr.strip()}"
    )
