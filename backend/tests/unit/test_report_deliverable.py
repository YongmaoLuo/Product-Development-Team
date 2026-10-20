"""A read-only task's deliverable is a FILE, not a diff (2026-10-19).

The executor had exactly two shapes of task: it dispatched the
sub-agent, then asked "did the project repo change?". For a code task
that is the right question — the diff IS the deliverable. For the
read-only / analysis tasks ("decide whether X is feasible", "assemble
the evidence for Y") it is the wrong question, and the framework had
no other one, so it answered it as best it could and got a second
question it could not answer at all:

  * the task declared no ``files_to_modify``, so ``PDT_READONLY_TASK``
    was stamped and the guard refused EVERY Edit/Write — including the
    one Write that was supposed to be the whole deliverable. The task
    was unsatisfiable, not read-only;
  * the report, if it was written anywhere, lived outside the repo, so
    the empty-diff gate read "no diff" as "the sub-agent produced
    nothing" and retried a task that was behaving exactly as asked;
  * and the adversarial second pass reviewed the CHAT REPLY — for a
    task told "write the report to this exact path" that reply is
    routinely "done, report written to <path>", which is nothing to
    judge.

Contract pinned here, at the three places it is spent:

1. the model: ``deliverable`` / ``report_path`` agree or the task is
   refused at parse time;
2. the executor's empty-diff gate routes BY DELIVERABLE, and a
   delivered report does not become a git commit;
3. the write-time guard lets exactly ONE file through for a report
   task, and still refuses everything else.

Every assertion below drives production code. The executor cases call
the real ``_execute_task_with_retry`` (only the LLM call and the
surrounding gates are stubbed) and the guard cases run the real
materialised hook script as a subprocess — because the thing under
test is a control-flow decision and a process exit code, and a copy of
either inside a test body can only ever prove the copy works.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from task import SubTask  # noqa: E402


# ---------------------------------------------------------------------------
# 1 — the model
# ---------------------------------------------------------------------------

def _base(**kw) -> SubTask:
    return SubTask(id="1", title="t", description="d", **kw)


def test_a_report_task_must_name_its_report():
    """``deliverable="report"`` with no path has no deliverable to check.

    Accepted, it would be dispatched with an instruction the sub-agent
    cannot satisfy ("write the report") and no address to write it to;
    the empty-diff gate would then retry it to the budget limit and the
    task would die having produced nothing.
    """
    with pytest.raises(ValueError) as exc:
        _base(deliverable="report")
    assert "report_path" in str(exc.value)

    with pytest.raises(ValueError):
        _base(deliverable="report", report_path="   ")


def test_a_code_task_may_not_carry_a_report_path():
    """The other direction: a path on a code task is a false belief.

    That belief — "this task's output is a file somewhere" — is exactly
    what used to produce a task marked completed with no diff and no
    artifact anywhere. Refusing it at parse time is the only place the
    two facts are both visible.
    """
    with pytest.raises(ValueError) as exc:
        _base(report_path="/tmp/report.md")
    assert "deliverable" in str(exc.value)


def test_the_default_is_code_and_survives_a_save_tasks_round_trip():
    """Every pre-2026-10-19 ``tasks.json`` must still parse.

    ``code`` is the default, and it is ALSO omitted from
    ``model_dump()`` — so a plan written by the new code is
    byte-identical to one written by the old. A report task, by
    contrast, must round-trip its path: dropping it would produce a row
    the validator then refuses, and the whole plan would fail to load
    after a single save.
    """
    plain = _base()
    assert plain.deliverable == "code"
    assert plain.report_path is None
    assert "deliverable" not in plain.model_dump()
    assert "report_path" not in plain.model_dump()

    report = _base(deliverable="report", report_path="/tmp/r.md")
    dumped = report.model_dump()
    assert dumped["deliverable"] == "report"
    assert dumped["report_path"] == "/tmp/r.md"
    assert SubTask(**dumped).report_path == "/tmp/r.md"


# ---------------------------------------------------------------------------
# 2 — the executor's empty-diff gate
# ---------------------------------------------------------------------------

def _executor_agent(project_dir: Path):
    """An ``AutonomousAgent`` skeleton wired so the REAL
    ``_execute_task_with_retry`` reaches its completion decision with
    everything around the deliverable dispatch neutralised.

    Only the two things under test are left real: the deliverable
    dispatch itself, and the branch that decides completed-vs-retry.
    ``_audit_task_second_pass`` is stubbed because it has its own
    tests below.
    """
    from agent import AutonomousAgent

    a = AutonomousAgent.__new__(AutonomousAgent)
    a.project_dir = Path(project_dir)
    a.subagent_cfg = None
    a.logger = MagicMock()
    a.config = MagicMock()
    a.config.executor_system_prompt = "fake prompt"
    a.executor = MagicMock()
    a.executor.had_previous_timeout.return_value = False
    a.retry_manager = MagicMock()
    a.retry_manager.get_retry_prompt_modifier.return_value = ""
    a.task_manager = MagicMock()
    # A real dict, so ``.setdefault`` in the report branch is exercised
    # for real rather than answering on a MagicMock.
    a.task_manager.runtime_overrides = {}
    a.rollback_manager = MagicMock()
    a._session_task_completed_counts = {}

    a.git_manager = MagicMock()
    a.git_manager.get_changed_files.return_value = []
    a.git_manager.get_diff.return_value = ""

    a.coding_tool = MagicMock()
    a.coding_tool.query.return_value = (
        "Wrote the report.\nTEST_RESULT: PASSED"
    )

    a._preflight_test_command_skip = MagicMock(return_value=False)
    a._parse_test_result = MagicMock(return_value=(True, ""))
    a._cross_verify_test_result = MagicMock(return_value=(True, ""))
    a._task_declared_files_exist = MagicMock(return_value=False)
    a._looks_like_audit_task = MagicMock(return_value=False)
    a._get_git_diff_stat_for_review = MagicMock(return_value="")
    a._refine_after_failure = MagicMock()
    a._persist_task_status = MagicMock()
    a._commit_task_changes = MagicMock()
    a._audit_task_second_pass = MagicMock(return_value=(True, ""))
    return a


def _report_task(report_path, project_dir: Path) -> SubTask:
    return SubTask(
        id="9-1",
        title="裁定 ServiceMesh 是否可行",
        description="读代码与文档，给出裁定与证据。",
        test_command="echo ok",
        # No source files touched — that is the whole point: the task
        # declares nothing, so it is stamped read-only.
        files_to_modify=[],
        deliverable="report",
        report_path=str(report_path),
        project_dir=str(project_dir),
    )


def _recorded_errors(agent) -> list:
    return [c.args[1] for c in agent.retry_manager.record_attempt.call_args_list]


def test_a_delivered_report_is_not_an_empty_diff(tmp_path):
    """The core case: file on disk, no repo change → COMPLETED.

    Pre-fix this hit the empty-diff gate and retried. The deliverable
    existed and was non-empty the whole time; the gate was asking about
    a different file.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text("# 裁定\n可行。\n", encoding="utf-8")

    agent = _executor_agent(project)
    task = _report_task(report, project)

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True
    assert (task.id, "completed") in [
        c.args[:2] for c in agent.task_manager.update_task_status.call_args_list
    ]
    assert not any(
        e.startswith("empty_diff_no_changes") for e in _recorded_errors(agent)
    ), (
        "a delivered report was classified as an empty diff: "
        f"{_recorded_errors(agent)!r}"
    )


