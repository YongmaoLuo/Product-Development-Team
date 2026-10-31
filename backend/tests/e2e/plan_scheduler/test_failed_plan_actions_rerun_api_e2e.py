"""End-to-end: re-running the verification of a **failed** plan, over HTTP.

What this file is for
---------------------
The panel's second action on a failed plan is 「重跑验证」 — re-run the
verification.  It is made of **two** commands, and the order is the
contract:

1. ``POST /api/verification/{id}/reset_rounds`` — hand the plan a fresh
   batch of rounds (the round *counter* is rewound; the budget is not);
2. ``POST /api/verification/{id}/start`` — actually begin the round.

Both halves have to work on a plan that is in the ``failed`` phase, and
the failure mode of the second one is a **409**: ``/start`` decides
whether a plan may enter a round through a compare-and-set over the
routing row, and a source phase outside that set is refused with
``stage_mismatch``.  A re-run that ends in a 409 is a button that does
nothing — and it looks exactly like a re-run that worked, because the
request went out and the plan stayed where it was.

So this file asserts the chain on the wire, against a really running
server: plan **B** (seeded in ``failed``) is reset, started, and its
status is read **immediately** — the round is written before the POST
returns, so a status that is not ``running`` right after the start is
the truth about the product, not a race this case may wait out.

What is real here
-----------------
The world is the one task 4 built (:class:`FailedPlanWorld`): a child
``uvicorn`` process, a SQLite file on disk, plan directories, and the
scheduler pointed at a settings file of its own.  This file imports
that world rather than building a second one, so the seed data, the
phase and the project directory all come from the same place the
「重新排队执行」 case uses.

The model is the scripted double the world already asks for — no real
provider is ever reached.  What the world does **not** do is point that
double at its fixture directory, and a double with no directory cannot
even be *created*: the round dies at ``orchestrator_init_failed``
before it enters a round, and the status the panel polls then says
``failed`` — a failure that has nothing to do with the re-run this case
is about.  :func:`rerun_world` supplies the missing half.

What this case does **not** claim
---------------------------------
It says the re-run **reaches a round**.  It does not say the round
passes: what the verification engine does after the start is that
engine's business, and this case has no business judging it.  The
assertion is deliberately narrow so that it stays true.

The world is left the way it was found: the round is stopped in a
``finally``, so the case does not leave a verification thread running
behind it.  A stop that fails cannot mask the assertion it is
cleaning up after — the cleanup is best-effort by design, and the
plan's directory is thrown away with the temporary root anyway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .test_failed_plan_actions_api_e2e import (
    FAILED_PHASE,
    PLAN_B_ID,
    PLAN_VERIFY,
    FailedPlanWorld,
)

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# The paths the re-run is posted to
# ---------------------------------------------------------------------------

#: The operator's "give this plan a fresh batch of rounds" action — the
#: first half of a re-run.
PLAN_VERIFY_RESET_ROUNDS: str = "/api/verification/{plan_id}/reset_rounds"

#: The status the panel polls — what the operator is shown.
PLAN_VERIFY_STATUS: str = "/api/verification/{plan_id}/status"

#: The operator's "stop the round" action — how this case leaves the
#: world the way it found it.
PLAN_VERIFY_STOP: str = "/api/verification/{plan_id}/stop"

#: The status a round that has just been entered reports.
RUNNING_STATUS: str = "running"


# ---------------------------------------------------------------------------
# The scripted model double
# ---------------------------------------------------------------------------

#: The environment variable that points the scripted double at the
#: directory its answers live in.
LLM_SCRIPT_DIR_ENV: str = "PDT_LLM_SCRIPT_DIR"

#: The key the double's registry holds the one answer under.
LLM_SCRIPT_KEY: str = "any"

#: The fragment of a prompt the answer is registered for.  One letter:
#: the double picks the *most specific* fragment that the prompt
#: contains, so a short one is the catch-all for the prompts that
#: carry it — and a prompt that does not carry it is refused by the
#: double, which is the double's "no fallback" rule and not a hole.
LLM_SCRIPT_MATCH: str = "e"

#: The answer itself.  It is deliberately **not** a verification plan:
#: the round then falls back to its minimal plan and is over in
#: seconds.  What this case measures is that a round was *entered*, not
#: what it concluded — and a round that lingers for minutes would make
#: the case slower without making it truer.
LLM_SCRIPT_ANSWER: str = "A CANSWER"


@pytest.fixture
def rerun_world(tmp_path, monkeypatch):
    """A running server whose scripted model double has its fixtures.

    The double is switched on by the world (no real provider is ever
    reached); this fixture gives it the directory it reads its answers
    from.  Without it the round cannot even be created, and the case
    would measure a configuration mistake instead of the re-run.
    """
    script_dir = Path(tmp_path) / "llm-scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "registry.json").write_text(
        json.dumps(
            {
                "prompts": {
                    LLM_SCRIPT_KEY: {
                        "match": LLM_SCRIPT_MATCH,
                        "file": f"{LLM_SCRIPT_KEY}.txt",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (script_dir / f"{LLM_SCRIPT_KEY}.txt").write_text(
        LLM_SCRIPT_ANSWER, encoding="utf-8"
    )
    monkeypatch.setenv(LLM_SCRIPT_DIR_ENV, str(script_dir))

    the_world = FailedPlanWorld(Path(tmp_path))
    try:
        the_world.start()
        yield the_world
    finally:
        the_world.stop()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def _post(world: FailedPlanWorld, path: str) -> tuple[int, Any]:
    """POST an empty JSON body to *path*; return ``(status, body)``.

    The body is ``{}`` and not "no body": the request models of the two
    endpoints (``ResetRoundsRequest``, ``StartVerificationRequest``) are
    read from the body, and an endpoint that is handed nothing answers
    422 rather than the thing this case is about.
    """
    return world.http(path, method="POST", body=b"{}")


def test_rerun_verification_reaches_running(rerun_world):
    """A failed plan's verification can be re-run, and the round is entered.

    The chain is asserted as the operator performs it: reset the round
    counter, start the round, and read the status.  The start must not
    be a 409 (``stage_mismatch`` — the plan's phase is not a source
    phase of ``/start``), and the status read immediately afterwards must
    say the round is ``running``.

    The status is read **without a sleep** on purpose: the round is
    written to the state database before the POST returns, so a status
    that is already terminal at that point means the round ran and
    finished before the assertion could look at it.  That is worth
    seeing — it is reported with the whole status document rather than
    waited out, because widening the wait would hide it.
    """
    world = rerun_world

    # The precondition the whole case rests on: plan B really is in the
    # failed phase, in the routing row the product reads.
    assert world.phase_of(PLAN_B_ID) == FAILED_PHASE, (
        "the seeded plan is not in the failed phase, so a conflict would "
        "not be about the re-run this case is about"
    )

    try:
        # 1. The first half of the re-run: a fresh batch of rounds.
        reset_status, reset_payload = _post(
            world, PLAN_VERIFY_RESET_ROUNDS.format(plan_id=PLAN_B_ID)
        )
        assert reset_status == 200, (
            f"resetting the rounds of a failed plan was answered with "
            f"{reset_status}: {reset_payload!r}"
        )

        # 2. The second half: the round itself.
        start_status, start_payload = _post(
            world, PLAN_VERIFY.format(plan_id=PLAN_B_ID)
        )
        assert start_status == 200, (
            f"starting the verification of a failed plan was answered with "
            f"{start_status}: {start_payload!r}; a 409 stage_mismatch means "
            f"the re-run never reached a round"
        )
        assert start_payload.get("status") == "started", (
            f"the start did not report a started round: {start_payload!r}"
        )

        # 3. The status the operator is shown — read immediately, with no
        #    sleep: the round is written before the POST returns.
        status_code, status_payload = world.http(
            PLAN_VERIFY_STATUS.format(plan_id=PLAN_B_ID)
        )
        assert status_code == 200, (
            f"the status of a re-run plan was answered with {status_code}: "
            f"{status_payload!r}"
        )
        if status_payload.get("verification_status") != RUNNING_STATUS:
            print(
                "[rerun_verification] the status right after the start was "
                f"not {RUNNING_STATUS!r}: {status_payload!r}"
            )
        assert status_payload.get("verification_status") == RUNNING_STATUS, (
            f"the verification status right after the start was "
            f"{status_payload!r}; the re-run did not enter a round"
        )
    finally:
        # Leave the world the way it was found.  A stop that fails is not
        # this case's business — it must not mask the assertion above.
        try:
            stop_status, stop_payload = _post(
                world, PLAN_VERIFY_STOP.format(plan_id=PLAN_B_ID)
            )
            if stop_status != 200:
                print(
                    f"[rerun_verification] the cleanup stop answered with "
                    f"{stop_status}: {stop_payload!r}"
                )
        except Exception as exc:  # noqa: BLE001 — cleanup must not fail the case
            print(f"[rerun_verification] the cleanup stop failed: {exc!r}")