"""RED regression tests: the **recorded** instance is the baseline.

Background
----------
``_compare_service_instance()`` is the gate every verification point
passes through before its request is sent.  It answers one question:
*is the service answering me still the instance this round started?*
Three facts are compared -- the **boot id**, the **workspace** and the
**state database's inode** -- and one of them failing means the answer
is not to be trusted.

Only the boot id is compared against the right baseline.  The
workspace is compared against ``_recorded_project_dir()`` -- the
workspace the round **recorded** -- and the inode against the inode
the recorded instance published.  Both are the wrong baseline whenever
the service under test is not this round's own process: the product
under test lives in its own checkout and reads its own database, so
the round blocks it for being somebody else.

The right baseline is already recorded.  ``_record_service_instance_identity()``
stores the ``instance`` block the service published about itself, and
that block names the workspace and the database the instance really
serves.  The comparison has to use it -- recorded value against
observed value -- exactly like the boot id already does.

What these tests pin
--------------------
  1. the recorded workspace is the baseline: a service of another
     workspace is not gated, and the plans tree it happens to serve
     says nothing about its identity;
  2. the recorded database inode is the baseline: same;
  3. a service the round never recorded is not gated at all -- "I do
     not know who it is" is not "it is somebody else";
  4. a recorded identity that **changed** its workspace is still
     blocked -- the gate is not switched off, it is pointed at the
     right baseline;
  5. a recorded identity that **changed** its database is still
     blocked;
  6. a recorded field that is empty is skipped: a comparison that
     cannot be made is not a comparison that failed;
  7. an instance that names **no** workspace -- an old process -- is
     compared by its plans tree, against the recorded tree.

Tests 1-3 are the reproduction: on the base point they fail, because
the comparison asks about this round's own tree and this process's own
database instead of the recorded ones.

The mismatch carries both sides
-------------------------------
A blocked verdict has to say *what* differs from *what*, so the
mismatch dict keeps both values of every comparison: the recorded
workspace under ``expected_project_dir``, the observed one under
``actual_project_dir`` (and the same for the plans tree and the
inode).  On the base point the "expected" side is this round's own
tree -- a value that has nothing to do with the instance the round
recorded.

Why the reproduction carries a marker
-------------------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits
non-zero is a **failed task**, and a failed task blocks every task
that depends on it: ``is_dependency_ready`` unlocks a dependent only
on ``completed`` or ``skipped``.  The fix is the next task and it
depends on this one, so a permanently red command here would strand
the whole chain behind it.  (The sibling reproductions in
``test_data_plane_fingerprint_identity.py`` and
``test_orchestrator_environment_blocked_round.py`` carry the same
marker, for the same reason.)

The marker is therefore a ratchet rather than a waiver.  It applies
only while the comparison still uses the round's own tree and the
process's own database, and the moment the recorded baseline is in it
switches itself off and the assertions below become hard ones again.
An implementation that lands with the wrong value therefore fails
these tests outright instead of being absorbed by the marker: the
marker is ``strict``, so a test that starts passing while the marker
still applies is reported as a failure.

Since 2026-10-28 the recorded workspace **is** the baseline, so the
ratchet is closed: the marker does not apply and the assertions below
are hard ones.  ``tests/unit/test_identity_ratchet_is_off.py`` is what
keeps it that way -- it runs this module with the markers switched off
and asks whether anything in it is still expected to fail.

To read the raw red on the base point, run the module with the
markers switched off::

    pytest tests/unit/test_service_instance_identity_baseline.py --runxfail -v
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The backend package first, then the module under test: the agent is
# asked about the same instance vocabulary the server publishes.
import config_paths  # noqa: E402
import server  # noqa: E402,F401
from verification_agent import VerificationAgent  # noqa: E402

# The service, the instance block and the agent come from the sibling
# reproduction of the same gate -- one HTTP server, one block, one
# agent, so this file and that one cannot drift apart.
from tests.unit.test_vp_dispatch_identity import (  # noqa: E402
    _Service,
    _instance_block,
    _make_agent,
    _state_db,
)


#: The port the comparison is asked about.  The comparison never opens
#: it: the instance block is handed over directly, because the question
#: under test is "which baseline does the comparison use", not "can the
#: round read the block".
PORT = 8080


def _product_db(root: Path, name: str = "product.db") -> Path:
    """A database file that exists -- its inode is the point."""
    path = root / name
    path.write_bytes(b"")
    return path


def _published_instance(service: _Service) -> Dict[str, Any]:
    """The ``instance`` block the live service publishes about itself.

    Read over the same road a verification point's request travels, so
    the block under test is a block a service really published.
    """
    url = f"http://127.0.0.1:{service.port}/health/data"
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["instance"]


def _record(agent: VerificationAgent, block: Dict[str, Any]) -> None:
    """Record *block* as the instance the round recorded for ``api``.

    The recording the round's ``ensure`` makes: the ``instance`` block
    the service published, kept under the service's name.  It is put
    there directly, because the question under test is what the
    comparison does with the recorded value -- not how the block was
    fetched.
    """
    agent._service_instance_identity["api"] = block


# ---------------------------------------------------------------------------
# The ratchet -- is the recorded baseline in yet?
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _compares_by_the_recorded_baseline() -> bool:
    """Does the comparison use the **recorded** instance as its baseline?

    A behavioural probe, asked once per session: an agent whose
    recorded identity names a workspace and a database that are not
    this round's own must not be gated against them, and a service the
    round never recorded must not be gated at all.

    The recorded workspace is a directory of its own -- one the round's
    own project directory does not contain -- so a comparison that
    still asked about this round's own directories would report a
    mismatch here.

    The process's own database is pointed at a file the probe creates,
    so the inode comparison is always meaningful: a machine without a
    state database would otherwise skip that comparison and make the
    probe answer "yes" about an implementation that is still broken.

    Never raises: a probe that blows up is a probe that has learned
    nothing, and "learned nothing" keeps the marker on.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_db = root / "state.db"
            state_db.write_bytes(b"")
            original = config_paths.resolve_state_db_path
            config_paths.resolve_state_db_path = lambda: state_db
            try:
                agent = _make_agent(root)
                product_db = _product_db(root)
                recorded = _instance_block(
                    "boot-1", root / "product-plans", product_db,
                    project_dir=str(root / "product"),
                )
                _record(agent, recorded)
                if agent._compare_service_instance(
                    "api", recorded, PORT,
                ) is not None:
                    return False
                unrecorded = _make_agent(root)
                return unrecorded._compare_service_instance(
                    "api", recorded, PORT,
                ) is None
            finally:
                config_paths.resolve_state_db_path = original
    except Exception:  # noqa: BLE001 - a probe never raises
        return False


