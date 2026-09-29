"""Regression tests for the verification terminal state-machine fix (2026-09-13).

Background
----------
Two defects combined to produce a split-brain the operator saw as
"飞书卡片说验证失败，但本地状态还在跑验证":

1. **``_chain_ending`` whitelist omitted the watchdog's stop reasons.**
   ``_persist_verification_terminal`` only CASes ``plan_routing.current_phase``
   out of ``verification_*`` (step 2) and mirrors ``current_phase``
   (step 3) when ``_chain_ending`` is true. The whitelist covered
   ``same_failure_repeated`` / ``max_rounds_reached`` / ``user_stopped``
   / … but *not* ``verification_log_stale`` /
   ``verification_results_stale`` /
   ``verification_thread_died_unexpectedly``. So when the watchdog
   killed a stuck round it wrote ``plan_verification.verification_status
   = 'failed'`` (step 1) and then skipped both remaining steps, leaving
   ``current_phase='verification_running'`` on disk forever.

2. **The staleness judgement ignored per-VP attempt logs.**
   ``_lazy_check_verification`` judged ``verification_log_stale`` from
   ``logs/verification_*.log`` alone. A long single-VP retry (VP-034's
   full-suite pytest runs ~15 min per attempt) only touches
   ``logs/vp_attempts/*.log``, so a perfectly healthy round looked
   identical to a hung orchestrator and the watchdog would kill it.

Contract pinned here
--------------------
* Every watchdog stop reason is a terminal (chain-ending) reason, and
  the watchdog call site says so *explicitly* so a future stop reason
  cannot silently regress.
* A terminal write with a watchdog reason moves ``plan_routing.current_phase``
  out of ``verification_running``.
* A genuine mid-loop terminal write still leaves the phase alone.
* The staleness check counts ``logs/vp_attempts/*.log`` as progress.
"""

from __future__ import annotations

import ast
from typing import Optional
import json
import sqlite3
from pathlib import Path

import pytest
from tests.app_source import find_def

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_server(tmp_path, monkeypatch):
    """Point ``server`` at a hermetic plans root + ``state.db``."""
    import server

    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", plans_root)
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    return server, plans_root, db_path


def _seed_running_verification(
    db_path: Path, plan_id: str, plans_root: Path, phase: str = "verification_running"
) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, phase, 0, "[]",
                "{}", "{}",
                json.dumps(
                    {"status": "running", "round": 1, "max_rounds": 3,
                     "stop_reason": None}
                ),
                "2026-09-13T00:00:00", "2026-09-13T00:00:00Z",
            ),
        )
        conn.commit()
        repo = VerificationRepository(conn)
        repo.insert(plan_id, "running")
        repo.init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()

    (plans_root / plan_id).mkdir(parents=True, exist_ok=True)


