"""The pre-fix shape of the verdict-parse arm, kept as a fixture.

What this file is for
---------------------
The negative control for the P0-1 fix (``scripts/negctl_p0_1.sh``)
makes two claims about the same code: that the fix is in place, and
that the bug the fix removed was really there. The second claim needs
a copy of the code as it was *before* the fix.

Usually that copy comes out of the repository's own history: the most
recent revision of ``backend/verification_subagent.py`` whose
``except VerdictParseError`` arm in ``_execute_attempt`` still builds
the inline ``Verdict(verdict="FAILED")``. A repository's history is
not guaranteed to hold it — a shallow clone has one commit in it, and
a history that was rewritten may have none.

So the shape is kept here as well, and the control falls back to this
file when the history cannot supply one. The fallback is not a
shortcut: the control analyses this file with the same analyser it
uses for a revision, and asserts the same things about it. A fixture
that stopped carrying the pre-fix shape makes the control fail, not
pass.

Why a fragment and not a copy
-----------------------------
Only the part the analyser looks at is kept: a ``_execute_attempt``
whose ``except VerdictParseError`` arm builds a FAILED verdict. The
arm itself is quoted verbatim from the pre-fix source; the method
around it is trimmed to what the arm needs.

Nothing here is run. The file is parsed, not imported, and it is not a
test: it is evidence that a shell script reads, so its name does not
start with ``test_``.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Verdict:
    """A stand-in for the verdict object the sub-agent returns."""

    def __init__(
        self,
        verdict: str,
        reasons: Optional[List[str]] = None,
        evidence: Optional[List[str]] = None,
        model_complexity: Any = None,
    ) -> None:
        self.verdict = verdict
        self.reasons = list(reasons or [])
        self.evidence = list(evidence or [])
        self.model_complexity = model_complexity


class VerdictParseError(Exception):
    """The payload was not a verdict this sub-agent can read."""


def parse_verdict_to_dataclass(raw: str) -> Verdict:
    """Stand-in for the parser the sub-agent uses; never called."""
    raise VerdictParseError(raw)


class _PreFixSubAgent:
    """The sub-agent as it was before the fix, in the part that matters."""

    def __init__(self, model_complexity: Any = None) -> None:
        self.model_complexity = model_complexity

    async def _execute_attempt(self, raw: str) -> Verdict:
        """Return the parsed verdict — or answer the failure inline."""
        try:
            return parse_verdict_to_dataclass(raw)
        except VerdictParseError as e:
            return Verdict(
                verdict="FAILED",
                reasons=[f"verdict parse error: {e}"],
                evidence=[raw[:500]],
                model_complexity=self.model_complexity,
            )
