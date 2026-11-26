"""验证计划**来源**的三态解析（``resolve_plan_source()``）。

为什么要有这组测试
------------------
"验证轮该消费哪一份计划"只有三个答案：``REVIEWED`` / ``MACHINE_ONLY`` /
``NONE``。判定一旦漂了，症状出现在很远的地方：

* 来源被写成 :func:`plan_artifacts.detect` 的派生 —— 开发分支上"有计划书、
  无机器计划"会落回 LLM 自动生成，用户审阅过的结果被忽略；
* 来源只看"机器计划在不在" —— 翻译环节被当成已发生，计划书形同虚设；
* 判定多写一次盘 —— 判定成了有状态的第二真源，产物删掉之后答案还留着。

所以这里穷举三个文件名（``tasks.json`` / ``verification-plan.md`` /
``verification_plan.json``）产生的 2³ = 8 种磁盘组合，而不是抽三个样例。
八格里最容易写错的两格：

* 任务清单 + 机器计划、无计划书 → :func:`plan_artifacts.detect` 说是 ``dev``，
  来源却是 ``MACHINE_ONLY`` —— 两个判定**正交**，来源不能从 ``detect()`` 派生；
* 计划书与机器计划同时在 → 来源仍是 ``REVIEWED``（计划书是真源，机器计划
  只是它的派生物，且必须过等价性门禁）。

判定是**纯读**：调用前后目录的 sha256 快照必须逐字节不变；机器计划的文件名
只有一处定义（``plan_artifacts``），路由模块只保留导入别名。

所有用例都用 ``tmp_path`` 真实落盘，不 mock 文件系统 —— 被测的就是
"文件在不在"。
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

# ``backend/`` 在 ``sys.path`` 上，``plan_artifacts`` / ``routes.verification``
# 才 import 得到（与同目录其它用例同一套路）。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_artifacts  # noqa: E402
from plan_artifacts import (  # noqa: E402
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    TASKS_FILENAME,
    detect,
)

# ``server`` 必须先 import：``server`` 自己会 import ``routes.verification``，
# 反过来先 import 会撞成半个模块的循环 import。
import server  # noqa: E402,F401
from routes import verification as routes_verification  # noqa: E402

pytestmark = pytest.mark.unit


#: ``(tasks.json 在, verification-plan.md 在, verification_plan.json 在)``
#: → ``(期望来源的枚举成员名, detect() 的分支取值)``。
#:
#: 2³ = 8 是判定的完整输入域 —— 八行就是全部。``detect()`` 那一列不是装饰：
#: 它把"两个判定正交"这条契约钉成可判定的断言（例如任务清单 + 机器计划、
#: 无计划书那格：``detect`` 判 ``dev``，来源却是 ``MACHINE_ONLY`` —— 如果谁把
#: 来源写成 ``detect`` 的派生，这一格立刻转红）。
#:
#: 期望值写**枚举成员名**而不是枚举对象：缺了 ``PlanSource`` 时失败要落在
#: 断言上（看得见"哪个取值不对"），而不是收集期就炸掉的 ImportError。
SOURCE_TRUTH_TABLE: List[Tuple[bool, bool, bool, str, str]] = [
    (False, False, False, "NONE", "empty"),
    (True, False, False, "NONE", "dev"),
    (False, True, False, "REVIEWED", "verification"),
    (False, True, True, "REVIEWED", "verification"),
    (True, True, False, "REVIEWED", "dev"),
    (True, True, True, "REVIEWED", "dev"),
    (True, False, True, "MACHINE_ONLY", "dev"),
    (False, False, True, "MACHINE_ONLY", "empty"),
]


def _write(path: Path, content: str = "artifact") -> None:
    path.write_text(content, encoding="utf-8")


def _make_plan_dir(
    tmp_path: Path,
    tasks_exists: bool = False,
    plan_doc_exists: bool = False,
    machine_plan_exists: bool = False,
) -> Path:
    if tasks_exists:
        _write(tmp_path / TASKS_FILENAME, "[]")
    if plan_doc_exists:
        _write(tmp_path / PLAN_DOC_FILENAME, "# 验证计划书")
    if machine_plan_exists:
        _write(tmp_path / plan_artifacts.MACHINE_PLAN_FILENAME, "{}")
    return tmp_path


def _dir_digest(plan_dir: Path) -> str:
    """目录内容（相对路径 + 字节 + mtime）的 sha256 快照。

    字节相同但被重写过（mtime 变了）也算变了 —— 所以 mtime 也进摘要。
    """
    digest = hashlib.sha256()
    for path in sorted(plan_dir.rglob("*")):
        digest.update(path.relative_to(plan_dir).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(str(path.stat().st_mtime_ns).encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# resolve_plan_source()：八格矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tasks_exists, plan_doc_exists, machine_plan_exists, expected_source, expected_branch",
    SOURCE_TRUTH_TABLE,
)
def test_resolve_plan_source_matrix(
    tmp_path: Path,
    tasks_exists: bool,
    plan_doc_exists: bool,
    machine_plan_exists: bool,
    expected_source: str,
    expected_branch: str,
) -> None:
    """八种磁盘组合 → 三种来源取值（并顺带钉住与 ``detect()`` 的正交性）。"""
    _make_plan_dir(
        tmp_path,
        tasks_exists=tasks_exists,
        plan_doc_exists=plan_doc_exists,
        machine_plan_exists=machine_plan_exists,
    )

    assert plan_artifacts.resolve_plan_source(tmp_path) is getattr(
        plan_artifacts.PlanSource, expected_source
    )
    # 来源不是 ``detect()`` 的派生：同一目录两套判定各说各的。
    assert detect(tmp_path).branch == expected_branch


def test_resolve_plan_source_on_missing_directory_is_none(tmp_path: Path) -> None:
    """目录本身不存在时不得抛错，也**不得把目录建出来** —— 判定是纯读。"""
    missing = tmp_path / "does-not-exist"

    assert plan_artifacts.resolve_plan_source(missing) is (
        plan_artifacts.PlanSource.NONE
    )
    assert not missing.exists()


def test_resolve_plan_source_is_recomputed_each_call(tmp_path: Path) -> None:
    """判定每次从磁盘重算：磁盘状态变了，答案必须当场跟着变（没有第二真源）。"""
    machine_plan = tmp_path / plan_artifacts.MACHINE_PLAN_FILENAME
    plan_doc = tmp_path / PLAN_DOC_FILENAME

    assert plan_artifacts.resolve_plan_source(tmp_path) is (
        plan_artifacts.PlanSource.NONE
    )

    _write(plan_doc, "# 验证计划书")
    assert plan_artifacts.resolve_plan_source(tmp_path) is (
        plan_artifacts.PlanSource.REVIEWED
    )

    _write(machine_plan, "{}")
    # 计划书仍在：来源不变（机器计划只是它的派生物）。
    assert plan_artifacts.resolve_plan_source(tmp_path) is (
        plan_artifacts.PlanSource.REVIEWED
    )

    plan_doc.unlink()
    assert plan_artifacts.resolve_plan_source(tmp_path) is (
        plan_artifacts.PlanSource.MACHINE_ONLY
    )


# ---------------------------------------------------------------------------
# 纯读性
# ---------------------------------------------------------------------------


def test_resolve_plan_source_is_pure(tmp_path: Path) -> None:
    """判定是纯读函数：调用前后目录的 sha256 快照必须逐字节相同。"""
    _make_plan_dir(
        tmp_path,
        tasks_exists=True,
        plan_doc_exists=True,
        machine_plan_exists=True,
    )
    before_digest = _dir_digest(tmp_path)
    before_names = sorted(p.name for p in tmp_path.iterdir())

    assert plan_artifacts.resolve_plan_source(tmp_path) is (
        plan_artifacts.PlanSource.REVIEWED
    )

    assert _dir_digest(tmp_path) == before_digest
    assert sorted(p.name for p in tmp_path.iterdir()) == before_names


# ---------------------------------------------------------------------------
# 文件名只有一个真源
# ---------------------------------------------------------------------------


def test_machine_plan_filename_single_source() -> None:
    """机器计划文件名只有一处定义：``plan_artifacts``；路由模块保留导入别名。"""
    assert plan_artifacts.MACHINE_PLAN_FILENAME == "verification_plan.json"
    # 同一个对象 —— 路由模块拿到的是导入别名，不是又抄了一份字面量。
    assert (
        routes_verification.MACHINE_PLAN_FILENAME
        is plan_artifacts.MACHINE_PLAN_FILENAME
    )

    source = Path(routes_verification.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_names = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "plan_artifacts"
        for alias in node.names
    }
    assert "MACHINE_PLAN_FILENAME" in imported_names

    # 并且路由模块没有再自己赋值一份。
    reassigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "MACHINE_PLAN_FILENAME" not in reassigned


# ---------------------------------------------------------------------------
# 文件名没有第二份抄写（AST 常量扫描）
# ---------------------------------------------------------------------------

#: 规范文件名 —— 全部定义在 :mod:`plan_artifacts`，别处不许再抄一遍。
CANONICAL_FILENAMES: Tuple[str, ...] = (
    TASKS_FILENAME,
    PLAN_DOC_FILENAME,
    MACHINE_PLAN_FILENAME,
)

#: 验证计划的两类产物名（计划书 + 机器计划）—— 整个路由层只允许
#: :mod:`plan_artifacts` 提到它们。
VERIFICATION_ARTIFACT_FILENAMES: Tuple[str, ...] = (
    PLAN_DOC_FILENAME,
    MACHINE_PLAN_FILENAME,
)


def _route_modules() -> List[Path]:
    """``backend/routes/*.py`` 下的全部路由模块。"""
    return sorted((_BACKEND_DIR / "routes").glob("*.py"))


def _docstring_constant_ids(tree: ast.AST) -> set:
    """docstring 常量节点的 id —— 散文提及不算抄写。

    docstring 是"整段字符串"，它与文件名**不等值**；但它可能**以**
    ``/verification_plan.json`` 结尾（"…写入 plans/{id}/verification_plan.json"），
    那条"以 / 加文件名结尾"的规则会误伤它，所以 docstring 显式剔除。
    """
    ids: set = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _string_constants(path: Path) -> List[Tuple[int, str]]:
    """模块里所有**代码里的**字符串常量（行号 + 值）。

    AST 而不是 grep：``#`` 注释里的散文提及不是常量，docstring 是整段字符串、
    既不与文件名等值也不是路径 —— 两者都不该被判成违规。这与仓内既有做法
    一致（``static_gates/test_state_db_path_has_one_resolver.py`` 就是 AST
    常量扫描）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_constant_ids(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _filename_offenders(path: Path, names: Tuple[str, ...]) -> List[str]:
    """``path`` 里又把 ``names`` 里的文件名抄了一遍的位置。"""
    offenders: List[str] = []
    for lineno, value in _string_constants(path):
        for name in names:
            if value == name or value.endswith("/" + name):
                offenders.append(f"{path.name}:L{lineno} {value!r}")
    return offenders


def test_routes_do_not_redeclare_canonical_artifact_filenames() -> None:
    """路由层不得再抄写规范文件名 —— 改名抄写与裸字面量都不漏检。

    :func:`test_machine_plan_filename_single_source` 只按**符号名**查重
    （``MACHINE_PLAN_FILENAME`` 有没有被重新赋值），于是两种抄写全部漏检：

    * **改名抄写** —— ``_VP_PLAN_FILENAME = "verification_plan.json"``；
    * **裸字面量** —— ``plan_dir / "verification_plan.json"``。

    这条门禁改用 AST 常量等值扫描：字符串值等于规范文件名、或以 ``/`` 加
    规范文件名结尾的常量，一律算违规。

    两条断言的分工：

    * **验证计划的两类产物名**（``verification-plan.md`` /
      ``verification_plan.json``）在整个路由层都不得再抄 —— 它们是本轮
      收敛的对象；
    * **三个规范文件名**（含 ``tasks.json``）在 :mod:`routes.verification`
      里一个都不许抄 —— 那是 VP-022 点名的文件。

    ``tasks.json`` 在**其它**路由模块（``execution`` / ``plans`` / ``phases``
    / ``fast_lane``）仍有历史抄写，那属于另一条收敛线，不在本 VP 的点名
    范围（架构决策点 3 只点名 ``routes/verification.py``）—— 一起纳入会让
    这条门禁在那边收敛完成之前一直红。
    """
    verification_route = _BACKEND_DIR / "routes" / "verification.py"

    artifact_offenders: List[str] = []
    for path in _route_modules():
        artifact_offenders.extend(
            _filename_offenders(path, VERIFICATION_ARTIFACT_FILENAMES)
        )
    assert not artifact_offenders, (
        "a route module names a verification-plan artifact file directly. "
        "Import it from plan_artifacts instead:\n  "
        + "\n  ".join(artifact_offenders)
    )

    own_offenders = _filename_offenders(verification_route, CANONICAL_FILENAMES)
    assert not own_offenders, (
        "routes/verification.py re-declares a canonical artifact filename. "
        "Import it from plan_artifacts instead:\n  " + "\n  ".join(own_offenders)
    )
