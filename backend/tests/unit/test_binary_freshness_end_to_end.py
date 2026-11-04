"""End-to-end replay of the 20261031 binary-freshness accident.

Background
----------
On 2026-10-31 a perfectly healthy project came back BLOCKED on every
Phase-1 verification point.  The reason was never the deliverable: the
verification harness writes throw-away probe fixtures **inside** the
project it verifies (``<project>/.pdt/vp_probe/probe_log.py`` and
friends), each of them with a ``__pycache__`` sibling whose ``.pyc`` is
older than the probe source it was compiled from.  The pure-Python
freshness check scanned the whole tree, counted the probes as project
source, and reported them as stale bytecode — so every round of a healthy
plan died on "``.pyc`` older by 3600s" against a file nobody had ever
looked at.

Four fixes landed since, one per upstream decision point:

  * **arch 1** — the scan surface is the git-tracked ``.py`` files
    (``scan_scope`` / ``scanned_count``); untracked probe artefacts are
    not project source.
  * **arch 2** — "no ``.pyc`` yet" and "``.pyc`` older than its source"
    are two different things (``missing_pyc_files`` /
    ``stale_pyc_files``); only the second one fails a round.
  * **arch 3** — a round the freshness gate blocks ends with an
    **environment** stop reason and an empty ``verification_results``:
    a round that dispatched nothing measured nothing, and a FAILED row
    would send the repair chain after a deliverable nobody looked at.
  * **arch 4** — the probe artefacts never pollute the project root.

This module is the acceptance suite for those four fixes **together**.
Each of the earlier tasks proved its own unit in isolation; this one
replays the accident against a real throw-away git repository and
asserts that the four fixes, wired the way the product wires them,
actually stop it.

TDD contract — 4 test cases
---------------------------
1. ``test_probe_noise_does_not_block_the_round`` — the accident itself:
   probe artefacts with stale bytecode of their own, and the round still
   goes on (``PASSED``, ``scan_scope == "git_tracked"``,
   ``scanned_count == 1``).
2. ``test_git_added_probe_fixture_counts_and_blocks`` — the narrowing is
   not "filter everything": once the probe file is genuinely
   ``git add``-ed it *is* project source, so it is counted
   (``scanned_count == 2``) and its stale bytecode *does* fail the round.
3. ``test_stale_binary_blocks_the_round_with_environment_stop_reason`` —
   the negative control: age the **tracked** source's ``.pyc`` by an
   hour and the round must be BLOCKED again, with the environment stop
   reason and no verification results.
4. ``test_blocked_round_keeps_its_round_number`` — a blocked round is
   not charged to the plan's round budget: the row keeps its round and a
   restart resumes that same round.

Boundary conditions
-------------------
* Every stage asserts on its own; the first failure fails the case.
  Nothing is swallowed — a blocked round must not be laundered into a
  "pass".
* Everything happens under ``tmp_path``: a throw-away git repository
  and a throw-away plan directory.  The repository the suite runs in is
  never touched.
* When git cannot be used the cases ``pytest.skip`` and say why —
  they never silently fall back to a full-tree scan, because that would
  test a different code path than the one under test.
* The rebuild attempt is stubbed to fail.  It is the one collaborator
  of the gate that must not run for real: its slow path spawns an LLM
  sub-agent, and its fast path would *repair* the tree and hide the
  thing under test.  A rebuild that fails is the ordinary case the gate
  exists for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ``backend/`` on sys.path so ``import binary_freshness`` works no matter
# which entry point runs the suite, and the repository root so the
# ``backend.framework`` package resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import server  # noqa: E402,F401  — seeds sys.modules["server"] for the loop
import verification_loop as vl  # noqa: E402
from binary_freshness import check_binary_freshness  # noqa: E402
from routes.verification import _resolve_start_round  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)


#: The tracked project source — the only source git knows about.
TRACKED_SOURCE = Path("src") / "app.py"

#: The probe fixture the verification harness writes under ``.pdt/``.
PROBE_SOURCE = Path(".pdt") / "vp_probe" / "probe_log.py"

#: How far the bytecode lags behind its source.  An hour is the shape
#: the accident had ("``.pyc`` older by 3600s").
LAG_SEC = 3600

#: The plan ids used by the cases below.  Distinct per case, so one
#: case cannot see another case's row.
ROUND_PLAN_ID = "20261101-binary-freshness-e2e-round"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _require_git() -> None:
    """Skip when git cannot be used — the fixtures need a real index."""
    if shutil.which("git") is None:
        pytest.skip(
            "git executable not found on PATH — the end-to-end freshness "
            "tests need git to build a tracked project"
        )
    probe = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        pytest.skip(
            "git is on PATH but not runnable "
            f"(exit {probe.returncode}): {(probe.stderr or '').strip()}"
        )


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside ``project_dir`` and fail loudly."""
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(project_dir: Path) -> None:
    """Initialise a throw-away git repository, skipping if that fails."""
    _require_git()
    project_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "init", "--initial-branch", "main"],
            cwd=str(project_dir), capture_output=True, text=True, check=True,
        )
        if not (project_dir / ".git").exists():
            raise subprocess.CalledProcessError(1, "git init")
    except (OSError, subprocess.CalledProcessError):
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir), capture_output=True, text=True, check=True,
        )
    try:
        _git(project_dir, "config", "user.email", "test@example.com")
        _git(project_dir, "config", "user.name", "Test User")
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"could not configure the throw-away git repository: {exc}")


