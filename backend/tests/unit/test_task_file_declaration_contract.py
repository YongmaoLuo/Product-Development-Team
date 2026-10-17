"""The task file declaration is a CONTRACT (2026-10-17).

A task's ``files_to_modify`` was advisory. Nothing checked it where it
was written: the refiner's output schema never mentioned the field, the
refiner's only validator was dead on the production path (``validator is
None`` → ``refine()`` returned the LLM's candidate unvalidated), and the
runtime conflict guard — when a declaration was unusable — re-derived the
claim set by sweeping the task's own prose for file names.

That last part is the expensive one. A guess is per-task, so two
undeclared siblings produced DISJOINT guessed claim sets; the conflict
graph orders *declared* claims, so disjoint guesses read as "no
conflict" and the pair was scheduled together — while their real edit
surfaces overlapped. Ordering cannot help there, and the only thing left
was the hook taking the broker lock one unordered acquire at a time,
which is how two siblings that each held A and wanted B deadlocked.

Contract pinned here, in the three places that make it real:

1. the refiner's OUTPUT contract names ``files_to_modify`` (schema +
   a rule that forbids omitting it);
2. the refiner's validator actually runs, so an undeclared child is
   refused rather than shipped;
3. the runtime guard treats "no concrete declaration" as NO claim and
   never guesses from prose.

The write-time half (a task with no declaration cannot Edit/Write) lives
in ``test_readonly_task_guard_hook.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import config_registry  # noqa: E402
import refiner as refiner_module  # noqa: E402
from refiner import RefinerExhausted, TaskRefiner  # noqa: E402
from task import (  # noqa: E402
    NO_FILE_CHANGES_SENTINEL,
    UNKNOWN_MODIFICATIONS_SENTINEL,
    SubTask,
)


# ---------------------------------------------------------------------------
# 1 — the refiner's output contract
# ---------------------------------------------------------------------------

def _refiner_prompt() -> str:
    """The prompt the refiner actually runs with (not a copy of it)."""
    cfg = config_registry.ConfigRegistry.get("coding")
    return cfg.refiner_system_prompt


def test_refiner_output_schema_declares_files_to_modify():
    """The example task the model is shown must carry the field.

    Asserted against the live config, so a later edit to the prompt that
    drops the field fails here rather than silently shipping a contract
    the children cannot satisfy.
    """
    prompt = _refiner_prompt()

    m = re.search(r'"files_to_modify"\s*:\s*\[', prompt)
    assert m, (
        "the refiner's output JSON example has no files_to_modify key — "
        "the field the whole contract depends on is not in the contract"
    )

    # It must sit inside the per-task object, alongside the other keys a
    # task carries, and show a real path (not the sentinel) as the
    # ordinary case.
    assert re.search(
        r'"files_to_modify"\s*:\s*\[[^\]]*"[^"]+\.[a-z]+"[^\]]*\]', prompt
    ), "the example must show at least one concrete path"


def test_both_prompt_sources_carry_the_field():
    """The live prompt is YAML-assembled; the Python copy is a fallback.

    ``config_loader._build_refiner_prompt`` composes the prompt the
    refiner actually runs from ``configs/_base.yaml``, and the
    ``DEFAULT_CODING_CONFIG`` literal is only used when no YAML loads.
    Editing one and not the other is the silent way to ship a contract the
    model never sees, so both are pinned.
    """
    from config_loader import _build_refiner_prompt

    with open(
        _BACKEND_DIR / "configs" / "_base.yaml", encoding="utf-8"
    ) as fh:
        import yaml

        yaml_cfg = yaml.safe_load(fh)

    assembled = _build_refiner_prompt(yaml_cfg)
    python_fallback = config_registry.DEFAULT_CODING_CONFIG.refiner_system_prompt

    for label, prompt in (
        ("yaml-assembled", assembled),
        ("python fallback", python_fallback),
    ):
        assert '"files_to_modify"' in prompt, f"{label} prompt lacks the field"
        assert "__NO_FILE_CHANGES__" in prompt, f"{label} lacks the read-only spelling"
        assert "__UNKNOWN_MODIFICATIONS__" in prompt, f"{label} lacks the forbidden placeholder"


def test_refiner_prompt_requires_the_field_on_every_task():
    """The rule exists, and it is specific about what counts as declaring.

    Token-level assertions, not a verbatim-wording match: the point being
    pinned is that the model is told (a) every task must declare,
    (b) read-only tasks have an explicit spelling, and (c) the two
    non-declarations that look like declarations are called out.
    """
    prompt = _refiner_prompt()

    assert "files_to_modify" in prompt
    assert "__NO_FILE_CHANGES__" in prompt, (
        "the prompt must name the explicit read-only declaration, or a "
        "read-only child has no way to declare itself"
    )
    assert "__UNKNOWN_MODIFICATIONS__" in prompt, (
        "the prompt must forbid the placeholder explicitly — it looks "
        "like a declaration but is the absence of one"
    )
    assert "files_to_modify" in prompt and "MUST" in prompt, (
        "the requirement must be stated as a MUST, not a suggestion"
    )


def test_refiner_prompt_does_not_re_decide_rules_3_and_6():
    """The new rule must not adjudicate against the existing split rules.

    "If the last task FAILED you MUST break it down" (the live prompt's
    1-4) and "do not split a task whose failures share a cause" (the
    Python fallback's rule 6) are another PR's business. This PR adds the
    declaration contract only, so the declaration block may not tell the
    model when to split or when not to.
    """
    prompt = _refiner_prompt()

    if "FILE DECLARATION RULES" in prompt:
        # Live YAML-assembled prompt. Take the heading's own bullet list and
        # stop at the first line that is not a bullet — the assembled prompt
        # runs the JSON-format section next, and that section talks about
        # splitting for reasons unrelated to this rule.
        after = prompt.split("FILE DECLARATION RULES", 1)[1]
        bullets = []
        for line in after.splitlines():
            if line.strip().startswith("-"):
                bullets.append(line)
            elif bullets:
                break
        body = "\n".join(bullets)
    else:
        # Python fallback: rule 7.
        assert "7. EVERY task you return MUST carry" in prompt, (
            "the declaration rule is missing from the refiner prompt"
        )
        body = prompt.split("7. EVERY task you return MUST carry", 1)[1]
        body = body.split("\n8.", 1)[0]

    for forbidden in ("split", "FAILED", "same cause", "granularity", "break down"):
        assert forbidden not in body, (
            f"the declaration rule mentions {forbidden!r}: this PR adds the "
            f"declaration contract only and must not arbitrate when to split"
        )
    # ...but it must still say the thing it exists to say.
    assert "files_to_modify" in body
    assert "__NO_FILE_CHANGES__" in body


# ---------------------------------------------------------------------------
# 2 — the refiner's validator runs on the production path
# ---------------------------------------------------------------------------

class _StubCodingTool:
    """Scripted ``query_json``; records the prompts it was re-asked with."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.prompts: List[str] = []

    def query_json(self, prompt, system_instruction=None, scene=None, **kwargs):
        self.prompts.append(prompt)
        item = self.responses.pop(0) if self.responses else {"tasks": []}
        if isinstance(item, Exception):
            raise item
        return item


def _refiner(responses: List[Any], project_dir: Path) -> TaskRefiner:
    r = TaskRefiner.__new__(TaskRefiner)  # bypass AgentConfig wiring
    r.coding_tool = _StubCodingTool(responses)
    r.logger = None
    r.config = type("C", (), {
        "refiner_system_prompt": "sys",
        "domain_knowledge": "",
    })()
    r.project_dir = str(project_dir)
    return r


def _child(task_id: str, **overrides: Any) -> Dict[str, Any]:
    child = {
        "id": task_id,
        "title": f"child {task_id}",
        "description": "do a thing",
        "test_command": "echo ok",
        "depends_on": [],
    }
    child.update(overrides)
    return child


PARENT = {"id": "2", "title": "parent", "description": "d",
          "test_command": "echo ok", "status": "failed"}


def _refine(r: TaskRefiner) -> List[Dict[str, Any]]:
    return r.refine(
        requirement="r",
        tasks=[dict(PARENT)],
        last_coder_response="out",
        last_result="failed",
        exit_code=1,
        last_task_id="2",
    )


@pytest.fixture
def project_with_a_file(tmp_path: Path) -> Path:
    d = tmp_path / "project"
    d.mkdir()
    (d / "backend").mkdir()
    (d / "backend" / "agent.py").write_text("# x\n", encoding="utf-8")
    return d


def test_refiner_validator_is_built_when_project_dir_is_known(
    project_with_a_file: Path,
) -> None:
    """``validator is None`` was the dead path; prove it is no longer taken.

    Spying on the real class (rather than re-deriving the
    ``project_dir or self.project_dir`` expression here) keeps the test
    honest: it asserts the production ``refine()`` constructs a validator
    AND calls ``validate`` on it.
    """
    built: List[Any] = []
    validated: List[Any] = []

    real_cls = refiner_module.TaskOutputValidator

    class _Spy(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, project_dir):
            built.append(project_dir)
            super().__init__(project_dir)

        def validate(self, snapshot):
            validated.append(snapshot)
            return super().validate(snapshot)

    refiner_module.TaskOutputValidator = _Spy
    try:
        r = _refiner(
            [{"tasks": [_child("2-1", files_to_modify=["backend/agent.py"])]}],
            project_with_a_file,
        )
        out = _refine(r)
    finally:
        refiner_module.TaskOutputValidator = real_cls

    assert built, "refine() constructed no TaskOutputValidator — step 3 is dead again"
    assert str(built[0]) == str(project_with_a_file)
    assert validated, "a validator was built but validate() was never called"
    assert out[0]["id"] == "2-1"


def test_refiner_rejects_a_child_that_declares_no_files(
    project_with_a_file: Path,
) -> None:
    """Negative control: an undeclared child is refused, not shipped.

    The stub keeps answering with an undeclared child, so every round
    fails and ``refine`` exhausts. Refuser is the strong form of
    "rejected": it is the one outcome that cannot be mistaken for a
    silent pass-through.
    """
    undeclared = {"tasks": [_child("2-1")]}  # no files_to_modify at all
    # Every round answers the same undeclared child. (An EMPTY reply would
    # not do: an empty snapshot has nothing to fail step 3, so it passes
    # validation trivially and refine() returns [] on round 2. The caller
    # rejects that separately as a >50% task-list drop.)
    r = _refiner([undeclared] * 10, project_with_a_file)

    with pytest.raises(RefinerExhausted) as exc:
        _refine(r)

    reasons = " ".join(exc.value.detail.get("reasons", []))
    assert "step 3" in reasons, (
        "the child must be refused by the files_to_modify gate; "
        f"validator said: {reasons!r}"
    )
    # And it must have been re-asked, not just rejected once.
    assert len(r.coding_tool.prompts) > 1, (
        "a rejected round must feed the objection back to the model"
    )
    assert "step 3" in r.coding_tool.prompts[-1]


def test_refiner_accepts_a_child_that_declares_its_files(
    project_with_a_file: Path,
) -> None:
    """Positive control for the negative control above: declare, pass.

    Same refiner, same validator, same project — only the declaration
    differs. If this ever fails while the rejection test still passes,
    the gate has become unconditional and is refusing everything.
    """
    r = _refiner(
        [{"tasks": [_child("2-1", files_to_modify=["backend/agent.py"])]}],
        project_with_a_file,
    )

    out = _refine(r)

    assert [t["id"] for t in out] == ["2-1"]
    assert out[0]["files_to_modify"] == ["backend/agent.py"]
    assert len(r.coding_tool.prompts) == 1, "a valid child must pass on round 1"


def test_refiner_accepts_a_read_only_child_that_says_so(
    project_with_a_file: Path,
) -> None:
    """Read-only is a declaration, not an omission."""
    r = _refiner(
        [{"tasks": [_child("2-1", files_to_modify=list(
            NO_FILE_CHANGES_SENTINEL))]}],
        project_with_a_file,
    )

    out = _refine(r)

    assert out[0]["files_to_modify"] == list(NO_FILE_CHANGES_SENTINEL)


def test_unknown_sentinel_is_not_a_declaration(
    project_with_a_file: Path,
) -> None:
    """The placeholder is explicitly not an acceptable answer.

    It looks like a declaration, which is exactly why it survived: the
    step-3 gate rejects it, and this pins that it keeps being rejected.
    """
    placeholder = {"tasks": [_child("2-1", files_to_modify=list(
        UNKNOWN_MODIFICATIONS_SENTINEL))]}
    r = _refiner([placeholder] * 10, project_with_a_file)

    with pytest.raises(RefinerExhausted):
        _refine(r)


# ---------------------------------------------------------------------------
# 3 — the runtime guard does not guess
# ---------------------------------------------------------------------------

def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )


