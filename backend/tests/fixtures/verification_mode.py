"""The fixtures an end-to-end verification run is handed, and the rule that
every one of them is handed back.

What this module is for
-----------------------
A verification run has to be reproducible: the same run, on another
machine, must see the same upstream commit, the same plan and the same LLM
answers.  Every source of variation is therefore pinned *here*, and pinned as
a fixture rather than as a convention — a convention is what a test forgets
at 2am.

  * :func:`upstream_repo` — a local git repository with two commits and a
    ``refs/pull/7/head``.  The commit dates and the identity are fixed, so
    two constructions produce the same SHA and two verdicts can be compared
    with each other.
  * :func:`minimal_prd` — the smallest PRD that still carries a decision
    point, i.e. the input the plan import reads.
  * :func:`scripted_llm` — the scripted double: every LLM answer comes from
    a file on disk, so the run never reaches a provider.
  * :func:`workspace_root` — where the verification worktrees live.  The
    root is forced into the test's own tmp tree, and ``$HOME`` is redirected
    with it, so nothing ever lands in the operator's real ``~/.pdt``.
  * :func:`verification_plan` — a plan directory holding the PRD, a
    structurally valid ``verification-plan.md`` and a *frozen* baseline,
    with the worktree the run works in already materialised.
  * :func:`running_server` — a real FastAPI instance on a kernel-assigned
    port, with its own plans tree, its own state database and the scripted
    LLM switched on in its environment.

The rule the module enforces
----------------------------
Everything a test starts is handed back: the worktrees, the server, the
ports.  :func:`reclaim_verification_resources` removes what was registered,
:func:`detect_leftover_resources` reports what is still on disk, and
:func:`assert_no_leftover_resources` turns a leftover into a failure.  The
suite's own teardown (``backend/tests/conftest.py``) calls them, so a test
that starts a worktree and forgets about it *fails* instead of quietly
littering a directory tree that nobody remembers to clean up.

Nothing here reaches the network: the upstream repository is a directory,
the LLM answers from files, and the server binds to loopback.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_HERE = Path(__file__).resolve()
_BACKEND_DIR = _HERE.parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_plan_doc import PLAN_DOC_FILENAME  # noqa: E402
from verification_workspace import (  # noqa: E402
    WORKSPACE_ROOT_ENV,
    Baseline,
    Workspace,
    ensure_workspace,
)

__all__ = [
    "CHILD_BACKEND_DIR_ENV",
    "PLAN_ID",
    "PR_NUMBER",
    "PR_REF",
    "PRD_FILENAME",
    "MinimalPrd",
    "RunningServer",
    "ScriptedLlm",
    "UpstreamRepo",
    "VerificationPlan",
    "assert_no_leftover_resources",
    "build_plan_doc",
    "build_scripted_llm",
    "build_upstream_repo",
    "child_bootstrap_dir",
    "clear_verification_registries",
    "detect_leftover_resources",
    "minimal_prd",
    "reclaim_and_verify",
    "reclaim_verification_resources",
    "remove_worktree",
    "running_server",
    "scripted_llm",
    "upstream_repo",
    "verification_plan",
    "workspace_root",
]


# ---------------------------------------------------------------------------
# The pinned facts
# ---------------------------------------------------------------------------

#: The plan id every fixture run uses.  A date prefix keeps it readable in a
#: directory listing; the suffix says what it is.  It is a constant so the
#: same plan shows up under the same name in every run.
PLAN_ID: str = "20261024-fixture"

#: The pull request the upstream fixture repository carries.  A pull request
#: head is what a verification run verifies, so the fixture has one.
PR_NUMBER: int = 7
PR_REF: str = f"refs/pull/{PR_NUMBER}/head"

#: The PRD's filename inside a plan directory.
PRD_FILENAME: str = "prd.md"

#: The prompt fragment that identifies the verification-plan generation
#: prompt.  The scripted double matches on a fragment rather than on the
#: whole prompt, because the whole prompt carries a timestamp and a plan id.
PLAN_DOC_MATCHER: str = "你要写一份「验证计划书」"

#: The key the scripted answer for that prompt is registered under.
PLAN_DOC_KEY: str = "verification_plan"

#: The identity and the dates that make a commit SHA reproducible.
#:
#: The dates are fixed and the identity is a reserved ``.invalid`` address,
#: so the SHA is a function of the tree and of the message alone.  The
#: developer's own git configuration is neutralised as well: a global
#: ``commit.gpgsign`` or ``core.autocrlf`` would otherwise make the SHA a
#: function of the machine the test ran on.
_GIT_ENV: Dict[str, str] = {
    "GIT_AUTHOR_NAME": "PDT Verification Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "PDT Verification Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git_env() -> Dict[str, str]:
    """The environment that makes git deterministic and offline."""
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return env


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout, stripped."""
    completed = _git_result(repo, *args)
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return (completed.stdout or "").strip()


