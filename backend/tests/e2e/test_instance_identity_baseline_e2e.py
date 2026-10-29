"""端到端验收：**真**后端进程下，实例身份基线不会被误判。

背景
----
单测（``tests/unit/test_service_instance_identity_baseline.py``）用的是**替身**
服务：同一个进程里的 ``http.server``，一个手写的 ``instance`` 块。单测证明的是
「比较函数拿记录值当基线」——它证明不了**生产形状**下这件事还成立。

生产形状是三个进程属性叠在一起：

* **一轮跑在一个进程里** —— 轮次的 agent（``verification_agent.Orchestrator``）
  在自己的进程里，持有自己的计划目录；
* **被测服务是另一个真实后端进程** —— ``backend.server`` 的真子进程，监听内核
  选的空闲端口，服务**它自己的**计划树、读**它自己的**状态库；
* **两者之间是真 HTTP** —— 轮次通过 ``/health/data`` 读那个进程自报的事实，
  没有任何网络替身。

这个文件要证明的正是第三个属性没有把前两个属性变成假的：真后端自报的计划树
**不等于**本轮的计划目录，而这样的实例**不得**被当成「别人」拦下 —— 那正是
2026-10-26 生产上「每一轮都被判成环境阻断」的形状。

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
``_round_agent()`` 先真的记录一次（``_record_service_instance_identity()``），
并且断言记录非空。不这么做的话，「无记录不拦」（R2）会让断言返回 ``None`` ——
而那**只**说明 R2 生效，不说明 R1（按记录值比）生效：这道题就空了。

负控
----
第二条测试把记录里的 ``boot_id`` 改成别的进程的值，判据必须回到
``["boot_id"]``：门不是被关掉了，是被指到了正确的基线上。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The siblings are reused **as they are**: the real ``backend.server``
# subprocess, the scripted LLM, the git checkout and the one HTTP client
# the whole E2E lane speaks through.
from tests.e2e.test_plan_lifecycle_ui_e2e import (  # noqa: E402
    _boot_server,
    _http,
    _make_checkout,
    _write_scripted_llm,
)

from verification_agent import VerificationAgent  # noqa: E402

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The material the import brings in.  The scripted LLM answers the
#: generation prompt from disk, so the text itself only has to be *a*
#: material; what the plan is called is what the round is about.
IMPORT_CONTENT = "1. 修一个东西"
IMPORT_TITLE = "instance-identity-baseline-e2e"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _tail(path: Path, lines: int = 60) -> str:
    """The last ``lines`` lines of a file, or why it could not be read."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(cannot read {path}: {exc})"
    if not content:
        return f"({path} is empty)"
    return "\n".join(content[-lines:])


def _boot_or_fail(work: Path, script_dir: Path) -> Tuple[str, subprocess.Popen]:
    """Boot the real backend; fail (never skip) when it does not come up."""
    try:
        return _boot_server(work, script_dir)
    except Exception as exc:  # noqa: BLE001 - every failure is a failure
        log = work / "server.log"
        raise AssertionError(
            f"the real backend did not come up: {exc}\n"
            f"--- tail of {log} ---\n{_tail(log)}"
        ) from exc


def _stop_server(proc: subprocess.Popen) -> None:
    """Terminate the server subprocess and close its log handle."""
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _server_port(base_url: str) -> int:
    """The port the booted server answers on."""
    return int(urlparse(base_url).port or 0)


def _live_instance_block(port: int) -> Dict[str, Any]:
    """The ``instance`` block the **real** backend publishes about itself.

    Read over the very road a verification point's request travels, so
    the block under test is a block a real process really published.
    """
    status, body = _http("GET", f"http://127.0.0.1:{int(port)}/health/data")
    assert status == 200, (
        f"the real backend answered {status} on /health/data: {body!r}"
    )
    block = (body or {}).get("instance")
    assert isinstance(block, dict), (
        f"the real backend published no instance block: {body!r}"
    )
    return block


def _import_plan(base_url: str, checkout: Path) -> str:
    """Import a real plan into the running backend; return its plan id."""
    status, body = _http(
        "POST",
        f"{base_url}/api/plan/import",
        {
            "project_dir": str(checkout),
            "content": IMPORT_CONTENT,
            "title": IMPORT_TITLE,
        },
        timeout=300,
    )
    assert status == 200, f"the import failed: {status} {body!r}"
    plan_id = str((body or {}).get("plan_id") or "")
    assert plan_id, f"the import produced no plan id: {body!r}"
    return plan_id


