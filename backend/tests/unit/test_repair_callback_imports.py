"""Pin the 2026-09-13 fix for _on_repair_complete NameError.

Bug
---
``_on_repair_complete`` (server.py:6318) is a closure nested inside
``_run_auto_verification_loop``. It references ``open_db`` and
``migrate`` at runtime, but those imports live in OTHER helper
functions in the same file, NOT at module top-level. The callback
crashed with::

    NameError: name 'open_db' is not defined

The executor subprocess that ran R6-2 (the rebuild Rust dylib +
raise coverage task) ran successfully for ~1h42min, then when its
``on_complete`` callback fired, the chain recorded
``repair_callback_crashed`` and stopped — RP-* was never re-persisted.

Fix
----
Import ``open_db`` and ``migrate`` inside the callback's try-block
so they're in local scope when needed. (Localised imports are fine
because Python caches them in ``sys.modules``; the only cost is the
``import`` statement lookup on the first call.)

Where the closure lives
-----------------------
The closure moved out of ``server.py`` and into
``backend/verification_loop.py``; the check follows it there.  The
scope is found by AST, not by counting characters: see
:func:`_callback_body` for why the character window had to go.
"""
from __future__ import annotations

from tests.app_source import find_def


def _callback_body() -> str:
    """The source of the ``_on_repair_complete`` closure, and nothing else.

    The closure is located by **AST**, not by counting characters.
    The imports this file checks live in the closure's body, and the
    closure's body is what the AST says it is — so the scope is the
    closure, and it stays the closure when the code around it moves.

    It used to be a window of 8000 characters, taken from the ``def``
    line.  The window is a guess about a length, and it guessed
    wrong: the merge pushed the distance from the closure's start to
    the migrated alias import past 8000 characters, so the two
    assertions below fell outside the window and read as "the closure
    lost its import" — while the closure had never lost anything.  A
    failure that says the code is gone when the code is there sends
    the reader to delete the test, and loses the contract.

    A bigger magic number would have the same defect one merge later.
    The AST has no such number: it knows where the function ends.
    """
    return find_def("_on_repair_complete").text


def test_server_callback_imports_open_db_and_migrate():
    """Static check: the ``_on_repair_complete`` closure body imports
    ``open_db`` and ``migrate`` so the callback doesn't NameError.
    """
    cb_body = _callback_body()

    assert "from state_machine.db.connection import open as open_db" in cb_body, (
        "BUG: _on_repair_complete closure body lost the open_db "
        "import — it will NameError when executor subprocess completes"
    )
    assert "from state_machine.db.schema import migrate" in cb_body, (
        "BUG: _on_repair_complete closure body lost the migrate "
        "import — it will NameError on the schema migration line"
    )


def test_callback_uses_aliases_consistently():
    """Pin that the alias names match the call sites."""
    cb_body = _callback_body()

    # open_db is imported with ``as open_db`` so the existing
    # ``open_db(_state_db_path())`` call works without a rename.
    assert "import open as open_db" in cb_body, (
        "open_db must be aliased from open so the existing call site works"
    )
    # migrate is imported as ``_migrate`` so the existing
    # ``_migrate(_v_conn)`` call works without a rename.
    assert "import migrate as _migrate" in cb_body, (
        "migrate must be aliased as _migrate so the existing call site works"
    )


def test_the_located_body_is_the_closure_and_not_its_neighbour():
    """The scope starts at the closure and stops at its end.

    The control for the locator.  A scope that ran past the closure
    would find the imports of a *different* function and pass for the
    wrong reason — and a scope that ran to the end of the file would
    pass whatever the file happens to contain.
    """
    located = find_def("_on_repair_complete")
    body = located.text
    body_lines = body.splitlines()
    file_lines = located.source.splitlines()

    assert body_lines[0].startswith("def _on_repair_complete"), (
        "the located body does not start at the closure: %r" % body_lines[0]
    )
    assert len(body_lines) < len(file_lines), (
        "the located body is the whole file (%d lines); the scope is not "
        "bounded by the closure" % len(body_lines)
    )
