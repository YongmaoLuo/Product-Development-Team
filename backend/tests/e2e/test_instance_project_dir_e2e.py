"""端到端验收：**真**后端进程下，实例身份按 ``project_dir`` 认人。

背景
----
单测（``tests/unit/test_service_instance_identity_baseline.py``）用的是**合成**
的 ``instance`` 块：块是测试自己造的，比对的两侧也是测试自己捏的。它能证明
「比较函数按记录值比」，证明不了**生产形状**下这件事还成立。

生产形状是三个进程属性叠在一起：

* **一轮跑在一个进程里** —— 轮次的 agent（``verification_agent.Orchestrator``）
  在自己的进程里，持有自己的计划目录；
* **被测服务是另一个真实后端进程** —— ``backend.server`` 的真子进程，监听内核
  选的空闲端口，服务**它自己的**计划树、读**它自己的**状态库；
* **两者之间是真 HTTP** —— 轮次通过 ``/health/data`` 读那个进程自报的事实，
  没有任何网络替身。

这个文件要证明的正是第三个属性没有把前两个属性变成假的：**真**后端自报的计划树
是一个**测试夹具目录**（``<work>/plans``），和本轮自己的计划目录、和仓库根都
不是一回事 —— 而这样的实例**不得**被当成「别人」拦下。

三条身份语义（上游「验收要点」①、②、③）
----------------------------------------
① ``project_dir`` 是被发布的字段：真后端在 ``/health/data`` 的 ``instance`` 块里
   自报**仓库根**，并且这个值**独立于**它服务的计划树（``plans_dir``）。计划树是
   可搬家的（``PDT_PLANS_DIR`` 一搬就走），搬走了它就不再是身份；仓库根不是。
② 真记录之后，被记录的那一份**不判成 mismatch**：判据是「记录值 vs 现场值」，
   不是「本轮自己的目录 vs 现场值」。
③ 门没有被关掉，是被指到了正确的基线上：记录的 ``project_dir`` 变了 → 判成
   ``project_dir`` 不符；记录的那一份**压根没有** ``project_dir``（老进程 /
   回滚到旧构建）→ 回退到计划树，判成 ``plans_dir`` 不符。

不换模型
--------
唯一的替身是模型，而那是仓库自己留的缝：``PDT_LLM_MODE=scripted`` 把每一次
模型调用换成磁盘上的固定回答（``backend/coding_tool.py`` 的
``ScriptedLLMRegistry``）。不换的话，``POST /api/plan/import`` 会真的去调模型
—— 不可复现，还烧钱。

不许 skip
----------
服务起不来是**失败**（并把 ``server.log`` 的尾巴打出来），不是 skip：一条 skip
的退出码是 0，和「验证通过」没法区分。这个文件存在的意义就是产出证据。

防空过
------
第二条测试断言的不只是「``None``」。它同时断言记录里的 ``boot_id`` 就是真进程
自报的那个、``project_dir`` 非空、``state_db_inode`` 非空。不这么做的话，
「无记录不拦」（R2）会让 ``None`` 变得毫无信息量 —— 那**只**说明 R2 生效，
不说明「按记录值比」生效：这道题就空了。

负控
----
第三、四条测试把记录里的 ``project_dir`` 改掉（第四条还把它**删掉**），判据
必须回到 ``["project_dir"]`` / ``["plans_dir"]``：门不是被关掉了，是被指到了
正确的基线上。改的是**记录下来的那一侧** —— 现场回答的那一侧始终是真进程通过
真 HTTP 读回来的 live 块。

证据
----
``/tmp/instance-project-dir-e2e/mechanism.json`` 落的是这份验收的机制证据：
真进程发布的块、期望值、以及三条语义各自的判据结果。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The siblings are reused **as they are**: the real ``backend.server``
# subprocess, the scripted LLM, the git checkout, the one HTTP client
# the whole E2E lane speaks through, and the helpers that already
# know how to boot a real backend and fail (never skip) when it does
# not come up.
from tests.e2e.test_instance_identity_baseline_e2e import (  # noqa: E402
    _boot_or_fail,
    _import_plan,
    _live_instance_block,
    _round_agent,
    _server_port,
    _stop_server,
)
from tests.e2e.test_plan_lifecycle_ui_e2e import (  # noqa: E402
    _make_checkout,
    _write_scripted_llm,
)

import config_paths  # noqa: E402

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Where the mechanism evidence goes.
EVIDENCE_DIR = Path("/tmp/instance-project-dir-e2e")  # nosec B108
EVIDENCE_PATH = EVIDENCE_DIR / "mechanism.json"

#: The repository root the backend under test belongs to.  It is read
#: from the **same** module the server reads it from
#: (``config_paths.PROJECT_ROOT``), so the expectation cannot drift
#: away from what the server publishes by a typo in a path literal.
EXPECTED_PROJECT_DIR = str(Path(config_paths.PROJECT_ROOT).resolve())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _same_path(left: str, right: str) -> bool:
    """Do two paths name the same place?

    Resolved on both sides, because one of them may reach the same
    directory through a symlink (``/var`` → ``/private/var`` on macOS).
    """
    return Path(left).resolve() == Path(right).resolve()


def _write_evidence(payload: Dict[str, Any]) -> None:
    """Write the mechanism evidence where the reviewer can pick it up."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


