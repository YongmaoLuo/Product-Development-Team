"""The manifest writer: the same tree must always produce the same bytes.

Why this file exists
--------------------
The material (§6.1) asks for a **re-runnable** census: CI re-runs the
script and compares what it produces with the manifest that lives in the
repository. A manifest that carried a timestamp, or whose rows came out
in whatever order the walker happened to meet them, would make that
comparison report a difference on *every* run. A gate that is noise is a
gate nobody reads — and this gate is the one that says "the judgment
surface changed".

So the contract is pinned here by name:

* ``test_render_is_byte_stable``
* ``test_points_sorted_by_module_function``
* ``test_check_reports_drift``
* ``test_write_then_check_is_clean``

The first two pin the determinism rule the material states (§12.5: sort
by ``(module, function)``, and a repeated run must diff to nothing). The
last two pin the drift check itself: a manifest that does not match the
tree has to be *reported*, not ignored — and a manifest that does match
must stay quiet, or the check would cry wolf on every commit.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_census_return_shape.py`` and
``test_census_tier_prelabel.py`` load their script that way.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

#: The repository root, four parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
#: The census script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "census_judgment_points.py"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``. It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs. The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "census_judgment_points_determinism_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
census = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = census
_spec.loader.exec_module(census)


def _synthetic_root(tmp_path: Path) -> Path:
    """A throw-away tree carrying one of the judgment modules.

    The write/check tests use it instead of the repository so that they
    never write into the tree they are testing, and so that they do not
    depend on how many functions the judgment modules happen to have.
    """
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "verification_evidence.py").write_text(
        "def verdict_subject_mismatch() -> bool:\n"
        "    return False\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# The four cases the material names
# ---------------------------------------------------------------------------


def test_render_is_byte_stable():
    """The same tree renders to the same bytes, every time.

    Two separate builds are compared, not one build rendered twice: the
    question is whether the *census* is reproducible, not whether
    ``json.dumps`` is deterministic. The rendered text ends with exactly
    one newline, and the shape carries no timestamp — a timestamp would
    make the drift check report a difference that says nothing about
    the code.
    """
    manifest = census.build_manifest(_REPO_ROOT)
    first = census.render_manifest(manifest)
    second = census.render_manifest(census.build_manifest(_REPO_ROOT))

    assert first == second
    assert first.endswith("\n")
    assert not first.endswith("\n\n")

    assert set(manifest) == {"schema_version", "points"}
    timestamp_keys = {"generated_at", "timestamp", "created_at", "updated_at"}
    for point in manifest["points"]:
        assert not timestamp_keys & set(point)


def test_points_sorted_by_module_function():
    """The rows come out sorted by ``(module, function)`` (§12.5).

    The sort is what makes a re-run diff to nothing: the order of the
    walk depends on the source layout, the order of the manifest does
    not. The keys are unique as well — two rows with the same key would
    name the same function twice, and a row has to name *one* function.
    """
    manifest = census.build_manifest(_REPO_ROOT)

    assert manifest["schema_version"] == 1
    keys = [
        (point["module"], point["function"]) for point in manifest["points"]
    ]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    assert keys, "the census found no judgment point at all"


def test_check_reports_drift(tmp_path: Path, capsys):
    """A manifest that does not match the tree is reported, and fails.

    The check has to be able to say *no*: a writer that always agrees
    with itself catches nothing, and a check that never fails is worse
    than no check at all, because it looks like one.
    """
    root = _synthetic_root(tmp_path)
    out = tmp_path / "docs" / "judgment_points.json"

    assert census.main(["--root", str(root), "--write", "--out", str(out)]) == 0

    # One byte of difference: the manifest no longer says what the
    # census says.
    out.write_text(out.read_text(encoding="utf-8") + " ", encoding="utf-8")

    assert census.main(["--root", str(root), "--check", "--out", str(out)]) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_write_then_check_is_clean(tmp_path: Path):
    """Writing the manifest and then checking it finds no drift.

    This is the CI path in miniature: the writer and the checker have
    to agree with each other, or the drift check would report a
    difference on a tree nobody touched.
    """
    root = _synthetic_root(tmp_path)
    out = tmp_path / "docs" / "judgment_points.json"

    assert census.main(["--root", str(root), "--write", "--out", str(out)]) == 0
    assert census.main(["--root", str(root), "--check", "--out", str(out)]) == 0


# ---------------------------------------------------------------------------
# The rest of the published interface
# ---------------------------------------------------------------------------


def test_manifest_rows_carry_the_published_fields():
    """A row names the module, the function, and everything the gates read.

    The fields are the material's own (§6.1 and §4.3): ``module`` and
    ``function`` say *which* function, ``return_shape`` says what it can
    return, ``tier`` says whether it is a confirmed judgment point,
    ``covers_verdict_write`` says whether its value reaches a verdict,
    and ``table`` / ``pinned_by`` say what pins it. The last two are
    ``None`` until the decision tables land — a row that claims a table
    nobody has written would be a promise the repository cannot keep.
    """
    manifest = census.build_manifest(_REPO_ROOT)
    point = manifest["points"][0]

    assert set(point) == {
        "module",
        "function",
        "tier",
        "table",
        "return_shape",
        "pinned_by",
        "covers_verdict_write",
    }


def test_missing_manifest_fails_the_check(tmp_path: Path, capsys):
    """A check against a manifest that is not there fails.

    "There is no manifest" and "the manifest matches" are different
    facts. Reading a missing file as a match would let the manifest
    disappear without anybody noticing.
    """
    root = _synthetic_root(tmp_path)
    out = tmp_path / "docs" / "judgment_points.json"

    assert census.main(["--root", str(root), "--check", "--out", str(out)]) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_written_manifest_ends_with_one_newline(tmp_path: Path):
    """The file the writer leaves behind ends with exactly one newline."""
    root = _synthetic_root(tmp_path)
    out = tmp_path / "docs" / "judgment_points.json"

    assert census.main(["--root", str(root), "--write", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
