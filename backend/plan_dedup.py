"""验证点重复检测：命令归一化、「命令相同」与「覆盖面 + 方法 + 标题」判据
（2026-11-08, R1）；空泛验收判据标记（2026-11-09, R2）。

这个模块解决什么
----------------
PDT 生成验证计划后**不做重复检测**。一份 import.json 里，VP-022
（【全量】Nightly CI）与 VP-025（【云端】整仓门禁）跑的是同一条
``bash scripts/ci_gate.sh`` —— 两条重复的整仓门禁照常出厂，前一条白跑
一整轮 Nightly CI（实测代价以小时计），而没有任何东西会提醒操作员。

判据
----
两条判据，**任一成立**即判为重复。判据刻意做得很窄，因为**误报比漏报更贵**：
把两条覆盖面其实不同的验证点判成重复，会把一条该跑的关卡删掉。

判据 1：命令相同 → 重复
~~~~~~~~~~~~~~~~~~~~~~~~
* **归一化**（:func:`normalize_command`）—— 去首尾空白、连续空白合并成一个、
  反斜杠统一为 ``/``。同一条命令的两种写法（``bash  scripts/ci_gate.sh`` 与
  `` bash scripts/ci_gate.sh ``）必须先合成一个，才谈得上"相同"。
* **命令字段一个都不能漏**（:data:`COMMAND_FIELDS`）——
  ``test_command`` / ``commands`` / ``focused_test_command`` / ``ci_entry`` /
  ``entry_command``。VP 自 2026-09-18 起没有 ``test_command``，整仓门禁只落在
  ``ci_entry``，command 类 e2e 只落在 ``entry_command``
  （``verification_ci_runner.ENTRY_COMMAND_FIELD``）；漏掉任一个字段，都有一
  类重复检不出来 —— 这不是假想，是"整仓门禁跑了两次"这件事本身。
* **比较的是命令**序列**（:func:`commands_of`）**，顺序敏感。
  ``["pytest a"]`` 与 ``["pytest b", "pytest a"]`` 跑的东西不一样：多出来的
  那一条有它自己的覆盖面，不是同一个东西的两种写法。

判据 2：覆盖面相同 + 方法相同 + 标题相似 → 重复
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
判据 1 抓不到的重复有另一半：两条验证点跑**不同的命令**，但它们验的是同一片
代码、用的是同一种验法 —— 是同一件事被写了两遍，而不是两件事。

* **覆盖面**（:func:`normalize_covers`）—— ``covers_paths`` 归一化后是**集合**
  （顺序无关、重复无关）：``backend/auth/`` 与 ``backend/auth`` 是同一片代码。
* **方法**（:func:`method_of`）—— 读 ``verification_method``，缺失时回落
  ``method``。同一片代码，API 测试与端到端测试是两件事。
* **标题相似**（:func:`titles_similar`）—— 归一化后相同、互为子串，或**词序
  不同但用词相同**。中文标题的语序会飘（``验证登录流程`` 与 ``登录流程验证``
  说的是一件事），只认逐字相同会漏掉最常见的一类重复。

组间互斥
--------
**同一个验证点不得出现在两个组里** —— 一条 id 出现在两个组，意味着"删掉这一条"
会得到两种不同的处置结果，读报告的人无从下手。规则是**命令组优先**：判据 1
先成组并占住自己的 id，判据 2 不再收这些 id。

边界
----
* 无命令字段、或归一化后为空 → **不参与判重**（不是"空命令的重复"）。
* ``covers_paths`` 归一化后为空 → **判据 2 不参与**（R5 第 3 条）。空覆盖面
  不足以支撑判定，标题再像也不能判重。
* 命令相同、但两条的覆盖面**明显冲突**（都非空、互不相交、且不是同一片代码里
  的两个兄弟模块）、标题也不同 → 这两条**不是同一件事**，不因命令相同成组
  （R5 例外，见下）。
* 标题归一化后为空 → **不做标题判定**：只剩标点的标题不携带任何身份信息。
* 非 dict 条目、缺 id 的条目 → 跳过，**不抛异常**。判据跑在别人写的
  JSON 上，输入不规矩是常态而不是事故；一份脏数据不该让整轮计划生成崩在
  最后一步。

本模块是**纯函数**：无 IO、无网络、无全局状态。所以判据本身没有"环境不对"
这种失败模式 —— 红灯只可能来自判据写错了。

R5 的例外
---------
R5 点名三条**不得**误伤的用例。命令判据（判据 1）里有一条例外与它同源
（:func:`_r5_command_exception_applies`）：

* 两条 ``covers_paths`` 归一化后**都非空**、**互不相交**、且**不是同一个父
  目录下的兄弟目录**（:func:`_covers_conflict`），同时标题归一化后**不相等**
  （:func:`_titles_differ`）→ 这两条**不是同一件事**，不因命令相同成组。同一
  入口脚本用于两个不同场景（后端整仓门禁 vs 前端整仓门禁）是常见写法，把它
  判成重复等于让操作员删掉其中一条该跑的关卡 —— 误伤比漏报贵。
* 一侧覆盖面为空 → 例外**不触发**（仍按命令判重）：空覆盖面不足以支撑任何
  判定，与 R5 第 3 条同一条道理。
* 标题口径只认**归一化后不相等**：``前端整仓门禁`` 与 ``整仓门禁`` 说的是两
  件事（一个是前端门禁、一个是后端门禁），后者恰好是前者的子串 —— 覆盖面那
  一条已经能区分它们，标题不必再重复把关。

例外**按对**判定：同一个命令的桶里，每一条各自跟所在簇的**锚点**比。被例外
排除的验证点说的是另一件事，它**不进组**；桶里真正的重复（同一条命令跑在同一
片代码上）照常成组 —— 例外不能把判据 1 废掉，也不能因为桶里混进了一条被排除
的就把整桶废掉。

范围
----
本模块按上面两条判据**找出**重复的组，并在命令判据上内建 R5 例外。

空泛判据（R2）
--------------
重复检测只回答"这两条是不是同一件事"，不回答"这条判据写得够不够"。R2 要把
**空泛的验收判据**（``expected_result``）单独标记成 ``vague_criterion``，与
重复组一起进报告 —— 此前**没有任何检查**，「验证通过」这种四个字照常出厂：
它没说怎么算通过，跑挂了也没人拦。

* **占位词**（:data:`PLACEHOLDER_EXPECTED_RESULTS`）—— 转小写后**精确匹配**。
  精确匹配而不是包含：「全部通过」不是占位词（但它太短，会被 ``too_short``
  抓到）。
* **太短** —— 归一化后不超过
  :data:`MAX_DESCRIPTIVE_EXPECTED_LENGTH` 个字，说不清怎么算通过。
* **占位词优先于「太短」** —— 「无」只有一个字，但它是被点名过的占位词，
  报 ``placeholder`` 而不是 ``too_short``。

判据同样**只抓明显空泛的**：误报比漏报贵 —— 把一条说清楚了怎么算通过的判据
标成空泛，会让人白改一遍（见 ``vague_criterion`` 的
``too_short`` / ``placeholder`` 两个 reason）。

任务级重复检测（R4）
--------------------
**同一套规则用于 ``tasks.json`` 的任务**（:func:`detect_duplicate_tasks`）：
两条任务的 ``files_to_modify`` **集合**相同、且测试命令归一化后**序列**相同 →
判重复。

* **覆盖面是集合** —— ``["a.py", "b.py"]`` 与 ``["b.py", "a.py"]`` 指的是同
  一片文件，是同一个东西的两种写法。
* **命令字段两个都要读**（:data:`TASK_COMMAND_FIELDS`）—— ``test_commands``
  （数组，仓库的唯一形式）与 ``test_command``（字符串，历史计划与手工编辑
  过的计划里还有）。只读一个，就有一类重复检不出来。
* **空覆盖面不支撑判定** —— ``files_to_modify`` 为空、缺失、或只含哨兵
  :data:`UNKNOWN_MODIFICATIONS_SENTINEL`（"作者没列出改哪些文件"）时，两条任务
  "改一样的文件"这件事根本没被声明过，不能据此判重（同 R5 第 3 条的道理）。

报告组装（R3 / R4）
-----------------
本模块末尾的 :func:`build_verification_report` 与 :func:`build_tasks_report`
把上面几个检测函数的结果组装成**报告**：重复组 + 空泛判据 + 一行摘要。

这两个函数同样**不碰文件系统** —— 落盘与开关在
``backend/plan_dedup_report.py`` 里。分开是有意的：判据与组装是纯函数，
写不写、写到哪、要不要写是策略，两者的失败模式不一样，混在一起就没法
单独测其中一边。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping as _MappingABC
from typing import Any, Dict, FrozenSet, List, Mapping, Sequence, Set, Tuple

#: 一条验证点可能把命令写在哪些字段里。
#:
#: 顺序固定（:func:`commands_of` 按这个顺序取），这样同一个验证点无论字段
#: 声明顺序如何，读出来的命令序列都一样 —— 判重不能受 JSON 里键的顺序影响。
#:
#: ``test_command`` 保留在列表里：VP 自 2026-09-18 起不再有这个字段，但历史
#: 计划、旧文档与手工编辑过的计划里还有；漏掉它，那一类重复照样检不出来。
COMMAND_FIELDS: Tuple[str, ...] = (
    "test_command",
    "commands",
    "focused_test_command",
    "ci_entry",
    "entry_command",
)

#: 重复组的 ``reason`` 前缀。带上前缀是为了让读报告的人一眼看出**为什么**
#: 这两条被判成重复（而不是"系统说它们重复"）。
REASON_PREFIX = "identical_command"

#: 判据 2 的 ``reason`` 前缀。
REASON_PREFIX_COVERS = "same_covers_and_method"

#: 连续空白（含制表符、换行）。
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_command(text: Any) -> str:
    """把一条命令归一化成"跑起来一样就是同一个"的写法。

    三步，顺序固定：

    1. 反斜杠统一为 ``/``（``scripts\\ci_gate.sh`` → ``scripts/ci_gate.sh``）；
    2. 连续空白合并成一个空格；
    3. 去首尾空白。

    非字符串输入（含 ``None``）返回空串 —— 归一化后为空即"没有命令"，
    不参与判重。

    >>> normalize_command(" bash  a   b ")
    'bash a b'
    """
    if not isinstance(text, str):
        return ""
    return _WHITESPACE_RE.sub(" ", text.replace("\\", "/")).strip()


def _command_values(raw: Any) -> List[str]:
    """把一个命令字段的原始值摊成一串字符串。

    字段可能是单条命令（字符串），也可能是一串命令（``commands`` 列表）。
    其它形态（``None``、数字、字典）没有可执行的含义，忽略。
    """
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, str)]
    return []


def commands_of(point: Mapping[str, Any]) -> Tuple[str, ...]:
    """一条验证点声明的全部命令（归一化后、非空、去重保序）。

    按 :data:`COMMAND_FIELDS` 的固定顺序取各字段，字段内部保持原有顺序 ——
    命令**序列**的顺序是判重的一部分（见模块文档）。

    同一个命令在一条验证点里出现多次（例如同时落在 ``ci_entry`` 与
    ``entry_command``）只算一次：那是同一条命令被声明了两遍，不是两遍命令。
    """
    if not isinstance(point, _MappingABC):
        return ()

    collected: List[str] = []
    for field in COMMAND_FIELDS:
        try:
            raw = point.get(field)
        except AttributeError:  # pragma: no cover - 防御性：非 Mapping 的怪对象
            return ()
        for value in _command_values(raw):
            normalized = normalize_command(value)
            if normalized and normalized not in collected:
                collected.append(normalized)
    return tuple(collected)


# ---------------------------------------------------------------------------
# 判据 2：覆盖面 + 方法 + 标题
# ---------------------------------------------------------------------------


def normalize_title(text: Any) -> str:
    """把标题归一化成"说的是同一件事就是同一个"的写法。

    两步，顺序固定：

    1. 去掉所有空白与标点（标点按 Unicode 的标点类别判定，中英文一视同仁 ——
       ``【】``、``，``、``。`` 都是标点）；
    2. 转小写。

    非字符串输入（含 ``None``）返回空串 —— 归一化后为空即"这个标题不携带任何
    身份信息"，不参与标题判定。

    >>> normalize_title("【全量】Nightly CI。")
    '全量nightlyci'
    """
    if not isinstance(text, str):
        return ""
    kept = [
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    ]
    return "".join(kept).lower()


def _normalize_path(value: str) -> str:
    """把一条覆盖面路径归一化。

    三步：去首尾空白、反斜杠统一为 ``/``、去尾部 ``/``。归一化后为空即
    "这条路径不说明任何范围"，丢弃。
    """
    text = value.strip().replace("\\", "/")
    while text.endswith("/"):
        text = text[:-1]
    return text


def normalize_covers(point: Mapping[str, Any]) -> FrozenSet[str]:
    """一条验证点声明的覆盖面（归一化后的集合）。

    ``covers_paths`` 是一个**集合**：顺序无关、重复无关 —— ``backend/auth/``
    与 ``backend/auth`` 指的是同一片代码，只是写法不同。

    归一化后为空集合即"这条验证点没有声明范围"，判据 2 不参与（见模块文档）。
    """
    if not isinstance(point, _MappingABC):
        return frozenset()
    try:
        raw = point.get("covers_paths")
    except AttributeError:  # pragma: no cover - 防御性：非 Mapping 的怪对象
        return frozenset()

    if isinstance(raw, str):
        values: List[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = []

    normalized = {
        path
        for path in (_normalize_path(value) for value in values if isinstance(value, str))
        if path
    }
    return frozenset(normalized)


def method_of(point: Mapping[str, Any]) -> str:
    """一条验证点声明的验证方法。

    读 ``verification_method``，缺失（或只有空白）时回落 ``method`` —— 两个
    字段在历史计划里都出现过。都没有则返回空串。
    """
    if not isinstance(point, _MappingABC):
        return ""
    for field in ("verification_method", "method"):
        try:
            value = point.get(field)
        except AttributeError:  # pragma: no cover - 防御性：非 Mapping 的怪对象
            return ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def titles_similar(left: Any, right: Any) -> bool:
    """两个标题是不是在说同一件事。

    归一化后满足**任一**一条即算相似：

    1. 逐字相同；
    2. 一个是另一个的子串（``验证登录流程`` 与 ``验证登录流程完整``）；
    3. 用词相同、只是语序不同（``验证登录流程`` 与 ``登录流程验证``）。
       中文标题的语序会飘，只认前两条会漏掉最常见的一类重复。

    任一侧归一化后为空 → **不相似**：只剩标点的标题不携带任何身份信息，
    把两条这样的标题判成"说的是同一件事"是纯误报。
    """
    first = normalize_title(left)
    second = normalize_title(right)
    if not first or not second:
        return False
    if first == second:
        return True
    if first in second or second in first:
        return True
    return sorted(first) == sorted(second)


# ---------------------------------------------------------------------------
# R5：命令判据的例外
# ---------------------------------------------------------------------------


def _titles_differ(left: Any, right: Any) -> bool:
    """两个标题说的是**不是同一件事**（R5 的标题口径）。

    归一化后**不相等**即"不是同一件事"。

    这里**不**要求"互不为子串"：``前端整仓门禁`` 与 ``整仓门禁`` 说的是两件事
    （一个是前端门禁、一个是后端门禁），后者恰好是前者的子串 —— 把它们判成
    "同一件事的两种写法"，等于让操作员删掉其中一条该跑的关卡。覆盖面那一条
    （明显不同的范围）已经能区分它们，标题不必再重复把关。

    任一侧归一化后为空 → **不算"不同"**：只剩标点的标题不携带任何身份信息，
    拿它断言"两条说的是两件事"是凭空推断。
    """
    first = normalize_title(left)
    second = normalize_title(right)
    if not first or not second:
        return False
    return first != second


def _cover_parent(path: str) -> str:
    """一条覆盖面路径的**父目录**；顶层目录（``backend``）的父目录是仓库根
    （空串）。"""
    head, separator, _tail = path.rpartition("/")
    return head if separator else ""


def _share_parent_directory(
    left_covers: FrozenSet[str],
    right_covers: FrozenSet[str],
) -> bool:
    """两条覆盖面里有没有**同一个父目录下的两个兄弟目录**。

    ``backend/auth`` 与 ``backend/user`` 住在同一个父目录 ``backend`` 下 ——
    它们是同一片代码里的两个模块，不是"明显不同的两件事"：拿"覆盖面不相交"
    证明它们是两件事，证据不足（仓库里同一条命令同时覆盖多个模块是常见写法）。

    仓库根（空串）**不算**父目录：``backend`` 与 ``frontend`` 都在根下，但它们
    是两片完全不同的代码 —— 正是 R5 要放过的那一类。
    """
    return any(
        _cover_parent(one) == _cover_parent(other) != ""
        for one in left_covers
        for other in right_covers
    )


def _covers_conflict(
    left_covers: FrozenSet[str],
    right_covers: FrozenSet[str],
) -> bool:
    """两条覆盖面是不是**明显冲突**（R5 的覆盖面口径）。

    三条**同时**成立才算冲突：

    1. 两条覆盖面都**非空**；
    2. **没有一条路径相同**（归一化后不相交）；
    3. **不是**同一个父目录下的兄弟目录（:func:`_share_parent_directory`）。
    """
    if not left_covers or not right_covers:
        return False
    if left_covers & right_covers:
        return False
    return not _share_parent_directory(left_covers, right_covers)


def _r5_command_exception_applies(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """R5 例外：两条明明是不同的事，却因为命令相同被判成重复。

    三条**同时**成立才触发：

    1. 两条的 ``covers_paths`` 归一化后**都非空**；
    2. 两个覆盖面**明显冲突**（:func:`_covers_conflict`）—— 互不相交，
       且不是同一片代码里的两个兄弟模块；
    3. 标题归一化后**不相等**（:func:`_titles_differ`）。

    一侧覆盖面为空 → 例外**不触发**（仍按命令判重）：空覆盖面不足以支撑任何
    判定，与 R5 第 3 条同一条道理 —— "没写范围"不能拿来证明"是两件事"，
    也不能拿来证明"是同一件事"。

    本函数判的是**一对**验证点，不是整个命令桶 —— 桶里既有真重复、又有被
    排除的，两边都要如实反映（见 :func:`detect_duplicate_points`）。
    """
    if not _covers_conflict(normalize_covers(left), normalize_covers(right)):
        return False
    return _titles_differ(left.get("title"), right.get("title"))


def detect_duplicate_points(
    points: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """找出重复的验证点，返回重复组。

    两条判据，任一成立即判为重复：

    1. **命令相同** —— :func:`commands_of` 归一化后的命令序列一致；
    2. **覆盖面相同 + 方法相同 + 标题相似** —— ``covers_paths`` 归一化后是
       同一个非空集合，验证方法相同，标题按 :func:`titles_similar` 相似。

    返回形如::

        [{"reason": "identical_command: bash scripts/ci_gate.sh",
          "member_ids": ["VP-022", "VP-025"]},
         {"reason": "same_covers_and_method: covers_paths=['backend/auth'] "
                    "method=api_test",
          "member_ids": ["VP-003", "VP-004"]}]

    * 只有**一个**成员的组不成组（没有重复可言）。
    * 成员按输入顺序排列，``member_ids`` 里的 id 去重。
    * **同一个 id 不会出现在两个组里**：命令组先成组并占住自己的 id，判据 2
      不再收这些 id（见模块文档的"组间互斥"）。
    * 组按**首个成员在输入中的位置**排序 —— 报告读起来跟输入顺序一致。
    * 没有命令、或命令归一化后为空的验证点不参与判据 1；覆盖面为空的验证点
      不参与判据 2。
    * **R5 例外** —— 命令相同、但两条的覆盖面**明显冲突**（都非空、互不相交、
      且不是同一片代码里的两个兄弟模块）、标题也不同，这两条不是同一件事
      （:func:`_r5_command_exception_applies`）。例外**按对**判定：被排除的
      验证点不进组，同一个命令桶里的真重复照常成组。
    * 非 dict 条目、缺 id（或 id 为空）的条目跳过，不抛异常。
    """
    usable: List[Tuple[int, str, Mapping[str, Any]]] = []
    for index, point in enumerate(points or ()):
        if not isinstance(point, _MappingABC):
            continue
        raw_id = point.get("id")
        point_id = str(raw_id).strip() if raw_id is not None else ""
        if not point_id:
            continue
        usable.append((index, point_id, point))

    # (首个成员在输入中的位置, 组) —— 最后按位置排序。
    groups: List[Tuple[int, Dict[str, Any]]] = []
    # 已经被某个组收下的 id。
    claimed: Set[str] = set()

    # --- 判据 1：命令相同 -------------------------------------------------
    command_buckets: Dict[Tuple[str, ...], List[Tuple[int, str, Mapping[str, Any]]]] = {}
    for index, point_id, point in usable:
        commands = commands_of(point)
        if not commands:
            continue
        members = command_buckets.setdefault(commands, [])
        if all(existing_id != point_id for _i, existing_id, _p in members):
            members.append((index, point_id, point))

    for commands, members in command_buckets.items():
        free = [
            (index, pid, point)
            for index, pid, point in members
            if pid not in claimed
        ]
        if len(free) < 2:
            continue
        # R5 例外**按对**判定：桶里每一条各自跟**簇的锚点**比 —— 被例外排除的
        # 验证点说的是另一件事，它不进组；但桶里真正的重复（同一条命令跑在同一
        # 片代码上）照常成组 —— 例外不能把判据 1 废掉，也不能把整桶废掉。
        #
        # 不用传递闭包（与判据 2 同一条规矩）：A 与 B 像、B 与 C 像，不代表
        # A 与 C 像，把三者并成一组是判据自己没说过的话。
        clusters: List[Dict[str, Any]] = []
        for index, point_id, point in free:
            target = next(
                (
                    cluster
                    for cluster in clusters
                    if not _r5_command_exception_applies(cluster["point"], point)
                ),
                None,
            )
            if target is None:
                clusters.append({"index": index, "ids": [point_id], "point": point})
            else:
                target["ids"].append(point_id)

        for cluster in clusters:
            if len(cluster["ids"]) < 2:
                # 落单的（被 R5 排除的，或本来就只有一个）没有重复可言 ——
                # 它的 id 也不占住，判据 2 仍可收它。
                continue
            groups.append((
                cluster["index"],
                {
                    "reason": f"{REASON_PREFIX}: " + "; ".join(commands),
                    "member_ids": list(cluster["ids"]),
                },
            ))
            claimed.update(cluster["ids"])

    # --- 判据 2：覆盖面相同 + 方法相同 + 标题相似 -------------------------
    cover_buckets: Dict[Tuple[FrozenSet[str], str], List[Tuple[int, str, str]]] = {}
    for index, point_id, point in usable:
        if point_id in claimed:
            continue
        covers = normalize_covers(point)
        if not covers:
            continue
        method = method_of(point)
        raw_title = point.get("title")
        title = raw_title if isinstance(raw_title, str) else ""
        cover_buckets.setdefault((covers, method), []).append((index, point_id, title))

    for (covers, method), members in cover_buckets.items():
        # 同一组里按"与首个成员标题相似"聚簇：簇的锚点是它的第一个成员。
        # 不用传递闭包 —— A 与 B 像、B 与 C 像，不代表 A 与 C 像，把三者并成
        # 一组是判据自己没说过的话。
        clusters: List[Dict[str, Any]] = []
        for index, point_id, title in members:
            if point_id in claimed:
                continue
            target = next(
                (cluster for cluster in clusters if titles_similar(cluster["title"], title)),
                None,
            )
            if target is None:
                clusters.append({"index": index, "ids": [point_id], "title": title})
            elif point_id not in target["ids"]:
                target["ids"].append(point_id)

        for cluster in clusters:
            free = [pid for pid in cluster["ids"] if pid not in claimed]
            if len(free) < 2:
                continue
            groups.append((
                cluster["index"],
                {
                    "reason": (
                        f"{REASON_PREFIX_COVERS}: "
                        f"covers_paths={sorted(covers)!r} method={method}"
                    ),
                    "member_ids": free,
                },
            ))
            claimed.update(free)

    groups.sort(key=lambda item: item[0])
    return [group for _index, group in groups]


# ---------------------------------------------------------------------------
# R2：空泛验收判据
# ---------------------------------------------------------------------------

#: 空泛判据的 ``reason`` 前缀。
#:
#: 与重复组的 ``reason`` 前缀同一套写法：读报告的人一眼能看出**为什么**这条
#: 被挑出来 —— 是"空泛"，不是"重复"。
REASON_PREFIX_VAGUE = "vague_criterion"

#: 占位词（归一化后转小写**精确匹配**）。
#:
#: 这些词只声明"过了就算过"，没说怎么算过；「待定」「待补充」更是连判据都
#: 没写。精确匹配而不是包含：「全部通过」不是占位词 —— 但它太短，会被
#: ``too_short`` 抓到，不会漏掉。
PLACEHOLDER_EXPECTED_RESULTS = frozenset(
    {"验证通过", "待定", "待补充", "无", "通过", "ok"}
)

#: 归一化后的判据长度不超过这个值 → 太短，说不清怎么算通过。
MAX_DESCRIPTIVE_EXPECTED_LENGTH = 6


def normalize_expected_result(text: Any) -> str:
    """把一条验收判据归一化成"说的是同一件事就是同一个"的写法。

    两步，顺序固定：

    1. 去掉所有空白与标点（标点按 Unicode 的标点类别判定，中英文一视同仁 ——
       ``，``、``。``、``!`` 都是标点）；
    2. **不转小写** —— 转小写只发生在占位词比对时（见
       :data:`PLACEHOLDER_EXPECTED_RESULTS`），报告里回显的仍是原文。

    非字符串输入（含 ``None``、数字）返回空串 —— 归一化后为空即"没有判据"。

    >>> normalize_expected_result(" 整仓门禁退出码 0，全部测试通过 ")
    '整仓门禁退出码0全部测试通过'
    """
    if not isinstance(text, str):
        return ""
    kept = [
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    ]
    return "".join(kept)


def detect_vague_criteria(
    points: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """找出空泛的验收判据（``expected_result``），逐条返回。

    两条判据，**任一**成立即判为空泛：

    1. **占位词** —— 归一化后转小写精确命中
       :data:`PLACEHOLDER_EXPECTED_RESULTS`（「验证通过」「待定」「OK」…）；
    2. **太短** —— 归一化后长度不超过
       :data:`MAX_DESCRIPTIVE_EXPECTED_LENGTH` 个字（含空串）。

    返回形如::

        [{"id": "VP-024", "reason": "vague_criterion: placeholder",
          "expected_result": "验证通过"}]

    * **占位词优先于「太短」** —— 「无」只有一个字，但它是被点名过的占位词，
      报 ``placeholder`` 而不是 ``too_short``。
    * ``expected_result`` 回显**原文**（不是归一化后的串）—— 报告要让人看到
      计划里实际写的是什么，而不是归一化后的样子。
    * 缺 ``expected_result`` 字段（或不是字符串）→ 判空泛，``reason`` 是
      ``too_short``，``expected_result`` 回显空串。
    * 非 dict 条目、缺 id（或 id 为空）的条目跳过，**不抛异常**；空列表
      返回 ``[]``。
    """
    findings: List[Dict[str, Any]] = []
    for point in points or ():
        if not isinstance(point, _MappingABC):
            continue
        raw_id = point.get("id")
        point_id = str(raw_id).strip() if raw_id is not None else ""
        if not point_id:
            continue

        raw_expected = point.get("expected_result")
        expected_result = raw_expected if isinstance(raw_expected, str) else ""
        normalized = normalize_expected_result(expected_result)

        if normalized.lower() in PLACEHOLDER_EXPECTED_RESULTS:
            reason = f"{REASON_PREFIX_VAGUE}: placeholder"
        elif len(normalized) <= MAX_DESCRIPTIVE_EXPECTED_LENGTH:
            reason = f"{REASON_PREFIX_VAGUE}: too_short"
        else:
            continue

        findings.append(
            {
                "id": point_id,
                "reason": reason,
                "expected_result": expected_result,
            }
        )
    return findings


# ---------------------------------------------------------------------------
# R4：任务级重复检测
# ---------------------------------------------------------------------------

#: 任务级重复组的 ``reason`` 前缀。
#:
#: 与验证点那套前缀同一套写法：读报告的人一眼能看出**为什么**这两条任务被判
#: 成重复 —— 是"覆盖面一样 + 命令一样"，不是"系统说它们重复"。
REASON_PREFIX_TASK_FILES = "identical_files_and_command"

#: 任务声明"作者没列出改哪些文件"时用的占位哨兵。
#:
#: 哨兵**不是文件路径** —— 两条任务都写这个哨兵，"改一样的文件"这件事根本没
#: 被声明过，不能据此判重（同 R5 第 3 条：空覆盖面不足以支撑判定）。
UNKNOWN_MODIFICATIONS_SENTINEL = "__UNKNOWN_MODIFICATIONS__"

#: 任务声明测试命令时可能用到的字段。
#:
#: 顺序固定（:func:`_task_commands` 按这个顺序取），这样同一个任务无论字段
#: 声明顺序如何，读出来的命令序列都一样 —— 判重不能受 JSON 里键的顺序影响。
#:
#: ``test_commands`` 是仓库的**唯一形式**（``tasks_generator`` 强制「一律写
#: ``test_commands: [...]``」），但 ``test_command`` 单数字段仍要读：历史计划、
#: 手工编辑过的计划、以及 ``Task.get_test_commands()`` 的回落分支都还有它。
#: 漏掉任一个字段，都有一类重复检不出来。
TASK_COMMAND_FIELDS: Tuple[str, ...] = ("test_command", "test_commands")


def _normalize_task_path(value: str) -> str:
    """把一条 ``files_to_modify`` 路径归一化。

    先按 :func:`normalize_command` 的口径归一（去首尾空白、连续空白合并成一个、
    反斜杠统一为 ``/``），再去掉尾部 ``/`` —— ``backend/auth/`` 与
    ``backend/auth`` 指的是同一片文件。归一化后为空即"这条路径不说明任何范围"，
    丢弃。
    """
    return _normalize_path(normalize_command(value))


def _task_files(task: Mapping[str, Any]) -> FrozenSet[str]:
    """一条任务声明的覆盖面（归一化后的**集合**）。

    ``files_to_modify`` 是一个集合：顺序无关、重复无关 ——
    ``["backend/a.py", "backend/b.py"]`` 与 ``["backend/b.py", "backend/a.py"]``
    指的是同一片文件。

    哨兵 :data:`UNKNOWN_MODIFICATIONS_SENTINEL` **不是文件路径**，从集合里剔除；
    剔除后为空即"这条任务没声明覆盖面"，不参与判重。
    """
    try:
        raw = task.get("files_to_modify")
    except AttributeError:  # pragma: no cover - 防御性：非 Mapping 的怪对象
        return frozenset()

    if isinstance(raw, str):
        values: List[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = []

    normalized = {
        path
        for path in (_normalize_task_path(value) for value in values if isinstance(value, str))
        if path and path != UNKNOWN_MODIFICATIONS_SENTINEL
    }
    return frozenset(normalized)


def _task_commands(task: Mapping[str, Any]) -> Tuple[str, ...]:
    """一条任务声明的全部测试命令（归一化后、非空、去重保序）。

    按 :data:`TASK_COMMAND_FIELDS` 的固定顺序取各字段，字段内部保持原有顺序 ——
    命令**序列**的顺序是判重的一部分。``["pytest a"]`` 与
    ``["pytest b", "pytest a"]`` 跑的东西不一样。

    同一条命令在一条任务里出现两次（例如同时落在 ``test_command`` 与
    ``test_commands``）只算一次：那是同一条命令被声明了两遍，不是两遍命令。
    """
    collected: List[str] = []
    for field in TASK_COMMAND_FIELDS:
        try:
            raw = task.get(field)
        except AttributeError:  # pragma: no cover - 防御性：非 Mapping 的怪对象
            return ()
        for value in _command_values(raw):
            normalized = normalize_command(value)
            if normalized and normalized not in collected:
                collected.append(normalized)
    return tuple(collected)


def detect_duplicate_tasks(
    tasks: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """找出重复的任务，返回重复组。

    判据（R4）：两条任务的 ``files_to_modify`` **集合**相同、且测试命令归一化后
    的**序列**相同 → 判为重复。

    返回形如::

        [{"reason": "identical_files_and_command: 2 file(s) + "
                    "pytest backend/tests/unit/test_a.py -v",
          "member_ids": ["1", "2"]}]

    * 只有**一个**成员的组不成组（没有重复可言）。
    * 成员按输入顺序排列，``member_ids`` 里的 id 去重。
    * **同一个 id 不会出现在两个组里** —— 一个 id 出现在两个组里，意味着"删掉
      这一条"会得到两种不同的处置结果，读报告的人无从下手。
    * 组按**首个成员在输入中的位置**排序 —— 报告读起来跟输入顺序一致。
    * ``files_to_modify`` 为空、缺失、或只含哨兵
      :data:`UNKNOWN_MODIFICATIONS_SENTINEL` → **不参与判重**（空覆盖面不足以
      支撑判定）。
    * 命令为空、缺失或归一化后为空 → **不参与判重**。
    * 非 dict 条目、缺 id（或 id 为空）的条目跳过，不抛异常。
    """
    usable: List[Tuple[int, str, Mapping[str, Any]]] = []
    for index, task in enumerate(tasks or ()):
        if not isinstance(task, _MappingABC):
            continue
        raw_id = task.get("id")
        task_id = str(raw_id).strip() if raw_id is not None else ""
        if not task_id:
            continue
        usable.append((index, task_id, task))

    # (覆盖面, 命令序列) —— 两条都相同才成组。
    buckets: Dict[Tuple[FrozenSet[str], Tuple[str, ...]], List[Tuple[int, str]]] = {}
    for index, task_id, task in usable:
        files = _task_files(task)
        if not files:
            continue
        commands = _task_commands(task)
        if not commands:
            continue
        members = buckets.setdefault((files, commands), [])
        if all(existing_id != task_id for _i, existing_id in members):
            members.append((index, task_id))

    # (首个成员在输入中的位置, 组) —— 最后按位置排序。
    groups: List[Tuple[int, Dict[str, Any]]] = []
    # 已经被某个组收下的 id。
    claimed: Set[str] = set()

    for (files, commands), members in buckets.items():
        free = [(index, pid) for index, pid in members if pid not in claimed]
        if len(free) < 2:
            continue
        groups.append((
            free[0][0],
            {
                "reason": (
                    f"{REASON_PREFIX_TASK_FILES}: {len(files)} file(s) + "
                    + "; ".join(commands)
                ),
                "member_ids": [pid for _i, pid in free],
            },
        ))
        claimed.update(pid for _i, pid in free)

    groups.sort(key=lambda item: item[0])
    return [group for _index, group in groups]


# ---------------------------------------------------------------------------
# R3 / R4：报告组装（纯函数，不做 IO）
# ---------------------------------------------------------------------------


def _unique_ids(ids: Sequence[Any]) -> List[str]:
    """把一串 id 去重并保持原序。

    去重是必须的：同一个 id 报两遍，只会让操作员以为有两个问题。
    """
    collected: List[str] = []
    for raw_id in ids:
        text = str(raw_id).strip() if raw_id is not None else ""
        if text and text not in collected:
            collected.append(text)
    return collected


def summarize_findings(
    duplicate_groups: Sequence[Mapping[str, Any]],
    vague_criteria: Sequence[Mapping[str, Any]] = (),
) -> str:
    """把两类发现压成**一行**摘要。

    形如::

        1 duplicate group(s): VP-022, VP-025; 1 vague criterion(s): VP-024

    * 两段之间用 ``"; "`` 分隔；**只有**实际存在的那一段才出现（没有重复
      就不会出现 ``0 duplicate group(s)`` 这种噪音）。
    * 摘要里带的是**成员 id** —— 操作员要照着 id 去找那条验证点，光说"有
      一组重复"没有用。
    """
    parts: List[str] = []

    groups = list(duplicate_groups or ())
    if groups:
        member_ids = _unique_ids(
            member_id
            for group in groups
            for member_id in (group.get("member_ids") or ())
        )
        parts.append(
            f"{len(groups)} duplicate group(s): {', '.join(member_ids)}"
        )

    vague = list(vague_criteria or ())
    if vague:
        vague_ids = _unique_ids(item.get("id") for item in vague)
        parts.append(
            f"{len(vague)} vague criterion(s): {', '.join(vague_ids)}"
        )

    return "; ".join(parts)


def build_verification_report(
    points: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """把验证点的检测结果组装成报告（**纯函数**）。

    返回形如::

        {"duplicate_groups": [{"reason": "identical_command: bash scripts/ci_gate.sh",
                               "member_ids": ["VP-022", "VP-025"]}],
         "summary": "1 duplicate group(s): VP-022, VP-025; "
                    "1 vague criterion(s): VP-024",
         "vague_criteria": [{"id": "VP-024",
                             "reason": "vague_criterion: placeholder",
                             "expected_result": "验证通过"}]}

    * 三段都在（``duplicate_groups`` / ``summary`` / ``vague_criteria``）——
      键固定，报告才能被 diff。
    * 空输入 → 空列表 + 空摘要，**不抛异常**（判据跑在别人写的 JSON 上）。
    * 本函数**不写盘**：写不写、写到哪由 ``backend/plan_dedup_report.py``
      决定。
    """
    points = list(points or ())
    groups = detect_duplicate_points(points)
    vague = detect_vague_criteria(points)
    return {
        "duplicate_groups": groups,
        "summary": summarize_findings(groups, vague),
        "vague_criteria": vague,
    }


def build_tasks_report(
    tasks: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """把任务的检测结果组装成报告（**纯函数**）。

    返回形如::

        {"duplicate_groups": [{"reason": "identical_files_and_command: 1 file(s) + "
                                         "pytest backend/tests/unit/test_a.py -v",
                               "member_ids": ["1", "2"]}],
         "summary": "1 duplicate group(s): 1, 2"}

    * 任务侧**没有**空泛判据这一段：``expected_result`` 是验证点的字段，
      任务里没有对应的东西 —— 两侧的键各自说清自己那侧的事。
    * 与 :func:`build_verification_report` 同一套摘要口径。
    """
    tasks = list(tasks or ())
    groups = detect_duplicate_tasks(tasks)
    return {
        "duplicate_groups": groups,
        "summary": summarize_findings(groups),
    }