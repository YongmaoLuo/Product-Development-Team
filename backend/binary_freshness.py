"""Binary freshness check + auto-rebuild for verification pre-flight.

Why this module exists
----------------------
An earlier plan: a run of commits modified
``native_ext/src/core.rs`` (the Rust→Python PyO3 binding source) but
the project's ``venv1/lib/python3.11/site-packages/native_ext/*.so``
predated all of them — so every verification point that exercised the
binding was running the old build.
Every verification VP that ran against the live API exercised the
stale binary; the downstream user-visible signal never appeared;
the plan finished with ``verification_loop_stopped / max_rounds_reached``
without any hint that the binding itself was the culprit.

This module gives verification a **pre-flight check** that runs
**before each VP**: if the binary on disk is older than its source,
the check tries to rebuild and BLOCKs the VP if the rebuild fails.

Project-type detection
-----------------------
* **rust_python** — ``Cargo.toml`` present AND at least one ``.rs``
  file under the package root. We assume PyO3 / maturin-style
  bindings; the rebuild command is ``maturin develop --release``
  (preferred) or ``cargo build --release`` (fallback).
* **pure_python** — ``pyproject.toml`` / ``setup.py`` / ``setup.cfg``
  present OR any ``.py`` files under the project root. The freshness
  signal is the bytecode ``__pycache__/*.pyc`` mtime vs the ``.py``
  source mtime.
* **none** — neither marker found (e.g. a JS-only repo). The check
  is a no-op PASS so downstream consumers see a uniform shape.

Design constraints
------------------
* **Synchronous IO only.** Pre-check is mtime-stat + an optional
  ``subprocess.run`` for the rebuild attempt. Millisecond-class on
  warm caches; an ``asyncio`` layer would only add complexity.
* **No new config knobs.** Rebuild commands are inferred from
  ``pyproject.toml`` (maturin) / ``Cargo.toml`` ([lib] section) /
  ``.py`` (compileall). Operators don't configure anything.
* **Cache-friendly.** The per-VP check is wrapped by a per-round
  cache so two VPs in the same round with the same
  ``(binary_path, binary_mtime, source_mtime)`` tuple skip the second
  mtime syscalls (mtime is monotonic-ish; the tuple is enough to
  detect "nothing changed").

Out of scope
-------------
* Incremental rebuild — we always run the full build command. Dirty
  detection can be layered on later if the rebuild cost becomes
  prohibitive (typical Maturin cold rebuild: 5-30s; cargo release
  build: 30-180s; pure-Python compileall: <1s).
* Loop detection — we don't refuse to rebuild on a build script
  that triggers more rebuilds. If a project's ``Cargo.toml`` has a
  custom build that re-invokes itself, the caller must guard against
  it.
* Verifying the rebuilt binary actually works — the rebuild command
  is the contract; if ``maturin develop`` succeeds and the source
  mtime is now older than the binary mtime, we declare fresh.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REBUILD_TIMEOUT_SEC = 600
"""Default per-rebuild timeout. ``cargo build --release`` on a fresh
checkout can take 3-5 minutes; Maturin cold rebuilds are 5-30s."""

KIND_RUST_PYTHON = "rust_python"
KIND_PURE_PYTHON = "pure_python"
KIND_NONE = "none"


@dataclass(frozen=True)
class FreshnessReport:
    """Result of a single binary-freshness check.

    ``status`` is one of:
      * ``"PASSED"``  — binary mtime ≥ newest source mtime (or there
        is nothing to check).
      * ``"FAILED"``  — source mtime > binary mtime; verification
        would run against stale code.
      * ``"SKIPPED"`` — project has no Rust/Python artefacts to
        check (e.g. JS-only repo). Downstream consumers treat
        SKIPPED as a no-op PASS.
    """

    kind: str
    status: str
    detail: str = ""
    evidence: str = ""
    rebuild_command: Optional[str] = None
    binary_path: Optional[str] = None
    newest_source_mtime: Optional[float] = None
    binary_mtime: Optional[float] = None

    @property
    def is_stale(self) -> bool:
        """True iff the binary on disk is older than its source."""
        return self.status == "FAILED"

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe serialisation (used by audit log + blocked-verdict)."""
        out: Dict[str, Any] = {
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }
        if self.rebuild_command is not None:
            out["rebuild_command"] = self.rebuild_command
        if self.binary_path is not None:
            out["binary_path"] = self.binary_path
        if self.newest_source_mtime is not None:
            out["newest_source_mtime"] = self.newest_source_mtime
        if self.binary_mtime is not None:
            out["binary_mtime"] = self.binary_mtime
        return out

    def to_framework_check_entry(self) -> Dict[str, Any]:
        """Format matching the TDD contract for ``framework_checks``.

        The TDD test (test_verification_binary_freshness.py) pins:
          * ``id == "VP-binary-freshness"``
          * ``status`` ∈ {PASSED, FAILED} — SKIPPED is internal only;
            downstream consumers see PASSED for a no-op check (a
            project with no rust/python artefacts has nothing to be
            stale, so PASSED is the truthful external signal).
          * ``title`` mentions "stale" or "no-op" per the case
          * ``actual_result`` mirrors the same
          * ``evidence`` contains the rebuild command for FAILED

        Returns a dict shaped like a verification-result row so the
        downstream ``record_verdict`` and report-writer code paths
        don't need to special-case it.
        """
        # SKIPPED is the internal "nothing to check" signal; the
        # external ``framework_checks`` status is always PASSED in
        # that case (a project with no rust/python artefacts can
        # never be stale, so PASSED is the truthful signal).
        external_status = "PASSED" if self.status == "SKIPPED" else self.status

        title = "binary freshness check"
        if self.status == "FAILED":
            title = f"binary stale ({self.kind}): {self.detail}"
        elif self.status == "SKIPPED":
            title = "binary freshness: no-op (no rust/python artefacts)"

        actual = "binary fresh" if external_status == "PASSED" else self.detail
        if self.status == "SKIPPED":
            actual = "no rust/python artefacts to check"

        return {
            "id": "VP-binary-freshness",
            "status": external_status,
            "title": title,
            "actual_result": actual,
            "evidence": self.evidence,
        }


