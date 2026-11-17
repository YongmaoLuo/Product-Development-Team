"""收尾阶段的要求书：加载与校验。

What this module owns
---------------------
The finishing stage (收尾阶段) judges a plan against a **requirement
book** — ``plans/{id}/postprocess.json`` — and nothing else.  The book
is the only source of criteria: the judge reads an item's
``acceptance_predicate`` and the evidence, and the executor reads the
item's ``command``.  If a book is loaded with something wrong in it, the
damage is not a crash at load time; it is a **wrong verdict later**,
with nothing in the report to point back at the entry that asked for
it.  So the loader is where illegality has to be caught.

The companion file ``plans/{id}/postprocess.md`` is free-form context.
It is loaded, it is carried on the specification, and the judge never
reads it (see the architecture's decision point 7).  Its absence is
therefore not an error: a book without it loads with an empty markdown.

The three layers of validation
------------------------------
The architecture's decision point 3 asks for "逐字段 + 闭合枚举 + 交叉
约束" — three layers, and this module implements exactly those three:

1. **per field** — every declared field is present and of the shape the
   model declares (``id`` a name, ``action`` a non-empty sentence,
   ``acceptance_predicate`` a structured criterion, ``external`` and
   ``required`` booleans, ``command`` an argv array);
2. **closed enum** — ``wait.type`` is one of ``none`` / ``human`` /
   ``external`` and nothing else, because a wait nobody can name is a
   wait nobody can implement;
3. **cross constraint** — an outward-facing entry (``external=true``)
   must say **what** it acts on (``target``) and **how** (``command``).
   The command runner's whitelist is built from the frozen book, so an
   outward-facing entry without an argv cannot be whitelisted at all:
   accepting it would mean the runner had to guess, and guessing a
   whitelist is how an outward-facing command escapes it.

Why every message names the item
--------------------------------
A book has many entries and the loader rejects exactly one of them at a
time.  "要求书不合法" tells the author nothing; a message that names
the entry tells them which one to open.  So the id is written into
every rejection message — **raw, not ``repr``-quoted**, because an id
containing a backslash would otherwise be escaped out of the message
and the id would no longer be findable in it.

Freezing the book
----------------
Entering the finishing stage freezes the book (the architecture's
decision point 3): this round's criteria are the book as it stood at
that moment, and an edit made while the round is running belongs to the
*next* round.  So the freeze produces a **fingerprint** — and a
fingerprint that answered "which bytes are on disk" would be the wrong
one: re-ordering the keys of a book, or saving it with CRLF endings,
does not change what the book asks for, so it must not produce a
different version number.  Two rounds whose fingerprints differ have
different criteria; two whose fingerprints agree have the same
criteria, however they were spelled.

So the fingerprint is taken over a **canonical form** of the book's
criteria:

* every object's keys sorted, so key order is not a difference;
* the compact separators, so whitespace is not a difference;
* non-ASCII characters written as themselves (``ensure_ascii=False``),
  so a Chinese or emoji criterion has exactly one spelling;
* CRLF normalised to LF, so the line endings of the file are not a
  difference either.

The companion file's digest is kept **apart** (``markdown_sha256``):
``postprocess.md`` is context the judge never reads, so rewriting it
must not change the identity of the round.

What this module does NOT do
----------------------------
This module does not judge anything and does not know what a criterion
*means* — which criterion kinds exist is the judge's vocabulary, not
this loader's.  It also does not execute the book's entries; that is
the executor's job, and the frozen digest is what tells the executor
which book it may run.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


#: The structured requirement book — the only input of the finishing
#: stage's judgement.
SPEC_FILENAME: str = "postprocess.json"

#: The free-form companion file.  Context only; the judge never reads
#: it.
MARKDOWN_FILENAME: str = "postprocess.md"

#: The **closed** vocabulary of wait types.  An entry waits for nothing,
#: for a human, or for something outside the plan — and the name of
#: which one it is has to be one of these three, because the waiting
#: lifecycle implements exactly these three.
WAIT_TYPES: Tuple[str, ...] = ("none", "human", "external")

#: The keys every entry of the book must carry.  The architecture's
#: data model declares these seven; ``command`` and ``target`` are
#: additional keys the outward-facing entries carry.
REQUIRED_ITEM_KEYS: Tuple[str, ...] = (
    "id",
    "action",
    "acceptance_predicate",
    "evidence_requirement",
    "external",
    "required",
    "wait",
)


class SpecError(ValueError):
    """The requirement book is missing, unparsable or illegal.

    Raised by :func:`load_spec` for every way a book can fail to be a
    book: the file is absent, the file is not JSON, the shape is not
    the model's, an enum value is outside its vocabulary, or a
    cross-field constraint is unmet.  It is a ``ValueError`` because
    that is what it is — the value handed to the loader is wrong — and
    because callers that only want "this book is unusable" can catch
    the built-in type.
    """


@dataclass(frozen=True)
class SpecItem:
    """One entry of the requirement book.

    Attributes:
        id: The entry's name — unique inside the book, and never a
            path.
        action: What the entry does, in words.
        command: The argv the entry runs, when it runs one.  ``None``
            for an entry that runs nothing.
        acceptance_predicate: The criterion the judge applies.
        evidence_requirement: Which evidence the entry must produce.
        external: Whether the entry acts outward.
        target: What the outward-facing entry acts on.
        required: Whether the entry's failure fails the whole book.
        wait: The wait shape — ``{"type": ..., "check": ...}``.
    """

    id: str
    action: str
    command: Optional[List[str]]
    acceptance_predicate: Dict[str, Any]
    evidence_requirement: Any
    external: bool
    target: Optional[str]
    required: bool
    wait: Dict[str, Any]


@dataclass(frozen=True)
class Spec:
    """A loaded requirement book.

    Attributes:
        items: The book's entries, in the order the book listed them.
        markdown: The companion file's content — empty when the file is
            absent.
    """

    items: Tuple[SpecItem, ...]
    markdown: str


def load_spec(plan_dir: Any) -> Spec:
    """Load and validate the requirement book of one plan.

    Args:
        plan_dir: The plan directory holding ``postprocess.json`` and,
            optionally, ``postprocess.md``.

    Returns:
        The loaded specification: the validated entries and the
        companion file's text.

    Raises:
        SpecError: The book is absent, not JSON, or illegal.
    """
    plan_path = Path(plan_dir)
    document = _read_json(plan_path / SPEC_FILENAME)
    items = _parse_items(document)
    markdown = _read_markdown(plan_path / MARKDOWN_FILENAME)
    return Spec(items=items, markdown=markdown)


def has_postprocess_spec(plan_dir: Any) -> bool:
    """这个计划声明了收尾要求书吗？

    2026-11-14: 验证通过出口上的**唯一**判定点（架构决策点 1）用它决定
    计划是直接收尾（``completed``），还是进收尾阶段（``post_processing``）。

    判定问的不是「文件在不在」，而是「**要求书是不是一本要求书**」：一份
    写坏了的要求书（不是 JSON、条目为空、条目不合法）不是要求书，拿它去
    驱动收尾阶段，只会让收尾阶段拿着空判据干活。

    这个判定**不抛异常**：它是出口上的开关，炸了就没人能记录验证结果。

    Args:
        plan_dir: 计划目录。

    Returns:
        ``True`` —— 目录里有一本**能加载**的要求书；``False`` —— 其余全部
        情况（没有文件、文件不是 JSON、条目为空、条目不合法、目录不存在）。
    """
    try:
        load_spec(plan_dir)
    except SpecError:
        return False
    except Exception:  # noqa: BLE001 — 门控不抛异常（见上）
        return False
    return True


def _read_json(path: Path) -> Any:
    """Read and decode the requirement book.

    Args:
        path: The book's path.

    Returns:
        The decoded document.

    Raises:
        SpecError: The file is absent, unreadable or not JSON.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SpecError(f"要求书文件不存在: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"要求书文件无法读取: {path}: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecError(f"要求书不是合法 JSON: {path}: {exc}") from exc


def _read_markdown(path: Path) -> str:
    """Read the companion file — its absence is not an error.

    Args:
        path: The companion file's path.

    Returns:
        The file's content, or an empty string when the file is absent.

    Raises:
        SpecError: The file exists but cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"要求书的补充文件无法读取: {path}: {exc}") from exc


def _parse_items(document: Any) -> Tuple[SpecItem, ...]:
    """Validate the book's shape and every entry in it.

    Args:
        document: The decoded requirement book.

    Returns:
        The validated entries, in the order the book listed them.

    Raises:
        SpecError: The book has no usable shape, or an entry is
            illegal.
    """
    if not isinstance(document, Mapping):
        raise SpecError("要求书顶层必须是 JSON 对象")
    if "items" not in document:
        raise SpecError("要求书缺少 items 字段")

    raw_items = document["items"]
    if not isinstance(raw_items, list):
        raise SpecError("要求书的 items 必须是数组")
    if not raw_items:
        raise SpecError("要求书的 items 不能为空")

    items: List[SpecItem] = []
    first_seen: Dict[str, int] = {}
    for index, raw_item in enumerate(raw_items):
        item = _parse_item(raw_item, index)
        if item.id in first_seen:
            raise SpecError(
                f"要求书条目 {item.id} 不合法: "
                f"id 与第 {first_seen[item.id] + 1} 条重复"
            )
        first_seen[item.id] = index
        items.append(item)
    return tuple(items)


def _parse_item(raw_item: Any, index: int) -> SpecItem:
    """Validate one entry against all three layers.

    Args:
        raw_item: The entry as it was decoded.
        index: The entry's position in the book, counted from zero.

    Returns:
        The validated entry.

    Raises:
        SpecError: The entry is illegal.  The message names the entry
            whenever the entry has a usable id.
    """
    position = index + 1
    if not isinstance(raw_item, Mapping):
        raise SpecError(f"要求书第 {position} 条不合法: 必须是 JSON 对象")

    item_id = raw_item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise SpecError(f"要求书第 {position} 条不合法: 缺少 id")
    if "/" in item_id or "\\" in item_id or ".." in item_id:
        raise SpecError(
            f"要求书条目 {item_id} 不合法: id 不得包含路径分隔符或 '..'"
        )

    action = raw_item.get("action")
    if not isinstance(action, str) or not action.strip():
        raise SpecError(f"要求书条目 {item_id} 不合法: action 不能为空")

    predicate = raw_item.get("acceptance_predicate")
    if predicate is None:
        raise SpecError(
            f"要求书条目 {item_id} 不合法: 缺少 acceptance_predicate"
        )
    if not isinstance(predicate, Mapping):
        raise SpecError(
            f"要求书条目 {item_id} 不合法: acceptance_predicate 必须是对象"
        )

    evidence_requirement = raw_item.get("evidence_requirement")
    if evidence_requirement is None:
        raise SpecError(
            f"要求书条目 {item_id} 不合法: 缺少 evidence_requirement"
        )

    external = raw_item.get("external")
    if not isinstance(external, bool):
        raise SpecError(
            f"要求书条目 {item_id} 不合法: external 必须是布尔值"
        )

    required = raw_item.get("required")
    if not isinstance(required, bool):
        raise SpecError(
            f"要求书条目 {item_id} 不合法: required 必须是布尔值"
        )

    wait = _parse_wait(raw_item.get("wait"), item_id)
    command = _parse_command(
        raw_item.get("command"), item_id, present="command" in raw_item
    )
    target = _parse_target(
        raw_item.get("target"), item_id, present="target" in raw_item
    )

    if external:
        if target is None:
            raise SpecError(
                f"要求书条目 {item_id} 不合法: external 条目必须声明 target"
            )
        if command is None:
            raise SpecError(
                f"要求书条目 {item_id} 不合法: external 条目必须声明 command"
            )

    return SpecItem(
        id=item_id,
        action=action,
        command=command,
        acceptance_predicate=copy.deepcopy(dict(predicate)),
        evidence_requirement=copy.deepcopy(evidence_requirement),
        external=external,
        target=target,
        required=required,
        wait=wait,
    )


def _parse_wait(raw_wait: Any, item_id: str) -> Dict[str, Any]:
    """Validate an entry's wait shape.

    Args:
        raw_wait: The wait shape as it was decoded.
        item_id: The entry's id, for the rejection message.

    Returns:
        The validated wait shape.

    Raises:
        SpecError: The wait is not an object, names a type outside the
            closed vocabulary, or waits for something outside without
            saying what to check.
    """
    if not isinstance(raw_wait, Mapping):
        raise SpecError(f"要求书条目 {item_id} 不合法: wait 必须是对象")

    wait_type = raw_wait.get("type")
    if wait_type not in WAIT_TYPES:
        raise SpecError(
            f"要求书条目 {item_id} 不合法: 未知 wait.type {wait_type}"
        )

    if wait_type == "external":
        check = raw_wait.get("check")
        if check is None or (
            isinstance(check, (str, list, tuple, dict, set))
            and not check
        ):
            raise SpecError(
                f"要求书条目 {item_id} 不合法: "
                f"wait.type=external 必须声明 check"
            )

    return copy.deepcopy(dict(raw_wait))


def _parse_command(
    raw_command: Any,
    item_id: str,
    *,
    present: bool,
) -> Optional[List[str]]:
    """Validate an entry's argv.

    Args:
        raw_command: The argv as it was decoded.
        item_id: The entry's id, for the rejection message.
        present: Whether the book carried the key at all.

    Returns:
        The argv, or ``None`` when the book carried no command.

    Raises:
        SpecError: The command is present but is not a non-empty array
            of non-empty strings.  A string is rejected on purpose: the
            runner's whitelist is built from the argv, and a command
            written as one string has to be split by somebody — and
            that somebody would be guessing.
    """
    if not present:
        return None
    if not isinstance(raw_command, list):
        raise SpecError(
            f"要求书条目 {item_id} 不合法: command 必须是字符串数组"
        )
    if not raw_command:
        raise SpecError(f"要求书条目 {item_id} 不合法: command 不能为空")
    for argument in raw_command:
        if not isinstance(argument, str) or not argument:
            raise SpecError(
                f"要求书条目 {item_id} 不合法: "
                f"command 的每一项必须是非空字符串"
            )
    return list(raw_command)


def _parse_target(
    raw_target: Any,
    item_id: str,
    *,
    present: bool,
) -> Optional[str]:
    """Validate an entry's target.

    Args:
        raw_target: The target as it was decoded.
        item_id: The entry's id, for the rejection message.
        present: Whether the book carried the key at all.

    Returns:
        The target, or ``None`` when the book carried none.

    Raises:
        SpecError: The target is present but is not a non-empty string.
    """
    if not present:
        return None
    if not isinstance(raw_target, str) or not raw_target.strip():
        raise SpecError(
            f"要求书条目 {item_id} 不合法: target 必须是非空字符串"
        )
    return raw_target


# ---------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenSpec:
    """A requirement book, frozen at one moment.

    Attributes:
        spec: A copy of the book as it stood when it was frozen — not
            the caller's own object, so a later edit cannot reach it.
        spec_sha256: The digest of the book's **criteria** — the
            canonical form of its entries.  This is the round's
            identity: two books with the same digest ask the same
            things, however they were spelled.
        markdown_sha256: The digest of the companion file's content.
            It is kept apart from ``spec_sha256`` because the
            companion file is context the judge never reads — rewriting
            it must not change the identity of the round.
        frozen_at: When the book was frozen — ISO-8601, UTC.
    """

    spec: Spec
    spec_sha256: str
    markdown_sha256: str
    frozen_at: str


def freeze(spec: Spec) -> FrozenSpec:
    """Freeze a requirement book: fix this round's criteria and fingerprint them.

    The digest is taken over a **canonical** form of the book, so it
    follows meaning and not spelling: the same book written with
    another key order, with CRLF endings, or with a Chinese or emoji
    criterion has the same digest, while a changed criterion — or one
    entry more or less — has a different one.

    Args:
        spec: The loaded requirement book.

    Returns:
        The frozen book: a copy of the book, the digest of its
        criteria, the digest of its companion file, and the moment it
        was frozen.

    Raises:
        TypeError: The argument is not a :class:`Spec`.
        SpecError: The book holds a value that cannot be written as
            JSON, and therefore cannot be canonicalised.
    """
    if not isinstance(spec, Spec):
        raise TypeError(f"freeze 需要 Spec, 得到 {type(spec).__name__}")

    frozen = _copy_spec(spec)
    return FrozenSpec(
        spec=frozen,
        spec_sha256=_spec_digest(frozen),
        markdown_sha256=_markdown_digest(frozen.markdown),
        frozen_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _copy_spec(spec: Spec) -> Spec:
    """Copy a loaded book, entry by entry.

    The frozen book must not change under the reader's feet: the
    caller's own :class:`Spec` may go on being edited, and a frozen
    object that shared it would change with it.

    Args:
        spec: The book to copy.

    Returns:
        A copy that shares nothing mutable with the original.
    """
    return Spec(
        items=tuple(_copy_item(item) for item in spec.items),
        markdown=spec.markdown,
    )


def _copy_item(item: SpecItem) -> SpecItem:
    """Copy one entry of a book.

    Args:
        item: The entry to copy.

    Returns:
        A copy that shares no nested dict with the original.
    """
    return SpecItem(
        id=item.id,
        action=item.action,
        command=copy.deepcopy(item.command),
        acceptance_predicate=copy.deepcopy(item.acceptance_predicate),
        evidence_requirement=copy.deepcopy(item.evidence_requirement),
        external=item.external,
        target=item.target,
        required=item.required,
        wait=copy.deepcopy(item.wait),
    )


def _spec_digest(spec: Spec) -> str:
    """The digest of a book's criteria.

    Args:
        spec: The frozen book.

    Returns:
        The sha256 of the canonical form of the book's entries.
    """
    document = {"items": [_canonical_item(item) for item in spec.items]}
    payload = _canonical_json(document).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _markdown_digest(markdown: str) -> str:
    """The digest of the companion file's content.

    Args:
        markdown: The companion file's text.

    Returns:
        The sha256 of the text with its line endings normalised.
    """
    canonical = markdown.replace("\r\n", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_item(item: SpecItem) -> Dict[str, Any]:
    """One entry, in the shape the digest is taken over.

    Every field of the entry takes part — a criterion that changed is a
    different round — and the shape is fixed, so two books that differ
    only in their key order produce the same document.

    Args:
        item: The entry to canonicalise.

    Returns:
        The entry as a plain object.
    """
    return {
        "id": item.id,
        "action": item.action,
        "command": None if item.command is None else list(item.command),
        "acceptance_predicate": item.acceptance_predicate,
        "evidence_requirement": item.evidence_requirement,
        "external": item.external,
        "target": item.target,
        "required": item.required,
        "wait": item.wait,
    }


def _canonical_json(document: Any) -> str:
    """Write a document in the one spelling the digest is taken over.

    Sorted keys, compact separators and unescaped non-ASCII characters:
    key order, whitespace and the choice between ``\\uXXXX`` and the
    character itself are not differences in meaning, so they are not
    differences in the digest.

    Args:
        document: The document to write.

    Returns:
        The canonical text.

    Raises:
        SpecError: The document holds a value that is not JSON — a
            number that is not finite, or an object of a kind JSON
            cannot write.  A book like that cannot be canonicalised,
            and a digest of it would be a digest of nothing.
    """
    try:
        return json.dumps(
            document,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SpecError(f"要求书无法规范化: {exc}") from exc