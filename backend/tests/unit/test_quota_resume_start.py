"""Starting the parked work again — through the start paths that exist.

Requirement R2 of the quota-park work says, in one line: *when the
quota window reopens, put the parked work back out through the paths
that already start work*.  There are two of those paths, and they are
the ones a human uses:

* an execution is started by ``routes.execution.start_execution``;
* a verification round is continued by
  ``routes.verification.start_verification``.

Why the resumer must not re-implement them
--------------------------------------------

A start is not one call.  It is a chain of guards — the ledger's
single-instance check, the routing CAS, the project-directory check —
and a chain of answers: a plan that is already running is refused with
409, an archived plan with 410, a plan without a usable working
directory with 400.  A resumer that grew a second copy of that chain
would be a second opinion on the same question, and the two copies
would drift.  That drift is how a plan ends up with two executors
writing the same task rows.

So what is pinned here is the *thin* half: the resumer names a plan,
calls the start path that already exists, and reads the answer.  Four
things follow from that, and the cases below are those four things.

1. **The existing path is the only decider.**  The resumer passes the
   plan id and takes the answer as it comes; it does not pre-judge
   whether the plan may be started.
2. **A refusal is not a crash.**  409 (already running), 410
   (archived) and 400 (no usable project directory) answer ``False``
   with a warning.  "This plan cannot be started right now" is an
   ordinary outcome of a sweep over many plans — not an error, and
   certainly not a reason to stop sweeping.
3. **Nothing escapes.**  Every other failure — any other HTTP status,
   any exception at all — answers ``False`` too, with the failure
   logged.  The caller is a heartbeat thread: a raise here would stop
   the sweep for *every* plan, not just this one.
4. **The single-instance rule is not re-implemented.**  R3 says one
   plan runs once.  The ledger inside the start path is what decides
   that, and the 409 it answers is what the resumer reports — the
   resumer adds no opinion of its own.

The starts are injected (``starter=``) so that these cases can pin the
contract without a server, a database or a plan directory; the
"which function is actually called" case pins the default wiring
separately.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import quota_resume  # noqa: E402
from quota_resume import ParkedPlan  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _execution(plan_id: str = "p9") -> ParkedPlan:
    """A parked execution, as the reader hands it over."""
    return ParkedPlan(plan_id, "execution", 0, "waiting_for_quota")


def _verification(plan_id: str = "p9") -> ParkedPlan:
    """A parked verification round, as the reader hands it over."""
    return ParkedPlan(plan_id, "verification", 2, "waiting_for_quota")


def _refusing(status: int):
    """A start path that refuses the plan the way the routes refuse it.

    The routes answer a refusal *by raising* :class:`HTTPException` for
    the checks that happen before anything is written (404 for a plan
    with no tasks, 400 for a plan with no usable project directory).
    """

    def _starter(plan_id: str):
        raise HTTPException(status, f"refused: {status}")

    return _starter


# ---------------------------------------------------------------------------
# The start path is called
# ---------------------------------------------------------------------------


def test_resume_execution_calls_start():
    """A parked execution goes back out through the execution start.

    The resumer does not spawn anything itself: it names the plan and
    calls the start path, once.  "Once" is the load-bearing word — a
    resumer that called the start twice would be the double-start that
    R3 forbids, and it would do it from the one place whose whole job
    is to be careful about that.

    The answer is ``True`` because the start path accepted the plan:
    the resumer's own opinion about the plan is not consulted.
    """
    calls: list = []

    def fake_start(plan_id: str):
        calls.append(plan_id)
        return {"status": "running"}

    parked = _execution()

    assert quota_resume.resume_execution(parked, starter=fake_start) is True
    assert calls == ["p9"]


def test_resume_verification_passes_plan_id():
    """A parked verification round is continued through the same start.

    The verification side is the same shape, with one difference that
    belongs to the start path and not to the resumer: the round that
    is continued is the round that stopped.  The resumer passes the
    plan id and nothing else — no round, no flags — so it cannot
    accidentally ask for a *new* round and throw away the verdicts the
    stopped one had already reached.
    """
    seen: list = []

    def fake_start(plan_id: str):
        seen.append(plan_id)
        return {"status": "started"}

    parked = _verification("p9")

    assert quota_resume.resume_verification(parked, starter=fake_start) is True
    assert seen == ["p9"]


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_resume_treats_409_as_not_started(caplog):
    """409 — the plan is already running — is a refusal, not a crash.

    The ledger inside the start path is what decides that a plan
    already has an executor, and it answers 409.  The resumer reports
    that as "not started" and moves on to the next plan: the plan is
    running, which is exactly the state the sweep wanted.

    It is a *warning*, not an exception: a sweep that meets a dozen
    running plans meets a dozen 409s, and a heartbeat thread that
    treats those as errors stops sweeping.
    """
    with caplog.at_level(logging.WARNING, logger="quota_resume"):
        assert quota_resume.resume_execution(_execution(), starter=_refusing(409)) is False

    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_resume_treats_a_refused_response_as_not_started():
    """A refusal *returned* as a response is a refusal too.

    The routes answer most of their refusals by returning a
    ``JSONResponse`` with an error status rather than by raising — the
    archived-plan answer (410), the artifact-conflict answer (409),
    the already-running answer (409) all come back that way.  So the
    resumer has to read the answer as well as catch the raise: a
    resumer that only caught exceptions would call a refused start a
    success, and would report a plan as running when it was not.
    """
    def fake_start(plan_id: str):
        return JSONResponse(status_code=409, content={"error": "already_running"})

    assert quota_resume.resume_execution(_execution(), starter=fake_start) is False


def test_resume_treats_410_as_not_started():
    """410 — the plan is archived — is a refusal.

    An archived plan is not coming back, so the sweep leaves it alone.
    The resumer does not decide that: the start path answers 410 and
    the resumer reports "not started".
    """
    assert quota_resume.resume_execution(_execution(), starter=_refusing(410)) is False


def test_resume_treats_400_as_not_started():
    """400 — no usable project directory — is a refusal.

    The start path refuses a plan whose working directory is missing or
    unusable, and it refuses *before* anything is spawned.  The resumer
    reports "not started" and the plan stays parked, which is the
    honest answer: nothing is running, and nothing will be until the
    operator fixes the directory.
    """
    assert quota_resume.resume_execution(_execution(), starter=_refusing(400)) is False


def test_resume_treats_other_http_failures_as_not_started():
    """Any other HTTP status is a refusal as well — and it is logged hard.

    404 (a plan with no tasks) is not one of the expected answers, so
    it is worth an exception-level log: it means the sweep found a
    parked plan that cannot be started at all.  The *answer* is the
    same (``False``) — nothing started — but the log level differs, so
    the anomaly is visible instead of being swallowed with the
    ordinary refusals.
    """
    assert quota_resume.resume_execution(_execution(), starter=_refusing(404)) is False


# ---------------------------------------------------------------------------
# Nothing escapes
# ---------------------------------------------------------------------------


def test_resume_swallows_start_exception(caplog):
    """A start that blows up answers "not started" and raises nothing.

    The caller is a heartbeat thread that sweeps every parked plan.  An
    exception raised here would unwind that thread and stop the sweep
    for every plan — one plan with a broken start path would keep the
    whole fleet from being revived.

    So the failure is absorbed, logged at exception level (it is a
    genuine anomaly, unlike the refusals), and reported as "not
    started".
    """
    def fake_start(plan_id: str):
        raise RuntimeError("the executor could not be spawned")

    with caplog.at_level(logging.ERROR, logger="quota_resume"):
        assert quota_resume.resume_execution(_execution(), starter=fake_start) is False

    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_resume_swallows_an_exception_from_the_verification_start():
    """The same absorption holds on the verification side.

    The two callers are halves of one sweep, and a sweep that survives
    one kind of failure has to survive both.
    """
    def fake_start(plan_id: str):
        raise ValueError("boom")

    assert quota_resume.resume_verification(_verification(), starter=fake_start) is False


# ---------------------------------------------------------------------------
# The default wiring: which start path is called
# ---------------------------------------------------------------------------


def test_resume_execution_uses_the_execution_route_by_default(monkeypatch):
    """With no starter injected, the execution start path is the one called.

    This is the whole point of the module: the revived work goes out
    through the *existing* start, not through a private one.  The
    route is looked up inside the call (not at import time) because
    the route modules import the application module, which imports
    them back — a module-level import of the routes would close that
    loop.

    The route module is stubbed here rather than imported: the wiring
    is what this case is about, and importing the application module
    would bring a server, a database and a plan directory into a unit
    case about two lines of wiring.
    """
    calls: list = []
    stub = types.ModuleType("routes.execution")

    def start_execution(plan_id: str):
        calls.append(plan_id)
        return {"status": "running"}

    stub.start_execution = start_execution
    monkeypatch.setitem(sys.modules, "routes.execution", stub)

    assert quota_resume.resume_execution(_execution("p7")) is True
    assert calls == ["p7"]


def test_resume_verification_uses_the_verification_route_by_default(monkeypatch):
    """The verification side resolves to the verification start path.

    Two different routes, two different plans of the same kind: a
    revived execution must not be sent to the verification start, and
    a revived verification round must not be sent to the execution
    start.  The stub names the route it stands in for, so a swapped
    pair cannot pass.
    """
    calls: list = []
    stub = types.ModuleType("routes.verification")

    def start_verification(plan_id: str):
        calls.append(plan_id)
        return {"status": "started"}

    stub.start_verification = start_verification
    monkeypatch.setitem(sys.modules, "routes.verification", stub)

    assert quota_resume.resume_verification(_verification("p8")) is True
    assert calls == ["p8"]


# ---------------------------------------------------------------------------
# The working directory the plan was in
# ---------------------------------------------------------------------------
#
# The start path refuses a plan it cannot be given a working directory
# for — a 400, raised before anything is spawned.  A human hands that
# directory over in the request body.  A *paused* plan does not need to:
# its checkpoint carries the directory, and the start path prefers the
# checkpoint over the body.
#
# A quota-parked run is not a paused run.  It stopped for want of
# capacity, so it has no checkpoint, so the only place its directory
# can come from is the durable row that records it.  A resumer that
# starts the plan with the plan id alone hands the start path nothing,
# the start path answers 400, and the plan is refused again on every
# tick — forever, and invisibly: from the outside a plan that cannot
# be revived looks exactly like a plan that is waiting.


def test_resume_execution_passes_the_recorded_project_dir():
    """The recorded directory travels in the request body.

    The start path is asked for the plan *and* for the directory the
    run was in.  Without it the start path cannot place the executor,
    and it refuses the plan before it spawns anything — so the run
    never comes back, and the only symptom is a refusal on every tick.

    The request is the real request class, not a stand-in: the start
    path reads other fields off it, so an object that merely carried a
    ``project_dir`` would be refused for a different reason.
    """
    from server import StartExecutionRequest

    calls: list = []

    def fake_start(plan_id: str, req=None):
        calls.append((plan_id, req))
        return {"status": "running"}

    parked = ParkedPlan(
        "p9", "execution", 0, "waiting_for_quota", "/tmp/worktrees/p9"
    )

    assert quota_resume.resume_execution(parked, starter=fake_start) is True

    assert len(calls) == 1
    plan_id, req = calls[0]
    assert plan_id == "p9"
    assert isinstance(req, StartExecutionRequest)
    assert req.project_dir == "/tmp/worktrees/p9"


def test_resume_execution_without_project_dir_keeps_the_plain_call(caplog):
    """A plan whose row records no directory is started as it was.

    The call shape does not change: the plan id alone, exactly as
    before.  The start path answers 400 — it has to, a plan with no
    directory cannot be placed — and the resumer reports the refusal
    and moves on.

    The refusal is *named*, though: ``missing_project_dir`` and not
    ``start_refused``.  The two are different problems, and an operator
    needs to tell them apart: a plan that is busy comes back on its
    own, and a plan with no recorded directory never will, because
    nothing in the product writes that column for a run that stopped
    for capacity.

    The stub takes the plan id and nothing else, so a start that
    carried a request body would raise ``TypeError`` before the body
    ran — and the refusal would then be reported as an anomaly rather
    than as the missing directory it is.
    """
    seen: list = []

    def fake_start(plan_id: str):
        seen.append(plan_id)
        raise HTTPException(400, "project_dir is required. Provide the absolute path.")

    parked = ParkedPlan("p9", "execution", 0, "waiting_for_quota")

    with caplog.at_level(logging.WARNING, logger="quota_resume"):
        assert quota_resume.resume_execution(parked, starter=fake_start) is False

    assert seen == ["p9"]
    messages = [record.getMessage() for record in caplog.records]
    assert any("reason=missing_project_dir" in message for message in messages)
