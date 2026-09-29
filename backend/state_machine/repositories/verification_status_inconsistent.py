"""Custom exceptions for the verification repository's atomicity contract.

Why this exists (2026-09-09 audit on the 2026-09-04 plan):
``_persist_verification_terminal``
step 1 called ``VerificationRepository.complete_round`` which in turn
ran a SQL UPDATE setting both ``verification_status`` AND ``results``
columns. The two columns live on the same row in the same transaction
so atomicity *should* be guaranteed. But the post-mortem state.db
showed ``results.recorded_by = "_persist_verification_terminal"`` (so
step 1 did execute) while ``verification_status`` was still
``"pending"`` (the initial INSERT value that step 1 should have
replaced). Root-causing why needs server.log retention that was
already rotated out, so instead of guessing, this exception lets the
repository raise loudly when a future drift happens, instead of
producing a "🔄 验证中" card forever.
"""

from __future__ import annotations

from typing import Optional


class VerificationStatusInconsistent(Exception):
    """Raised when a post-write invariant check fails.

    The repository's ``complete_round`` / ``mark_stopped`` /
    ``_persist_verification_terminal`` step 1 promises that after
    the SQL UPDATE commits, ``plan_verification.verification_status``
    matches what the caller asked for. If a read-back shows it
    drifted (e.g. an external writer reset it, or a future code
    change split the UPDATE), this exception carries the diagnostic
    detail so the framework's outer try/except can log a
    ``verification_status_inconsistent`` event and the operator can
    investigate instead of seeing a stuck "验证中" card.
    """

    def __init__(
        self,
        plan_id: str,
        expected_status: str,
        actual_status: str,
        actual_recorded_by: Optional[str],
        detail: str,
    ) -> None:
        self.plan_id = plan_id
        self.expected_status = expected_status
        self.actual_status = actual_status
        self.actual_recorded_by = actual_recorded_by
        self.detail = detail
        super().__init__(
            f"verification_status drift for plan {plan_id!r}: "
            f"expected={expected_status!r} actual={actual_status!r} "
            f"recorded_by={actual_recorded_by!r} ({detail})"
        )