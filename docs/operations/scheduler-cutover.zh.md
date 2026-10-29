# 调度器切换

原生调度器就在 PDT 后端内部，调度由它自己做。它取代的是原本与它并排运行的那个
调度器 —— 一个由外部定时器唤醒的 tick，以及（在跑常驻形态的那些部署上）一个自带
只读页和控制 API 的小服务，监听 8004 端口。

**这套流程存在的全部理由是下面这条不变量：任何一个时刻，调度器只有一个。**
两个调度器就是同一批队列上的两个写入者，而运维发现第二个的时候，它们已经互相
不一致了。下面每一步的顺序都保证旧调度器*先*停，新调度器*后*起。

三步，按这个顺序：

1. 跑满 24 小时的 dry-run，拿旧调度器当初的行为当判据；
2. 在一个对比窗口内完成切换；
3. 把剩下的东西下线。

这里没有任何一条是给某台机器写的。每份部署都不同的标识 —— 旧调度器的 job id、
状态文件的位置 —— 都在需要的那一刻现场去查，绝不写死在文档里。

---

## 1. 开始之前

### 两个开关，以及它们什么时候被读

`scheduler.enabled` 和 `scheduler.dry_run` 位于 `backend/config.yaml` 的
`scheduler:` 段 —— 只在配置文件里，别处没有。没有端点、也没有面板控件能改它们：
面板动的是单个 plan，从来不动调度器本身。

两个开关都是在**后端启动时**被读的。已经跑着的 worker 不会因为文件变了就自己停下，
所以改文件和重启后端是一次操作的两半，第二半不是可选项。

| 配置 | 作用 |
| --- | --- |
| `enabled: false` | worker 线程根本不启动；不调度任何东西，也不写任何东西 |
| `dry_run: true` | 流水线照跑，并把它的判断写下来；不启动、不停止、不移动任何 plan |
| `enabled: true`、`dry_run: false` | 调度器在线 |

### 前置检查

从仓库根目录执行。每一个都是只读的。

```bash
# 1. 后端活着，而且能读自己的状态库。
curl -fsS http://127.0.0.1:8000/health

# 2. 调度器当前的配置，原样记下来。
grep -A 8 '^scheduler:' backend/config.yaml

# 3. 旧调度器的 tick（如果它还有的话）：找到唤醒被退役调度器的那一条，把它的
#    job id 记下来。这个 id 每份部署都不一样 —— 去查，别假设。
cc-connect cron list

# 4. 旧调度器的只读页。它活着的时候会应答。
#    探这个页面，不要探它 /api/ 下的路径：/api/ 下的每条路径都在 guard 头后面，
#    不带头的探针会拿到 403 —— 哪怕服务正在跑，`curl -f` 也会把活着的服务报成死的。
curl -fsS --max-time 2 http://127.0.0.1:8004/

# 5. 旧调度器的状态目录，趁它还在的时候看一眼。状态文件就在里面；这个目录可以
#    用 AC_SCHEDULER_STATE_DIR 指到别处。
ls -l "${AC_SCHEDULER_STATE_DIR:-$HOME/.ac-scheduler}"

# 6. supervisor 现在托管着什么。
curl -fsS http://127.0.0.1:8002/api/v1/subsidiaries
```

在下面几条都满足之前，不要往下走：

- 后端在 `/health` 上有应答；
- 你知道 cron 列表里哪一条是旧调度器的，以及是哪个服务（如果有的话）在监听 8004；
- supervisor 的托管清单已经记下来了 —— 下线之后要拿它做对照。

---

## 2. dry-run 与产物判定

### dry-run 是什么

dry-run 是流水线按自己的节拍跑着、读 plan 但一个都不改。它是这次切换的证据：
24 小时的 dry-run 就是 48 个半小时窗口，而半小时窗口正是旧调度器干活的单位。

按下面这样配好，然后重启后端 —— 开关是在启动时读的：

```yaml
# backend/config.yaml —— 只在 dry-run 期间这样配
scheduler:
  enabled: true
  dry_run: true
```

### 三份产物

一天的 dry-run 会在 dry-run 根目录下留下三个文件 —— 默认是
`/tmp/pdt-native-scheduler/dryrun`，也就是 `backend/plan_scheduler/dryrun.py` 里的
常量 `DEFAULT_DRYRUN_ROOT` —— 放在一个以日期命名的目录里：