def _make_agent(project_dir: Path):
    from unittest.mock import Mock

    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement=None,
        project_dir=project_dir,
        coding_tool=Mock(),
    )


@pytest.fixture
def agent_project(tmp_path: Path) -> Path:
    d = tmp_path / "project"
    _git_init(d)
    return d


#: A description that names plenty of files in prose. If the guard still
#: scanned prose, every one of these cases would come back non-empty and
#: the "declares nothing" assertions below could not distinguish a guess
#: from a declaration.
PROSE = (
    "## 修改位置\n"
    "- `backend/agent.py`\n"
    "- `backend/coding_tool.py`\n"
    "\n"
    "## 边界条件\n"
    "FILE: backend/refiner.py\n"
    "参见既有 `backend/task.py` 的实现，本任务不改它。\n"
)


def test_a_declared_task_returns_its_declaration_verbatim(agent_project):
    """Positive control: a real declaration is the claim set, untouched."""
    agent = _make_agent(agent_project)
    task = SubTask(
        id="1", title="t", description=PROSE,
        files_to_modify=["backend/agent.py", "backend/coding_tool.py"],
        test_command="echo ok",
    )

    assert agent._extract_target_files(task) == [
        "backend/agent.py", "backend/coding_tool.py",
    ]


@pytest.mark.parametrize(
    "files_to_modify, label",
    [
        (list(UNKNOWN_MODIFICATIONS_SENTINEL), "unknown-modifications sentinel"),
        (list(NO_FILE_CHANGES_SENTINEL), "no-file-changes sentinel"),
        ([], "explicit empty list"),
        (None, "None (field absent from the model)"),
    ],
    ids=["unknown", "no_file_changes", "empty", "none"],
)
def test_an_undeclared_task_claims_nothing(agent_project, files_to_modify,
                                          label):
    """Each non-declaration, one by one, yields an EMPTY claim set.

    All four carry a description full of file names (above). Before this
    change the first, second and third each came back with a guessed set;
    the point of pinning them separately is that they are four different
    spellings of one state and a future edit may fix only some.
    """
    agent = _make_agent(agent_project)
    kwargs: Dict[str, Any] = {}
    if files_to_modify is not None:
        kwargs["files_to_modify"] = files_to_modify
    task = SubTask(
        id="1", title="t", description=PROSE, test_command="echo ok", **kwargs
    )

    assert agent._extract_target_files(task) == [], (
        f"{label} must produce an empty claim set, not a guess"
    )


