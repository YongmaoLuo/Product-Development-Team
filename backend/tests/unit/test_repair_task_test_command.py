"""
Every generated repair task must ship with a usable ``test_command``.

Background
--------------------------------------
``_generate_repair_tasks`` used to finish with
``task.setdefault("test_command", "")`` — a no-op safety net, because the
key was absent and ``setdefault`` happily supplied the empty string. All
four ``repair-r4-*`` tasks in that plan reached the executor with
``test_command = NULL``. The two that "completed" did so on the
subagent's self-report alone (``test_cross_verify_unverified``), which is
precisely the single-signal mode the dual-criterion completion rule
exists to prevent.

These tests pin the resolution order and, importantly, that a command the
quality checker rejects is NOT shipped: a structurally-broken command
guarantees a false failure on every attempt, whereas no command degrades
to the audit second pass — which can actually pass.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from repair_generator import RepairTaskGenerator  # noqa: E402


PROBE_CHAIN = (
    "cd ~/Documents/x && grep -n 'TODO' src/types.rs && "
    "ls -la target/lib.dylib 2>/dev/null && "
    "ls -la venv/lib/*.so 2>/dev/null && "
    "source venv/bin/activate && python3 -c \"import x\""
)


class _ScriptedTool:
    """``coding_tool`` double returning a fixed ``{"tasks": [...]}`` reply."""

    def __init__(self, tasks):
        self.calls = 0
        self._tasks = tasks

    def query_json(self, **kwargs):
        self.calls += 1
        return {"tasks": list(self._tasks)}


def _evidence(vp_id="VP-013", test_command="cargo test --test x"):
    return [{
        "vp_id": vp_id,
        "actual_result": "assert 0 == 1",
        "evidence": "tests/test_x.py::test_y",
        "test_command": test_command,
    }]


def _generator(tmp_path: Path, tool) -> RepairTaskGenerator:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return RepairTaskGenerator(tool, plan_dir, project_dir)


def _generate(tmp_path, tasks, evidence=None):
    gen = _generator(tmp_path, _ScriptedTool(tasks))
    return gen._generate_repair_tasks(
        requirement_context={},
        evidence_items=evidence if evidence is not None else _evidence(),
        round_number=4,
    )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_llm_authored_command_is_kept(tmp_path):
    tasks = _generate(tmp_path, [{
        "title": "fix it",
        "description": "d",
        "test_command": "cargo test --test signal_classification",
    }])
    assert tasks[0]["test_command"] == (
        "cargo test --test signal_classification"
    )


def test_missing_command_is_recovered_from_the_named_vp(tmp_path):
    tasks = _generate(tmp_path, [{
        "title": "fix it",
        "description": "d",
        "vp_id": "VP-013",
    }])
    assert tasks[0]["test_command"] == "cargo test --test x", (
        "the vp_id was available and the VP has a command; the task "
        "must not ship unverifiable"
    )


def test_missing_command_with_a_single_failed_vp_is_recovered(tmp_path):
    """One failed VP makes the task unambiguously about that VP."""
    tasks = _generate(tmp_path, [{
        "title": "fix it",
        "description": "d",
    }])
    assert tasks[0]["test_command"] == "cargo test --test x"


def test_missing_command_with_ambiguous_evidence_stays_empty(tmp_path):
    """Two failed VPs and no ``vp_id`` — guessing would be worse.

    The task still ships (the repair has to happen); it is surfaced as
    unverifiable rather than given an arbitrary command.
    """
    evidence = [
        {"vp_id": "VP-001", "actual_result": "a", "evidence": "e",
         "test_command": "pytest tests/a.py"},
        {"vp_id": "VP-002", "actual_result": "b", "evidence": "e",
         "test_command": "pytest tests/b.py"},
    ]
    tasks = _generate(tmp_path, [{
        "title": "fix it", "description": "d",
    }], evidence=evidence)
    assert tasks[0]["test_command"] == ""


def test_an_unusable_llm_command_is_dropped_not_shipped(tmp_path):
    """A probe chain guarantees a false failure; empty does not.

    Shipping ``PROBE_CHAIN`` would have the gate score every honest
    ``TEST_RESULT: PASSED`` as a lie — the probe-chain trap.
    """
    tasks = _generate(tmp_path, [{
        "title": "fix it",
        "description": "d",
        "test_command": PROBE_CHAIN,
    }])
    assert tasks[0]["test_command"] != PROBE_CHAIN
    # Falls through to the single-evidence derivation.
    assert tasks[0]["test_command"] == "cargo test --test x"


def test_an_unusable_llm_command_with_no_fallback_becomes_empty(tmp_path):
    evidence = [
        {"vp_id": "VP-001", "actual_result": "a", "evidence": "e",
         "test_command": "pytest tests/a.py"},
        {"vp_id": "VP-002", "actual_result": "b", "evidence": "e",
         "test_command": "pytest tests/b.py"},
    ]
    tasks = _generate(tmp_path, [{
        "title": "fix it", "description": "d", "test_command": PROBE_CHAIN,
    }], evidence=evidence)
    assert tasks[0]["test_command"] == ""


def test_whitespace_only_command_counts_as_missing(tmp_path):
    tasks = _generate(tmp_path, [{
        "title": "fix it", "description": "d", "test_command": "   ",
    }])
    assert tasks[0]["test_command"] == "cargo test --test x"


def test_source_is_not_written_onto_the_task_dict(tmp_path):
    """``add_task`` rejects keys outside its allow-list.

    ``test_command_source`` has no column, so persisting it would raise
    ``TaskProgressValidationError`` and break the single-writer path.
    """
    from state_machine.repositories.plan_task_repository import (
        ALLOWED_STATIC_TASK_FIELDS,
    )

    tasks = _generate(tmp_path, [{"title": "fix it", "description": "d"}])
    extra = set(tasks[0]) - ALLOWED_STATIC_TASK_FIELDS - {
        "status", "updated_time", "failure_reason",
    }
    assert not extra, (
        f"generated repair task carries fields add_task would reject: "
        f"{sorted(extra)}"
    )


def test_every_generated_task_has_the_key_present(tmp_path):
    """The key must exist even when its value is empty.

    ``setdefault(..., "")`` was the old bug: it made an absent field look
    handled. The contract is now explicit — the key is always set, and
    emptiness means "nothing derivable", which the caller reports.
    """
    tasks = _generate(tmp_path, [
        {"title": "a", "description": "d", "vp_id": "VP-013"},
        {"title": "b", "description": "d", "vp_id": "VP-013"},
    ])
    assert all("test_command" in t for t in tasks)
    assert all(t["test_command"] for t in tasks)


# ---------------------------------------------------------------------------
# The pre-repair probe
# ---------------------------------------------------------------------------
#
# ``inspect_command`` reads the command's *shape* and cannot see whether it
# discriminates. A well-formed command that already exits 0 before any
# repair runs makes the exit-code half of the completion verdict carry no
# information at all. The probe closes that gap by running the candidate
# against the current tree.

#: Well-formed, and already true — the shape the probe exists to catch.
VACUOUS = 'python3 -c "import sys; sys.exit(0)"'


def test_a_vacuous_llm_command_is_dropped_not_shipped(tmp_path):
    """It passes the static check; only running it reveals the problem."""
    tasks = _generate(tmp_path, [{
        "title": "fix it",
        "description": "d",
        "test_command": VACUOUS,
    }])
    assert tasks[0]["test_command"] != VACUOUS, (
        "a command that already exits 0 cannot certify the repair"
    )
    # Falls through to the single-evidence derivation.
    assert tasks[0]["test_command"] == "cargo test --test x"


def test_the_probe_is_memoised_per_command(tmp_path, monkeypatch):
    """Repair rounds emit many tasks sharing one command shape.

    The probe starts a subprocess, so re-running it per task would add
    minutes to every round for no information.
    """
    import repair_generator as rg

    calls = []
    real = rg.check_falsifiable

    def counting_check(command, **kwargs):
        calls.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(rg, "check_falsifiable", counting_check)

    gen = _generator(tmp_path, _ScriptedTool([]))
    for _ in range(3):
        gen._resolve_repair_test_command(
            {"test_command": "cargo test --test signal_classification"},
            _evidence(),
        )
    assert len(calls) == 1, f"probe ran {len(calls)} times, expected 1"


def test_the_probe_can_be_disabled(tmp_path, monkeypatch):
    """An operator whose project cannot execute needs a way out.

    Without this, a missing toolchain would make every LLM-authored
    command look vacuous and silently downgrade the whole round to
    unverifiable tasks.
    """
    monkeypatch.setenv("PDT_DISABLE_FALSIFIABILITY_PROBE", "1")
    tasks = _generate(tmp_path, [{
        "title": "fix it",
        "description": "d",
        "test_command": VACUOUS,
    }])
    assert tasks[0]["test_command"] == VACUOUS
