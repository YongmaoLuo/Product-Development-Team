"""The ``project_dir`` field of the ``/health/data`` instance block.

Backstory
---------
The instance block (:func:`server._instance_identity`) publishes the
paths this process is serving RIGHT NOW, so a reader can tell "this
instance" from "somebody else's instance on the same port".  Until
2026-10-28 it published ``plans_dir`` and nothing else about the tree
— and ``plans_dir`` is the wrong name for the workspace, because it is
relocatable: ``PDT_PLANS_DIR`` points it somewhere else, and the
per-test fixture redirects it on every single test.

So a comparison of two ``/health/data`` answers could not answer the
question it was asked.  Two instances of the same checkout, one of them
serving a relocated plans tree, published the same "identity" for the
workspace while differing in a field that describes a *configuration*,
not a *workspace*.

``project_dir`` is the field that names the workspace: the repository
root this process belongs to, anchored on
:data:`config_paths.PROJECT_ROOT`, which is resolved once at import
time and is NOT affected by ``PDT_PLANS_DIR``.  It is published
alongside ``plans_dir``, not instead of it — ``plans_dir`` keeps its
own meaning (the plans tree this process reads and writes), and the
two together say "this workspace, serving this tree".

These tests pin three things: the field is published and equals the
resolved project root, it does not move when the plans tree is
relocated, and the pre-existing ``plans_dir`` field is still there.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config_paths  # noqa: E402
import server  # noqa: E402


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app)


def _instance() -> dict:
    """The ``instance`` block a ``GET /health/data`` answers with."""
    resp = _client().get("/health/data")
    assert resp.status_code == 200, resp.text
    return resp.json()["instance"]


def test_health_data_publishes_project_dir_equal_to_project_root():
    """The published workspace is the repository root, resolved.

    Not "some path that looks like it" — the very value
    :data:`config_paths.PROJECT_ROOT` names, resolved, so a reader can
    compare it against its own root character for character.
    """
    instance = _instance()

    expected = str(Path(config_paths.PROJECT_ROOT).resolve())
    assert instance["project_dir"] == expected
    # An absolute path, so it can be compared against another absolute
    # path without knowing either side's working directory.
    assert Path(instance["project_dir"]).is_absolute()


def test_project_dir_is_independent_of_pdt_plans_dir_override(monkeypatch, tmp_path):
    """Relocating the plans tree must not move the workspace.

    ``PDT_PLANS_DIR`` (and the per-test fixture that redirects
    ``server.PLANS_DIR`` the same way) relocates the *plans tree* — the
    directory this process reads plan artifacts from.  It says nothing
    about which checkout this process belongs to, so a field that
    followed the override would name a configuration, not a workspace.
    """
    before = _instance()

    fixture_plans = tmp_path / "fixture-plans"
    monkeypatch.setattr(server, "PLANS_DIR", fixture_plans)

    after = _instance()

    # The plans tree moved...
    assert after["plans_dir"] == str(Path(fixture_plans))
    assert after["plans_dir"] != before["plans_dir"]
    # ...the workspace did not.
    assert after["project_dir"] == before["project_dir"]
    assert after["project_dir"] == str(Path(config_paths.PROJECT_ROOT).resolve())


def test_plans_dir_is_still_published():
    """``project_dir`` is additive: the pre-existing field survives.

    The block is a fact sheet, and the plans tree is a fact about this
    process that a reader still needs — the two fields describe two
    different things, and dropping either would lose information.
    """
    instance = _instance()

    assert "plans_dir" in instance
    assert instance["plans_dir"]
    assert "project_dir" in instance
    assert instance["project_dir"]
