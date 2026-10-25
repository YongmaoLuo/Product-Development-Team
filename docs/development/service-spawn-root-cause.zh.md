# 服务启动失败的根因

本文是这份说明的中文页。英文版是 `service-spawn-root-cause.md`；两份内容相同，
本文是下游工作读取的契约（`## 候选失败率`、`## 根因`、`## 修复形状`）。

## 现象

按生产参数从运行中的 Python 进程拉起后端服务时，服务在就绪之前退出，
启动方拿到的是「服务没有起来」。后端自己的日志里是一次异常：

```text
OSError: [Errno 9] Bad file descriptor
```

它来自 asyncio 事件循环的 selector：事件循环在 `select()` 上等一个已经
失效的 kqueue 描述符。本机是 macOS，asyncio 用的是 kqueue；换句话说，
事件循环在等一个已经不存在的描述符。

## 基线

未修复的代码上，失败率是满的。**这组数字是在旧的 spawn 形状下测的**：
那时候生产直接用 `/bin/bash -lc <cmd>` 起服务，还没有 wrapper。

| 指标 | 值 |
|---|---|
| 目标 | pdt |
| profile | raw |
| 启动次数 | 10 |
| 就绪 | 0 |
| 失败 | 10 |
| 失败率 | 1.000 |

复现命令（相对路径，不随检出位置失效）：

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --profile raw --target pdt --runs 10 --ready-timeout 8 --assert-failure-rate 0.5 --json-out .pdt/spawn-repro/baseline.json
```

这条命令以 0 退出：失败率断言（≥ 0.5）成立。

修复之后，生产换成了 wrapper 形状（见 `## 修复形状`），复现器也跟着换；
同一条命令在新形状下的复测见 `## 修复后的复测`。

## 候选失败率

每个候选测两格：参数取生产值（关）与取替代值（开），每格 5 次启动。
**下面这张表是在未修复的代码上、旧的 spawn 形状下测的**——根因是从这张表里
读出来的：它问的是「缺陷还在的时候，失败跟不跟着这个候选走」。

| 候选 | 开 | 关 | 结论 |
|---|---|---|---|
| start_new_session | 5/5 | 5/5 | 无关 |
| stdin | 5/5 | 5/5 | 无关 |
| parent_threads | 5/5 | 5/5 | 无关 |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --target pdt --runs-per-cell 5 --ready-timeout 8 --assert-single-cause --json-out .pdt/spawn-repro/matrix.json
```

这条命令以 1 退出，并说明「没有候选移动失败率，根因不在候选集合里」。

## 为什么原三候选复现不出

原三候选改的都是「进程怎么被拉起来」，不是「拉起来的时候交给了他什么」。

- `start_new_session`：会话是否独立。失败与服务属于不属于同一个会话无关，
  关掉它 5/5 失败，开着它也 5/5 失败。
- `stdin`：子进程的输入接 devnull 还是继承父进程。两者都是 5/5 失败，
  输入不是变量。
- `parent_threads`：父进程带 0 个还是 8 个忙线程。两者都是 5/5 失败，
  父进程的线程数不是变量。

这不是「偶发」：基线 10 次启动全部失败，矩阵 30 次启动全部失败，失败是
确定性的，每次都落在同一处异常上。原三候选复现不出，不是因为它们测得
不够仔细，而是因为根因不在它们测的那一层——它们改不到失败发生的地方。

## 补充候选

把问题换成「启动时交出去的东西是什么」，再加三个候选：交出去的环境、
继承的描述符、工作目录。**这张表同样是在未修复的代码上、旧的 spawn 形状下
测的。**

| 候选 | 开 | 关 | 结论 |
|---|---|---|---|
| clear_env | 0/5 | 5/5 | 因果 |
| close_fds | 5/5 | 5/5 | 无关 |
| cwd | 5/5 | 5/5 | 无关 |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --candidates clear_env,close_fds,cwd --target pdt --runs-per-cell 5 --ready-timeout 8 --assert-single-cause --json-out .pdt/spawn-repro/matrix-extra.json
```