def test_a_delivered_report_produces_no_commit(tmp_path):
    """The report is under TMP — committing it would be committing scratch.

    ``git_manager.commit`` is ``git add -A`` + ``index.commit``, and
    with nothing staged it does NOT raise — it writes a commit whose
    tree equals its parent's (measured: two commits, one tree). So this
    is not cosmetic: a report task that reached the commit call would
    append an empty checkpoint commit to the PROJECT's history, and
    ``_commit_task_changes`` would write that commit's SHA back as the
    task's ``commit_sha``.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text("# 裁定\n可行。\n", encoding="utf-8")

    agent = _executor_agent(project)
    task = _report_task(report, project)

    assert agent._execute_task_with_retry(task, max_retries=1) is True

    agent._commit_task_changes.assert_not_called()
    agent.git_manager.commit.assert_not_called()


def test_the_delivered_report_path_is_recorded(tmp_path):
    """A completed task has to say WHERE its artifact is.

    Without the path, a reader of the completion log — or of the plan
    state — sees a completed task and has to go hunting for the file.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text("# 裁定\n可行。\n", encoding="utf-8")

    agent = _executor_agent(project)
    task = _report_task(report, project)

    assert agent._execute_task_with_retry(task, max_retries=1) is True

    completed = [
        c for c in agent.logger.info.call_args_list
        if c.args and c.args[0] == "task_completed"
    ]
    assert completed, "the task completed without a task_completed event"
    data = completed[0].kwargs["data"]
    assert data["report_path"] == str(report.resolve())

    assert (
        agent.task_manager.runtime_overrides[task.id]["report_path"]
        == str(report.resolve())
    ), "the plan-state mirror must carry the deliverable's address too"


