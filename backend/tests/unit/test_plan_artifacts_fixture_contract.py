"""plan 目录状态构造器的契约 —— 目录即 fixture，绝不触碰真实 plans 树。

背景
----
本项目的测试数据不是"几条记录"，而是**一棵目录树的状态**：
``tasks.json`` / ``verification-plan.md`` / ``verification_plan.json`` /
``prd.json`` / ``review.json`` 五件套的任意子集。每个用例自己手写目录，
会出现三类漂移：各用例的"合法计划书"字节不同、有人图省事写真实的 plans
树、并发（``-n 8``）下同一个 plan id 被两个 worker 撞上。

钉住的不变量
------------
1. **标志位正交**：五个标志位的**每一种**组合，目录里只有对应的产物文件；
2. **快照跟得住改动**：改写文件 → sha256 变；只碰时间戳 → mtime 变；
3. **机器计划由计划书推导**：两者天然等价，不是另写一份常量；
4. **plan id 唯一且与 worker 无关**：同一个路径永远同一个 id，不同路径
   永远不同 id，xdist 的 worker 目录不算进去；
5. **只写文件系统**：不碰数据库（数据库行由 ``plan_sqlite_seeder`` 注入），
   也不在给定目录之外落任何东西。

这些用例全部用 ``tmp_path`` 真实落盘 —— 被测的就是"文件在不在、字节是不是
那一份"，mock 文件系统就把被测的东西换掉了。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework.ids import validate_plan_id  # noqa: E402
from tests.fixtures.plan_artifacts_fixture import (  # noqa: E402
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    PRD_FILENAME,
    REVIEW_FILENAME,
    TASKS_FILENAME,
    machine_plan_for,
    plan_dir,
    plan_id_for,
    snapshot,
    write_doc_bytes,
)
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    build_plan_doc,
)

pytestmark = pytest.mark.unit

#: 五个标志位 → 它们对应的产物文件名。
FLAG_ARTIFACTS = {
    "tasks": TASKS_FILENAME,
    "doc": PLAN_DOC_FILENAME,
    "machine_plan": MACHINE_PLAN_FILENAME,
    "prd": PRD_FILENAME,
    "review": REVIEW_FILENAME,
}

#: 标志位的全部组合（2^5）。正交性就是对这 32 种组合逐个断言。
FLAG_NAMES = tuple(FLAG_ARTIFACTS)


def _all_combinations() -> list[dict]:
    """五个标志位的全部组合，每个组合是一个 ``{标志: bool}`` 字典。"""
    return [
        dict(zip(FLAG_NAMES, values))
        for values in itertools.product((False, True), repeat=len(FLAG_NAMES))
    ]


def test_plan_dir_flags_are_orthogonal(tmp_path: Path) -> None:
    """每一种标志位组合，目录里只有对应的那些产物文件。"""
    combinations = _all_combinations()
    assert len(combinations) == 32, "组合数不对：正交性只覆盖了部分组合"

    for index, combination in enumerate(combinations):
        # 每个组合用自己的根目录：同一个根目录反复调用会把上一个组合的
        # 产物带进来，测的就不是正交性了。
        case_root = tmp_path / f"case-{index}"
        wanted = {
            FLAG_ARTIFACTS[flag]
            for flag, enabled in combination.items()
            if enabled
        }

        directory = plan_dir(case_root, **combination)

        assert directory.is_dir(), f"{combination}：目录没造出来"
        present = {entry.name for entry in directory.iterdir() if entry.is_file()}
        assert present == wanted, (
            f"{combination}：目录里的文件是 {sorted(present)}，"
            f"应该是 {sorted(wanted)}"
        )


def test_snapshot_tracks_sha_and_mtime(tmp_path: Path) -> None:
    """改写文件 → sha256 变；只碰时间戳 → mtime 变。"""
    target = tmp_path / TASKS_FILENAME
    target.write_text('{"tasks": []}', encoding="utf-8")
    # 时间戳钉死，不靠 sleep：mtime 的断言必须是确定的。
    os.utime(target, (1_000_000.0, 1_000_000.0))

    before = snapshot(tmp_path)
    assert set(before) == {TASKS_FILENAME}
    assert before[TASKS_FILENAME]["mtime"] == 1_000_000.0
    assert before[TASKS_FILENAME]["sha256"] == hashlib.sha256(
        b'{"tasks": []}'
    ).hexdigest()

    # 只改内容：sha 变，mtime 被显式拨到别的值。
    target.write_text('{"tasks": [{"id": "TASK-1"}]}', encoding="utf-8")
    os.utime(target, (2_000_000.0, 2_000_000.0))
    after = snapshot(tmp_path)

    assert after[TASKS_FILENAME]["sha256"] != before[TASKS_FILENAME]["sha256"]
    assert after[TASKS_FILENAME]["mtime"] == 2_000_000.0
    assert after[TASKS_FILENAME]["mtime"] != before[TASKS_FILENAME]["mtime"]

    # 目录里多一个文件，快照看得见。
    (tmp_path / PRD_FILENAME).write_text("{}", encoding="utf-8")
    assert set(snapshot(tmp_path)) == {TASKS_FILENAME, PRD_FILENAME}


def test_machine_plan_is_derived_from_the_document(tmp_path: Path) -> None:
    """机器计划由计划书推导，盘上的两份天然等价。"""
    directory = plan_dir(tmp_path, doc=True, machine_plan=True)

    doc_bytes = (directory / PLAN_DOC_FILENAME).read_bytes()
    assert doc_bytes == CANONICAL_PLAN_DOC.encode("utf-8"), (
        "计划书的字节不是 canonical 那一份：等价性门禁在不同用例里"
        "就会有不同含义"
    )

    on_disk = json.loads(
        (directory / MACHINE_PLAN_FILENAME).read_text(encoding="utf-8")
    )
    assert on_disk == machine_plan_for(build_plan_doc())


def test_plan_id_is_unique_and_worker_independent(tmp_path: Path) -> None:
    """同一个路径同一个 id；worker 目录换了 id 也不变。"""
    first = plan_id_for(tmp_path)
    assert first == plan_id_for(tmp_path), "同一个路径两次算出了两个 id"

    assert plan_id_for(tmp_path / "other") != first, (
        "两个不同的目录算出了同一个 id：并发时两个 plan 会撞名"
    )

    # xdist 给每个 worker 一个自己的 tmp 根（``popen-gw0`` 之类）。
    # plan id 与 worker 无关，所以同一个用例在哪个 worker 上跑都一样。
    on_gw0 = tmp_path / "popen-gw0" / "case-0"
    on_gw1 = tmp_path / "popen-gw1" / "case-0"
    assert plan_id_for(on_gw0) == plan_id_for(on_gw1), (
        "plan id 算进了 worker 目录：同一个用例在不同 worker 上会拿到"
        "不同的 plan id"
    )

    # 是合法的 plan id：过一遍生产代码自己的校验。
    assert validate_plan_id(first) == first


def test_plan_dir_writes_nothing_outside_the_given_directory(tmp_path: Path) -> None:
    """给定目录之外不落任何东西，别的文件一个字节都不动。"""
    # 故意不先建这个目录：构造器负责建它，所以"只多了它自己"这句话才成立。
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("untouched", encoding="utf-8")

    before_entries = {entry.name for entry in tmp_path.iterdir()}
    directory = plan_dir(workspace, tasks=True, doc=True)
    after_entries = {entry.name for entry in tmp_path.iterdir()}

    assert directory == workspace
    assert after_entries - before_entries == {directory.name}, (
        f"给定目录之外多出了 {sorted(after_entries - before_entries)}"
    )
    assert not before_entries - after_entries
    assert sibling.read_text(encoding="utf-8") == "untouched"


def test_plan_dir_never_opens_a_database(tmp_path: Path) -> None:
    """构造器只写文件：数据库行由 ``plan_sqlite_seeder`` 注入。"""
    import sqlite3

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "plan 目录状态构造器打开了数据库：写库的行由 "
            "plan_sqlite_seeder 注入，两套写库逻辑会漂成两套真源"
        )

    original = sqlite3.connect
    sqlite3.connect = _refuse  # type: ignore[assignment]
    try:
        directory = plan_dir(
            tmp_path,
            tasks=True,
            doc=True,
            machine_plan=True,
            prd=True,
            review=True,
        )
    finally:
        sqlite3.connect = original  # type: ignore[assignment]

    assert directory.is_dir()
    assert len(list(directory.iterdir())) == 5


def test_write_doc_bytes_writes_exact_bytes(tmp_path: Path) -> None:
    """字节写入：不经过文本模式的换行转换。"""
    target = tmp_path / "nested" / PLAN_DOC_FILENAME
    written = write_doc_bytes(target, build_plan_doc())

    assert written == target
    assert target.read_bytes() == build_plan_doc().encode("utf-8")
    assert b"\r\n" not in target.read_bytes()
