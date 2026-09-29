import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import (
    app,
    _execution_state,
    _execution_locks,
    _verification_state,
    DEFAULT_MAX_VERIFICATION_ROUNDS,
)
from plan_state import PlanState

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Clear global execution/verification state and redirect PLANS_DIR."""
    _execution_state.clear()
    _execution_locks.clear()
    _verification_state.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _execution_state.clear()
    _execution_locks.clear()
    _verification_state.clear()


class FakeProcess:
    """Fake subprocess.Popen that exits with a configurable return code."""

    def __init__(self, returncode=0):
        self.pid = 12345
        self._returncode = returncode

    @property
    def returncode(self):
        return self._returncode

    @property
    def stdout(self):
        return iter([])

    def wait(self, timeout=None):
        return self._returncode

    def poll(self):
        return self._returncode

    def terminate(self):
        pass


class FakeOrchestrator:
    """Verification orchestrator that reports passed immediately."""

    def __init__(self, plan_dir, project_dir, coding_tool=None, max_parallel=1, verif_repo=None):
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)

    def start_verification_cycle(self, round_number=1, force=False, resume=False):
        # Mirror the real orchestrator: drive PlanState through a valid
        # ``executing → verification_running → verification_passed`` cycle
        # so the state machine auto-appends ``execution`` to
        # ``completed_phases`` (plan_state.py:520-527 refuses verification
        # transitions otherwise).
        ps = PlanState(self.plan_dir)
        ps.force_set_phase("executing")
        ps.transition_to("verification")
        ps.transition_to("verification_running")
        ps.verification_passed()
        return {
            "verification_results": [{"status": "PASSED"}],
            "summary": "fake passed",
            "execution_profile": {},
        }

    def check_cycle_conditions(self, report, round_number=1):
        return {"status": "passed", "stop_reason": None, "repair_tasks": []}

    def confirm_repair_and_rerun(self):
        pass


def _setup_plan_dir(plan_id):
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "ready",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def _wait_for_db_status(state_db_reader, plan_id, target_status, timeout=5):
    """Poll the SQLite ``plan_execution`` row until ``exec_status`` matches.

    2026-09-13 port: ``execution.json`` was removed when execution
    state moved into the SQLite ``plan_execution`` table — the row is
    the source of truth (hermetic per-test DB via the
    ``state_db_reader`` fixture; the live ``state.db`` is untouched).
    """
    for _ in range(int(timeout * 20)):
        row = state_db_reader.execution(plan_id)
        if row and row.get("exec_status") == target_status:
            return row
        time.sleep(0.05)
    return None


class TestAutoVerificationState:
    """Regression tests ensuring auto-verification updates _verification_state."""

    def test_auto_verification_populates_state(self, monkeypatch, state_db_reader):
        """After execution exits 0, auto-verification must register in _verification_state."""
        plan_id = "test-auto-verify-state"
        plan_dir = _setup_plan_dir(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: FakeProcess(returncode=0))
        monkeypatch.setattr("server.VerificationOrchestrator", FakeOrchestrator)
        # 2026-09-13: production passes scene="verification" (scene-routing
        # refactor) — the stub must swallow the kwarg.
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda coding_tool: 1)

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200

        # Wait for execution to finish (SQLite plan_execution row)
        exec_data = _wait_for_db_status(state_db_reader, plan_id, "completed")
        assert exec_data is not None

        # Wait for auto-verification to register and finish
        for _ in range(100):
            state = _verification_state.get(plan_id)
            if state and state.get("verification_status") in ("passed", "failed", "loop_stopped"):
                break
            time.sleep(0.05)

        state = _verification_state.get(plan_id)
        assert state is not None, "auto-verification did not register _verification_state"
        assert state["verification_status"] == "passed"
        # The budget that reaches ``_verification_state`` is the default
        # from ``StartVerificationRequest`` — asserted against the
        # constant so this stays a *wiring* test (the policy value
        # itself is pinned once, in
        # ``test_default_max_verification_rounds_is_the_decided_budget``).
        assert state["verification_max_rounds"] == DEFAULT_MAX_VERIFICATION_ROUNDS
        assert state["results"]["pytest_summary"] == "1 passed, 0 failed"

        # Plan state should reflect verification_passed (or completed after transition)
        ps = PlanState(plan_dir)
        assert ps.get_current_phase() in ("verification_passed", "completed")

    def test_auto_verification_closes_loop_on_same_failure(self, monkeypatch, state_db_reader):
        """A convergence verdict must close the auto-loop, not start another round.

        This drives the verdict through the POST-REPAIR callback, whose
        branch has always keyed off ``status == "loop_stopped"`` and so
        was never affected by the defect — the sibling test below
        (``test_auto_loop_does_not_burn_a_round_on_a_convergence_verdict``)
        pins the main loop's guard, which was.

        2026-09-20 (post-mortem), two weaknesses fixed here anyway:
        the stub returned ``same_failure_repeated``, a spelling the
        orchestrator retired on 2026-09-12, so this test exercised a string
        production no longer produces; and it polled for a terminal-looking
        status instead of waiting for the loop to finish, which raced the
        loop's own exit.
        """
        plan_id = "test-auto-verify-same-failure"
        plan_dir = _setup_plan_dir(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        call_count = {"n": 0}
        illegal_reroutes = {"n": 0}
        shared_prev = {"failed": []}

        class StuckOrchestrator:
            """Always returns the same failed VP — repair never fixes it."""

            def __init__(self, plan_dir, project_dir, coding_tool=None, max_parallel=1, verif_repo=None):
                pass

            def start_verification_cycle(self, round_number=1, force=False, resume=False):
                call_count["n"] += 1
                ps = PlanState(plan_dir)
                ps.force_set_phase("executing")
                ps.transition_to("verification")
                ps.transition_to("verification_running")
                return {
                    "verification_results": [
                        {"status": "FAILED", "id": "VP-stuck"},
                    ],
                    "summary": "stuck on the same test_command mismatch",
                    "execution_profile": {},
                }

            def check_cycle_conditions(self, report, round_number=1):
                # The real orchestrator would detect that the previous
                # failed IDs match the current ones and emit its
                # convergence verdict. Mimic that here.
                #
                # 2026-09-20 (post-mortem): the stub used to return the
                # bare ``same_failure_repeated``. The real
                # ``check_cycle_conditions`` stopped emitting that spelling
                # on 2026-09-12 — it renamed it to
                # ``same_failure_repeated_after_max_attempts`` when the
                # consecutive-rounds counter landed — so this test was
                # exercising a string production no longer produces, and the
                # auto-loop's guard (which compared against the retired
                # spelling) drifted unnoticed for eight days. Pin the real
                # reason here; ``status`` is what the loop keys off.
                #
                # 2026-09-13: the failure history must live OUTSIDE the
                # instance. The 2026-09-11/12 closed-loop refactor has
                # the repair-completion callback construct a FRESH
                # orchestrator per post-repair round (resetting
                # ``_previous_failed_ids`` is intentional after a real
                # executor pass) — but this stub never gets a real
                # repair, so the stuck failure persists across
                # instances and the shared dict models that.
                prev_failed = shared_prev["failed"]
                cur_failed = ["VP-stuck"]
                if prev_failed == cur_failed and prev_failed:
                    return {
                        "status": "loop_stopped",
                        "stop_reason": "same_failure_repeated_after_max_attempts",
                        "repair_tasks": [],
                        "waiting_for_user": False,
                    }
                shared_prev["failed"] = cur_failed
                return {
                    "status": "verification_failed",
                    "stop_reason": None,
                    "repair_tasks": [
                        {"id": f"R{round_number}-1", "title": "stuck repair"},
                    ],
                    "waiting_for_user": True,
                }

            def confirm_repair_and_rerun(self):
                # A convergence verdict must NEVER reach this. The loop's
                # empty-repair-queue exit routes to ``executing`` through
                # here, and ``executing`` is not a declared edge out of
                # ``verification_loop_stopped``: the real method raises
                # ``ValueError: Cannot start repair execution from
                # 'verification_loop_stopped'``, and the blanket ``except``
                # around the call starts yet another round.
                #
                # Distinguish that reroute from the legitimate one: the
                # healthy round-1 path calls this too, after the orchestrator
                # returned repair tasks and no convergence verdict. The
                # dead-end reroute is the one that arrives with the
                # convergence reason already stamped on the in-memory state.
                _seen = _verification_state.get(plan_id) or {}
                if _seen.get("stop_reason") in (
                    "same_failure_repeated",
                    "same_failure_repeated_after_max_attempts",
                ):
                    illegal_reroutes["n"] += 1

        # 2026-09-13: production spawns repairs via
        # ``_run_repair_execution_async`` (2026-09-11 v14 auto-chain).
        # Stub it to invoke the completion callback SYNCHRONOUSLY with
        # rc=0 — simulating a repair pass that finishes immediately —
        # so the post-repair re-verification runs inline and the loop
        # terminates within the test instead of leaking background
        # threads into sibling tests.
        def _fake_repair_async(
            plan_id, project_dir, tool=None, on_complete=None, outcome=None,
        ):
            # Runs the completion callback inline, so this stub models a
            # repair round that has ALREADY finished by the time the
            # dispatcher returns — nothing is left in flight, and leaving
            # ``outcome`` untouched (terminal) is the faithful reading.
            # 2026-09-23: the real dispatcher records the hand-off through
            # the same ``outcome`` kwarg.
            if on_complete is not None:
                on_complete(0)
            return {"status": "started", "pid": 0}

        monkeypatch.setattr("server._run_repair_execution_async", _fake_repair_async)
        monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: FakeProcess(returncode=0))
        monkeypatch.setattr("server.VerificationOrchestrator", StuckOrchestrator)
        # 2026-09-13: production passes scene="verification" (scene-routing
        # refactor) — the stub must swallow the kwarg.
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda coding_tool: 1)

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200

        _wait_for_db_status(state_db_reader, plan_id, "completed")
        # 2026-09-20 (post-mortem): join the auto-loop thread rather
        # than polling for a terminal-looking status. The round body stamps
        # ``verification_status`` from the orchestrator's verdict BEFORE the
        # loop has decided what to do with that verdict, so a status poll
        # can return while the loop is still walking into (or past) its
        # exit. That race is precisely how the bug this test now pins went
        # unnoticed: ``call_count`` read 2 while the loop was already
        # heading into the fall-through round.
        #
        # The auto-loop thread is spawned after the executor finishes, so
        # wait for it to register itself first.
        _loop_thread = None
        for _ in range(200):
            _loop_thread = (_verification_state.get(plan_id) or {}).get("thread")
            if _loop_thread is not None:
                break
            time.sleep(0.05)
        assert _loop_thread is not None, "auto-verification thread never started"
        _loop_thread.join(timeout=30)
        assert not _loop_thread.is_alive(), (
            "auto-verification thread did not finish — the convergence "
            "verdict did not close the loop"
        )

        # The orchestrator returned its convergence verdict on round 2, so the
        # auto-loop should have terminated after exactly 2 calls, NOT 3.
        assert call_count["n"] == 2, (
            f"auto-verification ran {call_count['n']} times; the convergence "
            "verdict should close the loop after the second identical failure"
        )
        # The decisive one: an ignored convergence verdict falls through to
        # the empty-repair-queue exit, which asks the orchestrator to route
        # the plan to ``executing``. That must never happen here.
        assert illegal_reroutes["n"] == 0, (
            "the loop tried to route a converged plan to executing instead "
            "of stopping — the auto-loop is ignoring the convergence verdict"
        )
        state = _verification_state.get(plan_id)
        assert state is not None
        # 2026-09-12 closed-loop contract: a convergence verdict records
        # terminal status ``loop_stopped`` (the plan is recoverable via
        # verification_loop_stopped -> failed / verification_passed),
        # not a hard ``failed``.
        assert state["verification_status"] == "loop_stopped"
        assert state["stop_reason"] == "same_failure_repeated_after_max_attempts"

    def test_auto_loop_does_not_burn_a_round_on_a_convergence_verdict(
        self, monkeypatch, state_db_reader,
    ):
        """The MAIN loop's own guard must honour the convergence verdict.

        2026-09-20 (post-mortem). The sibling test above reaches its
        verdict through the post-repair callback, which has always keyed
        off ``status == "loop_stopped"`` and so never had this bug. This
        one drives the main loop's failure path — the one that compared
        ``stop_reason`` against the retired ``same_failure_repeated``
        spelling — using the exact shape the live run produced:

            round 1: repair-task generation fails (transient) -> ``continue``
            round 2: same failure set -> convergence verdict, empty queue

        Pre-fix, round 2's verdict fell through to the empty-repair-queue
        exit, which asked the orchestrator to route a plan already sitting
        in ``verification_loop_stopped`` to ``executing``. The reroute
        raised, the blanket ``except`` swallowed it, and round 3 started —
        repeating until the round budget ran out and overwrote the recorded
        ``same_failure_repeated_after_max_attempts`` with
        ``max_rounds_reached``.
        """
        plan_id = "test-auto-loop-burns-round"
        plan_dir = _setup_plan_dir(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        cycle_calls = {"n": 0}
        illegal_reroutes = {"n": 0}

        class ConvergingOrchestrator:
            """Round 1: generation fails. Round 2 onward: converged."""

            def __init__(self, plan_dir, project_dir, coding_tool=None,
                         max_parallel=1, verif_repo=None):
                pass

            def start_verification_cycle(self, round_number=1, force=False,
                                         resume=False):
                cycle_calls["n"] += 1
                ps = PlanState(plan_dir)
                ps.force_set_phase("executing")
                ps.transition_to("verification")
                ps.transition_to("verification_running")
                return {
                    "verification_results": [
                        {"status": "FAILED", "id": "VP-019"},
                    ],
                    "summary": "same failure set as the previous batch",
                    "execution_profile": {},
                }

            def check_cycle_conditions(self, report, round_number=1):
                if cycle_calls["n"] == 1:
                    # round 1: the repair generator broke — a
                    # transient fault the loop is supposed to retry, NOT
                    # a convergence. Drives the ``continue`` path, which
                    # is how the main loop reaches a second round.
                    return {
                        "status": "verification_failed",
                        "stop_reason": None,
                        "repair_tasks": [],
                        "repair_generation_error": "provider timeout",
                    }
                return {
                    "status": "loop_stopped",
                    "stop_reason": "same_failure_repeated_after_max_attempts",
                    "repair_tasks": [],
                    "waiting_for_user": False,
                }

            def confirm_repair_and_rerun(self):
                _seen = _verification_state.get(plan_id) or {}
                if _seen.get("stop_reason") in (
                    "same_failure_repeated",
                    "same_failure_repeated_after_max_attempts",
                ):
                    illegal_reroutes["n"] += 1

        def _fake_repair_async(
            plan_id, project_dir, tool=None, on_complete=None, outcome=None,
        ):
            # Runs the completion callback inline, so this stub models a
            # repair round that has ALREADY finished by the time the
            # dispatcher returns — nothing is left in flight, and leaving
            # ``outcome`` untouched (terminal) is the faithful reading.
            # 2026-09-23: the real dispatcher records the hand-off through
            # the same ``outcome`` kwarg.
            if on_complete is not None:
                on_complete(0)
            return {"status": "started", "pid": 0}

        monkeypatch.setattr("server._run_repair_execution_async", _fake_repair_async)
        monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: FakeProcess(returncode=0))
        monkeypatch.setattr("server.VerificationOrchestrator", ConvergingOrchestrator)
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda coding_tool: 1)

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200

        _wait_for_db_status(state_db_reader, plan_id, "completed")
        _loop_thread = None
        for _ in range(200):
            _loop_thread = (_verification_state.get(plan_id) or {}).get("thread")
            if _loop_thread is not None:
                break
            time.sleep(0.05)
        assert _loop_thread is not None, "auto-verification thread never started"
        _loop_thread.join(timeout=30)
        assert not _loop_thread.is_alive()

        assert cycle_calls["n"] == 2, (
            f"auto-verification ran {cycle_calls['n']} rounds; the convergence "
            "verdict on round 2 should have ended the loop there"
        )
        assert illegal_reroutes["n"] == 0, (
            "the main loop tried to route a converged plan to executing "
            "instead of stopping — its guard is not honouring the verdict"
        )
        state = _verification_state.get(plan_id)
        assert state is not None
        assert state["verification_status"] == "loop_stopped"
        assert state["stop_reason"] == "same_failure_repeated_after_max_attempts", (
            "the convergence reason must survive; it used to be overwritten "
            "by max_rounds_reached once the loop exhausted its budget"
        )

    def test_auto_verification_skips_duplicate(self, monkeypatch, state_db_reader):
        """If _verification_state already says running, auto-verification should not start again."""
        plan_id = "test-auto-verify-dup"
        plan_dir = _setup_plan_dir(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        # Pre-populate as if a manual verification is running
        _verification_state[plan_id] = {
            "plan_id": plan_id,
            "verification_status": "running",
            "verification_round": 1,
            "verification_max_rounds": 3,
            "results": {},
            "repair_tasks": [],
            "started_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "orchestrator": None,
            "stop_reason": None,
        }

        calls = []

        class NoopOrchestrator:
            def __init__(self, *args, **kwargs):
                calls.append("init")

        monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: FakeProcess(returncode=0))
        monkeypatch.setattr("server.VerificationOrchestrator", NoopOrchestrator)

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200

        _wait_for_db_status(state_db_reader, plan_id, "completed")
        time.sleep(0.3)

        assert not calls, "auto-verification should not spawn orchestrator when one is already running"
        assert _verification_state[plan_id]["verification_round"] == 1


class TestAutoVerificationStartRoundParam:
    """Regression tests for the ``start_round`` parameter on
    ``_run_auto_verification_loop`` (2026-08-26 audit).
    """

    def test_auto_loop_respects_start_round(self, monkeypatch):
        """``start_round=2`` must skip Round 1 and only run Round 2 onwards.
        Without this, the manual ``POST /api/verification/{id}/start`` path
        could not delegate to the auto-loop on Round 2 / 3 re-triggers —
        every ``/start`` would re-run Round 1 from scratch.
        """
        from server import _run_auto_verification_loop

        plan_id = "test-start-round"
        plan_dir = _setup_plan_dir(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        rounds_called = []

        class RoundTracker:
            def __init__(self, plan_dir, project_dir, coding_tool=None, max_parallel=1, verif_repo=None):
                self.plan_dir = Path(plan_dir)

            def start_verification_cycle(self, round_number=1, force=False, resume=False):
                rounds_called.append(round_number)
                # Drive PlanState through a valid cycle. Use ``executing`` as
                # the source phase so ``transition_to('verification_running')``
                # auto-appends ``execution`` to ``completed_phases`` (the
                # state machine refuses verification transitions otherwise;
                # see plan_state.py:520-527). Without this, Round 1's
                # ``ready → verification_running`` transition fails the
                # ``execution in completed_phases`` guard.
                ps = PlanState(self.plan_dir)
                ps.force_set_phase("executing")
                ps.transition_to("verification")
                ps.transition_to("verification_running")
                ps.verification_passed()
                return {
                    "verification_results": [{"status": "PASSED"}],
                    "summary": f"passed round {round_number}",
                    "execution_profile": {},
                }

            def check_cycle_conditions(self, report, round_number=1):
                return {"status": "passed", "stop_reason": None, "repair_tasks": []}

            def confirm_repair_and_rerun(self):
                pass

        monkeypatch.setattr("server.VerificationOrchestrator", RoundTracker)
        # 2026-09-13: production passes scene="verification" (scene-routing
        # refactor) — the stub must swallow the kwarg.
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda coding_tool: 1)

        # start_round=2, max_rounds=2 → loop body should run Round 2 only
        # (Round 1 was skipped by start_round, Round 3 doesn't exist because
        # max_rounds caps the upper bound). Verified: without start_round the
        # loop would have run Round 1 first; with start_round=2 the loop
        # body must only call start_verification_cycle(2).
        _run_auto_verification_loop(
            plan_id, plan_dir, project_dir,
            max_rounds=2, start_round=2,
        )

        assert rounds_called == [2], (
            f"expected start_round=2 (max_rounds=2) to skip Round 1 and run only Round 2, "
            f"got {rounds_called}"
        )

        state = _verification_state.get(plan_id)
        assert state is not None
        assert state["verification_status"] == "passed"


class TestStartVerificationAutoFix:
    """Regression tests for the ``auto_fix`` request flag on
    ``POST /api/verification/{plan_id}/start`` (2026-08-26 audit).

    ``auto_fix=True`` (default) must delegate to
    ``_run_auto_verification_loop`` so a manual ``/start`` after a
    failed Round 1 chains into repair → re-execute → Round 2
    automatically. ``auto_fix=False`` must keep the legacy single-round
    behaviour so operators can pause between rounds.
    """

    def _seed_running_state(self, plan_id):
        """Pre-populate state so /start CAS accepts (stage in
        {'executing', 'failed', 'completed',
         'verification_passed', 'verification_failed',
         'verification_loop_stopped'}).
        """
        from server import _open_verification_state

        conn, routing, verif = _open_verification_state()
        try:
            # Place plan in 'verification_failed' so /start CAS accepts it.
            # 2026-09-17 (schema v5): there is one workflow-state column,
            # so the old "set both, they are not aliases" dance is gone.
            routing.write_plan_state(
                plan_id,
                phase="verification_failed",
                completed_phases=[],
                review_rounds={},
                flags={},
                verification={},
                last_updated=None,
            )
            # Insert a plan_verification row first; complete_round is an
            # UPDATE that raises KeyError if the row does not exist.
            try:
                # 2026-09-20: write the budget explicitly. ``/start`` now
                # reads this column as the plan's own budget (an omitted
                # ``max_rounds`` no longer stamps the global default over
                # it), so a fixture relying on the schema's ``DEFAULT 3``
                # would be asserting against a schema artifact.
                verif.insert(
                    plan_id, "failed",
                    max_rounds=DEFAULT_MAX_VERIFICATION_ROUNDS,
                )
            except Exception:
                # Already exists (e.g. autouse fixture); update instead.
                pass
            verif.complete_round(
                plan_id,
                {"status": "failed", "stop_reason": "test_setup"},
                status="failed",
            )
            # ``complete_round`` does not bump ``round`` itself; the
            # ``/start`` endpoint derives ``next_round = round + 1``,
            # so we explicitly seed ``round=1`` to simulate "Round 1
            # finished, operator is now triggering Round 2".
            verif._update(plan_id, round=1)
            conn.commit()
        finally:
            conn.close()

    def _seed_project_dir(self, plan_id, project_dir):
        """``/start`` reads ``project_dir`` via ``_get_project_dir`` which
        falls back to ``plan_execution.project_dir``. Seed the execution row
        so the endpoint accepts the request.
        """
        from server import _open_verification_state

        conn, _, _ = _open_verification_state()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO plan_execution ("
                "  plan_id, current_phase, attempt_count, project_dir,"
                "  exec_status, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, "executing", 0, str(project_dir), "completed", "2026-01-01T00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def _setup_plan(self, plan_id):
        plan_dir = _setup_plan_dir(plan_id)
        # PlanState expects /tasks.json to exist; that's done by _setup_plan_dir.
        return plan_dir

    def test_start_verification_auto_fix_default_delegates_to_auto_loop(self, monkeypatch):
        """``POST /api/verification/{id}/start`` with empty body (default
        ``auto_fix=True``) must call ``_run_auto_verification_loop``
        instead of running a single round inline.
        """
        plan_id = "test-start-autofix-default"
        plan_dir = self._setup_plan(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        self._seed_running_state(plan_id)
        self._seed_project_dir(plan_id, project_dir)

        delegated = {"called": False, "kwargs": None}

        def fake_auto_loop(plan_id_, plan_dir_, project_dir, max_rounds=None, tool=None, start_round=1):
            delegated["called"] = True
            delegated["kwargs"] = {
                "plan_id": plan_id_,
                "max_rounds": max_rounds,
                "tool": tool,
                "start_round": start_round,
            }
            # Simulate a successful run by setting terminal state.
            _verification_state[plan_id_] = {
                "plan_id": plan_id_,
                "verification_status": "passed",
                "verification_round": start_round,
                "verification_max_rounds": max_rounds,
            }

        monkeypatch.setattr("server._run_auto_verification_loop", fake_auto_loop)
        # 2026-09-13: production passes scene="verification" (scene-routing
        # refactor) — the stub must swallow the kwarg.
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda ct: 1)

        # Empty body → auto_fix defaults to True per StartVerificationRequest.
        resp = client.post(f"/api/verification/{plan_id}/start", json={})
        assert resp.status_code == 200, resp.text

        # /start runs the auto-loop in a background thread; the
        # fake ``fake_auto_loop`` writes the terminal state synchronously,
        # but the thread isn't guaranteed to have run by the time the
        # HTTP response returns. Poll briefly to let the fake fire.
        for _ in range(200):
            if delegated["called"]:
                break
            time.sleep(0.05)

        assert delegated["called"], (
            "auto_fix=True (default) must delegate to _run_auto_verification_loop, "
            "not run a single round inline"
        )
        assert delegated["kwargs"]["start_round"] == 2, (
            f"after a failed Round 1, /start must enter the auto-loop at "
            f"Round 2; got start_round={delegated['kwargs']['start_round']}"
        )
        assert delegated["kwargs"]["max_rounds"] == DEFAULT_MAX_VERIFICATION_ROUNDS

    def test_start_verification_auto_fix_false_does_not_call_auto_loop(self, monkeypatch):
        """``POST /api/verification/{id}/start`` with ``auto_fix=False`` must
        NOT call ``_run_auto_verification_loop`` — the legacy single-round
        inline path is preserved so operators can pause between rounds.
        """
        plan_id = "test-start-autofix-false"
        plan_dir = self._setup_plan(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        self._seed_running_state(plan_id)
        self._seed_project_dir(plan_id, project_dir)

        delegated = {"called": False}

        def fake_auto_loop(*args, **kwargs):
            delegated["called"] = True

        monkeypatch.setattr("server._run_auto_verification_loop", fake_auto_loop)

        single_round_calls = {"n": 0}

        class PassedOrchestrator:
            def __init__(self, *args, **kwargs):
                pass

            def start_verification_cycle(self, round_number=1, force=False, resume=False):
                single_round_calls["n"] += 1
                ps = PlanState(plan_dir)
                ps.force_set_phase("executing")
                ps.transition_to("verification")
                ps.transition_to("verification_running")
                ps.verification_passed()
                return {
                    "verification_results": [{"status": "PASSED"}],
                    "summary": "single round passed",
                    "execution_profile": {},
                }

            def check_cycle_conditions(self, report, round_number=1):
                return {"status": "passed", "stop_reason": None, "repair_tasks": []}

            def confirm_repair_and_rerun(self):
                pass

        monkeypatch.setattr("server.VerificationOrchestrator", PassedOrchestrator)
        # 2026-09-13: production passes scene="verification" (scene-routing
        # refactor) — the stub must swallow the kwarg.
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda ct: 1)

        resp = client.post(
            f"/api/verification/{plan_id}/start",
            json={"auto_fix": False},
        )
        assert resp.status_code == 200, resp.text

        # /start is async — wait for the background thread to actually
        # run the single round. We don't get a handle back, so poll
        # the (now-populated) _verification_state for a terminal status.
        for _ in range(200):
            state = _verification_state.get(plan_id)
            if state and state.get("verification_status") in ("passed", "failed", "loop_stopped"):
                break
            time.sleep(0.05)

        assert not delegated["called"], (
            "auto_fix=False must NOT delegate to _run_auto_verification_loop"
        )
        assert single_round_calls["n"] == 1, (
            f"legacy single-round path must run exactly one cycle when auto_fix=False, "
            f"got {single_round_calls['n']}"
        )

    def test_start_verification_auto_fix_explicit_true_chains_rounds(self, monkeypatch):
        """Explicit ``auto_fix=True`` must also delegate to the auto-loop,
        not just the default. This guards against a future schema change
        that flips the default.
        """
        plan_id = "test-start-autofix-explicit-true"
        plan_dir = self._setup_plan(plan_id)
        project_dir = plan_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        self._seed_running_state(plan_id)
        self._seed_project_dir(plan_id, project_dir)

        delegated = {"called": False, "start_round": None}

        def fake_auto_loop(plan_id_, plan_dir_, project_dir, max_rounds=None, tool=None, start_round=1):
            delegated["called"] = True
            delegated["start_round"] = start_round
            _verification_state[plan_id_] = {
                "plan_id": plan_id_,
                "verification_status": "passed",
                "verification_round": start_round,
                "verification_max_rounds": max_rounds,
            }

        monkeypatch.setattr("server._run_auto_verification_loop", fake_auto_loop)
        # 2026-09-13: production passes scene="verification" (scene-routing
        # refactor) — the stub must swallow the kwarg.
        monkeypatch.setattr("server.create_coding_tool", lambda tool, cwd, **kwargs: None)
        monkeypatch.setattr("server._resolve_max_parallel", lambda ct: 1)

        resp = client.post(
            f"/api/verification/{plan_id}/start",
            json={"auto_fix": True},
        )
        assert resp.status_code == 200, resp.text

        # Wait for the auto-loop background thread to finish — the
        # fake records the call synchronously, but ``_run`` returns
        # immediately after spawning the thread, so the assertion
        # would race otherwise.
        for _ in range(200):
            state = _verification_state.get(plan_id)
            if state and state.get("verification_status") in ("passed", "failed", "loop_stopped"):
                break
            time.sleep(0.05)

        assert delegated["called"], "explicit auto_fix=True must still delegate"
        assert delegated["start_round"] == 2
