"""来源判定与验证启动的成本界。

决策点 10（性能与资源回归）：只测「不该发生的事」的代价，不做压测。

这次改动在热路径上加了两次目录判定和一次可能发生的翻译。三件事各有各的
「不该发生」：

* 盘上**已有**机器计划时，启动**不许**问模型 —— 那是一次几十秒的往返，而
  答案就在盘上；
* 盘上**没有**机器计划时，只许问**一次** —— 问两次就是两倍的钱；
* 列表给 200 条 plan 时，每条 plan 的判定成本必须是**常数** —— 否则「判定」
  这一层自己就长成了 O(n²)。

「判定成本」有两个维度，两个都得是常数：**stat 次数**（读了几个文件）与
**扫描条目数**（走过了多少目录体积）。只量前者会漏掉一整类超线性 —— 一段
「每条 plan 都把 plans 根目录再扫一遍」的代码，stat 次数完全正常，超线性
全在扫描里。

所以这一档测的是**次数**与**数量级**：没有压测、没有并发、没有忙循环。机器
忙不忙由 :func:`_host_is_busy` 判（load1 ≤ 0.5 × cpu_count 才算闲），忙的机器
上把「测不动」说成跳过，而不是把一次测量失败说成回归。

计划目录一律建在 ``tmp_path`` 之下：仓库里真实的计划树是运行时的活状态，
写进去就等于替别人声明了一条 plan。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

import pytest
from starlette.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from plan_artifacts import (  # noqa: E402
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    TASKS_FILENAME,
    resolve_plan_source,
)
from routes import verification as verification_routes  # noqa: E402
from tests.fixtures import plan_artifacts_fixture  # noqa: E402
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    build_machine_plan,
)
from verification_agent import Orchestrator  # noqa: E402
from verification_plan_review import PRD_FILENAME  # noqa: E402

#: 这一档跑在串行 lane 上（``-m perf``）。少了这个标记，选它的那条命令会
#: 选中 0 条用例然后以退出码 5 结束 —— 看起来是「跑过了」，其实什么都没跑。
pytestmark = pytest.mark.perf

#: 本模块用到的计划 id。形态合法（``<日期>-<slug>``），只是夹具名。
PLAN_ID = "20260901-perf"

#: 启动成本的上界（毫秒）。盘上已有机器计划时，验证轮启动前的那段准备是
#: 纯文件读写 —— 它不该花掉半秒。
STARTUP_BUDGET_MS = 500.0

#: 每条 plan 的判定成本上界（次 stat 类调用）。判定读的是**固定几个**文件，
#: 所以每条 plan 的成本是一个常数；这个上界是那个常数的天花板。
MAX_STATS_PER_PLAN = 40

#: 每条 plan 的扫描成本上界（枚举出来的目录条目数）。列 plans 时根目录被枚举
#: **一次** —— 那一次的条目数正好是 plan 条数，所以每条 plan 平均摊到 1 个条目。
#: 这个上界是那个常数的天花板：任何「每条 plan 都把 plans 根目录再扫一遍」的
#: 写法都会在这里露馅（它摊到的条目数会跟着 plan 条数一起长）。
MAX_SCAN_ENTRIES_PER_PLAN = 8

#: 来源判定自己的成本上界（次 stat）。背景里说的「两次目录判定」中的第二次
#: 就是它：读**固定两个**文件（计划书、机器计划），所以代价是一个常数。
#: 实测：REVIEWED 1 次、MACHINE_ONLY 2 次；这里留一倍余量。
MAX_SOURCE_PROBES = 4

#: 计划书里的哨兵：翻译提示词带的是验证点骨架，所以它**应当**出现在提示词里。
DOC_SENTINEL = "SENTINEL-PLAN-DOC-7f21"

#: PRD 里的哨兵：翻译阶段不读 PRD，所以它**不应当**出现在翻译提示词里。
PRD_SENTINEL = "SENTINEL-PRD-3a94"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class _RecordingTool:
    """模型替身：记下每一次调用，回一份事先准备好的机器计划。

    替身**不联网**：翻译这一步在生产里是要问模型的，而这一档要断言的是
    「问了几次、问了什么」，不是模型答得对不对。
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def query_json(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self.payload


def _install_tool(
    monkeypatch: pytest.MonkeyPatch,
    payload: Dict[str, Any],
) -> _RecordingTool:
    """把「造模型工具」这一步换成替身，返回替身本身（用来数调用次数）。"""
    tool = _RecordingTool(payload)
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)
    return tool


