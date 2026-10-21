"""``POST /api/plan/{plan_id}/revise`` — the fast lane's second door.

The lane's promise has two halves and this file pins the second one.
``POST /api/plan/import`` is the way in; a caller who reads the result
and says "not quite" needs a way to say it **about the list that is
already there**. Re-importing is not that way: the lane refuses to
overwrite a plan directory, so a second import lands in ``<id>-2`` and
the caller is left with two plans and one of them is wrong.

So the revise route has four promises, each pinned here from the
production entry point (a real request against the mounted app, never
a direct call into ``routes.fast_lane.revise_plan``):

1. **It lands where the plan already is.** The same directory, the same
   ``tasks.json``, no ``<id>-2`` anywhere on disk — and the plan's
   phase and execution state untouched, because a revision is a change
   to the work, not to the plan's place in the world.
2. **It is the same conversation.** The revision resumes the session
   the import ran in, so the model is answering a follow-up question
   about a list it has already seen rather than being interviewed from
   scratch. The id on the handle the generator is given is the evidence.
3. **Nothing to revise is refused.** A plan with no ``tasks.json`` has
   no list, and the answer is 409 — not a fresh plan silently created
   beside it.
4. **A rejected revision changes nothing.** The audit is the import's
   audit, and a list that fails it is refused with the problems to fix
   — and the plan's ``tasks.json`` is byte-for-byte what it was.

No real LLM is involved: ``server.create_coding_tool`` is monkeypatched
per-test to return a fake that scripts the calls the generator makes.

Isolation is the autouse ``isolated_plans_dir`` fixture: ``PLANS_DIR``
and the state database are both redirected at ``tmp_path``, so this file
never reads the operator's live ``plans/`` tree or ``state.db``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from request_guard import REQUEST_HEADER  # noqa: E402
from routes import fast_lane as routes_fast_lane  # noqa: E402


client = TestClient(server.app)


#: The session id the fake tool mints. Fixed, so a test can assert that
#: the id a revision resumed is the id the import created.
FAKE_SESSION_ID = "sess-fake-0001"


# ---------------------------------------------------------------------------
# The fake coding tool
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; scripts the calls the generator makes.

    Two behaviours the real tool has and a plain stub does not, both of
    which this lane's contract depends on:

    * it **binds the session id** onto the handle the generator holds.
      The real tool learns the id from the provider's system-init event
      and writes it back onto the handle, so the caller's next question
      can resume it. A stub that leaves the handle empty would make
      every plan look like it had no conversation to resume, and the
      reuse this lane is built around could not be observed at all.
    * it **echoes the synthesis payload by reference** in the
      self-review reply, which is what the generator mutates in place
      while it adds ``depends_on`` and resolves ``files_to_modify`` — so
      the fake always reviews the list as it stands, and the review
      comes back as the idempotent "reviewed it, found nothing" rewrite
      the generator already accepts.

    Replies are queued: one per ``query_json``, so an import and the
    revisions that follow it can each answer with a different list.
    """

    def __init__(self, *replies, session_id: str = FAKE_SESSION_ID):
        self._replies = list(replies)
        self._current = None
        self.session_id = session_id
        self.json_calls = []
        self.query_calls = []
        #: The session id on the handle at each dispatch — what the real
        #: tool would have resumed (``None`` means "opened a fresh one").
        self.seen_session_ids = []

    def _bind_session(self) -> None:
        from coding_tool import _JSON_SESSION

        handle = _JSON_SESSION.get()
        self.seen_session_ids.append(handle.session_id if handle else None)
        if handle is not None and not handle.session_id:
            # The real tool mints an id for a fresh session and leaves an
            # existing one alone — a resumed session keeps its id.
            handle.session_id = self.session_id

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        self._bind_session()
        reply = self._replies.pop(0) if self._replies else {"tasks": []}
        self._current = reply
        return reply

    def query(self, prompt: str) -> str:
        self.query_calls.append(prompt)
        self._bind_session()
        return json.dumps(
            {"findings": [], "fixed_content": self._current},
            ensure_ascii=False,
        )


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    """The RED probe would run the generated test commands for real."""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def checkout(tmp_path):
    """A real directory for the tasks to target.

    ``harden_for_execution`` refuses a plan whose ``project_dir`` does not
    exist, and the route runs that gate — so a fixture that pointed at a
    made-up path would be testing the 400 branch on the happy path.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    return path


def _tasks_payload(**overrides):
    """A task list a model would plausibly emit, and one that passes."""
    payload = {
        "requirement": "add a daily grouped bar chart to the reporting view",
        "tasks": [
            {
                "id": "T1",
                "title": "add the chart renderer",
                "description": "## 背景\nthe reporting view has no chart",
                "files_to_modify": ["backend/chart.py"],
                "test_command": "pytest -q backend/tests/test_chart.py",
            },
            {
                "id": "T2",
                "title": "wire the renderer into the view",
                "description": "## 目标\nmount the renderer",
                "files_to_modify": ["backend/reporting_view.py"],
                "test_command": "pytest -q",
                "depends_on": ["T1"],
            },
        ],
    }
    payload.update(overrides)
    return payload


def _revised_payload():
    """The list a revision answers with: the same work, split finer."""
    return _tasks_payload(tasks=[
        {
            "id": "T1",
            "title": "add the chart renderer",
            "description": "## 背景\nthe reporting view has no chart",
            "files_to_modify": ["backend/chart.py"],
            "test_command": "pytest -q backend/tests/test_chart.py",
        },
        {
            "id": "T2",
            "title": "wire the renderer into the view",
            "description": "## 目标\nmount the renderer",
            "files_to_modify": ["backend/reporting_view.py"],
            "test_command": "pytest -q",
            "depends_on": ["T1"],
        },
        {
            "id": "T3",
            "title": "cover the grouped axis with a test",
            "description": "## 目标\nassert the grouping",
            "files_to_modify": ["backend/a.py"],
            "test_command": "pytest -q",
            "depends_on": ["T2"],
        },
    ])


def _cyclic_payload():
    """A list the audit refuses: two tasks depending on each other."""
    return _tasks_payload(tasks=[
        {
            "id": "T1", "title": "a", "description": "d",
            "files_to_modify": ["backend/a.py"], "test_command": "pytest -q",
            "depends_on": ["T2"],
        },
        {
            "id": "T2", "title": "b", "description": "d",
            "files_to_modify": ["backend/b.py"], "test_command": "pytest -q",
            "depends_on": ["T1"],
        },
    ])


def _install(monkeypatch, *replies):
    """Point ``server.create_coding_tool`` at a fake and hand it back."""
    tool = FakeCodingTool(*replies)
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


@pytest.fixture
def plans_root(tmp_path):
    """The plans tree this test writes into (already redirected)."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def session_spy(monkeypatch):
    """Record the ``JsonSession`` handle the route hands the generator.

    The claim under test is about the *handle*, not about the file that
    remembers it: a route that read the id and then started a fresh
    conversation anyway would still leave a plausible id on disk. So
    the handle the generator is actually given is observed, before the
    call, and its id is what the assertions read.
    """
    seen = []
    original = routes_fast_lane.TasksGenerator

    class SpyGenerator(original):
        def revise_from_feedback(self, session, feedback):
            seen.append({
                "session_id": session.session_id,
                "handle": session,
                "feedback": feedback,
            })
            return super().revise_from_feedback(session, feedback)

    monkeypatch.setattr(routes_fast_lane, "TasksGenerator", SpyGenerator)
    return seen


