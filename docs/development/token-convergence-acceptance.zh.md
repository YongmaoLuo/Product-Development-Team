# Token 收敛 — 验收记录

六条硬性验收条件决定这批 token 收敛工作能不能落地。这一页是它们的书面记录：每条条件
一行，写清决定它的测试、复现判决的命令，以及那条命令产出的判决——在有人真的跑过之后。

静态门禁 `backend/tests/static_gates/test_token_convergence_acceptance_doc.py` 负责让这份
记录保持诚实：六条条件、它们的命令、它们引用的路径一旦与代码树不再一致，构建就失败；
某一格声称了一个本页自己都说没人跑出来的结果，构建同样失败。一份悄悄走样的记录比没有
记录更糟——读者分不清它和「一条从未成立过的条件」。

## 六条条件

| 编号 | 条件 | 证据（测试路径 / 命令） | 结果 |
| --- | --- | --- | --- |
| AC-1 | A1′ 判定：每次进入 verification 对全部活动 VP 各产生一次判定，批量模型调用次数 ≤1，判定抛异常时一律判保留。 | `backend/tests/unit/verification/test_strictness_invariants.py`、`backend/tests/unit/verification/test_topic_relevance.py`、`backend/tests/unit/verification/test_normalization_properties.py` | PENDING(operator) |
| AC-2 | A1′ 删除：每条删除都有完整审计记录，只有归档成功才移除该 VP，已分配的编号只增不减、已删编号永不复用。 | `backend/tests/integration/verification_chain/test_vp_removal_audit.py`、`backend/tests/unit/test_vp_removal_audit_is_append_only.py`、`backend/tests/static_gates/test_removal_audit_has_no_truncate_path.py`、`backend/tests/integration/verification_chain/test_vp_id_ledger.py`、`backend/tests/integration/verification_chain/test_storage_path_equivalence.py`、`backend/tests/static_gates/test_removal_exemption_wording.py` | PENDING(operator) |
| AC-3 | A2：根因演进时恰生成一个根因任务、至多一个实现任务，再失败则转人工，轮次上限与停止原因的语义保持不变。 | `backend/tests/integration/verification_chain/test_root_cause_task_flow.py`、`backend/tests/unit/verification/test_root_cause_task_build.py`、`backend/tests/integration/verification_chain/test_round_payload_migration.py`、`backend/tests/unit/verification/test_failure_fingerprint.py`、`backend/tests/unit/verification/test_failure_fingerprint_golden.py`、`backend/tests/unit/verification/test_root_cause_decision.py`、`backend/tests/integration/verification_chain/test_runner_outcomes.py`、`backend/tests/integration/verification_chain/test_zero_llm_on_deterministic_path.py` | PENDING(operator) |
| AC-4 | A4：扫描报告可复跑且数字自洽，确定性 VP 执行时不产生任何模型会话。 | `backend/tests/e2e/verification/test_scan_and_effect_report_cli.py`、`backend/tests/integration/verification_chain/test_scan_report_artifact.py`、`backend/tests/unit/verification/test_scan.py`、`backend/tests/unit/verification/test_dispatch_table.py`、`backend/tests/e2e/verification/test_deterministic_plan_flow.py`、`backend/tests/integration/verification_chain/test_run_pipeline_order.py` | PENDING(operator) |
| AC-5 | A5：上下文包字段精确等于白名单，每轮一个新文件，brief 只按路径引用它，包内零凭据。 | `backend/tests/unit/test_context_pack_schema.py`、`backend/tests/unit/test_context_pack_path_safety.py`、`backend/tests/static_gates/test_brief_references_context_pack.py`、`backend/tests/integration/test_context_pack_reduces_exploration.py` | PENDING(operator) |
| AC-6 | A9：验证会话都带 vp_id 与 round，并行跑的两个验证互相看不到对方的归因，未归因占比是一个读者能算出来的数。 | `backend/tests/integration/verification_chain/test_attribution_isolation.py`、`backend/tests/unit/test_usage_registry_attribution.py`、`backend/tests/unit/test_plan_usage_report.py`、`backend/tests/e2e/verification/test_effect_report_contract.py`、`backend/tests/unit/verification/test_effect_report_window.py`、`backend/tests/static_gates/test_effect_report_metrics_frozen.py` | PENDING(operator) |

末列写的是这条条件下面那条命令产出的判决。在那条命令被跑过之前，它写的是
`PENDING(operator)`——一格只有在自己背后的命令真的产出过结果之后，才能写结果。往下
一节的运行状态说明这份记录处在哪一种状态，旁边的门禁在两者对不上的时候立刻失败。

