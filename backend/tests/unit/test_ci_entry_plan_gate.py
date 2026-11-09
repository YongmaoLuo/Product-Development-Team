"""Plan-phase gate for the ``full_ci`` entry shape (2026-11-06).

背景
----
VP-022 是这份计划里唯一一条 ``full_ci``（整仓门禁）验证点。它的 ``ci_entry``
冻结成了一条**内联复合命令**::

    (test -e backend/.venv || ln -sfn <本机绝对路径> backend/.venv) && bash scripts/run_tests.sh

这条入口有两个问题，而且**都不在运行器里**：

1. **形态不合规**（2026-11-05 arch 决策点 2）：整仓门禁必须由**项目适配脚本**
   （``<project>/scripts/ci_gate.sh``）执行，不能是内联复合命令 —— 内联命令把
   「跑什么、失败了算谁的」写进了 ``ci_entry``，同一条门禁在不同机器上跑的是
   不同的东西。
2. **不可移植**：入口里那条绝对路径指向某一台机器的 venv。

第 2 条曾经还有一个更早的形状（路径读取器把 ``backend/.venv)`` 当成路径，
于是 VP 在跑任何一条命令之前就被 schema 校验拒绝 —— 那一条已经由
``backend/verification_ci_runner.py`` 修掉了：分组括号被剥掉、入口自建的路径
豁免存在性检查。**剩下的缺口在计划层**：生成 / 复用这条链路上没有任何一处
调用 ``verification_ci_runner.validate_vp``，所以一条跑不起来的入口能被写进
计划、被整份复用、每一轮都在执行期第一次被看见。

``verification_ci_runner.validate_vp`` 的 docstring 早就写着「Called at
plan-generation time (so a malformed ``full_ci`` plan is regenerated rather
than run)」—— 这条承诺一直没有接线。本模块钉的就是这次接线。

两处接线，语义不同：

* **生成路径**（:meth:`VerificationAgent._ci_entry_report` 挂进重生成回路）：
  违规**打回**给生成 LLM 重写 —— 新计划不许再长成这个形状。
* **复用路径**（:meth:`VerificationAgent._annotate_reused_ci_entry_violations`）：
  违规**只标注、不阻断** —— 盘上那份计划已经跑过多轮，阻断它等于让这一轮原地
  失败；而 ``ci_entry`` 不合规 ≠ 永远判不了（那是 ``api_test`` 断言不合规才有的
  语义），两者不能照抄同一种强度。

本模块全部是纯函数级别的断言：不碰网络、不跑门禁。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_agent import VerificationAgent  # noqa: E402
from verification_ci_runner import validate_vp  # noqa: E402


#: VP-022 冻结入口的**形状**（逐字取自
#: ``plans/<plan-id>/verification_plan.json``）。
#:
#: 绝对路径换成了 ``/opt/shared/venv``：入口里那条路径**本机专属**，而门禁
#: 对绝对路径不做存在性检查 —— 换掉它不影响任何一条断言，留在测试里反而会把
#: 一台机器的目录结构写进仓里。
VP022_FROZEN_ENTRY = (
    "(test -e backend/.venv || ln -sfn /opt/shared/venv backend/.venv) && "
    "bash scripts/run_tests.sh"
)


def _make_agent(tmp_path: Path) -> VerificationAgent:
    """Build a VerificationAgent wired to a throwaway plan/project dir.

    ``_ci_entry_report`` 与 ``_annotate_reused_ci_entry_violations`` 是
    计划对象上的纯方法 —— 它们不需要真实项目布局，只需要构造函数的表面
    （它会往 PATH 前置 venv）。空目录就够。
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _adapter_project(tmp_path: Path) -> Path:
    """A project that ships the CI adapter script the shape contract wants."""
    project_dir = tmp_path / "project"
    (project_dir / "scripts").mkdir(parents=True, exist_ok=True)
    script = project_dir / "scripts" / "ci_gate.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return project_dir


def _vp(vp_id: str = "VP-022", **over) -> dict:
    """A ``full_ci`` VP whose entry calls the project's adapter script."""
    vp = {
        "id": vp_id,
        "title": "整仓门禁",
        "verification_method": "full_ci",
        "ci_entry": "bash scripts/ci_gate.sh",
    }
    vp.update(over)
    return vp


# ---------------------------------------------------------------------------
# 1. 计划层闸门报出内联复合入口
# ---------------------------------------------------------------------------


