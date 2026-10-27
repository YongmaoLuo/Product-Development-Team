# Token convergence — acceptance record

Six hard acceptance conditions decide whether the token-convergence work may
land. This page is their written record: one row per condition, the tests that
decide it, the command that reproduces the verdict, and the verdict that
command produced — once somebody has run it.

The static gate `backend/tests/static_gates/test_token_convergence_acceptance_doc.py`
keeps the record honest: the build fails when the six conditions, their
commands, or the paths they name stop agreeing with the tree, and it fails
when a cell claims a result the page itself says nobody has produced. A
record that quietly drifts is worse than no record, because a reader cannot
tell it apart from a condition that never held.

## The six conditions

| 编号 | 条件 | 证据（测试路径 / 命令） | 结果 |
| --- | --- | --- | --- |
| AC-1 | A1′ judgement — entering verification judges every active verification point exactly once, spends at most one batched model call on the judgement, and keeps a verification point when the judgement raises instead of parsing. | `backend/tests/unit/verification/test_strictness_invariants.py`, `backend/tests/unit/verification/test_topic_relevance.py`, `backend/tests/unit/verification/test_normalization_properties.py` | 0 failed |
| AC-2 | A1′ removal — every removal leaves a complete audit record, a verification point is removed only after its archive succeeded, and an allocated identifier is only ever handed out once. | `backend/tests/integration/verification_chain/test_vp_removal_audit.py`, `backend/tests/unit/test_vp_removal_audit_is_append_only.py`, `backend/tests/static_gates/test_removal_audit_has_no_truncate_path.py`, `backend/tests/integration/verification_chain/test_vp_id_ledger.py`, `backend/tests/integration/verification_chain/test_storage_path_equivalence.py`, `backend/tests/static_gates/test_removal_exemption_wording.py` | 0 failed |
| AC-3 | A2 — a root cause produces exactly one root-cause task and at most one implementation task, a further failure stops the round for a person, and the round budget and the stop reason keep their meaning. | `backend/tests/integration/verification_chain/test_root_cause_task_flow.py`, `backend/tests/unit/verification/test_root_cause_task_build.py`, `backend/tests/integration/verification_chain/test_round_payload_migration.py`, `backend/tests/unit/verification/test_failure_fingerprint.py`, `backend/tests/unit/verification/test_failure_fingerprint_golden.py`, `backend/tests/unit/verification/test_root_cause_decision.py`, `backend/tests/integration/verification_chain/test_runner_outcomes.py`, `backend/tests/integration/verification_chain/test_zero_llm_on_deterministic_path.py` | 0 failed |
| AC-4 | A4 — the scan report can be regenerated and its numbers agree with each other, and a deterministic verification point costs no model session at all. | `backend/tests/e2e/verification/test_scan_and_effect_report_cli.py`, `backend/tests/integration/verification_chain/test_scan_report_artifact.py`, `backend/tests/unit/verification/test_scan.py`, `backend/tests/unit/verification/test_dispatch_table.py`, `backend/tests/e2e/verification/test_deterministic_plan_flow.py`, `backend/tests/integration/verification_chain/test_run_pipeline_order.py` | 0 failed |
| AC-5 | A5 — the context pack carries exactly the allowed fields, every round gets its own file, the brief refers to it by path only, and no credential is in it. | `backend/tests/unit/test_context_pack_schema.py`, `backend/tests/unit/test_context_pack_path_safety.py`, `backend/tests/static_gates/test_brief_references_context_pack.py`, `backend/tests/integration/test_context_pack_reduces_exploration.py` | 0 failed |
| AC-6 | A9 — every verification session carries the verification point and the round that produced it, two verifications running side by side cannot see each other's attribution, and the unattributed share is a number a reader can compute. | `backend/tests/integration/verification_chain/test_attribution_isolation.py`, `backend/tests/unit/test_usage_registry_attribution.py`, `backend/tests/unit/test_plan_usage_report.py`, `backend/tests/e2e/verification/test_effect_report_contract.py`, `backend/tests/unit/verification/test_effect_report_window.py`, `backend/tests/static_gates/test_effect_report_metrics_frozen.py` | 0 failed |

The last column holds the verdict each condition's command produced. A cell
may only carry a result after the command behind it has produced one, so
this record was written with every cell reading `PENDING(operator)` and the
verdicts were written in afterwards, once the commands had run. The run
status further down says which of the two states this record is in, and the
gate next to it fails the moment the two disagree.

