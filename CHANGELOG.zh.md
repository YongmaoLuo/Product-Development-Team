# 变更日志

本项目所有值得注意的变更，都记录在这里。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

[English](CHANGELOG.md)

## [Unreleased]

**钥匙串读取搬离了那个处理请求的进程。** 两个通知 secret 现在由启动器在启动时解析一次、
发布到匿名管道，然后 `exec` 出服务端 —— 于是应答 HTTP 的进程完全不跑钥匙串工具。与之
配套的还有一个可选读取器：把条目的访问控制名单收窄成只认你自己签名的那个二进制，让
`/usr/bin/security`（机器上任何进程都能执行）不再构成授权本身。

### 新增

- **预生成的验证计划书与任务列表共存，并按写好的内容被消费。** 同时携带
  `verification_plan.json` 与 `tasks.json` 的计划目录过去会丢掉其中一份；
  现在 `backend/plan_artifacts.py` 同时保存两份产物、计划路由同时提供两份，
  验证轮直接消费预生成的计划书，不再自行生成。

- **调度面板有了自己的「收尾」分区。** 正在收尾的计划不占槽位、不等容量，所以它既不
  在队列里也不在运行列表里，操作者根本看不到有计划走到了收尾阶段。
  `GET /api/scheduler/finishing`（`backend/routes/scheduler.py`）把这些计划点出来 ——
  它们处在哪个阶段（收尾中 / 收尾失败），以及它们在等哪些问题，那些问题带着等待表格
  同样的延长 / 终止 / 回答三个操作。面板把它画在 `frontend/index.html` 与
  `frontend/app.js` 里。

- **调度视图有了自己的地址。** 点开面板会把 `#/scheduler` 写进 URL
  （`frontend/app.js`），于是刷新之后回到面板，而不是把面板弄丢；hash 变成别的值
  就回到计划列表。

- **`backend.secret_launcher`：读一次钥匙串，然后变成服务端。** 它解析每个通知 secret、
  把值写进匿名管道、在环境变量里指明描述符，最后 `exec` 出 `backend.server` ——
  PID 不变，所以 supervisor 对这条服务的观察不会变。服务端随后读的是自己继承来的
  描述符，因此 `credentials.read_secret` 报 `"inherited_fd"`，钥匙串那一条分支已经
  不存在了。开关关着的部署不受影响，仍然用 `python -m backend.server` 起。见
  [钥匙串迁移](docs/operations/keychain-migration.md)。

- **`tools/pdt-secret-reader/`：一个你自己签名的读取器，以及信任它的那次改写。**
  `security` 建的条目，访问控制名单里写的是 `/usr/bin/security` —— 一个机器上任何进程
  都能执行的 Apple 签名二进制；于是这份默认名单把 secret 交给了任何能跑一条命令的
  东西，而且是无提示的。`setup.sh build` 签发一张自签名证书和一个签名读取器，
  `adopt --widen` 把读取器加进每个条目的名单、同时保留已经信任的那些，
  `adopt --narrow` 再把名单改写成只有读取器一个。设了 `PDT_SECRET_READER_PATH` 时读取
  经由它进行，argv 与 `security` 完全相同，所以读取本身没有任何别的变化。流程、退出
  码，以及它**防不住**什么，都在它的 README 里。

### 变更

- **`secrets verify` 现在问的是机器，不是进程。** 这条命令直接读钥匙串，因为那才是
  操作者能据以行动的问题：不经启动器起的服务端会把一切都报成 `missing`，什么也解释
  不了。它照旧打印 `source=keychain`，但这个标签现在的意思是「这台机器的钥匙串里有」，
  而不是「服务端能解析出来」。

### 判定语义变更

*判定语义发生了变化。小节名在两种语言里保持一致，是刻意的：CI 门禁在两份变更
日志里读的是同一个字符串，而只存在于其中一份的小节，有一半读者看不到。*

