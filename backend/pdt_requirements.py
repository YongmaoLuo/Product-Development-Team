"""部署级「操作者要求」的加载与渲染层（``PDT.md`` 家族）。

背景
----
本仓能影响模型行为的通道只有一个：gitignored 的**项目级**指引文件（仓库根
目录那份 AI 使用说明）。它跟着项目走（换一台机器、换一个 checkout，要求就变
了），而且没有地方放「这台部署上，所有阶段都要遵守的额外要求」。本模块提供的
正是那个位置的**唯一数据源**：把部署根目录下的要求文件读出来，渲染成注入到
system 提示最前的提示块。

三层文件
--------
部署根目录（``server._PROJECT_ROOT``）下：

* ``PDT.md`` —— base，所有 scene 都吃；
* ``PDT.<stage>.md`` —— 阶段文件，stage 由 scene 查表得到；
* ``PDT.<scene>.md`` —— 精确到单个 scene，优先级最高。

本版**不做计划级（plan-level）文件**：四层叠加的合并顺序不可预测，比"少一
层"更难排查 —— 少一层只是少一条要求，多一层却可能悄悄改变同一条要求的生效
范围。

stage 映射
----------
:data:`STAGE_BY_SCENE` 是全仓**唯一**的一张 scene→stage 表，只此一处。
``summarizer`` 故意不在表里：它不属于任何阶段，只吃 base。未知 scene 同样
只吃 base（安全回落，不报错）—— 认不出来的 scene 不该让整条链路失败。

为什么框架对内容保持无知
------------------------
本模块只做"把文本读出来"与"把文本包成提示块"这两件事：不解析、不按关键词
特判、不把任何**具体策略**写进代码 —— 哪条要求该怎么执行，是要求文件自己的
事，框架不该替它做决定。

块里的两句固定声明是框架唯一的"表态"，且都是**元层面**的（优先级与拼接
顺序），不含任何具体策略：

* 红线清单由代码写死（:data:`REDLINE`），**不来自要求文件** —— 所以要求
  文件可以补充要求，却不能把红线删掉、改写或顶到后面去；
* 优先级声明说明"这层内容高于框架默认行为"以及"多层冲突时后者覆盖前者"。

边界
----
* 单文件上限 8 KB，超限截断到字符边界并记一条 WARNING，**不抛异常**；
* 缓存键 ``(path, mtime_ns, size)``：文件改了，下一次调用即生效，不需要重启；
* ``import server`` 失败时回落到本文件所在包的上一级，并记一条 WARNING，
  同样不抛异常 —— 读取要求文件失败绝不该让一次 LLM 调用整个失败；
* 没有生效文件时 :func:`inject` 是**逐字节 no-op**（含 ``None`` → ``None``）。
"""

from __future__ import annotations

import codecs
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 单个要求文件的大小上限（字节）。超限的部分被截掉。
MAX_FILE_BYTES = 8192

#: base 层文件名 —— 所有 scene 都吃它。
BASE_FILENAME = "PDT.md"

#: 注入块的标签名。块形如 ``<pdt_user_requirements source="...">…</pdt_user_requirements>``。
BLOCK_TAG = "pdt_user_requirements"

#: 块的开标记 —— 幂等检测只认它（带 ``<`` 以免误伤正文里的其它提及）。
BLOCK_MARK = "<" + BLOCK_TAG

#: 块内第一句固定声明：内容由操作者落盘，优先级高于框架默认行为；多层按
#: base → stage → scene 顺序拼接，冲突时后者覆盖前者。
#:
#: 两句都是**元层面**的表态（优先级、拼接顺序），不含任何具体策略 —— 框架
#: 对内容保持无知。
PRIORITY_DECLARATION = (
    "以下内容是本部署操作者为 PDT 规定的额外要求，优先级高于框架默认行为。"
    "各层按 base → stage → scene 顺序拼接，后者在冲突时覆盖前者。"
)

#: 不可覆盖的红线清单 —— **由代码写死**，不来自要求文件。
#:
#: 要求文件可以补充要求，但不能把这份清单删掉、改写或顶到后面去，所以它永远
#: 排在块内最前，且不参与"后者覆盖前者"的拼接：内容与红线冲突时红线优先。
REDLINE = (
    "[红线] 不得伪造或篡改证据/测试结论；不得跳过或禁用验证；"
    "不得擅自 push、合入或对外发布；"
    "不得修改共享环境（生产 venv、其它 worktree、全局配置）。"
)

