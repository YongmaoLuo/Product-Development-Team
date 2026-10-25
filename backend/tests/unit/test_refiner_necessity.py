"""Tests for ``refiner_necessity`` — the Q1–Q4 necessity audit.

The audit runs **before** the refiner is allowed to split a task.  Four
questions get answered from task metadata and a project index alone, with
no model in the loop:

* **Q1** — is this task a duplicate of one already in the batch?
* **Q2** — can this task's acceptance be decided at all?
* **Q3** — does this task need an environment the project does not have?
* **Q4** — does this task's write domain collide with another task's?

Every one of those answers is a conclusion about a task, and a wrong one
costs more than no answer at all: a task dropped as a duplicate that was
not one loses its deliverable, and a task rejected as unverifiable that
was perfectly verifiable never gets fixed.  So the rules are deliberately
conservative — they claim only what their inputs support, and they say
"unknown" by staying silent.

The audit is a **pure function**.  It reads the caller's dictionaries and
never writes to them, and the one thing it cannot do by itself — run
``pytest --collect-only`` — arrives as an injected runner, exactly as in
:mod:`test_command_rules`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from refiner_necessity import (  # noqa: E402
    NecessityAudit,
    NecessityFinding,
    audit_tasks,
)


# ---------------------------------------------------------------------------
# Q1 — duplicate tasks
# ---------------------------------------------------------------------------


def test_q1_duplicate():
    """Three classes: same signature, same domain and command, title alike."""
    # (1) The same signature — the same normalised title, the same file
    #     domain and the same acceptance command.  The first task in the
    #     batch survives; the second one is the duplicate.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a",
            },
            {
                "id": "2",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a",
            },
        ],
        project_index={},
    )
    assert isinstance(audit, NecessityAudit)
    assert len(audit.findings) == 1
    finding = audit.findings[0]
    assert isinstance(finding, NecessityFinding)
    assert finding.rule == "Q1"
    assert finding.task_id == "2"
    assert finding.action == "drop"
    assert finding.detail == "duplicate signature"
    assert audit.droppable_task_ids == {"2"}
    assert audit.env_blocked_task_ids == set()
    assert audit.resolved_files == {}

    # (2) The same file domain and the same acceptance command, even
    #     though the titles differ — the second task writes the same
    #     bytes and is judged by the same command, so the first one
    #     already covers it.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "Implement the login flow",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a",
            },
            {
                "id": "2",
                "title": "Add the login handler",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a",
            },
        ],
        project_index={},
    )
    assert [f.rule for f in audit.findings] == ["Q1"]
    assert audit.findings[0].task_id == "2"
    assert audit.findings[0].action == "drop"
    assert audit.droppable_task_ids == {"2"}

    # (3) Titles that are alike, but a different file domain: two
    #     different pieces of work that happen to be named the same.
    #     Nothing here supports calling the second one a duplicate.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "Implement the login flow",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a",
            },
            {
                "id": "2",
                "title": "Implement the login flow!",
                "files_to_modify": ["src/b"],
                "test_command": "pytest -q tests/b",
            },
        ],
        project_index={},
    )
    assert audit.findings == []
    assert audit.droppable_task_ids == set()
    assert audit.resolved_files == {}


# ---------------------------------------------------------------------------
# Q2 — acceptance that cannot be decided
# ---------------------------------------------------------------------------


def test_q2_unverifiable():
    """Three ways a task's acceptance cannot be decided."""
    # (1) No acceptance criterion at all: nothing to run, nothing to
    #     judge the result by.
    audit = audit_tasks(
        [{"id": "1", "title": "Do the thing", "files_to_modify": ["src/a"]}],
        project_index={},
    )
    assert [f.rule for f in audit.findings] == ["Q2"]
    assert audit.findings[0].task_id == "1"
    assert audit.findings[0].action == "reject_split"
    assert audit.findings[0].detail == "missing acceptance criteria"
    # A rejected split is not a drop: the task goes back for a fix, it
    # does not disappear.
    assert audit.droppable_task_ids == set()

    # (2) The acceptance command collects nothing.  The probe runs
    #     through the injected runner — the rule starts no process.
    seen = []

    def runner(command):
        seen.append(command)
        return 0

    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a",
            }
        ],
        project_index={"probe_runner": runner},
    )
    assert [f.rule for f in audit.findings] == ["Q2"]
    assert audit.findings[0].action == "reject_split"
    assert audit.findings[0].detail == "probe collected no tests"
    assert len(seen) == 1
    assert "--collect-only" in seen[0]

    # (3) The only test the command names is a shell: it passes without
    #     asserting anything, so it certifies nothing.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
            }
        ],
        project_index={
            "files": {"tests/a.py": "def test_x():\n    assert True\n"},
        },
    )
    assert [f.rule for f in audit.findings] == ["Q2"]
    assert audit.findings[0].action == "reject_split"
    assert audit.findings[0].detail == "empty shell test"

    # The negative control: a test that asserts something is decidable,
    # and no finding is raised for it.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
            }
        ],
        project_index={
            "files": {"tests/a.py": "def test_x():\n    assert compute() == 2\n"},
        },
    )
    assert audit.findings == []