- **轮次收口的门禁重算，以本轮自己的最终结论为准。**
  `backend/verification_executor.py` 在收口时经 `backend/verification_agent.py`
  接收本轮执行信封，先读本轮账本、再退回跨轮裁决表——上一轮遗留的旧 `BLOCKED`
  不再把被延后的全量门禁挡在派发之外；两本账都没有终态成功时，逐字保留原行为。

- **被延后的全量门禁，在阻塞者翻绿后于轮次收口时真正补跑。**
  `backend/verification_executor.py` 在收口重算之前先按本轮账本把被延后的
  层找回来——执行器重建 / 续跑之后进程内的表已经忘了它，账本还记得；执行器
  读不到阻塞者结论时保留账本里的原理由。没被问过的门禁不再被报成跳过。

- **答不上来的门禁，不再拿上一轮的 junit 当证据。**
  `backend/verification_ci_runner.py` 按报告自身的时间戳判定新鲜度：本轮没产出
  junit 时，门禁判 INCONCLUSIVE，而不是把上一轮遗留的报告当作本轮证据；清理旧
  报告在前，清理失败会被记录而不是被吞掉。`backend/verification_plan_delta.py`
  认得出命令里引用的脚本名（含中文标点与绝对路径），只把命令改指到另一个脚本
  的改动不再被读成「没有变化」。**没有任何裁决被改动**：真正通过的门禁仍然通过，
  真正失败的门禁仍然失败。

- **被暂停的计划，只有它的进程真的停下才算停。** 修复轮里的暂停在停止动作之后读取
  该计划的进程台账（`backend/verification_loop.py`），只要计划还有任何一部分活着就报
  `pause_kill_incomplete`，而不是拿执行器的退出当作「已经停下」的证据 —— 计划绝不会
  在还有东西在跑的时候说「已停止」。**没有任何裁决被改动**：一轮对验证点报告什么
  结论还是什么结论。

- **整仓 CI 判红时点名失败的那条 lane。** `scripts/ci_gha.sh` 在发出 FAILED 之前把该
  run 的失败 job 打印出来（job 列表本来就在手上），于是一次判红带着可定位的证据，
  而不只是一个退出码。**没有任何裁决被改动**：门禁的通过/失败判定不变。

- **终态失败只宣布一次自己的复盘。** 执行或验证落到终态失败时，
  `backend/verification_loop.py` 在终态相位写入之后宣布一次失败复盘 —— 同 plan
  同轮只一次，通告失败不抛出。它创建的决策不设期限，也不会被超时窗口结算。
  **没有任何裁决被改动**：该轮报什么状态还是报什么状态。

- **一轮只按自己的台账认领它声明的服务。** `backend/verification_agent.py` 把本轮
  的 plan 目录与服务台账交给声明式服务回收，于是上一轮留下的监听者会被认成它本来
  就是的那个服务，而不是把整轮打成环境阻塞。**没有任何裁决被改动**：真正外来的
  服务仍会被拒绝。

- **调度卡片能把「收尾」和「验证在跑」分开了。** 收尾阶段在跑的那段时间，卡片以前
  一直显示「验证中」；收尾失败停下来的计划则**彻底不再推送** —— 卡片冻结在失败前的
  那一版，也没人知道这条计划正在等人决定。现在两个相位各有自己的标签与标题
  （「收尾中」/「收尾失败（待决定是否续跑）」），且两次相位转换各发一条状态事件，
  卡片在转换发生时就会刷新。**没有任何裁决被改动**：验证该报什么还报什么，收尾期间
  `verification_status` 仍是 `running`，计划照旧占着调度位。

- **`_ran_the_declared_entry` 命令豁免（`88d898f9`）。** 命令类验证点只问
  一件事 —— 这条命令退出码是不是 0 —— 而裁决记录着同一条命令与框架读到的退出
  码，就已经回答了它。输出里的文件名是这次运行的日志，不是这道题的主语，于是
  `verdict_subject_mismatch` 不再拿它们做比对（`backend/verification_evidence.py`）。
  在此之前，一条声明入口刚退出 0 的命令类验证点，会因为「回答的是别的问题」被
  改判 FAILED —— 一次由框架自己的记账产生的假红。这条豁免随一个不相干的 PR
  合入，此前没有留下任何记录；这一条就是那次补记。