这条命令以 0 退出：恰好一个候选移动了失败率。只有 `clear_env` 移动了——
子进程拿一份干净环境时 0/5 失败，拿父进程的环境时 5/5 失败。另外两个
候选不动失败率：继承的描述符（`close_fds`）与工作目录（`cwd`）都不是变量。

修复之后这三个候选在新形状下重测的结果见 `## 修复后的复测`。

## 根因

父进程的环境里有 `PDT_SECRET_FD_*` 这一类变量，记录的是「某个描述符号」。
这个号只在**发布它的那一个进程里**才有意义。子进程继承了这个变量，但那个
号在子进程里指向别的东西——父进程里它是管道的一端，子进程里它已经被别的
东西占用了。

后端启动时会去读这些变量：`credentials.read_secret_fd()` 按变量里的号去读，
并且**把这个号关掉**，无论读没读到。子进程自己启动时，asyncio 的 selector
先占了这个号。于是启动过程把关事件循环 selector 的那个描述符关掉了，事件
循环下一次 `select()` 就拿到 `Bad file descriptor`，服务在就绪之前退出。

在启动方这一侧，`clear_env` 是唯一能移动失败率的候选（0/5 对 5/5）：换掉
父进程的环境，子进程就看不到那个变量，也就不会去关一个不属于它的描述符。

后端自己的堆栈指到关掉描述符的那一行：

```text
server.py              feishu_notifier.start()
  feishu_notifier.py     self._client = FeishuClient()
    feishu_client.py       read_secret("feishu_app_secret")
      credentials.py         _resolve_from_inherited_fd(...)
        credentials.py           read_secret_fd(fd)
          credentials.py             os.close(fd)   <-- 关掉了事件循环的 selector
```

反证：把 `PDT_SECRET_FD_*` 这两个变量从子进程的环境里去掉，同一条启动命令
不再失败，后端照常起来并开始服务；变量留着，就复现。

## 修复形状

- 机制：wrapper

要修的不是启动的形状——会话、输入、用 fork 还是用 posix_spawn，都与失败
无关，三格数字已经把这三条排除掉了——要修的是启动时交出去的环境。修法是
让服务经由一个 wrapper 启动：wrapper 剥掉环境里 `PDT_SECRET_FD_*` 这一类
「描述符号」变量，再 exec 真正的服务。

三条约束：

- wrapper 不能伤到 launcher 自己的交接。launcher 直接 exec 服务，描述符号
  在那个进程里有效；这条路径不走 wrapper，所以不在这次改动的影响面里。
- wrapper 不改服务的启动参数：会话、输入、端口、工作目录一律保持生产值，
  否则下一次测量就分不清是修好了还是绕开了。
- 验证口径不变：未修复的代码上失败率 ≥ 0.5，修好之后同一条命令的失败率
  是 0。

## 修复后的复测

修复落地之后，生产的 spawn 形状换成了 wrapper：剥掉环境里的
`PDT_SECRET_FD_*`，`setsid()`，再 `execvp` 真正的命令。复现器也跟着换——
argv 直接取 `service_manager._detached_argv`，不复制一份，所以复现器测的
就是生产现在真的在用的形状，而不是一个已经不存在的形状。

**基线**（同一条命令，形状换了）：

| 指标 | 值 |
|---|---|
| 目标 | pdt |
| profile | raw |
| 启动次数 | 10 |
| 就绪 | 10 |
| 失败 | 0 |
| 失败率 | 0.000 |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --profile raw --target pdt --runs 10 --ready-timeout 8 --assert-failure-rate 0 --json-out .pdt/spawn-repro/baseline.json
```

这条命令以 0 退出：失败率断言（= 0）成立——十次启动全部就绪。这就是验收口径
的另一半：未修复的代码上失败率 ≥ 0.5，修好之后同一条命令的失败率是 0。

**三候选二分表**（每格 5 次启动）：

| 候选 | 开 | 关 | 结论 |
|---|---|---|---|
| start_new_session | 0/5 | 0/5 | 无关 |
| stdin | 0/5 | 0/5 | 无关 |
| parent_threads | 0/5 | 0/5 | 无关 |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --target pdt --runs-per-cell 5 --ready-timeout 8 --json-out .pdt/spawn-repro/matrix.json
```

