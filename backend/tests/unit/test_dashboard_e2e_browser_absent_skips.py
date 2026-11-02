"""浏览器缺失时，dashboard 端到端验收要 **skip**，不是 fail。

背景
----
``tests/e2e/test_dashboard_plan_and_verification_e2e.py`` 是 PRD 任务 8 的
端到端验收：真后端子进程、真 Playwright Chromium、真点击，断言任务卡数量、
验证徽标、轮次文案与零页面错误。

它的 ``dashboard`` fixture 把驱动过程包在 ``try`` 里，失败时把**后端自己的
输出**附在断言后面 —— 那是这份验收最有用的上下文：用户看到的不是一个空的
``AssertionError``，而是「后端为什么没起来」。

原来的写法是::

    except BaseException as exc:  # noqa: BLE001
        raise AssertionError(...) from exc

这一行把 pytest 自己的 ``Skipped`` 也吃掉了。pytest 9.1.1 的继承链是::

    Skipped -> OutcomeException -> BaseException

``OutcomeException`` 直接继承 ``BaseException``，所以 ``except BaseException``
**会**捕获 ``pytest.skip(...)``，并把它改写成一个 ``AssertionError``。

后果：浏览器缺失 —— 也就是「这台机器测不了」—— 变成了 4 个 error。CI 的
e2e lane 只要收集到这一份文件就会红，而真正的失败会被环境噪声淹没。这正是
PRD 写下的边界：浏览器缺失必须是 skip。

被钉住的东西
------------
* **子进程真跑一遍 e2e**：``PDT_TEST_REAL_HOME`` 指向一个**空目录**，
  浏览器发现因此返回 ``None``；这一跑必须退出码 0、报 ``skipped``、
  不许有 ``error``。
* **源码级 pin**：``dashboard`` fixture 的 ``except`` 必须是
  ``except Exception as exc:`` —— ``Skipped`` 与 ``KeyboardInterrupt`` /
  ``SystemExit`` 不再被改写；而 ``AssertionError`` 与后端输出尾巴**必须
  还在**，否则有人为了过 skip 用例把真实失败的上下文一并吞掉。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

#: 被验收的那一份 e2e（相对 ``backend/``，与仓库其它地方一致）。
E2E_MODULE = "tests/e2e/test_dashboard_plan_and_verification_e2e.py"

#: 同一份文件的源码路径 —— 源码级 pin 读它。
E2E_SOURCE = _BACKEND_DIR / "tests" / "e2e" / (
    "test_dashboard_plan_and_verification_e2e.py"
)

#: 子进程不许继承的变量：它们键住**跑这一份文件的那个 session** 的临时
#: 目录与 pytest 自己的簿记。子进程必须自己开一套，并且不许在退出时删掉
#: 父进程的。
_CHILD_ENV_DROP = (
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PDT_PLANS_DIR",
    "PDT_STATE_DB_PATH",
    "PDT_TEST_SESSION_ROOT",
    "_PDT_TEST_SESSION_ROOT_OWNER",
)

#: 浏览器发现读的是 ``$HOME``；这一份 e2e 用 ``PDT_TEST_REAL_HOME`` 把
#: ``$HOME`` 换回跑起来时的那个家目录。指到一个空目录 = 没有浏览器。
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"


def _child_env(real_home: Path) -> Dict[str, str]:
    """子进程的环境：丢掉父 session 的键，外加一个空的「真 home」。

    ``PDT_TEST_REAL_HOME`` 指向 ``real_home``：``install_hermetic_env()``
    用 ``setdefault`` 记下「跑起来时的 home」，浏览器发现再把 ``$HOME``
    换回它。一个空目录底下没有 Playwright 的缓存，浏览器发现因此返回
    ``None`` —— 这就是「这台机器测不了」的**确定**造法，不依赖这台机器
    到底装没装浏览器。
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _CHILD_ENV_DROP
    }
    env[REAL_HOME_ENV] = str(real_home)
    return env