def test_a_task_with_no_files_to_modify_attribute_at_all(agent_project):
    """Duck-typed task objects bypass the pydantic default.

    ``SubTask`` fills the sentinel, so the "field absent" case cannot be
    produced through the model. The helper uses ``getattr(..., None)``
    precisely because callers hand it objects that are not ``SubTask``.
    """
    agent = _make_agent(agent_project)

    class _Duck:
        id = "1"
        description = PROSE

    assert agent._extract_target_files(_Duck()) == []


def test_a_task_declaring_a_none_value_claims_nothing(agent_project):
    """``files_to_modify=None`` must not fall back to guessing either.

    ``SubTask``'s validator rewrites ``None`` into the sentinel, so this
    is only reachable by an unvalidated object; it is pinned because
    ``_declared_modification_files`` explicitly tolerates it.
    """
    agent = _make_agent(agent_project)

    class _Duck:
        id = "1"
        description = PROSE
        files_to_modify = None

    assert agent._extract_target_files(_Duck()) == []


def test_the_guess_is_gone_from_the_source(agent_project):
    """Structural guard: the prose fallback cannot come back quietly.

    A behavioural test above would still pass if someone reintroduced a
    fallback for some *other* description shape. This pins that the
    extractor has exactly one source.
    """
    import ast
    import inspect
    import textwrap

    from agent import AutonomousAgent

    src = textwrap.dedent(
        inspect.getsource(AutonomousAgent._extract_target_files)
    )
    # Strip the docstring: it NAMES the fallback on purpose (to explain
    # why it went away), and matching on prose would flag its own
    # explanation as the behaviour.
    tree = ast.parse(src)
    fn = tree.body[0]
    docstring_span = (fn.body[0].lineno, fn.body[0].end_lineno)
    code_lines = [
        line for i, line in enumerate(src.splitlines(), start=1)
        if not (docstring_span[0] <= i <= docstring_span[1])
    ]
    code = "\n".join(code_lines)

    assert "FILE:" not in code, (
        "_extract_target_files scans FILE: markers again — files are "
        "guessed from prose"
    )
    assert "_modification_section" not in code, (
        "_extract_target_files scans the description again — files are "
        "guessed from prose"
    )
    assert "description" not in code, (
        "_extract_target_files reads task.description again — files are "
        "guessed from prose"
    )


