# Security Audit

This document is the authoritative deliverable of the codebase's
security sweep. Every concrete finding lands here as a `### Finding
ENTRY-NNN` block, and the "blocker fixed" acceptance bullet for each
finding is checked against an entry in this file. The structure of
each entry, the five surface sections that organise them, and the
severity tiers are pinned by the meta-test
`backend/tests/meta_tests/test_security_audit_schema.py`.

The five surface sections (`入口面`, `路由与执行面`, `前端调用面`,
`配置与样例面`, `工程面`) are the *only* recognised surface names; an
entry that lands under any other heading will be invisible to the
audit tooling.

## Tiers (severity)

Entries are tagged with one of three severity tiers, ordered most → least
severe. The contract is pinned by `TIERS` in the meta-test; using any
other label is a schema violation.

* **blocker** — the finding must be remediated before any release
  candidate is cut. Acceptance bullet: the verification command exits 0
  against the current tree. Unremediated blockers ship = red.
* **should-fix** — the finding must be remediated before the next minor
  release. Acceptance bullet: the verification command exits 0, or a
  written waiver names the compensating control.
* **note** — informational; the finding documents a hardening
  opportunity or a defensive measure already in place. No acceptance
  bullet; tracked for the next sweep.

## Entry template

Every finding uses the same five-element shape. The parser
(`parse_entries` in the meta-test) checks each element; omitting any
of them marks the entry `valid=False` and the doc fails the schema
gate at PR time.

The heading below uses `Entry Template` (no `Finding` prefix and no
`ENTRY-NNN` id) so the parser does not mistake the template itself
for a real finding.

```markdown
### Finding ENTRY.NNN   ← template id (dot instead of dash)
档位: blocker | should-fix | note
问题: ...
影响: ...
攻击路径: 前置条件 ...；触发步骤 ...；可观测后果 ...
修复: ...
验证方式:
\`\`\`bash
backend/.venv/bin/python3 -m pytest <test-file-or-suite> -v
\`\`\`
```

> **Note on the template id.** The parser requires `ENTRY-NNN`
> (with a dash) for it to count as a real finding, so the dot in
> `ENTRY.NNN` above is what keeps the template from being parsed as
> a sixth entry. A real finding replaces the dot with a dash.

> The fenced block above is illustrative — copy it as the start of
> every new entry, then replace each placeholder (`...`,
> `<test-file-or-suite>`, the id) with concrete text. The parser
> will reject the entry if any element is left as a placeholder,
> because placeholder text such as `...` does not contain the
> required attack-path sub-section tokens.

The `档位:` line must be exactly one of the three tokens above. The
`攻击路径:` paragraph must contain all three of `前置条件`,
`触发步骤`, `可观测后果` (in any order, separated by `；`). The
`验证方式:` block must be a fenced `bash` code block — this is the
only machine-checkable link between a finding and its acceptance
bullet.

### 缺陷，不是事故

`问题:` / `影响:` / `攻击路径:` 描述的是**任何人读代码都能复现的东西** ——
哪段代码有什么性质、怎么被触发、危害是什么。**只在本机发生过的事实不属于本
文件**：计数、时长、暴露窗口的起止、部署方的路径或身份。

理由不是「事故更严重」，而是**事故不可从代码复现**。缺陷公开是零成本的 ——
读者自己就能得出同样的结论；事故公开则交出一个精确的时间窗和一条可操作的线
索，换不到任何可信度。

同一条线还排除第二类内容：**关于排查过程本身的记账**。带日期的墙钟基线、
「查过哪些格、每一格都是空的」的覆盖网格、逐条 grep 及其处置的表格 —— 它们
不是在说软件，而其中否定结果的那一半更是在告诉读者**哪里没人找到过东西**。
这类内容属于操作者本机的排查记录，不随本文件公开。判据还是同一条：读者
**能不能自己推出来**。

这条规则的背景值得知道：本文件的两个条目曾经都带着事故叙事，而且**写的时候
完全合理** —— 给出具体计数和日期，读起来是*证据*，而证据感觉像是应该写进去的
东西。所以它需要一条规则，而不是一次校对。

门禁的第一版正是从这里长出来的，也因此**只覆盖了当时手上那两个形状** ——
采集日期、套件基线数字、否定结果单元格，它一条都拦不下。按例子推广出来的规则，
覆盖的就是那些例子。现在它按**类别**写：第一人称测量动词、计数 + 制品名词、
**采集日期**、**套件基线词汇**、**否定结果单元格**，以及上面那几节的标题不得
回到本文件。边界仍然写在
`backend/tests/static_gates/test_public_security_docs_describe_defects_not_incidents.py`
的 docstring 的 "What this gate can and cannot see" 一节 —— 读它的绿灯要理解为
「不含这些形状」，**不是**「这份文档里没有事故叙事」。

---

## 入口面

The entry-surface audit covers how a request first reaches the
process: bind address, port, TLS posture, the request-guard layer, and
how the loopback server distinguishes its own UI from a browser page
the operator happens to have visited.

A request guard on the `X-PDT-Request` header — combined with an
`Origin` / `Host` allowlist — is what stops a sandboxed `file://`
page or a DNS-rebinding probe from issuing `/api/*` calls. The
guard, the loopback bind, and the static-asset exemption are the
three pieces that together make "this server is only reachable from
the local machine" true in practice rather than merely as a README
sentence.

### Finding ENTRY-001
档位: blocker
问题: Cross-origin browser requests can issue `/api/*` calls because the loopback bind was treated as a security boundary.
影响: A page the operator visits can enumerate plan ids (`GET /api/plans`) and trigger an agent (`POST /api/execution/<id>/start`), which writes files and runs shell commands in the target directory.
攻击路径: 前置条件 — operator visits a hostile page in the same browser that loaded the bundled UI; 触发步骤 — the page issues `fetch('http://127.0.0.1:<port>/api/...', {method: 'POST', body: JSON.stringify(...)})` with `Content-Type: text/plain` so no preflight fires; 可观测后果 — the plan list is exfiltrable and a process is spawned against any directory the operator can name.
修复: A `request_guard.RequestGuard` middleware requires `X-PDT-Request: 1` on every `/api/*` request and rejects requests whose `Origin` / `Host` is not loopback.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/security/test_request_guard.py -v
```

### Finding ENTRY-006
档位: should-fix
问题: The loopback request-guard's literal-value / host / origin / path variants were pinned only at the integration layer, so a refactor of `rejection_reason()` could silently regress boundary cases (`"1 "` / `"01"` / `"true"` header values, `Host: ""` / `Host: [::1]:8000`, `Origin: null`, non-`/api/` paths) that the suite's `TestClient` wrapper masks with its injected header and `testserver` host allowlist.
影响: A regression in any of these branches re-opens the cross-origin browser hole that ENTRY-001 closed (for the value-variant and `Origin: null` cases) or breaks the loopback UI's own navigation (for the `Host: [::1]:8000` and non-`/api/` cases); either way the integration suite still passes, so the regression reaches production under a green CI badge.
攻击路径: 前置条件 — `request_guard.rejection_reason()` is the sole gate; the test shim's header injection and host allowlist are off; 触发步骤 — a refactor changes the header-value comparison (e.g. `if not header` instead of `!=`), the host splitter (e.g. `split(":")` without bracket handling on `[::1]:8000`), or the origin parser (e.g. `if origin_host` instead of `in allowed`, which lets an empty hostname through); 可观测后果 — `TestClient` clients keep sending `X-PDT-Request: 1` and `Host: testserver`, so neither `test_request_guard.py` nor any other integration test ever feeds the malformed inputs to the guard and CI stays green.
修复: A unit-level rejection matrix in `backend/tests/security/test_request_guard_rejections.py` constructs every request through a real `fastapi.Request(scope)` and asserts each branch directly against `rejection_reason()`, bypassing the `TestClient` shim entirely.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/security/test_request_guard_rejections.py -v
```

---

## 路由与执行面

The routing-and-execution surface covers every route under `routes/`
and every code path that spawns a sub-agent or touches the local
filesystem from a request handler. The audit here focuses on (a)
input validation for paths the executor will write into, (b)
archive-state writes that must short-circuit with 410 Gone, and (c)
the cross-consumer state-machine guarantees that prevent one route
from corrupting another's row.

### Finding ENTRY-002
档位: should-fix
问题: The execution route accepts arbitrary `project_dir` strings without rejecting the live backend checkout.
影响: A request that names the running server's own source tree makes the executor write into the directory that is being served, risking self-inflicted corruption.
攻击路径: 前置条件 — the request-guard header is present (so this is an internal-only attack surface); 触发步骤 — caller POSTs `/api/execution/<id>/start` with `{"project_dir": "<server-root>"}`; 可观测后果 — the executor imports modules from the in-flight tree and may rewrite files under load.
修复: The execution-start handler refuses any `project_dir` that resolves to the project root or to `backend/`, returning 422 with a structured error.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/security/test_plan_id_containment.py -v
```

### Finding ENTRY-015
档位: should-fix
问题: `framework.ids.validate_plan_id` accepted plan ids of any length that still passed the byte-set guard, so a 300-character id composed entirely of `[A-Za-z0-9._-]` sailed through and was joined onto the plans root by `_plan_dir` as a 300-byte leaf name.
影响: A request that smuggles a 300-char id lands in `_plan_dir` (a 300-byte leaf, silently truncated on ext4 / NTFS / APFS past ~255 bytes) and then 404s — the URL the operator typed no longer matches the directory that was created. Worse, every guard downstream (filesystem ACLs, log readers, Feishu card renderers) is now keyed off a different string than the one the URL carried.
攻击路径: 前置条件 — the request-guard header is present (same as ENTRY-002); 触发步骤 — caller issues `GET /api/plan/<300-ascii-letters>/status`; 可观测后果 — the validator returns the id unchanged, `_plan_dir` joins it onto `PLANS_DIR`, the route 404s because no such directory exists, but the leaf-name divergence is now baked into every downstream consumer.
修复: A `MAX_PLAN_ID_LEN = 64` constant lives in `framework/ids.py`; `validate_plan_id` rejects any id longer than that before the absolute-path / separator / byte-set checks run. The cap is well above `derive_plan_id`'s `YYYYMMDD-slug(<=20)` shape (29 chars) so no legitimate id is tightened.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/security/test_plan_id_boundary_matrix.py -v
```

---

## 前端调用面

The frontend-call surface covers every `fetch` issued by the bundled
UI: how it carries the request-guard header, how it handles
authentication failures, and how it surfaces errors that the
backend flagged as user-actionable.