def _run_e2e_module(real_home: Path) -> subprocess.CompletedProcess:
    """在子进程里跑那一份 e2e，cwd=backend。

    子进程是一次**真的 pytest 运行**：``pytest.skip`` 的语义是 pytest 在
    **收集/执行期**给的，只有 pytest 能回答「skip 有没有生效」。
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", E2E_MODULE, "-q"],
        cwd=str(_BACKEND_DIR),
        env=_child_env(real_home),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _dashboard_fixture_source() -> str:
    """``dashboard`` fixture 的源码片段。

    按 AST 定位，不按行号：行号会漂，而且 ``except`` 那一行本来就是要改的。
    """
    source = E2E_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "dashboard":
            segment = ast.get_source_segment(source, node)
            assert segment is not None, (
                "the dashboard fixture's source could not be extracted"
            )
            return segment
    raise AssertionError(
        "the e2e module no longer carries a dashboard() fixture -- the "
        "browser-missing behaviour has to be re-pointed at whoever owns it"
    )


def test_the_dashboard_e2e_skips_when_the_browser_is_missing(tmp_path):
    """浏览器缺失 → skip（退出码 0），不是 error。

    这一跑是真的把后端起起来、真的去发现浏览器、真的发现不了。**任何**
    一处把 skip 改写成 error 的改动，都会让这一条立刻变红。
    """
    empty_home = tmp_path / "home-without-a-browser"
    empty_home.mkdir()

    result = _run_e2e_module(empty_home)
    output = result.stdout + result.stderr

    assert result.returncode == 0, (
        f"浏览器缺失时这一跑必须以 skip 收场，实际退出码是 "
        f"{result.returncode}：\n{output}"
    )
    assert re.search(r"\bskipped\b", output), (
        f"输出里没有 skipped —— 这一跑根本没有走到「这台机器测不了」"
        f"那一条路上：\n{output}"
    )
    assert not re.search(r"\berrors?\b", output, re.IGNORECASE), (
        f"输出里有 error —— 浏览器缺失被当成了产品失败：\n{output}"
    )


def test_the_dashboard_fixture_catches_only_ordinary_failures():
    """``except Exception`` —— ``Skipped`` 不再被改写。

    pytest 的 ``Skipped`` 继承自 ``BaseException`` 而不是 ``Exception``，
    所以收窄到 ``Exception`` 之后它会照常跳过；而 ``AssertionError`` /
    ``TimeoutExpired`` / 子进程失败仍然是 ``Exception`` 的子类，**照样**
    被附上后端自己的输出。
    """
    segment = _dashboard_fixture_source()

    assert "except BaseException" not in segment, (
        "except BaseException 会把 pytest.skip() 改写成 AssertionError —— "
        "浏览器缺失因此变成 error，而不是 skip：\n" + segment
    )
    assert "except Exception as exc:" in segment, (
        "驱动过程必须只收 Exception —— Skipped / KeyboardInterrupt / "
        "SystemExit 不该被改写：\n" + segment
    )


def test_the_dashboard_fixture_still_reports_the_backend_output():
    """跳过的那条路被改窄了，**真实失败**的那条路不许被吞掉。

    ``raise AssertionError`` 与后端输出尾巴是这份验收最有用的上下文：
    少了它们，一次「后端没起来」就只剩一个空的断言消息。
    """
    segment = _dashboard_fixture_source()

    assert "raise AssertionError(" in segment, (
        "驱动失败必须仍然是断言失败 —— 不能为了过 skip 用例把它吞掉：\n"
        + segment
    )
    assert "--- tail of the backend's own output ---" in segment, (
        "失败时必须附上后端自己的输出 —— 那段上下文不能被一起吞掉：\n"
        + segment
    )


def test_the_browser_missing_skip_reasons_are_still_in_the_module():
    """两条 skip 的**理由**都还在：没装 chromium、没装带 playwright 的解释器。

    少一条，另外一种「这台机器测不了」就会掉回 error。
    """
    source = E2E_SOURCE.read_text(encoding="utf-8")

    reasons: List[str] = [
        "chromium is not installed",
        "no interpreter with playwright is installed in this environment",
    ]
    for reason in reasons:
        assert reason in source, (
            f"skip 的理由「{reason}」不见了 —— 另一种「测不了」会掉回 "
            f"error：\n{source}"
        )