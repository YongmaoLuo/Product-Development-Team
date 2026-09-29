"""
Tests for ``TasksGenerator._backfill_files_to_modify_from_description``
(Principle 2 — 2026-08-19).

Background
----------
``framework/task_output_validator.py`` step-3 hard-rejects any
plan whose ``files_to_modify`` is ``[]`` or contains a path that
doesn't exist on disk. The post-read gate then aborts the dispatcher
with ``unfixable failure``.

The generator therefore backfills ``files_to_modify`` BEFORE the
validator sees it:
  1. Replace ``[]`` or missing with sentinel.
  2. Scan each task's description for relative source-file paths
     and merge them into ``files_to_modify`` (with hygiene rules).

This file pins that contract so LLM-output volatility cannot break
the plan-load path again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gen():
    """A TasksGenerator instance wired against an in-memory plan_dir.

    We don't need the LLM / coding tool to test this pure helper;
    only the helper method itself.
    """
    from tasks_generator import TasksGenerator

    tool = _StubCodingTool(response={})
    plan_dir = Path("/tmp/backfill-test-plan")
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "prd.md").write_text("# PRD\n", encoding="utf-8")
    return TasksGenerator(coding_tool=tool, plan_dir=plan_dir)


class _StubCodingTool:
    """Minimal stand-in for the coding tool — not exercised by these tests."""

    def __init__(self, response: dict):
        self._response = response

    def run_query(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Backfill behavior
# ---------------------------------------------------------------------------


def test_backfill_replaces_empty_with_sentinel(gen):
    tasks = [{"id": "1", "title": "investigation", "description": "", "files_to_modify": []}]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["files_to_modify"] == ["__UNKNOWN_MODIFICATIONS__"]


def test_backfill_replaces_missing_field_with_sentinel(gen):
    tasks = [{"id": "1", "title": "investigation", "description": ""}]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["files_to_modify"] == ["__UNKNOWN_MODIFICATIONS__"]


def test_backfill_extracts_paths_from_description(gen):
    tasks = [
        {
            "id": "1",
            "title": "modify auth",
            "description": (
                "Touch backend/agent.py and backend/tests/unit/test_auth.py "
                "to fix the login flow."
            ),
            "files_to_modify": [],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    files = out[0]["files_to_modify"]
    assert "__UNKNOWN_MODIFICATIONS__" in files
    assert "backend/agent.py" in files
    assert "backend/tests/unit/test_auth.py" in files


# ---------------------------------------------------------------------------
# Pass 5 (2026-09-20): clear a flagged test_command instead of shipping it
# ---------------------------------------------------------------------------
#
# The detector used to warn and leave the command in place. That argument
# was about not *blocking the plan*, which still holds — but shipping the
# command is a separate question, and `repair_generator` already answers
# it the other way: a structurally-unpassable command scores every honest
# ``TEST_RESULT: PASSED`` as a lie, whereas an empty one degrades to the
# audit second pass, which can actually pass.

#: The verbatim shape.
PROBE_CHAIN = (
    "cd ~/Documents/x && grep -n 'TODO' src/types.rs && "
    "ls -la target/lib.dylib 2>/dev/null && "
    "ls -la venv/lib/*.so 2>/dev/null && "
    "source venv/bin/activate && python3 -c \"import x\""
)


def test_a_flagged_command_is_cleared_not_shipped(gen):
    tasks = [{
        "id": "1", "title": "t", "description": "", "files_to_modify": [],
        "test_command": PROBE_CHAIN,
    }]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["test_command"] == "", (
        "a command that can never exit 0 must not survive generation"
    )


def test_a_clean_command_survives(gen):
    tasks = [{
        "id": "1", "title": "t", "description": "", "files_to_modify": [],
        "test_command": "backend/.venv/bin/python3 -m pytest tests/x.py -v",
    }]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["test_command"].endswith("pytest tests/x.py -v")


def test_a_flagged_command_in_the_list_form_is_cleared_too(gen):
    """The form the generator actually writes.

    The 2026-09-20 regeneration produced 17 tasks, every one carrying
    ``test_commands`` (a list). A pass that reads only ``test_command``
    sees none of them — which is how two provably vacuous commands
    survived a check that reported zero problems.
    """
    tasks = [{
        "id": "1", "title": "t", "description": "", "files_to_modify": [],
        "test_commands": [PROBE_CHAIN, "pytest tests/x.py -q"],
    }]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["test_commands"] == ["pytest tests/x.py -q"]


def test_a_task_with_both_forms_has_the_offender_dropped_from_both(gen):
    tasks = [{
        "id": "1", "title": "t", "description": "", "files_to_modify": [],
        "test_commands": ["pytest tests/x.py -q"],
        "test_command": PROBE_CHAIN,
    }]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["test_commands"] == ["pytest tests/x.py -q"]
    assert out[0]["test_command"] == ""


def test_a_clean_list_form_command_survives(gen):
    tasks = [{
        "id": "1", "title": "t", "description": "", "files_to_modify": [],
        "test_commands": ["cargo test -p native_ext --lib types"],
    }]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["test_commands"] == ["cargo test -p native_ext --lib types"]


def test_clearing_happens_after_the_verification_only_tagging(gen):
    """Order matters: the tag keys on the task *having* a command.

    Clearing first would leave a sentinel-only task untagged, and the
    empty-output gate in ``agent.py`` would then block a task whose only
    deliverable was a written finding.
    """
    tasks = [{
        "id": "1", "title": "audit", "description": "",
        "files_to_modify": [], "test_command": PROBE_CHAIN,
    }]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["verification_only"] is True
    assert out[0]["test_command"] == ""


def test_a_task_with_no_command_is_left_alone(gen):
    """Pass 5 must not invent the key on a task that never had one.

    A missing ``test_command`` is the dual-signal gate's problem, not
    this pass's — and adding an empty one here would make an absent
    field look handled, which is the ``setdefault(..., "")`` bug the
    repair path already fixed.
    """
    tasks = [{"id": "1", "title": "t", "description": "", "files_to_modify": []}]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0].get("test_command") is None
    assert "verification_only" not in out[0]


def test_backfill_dedupes_with_existing(gen):
    tasks = [
        {
            "id": "1",
            "title": "modify auth",
            "description": "Touch backend/agent.py again.",
            "files_to_modify": ["backend/agent.py"],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    # No duplicates, no sentinel added because files_to_modify was non-empty.
    assert out[0]["files_to_modify"] == ["backend/agent.py"]


def test_backfill_skips_absolute_paths(gen):
    tasks = [
        {
            "id": "1",
            "title": "x",
            "description": "Touch /Users/whoami/foo.py to see if it works.",
            "files_to_modify": [],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert "/Users/whoami/foo.py" not in out[0]["files_to_modify"]
    assert "__UNKNOWN_MODIFICATIONS__" in out[0]["files_to_modify"]


def test_backfill_skips_urls(gen):
    tasks = [
        {
            "id": "1",
            "title": "x",
            "description": "See https://example.com/api/v1/foo.py for details.",
            "files_to_modify": [],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    # URL path was skipped. The non-URL part "foo.py" must NOT survive
    # by itself either because it's not preceded by a path separator.
    assert "https://example.com/api/v1/foo.py" not in out[0]["files_to_modify"]
    assert "foo.py" not in out[0]["files_to_modify"]
    assert "__UNKNOWN_MODIFICATIONS__" in out[0]["files_to_modify"]


def test_backfill_caps_extracted_paths_only(gen):
    """Cap applies ONLY to description-extracted paths, never to
    ``files_to_modify`` entries the LLM already wrote explicitly.
    The downstream ``_enforce_files_to_modify_limit`` helper splits
    tasks with >5 explicit files into children, so truncation here
    would lose data."""
    paths_in_desc = [f"src/file_{i}.py" for i in range(10)]
    explicit = ["src/explicit_a.py", "src/explicit_b.py", "src/explicit_c.py"]
    tasks = [
        {
            "id": "1",
            "title": "x",
            "description": "Touch " + " ".join(paths_in_desc),
            "files_to_modify": explicit,
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    files = out[0]["files_to_modify"]
    # Explicit entries preserved verbatim (no truncation).
    for path in explicit:
        assert path in files
    # Extracted paths capped at 5.
    extracted = [f for f in files if f not in explicit]
    assert len(extracted) == 5
    # The remaining 5 description-paths are dropped — the cap kicks in.
    assert "src/file_9.py" not in files


def test_backfill_skips_non_task_dicts(gen):
    """Robust against malformed LLM output that yields a string or None."""
    tasks = [
        "this-is-a-string-not-a-dict",
        None,
        {"id": "1", "title": "ok", "description": "backend/agent.py", "files_to_modify": []},
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    # First two pass through unchanged; third gets sentinel + path
    assert out[0] == "this-is-a-string-not-a-dict"
    assert out[1] is None
    assert "__UNKNOWN_MODIFICATIONS__" in out[2]["files_to_modify"]
    assert "backend/agent.py" in out[2]["files_to_modify"]


def test_backfill_returns_same_list_in_place(gen):
    """The helper returns the input list (mutated in place) for chaining."""
    tasks = [{"id": "1", "title": "x", "description": "", "files_to_modify": []}]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out is tasks