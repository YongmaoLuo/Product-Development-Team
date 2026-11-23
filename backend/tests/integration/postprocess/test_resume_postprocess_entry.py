"""``verification_loop.resume_postprocess`` — the entry the route calls.

Background
----------
``POST /api/verification/{id}/postprocess/resume`` moves a plan off the
parking phase ``postprocess_failed`` and then calls
:func:`verification_loop.resume_postprocess`.  That function is the seam
between the route and the finishing stage (收尾阶段), and its whole job
is to be :func:`verification_loop._start_postprocess` **with one
difference**: ``orchestrator.resume()`` instead of ``orchestrator.start()``.

That difference is the whole point, so it is pinned here:

* ``resume()`` is called — the run continues from the progress file and
  repeats only the items that are unfinished or failed;
* ``start()`` is **not** called — it would recompute the first round;
* the stage still ends in ``_settle_postprocess_phase`` — the plan's
  phase follows the report the stage wrote, exactly as in the first run;
* it runs on a **background thread** — the caller (an HTTP request
  thread) must not block on outward-facing work.

Everything the stage would really do is injected: the orchestrator, the
command runner, the requirement book and the phase settle are all
stubs.  No command runs and no file is written.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import server
import verification_loop
from postprocess import executor as executor_module
from postprocess import orchestrator as orchestrator_module
from postprocess import spec as spec_module


pytestmark = pytest.mark.integration


PLAN_ID = "p-resume-entry"


class _OrchestratorSpy:
    """Records which of the two entry points was used.

    ``block`` makes the stubbed stage wait for ``release`` — that is how
    the "the caller is not blocked" case observes a stage that is still
    running.
    """

    calls = []
    block = False
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def __init__(self, plan_id, *, plan_dir=None, executor=None, sink=None):
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.executor = executor
        self.sink = sink

    def start(self):
        type(self).calls.append(("start", self.plan_id))
        type(self).entered.set()
        type(self).finished.set()
        return {}

    def resume(self):
        type(self).calls.append(("resume", self.plan_id))
        type(self).entered.set()
        if type(self).block:
            type(self).release.wait(timeout=10)
        type(self).finished.set()
        return {}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Wire the seam: the orchestrator, the runner and the book are stubs."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    _OrchestratorSpy.calls = []
    _OrchestratorSpy.block = False
    _OrchestratorSpy.entered = threading.Event()
    _OrchestratorSpy.release = threading.Event()
    _OrchestratorSpy.release.set()
    _OrchestratorSpy.finished = threading.Event()

    monkeypatch.setattr(
        executor_module, "CommandRunner", lambda frozen, workspace=None: object()
    )
    monkeypatch.setattr(
        spec_module, "load_spec", lambda plan_dir: {"items": []}
    )
    monkeypatch.setattr(spec_module, "freeze", lambda spec: spec)
    monkeypatch.setattr(
        orchestrator_module, "PostProcessOrchestrator", _OrchestratorSpy
    )
    monkeypatch.setattr(server, "_plan_dir", lambda plan_id: plan_dir)
    monkeypatch.setattr(server, "_get_project_dir", lambda plan_id: None)

    settled = []
    monkeypatch.setattr(
        verification_loop,
        "_settle_postprocess_phase",
        lambda plan_id, plan_dir: settled.append((plan_id, plan_dir)),
    )

    yield plan_dir, settled
    # The background thread must be gone before the next case runs.
    _OrchestratorSpy.release.set()
    _OrchestratorSpy.finished.wait(timeout=10)


def test_resume_postprocess_continues_the_stage_and_settles_the_phase(wired):
    """``resume()`` runs, ``start()`` does not, and the phase is settled."""
    plan_dir, settled = wired

    verification_loop.resume_postprocess(PLAN_ID)

    assert _OrchestratorSpy.finished.wait(timeout=10)
    assert _OrchestratorSpy.calls == [("resume", PLAN_ID)]
    assert settled == [(PLAN_ID, plan_dir)]


def test_resume_postprocess_returns_before_the_stage_finishes(wired):
    """The stage runs on a background thread — the caller is not blocked."""
    plan_dir, settled = wired
    # The stubbed stage now waits: the call under test must return
    # while the stage is still running.
    _OrchestratorSpy.block = True
    _OrchestratorSpy.release.clear()

    verification_loop.resume_postprocess(PLAN_ID)

    # The stage started...
    assert _OrchestratorSpy.entered.wait(timeout=10)
    # ...and it has NOT finished, while the call above already returned.
    assert not _OrchestratorSpy.finished.is_set()
    assert settled == []

    _OrchestratorSpy.release.set()
    assert _OrchestratorSpy.finished.wait(timeout=10)
    assert settled == [(PLAN_ID, plan_dir)]
