"""TDD spec —— 判定用的 harness 模块按**被测树**解析（VP-048 / repair-24 ①）。

背景
----
2026-11-08 的 VP-015 连续两轮失败，两轮都卡在 ``elapsed=900.0 /
threshold=900.0``（计划给这条 VP 声明的是 ``timeout_seconds=3600``），而
工作树里那份 ``verification_subagent.py`` 早就修好了 —— 它认那条声明
（``_declared_timeout_seconds``），预算真的给到 3600；它也会在宽限期内读回
``vp_artifacts/<vp_id>/checkpoints.json``（``_await_stuck_artifact`` →
``_verdict_from_checkpoints``），产物在就在宽限期内把判决重建出来。

差别只在**谁在判**：

* 验证轮由 harness（主检出）里的常驻进程执行（AC server 常年不重启）；
* 它通过 ``sys.path`` 解析 ``verification_subagent`` /
  ``verification_executor``，拿到的是**主检出里那份没修过的**文件；
* 于是修复停在分支上、判定跑的是旧代码。

仓库里已有成例：``backend/verification_ci_runner_loader.py`` 开头逐字记录了
同一失效类，并给出纪律（候选路径、``is_framework_checkout`` 依据
``<project_dir>/backend/pyproject.toml``、``spec_from_file_location`` + 摘要
命名 + ``sys.modules`` 登记后摘除、每轮重载、加载失败回落 harness 并把
``fallback_reason`` 写进 provenance）。本 spec 把同一套纪律推广到决定 e2e 腿
的这两个模块。

这条 spec 钉住的就是"判定用的那份代码从哪来"：

1. **候选路径与框架检出判定** → 被测树就是框架仓 → 必须用**被测树那两份**；
2. **按路径加载** → ``spec_from_file_location`` + 摘要命名 +
   ``sys.modules`` 登记后摘除（不用裸名，避免污染 harness 自身的 import）；
3. **每轮重新加载** → 同一进程里连着解析两次，拿到的是**两个不同的模块
   对象**（长驻进程不会拿着一份陈旧的模块判定一整轮）；
4. **坏文件回落** → 加载失败绝不抛出，回落 harness 副本，且
   ``fallback_reason`` 非空、**点名触发的规则**（负控）；
5. **两个模块各自独立判定** → 一份坏掉不拖累另一份，provenance 分别记录；
6. **出处可核** → ``sha256`` 与磁盘上那份文件的内容哈希一致，``git_head``
   是被测树的 HEAD（取不到时给一个能解释的值）。

测试命令::

    backend/.venv/bin/python -m pytest -q backend/tests/unit/test_verification_harness_loader.py
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

try:  # the module under test
    import verification_harness_loader as loader
except ImportError:  # pragma: no cover - the RED state, before it exists
    loader = None


#: A project-side stand-in for ``verification_subagent``.  It is a valid
#: module with the same names the framework uses, so the loader can load
#: it exactly as it would load the real one.
_PROJECT_SUBAGENT_SOURCE = '''\
"""A project-side stand-in for ``verification_subagent``."""

SUPPORTED_METHODS = ("method_a", "method_b")
LEGACY_METHODS = ("legacy_a",)


class VerificationSubAgent:
    origin = "project"
'''

#: A project-side stand-in for ``verification_executor``.
_PROJECT_EXECUTOR_SOURCE = '''\
"""A project-side stand-in for ``verification_executor``."""


class VerificationExecutor:
    origin = "project"
'''

#: A project-side module that cannot even be parsed.
_BROKEN_SOURCE = "class VerificationSubAgent::\n"


class _MissingLoader:
    """Stands in for the module under test while it does not exist yet.

    Every use of it raises — loudly, and inside the test body, so a
    missing deliverable is a **failure** rather than a skip or a
    collection error.
    """

    def __getattr__(self, name: str):
        raise AssertionError(
            "backend/verification_harness_loader.py is not importable — that "
            "file is the deliverable of this spec.",
        )


@pytest.fixture()
def harness_loader():
    """The module under test."""
    return loader if loader is not None else _MissingLoader()


def _framework_checkout(
    root: Path,
    subagent_source: str = _PROJECT_SUBAGENT_SOURCE,
    executor_source: str = _PROJECT_EXECUTOR_SOURCE,
) -> Path:
    """A project tree that *is* a checkout of this framework.

    The layout is the one the loader reads: ``backend/pyproject.toml`` says
    "this tree is the framework", and the two candidate modules are the
    things the loader resolves.
    """
    backend = root / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'fake-backend'\n", encoding="utf-8",
    )
    (backend / "verification_subagent.py").write_text(
        subagent_source, encoding="utf-8",
    )
    (backend / "verification_executor.py").write_text(
        executor_source, encoding="utf-8",
    )
    return backend


def _fake_git_checkout(root: Path, sha: str) -> None:
    """Give ``root`` the git metadata a checkout has — without git.

    The loader reads the repository's own files (no subprocess), so
    writing ``.git/HEAD`` and the ref it points at is enough.
    """
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1. 候选路径与框架检出判定
# ---------------------------------------------------------------------------
def test_the_candidate_paths_are_the_two_modules_of_the_tree(
    tmp_path, harness_loader,
):
    """候选路径就是 ``<project_dir>/backend/`` 下那两个模块。"""
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)

    assert harness_loader.project_module_path(
        project, "verification_subagent",
    ) == backend / "verification_subagent.py"
    assert harness_loader.project_module_path(
        project, "verification_executor",
    ) == backend / "verification_executor.py"
    assert harness_loader.project_module_path(
        project, "verification_nothing",
    ) is None


def test_a_framework_checkout_resolves_to_both_project_copies(
    tmp_path, harness_loader,
):
    """被测树就是框架仓 → 两份都用**被测树那两份**。"""
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["resolution"] == (
        harness_loader.RESOLUTION_PROJECT
    )
    assert loaded.provenance["executor"]["resolution"] == (
        harness_loader.RESOLUTION_PROJECT
    )
    assert loaded.provenance["subagent"]["module_path"] == str(
        backend / "verification_subagent.py"
    )
    assert loaded.provenance["executor"]["module_path"] == str(
        backend / "verification_executor.py"
    )
    assert loaded.provenance["fallback_reason"] == ""
    assert loaded.from_project is True


def test_a_tree_without_pyproject_is_not_the_framework(
    tmp_path, harness_loader,
):
    """只有同名模块、没有 ``backend/pyproject.toml`` → 不是框架仓检出。"""
    project = tmp_path / "some-project"
    backend = project / "backend"
    backend.mkdir(parents=True)
    (backend / "verification_subagent.py").write_text(
        _PROJECT_SUBAGENT_SOURCE, encoding="utf-8",
    )
    (backend / "verification_executor.py").write_text(
        _PROJECT_EXECUTOR_SOURCE, encoding="utf-8",
    )

    assert harness_loader.is_framework_checkout(project) is False

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["resolution"] == (
        harness_loader.RESOLUTION_HARNESS
    )
    assert loaded.provenance["executor"]["resolution"] == (
        harness_loader.RESOLUTION_HARNESS
    )
    assert "not a framework checkout" in loaded.provenance["fallback_reason"]


def test_a_tree_missing_one_candidate_is_not_the_framework(
    tmp_path, harness_loader,
):
    """候选少一个 → 判定依据说"不是框架仓检出"（两个候选都要在）。"""
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)
    (backend / "verification_executor.py").unlink()

    assert harness_loader.is_framework_checkout(project) is False


# ---------------------------------------------------------------------------
# 2. 按路径加载 —— 摘要命名 + sys.modules 登记后摘除
# ---------------------------------------------------------------------------
def test_the_modules_are_loaded_from_the_path_on_disk(
    tmp_path, harness_loader,
):
    """跑的是**磁盘上那一份**，不是同名模块的另一次 import。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.subagent_class.origin == "project"
    assert loaded.executor_class.origin == "project"
    assert loaded.supported_methods == ("method_a", "method_b")
    assert loaded.legacy_methods == ("legacy_a",)


