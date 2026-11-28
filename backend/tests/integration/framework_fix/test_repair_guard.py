"""修复生成器的框架修复守卫 —— 被阻塞的计划不自己修。

这个文件钉住什么
----------------
* ``test_skip_blocked`` —— 注册表里**有**这个指纹（``open``）时，修复生成器
  **一个修复任务都不生成**，并把原因写成 ``blocked_on_framework_fix``；
* ``test_unblocked_unchanged`` —— 注册表里**没有**这个指纹时，行为与改动前
  一致：失败照常变成修复任务，跳过原因是 ``None``；
* ``test_partial_skip`` —— 一半的失败命中注册表时，**只跳过命中的那些**，
  其余照常修。

后半段钉的是**内容生成那条路**（``generate_repair_contents``）：生产里真正
创建本地修复任务的两条门（编排器与终局裁决口）直接问它要内容，不经过
``generate``。守卫住在那里，才管得到它们：

* ``test_content_generation_never_asks_the_model_for_a_registered_defect``
  —— 命中的缺陷**连模型都不问**；
* ``test_content_generation_attaches_and_holds_the_plan`` —— 命中即挂接、
  计划即挂起，跳过原因与读模型用同一个词；
* ``test_unregistered_defect_still_reaches_the_model`` —— 没命中照常问模型；
* ``test_the_fix_plan_is_not_blocked_by_its_own_guard`` —— 修复计划自己不
  派生新修复计划，守卫不挡它；
* ``test_the_hold_of_one_failure_does_not_silence_another`` —— 一条失败被挂起
  不许把另一条本地失败一起消掉。

为什么这是集成测试
------------------
这道守卫读的是**跨进程的事实**：注册表（``framework_fix_registry`` 行）与计划
状态里的 ``framework_fix`` 块。两样都住在状态库里，替身（double）证明不了
「读的就是生产那张库」—— 迁移是真的、查询是真的、块是真的。所以每个用例都
对着 ``state_db`` fixture 给的那份真实 SQLite 文件跑。

边界条件
--------
* 指纹 ``fixed`` → **也**跳过（等合并重跑）：缺陷仍归框架修复车道；
* 注册表读不出来 → 记日志并**退化到不跳过**：读不到事实不等于事实成立；
* 计划自己挂起时（没有显式给指纹）→ 指纹从 ``framework_fix`` 块里读。

守卫问的是「这个缺陷归谁修」，不是「怎么修」：命中的失败根本不进生成步骤，
其余的失败走**原样的**那条路（模型出内容，组装器盖章）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework_fix.read_model import BLOCKED as READ_MODEL_BLOCKED  # noqa: E402
from framework_fix.registry import FrameworkFixRegistry  # noqa: E402
from plan_state import PlanState  # noqa: E402
from repair_generator import (  # noqa: E402
    GUARD_ATTACH_SOURCE,
    SKIPPED_BLOCKED_ON_FRAMEWORK_FIX,
    RepairTaskGenerator,
)


#: 守卫给出的跳过原因。与展示词表里的 ``blocked_on_framework_fix`` 同一个词 ——
#: 那是这件事在计划侧**看得见**的样子，这里是生成侧**说得出口**的原因。
BLOCKED = "blocked_on_framework_fix"


class _NoModelCodingTool:
    """一个拒绝回答的 coding tool。

    守卫是**问模型之前**做的判断：一个让模型先答了再断言结果的测试，证明不了
    守卫真的短路了。所以这里的替身一旦被调用就让测试失败。
    """

    def query(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the framework-fix guard must not call the model")

    def query_json(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the framework-fix guard must not call the model")


class _RecordingCodingTool:
    """一个记录调用、并且答得出内容的 coding tool。

    它回答的是**守卫放行**的那些失败：每一行都指向一个失败验证点，内容够
    解析器收下。没有它，「没命中就照常问模型」这件事没法证明。
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def query(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"kwargs": kwargs})
        return "the guard let this failure through"

    def query_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"kwargs": kwargs})
        prompt = str(kwargs.get("prompt") or "")
        return {
            "tasks": [
                {
                    "failed_vp_id": _vp_id_in(prompt),
                    "title": "repair the deliverable",
                    "description": "the guard let this failure through",
                    "acceptance_criteria": "the acceptance command passes",
                    "files_to_modify": ["backend/app.py"],
                }
            ]
        }