def _git_result(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``git`` inside ``repo`` and return the completed process."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )


# ---------------------------------------------------------------------------
# The upstream repository
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamRepo:
    """The repository under verification.

    ``repo`` is the directory, ``pr_ref`` the pull request head the
    baseline is frozen from, and ``head_sha()`` the commit that ref names.
    """

    repo: Path
    pr_ref: str = PR_REF
    pr_number: int = PR_NUMBER

    def head_sha(self) -> str:
        """The commit the pull request head names."""
        return _git(self.repo, "rev-parse", self.pr_ref)

    def base_sha(self) -> str:
        """The commit the pull request head's parent is."""
        return _git(self.repo, "rev-parse", f"{self.pr_ref}~1")

    def git(self, *args: str) -> str:
        """Run ``git`` in this repository and return its stdout."""
        return _git(self.repo, *args)


def build_upstream_repo(path: Path) -> UpstreamRepo:
    """Build the upstream repository at ``path``.

    Two commits: the first stands for the reviewed ancestor, the second for
    the pull request head under verification.  ``refs/pull/7/head`` points
    at the second, which is how a pull request head looks to a client that
    fetched the pull request ref.

    The content is written as bytes rather than as text: ``Path.write_text``
    translates the newlines, and a translated newline is a different tree
    and therefore a different SHA on another platform.
    """
    repo = Path(path)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    (repo / "README.md").write_bytes(b"baseline\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    (repo / "feature.txt").write_bytes(b"pr head\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "--quiet", "-m", "pr head")
    _git(repo, "update-ref", PR_REF, "HEAD")
    return UpstreamRepo(repo=repo)


# ---------------------------------------------------------------------------
# The PRD and the scripted LLM
# ---------------------------------------------------------------------------

#: The smallest PRD that still carries a decision point.  A PRD without one
#: has nothing for the plan to be derived from, and a PRD with three is a
#: document the fixture has to keep in sync with the plan it produces.
_MINIMAL_PRD: str = """# PRD — 登录接口

## 概述
实现一个登录接口，校验用户名与密码，失败率低于 1%。

## 决策点列表

### 决策点 1: 凭据校验方式
**[C] 背景：** 登录接口需要校验用户名与密码
**[P] 问题：** 用什么方式校验凭据
**[E] 评估：** 哈希比对足够，无需引入外部身份服务
**[A] 行动：** 用哈希比对校验凭据，失败率要求 <1%

影响范围：认证模块
备选方案：① 外部身份服务 ② 明文比对

## 约束条件
- 技术栈：Python FastAPI
- 失败率要求：<1%

## 验收标准
1. 正确凭据可以登录
2. 错误凭据被拒绝，且拒绝原因对用户可见
"""


@dataclass(frozen=True)
class MinimalPrd:
    """The PRD file the fixture wrote."""

    path: Path
    text: str

    def __fspath__(self) -> str:
        """So a ``MinimalPrd`` can be handed to anything that wants a path."""
        return str(self.path)


def build_plan_doc(
    repo_dir: Path,
    pr_number: int = PR_NUMBER,
    ref: str = PR_REF,
) -> str:
    """A verification plan document that passes the structure contract.

    The document is written by hand rather than by the model, because the
    point of the fixture is a document that is *already* valid: the
    structure check is the model boundary's job, and a fixture that failed
    it would turn every run into a formatting exercise.
    """
    baseline_line = f"baseline: repo={repo_dir} ref={ref} pr={pr_number}"
    return f"""## 被验证对象

{baseline_line}

被验证对象是上游仓库 {repo_dir} 的 {ref}。

## 验证范围

上游仓库该 pull request 头提交引入的改动。

## 排除范围

上游仓库里与本次改动无关的历史提交与其它分支。

## 验证方法

automated_test：在被验证对象自己的检出上跑一遍它自己的测试命令。

## 验证点清单

### VP-001 pull request 头提交可被检出

- 范围：{ref} 指向的提交
- 验证方法：automated_test
- 通过判据：
  - C1: git rev-parse {ref} 解析出一个提交号
- 证据要求：
  - E1: 该命令的 stdout 原文
- 预期结果：解析出一个 40 位十六进制提交号

## 预期结果

验证点清单里的每一条都通过。

## 证据要求

每个验证点的命令输出原文。

## 通过判据

全部验证点通过即本轮通过。

## 环境与依赖

git；不需要网络。

## 风险面

工作区写错仓库；由工作区根目录守卫拦下。
"""


@dataclass(frozen=True)
class ScriptedLlm:
    """The scripted double's fixture directory."""

    script_dir: Path

    def __fspath__(self) -> str:
        """So a ``ScriptedLlm`` can be handed to anything that wants a path."""
        return str(self.script_dir)


def build_scripted_llm(
    script_dir: Path,
    repo_dir: Path,
    pr_number: int = PR_NUMBER,
    ref: str = PR_REF,
) -> Path:
    """Write a scripted-LLM fixture directory and return its path.

    The directory holds the registry (which prompt answers under which key)
    and one response file per key.  The response for the plan-generation
    prompt is a document that is already structurally valid, so a run that
    asks for it gets a plan rather than an error.
    """
    script_dir = Path(script_dir)
    script_dir.mkdir(parents=True, exist_ok=True)
    registry = {"prompts": {PLAN_DOC_KEY: PLAN_DOC_MATCHER}}
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )
    (script_dir / f"{PLAN_DOC_KEY}.txt").write_text(
        build_plan_doc(repo_dir, pr_number, ref), encoding="utf-8"
    )
    return script_dir


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationPlan:
    """The plan a verification run works on.

    It carries the plan id, the directory, the two documents inside it, the
    frozen baseline and the materialised workspace — everything an
    end-to-end run needs to point at a real plan.
    """

    plan_id: str
    plan_dir: Path
    doc_path: Path
    prd_path: Path
    baseline: Dict[str, Any]
    workspace: Workspace
    upstream: UpstreamRepo
    workspace_root: Path

    def manifest(self) -> Dict[str, Any]:
        """The fixture set as one mapping — what a run is made of."""
        return {
            "repo": self.upstream.repo,
            "pr_ref": self.upstream.pr_ref,
            "plan_id": self.plan_id,
            "workspace_root": self.workspace_root,
        }


