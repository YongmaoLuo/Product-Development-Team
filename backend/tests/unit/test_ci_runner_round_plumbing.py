"""The runner that judges a round has to be told which round it is —
but only when it can hear it.

Why this file exists
--------------------
``run_ci_verification`` grew a ``round_no`` keyword (2026-11-10): the
evidence directory is reused across rounds, so each round's copy of
``ci_output.json`` has to be written beside the main artifact under its
own name (``ci_output.round-<n>.json``) — otherwise the next round
overwrites this round's evidence and the audit trail loses a round.

But the runner that grades a gate is not always *this* framework's copy
of ``verification_ci_runner``: the loader picks the tree under test's
copy and reloads it every round, so a project that has not been updated
yet hands back an **older** runner whose signature carries no
``round_no``. Calling that one with ``round_no=`` raises ``TypeError``,
and because ``Orchestrator._run_full_ci_vp`` wraps the call in
``try``/``except``, the crash does not take the round down — it turns
**every** ``full_ci`` VP into a FAILED verdict reading ``full_ci runner
crashed: TypeError``. A stale grader would then look exactly like a
broken product.

So the round is passed **only when the runner can take it**, and the
decision is made from the runner's *signature*, never from a guess.

What is pinned here
-------------------
* a runner whose signature carries ``round_no`` gets it;
* a runner whose signature does not is called exactly as before;
* a runner that accepts ``**kwargs`` accepts it too;
* a runner whose signature cannot be read is treated as one that does
  **not** accept it — an unreadable signature is never a licence to
  guess;
* round ``0`` is never passed: there is no "round 0" copy to write, and
  passing one would name a file the contract does not describe. The same
  goes for a negative round and for ``None``;
* a ``bool`` is never passed either: ``bool`` is a **subclass** of
  ``int``, so ``True`` slips through an "is it a number?" check and
  would name a file the contract does not describe.

The interface contract
----------------------
Two module-level functions in ``verification_agent``::

    _runner_accepts_round(runner) -> bool
    _ci_runner_kwargs(*, runner, project_dir, artifact_dir, round_no) -> dict

The second one is what the orchestrator actually passes to the runner;
the first one is the decision it is built on.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_agent import (  # noqa: E402
    _ci_runner_kwargs,
    _runner_accepts_round,
)


# ---------------------------------------------------------------------------
# Stubs — one per shape of runner the loader can hand back
# ---------------------------------------------------------------------------


def _accepting_runner(vp, *, project_dir, artifact_dir, round_no=None):
    """A runner from a tree that already knows about ``round_no``."""
    return {"round_no": round_no}


def _rejecting_runner(vp, *, project_dir, artifact_dir):
    """A runner from a tree that predates ``round_no``."""
    return {"round_no": None}


def _var_keyword_runner(vp, *, project_dir, artifact_dir, **kwargs):
    """A runner that swallows whatever it is given."""
    return {"round_no": kwargs.get("round_no")}


class _UnreadableSignatureRunner:
    """A callable whose signature cannot be read.

    ``inspect.signature`` raises ``ValueError`` for it — the same shape
    of failure a builtin without a text signature produces.
    """

    @property
    def __signature__(self):
        raise ValueError("no signature found for this runner")

    def __call__(self, vp, *, project_dir, artifact_dir, round_no=None):
        return {"round_no": round_no}


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_round_passed_when_runner_accepts():
    """A runner that knows ``round_no`` is told which round this is."""
    kwargs = _ci_runner_kwargs(
        runner=_accepting_runner,
        project_dir="/tmp/project",
        artifact_dir="/tmp/artifacts",
        round_no=3,
    )

    assert kwargs == {
        "project_dir": "/tmp/project",
        "artifact_dir": "/tmp/artifacts",
        "round_no": 3,
    }


def test_round_omitted_when_runner_rejects():
    """A runner that does not know ``round_no`` is called as it always was."""
    kwargs = _ci_runner_kwargs(
        runner=_rejecting_runner,
        project_dir="/tmp/project",
        artifact_dir="/tmp/artifacts",
        round_no=3,
    )

    assert kwargs == {
        "project_dir": "/tmp/project",
        "artifact_dir": "/tmp/artifacts",
    }
    assert "round_no" not in kwargs


def test_zero_round_is_not_passed():
    """Round 0 is not a round — there is no ``ci_output.round-0.json``."""
    kwargs = _ci_runner_kwargs(
        runner=_accepting_runner,
        project_dir="/tmp/project",
        artifact_dir="/tmp/artifacts",
        round_no=0,
    )

    assert "round_no" not in kwargs


def test_var_keyword_runner_accepts_round():
    """``**kwargs`` means the runner can take the round."""
    assert _runner_accepts_round(_var_keyword_runner) is True

    kwargs = _ci_runner_kwargs(
        runner=_var_keyword_runner,
        project_dir="/tmp/project",
        artifact_dir="/tmp/artifacts",
        round_no=7,
    )

    assert kwargs["round_no"] == 7


def test_unreadable_signature_omits_round():
    """An unreadable signature means "does not accept" — never a guess."""
    runner = _UnreadableSignatureRunner()

    assert _runner_accepts_round(runner) is False

    kwargs = _ci_runner_kwargs(
        runner=runner,
        project_dir="/tmp/project",
        artifact_dir="/tmp/artifacts",
        round_no=3,
    )

    assert kwargs == {
        "project_dir": "/tmp/project",
        "artifact_dir": "/tmp/artifacts",
    }


def test_negative_and_missing_round_are_not_passed():
    """A negative round and a missing round are both "no round"."""
    for round_no in (-1, None):
        kwargs = _ci_runner_kwargs(
            runner=_accepting_runner,
            project_dir="/tmp/project",
            artifact_dir="/tmp/artifacts",
            round_no=round_no,
        )
        assert "round_no" not in kwargs, round_no


def test_bool_round_is_not_passed():
    """``True`` and ``False`` are not round numbers — ``bool`` is an ``int``.

    ``bool`` is a **subclass** of ``int`` in Python, so
    ``isinstance(True, int)`` is ``True`` and ``True > 0`` holds: a guard
    that only asked "is it a number?" would let a bool through and the
    runner would be told "this is round 1" — or, worse, would write
    ``ci_output.round-True.json``, a file name no contract describes.
    So a bool is rejected **before** the number is compared, and no bool
    ever reaches the runner.
    """
    for round_no in (True, False):
        kwargs = _ci_runner_kwargs(
            runner=_accepting_runner,
            project_dir="/tmp/project",
            artifact_dir="/tmp/artifacts",
            round_no=round_no,
        )
        assert "round_no" not in kwargs, round_no


def test_accepting_runner_is_recognised():
    """The decision itself: signature carries ``round_no`` -> accepted."""
    assert _runner_accepts_round(_accepting_runner) is True
    assert _runner_accepts_round(_rejecting_runner) is False


# ---------------------------------------------------------------------------
# The plumbing itself — the orchestrator's call site
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=Mock(),
    )


def _ci_vp(**over) -> dict:
    vp = {
        "id": "VP-017",
        "title": "【全量】Nightly CI",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": "exit 0",
    }
    vp.update(over)
    return vp


def _passing_result(vp):
    from verification_ci_runner import CiRunResult

    return CiRunResult(
        status="PASSED",
        reasons=[],
        evidence={"vp_id": str(vp.get("id", "unknown"))},
    )


def test_orchestrator_tells_the_runner_which_round_it_is(tmp_path, monkeypatch):
    """The runner that judges this round is told this round's number."""
    import verification_ci_runner

    seen: dict = {}

    def _fake_run(vp, *, project_dir, artifact_dir=None, round_no=None):
        seen["round_no"] = round_no
        return _passing_result(vp)

    monkeypatch.setattr(
        verification_ci_runner, "run_ci_verification", _fake_run,
    )

    agent = _make_agent(tmp_path)
    agent._current_round = 3

    verdict = agent._run_full_ci_vp(_ci_vp())

    assert seen["round_no"] == 3
    assert verdict["status"] == "PASSED", verdict


def test_orchestrator_does_not_crash_a_runner_that_predates_round_no(
    tmp_path, monkeypatch,
):
    """An old runner is called as it always was — no ``TypeError``.

    This is the accident the plumbing exists to prevent: passing
    ``round_no`` to a runner that does not know it raises ``TypeError``,
    and the ``try``/``except`` around the call turns **every** ``full_ci``
    VP into a FAILED verdict reading ``full_ci runner crashed``.
    """
    import verification_ci_runner

    def _fake_run(vp, *, project_dir, artifact_dir=None):
        return _passing_result(vp)

    monkeypatch.setattr(
        verification_ci_runner, "run_ci_verification", _fake_run,
    )

    agent = _make_agent(tmp_path)
    agent._current_round = 3

    verdict = agent._run_full_ci_vp(_ci_vp())

    assert verdict["status"] == "PASSED", verdict
    assert "full_ci runner crashed" not in " ".join(verdict.get("reasons") or [])
