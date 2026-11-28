"""Injection guards on the framework-fix reproduction protocol.

What this file pins
-------------------
The reproduction protocol runs **external commands** and **git**; the
registry writes **SQL** and stores **plan ids**.  Each of those is a
place where a caller-supplied string can stop being data:

1. a repo address carrying shell metacharacters becomes a second command;
2. a file name that starts with ``-`` becomes a git *option*;
3. a fingerprint carrying a quote becomes SQL syntax;
4. a plan id carrying ``../`` escapes the plans root.

Every guard below sits next to the case that shows it is not "reject
everything" -- a legitimate value still has to work, or the guard is
just a wall.  A guard that rejects the whole world passes every
rejection case and means nothing.

The boundary is ``ValueError`` throughout: these are input problems, so
a caller can catch one exception type for all four vectors.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework_fix import repro as repro_module  # noqa: E402
from framework_fix.registry import FrameworkFixRegistry  # noqa: E402
from framework_fix.repro import ReproRequest  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _hit(**overrides: Any) -> Dict[str, Any]:
    """A hit, as the registry's callers describe one."""
    hit: Dict[str, Any] = {
        "plan_id": "plan-a",
        "vp_id": "VP-3",
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
    }
    hit.update(overrides)
    return hit


def _request(**overrides: Any) -> ReproRequest:
    """A minimal, legitimate reproduction request."""
    payload: Dict[str, Any] = {
        "request_id": "r1",
        "repo": "/tmp/origin.git",
        "command": ["git", "status"],
        "framework_files": ["a.py"],
        "error_class": "ValueError",
        "first_error_line": "repo name",
    }
    payload.update(overrides)
    return ReproRequest(**payload)