def _resolve_plans_dir() -> Path:
    """The plans tree this process is writing into right now."""
    from config_paths import resolve_plans_dir

    return resolve_plans_dir()


def _record_baseline(plan_dir: Path, baseline: Dict[str, Any]) -> None:
    """Freeze ``baseline`` into the plan's state.

    The baseline is written through the repository layer rather than
    through a hand-made file: the reader under test looks for it where the
    freeze step puts it, and a file written beside it would be a second
    source of truth.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_dir.name,
            phase="verification",
            completed_phases=[],
            review_rounds={},
            flags={},
            verification={
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
                "baseline": baseline,
            },
            last_updated=None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The child's bootstrap
# ---------------------------------------------------------------------------
#
# Why a child process needs its own copy of the stub
# ---------------------------------------------------
# ``provider_order.load_fallback_order()`` asks CC Switch's HTTP API for the
# provider chain and raises ``ProviderOrderError`` when it does not answer.
# CC Switch is another application: on a clean CI checkout it is not running,
# so a spawned server dies in its lifespan for an environment reason rather
# than a code one.
#
# The test process solves that with ``_pytest_hermetic_env
# .install_provider_chain_stub()`` — which is a **monkeypatch in this
# process**.  A child process does not inherit a monkeypatch: it starts from
# the interpreter's defaults, asks CC Switch, and dies in the lifespan.
#
# The hook that reaches a child is ``sitecustomize``: the ``site`` module
# imports it at interpreter startup, and it looks for it on ``sys.path`` — so
# a directory holding one on the child's ``PYTHONPATH`` is enough.  There is
# no other hook that needs no change to the production code.

#: The environment variable that tells the child where the backend package
#: directory is.  The stub installs itself by importing backend modules, and
#: those are imported by their **top-level** name (``import provider_order``),
#: so the backend directory has to be on the child's ``sys.path`` — which is
#: what the parent process has, and what the child would not have on its own.
CHILD_BACKEND_DIR_ENV = "PDT_TEST_BACKEND_DIR"

#: The child's ``sitecustomize.py``.
#:
#: It is a *string* rather than a file in the repository for the same reason
#: the fixtures live in a tmp tree: a file named ``sitecustomize.py`` in the
#: repository would be imported by every interpreter that happens to have the
#: repository on its ``PYTHONPATH`` — including the developer's own shell.
#:
#: The interpreter's own ``sitecustomize`` (Homebrew's Python ships one, and
#: it fixes up ``sys.prefix``) is **shadowed** by this file, so it is run
#: first, explicitly: shadowing it would change the child's interpreter for
#: no reason.
_CHILD_SITECUSTOMIZE_SOURCE = '''\
"""Install the provider-chain stub in a spawned child process.

