"""Unit tests for the deterministic banned-region fallback.

Why this exists
---------------
The repair prompt asks the model to *judge* whether a proposed fix
crosses one of the plan's own constraints, and the answer comes back as
the structured ``intent_check`` block (see
``test_repair_intent_check_parse.py``). A judgement is only as good as
the model's reading of the constraint — and a model that answers
``violates=false`` for a fix that rewrites a whole Rust crate is not
distinguishable, downstream, from a model that read the constraint
correctly and found no conflict.

So the constraint is re-read *deterministically* as well: the plan's own
constraint sentences are mined for the regions they put off limits
(``src/``, ``api/``, ``web-frontend``, "Rust"), and any file the
repair wants to touch is checked against those regions — by path
segment for a directory, by the shape of the code for a language.
The judgement stays the primary signal; this is the floor under it.

What is pinned here
-------------------
* the worked example — one constraint sentence yields four regions;
* "Rust" is kept as the language ``rust``, and a Rust path hits it;
* a bare (unquoted) ``xxx-yyy`` / ``xxx_yyy`` identifier is a region too;
* a path hits a region when the region's segments appear consecutively
  in the path, so a subdirectory hits its parent region;
* ``normalise_declared_files`` drops absolute paths and ``..``
  traversal and collapses duplicates — the same filter the repair task
  assembler has always applied, now a named function.

No network / database — pure in-process unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import repair_generator as rg  # noqa: E402


#: The sentence the task brief uses as its worked example.
EXAMPLE_INTENT = [
    "只改 Python（`src/`、`api/`）与 `web-frontend`；不碰 Rust"
]


# ---------------------------------------------------------------------------
# _extract_banned_regions
# ---------------------------------------------------------------------------


def test_rust_is_kept_as_a_language_region():
    """"不碰 Rust" puts Rust code off limits."""
    regions = rg._extract_banned_regions(["不碰 Rust"])

    assert "rust" in regions


def test_rust_mapping_is_case_insensitive():
    """The language name is capitalised freely in plan material."""
    for spelling in ("Rust", "rust", "RUST"):
        regions = rg._extract_banned_regions([f"不碰 {spelling} 代码"])

        assert "rust" in regions, spelling


def test_rust_named_at_the_end_of_a_sentence():
    """The full stop that closes the sentence is not part of the name."""
    regions = rg._extract_banned_regions(["不碰 Rust."])

    assert regions == ["rust"]


def test_backticked_and_path_tokens_extracted():
    """The worked example yields exactly four regions, in order."""
    assert rg._extract_banned_regions(EXAMPLE_INTENT) == [
        "src",
        "api",
        "web-frontend",
        "rust",
    ]


def test_bare_identifier_extracted():
    """A hyphenated identifier is a region even without backticks."""
    regions = rg._extract_banned_regions(["不要动 web-frontend 的代码"])

    assert "web-frontend" in regions


def test_underscored_identifier_extracted():
    """An underscored identifier is a region even without backticks."""
    regions = rg._extract_banned_regions(["不要动 engine_rs 目录"])

    assert "engine_rs" in regions


def test_plain_word_is_not_a_region():
    """A bare word names no region — "Python" alone is not a path."""
    regions = rg._extract_banned_regions(["只改 Python"])

    assert regions == []


def test_path_token_extracted_without_backticks():
    """A token carrying a slash is a region on its own."""
    regions = rg._extract_banned_regions(["不要改 src/ 下的任何文件"])

    assert "src" in regions


def test_trailing_slash_stripped():
    """The trailing slash is dropped before the region is used."""
    regions = rg._extract_banned_regions(["不要改 `api/` 目录"])

    assert regions == ["api"]


def test_empty_text_yields_no_regions():
    """Nothing in, nothing out."""
    assert rg._extract_banned_regions([]) == []
    assert rg._extract_banned_regions([""]) == []
    assert rg._extract_banned_regions(["   "]) == []
    assert rg._extract_banned_regions(None) == []


def test_regions_deduplicated_preserving_order():
    """The same region twice is kept once, at its first place."""
    regions = rg._extract_banned_regions([
        "不碰 `src/`；不改 `api/`；再次别动 `src/`",
    ])

    assert regions == ["src", "api"]


def test_rust_and_the_directory_are_separate_regions():
    """"Rust" and a directory named in the same breath are two regions.

    The language is not a directory: "不碰 Rust" puts every Rust file
    off limits, and naming a directory besides it puts that directory
    off limits too. Neither swallows the other.
    """
    regions = rg._extract_banned_regions([
        "不碰 Rust（`engine_rs/`）",
    ])

    assert regions == ["rust", "engine_rs"]


# ---------------------------------------------------------------------------
# _path_hits_region
# ---------------------------------------------------------------------------


def test_path_hits_region_by_segment():
    """A file below the region hits it — the region is a path prefix."""
    assert rg._path_hits_region("engine_rs/src/signals.rs", "engine_rs") is True
    assert rg._path_hits_region("backend/src/main.py", "src") is True
    assert rg._path_hits_region("engine_rs/src/signals.rs", "src") is True


def test_path_misses_unrelated_region():
    """A path that does not carry the region does not hit it."""
    assert rg._path_hits_region("backend/main.py", "src") is False
    assert rg._path_hits_region("backend/main.py", "engine_rs") is False


def test_path_hits_region_needs_consecutive_segments():
    """The region's segments have to be adjacent in the path."""
    assert rg._path_hits_region("a/src/b/api.py", "src/api") is False
    assert rg._path_hits_region("a/src/api/b.py", "src/api") is True


