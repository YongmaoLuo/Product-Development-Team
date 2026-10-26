"""No E2E screenshot may be tracked by git.

Why this gate exists
--------------------
The E2E lifecycle UI test drives a real browser and writes the screenshots
it takes into ``backend/tests/e2e/artifacts/<plan_id>/``. That directory is
a **runtime output**, not a fixture: nothing imports it, nothing asserts
against it, and the plan id in its name changes on every run.

One such screenshot was committed. The damage is not the bytes -- it is
that the commit-range privacy scan reads the tree of each commit and
**cannot decode a binary blob**. The scan does not skip what it cannot
read: it reports ``binary content, and its extension is not one this scan
treats as source`` and exits 2, which is the fail-closed exit. Every
push then stops at the pre-push hook, for a reason that has nothing to do
with what is being pushed.

So the rule is the simple one: **the artefacts directory is not tracked**.
``.gitignore`` says so, and this gate says so in a way that is checked
rather than assumed -- ``.gitignore`` only keeps ``git add -A`` from
staging a file, it does not remove a file that is already in the index,
and it does not fail when a path is added by other means.

Why the index and not ``HEAD``
------------------------------
``git ls-files`` reports what the **index** holds. ``HEAD`` reports what
is already committed, and the framework takes its checkpoint commit *after*
this suite runs -- so a gate reading ``HEAD`` would be reading a tree
that does not yet contain the fix, and would stay red for the rest of the
branch. The index is the state that the next commit is built from, which
is exactly the state that must not carry a screenshot.

The empty directory needs no ``.gitkeep``: git does not track
directories, and the test recreates the directory itself when it runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Repository root. This file lives at ``backend/tests/static_gates/``,
#: so the root is three parents up -- the same derivation the sibling
#: gates in this directory use.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The directory the E2E lifecycle UI test writes its screenshots into.
ARTIFACTS_DIR = "backend/tests/e2e/artifacts"


def test_e2e_screenshot_artifacts_are_not_tracked() -> None:
    """No file under the E2E artefacts directory may be in the index."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", ARTIFACTS_DIR],
        capture_output=True, text=True, check=True,
    )
    tracked = [line for line in out.stdout.splitlines() if line.strip()]
    assert tracked == [], (
        "runtime E2E screenshots are tracked; a binary blob makes the "
        "commit-range privacy scan fail closed (exit 2).\n  "
        + "\n  ".join(tracked)
    )
