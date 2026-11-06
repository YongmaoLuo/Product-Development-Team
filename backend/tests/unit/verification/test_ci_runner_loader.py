"""TDD spec —— 评分器（``verification_ci_runner``）按**被测树**解析（F1）。

背景
----
2026-11-05 的 VP-015 连续两轮失败，两轮的失败 detail **逐字节相同**
（``ci_entry 引用的路径不存在：backend/.venv)``），而工作树里那份
``verification_ci_runner.py`` 早就修好了 —— 复现用的就是它。差别只在
**谁在判**：

* 验证轮由 harness（主检出）里的常驻进程执行；
* 它通过 ``sys.path`` 解析 ``verification_ci_runner``，拿到的是**主检出
  里那份没修过的**文件；
* 于是修复停在分支上，判定跑的是旧代码。

这条 spec 钉住的就是"判定用的那份代码从哪来"：

1. **被测树就是框架仓** → 必须用**被测树那份**评分器；
2. **每轮重新加载** → 同一进程里连着解析两次，拿到的是**两个不同的模块
   对象**（长驻进程不会拿着一份陈旧的模块判定一整轮）；
3. **坏文件回落** → 被测树那份语法坏掉时，回落到 harness 副本，且
   ``fallback_reason`` 非空、**门禁仍然能跑**（负控）；
4. **出处可核** → ``sha256`` 与磁盘上那份文件的内容哈希一致；
5. **不是框架仓的被测树** → 用 harness 副本（这是设计，不是回落）。

测试命令::

    pytest backend/tests/unit/verification/test_ci_runner_loader.py -v
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification_ci_runner_loader import (  # noqa: E402
    RESOLUTION_HARNESS,
    RESOLUTION_PROJECT,
    harness_runner_path,
    is_framework_checkout,
    load_ci_runner,
    project_runner_path,
)


#: A project-side runner that is *not* the harness' copy.  It is a
#: valid module with the same entry point, so the loader can load it
#: exactly as it would load the real one.
_PROJECT_RUNNER_SOURCE = '''\
"""A project-side stand-in for ``verification_ci_runner``."""


def run_ci_verification(vp, *, project_dir, artifact_dir=None):
    return {
        "status": "PASSED",
        "reasons": [],
        "evidence": {"from": "project"},
    }
'''

#: A project-side runner that cannot even be parsed.
_BROKEN_RUNNER_SOURCE = "def run_ci_verification(vp, *, project_dir::\n"


def _framework_checkout(root: Path, source: str = _PROJECT_RUNNER_SOURCE) -> Path:
    """A project tree that *is* a checkout of this framework.

    The layout is the one the loader reads: ``backend/verification_ci_runner.py``
    **and** ``backend/pyproject.toml``.  The second file is what says
    "this tree is the framework" — a tree that merely happens to carry a
    file with that name is not judged by it.
    """
    backend = root / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'fake-backend'\n", encoding="utf-8",
    )
    (backend / "verification_ci_runner.py").write_text(
        source, encoding="utf-8",
    )
    return backend / "verification_ci_runner.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1. 被测树就是框架仓 → 用被测树那份
# ---------------------------------------------------------------------------
def test_framework_checkout_resolves_to_the_project_copy(tmp_path):
    """候选文件与 pyproject 都在 → resolution 是 ``project``。"""
    project = tmp_path / "worktree"
    runner_path = _framework_checkout(project)

    loaded = load_ci_runner(project)

    assert loaded.provenance["resolution"] == RESOLUTION_PROJECT
    assert loaded.provenance["module_path"] == str(runner_path)
    assert loaded.provenance["fallback_reason"] == ""


def test_the_project_copy_is_the_one_that_runs(tmp_path):
    """跑的确实是被测树那份 —— 不是"来源写着是、跑的却是别的"。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    loaded = load_ci_runner(project)

    assert loaded.run_ci_verification(
        {"id": "VP-1"}, project_dir=project,
    )["evidence"]["from"] == "project"


# ---------------------------------------------------------------------------
# 2. 每轮重新加载 —— 不吃陈旧缓存
# ---------------------------------------------------------------------------
def test_two_resolutions_give_two_distinct_modules(tmp_path):
    """同一进程里连着解析两次 → 两个不同的模块对象。

    这是"长驻进程持有陈旧模块"的解药：模块是**每次新建**的，所以
    磁盘上改过的代码下一轮就会生效。
    """
    project = tmp_path / "worktree"
    _framework_checkout(project)

    first = load_ci_runner(project)
    second = load_ci_runner(project)

    assert first.module is not second.module
    assert first.run_ci_verification is not second.run_ci_verification


def test_the_second_resolution_sees_the_file_on_disk(tmp_path):
    """改完磁盘上的代码，第二次解析拿到的是改过的那份。"""
    project = tmp_path / "worktree"
    runner_path = _framework_checkout(project)

    first = load_ci_runner(project)
    runner_path.write_text(
        _PROJECT_RUNNER_SOURCE.replace('"from": "project"', '"from": "v2"'),
        encoding="utf-8",
    )
    second = load_ci_runner(project)

    assert first.provenance["sha256"] != second.provenance["sha256"]
    assert second.run_ci_verification(
        {"id": "VP-1"}, project_dir=project,
    )["evidence"]["from"] == "v2"


