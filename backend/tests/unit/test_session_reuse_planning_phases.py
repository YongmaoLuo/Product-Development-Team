"""Three planning phases re-ask in the session they just used.

What this pins
--------------
Seven call sites used to pay for a fresh session on every turn, each of
them a *loop* -- the same subject asked again, with only the delta
changed:

1. ``CodingTool.query()`` had no reuse channel at all. The contextvar +
   ``JsonSession`` handle existed only for ``query_json()``, so a loop
   built on the plain-text path re-read everything its prompt names.
2. ``Interviewer`` re-asked the model from scratch on every turn of a
   multi-turn dialogue.
3. ``TasksGenerator._iterate_on_falsifiability`` opened a new uuid per
   repair round, and each round's prompt names the upstream architecture
   and test design by path.
4. ``PRDGenerator.generate`` emitted the PRD, then had the self-review
   re-establish the whole conversation to audit what it had just
   written.
5. ``ArchGenerator.generate`` did the same, on the plain-text path.
6. ``TestDesignGenerator.generate`` did the same again.
7. ``TasksGenerator.generate`` ran its synthesis, its first self-review
   and its falsifiability rounds in three unrelated conversations.

The contract
------------
Asserted **at the argv**, not at the plumbing. ``--session-id`` vs
``--resume`` is appended to the ``claude`` command inside
``coding_tool.py``, so a test that only checked "a handle was passed
down" would pass even if the id were dropped anywhere between the
caller and the CLI -- and would pass on code that reuses nothing. Every
test here reads the real spawn, through the real launcher.

Per point:

* **Point 1** -- two ``query()`` calls under one handle: the second
  carries ``--resume <the id the first used>``.
* **Point 2** -- ``Interviewer.start()`` then ``continue_interview()``.
  Driven through a **second, freshly constructed ``Interviewer``**, which
  is what ``routes/phases.py`` actually does: the HTTP routes build a
  new ``Interviewer`` per request and carry the conversation in
  ``interview.json``. A handle hung on the instance would be empty again
  on the second request, and a test that reused one instance would pass
  on reuse that never happens in production. The CLI shape (one
  instance, a loop) is pinned separately so neither shape is left
  unchecked.
* **Point 3** -- ``_iterate_on_falsifiability`` over two or more rounds.
* **Points 4-6** -- ``PRDGenerator`` / ``ArchGenerator`` /
  ``TestDesignGenerator.generate()`` end to end, generation and
  self-review, through the real ``run_doc_self_review``.
* **Point 7** -- ``TasksGenerator.generate()`` end to end: the synthesis,
  the first self-review and both falsifiability rounds on one id.

The re-ask must carry the new ask
---------------------------------
Resuming a conversation does not put the new question into it. The
provider-switch cue (``RESUME_CONTINUATION_PROMPT``) is the *wrong* text
for a loop: it says the previous turn was cut off mid-task and asks the
model to finish that task, which would make round two answer round one's
question again. Asserted directly.

Reverse controls
----------------
The paths that must not move: a ``query()`` caller that binds no handle
still gets a fresh session per call, and the provider-switch recursion
still gets the continuation cue and still does not re-send the prompt.
Without those, "everything resumes" would satisfy every test above.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool as ct  # noqa: E402
from coding_tool import (  # noqa: E402
    ClaudeCodingTool,
    JsonSession,
    query_in_session,
)


# ---------------------------------------------------------------------------
# argv capture -- the real spawn, no subprocess
# ---------------------------------------------------------------------------


class _ArgvRecorder:
    """Stands in for ``subprocess.Popen`` and records each ``claude`` argv.

    ``reply_for(call)`` returns the assistant text for a spawn, given the
    record of that spawn. Keying the reply off the system prompt is what
    lets one recorder serve a phase that makes several different kinds of
    call -- the interview's scope pre-check is a ``query_json`` carrying
    its own system prompt, the interview turn is a ``query`` carrying
    another.

    The prompt itself travels on stdin rather than in argv, so stdin is
    captured too; that is what lets a test read back what was actually
    asked.
    """

    def __init__(self, reply_for):
        self.calls: list[dict] = []
        # One entry per spawn; a prompt reaches the CLI as several
        # ``stdin.write`` calls, so they are grouped per spawn rather
        # than flattened.
        self.prompt_by_call: list[list[str]] = []
        self._reply_for = reply_for

    @staticmethod
    def _flag_value(cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    def popen(self, cmd, *args, **kwargs):
        cmd = list(cmd)
        call = {
            "cmd": cmd,
            "resume": "--resume" in cmd,
            "session_flag": (
                "--resume" if "--resume" in cmd
                else "--session-id" if "--session-id" in cmd
                else None
            ),
            "session_id": self._flag_value(cmd, "--resume")
            or self._flag_value(cmd, "--session-id"),
            "system": self._flag_value(cmd, "--append-system-prompt"),
        }
        self.calls.append(call)
        self.prompt_by_call.append([])
        proc = MagicMock()
        # A ``result`` event is what ends the read loop; the tool reads
        # the assistant text out of it.
        proc.stdout = iter([
            json.dumps({"type": "result", "result": self._reply_for(call)}) + "\n",
        ])
        proc.stdin = MagicMock()
        proc.stdin.write.side_effect = self.prompt_by_call[-1].append
        proc.stderr = MagicMock()
        return proc

    def sent_prompts(self):
        """Every prompt written to stdin, joined per spawn."""
        return ["".join(chunks) for chunks in self.prompt_by_call]

    def where(self, system_contains=None, with_system=None):
        """The recorded spawns matching a system prompt.

        ``with_system=False`` selects the calls that pass no system
        instruction at all, which is how ``run_doc_self_review`` and
        ``_run_tasks_self_review`` call the tool.
        """
        out = []
        for call in self.calls:
            if with_system is False and call["system"] is not None:
                continue
            if system_contains is not None:
                if call["system"] is None or system_contains not in call["system"]:
                    continue
            out.append(call)
        return out


@pytest.fixture
def recorder(monkeypatch):
    """Install an argv recorder for the duration of one test."""
    def _install(reply_for):
        rec = _ArgvRecorder(reply_for)
        monkeypatch.setattr(ct.subprocess, "Popen", rec.popen)
        return rec
    return _install


def _tool():
    tool = ClaudeCodingTool()
    tool.EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 0.0
    tool.logger = MagicMock()
    return tool


# ---------------------------------------------------------------------------
# Point 1 -- query() gains the reuse channel query_json() already had
# ---------------------------------------------------------------------------


def test_the_second_query_resumes_the_session_the_first_used(recorder):
    rec = recorder(lambda call: f"reply {len(rec.calls)}")
    tool = _tool()
    session = JsonSession()

    query_in_session(tool, session, prompt="round one")
    query_in_session(tool, session, prompt="round two, with an objection")

    assert len(rec.calls) == 2
    first, second = rec.calls
    assert first["session_flag"] == "--session-id", (
        "round one opens the session under a pinned id"
    )
    assert second["session_flag"] == "--resume", (
        "round two must re-open it; a fresh session re-reads every file "
        "the prompt names"
    )
    assert second["session_id"] == first["session_id"]


def test_the_re_ask_carries_the_new_ask_not_only_a_continuation_cue(recorder):
    """The restored transcript does not contain what changed.

    ``RESUME_CONTINUATION_PROMPT`` describes a turn cut off mid-task and
    asks the model to finish *that* task. A loop's next round is a
    different ask, so sending the cue alone makes round two answer round
    one's question again -- the one outcome reuse must never produce.
    """
    rec = recorder(lambda call: "ok")
    tool = _tool()
    session = JsonSession()

    query_in_session(tool, session, prompt="the original ask")
    query_in_session(tool, session, prompt="NEW ASK: and this objection")

    assert rec.calls[1]["session_flag"] == "--resume"
    resumed_prompt = rec.sent_prompts()[1]
    assert "NEW ASK: and this objection" in resumed_prompt, (
        "what changed since the last round has to travel with the turn"
    )
    assert "cut off" not in resumed_prompt, (
        "nothing was cut off; this turn is a re-ask, not a resumption"
    )


def test_a_query_caller_that_binds_no_handle_is_unchanged(recorder):
    """The reverse control for every test above.

    If "everything resumes" were true, these would still pass. They are
    what makes the reuse above mean something: the opt-in is explicit,
    and a caller that does not opt in gets the fresh-session-per-call
    dispatch it has always had.
    """
    rec = recorder(lambda call: "ok")
    tool = _tool()

    tool.query("one unrelated question")
    tool.query("a second unrelated question")

    assert [c["session_flag"] for c in rec.calls] == ["--session-id"] * 2
    assert rec.calls[0]["session_id"] != rec.calls[1]["session_id"], (
        "two unrelated asks are two conversations"
    )


def test_the_provider_switch_still_gets_the_continuation_cue(recorder):
    """The two resume reasons must not be confused for one another.

    A provider switch is the same ask on another provider: the cue is
    right there and the prompt is deliberately not sent twice. Reusing
    that cue for a loop's re-ask is the bug guarded against in the other
    direction.
    """
    rec = recorder(lambda call: "ok")
    tool = _tool()

    tool._query_internal_m1(
        "the ask", None, None, start_time=0.0, prompt_len=7,
        resume_session="11111111-2222-3333-4444-555555555555",
    )

    assert rec.calls[0]["session_flag"] == "--resume"
    assert rec.calls[0]["session_id"] == "11111111-2222-3333-4444-555555555555"
    sent = "".join(rec.sent_prompts())
    assert "the ask" not in sent, (
        "the prompt is deliberately absent from a provider switch"
    )
    assert "cut off" in sent, "and the continuation cue is present instead"


# ---------------------------------------------------------------------------
# Point 2 -- the interview reuses one session per plan
# ---------------------------------------------------------------------------


def _interviewer_reply(n: int) -> str:
    return json.dumps({
        "dimensions": {
            "background": f"背景 {n}", "goals": f"目标 {n}",
            "scope": {"in": [], "out": []},
            "acceptance": f"验收 {n}",
        },
        "questions": [f"问题 {n}"],
        "complete": False,
    }, ensure_ascii=False)


def _precheck_reply() -> str:
    return json.dumps({
        "product_form": "software", "in_scope": [],
        "out_scope": [], "open_questions": [],
    }, ensure_ascii=False)


def _install_interviewer(recorder):
    """Answer both interview calls, telling them apart by system prompt."""
    from interviewer import (
        INTERVIEWER_SYSTEM_PROMPT, SCOPE_PRECHECK_SYSTEM_PROMPT,
    )

    probe = INTERVIEWER_SYSTEM_PROMPT[:40]
    precheck = SCOPE_PRECHECK_SYSTEM_PROMPT[:40]
    holder: dict = {}

    def _reply_for(call):
        system = call["system"] or ""
        if precheck in system:
            return _precheck_reply()
        assert probe in system, (
            f"unexpected system prompt for an interview spawn: {system[:120]!r}"
        )
        rec = holder["rec"]
        turn = len([c for c in rec.calls if probe in (c["system"] or "")])
        return _interviewer_reply(turn)

    rec = recorder(_reply_for)
    holder["rec"] = rec
    return rec


def _interview_turns(rec):
    from interviewer import INTERVIEWER_SYSTEM_PROMPT

    return rec.where(system_contains=INTERVIEWER_SYSTEM_PROMPT[:40])


def test_the_second_interview_turn_reopens_the_first_turns_session(
    tmp_path, recorder
):
    """The HTTP shape: a **new** ``Interviewer`` for the continue route.

    ``routes/phases.py`` builds one ``Interviewer`` for
    ``/api/interview`` and another for ``/api/interview/{id}/continue``,
    carrying the conversation across in ``interview.json``. A handle on
    the instance is empty again on the second request, so this test
    constructs a second one -- a test that reused a single instance
    would pass on reuse that never happens in production.
    """
    from interviewer import Interviewer

    rec = _install_interviewer(recorder)
    tool = _tool()
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True)

    Interviewer(tool, plan_dir).start("我要一个记账工具")
    Interviewer(tool, plan_dir).continue_interview("要支持多币种")

    turns = _interview_turns(rec)
    assert len(turns) == 2, f"expected 2 interview turns, got {len(turns)}"
    first, second = turns
    assert first["session_flag"] == "--session-id"
    assert second["session_flag"] == "--resume", (
        "turn two is turn one plus the user's answer; re-establishing "
        "the whole conversation from nothing is the cost this avoids"
    )
    assert second["session_id"] == first["session_id"]


def test_the_cli_loop_shape_reuses_the_session_too(tmp_path, recorder):
    """The CLI shape: ``pipeline.py`` loops on **one** instance.

    Pinned separately from the HTTP shape so that a fix reaching only
    one of the two shapes could not pass unexamined.
    """
    from interviewer import Interviewer

    rec = _install_interviewer(recorder)
    tool = _tool()
    plan_dir = tmp_path / "plans" / "P-2"
    plan_dir.mkdir(parents=True)

    interviewer = Interviewer(tool, plan_dir)
    interviewer.start("我要一个记账工具")
    interviewer.continue_interview("要支持多币种")

    turns = _interview_turns(rec)
    assert [t["session_flag"] for t in turns] == ["--session-id", "--resume"]
    assert turns[1]["session_id"] == turns[0]["session_id"]


def test_two_plans_do_not_share_an_interview_session(tmp_path, recorder):
    """The handle is keyed by plan: one interview is not another's context."""
    from interviewer import Interviewer

    rec = _install_interviewer(recorder)
    tool = _tool()
    seen = []
    for name in ("P-1", "P-3"):
        plan_dir = tmp_path / "plans" / name
        plan_dir.mkdir(parents=True)
        Interviewer(tool, plan_dir).start("我要一个记账工具")
        seen.append(_interview_turns(rec)[-1]["session_id"])

    assert all(t["session_flag"] == "--session-id" for t in _interview_turns(rec))
    assert len(set(seen)) == 2, "each plan gets its own live session"


