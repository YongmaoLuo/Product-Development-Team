# 项目规则

从真实故障里长出来的规则。每条都写明它防的是什么故障，读的人可以据此判断边界，而不是盲从。英文版见 [project-rules.md](project-rules.md)。

## 测试与产品语义

1. **改契约就要改掉所有断言旧契约的测试。**
   当改动改变了可观察契约——相位名、接口返回、同步调用变成后台过程——合入前先 grep 测试树里的旧契约，逐个更新。改了哪条路径、那条路径的 CI 是绿的，都不算证据：过时的断言藏在这次改动没碰过的文件里。
   *它防的故障（2026-11-21）：* stop 变成异步后，`tests/api/test_plan_lifecycle_equivalence.py` 里一个用例改了、隔壁的漏了；漏掉的那个立即断言相位已是 `stopped`，本机 5 次跑红 4 次。

2. **断言异步操作结果的测试，必须等终态。**
   终态（相位已落地、记录已清、文件已存在）通过有界轮询读取（同文件的 `_wait_for_phase`，30 秒），绝不在请求返回后立即断言。请求只回答 `stopping`，什么时候真的停下只有后台层知道。
   *它防的故障（2026-11-21）：* `test_execution_status_stopping.py` 在相位刚变时就断言 stop 记录已清，而 finalize 要等 reap 与 requeue 之后才清，CI 负载下必输这一场竞速。

3. **测试不要依赖模块身份。**
   `server.py` 结尾是 `sys.modules.setdefault("server", sys.modules[__name__])`，谁先加载它，`"server"` 这个名字就归谁，日志记录随之带上那个模块的 `__name__`。过滤日志用 `server.__name__`，绝不用字面量 `"server"`。
   *它防的故障（2026-11-21）：* 只要 `test_partial_completion_path.py` 与 `test_heartbeat_tick_isolation.py` 同进程，后者必红——前者把 `server.py` 以别名 `partial_completion_path_test_server` 加载。

## CI 分片与顺序

4. **分片成员是位置切出来的；执行顺序是故意随机的。**
   文件按 `NR % count == index` 切片，再按 `run_id.run_attempt.shard` 的 seed 每轮打乱（`ci.yml`）。新增任何一个测试文件，它后面所有文件的槽位整体平移——同一个测试可能因为一个不相干的 PR 换上不同的邻居。

5. **随机顺序是探针，不是缺陷。**
   因顺序依赖变红，说明测试卫生有问题：修测试，不要关掉 shuffle。每轮都会打印 `execution order seed`，失败可复现。

6. **一个文件不等于一个进程。**
   `-n 8` 起 8 个 worker，但默认 `--dist load` 下**每个 worker 都会 import 全部被收集的文件**。只有「一文件一次调用」才隔离模块级副作用。

## Pull Request

7. **一份工作只对应一个 PR。**
   修复、压包、解冲突都推到现有 PR 分支上（`git push --force-with-lease`）。不要为同一个改动开第二个 PR。`main` 受保护，永不 force-push。

8. **分支上每一个 commit 都必须有绿色的 CI。**
   中间出现红的 commit 就重建（amend/squash）再 force-push，绝不把红的 commit 合进历史。

9. **「绿」指 merge gate 绿。**
   所有 PR job 通过；skip 可以，failure 不行。

10. **合入需要 owner 批准**，除非 owner 明确说过该 PR「绿的就可以合」。

11. **合入是核对出来的，不是假定出来的。**
    合入后在主 checkout `git pull --ff-only`，确认 `git diff --stat <旧main>..main` 恰好是该 PR 的文件。这条检查防的正是「合入时静默删掉 main 上的提交」。

12. **PR 元数据不带 plan id、内部代号、AI 署名。**

## 验证流程

13. **验证环境是 GitHub Actions。**
    本地只跑定向测试；全量门禁在 push 之后跑。公开仓的 PR 不需要本地 CI 容器。
