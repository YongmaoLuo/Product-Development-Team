"""Phase 1 提示词必须真的携带改动面（缺陷 B 第 1 条后半）。

为什么要有这组测试
------------------
任务 8 把 ``tasks.json`` 里全部任务的 ``files_to_modify`` 并集解析成了结构化的
改动面（文件清单 + 分类 + 是否触及界面）。但**解析出来不等于用上了** —— 如果
改动面只停在解析这一步，生成器仍然只看 PRD / 架构 / 测试文档，于是：

* 纯后端改动照样生成 ``ui_validation``（要起浏览器、要占机器）；
* 本地 CLI 改动被当成服务型改动（要起服务）。

所以改动面必须**进入生成器的输入**。这组测试钉的就是"提示词里有没有改动面"，
不是"提示词让 LLM 产出什么" —— 后者由任务 16 负责。

四条边界
--------
* **有改动面** → 提示词里有 ``## 本次改动面`` 段，段里能看到文件与分类；
* **空改动面** → 提示词与"不传改动面"**逐字节相同**（这次改动不能动到老
  提示词 —— 否则所有既有生成路径的提示词都被改过）；
* **文件多于 50 个** → 只列前 50 个，并写明还剩多少（全量列出来会让提示词
  本身变成噪声）；
* **多个分类** → 按固定顺序（frontend / framework / cli / backend / other）
  逗号分隔。``kinds`` 是无序集合，顺序不定就会让同一份改动面每次生成出不同
  的提示词 —— 固定顺序取自 ``change_surface.KIND_ORDER``（也就是
  ``ChangeSurface.ordered_kinds`` 的顺序，那个属性本来就是"写进提示词时用"的）。

  注：任务书里给这个用例写的是"``{cli, backend}`` → 按 ``backend, cli`` 顺序
  出现"，而同一份任务书的边界条件写的是固定顺序
  ``frontend / framework / cli / backend / other``，两者互相矛盾。这里以边界
  条件为准（它与 ``change_surface.KIND_ORDER`` 逐项一致），即 ``{cli, backend}``
  渲染成 ``cli, backend``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

pytestmark = pytest.mark.unit

#: 提示词里最多列出多少个改动文件（与实现里的上限一一对应）。
MAX_SURFACE_FILES = 50


def _make_agent(tmp_path: Path):
    """造一个只够用提示词构造的 :class:`VerificationAgent`。"""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _kind_line(prompt: str) -> str:
    """从提示词里取出"分类"那一行。"""
    for line in prompt.splitlines():
        if line.startswith("- 分类："):
            return line
    raise AssertionError(f"提示词里没有分类行：\n{prompt}")


def _file_lines(prompt: str) -> list:
    """从提示词里取出全部"文件"行。"""
    return [line for line in prompt.splitlines() if line.startswith("- 文件：")]


# ---------------------------------------------------------------------------
# 有改动面 → 提示词里有这一段
# ---------------------------------------------------------------------------


def test_prompt_contains_change_surface_section(tmp_path):
    """有改动面 → 提示词含 ``## 本次改动面`` 段与文件、分类。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    surface = ChangeSurface(
        files=("backend/cli.py",),
        kinds=frozenset({"cli"}),
        has_ui=False,
    )

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=surface)

    assert "## 本次改动面" in prompt
    assert "backend/cli.py" in prompt
    assert _file_lines(prompt) == ["- 文件：backend/cli.py"]
    assert _kind_line(prompt) == "- 分类：cli"
    assert "- 触及界面：否" in prompt


