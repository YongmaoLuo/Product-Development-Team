"""Unit tests: the pause-ux retry semantics and this branch's semantics coexist.

Background
----------
Two branches changed the scheduler's write path at the same time.

* **This branch** (``fix/scheduler-read-path``) made the read path
  cheap and the failures honest: the capacity ledger **reads before it
  writes** — it decides whether there is anything to write
  (:meth:`plan_scheduler.capacity.CapacityLedger._writes_needed`) before
  it takes SQLite's write lock — and a queue write that loses that lock
  **degrades** instead of failing
  (:func:`plan_scheduler.commands._is_locked_error`,
  :meth:`plan_scheduler.commands.CommandExecutor._degrade_locked_write`).
* **The pause-ux branch** (``fix/scheduler-pause-ux``) made the write
  path patient: the store's command writes are wrapped in a **bounded
  retry**, so a lock is waited out instead of surfacing.

The two are not rivals.  A bounded retry answers *how often do we try
again*; the degradation answers *what do we say when we give up*.  A
merge that keeps one side and drops the other loses a behaviour nobody
agreed to lose — and the merge that does it is silent: the tree still
imports, the suite is still green, and the behaviour is simply gone.

What this file holds down
-------------------------
1. **The three merge targets carry no conflict marker.**  A merge that
   was resolved by hand and left a marker in the tree is not resolved:
   the file does not even parse.
2. **This branch's own semantics are in place** — the read-before-judge
   decision and the degradation of a locked write.
3. **The retry side may not appear alone.**  The pause-ux semantics are
   **detected**, not assumed: the tree may not carry them yet, and the
   check has to be green before that branch is merged.  But when the
   tree *does* carry them, the two sides have to be there **together**.

The last rule is the point of the file, so the rules that decide it are
pinned as well: the marker detector has to see a marker, and the
coexistence rule has to be able to fail.  A rule that cannot fail
enforces nothing.

The checks read the tree's **syntax tree** for the retry side, not its
text: a word in a docstring is a word, not a behaviour.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# What the merge touches
# ---------------------------------------------------------------------------

#: The files the merge of the pause-ux branch touches, relative to the
#: root of the checkout.  These are the three files the task declares:
#: the capacity ledger, the store and the command executor.
MERGE_TARGETS: tuple[str, ...] = (
    "backend/plan_scheduler/capacity.py",
    "backend/plan_scheduler/store.py",
    "backend/plan_scheduler/commands.py",
)

#: The store — the file the pause-ux branch wraps its writes in.  This is
#: where the retry side of the merge shows up.
STORE_TARGET: str = "backend/plan_scheduler/store.py"

#: The symbols this branch owns.  ``_writes_needed`` is the
#: read-before-judge decision; ``_is_locked_error`` recognises SQLite's
#: write-lock refusal; ``_degrade_locked_write`` is what the executor
#: answers instead of failing.
PLAN_SEMANTICS: tuple[str, ...] = (
    "capacity._writes_needed",
    "commands._is_locked_error",
    "commands._degrade_locked_write",
)

#: The two of them the coexistence rule speaks about: the read-before-judge
#: decision and the recognition of a locked write.
COEXISTENCE_SEMANTICS: tuple[str, ...] = (
    "capacity._writes_needed",
    "commands._is_locked_error",
)


# ---------------------------------------------------------------------------
# Finding the checkout
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the root of the checkout this test lives in.

    The root is found by walking up from this file until the scheduler
    package turns up.  The test therefore does not care where the
    repository is checked out, and it does not have to be told: a path
    written into a test is a path that is wrong on the next machine.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "plan_scheduler" / "capacity.py").is_file():
            return parent
    raise AssertionError(
        "the checkout root was not found above {0}".format(here)
    )


def _source_of(relative: str) -> str:
    """Return the source of a file of the checkout, by its repo-relative path."""
    path = _repo_root() / relative
    assert path.is_file(), "{0} is not in the checkout".format(relative)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: The markers git leaves behind in a file it could not resolve.  The
#: middle one is a line of its own; the outer two are followed by the
#: name of the side they came from.  Git writes exactly seven of each
#: character, and the middle marker is written **alone** on its line, so
#: a separator drawn in a comment — which carries a ``#`` or an indent
#: in front of it — is not a marker.
_CONFLICT_MARKER_RE = re.compile(
    r"^(?P<marker><{7} |={7}$|>{7} |\|{7} )",
    re.MULTILINE,
)


def _conflict_markers(source: str) -> List[str]:
    """Return the conflict markers *source* carries, one entry per marker.

    A marker is recognised by its shape at the start of a line: seven
    ``<``, ``=``, ``>`` or ``|`` characters.  A separator drawn in a
    comment is indented, so it is not a marker.
    """
    return [
        "{0} at line {1}".format(
            match.group("marker"),
            source.count("\n", 0, match.start()) + 1,
        )
        for match in _CONFLICT_MARKER_RE.finditer(source)
    ]


def _name_of(node: ast.AST) -> Optional[str]:
    """Return the name a call or a reference carries, or ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _retry_markers(source: str) -> Set[str]:
    """Return the identifiers in *source* that carry a retry.

    The pause-ux branch wraps the store's command writes in a bounded
    retry, so its side of the merge shows up in the store as a **call**
    to the retry helper (``retry_on_busy(...)``) or as a **constant**
    that bounds it (``RETRY_ATTEMPTS = 3``).  Both are read off the
    syntax tree: a word in a docstring is a word, not a behaviour.
    """
    tree = ast.parse(source)
    found: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _name_of(node.func)
            if name and "retry" in name.lower():
                found.add(name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if "retry" in node.name.lower():
                found.add(node.name)
        elif isinstance(node, ast.alias):
            name = node.name or ""
            if "retry" in name.lower():
                found.add(name)
        elif isinstance(node, ast.Name):
            if "retry" in node.id.lower():
                found.add(node.id)
        elif isinstance(node, ast.arg):
            if "retry" in node.arg.lower():
                found.add(node.arg)
        elif isinstance(node, ast.Attribute):
            if "retry" in node.attr.lower():
                found.add(node.attr)

    return found


def _semantics_in_place() -> Dict[str, bool]:
    """Return, for each of this branch's own symbols, whether it is in the tree.

    The scheduler modules are imported **here** and not at the top of the
    file, on purpose: a merge target that still carries a conflict marker
    does not parse, and a module-level import would take the whole file
    down at collection — which would hide the very marker this file is
    here to report.  The check for the markers runs on the **text** of
    the files, so it can name the marker; the check for the symbols runs
    on the **modules**, so it needs them to parse.
    """
    from plan_scheduler import capacity as capacity_module
    from plan_scheduler import commands as commands_module

    return {
        "capacity._writes_needed": hasattr(
            capacity_module.CapacityLedger, "_writes_needed"
        ),
        "commands._is_locked_error": hasattr(commands_module, "_is_locked_error"),
        "commands._degrade_locked_write": hasattr(
            commands_module.CommandExecutor, "_degrade_locked_write"
        ),
    }


def _missing_sides(
    semantics: Dict[str, bool], retry_markers: Set[str]
) -> List[str]:
    """Return the semantics that have to be there for the two sides to coexist.

    The rule is one line: **the retry side may not appear alone**.  When
    the tree carries the pause-ux branch's bounded retry, it has to carry
    this branch's read-before-judge and its recognition of a locked write
    as well — a merge that kept one side and dropped the other lost a
    behaviour nobody agreed to lose.

    When the tree carries no retry side there is nothing to coexist with
    yet, and nothing is missing.
    """
    if not retry_markers:
        return []
    return [name for name in COEXISTENCE_SEMANTICS if not semantics.get(name, False)]


# ---------------------------------------------------------------------------
# 1. The merge targets are resolved
# ---------------------------------------------------------------------------


def test_no_conflict_markers_in_merge_targets() -> None:
    """The three files the merge touches carry no conflict marker.

    A merge that was resolved by hand and left a marker in the tree is
    not resolved — the file does not even parse, so the branch that
    carries it cannot run at all.  The markers are looked for in all
    three of the merge's files, because a merge resolves them one file
    at a time and a half-resolved merge is the shape this defect takes.
    """
    for relative in MERGE_TARGETS:
        source = _source_of(relative)
        markers = _conflict_markers(source)
        assert not markers, (
            "{0} carries a conflict marker: {1}. The merge was not "
            "resolved.".format(relative, "; ".join(markers))
        )


# ---------------------------------------------------------------------------
# 2. Both sides are in place
# ---------------------------------------------------------------------------


def test_both_semantics_present() -> None:
    """This branch's semantics are in place, and the retry side is never alone.

    The read-before-judge decision and the degradation of a locked write
    are this branch's own behaviour, and they are in place whichever way
    the merge went.

    The pause-ux side is **detected** rather than assumed: the tree may
    not carry the bounded retry yet, and the check has to be green
    before that branch is merged.  But once the tree does carry it, the
    two sides have to be there together — the retry may not have
    replaced the degradation.
    """
    retry_markers = _retry_markers(_source_of(STORE_TARGET))
    semantics = _semantics_in_place()

    # The headline: the retry side may not appear alone.  A tree that
    # carries the pause-ux bounded retry has to carry this branch's
    # read-before-judge and its recognition of a locked write as well.
    missing_sides = _missing_sides(semantics, retry_markers)
    assert not missing_sides, (
        "the tree carries the pause-ux retry semantics ({0}) but not {1}: a "
        "merge that keeps one side and drops the other loses a behaviour "
        "nobody agreed to lose.".format(sorted(retry_markers), missing_sides)
    )

    # And this branch's own semantics are in place whichever way the merge
    # went: the ledger decides whether it has anything to write before it
    # takes the write lock, and a queue write that lost the lock degrades
    # instead of failing.
    missing = [name for name in PLAN_SEMANTICS if not semantics.get(name, False)]
    assert not missing, (
        "this branch's own semantics are not in the tree: {0}. The merge "
        "dropped them.".format(missing)
    )


# ---------------------------------------------------------------------------
# 3. The rules have to be able to fail
# ---------------------------------------------------------------------------


def test_the_marker_rule_sees_a_marker() -> None:
    """The marker rule finds a marker, and does not fire on a clean file.

    A rule that cannot fail enforces nothing, and a marker rule that
    cannot see a marker would let an unresolved merge through the first
    test above.
    """
    marked = (
        "def reconcile():\n"
        "<<<<<<< HEAD\n"
        "    return 1\n"
        "=======\n"
        "    return 2\n"
        ">>>>>>> fix/scheduler-pause-ux\n"
    )
    clean = (
        "def reconcile():\n"
        "    # a section separator, indented\n"
        "    # ===============\n"
        "    return 1\n"
    )

    markers = _conflict_markers(marked)
    assert len(markers) == 3, (
        "the marker rule did not find the three markers: {0!r}".format(markers)
    )
    assert _conflict_markers(clean) == [], (
        "the marker rule fired on a file that carries no marker: {0!r}".format(
            _conflict_markers(clean)
        )
    )


def test_the_coexistence_rule_can_fail() -> None:
    """The coexistence rule reports the side that went missing.

    The rule is what holds the merge to keeping both sides, so it has to
    report a tree that carries the retry and has lost one of this
    branch's semantics — and it has to say nothing about a tree that
    carries no retry side yet.
    """
    both = {"capacity._writes_needed": True, "commands._is_locked_error": True}
    without_read = {"capacity._writes_needed": False, "commands._is_locked_error": True}
    without_lock = {"capacity._writes_needed": True, "commands._is_locked_error": False}

    assert _missing_sides(both, {"retry_on_busy"}) == [], (
        "the rule complained about a tree that carries both sides"
    )
    assert _missing_sides(without_read, {"retry_on_busy"}) == [
        "capacity._writes_needed"
    ], "the rule did not report the missing read-before-judge decision"
    assert _missing_sides(without_lock, {"retry_on_busy"}) == [
        "commands._is_locked_error"
    ], "the rule did not report the missing recognition of a locked write"
    assert _missing_sides(without_read, set()) == [], (
        "the rule complained about a tree that carries no retry side yet"
    )


def test_the_retry_rule_sees_a_retry() -> None:
    """The retry rule finds a retry — and only a retry.

    The whole coexistence rule hangs off this detector: if it cannot see
    the pause-ux branch's retry, the tree looks like a tree that carries
    no retry side and the rule never fires.
    """
    retrying = (
        "from .retry import retry_on_busy\n"
        "\n"
        "RETRY_ATTEMPTS = 3\n"
        "\n"
        "def record_command(conn, plan_id):\n"
        "    return retry_on_busy(lambda: conn.execute('INSERT'))\n"
    )
    patient = (
        "def record_command(conn, plan_id):\n"
        "    with conn:\n"
        "        return conn.execute('INSERT')\n"
    )

    markers = _retry_markers(retrying)
    assert markers, "the retry rule did not see the retry in a source that has one"
    assert "retry_on_busy" in markers, (
        "the retry rule did not name the retry it saw: {0!r}".format(markers)
    )
    assert _retry_markers(patient) == set(), (
        "the retry rule fired on a source that carries no retry: {0!r}".format(
            _retry_markers(patient)
        )
    )
