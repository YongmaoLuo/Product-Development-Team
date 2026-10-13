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


def _asm(round_number=1, failed_vps=(), vp_test_commands=None,
         plan_path=None, seq_base=1):
    """Build a :class:`RepairTaskAssembler` that clears both W3 gates.

    2026-10-11: ``assemble`` now refuses a task whose ``test_command`` or
    ``files_to_modify`` resolves empty, so every fixture below has to
    declare both. This helper supplies the command for each failed VP
    (``Production`` gets it from the VP's ``evidence_command`` in
    ``verification_plan.json``); the file scope comes from the content
    dicts themselves, which mirror the repair prompt's JSON schema.
    """
    if vp_test_commands is None:
        vp_test_commands = {
            str(vp.get("id")): f"pytest -q tests/test_{vp.get('id')}.py"
            for vp in failed_vps
            if vp.get("id")
        }
    return RepairTaskAssembler(
        round_number=round_number,
        failed_vps=failed_vps,
        vp_test_commands=vp_test_commands,
        plan_path=plan_path,
        seq_base=seq_base,
    )


class TestRepairTaskAssemblerBasics(unittest.TestCase):
    def test_round_one_first_task_depends_on_nothing(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix VP-1", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
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
        a = _asm(round_number=2, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix VP-1", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
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
                a = _asm(
                    round_number=round_number,
                    failed_vps=[_vp(f"VP-{i}") for i in range(1, 4)],
                )
                tasks = a.assemble([
                    {
  "failed_vp_id": f"VP-{i}", "title": f"Fix VP-{i}",
                     "description": "d",
  "files_to_modify": ["src/remote.rs"],
}
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

    def test_no_task_in_a_batch_chains_to_the_previous_one(self):
        """W1 (2026-10-11): a repair batch is a forest of roots.

        Replaces ``test_round_two_second_task_chains_to_first``, which
        asserted ``tasks[1]["depends_on"] == ["repair-r2-01"]``. That
        edge was justified in ``_infer_depends_on``'s own docstring by
        "protects against racing two repair tasks against the same
        ``verification_plan.json`` write" — and the code path it
        described was deleted on 2026-09-18 (Pattern 1 / Pattern 2,
        which let a repair agent rewrite the acceptance criterion).

        Serialised into one chain, so only the head
        (``-01``) ran. When the head failed, every other task
        inherited a ``failed`` upstream and was never schedulable —
        "No schedulable micro-layer found", the rest of the batch left
        ``pending`` forever, and the round was recorded as a success.

        Genuine same-file conflicts are still serialised, by
        ``agent._build_micro_layers``, which reads ``files_to_modify``.
        Both tasks below deliberately declare the SAME file — they still
        get no dependency edge, because a DAG edge and a file conflict
        are different mechanisms.
        """
        a = _asm(round_number=2, failed_vps=[_vp("VP-1"), _vp("VP-2")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix VP-1", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
            {
  "failed_vp_id": "VP-2", "title": "Fix VP-2", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual([t["id"] for t in tasks],
                         ["repair-r2-01", "repair-r2-02"])
        self.assertEqual(tasks[1]["depends_on"], [])
        # …and the whole batch, not just the second row.
        for task in tasks:
            self.assertEqual(
                task["depends_on"], [],
                f"{task['id']} carries a fabricated intra-batch edge",
            )

    def test_every_task_in_a_long_batch_is_a_root(self):
        """The 2026-10-11 shape, at the size where it actually hurt."""
        ids = [f"VP-{i}" for i in range(1, 9)]
        a = _asm(round_number=4, failed_vps=[_vp(i) for i in ids])
        tasks = a.assemble([
            {
  "failed_vp_id": i, "title": f"Fix {i}", "description": "d",
  "files_to_modify": [f"src/{i}.rs"],
} for i in ids
        ])
        self.assertEqual(len(tasks), 8)
        self.assertTrue(all(t["depends_on"] == [] for t in tasks))
        self.assertEqual(a.dropped_empty_files_to_modify, 0)
        self.assertEqual(a.unverifiable_no_test_command, 0)


class TestRepairTaskAssemblerFields(unittest.TestCase):
    def test_status_is_pending(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        for task in tasks:
            self.assertEqual(task["status"], "pending")

    def test_task_group_matches_round(self):
        a = _asm(round_number=3, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["task_group"], "repair-round-3")
        self.assertEqual(tasks[0]["round"], 3)

    def test_priority_from_llm_hint_overrides_plan(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1", priority="low")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "priority_hint": "high",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["priority"], "high")

    def test_priority_falls_back_to_plan_priority(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1", priority="low")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["priority"], "low")

    def test_unknown_priority_normalises_to_medium(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "priority_hint": "critical",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["priority"], "medium")

    def test_execution_group_hint_passes_through(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "execution_group_hint": 5,
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["execution_group"], 5)

    def test_acceptance_criteria_preserved(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "acceptance_criteria": "test_xxx: true → 1",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["acceptance_criteria"], "test_xxx: true → 1")

    def test_failed_vp_id_preserved(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-034")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-034", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["failed_vp_id"], "VP-034")


class TestRepairTaskAssemblerDefensive(unittest.TestCase):
    def test_empty_contents_returns_empty_list(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
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
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
            {
  "failed_vp_id": "VP-1", "title": "t", "description": "",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(len(tasks), 2)
        # Empty title → fallback title still names the failed VP.
        self.assertIn("VP-1", tasks[0]["title"])
        self.assertEqual(tasks[0]["description"], "d")
        # Empty description → fallback description anchored on the evidence.
        self.assertEqual(tasks[1]["title"], "t")
        self.assertIn("VP-1", tasks[1]["description"])

    def test_unknown_vp_id_is_dropped(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        # LLM hallucinated a VP ID that's not in our failed list.
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-999", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks, [])

    def test_round_zero_rejected(self):
        with self.assertRaises(ValueError):
            _asm(round_number=0, failed_vps=[_vp("VP-1")])

    def test_negative_round_rejected(self):
        with self.assertRaises(ValueError):
            _asm(round_number=-1, failed_vps=[_vp("VP-1")])

    def test_non_dict_content_items_skipped(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            "not a dict",  # type: ignore[list-item]
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
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
        a = _asm(round_number=2, failed_vps=[_vp(f"VP-{i}") for i in range(1, 4)])
        # LLM provides garbage IDs; assembler must overwrite with deterministic ones.
        tasks = a.assemble([
            {
  "failed_vp_id": f"VP-{i}", "title": "Fix", "description": "d",
             "id": f"BAD-{i}",
  "files_to_modify": ["src/remote.rs"],
}
            for i in range(1, 4)
        ])
        self.assertEqual([t["id"] for t in tasks],
                         ["repair-r2-01", "repair-r2-02", "repair-r2-03"])

    def test_priority_enum_is_validated(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        # LLM tries to inject an invalid priority.
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "priority_hint": "P0-URGENT-CRITICAL",
  "files_to_modify": ["src/remote.rs"],
},
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
        a = _asm(
            round_number=1, failed_vps=[_vp("VP-1")],
            vp_test_commands={"VP-1": "pytest tests/test_x.py -q"},
        )
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["test_command"], "pytest tests/test_x.py -q")

    def test_a_task_with_no_resolvable_command_is_emitted_and_labelled(self):
        """An empty ``test_command`` is no longer a refusal.

        This replaced a test asserting the row was dropped. The refusal
        was structurally wrong, not situational: a plan whose points are
        declarative carries no command string on any of them, so the
        model is never asked for one, the VP path returns nothing and
        the single-evidence path sees zero usable commands rather than
        one. Every derived task then died at this gate and the round
        repaired nothing.

        The two remaining costs are kept visible rather than avoided: the
        row is counted in ``unverifiable_no_test_command`` and given a
        source from ``ASSEMBLER_TEST_COMMAND_SOURCES`` that distinguishes
        "this class of task needs no command" from "we never found one".
        This fixture's method is unstated, so it lands on ``missing``.
        """
        a = _asm(round_number=2, failed_vps=[_vp("VP-1")],
                 vp_test_commands={})
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["test_command"], "")
        self.assertEqual(a.test_command_sources["repair-r2-01"], "missing")
        self.assertEqual(a.unverifiable_no_test_command, 1)
        self.assertEqual(a.dropped_empty_files_to_modify, 0)

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
        a = _asm(
            round_number=1, failed_vps=[vp],
            vp_test_commands={},  # force the method-derived path
            plan_path="/tmp/plans/p/verification_plan.json",
        )

        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])

        cmd = tasks[0]["test_command"]
        self.assertIn("verification_api_runner", cmd)
        self.assertIn("--vp", cmd)
        self.assertIn("VP-1", cmd)
        self.assertIn("/tmp/plans/p", cmd)
        self.assertNotIn("verification_plan.json", cmd)

    def test_a_non_api_test_repair_is_emitted_not_given_a_fake_command(self):
        """``ui_validation`` / ``code_review`` rest on an evidence artifact
        a shell command cannot reproduce.

        What matters here is that ``_api_replay_command`` returns ``""``
        for them — the framework still refuses to invent a command for a
        method whose judging basis is an artifact — and that the task is
        then emitted under the ``audit_style`` label rather than
        discarded. An empty ``test_command`` is the honest shape here;
        a fabricated one would fail for reasons unrelated to the fix.
        """
        for method in ("ui_validation", "code_review"):
            with self.subTest(method=method):
                vp = dict(_vp("VP-1"), verification_method=method)
                a = _asm(
                    round_number=1, failed_vps=[vp],
                    vp_test_commands={},
                    plan_path="/tmp/plans/p/verification_plan.json",
                )
                tasks = a.assemble([
                    {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
                ])
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["test_command"], "")
                self.assertEqual(
                    a.test_command_sources["repair-r1-01"], "audit_style",
                )
                self.assertEqual(a.unverifiable_no_test_command, 1)

    def test_plan_repair_without_a_plan_path_keeps_the_verbatim_command(self):
        a = _asm(
            round_number=1, failed_vps=[_vp("VP-1")],
            vp_test_commands={"VP-1": "pytest -q"},
        )
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "t",
             "description": "改 verification_plan.json",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(tasks[0]["test_command"], "pytest -q")

    def test_files_to_modify_comes_from_the_llm_hint(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "files_to_modify": ["native_ext/src/core.rs"]},
        ])
        self.assertEqual(
            tasks[0]["files_to_modify"], ["native_ext/src/core.rs"]
        )

    def test_files_to_modify_rejects_absolute_and_traversal_entries(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "files_to_modify": [
                 "/etc/passwd", "../escape.py", "a/../../b.py",
                 "  ", "backend/ok.py", "backend/ok.py",
             ]},
        ])
        self.assertEqual(tasks[0]["files_to_modify"], ["backend/ok.py"])

    def test_files_to_modify_accepts_a_bare_string(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
             "files_to_modify": "backend/ok.py"},
        ])
        self.assertEqual(tasks[0]["files_to_modify"], ["backend/ok.py"])

    def test_a_task_with_no_declared_scope_is_refused_not_emitted(self):
        """W3 (2026-10-11): empty ``files_to_modify`` is a refusal.

        Replaces ``test_files_to_modify_defaults_to_empty_list``, which
        pinned the old "empty list means scope unknown, let the subagent
        find its own paths" behaviour. Two reasons that is no longer
        good enough:

        1. ``agent._declared_modification_files`` reads ``[]`` as "this
           task touches no files", so a task declared with an empty
           scope is indistinguishable from one that really changed
           nothing.
        2. ``agent._build_micro_layers`` derives its conflict graph from
           ``files_to_modify``. With W1's intra-batch chain gone, that
           graph is the *only* thing serialising two repairs that touch
           the same file (``repair-r4-01`` and ``repair-r4-05`` both
           wanted ``src-tauri/src/remote/e2e.rs``). A ``[]`` hands it
           nothing, and the two agents then run concurrently over one
           file.

        So an undeclared scope is refused rather than guessed at. The
        counter exists so the round reports "3 VPs got no repair"
        instead of quietly producing fewer tasks than failures.
        """
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
},
        ])
        self.assertEqual(tasks, [])
        self.assertEqual(a.dropped_empty_files_to_modify, 1)
        self.assertEqual(a.unverifiable_no_test_command, 0)

    def test_a_scope_that_normalises_to_empty_is_also_refused(self):
        """Entries that all get filtered out are the same as no entries.

        Absolute paths and ``..`` traversal are rejected by
        ``_resolve_files_to_modify``, so an LLM that answers with only
        those produces an empty scope — and must meet the same refusal,
        not slip past the gate.
        """
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1", "title": "Fix", "description": "d",
  "files_to_modify": ["/etc/passwd", "../escape.py", "a/../../b.py"],
},
        ])
        self.assertEqual(tasks, [])
        self.assertEqual(a.dropped_empty_files_to_modify, 1)

    def test_both_keys_are_always_present_on_every_task(self):
        """The executor reads these keys unconditionally; a half-formed
        LLM row must not produce a task that lacks them."""
        a = _asm(
            round_number=2, failed_vps=[_vp("VP-1"), _vp("VP-2")],
        )
        tasks = a.assemble([
            {
  "failed_vp_id": "VP-1",
  "files_to_modify": ["src/remote.rs"],
},
            {
  "failed_vp_id": "VP-2", "title": "t", "description": "d",
  "files_to_modify": ["src/remote.rs"],
},
        ])
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.assertIn("test_command", task)
            self.assertIn("files_to_modify", task)


