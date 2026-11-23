"""The goldens' coverage report — what is frozen, and what is still open.

Why this file exists
--------------------
§5 of the judgment-surface material makes the Tier B goldens the long
tail: a golden freezes a fixed set of inputs and compares the output
byte for byte, so it can stand in for a semantic expectation on every
judgment function nobody has written a decision table for.  A golden
answers "has the behavior changed", never "is the behavior right" — so
it is a safety net, not a proof, and a reader who cannot see **which**
judgment functions are still open cannot tell how much of the tail is
really pinned.

``scripts/judgment_snapshots.py::coverage_report`` is that view: it
reads the inventory (``docs/judgment_points.json``) and the groups the
script registers, and answers two questions:

* which judgment modules have no golden at all — a module that can flip
  a verdict and has no golden is a hole, and the gate is red for it;
* which judgment points are still open — the long tail.  Those are
  **reported**, not failed: the goldens are a net, not a quota, and a
  red gate over an unfinished tail would only teach everybody to
  ignore the gate.

What it pins
------------
1. ``test_every_judgment_module_has_a_snapshot_group`` — every module
   that makes judgments has a group.
2. ``test_no_tier_a_point_is_uncovered`` — no Tier A point is open: a
   Tier A point is pinned by a decision table, which is a stronger
   mechanism than a golden.
3. ``test_uncovered_is_reported_not_failed`` — the open tail is
   reported, and it does not fail anything.

The cases below those are the **supporting checks**: they pin the
report's own bookkeeping — that its two halves add up to the inventory
it reads, that every surface a group claims resolves to exactly one
judgment point, and that the inventory records the golden that pins
each point.

Two of them are **negative controls**: each breaks the tree on purpose
and requires the check above to *name the offender*.  Without them, a
check that reported nothing would pass the cases above as happily as it
passes a correct tree — and the gate would be green over a report that
names nothing at all.  A gate that cannot go red is not a gate.

Three more are negative controls of their own, in the section below:
one breaks the inventory to prove the group check names the module it
could not cover, one breaks it to prove the Tier A check names the
point nothing pins, and one breaks it to prove the report names a
disagreement between the inventory and the groups.  The disagreement is
a **warning**, never a failure: §9 of the material decides that a
manifest which does not agree yet only warns in the first rollout.

Scope, stated honestly: this gate **reads** the inventory and the
script.  It never writes either.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from pathlib import Path

#: The repository root — three parents up from ``backend/tests/static_gates/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "judgment_snapshots.py"
#: The inventory of judgment points, relative to the repository root.
_MANIFEST_RELATIVE = Path("docs") / "judgment_points.json"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs.  The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "judgment_snapshots_coverage_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
snapshots = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = snapshots
_spec.loader.exec_module(snapshots)


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def _manifest(root: Path = _REPO_ROOT) -> dict:
    """The inventory of judgment points, as it is on disk."""
    path = root / _MANIFEST_RELATIVE
    assert path.is_file(), f"判定面清单不在：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _points(manifest: dict) -> list[dict]:
    """The manifest's rows — the judgment points it names."""
    return [point for point in manifest.get("points", []) if isinstance(point, dict)]


def _label(point: dict) -> str:
    """How a judgment point is named in the report."""
    return f"{point['module']}:{point['function']}"


def _judgment_modules(manifest: dict) -> list[str]:
    """The modules that make judgments, found from the manifest alone.

    A module makes judgments when it holds a point whose value reaches
    a verdict (``covers_verdict_write``) or a Tier A point — the
    material's own definition of a judgment point (§4.1): the value can
    flip a PASS into a FAIL.  A module with neither is a helper module
    that happens to live next to the judgment ones, and the goldens are
    not required to cover it.
    """
    facts: dict[str, dict[str, bool]] = {}
    for point in _points(manifest):
        module = point.get("module")
        if not isinstance(module, str):
            continue
        entry = facts.setdefault(module, {"verdict": False, "tier_a": False})
        if point.get("covers_verdict_write"):
            entry["verdict"] = True
        if point.get("tier") == "A":
            entry["tier_a"] = True
    return sorted(
        module
        for module, entry in facts.items()
        if entry["verdict"] or entry["tier_a"]
    )