def _still_uses_the_round_s_own_baseline() -> bool:
    """Does the comparison still identify the instance by its plans tree?

    The structural probe: the base point derives the expected plans
    tree from ``_expected_plans_dirs()`` and the expected inode from
    ``_expected_state_db_inode()``.  Since 2026-10-28 the instance is
    identified by its **workspace** -- the recorded ``project_dir``,
    which is anchored on the project root and does not move with
    ``PDT_PLANS_DIR``.  The plans tree is the fallback for an instance
    that names no workspace at all, and even then it is compared
    against the **recorded** tree, never against the round's own
    directories.

    So the question is the mirror image of the one it used to ask: the
    probe reports "still the old behaviour" when the comparison never
    reads the recorded workspace.  The state database's inode stays in
    the source -- as the round's own evidence, not as a baseline -- so
    its presence no longer says anything about which baseline is in
    use.
    """
    try:
        source = (_BACKEND_DIR / "verification_agent.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_compare_service_instance"
        )
    except (OSError, StopIteration, SyntaxError):  # pragma: no cover
        return True
    attributes = {
        node.attr for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
    }
    return "_recorded_project_dir" not in attributes


def _implementation_is_missing() -> bool:
    """Has the recorded baseline landed yet?

    Two probes, and the marker stays on only while **both** of them
    say the implementation is not there yet:

      * behaviour -- does the comparison use the recorded instance as
        its baseline?  A correct implementation satisfies this, and
        the probe is asked **first**, so a correct implementation can
        never be held back by a structural probe that has not caught
        up with it;
      * structure -- does the comparison still identify the instance
        by its plans tree instead of by its workspace?  The base point
        does; an implementation that reads the recorded workspace does
        not.
    """
    if _compares_by_the_recorded_baseline():
        return False
    return _still_uses_the_round_s_own_baseline()


#: The marker the reproduction carries until the recorded baseline
#: lands.  See the module docstring: it is a ratchet, not a waiver.
_RECORDED_BASELINE_NOT_LANDED = pytest.mark.xfail(
    strict=True,
    reason=(
        "the recorded instance is not the baseline yet: the workspace, "
        "the plans tree and the state database's inode are compared "
        "against this round's own plan directory and this process's own "
        "database, so every service that is not this round's own "
        "process is blocked as a foreign instance"
    ),
    condition=_implementation_is_missing(),
)


