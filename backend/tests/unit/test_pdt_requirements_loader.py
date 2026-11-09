"""加载层单测：部署级「操作者要求」文件（``PDT.md`` 家族）的读取契约。

被测模块
--------
``backend/pdt_requirements.py`` —— 部署根目录下三层要求文件的**唯一**读取入口：
``PDT.md``（base）、``PDT.<stage>.md``（阶段）、``PDT.<scene>.md``（精确到单个
scene）。本文件只覆盖加载层：解析顺序、stage 映射、8 KB 上限、mtime 缓存失效、
以及 ``server`` 不可导入时的回落。渲染与注入不在本文件范围内。

契约（2026-11-03 用户要求注入机制 —— arch 决策点 1 / test 决策点 1）
-------------------------------------------------------------------

* 顺序固定 base → stage → scene，三层各自独立可缺；
* stage 由 scene 查 :data:`STAGE_BY_SCENE` 得到：16 个 scene 有阶段归属，
  ``summarizer`` **故意**不归属任何阶段；
* 未知 scene 只吃 base（安全回落，不报错）；
* 单文件超过 8 KB 截断到字符边界并记一条 WARNING，不抛异常；
* 缓存键 ``(path, mtime_ns, size)``：键不变不重读盘，键一变立刻读到新内容；
* 计划级（plan-level）文件本版不做，本文件也不期望它出现。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pdt_requirements
from pdt_requirements import STAGE_BY_SCENE, candidate_files, load_layers


#: 全仓唯一的一张 scene → stage 映射表（逐项照抄实现侧）。
MAPPED_SCENES = {
    # --- plan ---
    "prd": "plan",
    "prd_refine": "plan",
    "arch": "plan",
    "arch_refine": "plan",
    "test_design": "plan",
    "test_refine": "plan",
    "tasks_generation": "plan",
    "interview": "plan",
    "planning": "plan",
    "refiner": "plan",
    # --- execution ---
    "execution": "execution",
    "service_restart": "execution",
    # --- verification ---
    "verification": "verification",
    "verification_plan_delta": "verification",
    "verification_command_audit": "verification",
    "audit_second_pass": "verification",
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把加载器的部署根目录指到一个空的临时目录。"""
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: tmp_path)
    return tmp_path


def test_order_base_stage_scene(root):
    """三文件齐 → 顺序 base → stage → scene。"""
    (root / "PDT.md").write_text("base", encoding="utf-8")
    (root / "PDT.verification.md").write_text("stage", encoding="utf-8")
    (root / "PDT.verification_plan_delta.md").write_text("scene", encoding="utf-8")

    layers = load_layers("verification_plan_delta")

    assert [p.name for p, _ in layers] == [
        "PDT.md",
        "PDT.verification.md",
        "PDT.verification_plan_delta.md",
    ]
    assert [text for _, text in layers] == ["base", "stage", "scene"]


def test_mapping_table(root):
    """16 个已映射 scene 逐一断言 stage，映射表不多不少。"""
    assert STAGE_BY_SCENE == MAPPED_SCENES

    for scene, stage in MAPPED_SCENES.items():
        assert STAGE_BY_SCENE[scene] == stage
        names = [p.name for p in candidate_files(scene)]
        # stage 文件与 scene 文件同名时（scene == stage）只出现一次。
        expected = list(dict.fromkeys(["PDT.md", f"PDT.{stage}.md", f"PDT.{scene}.md"]))
        assert names == expected, f"{scene} 的候选文件顺序不对"


def test_summarizer_base_only(root):
    """``summarizer`` 故意不归属任何阶段 → 只吃 base。"""
    assert "summarizer" not in STAGE_BY_SCENE

    (root / "PDT.md").write_text("base", encoding="utf-8")
    (root / "PDT.summarizer.md").write_text("scene", encoding="utf-8")

    layers = load_layers("summarizer")

    assert [p.name for p, _ in layers] == ["PDT.md"]
    assert layers[0][1] == "base"


def test_unknown_scene_base_only(root):
    """未知 scene → 只吃 base，不报错。"""
    (root / "PDT.md").write_text("base", encoding="utf-8")
    (root / "PDT.no_such_scene.md").write_text("scene", encoding="utf-8")

    layers = load_layers("no_such_scene")

    assert [p.name for p, _ in layers] == ["PDT.md"]


def test_no_files_returns_empty(root):
    """一个文件都不存在 → 空列表。"""
    assert load_layers("planning") == []
    assert load_layers("summarizer") == []


def test_layers_are_independently_optional(root):
    """三层各自独立可缺：只存在其中一层时，那一层照样被返回。"""
    scene = "verification_plan_delta"
    base, stage, scene_file = (
        "PDT.md",
        "PDT.verification.md",
        "PDT.verification_plan_delta.md",
    )

    (root / base).write_text("base", encoding="utf-8")
    assert [p.name for p, _ in load_layers(scene)] == [base]
    (root / base).unlink()

    (root / stage).write_text("stage", encoding="utf-8")
    assert [p.name for p, _ in load_layers(scene)] == [stage]
    (root / stage).unlink()

    (root / scene_file).write_text("scene", encoding="utf-8")
    assert [p.name for p, _ in load_layers(scene)] == [scene_file]


def test_truncate_8kb(root, caplog):
    """单文件 > 8192 字节 → 截断到字符边界并记一条 WARNING。"""
    (root / "PDT.md").write_text("x" * 9000, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pdt_requirements"):
        layers = load_layers("planning")

    assert len(layers) == 1
    text = layers[0][1]
    assert len(text.encode("utf-8")) <= 8192
    assert text == "x" * 8192

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "超限文件必须记一条 WARNING"
    assert any(r.name == "pdt_requirements" for r in warnings)


def test_cache_invalidated_on_mtime(root, monkeypatch):
    """缓存键含 mtime：文件改了（大小不变）下一次调用即生效。"""
    target = root / "PDT.md"
    target.write_text("first", encoding="utf-8")
    assert load_layers("planning")[0][1] == "first"

    reads = []
    original_open = Path.open

    def counting_open(self, mode="r", *args, **kwargs):
        if self == target and "r" in mode:
            reads.append(self)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    # 键不变 → 不重读盘。
    assert load_layers("planning")[0][1] == "first"
    assert reads == []

    # 内容变了但大小相同 → 只有 mtime 变 → 键变 → 重读盘并拿到新内容。
    target.write_text("other", encoding="utf-8")
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert load_layers("planning")[0][1] == "other"
    assert len(reads) == 1


def test_deployment_root_falls_back(monkeypatch, caplog):
    """``import server`` 失败 → 回落到包目录的上一级，不抛异常。"""
    monkeypatch.setitem(sys.modules, "server", None)

    with caplog.at_level(logging.WARNING, logger="pdt_requirements"):
        resolved = pdt_requirements._deployment_root()

    assert resolved == Path(pdt_requirements.__file__).resolve().parent.parent
    assert any(
        r.levelno == logging.WARNING and r.name == "pdt_requirements"
        for r in caplog.records
    )
