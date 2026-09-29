"""VP-024 / bug marker anchor completeness meta-test.

Contract: every ``bug_*`` marker registered in ``backend/pytest.ini``
(``bug_1`` .. ``bug_5``) must have at least one *anchor* test case
under the ``backend/tests/`` tree.

Anchor tests are the regression-binding tests referenced in the
``bug_N:`` description line of the registry — the ones that lock the
fix in place.  If a ``bug_N`` marker has no anchor test, a future
refactor could silently drop the regression coverage for that bug.

This meta-test asserts the *coverage* property (>=1 case per marker)
that anchors the marker contract; the per-anchor-naming assertion
lives in ``test_bug_anchor_cases_are_not_skipped.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


BUG_MARKERS = ("bug_1", "bug_2", "bug_3", "bug_4", "bug_5")


def _collect_marker_usages(tests_root: Path) -> dict:
    """Walk ``backend/tests/`` and collect decorator / pytest.mark usages.

    Returns ``{marker_name: [relative_file_path, ...]}``.
    """
    usages: dict = {m: [] for m in BUG_MARKERS}
    pattern = re.compile(
        r"pytest\.mark\.(bug_[1-5])\b"
    )
    for path in tests_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in pattern.findall(text):
            usages.setdefault(marker, []).append(
                str(path.relative_to(tests_root.parent))
            )
    return usages


@pytest.fixture(scope="module")
def bug_marker_usages() -> dict:
    backend_tests = Path(__file__).resolve().parent.parent  # backend/tests/
    return _collect_marker_usages(backend_tests)


@pytest.mark.parametrize("marker_name", BUG_MARKERS)
def test_bug_marker_has_at_least_one_anchor_case(
    marker_name: str, bug_marker_usages: dict
) -> None:
    """Each bug_* marker must appear on >=1 anchor test case."""
    files = bug_marker_usages.get(marker_name, [])
    assert files, (
        f"marker {marker_name!r} is registered in backend/pytest.ini "
        f"but no test under backend/tests/ references it; "
        f"add at least one @pytest.mark.{marker_name} test or "
        f"remove the marker from the registry"
    )
