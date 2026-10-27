"""Unit tests for ``backend.context_pack`` — path safety of the writer.

What this pins
--------------
The context pack is written into a directory named by an id that arrives
from a request or from plan state, and a plan directory is a place the
operator's files live. So the writer has to refuse an id that would put
the file somewhere else, and it has to refuse **before** it creates
anything: a rejected id must not leave an empty directory behind as
evidence of the attempt.

The three shapes that matter:

* ``../etc`` — a traversal out of the plans tree;
* an absolute path — the same escape, spelled differently;
* a symlink pointing outside the plans tree — an escape that looks like a
  perfectly ordinary name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from context_pack import (  # noqa: E402
    ContextPackPathError,
    build_context_pack,
)


def _plans_dir(tmp_path: Path) -> Path:
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir


def test_path_traversal_rejected(tmp_path: Path) -> None:
    """``../etc`` is refused, and nothing is created on the way out."""
    plans_dir = _plans_dir(tmp_path)

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id="../etc",
            round_no=1,
            plan_dir=plans_dir / ".." / "etc",
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert not (tmp_path / "etc").exists()
    assert list(plans_dir.iterdir()) == []


def test_absolute_plan_id_rejected(tmp_path: Path) -> None:
    """An absolute id is refused for the same reason a traversal is."""
    plans_dir = _plans_dir(tmp_path)

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id="/etc",
            round_no=1,
            plan_dir=plans_dir / "etc",
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert not (tmp_path / "etc").exists()
    assert list(plans_dir.iterdir()) == []


def test_nested_plan_id_rejected(tmp_path: Path) -> None:
    """An id carrying a separator is refused — a plan id is one segment."""
    plans_dir = _plans_dir(tmp_path)

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id="nested/plan",
            round_no=1,
            plan_dir=plans_dir / "nested" / "plan",
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert not (plans_dir / "nested").exists()


@pytest.mark.parametrize(
    "plan_id",
    [
        "../etc",
        "..",
        ".",
        "/etc",
        "nested/plan",
        "back\\slash",
        "",
        "   ",
        "p1\x00",
    ],
)
def test_unsafe_plan_id_is_refused(tmp_path: Path, plan_id: str) -> None:
    """No id that is not a single safe segment ever reaches the disk.

    The whole class is pinned here rather than one spelling of it: the
    writer has more than one guard (the id itself, the directory it names,
    and the containment of the resolved path), and the contract is about
    the *outcome* — refused, and nothing created — not about which guard
    caught it.
    """
    plans_dir = _plans_dir(tmp_path)

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id=plan_id,
            round_no=1,
            plan_dir=plans_dir / plan_id,
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert list(plans_dir.iterdir()) == []


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    """A symlink whose target is outside the plans tree is refused."""
    plans_dir = _plans_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = plans_dir / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id="escape",
            round_no=1,
            plan_dir=link,
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert not (outside / "context").exists()
    assert list(outside.iterdir()) == []


def test_context_dir_symlink_escape_rejected(tmp_path: Path) -> None:
    """A symlink planted where the ``context`` directory goes is refused too."""
    plans_dir = _plans_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    plan_dir = plans_dir / "p1"
    plan_dir.mkdir()
    (plan_dir / "context").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id="p1",
            round_no=1,
            plan_dir=plan_dir,
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert list(outside.iterdir()) == []


def test_plan_dir_must_be_the_plans_own_directory(tmp_path: Path) -> None:
    """The plan directory has to be the directory named by the plan id."""
    plans_dir = _plans_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(ContextPackPathError):
        build_context_pack(
            plan_id="p1",
            round_no=1,
            plan_dir=elsewhere,
            project_dir=tmp_path,
            focused_test_command="pytest -q",
        )

    assert list(elsewhere.iterdir()) == []


def test_valid_plan_id_resolves_inside_the_plans_dir(tmp_path: Path) -> None:
    """An ordinary id lands inside the plans tree."""
    plans_dir = _plans_dir(tmp_path)

    pack = build_context_pack(
        plan_id="p1",
        round_no=2,
        plan_dir=plans_dir / "p1",
        project_dir=tmp_path,
        focused_test_command="pytest -q",
    )

    resolved_plans = plans_dir.resolve()
    assert resolved_plans in pack.resolve().parents
    assert pack == plans_dir / "p1" / "context" / "round-2.json"