def _import(tool_replies, checkout, monkeypatch, **overrides):
    """Import a plan through the production route.

    Returns ``(plan_id, tool)``: the id, and the fake that answered the
    import — the import is the run that *opened* the session, so its
    tool is the one that shows a fresh handle.
    """
    tool = _install(monkeypatch, *tool_replies)
    body = {"project_dir": str(checkout), "content": "add a chart"}
    body.update(overrides)
    response = client.post("/api/plan/import", json=body)
    assert response.status_code == 200, response.text
    return response.json()["plan_id"], tool


def _revise(plan_id, feedback, **extra):
    body = {"feedback": feedback}
    body.update(extra)
    return client.post(f"/api/plan/{plan_id}/revise", json=body)


# ---------------------------------------------------------------------------
# 1 — the revision lands in the plan that is already there
# ---------------------------------------------------------------------------


def test_a_revision_lands_in_the_same_plan_directory(
    plans_root, checkout, monkeypatch, state_db_reader
):
    """One plan, one directory, one ``tasks.json``.

    The failure this guards against is the one the caller cannot see:
    a revise that quietly created ``<id>-2`` would answer 200, and the
    caller would believe their plan changed while a second, correct
    plan sat next to it.
    """
    plan_id, _import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    _install(monkeypatch, _revised_payload())

    response = _revise(plan_id, "把测试也拆开，单独一个任务")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["plan_id"] == plan_id
    assert payload["revised"] is True

    # Exactly one directory, and it is the import's. No ``<id>-2``.
    assert sorted(p.name for p in plans_root.iterdir()) == [plan_id]
    assert not (plans_root / f"{plan_id}-2").exists()

    # The revised list is the one on disk, in that directory.
    saved = json.loads(
        (plans_root / plan_id / "tasks.json").read_text(encoding="utf-8")
    )
    assert [row["id"] for row in saved["tasks"]] == ["T1", "T2", "T3"]
    assert payload["tasks"] == saved["tasks"]

    # The plan's place in the world is unchanged: same phase, nothing
    # running, no execution row.
    assert state_db_reader.routing(plan_id)["current_phase"] == "ready"
    assert state_db_reader.execution(plan_id) is None
    assert plan_id not in server._execution_state