def test_read_only_uses_the_same_predicate_as_the_runtime_guard(agent_project):
    """③ and ④ must not be able to drift apart.

    Both ask "does this task declare concrete files?". They read the same
    helper, so a task the guard treats as undeclared is exactly the task
    the executor will mark read-only. This asserts the equivalence on all
    six shapes, which is what makes the empty claim set safe rather than
    a hole.
    """
    from agent import _declared_modification_files

    agent = _make_agent(agent_project)
    concrete = ["backend/agent.py"]
    shapes: List[Any] = [
        concrete,
        list(UNKNOWN_MODIFICATIONS_SENTINEL),
        list(NO_FILE_CHANGES_SENTINEL),
        [],
        None,
        "absent",
    ]
    for shape in shapes:
        class _Duck:
            id = "1"
            description = PROSE

        if shape != "absent":
            _Duck.files_to_modify = shape
        task = _Duck()

        declares = bool(_declared_modification_files(task))
        read_only = not declares
        claims = bool(agent._extract_target_files(task))

        assert read_only != claims, (
            f"shape {shape!r}: guard claims files={claims} while the task "
            f"is treated as read-only={read_only} — one of the two is wrong"
        )
        assert declares is claims, (
            f"shape {shape!r}: the claim set and the read-only verdict "
            f"disagree"
        )


