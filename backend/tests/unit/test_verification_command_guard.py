"""``verification_command_guard`` —— VP 命令的有界性护栏。

2026-09-15：VP 没有必要去跑整个 nightly CI —— 太久了；把门禁的 CI
跑过就可以。

这些测试锁两件事：

1. **真阳性** —— 无界命令必须被打中。用例直接取自a production plan 的真实
   ``test_command``（VP-023 的 nightly 全量、VP-007 的
   ``cargo test --workspace``），它们是这条规则存在的理由。
2. **零假阳性** —— 合法的有界命令一条都不能误判。这条同样是硬约束：
   护栏的初版要求"每条测试命令都要有显式时间上限"，实测把 30 个 VP 里的
   **23 个合法单测命令**全判违规；宁可错杀的检查会让 LLM 为了过检查空转，
   比不做更糟。
"""

from __future__ import annotations

import pytest

from verification_command_guard import (
    GENERATION_RULES,
    Violation,
    annotate_plan,
    find_violations,
)


# ---------------------------------------------------------------------------
# 真阳性：无界命令
# ---------------------------------------------------------------------------


#: (用例名, 命令, 期望命中的违规 code)
UNBOUNDED_CASES = [
    (
        "VP-023 —— docker compose 起全家桶再跑全量（真实命令）",
        'bash -c "export DEVELOPER_DIR=/Library/Developer/CommandLineTools '
        '&& source venv1/bin/activate && docker compose up -d && pytest tests/ -v; '
        'rc=$?; echo PYTEST_EXIT=$rc; exit $rc"',
        "unbounded_pytest",
    ),
    (
        "VP-007 —— cargo test --workspace（真实命令）",
        'bash -c "source venv1/bin/activate && cd native_ext && '
        'cargo test --workspace -- --nocapture"',
        "unbounded_cargo",
    ),
    ("裸 pytest", "pytest", "unbounded_pytest"),
    ("tests/ 无任何收窄", "pytest tests/ -v", "unbounded_pytest"),
    (
        "一次点一堆目录 == 整棵树",
        'bash -c "cd proj && pytest tests/unit tests/integration"',
        "unbounded_pytest",
    ),
    ("裸 cargo test", "cd native_ext && cargo test", "unbounded_cargo"),
    (
        "nightly marker",
        'source venv1/bin/activate && pytest tests/ -m nightly',
        "nightly_marker",
    ),
    (
        "nightly workflow 命令",
        "bash -c \"gh workflow run nightly.yml --repo foo/bar\"",
        "nightly_marker",
    ),
    # 2026-09-17: 有界 ≠ 可判。管道结尾把退出码吞掉是另一个维度的问题，
    # 与"范围是否有界"无关，同样必须报出来。
    (
        "纯 shell 审计但退出码被 head 吞掉",
        'grep -rn "foo" backend/ | head -20',
        "exit_code_swallowed_by_pipe",
    ),
]


@pytest.mark.parametrize("name,cmd,code", UNBOUNDED_CASES, ids=[c[0] for c in UNBOUNDED_CASES])
def test_unbounded_commands_are_flagged(name: str, cmd: str, code: str) -> None:
    codes = [v.code for v in find_violations(cmd)]
    assert code in codes, f"{name}: expected {code}, got {codes}"


# ---------------------------------------------------------------------------
# 零假阳性：有界命令
# ---------------------------------------------------------------------------


BOUNDED_CASES = [
    (
        "VP-013 修复后 —— --test <集成测试文件> + 过滤器（真实命令）",
        'bash -c "source venv1/bin/activate && cd native_ext && '
        'cargo test --test signal_classification test_no_cross_branch_fallback '
        '-- --nocapture 2>&1; echo EXIT_CODE=$?"',
    ),
    (
        "点名单个测试文件",
        "source venv1/bin/activate && pytest tests/api/test_foo.py -v",
    ),
    (
        "显式节点 ID",
        "source venv1/bin/activate && pytest tests/api/test_foo.py::test_bar -v",
    ),
    (
        "-k 表达式收窄整棵树",
        'source venv1/bin/activate && pytest tests/ -k "test_a or test_b"',
    ),
    (
        "pytest 子目录后面还跟了别的命令（分段判定的回归锁）",
        'bash -c "cargo test --lib foo && pytest tests/serialization/ -v '
        '&& npx playwright test tests/e2e/x.spec.ts"',
    ),
    (
        "cargo --lib <过滤器>",
        "cd native_ext && cargo test --lib signal_serialization_contract -- --nocapture",
    ),
    (
        "cargo --test=<文件>",
        "cd native_ext && cargo test --test=dual_track_integration",
    ),
    (
        "纯 shell 审计没有测试命令（退出码可判形态）",
        'grep -rn "foo" backend/ > /tmp/audit.log 2>&1; rc=$?; '
        'tail -n 20 /tmp/audit.log; exit $rc',
    ),
    (
        "--ignore + -m 双重收窄",
        'source venv1/bin/activate && pytest tests/unit '
        '--ignore=tests/unit/test_x.py -m "not integration"',
    ),
    (
        "npm 测试（本模块不管 npm 的有界性）",
        "cd frontend && npx jest src/__tests__/tooltip.test.ts",
    ),
    ("空命令", ""),
    ("None 命令", None),
]


@pytest.mark.parametrize("name,cmd", BOUNDED_CASES, ids=[c[0] for c in BOUNDED_CASES])
def test_bounded_commands_are_not_flagged(name: str, cmd) -> None:
    violations = find_violations(cmd)
    assert violations == [], (
        f"{name}: false positive — {[v.code for v in violations]}"
    )