def test_prompt_contains_change_surface_section_for_ui_surface(tmp_path):
    """触及界面 → "触及界面：是"；这一段是纯输入注入，不规定输出格式。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    surface = ChangeSurface(
        files=("frontend/app.js",),
        kinds=frozenset({"frontend"}),
        has_ui=True,
    )

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=surface)

    assert "## 本次改动面" in prompt
    assert _kind_line(prompt) == "- 分类：frontend"
    assert "- 触及界面：是" in prompt


# ---------------------------------------------------------------------------
# 空改动面 → 老提示词逐字节不变
# ---------------------------------------------------------------------------


def test_empty_surface_keeps_prompt_unchanged(tmp_path):
    """空改动面 → 输出与无参调用一致（老提示词一个字节都不许动）。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    documents = {"prd": "P", "arch": "A", "test": "T"}

    without = agent._build_planning_prompt(documents)
    with_empty = agent._build_planning_prompt(documents, surface=ChangeSurface())

    assert with_empty == without
    assert "## 本次改动面" not in with_empty


def test_missing_surface_keeps_prompt_unchanged(tmp_path):
    """不传改动面（``None``）→ 与空改动面同样不产出这一段。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    documents = {"prd": "P"}

    assert agent._build_planning_prompt(documents, surface=None) == (
        agent._build_planning_prompt(documents)
    )
    assert agent._build_planning_prompt(documents, surface=ChangeSurface()) == (
        agent._build_planning_prompt(documents)
    )


# ---------------------------------------------------------------------------
# 文件多于 50 个 → 截断并写明剩余数量
# ---------------------------------------------------------------------------


def test_surface_truncates_at_50_files(tmp_path):
    """60 个文件 → 只列 50 个，并写 ``(另有 10 个)``。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    files = tuple(f"backend/mod_{index:02d}.py" for index in range(60))
    surface = ChangeSurface(
        files=files, kinds=frozenset({"backend"}), has_ui=False,
    )

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=surface)

    listed = _file_lines(prompt)
    assert len(listed) == MAX_SURFACE_FILES
    assert listed[0] == "- 文件：backend/mod_00.py"
    assert listed[-1] == "- 文件：backend/mod_49.py"
    assert "backend/mod_50.py" not in prompt
    assert "(另有 10 个)" in prompt


def test_surface_at_limit_lists_every_file(tmp_path):
    """正好 50 个文件 → 全部列出，且不写"另有 N 个"。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    files = tuple(f"backend/mod_{index:02d}.py" for index in range(50))
    surface = ChangeSurface(
        files=files, kinds=frozenset({"backend"}), has_ui=False,
    )

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=surface)

    assert len(_file_lines(prompt)) == MAX_SURFACE_FILES
    assert "另有" not in prompt


# ---------------------------------------------------------------------------
# 分类顺序
# ---------------------------------------------------------------------------


def test_kinds_are_ordered_in_prompt(tmp_path):
    """多个分类 → 按固定顺序（frontend / framework / cli / backend / other）排列。"""
    from change_surface import KIND_ORDER, ChangeSurface

    agent = _make_agent(tmp_path)
    kinds = frozenset({"cli", "backend"})
    surface = ChangeSurface(
        files=("backend/cli.py", "backend/api.py"),
        kinds=kinds,
        has_ui=False,
    )

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=surface)

    expected = ", ".join(kind for kind in KIND_ORDER if kind in kinds)
    assert _kind_line(prompt) == f"- 分类：{expected}"
    assert _kind_line(prompt) == "- 分类：cli, backend"


def test_kinds_order_is_stable_across_runs(tmp_path):
    """同一份改动面 → 每次生成出的分类顺序都一样（集合不能决定顺序）。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    surface = ChangeSurface(
        files=("a.py", "b.py", "c.py"),
        kinds=frozenset({"other", "backend", "frontend", "cli"}),
        has_ui=False,
    )

    lines = {
        _kind_line(agent._build_planning_prompt({"prd": "P"}, surface=surface))
        for _ in range(5)
    }

    assert lines == {"- 分类：frontend, cli, backend, other"}


def test_empty_kinds_render_as_unknown(tmp_path):
    """分类为空集 → 写 ``unknown``。"""
    from change_surface import ChangeSurface

    agent = _make_agent(tmp_path)
    surface = ChangeSurface(
        files=("backend/cli.py",), kinds=frozenset(), has_ui=False,
    )

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=surface)

    assert _kind_line(prompt) == "- 分类：unknown"