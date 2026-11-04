"""Scan-surface tests for the pure-Python binary-freshness check.

Background
----------
``backend/binary_freshness.py::_check_pure_python`` used to collect
its ``.py`` sources with ``rglob('*.py')``, excluding only
``__pycache__``. The verification harness writes throw-away probe
fixtures under ``<project>/.pdt/``; those probes are *not* project
source, so they were picked up as ``.py`` files with no ``.pyc``
sibling and every Phase-1 verification point came back BLOCKED
("stale bytecode") on a perfectly healthy project.

The fix narrows the scan surface to the ``.py`` files git actually
tracks and records what happened in the report:

  * ``scan_scope``    — ``'git_tracked'`` or ``'full_tree_fallback'``
  * ``scan_note``     — *why* we fell back (empty when we did not)
  * ``scanned_count`` — how many ``.py`` files were considered

The tests below pin that contract, and pin that the pre-existing
pure-Python fixtures (``_setup_pure_python``) keep their old
verdicts — the narrowing must not turn "stale" into "fresh".
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from binary_freshness import check_binary_freshness  # noqa: E402
from tests.unit.test_verification_binary_freshness import (  # noqa: E402
    _git_init,
    _setup_pure_python,
)


_PROBE_REL = Path(".pdt") / "probe" / "x.py"


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside ``project_dir`` and fail loudly."""
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _setup_tracked_project(project_dir: Path) -> None:
    """A git repo holding one tracked ``.py`` whose ``.pyc`` is fresh.

    ``src/main.py`` is staged (``git add -A``) so the git-tracked scan
    finds exactly one source file.
    """
    _git_init(project_dir)
    src = project_dir / "src"
    src.mkdir(parents=True, exist_ok=True)
    py = src / "main.py"
    py.write_text("# stub\n", encoding="utf-8")
    cache = src / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    pyc = cache / "main.cpython-311.pyc"
    pyc.write_text("# cached\n", encoding="utf-8")
    # source older than bytecode -> fresh
    os.utime(py, (time.time() - 3600, time.time() - 3600))
    os.utime(pyc, (time.time(), time.time()))
    _git(project_dir, "add", "-A")


def _write_probe(project_dir: Path) -> Path:
    """Write an *untracked* probe fixture under ``.pdt/probe/``."""
    probe = project_dir / _PROBE_REL
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("# probe fixture\n", encoding="utf-8")
    return probe


def test_untracked_probe_py_under_pdt_is_not_scanned(tmp_path):
    """An untracked ``.pdt/probe/x.py`` must not be counted as source.

    Before the fix the probe was picked up by the full-tree rglob, had
    no ``.pyc`` sibling, and turned a healthy project into FAILED.
    """
    _setup_tracked_project(tmp_path)
    probe = _write_probe(tmp_path)

    report = check_binary_freshness(tmp_path)

    assert report.kind == "pure_python", (
        f"fixture must be classified as pure_python; got {report.kind!r}"
    )
    assert report.status == "PASSED", (
        f"untracked .pdt probe must not make the project stale; got "
        f"{report.status!r} — {report.detail!r}"
    )
    assert report.scanned_count == 1, (
        f"only the tracked src/main.py may be scanned; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert report.scan_scope == "git_tracked", (
        f"scan_scope must record the git-tracked scan; got "
        f"{report.scan_scope!r}"
    )
    assert str(_PROBE_REL) not in (report.evidence or ""), (
        f"the probe must not appear in the evidence; got {report.evidence!r}"
    )


def test_git_add_makes_probe_py_counted(tmp_path):
    """Once the probe is ``git add``-ed it *is* tracked, so it counts.

    This is the other half of the contract: the scan surface follows
    git, it does not hard-code an exclusion list.
    """
    _setup_tracked_project(tmp_path)
    _write_probe(tmp_path)
    _git(tmp_path, "add", "-A")

    report = check_binary_freshness(tmp_path)

    assert report.scan_scope == "git_tracked", (
        f"scan_scope must record the git-tracked scan; got "
        f"{report.scan_scope!r}"
    )
    assert report.scanned_count == 2, (
        f"the staged probe must be counted on top of src/main.py; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert str(_PROBE_REL) in (report.evidence or ""), (
        f"the tracked probe must show up in the evidence; got "
        f"{report.evidence!r}"
    )


def test_non_git_project_falls_back_to_full_tree_and_says_so(tmp_path):
    """Outside a git repository we fall back to the full tree — and say why."""
    inside_repo = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    assert inside_repo.returncode != 0, (
        "test premise: tmp_path must NOT be inside a git repository "
        f"(got toplevel {inside_repo.stdout.strip()!r})"
    )

    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    py = src / "main.py"
    py.write_text("# stub\n", encoding="utf-8")
    cache = src / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    pyc = cache / "main.cpython-311.pyc"
    pyc.write_text("# cached\n", encoding="utf-8")
    os.utime(py, (time.time() - 3600, time.time() - 3600))
    os.utime(pyc, (time.time(), time.time()))

    report = check_binary_freshness(tmp_path)

    assert report.scan_scope == "full_tree_fallback", (
        f"a non-git project must fall back to the full tree; got "
        f"{report.scan_scope!r}"
    )
    assert "git" in (report.scan_note or "").lower(), (
        f"the fallback reason must mention git; got {report.scan_note!r}"
    )
    assert report.scanned_count == 1, (
        f"the fallback scan still counts the .py files it looked at; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert report.status == "PASSED", (
        f"the fallback scan must still produce a verdict; got "
        f"{report.status!r} — {report.detail!r}"
    )


def test_zero_tracked_py_files_is_skipped(tmp_path):
    """A repo that tracks no ``.py`` at all keeps the SKIPPED semantics."""
    _git_init(tmp_path)
    probe = tmp_path / ".pdt" / "x.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("# probe fixture\n", encoding="utf-8")

    report = check_binary_freshness(tmp_path)

    assert report.status == "SKIPPED", (
        f"a repo with zero tracked .py files must stay SKIPPED; got "
        f"{report.status!r} — {report.detail!r}"
    )
    assert report.scanned_count == 0, (
        f"nothing was scanned, so scanned_count must be 0; got "
        f"{report.scanned_count!r}"
    )


def test_existing_pure_python_fixture_still_reports_stale(tmp_path):
    """The pre-existing stale fixture must still be FAILED.

    Regression guard: narrowing the scan surface must not silently
    turn "stale bytecode" into "fresh".
    """
    _setup_pure_python(tmp_path, fresh=False)

    report = check_binary_freshness(tmp_path)

    assert report.status == "FAILED", (
        f"stale bytecode must still be FAILED; got {report.status!r} — "
        f"{report.detail!r}"
    )
    assert report.scanned_count == 1, (
        f"the tracked src/main.py must be the one scanned; got "
        f"scanned_count={report.scanned_count!r}"
    )


def test_existing_fresh_fixture_still_reports_passed(tmp_path):
    """The pre-existing fresh fixture must still be PASSED.

    Regression guard for the other direction: adding ``git add -A`` to
    the shared fixture must not change what the check decides.
    """
    _setup_pure_python(tmp_path, fresh=True)

    report = check_binary_freshness(tmp_path)

    assert report.status == "PASSED", (
        f"fresh bytecode must still be PASSED; got {report.status!r} — "
        f"{report.detail!r}"
    )
    assert report.scanned_count == 1, (
        f"the tracked src/main.py must be the one scanned; got "
        f"scanned_count={report.scanned_count!r}"
    )