def _write_source_with_pyc(
    project_dir: Path,
    rel: Path,
    *,
    pyc_state: str,
) -> Path:
    """Write a ``.py`` source plus a ``__pycache__`` sibling.

    ``pyc_state``:
      * ``"fresh"`` — the ``.pyc`` is newer than the source;
      * ``"stale"`` — the ``.pyc`` is :data:`LAG_SEC` older than the
        source (genuinely stale bytecode: the interpreter would run the
        previous code).
    """
    now = time.time()
    py = project_dir / rel
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("def run(): return 1\n", encoding="utf-8")

    cache = py.parent / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    pyc = cache / f"{py.stem}.cpython-311.pyc"
    pyc.write_text("# cache\n", encoding="utf-8")

    if pyc_state == "fresh":
        os.utime(py, (now - LAG_SEC, now - LAG_SEC))
        os.utime(pyc, (now, now))
    else:
        os.utime(py, (now, now))
        os.utime(pyc, (now - LAG_SEC, now - LAG_SEC))
    return py


def _build_project(project_dir: Path) -> None:
    """Build the accident's project: one tracked source, one probe.

    The layout is the one the harness produces at the moment of the
    accident:

      * ``src/app.py`` — the tracked project source, with FRESH bytecode;
      * ``.pdt/vp_probe/probe_log.py`` — a harness probe artefact with
        STALE bytecode of its own.

    ``.gitignore`` keeps ``__pycache__/`` out of the index (bytecode is
    never tracked) and ``.pdt/`` out of it as well (the probe is harness
    scratch space, not project source).  ``git add -A`` therefore stages
    the tracked source only — which is exactly the state the accident
    was in.
    """
    _init_repo(project_dir)
    (project_dir / ".gitignore").write_text(
        "__pycache__/\n.pdt/\n", encoding="utf-8",
    )
    _write_source_with_pyc(
        project_dir, TRACKED_SOURCE, pyc_state="fresh",
    )
    _write_source_with_pyc(
        project_dir, PROBE_SOURCE, pyc_state="stale",
    )
    _git(project_dir, "add", "-A")


def _tracked_py_files(project_dir: Path) -> list[str]:
    """The ``.py`` files git tracks in ``project_dir``."""
    listing = subprocess.run(
        ["git", "-C", str(project_dir), "ls-files", "-z", "--", "*.py"],
        capture_output=True, text=True, check=True,
    )
    return [entry for entry in listing.stdout.split("\0") if entry]


