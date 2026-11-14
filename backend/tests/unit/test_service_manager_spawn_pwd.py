"""The service child's ``PWD`` is the directory the child is started in.

What is under test
------------------
The environment a declared service is started with -- and specifically
one key in it: ``PWD``.

Why the key has to be pinned
----------------------------
A declared service is started by ``start_service()`` as a **child
process** whose working directory is the service's declared ``cwd``
(``<project>/<cwd>``).  The child inherits the parent's environment
unless the parent says otherwise -- and the parent here is the backend,
whose own ``PWD`` is whatever the process that started *it* had.  That
value is not the child's directory: the backend is sometimes started
with a **relative** ``PWD`` (``PWD=.``), and a relative ``PWD`` is
inherited verbatim -- ``bash`` does not repair it.

A service's ``start_cmd`` routinely spells its paths in terms of
``$PWD`` -- "the executor lives at ``$PWD/backend/tests/...``", "the
state database is ``$PWD/tmp/.../state.db``".  Those paths are then
resolved **against the child's working directory**, which is the
service's ``cwd`` and not the project root.  So a relative ``PWD``
turns a path that names a file in the project into a path that names a
file *inside the backend directory*: the executor at
``<project>/backend/tests/e2e/fixtures/deterministic_executor.py``
becomes ``<project>/backend/./backend/tests/e2e/fixtures/...``, the
file is not there, and every plan the service was started for dies
before it runs.

The fix belongs at the child boundary: the wrapper the service is
started through (:data:`service_manager._DETACHED_WRAPPER_SOURCE`)
names the child's own directory in ``PWD`` before it execs the real
command.  The wrapper is the last place that knows the child's
directory for sure, and it is shared by production and by the manual
spawn harness -- so both sides of a measurement see the same child.
The same rule is already followed elsewhere in this tree:
``backend/bounded_subprocess.py`` stamps ``child["PWD"] =
os.path.abspath(cwd)`` and ``backend/verification_evidence.py`` runs
its commands with ``env=dict(os.environ, PWD=str(project_dir))``.

The rule these pins hold
------------------------
The child is told, in its own environment, that its ``PWD`` is the
absolute path of the directory it is running in -- whatever the parent
held.

Most of these pins start a **real** child and ask the child itself
what ``$PWD`` expands to.  A pin on the environment this process hands
over would describe the intent; these describe what the child does
with it, which is the thing that broke.

Run them with:

    cd backend && .venv/bin/python3 -m pytest \
        tests/unit/test_service_manager_spawn_pwd.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


def _decl(**overrides) -> ServiceDeclaration:
    base = dict(
        name="chart",
        port=3000,
        start_cmd="npx next dev -p 3000",
        cwd=".",
    )
    base.update(overrides)
    return ServiceDeclaration(**base)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _child_that_reports_its_pwd(
    project: Path, monkeypatch, parent_pwd: str
) -> str:
    """Start a real service child and return what ``$PWD`` meant to it.

    The child's command writes out the value ``$PWD`` expands to, in
    the directory the child runs in.  The parent is made to carry
    *parent_pwd*, which is what the child's environment would have
    inherited had the wrapper not corrected it.
    """
    monkeypatch.setenv("PWD", parent_pwd)
    workdir = project / "svc"
    workdir.mkdir(exist_ok=True)
    decl = _decl(cwd="svc", start_cmd="printf '%s' \"$PWD\" > pwd.txt")
    # The readiness probe is not what this is about, and waiting for a
    # port nobody serves would spend the whole timeout on every test.
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)

    runtime = sm.start_service(decl, project)

    reported = workdir / "pwd.txt"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not reported.exists():
        time.sleep(0.05)
    assert reported.exists(), (
        "the child never wrote its PWD out: {0}".format(runtime.detail)
    )
    return reported.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# What the child itself sees
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_the_child_expands_pwd_to_the_directory_it_runs_in(project, monkeypatch):
    """The child's ``$PWD`` is the directory it was started in.

    The parent here carries a usable ``PWD`` -- the usual case -- and
    the child still has to end up with its *own* directory, spelled
    absolutely: a path built from ``$PWD`` in the service's command
    line is resolved against it.
    """
    value = _child_that_reports_its_pwd(project, monkeypatch, str(project))

    assert os.path.isabs(value), (
        "the child expanded $PWD to {0!r}; a path built from it in the "
        "service's command line would be resolved against the child's "
        "cwd and name a file that is not there".format(value)
    )
    assert Path(value).resolve() == (project / "svc").resolve(), (
        "the child expanded $PWD to {0!r}, which is not the directory it "
        "was started in ({1!r})".format(value, str(project / "svc"))
    )


@pytest.mark.timeout(120)
def test_the_child_pwd_is_absolute_when_the_parent_carries_a_relative_one(
    project, monkeypatch
):
    """The case that actually bites: the parent says ``PWD=.``.

    The backend is started by the supervisor, which hands it a relative
    ``PWD``.  Inherited verbatim, that value is useless to the child: it
    names nothing absolute, and the child resolves it against its own
    working directory.
    """
    value = _child_that_reports_its_pwd(project, monkeypatch, ".")

    assert os.path.isabs(value), (
        "the child expanded $PWD to {0!r}; a path built from it in the "
        "service's command line would be resolved against the child's "
        "cwd and name a file that is not there".format(value)
    )
    assert Path(value).resolve() == (project / "svc").resolve(), (
        "the child expanded $PWD to {0!r}, which is not the directory it "
        "was started in ({1!r})".format(value, str(project / "svc"))
    )


@pytest.mark.timeout(120)
def test_the_child_pwd_is_not_the_parents_directory(project, monkeypatch):
    """The parent's ``PWD`` is the parent's directory, not the child's.

    An inherited ``PWD`` that is absolute is not thereby right: it
    names wherever the parent happened to be, and the child runs
    somewhere else.  The child is told about its own directory.
    """
    value = _child_that_reports_its_pwd(project, monkeypatch, "/somewhere/else")

    assert Path(value).resolve() != Path("/somewhere/else").resolve(), (
        "the child kept the parent's PWD; the two processes do not share "
        "a working directory"
    )
    assert Path(value).resolve() == (project / "svc").resolve(), (
        "the child expanded $PWD to {0!r}, which is not the directory it "
        "was started in ({1!r})".format(value, str(project / "svc"))
    )


# ---------------------------------------------------------------------------
# Where the correction is made
# ---------------------------------------------------------------------------


def test_the_wrapper_names_the_childs_own_directory():
    """The wrapper is where the child's directory is spelled out.

    The wrapper is the last place that knows the child's directory for
    sure -- it is the process the child *is* until it execs the real
    command -- and it is shared by production and by the manual spawn
    harness, so both sides of a measurement see the same child.
    """
    wrapper = sm._DETACHED_WRAPPER_SOURCE

    assert "os.getcwd()" in wrapper, (
        "the wrapper does not ask for the child's own directory, so the "
        "child's PWD stays the parent's -- which may be a relative path "
        "that names nothing"
    )
    assert "'PWD'" in wrapper or '"PWD"' in wrapper, (
        "the wrapper asks for the child's directory but does not put it "
        "in PWD"
    )