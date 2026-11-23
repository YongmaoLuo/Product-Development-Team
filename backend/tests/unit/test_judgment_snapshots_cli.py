"""The Tier B golden CLI: write a snapshot, compare it, and report the diff.

Why this file exists
--------------------
§5 of the judgment-surface material describes a *behavioral golden*: a
fixed set of inputs, run through a judgment function, with the output
frozen into a JSON file.  The golden does not claim the behavior is
right — it claims the behavior has not *changed*.  §12.1 pins how the
golden is refreshed: the repository has no ``syrupy`` and no
``pytest-snapshot``, no new dependency is allowed, and the refresh is a
script in this repository.

So the contract this file pins is the CLI's:

* ``--update`` writes the snapshot file, byte for byte the same way
  every time (test decision point 4: determinism);
* ``--check`` is quiet and exits ``0`` when the snapshot matches;
* ``--check`` exits ``1`` and prints the differing lines when it does
  not — and says which lines changed, so the diff in a pull request is
  readable;
* a missing snapshot is a failure that points at ``--update``;
* ``--list`` prints the registered group names, and an empty registry
  is not an error.

What this file deliberately does not do
--------------------------------------
It does not run any judgment function.  A golden's *inputs* are the
fixed corpus (a later task's job) and its *outputs* are what the
behavior produces; this CLI only has to move those bytes around
faithfully, so the tests below use a fixed document and pin the
mechanics — the writing, the comparing and the reporting.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_census_determinism.py`` loads
``scripts/census_judgment_points.py`` that way.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

#: The repository root, four parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
#: The script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "judgment_snapshots.py"
#: Where the snapshot file lives, relative to the repository root.
_SNAPSHOT_RELATIVE = Path("backend") / "tests" / "fixtures" / "judgment_snapshots.json"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs.  The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "judgment_snapshots_cli_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
snapshots = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = snapshots
_spec.loader.exec_module(snapshots)

#: The document the material's §5.2 shows: one case, one output.
SNAPSHOT: dict = {"cases": [{"id": "command-ok", "out": True}]}


@pytest.fixture()
def registry():
    """An empty registry, restored to what it was when the test ends.

    The registry is module-level state.  A test that leaves a group in
    it changes what every later test in this file — and every later
    test in the process, if the module is shared — sees, and the suite
    is run under ``-n 8`` with the shards in no fixed order (§12.4).
    So the registry is emptied here and put back afterwards.
    """
    saved = dict(snapshots.REGISTRY)
    snapshots.REGISTRY.clear()
    try:
        yield snapshots.REGISTRY
    finally:
        snapshots.REGISTRY.clear()
        snapshots.REGISTRY.update(saved)


def _snapshot_path(root: Path) -> Path:
    """Where the snapshot file of ``root`` lives."""
    return root / _SNAPSHOT_RELATIVE


def _write_snapshot(root: Path, data: dict) -> None:
    """Write ``data`` as the snapshot file of ``root``, the way the CLI does."""
    path = _snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The three cases the material names
# ---------------------------------------------------------------------------


def test_update_writes_deterministic_json(tmp_path, registry, capsys):
    """``--update`` writes the same bytes every time, and they are the JSON the contract names.

    Two groups are registered so that ``sort_keys=True`` is visible in
    the file's own bytes: the group names have to come out in
    alphabetical order, not in registration order.  One case carries a
    non-ASCII output so that ``ensure_ascii=False`` is visible too —
    the Chinese characters have to be in the file literally, not as
    ``\\uXXXX`` escapes.
    """
    registry["zeta"] = snapshots.Group(
        "zeta", lambda: {"cases": [{"id": "command-ok", "out": True}]}
    )
    registry["alpha"] = snapshots.Group(
        "alpha", lambda: {"cases": [{"id": "prose-only", "out": "只提到散文"}]}
    )

    assert snapshots.main(["--update", "--root", str(tmp_path)]) == 0

    path = _snapshot_path(tmp_path)
    text = path.read_text(encoding="utf-8")

    expected = json.dumps(
        {
            "alpha": {"cases": [{"id": "prose-only", "out": "只提到散文"}]},
            "zeta": {"cases": [{"id": "command-ok", "out": True}]},
        },
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    assert text == expected
    # Exactly one trailing newline: the file ends with a newline and
    # does not end with a blank line.
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    # The non-ASCII output is written literally.
    assert "只提到散文" in text

    # A second run over the same behavior changes nothing.
    before = path.read_bytes()
    assert snapshots.main(["--update", "--root", str(tmp_path)]) == 0
    assert path.read_bytes() == before


def test_check_passes_on_matching_snapshot(tmp_path, registry, capsys):
    """``--check`` is quiet and exits ``0`` when the snapshot matches."""
    registry["demo"] = snapshots.Group("demo", lambda: SNAPSHOT)

    assert snapshots.main(["--update", "--root", str(tmp_path)]) == 0
    capsys.readouterr()

    assert snapshots.main(["--check", "--root", str(tmp_path)]) == 0


def test_check_fails_with_line_diff(tmp_path, registry, capsys):
    """``--check`` exits ``1`` and names the line that differs.

    The golden is tampered with — the case now says the command did
    *not* pass — and the check has to say so with the line number, the
    old value and the new value, and point at ``--update``.
    """
    registry["demo"] = snapshots.Group("demo", lambda: SNAPSHOT)

    assert snapshots.main(["--update", "--root", str(tmp_path)]) == 0
    _write_snapshot(
        tmp_path,
        {"demo": {"cases": [{"id": "command-ok", "out": False}]}},
    )
    capsys.readouterr()

    assert snapshots.main(["--check", "--root", str(tmp_path)]) == 1

    out = capsys.readouterr().out
    # The differing line is named: its number, the old value, the new
    # value.  The numbers are not written down here on purpose — what
    # is pinned is the shape of the report and the two values.
    assert re.search(r"第 \d+ 行：old=.*false.* → new=.*true", out), out
    # The reader is told how to accept the new behavior.
    assert "--update" in out


# ---------------------------------------------------------------------------
# The boundary conditions the material names
# ---------------------------------------------------------------------------


def test_missing_snapshot_exits_one_and_points_at_update(tmp_path, registry, capsys):
    """A missing snapshot is a failure, and the message says how to make one."""
    registry["demo"] = snapshots.Group("demo", lambda: SNAPSHOT)

    assert snapshots.main(["--check", "--root", str(tmp_path)]) == 1

    out = capsys.readouterr().out
    assert "--update" in out


def test_list_with_no_registry_prints_empty_list_and_exits_zero(tmp_path, registry, capsys):
    """``--list`` with nothing registered prints an empty list and exits ``0``."""
    assert snapshots.main(["--list", "--root", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert out.strip() == "[]"