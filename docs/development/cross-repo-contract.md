# Cross-repo contract

Two repositories share one plan lifecycle. **PDT** — this repository — is
the provider: it owns the plan state machine, the executor and the
verification loop. The **scheduler** is the consumer: it decides *which*
plan runs next, and it does that by reading PDT's plan list and calling
PDT's lifecycle endpoints.

本文冻结这层关系里的四件事：**1. 提供方（PDT）这一侧**、**2. 消费方（调度器）
这一侧**、**3. 测试基线**与**4. 合入顺序**。这里的每一条，要么是 PDT 对另一个仓库里
的代码作出的承诺，要么是 PDT 对那个仓库里的代码提出的约束 —— 它不能靠某次提交的口头
约定维持：写在这里，并由静态门禁
`backend/tests/static_gates/test_cross_repo_contract_documented.py`（第 1 节）、
`backend/tests/static_gates/test_scheduler_contract_sections.py`（第 2 节）与
`backend/tests/static_gates/test_contract_test_baseline.py`（第 3、4 节）盯着，写法
与代码一旦漂移，门禁就红。

Every promise below is a promise PDT makes to code that lives in another
repository, or a constraint PDT places on that code, so it cannot be
renegotiated in a commit message on either side: it is written down here,
and the static gates
`backend/tests/static_gates/test_cross_repo_contract_documented.py` (section 1),
`backend/tests/static_gates/test_scheduler_contract_sections.py` (section 2)
and `backend/tests/static_gates/test_contract_test_baseline.py`
(sections 3 and 4) fail the build when the writing drifts away from the code.

The section titles are pinned verbatim — the consumer's own gate asserts
on the same strings — so they read the same in every locale. Where the
prose and a pinned title disagree, the title is the contract.

## 1. 提供方（PDT）契约

Provider contract, PDT side: the endpoints the scheduler may call, the
vocabulary those endpoints speak, and the answers they give when they
refuse.

本节是**提供方侧的契约**：调度器可以调哪些端点、这些端点说什么词、以及它们拒绝时
怎么回答。

### 1.1 端点表

下面这四个端点是这份契约覆盖的全部：调度器需要的能力都在这里，别的端点不归这份
契约管。`{plan_id}` 是计划标识；正文里有时写作 `{id}` —— 同一个占位符的短名。

| 端点 | 方法 | 请求体 | 成功响应 |
| --- | --- | --- | --- |
| `/api/plan/{plan_id}/lifecycle/start` | POST | `StartPlanRequest`（五个字段全可选） | `LifecycleResult`（六字段） |
| `/api/plan/{plan_id}/lifecycle/stop` | POST | `StopPlanRequest`（`requeue` 默认 `false`） | `LifecycleResult`（六字段） |
| `/api/plan/{plan_id}/state-from-db` | GET | 无 | `plan_id` / `current_phase` / `completed_phases` |
| `/api/plans` | GET | 无（可选 `?include_terminal=true`） | 计划条目数组 |

The same four endpoints, in the shape both sides write about:

- `POST /api/plan/{id}/lifecycle/start`
- `POST /api/plan/{id}/lifecycle/stop`
- `GET /api/plan/{id}/state-from-db`
- `GET /api/plans`

请求体的全部字段：

- `StartPlanRequest` —— `project_dir`、`tool`、`max_rounds`、
  `sync_targets`（`feishu` / `telegram` 的列表）、`force`。五个字段**全部可选**；
  唯一必须给 `project_dir` 的场景是**全新执行**（没有检查点可恢复时）。
- `StopPlanRequest` —— `requeue`，布尔值，默认 `false`。`true` 的意思是
  「停完之后把它放回等待相位」。

`GET /api/plans` 每条计划一个条目；调度器用得上的键是 `id`、`status`、
`current_phase`、`current_phase_label`、`created_at`、`requirement`、
`flags`、`steps`、`pid`、`archived`。归档条目（`archived` 为 `true`）不带
`current_phase` / `current_phase_label` —— 归档计划没有活跃相位。

`GET /api/plan/{id}/state-from-db` 直接读状态机那张 routing 表：成功响应体是
`plan_id`、`current_phase`（可能为 `null`）与 `completed_phases`（列表，缺省
`[]`）。它不读磁盘上的计划目录 —— 要当前相位就读它，别去猜别的端点。