def test_a_revision_that_fails_the_audit_leaves_the_plan_untouched(
    plans_root, checkout, monkeypatch
):
    """A refused revision changes nothing at all.

    Byte-for-byte, not "roughly the same": the generator's writes are
    deferred until the result has been audited, so the file the
    executor reads is still the file the executor read before.
    """
    plan_id, _import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    tasks_file = plans_root / plan_id / "tasks.json"
    before = tasks_file.read_bytes()
    _install(monkeypatch, _cyclic_payload())

    response = _revise(plan_id, "让 T1 依赖 T2，T2 依赖 T1")

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["problems"] == ["dependency cycle between tasks: T1, T2"]
    assert body["plan_id"] == plan_id

    assert tasks_file.read_bytes() == before


# ---------------------------------------------------------------------------
# 2 — the revision is the same conversation
# ---------------------------------------------------------------------------


def test_two_revisions_resume_the_one_session(
    plans_root, checkout, monkeypatch, session_spy
):
    """The second revision asks in the session the first one used.

    Three observations, because "the session was reused" has more than
    one way to be true: the handle the generator was given carried the
    same id both times, that id is the one the import minted and
    remembered, and the model was actually resumed in it.
    """
    plan_id, import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    tool = _install(monkeypatch, _revised_payload(), _revised_payload())

    first = _revise(plan_id, "第一轮：把测试拆开")
    second = _revise(plan_id, "第二轮：再补一个边界用例")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["session_reused"] is True
    assert second.json()["session_reused"] is True

    # The handle the generator was given: the same id both times, and it
    # is the id the import created.
    ids = [entry["session_id"] for entry in session_spy]
    assert ids == [FAKE_SESSION_ID, FAKE_SESSION_ID]

    # The id is remembered on disk, one line, in the plan directory.
    remembered = (plans_root / plan_id / ".import_session").read_text(
        encoding="utf-8"
    )
    assert remembered.strip() == FAKE_SESSION_ID

    # And the model was resumed in it: every dispatch of the revisions
    # saw the id, while the import's first dispatch saw none — that one
    # is the run that opened the session.
    assert import_tool.seen_session_ids[0] is None
    assert tool.seen_session_ids
    assert set(tool.seen_session_ids) == {FAKE_SESSION_ID}