def test_a_missing_report_is_a_failure_that_names_the_path(tmp_path):
    """No report ⇒ the one thing the task owed is not there ⇒ fail.

    The message has to carry the path, because the retry hint is the
    only channel the sub-agent gets. The generic empty-diff hint
    ("modify the files in files_to_modify") points at a list a report
    task does not have.
    """
    project = tmp_path / "project"
    project.mkdir()
    missing = tmp_path / "never-written.md"

    agent = _executor_agent(project)
    task = _report_task(missing, project)

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is False
    errors = _recorded_errors(agent)
    assert errors, "a report task that wrote nothing recorded no attempt"
    assert errors[0] == (
        f"report file missing or empty at {missing.resolve()}; "
        f"use the Write tool to write the report to exactly that path"
    ), (
        "the failure must name the path AND say what to do about it, "
        f"got {errors[0]!r}"
    )
    assert not any(
        (task.id, "completed") == c.args[:2]
        for c in agent.task_manager.update_task_status.call_args_list
    ), "a task that delivered nothing must never be marked completed"


def test_an_empty_report_file_is_also_a_failure(tmp_path):
    """Zero bytes is not a deliverable.

    It is the Write tool's output when the sub-agent opened the file
    and never filled it. Accepting one marks the task completed with
    nothing in it — the exact shape this feature exists to end.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text("", encoding="utf-8")

    agent = _executor_agent(project)
    task = _report_task(report, project)

    assert agent._execute_task_with_retry(task, max_retries=1) is False
    assert _recorded_errors(agent)[0].startswith("report file missing or empty at ")


def test_a_relative_report_path_resolves_against_project_dir(tmp_path):
    """``report_path`` may be relative; it is anchored at the project dir."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "out").mkdir()
    (project / "out" / "verdict.md").write_text("ok\n", encoding="utf-8")

    agent = _executor_agent(project)
    task = _report_task(Path("out/verdict.md"), project)

    assert agent._execute_task_with_retry(task, max_retries=1) is True
    assert not any(
        e.startswith("report file missing") for e in _recorded_errors(agent)
    )


def test_a_code_task_keeps_the_old_empty_diff_verdict(tmp_path):
    """Negative control for the whole section.

    Same executor, same empty diff, same missing ``files_to_modify`` —
    only ``deliverable`` differs. Without this, a dispatch that routed
    EVERY task by deliverable (or that dropped the empty-diff gate)
    would still satisfy every assertion above.
    """
    project = tmp_path / "project"
    project.mkdir()

    agent = _executor_agent(project)
    task = SubTask(
        id="9-2", title="实现 X", description="写代码",
        test_command="echo ok", files_to_modify=["a.py"],
    )

    assert agent._execute_task_with_retry(task, max_retries=1) is False
    assert _recorded_errors(agent)[0].startswith("empty_diff_no_changes:")


# ---------------------------------------------------------------------------
# 3 — the second pass reviews the FILE
# ---------------------------------------------------------------------------

def _audit_agent(project_dir: Path):
    from agent import AutonomousAgent

    a = AutonomousAgent.__new__(AutonomousAgent)
    a.project_dir = Path(project_dir)
    a.logger = MagicMock()
    a.coding_tool = MagicMock()
    a.coding_tool.query.return_value = "AUDIT_VERDICT: PASSED"
    return a


def test_the_second_pass_reviews_the_report_not_the_chat_reply(tmp_path):
    """The artifact is the answer; the chat reply is not.

    For a report task the reply is typically "done, report written to
    <path>" — a reviewer handed that has nothing to judge, which is how
    a report task's verdict came to rest on its own summary.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text(
        "证据：src/a.py:42 仍无重试；src/b.py:11 捕获后丢弃。\n裁定：不可行。\n",
        encoding="utf-8",
    )

    agent = _audit_agent(project)
    task = _report_task(report, project)

    passed, _ = agent._audit_task_second_pass(
        task, "报告已写好，见 report.md。"
    )

    assert passed is True
    prompt = agent.coding_tool.query.call_args.args[0]
    assert "src/a.py:42" in prompt, (
        "the reviewer never saw the report's evidence"
    )
    assert "报告已写好" not in prompt, (
        "the reviewer is still being handed the chat reply instead of "
        "the deliverable"
    )


def test_a_long_report_is_bounded_and_marked(tmp_path):
    """Unbounded input is not free — and a silent cut reads as a short file."""
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text("A" * 25000, encoding="utf-8")

    agent = _audit_agent(project)
    task = _report_task(report, project)

    agent._audit_task_second_pass(task, "chat reply")

    prompt = agent.coding_tool.query.call_args.args[0]
    assert "A" * 20000 in prompt
    assert "...[truncated]" in prompt
    assert "A" * 20001 not in prompt


def test_a_missing_report_fails_the_second_pass(tmp_path):
    """Missing file ⇒ FAILED, not "inconclusive", not a fallback to the reply.

    Silently falling back to the chat text here is what produced the
    original defect: a task marked completed whose deliverable exists
    nowhere.
    """
    project = tmp_path / "project"
    project.mkdir()
    missing = tmp_path / "gone.md"

    agent = _audit_agent(project)
    task = _report_task(missing, project)

    passed, reason = agent._audit_task_second_pass(task, "I did the analysis.")

    assert passed is False
    assert str(missing.resolve()) in reason
    agent.coding_tool.query.assert_not_called(), (
        "the reviewer was invoked on a task whose deliverable does not "
        "exist — that is the silent fallback"
    )


def test_a_report_task_is_reviewed_even_with_a_passing_command(tmp_path):
    """A passing test_command must not exempt a report task.

    Its deliverable is prose; the thing that can be wrong is the prose.
    A command that passes on an unrelated assertion would otherwise
    carry the whole verdict.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "report.md"
    report.write_text("结论如下。\n", encoding="utf-8")

    agent = _audit_agent(project)
    task = _report_task(report, project)

    # A plain, non-audit-looking, command-bearing task would short-circuit.
    task.title = "重构 src/x.py 的缓存层"
    task.description = "实现新的缓存层"
    task.verification_only = False

    passed, _ = agent._audit_task_second_pass(task, "chat reply")
    assert passed is True
    agent.coding_tool.query.assert_called_once()