def test_the_frozen_vp022_entry_is_reported_by_the_plan_gate(tmp_path: Path):
    """VP-022 那条冻结入口在计划期必须被报出来。

    这是整件事的起点：入口被写进计划、被整份复用、每一轮都在执行期第一次被
    看见 —— 因为生成 / 复用链路上没有任何一处问过「这条入口能不能跑」。
    """
    agent = _make_agent(tmp_path)

    report = agent._ci_entry_report({"verification_points": [
        _vp(ci_entry=VP022_FROZEN_ENTRY),
    ]})

    assert [r["id"] for r in report] == ["VP-022"], (
        f"_ci_entry_report should flag the frozen compound entry; got "
        f"{report!r}"
    )
    assert any("操作符" in d for d in report[0]["issues"]), (
        f"the issue must name the shell operators so the planner knows what "
        f"to rewrite; got {report[0]['issues']!r}"
    )


def test_the_adapter_entry_the_planner_is_taught_is_not_reported(tmp_path: Path):
    """闸门不能报它自己教的那条入口 —— 那是死循环。

    生成 LLM 被告知要写 ``bash scripts/ci_gate.sh``；闸门必须接受这一条，
    否则每次重试都产出同一条违规，重生成回路永远收敛不了。
    """
    _adapter_project(tmp_path)
    agent = _make_agent(tmp_path)

    assert agent._ci_entry_report({"verification_points": [_vp()]}) == [], (
        "_ci_entry_report flagged the adapter entry it teaches — "
        "regeneration would be a fixed point."
    )
    assert validate_vp(_vp(), project_dir=tmp_path / "project") == [], (
        f"validate_vp returned issues for the taught entry: "
        f"{[i.to_dict() for i in validate_vp(_vp(), project_dir=tmp_path / 'project')]!r}"
    )


def test_an_entry_naming_a_script_the_project_does_not_have_is_reported(
    tmp_path: Path,
):
    """入口点名一个项目里没有的脚本 —— 报出来。

    这一条不是形态问题而是**存在性**问题：入口指向的脚本不存在，门禁跑起来
    只会得到一个「文件不存在」，和整仓门禁的结论毫无关系。
    """
    agent = _make_agent(tmp_path)

    report = agent._ci_entry_report({"verification_points": [
        _vp(ci_entry="bash scripts/ci_gate.sh"),
    ]})

    assert [r["id"] for r in report] == ["VP-022"], (
        f"_ci_entry_report should flag an entry naming a script the project "
        f"does not have; got {report!r}"
    )
    assert any("scripts/ci_gate.sh" in d for d in report[0]["issues"]), (
        f"the issue must name the missing path; got {report[0]['issues']!r}"
    )


# ---------------------------------------------------------------------------
# 2. 闸门只管 full_ci
# ---------------------------------------------------------------------------


def test_the_gate_only_inspects_full_ci_vps(tmp_path: Path):
    """别的验证方法不进这条闸门。

    ``api_test`` 有自己的 schema 闸门，``e2e`` 有自己的入口契约；``ci_entry``
    这一条只对 ``full_ci`` 有意义。
    """
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [
        {"id": "VP-API", "verification_method": "api_test", "title": "x"},
        {"id": "VP-CR", "verification_method": "code_review", "title": "x"},
        {"id": "VP-E2E", "verification_method": "e2e", "title": "x"},
    ]}

    assert agent._ci_entry_report(plan) == [], (
        f"_ci_entry_report must NOT touch non-full_ci VPs; got "
        f"{agent._ci_entry_report(plan)!r}"
    )


def test_the_gate_survives_malformed_plans(tmp_path: Path):
    """计划是模型吐出来的 —— 闸门自己不能崩。

    ``_api_schema_report`` 有同样的约定：门禁自己抛异常，会把一条可读的原因
    变成一条栈回溯，而后者对操作员没有任何意义。
    """
    agent = _make_agent(tmp_path)

    assert agent._ci_entry_report(None) == []
    assert agent._ci_entry_report({}) == []
    assert agent._ci_entry_report({"verification_points": "not-a-list"}) == []
    assert agent._ci_entry_report({"verification_points": ["not-a-dict"]}) == []
    assert agent._ci_entry_report(
        {"verification_points": [None, 42]},
    ) == []


# ---------------------------------------------------------------------------
# 3. 重生成回路的反馈：教的是适配脚本，不是别的
# ---------------------------------------------------------------------------