def test_the_revision_is_asked_with_the_list_and_the_feedback(
    plans_root, checkout, monkeypatch
):
    """The model is shown the list as it stands, and the note about it.

    A revision that did not carry the current list would be a second
    import wearing a revision's clothes, and a revision that did not
    carry the feedback would be a no-op the caller paid for.
    """
    plan_id, _import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    tool = _install(monkeypatch, _revised_payload())

    response = _revise(plan_id, "把测试也拆开，单独一个任务")

    assert response.status_code == 200, response.text
    prompt = tool.json_calls[0]["prompt"]
    assert "把测试也拆开，单独一个任务" in prompt
    assert "T1" in prompt and "T2" in prompt
    # The whole list, not a diff of it.
    assert "完整的" in prompt


def test_a_revision_can_replace_the_imported_material(
    plans_root, checkout, monkeypatch
):
    """New upstream material can arrive with the feedback.

    The material is replaced the way the import stores it — verbatim,
    in the same file — so the revision is audited against what the
    caller just sent rather than against what they sent first.
    """
    plan_id, _import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    _install(monkeypatch, _revised_payload())

    response = _revise(
        plan_id, "按新材料重排", content={"ticket": "PROJ-9", "notes": "new shape"},
    )

    assert response.status_code == 200, response.text
    stored = json.loads(
        (plans_root / plan_id / "import.json").read_text(encoding="utf-8")
    )
    assert stored == {"ticket": "PROJ-9", "notes": "new shape"}


# ---------------------------------------------------------------------------
# 3 — there has to be something to revise
# ---------------------------------------------------------------------------


def test_a_plan_without_tasks_cannot_be_revised(plans_root):
    """No ``tasks.json`` means no list — and no second plan either.

    The 409 is the point: creating a plan here would be the very thing
    the lane refuses to do on the import side, where a second import
    lands in ``<id>-2`` and the caller is left with two plans.
    """
    (plans_root / "20260101-empty").mkdir(parents=True)

    response = _revise("20260101-empty", "改一下")

    assert response.status_code == 409, response.text
    assert response.json()["error"] == "this plan has no tasks to revise"
    # Nothing was created next to it.
    assert list(plans_root.iterdir()) == [plans_root / "20260101-empty"]


def test_a_plan_that_does_not_exist_cannot_be_revised(plans_root):
    """A plan that is not there is a 404, not a 409.

    The two are different answers to different questions — "no such
    plan" versus "such a plan, but no tasks in it" — and a caller
    retrying with a right id needs to be able to tell them apart.
    """
    response = _revise("20260101-missing", "改一下")

    assert response.status_code == 404, response.text
    assert response.json()["error"] == "Plan not found"
    assert list(plans_root.iterdir()) == []


def test_a_revision_without_feedback_is_refused(plans_root, checkout, monkeypatch):
    """A revision with nothing to say is not a revision.

    It would ask the model to redo the list for no reason and charge
    the caller for the answer, so it is refused before the model is
    called at all.
    """
    plan_id, _import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    tool = _install(monkeypatch, _revised_payload())

    response = _revise(plan_id, "   ")

    assert response.status_code == 400, response.text
    assert any(
        "feedback is required" in problem
        for problem in response.json()["problems"]
    )
    assert tool.json_calls == []


# ---------------------------------------------------------------------------
# 4 — the route is really there
# ---------------------------------------------------------------------------


def test_the_revise_route_is_registered_on_the_app(plans_root):
    """A module that is never mounted answers none of the above."""
    paths = [route.path for route in server.iter_app_routes()]

    assert "/api/plan/{plan_id}/revise" in paths


def test_the_revise_route_is_behind_the_request_guard(plans_root, checkout, monkeypatch):
    """No ``X-PDT-Request``, no revision — the middleware, not the handler.

    Asserted on the mounted route rather than on a helper, because the
    failure this guards against is precisely a route registered outside
    the guard's reach.
    """
    plan_id, _import_tool = _import(
        [_tasks_payload()], checkout, monkeypatch, title="Fast lane revise"
    )
    raw = TestClient(server.app)
    raw.headers.pop(REQUEST_HEADER, None)

    response = raw.post(f"/api/plan/{plan_id}/revise", json={"feedback": "改一下"})

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
