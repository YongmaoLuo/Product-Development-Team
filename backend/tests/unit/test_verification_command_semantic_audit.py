"""V4 —— 静态判不了时，用 LLM 读懂命令的语义（2026-09-15）。

静态护栏只能认形态，有些情况它无从判断：当测试是自定义脚本时，命令的
语义是什么、有没有真正界定范围，只能读懂它才知道。这类判断要求高，所以
走 high 档。

有一类命令形态完全正常、语义上却什么都没验：

* VP-020 ``python tools/verify_fixture_integrity.py`` —— 退出 0，但脚本实际
  采样了 0 个 fixture（"Sampled 0 fixtures 验证通过"），内部空转。
* VP-017 ``grep -rn ... frontend/ || echo CLEAN`` —— 目录不存在，grep 退出
  码 2 被 ``|| echo CLEAN`` 吞掉，看起来"干净"。

两者都不是"无界"，是"看起来跑了、其实什么都没验"。判这个要读懂命令和它断言
的语义，所以走 ``verification_command_audit`` 场景（配置里映射到 **high** 档）。

**实现上有一条踩过的坑，值得留在测试里**：初版另起一个
``create_coding_tool(scene=...)`` 来做审计，结果在单测里建出了**真实**工具、
真的打了一次 provider（实测跑出了真实的 LLM 判定文本）。现在复用 agent 自己
的 ``coding_tool`` 并只做 per-call 场景覆盖 —— mock 自然吸收，没有那个缝。
下面的用例全部基于 mock 工具，**不碰任何真实 provider**。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import verification_agent as va


def _agent(tmp_path: Path, payload=None):
    """返回 ``(agent, mock_tool)``。

    mock 工具吸收审计调用 —— 这本身就是回归锁：审计必须走 agent 自己的
    ``coding_tool``，不能再自己建一个。
    """
    tool = MagicMock()
    if isinstance(payload, Exception):
        tool.query_json.side_effect = payload
    else:
        tool.query_json.return_value = (
            {"results": []} if payload is None else payload
        )
    agent = va.VerificationAgent(
        plan_dir=tmp_path / "plan",
        project_dir=tmp_path / "proj",
        coding_tool=tool,
    )
    return agent, tool


def _plan(*commands) -> dict:
    return {
        "verification_points": [
            {
                "id": f"VP-{i:03d}",
                "title": f"point {i}",
                "expected_result": "exit 0",
                "test_command": cmd,
            }
            for i, cmd in enumerate(commands, start=1)
        ],
    }


CUSTOM = "bash -c \"source venv1/bin/activate && python tools/verify_fixture_integrity.py\""
SHELL_AUDIT = "bash -c \"grep -rn 'EXAMPLE' frontend/ || echo CLEAN\""
PYTEST_CMD = "pytest tests/api/test_foo.py -v"


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------


def test_standard_runners_never_reach_the_llm(tmp_path) -> None:
    """全是标准 runner → 一次 LLM 调用都不该发生。"""
    agent, tool = _agent(tmp_path)

    findings = agent._llm_audit_commands(
        _plan(PYTEST_CMD, "cd x && cargo test --test f test_a"),
    )

    assert findings == []
    assert tool.query_json.call_count == 0, "没有候选就不该花这次钱"


def test_a_custom_script_triggers_the_audit(tmp_path) -> None:
    agent, tool = _agent(tmp_path)

    agent._llm_audit_commands(_plan(CUSTOM))

    assert tool.query_json.call_count == 1


def test_the_audit_uses_the_high_tier_scene(tmp_path) -> None:
    """走 high 档 —— 必须通过 per-call 场景覆盖请求 high 档。

    这也锁住实现方式：审计走 agent 自己的 ``coding_tool``（mock 吸收），
    不是另起一个工具。
    """
    agent, tool = _agent(tmp_path)

    agent._llm_audit_commands(_plan(CUSTOM))

    assert tool.query_json.call_args.kwargs.get("scene") == (
        "verification_command_audit"
    )


def test_the_audit_scene_is_configured_as_high() -> None:
    """The SHIPPED routing template must route the audit at ``high``.

    The live routing file is per-user: it lives in ``.config/``
    (gitignored) and is seeded by copying ``example/provider_routing.yaml.example``.
    So the file to assert on is the template — it is what every user
    starts from, and the only copy a reviewer can see in the repo. A
    user who edits their own copy is exercising their own choice.
    """
    import yaml

    from config_paths import resolve_config_dir

    repo_root = resolve_config_dir().parent
    cfg = repo_root / "example" / "provider_routing.yaml.example"
    assert cfg.exists(), (
        f"the shipped routing template is missing: {cfg}. Every user "
        "seeds .config/provider_routing.yaml from it."
    )
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["scenes"]["verification_command_audit"] == "high", (
        "该判断按约定走 high 档"
    )
    assert "high" in data["tiers"]


def test_the_live_routing_file_is_not_shipped_in_the_repo() -> None:
    """Routing is deployment config, and it must not come back into ``backend/``.

    It used to live at ``backend/configs/provider_routing.yaml`` — a
    path inside the distributed source tree — which meant one
    operator's provider names shipped to every user. Resolution now
    goes through :func:`config_paths.resolve_provider_routing_file`.
    """
    from config_paths import resolve_config_dir, resolve_provider_routing_file

    resolved = resolve_provider_routing_file()
    assert resolved.parent == resolve_config_dir(), (
        f"routing must resolve inside the config dir; got {resolved}"
    )
    stale = Path(va.__file__).parent / "configs" / "provider_routing.yaml"
    assert not stale.exists(), (
        f"{stale} is back in the distributed source tree — provider "
        "routing is deployment config and belongs in .config/"
    )


def test_claude_tool_actually_honours_a_per_call_scene() -> None:
    """复用工具的前提是它真的支持 per-call ``scene``。

    这条不去跑 provider，只检查签名 —— 生产路径靠的就是这个 kwarg。
    """
    import inspect

    from coding_tool import ClaudeCodingTool

    params = inspect.signature(ClaudeCodingTool.query_json).parameters
    assert "scene" in params, (
        "ClaudeCodingTool.query_json 必须接受 per-call scene"
    )


# ---------------------------------------------------------------------------
# 判定结果的处理
# ---------------------------------------------------------------------------


def test_bounded_false_annotates_the_vp(tmp_path) -> None:
    agent, _ = _agent(tmp_path, {"results": [
        {"id": "VP-001", "bounded": False,
         "reason": "脚本采样 0 条却仍打印通过"},
    ]})
    plan = _plan(CUSTOM)

    findings = agent._llm_audit_commands(plan)

    assert [f["id"] for f in findings] == ["VP-001"]
    assert findings[0]["violations"] == ["llm_semantic_audit"]
    guard = plan["verification_points"][0]["command_guard"]
    assert guard["violations"][0]["code"] == "llm_semantic_audit"
    assert "采样 0 条" in guard["violations"][0]["detail"], (
        "LLM 给的理由必须进 detail —— 重新生成时最该看到的就是它"
    )


def test_bounded_true_is_left_alone(tmp_path) -> None:
    agent, _ = _agent(tmp_path, {"results": [
        {"id": "VP-001", "bounded": True, "reason": "确实在比对 fixture 哈希"},
    ]})
    plan = _plan(CUSTOM)

    assert agent._llm_audit_commands(plan) == []
    assert "command_guard" not in plan["verification_points"][0]


def test_only_an_explicit_false_counts(tmp_path) -> None:
    """字段缺失 / 类型不对 / 非 false —— 一律当没判过。

    误报的代价是 LLM 为过检查空转（同一教训在"每条命令都要有显式时间上限"
    上吃过一次），所以宁可漏报。
    """
    agent, _ = _agent(tmp_path, {"results": [
        {"id": "VP-001"},                       # 没有 bounded
        {"id": "VP-002", "bounded": "false"},   # 字符串，不是布尔
        {"id": "VP-003", "bounded": None},
    ]})
    plan = _plan(CUSTOM, SHELL_AUDIT, CUSTOM)

    assert agent._llm_audit_commands(plan) == []
    for vp in plan["verification_points"]:
        assert "command_guard" not in vp


def test_a_verdict_for_an_unknown_vp_is_ignored(tmp_path) -> None:
    agent, _ = _agent(tmp_path, {"results": [
        {"id": "VP-999", "bounded": False, "reason": "x"},
    ]})

    assert agent._llm_audit_commands(_plan(CUSTOM)) == []


# ---------------------------------------------------------------------------
# 审计是增益，不是闸门 —— 任何失败都不能阻断计划生成
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    RuntimeError("provider 挂了"),
    {"results": "not-a-list"},
    {},
    None,
    "totally not a dict",
])
def test_audit_failures_never_raise(tmp_path, payload) -> None:
    agent, _ = _agent(tmp_path, payload)
    plan = _plan(CUSTOM)

    assert agent._llm_audit_commands(plan) == []
    assert "command_guard" not in plan["verification_points"][0]


def test_provider_failure_is_swallowed(tmp_path) -> None:
    agent, _ = _agent(tmp_path, va.ApiError("boom"))

    assert agent._llm_audit_commands(_plan(CUSTOM)) == []


def test_more_than_a_batch_is_split(tmp_path) -> None:
    """候选多于一批时分多次调用（防止单次 prompt 过长）。"""
    agent, tool = _agent(tmp_path)
    n = agent._SEMANTIC_AUDIT_BATCH + 3

    agent._llm_audit_commands(_plan(*([CUSTOM] * n)))

    assert tool.query_json.call_count == 2
