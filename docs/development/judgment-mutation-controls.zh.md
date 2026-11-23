# Tier A 决策表的变异负控

judgment-surface 交接文档的 §12.7 要求每张决策表配一条负控：故意翻转一个分支，
看这张表变红，把模块逐字节放回去，再核对哈希。这一页是这些运行的记录——跑过的
那条命令，以及它看到的东西。

## 为什么留这份记录

一张决策表只有在**能够失败**的时候才值得跑。一张不可能失败的表，不过是一个挂着
`pytest` 装饰器的注释：它在每个提交上都跑，不花什么成本，也什么都钉不住。要诚实地
说明一张表是有分量的，唯一的办法就是故意破坏那个函数，然后看这张表察觉。

探针分三步做这件事，而这个顺序本身就是设计：

1. **变异（Mutate）**——改动被测模块的一行：能翻转该函数结论的最小改动。
2. **运行（Run）**——跑这张表的测试文件，记录它是否变红。
3. **还原（Restore）**——把模块逐字节放回去，并核对摘要。

第 3 步正是第 1 步可以放心对着真实代码树跑的原因。一个把变异后的文件留在原处的探针
改变了它本来要测的东西：下一次运行测的就是变异体，而变异体不是任何人会发布的代码。
所以还原是**被验证过的**——磁盘上的字节与变异前读到的字节逐一比对，摘要也与变异前
取的摘要比对。一个「看起来对」的还原不算还原。

## 九次运行

下面每一行都是在仓库根目录跑的，探针是 `scripts/judgment_mutation_probe.py`。每次
运行都以 `0` 退出，这是探针自己的契约：表变红了**并且**模块被还原了。每次运行都把
工作区恢复成它进来时的样子。

