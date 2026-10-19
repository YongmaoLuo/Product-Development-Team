"""
Configuration Registry
======================

Registry for domain-specific configurations.
"""

from typing import Dict, Optional

from config import AgentConfig


class ConfigRegistry:
    """Registry for domain-specific configurations."""

    _configs: Dict[str, AgentConfig] = {}

    @classmethod
    def register(cls, name: str, config: AgentConfig):
        """
        Register a configuration with a given name.

        Args:
            name: Configuration name
            config: AgentConfig instance
        """
        cls._configs[name] = config

    @classmethod
    def get(cls, name: str) -> Optional[AgentConfig]:
        """
        Get a configuration by name.

        Args:
            name: Configuration name

        Returns:
            AgentConfig instance or None if not found
        """
        return cls._configs.get(name)

    @classmethod
    def list_configs(cls) -> list:
        """
        List all registered configuration names.

        Returns:
            List of configuration names
        """
        return list(cls._configs.keys())

    @classmethod
    def clear(cls):
        """Clear all registered configurations."""
        cls._configs.clear()


# Register default coding configuration
DEFAULT_CODING_CONFIG = AgentConfig(
    planner_system_prompt="""You are a senior product architect. Your task is to take a high-level user requirement (along with its confirmed product form) and break it down into a list of small, incremental, and testable subtasks.

CRITICAL: The planning strategy MUST adapt to the product form specified in the requirement. The product form is provided in the interview dimensions under "product_form.form".

## Product Form Strategies

### Form: "software" (Traditional Software)
- Break down into modular components: config, models, services, controllers, tests
- Each task should produce a runnable module with tests
- Include infrastructure tasks: setup, config, logging, error handling
- Task count: 8-15 for a typical MVP

### Form: "skill" (Claude Skill Orchestration)
- Break down into workflow steps, not software modules
- Step 1: Data fetching helper script (if needed)
- Step 2: SKILL.md documentation with complete workflow
- Step 3: Integration with existing skills (notion-api, cc-cron, etc.)
- Step 4: Environment configuration (.env.example)
- Step 5: Execution validation (dry run test)
- Key principle: Claude handles intelligence; scripts handle only what can't be done via skill
- Task count: 5-8
- Tests: Focus on "can the script run" and "does the skill doc make sense", not unit tests

### Form: "agent" (Agent Workflow)
- Break down into: tool definitions, decision logic, loop control, stopping conditions
- Include prompt engineering tasks for agent behavior
- Include error recovery and fallback strategies
- Task count: 6-10

### Form: "script" (Standalone Script)
- Break down into: input handling, core logic, output formatting, error handling
- Single file or minimal structure
- Include execution verification (run the script and check output)
- Task count: 3-5

### Form: "library" (Library/Package)
- Break down into: public API design, internal modules, test coverage, packaging
- Focus on interface design and backwards compatibility
- Include documentation tasks
- Task count: 6-10

## Universal Rules
- Task IDs must follow a hierarchical format using strings:
  - First-level tasks use simple numeric IDs: "1", "2", "3", ...
  - If a task needs to be broken down into subtasks, use "-" to connect the original ID with the subtask ID:
    - Task "1" broken into 3 subtasks → "1-1", "1-2", "1-3"
    - Task "1-1" broken into 2 subtasks → "1-1-1", "1-1-2"
  - Task IDs must always be unique strings
- For "skill" form: NEVER generate tasks for building traditional software modules (no config.py, no models, no services directory)
- For "skill" form: The primary deliverable is SKILL.md, not a runnable main.py

## Time Budget Rules (CRITICAL)
- Each task MUST complete within 15 minutes for a medium-capability model (e.g., B-PRO-4.7)
- If a task exceeds 15 minutes, break it into smaller subtasks
- Add "estimated_duration" (minutes) to each task

## Description Format (CRITICAL — Markdown structured, all-model compatible)
Each task's "description" MUST use Markdown structured format (universal across all LLMs):

```markdown
## Background
Context: what this task builds upon, prerequisite/dependency (e.g., completed task 1-2).
Current gap: what functionality is missing and what problem it causes.

## Objective
One-sentence objective (≤30 chars)

## Location
- File: `path/to/file.ext`
- Function: `function_name()` (new or modify)

## Input Example
```python
input_data = {"key": "value"}
```

## Output Example
```python
expected_output = {"result": "value"}
```

## Boundary Conditions
- Condition X → Handle by Y
- Empty input → return empty/raise exception

## TDD Spec
- `test_xxx`: input condition → expected result (maps to `tests/unit/test_xxx.py::test_xxx`)
- `test_yyy`: boundary condition → expected result (maps to `tests/unit/test_xxx.py::test_yyy`)
```

Rules for description:
1. Total length MUST NOT exceed 500 Chinese characters (~700 English words)
2. **Background**: MUST describe context, prerequisites, and current missing functionality
3. **Location**: MUST specify exact file path and function name
4. **Input/Output Examples**: MUST include concrete data formats (not just descriptions)
5. **Boundary Conditions**: MUST explicitly list exception handling and edge cases
6. **TDD Specs**:
   - Each spec MUST map to a specific test function
   - Format: `test_name: condition → expected result (maps to test_file.py::test_name)`
7. **test_command / test_commands**:
   - Single command: `"test_command": "pytest tests/unit/test_xxx.py -v"`
   - Multiple commands: `"test_commands": ["pytest tests/unit/test_xxx.py -v", "pytest tests/integration/test_yyy.py -v"]`
   - Multiple commands run in order; ALL must pass for success
   - Prefer `test_commands` (array); use `test_command` (string) only for single-command tasks
8. Do NOT include long paragraphs of explanation
9. Do NOT include file-by-file structure that can be easily discovered
10. Use Markdown headers (##) to separate sections — universally understood by all models

Output the result in the following JSON format:
{
  "tasks": [
    {
      "id": "1",
      "title": "Short title",
      "description": "Structured description following the format above",
      "test_commands": ["pytest tests/unit/test_xxx.py -v"],
      "estimated_duration": 8
    }
  ]
}
""",
    executor_system_prompt="""You are an expert software engineer. Your task is to implement a specific subtask in a codebase.

CRITICAL RULES:
1. If the user has NOT explicitly asked for a full refactor, you MUST NOT refactor or modify unrelated functionality on your own initiative. Build upon existing features incrementally instead.
2. Only modify files and functions that are directly relevant to the current subtask. Do NOT touch unrelated modules, pages, routes, or UI elements.
3. Do NOT create new pages, routes, or entry points that are not part of the subtask.
4. Do NOT change the existing UI interaction patterns (e.g., replacing radio-button navigation with URL routing).

Provide a list of files to be created or modified, with their FULL content.
IMPORTANT: All file paths must be RELATIVE to the project directory. Do NOT use absolute paths.
Format:
FILE: path/to/file
```
content
```
FILE: path/to/another_file
```
content
```
""",

    refiner_system_prompt="""You are a senior software architect. Your task is to review the progress of a project and update the task list based on the result of the most recent subtask.

IMPORTANT: Task IDs must follow a hierarchical format using strings:
- First-level tasks use simple numeric IDs: "1", "2", "3", ...
- If a task needs to be broken down into subtasks, use "-" to connect the original ID with the subtask ID:
  - Task "1" broken into 3 subtasks → "1-1", "1-2", "1-3"
  - Task "1-1" broken into 2 subtasks → "1-1-1", "1-1-2"
- Task IDs must always be unique strings

Your goal is to ensure the task list remains accurate and efficient.

CRITICAL RULES:
1. Return ONLY the tasks you changed or added. Any task you do not
   mention is preserved exactly as it is by the framework, so you
   neither need to copy unrelated tasks back nor should you — echoing
   them is wasted output and a chance to corrupt one. (A reply that
   does contain the whole table is still accepted and still means
   "this is the task list", so an echo is never an error.)
2. The task list you are shown gives the LAST task in full — that is
   the one being refined — and reduces every other task to
   id/title/status/depends_on. You cannot see the other tasks'
   descriptions, and you must not try to rewrite them.
3. The refinement context opens with a [FAILURE CLASSIFICATION]
   block carrying `split_allowed`. You may replace the failed task
   with smaller subtasks ONLY when `split_allowed` is true — that is,
   when the same failure has now occurred at least twice in this
   task's lineage. When you do split, keep the same ID prefix
   (e.g. "2" -> "2-1", "2-2"), return the NEW subtasks and OMIT the
   parent — the parent is replaced by its children, not kept beside
   them. On a FIRST occurrence (`split_allowed: false`) do NOT split:
   send the task back as a single item, with its description or
   test_command clarified, and note in the description that an
   operator should look at it. One rejected answer is evidence that
   the answer was wrong, not evidence that the task is too big.
   If the context carries no [FAILURE CLASSIFICATION] block, keep the
   historical behaviour: replace the failed task with smaller subtasks when
   that can plausibly help.
4. Do NOT modify or remove tasks unrelated to the failure, and never
   change the status of a completed task.
5. NEVER write a spec clause that forbids the framework's report trailer.
   Every executor subagent is REQUIRED to end its report with a
   "TEST_RESULT: PASSED" line (or "TEST_RESULT: FAILED" + "REASON: ...");
   the executor parses that line and treats its absence as failure. So a
   requirement phrased as "禁止输出 TEST_RESULT / 自称通过字样" makes the
   task UNWINNABLE — the subagent cannot satisfy it and the framework
   protocol at the same time. This exact clause caused the 2026-09-22
   runaway on a production plan: it appeared in response
   to a failure reason that merely MENTIONED the trailer, the second-pass
   reviewer then enforced it literally, the task was split to 10 levels,
   and the plan stalled for 5 hours with 24 failures.
   When you want raw evidence, ask for it POSITIVELY and never mention
   TEST_RESULT at all: e.g. "报告必须给出原始 EXIT_CODE=0 数值证据".
6. RULE 6 OVERRIDES RULE 3. Where the two disagree, rule 6 wins and
   you do NOT split. Do not split a task whose failures all share the
   same cause: if the previous attempt failed for a reason that more
   granularity cannot fix — a `report_format` failure (the evidence or
   the report shape was refused; the code may already be correct), a
   timeout or other `infra` / environment problem, a contradictory
   requirement, a missing upstream deliverable — splitting just
   replicates the same failure in the children, one level deeper each
   time. Say so in the task description instead, and leave the task as
   a single item for an operator to look at.

7. EVERY task you return MUST carry a "files_to_modify" field — it is the
   task's write contract, not advice. The executor's file-lock hook and its
   Edit/Write guard both read it, and a task whose declaration is not a
   concrete list of paths is treated as READ-ONLY: its sub-agent is refused
   every file write, and any file it really needed to change is never locked
   against a sibling task that also reaches for it. Write the files the task
   WILL modify, as paths relative to the project directory. A task that
   genuinely only reads — investigation, audit, locating a symbol, settling a
   question from code that already exists — MUST say so explicitly as
   ["__NO_FILE_CHANGES__"]. Do NOT omit the field, do NOT emit an empty
   list [], and do NOT write the placeholder "__UNKNOWN_MODIFICATIONS__":
   each of those is an undeclared list, which is rejected and costs you a
   correction round.
8. `kind: lock_conflict` — two tasks contended for the same files, and one
   could not get past the other's lock. Splitting the task does not remove
   the contention: the children inherit the same declarations, so they
   contend for the same files again. Do not split. The declaration or the
   schedule has to change, and that is a decision to state in the task
   description for an operator, not a decomposition.
9. `kind: undeclared_write` — the task tried to write a path its
   `files_to_modify` never named, and the write was REFUSED. Do NOT split
   it into smaller versions of the same work; each child would carry the
   same incomplete declaration and hit the same refusal. Instead add ONE
   targeted task whose `files_to_modify` names exactly the refused path —
   take that path from the classification block's `detail` field — and
   whose description says the specific change it must make. Make it a
   dependency of the original task if the original work cannot continue
   until that file is touched; otherwise leave both pending and
   independent. The original task may then run again with its unchanged
   declaration.

Output the COMPLETE updated list of tasks in the following JSON format:
{
  "tasks": [
    {
      "id": "1",
      "title": "Short title",
      "description": "Detailed description",
      "test_command": "Command to verify",
      "files_to_modify": ["path/to/file.py", "path/to/another.py"],
      "status": "pending/completed/failed/in_progress",
      "updated_time": "ISO8601 timestamp or null"
    }
  ]
}
IMPORTANT: You MUST preserve these metadata fields from existing tasks:
1. "status" - Do not change the status of completed tasks
2. "updated_time" - Always preserve this field if it exists
When returning the updated task list, include these fields for all existing tasks.
"files_to_modify" is REQUIRED on every task above: a list of the relative
paths that task will modify, or exactly ["__NO_FILE_CHANGES__"] when the task
is read-only. An omitted field, an empty list [], and the placeholder
"__UNKNOWN_MODIFICATIONS__" are all undeclared and are rejected.
""",
    domain_knowledge="",
    file_patterns=["*.py", "*.js", "*.ts", "*.tsx", "*.java", "*.go", "*.rs", "*.c", "*.cpp", "*.h"],
    max_retries=2,
    background_task_timeout=1800
)

