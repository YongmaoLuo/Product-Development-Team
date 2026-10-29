"""The **workspace** is the third comparison of the instance identity.

Background
----------
``_compare_service_instance()`` is the gate every verification point
passes through before its request is sent.  It answers one question:
*is the service answering me still the instance this round recorded?*
Three facts are compared -- the **boot id**, the **plans tree** and the
**state database's inode**.

The plans tree was the wrong third fact.  ``plans_dir`` is not an
identity: it says "which tree is this process pointed at **right
now**".  It is derived from the project root, it can be overridden on
its own (``PDT_PLANS_DIR``), and the per-test fixture redirects it on
every single test.  So two processes of the same checkout -- one of them
serving a relocated plans tree -- published the same "identity" for the
workspace while differing in a field that describes a *configuration*,
not a *workspace*.

``project_dir`` is the field that names the workspace: the repository
root the process belongs to, anchored on
:data:`config_paths.PROJECT_ROOT`, which is resolved once at import
time and is NOT affected by the plans-dir override.

What these tests pin
--------------------
  1. the same workspace with another plans tree is **not** a mismatch --
     this is the capability the change buys;
  2. another workspace **is** a mismatch, and the mismatch names both
     sides;
  3. an instance that publishes no workspace at all (an old process)
     falls back to the plans tree -- against the **recorded** tree;
  4. a round that recorded **no** tree judges nothing: neither the
     round's own tree nor a third party's tree is a verdict, because
     there is no baseline to compare against.

The comparison is always **recorded value against observed value**.
The round's own directories are not a baseline and are not accepted
either -- they are not what the recorded instance declared.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The service, the instance block and the agent come from the sibling
# reproduction of the same gate -- one HTTP server, one block, one
# agent, so this file and that one cannot drift apart.
from tests.unit.test_vp_dispatch_identity import (  # noqa: E402
    _instance_block,
    _make_agent,
)


#: The port the comparison is asked about.  The comparison never opens
#: it: the instance block is handed over directly, because the question
#: under test is "which baseline does the comparison use", not "can the
#: round read the block".
PORT = 8080


def _database(root: Path, name: str = "product.db") -> Path:
    """A database file that exists -- its inode is the point."""
    path = root / name
    path.write_bytes(b"")
    return path


def _recorded(agent, block: Dict[str, Any]) -> None:
    """Record *block* as the instance the round recorded for ``api``.

    The recording the round's ``ensure`` makes: the ``instance`` block
    the service published, kept under the service's name.  It is put
    there directly, because the question under test is what the
    comparison does with the recorded value -- not how the block was
    fetched.
    """
    agent._service_instance_identity["api"] = block


def test_same_project_dir_with_a_different_plans_dir_is_not_a_mismatch(
    tmp_path: Path,
):
    """The same workspace, another plans tree -- still the same instance.

    This is the shape the change buys: the service serves a relocated
    plans tree (``PDT_PLANS_DIR`` pointed it there) and the round must
    not call it a stranger.  The workspace is what the instance is
    identified by; the plans tree is where it happens to read plans
    from right now.
    """
    root = Path(tmp_path).resolve()
    agent = _make_agent(tmp_path)
    database = _database(root)
    workspace = root / "workspace"
    recorded = _instance_block(
        "boot-1", root / "product-plans", database,
        project_dir=str(workspace),
    )
    _recorded(agent, recorded)
    answering = _instance_block(
        "boot-1", root / "fixture-plans", database,
        project_dir=str(workspace),
    )

    mismatch = agent._compare_service_instance("api", answering, PORT)

    assert mismatch is None, (
        "同一个 workspace 里换了 plans 树不得判成别人，实际判成了 "
        f"{mismatch!r}"
    )


def test_a_different_project_dir_is_a_mismatch(tmp_path: Path):
    """Another workspace is another instance -- and the names are kept.

    The block has to say *what* differs from *what*: the workspace the
    round recorded, and the workspace that answered.
    """
    root = Path(tmp_path).resolve()
    agent = _make_agent(tmp_path)
    database = _database(root)
    recorded = _instance_block(
        "boot-1", root / "product-plans", database,
        project_dir=str(root / "workspace-a"),
    )
    _recorded(agent, recorded)
    answering = _instance_block(
        "boot-1", root / "product-plans", database,
        project_dir=str(root / "workspace-b"),
    )

    mismatch = agent._compare_service_instance("api", answering, PORT)

    assert mismatch is not None, "别的 workspace 不得被放过"
    assert mismatch["mismatched_fields"] == ["project_dir"], mismatch
    assert mismatch["expected_project_dir"] == str(root / "workspace-a"), (
        "expected 一侧必须是记录下来的那个 workspace"
    )
    assert mismatch["actual_project_dir"] == str(root / "workspace-b"), (
        "actual 一侧必须是这一次观测到的那个 workspace"
    )


def test_an_old_instance_without_project_dir_falls_back_to_the_recorded_plans_dir(
    tmp_path: Path,
):
    """An instance that publishes no workspace falls back to the tree.

    An old process (or a rolled-back one) answers without a
    ``project_dir``.  The comparison cannot be made on the workspace,
    so it falls back to the plans tree -- and still against the
    **recorded** tree only, never against the round's own directories.
    """
    root = Path(tmp_path).resolve()
    agent = _make_agent(tmp_path)
    database = _database(root)
    recorded = _instance_block("boot-1", root / "product-plans", database)
    assert "project_dir" not in recorded
    _recorded(agent, recorded)
    answering = _instance_block("boot-1", root / "other-plans", database)

    mismatch = agent._compare_service_instance("api", answering, PORT)

    assert mismatch is not None, "老实例换了 plans 树必须被拦下"
    assert mismatch["mismatched_fields"] == ["plans_dir"], mismatch
    assert mismatch["expected_plans_dir"] == str(root / "product-plans"), (
        "expected 一侧必须是记录下来的那棵树"
    )
    assert mismatch["actual_plans_dir"] == str(root / "other-plans"), (
        "actual 一侧必须是这一次观测到的那棵树"
    )


def test_an_answering_instance_without_project_dir_falls_back_to_the_recorded_plans_dir(
    tmp_path: Path,
):
    """The other half of the fallback: the **answering** side is old.

    The recorded instance is a modern one and names its workspace; the
    process that answers is a rolled-back old build and names none.  A
    comparison that cannot be made on the workspace is not a comparison
    that failed -- the plans tree takes over, still against the
    recorded tree.
    """
    root = Path(tmp_path).resolve()
    agent = _make_agent(tmp_path)
    database = _database(root)
    recorded = _instance_block(
        "boot-1", root / "product-plans", database,
        project_dir=str(root / "workspace"),
    )
    _recorded(agent, recorded)
    answering = _instance_block("boot-1", root / "other-plans", database)
    assert "project_dir" not in answering

    mismatch = agent._compare_service_instance("api", answering, PORT)

    assert mismatch is not None, "老进程换了 plans 树必须被拦下"
    assert mismatch["mismatched_fields"] == ["plans_dir"], mismatch
    assert mismatch["expected_plans_dir"] == str(root / "product-plans"), (
        "expected 一侧必须是记录下来的那棵树"
    )
    assert mismatch["actual_plans_dir"] == str(root / "other-plans"), (
        "actual 一侧必须是这一次观测到的那棵树"
    )


def test_the_rounds_own_tree_is_not_accepted_when_no_tree_was_recorded(
    tmp_path: Path,
):
    """No recorded tree -- the round judges nothing.

    The round's own directories used to be appended to the expectation,
    so a service serving the round's own tree passed and a service
    serving a third tree was blocked.  With the recorded value as the
    only baseline, a round that recorded **no** tree has no baseline at
    all: neither the round's own tree nor anybody else's is a verdict.

    "Not comparable" is not "wrong".
    """
    agent = _make_agent(tmp_path)
    _recorded(agent, {"boot_id": "boot-1"})

    own_tree = {
        "boot_id": "boot-1",
        "plans_dir": str(agent.plan_dir.parent),
    }
    assert agent._compare_service_instance("api", own_tree, PORT) is None, (
        "没有记录树时，本轮自己的树不再是可接受的基线"
    )

    third_tree = {
        "boot_id": "boot-1",
        "plans_dir": str(Path(tmp_path) / "a-third-tree" / "plans"),
    }
    assert agent._compare_service_instance("api", third_tree, PORT) is None, (
        "没有记录树时，没有基线可比，谁都不是判决的对象"
    )


def test_the_verdict_evidence_carries_both_workspaces(tmp_path: Path):
    """A blocked verdict says *what* differs from *what*.

    The workspace comparison is the third one, so the evidence of the
    blocked verdict has to name both sides of it — otherwise a reader
    of the round's report sees "the service answered as another
    instance" and cannot tell **which** instance it was.
    """
    root = Path(tmp_path).resolve()
    agent = _make_agent(tmp_path)
    database = _database(root)
    recorded = _instance_block(
        "boot-1", root / "product-plans", database,
        project_dir=str(root / "workspace-a"),
    )
    _recorded(agent, recorded)
    answering = _instance_block(
        "boot-1", root / "product-plans", database,
        project_dir=str(root / "workspace-b"),
    )

    mismatch = agent._compare_service_instance("api", answering, PORT)
    assert mismatch is not None

    verdict = agent._build_service_instance_mismatch_verdict(
        "VP-001", mismatch,
    )

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict["evidence"]
    assert evidence["expected_project_dir"] == str(root / "workspace-a"), (
        f"the evidence does not name the workspace the round recorded: "
        f"{evidence!r}"
    )
    assert evidence["actual_project_dir"] == str(root / "workspace-b"), (
        f"the evidence does not name the workspace that answered: "
        f"{evidence!r}"
    )
    assert evidence["mismatched_fields"] == ["project_dir"], evidence