# ---------------------------------------------------------------------------
# 4 — the write-time guard
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    from coding_tool import _ensure_guard_hook_path

    project = tmp_path / "project"
    plans = tmp_path / "plans"
    project.mkdir()
    plans.mkdir()
    return {
        "project": project,
        "plans": plans,
        "script": _ensure_guard_hook_path(),
        "cli": _BACKEND_DIR / "file_lock_cli.py",
        "scratch": Path(tempfile.gettempdir()),
    }


def _run_hook(workspace, target, *, read_only=True, report_path=None):
    """Run the REAL materialised guard script for one Edit/Write.

    ``(exit_code, stderr)``. The env is built by hand because the
    contract under test IS an exit code produced by a separate process
    reading these variables — so the only honest way to check it is to
    make that process produce it.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "PDT_PROJECT_DIR": str(workspace["project"]),
        "PDT_PLANS_DIR": str(workspace["plans"]),
        "PDT_FORMAL_REPO_PATH": str(workspace["project"]),
        "PDT_LOCK_CLI": str(workspace["cli"]),
    }
    if read_only:
        env["PDT_READONLY_TASK"] = "1"
    if report_path is not None:
        env["PDT_REPORT_PATH"] = str(report_path)

    proc = subprocess.run(
        [sys.executable, str(workspace["script"])],
        input=json.dumps({"tool_input": {"file_path": str(target)}}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc.returncode, proc.stderr


def test_the_report_file_itself_is_writable(workspace):
    """The deliverable Write is allowed — even where containment would not.

    The carve-out is placed BEFORE the containment refusal, and this is
    what pins that ordering: a path that is neither inside the project
    dir, nor the plans dir, nor scratch space is refused by
    containment on its own. The report is allowed anyway because it was
    named, which is the only reason it is allowed at all.
    """
    # Never created on disk: the guard string-compares and exits before
    # it looks at the filesystem, so naming a path is enough.
    target = Path.home() / ".pdt-report-deliverable-probe.md"
    code, stderr = _run_hook(
        workspace, target, read_only=True, report_path=target,
    )
    assert code == 0, stderr


def test_a_second_file_is_still_refused(workspace):
    """One file, not TMP.

    A prefix match would hand the read-only exemption to everything
    under the scratch dir — the same hole as the directory declaration
    this guard already refuses.
    """
    report = workspace["scratch"] / "pdt-report-a.md"
    other = workspace["scratch"] / "pdt-report-b.md"
    code, stderr = _run_hook(
        workspace, other, read_only=True, report_path=report,
    )
    assert code == 2, stderr
    assert "files_to_modify" in stderr


def test_the_report_directory_is_not_a_carveout(workspace):
    """The carve-out is a FILE, never a directory prefix."""
    d = workspace["scratch"] / "pdt-report-dir-probe"
    d.mkdir(exist_ok=True)
    try:
        inside = d / "anything.md"
        code, stderr = _run_hook(
            workspace, inside, read_only=True, report_path=d,
        )
        assert code == 2, stderr
    finally:
        try:
            (d / "anything.md").unlink()
        except OSError:
            pass
        try:
            d.rmdir()
        except OSError:
            pass


def test_an_ordinary_in_project_file_is_still_refused(workspace):
    """Negative control: the stamp must not leak onto the project's files."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "app.py",
        read_only=True, report_path=workspace["scratch"] / "pdt-report-c.md",
    )
    assert code == 2, stderr
    assert "files_to_modify" in stderr


