"""TDD spec —— 被测树那份评分器**能被加载**，哪怕 harness 的 backend 不带它新增的模块。

背景
----
2026-11-09 的整仓门禁：VP-014 的判定**根本没跑分支那份 runner**。框架回传
的出处是 ``resolution=harness``、``fallback_reason="ModuleNotFoundError: No
module named 'ci_artifact'"`` —— 被测树那份 runner 因为一条**平铺 import**
加载不了，落回了 harness 自己的副本。于是分支上的交付物（产物契约 + 轮次
副本）**一次都没有被 full_ci 执行过**。

原因在加载器的一条约定上：``verification_ci_runner_loader`` 只把 **harness 的**
``backend/`` 放进 ``sys.path``，所以被测树那份 runner 里的
``from ci_artifact import ...`` 解析到的是 harness 的副本 —— 而 harness 的
``backend/`` 当时**没有** ``ci_artifact.py``，import 于是失败，整份 runner
加载不了。

修的不是加载器的通用策略（它为什么只放 harness 的 backend，模块文档里写了
理由），而是让 runner **自己**解析它新增的那个模块：解析建立在**模块自身的
位置**上，而不是调用方 ``sys.path`` 里恰好有什么。

钉住的是三条：

1. harness 的 backend **没有** ``ci_artifact`` 时，被测树那份 runner 仍然加载
   成功，``resolution`` 是 ``project``（本文件的主用例）；
2. 落回 harness 副本时，``fallback_reason`` 说得出**为什么**（负控：回落不是
   静默降级，也不是崩溃）；
3. 校验器**只有一份真源**：被测树那份 runner 用的，是**它自己目录里**那份
   ``ci_artifact``，不是 harness 的。

测试命令::

    pytest backend/tests/unit/test_ci_runner_loader_project_tree.py -v
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification_ci_runner_loader import (  # noqa: E402
    RESOLUTION_HARNESS,
    RESOLUTION_PROJECT,
    harness_runner_path,
    load_ci_runner,
)


def _framework_checkout(root: Path, *, with_contract: bool = True) -> Path:
    """A tree under test that *is* a checkout of this framework.

    The layout is the one the loader reads: ``backend/verification_ci_runner.py``
    **and** ``backend/pyproject.toml``.  The runner is the **real** one — the
    point is that the runner that ships with the tree has to load, not a
    stand-in that imports nothing.

    ``with_contract=False`` leaves out ``backend/ci_artifact.py``: the tree
    under test then carries a runner whose contract module is nowhere to be
    found, which is the case that must fall back **loudly**.
    """
    backend = root / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'fake-backend'\n", encoding="utf-8",
    )
    shutil.copy(harness_runner_path(), backend / "verification_ci_runner.py")
    if with_contract:
        shutil.copy(
            BACKEND_DIR / "ci_artifact.py", backend / "ci_artifact.py",
        )
    return backend


def _without_a_flat_ci_artifact(monkeypatch) -> None:
    """Make ``import ci_artifact`` impossible, the way a harness without it is.

    The harness' ``backend/`` is what the loader puts on ``sys.path``, and a
    harness that does not carry ``ci_artifact.py`` makes the **flat** import
    fail — that is the state the 2026-11-09 gate judged in.  ``sys.modules``
    holding ``None`` under a name is how CPython says "this name is not
    importable", and it blocks the flat import without touching the file
    system.
    """
    monkeypatch.setitem(sys.modules, "ci_artifact", None)


# ---------------------------------------------------------------------------
# 1. 主用例 —— harness 不带 ci_artifact，被测树那份 runner 仍然被加载
# ---------------------------------------------------------------------------
def test_loader_loads_project_runner_when_harness_backend_lacks_ci_artifact(
    tmp_path, monkeypatch,
):
    """harness 的 backend 没有 ``ci_artifact`` → 被测树那份 runner 仍然被加载。

    这就是 2026-11-09 那次判定的形状：一份带新 import 的 runner，被测树
    里有它需要的模块，而加载器**只**把 harness 的 ``backend/`` 放进
    ``sys.path``。修好之后，判定跑的是被测树那份 —— 出处里写着
    ``resolution=project``。
    """
    project = tmp_path / "worktree"
    _framework_checkout(project)
    _without_a_flat_ci_artifact(monkeypatch)

    loaded = load_ci_runner(project)

    assert loaded.provenance["resolution"] == RESOLUTION_PROJECT, (
        "the tree under test's own runner was not the one that judged: "
        f"{loaded.provenance}"
    )
    assert loaded.provenance["fallback_reason"] == "", (
        "the tree under test's runner fell back to the harness' copy: "
        f"{loaded.provenance}"
    )


# ---------------------------------------------------------------------------
# 2. 真源唯一 —— 用的是被测树自己那份契约
# ---------------------------------------------------------------------------
def test_the_runner_uses_the_contract_that_ships_next_to_it(
    tmp_path, monkeypatch,
):
    """校验器只有一份真源：runner 旁边那份 ``ci_artifact``。

    解析建立在**模块自身的位置**上，所以被测树那份 runner 用的是**它自己
    目录里**的契约文件 —— 不是 harness 的，哪怕 harness 也有一份。
    """
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)
    _without_a_flat_ci_artifact(monkeypatch)

    loaded = load_ci_runner(project)

    validator = loaded.module.validate_ci_artifact
    used_file = Path(validator.__globals__["__file__"]).resolve()
    assert used_file == (backend / "ci_artifact.py").resolve(), (
        "the runner validated its payload against a contract that does not "
        f"ship with it: {used_file}"
    )


# ---------------------------------------------------------------------------
# 3. 负控 —— 找不到契约时回落，并且说得出为什么
# ---------------------------------------------------------------------------
def test_a_runner_whose_contract_is_nowhere_falls_back_and_says_why(
    tmp_path, monkeypatch,
):
    """契约哪儿都没有 → 回落 harness 副本，原因写进出处。

    回落不是崩溃，也不是"静默地没有校验"：出处里必须说得出是哪个模块
    找不到，读者才知道这一轮判的是谁。
    """
    project = tmp_path / "worktree"
    _framework_checkout(project, with_contract=False)
    _without_a_flat_ci_artifact(monkeypatch)

    loaded = load_ci_runner(project)

    assert loaded.provenance["resolution"] == RESOLUTION_HARNESS, (
        f"a runner whose contract is nowhere must not judge the round: "
        f"{loaded.provenance}"
    )
    assert "ci_artifact" in loaded.provenance["fallback_reason"], (
        "the fallback did not say which module was missing: "
        f"{loaded.provenance}"
    )
    assert callable(loaded.run_ci_verification)