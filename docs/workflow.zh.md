# 工作流

七个阶段，从一行需求到经过验证的交付。其中三个在动手之前就可评审，两个并发运行。

```
requirement
    │
    ▼
[1] Requirement clarification ──► interview.json
    │                              (5 dimensions: background, goals,
    │                               scope, constraints, acceptance)
    ▼
[2] PRD generation ─────────────► prd.json (CPEA decision points)
    │        ▲
    │        └── review / revise loop until every point is accepted
    ▼
[3] Architecture design (opt.) ─► arch-design.md     ┐
    │        ▲                                        │ same
    │        └── review / revise loop                 │ review
    ▼                                                  │ machinery
[4] Test design (opt.) ─────────► test-design.md      ┘
    │
    ▼
[5] Task generation ────────────► tasks.json
    │
    ▼
[6] Autonomous execution ───────► code + execution.log
    │   ⚡ CONCURRENT — dependency layers, then conflict-free micro
    │      layers inside each layer (file-level conflict graph),
    │      bounded by provider slots; runtime file locks as backstop
    │   ▶ runtime state lives in state.db (SQLite), not in the artifacts
    ▼
[7] Verification ───────────────► verification_report.json
    │   ⚡ CONCURRENT — VPs partitioned by method; groups and the VPs
    │      inside them both run in parallel, nested under one shared
    │      plan-wide ceiling
    │
    ├─ ✗ FAILED ──► repair tasks written to state.db ──► back to [6]
    │               (the executor reconciles them into the DAG from
    │                there; tasks.json is never rewritten)
    │               (bounded: stops on pass, round budget exhausted,
    │                the same failure set repeating, or operator stop)
    │
    └─ ✓ PASSED ──► done
```

## 各阶段

| # | 阶段 | 产出 | 可选 |
|---|---|---|---|
| 1 | 需求澄清 | `interview.json` | 否 |
| 2 | PRD 生成 | `prd.json` | 否 |
| 3 | 架构设计 | `arch-design.md` | 是 |
| 4 | 测试设计 | `test-design.md` | 是 |
| 5 | 任务生成 | `tasks.json` | 否 |
| 6 | 自主执行 | 代码、`execution.log` | 否 |
| 7 | 验证 | `verification_report.json` | 否 |

各阶段**互相承重**。验证是拿运行中的代码去比前面阶段产出的文档 —— 所以改了 PRD
生成却没有配套的验收测试，会**通过 `pytest` 而依然是错的**。这在实践里意味着什么，
见[贡献](development/contributing.md)。

## CPEA —— 一个决策点长什么样

PRD、架构文档、测试设计里的每一个决策点都是一条 **CPEA** 记录：

| 元素 | 回答什么 |
|---|---|
| **C**ontext 背景 | 这个决定是在什么情况下做的？ |
| **P**roblem 问题 | 具体要决定的是什么？ |
| **E**valuation 评估 | 有哪些选项，各自的证据是什么？ |
| **A**ction 行动 | 选了哪个？ |

这个形状的意义在于：评审者看到的是**选择背后的证据**，而不只是选择本身。
"我们选了 X" 不可评审；"我们选 X，因为 Y 和 Z，而另一条路要付出 W" 才可评审。

## 评审循环

PRD、架构文档、测试设计走的是同一套机制。每个决策点可以：

- **接受** —— 该点通过；
- **拒绝** —— 附理由，触发**只针对该点的定向重写**。已接受的点会被保留；精化器
  没有机会重新翻你已经批准过的东西。
- **追问** —— 系统作答，然后你再决定；
- **跳过** —— 先搁置，之后再回来看。

只有当每个决策点都被接受或跳过，一份文档才算通过。可选阶段走完之后，系统会**明确
询问**你是要生成架构和测试设计，还是直接进入任务生成 —— 那是一个有真实答案的真
问题，不是走过场。

## 两处并发发生的地方

评审之外的那两半各有自己的页面，因为有意思的工程都在那里：

- **[执行](architecture/execution.md)** —— 依赖层、文件级冲突图、provider 槽位，
  以及运行时文件锁。
- **[验证](architecture/verification.md)** —— 按方法分组的并行验证，以及那条把失败
  变回修复任务、喂回执行的有界循环。

## 产物落在哪里

上面所有东西都在 plan 目录（`plans/{plan_id}/`）下，外加执行器写进你指定的项目
目录的内容。验证日志**按轮次分文件**
（`logs/verification_{round}_{timestamp}.log`），所以长跑的计划可以一轮一轮地读，
而不是读一个不断变大的文件。
