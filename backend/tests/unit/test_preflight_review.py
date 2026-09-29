"""
TDD tests for ``backend.preflight_review.PreFlightReviewer`` (DP2).

These tests pin the 5 contracts named in the task spec:

  1. ``test_preflight_detects_missing_arch_for_acceptance``
     PRD acceptance item ACC-1 has no arch correspondence → the LLM
     is asked and returns a high-severity finding; ``run`` surfaces
     it via ``high_count >= 1``.

  2. ``test_preflight_no_findings_when_aligned``
     LLM reports ``{"findings": []}`` (aligned) → ``findings`` is
     empty and ``high_count == 0``.

  3. ``test_preflight_llm_failure_fallback_pass``
     ``coding_tool.query_json`` raises → ``high_count == 0`` and
     ``findings == []`` (degraded pass — never block task gen).

  4. ``test_preflight_skip_when_arch_disabled``
     ``plan_state.flags.arch_enabled == False`` → the LLM is NOT
     asked about PRD→arch or arch→test dimensions; only the
     test→task dimension runs (if ``test_enabled == True``).

  5. ``test_preflight_writes_report_json``
     After ``run`` returns, ``plans/<plan_id>/preflight_report.json``
     exists on disk.

Edge cases pinned:

  * ``prd.json`` missing → ``run`` raises ``FileNotFoundError`` (no
    silent fallback — task gen MUST stop).
  * ``report_path`` is included in the returned dict and points at
    ``plans/<plan_id>/preflight_report.json``.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _passthrough_self_review(*args, **kwargs):
    """No-op stub for the mandatory second-pass self-review."""
    doc_content = kwargs.get("doc_content") or (
        args[0] if args else ""
    )
    return {
        "doc_type": kwargs.get("doc_type") or "tasks",
        "attempted": True,
        "succeeded": True,
        "rewrote": False,
        "findings": [],
        "fixed_content": doc_content,
        "input_content_hash": "sha256:" + "x" * 64,
        "fixed_content_hash": "sha256:" + "x" * 64,
        "severity_high_count": 0,
        "severity_medium_count": 0,
        "severity_low_count": 0,
        "mandatory": True,
        "error": None,
    }


import tasks_generator as _ts_gen
_ts_gen.run_doc_self_review = _passthrough_self_review


def _make_coding_tool(canned_response):
    """Build a CodingTool-like MagicMock whose ``query_json`` returns
    the canned dict. Also stubs ``query`` so any incidental caller
    doesn't trip a MissingMock error.
    """
    tool = MagicMock()
    tool.query_json.return_value = canned_response
    tool.query.return_value = json.dumps(canned_response, ensure_ascii=False)
    return tool


def _write_prd(plan_dir: Path, acceptance_ids=("AC-1", "AC-2")) -> None:
    prd = {
        "title": "Test PRD",
        "overview": "Cross-doc consistency test fixture.",
        "acceptance": list(acceptance_ids),
        "decision_points": [],
    }
    (plan_dir / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_arch(plan_dir: Path, body: str) -> None:
    (plan_dir / "arch-design.md").write_text(body, encoding="utf-8")


def _write_test_design(plan_dir: Path, body: str) -> None:
    (plan_dir / "test-design.md").write_text(body, encoding="utf-8")


def _write_plan_state(plan_dir: Path, *, arch_enabled=True, test_enabled=True) -> None:
    """Write a minimal ``plan_state.json`` with the two phase flags."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": "tasks_generation",
        "completed_phases": ["interview", "prd_generation", "prd_review"],
        "review_rounds": {"prd": 1, "arch": 0, "test": 0},
        "flags": {
            "arch_enabled": arch_enabled,
            "test_enabled": test_enabled,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Test 1 — PRD acceptance missing arch correspondence → high-severity finding
# ---------------------------------------------------------------------------


def test_preflight_detects_missing_arch_for_acceptance(tmp_path):
    """PRD acceptance ACC-1 has no arch correspondence.

    The LLM is asked to check alignment and returns a high-severity
    finding for ``ACC-1``. The reviewer surfaces it via
    ``high_count >= 1`` and the finding list.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["ACC-1: must do X"])
    _write_arch(
        plan_dir,
        "# Architecture\n\n"
        "## Modules\n"
        "- LoggerModule: wraps stdlib logging\n",
    )
    _write_test_design(plan_dir, "# Test Design\n\n## Scenarios\n- T-001: smoke\n")

    canned_response = {
        "findings": [
            {
                "source_doc": "prd",
                "source_id": "ACC-1",
                "target_doc": "arch",
                "target_id": None,
                "severity": "high",
                "finding": "PRD acceptance ACC-1 has no corresponding arch component.",
                "suggested_fix": "Add an arch module that implements ACC-1.",
            },
        ]
    }
    coding_tool = _make_coding_tool(canned_response)

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    assert report["high_count"] >= 1, (
        f"expected high_count >= 1, got {report.get('high_count')}; "
        f"findings={report.get('findings')}"
    )

    arch_high_findings = [
        f for f in report["findings"]
        if f["severity"] == "high" and f["target_doc"] == "arch"
    ]
    assert arch_high_findings, (
        f"expected at least one high-severity finding targeting 'arch'; "
        f"got findings={report['findings']}"
    )


# ---------------------------------------------------------------------------
# Test 2 — aligned docs → no findings
# ---------------------------------------------------------------------------


def test_preflight_no_findings_when_aligned(tmp_path):
    """Three docs aligned → findings empty, high_count == 0."""
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["ACC-200: aligned feature"])
    _write_arch(
        plan_dir,
        "# Architecture\n\n## Modules\n- AlignedModule: implements ACC-200\n",
    )
    _write_test_design(
        plan_dir,
        "# Test Design\n\n## Scenarios\n- T-ALIGNED-1: covers ACC-200\n",
    )

    canned_response = {"findings": []}
    coding_tool = _make_coding_tool(canned_response)

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    assert report["findings"] == [], (
        f"expected empty findings on aligned docs; got {report['findings']}"
    )
    assert report["high_count"] == 0


# ---------------------------------------------------------------------------
# Test 3 — LLM raises → degraded pass (high_count == 0)
# ---------------------------------------------------------------------------


def test_preflight_llm_failure_fallback_pass(tmp_path):
    """LLM raises → high_count == 0 and findings == [] (never block)."""
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["ACC-300: degraded path"])
    _write_arch(plan_dir, "# Architecture\n\n## Modules\n- X\n")
    _write_test_design(plan_dir, "# Test Design\n\n## Scenarios\n- T\n")

    coding_tool = MagicMock()
    coding_tool.query_json.side_effect = RuntimeError("LLM service unavailable")

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")

    # Must NOT raise.
    report = reviewer.run("test-plan")

    assert report["high_count"] == 0, (
        f"expected high_count == 0 on LLM failure; got {report}"
    )
    assert report["findings"] == []


# ---------------------------------------------------------------------------
# Test 4 — arch_enabled=False → skip dimensions 1 and 2
# ---------------------------------------------------------------------------


def test_preflight_skip_when_arch_disabled(tmp_path):
    """arch_enabled=False → dimensions 1 (PRD→arch) and 2 (arch→test)
    are skipped; the LLM is only asked about dimension 3
    (test→task, if test_enabled=True).
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["ACC-400: arch disabled"])
    # NOTE: arch-design.md is intentionally present on disk — the
    # flag is what controls the skip, not file existence. This makes
    # the test pin the flag-driven contract unambiguously.
    _write_arch(plan_dir, "# Architecture\n\n## Modules\n- ShouldBeIgnored\n")
    _write_test_design(plan_dir, "# Test Design\n\n## Scenarios\n- T-400\n")

    # Explicitly disable arch; keep test enabled so dim 3 runs.
    _write_plan_state(plan_dir, arch_enabled=False, test_enabled=True)

    coding_tool = _make_coding_tool({"findings": []})

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    # The LLM must have been asked exactly once.
    assert coding_tool.query_json.call_count == 1, (
        f"expected exactly one LLM call; got {coding_tool.query_json.call_count}"
    )

    sent_prompt = coding_tool.query_json.call_args.kwargs.get("prompt", "") or ""
    if not sent_prompt:
        # Some CodingTool signatures pass prompt positionally.
        args = coding_tool.query_json.call_args.args
        sent_prompt = args[0] if args else ""

    # The reviewer's prompt MUST carry explicit, machine-grepable
    # dimension markers ("## 维度 1", "## 维度 2", "## 维度 3") so
    # callers can verify which dimensions were actually requested.
    # When ``arch_enabled == False``, dimensions 1 and 2 (PRD→arch
    # and arch→test) must NOT appear in the prompt.
    assert "## 维度 1" not in sent_prompt, (
        f"prompt should NOT include '## 维度 1' (arch disabled); "
        f"prompt head: {sent_prompt[:500]!r}"
    )
    assert "## 维度 2" not in sent_prompt, (
        f"prompt should NOT include '## 维度 2' (arch disabled); "
        f"prompt head: {sent_prompt[:500]!r}"
    )

    # Conversely, dimension 3 (test→task) MUST still be present
    # because ``test_enabled == True``.
    assert "## 维度 3" in sent_prompt, (
        f"prompt SHOULD include '## 维度 3' (test enabled); "
        f"prompt head: {sent_prompt[:500]!r}"
    )

    # And the run still succeeds (no findings, since LLM was mocked empty).
    assert report["high_count"] == 0


