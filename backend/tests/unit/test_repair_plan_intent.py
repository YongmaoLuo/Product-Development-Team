"""Unit tests for ``_plan_intent`` — the plan-intent constraint extractor.

Why this extractor exists
-------------------------
The repair loop decides what to change from the *verification evidence*
alone.  A plan whose own material says "不碰 Rust" therefore still had
Rust files rewritten by the auto-repair, because the constraint lived in
the plan directory and nowhere the repair path reads.  The extractor
below pulls those constraint sentences back out of the plan directory so
the repair prompt can carry them.

What is pinned here
-------------------
1. a plain-text ``import.json`` yields the constraint sentence;
2. a nested-dict ``import.json`` yields it too — the imported payload is
   a JSON *value*, not always a string;
3. a plan directory with no material yields ``[]``;
4. malformed JSON does not raise;
5. the result is capped at 20 sentences.

Plus the boundary conditions the extractor documents: order-preserving
de-duplication, a 300-character cap per sentence, and sentences that
carry no constraint marker being dropped.

No network / database — pure local-disk unit tests.
"""
import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import repair_generator as rg  # noqa: E402


# ---------------------------------------------------------------------------
# _plan_intent
# ---------------------------------------------------------------------------


def test_extracts_string_import_constraint(tmp_path):
    """A plain-text import payload yields the constraint sentence."""
    (tmp_path / "import.json").write_text(
        "只改 Python（src/、api/）；不碰 Rust。",
        encoding="utf-8",
    )

    assert rg._plan_intent(tmp_path) == ["只改 Python（src/、api/）", "不碰 Rust"]


def test_dict_import_flattened(tmp_path):
    """A nested-dict payload is flattened and its constraint extracted."""
    payload = {
        "scope": {
            "languages": ["Python"],
            "rule": "不要改 rust_core/ 下的任何文件",
        },
    }
    (tmp_path / "import.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    assert rg._plan_intent(tmp_path) == ["不要改 rust_core/ 下的任何文件"]


def test_missing_files_return_empty(tmp_path):
    """A plan directory with no material yields an empty list."""
    assert rg._plan_intent(tmp_path) == []


def test_malformed_json_does_not_raise(tmp_path):
    """Unparseable material is treated as plain text, not as a failure."""
    (tmp_path / "import.json").write_text("{", encoding="utf-8")

    assert rg._plan_intent(tmp_path) == []


def test_caps_twenty_items(tmp_path):
    """More than 20 constraints yield the first 20, in order."""
    payload = [f"不碰 module_{index}" for index in range(25)]
    (tmp_path / "import.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = rg._plan_intent(tmp_path)

    assert len(result) == 20
    assert result == [f"不碰 module_{index}" for index in range(20)]


# ---------------------------------------------------------------------------
# _plan_intent — boundary conditions
# ---------------------------------------------------------------------------


def test_drops_sentences_without_a_constraint_marker(tmp_path):
    """Prose that states no constraint is not collected."""
    (tmp_path / "import.json").write_text(
        "这个功能需要一个后端接口；前端页面也要改。",
        encoding="utf-8",
    )

    assert rg._plan_intent(tmp_path) == []


def test_deduplicates_preserving_first_occurrence(tmp_path):
    """The same constraint twice is collected once, at its first place."""
    (tmp_path / "import.json").write_text(
        "不碰 Rust。\n不碰 Python。\n不碰 Rust。",
        encoding="utf-8",
    )

    assert rg._plan_intent(tmp_path) == ["不碰 Rust", "不碰 Python"]


def test_truncates_a_single_sentence_to_300_chars(tmp_path):
    """A very long constraint sentence is capped at 300 characters."""
    (tmp_path / "import.json").write_text(
        "不碰" + ("啊" * 500),
        encoding="utf-8",
    )

    result = rg._plan_intent(tmp_path)

    assert len(result) == 1
    assert len(result[0]) == 300
    assert result[0].startswith("不碰")


def test_reads_a_plain_text_plan_document(tmp_path):
    """A non-JSON plan document is read as plain text."""
    (tmp_path / "prd.md").write_text(
        "## 范围\n\n只改 Python；不得改动 Rust 代码。\n",
        encoding="utf-8",
    )

    assert rg._plan_intent(tmp_path) == ["只改 Python", "不得改动 Rust 代码"]


# ---------------------------------------------------------------------------
# _flatten_json_strings
# ---------------------------------------------------------------------------


def test_flatten_json_strings_collects_nested_strings_in_order():
    """Dict / list values are collected depth-first, in document order."""
    assert rg._flatten_json_strings(
        {"a": "one", "b": ["two", {"c": "three"}]}
    ) == ["one", "two", "three"]


def test_flatten_json_strings_keeps_plain_text_unchanged():
    """A string that is not JSON stays the string it was."""
    assert rg._flatten_json_strings("只改 Python；不碰 Rust。") == [
        "只改 Python；不碰 Rust。"
    ]


def test_flatten_json_strings_parses_a_json_encoded_string():
    """A JSON-encoded string is decoded rather than kept quoted."""
    assert rg._flatten_json_strings('"不碰 Rust"') == ["不碰 Rust"]


# ---------------------------------------------------------------------------
# _iter_intent_sentences
# ---------------------------------------------------------------------------


def test_iter_intent_sentences_splits_on_terminators():
    """Full-width terminators and newlines separate sentences."""
    assert list(rg._iter_intent_sentences("不碰 Rust。禁止改 Python！只改前端？")) == [
        "不碰 Rust",
        "禁止改 Python",
        "只改前端",
    ]


def test_iter_intent_sentences_keeps_only_constraint_sentences():
    """A sentence without a marker is not yielded."""
    assert list(rg._iter_intent_sentences("随便写点什么")) == []


def test_iter_intent_sentences_matches_english_out_of_scope():
    """The English marker is recognised as well."""
    assert list(rg._iter_intent_sentences("Rust changes are out of scope")) == [
        "Rust changes are out of scope"
    ]


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))