- **`scripts/ci_judgment_surface.sh` 是判定面三项一致性检查的唯一入口，
  `.github/workflows/ci.yml` 把它接成一道门禁。** 判定面由三件允许彼此
  漂移的产物描述：裁决 PASS/FAIL 的函数清单、清单点名的决策表，以及按
  字节冻结的行为金标。三件各有各的检查器，而三个入口就是链子腐坏的三条
  路 —— 其中一条可能已经不再运行，另外两条却照样报绿，读 CI 日志的人还
  得先弄清该信哪一条。脚本现在按顺序把三件都跑一遍，只回一个数字：普查
  是 advisory 的 —— 清单与代码对不上时先报警；真正拦人的是「清单点名的
  表确实在」这道静态门禁与行为金标。工作流把它接到一个独立的
  `judgment-surface-consistency` job 上，合并门禁把这个 job 列进自己要等
  的那些 job，于是这三项检查不再是某个名字里没提判定面的泳道里的一个步骤。

- **`backend/verification_loop.py` 给收尾阶段加了一个有上限的续跑入口。**
  停在 `postprocess_failed` 的计划以前没有出口：相位存在，却没有人能触发
  续跑。现在由人显式调用续跑端点，它只继续进度文件里记录为未完成或失败的
  条目 —— 对外动作已经成功的条目不会被重复执行。没有任何自动续跑，续跑
  次数由一份固定预算（`backend/postprocess/retry_budget.py`）封顶：续跑会
  重复对外动作（开 PR、推送），而无人看着的无上限重跑，正是这道人审门
  要防的东西。

- **`.github/workflows/ci.yml` 现在把 PR 的提交数限制在 5 个以内。**
  这条门禁把「开 PR 前先压一下」从习惯变成检查：超过 5 个 commit，
  分支历史就不再像一个决定那样可读，而「每个 commit 都有 CI 记录」
  正是第一个丢掉的东西。这个 job 读的是 `pull_request.commits` ——
  也就是 PR 页面自己显示的数字 —— 并接入 merge gate 的 `needs`。

- **失败点只在双条件下阻塞。** `backend/framework_fix/classifier.py` 里的
  `decide()` 就是这条新规则的全部：只有「验收关键 **且**（已复现为框架缺陷
  （`framework_deterministic`、`framework_timeout`）或改动越界）」时才算
  `blocking`。两个合取项都要成立 —— 只成立其一，或只成立其二，都不是框架
  缺陷。非验收关键的失败点**永不阻塞**，别的条件成立与否都不改变这一点。
  复现结论是 `flake` 或 `not_reproducible` 的同样不阻塞，而且它**优先于**
  「改动越界」：刚被证明是 flaky 的这一轮，不会因为改动也越了界就被按住。
  「改动越界」这一半自己就能成立 —— 改动越出计划范围，无需先复现即可
  `blocking`。复现过却归不了类的失败点是 `unclassified`，登记为证据；连续
  两轮如此则是 `exhausted`，交给人看；还没复现的失败点是 `needs_repro`，
  本轮不阻塞。产出是一个 `FailureDecision`，它的 `blocking` 标志恰在处置
  为 `blocking` 时为真 —— 而非验收关键与 flaky 复现产出的都是
  `not_blocking`。

- **计划现在有了类型，执行器只在一处读它。** `plan_kind`
  （`backend/plan_state.py`）说明一个计划是什么：`normal` 是普通业务计划，
  `framework_fix` 是为了修框架本身而存在的计划。执行器
  （`backend/agent.py`）里只有一处判断缝隙 `plan_start_decision()`：
  `framework_fix` 计划不再等批量调度，直接开跑当前任务，因为这类计划的意义
  就在于现在就到那个任务。执行的其它一切都没变 —— 没有第二条执行路径，
  没有新的调度机制，没有第二处判断。没说自己是什么类型的计划按 `normal`
  处理，行为与现状逐字未变；而类型读不出来时**抛错**，不猜：猜成 `normal`
  会悄悄改掉这条计划正在做的事，而它自己永远不会知道。

