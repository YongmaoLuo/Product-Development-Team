"""
Interpreter-contract tests for ``test_command_quality``.

Why this file exists (2026-09-21, a production plan
a production plan): the task generator emitted
``uv run pytest tests/...`` for tasks targeting ``the project``, which
ships its own ``venv1/``. Everything downstream looked fine —

  * the prompt already carried a "路径一致性" rule, but nothing about
    *which interpreter*;
  * ``TasksGenerator._needs_venv`` treated ``uv run`` as "the env is
    already handled" and skipped venv injection entirely;
  * no validator looked at interpreter identity at all.

...so the defect shipped silently. ``uv`` resolves its own environment,
which need not be the project's venv; the subagent and the framework
framework's independent re-run can then disagree about whether the
command passes — the classic test-report mismatch.

Two defences are pinned here:

  1. ``find_foreign_env_runner`` — context-free detection of the runner.
  2. ``find_interpreter_mismatch`` — the same check, but only reported
     when the project actually owns a virtualenv (a repo with no venv
     may legitimately use ``uv run``; reporting it there would be noise).

The last test is the corpus test, and it is the one that matters: it
runs the detector over the *real* commands this plan generated, so a
regression in the detector shows up as a wrong verdict on real input
rather than on a hand-written string.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from test_command_quality import (  # noqa: E402
    FOREIGN_ENV_RUNNERS,
    find_foreign_env_runner,
    find_interpreter_mismatch,
)
from tasks_generator import TASKS_SYSTEM_PROMPT, _workspace_line  # noqa: E402


# ---------------------------------------------------------------------------
# find_foreign_env_runner — context-free
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runner", FOREIGN_ENV_RUNNERS)
def test_every_declared_runner_is_detected(runner):
    """Each name in the table must actually fire.

    A table whose entries are never matched is dead weight — the
    detection would look wired up while covering nothing.
    """
    issue = find_foreign_env_runner(f"{runner} pytest tests/x.py -v")
    assert issue is not None, f"{runner!r} is declared but not detected"
    assert issue.code == "FOREIGN_PY_ENV_RUNNER"


def test_uv_run_pytest_is_flagged():
    issue = find_foreign_env_runner("uv run pytest tests/integration/test_x.py -v")
    assert issue is not None
    # The message must tell the operator what to do instead, not just
    # that something is wrong.
    assert "bin/pytest" in issue.detail
    assert "test-report mismatch" in issue.detail


def test_uv_run_as_mid_chain_link_is_flagged():
    """The runner is not always at the head of the command."""
    issue = find_foreign_env_runner("cd frontend && uv run pytest tests/x.py")
    assert issue is not None


def test_venv_binary_path_is_clean():
    """The fix we recommend must not itself be flagged."""
    assert find_foreign_env_runner(
        "/Users/me/proj/venv1/bin/pytest tests/integration/test_x.py -v"
    ) is None


def test_cargo_and_node_commands_are_clean():
    assert find_foreign_env_runner("cd native_ext && cargo test --test metric_segmentation") is None
    assert find_foreign_env_runner(
        "cd frontend-app && npx playwright test tests/e2e/metric-panel.spec.ts"
    ) is None


def test_plain_pytest_is_clean():
    """A bare ``pytest`` is the venv-injection path's problem, not this one."""
    assert find_foreign_env_runner("pytest tests/unit/test_x.py -v") is None


def test_runner_name_inside_quotes_is_not_flagged():
    """A test *named* after uv must not trip the detector."""
    assert find_foreign_env_runner('pytest -k "uv_run_pytest" tests/x.py') is None


# ---------------------------------------------------------------------------
# find_interpreter_mismatch — project-aware
# ---------------------------------------------------------------------------

def _task(*commands):
    return {"id": "1", "test_commands": list(commands)}


def test_no_venv_means_foreign_runner_is_legitimate():
    """A project with no virtualenv of its own may use uv. Stay quiet."""
    task = _task("uv run pytest tests/x.py -v")
    assert find_interpreter_mismatch(task, None) == []
    assert find_interpreter_mismatch(task, "") == []


def test_project_venv_makes_foreign_runner_a_defect():
    task = _task("uv run pytest tests/x.py -v")
    found = find_interpreter_mismatch(task, "/Users/me/proj/venv1/bin/activate")
    assert len(found) == 1
    command, issue = found[0]
    assert command == "uv run pytest tests/x.py -v"
    assert issue.code == "FOREIGN_PY_ENV_RUNNER"


def test_project_venv_leaves_correct_commands_alone():
    task = _task(
        "/Users/me/proj/venv1/bin/pytest tests/x.py -v",
        "cd native_ext && cargo test --test metric_segmentation",
    )
    assert find_interpreter_mismatch(task, "/Users/me/proj/venv1/bin/activate") == []


# ---------------------------------------------------------------------------
# Corpus — the real commands this plan generated
# ---------------------------------------------------------------------------

