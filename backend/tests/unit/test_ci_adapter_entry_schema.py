"""整仓门禁的入口形态：一次适配脚本调用，不是内联复合命令。

为什么要有这条规则（2026-11-05，arch 决策点 2 —— VP 校验）
----------------------------------------------------------
整仓门禁必须由**项目适配脚本**执行。一条内联复合命令
（``(test -e backend/.venv) && bash scripts/run_tests.sh``）把「跑不跑」、
「跑哪一段」、「失败了算谁的」全塞进了 ``ci_entry`` 里 —— 于是同一条门禁在
不同机器上跑的是**不同的东西**，退出码也不再是「整仓门禁」这一条判据。

规则落在**规划期**（``validate_vp`` 的缺省行为）：不合形态的入口在生成阶段
就被退回重写，而不是跑到一半才发现判据不对。

执行期**不启用**这条新规则
--------------------------
历史计划里的复合入口不重写 —— 执行时仍然要能跑。所以两个执行入口
（``run_ci_verification`` 与分派器 ``_runner_issues``）都传
``require_single_entry=False``：它们判的是「这条验证点能不能执行」，
不是「这条入口合不合规划期的形态契约」。

本模块钉住四件事：

1. 含 shell 操作符的 ``ci_entry`` 在规划期被拒；
2. 单条脚本调用零 issue；
3. 执行期仍然执行复合入口（历史计划不重写）；
4. 空入口仍然只报「缺字段」，不会被新规则改写；重定向同样被拒。
"""

from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    STATUS_PASSED,
    compound_operators,
    run_ci_verification,
    validate_vp,
)


#: 一条内联复合命令 —— 「先探环境，再跑门禁」全写在 ``ci_entry`` 里。
COMPOUND_ENTRY = "(test -e backend/.venv) && bash scripts/run_tests.sh"


def _vp(entry, **extra):
    vp = {
        "id": "VP-1",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


# ---------------------------------------------------------------------------
# 规划期：内联复合命令被拒
# ---------------------------------------------------------------------------


def test_compound_ci_entry_is_rejected():
    """含 ``&&`` 的入口在规划期判为不可运行。"""
    issues = validate_vp(_vp(COMPOUND_ENTRY))

    assert issues, "内联复合命令必须被判为不可运行"
    assert len(issues) == 1, [issue.to_dict() for issue in issues]
    detail = issues[0].detail
    assert "复合" in detail, detail
    assert "适配脚本" in detail, detail


def test_single_script_entry_has_no_issue():
    """一次适配脚本调用是唯一合法的形态 —— 零 issue。"""
    assert validate_vp(_vp("bash scripts/ci_gate.sh")) == []


def test_redirect_is_rejected():
    """重定向（``2>&1``）同样把 shell 语义塞进了入口 —— 一样被拒。"""
    issues = validate_vp(_vp("bash scripts/ci_gate.sh 2>&1"))

    assert issues, "重定向入口必须被判为不可运行"
    assert "复合" in issues[0].detail, issues[0].detail


def test_empty_entry_still_reports_only_the_missing_field():
    """空入口只报原有的「缺字段」错误 —— 新规则不改写它。"""
    issues = validate_vp({"id": "VP-1", "verification_method": "full_ci"})

    assert len(issues) == 1, [issue.to_dict() for issue in issues]
    assert "ci_entry" in issues[0].detail, issues[0].detail
    assert "复合" not in issues[0].detail, issues[0].detail


def test_reported_operators_are_not_redundant():
    """``||`` 已经覆盖 ``|`` —— 报两遍是噪声，不是信息。"""
    assert compound_operators("a || b") == ["||"]
    assert compound_operators("a && b") == ["&&"]
    assert compound_operators("(a) && b") == ["&&", "(", ")"]


# ---------------------------------------------------------------------------
# 执行期：历史计划不重写，复合入口仍然能跑
# ---------------------------------------------------------------------------


def test_execution_path_accepts_compound_entry(tmp_path):
    """执行期不启用新规则 —— 复合入口照跑，判定只看退出码。"""
    gate = tmp_path / "gate.sh"
    gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gate.chmod(0o755)

    result = run_ci_verification(
        _vp(f"(test -f {gate.name}) && bash {gate.name}"),
        project_dir=tmp_path,
    )

    assert result.status == STATUS_PASSED, result.reasons
    assert result.evidence["exit_code"] == 0, result.evidence
    assert "schema_issues" not in result.evidence, result.evidence


def test_execution_validator_flag_disables_the_compound_rule():
    """``require_single_entry=False`` 是一条显式的豁免，不是默认行为。"""
    assert validate_vp(_vp(COMPOUND_ENTRY), require_single_entry=False) == []


def test_dispatcher_does_not_escalate_a_legacy_compound_entry():
    """分派器判的是「能不能执行」—— 复合入口仍走确定性 runner。

    历史计划里的复合入口不重写，所以它不能被升级到 LLM 路径：那条路上
    判定落在子 agent 的自报上，而整仓门禁的判定必须是退出码的纯函数。
    """
    route, mode = verification_subagent.resolve(_vp(COMPOUND_ENTRY))

    assert route == "verification_ci_runner", route
    assert mode == verification_subagent.DETERMINISTIC_MODE, mode