def _doc_with_sentinel() -> str:
    """计划书 + 一个哨兵。

    哨兵落在验证点的**预期结果**里 —— 那正是翻译提示词带的骨架字段，所以
    「提示词里有计划书」这件事有一个能查的字串。
    """
    doc = CANONICAL_PLAN_DOC.replace(
        "解析出一个 40 位十六进制提交号",
        f"解析出一个 40 位十六进制提交号 {DOC_SENTINEL}",
    )
    assert DOC_SENTINEL in doc, "the sentinel must reach the plan document"
    return doc


def _plan_dir(
    base: Path,
    *,
    doc: Optional[str] = None,
    machine_plan: bool = False,
) -> Path:
    """造一个**开发分支**的计划目录（``tasks.json`` 在 → 开发分支）。

    开发分支是这里唯一能整条走完前奏又不碰 worktree 的分支：纯验证分支还要
    物化 worktree、注册 ``project_dir``，那不是这一档要测的东西。
    """
    base.mkdir(parents=True, exist_ok=True)
    (base / TASKS_FILENAME).write_text(
        json.dumps({"tasks": []}, ensure_ascii=False), encoding="utf-8"
    )
    if doc is not None:
        (base / PLAN_DOC_FILENAME).write_text(doc, encoding="utf-8")
    if machine_plan:
        (base / MACHINE_PLAN_FILENAME).write_text(
            json.dumps(
                build_machine_plan(doc if doc is not None else CANONICAL_PLAN_DOC),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return base


def _host_is_busy() -> bool:
    """这台机器现在忙不忙（load1 > 0.5 × cpu_count 就算忙）。

    「夜里通常比较闲」不是许可 —— 闲不闲只能在启动的那一刻判断。所以这道
    判据写在夹具里，而不是靠命令行手搓的负载。
    """
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):  # pragma: no cover - 平台不支持
        return False
    cpu_count = os.cpu_count() or 1
    return load1 > 0.5 * cpu_count


def _assert_within_budget(elapsed_ms: float, what: str) -> None:
    """断言这次启动落在预算之内。

    机器忙的时候，一次测量失败说明的是「机器没给到 CPU」，不是「代码慢了」。
    这时说的是跳过，而不是把一次测不动说成一次回归 —— 但机器闲的时候，
    上界一个字节都不让。
    """
    if elapsed_ms >= STARTUP_BUDGET_MS and _host_is_busy():
        pytest.skip(
            f"skipped: the host was too busy to measure {what} "
            "(load1 above 0.5 x cpu_count)"
        )
    assert elapsed_ms < STARTUP_BUDGET_MS, (
        f"{what} took {elapsed_ms:.1f} ms, over the "
        f"{STARTUP_BUDGET_MS:.0f} ms budget"
    )


class _CountingScandir:
    """:func:`os.scandir` 的代理：数一数它枚举出了多少条目。

    代理只**加计数**，不改变语义：条目原样透传，``with`` / ``close`` 都按
    真身那一套走。
    """

    def __init__(self, iterator: Any, counter: Dict[str, int], key: str) -> None:
        self._iterator = iterator
        self._counter = counter
        self._key = key

    def __iter__(self) -> Any:
        for entry in self._iterator:
            self._counter[self._key] += 1
            yield entry

    def __enter__(self) -> "_CountingScandir":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._iterator.close()


def _count_fs_probes_under(root: Path, action: Callable[[], None]) -> Dict[str, int]:
    """数一数 ``action`` 对 ``root`` 这棵树动了多少「文件系统对象」。

    两类探针，都算**工作单位**：

    * ``stat`` —— 每次 :meth:`Path.stat` 算 1 个。一次 stat 是 O(1) 的工作量。
    * ``scan`` —— 每次 :func:`os.listdir` / :func:`os.scandir` **枚举出来的条目
      数**。这里数的是条目数而不是调用次数：一次 scandir 是 O(1) 次系统调用，
      但它枚举出来的条目数才是它真正走过的目录体积。只数调用次数的话，一段
      「每条 plan 都把 plans 根目录再扫一遍」的代码看起来仍然只有 n 次调用 ——
      而它走过的目录体积是 n²，超线性就藏在那里。

    数的是**落在树里**的那些：判定读的是每个 plan 目录下的固定几个文件，
    所以这个数除以 plan 条数，就是每条 plan 的判定成本。
    """
    counters: Dict[str, int] = {"stat": 0, "scan": 0}
    real_stat = Path.stat
    real_listdir = os.listdir
    real_scandir = os.scandir
    root_str = str(root)

    def under(path: Any) -> bool:
        try:
            candidate = os.path.abspath(os.fspath(path))
        except TypeError:
            return False
        return candidate == root_str or candidate.startswith(root_str + os.sep)

    def counting_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if under(self):
            counters["stat"] += 1
        return real_stat(self, *args, **kwargs)

    def counting_listdir(path: Any = ".") -> Any:
        entries = real_listdir(path)
        if under(path):
            counters["scan"] += len(entries)
        return entries

    def counting_scandir(path: Any = ".") -> Any:
        iterator = real_scandir(path)
        if not under(path):
            return iterator
        return _CountingScandir(iterator, counters, "scan")

    with mock.patch.object(Path, "stat", counting_stat), mock.patch.object(
        os, "listdir", counting_listdir
    ), mock.patch.object(os, "scandir", counting_scandir):
        action()
    return counters


# ---------------------------------------------------------------------------
# 1. 盘上已有机器计划时，启动不花钱
# ---------------------------------------------------------------------------


def test_reviewed_start_with_cached_json_is_millisecond(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两种来源的启动都不问模型，而且都快。

    * ``REVIEWED``（计划书在）：机器计划也已经在盘上 —— 读它、过等价性门禁，
      结束。**不重新落盘**（盘上那一份是上一轮管线的产物，原样留着）。
    * ``MACHINE_ONLY``（只有机器计划）：连计划准备这一段都不走。

    两种都不该有模型往返：问一次模型就是几十秒，而这里的答案是文件。
    """
    tool = _install_tool(monkeypatch, build_machine_plan(CANONICAL_PLAN_DOC))

    cases = {
        "REVIEWED": _plan_dir(
            tmp_path / "reviewed", doc=CANONICAL_PLAN_DOC, machine_plan=True
        ),
        "MACHINE_ONLY": _plan_dir(tmp_path / "machine-only", machine_plan=True),
    }

    # 背景里说的「两次目录判定」中的第二次就是来源判定本身。它是常数成本：
    # 读固定两个文件（计划书、机器计划），命中即止。直接量它，而不是只看整段
    # 启动的墙钟 —— 墙钟把判定、等价性门禁、读盘三件事混在一起了。
    for label, plan_dir in cases.items():
        probes = _count_fs_probes_under(
            plan_dir, lambda d=plan_dir: resolve_plan_source(d)
        )
        assert probes["scan"] == 0, (
            f"{label}: the source determination enumerated the plan directory "
            f"({probes['scan']} entries) — it must read two known paths, not "
            "walk the directory"
        )
        assert probes["stat"] <= MAX_SOURCE_PROBES, (
            f"{label}: the source determination touched {probes['stat']} "
            f"filesystem objects, over the {MAX_SOURCE_PROBES} budget"
        )

    for label, plan_dir in cases.items():
        start = time.perf_counter()
        failure = verification_routes._verification_branch_prelude(
            PLAN_ID, plan_dir, review_ready=False
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert failure is None, f"{label}: {failure.body!r}"
        assert not tool.calls, (
            f"{label}: the model was asked {len(tool.calls)} time(s) "
            "although the machine plan was on disk"
        )
        _assert_within_budget(elapsed_ms, f"{label} startup")


# ---------------------------------------------------------------------------
# 2. 机器计划缺失时，只付一次翻译
# ---------------------------------------------------------------------------


def test_reviewed_start_without_json_pays_one_translation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """机器计划缺失时：恰一次翻译；提示词里有计划书、没有 PRD。

    「恰一次」是钱：翻译要问一次模型，问两次就是两倍。提示词的内容是另一
    件事 —— 翻译阶段只需要验证点的骨架，PRD 是 Phase-1 的输入，带进来只是
    白白多花 token，还让模型有一份用不上的上下文。

    区分这两件事**靠哨兵，不靠墙钟**：同一个哨兵放进 Phase-1 的提示词里
    会出现一次，放进翻译的提示词里不该出现。
    """
    doc = _doc_with_sentinel()
    tool = _install_tool(monkeypatch, build_machine_plan(doc))
    plan_dir = _plan_dir(tmp_path / "translate", doc=doc)
    (plan_dir / PRD_FILENAME).write_text(
        json.dumps({"sentinel": PRD_SENTINEL}, ensure_ascii=False),
        encoding="utf-8",
    )

    failure = verification_routes._verification_branch_prelude(
        PLAN_ID, plan_dir, review_ready=False
    )

    assert failure is None, f"the translation failed: {failure.body!r}"
    assert len(tool.calls) == 1, (
        f"expected exactly one translation, got {len(tool.calls)}"
    )

    prompt = str(tool.calls[0].get("prompt", ""))
    assert DOC_SENTINEL in prompt, (
        "the translation prompt must carry the plan document"
    )
    assert PRD_SENTINEL not in prompt, (
        "the translation prompt must not carry the PRD"
    )
    assert (plan_dir / MACHINE_PLAN_FILENAME).exists(), (
        "the translated machine plan was not published"
    )

    # Phase-1 的提示词**带** PRD：同一个哨兵在那边出现一次。所以上面那句
    # 「没有出现」说的是提示词的形状，不是哨兵本身不成立。
    phase1_prompt = Orchestrator._build_planning_prompt(None, {"prd": PRD_SENTINEL})
    assert phase1_prompt.count(PRD_SENTINEL) == 1


# ---------------------------------------------------------------------------
# 3. 列表：每条 plan 的判定成本是常数
# ---------------------------------------------------------------------------


def _fetch_listing(monkeypatch: pytest.MonkeyPatch, tree: Path) -> None:
    """跑一次计划列表，把计划根指到夹具树。

    走的是「没有状态库」的那条路（目录扫描）：这一档要量的是**文件系统**
    上的判定成本，数据库那一层与它无关。
    """
    monkeypatch.setattr(server, "PLANS_DIR", tree)
    monkeypatch.setattr(server, "_open_state_machine", lambda request: None)
    server.invalidate_plans_cache()
    client = TestClient(server.app)
    response = client.get("/api/plans")
    assert response.status_code == 200, response.text


def test_detect_is_constant_per_plan_in_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """200 条 plan 的列表：每条 plan 的判定成本不随条数增长。

    判的是**每条 plan 的平均成本**在 50 条与 200 条时是否一样：一样就是
    线性（总成本 = 条数 × 常数），不一样就是判定这一层自己长出了超线性。

    两个维度都判：**stat 次数**（判定读了几个文件）与**扫描条目数**（判定走过
    了多少目录体积）。只看 stat 次数的话，一段每条 plan 都把 plans 根目录再
    扫一遍的代码是看不出来的 —— 那段代码的 stat 次数完全正常，超线性全在扫
    描里。
    """
    template = _plan_dir(
        tmp_path / "template", doc=CANONICAL_PLAN_DOC, machine_plan=True
    )

    totals: Dict[int, Dict[str, int]] = {}
    for count in (0, 50, 200):
        tree = tmp_path / f"tree-{count}"
        tree.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            shutil.copytree(template, tree / f"20260901-plan-{index:04d}")
        totals[count] = _count_fs_probes_under(
            tree, lambda tree=tree: _fetch_listing(monkeypatch, tree)
        )

    def per_plan(count: int, key: str) -> float:
        """每条 plan 平均动了多少个 ``key`` 类的文件系统对象。"""
        return (totals[count][key] - totals[0][key]) / count

    per_stat_small = per_plan(50, "stat")
    per_stat_large = per_plan(200, "stat")
    per_scan_small = per_plan(50, "scan")
    per_scan_large = per_plan(200, "scan")

    assert per_stat_small > 0, (
        "the listing did not touch the plan tree at all — nothing was measured"
    )
    assert per_stat_large <= per_stat_small * 1.5, (
        "the per-plan cost grew with the plan count: "
        f"{per_stat_small:.1f} -> {per_stat_large:.1f} stats per plan"
    )
    assert per_stat_large <= MAX_STATS_PER_PLAN, (
        f"{per_stat_large:.1f} stats per plan, over the "
        f"{MAX_STATS_PER_PLAN} budget"
    )

    assert per_scan_large <= per_scan_small * 1.5, (
        "the per-plan scan cost grew with the plan count: "
        f"{per_scan_small:.1f} -> {per_scan_large:.1f} directory entries per "
        "plan — the listing is walking the plans tree superlinearly"
    )
    assert per_scan_large <= MAX_SCAN_ENTRIES_PER_PLAN, (
        f"{per_scan_large:.1f} directory entries scanned per plan, over the "
        f"{MAX_SCAN_ENTRIES_PER_PLAN} budget"
    )


# ---------------------------------------------------------------------------
# 4. 落盘不留下临时文件
# ---------------------------------------------------------------------------


def test_no_temp_files_left_in_plan_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """发布机器计划之后，plan 目录里没有 ``.tmp`` / ``.bak`` 残留。

    发布走的是「先写临时文件、再改名」：改名是原子的，所以读的人看到的要么
    是旧的那一份、要么是完整的新的一份。临时文件只是这中间那一步的脚手架 ——
    它留在盘上就说明那次发布崩在了半路。
    """
    doc = _doc_with_sentinel()
    _install_tool(monkeypatch, build_machine_plan(doc))

    published = _plan_dir(tmp_path / "published", doc=doc)
    # 先把计划状态读一遍（它会落盘自己的状态文件），这样「新增了哪些文件」
    # 这个问题里只剩机器计划这一个答案。
    verification_routes._recorded_baseline(published)
    before = plan_artifacts_fixture.snapshot(published)

    failure = verification_routes._verification_branch_prelude(
        PLAN_ID, published, review_ready=False
    )
    assert failure is None, f"the publication failed: {failure.body!r}"

    after = plan_artifacts_fixture.snapshot(published)
    assert set(after) - set(before) == {MACHINE_PLAN_FILENAME}, (
        "the publication touched more than the machine plan: "
        f"{sorted(set(after) - set(before))}"
    )
    assert after[PLAN_DOC_FILENAME]["sha256"] == before[PLAN_DOC_FILENAME]["sha256"], (
        "the reviewed plan document was rewritten"
    )
    residue = sorted(name for name in after if name.endswith((".tmp", ".bak")))
    assert not residue, f"temporary files left behind: {residue}"

    # 已经有一份机器计划时：启动不落盘，盘上自然也没有临时文件。
    cached = _plan_dir(
        tmp_path / "cached", doc=doc, machine_plan=True
    )
    failure = verification_routes._verification_branch_prelude(
        PLAN_ID, cached, review_ready=False
    )
    assert failure is None, f"the cached start failed: {failure.body!r}"
    cached_residue = sorted(
        name for name in plan_artifacts_fixture.snapshot(cached)
        if name.endswith((".tmp", ".bak"))
    )
    assert not cached_residue, (
        f"temporary files left behind: {cached_residue}"
    )
