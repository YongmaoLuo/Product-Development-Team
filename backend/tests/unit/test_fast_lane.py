"""Fast lane — storing imported material and auditing the generated list.

The caller of ``POST /api/plan/import`` has already finished the thinking
and sends whatever their own system produced. That shapes every decision
in ``fast_lane.py``, and this module pins the three a passing test run
cannot otherwise see:

* **The material is stored, not parsed.** A string, a bare array and an
  object with keys nobody here has heard of all have to survive
  :func:`store_import_payload` byte for byte. A lane that normalised the
  caller's fields into ``SubTask`` fields would look identical on a
  well-formed list and quietly drop everything it did not recognise on a
  real one.
* **The audit is the product.** The brief for this lane generates the task
  list with the same model ``tasks_generation`` uses, so the checks in
  :func:`validate_tasks` are the last thing standing between a generated
  list and an executor that cannot schedule it. Each test therefore
  asserts on the *message* as well as the verdict: a validation pass that
  returns ``["invalid"]`` has told the caller nothing they can act on.
* **The import prompt is a real prompt.** ``import.json`` is shown to the
  model by path under the same path-only contract every other upstream doc
  uses, and the system prompt has to carry the whole ``TASKS_SYSTEM_PROMPT``
  — a weaker schema in import mode is a plan the executor cannot run.

The HTTP layer is pinned separately in
``backend/tests/api/test_plan_import_api.py``, which drives the same
generator through the mounted route with a fake coding tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fast_lane import (  # noqa: E402
    IMPORT_PAYLOAD_FILENAME,
    import_payload_summary,
    store_import_payload,
    validate_tasks,
)
from task import UNKNOWN_MODIFICATIONS_SENTINEL, SubTask  # noqa: E402
from tasks_generator import (  # noqa: E402
    NO_WORKSPACE_PROMPT,
    TASKS_SYSTEM_PROMPT,
    TasksGenerator,
)


PROJECT = "/tmp/example-repo"


def _code_task(**overrides):
    """A task that passes every rule, so each test can break exactly one."""
    task = {
        "id": "t-1",
        "title": "do the thing",
        "description": "why",
        "files_to_modify": ["backend/thing.py"],
        "test_command": "pytest -q backend/tests/test_thing.py",
    }
    task.update(overrides)
    return task


def _plan_dir(tmp_path):
    plan_dir = tmp_path / "20260101-fast-lane"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


# ---------------------------------------------------------------------------
# 1 — the caller's material is stored, never interpreted
# ---------------------------------------------------------------------------

#: One payload per shape the lane must accept. The object is deliberately
#: made of keys this repository has never heard of: if anything here ever
#: grows a "known keys" check again, that is the row that goes red.
THE_THREE_SHAPES = [
    ("a_string", "给报表页加一个按天分组的柱状图，入口在 /reports"),
    ("a_bare_array", [
        {"name": "wire the loader", "notes": "reads csv"},
        {"name": "render the chart"},
    ]),
    ("an_arbitrary_object", {
        "ticket": "PROJ-1234",
        "epic": {"name": "reporting", "owner": "someone"},
        "acceptance": ["chart renders", "no N+1 queries"],
        "links": {"figma": "https://example.invalid/figma/1"},
    }),
]


@pytest.mark.parametrize("label,payload", THE_THREE_SHAPES, ids=[s[0] for s in THE_THREE_SHAPES])
def test_every_input_shape_is_stored_verbatim(tmp_path, label, payload):
    """What the caller sent is what the model will read.

    Asserted on the round-tripped JSON rather than on "the file exists":
    a writer that coerced the array into ``{"tasks": [...]}`` or dropped
    the object's unknown keys would still produce a readable file, and
    that is exactly the silent truncation this lane must not do.
    """
    plan_dir = _plan_dir(tmp_path)

    path = store_import_payload(plan_dir, payload)

    assert path == plan_dir / IMPORT_PAYLOAD_FILENAME
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_the_payload_file_is_named_the_same_thing_the_prompt_names(tmp_path):
    """The prompt points at ``import.json``; the file has to be that name.

    Pinned as a literal rather than derived from the constant on both
    sides: a rename that moved the writer and left the prompt naming the
    old path would leave the model reading a file that is not there, and
    it would do so silently.
    """
    plan_dir = _plan_dir(tmp_path)

    assert store_import_payload(plan_dir, "x").name == "import.json"
    assert IMPORT_PAYLOAD_FILENAME == "import.json"


def test_storing_leaves_no_temp_file_behind(tmp_path):
    """The write is a rename, not a truncate-then-write.

    A crash between truncate and write leaves the model reading half a
    document and blaming the payload for it.
    """
    plan_dir = _plan_dir(tmp_path)

    store_import_payload(plan_dir, {"a": [1, 2, 3]})

    assert sorted(p.name for p in plan_dir.iterdir()) == ["import.json"]


def test_a_second_store_replaces_the_first(tmp_path):
    plan_dir = _plan_dir(tmp_path)

    store_import_payload(plan_dir, "first")
    store_import_payload(plan_dir, ["second"])

    assert json.loads((plan_dir / "import.json").read_text(encoding="utf-8")) == ["second"]


@pytest.mark.parametrize(
    "payload,expected_fragment",
    [
        ("  add a demo endpoint  ", "add a demo endpoint"),
        (["wire", "the", "loader"], "wire"),
        ({"epic": "reporting"}, "reporting"),
    ],
)
def test_the_plan_id_seed_has_a_reading_for_every_shape(payload, expected_fragment):
    """A caller who sent no ``title`` still gets a plan id derived from
    what they sent — the string form is the one rendering every JSON value
    has, so this needs no opinion about the payload's structure."""
    summary = import_payload_summary(payload)

    assert expected_fragment in summary
    assert "\n" not in summary
    assert len(summary) <= 60


