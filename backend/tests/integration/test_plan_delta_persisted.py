"""验证计划增量的**原子持久化**（``apply_delta_persisted``）。

背景
----
``apply_delta`` 只改内存：它把新 VP 追加进 ``verification_points``、给旧 VP
打 ``obsolete`` 标记，全程没有任何文件 I/O。``verification_plan.json`` 只有
"首次生成"那一条写路径，于是**复用路径**（从磁盘读入已有计划 → 评估增量 →
直接返回）每轮都把上一轮的作废标记丢掉：判据作废在下一轮复活，替换 VP 也
跟着消失，修复任务照旧围绕一条已经不成立的判据生成。

本模块钉住的是新入口 :func:`apply_delta_persisted` 的契约：

* **copy-then-swap** —— 增量先应用在深拷贝上，落盘成功之后才让调用方持有的
  ``plan_data`` 生效；写失败时内存里**没有**任何标记（内存不领先磁盘）。
* **原子替换** —— 写盘走 ``utils.atomic_io.atomic_write_json``（临时文件 +
  fsync + ``os.replace``），读者永远看到完整的旧文件或完整的新文件。
* **写前 JSON 往返校验** —— 写下去的就是校验过的那一份；``Path`` /
  ``datetime`` 之类混进来时拒绝落盘，磁盘文件一个字节都不变。
* **写前摘要复核** —— ``expected_digest`` 与计划文件当前内容不一致时抛
  :class:`PlanChangedDuringDelta`，不覆盖别人的改动。
* **空增量不写盘** —— 全部被拒时既不写文件也不动内存。
* **护栏在落盘之前生效** —— ``post_apply`` 拿到的候选计划还没进磁盘。

为什么用真实文件而不是内存 dict
-------------------------------
原子性、"字节不变"、"mtime 不变"这些性质只在真实文件上才存在。用内存 dict
替身会把"写失败了"这件事藏起来 —— 那正是本模块要消灭的失败形态。

测试命令::

    cd backend && .venv/bin/python3 -m pytest tests/integration/test_plan_delta_persisted.py -q
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Make ``verification_plan_delta`` importable when pytest is invoked from
# the project root (``backend/`` is the package root for these modules).
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vp(vp_id: str, **extra: Any) -> Dict[str, Any]:
    """一条最小可用的 VP。"""
    vp: Dict[str, Any] = {
        "id": vp_id,
        "title": f"验证点 {vp_id}",
        "verification_method": "api_test",
        "related_prd_criteria": "验收标准 1",
        "expected_result": "验证通过",
        "verification_phase": 1,
    }
    vp.update(extra)
    return vp


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_plan(path: Path, plan: Dict[str, Any]) -> str:
    """把计划写到磁盘，返回内容摘要（调用方的 ``expected_digest``）。"""
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return _sha256(path)


def _read_plan(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_id(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(vp.get("id")): vp
        for vp in plan.get("verification_points") or []
        if isinstance(vp, dict)
    }


def _replacement_delta(replacement_for: str = "VP-002") -> Any:
    """一次"作废 VP-002 + 新增一条替换 VP"的增量。"""
    from verification_plan_delta import PlanDelta

    return PlanDelta(
        added=[{
            "title": "替换 VP-002 的判据",
            "verification_method": "api_test",
            "related_prd_criteria": "验收标准 2",
            "expected_result": "新接口返回 200",
            "reason": f"替换 {replacement_for}：原判据与实现不一致",
            "replacement_for": replacement_for,
        }],
        obsoleted=[{
            "id": replacement_for,
            "reason": "判据缺陷：断言与实现不一致",
        }],
    )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_persist_roundtrip(tmp_path: Path) -> None:
    """落盘后磁盘同时含作废标记与替换指针，且内存与磁盘一致。

    这是本模块的正控：写成功时，"作废 → 替换"这一对必须**一起**落到磁盘上
    —— 旧 VP 打标记（不删除，保留审计痕迹），新 VP 记 ``replacement_for``
    指回被它替换掉的那条。
    """
    from verification_plan_delta import apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)

    seen: Dict[str, Any] = {}

    def guard(candidate: Dict[str, Any], summary: Dict[str, Any]) -> None:
        # 护栏必须发生在**落盘之前**：此刻磁盘上还是旧内容。
        seen["digest_at_guard"] = _sha256(plan_file)
        seen["added_at_guard"] = [a["id"] for a in summary["added"]]
        seen["candidate_ids"] = [
            str(vp.get("id"))
            for vp in candidate.get("verification_points") or []
        ]

    summary = apply_delta_persisted(
        plan_file, plan_data, _replacement_delta(), 4,
        post_apply=guard, expected_digest=digest,
    )

    # 护栏在写盘之前跑过，看到的是候选计划而不是磁盘内容。
    assert seen["digest_at_guard"] == digest
    assert seen["added_at_guard"] == ["VP-003"]
    assert seen["candidate_ids"] == ["VP-001", "VP-002", "VP-003"]

    # 摘要描述的是落盘内容。
    assert [a["id"] for a in summary["added"]] == ["VP-003"]
    assert [o["id"] for o in summary["obsoleted"]] == ["VP-002"]

    on_disk = _read_plan(plan_file)
    by_id = _by_id(on_disk)

    # 旧 VP：打标记、不删除。
    assert by_id["VP-002"]["obsolete"] is True
    assert by_id["VP-002"]["obsolete_reason"] == "判据缺陷：断言与实现不一致"
    assert by_id["VP-002"]["obsoleted_round"] == 4
    assert "obsolete" not in by_id["VP-001"]

    # 新 VP：指向被它替换掉的那条。
    assert by_id["VP-003"]["replacement_for"] == "VP-002"

    # 内存与磁盘一致（copy-then-swap 的另一半：写成功之后才换内存）。
    assert plan_data == on_disk

    # 原子替换不留残骸：目录里只有那个计划文件，没有半个临时文件。
    assert [p.name for p in tmp_path.iterdir()] == ["verification_plan.json"]


def test_bare_apply_delta_does_not_persist(tmp_path: Path) -> None:
    """负控：裸 ``apply_delta`` 依然只改内存 —— 这正是本模块要修的缺陷。

    没有这一条，"落盘了"这件事就只是"内存里看见了"：哪天有人把入口换回
    裸 :func:`apply_delta`，上面的用例会一起变红，而这一条会先指出原因。
    """
    from verification_plan_delta import apply_delta

    plan_file = tmp_path / "verification_plan.json"
    _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)

    apply_delta(plan_data, _replacement_delta(), 4)

    # 内存变了，磁盘没变。
    assert _by_id(plan_data)["VP-002"]["obsolete"] is True
    assert _sha256(plan_file) == before


def test_write_failure_keeps_memory(tmp_path: Path, monkeypatch) -> None:
    """写盘失败 → 抛错，且内存里没有任何标记（内存不领先磁盘）。"""
    from utils import atomic_io
    from verification_plan_delta import PlanDeltaPersistError, apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io, "atomic_write_json", boom)

    with pytest.raises(PlanDeltaPersistError):
        apply_delta_persisted(
            plan_file, plan_data, _replacement_delta(), 4,
            expected_digest=digest,
        )

    # 磁盘一个字节都没变。
    assert _sha256(plan_file) == before

    # 内存里既没有作废标记，也没有多出来的 VP —— 调用方拿到的还是原计划。
    assert all("obsolete" not in vp for vp in plan_data["verification_points"])
    assert [vp["id"] for vp in plan_data["verification_points"]] == [
        "VP-001", "VP-002",
    ]


def test_non_serializable_rejected(tmp_path: Path) -> None:
    """``Path`` / ``datetime`` 混进增量 → 往返校验失败，磁盘零写入。

    写盘之前先做一次 JSON 往返：写下去的就是校验过的那一份。混进
    ``Path`` / ``datetime`` 这类不可序列化的对象时，磁盘必须保持原样 ——
    半个计划文件比旧计划更糟。
    """
    from verification_plan_delta import PlanDeltaPersistError, apply_delta_persisted

    for poison in (Path("/etc/hosts"), datetime(2026, 10, 21)):
        plan_file = tmp_path / f"verification_plan_{type(poison).__name__}.json"
        digest = _write_plan(plan_file, {
            "plan_id": "p-1",
            "verification_points": [_vp("VP-001"), _vp("VP-002")],
        })
        plan_data = _read_plan(plan_file)
        before = _sha256(plan_file)

        delta = _replacement_delta()
        # ``assertions`` 是原样搬进新 VP 的透传字段，毒药落在那里。
        delta.added[0]["assertions"] = [poison]

        with pytest.raises(PlanDeltaPersistError):
            apply_delta_persisted(
                plan_file, plan_data, delta, 4, expected_digest=digest,
            )

        assert _sha256(plan_file) == before
        assert all("obsolete" not in vp for vp in plan_data["verification_points"])


def test_lossy_roundtrip_rejected(tmp_path: Path) -> None:
    """能序列化但**有损**的值（元组）→ 往返校验失败，磁盘零写入。

    ``json.dumps`` 不认识元组 —— 它会把它写成数组。于是"写下去的那份"和
    "调用方手里那份"已经不是同一份计划了：落盘等于悄悄换了一份计划。
    往返等值核对把这一类挡在写盘之前。
    """
    from verification_plan_delta import PlanDeltaPersistError, apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)

    delta = _replacement_delta()
    delta.added[0]["assertions"] = [(1, 2)]  # 元组：能 dumps，但往返后变了

    with pytest.raises(PlanDeltaPersistError):
        apply_delta_persisted(
            plan_file, plan_data, delta, 4, expected_digest=digest,
        )

    assert _sha256(plan_file) == before
    assert all("obsolete" not in vp for vp in plan_data["verification_points"])


def test_roundtrip_failure_raises_persist_error(tmp_path: Path) -> None:
    """往返校验崩在别的异常上（深到编码器自己递归爆掉）→ 也要抛 ``PlanDeltaPersistError``。

    契约是"第 2/3/5/6/7 步任一异常 → 抛 :class:`PlanDeltaPersistError`"。
    ``json.dumps`` 除了 ``TypeError`` / ``ValueError`` 还会抛
    ``RecursionError``（结构深到编码器自己撑不住）—— 它漏出去，调用方拿到的
    就是个裸异常，而不是"这次增量没有落盘"这个可处理的信号。
    """
    from verification_plan_delta import PlanDeltaPersistError, apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)

    # 深到 JSON 编码器自己递归爆掉（json.dumps / deepcopy 都会 RecursionError）。
    deep: Any = {}
    cursor = deep
    for _ in range(2000):
        cursor["n"] = {}
        cursor = cursor["n"]

    delta = _replacement_delta()
    delta.added[0]["assertions"] = [deep]

    with pytest.raises(PlanDeltaPersistError):
        apply_delta_persisted(
            plan_file, plan_data, delta, 4, expected_digest=digest,
        )

    assert _sha256(plan_file) == before
    assert all("obsolete" not in vp for vp in plan_data["verification_points"])


def test_digest_mismatch(tmp_path: Path) -> None:
    """外部改写计划文件 → 抛 ``PlanChangedDuringDelta``，且不覆盖外部改动。

    摘要是"读入时记下、写回前复核"的那道闸：计划在增量评估期间被别人改过，
    此时写回会覆盖掉别人的改动，所以宁可放弃这一轮增量。
    """
    from verification_plan_delta import PlanChangedDuringDelta, apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)

    # 另一个写者改了同一个文件。
    external = {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001", title="被外部改写")],
    }
    plan_file.write_text(
        json.dumps(external, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    external_digest = _sha256(plan_file)

    with pytest.raises(PlanChangedDuringDelta):
        apply_delta_persisted(
            plan_file, plan_data, _replacement_delta(), 4,
            expected_digest=digest,
        )

    # 外部改动没被覆盖，也没被半途截断。
    assert _sha256(plan_file) == external_digest
    assert _read_plan(plan_file) == external

    # 内存没动。
    assert all("obsolete" not in vp for vp in plan_data["verification_points"])


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------


def test_rejected_delta_does_not_write(
    tmp_path: Path, monkeypatch,
) -> None:
    """全部被拒 → 不写盘：内容和 mtime 都不变，摘要为空。"""
    from utils import atomic_io
    from verification_plan_delta import PlanDelta, apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)
    mtime_before = plan_file.stat().st_mtime_ns

    calls: List[Any] = []
    monkeypatch.setattr(
        atomic_io, "atomic_write_json",
        lambda *args, **kwargs: calls.append(args),
    )

    delta = PlanDelta(
        rejected_additions=[{"item": {"title": ""}, "why": "缺少 title"}],
        rejected_obsoletes=[{"item": {"id": "VP-404"}, "why": "不存在"}],
    )
    summary = apply_delta_persisted(
        plan_file, plan_data, delta, 4, expected_digest=digest,
    )

    assert calls == []
    assert summary["added"] == []
    assert summary["obsoleted"] == []
    assert _sha256(plan_file) == before
    assert plan_file.stat().st_mtime_ns == mtime_before


def test_guard_rejection_does_not_write(
    tmp_path: Path, monkeypatch,
) -> None:
    """护栏在落盘前生效：替换 VP 被拒 → 整对（作废 + 替换）都不落盘。

    "作废原 VP + 新增替换 VP" 是一个原子对：替换没通过护栏，原 VP 就不能
    先被作废 —— 否则那条判据就死了，而且没人接替它。
    """
    from utils import atomic_io
    from verification_plan_delta import PlanDelta, apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)

    calls: List[Any] = []
    monkeypatch.setattr(
        atomic_io, "atomic_write_json",
        lambda *args, **kwargs: calls.append(args),
    )

    # schema 不合法的替换（缺 ``verification_method``）。
    delta = PlanDelta(
        added=[{
            "title": "替换 VP-002 的判据",
            "reason": "替换 VP-002：原判据缺陷",
            "replacement_for": "VP-002",
        }],
        obsoleted=[{"id": "VP-002", "reason": "判据缺陷"}],
    )

    def guard(candidate: Dict[str, Any], summary: Dict[str, Any]) -> None:
        """模拟"method / schema 护栏"：不合格的替换整条丢弃。"""
        kept = [
            vp for vp in candidate.get("verification_points") or []
            if vp.get("verification_method")
        ]
        candidate["verification_points"] = kept
        summary["added"] = []
        # 作废与替换成对：替换没了，作废也一并撤回。
        for vp in kept:
            vp.pop("obsolete", None)
            vp.pop("obsolete_reason", None)
            vp.pop("obsoleted_round", None)
        summary["obsoleted"] = []

    summary = apply_delta_persisted(
        plan_file, plan_data, delta, 4,
        post_apply=guard, expected_digest=digest,
    )

    assert calls == []
    assert summary["added"] == []
    assert summary["obsoleted"] == []
    assert _sha256(plan_file) == before
    assert all("obsolete" not in vp for vp in plan_data["verification_points"])


def test_summary_reports_only_landed_obsoletions(
    tmp_path: Path, monkeypatch,
) -> None:
    """护栏撤回作废标记 → 摘要不得声称那条 VP 已被作废，且不写盘。

    摘要的承诺是"``added`` / ``obsoleted`` 描述的是**落盘后**的内容"。
    护栏否掉替换 VP 时必须连带撤回对原 VP 的作废（"作废 + 替换"是一个原子
    对）—— 护栏改了候选计划却忘了同步摘要时，摘要里再留着"已作废 VP-002"
    就是撒谎：落盘后的磁盘上那条 VP 根本没有作废标记。

    这种情况下**什么都没落盘**，所以也就不该写盘（写出来的内容与原来一字
    不差，白写一次；而且写的那一次是拿"已作废"的说法换来的）。
    """
    from utils import atomic_io
    from verification_plan_delta import apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)
    before = _sha256(plan_file)

    calls: List[Any] = []
    monkeypatch.setattr(
        atomic_io, "atomic_write_json",
        lambda *args, **kwargs: calls.append(args),
    )

    def guard(candidate: Dict[str, Any], summary: Dict[str, Any]) -> None:
        """否掉替换 VP 并撤回作废 —— 但没有同步 ``summary``。"""
        candidate["verification_points"] = [
            vp for vp in candidate["verification_points"]
            if vp.get("id") != "VP-003"
        ]
        for vp in candidate["verification_points"]:
            vp.pop("obsolete", None)
            vp.pop("obsolete_reason", None)
            vp.pop("obsoleted_round", None)

    summary = apply_delta_persisted(
        plan_file, plan_data, _replacement_delta(), 4,
        post_apply=guard, expected_digest=digest,
    )

    # 摘要描述的是落盘后的内容 —— 落盘后的内容里 VP-002 没有被作废。
    assert summary["added"] == []
    assert summary["obsoleted"] == []

    # 什么都没落盘 → 不写盘。
    assert calls == []
    assert _sha256(plan_file) == before
    assert all("obsolete" not in vp for vp in plan_data["verification_points"])


def test_persist_uses_atomic_writer_with_reraise(
    tmp_path: Path, monkeypatch,
) -> None:
    """落盘必须走 ``atomic_io.atomic_write_json``，且必须带 ``reraise=True``。

    这是"内存不领先磁盘"这条底线的**接线钉**。``atomic_write_json`` 默认
    ``reraise=False``：``OSError`` 会被它记进日志然后**吞掉**。一旦这里的
    调用漏掉 ``reraise=True``，写盘失败就会变成"静默成功"—— 函数照常返回、
    磁盘还是旧内容、内存却已经换成了新计划：**内存领先磁盘**，也就是这个
    入口存在的全部理由被推翻，而且上面所有用例都还是绿的（它们断言的是
    抛错路径）。

    所以这条不测行为、只测接线：写盘走的是共享的原子写入口，且失败会浮
    上来。
    """
    from utils import atomic_io
    from verification_plan_delta import apply_delta_persisted

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001"), _vp("VP-002")],
    })
    plan_data = _read_plan(plan_file)

    calls: List[Any] = []
    original = atomic_io.atomic_write_json

    def spy(path: Any, payload: Any, **kwargs: Any) -> None:
        calls.append((path, payload, kwargs))
        return original(path, payload, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_json", spy)

    apply_delta_persisted(
        plan_file, plan_data, _replacement_delta(), 4, expected_digest=digest,
    )

    assert len(calls) == 1, "落盘必须且只能走一次共享的原子写入口"
    written_path, written_payload, written_kwargs = calls[0]
    assert Path(written_path) == plan_file
    # 失败必须浮上来：被吞掉的话"写失败"就变成了"静默成功"。
    assert written_kwargs.get("reraise") is True
    # 写下去的是校验过的那一份（含作废标记与替换指针）。
    by_id = {
        str(vp.get("id")): vp
        for vp in written_payload.get("verification_points") or []
    }
    assert by_id["VP-002"]["obsolete"] is True
    assert by_id["VP-003"]["replacement_for"] == "VP-002"


def test_plan_file_digest_semantics(tmp_path: Path) -> None:
    """``plan_file_digest`` 的三种结果各有各的下场，这里逐个钉住。

    它是 ``expected_digest`` 复核那道闸的底座：读入计划时记下它，写回前
    再算一次，比不上就放弃这一轮增量。

    * 正常文件 → sha256 十六进制（能直接当摘要比）；
    * 文件不存在 → ``None``（"文件没了"与"没变化"是两回事：与调用方手里
      记的摘要必然对不上 → 放弃这一轮，而不是当成"没被改过"）；
    * 读不动（这里是目录）→ 抛 :class:`PlanDeltaPersistError`，不能悄悄
      当成"没变化"。
    """
    from verification_plan_delta import PlanDeltaPersistError, plan_file_digest

    plan_file = tmp_path / "verification_plan.json"
    digest = _write_plan(plan_file, {
        "plan_id": "p-1",
        "verification_points": [_vp("VP-001")],
    })

    assert plan_file_digest(plan_file) == digest
    assert len(plan_file_digest(plan_file)) == 64

    # 文件不存在 → None。
    assert plan_file_digest(tmp_path / "does-not-exist.json") is None

    # 读不动 → 抛错（目录不是文件）。
    with pytest.raises(PlanDeltaPersistError):
        plan_file_digest(tmp_path)