def _vp_id_in(prompt: str) -> str:
    """从提示词里认出被问的那个验证点。

    提示词里有验证点的清单；这里认出第一个 ``VP-`` 编号就够了 —— 这道用例
    只关心「模型被问了哪个失败」。
    """
    for token in prompt.replace(",", " ").replace('"', " ").split():
        cleaned = token.strip(":;()[]{}.,")
        if cleaned.startswith("VP-"):
            return cleaned
    return "VP-3"


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """计划自己的目录 —— 目录名就是计划号。"""
    directory = tmp_path / "plans" / "p1"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def generator(plan_dir: Path, tmp_path: Path) -> RepairTaskGenerator:
    """一台修复生成器，指向那个计划。"""
    return RepairTaskGenerator(_NoModelCodingTool(), plan_dir, tmp_path)


def _register(
    plan_id: str = "p1", vp_id: str = "VP-3", **overrides: Any,
) -> str:
    """登记一次框架缺陷命中，返回它的指纹。

    走生产写路径（``FrameworkFixRegistry.register_hit``），库是
    ``state_db`` fixture 给的那份 —— 迁移是真的，表是真的。
    """
    hit: Dict[str, Any] = {
        "plan_id": plan_id,
        "vp_id": vp_id,
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
    }
    hit.update(overrides)
    return FrameworkFixRegistry().register_hit(hit)


def _set_registry_status(fingerprint: str, status: str) -> None:
    """直接写注册表行的 ``status``。

    注册表现在还没有改状态的写入口（任务 10 只做了登记与查询），所以这一列
    用裸 SQL 写 —— 写的就是后来那个写入口会写的那一列。
    """
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    try:
        conn.execute(
            "UPDATE framework_fix_registry SET status = ? WHERE fingerprint = ?",
            (status, fingerprint),
        )
        conn.commit()
    finally:
        conn.close()


