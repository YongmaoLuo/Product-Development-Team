# 跨仓契约

本文是这份契约的中文页。英文版是 `cross-repo-contract.md`；两份说的是同一份契约 ——
章节标题、端点表、字段名、相位词与状态码两边逐字一致，因为它们正是两个仓库各自断言
的东西。

两个仓库共用一套计划生命周期。**PDT**（本仓）是提供方：状态机、执行器、验证回路
都归它。**调度器**是消费方：它决定下一个跑哪个计划，做法是读 PDT 的计划列表、
调 PDT 的生命周期端点。

下面每一条承诺，都是 PDT 对另一个仓库里的代码作出的承诺 —— 它不能靠某次提交的
口头约定维持：写在这里，并由静态门禁
`backend/tests/static_gates/test_cross_repo_contract_documented.py` 盯着，写法与代码
一旦漂移，门禁就红。

章节标题与端点表逐字钉死 —— 消费方自己的门禁断言的是同一批字符串 —— 所以它们在
任何语言下读起来都一样。散文与钉死的契约件冲突时，以契约件为准。

## 1. 提供方（PDT）契约

提供方侧的契约：调度器可以调哪些端点、这些端点说什么词、以及它们拒绝时怎么回答。

### 1.1 端点表

下面这四个端点是这份契约覆盖的全部：调度器需要的能力都在这里，别的端点不归这份
契约管。`{plan_id}` 是计划标识；正文里有时写作 `{id}` —— 同一个占位符的短名。

下表的表头逐字钉死，所以在任何语言下都原样印出：

| 端点 | 方法 | 请求体 | 成功响应 |
| --- | --- | --- | --- |
| `/api/plan/{plan_id}/lifecycle/start` | POST | `StartPlanRequest`（五个字段全可选） | `LifecycleResult`（六字段） |
| `/api/plan/{plan_id}/lifecycle/stop` | POST | `StopPlanRequest`（`requeue` 默认 `false`） | `LifecycleResult`（六字段） |
| `/api/plan/{plan_id}/state-from-db` | GET | 无 | `plan_id` / `current_phase` / `completed_phases` |
| `/api/plans` | GET | 无（可选 `?include_terminal=true`） | 计划条目数组 |

同样这四个端点，按两边都这么写的形状：

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
- **`ready` 只是输入别名。** 读路径把 `ready` 归一化成 `queued`；消费方不必再发
  `ready`，也不该把它当成一个自己的取值。
- **每次读到 `ready` 都记一条弃用日志**（warning，带 `plan_id` 与读取来源）。
  旧数据要清得掉，先得知道它在哪儿、被谁读到。
- **`GET /api/plans` 的输出不含 `ready`。** 列表在读路径上就归一化了，所以它报出来
  的相位是 `queued`。
- **写路径不再产生 `ready`。** 生产代码里没有任何一处把 `ready` 写进相位字段
  （有静态门禁盯着）。

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

错误体是处理器自己的：带 `error` 键的字典体原样透传，其余的一律以
`{"detail": ...}` 到达。门面不改写它们。

### 1.5 请求守卫

四个端点都在 `/api/` 底下，所以每个请求都要带 `X-PDT-Request: 1`；不带的话，
在进处理器之前就被 403 拒掉。