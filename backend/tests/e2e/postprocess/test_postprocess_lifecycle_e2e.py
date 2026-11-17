"""真实 server 下的收尾阶段（postprocess）全链路 E2E —— S1 / S2 / S3。

这个文件证明的是**真进程**上的三件事，不是 mock 上的三件事
--------------------------------------------------------

* **S1 —— 没有要求书的 plan**：验证通过后直接落到 ``completed``；plan 目录里
  **一个** ``postprocess*`` 文件都没有（零侵入基线
  ``tests/fixtures/postprocess/plan_dir_baseline.json``）；调度读模型的
  ``waiting[]`` 是空的；终态事件只发一次。
* **S2 —— 要求书里每条都通过**：收尾阶段逐条执行、逐条 PASS，报告终态是
  ``completed``；本地裸仓里真的多出一条分支；每条都带完整的审计三元组
  ``{action, command, target}``。
* **S3 —— 有一条目失败**：失败条目**只**重跑一次（第二轮只含失败的那条），
  报告终态是 ``postprocess_failed``；失败通知的审计行**恰好一行**，且是三段式
  （plan、失败条目、证据路径）。

真实依赖与替身
--------------

* **server 进程** —— 真起一个 ``python -m backend.server``。就绪判据是
  ``GET /api/health`` 返回 200，**不是**"进程还在"：进程存在只说明它正在启动。
* **SQLite** —— seed 与 server 指向同一个 ``state.db``；行写进 A 库、server 读
  B 库会把"读不到行"误判成"功能坏了"。
* **文件系统** —— plan 目录、进度文件、终态报告、通知审计行，全是磁盘上的真文件。
* **git** —— 只对 ``tmp_path`` 下的**临时本地裸仓**操作；远端是一个本地路径，
  不是任何真实远端。
* **gh** —— 由 PATH 上的桩 ``tests/fixtures/postprocess/bin/gh`` 承接。这三张
  要求书里没有一条命令用 gh，所以桩一次都不该被调用（下面有断言）。
* **验证轮** —— ``VERIFICATION_PROFILE=dry_run``，加上 ``tasks.json`` 的条目
  列表为空，走的是既有的确定性短路（``verification/orchestrator.py`` 的
  ``short_circuit_empty_tasks``）：不调模型、不触网、逐条 PASS。

全程不触网
----------

这个文件里没有任何一次出网请求：git 的远端是临时裸仓，gh 是 PATH 上的桩，
验证轮是空任务短路。三条断言把这个性质钉住：裸仓里真的多出分支（动作真的
执行了）、桩的调用日志不存在（没有一次 gh 调用）、报告里的证据原文是 git
自己的输出。

失败通知走 plan 自己的通道
--------------------------

收尾阶段的失败通知**不自己长通道**：它走 plan 已经绑好的那条路 ——
plan 目录里的 ``hitl_config.json``，由既有的 hook dispatcher 投递。这个 E2E
把那条路指向一个**本地**目的地（一条 ``cat >> 文件`` 的 cmd hook），于是
"通知真的发出去了"是磁盘上的事实，而**全程没有一次出网请求**：不连飞书、不连
Telegram、不连任何 cc-connect 会话。

通知模块自己的设计说得很清楚："没有通道就不写审计行"，因为审计行的含义是
"这条已经发出去了"。所以这条 E2E 同时钉住两件事：审计行**恰好一行**，而且
plan 的通道**真的收到了那条消息**（通道的落地文件里有一行同样的三段式）。
通道没有收到而审计行写了，是这个契约被绕开的样子。

收尾阶段的终态由它自己写
----------------------

收尾阶段拥有它的终态出口：阶段写下报告，plan 的相位跟着报告走 ——
每条必做项都通过 → ``completed``；有必做项失败 → ``postprocess_failed``；
还在等人 → 留在 ``post_processing``。相位落在 SQLite 的 routing 行里，所以
断言读的是 ``state-from-db``（直接读库），而不是某个进程内的内存状态。
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

#: ``backend/`` —— 仓库自己的布局，不写死任何机器上的路径。
BACKEND_DIR = Path(__file__).resolve().parents[3]
PROJECT_ROOT = BACKEND_DIR.parent

#: The backend subprocess has to run on the interpreter this suite was
#: installed into.  A checkout with no ``backend/.venv`` (fresh worktree,
#: container) falls back to the interpreter already running pytest — never
#: to a system python, which would not have the dependencies installed.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The requirement book's fixtures: the ``gh`` stub and the zero-intrusion
#: baseline snapshot.
FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures" / "postprocess"
GH_STUB_BIN = FIXTURES_DIR / "bin"
BASELINE_FILE = FIXTURES_DIR / "plan_dir_baseline.json"

#: Every ``/api/*`` request must carry this header (``backend/request_guard.py``).
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: The plan this file seeds.  A leaf name, nothing exotic: the endpoint
#: validates it (``server._validated_plan_id``) before it resolves a path.
PLAN_ID = "p1"

#: How long the backend may take to answer its readiness probe.  A boot
#: that has not answered within this budget is a failure, and the failure
#: carries the tail of the server's own output — a boot that died says why
#: in there, and a boot that hung says where it stopped.
READY_TIMEOUT_SECONDS = 90.0

#: How long the finishing stage may take to write its terminal report.
REPORT_TIMEOUT_SECONDS = 120.0

#: How long the failure notification may take to reach the audit file
#: after the report landed.  The notification is delivered right after the
#: report is written, so this budget is generous on purpose: a short one
#: would turn a slow machine into a false "the notification never came".
NOTIFICATION_TIMEOUT_SECONDS = 10.0

#: The branch the finishing stage is asked to publish into the bare repo.
PUBLISHED_BRANCH = "finishing"


# ---------------------------------------------------------------------------
# The requirement books
# ---------------------------------------------------------------------------


def _item(
    item_id: str,
    action: str,
    command: List[str],
    target: str,
) -> Dict[str, Any]:
    """One requirement item, in the shape the requirement book declares.

    The seven keys are the ones ``postprocess/spec.py`` requires.  An item
    that carries a ``command`` must be ``external`` — a local item has no
    command to run, so the executor has nothing to do.

    The acceptance predicate is ``exit_code == 0``: the command's exit code
    decides, and the evidence requirement is the same.  Both commands used
    here print something on success, which matters: the judge treats a
    record with no output as the *absence* of evidence, and an item with no
    usable evidence is a FAIL.
    """
    return {
        "id": item_id,
        "action": action,
        "command": list(command),
        "target": target,
        "acceptance_predicate": {"kind": "exit_code", "equals": 0},
        "evidence_requirement": {"kind": "exit_code"},
        "external": True,
        "required": True,
        "wait": {"type": "none"},
    }


def _book_all_pass() -> Dict[str, Any]:
    """A book whose every item passes.

    The first item pushes the finishing branch into the **local bare repo**
    — that is the outward action the whole stage exists for, and it is the
    only way to prove from outside that the command really ran.  The second
    one reads the head commit: read-only, idempotent, and it prints, so it
    is a second item with its own verdict and its own audit triple.
    """
    return {
        "items": [
            _item(
                "publish-branch",
                "publish the finishing branch",
                ["git", "push", "origin", f"HEAD:refs/heads/{PUBLISHED_BRANCH}"],
                PUBLISHED_BRANCH,
            ),
            _item(
                "read-head",
                "read the head commit",
                ["git", "rev-parse", "--verify", "HEAD"],
                "HEAD",
            ),
        ]
    }


def _book_one_fails() -> Dict[str, Any]:
    """A book with one item that cannot pass, twice in a row.

    The failing item resolves a ref that does not exist: git exits
    non-zero and says why on stderr, so the judge has usable evidence and
    a clean FAIL.  It fails the same way on the second attempt too — a
    failure that only happened once would make "retried exactly once"
    indistinguishable from "failed once and never retried".
    """
    book = _book_all_pass()
    book["items"].append(
        _item(
            "missing-ref",
            "resolve a branch that does not exist",
            ["git", "rev-parse", "--verify", "refs/heads/does-not-exist"],
            "refs/heads/does-not-exist",
        )
    )
    return book


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind an ephemeral loopback port and hand the number back.

    The kernel picks the port, so the number is free at the moment it is
    returned.  The port is never a fixed one: a fixed port collides with
    whatever else is listening, and the second boot of a previous run of
    this file may still hold it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """One HTTP request against the backend; ``(status, decoded body)``.

    The guard header goes with every call: ``/api/*`` is guarded, and a
    request without the header is refused with 403 before it reaches a
    route.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``git`` in ``cwd`` and hand the finished process back.

    Every git call in this file talks to a repository under ``tmp_path``:
    the working copy's remote is a bare repo on the same filesystem, so no
    git call in this file can reach the network.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _read_json(path: Path) -> Optional[Any]:
    """The decoded JSON of ``path``, or ``None`` when it is not there."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _wait_for(
    predicate: Callable[[], Any],
    timeout: float,
    *,
    interval: float = 0.25,
) -> Any:
    """Call ``predicate`` until it returns something truthy.

    Returns the truthy value, or ``None`` when the budget ran out.  The
    finishing stage runs on a background thread of the server, so "the
    report exists" is a thing that becomes true a moment after the round
    passed — the test waits for it instead of guessing a sleep.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# The harness: one plan, its git workspace, and the real server
# ---------------------------------------------------------------------------


class _Harness:
    """A plan, the workspace it acts on, and the real server that runs it.

    Everything lives under one ``tmp_path``: the plans tree the server is
    pointed at, the state database, the git working copy, the bare repo it
    pushes into, and the log the server's own output goes to.
    """

    def __init__(self, work: Path, book: Optional[Dict[str, Any]]) -> None:
        self.work = work
        self.book = book
        self.plan_id = PLAN_ID
        self.plan_dir = work / "plans" / self.plan_id
        self.project = work / "project"
        self.bare = work / "bare.git"
        self.gh_calls_file = work / "gh-calls.jsonl"
        self.delivery_file = work / "channel-delivery.jsonl"
        self.log_path = work / "server.log"
        self.proc: Optional[subprocess.Popen] = None
        self.base_url = ""

    # -- setup ---------------------------------------------------------

    def prepare(self) -> None:
        """Write the plan's directory, its git workspace and its state rows.

        The plan directory is seeded with the **zero-intrusion baseline's**
        file set — the same names ``plan_dir_baseline.json`` names — so
        "the finishing stage added nothing" is a statement about a file set
        that existed before the stage ran.

        The state rows are the ones the server needs to find the plan:
        ``plan_routing`` says which phase the plan is in (``executing`` is
        one of the phases a verification start may be issued from), and
        ``plan_execution`` says which directory the plan acts on.  The
        finishing stage's commands run **in the plan's project** — that is
        where the git working copy is, and that is why the branch shows up
        in the bare repo.
        """
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        self._write_baseline_files()
        self._write_requirement_book()
        self._write_notification_channel()
        self._make_git_workspace()
        self._seed_state()

    def _write_baseline_files(self) -> None:
        """The baseline's file set, with the content each one needs.

        ``tasks.json`` carries an **empty** task list on purpose: that is
        the existing deterministic short-circuit in the verification
        orchestrator, so the round passes without a model and without a
        network.  The other three are the files a plan directory normally
        carries; their content is irrelevant here, but their presence is
        what the baseline is about.
        """
        (self.plan_dir / "execution.log").write_text("", encoding="utf-8")
        (self.plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": []}, ensure_ascii=False), encoding="utf-8"
        )
        (self.plan_dir / "verification_plan.json").write_text(
            json.dumps(
                {
                    "id": "VP-001",
                    "verification_method": "api_test",
                    "request": {
                        "method": "GET",
                        "url": "http://127.0.0.1:1/unreachable",
                        "headers": {},
                        "body": None,
                        "timeout_seconds": 5,
                    },
                    "assertions": [{"name": "status", "status": 200}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.plan_dir / "verification_report.json").write_text(
            json.dumps(
                {
                    "plan_id": self.plan_id,
                    "round": 1,
                    "overall_status": "PASSED",
                    "verification_points": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.plan_dir / "llm_sessions.jsonl").write_text("", encoding="utf-8")

    def _write_requirement_book(self) -> None:
        """The requirement book — or nothing at all, for S1.

        No book means no ``postprocess.json`` in the plan directory, which
        is what the gate reads: ``has_postprocess_spec`` is the one
        decision point that says whether this plan has a finishing stage
        at all.
        """
        if self.book is None:
            return
        (self.plan_dir / "postprocess.json").write_text(
            json.dumps(self.book, ensure_ascii=False), encoding="utf-8"
        )

    def _write_notification_channel(self) -> None:
        """Bind the plan to a **local** notification channel.

        A plan's failure notification goes out through the route the plan
        is already bound to: the ``hitl_config.json`` binding, carried by
        the existing hook dispatcher.  This file binds that route to a
        command that appends the delivered payload to a file under
        ``tmp_path`` — so "the notification was delivered" is a fact on
        disk, and no request leaves the machine.

        A plan with no requirement book gets no channel: nothing about
        the finishing stage can fail on a plan that has no finishing
        stage, so such a plan must not grow a route that would only ever
        carry silence.
        """
        if self.book is None:
            return
        command = f"cat >> {shlex.quote(str(self.delivery_file))}"
        (self.plan_dir / "hitl_config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "mode": "auto",
                    "timeout_sec": 30,
                    "hooks": [{"kind": "cmd", "command": command}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _make_git_workspace(self) -> None:
        """A working copy whose remote is a **local bare repo**.

        The bare repo is the whole "outward action" surface of this file:
        a branch the finishing stage pushes here is a branch that exists
        on disk, and nothing about it touches a real remote.
        """
        bare = _git("init", "--bare", "-b", "main", str(self.bare), cwd=self.work)
        assert bare.returncode == 0, f"git init --bare failed: {bare.stderr}"
        project = _git("init", "-b", "main", str(self.project), cwd=self.work)
        assert project.returncode == 0, f"git init failed: {project.stderr}"
        _git("config", "user.email", "postprocess-e2e@example.invalid",
             cwd=self.project)
        _git("config", "user.name", "PDT Postprocess E2E", cwd=self.project)
        (self.project / "README.md").write_text(
            "postprocess e2e\n", encoding="utf-8"
        )
        _git("add", "README.md", cwd=self.project)
        commit = _git("commit", "-m", "initial commit", cwd=self.project)
        assert commit.returncode == 0, f"git commit failed: {commit.stderr}"
        _git("remote", "add", "origin", str(self.bare), cwd=self.project)
        push = _git("push", "origin", "main", cwd=self.project)
        assert push.returncode == 0, f"git push failed: {push.stderr}"

    def _seed_state(self) -> None:
        """Write the plan's rows into the state database the server opens.

        The database is the **same file** the server is pointed at.  Rows
        written into one database and read out of another would turn "the
        server cannot see the plan" into a false verdict about the feature.
        """
        from state_machine.db.connection import open as db_open
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = db_open(self.work / "state.db")
        try:
            migrate(conn)
            RoutingRepository(conn).insert(self.plan_id, "executing")
            ExecutionRepository(conn).insert(
                self.plan_id, project_dir=str(self.project)
            )
        finally:
            conn.close()

    # -- the server ----------------------------------------------------

    def start(self) -> None:
        """Boot the real server and wait until it answers.

        The environment is the whole contract between the seed and the
        server.  Six of the variables are named in the requirement book of
        this task; the rest are the ones a throwaway server needs so it
        does not touch the developer's machine:

        * ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` / ``PDT_PORT`` — the
          plans tree, the database and the port the server answers on.
        * ``PDT_PROVIDER_MODE=inherit`` — the boot asks CC Switch for the
          provider chain and **aborts the startup** when it cannot be
          asked.  This server must not need a provider fleet to come up.
        * ``PDT_ORPHAN_REAPER=0`` — otherwise the boot walks **every
          process on the machine** to find orphans, which takes minutes on
          a busy box, and the request guard's self-check kills the boot
          before it ever answers.  A throwaway server has no orphans.
        * ``VERIFICATION_PROFILE=dry_run`` — the deterministic verifier.
        * ``PYTHONPATH`` — ``backend/`` has to be importable, because the
          server's own modules import each other by their flat names.
        * ``PYTEST_CURRENT_TEST`` — the product's own answer to "may this
          process touch the machine?".  This child is a test process.
        * ``PDT_DEBUG_ROUTES=1`` — the operator endpoints that expose the
          in-process event bus are shipped off by default.  The bus
          counter is the only way to see **how many** events the flow
          published from outside the process, and "the terminal was
          published exactly once" is one of the things S1 has to prove.
        * ``GH_CALLS_JSONL`` — where the ``gh`` stub records its calls.
          The path is under ``tmp_path``, so a stub call can never leave a
          file in the repository.
        """
        self.prepare()

        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env.update(
            {
                "PDT_PLANS_DIR": str(self.work / "plans"),
                "PDT_STATE_DB_PATH": str(self.work / "state.db"),
                "PDT_PORT": str(port),
                "PDT_PROVIDER_MODE": "inherit",
                "PDT_ORPHAN_REAPER": "0",
                "PDT_DEBUG_ROUTES": "1",
                "VERIFICATION_PROFILE": "dry_run",
                "PATH": f"{GH_STUB_BIN}{os.pathsep}{env.get('PATH', '')}",
                "PYTHONPATH": (
                    f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}"
                ),
                "PYTEST_CURRENT_TEST": (
                    "backend/tests/e2e/postprocess/"
                    "test_postprocess_lifecycle_e2e.py (setup)"
                ),
                "GH_CALLS_JSONL": str(self.gh_calls_file),
            }
        )
        # A stale secret descriptor must never be closed by a process that
        # never owned it.
        for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
            env.pop(key, None)
        env.pop("ANTHROPIC_BASE_URL", None)

        with self.log_path.open("wb") as handle:
            self.proc = subprocess.Popen(
                [str(VENV_PYTHON), "-m", "backend.server"],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._wait_ready()

    def _wait_ready(self) -> None:
        """Wait until the backend answers its readiness probe.

        The backend is up when the API answers, not when the process
        exists: the process exists for a moment before uvicorn is
        listening.  A backend that dies on the way up says so in its own
        output, so that output is attached to the failure.
        """
        assert self.proc is not None
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last: Any = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"the backend exited with {self.proc.returncode} before it "
                    f"was ready:\n{self.server_output()}"
                )
            try:
                status, body = _http_json("GET", f"{self.base_url}/api/health")
                if status == 200:
                    return
                last = (status, body)
            except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
                last = exc
            time.sleep(0.2)
        raise AssertionError(
            f"{self.base_url}/api/health was not ready within "
            f"{READY_TIMEOUT_SECONDS}s; last={last!r}\n{self.server_output()}"
        )

    def stop(self) -> None:
        """Stop the backend and let go of its port.

        ``terminate`` first — uvicorn shuts down gracefully on SIGTERM —
        and ``kill`` only when that did not finish in time.
        """
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    def server_output(self, limit: int = 4000) -> str:
        """The tail of the backend's own output."""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - the file is ours
            return "(no server output was captured)"
        return text[-limit:]

    # -- driving -------------------------------------------------------

    def start_verification(self) -> Dict[str, Any]:
        """Start the verification round through the API, the way an operator does."""
        status, body = _http_json(
            "POST",
            f"{self.base_url}/api/verification/{self.plan_id}/start",
            {"auto_fix": True},
        )
        assert status == 200, f"the start was refused: {status} {body!r}"
        assert isinstance(body, dict), f"the start answered {body!r}"
        return body

    def wait_for_report(self) -> Optional[Dict[str, Any]]:
        """Wait until the finishing stage has written its terminal report."""
        def _report() -> Optional[Dict[str, Any]]:
            report = self.report()
            if report and report.get("terminal_status"):
                return report
            return None

        return _wait_for(_report, REPORT_TIMEOUT_SECONDS)

    # -- observing -----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """The verification status answer for this plan."""
        status, body = _http_json(
            "GET", f"{self.base_url}/api/verification/{self.plan_id}/status"
        )
        assert status == 200, f"the status endpoint answered {status}: {body!r}"
        assert isinstance(body, dict), f"the status answer is not an object: {body!r}"
        return body

    def phase(self) -> str:
        """The plan's current phase, as the status endpoint reports it."""
        return str(self.status().get("current_phase") or "")

    def routing_state(self) -> Dict[str, Any]:
        """The plan's routing row, straight out of the state database.

        The endpoint reads ``plan_routing`` in SQLite — no plan directory,
        no JSON fallback — so what it answers is the state the server
        itself is working from.
        """
        status, body = _http_json(
            "GET", f"{self.base_url}/api/plan/{self.plan_id}/state-from-db"
        )
        assert status == 200, f"the state endpoint answered {status}: {body!r}"
        assert isinstance(body, dict), f"the state answer is not an object: {body!r}"
        return body

    def reached_finishing_phase(self) -> bool:
        """Whether the plan was ever in the finishing phase.

        Two ways to know, and they cover both worlds:

        * the plan is in ``post_processing`` **now** — which is what a
          plan whose phase is never written looks like; and
        * the plan has **left** ``post_processing`` — the routing row
          records the phases it left in ``completed_phases``, so a plan
          that went on to ``completed`` still shows it.

        The second one matters: "the plan was in the finishing phase" is
        a statement about the past, and a sampled phase is a snapshot of
        the present.  Reading the row is the difference between the two.
        """
        state = self.routing_state()
        if state.get("current_phase") == "post_processing":
            return True
        completed = state.get("completed_phases")
        return isinstance(completed, list) and "post_processing" in completed

    def wait_for_phase(self, phase: str, timeout: float) -> bool:
        """Wait until the plan's phase is ``phase``.

        The phase is read from the routing row in SQLite (the
        ``state-from-db`` endpoint), so what the test sees is the state
        the server itself is working from — not a process-local answer
        that could be a moment ahead of the durable one.

        The finishing stage settles the phase **after** it wrote its
        report and delivered its notification, so a test that waits for
        the phase waits for the whole chain, and everything the chain
        leaves on disk is there when the wait is over.
        """
        def _reached() -> bool:
            return self.routing_state().get("current_phase") == phase

        return bool(_wait_for(_reached, timeout))

    def report(self) -> Optional[Dict[str, Any]]:
        """The finishing stage's terminal report, if it wrote one."""
        report = _read_json(self.plan_dir / "postprocess_report.json")
        return report if isinstance(report, dict) else None

    def progress(self) -> Optional[Dict[str, Any]]:
        """The finishing stage's progress file, if it wrote one."""
        progress = _read_json(self.plan_dir / "postprocess_progress.json")
        return progress if isinstance(progress, dict) else None

    def notification_lines(self) -> List[Dict[str, Any]]:
        """The failure notification's audit lines, decoded.

        One line per **actual** delivery.  A file that does not exist means
        no delivery was recorded — which is a different thing from "a
        delivery was recorded with an empty payload".
        """
        path = self.plan_dir / "postprocess_notifications.jsonl"
        if not path.exists():
            return []
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            decoded = json.loads(raw)
            lines.append(decoded)
        return lines

    def channel_deliveries(self) -> List[Dict[str, Any]]:
        """The messages the plan's own channel received.

        The channel is the plan's ``hitl_config.json`` binding, pointed at
        a local file by the harness.  A message in this file is a
        message the channel really took — the dispatcher ran the
        destination and it exited cleanly.
        """
        if not self.delivery_file.exists():
            return []
        deliveries = []
        for raw in self.delivery_file.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            deliveries.append(json.loads(raw))
        return deliveries

    def plan_files(self) -> List[str]:
        """The names of the files in the plan's own directory."""
        return sorted(
            entry.name for entry in self.plan_dir.iterdir() if entry.is_file()
        )

    def finishing_stage_files(self) -> List[str]:
        """The files in the plan directory the finishing stage could have written."""
        return [
            name for name in self.plan_files() if name.startswith("postprocess")
        ]

    def bare_branches(self) -> List[str]:
        """The branch names the **bare repo** holds."""
        result = _git(
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            cwd=self.bare,
        )
        assert result.returncode == 0, f"git for-each-ref failed: {result.stderr}"
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    def bus_published(self) -> int:
        """How many events the in-process state bus has published."""
        status, body = _http_json("GET", f"{self.base_url}/api/debug/notifications")
        assert status == 200, f"the debug endpoint answered {status}: {body!r}"
        assert isinstance(body, dict), f"the debug answer is not an object: {body!r}"
        bus = body.get("bus")
        assert isinstance(bus, dict), f"the debug answer carries no bus: {body!r}"
        return int(bus.get("published") or 0)

    def scheduler_state(self) -> Dict[str, Any]:
        """The scheduler's read model."""
        status, body = _http_json("GET", f"{self.base_url}/api/scheduler/state")
        assert status == 200, f"the scheduler endpoint answered {status}: {body!r}"
        assert isinstance(body, dict), f"the scheduler answer is not an object: {body!r}"
        return body

    def gh_calls(self) -> List[Any]:
        """The calls the ``gh`` stub recorded, if it was called at all."""
        if not self.gh_calls_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.gh_calls_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def boot_plan(tmp_path):
    """Start a real server for one plan; the caller says which book it has.

    The fixture is a factory because the three scenarios differ in exactly
    one thing — whether the plan declares a requirement book, and what is
    in it — and everything else about them is the same: the same boot
    contract, the same seed, the same observations.

    The server is stopped on the way out, so a test that fails half way
    through still gives its port back.
    """
    started: List[_Harness] = []

    def _boot(book: Optional[Dict[str, Any]] = None) -> _Harness:
        harness = _Harness(tmp_path, book)
        harness.start()
        started.append(harness)
        return harness

    yield _boot

    for harness in started:
        harness.stop()


# ---------------------------------------------------------------------------
# The assertions
# ---------------------------------------------------------------------------


def test_no_spec_plan_completes_without_postprocess_artifacts(boot_plan):
    """S1 —— 没有要求书的 plan：验证通过即 ``completed``，收尾阶段零侵入。

    这一条是"没有要求书时行为不变"（架构决策点 1 的 A1）的验收：这样的
    plan **不该**碰到收尾阶段的任何一个文件，也不该在调度读模型里留下任何
    等待行，而终态事件只该发一次。

    基线快照 ``plan_dir_baseline.json`` 记的是"没有要求书时 plan 目录顶层应有
    的文件集合"。真正跑起来的验证轮会往 plan 目录里写它自己的记账文件
    （``verification_loop_tracking.json`` 等），所以"文件集合与基线完全相同"
    在真进程上**不成立** —— 基线约束的是**收尾阶段**对这份集合的贡献，不是
    验证轮自己的。断言因此落在两件事上：基线点名的文件一个都没少，以及 plan
    目录里**没有任何** ``postprocess*`` 文件（收尾阶段可能写的文件，一个都
    没有出现）。
    """
    plan = boot_plan(None)

    before = plan.bus_published()
    plan.start_verification()

    # The plan reaches its terminal phase on its own: no requirement book
    # means the finishing gate is not taken and the existing path runs.
    deadline = time.monotonic() + REPORT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if plan.phase() in ("completed", "failed"):
            break
        time.sleep(0.25)

    status = plan.status()
    assert status.get("current_phase") == "completed", (
        f"a plan with no requirement book did not complete: {status!r}"
    )
    assert status.get("verification_status") == "passed", (
        f"the round did not pass: {status!r}"
    )

    # --- the finishing gate was not taken ---------------------------------
    # A plan with no requirement book never enters the finishing phase.
    # This is the other half of "zero intrusion": not only did the stage
    # write nothing, it was not even entered.
    assert not plan.reached_finishing_phase(), (
        "a plan with no requirement book entered the finishing phase: "
        f"{plan.routing_state()!r}"
    )

    # --- the zero-intrusion baseline -------------------------------------
    baseline = _read_json(BASELINE_FILE)
    assert isinstance(baseline, dict), f"the baseline is not an object: {baseline!r}"
    baseline_files = set(baseline.get("files") or {})
    assert baseline_files, "the baseline names no files"

    actual = set(plan.plan_files())
    assert baseline_files <= actual, (
        "the plan directory lost a file the baseline names: "
        f"missing={sorted(baseline_files - actual)}"
    )
    assert plan.finishing_stage_files() == [], (
        "a plan with no requirement book grew finishing-stage artifacts: "
        f"{plan.finishing_stage_files()}"
    )

    # --- nothing is waiting ----------------------------------------------
    scheduler = plan.scheduler_state()
    assert scheduler.get("waiting") == [], (
        f"a plan with no requirement book left a waiting row: {scheduler!r}"
    )

    # --- the terminal is published exactly once ---------------------------
    # The flow publishes two phase changes (the start, and the terminal
    # move to ``completed``) and the terminal event itself.  Three events
    # in total means the terminal was published once and only once: a
    # second ``plan_closed`` would make it four.
    after = plan.bus_published()
    assert after - before == 3, (
        "the flow published {0} events, not the three it should (the start's "
        "phase change, the terminal phase change and one plan_closed)".format(
            after - before
        )
    )

    # --- nothing reached GitHub -------------------------------------------
    assert plan.gh_calls() == [], (
        f"the gh stub was called: {plan.gh_calls()!r}"
    )


def test_spec_plan_runs_all_items_and_completes(boot_plan):
    """S2 —— 要求书里每条都通过：逐条 PASS，裸仓多出分支，审计三元组完整。

    这一条钉的是收尾阶段"真的把事情做了"：报告里的每一条 verdict 都是 PASS
    不是它自己说了算——判定器是独立的模块，它只看判据和证据记录；而命令
    真的执行过，由**裸仓里多出来的那条分支**作证。

    审计三元组 ``{action, command, target}`` 是"对外动作"的可追溯记录：人
    看到报告就能知道跑了哪条命令、动的是哪个目标。
    """
    plan = boot_plan(_book_all_pass())

    plan.start_verification()
    report = plan.wait_for_report()
    assert report is not None, (
        f"the finishing stage wrote no terminal report:\n{plan.server_output()}"
    )

    # --- the plan really went into the finishing phase --------------------
    # The plan is in the finishing phase, or it has been through it: either
    # way the gate was taken and the stage ran.
    assert plan.reached_finishing_phase(), (
        f"the plan never entered the finishing phase: {plan.routing_state()!r}"
    )

    # --- every item ran and every item passed -----------------------------
    assert report.get("terminal_status") == "completed", (
        f"the stage did not complete: {report!r}"
    )
    items = report.get("items")
    assert isinstance(items, list) and items, f"the report carries no items: {report!r}"

    by_id = {item.get("id"): item for item in items}
    assert set(by_id) == {"publish-branch", "read-head"}, (
        f"the report does not carry the book's items: {sorted(by_id)}"
    )
    for item_id, item in by_id.items():
        assert item.get("verdict") == "PASS", (
            f"item {item_id!r} did not pass: {item!r}"
        )
        assert item.get("evidence_excerpt"), (
            f"item {item_id!r} passed without quoting any evidence: {item!r}"
        )
        audit = item.get("audit")
        assert isinstance(audit, dict), f"item {item_id!r} carries no audit: {item!r}"
        assert audit.get("action"), f"item {item_id!r} audited no action: {audit!r}"
        command = audit.get("command")
        assert isinstance(command, list) and command, (
            f"item {item_id!r} audited no command: {audit!r}"
        )
        assert audit.get("target"), f"item {item_id!r} audited no target: {audit!r}"

    # --- the outward action really happened ------------------------------
    # The branch exists in the bare repo on disk.  Nothing else could put
    # it there: the repo was seeded with ``main`` alone.
    assert PUBLISHED_BRANCH in plan.bare_branches(), (
        f"the bare repo holds {plan.bare_branches()!r}, not the published branch"
    )

    # --- nothing was retried ----------------------------------------------
    progress = plan.progress()
    assert isinstance(progress, dict), f"the stage wrote no progress file: {progress!r}"
    for item_id, entry in (progress.get("items") or {}).items():
        assert entry.get("attempts") == 1, (
            f"item {item_id!r} was attempted {entry.get('attempts')!r} times, "
            "but nothing failed"
        )

    # --- nothing reached GitHub -------------------------------------------
    assert plan.gh_calls() == [], f"the gh stub was called: {plan.gh_calls()!r}"

    # --- the phase reaches the terminal -----------------------------------
    # The finishing stage owns the terminal exit: the plan leaves
    # ``post_processing`` for ``completed`` because every required item
    # passed.  The phase is read from the routing row in SQLite, and the
    # wait covers the whole chain (report, notification, phase).
    assert plan.wait_for_phase("completed", REPORT_TIMEOUT_SECONDS), (
        "the plan never reached 'completed': "
        f"{plan.routing_state()!r}\n{plan.server_output()}"
    )
    assert plan.status().get("verification_status") == "passed", (
        "the round's verdict was not recorded: "
        f"{plan.status()!r}"
    )


def test_failed_item_retried_once_then_postprocess_failed(boot_plan):
    """S3 —— 有一条目失败：只重跑失败的那条，终态是 ``postprocess_failed``。

    "只重跑失败条目一次"是这一条的核心：进度文件里每条目的 ``attempts`` 说
    了它被试了几次。失败的那条是 2（跑了一次、失败、再跑一次），通过的那条
    仍然是 1（**没有**被重跑）—— 一起证明第二轮的范围只有失败条目。

    两次都失败之后，收尾阶段的终态是 ``postprocess_failed``，报告里那条失败
    条目的 verdict 是 FAIL、通过的那条仍然是 PASS。
    """
    plan = boot_plan(_book_one_fails())

    plan.start_verification()
    report = plan.wait_for_report()
    assert report is not None, (
        f"the finishing stage wrote no terminal report:\n{plan.server_output()}"
    )

    # --- the plan really went into the finishing phase --------------------
    assert plan.reached_finishing_phase(), (
        f"the plan never entered the finishing phase: {plan.routing_state()!r}"
    )

    # --- the second round carried only the failed item --------------------
    progress = plan.progress()
    assert isinstance(progress, dict), f"the stage wrote no progress file: {progress!r}"
    entries = progress.get("items") or {}
    assert set(entries) == {"publish-branch", "read-head", "missing-ref"}, (
        f"the progress file does not carry the book's items: {sorted(entries)}"
    )
    assert entries["missing-ref"].get("attempts") == 2, (
        "the failed item was attempted "
        f"{entries['missing-ref'].get('attempts')!r} times, not twice "
        "(one attempt and one retry)"
    )
    assert entries["missing-ref"].get("status") == "failed", (
        f"the failed item's status is {entries['missing-ref'].get('status')!r}"
    )
    for item_id in ("publish-branch", "read-head"):
        assert entries[item_id].get("attempts") == 1, (
            f"item {item_id!r} was attempted {entries[item_id].get('attempts')!r} "
            "times, but it passed — only the failed item may be retried"
        )
        assert entries[item_id].get("status") == "passed", (
            f"item {item_id!r} ended {entries[item_id].get('status')!r}"
        )

    # --- the terminal status and the per-item verdicts ---------------------
    assert report.get("terminal_status") == "postprocess_failed", (
        f"the stage did not end at postprocess_failed: {report!r}"
    )
    verdicts = {item.get("id"): item.get("verdict") for item in report.get("items") or []}
    assert verdicts.get("missing-ref") == "FAIL", (
        f"the failed item is not a FAIL in the report: {verdicts!r}"
    )
    for item_id in ("publish-branch", "read-head"):
        assert verdicts.get(item_id) == "PASS", (
            f"item {item_id!r} is not a PASS in the report: {verdicts!r}"
        )
    assert report.get("failure_context_ref"), (
        "a failed stage must point at the file a human resumes from: "
        f"{report!r}"
    )

    # --- the phase reaches the terminal -----------------------------------
    # The finishing stage owns the terminal exit here too: a failed
    # required item parks the plan at ``postprocess_failed`` instead of
    # ``completed``.
    assert plan.wait_for_phase("postprocess_failed", REPORT_TIMEOUT_SECONDS), (
        "the plan never reached 'postprocess_failed': "
        f"{plan.routing_state()!r}\n{plan.server_output()}"
    )


def test_failure_notification_sent_once(boot_plan):
    """S3 —— 失败通知的审计行**恰好一行**，且是三段式。

    通知模块的设计说得很直白：审计行里的一行意思是"这条已经发出去了"，所以
    每次**真的投递**追加一行，不多不少；而没有通道的时候一行都不写，因为
    "一条没发出去的通知"不能被记成"已经通知过了"。

    三段式是通知的内容：plan、失败的条目、证据路径 —— 不套 CPEA，也不带
    别的散文。

    这一条与 S3 的另一条读的是同一份磁盘事实，坏掉的却是另一处：报告说
    "失败"，而**通知本身**没有发出去。
    """
    plan = boot_plan(_book_one_fails())

    plan.start_verification()
    report = plan.wait_for_report()
    assert report is not None, (
        f"the finishing stage wrote no terminal report:\n{plan.server_output()}"
    )
    assert report.get("terminal_status") == "postprocess_failed", (
        f"the stage did not end at postprocess_failed: {report!r}"
    )
    failed = [
        item.get("id")
        for item in report.get("items") or []
        if item.get("verdict") == "FAIL"
    ]
    assert failed == ["missing-ref"], (
        f"the report does not name the failed item: {failed!r}"
    )

    # The notification would be delivered right after the report lands, so
    # the file is given a real budget before it is called missing.
    _wait_for(
        lambda: plan.notification_lines() or None,
        NOTIFICATION_TIMEOUT_SECONDS,
    )
    lines = plan.notification_lines()

    # --- exactly one line --------------------------------------------------
    assert len(lines) == 1, (
        f"the audit file carries {len(lines)} lines, not exactly one: {lines!r}"
    )
    line = lines[0]
    assert isinstance(line, dict), f"the audit line is not an object: {line!r}"

    # --- the three-part payload -------------------------------------------
    assert set(line) == {"plan_id", "failed_items", "evidence_path"}, (
        f"the notification is not the three-part message: {sorted(line)}"
    )
    assert line["plan_id"] == plan.plan_id, (
        f"the notification names another plan: {line!r}"
    )
    assert line["failed_items"] == failed, (
        f"the notification names other failed items: {line!r}"
    )
    assert line["evidence_path"], (
        f"the notification points at no evidence: {line!r}"
    )
    assert Path(line["evidence_path"]).name == "postprocess_progress.json", (
        f"the notification points at no failure context: {line!r}"
    )

    # --- it really went out through the plan's own channel -----------------
    # The audit line says "delivered"; the delivery record proves it.  The
    # channel is the plan's own ``hitl_config.json`` binding, pointed at a
    # local destination — so this is a delivery that never touches the
    # network.
    deliveries = plan.channel_deliveries()
    assert len(deliveries) == 1, (
        f"the channel carries {len(deliveries)} deliveries, not exactly one: "
        f"{deliveries!r}"
    )
    delivery = deliveries[0]
    assert delivery.get("event") == "postprocess_failed", (
        f"the delivery is not the finishing-stage event: {delivery!r}"
    )
    assert delivery.get("plan_id") == plan.plan_id, (
        f"the delivery names another plan: {delivery!r}"
    )
    assert delivery.get("failed_items") == failed, (
        f"the delivery names other failed items: {delivery!r}"
    )
    assert delivery.get("evidence_path") == line["evidence_path"], (
        f"the delivery and the audit line disagree about the evidence: "
        f"{delivery!r} vs {line!r}"
    )
