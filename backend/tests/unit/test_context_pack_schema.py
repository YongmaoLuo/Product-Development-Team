"""Unit tests for ``backend.context_pack`` — the per-round read-only context pack.

What this pins
--------------
Two contracts:

* **The schema** (architecture decision point 6): the pack carries exactly
  the eight whitelisted environment fields plus ``generated_at`` and
  ``schema_version`` — no more, no fewer. A field that could not be
  collected is written as ``null`` rather than omitted, so a reader can
  tell "this fact is absent" from "this fact was never asked for".
* **The credential whitelist** (test-design decision point 10): the pack
  is a new persistence surface inside the plan directory, and a plan
  directory can be packaged, uploaded or swept into an analysis artifact.
  So nothing that looks like a credential may land in it.

The credential scan below uses its **own** literals rather than the
writer's patterns on purpose: a test that shares the implementation's
regexes cannot catch the implementation getting those regexes wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from context_pack import (  # noqa: E402
    CONTEXT_PACK_FIELDS,
    SCHEMA_VERSION,
    build_context_pack,
)


#: Literal shapes the upstream credential scan forbids. Matched
#: case-insensitively: the scan is about the *shape* of a credential, not
#: about the spelling a writer happened to pick.
CREDENTIAL_SHAPES = ("token=", "authorization", "cookie", "://user:pass@")


def _build(
    tmp_path: Path,
    *,
    plan_id: str = "p1",
    round_no: int = 1,
    project_dir: Path | None = None,
    focused_test_command: str = "pytest -q",
) -> Path:
    """Build one pack in a throwaway plans tree and return its path."""
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    if project_dir is None:
        project_dir = tmp_path / "wt"
        project_dir.mkdir(parents=True, exist_ok=True)
    return build_context_pack(
        plan_id=plan_id,
        round_no=round_no,
        plan_dir=plan_dir,
        project_dir=project_dir,
        focused_test_command=focused_test_command,
    )


def _load(pack: Path) -> dict:
    return json.loads(pack.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The whitelist
# ---------------------------------------------------------------------------


def test_keys_equal_whitelist(tmp_path: Path) -> None:
    """The key set is the whitelist plus the two metadata keys — no more."""
    data = _load(_build(tmp_path))

    assert set(data) == set(CONTEXT_PACK_FIELDS) | {"generated_at", "schema_version"}
    assert len(CONTEXT_PACK_FIELDS) == 8


def test_whitelist_is_the_eight_agreed_fields_in_order(tmp_path: Path) -> None:
    """The whitelist is fixed and ordered — the brief references it by position."""
    assert CONTEXT_PACK_FIELDS == (
        "worktree_abs_path",
        "branch",
        "base_commit",
        "diff_stat",
        "changed_files",
        "plan_dir",
        "service_ports",
        "focused_test_command",
    )


def test_whitelisted_fields_come_first_and_in_order(tmp_path: Path) -> None:
    """The file lists the eight fields first, in whitelist order."""
    data = _load(_build(tmp_path))

    assert list(data)[: len(CONTEXT_PACK_FIELDS)] == list(CONTEXT_PACK_FIELDS)


# ---------------------------------------------------------------------------
# Missing fields
# ---------------------------------------------------------------------------


def test_missing_field_is_null(tmp_path: Path) -> None:
    """A field that could not be collected is ``null``, not absent."""
    # A directory that is not a git repository: every git-derived fact
    # is unavailable, and all of them must still be present as keys.
    pack = _build(tmp_path, project_dir=tmp_path / "not-a-repo")
    data = _load(pack)

    assert "diff_stat" in data
    assert data["diff_stat"] is None
    assert data["branch"] is None
    assert data["base_commit"] is None
    assert data["changed_files"] is None


def test_worktree_path_is_absolute(tmp_path: Path) -> None:
    """The worktree fact is an absolute path, wherever the caller ran from."""
    data = _load(_build(tmp_path))

    assert Path(data["worktree_abs_path"]).is_absolute()
    assert data["worktree_abs_path"] == str((tmp_path / "wt").resolve())


def test_plan_dir_field_names_the_plan_directory(tmp_path: Path) -> None:
    """``plan_dir`` is the plan's own directory, absolute."""
    data = _load(_build(tmp_path))

    assert data["plan_dir"] == str((tmp_path / "plans" / "p1").resolve())


