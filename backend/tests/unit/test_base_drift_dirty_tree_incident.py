"""The incident of 2026-11-03, in one test.

A repair subtask was killed on a working tree and left its work
uncommitted.  The branch was five commits behind ``origin/main`` at the
time.  The old check read a dirty tree as a reason to skip the merge
altogether, so the branch never caught up with ``main`` — the plan
chased a base it could never reach, and the uncommitted work stayed
uncommitted for good.

This module pins the whole shape down on a **real repository**: a work
tree that is behind its base, carrying the two dirty files the killed
subtask left behind plus one untracked file, and a base that moved on
in a file nobody had touched.

What has to hold once the check has run:

* the base really was merged in — the branch is no longer behind;
* the uncommitted work survived byte for byte;
* the uncommitted work is **still uncommitted** — it did not sneak into
  the merge commit, and it still shows up in ``git status``;
* the merge commit carries the **committed** version of the dirty file,
  not the edit that was lying in the working tree.

The last one is the sharp edge.  A merge that swept the local edit into
the commit would leave a work tree that looks clean and has silently
published work nobody ever reviewed — the opposite of what "the dirty
work was left alone" means.

Everything here runs against a **local bare repository**: no network, no
remote, no real origin.  The identity is passed with ``-c`` and set
locally in the clone, because the suite runs with ``$HOME`` pointed at a
scratch directory where ``user.email`` is not set, and a commit without
an identity fails outright.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402

#: The identity every commit in this file carries.  It is passed with
#: ``-c`` instead of being configured globally, and set locally in the
#: clone as well — the merge inside the check creates a commit, and that
#: commit is made by a plain ``git`` call with no ``-c`` to fall back on.
GIT_IDENTITY = (
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
)

#: What the two files the killed subtask touched look like **committed**.
COMMITTED_SERVICE = b"# committed service manager\n"
COMMITTED_KILL_TEST = b"# committed kill attribution test\n"

#: What the killed subtask left behind in the working tree, uncommitted.
LOCAL_SERVICE_EDIT = b"LOCAL EDIT\n"
LOCAL_KILL_TEST_EDIT = b"LOCAL TEST\n"

#: The scratch file the killed subtask never even got to commit.
UNTRACKED_SCRATCH = b"untracked\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo`` and fail loudly if it errors."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        text=True,
        capture_output=True,
    )


def _git_bytes(repo: Path, *args: str) -> bytes:
    """The raw bytes git prints — the blob itself, not a decoded string."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
    ).stdout


def _incident_shape_repo(tmp_path: Path) -> Path:
    """A work tree in the shape the incident had.

    The initial commit carries the two files the killed repair subtask
    was working on — ``backend/service_manager.py`` and
    ``backend/tests/unit/test_service_kill_attribution.py`` — plus
    ``a.txt`` and ``b.txt``.

    The base then moves on by **five commits**, and none of them touches
    either of the two files the work tree will dirty: one of them moves
    ``a.txt`` on, the other four move ``b.txt`` on.  So the merge has no
    reason to refuse — the uncommitted work is nowhere in its way.

    The base lives in a **local bare repository**, so the fetch inside
    the check really runs, against a path on disk, never the network.
    """
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(source),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(source, "config", "user.email", "t@example.com")
    _git(source, "config", "user.name", "t")
    _git(source, "config", "commit.gpgsign", "false")

    (source / "backend").mkdir()
    (source / "backend/tests/unit").mkdir(parents=True)
    (source / "backend/service_manager.py").write_bytes(COMMITTED_SERVICE)
    (source / "backend/tests/unit/test_service_kill_attribution.py").write_bytes(
        COMMITTED_KILL_TEST
    )
    (source / "a.txt").write_bytes(b"a\nbase\n")
    (source / "b.txt").write_bytes(b"b\nbase\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "initial")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")

    # The base moves on: five commits, none of them in the way of the
    # uncommitted work the work tree will carry.
    (source / "a.txt").write_bytes(b"a\nbase moved\n")
    _git(source, "commit", "-am", "base moved a.txt")
    for step in range(2, 6):
        (source / "b.txt").write_bytes(f"b\nbase moved {step}\n".encode())
        _git(source, "commit", "-am", f"base moved b.txt {step}")
    subprocess.run(
        ["git", "-C", str(source), "push", str(bare), "main:main"],
        check=True,
        text=True,
        capture_output=True,
    )
    # The work tree learns about the five commits — otherwise there would
    # be no drift to measure at all.
    _git(work, "fetch", "origin", "main")
    return work


