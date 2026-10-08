# Product Development Team / 产品开发团队

## What it is / Problem it solves / 它是什么、解决什么问题

Everyone has an idea worth building. Almost nobody has the *team* it takes
to build it — the software engineering, the project management, the
computer science, and the discipline of writing it down before writing the
code. The know-how is sprawling and interdependent, so most ideas stop at
the idea.

**PDT is a spec-driven agent harness that takes the place of that team.** It
does the work the dozens of people on a product development team would do —
with one operator driving it, from a one-line idea to something verified and
delivered.

It is a **loop, not a pipeline**. You talk to it: it clarifies the
requirement, surfaces the decisions that need making, and writes them down
as a PRD, an architecture and a test strategy you can review — accept,
reject, or question, point by point. Those accepted decisions become a task
graph. The tasks run **concurrently**, their results are verified
**concurrently**, and what fails comes back as repair work. Round after
round, each judged against the artifacts the earlier rounds produced, until
it converges on an answer that holds.

The point is not "an AI writes code". The point is that **every step leaves
a structured, reviewable artifact, and each step advances on the decision
points the previous one made** — which is what keeps the work converging on
the right thing instead of drifting somewhere plausible. It is also what
makes the result auditable: you can read *why*, not just *what*.

每个人都有值得做出来的想法。几乎没有人拥有把它建起来所需要的那支*团队* ——
软件工程、项目管理、计算机科学，以及在写代码之前先把它写下来的纪律。这些知识
庞杂又互相依赖，于是大多数想法就停在了想法。

**PDT 是一个 spec 驱动的 agent harness，用来顶替那支团队。** 一个产品开发团队
几十个人会做的事，它来做 —— 由一名操作者驱动，从一句话想法走到经过验证的交付。

它是一个**循环，不是一条流水线**。你同它讲话：它澄清需求，把需要拍板的决策点
摆到台面上，并写成你可以逐条评审的 PRD、架构和测试策略 —— 接受、拒绝、或追问。
被接受的决策构成一张任务图。任务**并发**执行，结果**并发**验证，失败的以修复
工作的形式回来。一轮又一轮，每一轮都对着前面几轮产出的产物被评判，直到收敛到
一个站得住的答案。

重点不是"AI 写代码"。重点是**每一步都留下结构化、可评审的产物，而每一步都踩在
前一步做过的决策点上** —— 这正是让工作收敛到对的地方、而不是漂到某个看起来合理
的地方的东西。也正是它让结果可审计：你读到的是*为什么*，不只是*是什么*。

