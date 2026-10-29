"""Dispatch-level regression: the instance identity is compared on the **workspace**.

Background
----------
Every verification point passes through one gate before its request is
sent: the instance that answered must be the instance the round
recorded.  Since 2026-10-28 the third comparison of that gate is the
**workspace** -- the ``project_dir`` the recorded instance named about
itself, the repository root it belongs to.

The plans tree is not an identity.  ``plans_dir`` says "which tree is
this process pointed at **right now**": it is relocatable through
``PDT_PLANS_DIR``, and the per-test fixture redirects it on every
single test.  Two processes of the same checkout -- one of them
serving a relocated tree -- are the same instance, and a gate that
calls them strangers stops the round from asking anything at all.

The unit-level rules live in
``tests/unit/test_instance_identity_project_dir.py``.  This file pins
the same rules at the **dispatch** level -- the request really goes
out, or really does not -- because that is where a wrong baseline
costs a round its verification points, and because the dispatch is the
place the verdict is handed to the report.

What these tests pin
--------------------
  1. the same workspace with another plans tree is **dispatched**: the
     instance is the same instance, so the request goes out and the
     verdict is about the product;
  2. an instance that publishes no workspace at all -- an old process,
     or one rolled back to an old build -- falls back to the plans
     tree, still against the **recorded** tree, so a changed tree is
     still blocked and the request still does not go out;
  3. the two dispatch regression files that already existed stay green:
     the semantics of the gate changed under them, and a change of
     semantics that is not mirrored in the files that own it is a
     change nobody notices until a round stops dispatching.

Test 3 is the reason this file exists as a separate one: the test
command names this file only, so the regressions of the two files it
guards are run here, in a subprocess, by name.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

import pytest


#: The backend directory -- the root of the package under test and the
#: working directory the guarded regressions are run in.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# The service, the instance block, the round, the dispatch spy and the
# log reader come from the sibling file that owns them -- one HTTP
# server, one block, one agent, so this file and that one cannot drift
# apart.
from tests.unit.test_vp_dispatch_identity import (  # noqa: E402
    _Service,
    _instance_block,
    _mismatch_events,
    _prepare,
    _run_all_vps,
    _spy_api_runner,
    _state_db,
)


#: The two files whose regressions this file guards.  They are named
#: one by one -- a directory would collect this file as well and the
#: subprocess would run itself.
_GUARDED_REGRESSION_FILES = (
    "tests/unit/test_vp_dispatch_identity.py",
    "tests/unit/test_service_instance_dispatch_baseline.py",
)


def _product_db(root: Path, name: str = "product.db") -> Path:
    """The database the service under test reads -- with a real inode."""
    path = root / name
    path.write_bytes(b"")
    return path


# ---------------------------------------------------------------------------
# 1. The same workspace with another plans tree → the request goes out
# ---------------------------------------------------------------------------


def test_a_fixture_plans_dir_does_not_block_a_recorded_project_dir_instance(
    tmp_path: Path, monkeypatch,
):
    """The instance kept its workspace and moved its tree → still ours.

    This is the shape the change buys.  The service under test is a
    different process of the **same** checkout, and the plan's
    ``start_cmd`` pointed its plans tree at a fixture directory.  The
    workspace is what the instance *is*; the tree is where it happens
    to read plans from right now.  The gate must not turn that into
    "somebody else", or the round never asks anything.
    """
    service = _Service()
    try:
        _state_db(tmp_path, monkeypatch)     # this process's own db
        product_db = _product_db(tmp_path)   # the service's own db
        agent, plan = _prepare(tmp_path, monkeypatch, service)
        workspace = tmp_path / "product"

        # The round records the instance: the workspace, and the tree
        # the service serves right now.
        service.publish(_instance_block(
            "boot-1", tmp_path / "product-plans", product_db,
            project_dir=str(workspace),
        ))
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        agent._preflight_service_freshness(plan)

        # ... and the instance is still the same instance: same
        # workspace, another tree.
        service.publish(_instance_block(
            "boot-1", tmp_path / "fixture" / "plans", product_db,
            project_dir=str(workspace),
        ))

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert str(agent.project_dir) != str(workspace), (
        "被测实例的 workspace 必须不是本轮自己的 workspace，"
        "否则这条用例测不到「按记录值比较」这条路径"
    )
    assert dispatched == ["VP-001"], (
        "同一个 workspace 换了 plans 树不得被拦下，实际派发的是 "
        f"{dispatched!r}"
    )
    assert results[0]["status"] == "PASSED", results[0]
    assert _mismatch_events(agent) == []


# ---------------------------------------------------------------------------
# 2. An instance without a workspace falls back to the recorded tree
# ---------------------------------------------------------------------------


def test_an_old_instance_without_project_dir_still_blocks_on_a_changed_plans_dir(
    tmp_path: Path, monkeypatch,
):
    """An old process publishes no workspace → the plans tree takes over.

    A comparison that cannot be made on the workspace is not a
    comparison that failed.  The fallback is the **recorded** tree --
    never the round's own directories -- so an old instance that moved
    its tree is still a stranger and the request still does not go out.
    """
    service = _Service()
    try:
        _state_db(tmp_path, monkeypatch)     # this process's own db
        product_db = _product_db(tmp_path)   # the service's own db
        agent, plan = _prepare(tmp_path, monkeypatch, service)

        # The round records an old instance: no workspace at all.
        service.publish(_instance_block(
            "boot-1", tmp_path / "product-plans", product_db,
        ))
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        agent._preflight_service_freshness(plan)

        # ... and then the instance behind the port moved to another tree.
        service.publish(_instance_block(
            "boot-1", tmp_path / "other-plans", product_db,
        ))

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == [], "换了 plans 树的实例不得收到请求"
    assert results[0]["status"] == "BLOCKED", results[0]
    assert results[0]["status"] != "FAILED", results[0]
    evidence = results[0]["evidence"]
    assert evidence["error_kind"] == "service_instance_mismatch", evidence
    assert evidence["mismatched_fields"] == ["plans_dir"], (
        "没有 workspace 可比时，回退的比较必须落在 plans_dir 上，实际是 "
        f"{evidence['mismatched_fields']!r}"
    )
    assert Path(evidence["expected_plans_dir"]) == (
        tmp_path / "product-plans"
    ).resolve(), (
        "expected 一侧必须是记录下来的那棵树，而不是本轮自己的目录："
        f"{evidence['expected_plans_dir']!r}"
    )

    events = _mismatch_events(agent)
    assert len(events) == 1, events
    assert events[0]["data"]["mismatched_fields"] == ["plans_dir"], events[0]


# ---------------------------------------------------------------------------
# 3. The dispatch regressions that already existed stay green
# ---------------------------------------------------------------------------


def test_the_existing_dispatch_regressions_stay_green():
    """The two dispatch regression files are still green.

    The gate's semantics changed under them -- the plans tree is no
    longer the third comparison, and the round's own directories are no
    longer a baseline -- so the files that own those semantics have to
    be updated **and** stay green.  They are run here by name, in a
    subprocess, so a regression in either of them fails in this file
    even when this file is the only one the test command names.
    """
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", *_GUARDED_REGRESSION_FILES,
        ],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert completed.returncode == 0, (
        "既有 dispatch 回归必须是绿的：\n"
        f"returncode={completed.returncode}\n"
        "--- stdout ---\n"
        f"{completed.stdout[-4000:]}\n"
        "--- stderr ---\n"
        f"{completed.stderr[-2000:]}"
    )
