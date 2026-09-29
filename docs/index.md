# Product Development Team

A spec-driven **agent harness** that does the work of a product development
team — the dozens of people it normally takes to go from a one-line idea to
something verified and delivered — with one operator driving it.

You give it a one-line requirement. It clarifies the requirement, writes a
reviewable PRD, optionally designs the architecture and the test strategy,
generates a task graph, **executes the tasks concurrently**, and then
**verifies the result concurrently** against the documents it produced —
feeding failures back as repair tasks and re-verifying, until the acceptance
criteria hold or the loop budget runs out. The whole span, from requirement
analysis to final test acceptance, is one auditable pipeline rather than a
chat session.

---

一个 spec 驱动的 **agent harness**，用来替代原本需要几十个人才能组成的一个完整
产品开发团队 —— 从一句话想法走到经过验证的交付，全程由一名操作者驱动。

---

## The point is not "an AI writes code"

Plenty of tools will turn a prompt into a diff. What is different here is
that **every step leaves a structured artifact, and the system is judged
against those artifacts** rather than against its own summary of what it
did.

That has three consequences you can observe:

* The PRD, the architecture document and the test design are **reviewable**
  before anything is built. Each decision is a CPEA record — Context,
  Problem, Evaluation, Action — so a reviewer sees the evidence behind a
  choice, not just the choice.
* Execution is judged by **two independent signals**: the agent's own
  report *and* its declared `test_command` exit code. Either alone is
  insufficient.
* Verification reads the documents the earlier phases produced and checks
  the running code against them. A verifier that only asked "does the test
  pass?" could not notice that the tests were written for the wrong
  requirement.

## Where to go next

- [Getting started](getting-started.md) — install, start the server, run
  your first plan.
- [Workflow](workflow.md) — the seven phases and the review machinery that
  backs three of them.
- [Execution](architecture/execution.md) — how tasks run concurrently
  without writing over each other.
- [Verification](architecture/verification.md) — the parallel verifier and
  the loop back into execution.
- [Running it](operations/running.md) — local-only scope, the request
  guard, and the UI's fetch contract.
- [Security](security.md) — the trust model and how to report a problem.

## Requirements

A Python 3.11 environment and a Claude Code CLI on `PATH`. The server is a
local tool: it runs as you, on your machine, and is not built for shared or
public hosting — see [Running it](operations/running.md) before you point it
anywhere but loopback.