def test_a_readonly_task_without_a_report_still_refuses_everything(workspace):
    """No carve-out issued ⇒ the pre-existing invariant is untouched."""
    scratch = workspace["scratch"] / "pdt-report-absent.md"
    code, stderr = _run_hook(
        workspace, scratch, read_only=True, report_path=None,
    )
    assert code == 2, stderr
    assert "files_to_modify" in stderr

    code, stderr = _run_hook(workspace, workspace["plans"] / "n.md")
    assert code == 2, stderr


def test_a_report_task_with_no_stamp_is_still_refused(workspace):
    """``PDT_REPORT_PATH`` absent must not fall open.

    The stamp is per task; a stale value inherited from a previous task
    would otherwise grant its carve-out to this one.
    """
    target = workspace["scratch"] / "pdt-report-nostamp.md"
    code, stderr = _run_hook(workspace, target, read_only=True)
    assert code == 2, stderr


# ---------------------------------------------------------------------------
# 5 — the seam: task → context → PDT_REPORT_PATH
# ---------------------------------------------------------------------------

def test_the_executor_binds_the_resolved_report_path(tmp_path, monkeypatch):
    """The production binding, driven through the real method.

    ``_run_task_with_provider_slot`` is where a task becomes an
    execution. ``plan_usage_context`` is what the spawned CLI's settings
    assembly reads, and it runs in ANOTHER process with no access to
    this object — so if the value is not bound here, it cannot be bound
    anywhere, and every guard test above would be testing a variable no
    production code ever sets.
    """
    import asyncio
    import contextlib
    import subprocess as _sp

    import agent as agent_module
    from usage_registry import plan_usage_context

    recorded: list = []

    @contextlib.contextmanager
    def _spy(**kwargs):
        recorded.append(kwargs)
        with plan_usage_context(**kwargs):
            yield

    monkeypatch.setattr(agent_module, "plan_usage_context", _spy)

    class _Controller:
        async def acquire(self, provider):
            return None

        def release(self, provider):
            return None

    project = tmp_path / "project"
    project.mkdir()
    _sp.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project), capture_output=True, check=True,
    )

    a = agent_module.AutonomousAgent(
        requirement=None, project_dir=project, coding_tool=MagicMock(),
    )
    a._execute_task_with_retry = MagicMock(return_value=True)

    loop = asyncio.new_event_loop()
    try:
        # a) report task, absolute path
        recorded.clear()
        report = tmp_path / "r.md"
        task = SubTask(
            id="1", title="t", description="d",
            files_to_modify=[], deliverable="report",
            report_path=str(report),
        )
        loop.run_until_complete(
            a._run_task_with_provider_slot(task, _Controller(), None)
        )
        assert recorded[0]["task_report_path"] == str(report.resolve())

        # b) report task, relative path — resolved, not passed through
        recorded.clear()
        task = SubTask(
            id="2", title="t", description="d",
            files_to_modify=[], deliverable="report",
            report_path="out/r.md", project_dir=str(project),
        )
        loop.run_until_complete(
            a._run_task_with_provider_slot(task, _Controller(), None)
        )
        assert recorded[0]["task_report_path"] == str(
            (project / "out" / "r.md").resolve()
        )

        # c) code task — NO carve-out. This is the one that keeps the
        #    guard's exemption from becoming general.
        recorded.clear()
        task = SubTask(
            id="3", title="t", description="d", files_to_modify=["a.py"],
        )
        loop.run_until_complete(
            a._run_task_with_provider_slot(task, _Controller(), None)
        )
        assert recorded[0]["task_report_path"] is None
    finally:
        loop.close()


def test_the_seam_stamps_only_a_bound_report_task():
    """``_stamp_report_path`` is the only writer of the env var.

    Driven through the real context binding, not by handing the answer
    in — the thing that can be wrong is the pair of them together.
    """
    from coding_tool import _stamp_report_path
    from usage_registry import plan_usage_context

    def _settings() -> dict:
        return {"env": {}}

    with plan_usage_context(task_id="7", task_read_only=True,
                            task_report_path="/tmp/r.md"):
        s = _settings()
        _stamp_report_path(s, "7")
    assert s["env"]["PDT_REPORT_PATH"] == "/tmp/r.md"

    # code task: the key must be ABSENT, not blank.
    with plan_usage_context(task_id="8", task_read_only=False):
        s = _settings()
        _stamp_report_path(s, "8")
    assert "PDT_REPORT_PATH" not in s["env"]

    # no task bound at all — the refiner's own queries must be unaffected
    s = _settings()
    _stamp_report_path(s, None)
    assert "PDT_REPORT_PATH" not in s["env"]


