---
name: req-clarification
description: |
  Use when user describes a new software project, feature, skill, agent, or
  automated workflow but hasn't pinned down the product form, scope, or
  acceptance criteria. Triggers on vague one-sentence requests ("build me X"),
  ambiguous intents, or when a downstream phase (PRD, architecture) lacks
  concrete inputs. Activates when the user has not yet confirmed whether they
  want a deployable service, a Claude skill, an agent loop, a standalone
  script, or a library.
license: MIT
compatibility: opencode
metadata:
  author: "Autonomous Coding System"
  version: "1.1.0"
  category: "development"
  workflow: "multi-turn"
  global: false
---

# Requirement Clarification Skill

## Overview

This skill guides structured multi-turn requirement gathering to ensure all development tasks start with well-defined, actionable requirements. It prevents ambiguity, scope creep, and misalignment between user intent and implementation.

**Critical principle: The "product form" (what shape the deliverable takes) must be clarified FIRST, before any technical details.**

## When to Use

- User says "I want to build...", "Create a...", "Implement...", "Add a feature...", "Make a skill..."
- User provides a vague or one-sentence requirement
- Starting any new project, skill, agent, or automated workflow
- Before any code generation, architecture discussion, or documentation writing

## Core Methodology: 6-Dimension Framework

Every requirement must be clarified across these dimensions. **Dimension 1 (Product Form) is the most critical — it determines everything that follows.**

| Dimension | Required | Description |
|-----------|----------|-------------|
| **Product Form** | **YES** | What shape does the deliverable take? Traditional software, Claude skill, agent workflow, standalone script, library/package, or documentation-only? |
| **Background** | Yes | What problem are we solving? Who is the target user? What is the current pain point? |
| **Goals** | Yes | What does success look like? Quantifiable or verifiable outcomes. |
| **Scope** | Yes | What is IN scope? What is explicitly OUT of scope? Clear boundaries. |
| **Constraints** | Optional | Tech stack, timeline, budget, compliance, performance requirements. |
| **Acceptance** | Yes | How do we verify completion? Executable test conditions. |

## Product Form Decision Tree

The product form is not a vague preference — it is a concrete architectural decision with significant implications. Use this decision tree to guide the conversation:

### Form A: Traditional Software (独立软件)
- **Definition**: A standalone, deployable software system with its own entry point, configuration, and lifecycle.
- **Examples**: Web service, CLI tool, desktop app, mobile app.
- **Key traits**: Has `main.py`, config files, tests, CI/CD. Runs independently without Claude Code.
- **When to choose**: User needs a service that runs on a schedule (cron), serves requests (API), or runs as a background process.

### Form B: Claude Skill (纯Skill编排)
- **Definition**: An orchestration document (SKILL.md) that guides Claude Code through a multi-step workflow, leveraging other skills for sub-tasks.
- **Examples**: A skill that fetches data, then uses `notion-api` skill to write to Notion, then uses `cc-cron` skill to schedule.
- **Key traits**: No `main.py`. Logic is in Claude's context. Heavy use of other skills. Minimal helper scripts (only for data fetching/API calls that can't be done via skill).
- **When to choose**: The workflow requires intelligence (semantic understanding, quality judgment) that Claude can provide directly. The workflow composes existing capabilities (Notion, cron, web search) rather than building new ones.

### Form C: Agent Workflow (Agent工作流)
- **Definition**: An autonomous agent that makes decisions, uses tools, and iterates toward a goal with minimal human intervention.
- **Examples**: A research agent that searches the web, reads papers, and writes a report.
- **Key traits**: Has a loop (observe → think → act). May use LLM for planning. Tool use is dynamic, not pre-scripted.
- **When to choose**: The task requires exploration, iteration, or adaptation. The exact steps can't be fully predetermined.

### Form D: Standalone Script (独立脚本)
- **Definition**: A single-file or minimal script that performs a specific task when executed.
- **Examples**: A data migration script, a one-time report generator.
- **Key traits**: Usually one file. No complex architecture. No tests needed. Run manually or via cron.
- **When to choose**: One-off or simple recurring task. No need for skill orchestration or complex logic.

### Form E: Library/Package (库/包)
- **Definition**: A reusable module designed to be imported by other code.
- **Examples**: A Python package for data transformation, a utility library.
- **Key traits**: Has `setup.py` or `pyproject.toml`. Public API. Documentation focused on usage, not execution.
- **When to choose**: The user explicitly says "I want a reusable component" or "package/library".

## Scope Pre-check（首轮）

The first round of the interview is a **single-shot LLM call** that runs BEFORE the 6-dimension framework is iterated. It classifies the user's intent into a 4-class uppercase `product_form` enum and proposes draft `in_scope` / `out_of_scope` lists.

This is the backend `interviewer.scope_precheck()` step (DP8-1). LUI mode (Claude Code invoking the skill directly) MUST perform this step on the first turn — skipping it forces the downstream 6-dim loop to guess the product form, which is exactly the drift the 6-dim framework is designed to prevent.