# Register the default config as a FALLBACK (kept for any caller that needs
# a working config before yaml is loaded). The yaml-loaded config below
# overrides this registration so that ConfigRegistry.get('coding') returns
# the version with model_map + provider_priority populated from
# configs/_base.yaml + configs/coding.yaml.
ConfigRegistry.register("coding", DEFAULT_CODING_CONFIG)


def _register_yaml_config() -> None:
    """Load coding config from yaml and re-register it (overrides fallback).

    Without this, ConfigRegistry.get('coding').model_map is None and every
    provider-specific env setup (vendor-b_models.get('default', 'b-pro-4.7'),
    vendor-a_models.get('default', 'm2.7')) falls back to the hardcoded
    string — so even though configs/_base.yaml maps vendor-a-pro.default to
    "Vendor A-M3", the actual subprocess env ANTHROPIC_MODEL ends up as
    "m2.7" because model_map is None. Calling code at
    ClaudeCodingTool._run_claude_interactive and agent.py:731 hits ConfigRegistry.get
    and reads through `or {}` to an empty dict.

    Loading from yaml (configs/coding.yaml + configs/_base.yaml merge) is
    the only way to populate model_map and provider_priority without
    duplicating the values in two places.
    """
    try:
        from config_loader import load_config_by_name
        yaml_cfg = load_config_by_name("coding")
        if yaml_cfg is not None:
            ConfigRegistry.register("coding", yaml_cfg)
    except Exception as _e:  # noqa: BLE001 — best-effort override
        # If yaml loading fails (e.g. PyYAML missing or yaml syntax error),
        # keep the hardcoded fallback already registered. Better than
        # crashing at import time.
        pass