def test_the_env_var_is_the_one_the_guard_reads():
    """A rename on either side would leave both sides green.

    The stamp test asserts a dict key; the hook tests assert a
    behaviour driven by an env var this test sets by hand. Only this
    ties the two names together.
    """
    import inspect

    from coding_tool import (
        _EDIT_WRITE_FORMAL_REPO_GUARD_SRC,
        _stamp_report_path,
    )

    assert '"PDT_REPORT_PATH"' in _EDIT_WRITE_FORMAL_REPO_GUARD_SRC
    assert "PDT_REPORT_PATH" in inspect.getsource(_stamp_report_path)


# ---------------------------------------------------------------------------
# 6 — the plan file round-trip
# ---------------------------------------------------------------------------
#
# The model round-trip above (section 1) is not enough: ``model_dump``
# and ``load_tasks`` filter through DIFFERENT field lists. ``save_tasks``
# writes whatever the dump produced; ``load_tasks`` keeps only the names
# in ``TaskManager._STATIC_TASK_FIELDS``. A deliverable field missing
# from that second list is written to ``tasks.json`` and then dropped on
# the way back in — so a report task survives exactly zero save/load
# cycles, and the executor (which loads the plan from disk) would never
# see one.


def _plan_file(tmp_path):
    return tmp_path / "tasks.json"


def _write_plan(tmp_path, rows):
    path = _plan_file(tmp_path)
    path.write_text(
        json.dumps({"requirement": "r", "tasks": rows}), encoding="utf-8"
    )
    return path


def test_a_report_task_survives_save_then_load(tmp_path):
    """The deliverable a plan file carries must come back as itself.

    ``save_tasks`` already writes ``deliverable`` / ``report_path`` for a
    report task. ``load_tasks`` filtered by ``_STATIC_TASK_FIELDS``,
    which named neither, so the row came back as a code task with no
    report path — indistinguishable from a task that was never meant to
    produce a file, and dispatched to the empty-diff verdict that ends
    in failure.
    """
    from task_manager import TaskManager

    path = _plan_file(tmp_path)
    manager = TaskManager(tmp_path, tasks_file=path)
    manager.requirement = "r"
    manager.tasks = [
        SubTask(id="1", title="a", description="d",
                deliverable="report", report_path="/tmp/report.md"),
    ]
    manager.save_tasks()

    on_disk = json.loads(path.read_text(encoding="utf-8"))["tasks"][0]
    assert on_disk["deliverable"] == "report"
    assert on_disk["report_path"] == "/tmp/report.md"

    reloaded = TaskManager(tmp_path, tasks_file=path)
    assert len(reloaded.tasks) == 1
    assert reloaded.tasks[0].deliverable == "report"
    assert reloaded.tasks[0].report_path == "/tmp/report.md"


def test_a_code_task_keeps_the_two_keys_off_disk(tmp_path):
    """The other half: the fix must not put defaults into every row.

    Adding the names to the load filter also adds them to what a plan
    file is allowed to carry. If the dump started emitting them, every
    existing plan would be rewritten with two explicit defaults on every
    task — a diff on every file in ``plans/`` for a change to two rows
    that need them.
    """
    from task_manager import TaskManager

    path = _plan_file(tmp_path)
    manager = TaskManager(tmp_path, tasks_file=path)
    manager.requirement = "r"
    manager.tasks = [SubTask(id="1", title="a", description="d")]
    manager.save_tasks()

    on_disk = json.loads(path.read_text(encoding="utf-8"))["tasks"][0]
    assert "deliverable" not in on_disk
    assert "report_path" not in on_disk

    reloaded = TaskManager(tmp_path, tasks_file=path)
    assert reloaded.tasks[0].deliverable == "code"
    assert reloaded.tasks[0].report_path is None


def test_the_two_field_lists_agree_on_the_deliverable_fields():
    """A name the dump writes must be a name the loader keeps.

    The two lists are edited separately and nothing else ties them
    together: ``save_tasks`` filters through ``model_dump``'s opt-in
    serialisation and ``load_tasks`` through ``_STATIC_TASK_FIELDS``.
    This pins the pairing so a future field cannot be added to one side
    only — which is precisely how a report task came back as a code
    task.
    """
    import inspect

    from task import SubTask as _SubTask
    from task_manager import TaskManager

    dump_src = inspect.getsource(_SubTask.model_dump)
    static = TaskManager._STATIC_TASK_FIELDS
    for field in ("deliverable", "report_path"):
        assert field in static, f"{field} is dropped on load"
        assert f'"{field}"' in dump_src, f"{field} is never written"