def test_q2_a_probe_that_blew_up_claims_nothing():
    """A probe that failed measured nothing, and says so by saying nothing.

    A probe that raises is not a probe that counted zero.  Reading it as
    zero would refuse the split on evidence nobody collected — the task
    would be sent back for a defect the probe never found.  "Unknown" is
    the only honest answer, so the audit stays silent.
    """

    def runner(command):
        raise RuntimeError("the probe could not run")

    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
            }
        ],
        project_index={"probe_runner": runner},
    )

    assert audit.findings == []
    assert audit.droppable_task_ids == set()


def test_q2_no_command_with_judgeable_criteria_is_not_rejected():
    """A task whose acceptance is written down but not yet runnable is judged.

    Q2 refuses a split when the acceptance *cannot be decided*.  A task
    that states what "done" looks like — and has no command attached
    yet — has an acceptance criterion: the command is what runs it, not
    what makes it one.  Refusing such a task sends work back for a fix
    it does not need, and stops the refiner before the model is ever
    asked.
    """
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "Do the thing",
                "files_to_modify": ["src/a"],
                "acceptance_criteria": (
                    "TDD 规格：test_a 断言 parse() 返回 2，缺一个字段时抛 ValueError"
                ),
            }
        ],
        project_index={},
    )

    assert [f.rule for f in audit.findings] == []
    assert audit.droppable_task_ids == set()


def test_q2_unjudgeable_criteria_still_rejected():
    """An acceptance that says nothing is refused, whatever shape it has.

    The three unjudgeable shapes: no acceptance at all, an acceptance
    that is only a placeholder, and an acceptance whose command
    collects no tests.
    """
    # (1) No acceptance at all.
    missing = audit_tasks(
        [{"id": "1", "title": "Do the thing", "files_to_modify": ["src/a"]}],
        project_index={},
    )
    assert [f.action for f in missing.findings] == ["reject_split"]
    assert missing.findings[0].detail == "missing acceptance criteria"

    # (2) A placeholder is not an acceptance criterion: any outcome
    #     would satisfy it.
    placeholder = audit_tasks(
        [
            {
                "id": "1",
                "title": "Do the thing",
                "files_to_modify": ["src/a"],
                "acceptance_criteria": "TODO",
            }
        ],
        project_index={},
    )
    assert [f.action for f in placeholder.findings] == ["reject_split"]
    assert placeholder.findings[0].detail == "placeholder acceptance criteria"

    # (2b) Punctuation alone is not an acceptance criterion either —
    #      it says no more than an empty string does.
    punctuation = audit_tasks(
        [
            {
                "id": "1",
                "title": "Do the thing",
                "files_to_modify": ["src/a"],
                "acceptance_criteria": " .。- ",
            }
        ],
        project_index={},
    )
    assert [f.action for f in punctuation.findings] == ["reject_split"]
    assert punctuation.findings[0].detail == "placeholder acceptance criteria"

    # (3) The command collects nothing — the explicit mapping to a
    #     criterion defect stays as it was.
    def runner(command):
        return 0

    collected = audit_tasks(
        [
            {
                "id": "1",
                "title": "Do the thing",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
            }
        ],
        project_index={"probe_runner": runner},
    )
    assert [f.action for f in collected.findings] == ["reject_split"]
    assert collected.findings[0].detail == "probe collected no tests"


def test_the_agent_hands_the_refiner_a_project_index():
    """The production call site passes the project to the audit.

    Without an index the audit has nothing to read: the collect-only
    probe, the empty-shell check and the environment check never fire
    outside the tests, and the step that was added to keep failures out
    of the triage stays switched off in production.
    """
    import agent as agent_module
    from task import SubTask

    captured = {}

    class _Refiner:
        def refine(self, **kwargs):
            captured.update(kwargs)
            return list(kwargs.get("tasks") or [])

    class _TaskManager:
        requirement = "req"

        def __init__(self, tasks):
            self.tasks = tasks

    stub = agent_module.AutonomousAgent.__new__(agent_module.AutonomousAgent)
    stub._refinement_count = 0
    stub.logger = None
    stub.plan_id = "p1"
    stub.project_dir = BACKEND_DIR
    stub.task_manager = _TaskManager(
        [
            SubTask(
                id="1",
                title="a",
                description="d",
                test_command="pytest -q tests/unit/test_refiner_necessity.py",
                files_to_modify=["src/a.py"],
            )
        ]
    )
    stub.refiner = _Refiner()
    stub._persist_refiner_output = lambda *args, **kwargs: None

    stub._refine_after_failure(
        stub.task_manager.tasks[0],
        coder_response="r",
        result_context="x",
        file_context="",
        exit_code=1,
    )

    assert "project_index" in captured
    assert captured["project_index"] is not None
    assert captured["project_index"].get("files"), captured["project_index"]
    runner = captured["project_index"].get("probe_runner")
    assert callable(runner)

    # The probe answers for the round's own task and refuses the rest:
    # one round refines one task, and a probe is a process — probing
    # the whole batch would make every refinement pay for the size of
    # the plan.  A refusal is "unknown", not "zero": the audit makes no
    # claim about a task it could not measure.
    import pytest

    with pytest.raises(RuntimeError):
        runner("pytest --collect-only -q tests/unit/some_other_task.py")


