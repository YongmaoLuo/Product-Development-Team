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
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.app_source import app_source


def test_server_callback_imports_open_db_and_migrate():
    """Static check: the ``_on_repair_complete`` closure body imports
    ``open_db`` and ``migrate`` so the callback doesn't NameError.
    """
    src = app_source()

    # Locate _on_repair_complete function
    cb_start = src.find("def _on_repair_complete")
    assert cb_start >= 0, "_on_repair_complete not found"

    # Find the next nested def or top-level closure boundary by scanning
    # until we hit the next dedent that returns to module-level. The
    # callback body runs roughly 200 lines, so scan the next 400 lines
    # from cb_start.
    cb_body = src[cb_start:cb_start + 8000]

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
    src = app_source()

    cb_start = src.find("def _on_repair_complete")
    cb_body = src[cb_start:cb_start + 8000]

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
