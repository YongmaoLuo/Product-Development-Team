"""Tier B 固定语料的匿名化门禁。

为什么这道门禁存在
------------------
§5.2 的做法是「一次性手写一小撮固定输入，存在测试夹具里」。这些输入随
仓库入库、进 git 历史、会被任何人读到；一旦其中一条带了一个真实的 plan
id、一条私有路径、一个邮箱或一个人名，它就跟着仓库一起公开了，而且
**再也收不回来**——公开过的文本不能靠后一次提交撤回（强制推送只删 ref，
不删对象）。

所以四种形状必须在语料入库之前就被挡住：

* **plan id**：``20\\d{6}-`` 打头、日期加 slug，那是计划目录的命名形状，
  它指向某一次具体的执行；
* **绝对路径**：以 ``/`` 开头的 POSIX 路径、盘符开头的 Windows 路径、
  home 前缀（``/Users/``、``/home/``）——它们指向某台机器上的某个位置；
* **邮箱**：邮箱里带的是名字，而名字是最难从公开文本里收回的一种标识；
* **人名**：称谓后的人名（``Dr Smith``、``张老师``）与 ``@`` 提及。

四种形状都只靠**文本**判定：门禁读不懂语料的含义，只能读它的字面。
所以它拦的是形状而不是语义——语料写「验证员」这样的角色词不是人名，
不会被拦下；语料写「张老师」是人名，会被拦下。这个分工是刻意的：语义
靠人读，形状靠机器守。

语料只读、无副作用
------------------
语料是**输入**，不是状态：导入它不写文件、不改环境、不跑外部命令，也不
在导入时算出什么需要缓存的东西。这道门禁用 AST 读它的源码来钉住这一
点——一个会写盘或跑命令的语料模块，把「读一份固定输入」变成了「跑一段
代码」，而金标要比的恰恰是前者的输出：语料一旦自己会动，它钉住的就不是
输入，而是当时的运行环境。

门禁读源码而不是读运行时状态，是刻意的选择：运行时状态只能证明「这次
导入没写盘」，源码能证明「它没有写盘这件事」，后者才是语料该有的性质。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any, Iterator

#: The backend package root — this file lives at ``backend/tests/unit/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import tests.fixtures.judgment_corpus as corpus_module  # noqa: E402
from tests.fixtures.judgment_corpus import CASES  # noqa: E402

# ---------------------------------------------------------------------------
# The forbidden shapes
# ---------------------------------------------------------------------------

#: The plan-id shape: an eight-digit date, a hyphen, then a slug.
_PLAN_ID_RE = re.compile(r"20[0-9]{6}-[A-Za-z][A-Za-z0-9._-]*")

#: A POSIX absolute path: a leading slash and at least two segments.
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s\"'(\[<])(/[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)+)")

#: A Windows absolute path: a drive letter followed by a separator.
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/]")

#: A home prefix: it names the account directory of one machine.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")

#: An email address: a local part, an ``@`` and a domain.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: A person: a title plus a name, a Chinese honorific plus a name, or an
#: ``@`` mention.  Only the *shapes* are listed — a role word on its own
#: ("验证员") says no more than the surrounding prose does.
_PERSON_NAME_RE = re.compile(
    r"(?:\b(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+[A-Z][a-z]+"
    r"|(?:先生|女士|老师|同学)\s*[一-鿿]{2,4}"
    r"|(?<![\w.+-])@[A-Za-z][A-Za-z0-9_]{2,})"
)

#: An id that is only a counter says nothing about why the row exists.
_PLACEHOLDER_ID_RE = re.compile(r"^(case|情况)\d+$")

#: A descriptive id: lower-case words joined by hyphens, at least two of
#: them.  One word is a label; two words say what the row pins.
_DESCRIPTIVE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")

#: A group name: lower-case words joined by underscores.
_GROUP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: The keys every corpus case carries.  A case may carry more — a group
#: whose judgment needs a third input can add one — but these three are
#: what makes a case readable: what it is, what went in, what came back.
_CASE_KEYS = frozenset({"id", "vp", "verdict"})

# ---------------------------------------------------------------------------
# Walking the corpus
# ---------------------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    """Every string the corpus holds, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _cases() -> Iterator[tuple[str, dict]]:
    """Every case of every group, with the group it belongs to."""
    for group_name, cases in CASES.items():
        for case in cases:
            yield group_name, case


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_corpus_has_no_plan_id_or_absolute_path() -> None:
    """The corpus carries no plan id, no absolute path, no email, no name."""
    offenders: list[str] = []
    for text in _strings(CASES):
        for pattern, label in (
            (_PLAN_ID_RE, "plan id"),
            (_ABSOLUTE_PATH_RE, "absolute path"),
            (_WINDOWS_PATH_RE, "absolute path"),
            (_HOME_PATH_RE, "home path"),
            (_EMAIL_RE, "email address"),
            (_PERSON_NAME_RE, "person name"),
        ):
            for match in pattern.finditer(text):
                offenders.append(f"{label}: {match.group(0)!r} in {text!r}")
    assert not offenders, (
        "语料里出现了真实标识 —— 语料随仓库公开，公开过就收不回来：\n"
        + "\n".join(offenders)
    )


