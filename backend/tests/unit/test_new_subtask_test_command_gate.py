"""The gate on a test_command the refiner just wrote (2026-10-11).

The rule this pins is *not* "the refiner may not touch test_commands".
It is the narrower one: **the refiner may write one, once.**

Someone has to author the command the first time. When a failed task is
split into children, each child is a different task with a smaller scope,
so it needs its own judge — freezing the parent's onto it would grade the
child against work it was never asked to do. What must not happen is the
*second* write, because a command that grades a task and can be edited by
the loop being graded is not a judge. That half is enforced in
``refiner_structure.plan_refiner_structure`` (see
``tests/unit/test_refiner_structure.py``).

This file covers the other half: the first write still has to be a
*usable* command, and nothing checked. ``_refine_after_failure`` ran
neither the shape inspector nor the falsifiability probe over the
refiner's output, while the generation path runs both — which is how the
20261010-CC-Switch-Remote-Aut refiner answered an environment failure
with 25 rewritten commands (each carrying a
``PATH="$HOME/.cargo/bin:$PATH"`` prefix) that could never go green.

A rejected command is DISCARDED rather than shipped, matching
``RepairTaskGenerator._resolve_repair_test_command``: a command that
cannot report failure guarantees a false verdict every time, whereas no
command degrades to the audit second pass, which can still decide.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=str(project_dir), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=str(project_dir), capture_output=True, check=True,
    )


class _DummyCodingTool:
    def __init__(self):
        self.calls = []

    def query_json(self, prompt, system_instruction=""):
        self.calls.append(prompt)
        return {"tasks": []}


@pytest.fixture
def agent(tmp_path):
    project = tmp_path / "project"
    _git_init(project)
    return AutonomousAgent(
        requirement="t",
        project_dir=project,
        coding_tool=_DummyCodingTool(),
        logger=None,
    ), project


def _subtask(project: Path, **overrides) -> dict:
    task = {
        "id": "1-1",
        "title": "t",
        "description": "d",
        "project_dir": str(project),
        "files_to_modify": ["src/x.rs"],
        "depends_on": [],
    }
    task.update(overrides)
    return task


def test_a_command_that_cannot_start_is_discarded(agent):
    """The exact shape that ran away in 20261010-CC-Switch-Remote-Aut."""
    a, project = agent
    task = _subtask(
        project,
        test_command="no_such_binary_xyz test --lib remote::auth::secret",
    )

    a._enforce_new_subtask_test_command(task)

    assert not (task.get("test_command") or "").strip(), (
        "a command whose runner does not exist can never turn green; it "
        "must not be shipped as this task's judge"
    )


def test_a_command_that_already_passes_is_discarded(agent):
    """It cannot distinguish a correct fix from no change at all."""
    a, project = agent
    task = _subtask(project, test_command="exit 0")

    a._enforce_new_subtask_test_command(task)

    assert not (task.get("test_command") or "").strip()


def test_a_structurally_unusable_command_is_discarded(agent):
    """Shape is checked too, and without executing anything."""
    a, project = agent
    chain = (
        "test -f src/x.rs && grep -q 'mod x;' src/mod.rs && "
        "test -f src/y.rs && grep -q 'y' src/mod.rs"
    )
    task = _subtask(project, test_command=chain)

    a._enforce_new_subtask_test_command(task)

    assert not (task.get("test_command") or "").strip()


def test_a_genuinely_red_command_is_kept(agent):
    """The gate must not reject a legitimate first write."""
    a, project = agent
    task = _subtask(project, test_command="exit 3")

    a._enforce_new_subtask_test_command(task)

    assert task["test_command"] == "exit 3"


def test_a_cargo_command_against_a_missing_module_is_discarded(agent):
    """``cargo test --lib <absent module>`` exits 0 with zero tests.

    This is the command family the refiner reached for, and the reason
    the gate has to *execute* the candidate rather than reason about it:
    the string is perfectly well-formed Rust.
    """
    a, project = agent
    task = _subtask(
        project,
        test_command="cargo test --lib remote::auth::secret_not_here",
    )
    if subprocess.run(
        ["sh", "-c", "command -v cargo"], capture_output=True,
    ).returncode != 0:
        pytest.skip("cargo not on PATH for this test runner")
    if not (project / "Cargo.toml").exists():
        (project / "src").mkdir(exist_ok=True)
        (project / "Cargo.toml").write_text(
            '[package]\nname = "p"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        (project / "src" / "lib.rs").write_text("")

    a._enforce_new_subtask_test_command(task)

    assert not (task.get("test_command") or "").strip(), (
        "a module-wide filter naming something absent collects zero tests "
        "and exits 0 — it can never certify the work"
    )


def test_a_task_with_no_command_is_left_alone(agent):
    """The refiner choosing not to write one is a legitimate choice."""
    a, project = agent
    task = _subtask(project)

    a._enforce_new_subtask_test_command(task)

    assert "test_command" not in task
    assert not task.get("test_commands")


def test_the_bad_one_of_several_commands_is_the_only_one_dropped(agent):
    a, project = agent
    task = _subtask(
        project,
        test_commands=["exit 3", "no_such_binary_xyz --check"],
    )

    a._enforce_new_subtask_test_command(task)

    assert task["test_commands"] == ["exit 3"]


def test_the_probe_can_be_switched_off_for_debugging(agent, monkeypatch):
    """Shape is still checked; executing is what the flag skips.

    An operator debugging refinement without a working toolchain needs
    that escape hatch — the same one ``RepairTaskGenerator`` offers.
    """
    a, project = agent
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")

    executable_but_unproven = _subtask(project, test_command="exit 0")
    a._enforce_new_subtask_test_command(executable_but_unproven)
    assert executable_but_unproven["test_command"] == "exit 0", (
        "with the probe off, an unexecuted command must survive"
    )

    bad_shape = _subtask(
        project,
        test_command="test -f a && grep -q b c && test -f d && grep -q e f",
    )
    a._enforce_new_subtask_test_command(bad_shape)
    assert not (bad_shape.get("test_command") or "").strip(), (
        "the shape inspector does not execute anything, so the flag must "
        "not disable it"
    )
