"""导入用例的 CI 适配脚本夹具契约（2026-11-05）。

「导入 fail-close」的 arch 决策点 6：**既有导入用例保持全绿**。
Task 6 让 ``POST /api/plan/import`` 在项目没有可执行的 ``scripts/ci_gate.sh``
时拒绝（422），于是四个既有导入用例的 ``checkout`` 夹具必须跟着升级：它们造
出来的临时项目目录里要有一份**可执行**的适配脚本，否则这些用例测到的将是
「拒绝」分支，而不是它们各自要测的东西。

「跟着升级」这件事本身需要一个门禁钉住，否则它会悄悄烂掉。夹具是测试代码，
没有人 review 它；等哪天有人清理临时目录时顺手删掉那三行适配脚本，四个导入
用例会集体变红 —— 而那份红看起来像是「Task 6 的门禁太严」，不像「夹具退化」，
排查方向会被带偏。

本文件钉住两件事：

* **源码级**：四个模块的源码里都出现 ``ci_gate.sh``，且都显式设置执行位
  （``chmod`` + ``0o755``）。只写文件不 chmod 是不够的 —— 判定把「文件存在
  但没有执行位」同样算成「缺适配脚本」；而「设的权限位里真的有执行位」这一
  条用 AST 判（源码里的 ``chmod(0o755)`` 是不是一次带字面权限位的调用），
  不靠 grep 一个字面量。
* **行为级**：四个模块的 ``checkout`` 夹具在 tmp 目录里造出来的适配脚本真的
  存在、真的有执行位（``os.access(..., os.X_OK)``）。

门禁只读这四个模块的**源码**：它不 import 被测代码（``server`` /
``ci_adapter``），也不起后端，因此后端依赖坏掉时它不会跟着变红 —— 一份
「因为别处坏了所以这里也红」的门禁，用两天就没人看了。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path


#: 走 ``POST /api/plan/import`` 造计划的四个 api 层模块。它们各自的
#: ``checkout`` 夹具造的是同一个东西 —— 一个「项目检出目录」—— 所以四处的
#: 夹具必须一起升级，缺一处就有用例掉进拒绝分支。
_API_MODULES = (
    "test_plan_import_api.py",
    "test_plan_import_queued.py",
    "test_plan_import_failed_phase.py",
    "test_plan_creation_hook_binding.py",
)

_API_DIR = Path(__file__).resolve().parent.parent / "api"

#: 执行位（owner / group / other 的任意一位）。
_EXEC_BITS = 0o111


def _module_path(name: str) -> Path:
    """四个模块里某一个的路径。"""
    return _API_DIR / name


def _chmod_modes(tree: ast.AST) -> list:
    """源码里所有 ``chmod(...)`` 调用的字面权限位。

    只认字面量：``chmod(0o755)`` 认，``chmod(mode)`` 不认（那个值在运行时
    才知道，源码这一层判不了）。判不了的写法宁可放过，也不误伤 —— 这道门禁
    要抓的是「根本没设执行位」，不是「写法不常见」。
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


def _checkout_fixture_source(module_path: Path) -> str:
    """取出模块里 ``checkout`` 夹具的源码（不含装饰器）。

    用 ``ast`` 而不是正则：正则分不清「函数体里的那几行」和「注释里提到的那
    几行」，而这个门禁要判的恰恰是**夹具真的做了这件事**，不是「文件里提到过
    这件事」。
    """
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "checkout":
            segment = ast.get_source_segment(source, node)
            if segment is None:
                raise AssertionError(
                    f"{module_path.name}: 取不到 checkout 夹具的源码"
                )
            return segment
    raise AssertionError(f"{module_path.name}: 找不到 checkout 夹具")


