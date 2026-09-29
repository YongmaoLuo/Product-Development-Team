"""
No-bulk-context regression tests
================================

Background
----------
The AutonomousAgent's ``_execute_task_with_retry`` used to inject a
repository-wide ``get_file_context()`` snapshot (up to 300,000 chars
of every source file under ``project_dir``) into every prompt
sent to the coding subagent. Likewise the ``TaskRefiner.refine``
prompt accepted a ``file_context`` kwarg that, when non-empty,
appended the same bulk snapshot to every LLM call.

This burned tokens, leaked unrelated code into every prompt, and
made the subagent skip its own on-demand ``Read``/``Grep`` tools.

This suite pins down the post-fix contract:

  1. ``_execute_task_with_retry`` MUST NOT inject
     ``get_file_context()`` output. The only code-context the
     subagent receives is the *hint* derived from
     ``task.files_to_modify`` (a small bulleted list of relative
     paths). When ``files_to_modify`` is the
     ``UNKNOWN_MODIFICATIONS_SENTINEL`` the hint MUST NOT leak the
     sentinel token, and when it is an explicit empty list the hint
     MUST be empty.
  2. ``TaskRefiner.refine`` MUST NOT inject any bulk file context
     to the LLM. The refiner is a re-planner — it works off the
     failed task's response, the test result, and the current task
     list. No bulk repository snapshot.
  3. ``get_file_context()`` is preserved (it still exists as a
     private helper on the agent) but is no longer wired into the
     execution or refiner prompts. The method may be deleted in a
     later cleanup; the contract here is only "not called from
     the execution or refiner path".

These tests build a real ``AutonomousAgent`` against a tmp_path
project dir with a small repo (so ``get_file_context`` would
have plenty of bulk content if it were still being called) and
stub the LLM calls so the assertions can inspect the prompt
content directly.
"""

from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL  # noqa: E402
from config_registry import ConfigRegistry  # noqa: E402


#: Marker the stubbed subagent creates so the fixture task's
#: ``test_command`` fails the pre-flight gate and passes the post-run
#: cross-verify. See :func:`_make_task` / :func:`_capture_coder_prompt`.
_SUBAGENT_MARKER = ".pdt_subagent_ran"


# ---------------------------------------------------------------------------
# Test fixtures: tmp_path project with a handful of plausible files
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


def _seed_repo(project_dir: Path) -> None:
    """Drop a handful of distinct files into the repo so
    ``get_file_context`` would have plenty to leak if it were called.

    The file bodies are deliberately distinctive — each contains a
    sentinel string (``BULK_SENTINEL_FILE_<name>``) that the tests
    can grep for in the captured prompt.
    """
    files = {
        "module_a.py": (
            "# BULK_SENTINEL_FILE_module_a\n"
            "def alpha():\n    return 'alpha-from-module-a'\n"
        ),
        "module_b.py": (
            "# BULK_SENTINEL_FILE_module_b\n"
            "def beta():\n    return 'beta-from-module-b'\n"
        ),
        "subdir/module_c.py": (
            "# BULK_SENTINEL_FILE_module_c\n"
            "def gamma():\n    return 'gamma-from-module-c'\n"
        ),
        "README.md": "# BULK_SENTINEL_FILE_README\nProject overview.\n",
        "tests/test_x.py": "# BULK_SENTINEL_FILE_test_x\n",
    }
    for rel, body in files.items():
        p = project_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


def _build_agent(project_dir: Path, coding_tool: MagicMock) -> AutonomousAgent:
    """Build a real ``AutonomousAgent`` with a stub ``CodingTool`` so we can
    inspect every prompt sent into the coding pipeline.

    The agent's logger is also stubbed to silence log emission during the
    test runs. Other than that we want the *real* agent — the point of
    these tests is to catch any regression that re-wires
    ``get_file_context`` into the prompt path.
    """
    logger = MagicMock()
    tasks_file = project_dir / "tasks.json"
    return AutonomousAgent(
        requirement="test requirement",
        project_dir=project_dir,
        coding_tool=coding_tool,
        config=ConfigRegistry.get('coding'),
        logger=logger,
        tasks_file=tasks_file,
    )