def test_the_bare_module_names_are_not_taken_over(tmp_path, harness_loader):
    """不用裸名登记 —— harness 自己的 import 不被污染。

    登记成 ``verification_subagent`` 会让框架后面那次普通 import 拿到
    被测树那份；摘要命名 + 登记后摘除才不会。
    """
    project = tmp_path / "worktree"
    _framework_checkout(project)
    before = sys.modules.get("verification_subagent")

    loaded = harness_loader.load_harness_modules(project)

    assert sys.modules.get("verification_subagent") is before
    assert loaded.subagent_module is not before
    assert loaded.subagent_module.__name__.startswith(
        "pdt_project_verification_subagent_",
    )
    assert loaded.executor_module.__name__.startswith(
        "pdt_project_verification_executor_",
    )


def test_no_digest_named_module_is_left_behind(tmp_path, harness_loader):
    """执行后摘除 —— ``sys.modules`` 里不留摘要命名的残骸。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    harness_loader.load_harness_modules(project)

    leftovers = [
        name for name in sys.modules
        if name.startswith("pdt_project_verification_")
    ]
    assert leftovers == []


def test_the_real_modules_survive_being_loaded_by_path(
    tmp_path, harness_loader,
):
    """真模块按路径加载得起来 —— 加载器不假设它们的 import 上下文。

    替身只能证明"机制对"；真模块才能证明"这条路走得通"：它们是几百 KB 的
    真实模块，装着自己的平铺 import，路径加载不能假设任何 import 上下文。
    """
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)
    for module_name in ("verification_subagent", "verification_executor"):
        shutil.copy(
            harness_loader.harness_module_path(module_name),
            backend / f"{module_name}.py",
        )

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["resolution"] == (
        harness_loader.RESOLUTION_PROJECT
    )
    assert loaded.provenance["executor"]["resolution"] == (
        harness_loader.RESOLUTION_PROJECT
    )
    assert loaded.subagent_class is not None
    assert loaded.executor_class is not None
    assert loaded.supported_methods
    assert loaded.legacy_methods


# ---------------------------------------------------------------------------
# 3. 每轮重新加载 —— 不吃陈旧缓存
# ---------------------------------------------------------------------------
def test_two_rounds_give_two_distinct_modules(tmp_path, harness_loader):
    """同一进程里连着解析两次 → 两个不同的模块对象。

    这是"长驻进程持有陈旧模块"的解药：模块是**每次新建**的，所以
    磁盘上改过的代码下一轮就会生效。
    """
    project = tmp_path / "worktree"
    _framework_checkout(project)

    first = harness_loader.load_harness_modules(project)
    second = harness_loader.load_harness_modules(project)

    assert first.subagent_module is not second.subagent_module
    assert first.executor_module is not second.executor_module


def test_the_second_round_sees_the_file_that_was_just_fixed(
    tmp_path, harness_loader,
):
    """改完磁盘上的代码，第二次解析拿到的是改过的那份。"""
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)

    first = harness_loader.load_harness_modules(project)
    (backend / "verification_subagent.py").write_text(
        _PROJECT_SUBAGENT_SOURCE.replace('"project"', '"fixed"'),
        encoding="utf-8",
    )
    second = harness_loader.load_harness_modules(project)

    assert first.subagent_class.origin == "project"
    assert second.subagent_class.origin == "fixed"
    assert (
        first.provenance["subagent"]["sha256"]
        != second.provenance["subagent"]["sha256"]
    )


# ---------------------------------------------------------------------------
# 4. 出处可核 —— 四字段 + fallback_reason
# ---------------------------------------------------------------------------
def test_the_provenance_carries_every_field(tmp_path, harness_loader):
    """出处说得出：哪份文件、什么哈希、哪个 HEAD、怎么来的。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    loaded = harness_loader.load_harness_modules(project)

    for key in ("subagent", "executor"):
        assert set(loaded.provenance[key]) == {
            "resolution", "module_path", "sha256", "git_head", "fallback_reason",
        }
        assert loaded.provenance[key]["resolution"] in {
            harness_loader.RESOLUTION_PROJECT,
            harness_loader.RESOLUTION_HARNESS,
        }
        assert loaded.provenance[key]["module_path"]
        assert loaded.provenance[key]["sha256"]