Run by the ``site`` module at interpreter startup.  See
``backend/tests/fixtures/verification_mode.py`` for why this exists.
"""

import os
import sys


def _run_interpreter_sitecustomize():
    """Run the interpreter's own ``sitecustomize``, if it has one.

    This file shadows it (it comes first on ``PYTHONPATH``), so the original
    is run explicitly to keep the child's interpreter the same as it would
    have been.
    """
    import sysconfig

    here = os.path.dirname(os.path.abspath(__file__))
    for key in ("stdlib", "platstdlib"):
        try:
            directory = sysconfig.get_path(key)
        except Exception:
            continue
        candidate = os.path.join(directory, "sitecustomize.py")
        if not os.path.isfile(candidate):
            continue
        if os.path.dirname(os.path.abspath(candidate)) == here:
            continue
        with open(candidate, "r", encoding="utf-8") as handle:
            source = handle.read()
        exec(
            compile(source, candidate, "exec"),
            {"__file__": candidate, "__name__": "sitecustomize"},
        )
        return


def _install_provider_chain_stub():
    """Install the stub the test process installs, in this process too."""
    backend = os.environ.get("PDT_TEST_BACKEND_DIR")
    if backend and backend not in sys.path:
        sys.path.insert(0, backend)
    try:
        from _pytest_hermetic_env import install_provider_chain_stub
    except Exception:
        return
    install_provider_chain_stub()


_run_interpreter_sitecustomize()
_install_provider_chain_stub()
'''

#: The directory holding the child's ``sitecustomize.py``; ``None`` until
#: :func:`child_bootstrap_dir` makes it.
_CHILD_BOOTSTRAP_DIR: Optional[Path] = None


def child_bootstrap_dir() -> Path:
    """The directory holding the child's ``sitecustomize.py``.

    One per process: the file is the same for every child, so making it once
    is enough.  It lives in the system temp directory and is removed when the
    test process exits — it is a bootstrap, not a fixture, so it is not handed
    back through the reclamation.

    Returns:
        The directory to put on a child's ``PYTHONPATH``.
    """
    global _CHILD_BOOTSTRAP_DIR
    if _CHILD_BOOTSTRAP_DIR is not None:
        return _CHILD_BOOTSTRAP_DIR
    import atexit
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="pdt-child-bootstrap-"))
    (directory / "sitecustomize.py").write_text(
        _CHILD_SITECUSTOMIZE_SOURCE, encoding="utf-8"
    )
    atexit.register(shutil.rmtree, str(directory), True)
    _CHILD_BOOTSTRAP_DIR = directory
    return directory


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused loopback port, then let it go."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port_released(port: int, timeout: float = 10.0) -> bool:
    """True once ``port`` can be bound again, False if it is still held."""
    deadline = time.monotonic() + timeout
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        finally:
            probe.close()


def _child_path() -> str:
    """PATH for a spawned server: this process's, plus the venv if any."""
    parts = [os.path.dirname(sys.executable)]
    parts += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return os.pathsep.join(parts)