#: The mechanism evidence, accumulated across the tests and written
#: out when the session's fixture tears the server down.
_EVIDENCE: Dict[str, Any] = {"expected": {}}


# ---------------------------------------------------------------------------
# The scene: a real backend process, serving a real plan
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def project_dir_backend(tmp_path_factory) -> Dict[str, Any]:
    """Boot the real backend once and hand the facts about it to the tests.

    The fixture **starts** the process: ``cwd`` is the repository root,
    the plans tree is a **fixture** directory under the work directory
    (``<work>/plans``), the state database is another fixture file, and
    the only thing that is replaced is the model (scripted).

    A backend that does not come up is an ``AssertionError`` carrying
    the tail of ``server.log`` — never a skip.
    """
    work = tmp_path_factory.mktemp("instance-project-dir-e2e")
    script_dir = work / "llm-script"
    script_dir.mkdir(parents=True, exist_ok=True)
    _write_scripted_llm(script_dir)
    checkout = _make_checkout(work)

    base_url, proc = _boot_or_fail(work, script_dir)
    try:
        # The import comes first: it is what makes the server **write**
        # its own state database, and a database that does not exist yet
        # is published without an inode -- the comparison would then have
        # one field less to make.
        plan_id = _import_plan(base_url, checkout)
        port = _server_port(base_url)
        block = _live_instance_block(port)

        _EVIDENCE["expected"] = {
            "project_dir": EXPECTED_PROJECT_DIR,
            "plans_dir": str((work / "plans").resolve()),
            "base_url": base_url,
            "port": port,
            "plan_id": plan_id,
            "checkout": str(checkout),
        }
        _EVIDENCE["published"] = block
        _write_evidence(_EVIDENCE)

        yield {
            "base_url": base_url,
            "work": work,
            "checkout": checkout,
            "plan_id": plan_id,
            "port": port,
        }
    finally:
        _stop_server(proc)
        _write_evidence(_EVIDENCE)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_the_real_backend_publishes_a_project_dir_independent_of_its_plans_dir(
    project_dir_backend,
):
    """The real backend names its workspace, and that name is not its tree.

    The instance block a live ``backend.server`` publishes carries two
    different facts:

      * ``project_dir`` — the repository root this process belongs to.
        It is anchored on ``config_paths.PROJECT_ROOT`` and does not
        move when the plans tree is relocated.
      * ``plans_dir`` — the plans root this process is serving RIGHT
        NOW.  The E2E lane points it at a **fixture** directory
        (``<work>/plans``), so the two must be different — otherwise
        this file would be testing a coincidence instead of a
        mechanism.
    """
    work = project_dir_backend["work"]
    port = project_dir_backend["port"]

    block = _live_instance_block(port)

    assert block.get("project_dir"), (
        f"真后端必须自报自己的 workspace，否则身份无从比较：{block!r}"
    )
    assert _same_path(str(block["project_dir"]), EXPECTED_PROJECT_DIR), (
        f"published project_dir {block['project_dir']!r} 必须是仓库根 "
        f"{EXPECTED_PROJECT_DIR!r}"
    )
    assert _same_path(str(block.get("plans_dir") or ""), str(work / "plans")), (
        f"published plans_dir {block.get('plans_dir')!r} 必须是夹具目录 "
        f"{str(work / 'plans')!r}"
    )
    assert not _same_path(
        str(block["project_dir"]), str(block.get("plans_dir") or "")
    ), (
        "workspace 与计划树必须是两个地方，否则「按 workspace 认人」这条"
        f"语义没被测到：{block!r}"
    )

    # The round's own plans tree is a third place again -- the shape
    # that used to be read as "a foreign instance".
    round_tree = work / "round-plans"
    assert not _same_path(str(round_tree), str(block.get("plans_dir") or "")), (
        "本轮的计划树必须不同于被测服务自报的计划树，否则测的不是生产形状"
    )

    _EVIDENCE["published"] = block