def test_the_fix_guidance_teaches_the_adapter_shape(tmp_path: Path):
    """违规要带着「怎么改」回到生成 LLM 那里。

    提示词不是闸门，但闸门报出来的问题如果没有可执行的改法，重生成回路
    只会再吐一遍同样的入口。
    """
    agent = _make_agent(tmp_path)

    text = agent._violation_fix_guidance(
        service_violations=[],
        api_schema_violations=[],
        gate_gaps=[],
        ci_entry_violations=[{"id": "VP-022"}],
    )

    assert "ci_gate.sh" in text, (
        f"the ci_entry guidance must teach the project adapter script; "
        f"got {text!r}"
    )
    assert "ci_entry" in text, (
        f"the guidance must name the field it is about; got {text!r}"
    )


def test_the_guidance_has_no_ci_entry_paragraph_without_ci_entry_violations(
    tmp_path: Path,
):
    """没有这一类违规时就不出现这一段话 —— 每类违规带自己的教法。

    照 :meth:`VerificationAgent._violation_fix_guidance` 的约定：一段针对
    入口形态的话不该出现在「api_test 断言不合规」的那份反馈里。
    """
    agent = _make_agent(tmp_path)

    text = agent._violation_fix_guidance(
        service_violations=[],
        api_schema_violations=[{"id": "VP-007"}],
        gate_gaps=[],
    )

    assert "ci_gate.sh" not in text, (
        f"the ci_entry guidance must not leak into another violation "
        f"family's feedback; got {text!r}"
    )


def test_the_guidance_names_the_gate_script_the_repo_actually_has(
    tmp_path: Path,
):
    """仓库的门禁脚本不叫 ``ci_gate.sh`` 时，教法要指出真正存在的那一条。

    否则规划 LLM 每一轮都收到同一句「用 ``scripts/ci_gate.sh``」，而那条脚本
    在这个仓库里根本不存在 —— 重生成回路原地打转：每一轮烧一次 LLM 调用，
    却永远收敛不了。
    """
    project_dir = tmp_path / "project"
    (project_dir / "scripts").mkdir(parents=True)
    (project_dir / "scripts" / "ci_local.py").write_text("print()\n")
    agent = _make_agent(tmp_path)

    text = agent._violation_fix_guidance(
        service_violations=[],
        api_schema_violations=[],
        gate_gaps=[],
        ci_entry_violations=[{"id": "VP-022"}],
    )

    assert "scripts/ci_local.py" in text, (
        f"the guidance must name the gate script the repo actually has; "
        f"got {text!r}"
    )


# ---------------------------------------------------------------------------
# 4. 接线：闸门真的在重生成回路里
# ---------------------------------------------------------------------------
#
# 上面几条钉的是**零件**：报告函数、反馈教法、复用路径的标注。零件都在，
# 接线没接，也是零效果 —— 报告里那句「计划层无校验」的证据正是
# ``grep -rn 'validate_vp' backend/`` 只看得到执行期那一处。
#
# 所以这一节**驱动真实的生成回路**：给它一个只会吐坏入口的假 LLM，
# 看它到底被问了几次、第二次的提示词里有没有那条违规。


class _FakeCodingTool:
    """A coding tool that hands out canned plans and records its prompts."""

    def __init__(self, plans):
        self.plans = list(plans)
        self.prompts = []

    def query_json(self, prompt, system_instruction=None, **kwargs):
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.plans) - 1)
        return self.plans[index]


def _plan_with(entry: str) -> dict:
    return {"verification_points": [_vp(ci_entry=entry)]}


def test_the_generation_loop_rejects_a_compound_entry_and_re_asks(tmp_path: Path):
    """坏入口在**第 1 轮生成**就被打回，生成 LLM 被重新问一次。

    这就是 L2 的全部意义：同样的入口不再等到第 4 轮执行期才第一次被看见。
    """
    _adapter_project(tmp_path)
    tool = _FakeCodingTool([
        _plan_with(VP022_FROZEN_ENTRY),      # 第 1 版：内联复合入口
        _plan_with("bash scripts/ci_gate.sh"),  # 第 2 版：改成适配脚本
    ])
    agent = VerificationAgent(
        plan_dir=tmp_path / "plan",
        project_dir=tmp_path / "project",
        coding_tool=tool,
    )

    agent.generate_verification_plan(retry_llm=3)

    assert len(tool.prompts) == 2, (
        f"the compound entry must be rejected on the first generation; "
        f"got {len(tool.prompts)} LLM call(s)"
    )
    second_prompt = tool.prompts[1]
    assert "整仓门禁入口不合规" in second_prompt, (
        f"the re-ask must carry the ci_entry violation section; got "
        f"{second_prompt[-2000:]!r}"
    )
    assert "ci_entry" in second_prompt, (
        f"the re-ask must name the field it is about; got "
        f"{second_prompt[-2000:]!r}"
    )


