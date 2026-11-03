"""VP 子 agent 的可写范围护栏 —— 只放行 plan 目录与临时目录。

背景（2026-11-02）
------------------
VP-055 的子 agent 直接改写了**被验证仓库**的 ``backend/verification_agent.py``。

现有 9 条 PreToolUse 钩子（``verification_subagent._HOOK_RULES``）里唯一管
写入路径的是 ``exec_path_block``：它把 ``PDT_PROJECT_DIR`` 整个目录算作
合法写入范围。对**执行**子 agent 来说那是它的家；对**验证**子 agent 来说，
被验证仓库恰恰是它只读的对象。于是"验证"这件事可以被"验证"自己改掉 ——
守卫在、守卫放行、结果不作数。

这条护栏换一个方向：不问"能不能写进 project_dir"，只问"是不是写进了 plan
目录或临时目录"。写别处 —— 包括被验证仓库的源码 —— 一律 exit 2。

四条边界
--------
* ``PDT_VP_WRITE_SCOPE`` 没设 → exit 0。旧调用方（执行子 agent、refiner、
  各类自己写 settings.json 的地方）完全不受影响。
* plan 目录内 / ``/tmp`` / ``/private/tmp`` / ``tempfile.gettempdir()`` → exit 0。
* payload 里没有 ``file_path`` → exit 0（没有写入目标就谈不上越界）。
* 既不在 plan 也不在 tmp → exit 2，reason ``outside_scope``，并把拒绝证据
  追加成 ``<plan>/vp_write_scope/refusals/round-<n>/<vp_id>.jsonl``。

拒绝证据写失败**不改变**退出码：写不进去只是"这次没留下痕"，不是"这次
放行"。所以仍然 exit 2，写不进去的原因记到 stderr。

这些断言走的是**黑盒**：钩子的契约就是"退出码 + stderr"，所以唯一的诚实
验法是让进程真的产出它（``test_allowed_write_files_hook.py`` 同一个道理）。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_HOOK_SCRIPT = _BACKEND_DIR / "verification_write_guard.py"

#: 被验证仓库自己的一个源文件。写它正是本护栏必须拒绝的事：验证子 agent
#: 在验证这棵树，不是在这棵树里开发。
_REPO_FILE = _BACKEND_DIR / "agent.py"


def _guard():
    """Return the guard module.

    在测试体里 import，而不是在模块顶层：模块还不存在时，缺的是**这一条
    断言**要用的东西，报成 FAILED（traceback 里带着 ImportError）比报成
    collection error 诚实 —— 后者会让整个文件看起来像"没跑"。
    """
    import verification_write_guard

    return verification_write_guard


def _hook_env(**overrides):
    """Build the environment for one hook invocation.

    继承的环境照抄（``TMPDIR`` 必须跟着走：护栏在**它自己的进程**里问
    ``tempfile.gettempdir()``，父子两个进程得对"临时目录在哪"达成一致），
    然后先把三个 scope 变量清掉 —— 免得外层残留的值替某条断言做决定。
    """
    env = os.environ.copy()
    for name in ("PDT_VP_WRITE_SCOPE", "PDT_VP_ID", "PDT_VP_ROUND"):
        env.pop(name, None)
    for key, value in overrides.items():
        env[key] = str(value)
    return env


def _run_hook(payload, env):
    """Run the guard as a black box: JSON payload on stdin, exit code out."""
    # python 自己也会用 exit 2 报"文件不存在" —— 不先把这件事挑明，
    # "护栏拒了"和"脚本压根没跑起来"会穿着同一件退出码的衣服。
    assert _HOOK_SCRIPT.is_file(), f"护栏脚本不存在：{_HOOK_SCRIPT}"
    proc = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.returncode, proc.stderr


def _plan_dir(tmp_path):
    """A plan directory standing in for the plan the VP is verifying."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    return plan_dir


def _marker(plan_dir, round_no=3, vp_id="VP-055"):
    """The refusal marker path the guard is contracted to write."""
    return (
        plan_dir
        / "vp_write_scope"
        / "refusals"
        / f"round-{round_no}"
        / f"{vp_id}.jsonl"
    )


# ---------------------------------------------------------------------------
# 越界写入：拒绝，并留下证据
# ---------------------------------------------------------------------------
def test_project_dir_write_is_refused(tmp_path):
    """写被验证仓库的源码 → exit 2，且拒绝证据落到 round-3/VP-055.jsonl。"""
    plan_dir = _plan_dir(tmp_path)
    code, stderr = _run_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(_REPO_FILE)},
        },
        _hook_env(
            PDT_VP_WRITE_SCOPE=plan_dir,
            PDT_VP_ID="VP-055",
            PDT_VP_ROUND=3,
        ),
    )

    assert code == 2, stderr
    assert _marker(plan_dir).is_file(), (
        "拒绝必须留下证据：没有 marker 的话，这次越界写就事后无人查证。"
    )
    records = _guard().read_refusals(plan_dir, 3)
    assert len(records) == 1
    assert records[0]["path"] == str(_REPO_FILE)
    assert records[0]["reason"] == "outside_scope"
    assert records[0]["tool"] == "Edit"