这些判决背后有少数用例是**跳过**而不是跑过的：
`backend/tests/integration/test_context_pack_reduces_exploration.py` 里的观测用例在检出
里没有可数的会话日志时跳过，`backend/tests/integration/verification_chain/test_scan_report_artifact.py`
里的产物用例在检出里没有扫描报告时跳过。跳过既不是通过也不是失败——它是一个没跑的
用例——所以这里把它写出来，而不是让它留在输出里。

各条件对应的内核：

| 编号 | 内核 | 实现 |
| --- | --- | --- |
| AC-1 | 主题相关性两级判定 | `backend/verification/topic_relevance.py` |
| AC-2 | 主题外删除审计与编号账本 | `backend/verification/vp_removal_audit.py` |
| AC-3 | 失败指纹与根因任务 | `backend/verification/failure_fingerprint.py`、`backend/root_cause_task.py` |
| AC-4 | 扫描与派发表 | `backend/verification/scan.py` |
| AC-5 | 每轮只读上下文包 | `backend/context_pack.py` |
| AC-6 | usage 归因（vp_id / round） | `backend/verification_executor.py`、`backend/verification_subagent.py` |

## 命令

下面每条命令就是产出表格里那个判决的命令。每条都写明在哪个目录跑、用几个 worker，
因为这两件事互相推不出来：`pytest tests/unit` 说明不了*在哪里*跑，而 worker 数决定
测试怎么切分。

### AC-1 — 判定

```bash
cd backend && .venv/bin/python3 -m pytest tests/unit/verification/test_strictness_invariants.py tests/unit/verification/test_topic_relevance.py tests/unit/verification/test_normalization_properties.py -q -n 4
```

### AC-2 — 删除

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain/test_vp_removal_audit.py tests/unit/test_vp_removal_audit_is_append_only.py tests/static_gates/test_removal_audit_has_no_truncate_path.py tests/integration/verification_chain/test_vp_id_ledger.py tests/integration/verification_chain/test_storage_path_equivalence.py tests/static_gates/test_removal_exemption_wording.py -q -n 4
```

### AC-3 — 根因

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain/test_root_cause_task_flow.py tests/unit/verification/test_root_cause_task_build.py tests/integration/verification_chain/test_round_payload_migration.py tests/unit/verification/test_failure_fingerprint.py tests/unit/verification/test_failure_fingerprint_golden.py tests/unit/verification/test_root_cause_decision.py tests/integration/verification_chain/test_runner_outcomes.py tests/integration/verification_chain/test_zero_llm_on_deterministic_path.py -q -n 4
```

### AC-4 — 扫描

```bash
cd backend && .venv/bin/python3 -m pytest tests/e2e/verification/test_scan_and_effect_report_cli.py tests/integration/verification_chain/test_scan_report_artifact.py tests/unit/verification/test_scan.py tests/unit/verification/test_dispatch_table.py tests/e2e/verification/test_deterministic_plan_flow.py tests/integration/verification_chain/test_run_pipeline_order.py -q -n 4
```

### AC-5 — 上下文包

```bash
cd backend && .venv/bin/python3 -m pytest tests/unit/test_context_pack_schema.py tests/unit/test_context_pack_path_safety.py tests/static_gates/test_brief_references_context_pack.py tests/integration/test_context_pack_reduces_exploration.py -q -n 4
```

### AC-6 — 归因

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain/test_attribution_isolation.py tests/unit/test_usage_registry_attribution.py tests/unit/test_plan_usage_report.py tests/e2e/verification/test_effect_report_contract.py tests/unit/verification/test_effect_report_window.py tests/static_gates/test_effect_report_metrics_frozen.py -q -n 4
```

### AC-6 的模型侧一半 — 需要显式打开，不计入上面的判决

`backend/tests/e2e/verification/test_real_model_attribution.py` 是 AC-6 的另一半：它证明
归因那一行是挂在一次真实的模型往返之后，而不是挂在一个记账替身之后。门禁关闭时它
是**跳过**而不是失败；打开门禁跑它的是夜间 lane。跳过的可选 lane 既不是被掩盖的失败，
也不是证据——它就是没跑。

```bash
cd backend && PDT_E2E_REAL_MODEL=1 PDT_E2E_REAL_MODEL_BASE_URL=<provider endpoint> PDT_E2E_REAL_MODEL_API_KEY=<provider key> PDT_E2E_REAL_MODEL_MODEL=<model id> .venv/bin/python3 -m pytest tests/e2e/verification/test_real_model_attribution.py -q -n 1
```

## 全量门禁

六条条件是逐条判的；下面这几条判的是整棵树。worker 数与拉取请求门禁一致——单元
lane 跑 8 个 worker，其余跑 4 个。

```bash
cd backend && .venv/bin/python3 -m pytest tests/unit -q -n 8
```

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain -q -n 4
```