def _record_git_argv(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Capture every argv the module hands to the process runner.

    The real runner is replaced, not the call site: the argv under test
    is the one production assembles, including the ``--`` separator.
    """
    recorded: List[List[str]] = []

    def fake_run_bounded(command, **kwargs):
        argv = [str(part) for part in command]
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(repro_module, "run_bounded", fake_run_bounded)
    return recorded


def _count(db_path: Path, table: str) -> int:
    """How many rows a table holds -- read through a plain connection."""
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _tables(db_path: Path) -> set:
    """The table names the state database holds."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. shell metacharacters in the repo address
# ---------------------------------------------------------------------------
def test_shell_metachars_rejected():
    """A repo address carrying shell metacharacters is refused.

    The protocol never runs anything through a shell, so ``;`` in the
    repo address cannot execute a second command *today*.  The guard is
    about the property rather than about the accident: a repo address
    that is safe to read off a screen and paste into a terminal is a
    property worth holding, and the string is the one thing in the
    request an operator types by hand.
    """
    with pytest.raises(ValueError) as excinfo:
        ReproRequest(
            "r1",
            "repo; rm -rf /",
            ["git", "checkout", "--evil"],
            ["a.py"],
            "ValueError",
            "repo name",
        )
    assert "repo" in str(excinfo.value).lower(), (
        f"the diagnostic must name the offending field: {excinfo.value!r}"
    )

    # The rejection is about the repo address, not about the command:
    # argv arguments are allowed to carry metacharacters -- that is what
    # argv is for.  The same request with a legitimate address is fine.
    request = _request(
        repo="/tmp/origin.git",
        command=["git", "checkout", "--evil"],
    )
    assert request.repo == "/tmp/origin.git"
    assert request.command == ["git", "checkout", "--evil"]


def test_command_must_be_an_argv_list():
    """A command given as a shell string is refused, not split.

    ``"pytest -k x"`` handed to a runner that expects argv would be
    iterated character by character -- or handed to a shell, where the
    ``;`` starts a second command.  Either way the caller loses control
    of what runs.
    """
    with pytest.raises(ValueError) as excinfo:
        _request(command="pytest -k x")
    assert "argv" in str(excinfo.value).lower(), (
        f"the diagnostic must say what was expected: {excinfo.value!r}"
    )

    # A list of strings is the accepted shape.
    assert _request(command=["pytest", "-k", "x"]).command == [
        "pytest",
        "-k",
        "x",
    ]


# ---------------------------------------------------------------------------
# 2. a file name that starts with '-' is a path, not an option
# ---------------------------------------------------------------------------
def test_git_option_injection(monkeypatch, tmp_path):
    """A file name starting with ``-`` reaches git after a ``--``.

    git reads a leading ``-`` as "this is an option".  A file called
    ``-x`` therefore becomes ``git checkout -x`` -- an option, not a
    file -- and a repository address called ``--upload-pack=...``
    becomes a command for the far side to run.  The separator ``--`` is
    git's own "options end here" marker, and every path argument has to
    sit behind it.

    The two defences are separate on purpose: validation refuses the
    addresses that *look* like options, and the separator keeps every
    path behind the options even for the addresses that do not.

    Why the production half of this case changed shape: the protocol
    used to **clone** the repository into a temporary tree, so the
    repository address was a *path argument* of ``git clone`` and had to
    sit behind a ``--``.  It now builds a **linked worktree** inside the
    failing repository itself (``git worktree add --detach``), so the
    repository address is not a path argument any more -- it is the
    working directory (``-C <repo>``) of the very same argv builder.
    What the separator has to carry now is the *worktree path* and the
    *base ref*.  The property under test is unchanged: no
    caller-supplied string reaches git as an option.
    """
    recorded = _record_git_argv(monkeypatch)

    argv = repro_module.build_git_argv(["checkout", "origin/main"], ["-x"])
    separator = argv.index("--")
    assert argv[separator + 1:] == ["-x"], (
        f"the path must sit behind the separator: {argv!r}"
    )
    assert "-x" not in argv[:separator], (
        f"a path that starts with '-' must not be read as an option: {argv!r}"
    )

    # The production path uses the same builder: the worktree build
    # hands the temporary worktree path and the base ref over behind a
    # separator, so a string that looks like an option stays data.
    coordinator = repro_module.ReproCoordinator(worktree_root=tmp_path / "wt")
    request = _request(repo="/tmp/origin.git")
    worktree = coordinator._create_worktree(request)
    try:
        production = next(
            argv for argv in recorded if "worktree" in argv and "add" in argv
        )
    finally:
        shutil.rmtree(worktree, ignore_errors=True)
    separator = production.index("--")
    assert production[separator + 1:] == [
        str(worktree),
        request.base_ref,
    ], (
        "the worktree path and the base ref must sit behind the "
        f"separator: {production!r}"
    )
    for value in (str(worktree), request.base_ref):
        assert value not in production[:separator], (
            "a caller-supplied string must not be read as an option: "
            f"{production!r}"
        )

    # The repository address is not a path argument any more: it is the
    # working directory of the same command.  So it must not appear
    # behind the separator -- there git would act on it as a path -- and
    # it must not be read as an option either: it is the value that
    # follows ``-C``, and it appears nowhere else.
    assert request.repo not in production[separator + 1:], (
        "the repository address must not be handed to git as a path "
        f"argument: {production!r}"
    )
    assert production[production.index("-C") + 1] == request.repo, (
        "the repository address must be handed to git as the working "
        f"directory: {production!r}"
    )
    assert production.count(request.repo) == 1, (
        "the repository address must appear exactly once -- as the value "
        f"of '-C': {production!r}"
    )

    # And the option-shaped addresses are refused outright: they would
    # ask the far side to run a command.
    with pytest.raises(ValueError):
        _request(repo="--upload-pack=touch /tmp/pwned")
    with pytest.raises(ValueError):
        _request(base_ref="-c core.pager=cat")


def test_command_injection_runs_as_literal_arguments(tmp_path):
    """A command carrying shell metacharacters runs them as arguments.

    The failing command comes from a failure site, so its text may
    carry ``;`` / ``&&`` / ``$(...)`` / a backtick / a newline.  Those
    are *arguments*, not syntax: the protocol never hands a command to
    a shell.  The proof is a side-effect marker file -- if any of those
    strings were executed, the marker would exist.
    """
    marker = tmp_path / "shell-ran"
    received = tmp_path / "received.txt"
    payloads = [
        f"; touch {marker}",
        f"&& touch {marker}",
        f"$(touch {marker})",
        f"`touch {marker}`",
        f"\ntouch {marker}",
    ]
    runner = repro_module.default_runner()
    for payload in payloads:
        attempt = runner(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib, sys; "
                    "pathlib.Path(sys.argv[2]).write_text("
                    "sys.argv[1], encoding='utf-8')"
                ),
                payload,
                str(received),
            ],
            tmp_path,
            30.0,
        )
        assert attempt.exit_code == 0, (
            f"the command must run: {attempt.first_error_line!r}"
        )
        assert received.read_text(encoding="utf-8") == payload, (
            "the argument must arrive verbatim, not split into words"
        )
        assert not marker.exists(), (
            f"the payload was executed as a command, not passed as an "
            f"argument: {payload!r}"
        )