# ---------------------------------------------------------------------------
# 7 — the EXECUTOR's own loader
# ---------------------------------------------------------------------------
#
# Section 6 pins ``TaskManager.load_tasks``. It is not the only reader:
# ``AutonomousAgent._load_tasks`` re-reads ``tasks.json`` through its own
# ``_TASK_FIELDS`` allow-list (agent.py), and that is the one the executor
# actually runs on — ``run()`` → ``asyncio.run(self._run_async())`` →
# ``_load_tasks()``. PR #5 added the two names to ``_STATIC_TASK_FIELDS``
# and left this second list alone, so a plan file could carry the
# deliverable and the executor still would not see it.
#
# The failure was silent and self-concealing. ``is_report_task`` reads
# ``getattr(task, "deliverable", "code")``, so a dropped field does not
# raise — it yields ``"code"`` and the task takes the ordinary
# empty-diff path. On a live plan (20261015-perpetual-tests-perp,
# 2026-10-19) task ``37-8-3-4-1`` therefore completed with
# ``"report_path": null`` and ``changed_files`` pointing at a markdown
# file written into the PROJECT repo: it passed the empty-diff check by
# producing an unwanted commit, and the report mechanism never ran.


class _DummyCodingTool:
    """Only ``AutonomousAgent.__init__``'s signature is consumed — these
    tests stop at the load, so no LLM call is ever made."""

    def __init__(self, *args, **kwargs):
        pass


def _load_agent_for(tmp_path, monkeypatch, rows):
    """Build an agent over a real ``tasks.json`` and return its loaded tasks.

    This drives the production entry: the agent reads the plan through
    the same ``TaskRepository`` / ``_TASK_FIELDS`` path the executor
    does. ``PDT_STATE_DB_PATH`` is repointed so Phase 2's orphan
    reconcile cannot merge in rows from the canonical repo-root
    ``state.db`` — ``plan_id`` is derived from the tasks.json parent
    dir, so a real plan sharing this repo could otherwise leak into
    the assertion.
    """
    from agent import AutonomousAgent

    monkeypatch.setenv(
        "PDT_STATE_DB_PATH", str(tmp_path / "test_state.db")
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    # GitManager binds with search_parent_directories=True; without a
    # repo here it would walk up to this checkout.
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )

    (project_dir / "tasks.json").write_text(
        json.dumps({"requirement": "deliverable load", "tasks": rows}),
        encoding="utf-8",
    )

    agent = AutonomousAgent(
        requirement="deliverable load",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
    )
    return agent, agent._load_tasks()


def test_a_report_task_loaded_by_the_executor_is_still_a_report_task(
    tmp_path, monkeypatch
):
    """The executor's loader must not degrade a report task to a code task.

    Asserted through ``is_report_task`` — the predicate the executor's
    dispatch actually branches on — rather than through the attribute
    alone, because the attribute can be present while a consumer reads
    it through a different path. Before the fix this loaded a perfectly
    well-formed ``SubTask`` whose ``deliverable`` was the ``"code"``
    default, and ``is_report_task`` answered ``False``.
    """
    from agent import is_report_task
    from task import NO_FILE_CHANGES_SENTINEL

    report_path = "/tmp/pdt-perp-reports/37-8-3-4-1.md"
    _, loaded = _load_agent_for(
        tmp_path, monkeypatch,
        [{
            "id": "37-8-3-4-1",
            "title": "Write the CI attribution report",
            "description": "d",
            "test_command": "echo ok",
            "status": "pending",
            "depends_on": [],
            # A report task is read-only: its one deliverable lives
            # outside the project, so the model must modify nothing
            # in-repo. (That is exactly how the degraded run got a
            # stray markdown file committed under reports/ci/ — it
            # had no report path to write and wrote here instead.)
            "files_to_modify": list(NO_FILE_CHANGES_SENTINEL),
            "deliverable": "report",
            "report_path": report_path,
        }],
    )

    assert len(loaded) == 1
    task = loaded[0]
    assert task.deliverable == "report"
    assert task.report_path == report_path
    assert is_report_task(task) is True


def test_a_code_task_loaded_by_the_executor_keeps_the_defaults(
    tmp_path, monkeypatch
):
    """The other half: adding the names must not invent a deliverable.

    A plan written before 2026-10-19 carries neither key. Widening an
    allow-list cannot manufacture a value, but it must be pinned: a
    loader that defaulted ``deliverable`` to ``"report"``, or hydrated a
    stray ``report_path`` onto a code task, would silently send every
    ordinary task down the report branch.
    """
    from agent import is_report_task

    _, loaded = _load_agent_for(
        tmp_path, monkeypatch,
        [{
            "id": "1",
            "title": "an ordinary code task",
            "description": "d",
            "test_command": "echo ok",
            "status": "pending",
            "depends_on": [],
            "files_to_modify": ["src/a.py"],
        }],
    )

    assert len(loaded) == 1
    task = loaded[0]
    assert task.deliverable == "code"
    assert task.report_path is None
    assert is_report_task(task) is False