#: scene → stage 的**唯一**映射表。
#:
#: ``summarizer`` 故意不在表里：它不属于任何阶段，只吃 base。未知 scene 也
#: 只吃 base（安全回落）。
STAGE_BY_SCENE: dict[str, str] = {
    # --- plan stage ---
    "prd": "plan",
    "prd_refine": "plan",
    "arch": "plan",
    "arch_refine": "plan",
    "test_design": "plan",
    "test_refine": "plan",
    "tasks_generation": "plan",
    "interview": "plan",
    "planning": "plan",
    "refiner": "plan",
    # --- execution stage ---
    "execution": "execution",
    "service_restart": "execution",
    # --- verification stage ---
    "verification": "verification",
    "verification_plan_delta": "verification",
    "verification_command_audit": "verification",
    "audit_second_pass": "verification",
}

#: ``server`` 不可导入时的回落根目录 —— 本文件位于 ``backend/``，上一级
#: 就是仓库根。
_FALLBACK_ROOT = Path(__file__).resolve().parent.parent

#: 内容缓存。键是 ``(str(path), st_mtime_ns, st_size)`` —— 键相同即代表文件
#: 没变，不重读盘。
_CACHE: dict[tuple[str, int, int], str] = {}


def _deployment_root() -> Path:
    """返回部署根目录（``server._PROJECT_ROOT``）。

    ``server`` 是**惰性**导入的：本模块会被 ``coding_tool`` 导入，而
    ``coding_tool`` 又被 ``server`` 导入 —— 模块作用域里就 import 会形成
    循环。``server.py`` 自己把 ``sys.modules["server"]`` 指向 ``__main__``
    时的自己，所以 ``python server.py`` 启动的进程里这个 import 也不会
    跑出第二份应用。

    导入失败（依赖缺失、``server`` 尚未就绪）时回落到
    :data:`_FALLBACK_ROOT` 并记一条 WARNING，不抛异常。
    """
    try:
        import server as _server
    except Exception as exc:  # pragma: no cover —— 取决于运行环境
        logger.warning(
            "pdt_requirements: 无法 import server（%s），回落 %s",
            exc,
            _FALLBACK_ROOT,
        )
        return _FALLBACK_ROOT

    root = getattr(_server, "_PROJECT_ROOT", None)
    if root is None:
        logger.warning(
            "pdt_requirements: server._PROJECT_ROOT 不存在，回落到 %s",
            _FALLBACK_ROOT,
        )
        return _FALLBACK_ROOT
    return Path(root)


def candidate_files(scene: str) -> list[Path]:
    """返回该 scene 的候选要求文件，按 base → stage → scene 顺序。

    只返回**候选**：文件是否存在由 :func:`load_layers` 判断。未知 scene
    （以及不属于任何阶段的 ``summarizer``）只返回 base —— 认不出来的 scene
    不该被当成一个可以命名的层级。

    stage 文件与 scene 文件同名时（``scene == stage``）只出现一次，否则同
    一份内容会被加载两遍。
    """
    root = _deployment_root()
    candidates = [root / BASE_FILENAME]

    stage = STAGE_BY_SCENE.get(scene)
    if stage is None:
        return candidates

    candidates.append(root / f"PDT.{stage}.md")
    scene_file = root / f"PDT.{scene}.md"
    if scene_file not in candidates:
        candidates.append(scene_file)
    return candidates


def load_layers(scene: str) -> list[tuple[Path, str]]:
    """返回该 scene 生效的 ``(文件, 内容)``，按 base → stage → scene 顺序。

    三层各自独立可缺：只有存在的文件才出现在结果里；一个都不存在时返回
    空列表。内容里的冲突由上层按"后者覆盖前者"处理。
    """
    layers: list[tuple[Path, str]] = []
    for path in candidate_files(scene):
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        layers.append((path, text))
    return layers


def _read_text(path: Path) -> str | None:
    """读一个要求文件，带 mtime/size 缓存。读不了返回 ``None``。"""
    try:
        stat = path.stat()
    except OSError as exc:
        logger.warning("pdt_requirements: 无法 stat %s（%s）", path, exc)
        return None

    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("pdt_requirements: 无法读取 %s（%s）", path, exc)
        return None

    if len(raw) > MAX_FILE_BYTES:
        logger.warning(
            "pdt_requirements: %s 超过 %d 字节（%d），已截断",
            path.name,
            MAX_FILE_BYTES,
            len(raw),
        )
        text = _truncate(raw, MAX_FILE_BYTES)
    else:
        text = raw.decode("utf-8", errors="replace")

    _CACHE[key] = text
    _drop_stale_versions(key)
    return text