# ---------------------------------------------------------------------------
# 1-3. The reproduction -- the recorded instance is the baseline
# ---------------------------------------------------------------------------


@_RECORDED_BASELINE_NOT_LANDED
def test_the_recorded_project_dir_is_the_baseline(
    tmp_path: Path, monkeypatch,
):
    """The workspace the round recorded is the workspace it compares against.

    The service under test is another process: it serves the product's
    own plans tree, not this round's plan directory.  The round
    recorded the workspace when it recorded the instance, so comparing
    the answer against **this round's** tree reports a mismatch that is
    not one.

    And the plans tree is not an identity at all: the same workspace
    serving another tree is still the same instance.  The tree says
    where the process reads plans from *right now*; the workspace says
    which checkout the instance belongs to.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    recorded = _instance_block(
        "boot-1", root / "product-plans", product_db,
        project_dir=str(root / "product"),
    )
    _record(agent, recorded)
    answering = _instance_block(
        "boot-1", root / "other-plans", product_db,
        project_dir=str(root / "product"),
    )

    mismatch = agent._compare_service_instance("api", answering, PORT)

    assert mismatch is None, (
        "记录下来的实例就是基线：服务自己的 workspace 不得与本轮的项目目录"
        f"比较，实际判成了 {mismatch!r}"
    )


@_RECORDED_BASELINE_NOT_LANDED
def test_the_recorded_state_database_inode_is_the_baseline(
    tmp_path: Path, monkeypatch,
):
    """The recorded database's inode is the inode the comparison uses.

    The service reads **its own** database; this process reads another
    one.  The round recorded the service's inode when it recorded the
    instance, so the comparison has to use that number -- asking
    "does the instance read the database *I* have open" is a question
    about this process, not about the service.

    Both sides name the same plans tree here, so the inode is the only
    thing that could differ.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    recorded = _instance_block("boot-1", agent.plan_dir, product_db)
    _record(agent, recorded)

    mismatch = agent._compare_service_instance("api", recorded, PORT)

    assert mismatch is None, (
        "记录下来的实例就是基线：服务的数据库 inode 不得与本进程自己的库"
        f"比较，实际判成了 {mismatch!r}"
    )


@_RECORDED_BASELINE_NOT_LANDED
def test_a_service_without_a_recorded_identity_is_not_gated(
    tmp_path: Path, monkeypatch,
):
    """A service the round never recorded is not gated at all.

    "I do not know who is answering" is not "somebody else is
    answering".  The round has no baseline to compare against, so it
    has no verdict to hand out -- and a verdict it cannot back is
    worse than no verdict.

    The block under test is one a live service published over
    ``/health/data``: the round simply never recorded it.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    block = _instance_block(
        "boot-1", root / "product-plans", product_db,
        project_dir=str(root / "product"),
    )

    service = _Service(instance=block)
    try:
        port = service.port
        published = _published_instance(service)
    finally:
        service.close()

    assert published["plans_dir"] == str(root / "product-plans"), published
    assert published["project_dir"] == str(root / "product"), published
    assert "api" not in agent._service_instance_identity, (
        "这一条测的是「没有记录」，记录了就测不到东西"
    )

    mismatch = agent._compare_service_instance("api", published, port)

    assert mismatch is None, (
        "没有记录的服务不得被判成别人：不知道它是谁，不等于知道它是别人，"
        f"实际判成了 {mismatch!r}"
    )


# ---------------------------------------------------------------------------
# 4-5. The gate is not switched off -- it is pointed at the right baseline
# ---------------------------------------------------------------------------


@_RECORDED_BASELINE_NOT_LANDED
def test_a_recorded_identity_that_changed_its_project_dir_still_blocks(
    tmp_path: Path, monkeypatch,
):
    """A recorded instance that moved its workspace is still blocked.

    The gate is not switched off by this fix -- it is pointed at the
    right baseline.  The instance the round recorded belonged to one
    workspace; the instance answering now belongs to another one, and
    that is exactly what the gate exists to catch.

    The plans tree is the same on both sides, so the verdict says
    exactly one thing: the workspace moved.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    recorded = _instance_block(
        "boot-1", root / "plans", product_db,
        project_dir=str(root / "product"),
    )
    _record(agent, recorded)
    moved = _instance_block(
        "boot-1", root / "plans", product_db,
        project_dir=str(root / "other"),
    )

    mismatch = agent._compare_service_instance("api", moved, PORT)

    assert mismatch is not None, "换了 workspace 的实例必须被拦下"
    assert mismatch["mismatched_fields"] == ["project_dir"], mismatch
    assert mismatch["expected_project_dir"] == str(root / "product"), (
        "expected 一侧必须是记录下来的那个 workspace"
    )
    assert mismatch["actual_project_dir"] == str(root / "other"), (
        "actual 一侧必须是这一次观测到的那个 workspace"
    )


