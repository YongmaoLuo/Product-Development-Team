"""TDD: the round-level binary-freshness preflight.

Background
----------
Before this, a stale build artefact was only noticed **per VP**.  Every
verification point that touched the stale code judged it stale for
itself, and each one ended BLOCKED on its own.  The round then reported
N independent VP failures about a problem that was never about the
product: the working tree was stale, so the VPs were grading yesterday's
code.  Nothing in the report said so — the round looked busy and the
repair chain went after the deliverable.

What this pins
--------------
``Orchestrator._run_binary_freshness_round_preflight`` is a **round
level** gate, and its verdict is about the environment, not the product:

  1. a fresh tree lets the round go on, and nothing is rebuilt;
  2. a stale tree that a rebuild can fix lets the round go on;
  3. a stale tree that survives the rebuild ends the round as BLOCKED
     with an **empty** ``verification_results`` — a round that measured
     nothing must not look like a round that measured a failure;
  4. the blocked report carries the rebuild attempt's own evidence.

The rebuild itself is still the round's single-flight
(``_BinaryRebuildCoordinator``): this gate adds a **verdict**, not a
second rebuild path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_agent(tmp_path: Path):
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(exist_ok=True)
    project_dir.mkdir(exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _freshness(stale: bool):
    """A real :class:`FreshnessReport`, so the production code under test
    reads the same attributes it reads for real.

    ``kind`` is ``pure_python`` and the report carries the two
    classifications the freshness scan produces — ``missing_pyc_files``
    (absent bytecode, harmless) and ``stale_pyc_files`` (bytecode older
    than its source, the thing that actually blocks).
    """
    from binary_freshness import FreshnessReport, KIND_PURE_PYTHON

    if stale:
        return FreshnessReport(
            kind=KIND_PURE_PYTHON,
            status="FAILED",
            detail="1 of 3 .py files have stale .pyc",
            evidence="stale files: src/a.py (.pyc older by 3600s)",
            rebuild_command="python -m compileall -f .",
            scan_scope="git_tracked",
            scan_note="",
            missing_pyc_files=(),
            stale_pyc_files=("src/a.py",),
        )
    return FreshnessReport(
        kind=KIND_PURE_PYTHON,
        status="PASSED",
        detail="binary fresh",
        evidence="mtime ok",
        rebuild_command="python -m compileall -f .",
        scan_scope="git_tracked",
        scan_note="",
        missing_pyc_files=(),
        stale_pyc_files=(),
    )


class _FreshnessStub:
    """Answers ``check_binary_freshness`` the way the tree really behaves.

    The tree is stale until a rebuild has run, and stale-or-fresh after
    it, depending on whether the rebuild could fix it.  Modelling it
    this way rather than scripting a fixed sequence of answers keeps the
    test honest about **how many** checks the production code makes: the
    gate looks, ``_maybe_rebuild_before_service_start`` looks again
    before it rebuilds (it has to make its own judgement), and the gate
    looks once more afterwards.  A scripted sequence would silently
    encode one particular number of checks.
    """

    def __init__(self, *, stale: bool, stale_after_rebuild: bool = False) -> None:
        self.stale = stale
        self.stale_after_rebuild = stale_after_rebuild
        self.rebuild_ran = False
        self.checks = 0

    def __call__(self, project_dir: Any, **kwargs: Any) -> Any:
        self.checks += 1
        if not self.stale:
            return _freshness(stale=False)
        if not self.rebuild_ran:
            return _freshness(stale=True)
        return _freshness(stale=self.stale_after_rebuild)

    def mark_rebuilt(self) -> None:
        self.rebuild_ran = True


class _RebuildStub:
    """Stands in for ``attempt_intelligent_rebuild``.

    Records the calls so a test can assert that a fresh tree triggers
    **no** rebuild at all — the gate must not burn a build on a tree
    that never needed one — and tells the freshness stub that the tree
    has been rebuilt.
    """

    def __init__(self, success: bool, on_rebuild: Any = None) -> None:
        self.success = success
        self.on_rebuild = on_rebuild
        self.calls: List[str] = []

    def __call__(self, *a: Any, **k: Any) -> Any:
        from binary_rebuild_agent import RebuildResult

        self.calls.append(str(k.get("vp_id", "?")))
        if self.on_rebuild is not None:
            self.on_rebuild()
        return RebuildResult(
            success=self.success,
            diagnostics="test: stubbed rebuild",
            commands_run=["python -m compileall -f ."],
            agent_used=False,
            post_check_passed=self.success,
        )


def _install(monkeypatch, freshness: _FreshnessStub, rebuild: _RebuildStub):
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness", freshness,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", rebuild,
    )
    return freshness, rebuild


def test_fresh_binary_lets_the_round_go_on(tmp_path: Path, monkeypatch) -> None:
    """A fresh tree: no report, and no rebuild either.

    The gate must not turn a healthy tree into a build — the whole point
    of checking first is that the common case costs one stat sweep.
    """
    from verification_agent import BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND

    agent = _make_agent(tmp_path)
    freshness = _FreshnessStub(stale=False)
    rebuild = _RebuildStub(success=True, on_rebuild=freshness.mark_rebuilt)
    _install(monkeypatch, freshness, rebuild)

    report = agent._run_binary_freshness_round_preflight()

    assert report is None, (
        f"a fresh tree must let the round go on; got {report!r}"
    )
    assert rebuild.calls == [], (
        f"a fresh tree must not trigger a rebuild; got {rebuild.calls}"
    )


def test_successful_rebuild_lets_the_round_go_on(
    tmp_path: Path, monkeypatch,
) -> None:
    """Stale, rebuilt, fresh again: the round goes on.

    The rebuild is what the gate is allowed to do; once it worked, the
    round has nothing to report.
    """
    agent = _make_agent(tmp_path)
    freshness = _FreshnessStub(stale=True, stale_after_rebuild=False)
    rebuild = _RebuildStub(success=True, on_rebuild=freshness.mark_rebuilt)
    _install(monkeypatch, freshness, rebuild)

    report = agent._run_binary_freshness_round_preflight()

    assert report is None, (
        f"a rebuild that made the tree fresh must let the round go on; "
        f"got {report!r}"
    )
    assert len(rebuild.calls) == 1, (
        f"the stale tree must have been rebuilt exactly once; got "
        f"{rebuild.calls}"
    )


def test_failed_rebuild_returns_blocked_report(
    tmp_path: Path, monkeypatch,
) -> None:
    """Stale before and after the rebuild: the round is BLOCKED.

    And it is BLOCKED **without** verification results: the round
    dispatched no verification point, so it has no verdict about the
    product to report.  A non-empty ``verification_results`` here would
    send the repair chain after code nobody ever looked at.
    """
    from verification_agent import BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND

    agent = _make_agent(tmp_path)
    freshness = _FreshnessStub(stale=True, stale_after_rebuild=True)
    rebuild = _RebuildStub(success=False, on_rebuild=freshness.mark_rebuilt)
    _install(monkeypatch, freshness, rebuild)

    report = agent._run_binary_freshness_round_preflight()

    assert isinstance(report, dict), (
        f"a stale tree that survives the rebuild must produce a report; "
        f"got {report!r}"
    )
    assert report.get("overall_status") == "BLOCKED", (
        f"the round must be BLOCKED; got {report.get('overall_status')!r}"
    )
    assert report.get("stop_reason") == BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND, (
        f"the stop_reason must be the environment kind; got "
        f"{report.get('stop_reason')!r}"
    )
    assert report.get("verification_results") == [], (
        f"a round that dispatched nothing must carry no verification "
        f"results; got {report.get('verification_results')!r}"
    )
    assert report.get("requirement_deviations") == [], (
        f"a round that dispatched nothing has no requirement deviations; "
        f"got {report.get('requirement_deviations')!r}"
    )


def test_blocked_report_carries_rebuild_evidence(
    tmp_path: Path, monkeypatch,
) -> None:
    """The blocked report says what was tried and what was stale.

    Without the rebuild attempt's own outcome the report would say
    "stale" and leave the reader guessing whether a rebuild was even
    tried.
    """
    agent = _make_agent(tmp_path)
    freshness = _FreshnessStub(stale=True, stale_after_rebuild=True)
    rebuild = _RebuildStub(success=False, on_rebuild=freshness.mark_rebuilt)
    _install(monkeypatch, freshness, rebuild)

    report = agent._run_binary_freshness_round_preflight()

    payload: Dict[str, Any] = report["binary_freshness_preflight_failed"]
    assert payload["rebuild_attempt"]["success"] is False, (
        f"the report must carry the rebuild attempt's own verdict; got "
        f"{payload['rebuild_attempt']!r}"
    )
    assert payload["stale_pyc_files"] == ["src/a.py"], (
        f"the report must name the stale files; got "
        f"{payload['stale_pyc_files']!r}"
    )
    assert payload["scan_scope"] == "git_tracked", (
        f"the report must say which surface was scanned; got "
        f"{payload['scan_scope']!r}"
    )
    assert payload["rebuild_command"] == "python -m compileall -f .", (
        f"the report must name the command that was supposed to fix it; "
        f"got {payload['rebuild_command']!r}"
    )