- **验证结果按树追加，状态接口现在说得出结论出自哪棵树。** 结果行只写一次，
  永不更新：同一个验证点在另一棵树上再跑一次，就在只追加的表
  `plan_verification_result`（`backend/state_machine/db/schema.py`）里再追加
  一行 —— 于是「在什么代码上试过什么」被保留下来而不是被覆盖；一次失败若
  覆盖了自己先前那一行，账就少了一行。把这些行读回来是 `read_vp_results()`
  （`backend/verification_persistence.py`），而状态响应
  （`backend/routes/verification.py`）现在多带一个 `vp_results`：每个验证点
  一行，取最新那棵树上的那行，带上该验证点的 `tree_fingerprint`，以及它
  一共留下过几行（`history_count`）。还没有结论时是空列表，而不是缺字段 ——
  「还没有裁决」本身就是一条答案。被框架缺陷挂起的计划不许起验证、也不许
  重置：那一轮等的是框架被修好，而不是再一次尝试；拒绝是 `409`，带上
  `blocked_on_framework_fix` 与它被挂在哪个缺陷上。守卫读不到挂起状态时
  拒绝（`500`），**不放行**：把「读不到」当成「没挂起」的守卫等于没有
  守卫。

- **base drift 的基线改为每个 plan 自己声明（`backend/verification_agent.py`）。**
  分支该跟谁比，是**仓库**的属性，不是部署的属性：remote 可能叫 `origin`
  也可能叫 `github`，默认分支可能叫 `main` 也可能叫 `master`。一个环境变量
  装不下这些，而默认的 `origin/main` 在有些仓里根本解析不出来 —— 在那些仓里，
  这道检查每轮都报一次失败，一次真实漂移也没量到过，落在日志里跟「分支从来没
  落后过」长得一模一样。

  现在每个 plan 在 `plans/<plan_id>/base_drift.json` 里写自己的基线，没写的
  plan 仍走环境变量。优先级是 plan 文件 > 环境变量 > 默认值，所以**没有这个
  文件的 plan，行为与改动前完全一致** —— 同一个基线、同一个模式、同一个结果。
  这个文件永远不抛异常：文件缺失、JSON 坏掉、键类型不对，都逐键回落到下层，
  因为配置里的笔误不该悄悄关掉这道检查，也不该把读它的验证轮带崩。被它取代的
  恰恰是「猜一堆候选 remote 名」那种做法 —— 那会量错东西，或者什么都量不到，
  而看上去像是量到了。

- **编码工具现在必须被告知它是在哪棵树里干活，而失败指纹改由载荷决定。**
  `ClaudeCodingTool`（`backend/coding_tool.py`）把工作根变成必填的关键字
  参数，不给名字就不肯被造出来 —— 没有名字的根会让子进程落在服务端自己的
  目录里，而那不是这次调用关心的那棵树。现在每个构造点都点名了根：验证
  编排器把自己建出来的工具指向计划的 project directory
  （`backend/verification_agent.py`），而给卡住的那次尝试下裁决的
  summarizer 拿到的是同一个根（`backend/verification_subagent.py`）。
  终局裁决的判定者与 HITL 端口不再自己猜这个根：判定者在被告知的那棵树里
  调查，点不出树名就干脆不给工具（`backend/verification/terminal_analysis.py`），
  端口则先取计划登记的 project directory、其次才是计划自己的目录
  （`backend/hitl/terminal_decision_port.py`）。失败指纹遵守同一条「一个问题
  一个权威」：评分器自己的 `failed_assertions` 载荷在场时，由载荷决定指纹，
  报告的散文不再被一起哈希，于是同一个缺陷无论散文怎么改写都得到同一个
  指纹；散文（`first_error_line`）只在没有载荷可哈希时才参与
  （`backend/verification/failure_fingerprint.py`）。

