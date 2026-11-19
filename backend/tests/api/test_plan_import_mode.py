"""``POST /api/plan/import`` declares the plan's mode -- and keeps it.

A plan's mode is the difference between "exercise the pipeline" and "run
it for real".  The fast lane is the short way in, so it is also the place
where the mode has to be *declared*: a caller that imports material
without saying which mode it means gets the mode that is safe to assume
(``production``), and a caller that says ``test`` gets a plan every
process downstream reads back as ``test``.

The claims pinned here, from the production entry point (a real request
against the mounted app, never a direct call into
``routes.fast_lane``):

1. **A request that says nothing runs for real.**  The response echoes
   ``production``.
2. **A declared mode is persisted.**  ``mode: "test"`` comes back in
   the response *and* is readable from the plan's state -- the flags are
   what the cost gate, the executor and the verification sub-agent read,
   so a mode that only lived in the response would be a mode nobody
   downstream could see.
3. **A mode nobody recognises is not an error.**  It falls back to
   ``production``, says so in the log, and the response echoes the
   fallback -- the caller is told what the plan actually is, rather than
   finding out later that its ``mode`` was ignored.
4. **The mode is written once.**  ``POST /api/plan/{id}/state`` cannot
   change it: that endpoint writes the phase and the two design flags,
   and a plan whose mode could be flipped after the fact would be a plan
   whose meaning depends on who asked last.

No real LLM is involved: ``server.create_coding_tool`` is monkeypatched
per-test to return a fake that scripts the two calls the generator makes.
Isolation is the autouse ``isolated_plans_dir`` fixture:
``PDT_PLANS_DIR`` and ``PDT_STATE_DB_PATH`` are both redirected at
``tmp_path``, so this file never reads the operator's live ``plans/``
tree or ``state.db``.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# The fake coding tool
# ---------------------------------------------------------------------------


class FakeCodingTool:
    """Stands in for ``CodingTool``; scripts the two calls the generator makes.

    ``query_json`` is the synthesis and ``query`` is the mandatory
    self-review.  The self-review reply echoes the synthesis payload *by
    reference*, which is what the generator mutates in place while it
    adds ``depends_on`` and resolves ``files_to_modify`` -- so the fake
    always reviews the list as it stands at that moment.
    """

    def __init__(self, reply):
        self._reply = reply
        self.json_calls = []
        self.query_calls = []

    def query_json(self, prompt: str, system_instruction: str = "") -> dict:
        self.json_calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        return self._reply

    def query(self, prompt: str) -> str:
        self.query_calls.append(prompt)
        return json.dumps(
            {"findings": [], "fixed_content": self._reply},
            ensure_ascii=False,
        )


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    """The RED probe would run the generated test commands for real."""
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")


@pytest.fixture
def checkout(tmp_path):
    """A real directory for the tasks to target.

    ``harden_for_execution`` refuses a plan whose ``project_dir`` does
    not exist, and the route runs that gate -- so a fixture that pointed
    at a made-up path would be testing the 422 branch on the happy
    path.
    """
    path = tmp_path / "checkout"
    (path / "backend").mkdir(parents=True)
    for name in ("chart.py", "reporting_view.py", "loader.py", "a.py", "b.py"):
        (path / "backend" / name).write_text("# existing\n", encoding="utf-8")
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    adapter = path / "scripts" / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)
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


def _install(monkeypatch, reply):
    """Point ``server.create_coding_tool`` at a fake and hand it back."""
    tool = FakeCodingTool(reply)
    monkeypatch.setattr(server, "create_coding_tool", lambda **kwargs: tool)
    return tool


def _import(body):
    """One import request, through the mounted app."""
    return client.post("/api/plan/import", json=body)


# ---------------------------------------------------------------------------
# 1 -- a request that says nothing runs for real
# ---------------------------------------------------------------------------


def test_import_defaults_to_production(
    isolated_plans_dir, checkout, monkeypatch
):
    """An import without a mode is a plan that runs for real.

    ``production`` is the fallback because it is the mode that must be
    safe to assume: falling back to ``test`` would silently turn real
    work into a rehearsal.  The response echoes the mode the plan
    actually got, so the caller is not left guessing.
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "make a chart page",
        "title": "Mode default",
    })

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "production"


# ---------------------------------------------------------------------------
# 2 -- a declared mode is persisted
# ---------------------------------------------------------------------------


def test_import_persists_test_mode(
    isolated_plans_dir, checkout, monkeypatch
):
    """``mode: "test"`` survives the import and is readable from the plan.

    The response is only half of the claim.  The mode has to be in the
    plan's **flags**, because that is where the cost gate, the executor
    and the verification sub-agent read it from -- three processes that
    never see the HTTP response.  A mode that lived only in the response
    would be a mode nobody downstream could act on.
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "make a chart page",
        "title": "Mode test",
        "mode": "test",
    })

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "test"

    plan_id = response.json()["plan_id"]
    state = client.get(f"/api/plan/{plan_id}/state")
    assert state.status_code == 200, state.text
    assert state.json()["flags"]["mode"] == "test"


# ---------------------------------------------------------------------------
# 3 -- a mode nobody recognises is not an error
# ---------------------------------------------------------------------------


def test_import_invalid_mode_falls_back(
    isolated_plans_dir, checkout, monkeypatch, caplog
):
    """``mode: "prod"`` is not a mode; it becomes ``production``, loudly.

    The fallback is never silent: the log says which value was ignored,
    so a plan whose mode was ignored is not indistinguishable from a plan
    that declared ``production`` on purpose.  The response echoes the
    fallback -- the caller learns what the plan actually is.
    """
    _install(monkeypatch, _tasks_payload())

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        response = _import({
            "project_dir": str(checkout),
            "content": "make a chart page",
            "title": "Mode invalid",
            "mode": "prod",
        })

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "production"
    assert "plan_mode_invalid=" in caplog.text


# ---------------------------------------------------------------------------
# 4 -- the mode is written once
# ---------------------------------------------------------------------------


def test_state_endpoint_cannot_change_mode(
    isolated_plans_dir, checkout, monkeypatch
):
    """``POST /api/plan/{id}/state`` cannot rewrite the plan's mode.

    The state endpoint writes the phase and the two design flags.  The
    mode is not among them, and a body that carries one is ignored: a
    plan whose mode could be flipped after the fact would be a plan
    whose meaning depends on who asked last -- and the cost gate would
    make its spending decision on a mode the caller could change behind
    its back.
    """
    _install(monkeypatch, _tasks_payload())

    response = _import({
        "project_dir": str(checkout),
        "content": "make a chart page",
        "title": "Mode fixed",
        "mode": "test",
    })
    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]

    update = client.post(
        f"/api/plan/{plan_id}/state",
        json={"flags": {"mode": "production"}},
    )
    assert update.status_code == 200, update.text
    assert update.json()["flags"]["mode"] == "test"

    after = client.get(f"/api/plan/{plan_id}/state")
    assert after.status_code == 200, after.text
    assert after.json()["flags"]["mode"] == "test"
