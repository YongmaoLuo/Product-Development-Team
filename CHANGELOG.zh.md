# 变更日志

本项目所有值得注意的变更，都记录在这里。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

[English](CHANGELOG.md)

## [0.1.2] - 2026-10-07

**一轮运行能碰什么，一份绿灯报告证明了什么。** 这一版新增三项能力：通知密钥可以
存在 macOS 钥匙串里、子 agent 可以运行在操作系统沙箱里、验证阶段不能再对着一条
没有任何验证点的验收标准说通过。其余条目，是你在计划运行过程中看得见的东西的修复，
以及仓库自己的检查 —— 它们现在真的能拦住一次合并，而不只是报告。

### 新增

- **通知密钥可以不经过环境变量，改由 macOS 钥匙串供给。** 飞书 app secret 与
  Telegram bot token 可以存在本项目自己的钥匙串里（不是 login keychain —— 那里
  存着这个账号历来保存过的所有凭据），再由需要它的进程通过一个文件描述符拿到：
  值不进任何环境变量，`ps eew` 里看到的也只是描述符编号。默认关闭、仅限 macOS：
  什么都不改的部署照旧从 `backend/.env` 读；Linux 上钥匙串根本不会被查询。开启之后
  不做回退 —— 条目缺失就报缺失，不会悄悄降级去读明文变量，因为「降级成一个同机
  任何进程都能读到的值」比明确失败更糟。用
  `backend/.venv/bin/python3 -m backend.cli secrets verify` 看每条密钥实际来自
  哪里；在它给出 `source=keychain` 之前，迁移还不算完成。见
  [钥匙串迁移](docs/operations/keychain-migration.md)。
  （[#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34)）

- **子 agent 可以运行在操作系统沙箱里。** 子 agent 以关掉确认提示的方式启动 ——
  而那正是文件系统最需要一道「不是提示的边界」的时刻：把 `PDT_SANDBOX_PROFILE`
  指向一份 Seatbelt profile（模板见 `example/sandbox_profile.sb.example`），
  每个子 agent 就落在它里面跑。什么都不配，行为与从前完全一致；配了却装不上则
  **停止**，不会退回去无沙箱启动 —— 会自己悄悄关掉的沙箱比没有沙箱更糟，因为配置
  仍然声称它是开着的。见[配置](docs/operations/configuration.md)。
  （[#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34)）

- **验证不能再放过一条没有任何验证点的验收标准。** 在这之前，一条验收标准可能在
  测试设计阶段被整条漏掉而无人察觉：没有验证点去判它，报告照旧是 PASSED，这个遗漏
  从外面完全看不见。现在验证计划会对着 PRD 的验收标准做核对：有标准没有被任何
  验证点声明覆盖，就把规划送回去补上对应验证点；重试之后仍然缺的，记在计划本身上，
  而不是消失在一份报告里。匹配刻意保持保守 —— 只认带显式【标签】的标准，没有标签的
  句子跳过不猜：对一个本来健康的计划误报，比漏报更贵。
  （[#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34)）

### 变更

- **合并由「跑过的检查」拦住，不再由「被跳过的检查」放行。** GitHub 把被跳过的
  检查算作通过，所以把测试**下游**的检查设成必须通过，从来没有真正覆盖到测试；
  现在有一道检查会读取这个 PR 上每一个检查的结果，只要有一个不是成功就判失败。
  仓库的静态契约检查拆成单独一步、排在全部测试之前：一条坏掉的契约只烧一台机器，
  而不是二十台。守护合并的那道检查也不再出现在手动运行里 —— 在那里它按构造就是
  红的。`main` 也不再于合并之后重跑整套：PR 已经跑过，而且跑的就是将要落地的那棵树。
  （[#23](https://github.com/YongmaoLuo/Product-Development-Team/pull/23)、
  [#24](https://github.com/YongmaoLuo/Product-Development-Team/pull/24)、
  [#30](https://github.com/YongmaoLuo/Product-Development-Team/pull/30)、
  [#31](https://github.com/YongmaoLuo/Product-Development-Team/pull/31)）

- **隐私与密钥扫描现在覆盖历史，以及合并往历史里复制的东西。** 只读工作区的检查
  看不见「某次提交加了一个私有标识、后来某次提交把它删掉」这种形状：树是干净的，
  而那次提交仍然可以按 SHA 取到。扫描现在走遍一个区间里的每一次提交，并且覆盖
  GitHub 原样复制进 `main` 历史的那三个字段 —— 分支名、PR 标题、PR 正文。另有一个
  pre-push hook（`scripts/install_git_hooks.sh`）：让失败发生在推送之前，那时修它
  是改写，而不是一起历史事故。
  （[#2](https://github.com/YongmaoLuo/Product-Development-Team/pull/2)、
  [#30](https://github.com/YongmaoLuo/Product-Development-Team/pull/30)）

- **安装后端改为走一份钉死的依赖集合。** `uv sync --project backend` 取代原来的
  pip 步骤；`backend/pyproject.toml` 与 `backend/uv.lock` 钉住整棵传递依赖树 ——
  一份全新的 clone 装到的就是 CI 测过的那套版本。CI 用 `--locked` 安装：清单与锁
  一旦对不上就直接失败，而不是当天重新解析出别的版本。Python 3.11 也从「假设」
  变成结构性要求（`>=3.11,<3.12`）：uv 拒绝在这棵树从未测过的解释器上建环境。
  （[#35](https://github.com/YongmaoLuo/Product-Development-Team/pull/35)、
  [#36](https://github.com/YongmaoLuo/Product-Development-Team/pull/36)）

- **带 provider 密钥的私有文件有了固定住处。** 子 agent 的 settings 文件里是
  路由到的那家 provider 的 key；它一直是私有写入、进程退出后脱敏，但落在系统临时
  目录里 —— 每台机器路径不同，而且没有任何东西会回收它。现在统一落在
  `~/.pdt-scratch`：每个用户一个可预期的位置，私有权限，每一次派发留下的目录在
  30 天后被移除 —— 那是留给「脱敏后的副本还能回看」的时间窗。
  （[#11](https://github.com/YongmaoLuo/Product-Development-Team/pull/11)）

### 修复

- **计划进度卡片在任何时刻都说得清「现在在跑什么」。** 飞书卡片（以及伴随它的
  Telegram 消息）曾经会沉默 —— 验证轮在规划或判定时只剩一句没有名字的「验证中」；
  会长时间冻结在旧正文上；会在修复轮之后让表头与正文互相打架；会把时长按错误的
  时区算出来，于是一个十分钟的窗口显示成八小时；Telegram 还会为没变过的状态新开
  一条消息。现在，任务或验证点自己说不出名字时，当前活动从执行日志里推导；表头与
  正文对着同一份来源对账；内容没变的卡片是原地更新。
  （[#33](https://github.com/YongmaoLuo/Product-Development-Team/pull/33)）

- **被拆分的任务，不再作为一条空记录回来。** 执行器把一个任务拆成子任务之后，
  一次运行时的记账写入可能把已删除的父节点重新造出来 —— 没有状态、没有标题 ——
  调度器随即停在 `No schedulable micro-layer found`，而真实的子任务全部留在
  pending。现在，把已删除任务挡在外面的守卫覆盖每一条写入路径；被跳过的写入会记
  一条日志，而不是悄悄丢掉。
  （[#29](https://github.com/YongmaoLuo/Product-Development-Team/pull/29)）

- **任务不再因为「不是它自己的原因」而失败。** 子 agent 报告里的一段散文可能被当成
  文件路径写到磁盘（一次运行因此死在 `File name too long`）；捕获到的目标不是可用的
  项目内路径时，现在按散文跳过，而不是当作写入指令。一个自己声明的测试命令要求
  「工作树干净」的任务（恢复 / 回滚类任务，空 diff 正是它的成功条件）会被空 diff
  门禁判失败 —— 门禁拒绝的恰好是它被要求做出的结果；这类任务现在被识别。而一个被
  执行器拆分出子节点的任务，可能作为 failed 的父节点留在已经全部完成的子节点旁边；
  拆分不再保留父节点。
  （[#34](https://github.com/YongmaoLuo/Product-Development-Team/pull/34)）

- **停止一个服务，不再被报成失败；启动一个服务，只认自己拉起的那个进程。**
  「端口上有没有东西」这个问题，以前问的是哪个进程*提到*了这个端口，而不是哪个
  进程在*监听*它 —— 一条已经关闭的客户端连接足以让空端口看起来被占着，于是一次
  成功的停止被报成失败；启动也只要端口上有人应答就记为 started，哪怕应答的根本
  不是本工具拉起的进程。现在只认监听者，而且只有监听者就是被拉起的那个进程，
  启动才算数。
  （[#21](https://github.com/YongmaoLuo/Product-Development-Team/pull/21)）

- **一次清理，不会再变成一次广播。** 负责结束一个子进程连同它启动的一切的代码，
  会接受不是真实进程 id 的值；而 1 这个值在内核里不是「第一个进程组」，是对同一
  用户的所有进程发信号 —— 在你自己的机器上，那就是你开着的每一个进程。现在调用
  之前先检查值：0 和 1 被拒绝，各种以前能从类型检查下面溜过去的占位值同样被拒绝。
  （[#14](https://github.com/YongmaoLuo/Product-Development-Team/pull/14)、
  [#21](https://github.com/YongmaoLuo/Product-Development-Team/pull/21)）

- **一次全绿，说明的是代码，不是它碰巧跑在哪台机器、哪个顺序里。** 测试之间不再
  互相遗留运行态，一条断言不会因为自己在运行序列里的位置而对；每个并行测试集合按
  打乱的顺序执行自己的文件，并把种子打进日志，下一次可复现。这些测试集合也被切到
  能在所跑机器的时间预算内完成的规模。并发压力测试不会再报出「两个任务同时持有
  一把锁」—— 它测量重叠的窗口原本多算到了锁释放之后，那个违例是测量的产物，
  不是锁的。
  （[#7](https://github.com/YongmaoLuo/Product-Development-Team/pull/7)、
  [#24](https://github.com/YongmaoLuo/Product-Development-Team/pull/24)、
  [#32](https://github.com/YongmaoLuo/Product-Development-Team/pull/32)）

## [0.1.0] - 2026-09-29

首个发布，也是本仓库的根提交：前面没有可供比较的状态，这一节里的每一条都是新的。

### 新增

- **一个 spec 驱动的 agent harness：一名操作者，一句话想法，到经过验证的交付。**
  一个产品开发团队几十个人要做的事，它来做；而它跑的是一个循环，不是一条流水线 ——
  每一轮都对着前面几轮产出的产物被评判，每一步都踩在前一步做过的决策点上。这正是让
  工作收敛到对的答案、而不是漂到某个看起来合理的地方的原因，也正是它可审计的原因：
  你读到的是*为什么*，不只是*是什么*。

- **没评审过的东西，不会被建出来。** 七个阶段：需求澄清、PRD、可选的架构、可选的
  测试设计、任务生成、执行、验证。PRD、架构与测试设计共用同一套评审循环，每个决策点
  都是一条 **CPEA** 记录（Context / Problem / Evaluation / Action）—— 一个选择背后
  的证据，和这个选择写在同一页上。每条决策点都可以接受、拒绝（只重写那一条）、
  追问、搁置；在所有决策点被接受或搁置之前，不会开始执行。

- **执行阶段并发跑任务，边界按真正安全的东西划定。** 声明的依赖决定哪些任务可以
  同时开始；在一层内部，按每个任务声明的目标文件建一张冲突图 —— 碰同一个文件的任务
  串行，其余的并行。每个 provider 在飞的子 agent 数量有上限；而一个实际碰到了自己
  没有声明的文件的任务，由操作系统级文件锁兜底。

- **验证也并发跑，而且失败会生成工作，而不是结束这一轮。** 验收标准变成验证点，
  逐条对着**运行中的代码**核验 —— 一条测试命令、一次 HTTP 调用、一个真实浏览器、
  一次代码评审 —— 按方法分组，组与组内的验证点都并行，共用一个计划级的并发上限。
  失败变成修复任务，按同样的并发规则、同样的完成判据重新进入执行，没有第二条更弱的
  路；而一个任务，只有在 agent 的报告与它声明的测试命令一致时才算完成。循环在四种
  情况下停：通过、轮次预算耗尽、同一失败集合重复、操作者叫停。

- **进度是数据库里的状态机，不是产物文件里的字段。** 一个计划处在哪个阶段，是 schema
  的事实；阶段之间合法的移动是状态转移，而不是赋值。运行态住在一个 SQLite 数据库
  里；JSON 产物是种子和记录，任务列表是派生产物、从不重写 —— 于是一轮修复不会留下
  第二份任务图，被中断的一轮是继续，而不是重来。服务器也不会启动到一个写路径已坏的
  数据库上，而一次干净的关闭会留下一份可恢复的副本。

- **边界是写明的，不是留给读者推断的。** 这是一个单用户本机工具：没有账号、没有
  认证、没有用户隔离，子 agent 以操作者本人的权限运行 —— 边界是那台机器，而不是
  这个应用。在这个范围之内：你恰好开着的某个网页无法驱动这个实例，解析到回环的
  主机名会被拒，带 provider 凭据的文件私有写入、并在需要它的进程退出后脱敏。仓库
  对自己携带什么同样刻意：provider 容量与路由以 `example/` 下的模板交付，由你复制
  进一个被 gitignore 的目录 —— 于是没有任何一次部署的具体 provider 名字，被编译进
  别人的安装。见[安全](docs/security.md)。

[0.1.2]: https://github.com/YongmaoLuo/Product-Development-Team/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/YongmaoLuo/Product-Development-Team/releases/tag/v0.1.0