def test_whole_cmd_nightly_scan_sees_every_segment() -> None:
    """``nightly`` 只在整条命令上扫一次（任何位置出现都算）。"""
    cmd = "echo start && pytest tests/x.py -v && gh workflow run nightly.yml"
    assert "nightly_marker" in [v.code for v in find_violations(cmd)]


def test_identical_violations_are_deduped_but_distinct_ones_survive() -> None:
    """去重按 ``(code, detail)``，**不是**按 ``code``。

    按 code 去重会把"两条命令各自点名了不同的不存在目标"压成一条，重生成的
    反馈里就只说得出一个 —— LLM 会改漏。完全相同的违规仍然只报一次。
    """
    identical = "pytest tests/ && pytest tests/"
    assert [
        v.code for v in find_violations(identical)
    ].count("unbounded_pytest") == 1

    distinct = "pytest tests/ && pytest tests/unit tests/api"
    codes = [v.code for v in find_violations(distinct)]
    assert codes.count("unbounded_pytest") == 2, (
        f"两条范围不同的调用都要报出来，不能按 code 压成一条：{codes}"
    )


# ---------------------------------------------------------------------------
# annotate_plan
# ---------------------------------------------------------------------------


def _plan(*commands):
    return {
        "verification_points": [
            {"id": f"VP-{i:03d}", "title": f"point {i}",
             "test_command": cmd}
            for i, cmd in enumerate(commands, start=1)
        ],
    }


def test_annotate_marks_only_the_unbounded_vp() -> None:
    plan = _plan(
        "pytest tests/api/test_foo.py -v",
        "pytest tests/ -v",
    )

    findings = annotate_plan(plan)

    assert [f["id"] for f in findings] == ["VP-002"]
    assert findings[0]["violations"] == ["unbounded_pytest"]
    assert plan["verification_points"][0].get("command_guard") is None
    guard = plan["verification_points"][1]["command_guard"]
    assert guard["violations"][0]["code"] == "unbounded_pytest"
    assert guard["violations"][0]["detail"], "detail 必须能给人看"


def test_annotate_does_not_rewrite_the_command() -> None:
    """护栏只报告，不改写。

    替 LLM 猜一条"正确"的命令是另一种越权 —— VP-013 的 ``--lib`` 就是被
    "善意改写"改写坏的，那次错误花了整整一轮 + 一次 51 分钟的修复执行才
    被系统自己发现。
    """
    original = "pytest tests/ -v"
    plan = _plan(original)

    annotate_plan(plan)

    assert plan["verification_points"][0]["test_command"] == original


def test_annotate_clears_stale_guard_on_rerun() -> None:
    """重跑护栏时，已经变合法的 VP 要摘掉旧的 ``command_guard``。"""
    plan = _plan("pytest tests/ -v")
    annotate_plan(plan)
    assert "command_guard" in plan["verification_points"][0]

    plan["verification_points"][0]["test_command"] = "pytest tests/api/test_foo.py"
    findings = annotate_plan(plan)

    assert findings == []
    assert "command_guard" not in plan["verification_points"][0]


def test_annotate_tolerates_a_malformed_plan() -> None:
    assert annotate_plan({}) == []
    assert annotate_plan({"verification_points": "not-a-list"}) == []
    assert annotate_plan({"verification_points": [None, 42]}) == []


# ---------------------------------------------------------------------------
# 提示词与检测共用同一份规则（防漂移）
# ---------------------------------------------------------------------------


def test_prompt_rules_are_not_interpolated_into_the_plan_prompt() -> None:
    """2026-09-18：`GENERATION_RULES` **不再**进计划生成提示词。

    它管的是**验收命令**的有界性，而 VP 已经没有 `test_command` 了（判定
    依据是 method 自己的产物：api_test 的 request+assertions、code_review
    的 citations、ui_validation 的 checkpoints）。同一条规则仍然管**任务**的
    test_command —— `test_command_quality` 复用它，这也是常量本身保留的原因。

    这条断言是倒过来的：它锁住"别把已经不适用的规则再插回去"。提示词里
    描述一个不存在的字段，会让规划器继续产出跑不动的东西。
    """
    import prompts

    assert GENERATION_RULES not in prompts.VERIFICATION_PLAN_SYSTEM_PROMPT, (
        "GENERATION_RULES 又被插回 VERIFICATION_PLAN_SYSTEM_PROMPT 了 —— "
        "VP 没有 test_command，这些规则对 VP 不适用"
    )


def test_rules_frame_the_problem_as_scope_not_as_banned_suites() -> None:
    """措辞锁：问题不是"跑全量"，是"一个 VP 没有界定范围"。

    2026-09-15 用户纠正::

        "不是 Nightly CI 不应该被跑，理论上来说，所有的测试用例都应该被跑。
         但是直接跑 Nightly CI 是太不负责任的行为……正常情况下，我们应该
         根据任务的内容，指定对应的测试用例。"

    把手段当目的，规则就会读成"别跑测试"——那会让 LLM 为了过检查而砍掉
    本该系统覆盖的东西。覆盖面由多个有界 VP 共同构成。
    """
    assert "界定" in GENERATION_RULES, (
        "规则的落点应该是「必须界定到具体用例」"
    )
    assert "跑全量测试本身没有错" in GENERATION_RULES, (
        "规则必须明说「跑全量不是错，错的是拿它当一个 VP 的定义」，"
        "否则会被读成「禁止跑测试」"
    )
    assert "多个各自有界的 VP 共同构成" in GENERATION_RULES, (
        "必须说清覆盖面从哪来 —— 多个有界 VP,而不是单个宽范围 VP"
    )


def test_violation_is_comparable_for_log_assertions() -> None:
    v = Violation("unbounded_pytest", "because")
    assert v.code == "unbounded_pytest" and v != Violation("other", "because")
