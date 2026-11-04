"""纯后端改动不该生成 ``ui_validation`` 验证点（缺陷 B 第 2 条）。

为什么要有这组测试
------------------
任务 8 把 ``tasks.json`` 里全部任务的 ``files_to_modify`` 并集解析成了改动面，
任务 9 把它注进了 Phase 1 的提示词。但**提示词里写了改动面 ≠ 生成器就一定
照做** —— 规划 LLM 仍然可能为一次纯后端改动生成 ``ui_validation``：它要起
浏览器、要占机器，而这次改动连界面都没碰。

所以这里加一道**确定性裁剪**：计划生成后按改动面把用不上的 ``ui_validation``
验证点裁掉。关键在于**不能静默** —— 裁掉的同一次写一条 ``ui_validation_skipped``
（原因 + 被检查的文件 + 被裁掉的验证点 + 一句人话），随计划落盘。一份"全绿"的
报告里不该藏着"这轮少验了一件事"。

四条边界
--------
* **改动面没有界面** → ``ui_validation`` 验证点被裁掉，说明里写清原因与被检查
  的文件；
* **改动面触及界面** → 一条都不删（也没有说明 —— 没有裁剪就没有说明）；
* **其它方法不受影响** → ``api_test`` / ``code_review`` 一条都不动；
* **说明是可读的、具体的** → 说明里能看到被检查的文件名。

另有一条集成断言：说明随计划文件落盘（``verification_plan.json``），以及一条
"改动面未知就不裁"的断言 —— **"没看见界面文件"不等于"确定没有界面"**。

"改动面未知"这一条为什么要单独钉
--------------------------------
``load_change_surface`` 在读不到 / 解析不了 ``tasks.json`` 时返回**空改动面**
而不是错误（改动面是辅助输入，不是门禁）。空改动面里 ``has_ui`` 也是
``False`` —— 如果按 ``has_ui`` 一刀切，空改动面会把 UI 验证静默关掉，而
"我们没看过任何文件"根本推不出"这次改动没有界面"。所以空改动面**不裁**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

pytestmark = pytest.mark.unit

#: 计划顶层记录"这一轮的 ui_validation 被裁掉了"的键。
SKIPPED_FIELD = "ui_validation_skipped"


def _ui_vp(vp_id: str = "VP-001") -> dict:
    """一条最小可用的 ``ui_validation`` 验证点。

    ``target_url`` 用一个**没有声明**的端口：服务引用护栏只对"声明过的服务"
    报错（未声明的端口是合法的外部端点），这样这条用例测的是裁剪策略本身，
    而不是服务引用护栏。
    """
    return {
        "id": vp_id,
        "title": "界面可用",
        "verification_method": "ui_validation",
        "priority": "medium",
        "expected_result": "界面正常显示",
        "target_url": "http://127.0.0.1:8123/",
    }


def _backend_surface():
    """纯后端改动面：只碰了 ``backend/cli.py``。"""
    from change_surface import ChangeSurface

    return ChangeSurface(
        files=("backend/cli.py",), kinds=frozenset({"cli"}), has_ui=False,
    )


def _frontend_surface():
    """触及界面的改动面。"""
    from change_surface import ChangeSurface

    return ChangeSurface(
        files=("frontend/app.js",), kinds=frozenset({"frontend"}), has_ui=True,
    )


# ---------------------------------------------------------------------------
# 纯后端改动 → ui_validation 被裁掉，并留下说明
# ---------------------------------------------------------------------------


def test_ui_vp_dropped_when_surface_has_no_ui():
    """改动面没有界面 → ui_validation 验证点被裁掉，说明写清原因与文件数。"""
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [_ui_vp()]}
    surface = _backend_surface()

    result = _apply_ui_validation_policy(plan, surface)

    assert result["verification_points"] == []
    note = result[SKIPPED_FIELD]
    assert note["reason"] == "no_ui_surface"
    assert note["files_checked"] == 1
    assert note["method"] == "ui_validation"


def test_dropped_vp_ids_are_recorded():
    """被裁掉的是哪几条验证点，必须写进说明（不能只写"裁了一些"）。"""
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [_ui_vp("VP-001"), _ui_vp("VP-007")]}

    result = _apply_ui_validation_policy(plan, _backend_surface())

    note = result[SKIPPED_FIELD]
    assert note["dropped"] == ["VP-001", "VP-007"]


def test_plan_without_ui_vp_gets_no_note():
    """没有 ui_validation 可裁 → 不写说明（没有裁剪就没有说明）。"""
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [
        {"id": "VP-001", "verification_method": "api_test"},
    ]}

    result = _apply_ui_validation_policy(plan, _backend_surface())

    assert SKIPPED_FIELD not in result
    assert len(result["verification_points"]) == 1


# ---------------------------------------------------------------------------
# 触及界面 → 一条都不删
# ---------------------------------------------------------------------------


def test_ui_vp_kept_when_frontend_touched():
    """改动面含 frontend 文件 → ui_validation 验证点保留，也不写说明。"""
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [_ui_vp()]}

    result = _apply_ui_validation_policy(plan, _frontend_surface())

    assert len(result["verification_points"]) == 1
    assert result["verification_points"][0]["id"] == "VP-001"
    assert SKIPPED_FIELD not in result


# ---------------------------------------------------------------------------
# 其它方法不受影响
# ---------------------------------------------------------------------------


def test_non_ui_methods_untouched():
    """api_test / code_review 一条都不动，也不进说明。"""
    from verification_agent import _apply_ui_validation_policy

    api_vp = {
        "id": "VP-010",
        "title": "接口契约",
        "verification_method": "api_test",
        "priority": "high",
        "expected_result": "返回 200",
    }
    review_vp = {
        "id": "VP-011",
        "title": "代码评审",
        "verification_method": "code_review",
        "priority": "medium",
        "expected_result": "无阻塞意见",
    }
    plan = {"verification_points": [api_vp, review_vp, _ui_vp()]}

    result = _apply_ui_validation_policy(plan, _backend_surface())

    assert [vp["id"] for vp in result["verification_points"]] == ["VP-010", "VP-011"]
    assert result["verification_points"][0] == api_vp
    assert result["verification_points"][1] == review_vp
    assert result[SKIPPED_FIELD]["dropped"] == ["VP-001"]


# ---------------------------------------------------------------------------
# 说明是可读的、具体的
# ---------------------------------------------------------------------------


def test_note_is_readable_and_specific():
    """说明里能看到被检查的文件名（不是"检查了 1 个文件"这种没法追的写法）。"""
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [_ui_vp()]}

    result = _apply_ui_validation_policy(plan, _backend_surface())
    note = result[SKIPPED_FIELD]

    assert "backend/cli.py" in note["detail"]
    assert "backend/cli.py" in json.dumps(note, ensure_ascii=False)
    assert "ui_validation" in note["detail"]
    assert note["files"] == ["backend/cli.py"]


def test_note_lists_every_checked_file():
    """检查了多个文件 → 说明里逐个列出，不截断。"""
    from change_surface import ChangeSurface
    from verification_agent import _apply_ui_validation_policy

    surface = ChangeSurface(
        files=("backend/a.py", "backend/b.py", "scripts/run.sh"),
        kinds=frozenset({"backend", "cli"}),
        has_ui=False,
    )
    plan = {"verification_points": [_ui_vp()]}

    note = _apply_ui_validation_policy(plan, surface)[SKIPPED_FIELD]

    assert note["files_checked"] == 3
    for path in ("backend/a.py", "backend/b.py", "scripts/run.sh"):
        assert path in note["detail"]


# ---------------------------------------------------------------------------
# 改动面未知 → 不裁（"没看见"不等于"没有"）
# ---------------------------------------------------------------------------


def test_unknown_surface_keeps_ui_vp():
    """空改动面（读不到 tasks.json）→ 不裁：没有证据就没有裁剪。"""
    from change_surface import ChangeSurface
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [_ui_vp()]}

    result = _apply_ui_validation_policy(plan, ChangeSurface())

    assert len(result["verification_points"]) == 1
    assert SKIPPED_FIELD not in result


def test_missing_surface_keeps_ui_vp():
    """改动面是 ``None`` → 同样不裁。"""
    from verification_agent import _apply_ui_validation_policy

    plan = {"verification_points": [_ui_vp()]}

    result = _apply_ui_validation_policy(plan, None)

    assert len(result["verification_points"]) == 1
    assert SKIPPED_FIELD not in result


# ---------------------------------------------------------------------------
# 接进生成回路：说明随计划落盘
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """只回一份固定计划的假 coding tool。"""

    def __init__(self, plan: dict):
        self._plan = plan

    def query_json(self, prompt, system_instruction, timeout=None):
        return self._plan


def _make_agent(tmp_path: Path, plan: dict, files_to_modify=None):
    """造一个 agent，并按需写一份 ``tasks.json``（改动面的来源）。"""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    if files_to_modify is not None:
        (plan_dir / "tasks.json").write_text(
            json.dumps({
                "tasks": [{"id": "1", "files_to_modify": list(files_to_modify)}],
            }),
            encoding="utf-8",
        )
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir,
        coding_tool=_FakeCodingTool(plan),
    )
    return agent


def test_generated_plan_drops_ui_vp_for_backend_only_change(tmp_path):
    """生成回路：纯后端改动 → 计划里没有 ui_validation 验证点，且有说明。"""
    agent = _make_agent(
        tmp_path,
        {"verification_points": [_ui_vp()]},
        files_to_modify=["backend/cli.py"],
    )

    plan = agent.generate_verification_plan(retry_llm=1)

    assert plan["verification_points"] == []
    assert plan[SKIPPED_FIELD]["reason"] == "no_ui_surface"
    assert plan[SKIPPED_FIELD]["files_checked"] == 1


def test_skipped_note_is_persisted_to_the_plan_file(tmp_path):
    """说明随计划落盘 —— 操作者能看见这一轮少了什么。"""
    agent = _make_agent(
        tmp_path,
        {"verification_points": [_ui_vp()]},
        files_to_modify=["backend/cli.py"],
    )

    agent.generate_verification_plan(retry_llm=1)

    on_disk = json.loads(agent.verification_plan_file.read_text(encoding="utf-8"))
    assert SKIPPED_FIELD in on_disk
    assert on_disk[SKIPPED_FIELD]["reason"] == "no_ui_surface"
    assert "backend/cli.py" in on_disk[SKIPPED_FIELD]["detail"]


def test_generated_plan_keeps_ui_vp_when_frontend_touched(tmp_path):
    """生成回路：改动面含 frontend 文件 → ui_validation 验证点留下。"""
    from verification_agent import _apply_ui_validation_policy

    agent = _make_agent(
        tmp_path,
        {"verification_points": [_ui_vp()]},
        files_to_modify=["frontend/app.js"],
    )

    plan = agent.generate_verification_plan(retry_llm=1)

    assert [vp["id"] for vp in plan["verification_points"]] == ["VP-001"]
    assert SKIPPED_FIELD not in plan
    # 再问策略本身一次：触及界面 → 一条都不删（这条断言证明策略**存在**
    # 且确实认得"触及界面"，而不只是"什么都没做"）。
    kept = _apply_ui_validation_policy(plan, _frontend_surface())
    assert [vp["id"] for vp in kept["verification_points"]] == ["VP-001"]
    assert SKIPPED_FIELD not in kept