### 1.2 等待相位口径

- **唯一的等待相位是 `queued`。**「这件事在等人」只有一个名字。
  The only waiting phase is `queued` — one name for "this plan is waiting
  for somebody".
- **`ready` 只是输入别名。** 读路径把 `ready` 归一化成 `queued`；消费方不必再发
  `ready`，也不该把它当成一个自己的取值。 `ready` is an input alias, not a
  second waiting phase.
- **每次读到 `ready` 都记一条弃用日志**（warning，带 `plan_id` 与读取来源）。
  旧数据要清得掉，先得知道它在哪儿、被谁读到。 Every read that sees the
  legacy word leaves a deprecation log.
- **`GET /api/plans` 的输出不含 `ready`。** 列表在读路径上就归一化了，所以它报出来
  的相位是 `queued`。 The plan listing never emits the legacy spelling.
- **写路径不再产生 `ready`。** 生产代码里没有任何一处把 `ready` 写进相位字段
  （有静态门禁盯着）。 Nothing writes `ready` any more.

### 1.3 LifecycleResult 的六个字段

两个生命周期端点的成功响应体就是 `LifecycleResult` 的字段 —— 多一个少一个都是
契约变更。

| 字段 | 含义 |
| --- | --- |
| `plan_id` | 被操作的计划。 |
| `dispatched` | 实际派给了谁：`verification` 或 `execution`；没有在跑的东西可派时是 `null`。 |
| `berth_phase` | 这次操作之后计划停泊在哪儿。 |
| `final_phase` | 调用方最终会看到的相位。 |
| `checkpoint` | 恢复点：只回显、不消费；没有检查点时是 `null`。 |
| `detail` | 这次分派做的具体动作。 |

### 1.4 错误码

| 状态码 | 何时 | 响应体 |
| --- | --- | --- |
| `404` | 生命周期门面：计划不存在 | `{"error": "Plan not found"}` |
| `404` | `state-from-db`：routing 表里没有这一行 | `{"error": "plan not found in routing table", "error_code": "PLAN_NOT_FOUND"}` |
| `409` | 冲突 —— 计划正在跑，或相位前提不成立 | `{"error": "conflict", "reason": ...}`（处理器用错误响应回答时）；处理器直接抛异常时是 `{"detail": ...}` |
| `400` | 请求体不可用 —— 例如全新执行缺 `project_dir` | `{"detail": ...}` |
| `503` | `state-from-db`：状态库文件不在磁盘上 | `{"error": "state db unavailable", "error_code": "STATE_DB_MISSING"}` |
| `500` | `state-from-db`：读库时 sqlite 报错 | `{"error": "state db read failed", "error_code": "SQLITE_ERROR"}` |

门面**不自己编错误**：处理器抛什么就原样透传什么。409 不会被包成 500 ——
「这个计划正在跑，停不了」和「服务器坏了」是两件不同的事，调用方要能分开处理。

An error body is the handler's own: a dict body that carries an `error`
key is passed through unchanged, anything else arrives as
`{"detail": ...}`. The facade does not re-label them.

The facade does not invent errors: whatever the handler answers with is
what the caller gets. A 409 stays a 409.

### 1.5 请求守卫

四个端点都在 `/api/` 底下，所以每个请求都要带 `X-PDT-Request: 1`；不带的话，
在进处理器之前就被 403 拒掉。

All four endpoints live under `/api/`, so every request carries the
header `X-PDT-Request: 1`; a request without it is refused with 403
before it reaches a handler.

## 2. 消费方（调度器）契约

消费方侧的契约：调度器**怎么读** PDT 的状态、**什么时候**可以动手、**动手时**把
决定权交给谁。这三条约束的是另一个仓库里的代码；本仓只写描述，不写实现。

本节与第 1 节互为两半：第 1 节说 PDT 承诺什么，第 2 节说调度器据此必须怎么跑。
两节各有一道门禁盯着，写法与代码一旦漂移，门禁就红。

### 2.1 活跃相位白名单

- 活跃相位白名单：queued / executing / verification*
  —— `verification*` 指所有以 `verification` 开头的验证相位。调度器只对白名单里的
  相位记账；白名单之外的相位由 PDT 自己推进，调度器不碰。