def test_a_payload_with_nothing_to_say_yields_no_seed():
    """The route falls back to a fixed slug. An empty string is the one
    reading every shape agrees on being empty, so it is the one this
    returns empty for."""
    assert import_payload_summary("") == ""
    assert import_payload_summary("   \n  ") == ""


# ---------------------------------------------------------------------------
# 2 — the import prompt
# ---------------------------------------------------------------------------


class _RecordingTool:
    """Records what the generator asked; returns one scripted reply."""

    def __init__(self, reply):
        self._reply = reply
        self.json_calls: list = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append({"prompt": prompt, "system_instruction": system_instruction})
        return self._reply


def _import_generator(tmp_path, project_dir=PROJECT):
    plan_dir = _plan_dir(tmp_path)
    store_import_payload(plan_dir, "add a chart to the reports page")
    return TasksGenerator(
        _RecordingTool({"tasks": [_code_task()]}),
        plan_dir,
        self_review_enabled=False,
        import_mode=True,
        import_project_dir=project_dir,
    )


def test_the_import_prompt_names_the_payload_by_path_not_by_content(tmp_path):
    """Path-only, like every other upstream doc in this generator.

    The generated path inlines nothing either (the v15 contract): the
    model ``Read``s the document itself. Inlining here would make this
    lane the one place where a caller's payload is truncated to fit a
    prompt, which is the truncation the whole design refuses.
    """
    generator = _import_generator(tmp_path)

    prompt, _system = generator._build_import_prompts()

    assert str(generator.import_file) in prompt
    assert "add a chart to the reports page" not in prompt
    assert "Read" in prompt


def test_the_import_prompt_says_the_material_stands_in_for_the_whole_chain(tmp_path):
    """The one thing the model cannot infer from the file.

    Absent this, a model told "generate tasks from the PRD" and handed
    ``import.json`` spends its budget looking for a PRD that was never
    going to exist — the failure mode the generated path's smoke runs
    already showed once.
    """
    generator = _import_generator(tmp_path)

    prompt, _system = generator._build_import_prompts()

    for fragment in ("interview", "PRD", "架构设计", "测试设计"):
        assert fragment in prompt, fragment
    assert "唯一需求来源" in prompt


def test_the_import_system_prompt_keeps_the_whole_generated_schema(tmp_path):
    """Import mode changes what the model is shown, not what it owes.

    Every rule about descriptions, dependencies, ``files_to_modify`` and
    ``test_command`` is a property of the product; a lane that shipped a
    trimmed schema would produce plans the executor refuses.
    """
    generator = _import_generator(tmp_path)

    _prompt, system = generator._build_import_prompts()

    assert TASKS_SYSTEM_PROMPT in system


def test_the_import_system_prompt_declares_the_inputs_are_not_a_prd(tmp_path):
    generator = _import_generator(tmp_path)

    _prompt, system = generator._build_import_prompts()

    assert "上游输入**不是 PRD**" in system
    # And it is explicit that the shape is the caller's, not ours.
    assert "格式没有约定" in system
    assert system.index("上游输入**不是 PRD**") < system.index(TASKS_SYSTEM_PROMPT)