# ---------------------------------------------------------------------------
# Point 3 -- the falsifiability repair loop reuses one session
# ---------------------------------------------------------------------------


def _task_doc() -> dict:
    return {"tasks": [{
        "id": "1", "title": "写一个记账接口", "description": "支持新增和查询",
        "files_to_modify": ["app.py"],
        "test_command": "pytest -q tests/test_api.py",
        "status": "pending",
    }]}


def test_the_falsifiability_loop_reuses_one_session_across_rounds(
    tmp_path, recorder, monkeypatch
):
    """Every round of the loop resumes the round before it.

    Driven through the real ``_iterate_on_falsifiability`` and the real
    ``_run_tasks_self_review``; only the probe (which shells out to the
    task's own test command) and the upstream file reads are stubbed.
    """
    from tasks_generator import TasksGenerator

    tasks = _task_doc()

    def _reply(call):
        # Same task ids and count, one corrected field -- which is the
        # contract the self-review prompt asks the LLM for, and what
        # makes the rewrite applicable.
        fixed = json.loads(json.dumps(tasks))
        fixed["tasks"][0]["description"] = "支持新增、查询和多币种"
        return json.dumps({"tasks": fixed["tasks"], "findings": []},
                          ensure_ascii=False)

    rec = recorder(_reply)

    tool = _tool()
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "prd.json").write_text("{}", encoding="utf-8")

    gen = TasksGenerator(tool, plan_dir)
    monkeypatch.setattr(gen, "_load_tasks_upstream_block", lambda: "")
    monkeypatch.setattr(gen, "_compute_decision_coverage_findings",
                        lambda data: [])

    # The loop stops early when a round does not *reduce* the number of
    # unusable commands. Starting at two, and having the probe report one
    # then zero, keeps it going for both of its rounds
    # (``FALSIFIABILITY_MAX_ROUNDS - 1``), so two self-review passes run.
    counts = iter([1, 0])
    monkeypatch.setattr(
        gen, "probe_falsifiability",
        lambda t, **k: {"skipped": False,
                        "vacuous": [{"command": "exit 1"}] * next(counts),
                        "unreachable": []},
    )

    falsifiability = {
        "skipped": False,
        "vacuous": [{"command": "exit 1"}, {"command": "exit 1"}],
        "unreachable": [],
    }
    gen._iterate_on_falsifiability(tasks, falsifiability)

    calls = rec.where(with_system=False)
    assert len(calls) >= 2, (
        f"expected the loop to run the self-review twice, got {len(calls)}"
    )
    assert calls[0]["session_flag"] == "--session-id", (
        "the loop's first round opens the session"
    )
    assert all(c["session_flag"] == "--resume" for c in calls[1:]), (
        "every later round re-opens it rather than re-reading the "
        "upstream architecture and test design by path"
    )
    assert len({c["session_id"] for c in calls}) == 1, (
        "one session for the loop, not one per round"
    )