- **唯一的可调度相位是 `queued`。** 调度器把 `queued` 当收件箱：只有停在 `queued`
  的计划才允许被自动启动。 The only schedulable phase is `queued`.
- **调度器从不读 `ready`。** `ready` 是历史别名（见 1.2）：调度器不把它当成第二个
  等待相位，不按它排程，也不把它写回去。 The scheduler never reads `ready`.
- 「现在该停执行还是停验证」不是调度器能判断的事 —— 见 2.2。

### 2.2 tick 管线：固定顺序、最小运行时长与「停谁」

每一轮 tick 的顺序是固定的，三步，一步都不能换：

1. **对账** —— 用 `GET /api/plan/{id}/state-from-db` 把 PDT 的 routing 表与调度器
   自己的持久化状态对齐：谁在跑、跑到了哪一步。
2. **收敛** —— 把对账发现的偏差收敛掉，方向只有一个：向 routing 表看齐（见 2.3）。
3. **补位** —— 收敛之后，再把 `queued` 里的计划按 FIFO 补进空出来的名额。

- **最小运行时长保护**：一个执行/验证轮次跑过最小运行时长之前，调度器**不停**它，
  也不因为它「看起来慢」而抢先收敛 —— 反复起停比多等一会儿更贵。阈值由消费方自己
  配置，本仓不定这个数字。 A young run is not stopped.
- **「停谁」由统一门面决定，调度器不自己判断。** 调度器只说「停这个计划」，调
  `POST /api/plan/{id}/lifecycle/stop`：执行轮次在跑就停执行、验证轮次在跑就停验证。
  这是**进程内**的实时判断，进程外复制不了。调度器自己判「现在跑的是执行还是验证」，
  就会与 PDT 的实时状态分叉 —— 所以它不判。

### 2.3 adoption 与外部越权收敛：routing 表是唯一权威

- **routing 表是唯一权威。** `plan_routing` 就是 `GET /api/plan/{id}/state-from-db`
  读的那张表；它是当前相位的唯一真相。调度器的本地状态是缓存，不是真相。
- **adoption（启动接管）**：调度器启动或重启时，把本地持久化的队列与执行集合
  **adopt** 到 routing 表上 —— 逐条对齐，以 routing 表为准。
- **外部越权收敛**：有人绕过调度器直接调 PDT 的端点改了状态时，调度器在下一个
  tick 的**对账**步发现偏差，在**收敛**步里向 routing 表看齐。调度器不与外部改动
  对冲，也不拿自己的旧判断当真相。

### 2.4 本节是契约描述，PDT 不实现 tick

- arch 决策点 4（tick 管线：对账、收敛、补位的固定顺序与最小运行时长保护）在
  **本仓只作契约描述**：这里写的是消费方**必须**怎么跑。运行实现与运行测试由
  **消费方仓**承担，**PDT 不新增 tick 代码**。
- 所以本节没有 PDT 侧的运行测试。PDT 侧的门禁只钉住本节的文字：标题固定、
  关键词固定、tick 顺序写死 ——
  `backend/tests/static_gates/test_scheduler_contract_sections.py`。

## 3. 测试基线

契约写得再细，没有测试钉住就只是文档。本节列出这份契约的**测试基线**：有哪些用例、
每个用例断言什么、谁来写、在哪断言。

先说清一件事：**下面三类用例的运行实现全部在消费方仓**，本仓不新增消费方的运行代码。
每一类都必须由消费方实现并在其 CI 断言 —— 本节只把「必须断言什么」写死，不替消费方
承诺它已经做到了。

The runtime implementation of the three case groups below belongs to the
consumer's repository: each of them must be implemented by the consumer
and asserted in its CI. This section pins *what* has to be asserted, not
that somebody already did it.

### 3.1 消费方 `PDTClient` 边界测试

`PDTClient` 是消费方访问 PDT 的唯一入口。三组边界用例：

- **相位白名单** —— `PDTClient` 只把 `queued` 当作可调度相位（见 2.1）；活跃相位白名单
  是 queued / executing / verification*；`ready` 不是它的取值（读路径已归一化，见 1.2），
  它也不把 `ready` 写回去。