这条命令以 0 退出：没有任何候选移动失败率——因为失败率本来就是 0。旧形状下
这三行是 5/5 对 5/5，那是在**缺陷还在**的时候测的，问的是别的问题。

**补充候选**：

| 候选 | 开 | 关 | 结论 |
|---|---|---|---|
| clear_env | 0/5 | 0/5 | 无关 |
| close_fds | 0/5 | 0/5 | 无关 |
| cwd | 0/5 | 0/5 | 无关 |

```bash
cd backend || exit 1
.venv/bin/python3 tests/manual/spawn_repro.py --matrix --candidates clear_env,close_fds,cwd --target pdt --runs-per-cell 5 --ready-timeout 8 --json-out .pdt/spawn-repro/matrix-extra.json
```

这条命令以 0 退出。`clear_env` 在旧形状下是因果（0/5 对 5/5），在新形状下不动
失败率（0/5 对 0/5）：wrapper 已经在交接的时候把那些变量拿掉了，不必再换一整份
环境。这正是修复要的效果——把「换一份干净环境」这件笨事，变成「交接时就不给
子进程那根号」。

## 负控

复现测试在修好的代码上是绿的，但「绿的复现」证明不了什么：同一条测试在缺陷根
本不出现的机器上也是绿的，从输出上看不出两者的区别。负控就是把这层区别去掉——
把修复回退一次，同一条测试必须变红；再把修复装回去。

回退用 patch 文件，不用 `git checkout --`，也不用 `git stash`：这两条会把同一批
里的其它改动一起冲掉。补丁取自 task-4 那次改动（`[task-4] 修复 spawn 形状：wrapper
剥掉描述符变量后 exec 服务`）：

- 修复补丁：`git -C .. show 27d7237 -- backend/service_manager.py > /tmp/service-spawn/fix.patch`
- 回退：`git -C .. apply -R /tmp/service-spawn/fix.patch`
- 回退后：`test_three_consecutive_spawns_all_ready` FAILED —— `assert failed == 0`（`failed=3`）
- 恢复：`git -C .. apply /tmp/service-spawn/fix.patch`
- 恢复后：`git -C .. diff --stat` 无输出（工作区与 HEAD 一致）
- 恢复后：3 passed

整条命令序列（相对路径，不随检出位置失效）：

```bash
cd backend || exit 1
PY=.venv/bin/python3
git -C .. show 27d7237 -- backend/service_manager.py > /tmp/service-spawn/fix.patch
git -C .. apply -R /tmp/service-spawn/fix.patch
$PY -m pytest tests/integration/test_service_spawn_repro.py -v
git -C .. apply /tmp/service-spawn/fix.patch
$PY -m pytest tests/integration/test_service_spawn_repro.py -v
git -C .. diff --stat
```

回退后那次运行的红色输出（首行是用例名，紧跟着的是断掉的断言）：

```text
tests/integration/test_service_spawn_repro.py::test_three_consecutive_spawns_all_ready FAILED
tests/integration/test_service_spawn_repro.py:368: in test_three_consecutive_spawns_all_ready
    assert failed == 0, (
E   AssertionError: batch 1 of 3: failed=3 of 3 spawns
```

`failed=3` 不是零：这一批三次启动，一次都没起来。恢复之后同一条命令是 3 passed，
`git diff --stat` 无输出——修复装回去了，工作区回到 HEAD。

## 第二道防线：不属于自己的描述符，一个 bit 也不能动

wrapper 修的是**交接**：服务看不到 `PDT_SECRET_FD_*`，于是没有描述符会被误当成
自己发布的管道。但交接本身会丢——父进程发布管道之后、子进程 exec 之前，管道可能
没有跟着继承过去，而**号**还在环境变量里。这个号在子进程里指向的是子进程自己的
东西，最常见的是 asyncio 的 selector。于是读者一侧还需要第二道防线：拿到一个不是
自己发布的描述符时，不读它、不关它、也不改它的 flags。