| 文件 | 内容 |
| --- | --- |
| `ticks.jsonl` | 每个判断一行，四个字段：`ts`、`action`、`plan_id`、`reason` |
| `windows.json` | 每个半小时边界一条记录：正在跑的集合和队列快照 |
| `diff.md` | 这批窗口的报告，每条差异一行 |

### 判据

下面几条同时成立，才允许切换：

- 这一轮覆盖了 **48** 个窗口 —— 一天，正是旧调度器自己的节拍；
- 其中至少 **45** 个完全一致；
- 不一致的那几个窗口，差异**只在允许列表里**；
- **任何地方都没有**被禁的差异。

| 判定 | 差异种类 |
| --- | --- |
| 允许 | `transient_set_diff`、`workspace_order_diff` |
| 禁止 | `new_start`、`eviction_flip`、`queue_order_flip` |

没人认识的种类按禁止算。没人看过的差异，不等于不重要的差异。

### 怎么判

这个判定是可执行的，而且和发布门禁用的是同一个判定 —— 重放脚本直接调用
`plan_scheduler.dryrun` 里的判据，而不是另写一份：

```bash
DRYRUN_ROOT=/tmp/pdt-native-scheduler/dryrun
DAY=$(date -u +%Y%m%d)

ls -l "$DRYRUN_ROOT/$DAY"

python3 scripts/scheduler_dryrun_diff.py \
  --ticks   "$DRYRUN_ROOT/$DAY/ticks.jsonl" \
  --windows "$DRYRUN_ROOT/$DAY/windows.json" \
  --out     "$DRYRUN_ROOT/$DAY/diff.md"
echo "exit=$?"
```

| 退出码 | 含义 | 怎么办 |
| --- | --- | --- |
| 0 | 判据成立 —— 可以切换 | 去第 3 节 |
| 1 | 出现了被禁的差异、窗口不足 48 个、或一致的不足 45 个 | 不要切换。读 `diff.md`，找到不一样的那个窗口，修掉，再跑一轮 dry-run |
| 2 | 输入文件缺失、读不了或格式不对 —— 缺失的路径会打印出来 | 这一轮 dry-run 没产出该产的东西。检查它是不是在 `dry_run: true` 下跑的，以及这一天是不是完整 |

---

## 3. 单窗口切换

对比是在半小时窗口里做的，所以切换也在一个窗口里做：紧挨着一个窗口边界开始 ——
`:00` 或 `:30` —— 并在下一个半小时内结束。一个窗口被覆盖两次，就是有一段时间两个
调度器同时活着；一个窗口谁也没覆盖，就是有一段时间什么都没跑。

**旧调度器先停。** 不存在两个都活着的时刻。

```bash
# 1. 停掉旧调度器的 tick。job id 从前置检查里记下的 cron 列表里取 —— 那个 id
#    别处没有，而且每份部署都不一样。
cc-connect cron edit <old-scheduler-job-id> enabled false

# 2. 在任何东西启动之前，确认它确实停了。
cc-connect cron list
```

到这一步原生调度器才轮到上。改 `backend/config.yaml`：

```yaml
# backend/config.yaml
scheduler:
  enabled: true
  dry_run: false
```

```bash
# 3. 重启后端。两个开关是在启动时读的，所以重启是这次操作的一部分，不是它的
#    后续动作。从仓库根目录：  python3 -m backend.server
#    （这台部署上实际怎么跑后端，就用什么）

# 4. 验证：读模型有应答，队列还在 dry-run 留下的位置上。
curl -fsS -H 'X-PDT-Request: 1' http://127.0.0.1:8000/api/scheduler/state
```

应答就是那个读模型：`capacity`（总量和已用）、`queue_length`、`over_capacity`、
`queue`、`running` 和 `generated_at`。启动日志会说明发生的是哪一种：

```
[scheduler] started: capacity=4 dry_run=False tick_seconds=5.0
```

如果日志说的是 `disabled by the configuration; the worker thread is not
started`，说明开关没生效 —— 检查文件，再重启一次。

---

## 4. 回滚

### 回滚是一个设置加一次重启

```yaml
# backend/config.yaml
scheduler:
  enabled: false
```

