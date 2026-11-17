"""要求书的冻结与指纹。

What this file witnesses
------------------------
进入收尾阶段时，要求书的版本被**冻结**（架构决策点 3）：本轮判据是
冻结那一刻的要求书，之后对 ``postprocess.json`` 的编辑只影响下一轮。
所以冻结必须钉住两件事：

1. **指纹只跟随语义变化** —— 同一份要求书写法不同（键序不同、CRLF 与
   LF、中文与 emoji），指纹是同一个；判据变了、条目加了或删了，指纹必须
   变。指纹是本轮的身份，语义变了还认成同一轮，重试与审计就对不上。
2. **冻结之后文件再改，本轮不变** —— 已经返回的冻结对象带着冻结那一刻
   的条目集与哈希；文件随后被编辑，那只影响下一轮。

``postprocess.md`` 是自由补充，不是判据，所以它的哈希单列
（``markdown_sha256``）：改了补充说明，本轮的 ``spec_sha256`` 必须不动。

Why the samples come from the shared fixture
--------------------------------------------
``tests/fixtures/postprocess/builders.py`` owns the item shape, so these
cases spell only what they are about.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from postprocess.spec import (
    MARKDOWN_FILENAME,
    SPEC_FILENAME,
    FrozenSpec,
    Spec,
    freeze,
    load_spec,
)
from tests.fixtures.postprocess.builders import make_item, make_spec


#: The item keys in the order the model's data model declares them.
#: The book is written once in this order and once with every dict's keys
#: reversed — two genuinely different byte sequences carrying one meaning.
ITEM_KEY_ORDER = (
    "id",
    "action",
    "acceptance_predicate",
    "evidence_requirement",
    "external",
    "required",
    "wait",
)


def _item(**overrides: Any) -> Dict[str, Any]:
    """One requirement item, built by the shared fixture."""
    return make_item(**overrides)


def _reverse_keys(value: Any) -> Any:
    """Every dict's keys, reversed — recursively.

    Args:
        value: Any JSON-shaped value.

    Returns:
        The same value with every object's key order reversed.
    """
    if isinstance(value, dict):
        return {
            key: _reverse_keys(value[key]) for key in reversed(list(value))
        }
    if isinstance(value, list):
        return [_reverse_keys(entry) for entry in value]
    return value


def _write_book(
    plan_dir: Path,
    items: List[Dict[str, Any]],
    *,
    markdown: Optional[str] = None,
    newline: str = "\n",
    reorder: bool = False,
) -> None:
    """Write a requirement book into ``plan_dir``.

    Args:
        plan_dir: The directory to write the book into.
        items: The items of the book.
        markdown: The free-form companion file's content; ``None``
            means the file is not written at all.
        newline: The line ending the files are written with — ``"\\n"``
            or ``"\\r\\n"``.
        reorder: Whether every object's keys are written in reverse
            order — the same meaning, different bytes.
    """
    document: Any = {"items": [dict(item) for item in items]}
    if reorder:
        document = _reverse_keys(document)
    text = json.dumps(document, ensure_ascii=False, indent=2)
    (plan_dir / SPEC_FILENAME).write_text(
        text.replace("\n", newline), encoding="utf-8", newline=""
    )
    if markdown is not None:
        (plan_dir / MARKDOWN_FILENAME).write_text(
            markdown.replace("\n", newline), encoding="utf-8", newline=""
        )


def _freeze_book(
    plan_dir: Path,
    items: List[Dict[str, Any]],
    *,
    markdown: Optional[str] = None,
    newline: str = "\n",
    reorder: bool = False,
) -> FrozenSpec:
    """Write a book, load it and freeze it — the whole round's entry."""
    _write_book(
        plan_dir, items, markdown=markdown, newline=newline, reorder=reorder
    )
    return freeze(load_spec(plan_dir))


# ---------------------------------------------------------------------
# The fingerprint follows meaning, not spelling
# ---------------------------------------------------------------------


def test_key_order_does_not_change_hash(tmp_path):
    """The same book written with another key order is the same book."""
    items = [
        _item(
            id="first",
            action="检查第一条",
            acceptance_predicate={"kind": "exit_code", "value": 0},
        ),
        _item(
            id="second",
            action="检查第二条",
            acceptance_predicate={"kind": "exit_code", "value": 0},
        ),
    ]

    straight = _freeze_book(tmp_path, items)
    reversed_order = _freeze_book(tmp_path, items, reorder=True)

    assert straight.spec_sha256 == reversed_order.spec_sha256
    assert straight.markdown_sha256 == reversed_order.markdown_sha256


def test_crlf_and_lf_hash_identically(tmp_path):
    """CRLF and LF carry the same meaning, so they carry the same digest."""
    items = [_item(id="only", action="检查第一条")]

    lf = _freeze_book(tmp_path, items, markdown="# 补充\n说明", newline="\n")
    crlf = _freeze_book(tmp_path, items, markdown="# 补充\n说明", newline="\r\n")

    assert crlf.spec_sha256 == lf.spec_sha256
    assert crlf.markdown_sha256 == lf.markdown_sha256


