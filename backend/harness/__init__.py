"""The harness layer — one execution backend behind one contract.

Why this file exists
--------------------
The harness layer is imported as the **top-level** ``harness`` package:
``backend/__init__.py`` puts ``backend/`` on ``sys.path`` so that
``from harness.contract import ...`` resolves against ``backend/harness/``
from every entry point (pytest, uvicorn, a bare ``python3 -c``).

``backend/harness/`` used to have no ``__init__.py``, which made it a
**namespace package** (PEP 420).  That is fragile in one specific way: a
namespace portion never wins against a regular package, no matter where
each of them sits on ``sys.path``.  So a regular package called
``harness`` anywhere else on the path — and there is one: the test
suite's own ``tests/unit/harness/`` — silently **shadowed** the
production package, and ``from harness.contract import ...`` failed with
``ModuleNotFoundError: No module named 'harness.contract'``.

This file makes the production package a **regular** package.  A regular
package wins by being found first, so ``backend/harness/`` keeps the
name in every context, and no test directory can take it away.
"""

from __future__ import annotations