A few cases behind these verdicts are **skipped** rather than run: the
observational case in
`backend/tests/integration/test_context_pack_reduces_exploration.py` skips
when the checkout holds no countable session log, and the artefact cases in
`backend/tests/integration/verification_chain/test_scan_report_artifact.py` skip
when the checkout holds no scan report. A skipped case is neither a pass nor
a failure — it is a case that did not run — so it is written down here
rather than left to sit in the output.

The kernel each condition belongs to:

| 编号 | 内核 | 实现 |
| --- | --- | --- |
| AC-1 | 主题相关性两级判定 | `backend/verification/topic_relevance.py` |
| AC-2 | 主题外删除审计与编号账本 | `backend/verification/vp_removal_audit.py` |
| AC-3 | 失败指纹与根因任务 | `backend/verification/failure_fingerprint.py`, `backend/root_cause_task.py` |
| AC-4 | 扫描与派发表 | `backend/verification/scan.py` |
| AC-5 | 每轮只读上下文包 | `backend/context_pack.py` |
| AC-6 | usage 归因（vp_id / round） | `backend/verification_executor.py`, `backend/verification_subagent.py` |

## Commands

Every command below is the one that produces the verdict in the table. Each
names the directory it runs in and the worker count it runs at, because
neither is recoverable from the other: `pytest tests/unit` says nothing about
*where*, and the worker count decides how the suite is split.

### AC-1 — judgement

```bash
cd backend && .venv/bin/python3 -m pytest tests/unit/verification/test_strictness_invariants.py tests/unit/verification/test_topic_relevance.py tests/unit/verification/test_normalization_properties.py -q -n 4
```

### AC-2 — removal

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain/test_vp_removal_audit.py tests/unit/test_vp_removal_audit_is_append_only.py tests/static_gates/test_removal_audit_has_no_truncate_path.py tests/integration/verification_chain/test_vp_id_ledger.py tests/integration/verification_chain/test_storage_path_equivalence.py tests/static_gates/test_removal_exemption_wording.py -q -n 4
```

### AC-3 — root cause

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain/test_root_cause_task_flow.py tests/unit/verification/test_root_cause_task_build.py tests/integration/verification_chain/test_round_payload_migration.py tests/unit/verification/test_failure_fingerprint.py tests/unit/verification/test_failure_fingerprint_golden.py tests/unit/verification/test_root_cause_decision.py tests/integration/verification_chain/test_runner_outcomes.py tests/integration/verification_chain/test_zero_llm_on_deterministic_path.py -q -n 4
```

### AC-4 — scan

```bash
cd backend && .venv/bin/python3 -m pytest tests/e2e/verification/test_scan_and_effect_report_cli.py tests/integration/verification_chain/test_scan_report_artifact.py tests/unit/verification/test_scan.py tests/unit/verification/test_dispatch_table.py tests/e2e/verification/test_deterministic_plan_flow.py tests/integration/verification_chain/test_run_pipeline_order.py -q -n 4
```

### AC-5 — context pack

```bash
cd backend && .venv/bin/python3 -m pytest tests/unit/test_context_pack_schema.py tests/unit/test_context_pack_path_safety.py tests/static_gates/test_brief_references_context_pack.py tests/integration/test_context_pack_reduces_exploration.py -q -n 4
```

### AC-6 — attribution

```bash
cd backend && .venv/bin/python3 -m pytest tests/integration/verification_chain/test_attribution_isolation.py tests/unit/test_usage_registry_attribution.py tests/unit/test_plan_usage_report.py tests/e2e/verification/test_effect_report_contract.py tests/unit/verification/test_effect_report_window.py tests/static_gates/test_effect_report_metrics_frozen.py -q -n 4
```

### The provider half of AC-6 — opt-in, and not part of the verdict above

`backend/tests/e2e/verification/test_real_model_attribution.py` is the half of
AC-6 that proves the attribution row sits behind a real provider round trip
rather than behind a bookkeeping double. It is skipped — not failed — when
the gate is closed, and the nightly lane is the job that runs it with the gate
open. A skipped opt-in lane is not a masked failure, and it is not evidence
either; it is a lane that has not run.