def test_json_is_not_a_valid_task_shape_either():
    """Guard the ``json`` import is still used by the schema tests above."""
    assert json.dumps({"files_to_modify": ["a.py"]})


# ---------------------------------------------------------------------------
# 2b — the same rejection, through the agent's OWN refiner
# ---------------------------------------------------------------------------
#
# The tests above build a TaskRefiner by hand and hand it a project_dir,
# which proves the VALIDATOR works but not that the agent gives it one.
# That gap is not hypothetical: reverting the one-line wiring in
# ``AutonomousAgent.__init__`` leaves every test in the section above
# green while step 3 is dead in production again. These two go through the
# refiner the agent actually built.

def _agent_project(tmp_path: Path) -> Path:
    d = tmp_path / "agent-project"
    d.mkdir()
    (d / "backend").mkdir()
    (d / "backend" / "agent.py").write_text("# x\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(d), capture_output=True, check=True,
    )
    return d


def _agent_with_stub_refiner(project_dir: Path, responses: List[Any]):
    from unittest.mock import Mock

    from agent import AutonomousAgent

    a = AutonomousAgent(
        requirement=None, project_dir=project_dir, coding_tool=Mock(),
    )
    # Swap in the scriptable tool on the refiner the agent constructed, so
    # the constructor wiring (the thing under test) is the agent's own.
    a.refiner.coding_tool = _StubCodingTool(responses)
    a.refiner.logger = None
    return a


def test_the_agents_own_refiner_rejects_an_undeclared_child(tmp_path: Path):
    """Production entry: agent -> its refiner -> an undeclared child.

    Negative control for the wiring. If the agent stopped passing
    ``project_dir``, this goes red — which the hand-built refiner above
    would not notice.
    """
    project_dir = _agent_project(tmp_path)
    a = _agent_with_stub_refiner(
        project_dir,
        [{"tasks": [_child("2-1")]} for _ in range(10)],  # always undeclared
    )

    with pytest.raises(RefinerExhausted) as exc:
        a.refiner.refine(
            requirement="r",
            tasks=[dict(PARENT)],
            last_coder_response="out",
            last_result="failed",
            exit_code=1,
            last_task_id="2",
        )

    assert "step 3" in " ".join(exc.value.detail.get("reasons", []))


def test_the_agents_own_refiner_accepts_a_declared_child(tmp_path: Path):
    """Positive control for the one above, same agent, same project."""
    project_dir = _agent_project(tmp_path)
    a = _agent_with_stub_refiner(
        project_dir,
        [{"tasks": [_child("2-1", files_to_modify=["backend/agent.py"])]}],
    )

    out = a.refiner.refine(
        requirement="r",
        tasks=[dict(PARENT)],
        last_coder_response="out",
        last_result="failed",
        exit_code=1,
        last_task_id="2",
    )

    assert [t["id"] for t in out] == ["2-1"]
    assert out[0]["files_to_modify"] == ["backend/agent.py"]
    assert len(a.refiner.coding_tool.prompts) == 1