def test_every_case_id_is_unique_and_descriptive() -> None:
    """Every case carries an id that is unique and says what it pins."""
    seen: dict[str, str] = {}
    problems: list[str] = []
    for group_name, case in _cases():
        assert isinstance(case, dict), (
            f"组 {group_name!r} 里的用例不是一个字典：{type(case).__name__}"
        )
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            problems.append(f"组 {group_name!r} 有一个用例没有 id")
            continue
        if _PLACEHOLDER_ID_RE.match(case_id):
            problems.append(
                f"组 {group_name!r} 的 id {case_id!r} 只是一个计数器："
                "它没有说这一行钉住的是什么"
            )
        if not _DESCRIPTIVE_ID_RE.match(case_id):
            problems.append(
                f"组 {group_name!r} 的 id {case_id!r} 不是描述性的 id："
                "至少两个小写词，用短横连起来"
            )
        if case_id in seen:
            problems.append(
                f"id {case_id!r} 出现了两次（{seen[case_id]} 与 {group_name!r}）："
                "id 是读者找到用例的办法"
            )
        else:
            seen[case_id] = group_name
        if _CASE_KEYS - set(case):
            problems.append(
                f"组 {group_name!r} 的用例 {case_id!r} 少了键："
                f"{sorted(_CASE_KEYS - set(case))}"
            )
    assert not problems, "\n".join(problems)


def test_corpus_groups_are_non_empty() -> None:
    """Every group is named, and holds at least one case."""
    assert isinstance(CASES, dict), (
        f"CASES 不是一个字典：{type(CASES).__name__}"
    )
    assert CASES, "语料是空的：没有组就没有固定输入"
    empty: list[str] = []
    for group_name, cases in CASES.items():
        if not isinstance(group_name, str) or not _GROUP_NAME_RE.match(group_name):
            empty.append(f"组名 {group_name!r} 不是小写下划线形状")
        if not isinstance(cases, list) or not cases:
            empty.append(f"组 {group_name!r} 没有用例")
    assert not empty, "\n".join(empty)

    assert "evidence" in CASES, "语料里没有 evidence 组：命令证据是判定面的起点"
    assert len(CASES["evidence"]) >= 8, (
        f"evidence 组只有 {len(CASES['evidence'])} 条用例，"
        "不够覆盖命令证据的分支"
    )


def test_corpus_is_read_only_and_import_has_no_side_effects() -> None:
    """The corpus declares data and nothing else."""
    source = Path(corpus_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                problems.append(f"第 {node.lineno} 行导入了 {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module != "__future__":
                names = ", ".join(alias.name for alias in node.names)
                problems.append(f"第 {node.lineno} 行导入了 {module!r}（{names}）")

    forbidden_calls = {
        "open",
        "mkdir",
        "makedirs",
        "write_text",
        "write_bytes",
        "unlink",
        "remove",
        "rmtree",
        "rename",
        "replace",
        "system",
        "popen",
        "Popen",
        "run",
        "check_output",
        "check_call",
        "getcwd",
        "chdir",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in forbidden_calls:
            problems.append(f"第 {node.lineno} 行调用了 {name}()：语料只声明数据")

    assert not problems, "\n".join(problems)