# ---------------------------------------------------------------------------
# Test 5 — preflight_report.json is persisted to disk
# ---------------------------------------------------------------------------


def test_preflight_writes_report_json(tmp_path):
    """After ``run`` returns, ``plans/<plan_id>/preflight_report.json``
    exists on disk and contains the same report that was returned.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    _write_prd(plan_dir, acceptance_ids=["ACC-500: persistence check"])
    _write_arch(plan_dir, "# Architecture\n\n## Modules\n- M\n")
    _write_test_design(plan_dir, "# Test Design\n\n## Scenarios\n- T\n")

    coding_tool = _make_coding_tool({"findings": []})

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")
    report = reviewer.run("test-plan")

    artifact = plan_dir / "preflight_report.json"
    assert artifact.exists(), f"missing artifact: {artifact}"

    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["high_count"] == report["high_count"]
    assert persisted["findings"] == report["findings"]

    # The returned report must include the absolute (or relative)
    # path to the persisted artifact so downstream callers (Task 9/10)
    # can read it back.
    assert "report_path" in report, (
        f"return dict missing 'report_path' key; got keys={list(report.keys())}"
    )
    assert "preflight_report.json" in str(report["report_path"]), (
        f"report_path should reference preflight_report.json; "
        f"got {report['report_path']!r}"
    )


# ---------------------------------------------------------------------------
# Bonus edge case — PRD missing → FileNotFoundError (NO silent fallback)
# ---------------------------------------------------------------------------


def test_preflight_raises_when_prd_missing(tmp_path):
    """``prd.json`` missing → ``run`` raises ``FileNotFoundError``.

    Per the task spec edge case list, PRD missing is a hard error:
    task generation MUST stop rather than silently emit an empty
    report. This test pins the contract so a regression to the old
    "return degraded report" behaviour is caught.
    """
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True)

    # PRD intentionally NOT written.
    _write_arch(plan_dir, "# Architecture\n")
    _write_test_design(plan_dir, "# Test Design\n")

    coding_tool = _make_coding_tool({"findings": []})

    from preflight_review import PreFlightReviewer

    reviewer = PreFlightReviewer(coding_tool, plans_root=tmp_path / "plans")

    with pytest.raises(FileNotFoundError):
        reviewer.run("test-plan")


# ---------------------------------------------------------------------------
# Task 9 — plan_state has ``preflight_review`` phase and the tasks
# generator aborts when the reviewer reports a high-severity finding.
# ---------------------------------------------------------------------------


def test_plan_state_has_preflight_review_phase():
    """``plan_state.VALID_PHASES`` must include ``preflight_review`` so
    the workflow can transition ``prd_approved`` / ``arch_approved`` /
    ``test_approved`` → ``preflight_review`` → ``tasks_generation``.

    This is the core DP2 contract: preflight is a real workflow phase,
    not just an in-memory check inside the tasks generator.
    """
    # Force re-import in case plan_state was imported before this test
    # ran (other tests may have cached an older version).
    import importlib

    import plan_state as plan_state_mod

    # ``reload`` re-executes the module, which rebinds every module-level
    # name — including ``MIGRATION_AUDIT_SCHEMA`` — to a NEW object, while
    # every already-imported consumer (tasks_generator, preflight_review)
    # still holds the original reference. That breaks the cross-module
    # identity contract pinned by
    # ``tests/integration/test_migration_audit_schema.py`` when the whole
    # suite runs in one process. Snapshot and restore it.
    _original_schema = plan_state_mod.MIGRATION_AUDIT_SCHEMA
    try:
        importlib.reload(plan_state_mod)
        assert "preflight_review" in plan_state_mod.VALID_PHASES, (
            f"VALID_PHASES must include 'preflight_review'; "
            f"got {plan_state_mod.VALID_PHASES}"
        )
    finally:
        plan_state_mod.MIGRATION_AUDIT_SCHEMA = _original_schema


# ---------------------------------------------------------------------------
# Task 9 — TasksGenerator abort / pass / skip contracts
# ---------------------------------------------------------------------------


def _build_tasks_generator(plan_dir: Path, llm_response: dict):
    """Build a TasksGenerator with a stubbed coding tool.

    The stub records each call so tests can assert the LLM was /
    was not invoked. The PRD on disk is the minimum needed for
    ``_load_prd`` to succeed.
    """
    from tasks_generator import TasksGenerator

    tool = MagicMock()
    tool.query_json.return_value = llm_response
    (plan_dir / "prd.md").write_text(
        "# PRD\nStub for tasks generator preflight abort test.\n",
        encoding="utf-8",
    )
    return TasksGenerator(coding_tool=tool, plan_dir=plan_dir), tool


def _patch_preflight(monkeypatch, *, high_count: int, call_count_holder=None):
    """Patch ``PreFlightReviewer.run`` inside the ``tasks_generator`` module.

    The patched ``run`` returns a dict whose ``high_count`` matches the
    caller's wish. When ``call_count_holder`` is a list, the patch
    appends ``True`` on every invocation so the test can assert the
    reviewer was / was not called.
    """
    import tasks_generator as tasks_generator_mod

    canned_report = {
        "findings": (
            [
                {
                    "source_doc": "prd",
                    "source_id": "ACC-1",
                    "target_doc": "arch",
                    "target_id": None,
                    "severity": "high",
                    "finding": "PRD AC has no arch mapping",
                    "suggested_fix": "add arch",
                }
            ]
            if high_count > 0
            else []
        ),
        "high_count": high_count,
        "report_path": "plans/x/preflight_report.json",
        "passed": high_count == 0,
    }

    def _fake_run(self, plan_id):
        if call_count_holder is not None:
            call_count_holder.append(True)
        return canned_report

    monkeypatch.setattr(
        tasks_generator_mod.PreFlightReviewer, "run", _fake_run, raising=True
    )
    return canned_report


def _set_preflight_enabled(plan_dir: Path, enabled: bool) -> None:
    """Write a minimal ``plan_state.json`` with the preflight flag set."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": "tasks_generation",
        "completed_phases": ["interview", "prd_generation", "prd_review"],
        "flags": {
            "arch_enabled": False,
            "test_enabled": False,
            "preflight_enabled": enabled,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def test_tasks_generator_aborts_on_high_severity(tmp_path, monkeypatch):
    """``PreFlightReviewer.run`` returns ``high_count >= 1`` →
    ``TasksGenerator.generate`` raises ``PreflightFailedError`` and
    does NOT write tasks.json.

    Boundary pinned: the tasks generator must never emit a tasks.json
    that the executor would consume while a high-severity
    cross-document mismatch remains unresolved.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True)
    _set_preflight_enabled(plan_dir, enabled=True)

    call_log: list = []
    _patch_preflight(monkeypatch, high_count=1, call_count_holder=call_log)

    canned_tasks = {
        "requirement": "stub",
        "tasks": [
            {
                "id": "1",
                "title": "stub",
                "description": "stub",
                "test_command": "echo",
            }
        ],
    }
    gen, tool = _build_tasks_generator(plan_dir, llm_response=canned_tasks)

    from tasks_generator import PreflightFailedError

    with pytest.raises(PreflightFailedError) as exc_info:
        gen.generate()

    # Reviewer MUST have been called.
    assert call_log, (
        "TasksGenerator.generate() must invoke PreFlightReviewer.run() before emitting tasks.json"
    )

    # The exception must carry the findings so callers can surface them.
    findings = getattr(exc_info.value, "findings", None)
    assert findings, (
        "PreflightFailedError must expose the high-severity findings via the "
        "``findings`` attribute so callers can render them to the user."
    )

    # tasks.json MUST NOT exist (abort happens before the file is written).
    assert not (plan_dir / "tasks.json").exists(), (
        "TasksGenerator emitted tasks.json despite a high-severity preflight finding; "
        "the abort gate must run BEFORE the file is persisted."
    )

    # And the LLM was never asked to generate tasks (abort before query_json).
    assert tool.query_json.call_count == 0, (
        "TasksGenerator must not invoke the LLM for task generation when preflight aborts."
    )


def test_tasks_generator_passes_when_no_high(tmp_path, monkeypatch):
    """``PreFlightReviewer.run`` returns ``high_count == 0`` →
    ``TasksGenerator.generate`` proceeds normally and writes tasks.json.

    This pins the happy path: the preflight gate is transparent to
    aligned docs and does not block task generation.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True)
    _set_preflight_enabled(plan_dir, enabled=True)

    call_log: list = []
    _patch_preflight(monkeypatch, high_count=0, call_count_holder=call_log)

    canned_tasks = {
        "requirement": "stub",
        "tasks": [
            {
                "id": "1",
                "title": "stub",
                "description": "stub",
                "test_command": "echo",
            }
        ],
    }
    gen, tool = _build_tasks_generator(plan_dir, llm_response=canned_tasks)

    result = gen.generate()

    # Reviewer was called, LLM was called, and tasks.json was written.
    assert call_log, "PreFlightReviewer.run must be invoked on the happy path"
    assert tool.query_json.call_count == 1, (
        "TasksGenerator must invoke the LLM exactly once on the happy path"
    )

    canonical = plan_dir / "tasks.json"
    assert canonical.exists(), "tasks.json was not written on the happy path"
    persisted = json.loads(canonical.read_text(encoding="utf-8"))
    assert persisted["tasks"][0]["id"] == "1"
    assert result["tasks"][0]["id"] == "1"


def test_tasks_generator_skips_when_flag_disabled(tmp_path, monkeypatch):
    """``plan_state.flags.preflight_enabled == False`` →
    ``TasksGenerator.generate`` does NOT call PreFlightReviewer at all
    and proceeds directly to emit tasks.json.

    This pins the boundary condition: preflight is opt-out. When the
    flag is False, the gate is fully bypassed (no LLM call, no
    preflight_report.json written, no abort path possible).
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True)
    _set_preflight_enabled(plan_dir, enabled=False)

    call_log: list = []
    _patch_preflight(monkeypatch, high_count=1, call_count_holder=call_log)

    canned_tasks = {
        "requirement": "stub",
        "tasks": [
            {
                "id": "1",
                "title": "stub",
                "description": "stub",
                "test_command": "echo",
            }
        ],
    }
    gen, tool = _build_tasks_generator(plan_dir, llm_response=canned_tasks)

    gen.generate()

    # Reviewer was NOT called.
    assert not call_log, (
        "PreFlightReviewer.run must NOT be invoked when plan_state.flags.preflight_enabled == False"
    )

    # tasks.json still got written.
    canonical = plan_dir / "tasks.json"
    assert canonical.exists(), (
        "tasks.json was not written even though preflight was disabled"
    )
