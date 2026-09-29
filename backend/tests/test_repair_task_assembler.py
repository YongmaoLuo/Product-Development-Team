"""Unit tests for :class:`backend.repair_generator.RepairTaskAssembler`.

Why this module exists
----------------------
2026-09-07: the repair-task generator was refactored to split
LLM output (``title / description / acceptance_criteria``) from
schema-stable tags (``id / priority / depends_on / task_group``).
:class:`RepairTaskAssembler` is the local-code half of that split —
it stamps the tags deterministically so the state machine can
recognise repair tasks regardless of LLM behaviour. These tests pin
the assembler's behaviour down.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the backend module importable when pytest is invoked from the
# repo root or from backend/ directly.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.repair_generator import RepairTaskAssembler  # noqa: E402


def _vp(vp_id: str, priority: str = "medium") -> dict:
    """Helper: build a minimal failed-VP dict shaped like what
    :func:`extract_failed_vps_from_report` returns."""
    return {
        "id": vp_id,
        "title": f"VP {vp_id} title",
        "priority": priority,
        "actual_result": "broken",
        "evidence": "...",
    }


class TestRepairTaskAssemblerBasics(unittest.TestCase):
    def test_round_one_first_task_depends_on_nothing(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix VP-1", "description": "d"},
        ])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "repair-r1-01")
        self.assertEqual(tasks[0]["depends_on"], [])

    def test_round_two_first_task_has_no_cross_round_dependency(self):
        """2026-09-16: round N's first task no longer depends on round N-1.

        It used to carry a ``repair-r1-99`` sentinel for "round 1's last
        task". No code path ever assigns that id, so the DAG validator
        rejected the whole task list at load time (``Task repair-r2-01
        depends on missing task repair-r1-99``) and the executor exited
        before running anything — every plan reaching a ≥2 repair round
        died this way.
        """
        a = RepairTaskAssembler(round_number=2, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix VP-1", "description": "d"},
        ])
        self.assertEqual(tasks[0]["id"], "repair-r2-01")
        self.assertEqual(tasks[0]["depends_on"], [])

    def test_every_dependency_resolves_inside_its_own_round(self):
        """Regression guard: no task may depend on an id nobody emits.

        The assembler stamps ``id`` and ``depends_on`` from the same
        pass, so every dependency it writes must name a task that this
        same call produced. A fabricated id (the old ``-99`` sentinel)
        trips ``Agent._validate_dependencies`` and kills the run at
        load time.
        """
        for round_number in (1, 2, 3, 7):
            with self.subTest(round=round_number):
                a = RepairTaskAssembler(
                    round_number=round_number,
                    failed_vps=[_vp(f"VP-{i}") for i in range(1, 4)],
                )
                tasks = a.assemble([
                    {"failed_vp_id": f"VP-{i}", "title": f"Fix VP-{i}",
                     "description": "d"}
                    for i in range(1, 4)
                ])
                emitted = {t["id"] for t in tasks}
                for task in tasks:
                    for dep in task["depends_on"]:
                        self.assertIn(
                            dep, emitted,
                            f"{task['id']} depends on {dep}, which the "
                            f"assembler never emitted (emitted={sorted(emitted)})",
                        )

    def test_round_two_second_task_chains_to_first(self):
        a = RepairTaskAssembler(round_number=2, failed_vps=[_vp("VP-1"), _vp("VP-2")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix VP-1", "description": "d"},
            {"failed_vp_id": "VP-2", "title": "Fix VP-2", "description": "d"},
        ])
        # Second task depends on the first in the same round (sequential chain).
        self.assertEqual(tasks[1]["id"], "repair-r2-02")
        self.assertEqual(tasks[1]["depends_on"], ["repair-r2-01"])


class TestRepairTaskAssemblerFields(unittest.TestCase):
    def test_status_is_pending(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        for task in tasks:
            self.assertEqual(task["status"], "pending")

    def test_task_group_matches_round(self):
        a = RepairTaskAssembler(round_number=3, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks[0]["task_group"], "repair-round-3")
        self.assertEqual(tasks[0]["round"], 3)

    def test_priority_from_llm_hint_overrides_plan(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1", priority="low")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "priority_hint": "high"},
        ])
        self.assertEqual(tasks[0]["priority"], "high")

    def test_priority_falls_back_to_plan_priority(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1", priority="low")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks[0]["priority"], "low")

    def test_unknown_priority_normalises_to_medium(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "priority_hint": "critical"},
        ])
        self.assertEqual(tasks[0]["priority"], "medium")

    def test_execution_group_hint_passes_through(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "execution_group_hint": 5},
        ])
        self.assertEqual(tasks[0]["execution_group"], 5)

    def test_acceptance_criteria_preserved(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "acceptance_criteria": "test_xxx: true → 1"},
        ])
        self.assertEqual(tasks[0]["acceptance_criteria"], "test_xxx: true → 1")

    def test_failed_vp_id_preserved(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-034")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-034", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks[0]["failed_vp_id"], "VP-034")


class TestRepairTaskAssemblerDefensive(unittest.TestCase):
    def test_empty_contents_returns_empty_list(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        self.assertEqual(a.assemble([]), [])

    def test_half_formed_contents_are_salvaged_not_dropped(self):
        """A row with an empty title or description is repaired, not dropped.

        2026-09-10 (P2): dropping
        half-formed rows silently shrank the repair round — and when every
        row came back half-formed the round produced "0 repair tasks"
        despite real verification failures, which is one of the ways the
        closed loop died. ``assemble`` now falls back to a minimal task
        anchored on the failed VP's evidence so the executor still gets
        something actionable.
        """
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "", "description": "d"},
            {"failed_vp_id": "VP-1", "title": "t", "description": ""},
        ])
        self.assertEqual(len(tasks), 2)
        # Empty title → fallback title still names the failed VP.
        self.assertIn("VP-1", tasks[0]["title"])
        self.assertEqual(tasks[0]["description"], "d")
        # Empty description → fallback description anchored on the evidence.
        self.assertEqual(tasks[1]["title"], "t")
        self.assertIn("VP-1", tasks[1]["description"])

    def test_unknown_vp_id_is_dropped(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        # LLM hallucinated a VP ID that's not in our failed list.
        tasks = a.assemble([
            {"failed_vp_id": "VP-999", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks, [])

    def test_round_zero_rejected(self):
        with self.assertRaises(ValueError):
            RepairTaskAssembler(round_number=0, failed_vps=[_vp("VP-1")])

    def test_negative_round_rejected(self):
        with self.assertRaises(ValueError):
            RepairTaskAssembler(round_number=-1, failed_vps=[_vp("VP-1")])

    def test_non_dict_content_items_skipped(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            "not a dict",  # type: ignore[list-item]
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "repair-r1-01")


class TestRepairTaskAssemblerLLMCallCount(unittest.TestCase):
    """LLM must never see the task_id / priority / depends_on fields.

    The assembler's contract is that those tags are stamped locally,
    so a misbehaving LLM cannot fabricate a duplicate ID, an invalid
    priority enum, or a circular dependency. These tests verify the
    contract by passing malformed LLM content and asserting the
    assembler always produces stable, schema-valid output.
    """

    def test_id_format_is_deterministic(self):
        a = RepairTaskAssembler(round_number=2, failed_vps=[_vp(f"VP-{i}") for i in range(1, 4)])
        # LLM provides garbage IDs; assembler must overwrite with deterministic ones.
        tasks = a.assemble([
            {"failed_vp_id": f"VP-{i}", "title": "Fix", "description": "d",
             "id": f"BAD-{i}"}
            for i in range(1, 4)
        ])
        self.assertEqual([t["id"] for t in tasks],
                         ["repair-r2-01", "repair-r2-02", "repair-r2-03"])

    def test_priority_enum_is_validated(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        # LLM tries to inject an invalid priority.
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "priority_hint": "P0-URGENT-CRITICAL"},
        ])
        self.assertIn(tasks[0]["priority"], {"high", "medium", "low"})


# ---------------------------------------------------------------------------
# test_command / files_to_modify (2026-09-14)
#
# Regression: the 2026-09-07 "content-only LLM + local tag stamping"
# refactor dropped ``test_command`` (and ``files_to_modify``) from the
# emitted task. Every repair task therefore reached the executor with no
# command, and `agent._cross_verify` fell back to
# ``test_cross_verify_skipped → trusting AI claim`` — i.e. the
# dual-criterion completion rule degenerated to a single signal. Observed
# on an earlier plan, whose round-4 repair
# tasks all carried ``test_command = None``.
# ---------------------------------------------------------------------------


class TestRepairTaskCommandAndScope(unittest.TestCase):
    def test_test_command_is_stamped_from_the_plan_verbatim(self):
        a = RepairTaskAssembler(
            round_number=1, failed_vps=[_vp("VP-1")],
            vp_test_commands={"VP-1": "pytest tests/test_x.py -q"},
        )
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks[0]["test_command"], "pytest tests/test_x.py -q")

    def test_test_command_is_empty_rather_than_fabricated(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks[0]["test_command"], "")

    def test_an_api_test_repair_is_gated_by_replaying_its_assertions(self):
        """2026-09-18: a VP has no ``test_command`` any more, so a repair
        task's gate comes from the *method's* judging basis.

        For ``api_test`` that is the ideal shape: replay the VP's own
        request + assertions. The criterion comes from the requirement
        (not from the implementation) and the framework runs it
        deterministically.

        This replaces ``test_plan_repair_gets_a_guard_command_not_the_broken_one``
        — that one asserted the task re-read ``verification_plan.json``
        to check its own ``test_command`` had been edited, i.e. the graded
        artifact rewriting its own criterion.
        """
        vp = dict(_vp("VP-1"), verification_method="api_test")
        a = RepairTaskAssembler(
            round_number=1, failed_vps=[vp],
            plan_path="/tmp/plans/p/verification_plan.json",
        )

        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])

        cmd = tasks[0]["test_command"]
        self.assertIn("verification_api_runner", cmd)
        self.assertIn("--vp", cmd)
        self.assertIn("VP-1", cmd)
        self.assertIn("/tmp/plans/p", cmd)
        self.assertNotIn("verification_plan.json", cmd)

    def test_a_non_api_test_repair_gets_no_fabricated_command(self):
        """``ui_validation`` / ``code_review`` rest on an evidence artifact
        a shell command cannot reproduce. The task runs with an empty
        command rather than a made-up one — the executor's
        ``test_cross_verify_unverified`` warning is what stops that from
        silently counting as a verified pass."""
        for method in ("ui_validation", "code_review"):
            with self.subTest(method=method):
                vp = dict(_vp("VP-1"), verification_method=method)
                a = RepairTaskAssembler(
                    round_number=1, failed_vps=[vp],
                    plan_path="/tmp/plans/p/verification_plan.json",
                )
                tasks = a.assemble([
                    {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
                ])
                self.assertEqual(tasks[0]["test_command"], "")

    def test_plan_repair_without_a_plan_path_keeps_the_verbatim_command(self):
        a = RepairTaskAssembler(
            round_number=1, failed_vps=[_vp("VP-1")],
            vp_test_commands={"VP-1": "pytest -q"},
        )
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "t",
             "description": "改 verification_plan.json"},
        ])
        self.assertEqual(tasks[0]["test_command"], "pytest -q")

    def test_files_to_modify_comes_from_the_llm_hint(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "files_to_modify": ["native_ext/src/core.rs"]},
        ])
        self.assertEqual(
            tasks[0]["files_to_modify"], ["native_ext/src/core.rs"]
        )

    def test_files_to_modify_rejects_absolute_and_traversal_entries(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "files_to_modify": [
                 "/etc/passwd", "../escape.py", "a/../../b.py",
                 "  ", "backend/ok.py", "backend/ok.py",
             ]},
        ])
        self.assertEqual(tasks[0]["files_to_modify"], ["backend/ok.py"])

    def test_files_to_modify_accepts_a_bare_string(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "files_to_modify": "backend/ok.py"},
        ])
        self.assertEqual(tasks[0]["files_to_modify"], ["backend/ok.py"])

    def test_files_to_modify_defaults_to_empty_list(self):
        a = RepairTaskAssembler(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
        ])
        self.assertEqual(tasks[0]["files_to_modify"], [])

    def test_both_keys_are_always_present_on_every_task(self):
        """The executor reads these keys unconditionally; a half-formed
        LLM row must not produce a task that lacks them."""
        a = RepairTaskAssembler(
            round_number=2, failed_vps=[_vp("VP-1"), _vp("VP-2")],
        )
        tasks = a.assemble([
            {"failed_vp_id": "VP-1"},
            {"failed_vp_id": "VP-2", "title": "t", "description": "d"},
        ])
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.assertIn("test_command", task)
            self.assertIn("files_to_modify", task)


if __name__ == "__main__":
    unittest.main()