这道防线曾经漏在 flags 上。`_drain_published_pipe` 为了把管道切到非阻塞模式，先
`os.set_blocking(fd, False)`；在 macOS 上这个 fd 常常是 kqueue 描述符——`fstat`
说它是 FIFO（所以「是管道」这个必要条件过不去），而 `fcntl` 对它 ENOTTY，于是
`os.set_blocking` 抛错。

**抛错不等于什么都没改。** 内核是先执行 F_SETFL、再报 ENOTTY 的，于是这个属于
别人的描述符被永久置成了非阻塞。实测（macOS）：

```text
新建 kqueue fd      F_GETFL=2   O_NONBLOCK=False
os.set_blocking(fd, False) → OSError: [Errno 25] Inappropriate ioctl for device
之后                 F_GETFL=6   O_NONBLOCK=True    ← 别人的 fd 被改了
os.set_blocking(fd, True)  → OSError: [Errno 25] Inappropriate ioctl for device
之后                 F_GETFL=2   O_NONBLOCK=False   ← 还原照样生效
```

隔离方式：把「只读快照」和「候选描述符处理」分开——`os.get_blocking(fd)` 走
F_GETFL，只读，失败就直接返回 None，那一步确实什么都没碰；而**一旦尝试过**
`os.set_blocking(fd, False)`（不论它成功还是抛错），**所有**出口都必须经过还原，
于是还原用的 `finally` 从「只包住读循环」提升成「覆盖那一次调用」。还原在
kqueue 上同样抛 ENOTTY，却仍然清掉 O_NONBLOCK——所以还原是可行的，缺口一直在
代码里，不在内核里。

判据（VP-019）：判定不成立时返回 None / 抛 ValueError，对 fd 既不 read 也不
close，不改其 flags。钉在 `backend/tests/unit/test_credentials_fd_payload.py`：
`test_drain_leaves_a_foreign_descriptor_flags_untouched`、
`test_reader_leaves_a_foreign_descriptor_flags_untouched`、
`test_reader_restores_the_flags_of_a_pipe_it_did_not_publish`。

## 验收：一条命令重跑 VP-019 全部条款

写下这一节时的 HEAD：`63b96593d0bcb2a68b973d1f5faa4e41d08e7fb7`——本节与
`backend/tests/unit/test_credentials_fd_protocol.py` 一起落地的那个提交。判据、条款
对照表与负控转录自那次提交起未变；此后本节只被追加过说明性文字。

VP-019 的判据（不可改）：

> 仅当 fd 为 FIFO（fstat 判定）且非阻塞读在写端已关闭时读到 EOF，才返回 payload
> 并在解析前 os.close(fd)；判定不成立时返回 None / 抛 ValueError，对 fd 既不 read
> 也不 close，不改其 flags；注释写明 macOS 上 kqueue 描述符对 fstat 同样是 FIFO
> （必要而非充分），并指向 docs/development/service-spawn-root-cause.md；
> PDT_SECRET_FD_* 变量名未被删除；审查须给出 file:line 引用

判据原先散在评审的散文里，于是「在 HEAD 上重跑一遍」没有可执行的载体：HEAD 一动，
上一轮的结论就无物可跑，只能回落到更早的执行轮。这一节把它变成代码里的产物——
一条命令，一次运行，**条款 ↔ 测试 ↔ 源码位置**三边对得上，不需要再从散文里拼协议。

### 验收命令

```bash
cd backend && .venv/bin/python -m pytest tests/unit/test_credentials_fd_protocol.py -v
```

只用 pytest。**不要**用 `python3 -c` 内联探针：上一轮任务自己的 pre-flight 命令
（`pytest ... && python3 -c '...'`）就是在 executor 动手之前先挂掉的，而那次任务
照样被判成 completed。判据要能被重跑，就不能依赖一条 cwd 敏感的一次性命令。

### 条款 ↔ 测试 ↔ 源码