def _claimed_modules() -> dict[str, str]:
    """Which module each registered group covers, from the script itself.

    Keyed by the **module**, because that is what the inventory names:
    a group is what covers a module, not the other way round.
    """
    return {
        module: entry["group"]
        for module, entry in snapshots.COVERAGE_REPORT.items()
    }


def _modules_without_group(manifest: dict, claimed: dict[str, str]) -> list[str]:
    """The judgment modules no group covers.

    The check itself, as a function of its two inputs — so that the
    negative control below can hand it a manifest with a hole in it and
    see whether it names the hole.
    """
    return [
        module
        for module in _judgment_modules(manifest)
        if module not in claimed
    ]


# ---------------------------------------------------------------------------
# The three cases the task names
# ---------------------------------------------------------------------------


def test_every_judgment_module_has_a_snapshot_group() -> None:
    """Every module that makes judgments has a golden group.

    The groups are the long tail's only net.  A module that can flip a
    verdict and has no group is a hole in that net, and the hole is
    invisible until somebody asks — which is what this report is for.

    Three things have to hold at once: every judgment module is claimed
    by a group, every group claims a module the inventory really names,
    and no two groups claim the same module (two groups over one module
    would leave another module uncovered while looking busy).
    """
    manifest = _manifest()
    judgment_modules = _judgment_modules(manifest)
    claimed = _claimed_modules()

    assert judgment_modules, (
        "the inventory names no judgment module at all: an inventory with "
        "no judgment in it is not a census"
    )

    missing = _modules_without_group(manifest, claimed)
    assert not missing, f"这些判定模块还没有金标组：{missing}"

    modules = {point["module"] for point in _points(manifest)}
    for module, group in sorted(claimed.items()):
        assert module in modules, (
            f"金标组 {group!r} 认领的模块 {module!r} 不在清单里；"
            f"清单里的模块是 {sorted(modules)}"
        )
        assert group in snapshots.REGISTRY, (
            f"金标组 {group!r} 没有注册；已注册的组是 {sorted(snapshots.REGISTRY)}"
        )

    by_module: dict[str, list[str]] = {}
    for module, group in claimed.items():
        by_module.setdefault(module, []).append(group)
    doubled = {
        module: groups for module, groups in by_module.items() if len(groups) > 1
    }
    assert not doubled, f"这些模块被不止一个组认领：{doubled}"


def test_no_tier_a_point_is_uncovered() -> None:
    """No Tier A point is left open.

    A Tier A point is pinned by a **decision table** — a hand-written
    expectation that says what the right answer is.  That is a stronger
    mechanism than a golden, so a Tier A point that no golden freezes
    is not a gap.  A Tier A point that *nothing* pins is a gap, and it
    is the one shape this gate refuses to let pass.
    """
    manifest = _manifest()
    points = _points(manifest)
    tier_a = [point for point in points if point.get("tier") == "A"]
    assert tier_a, "the inventory names no Tier A point at all"

    report = snapshots.coverage_report(_REPO_ROOT)

    open_points = [
        _label(point) for point in tier_a if _label(point) in report["uncovered"]
    ]
    assert not open_points, f"这些 Tier A 判定点出现在未覆盖清单里：{open_points}"

    accounted = set(report["tier_a_covered"])
    for point in tier_a:
        label = _label(point)
        assert label in accounted or point.get("table"), (
            f"Tier A 判定点 {label} 什么也没有钉住它："
            "它既没有决策表，也没有金标"
        )

    assert not report["tier_a_uncovered"], (
        f"这些 Tier A 判定点什么也没有钉住：{report['tier_a_uncovered']}"
    )