```bash
cd backend && PDT_E2E_REAL_MODEL=1 PDT_E2E_REAL_MODEL_BASE_URL=<provider endpoint> PDT_E2E_REAL_MODEL_API_KEY=<provider key> PDT_E2E_REAL_MODEL_MODEL=<model id> .venv/bin/python3 -m pytest tests/e2e/verification/test_real_model_attribution.py -q -n 1
```

## Whole-gate runs

The six conditions are judged per condition; these are the lanes that judge
the tree as a whole. The worker counts are the ones the pull request gate
uses — the unit lane runs at eight workers, the rest at four.

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

## Merge criteria

Run status: RUN

The three criteria that decide whether this work may land. The first two are
mechanical and are re-runnable from this page; the third is a person's call,
and this page does not make it.

| 判据 | 证据 | 状态 |
| --- | --- | --- |
| 目标测试 0 failed | 上文六条命令，逐条结果写在各自小节 | 0 failed |
| 本地完整 CI 容器 0 failed | 容器门禁：见下方「合入门禁记录」一节，逐个 job 类一行 | 0 failed |
| PR 合入 main | PR 未开；由操作者开 PR，合入由用户决定 | PENDING(operator) |

The run status above is the switch that says whether the gate has been run.
While it reads `PENDING(operator)`, every cell in this record reads the same
way: a verdict that nobody produced is not a verdict, and a record that
claims one is worth less than a record that admits it has not run yet. The
gate has been run, so the status reads `RUN` and the cells below carry what
the commands produced.

### 合入门禁记录

The local container gate is what decides "本地完整 CI 容器 0 failed". It runs
the job classes the pull request gate runs, against the same tree this page
describes. Its result is recorded here so the criterion above is an assertion
rather than a hope.

| job 类 | 结果 |
| --- | --- |
| lint (bandit + `uv lock --check`) | 0 failed |
| grep-guard | 0 failed |
| static-gates | 0 failed |
| commit-hygiene | 0 failed |
| commit-range-privacy | 0 failed |
| pr-metadata-privacy | 0 failed |
| json-cleanup | 0 failed |
| docs build (`mkdocs build --strict`) | 0 failed |
| unit 分片（12 片） | 0 failed |
| root 分片（7 片） | 0 failed |
| misc 分片 | 0 failed |
| coverage-gate（`--fail-under=80`） | 0 failed |
| integration | 0 failed |
| e2e | 0 failed |
| e2e-verification | 0 failed |
| verification-e2e | 0 failed |
| verification-perf | 0 failed |
| time-sensitive + perf | 0 failed |
| negctl | 0 failed |
| e2e-keychain | 0 failed |
| macOS 钥匙串 lane | 不在容器内复刻 |

The macOS keychain lane runs on a macOS runner against a real keychain. The
Linux container cannot reproduce it, so its row says so instead of claiming a
pass it never ran; that lane is a job in `.github/workflows/ci.yml`, and it is
covered there.

One gate has no row above because it does not run in the pull request gate:
the forbidden-name scan, whose needle list is operator-owned and never
committed, runs on the host before a push. It is green on this branch too.

## How this record was filled in

The record was written with every cell reading `PENDING(operator)`: the six
commands under the conditions were run, and the verdict each produced was
written into that condition's result cell. The whole gate was then run
against the same tree, and the run status became `RUN` once it had finished.

Re-running it is the same three steps. Run the command under the condition,
write the verdict it produced into that condition's result cell, and set the
run status back to `PENDING(operator)` before you start. The gate fails
while the run status says the gate has not run and a cell claims a result
anyway, and it fails when a criterion row disagrees with the table behind it
— so the record cannot drift into saying something the commands did not say.

## What this page does not claim

* **It does not measure the effect.** The effect report is a measurement, not
  a gate: a metric that misses its target leaves a verdict in the report and
  still exits zero. AC-6 is about the attribution being *complete and
  computable*, not about the token bill having shrunk. The re-measurement
  window is reported on its own and does not block the merge.
* **It does not run the suite.** This page records conditions, paths, and the
  commands that judge them; the commands produce the verdicts, and the cells
  below are what they produced when they were run. The static gate next to it
  checks that this page is well-formed, that it still points at files that
  exist, and that it claims no result it has not earned — not that the
  verdicts would come out the same way today.
* **It does not cover the macOS keychain lane.** That lane runs on a macOS
  runner against a real keychain and cannot be reproduced inside the Linux
  container; it is a job in `.github/workflows/ci.yml` and it is covered
  there, not here.
