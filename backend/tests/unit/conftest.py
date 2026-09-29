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
"""

import pytest


def pytest_collection_modifyitems(config, items):
    marker = pytest.mark.unit
    for item in items:
        item.add_marker(marker)