# ---------------------------------------------------------------------------
# W3 — generation-time gates (2026-10-11)
#
# Both gates live in ``RepairTaskAssembler.assemble``, after
# ``_resolve_files_to_modify`` / ``_resolve_test_command``. The point of
# the section below is not "the field is empty" — it is that emptiness
# now produces a *refusal plus a record*, instead of a task that sails
# through to the executor looking well-formed.
#
# The contrast being pinned: the main task generator runs
# ``inspect_command`` + ``check_falsifiable``, and the refiner's split
# children go through ``AutonomousAgent._enforce_new_subtask_test_command``.
# The repair path had neither, so every task went out with
# ``test_command=""`` and ``files_to_modify=[]``.
# ---------------------------------------------------------------------------


class captureStdout:  # noqa: N801 — context manager, not a TestCase helper
    """Capture ``print()`` output. ``with captureStdout() as text:``.

    ``repair_generator`` prints rather than logs (its callers run in
    contexts where no logger is configured), so ``assertLogs`` — which
    hooks the ``logging`` machinery — would see nothing.
    """

    def __init__(self):
        self._buf = None
        self._old = None
        self._value = ""

    def __enter__(self):
        import io
        import sys as _sys
        self._buf = io.StringIO()
        self._old = _sys.stdout
        _sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        import sys as _sys
        _sys.stdout = self._old
        self._value = self._buf.getvalue()
        return False

    def __str__(self):
        return self._value