# ---------------------------------------------------------------------------
# Service ports
# ---------------------------------------------------------------------------


def test_service_ports_shape(tmp_path: Path) -> None:
    """Every port entry is an integer plus a purpose label — nothing else."""
    pack = _build(tmp_path, focused_test_command="pytest --port 8765 -q")
    data = _load(pack)

    ports = data["service_ports"]
    assert isinstance(ports, list)
    assert ports, "the focused command names a port, so the pack must list it"

    for entry in ports:
        assert set(entry) == {"port", "purpose"}
        assert isinstance(entry["port"], int)
        assert isinstance(entry["purpose"], str)
        assert entry["port"] > 0

    assert 8765 in {entry["port"] for entry in ports}


def test_service_ports_carry_no_configuration_text(tmp_path: Path) -> None:
    """The purpose label is a label, not a snippet of a config file."""
    pack = _build(tmp_path, focused_test_command="pytest --port 8765 -q")
    data = _load(pack)

    for entry in data["service_ports"]:
        assert "\n" not in entry["purpose"]
        assert "=" not in entry["purpose"]
        assert len(entry["purpose"]) <= 64


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_no_credentials(tmp_path: Path) -> None:
    """Nothing credential-shaped reaches the file, whatever the inputs say."""
    pack = _build(
        tmp_path,
        focused_test_command=(
            "pytest --token=abc123 -q && "
            "curl -H 'Authorization: Bearer xyz' "
            "http://user:pass@example.invalid/x"
        ),
    )

    text = pack.read_text(encoding="utf-8").lower()
    for shape in CREDENTIAL_SHAPES:
        assert shape not in text, f"{shape!r} reached the context pack"


def test_no_credentials_in_a_plain_pack(tmp_path: Path) -> None:
    """The ordinary pack is credential-free too, not just the poisoned one."""
    text = _build(tmp_path).read_text(encoding="utf-8").lower()

    for shape in CREDENTIAL_SHAPES:
        assert shape not in text


# ---------------------------------------------------------------------------
# Storage layout and idempotency
# ---------------------------------------------------------------------------


def test_storage_layout(tmp_path: Path) -> None:
    """The pack lands at ``<plan_dir>/context/round-<n>.json``."""
    pack = _build(tmp_path, round_no=2)

    assert pack == tmp_path / "plans" / "p1" / "context" / "round-2.json"
    assert pack.is_file()


def test_each_round_gets_its_own_file(tmp_path: Path) -> None:
    """Round 1 and round 2 are two files, not one overwritten file."""
    first = _build(tmp_path, round_no=1)
    second = _build(tmp_path, round_no=2)

    assert first.name == "round-1.json"
    assert second.name == "round-2.json"
    assert first.parent == second.parent
    assert sorted(p.name for p in first.parent.iterdir()) == [
        "round-1.json",
        "round-2.json",
    ]


def test_same_round_is_not_rewritten(tmp_path: Path) -> None:
    """A second call for the same round neither adds a file nor rewrites one."""
    pack = _build(tmp_path, round_no=2)

    sentinel = '{"sentinel": true}\n'
    pack.write_text(sentinel, encoding="utf-8")

    again = _build(tmp_path, round_no=2)

    assert again == pack
    assert pack.read_text(encoding="utf-8") == sentinel
    assert [p.name for p in pack.parent.iterdir()] == ["round-2.json"]


def test_generated_at_and_schema_version_are_present(tmp_path: Path) -> None:
    """The two metadata keys are always there, with usable values."""
    data = _load(_build(tmp_path))

    assert data["generated_at"]
    assert data["schema_version"] == SCHEMA_VERSION
