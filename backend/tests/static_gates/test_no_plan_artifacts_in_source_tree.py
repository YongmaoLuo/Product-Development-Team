"""The backend package must not contain a plan's runtime artefacts.

Why this gate exists
--------------------
A plan's artefacts live in ``plans/<id>/`` and every reader in the backend
resolves them through its own ``plan_dir`` — ``self.plan_dir /
"verification_plan.json"``, never a path relative to the source tree. So a
file with one of those names sitting *inside* ``backend/`` has no reader at
all: it is a run that resolved its output directory wrongly, committed.

One did. ``backend/verification_plan.json`` (9,996 bytes) carried a stale
plan's ten verification points into the repository, where it survived
review precisely because it looks like a fixture: it is valid JSON, in a
directory full of real fixtures, and no test imports it. The cost is not
the bytes — it is that a reader of the repository cannot tell a stray
artefact from a deliberate sample, and that a future contributor may
mistake it for the schema's canonical shape.

The check is a filename match, not a schema check: the point is the
*location*, and a matching name in the source tree is wrong regardless of
what is in the file. Fixture directories are exempt by inclusion — the
rule looks only at files directly inside ``backend/``, and
``backend/tests/fixtures/`` is where a deliberately-committed sample
belongs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: Filenames the backend writes into a plan directory. None of them has a
#: legitimate home directly inside the source tree.
PLAN_ARTIFACT_NAMES = frozenset({
    "interview.json",
    "prd.json",
    "plan_state.json",
    "tasks.json",
    "execution.json",
    "execution.log",
    "verification_plan.json",
    "verification_report.json",
    "verification_execution_results.json",
})


@pytest.mark.parametrize("name", sorted(PLAN_ARTIFACT_NAMES))
def test_backend_dir_holds_no_such_file(name: str) -> None:
    stray = _BACKEND_DIR / name
    assert not stray.exists(), (
        f"{stray.relative_to(_BACKEND_DIR.parent)} is a plan artefact in the "
        f"source tree. Its readers resolve it from a plan directory "
        f"(``plan_dir / {name!r}``), so nothing here can read this copy — a "
        f"write resolved the wrong output directory. Delete it and check "
        f"whatever produced it; if it is meant as a sample, it belongs under "
        f"backend/tests/fixtures/."
    )
