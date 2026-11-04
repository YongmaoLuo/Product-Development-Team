"""
The wall-clock budget a verification sub-agent may spend — and the
"no progress" heartbeat that has to fire before it does.

2026-11-05 (arch decision point 9, defect B item 5).  Two of the five
verification methods — ``ui_validation`` and ``e2e`` — used to carry the
same flat one-hour budget as the whole-repo gate.  A sub-agent that hung
therefore burned the entire hour before anything was said about it, and
the only bound in force was "how long has this been going".

The budget is per method now: 900 seconds for those two, 3600 for the
rest.  And the 900 is paired with a heartbeat at 300 seconds of *no
progress* — no new tool call, no new output — so a wedged sub-agent is
terminated on the evidence of its own silence rather than on the
exhaustion of a total.

The heartbeat has to be strictly earlier than the ceiling.  A heartbeat
at or above the wall clock could never fire, and the "no progress"
contract would silently degrade back into "the total duration is the
only bound" — the thing this change exists to remove.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_config import DEFAULT_PER_METHOD_TIMEOUT_SECONDS
from verification_subagent import (
    METHOD_WALL_CLOCK_CAPS,
    NO_PROGRESS_HEARTBEAT_SECONDS,
    VerificationSubAgent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingTool:
    """A coding-tool double that remembers the timeout it was handed.

    The timeout the query is called with IS the inner silence window
    (``coding_tool`` reads it as ``total_timeout`` — seconds of stdout
    silence before the subprocess is killed).  Remembering it is how the
    heartbeat is observed from the outside.
    """

    def __init__(self) -> None:
        self.received_timeout: Optional[int] = None
        self.calls: int = 0

    def query_json(
        self,
        prompt: Optional[str] = None,
        system_instruction: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls += 1
        self.received_timeout = timeout
        return {"verdict": "PASSED", "reasons": [], "evidence": []}


async def _run_one_attempt(method: str, tmp_path: Path) -> _RecordingTool:
    """Drive one attempt of ``method`` against a recording tool."""
    tool = _RecordingTool()
    agent = VerificationSubAgent(method=method, max_retries=0)
    await agent._execute_attempt(
        vp_node={
            "id": "VP-BUDGET",
            "title": "timeout budget",
            "expected_result": "green",
        },
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=tmp_path / f"{method}-attempt.log",
        attempt=0,
        project_dir=tmp_path,
    )
    return tool


# ---------------------------------------------------------------------------
# 1. The declared per-method defaults
# ---------------------------------------------------------------------------


def test_config_defaults_for_ui_and_e2e():
    """The two methods that used to burn an hour declare 900 seconds.

    ``DEFAULT_PER_METHOD_TIMEOUT_SECONDS`` is what
    :class:`verification_config.TimeoutPolicy` falls back to, and what
    the execution profile reports per method.
    """
    assert DEFAULT_PER_METHOD_TIMEOUT_SECONDS["ui_validation"] == 900
    assert DEFAULT_PER_METHOD_TIMEOUT_SECONDS["e2e"] == 900


# ---------------------------------------------------------------------------
# 2. The methods that keep the hour
# ---------------------------------------------------------------------------


def test_full_ci_unchanged():
    """The whole-repo gate and the two other methods keep 3600.

    ``full_ci`` runs the repository's own gate (tens of minutes by
    design), and the flat hour is the only bound it has ever had.
    """
    assert DEFAULT_PER_METHOD_TIMEOUT_SECONDS["full_ci"] == 3600
    assert DEFAULT_PER_METHOD_TIMEOUT_SECONDS["api_test"] == 3600
    assert DEFAULT_PER_METHOD_TIMEOUT_SECONDS["code_review"] == 3600


# ---------------------------------------------------------------------------
# 3. The per-method wall-clock cap the sub-agent actually enforces
# ---------------------------------------------------------------------------


def test_wall_clock_cap_per_method():
    """``wall_clock_cap()`` is the budget one attempt may spend.

    The two narrowed methods get 900; every other method keeps the flat
    hour.  The cap is the *smaller* of the two — a per-instance override
    that is already tighter still wins, because it was declared for that
    attempt on purpose.
    """
    assert VerificationSubAgent(method="ui_validation").wall_clock_cap() == 900
    assert VerificationSubAgent(method="e2e").wall_clock_cap() == 900

    for method in ("full_ci", "api_test", "code_review"):
        assert VerificationSubAgent(method=method).wall_clock_cap() == 3600

    # A method name that arrives with padding or a different case is
    # still that method.  (The constructor validates the method against
    # the template registry, so a re-cased name can only arrive by
    # mutation — which is exactly why the lookup cannot rely on the
    # constructor having normalised it.)
    padded = VerificationSubAgent(method="e2e")
    padded.method = "  E2E  "
    assert padded.wall_clock_cap() == 900


# ---------------------------------------------------------------------------
# 4. The heartbeat fires strictly before the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_is_strictly_below_wall_clock(tmp_path):
    """300 seconds of silence is a verdict; 900 seconds is the ceiling.

    The heartbeat is handed to the query as its silence window, so it is
    the number the subprocess is killed on when nothing comes out of it.
    It has to be strictly below the wall clock: at or above it, the
    ceiling would always win the race and "no progress" would never be
    distinguishable from "still running".
    """
    assert NO_PROGRESS_HEARTBEAT_SECONDS < METHOD_WALL_CLOCK_CAPS["ui_validation"]
    assert NO_PROGRESS_HEARTBEAT_SECONDS < METHOD_WALL_CLOCK_CAPS["e2e"]
    assert NO_PROGRESS_HEARTBEAT_SECONDS < 900

    for method in ("ui_validation", "e2e"):
        tool = await _run_one_attempt(method, tmp_path)
        assert tool.calls == 1
        assert tool.received_timeout == NO_PROGRESS_HEARTBEAT_SECONDS, (
            f"{method} did not get the no-progress heartbeat as its "
            f"silence window: {tool.received_timeout!r}"
        )

    # A method that was not narrowed keeps the coding tool's own default
    # (``None`` → ``coding_tool.DEFAULT_TOTAL_TIMEOUT``): the heartbeat
    # is a per-method decision, not a blanket one.
    untouched = await _run_one_attempt("code_review", tmp_path)
    assert untouched.received_timeout is None