def test_the_sha256_is_the_hash_of_the_file_that_judged(
    tmp_path, harness_loader,
):
    """出处里的 sha256 就是磁盘上那份文件的内容哈希。"""
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["sha256"] == _sha256(
        backend / "verification_subagent.py"
    )
    assert loaded.provenance["executor"]["sha256"] == _sha256(
        backend / "verification_executor.py"
    )


def test_the_module_path_is_absolute(tmp_path, harness_loader):
    """出处给的是绝对路径 —— 说得出是哪一份文件。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    loaded = harness_loader.load_harness_modules(project)

    for key in ("subagent", "executor"):
        assert Path(loaded.provenance[key]["module_path"]).is_absolute()


def test_the_git_head_is_the_trees_head(tmp_path, harness_loader):
    """``git_head`` 是被测树的 HEAD。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)
    sha = "1" * 40
    _fake_git_checkout(project, sha)

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["git_head"] == sha
    assert loaded.provenance["executor"]["git_head"] == sha


def test_the_git_head_is_explainable_when_it_cannot_be_read(
    tmp_path, harness_loader,
):
    """取不到 HEAD 时给一个**能解释**的值，不是空串。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    loaded = harness_loader.load_harness_modules(project)

    head = loaded.provenance["subagent"]["git_head"]
    assert head
    assert re.fullmatch(r"[0-9a-f]{40}", head) or head.startswith("unknown")


# ---------------------------------------------------------------------------
# 5. 加载失败绝不抛出（负控）
# ---------------------------------------------------------------------------
def test_a_broken_module_falls_back_without_raising(
    tmp_path, harness_loader,
):
    """被测树那份语法坏掉 → 回落 harness 副本，**不抛**。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, subagent_source=_BROKEN_SOURCE)

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["resolution"] == (
        harness_loader.RESOLUTION_HARNESS
    )
    assert loaded.provenance["subagent"]["fallback_reason"]
    assert loaded.subagent_class is not None


