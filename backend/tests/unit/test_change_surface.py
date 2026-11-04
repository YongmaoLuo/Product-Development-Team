"""改动面解析模块 :mod:`change_surface` 的单元测试。

为什么要有这组测试
------------------
VP 生成器要决定"这次改动该生成哪类 E2E"，前提是它知道**这次改动碰了哪些
文件**。这个信息只存在于 ``tasks.json`` 的 ``tasks[].files_to_modify`` 里，
所以必须有人把它解析成结构化的改动面（文件并集 + 分类 + 是否触及界面）。

这条链路上最容易出错的两处：

* **并集** —— 多个任务可能声明同一个文件；文件列表要**去重且保序**，否则
  提示词里的"本次改动面"会重复出现同一个文件。
* **分类** —— 分类直接决定生成器产出哪类 E2E。``backend/routes/`` 下的
  HTTP 路由**不是**界面改动（纯后端改动不该生成 ``ui_validation``）；
  ``backend/verification_agent.py`` 是框架层（``verification_*.py``），
  不是普通后端代码。

所有用例都用 ``tmp_path`` 真实落盘，不 mock 文件系统 —— 被测的就是
"tasks.json 在不在、能不能解析"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

pytestmark = pytest.mark.unit

TASKS = "tasks.json"


def _write_tasks(plan_dir: Path, tasks: list) -> Path:
    """把一个 ``{"tasks": [...]}`` 信封写进 ``<plan_dir>/tasks.json``。"""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / TASKS).write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# classify()：分类规则
# ---------------------------------------------------------------------------


def test_classify_frontend_is_ui() -> None:
    """前端文件 → kinds 含 frontend，has_ui 为 True。"""
    from change_surface import classify

    surface = classify(["frontend/index.html", "frontend/app.js"])

    assert "frontend" in surface.kinds
    assert surface.has_ui is True
    assert surface.files == ("frontend/index.html", "frontend/app.js")


def test_classify_backend_only_is_not_ui() -> None:
    """纯后端改动 → kinds 为 backend，has_ui 为 False。

    ``backend/routes/`` 是 HTTP 路由，不是界面 —— 这条断言钉住"后端改动
    不生成 ui_validation"的前提。
    """
    from change_surface import classify

    surface = classify(["backend/server.py", "backend/routes/api.py"])

    assert surface.kinds == frozenset({"backend"})
    assert surface.has_ui is False


def test_classify_cli_and_framework() -> None:
    """``scripts/`` 下的脚本归 cli；``verification_*.py`` 归 framework。"""
    from change_surface import classify

    cli_surface = classify(["scripts/run.py"])
    framework_surface = classify(["backend/verification_agent.py"])

    assert cli_surface.kinds == frozenset({"cli"})
    assert framework_surface.kinds == frozenset({"framework"})
    assert cli_surface.has_ui is False
    assert framework_surface.has_ui is False


def test_classify_framework_prefix_and_coding_tool() -> None:
    """``backend/framework/`` 前缀与 ``coding_tool.py`` 都归 framework。"""
    from change_surface import classify

    surface = classify(["backend/framework/task_graph.py", "backend/coding_tool.py"])

    assert surface.kinds == frozenset({"framework"})


def test_classify_template_and_static_prefixes_are_frontend() -> None:
    """``templates/`` 与 ``static/`` 前缀也算界面改动。"""
    from change_surface import classify

    surface = classify(["templates/index.html", "static/app.css"])

    assert surface.kinds == frozenset({"frontend"})
    assert surface.has_ui is True


def test_classify_unrelated_path_is_other() -> None:
    """既不是前端、框架、CLI 也不是后端的路径归 other。"""
    from change_surface import classify

    surface = classify(["docs/readme.md"])

    assert surface.kinds == frozenset({"other"})
    assert surface.has_ui is False


def test_classify_counts_every_kind_a_file_hits() -> None:
    """一个文件命中多类时，全部计入 kinds。

    ``scripts/build.js`` 既是脚本（``scripts/`` 前缀）又是前端资源
    （``.js`` 扩展名）—— 两类都记，界面判定也因此为真。
    """
    from change_surface import classify

    surface = classify(["scripts/build.js"])

    assert surface.kinds == frozenset({"frontend", "cli"})


# ---------------------------------------------------------------------------
# load_change_surface()：读 tasks.json
# ---------------------------------------------------------------------------


def test_missing_tasks_json_yields_empty_surface(tmp_path: Path) -> None:
    """tasks.json 不存在 → 空改动面，不抛异常。"""
    from change_surface import load_change_surface

    surface = load_change_surface(tmp_path / "no-such-plan")

    assert surface.files == ()
    assert surface.kinds == frozenset()
    assert surface.has_ui is False


def test_malformed_tasks_json_yields_empty_surface(tmp_path: Path) -> None:
    """tasks.json 解析失败 → 空改动面，不抛异常。"""
    from change_surface import load_change_surface

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / TASKS).write_text("{not json", encoding="utf-8")

    surface = load_change_surface(plan_dir)

    assert surface.files == ()
    assert surface.kinds == frozenset()
    assert surface.has_ui is False


def test_union_of_all_tasks_and_dedup(tmp_path: Path) -> None:
    """多个任务的 files_to_modify 求并集，去重且保序。"""
    from change_surface import load_change_surface

    plan_dir = _write_tasks(
        tmp_path / "plan",
        [
            {"id": "T1", "files_to_modify": ["backend/a.py", "frontend/index.html"]},
            {"id": "T2", "files_to_modify": ["backend/a.py", "scripts/run.py"]},
        ],
    )

    surface = load_change_surface(plan_dir)

    assert surface.files == (
        "backend/a.py",
        "frontend/index.html",
        "scripts/run.py",
    )
    assert surface.kinds == frozenset({"backend", "frontend", "cli"})
    assert surface.has_ui is True


def test_task_without_files_to_modify_is_skipped(tmp_path: Path) -> None:
    """没有 ``files_to_modify`` 的任务被跳过，不影响其它任务。"""
    from change_surface import load_change_surface

    plan_dir = _write_tasks(
        tmp_path / "plan",
        [
            {"id": "T1", "title": "没有文件声明"},
            {"id": "T2", "files_to_modify": ["backend/a.py"]},
        ],
    )

    surface = load_change_surface(plan_dir)

    assert surface.files == ("backend/a.py",)
    assert surface.kinds == frozenset({"backend"})


def test_unknown_modifications_task_is_skipped(tmp_path: Path) -> None:
    """``__UNKNOWN_MODIFICATIONS__`` 哨兵不是文件路径，跳过该任务。"""
    from change_surface import load_change_surface

    plan_dir = _write_tasks(
        tmp_path / "plan",
        [
            {"id": "T1", "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"]},
            {"id": "T2", "files_to_modify": ["backend/a.py"]},
        ],
    )

    surface = load_change_surface(plan_dir)

    assert surface.files == ("backend/a.py",)
    assert surface.kinds == frozenset({"backend"})
    assert surface.has_ui is False
