"""生成侧的输出约束与确定性归一化（缺陷 B 第 1、3、4 条的生成侧缺口）。

为什么要有这组测试
------------------
执行器已经支持 command 类 e2e（``e2e_kind`` + ``entry_command``）、也已经支持
成本闸（VP 的 ``cost`` 字段）。但**只要生成器不产出这些字段，那两套能力在真实
计划里永远不会被触发** —— 能力存在 ≠ 能力被用上。所以这里钉的是**生成侧**：

* **提示词**必须把"计划该长成什么样"讲清楚（``## 输出约束`` 段），而且讲的内容
  要随改动面变：非界面改动要求"至少一条 command 类 e2e 且 ``cost=none``"；
  界面改动要求"``ui_validation`` 必须带 ``target_url``"。
* **生成结果**必须被确定性地归一化：有 ``target_url`` → ``e2e_kind=browser``；
  无 ``target_url`` 而有 ``entry_command`` → ``e2e_kind=command``；两条腿都没有
  → 保留这条 VP、标 ``invalid_e2e``（**不改写它的 id**，也不替它伪造
  ``entry_command`` —— 缺口交给完整性检查报）。

四条 TDD 规格
-------------
  1. ``test_backend_surface_prompt_requires_command_e2e``；
  2. ``test_ui_surface_prompt_requires_target_url``；
  3. ``test_policy_normalizes_e2e_kind``；
  4. ``test_policy_defaults_cost_to_none_and_keeps_llm_quota``；
  5. ``test_three_change_surface_samples``（上游验收第 3 条要求的三份样本）。

外加几条边界：归一化幂等、不伪造 ``entry_command``、``full_ci`` 的字段语义不动。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

pytestmark = pytest.mark.unit

#: 提示词里输出约束段的标题。
CONSTRAINT_HEADING = "## 输出约束"

#: 界面改动面下"``ui_validation`` 必须带 ``target_url``"那一句（逐字节固定 ——
#: 两份样本就是靠它区分界面改动与非界面改动的）。
UI_TARGET_URL_RULE = (
    "每条 `ui_validation` 验证点都必须带 `target_url`"
)


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


def _backend_surface():
    """纯后端改动面：只碰了 ``backend/api/handler.py``。"""
    from change_surface import ChangeSurface

    return ChangeSurface(
        files=("backend/api/handler.py",),
        kinds=frozenset({"backend"}),
        has_ui=False,
    )


def _frontend_surface():
    """触及界面的改动面：改了前端模板。"""
    from change_surface import ChangeSurface

    return ChangeSurface(
        files=("templates/dashboard.html",),
        kinds=frozenset({"frontend"}),
        has_ui=True,
    )


def _cli_surface():
    """本地 CLI 改动面：只碰了 ``backend/cli.py``。"""
    from change_surface import ChangeSurface

    return ChangeSurface(
        files=("backend/cli.py",),
        kinds=frozenset({"cli"}),
        has_ui=False,
    )


def _apply_policies(plan: dict, surface) -> dict:
    """按生成回路的接线顺序跑一遍两条策略。

    顺序与 :meth:`VerificationAgent.generate_verification_plan` 里一致：
    **先**裁 ``ui_validation``（缺陷 B 第 2 条），**再**归一化方法字段
    （缺陷 B 第 1、3、4 条）。先裁后归一化是有意义的：被裁掉的那条
    ``ui_validation`` 不该再进归一化。
    """
    from verification_agent import (
        _apply_method_policy,
        _apply_ui_validation_policy,
    )

    plan = _apply_ui_validation_policy(plan, surface)
    return _apply_method_policy(plan, surface)


# ---------------------------------------------------------------------------
# 1) 非界面改动面 → 提示词要求"至少一条 command 类 e2e 且 cost=none"
# ---------------------------------------------------------------------------


def test_backend_surface_prompt_requires_command_e2e(tmp_path):
    """纯后端改动 → 提示词里必须有"至少一条 command 类 e2e、cost=none"。"""
    agent = _make_agent(tmp_path)

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=_backend_surface())

    assert CONSTRAINT_HEADING in prompt
    # 子类字段与它的两种取值都要讲清楚（否则生成器不会写 ``e2e_kind``）。
    assert "e2e_kind" in prompt
    assert "browser" in prompt
    assert "command" in prompt
    assert "entry_command" in prompt
    # 成本字段与它的取值也要讲清楚。
    assert "cost" in prompt
    assert "none" in prompt
    assert "llm_quota" in prompt
    # "至少一条"这个要求本身。
    assert "至少有一条" in prompt
    # 非界面改动面**不**要求 ui_validation 带 target_url（那是界面改动的要求）。
    assert UI_TARGET_URL_RULE not in prompt


# ---------------------------------------------------------------------------
# 2) 界面改动面 → 提示词要求 ui_validation 带 target_url
# ---------------------------------------------------------------------------


def test_ui_surface_prompt_requires_target_url(tmp_path):
    """触及界面 → 提示词里必须要求 ``ui_validation`` 带 ``target_url``。"""
    agent = _make_agent(tmp_path)

    prompt = agent._build_planning_prompt({"prd": "P"}, surface=_frontend_surface())

    assert CONSTRAINT_HEADING in prompt
    assert UI_TARGET_URL_RULE in prompt
    assert "target_url" in prompt
    # 子类 / 成本的字段约定在界面改动面下同样要讲。
    assert "e2e_kind" in prompt
    assert "cost" in prompt


# ---------------------------------------------------------------------------
# 3) 归一化：e2e_kind 按证据推断，两条腿都没有 → invalid_e2e
# ---------------------------------------------------------------------------


def test_policy_normalizes_e2e_kind():
    """有 target_url → browser；无 target_url 且有 entry_command → command。

    两条腿都没有时：**保留这条 VP**、标 ``invalid_e2e=True``、**不改写它的
    id** —— 缺口交给完整性检查去报，生成侧不替它猜一条"正确的"入口命令。
    """
    from verification_agent import _apply_method_policy

    plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "e2e",
                "target_url": "http://127.0.0.1:{{svc.api.port}}/",
            },
            {
                "id": "VP-002",
                "verification_method": "e2e",
                "entry_command": "python -m pytest tests/e2e/test_cli.py -q",
            },
            {
                "id": "VP-003",
                "verification_method": "e2e",
            },
        ],
    }

    result = _apply_method_policy(plan, _backend_surface())
    points = result["verification_points"]

    assert points[0]["e2e_kind"] == "browser"
    assert points[0]["target_url"] == "http://127.0.0.1:{{svc.api.port}}/"

    assert points[1]["e2e_kind"] == "command"
    assert points[1]["entry_command"] == (
        "python -m pytest tests/e2e/test_cli.py -q"
    )

    assert points[2]["invalid_e2e"] is True
    assert points[2]["id"] == "VP-003"
    assert "e2e_kind" not in points[2]


def test_policy_does_not_fabricate_an_entry_command():
    """非界面改动面要求 command 类 e2e，但**代码不许伪造** ``entry_command``。

    没有入口命令时留 ``invalid_e2e`` 交给完整性检查报缺口 —— 凭空造一条命令
    等于替生成器猜一条"正确的"入口，那是另一种越权。
    """
    from verification_agent import _apply_method_policy

    plan = {
        "verification_points": [
            {"id": "VP-007", "verification_method": "e2e"},
        ],
    }

    points = _apply_method_policy(plan, _backend_surface())["verification_points"]

    assert points[0]["invalid_e2e"] is True
    assert "entry_command" not in points[0]


def test_policy_is_idempotent():
    """归一化幂等：连续调用两次结果一致，且不产生重复的验证点。"""
    from verification_agent import _apply_method_policy

    plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "e2e",
                "target_url": "http://127.0.0.1:{{svc.api.port}}/",
            },
            {
                "id": "VP-002",
                "verification_method": "e2e",
                "entry_command": "python -m pytest tests/e2e/test_cli.py -q",
            },
            {"id": "VP-003", "verification_method": "e2e"},
        ],
    }

    once = _apply_method_policy(plan, _backend_surface())
    twice = _apply_method_policy(once, _backend_surface())

    assert once == twice
    assert [vp["id"] for vp in twice["verification_points"]] == [
        "VP-001", "VP-002", "VP-003",
    ]


# ---------------------------------------------------------------------------
# 4) 成本：缺省补 none，显式 llm_quota 原样保留
# ---------------------------------------------------------------------------


def test_policy_defaults_cost_to_none_and_keeps_llm_quota():
    """缺省 → ``cost="none"``；显式 ``cost="llm_quota"`` → 一个字节都不改。"""
    from verification_agent import _apply_method_policy

    plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "e2e",
                "target_url": "http://127.0.0.1:{{svc.api.port}}/",
            },
            {
                "id": "VP-002",
                "verification_method": "code_review",
                "cost": "llm_quota",
            },
        ],
    }

    points = _apply_method_policy(plan, _backend_surface())["verification_points"]

    assert points[0]["cost"] == "none"
    # 显式声明的成本不许被擅自降级成 "none"（那等于悄悄放行一条要花钱的验证）。
    assert points[1]["cost"] == "llm_quota"


def test_full_ci_vp_semantics_are_untouched():
    """``full_ci`` 的字段语义不变：``ci_entry`` / ``ci_timeout_seconds`` 不动。

    归一化只碰 e2e 的子类字段与成本字段；整仓门禁的入口与预算一个字都不许改。
    """
    from verification_agent import _apply_method_policy

    plan = {
        "verification_points": [
            {
                "id": "VP-017",
                "verification_method": "full_ci",
                "ci_entry": "python scripts/ci_local.py",
                "ci_timeout_seconds": 7200,
            },
        ],
    }

    vp = _apply_method_policy(plan, _backend_surface())["verification_points"][0]

    assert vp["ci_entry"] == "python scripts/ci_local.py"
    assert vp["ci_timeout_seconds"] == 7200
    assert "e2e_kind" not in vp
    assert "invalid_e2e" not in vp


# ---------------------------------------------------------------------------
# 5) 三份改动面样本（上游验收第 3 条）
# ---------------------------------------------------------------------------


def test_three_change_surface_samples():
    """三份样本：纯后端 / 前端模板 / 本地 CLI。

    * **纯后端** → 没有 ``ui_validation``，并且有一条 command 类 e2e；
    * **前端模板** → ``ui_validation`` 保留（且带 ``target_url``）；
    * **本地 CLI** → command 类 e2e，且 ``cost=none``。
    """
    # --- 样本一：纯后端 ---------------------------------------------------
    backend_plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "ui_validation",
                "target_url": "http://127.0.0.1:{{svc.api.port}}/dashboard",
            },
            {
                "id": "VP-002",
                "verification_method": "e2e",
                "entry_command": "python -m pytest tests/e2e/test_api.py -q",
            },
        ],
    }

    backend_points = _apply_policies(
        backend_plan, _backend_surface(),
    )["verification_points"]

    assert [vp["id"] for vp in backend_points] == ["VP-002"]
    assert all(
        vp["verification_method"] != "ui_validation" for vp in backend_points
    )
    assert backend_points[0]["e2e_kind"] == "command"

    # --- 样本二：前端模板 -------------------------------------------------
    frontend_plan = {
        "verification_points": [
            {
                "id": "VP-010",
                "verification_method": "ui_validation",
                "target_url": "http://127.0.0.1:{{svc.web.port}}/dashboard",
            },
            {
                "id": "VP-011",
                "verification_method": "e2e",
                "target_url": "http://127.0.0.1:{{svc.web.port}}/checkout",
            },
        ],
    }

    frontend_points = _apply_policies(
        frontend_plan, _frontend_surface(),
    )["verification_points"]

    assert [vp["id"] for vp in frontend_points] == ["VP-010", "VP-011"]
    ui_vp = frontend_points[0]
    assert ui_vp["verification_method"] == "ui_validation"
    assert ui_vp["target_url"] == (
        "http://127.0.0.1:{{svc.web.port}}/dashboard"
    )
    # 界面改动面下的 e2e 走 browser 子类（它有 target_url）。
    assert frontend_points[1]["e2e_kind"] == "browser"

    # --- 样本三：本地 CLI -------------------------------------------------
    cli_plan = {
        "verification_points": [
            {
                "id": "VP-020",
                "verification_method": "e2e",
                "entry_command": "python -m backend.cli --help",
            },
        ],
    }

    cli_points = _apply_policies(cli_plan, _cli_surface())["verification_points"]

    assert len(cli_points) == 1
    assert cli_points[0]["e2e_kind"] == "command"
    assert cli_points[0]["cost"] == "none"
    assert cli_points[0]["entry_command"] == "python -m backend.cli --help"


def test_three_samples_prompts_differ_by_surface(tmp_path):
    """三份改动面 → 三份提示词：界面与非界面的要求各出现一次，不互相污染。"""
    agent = _make_agent(tmp_path)

    backend_prompt = agent._build_planning_prompt(
        {"prd": "P"}, surface=_backend_surface(),
    )
    frontend_prompt = agent._build_planning_prompt(
        {"prd": "P"}, surface=_frontend_surface(),
    )
    cli_prompt = agent._build_planning_prompt(
        {"prd": "P"}, surface=_cli_surface(),
    )

    assert UI_TARGET_URL_RULE not in backend_prompt
    assert UI_TARGET_URL_RULE not in cli_prompt
    assert UI_TARGET_URL_RULE in frontend_prompt
    # 非界面改动面（纯后端 / 本地 CLI）都要"至少一条 command 类 e2e"。
    for prompt in (backend_prompt, cli_prompt):
        assert "至少有一条" in prompt
        assert "`command`" in prompt
        assert "`none`" in prompt