# ---------------------------------------------------------------------------
# 3. 坏文件回落（负控）—— 门禁仍然能跑
# ---------------------------------------------------------------------------
def test_broken_project_copy_falls_back_to_the_harness(tmp_path):
    """被测树那份语法坏掉 → 回落 harness 副本，原因写进出处。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, source=_BROKEN_RUNNER_SOURCE)

    loaded = load_ci_runner(project)

    assert loaded.provenance["resolution"] == RESOLUTION_HARNESS
    assert loaded.provenance["fallback_reason"]
    assert loaded.provenance["module_path"] == str(harness_runner_path())


def test_the_gate_still_runs_after_a_fallback(tmp_path):
    """回落之后门禁**仍然能跑** —— 回落不是"这一轮没法判"。"""
    project = tmp_path / "worktree"
    _framework_checkout(project, source=_BROKEN_RUNNER_SOURCE)

    loaded = load_ci_runner(project)

    assert callable(loaded.run_ci_verification)
    assert loaded.run_ci_verification(
        {"id": "VP-1"}, project_dir=project,
    ) is not None


# ---------------------------------------------------------------------------
# 4. 出处可核 —— sha256 与磁盘一致
# ---------------------------------------------------------------------------
def test_provenance_sha256_matches_the_file_on_disk(tmp_path):
    """出处里的 sha256 就是磁盘上那份文件的内容哈希。"""
    project = tmp_path / "worktree"
    runner_path = _framework_checkout(project)

    loaded = load_ci_runner(project)

    assert loaded.provenance["sha256"] == _sha256(runner_path)


def test_provenance_carries_every_key_a_reader_needs(tmp_path):
    """出处说得出：哪份文件、什么哈希、哪个 HEAD、怎么来的。"""
    project = tmp_path / "worktree"
    _framework_checkout(project)

    loaded = load_ci_runner(project)

    assert set(loaded.provenance) == {
        "resolution", "module_path", "sha256", "git_head", "fallback_reason",
    }
    assert loaded.provenance["module_path"]
    assert loaded.provenance["sha256"]


# ---------------------------------------------------------------------------
# 5. 不是框架仓的被测树 → harness 副本（设计，不是回落）
# ---------------------------------------------------------------------------
def test_a_plain_project_uses_the_harness_copy(tmp_path):
    """被测树不是框架仓 → 用 harness 副本，且**不是**回落。"""
    project = tmp_path / "some-project"
    (project / "backend").mkdir(parents=True)

    loaded = load_ci_runner(project)

    assert loaded.provenance["resolution"] == RESOLUTION_HARNESS
    assert loaded.provenance["fallback_reason"] == ""
    assert loaded.provenance["module_path"] == str(harness_runner_path())


def test_a_project_with_the_file_but_no_pyproject_is_not_the_framework(tmp_path):
    """只有同名文件、没有 ``backend/pyproject.toml`` → 不是框架仓检出。"""
    project = tmp_path / "some-project"
    (project / "backend").mkdir(parents=True)
    (project / "backend" / "verification_ci_runner.py").write_text(
        _PROJECT_RUNNER_SOURCE, encoding="utf-8",
    )

    assert is_framework_checkout(project) is False
    # The candidate file is there — it is the *second* file that says
    # "this tree is the framework", so the candidate alone does not.
    assert project_runner_path(project) is not None
    assert load_ci_runner(project).provenance["resolution"] == RESOLUTION_HARNESS


# ---------------------------------------------------------------------------
# 6. 判据本身
# ---------------------------------------------------------------------------
def test_is_framework_checkout_reads_both_files(tmp_path):
    """两个文件都在 → 是框架仓检出；少一个 → 不是。"""
    project = tmp_path / "worktree"
    runner_path = _framework_checkout(project)

    assert is_framework_checkout(project) is True
    assert project_runner_path(project) == runner_path

    runner_path.unlink()
    assert is_framework_checkout(project) is False


# ---------------------------------------------------------------------------
# 7. 真模块自报出处 —— 落盘的那份证据说得出"判的是哪一份"
# ---------------------------------------------------------------------------
def test_the_real_runner_reports_the_project_resolution(tmp_path):
    """真模块被当作"被测树那份"加载时，它自报 ``project``。

    结论要落在磁盘上的 artifact 里，所以"这份是被测树自己的还是
    harness 的"必须由**模块自己**答得出来 —— 只有它知道自己是哪个
    文件。用真模块（而不是替身）才验得住这件事。
    """
    project = tmp_path / "worktree"
    backend = project / "backend"
    backend.mkdir(parents=True)
    (backend / "pyproject.toml").write_text(
        "[project]\nname = 'fake-backend'\n", encoding="utf-8",
    )
    shutil.copy(harness_runner_path(), backend / "verification_ci_runner.py")

    loaded = load_ci_runner(project)

    assert loaded.provenance["resolution"] == RESOLUTION_PROJECT
    source = loaded.module.verifier_source(project)
    assert source["resolution"] == RESOLUTION_PROJECT
    assert source["module_path"] == str(
        (backend / "verification_ci_runner.py").resolve()
    )