def test_the_import_system_prompt_names_the_requested_checkout(tmp_path):
    """The generated path names the workspaces the plan declared; the
    import path names the one the request did."""
    generator = _import_generator(tmp_path, project_dir="/srv/checkout")

    _prompt, system = generator._build_import_prompts()

    assert f"- `/srv/checkout`" in system
    assert NO_WORKSPACE_PROMPT not in system


def test_the_generated_prompts_are_unchanged_by_the_import_constructor(tmp_path):
    """A generated plan must send exactly the prompt it always sent.

    ``import_mode`` defaults off, so this is the regression guard for the
    refactor that split prompt-building out of ``generate()``: a generated
    plan whose prompt grew an import clause would be the fastest possible
    way to break every existing plan.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / "prd.md").write_text("# PRD\nminimal\n", encoding="utf-8")
    generator = TasksGenerator(_RecordingTool({}), plan_dir, self_review_enabled=False)

    prompt, system = generator._build_plan_prompts()

    assert "PRD 文档" in prompt
    assert system.startswith(TASKS_SYSTEM_PROMPT)
    assert NO_WORKSPACE_PROMPT in system


def test_the_self_review_sees_the_imported_material(tmp_path):
    """The review pass audits the list against an upstream. In import mode
    that upstream is ``import.json``, not an architecture doc that does not
    exist — a review shown nothing checks the model against itself."""
    generator = _import_generator(tmp_path)

    block = generator._load_tasks_upstream_block()

    assert "import.json" in block
    assert "add a chart to the reports page" in block


# ---------------------------------------------------------------------------
# 3 — the audit
# ---------------------------------------------------------------------------


def test_a_good_list_has_no_problems():
    tasks_data = {"tasks": [_code_task(), _code_task(id="t-2", depends_on=["t-1"])]}

    assert validate_tasks(tasks_data, PROJECT) == []


def test_a_duplicate_id_is_reported_against_both_occurrences():
    problems = validate_tasks({"tasks": [_code_task(id="dup"), _code_task(id="dup")]})

    assert problems == ["task 'dup': duplicate id, already used by tasks[0]"]


def test_a_dependency_on_a_missing_task_is_reported():
    problems = validate_tasks({"tasks": [_code_task(id="t-1", depends_on=["nope"])]})

    assert problems == [
        "task 't-1': depends_on names 'nope', which is not a task in this list"
    ]


def test_a_two_task_cycle_is_reported():
    problems = validate_tasks({"tasks": [
        _code_task(id="a", depends_on=["b"]),
        _code_task(id="b", depends_on=["a"]),
    ]})

    assert problems == ["dependency cycle between tasks: a, b"]


def test_a_task_depending_on_itself_is_reported_as_a_cycle():
    problems = validate_tasks({"tasks": [_code_task(id="a", depends_on=["a"])]})

    assert problems == ["dependency cycle between tasks: a"]


def test_a_task_blocked_downstream_of_a_cycle_is_not_called_cyclic():
    """Only the members are reported.

    Reporting the blocked node as a cycle member is what made a single
    stale edge look like an 18-task cycle on the dispatcher path; the
    graph helpers are shared with ``_ensure_acyclic`` precisely so this
    lane cannot reintroduce it.
    """
    problems = validate_tasks({"tasks": [
        _code_task(id="a", depends_on=["b"]),
        _code_task(id="b", depends_on=["a"]),
        _code_task(id="downstream", depends_on=["a"]),
    ]})

    assert problems == ["dependency cycle between tasks: a, b"]


def test_a_code_task_with_no_files_to_modify_is_reported():
    problems = validate_tasks({"tasks": [_code_task(files_to_modify=[])]})

    assert problems == [
        "task 't-1': deliverable='code' needs files_to_modify naming at "
        "least one file the task will change"
    ]


def test_a_code_task_declaring_the_read_only_sentinel_is_reported():
    """``__NO_FILE_CHANGES__`` is the author saying "read-only". A code
    task that claims it declares nothing it will change."""
    problems = validate_tasks({"tasks": [
        _code_task(files_to_modify=["__NO_FILE_CHANGES__"]),
    ]})

    assert any("needs files_to_modify" in problem for problem in problems)


def test_the_unknown_modifications_sentinel_is_a_warning_not_a_failure():
    """The other sentinel means "meant to change something, never said
    what". ``harden_for_execution`` already partitions those tasks out and
    reports them under ``harden_warnings``; refusing them here would
    refuse a plan the executor will run."""
    problems = validate_tasks({"tasks": [
        _code_task(files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL)),
    ]})

    assert problems == []


def test_a_code_task_with_no_test_command_is_reported():
    problems = validate_tasks({"tasks": [dict(_code_task(), test_command="")]})

    assert problems == [
        "task 't-1': deliverable='code' needs a test_command (or "
        "test_commands) so the change can be verified"
    ]


def test_test_commands_satisfies_the_verification_requirement():
    tasks_data = {"tasks": [dict(_code_task(), test_command="", test_commands=["pytest -q"])]}

    assert validate_tasks(tasks_data, PROJECT) == []


def test_a_report_task_needs_neither_files_nor_a_test_command():
    tasks_data = {"tasks": [{
        "id": "r-1", "title": "investigate", "description": "d",
        "deliverable": "report", "report_path": "/tmp/r.md",
    }]}

    assert validate_tasks(tasks_data, PROJECT) == []


def test_a_report_task_with_no_report_path_is_refused():
    """Named in the message, so the author knows what to add."""
    problems = validate_tasks({"tasks": [
        {
            "id": "r-1", "title": "investigate", "description": "read-only",
            "deliverable": "report",
        },
    ]})

    assert problems == [
        "task 'r-1': is not a valid task "
        "(deliverable='report' requires a non-empty report_path)"
    ]


def test_a_task_pointing_at_another_checkout_is_reported():
    problems = validate_tasks(
        {"tasks": [_code_task(project_dir="/somewhere/else")]}, PROJECT
    )

    assert problems == [
        f"task 't-1': project_dir '/somewhere/else' differs from the "
        f"request's '{PROJECT}'; every task must target one checkout"
    ]


def test_a_task_that_is_not_a_valid_model_row_is_reported_by_id():
    problems = validate_tasks(
        {"tasks": [_code_task(files_to_modify=["/etc/passwd"])]}, PROJECT
    )

    assert len(problems) == 1
    assert problems[0].startswith("task 't-1': is not a valid task (")
    assert "must be relative" in problems[0]


def test_every_problem_is_listed_not_just_the_first():
    """The route answers 422 with this list; a caller fixing their material
    needs all of it, not a first error to iterate against."""
    problems = validate_tasks({"tasks": [
        _code_task(id="a", depends_on=["ghost"]),
        _code_task(id="b", depends_on=["c"]),
        _code_task(id="c", depends_on=["b"]),
    ]}, PROJECT)

    assert problems == [
        "task 'a': depends_on names 'ghost', which is not a task in this list",
        "dependency cycle between tasks: b, c",
    ]


def test_a_payload_that_is_not_a_task_list_is_refused():
    assert validate_tasks({"nope": 1}, PROJECT) == [
        "tasks: expected an object with a 'tasks' list"
    ]


# ---------------------------------------------------------------------------
# 4 — the project_dir backfill, driven through generate()
# ---------------------------------------------------------------------------


@pytest.fixture
def no_probe(monkeypatch):
    """Skip the RED probe: it would run the generated test commands."""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


def test_a_task_that_omits_project_dir_is_given_the_requested_one(tmp_path, no_probe):
    """Driven through ``generate()``, not through the helper, because the
    claim is about the list the caller gets back."""
    generator = _import_generator(tmp_path, project_dir="/srv/checkout")
    generator.coding_tool._reply = {"tasks": [_code_task()]}

    tasks_data = generator.generate()

    assert [t["project_dir"] for t in tasks_data["tasks"]] == ["/srv/checkout"]


def test_a_task_that_names_another_checkout_keeps_it(tmp_path, no_probe):
    """Kept, not overwritten — the audit is what reports the disagreement.

    Silently rewriting it would turn a real mistake (one task pointed at
    the wrong checkout) into a plan that runs the wrong command.
    """
    generator = _import_generator(tmp_path, project_dir="/srv/checkout")
    generator.coding_tool._reply = {"tasks": [
        _code_task(id="one", project_dir="/elsewhere"),
    ]}

    tasks_data = generator.generate()

    assert tasks_data["tasks"][0]["project_dir"] == "/elsewhere"
    assert validate_tasks(tasks_data, "/srv/checkout") == [
        "task 'one': project_dir '/elsewhere' differs from the "
        "request's '/srv/checkout'; every task must target one checkout"
    ]


def test_the_backfill_leaves_a_generated_plan_alone(tmp_path, no_probe):
    """``import_mode`` is off by default; nothing new runs for the plans
    that came through the interview."""
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / "prd.md").write_text("# PRD\nminimal\n", encoding="utf-8")
    generator = TasksGenerator(
        _RecordingTool({"tasks": [_code_task()]}),
        plan_dir,
        self_review_enabled=False,
    )

    tasks_data = generator.generate()

    assert "project_dir" not in tasks_data["tasks"][0]