def test_uncovered_is_reported_not_failed(tmp_path: Path) -> None:
    """The open tail is reported, and it does not fail anything.

    The goldens are a net, not a quota.  A gate that went red over the
    unfinished tail would be red on day one and would stay red, and a
    gate that is always red is a gate nobody reads.  So the tail is
    printed; only a judgment module with no group at all is a failure.

    The second half of the case is the negative control: a manifest
    that records **no** pinning whatsoever still produces a report.  A
    report that raised on an open point would be a report that fails
    exactly when it has the most to say.
    """
    report = snapshots.coverage_report(_REPO_ROOT)

    # The tail is said out loud on every run, and said as a warning:
    # a warning is in the log, a failure is in everybody's face.
    warnings.warn(
        f"金标覆盖：已覆盖 {report['covered']} 个 Tier B 判定点，"
        f"仍有 {len(report['uncovered'])} 个未覆盖（只告警，不阻断）",
        stacklevel=2,
    )

    assert report["uncovered"], (
        "the report names no open judgment point: a report that says "
        "nothing is open cannot be read as 'nothing is open'"
    )
    assert report["judgment_modules_without_group"] == [], (
        "这些判定模块没有金标组："
        f"{report['judgment_modules_without_group']}"
    )

    manifest = _manifest()
    for point in _points(manifest):
        point["pinned_by"] = None
    stripped = tmp_path / _MANIFEST_RELATIVE
    stripped.parent.mkdir(parents=True, exist_ok=True)
    stripped.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    quiet = snapshots.coverage_report(tmp_path)

    assert quiet["covered"] == 0, (
        f"a manifest that pins nothing still reports {quiet['covered']} "
        "covered points"
    )
    assert len(quiet["uncovered"]) > len(report["uncovered"]), (
        "a manifest that pins nothing reports fewer open points than the "
        "one that pins something: the report is not reading the manifest"
    )


# ---------------------------------------------------------------------------
# The negative controls
# ---------------------------------------------------------------------------


def test_the_group_check_names_the_module_it_could_not_cover() -> None:
    """The group check names a judgment module nothing covers.

    The negative control for the first case: an inventory holding a
    judgment module that no group claims must make the check name it.
    A check that reported nothing would pass the case above as happily
    as it passes a correct tree — and the gate would be green over a
    hole nobody had looked for.
    """
    manifest = _manifest()
    manifest["points"].append(
        {
            "module": "verification_nowhere.py",
            "function": "decides_something",
            "tier": "B",
            "table": None,
            "pinned_by": None,
            "covers_verdict_write": True,
        }
    )

    missing = _modules_without_group(manifest, _claimed_modules())

    assert missing == ["verification_nowhere.py"], (
        f"检查没有点出没有金标组的判定模块：{missing}"
    )


def test_the_tier_a_check_names_the_point_nothing_pins(tmp_path: Path) -> None:
    """The Tier A check names a point that nothing pins.

    The negative control for the second case: an inventory holding a
    Tier A point with neither a decision table nor a golden must make
    the report name it.  Without that, "no Tier A point is uncovered"
    could be true of a report that never looked.
    """
    manifest = _manifest()
    manifest["points"].append(
        {
            "module": "verification_nowhere.py",
            "function": "decides_something",
            "tier": "A",
            "table": None,
            "pinned_by": None,
            "covers_verdict_write": True,
        }
    )
    broken = tmp_path / _MANIFEST_RELATIVE
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    report = snapshots.coverage_report(tmp_path)

    assert report["tier_a_uncovered"] == [
        "verification_nowhere.py:decides_something"
    ], (
        "报告没有点出钉不住它的 Tier A 判定点："
        f"{report['tier_a_uncovered']}"
    )


