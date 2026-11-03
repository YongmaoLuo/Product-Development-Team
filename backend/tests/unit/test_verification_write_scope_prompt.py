"""提示词里的可写范围（WRITE SCOPE）。

背景
----
任务 1~4 已经把越权写入**机械地**挡住了：``verification_write_guard`` 拒绝
落在 plan 目录与临时目录之外的写入，``SettingsJsonBuilder`` 把可写范围写进
settings.json 的 env，``run()`` 把 plan 目录与轮次交给 builder。

但护栏是**事后**说话：子 agent 写下去，才被拒绝。一次被拒的写入要烧掉一次
重试（默认总共 3 次尝试），而写下去的那一刻，被验证仓库的工作区可能已经被
改过 —— "验证"这件事被"验证"自己改掉了。

所以范围必须在**写之前**可见：源码只读；脚本、日志、证据只写 plan 目录或
/tmp。本文件钉住三件事：

  1. 路径齐备时，WRITE SCOPE 行里同时出现**仓库绝对路径**与 **plan 目录
     绝对路径**；
  2. 路径缺失时，WRITE SCOPE 仍然出现 —— 用不带路径的说法（"the verified
     repository" / "the plan directory"）；
  3. WRITE SCOPE 的出现**不改变**既有的 EVIDENCE CONTRACT 段。

断言方式
--------
走生产入口 ``VerificationSubAgent._build_prompt(vp_node, attempt)`` —— 就是
子 agent 真正收到的那份提示词。WRITE SCOPE 行单独抽出来断言，免得被提示词
别处出现过的同名字符串蒙混过关。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import VerificationSubAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompt_for(vp_node: Dict[str, Any]) -> str:
    """子 agent 真正收到的那份提示词。"""
    agent = VerificationSubAgent(method="code_review")
    return agent._build_prompt(vp_node, attempt=1)


def _scope_line(prompt: str) -> str:
    """提示词里那一条 WRITE SCOPE 行。"""
    lines = [
        line for line in prompt.splitlines()
        if line.startswith("WRITE SCOPE:")
    ]
    assert lines, (
        "提示词里没有 WRITE SCOPE 行 —— 子 agent 看不到可写范围，"
        "越权写入只能靠护栏事后拒绝"
    )
    return lines[0]


# ---------------------------------------------------------------------------
# 1. 路径齐备 → WRITE SCOPE 点名仓库与 plan 目录
# ---------------------------------------------------------------------------


def test_prompt_states_write_scope() -> None:
    """仓库路径、plan 目录路径、/tmp、越权后果，四样都在这一行里。"""
    vp = {
        "id": "VP-055",
        "project_root": "/repo",
        "evidence_artifact_dir": "/plan/vp_artifacts/VP-055",
    }
    prompt = _prompt_for(vp)
    scope = _scope_line(prompt)

    assert "/repo" in scope, scope
    assert "/plan" in scope, scope
    assert "/tmp" in scope, scope
    # arch 决策点 3：越权后果在提示词里被点名。
    assert "verifier_mutated_tree" in scope, scope
    # 范围声明放在 expected result 之后 —— 它是这条提示词的一部分，
    # 不是事后补在末尾的附注。
    assert prompt.index("WRITE SCOPE:") > prompt.index("Expected result:")


# ---------------------------------------------------------------------------
# 2. 路径缺失 → 仍然有 WRITE SCOPE
# ---------------------------------------------------------------------------


def test_prompt_without_paths_still_states_scope() -> None:
    """没有 project_root / evidence_artifact_dir → 用不带路径的说法。"""
    vp = {"id": "VP-055"}
    scope = _scope_line(_prompt_for(vp))

    assert "the verified repository" in scope, scope
    assert "the plan directory" in scope, scope
    assert "/tmp" in scope, scope


# ---------------------------------------------------------------------------
# 3. 既有 EVIDENCE CONTRACT 段不受影响
# ---------------------------------------------------------------------------


def test_write_scope_leaves_evidence_contract_untouched() -> None:
    """加了 WRITE SCOPE 之后，EVIDENCE CONTRACT 一字不变。"""
    vp = {
        "id": "VP-055",
        "project_root": "/repo",
        "evidence_artifact_dir": "/plan/vp_artifacts/VP-055",
    }
    agent = VerificationSubAgent(method="code_review")
    prompt = agent._build_prompt(vp, attempt=1)

    contract = "\n".join(agent._evidence_contract(vp))
    assert contract, "code_review 的 EVIDENCE CONTRACT 不该是空的"
    assert contract in prompt, "EVIDENCE CONTRACT 段被改动了"
