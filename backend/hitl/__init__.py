"""Human-in-the-loop (HITL) decisions.

Why this package exists
-----------------------
Some questions cannot be answered by the backend: *is this the change
you wanted?*, *the verification found three failures — retry or stop?*.
The question is asked from one thread (an executor, a verification
agent, a route handler) and answered from another (a human, on a
different machine, minutes later).  The two never share memory, so the
decision has to be stored somewhere both can reach — the SQLite
database the state machine already owns — and it has to carry the whole
fact on its own: the question, the options, the deadline, the answer
and who gave it.

What lives here
---------------
:mod:`hitl.models` holds the value objects that travel over that
table:

  * :class:`~hitl.models.DecisionRequest` — what a caller asks.
  * :class:`~hitl.models.Decision` — what is stored.
  * :class:`~hitl.models.DecisionStatus` and
    :class:`~hitl.models.DecisionMode` — the two closed vocabularies
    both objects are drawn from.

The storage half lives in :mod:`state_machine.db.schema` as the
``plan_decisions`` table; this package deliberately knows nothing about
SQLite, so the model can be imported by a request handler, a worker
thread or a CLI without dragging a database connection along.

Both vocabularies are *closed*: an unknown ``mode`` or ``status`` is
rejected with ``ValueError`` at construction time rather than written
to the database, where a reader that does not know the word would have
to guess what to do with it.
"""

from __future__ import annotations

from .models import Decision, DecisionMode, DecisionRequest, DecisionStatus

__all__ = [
    "Decision",
    "DecisionMode",
    "DecisionRequest",
    "DecisionStatus",
]
