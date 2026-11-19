#!/usr/bin/env python3
"""Refuse a pasted copy at commit time.

Why this exists
---------------
The rules that decide what a pasted copy is live in
``backend/tests/static_gates/test_no_paste_artifact_copies.py``. Until
now they only ran in ``pytest`` and in CI, which means a pasted copy was
found once the commit carrying it already existed — at which point the
copy is in the history and taking it out costs a rewrite.

This wrapper puts the same rules in front of the commit, by asking them
about the **index**: the files staged for the next commit, with the
bodies they are staged with. A copy that is still a file somebody can
edit is refused before the commit exists, which is the only point at
which refusing costs nothing.

What this file is, and what it is not
-------------------------------------
It is a forwarder and nothing else. The gate module owns the rules, and
this file owns no rule of its own: it puts the backend package on
``sys.path``, imports the module that holds the rules, and hands the
command line to it. A second copy of a rule here would be a rule that
could drift from the one the CI gate runs, and a drift there would be
invisible — the gate would still be right, and the hook would be wrong.

Why it runs under a bare ``python3``
------------------------------------
The pre-commit hook calls it as ``python3``, with no venv and no
third-party packages, exactly as the commit-message hook does. So the
module this file imports has to import without pytest installed, which
is why the gate module's pytest import is optional.

One entry point, one rule
-------------------------
The gate module is also runnable on its own::

    python3 backend/tests/static_gates/test_no_paste_artifact_copies.py

and this wrapper is what the pre-commit hook calls. Both reach the same
``main``.

Exit contract
-------------
  * ``0`` — the scan ran and found nothing
  * ``1`` — the scan ran and found at least one pasted copy; every
    finding is printed as ``file:line`` and the rule it broke
  * ``2`` — a usage error, or a scan that could not run

Usage
-----
::

    python3 scripts/check_no_paste_artifacts.py               # the tracked tree
    python3 scripts/check_no_paste_artifacts.py --check-staged  # the index
    python3 scripts/check_no_paste_artifacts.py --dir <dir>     # a directory tree
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The backend package root. The gate module lives at
#: ``backend/tests/static_gates/``, so it is importable as
#: ``tests.static_gates.<module>`` once ``backend/`` is on the path.
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if not _BACKEND.is_dir():  # pragma: no cover - broken checkout
    print(f"error: backend package not found: {_BACKEND}", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(_BACKEND))

try:
    from tests.static_gates.test_no_paste_artifact_copies import main  # noqa: E402
except ImportError as exc:  # pragma: no cover - broken checkout
    print(
        f"error: cannot import the paste-artifact gate from {_BACKEND}: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
