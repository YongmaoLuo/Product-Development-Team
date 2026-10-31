"""The snapshot says where the capacity came from.

Background
----------
The scheduler's capacity has two sources: the configuration file, and
the operator's runtime override — a row in the state database that
changes the number while the scheduler runs (see
``test_engine_capacity_override.py``).  The panel has to be able to
tell them apart, because the two are different things to an operator:
a number that came from the file is the standing setting, and a number
the operator set is a decision that is in force *right now*.  A panel
that shows both the same way tells the operator a number is the
configured one while it is not.

So the read model carries the **source** next to the number.  The
snapshot does not decide which source is in charge — the engine does,
when it reads the state — and the snapshot does not rewrite a source
it does not recognise: it passes on what it was given, because the
layer that wrote it chose that word on purpose.

These cases pin:

* a state that names the override says ``override``;
* a state that names nothing says ``config`` — the configured
  capacity is the ordinary case, and the override is the one that has
  to be announced;
* a source the snapshot does not know is passed on as it is;
* the engine's state carries the source, so the panel can read it
  there and not have to guess.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Facts:
    """The plan facts, with nothing in them.

    The capacity source does not depend on any plan, so the facts say
    that no plan is queued and no plan is running.
    """

    def queued_plans(self) -> List[dict]:
        """No plan is waiting."""
        return []

    def active_plans(self) -> List[dict]:
        """No plan is running."""
        return []

    def has_pending_decision(self, plan_id: str) -> bool:
        """No plan is waiting for a human."""
        return False


class _Ledger:
    """The concurrency ledger, with no slot taken."""

    def used(self) -> int:
        """No plan holds a slot."""
        return 0


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection, closed when the test is over."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    connection = open_db(tmp_path / "state.db")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


def _engine(conn: sqlite3.Connection, *, capacity: int = 6) -> Any:
    """An engine whose configured capacity is ``capacity``."""
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.store import SqliteSchedulerStore

    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig(capacity=capacity)),
        store=SqliteSchedulerStore(),
        facts=_Facts(),
        ledger=_Ledger(),
        commands=object(),
        sink=object(),
        conn=conn,
    )
    engine._config = engine._read_config()
    return engine


def _write_override(conn: sqlite3.Connection, value: str) -> None:
    """Write the capacity override."""
    from plan_scheduler.store import CAPACITY_OVERRIDE_KEY, SqliteSchedulerStore

    SqliteSchedulerStore().set_setting(
        conn, CAPACITY_OVERRIDE_KEY, value, stamp="2026-10-30T09:00:00"
    )


# ---------------------------------------------------------------------------
# 1. A state that names the override says so
# ---------------------------------------------------------------------------


def test_capacity_source_override() -> None:
    """A capacity that came from the override says so, next to the number.

    The two travel together: the panel marks the number as an override
    *because* the source says so, and a source without a number beside
    it would leave the operator guessing which number it describes.
    """
    from plan_scheduler.snapshot import build

    state = {"capacity": 4, "used": 2, "capacity_source": "override"}

    assert build(state)["capacity"] == {
        "total": 4,
        "used": 2,
        "source": "override",
    }


def test_capacity_source_config_is_carried_as_it_is() -> None:
    """A state that names the configuration says so."""
    from plan_scheduler.snapshot import build

    state = {"capacity": 6, "used": 1, "capacity_source": "config"}

    assert build(state)["capacity"] == {
        "total": 6,
        "used": 1,
        "source": "config",
    }


# ---------------------------------------------------------------------------
# 2. A state that names nothing is read as the configured capacity
# ---------------------------------------------------------------------------


def test_capacity_source_config_default() -> None:
    """A missing source is read as the configured capacity.

    The configured capacity is the ordinary case: a state that says
    nothing about where the number came from is a state whose number
    came from the configuration.  The override is the one that has to
    be announced, so silence means the standing setting.
    """
    from plan_scheduler.snapshot import build

    assert build({})["capacity"]["source"] == "config"
    assert build({"capacity": 4, "used": 2})["capacity"]["source"] == "config"


def test_capacity_source_none_is_the_default() -> None:
    """A source that is present but empty is the default too.

    ``None`` and an absent field mean the same thing here: there is no
    source to report, and the configured capacity is what is left.
    """
    from plan_scheduler.snapshot import build

    state = {"capacity": 4, "used": 2, "capacity_source": None}

    assert build(state)["capacity"]["source"] == "config"


# ---------------------------------------------------------------------------
# 3. A source the snapshot does not know is passed on
# ---------------------------------------------------------------------------


def test_an_unknown_capacity_source_is_not_rewritten() -> None:
    """The snapshot does not turn a word it does not know into one it does.

    The snapshot is a read model, not a policy: it reports what the
    state said.  A source that is not ``override`` and not ``config`` is
    still what the layer that wrote it chose to write, and rewriting it
    here would hide it from the panel — which is exactly the layer that
    would have to decide what to do with it.
    """
    from plan_scheduler.snapshot import build

    state = {"capacity": 4, "used": 2, "capacity_source": "something_else"}

    assert build(state)["capacity"]["source"] == "something_else"


# ---------------------------------------------------------------------------
# 4. The engine's state carries the source
# ---------------------------------------------------------------------------


def test_read_state_says_the_capacity_is_the_configured_one(
    conn: sqlite3.Connection,
) -> None:
    """With no override, the state says the capacity came from the config."""
    engine = _engine(conn, capacity=6)

    state = engine._read_state()

    assert state["capacity"] == 6
    assert state["capacity_source"] == "config"


def test_read_state_says_the_capacity_is_overridden(
    conn: sqlite3.Connection,
) -> None:
    """With an override, the state says so — and the number is the override.

    The two are read together, so the number and the word beside it
    always agree: a capacity read as ``override`` is the operator's
    number, not the configured one.
    """
    engine = _engine(conn, capacity=6)
    _write_override(conn, "3")

    state = engine._read_state()

    assert state["capacity"] == 3
    assert state["capacity_source"] == "override"


def test_the_snapshot_of_a_read_state_carries_the_source(
    conn: sqlite3.Connection,
) -> None:
    """The panel reads the source out of the snapshot the engine builds.

    This is the whole path the panel takes: the engine reads the state,
    the snapshot assembles the read model, and the panel reads the
    source out of it.
    """
    from plan_scheduler.snapshot import build

    engine = _engine(conn, capacity=6)
    _write_override(conn, "3")

    snapshot = build(engine._read_state())

    assert snapshot["capacity"]["source"] == "override"
    assert snapshot["capacity"]["total"] == 3