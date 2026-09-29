"""
The RED step for generated tasks: a command must fail before the work.

Background
----------
A task's completion verdict cross-checks the subagent's ``TEST_RESULT``
against the command's exit code. That second signal is worth nothing
unless the command can fail. A command that already exits 0 before any
task runs can be "passed" by doing nothing.

Why the probe *reports* rather than rejects
-------------------------------------------
Three situations produce the same ``exit 0``:

* **A** — the feature already exists; the task is redundant.
* **B** — the command points at an artifact that already exists and does
  not cover this task's change (the classic "append cases to an existing
  spec file" shape: that file passes before and after).
* **C** — the command is vacuous by construction.

They need different resolutions, and the probe cannot tell them apart on
its own. What it can add is the mechanical half: whether the command's
path-like arguments already exist. That is a hint, labelled as one.

Two facts this file pins, both measured rather than assumed
-----------------------------------------------------------
* ``cargo test --lib <name>`` exits **0** when the filter matches
  nothing — so "filter an existing test collection by name" is not a
  falsifiable shape, however specific the name looks.
* ``cd subdir && <cmd>`` resolves its path arguments against
  ``subdir``, not the project root. Getting this wrong flipped the hint
  for tasks 13/14 from "points at an existing artifact" to the opposite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tasks_generator import TasksGenerator  # noqa: E402


class _StubCodingTool:
    """Not exercised by these tests."""

    def run_query(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def gen(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    return TasksGenerator(_StubCodingTool(), plan_dir)


def _task(tmp_path, commands, task_id="1"):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return {
        "id": task_id,
        "title": "t",
        "description": "",
        "project_dir": str(project),
        "test_commands": list(commands),
    }


# ---------------------------------------------------------------------------
# What the probe reports
# ---------------------------------------------------------------------------


def test_a_command_that_already_passes_is_reported(gen, tmp_path):
    report = gen.probe_falsifiability([_task(tmp_path, ["exit 0"])])
    assert report["checked"] == 1
    assert len(report["vacuous"]) == 1
    assert report["vacuous"][0]["task_id"] == "1"


def test_a_command_that_fails_is_not_reported(gen, tmp_path):
    report = gen.probe_falsifiability([_task(tmp_path, ["exit 1"])])
    assert report["checked"] == 1
    assert report["vacuous"] == []


def test_the_list_form_is_probed(gen, tmp_path):
    """The form the generator writes; a probe reading only the string
    form would report ``checked: 0`` and look healthy."""
    report = gen.probe_falsifiability([_task(tmp_path, ["exit 0", "exit 1"])])
    assert report["checked"] == 2
    assert len(report["vacuous"]) == 1


def test_the_report_is_written_to_the_plan_dir(gen, tmp_path):
    gen.probe_falsifiability([_task(tmp_path, ["exit 0"])])
    written = json.loads(
        (Path(gen.plan_dir) / "falsifiability_report.json").read_text()
    )
    assert written["vacuous"][0]["task_id"] == "1"


def test_the_probe_runs_once_per_distinct_command(gen, tmp_path, monkeypatch):
    """It starts subprocesses; a plan with N tasks sharing one command
    shape must not pay N times."""
    import tasks_generator as tg

    calls = []
    real = tg.check_falsifiable

    def counting_check(command, **kwargs):
        calls.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(tg, "check_falsifiable", counting_check)

    tasks = [_task(tmp_path, ["exit 0"], task_id=str(i)) for i in range(4)]
    report = gen.probe_falsifiability(tasks)

    assert calls == ["exit 0"], f"probe ran {len(calls)} times, expected 1"
    # Every task still gets its own entry — the cache is about cost, not
    # about collapsing the report.
    assert report["checked"] == 4
    assert len(report["vacuous"]) == 4


def test_the_probe_can_be_disabled(gen, tmp_path, monkeypatch):
    monkeypatch.setenv(TasksGenerator.FALSIFIABILITY_ENV, "1")
    report = gen.probe_falsifiability([_task(tmp_path, ["exit 0"])])
    assert report["skipped"] is True
    assert report["vacuous"] == []


# ---------------------------------------------------------------------------
# The A/B/C hint
# ---------------------------------------------------------------------------


def test_a_command_pointing_at_an_existing_artifact_says_so(gen, tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "tests" / "existing.spec.ts").write_text("//", encoding="utf-8")
    report = gen.probe_falsifiability(
        [_task(tmp_path, ["exit 0  # tests/existing.spec.ts"])]
    )
    item = report["vacuous"][0]
    assert item["likely"] == "references_existing_artifact"
    assert item["existing_paths"] == ["tests/existing.spec.ts"]


def test_a_command_naming_no_artifact_says_so(gen, tmp_path):
    """The ``cargo test --lib x`` shape: no path, passes with 0 tests."""
    report = gen.probe_falsifiability([_task(tmp_path, ["exit 0"])])
    assert report["vacuous"][0]["likely"] == "references_no_new_artifact"


def test_paths_resolve_against_a_leading_cd(gen, tmp_path):
    """``cd frontend && vitest run a/b.spec.ts`` resolves under ``frontend``.

    Resolving it against the project root instead reports no existing
    artifact, which inverts the task's hint.
    """
    project = tmp_path / "project"
    (project / "frontend" / "a").mkdir(parents=True, exist_ok=True)
    (project / "frontend" / "a" / "b.spec.ts").write_text("//", encoding="utf-8")

    resolved = gen._effective_cwd(
        "cd frontend && npx vitest run a/b.spec.ts", project
    )
    assert resolved == project / "frontend"
    assert gen._existing_paths_in(
        "cd frontend && npx vitest run a/b.spec.ts", project
    ) == ["a/b.spec.ts"]


def test_an_absolute_cd_is_honoured(gen, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert gen._effective_cwd(f"cd {other} && ls", tmp_path) == other


# ---------------------------------------------------------------------------
# Feeding the findings back to the rewriting agent
# ---------------------------------------------------------------------------
#
# Measuring is only half the loop. The other half is handing the
# rewriting agent what was measured, why the obvious fix does not work,
# and what does — otherwise a round of self-review re-emits the same
# shape and the loop spins.


def _report(*commands):
    return {
        "checked": len(commands),
        "skipped": False,
        "vacuous": [
            {"task_id": str(i + 1), "command": c, "likely": "references_no_new_artifact"}
            for i, c in enumerate(commands)
        ],
    }


def test_no_block_when_there_is_nothing_to_report():
    from tasks_generator import _render_falsifiability_block

    assert _render_falsifiability_block(None) == ""
    assert _render_falsifiability_block({}) == ""
    assert _render_falsifiability_block({"vacuous": []}) == ""


def test_the_block_names_the_commands_and_the_forbidden_shapes():
    """The measured commands must appear, and so must the anti-patterns —
    "narrow the filter" is the fix a rewriting agent reaches for, and it
    does not work."""
    from tasks_generator import _render_falsifiability_block

    block = _render_falsifiability_block(_report("cargo test --lib types"))
    assert "cargo test --lib types" in block
    assert "尚不存在" in block
    assert "cargo test --lib" in block
    assert "vitest" in block


def test_the_self_review_prompt_carries_the_block():
    from tasks_generator import _build_tasks_self_review_prompt

    without = _build_tasks_self_review_prompt("{}", "md", "")
    with_block = _build_tasks_self_review_prompt(
        "{}", "md", "", _report("cargo test --lib types")
    )
    assert without == _build_tasks_self_review_prompt("{}", "md", "", None)
    assert "命令可失败性（RED）实测结果" not in without
    assert "命令可失败性（RED）实测结果" in with_block


# ---------------------------------------------------------------------------
# The iteration loop
# ---------------------------------------------------------------------------


def test_the_loop_stops_when_nothing_is_vacuous(gen, tmp_path):
    gen.probe_falsifiability = lambda tasks, **k: {"skipped": False, "vacuous": []}
    calls = []
    gen._run_tasks_self_review = lambda *a, **k: calls.append(1)
    gen._iterate_on_falsifiability({"tasks": []}, {"skipped": False, "vacuous": []})
    assert calls == [], "no round should run when the plan is already clean"


def test_the_loop_stops_when_a_round_does_not_improve(gen):
    """Repeating a rewrite that did not help only burns tokens."""

    def probe(tasks, **kwargs):
        return {"skipped": False, "vacuous": [{"task_id": "1", "command": "x"}]}

    rounds = []
    gen.probe_falsifiability = probe
    gen._run_tasks_self_review = lambda *a, **k: rounds.append(1)

    gen._iterate_on_falsifiability(
        {"tasks": []}, {"skipped": False, "vacuous": [{"task_id": "1", "command": "x"}]}
    )
    assert len(rounds) == 1, "should stop after one non-improving round"


def test_the_loop_succeeds_when_a_round_clears_them(gen):
    state = {"n": 0}

    def probe(tasks, **kwargs):
        state["n"] += 1
        return {"skipped": False, "vacuous": []}

    rounds = []
    gen.probe_falsifiability = probe
    gen._run_tasks_self_review = lambda *a, **k: rounds.append(1)

    result = gen._iterate_on_falsifiability(
        {"tasks": []}, {"skipped": False, "vacuous": [{"task_id": "1", "command": "x"}]}
    )
    assert result["vacuous"] == []
    assert len(rounds) == 1


def test_the_loop_is_bounded_by_max_rounds(gen):
    """Each round is a full LLM pass over tasks.json.

    The probe improves every round but never reaches zero, so only the
    round cap can stop it.
    """
    counts = iter([5, 4, 3, 2, 1, 0])

    def probe(tasks, **kwargs):
        n = next(counts, 1)
        return {
            "skipped": False,
            "vacuous": [{"task_id": str(i), "command": "x"} for i in range(n)],
        }

    rounds = []
    gen.probe_falsifiability = probe
    gen._run_tasks_self_review = lambda *a, **k: rounds.append(1)

    gen._iterate_on_falsifiability(
        {"tasks": []}, {"skipped": False, "vacuous": [{"task_id": str(i), "command": "x"} for i in range(9)]}
    )
    # One mandatory round already ran in generate(); the loop adds
    # MAX_ROUNDS - 1 more. `0` is never reached before the cap.
    assert len(rounds) == TasksGenerator.FALSIFIABILITY_MAX_ROUNDS - 1


def test_a_failing_round_does_not_abort_generation(gen):
    """The plan is still usable; the findings stay on disk for an operator."""

    def boom(*args, **kwargs):
        raise RuntimeError("provider hiccup")

    gen.probe_falsifiability = lambda tasks, **k: {
        "skipped": False, "vacuous": [{"task_id": "1", "command": "x"}],
    }
    gen._run_tasks_self_review = boom

    result = gen._iterate_on_falsifiability(
        {"tasks": []}, {"skipped": False, "vacuous": [{"task_id": "1", "command": "x"}]}
    )
    assert len(result["vacuous"]) == 1


def test_the_loop_does_not_run_when_the_probe_was_skipped(gen):
    rounds = []
    gen._run_tasks_self_review = lambda *a, **k: rounds.append(1)
    gen._iterate_on_falsifiability({"tasks": []}, {"skipped": True, "vacuous": []})
    assert rounds == []


# ---------------------------------------------------------------------------
# Unreachable targets: the other half of usability
# ---------------------------------------------------------------------------
#
# A command that can never pass satisfies RED just as well as a vacuous
# one — and the probe reports ``vacuous: []`` for it, because a
# permanently-broken command is not what "vacuous" means. The two failure
# modes need two checks.


def test_the_probe_reports_unreachable_targets(gen, tmp_path):
    task = _task(tmp_path, ["exit 1"])  # fails, so not vacuous
    task["test_commands"] = [
        "cd frontend && npx vitest run frontend/tests/x.spec.ts"
    ]
    report = gen.probe_falsifiability([task])
    assert report["vacuous"] == []
    assert len(report["unreachable"]) == 1
    assert report["unreachable"][0]["code"] == "doubled_cd_prefix"


def test_the_block_renders_the_unreachable_section():
    from tasks_generator import _render_falsifiability_block

    block = _render_falsifiability_block({
        "vacuous": [],
        "unreachable": [{
            "task_id": "19",
            "code": "doubled_cd_prefix",
            "detail": "…",
            "reference": "frontend/tests/x.spec.ts",
        }],
    })
    assert "不可达" in block
    assert "doubled_cd_prefix" in block


def test_the_block_is_empty_only_when_both_are_empty():
    from tasks_generator import _render_falsifiability_block

    assert _render_falsifiability_block(
        {"vacuous": [], "unreachable": []}
    ) == ""


def test_the_loop_keeps_going_for_unreachable_targets(gen):
    """Both categories are the same kind of unusable."""
    rounds = []
    gen.probe_falsifiability = lambda tasks, **k: {
        "skipped": False,
        "vacuous": [],
        "unreachable": [{"task_id": "19", "code": "doubled_cd_prefix"}],
    }
    gen._run_tasks_self_review = lambda *a, **k: rounds.append(1)

    gen._iterate_on_falsifiability(
        {"tasks": []},
        {"skipped": False, "vacuous": [], "unreachable": [
            {"task_id": "19", "code": "doubled_cd_prefix"}
        ]},
    )
    assert len(rounds) == 1, (
        "an unreachable target must trigger a rewrite, not be ignored"
    )


def test_problem_count_adds_both_categories():
    from tasks_generator import _probe_problem_count

    assert _probe_problem_count(None) == 0
    assert _probe_problem_count({"vacuous": [1], "unreachable": [1, 2]}) == 3
    assert _probe_problem_count({"vacuous": [1]}) == 1


# ---------------------------------------------------------------------------
# A command with no declared workspace is NEVER executed
# ---------------------------------------------------------------------------
#
# 2026-09-22. ``cwd`` was ``None`` whenever a task declared no
# ``project_dir``, and ``subprocess.run(cwd=None)`` means "inherit the
# current working directory" — the backend's own checkout. So the probe executed an
# LLM-authored shell command inside the this repositorysitory.
#
# the backend's own unit fixture emits ``test_command="pytest tests/ -v"``. With no
# ``project_dir`` the probe ran that in ``backend/``: a nested pytest over
# the entire suite, which re-entered this very test. The nested child
# outlived the outer runner's 60s timeout kill, and the recursive orphans
# starved the GitHub runner until it lost contact with the server 48
# minutes later (runs 35725371222 / 35077046987, no logs uploaded).


def _task_without_workspace(commands, task_id="1"):
    return {
        "id": task_id,
        "title": "no workspace declared",
        "description": "",
        "project_dir": "",
        "test_commands": list(commands),
    }


def test_a_task_without_a_workspace_is_never_executed(gen, monkeypatch):
    """The command must not be run — not in the backend's cwd, not anywhere."""
    import tasks_generator as tg

    calls: list = []

    def _forbidden(command, cwd=None, timeout=None):
        calls.append((command, cwd))
        raise AssertionError(
            "the probe executed a command for a task with no declared "
            "workspace — cwd would be inherited from the backend process"
        )

    monkeypatch.setattr(tg, "check_falsifiable", _forbidden)

    report = gen.probe_falsifiability(
        [_task_without_workspace(["pytest tests/ -v"])]
    )

    assert calls == [], (
        "an LLM-authored shell command was executed against the backend's own "
        "working directory"
    )
    assert report["checked"] == 0
    assert len(report["not_probed"]) == 1
    entry = report["not_probed"][0]
    assert entry["command"] == "pytest tests/ -v"
    assert entry["task_id"] == "1"
    assert "project_dir" in entry["detail"]

    # Refusing to probe is not "everything is fine" — the report must not
    # claim the command was checked and found falsifiable.
    assert report["vacuous"] == []


def test_a_task_with_a_workspace_is_still_probed(gen, monkeypatch, tmp_path):
    """The guard must not disable the probe for real plans."""
    import tasks_generator as tg

    calls: list = []
    monkeypatch.setattr(
        tg, "check_falsifiable",
        lambda command, cwd=None, timeout=None: (
            calls.append((command, cwd)) or (True, "fails as required")
        ),
    )

    report = gen.probe_falsifiability(
        [_task(tmp_path, ["pytest tests/ -v"])]
    )

    assert len(calls) == 1
    assert calls[0][1] == str(tmp_path / "project")
    assert report["checked"] == 1
    assert report["not_probed"] == []
