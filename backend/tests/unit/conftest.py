"""Auto-apply the ``unit`` marker to every test collected under this directory.

The autonomous-coding verification gate runs:

    pytest backend/tests/unit --cov=backend --cov-fail-under=80 -m "unit"

which requires every test in this tree to carry ``@pytest.mark.unit`` (or be
auto-marked). Adding the marker by hand across 50+ files is fragile, so we
inject it here at collection time.

Note: we cannot short-circuit on ``"unit" in item.keywords`` — that mapping is
populated from path components (``tests/unit``), not from registered markers.
Pytest's ``-m EXPR`` filter matches registered markers via ``iter_markers``,
so we must call ``add_marker`` unconditionally.

2026-10-12 — the scoping below is load-bearing. A subdirectory conftest's
``pytest_collection_modifyitems`` is a **session-level** hook: ``items`` is
every test pytest collected, not only the ones under this directory. Marking
all of them made ``-m unit`` select the **entire** suite — 6679 tests,
``tests/e2e/`` included, against 4108 for this directory — while ``README.md``
and ``docs/development/testing.md`` both advertised ``-m unit`` as the unit
lane. The e2e tests it dragged in prompt for the keychain. The hook now marks
only what this docstring always claimed it marked.
"""

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    marker = pytest.mark.unit
    for item in items:
        if _HERE in Path(item.path).resolve().parents:
            item.add_marker(marker)
