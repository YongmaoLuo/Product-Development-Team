"""The OpenAPI baseline must **carry** the failure-review endpoint — and the
contract gate that reads that baseline must be green.

Why this file exists
--------------------
``test_openapi_unchanged.py`` freezes the published HTTP contract.  It is a
**comparator**: it says whether the contract moved, and — when it moved —
which endpoints moved.  It cannot say what the contract *is*.

That gap matters here.  Task 9 added one endpoint on purpose::

    GET /api/plan/{plan_id}/failure-review

A purposeful addition and an accidental one look identical to the
comparator: both are "the schema differs from the baseline".  The
comparator's answer to both is the same — *re-take the baseline and update
the fingerprint* — which is exactly the answer that is right for the
accidental case and **wrong** for the deliberate one.  A reviewer who only
has the comparator cannot tell "the endpoint we meant to add is in the
baseline" from "someone quietly changed the HTTP surface".

This file is the **inclusion** half of the pair.  It pins two things:

1. the committed snapshot **contains** ``/api/plan/{plan_id}/failure-review``
   with its ``get`` operation, and
2. the committed contract gate is **green** — it exits zero.

The gate is *run*, not re-implemented
-------------------------------------
The second check spawns ``pytest backend/tests/api/test_openapi_unchanged.py``
in a subprocess and asserts that it exits zero.  Re-implementing the
comparison here would be a second copy of the rule, free to drift from the
first one; **running** the rule is the only way the answer can mean "the
gate this repository ships is green".  The gate's own output is carried into
the failure message, so a red run says *why* it was red.

The nested run gets a clean environment
----------------------------------------
The suite's hermetic environment (``backend/_pytest_hermetic_env.py``) keys
the throwaway state database and the plans tree on a session root that it
mints at import time.  That variable is **inherited** by child processes,
so a nested run would otherwise share the outer session's database and
plans tree — two runs pretending to be independent while writing to one
place.  The variables that decide the session are therefore dropped from
the child's environment and the nested run mints its own.

The snapshot path is derived from this file's own location
------------------------------------------------------------
``Path(__file__).resolve().parent`` — the directory holding this file —
rather than a relative path or one built from the working directory.  The
gate is run from several places (the repository root, ``backend/``, the CI
container); a baseline that resolves from only one of them is a baseline
that silently stops being checked from the others.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

#: The directory holding this file (``backend/tests/api/``).
_TESTS_DIR = Path(__file__).resolve().parent

#: The repository root (the parent of ``backend/``).
_REPO_ROOT = _TESTS_DIR.parents[2]

#: The committed OpenAPI baseline — the file the contract gate compares
#: against.
SNAPSHOT_PATH = _TESTS_DIR / "openapi_snapshot.json"

#: The contract gate this file checks, as a path relative to the
#: repository root (the subprocess runs from there).
GATE_PATH = "backend/tests/api/test_openapi_unchanged.py"

#: The path the failure-review endpoint publishes.
FAILURE_REVIEW_PATH = "/api/plan/{plan_id}/failure-review"

#: Environment variables that decide *which* session a pytest run belongs
#: to.  They are inherited by child processes, so a nested pytest run would
#: share the outer session's throwaway state database and plans tree
#: unless they are dropped.
_SESSION_ENV_VARS = (
    "PDT_TEST_SESSION_ROOT",
    "_PDT_TEST_SESSION_ROOT_OWNER",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_CURRENT_TEST",
)


def _load_snapshot() -> Dict[str, Any]:
    """The committed baseline, parsed.

    A missing baseline is a failure, not an excuse to generate one: this
    file asserts about the **committed** artefact, and a snapshot that only
    exists in the working tree proves nothing about the repository.
    """
    assert SNAPSHOT_PATH.exists(), (
        f"the committed OpenAPI baseline is missing ({SNAPSHOT_PATH.name}). "
        "This file asserts about what the repository carries, so there is "
        "nothing to compare against."
    )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _child_environment() -> Dict[str, str]:
    """The environment for the nested gate run — the outer one, minus the
    variables that decide which session a pytest run belongs to."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in _SESSION_ENV_VARS
    }


def test_snapshot_contains_failure_review_endpoint() -> None:
    """The committed baseline carries the failure-review endpoint.

    Without this entry the contract gate compares the published schema
    against a shape that predates the endpoint, so every run of it reports
    a difference nobody intended — and the difference it reports is
    indistinguishable from a real, unwanted change.
    """
    paths = _load_snapshot().get("paths", {})

    assert FAILURE_REVIEW_PATH in paths, (
        f"the committed OpenAPI baseline ({SNAPSHOT_PATH.name}) does not "
        f"contain {FAILURE_REVIEW_PATH}. The baseline was not re-taken "
        "after the failure-review endpoint was added, so the contract gate "
        "is comparing the published schema against a shape the service no "
        "longer has."
    )

    operations = paths[FAILURE_REVIEW_PATH]
    assert "get" in operations, (
        f"the committed baseline carries {FAILURE_REVIEW_PATH} but not as a "
        f"GET (it carries: {sorted(operations)}). The endpoint is a read; "
        "the baseline must describe it as one."
    )


def test_openapi_gate_is_green() -> None:
    """The committed contract gate passes on the committed tree.

    This is the whole point of the file: the baseline and the fingerprint
    are only a contract if something **checks** them, and the check has to
    be green for the tree as it stands.  The gate is run in its own
    process — the same way the repository runs it — so the answer is an
    observation about a real run rather than a second opinion about what
    the gate would say.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", GATE_PATH, "-v"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=_child_environment(),
        timeout=600,
    )

    assert proc.returncode == 0, (
        f"the OpenAPI contract gate ({GATE_PATH}) is not green "
        f"(exit {proc.returncode}).\n"
        f"--- stdout (tail) ---\n{(proc.stdout or '')[-4000:]}\n"
        f"--- stderr (tail) ---\n{(proc.stderr or '')[-2000:]}"
    )