| 条款 | 测试 | 符号 | 源码 |
| --- | --- | --- | --- |
| C1 | `test_clause_c1_fifo_is_decided_by_fstat` | `_is_pipe` | `backend/credentials.py:734` |
| C2 | `test_clause_c2_fifo_alone_is_not_sufficient` | `_is_pipe` | `backend/credentials.py:730` |
| C3 | `test_clause_c3_payload_is_returned_only_after_eof` | `_drain_published_pipe` | `backend/credentials.py:740` |
| C4 | `test_clause_c4_fd_is_closed_before_the_payload_is_parsed` | `read_secret_fd` | `backend/credentials.py:1416` |
| C5 | `test_clause_c5_unpublished_descriptor_raises_value_error` | `read_secret_fd` | `backend/credentials.py:1416` |
| C6 | `test_clause_c6_unpublished_descriptor_is_not_read` | `_drain_published_pipe` | `backend/credentials.py:740` |
| C7 | `test_clause_c7_unpublished_descriptor_is_not_closed` | `read_secret_fd` | `backend/credentials.py:1416` |
| C8 | `test_clause_c8_flags_of_a_live_writer_pipe_are_unchanged` | `_drain_published_pipe` | `backend/credentials.py:812` |
| C9 | `test_clause_c9_flags_of_a_regular_file_are_unchanged` | `_is_pipe` | `backend/credentials.py:718` |
| C10 | `test_clause_c10_flags_of_a_kqueue_descriptor_are_unchanged` | `_drain_published_pipe` | `backend/credentials.py:812` |
| C11 | `test_clause_c11_the_pre_fix_shape_changed_those_flags` | `_drain_published_pipe` | `backend/credentials.py:812` |
| C12 | `test_clause_c12_kqueue_is_a_fifo_to_fstat_and_the_comment_says_so` | `_is_pipe` | `backend/credentials.py:734` |
| C13 | `test_clause_c13_the_source_points_at_the_root_cause_document` | `_drain_published_pipe` | `backend/credentials.py:740` |
| C14 | `test_clause_c14_a_failed_f_setfl_still_takes_effect_and_the_source_says_so` | `_drain_published_pipe` | `backend/credentials.py:774` |
| C15 | `test_clause_c15_pdt_secret_fd_variable_names_are_not_removed` | `secret_fd_env_var` | `backend/credentials.py:1216` |
| C16 | `test_clause_c16_drain_never_touches_descriptor_flags` | `_drain_published_pipe` | `backend/credentials.py:812` |
| C17 | `test_clause_c17_the_guard_rejects_the_shape_it_exists_to_reject` | `_drain_published_pipe` | `backend/credentials.py:812` |
| C18 | `test_clause_c18_the_acceptance_table_matches_this_module` | `test_clause_c18_the_acceptance_table_matches_this_module` | `backend/tests/unit/test_credentials_fd_protocol.py:1133` |
| C19 | `test_clause_c19_drain_never_attempts_to_change_the_flags` | `_drain_published_pipe` | `backend/credentials.py:812` |
| C20 | `test_clause_c20_no_read_can_block` | `_drain_published_pipe` | `backend/credentials.py:825` |
条款与判据原文的对应：

- **C1**「仅当 fd 为 FIFO（fstat 判定）」——FIFO 由 `os.fstat` + `stat.S_ISFIFO` 判定。
- **C2**「**且**」——FIFO 只是必要条件：写端还活着的管道同样被拒。
- **C3**「非阻塞读在写端已关闭时读到 EOF，才返回 payload」——非阻塞切换在读之前。
- **C4**「在解析前 os.close(fd)」——close 发生在解析之前，解析失败也照样关掉。
- **C5**「判定不成立时返回 None / 抛 ValueError」——内层返回 None，外层抛 ValueError。
- **C6**「对 fd 既不 read」——见下面的「已知边界」。
- **C7**「也不 close」——描述符还在。
- **C8 / C9 / C10**「不改其 flags」——活写端管道、普通文件、kqueue 三种。
- **C11** 负控：修复前的形状**确实**改了 flags（`2 -> 6`），所以这条判据不是空话。
- **C12**「注释写明 macOS 上 kqueue 描述符对 fstat 同样是 FIFO（必要而非充分）」。
- **C13**「并指向 docs/development/service-spawn-root-cause.md」——指针指向的文件必须存在。
- **C14**「失败的 F_SETFL 依然生效」——这句事实写在还原所在的位置。
- **C15**「PDT_SECRET_FD_* 变量名未被删除」。
- **C16 / C17** 结构守卫：每一次 `os.set_blocking(fd, False)` 都必须落在带 `finally`
  还原的 `try` 里——行为测试测不出「有人把 finally 窄化回只包读循环」，AST 守卫测得出。
