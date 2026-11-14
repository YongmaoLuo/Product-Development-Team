"""The OpenAPI baseline must **carry** the scheduler's plan-detail endpoint.

Why this file exists
--------------------
``test_openapi_unchanged.py`` freezes the published HTTP contract.  It is
a **comparator**: it says whether the contract moved, and — when it
moved — which endpoints moved.  It cannot say what the contract *is*.

That gap matters here.  The scheduler grew one endpoint on purpose::

    GET /api/scheduler/plan/{plan_id}/detail

A purposeful addition and an accidental one look identical to the
comparator: both are "the schema differs from the baseline".  The
comparator's answer to both is the same — *re-take the baseline and
update the fingerprint* — which is exactly the answer that is right for
the accidental case and **wrong** for the deliberate one.  A reviewer who
only has the comparator cannot tell "someone added the endpoint we meant
to add" from "someone quietly changed the HTTP surface".

So this file is the **inclusion** half of the pair.  It names the
endpoint that was deliberately added and asserts two things about the
committed artefacts:

1. the committed snapshot **contains** that path (with its ``get``
   operation), and
2. the committed ``BASELINE_FINGERPRINT`` is the fingerprint **of that
   snapshot**.

Together the two files answer "what is the contract" and not only "did
it change".  The first answers *which* endpoints are in the baseline;
the second answers *whether the committed anchor still describes it*.

The fingerprint is computed, never written by hand
--------------------------------------------------
``BASELINE_FINGERPRINT`` is the SHA-256 of the baseline file's content in
the canonical form the existing gate already uses: the parsed JSON,
re-serialised with sorted keys and no whitespace.  This file computes it
the same way — the constant cannot be a number somebody typed, and the
snapshot and the constant cannot disagree about what "the same shape"
means.

The snapshot path is derived from this file's own location
----------------------------------------------------------
``Path(__file__).resolve().parent`` — the directory holding this file —
rather than a relative path or one built from the working directory.
The gate is run from several places (the repository root, ``backend/``,
the CI container); a baseline that resolves from only one of them is a
baseline that silently stops being checked from the others.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from backend.tests.api.test_openapi_unchanged import BASELINE_FINGERPRINT

#: The directory holding this file (``backend/tests/api/``).
_TESTS_DIR = Path(__file__).resolve().parent

#: The committed OpenAPI baseline — the file the contract gate compares
#: against.  Derived from this file's own directory, so it resolves the
#: same way whatever the working directory is.
SNAPSHOT_PATH = _TESTS_DIR / "openapi_snapshot.json"

#: The path the scheduler's plan-detail endpoint publishes.
DETAIL_PATH = "/api/scheduler/plan/{plan_id}/detail"


def _load_snapshot() -> Dict[str, Any]:
    """The committed baseline, parsed.

    A missing baseline is a failure, not an excuse to generate one: this
    file asserts about the **committed** artefact, and a snapshot that
    only exists in the working tree proves nothing about the repository.
    """
    assert SNAPSHOT_PATH.exists(), (
        f"the committed OpenAPI baseline is missing ({SNAPSHOT_PATH.name}). "
        "This file asserts about what the repository carries, so there is "
        "nothing to compare against."
    )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _canonical(schema: Dict[str, Any]) -> str:
    """The stable text form of a schema — what the fingerprint hashes.

    The same rule the contract gate uses, so the two files agree on
    which bytes "the same shape" means.
    """
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def _fingerprint(schema: Dict[str, Any]) -> str:
    """The SHA-256 of a schema's canonical form."""
    return hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()


def test_snapshot_contains_detail_path() -> None:
    """The committed baseline carries the scheduler's plan-detail endpoint.

    Without this entry the contract gate compares the published schema
    against a shape that predates the endpoint, so every run of it
    reports a difference nobody intended — and the difference it reports
    is indistinguishable from a real, unwanted change.
    """
    paths = _load_snapshot().get("paths", {})

    assert DETAIL_PATH in paths, (
        f"the committed OpenAPI baseline ({SNAPSHOT_PATH.name}) does not "
        f"contain {DETAIL_PATH}. The baseline was not re-taken after the "
        "detail endpoint was added, so the contract gate is comparing the "
        "published schema against a shape the service no longer has."
    )

    operations = paths[DETAIL_PATH]
    assert "get" in operations, (
        f"the committed baseline carries {DETAIL_PATH} but not as a GET "
        f"(it carries: {sorted(operations)}). The endpoint the scheduler "
        "calls is a read; the baseline must describe it as one."
    )


def test_fingerprint_matches_snapshot() -> None:
    """The committed constant is the fingerprint of the committed snapshot.

    The snapshot file is the baseline; ``BASELINE_FINGERPRINT`` is the
    anchor that travels with the code and keeps the gate meaningful on a
    machine that has never seen the snapshot file.  If the two disagree,
    one of them describes a shape the other does not — and the gate
    becomes a comparison against whichever the reader happened to trust.
    """
    fingerprint = _fingerprint(_load_snapshot())

    assert fingerprint == BASELINE_FINGERPRINT, (
        "BASELINE_FINGERPRINT does not describe the committed OpenAPI "
        f"baseline (the snapshot fingerprints as {fingerprint}, the "
        f"constant says {BASELINE_FINGERPRINT}). The two artefacts that "
        "pin the HTTP contract disagree about what the contract is. The "
        "fingerprint must be computed from the snapshot's canonical JSON, "
        "not written by hand."
    )