def test_the_report_names_a_disagreement_between_the_manifest_and_the_groups(
    tmp_path: Path,
) -> None:
    """A disagreement is named, not hidden.

    The negative control for the warning in the case above: a manifest
    that records a pin no group declares must make the report name it.
    A warning nobody can act on is a warning nobody reads — and a
    report that swallowed the disagreement would leave the manifest
    quietly claiming a cover that is not there.
    """
    manifest = _manifest()
    manifest["points"][0]["pinned_by"] = (
        "backend/tests/fixtures/judgment_snapshots.json::no_such_group"
    )
    broken = tmp_path / _MANIFEST_RELATIVE
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    report = snapshots.coverage_report(tmp_path)

    assert report["inconsistent"], "报告没有点出清单与金标组之间的不一致"
    assert any(
        "no_such_group" in problem for problem in report["inconsistent"]
    ), f"报告没有点出是哪一个钉住关系对不上：{report['inconsistent']}"


# ---------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------


def test_the_report_counts_the_points_the_goldens_freeze() -> None:
    """The report's numbers add up to the inventory it reads.

    ``covered`` plus the open tail is the number of Tier B judgment
    points: the goldens and the tail are two halves of one set, and a
    report whose halves do not add up is a report about something else.
    The Tier A points are counted apart — a decision table is a stronger
    mechanism than a golden, so they do not belong in the tail.
    """
    manifest = _manifest()
    points = _points(manifest)
    tier_b = [point for point in points if point.get("tier") != "A"]

    report = snapshots.coverage_report(_REPO_ROOT)

    covered = [
        point for point in tier_b if _label(point) in report["covered_points"]
    ]
    assert report["covered"] == len(covered), (
        f"the report says {report['covered']} covered, but it names "
        f"{len(covered)} covered points"
    )
    assert report["covered"] + len(report["uncovered"]) == len(tier_b), (
        f"{report['covered']} covered + {len(report['uncovered'])} open "
        f"is not the {len(tier_b)} Tier B judgment points of the inventory"
    )
    assert covered, "the report says no golden covers anything"
    assert report["uncovered"], "the report says nothing is open"
    assert not {_label(point) for point in covered} & set(report["uncovered"]), (
        "a judgment point is counted as both covered and open"
    )


def test_every_declared_surface_resolves_to_one_judgment_point() -> None:
    """Every surface a group claims is one judgment point of its module.

    A name that resolves to nothing is a claim about a function that is
    not there; a name that resolves to two is a claim about two
    functions at once.  Either way the group's coverage is not a fact
    about the inventory, and the report would be counting a point that
    does not exist.
    """
    manifest = _manifest()
    functions: dict[str, list[str]] = {}
    for point in _points(manifest):
        functions.setdefault(point["module"], []).append(point["function"])

    for module, entry in snapshots.COVERAGE_REPORT.items():
        for name in entry["pinned"]:
            matches = [
                function
                for function in functions.get(module, [])
                if function == name or function.endswith(f".{name}")
            ]
            assert len(matches) == 1, (
                f"模块 {module!r} 的 {name!r} 在清单里不是唯一的判定点：{matches}"
            )


def test_the_manifest_records_the_golden_that_pins_each_point() -> None:
    """The inventory records what pins a point, and the report says so.

    The manifest is the artifact the CI gates read.  A row whose
    ``pinned_by`` is missing is a point the report cannot count, and a
    row whose ``pinned_by`` names a golden that does not pin it is a
    claim about a cover that is not there.

    A disagreement between the inventory and the groups is **reported,
    not failed**.  §9 of the material asks the open question "清单不一致
    时是红还是 WARN" and answers it for the first rollout: WARN, because
    blocking every pull request that touches a judgment module would
    teach everybody to ignore the gate.  So the disagreement is a
    warning here, and the case below it is the negative control that
    proves the warning has something to say.
    """
    manifest = _manifest()
    claimed = _claimed_modules()

    report = snapshots.coverage_report(_REPO_ROOT)

    if report["inconsistent"]:
        warnings.warn(
            "清单记下的钉住关系与金标组声明的对不上（只告警，不阻断）："
            f"{report['inconsistent']}",
            stacklevel=2,
        )

    assert report["covered"] > 0
    assert set(report["covered_points"]) <= {
        _label(point)
        for point in _points(manifest)
        if point.get("pinned_by")
    }
    assert claimed, "the script claims no module at all"