# ---------------------------------------------------------------------------
# Point 4 -- the PRD's self-review audits in the generation's session
# ---------------------------------------------------------------------------


def _plan_dir_with_interview(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    interview = {
        "dimensions": {
            "background": {"value": "测试背景"},
            "goals": {"value": "测试目标"},
            "scope": {"value": "测试范围"},
            "constraints": {"value": "测试约束"},
            "acceptance": {"value": "测试验收"},
        },
        "product_form": {"form": "software"},
        "chat_history": [{"content": "测试需求"}],
    }
    (plan_dir / "interview.json").write_text(
        json.dumps(interview, ensure_ascii=False), encoding="utf-8"
    )
    return plan_dir


def _install_prd(recorder):
    """Answer the generation and the self-review with different shapes.

    The generation passes a system instruction and the self-review passes
    none, which is what tells them apart at the argv.
    """
    prd_data = {
        "title": "测试项目",
        "overview": "测试概述",
        "decision_points": [{"title": "决策点1", "context": "x", "problem": "y"}],
    }

    def _reply(call):
        if call["system"] is None:
            # The self-review's report: no rewrite, but a clean parse, so
            # the pass ends on its first attempt.
            return json.dumps(
                {"findings": [], "fixed_content": "reviewed"},
                ensure_ascii=False,
            )
        return json.dumps(prd_data, ensure_ascii=False)

    return recorder(_reply)


def test_the_prd_self_review_reuses_the_generation_session(tmp_path, recorder):
    """The audit of a document re-opens the conversation that wrote it.

    Driven through the production entry point (``PRDGenerator.generate``)
    and the real ``run_doc_self_review``, because wiring the handle into
    one of them without the other is exactly the failure this catches.
    """
    import prd_generator

    rec = _install_prd(recorder)
    tool = _tool()
    plan_dir = _plan_dir_with_interview(tmp_path)

    prd_generator.PRDGenerator(
        tool, plan_dir, self_review_enabled=True,
    ).generate()

    generation = [c for c in rec.calls if c["system"] is not None]
    review = rec.where(with_system=False)
    assert generation, "the PRD generation should have dispatched"
    assert review, "the self-review should have dispatched"

    assert generation[0]["session_flag"] == "--session-id"
    assert review[0]["session_flag"] == "--resume", (
        "the self-review audits the document the generation just wrote; "
        "re-establishing that whole conversation is the cost this avoids"
    )
    assert review[0]["session_id"] == generation[0]["session_id"]


def test_a_prd_generation_that_is_not_self_reviewed_still_answers(
    tmp_path, recorder
):
    """The opt-in is additive: with the audit off there is one dispatch."""
    import prd_generator

    rec = _install_prd(recorder)
    tool = _tool()
    plan_dir = _plan_dir_with_interview(tmp_path)

    prd_generator.PRDGenerator(
        tool, plan_dir, self_review_enabled=False,
    ).generate()

    assert rec.where(with_system=False) == [], (
        "no self-review was requested, so none should have dispatched"
    )


# ---------------------------------------------------------------------------
# Point 5 -- the arch's self-review audits in the generation's session
# ---------------------------------------------------------------------------


_ARCH_MD = """# 架构设计

### 决策点 1: 模块划分

**[C] 背景：** 需要拆分模块。
**[P] 问题：** 单文件过重。
**[E] 评估：** 单一职责更清晰。
**[A] 行动：** 每个模块一个 reason to change。

## Design Principles

- **single responsibility (SOLID)**: 每个模块只有一个变更理由。
- **twelve-factor config**: 配置来自 env var。
- **layered architecture**: layer 之间单向依赖。
- **stateless services**: 服务不持有可变状态。
- **explicit error boundaries**: 跨层返回 typed error / Result。
"""


def _install_doc_phase(recorder, doc_md):
    """Answer a doc phase's two calls: generation (system) then review (none).

    Same shape as :func:`_install_prd` -- the generation passes the
    phase's system instruction and the self-review passes none, which is
    what tells the two apart at the argv.
    """
    def _reply(call):
        if call["system"] is None:
            return json.dumps(
                {"findings": [], "fixed_content": doc_md},
                ensure_ascii=False,
            )
        return doc_md

    return recorder(_reply)


def test_the_arch_self_review_reuses_the_generation_session(
    tmp_path, recorder, monkeypatch
):
    """The audit of the arch design re-opens the conversation that wrote it.

    Driven through the production entry point (``ArchGenerator.generate``)
    and the real ``run_doc_self_review``, because wiring the handle into
    one of them without the other is exactly the failure this catches.
    """
    import arch_generator
    import self_review
    from arch_generator import ArchGenerator, ARCH_SYSTEM_PROMPT

    # ``tests/unit/test_arch_generator_hard_gate.py`` rebinds
    # ``arch_generator.run_doc_self_review`` to a no-op stub at *import*
    # time and never restores it, so any later test in the same process
    # that drives the real self-review silently gets the stub instead.
    # Rebind the real one here rather than depend on collection order.
    monkeypatch.setattr(
        arch_generator, "run_doc_self_review", self_review.run_doc_self_review,
    )

    rec = _install_doc_phase(recorder, _ARCH_MD)
    tool = _tool()
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "prd.md").write_text("# PRD\n测试 PRD", encoding="utf-8")

    ArchGenerator(tool, plan_dir, self_review_enabled=True).generate()

    generation = rec.where(system_contains=ARCH_SYSTEM_PROMPT[:40])
    review = rec.where(with_system=False)
    assert len(generation) == 1, (
        f"expected one arch render dispatch, got {len(generation)}"
    )
    assert len(review) == 1, (
        f"expected one arch self-review dispatch, got {len(review)}"
    )

    assert generation[0]["session_flag"] == "--session-id"
    assert review[0]["session_flag"] == "--resume", (
        "the self-review audits the arch design the render just wrote; "
        "re-establishing that whole conversation is the cost this avoids"
    )
    assert review[0]["session_id"] == generation[0]["session_id"]