# ---------------------------------------------------------------------------
# Q3 — the environment the task needs is not there
# ---------------------------------------------------------------------------


def test_q3_env():
    """A task that needs a service, a port or an env var the project lacks."""
    tasks = [
        {
            "id": "1",
            "title": "Talk to redis",
            "files_to_modify": ["src/a.py"],
            "test_command": "pytest -q tests/a.py",
            "requires": {
                "services": ["redis"],
                "ports": [6379],
                "env_vars": ["REDIS_URL"],
            },
        }
    ]
    project_index = {
        "services": ["postgres"],
        "ports": [5432],
        "env_vars": ["DATABASE_URL"],
    }

    audit = audit_tasks(tasks, project_index)
    q3 = [f for f in audit.findings if f.rule == "Q3"]
    assert len(q3) == 3
    assert {f.task_id for f in q3} == {"1"}
    assert {f.action for f in q3} == {"env_infra"}
    assert {f.detail for f in q3} == {
        "service redis is not available in the project index",
        "port 6379 is not available in the project index",
        "env var REDIS_URL is not available in the project index",
    }
    # An environment failure is not a duplicate and not a resolved
    # conflict: the task is held for the environment, and it is neither
    # dropped nor rewritten.
    assert audit.env_blocked_task_ids == {"1"}
    assert audit.droppable_task_ids == set()
    assert audit.resolved_files == {}

    # The negative control: what the project does provide is not a
    # finding.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "Talk to postgres",
                "files_to_modify": ["src/a.py"],
                "test_command": "pytest -q tests/a.py",
                "requires": {"services": ["postgres"], "ports": [5432]},
            }
        ],
        project_index={"services": ["postgres"], "ports": [5432]},
    )
    assert audit.findings == []
    assert audit.env_blocked_task_ids == set()


# ---------------------------------------------------------------------------
# Q4 — two tasks write the same bytes
# ---------------------------------------------------------------------------


def test_q4_prefix_conflict():
    """A directory domain and a file written under it are one domain."""
    # (1) Resolvable: the pair can be serialised, and the two
    #     declarations are merged into one domain both tasks carry.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
            },
            {
                "id": "2",
                "title": "B",
                "files_to_modify": ["src/a/b.py"],
                "test_command": "pytest -q tests/b.py",
            },
        ],
        project_index={},
    )
    q4 = [f for f in audit.findings if f.rule == "Q4"]
    assert len(q4) == 1
    assert q4[0].task_id == "2"
    assert q4[0].action == "serialize"
    assert audit.resolved_files == {
        "1": ["src/a", "src/a/b.py"],
        "2": ["src/a", "src/a/b.py"],
    }
    # A conflict is not a duplicate: nothing is dropped.
    assert audit.droppable_task_ids == set()

    # (2) Unresolvable: the two tasks already depend on each other, so
    #     ordering them would close a cycle.  The task is blocked.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
                "depends_on": ["2"],
            },
            {
                "id": "2",
                "title": "B",
                "files_to_modify": ["src/a/b.py"],
                "test_command": "pytest -q tests/b.py",
                "depends_on": ["1"],
            },
        ],
        project_index={},
    )
    q4 = [f for f in audit.findings if f.rule == "Q4"]
    assert len(q4) == 1
    assert q4[0].task_id == "2"
    assert q4[0].action == "block"
    # Nothing was resolved, so nothing is rewritten.
    assert audit.resolved_files == {}

    # (3) The negative control: a shared prefix that is not a shared
    #     domain.  ``src/ab`` is a different directory from ``src/a``.
    audit = audit_tasks(
        [
            {
                "id": "1",
                "title": "A",
                "files_to_modify": ["src/a"],
                "test_command": "pytest -q tests/a.py",
            },
            {
                "id": "2",
                "title": "B",
                "files_to_modify": ["src/ab/c.py"],
                "test_command": "pytest -q tests/c.py",
            },
        ],
        project_index={},
    )
    assert audit.findings == []
    assert audit.resolved_files == {}


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_audit_does_not_mutate_its_inputs():
    """The caller's data comes back byte for byte unchanged."""
    tasks = [
        {
            "id": "1",
            "title": "A",
            "files_to_modify": ["src/a"],
            "test_command": "pytest -q tests/a",
        },
        {
            "id": "2",
            "title": "A",
            "files_to_modify": ["src/a"],
            "test_command": "pytest -q tests/a",
        },
        {
            "id": "3",
            "title": "B",
            "files_to_modify": ["src/a/b.py"],
            "test_command": "pytest -q tests/b.py",
        },
    ]
    project_index = {"services": ["postgres"], "files": {"tests/a.py": "x = 1\n"}}

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    before = digest(tasks)
    index_before = digest(project_index)

    audit_tasks(tasks, project_index)

    assert digest(tasks) == before
    assert digest(project_index) == index_before