```bash
cd backend && .venv/bin/python3 -m pytest tests/static_gates -q -n 4
```

```bash
cd backend && .venv/bin/python3 -m pytest tests/e2e/verification -q -n 4
```

## 合入门禁

运行状态：PENDING(operator)

决定这批工作能不能落地的是三条判据。前两条是机械的，可以从这一页重跑；第三条是人的
判断，这一页不替他做。

| 判据 | 证据 | 状态 |
| --- | --- | --- |
| 目标测试 0 failed | 上文六条命令，逐条结果写在各自小节 | PENDING(operator) |
| 本地完整 CI 容器 0 failed | 容器门禁：见下方「合入门禁记录」一节，逐个 job 类一行 | PENDING(operator) |
| PR 合入 main | 由操作者开 PR，合入由用户决定 | PENDING(operator) |

上面那行运行状态就是「门禁跑没跑过」的开关。它写着 `PENDING(operator)` 的时候，这一页
的每一格也都这么写：没人产出过的结果不叫结果，一份声称有结果的记录比一份承认自己还没
跑的记录更不值钱。门禁跑过之后，运行状态改成 `RUN`，下面各格才写命令真正产出的东西。

### 合入门禁记录

「本地完整 CI 容器 0 failed」由本地容器门禁决定。它跑的是拉取请求门禁跑的那些 job 类，
对象就是这一页描述的同一棵树。结果记在这里，上面那条判据才是一条断言而不是一句愿望。

| job 类 | 结果 |
| --- | --- |
| lint (bandit + `uv lock --check`) | PENDING(operator) |
| grep-guard | PENDING(operator) |
| static-gates | PENDING(operator) |
| commit-hygiene | PENDING(operator) |
| commit-range-privacy | PENDING(operator) |
| pr-metadata-privacy | PENDING(operator) |
| json-cleanup | PENDING(operator) |
| docs build (`mkdocs build --strict`) | PENDING(operator) |
| unit 分片（12 片） | PENDING(operator) |
| root 分片（7 片） | PENDING(operator) |
| misc 分片 | PENDING(operator) |
| coverage-gate（`--fail-under=80`） | PENDING(operator) |
| integration | PENDING(operator) |
| e2e | PENDING(operator) |
| e2e-verification | PENDING(operator) |
| verification-e2e | PENDING(operator) |
| verification-perf | PENDING(operator) |
| time-sensitive + perf | PENDING(operator) |
| negctl | PENDING(operator) |
| e2e-keychain | PENDING(operator) |
| macOS 钥匙串 lane | 不在容器内复刻 |

macOS 钥匙串那条 lane 跑在 macOS runner 上、对的是真钥匙串。Linux 容器复刻不了它，
所以它那一行写明复刻不了，而不是声称一个它从未跑出来的通过；那条 lane 是
`.github/workflows/ci.yml` 里的一个 job，在那里覆盖。

## 怎么回填

跑完某条条件下面的命令，把它产出的判决写进那一行的结果格；整个门禁跑完之后，把运行
状态改成 `RUN`。门禁在「运行状态还说没跑、某一格却声称有结果」的时候失败，也在判据行
和它背后的表对不上的时候失败——所以这份记录没法漂移成一句命令没有说过的话。

## 这一页不声称什么

* **它不度量效果。** 效果报告是一次测量，不是一道门禁：指标没达标，报告里留一条判决，
  进程照样以零退出。AC-6 说的是归因*完整且可计算*，不是 token 账单变小了。复测窗口
  单独出报告，不阻断合入。
* **它不跑测试。** 这一页记录条件、路径和判决它们的命令；命令才产出判决，在命令被跑过
  之前各格写的是 `PENDING(operator)`。旁边的静态门禁检查的是这一页格式正确、引用的文件
  还在、且没有声称一个它没挣到的结果——不是那些判决今天重跑还是同样结果。
* **它不覆盖 macOS 钥匙串 lane。** 那条 lane 跑在 macOS runner 上、对的是真钥匙串，
  在 Linux 容器里复刻不了；它是 `.github/workflows/ci.yml` 里的一个 job，在那里覆盖，
  不在这里。