# ---------------------------------------------------------------------------
# Point 6 -- the test design's self-review audits in the generation's session
# ---------------------------------------------------------------------------


_TEST_DESIGN_MD = """# 测试设计

### 决策点 1: 单元测试策略

**[C] 背景：** 需要覆盖核心逻辑。
**[P] 问题：** 无自动化回归。
**[E] 评估：** 单元测试先行。
**[A] 行动：** 每个决策点配一个 pytest 用例。
"""


def test_the_test_design_self_review_reuses_the_generation_session(
    tmp_path, recorder
):
    """Same contract as the arch phase, on the plain-text path."""
    from test_design_generator import (
        TestDesignGenerator, TEST_DESIGN_SYSTEM_PROMPT,
    )

    rec = _install_doc_phase(recorder, _TEST_DESIGN_MD)
    tool = _tool()
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "arch-design.md").write_text(_ARCH_MD, encoding="utf-8")
    (plan_dir / "interview.json").write_text(
        json.dumps({"dimensions": {"background": "背景", "goals": "目标"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    TestDesignGenerator(tool, plan_dir, self_review_enabled=True).generate()

    generation = rec.where(system_contains=TEST_DESIGN_SYSTEM_PROMPT[:40])
    review = rec.where(with_system=False)
    assert len(generation) == 1, (
        f"expected one test-design render dispatch, got {len(generation)}"
    )
    assert len(review) == 1, (
        f"expected one test-design self-review dispatch, got {len(review)}"
    )

    assert generation[0]["session_flag"] == "--session-id"
    assert review[0]["session_flag"] == "--resume"
    assert review[0]["session_id"] == generation[0]["session_id"]


# ---------------------------------------------------------------------------
# Point 7 -- one generate() for tasks: synthesis, review, repair rounds
# ---------------------------------------------------------------------------


def _tasks_payload():
    return {"tasks": [
        {
            "id": "1", "title": "写记账接口",
            "description": "支持新增与查询", "files_to_modify": ["app.py"],
            "test_command": "pytest -q tests/test_api.py",
        },
        {
            "id": "2", "title": "端到端验收",
            "description": "模拟用户输入并断言页面",
            "files_to_modify": ["app.py", "tests/test_e2e.py"],
            "test_command": "pytest -q tests/test_e2e.py",
        },
    ]}


def test_the_whole_tasks_generate_shares_one_session(tmp_path, recorder,
                                                    monkeypatch):
    """Synthesis, first review and every repair round are one conversation.

    Driven through the production entry point (``TasksGenerator.generate``)
    so the three steps cannot drift apart again: the synthesis is a
    ``query_json`` carrying the tasks system instruction, and both the
    first review and the falsifiability rounds are plain ``query`` calls
    that carry none.
    """
    from tasks_generator import TasksGenerator

    payload = _tasks_payload()

    def _reply(call):
        if call["system"] is not None:
            return json.dumps(payload, ensure_ascii=False)
        # Same ids, same count, no changes -- the idempotent rewrite, so
        # the self-review pass does not have to invent a correction to
        # be applicable.
        return json.dumps({"tasks": payload["tasks"], "findings": []},
                          ensure_ascii=False)

    rec = recorder(_reply)
    tool = _tool()
    plan_dir = tmp_path / "plans" / "P-1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "prd.md").write_text("# PRD", encoding="utf-8")
    (plan_dir / "arch-design.md").write_text(_ARCH_MD, encoding="utf-8")

    gen = TasksGenerator(tool, plan_dir, self_review_enabled=True)

    # Everything below shells out or reads the plan's own upstream docs;
    # none of it is the session path under test.
    monkeypatch.setattr(gen, "_run_preflight_gate", lambda: None)
    monkeypatch.setattr(gen, "_enforce_e2e_task_gate", lambda data: None)
    monkeypatch.setattr(gen, "_load_tasks_upstream_block", lambda: "")
    monkeypatch.setattr(gen, "_compute_decision_coverage_findings",
                        lambda data: [])

    # First probe (before the first review) reports two unusable
    # commands; each repair round then removes one, so the loop keeps
    # going for its full ``FALSIFIABILITY_MAX_ROUNDS - 1`` == 2 rounds.
    counts = iter([2, 1, 0])
    monkeypatch.setattr(
        gen, "probe_falsifiability",
        lambda t, **k: {"skipped": False,
                        "vacuous": [{"command": "exit 1"}] * next(counts),
                        "unreachable": []},
    )

    gen.generate()

    synthesis = [c for c in rec.calls if c["system"] is not None]
    plain = rec.where(with_system=False)
    assert len(synthesis) == 1, (
        f"expected one tasks synthesis dispatch, got {len(synthesis)}"
    )
    assert len(plain) == 3, (
        "expected the first self-review plus two falsifiability rounds, "
        f"got {len(plain)}"
    )

    assert synthesis[0]["session_flag"] == "--session-id", (
        "the synthesis opens the session"
    )
    assert all(c["session_flag"] == "--resume" for c in plain), (
        "the review and both repair rounds re-open it rather than each "
        "re-reading the upstream architecture and test design by path"
    )
    assert len({c["session_id"] for c in rec.calls}) == 1, (
        "one session for the whole generate(), not one per step"
    )