#: Commands the generator emitted for a production plan,
#: verbatim. Six used a foreign runner; the rest were already right.
#: Flipping any of these verdicts is a regression.
_CORPUS = {
    "uv run pytest tests/integration/test_metric_boundary_parity.py -v": True,
    "uv run pytest tests/integration/test_no_duplicate_outputs.py -v": True,
    "uv run pytest tests/api/test_api_contract.py -v": True,
    "uv run pytest tests/regression/test_example_baseline.py -v": True,
    "uv run pytest tests/api/test_api_input_robustness.py -v": True,
    'uv run python -c "import json; d=json.load(open(\'tests/fixtures/x.json\'))"': True,
    "the production checkout/venv1/bin/pytest tests/api/test_api_contract.py -v": False,
    "cd native_ext && cargo test --test metric_segmentation": False,
    "cd native_ext && cargo test --test perf_scaling -- --include-ignored": False,
    "cd frontend-app && npx playwright test tests/e2e/metric-panel.spec.ts": False,
    "test -s native_ext/docs/audit-0f0f0f0f.md && grep -q 处理结果 native_ext/docs/audit-0f0f0f0f.md": False,
}


@pytest.mark.parametrize("command,expected_flagged", sorted(_CORPUS.items()))
def test_corpus_verdicts(command, expected_flagged):
    flagged = find_foreign_env_runner(command) is not None
    assert flagged is expected_flagged, (
        f"{command!r}: expected flagged={expected_flagged}, got {flagged}"
    )


def test_corpus_flags_exactly_the_six_foreign_runner_commands():
    """Aggregate check: the corpus contains six defects, no more, no fewer."""
    task = _task(*_CORPUS.keys())
    found = find_interpreter_mismatch(task, "the production checkout/venv1/bin/activate")
    assert len(found) == 6, [c for c, _ in found]


# ---------------------------------------------------------------------------
# Genericity gate — the tool is shared across projects
# ---------------------------------------------------------------------------
#
# 2026-09-21: the first cut of the interpreter-consistency work put
# a real project's ``venv1`` into the *generic* tool as the example — once in
# the tasks system prompt, once in the FOREIGN_PY_ENV_RUNNER message.
# Both are runtime strings: a project using ``.venv`` or ``venv/`` would
# have been told to run ``venv1/bin/pytest``, a path it does not have.
#
# The fix is not "write a better example" — it is that a generic tool
# must never name a convention at all. The concrete path is *derived*:
# ``workspace_utils.detect_venv`` probes the project, ``_workspace_line``
# writes whatever it found into the workspace config, and the prompt
# points at that. These tests pin the property, not the wording.

#: Directory names projects use for their virtualenv, plus real operator
#: home roots. None of these may appear in a runtime string shipped by
#: generic tooling.
_PROJECT_VENV_CONVENTIONS = ("venv1", "venv2", ".venv", "venv/", "env/")
_USER_HOME_PATHS = ("/Users/", "/home/")


def _looks_project_specific(text: str) -> bool:
    return any(n in text for n in _PROJECT_VENV_CONVENTIONS + _USER_HOME_PATHS)


def test_gate_matcher_fires_on_the_shape_it_exists_to_catch():
    """A guard that cannot fail on the defect is decoration.

    Pinned against the exact strings the first cut shipped, so a future
    refactor that loosens ``_looks_project_specific`` into vacuity is
    caught here rather than by a user in an earlier run.
    """
    assert _looks_project_specific('- ✅ `"<项目>/venv1/bin/pytest tests/x.py -v"`')
    assert _looks_project_specific("(e.g. `<project>/venv1/bin/pytest …`) instead.")
    assert _looks_project_specific("/Users/someone/proj/.venv/bin/pytest")
    # ...and must NOT fire on the neutral forms we replaced them with.
    assert not _looks_project_specific('"<该项目虚拟环境的 bin 目录>/pytest tests/x.py -v"')
    assert not _looks_project_specific("(e.g. `<venv>/bin/pytest …`)")


def test_system_prompt_is_project_agnostic():
    assert not _looks_project_specific(TASKS_SYSTEM_PROMPT), (
        "TASKS_SYSTEM_PROMPT is generic tooling — it must not name one "
        "project's virtualenv convention or an operator's home path. The "
        "concrete interpreter is filled in per project by _workspace_line."
    )


def test_foreign_env_runner_message_is_project_agnostic():
    detail = find_foreign_env_runner("uv run pytest tests/x.py").detail
    assert not _looks_project_specific(detail)


def test_workspace_line_derives_the_path_from_the_project(tmp_path):
    """The interpreter path must follow what the project actually has.

    This is the property that makes the whole fix generic: a project laid
    out as ``.venv/`` gets ``.venv/bin/pytest``, with no code change.
    """
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")

    line = _workspace_line("primary_project_dir", str(tmp_path))

    assert str(tmp_path / ".venv" / "bin" / "pytest") in line
    assert "venv1" not in line


def test_workspace_line_is_silent_when_the_project_has_no_venv(tmp_path):
    """No venv ⇒ no annotation. Never invent one."""
    line = _workspace_line("primary_project_dir", str(tmp_path))
    assert line == f"- primary_project_dir: {tmp_path}"
    assert not _looks_project_specific(line)