class TestRepairTaskGenerationGates(unittest.TestCase):
    """Two outcomes, kept apart: refused (no scope) vs emitted unchecked."""

    def test_empty_files_to_modify_is_refused_counted_and_logged(self):
        a = _asm(round_number=3, failed_vps=[_vp("VP-1")])
        with captureStdout() as out:
            tasks = a.assemble([
                {"failed_vp_id": "VP-1", "title": "Fix", "description": "d"},
            ])
        self.assertEqual(tasks, [], "a scope-less task must not be emitted")
        self.assertEqual(a.dropped_empty_files_to_modify, 1)
        self.assertEqual(a.unverifiable_no_test_command, 0)
        # Greppable event name + the round, so refusals tally per round.
        self.assertIn("repair_task_dropped_empty_files_to_modify", str(out))
        self.assertIn("round=3", str(out))
        # The VP and its title, so an operator can go find the evidence.
        self.assertIn("failed_vp_id=VP-1", str(out))
        self.assertIn("Fix", str(out))

    def test_empty_test_command_is_emitted_counted_and_logged(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")],
                 vp_test_commands={})
        with captureStdout() as out:
            tasks = a.assemble([
                {"failed_vp_id": "VP-1", "title": "Fix", "description": "d",
                 "files_to_modify": ["src/remote.rs"]},
            ])
        self.assertEqual(len(tasks), 1, "the repair must still be dispatched")
        self.assertEqual(a.unverifiable_no_test_command, 1)
        self.assertEqual(a.dropped_empty_files_to_modify, 0)
        # A different event from the refusal, so "repaired nothing" and
        # "repaired something it cannot objectively check" are two
        # countable numbers rather than one silence.
        self.assertIn("repair_task_unverifiable_no_test_command", str(out))
        self.assertNotIn("repair_task_dropped_empty_test_command", str(out))
        self.assertIn("round=1", str(out))
        self.assertIn("failed_vp_id=VP-1", str(out))
        # The source, so a tally can be broken down by kind of nothing.
        self.assertIn("source=missing", str(out))

    def test_the_two_outcomes_count_independently(self):
        """A batch mixing resolvable and hollow rows: both are accounted.

        Only the scope-less row is refused. The command-less row is
        emitted with an empty command and its own counter, which is the
        point of the split — one number would hide one of the two.
        """
        a = _asm(round_number=2, failed_vps=[
            _vp("VP-1"), _vp("VP-2"), _vp("VP-3"),
        ], vp_test_commands={"VP-1": "pytest -q"})
        tasks = a.assemble([
            # good: scope + command
            {"failed_vp_id": "VP-1", "title": "ok", "description": "d",
             "files_to_modify": ["src/a.rs"]},
            # no scope
            {"failed_vp_id": "VP-2", "title": "no scope", "description": "d"},
            # scope but no command
            {"failed_vp_id": "VP-3", "title": "no cmd", "description": "d",
             "files_to_modify": ["src/c.rs"]},
        ])
        self.assertEqual(
            [t["failed_vp_id"] for t in tasks], ["VP-1", "VP-3"],
        )
        self.assertEqual(
            [t["id"] for t in tasks], ["repair-r2-01", "repair-r2-02"],
        )
        self.assertEqual(a.dropped_empty_files_to_modify, 1)
        self.assertEqual(a.unverifiable_no_test_command, 1)

    def test_a_refused_row_consumes_no_task_id(self):
        """Ids stay contiguous over what was actually emitted.

        A refused row must not burn a ``seq``: ``repair-r1-01`` missing
        from ``plan_tasks`` with ``repair-r1-02`` present reads as "a
        task vanished", which is precisely the failure mode this gate
        exists to make visible rather than to reproduce.
        """
        a = _asm(round_number=1, failed_vps=[_vp("VP-1"), _vp("VP-2")],
                 vp_test_commands={"VP-2": "pytest -q"})
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "no scope", "description": "d"},
            {"failed_vp_id": "VP-2", "title": "ok", "description": "d",
             "files_to_modify": ["src/b.rs"]},
        ])
        self.assertEqual([t["id"] for t in tasks], ["repair-r1-01"])
        self.assertEqual(a.dropped_empty_files_to_modify, 1)

    def test_a_refused_row_does_not_shift_later_ids_off_seq_base(self):
        a = _asm(round_number=4, failed_vps=[_vp("VP-1"), _vp("VP-2")],
                 seq_base=7, vp_test_commands={"VP-2": "pytest -q"})
        tasks = a.assemble([
            {"failed_vp_id": "VP-1", "title": "no scope", "description": "d"},
            {"failed_vp_id": "VP-2", "title": "ok", "description": "d",
             "files_to_modify": ["src/b.rs"]},
        ])
        self.assertEqual([t["id"] for t in tasks], ["repair-r4-07"])

    def test_counters_are_per_assembler_and_start_at_zero(self):
        a = _asm(round_number=1, failed_vps=[_vp("VP-1")])
        self.assertEqual(a.dropped_empty_files_to_modify, 0)
        self.assertEqual(a.unverifiable_no_test_command, 0)
        a.assemble([{"failed_vp_id": "VP-1", "title": "t", "description": "d"}])
        self.assertEqual(a.dropped_empty_files_to_modify, 1)
        # A second assembler is a fresh ledger.
        b = _asm(round_number=2, failed_vps=[_vp("VP-1")])
        self.assertEqual(b.dropped_empty_files_to_modify, 0)


if __name__ == "__main__":
    unittest.main()