- **门面调用** —— 启动与停止一律走统一门面：`POST /api/plan/{id}/lifecycle/start` 与
  `POST /api/plan/{id}/lifecycle/stop`。`PDTClient` 不得直接调相位推进类端点，也不得
  自己判断「停执行还是停验证」—— 那件事由门面在进程内对实时状态决定（见 2.2）。
- **错误码映射** —— `PDTClient` 把 PDT 的状态码映射成消费方自己的异常类型：404
  （计划不存在）、409（冲突：正在跑，或相位前提不成立）、400（请求体不可用）、503
  （状态库文件不在磁盘上）、500（读库失败）。**409 必须映射成「冲突」，不得被当成
  500** ——「现在停不了」和「服务器坏了」在消费方侧要能分开处理。

### 3.2 并发收敛用例

- **并发 start/stop** —— 两个并发请求对同一个计划做 start 或 stop，只能有一个成功，
  另一个拿到 409。
- **对账与收敛** —— 调度器本地状态与 routing 表不一致时，下一个 tick 的对账步发现
  偏差、收敛步向 routing 表看齐（见 2.2 与 2.3）。
- **adoption** —— 调度器重启后把本地队列与执行集合 adopt 到 routing 表上，以 routing
  表为准。

### 3.3 E2E 主流程与故障注入用例

主流程跑在**真实常驻调度器 + 真实 PDT 后端**上（不用替身）：

1. 计划入队，停在 `queued`；
2. 调度器发现 `queued`，调 `POST /api/plan/{id}/lifecycle/start`；
3. 计划推进到验证相位；
4. 调度器调 `POST /api/plan/{id}/lifecycle/stop`（带 `requeue`）；
5. 计划回到 `queued`，被再次调度。

故障注入至少覆盖：404（计划不存在）、409（冲突）、500（读库失败）、503（状态库文件
缺失）、调度器重启（adoption），以及绕过调度器直接改状态（外部越权收敛）。

### 3.4 本节是基线描述，PDT 不实现这些用例

与 2.4 同理：PDT 侧的运行测试覆盖提供方自己（既有端点与统一门面的等价性、门禁自身
的静态断言），消费方侧的运行测试住在消费方仓。上面三类用例必须由消费方实现并在其 CI
断言；本仓只钉住它们的文字：标题固定、关键词固定 —— 门禁是
`backend/tests/static_gates/test_contract_test_baseline.py`。

## 4. 合入顺序

两仓的合并顺序是固定的，而且必须是可判定的。

### 4.1 PDT 先行

**PDT 先行。** 跨仓改动的合入顺序只有一种：先合 PDT，再合调度器。调度器只消费**已经
合入**的语义 —— 它不能对着还没合入的 PDT 分支开工，也不能把「等 PDT 合了再说」当成
计划的一部分。

理由：调度器的每一条行为都对着 PDT 的端点、字段与相位词。PDT 改了这些而调度器先合，
两仓就在一段时间里各说各话，而这段时间里没有任何一道门禁会红。

PDT goes first. The scheduler consumes only what has already been merged.

### 4.2 调度器 PR 的 CI 必须跑契约测试 job

**调度器 PR 的 CI 必须跑契约测试 job。** 这个 job 断言的正是第 3 节的基线：

- `PDTClient` 边界测试（相位白名单、门面调用、错误码映射）；
- 并发收敛用例；
- E2E 主流程与故障注入用例。

契约测试 job 必须由消费方实现并在其 CI 断言 —— 缺了它，调度器的 PR 就只是「看起来
兼容」。

The scheduler's pull request has to run a contract-test job in its CI;
that job must be implemented by the consumer and asserted in its CI.

### 4.3 门禁在两仓各管一段

- **PDT 侧**：本仓的静态门禁盯住本文四节的文字与代码是否一致 ——
  `backend/tests/static_gates/test_cross_repo_contract_documented.py`（第 1 节）、
  `backend/tests/static_gates/test_scheduler_contract_sections.py`（第 2 节）、
  `backend/tests/static_gates/test_contract_test_baseline.py`（第 3、4 节）。
- **调度器侧**：契约测试 job 盯住消费方的行为是否与本文一致。
- **合入规则**：调度器 PR 只有在契约测试 job 绿了之后才允许合入；PDT PR 只有在本仓门禁
  绿了之后才允许合入。任一侧红了，另一侧不许合入。
