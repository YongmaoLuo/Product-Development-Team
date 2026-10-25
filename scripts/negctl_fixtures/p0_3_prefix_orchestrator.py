"""The pre-fix shape of the same-failure stop, kept as a fixture.

What this file is for
---------------------
The negative control for the P0-3 fix (``scripts/negctl_p0_3.sh``)
makes two claims about the same code: that the fix is in place, and
that the bug the fix removed was really there. The second claim needs
a copy of the code as it was *before* the fix.

Usually that copy comes out of the repository's own history: the most
recent revision of ``backend/verification/orchestrator.py`` whose
same-failure branch still stops with an empty ``repair_tasks`` and
does not dispatch a triage. A repository's history is not guaranteed
to hold it — a shallow clone has one commit in it, and a history that
was rewritten may have none.

So the shape is kept here as well, and the control falls back to this
file when the history cannot supply one. The fallback is not a
shortcut: the control analyses this file with the same analyser it
uses for a revision, and asserts the same things about it. A fixture
that stopped carrying the pre-fix shape makes the control fail, not
pass.

Why a fragment and not a copy
-----------------------------
Only the part the analyser looks at is kept: the threshold at ``1``
and the branch that terminates the chain at the cap with an empty
``repair_tasks`` and no triage. The branch itself is quoted verbatim
from the pre-fix source; the method around it is trimmed to what the
branch needs.

Nothing here is run. The file is parsed, not imported, and it is not a
test: it is evidence that a shell script reads, so its name does not
start with ``test_``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


class _PlanState:
    """A stand-in for the plan state the pre-fix branch stops."""

    def __init__(self) -> None:
        self.stopped_for: str = ""

    def stop_verification_loop(self, reason: str) -> None:
        self.stopped_for = reason


class _PreFixOrchestrator:
    """The same-failure branch as it was before the fix."""

    def __init__(self) -> None:
        # 3. Hard cap ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` as
        #    a safety net — enough chances for feedback to land, but
        #    not so many that a truly broken VP burns tokens forever.
        self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 1
        self._consecutive_same_failure_rounds = 0
        self.plan_state = _PlanState()
        self.logger: Any = None

    def _safe_phase_call(self, phase: str, status: str) -> None:
        """A stand-in for the phase call the branch made; never called."""
        return None

    def handle_same_failure(
        self, current_failed_ids: Iterable[str]
    ) -> Dict[str, Any]:
        """The pre-fix same-failure branch: stop at the cap, ask nobody."""
        current_failed_ids = list(current_failed_ids)
        if (
            current_failed_ids
            and len(current_failed_ids) > 0
        ):
            self._consecutive_same_failure_rounds += 1
            if (
                self._consecutive_same_failure_rounds
                >= self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS
            ):
                # Genuinely stuck — fall through to terminate.
                self._safe_phase_call("verification_failed", "verification_failed")
                self.plan_state.stop_verification_loop(
                    "same_failure_repeated_after_max_attempts"
                )
                return {
                    "should_continue": False,
                    "should_stop": True,
                    "stop_reason": "same_failure_repeated_after_max_attempts",
                    "repair_tasks": [],
                    "waiting_for_user": False,
                    "status": "loop_stopped",
                }
        # Not yet at the cap — keep generating repair tasks so the
        # executor can try once more.
        repair_tasks: List[Dict[str, Any]] = [
            {"vp_id": vp_id, "instruction": "try again"}
            for vp_id in current_failed_ids
        ]
        return {
            "should_continue": True,
            "repair_tasks": repair_tasks,
        }
