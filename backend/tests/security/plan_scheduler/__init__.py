"""Security tests for the PDT-native scheduler's HTTP surface.

The name collision this file has to survive
--------------------------------------------
This package is called ``plan_scheduler``, and so is the application's
own package (``backend/plan_scheduler/``).  Both want the top-level name
``plan_scheduler``, and only one of them can have it.

pytest decides which by walking up from the test module to the first
directory that is *not* a package and putting that directory on
``sys.path``.  In this tree that directory is ``tests/security/`` — it
carries no ``__init__.py`` — so without the defence below this package
would be imported first, under the name ``plan_scheduler``, and the
application's package would be left unreachable.  Every test that
imports the application's package (``from plan_scheduler.store import
SqliteSchedulerStore``) would then fail to collect with
``ModuleNotFoundError``.

:func:`pkgutil.extend_path` is the standard library's answer to exactly
this: a package whose ``__path__`` covers more than one directory.  It
appends every other ``plan_scheduler/`` directory found on ``sys.path``,
so the application's modules stay importable under the name they own
while this package keeps the name pytest gave it.

The sibling ``tests/e2e/plan_scheduler/__init__.py`` carries the same
defence for the same reason; the difference is that the chain above it
is whole there, so the line below is a no-op, while here it is
load-bearing.

Because this package *is* the top-level ``plan_scheduler`` for the
duration of a session that collects it first, the application's package
initialiser never runs — and with it the two names it re-exports.  They
are re-exported here so that ``from plan_scheduler import
SchedulerConfig`` keeps meaning what it means everywhere else.
"""

import pkgutil

#: The directories this package spans: this one first, then every other
#: ``plan_scheduler/`` directory on ``sys.path``.
__path__ = pkgutil.extend_path(__path__, __name__)

# The application's package initialiser exports these from the top-level
# ``plan_scheduler`` namespace; this package took that name, so it
# re-exports them too.
from plan_scheduler.config import (  # noqa: E402,F401  (needs __path__ above)
    SchedulerConfig,
    SchedulerConfigLoader,
)

__all__ = ["SchedulerConfig", "SchedulerConfigLoader"]