def _drop_stale_versions(key: tuple[str, int, int]) -> None:
    """丢掉同一路径的旧版本条目。

    缓存键带 mtime，所以文件每改一次就多一个条目；不留旧条目，缓存才不会
    随改动次数无限增长。
    """
    for stale in [k for k in _CACHE if k[0] == key[0] and k != key]:
        del _CACHE[stale]


def _truncate(raw: bytes, limit: int) -> str:
    """把 ``raw`` 截到不超过 ``limit`` 字节，且不切在字符中间。

    先按字节切，再用增量解码器解出完整字符 —— 落在多字节字符中间的那几个
    字节被留在解码器里，不会变成乱码。解码后如果仍超限（非法字节被替换成
    ``U+FFFD`` 后变长了），再按字符逐个裁到字节上限以内。
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(raw[:limit], final=False)
    if len(text.encode("utf-8")) <= limit:
        return text

    kept: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > limit:
            break
        kept.append(char)
        used += size
    return "".join(kept)


# ---------------------------------------------------------------------------
# 渲染层：把生效的要求文件包成一个提示块，注入到 system 提示的最前。
# ---------------------------------------------------------------------------


def _relative_name(path: Path, root: Path) -> str:
    """返回 ``path`` 相对部署根目录的路径（POSIX 风格）。

    正常情况下要求文件就在根目录下，相对路径就是文件名；万一不在（例如根
    目录被换掉），退回到文件名 —— 至少不抛异常。
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _render(scene: str | None) -> tuple[str | None, list[str]]:
    """渲染提示块。

    返回 ``(块, 生效文件的相对路径列表)``；没有任何生效文件时返回
    ``(None, [])``。

    块的结构::

        <pdt_user_requirements source="PDT.md">
        <优先级声明>
        <空行>
        [红线] …
        <空行>
        --- PDT.md ---
        <文件内容>
        </pdt_user_requirements>

    文件按 base → stage → scene 顺序拼接，**不做任何内容解析** —— 冲突留给
    声明去说，框架不替操作者判定谁对。
    """
    layers = load_layers(scene or "")
    if not layers:
        return None, []

    root = _deployment_root()
    files = [_relative_name(path, root) for path, _ in layers]
    sections = [
        "--- {} ---\n{}".format(name, text.rstrip("\n"))
        for name, (_, text) in zip(files, layers)
    ]

    block = "\n".join(
        (
            '<{} source="{}">'.format(BLOCK_TAG, ",".join(files)),
            PRIORITY_DECLARATION,
            "",
            REDLINE,
            "",
            "\n".join(sections),
            "</{}>".format(BLOCK_TAG),
        )
    )
    return block, files


def render_block(scene: str | None) -> str | None:
    """渲染该 scene 的提示块；没有生效文件时返回 ``None``。

    这是**唯一**的块生成入口：:func:`inject` 与 :func:`describe` 都走它，
    所以"注进去的那块"与"记到日志里的那块"永远一致。
    """
    block, _ = _render(scene)
    return block


def inject(system_instruction: str | None, scene: str | None) -> str | None:
    """把提示块放到 system 提示的**最前**。

    规则：

    * 输入里已经有块（``<pdt_user_requirements``）→ 原样返回，不叠加 ——
      重复注入是幂等的；
    * 没有生效文件 → 逐字节原样返回输入（``None`` → ``None``），一个字都不
      改、不多一个换行；
    * 有生效文件但输入是 ``None`` 或空串 → 返回块本身，块后不加多余换行 ——
      否则不传 system 提示的调用点会静默跳过整个机制；
    * 其余情况 → ``块 + "\\n\\n" + 原输入``。
    """
    if system_instruction and BLOCK_MARK in system_instruction:
        return system_instruction

    block = render_block(scene)
    if block is None:
        return system_instruction
    if not system_instruction:
        return block
    return "{}\n\n{}".format(block, system_instruction)


def describe(scene: str | None) -> dict:
    """返回该 scene 生效的文件与渲染块的哈希，供日志与观测使用。

    没有生效文件时返回 ``{}`` —— 调用方据此**不写**相关字段，而不是写一对
    空值。
    """
    block, files = _render(scene)
    if block is None:
        return {}
    return {
        "files": files,
        "sha256": hashlib.sha256(block.encode("utf-8")).hexdigest(),
    }
