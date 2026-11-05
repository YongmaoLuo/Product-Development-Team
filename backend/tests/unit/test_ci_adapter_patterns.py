"""CI 适配脚本候选表（2026-11-05，arch 决策点 1 —— 生成侧候选表）。

契约来源：缺口反馈把**首个**命中的 CI 候选当推荐证据（``find_missing_gates``
的 ``head = kind_entries[0]``，``render_gap_feedback`` 把它写进重生成提示）。
候选表首项是 ``scripts/ci_local.py`` —— 不是本项目约定的适配脚本
``scripts/ci_gate.sh``。于是提示指向一条谁都不会用的入口，重生成回路照着
填出来的 ``ci_entry`` 也就照着错。

本文件钉住三道缝：
  * ``scripts/ci_gate.sh`` 排在候选表第一位；
  * 没有它的时候回落原有候选，顺序不变；
  * 一级子目录里的适配脚本同样被认出来（扫描根含一级子目录）。
"""

from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_plan_completeness import (  # noqa: E402
    _CI_ENTRY_PATTERNS,
    detect_full_gate_entries,
)


def _touch(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _ci_evidence(root: Path):
    """项目里所有 CI 候选入口的 evidence，按候选表顺序。"""
    return [
        e.evidence for e in detect_full_gate_entries(root) if e.kind == "ci"
    ]


def test_ci_gate_script_is_first_candidate(tmp_path):
    """两者都在时，首项必须是适配脚本 scripts/ci_gate.sh。"""
    _touch(tmp_path, "scripts/ci_gate.sh")
    _touch(tmp_path, "scripts/ci_local.py")

    assert _ci_evidence(tmp_path)[0] == "scripts/ci_gate.sh"


def test_other_candidates_still_detected(tmp_path):
    """没有适配脚本时，原有候选照样命中（不被新首项挤掉）。"""
    _touch(tmp_path, "scripts/ci_local.py")

    assert _ci_evidence(tmp_path) == ["scripts/ci_local.py"]


def test_ci_gate_script_is_only_candidate(tmp_path):
    """只有适配脚本时命中的就是它。"""
    _touch(tmp_path, "scripts/ci_gate.sh")

    assert _ci_evidence(tmp_path) == ["scripts/ci_gate.sh"]


def test_fallback_order_is_unchanged_without_adapter(tmp_path):
    """回落时原有候选的相对顺序不变（ci_local.py 在 ci_prgate.py 之前）。"""
    _touch(tmp_path, "scripts/ci_local.py")
    _touch(tmp_path, "scripts/ci_prgate.py")

    assert _ci_evidence(tmp_path) == [
        "scripts/ci_local.py", "scripts/ci_prgate.py",
    ]


def test_ci_gate_script_in_first_level_subdir(tmp_path):
    """一级子目录扫描同样生效，evidence 带子目录前缀。"""
    _touch(tmp_path, "frontend-app/scripts/ci_gate.sh")

    assert _ci_evidence(tmp_path) == ["frontend-app/scripts/ci_gate.sh"]


def test_adapter_pattern_is_first_in_table():
    """接口契约：候选表首项就是 scripts/ci_gate.sh。"""
    assert _CI_ENTRY_PATTERNS[0] == "scripts/ci_gate.sh"