def test_the_two_loaders_agree_on_the_deliverable_fields():
    """Both allow-lists must name the same deliverable fields.

    Two independent lists, edited separately, with nothing tying them
    together: that is the whole bug. Section 6 pins the
    ``_STATIC_TASK_FIELDS`` side; this pins the executor's
    ``_TASK_FIELDS``, which lives inside a function body and so cannot
    be imported — it is read out of the source, the same way the
    ``model_dump`` pairing is checked above.

    Comments are stripped before matching. The block above the literal
    explains the failure in prose and necessarily mentions
    ``"deliverable"``; a naive substring search over the raw slice
    finds THAT and passes with the field removed, which is exactly what
    a negative control is for.
    """
    import inspect

    import agent as agent_module

    src = inspect.getsource(agent_module.AutonomousAgent._load_tasks)
    body = src.split("_TASK_FIELDS = {", 1)[1].split("}", 1)[0]
    code_only = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )
    for field in ("deliverable", "report_path"):
        assert f'"{field}"' in code_only, (
            f"{field} is dropped by _load_tasks, so a report task "
            f"degrades to a code task on the executor's path"
        )


# ---------------------------------------------------------------------------
# 8 — the FIRST prompt must carry the report's address
# ---------------------------------------------------------------------------
#
# The report path reached the sub-agent by exactly one channel:
# ``PDT_REPORT_PATH`` stamped into the spawned CLI's settings by
# ``coding_tool``. The prompt never named it. So a sub-agent had no way
# to know the file existed until the retry hint told it — and the retry
# hint arrives on the LAST attempt, which is the attempt after the one
# that was supposed to succeed. Task 37-8-3-5-3 failed that way.
#
# This section pins the fix at the production entry: the real
# ``_execute_task_with_retry``, with the LLM call stubbed. The prompt
# is read off ``coding_tool.query``'s first positional argument, which
# is the assembled ``context`` — the same string the sub-agent receives.


def _prompt_sent_to_subagent(agent) -> str:
    """The ``context`` string handed to the sub-agent on the first ask.

    ``query_in_session(tool, session, context, system_instruction=...)``
    forwards positionally to ``tool.query``, so the assembled prompt is
    arg 0 of the first call.
    """
    assert agent.coding_tool.query.call_args_list, (
        "the task never asked the sub-agent anything, so no prompt "
        "was assembled — the assertion below would be vacuous"
    )
    return agent.coding_tool.query.call_args_list[0].args[0]


def test_the_first_prompt_names_the_report_file(tmp_path):
    """A report task's FIRST attempt must be told where to write.

    Asserted through the real executor entry, not through a helper:
    the helper could be wired into the prompt while the prompt itself
    stayed silent, and only the assembled ``context`` proves the two
    are actually connected.
    """
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "reports" / "verdict.md"
    report.parent.mkdir()
    report.write_text("# 裁定\n可行。\n", encoding="utf-8")

    agent = _executor_agent(project)
    task = _report_task(report, project)

    assert agent._execute_task_with_retry(task, max_retries=1) is True

    prompt = _prompt_sent_to_subagent(agent)
    assert str(report.resolve()) in prompt, (
        "the first prompt never named the report's absolute path, so "
        "the sub-agent had no address to write to until the retry "
        f"hint — which is usually the last attempt:\n{prompt}"
    )
    assert "交付物" in prompt, (
        "the hint must state that the deliverable is one file rather "
        f"than a code diff:\n{prompt}"
    )
    assert "Write" in prompt, (
        "the hint must name the tool that performs the delivery:\n"
        f"{prompt}"
    )
    assert "二次审查" in prompt, (
        "the hint must say the file is re-read downstream, which is "
        f"what makes an unwritten report a failure:\n{prompt}"
    )


def test_a_code_task_says_nothing_about_a_report(tmp_path):
    """The negative control: an ordinary code task gains no hint.

    Scoped to the same prompt builder, so a hint that leaks onto every
    task would show up here as noise spent on diff-oriented sub-agents.

    The verdict is deliberately not asserted. The prompt is assembled
    and handed to the tool BEFORE any gate runs, so reading it back off
    the first call is both sufficient and stable — asserting on the
    completed-vs-retried outcome would pin this fixture's git stubs
    rather than the hint's reach.
    """
    project = tmp_path / "project"
    project.mkdir()

    agent = _executor_agent(project)
    task = SubTask(
        id="1-1",
        title="an ordinary code task",
        description="改一行代码。",
        test_command="echo ok",
        files_to_modify=["src/a.py"],
        project_dir=str(project),
    )

    agent._execute_task_with_retry(task, max_retries=1)

    prompt = _prompt_sent_to_subagent(agent)
    assert "交付物" not in prompt, (
        "the report hint leaked onto a code task:\n" + prompt
    )
    assert "Write 工具" not in prompt, (
        "a code task was told to Write a report file:\n" + prompt
    )
