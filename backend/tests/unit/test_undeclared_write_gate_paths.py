"""The git-level change set must not contain paths the task never declared.

Material R1 asks the executor to compare the ACTUAL change set (what git
says changed) against the task's ``files_to_modify`` before the task is
allowed to complete. The existing tool guard cannot answer that
question: :meth:`AutonomousAgent._take_undeclared_writes` only reads the
Edit/Write refusal marker, so a write performed by a shell command never
reaches it at all.

This file pins the pure RULE that comparison needs — no git, no IO:

* a path that changed but was not declared is reported;
* an unusable declaration (``None`` / empty / either sentinel) disables
  the check entirely, because "no declaration" is not "a declaration of
  the empty set";
* runtime artefacts (plan bookkeeping, scratch space, lock and log
  files) are NOT undeclared writes — flagging them would make the gate
  fire on the runtime's own bookkeeping and get it switched off.

The caller (a later task) wires this to the real git change set; this
file only fixes the rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import excess_declared_paths  # noqa: E402


def test_excess_paths_are_reported():
    """A changed-but-undeclared path is named; declared ones are not."""
    excess = excess_declared_paths(
        ["backend/agent.py", "scripts/ci_gate.sh", "plans/P-1/tasks.json"],
        ["backend/agent.py"],
    )
    assert excess == ["scripts/ci_gate.sh"]


def test_excess_paths_are_empty_when_nothing_changed():
    """No change set means nothing to report."""
    assert excess_declared_paths([], ["backend/agent.py"]) == []


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="empty-list"),
        pytest.param(["__UNKNOWN_MODIFICATIONS__"], id="unknown-sentinel"),
        pytest.param(["__NO_FILE_CHANGES__"], id="no-changes-sentinel"),
    ],
)
def test_missing_or_empty_declaration_disables_the_check(declaration):
    """An unusable declaration disables the check — it flags nothing.

    "The task declared nothing" and "the task declared the empty set"
    are different statements, and only the second one would make every
    changed path an excess path. Treating them the same would make the
    gate fire on every task whose ``files_to_modify`` was never filled
    in — which is exactly the task shape the runtime already treats as
    read-only.
    """
    assert excess_declared_paths(["backend/agent.py"], declaration) == []


def test_plan_and_tmp_artifacts_are_excluded():
    """Plan bookkeeping and scratch space are runtime artefacts."""
    assert (
        excess_declared_paths(
            ["plans/P-1/tasks.json", "tmp/x.py", ".pdt/state.json"],
            ["backend/agent.py"],
        )
        == []
    )


def test_locks_and_logs_are_excluded():
    """Lock and log files are runtime artefacts whatever their directory."""
    assert (
        excess_declared_paths(
            ["tasks.lock", "run.log", "backend/agent.py.lock"],
            ["backend/agent.py"],
        )
        == []
    )


def test_paths_are_normalised_and_deduplicated():
    """``./a.py`` and ``a.py`` are one path, and ``\\`` is a separator."""
    assert excess_declared_paths(["./a.py", "a.py", "b.py"], ["a.py"]) == ["b.py"]
    assert excess_declared_paths(["a\\b.py"], ["a/b.py"]) == []
