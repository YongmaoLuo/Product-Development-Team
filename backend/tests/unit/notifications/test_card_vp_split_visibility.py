"""2026-09-14 卡片/接口暴露 VP 拆分。

用户关心的可见性: 一个只做了拆分的轮次没有修复任务, 如果卡片上什么都不
显示, 操作者会以为这一轮是空转。本文件钉住两条：

  * ``_load_vp_splits_for_progress`` —— 从 ``verification_plan.json``
    的 ``superseded_by`` 派生拆分记录 (跨重启持久), 并用内存里的
    judge 记录补 ``hint``;
  * ``_verification_sections`` —— 渲染 "🔀 已拆分 VP" 区块, 父 VP →
    子 VP 列表 + 维度/原因。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import server as server_mod
from notifications.cards import _verification_sections


PLAN_ID = "plan-split-card"


def _line_text(elements) -> str:
    return "\n".join(
        e.get("text", {}).get("content", "")
        for e in elements
        if isinstance(e, dict)
    )


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans" / PLAN_ID
    d.mkdir(parents=True)
    monkeypatch.setattr(server_mod, "PLANS_DIR", tmp_path / "plans")
    server_mod._verification_state.pop(PLAN_ID, None)
    yield d
    server_mod._verification_state.pop(PLAN_ID, None)


def _write_plan(d: Path, entries) -> None:
    (d / "verification_plan.json").write_text(
        json.dumps({"verification_points": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# server-side loader
# ---------------------------------------------------------------------------

def test_loader_reads_split_from_plan_file(plan_dir):
    _write_plan(plan_dir, [
        {"id": "VP-006", "title": "函数体 diff"},
        {
            "id": "VP-023", "title": "Nightly CI 全过",
            "superseded_by": ["VP-023-1", "VP-023-2"],
            "split_reason": "整树 18 分钟",
        },
        {"id": "VP-023-1", "title": "拆分子 VP 1", "parent_vp_id": "VP-023"},
    ])
    splits = server_mod._load_vp_splits_for_progress(PLAN_ID)
    assert len(splits) == 1
    assert splits[0]["vp_id"] == "VP-023"
    assert splits[0]["child_vp_ids"] == ["VP-023-1", "VP-023-2"]
    assert splits[0]["reason"] == "整树 18 分钟"


def test_loader_empty_when_nothing_split(plan_dir):
    _write_plan(plan_dir, [{"id": "VP-006", "title": "T"}])
    assert server_mod._load_vp_splits_for_progress(PLAN_ID) == []


def test_loader_survives_missing_plan_file(plan_dir):
    """No plan file on disk → empty list, never an exception (the card
    builder must not blow up on a plan mid-generation)."""
    assert server_mod._load_vp_splits_for_progress(PLAN_ID) == []


def test_loader_merges_in_memory_hint(plan_dir):
    """Plan-file record carries the reason; the in-memory judge record
    carries the split dimension — merge instead of dropping either."""
    _write_plan(plan_dir, [{
        "id": "VP-023", "title": "Nightly CI 全过",
        "superseded_by": ["VP-023-1"], "split_reason": "太慢",
    }])
    server_mod._verification_state[PLAN_ID] = {
        "vp_splits": [{
            "vp_id": "VP-023", "child_vp_ids": ["VP-023-1"],
            "hint": "by_directory", "reason": "太慢",
        }],
    }
    splits = server_mod._load_vp_splits_for_progress(PLAN_ID)
    assert len(splits) == 1
    assert splits[0]["hint"] == "by_directory"
    assert splits[0]["reason"] == "太慢"


def test_loader_includes_memory_only_split(plan_dir):
    """A split whose plan write has not landed yet still shows (the
    in-memory record is the only source during that window)."""
    server_mod._verification_state[PLAN_ID] = {
        "vp_splits": [{
            "vp_id": "VP-030", "child_vp_ids": ["VP-030-1"],
            "hint": "by_file_group", "reason": "in-flight",
        }],
    }
    splits = server_mod._load_vp_splits_for_progress(PLAN_ID)
    assert [s["vp_id"] for s in splits] == ["VP-030"]
    assert splits[0]["hint"] == "by_file_group"


# ---------------------------------------------------------------------------
# card rendering
# ---------------------------------------------------------------------------

def _progress(vp_splits, **over) -> dict:
    base = {
        "verification_status": "failed",
        "verification_round": 2,
        "max_rounds": 3,
        "vps": [],
        "counts": {"total": 30, "completed": 28, "failed": 0,
                   "skipped": 1, "in_progress": 0},
        "repair_tasks": [],
        "vp_splits": vp_splits,
    }
    base.update(over)
    return base


def test_card_renders_split_block_without_repair_tasks():
    """The pure-split case: no repair tasks, but the split must be
    visible (otherwise the round looks like a no-op)."""
    elements = _verification_sections(_progress([{
        "vp_id": "VP-023",
        "title": "Nightly CI 全过",
        "child_vp_ids": ["VP-023-1", "VP-023-2"],
        "hint": "by_directory",
        "reason": "整个 tests/ 套件 18 分钟",
    }]), False)
    text = _line_text(elements)
    assert "🔀 已拆分 VP (1)" in text
    assert "`VP-023`" in text
    assert "Nightly CI 全过" in text
    assert "`VP-023-1`" in text and "`VP-023-2`" in text
    assert "by_directory" in text
    assert "18 分钟" in text


def test_card_omits_split_block_when_none():
    elements = _verification_sections(_progress([]), False)
    assert "已拆分 VP" not in _line_text(elements)


def test_card_handles_malformed_split_entries():
    """Defensive: junk entries must not crash the card builder."""
    elements = _verification_sections(
        _progress([{"vp_id": "VP-023"}, "not-a-dict", {}]), False,
    )
    text = _line_text(elements)
    assert "已拆分 VP (3)" in text
    assert "（无子 VP）" in text