def _age_tracked_bytecode(project_dir: Path) -> None:
    """Make the tracked source's ``.pyc`` an hour older than its source.

    This is the negative control: the probe narrowing must not have
    loosened the check for the files that ARE project source.
    """
    py = project_dir / TRACKED_SOURCE
    pyc = py.parent / "__pycache__" / f"{py.stem}.cpython-311.pyc"
    now = time.time()
    os.utime(py, (now, now))
    os.utime(pyc, (now - LAG_SEC, now - LAG_SEC))


def _stub_failing_rebuild(monkeypatch) -> None:
    """Make the rebuild attempt fail without touching the tree.

    The rebuild is the one collaborator of the round gate that must not
    run for real: its slow path spawns an LLM sub-agent, and its fast
    path would *repair* the tree — which would hide the very thing
    under test.  A rebuild that fails is the ordinary case the gate
    exists for: the environment could not be repaired, so the round ends
    BLOCKED rather than pretending the deliverable is at fault.
    """
    from binary_rebuild_agent import RebuildResult

    def _failed_rebuild(*args, **kwargs):
        return RebuildResult(
            success=False,
            diagnostics="test: the rebuild could not repair the tree",
            commands_run=[],
            agent_used=False,
            post_check_passed=False,
        )

    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", _failed_rebuild,
    )