### Scope Pre-check Contract

- **Input**: the user's initial requirement (one or a few sentences)
- **Output**: a JSON object with exactly 4 top-level keys:
  ```json
  {
    "product_form": "SOFTWARE" | "SKILL" | "AGENT" | "WORKFLOW",
    "in_scope":     ["...", "..."],
    "out_of_scope": ["...", "..."],
    "reasoning":    "one sentence explaining the classification"
  }
  ```
- **product_form** MUST be one of the 4 uppercase enum values below. Any other token (e.g. "ROBOT", "service", "tool") falls back to `"SOFTWARE"` — never guess outside the enum.

### 4-class Product Form enum

| Value | Meaning |
|-------|---------|
| `SOFTWARE` | 通用软件 / 库 / SDK / 服务 / daemon / CLI 工具 |
| `SKILL`    | Claude / Agent 技能包（如 `.claude/skills/<name>/SKILL.md` + `scripts/`） |
| `AGENT`    | 自主代理 / agent loop / 长期运行的 agent 流程 |
| `WORKFLOW` | 多步骤工作流编排 / pipeline |

The `product_form` value is persisted onto the interview state and carried forward through every subsequent round (one-question-per-turn loop, dimension filling, etc.). The downstream PRD generator reads it to pick the appropriate planning strategy.

### When the pre-check is ambiguous

If the user's initial requirement is too vague to classify (e.g. "build me something cool"), the pre-check still runs — it defaults to `product_form="SOFTWARE"` with empty `in_scope` / `out_of_scope` lists. The Round 2+ questions then probe the user to refine the form if needed.

## 一次一个问题（默认）

After the scope pre-check, the interview proceeds **one question at a time** (replacing the legacy "ask up to 3 questions per round" behaviour). This is the backend `interviewer.next_question()` contract (DP8-2).

### Why one question per turn

- **Auditability**: each `chat_history` row maps to one user-message → one assistant-question, so the interview JSON is easy to replay and diff.
- **State machine clarity**: the dimension-filling loop advances exactly one step per LLM call. The state machine has no "batch advancement" edge cases.
- **Failure recovery**: if an LLM call fails mid-way, the partial progress is preserved as N rows in `chat_history` instead of a half-answered batch.
- **Cognitive load**: the user sees a single, focused question — not a wall of three — and is more likely to give a concrete answer.

### One-question contract

- Each call to "next question" returns **at most one** question (a single string, possibly with a short list of example options).
- The question targets the **highest-priority unfilled dimension** in the order: Product Form (already filled by pre-check) → Background → Goals → Scope → Constraints → Acceptance.
- After the user answers, the dimension is updated, the `chat_history` row is appended, and the loop asks the NEXT single question.
- The loop terminates when ALL required dimensions have concrete, actionable descriptions — at which point `interview.json` is written with `status="complete"`.

LUI mode MUST NOT batch multiple questions into a single assistant message. Doing so breaks the audit chain (`chat_history` rows would mix multiple user answers into one) and forces the backend to re-derive which dimension each answer belongs to.

## Product Form 字段（SOFTWARE/SKILL/AGENT/WORKFLOW）

The `product_form` field is a **REQUIRED** top-level key on every `interview.json` written by LUI mode. The value MUST be one of the 4 uppercase enum strings — case-sensitive, no lower-case variants, no additions.

### Enum definition (locked contract)

```python
SCOPE_PRECHECK_VALID_FORMS = frozenset({"SOFTWARE", "SKILL", "AGENT", "WORKFLOW"})
```

| Form | LUI mode role |
|------|---------------|
| `SOFTWARE` | Independent deployable software. Tasks decomposed as modular code + tests + CI/CD. |
| `SKILL` | Claude / Agent skill package. Tasks decomposed as `SKILL.md` + helper scripts + skill-composition wiring. |
| `AGENT` | Autonomous agent loop. Tasks decomposed as tools + decision rules + stop conditions. |
| `WORKFLOW` | Multi-step workflow / pipeline. Tasks decomposed as ordered steps + state-passing contracts. |

### Mapping rules

- If the user explicitly says "skill" / "Claude skill" / ".claude/skills/..." → `SKILL`.
- If the user says "agent" / "agent loop" / "long-running bot" / "autonomous research" → `AGENT`.
- If the user describes a pipeline / orchestration / sequential steps that compose existing tools → `WORKFLOW`.
- Otherwise (CLI tool, web service, library, daemon, mobile app, etc.) → `SOFTWARE`.

### Output example (LUI mode writes this into `interview.json`)

```json
{
  "plan_id": "20260707-task-app",
  "created_at": "2026-07-07T10:00:00Z",
  "status": "in_progress",
  "product_form": {
    "form": "SOFTWARE",
    "form_label": "Software",
    "reasoning": "User wants a FastAPI + SQLite task tracker, runnable as a service."
  },
  "dimensions": {
    "background": "...",
    "goals": "...",
    "scope": {"in": [...], "out": [...]},
    "constraints": {},
    "acceptance": "..."
  },
  "chat_history": [...]
}
```

