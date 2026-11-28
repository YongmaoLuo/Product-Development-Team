#!/usr/bin/env python3
"""Tier B behavioral goldens: write a judgment snapshot, and compare it.

Why this file exists
--------------------
§5 of the judgment-surface material asks for a *behavioral golden*: a
fixed set of inputs, run through a judgment function, with the output
frozen into a JSON file.  The golden does not claim the behavior is
right — it claims the behavior has not *changed*.  That is what lets it
cover the long tail: nobody has to write a semantic expectation for
every branch, only the inputs.

§12.1 pins how a golden is refreshed.  The repository has no
``syrupy`` and no ``pytest-snapshot``, no new dependency is allowed,
and ``--update-snapshots`` is implemented as this script.  The
contract is small on purpose:

* ``--update`` writes the snapshot file;
* ``--check`` compares what the code produces now with what the file
  holds, exits ``1`` when they differ, and prints the differing lines;
* a missing snapshot is a failure, and the message says how to make
  one;
* ``--list`` prints the registered group names.

What a group is
---------------
A *group* is a named set of cases.  :class:`Group` holds a name and a
``render`` callable that produces the group's document::

    {"cases": [{"id": "command-ok", "out": true}]}

``render`` is a callable rather than a value on purpose: a group
computes its cases from the fixed corpus when it is asked for them, so
importing this module never runs a judgment function.

The three core groups
---------------------
``evidence``, ``agent`` and ``buckets`` are registered here: they are
the three core modules the judgment-surface material names, and they
are the first goldens in the repository.

Their inputs are the fixed corpus of task 16
(``backend/tests/fixtures/judgment_corpus.py``) — the corpus's group of
the same name holds that group's cases, so every case id in the golden
can be looked up in the corpus to see what it fed in.  Their outputs
are computed by the modules under test:

``evidence``
    ``verification_evidence._ran_the_declared_entry`` — did this
    verdict run the very command the point declared?
``agent``
    ``agent.excess_declared_paths`` — which changed paths does the
    task's declaration not cover?
``buckets``
    ``verification_buckets.derive_index_buckets`` — which of the four
    buckets does this verdict land in?

The executor and the runner
---------------------------
``executor`` and ``ci_runner`` are the two groups that pin the
judgment surfaces deciding **whether the work ran at all** and
**whether it passed**: the executor (``verification_executor``, the
layer planner it shares with the task executor in ``base_executor``,
and the phase semantics in ``verification_phases``) and the CI runner
(``verification_ci_runner``).

Only pure functions are driven here.  The runner reads a junit report
from disk, so the ``ci_runner`` group builds the report **in memory**
out of the corpus's counts and hands it to the runner's own
element-level judges — the runner still decides, the script only
assembles what the runner is handed.  Nothing is started and nothing is
written.

The remaining modules
---------------------
The inventory (``docs/judgment_points.json``) names ten judgment
modules.  The groups above pin five of them; the three this file adds
are the remaining ``verification_*`` ones:

``loop``
    ``verification_loop`` — when a round's deadline has arrived, whether
    a snapshot belongs to a round the plan has left, and whether the
    round's own thread is still running.
``subagent``
    ``verification_subagent`` — which checkpoints did not pass, what
    budget a point declared, and what the sub-agent is told when its
    artifact did not satisfy the evidence contract.
``verification_agent``
    ``verification_agent`` — how the split children roll up into one
    verdict, how many attempts a point has spent, and what a result's
    evidence says when it is read back.

The same rule holds here: only pure functions are driven, and a
judgment that needs a minimal stub to be handed its input gets one.  A
stub carries the **input** — a question with a deadline, a thread that
answers whether it is alive — and never replaces the judgment itself.

The framework-fix lane
----------------------
``framework_fix`` pins the two judgments the framework-fix lane answers
with, both of them pure functions of their inputs:

``backend.framework_fix.classifier.decide``
    whether one failure blocks the plan, and what has to happen next —
    register it, reproduce it again, or escalate.
``backend.framework_fix.read_model.project_display_state``
    what a plan is displayed as right now, out of the five visible
    states.

Both are driven from the corpus's ``framework_fix`` cases, whose
``evidence`` holds the **inputs** rather than a conclusion: the facts of
one failure hit, the reproduction verdict, and the two carriers the
visible state is read from.

The inventory (``docs/judgment_points.json``) does not describe those two
modules yet: they live in a package, and the census that generates the
inventory surveys the flat ``backend/*.py`` modules.  The group is
therefore also registered in :data:`GROUPS_OUTSIDE_THE_INVENTORY` —
that is where the gap is written down.

What could not be driven is written down rather than forgotten:
:data:`COVERAGE_REPORT` names, per module, the judgment surfaces the
group freezes and the ones that cannot be (a judgment that reads the
disk, the runtime database or a live thread has no fixed input).

The coverage report
-------------------
:func:`coverage_report` answers the question the goldens alone cannot:
**which judgment points are still open**.  It reads the inventory
(:data:`MANIFEST_PATH`, whose rows record what pins each point) and the
groups this file registers, and returns the covered points, the open
tail, the modules no group covers, and any place where the inventory
and the groups disagree.

The tail is **reported, not failed**.  The goldens are a net, not a
quota: a gate that went red over the unfinished tail would be red on
day one and stay red, and a gate that is always red is a gate nobody
reads.  What *is* a failure is a judgment module with no group at all
— a module that can flip a verdict with nothing under it.

The backend modules are imported **inside** the render functions, not
at import time: importing this module must not import the backend and
must not run a judgment function (the same rule the groups themselves
follow).  A group that reads the repository says so by taking the
root as its one argument.

Determinism (§12.5, test decision point 4)
-------------------------------------------
The file is written with ``sort_keys=True`` and ``ensure_ascii=False``
and ends with exactly one newline, so two runs over the same behavior
produce the same bytes and a repeated run diffs to nothing.  The
comparison is a line-by-line comparison of that text, and the report
names the line number, so the diff in a pull request is readable.

Exit codes
----------
``0`` — the snapshot matches (or was written); ``1`` — the snapshot
differs, or is missing; ``2`` — the comparison could not be run at all
(an unknown group, an unreadable file, a file that is not JSON).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "COVERAGE_REPORT",
    "CORE_GROUPS",
    "CORPUS_PATH",
    "GROUPS_OUTSIDE_THE_INVENTORY",
    "Group",
    "MANIFEST_PATH",
    "MIN_CASES",
    "REGISTRY",
    "REMAINING_GROUPS",
    "RUNNER_GROUPS",
    "SNAPSHOT_PATH",
    "check_group",
    "coverage_report",
    "main",
    "render_group",
]

#: Where the snapshot file lives, relative to the repository root.
SNAPSHOT_PATH = "backend/tests/fixtures/judgment_snapshots.json"

#: Where the fixed corpus lives, relative to the repository root.
CORPUS_PATH = "backend/tests/fixtures/judgment_corpus.py"

#: Where the inventory of judgment points lives, relative to the
#: repository root.  Every row of it records what pins that judgment
#: point, so it is what the coverage report reads.
MANIFEST_PATH = "docs/judgment_points.json"

#: The three core modules the first goldens pin.
CORE_GROUPS = ("evidence", "agent", "buckets")

#: The executor and the CI runner — the two judgment surfaces that
#: decide whether the work ran at all and whether it passed.
RUNNER_GROUPS = ("executor", "ci_runner")

#: The remaining ``verification_*`` judgment modules — the ones the
#: inventory names and no golden covers yet.
#:
#: The group name follows the module's own name: ``verification_loop``
#: is the group ``loop`` and ``verification_subagent`` is the group
#: ``subagent``.  ``verification_agent`` keeps its prefix because the
#: short name is already taken — ``agent`` is ``agent.py``'s group.
REMAINING_GROUPS = ("loop", "subagent", "verification_agent")

#: The groups the judgment-point inventory does not describe yet.
#:
#: The inventory (``docs/judgment_points.json``) is generated by
#: ``scripts/census_judgment_points.py``, whose module list holds the
#: flat ``backend/*.py`` judgment modules.  The framework-fix lane's
#: judgments live in a **package** (``backend/framework_fix/``), and the
#: census does not survey packages — so the inventory names no module
#: for them, and a group named after a module the inventory does not
#: have cannot be resolved by the naming rule the inventory and the
#: groups share.
#:
#: This constant is that gap, written down where the groups live: the
#: group, and the files that hold the judgments it freezes.  It is a
#: **debt**, not a permission — it has to shrink to empty when the
#: census learns to survey packages, and a group that is declared here
#: while the inventory already describes it is a declaration nobody
#: removed.  ``backend/tests/static_gates/test_framework_fix_judgment_snapshot.py``
#: holds this file to that.
GROUPS_OUTSIDE_THE_INVENTORY: dict[str, tuple[str, ...]] = {
    "framework_fix": (
        "backend/framework_fix/classifier.py",
        "backend/framework_fix/read_model.py",
    ),
}

#: The smallest number of cases a core group may hold.
MIN_CASES = 6

#: The hint printed whenever the snapshot is missing or differs: the
#: reader has to be told how to make a new one, not only that there is
#: none.
_UPDATE_HINT = "运行 --update 重新生成快照。"


@dataclass(frozen=True)
class Group:
    """One snapshot group: a name, and the document it pins.

    ``name`` is the group's key in :data:`REGISTRY` and in the snapshot
    file.  ``render`` produces the group's document: a mapping with a
    ``cases`` list, every case carrying an ``id`` and an ``out``.

    ``render`` may take the repository root as its single argument —
    the tree it reads its corpus and its modules from — or no argument
    at all.  Both are called correctly; see :func:`_render_with_root`.
    """

    name: str
    render: Callable[..., Mapping[str, Any]]


# ---------------------------------------------------------------------------
# The three core groups
# ---------------------------------------------------------------------------


def _tree(root: Path | None) -> Path:
    """The repository the groups read from.

    ``None`` means "the checkout this script lives in" — the root is
    derived from the file's own location, so it is right on every
    machine and wrong nowhere.
    """
    return Path(root) if root else _default_root()


def _jsonable(value: Any) -> Any:
    """``value`` reduced to what JSON can carry.

    A golden is a JSON file, so a value the file cannot hold is a value
    nobody can diff.  A set is sorted on the way in: the order of a set
    is not a fact about the behavior, and an unstable order would make
    the file differ from itself.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    return str(value)