def _run_checkout_fixture(module_path: Path, project_root: Path) -> Path:
    """在 ``project_root`` 里跑一遍模块自己的 ``checkout`` 夹具。

    夹具被抽出来单独执行，而不是 import 整个模块 —— import 整个模块会把
    ``server`` 一起拖进来，门禁就变成「依赖被测代码」的了，那正是这个门禁
    要避免的耦合。夹具本身只依赖 ``tmp_path``，所以单独跑是安全的。
    """
    segment = _checkout_fixture_source(module_path)
    namespace: dict = {"Path": Path}
    exec(  # nosec B102  # noqa: S102 — 受控的夹具源码，只在 tmp 目录里跑
        compile(segment, str(module_path), "exec"), namespace
    )
    checkout = namespace["checkout"]
    return Path(checkout(project_root))


def test_all_api_import_fixtures_install_ci_adapter():
    """四个模块的源码都装得出**可执行**的适配脚本。

    源码级检查，三样都要有：``ci_gate.sh`` 这个名字、设置权限位的那一行
    （``chmod``）、以及它设的权限位里**真的有执行位**。少 chmod 的话，
    判定仍然认为项目「缺适配脚本」，四个导入用例会集体掉进拒绝分支。

    一次把四个模块的问题全报出来，不是遇到第一个就停：四个模块是同一种
    病，改一个漏三个时，一次看到三个漏比看到三个「跑到第二个才停」省事。
    """
    problems = []

    for name in _API_MODULES:
        path = _module_path(name)
        assert path.is_file(), f"{name}: 找不到模块源码 {path}"

        source = path.read_text(encoding="utf-8")

        if "ci_gate.sh" not in source:
            problems.append(
                f"{name}: 源码里没有 ci_gate.sh —— 导入通道现在拒绝没有"
                "可执行适配脚本的项目"
            )
            continue

        if "chmod" not in source or "0o755" not in source:
            problems.append(
                f"{name}: 源码里没有设置权限位（chmod 0o755）—— "
                "文件存在但没有执行位同样算「缺适配脚本」"
            )
            continue

        modes = _chmod_modes(ast.parse(source, filename=str(path)))
        if not modes:
            problems.append(
                f"{name}: 没有设置任何权限位 —— 适配脚本必须是可执行文件"
            )
        elif not any(mode & _EXEC_BITS for mode in modes):
            problems.append(
                f"{name}: 设置的权限位 {modes!r} 里没有执行位 —— "
                "只有文件没有执行位同样算缺"
            )

    assert not problems, "CI 适配脚本夹具契约被破了:\n" + "\n".join(problems)


def test_fixture_recipe_creates_executable_script(tmp_path):
    """按契约写明的配方复现一次，再把四个模块自己的夹具各跑一遍。

    第一段是契约里写明的那份配方，原样复现：脚本存在，且有执行位。第二段
    是四个模块**自己的** ``checkout`` 夹具在 tmp 目录里跑出来的结果 —— 它们
    必须造出同样可执行的脚本，否则上面的源码检查只是一句文本约定。

    全程只写 ``tmp_path``，不碰任何真实 checkout。
    """
    # 契约里写明的配方，原样复现一次。
    recipe_root = tmp_path / "recipe"
    (recipe_root / "scripts").mkdir(parents=True, exist_ok=True)
    adapter = recipe_root / "scripts" / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)

    assert adapter.is_file(), "配方没有造出 scripts/ci_gate.sh"
    assert os.access(adapter, os.X_OK), "配方造出的适配脚本没有执行位"

    # 四个模块自己的夹具，各自在自己的 tmp 子目录里跑一遍。
    for name in _API_MODULES:
        module_path = _module_path(name)
        project_dir = _run_checkout_fixture(
            module_path, tmp_path / name.replace(".py", "")
        )

        adapter = project_dir / "scripts" / "ci_gate.sh"
        assert adapter.is_file(), (
            f"{name}: checkout 夹具没有造出 scripts/ci_gate.sh —— "
            f"导入通道会拒绝这个项目，四个导入用例会集体掉进拒绝分支"
        )
        assert os.access(adapter, os.X_OK), (
            f"{name}: checkout 夹具造出的适配脚本没有执行位 —— "
            f"文件存在但没有执行位同样算「缺适配脚本」"
        )