The `form` field inside `product_form` MUST equal one of the 4 uppercase enum values. Downstream phases (PRD generator, arch generator, task generator) read this field and refuse to proceed if it is missing or invalid.

## Multi-Turn Dialogue Strategy

### Round 1: Product Form (MANDATORY — never skip)

**The first question must ALWAYS be about product form.** Do not ask about tech stack, features, or architecture before the product form is confirmed.

Examples:
- "你期望的交付物是什么形式？选项：A) 独立运行的软件系统 B) Claude skill 编排文档 C) 自主Agent工作流 D) 独立脚本 E) 可复用的库/包"
- "这个功能是需要独立部署的服务，还是通过 Claude skill 编排来完成？"
- "你需要的是一个可独立运行的 Python 项目，还是一个 Claude 可以加载并执行的 skill？"

> **Note**: the legacy "Round 1 = product form" workflow above is now superseded by the **Scope Pre-check（首轮）** step. The pre-check classifies product_form, in_scope, and out_of_scope in a single LLM call before the 6-dim loop starts. LUI mode should call the pre-check first, then proceed to the one-question-per-turn loop.

### Round 2+: Derive Based on Product Form

Once the product form is confirmed, all subsequent questions are tailored to that form:

**If Form A (Traditional Software):**
- Q: What platforms? Web, CLI, desktop?
- Q: Deployment target? Local, server, cloud?
- Q: Any existing codebase to integrate with?

**If Form B (Claude Skill):**
- Q: What existing skills should this compose? (e.g., notion-api, cc-cron, tushare)
- Q: What parts need Claude's intelligence vs. what can be a simple script?
- Q: Trigger condition? Manual, cron, or event-driven?

**If Form C (Agent Workflow):**
- Q: What tools does the agent need? (web search, file read, code execution)
- Q: What is the stopping condition? (time limit, goal achieved, max iterations)
- Q: How does the agent handle failure or ambiguity?

**If Form D (Standalone Script):**
- Q: How is it triggered? Manual run, cron, or CI pipeline?
- Q: Input/output format?
- Q: Any error handling requirements?

**If Form E (Library/Package):**
- Q: Target language and ecosystem? (Python, JavaScript, etc.)
- Q: Public API surface — what functions/classes should be exposed?
- Q: Backwards compatibility requirements?

### Rules
1. **Max 3 questions per round** — respect user's cognitive load
2. **Be specific** — avoid generic questions like "What do you want?"
3. **Derive, don't repeat** — build on answers already given
4. **Probe for actionability** — reject vague descriptions like "make it good"
5. **Stop when all dimensions have concrete, actionable descriptions**
6. **NEVER proceed to PRD generation before product form is confirmed**

### Anti-Patterns to Avoid

- ❌ Accepting vague requirements like "make it fast" or "good UX"
- ❌ Skipping the product form dimension
- ❌ Assuming "software" when user says "skill"
- ❌ Asking tech stack questions before confirming product form
- ❌ Proceeding to code without all required dimensions filled
- ❌ Treating "skill" as "software with a skill wrapper" — they are fundamentally different architectures

## Output Format

When all dimensions are complete, produce `interview.json`:

```json
{
  "plan_id": "20260424-task-app",
  "created_at": "2026-04-24T10:00:00Z",
  "status": "complete",
  "product_form": {
    "form": "skill",
    "form_label": "Claude Skill",
    "reasoning": "User needs semantic deduplication and quality filtering that Claude can provide directly. Notion operations can reuse notion-api skill."
  },
  "dimensions": {
    "background": "Team of 10 developers needs lightweight task tracking...",
    "goals": "Launch MVP in 2 weeks with basic CRUD + drag-drop prioritization",
    "scope": {
      "in": ["Task CRUD", "Drag-drop priority", "Due dates"],
      "out": ["Real-time sync", "Mobile app", "Reporting dashboard"]
    },
    "constraints": {
      "tech_stack": "React + FastAPI + SQLite",
      "deadline": "2026-05-08"
    },
    "acceptance": "All API endpoints tested with pytest, frontend E2E with Playwright, code coverage > 80%"
  },
  "chat_history": [...]
}
```

**Important**: The `product_form` field is REQUIRED. It must be one of: `software`, `skill`, `agent`, `script`, `library`.

## Integration with System

1. Save `interview.json` to `plans/{plan-id}/`
2. The planner reads `product_form` from `interview.json` and selects the appropriate planning strategy:
   - `software` → Traditional modular task decomposition
   - `skill` → Workflow step decomposition + helper script tasks
   - `agent` → Tool + decision loop decomposition
   - `script` → Single-file task with execution verification
   - `library` → API surface + test coverage decomposition
3. Call `POST /api/prd/{plan_id}/generate` to trigger PRD generation
4. The PRD generator reads `product_form` and adapts the PRD structure accordingly
5. Hand off to PRD review phase — do NOT generate PRD yourself
