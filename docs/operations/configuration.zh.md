# 配置

## `backend/config.yaml` 刻意很薄

三点值得知道：

- **什么都不配也能跑。** 它不带 provider order 文件、也不带子公司进程。服务照常启动、
  工作流照常运行；依赖外部组件的功能只是没东西可读而已。
- **`provider_order_file`** 指向一份描述 provider 回退链的 JSON 契约。没有内置链 ——
  想要 provider failover 就把它指向任何产出该文件的东西。环境变量
  `PROVIDER_ORDER_FILE` 覆盖配置值。
- **`subsidiary_processes`** 是空列表。它是一个通用机制，用于在 FastAPI lifespan
  期间拉起长期运行的辅助进程，有两条承重性质：条目**按顺序**启动；启动失败会
  **中止整个启动**，除非该条目标了 `optional: true`。

## `example/` 与 `.config/`

操作者专属配置 —— provider 并发上限与 provider 路由 —— 以**提交进仓的模板**形式放在
`example/`，你把它拷进仓库根下已 gitignore 的 `.config/` 来定制：

```bash
mkdir -p .config
cp example/provider_capacity.yaml.example .config/provider_capacity.yaml
cp example/provider_routing.yaml.example  .config/provider_routing.yaml
# 编辑这两份副本 —— 把占位 pattern 换成你自己的 provider 名，然后保存
```

`.config/` 是**刻意** gitignore 的：把一张真实名字的表写进源码，等于把**一个**部署的
provider 集合编译进**每一次**安装。这里刻意**没有**任何"带真实名字的示例" —— 每一次
安装都从占位模板开始，自己加行。

两个文件也都可以用环境变量覆盖，所以把配置放在别处的部署不需要往仓库根丢文件：

| 环境变量 | 覆盖 |
|---|---|
| `PDT_PROVIDER_CAPACITY_FILE` | capacity YAML 的路径 |
| `PDT_PROVIDER_ROUTING_FILE` | routing YAML 的路径 |

!!! warning "不要在模板上点 provider 的名"
    如果一次贡献引入了新的外部 provider，**不要**去改 example 文件给它命名。一串
    真实名字属于部署机器上的 `PDT_PROVIDER_*` 配置 —— 写进 `example/` 等于把一个
    安装的 provider 列表编译进每一次安装的 onboarding。schema 接受哪些占位符，
    读每个 example 文件的头部。

## 验证的边界

`backend/configs/verification.yaml` 放的是验证循环的各项限制：

| 键 | 约束什么 |
|---|---|
| 轮次预算 | 可以跑多少轮 验证 → 修复 → 再验证 |
| 每个 VP 的超时 | 单个验证点可以跑多久 |
| `parallelism_cap` | **单个方法组**自己的扇出 |

`parallelism_cap` **不是**舰队上限。组是并发的，所以每组的上限会相乘；真正的限制是
每轮创建一次、由所有组共享的 `plan_semaphore`。见
[验证](../architecture/verification.md)。

## 凭据

凭据来自 `backend/.env`，它是 gitignored 的。`backend/.env.ci` 放的是提交进仓的
**占位值** —— 它是测试夹具，真密钥永远不要放进去。

## 运行态

一个 checkout **产出**的（而不是**分发**的）所有东西，都放在一个 gitignored 的
`<repo>/.pdt/` 目录下：状态库、它的 WAL 兄弟、服务 boot 计数器，以及那个轮转的
关闭备份。

把它们放在一起是刻意的。它们是**一个整体** —— boot 计数器和备份目录的路径都是从
数据库的父目录推出来的 —— 所以搬数据库就会搬走全部四个，而一个 checkout 产出的
东西也不会跟它分发的东西混在一起。

!!! note "搬动状态库"
    WAL 模式下的 SQLite 会把已提交事务留在 `-wal` 里，直到发生 checkpoint。
    请先**优雅地**停服（让 SQLite 在关闭时 checkpoint），确认 `-wal` 已归零，
    然后再搬 —— 只复制主库、不带 WAL，会丢掉里面全部内容。这个路径只在
    `backend/config_paths.py` 里声明一次；不要在别处从 `__file__` 重新推导它 ——
    这个项目已经犯过两次，并为此立了门禁
    （`backend/tests/static_gates/test_state_db_path_has_one_resolver.py`）。