def test_the_package_never_hands_a_command_to_a_shell():
    """``shell=True`` would turn every argument above back into syntax."""
    pattern = re.compile(r"shell\s*=\s*True")
    package = Path(repro_module.__file__).parent
    offenders = [
        path.name
        for path in sorted(package.glob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"the framework-fix package must not run anything through a shell: "
        f"{offenders!r}"
    )


# ---------------------------------------------------------------------------
# 3. a fingerprint carrying a quote is data, not SQL
# ---------------------------------------------------------------------------
def test_sql_injection(state_db):
    """A fingerprint carrying a quote is looked up as data.

    The registry's queries bind their values; a fingerprint that quotes
    itself out of the statement finds nothing, and the tables it tried
    to break are still there afterwards.
    """
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit())

    injection = fingerprint + "'; DROP TABLE framework_fix_hit; --"

    assert registry.lookup(injection) is None
    assert registry.list_hits(injection) == []
    with pytest.raises(KeyError):
        registry.attach(injection, "plan-a", "VP-3", 1, "clean_main_repro")

    # Nothing was dropped, and the row that does exist is still readable:
    # the query found what it should have found.
    assert "framework_fix_hit" in _tables(state_db)
    assert _count(state_db, "framework_fix_hit") == 1
    assert _count(state_db, "framework_fix_attach") == 0
    assert registry.lookup(fingerprint) is not None


# ---------------------------------------------------------------------------
# 4. a plan id carrying '../' is refused
# ---------------------------------------------------------------------------
def test_path_traversal(state_db):
    """A plan id that traverses is refused before anything is written.

    Plan ids are filesystem leaves: ``plans/{plan_id}/...``.  A plan id
    carrying ``../`` would point at the plans root's parent, so it is
    refused at the door -- the reproduction request is the boundary the
    protocol owns, and its plan id is the one that ends up on the
    attach relation.  The canonical validator
    (``framework.ids.validate_plan_id``) is the same one the rest of
    the backend uses for ``plans/{plan_id}``.
    """
    with pytest.raises(ValueError):
        _request(plan_id="../escape")

    # And on the files the failure is about: they are repo-relative.
    with pytest.raises(ValueError):
        _request(framework_files=["../outside.py"])

    # A legitimate plan id is still good -- and it still attaches.
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit())
    attach_id = registry.attach(
        fingerprint, "plan-a", "VP-3", 1, "clean_main_repro"
    )
    assert attach_id
    assert _count(state_db, "framework_fix_attach") == 1


# ---------------------------------------------------------------------------
# 5. bounded input
# ---------------------------------------------------------------------------
def test_overlong_input_rejected():
    """Every free-form field has an upper bound.

    An unbounded field is an unbounded request: the protocol's inputs
    come from a plan, and a plan is not a trusted source of lengths.
    """
    with pytest.raises(ValueError):
        _request(request_id="r" * 5000)
    with pytest.raises(ValueError):
        _request(repo="/tmp/" + "x" * 5000)
    with pytest.raises(ValueError):
        _request(command=["pytest"] + ["x" * 5000])
    with pytest.raises(ValueError):
        _request(framework_files=["a" * 5000 + ".py"])
    with pytest.raises(ValueError):
        _request(first_error_line="x" * 50000)
    with pytest.raises(ValueError):
        _request(plan_id="p" * 200)