def _phase(db_path: Path, plan_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# 1. The stop-reason vocabulary is closed over by construction
# ---------------------------------------------------------------------------


def test_watchdog_stop_reasons_are_terminal(hermetic_server):
    """Every watchdog stop reason must count as chain-ending.

    On the pre-fix code this set did not exist and the three watchdog
    reasons were absent from the inline whitelist — that is the bug.
    """
    server, _plans_root, _db_path = hermetic_server

    missing = sorted(
        server.VERIFICATION_WATCHDOG_STOP_REASONS
        - server.VERIFICATION_TERMINAL_STOP_REASONS
    )
    assert missing == [], (
        f"watchdog stop reason(s) {missing} are not chain-ending; "
        "_persist_verification_terminal will skip the routing CAS and "
        "leave the plan stuck at current_phase='verification_running'."
    )
    # Anti-vacuous guard: the watchdog must actually produce reasons.
    assert len(server.VERIFICATION_WATCHDOG_STOP_REASONS) >= 3


def test_convergence_stop_reasons_are_terminal_and_cover_the_orchestrator(
    hermetic_server,
):
    """2026-09-20 (post-mortem): the convergence vocabulary, twice over.

    Two failures are pinned at once:

    1. **Membership** — a convergence reason must be chain-ending, or
       ``_persist_verification_terminal`` skips the routing CAS and leaves
       the plan inside the verification family.
    2. **Coverage** — the reason ``check_cycle_conditions`` actually
       returns must be classified. This is the live defect: the auto-loop
       compared ``stop_reason == "same_failure_repeated"``, but the
       orchestrator had renamed the verdict to
       ``same_failure_repeated_after_max_attempts`` when the
       consecutive-rounds counter landed on 2026-09-12. The guard never
       matched, so every converged plan silently started another round
       until its budget ran out (that run: rounds 3 and 4 both logged the
       verdict, and the recorded reason ended as ``max_rounds_reached``).

       Reading the producer's own return literal is the only way to catch
       a rename on the day it lands — the auto-loop's own tests use a stub,
       and a stub keeps whatever spelling it was written with.
    """
    server, _plans_root, _db_path = hermetic_server

    missing = sorted(
        server.VERIFICATION_CONVERGENCE_STOP_REASONS
        - server.VERIFICATION_TERMINAL_STOP_REASONS
    )
    assert missing == [], (
        f"convergence stop reason(s) {missing} are not chain-ending; "
        "_persist_verification_terminal will skip the routing CAS and "
        "leave the plan stuck inside the verification family."
    )
    # Anti-vacuous guard, mirroring the watchdog twin above: both spellings
    # of the verdict must stay classified.
    assert server.VERIFICATION_CONVERGENCE_STOP_REASONS >= {
        "same_failure_repeated",
        "same_failure_repeated_after_max_attempts",
    }

    tree = ast.parse((_BACKEND_DIR / "verification" / "orchestrator.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "check_cycle_conditions"
    )
    emitted = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "stop_reason"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                emitted.add(value.value)

    assert emitted, (
        "no string ``stop_reason`` literal found in check_cycle_conditions — "
        "this guard would otherwise pass vacuously"
    )
    # (a) Any reason this function can return must at least be terminal.
    unclassified = sorted(emitted - server.VERIFICATION_TERMINAL_STOP_REASONS)
    assert unclassified == [], (
        f"check_cycle_conditions returns stop_reason(s) {unclassified} that "
        "are not terminal — the round would end without clearing the "
        "routing row."
    )
    # (b) The convergence verdict specifically must be classified as such,
    # because callers that only see a ``stop_reason`` string branch on it.
    assert "same_failure_repeated_after_max_attempts" in emitted, (
        "check_cycle_conditions no longer returns "
        "'same_failure_repeated_after_max_attempts'. If the verdict was "
        "renamed, add the new spelling to "
        "VERIFICATION_CONVERGENCE_STOP_REASONS and update "
        "test_auto_loop_does_not_burn_a_round_on_a_convergence_verdict."
    )


def _callee_name(func: ast.AST) -> Optional[str]:
    """The called name, whether spelled bare or through the app module.

    ``_persist_verification_terminal`` is one of the names the suite patches
    as ``server.<name>``, so the watchdog — which now lives in
    ``backend/verification_loop.py`` — calls it as
    ``_server._persist_verification_terminal`` and the patch still lands.
    Both spellings are the same call; this test is about the arguments.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_watchdog_call_site_forces_chain_ending():
    """``_lazy_check_verification`` must pass ``chain_ending=True``.

    Reason membership alone is a whitelist that the next watchdog stop
    reason will be forgotten from; the explicit flag makes the watchdog
    correct by construction.
    """
    # The watchdog lives in ``backend/verification_loop.py`` since the
    # 2026-09-25 split; ``find_def`` follows the source across modules.
    fn = find_def("_lazy_check_verification").node
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and _callee_name(n.func) == "_persist_verification_terminal"
    ]
    assert calls, "_lazy_check_verification no longer calls the terminal persister"
    for call in calls:
        kwargs = {kw.arg: kw for kw in call.keywords}
        assert "chain_ending" in kwargs, (
            "watchdog must pass chain_ending=True explicitly so a future "
            "stop reason cannot regress into a split-brain state."
        )
        assert isinstance(kwargs["chain_ending"].value, ast.Constant)
        assert kwargs["chain_ending"].value.value is True


# ---------------------------------------------------------------------------
# 2. Behaviour: the routing stage actually leaves verification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stop_reason",
    [
        "verification_log_stale",
        "verification_results_stale",
        "verification_thread_died_unexpectedly",
    ],
)
def test_watchdog_terminal_write_advances_routing_stage(
    hermetic_server, stop_reason
):
    """A watchdog kill must move ``plan_routing.current_phase`` to ``failed``.

    This is the exact live symptom: ``verification_status='failed'`` on
    the card while ``stage`` stayed ``verification_running``.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = f"watchdog-{stop_reason}"
    _seed_running_verification(db_path, plan_id, plans_root)

    server._persist_verification_terminal(plan_id, "failed", stop_reason)

    assert _phase(db_path, plan_id) == "failed", (
        f"stop_reason={stop_reason!r} left current_phase at "
        f"{_phase(db_path, plan_id)!r} — the card and the local state disagree."
    )


def test_watchdog_terminal_write_works_before_verification_cas(hermetic_server):
    """A watchdog that fires while ``stage`` is still ``executing`` must
    still roll the plan out of the chain (the auto-verification loop's
    own ``executing → verification`` CAS may have been lost)."""
    server, plans_root, db_path = hermetic_server
    plan_id = "watchdog-before-verification-cas"
    _seed_running_verification(
        db_path, plan_id, plans_root, phase="executing"
    )

    server._persist_verification_terminal(
        plan_id, "failed", "verification_log_stale"
    )

    assert _phase(db_path, plan_id) == "failed"


def test_mid_loop_terminal_write_leaves_routing_stage_alone(hermetic_server):
    """Guard the original intent: an interim ``_record_terminal`` call made
    while the executor subprocess is still mid-flight must NOT CAS the
    routing row (the executor owns ``current_phase`` until the chain
    really ends)."""
    server, plans_root, db_path = hermetic_server
    plan_id = "mid-loop-terminal"
    _seed_running_verification(db_path, plan_id, plans_root)

    server._persist_verification_terminal(plan_id, "failed", "no_repair_tasks")

    assert _phase(db_path, plan_id) == "verification_running"


def test_chain_ending_override_beats_reason_lookup(hermetic_server):
    """``chain_ending=True`` wins for a reason the vocabulary doesn't know."""
    server, plans_root, db_path = hermetic_server
    plan_id = "override-chain-ending"
    _seed_running_verification(db_path, plan_id, plans_root)

    server._persist_verification_terminal(
        plan_id, "failed", "some_future_watchdog_reason", chain_ending=True
    )

    assert _phase(db_path, plan_id) == "failed"


# ---------------------------------------------------------------------------
# 3. Staleness: per-VP attempt logs count as forward progress
# ---------------------------------------------------------------------------


def test_latest_mtime_accepts_multiple_globs(tmp_path):
    """``_latest_mtime`` must return the newest mtime across every glob."""
    import os
    import time

    import server

    logs = tmp_path / "logs"
    attempts = logs / "vp_attempts"
    attempts.mkdir(parents=True)
    orchestrator = logs / "verification_1_x.log"
    orchestrator.write_text("orchestrator\n")
    attempt = attempts / "vp_attempt_VP-034_1.log"
    attempt.write_text("attempt\n")

    old = time.time() - 5000.0
    new = time.time() - 5.0
    os.utime(orchestrator, (old, old))
    os.utime(attempt, (new, new))

    latest = server._latest_mtime(
        tmp_path, ("logs/verification_*.log", "logs/vp_attempts/*.log")
    )
    assert latest is not None
    assert abs(latest - new) < 60.0, (
        "a fresh vp_attempts log must win over a stale orchestrator log — "
        "otherwise a long single-VP retry is misjudged as a hung orchestrator"
    )

    # Back-compat: a bare string still works.
    assert server._latest_mtime(tmp_path, "logs/verification_*.log") == pytest.approx(
        old, abs=1.0
    )
    # No match → None (unchanged contract).
    assert server._latest_mtime(tmp_path, "logs/nope_*.log") is None


def test_staleness_check_covers_vp_attempts():
    """Source guard: the watchdog's log-staleness glob set must include
    ``logs/vp_attempts/*.log``."""
    segment = find_def("_lazy_check_verification").text
    assert "logs/vp_attempts/*.log" in segment, (
        "_lazy_check_verification no longer treats vp_attempts logs as "
        "progress evidence for verification_log_stale."
    )
    assert "verification_log_stale" in segment
