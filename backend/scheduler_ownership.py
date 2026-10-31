"""Who owns a plan's run — an operator's start, or the scheduler?

The scheduler holds a **concurrency slot** for every plan it admitted
that is still working (``scheduler_slot``).  A run that outlived the
backend that started it is **adopted** by the scheduler: the
scheduler's own recovery (``plan_scheduler.engine``'s ``_recover``)
re-times the slot from the moment of the restart, and the plan goes on
running with the very process it was running with.

That adoption is a product contract, not an accident — it is what
makes a ``kill -9`` of the backend cost the operator nothing.  So the
two places that would otherwise take such a run away have to leave it
alone:

* the **startup reconcile** (``routes.execution._reconcile_exec_pid``),
  which reaps an executor whose backend is gone; and
* the **startup sweep** (``service_manager.reap_all_plans``), which
  cleans up after the previous life.

Both ask this module, so the two answers cannot drift apart.

A run the scheduler does **not** hold is none of this module's
business: it is reaped exactly as it was before this module existed.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def scheduler_holds(plan_id: str, db_path: Optional[Path] = None) -> bool:
    """Whether the scheduler holds a concurrency slot for *plan_id*.

    The answer is read out of the scheduler's own table, because that
    table is the scheduler's own statement about which plans it is
    running.  It is a durable statement: the rows outlive the backend
    that wrote them, which is exactly what makes it the right thing to
    ask at boot — the previous life's rows are still there, saying
    which plans that life was running.

    Never raises.  A question asked on the recovery path must not cost
    the recovery its answer, so a database that cannot be read, or one
    that has no ``scheduler_slot`` table at all, answers *no* — and
    *no* is the answer that leaves every other rule alone.

    Args:
        plan_id: The plan whose ownership is in question.
        db_path: The state database to read.  Defaults to the one this
            instance is configured to use.

    Returns:
        ``True`` when the scheduler holds a slot for the plan.
    """
    if not plan_id:
        return False
    try:
        path = Path(db_path) if db_path is not None else _state_db_path()
    except Exception:  # noqa: BLE001 — an unresolvable path is a "no"
        return False
    try:
        if not path.exists():
            return False
        connection = sqlite3.connect(
            "{0}?mode=ro".format(path.absolute().as_uri()),
            uri=True,
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001 — an unreadable database is a "no"
        logger.warning(
            "[scheduler_ownership] could not open %s; treating every plan "
            "as unowned",
            path,
        )
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM scheduler_slot WHERE plan_id = ? LIMIT 1",
            (str(plan_id),),
        ).fetchone()
    except sqlite3.Error:
        # No ``scheduler_slot`` table: the scheduler has never run
        # against this database, so it holds nothing.
        return False
    finally:
        connection.close()
    return row is not None


def _state_db_path() -> Path:
    """The state database this instance uses."""
    from config_paths import resolve_state_db_path

    return resolve_state_db_path()