def _make_task(task_id: str, **overrides) -> SubTask:
    """Build a minimal ``SubTask`` for execution-path testing.

    2026-09-14: the default ``test_command`` probes for a marker file
    that only the stubbed subagent creates (see
    :func:`_capture_coder_prompt`). A trivially-passing command such as
    ``echo ok`` would trip the dispatcher's "skip-already-done"
    pre-flight gate (``agent.py:_preflight_test_command_skip``), which
    short-circuits the task and never invokes the coding tool — so the
    prompt these tests exist to inspect would never be built.
    """
    base = {
        "id": task_id,
        "title": f"task {task_id}",
        "description": f"description for {task_id}",
        "test_command": f"test -f {_SUBAGENT_MARKER}",
    }
    base.update(overrides)
    return SubTask(**base)


# ---------------------------------------------------------------------------
# Tests: _execute_task_with_retry must not inject bulk file context
# ---------------------------------------------------------------------------


def _capture_coder_prompt(project_dir: Path, task: SubTask) -> str:
    """Run ``_execute_task_with_retry`` against a stubbed coding tool and
    return the prompt that would have been sent to the LLM.

    The coding tool is stubbed to:
      - return a response that contains ``TEST_RESULT: PASSED`` so the
        loop exits cleanly on the first try.
      - create ``_SUBAGENT_MARKER`` in ``project_dir``, so the task's
        ``test_command`` fails on the pre-flight check (forcing the real
        subagent path these tests inspect) and passes on the post-run
        cross-verify (so the task completes without a retry loop).
      - record the first positional arg of every ``query`` call so the
        test can inspect the exact prompt.
    """
    marker = project_dir / _SUBAGENT_MARKER

    coding_tool = MagicMock()
    coding_tool.query.return_value = "TEST_RESULT: PASSED\nDone."

    def _run(*_args, **_kwargs):
        marker.touch()
        return "TEST_RESULT: PASSED\nDone."

    coding_tool.query.side_effect = _run

    agent = _build_agent(project_dir, coding_tool)

    agent._execute_task_with_retry(task)

    # coding_tool.query is called with (context, system_instruction, ...).
    # The prompt under test is the first positional arg.
    assert coding_tool.query.called, "coding_tool.query was never invoked"
    prompt = coding_tool.query.call_args_list[0][0][0]
    return prompt


def test_execute_task_does_not_inject_get_file_context_snapshot(tmp_path):
    """The prompt must not contain any of the bulk file bodies.

    If ``get_file_context`` were still being called, each of the
    seeded files would appear in the prompt with its
    ``BULK_SENTINEL_FILE_<name>`` marker. This test fails if any
    such marker leaks.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    task = _make_task("1", files_to_modify=["module_a.py"])
    prompt = _capture_coder_prompt(project_dir, task)

    # None of the seeded files' contents should appear in the prompt.
    for sentinel in (
        "BULK_SENTINEL_FILE_module_a",
        "BULK_SENTINEL_FILE_module_b",
        "BULK_SENTINEL_FILE_module_c",
        "BULK_SENTINEL_FILE_README",
        "BULK_SENTINEL_FILE_test_x",
    ):
        assert sentinel not in prompt, (
            f"bulk file content leaked into the coder prompt "
            f"(sentinel={sentinel!r}). get_file_context() must not "
            f"be wired into the execution prompt."
        )


def test_execute_task_does_not_call_get_file_context(tmp_path):
    """``get_file_context`` must not be called during task execution.

    After the dead-code cleanup, the method no longer exists on
    ``AutonomousAgent``. This test pins the post-cleanup contract:
    the agent class MUST NOT expose ``get_file_context`` (so no
    future refactor can accidentally re-add a bulk-context
    snapshot path without the test suite catching it). The
    execution path is verified separately by
    ``test_execute_task_does_not_inject_get_file_context_snapshot``
    (the prompt-content check) and
    ``test_execute_task_prompt_does_not_say_current_codebase``
    (the legacy-header check).
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    coding_tool = MagicMock()
    coding_tool.query.return_value = "TEST_RESULT: PASSED\nDone."
    agent = _build_agent(project_dir, coding_tool)

    assert not hasattr(agent, "get_file_context"), (
        "AutonomousAgent re-introduced a get_file_context() method. "
        "Bulk file-context snapshots are not allowed on the "
        "execution path; remove the method again."
    )


