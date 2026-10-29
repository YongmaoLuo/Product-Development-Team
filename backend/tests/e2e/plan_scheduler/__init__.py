"""End-to-end tests for the native plan scheduler.

This package holds the tests that drive the scheduler the way the
application does: through real processes, real exit codes and real
process groups, rather than through doubles.

It is a package (it carries this ``__init__.py``) so that the modules
under it can import each other by a qualified name rather than by
picking one of them up from a directory on ``sys.path``.

The name collision this file has to survive
--------------------------------------------
This package is called ``plan_scheduler``, and so is the application's
own package (``backend/plan_scheduler/``).  Both want the top-level
name ``plan_scheduler``, and only one of them can have it.

pytest decides which by walking up from the test module to the first
directory that is *not* a package and putting that directory on
``sys.path``.  When that directory is ``tests/e2e/`` — which is what
happens if the chain above this package is ever cut — this package is
imported first, under the name ``plan_scheduler``, and the
application's package is left unreachable.  Every test that imports the
application's package (``from plan_scheduler.store import
SqliteSchedulerStore``) then fails to collect with
``ModuleNotFoundError``.

:func:`pkgutil.extend_path` is the standard library's answer to exactly
this: a package whose ``__path__`` covers more than one directory.  It
appends every other ``plan_scheduler/`` directory found on ``sys.path``,
so the application's modules stay importable under the name they own
while this package keeps the name pytest gave it.

Today the chain above this package is whole — ``tests/e2e/`` and
``tests/`` are packages — so pytest imports this one as
``backend.tests.e2e.plan_scheduler`` and the application's package keeps
its name to itself; the line below is then a no-op.  It is here so that
cutting the chain above this package cannot break the tree: the cost of
the defence is one line, and the cost of not having it is a collection
failure in every test that imports the application's scheduler.
"""

import pkgutil

#: The directories this package spans: this one first, then every
#: other ``plan_scheduler/`` directory on ``sys.path``.
__path__ = pkgutil.extend_path(__path__, __name__)