# ---------------------------------------------------------------------------
# 合法写入：放行，且不留证据
# ---------------------------------------------------------------------------
def test_plan_dir_write_is_allowed(tmp_path):
    """写 plan 目录 → exit 0，且没有 marker（放行不该留拒绝痕迹）。"""
    plan_dir = _plan_dir(tmp_path)
    code, stderr = _run_hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(plan_dir / "notes.md")},
        },
        _hook_env(
            PDT_VP_WRITE_SCOPE=plan_dir,
            PDT_VP_ID="VP-055",
            PDT_VP_ROUND=3,
        ),
    )

    assert code == 0, stderr
    assert not (plan_dir / "vp_write_scope").exists()


def test_tmp_write_is_allowed(tmp_path):
    """写临时目录 → exit 0。"""
    plan_dir = _plan_dir(tmp_path)
    code, stderr = _run_hook(
        {
            "tool_name": "Write",
            # nosec B108 — 硬写的 /tmp 就是这里要的东西：临时目录必须放行
            "tool_input": {"file_path": "/tmp/x.sh"},  # nosec B108
        },
        _hook_env(
            PDT_VP_WRITE_SCOPE=plan_dir,
            PDT_VP_ID="VP-055",
            PDT_VP_ROUND=3,
        ),
    )

    assert code == 0, stderr
    assert not (plan_dir / "vp_write_scope").exists()


def test_scope_off_is_noop(tmp_path):
    """没设 ``PDT_VP_WRITE_SCOPE`` → exit 0（旧调用方不受影响）。"""
    plan_dir = _plan_dir(tmp_path)
    code, stderr = _run_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(_REPO_FILE)},
        },
        _hook_env(),
    )

    assert code == 0, stderr
    assert not (plan_dir / "vp_write_scope").exists()


# ---------------------------------------------------------------------------
# 拒绝证据读回
# ---------------------------------------------------------------------------
def test_read_refusals_parses_marker(tmp_path):
    """``read_refusals`` 把 marker 读回成结构化记录，并按 VP / round 过滤。"""
    plan_dir = _plan_dir(tmp_path)
    code, stderr = _run_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(_REPO_FILE)},
        },
        _hook_env(
            PDT_VP_WRITE_SCOPE=plan_dir,
            PDT_VP_ID="VP-055",
            PDT_VP_ROUND=3,
        ),
    )
    assert code == 2, stderr

    guard = _guard()
    records = guard.read_refusals(plan_dir, 3)
    assert len(records) == 1
    assert records[0]["vp_id"] == "VP-055"
    assert records[0]["round"] == 3
    assert records[0]["tool"] == "Edit"
    assert records[0]["reason"] == "outside_scope"
    assert records[0]["ts"]

    # 只读一个 VP 的记录；别人的记录读不到。
    assert guard.read_refusals(plan_dir, 3, vp_id="VP-055") == records
    assert guard.read_refusals(plan_dir, 3, vp_id="VP-999") == []
    # 别的 round 没有记录。
    assert guard.read_refusals(plan_dir, 2) == []


# ---------------------------------------------------------------------------
# 其余边界
# ---------------------------------------------------------------------------
def test_missing_file_path_is_allowed(tmp_path):
    """payload 里没有 ``file_path`` → exit 0（没有写入目标就谈不上越界）。"""
    plan_dir = _plan_dir(tmp_path)
    code, stderr = _run_hook(
        {"tool_name": "Edit", "tool_input": {}},
        _hook_env(
            PDT_VP_WRITE_SCOPE=plan_dir,
            PDT_VP_ID="VP-055",
            PDT_VP_ROUND=3,
        ),
    )

    assert code == 0, stderr


def test_marker_write_failure_still_refuses(tmp_path):
    """证据写不进去 → 仍然 exit 2（写不进去不是放行的理由）。"""
    plan_dir = _plan_dir(tmp_path)
    # 让 marker 目录建不出来：<plan>/vp_write_scope 已经被一个普通文件占了。
    (plan_dir / "vp_write_scope").write_text("not a directory", encoding="utf-8")

    code, stderr = _run_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(_REPO_FILE)},
        },
        _hook_env(
            PDT_VP_WRITE_SCOPE=plan_dir,
            PDT_VP_ID="VP-055",
            PDT_VP_ROUND=3,
        ),
    )

    assert code == 2
    assert "refusal" in stderr, (
        "证据没写成要说明原因：stderr 必须讲清这一次拒绝没能落盘。"
    )


def test_main_in_process_refuses_and_records(tmp_path, monkeypatch):
    """``main`` 本身：exit 2 + 落证据（进程内调用，钩子的契约入口）。"""
    plan_dir = _plan_dir(tmp_path)
    guard = _guard()
    monkeypatch.setenv("PDT_VP_WRITE_SCOPE", str(plan_dir))
    monkeypatch.setenv("PDT_VP_ID", "VP-055")
    monkeypatch.setenv("PDT_VP_ROUND", "3")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(_REPO_FILE)},
                }
            )
        ),
    )

    assert guard.main([]) == 2
    assert len(guard.read_refusals(plan_dir, 3)) == 1


def test_main_in_process_allows_plan_write(tmp_path, monkeypatch):
    """``main`` 对 plan 目录内的写入放行，且不留证据。"""
    plan_dir = _plan_dir(tmp_path)
    guard = _guard()
    monkeypatch.setenv("PDT_VP_WRITE_SCOPE", str(plan_dir))
    monkeypatch.setenv("PDT_VP_ID", "VP-055")
    monkeypatch.setenv("PDT_VP_ROUND", "3")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(plan_dir / "notes.md")},
                }
            )
        ),
    )

    assert guard.main([]) == 0
    assert guard.read_refusals(plan_dir, 3) == []