def test_execute_task_prompt_includes_files_to_modify_hint(tmp_path):
    """When ``files_to_modify`` lists real paths, the prompt must
    include those paths as a concise hint.

    This is the *positive* contract: the prompt is no-bulk, but it
    still tells the subagent which files are likely in scope.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    task = _make_task(
        "1",
        files_to_modify=["module_a.py", "subdir/module_c.py"],
    )
    prompt = _capture_coder_prompt(project_dir, task)

    # The relative paths must appear in the prompt somewhere.
    assert "module_a.py" in prompt, (
        "files_to_modify hint missing module_a.py — subagent won't "
        "know which file is in scope without it."
    )
    assert "subdir/module_c.py" in prompt, (
        "files_to_modify hint missing subdir/module_c.py — subagent "
        "won't know which file is in scope without it."
    )


def test_execute_task_prompt_hides_sentinel_in_hint(tmp_path):
    """When ``files_to_modify`` is the unknown-modification sentinel,
    the prompt must NOT leak the sentinel literal
    ``__UNKNOWN_MODIFICATIONS__`` to the subagent.

    The sentinel is an internal bookkeeping device — telling the
    subagent "you may modify unknown files" is meaningless and
    confusing. The prompt should just say "no files declared" or
    equivalent.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    # Default sentinel — SubTask default factory puts it in.
    task = _make_task("1")  # files_to_modify = UNKNOWN_MODIFICATIONS_SENTINEL
    assert task.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL

    prompt = _capture_coder_prompt(project_dir, task)

    assert "__UNKNOWN_MODIFICATIONS__" not in prompt, (
        "sentinel token leaked into the coder prompt. The hint "
        "must use a human-readable phrase (e.g. 'no files "
        "declared') instead."
    )


def test_execute_task_prompt_omits_hint_for_empty_files_to_modify(tmp_path):
    """When ``files_to_modify`` is the explicit empty list ``[]``
    (the "this task is read-only" signal), the prompt must NOT
    emit a files_to_modify hint at all.

    The empty-list case is the audit 2026-08-18 convention: a
    task author who knows the work is read-only writes ``[]`` to
    opt into lightweight micro-layer semantics. Echoing back an
    empty hint is fine; echoing back the sentinel would
    accidentally re-opt the task into the unknown-modification
    pessimism path. This test pins that.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    task = _make_task("1", files_to_modify=[])
    prompt = _capture_coder_prompt(project_dir, task)

    assert "__UNKNOWN_MODIFICATIONS__" not in prompt, (
        "explicit-empty files_to_modify must NOT be substituted "
        "back to the sentinel — that would re-enable the "
        "pessimistic unknown-modifications path."
    )


def test_execute_task_prompt_does_not_say_current_codebase(tmp_path):
    """The prompt must not contain the legacy ``Current codebase:``
    header that used to precede the bulk injection.

    This header was the only consumer of ``get_file_context`` in
    the execution path. Its absence in the post-fix prompt is
    the visible signal that bulk injection is gone.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    task = _make_task("1", files_to_modify=["module_a.py"])
    prompt = _capture_coder_prompt(project_dir, task)

    assert "Current codebase:" not in prompt, (
        "the legacy 'Current codebase:' header is still in the "
        "execution prompt. That header is the bulk-injection "
        "anchor — remove it together with the get_file_context "
        "call."
    )


# ---------------------------------------------------------------------------
# Tests: TaskRefiner.refine must not inject bulk file context
# ---------------------------------------------------------------------------