@dataclass
class RunningServer:
    """A live FastAPI instance on an ephemeral port."""

    port: int
    proc: subprocess.Popen
    log_path: Path
    stopped: bool = False

    @property
    def base_url(self) -> str:
        """The server's root URL."""
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        """Whether the server process is still running."""
        return self.proc.poll() is None

    def get(self, path: str, timeout: float = 5.0) -> int:
        """GET ``path`` and return the status code."""
        from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE

        request = urllib.request.Request(
            self.base_url + path,
            headers={REQUEST_HEADER: REQUEST_HEADER_VALUE},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)

    def port_released(self, timeout: float = 2.0) -> bool:
        """Whether the port is free again."""
        return _wait_port_released(self.port, timeout=timeout)

    def stop(self) -> None:
        """Stop the server and confirm the port is free again.

        The order is the point: ``terminate`` only *asks*, ``wait`` reaps,
        and the port check runs after the reap — a check that runs before it
        sees a socket the kernel has not finished tearing down.
        """
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.stopped = True
        if not self.port_released():
            raise AssertionError(
                f"the spawned server (pid={self.proc.pid}, rc={self.proc.returncode}) "
                f"was stopped but :{self.port} is still bound. A listener this "
                "fixture started would outlive the test that spawned it and "
                "answer a later run's health probe."
            )

    def log_tail(self, limit: int = 2000) -> str:
        """The tail of the server's output — the only place its failure is written."""
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return "<server log unavailable>"


def _uvicorn_path() -> Optional[str]:
    """The uvicorn to spawn, or ``None`` when there is none."""
    found = shutil.which("uvicorn")
    if found:
        return found
    beside = Path(sys.executable).parent / "uvicorn"
    return str(beside) if beside.exists() else None


