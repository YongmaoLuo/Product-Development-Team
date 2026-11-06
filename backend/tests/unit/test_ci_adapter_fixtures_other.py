"""其它导入用例的 CI 适配脚本夹具契约（2026-11-05）。

导入 fail-close 之后，凡是**经由
``POST /api/plan/import`` 造计划**的测试，都得给它的临时项目目录装一份
**可执行**的 ``scripts/ci_gate.sh`` —— 否则这些用例测的根本不是自己
要测的东西：请求在导入的第二个门禁上就被 422 挡回来了，后面那些关于
「修订落在同一个计划里」「优先级被持久化」「被拒的导入留下注册行」的
断言一句也跑不到。

本文件是那道夹具契约的**门禁**。它只读源码，外加一次 ``ast.parse``：

* 不起真后端；
* 不跑真导入；
* 不碰任何 tmp 目录。

检查的是「源码里写了没有」，不是「跑起来绿不绿」—— 后者由整仓门禁覆盖。

**为什么用 AST 判执行位，而不是 grep 一个 ``0o755``**：权限位写成
``0o755``、``0o750`` 还是 ``0o744``，都是「可执行」；写成 ``0o644``
就不是。grep 一个字面量只能钉死一种写法，AST 判的却是**有没有执行位**
—— 而这正是门禁要守的那条规则（``ci_adapter.missing_ci_adapter`` 判的
也是执行位，不是某个具体数字）。执行位判错，导入照样 422，夹具照样不
合格。

受检的四个模块：

* ``backend/tests/api/test_plan_revise_api.py``
* ``backend/tests/api/plan_scheduler/test_import_priority.py``
* ``backend/tests/e2e/plan_scheduler/test_plan_admission_end_to_end.py``
* ``backend/tests/e2e/test_plan_lifecycle_ui_e2e.py``
"""

from __future__ import annotations

import ast
from pathlib import Path


#: ``backend/`` —— 本文件在 ``backend/tests/unit/`` 下面。
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: ``backend/tests/`` —— 受检模块都在这儿。
_TESTS_DIR = _BACKEND_DIR / "tests"

#: 受检模块：源码里必须写出 CI 适配脚本的安装（存在 + 执行位）。
_FIXTURE_MODULES = (
    "api/test_plan_revise_api.py",
    "api/plan_scheduler/test_import_priority.py",
    "e2e/plan_scheduler/test_plan_admission_end_to_end.py",
    "e2e/test_plan_lifecycle_ui_e2e.py",
)

#: 两个 e2e 模块：改完之后仍必须能被 Python 解析。
_E2E_MODULES = (
    "e2e/plan_scheduler/test_plan_admission_end_to_end.py",
    "e2e/test_plan_lifecycle_ui_e2e.py",
)

#: 执行位（owner / group / other 的任意一位）。
_EXEC_BITS = 0o111


def _read(relative: str) -> str:
    """读一个受检模块的源码。"""
    return (_TESTS_DIR / relative).read_text(encoding="utf-8")


def _chmod_modes(tree: ast.AST) -> list:
    """源码里所有 ``chmod(...)`` 调用的字面权限位。

    只认字面量：``chmod(0o755)`` 认，``chmod(mode)`` 不认（那个值在
    运行时才知道，源码这一层判不了）。判不了的写法宁可放过，也不误伤 ——
    这道门禁要抓的是「根本没设执行位」，不是「写法不常见」。
    """
    modes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "chmod"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                modes.append(arg.value)
    return modes


def test_all_other_import_fixtures_install_ci_adapter():
    """四个模块的源码都写出 ``ci_gate.sh`` 且带执行位。

    一条一条报出来，不是遇到第一个就停：四个模块是同一种病，改一个漏
    三个时，一次看到三个漏比看到三个「跑到第二个才停」省事。
    """
    problems = []

    for relative in _FIXTURE_MODULES:
        source = _read(relative)

        if "ci_gate.sh" not in source:
            problems.append(
                f"{relative}: 源码里没有 ci_gate.sh —— 导入通道 fail-close "
                "之后，它的临时项目目录必须装一份 CI 适配脚本"
            )
            continue

        modes = _chmod_modes(ast.parse(source, filename=relative))
        if not modes:
            problems.append(
                f"{relative}: 没有设置任何权限位 —— 适配脚本必须是可执行文件"
            )
        elif not any(mode & _EXEC_BITS for mode in modes):
            problems.append(
                f"{relative}: 设置的权限位 {modes!r} 里没有执行位 —— "
                "只有文件没有执行位同样算缺"
            )

    assert not problems, "CI 适配脚本夹具契约被破了:\n" + "\n".join(problems)


def test_e2e_modules_still_parse():
    """两个 e2e 模块改动后仍能被 ``ast.parse`` 解析。

    这两个文件是 e2e，改坏了不会被普通的单元层拦住 —— 它们要到真跑
    一次才炸，而真跑一次要起浏览器。语法校验在这里替它们挡一道。
    """
    problems = []

    for relative in _E2E_MODULES:
        path = _TESTS_DIR / relative
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"{relative}: {exc}")

    assert not problems, "e2e 模块解析失败:\n" + "\n".join(problems)