def test_the_fallback_reason_names_the_rule_and_the_exception(
    tmp_path, harness_loader,
):
    """回落原因**点名触发的规则**与异常类型。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, subagent_source=_BROKEN_SOURCE)

    loaded = harness_loader.load_harness_modules(project)

    reason = loaded.provenance["subagent"]["fallback_reason"]
    assert "load failed" in reason
    assert "SyntaxError" in reason


def test_a_fallback_reports_the_harness_file(tmp_path, harness_loader):
    """回落到 harness → 出处说的是 harness 那份文件的哈希与路径。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, executor_source=_BROKEN_SOURCE)
    harness_executor = harness_loader.harness_module_path(
        "verification_executor"
    )

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["executor"]["module_path"] == str(
        harness_executor
    )
    assert loaded.provenance["executor"]["sha256"] == _sha256(
        harness_executor
    )


# ---------------------------------------------------------------------------
# 6. 两个模块各自独立判定
# ---------------------------------------------------------------------------
def test_the_two_modules_are_judged_independently(tmp_path, harness_loader):
    """一份坏掉不拖累另一份 —— 出处分别记录。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, executor_source=_BROKEN_SOURCE)

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["resolution"] == (
        harness_loader.RESOLUTION_PROJECT
    )
    assert loaded.provenance["executor"]["resolution"] == (
        harness_loader.RESOLUTION_HARNESS
    )
    assert loaded.provenance["subagent"]["fallback_reason"] == ""
    assert loaded.provenance["executor"]["fallback_reason"]
    assert loaded.subagent_class.origin == "project"


def test_a_missing_candidate_only_falls_back_for_that_module(
    tmp_path, harness_loader,
):
    """候选缺失 → **那一份**回落，另一份仍用被测树的。"""
    project = tmp_path / "worktree"
    backend = _framework_checkout(project)
    (backend / "verification_executor.py").unlink()

    loaded = harness_loader.load_harness_modules(project)

    assert loaded.provenance["subagent"]["resolution"] == (
        harness_loader.RESOLUTION_PROJECT
    )
    assert loaded.provenance["executor"]["resolution"] == (
        harness_loader.RESOLUTION_HARNESS
    )
    assert "candidate missing" in (
        loaded.provenance["executor"]["fallback_reason"]
    )


# ---------------------------------------------------------------------------
# 7. 纪律 —— 不引入端口字面量，不碰陌生进程
# ---------------------------------------------------------------------------
def test_the_loader_carries_no_port_literal_and_kills_nothing(harness_loader):
    """这个模块不碰端口，也不杀任何进程。"""
    source = Path(harness_loader.__file__).read_text(encoding="utf-8")

    assert not re.search(r":\s*\d{4,5}\b", source)
    assert not re.search(r"\bport\b", source, re.IGNORECASE)
    for forbidden in ("os.kill", "SIGKILL", "SIGTERM", "killpg", "pkill"):
        assert forbidden not in source