- **声明的 squash 条目可以暂存产品代码 diff。**
  `backend/postprocess/executor.py` 认出声明的 squash 条目
  （`git reset --soft origin/main`，逐字匹配，且条目自己的 `target` 必须
  与之一致），让这一条跳过产品代码守卫的 porcelain 判据：把分支 diff
  放进暂存区正是这一条要做的事，产物由紧随其后的 commit 条目提交。
  **内容判据不变** —— `reset --soft` 不碰文件内容，产品代码路径下的
  sha256 比对照旧能抓到真正的内容改动；这一条执行期间真的改写了产品
  文件，照样判红。**其他条目的裁决没有任何变化**：压扁到别的基线、
  `git reset --mixed` / `--hard`，以及任何写下产品代码的条目，都还是
  原来那个判法。

- **被 Phase-1 结论延后的 Phase-2 门禁层，改按该结论的最终状态重判；阻塞者
  翻绿之后，这一层在同一轮里真正跑起来。**
  `backend/verification_executor.py` 把被延后的门禁层记在本轮裁决账本里 ——
  账本才是「这一轮有门禁层被延后、还没跑过」的真相 —— 于是当执行器被重建、
  进程被换掉时，某个在阻塞者还是 `BLOCKED` 时被延后的层依然能被找回来，
  延后理由也会改口成**当下**真正阻塞它的那一个：理由不会再继续点名一个
  已经转绿的阻塞者。`backend/verification_agent.py` 在轮次收口处重新判定并
  派发这些层 —— 位置在本轮最后一件能改写结论的事之后、报告生成之前 ——
  于是阻塞者在轮次后半程翻绿的，那一层仍在**同一轮**里跑起来。阻塞者仍然
  `FAILED` / `BLOCKED`、或者根本没有结论时，那一层照旧被延后。**这次改的
  是裁决本身**：过去一个门禁层因为过期的 Phase-1 结论而一直延后的轮次会被
  报成 `SKIPPED`，现在它带上关卡自己的结论。

- **整仓门禁只判当前树，不再判分支历史。**
  `scripts/ci_gha.sh` 以前把 HEAD 原样推到它的一次性探针分支上，而推一个 ref 会
  触发仓库的 pre-push 钩子 —— 钩子要扫「被推 ref 上尚未在远端的每一个提交」，
  于是分支历史里一个当前树早已不存在的旧提交，也能把门禁挡在推不动探针分支的
  地方，门禁于是判红，而没有任何一条 lane 真的跑过。现在探针分支指向一个由
  HEAD 的树构成、父提交为 `origin/main` 的单提交，钩子扫到的就只是当前树相对
  基线的净变更，分支历史不再进入扫描范围。历史检查并没有丢：pre-push 钩子守着
  分支的真实推送，ci.yml 的 `commit-range-privacy` 作业在 PR 上跑 —— 门禁走的是
  `workflow_dispatch`，本来就不跑那个作业。**没有任何裁决被改动**：门禁仍然是
  0 / 1 / 3 那三个退出码；变的只是它推什么。

- **不合规的判定输出不再被记成代码失败。** 子代理给出的答案不满足裁决契约时，
  关于交付物其实什么都没确立，于是现在它被记成 `UNKNOWN` 并带上 `infra` 失败
  类别，而不是 `FAILED`（`backend/verification_subagent.py`）；报告里那条判了
  失败、却从未自述交付物缺陷的条目会被还原成 `UNKNOWN`，而不是留下一条替没人
  验过的东西买下的修复任务（`backend/verification_agent.py`）；「没有结论」这个
  状态有了自己的桶，不再被折进失败计数里（`backend/verification_buckets.py`）。
  修复批次的结束由一个分类器判定，执行器的死亡被记在那个裁决旁边，而不是写在
  它上面 —— 于是「代码还是不对」与「还有活要干」不再被压成同一件事
  （`backend/verification_loop.py`）。收敛汇总读的是同一本账，把每个计划归进
  `passed`、`user_stopped`、`env_blocked`、`infra_flake`、`code_defect` 或
  `unclassified`（`backend/verification/convergence_metrics.py`、
  `backend/routes/verification.py`）。**这次改的是裁决本身**：一轮什么都没验出来的
  情况，过去被报成交付物的失败，现在被报成「没有结论」。



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
