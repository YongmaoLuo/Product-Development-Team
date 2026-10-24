"""验证计划**增量**评估（每轮增减 VP）的单元测试。

（2026-09-16）：

* 每次验证开始前重新评估 VP 方案是否合理，需要增减就增减；
* 新增/废弃只需要日志记录，不要人工审批；
* **绝不允许修改已有 VP**（会因指纹失效作废已挣到的结论）；
* 数量不设上限，以现实为准。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _plan(*vps):
    return {"verification_points": list(vps)}


def _vp(vp_id, title="t", **extra):
    vp = {
        "id": vp_id,
        "title": title,
        "verification_method": "automated_test",
        "test_command": f"pytest tests/{vp_id}.py",
        "related_prd_criteria": "验收标准 1",
        "verification_phase": 1,
    }
    vp.update(extra)
    return vp


# ---------------------------------------------------------------------------
# 输入采集
# ---------------------------------------------------------------------------

def test_collect_new_tasks_returns_only_unseen(tmp_path: Path) -> None:
    from verification_plan_delta import collect_new_tasks
    (tmp_path / "tasks.json").write_text(json.dumps({
        "tasks": [
            {"id": "1", "title": "原始任务"},
            {"id": "repair-r1-01", "title": "修复：补 VP-009 覆盖"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    fresh, all_ids = collect_new_tasks(tmp_path, ["1"])
    assert [t["id"] for t in fresh] == ["repair-r1-01"]
    assert all_ids == ["1", "repair-r1-01"]


def test_collect_new_tasks_tolerates_missing_file(tmp_path: Path) -> None:
    from verification_plan_delta import collect_new_tasks
    assert collect_new_tasks(tmp_path, []) == ([], [])


def test_collect_acceptance_reads_prd(tmp_path: Path) -> None:
    from verification_plan_delta import collect_acceptance
    (tmp_path / "prd.json").write_text(json.dumps({
        "acceptance": [
            "AC-1: 9/3 14:09 盘整信号 marker 必须发出",
            {"criteria": "AC-2: 9/2 三个 spurious 不被发出"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    out = collect_acceptance(tmp_path)
    assert len(out) == 2
    assert "AC-1" in out[0]


def test_next_vp_id_avoids_collisions() -> None:
    from verification_plan_delta import next_vp_id
    assert next_vp_id(_plan(_vp("VP-001"), _vp("VP-030"))) == "VP-031"
    assert next_vp_id(_plan()) == "VP-001"


# ---------------------------------------------------------------------------
# 硬约束：不允许修改已有 VP
# ---------------------------------------------------------------------------

def test_add_reusing_existing_id_is_rejected() -> None:
    from verification_plan_delta import parse_delta_payload
    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload({
        "add": [{
            "id": "VP-001",                      # ← 复用已有 id = 改已有 VP
            "title": "换个说法",
            "reason": "我觉得断言应该更严",
            "test_command": "pytest tests/other.py",
        }],
    }, plan)
    assert delta.added == []
    assert delta.rejected_modifications
    assert "VP-001" in delta.rejected_modifications[0]["id"]


def test_add_without_reason_is_rejected() -> None:
    from verification_plan_delta import parse_delta_payload
    delta = parse_delta_payload({
        "add": [{"title": "顺手加一条", "test_command": "pytest tests/x.py"}],
    }, _plan(_vp("VP-001")))
    assert delta.added == []
    assert delta.rejected_additions
    assert "reason" in delta.rejected_additions[0]["why"]


def test_add_with_reason_is_accepted() -> None:
    from verification_plan_delta import parse_delta_payload
    delta = parse_delta_payload({
        "add": [{
            "title": "修复任务 repair-r1-01 引入的新行为验收",
            "related_prd_criteria": "repair-r1-01",
            "reason": "本轮新增了该任务，当前没有任何 VP 覆盖它",
            "test_command": "pytest tests/test_new.py -k test_new",
        }],
    }, _plan(_vp("VP-001")))
    assert len(delta.added) == 1


# ---------------------------------------------------------------------------
# 废弃
# ---------------------------------------------------------------------------

def test_obsolete_marks_without_deleting() -> None:
    from verification_plan_delta import apply_delta, parse_delta_payload
    plan = _plan(_vp("VP-001"), _vp("VP-002"))
    delta = parse_delta_payload(
        {"obsolete": [{"id": "VP-002", "reason": "该功能已从计划范围移除"}]},
        plan,
    )
    summary = apply_delta(plan, delta, round_number=3)

    assert summary["obsoleted"] == [
        {"id": "VP-002", "reason": "该功能已从计划范围移除"}
    ]
    # 标记而非删除 —— 审计要用
    assert len(plan["verification_points"]) == 2
    vp2 = plan["verification_points"][1]
    assert vp2["obsolete"] is True
    assert vp2["obsolete_reason"] == "该功能已从计划范围移除"
    assert vp2["obsoleted_round"] == 3


def test_obsolete_unknown_id_is_rejected() -> None:
    from verification_plan_delta import parse_delta_payload
    delta = parse_delta_payload(
        {"obsolete": [{"id": "VP-999", "reason": "x"}]}, _plan(_vp("VP-001")),
    )
    assert delta.obsoleted == []
    assert delta.rejected_obsoletes


def test_obsolete_missing_reason_is_rejected() -> None:
    from verification_plan_delta import parse_delta_payload
    delta = parse_delta_payload(
        {"obsolete": [{"id": "VP-001"}]}, _plan(_vp("VP-001")),
    )
    assert delta.obsoleted == []
    assert delta.rejected_obsoletes


def test_obsolete_is_idempotent() -> None:
    from verification_plan_delta import parse_delta_payload
    plan = _plan(_vp("VP-001", obsolete=True))
    delta = parse_delta_payload(
        {"obsolete": [{"id": "VP-001", "reason": "again"}]}, plan,
    )
    assert delta.obsoleted == []


# ---------------------------------------------------------------------------
# 应用：新增 VP 的形态
# ---------------------------------------------------------------------------

def test_applied_vps_land_in_phase_one_with_audit_fields() -> None:
    from verification_plan_delta import apply_delta, parse_delta_payload
    from verification_phases import PHASE_CORE
    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload({
        "add": [{
            "title": "新行为验收", "reason": "repair-r1-02 引入",
            "test_command": "pytest tests/test_x.py -k test_y",
        }],
    }, plan)
    summary = apply_delta(plan, delta, round_number=2)

    new_vp = plan["verification_points"][-1]
    assert new_vp["id"] == "VP-002"
    assert new_vp["verification_phase"] == PHASE_CORE   # 新增落 Phase 1
    assert new_vp["added_reason"] == "repair-r1-02 引入"
    assert new_vp["added_round"] == 2
    assert summary["added"][0]["id"] == "VP-002"


def test_multiple_additions_get_distinct_ids() -> None:
    from verification_plan_delta import apply_delta, parse_delta_payload
    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload({
        "add": [
            {"title": "a", "reason": "r1", "test_command": "pytest a"},
            {"title": "b", "reason": "r2", "test_command": "pytest b"},
        ],
    }, plan)
    apply_delta(plan, delta, round_number=2)
    ids = [vp["id"] for vp in plan["verification_points"]]
    assert len(ids) == len(set(ids)) == 3


def test_delta_summary_counts_rejections_for_audit() -> None:
    """新增/废弃只需日志，但必须能回溯 —— 拒绝也要计数。"""
    from verification_plan_delta import apply_delta, parse_delta_payload
    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload({
        "add": [
            {"id": "VP-001", "title": "改已有", "reason": "x"},
            {"title": "没依据"},
        ],
        "obsolete": [{"id": "VP-404"}],
    }, plan)
    summary = apply_delta(plan, delta, round_number=2)
    assert summary["added"] == [] and summary["obsoleted"] == []
    assert summary["rejected_modifications"] == 1
    assert summary["rejected_additions"] == 1
    assert summary["rejected_obsoletes"] == 1


# ---------------------------------------------------------------------------
# 提示词与状态
# ---------------------------------------------------------------------------

def test_delta_prompt_lists_existing_vps_and_new_tasks() -> None:
    from verification_plan_delta import build_delta_prompt
    prompt = build_delta_prompt(
        _plan(_vp("VP-001", title="旧验证点")),
        [{"id": "repair-r1-01", "title": "修复任务", "description": "d"}],
        ["AC-1: 某某"],
        round_number=2,
    )
    assert "VP-001" in prompt
    assert "repair-r1-01" in prompt
    assert "AC-1" in prompt
    assert "禁止修改" in prompt


def test_delta_state_roundtrip(tmp_path: Path) -> None:
    from verification_plan_delta import load_delta_state, save_delta_state
    save_delta_state(tmp_path, {"seen_task_ids": ["1", "repair-r1-01"]})
    assert load_delta_state(tmp_path)["seen_task_ids"] == ["1", "repair-r1-01"]


def test_load_delta_state_tolerates_garbage(tmp_path: Path) -> None:
    from verification_plan_delta import load_delta_state
    (tmp_path / "verification_plan_delta_state.json").write_text("{not json", encoding="utf-8")
    assert load_delta_state(tmp_path) == {}


# ---------------------------------------------------------------------------
# 执行器：废弃 VP 不跑
# ---------------------------------------------------------------------------

def test_executor_skips_obsolete_vps(tmp_path: Path) -> None:
    import asyncio
    from verification_executor import VerificationExecutor

    plan = {"vps": [
        {"id": "VP-001", "method": "automated_test", "title": "a",
         "test_command": "pytest a"},
        {"id": "VP-002", "method": "automated_test", "title": "b",
         "test_command": "pytest b", "obsolete": True},
    ]}
    calls = []

    async def runner(vp):
        calls.append(vp["id"])
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    ex = VerificationExecutor(
        verification_plan=plan, plan_id="p", plan_dir=plan_dir,
        sub_agent_runner=runner, verif_repo=None,
    )
    asyncio.run(ex.run(max_parallel=1))
    assert calls == ["VP-001"], "废弃的 VP 不该被执行"


# ---------------------------------------------------------------------------
# 方法词汇表 + 阶段声明（2026-09-19）
# ---------------------------------------------------------------------------


def test_added_vp_without_a_method_does_not_default_to_the_retired_one() -> None:
    """省略 method 时不再凭空造出退休的 ``automated_test``。

    那个默认值会让一条 method 缺失的新增变成**永远跑不出结论**的 VP：方法
    已不在 ``SUPPORTED_METHODS`` 里，只能被 retired / SKIPPED。空串会让
    ``_maybe_evaluate_plan_delta`` 的护栏把它整条丢掉。
    """
    from verification_plan_delta import apply_delta, parse_delta_payload

    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload(
        {"add": [{"title": "没有写方法", "reason": "依据"}]}, plan,
    )
    apply_delta(plan, delta, round_number=2)

    new_vp = plan["verification_points"][-1]
    assert new_vp["verification_method"] == "", (
        f"missing method must stay empty so the guard drops the whole VP, "
        f"got {new_vp['verification_method']!r}"
    )


def test_added_vp_has_no_test_command_field() -> None:
    """VP 早已没有 ``test_command``（C8），增量路径不许把它塞回来。"""
    from verification_plan_delta import apply_delta, parse_delta_payload

    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload(
        {
            "add": [{
                "title": "新行为验收", "reason": "依据",
                "verification_method": "api_test",
                "test_command": "pytest tests/test_x.py",
            }],
        },
        plan,
    )
    apply_delta(plan, delta, round_number=2)

    assert "test_command" not in plan["verification_points"][-1]


def test_added_vp_honours_a_declared_phase_two() -> None:
    """声明为 Phase 2 的全量关卡必须落在 Phase 2。

    增量路径原来硬编码 Phase 1，于是一条「Nightly CI 全过」被按 Phase 1
    排进去 —— 在每条子功能都还没验完时就开跑，整轮只能 SKIPPED。
    """
    from verification_plan_delta import apply_delta, parse_delta_payload
    from verification_phases import PHASE_FINAL_GATE

    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload(
        {
            "add": [{
                "title": "Nightly CI 全过", "reason": "本轮新增门禁要求",
                "verification_method": "full_ci",
                "verification_phase": 2,
                "ci_entry": "python scripts/ci_local.py",
            }],
        },
        plan,
    )
    apply_delta(plan, delta, round_number=2)

    new_vp = plan["verification_points"][-1]
    assert new_vp["verification_phase"] == PHASE_FINAL_GATE, (
        "a VP that declares itself a Phase-2 gate must not be scheduled "
        "into Phase 1"
    )


def test_added_vp_with_an_illegal_phase_falls_back_to_phase_one() -> None:
    """非法 ``verification_phase`` 退回 Phase 1，而不是崩掉增量评估。"""
    from verification_plan_delta import apply_delta, parse_delta_payload
    from verification_phases import PHASE_CORE

    plan = _plan(_vp("VP-001"))
    delta = parse_delta_payload(
        {
            "add": [{
                "title": "x", "reason": "r",
                "verification_method": "api_test",
                "verification_phase": "two",
            }],
        },
        plan,
    )
    apply_delta(plan, delta, round_number=2)

    assert plan["verification_points"][-1]["verification_phase"] == PHASE_CORE


def test_delta_prompt_teaches_the_current_method_vocabulary() -> None:
    """Delta prompt 是第二条生成路径，方法清单必须跟上。

    它曾经仍写着 ``automated_test``、漏掉 ``e2e``/``full_ci``、还把已删除的
    ``test_command`` 描述成必填 —— 于是增量生成的 VP 会带着退休方法进计划。
    """
    from verification_plan_delta import DELTA_SYSTEM_PROMPT
    from verification_subagent import SUPPORTED_METHODS

    assert "automated_test" not in DELTA_SYSTEM_PROMPT
    assert "test_command" not in DELTA_SYSTEM_PROMPT
    for method in SUPPORTED_METHODS:
        assert method in DELTA_SYSTEM_PROMPT, (
            f"{method!r} missing from the delta prompt"
        )
    assert "verification_phase" in DELTA_SYSTEM_PROMPT


def test_added_api_test_keeps_the_fields_that_make_it_runnable() -> None:
    """新增的 api_test VP 必须**原样**落进计划，否则整条路是堵死的。

    2026-10-14（本 plan 的 VP-009 退休）：``apply_delta`` 的 add 分支按一份
    写死白名单建 VP，``request`` / ``assertions`` / ``target_url`` /
    ``timeout_seconds`` / ``execution_group`` 从不被读取 —— 于是 delta 明明
    写了，落地却消失。新 VP 因缺 ``request`` 被
    ``verification_api_runner.validate_vp`` 在 schema 阶段判为不可运行，
    又被 ``verification_agent._maybe_evaluate_plan_delta`` 的护栏**整条丢弃**。

    后果不是「少一条 VP」，而是「作废旧 VP + 新增一条表达正确的」这条路在框架层
    根本不存在 —— 只能看着旧判据继续失败。所以这里钉的是**透传**：计划里
    落地的那条 VP，经 ``validate_vp`` 检查必须零 issue。
    """
    from verification_api_runner import validate_vp
    from verification_plan_delta import apply_delta, parse_delta_payload

    plan = _plan(_vp("VP-009"))
    item = {
        "title": "空窗与数据源故障可区分",
        "reason": "接替已作废的 VP-009",
        "related_prd_criteria": "PRD 验收 1 / 测试设计决策点 9",
        "verification_method": "api_test",
        "expected_result": "200 + 空数组；同形状注入故障时是 5xx",
        "request": {
            "method": "GET",
            "url": "http://127.0.0.1:1/never-reached",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 240,
        },
        "assertions": [{"name": "状态码 200（不是 5xx）", "status": 200}],
        "target_url": "http://127.0.0.1:1/never-reached",
        "timeout_seconds": 3600,
        "execution_group": 0,
    }

    apply_delta(plan, parse_delta_payload({"add": [item], "obsolete": []}, plan), 4)

    added = plan["verification_points"][-1]
    assert added["id"] == "VP-010"
    for key in ("request", "assertions", "target_url",
                "timeout_seconds", "execution_group"):
        assert key in added, (
            f"apply_delta dropped {key!r}: it builds the new VP from a fixed "
            f"whitelist, so the delta's {key!r} lands nowhere and the VP "
            f"comes out unrunnable. 落地键: {sorted(added)}"
        )
        assert added[key] == item[key], (
            f"{key!r} was carried but rewritten: {added[key]!r} != {item[key]!r}"
        )

    issues = validate_vp(added)
    assert issues == [], (
        "an api_test VP added through the delta must survive schema "
        "validation, otherwise _maybe_evaluate_plan_delta's guardrail drops "
        f"the whole VP and 'obsolete + add' is a dead end: "
        f"{[i.detail for i in issues]}"
    )


def test_added_vp_without_the_optional_execution_fields_stays_clean() -> None:
    """透传是**有条件**的：delta 没写的键不能凭空长出来。

    白名单扩了不等于要给每条新增 VP 塞五个键 —— 一条没有 ``request`` 的
    ``code_review`` VP 不应该因为框架改了就被塞进 ``timeout_seconds: None``。
    """
    from verification_plan_delta import apply_delta, parse_delta_payload

    plan = _plan(_vp("VP-001"))
    apply_delta(plan, parse_delta_payload({
        "add": [{
            "title": "代码审查",
            "reason": "本轮新增了批处理路径",
            "verification_method": "code_review",
        }],
        "obsolete": [],
    }, plan), 5)

    added = plan["verification_points"][-1]
    for key in ("request", "assertions", "target_url", "execution_group"):
        assert key not in added, (
            f"{key!r} appeared on a VP whose delta never mentioned it"
        )


# ---------------------------------------------------------------------------
# 增量提示词的输出契约（2026-10-24）
# ---------------------------------------------------------------------------
#
# 2026-10-14（VP-009 退休）修了一半：``ADD_THROUGH_FIELDS`` 让 ``request`` /
# ``assertions`` 能原样搬进新 VP，**提示词却仍然只要 title / expected_result /
# reason** —— 增量 LLM 因此从不产出判据载荷，新 VP 缺 ``request``，被
# ``verification_api_runner.validate_vp`` 判为不可运行，又被
# ``verification_agent._maybe_evaluate_plan_delta`` 的护栏整条丢弃。
#
# 后果不是「少一条 VP」，而是「作废一条写错的 VP + 新增一条表达正确的」这条
# sanctioned 路径在框架层根本不存在：旧判据继续红，而唯一合法的修法（改判据）
# 被硬规则禁止。下面这几条钉的是提示词契约。


def test_delta_prompt_demands_the_api_test_payload() -> None:
    """增量提示词必须要求 api_test 带 ``request`` + ``assertions``。

    少了输出契约里的这两项，增量 LLM 只会写标题和预期结果 —— 落地时判据载荷
    消失，那条新 VP 永远跑不出结论。
    """
    from verification_api_runner import SUBJECT_KEYS
    from verification_plan_delta import DELTA_SYSTEM_PROMPT

    assert "request" in DELTA_SYSTEM_PROMPT, (
        "the delta prompt must ask for the request object of an api_test VP"
    )
    assert "assertions" in DELTA_SYSTEM_PROMPT, (
        "the delta prompt must ask for the assertions of an api_test VP"
    )
    for subject in SUBJECT_KEYS:
        assert subject in DELTA_SYSTEM_PROMPT, (
            f"the delta prompt must teach the assertion subject {subject!r} — "
            "an api_test VP naming an unknown subject is dropped by the "
            "schema guard"
        )


def test_delta_prompt_names_the_mandatory_guard_header() -> None:
    """打 ``/api/*`` 的 api_test 必须显式带上仓库强制的请求头。

    ``backend/request_guard.py`` 对 ``/api/*`` 强制要求 ``X-PDT-Request: 1``：
    不带就 403，响应体是错误信封而不是业务数据 —— 那条验证点的断言因此永远
    无法满足。提示词不教这件事，增量 LLM 就会再造一条同样漏头的验证点
    （本轮 VP-004 就是这么来的）。
    """
    from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
    from verification_plan_delta import DELTA_SYSTEM_PROMPT

    assert f"{REQUEST_HEADER}: {REQUEST_HEADER_VALUE}" in DELTA_SYSTEM_PROMPT, (
        f"the delta prompt must name the mandatory guard header "
        f"{REQUEST_HEADER!r}; without it the LLM writes api_test VPs whose "
        "assertions can never hold"
    )


def test_delta_prompt_offers_obsolete_plus_add_instead_of_editing() -> None:
    """已有 VP 写错了时的 sanctioned 路径必须写在提示词里：作废 + 新增。

    框架硬规则是「绝不修改已有验证点」（改了它此前挣到的结论就因出处指纹
    失效而作废，整轮重跑）。于是「一条验证点的请求规格写错了」只剩一条出路：
    ``obsolete`` 掉它 + ``add`` 一条表达正确的。提示词不教这条路，增量 LLM
    只会照着「不要改已有 VP」硬编，最后什么都不做 —— 旧判据继续红。
    """
    from verification_plan_delta import DELTA_SYSTEM_PROMPT

    assert "作废" in DELTA_SYSTEM_PROMPT, (
        "the delta prompt must teach the sanctioned replacement path: "
        "obsolete the broken verification point + add a correct one"
    )
    assert "obsolete" in DELTA_SYSTEM_PROMPT
    assert "assertions" in DELTA_SYSTEM_PROMPT, (
        "the replacement path is only usable if the added VP can carry its "
        "own payload"
    )


def test_build_delta_prompt_gives_the_whole_new_task_description() -> None:
    """新增任务的描述必须给全 —— 截断到 200 字会把判据依据切掉。

    描述是「新增必须有依据」的依据来源：被截断的描述让增量 LLM 猜一个请求
    规格出来，而不是照着任务真正要求的接口写。
    """
    from verification_plan_delta import build_delta_prompt

    description = "任务要求服务在 spawn 后就绪，并暴露 /health 与 /api/plans。" * 12
    assert len(description) > 200

    prompt = build_delta_prompt(
        _plan(),
        [{"id": "repair-r1-01", "title": "修复任务", "description": description}],
        [],
        round_number=2,
    )

    assert description in prompt, (
        "the new task's description was truncated — the LLM cannot write a "
        "correct request spec from a half description"
    )


def test_build_delta_prompt_shows_the_request_spec_of_existing_vps() -> None:
    """清单里必须看得见已有 VP 的请求规格。

    增量 LLM 要判断「这条已有 VP 的请求规格是不是漏了必需请求头」，前提是
    它看得见那条请求长什么样。原来的清单只给 id / method / title / 依据，
    请求头根本没进提示词 —— 漏头这件事因此**不可见**，也就无从作废。
    """
    from verification_plan_delta import build_delta_prompt

    broken = _vp(
        "VP-004",
        verification_method="api_test",
        request={
            "method": "GET",
            "url": "{{svc.pdt.url}}/api/plans",
            "headers": {"Accept": "application/json"},
        },
    )

    prompt = build_delta_prompt(_plan(broken), [], [], round_number=2)

    assert "{{svc.pdt.url}}/api/plans" in prompt, (
        "the existing VP's request spec must reach the prompt — otherwise a "
        "missing mandatory header is invisible to the delta LLM"
    )
    assert "Accept" in prompt


def test_build_delta_prompt_states_the_obsolete_plus_add_rule() -> None:
    """用户提示词也要把「作废 + 新增」和必需请求头说清楚。"""
    from verification_plan_delta import build_delta_prompt

    prompt = build_delta_prompt(_plan(), [], [], round_number=2)

    assert "作废" in prompt
    assert "X-PDT-Request" in prompt, (
        "the prompt must name the mandatory header so the LLM can spot the "
        "verification points whose request spec is missing it"
    )


def test_obsolete_plus_add_replaces_a_broken_api_test_verification_point() -> None:
    """sanctioned 路径的端到端形状：作废写错的那条 + 新增一条可运行的。

    这是「作废 + 新增」这条路的验收形状：

    * 旧 VP **一个字节都没被改**（只多了 obsolete 标记）——「绝不修改已有
      验证点」的硬规则仍然成立；
    * 新 VP 带上了必需请求头，且经 ``validate_vp`` 零 issue —— 否则护栏会
      把它整条丢弃，「作废 + 新增」就白做了。
    """
    from verification_api_runner import validate_vp
    from verification_plan_delta import apply_delta, parse_delta_payload

    broken_request = {
        "method": "GET",
        "url": "{{svc.pdt.url}}/api/plans",
        "headers": {"Accept": "application/json"},
        "body": None,
        "timeout_seconds": 30,
    }
    broken_assertions = [{"name": "列表接口返回 200", "status": 200}]
    plan = _plan(_vp(
        "VP-004",
        verification_method="api_test",
        request=broken_request,
        assertions=broken_assertions,
    ))

    delta = parse_delta_payload({
        "obsolete": [{
            "id": "VP-004",
            "reason": "请求规格漏了必需请求头，断言永远无法满足",
        }],
        "add": [{
            "title": "API 契约：/api/plans 返回 JSON 数组（带跨源守卫头）",
            "related_prd_criteria": "集成验证：计划列表 API 可用",
            "verification_method": "api_test",
            "verification_phase": 1,
            "priority": "medium",
            "expected_result": "200 + JSON 数组",
            "reason": "接替请求规格写错的 VP-004",
            "request": {
                "method": "GET",
                "url": "{{svc.pdt.url}}/api/plans",
                "headers": {
                    "Accept": "application/json",
                    "X-PDT-Request": "1",
                },
                "body": None,
                "timeout_seconds": 30,
            },
            "assertions": [
                {"name": "列表接口返回 200", "status": 200},
                {"name": "响应体是数组", "body_contains": "["},
            ],
            "execution_group": 0,
        }],
    }, plan)

    summary = apply_delta(plan, delta, round_number=2)

    assert summary["obsoleted"] and summary["obsoleted"][0]["id"] == "VP-004"
    assert summary["added"] and summary["added"][0]["id"] == "VP-005"

    old = plan["verification_points"][0]
    assert old["obsolete"] is True
    assert old["request"] == broken_request, (
        "the retired VP's request spec must stay byte-identical — the "
        "framework never edits an existing verification point"
    )
    assert old["assertions"] == broken_assertions

    added = plan["verification_points"][-1]
    assert added["request"]["headers"]["X-PDT-Request"] == "1"
    assert validate_vp(added) == [], (
        "the added api_test VP must survive schema validation, otherwise the "
        "guardrail drops it and 'obsolete + add' stays a dead end"
    )