def test_a_round_recorded_instance_is_not_a_mismatch_with_a_fixture_plans_dir(
    project_dir_backend,
):
    """A recorded instance is judged against itself, not against the round.

    The service under test serves a **fixture** plans tree and reads its
    own database; neither is this round's.  The round recorded that
    instance when it started, so the comparison has to be against the
    **recorded** values -- a service that is not this round's own
    process is not thereby a foreign instance.

    The assertions before the verdict are the anti-false-pass guard:
    the identity was really recorded (``boot_id`` matches what the
    live process publishes), and every compared field is comparable.
    Without them, a ``None`` verdict would only say "no record, no
    verdict" (R2) and the fixture plans tree would never enter the
    picture.
    """
    work = project_dir_backend["work"]
    checkout = project_dir_backend["checkout"]
    plan_id = project_dir_backend["plan_id"]
    port = project_dir_backend["port"]

    agent = _round_agent(work, checkout, plan_id, port)
    recorded = agent._service_instance_identity.get("api") or {}

    # --- the identity was really recorded ------------------------------
    live = _live_instance_block(port)
    assert recorded.get("boot_id"), (
        "这一轮没有真的记录下实例身份，后面的 None 只反映「无记录不拦」："
        f"recorded={recorded!r}"
    )
    assert recorded["boot_id"] == live["boot_id"], (
        f"记录的 boot id 必须就是真进程自报的那个：recorded={recorded!r} "
        f"published={live!r}"
    )
    assert recorded.get("project_dir"), (
        f"真进程必须自报自己的 workspace，否则 project_dir 这一项无从比较："
        f"{recorded!r}"
    )
    assert recorded.get("state_db_inode") is not None, (
        f"真进程必须自报自己的数据库 inode，否则 inode 这一项无从比较："
        f"{recorded!r}"
    )

    # --- the recorded tree really is the fixture tree ------------------
    assert _same_path(str(recorded.get("plans_dir") or ""), str(work / "plans")), (
        f"记录下来的计划树必须是夹具目录：{recorded!r}"
    )
    assert not _same_path(
        str(agent.plan_dir.parent), str(recorded.get("plans_dir") or "")
    ), (
        "本轮的计划树必须不同于记录下来的那棵树，否则「按记录值比」这条"
        f"语义没被测到：recorded={recorded!r}"
    )

    # --- and the verdict -----------------------------------------------
    mismatch = agent._service_instance_mismatch(
        "api", SimpleNamespace(port=port),
    )
    assert mismatch is None, (
        "真后端进程服务的是它自己的夹具计划树，不得被判成「别人」："
        f"mismatch={mismatch!r}"
    )

    _EVIDENCE["recorded"] = recorded
    _EVIDENCE["verdicts"] = _EVIDENCE.get("verdicts", {})
    _EVIDENCE["verdicts"]["recorded_instance_is_not_a_mismatch"] = None


def test_a_different_project_dir_is_still_a_mismatch(
    project_dir_backend,
):
    """The gate is not switched off -- it is pointed at the right baseline.

    The negative control: the recorded ``project_dir`` names another
    workspace than the one answering now, and the gate must say so and
    name the field.  The instance answering is still the **real**
    process, fetched over the real road -- only the recorded baseline
    was moved.

    Without this control, "not blocked" could also mean "the gate does
    nothing at all".
    """
    work = project_dir_backend["work"]
    checkout = project_dir_backend["checkout"]
    plan_id = project_dir_backend["plan_id"]
    port = project_dir_backend["port"]

    agent = _round_agent(work, checkout, plan_id, port)
    recorded = agent._service_instance_identity["api"]

    other_workspace = str((work / "other-workspace").resolve())
    recorded["project_dir"] = other_workspace

    mismatch = agent._service_instance_mismatch(
        "api", SimpleNamespace(port=port),
    )
    assert mismatch is not None, (
        "记录里的 workspace 换成别的之后，实例必须被拦下"
    )
    assert mismatch["mismatched_fields"] == ["project_dir"], mismatch
    assert mismatch["expected_project_dir"] == other_workspace, mismatch
    assert _same_path(
        str(mismatch["actual_project_dir"]), EXPECTED_PROJECT_DIR
    ), (
        f"actual 一侧必须是真进程自报的那个 workspace：{mismatch!r}"
    )

    _EVIDENCE["verdicts"] = _EVIDENCE.get("verdicts", {})
    _EVIDENCE["verdicts"]["different_project_dir"] = mismatch["mismatched_fields"]


def test_an_old_instance_without_project_dir_falls_back_to_plans_dir(
    project_dir_backend,
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
    work = project_dir_backend["work"]
    checkout = project_dir_backend["checkout"]
    plan_id = project_dir_backend["plan_id"]
    port = project_dir_backend["port"]

    agent = _round_agent(work, checkout, plan_id, port)
    recorded = agent._service_instance_identity["api"]

    # The recorded instance is an **old** one: no workspace at all.
    recorded.pop("project_dir", None)
    other_tree = str((work / "other-plans").resolve())
    recorded["plans_dir"] = other_tree

    mismatch = agent._service_instance_mismatch(
        "api", SimpleNamespace(port=port),
    )
    assert mismatch is not None, (
        "老实例换了 plans 树必须被拦下"
    )
    assert mismatch["mismatched_fields"] == ["plans_dir"], mismatch
    assert _same_path(str(mismatch["expected_plans_dir"]), other_tree), (
        f"expected 一侧必须是记录下来的那棵树：{mismatch!r}"
    )
    assert _same_path(
        str(mismatch["actual_plans_dir"]), str(work / "plans")
    ), (
        f"actual 一侧必须是真进程自报的那棵树：{mismatch!r}"
    )

    _EVIDENCE["verdicts"] = _EVIDENCE.get("verdicts", {})
    _EVIDENCE["verdicts"]["old_instance_without_project_dir"] = (
        mismatch["mismatched_fields"]
    )