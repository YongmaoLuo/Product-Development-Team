"""要求书的加载与三层校验。

What this file witnesses
------------------------
The requirement book (``plans/{id}/postprocess.json``) is the **only**
source of criteria for the finishing stage: whatever the judge is going
to accept is spelled out there, and nothing else may add to it.  A
loader that is too permissive therefore does not fail loudly at load
time — it fails later, in the judge, as a wrong verdict nobody can
trace back to the book that asked for it.

So this file pins the loader from three sides:

1. **the legal matrix** — every combination the model allows loads and
   parses (``none`` / ``human`` / ``external`` waits, ``external`` true
   and false, ``required`` true and false);
2. **the illegal matrix** — every shape the model forbids raises
   :class:`SpecError`, and the message names the offending item, so the
   author of the book knows *which* entry to fix;
3. **the two-file rule** — ``postprocess.md`` is optional context, so a
   book without it loads with an empty markdown, never an error.

Why the messages must carry the item id
---------------------------------------
A book has many entries and the loader rejects exactly one of them at a
time.  "要求书不合法" tells the author nothing; "条目 'open-pr' 的
acceptance_predicate 缺失" tells them which entry to open.  Every
rejection case below therefore asserts that the id appears in the
message — with the id written **raw**, not ``repr``-quoted, because a
backslash in an id would otherwise be escaped out of the message and the
substring would no longer be there.

Why the samples come from the shared fixture
--------------------------------------------
``tests/fixtures/postprocess/builders.py`` owns the sample shapes, so
these cases spell only what they are about.  The fixture's item builder
knows the model's seven keys; ``command`` and ``target`` belong to the
outward-facing entries and are layered on top of the fixture's item here,
so a sample never drifts from the shape its neighbours use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from postprocess.spec import Spec, SpecError, SpecItem, load_spec
from tests.fixtures.postprocess.builders import make_item, make_spec


#: The two keys the shared item builder does not know — they belong to
#: the outward-facing entries (``external=true``) and are layered on top
#: of the fixture's item.
EXTERNAL_ONLY_KEYS = ("command", "target")


def _item(**overrides: Any) -> Dict[str, Any]:
    """One requirement item, built by the shared fixture.

    The fixture owns the seven keys the model declares; ``command`` and
    ``target`` are added afterwards because only the outward-facing
    entries carry them.

    Args:
        **overrides: Any field of the item, including the two
            outward-facing ones.

    Returns:
        A fresh item dict.
    """
    extra = {
        key: overrides.pop(key)
        for key in EXTERNAL_ONLY_KEYS
        if key in overrides
    }
    item = make_item(**overrides)
    item.update(extra)
    return item


def _load(
    plan_dir: Path,
    items: List[Mapping[str, Any]],
    markdown: Optional[str] = None,
) -> Spec:
    """Write a requirement book into ``plan_dir`` and load it back.

    Args:
        plan_dir: The directory to write the book into.
        items: The items of the book.
        markdown: The free-form companion file's content; ``None``
            means the file is not written at all.

    Returns:
        The loaded specification.
    """
    document = make_spec(items)
    (plan_dir / "postprocess.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    if markdown is not None:
        (plan_dir / "postprocess.md").write_text(markdown, encoding="utf-8")
    return load_spec(plan_dir)


def _external_item(**overrides: Any) -> Dict[str, Any]:
    """One outward-facing item — it carries a command and a target."""
    base: Dict[str, Any] = {
        "external": True,
        "target": "owner/repo",
        "command": ["gh", "pr", "create", "--title", "x"],
    }
    base.update(overrides)
    return _item(**base)


# ---------------------------------------------------------------------
# The legal matrix
# ---------------------------------------------------------------------


def test_valid_spec_loads_with_all_wait_types(tmp_path):
    """One item per wait type — all three parse."""
    items = [
        _item(id="no-wait", wait={"type": "none"}),
        _item(id="human-wait", wait={"type": "human"}),
        _item(
            id="external-wait",
            wait={"type": "external", "check": {"kind": "pr_state"}},
        ),
    ]

    spec = _load(tmp_path, items)

    assert isinstance(spec, Spec)
    assert [item.id for item in spec.items] == [
        "no-wait",
        "human-wait",
        "external-wait",
    ]
    assert [item.wait["type"] for item in spec.items] == [
        "none",
        "human",
        "external",
    ]
    assert all(isinstance(item, SpecItem) for item in spec.items)


def test_valid_spec_accepts_the_whole_legal_matrix(tmp_path):
    """``external`` and ``required`` may each be either value."""
    items = [
        _external_item(id="outward"),
        _item(id="inward"),
        _item(id="optional", required=False),
        _item(id="mandatory", required=True),
    ]

    spec = _load(tmp_path, items)

    assert [item.external for item in spec.items] == [True, False, False, False]
    assert [item.required for item in spec.items] == [True, True, False, True]


def test_external_item_keeps_its_command_and_target(tmp_path):
    """The outward-facing entry keeps its argv and its target verbatim."""
    items = [
        _external_item(
            id="open-pr",
            action="open a pull request",
            command=["gh", "pr", "create", "--title", "x"],
            acceptance_predicate={"kind": "exit_code"},
            evidence_requirement="exit 0",
        )
    ]

    spec = _load(tmp_path, items)

    item = spec.items[0]
    assert item.id == "open-pr"
    assert item.command == ["gh", "pr", "create", "--title", "x"]
    assert item.acceptance_predicate == {"kind": "exit_code"}
    assert item.evidence_requirement == "exit 0"
    assert item.target == "owner/repo"
    assert item.external is True


def test_items_are_immutable_and_ordered(tmp_path):
    """The item list is a tuple, in the order the book listed it."""
    items = [_item(id="first"), _item(id="second")]

    spec = _load(tmp_path, items)

    assert isinstance(spec.items, tuple)
    assert [item.id for item in spec.items] == ["first", "second"]


# ---------------------------------------------------------------------
# The two-file rule
# ---------------------------------------------------------------------


def test_missing_markdown_yields_empty_string(tmp_path):
    """``postprocess.md`` is optional context — its absence is no error."""
    spec = _load(tmp_path, [_item(id="only")])

    assert spec.markdown == ""


def test_markdown_is_loaded_when_present(tmp_path):
    """The companion file is read as-is."""
    spec = _load(tmp_path, [_item(id="only")], markdown="# 补充说明")

    assert spec.markdown == "# 补充说明"


def test_missing_spec_file_rejected(tmp_path):
    """A plan without a requirement book has no specification."""
    with pytest.raises(SpecError):
        load_spec(tmp_path)


def test_unparsable_spec_file_rejected(tmp_path):
    """A broken book is an error, not an empty specification."""
    (tmp_path / "postprocess.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SpecError):
        load_spec(tmp_path)


# ---------------------------------------------------------------------
# The illegal matrix
# ---------------------------------------------------------------------


def test_empty_items_rejected(tmp_path):
    """A book with no entry judges nothing — it is rejected."""
    with pytest.raises(SpecError):
        _load(tmp_path, [])


def test_duplicate_id_rejected(tmp_path):
    """Two entries with one id cannot both be judged."""
    items = [_item(id="dup"), _item(id="dup")]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, items)

    assert "dup" in str(excinfo.value)


def test_missing_predicate_rejected(tmp_path):
    """An entry without a criterion cannot be judged — and says which."""
    item = _item(id="no-predicate")
    del item["acceptance_predicate"]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, [item])

    assert "no-predicate" in str(excinfo.value)


def test_empty_action_rejected(tmp_path):
    """An entry that says nothing about what to do is rejected."""
    items = [_item(id="silent", action="")]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, items)

    assert "silent" in str(excinfo.value)


def test_non_bool_required_rejected(tmp_path):
    """``required`` is a boolean, not a truthy value."""
    items = [_item(id="maybe", required="yes")]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, items)

    assert "maybe" in str(excinfo.value)


def test_external_without_target_rejected(tmp_path):
    """An outward-facing entry without a target names no one to act on."""
    item = _external_item(id="targetless")
    del item["target"]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, [item])

    assert "targetless" in str(excinfo.value)


def test_external_without_command_rejected(tmp_path):
    """An outward-facing entry without an argv cannot be whitelisted."""
    missing = _external_item(id="cmdless")
    del missing["command"]
    stringy = _external_item(id="cmdstring", command="gh pr create")
    empty = _external_item(id="cmdempty", command=[])

    for item in (missing, stringy, empty):
        with pytest.raises(SpecError) as excinfo:
            _load(tmp_path, [item])

        assert item["id"] in str(excinfo.value)


def test_external_wait_without_check_rejected(tmp_path):
    """Waiting for something outside needs to say what to check."""
    items = [_item(id="waiter", wait={"type": "external"})]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, items)

    assert "waiter" in str(excinfo.value)


def test_unknown_wait_type_rejected(tmp_path):
    """The wait vocabulary is closed."""
    items = [_item(id="waiter", wait={"type": "sometimes"})]

    with pytest.raises(SpecError) as excinfo:
        _load(tmp_path, items)

    assert "waiter" in str(excinfo.value)


def test_id_with_path_separator_rejected(tmp_path):
    """An id is a name, never a path."""
    for bad_id in ("a/b", "a\\b", "../escape", ".."):
        items = [_item(id=bad_id)]

        with pytest.raises(SpecError) as excinfo:
            _load(tmp_path, items)

        assert bad_id in str(excinfo.value), (
            f"the message must name the offending id {bad_id!r}"
        )