def test_refine_does_not_inject_bulk_file_context_to_llm(tmp_path):
    """The refiner prompt must not contain any bulk file body even
    when the caller passes a non-empty ``file_context`` argument.

    The refiner is a re-planner — its LLM call should be sized
    like a planning request (requirement + task list + last
    failure), not a repository-wide code dump. If the caller
    passes ``file_context="BULK_SENTINEL_FILE_module_a ..."``
    the refiner must drop it on the floor before sending the
    prompt.
    """
    from refiner import TaskRefiner

    coding_tool = MagicMock()
    coding_tool.query_json.return_value = {"tasks": []}
    refiner = TaskRefiner(coding_tool=coding_tool)

    bulk_blob = (
        "\nFILE: module_a.py\n---\n"
        "# BULK_SENTINEL_FILE_module_a\n"
        "def alpha():\n    return 'alpha'\n---\n"
        "\nFILE: module_b.py\n---\n"
        "# BULK_SENTINEL_FILE_module_b\n"
        "def beta():\n    return 'beta'\n---\n"
    )
    refiner.refine(
        requirement="req",
        tasks=[],
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id="1",
        file_context=bulk_blob,  # MUST be ignored
    )

    assert coding_tool.query_json.called
    prompt = coding_tool.query_json.call_args_list[0][0][0]

    assert "BULK_SENTINEL_FILE_module_a" not in prompt, (
        "refiner injected the bulk file_context blob into its "
        "LLM prompt. The refiner is a planner; it must NOT "
        "accept repository-wide context from the caller."
    )
    assert "BULK_SENTINEL_FILE_module_b" not in prompt, (
        "refiner injected the bulk file_context blob into its "
        "LLM prompt (second file). The refiner is a planner; "
        "it must NOT accept repository-wide context from the "
        "caller."
    )
    assert "Current codebase" not in prompt, (
        "refiner prompt contains the legacy 'Current codebase' "
        "header. That header is the bulk-injection anchor — "
        "remove it together with the file_context handling."
    )


def test_refine_does_not_inject_empty_file_context_section(tmp_path):
    """When ``file_context`` is the empty string (the realistic
    post-fix path), the refiner prompt must not contain a
    ``Current codebase (truncated):`` section at all.

    This is the cleaner version of the previous test: even an
    empty file_context should not cause the refiner to emit the
    legacy section header.
    """
    from refiner import TaskRefiner

    coding_tool = MagicMock()
    coding_tool.query_json.return_value = {"tasks": []}
    refiner = TaskRefiner(coding_tool=coding_tool)

    refiner.refine(
        requirement="req",
        tasks=[],
        last_coder_response="resp",
        last_result="result",
        exit_code=1,
        last_task_id="1",
        file_context="",
    )

    prompt = coding_tool.query_json.call_args_list[0][0][0]

    assert "Current codebase" not in prompt
    assert "truncated" not in prompt


# ---------------------------------------------------------------------------
# Tests: agent._refine_after_failure must pass empty file_context
# ---------------------------------------------------------------------------


def test_refine_after_failure_passes_empty_file_context(tmp_path):
    """``_refine_after_failure`` is the bridge between the agent's
    execution loop and the refiner. After the no-bulk-context fix,
    it must pass ``file_context=""`` to the refiner regardless of
    whatever bulk snapshot the executor may still hold locally.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _seed_repo(project_dir)

    coding_tool = MagicMock()
    coding_tool.query.return_value = "TEST_RESULT: PASSED\nDone."
    coding_tool.query_json.return_value = {
        "tasks": [
            {
                "id": "1",
                "title": "fixed",
                "description": "fixed",
                "test_command": "echo ok",
                "depends_on": [],
            }
        ]
    }
    agent = _build_agent(project_dir, coding_tool)

    # Stub _refine_after_failure's downstream writes so the test
    # doesn't have to satisfy persistence rules.
    agent.task_manager.set_tasks(
        [
            {
                "id": "1",
                "title": "broken",
                "description": "broken",
                "test_command": "echo ok",
                "depends_on": [],
                "status": "pending",
            }
        ],
        requirement="req",
    )

    captured_kwargs = {}

    def spy_refine(*args, **kwargs):
        captured_kwargs.update(kwargs)
        # Return a minimal task list so the caller doesn't choke
        # on persistence.
        return kwargs["tasks"]

    agent.refiner.refine = spy_refine

    task = _make_task("1")
    # Pass a deliberately polluted bulk context — the bridge must
    # scrub it before calling the refiner.
    agent._refine_after_failure(
        task=task,
        coder_response="resp",
        result_context="result",
        file_context=(
            "FILE: module_a.py\n---\n"
            "# BULK_SENTINEL_FILE_module_a\n---\n"
        ),
        exit_code=1,
    )

    assert "file_context" in captured_kwargs, (
        "_refine_after_failure did not forward file_context to "
        "the refiner at all. The bridge must explicitly pass "
        "file_context=\"\"."
    )
    assert captured_kwargs["file_context"] == "", (
        f"_refine_after_failure forwarded a non-empty "
        f"file_context ({len(captured_kwargs['file_context'])} "
        f"chars) to the refiner. The bulk-context contract "
        f"requires this to be empty."
    )