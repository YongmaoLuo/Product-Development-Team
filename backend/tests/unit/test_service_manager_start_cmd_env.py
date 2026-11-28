"""A shell separator ends an assigned value -- it is not part of it.

What is under test
------------------
:func:`service_manager.start_cmd_env` and the declaration-level reader
that stands on top of it, :func:`service_manager.declared_state_db_path`.

The defect
----------
A start command spells the environment the service runs in::

    export PDT_STATE_DB_PATH=/tmp/x/state.db; exec python -m backend.server

The value of that assignment ends at the ``;`` -- ``;`` is the shell's
own operator, not a character of the path.  The parser used to take the
value as "everything that is not whitespace", so it produced
``"/tmp/x/state.db;"`` -- a name no file has.

Why that breaks a round
-----------------------
:func:`declared_state_db_path` is the service's **own** statement about
which database it is going to serve, and it is the statement the
instance is judged against.  With the separator glued to the path, the
declared name and the path the running instance reports are two
different *strings*, and :func:`service_manager._same_state_db` compares
two absolute names by their real path -- ``/tmp/x/state.db;`` and
``/tmp/x/state.db`` are not the same file, so it answers **False**.

``False`` means "the instance is serving a database its own declaration
never named", and :func:`service_manager._data_plane_is_acceptable`
refuses the instance outright in the empty-data-plane branch.  The
readiness wait therefore never sees a ready service and the service's
preflight fails after its whole budget.

What these pins hold
--------------------
  * a separator terminates the value: ``export A=1; export B=2;`` is two
    assignments, ``{"A": "1", "B": "2"}``;
  * ``&&`` never becomes part of a value either;
  * a value that carries no separator is parsed exactly as before;
  * the declared state database is the path the file actually has, and
    it is the same database the instance reports;
  * a service that serves the database it declared is accepted.

The pins are hermetic: they read a command line and a temporary
database, and they start no process and sleep for no time.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


def _declaration(start_cmd: str) -> ServiceDeclaration:
    """A declaration that starts its service with *start_cmd*."""
    return ServiceDeclaration(name="svc", port=8811, start_cmd=start_cmd)


def test_a_semicolon_separated_chain_yields_every_assignment():
    """``export A=1; export B=2;`` is two assignments, not two values
    that each carry a trailing ``;``."""
    assert sm.start_cmd_env("export A=1; export B=2;") == {
        "A": "1",
        "B": "2",
    }


def test_a_semicolon_terminates_the_value():
    """The value of an assignment ends at the separator."""
    env = sm.start_cmd_env("export PDT_STATE_DB_PATH=/tmp/x/state.db;")
    assert env["PDT_STATE_DB_PATH"] == "/tmp/x/state.db"


def test_a_bare_assignment_next_to_a_semicolon_is_read_too():
    """A bare ``FOO=bar`` prefix ends at the separator as well."""
    assert sm.start_cmd_env("FOO=bar; exec python server.py") == {
        "FOO": "bar",
    }


def test_the_and_and_operator_never_enters_a_value():
    """``&&`` separates segments; it is not part of the value before it."""
    env = sm.start_cmd_env("A=1 && B=2 && exec python server.py")
    assert env == {"A": "1", "B": "2"}
    assert env["A"] == "1"
    assert env["B"] == "2"


def test_a_value_without_a_separator_is_unchanged():
    """The ordinary case -- a value that ends at whitespace or at the
    end of the command -- parses exactly as it always did."""
    assert sm.start_cmd_env("export A=1") == {"A": "1"}
    assert sm.start_cmd_env(
        "export PDT_STATE_DB_PATH=/var/lib/pdt/state.db "
        "&& exec python -m backend.server",
    ) == {"PDT_STATE_DB_PATH": "/var/lib/pdt/state.db"}


def test_declared_state_db_path_drops_the_trailing_separator():
    """The declaration names the path the file has, separator or not."""
    decl = _declaration("export PDT_STATE_DB_PATH=/tmp/x/state.db;")
    declared = sm.declared_state_db_path(decl)
    assert declared == "/tmp/x/state.db"
    assert ";" not in declared


def test_the_declared_and_the_reported_database_are_the_same_file(tmp_path):
    """The path the declaration names and the path the instance reports
    are two spellings of one file, so they are the same database."""
    db_path = tmp_path / "state.db"
    decl = _declaration(f"export PDT_STATE_DB_PATH={db_path};")
    declared = sm.declared_state_db_path(decl)
    assert sm._same_state_db(declared, str(db_path)) is True


def test_an_empty_data_plane_on_the_declared_database_is_accepted(
    tmp_path,
):
    """The whole chain, from the start command to the verdict: a service
    that declares -- and serves -- an empty database of its own is
    accepted.  With the separator glued to the declared path the two
    names stop matching and the instance is refused for ever."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE plan_routing (plan_id TEXT PRIMARY KEY)",
        )
        conn.commit()
    finally:
        conn.close()

    decl = _declaration(f"export PDT_STATE_DB_PATH={db_path};")
    evidence = sm.DataPlaneEvidence(
        verdict=sm.DATA_PLANE_EMPTY,
        plan_routing_rows=0,
        state_db_path=str(db_path),
    )
    assert sm._data_plane_is_acceptable(evidence, None, decl) is True