def spawn_server(
    log_path: Path,
    workspace_root: Path,
    scripted_llm: ScriptedLlm,
    timeout: float = 30.0,
) -> RunningServer:
    """Spawn a real FastAPI instance and wait until it answers.

    The child inherits this process's environment — which already carries
    the redirected plans tree, the redirected state database and the
    redirected home — and adds the verification workspace root and the
    scripted LLM switch on top.  Inheriting rather than re-deriving is what
    keeps the server and the test looking at the *same* plan.

    The child's ``PYTHONPATH`` also carries the **child bootstrap**: the
    provider-chain stub is a monkeypatch in this process, and a child does
    not inherit a monkeypatch.  Without it the child asks CC Switch for the
    provider chain, CC Switch is not running, and the server dies in its
    lifespan with ``ProviderOrderError`` — an environment failure, not a
    code one.
    """
    from coding_tool import LLM_MODE_ENV, LLM_MODE_SCRIPTED, LLM_SCRIPT_DIR_ENV

    uvicorn_path = _uvicorn_path()
    if uvicorn_path is None:
        raise RuntimeError(
            "uvicorn is not available; the verification-mode server fixture "
            "needs a real server instance to exercise"
        )

    env = dict(os.environ)
    pythonpath = [str(child_bootstrap_dir()), str(_REPO_ROOT)]
    inherited = env.get("PYTHONPATH")
    if inherited:
        pythonpath.append(inherited)
    env.update(
        {
            "PDT_VERIFICATION_WORKSPACE_ROOT": str(workspace_root),
            CHILD_BACKEND_DIR_ENV: str(_BACKEND_DIR),
            LLM_MODE_ENV: LLM_MODE_SCRIPTED,
            LLM_SCRIPT_DIR_ENV: str(scripted_llm.script_dir),
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "PATH": _child_path(),
        }
    )

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    server_log = open(log_path, "w+b")  # noqa: SIM115
    proc: Optional[subprocess.Popen] = None
    port = _free_port()
    ready = False
    try:
        proc = subprocess.Popen(  # noqa: S603
            [
                uvicorn_path,
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(_BACKEND_DIR),
            env=env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as response:
                    if 200 <= response.status < 300:
                        ready = True
                        break
            except (urllib.error.URLError, ConnectionResetError, OSError):
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.25)
    finally:
        server_log.close()

    if not ready:
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait(timeout=10)
        raise RuntimeError(
            f"the server on :{port} did not become healthy within {timeout}s. "
            f"Server output:\n{tail}"
        )

    return RunningServer(port=port, proc=proc, log_path=log_path)


# ---------------------------------------------------------------------------
# The registries and the reclamation
# ---------------------------------------------------------------------------

#: The worktrees this test registered, as ``{"repo": Path, "path": Path}``.
_WORKTREES: List[Dict[str, Path]] = []

#: The servers this test registered.
_SERVERS: List[RunningServer] = []

#: The workspace roots this test registered.  Only the roots registered here
#: are scanned for leftovers: a root a test made for itself is its own
#: business, and sweeping every workspace tree on the machine would report
#: other runs' leftovers as this run's failure.
_WORKSPACE_ROOTS: List[Path] = []


def clear_verification_registries() -> None:
    """Forget everything the previous test registered.

    Called at the start of every test, for the reason the suite's other
    registries are cleared there: a session interrupted mid-teardown would
    otherwise carry the last test's registrations into the next one.
    """
    _WORKTREES.clear()
    _SERVERS.clear()
    _WORKSPACE_ROOTS.clear()


def register_worktree(repo_dir: Path, path: Path) -> None:
    """Hand a worktree to the teardown; return it, so a call can be inlined."""
    _WORKTREES.append({"repo": Path(repo_dir), "path": Path(path)})
    return None


def register_workspace_root(root: Path) -> None:
    """Remember a workspace root, so leftovers under it can be found."""
    root = Path(root)
    if root not in _WORKSPACE_ROOTS:
        _WORKSPACE_ROOTS.append(root)


def register_server(server: RunningServer) -> RunningServer:
    """Hand a server to the teardown; return it, so a call can be inlined."""
    _SERVERS.append(server)
    return server


def remove_worktree(repo_dir: Path, path: Path) -> None:
    """Remove a worktree and its registration.

    ``git worktree remove`` is asked first, because that is what keeps the
    repository's own bookkeeping straight.  A directory it refuses to
    remove is removed directly and the registration is pruned — the point
    is that the tree is gone, not that git was happy about it.
    """
    repo_dir = Path(repo_dir)
    path = Path(path)
    completed = _git_result(repo_dir, "worktree", "remove", "--force", str(path))
    if completed.returncode != 0 and path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        _git_result(repo_dir, "worktree", "prune")
    for entry in list(_WORKTREES):
        if entry["path"] == path:
            _WORKTREES.remove(entry)


def reclaim_verification_resources() -> List[str]:
    """Reclaim every worktree and every server this test registered.

    Returns the problems that could not be reclaimed — a worktree git would
    not remove, a server that would not die.  A problem is reported rather
    than raised: the caller decides what a leftover means, and the caller
    is the suite's teardown.
    """
    problems: List[str] = []
    for entry in list(_WORKTREES):
        try:
            remove_worktree(entry["repo"], entry["path"])
        except Exception as exc:  # noqa: BLE001 - the report is the verdict
            problems.append(f"worktree {entry['path']} was not reclaimed: {exc}")
    for server in list(_SERVERS):
        try:
            server.stop()
        except Exception as exc:  # noqa: BLE001 - the report is the verdict
            problems.append(f"server on :{server.port} was not stopped: {exc}")
        else:
            _SERVERS.remove(server)
    return problems


def detect_leftover_resources() -> List[str]:
    """The resources that are still on disk after the reclamation.

    Two sources: the worktrees and servers the reclamation could not
    remove, and the directories that are still sitting under a workspace
    root this test registered.  The second is the one that catches a test
    that started something on its own and never said so.
    """
    leftovers: List[str] = []
    for entry in _WORKTREES:
        leftovers.append(f"verification worktree left behind: {entry['path']}")
    for server in _SERVERS:
        leftovers.append(f"verification server left running on :{server.port}")
    for root in _WORKSPACE_ROOTS:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir():
                leftovers.append(f"verification workspace left behind: {child}")
    return leftovers


def assert_no_leftover_resources() -> None:
    """Fail when anything is left behind.

    The failure names every leftover, because "something is left" is not a
    report a reader can act on — "this directory is still there" is.
    """
    leftovers = detect_leftover_resources()
    if leftovers:
        raise AssertionError(
            "the test left resources behind:\n  "
            + "\n  ".join(leftovers)
            + "\nEvery test hands back what it starts: a worktree or a server "
            "that outlives its test keeps writing into a later run's tree."
        )


def reclaim_and_verify() -> None:
    """Reclaim everything this test started, and fail if anything survived.

    This is the whole rule in one call: the reclamation runs first, and
    then whatever is still on disk is a leftover — the thing that has to
    fail the test.
    """
    problems = reclaim_verification_resources()
    problems.extend(detect_leftover_resources())
    if problems:
        raise AssertionError(
            "the test left resources behind:\n  "
            + "\n  ".join(problems)
            + "\nEvery test hands back what it starts: a worktree or a server "
            "that outlives its test keeps writing into a later run's tree."
        )


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_repo(tmp_path: Path) -> UpstreamRepo:
    """A local repository whose ``refs/pull/7/head`` points at the second commit.

    The first commit stands for the reviewed ancestor and the second for
    the pull request head under verification.  The dates and the identity
    are fixed, so two constructions produce the same SHA.
    """
    return build_upstream_repo(tmp_path / "upstream")


@pytest.fixture
def minimal_prd(tmp_path: Path) -> MinimalPrd:
    """The smallest PRD that still carries a decision point."""
    path = tmp_path / "prd.md"
    path.write_bytes(_MINIMAL_PRD.encode("utf-8"))
    return MinimalPrd(path=path, text=_MINIMAL_PRD)


@pytest.fixture
def scripted_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_repo: UpstreamRepo,
) -> ScriptedLlm:
    """The scripted LLM double, switched on.

    The switch and the fixture directory are the ones the production code
    reads, so a run that asks the model for a plan gets the fixture's
    answer — and the run never reaches a provider.
    """
    from coding_tool import LLM_MODE_ENV, LLM_MODE_SCRIPTED, LLM_SCRIPT_DIR_ENV

    script_dir = build_scripted_llm(tmp_path / "llm-scripts", upstream_repo.repo)
    monkeypatch.setenv(LLM_MODE_ENV, LLM_MODE_SCRIPTED)
    monkeypatch.setenv(LLM_SCRIPT_DIR_ENV, str(script_dir))
    return ScriptedLlm(script_dir=script_dir)


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the verification worktrees live — inside the test's own tmp tree.

    The override is forced rather than merely honoured, and ``$HOME`` is
    redirected with it: the default root is ``~/.pdt/verification-workspaces``,
    and a test that fell back to it would write into the operator's home.
    With the home redirected as well, the fallback lands in tmp too.

    The redirected home gets the same empty CC Switch database the session's
    own home has.  A redirected home without one is not a home the
    application can boot in: ``load_providers`` reads that database during
    startup and re-raises a missing one out of the lifespan, so a server
    spawned under a half-built home dies before it answers ``/health``.
    """
    from _pytest_hermetic_env import write_minimal_cc_switch_db

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    write_minimal_cc_switch_db(home / ".cc-switch")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = tmp_path / "verification-workspaces"
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(root))
    register_workspace_root(root)
    return root


@pytest.fixture
def verification_plan(
    upstream_repo: UpstreamRepo,
    minimal_prd: MinimalPrd,
    scripted_llm: ScriptedLlm,
    workspace_root: Path,
) -> VerificationPlan:
    """A plan directory with a PRD, a valid plan document and a frozen baseline.

    The worktree the run works in is materialised here too, through
    :func:`verification_workspace.ensure_workspace` — the same call the
    production flow makes — and registered, so the teardown hands it back.
    """
    plan_dir = _resolve_plans_dir() / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    prd_path = plan_dir / PRD_FILENAME
    prd_path.write_bytes(minimal_prd.path.read_bytes())

    doc_path = plan_dir / PLAN_DOC_FILENAME
    doc_path.write_bytes(
        build_plan_doc(
            upstream_repo.repo, upstream_repo.pr_number, upstream_repo.pr_ref
        ).encode("utf-8")
    )

    baseline = {
        "repo_dir": str(upstream_repo.repo),
        "ref": upstream_repo.pr_ref,
        "commit_sha": upstream_repo.head_sha(),
        "pr_number": upstream_repo.pr_number,
    }
    _record_baseline(plan_dir, baseline)

    workspace = ensure_workspace(
        PLAN_ID,
        Baseline(
            repo_dir=upstream_repo.repo,
            ref=upstream_repo.pr_ref,
            commit_sha=baseline["commit_sha"],
            pr_number=upstream_repo.pr_number,
        ),
    )
    register_worktree(upstream_repo.repo, workspace.path)

    return VerificationPlan(
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        doc_path=doc_path,
        prd_path=prd_path,
        baseline=baseline,
        workspace=workspace,
        upstream=upstream_repo,
        workspace_root=workspace_root,
    )


@pytest.fixture
def running_server(
    tmp_path: Path,
    workspace_root: Path,
    scripted_llm: ScriptedLlm,
) -> RunningServer:
    """A real FastAPI instance on a kernel-assigned port.

    The plans tree, the state database and the home are the ones this
    process is already using, so the server and the test see the same
    plan.  The port is the one the kernel handed out, and it is free again
    once the fixture is done.
    """
    server = spawn_server(
        tmp_path / "uvicorn.log", workspace_root, scripted_llm
    )
    register_server(server)
    try:
        yield server
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - the teardown reports, never hides
            pass
        if server in _SERVERS:
            _SERVERS.remove(server)