def test_incident_shape_merges_and_keeps_dirty_work(tmp_path, monkeypatch):
    """Behind the base, dirty, and it still merges — the incident, fixed.

    The branch is five commits behind, the working tree carries the two
    dirty files and one untracked file of a killed repair subtask, and
    the base moved on in a file nobody touched.  The base is merged in;
    the uncommitted work is left exactly as it was, byte for byte and
    still uncommitted; and the merge commit carries the committed
    version of the dirty file, not the local edit.
    """
    # The check reads its configuration from the environment.  A stray
    # ``PDT_BASE_DRIFT_ENABLED=0`` in somebody's shell must not decide
    # the outcome of this test.
    monkeypatch.delenv("PDT_BASE_DRIFT_ENABLED", raising=False)
    monkeypatch.delenv("PDT_BASE_DRIFT_BASE", raising=False)
    monkeypatch.delenv("PDT_BASE_DRIFT_MODE", raising=False)

    work = _incident_shape_repo(tmp_path)

    # The killed repair subtask left its work in the working tree.
    (work / "backend/service_manager.py").write_bytes(LOCAL_SERVICE_EDIT)
    (work / "backend/tests/unit/test_service_kill_attribution.py").write_bytes(
        LOCAL_KILL_TEST_EDIT
    )
    (work / "scratch.txt").write_bytes(UNTRACKED_SCRATCH)

    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    committed_service = _git_bytes(
        work, "show", f"{head_before}:backend/service_manager.py"
    )

    report = base_drift.check_and_merge_base(work)

    status_out = _git(work, "status", "--porcelain").stdout
    context = (
        f"status={report.status} behind={report.behind} "
        f"merge_commit={report.merge_commit} head_before={report.head_before} "
        f"head_after={report.head_after} detail={report.detail!r} "
        f"git_status={status_out!r}"
    )

    # ── the base was merged in ──
    assert report.status == base_drift.STATUS_MERGED, context
    assert report.behind >= 5, context
    assert report.merge_commit != "", context
    assert report.head_before != report.head_after, context
    assert report.head_after == report.merge_commit, context
    # The base's own work landed in the work tree.
    assert (work / "a.txt").read_bytes() == b"a\nbase moved\n", context

    # ── the uncommitted work survived, byte for byte ──
    assert (
        (work / "backend/service_manager.py").read_bytes() == LOCAL_SERVICE_EDIT
    ), context
    assert (
        (work / "backend/tests/unit/test_service_kill_attribution.py").read_bytes()
        == LOCAL_KILL_TEST_EDIT
    ), context
    assert (work / "scratch.txt").read_bytes() == UNTRACKED_SCRATCH, context

    # ── and it is still uncommitted ──
    assert (
        " M backend/service_manager.py" in status_out
        or "M  backend/service_manager.py" in status_out
    ), context
    assert (
        " M backend/tests/unit/test_service_kill_attribution.py" in status_out
        or "M  backend/tests/unit/test_service_kill_attribution.py" in status_out
    ), context
    assert "?? scratch.txt" in status_out, context

    # ── the local edit did not get into the merge commit ──
    merged_service = _git_bytes(
        work, "show", f"{report.merge_commit}:backend/service_manager.py"
    )
    assert merged_service == committed_service, context
    assert merged_service != LOCAL_SERVICE_EDIT, context