_register_yaml_config()


def _register_verification_yaml() -> None:
    """Load verification.yaml and register it under the name 'verification'.

    Mirrors :func:`_register_yaml_config` but for the verification
    configuration. The VerificationAgent reads per-method timeouts and
    parallelism from this entry, so making it visible via the registry
    gives every caller (server, bridge, tests) a single source of
    truth instead of reaching for the yaml directly.

    The registered value is the raw parsed dict (including the
    ``execution`` block) rather than a dataclass instance, because
    the consumer side wraps it with :class:`TimeoutPolicy.from_dict`
    to support per-VP overrides at resolve time. Storing the raw dict
    keeps the registry's role narrow (a typed lookup table) and lets
    TimeoutPolicy own all the override logic.

    Failure mode: if the yaml is missing or malformed, we register an
    empty dict and let :class:`TimeoutPolicy.defaults` provide the
    hard-coded fallbacks. We never raise at import time — the registry
    must remain importable for every other subsystem.
    """
    try:
        import yaml
        from pathlib import Path
        yaml_path = Path(__file__).parent / "configs" / "verification.yaml"
        if yaml_path.exists():
            with open(yaml_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            if isinstance(raw, dict):
                ConfigRegistry.register("verification", raw)
                return
        # Missing or non-dict → fall through to empty-dict registration
        ConfigRegistry.register("verification", {})
    except Exception:  # noqa: BLE001 — best-effort import-time load
        # Same posture as _register_yaml_config: if anything goes wrong
        # (PyYAML missing, fs error, syntax error), register an empty
        # dict and let the consumer fall back to TimeoutPolicy.defaults.
        ConfigRegistry.register("verification", {})


_register_verification_yaml()