The rule is that a new `fetch` in `frontend/` must go through an
existing wrapper (`frontend/api.js`, or `app.js`'s `api()`), which
sends the header. A bare `fetch` will 403 because the request guard
won't see `X-PDT-Request: 1`. The suite satisfies the guard rather
than bypassing it — `backend/tests/conftest.py` injects the header
into every `TestClient` and registers `testserver` as an allowed
host.

### Finding ENTRY-003
档位: note
问题: Several frontend modules issue `fetch` calls inline rather than going through the shared `api()` wrapper.
影响: A new endpoint that requires the request-guard header will return 403 when called from these modules until the call is migrated.
攻击路径: 前置条件 — the request-guard is in force; 触发步骤 — a frontend module issues `fetch('/api/<endpoint>')` without the header; 可观测后果 — the call fails with 403 and the UI shows a generic error.
修复: Migrate inline `fetch` calls to `frontend/api.js` so the header is attached uniformly.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/security/test_request_guard_rejections.py -v
```

---

## 配置与样例面

The configuration-and-examples surface covers per-deployment config
files (`provider_capacity.yaml`, `provider_routing.yaml`), the
example files under `example/`, and any test fixture that ships in
the repo. The rule is that per-installation settings — which
providers a deployment uses, how many concurrent agents each
tolerates, which local ports its helper applications listen on —
live in these config files and are read at runtime, never hardcoded
in source.

A hardcoded ceiling duplicates what the caps already determine and
has to be re-asserted every time a provider is added or retired. The
schema gate that scans for hardcoded provider lists is the
acceptance bullet for this surface.

### Finding ENTRY-004
档位: note
问题: A test fixture inlined a list of provider names instead of pointing at the runtime config.
影响: Adding a new provider requires editing source, not just config; the test fixture drifts from the operator's real deployment.
攻击路径: 前置条件 — none (the fixture is loaded only under test); 触发步骤 — a CI run executes the fixture's test; 可观测后果 — the test may assert against provider names the operator has not enabled, masking regressions.
修复: The fixture reads from the operator-configured `provider_routing.yaml` and falls back to the example file when unset.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/test_provider_order_no_server_import.py -v
```

### Finding ENTRY-016
档位: note
问题: The `example/provider_routing.yaml.example` and `example/provider_capacity.yaml.example` templates could be edited to carry a real provider name (e.g. a copy-paste from one install's live config), and the live `.config/*.yaml` files could stop being gitignored, without the audit sweep catching either defect. A leaked deployment name in the template would compile one install's provider set into every checkout, and a leaked live config would publish the operator's real routing on the next export. The leaked content is not a credential so no secret scanner would flag it.
影响: One deployment's providers reach every install that copies the example, or one operator's live routing ships in the public repository — the same shape that the first export carried ~340 times before the attribution gate was added.
攻击路径: 前置条件 — a contributor edits `example/*.example` or `.gitignore`; 触发步骤 — the new content enters the repo without the audit sweep rejecting the leak; 可观测后果 — readers see real provider names in the public template, or the live operator config ships in source control; a subsequent private export picks the same shape up under different names.
修复: A static gate (`backend/tests/unit/test_config_placeholders_are_neutral.py`) pins three layers: patterns start with `^Example `; no overlap with live `tiers`; `git check-ignore` exits 0.

详细论证: The live `tiers` list is read from `.config/provider_routing.yaml` at runtime, with the intersection check skipped when `.config/` is absent. The intersection is computed against the live list — no provider name is hardcoded in the gate, so adding a new provider does not require updating the test.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/unit/test_config_placeholders_are_neutral.py -v
```

Three grep commands document the current state of the configuration surface
(command → expected hit → disposition conclusion):

```bash
# 1. .config/ must be matched by .gitignore so a plain `git add -A` cannot
#    commit the live operator config into the public repository.
git check-ignore -v .config/provider_routing.yaml
# expected:  exit code 0; stdout ".gitignore:86:.config/  .config/provider_routing.yaml"
# hit:       ".gitignore:86:.config/	.config/provider_routing.yaml"
# disposition: PASS — live config is excluded from version control.

# 2. every `pattern:` field in the capacity template must start with the
#    placeholder prefix; a deployment name here would compile one install
#    into every checkout that copies the template.
grep -nE '^[[:space:]]*-[[:space:]]*pattern:' example/provider_capacity.yaml.example \
    | grep -v '\^Example '
# expected:  no matches (all 3 `pattern:` lines in the file start with `^Example `)
# hit:       (no offenders)
# disposition: PASS — capacity template carries placeholders only.

# 3. every `- "..."` tier-list entry in the routing template must start with
#    the placeholder prefix; same rationale as (2).
grep -nE '^[[:space:]]*-[[:space:]]*"' example/provider_routing.yaml.example \
    | grep -v '"\^Example '
# expected:  no matches (all 3 tier entries start with `^Example `)
# hit:       (no offenders)
# disposition: PASS — routing template carries placeholders only.

# 4. `.config/` must hold no tracked file. Rule (1) confirms the
#    directory is gitignored; this one confirms the directory has
#    *also* never been added — a stray `git add -f`, an early export
#    before the ignore rule shipped, or a manual re-track would all
#    bypass (1) while still landing a real config in source.
git ls-files .config/
# expected:  no matches (empty stdout; rule holds on a fresh clone
#            too, where `.config/` is absent)
# hit:       (no offenders)
# disposition: PASS — no live operator config has leaked into the
#            tracked tree.
```

---

## 工程面

The engineering surface covers the build, the test harness, the
release process, and the developer-machine hygiene rules that keep
private data out of the public export. The rule is that a comment
may say what the code does and why; it may not quote the operator
or name a sibling checkout that exists on a developer's local
machine.

The grep guard under `backend/tests/grep_guard.py` walks the tree
for `/Users/<name>/...`, `/home/<name>/...`, and a configurable list
of local-repo names, and fails the PR if any of them appear in
checked-in source. This is the acceptance bullet for the surface.

### Finding ENTRY-005
档位: should-fix
问题: A historical comment quoted an operator's verbatim remark and named a sibling repo.
影响: The public export carried ~340 references to unrelated private projects and their checkouts into `backend/`; future exports may carry the same shape under different names.
攻击路径: 前置条件 — none (this is an export hygiene issue, not a runtime vulnerability); 触发步骤 — the repo is exported without redaction; 可观测后果 — readers see names of private projects that have no business in a public codebase.
修复: Restate the *decision* in the third person and keep the date; never quote the operator; never name a sibling checkout.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/test_grep_guard.py -v
```

### Finding ENTRY-007
档位: should-fix
问题: The CI workflow at `.github/workflows/ci.yml` could be edited until it no longer parses as YAML or until its top-level `jobs:` block disappears, and the audit sweep would still report green because every other gate still passes in isolation.
影响: A workflow that does not parse does not run, so the audit's "all green" badge becomes a lie — the layer that was supposed to enforce the rest of the contract never executes.
攻击路径: 前置条件 — a contributor edits `ci.yml` and introduces a structural mistake (unmatched bracket, stray tab, deleted `jobs:` block); 触发步骤 — the change is pushed to main; 可观测后果 — GitHub Actions refuses to schedule any job at the workflow level; the local string-level tests still match the file's literal text so they pass.
修复: An integration test (`test_ci_definition_parses` in `backend/tests/integration/test_ci_gate_reuse_contract.py`) loads `ci.yml` through `yaml.safe_load` and asserts that the parsed mapping carries a non-empty `jobs:` block. A parse error surfaces as `pytest.fail()` with the parser traceback.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/integration/test_ci_gate_reuse_contract.py::test_ci_definition_parses -v
```

### Finding ENTRY-008
档位: should-fix
问题: The CI install steps (`pip install -r backend/requirements.txt`) could be narrowed (e.g. to `pip install .`) and the local string-level grep would still find a `pip install` substring, masking the regression.
影响: A narrowed install drops the dev / test extras that `requirements.txt` pins, so pytest collection fails silently on the runner under a green CI badge.
攻击路径: 前置条件 — a contributor tightens the install to drop an extra; 触发步骤 — the change is pushed to main; 可观测后果 — the runner's pytest collection reports `ModuleNotFoundError` for a transitive dep; the other gates (lint, grep-guard) still pass because they do not depend on the dropped extras.
修复: An integration test (`test_install_steps_are_unchanged` in the same file) asserts the literal substring `pip install -r backend/requirements.txt` is present in the workflow text — a substring-only grep on `pip install` is not enough; the exact requirements-file form must stay.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/integration/test_ci_gate_reuse_contract.py::test_install_steps_are_unchanged -v
```

### Finding ENTRY-009
档位: should-fix
问题: The CI `grep-guard` job's shared wrapper invocation (`bash ../scripts/grep_guard.sh`) could be replaced with an inline `python3 -c` invocation or removed entirely, and the audit would only notice when a forbidden filename pattern already slipped in.
影响: An inlined invocation silently diverges from the same wrapper that `.pre-commit-config.yaml` invokes — the two scanners will drift the next time the wrapper's flag set changes; a removed job closes the CI gate while leaving the local hook untouched.
攻击路径: 前置条件 — a contributor inlines the scanner to "make the step easier to read" or drops the job as "redundant with the pre-commit hook"; 触发步骤 — the change is pushed to main; 可观测后果 — CI no longer sweeps the production tree for the forbidden filename patterns; the local hook still does, so the divergence only shows up when a contributor skips the local hook.
修复: An integration test (`test_grep_guard_step_still_present` in the same file) asserts the literal substring `bash ../scripts/grep_guard.sh` is present in the workflow text. The shared wrapper is the single source of truth consumed by both CI and pre-commit; the substring check pins that contract from the audit side.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/integration/test_ci_gate_reuse_contract.py::test_grep_guard_step_still_present -v
```

### Finding ENTRY-010
档位: blocker
问题: A bare `python3 -m pytest` invocation could land in `ci.yml` (the ubuntu-latest runner ships with system Python whose stale urllib3 + missing deps break pytest collection silently) and the audit would not catch it until a 45-minute CI run reported "0 tests collected".
影响: A bare system-python pytest invocation reports success without having collected anything, so every gate that depends on pytest coverage is green by construction rather than by content.
攻击路径: 前置条件 — a contributor copies a local recipe that omits the venv prefix; 触发步骤 — the change is pushed to main; 可观测后果 — pytest collection fails silently with `ModuleNotFoundError` for a transitive dep, the runner reports "all tests passed" because nothing was collected, and the layered gates downstream (coverage-gate, integration, e2e) all skip.
修复: An integration test (`test_every_pytest_invocation_uses_project_venv` in the same file) walks every `-m pytest` line in the workflow and asserts the literal substring `.venv/bin/python3` appears. The CLAUDE.md rule "never use system Python to run pytest" is pinned at the audit level rather than relying on reviewer memory.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/integration/test_ci_gate_reuse_contract.py::test_every_pytest_invocation_uses_project_venv -v
```

### Finding ENTRY-011
档位: should-fix
问题: The CI e2e job could be excluded from a push to main (via `if: github.event_name == 'pull_request'`) while keeping the e2e marker in its shell line, and the audit would still see the substring match — the marker is present, but the job is unreachable on the trigger path it is supposed to protect.
影响: This repo's workflow is a local merge into main followed by a direct push (no PRs), so a push-only exclusion means the e2e layer never runs and the audit's "all green" badge reports success without having exercised the e2e pipeline.
攻击路径: 前置条件 — a contributor copies an upstream example that gates the e2e job to PR-only; 触发步骤 — the change is pushed to main; 可观测后果 — the e2e job's own `if` predicate may still allow the push, but if any job in its `needs:` chain is PR-only, GitHub skips the chain and the e2e layer reports as skipped while the workflow returns success.
修复: An integration test (`test_e2e_job_runs_on_main_push` in the same file) finds the top-level job whose steps invoke `-m e2e` and walks its `needs:` graph; every job in the graph must NOT be excluded from a push to main (`github.event_name == 'push' && github.ref == 'refs/heads/main'` must appear in each job's `if:` predicate).
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/integration/test_ci_gate_reuse_contract.py::test_e2e_job_runs_on_main_push -v
```

### Finding ENTRY-012
档位: blocker
问题: `scripts/run_tests.sh` declared only `set -e` (no `-u`, no `-o pipefail`), so an unset variable inside the script would silently expand to an empty string and a failed command inside a pipeline would not fail the script — the "all green" badge would still report success while regressions slip past.
影响: A shell that exits 0 on partial failure means CI's `bash scripts/run_tests.sh` step can never fail on the most common automation defects (typo'd variable names, `grep foo | wc -l` masking grep's non-zero exit). Reviewers cannot rely on a green `set -e` line meaning what they think it means.
攻击路径: 前置条件 — a contributor tightens or copies a script in `scripts/`; 触发步骤 — the new script keeps (or drops) the `set -e` prefix but skips `-u` / `-o pipefail`; 可观测后果 — the script proceeds past the failure (silent unset expansion or hidden pipe-mask); the CI step exits 0, and downstream gates reading the script's exit code never see the regression.
修复: A gate (`test_no_dangerous_defaults_in_scripts` in `backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py`) fires on `-u` / `pipefail` missing; fix lifts `scripts/run_tests.sh`

详细论证: Walks every `.sh` under `scripts/`, fires on `set -e` missing `-u` or `-o pipefail`. Paired with the one-line shell-options change.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py::test_no_dangerous_defaults_in_scripts -v
```

### Finding ENTRY-013
档位: blocker
问题: `scripts/run_tests.sh` defaulted to `pytest "$PROJECT_ROOT/tests"` when invoked with no arguments, but the project-root `tests/` directory currently contains only `fixtures/` — running with no args collects 0 tests and exits 0 unconditionally, so a "green CI badge" based on this script's exit code would always be a lie.
影响: The default no-args invocation silently passes any local or CI run that has not been re-pointed at `backend/tests`; a developer invoking `bash scripts/run_tests.sh` from the repo root sees pytest "collect nothing / pass" every time, masking real test failures until the next engineer consciously passes a path.
攻击路径: 前置条件 — the script is invoked with no arguments (the documented default); 触发步骤 — the `if [ $# -eq 0 ]` branch picks `$PROJECT_ROOT/tests`, which only carries `fixtures/`; 可观测后果 — pytest's summary line reports `0 collected` and the shell exits 0; the audit's "all green" badge reports success without having exercised any test under `backend/tests/`.
修复: A targeted static check (`test_run_tests_default_target_is_not_empty` in the same file) parses the `if [ $# -eq 0 ]; then pytest ...` branch and asserts the argument is NOT the literal `$PROJECT_ROOT/tests`. The minimal fix in `scripts/run_tests.sh` retargets the default at `$PROJECT_ROOT/backend/tests`, where the real suite lives.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py::test_run_tests_default_target_is_not_empty -v
```

### Finding ENTRY-014
档位: note
问题: Scripts in `scripts/` accept operator-supplied paths without validating them against the project layout — e.g. `scripts/write_nightly_results.py --results-path PATH` and `--from-json PATH` write / read whatever path the operator names; scanner `--file PATH` arguments likewise point at arbitrary filesystem locations.
影响: An operator that types a path that resolves outside the project (or against a sensitive sibling tree) gets a result file written where they did not intend, with no warning. The risk is operator-only (no untrusted input reaches these scripts), so the gate stops at documenting the shape rather than blocking it.
攻击路径: 前置条件 — the operator has shell access; 触发步骤 — a mistyped argument makes the script write to a sensitive system path (e.g. an absolute path under the operator's shell namespace); 可观测后果 — the artifact lands or is read from the wrong place; CI sees a passing summary because the script's own checks passed.
修复: A note-only entry. The audit documents the shape; no static gate is added because the input is operator-supplied and the projects-root resolution the brief forbids (per CLAUDE.md) would itself be a safer alternative. The contract is owned by review, not by an automatic gate.
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py::test_unquoted_expansion_is_detected -v
```

### Finding ENTRY-017
档位: blocker
问题: 子 agent 的 `--settings` 载荷在 `env` 块里带着**路由后 provider 的 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`**，而它被写到扁平的 `/tmp/subagent_settings_<uuid>.json`，用默认文件模式（`0644`），并且**从不清理**。两个写入口都是这样：`backend/subagent_config.py:write_tmp_settings` 与 `backend/coding_tool.py` 的派发写入。`/tmp` 是 `1777` —— 粘滞位只阻止别的本地账户**删除**这些文件，不阻止**读取**；`0644` 的文件对机器上任何账户可读。文件名也不难获得：`claude --settings <path>` 把它放进了进程表，同一个路径还作为 `CLAUDE_SETTINGS_PATH` 交给了子 agent 自己。该模式产生的文件没有任何路径删除，所以暴露面只增不减：每一次派发都再留一份。
影响: 任何本机账户无需任何权限提升即可读取全部存量：`ls /tmp/subagent_settings_*.json` 或 `ps aux | grep -- --settings` 找到名字，再 `cat` 出 live 的 provider 凭据，可用其消耗被泄露账户的额度。文件里还有 `PDT_FORMAL_REPO_PATH`（操作者用户名与仓库路径）。因为从不清理，泄露面只增不减：每一次派发都再留一份。本轮审计此前只覆盖了源码字面量与 API 响应两条泄露面，**运行时落盘物**这一整类没有进入覆盖面。
攻击路径: 前置条件 — 攻击者在本机拥有任意一个账户（或 `/tmp` 被快照/备份/容器共享）；触发步骤 — 任一子 agent 派发写出 settings 文件后，攻击者枚举 `/tmp/subagent_settings_*.json` 并读取 `env.ANTHROPIC_API_KEY`；可观测后果 — 拿到长期有效的 provider 密钥，可在不接触本机的情况下以其名义调用 API。
修复: 新增 `backend/utils/secret_files.py`，把三层独立防线集中在一处 —— `private_dir()` 用 `mkdtemp` 建 `0700` 目录（Linux 上 `gettempdir()` 就是 `/tmp`，只有该目录本身私有；macOS 上是在已有的 per-user 私有临时目录之上再加一层）；`write_private_json()` 显式设置并强制 `0600`（`mkdtemp` 只管目录模式，目录内新建文件仍是 `0644`；且 `os.open` 的模式参数会被 umask 过滤，故写入后再 `chmod` 一次）；`redact()` 在子进程被 reap 之后把凭据换成 `<redacted>`，保留 hook 配置等非敏感字段供事后调试。时机是硬约束：`pre_tool_use.sh` 每次工具调用都读 `$CLAUDE_SETTINGS_PATH` 取 `ANTHROPIC_BASE_URL`，脱敏只能在 reap 之后；后端若在此之前崩溃，兜底的就是 `0700`/`0600` 这两层。`redact()` 用 `is_managed()` 拒绝对非本进程创建的目录动手 —— 某些配置下 `self.settings` 是操作者自己的 `~/.claude/settings.json`，改写它会毁掉用户配置，比泄密更糟；派发点因此只传自己那一个文件（曾误传两个，被 `backend/tests/unit/test_coding_tool_settings_write_order.py` 与 `backend/tests/unit/test_coding_tool_secret_redaction.py` 当场拦下：第二次 dispatch 会读到 `<redacted>` 当密钥）。改动落在四个生产文件：`backend/utils/secret_files.py`（新增）、`backend/subagent_config.py`（`write_tmp_settings` 改走私有目录 + `0600`）、`backend/coding_tool.py`（派发写入同上，并在 `_graceful_shutdown` 之后对本次 settings 脱敏）、`backend/verification_subagent.py`（`SettingsJsonBuilder` 虽不含凭据，也从扁平 `/tmp` 迁入私有目录并记忆化路径，否则 `build()` 与 `cleanup()` 会指向不同目录）。静态门禁在 `backend/tests/static_gates/test_credential_files_are_written_private.py`，双向钉住：生产源码不得出现字面量 `/tmp/*settings*.json`，且每个写 settings 的模块必须走 `utils.secret_files`。代码修复只防下一次，所以另配 `scripts/redact_leaked_secrets.py` 处理崩溃残留：默认 dry-run，要 `--apply` 才改写；从进程表里捞出 `--settings <path>` 并跳过活进程正在用的文件；拒绝跟随 symlink（在 `1777` 的 `/tmp` 里，一个名字合法的软链就能把重写引到任意文件上）；改写本身复用 `utils.secret_files.redact`，因此不会碰非本进程创建的目录。并已用它清理过因后端被杀、未走到脱敏点而残留的凭据文件。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_credential_files_are_written_private.py backend/tests/unit/test_secret_files.py backend/tests/unit/test_coding_tool_secret_redaction.py -v
```

### Finding ENTRY-018
档位: should-fix
问题: `state.db` 的位置由**九处独立实现的两步规则**各自解析 —— 先看 `PDT_STATE_DB_PATH` 环境变量，否则从该模块自己的 `__file__` 推算。分布在 `backend/server.py`、`backend/plan_state.py`、`backend/task_repository.py`、`backend/task_manager.py`（两处）、`backend/agent.py`、`backend/watchdog.py`、`backend/verification/orchestrator.py`、`backend/notifications/plan_dir_resolver.py`，以及 `backend/state_machine/db/connection.py`（用 `parents[3]`）。`backend/config_paths.py` 早已定义 `STATE_DB` 并声明自己的存在意义就是消除这类逐模块重推，但只有一处走了它。
影响: 九份拷贝里**两份已经漂移，且都没有报错**。`verification/orchestrator` 的默认路径少了一层 `.parent`，于是落到 `<repo>/backend/state.db` —— 与真实库同名、却位于另一个目录的位置；写进去的行任何其他消费者都读不到，于是执行器 Phase 2 reconcile 依赖的 RP-* 修复任务行被静默丢弃。当时的修复是补齐那一层 `.parent`，即修的是**那份拷贝**，而不是拷贝这件事本身。`watchdog` 则**完全不读** `PDT_STATE_DB_PATH`，所以一个把库重定向到临时文件的测试进程，watchdog 仍然打开部署方的实时库 —— 而那正是「fixture 行被写进实时库」这一类事故的入口。失败模式与拼写错误不同：进程照常启动、查询照常返回，只是行写进了另一个**合法但无人在读**的文件，没有任何日志指向路径。
攻击路径: 前置条件 — 无（这不是运行时漏洞，而是重复实现路径解析造成的静默状态错位；列出它是为了让本轮重构可归因）；触发步骤 — 任一承载该拷贝的模块被移动或重构，或某处新增第十份拷贝，而目标位置已经变了；可观测后果 — 进程健康、API 正常，但计划/修复任务状态写进另一个 `state.db`，操作者看到的是「计划卡住 / 任务丢失」，没有任何错误可追。
修复: `backend/config_paths.py` 新增 `STATE_DB_ENV` 与 `resolve_state_db_path()`（并在 `STATE_DB` 上写明其**父目录**同时承载 `pdt_server_boot_id` 与 `backups/state-db/`，三者是一体的本地运行态），随后把九处拷贝全部改为调用它：`backend/server.py`（`_state_db_path` 只保留 live-DB 守卫，解析交给解析器）、`backend/plan_state.py`、`backend/task_repository.py`、`backend/task_manager.py`、`backend/agent.py`、`backend/watchdog.py`（顺带修好它不认环境变量覆盖）、`backend/state_machine/db/connection.py`（`REAL_STATE_DB` 直接取 `STATE_DB`）、`backend/verification/orchestrator.py`、`backend/notifications/plan_dir_resolver.py`。静态门禁在 `backend/tests/static_gates/test_state_db_path_has_one_resolver.py`，共四条：生产代码里只有 `config_paths.py` 可以出现字面量字符串 `"state.db"`（用 AST 而非正则，注释里的同名文本不算）；只有它可以读 `PDT_STATE_DB_PATH`；九个消费方必须引用解析器，且模块改名会让门禁变红（防止列表悄悄缩短）；解析器必须遵守覆盖且不得在 import 期缓存。该门禁已做过灵敏度验证：把旧形状塞回 `backend/plan_state.py` 会立刻报 `backend/plan_state.py:L49`。搬迁本身在 2026-09-28 执行：`STATE_DIR = <repo>/.pdt`，`state.db` 与其 `-wal`/`-shm`、`pdt_server_boot_id`、`backups/state-db/` 一起迁入，`.gitignore` 增加 `.pdt/` 并**保留**原有的逐条忽略项 —— 万一有东西把 state 文件写回仓库根，它仍然是被忽略的而不是变得可提交，这个方向的失败才是安全的。顺序是硬要求：先 `SIGTERM` 优雅停服让 SQLite checkpoint，**确认 `-wal` 归零**之后再搬 —— WAL 模式下未 checkpoint 的事务全部堆在 `-wal` 里，它的体积可以**远超主库文件本身**，只搬主库会丢掉绝大部分已提交事务。搬后 `integrity_check: ok`，计划与任务行数与搬迁前逐项一致。同一次搬迁还把 nightly 产物从根目录的 `.pdt-nightly-results.json` 收到 `.pdt/nightly-results.json`：这涉及 `scripts/write_nightly_results.py`（写入端的 `DEFAULT_RESULTS_PATH`），并且该路径**同时硬编码在本仓之外**的操作者调度脚本里（写端的提示词文本与归档端的读取常量），两处已同步更新并验证写端与读端解析到同一个文件；写入端注释里记下了这条跨仓依赖，避免下次搬迁只改一半而让 nightly 写一份、归档读另一份。另外 `backend/verification_plan_completeness.py` 的 `_SKIP_DIRS` 增加 `.pdt`，否则完整性探针会把运行态目录当成项目源码扫描（旧名 `.pdt-nightly` 保留，让陈旧目录仍被跳过）。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_state_db_path_has_one_resolver.py -v
```

### Finding ENTRY-019
档位: note
问题: 仓库里**对自身行为的文字描述可以无声地偏离实现**，而且没有任何构建级检查会发现。本轮实例：`backend/configs/verification.yaml` 的 `parallelism_cap` 注释声称验证点"currently runs sequentially"、该开关是给"future concurrent implementation"用的 —— 而 `verification_agent._execute_group` 早已是两层并发（组间 `asyncio.gather`、组内 `asyncio.gather`、外加逐轮创建、所有组共享的 `plan_semaphore`）。注释不是笔误：它**当时是对的**，实现变了而注释没变。同一个文件里，这个注释还直接决定读者怎么理解那个开关 —— 把它读成"未来的开关"就会低估真实的并发度。更深一层的问题是结构性的：这个仓库没有开发者文档的位置，于是所有叙述都挤在 README 和源码注释里，而两者都没有被任何东西校验。
影响: 一份说"这里是串行的"的注释会让读者按错误的并发模型推理 —— 例如把 `parallelism_cap` 当成全局上限，而真实上限是那个共享信号量（每组各自的 cap 会相乘）。这类偏离不会让任何测试变红，因为测试断言的是行为，而偏离的是**关于行为的陈述**。本仓库已经因为同一类问题吃过亏（见 ENTRY-005：注释把操作者原话写进了源码；ENTRY-013：门禁期望的契约被重构偏离）。
攻击路径: 前置条件 — 无（这不是运行时缺陷，而是文字与实现之间的漂移；列出它是因为本轮新增的文档站点属于同一类工作，需要被审计授权）；触发步骤 — 实现变化而描述它的散文没有同步更新，且没有任何构建步骤读那份散文；可观测后果 — 读者（包括未来的维护者和下游使用者）按错误的模型推理，而套件全绿。
修复: 订正 `backend/configs/verification.yaml` 的注释，写明两层边界的区别（per-group 的 `parallelism_cap` 与逐轮创建、所有组共享的 `plan_semaphore`），并注明旧注释何时起已经不成立。结构性的那一半：给开发者文档一个**可被构建校验的家** —— `docs/`（MkDocs 源）+ `mkdocs.yml`（含 `strict: true`）+ `.github/workflows/pages.yml`（PR 上跑 `mkdocs build --strict`，main 上才部署到 GitHub Pages）。`--strict` 会把"nav 指向不存在的页面""链接指向不存在的页面"变成非零退出，于是文档第一次有了和代码同一类的门禁。同时 README 收缩为入口（原 702 行 → 283 行），深度内容迁入 `docs/`，但保留 `backend/tests/static_gates/test_readme_onboarding_sections.py` 逐字钉住的那几处（架构图、CPEA 说明、venv 告警、request guard 的包装规则、`.config/` vs `example/`、贡献入口）—— 那条门禁是给"零提问走查"用的定位契约，不因为内容搬家而放宽。配套：`docs/` 整体纳入「缺陷 vs 事故」门禁的扫描面（它现在是比根目录两份文档更大的公开面）。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_readme_onboarding_sections.py backend/tests/static_gates/test_public_security_docs_describe_defects_not_incidents.py -v
```

### Finding ENTRY-020
档位: should-fix
问题: 本文件此前同时装着两种内容，而它们的公开性判断**相反**。除缺陷条目外，它还带着**关于本轮排查过程本身的记账**：带采集日期的三层墙钟基线、哪些（面 × 问题类）格子查过且为空的覆盖矩阵、逐条 grep 的命中与处置、以及按档位的条目索引。这些不是在说软件 —— 否定结果那一半尤其如此：「查过，没找到」的集合是一张**哪里没人找到过东西**的地图，而没人找到过的地方，正是后续最不会被回头看的地方。执行「缺陷 vs 事故」门禁的第一版**没有覆盖它们**：那条规则是从当时手上已经找到的两个实例反推出来的，于是只覆盖了那两个形状，这几节一直全绿。
影响: 公开文档携带的是一份**排查过程档案**，而不是一份公告。读者从中拿到的不是「哪里有洞」，而是「哪里查过且是空的」和「我们什么时候跑过一次、跑了多久」—— 前者是攻击面的优先级线索，后者是本机事实。二者都无法从源码推出，所以公开它们换不到可信度，只交出线索。另一半代价是结构性的：把两半长期放在同一个文件里，「这条该不该公开」这个判断就要在每次新增小节时重做一遍，而不是由一条规则一次定死。
攻击路径: 前置条件 — 无（这是内容策略缺陷，不是运行时漏洞；列出它是为了给本轮拆分可归因）；触发步骤 — 审计过程中新增一节过程记账，作者按其「看起来更严谨」的直觉把它写进公开文档，而门禁只认两种形状、放行；可观测后果 — 公开面同时包含缺陷叙事与排查记录，且后者的否定结果部分构成一张「以后不必再看」的地图。
修复: 按**主语**把审计拆成两份，判据是「这句话，一个陌生人读完代码之后能不能自己得出」。`SECURITY_AUDIT.md`（公开）保留档位定义、条目模板、五个面与全部条目，以及两个**确实在陈述本仓**的附录：Appendix A（改过哪些测试）与 Appendix B（审计授权改了哪些源码）—— 两者 `git log` 推得出来，且 B 是改动归因门禁的输入，搬走它会让陌生 clone 上那条门禁失效。过程记账迁入 gitignored 的 `.config/security-audit/sweep.md`，解析它们的两个 meta-test（`test_security_audit_completeness.py`、`test_timeout_contract_not_relaxed.py`）改读私有路径、文件不在时 skip 而非失败。门禁按**类别**重写，新增三类形状：采集日期（**采集动作** + 日期；代码改动的日期属于可推导，继续放行 —— 判别的是动词不是数字）、套件运行记录的词汇与数字、否定结果单元格（覆盖矩阵的结论词）；另加一条结构性断言：上面那几节标题不得回流公开文档。并配一条**用原文做样本**的回归用例 —— 拿迁移前该节的逐字文本喂进 `find_incident_narrative`，五类形状必须各自命中，防止日后把模式悄悄收窄回最初那两个。收窄过程中出现过一次误报并已订正：套件记录词表里的 `tests collected` 会命中 ENTRY-010 引用的 pytest 失败信息（"0 tests collected"），那是对工具行为的陈述而非对某次运行的记录，故从词表移除，并在「不得误报」的用例里钉住这一行。**残留**：拆分只约束工作树 —— 公开仓若带着历史发布，拆分前的版本仍可从历史中取回，所以首次公开前需要把本地提交压平（操作步骤记在本地 `CLAUDE.md`，不进公开面）。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_public_security_docs_describe_defects_not_incidents.py backend/tests/meta_tests/test_security_audit_completeness.py backend/tests/meta_tests/test_timeout_contract_not_relaxed.py -v
```

### Finding ENTRY-021
档位: note
问题: **生产源码与 CI 配置的注释里带着带日期的本机测量。** 若干注释以「Measured <日期>：<观察>」的句式给出结论 —— `backend/server.py`、`backend/routes/plans.py`、`backend/agent.py`、`backend/coding_tool.py`、`backend/bounded_subprocess.py`、`backend/plan_status.py`、`backend/tasks_generator.py`，以及 `.github/workflows/ci.yml` 的 job 预算说明。观察本身大多**是可复现的**（某个命令对不存在的测试名退出 0；`lsof` 显示通配绑定而非回环；一条空声明的任务占住锁不放），但**日期**是操作者本机的事实，读者推不出来；其中若干处还附带了本机运行的量级（一次扫描覆盖多少个计划、一次卡片渲染数出多少任务、本机 unit+integration 大约跑多久），同样不可推导。
影响: 与本文件「缺陷公开、事故私有」是同一条线，判据也一样：**读者读完代码能不能自己得出这句话**。可复现的观察能，日期与量级不能。单条线索很弱，但这类注释是**最容易被后来者当模板照抄**的形态 —— 一个句式被抄开之后，「本机事实不进公开面」这条规则就从内部被溶解了，而且没有任何构建步骤会发现。另一个代价是专一性：注释里写着只有本机才成立的具体量级，会让读者以为那是这条代码路径的规格，而不是一次运行的结果。
攻击路径: 前置条件 — 无（这是内容卫生缺陷，不是运行时漏洞；列出它是为了让本轮扫尾可归因）；触发步骤 — 作者把一次本机复现的结论连同日期与量级写进注释，后来者照着这个句式写下一处；可观测后果 — 公开源码里散布着带日期的本机测量，而「缺陷 vs 事故」门禁的扫描面只有 `SECURITY_AUDIT.md`、`README.md` 与 `docs/`，源码注释不在其中。
修复: 逐处改写为**陈述缺陷本身**，涉及 `backend/server.py`、`backend/routes/plans.py`、`backend/routes/execution.py`、`backend/routes/verification.py`、`backend/agent.py`、`backend/coding_tool.py`、`backend/bounded_subprocess.py`、`backend/plan_status.py`、`backend/tasks_generator.py`、`backend/task.py`、`backend/executor.py`、`backend/retry_manager.py`、`backend/repair_generator.py`、`backend/sub_agent_registry.py`、`backend/verification_agent.py`、`backend/verification_config.py`、`backend/verification_split.py`、`backend/verification_split_llm.py`、`backend/verification_loop.py`、`backend/verification_subagent.py`、`backend/framework/ids.py`、`backend/framework/task_output_validator.py`、`backend/test_command_quality.py`、`backend/notifications/cards.py`、`backend/notifications/feishu_notifier.py`、`backend/state_machine/repositories/plan_task_repository.py`、`backend/state_machine/repositories/verification_repository.py`、`backend/verification/orchestrator.py`、`scripts/write_nightly_results.py` 与 `.github/workflows/ci.yml`（另有测试 docstring 同步改写，测试文件本身由归因门禁自动覆盖）。三类改动：(1) 删掉日期与「Measured <日期>：」句式；(2) 删掉「某次运行」的具体痕迹 —— 计划 id、任务编号、任务计数、墙上时间戳、「observed in production」「现场证据」这类取证措辞，改为描述缺陷本身；(3) 把「<日期> plan (<主题>)」这一变更日志句式统一为「<日期> (<主题>)」，因为内容和日期 `git log` 推得出来，而 "plan" 一词把一条变更记录框成了「某人跑过的一个计划」。技术内容一处未减 —— 可复现的命令、观察到的行为、以及代码为什么写成现在这样，全部保留；这正是「缺陷」与「事故」的分界：前者留下来，后者不留。**没有**同时把门禁的扫描面扩到源码注释，这是刻意的：门禁现有的「套件记录」与「否定结果」两类形状必须限定在文档上，否则会与测试夹具里合法的 pytest 输出字符串（形如 `<n> passed`）大量误报；而「测量动词 + 日期」这一类要单独定域，还需要处理门禁文件自身的引用问题（它必须包含这些形状才能检查它们）。那是一次独立的改动，属于下一轮，不混在本轮的内容清理里。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_diff_is_attributable_to_audit_findings.py backend/tests/static_gates/test_public_security_docs_describe_defects_not_incidents.py -v
```

### Finding ENTRY-022
档位: should-fix
问题: **源码把一次部署的本机配置编译进了每个安装**：一个具体模型名，和一个**本仓库之外的工具**的端口。前者有三处 —— `coding_tool.py` 的类常量默认模型、`env_config.py` 的配置默认值、以及 `subagent_config.py` 在模型解析不出来时的兜底默认（三者都是「解析不到就替你选一个模型」）。后者是 CC Switch（操作者本机的一个代理工具）的监听端口，出现在若干测试里。本仓在 `example/` 上已经明确拒绝这种做法（**刻意不放带真名的示例**，理由是「一串真实名字属于部署机器上的配置」），但这两类内容当时没有被同一条规则覆盖。
影响: 与 ENTRY-004 / ENTRY-016 是同一个失效模式，只是换了个轴。模型名：任何一份没写 model 的配置都会静默拿到这一个部署选的模型，而模型选择恰恰是最该由使用方决定的东西 —— 本仓自己的文档也把「不要用 `--model` 覆盖使用方的选择」写成了契约。端口：那个端口属于**另一个程序**，它出现在本仓测试里既非本仓的契约、也不在任何本仓的配置面上，读者只能把它当成一个真实部署的痕迹。判据仍然是那句：读者读完代码能不能自己得出？模型名和那个端口都不能。
攻击路径: 前置条件 — 无（这是可移植性与内容卫生缺陷，不是运行时漏洞；列出它是为了给本轮清理可归因）；触发步骤 — 新增一条模型解析路径时顺手加一个「总得有个默认」的兜底名字，或把本机某个第三方工具的端口抄进测试常量；可观测后果 — 公开源码里带着一个具体模型名与一个外部工具端口，而没有任何构建步骤会发现（`example/` 的中立性门禁只扫 `example/`）。
修复: 删掉两处**死代码默认**（`backend/coding_tool.py` 的 `DEFAULT_MODEL`、`backend/env_config.py` 中 `DEFAULTS["CLAUDE_MODEL"]`）—— 两者都无任何调用方，构造函数早已不再自动套用默认模型，配置加载器也没有任何东西读那个键；同步清掉 `backend/.env.ci` 与 `backend/.env.example` 里的对应条目（改由使用方在自己的 `.env` 里设置）。把 `backend/subagent_config.py` 的兜底从 `_model or '<具体模型>'` 改成**解析不出来就不写** `ANTHROPIC_MODEL`：解析顺序不变（调用方 `model_env` > CC Switch 该行的模型），只是最后不再由本仓指定一个名字，字段缺省时由 Claude CLI 从本地配置解析 —— 这正是「模型选择属于使用方」的落地。字段数硬契约随之从 4 降回 3（endpoint + credentials），并在 `to_settings_dict` 处写明为什么模型不是必填。CC Switch 的端口在测试里全部换成保留域名（RFC 2606 的 `.invalid`）—— 测试只需要「一个不能被子 agent 继承的父进程代理端点」，值的具体性不是被测对象。本仓自己的端口（`8000` 后端默认、`8001` E2E、`8002`/`8003` 随附服务）是**既定契约**，保留不动。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/unit/test_config_loader.py backend/tests/unit/test_subagent_config.py backend/tests/integration/test_agent_wiring.py backend/tests/unit/test_dispatch_inherit_mode.py -v
```

---

### Finding ENTRY-023
档位: should-fix
问题: **源码里仍然带着操作者本人的痕迹**，而这些痕迹**一个都不含既有门禁在查的那两个词**。上一轮把「操作者归属」收窄成了两个字符串（`用户原话` / `operator report`），于是下面这些形状全都从门禁底下走了过去，而且它们比被查的那两个更常见：(a) **逐字引用的私人语句**，写在公开 docstring 里 —— 有中文整句，也有英文的 `Per the user's request ("…")`；(b) **把设计决定归因给某个人** —— `the operator asked`、`per the user's principle`、`operator directive`、`User feedback <日期>`、`用户提出`；(c) **第一手的量测数字** —— 某一轮里「N 条修复任务中有 M 条被误判」、「一批 VP 从多少个变成了多少个」、某次任务「卡了多久」、某个 prompt「有多大」、某端口「被占了多少天」、「本仓多少个计划里命中多少个」；(d) **具体计划的条目号**，形如 `11-5-1` / `repair-r4-01`。
影响: 这一类的判据和 ENTRY-016 / ENTRY-021 是同一条 —— 读者读完代码能不能自己得出？私人语句、归因、以及在一台具体机器上跑出来的计数，三者都不能：它们不是软件的性质，是**这一次部署的性质**。危害不在运行时而在于：把本仓当通用工具来读的人，会读到一段记录着某个陌生人私有会话的注释；而第一手计数还会被误读成软件的性能承诺。此外这类内容会**随时间失效**（计数对应的是某一天的语料），保留它们等于在源码里维护一份会腐烂的档案。
攻击路径: 前置条件 — 无（内容卫生缺陷，不是运行时漏洞；列出它是为了给本轮清理可归因）；触发步骤 — 修完一个 bug 时，把「谁要求的、要求了什么、我实测到多少次」顺手写进注释，而不是只写清不变量；可观测后果 — 公开源码里长期带着私人对话片段、对个人的归因、以及会失效的实测数字，而没有任何构建步骤会发现（既有门禁只查两个固定字符串）。
修复: 逐处改写为**只陈述缺陷与不变量本身**，删掉归因、日期化的指令、逐字引用与实测计数：含中文整句引用的 docstring 重写为对行为的陈述；`the operator asked` / `per the user's X` 一类改成无主语的规则陈述；`User feedback <日期>:` 改成「行为是什么」；这一类计数一律删去，只留定性结论（若定性结论本身站不住，那才是应该重新想的信号）。产品自身的提示词模版（`用户反馈：{feedback}` —— 那是给 LLM 看的评审 UI 字段标签）与 `the operator` 指代软件使用者的一般用法不在此列，不改。同时**把门禁从两个字符串泛化成形状匹配**：`backend/tests/static_gates/test_no_operator_attribution_in_source.py` 新增四条结构性规则（拥有格短语、拥有格 + 诉求名词、归属者作言语动词的主语、日期化的条目正文以引号开头），补上 `user feedback` / `operator feedback` / `用户提出` 三个短语，并新增正反两个测试，把「提示词模版」「行内引用报错」「dated 但不带引号的条目」钉成**不得命中**的反例 —— 门禁自身必须保持 phrase-free，为此把注释里的示例改成对形状的描述、测试样例改为运行时拼接。涉及 `backend/agent.py`、`backend/base_executor.py`、`backend/coding_tool.py`、`backend/server.py`、`backend/task_manager.py`、`backend/tasks_generator.py`、`backend/verification_loop.py`、`backend/verification_subagent.py`、`backend/verification_plan_delta.py`、`backend/refiner.py`、`backend/orphan_rules.py`、`backend/plan_usage.py`、`backend/self_review.py`、`backend/binary_freshness.py`、`backend/task_plan_delta.py`、`backend/arch_reviewer.py`、`backend/prd_refiner.py`、`backend/test_design_reviewer.py`、`backend/routes/execution.py`、`backend/routes/phases.py`、`backend/routes/verification.py`、`backend/notifications/cards.py`、`backend/notifications/feishu_notifier.py`、`backend/notifications/plan_dir_resolver.py`、`scripts/migrate_feishu_card_state.py`、`backend/state_machine/tests/integration/test_verification_routes.py`、`backend/state_machine/tests/unit/test_scheduler_support.py`、`backend/state_machine/tests/unit/test_db_schema_v4.py`。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_no_operator_attribution_in_source.py -v
```

### Finding ENTRY-024
档位: should-fix
问题: **操作者另一个私有项目的外观被编译进了示例与夹具**。本仓的 `services` 示例、`binary_freshness` 的 PyO3 例子、验证命令的样例、若干测试夹具，用的都不是中立名字，而是操作者本地那个项目的真实构件：一个具体的 Rust 扩展包名与其 `lib<name>.so` / `.dylib`、一个具体的服务名、两个具体端口（其中一个在注释里被当作「某个 plan 实测占用了 3.4 天」的证据）、一个真实存在的 A 股代码（出现在 curl 样例、`grep -rn` 样例与一个以它命名的「基线」测试文件名里）、若干真实目录名与一个真实的审计产物文件名。另有一个**本仓之外的工具**（CC Switch）的监听端口做默认值留在测试里。ENTRY-016 已经就 `example/` 立过同一类规则（**刻意不放带真名的示例**，理由是「一串真实名字属于部署机器上的配置」），但那条规则当时只覆盖了 `example/` 目录。
影响: 与 ENTRY-016 / ENTRY-022 同一个失效模式，换了个轴。示例与夹具是**公开仓里最容易被读者当成通用样例来读的部分**，而它们实际描述的是一个特定私有项目的目录结构、构建产物命名、服务拓扑与业务领域。读者读完会以为自己知道了这个工具作者的另一个项目长什么样 —— 这正是当初拒绝在 `example/` 里放真名的理由。判据仍然是：任何读者能不能从源码自己得出？不能 —— 这些具体值只能来自那台机器。
攻击路径: 前置条件 — 无（内容卫生缺陷）；触发步骤 — 为一个真实 bug 写回归夹具时，直接把现场的命令、端口、符号、文件名粘进测试；可观测后果 — 公开源码里带着操作者另一个项目的构件名与端口，且因为夹具自身是绿的、门禁只扫 `example/`，没有任何构建步骤会发现。
修复: 把示例与夹具里的真实构件换成中立占位，并保持全仓一致（同一个名字在源码与测试里必须同时改，否则断言会与夹具脱节）：Rust 扩展包名统一改成 `native_ext`、其库文件名 `libnative_ext`、其源文件名改成 `core.rs`、其测试文件名改成 `lifecycle.rs`；服务名改成 `api`、端口改成 `8080`（另一个示例端口改成 `3000`）；curl 样例的端点改成 `/api/items/EXAMPLE`；股票代码改成 `EXAMPLE`、以它命名的基线测试改成 `example_baseline`；真实目录名改成 `frontend-app` / `handlers/`；真实审计产物文件名里的哈希改成 `0f0f0f0f`。**CC Switch 的监听端口从测试里删除，不给默认值** —— 该端口的地址是某台机器的属性，而生产代码从来不调这个 REST API（它读的是 cc-switch 的 SQLite 库，见 `backend/cc_switch.py`），所以测试改为「`PDT_TEST_CC_SWITCH_API_URL` 未设置即跳过」，边界用例改用一个不可解析的保留域名（RFC 2606 的 `.invalid`）。本仓自己的端口（`8000` 后端默认、`8001` E2E、`8002`/`8003` 随附服务）是**既定契约**，保留不动。涉及 `backend/service_declaration.py`、`backend/service_freshness.py`、`backend/service_manager.py`、`backend/service_restart_agent.py`、`backend/binary_freshness.py`、`backend/verification_command_guard.py`、`backend/verification_ci_runner.py`、`backend/verification_agent.py`、`backend/verification_plan_completeness.py`、`backend/tasks_generator.py`、`backend/test_command_quality.py`、`backend/framework/task_output_validator.py`、`backend/routes/execution.py`、`backend/routes/phases.py`、`backend/agent.py`、`backend/tests/conftest.py`。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_diff_is_attributable_to_audit_findings.py backend/tests/static_gates/test_no_operator_attribution_in_source.py backend/tests/static_gates/test_public_security_docs_describe_defects_not_incidents.py -v
```

---

### Finding ENTRY-025
档位: blocker
问题: **ENTRY-017 的凭据脱敏只覆盖了派发的一条出口，而它的清理脚本没有任何调用方。** 三处独立缺陷，任一处都足以让一份 live 凭据长期留在盘上。(a) `coding_tool._run_claude_interactive` 的脱敏调用坐在读循环的 `try` **之后**，而 `ApiError` 不是 `ValueError`：它从循环体抛出时会穿透 `except` 子句，`finally` 跑完就一路离开函数，跳过脱敏 —— 而 provider 报错正是最常见的非干净出口，也就是说最需要清理的那些派发恰好落在保证之外。同一函数的 setup 区 `except BaseException` 只归还了 scene slot，而 settings 文件在它之前就已经落盘，所以「写成功、随后 `Popen` 或 watcher 启动抛异常」同样留下凭据。(b) `scripts/redact_leaked_secrets.py` 是崩溃残留唯一的清理手段，却始终只是一条手动命令，没有任何东西调用它 —— 后端被 kill 之后的残留因此没有任何边界。(c) 操作者舰队的 nightly runner 在**另一个仓**里自己写 settings 文件：扁平 `/tmp`、`open()` 走默认 umask 得到 `0644`、docstring 明写「刻意不删」，既不用 `private_dir()` 也不用 `write_private_json()`；而清理脚本按文件名认领候选，认不出这个名字，所以它在那份 dry-run 报告里显示为 `clean`。
影响: ENTRY-017 把「运行时落盘物」这一整类收进了覆盖面，但收的是**写入侧**的三种形状（目录模式、文件模式、reap 后脱敏）。它没有回答「脱敏本身在哪些出口上会跑不到」，也没有回答「谁保证崩溃残留会被清理」。结果是防线在纸面上完整、在运行中按出口分流：干净退出的派发被脱敏，报错退出的不；被 kill 的后端留下的残留要等操作者想起来手动跑一次脚本；nightly 那一路则从一开始就在 `1777` 的 `/tmp` 里以全机可读的模式写一份 live 凭据。判据与 ENTRY-017 一致：这些文件里装的是能在不接触本机的情况下消耗账户额度的长期密钥，差别只在于攻击者要不要多等一个出口。
攻击路径: 前置条件 — 攻击者在本机拥有任意一个账户（或 temp 目录被快照、备份、容器共享）；触发步骤 — 等到一次 provider 报错的派发、一次被 kill 的后端、或一次 nightly 运行，然后枚举 temp root 下名字形如 `<prefix>_settings_*.json` 的文件并读取 `env.ANTHROPIC_AUTH_TOKEN`；可观测后果 — 拿到长期有效的 provider 密钥，可以其名义调用 API，而三种出口都发生在正常的运维动作里，不需要攻击者做任何事。
修复: 把脱敏从「干净出口之后」改成「reap 之后必然执行」：`backend/coding_tool.py` 里把它移进读循环的 `finally`（与 watcher 取消、scene slot 归还同一处，都因为 `ApiError` 会穿透 `except` 而必须在那里），并在 setup 区的 `except BaseException` 里补一次「已存在的子进程先 reap、再脱敏」——`process` 因此提到 try 之前初始化为 `None`，因为抛异常时 `Popen` 可能还没执行。清理脚本从「一条没人调用的命令」变成有调用方的模块：核心逻辑从 `scripts/redact_leaked_secrets.py` 提到 `backend/utils/secret_sweep.py`（`backend/` 内的调用方要能 import，而 `scripts/` 不在 `sys.path` 上；脚本保留全部 CLI 语义，改为薄封装），由 `backend/server.py` 的 `_lifespan` 在启动时调用一次 —— 与紧邻的 service-orphan sweep 同一个理由：启动是唯一「确定没有我们自己的东西在跑」的时刻，失败只记日志、绝不阻断启动。nightly 那一路的残留要能被认领，需要两处配合：`backend/utils/secret_files.py` 新增 `FLAT_TEMP_SETTINGS_RE` 与 `is_flat_temp_settings()`，把「扁平 temp 根里的 payload」按**精确名字 + 精确位置**两个条件收进来（`is_managed()` 本身不放宽 —— 那个守卫挡的是把操作者自己的 `~/.claude/settings.json` 改写成 `<redacted>`），`redact()` 通过 `flat_temp_roots` 参数显式接收扫描过的根，默认空所以派发点的调用面完全不受影响；`secret_sweep` 的候选集同时按两种深度认名字，根目录一层只收那个精确形状，不按前缀。`private-skills/local-actions/nightly_ci_runner.py` 改为 `mkdtemp(prefix="pdt-subagent-nightly-")` + 显式 `0600`，在所有子进程 reap 之后抹掉凭据（`workdir` 缺失的提前返回也要抹，否则一条拼错的路径就能把 live key 留在盘上），并把 `--dry-run` 挪到写文件之前 —— 干跑不该为一次预览在盘上落一份真 key。测试隔离随同一并做：启动 sweep 是**全机范围**的重写，而 `test_server_lifespan_runtime_state.py` 直接进 `_lifespan`、`test_state_from_db.py` 通过 `TestClient` 上下文进，于是 `PDT_DISABLE_TEMP_SWEEP` 在 `backend/tests/conftest.py` 里对整个会话置位（与 `PDT_PLANS_DIR` 同族的隔离开关），`sweep` 与 `default_roots` 不受影响，管线仍可对着 `tmp_path` 测。**第二轮补的是「目录数量」这条边界** —— 上一轮只修了凭据，没修人口：每次派发都新建一个目录，脱敏又刻意保留文件供事后排查，而写入前就失败的派发留下一个从一开始就空的目录，两者都没有任何东西回收，于是 temp root 每派发一次至少长一个。启动挂钩因此从「只脱敏」改成「脱敏 + 删空目录」两半，`prune_empty_dirs` 的候选前缀也从 `pdt-subagent-` 扩到 `pdt-ws-locks-`（后者的写入方用 `mkdir(parents=True, exist_ok=True)` 建目录，所以删掉一个空的不会让后续写入撞 ENOENT）；「空 + 超过年龄门」是全部的安全论证 —— 装了脱敏 payload 的目录永远不会被删，那是本仓刻意留下的验尸副本。`PDT_DISABLE_TEMP_SWEEP` 同时管住这两半，因为删目录也是一种全机副作用。另一半源头在测试侧：锁目录与 broker socket 都按 workspace digest 派生，测试里每个 `tmp_path` 都是一个新 workspace，于是每个用例都会往机器级 temp root 里留下一个锁目录 —— `backend/file_lock_protocol.py` 新增 `PDT_LOCK_ROOT` 覆盖（与 `PDT_PLANS_DIR` / `PDT_STATE_DB_PATH` 同族，默认仍是系统 temp 根），`backend/tests/conftest.py` 把它指向一个**独立短前缀**的会话目录。这里不能嵌在既有的 state 目录下：那会让派生出的 socket 路径突破 macOS 的 `sun_path` 字节上限，`bind()` 以 "AF_UNIX path too long" 失败 —— 正是 `socket_path` 自己的 docstring 警告过的那个失败。**第三轮补的是同一类边界的剩下那一半** —— 锁目录重定向之后，测试仍然在机器级 temp root 里留下 `pdt-subagent-*`：那是每次派发新建一个、脱敏后刻意保留（验尸副本）、因而永远不满足「空 + 超过年龄门」的目录，而启动 sweep 在测试期是关掉的，所以它既不会被抹也不会被删。删不掉的东西不该产生：`backend/utils/secret_files.py` 新增 `PRIVATE_ROOT_ENV_VAR`（`PDT_SECRET_TEMP_ROOT`）与 `_private_root()`，`private_dir()` 在设了覆盖时把 `mkdtemp` 指向它、未设时仍不带 `dir` 参数（默认行为逐字不变），`backend/tests/conftest.py` 把它指向会话目录下的 `private/` —— 这里嵌在 state 目录下是安全的，因为这些都是普通路径，没有 `sun_path` 那种字节上限要过。`is_managed()` 一个字也没动：它认的是目录**名**，所以重定向之后 payload 仍然可被脱敏，这正是重定向不能悄悄把文件变成 unmanaged 的原因。**第四轮补的是「存量」而不是「流量」** —— 重定向止住了测试侧的新增，但机器上已经攒下的、以及生产环境每天都在长的那一半，「空 + 超过年龄门」这条规则**永远够不到**：装了 payload 的目录从来不是空的，也永远不会变空。`backend/utils/secret_sweep.py` 因此新增 `prune_aged_residue` 与把它们合成一次的 `prune_residue`：`pdt-subagent-*` 超过 `DEFAULT_RESIDUE_MAX_AGE_SEC`（90 天）的目录**不论里面有什么**，连树一起删。安全论证是 mtime 而不是内容 —— 派发的子进程只活几分钟，脱敏在 reap 时就跑完，之后没有任何东西再读那个文件，所以目录的 mtime 就是「它最后一次被使用的时间」，一个三个月前的 mtime 不可能属于一个还在跑的子进程。**这条规则刻意不套到 `pdt-ws-locks-*`**，而这个省略是方案里最要紧的一处：锁目录的 mtime 记的是**目录何时被创建**，不是最后一次被使用 —— 写入方的 `mkdir(exist_ok=True)` 只在目录里出现第一个锁文件时碰它一次，于是一个全年都在用的 workspace 可以坐在一个 mtime 是一年前的目录里；在那个目录上做递归删除会把它下面正被持有的锁一并删掉，而持有者毫无察觉，互斥从此静默失效（下一个进程新建目录、发现里面没有锁、于是和持有者并排跑）。锁目录的数量也由「这台机器跑过多少个 workspace」决定而不是按派发计数，本来就没有要封顶的东西。还有一处细节写进了函数的 docstring：年龄度量的是「目录最后一次变化」，而脱敏是原子重写（tmpfile + `os.replace`），会在目录里创建并改名条目 —— 所以一个在启动时才被脱敏的 payload，时钟从那次启动开始走而不是从派发那一刻；这只延长窗口，且不会反复延长，因为 payload 一旦读到 `<redacted>` 重写就变成空操作，mtime 不再移动，时钟必然走到头。同一个规则接到 CLI：`--prune-aged-residue` 与 `--residue-max-age`，与 `--prune-empty-dirs` 一样默认关闭、默认干跑。涉及 `backend/utils/secret_sweep.py`（新增）、`backend/utils/secret_files.py`、`backend/coding_tool.py`、`backend/server.py`、`backend/file_lock_protocol.py`、`scripts/redact_leaked_secrets.py`、`backend/tests/conftest.py`。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/unit/test_coding_tool_secret_redaction.py backend/tests/unit/test_secret_files.py backend/tests/unit/test_redact_leaked_secrets.py backend/tests/test_server_lifespan_runtime_state.py backend/tests/static_gates/test_credential_files_are_written_private.py -v
```

---

### Finding ENTRY-026
档位: should-fix
问题: **发布树里带着两个只属于某一个安装的脚本，于是 `scripts/` 的含义被稀释了。** `scripts/write_nightly_results.py` 的存在理由是 nightly 的**提示词**让一个 LLM 自己拼 JSON、key 集会漂；它的输出落在 `.pdt/nightly-results.json` —— 一个**已经被 ignore 的本地路径**。本仓的 CI、测试、pre-commit 都不调用它，外面那条每晚真正在跑的调度也不调用它：那条调度是 `--exec` 直接跑 runner，而它的提示词仍然用散文列出 key，让子 agent 自己写文件。也就是说这个「唯一真相源」既没有被接上，它要防的漂移**已经发生了** —— 提示词今天的 key 集合（`status` / `pytest_summary` / `failing_tests` / `llm_timed_out` / `llm_killed_at` / `last_run_ts`）和脚本的规范 schema（`timestamp` / `exit_code` / `pytest_summary` + 兼容键）互不包含。`scripts/migrate_feishu_card_state.py` 的 docstring 第一行写着「One-time migration」：它迁移的是 `tools/data/feishu_sent_cards.json`，即那个**已退役的轮询桥**留在某一台 checkout 里的状态；新 clone 没有 `tools/data/`，没有东西可迁。
影响: `scripts/` 是「CI、pre-commit、开发者 shell 会执行的第一方代码」——本仓自己的静态门禁 `backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py` 就是按这个定义写的（每个脚本既是攻击面，也是门禁判定的来源）。往里放一次性脚本和操作者调度产物，等于把两个不同的东西合成一个命名空间：贡献者无法从目录名判断哪些脚本属于产品、哪些属于某台机器，而一台机器上的路径与流程**不是软件的性质**。另一面是它诱使别人把这个目录当作「放脚本的地方」，从而继续扩大这个歧义。判据与 ENTRY-023 / ENTRY-021 同源：读者能不能只看仓库就理解这个目录？一次性迁移和某个安装的调度产物都不能。
攻击路径: 前置条件 — 无（发布面卫生缺陷，不是运行时漏洞；列出它是为了给本轮删除可归因）；触发步骤 — 在一个新 checkout 上按目录名理解 `scripts/`，把 `migrate_feishu_card_state.py` 当成需要跑的初始化步骤（它读的 `tools/data/` 不存在），或者改动 `write_nightly_results.py` 的 schema 以为会改变 nightly 产物（调它的只有一段本仓之外的提示词散文）；可观测后果 — 贡献者的改动落在一条死路径上，而真正决定产物形状的那段提示词无人维护。
修复: 删掉两个脚本（`scripts/write_nightly_results.py`、`scripts/migrate_feishu_card_state.py`），`scripts/` 只留本仓 CI / pre-commit / 开发者 shell 真正会执行的那七个：`check_commit_msg.py`、`grep_guard.sh`、`install_git_hooks.sh`、`run_tests.sh`、`redact_leaked_secrets.py`、`scanners/scan_hardcoded_provider_ids.py`、`scanners/scan_provider_url_map_refs.py`。删除**不**触及 nightly 链路：产物路径 `.pdt/nightly-results.json` 仍由本仓之外的调度提示词与归档端常量约定，二者本次未动，读端与写端依旧解析到同一个文件。规则随之上墙，并且写在**会被发布的地方**而不只是本机的 `CLAUDE.md`（后者在本仓是 gitignore 的，见 `.gitignore` 里那条「this file documents my machine」）：`docs/development/contributing.md` 与 `docs/development/contributing.zh.md` 的「贡献面 / The contribution surface」一节新增 `scripts/` 与本地目录的处置规则 —— `scripts/` 只放通用脚本，任何只对某一次运行或某一台机器有意义的文件都放 `.pdt/`（该目录已被 ignore）。`README.md` 的 Layout 表同时补全：此前它列了 `backend/`、`docs/`、`frontend/` 等，却没有 `scripts/`、`example/`、以及几个本地目录，读者无从知道哪些目录属于产品、哪些属于运行这台机器的人。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/ backend/tests/meta_tests/ -q
git ls-files scripts/ | wc -l    # 7
```

---

### Finding ENTRY-027
档位: should-fix
问题: **`scripts/install_git_hooks.sh` 在 linked worktree 里拒绝运行，而它生成的 hook 把安装时的绝对路径写死。** 两半是同一个假设。脚本把 hooks 目录直接算成 `$REPO_ROOT/.git/hooks`；但 linked worktree 里 `.git` 是一个**文件**（内容是 `gitdir: <主 checkout>/.git/worktrees/<名>`），那条路径并不存在，于是脚本以 exit 2 退出 —— 恰好在一个 checkout、多个 worktree 这种布局里失败，而那正是 hook 最需要装上的地方。另一半是同一个假设的运行时版本：生成的 hook 体里 `REPO_ROOT="$REPO_ROOT"` 位于**未加引号**的 heredoc 中，所以它在**安装时**就被展开成一个绝对路径。
影响: 两个方向都是「看起来装上了、其实没生效」，而后一个方向是沉默的。worktree 里失败至少带着 exit code，运行的人会发现；路径写死那一次不然 —— hook 仍然是 executable，`git commit` 仍然会执行它，只是它去的是旧位置。checkout 一旦移动，轻则 hook 因为找不到脚本而失败（挡住所有提交），重则旧位置现在住着**另一个**仓库，于是你在这个仓提交、执行的是那个仓的脚本。对一条「不让 AI 归属写进永久历史」的策略来说，沉默的那半边是主要危害：这个策略的强制力完全等价于「hook 落在正确的位置」。
攻击路径: 前置条件 — 使用一个 linked worktree，或曾经移动过这个 checkout；触发步骤 — 在 worktree 里运行 `bash scripts/install_git_hooks.sh`（拒绝运行，于是该工作树的提交不受任何 hook 约束），或者移动 checkout 之后由仍然 executable 的旧 hook 在下次提交时去旧路径取脚本；可观测后果 — 策略在某个工作树里静默失效，或者一次提交执行了另一个仓库的代码。
修复: `scripts/install_git_hooks.sh` 改为向 git 询问 hooks 目录，而不是假设 —— `git rev-parse --git-common-dir` 在 linked worktree 下回答 hooks 真正被读取的那个**共享** `.git`，在主 checkout 下回答同一个目录；并保留对相对返回值的处理（旧版 git 会给出相对于仓库根的路径）。生成的 hook 体改为在**运行时**解析 `REPO_ROOT="$(git rev-parse --show-toplevel)"` —— git 执行 hook 时把工作树设为 cwd，所以它永远是正在提交的那棵树。`--check` 的判据（marker + 相对脚本路径）没有变，两种布局下都仍然正确。涉及 `scripts/install_git_hooks.sh`。
验证方式:
```bash
git worktree add --detach /tmp/wt HEAD
bash /tmp/wt/scripts/install_git_hooks.sh          # exit 0（修复前 exit 2）
grep -n 'show-toplevel' .git/hooks/commit-msg      # 运行时解析，非安装时写死
```

---

### Finding ENTRY-028
档位: should-fix
问题: **三处注释把一条已经删掉的写入路径描述成现役的，而那条路径的入口函数还留在代码里。** `backend/agent.py` 有三处说 breakdown 产生的子任务「已被追加进 `tasks.json`」—— 两处在 `_load_tasks` 的状态语义段落，一处在 same-id 循环的 trip actions 列表（「Reload tasks.json so any subtasks an earlier breakdown pass appended…」，而那段代码里根本没有 reload）。实际上分派器自己的 self-split 路径（`_breakdown_task` / `_breakdown_failed_task`）**已经被删除**，`backend/tests/unit/test_agent_no_self_split.py` 正是防止它回来的回归门禁；今天新增子任务的唯一实现是 refiner，它通过 `PlanTaskRepository.add_task` **直接写 `state.db`**，再由 `_load_tasks` 的 Phase-2 reconcile 注入 DAG。剩下的是 `TaskManager.add_task` —— 一个**零调用方**的方法，形状恰好就是那条被禁止路径的入口。同一段注释里还有第二个过时项：same-id 的 trip action 1 写着「Mark the looping task failed on disk」，而代码在会话计数表明工作确实提交过时会把它改写成 `completed` 并只加入 blocker 集合，刻意避免把已提交的工作覆盖成失败。
影响: 注释是读者理解「谁在写什么」的唯一入口，而这里它指向一张**不存在**的图。危害是具体的：按注释去找「谁在改 `tasks.json`」的人会走进死路，进而可能在分派器里重新实现那条被禁止的路径 —— 门禁拦得住**调用点**，拦不住被误导的人先把入口函数捡回来。判据与 ENTRY-023 同源：读者能不能只看代码就得出正确结论？这三处让他得出的是错的。
攻击路径: 前置条件 — 无（内容卫生缺陷，不是运行时漏洞；列出它是为了给本轮改动可归因）；触发步骤 — 按 `_load_tasks` 的状态语义注释判断「子任务写在 `tasks.json` 里」，据此在 dispatcher 中新增一个写入点；可观测后果 — self-split 路径以门禁所禁止的形状被重新引入，或一次排查终止在一条不存在的写入链上。
修复: 三处注释改写为陈述真实机制 —— 子任务由 refiner 写进 `state.db`，由 `_load_tasks` 的 Phase-2 reconcile 注入 DAG；same-id 的 trip actions 列表按代码实际行为重写（会话计数表明确实提交过时改写成 `completed` 并阻塞该 id，否则回落到 `record_task_failure`）。删掉零调用方的 `TaskManager.add_task`，原位留一段说明它随 self-split 一起消失、以及现在由谁负责，免得被禁的形状离一次调用只有一步。**没有改任何运行时行为**：本轮全部改动是注释与死代码，`save_tasks()` 仍只写静态字段、仍由状态变更触发。涉及 `backend/agent.py`、`backend/task_manager.py`。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/unit/test_agent_no_self_split.py backend/tests/unit/test_agent_load.py backend/tests/static_gates/ backend/tests/meta_tests/ -q
```

### Finding ENTRY-029
档位: should-fix
问题: **所有"防隐私"静态门禁扫的都是当前树，没有一条扫提交历史。** 三条规则（本机家目录路径、操作者归属、带日期的本机测量）各自由 `backend/tests/static_gates/` 下的一条门禁执行，而它们都经 `source_scan.iter_first_party_sources()` 遍历磁盘上的文件 —— 看到的只有最终状态。于是"先加后删"是一个隐形形状：某个提交写入私有标识，下一个提交把它删掉，交付树干净、全部门禁绿，而携带该标识的那一份快照仍然留在历史里。历史不是本地残留：公开仓库的任意提交都可由 `git fetch origin <sha>` 匿名取出，`refs/pull/<N>/head` 在 squash merge 之后仍然保留 PR 的原始提交 —— squash 决定的是默认分支上留下什么，不是仓库里还留着什么。净 diff 与最终树都不是"已经公开了什么"的度量。
影响: 树干净会被读成"没有泄露"，而这个结论对历史不成立。私有项目名、真实计划号与本机测量数据可以在全部树形门禁为绿的同时已经公开，且推送之前与之后都没有机制提示。修复代价极不对称：树上的问题改一行即可，历史里的问题只能删除仓库重建或联系托管方，二者都远大于在推送前发现。
攻击路径: 前置条件 — 一次把本地未打算公开的祖先提交一并推上去的 push（本地存在未被推送的历史，而被推的是一个带祖先链的 ref）；触发步骤 — 对一个公开仓库执行 `git fetch origin <任意历史 SHA>`，或读取 `refs/pull/<N>/head`，两步都不需要认证；可观测后果 — 该快照里的私有标识对匿名读者可见，而当前树与全部树形门禁仍报告干净。
修复: 新增按提交区间扫描的机制，规则不重写，直接复用树门禁导出的纯函数。新增 `backend/tests/static_gates/commit_range_scan.py`（`git ls-tree` 枚举树、`git cat-file --batch` 单进程流式取内容、逐提交套用 `find_home_paths` / `find_attribution` / `find_local_measurements`，空区间与读不全一律报错而不是当作干净）与 `scripts/scan_commit_range.sh`（CI 与 pre-push 共用的单一入口，退出码 0/1/2 与 `scripts/grep_guard.sh` 一致；gitleaks 存在则对同一区间扫描，不存在则显式警告并跳过该步）。把 `backend/tests/static_gates/source_scan.py` 的过滤拆成两半：`is_first_party_source` 保持"相对扫描根"的原语义不变（磁盘遍历与传绝对根的 `scripts/` 门禁都依赖它），新增 `is_under_scan_root` 与 `is_first_party_path` 供持有仓库根相对路径的调用方使用 —— 把根归属判断塞进前者会让后两个调用方静默扫不到文件，而"非空"断言仍然为绿。CI 侧 `.github/workflows/ci.yml` 增加 `commit-range-privacy` job（`fetch-depth: 0`、`--ci --max-commits 5`、gitleaks 固定版本并以已发布的 SHA256 校验）。`scripts/install_git_hooks.sh` 增加 `pre-push` hook —— 生成器必须按 hook 类型传参（git 只在 stdin 给 ref 行），且 `--check` 必须连参数一起比对，否则参数失效的陈旧 hook 会被判为最新。**没有改任何运行时行为。**
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_commit_range_scan.py backend/tests/static_gates/ backend/tests/meta_tests/ -q
bash scripts/scan_commit_range.sh --range HEAD
```

### Finding ENTRY-030
档位: should-fix
问题: **树形隐私门禁的扫描面取决于进程的工作目录。** `source_scan.SCAN_ROOTS` 是相对名（`backend` / `frontend` / `scripts` / `example`），而遍历器把它们相对**cwd** 解析。CI 的单元分片用 `working-directory: backend` 运行套件，那里不存在 `backend/` 根，于是遍历器退回到**恰好存在**的 `backend/scripts/`，只返回少量互不相关的文件。后果是双向的：`test_scan_is_non_empty` 那类"非空"断言仍然通过（少量并非零），而它本该扫描的 `backend/` 一个文件都没被读取；同时，按 cwd 相对字面量定位模块的门禁会报"模块已不存在"。同类 cwd 依赖还出现在 `test_credential_files_are_written_private` 的 `_SETTINGS_WRITERS` 上。
影响: 一个"看起来在防、实际没扫"的门禁比没有门禁更糟 —— 它为一次发布提供一份虚假的清洁证明。本仓全部树形隐私门禁（家目录路径、操作者归属、本机测量、凭据写入点）都建立在这个遍历器上，所以在那种调用下它们**同时**失效，而 CI 报出来的却是别的失败，这个缺陷因此一直没有名字。
攻击路径: 前置条件 — 以仓库根以外的工作目录运行套件（CI 正是如此）；触发步骤 — 让一个树形门禁在其扫描面内找不到任何目标文件，然后看它结束时报什么；可观测后果 — 门禁以"通过"结束，而它本应检查的源码从未被打开。
修复: `backend/tests/static_gates/source_scan.py` 新增 `REPO_ROOT`（由 `__file__` 解析，四个父目录，与同目录各门禁的既有算法一致）、`resolve_root`（相对根解析到 `REPO_ROOT` 而非 cwd；绝对根原样保留，`test_scripts_have_no_dangerous_defaults` 传的正是绝对根）与 `repo_relative`（供展示、以及同仓库根相对路径比较之用）。`iter_first_party_sources` 改为产出**绝对**路径，使只做 `read_text()` 的调用方也不再依赖 cwd；按 `path.parts` / `path.name` 分类的调用方本就与基准无关，不受影响。`repo_relative` **不先解析符号链接** —— `backend/tests/` 下若干文件是指向 `unit/` 的链接，git 记录的是链接名，解析会把它们换成任何提交里都不存在的路径。`test_state_db_path_has_one_resolver` 的两处 resolver 豁免比较、`test_no_local_home_path_in_first_party` 的展示、`test_credential_files_are_written_private` 的 `_SETTINGS_WRITERS` 分别改用 `repo_relative` / `REPO_ROOT` 解析。`test_commit_range_scan` 新增回归门禁：从仓库根、`backend/` 与 `/` 三个 cwd 遍历必须得到同一个文件集 —— 计数下限抓不到这个缺陷（少量不是零），集合相等才能。**没有改任何运行时行为。**
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/ -q
cd backend && ./.venv/bin/python3 -m pytest tests/static_gates -q
```

### Finding ENTRY-031
档位: should-fix
问题: **CI 默认分片的 marker 表达式与文档声明的契约不一致，两条测试因此进了它们不该在的 lane。** 第一，`perf` marker 的说明写着这类基准只在 slow lane 运行（原文称 addopts 的 `-m "not slow"` 会排除它们），但 `backend/pytest.ini` 的 addopts 只有 `-m "not slow"` —— 它不排除 `perf`，于是启动延迟基准留在了默认分片里，而它断言的是一个以毫秒计的启动差异。第二，`backend/tests/integration/test_agent_execute_task_first5.py` 的每个用例都自行 spawn `pytest` 子进程去驱动真实任务管线 —— 那正是 `integration` marker 的定义 —— 却没有打这个 marker，于是被 `-m "not e2e and not integration"` 的单元分片收走，在其 60s 上限下被杀在 `subprocess.run` 里。
影响: CI 自第一次完整运行起就是红的，而两次红都与当时的改动无关：一条基准超时、一条嵌套运行超时。红色 CI 的代价是它不再传递信息 —— 真正的回归会混在恒定的噪声里；更具体的是分片在到达 `static_gates` 之前就死掉，所以 ENTRY-030 的缺陷（树形隐私门禁在 CI 里形同虚设）从未被执行过，也就无人发现。
攻击路径: 前置条件 — 无（这是 CI 有效性问题，不是运行时漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 检查默认分片是否收集了被文档声明属于其他 lane 的用例；可观测后果 — 分片在到达静态门禁之前被环境相关的超时终止，lint 之外的检查事实上不运行，而它们"存在"这一点让人以为已经跑过。
修复: `unit-tests` 与 `unit-staircase` 的表达式改为 `not e2e and not integration and not perf`；slow lane 改为 `slow or perf`（它的 `--timeout=1800` 是这类基准唯一能承受的预算）；`backend/pytest.ini` 中 `perf` 的说明改写为陈述真实机制，包括那句错误声明本身。`test_agent_execute_task_first5.py` 补上模块级 `pytestmark = pytest.mark.integration`，使 integration lane（`-m "integration and not e2e"`，`--timeout=120`）收它、单元分片不再收它。新增门禁 `backend/tests/static_gates/test_ci_lanes_match_marker_contracts.py`：它读 `ci.yml` 里各 job 的 `-m` 表达式与源码里的 marker 声明，钉住上面三项契约（默认 lane 必须排除 `perf`、slow lane 必须收 `perf`、自行 spawn 子进程的用例必须声明 `integration`），并在表达式缺失或数量不为一时直接报错而不是空过。**没有改任何断言**，改的是这些用例在哪个预算下被执行。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_ci_lanes_match_marker_contracts.py -q
```

### Finding ENTRY-032
档位: should-fix
问题: **声明在 CI 里的超时一个都不生效，分片被回收时既没有日志也没有产物。** `unit-tests` 的 job 声明 `timeout-minutes: 40`、pytest 那一步声明 `timeout-minutes: 20`，而 `unit` 分片在 run 36544551232 上：这一步 `in_progress` 持续 44 分钟，后续的 `Upload pytest output` / `Upload coverage data` 与 runner 自己的 post-step 全部停在 `pending`，GitHub 在整 45m00s 回收 job。同一个签名（被回收时没有任何日志、没有任何产物）在 36525529220 / 36529881479 / 36537622223 / 36539731327 / 36544551232 上重复出现。被回收的 job 不执行 post-step，所以那条本可以指名"死在哪个用例"的上传永远不会发生 —— 失败销毁了自己的证据。**起初的判断（"`setsid` 让 runner 的超时够不着 pytest"）已被后来的证据否定**：在 run 36561917024 上，除声明的那条之外又加了自设的 watchdog（应于 11:44:27 触发），而这一步的 `timeout-minutes: 20` 应于 11:49:27 生效 —— 到 11:56 两条都没生效，同一 run 的 `misc`（另一台 VM）99 秒通过。执行者都在 runner 上的两条独立截止同时失效，说明是 runner 停止上报，不是某个用例卡住；这也解释了为什么在 workflow 里再写多少截止都无济于事。第二处：`perf` 归入 slow lane 后（ENTRY-031 的改动）暴露出 `backend/tests/performance/test_backend_startup_perf.py` 的轮询只 `except urllib.error.URLError`，而 `urllib` 把 `h.getresponse()` 放在 `AbstractHTTPHandler.do_open` 的包装之外，读超时以裸 `TimeoutError` 抛出；于是"端口已绑、应用尚未就绪"这个轮询本就为等待而设的瞬态，变成了不重试的 ERROR（run 36549676307 的 slow lane：`TimeoutError: timed out` 一路抛到用例之外，把一条基准抖动变成了发布门禁的失败）。
影响: 两者都让红/绿不再携带信息。前者的代价更具体：分片挂死时唯一能定位的线索是它自己的日志，而日志恰恰是唯一没有留下的东西 —— 五次运行、每次 45 分钟，runner 时间花掉了，`unit` 分片究竟卡在哪个用例至今没有名字。后者把一次基准抖动升级成 slow lane 的失败，而 slow lane 在 main 上是发布门禁。
攻击路径: 前置条件 — 无（这是 CI 有效性缺陷，不是运行时漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 让一个分片的 pytest 超过它声明的上限（或让它在 `setsid` 之后挂住），然后观察这一步结束时留下了什么；可观测后果 — 步骤与 job 的超时都不触发、post-step 停在 `pending`、job 在 45m00s 被回收且日志与产物一个都没有，而"这条 lane 有超时保护"这一点让人以为最坏情况已经被兜住。
修复: `.github/workflows/ci.yml` 的两个 `setsid` lane（`unit-tests` 与 `unit-staircase`）在 pytest 之后加一个自设截止：一个普通的 `sleep` + `kill -TERM -<PGID>` 子 shell（`setsid` 使 pytest 成为组长，故 PGID 即 PID），到点杀掉整个进程组并把一行截止说明追加进分片日志。它不依赖 runner 的超时机制（那是失效的那一环），只依赖 runner 还在执行用户态代码。这两者并不等价，而 run 36561917024 正好落在它们的差别上：watchdog 也没触发，说明那台 VM 连用户态都已经不走了。所以 watchdog 防的是**用例真卡住**这一类，防不了 runner 停止上报这一类 —— 后者只能靠不触发它（见本条目末尾的缓解步骤）。当 watchdog 确实触发时，杀组让 `wait` 返回，这一步因此能**结束**，`if: always()` 的上传才会发生 —— 上传正是整件事的目的。预算（900s / 1500s）刻意小于步骤自身上限，否则它永远轮不到。两条 lane 另加 `--durations=25`，让拿到的日志自带耗时分布。**只让分片结束还不够**：日志只能说到"哪个用例开始了"，而 `pytest-timeout` 的 thread 方法无法打断停在阻塞调用里的用例（它经 `PyThreadState_SetAsyncExc` 在主线程里抛异常，只在字节码边界生效），所以真卡住时它连 traceback 都不产生。于是 `backend/ci_process_guard.py` 增加 `_arm_stack_dump` / `_disarm_stack_dump`：在 SIGUSR1 上注册 `faulthandler`，把每个线程的 C 栈写进 `CI_STACK_DUMP_PATH`。两个细节都是跑出来才发现的 —— 汇必须是**文件**而不是 stderr（pytest 默认的 fd 级 capture 会在用例执行期间顶掉 fd 2，dump 会在进程被杀的那一刻被丢掉），信号必须只发给**组长**而不是进程组（未处理的 SIGUSR1 会终止目标，进程组级发送会顺手杀掉分片正阻塞其上的那个子进程，等于把要拍的那张照片弄丢）。watchdog 现在先 `kill -USR1`、停 10s、再 TERM/KILL，dump 文件与日志一起作为 artifact 上传。watchdog 自己的 stdout/stderr 必须重定向掉：`kill $WATCHDOG_PID` 杀的是子 shell 而不是它 `sleep`，那个孩子会被 reparent 并继续活到预算用尽；若它还攥着这一步的输出管道，runner 就会一直等一个 15 分钟后才到的 EOF —— 与这一步历史上那个 `tee` 楔子同一机制。截止是否触发由一个 flag 文件记，失败信息只据它来判定"是我们的预算到了"，而不是把一次 OOM（同样是 137）算到预算头上。`backend/tests/performance/test_backend_startup_perf.py` 把轮询抽成 `_poll_until_ready`，`except` 补上 `TimeoutError`（读超时的真实类型），并把单次尝试超时 `_POLL_TIMEOUT_SECONDS` 与整体截止 `_STARTUP_DEADLINE_SECONDS`（15s→30s）分成两个常量；`baseline` 半边的失败仍按原设计降级为 skip。新增两个用例钉住这条重试契约，并已验证它们对旧写法会失败。新增门禁 `test_a_lane_that_detaches_pytest_bounds_it_itself`、`test_the_watchdog_photographs_the_shard_before_it_kills` 与 `test_the_watchdog_does_not_hold_the_step_open`：前者要求任何在 `setsid` 下跑 pytest 的 lane 声明 `WATCHDOG_SECONDS` 且同时小于步骤与 job 上限；后者要求 watchdog 在杀之前先要一次栈、且 SIGUSR1 不得发给进程组、且 dump 文件确实在上传清单里；第三个要求 watchdog 自己重定向掉 stdout/stderr 并写下截止标记 —— 七种改坏方式（预算超上限 / 预算整段删掉 / USR1 删掉 / USR1 改发进程组 / dump 移出上传清单 / 去掉重定向 / 去掉截止标记）均已验证会失败。**但真正让 job 走到 45 分钟的是 runner 本身停止上报，而不是某个用例卡住。** run 36549676307 上 unit 分片的 pytest 步同时越过了它自己声明的 `timeout-minutes: 20`（应于 11:49:27 生效）与 watchdog 的 15 分钟预算（应于 11:44:27 生效），而同一 run 的 `misc` —— 另一台更小的 VM —— 99 秒通过。两条互相独立的截止同时失效不是"测试慢"，是 runner 不再上报；这也意味着**任何在 workflow 里写的截止都不可能是解药**：它们的执行者都在 runner 上，一个无法上报的 runner 既不能执行截止也不能上传日志。于是修复落在"别触发它"：把 nightly job 自 runner 缺陷首次出现起就带着的 `read_ahead_kb=128` 缓解步骤（actions/runner-images#13770）加到两条 `setsid` lane 上，并排在该步之前。与 nightly 的副本不同，这份会打印它改了哪些块设备、且在一个都没匹配到时发 `::warning::` —— 静默 no-op 的缓解与删掉的缓解无法区分，而 runner 镜像的设备名已经变过一次。新增门禁 `test_a_heavy_lane_mitigates_the_known_runner_bug` 钉住这一点（存在、在 pytest 之前、且会自报 no-op），三种改坏方式均已验证会失败。**没有降低任何断言强度**：perf 用例的判据（median 差值）与 pytest 的 `--timeout` 都没动，改的是超时能否触发、瞬态能否重试、以及卡住时能否留下栈。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_ci_lanes_match_marker_contracts.py backend/tests/performance/test_backend_startup_perf.py -q
```

### Finding ENTRY-033
档位: should-fix
问题: **文件锁 broker 的服务线程在 Linux 上停不下来，泄漏到用例之外。** `FileLockBroker.stop()` 靠关闭监听套接字来结束服务线程，但那个线程正阻塞在 `server.accept()` 上。在 Linux 上，一个已经阻塞在 `accept()` 里的线程会通过未决的系统调用继续持有文件描述符，所以 `close()` 并不唤醒它 —— `_serve()` 唯一的退出信号（`accept()` 抛 `OSError`）永远不会到达，线程活过 `stop()` 的 `join(timeout=5.0)`，也活过 conftest 泄漏守卫的 `join(timeout=10)`。macOS 会唤醒它（阻塞中的 `accept` 以 ECONNABORTED 失败），所以这个缺陷在开发机上完全不可见，只在 Linux runner 上现形。
影响: 每一次 agent 运行结束都留下一条 daemon 线程和一个监听套接字。在 CI 上它以泄漏守卫的形式报错，并指名肇事线程：run 36576222631 的 `alt-26-50` 分片有 6 个用例 ERROR（`test_agent_dispatch.py` 3 个、`test_agent_execution_log.py` 3 个），断言为 `1 application worker thread(s) outlived their test` / `assert not [<_RecordingThread(pdt-lock-broker, started daemon ...)>]`。单个分片里这是可数的几条；`unit` 分片要跑几千个用例，累积的线程与套接字是那条 lane 被 45 分钟回收的一个可信来源 —— 这一条尚未单独确证，先按已证实的缺陷记账。
攻击路径: 前置条件 — 无（这是测试隔离与 CI 有效性的缺陷，不是运行时漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 在 Linux 上跑任何一个会启动 broker 的用例，然后等它结束；可观测后果 — conftest 的泄漏断言以 `pdt-lock-broker` 点名该线程、用例 ERROR，而每个这样的用例还留下一条永不退出的线程与一个占用的 fd。
修复: `backend/file_lock_broker.py` 让服务循环轮询停止标志，而不是指望 `close()` 去唤醒 `accept()`：`start()` 在 `listen()` 之后调用 `server.settimeout(_ACCEPT_POLL_SECONDS)`，`_serve()` 增加 `except socket.timeout: continue`。轮询间隔 0.2s —— 它就是关停延迟，短到无感，又长到不构成忙等（线程除这一小段时间外都在睡眠）。`accept()` 返回的套接字无论监听套接字是否带超时都仍是阻塞模式（Python 3.7 起），所以每连接的 `_handle` 线程不受影响。**不能改用 `shutdown()`**：未连接的监听套接字上调用它会以 `ENOTCONN` 失败（在本机复现过），那条路走不通，这也是为什么选择轮询而不是"先 shutdown 再 close"。新增 `test_stop_ends_the_serving_thread_even_when_close_cannot_wake_it` 钉住这条契约：它用一层只截掉 `close()` 的代理把 Linux 的行为模拟出来（`socket.close` 在 C 类型上是只读属性，只能靠委托拦截），因此这条用例在 macOS 上也能区分修复前后 —— 旧写法会把 `join` 的 5 秒预算耗尽仍然失败，新写法让线程立即退出。已验证该用例对旧写法失败、对新写法通过。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/unit/test_file_lock_broker.py backend/tests/static_gates/test_diff_is_attributable_to_audit_findings.py -q
```

### Finding ENTRY-034
档位: should-fix
问题: **两条主测试分片的规模超出了托管 runner 能跑完的量级，被整段回收，而 ENTRY-032 交付的那套截止机制已被证伪。** 这两条分片各自把一个测试目录整体交给单个 job；它们的 pytest 步从未完成过一次。ENTRY-032 交付的 watchdog 从未在这些 job 上触发，步骤自身声明的 `timeout-minutes` 与 job 的 `timeout-minutes` 同样没有触发。job 被回收之后，连它们的日志 blob 都不存在 —— 连 checkout、pip install 这些明确成功、必然打印过输出的步骤都没有留下记录。ENTRY-032 的 `read_ahead_kb` 缓解同样没有阻止任何一次回收。真正的分辨依据不是"哪条截止生效了"，而是**每个 job 的工作量**：仓库里那条专用二分 harness 反复量到的结果是，小到某个量级的分片总能跑完并报出真实结果，越过那个量级就一律在回收线上一言不发地被收走。本条记的是这个缺陷类别 —— **分片规模没有上限约束**，而不是某一次的具体数字。
影响: `main` 的合并门禁长期是红的，而且红得没有信息 —— 每次回收、每次零日志、零产物。更实际的后果是它训练人忽略这条 lane：一个每次都红、且从不携带证据的检查等于没有检查，因此真正的回归（ENTRY-033 那种指名肇事线程的泄漏守卫报错）会被淹没在里面无人查看。
攻击路径: 前置条件 — 无（这是 CI 有效性的缺陷，不是运行时漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 让 `main` 上任意一次 push 或 PR 触发 CI；可观测后果 — 两条大分片在回收线上一言不发地消失、无日志无产物，而小分片绿灯，合并门禁因此长期不可用。
修复: `.github/workflows/ci.yml` 的 `unit-tests` lane 把 `unit` 与 `root` 两条 lane 各自切成多片，每片的文件数取在实测能跑完的量级内。分片方式是对**收集到的文件列表**取模轮转（`NR % count == index`），因此每个文件恰好属于一片，各片之并等于原来那一个 job 的全集 —— 覆盖率不变；覆盖率门禁本来就是下载全部 `.coverage.<shard>` 再 `coverage combine`，多几个分片对它透明。矩阵键从 `paths` 换成 `lane` 加上 `index`/`count`，路径集在步骤里由 `case` 映射，这样每个矩阵项各占一行；**不能用目录来切**，因为 `tests/unit` 下绝大多数文件直接躺在顶层，按目录切分不出来。收集那一步**故意不写** `-m`：标记只写在真正的 pytest 调用上，`test_default_lane_excludes_the_marker` 要求每条 lane 只有一处权威标记，第二个字面副本会让那条门禁无法判断以哪个为准；省掉它没有代价 —— 收集到的是文件的超集，而真调用上的标记会精确丢掉那些被标记的用例，实际执行的用例集合不变。**空分片必须报错退出**：切分或路径集写错时，零文件的分片会报成功，那是套件里的一个静默窟窿。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_ci_lanes_match_marker_contracts.py -q
```

### Finding ENTRY-035
档位: should-fix
问题: **分片让 CI 跑完之后，露出三条只在 runner 上出现的失败；其中两条是断言写错了，一条是真的校验漏洞。** 这三条此前一直被 ENTRY-034 那条从不完成的分片盖着，从未报出过。第一条：`_connect_db` 用 `SELECT 1` 校验 CC Switch 数据库，而 `SELECT 1` 是一条不需要读文件的语句 —— 它能否识别"这不是数据库"完全取决于 sqlite 构建的偶然行为。在本机它会抛 `DatabaseError`，看起来是对的；在 runner 上它不抛，文本文件顺利通过校验，然后由调用方自己的查询在查找中途抛出裸的 `sqlite3.DatabaseError`，而不是被包装成 `CCSwitchError`。同一份代码、同一条断言，在一个平台通过、在另一个平台失败 —— 这类测试什么也钉不住。第二条：一个用例要求 settings 文件必须带上每档模型键，而工具只在**操作者的 shell 导出了这些变量**时才写它们：模型管理是有意委托给 CC Switch 的（见 `SubagentConfig.to_settings_dict`），所以这些键的出现与否是环境的函数，不是这段代码的函数。开发机上过，干净的 runner 上不过。第三条：拉起真实 server 的那个 fixture 把 server 的 stdout/stderr 丢进了 `DEVNULL`，于是"server 根本没建库"和"server 建了个空库"变得无法区分，schema 断言只会报一个空的 `sqlite_master` —— 读起来像 schema 回归，而它不是。
影响: 一条真实的健壮性缺口（不可信的数据库文件会以未包装的 sqlite 异常的形式在调用中途冒出来，而不是在建连时就被拒绝），以及两条会随环境翻转的断言。第三条最贵：在它被修好之前，这个 fixture 无论失败多少次都不会说出真正的原因。
攻击路径: 前置条件 — 无（这是 CI 有效性与健壮性的缺陷，不是可被外部触发的漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 让一个不可信的 `cc-switch.db`（文本文件、被截断的文件、别人留下的空文件）走到 provider 查找；可观测后果 — 抛出的是裸 `sqlite3.DatabaseError` 而不是 `CCSwitchError`，调用方无法按"这个 provider 不可用"来降级，只能让它冒到 dispatch 中段。
修复: `backend/cc_switch.py` 的 `_connect_db` 改用 `SELECT name FROM sqlite_master LIMIT 1` 作为校验语句 —— `sqlite_master` 是 SQLite 在回答任何东西之前必须先解析的那张表，所以这条语句在**任何**平台上都会真正读到文件头，行为不再依赖 sqlite 构建。同一条 docstring 里"没有这个探测就会在 dispatch 中段才暴露"的说法此前是错的，一并改正。`backend/tests/unit/test_coding_tool_settings_injection.py` 把 `sdk_required` 收窄到三个无条件写出的字段（见 Appendix A）。`backend/tests/test_server_json_cleanup.py` 的 fixture 不再丢弃 server 输出（改为写入 tmp 文件并在失败信息里附上尾部），并在健康检查通过之后**断言被拉起的进程仍然活着** —— 端口是写死的，"8001 有应答"并不等于"应答的是我拉起的那个"，任何别的进程（上一个用例还没被回收的 server、镜像自带的监听者）都会让健康探测瞬间成功却写进另一个 `state.db`；fixture 还在 POST 之后立刻断言目标库已建成，让失败信息说"server 从未建库"，而不是让下游三个用例各自报一个空 `sqlite_master`。新增 `test_opening_a_database_is_validated_by_reading_the_schema` 钉住校验语句本身：用代理记录 `execute` 的 SQL，断言其中有一条读 `sqlite_master` —— 行为契约由既有的 `test_invalid_database_file_raises` 负责，但它在两个平台上的结果不同，钉不住任何东西。记录用代理而不是给 `sqlite3.Connection.execute` 打补丁，因为那是不可变 C 类型的属性，赋不上去。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/unit/test_cc_switch_reads.py backend/tests/unit/test_coding_tool_settings_injection.py backend/tests/test_server_json_cleanup.py -q
```

### Finding ENTRY-036
档位: should-fix
问题: **共用的 kill 辅助函数把一个 `MagicMock` 的 pid 解析成 PID 1，并对 init 的进程组发了 SIGKILL；而那四个分片的"卡死"，是这一步自己毁掉自己的证据造成的。** 两条独立缺陷。第一条在 `backend/utils/process.py` 的 `kill_process_group`：它用 `getattr(proc, "pid", None)` 取 pid，再交给 `os.getpgid`。但 `os.getpgid` 要的不是 `int`，而是任何实现了 `__index__` 的对象，而 `MagicMock` 恰好实现了 `__index__`、**返回 1**。于是"子进程的进程组"解析成 PID 1 所在的组，`killpg(1, SIGKILL)` 照发不误。`coding_tool` 里六条 provider 回退路径全部在 mock 进程上走到这里，所以单元套件每次跑都在朝 init 甩一发 SIGKILL。同一个函数还有第二个缺口：它不检查目标组是否就是调用者自己的组 —— `bounded_subprocess` 早就拒绝这件事了，这个共用版本没有，因此一个没用 `start_new_session=True` 起的子进程会连调用者一起杀掉。第二条在 `.github/workflows/ci.yml`：跑 pytest 的那一步由 `setsid` 后台启动 + `tail -f --pid` + 后台 watchdog + `wait` 四段拼成，**四段各自都是一种"这一步的 shell 不返回"的方式**，而它确实没返回过 —— 分片一直 `in_progress` 到 job 自己的 40 分钟上限把 job 取消掉，而 job 被取消时 GitHub 不上传任何东西：日志、栈转储、覆盖率数据，全丢。
影响: 第一条在开发机上无害（组 1 里只有 init，非特权进程发信号会拿到 `EPERM`，而 best-effort 的 `except OSError` 把它吞掉），所以它一直没有症状；它的代价是这条"杀干净"的承诺在测试里从未被验证过，而一个会朝 init 发信号的辅助函数，在任何特权下都不是可以放着不管的东西。第二条的代价大得多：**它删掉了唯一能指出元凶的证据。** `pytest-timeout` 的 thread 方法本来就打断不了停在阻塞调用里的用例（`PyThreadState_SetAsyncExc` 只在字节码边界生效），所以真卡住时连 traceback 都没有；`ci_process_guard` 在 SIGUSR1 上注册的 `faulthandler` 正是为此准备的，结果连它一起没了。分片从"卡住"降级成"消失"，定位只能靠猜 —— 这正是本轮在 `unit-00` / `root-4` 上花掉十个小时的原因。
攻击路径: 前置条件 — 无（这是测试基础设施与进程管理的缺陷，不是可被外部触发的漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 任何一次让 `kill_process_group` 收到 mock 进程、或收到未脱离本进程组的子进程的调用；CI 侧则是任何一个真正卡住的分片；可观测后果 — 单测在朝 PID 1 的进程组发信号（开发机上被 `EPERM` 吞掉），而一个卡住的分片在 40 分钟后被 job 超时取消、不留日志，定位它的人拿不到任何指向具体帧的信息。
修复: 两处都改，范围只到让契约成立。`backend/utils/process.py` 新增 `_child_pid`，要求 pid 是**真正的 `int`**（`bool` 单独排除，因为它是 `int`，而 `True` 又会解析回 PID 1）；取不到就整个函数变成 no-op。`kill_process_group` 随后比对 `os.getpgrp()`，目标组是自己就不发信号 —— 与 `bounded_subprocess` 早已有的那条约束对齐。`backend/tests/test_process_utils.py` 新增四个用例：`MagicMock` 进程不得成为 `killpg` 目标（断言的是**调用**而不是结果，因为落地与否取决于权限，而调用才是缺陷本身）、pid 是字符串时同样不是、自己的进程组永不被发信号、以及**真实子进程仍然被杀**（两条新护栏都是拒绝输入的，这条钉住它们没有把函数悄悄关掉）。四个用例已逐一验证：拆掉 `_child_pid` 两个转红，拆掉 own-group 护栏一个转红。CI 侧把四段拼成一段：`timeout --foreground --signal=USR1 --kill-after=30s` 前台跑 pytest，输出重定向到文件、stdin 接 `/dev/null`。三个性质合起来就是全部修复：`timeout` 是这一步 shell 的普通前台子进程，所以 shell 一定返回、这一步一定结束，`if: always()` 的上传因此一定发生；没有 `wait`、没有 `tail -f`、没有后台 subshell，也就没有任何东西能攥着 runner 的输出管道不放（pytest 的输出进了文件，泄漏的子进程继承的是文件而不是管道，`tee` 楔子的机制被彻底移除）；`--signal=USR1` 让 `ci_process_guard` 在预算到点时把每个线程的 C 栈写进 `CI_STACK_DUMP_PATH`，`--kill-after` 给它时间写完。`--foreground` 是必需的：没有它 `timeout` 发的是整个进程组，泄漏的子进程没有 SIGUSR1 handler，会当场死掉并带走 pytest 正阻塞其上的那个 EOF，分片就结束了、dump 反而没写出来 —— 被拍的东西必须比照片活得久。同一条 `timeout` 也用在 staircase lane 上。门禁 `backend/tests/static_gates/test_ci_lanes_match_marker_contracts.py` 的三条 watchdog 断言随之重写为守**不变量**而非旧写法：预算存在且小于步骤与 job 两个上限、`--signal=USR1` + `--foreground` + `--kill-after` 三件齐全且 dump 文件在上传清单里、以及 `tee` / `tail -f` / 裸 `wait` / `setsid` 跑 pytest 四种形状一个都不许出现。顺带修掉这个门禁自己的一个假阳性：它原来用 `"setsid" in step["run"]` 找步骤，于是新写的注释里只要提到 `setsid` 就会把一条不再跑 pytest 的步骤认成目标；现在按预算正则匹配，并在做形状检查前先剥掉注释行。八种改坏方式（去掉 `--foreground` / 去掉 `--kill-after` / stdin 不接 `/dev/null` / 改回 `| tee` / 预算改成大于步骤上限 / pytest 塞回 `setsid` / 删掉 dump 环境变量 / 加回后台 `tail`）已逐一验证会失败，且合法的 `tail -n 40` 不误报。`backend/tests/unit/test_coding_tool.py` 里那个唯一没有 `@patch` 覆盖的 `subprocess.run` 补上 `timeout=60` —— 同文件另外 28 处 `Popen` 都被 mock 罩着，唯独它是真的起进程，没有 timeout 就意味着 `communicate()` 永远等一个不会来的 EOF。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/test_process_utils.py backend/tests/unit/test_coding_tool.py backend/tests/static_gates/test_ci_lanes_match_marker_contracts.py -q
```
---

### Finding ENTRY-037
档位: should-fix
问题: **合并门禁是一份「只报告不阻止」的清单：它列出的 9 个 required check 全部位于某个 job 的下游，于是 GitHub 把它们全部跳过，而被跳过的 required check 算通过。** GitHub 判定 required status check 通过的条件是 `success` / `skipped` / `neutral` 三者之一；而一个依赖失败 job 的下游报的是 `skipped`，不是 `failed`。因此「把下游设成 required 就等于覆盖了上游」这个推断是错的，`needs:` 链看上去闭合了这个洞，实际上没有。配置时的意图写得很明确（四个 check 是为了「unit 被传递性覆盖」），实际效果是：`unit-tests` 任一分片变红 → `coverage-gate` / `integration-tests` / `e2e-on-demand` / `existing-test` 全部 skipped → 全部算通过 → 合并照常进行。第二处独立缺陷在同一份清单的另一端：**`test_json_cleanup.yml` 的 `pull_request` 触发带 `paths:` 过滤**，而它的 job 是 required check。GitHub 的规则是：被路径过滤跳过的 workflow，其 check 永远停在 `Pending`，而 Pending 的 required check 会卡住每一次合并，且没有任何操作能让它变绿。两者叠加的实际后果是，一个只改 `.github/workflows/ci.yml` 的 PR **永远无法合并** —— 这不是「门禁被绕过」，是「门禁无法被满足」，而它看起来像 CI 还在忙。第三处：`.github/workflows/pages.yml` 里 `build (strict)` 一节写着「the PR cannot merge」，但它既不是 required check、又带 `paths:` 过滤，两个条件各自都足以让那句话不成立。
影响: 三处都是「一个会照字面相信就出错的东西」，而它们的形状一致：报出来的东西看起来像保护，实际不保护。第一处让红色单元分片完全不阻止合并；第二处让合并按钮永久停在不可用，而诊断一个「CI 迟迟不变绿」的人会先怀疑 CI 卡住，不会想到是 required check 根本没报；第三处让文档严格构建的「PR 不能合」只是一句注释。
攻击路径: 前置条件 — 无（这是 CI 配置缺陷，不是可被外部触发的漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 第一处，任何让 `unit-tests` 变红的改动；第二处，任何只触及 `ci.yml` 或 `backend/tests/static_gates/` 的改动；第三处，任何只改 `docs/` 之外的文件的改动；可观测后果 — 合并在 unit 红的情况下照常发生，或在只改了无关文件的 PR 上永远无法发生。
修复: 加一个终端 job `merge-gate`，形状是 GitHub 官方给的那个：`if: always()` + 读 `needs.*.result`，凡不是 `success` 一律 `exit 1`，并把 branch protection 的 required 清单加上它。`always()` 是承重的那一句 —— 没有它门禁自己会被 skip，而 skip 算通过，门禁会在最需要它的时候打开；`skipped` 在门禁内部也不放过，因为这 9 个 job 在 PR 上出现 skip 只可能是上游出事或 `if:` 被改窄。dispatch/schedule 专用的三个 job（`unit-staircase` / `nightly-regression` / `real-plan-migration`）不进 `needs`：它们在 PR 上不报点，required 一个永远不出现的 check 会把合并永久卡死。`test_json_cleanup.yml` 与 `pages.yml` 的 `pull_request` `paths:` 过滤一并去掉 —— required check 与路径过滤互斥，只能留一个，留 check。`pages.yml` 的 `build (strict)` 补进 required 清单，让它那句「the PR cannot merge」变成真的。新增门禁 `backend/tests/static_gates/test_merge_gate_contract.py`，它**执行**门禁的 `run:` body 而不是扫字符串（门禁是 `NEEDS_JSON` 的纯函数，无网络无文件系统，本机与 runner 一样可测），四个场景钉死：全绿放行 / 有 `failure` 拦 / 有 `skipped` 拦 / `cancelled` 也拦；另有一条钉住「新增的 PR job 必须进 `needs`」，清单是从 workflow 按 `if:` 含不含 `pull_request` **推出来**的，不是写死的。第一版这个门禁是扫字符串的，断言 `"exit 1" in source`，把真的 `sys.exit(1)` 改成 `sys.exit(0)` 之后**依然全绿** —— 因为 shell 兜底里还留着第二个 `exit 1` 字样；改执行之后 8 个变异（删 `always()` / 从 `needs` 删 job / 塞入 dispatch-only job / 放行 `skipped` / `sys.exit(1)→0` / shell `exit 1→0` / `if` 去掉 `pull_request` / job 改名）全部变红。门禁自己的汇总行也在第一次真跑时被查出 `${#NEEDS_JSON}` 报的是 JSON 字符串的**字符长度**：9 个 job 打印成「All 620 upstream jobs succeeded」，已改为由 python 从它自己那份 parse 出数。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/test_merge_gate_contract.py -q
```

---

### Finding ENTRY-038
档位: should-fix
问题: **两件「看起来在保护、实际不在保护」的事，都不在测试集里。** 第一件：那 200 多条静态契约是 pytest 文件，因此跑在 `root` lane 里、被拆进 `root-0..root-6`，**与另外 19 个分片并行**；`unit-tests` 的 `needs` 也只有 `grep-guard`。于是 `lint`、`commit-hygiene`、`commit-range-privacy` 任何一个红，二十个分片照起；而一条静态契约失败到达时，二十台 runner 早已承诺跑完。契约在集合里，只是不在任何东西前面。第二件：**套件从未以不同顺序跑过，因此「全绿」不是与顺序无关的证据。** `server` 持有 `_verification_state` 与 `_execution_state` 两个进程级字典，十五个与十三个测试文件分别以裸 `dict[key] = ...` 写入（同函数里的 `monkeypatch` 管的是别的东西，不会撤销这些），于是每条用例都把条目漏给同进程后面所有用例。具体到一处：`test_round_start_liveness_state` 自己塞一个终态条目（`loop_stopped`）因而期望 `get_active_tasks()` 报 0，**而那个计数跨越整个字典**——前面任何一条留下的非终态条目就把它从 0 翻成 1。它在自然位置上一直是绿的。
影响: 第一件把门禁的顺序属性整个抵消掉：报告仍然到达，但代价是整个矩阵，而定位「CI 还没变绿」的人首先想到的是 CI 卡住，不会想到静态门禁本来应该更早说话。第二件让一类只在特定顺序下发作的失败完全不可见——它已经发作过一次，只是恰好发作在没人跑过的顺序上。两条的形状一致：报出来的东西看起来像保护，实际不保护。
攻击路径: 前置条件 — 无（CI 配置与测试隔离缺陷，非可被外部触发的漏洞；列出它是为了让本轮改动可归因）；触发步骤 — 第一件，任何让 bandit / commit 卫生 / 隐私扫描失败的改动；第二件，任何让某条用例在 `_verification_state` 里留下非终态条目的改动被排在另一条之前；可观测后果 — 静态门禁红了仍烧掉 20 台 runner；或一条断言在自然顺序下永远绿、在换一个顺序后失败，而 CI 每次都跑自然顺序。
修复: 两件分开做。**其一**，把 `backend/tests/static_gates/` 抽成独立的 `static-gates` job（单 VM、无分片、无 coverage 插桩；`.coveragerc` 的 `omit` 本就含 `tests/*`，所以 80% 聚合不变），`unit-tests.needs` 扩到 `[lint, grep-guard, commit-hygiene, commit-range-privacy, static-gates]`，`root` lane 显式排除 `tests/static_gates` 避免重复跑，`static-gates` 进 `merge-gate.needs`。顺序本身也被钉住：新增断言要求凡是跑 `tests/` 且属于 PR 门禁的 job 必须能沿依赖边走到 `static-gates`，范围限定在 PR 是刻意的——`nightly-regression` 跑在 `schedule` 上而 `static-gates` 在 schedule 上是 skip 的，让它依赖等于让它永远 skip。**其二**，`tests/conftest.py` 新增 autouse 的 `isolated_runtime_plan_state`（每条用例前清空、结束后按快照还原；浅拷贝且刻意如此，因为值里持有线程句柄与取消令牌），并让分片在执行前用 `random.Random(seed).shuffle` 重排 `$TARGETS`，种子取自 `github.run_id.run_attempt.shard` 并打进日志。分片归属保持确定——跨分片本就在不同 runner 上不可能互相影响，要变的是同一进程内的先后。四个与五个变异分别已验证会失败：删 `unit-tests.needs` 里的 `static-gates` / 删 `merge-gate.needs` 里的 `static-gates` / `if` 去掉 `pull_request` / `root` lane 不再忽略；以及把 `pytest $EXEC_ORDER` 改回 `$TARGETS` / `shuffle` 换成 `sorted` / 删掉 `shuffle` 调用 / 种子不来自 run / 不打印种子。形状断言一律先剥整行注释再匹配：子串匹配会命中**注释里的词**（`shuffle`、`exit 1` 都能出现在解释性文字中）——这个仓库在 watchdog 门禁上踩过同一个坑，现已先剥整行注释再判形状。
验证方式:
```bash
backend/.venv/bin/python3 -m pytest backend/tests/static_gates/ -q
backend/.venv/bin/python3 -m pytest backend/tests/unit -m "not e2e and not integration and not time_sensitive and not perf" -q
```

---

## Appendix A — modified tests

The audit policy permits modifying existing tests that encoded
unsafe behavior — for example, an assertion that accepted a response
even when the request-guard header was missing.  The risk is
symmetric: removing such an assertion (silently or by accident) can
make a regression look green.  Without a written record of *why* an
assertion was changed, a future contributor has no way to tell a
legitimate "tighten the assertion" edit from a careless or
malicious "loosen the assertion" edit.

Every modification to an existing test function during this audit
round therefore lands here as one row per `path::function`, with
three required fields:

* **test_id** — `backend/tests/.../test_x.py::test_y`
* **original** — the pre-modification assertion text
* **reason** — why the original assertion was unsafe
* **replacement** — the post-modification assertion text

The body may legitimately be empty when no test was modified in a
given round (the schema gate tolerates that case).  The header row,
however, is mandatory — removing it makes the meta-test's
`test_appendix_a_table_exists` fail loud.  The bidirectional
consistency with the actual git diff is enforced by
`backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py`,
whose `parse_appendix_a(text) -> list[dict]` is the public
interface downstream tasks (e.g. task 16) depend on.

| test_id | original | reason | replacement |
|---|---|---|---|
| backend/tests/unit/test_ids.py::test_valid_plan_id_round_trips | Parametrize row `"X" * 200` asserted the validator round-trips a 200-character id. | The 200-character case encoded the pre-cap behaviour: `validate_plan_id` had no length guard, so any id that passed the byte-set check (every char `[A-Za-z0-9._-]`) was accepted. ENTRY-015 closes that hole with `MAX_PLAN_ID_LEN = 64`; the 200-char row now trips the new guard and would fail loudly, so it must drop out of the acceptance table. | Parametrize row `"X" * MAX_PLAN_ID_LEN` keeps one long-but-valid sample at the boundary (inclusive lower edge), so a future bump to the constant still has a parametric acceptance fixture to anchor against. |
| backend/tests/unit/test_ids.py::test_extreme_length_is_rejected | Originally `test_extreme_length_can_be_accepted`; asserted `validate_plan_id("a" + "b" * 1000 + "c") == "a" + "b" * 1000 + "c"` — i.e. a 1002-character id round-trips. | The original function name and assertion encoded the pre-cap behaviour. After ENTRY-015 lands, the same input raises `InvalidPlanIdError`; leaving the original assertion in would re-open the boundary that the cap exists to close. The original docstring ("very long ids are accepted as long as every byte is in the safe set") is exactly the safety-bypass phrasing Appendix A's row exists to record. The function was renamed so a future grep for the old name returns nothing rather than silently referring to a now-rejection-flavoured body. | `test_extreme_length_is_rejected` asserts `validate_plan_id("a" * n)` raises `InvalidPlanIdError` for `n` in `(MAX_PLAN_ID_LEN + 1, MAX_PLAN_ID_LEN + 100)`. The unit layer now pins the *rejection* side of the 64/65 boundary; the matching acceptance-side pin lives in `backend/tests/security/test_plan_id_boundary_matrix.py::test_max_len_boundary_is_exact`. |
| backend/tests/unit/test_verification_plan_wide_concurrency.py::test_the_group_product_is_bounded_by_the_declared_ceiling | Asserted `agent._peak == ceiling` after `asyncio.run(agent.execute_verification_plan_async(plan))`, where `_peak` is set by `_ConcurrencyProbe._run_single_vp_async` and the probe is built by the synchronous fixture `agent_factory._make`. The probe's original `__init__` ran `self._lock = asyncio.Lock()`, which on Python 3.9 calls `get_event_loop()` and raises `RuntimeError: There is no current event loop in thread 'MainThread'`; on 3.9 the assertion never executed at all — the four `agent_factory(...)`-driven tests all died at construction time with `RuntimeError` instead of running their asserts. | The plan-wide ceiling has no behavioural pin while the probe cannot be constructed. Leaving the lock in `__init__` keeps the suite permanently red on the project's venv (Python 3.9) and silently drops the safety property the test exists to pin. Loosening the assertion (e.g. to `peak <= ceiling` or to a generous `peak < ceiling + slack`) is not an option: `peak == ceiling` is exactly what proves the bound *binds*, not merely that it does not blow past it. | Assertion text is unchanged: `assert agent._peak == ceiling, (f"peak in-flight was {agent._peak} for 60 VPs across 6 groups at cap 10 — the per-group caps multiplied past the declared ceiling")`. The fix lives in the helper class `_ConcurrencyProbe` only: `self._lock` is now `None` in `__init__` and is created lazily inside `_run_single_vp_async` (`if self._lock is None: self._lock = asyncio.Lock()`) where a running loop is guaranteed. The lock itself is decorative — `_active += 1` and `_peak = max(...)` run between no `await` points under single-threaded asyncio, so no other coroutine can interleave — but it is kept so a future reader who adds awaits inside the critical section cannot silently break the measurement. Lazy construction also works on 3.10+ where `Lock.__init__` no longer reaches for a loop, so the same code is portable across both interpreters. The docstring of `_ConcurrencyProbe` now states the reasoning so a future contributor does not "simplify" it back into a crashing form. |
| backend/tests/e2e/test_agent_file_conflict.py::test_no_lock_leak | Computed `locks_dir = project_dir / ".pdt" / "locks"` from the project root and asserted the path existed. | Lock files were moved out of `<project_dir>/.pdt/locks/` to the agent's own `_locks_dir` (workspace-derived, outside the project tree) so `git add -A` checkpoints could no longer commit them. Recomputing the formula in the test pins a location instead of the wiring — a future move of the lock directory would leave the assertion green against an empty path while production writes elsewhere. | Reads `locks_dir = agent._locks_dir` and asserts `locks_dir.exists()`. The assertion tracks whatever location the wiring chose this round, so the test fails loud if the production-side refactor breaks the hook entirely but tolerates a future move. |
| backend/tests/integration/test_agent_lock_hooks.py::test_pre_task_acquires_locks | Called `_lock_file_path(project_dir, file_path)` — a local helper that sha256-hashed the normalised path under `<project_dir>/.pdt/locks/`. | Same location move as `test_no_lock_leak`: recomputing the lock-file path here would diverge from what the agent actually wrote. The test would pass against an empty or stale path while production wrote elsewhere. | Calls `_lock_file_path(agent, file_path)`, which delegates to `file_lock_protocol.lock_file_path(agent._locks_dir, agent.project_dir, file_path)` — the production-side resolver. The assertion still requires `lock_path.exists()`; only the location-discovery step is asked of production. |
| backend/tests/integration/test_agent_lock_hooks.py::test_post_task_releases_locks | Asserted `_lock_file_path(project_dir, "backend/agent.py").exists()` against the recomputed `<project_dir>/.pdt/locks/...` location. | Same reasoning as the previous row: recomputing here pins a moved location. The post-task hook's contract is "the lock the agent wrote this round exists", not "the lock at the old project-root location exists". | Asserts `_lock_file_path(agent, "backend/agent.py").exists()` — the agent's own location. The companion release assertion (`fresh = FileLockManager(); fresh.acquire(...)`) is unchanged and still proves the OS-level lock is free. |
| backend/tests/unit/test_coding_tool_settings_injection.py::test_coding_tool_settings_file_exists_at_startup | Required the on-disk settings `env` block to carry `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` and `ANTHROPIC_DEFAULT_OPUS_MODEL` in addition to the endpoint and credential fields. | Those three keys are written by `ClaudeCodingTool` only when the operator's own shell exports them — model management is delegated to CC Switch by design (see `SubagentConfig.to_settings_dict`). Requiring them made the assertion a function of the machine: it passed on a configured developer workstation and failed on a clean runner with `on-disk env block is missing SDK-required key(s)`, i.e. the test asserted a property of the developer's environment, not of this code. | `sdk_required` is narrowed to the three fields the tool writes unconditionally — `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`. The tier keys stay covered by the allowlist assertion immediately below (`env_keys - tool_owned`), which is the property that actually matters: nothing the tool does not own leaks into the file. The test now passes both with and without those variables exported, verified by running it under `env -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u ANTHROPIC_DEFAULT_OPUS_MODEL`. |
| backend/tests/integration/test_agent_lock_hooks.py::test_in_memory_conflict_check | Asserted `_lock_file_path(project_dir, "backend/agent.py").exists()` to confirm task A's locks remained on disk after task B was rejected. | Same reasoning: the test must ask production where the lock landed, not recompute the formula against the moved location. | Asserts `_lock_file_path(agent, "backend/agent.py").exists()` — same shape, asked of the agent. The in-memory conflict check and its rejection message (`RuntimeError, match="File conflict detected"`) are unchanged. |
| backend/tests/integration/test_agent_lock_hooks.py::test_agent_broker_binds_a_socket_and_stops_on_demand | New test added in this round — no prior version. | The file-lock broker was previously started lazily from `_pre_task_lock_hook`. Any caller using the hook without `agent.run` (every test in this file, plus any embedded use) spawned a thread nothing reclaimed; the conftest's "no worker outlives its test" guard surfaced the leak. A test that exercises the lifetime end-to-end (start + stop + socket-gone) is the only way to pin the fix. | Constructs the agent via the existing `_make_agent` fixture, calls `_ensure_lock_broker()`, asserts the broker is live and `socket_path(project_dir)` exists, then calls `_stop_lock_broker()` and asserts the socket is gone. The fixture is unchanged; only the assertion scaffolding is new. |
| backend/tests/integration/test_agent_lock_hooks.py::test_active_broker_never_starts_one | New test added in this round — no prior version. | Reading the active broker used to silently start one. The "ask whether a broker is running" code path needs a regression test, otherwise a future refactor that lazily starts on `_active_lock_broker()` would silently leak threads again. | Asserts `agent._active_lock_broker() is None` and `agent._lock_broker is None` after `_make_agent` (no `_ensure_lock_broker` call), then drives a `SubTask` through `_pre_task_lock_hook` / `_post_task_lock_hook` on the single-process fallback to prove the hook still works without the broker. |
| backend/tests/integration/test_file_lock_manager.py::test_lock_file_location | Asserted `str(locks[0]).startswith(str(project_dir / ".pdt" / "locks"))` and that the filename ends with `.lock`. | Lock files were moved out of the project tree (a git working tree) for the same reason as `test_agent_file_conflict.py`. Pinning the literal `.pdt/locks/` prefix in the test would re-open the safety boundary that the move exists to close — and would silently pass against a stale on-disk lock if production moved again. | Asserts `project_dir not in locks[0].parents` (property: "outside the project tree") and `locks[0].name.endswith(".lock")` (filename shape). The negative containment check tracks the move without naming the new location. |
| backend/tests/integration/test_file_lock_manager.py::test_lock_dir_can_be_pinned_to_the_plan | New test added in this round — no prior version. | `FileLockManager.acquire` now takes a `locks_dir` parameter; the executor pins `plans/<plan_id>/locks/` and hands the same directory to the broker and the manager. Without a test that pins a specific dir to a plan, a future refactor could silently regress to the workspace-only fallback and re-open the leak `test_lock_file_location` exists to close. | Builds a `plan_dir` under `tmp_path`, calls `locks_dir_for_plan(plan_dir)`, then `flm.acquire(["src/a.py"], str(project_dir), locks)`. Asserts exactly one `.lock` file lands under `locks` and that `project_dir not in written[0].parents` (the workspace must not be touched). |
| backend/tests/static_gates/test_no_module_path_outside_the_repo.py::test_no_production_module_paths_point_outside_the_repository | Built `allowed = _application_roots() ∪ set(sys.stdlib_module_names) ∪ THIRD_PARTY_ALLOWED` directly off `sys.stdlib_module_names`. | `sys.stdlib_module_names` is missing on Python 3.9 (the project's venv); reading it directly raises `AttributeError` and crashes the gate on the CI runner before it inspects a single file. The gate must work on the project's venv or it does not run at all. | Builds `allowed = _application_roots() ∪ set(_stdlib_module_names()) ∪ THIRD_PARTY_ALLOWED`. The new helper `sys.stdlib_module_names` is used on 3.10+ and a `sysconfig.get_path('stdlib')` walk is the fallback on 3.9. The downstream walk of `_production_files()` / `_literal_import_targets` is unchanged. |
| backend/tests/static_gates/test_no_module_path_outside_the_repo.py::test_the_gate_would_catch_the_shape_it_was_written_for | Built `allowed = _application_roots() ∪ set(sys.stdlib_module_names)` for the synthetic-snippet check. | Same Python-3.9-compat reason as the sibling test; without the helper, the gate cannot run on the project's venv. | Builds `allowed = _application_roots() ∪ set(_stdlib_module_names())`. The "tools is not allowed, notifications is allowed, sys is allowed" assertions are unchanged — only the stdlib source flips. |
| backend/tests/test_edit_write_containment_guard.py::test_guard_is_wired_into_settings_file | Asserted `"sys.exit(2 if blocked else 0)" in body` against the settings-file body string. | The `edit_write_containment_guard.py` script gained a second stage (the file-lock broker) and its single-line `sys.exit(2 if blocked else 0)` was split into two branches (`blocked = not allowed` then `sys.exit(2)`). Pinning the literal phrasing pins one spelling of the wiring, not the safety property; future refactors would force this test to follow. | Asserts both `"blocked = not allowed" in body` and `"sys.exit(2)" in body` independently, so the script can grow (more branches, separate stages) without forcing this assertion to track each spelling. The exit code (2) and the containment decision (the assignment to `blocked`) are the two invariants the test pins. |
| backend/tests/unit/test_backend_prompts.py::test_api_test_table_covers_every_subject | New test added in this round — no prior version. | The api_test spec table in `VERIFICATION_PLAN_SYSTEM_PROMPT` is the only place the LLM learns the canonical subject vocabulary. Earlier rounds taught a smaller set than the runner accepted, or vice versa. Pinning the spec table against `verification_api_runner.SUBJECT_KEYS` means a future vocabulary change in either direction surfaces here as a red test, not a silently-drifting prompt. | Anchors the section on the existing prompt heading `"api_test 断言规范"`, slices to the `"**VP 的判定依据"` marker, and asserts every key in `SUBJECT_KEYS` appears as a literal token (`f'"{subject}":'`) in that slice. The error message names the missing subject and the canonical fix (add a row to `_API_TEST_SUBJECT_DISPLAY` or remove from `SUBJECT_KEYS`). |
| backend/tests/unit/test_backend_prompts.py::test_api_test_table_uses_self_contained_marker_consistently | New test added in this round — no prior version. | Self-contained subjects (`status`) need the `自带期望值` footer in the prompt; non-self-contained subjects need `必须 带比较符`. A row missing its marker makes the LLM emit `{"status": 200, "equals": 200}` and bounce off the schema validator, or `{"json_path": ...}` with no comparator and silently pass the gate. Pinning the marker per row closes both holes. | For each subject in `SUBJECT_KEYS`, locates the row by its `f'"{subject}":'` marker and asserts the row carries `自带期望值` when the subject is in `SELF_CONTAINED_SUBJECTS`, else carries `必须` and `带比较符`. |
| backend/tests/unit/test_backend_prompts.py::test_api_test_table_lists_every_comparator | New test added in this round — no prior version. | The api_test comparators line is the only place the LLM learns which comparator spellings are valid; a missing comparator means a plan that uses it (e.g. `length_lte`) is generated under a vocabulary the validator rejects (`永远判不了`). Pin it against `verification_api_runner.COMPARATOR_KEYS`. | Asserts every comparator in `COMPARATOR_KEYS` appears as a literal token in the api_test spec-table slice. The error message names the missing comparator and the canonical fix (add to `COMPARATOR_KEYS` first, then re-export from `prompts._api_test_comparator_table`). |
| backend/tests/unit/test_backend_prompts.py::test_api_test_table_keys_are_all_in_vocabulary | New test added in this round — no prior version. | The api_test spec table is a single-source-of-truth contract: every key it teaches must be in `SUBJECT_KEYS ∪ COMPARATOR_KEYS`. A fifth spelling (e.g. `body_not_contains` as a subject) re-opens the VP-007 hole — the LLM emits a VP the runner cannot grade, the schema gate stays silent, and execution proceeds to mark the VP FAILED. This test makes the closed-set gate round-trip through the prompt surface too. | Runs `re.findall(r'"([a-z_]+)":', mod._API_TEST_TABLE)` against the table constant and asserts the result is a subset of `SUBJECT_KEYS ∪ COMPARATOR_KEYS`. The error message names the offending keys and the fix path (`_API_TEST_SUBJECT_DISPLAY` in prompts.py or the runner vocabulary). |
| backend/tests/unit/test_verification_api_runner.py::test_vocabulary_is_exported_as_public_constants | New test added in this round — no prior version. | Earlier rounds hand-copied the vocabulary into prompts.py; whenever the runner trimmed, the prompts drifted and the cycle restarted. Exporting the vocabulary as public module attributes (`SUBJECT_KEYS`, `COMPARATOR_KEYS`, `SELF_CONTAINED_SUBJECTS`) closes the loop: `from verification_api_runner import SUBJECT_KEYS` is the contract, and the closed-set gate can round-trip through the same attribute the prompt uses. | Asserts `SUBJECT_KEYS`, `COMPARATOR_KEYS`, `SELF_CONTAINED_SUBJECTS` are reachable as public module attributes, and `SELF_CONTAINED_SUBJECTS ⊆ SUBJECT_KEYS` (the self-contained set is a subset of the subjects, not a parallel axis). |
| backend/tests/unit/test_verification_api_runner.py::test_docstring_assertion_vocabulary_lists_every_subject | New test added in this round — no prior version. | The module docstring's "Assertion vocabulary" section is the canonical doc the prompts mirror. A new subject added to `SUBJECT_KEYS` but not added to the docstring would land in runners the docs never mentioned — exactly the drift this audit round is closing. | Asserts the docstring contains an "Assertion vocabulary" section, then asserts every key in `SUBJECT_KEYS` appears as a token in the docstring. The error message names the missing subject and frames the divergence as "vocabulary table vs. runner" — i.e. one of them is wrong, both cannot be right. |
| backend/tests/unit/test_verification_api_runner.py::test_validate_vp_rejects_unknown_subject | New test added in this round — no prior version. | A typo / unknown subject must come back as a schema issue, not a silent pass. The vocabulary is supposed to be a closed set, so any assertion key outside `SUBJECT_KEYS` (e.g. `body_doesnt_contain`, `response_json`) cannot pass `validate_vp`. Without this gate the runner would mark an unknown-subject VP FAILED at execution time, and the operator would never see the planner was emitting the wrong spelling. | Picks an unknown key from `("body_doesnt_contain", "response_json", "totally_made_up")` that is not in `SUBJECT_KEYS`, builds a VP with one assertion using it, and asserts `validate_vp` returns issues whose `detail` contains the phrase "has no subject". |
| backend/tests/unit/test_verification_api_runner.py::test_the_traversal_vp_shape_is_expressible | New test added in this round — no prior version. | This is the regression pin for VP-007. The traversal criterion's body half ("the response must not hand back `/etc/passwd`") had no subject to name while the vocabulary was asymmetric — `json_path` / `header` both accepted `not_contains`, but the raw body accepted only `body_contains`. A planner reaching for the symmetric spelling `body_not_contains` got the **whole VP** rejected at schema time, the valid `{"status": 404}` assertion included, and the VP sent no request at all for four consecutive rounds. | Builds the exact VP-007 assertion pair — `{"name": "穿越序列被拒为 404", "status": 404}` plus `{"name": "响应体不是系统文件内容", "body_not_contains": "root:x:0:0"}` — and asserts `validate_vp` returns zero issues. The assertion under test is that the *pair* survives validation, because the earlier failure voided both halves at once. |
| backend/tests/unit/test_verification_api_runner.py::test_body_not_contains_fails_when_the_substring_is_present | New test added in this round — no prior version. | Anti-vacuity control for the new subject. "The vocabulary accepts the spelling" is not the same as "the spelling decides something": a `body_not_contains` that always returned found-and-passing would satisfy every schema test and verify nothing. | Drives the runner against a live local HTTP fixture whose body genuinely contains the probe substring (`{"error": "not found"}`). Asserts the verdict is FAILED and that the reason says "NOT to contain", so the negative comparator is shown to be evaluated rather than short-circuited. |
| backend/tests/unit/test_verification_api_test_wiring.py::test_api_schema_violations_fail_loudly_after_retries_exhausted | New test added in this round — no prior version. | Earlier rounds kept the "best of bad" plan when the LLM kept emitting unrunnable api_test VPs and let the runner mark them FAILED at execution time. Persisting an unrunnable plan is worse than refusing outright — the orchestrator's only correct action on a schema violation is to surface it to the operator, not to let execution proceed. | Mocks `query_json` to always return a malformed plan (VP-007 with `body_doesnt_contain` as a subject), runs `generate_verification_plan(retry_llm=2)`, and asserts an exception is raised, that `query_json` was called exactly 2 times (every retry was tried), that the message names "schema" / "api_test" / "assertion" / "body_doesnt_contain", and that `verification_plan.json` is NOT persisted. |
| backend/tests/unit/test_verification_api_test_wiring.py::test_api_schema_violations_free_other_methods | New test added in this round — no prior version. | Service-reference and gate-gap violations can fall back to "best of bad" — they do not keep the runner from grading the VPs the way they are written. Only api_test schema violations make a VP literally un-runnable, so only they trigger the hard reject. Pinning this asymmetry prevents a future contributor from over-applying the reject (which would block plans with recoverable service-reference issues) or under-applying it (which would re-open the VP-007 hole). | Mocks `query_json` to return a plan with a single valid api_test VP, calls `generate_verification_plan(retry_llm=1)`, and asserts no exception is raised and the plan is returned. The service-reference violation is taken from `_service_reference_report` (a different code path), so this test pins the api_test-only asymmetry. |
| backend/tests/unit/test_verification_api_test_wiring.py::test_disk_plan_with_api_schema_violations_is_rejected_on_load | New test added in this round — no prior version. | The brief calls out "generation / loading" both; a hand-edited or stale plan on disk with api_test schema violations must NOT silently re-enter execution. Without this test the load path is unobserved and a future contributor could short-circuit the schema check for the cache path while leaving the generation-side gate intact. | Writes a malformed plan (`{"verification_points": [{"id": "VP-100", "verification_method": "api_test", "request": {"method": "GET", "url": "http://x/y"}, "assertions": [{"name": "bad", "totally_made_up_subject": "hi"}]}]}`) to `plan_dir/verification_plan.json`, runs `generate_verification_plan(retry_llm=1)`, and asserts an exception is raised. The mock is asserted to have been called 0 times — the load path short-circuits before Phase-1. |
| backend/tests/unit/test_subagent_write_tmp_settings.py::test_write_tmp_settings_path_under_tmp | `temp_root = Path(tempfile.gettempdir()).resolve()` then `assert temp_root in write_path.resolve().parents` — i.e. the payload must be somewhere below the machine's system temp root. | `private_dir()` gained a root override (`secret_files.PRIVATE_ROOT_ENV_VAR`) and the suite sets it, because every dispatch minted a `pdt-subagent-*` directory in the machine's temp root that nothing collects — the boot sweep is off under test by design, and a directory holding a payload is never eligible for the empty-directory prune. Leaving the assertion reading `gettempdir()` unconditionally would make the redirect itself look like a violation of the layout rule it exists to serve: the test would go red on exactly the change that stops the suite leaving residue behind. The property the test is actually for is "the payload lands in the private root we configured, never in the workspace or `Path.home()`" — the system temp root is only that root's default value. | Asserts containment against the **configured** root: `Path(os.environ.get(PRIVATE_ROOT_ENV_VAR) or tempfile.gettempdir()).resolve()`. The remaining assertions are unchanged and still pin the parts that are unconditional — absolute path, `pdt-subagent-` parent name, `0700` directory, `0600` file. The companion unit tests `test_private_dir_defaults_to_the_system_temp_root` and `test_private_dir_honours_the_root_override_and_stays_managed` pin both sides of the default/override split directly. |
| backend/tests/unit/test_redact_leaked_secrets.py::test_the_boot_sweep_does_both_halves | Assertion unchanged — `assert live.parent.exists()` — carrying the message "the redacted payload is the post-mortem copy this project keeps; the boot pass must not turn into a temp cleaner". | The boot pass gained a second removal rule (`prune_aged_residue`) which deletes a `pdt-subagent-*` directory past a three-month gate **whatever it holds**, so "must not turn into a temp cleaner" stopped being true of the pass as a whole, and the docstring's "for the life of the machine" stopped being true with it. The assertion itself is still exactly right and is the point of the test: a *freshly* redacted payload is the post-mortem copy and must survive an ordinary boot. Only the wording that described the pass had to move. | Same assertion, same condition. The message is reworded to "the redacted payload is the post-mortem copy this project keeps; the boot pass must not age out one that is still fresh", and the docstring names `prune_aged_residue` as what bounds the other half — so a future reader sees why the age gate is wide without having to read the sweep module. A docstring-only edit would not have been recorded here; the assertion *message* is inside the function body, which is what pulls this row in. |

## Appendix B — verification command inventory and necessary refactors

The appendix has two parts. **B.1** is the verification command
inventory — every `bash`-fenced pytest invocation referenced by an
entry in this document. The list is derived from `parse_entries` and
is checked for staleness at every release cut: a command that no
longer corresponds to a real test path is removed from the document
and from this appendix. **B.2** is the necessary-refactors table —
every first-party source-file change made during this audit round
that is not already attributed by a finding's `修复:` field. The
diff-attribution gate
(`backend/tests/static_gates/test_diff_is_attributable_to_audit_findings.py`)
walks `git diff --name-only HEAD`, classifies each changed path, and
requires every source-tree file in the diff to appear either in some
finding's `修复:` field (regex-extracted path token) or in the B.2
table.

### B.1 — verification command inventory

* ENTRY-001 -> `backend/tests/security/test_request_guard.py`
* ENTRY-002 -> `backend/tests/security/test_plan_id_containment.py`
* ENTRY-003 -> `backend/tests/security/test_request_guard_rejections.py`
* ENTRY-004 -> `backend/tests/test_provider_order_no_server_import.py`
* ENTRY-005 -> `backend/tests/test_grep_guard.py`
* ENTRY-006 -> `backend/tests/security/test_request_guard_rejections.py`
* ENTRY-007 -> `backend/tests/integration/test_ci_gate_reuse_contract.py::test_ci_definition_parses`
* ENTRY-008 -> `backend/tests/integration/test_ci_gate_reuse_contract.py::test_install_steps_are_unchanged`
* ENTRY-009 -> `backend/tests/integration/test_ci_gate_reuse_contract.py::test_grep_guard_step_still_present`
* ENTRY-010 -> `backend/tests/integration/test_ci_gate_reuse_contract.py::test_every_pytest_invocation_uses_project_venv`
* ENTRY-011 -> `backend/tests/integration/test_ci_gate_reuse_contract.py::test_e2e_job_runs_on_main_push`
* ENTRY-012 -> `backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py::test_no_dangerous_defaults_in_scripts`
* ENTRY-013 -> `backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py::test_run_tests_default_target_is_not_empty`
* ENTRY-014 -> `backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py::test_unquoted_expansion_is_detected`
* ENTRY-015 -> `backend/tests/security/test_plan_id_boundary_matrix.py`
* ENTRY-016 -> `backend/tests/unit/test_config_placeholders_are_neutral.py`
* ENTRY-017 -> `backend/tests/static_gates/test_credential_files_are_written_private.py`
* ENTRY-018 -> `backend/tests/static_gates/test_state_db_path_has_one_resolver.py`
* ENTRY-019 -> `backend/tests/static_gates/test_readme_onboarding_sections.py`
* ENTRY-020 -> `backend/tests/static_gates/test_public_security_docs_describe_defects_not_incidents.py`
* ENTRY-021 -> `backend/tests/static_gates/test_diff_is_attributable_to_audit_findings.py`
* ENTRY-022 -> `backend/tests/unit/test_subagent_config.py`

### B.2 — necessary refactors

Each row records a first-party source-file change made during this
audit round that is **not** attributed by the corresponding finding's
`修复:` field — the finding authorises the refactor's intent, but the
prose does not name the file, so the diff-attribution gate would
otherwise flag any future edit to the same file as unattributable.

The columns are:

* `path` — repo-relative path of the changed file
* `finding` — the audit finding whose `修复:` prose authorises the
  refactor; one of `ENTRY-*` / `CONF-*` / `ROUTE-*` / `CI-*`
* `refactor` — what was actually done to the file
* `minimal_reason` — why this refactor was the minimum needed to
  satisfy the finding without expanding scope

A row whose `finding` column does not name a finding that exists in
the main body of this document fails the gate's
`test_appendix_b_entries_name_a_finding` check (the prefix regex
alone is not enough — see the test source for the exact contract).

| path | finding | refactor | minimal_reason |
|---|---|---|---|
| `backend/request_guard.py` | ENTRY-001 | New module hosting `RequestGuard` ASGI middleware. | Class moved here; listing the path keeps the gate honest when the prose stops naming it. |
| `backend/routes/__init__.py` | ENTRY-002 | Empty marker module re-exporting the extracted routes. | Single import surface enforces late-binding per CLAUDE.md. |
| `backend/routes/execution.py` | ENTRY-002 | Lifted out of `server.py`; exposes `_server.execution_start` handle. | Same reasoning as `routes/__init__.py`; keeps the `project_dir` refusal local. |
| `backend/routes/phases.py` | ENTRY-002 | Lifted out of `server.py`; phase-transition handlers. | Co-extracted handler; one row prevents future half-orphan splits. |
| `backend/routes/plans.py` | ENTRY-002 | Lifted out of `server.py`; plan-list and plan-status routes. | Same reasoning as `routes/execution.py`. |
| `backend/routes/verification.py` | ENTRY-002 | Lifted out of `server.py`; verification-status routes. | Pairs with `verification_loop.py`; one row pins both moves. |
| `backend/routes/debug.py` | CI-001 | Optional debug router behind `PDT_DEBUG_ROUTES`. | Mounting order is load-bearing; row keeps the before-catch-all contract. |
| `backend/verification_loop.py` | ENTRY-011 | Lifted out of `server.py`; background poll-and-verify cycle. | Function the e2e `needs:` graph anchors on; row lets the integration test patch it. |
| `backend/server.py` | ENTRY-001 | Hosts `_PROJECT_ROOT` / `_BACKEND_DIR` and `_server.<name>` handles. | Referenced from many routes; one row covers all constant additions. |
| `backend/framework/ids.py` | ENTRY-015 | Added `MAX_PLAN_ID_LEN = 64` constant; `validate_plan_id` checks it. | Gate's path extractor confirms the prose citation rather than re-deriving it. |
| `scripts/run_tests.sh` | ENTRY-012 | Lifted options to `set -euo pipefail`; retargeted default to `backend/tests`. | One line covers ENTRY-012 and ENTRY-013; row lets the gate accept either. |
| `example/provider_capacity.yaml.example` | ENTRY-016 | Every `pattern:` carries the `^Example ` placeholder prefix. | Operator-template example edited to neutralise leaked provider names. |
| `example/provider_routing.yaml.example` | ENTRY-016 | Every tier entry carries the `^Example ` placeholder prefix. | Same gate, separate files; same reason as capacity example. |
| `.github/workflows/ci.yml` | ENTRY-007 | Non-empty `jobs:`, `pip install -r backend/requirements.txt`, venv pytest prefix. | ENTRY-007 to ENTRY-011 share one file; one row covers all five. |
| `.github/workflows/test_json_cleanup.yml` | ENTRY-037 | `pull_request` trigger carries no `paths:` filter. | It is a required check; a path-filtered required check stays pending forever. |
| `.github/workflows/pages.yml` | ENTRY-037 | `pull_request` trigger carries no `paths:` filter; `build (strict)` is required. | The header claims a rotten link blocks the PR; two separate conditions kept that false. |
| `.github/workflows/ci.yml` | ENTRY-038 | `static-gates` job is upstream of every PR lane; shards execute in a run-seeded shuffled order. | Shares one file with ENTRY-007..011; one row per concern keeps the reason a change was made attributable. |
| `frontend/api.js` | ENTRY-001 | Shared `api()` wrapper injecting `X-PDT-Request: 1`. | Prose names the header not the file; row keeps `api.js` attributable. |
| `frontend/app.js` | ENTRY-001 | Calls `api()` instead of bare `fetch`; partial migration tracked here. | ENTRY-003 names the migration; row records the touched file. |
| `.gitignore` | ENTRY-016 | Confirms `.config/` is ignored and no tracked file lives under it. | Rule set here; row keeps the gate honest if the ignore drifts. |
| `backend/verification_subagent.py` | ENTRY-014 | Tightened `_evidence_contract` to reject ellipsis and outside-project paths. | Pin path-validation at the verifier; closes the `/tmp` hole. |