def test_in_memory_crlf_markdown_hashes_like_lf(tmp_path):
    """A frozen book built in memory normalises its line endings too.

    The file path already normalises CRLF while reading; a ``Spec``
    assembled in code does not go through that reader, so the
    normalisation has to belong to the hashing, not to the loader.
    """
    _write_book(tmp_path, [_item(id="only")], markdown="# 补充\n说明")
    spec = load_spec(tmp_path)

    lf = freeze(spec)
    crlf = freeze(
        Spec(items=spec.items, markdown=spec.markdown.replace("\n", "\r\n"))
    )

    assert crlf.markdown_sha256 == lf.markdown_sha256
    assert crlf.spec_sha256 == lf.spec_sha256


def test_unicode_content_hashes_stably(tmp_path):
    """Chinese and emoji are content, not encoding noise."""
    items = [
        _item(
            id="条目",
            action="验证中文与 emoji 🚀",
            acceptance_predicate={"kind": "exit_code", "note": "✅ 通过"},
        )
    ]

    first = _freeze_book(tmp_path, items)
    second = _freeze_book(tmp_path, items)

    assert first.spec_sha256 == second.spec_sha256
    assert len(first.spec_sha256) == 64
    assert first.spec_sha256 == first.spec_sha256.lower()


def test_predicate_change_changes_hash(tmp_path):
    """A changed criterion is a different round — the digest must say so."""
    before = _freeze_book(
        tmp_path,
        [_item(id="only", acceptance_predicate={"kind": "exit_code", "value": 0})],
    )
    after = _freeze_book(
        tmp_path,
        [_item(id="only", acceptance_predicate={"kind": "exit_code", "value": 1})],
    )

    assert after.spec_sha256 != before.spec_sha256


def test_added_item_changes_hash(tmp_path):
    """One more entry is a different book."""
    before = _freeze_book(tmp_path, [_item(id="first")])
    after = _freeze_book(tmp_path, [_item(id="first"), _item(id="second")])

    assert after.spec_sha256 != before.spec_sha256


def test_removed_item_changes_hash(tmp_path):
    """One entry fewer is a different book."""
    before = _freeze_book(tmp_path, [_item(id="first"), _item(id="second")])
    after = _freeze_book(tmp_path, [_item(id="first")])

    assert after.spec_sha256 != before.spec_sha256


# ---------------------------------------------------------------------
# The companion file is context, not criteria
# ---------------------------------------------------------------------


def test_markdown_change_keeps_spec_hash(tmp_path):
    """Editing ``postprocess.md`` moves the markdown digest only."""
    items = [_item(id="only")]
    before = _freeze_book(tmp_path, items, markdown="# 第一版")
    after = _freeze_book(tmp_path, items, markdown="# 第二版")

    assert after.spec_sha256 == before.spec_sha256
    assert after.markdown_sha256 != before.markdown_sha256


# ---------------------------------------------------------------------
# Freezing holds
# ---------------------------------------------------------------------


def test_frozen_spec_immutable_after_file_edit(tmp_path):
    """Editing the file after the freeze does not touch this round."""
    items = [_item(id="first"), _item(id="second")]
    frozen = _freeze_book(tmp_path, items)
    before_hash = frozen.spec_sha256
    before_ids = [item.id for item in frozen.spec.items]

    _write_book(
        tmp_path,
        [
            _item(
                id="first",
                acceptance_predicate={"kind": "exit_code", "value": 9},
            ),
            _item(id="third"),
        ],
    )

    assert frozen.spec_sha256 == before_hash
    assert [item.id for item in frozen.spec.items] == before_ids
    assert frozen.spec_sha256 == before_hash
    # The file itself did change — the next round freezes a different book.
    assert freeze(load_spec(tmp_path)).spec_sha256 != before_hash


def test_frozen_spec_carries_both_digests_and_a_timestamp(tmp_path):
    """The frozen object says what it froze, when, and how."""
    _write_book(tmp_path, [_item(id="only")], markdown="# 补充")

    frozen = freeze(load_spec(tmp_path))

    assert isinstance(frozen, FrozenSpec)
    assert isinstance(frozen.spec, Spec)
    assert [item.id for item in frozen.spec.items] == ["only"]
    assert len(frozen.spec_sha256) == 64
    assert len(frozen.markdown_sha256) == 64
    assert frozen.spec_sha256 != frozen.markdown_sha256
    assert datetime.fromisoformat(frozen.frozen_at).tzinfo is not None


def test_frozen_spec_is_a_copy_of_the_loaded_book(tmp_path):
    """The frozen book is the caller's own — later edits do not reach it."""
    _write_book(tmp_path, [_item(id="only")])

    spec = load_spec(tmp_path)
    frozen = freeze(spec)

    assert frozen.spec is not spec
    assert frozen.spec.items is not spec.items
    assert [item.id for item in frozen.spec.items] == ["only"]
