"""TDD spec —— e2e 的两个子类：browser（子 agent 驱动）与 command（框架直跑）。

背景
----
``e2e`` 以前在定义上被绑死成「真开浏览器」：一条本地 CLI / 服务级的端到端
验证点也得起一个 LLM 子会话去驱动浏览器，判定落在子 agent 的自报上。

现在 ``e2e`` 分成两个子类：

* **browser**（``e2e_kind: "browser"``，缺省）—— 保留现状：必须写
  ``target_url``，由子 agent 驱动真实浏览器。**框架判不了它**，所以
  :func:`verification_ci_runner.validate_vp` 对它返回 issue，上层据此
  ESCALATE 回 LLM 子 agent。
* **command**（``e2e_kind: "command"``）—— 声明一条入口命令
  （``entry_command``），**由框架直接执行**，判定是退出码的纯函数 ——
  与 ``full_ci`` 同一档，不起 LLM。

TDD contract —— 5 cases
-----------------------
1. ``test_command_e2e_validates_without_ci_entry`` —— command 类 e2e
   **不需要** ``ci_entry``，有 ``entry_command`` 就零 issue。
2. ``test_browser_e2e_escalates`` —— 缺省（browser）类 e2e 返回 issue，
   上层 ESCALATE 回 LLM 路径。
3. ``test_entry_command_path_must_exist`` —— ``entry_command`` 引用的路径
   不存在 ⇒ issue，且 issue 里写着那个路径。
4. ``test_e2e_gate_accepts_command_kind`` —— 完整性门禁的 ``_satisfies``
   对 e2e(command) 仍然返回 True（回归锁：判定的行为一个字没改）。
5. ``test_full_ci_contract_unchanged`` —— ``full_ci`` 仍然要求 ``ci_entry``，
   缺省即 issue。

边界（另外钉住的几条）
----------------------
* ``entry_command`` 为空串 / 非 str ⇒ SchemaIssue。
* command 类**不做** ``looks_narrowed`` 检查 —— 点名一个测试文件正是
  一条 command 类 e2e 的常态（``pytest tests/e2e/test_cli.py``）。
* command 类的判定只看退出码，不读 stdout 的内容。
* command 类由框架直跑 ⇒ 分派器把它交给确定性 runner，不起 LLM 子会话。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_ci_runner import (  # noqa: E402
    ENTRY_COMMAND_FIELD,
    E2E_KIND_BROWSER,
    E2E_KIND_COMMAND,
    E2E_METHOD,
    STATUS_FAILED,
    STATUS_PASSED,
    run_ci_verification,
    validate_vp,
)


def _command_vp(entry_command: str, **extra) -> dict:
    """一条 command 类 e2e 验证点（子 agent 不参与，框架直跑）。"""
    vp = {
        "id": "VP-009",
        "verification_method": E2E_METHOD,
        "e2e_kind": E2E_KIND_COMMAND,
        "entry_command": entry_command,
    }
    vp.update(extra)
    return vp


def _repo_with_e2e_file(tmp_path: Path) -> Path:
    """一个只含 ``tests/e2e/test_cli.py`` 的假仓库根。"""
    root = tmp_path / "repo"
    (root / "tests" / "e2e").mkdir(parents=True)
    (root / "tests" / "e2e" / "test_cli.py").write_text(
        "def test_cli():\n    assert True\n", encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# 1. command 类 e2e：不需要 ci_entry，零 issue
# ---------------------------------------------------------------------------


def test_command_e2e_validates_without_ci_entry(tmp_path: Path) -> None:
    """一条 command 类 e2e 有 ``entry_command`` 就判得了 —— 没有 ``ci_entry``。

    这条入口点名了一个测试文件（``tests/e2e/test_cli.py``），而
    ``looks_narrowed`` 会把这种入口判成「收窄了验证范围」—— command 类
    **不做**这项检查：一条 e2e 就是要跑哪条链路就跑哪条链路。
    """
    root = _repo_with_e2e_file(tmp_path)
    vp = _command_vp("python -m pytest tests/e2e/test_cli.py -q")

    assert validate_vp(vp, project_dir=root) == []


# ---------------------------------------------------------------------------
# 2. browser 类 e2e：框架判不了 ⇒ issue ⇒ 上层 ESCALATE 回 LLM
# ---------------------------------------------------------------------------


def test_browser_e2e_escalates() -> None:
    """缺省子类是 browser —— 它由子 agent 驱动真浏览器，框架判不了。"""
    vp = {
        "id": "VP-010",
        "verification_method": E2E_METHOD,
        "target_url": "http://127.0.0.1:5173/checkout",
    }

    issues = validate_vp(vp)
    assert issues, (
        "browser 类 e2e 由子 agent 驱动真实浏览器，框架判不了 —— "
        "校验器必须返回 issue，上层据此 ESCALATE 回 LLM 子 agent"
    )

    from verification_subagent import (
        ESCALATE_PATH_MODE,
        LLM_ROUTE,
        resolve,
    )

    assert resolve(vp) == (LLM_ROUTE, ESCALATE_PATH_MODE)


def test_command_e2e_runs_in_the_framework_without_an_llm_session() -> None:
    """command 类走确定性 runner —— 不起 LLM 子会话。

    这是「e2e 与 UI 解耦」的另一半：browser 类判不了（issue ⇒ ESCALATE
    回 LLM），command 类判得了，于是分派器把它交给框架里的 runner。
    """
    from verification_subagent import (
        DETERMINISTIC_MODE,
        DETERMINISTIC_RUNNERS,
        resolve,
    )

    vp = _command_vp("python -m pytest tests/e2e -q")

    assert resolve(vp) == (
        DETERMINISTIC_RUNNERS[E2E_METHOD], DETERMINISTIC_MODE,
    )


def test_browser_e2e_kind_is_the_default() -> None:
    """写了 ``e2e_kind: "browser"`` 与一个字不写，判定完全一样。"""
    default_kind = {
        "id": "VP-010",
        "verification_method": E2E_METHOD,
        "target_url": "http://127.0.0.1:5173/checkout",
    }
    explicit_kind = dict(default_kind, e2e_kind=E2E_KIND_BROWSER)

    assert validate_vp(default_kind) == validate_vp(explicit_kind)
    assert validate_vp(explicit_kind), "browser 类仍然判不了"


# ---------------------------------------------------------------------------
# 3. entry_command 的路径必须存在
# ---------------------------------------------------------------------------


def test_entry_command_path_must_exist(tmp_path: Path) -> None:
    """入口点名了一个不存在的文件 ⇒ issue，而且 issue 里写着那个路径。"""
    vp = _command_vp("python -m pytest tests/e2e/missing_cli.py -q")

    issues = validate_vp(vp, project_dir=tmp_path)

    assert issues, "入口引用的路径不存在时必须判不可运行"
    assert "tests/e2e/missing_cli.py" in issues[0].detail


# ---------------------------------------------------------------------------
# 4. 完整性门禁：e2e(command) 仍然算「全量 E2E 关卡」（回归锁）
# ---------------------------------------------------------------------------


def test_e2e_gate_accepts_command_kind() -> None:
    """``_satisfies`` 对 e2e(command) 返回 True —— 判定的行为一个字没改。

    完整性门禁查的是**覆盖**（这条入口有没有人把关），不是**命名** ——
    所以子类是 browser 还是 command 都一样算数。
    """
    from verification_plan_completeness import FullGateEntry, _satisfies

    entry = FullGateEntry(
        kind="e2e", label="全量 E2E 套件", evidence="tests/e2e",
    )
    vp = _command_vp("python -m pytest tests/e2e -q")

    assert _satisfies(entry, vp) is True


# ---------------------------------------------------------------------------
# 5. full_ci 的契约一个字没改
# ---------------------------------------------------------------------------


def test_full_ci_contract_unchanged() -> None:
    """``full_ci`` 仍然要 ``ci_entry``，缺省即 issue。"""
    missing = {"id": "VP-013", "verification_method": "full_ci"}
    issues = validate_vp(missing)
    assert issues, "没有 ci_entry 的 full_ci 判不了 —— 退出码没有依据"
    assert "ci_entry" in issues[0].detail

    complete = {
        "id": "VP-014",
        "verification_method": "full_ci",
        "ci_entry": "python scripts/ci_local.py",
    }
    assert validate_vp(complete) == []


# ---------------------------------------------------------------------------
# 边界：entry_command 本身
# ---------------------------------------------------------------------------


def test_command_e2e_rejects_an_empty_entry_command() -> None:
    """空串不是入口 —— 判不了就判不了，不能当成「跑过了」。"""
    issues = validate_vp(_command_vp(""))
    assert issues
    assert ENTRY_COMMAND_FIELD in issues[0].detail


def test_command_e2e_rejects_a_non_string_entry_command() -> None:
    """非字符串的入口同样判不了。"""
    issues = validate_vp(_command_vp(123))  # type: ignore[arg-type]
    assert issues
    assert ENTRY_COMMAND_FIELD in issues[0].detail


# ---------------------------------------------------------------------------
# 边界：判定只看退出码
# ---------------------------------------------------------------------------


def test_command_e2e_is_executed_and_graded_on_the_exit_code(
    tmp_path: Path,
) -> None:
    """command 类 e2e 由框架直跑：退出 0 ⇒ PASSED，非 0 ⇒ FAILED。

    stdout 里写什么都不影响判定 —— 判定是退出码的纯函数。
    """
    root = _repo_with_e2e_file(tmp_path)
    passing = _command_vp(
        f'{sys.executable} -c "print(\'all good\'); raise SystemExit(0)"',
    )
    failing = _command_vp(
        f'{sys.executable} -c "print(\'all good\'); raise SystemExit(3)"',
    )

    ok = run_ci_verification(passing, project_dir=root)
    bad = run_ci_verification(failing, project_dir=root)

    assert ok.status == STATUS_PASSED, ok.reasons
    assert ok.evidence["exit_code"] == 0
    assert bad.status == STATUS_FAILED, bad.reasons
    assert bad.evidence["exit_code"] == 3
    # stdout 的内容没有参与判定：两次运行的输出一样，判定不同。
    assert "all good" in ok.evidence["stdout_tail"]
    assert "all good" in bad.evidence["stdout_tail"]
