"""``_cleanup_tmp_files`` must delete only the files the test created.

2026-09-23 incident (cross-process kill switch)
----------------------------------------------
``/tmp/subagent_settings_*.json`` is not a test-private namespace. It is
also how a **live** backend execution hands its provider config to a running
``claude`` subprocess (``SubagentConfig.write_tmp_settings`` — the file
is deliberately left on disk for post-mortem, per decision 3).

``test_provider_order_integration._cleanup_tmp_files`` used to do::

    for p in Path("/tmp").glob("subagent_settings_*.json"):
        p.unlink()

— a machine-wide sweep. A task whose ``test_command`` is the full
``pytest backend/tests/`` ran for ~90 minutes and deleted the
in-flight settings file of a live plan twice. Its sub-agents died in
0.2 s::

    Claude sub-process exited without assistant text (returncode=1,
    elapsed=0.2s). Stderr: Error: Settings file not found:
    /tmp/subagent_settings_87ccd91b….json

Each death failed a task → deadlocked its dependency chain → the plan
parked at ``No schedulable micro-layer found``. From the operator's side
it looked like a plan that "kept pausing" for no reason, and the card
that said so was telling the truth.

Contract pinned here
--------------------
1. A file that existed **before** the test ran survives the cleanup,
   even though it matches the glob exactly.
2. A cleanup call with no snapshot deletes **nothing** — the missing
   snapshot must degrade toward leakage, never toward a wider sweep.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_sibling():
    """Import ``test_provider_order_integration`` by path.

    Not ``import tests.integration...``: ``tests`` resolves to the
    repo-root ``tests/`` package, not ``backend/tests/``, so the
    dotted name does not exist. Loading by file path sidesteps the
    ambiguity entirely.
    """
    path = Path(__file__).with_name("test_provider_order_integration.py")
    spec = importlib.util.spec_from_file_location(
        "_test_provider_order_integration_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_src = _load_sibling()
_cleanup_tmp_files = _src._cleanup_tmp_files
_snapshot_tmp_settings = _src._snapshot_tmp_settings


def _make_settings_file() -> Path:
    """A file that matches the production glob exactly."""
    p = Path("/tmp") / f"subagent_settings_{uuid.uuid4().hex}.json"
    p.write_text('{"env": {}}', encoding="utf-8")
    return p


@pytest.fixture
def tmp_settings_janitor():
    """Remove any file this test created, pass or fail."""
    created: list = []
    yield created
    for p in created:
        try:
            p.unlink()
        except OSError:
            pass


def test_a_file_that_predates_the_run_survives(tmp_settings_janitor):
    """The incident, distilled: a live execution's file is not ours."""
    other_execution = _make_settings_file()
    tmp_settings_janitor.append(other_execution)

    # What a well-behaved test does: snapshot the world, then run.
    before = _snapshot_tmp_settings()
    assert other_execution in before, (
        "precondition: the foreign file must be visible to the snapshot"
    )

    ours = _make_settings_file()
    tmp_settings_janitor.append(ours)

    _cleanup_tmp_files({"settings_before": before})

    assert not ours.exists(), "the test's own file must be cleaned up"
    assert other_execution.exists(), (
        "a settings file belonging to a live execution was deleted — this is "
        "exactly the 2026-09-23 cross-process kill switch, and it strands "
        "whatever plan owns that file"
    )


def test_a_missing_snapshot_deletes_nothing(tmp_settings_janitor):
    """Fail toward a leaked tmpfile, never toward a wider sweep.

    ``_cleanup_tmp_files`` is called from a ``finally`` block; if the run
    raised before the snapshot was taken, ``result`` carries no
    ``settings_before``. That must be a no-op — not "before is empty, so
    everything is ours".
    """
    survivor = _make_settings_file()
    tmp_settings_janitor.append(survivor)

    _cleanup_tmp_files({})

    assert survivor.exists(), (
        "an empty/missing snapshot must not be read as 'delete everything'"
    )
