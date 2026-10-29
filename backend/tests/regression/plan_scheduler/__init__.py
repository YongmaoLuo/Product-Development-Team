"""Regression tests for the PDT-native plan scheduler.

The name collision this file has to survive
--------------------------------------------
This package is called ``plan_scheduler``, and so is the application's
own package (``backend/plan_scheduler/``).  Both want the top-level name
``plan_scheduler``, and only one of them can have it.

pytest decides which by walking up from the test module to the first
directory that is *not* a package and putting that directory on
``sys.path``.  In this tree the chain above this package is whole —
``tests/`` and ``tests/regression/`` both carry an ``__init__.py`` — so
this package is imported as ``backend.tests.regression.plan_scheduler``
and never claims the top-level name; the application's package keeps it.

Should that chain ever be cut, this package *would* be imported first,
under the name ``plan_scheduler``, and the application's package would be
left unreachable: every test that imports the application's package
(``from plan_scheduler.store import SqliteSchedulerStore``) would then
fail to collect with ``ModuleNotFoundError``.

:func:`pkgutil.extend_path` is the standard library's answer to exactly
this: a package whose ``__path__`` covers more than one directory.  It
appends every other ``plan_scheduler/`` directory found on ``sys.path``,
so the application's modules stay importable under the name they own
while this package keeps the name pytest gave it.

Nothing is imported from the application's package here, and that is
deliberate.  This module is imported during *collection*, and importing
``plan_scheduler`` at that moment binds the top-level name for the whole
session — and that name belongs to the sibling package
``tests/security/plan_scheduler/``, which claims it for itself.  The
sibling packages ``tests/e2e/plan_scheduler/`` and
``tests/security/plan_scheduler/`` carry the same defence for the same
reason.
"""

import pkgutil

#: The directories this package spans: this one first, then every other
#: ``plan_scheduler/`` directory on ``sys.path``.
__path__ = pkgutil.extend_path(__path__, __name__)