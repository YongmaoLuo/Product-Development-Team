---
name: arch-design
description: |
  Use when a project spans multiple modules or services AND its module
  boundaries, tech stack, data model, or interface contracts haven't been
  pinned down yet. Triggers on "architecture", "tech stack", "system design",
  "module boundaries", "how should I structure this", or when the approved
  PRD references decisions that belong at this layer. Skips for single-file
  scripts, prototypes with pre-fixed stacks, or when the user has already
  supplied the architecture.
license: MIT
compatibility: opencode
metadata:
  author: "Autonomous Coding System"
  version: "1.0.0"
  category: "development"
  workflow: "multi-turn"
  global: false
---

# Architecture Design Skill

## Overview

This skill guides structured architecture design based on an approved PRD. It produces an architecture design document with SCQA decision points that the user reviews and approves before implementation.

## When to Use

- PRD is fully approved (all decision points accepted/skipped)
- User explicitly asks for architecture design
- User says "design the architecture", "how should I structure this", "what tech stack"
- The project involves multiple components, services, or architectural choices

## When to Skip

- Simple single-file scripts or trivial utilities
- User explicitly says "no architecture phase needed"
- Prototype/MVP where tech decisions are already fixed

## Core Methodology: SCQA Decision Points

Each architecture decision is documented as a decision point using the SCQA framework:

| Field | Description |
|-------|-------------|
| **S**ituation | Current technical background or context |
| **C**omplication | Technical challenge, constraint, or conflict |
| **Q**uestion | The specific architectural decision needed |
| **A**nswer | Recommended solution with justification |
| **Impact Scope** | Which modules/layers/components are affected |
| **Alternatives** | 1-2 alternative approaches with trade-offs |

## Key Architecture Areas

Ensure coverage of relevant areas based on PRD scope:

1. **Tech Stack Selection** — Language, framework, database, deployment platform
2. **System Layering** — Presentation, business logic, data access layers
3. **Module Boundaries** — How functionality is partitioned into modules/services
4. **Data Model** — Core entities, relationships, storage strategy
5. **Interface Design** — API style (REST/GraphQL/gRPC), protocols, serialization
6. **Concurrency Strategy** — Threading, async, message queues
7. **Security Architecture** — Authentication, authorization, data protection
8. **Deployment Architecture** — Containerization, orchestration, CI/CD
9. **Observability** — Logging, metrics, tracing, alerting
10. **Scalability Strategy** — Horizontal/vertical scaling, caching, load balancing

## Review-Correction Loop

After generating the architecture document:

1. Present each decision point to the user
2. User actions: accept / reject / question / skip
3. If any rejected → collect feedback → refine document → re-present
4. Loop until all accepted/skipped (max 3 rounds)
5. Only proceed to test design or task generation after full approval

## Output Format

Architecture design is saved as `plans/{plan-id}/arch-design.md`:

```markdown
# 架构设计 — {Project Name}

## 概述
{Design philosophy and high-level goals}

## 架构决策点列表

### 决策点 1: {Title}

**[S] 现状：** ...
**[C] 矛盾：** ...
**[Q] 问题：** ...
**[A] 方案：** ...

影响范围：...
备选方案：① ... ② ...
```

## Integration with System

1. Load approved PRD from `plans/{plan-id}/prd.md`
2. Call `POST /api/arch/{plan_id}/generate` to trigger generation
3. Present decision points for user review via Web UI or chat
4. Call `POST /api/arch/{plan_id}/review/item/{index}` for each action
5. If rejected items exist, call `POST /api/arch/{plan_id}/refine`
6. After approval, ask user if they want test design phase