def _import_backend(root: Path, name: str) -> Any:
    """The backend module ``name``, from this repository's own backend.

    The module is imported from ``root/backend`` and not from wherever
    it happens to sit on ``sys.path``: a golden computed against
    another checkout's module is not this repository's golden.  The
    origin of what was imported is checked for the same reason.

    Two directories go on the path, in this order.  ``root/backend``
    comes **first**, so that ``verification`` resolves to
    ``backend/verification`` and never to a stray sibling directory at
    the repository root.  ``root`` comes second, because a few
    judgment modules reach the framework through ``backend.framework``
    (``prompts`` does) and that import needs the root on the path.
    """
    backend = (root / "backend").resolve()
    for index, entry in enumerate((str(backend), str(root.resolve()))):
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(index, entry)
    module = importlib.import_module(name)
    origin = getattr(module, "__file__", None)
    if origin:
        try:
            Path(origin).resolve().relative_to(backend)
        except ValueError:
            raise RuntimeError(
                f"模块 {name!r} 不是这个仓的：{origin}（应在 {backend} 下）"
            ) from None
    return module


def _load_corpus(root: Path) -> dict[str, list[dict]]:
    """The fixed corpus, loaded by path.

    The corpus is loaded by path rather than imported as
    ``tests.fixtures.judgment_corpus`` so that another package on
    ``sys.path`` cannot stand in for it: a golden computed from a
    stranger's corpus is not this repository's golden.
    """
    path = root / CORPUS_PATH
    if not path.exists():
        raise RuntimeError(f"固定语料不在：{path}")
    spec = importlib.util.spec_from_file_location(
        "judgment_corpus_for_snapshots", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"固定语料读不出来：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.CASES)


def _core_document(name: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """The core group's document, with the case floor enforced.

    A group of one or two rows pins a single path through a function
    and says nothing about the others.  The floor is checked here rather
    than in the comparison, so a corpus that shrank cannot quietly
    produce a golden nobody can rely on.
    """
    if len(cases) < MIN_CASES:
        raise ValueError(
            f"组 {name!r} 只有 {len(cases)} 个用例；金标至少要 {MIN_CASES} 个"
        )
    return {"cases": cases}


def _render_evidence(root: Path | None = None) -> Mapping[str, Any]:
    """The command-evidence judgment, one case per corpus case.

    The judgment is ``verification_evidence._ran_the_declared_entry`` —
    the one the first decision table pins: a command-kind point asks
    whether *this* command exited 0, and a verdict that records that
    command together with the exit code has answered it by
    construction.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    module = _import_backend(tree, "verification_evidence")
    cases: list[dict[str, Any]] = []
    for case in corpus["evidence"]:
        out = module._ran_the_declared_entry(case["vp"], case["verdict"])
        cases.append({"id": case["id"], "out": _jsonable(out)})
    return _core_document("evidence", cases)


def _render_agent(root: Path | None = None) -> Mapping[str, Any]:
    """The undeclared-write judgment, one case per corpus case.

    The judgment is ``agent.excess_declared_paths`` — the one the
    undeclared-write gate runs on: the changed paths the task's
    declaration does not cover.  It is pure (no git, no filesystem), so
    its golden is the same on every machine.

    The corpus's cases are prose ("改的是 backend/x.py") and the
    function takes two lists of paths.  The paths are read out of the
    text by the repository's own word reader
    (``verification_evidence._path_words``), so the golden pins the
    judgment rather than a second, private path parser.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    agent = _import_backend(tree, "agent")
    evidence = _import_backend(tree, "verification_evidence")
    cases: list[dict[str, Any]] = []
    for case in corpus["agent"]:
        point = case.get("vp") or {}
        verdict = case.get("verdict")
        declared = evidence._path_words(
            evidence._flatten_text(point.get("expected_result"))
        )
        actual = evidence._path_words(
            evidence._verdict_text(verdict) if isinstance(verdict, dict) else ""
        )
        out = agent.excess_declared_paths(actual, declared)
        cases.append({"id": case["id"], "out": _jsonable(out)})
    return _core_document("agent", cases)


def _verdict_shape(case: Mapping[str, Any]) -> str:
    """What kind of verdict a corpus case carries.

    The shape is what the bucketing judgment reads: the verdict's
    status, or the fact that the entry is not a verdict at all.
    """
    verdict = case.get("verdict")
    if not isinstance(verdict, dict):
        return "<not-a-verdict>"
    return f"status={verdict.get('status')!r}"


def _buckets_corpus_cases(corpus: Mapping[str, Any]) -> list[dict]:
    """The corpus cases the buckets group pins.

    The group's own corpus cases come first.  They are then topped up
    with the corpus cases that carry a verdict shape the group's own
    cases do not cover: the corpus's ``buckets`` cases all carry a
    PASSED verdict, and a golden that pins only that pins one of the
    four buckets.  The top-up walks the corpus in the corpus's own
    order and takes the first case of each shape still missing, so the
    group is the same on every run.
    """
    selected = [dict(case) for case in corpus.get("buckets", [])]
    covered = {_verdict_shape(case) for case in selected}
    for group in corpus:
        for case in corpus[group]:
            shape = _verdict_shape(case)
            if shape in covered:
                continue
            covered.add(shape)
            selected.append(dict(case))
    return selected


def _render_buckets(root: Path | None = None) -> Mapping[str, Any]:
    """The bucketing judgment, one case per selected corpus case.

    The judgment is ``verification_buckets.derive_index_buckets``: the
    whole verdict table becomes the four buckets, and a verdict that
    lands in the failed bucket is what the next round's repair tasks
    are built from.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    module = _import_backend(tree, "verification_buckets")
    cases: list[dict[str, Any]] = []
    for case in _buckets_corpus_cases(corpus):
        point = case.get("vp") or {}
        vp_id = str(point.get("id") or case.get("id"))
        out = module.derive_index_buckets({vp_id: case.get("verdict")})
        cases.append({"id": case["id"], "out": _jsonable(out)})
    return _core_document("buckets", cases)


# ---------------------------------------------------------------------------
# The executor and the CI runner
# ---------------------------------------------------------------------------
#
# The three core groups pin the judgment modules that decide **what the
# answer says**.  These two pin the ones that decide **whether the work
# ran at all and whether it passed**:
#
# ``executor``
#     ``verification_executor`` — what counts as a conclusion this
#     round (``is_round_conclusion``), what still has to be dispatched
#     (``vps_requiring_dispatch``), and what a deferred marker may and
#     may not override (``keeps_previous_conclusion``) — plus the layer
#     planner the two executors share (``base_executor._build_layers``)
#     and the phase semantics that decide which points are gates
#     (``verification_phases``).
# ``ci_runner``
#     ``verification_ci_runner`` — which sub-class an ``e2e`` point is
#     (``e2e_kind``), which lane an entry asks for (``_lane_of``), what
#     a failure's shape is (``_failure_shape``), which failures are
#     **blocking** (``_blocking_failures``), and what a gate that could
#     not decide means (``_gate_could_not_decide``).
#
# Only pure functions are driven.  The runner's own report reading needs
# a junit file on disk, so the group **builds the report in memory**
# from the corpus's counts and hands it to the runner's own element
# level judges — the runner still decides, the script only assembles
# what the runner is handed.  Nothing is started and nothing is written.


def _corpus_case(corpus: Mapping[str, Any], group: str, vp_id: str) -> dict[str, Any]:
    """One corpus case, named by the verification point it carries.

    The corpus is fixed, so a missing case means the corpus moved: the
    golden's input is gone and the golden cannot be rendered.  The error
    names the point that is missing instead of raising a bare
    ``KeyError``, so the report says which input to fix.
    """
    for case in corpus.get(group, []):
        if str((case.get("vp") or {}).get("id")) == vp_id:
            return dict(case)
    raise ValueError(f"语料里没有组 {group!r} 的验证点 {vp_id!r}")


def _with_status(verdict: Mapping[str, Any], status: str) -> dict[str, Any]:
    """The same verdict, carrying another status.

    The executor's own marker — ``DEFERRED`` means "this round has not
    run it yet" — is the one status that is not a conclusion, so the
    judgments that read it cannot be reached from a corpus case that
    only carries ``PASSED`` and ``FAILED``.  The rest of the verdict is
    the corpus's own.
    """
    out = dict(verdict)
    out["status"] = status
    return out


def _render_executor(root: Path | None = None) -> Mapping[str, Any]:
    """The executor's judgment: conclusions, dispatch, and layer order.

    The inputs are the corpus's ``executor`` cases — a verification
    point and the verdict it got — plus the corpus's ``ci_runner``
    cases, whose points are ``full_ci`` and therefore declare themselves
    to be final gates.

    The outputs are the executor's own answers: whether a verdict
    counts as a conclusion this round, what is left to dispatch, what a
    deferred marker does to an earlier conclusion, how the layers are
    ordered, and which points the phase semantics put in the gate
    layer.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    executor = _import_backend(tree, "verification_executor")
    base = _import_backend(tree, "base_executor")
    phases = _import_backend(tree, "verification_phases")

    cases = list(corpus.get("executor", []))
    passed = _corpus_case(corpus, "executor", "VP-153")["verdict"]
    failed = _corpus_case(corpus, "executor", "VP-154")["verdict"]
    deferred = _with_status(failed, "DEFERRED")

    rows: list[dict[str, Any]] = [
        {
            "id": "a-passed-verdict-is-a-round-conclusion",
            "out": _jsonable(executor.is_round_conclusion(passed)),
        },
        {
            "id": "a-failed-verdict-is-a-round-conclusion",
            "out": _jsonable(executor.is_round_conclusion(failed)),
        },
        {
            "id": "a-deferred-marker-is-not-a-round-conclusion",
            "out": _jsonable(executor.is_round_conclusion(deferred)),
        },
        {
            "id": "a-passed-verdict-needs-no-further-dispatch",
            "out": _jsonable(sorted(executor.vps_requiring_dispatch(
                ["VP-153"], {"VP-153": passed},
            ))),
        },
        {
            "id": "a-failed-verdict-needs-no-further-dispatch",
            "out": _jsonable(sorted(executor.vps_requiring_dispatch(
                ["VP-154"], {"VP-154": failed},
            ))),
        },
        {
            "id": "a-deferred-verdict-needs-another-dispatch",
            "out": _jsonable(sorted(executor.vps_requiring_dispatch(
                ["VP-154"], {"VP-154": deferred},
            ))),
        },
        {
            "id": "a-verdict-without-a-round-number-is-round-zero",
            "out": _jsonable(executor.verdict_round_no(passed)),
        },
        {
            "id": "a-later-conclusion-does-not-keep-the-earlier-one",
            "out": _jsonable(executor.keeps_previous_conclusion(passed, failed)),
        },
        {
            "id": "a-deferred-marker-in-the-same-round-tops-the-earlier-conclusion",
            "out": _jsonable(executor.keeps_previous_conclusion(passed, deferred)),
        },
    ]

    # The layer plan: the corpus's own points, each waiting for the one
    # before it.  The dependency edges are the corpus's order — the
    # layer planner is what turns that order into layers, and the
    # golden pins that it does so the same way every time.
    chain = [
            {
                "id": str((case.get("vp") or {}).get("id")),
                "depends_on": (
                    [] if index == 0
                    else [str((cases[index - 1].get("vp") or {}).get("id"))]
                ),
            }
            for index, case in enumerate(cases)
        ]
    rows.append({
        "id": "the-deferred-layer-order-is-stable",
        "out": _jsonable([
            [str(item.get("id")) for item in layer]
            for layer in base._build_layers(chain)
        ]),
    })

    # The phase semantics: the corpus's ``full_ci`` points declare
    # themselves to be final gates, so they are promoted out of Phase 1
    # and ordered as gates.
    gates = [dict(case.get("vp") or {}) for case in corpus.get("ci_runner", [])]
    rows.append({
        "id": "a-full-ci-point-is-promoted-to-the-final-gate",
        "out": _jsonable(phases.normalize_plan_phases(
            {"verification_points": gates},
        )),
    })
    core, gate_list = phases.split_by_phase(gates)
    rows.append({
        "id": "the-final-gates-keep-their-plan-order",
        "out": _jsonable([
            [str(point.get("id")) for point in core],
            [str(point.get("id")) for point in gate_list],
        ]),
    })

    return _core_document("executor", rows)


#: The junit ``classname`` a repository-local unit test carries in the
#: runner's report format.  The runner rebuilds the pytest node id out
#: of it, so the golden pins that rebuild.
_JUNIT_CLASSNAME = "backend.tests.unit.test_judgment_corpus"


def _junit_suite(case: Mapping[str, Any]) -> ET.Element:
    """The junit report the runner reads, built from a corpus case.

    The corpus declares a report as counts (``tests`` / ``failures`` /
    ``errors``); the runner reads a report as elements.  This turns the
    one into the other **in memory** — the element is the runner's own
    input format, and every judgment below is the runner's own.  The
    script only assembles what the runner is handed.
    """
    evidence = (case.get("verdict") or {}).get("evidence") or {}
    counts = evidence.get("junit") or {}
    tests = int(counts.get("tests") or 0)
    failures = int(counts.get("failures") or 0)
    errors = int(counts.get("errors") or 0)
    details = str(evidence.get("details") or "")

    suite = ET.Element("testsuite", {
        "tests": str(tests),
        "failures": str(failures),
        "errors": str(errors),
        "skipped": "0",
    })
    for index in range(tests):
        testcase = ET.SubElement(suite, "testcase", {
            "classname": _JUNIT_CLASSNAME,
            "name": details or f"test_case_{index}",
        })
        if index < failures:
            ET.SubElement(testcase, "failure", {
                "message": details or "assert 1 == 2",
            })
        elif index < failures + errors:
            ET.SubElement(testcase, "error", {
                "message": details or "the run could not decide",
            })
    return suite


def _junit_failures(runner: Any, suite: ET.Element) -> list[dict[str, Any]]:
    """The failure records the runner reads out of ``suite``.

    The records are the ones the runner itself writes; the two helpers
    that fill them are the runner's own, so both the record's shape and
    the shape's verdict come from the runner rather than from this
    script.
    """
    failures: list[dict[str, Any]] = []
    for testcase in suite.findall("testcase"):
        for kind in ("failure", "error"):
            for node in testcase.findall(kind):
                failures.append({
                    "phase": 1,
                    "node_id": runner._junit_node_id(testcase),
                    "kind": kind,
                    "first_line": runner._junit_first_line(node),
                    "shape": runner._failure_shape(node),
                })
    return failures


def _render_ci_runner(root: Path | None = None) -> Mapping[str, Any]:
    """The CI runner's judgment: kind, lane, failure shape, blocking.

    The inputs are the corpus's ``ci_runner`` cases — a ``full_ci``
    point with its entry, and the verdict with the report it read — plus
    the corpus's ``declared_entry`` case, whose point is an ``e2e``
    point that declares its sub-class.

    The outputs are the runner's own answers: which sub-class an
    ``e2e`` point is, which lane an entry asks for, which paths an
    entry reads, whether a token looks like a repository-local path,
    what a failure's shape is and whether it is **blocking**, and what a
    gate that could not decide means.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    runner = _import_backend(tree, "verification_ci_runner")
    contracts = _import_backend(tree, "ci_contract")

    failing = _corpus_case(corpus, "ci_runner", "VP-170")
    skipped = _corpus_case(corpus, "ci_runner", "VP-171")
    empty = _corpus_case(corpus, "ci_runner", "VP-172")
    named = _corpus_case(corpus, "ci_runner", "VP-173")
    path_like = _corpus_case(corpus, "ci_runner", "VP-175")
    entry_point = _corpus_case(corpus, "ci_runner", "VP-174")["vp"]

    # The corpus's ``declared_entry`` case carries the ``e2e`` point that
    # declares its sub-class; the runner reads that field and nothing
    # else.
    e2e_point = dict(
        (corpus.get("declared_entry") or [{}])[0].get("vp") or {}
    )

    def _blocking(case: Mapping[str, Any]) -> int:
        """How many of the report's failures are **blocking**."""
        suite = _junit_suite(case)
        evidence = {"junit_failures": _junit_failures(runner, suite)}
        return len(runner._blocking_failures(evidence))

    rows: list[dict[str, Any]] = [
        {
            "id": "e2e-kind-interpreted-as-command",
            "out": _jsonable(runner.e2e_kind(e2e_point)),
        },
        {
            "id": "a-point-without-an-e2e-kind-is-a-browser-flow",
            "out": _jsonable(runner.e2e_kind(failing["vp"])),
        },
        {
            "id": "the-entry-asks-for-the-whole-gate",
            "out": _jsonable(runner._lane_of(entry_point.get("ci_entry"))),
        },
        {
            "id": "a-python-test-belongs-to-the-unit-lane",
            "out": _jsonable(sorted(runner._collect_entry_paths(
                entry_point.get("ci_entry") or "",
            )[0])),
        },
        {
            "id": "a-path-like-token-is-recognized",
            "out": _jsonable(runner._looks_like_path(
                (path_like["verdict"].get("evidence") or {}).get("details") or "",
            )),
        },
        {
            "id": "a-failing-test-counts-as-a-blocking-failure",
            "out": _jsonable(_blocking(failing)),
        },
        {
            "id": "a-skipped-test-is-not-a-blocking-failure",
            "out": _jsonable(_blocking(skipped)),
        },
        {
            "id": "an-empty-report-names-no-failure",
            "out": _jsonable(_blocking(empty)),
        },
        {
            "id": "a-gate-that-could-not-decide-is-inconclusive",
            "out": _jsonable(runner._gate_could_not_decide(
                runner.FRAMEWORK_INCONCLUSIVE_EXIT_CODES[0],
                runner.parse_gate_summary("gate: INCONCLUSIVE (exit 3)"),
                contracts.CiContract(),
            )),
        },
        {
            "id": "a-failure-shape-keeps-the-test-name",
            "out": _jsonable(runner._junit_node_id(
                _junit_suite(named).find("testcase"),
            )),
        },
    ]

    return _core_document("ci_runner", rows)


# ---------------------------------------------------------------------------
# The remaining judgment modules
# ---------------------------------------------------------------------------
#
# The inventory (``docs/judgment_points.json``) names ten judgment modules.
# Five of them are pinned by the groups above; the three this section adds
# are the remaining ``verification_*`` ones.  The rule is the same one the
# other groups follow: only pure functions are driven, and a judgment that
# needs a stub to be handed its input gets one — a stub that carries the
# **input**, never one that replaces the judgment.


#: The instant the loop's deadlines are read against.  It is written down
#: so the golden does not depend on when it was run: "the deadline is in
#: the future" has to stay in the future on every machine, forever.
_LOOP_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Deadline:
    """The one field the deadline judgment reads.

    ``_deadline_has_passed`` asks a *question* for its deadline rather
    than reading a dictionary, so the input it is handed is a question.
    This is that input and nothing else — the judgment stays the
    module's own.
    """

    def __init__(self, deadline_at: Any) -> None:
        self.deadline_at = deadline_at


class _Thread:
    """A thread that answers whether it is still alive."""

    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def _render_loop(root: Path | None = None) -> Mapping[str, Any]:
    """The loop's judgments: the round's deadline, and the divergence gates.

    The inputs are the corpus's ``loop`` cases and the questions and
    snapshots they stand for; the outputs are the loop's own answers: has
    the round's deadline arrived, does a snapshot belong to a round the
    plan has left, what did the round record as its stop reason, and is
    the round's own thread still running.

    Two of the corpus's four cases name judgments that cannot be driven
    here — the blocked-by-upstream question reads ``tasks.json`` and the
    runtime database, and the divergence gates read the thread, the
    database and the plan's own row.  They are written down in
    :data:`COVERAGE_REPORT` instead.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    # ``verification_loop`` and ``server`` import each other, so the
    # server has to be in ``sys.modules`` before the loop is imported —
    # otherwise the loop's own import of the server lands in the middle
    # of the server's import of the loop.
    _import_backend(tree, "server")
    loop = _import_backend(tree, "verification_loop")

    not_reached = _corpus_case(corpus, "loop", "VP-176")
    reached = _corpus_case(corpus, "loop", "VP-177")

    rows: list[dict[str, Any]] = [
        {
            "id": "a-question-without-a-deadline-never-expires",
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline(None), _LOOP_NOW,
            )),
        },
        {
            "id": not_reached["id"],
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("2026-01-01T12:01:00+00:00"), _LOOP_NOW,
            )),
        },
        {
            "id": reached["id"],
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("2026-01-01T11:59:00+00:00"), _LOOP_NOW,
            )),
        },
        {
            "id": "a-deadline-reached-exactly-now-has-passed",
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("2026-01-01T12:00:00+00:00"), _LOOP_NOW,
            )),
        },
        {
            "id": "a-deadline-without-a-timezone-is-read-as-utc",
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("2026-01-01T12:01:00"), _LOOP_NOW,
            )),
        },
        {
            "id": "a-naive-deadline-in-the-past-has-passed",
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("2026-01-01T11:59:00"), _LOOP_NOW,
            )),
        },
        {
            "id": "a-deadline-written-in-zulu-has-passed",
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("2026-01-01T11:59:00Z"), _LOOP_NOW,
            )),
        },
        {
            "id": "an-unreadable-deadline-never-expires",
            "out": _jsonable(loop._deadline_has_passed(
                _Deadline("soon"), _LOOP_NOW,
            )),
        },
        {
            "id": "a-snapshot-from-an-earlier-round-is-an-old-verdict",
            "out": _jsonable(loop._snapshot_belongs_to_an_earlier_round(
                {"verification_round": 1}, {"round": 2},
            )),
        },
        {
            "id": "a-snapshot-from-the-live-round-is-not-an-old-verdict",
            "out": _jsonable(loop._snapshot_belongs_to_an_earlier_round(
                {"verification_round": 2}, {"round": 2},
            )),
        },
        {
            "id": "a-snapshot-without-a-round-number-cannot-be-placed",
            "out": _jsonable(loop._snapshot_belongs_to_an_earlier_round(
                {"verification_round": "1"}, {"round": 2},
            )),
        },
        {
            "id": "the-stop-reason-comes-from-the-snapshot",
            "out": _jsonable(loop._divergent_stop_reason(
                {"stop_reason": " 轮次超时 "},
            )),
        },
        {
            "id": "a-snapshot-without-a-stop-reason-gets-the-placeholder",
            "out": _jsonable(loop._divergent_stop_reason({})),
        },
        {
            "id": "a-missing-state-counts-as-a-gone-thread",
            "out": _jsonable(loop._divergence_thread_is_gone(None)),
        },
        {
            "id": "a-live-thread-is-not-gone",
            "out": _jsonable(loop._divergence_thread_is_gone(
                {"thread": _Thread(alive=True)},
            )),
        },
        {
            "id": "a-dead-thread-is-gone",
            "out": _jsonable(loop._divergence_thread_is_gone(
                {"thread": _Thread(alive=False)},
            )),
        },
    ]

    return _core_document("loop", rows)


def _render_subagent(root: Path | None = None) -> Mapping[str, Any]:
    """The sub-agent's judgments: the checkpoints, the budget, the contract.

    The inputs are the corpus's ``subagent`` cases — a point that declares
    a timeout, a point that owes an artifact it did not ship, and the
    checkpoints a browser run recorded — plus the questions those cases
    stand for.  The outputs are the sub-agent's own answers: which
    checkpoints did not pass, what budget the point declared, and what the
    sub-agent is told when the framework rejected its artifact.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    module = _import_backend(tree, "verification_subagent")
    agent = module.VerificationSubAgent(method="code_review")

    checkpoints = _corpus_case(corpus, "subagent", "VP-183")
    declared = _corpus_case(corpus, "subagent", "VP-180")
    point = dict(declared.get("vp") or {})

    # The complaints the framework made about the artifact the previous
    # attempt shipped.  They are quoted verbatim in the retry block, so
    # the block is only worth pinning when the complaints are real ones.
    agent.pending_evidence_issues = [
        "[missing_snippet] citations[0] 缺少 snippet",
    ]

    rows: list[dict[str, Any]] = [
        {
            "id": "a-checkpoint-that-passed-is-not-named",
            "out": _jsonable(module.failed_checkpoint_ids(
                [{"id": "cp-1", "passed": True}],
            )),
        },
        {
            "id": "checkpoint-summary-counts-the-failed-ones",
            "out": _jsonable(module.failed_checkpoint_ids(
                (checkpoints.get("verdict") or {}).get("evidence", {}).get(
                    "checkpoints",
                ),
            )),
        },
        {
            "id": "a-checkpoint-without-a-boolean-passed-did-not-pass",
            "out": _jsonable(module.failed_checkpoint_ids(
                [{"selector": "#result-row"}],
            )),
        },
        {
            "id": "a-checkpoint-that-is-not-a-mapping-is-skipped",
            "out": _jsonable(module.failed_checkpoint_ids(
                ["not-a-checkpoint", {"id": "cp-2", "passed": False}],
            )),
        },
        {
            "id": "a-checkpoint-with-a-written-id-answers-to-that-id",
            "out": _jsonable(module.failed_checkpoint_ids(
                [{"id": "cp-3", "passed": False}],
            )),
        },
        {
            "id": "declared-timeout-comes-from-the-point",
            "out": _jsonable(agent._declared_timeout_seconds(point)),
        },
        {
            "id": "a-point-that-declares-no-timeout-declares-nothing",
            "out": _jsonable(agent._declared_timeout_seconds({})),
        },
        {
            "id": "a-timeout-of-zero-is-not-a-declaration",
            "out": _jsonable(agent._declared_timeout_seconds(
                {"timeout_seconds": 0},
            )),
        },
        {
            "id": "a-timeout-that-is-not-a-number-is-not-a-declaration",
            "out": _jsonable(agent._declared_timeout_seconds(
                {"timeout_seconds": "soon"},
            )),
        },
        {
            "id": "the-evidence-retry-block-quotes-the-frameworks-complaints",
            "out": _jsonable(agent._evidence_retry_block({
                "verification_method": "code_review",
                "evidence_artifact_dir": "plans/demo/vp_artifacts/VP-180",
            })),
        },
        {
            "id": "the-evidence-retry-block-names-the-checkpoints-artifact",
            "out": _jsonable(agent._evidence_retry_block({
                "verification_method": "ui_validation",
                "evidence_artifact_dir": "plans/demo/vp_artifacts/VP-183",
            })),
        },
        {
            "id": "a-method-that-owes-no-evidence-has-no-complaint",
            "out": _jsonable(agent._evidence_contract_issues(
                {"verification_method": "manual_check"},
            )),
        },
        {
            "id": "a-method-that-owes-evidence-but-has-no-artifact-has-no-complaint",
            "out": _jsonable(agent._evidence_contract_issues(
                {"verification_method": "code_review"},
            )),
        },
    ]

    return _core_document("subagent", rows)


def _render_verification_agent(root: Path | None = None) -> Mapping[str, Any]:
    """The orchestrator's judgments: the split roll-up, the ledger, the evidence.

    The inputs are the corpus's ``verification_agent`` cases — a point
    whose split children came back, and a point that has been asked twice
    — plus the result envelopes the orchestrator reads.  The outputs are
    the orchestrator's own answers: the single verdict the children roll
    up into, how many attempts the ledger holds, and what a result's
    evidence says once it has been read back.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    module = _import_backend(tree, "verification_agent")
    orchestrator = module.Orchestrator

    split = _corpus_case(corpus, "verification_agent", "VP-186")
    children = (split.get("verdict") or {}).get("evidence", {}).get("results")
    parent = {"id": str((split.get("vp") or {}).get("id") or "unknown")}

    def _aggregate(results: list[dict[str, Any]]) -> Any:
        """The single verdict the split children roll up into."""
        try:
            return orchestrator._aggregate_split_results(parent, results)
        except ValueError as exc:
            return type(exc).__name__

    # The attempt ledger lives on the orchestrator, so the golden needs
    # one.  It is built without ``__init__`` — an orchestrator that has
    # asked nobody anything has an empty ledger, which is the fact the
    # judgment reads.
    ledger = object.__new__(orchestrator)

    rows: list[dict[str, Any]] = [
        {
            "id": "split-verdicts-are-aggregated-into-one",
            "out": _jsonable(_aggregate(children)),
        },
        {
            "id": "a-failed-split-child-fails-the-parent",
            "out": _jsonable(_aggregate([
                {"id": "split-1", "status": "PASSED"},
                {"id": "split-2", "status": "FAILED"},
            ])),
        },
        {
            "id": "all-skipped-split-children-leave-the-parent-split",
            "out": _jsonable(_aggregate([
                {"id": "split-1", "status": "SKIPPED"},
            ])),
        },
        {
            "id": "mixed-split-children-leave-the-parent-split",
            "out": _jsonable(_aggregate([
                {"id": "split-1", "status": "PASSED"},
                {"id": "split-2", "status": "SKIPPED"},
            ])),
        },
        {
            "id": "a-non-terminal-child-leaves-the-parent-split",
            "out": _jsonable(_aggregate([
                {"id": "split-1", "status": "timeout"},
            ])),
        },
        {
            "id": "an-empty-split-is-an-illegal-state",
            "out": _jsonable(_aggregate([])),
        },
        {
            "id": "attempt-count-comes-from-the-ledger",
            "out": _jsonable([
                ledger._note_vp_attempt("VP-185"),
                ledger._note_vp_attempt("VP-185"),
            ]),
        },
        {
            "id": "a-point-that-was-never-asked-has-no-attempt",
            "out": _jsonable(ledger._attempt_counts().get("VP-999", 0)),
        },
        {
            "id": "evidence-written-as-json-comes-back-as-a-dict",
            "out": _jsonable(module._result_evidence(
                {"evidence": '{"citations": []}'},
            )),
        },
        {
            "id": "evidence-that-is-not-json-is-not-evidence",
            "out": _jsonable(module._result_evidence({"evidence": "prose"})),
        },
        {
            "id": "evidence-that-is-not-a-mapping-is-not-evidence",
            "out": _jsonable(module._result_evidence({"evidence": "[1, 2]"})),
        },
    ]

    return _core_document("verification_agent", rows)


# ---------------------------------------------------------------------------
# The framework-fix judgment surface
# ---------------------------------------------------------------------------
#
# ``framework_fix``
#     ``backend.framework_fix.classifier`` — the double-condition blocking
#     decision (``decide``): does one failure block the plan, and what has
#     to happen next (register it, reproduce it again, escalate).
#     ``backend.framework_fix.read_model`` — the one projection of the five
#     visible states (``project_display_state``): what a plan looks like
#     from the outside right now.
#
# Both are pure functions of their inputs, which is what makes them
# freezable: the same facts give the same decision and the same visible
# state on every machine, so a golden can hold them down.


def _render_framework_fix(root: Path | None = None) -> Mapping[str, Any]:
    """The framework-fix judgments: the blocking decision and the visible state.

    The inputs are the corpus's ``framework_fix`` cases — the facts of one
    failure hit, the reproduction verdict, and the two carriers the
    visible state is read from (the plan's ``framework_fix`` block and the
    registry row).  The outputs are the two answers the framework itself
    gives: the disposition of the failure, and the state a plan is
    displayed as.

    The golden freezes **what the code returns**, not what it should
    return: the corpus declares the inputs and says nothing about the
    answer, so a change in either judgment shows up as a changed golden
    rather than as a passed expectation.

    The hit carries the two boolean facts the judgment reads —
    ``acceptance_critical`` and ``out_of_scope_change`` — and nothing
    else: the fields ``decide`` does not read are filled in empty (the
    corpus's ``vp`` id comes along because every case has one, and no
    judgment reads it either), so that a reader of the corpus does not
    mistake them for inputs of the decision.
    """
    tree = _tree(root)
    corpus = _load_corpus(tree)
    classifier = _import_backend(tree, "backend.framework_fix.classifier")
    read_model = _import_backend(tree, "backend.framework_fix.read_model")

    cases: list[dict[str, Any]] = []
    for case in corpus.get("framework_fix", []):
        evidence = (case.get("verdict") or {}).get("evidence") or {}
        facts = evidence.get("hit") or {}
        hit = classifier.FailureHit(
            vp_id=str((case.get("vp") or {}).get("id") or "") or None,
            plan_id=None,
            error_class="",
            first_error_line="",
            framework_files=(),
            acceptance_critical=bool(facts.get("acceptance_critical")),
            out_of_scope_change=bool(facts.get("out_of_scope_change")),
        )
        decision = classifier.decide(
            hit,
            reproduced=evidence.get("reproduced"),
            unclassified_rounds=int(evidence.get("unclassified_rounds") or 0),
        )
        visible = read_model.project_display_state(
            evidence.get("plan_state") or None,
            evidence.get("registry_row") or None,
        )
        cases.append(
            {
                "id": case["id"],
                "out": _jsonable(
                    {
                        "disposition": decision.disposition,
                        "blocking": decision.blocking,
                        "register": decision.register,
                        "reproduce": decision.reproduce,
                        "unclassified_rounds": decision.unclassified_rounds,
                        "display_state": visible,
                    }
                ),
            }
        )
    return _core_document("framework_fix", cases)


#: What each group covers, and what it could not.
#:
#: The key is the module, named as the inventory names it
#: (``docs/judgment_points.json``).  ``group`` is the snapshot group that
#: covers the module — one group per module, and one module per group.
#: ``pinned`` names the judgment surfaces the group freezes.  ``undriven``
#: names the ones that cannot be frozen: a judgment that reads the disk,
#: the runtime database or a live thread has no fixed input, so it is
#: written down here instead of being quietly forgotten.  Every name in
#: here is a function that really exists in the module — the test checks.
#:
#: A group's coverage is what it freezes **as a judgment**.  A helper
#: another module provides, and that the group happens to call to build
#: its input, belongs to that other module's own group — counting it here
#: would claim a cover the golden does not make.
COVERAGE_REPORT: dict[str, dict[str, Any]] = {
    "verification_evidence.py": {
        "group": "evidence",
        "pinned": (
            "_ran_the_declared_entry",
        ),
        "undriven": {},
    },
    "verification_ci_runner.py": {
        "group": "ci_runner",
        "pinned": (
            "e2e_kind",
            "_lane_of",
            "_collect_entry_paths",
            "_looks_like_path",
            "_blocking_failures",
            "_gate_could_not_decide",
            "_junit_node_id",
            "parse_gate_summary",
        ),
        "undriven": {},
    },
    "verification_buckets.py": {
        "group": "buckets",
        "pinned": (
            "derive_index_buckets",
        ),
        "undriven": {},
    },
    "verification_executor.py": {
        "group": "executor",
        "pinned": (
            "is_round_conclusion",
            "vps_requiring_dispatch",
            "verdict_round_no",
            "keeps_previous_conclusion",
        ),
        "undriven": {},
    },
    "agent.py": {
        "group": "agent",
        "pinned": (
            "excess_declared_paths",
        ),
        "undriven": {},
    },
    "verification_agent.py": {
        "group": "verification_agent",
        "pinned": (
            "_aggregate_split_results",
            "_attempt_counts",
            "_note_vp_attempt",
            "_result_evidence",
        ),
        "undriven": {
            "_apply_evidence_check": (
                "读真实产物目录与工作树 —— 没有固定的输入"
            ),
        },
    },
    "verification_loop.py": {
        "group": "loop",
        "pinned": (
            "_deadline_has_passed",
            "_snapshot_belongs_to_an_earlier_round",
            "_divergent_stop_reason",
            "_divergence_thread_is_gone",
        ),
        "undriven": {
            "_are_all_unfinished_blocked_by_failed_upstream": (
                "读 tasks.json 与运行时数据库 —— 没有固定的输入"
            ),
            "_divergence_gates_pass": (
                "读线程、运行时数据库与计划自己的行 —— 没有固定的输入"
            ),
        },
    },
    "verification_subagent.py": {
        "group": "subagent",
        "pinned": (
            "failed_checkpoint_ids",
            "_declared_timeout_seconds",
            "_evidence_retry_block",
            "_evidence_contract_issues",
        ),
        "undriven": {},
    },
}


#: The registered groups, keyed by name.
#:
#: The three core groups are the ones the judgment-surface material
#: names.  A group that renders its cases from a fixed corpus is
#: registered by the module that owns that corpus, and nothing here
#: decides what a group means.
REGISTRY: dict[str, Group] = {
    "evidence": Group("evidence", _render_evidence),
    "agent": Group("agent", _render_agent),
    "buckets": Group("buckets", _render_buckets),
    "executor": Group("executor", _render_executor),
    "ci_runner": Group("ci_runner", _render_ci_runner),
    "loop": Group("loop", _render_loop),
    "subagent": Group("subagent", _render_subagent),
    "verification_agent": Group("verification_agent", _render_verification_agent),
    "framework_fix": Group("framework_fix", _render_framework_fix),
}


# ---------------------------------------------------------------------------
# Rendering a group
# ---------------------------------------------------------------------------


def _names() -> list[str]:
    """The registered group names, in the order they are reported."""
    return sorted(REGISTRY)


def _render_with_root(render: Callable[..., Any], root: Path | None) -> Any:
    """Call ``render``, handing it the root when it asks for one.

    A group's ``render`` may take no argument — the contract the CLI
    fixed first — or one: the repository it reads its corpus and its
    modules from.  Both shapes are called correctly, so a group that
    wants the root says so by taking it.
    """
    try:
        parameters = inspect.signature(render).parameters
    except (TypeError, ValueError):  # pragma: no cover — a C callable
        return render()
    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if positional:
        return render(root)
    return render()


def _resolve_group(group: Group | str) -> Group:
    """The group named by ``group``, which may be the group itself."""
    if isinstance(group, Group):
        return group
    try:
        return REGISTRY[group]
    except KeyError:
        raise KeyError(
            f"未注册的快照组：{group!r}；已注册的组：{_names()}"
        ) from None


def _validated_document(name: str, document: Any) -> dict:
    """The group's document, checked so that it can be compared.

    A document that cannot be compared is a document nobody can read,
    so the shape is checked here rather than at the point of use: the
    ``cases`` list has to be there, every case needs an ``id`` that
    says what it pins and an ``out`` that says what came back, and the
    ids have to be unique — a duplicated id is a row a reader cannot
    find.
    """
    if not isinstance(document, Mapping):
        raise ValueError(
            f"组 {name!r} 渲染出的不是一个映射：{type(document).__name__}"
        )
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"组 {name!r} 的文档没有一个 cases 列表")

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ValueError(
                f"组 {name!r} 的第 {index + 1} 个用例不是一个映射："
                f"{type(case).__name__}"
            )
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(
                f"组 {name!r} 的第 {index + 1} 个用例没有 id："
                "每个用例都要说清它钉住的是什么"
            )
        if case_id in seen:
            raise ValueError(
                f"组 {name!r} 的用例 id {case_id!r} 重复："
                "id 是读者找到用例的办法"
            )
        if "out" not in case:
            raise ValueError(
                f"组 {name!r} 的用例 {case_id!r} 没有 out："
                "没有 out 的用例什么都没有钉住"
            )
        seen.add(case_id)
        validated.append(dict(case))
    return {"cases": validated}


def render_group(group: Group | str, root: str | Path | None = None) -> dict:
    """The snapshot document of ``group``.

    ``group`` may be a :class:`Group` or the name of one in
    :data:`REGISTRY`.  ``root`` is the repository the group reads from;
    it defaults to the checkout this script lives in.  The document is
    validated here — see :func:`_validated_document` — and a group that
    renders an unusable document raises rather than writing a golden
    nobody can read.
    """
    resolved = _resolve_group(group)
    fresh = _render_with_root(resolved.render, Path(root) if root else None)
    return _validated_document(resolved.name, fresh)


# ---------------------------------------------------------------------------
# The snapshot file
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    """The repository root, derived from this file's own location.

    ``scripts/judgment_snapshots.py`` sits one directory below the
    root, so the root is the parent of the parent of this file.
    Nothing here is written down: a path written down is a path that is
    wrong on somebody else's machine.
    """
    return Path(__file__).resolve().parents[1]


def _snapshot_path(root: Path) -> Path:
    """Where the snapshot file lives for ``root``."""
    return root / SNAPSHOT_PATH


def _render_text(document: Any) -> str:
    """The exact text a document is stored as.

    ``sort_keys`` keeps the keys in one order whatever order they were
    built in, ``ensure_ascii`` keeps the non-ASCII characters
    readable, and the single trailing newline is the end of the file.
    """
    return json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _read_document(path: Path) -> tuple[dict | None, str | None]:
    """The snapshot file's content, or the reason it cannot be read.

    A file that is missing is not an error here — it is the state the
    ``--update`` mode is there to fix.  A file that cannot be parsed is
    an error: overwriting it would throw away the other groups' goldens
    without anybody asking for that.
    """
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"快照文件读不出来：{path}：{exc}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"快照文件不是合法的 JSON：{path}：{exc}"
    if not isinstance(data, dict):
        return None, f"快照文件的内容不是一个对象：{path}"
    return data, None


def _write_document(path: Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` as the snapshot file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_text(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def _show(line: str | None) -> str:
    """One side of a difference, as it is printed."""
    if line is None:
        return "<无>"
    return line.strip()


def _diff_lines(old_text: str, new_text: str) -> list[str]:
    """The lines in which the two texts differ, named by their number.

    The two texts are compared line by line rather than as one blob,
    so the report says *which* line changed and what it changed from
    and to — a golden that only says "different" makes the reader hunt
    for the difference themselves.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    differences: list[str] = []
    for index in range(max(len(old_lines), len(new_lines))):
        old = old_lines[index] if index < len(old_lines) else None
        new = new_lines[index] if index < len(new_lines) else None
        if old == new:
            continue
        differences.append(
            f"第 {index + 1} 行：old={_show(old)} → new={_show(new)}"
        )
    return differences


#: The failures a group's rendering may raise.  Each one means the
#: comparison could not be run at all, which is exit code ``2`` — the
#: judgment moved (a renamed function), the corpus is not there, or
#: another checkout's module stood in for this one's.
_RENDER_FAILURES = (
    AttributeError,
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


def check_group(name: str, root: str | Path, update: bool = False) -> int:
    """Compare — or write — the snapshot of the group ``name``.

    The exit code is the contract: ``0`` when the snapshot matches (or
    was written), ``1`` when it differs or is missing, ``2`` when the
    comparison could not be run at all.
    """
    group = REGISTRY.get(name)
    if group is None:
        print(
            f"未注册的快照组：{name!r}；已注册的组：{_names()}",
            file=sys.stderr,
        )
        return 2

    path = _snapshot_path(Path(root))
    document, error = _read_document(path)
    if error is not None:
        print(error, file=sys.stderr)
        return 2

    try:
        fresh = render_group(group, root=root)
    except _RENDER_FAILURES as exc:
        print(f"组 {name!r} 渲染失败：{exc}", file=sys.stderr)
        return 2

    if update:
        data = dict(document or {})
        data[name] = fresh
        _write_document(path, data)
        print(f"已更新 {path}：{name}")
        return 0

    stored = (document or {}).get(name)
    if stored is None:
        print(f"快照缺失：{path} 里没有组 {name!r}。{_UPDATE_HINT}")
        return 1

    old_text = _render_text(stored)
    new_text = _render_text(fresh)
    if old_text == new_text:
        print(f"一致：{name}")
        return 0

    differences = _diff_lines(old_text, new_text)
    print(f"不一致：{name}（{len(differences)} 行不同）")
    for line in differences:
        print(line)
    print(_UPDATE_HINT)
    return 1


# ---------------------------------------------------------------------------
# The coverage report
# ---------------------------------------------------------------------------


def _manifest_points(root: Path) -> list[dict[str, Any]]:
    """The inventory's rows, as they are on disk.

    The inventory is read from ``root`` and not from wherever it happens
    to sit on ``sys.path``: a report computed against another
    checkout's inventory is not this repository's report.
    """
    path = root / MANIFEST_PATH
    if not path.is_file():
        raise RuntimeError(f"判定面清单不在：{path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"判定面清单读不出来：{path}：{exc}") from None
    points = document.get("points") if isinstance(document, dict) else None
    if not isinstance(points, list):
        raise RuntimeError(f"判定面清单里没有 points：{path}")
    return [point for point in points if isinstance(point, dict)]


def _label(point: Mapping[str, Any]) -> str:
    """How a judgment point is named in the report."""
    return f"{point.get('module')}:{point.get('function')}"


def _by_module(points: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """The inventory's rows, grouped by the module they live in."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for point in points:
        module = point.get("module")
        if isinstance(module, str):
            grouped.setdefault(module, []).append(point)
    return grouped


def _matches(
    rows: Sequence[Mapping[str, Any]], name: str
) -> list[Mapping[str, Any]]:
    """The rows of one module a surface name refers to.

    A name is either the row's own function name or the tail of a
    class-qualified one (``Orchestrator._note_vp_attempt``).  A name
    that refers to two rows refers to neither: a cover claimed for two
    points at once is not a cover.
    """
    return [
        row
        for row in rows
        if row.get("function") == name
        or str(row.get("function", "")).endswith(f".{name}")
    ]


def _judgment_modules(points: Sequence[Mapping[str, Any]]) -> list[str]:
    """The modules that make judgments, found from the inventory alone.

    A module makes judgments when it holds a point whose value can
    reach a verdict (``covers_verdict_write``) or a Tier A point — the
    material's own definition of a judgment point (§4.1): the value can
    flip a PASS into a FAIL.  A module with neither is a helper that
    happens to live next to the judgment ones, and the goldens are not
    required to cover it.
    """
    facts: dict[str, dict[str, bool]] = {}
    for point in points:
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


def _declared_pins(
    grouped: Mapping[str, Sequence[Mapping[str, Any]]]
) -> tuple[dict[str, str], list[str]]:
    """What the groups say they pin, and what does not resolve.

    The first value maps the label of each inventory point a group
    freezes to the ``pinned_by`` string the inventory is expected to
    hold for it.  The second holds the problems: a surface the
    inventory does not name, or names twice.
    """
    pins: dict[str, str] = {}
    problems: list[str] = []
    for module, entry in COVERAGE_REPORT.items():
        group = entry["group"]
        expected = f"{SNAPSHOT_PATH}::{group}"
        for name in entry["pinned"]:
            matches = _matches(grouped.get(module, []), name)
            if len(matches) == 1:
                pins[_label(matches[0])] = expected
                continue
            found = sorted(str(row.get("function")) for row in matches)
            problems.append(
                f"金标组 {group!r} 说它钉住了 {module} 的 {name!r}，"
                f"但清单里的判定点匹配到 {len(matches)} 个：{found}"
            )
    return pins, problems


def coverage_report(root: str | Path | None = None) -> dict[str, Any]:
    """What the goldens cover, and what is still open.

    The report answers the question the goldens alone cannot: **which
    judgment points are still open**.  It reads the inventory
    (:data:`MANIFEST_PATH` — every row records what pins that point)
    and the groups this file registers, and returns:

    ``covered``
        how many Tier B judgment points a golden freezes;
    ``covered_points``
        their labels (``module:function``);
    ``uncovered``
        the labels of the Tier B points no golden freezes — the long
        tail.  It is **reported, not failed**: the goldens are a net,
        not a quota, and a gate that went red over the unfinished tail
        would be red on day one and stay red;
    ``tier_a_covered`` / ``tier_a_uncovered``
        the Tier A points a golden also freezes, and the Tier A points
        that *nothing* pins — neither a decision table nor a golden.
        A Tier A point in the second list is a real gap;
    ``judgment_modules`` / ``judgment_modules_without_group``
        the modules that make judgments, and those among them that no
        group covers — the one thing in here that is a failure;
    ``modules_without_group``
        the inventory's other modules, which make no judgment;
    ``groups``
        which module each registered group covers;
    ``inconsistent``
        where the inventory's ``pinned_by`` and the groups' own
        declaration disagree.  This is a warning, never a failure: the
        two are bookkeeping about the same covers, and a drift between
        them is worth saying out loud rather than blocking a pull
        request over;
    ``undriven``
        the judgment surfaces a group declared it could not freeze.
    """
    base = _tree(Path(root) if root is not None else None)
    points = _manifest_points(base)
    grouped = _by_module(points)
    declared, problems = _declared_pins(grouped)

    covered_points: list[str] = []
    uncovered: list[str] = []
    tier_a_covered: list[str] = []
    tier_a_uncovered: list[str] = []

    for point in points:
        label = _label(point)
        if point.get("tier") == "A":
            if label in declared:
                tier_a_covered.append(label)
            elif not point.get("table"):
                tier_a_uncovered.append(label)
            continue
        if point.get("pinned_by"):
            covered_points.append(label)
        else:
            uncovered.append(label)

    # The inventory and the groups have to tell the same story about
    # what pins what.  Both directions are checked: a cover the groups
    # claim and the inventory does not record, and a pin the inventory
    # records and no group claims.
    for point in points:
        label = _label(point)
        pinned_by = point.get("pinned_by")
        expected = declared.get(label)
        if pinned_by and expected is None:
            problems.append(
                f"清单说 {label} 被 {pinned_by!r} 钉住，但没有金标组声明覆盖它"
            )
        elif pinned_by and expected != pinned_by:
            problems.append(
                f"清单说 {label} 被 {pinned_by!r} 钉住，"
                f"金标组说的是 {expected!r}"
            )
        elif expected and pinned_by != expected:
            problems.append(
                f"金标组说 {label} 被 {expected!r} 钉住，"
                f"清单里写的是 {pinned_by or '空'}"
            )

    undriven: list[str] = []
    for module, entry in COVERAGE_REPORT.items():
        for name in entry["undriven"]:
            for row in _matches(grouped.get(module, []), name):
                undriven.append(_label(row))

    modules = sorted(grouped)
    judgment_modules = _judgment_modules(points)

    return {
        "covered": len(covered_points),
        "covered_points": sorted(covered_points),
        "uncovered": sorted(uncovered),
        "tier_a_covered": sorted(tier_a_covered),
        "tier_a_uncovered": sorted(tier_a_uncovered),
        "judgment_modules": judgment_modules,
        "judgment_modules_without_group": [
            module for module in judgment_modules if module not in COVERAGE_REPORT
        ],
        "modules_without_group": [
            module for module in modules if module not in COVERAGE_REPORT
        ],
        "groups": {
            entry["group"]: module
            for module, entry in sorted(COVERAGE_REPORT.items())
        },
        "inconsistent": problems,
        "undriven": sorted(undriven),
    }


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _run_groups(root: Path, update: bool) -> int:
    """Run every registered group, and report the worst outcome."""
    results = [check_group(name, root, update=update) for name in _names()]
    if any(code == 2 for code in results):
        return 2
    return 0 if all(code == 0 for code in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Write or check the behavioral goldens.  Returns the exit code.

    * ``--update`` writes the snapshot file;
    * ``--check`` compares the snapshot with the current behavior and
      exits ``1`` when they differ — printing the differing lines, so
      that the reason is in the log and not only in the exit code;
    * ``--list`` prints the registered group names;
    * without a mode flag, the rendered documents are printed.

    ``--root`` is the repository root (the directory that holds
    ``backend/`` and ``scripts/``); it defaults to the root this script
    lives in.
    """
    parser = argparse.ArgumentParser(
        prog="judgment_snapshots.py",
        description=(
            "生成或比对判定面行为金标（Tier B 快照）。"
        ),
    )
    parser.add_argument(
        "--root",
        default=str(_default_root()),
        help="repository root (default: the root of this checkout)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--update",
        action="store_true",
        help="write the snapshot file",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a snapshot differs from the current behavior",
    )
    mode.add_argument(
        "--list",
        action="store_true",
        help="print the registered group names",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root)

    if args.list:
        print(json.dumps(_names(), ensure_ascii=False))
        return 0

    if args.update:
        return _run_groups(root, update=True)

    if args.check:
        return _run_groups(root, update=False)

    documents = {
        name: render_group(group, root=root)
        for name, group in sorted(REGISTRY.items())
    }
    print(json.dumps(documents, sort_keys=True, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())