| table | function | mutation | command | RED | sha256 |
| --- | --- | --- | --- | --- | --- |
| `backend/tests/unit/test_decision_table_ran_declared_entry.py` | `_ran_the_declared_entry` | `return False` -> `return True` (backend/verification_evidence.py:976) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 976 --old 'return False' --new 'return True' --test backend/tests/unit/test_decision_table_ran_declared_entry.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_projected_ran_declared_entry.py` | `_projected_ran_the_declared_entry` | `return False` -> `return True` (backend/verification_evidence.py:932) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 932 --old 'return False' --new 'return True' --test backend/tests/unit/test_decision_table_projected_ran_declared_entry.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_apply_subject_check.py` | `apply_subject_check_to_verdict` | `if not reason:` -> `if reason:` (backend/verification_evidence.py:1087) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 1087 --old 'if not reason:' --new 'if reason:' --test backend/tests/unit/test_decision_table_apply_subject_check.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_verdict_subject_mismatch.py` | `verdict_subject_mismatch` | `in bare:` -> `not in bare:` (backend/verification_evidence.py:1047) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 1047 --old 'in bare:' --new 'not in bare:' --test backend/tests/unit/test_decision_table_verdict_subject_mismatch.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_excess_declared_paths.py` | `excess_declared_paths` | `not norm or norm in declared_paths` -> `not norm` (backend/agent.py:1815) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/agent.py --line 1815 --old 'not norm or norm in declared_paths' --new 'not norm' --test backend/tests/unit/test_decision_table_excess_declared_paths.py` | RED | `sha256 ok 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65 == 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65` |
| `backend/tests/unit/test_decision_table_task_declared_files_exist.py` | `_task_declared_files_exist` | `return all(` -> `return any(` (backend/agent.py:10137) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/agent.py --line 10137 --old 'return all(' --new 'return any(' --test backend/tests/unit/test_decision_table_task_declared_files_exist.py` | RED | `sha256 ok 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65 == 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65` |
| `backend/tests/unit/test_decision_table_derive_index_buckets.py` | `derive_index_buckets` | `status, vp_id=vp_id, retired=retired` -> `status, vp_id=vp_id` (backend/verification_buckets.py:210) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_buckets.py --line 210 --old 'status, vp_id=vp_id, retired=retired' --new 'status, vp_id=vp_id' --test backend/tests/unit/test_decision_table_derive_index_buckets.py` | RED | `sha256 ok 2ad23565558dad6f0a0e1d5d4396a18aa7fe07c2d8204af04880804f8ed662ca == 2ad23565558dad6f0a0e1d5d4396a18aa7fe07c2d8204af04880804f8ed662ca` |
| `backend/tests/unit/test_decision_table_gate_block_reason.py` | `_gate_block_reason_for` | `or self._gate_predecessor_reason(gate_order)` -> `and self._gate_predecessor_reason(gate_order)` (backend/verification_executor.py:4421) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_executor.py --line 4421 --old 'or self._gate_predecessor_reason(gate_order)' --new 'and self._gate_predecessor_reason(gate_order)' --test backend/tests/unit/test_decision_table_gate_block_reason.py` | RED | `sha256 ok bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85 == bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85` |
| `backend/tests/unit/test_decision_table_rerun_deferred_gate_layers.py` | `rerun_deferred_gate_layers` | `if reason:` -> `if not reason:` (backend/verification_executor.py:4483) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_executor.py --line 4483 --old 'if reason:' --new 'if not reason:' --test backend/tests/unit/test_decision_table_rerun_deferred_gate_layers.py` | RED | `sha256 ok bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85 == bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85` |

## 每处变异破坏了什么

每一次变异都是把某个函数的答案翻转成相反答案的最小改动——一个被翻转的条件、一个被
取反的分支、一个被删掉的从句。它们都不碰测试文件：察觉的是那张表，而一张只是因为
测试被改过才察觉的表不算表。

- `_ran_the_declared_entry`——一个没有声明任何 entry 的验证点现在声称自己跑过了一个，
  于是它会被豁免掉主题检查。
- `_projected_ran_the_declared_entry`——一个浏览器验证点现在算作 runner 记录，于是散文
  就能换来一份它从未挣得的豁免。
- `apply_subject_check_to_verdict`——一个**确实**在主题上的判定现在会被降级为 FAILED，
  因为「无理由」这道守卫没了。
- `verdict_subject_mismatch`——一个裸文件名现在算作对某个它并不匹配的文件的匹配，于是
  救援分支在错误的地方触发。
- `excess_declared_paths`——一个已声明的路径现在算作未声明的写入，因为豁免从句没了。
- `_task_declared_files_exist`——一个存在的文件现在能替所有文件通过，因为 `all` 变成了
  `any`。
- `derive_index_buckets`——计划里已经退休的一个验证点现在落进某个桶，而不是被丢弃，
  因为退休映射不再被查问了。
- `_gate_block_reason_for`——前置理由现在压过全局阻断理由，因为两者是用 `and` 而不是
  `or` 组合的。
- `rerun_deferred_gate_layers`——一个被阻断的层现在会跑，一个空闲的层现在会等，因为
  「仍然被阻断」的判定被取反了。

## 各列的含义

- **table**——被测量的那张决策表。它就是清单（`docs/judgment_points.json`）为该函数
  指名的表，所以报告与清单不会悄悄地走散。
- **function**——变异所在的判定函数。
- **mutation**——旧文本、新文本，以及它们所在的文件与行号。
- **command**——探针的调用方式，与实际跑的时候写下的完全一致，在仓库根目录执行。
  复制下来就能跑。
- **RED**——该表在变异下变红。写着别的任何内容的行，都是一条没有咬住的负控。
- **sha256**——探针的判决，然后是它在变异前取的摘要和还原后取的摘要。每一行上两者
  都相同：这就是「模块被放回去了」的含义。

## 复现其中一行

从仓库根目录跑表格 `command` 列里的命令。探针会打印它自己的判决：

```
MUTATION PROBE
module: backend/verification_evidence.py
line: 976
old: return False
new: return True
test: backend/tests/unit/test_decision_table_ran_declared_entry.py
RED: yes
RESTORED: yes
SHA256: match
SHA256_BEFORE: 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82
SHA256_AFTER: 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82
PYTEST_EXIT_CODE: 1
TIMED_OUT: no
```

退出码就是契约：`0`——表变红了，并且模块被还原；`1`——表仍然是绿的，所以这张表不
承载分量；`2`——探针跑不起来，或者模块没有被还原。

## 让这一页保持诚实的门禁

`backend/tests/static_gates/test_judgment_mutation_controls_report.py`
读这一页和那份清单，并且不允许它走样：

- 清单里的每一张 Tier A 表在这里恰好有一行，而这一页没有别的行——一份悄悄跳过了
  某张表的报告读起来像一份完整审计；
- 每一行的命令都指名了探针，且它的 `--module` 与 `--test` 就是该行自己的模块与表，
  所以一行不可能指向别人的那次运行；
- 每一行都记录了 `RED` 和两个相等的摘要，所以一次没有咬住的运行、或者一个没被放
  回去的模块，都不能被写成一条通过的负控。

门禁从不运行探针：一个会变异自己所审计模块的门禁，就是在改变它要测的东西。它也
不拿这里记录的摘要去重新哈希那些模块——`backend/agent.py` 每隔几周就变一次，一份
因为无关编辑而变红的报告是一份没人会保留的报告。重新跑探针才是刷新某一行的方式。