def _round_agent(
    work: Path, checkout: Path, plan_id: str, port: int,
) -> VerificationAgent:
    """The round's own agent -- running in **this** process.

    The agent's plan directory lives in the round's **own** plans tree
    (``<work>/round-plans/<plan_id>``), which is **not** the tree the
    backend under test serves (``<work>/plans``).  That difference is the
    production shape: the service under test is somebody else's process,
    serving somebody else's tree.

    The recording happens **here**, before the agent is handed back, and
    its result is asserted: without a recorded identity the mismatch
    check answers ``None`` for the wrong reason ("I do not know who it
    is"), and the test would pass while proving nothing.
    """
    plan_dir = work / "round-plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=checkout,
        coding_tool=None,
    )
    agent._record_service_instance_identity("api", port)

    recorded = agent._service_instance_identity.get("api") or {}
    assert recorded.get("boot_id"), (
        "这一轮没有真的记录下实例身份，后面的断言会因「无记录不拦」空过："
        f"recorded={recorded!r}"
    )

    live = _live_instance_block(port)
    assert Path(agent.plan_dir.parent).resolve() != Path(
        str(live.get("plans_dir") or "")
    ).resolve(), (
        "本轮的计划目录与被测服务自报的计划树必须不同，否则测的不是生产形状："
        f"round={agent.plan_dir.parent!s} service={live.get('plans_dir')!r}"
    )
    return agent


# ---------------------------------------------------------------------------
# The scene: a real backend process, serving a real plan
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_backend(tmp_path_factory) -> Dict[str, Any]:
    """Boot the real backend once and hand the facts about it to the tests."""
    work = tmp_path_factory.mktemp("instance-identity-baseline-e2e")
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
        block = _live_instance_block(_server_port(base_url))
        yield {
            "work": work,
            "checkout": checkout,
            "base_url": base_url,
            "port": _server_port(base_url),
            "block": block,
            "plan_id": plan_id,
        }
    except Exception as exc:  # noqa: BLE001 - the server log is the context
        log = work / "server.log"
        raise AssertionError(
            f"{exc}\n--- tail of {log} ---\n{_tail(log)}"
        ) from exc
    finally:
        _stop_server(proc)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_a_round_in_another_plans_tree_is_not_blocked_by_the_live_instance(
    live_backend,
):
    """A round whose plan tree is not the service's tree is not blocked.

    The service under test is a **real** backend process: it serves its
    own plans tree and reads its own database, and neither is this
    round's.  The round recorded that instance when it started, so the
    comparison has to be against the **recorded** values -- a service
    that is not this round's own process is not thereby a foreign
    instance.

    The first assertion is the anti-false-pass guard: the identity was
    really recorded (``boot_id`` matches what the live process
    publishes).  Without it, a ``None`` verdict would only say "no
    record, no verdict" (R2) and the round's own tree would never enter
    the picture.
    """
    work = live_backend["work"]
    checkout = live_backend["checkout"]
    plan_id = live_backend["plan_id"]
    port = live_backend["port"]
    block = live_backend["block"]

    agent = _round_agent(work, checkout, plan_id, port)

    # --- the scene is the production shape ----------------------------
    assert Path(agent.plan_dir.parent).resolve() != Path(
        str(block.get("plans_dir") or "")
    ).resolve(), (
        "本轮的计划树必须不同于被测服务自报的计划树，否则测的不是生产形状"
    )

    # --- the identity was really recorded ------------------------------
    recorded = agent._service_instance_identity.get("api") or {}
    assert recorded.get("boot_id"), (
        "这一轮没有记录下实例身份，后面的 None 只反映「无记录不拦」"
    )
    assert recorded["boot_id"] == block["boot_id"], (
        f"记录的 boot id 必须就是真进程自报的那个：recorded={recorded!r} "
        f"published={block!r}"
    )
    # All three compared fields have to be **comparable**: a field the
    # instance did not answer is skipped by the comparison, and a
    # comparison that cannot be made is not a comparison that failed.
    assert recorded.get("plans_dir"), (
        f"真进程必须自报自己的计划树，否则 plans 这一项无从比较：{recorded!r}"
    )
    assert recorded.get("state_db_inode") is not None, (
        f"真进程必须自报自己的数据库 inode，否则 inode 这一项无从比较："
        f"{recorded!r}"
    )

    # --- and the verdict -----------------------------------------------
    mismatch = agent._service_instance_mismatch(
        "api", SimpleNamespace(port=port),
    )
    assert mismatch is None, (
        "真后端进程服务的是它自己的计划树，不得被判成「别人」："
        f"mismatch={mismatch!r}"
    )


def test_a_recorded_instance_whose_boot_id_changed_is_still_blocked(
    live_backend,
):
    """The gate is not switched off -- it is pointed at the right baseline.

    The negative control: the recorded ``boot_id`` is replaced by another
    process's, so the instance answering now is **not** the instance the
    round recorded -- and the gate must say so, and name the field.

    Without this control, "not blocked" could also mean "the gate does
    nothing at all".
    """
    work = live_backend["work"]
    checkout = live_backend["checkout"]
    plan_id = live_backend["plan_id"]
    port = live_backend["port"]

    agent = _round_agent(work, checkout, plan_id, port)

    recorded = agent._service_instance_identity["api"]
    other_boot_id = f"{recorded['boot_id']}-from-another-process"
    recorded["boot_id"] = other_boot_id

    mismatch = agent._service_instance_mismatch(
        "api", SimpleNamespace(port=port),
    )
    assert mismatch is not None, (
        "记录里的 boot id 换成别的进程后，实例必须被拦下"
    )
    assert mismatch["mismatched_fields"] == ["boot_id"], mismatch
    assert mismatch["expected_boot_id"] == other_boot_id, mismatch