def _stub_content_call(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Dict[str, Any]]:
    """把模型那一次内容调用换成记录器。

    返回记录器收集到的调用列表：每次调用一项，带着守卫**放行**的失败。一道
    把所有失败都挡住的守卫必须让这个列表保持空 —— 模型根本没被问。
    """
    calls: List[Dict[str, Any]] = []

    def fake(
        self: Any,
        failed_vps: List[Dict[str, Any]],
        round_number: int,
        *args: Any,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        calls.append(
            {"failed_vps": list(failed_vps), "round_number": round_number}
        )
        return [
            {
                "failed_vp_id": str(vp.get("id") or ""),
                "title": f"repair {vp.get('id')}",
                "description": "the guard let this failure through",
                "files_to_modify": ["backend/app.py"],
                "test_command": "pytest backend/tests/test_app.py",
            }
            for vp in failed_vps
        ]

    monkeypatch.setattr(
        RepairTaskGenerator, "generate_repair_contents", fake,
    )
    return calls


def test_skip_blocked(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表里有这个指纹（``open``）→ 空任务，原因 ``blocked_on_framework_fix``。"""
    calls = _stub_content_call(monkeypatch)
    fingerprint = _register(plan_id="p1", vp_id="VP-3")

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3"}],
        held_fp=fingerprint,
    )

    assert result["tasks"] == [], (
        "注册表已经登记了这个缺陷，本计划就不该再为它生成本地修复任务 —— "
        "两份修复会改同一批文件、互相打架"
    )
    assert result["skipped_reason"] == BLOCKED, (
        f"跳过原因必须说得出是哪件事；got {result['skipped_reason']!r}"
    )
    assert calls == [], (
        "被挡住的那一轮不该问模型要修复内容 —— 守卫是问模型**之前**的判断"
    )


def test_unblocked_unchanged(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表里没有这个指纹 → 行为与改动前一致。"""
    calls = _stub_content_call(monkeypatch)

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3"}],
        held_fp="sha256:" + "0" * 64,
    )

    assert result["skipped_reason"] is None, (
        "注册表没有这条记录时，守卫不该声称跳过了什么"
    )
    assert len(calls) == 1, "失败照常进生成步骤"
    assert [vp["vp_id"] for vp in calls[0]["failed_vps"]] == ["VP-3"], (
        "放行的失败必须原样到达生成步骤 —— 守卫改的是**要不要修**，不是**怎么修**"
    )
    assert [task["failed_vp_id"] for task in result["tasks"]] == ["VP-3"], (
        "改动前这个失败会得到一个修复任务，现在也必须得到一个"
    )


def test_partial_skip(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一半命中 → 只跳过命中的那一半，其余照常修。"""
    calls = _stub_content_call(monkeypatch)
    blocked_fp = _register(plan_id="p1", vp_id="VP-3")

    result = generator.generate(
        plan_id="p1",
        failures=[
            {"vp_id": "VP-3", "fingerprint": blocked_fp},
            {"vp_id": "VP-7", "fingerprint": "sha256:" + "1" * 64},
        ],
        held_fp=blocked_fp,
    )

    assert result["skipped_reason"] == BLOCKED, (
        "有一个失败被跳过了，原因就该说出来"
    )
    assert len(calls) == 1
    assert [vp["vp_id"] for vp in calls[0]["failed_vps"]] == ["VP-7"], (
        "只有命中的那个失败该被挡住；其余的失败是本地缺陷，照常修"
    )
    assert [task["failed_vp_id"] for task in result["tasks"]] == ["VP-7"], (
        "没命中的失败必须照常拿到修复任务"
    )


def test_fixed_fingerprint_still_skipped(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指纹 ``fixed`` → 也跳过（等合并重跑）。"""
    calls = _stub_content_call(monkeypatch)
    fingerprint = _register(plan_id="p1", vp_id="VP-3")
    _set_registry_status(fingerprint, "fixed")

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3"}],
        held_fp=fingerprint,
    )

    assert result == {"tasks": [], "skipped_reason": BLOCKED}, (
        "修复合入不等于缺陷已经消失：合并之后还要按树重跑，所以本地修复"
        "仍然不生成"
    )
    assert calls == []


def test_registry_unreadable_degrades_to_no_skip(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """注册表读不出来 → 记日志并退化到不跳过。"""
    calls = _stub_content_call(monkeypatch)

    def boom(self: Any, fingerprint: str) -> Any:
        raise RuntimeError("the state database is locked")

    monkeypatch.setattr(FrameworkFixRegistry, "lookup", boom)

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3"}],
        held_fp="sha256:" + "0" * 64,
    )

    assert result["skipped_reason"] is None, (
        "读不到注册表不等于「缺陷归别人修」—— 退化到不跳过，日志留下证据"
    )
    assert len(calls) == 1, "退化路径下失败照常进生成步骤"
    assert "repair_guard_registry_unreadable" in capsys.readouterr().out, (
        "读不到注册表必须留下日志，否则「为什么没跳过」无从查起"
    )


def test_blocked_failures_are_reported_to_the_caller(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被挡下的失败要说得出是**哪几条**。

    ``skipped_reason`` 只说得出「这一轮有东西被挡下了」，说不出是哪几条。
    而「哪几条」正是接下来要做的事：挂接（attach）要按指纹把每个被挡下的
    验证点挂到修复计划上，操作员要看的也是「VP-3 没有修复任务，因为它是
    框架缺陷」而不是一句笼统的「被挡住了」。
    """
    _stub_content_call(monkeypatch)
    blocked_fp = _register(plan_id="p1", vp_id="VP-3")

    generator.generate(
        plan_id="p1",
        failures=[
            {"vp_id": "VP-3", "fingerprint": blocked_fp},
            {"vp_id": "VP-7", "fingerprint": "sha256:" + "1" * 64},
        ],
        held_fp=blocked_fp,
    )

    assert [item["id"] for item in generator.blocked_vps] == ["VP-3"], (
        "被挡下的必须逐条说出来 —— 只说「有东西被挡下了」，调用方无从知道"
        "该把哪几个验证点挂到修复计划上"
    )
    assert generator.blocked_vps[0]["fingerprint"] == blocked_fp, (
        "挂接按指纹走，所以被挡下的那条必须带着它匹配上的指纹"
    )
    assert generator.blocked_vps[0]["reason"] == BLOCKED


def test_own_fingerprint_wins_over_the_held_one(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败自带自己的指纹 → 按它自己的判，不按计划挂着的那个。

    这一条是「部分阻塞时计划继续跑其它工作」在守卫里的落点：计划挂在框架
    缺陷 A 上，而某个失败自带一个**没有登记过**的指纹 B —— B 是本地缺陷，
    照常修。只按挂着的指纹判会把 B 一起挡掉，那条本地缺陷就再没有人修。
    """
    calls = _stub_content_call(monkeypatch)
    held_fp = _register(plan_id="p1", vp_id="VP-3")

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3", "fingerprint": "sha256:" + "2" * 64}],
        held_fp=held_fp,
    )

    assert result["skipped_reason"] is None, (
        "挂着的那个指纹归框架修复车道，不代表**每一条**失败都归它 —— "
        "自带了别的指纹的失败按自己的指纹判"
    )
    assert [vp["vp_id"] for vp in calls[0]["failed_vps"]] == ["VP-3"]
    assert [task["failed_vp_id"] for task in result["tasks"]] == ["VP-3"], (
        "本地缺陷照常拿到修复任务"
    )


def test_hit_from_another_plan_still_blocks(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别的计划登记的同一个签名 → 也挡住。

    注册表是按**签名**索引的，不是按计划索引：五个计划命中同一个签名时，
    只有第一个创建修复计划，后四个是挂接。五份计划都不该再为这个签名
    生成本地修复任务 —— 否则「修复工作量恒为 1 份」就破了。
    """
    calls = _stub_content_call(monkeypatch)
    fingerprint = _register(plan_id="p2", vp_id="VP-3")

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3", "fingerprint": fingerprint}],
        held_fp=fingerprint,
    )

    assert result == {"tasks": [], "skipped_reason": BLOCKED}, (
        "签名是全局的：谁登记的不影响谁被挡住"
    )
    assert calls == []


def test_alias_hit_is_blocked(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语义复核把签名并进另一个签名 → 仍然挡住。

    任务 14 的语义复核把「症状不同但同因」的签名写成别名。守卫查的是
    注册表，别名链在注册表里解析 —— 所以按别名指纹问，拿到的仍是那个
    规范指纹的行，还是归框架修复车道。不挡的话，同一个缺陷会拿到第二份
    本地修复。
    """
    calls = _stub_content_call(monkeypatch)
    canonical = _register(plan_id="p1", vp_id="VP-3")
    alias = "sha256:" + "3" * 64
    FrameworkFixRegistry().write_alias(alias, canonical)

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3", "fingerprint": alias}],
        held_fp=alias,
    )

    assert result == {"tasks": [], "skipped_reason": BLOCKED}, (
        "别名解析回规范指纹之后，命中的还是同一个缺陷"
    )
    assert calls == []


def test_empty_round_asks_nothing(
    state_db: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有失败 → 不问注册表。

    一轮没有失败就没有指纹可查：查注册表既没有答案可读，也会把「这一轮
    什么都没修」和「这一轮读不到注册表」混成同一件事。
    """

    def boom(self: Any, fingerprint: str) -> Any:
        raise AssertionError("an empty round must not query the registry")

    monkeypatch.setattr(FrameworkFixRegistry, "lookup", boom)

    result = generator.generate(plan_id="p1", failures=[])

    assert result == {"tasks": [], "skipped_reason": None}


def test_held_fingerprint_comes_from_the_framework_fix_block(
    state_db: Path,
    plan_dir: Path,
    generator: RepairTaskGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有显式给指纹 → 从计划自己的 ``framework_fix`` 块里读。"""
    calls = _stub_content_call(monkeypatch)
    fingerprint = _register(plan_id="p1", vp_id="VP-3")
    PlanState(plan_dir).enter_block(
        "p1", fingerprint=fingerprint, fix_plan_id="fix-1",
    )

    result = generator.generate(
        plan_id="p1",
        failures=[{"vp_id": "VP-3"}],
    )

    assert result == {"tasks": [], "skipped_reason": BLOCKED}, (
        "计划挂起时，指纹在它自己的 framework_fix 块里；守卫必须读得到"
    )
    assert calls == []


# ---------------------------------------------------------------------------
# 内容生成这条路上也有守卫 —— 生产的两条门都走它
# ---------------------------------------------------------------------------
#
# 上面那些用例走的是 ``generate()``。生产里真正创建本地修复任务的两条门
# （编排器与终局裁决口）**不经过** ``generate()``：它们直接问
# ``generate_repair_contents`` 要内容。守卫只在 ``generate()`` 里的时候，
# 这两条门照样给同一个缺陷生成本地修复任务 —— 两份修复改同一批文件、
# 互相打架，「修复工作量恒为 1 份」就破了。
#
# 所以守卫必须住在 ``generate_repair_contents()`` 里：那是两条门与
# ``generate()`` 的公共必经点，一份判据，三条路。
def _failure(vp_id: str, fingerprint: str = "") -> Dict[str, Any]:
    """一条失败记录 —— 生产里失败记录的样子。"""
    record: Dict[str, Any] = {
        "id": vp_id,
        "title": f"{vp_id} failed",
        "priority": "high",
        "failure_class": "deliverable_defect",
        "actual_result": "the acceptance command failed",
        "evidence": "backend/app.py:1",
    }
    if fingerprint:
        record["fingerprint"] = fingerprint
    return record


def _query(state_db: Path, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    """查一张表，返回字典行。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _set_plan_kind(state_db: Path, plan_id: str, kind: str) -> None:
    """把计划的种类写成 ``kind``（``normal`` / ``framework_fix``）。

    种类住在路由表的 ``plan_kind`` 列上 —— 谁是修复计划，只有那一列说了算。
    """
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO plan_routing "
            "(plan_id, current_phase, version, updated_at) "
            "VALUES (?, ?, 0, ?)",
            (plan_id, "queued", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "UPDATE plan_routing SET plan_kind = ? WHERE plan_id = ?",
            (kind, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _no_fill_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「补问验收命令」那一轮关掉。

    它是**另一件事**（模型给缺命令的行补一条），与守卫无关；在这里留着它
    只会让模型多被问一次，用例要证明的就说不清了。
    """
    monkeypatch.setattr(
        RepairTaskGenerator,
        "_fill_missing_test_commands",
        lambda self, contents, failed_vps, round_number: {},
    )


def test_content_generation_never_asks_the_model_for_a_registered_defect(
    state_db: Path,
    generator: RepairTaskGenerator,
) -> None:
    """注册表里有这个指纹 → 一条内容都不生成，模型根本没被问。

    这是生产的两条门走的那条路。它们不经过 ``generate()``，所以守卫必须
    在 ``generate_repair_contents()`` 里也成立。
    """
    fingerprint = _register(plan_id="p1", vp_id="VP-3")

    contents = generator.generate_repair_contents(
        [_failure("VP-3", fingerprint=fingerprint)], 1,
    )

    assert contents == [], (
        "框架修复车道已经拥有这个缺陷 → 本地不该再修一份；实际 "
        f"{contents!r}"
    )


def test_content_generation_attaches_and_holds_the_plan(
    state_db: Path,
    plan_dir: Path,
    generator: RepairTaskGenerator,
) -> None:
    """命中 → 写挂接、挂起计划；跳过原因与读模型用同一个状态词。

    「命中即挂接」不是一句话：挂接关系只有注册表能表达（「谁在等这个修复」，
    一个计划目录看不见别的计划），挂起只有计划状态能表达（「这条计划停着」）。
    两样都写下来，这条失败才真的在等那个修复，而不是凭空少了一个任务。
    """
    fingerprint = _register(plan_id="p1", vp_id="VP-3")

    generator.generate_repair_contents(
        [_failure("VP-3", fingerprint=fingerprint)], 1,
    )

    rows = _query(
        state_db,
        "SELECT plan_id, vp_id, round, source FROM framework_fix_attach "
        "ORDER BY plan_id, vp_id",
    )
    assert [(row["plan_id"], row["vp_id"]) for row in rows] == [("p1", "VP-3")], (
        "命中的失败要挂到那个修复计划上；实际 {rows!r}".format(rows=rows)
    )
    assert rows[0]["round"] == 1, (
        "挂接要记着它发生在第几轮；实际 {rows!r}".format(rows=rows)
    )
    assert rows[0]["source"] == GUARD_ATTACH_SOURCE, (
        "挂接来源要说得清是谁挂的；实际 {rows!r}".format(rows=rows)
    )

    block = PlanState(plan_dir).get_framework_fix()
    assert block.get("hold") is True, (
        "计划要挂起 —— 它在等那个修复；实际 {block!r}".format(block=block)
    )
    assert block.get("fingerprint") == fingerprint, (
        "挂起块要记着挂它的是哪个缺陷；实际 {block!r}".format(block=block)
    )

    assert [item["reason"] for item in generator.blocked_vps] == [BLOCKED], (
        "被挡下的失败要说得出是哪一条、为什么"
    )


def test_unregistered_defect_still_reaches_the_model(
    state_db: Path,
    plan_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册表里没有这个指纹 → 照常问模型要内容。"""
    _no_fill_loop(monkeypatch)
    tool = _RecordingCodingTool()
    generator = RepairTaskGenerator(tool, plan_dir, tmp_path)

    contents = generator.generate_repair_contents(
        [_failure("VP-3", fingerprint="sha256:" + "9" * 64)], 1,
    )

    assert len(tool.calls) == 1, (
        "没命中注册表 → 失败照常进生成步骤；实际问了 "
        f"{len(tool.calls)} 次"
    )
    assert [content["failed_vp_id"] for content in contents] == ["VP-3"], (
        "放行的失败照常拿到内容"
    )
    assert _query(state_db, "SELECT * FROM framework_fix_attach") == [], (
        "没命中就没有挂接 —— 挂接是命中的后果，不是每轮的仪式"
    )


def test_the_fix_plan_is_not_blocked_by_its_own_guard(
    state_db: Path,
    plan_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这个计划就是那个修复计划 → 守卫不挡它。

    **修复计划不派生新修复计划**：一个签名一个修复计划，修复计划自己再撞上
    同一个签名时，它就是那个在修的人 —— 把它挂起等另一个修复，等于把唯一
    的那条修复线停在自己手里。
    """
    _no_fill_loop(monkeypatch)
    fingerprint = _register(plan_id="p1", vp_id="VP-3")
    FrameworkFixRegistry().link_fix_plan(fingerprint, "p1")
    _set_plan_kind(state_db, "p1", "framework_fix")
    tool = _RecordingCodingTool()
    generator = RepairTaskGenerator(tool, plan_dir, tmp_path)

    contents = generator.generate_repair_contents(
        [_failure("VP-3", fingerprint=fingerprint)], 1,
    )

    assert len(tool.calls) == 1, (
        "修复计划自己修这个缺陷 —— 守卫不挡它；实际问了 "
        f"{len(tool.calls)} 次"
    )
    assert [content["failed_vp_id"] for content in contents] == ["VP-3"]
    assert _query(state_db, "SELECT * FROM framework_fix_attach") == [], (
        "修复计划不该被挂起 —— 它就是那个在修的计划"
    )


def test_the_skip_reason_and_the_read_model_share_one_word() -> None:
    """挂起拒绝与读模型用同一个状态词 —— 没有第二套说法。

    生成侧说 ``blocked_on_framework_fix``、展示侧说另一个词的时候，操作员
    要在两套词之间翻译才能把两件事对上。这里钉住它们是同一个词。
    """
    assert SKIPPED_BLOCKED_ON_FRAMEWORK_FIX == READ_MODEL_BLOCKED, (
        "生成侧的跳过原因与展示侧的词必须一致；实际 "
        f"{SKIPPED_BLOCKED_ON_FRAMEWORK_FIX!r} != {READ_MODEL_BLOCKED!r}"
    )


def test_the_hold_of_one_failure_does_not_silence_another(
    state_db: Path,
    plan_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条命中的失败被挂起，不该把另一条本地失败一起消掉。

    守卫挂起计划时会写计划的挂起块。后面那一趟（内容生成那一趟）如果回头
    去读那个块，就会把「本来没有指纹」的失败算到刚挂上的那个缺陷头上 ——
    一条本地缺陷从此没人修，而且没人说得清它去哪了：它既没有修复任务，
    也不在「被挡下」名单里。

    这里走的是真的内容生成那条路（只有模型被换成记录器），因为要验的正是
    「两趟守卫之间不许漂」。
    """
    _no_fill_loop(monkeypatch)
    tool = _RecordingCodingTool()
    generator = RepairTaskGenerator(tool, plan_dir, tmp_path)
    fingerprint = _register(plan_id="p1", vp_id="VP-3")

    result = generator.generate(
        plan_id="p1",
        failures=[
            _failure("VP-3", fingerprint=fingerprint),
            _failure("VP-7"),
        ],
    )

    assert result["skipped_reason"] == BLOCKED, (
        "命中的那条被挡下了，原因要说得出"
    )
    assert len(tool.calls) == 1, (
        "没命中的那条照常问模型；实际问了 " f"{len(tool.calls)} 次"
    )
    prompt = str(tool.calls[0]["kwargs"].get("prompt") or "")
    assert "VP-7" in prompt, (
        "没命中的那条要真的送到模型面前"
    )
    assert "VP-3" not in prompt, (
        "命中的那条不许出现在模型面前 —— 它归框架修复车道"
    )
