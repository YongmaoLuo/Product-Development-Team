"""End-to-end tests for binary freshness pre-check + auto-rebuild.

Covers:
  * ``detect_project_kind`` for empty / rust / pure-python / mixed /
    JS-only trees.
  * ``check_binary_freshness`` returns FreshnessReport objects
    matching the production contract (``is_stale``,
    ``rebuild_command``, ``to_framework_check_entry``).
  * ``rebuild_binary`` shells out to the inferred command and
    returns True iff the post-rebuild check is PASSED.
  * The ``VerificationAgent._sub_agent_runner`` pre-check BLOCKs the
    VP when binary is stale and PASSes when fresh.
  * The per-round cache records every check and is never read back
    (write-only) — verified by counting ``Path.stat`` syscalls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# detect_project_kind
# ---------------------------------------------------------------------------

def test_detect_project_kind_empty(tmp_path: Path) -> None:
    from binary_freshness import detect_project_kind, KIND_NONE
    assert detect_project_kind(tmp_path) == KIND_NONE


def test_detect_project_kind_pure_python(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("# x\n")
    from binary_freshness import detect_project_kind, KIND_PURE_PYTHON
    assert detect_project_kind(tmp_path) == KIND_PURE_PYTHON


def test_detect_project_kind_rust_python_top_level(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("// stub\n")
    from binary_freshness import detect_project_kind, KIND_RUST_PYTHON
    assert detect_project_kind(tmp_path) == KIND_RUST_PYTHON


def test_detect_project_kind_rust_python_subdir(tmp_path: Path) -> None:
    """The project real case: Cargo.toml sits in <root>/native_ext/."""
    pkg = tmp_path / "native_ext"
    (pkg / "src").mkdir(parents=True)
    (pkg / "Cargo.toml").write_text('[package]\nname = "native_ext"\n')
    (pkg / "src" / "lib.rs").write_text("// stub\n")
    from binary_freshness import detect_project_kind, KIND_RUST_PYTHON
    assert detect_project_kind(tmp_path) == KIND_RUST_PYTHON


def test_detect_project_kind_pyproject_subdir(tmp_path: Path) -> None:
    pkg = tmp_path / "libs" / "core"
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname = 'core'\n")
    from binary_freshness import detect_project_kind, KIND_PURE_PYTHON
    assert detect_project_kind(tmp_path) == KIND_PURE_PYTHON


def test_detect_project_kind_js_only_is_none(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}\n")
    from binary_freshness import detect_project_kind, KIND_NONE
    assert detect_project_kind(tmp_path) == KIND_NONE


def test_detect_project_kind_cargo_without_rs_is_none(tmp_path: Path) -> None:
    """A Cargo.toml with no .rs files is not a Rust/Python project."""
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    from binary_freshness import detect_project_kind, KIND_NONE
    assert detect_project_kind(tmp_path) == KIND_NONE


# ---------------------------------------------------------------------------
# check_binary_freshness
# ---------------------------------------------------------------------------

def _make_rust_pkg(project_dir: Path, *, fresh: bool) -> Path:
    pkg = project_dir / "native_ext"
    (pkg / "src").mkdir(parents=True)
    (pkg / "target" / "release").mkdir(parents=True)
    (pkg / "Cargo.toml").write_text('[package]\nname = "native_ext"\n')
    rs = pkg / "src" / "lib.rs"
    rs.write_text("// stub\n")
    so = pkg / "target" / "release" / "libnative_ext.so"
    so.write_text("// stub .so\n")
    if fresh:
        os.utime(rs, (time.time() - 3600, time.time() - 3600))
        os.utime(so, (time.time(), time.time()))
    else:
        os.utime(rs, (time.time(), time.time()))
        os.utime(so, (time.time() - 3600, time.time() - 3600))
    return pkg


def _stats_and_result(fn: Any) -> Tuple[int, Any]:
    """Run ``fn()``; return ``(Path.stat call count, fn's return value)``.

    ``Path.stat`` is the syscall a freshness check is actually made of,
    so its call count measures how much of the check really ran — which
    is the only honest way to test "did the cache skip the work?".
    """
    real_stat = Path.stat
    count = 0

    def _spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        return real_stat(self, *args, **kwargs)

    Path.stat = _spy  # type: ignore[assignment]
    try:
        result = fn()
    finally:
        Path.stat = real_stat  # type: ignore[assignment]
    return count, result


def test_check_freshness_rust_python_fresh(tmp_path: Path) -> None:
    from binary_freshness import check_binary_freshness
    _make_rust_pkg(tmp_path, fresh=True)
    report = check_binary_freshness(tmp_path)
    assert report.status == "PASSED"
    assert report.kind == "rust_python"
    assert report.is_stale is False
    assert report.binary_path is not None
    assert report.rebuild_command is None  # nothing to rebuild


def test_check_freshness_rust_python_stale(tmp_path: Path) -> None:
    from binary_freshness import check_binary_freshness
    _make_rust_pkg(tmp_path, fresh=False)
    report = check_binary_freshness(tmp_path)
    assert report.status == "FAILED"
    assert report.is_stale is True
    assert report.rebuild_command is not None
    assert (
        "maturin" in report.rebuild_command
        or "cargo build" in report.rebuild_command
    )


def test_check_freshness_pure_python_fresh(tmp_path: Path) -> None:
    from binary_freshness import check_binary_freshness
    (tmp_path / "src").mkdir()
    py = tmp_path / "src" / "main.py"
    py.write_text("# x\n")
    cache = tmp_path / "src" / "__pycache__"
    cache.mkdir()
    pyc = cache / "main.cpython-311.pyc"
    pyc.write_text("# cached\n")
    os.utime(py, (time.time() - 3600, time.time() - 3600))
    os.utime(pyc, (time.time(), time.time()))
    report = check_binary_freshness(tmp_path)
    assert report.status == "PASSED"
    assert report.kind == "pure_python"


def test_check_freshness_pure_python_stale(tmp_path: Path) -> None:
    from binary_freshness import check_binary_freshness
    (tmp_path / "src").mkdir()
    py = tmp_path / "src" / "main.py"
    py.write_text("# x\n")
    cache = tmp_path / "src" / "__pycache__"
    cache.mkdir()
    pyc = cache / "main.cpython-311.pyc"
    pyc.write_text("# cached\n")
    os.utime(py, (time.time(), time.time()))     # py newer
    os.utime(pyc, (time.time() - 3600, time.time() - 3600))
    report = check_binary_freshness(tmp_path)
    assert report.status == "FAILED"
    assert "compileall" in (report.rebuild_command or "")


def test_check_freshness_no_artefacts_is_skipped(tmp_path: Path) -> None:
    from binary_freshness import check_binary_freshness
    (tmp_path / "package.json").write_text("{}\n")
    report = check_binary_freshness(tmp_path)
    assert report.status == "SKIPPED"
    assert report.is_stale is False  # SKIPPED is not stale
    # framework_checks external representation is PASSED
    entry = report.to_framework_check_entry()
    assert entry["status"] == "PASSED"
    assert "no-op" in entry["title"].lower()


def test_check_freshness_cache_is_write_only(tmp_path: Path) -> None:
    """The per-round cache records reports; it never reads them back.

    This pins the *real* behaviour: ``check_binary_freshness`` re-runs
    the entire mtime sweep on every call, even when handed a cache that
    already holds the byte-identical report. ``FreshnessCache.get()``
    was removed on 2026-10-14 because it had no caller and could not
    have saved anything — the key it took,
    ``(kind, binary_path, binary_mtime, source_mtime)``, is itself only
    computable by doing the work it would have skipped.

    Replaces ``test_check_freshness_cache_short_circuits``, which
    promised a short-circuit and a ``Path.stat`` spy that its body
    never contained: it only asserted that ``_store`` was non-empty
    and that two calls to a deterministic function agree.

    If a read path is ever reintroduced this test goes red — but
    *which* assertion catches it depends on where the read lands:

      * read at the top of ``check_binary_freshness``, before any stat
        work → caught by ``second_stats == first_stats``, since a
        short-circuit would leave the second call doing no stats at all;
      * read next to the existing ``put``, after the check has already
        run → caught by ``len(cache._store) == 1``, because the cheap
        key adds a second entry. Note this placement saves nothing:
        the check has already been paid for by then.

    Either way the signal is the same: revisit the corrected
    docstrings in ``binary_freshness``.
    """
    from binary_freshness import FreshnessCache, check_binary_freshness
    _make_rust_pkg(tmp_path, fresh=True)

    cache = FreshnessCache()
    first_stats, first = _stats_and_result(
        lambda: check_binary_freshness(tmp_path, cache=cache))
    second_stats, second = _stats_and_result(
        lambda: check_binary_freshness(tmp_path, cache=cache))

    assert first.status == "PASSED"
    # Both calls agree — because the check is deterministic, NOT because
    # the second one was served from the cache.
    assert second.status == first.status
    assert second.binary_mtime == first.binary_mtime
    assert second.newest_source_mtime == first.newest_source_mtime

    assert first_stats > 0, "sanity: the check does stat the filesystem"
    assert second_stats == first_stats, (
        "the cache stopped short-circuiting (or gained a read path): "
        f"first call made {first_stats} Path.stat calls, second made "
        f"{second_stats}. If this is now intentional, update the "
        "FreshnessCache docstrings, which now describe write-only."
    )

    # The write side is real: exactly one entry (both calls produced the
    # same key), keyed as documented, holding the returned report.
    assert len(cache._store) == 1
    ((key, stored),) = cache._store.items()
    assert key == (first.kind, first.binary_path,
                   first.binary_mtime, first.newest_source_mtime)
    assert stored.status == "PASSED"


# ---------------------------------------------------------------------------
# rebuild_binary
# ---------------------------------------------------------------------------

def test_rebuild_rust_python_succeeds_when_command_is_noop(tmp_path: Path) -> None:
    """Mock subprocess.run so the rebuild command exits 0 — then
    the post-rebuild freshness check (which we expect PASSED after
    the rebuild) drives the return value.

    We simulate "rebuild succeeded and now binary is fresh" by
    making both the .rs and the .so timestamps match before the
    post-check runs. The mock exits 0 for any command, so we can
    use the real binary_freshness.post-check to validate.
    """
    from binary_freshness import rebuild_binary, KIND_RUST_PYTHON
    pkg = _make_rust_pkg(tmp_path, fresh=False)
    # After rebuild, bring binary up to date by setting mtime = now.
    so = pkg / "target" / "release" / "libnative_ext.so"
    rs = pkg / "src" / "lib.rs"
    now = time.time()
    os.utime(rs, (now, now))
    os.utime(so, (now, now))

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        # Pretend cargo build succeeded.
        from subprocess import CompletedProcess
        return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    subprocess.run = fake_run  # type: ignore
    try:
        ok = rebuild_binary(tmp_path, KIND_RUST_PYTHON, timeout=10)
    finally:
        subprocess.run = real_run  # type: ignore
    assert ok is True


def test_rebuild_returns_false_on_subprocess_timeout(tmp_path: Path) -> None:
    """rebuild_binary must swallow subprocess.TimeoutExpired and
    return False (it must never propagate to the pre-check caller)."""
    from binary_freshness import rebuild_binary, KIND_RUST_PYTHON
    _make_rust_pkg(tmp_path, fresh=False)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    real_run = subprocess.run
    subprocess.run = fake_run  # type: ignore
    try:
        ok = rebuild_binary(tmp_path, KIND_RUST_PYTHON, timeout=1)
    finally:
        subprocess.run = real_run  # type: ignore
    assert ok is False


def test_rebuild_unknown_kind_returns_false(tmp_path: Path) -> None:
    from binary_freshness import rebuild_binary
    assert rebuild_binary(tmp_path, "nonexistent_kind") is False


# ---------------------------------------------------------------------------
# VerificationAgent._sub_agent_runner pre-check (integration)
# ---------------------------------------------------------------------------

def _make_agent(plan_dir: Path, project_dir: Path):
    from verification_agent import VerificationAgent
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def test_sub_agent_runner_blocks_vp_when_binary_stale(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: agent._sub_agent_runner must BLOCK the VP when the
    binary is stale.

    2026-09-14 determinism fix: the pre-check's rebuild step is
    ``binary_rebuild_agent.attempt_intelligent_rebuild``, whose fast path
    shells out to the machine's toolchain and whose fallback spawns an
    LLM sub-agent.  On a machine WITH ``cargo`` installed the rebuild can
    actually SUCCEED, in which case the VP is (correctly) not blocked and
    execution falls through to :meth:`_run_single_vp_async`, whose
    ``write_verification_point_log`` raises
    ``RuntimeError: No log file active. Call start_round() first.``
    because this unit test never starts a round — observed as a
    load-dependent full-lane failure (passes in isolation, red in the
    full suite where the sub-agent wins the race).  Pinning the rebuild
    outcome keeps the contract under test (stale binary → BLOCKED verdict
    carrying freshness evidence) independent of the host toolchain.
    """
    _make_rust_pkg(tmp_path, fresh=False)  # stale

    import binary_rebuild_agent
    from binary_rebuild_agent import RebuildResult

    def _rebuild_always_fails(*args, **kwargs):
        return RebuildResult(
            success=False,
            diagnostics="test: rebuild forced to fail (determinism)",
        )

    monkeypatch.setattr(
        binary_rebuild_agent,
        "attempt_intelligent_rebuild",
        _rebuild_always_fails,
    )

    agent = _make_agent(tmp_path, tmp_path)
    executor = agent._build_verification_executor(
        plan_data={
            "verification_points": [
                {"id": "VP-001", "title": "Smoke",
                 "verification_method": "code_review",
                 "test_command": "pytest -q",
                 "expected_result": "exit 0",
                 "priority": 1, "related_prd_criteria": []},
            ],
        },
    )

    # Pull the sub_agent_runner closure out of the executor and
    # invoke it directly with a VP that has ``method`` set.
    sub_runner = executor.sub_agent_runner

    # Real call: pre-check should block the VP before sub_agent_runner
    # runs the LLM.
    import asyncio
    async def _run_vp():
        return await sub_runner({"id": "VP-001", "method": "code_review"})

    verdict = asyncio.run(_run_vp())
    assert verdict["status"] == "BLOCKED", (
        f"stale binary must BLOCK the VP; got: {verdict}"
    )
    # The blocked verdict carries the freshness evidence so the
    # operator knows why the VP was blocked.
    assert "binary_freshness" in verdict["evidence"], (
        f"BLOCKED verdict must carry binary_freshness evidence; "
        f"got evidence={verdict['evidence']!r}"
    )


def test_sub_agent_runner_passes_when_binary_fresh(tmp_path: Path) -> None:
    """End-to-end: agent._sub_agent_runner must NOT block the VP
    when binary is fresh. We monkey-patch the downstream
    ``_run_single_vp_async`` so the test only exercises the
    pre-check branch (the downstream LLM call would otherwise need
    a real coding_tool and round-start boilerplate)."""
    _make_rust_pkg(tmp_path, fresh=True)

    from unittest.mock import patch as _patch, AsyncMock
    from binary_freshness import FreshnessReport, KIND_RUST_PYTHON
    fake_report = FreshnessReport(
        kind=KIND_RUST_PYTHON, status="PASSED",
        detail="fresh (test mock)",
        evidence="mock",
    )

    agent = _make_agent(tmp_path, tmp_path)
    executor = agent._build_verification_executor(
        plan_data={
            "verification_points": [
                {"id": "VP-001", "title": "Smoke",
                 "verification_method": "code_review",
                 "test_command": "pytest -q",
                 "expected_result": "exit 0",
                 "priority": 1, "related_prd_criteria": []},
            ],
        },
    )
    sub_runner = executor.sub_agent_runner

    # Sentinel returned by the patched downstream — its shape
    # matches a healthy FAILED verdict (because we deliberately
    # let the pre-check pass and the LLM downstream error out).
    sentinel = {"status": "FAILED", "reasons": ["sentinel"]}

    async def _run_vp():
        with _patch(
            "binary_freshness.check_binary_freshness",
            return_value=fake_report,
        ), _patch.object(
            agent, "_run_single_vp_async",
            new=AsyncMock(return_value=sentinel),
        ):
            return await sub_runner(
                {"id": "VP-001", "method": "code_review"}
            )

    import asyncio
    verdict = asyncio.run(_run_vp())
    assert verdict["status"] != "BLOCKED", (
        f"fresh binary must NOT block; got BLOCKED verdict: {verdict}"
    )
    assert verdict == sentinel, (
        f"fresh binary should pass-through to downstream; got: {verdict}"
    )


def test_freshness_cache_clears_on_demand(tmp_path: Path) -> None:
    """Sanity: the cache has a clear() method used at round boundaries."""
    from binary_freshness import FreshnessCache, check_binary_freshness
    _make_rust_pkg(tmp_path, fresh=True)
    cache = FreshnessCache()
    check_binary_freshness(tmp_path, cache=cache)
    assert len(cache._store) > 0
    cache.clear()
    assert len(cache._store) == 0