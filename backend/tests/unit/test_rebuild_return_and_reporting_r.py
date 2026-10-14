"""Rebuild: file a lost report honestly instead of as "nothing ran" (task R).

2026-10-14, a 2026-10-12 production plan, round 1
(``logs/verification_1_20261014_102843.log``)::

    10:34:30  the rebuild agent ran maturin
    10:40:51  lib*.dylib mtime (measured)
    10:41:56  agent self-check: "FRESH: artefact newer than newest .rs"
    11:22:22  the turn in which it would report that raised
              SceneProviderUnavailable (8 x 300 s quota probes first)

That round's ``binary_rebuild_attempted`` row said
``success: false``, ``commands_run: []`` and
``diagnostics: "agent invocation failed: ..."``. All three were false:
the agent *had* run. What failed was the collection of its answer. The
old wording collapsed "the reply never arrived" and "the agent was
never sent" into one sentence, and the empty ``commands_run`` that came
with it travelled on into ``binary_stale_blocked``'s evidence pack -
which is what the repair agent reads.

What is pinned here
-------------------
1. **Two dispatch failures are two different failures.** "Report lost"
   and "never dispatched" get different diagnostics, and ``commands_run``
   says "unknown" rather than "nothing ran" on the first while staying
   genuinely empty on the second.
2. **The deliberate conjunction survives.** A lost report on a fresh
   artefact still reports ``success=False`` with ``post_check_passed=True``
   - the conservative contract at the end of ``attempt_intelligent_rebuild``
   is unchanged, and only the *description* got fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _stale() -> Any:
    from binary_freshness import FreshnessReport, KIND_RUST_PYTHON

    return FreshnessReport(
        kind=KIND_RUST_PYTHON, status="FAILED",
        detail="test: libnative.so mtime (1) is older than newest .rs mtime (2)",
        evidence="test: mtime mismatch",
        rebuild_command="maturin develop --release",
        binary_path="/x/target/release/libnative.so",
        newest_source_mtime=2.0, binary_mtime=1.0,
    )


def _fresh() -> Any:
    from binary_freshness import FreshnessReport, KIND_RUST_PYTHON

    return FreshnessReport(
        kind=KIND_RUST_PYTHON, status="PASSED",
        detail="test: libnative.so is fresh",
        evidence="test: mtime ok",
        rebuild_command=None,
        binary_path="/x/target/release/libnative.so",
        newest_source_mtime=1.0, binary_mtime=2.0,
    )


class _ExplodingClaudeCodingTool:
    """Dispatched, and the provider layer ate the answer.

    ``RuntimeError`` stands in for ``SceneProviderUnavailable``, which is
    what the 2026-10-14 rebuild actually died on.
    """

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def query_json(self, prompt, system_instruction=None, timeout=None,
                   allowed_tools=None, **kwargs):
        self.calls += 1
        raise self.exc


class _NullSettingsBuilder:
    """``SettingsJsonBuilder`` stand-in that writes no temp file."""

    def __init__(self, vp_id=None, plan_id=None, **kwargs) -> None:
        pass

    def build(self):
        return None

    def cleanup(self) -> None:
        return None


# ---------------------------------------------------------------------------
# The record must not contradict what happened
# ---------------------------------------------------------------------------


def test_lost_report_and_never_dispatched_are_different_failures(
    tmp_path: Path, monkeypatch,
) -> None:
    """The old wording said "agent invocation failed" for both.

    On 2026-10-14 that single sentence told the operator the agent had
    never been sent, when it had been running maturin for six minutes.
    This pins the two cases apart: the diagnostics must name which one
    happened, and ``commands_run`` must not say "nothing ran" for the
    case where the agent demonstrably ran.
    """
    import binary_rebuild_agent
    from binary_rebuild_agent import BinaryRebuildAgent

    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _NullSettingsBuilder,
    )
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    # --- case 1: dispatched, answer lost ------------------------------
    tool = _ExplodingClaudeCodingTool(
        RuntimeError("SceneProviderUnavailable: no provider"),
    )
    lost = BinaryRebuildAgent(tool, plan_id="p", vp_id="VP-1").run(
        project, _stale(),
    )
    assert lost.success is False
    assert "dispatched but its report never came back" in lost.diagnostics, (
        f"a lost report must say so; got {lost.diagnostics!r}"
    )
    assert "SceneProviderUnavailable" in lost.diagnostics, lost.diagnostics
    assert lost.commands_run == [
        binary_rebuild_agent.AGENT_REPORT_UNAVAILABLE_COMMANDS,
    ], (
        "commands_run must say 'unknown', not 'nothing ran'; the agent "
        f"may well have rebuilt the binary before its report died; got "
        f"{lost.commands_run!r}"
    )
    assert lost.agent_used is True, (
        "an agent that was dispatched was used, whatever became of it"
    )
    assert tool.calls == 1

    # --- case 2: never dispatched -------------------------------------
    class _BoomSettingsBuilder:
        def __init__(self, vp_id=None, plan_id=None, **kwargs) -> None:
            pass

        def build(self):
            raise OSError("no writable temp dir for settings.json")

        def cleanup(self) -> None:
            return None

    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _BoomSettingsBuilder,
    )
    never = _ExplodingClaudeCodingTool(RuntimeError("must not be reached"))
    not_sent = BinaryRebuildAgent(never, plan_id="p", vp_id="VP-2").run(
        project, _stale(),
    )
    assert "never dispatched" in not_sent.diagnostics, not_sent.diagnostics
    assert not_sent.commands_run == [], (
        f"nothing ran, so an empty list is the true answer here; got "
        f"{not_sent.commands_run!r}"
    )
    assert not_sent.agent_used is False, (
        "no agent was used - the failure was before dispatch"
    )
    assert never.calls == 0, "the coding tool must not have been called at all"


def test_lost_report_stays_a_failure_even_when_the_artefact_is_fresh(
    tmp_path: Path, monkeypatch,
) -> None:
    """The conjunction is deliberate and must survive the reporting fix.

    ``result.success = result.success and result.post_check_passed`` is
    strictly more conservative than ``service_restart_agent``'s, and
    :func:`attempt_intelligent_rebuild` keeps it. So the 2026-10-14 shape
    - agent's report lost, artefact demonstrably fresh - still reports
    ``success=False`` with ``post_check_passed=True``. What this test
    forbids is the *other* half of the temptation: reporting ``success``
    straight off ``post_check_passed`` and calling it a day.
    """
    from binary_rebuild_agent import attempt_intelligent_rebuild

    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _NullSettingsBuilder,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.rebuild_binary", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.check_binary_freshness", lambda *a, **k: _fresh(),
    )

    result = attempt_intelligent_rebuild(
        Path(tmp_path), _stale(),
        coding_tool=_ExplodingClaudeCodingTool(
            RuntimeError("SceneProviderUnavailable: no provider"),
        ),
        plan_id="p", vp_id="VP-1",
    )

    assert result.post_check_passed is True, (
        f"the artefact is fresh and the post-check must say so; got {result}"
    )
    assert result.success is False, (
        "success must stay the conjunction - a lost report is still a "
        f"failure; got {result.to_dict()}"
    )
    assert result.rebuilt_binary_path == "/x/target/release/libnative.so", result
    assert result.to_dict()["commands_run"], (
        "the repair agent reads this field; an empty list tells it the "
        f"agent ran nothing. Got {result.to_dict()}"
    )


def test_lost_report_reaches_the_repair_agent_as_unknown_not_empty(
    tmp_path: Path, monkeypatch,
) -> None:
    """The channel the empty list used to leak down.

    ``_build_binary_freshness_blocked_verdict`` is what a stale VP's
    BLOCKED verdict is built from, and ``evidence['rebuild_attempt']`` is
    what a repair-task generator reads. That round had no VP reach
    BLOCKED, so nothing fired - but the channel is open, and this walks a
    real lost-report result all the way into the evidence pack.
    """
    from binary_rebuild_agent import BinaryRebuildAgent
    from verification_agent import Orchestrator

    monkeypatch.setattr(
        "verification_subagent.SettingsJsonBuilder", _NullSettingsBuilder,
    )
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    result = BinaryRebuildAgent(
        _ExplodingClaudeCodingTool(
            RuntimeError("SceneProviderUnavailable: no provider"),
        ),
        plan_id="p", vp_id="VP-1",
    ).run(project, _stale())

    orchestrator = Orchestrator.__new__(Orchestrator)
    verdict = orchestrator._build_binary_freshness_blocked_verdict(
        "VP-1", _stale(), result,
    )
    attempt = verdict["evidence"]["rebuild_attempt"]
    assert attempt["commands_run"] == result.commands_run
    assert attempt["commands_run"], (
        f"the repair agent would read this as 'the agent ran no "
        f"commands'; got {attempt}"
    )
    assert "never came back" in attempt["diagnostics"], attempt
