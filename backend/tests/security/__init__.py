"""The security test tree, as a package.

Why this directory carries a package marker
-------------------------------------------
``tests/security/plan_scheduler/`` is a package, and pytest imports a test
module by walking up from the file to the first directory that is **not** a
package.  Without a marker here that walk stops at ``tests/security/``, and
the module is imported under the top-level name ``plan_scheduler`` -- the
name the application's own package (``backend/plan_scheduler/``) already
owns.  Only one of the two can hold it: whichever is imported first wins,
the other becomes unreachable, and the suite fails to collect with
``ModuleNotFoundError: No module named 'plan_scheduler.test_scheduler_security'``.

This marker makes the chain whole, so the test module is imported as
``backend.tests.security.plan_scheduler.test_scheduler_security`` and the
application's package keeps the name it owns.  The sibling trees --
``tests/unit/``, ``tests/integration/``, ``tests/regression/``,
``tests/e2e/`` and ``tests/perf/`` -- carry the same marker for the same
reason.
"""