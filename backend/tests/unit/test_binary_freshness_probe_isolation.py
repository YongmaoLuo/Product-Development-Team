"""Probe-isolation regression tests for the pure-Python freshness check.

Background
----------
The verification harness runs throw-away probe fixtures *inside* the
project it is verifying. They land under ``<project>/.pdt/``::

    <project>/.pdt/vp_probe/probe_log.py
    <project>/.pdt/vp_loop_probe/work/normal/project/src/app.py

Those files are harness scratch space, not project source, and they are
not tracked by git. Before the scan surface was narrowed to the git
index they were picked up as ``.py`` sources; and because each probe's
sibling ``__pycache__/*.pyc`` is *older* than the probe source, they
were reported as stale bytecode ("``.pyc`` older by Ns"). Every Phase-1
verification point came back BLOCKED ("stale bytecode") on a perfectly
healthy project.

Two earlier changes already keep the probes out of the verdict:

  * task 1 — the scan surface is the git-tracked ``.py`` files
    (``scan_scope`` / ``scanned_count``);
  * task 2 — "no ``.pyc`` yet" and "``.pyc`` older than the source"
    are two separate categories (``missing_pyc_files`` /
    ``stale_pyc_files``).

Together they cover upstream decision point 4 (probe artefacts must not
pollute the project root). This module only adds the regression cases
that pin the behaviour, so a future change cannot quietly loosen the
check:

  * probes under ``.pdt/`` must not stale the project;
  * running the probes twice must not change the verdict either;
  * a *genuinely* stale tracked source must still be FAILED (negative
    control — the check must not have been made too permissive).

No product code is touched here; these are pure regression tests. Every
case builds its own throw-away git repository, so the tests do not
depend on the repository they run in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from binary_freshness import check_binary_freshness  # noqa: E402
from tests.unit.test_verification_binary_freshness import (  # noqa: E402
    _git_init,
)


#: The real source of the project under test — tracked by git.
_TRACKED_SOURCE = Path("src") / "main.py"

#: The probe fixtures the verification harness writes under ``.pdt/``.
#: They are deliberately *not* covered by a ``.gitignore`` rule here:
#: they are plain untracked files, which is the premise under test.
_PROBE_SOURCES = (
    Path(".pdt") / "vp_probe" / "probe_log.py",
    Path(".pdt") / "vp_loop_probe" / "work" / "normal" / "project" / "src" / "app.py",
)

#: How far the probe bytecode lags behind the probe source. Any positive
#: gap would do; an hour makes the "``.pyc`` older by" wording stable.
_PROBE_LAG_SEC = 3600


def _require_git() -> None:
    """Skip when git cannot be used — the fixtures need a real index."""
    if shutil.which("git") is None:
        pytest.skip(
            "git executable not found on PATH — the probe-isolation tests "
            "need git to build a tracked project"
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
    try:
        _git_init(project_dir)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"could not initialise a git repository: {exc}")


def _write_source_with_pyc(
    project_dir: Path,
    rel: Path,
    *,
    pyc_state: str,
) -> Path:
    """Write a ``.py`` source plus a ``__pycache__`` sibling.

    ``pyc_state``:
      * ``"fresh"`` — the ``.pyc`` is newer than the source;
      * ``"stale"`` — the ``.pyc`` is :data:`_PROBE_LAG_SEC` older than
        the source (genuinely stale bytecode).
    """
    now = time.time()
    py = project_dir / rel
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("# stub\n", encoding="utf-8")

    cache = py.parent / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    pyc = cache / f"{py.stem}.cpython-311.pyc"
    pyc.write_text("# cached\n", encoding="utf-8")

    if pyc_state == "fresh":
        os.utime(py, (now - _PROBE_LAG_SEC, now - _PROBE_LAG_SEC))
        os.utime(pyc, (now, now))
    else:
        os.utime(py, (now, now))
        os.utime(pyc, (now - _PROBE_LAG_SEC, now - _PROBE_LAG_SEC))
    return py


def _setup_tracked_project(project_dir: Path, *, pyc_state: str = "fresh") -> None:
    """A git repo with one tracked source file and its bytecode sibling.

    ``src/main.py`` is staged, so the git-tracked scan finds exactly one
    source file. ``__pycache__/`` is ignored the way a real repository
    ignores bytecode.
    """
    _init_repo(project_dir)
    (project_dir / ".gitignore").write_text(
        "__pycache__/\n", encoding="utf-8"
    )
    _write_source_with_pyc(
        project_dir, _TRACKED_SOURCE, pyc_state=pyc_state
    )
    _git(project_dir, "add", "-A")


def _write_probe_fixtures(project_dir: Path) -> None:
    """Write the verification probe fixtures under ``.pdt/``.

    Each probe source gets a ``__pycache__`` sibling whose ``.pyc`` is
    :data:`_PROBE_LAG_SEC` older than the source — exactly the shape
    that used to be reported as "``.pyc`` older by Ns". Writing them
    *after* the staging step keeps them untracked.
    """
    for rel in _PROBE_SOURCES:
        _write_source_with_pyc(project_dir, rel, pyc_state="stale")


def _probe_fixtures_are_untracked(project_dir: Path) -> bool:
    """True when git does not track any of the probe fixtures."""
    listing = subprocess.run(
        ["git", "-C", str(project_dir), "ls-files", "-z", "--", "*.py"],
        capture_output=True, text=True, check=True,
    )
    tracked = {entry for entry in listing.stdout.split("\0") if entry}
    return not any(
        str(rel).replace("\\", "/") in tracked for rel in _PROBE_SOURCES
    )


def _probe_fixtures_carry_stale_bytecode(project_dir: Path) -> bool:
    """True when every probe source really is newer than its ``.pyc``.

    A guard against a vacuous test: if the fixtures stopped being
    stale, "the probes are not reported as stale" would prove nothing.
    """
    for rel in _PROBE_SOURCES:
        py = project_dir / rel
        pyc = py.parent / "__pycache__" / f"{py.stem}.cpython-311.pyc"
        if not py.is_file() or not pyc.is_file():
            return False
        if pyc.stat().st_mtime >= py.stat().st_mtime:
            return False
    return True


def test_probe_fixtures_under_pdt_do_not_stale_the_project(tmp_path):
    """Probe fixtures under ``.pdt/`` must not make the project stale.

    This is the accident, reproduced: the probes carry stale bytecode
    of their own, and before the scan surface was narrowed they were
    counted as project source and reported as "``.pyc`` older by Ns".
    """
    _setup_tracked_project(tmp_path, pyc_state="fresh")
    _write_probe_fixtures(tmp_path)

    # The premise of the test, spelled out so a vacuous fixture fails
    # loudly instead of passing for the wrong reason.
    assert _probe_fixtures_carry_stale_bytecode(tmp_path), (
        "fixture premise: every probe source must be newer than its .pyc"
    )
    assert _probe_fixtures_are_untracked(tmp_path), (
        "fixture premise: the probe fixtures must not be git-tracked"
    )

    report = check_binary_freshness(tmp_path)

    assert report.kind == "pure_python", (
        f"fixture must be classified as pure_python; got {report.kind!r}"
    )
    assert report.status == "PASSED", (
        f"probe fixtures under .pdt must not make the project stale; got "
        f"{report.status!r} — {report.detail!r}"
    )
    assert report.scan_scope == "git_tracked", (
        f"the scan must stay on the git-tracked sources; got "
        f"{report.scan_scope!r}"
    )
    assert report.scanned_count == 1, (
        f"only the tracked src/main.py may be scanned; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert report.stale_pyc_files == (), (
        f"the untracked probes must not be reported as stale; got "
        f"{report.stale_pyc_files!r}"
    )
    assert report.missing_pyc_files == (), (
        f"the untracked probes must not be reported as missing either; got "
        f"{report.missing_pyc_files!r}"
    )
    assert ".pyc older by" not in (report.evidence or ""), (
        f"the stale-bytecode wording must not appear; got "
        f"{report.evidence!r}"
    )
    assert ".pdt" not in (report.evidence or ""), (
        f"the probe paths must not appear in the evidence; got "
        f"{report.evidence!r}"
    )

    # The serialised form is what the blocked-verdict consumer reads.
    as_dict = report.to_dict()
    assert as_dict["status"] == "PASSED", (
        f"to_dict must carry the PASSED verdict; got {as_dict!r}"
    )
    assert as_dict["scan_scope"] == "git_tracked", (
        f"to_dict must carry the scan scope; got {as_dict!r}"
    )
    assert as_dict["scanned_count"] == 1, (
        f"to_dict must carry the scanned count; got {as_dict!r}"
    )
    assert as_dict["stale_pyc_files"] == [], (
        f"to_dict must not list any stale file; got {as_dict!r}"
    )


def test_checks_stay_passed_after_repeated_probe_runs(tmp_path):
    """Two rounds of probe fixtures in a row must not change the verdict.

    The harness re-runs its probes; each round rewrites the fixtures.
    The check must stay PASSED after every round.
    """
    _setup_tracked_project(tmp_path, pyc_state="fresh")

    for round_no in (1, 2):
        _write_probe_fixtures(tmp_path)

        report = check_binary_freshness(tmp_path)

        assert report.status == "PASSED", (
            f"round {round_no}: probe fixtures must not make the project "
            f"stale; got {report.status!r} — {report.detail!r}"
        )
        assert report.scanned_count == 1, (
            f"round {round_no}: only the tracked src/main.py may be "
            f"scanned; got scanned_count={report.scanned_count!r}"
        )
        assert report.stale_pyc_files == (), (
            f"round {round_no}: the untracked probes must not be reported "
            f"as stale; got {report.stale_pyc_files!r}"
        )


def test_real_stale_tracked_file_still_fails_after_probe_run(tmp_path):
    """Negative control: a genuinely stale tracked source is still FAILED.

    Guards against "fixing" the probe pollution by loosening the check.
    The tracked ``src/main.py`` has a ``.pyc`` that is one hour older
    than the source — the interpreter would run the old code.
    """
    _setup_tracked_project(tmp_path, pyc_state="stale")
    _write_probe_fixtures(tmp_path)

    report = check_binary_freshness(tmp_path)

    assert report.status == "FAILED", (
        f"genuinely stale bytecode must still fail the round; got "
        f"{report.status!r} — {report.detail!r}"
    )
    assert report.is_stale is True, (
        f"is_stale must follow the status; got {report.is_stale!r}"
    )
    assert report.stale_pyc_files == (str(_TRACKED_SOURCE),), (
        f"only the tracked stale source may be listed; got "
        f"{report.stale_pyc_files!r}"
    )
    assert report.scanned_count == 1, (
        f"the tracked src/main.py must be the one scanned; got "
        f"scanned_count={report.scanned_count!r}"
    )
    assert report.detail == "1 of 1 .py files have stale .pyc", (
        f"the stale verdict keeps the old wording; got {report.detail!r}"
    )