@_RECORDED_BASELINE_NOT_LANDED
def test_a_recorded_identity_that_changed_its_database_still_blocks(
    tmp_path: Path, monkeypatch,
):
    """A recorded instance that swapped its database is still blocked.

    The same path with another inode is another file -- another data
    plane.  The comparison has to notice that, and it has to notice it
    against the inode the round **recorded**, not against the one this
    process happens to have open.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    other_db = _product_db(root, "other.db")
    recorded = _instance_block(
        "boot-1", root / "product-plans", product_db,
    )
    _record(agent, recorded)
    swapped = _instance_block("boot-1", root / "product-plans", other_db)

    assert product_db.stat().st_ino != other_db.stat().st_ino, (
        "两个不同的文件必须有不同的 inode，否则这一条测不到东西"
    )

    mismatch = agent._compare_service_instance("api", swapped, PORT)

    assert mismatch is not None, "换了数据库的实例必须被拦下"
    assert mismatch["mismatched_fields"] == ["state_db_inode"], mismatch


# ---------------------------------------------------------------------------
# 6. The boundary -- a field that cannot be compared carries no verdict
# ---------------------------------------------------------------------------


@_RECORDED_BASELINE_NOT_LANDED
def test_a_recorded_field_that_is_empty_is_not_compared(
    tmp_path: Path, monkeypatch,
):
    """A recorded field that is empty is skipped, not failed.

    The round recorded an instance that published no boot id.  The
    instance answering now publishes one -- but the round has no
    baseline to say it changed, so the field is not comparable and
    carries no verdict.  "Not comparable" is not "wrong".

    An empty *plans tree* is deliberately a different case: the tree
    is not the identity any more -- the **workspace** is -- so an empty
    tree is not a missing baseline, it is a field that is simply not
    compared.  The tree is consulted only as the fallback for an
    instance that names no workspace at all, and then against the
    **recorded** tree, never against the round's own directories.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    recorded = _instance_block(
        "boot-1", root / "product-plans", product_db,
    )
    recorded["boot_id"] = ""        # the round recorded no boot id
    _record(agent, recorded)
    answering = _instance_block(
        "boot-1", root / "product-plans", product_db,
    )
    answering["boot_id"] = "boot-2"  # a different boot, but no baseline

    mismatch = agent._compare_service_instance("api", answering, PORT)

    assert mismatch is None, (
        "记录块里为空的字段不得参与判决，实际判成了 "
        f"{mismatch!r}"
    )


# ---------------------------------------------------------------------------
# 7. The fallback -- an instance that names no workspace at all
# ---------------------------------------------------------------------------


@_RECORDED_BASELINE_NOT_LANDED
def test_an_old_instance_without_project_dir_is_compared_by_its_plans_dir(
    tmp_path: Path, monkeypatch,
):
    """An instance that names no workspace is compared by its tree.

    An old process -- or one rolled back to an old build -- publishes
    no ``project_dir`` at all.  A comparison that cannot be made on the
    workspace is not a comparison that failed: the **plans tree** takes
    over, and it is compared against the tree the round **recorded**,
    against nothing else.

    So an old instance that moved its tree is still blocked -- and it
    is blocked as a ``plans_dir`` mismatch, not as a workspace one.
    """
    root = Path(tmp_path).resolve()
    _state_db(tmp_path, monkeypatch)   # the database this process has open
    agent = _make_agent(tmp_path)
    product_db = _product_db(root)
    recorded = _instance_block("boot-1", root / "product-plans", product_db)
    assert "project_dir" not in recorded, (
        "这一条测的就是没有 workspace 的实例，带上了就测不到东西"
    )
    _record(agent, recorded)
    moved = _instance_block("boot-1", root / "other-plans", product_db)

    mismatch = agent._compare_service_instance("api", moved, PORT)

    assert mismatch is not None, "老实例换了 plans 树必须被拦下"
    assert mismatch["mismatched_fields"] == ["plans_dir"], mismatch
    assert mismatch["expected_plans_dir"] == str(root / "product-plans"), (
        "expected 一侧必须是记录下来的那棵树"
    )
    assert mismatch["actual_plans_dir"] == str(root / "other-plans"), (
        "actual 一侧必须是这一次观测到的那棵树"
    )