def test_a_conforming_entry_is_accepted_on_the_first_generation(tmp_path: Path):
    """合规入口**一次**就过 —— 闸门不能给每一份计划都加一轮。"""
    _adapter_project(tmp_path)
    tool = _FakeCodingTool([_plan_with("bash scripts/ci_gate.sh")])
    agent = VerificationAgent(
        plan_dir=tmp_path / "plan",
        project_dir=tmp_path / "project",
        coding_tool=tool,
    )

    agent.generate_verification_plan(retry_llm=3)

    assert len(tool.prompts) == 1, (
        f"a conforming entry must not cost a regeneration; got "
        f"{len(tool.prompts)} LLM call(s)"
    )


# ---------------------------------------------------------------------------
# 5. 复用路径：只标注，不阻断
# ---------------------------------------------------------------------------


def test_the_reused_plan_is_annotated_but_not_blocked(tmp_path: Path):
    """盘上那份计划：违规要看得见，但不能让它原地失败。

    ``ci_entry`` 不合规 ≠ 永远判不了 —— 那条冻结入口在运行器修好之后**是能
    跑的**（执行期不做形态检查）。所以复用路径只标注 + 记日志，不抛异常、
    不阻断这一轮。

    阻断的强度只属于「运行器永远判不了」的那一类（``api_test`` 断言不合规）。
    """
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [_vp(ci_entry=VP022_FROZEN_ENTRY)]}

    violations = agent._annotate_reused_ci_entry_violations(plan)

    assert [v["id"] for v in violations] == ["VP-022"], (
        f"the reused plan's entry violations must be surfaced; got "
        f"{violations!r}"
    )
    # 违规被写进计划里 —— 缺口必须可见。
    assert "ci_entry" in json.dumps(plan, ensure_ascii=False), (
        f"the reused plan must carry the violation so the operator sees it; "
        f"got {plan!r}"
    )


def test_a_clean_reused_plan_is_not_annotated(tmp_path: Path):
    """干净的复用计划不留痕 —— 标注是给有缺口的那份用的。"""
    _adapter_project(tmp_path)
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [_vp()]}

    assert agent._annotate_reused_ci_entry_violations(plan) == []
    # 断言的是**注解字段**，不是 ``ci_entry`` 这个词本身 —— VP 自己本来就带
    # ``ci_entry`` 字段（那是要跑的入口），注解字段是另一回事。
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "ci_entry_violations" not in serialized, (
        f"a clean reused plan must not carry a violation annotation; got "
        f"{serialized!r}"
    )
    assert "ci_entry_issues" not in serialized, (
        f"a clean reused plan must not carry a per-VP annotation; got "
        f"{serialized!r}"
    )


# ---------------------------------------------------------------------------
# 6. 提示词：形状约束要写出来
# ---------------------------------------------------------------------------


def _full_ci_spec_paragraph(prompt: str) -> str:
    """The ``full_ci`` bullet of the field-spec list in the prompt."""
    start = prompt.index("   - full_ci: **Phase 2 专用")
    end = prompt.index("\n4. ", start)
    return prompt[start:end]


def test_the_prompt_states_the_three_shape_constraints():
    """提示词必须把三条形状约束写出来。

    提示词不是闸门 —— 真正的闸门是 :meth:`VerificationAgent._ci_entry_report`。
    但闸门报出来的问题如果没有可执行的改法，重生成回路只会再吐一遍同样的
    入口：模型必须同时知道「该怎么写」和「为什么这么写」。
    """
    from prompts import VERIFICATION_PLAN_SYSTEM_PROMPT

    spec = _full_ci_spec_paragraph(VERIFICATION_PLAN_SYSTEM_PROMPT)

    assert "绝对路径" in spec, (
        f"the full_ci spec must forbid absolute paths (they are not "
        f"portable and are never existence-checked); got {spec!r}"
    )
    assert "相对仓库根" in spec, (
        f"the full_ci spec must require repo-relative paths; got {spec!r}"
    )
    assert "不要在入口里自己造环境" in spec, (
        f"the full_ci spec must forbid building the environment inside the "
        f"entry; got {spec!r}"
    )