```bash
# 重启后端：开关是在启动时读的。
# 从仓库根目录：  python3 -m backend.server
```

重启之后 worker 线程根本不会启动，什么都不写：队列不写、槽位不写、命令不写，
plan 的 phase 也不写。

### 回滚停的是写入，不是把历史撤回来

已经在那里的行原样保留。已经启动的 plan 继续跑 —— 回滚既不是停止，也不是暂停。
回滚把 plan 交还给运维，不会把它们重新排队。

### 这个开关不在面板上

面板不暴露这个开关。没有按钮，也没有能翻 `scheduler.enabled` 的端点：面板上的四个
动作每一个都只作用于一个 plan，而一个能停掉全系统所有 plan 的控件，离一次误点造成
的故障只差一步。这个开关只住在配置文件里，改文件是唯一的退路。

### 怎么验证回滚

```bash
# 读模型以空闲文档作答：没有容量被占用，没有排队，没有在跑。
curl -fsS -H 'X-PDT-Request: 1' http://127.0.0.1:8000/api/scheduler/state

# 启动日志说明 worker 没有被启动：
#   [scheduler] disabled by the configuration; the worker thread is not started
```

---

## 5. 下线核对清单

一行一条。每一条都带证明它的命令，以及"做完"长什么样。按清单往下走：只读一遍的
清单不是清单。

### 旧的只读页没了

```bash
curl -sS --max-time 2 http://127.0.0.1:8004/
echo "exit=$?"
```

做完的标志是连接被拒绝。旧的只读页什么都不应答。

### 旧的控制 API 没了

```bash
curl -sS --max-time 2 -X POST http://127.0.0.1:8004/api/control/promote
echo "exit=$?"
```

做完的标志是连接被拒绝。控制 API 是这个端口的另一半，页面没了而控制 API 还在应答，
只下线了一半。

### 旧端口上没有任何东西在监听

```bash
python3 -c 'import socket; s = socket.socket(); s.settimeout(2); print("open" if s.connect_ex(("127.0.0.1", 8004)) == 0 else "closed")'
```

做完的标志是它打印 `closed`。这个探针有超时：没有边界的探针不是把检查挂住，
而是替检查给出答案。

### 旧的状态文件没了

```bash
test ! -e "${AC_SCHEDULER_STATE_DIR:-$HOME/.ac-scheduler}/state.json"
echo "exit=$?"
```

做完的标志是文件不在了。退役调度器的状态属于退役调度器；一个看起来还活着、其实
已经死了的文件，正是下一个运维被误导的方式。

### supervisor 不再托管被退役的调度器

```bash
curl -fsS http://127.0.0.1:8002/api/v1/subsidiaries \
  | python3 -c 'import json, sys; print([row["name"] for row in json.load(sys.stdin)])'
```

做完的标志是列表里没有 `ac-scheduler` —— supervisor 登记被退役调度器时用的就是
这个名字。还在托管它的 supervisor 会在下一个 tick 把它重新拉起来，而那个 tick
就是不变量被打破的那一刻。

如果这个条目要保留，那它只能以只读飞书转发器的形态存在，而且只剩下这一件事：
没有 tick、没有 8004 端口、没有控制 API、没有只读页、没有旧状态文件 —— 只带
`X-PDT-Request: 1` 去读 PDT 的只读端点，除了进度卡片之外不发布任何东西。

列表里的其余条目不归这一条管。`provider-optimizer`、`provider-usage-forwarder`、
`perpetual-*` 和 `pdt-backend` 各自有各自的托管理由，这一条不要求把它们移除。
「被退役调度器的那一条」和「supervisor 的托管清单」是两件事；把两件事读成一件
的手册，会让运维把 PDT 后端跟旧调度器一起删掉。

### 旧的 cron 条目退场了

```bash
cc-connect cron list
```

做完的标志是列表里不再有被退役调度器的那一条。等切换验证通过之后再让它退场：

```bash
cc-connect cron del <old-scheduler-job-id>
```

这个 job 住在调度器工具自己的存储里，不在这个仓库里，所以删掉条目就是退场的全部。

### 原生调度器是剩下的唯一一个

```bash
curl -fsS -H 'X-PDT-Request: 1' http://127.0.0.1:8000/api/scheduler/state
```

做完的标志是读模型有应答，并且后端日志里只有一个调度器被启动。
