---
name: test-design
description: |
  Use when a project's test strategy (unit/integration/E2E coverage, mocking
  approach, CI gates, performance and security testing) hasn't been pinned
  down yet AND downstream task generation needs test specifications to embed
  into each task. Triggers on "how should I test this", "TDD strategy", "test
  coverage", or when the approved architecture introduces testability concerns
  the implementer can't infer. Skips for throwaway prototypes or when the
  user has already supplied the test plan.
license: MIT
compatibility: opencode
metadata:
  author: "Autonomous Coding System"
  version: "1.0.0"
  category: "development"
  workflow: "multi-turn"
  global: false
---

# Test Design Skill

## Overview

This skill guides structured test design based on an approved architecture (or PRD if architecture is skipped). It produces a test design document with SCQA decision points that define the testing strategy, coverage goals, and acceptance criteria before implementation begins.

## When to Use

- Architecture design is approved (or PRD is approved if no architecture phase)
- User explicitly asks for test design
- User says "design tests", "how should I test this", "TDD strategy"
- The project requires clear testing expectations and coverage goals

## When to Skip

- User explicitly says "no test design needed"
- Trivial prototypes with no testing requirements
- Test strategy is already defined by organization standards

## Core Methodology: SCQA Decision Points

Each test strategy decision is documented as a decision point:

| Field | Description |
|-------|-------------|
| **S**ituation | Current code characteristics or testing context |
| **C**omplication | Testing challenge, risk, or coverage gap |
| **Q**uestion | The specific test strategy decision needed |
| **A**nswer | Recommended test approach with justification |
| **Test Type** | Unit / Integration / E2E / Performance / Security |
| **Coverage Scope** | Which modules/layers this strategy covers |
| **Alternatives** | 1-2 alternative approaches with trade-offs |

## Key Test Design Areas

Ensure coverage of relevant areas:

1. **Unit Test Strategy** — Framework, mocking approach, coverage target
2. **Integration Test Strategy** — Module integration order, test data
3. **API Test Strategy** — Contract testing, boundary values, error cases
4. **E2E Test Strategy** — Critical user flows, test environment
5. **Performance Test Strategy** — Load metrics, stress scenarios, tools
6. **Security Test Strategy** — Auth testing, input validation, scanning
7. **Test Data Strategy** — Generation, isolation, cleanup
8. **CI/CD Test Pipeline** — Automation triggers, gating, reporting

## Test Case Design Patterns

### Equivalence Partitioning
Divide input domain into equivalent classes. Test one representative from each class.

### Boundary Value Analysis
Test at boundaries: min-1, min, min+1, max-1, max, max+1.

### Decision Table Testing
For complex business rules, create a table of conditions vs actions.

### State Transition Testing
For stateful systems, test all valid and invalid transitions.

## Review-Correction Loop

Same pattern as PRD and architecture review:

1. Present each test strategy decision point
2. User actions: accept / reject / question / skip
3. If any rejected → collect feedback → refine → re-present
4. Loop until all accepted/skipped (max 3 rounds)

## Output Format

Test design saved as `plans/{plan-id}/test-design.md`:

```markdown
# 测试设计 — {Project Name}

## 概述
{Testing philosophy and goals}

## 测试策略决策点列表

### 决策点 1: {Title}

**[S] 现状：** ...
**[C] 矛盾：** ...
**[Q] 问题：** ...
**[A] 方案：** ...

测试类型：...
覆盖范围：...
备选方案：① ... ② ...
```

## Integration with System

1. Load approved architecture from `plans/{plan-id}/arch-design.md` (or PRD if no arch)
2. Call `POST /api/test/{plan_id}/generate` to trigger generation
3. Present decision points for user review
4. Call `POST /api/test/{plan_id}/review/item/{index}` for each action
5. If rejected items exist, call `POST /api/test/{plan_id}/refine`
6. After approval, proceed to task generation

## TDD Integration

When test design is approved, the test specifications should be embedded in task descriptions as TDD specs:

```
TDD 规格：
- test_login_success: 有效凭证 → 200 + token
- test_login_invalid: 错误密码 → 401
- test_login_not_found: 不存在用户 → 401
```

This format is consumed by the TasksGenerator to produce testable tasks.