📖 **[Full documentation →](https://yongmaoluo.github.io/Product-Development-Team/)**
(source in [`docs/`](docs/); preview locally with `mkdocs serve`)
（源码在 [`docs/`](docs/)，`mkdocs serve` 可本地预览）

## How it works / 工作流

```
requirement
    │
    ▼
[1] Requirement clarification ──► interview.json
    │                              (5 dimensions: background, goals,
    │                               scope, constraints, acceptance)
    ▼
[2] PRD generation ─────────────► prd.json (CPEA decision points)
    │        ▲
    │        └── review / revise loop until every point is accepted
    ▼
[3] Architecture design (opt.) ─► arch-design.md     ┐
    │        ▲                                        │ same
    │        └── review / revise loop                 │ review
    ▼                                                  │ machinery
[4] Test design (opt.) ─────────► test-design.md      ┘
    │
    ▼
[5] Task generation ────────────► tasks.json
    │
    ▼
[6] Autonomous execution ───────► code + execution.log
    │   ⚡ CONCURRENT — dependency layers, then conflict-free micro
    │      layers inside each layer (file-level conflict graph),
    │      bounded by provider slots; runtime file locks as backstop
    │   ▶ runtime state lives in state.db (SQLite), not in the artifacts
    ▼
[7] Verification ───────────────► verification_report.json
    │   ⚡ CONCURRENT — VPs partitioned by method; groups and the VPs
    │      inside them both run in parallel, nested under one shared
    │      plan-wide ceiling
    │
    ├─ ✗ FAILED ──► repair tasks written to state.db ──► back to [6]
    │               (the executor reconciles them into the DAG from
    │                there; tasks.json is never rewritten)
    │               (bounded: stops on pass, round budget exhausted,
    │                the same failure set repeating, or operator stop)
    │
    └─ ✓ PASSED ──► done
```

Every decision point is a **CPEA** record — Context, Problem, Evaluation,
Action — so a reviewer sees the evidence behind a choice, not just the
choice. Each one can be accepted, rejected (which triggers a targeted
rewrite of that point only), or questioned. The PRD, the architecture and
the test design share this review machinery.

每个决策点都是一条 **CPEA** 记录（Context / Problem / Evaluation / Action），
所以评审者看到的是选择背后的证据，而不只是选择本身。每条都可以接受、拒绝（只重写
那一条）、或追问。PRD、架构、测试设计三份文档共用这套评审机制。

**The two halves that run concurrently** — and why each is designed the way
it is:

- **[Execution](docs/architecture/execution.md)** — dependency layers, then
  a **file-level conflict graph** that splits each layer into micro layers
  that never share a file; provider slots bound cost, runtime file locks
  catch the case where a task touches a file it did not declare.
- **[Verification](docs/architecture/verification.md)** — points partitioned
  by method, groups *and* the points inside them in parallel, nested under
  one shared plan-wide semaphore. **A failure is not an ending**: deviations
  become repair tasks written straight into the state database, which the
  executor reconciles into the DAG — so they arrive under the same
  concurrency rules and the same completion check, without `tasks.json`
  being rewritten. The loop is bounded on four sides, including "the same
  failure set repeated" — repairing is not working, so stop instead of
  burning budget.

## Quick start / 快速开始

### Installation and startup / 安装与启动

```bash
uv sync --project backend        # builds backend/.venv from backend/uv.lock
source backend/.venv/bin/activate

cp .env.example .env             # optional — every setting in it is optional

# From the repository root — the app is a package (`backend/`), so `-m` is
# what puts both the root and `backend/` on `sys.path`.
python -m backend.server
```

The server binds **loopback only** — `http://127.0.0.1:8000` — and serves
the static frontend from the same process. Open <http://localhost:8000> for
the UI.

> **Always use `backend/.venv`, never the system Python.**
> Run `pytest` as `backend/.venv/bin/python3 -m pytest ...`. The **system
> Python** is missing the pinned wheels and carries a stale `urllib3` whose
> warning shim no longer matches the runtime build, so the suite fails at
> **collection** — before the first test runs, which reads like a broken
> repository rather than a wrong interpreter. There is no fallback
> configuration that makes the system interpreter work.

Step-by-step walkthrough, including a first plan driven over HTTP:
**[Getting started](docs/getting-started.md)**.

### Local use only / 仅限本机使用

It writes files, spawns subprocesses and runs commands **as you, on your
machine**. **The API is unauthenticated** — no accounts, no login, no
per-user separation — so it is not built for shared or public hosting, and
the REST API is the management surface for *your* instance, not a service
for other people's clients. Read
**[Running it](docs/operations/running.md)** before you point it anywhere
but loopback.

### The request guard / 请求守卫

Loopback is not a security boundary against a **browser**: any page you
visit can issue requests to `127.0.0.1:<port>`, and the server cannot tell
those from its own UI. So every `/api/*` request must carry:

```
X-PDT-Request: 1
```

The value is not a secret and is not meant to be — read it as proof of *not
being a foreign web page*. A cross-origin request with a custom header is
no longer a CORS "simple request", so the browser must preflight it, and
this server answers no preflight. Knowing the value does not help: the
browser refuses to send the request. `Origin` and `Host` are additionally
checked, which is what answers DNS rebinding.

The bundled UI sends the header for you — but only through its two
wrappers, `apiRequest()` in `frontend/api.js` and `api()` inside
`app.js`. **A bare `fetch(...)` from any other JS module gets `403
Forbidden`**, because the browser will not attach the header on its own;
`backend/tests/static_gates/test_frontend_uses_api_wrapper.py` refuses a
bare `fetch(` it cannot trace back to one of those two. Full mechanics:
**[Running it](docs/operations/running.md)**.

```bash
curl -H 'X-PDT-Request: 1' http://localhost:8000/api/plans
```

## Layout / 目录结构

The first table is **the product** — everything a fresh clone contains.

| Path | What lives there |
|---|---|
| `backend/` | FastAPI server, phase generators, executor, verification agent |
| `backend/routes/` | HTTP routers extracted from `server.py` |
| `backend/framework/` | Shared primitives (clock, ids, task graph, validators) |
| `backend/configs/` | Layered YAML config: `_base.yaml` plus overlays |
| `frontend/` | Static UI, served by the backend |
| `backend/tests/` | Test suites — unit / integration / e2e / security / contract, plus the `static_gates/` and `meta_tests/` gates over the tree and the audit document |
| `scripts/` | General scripts only: the ones CI, pre-commit and a developer's shell run |
| `.claude/skills/` | The skills that drive the LUI workflow |
| `example/` | `.example` templates for the per-deployment provider config |
| `docs/` | The MkDocs source for the documentation site |
| `tests/` | Top-level fixtures for tooling that runs outside `backend/tests/` |
| `.github/workflows/` | CI, and the Pages deployment |
| `SECURITY_AUDIT.md` | The audit trail — findings, remediations, and the gates that pin them |
| `mkdocs.yml`, `pytest.ini`, `.pre-commit-config.yaml`, `.gitleaks.toml`, `.coveragerc` | Build, test and gate configuration |

The second is **local runtime state** — created on first run, `.gitignore`d,
never part of a clone. The rule that decides which side a new file lands on
is in [Contributing](docs/development/contributing.md#where-a-file-belongs).

| Local path | What lives there |
|---|---|
| `.pdt/` | The state family — `state.db` (+ `-wal`/`-shm`), `pdt_server_boot_id`, `backups/`, `nightly-results.json`. **One-off and machine-local scripts belong here too, not in `scripts/`** |
| `plans/` | One directory per plan: interview, PRD, arch, tasks, execution, verification |
| `.env` | This deployment's non-secret settings — the keychain *indexes*, timezone, notification targets |
| `.config/` | Per-user tool state (provider blacklist, ordering) |
| `tools/` | The operator fleet's subsidiary processes |
| `site/`, `.pytest_cache/`, `ac_server_boot_id`, `CLAUDE.md` | Build output, caches, and this checkout's own operator notes |

## Configuration / 配置

`backend/config.yaml` is intentionally thin — nothing is required to run.
There is no built-in provider chain (point `provider_order_file` at
whatever produces that contract), and `subsidiary_processes` ships empty.

Provider concurrency caps and provider routing are **operator-specific**.
They ship as committed templates under `example/`; you copy them into the
gitignored `.config/` at the repository root and edit the copies:

```bash
mkdir -p .config
cp example/provider_capacity.yaml.example .config/provider_capacity.yaml
cp example/provider_routing.yaml.example  .config/provider_routing.yaml
```

`.config/` is gitignored on purpose: a table of concrete names in source
would compile one deployment's providers into every install. There is
deliberately **no** example-with-real-names. Both files are also
overridable from the environment, for deployments that keep their config
elsewhere:

| Env var | Overrides |
|---|---|
| `PDT_PROVIDER_CAPACITY_FILE` | path to the capacity YAML |
| `PDT_PROVIDER_ROUTING_FILE` | path to the routing YAML |

Credentials do **not** come from a dotenv file. A provider API key is
supplied at runtime by whatever provider layer a deployment runs —
`backend/cc_switch.py` is the integration this codebase ships, and it
reads one provider row at a time; the two notification secrets (Feishu
app secret, Telegram bot token) are read from a macOS keychain. What
`.env` (gitignored, templated by the committed `.env.example`) carries is
the non-secret half — the `account` each keychain item is filed under,
timezone, and the notification targets. It is entirely optional: a
deployment that exports its configuration directly, or that has no
notifications at all, runs without the file. Runtime state (the state
database, its boot counter, its backups) lives under a single gitignored
`<repo>/.pdt/`.

More: **[Configuration](docs/operations/configuration.md)**.

## How to run the tests / 如何跑测试

```bash
# from the repository root, with backend/.venv activated — the default lane
backend/.venv/bin/python3 -m pytest backend/tests -m unit -q

# or via the canonical wrapper, which resolves the venv for you
scripts/run_tests.sh
```

`-m unit` skips the `time_sensitive`, `integration`, `e2e` and `real_model` markers;
the missing models are satisfied by stubs the conftest installs.

Two collection rules worth knowing: `pytest.ini` is directory-sensitive
(from `backend/` pytest reads `backend/pytest.ini`, from the root it reads
the root one — same markers, different addopts), and `backend/.venv` is the
only interpreter the suite imports cleanly under.

Lanes, fixtures and how the gate culture works:
**[Tests](docs/development/testing.md)**.

## Design notes / 设计说明

- **Two independent completion signals.** A task is done only when the
  agent's report *and* its declared `test_command` exit code agree.
- **Empty diff is a warning, not a failure.** Audit-style tasks whose
  deliverable is a finding are legitimate, and are verified by a second-pass
  audit instead of being retried until they strand.
- **Bounded loops everywhere.** Caps on retries and rounds, an early exit
  when the same failure repeats, and subprocess timeouts that kill the whole
  process group rather than just the direct child.

**[Design notes](docs/development/design-notes.md)** explains what each of
these cost the last time it was wrong.

## How to contribute / 如何贡献

The repository split *is* the contribution surface: phase generators and
their routers under `backend/`, the static UI under `frontend/`, and the
suite under `backend/tests/`. Add the test first, and remember that phases
are load-bearing on each other — a change to PRD generation without a
matching acceptance test will pass `pytest` and still be wrong, because
verification compares the running code against the documents the earlier
phases produced.

If your change adds an external provider, do **not** name it on a template —
a row of concrete names belongs in `PDT_PROVIDER_*` config on the deploying
machine.

Full workflow, the naming rule, and how to write a gate that survives:
**[Contributing](docs/development/contributing.md)**.

## Documentation

The developer documentation is published at
<https://yongmaoluo.github.io/Product-Development-Team/>, built from
[`docs/`](docs/) with MkDocs. It is **bilingual** — every page has an
English and a Chinese version, with a language selector in the header that
keeps you on the same page when you switch.

```bash
backend/.venv/bin/python3 -m pip install -r docs/requirements.txt
backend/.venv/bin/python3 -m mkdocs serve          # preview
backend/.venv/bin/python3 -m mkdocs build --strict # what CI runs
```

`--strict` is the gate: a nav entry pointing at a missing page, or a link to
a page that does not exist, exits non-zero. Pairing is gated too — a page
that exists in only one locale fails
`backend/tests/static_gates/test_docs_are_bilingual.py`, because the build
itself would succeed and silently serve the other language. The Markdown is
plain and renders on github.com too.

## License / 许可证

See [`LICENSE`](LICENSE). / 见 [`LICENSE`](LICENSE)。
