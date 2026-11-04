"""Missing-vs-stale ``.pyc`` classification for the pure-Python check.

Background
----------
``backend/binary_freshness.py::_check_pure_python`` used to lump two
very different situations into one ``stale_entries`` list:

  * a ``.py`` file whose ``.pyc`` sibling does not exist **yet** — this
    is the normal state of a freshly added source file; Python compiles
    it on first import, so the bytecode is not stale, it is merely
    absent;
  * a ``.py`` file whose ``.pyc`` exists but is **older** than the
    source — this is genuinely stale bytecode: the running interpreter
    would execute the old code.

As long as *any* ``.pyc`` existed in the tree, the first case made the
whole round FAILED ("N of M .py files have stale .pyc"), so adding one
new source file blocked every verification point even though nothing
was stale.

The check now keeps the two apart:

  * ``missing_pyc_files`` — sources with no ``.pyc`` yet. Never stale,
    never fails the round.
  * ``stale_pyc_files``  — sources whose ``.pyc`` is older than the
    source. Still fails the round.

``is_stale`` remains a pure function of ``status == "FAILED"``.

The tests below pin that contract, including the two counter-examples
the design decision called out: "no .pyc at all" stays SKIPPED, and a
``.pyc`` that really is older still fails.
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
)


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside ``project_dir`` and fail loudly."""
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _add_source(project_dir: Path, rel: str, *, pyc_state: str) -> Path:
    """Create a source file, optionally with a ``__pycache__`` sibling.

    ``pyc_state``:
      * ``"fresh"`` — a ``.pyc`` newer than the source (nothing wrong);
      * ``"stale"`` — a ``.pyc`` 3600s older than the source (genuinely
        stale bytecode);
      * ``"none"``  — no ``.pyc`` sibling at all (Python will compile
        the source on first import).
    """
    now = time.time()
    py = project_dir / rel
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("# stub\n", encoding="utf-8")
    os.utime(py, (now, now))

    if pyc_state in ("fresh", "stale"):
        cache = py.parent / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        pyc = cache / f"{py.stem}.cpython-311.pyc"
        pyc.write_text("# cached\n", encoding="utf-8")
        age = 60 if pyc_state == "fresh" else -3600
        os.utime(pyc, (now + age, now + age))
    return py


def _commit_all(project_dir: Path) -> None:
    """Stage everything so the git-tracked scan sees the sources."""
    _git(project_dir, "add", "-A")


def test_missing_pyc_does_not_make_the_round_stale(tmp_path):
    """A source with no ``.pyc`` yet must not fail the round.

    Before the fix this file was listed in ``stale_entries`` and the
    whole check came back FAILED, even though Python compiles it on
    first import and nothing was actually stale.
    """
    _git_init(tmp_path)
    _add_source(tmp_path, "src/main.py", pyc_state="fresh")
    _add_source(tmp_path, "src/new.py", pyc_state="none")
    _commit_all(tmp_path)

    report = check_binary_freshness(tmp_path)

    assert report.kind == "pure_python", (
        f"fixture must be classified as pure_python; got {report.kind!r}"
    )
    assert report.status == "PASSED", (
        f"a missing .pyc must not make the round stale; got "
        f"{report.status!r} — {report.detail!r}"
    )
    assert report.is_stale is False, (
        f"is_stale must follow the status; got {report.is_stale!r}"
    )
    assert report.missing_pyc_files == ("src/new.py",), (
        f"the source without a .pyc must be reported as missing; got "
        f"{report.missing_pyc_files!r}"
    )
    assert report.stale_pyc_files == (), (
        f"nothing is stale here; got {report.stale_pyc_files!r}"
    )
    assert "no .pyc yet" in report.detail, (
        f"the detail must explain the missing bytecode; got {report.detail!r}"
    )
    assert "first import" in report.detail, (
        f"the detail must say that Python compiles on first import; got "
        f"{report.detail!r}"
    )

    # The serialised form carries both lists (JSON has no tuples).
    as_dict = report.to_dict()
    assert as_dict["missing_pyc_files"] == ["src/new.py"], (
        f"to_dict must expose missing_pyc_files as a list; got "
        f"{as_dict['missing_pyc_files']!r}"
    )
    assert as_dict["stale_pyc_files"] == [], (
        f"to_dict must expose stale_pyc_files as a list; got "
        f"{as_dict['stale_pyc_files']!r}"
    )


def test_older_pyc_still_fails(tmp_path):
    """A ``.pyc`` that really is older than its source is still stale."""
    _git_init(tmp_path)
    _add_source(tmp_path, "src/main.py", pyc_state="stale")
    _commit_all(tmp_path)

    report = check_binary_freshness(tmp_path)

    assert report.status == "FAILED", (
        f"older bytecode must still fail the round; got {report.status!r} "
        f"— {report.detail!r}"
    )
    assert report.is_stale is True
    assert report.stale_pyc_files == ("src/main.py",), (
        f"the outdated source must be reported as stale; got "
        f"{report.stale_pyc_files!r}"
    )
    assert report.missing_pyc_files == (), (
        f"nothing is missing here; got {report.missing_pyc_files!r}"
    )
    assert report.detail == "1 of 1 .py files have stale .pyc", (
        f"the stale verdict keeps the old wording; got {report.detail!r}"
    )


def test_missing_and_older_pyc_are_reported_separately(tmp_path):
    """Both categories at once: FAILED, and each file in its own list."""
    _git_init(tmp_path)
    _add_source(tmp_path, "src/fresh.py", pyc_state="fresh")
    _add_source(tmp_path, "src/missing.py", pyc_state="none")
    _add_source(tmp_path, "src/stale.py", pyc_state="stale")
    _commit_all(tmp_path)

    report = check_binary_freshness(tmp_path)

    assert report.status == "FAILED", (
        f"one stale .pyc must fail the round; got {report.status!r} — "
        f"{report.detail!r}"
    )
    assert report.missing_pyc_files == ("src/missing.py",), (
        f"the source without a .pyc must be reported as missing; got "
        f"{report.missing_pyc_files!r}"
    )
    assert report.stale_pyc_files == ("src/stale.py",), (
        f"the source with an older .pyc must be reported as stale; got "
        f"{report.stale_pyc_files!r}"
    )
    assert report.detail == "1 of 3 .py files have stale .pyc", (
        f"only the genuinely stale file counts towards the verdict; got "
        f"{report.detail!r}"
    )


def test_no_pyc_at_all_stays_skipped(tmp_path):
    """No bytecode cache at all is still SKIPPED, not FAILED."""
    _git_init(tmp_path)
    _add_source(tmp_path, "src/main.py", pyc_state="none")
    _add_source(tmp_path, "src/other.py", pyc_state="none")
    _commit_all(tmp_path)

    report = check_binary_freshness(tmp_path)

    assert report.status == "SKIPPED", (
        f"a tree without any .pyc must stay SKIPPED; got {report.status!r} "
        f"— {report.detail!r}"
    )
    assert report.is_stale is False