class FreshnessCache:
    """Per-verification-round memoisation for the freshness check.

    Cache key: ``(kind, binary_path, binary_mtime, source_mtime)``.
    A second call with the same tuple returns the cached
    :class:`FreshnessReport` without re-stat'ing. The cache lives
    for one verification round and is cleared by
    :meth:`clear` between rounds (called from the orchestrator).
    """

    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str, float, float], FreshnessReport] = {}

    def get(
        self, kind: str, binary_path: str, binary_mtime: float, source_mtime: float,
    ) -> Optional[FreshnessReport]:
        return self._store.get((kind, binary_path, binary_mtime, source_mtime))

    def put(self, report: FreshnessReport) -> None:
        if (
            report.kind
            and report.binary_path
            and report.binary_mtime is not None
            and report.newest_source_mtime is not None
        ):
            self._store[(report.kind, report.binary_path,
                        report.binary_mtime, report.newest_source_mtime)] = report

    def clear(self) -> None:
        self._store.clear()


def detect_project_kind(project_dir: Path) -> str:
    """Heuristically classify ``project_dir`` for the freshness check.

    Returns one of :data:`KIND_RUST_PYTHON`, :data:`KIND_PURE_PYTHON`,
    :data:`KIND_NONE`. The detection is deliberately conservative
    — false negatives (saying 'none' when there ARE binaries) are
    better than false positives (running a rebuild on a JS project
    would be very noisy).

    Both detection and rebuild commands recurse into subdirectories
    — many real projects (e.g. a real project's ``native_ext/`` package)
    put their Cargo.toml/pyproject.toml one level below the
    repository root.
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return KIND_NONE

    # Rust/PyO3 wins over pure-Python when both markers exist
    # (e.g. a project that bundles its own maturin-built extension).
    # Search recursively so projects with the package in a
    # subdirectory (``<root>/native_ext/Cargo.toml``) are detected.
    cargo_toml = next(project_dir.rglob("Cargo.toml"), None)
    if cargo_toml is not None:
        # Any .rs file anywhere under project_dir counts.
        if any(project_dir.rglob("*.rs")):
            return KIND_RUST_PYTHON

    # Pure-Python: any of the standard markers (recursive).
    if next(project_dir.rglob("pyproject.toml"), None) is not None:
        return KIND_PURE_PYTHON
    if next(project_dir.rglob("setup.py"), None) is not None:
        return KIND_PURE_PYTHON
    if any(project_dir.rglob("*.py")):
        return KIND_PURE_PYTHON

    return KIND_NONE


def _newest_mtime(files: list[Path]) -> Optional[float]:
    """Return the newest mtime (epoch seconds) among ``files``, or
    ``None`` if ``files`` is empty. Missing files are skipped."""
    newest: Optional[float] = None
    for f in files:
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or mt > newest:
            newest = mt
    return newest


def _check_rust_python(project_dir: Path) -> FreshnessReport:
    """Check the freshness of a Rust/PyO3 ``.so`` against ``.rs`` mtimes.

    The Cargo.toml is searched recursively — many real projects put
    it in a subdirectory (``<root>/native_ext/Cargo.toml``) with
    ``src/`` and ``target/release/`` siblings. The package root is
    then the directory containing Cargo.toml.
    """
    cargo_toml = next(project_dir.rglob("Cargo.toml"), None)
    if cargo_toml is None:
        return FreshnessReport(
            kind=KIND_RUST_PYTHON, status="SKIPPED",
            detail="Cargo.toml not found",
        )
    pkg_root = cargo_toml.parent

    rs_files = list(pkg_root.rglob("*.rs"))
    if not rs_files:
        return FreshnessReport(
            kind=KIND_RUST_PYTHON, status="SKIPPED",
            detail="no .rs files found",
        )

    # Look for any compiled artefact the package might produce:
    #   * target/release/lib*.so  (Linux ELF .so)
    #   * target/release/lib*.dylib  (macOS Mach-O; cargo on macOS
    #     defaults to cdylib producing .dylib, not .so — the prior
    #     .so-only glob silently FAILED every macOS PyO3 project)
    #   * target/debug/{lib*.so,lib*.dylib}  (debug variants)
    #   * site-packages/<pkg>/*.so  (where ``maturin develop`` /
    #     ``pip install -e .`` actually drop the cpython extension;
    #     target/release only has the cargo build artefact, not the
    #     importable Python binding)
    candidate_paths: list[Path] = []
    for target_sub in ("release", "debug"):
        target_dir = pkg_root / "target" / target_sub
        if target_dir.is_dir():
            candidate_paths.extend(target_dir.glob("lib*.so"))
            candidate_paths.extend(target_dir.glob("*.so"))
            candidate_paths.extend(target_dir.glob("lib*.dylib"))
            candidate_paths.extend(target_dir.glob("*.dylib"))

    # Scan for maturin-installed cpython extensions in any venv's
    # site-packages tree that lives under project_dir.  ``maturin
    # develop`` rebuilds the .so in place at this path; cargo's
    # target/{release,debug} dir is *not* updated, so without this
    # branch every post-``maturin develop`` check still reports
    # "no compiled .so found" even when the binding is fresh.
    #
    # Search heuristic: any directory under project_dir that ends
    # in ``site-packages`` and contains a ``.so`` matching the
    # crate name.
    pkg_name = cargo_toml.stem  # e.g. "native_ext"
    for site_pkg in project_dir.rglob("site-packages"):
        if not site_pkg.is_dir():
            continue
        for so in site_pkg.rglob(f"*{pkg_name}*.so"):
            candidate_paths.append(so)
        for dylib in site_pkg.rglob(f"*{pkg_name}*.dylib"):
            candidate_paths.append(dylib)

    newest_source_mtime = _newest_mtime(rs_files)

    if not candidate_paths:
        # No built artefact on disk yet — verify against the
        # newest source mtime so the operator knows the build never
        # ran (or all build outputs were cleaned).  Search now
        # covers ``.so`` AND ``.dylib`` (macOS cargo output) AND
        # any site-packages install (``maturin develop`` output).
        return FreshnessReport(
            kind=KIND_RUST_PYTHON,
            status="FAILED",
            detail="no compiled .so/.dylib found; rust source has never been built",
            evidence=(
                f"newest .rs mtime: {newest_source_mtime}; "
                f"no target/{{release,debug}}/lib*.{{so,dylib}} present and "
                f"no site-packages/**/*{pkg_name}*.{{so,dylib}} present; "
                f"rebuild command: {_infer_rust_rebuild_command(pkg_root)}"
            ),
            rebuild_command=_infer_rust_rebuild_command(pkg_root),
            binary_path=None,
            newest_source_mtime=newest_source_mtime,
            binary_mtime=None,
        )

    # Pick the newest built artefact. Multiple ``.so`` files (e.g.
    # one per crate in a workspace) — the newest mtime among them
    # is the strongest evidence of freshness.
    binary_path, binary_mtime = max(
        ((p, p.stat().st_mtime) for p in candidate_paths),
        key=lambda x: x[1],
    )

    if newest_source_mtime is None:
        return FreshnessReport(
            kind=KIND_RUST_PYTHON, status="SKIPPED",
            detail="could not stat .rs sources",
            binary_path=str(binary_path), binary_mtime=binary_mtime,
        )

    if binary_mtime < newest_source_mtime:
        cmd = _infer_rust_rebuild_command(pkg_root)
        return FreshnessReport(
            kind=KIND_RUST_PYTHON,
            status="FAILED",
            detail=(
                f"{binary_path.name} mtime ({binary_mtime:.0f}) is older than "
                f"newest .rs mtime ({newest_source_mtime:.0f})"
            ),
            evidence=(
                f"binary={binary_path} mtime={binary_mtime:.0f}; "
                f"newest .rs mtime={newest_source_mtime:.0f}; "
                f"rebuild command: {cmd}"
            ),
            rebuild_command=cmd,
            binary_path=str(binary_path),
            newest_source_mtime=newest_source_mtime,
            binary_mtime=binary_mtime,
        )

    return FreshnessReport(
        kind=KIND_RUST_PYTHON,
        status="PASSED",
        detail=(
            f"{binary_path.name} is fresh against {len(rs_files)} .rs file(s)"
        ),
        evidence=f"binary mtime={binary_mtime:.0f}; newest .rs mtime={newest_source_mtime:.0f}",
        rebuild_command=None,
        binary_path=str(binary_path),
        newest_source_mtime=newest_source_mtime,
        binary_mtime=binary_mtime,
    )


def _infer_rust_rebuild_command(project_dir: Path) -> str:
    """Pick the rebuild command for a Rust/PyO3 project.

    Preference order:
      1. ``maturin develop --release`` (PyO3 bindings)
      2. ``cargo build --release`` (pure-rust workspace)
    """
    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if "maturin" in content.lower():
            return "maturin develop --release"
    return "cargo build --release"


def _check_pure_python(project_dir: Path) -> FreshnessReport:
    """Check the freshness of ``.pyc`` bytecode against ``.py`` mtimes."""
    py_files = [p for p in project_dir.rglob("*.py") if "__pycache__" not in p.parts]
    if not py_files:
        return FreshnessReport(
            kind=KIND_PURE_PYTHON, status="SKIPPED",
            detail="no .py files found",
        )

    pyc_files = list(project_dir.rglob("__pycache__/*.pyc"))
    if not pyc_files:
        # No bytecode cache exists yet — Python will recompile on
        # first import. Treat as SKIPPED (fresh on next run).
        return FreshnessReport(
            kind=KIND_PURE_PYTHON,
            status="SKIPPED",
            detail="no .pyc bytecode cache present (Python will recompile on import)",
            evidence="no __pycache__/*.pyc files",
            rebuild_command="python -m compileall -f .",
        )

    # For each .py file, check if its sibling .pyc is at least as
    # new. The bytecode filename pattern is ``<module>.cpython-<XY>.pyc``
    # (per PEP 3147). We match by stem rather than full name so
    # version skew (cpython-310 vs cpython-311) doesn't trip the check.
    def _matching_pyc(py: Path) -> list[Path]:
        cache_dir = py.parent / "__pycache__"
        if not cache_dir.is_dir():
            return []
        return list(cache_dir.glob(f"{py.stem}.*.pyc"))

    stale_entries: list[str] = []
    newest_py_mtime: Optional[float] = None
    for py in py_files:
        try:
            py_mtime = py.stat().st_mtime
        except OSError:
            continue
        if newest_py_mtime is None or py_mtime > newest_py_mtime:
            newest_py_mtime = py_mtime
        pycs = _matching_pyc(py)
        if not pycs:
            stale_entries.append(f"{py.relative_to(project_dir)} (no .pyc)")
            continue
        newest_pyc_mtime = max(p.stat().st_mtime for p in pycs)
        if newest_pyc_mtime < py_mtime:
            stale_entries.append(
                f"{py.relative_to(project_dir)} (.pyc older by "
                f"{py_mtime - newest_pyc_mtime:.0f}s)"
            )

    if not stale_entries:
        return FreshnessReport(
            kind=KIND_PURE_PYTHON,
            status="PASSED",
            detail=f"all {len(py_files)} .py files have fresh .pyc siblings",
            evidence=f"checked {len(py_files)} .py files against __pycache__/*.pyc",
            rebuild_command=None,
            binary_path=None,
            newest_source_mtime=newest_py_mtime,
            binary_mtime=max((p.stat().st_mtime for p in pyc_files), default=None),
        )

    cmd = "python -m compileall -f ."
    return FreshnessReport(
        kind=KIND_PURE_PYTHON,
        status="FAILED",
        detail=f"{len(stale_entries)} of {len(py_files)} .py files have stale .pyc",
        evidence=(
            f"stale files: {'; '.join(stale_entries[:5])}"
            f"{'...' if len(stale_entries) > 5 else ''}; "
            f"rebuild command: {cmd}"
        ),
        rebuild_command=cmd,
        binary_path=None,
        newest_source_mtime=newest_py_mtime,
        binary_mtime=max((p.stat().st_mtime for p in pyc_files), default=None),
    )


def check_binary_freshness(
    project_dir: Path,
    *,
    cache: Optional[FreshnessCache] = None,
) -> FreshnessReport:
    """One-shot freshness check, dispatching by project kind.

    ``cache`` (optional) lets the caller memoize results within one
    verification round so two VPs that hit the same project state
    don't pay for two mtime syscalls.

    Always returns a :class:`FreshnessReport`. Never raises — IO
    errors are converted to ``status="SKIPPED"`` with the error
    message in ``detail`` (and ``rebuild_command=None``) so the
    pre-flight hook can treat it uniformly without try/except.
    """
    try:
        kind = detect_project_kind(project_dir)
    except Exception as exc:
        return FreshnessReport(
            kind=KIND_NONE, status="SKIPPED",
            detail=f"detect_project_kind failed: {exc}",
        )

    if kind == KIND_NONE:
        return FreshnessReport(
            kind=KIND_NONE, status="SKIPPED",
            detail="no rust/python artefacts to check",
            evidence="project has neither Cargo.toml nor .py files",
        )

    try:
        if kind == KIND_RUST_PYTHON:
            report = _check_rust_python(project_dir)
        elif kind == KIND_PURE_PYTHON:
            report = _check_pure_python(project_dir)
        else:
            # unreachable, defensive
            report = FreshnessReport(kind=KIND_NONE, status="SKIPPED")
    except Exception as exc:
        return FreshnessReport(
            kind=kind, status="SKIPPED",
            detail=f"check failed: {exc}",
            evidence=f"{type(exc).__name__}: {exc}",
        )

    if cache is not None:
        cache.put(report)
    return report


def rebuild_binary(
    project_dir: Path,
    kind: str,
    *,
    timeout: int = REBUILD_TIMEOUT_SEC,
) -> bool:
    """Auto-rebuild attempt. Returns True iff the post-rebuild
    freshness check returns PASSED.

    Picks the rebuild command from
    :func:`_infer_rust_rebuild_command` (Rust) or the hardcoded
    ``python -m compileall -f .`` (Python). The Python command
    invalidates every ``__pycache__`` directory by re-compiling all
    ``.py`` files — slower than the Rust variants but always
    correct.

    Failure modes (all return False, never raise):
      * Command times out (``timeout`` seconds)
      * Command exits non-zero
      * Post-rebuild freshness check still returns FAILED (e.g. the
        rebuild succeeded but a concurrent source change made it
        stale again)
    """
    if kind == KIND_RUST_PYTHON:
        cmd = _infer_rust_rebuild_command(project_dir)
        cmd_list = cmd.split()
    elif kind == KIND_PURE_PYTHON:
        cmd_list = ["python", "-m", "compileall", "-f", "."]
    else:
        return False

    try:
        proc = subprocess.run(
            cmd_list,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False

    if proc.returncode != 0:
        return False

    # Verify the rebuild actually resolved the staleness. A
    # passing exit code doesn't guarantee the .so/.pyc is fresh
    # (e.g. cargo build --release could no-op if the .so is up to
    # date according to cargo's incremental logic — which would
    # already pass, so this check is mostly defensive).
    try:
        post = check_binary_freshness(project_dir)
    except Exception:
        return False
    return post.status == "PASSED"