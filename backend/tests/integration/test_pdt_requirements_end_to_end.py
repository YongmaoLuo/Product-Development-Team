"""端到端验收：真实要求文件 + 真实工具对象走完整链路。

被测对象
--------
``backend/pdt_requirements.py`` 的加载/渲染层，与 ``backend/coding_tool.py``
的 ``ScriptedCodingTool``（严格替身）串起来跑一遍真实链路：

    落盘 ``PDT.md`` → 构造 ``ScriptedCodingTool`` → ``query()`` →
    system 提示被注入 → 块在最前 → 日志字段可见 → 删文件后逐字节回到原样

契约（arch 决策点 2 / 3 / 5，test 决策点 2）
--------------------------------------------
* **注入在最前**：块是 system 提示的第一个东西，base 要求文件的内容在块里，
  原来的 system 提示在块之后逐字保留；
* **描述一致**：``describe()["files"]`` 与磁盘一致，``describe()["sha256"]``
  与**真正注进去的那一块**的 sha256 一致 —— 记到日志里的哈希必须是这一块，
  不是另一块重渲染出来的；
* **可见**：给 tool 注入假 logger 后显式调用 ``_log_query_context``，data 里
  带 ``pdt_requirements_files`` 与 ``pdt_requirements_sha256``；
* **负控**：把 ``PDT.md`` 删掉，同一次调用逐字节回到 ``"SYS"`` —— 机制在没
  有生效文件时不留任何痕迹（连一个多余换行都不加）。

为什么用 ``ScriptedCodingTool`` 而不是真 provider
--------------------------------------------------
这是**端到端**，但要端到端的是"注入"这一段，不是"模型答了什么"：真 provider
会花钱、要网络、答案不可复现。``ScriptedCodingTool`` 是个真的 ``CodingTool``
子类，走的是**同一个** ``_apply_pdt_requirements`` 入口，且把注入后的
system 提示记在 ``last_system_instruction`` 上 —— 那是这个替身上唯一的观测
窗口。没有它，"注进去的那块"就只能靠猜。

为什么断言 3 要显式调 ``_log_query_context``
--------------------------------------------
scripted 的 ``query`` **本身不写日志**（它没有 provider，也没有 token 可估），
所以可见性只能靠显式调用基类上那个钩子。钩子本身是任务 5 放到基类的公开
观测点，不是内部缓存 —— 断言的是"调用点会把字段写进日志"，不是"某处缓存里
有值"。

``.gitignore`` 那一半不在这里
----------------------------
"运行期要求文件永远不入库"由任务 8 的门禁单独守着（``git check-ignore``），
端到端这一层不重复它：本文件的 ``PDT.md`` 落在 ``tmp_path`` 里，本来就不在
仓库内，写进去也碰不到忽略规则。

路径全走 ``tmp_path`` 与 monkeypatch
------------------------------------
不依赖仓库根，不留后台线程或子进程：``ScriptedCodingTool`` 不起进程、不开
套接字、不读凭据。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import coding_tool
import pdt_requirements


#: base 要求文件的内容 —— 断言"文件内容真的进了块"。
BASE_TEXT = "必须给出可复核证据"

#: 原 system 提示 —— 断言"块在最前，原文逐字保留"。
BASE_SYSTEM = "SYS"

#: 块的收尾标签。
BLOCK_CLOSE = "</pdt_user_requirements>"


class _CaptureLogger:
    """最小 logger 替身：只记 ``info`` 事件带来的 data。

    ``_log_query_context`` 调的是 ``logger.info(event, message, data=...)``，
    这里只留 data —— 断言要看的两个字段就在 data 里。
    """

    def __init__(self):
        self.records: list[dict] = []

    def info(self, event, message=None, data=None):
        self.records.append(dict(data or {}))

    def debug(self, *args, **kwargs):  # pragma: no cover —— 本文件不触发
        pass

    def warning(self, *args, **kwargs):  # pragma: no cover —— 本文件不触发
        pass

    def error(self, *args, **kwargs):  # pragma: no cover —— 本文件不触发
        pass


@pytest.fixture
def deployment_root(tmp_path, monkeypatch):
    """部署根目录 → 临时目录，并落一份 base 要求文件。

    ``_deployment_root`` 被 monkeypatch 掉，所以整条链路看到的"部署根"就是
    这个临时目录：真实文件、真实 stat、真实读取，只是不碰仓库。
    """
    root = tmp_path / "deployment"
    root.mkdir()
    (root / "PDT.md").write_text(BASE_TEXT, encoding="utf-8")
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: root)
    return root


@pytest.fixture
def scripted_tool(tmp_path, deployment_root):
    """造一个真的 ``ScriptedCodingTool``。

    fixture 目录必须同时有 ``registry.json``（``SCRIPT_REGISTRY_FILENAME``）
    与同名响应文件，否则构造时就抛 ``ScriptedLLMError``。
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "registry.json").write_text(
        json.dumps({"prompts": {"hello": "hello"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (scripts / "hello.txt").write_text("ok", encoding="utf-8")
    return coding_tool.ScriptedCodingTool(
        script_dir=str(scripts),
        scene="prd",
        logger=_CaptureLogger(),
    )


def _extract_block(injected: str) -> str:
    """从注入后的 system 提示里取出提示块本体（不含块后的原文）。"""
    start = injected.index("<pdt_user_requirements ")
    end = injected.index(BLOCK_CLOSE) + len(BLOCK_CLOSE)
    return injected[start:end]


@pytest.mark.integration
def test_end_to_end_injection_visible(scripted_tool):
    """落盘 → 注入 → 块在最前，base 内容与原 system 提示都在。"""
    scripted_tool.query("hello", system_instruction=BASE_SYSTEM)

    injected = scripted_tool.last_system_instruction
    assert injected is not None, "没有记下注入后的 system 提示"
    # 块在最前
    assert injected.startswith("<pdt_user_requirements ")
    assert injected.endswith(BLOCK_CLOSE + "\n\n" + BASE_SYSTEM)
    # base 要求文件的内容进了块
    assert BASE_TEXT in injected
    # 原 system 提示逐字保留，且排在块之后
    assert injected.index(BASE_TEXT) < injected.index(BASE_SYSTEM)
    # 红线清单由代码写死，不来自要求文件 —— 它也在块里
    assert "[红线]" in injected


@pytest.mark.integration
def test_end_to_end_describe_matches_block(scripted_tool, deployment_root):
    """``describe()`` 的 files / sha256 与真正注进去的那一块一致。"""
    scripted_tool.query("hello", system_instruction=BASE_SYSTEM)

    injected = scripted_tool.last_system_instruction
    described = pdt_requirements.describe("prd")

    # files 与磁盘一致：只有 base 一份，且是相对路径
    assert described["files"] == ["PDT.md"]
    assert (deployment_root / "PDT.md").is_file()
    assert not Path(described["files"][0]).is_absolute()

    # sha256 与**注进去的那一块**一致（不是与另一次渲染一致）
    block = _extract_block(injected)
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == described["sha256"]
    # 同一块：模块渲染出来的与 tool 手里的是同一段文本
    assert block == pdt_requirements.render_block("prd")


@pytest.mark.integration
def test_end_to_end_log_fields(scripted_tool):
    """假 logger 收到的 data 带两个可见性字段。"""
    scripted_tool.query("hello", system_instruction=BASE_SYSTEM)

    scripted_tool._log_query_context(
        "hello",
        scripted_tool.last_system_instruction,
        phase="query",
        scene="prd",
    )

    assert scripted_tool.logger.records, "没有日志输出"
    record = scripted_tool.logger.records[0]
    described = pdt_requirements.describe("prd")
    assert record["pdt_requirements_files"] == ["PDT.md"]
    assert record["pdt_requirements_sha256"] == described["sha256"]
    # 日志里的哈希与注进去的那一块同源
    block = _extract_block(scripted_tool.last_system_instruction)
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == record[
        "pdt_requirements_sha256"
    ]


@pytest.mark.integration
def test_end_to_end_negative_control(scripted_tool, deployment_root):
    """删掉 ``PDT.md`` → 同一调用逐字节回到 ``"SYS"``。"""
    (deployment_root / "PDT.md").unlink()

    scripted_tool.query("hello", system_instruction=BASE_SYSTEM)

    assert scripted_tool.last_system_instruction == BASE_SYSTEM

    # 再来一个全新的 tool 也一样：证明不是缓存把删掉的文件端了出来
    fresh = coding_tool.ScriptedCodingTool(
        script_dir=scripted_tool.script_dir,
        scene="prd",
        logger=_CaptureLogger(),
    )
    fresh.query("hello", system_instruction=BASE_SYSTEM)
    assert fresh.last_system_instruction == BASE_SYSTEM


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
