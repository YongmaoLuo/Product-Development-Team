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

The harness suite's fixtures live here as well, and the reason is a second
surprise of the same family: **pytest does not keep a directory conftest's
fixtures for a directory that is collected more than once.**

A conftest's fixtures are not bound when the conftest is imported. They go
into a pending map keyed by the conftest's directory and are attached to a
node only when that directory's collection *finishes*
(``_pytest/fixtures.py`` — ``pytest_make_collect_report`` pops the pending
entry after the collector returns). The fixture lookup then matches on the
**identity** of that node (``_matchfactories`` compares the node itself, not
its path). The CI shard runs pytest with an explicit list of *files*, and for
a file argument pytest rebuilds the collector of the directory that directly
holds that file — so a directory that owns a conftest and is asked for two
files in one worker sees its conftest's fixtures on the first file only, and
the second file's items report ``fixture 'frozen_env' not found``. The
directory that holds ``tests/unit/*.py`` is not rebuilt that way: it is
reached through the cached report of its own parent, so the node pytest binds
this file's fixtures to is the same node for every item under this
directory. That is why the harness fixtures are here and not in
``tests/unit/harness/conftest.py``.

The registry reset is a hook rather than an autouse fixture for the same
reason — an autouse fixture defined in this directory would be bound to this
directory's node, and would be silently missing for every harness file but
the first. Hooks are bound when the conftest is registered, so they are in
force for every item regardless of collection order. The credential pin in
``tests/unit/notifications/conftest.py`` is a hook for the same reason, and
its docstring says so.
"""

from pathlib import Path

import pytest

from harness.registry import reset_runtime_config_for_tests

_HERE = Path(__file__).resolve().parent

#: The harness suite, which lives under this directory.
_HARNESS_DIR = _HERE / "harness"


def pytest_collection_modifyitems(config, items):
    marker = pytest.mark.unit
    for item in items:
        if _HERE in Path(item.path).resolve().parents:
            item.add_marker(marker)


# ---------------------------------------------------------------------------
# The harness suite's environment and cached configuration
# ---------------------------------------------------------------------------
#
# Two things have to be pinned before a harness test can mean the same
# thing on every machine:
#
# * the **environment**. ``PDT_HARNESS`` decides which backend runs, and a
#   MiniMax Code profile contributes ``MCODE_PROFILE``, ``HOME`` and
#   ``MINIMAX_DATA_DIR`` to the environment it runs in. A developer's own
#   shell exports some of these, so a test that reads ``os.environ``
#   without isolating it answers a different question on a different
#   machine. :func:`frozen_env` removes ``PDT_HARNESS`` — "nothing
#   configured" is one of the cases under test, and it is only observable
#   when the variable is absent — and points the other three at a
#   throwaway directory.
#
# * the **cached configuration**. The registry resolves the process-wide
#   configuration once and caches it, so a test that ran earlier would
#   otherwise hand this one a configuration it never asked for.

#: The environment variables the harness layer reads. ``PDT_HARNESS``
#: chooses the backend; the other three are what a profile contributes to
#: the environment its backend runs in.
HARNESS_ENV_VARS = ("PDT_HARNESS", "MCODE_PROFILE", "HOME", "MINIMAX_DATA_DIR")

#: The profile name the frozen environment carries. It is a value no real
#: profile has, so a test can tell the frozen environment from a real one.
FROZEN_PROFILE = "pdt-unit-test"


def _is_harness_item(item) -> bool:
    """Whether ``item`` is a test of the harness suite."""
    path = getattr(item, "path", None)
    return path is not None and _HARNESS_DIR in path.parents


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    """Start every harness test without a cached configuration.

    A wrapper rather than a plain hook so the reset happens *outside* the
    fixture setup: the configuration is forgotten before anything the test
    asks for is built, so a test that resolves the configuration itself is
    the one that gets it.
    """
    if _is_harness_item(item):
        reset_runtime_config_for_tests()
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """End every harness test without a cached configuration either.

    The configuration is process-wide, so a configuration resolved by one
    test would otherwise be the starting point of the next one — and the
    next one may not be a harness test at all.
    """
    try:
        return (yield)
    finally:
        if _is_harness_item(item):
            reset_runtime_config_for_tests()


@pytest.fixture
def frozen_env(monkeypatch, tmp_path):
    """Freeze the harness-related environment for the duration of one test.

    Returns the mapping of the values that were pinned, so a test can
    assert against the environment it was given.
    """
    home = tmp_path / "home"
    data_dir = tmp_path / "minimax-data"
    home.mkdir()
    data_dir.mkdir()

    # Removed rather than set: the default path ("nothing configured")
    # is part of the contract, and it is only observable when the
    # variable is absent.
    monkeypatch.delenv("PDT_HARNESS", raising=False)
    monkeypatch.setenv("MCODE_PROFILE", FROZEN_PROFILE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(data_dir))

    return {
        "MCODE_PROFILE": FROZEN_PROFILE,
        "HOME": str(home),
        "MINIMAX_DATA_DIR": str(data_dir),
    }