def test_path_hits_region_on_an_empty_pair():
    """Nothing to compare means no hit."""
    assert rg._path_hits_region("", "src") is False
    assert rg._path_hits_region("src/app.py", "") is False


# ---------------------------------------------------------------------------
# _path_hits_region — language regions
# ---------------------------------------------------------------------------


def test_rust_region_matches_rs_file():
    """A ``.rs`` file is Rust code, whatever the project calls it."""
    assert rg._path_hits_region("crates/core/src/lib.rs", "rust") is True


def test_rust_region_matches_cargo_manifest():
    """A Cargo manifest is Rust code too."""
    assert rg._path_hits_region("engine_rs/Cargo.toml", "rust") is True


def test_rust_region_matches_crate_directory():
    """A crate directory (a segment ending in ``_rs``) is Rust code."""
    assert rg._path_hits_region("engine_rs/src/signals.rs", "rust") is True


def test_rust_region_does_not_match_unrelated_file():
    """A file that is not Rust code does not hit the Rust region."""
    assert rg._path_hits_region("backend/routes/plans.py", "rust") is False


# ---------------------------------------------------------------------------
# normalise_declared_files
# ---------------------------------------------------------------------------


def test_normalise_declared_files_filters_absolute_and_parent():
    """Absolute paths and ``..`` traversal are dropped; duplicates collapse."""
    raw = [
        "src/app.py",
        "/etc/passwd",
        "../escape.py",
        "a/../../b.py",
        "src/app.py",
        "   ",
        "backend/api/x.py",
    ]

    assert rg.normalise_declared_files(raw) == [
        "src/app.py",
        "backend/api/x.py",
    ]


def test_normalise_declared_files_accepts_a_bare_string():
    """A single string is one entry, not a sequence of characters."""
    assert rg.normalise_declared_files("src/app.py") == ["src/app.py"]


def test_normalise_declared_files_of_a_non_list_is_empty():
    """A shape that is not a scope carries no scope."""
    assert rg.normalise_declared_files(None) == []
    assert rg.normalise_declared_files({"a": 1}) == []
    assert rg.normalise_declared_files([None, 7]) == []


def test_normalise_declared_files_keeps_the_first_occurrence_order():
    """Order follows the input, not the alphabet."""
    assert rg.normalise_declared_files(["b.py", "a.py", "b.py"]) == [
        "b.py",
        "a.py",
    ]


# ---------------------------------------------------------------------------
# _constraint_for_region
# ---------------------------------------------------------------------------


def test_constraint_for_region_finds_the_sentence():
    """The sentence that names the region is the one reported."""
    constraint = rg._constraint_for_region("rust", EXAMPLE_INTENT)

    assert constraint == EXAMPLE_INTENT[0]


def test_constraint_for_region_finds_a_plain_name():
    """A region named without backticks is still located."""
    constraint = rg._constraint_for_region("src", ["不要改 src 目录"])

    assert constraint == "不要改 src 目录"


def test_constraint_for_region_of_an_unknown_region_is_empty():
    """No sentence names it, so there is nothing to report."""
    assert rg._constraint_for_region("nowhere", EXAMPLE_INTENT) == ""
    assert rg._constraint_for_region("", EXAMPLE_INTENT) == ""


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))