def _make_agent(project_dir: Path):
    """An :class:`Orchestrator` looking at ``project_dir``.

    The plan directory sits NEXT TO the project, not inside it: a plan
    directory inside the project would put the orchestrator's own files
    inside the tree the freshness check scans.
    """
    from verification_agent import Orchestrator

    plan_dir = project_dir.parent / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _state_db():
    """Open the suite's hermetic state DB with the schema applied.

    A FRESH connection per use: the terminal write under test opens its
    own connection, and a connection that still holds a read snapshot
    would read the row as it was before that write.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate as migrate_db

    conn = open_db(str(server._state_db_path()))
    migrate_db(conn)
    return conn


def _read_row(plan_id: str) -> dict:
    """The plan's ``plan_verification`` row, read on a fresh connection."""
    conn = _state_db()
    try:
        return VerificationRepository(conn).summary(plan_id) or {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The accident itself: probe noise must not block the round
# ---------------------------------------------------------------------------


def test_probe_noise_does_not_block_the_round(tmp_path: Path) -> None:
    """Probe artefacts with stale bytecode must not block the round.

    This is the 20261031 accident, replayed: the harness has written its
    probe fixture into the project, the probe's own ``__pycache__`` is an
    hour older than the probe source, and the project itself is
    perfectly healthy.  The round must go on.

    The verdict is the one the blocked-report consumer reads, so it is
    asserted on the serialised form as well as on the report object.
    """
    project_dir = tmp_path / "project"
    _build_project(project_dir)

    # --- the premise, spelled out so a vacuous fixture fails loudly ---
    assert _tracked_py_files(project_dir) == [str(TRACKED_SOURCE)], (
        "fixture premise: only the tracked source may be in git's index; "
        f"got {_tracked_py_files(project_dir)!r}"
    )
    probe_pyc = (
        project_dir / PROBE_SOURCE.parent / "__pycache__"
        / f"{PROBE_SOURCE.stem}.cpython-311.pyc"
    )
    assert probe_pyc.stat().st_mtime < (
        project_dir / PROBE_SOURCE
    ).stat().st_mtime, (
        "fixture premise: the probe's bytecode must be older than the "
        "probe source — that is what used to block the round"
    )

    # --- the accident's verdict ---
    report = check_binary_freshness(project_dir)

    assert report.kind == "pure_python", report.to_dict()
    assert report.status == "PASSED", (
        f"probe noise must not block the round; got {report.status!r} — "
        f"{report.detail!r}"
    )
    assert report.scan_scope == "git_tracked", (
        f"the scan must stay on the git-tracked sources; got "
        f"{report.scan_scope!r}"
    )
    assert report.scanned_count == 1, (
        f"only the tracked src/app.py may be scanned; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert report.stale_pyc_files == (), (
        f"the untracked probe must not be reported as stale; got "
        f"{report.stale_pyc_files!r}"
    )
    assert report.missing_pyc_files == (), (
        f"the untracked probe must not be reported as missing either; got "
        f"{report.missing_pyc_files!r}"
    )

    as_dict = report.to_dict()
    assert {
        "kind": as_dict["kind"],
        "status": as_dict["status"],
        "scan_scope": as_dict["scan_scope"],
        "scanned_count": as_dict["scanned_count"],
    } == {
        "kind": "pure_python",
        "status": "PASSED",
        "scan_scope": "git_tracked",
        "scanned_count": 1,
    }, as_dict


# ---------------------------------------------------------------------------
# 2. The narrowing is not "filter everything"
# ---------------------------------------------------------------------------


def test_git_added_probe_fixture_counts_and_blocks(tmp_path: Path) -> None:
    """A probe that is genuinely ``git add``-ed becomes project source.

    The scan surface was narrowed to git's index.  That narrowing must
    not become a blanket filter: once the probe file itself is in the
    index it IS a tracked source, so it is counted — and its stale
    bytecode really does fail the round.

    The ``git add`` below adds **the probe file itself**.  Adding some
    other new file would bump ``scanned_count`` for the wrong reason
    and would leave the probe untracked; the assertions below check the
    index directly so that mistake cannot pass.
    """
    project_dir = tmp_path / "project"
    _build_project(project_dir)

    # --- the probe file itself enters the index ---
    _git(project_dir, "add", "-f", str(PROBE_SOURCE))

    tracked = _tracked_py_files(project_dir)
    assert str(PROBE_SOURCE) in tracked, (
        f"the probe file itself must be in git's index; got {tracked!r}"
    )
    assert len(tracked) == 2, (
        f"the index must hold exactly the tracked source and the probe; "
        f"got {tracked!r}"
    )

    # --- and it now counts, and its stale bytecode blocks the round ---
    report = check_binary_freshness(project_dir)

    assert report.scan_scope == "git_tracked", report.to_dict()
    assert report.scanned_count == 2, (
        f"the tracked probe must be counted; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert report.status == "FAILED", (
        f"a tracked source with stale bytecode must fail the round; got "
        f"{report.status!r} — {report.detail!r}"
    )
    assert report.stale_pyc_files == (str(PROBE_SOURCE),), (
        f"the tracked probe must be the one reported as stale; got "
        f"{report.stale_pyc_files!r}"
    )
    assert report.missing_pyc_files == (), (
        f"a source with bytecode is not a missing one; got "
        f"{report.missing_pyc_files!r}"
    )


# ---------------------------------------------------------------------------
# 3. Negative control: a stale TRACKED source still blocks the round
# ---------------------------------------------------------------------------


def test_stale_binary_blocks_the_round_with_environment_stop_reason(
    tmp_path: Path, monkeypatch,
) -> None:
    """Ageing the tracked source's bytecode must BLOCK the round again.

    The negative control for the whole change set: the probe narrowing
    must not have loosened the check for the files that ARE project
    source.  A tracked source whose ``.pyc`` is an hour older than the
    source is exactly what the interpreter would refuse to run — so the
    round gate must end the round.

    And the round must end it as an **environment** failure:
      * ``stop_reason`` is ``binary_freshness_preflight_failed`` — the
        environment class, not a product verdict;
      * ``verification_results`` is **empty** — the round dispatched
        nothing, so it measured nothing.  A FAILED row here would send
        the repair chain after a deliverable nobody ever looked at.
    """
    from verification_agent import BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND

    project_dir = tmp_path / "project"
    _build_project(project_dir)

    # The healthy tree goes on — this is the accident's premise.
    assert check_binary_freshness(project_dir).status == "PASSED", (
        "fixture premise: the healthy project must pass the check"
    )

    # --- negative control: age the TRACKED source's bytecode ---
    _age_tracked_bytecode(project_dir)
    assert check_binary_freshness(project_dir).status == "FAILED", (
        "fixture premise: the tracked source must now be stale"
    )

    _stub_failing_rebuild(monkeypatch)
    agent = _make_agent(project_dir)

    report = agent._run_binary_freshness_round_preflight()

    assert isinstance(report, dict), (
        f"a stale tracked source must block the round; got {report!r}"
    )
    assert report.get("overall_status") == "BLOCKED", report
    assert report.get("stop_reason") == BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND, (
        f"the round must stop with the environment reason; got "
        f"{report.get('stop_reason')!r}"
    )
    assert report.get("stop_reason") == "binary_freshness_preflight_failed", (
        f"the environment reason must keep its wire spelling; got "
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

    payload = report["binary_freshness_preflight_failed"]
    assert payload["kind"] == "pure_python", (
        f"the report must name the artefact kind; got {payload['kind']!r}"
    )
    assert payload["stale_pyc_files"] == [str(TRACKED_SOURCE)], (
        f"the report must name the stale tracked file; got "
        f"{payload['stale_pyc_files']!r}"
    )
    assert payload["scan_scope"] == "git_tracked", (
        f"the report must say which surface was scanned; got "
        f"{payload['scan_scope']!r}"
    )
    assert payload["rebuild_attempt"]["success"] is False, (
        f"the gate must have tried to repair the tree and recorded the "
        f"attempt; got {payload['rebuild_attempt']!r}"
    )


# ---------------------------------------------------------------------------
# 4. A blocked round is not charged to the plan's round budget
# ---------------------------------------------------------------------------


def test_blocked_round_keeps_its_round_number(
    tmp_path: Path, monkeypatch,
) -> None:
    """The blocked round keeps its number, and a restart resumes it.

    The round was blocked before it dispatched anything, so it measured
    nothing about the product — and a round that measured nothing must
    not be counted as a round that ran.  The counter therefore stays
    where it was: the plan still has its next round ahead of it, and the
    blocked round is the one a restart resumes.

    If the restart started round 3 instead, the blocked round's verdicts,
    report and repair tasks would be filed under a round that never
    happened.
    """
    from verification_agent import BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND

    project_dir = tmp_path / "project"
    _build_project(project_dir)
    _age_tracked_bytecode(project_dir)
    _stub_failing_rebuild(monkeypatch)
    agent = _make_agent(project_dir)

    report = agent._run_binary_freshness_round_preflight()
    assert isinstance(report, dict), (
        f"the stale tree must block the round; got {report!r}"
    )
    stop_reason = report["stop_reason"]
    assert stop_reason == BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND, report

    # --- the plan was in round 2 of 3 when the round was blocked ---
    conn = _state_db()
    try:
        VerificationRepository(conn).init_round(ROUND_PLAN_ID, 2, 3)
    finally:
        conn.close()

    vl._persist_verification_terminal(
        ROUND_PLAN_ID,
        "loop_stopped",
        stop_reason,
    )

    row = _read_row(ROUND_PLAN_ID)

    assert row.get("round") == 2, (
        f"the blocked round was charged to the budget: {row!r}"
    )
    assert row.get("verification_status") == "loop_stopped", row
    assert row.get("verification_stop_reason") == (
        BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND
    ), row
    assert _resolve_start_round(row, None) == (2, True), (
        f"a restart must resume the blocked round, not start the next "
        f"one; got {_resolve_start_round(row, None)!r}"
    )