- **C18**「审查须给出 file:line 引用」——上面这张表由测试守着。

表本身由 `test_clause_c18_the_acceptance_table_matches_this_module` 守着：表里多一条、
少一条、指向不存在的符号、行号落到函数外面、或者两个语言版本不一致，都红。

### 负控

把 `49f93be~1` 的 `backend/credentials.py` 换上去，C10 / C11 / C14 / C16 必须红：

```bash
TMP=$(mktemp -d)
git show 49f93be~1:backend/credentials.py > "$TMP/credentials.py"
cat > "$TMP/pre_fix_plugin.py" <<'PY'
import importlib.util, os, sys
_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.py")
_spec = importlib.util.spec_from_file_location("credentials", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules["credentials"] = _module
_spec.loader.exec_module(_module)
PY
cd backend && PYTHONPATH="$TMP" .venv/bin/python -m pytest \
    tests/unit/test_credentials_fd_protocol.py -v -p pre_fix_plugin
```

转录（HEAD = 本节写下的那个提交；节选：pytest 自己展开的 `assert` 比对行与末尾的汇总行
略去，汇总行里带着的是一次运行的条数，属于记录而不是判据）：

```text
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c10_flags_of_a_kqueue_descriptor_are_unchanged
E   AssertionError: the reader changed the flags of a descriptor it does not own: 2 -> 6
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c11_the_pre_fix_shape_changed_those_flags
E   AssertionError: the shape in the tree changes the flags of a descriptor it does not own
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c14_a_failed_f_setfl_still_takes_effect_and_the_source_says_so
E   AssertionError: the source no longer says that a failed F_SETFL still takes effect
FAILED tests/unit/test_credentials_fd_protocol.py::test_clause_c16_every_nonblocking_switch_is_covered_by_a_restore
E   AssertionError: the non-blocking switch is not covered by a restoring finally:
E     line 39 of the function: os.set_blocking(..., False) is not covered by a finally that restores the flags
```

那一次跑下来，红的就是上面这四条；其余条款都是绿的。

C16 的那条断言是结构性的，也是这条守卫存在的理由：修复前的形状里
`os.set_blocking(fd, False)` 与 `os.get_blocking(fd)` 待在同一个**没有** `finally` 的
`try` 里，还原在另一个只包读循环的 `finally` 里——两种形状对「fcntl 能用的描述符」
表现完全一样，只有 kqueue 那种 `fcntl` 会失败的描述符才分得出高下。

### 已知边界

判据说「对 fd 既不 read 也不 close」。这句对**被 FIFO 判定排除掉的**描述符成立
（普通文件：文件偏移一个字节没动），对**空的**活写端管道也成立（读回来的是 EAGAIN，
什么都没消耗，管道的主人照样能写能读）。但对一条**已经带着数据**的外来管道不成立：
「写端还在不在」只能靠读来回答，而从管道里读出来的字节放不回去。所以这种管道会被
读一遍、然后被拒（返回 None / 抛 ValueError），数据被消耗掉。

这是管道的性质，不是这个模块做的选择，所以它被写在这里，而不是留给下一个读判据的
人自己去踩。生产事故走的是另一条路——kqueue 描述符在 `fstat` 看来是 FIFO、在 `fcntl`
看来一塌糊涂，读者在读到任何东西之前就退了。
