"""共存目录的完整用户流程 —— 真实子进程后端的端到端。

这个文件回答一个问题：**一个 plan 目录里同时躺着 ``tasks.json`` 与
``verification-plan.md`` 时，一个真人从零开始的流程能不能走通？**

走通的形状（本文件逐步走完，每一步都对着**真实后端进程**发真实请求）：

1. **生成** —— ``POST /api/verification-plan/{id}/generate``：后端向模型要一份
   验证计划书，落盘。
2. **逐条审阅** —— ``GET .../review/items`` 取条目，逐条
   ``POST .../review/item/{index}`` 记 ``accept``。
3. **refine** —— ``POST .../refine``：按反馈改写计划书。文档字节变了，之前那份
   审核针对的是另一份文档，条目全部回到 pending。
4. **再审阅** —— 重新逐条 accept。
5. **启动验证轮** —— ``POST /api/verification/{id}/start``：机器计划由审核过的
   计划书翻译而来并落盘，验证轮启动。
6. **第二次启动** —— 再发一次 ``/start``：机器计划已经在盘上，**不再翻译**。

「不再翻译」这句话由两条用例分担：主用例数**调用次数**（翻译桶第二次是 0），
:func:`test_second_start_is_idempotent` 数**产物**（盘上那份机器计划的 sha256
在第二次启动前后一字不差）。第二条还多做一件事：第二次启动之前把翻译用的
fixture 换成**不等价**的内容 —— 于是「第二次启动仍然返回 200」就成了一句
有分量的断言（问了模型就会拿到那份内容，就会被等价性门禁拒成 422）。

断言按**调用点分桶**
------------------
每一次模型调用都落在四个桶之一（桶的判据是**提示词的身份**，不是调用点的
位置 —— 位置会变，身份不会）：

=================  ======  ================================================
桶                 次数    为什么
=================  ======  ================================================
``generate``      1       计划书只生成一次
``refine``        1       反馈只改写一次
``translate``     1       机器计划只翻译一次（第二次启动复用盘上那份）
``phase1``        0       共存目录的计划书是**真源**，Phase-1 一次都不该跑
=================  ======  ================================================

计数是**活的**：同一份记录里另外三桶都是非零 —— 「Phase-1 是 0」因此是
「没跑过」，不是「没数到」。一个恒为零的计数器和一个死掉的计数器长得一模一样，
所以对照组（:func:`test_the_counter_sees_the_calls_it_is_counting`）单独钉住
「计数器看得见调用」这件事。

为什么是子进程
--------------
流程里有几段只有**真后端**才有：状态机的 CAS、验证轮的工作线程、机器计划的
原子落盘。在进程内用 ``TestClient`` 走一遍，走的是同一段路由代码，但**没有**
子进程边界：模块级状态、后台线程、文件系统时序全是同一个解释器里的。
所以这里起一个真的 uvicorn 子进程，让它读同一棵计划树、同一个 SQLite 库。

子进程里怎么数模型调用
----------------------
模型调用发生在子进程里，父进程看不见。办法是**在子进程里数**：本文件给子进程
的 ``PYTHONPATH`` 前面放一个自带的 ``sitecustomize.py``（见
:data:`_CHILD_SITECUSTOMIZE_SOURCE`），它在 ``coding_tool`` 被导入的那一刻给
:class:`ScriptedCodingTool` 的两个入口装上计数，调用一次就往计数文件里追加
一行（提示词与系统提示词原文）。分类留给父进程 —— 桶的判据只写一份。

计数器文件是父子之间唯一的通道，所以它也得能被**证伪**：计数文件为空时，
桶全是 0，断言「Phase-1 是 0」就成了空转。:func:`test_the_counter_sees_the_calls_it_is_counting`
就是钉这一点的。

``scripts/negctl_p0_plan_source.sh`` 是本文件的负控：它把子进程的来源判定强行
钉成 ``PlanSource.NONE``，机器计划于是不落盘，本文件的断言转红 —— 证明这些
断言不是空转。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
from tests.fixtures.verification_mode import (
    CANONICAL_ITEM_COUNT,
    build_machine_plan,
    build_plan_doc,
    build_upstream_repo,
    hermetic_env,  # noqa: F401  (fixture re-exported into this module)
)

#: 这些用例起**真子进程**、写真实文件系统、连真实端口 —— 它们不属于「纯单元
#: 测试」那条 lane（那条 lane 是 8 个 worker 并行、每例 60 秒上限），也不属于
#: ``integration`` lane（那条按目录收 ``tests/integration/``）。它们属于
#: ``e2e`` lane：测试设计决策点 8 的 lane 分配表把本文件放进 ``e2e-on-demand``。
#:
#: ``pytestmark`` 是模块级的，所以本文件与它的伴随文件走同一条 lane —— 一条
#: 流程和它的对照用例不该被拆到两条 lane 上。
pytestmark = [pytest.mark.e2e]

#: 本文件所在目录，以及它上溯出来的后端目录与仓库根。
#:
#: 用 ``__file__`` 上溯而不是写死路径：这份测试要在别人的机器上、在别的
#: checkout 里跑，写死绝对路径等于让它只在作者机器上成立。
_HERE: Path = Path(__file__).resolve()
_BACKEND_DIR: Path = _HERE.parents[2]
_REPO_ROOT: Path = _HERE.parents[3]

#: 这条流程用的 plan id。
#:
#: 形状受 plan id 校验器管（扁平单段、只含安全字符、长度有上限）—— 与生产
#: 走的是同一个校验器，所以这里写错会在第一次请求就被 400 挡下。
PLAN_ID: str = "coexist-e2e"

#: 子进程里那个计数文件的**环境变量名**。
COUNTER_ENV: str = "PDT_E2E_LLM_COUNTER"

#: 负控用的「强行改写来源判定」的环境变量名。
#:
#: 父进程改不了子进程里的函数，所以负控走环境变量：子进程的 ``sitecustomize``
#: 看到它就把 :func:`plan_artifacts.resolve_plan_source` 换成恒返回
#: ``PlanSource.NONE`` 的替身。
FORCED_SOURCE_ENV: str = "PDT_E2E_FORCED_PLAN_SOURCE"

#: 四个桶的判据：``(提示词里的标记, 桶名)``，**按顺序**匹配。
#:
#: 顺序有意义：先认最特殊的标记。翻译的系统提示词里带「验证执行工程师」，
#: Phase-1 的带「资深测试架构师」，计划书的生成/改写各带一句固定的开场白。
#: 四句互不包含，所以顺序其实不改变结果 —— 但写成有序表，读者一眼能看出
#: 「先匹配到的赢」。
BUCKETS: Tuple[Tuple[str, str], ...] = (
    ("资深测试架构师", "phase1"),
    ("验证执行工程师", "translate"),
    ("待补全的验证点骨架", "translate"),
    ("你要改写一份「验证计划书」", "refine"),
    ("你要写一份「验证计划书」", "generate"),
)

#: 没有命中任何判据的调用落进的桶。
OTHER_BUCKET: str = "other"


def bucket_for(record: Dict[str, Any]) -> str:
    """把一条模型调用记录归进某个桶。

    判据是**提示词的身份**：系统提示词与用户提示词拼在一起，看它是不是
    那一类调用的那句开场白。位置会变（同一个端点在不同分支上调不同的提示词），
    身份不会。

    Args:
        record: 计数文件里的一行（``{"system": ..., "prompt": ...}``）。

    Returns:
        桶名；命中不了判据时是 :data:`OTHER_BUCKET`。
    """
    text = f"{record.get('system') or ''}\n{record.get('prompt') or ''}"
    for marker, bucket in BUCKETS:
        if marker in text:
            return bucket
    return OTHER_BUCKET


def count_buckets(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """把调用记录按桶计数。

    Args:
        records: 计数文件里的全部记录。

    Returns:
        每个桶的次数（没出现过的桶不在表里）。
    """
    counts: Dict[str, int] = {}
    for record in records:
        bucket = bucket_for(record)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 子进程的引导：计数 + 负控的来源钉死
# ---------------------------------------------------------------------------

_CHILD_SITECUSTOMIZE_SOURCE = '''\
"""Child-process instrumentation for the coexistence end-to-end test.

Run by the ``site`` module at interpreter startup of the spawned backend, so
it is in place before the backend imports anything of its own.

It does three things, and nothing else:

1. runs the interpreter's own ``sitecustomize`` (this file shadows it, and
   shadowing it would change the child's interpreter for no reason);
2. installs the provider-chain stub, so the child does not ask CC Switch for
   a provider that is not running;
3. counts the model calls: every scripted-model call appends one line to the
   file named by ``PDT_E2E_LLM_COUNTER``.

The classification into buckets happens in the parent process -- this file
only records what was said.
"""

import builtins
import json
import os
import sys

COUNTER_ENV = "PDT_E2E_LLM_COUNTER"
FORCED_SOURCE_ENV = "PDT_E2E_FORCED_PLAN_SOURCE"


def _run_interpreter_sitecustomize():
    """Run the interpreter's own ``sitecustomize``, if it has one."""
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


def _record(system_instruction, prompt):
    """Append one model call to the counter file."""
    path = os.environ.get(COUNTER_ENV)
    if not path:
        return
    record = {
        "system": system_instruction or "",
        "prompt": prompt or "",
    }
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\\n")
    except OSError:
        return


def _install_counter(module):
    """Count every scripted-model call."""
    original_query = module.ScriptedCodingTool.query
    original_query_json = module.ScriptedCodingTool.query_json

    def query(self, prompt, system_instruction=None, *args, **kwargs):
        _record(system_instruction, prompt)
        return original_query(self, prompt, system_instruction, *args, **kwargs)

    def query_json(self, prompt, system_instruction=None, *args, **kwargs):
        _record(system_instruction, prompt)
        return original_query_json(self, prompt, system_instruction, *args, **kwargs)

    module.ScriptedCodingTool.query = query
    module.ScriptedCodingTool.query_json = query_json


def _install_forced_source(module):
    """Pin the plan source to ``NONE`` (the negative control's switch)."""
    forced = (os.environ.get(FORCED_SOURCE_ENV) or "").strip().lower()
    if forced != "none":
        return

    def resolve_plan_source(plan_dir):
        return module.PlanSource.NONE

    module.resolve_plan_source = resolve_plan_source


def _import_hook(name, globals=None, locals=None, fromlist=(), level=0):
    """Patch the two modules the instrumentation cares about, on import."""
    module = _real_import(name, globals, locals, fromlist, level)
    try:
        if name == "coding_tool" and not getattr(module, "_e2e_counted", False):
            _install_counter(module)
            module._e2e_counted = True
        elif name == "plan_artifacts" and not getattr(module, "_e2e_forced", False):
            _install_forced_source(module)
            module._e2e_forced = True
    except Exception:
        pass
    return module


_real_import = builtins.__import__
builtins.__import__ = _import_hook

_run_interpreter_sitecustomize()
_install_provider_chain_stub()
'''


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused loopback port, then let it go."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop_process(proc: subprocess.Popen) -> None:
    """Stop a spawned backend and wait until it is really gone.

    ``terminate`` first, ``wait`` with a timeout, and ``kill`` only when the
    process refuses to go.  The **wait** is the load-bearing part: a process
    that is still winding down still holds its socket, so a port is only free
    again once the wait returned.
    """
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()
        proc.wait(timeout=10)


def _child_path() -> str:
    """PATH for the spawned server: this process's, plus the venv if any."""
    parts = [os.path.dirname(sys.executable)]
    parts += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return os.pathsep.join(parts)


def _uvicorn_command() -> Optional[List[str]]:
    """The command that starts the backend, or ``None`` when there is none.

    The interpreter **under test** comes first: the ``uvicorn`` sitting
    beside ``sys.executable`` (or the interpreter itself, running
    ``-m uvicorn``) belongs to the environment this test is asserting
    about.  The ambient ``PATH`` is only a fallback — a ``uvicorn`` found
    there is some other install, and a test that spawns it measures a
    different environment than the one it claims to measure.  Picking the
    ambient one first is how this test used to depend on whatever the
    operator happened to have on their ``PATH``.
    """
    beside = Path(sys.executable).parent / "uvicorn"
    if beside.exists():
        return [str(beside)]
    if importlib.util.find_spec("uvicorn") is not None:
        return [sys.executable, "-m", "uvicorn"]
    found = shutil.which("uvicorn")
    return [found] if found else None


class Server:
    """A live backend on an ephemeral loopback port."""

    def __init__(self, port: int, proc: subprocess.Popen, log_path: Path):
        self.port = port
        self.proc = proc
        self.log_path = log_path

    @property
    def base_url(self) -> str:
        """The server's root URL."""
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        """Stop the server and wait for it to be gone."""
        _stop_process(self.proc)

    def log_tail(self, limit: int = 4000) -> str:
        """The tail of the server's output — where its failures are written."""
        try:
            return self.log_path.read_text(
                encoding="utf-8", errors="replace"
            )[-limit:]
        except OSError:
            return "<server log unavailable>"


def _child_bootstrap_dir(tmp_path: Path) -> Path:
    """The directory holding the child's ``sitecustomize.py``.

    The file is written on every call: it is the child's instrumentation, and
    a stale copy from an earlier case would count somebody else's calls.
    """
    bootstrap_dir = tmp_path / "child-bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "sitecustomize.py").write_text(
        _CHILD_SITECUSTOMIZE_SOURCE, encoding="utf-8"
    )
    return bootstrap_dir


def _verification_workspace_root(tmp_path: Path) -> Path:
    """The root the verification round materialises its workspaces under.

    One root per case: two cases that share a root would fight over the same
    worktree path, and the loser would be told ``409 workspace conflict``
    instead of walking the pipeline.
    """
    workspace_root = tmp_path / "verification-workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    return workspace_root


def _boot_coexist_server(
    tmp_path: Path,
    script_dir: Path,
    *,
    counter_path: Optional[Path] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[str, subprocess.Popen]:
    """Boot a real backend for ``script_dir``; return ``(base_url, proc)``.

    This is the module's **produced interface**: the companion file imports it
    when it needs a backend of its own, so "how a backend is started here"
    lives in exactly one place instead of being copied per case.

    The caller owns the process: ``_stop_process(proc)`` in a ``finally`` is
    what shuts it down (terminate, then wait — the wait is what frees the
    port again).

    Args:
        tmp_path: The case's scratch directory.  The child's bootstrap
            directory, the verification workspace root and the server log all
            live under it.
        script_dir: The scripted model's fixture directory.
        counter_path: Where the child appends its model-call records.
            Defaults to ``<tmp_path>/llm-calls.jsonl`` — the same path the
            :func:`counter_file` fixture uses.
        extra_env: Merged into the child's environment last, so a caller can
            steer the child without touching this process's environment.

    Returns:
        ``(base_url, proc)`` — the URL to talk to and the process to stop.
    """
    server = _spawn_server(
        tmp_path / "uvicorn.log",
        _child_bootstrap_dir(tmp_path),
        script_dir,
        counter_path or (tmp_path / "llm-calls.jsonl"),
        _verification_workspace_root(tmp_path),
        extra_env=extra_env,
    )
    return server.base_url, server.proc


def _spawn_server(
    log_path: Path,
    bootstrap_dir: Path,
    script_dir: Path,
    counter_path: Path,
    workspace_root: Path,
    extra_env: Optional[Dict[str, str]] = None,
) -> Server:
    """Spawn a real backend and wait until it answers ``/health``.

    The child inherits this process's environment — which already carries the
    redirected plan tree, the redirected state database and the redirected
    home — so the server and the test look at the **same** plan.

    ``extra_env`` is merged into the child's environment last, so a caller
    can steer the child without touching this process's own environment.
    """
    from coding_tool import LLM_MODE_ENV, LLM_MODE_SCRIPTED, LLM_SCRIPT_DIR_ENV

    uvicorn_command = _uvicorn_command()
    if uvicorn_command is None:
        raise RuntimeError(
            "uvicorn is not available for the interpreter under test "
            f"({sys.executable}); this end-to-end test needs a real backend "
            "process to exercise"
        )

    env = dict(os.environ)
    pythonpath = [str(bootstrap_dir), str(_BACKEND_DIR), str(_REPO_ROOT)]
    inherited = env.get("PYTHONPATH")
    if inherited:
        pythonpath.append(inherited)
    env.update(
        {
            "PDT_VERIFICATION_WORKSPACE_ROOT": str(workspace_root),
            "PDT_TEST_BACKEND_DIR": str(_BACKEND_DIR),
            LLM_MODE_ENV: LLM_MODE_SCRIPTED,
            LLM_SCRIPT_DIR_ENV: str(script_dir),
            COUNTER_ENV: str(counter_path),
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "PATH": _child_path(),
        }
    )
    if extra_env:
        env.update(extra_env)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    server_log = open(log_path, "w+b")  # noqa: SIM115
    proc: Optional[subprocess.Popen] = None
    port = _free_port()
    ready = False
    try:
        proc = subprocess.Popen(  # noqa: S603
            [
                *uvicorn_command,
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
        deadline = time.monotonic() + 60.0
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
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
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
            f"the backend on :{port} did not become healthy within 60s "
            f"(started with {uvicorn_command!r}). "
            f"Backend output:\n{tail}"
        )

    return Server(port=port, proc=proc, log_path=log_path)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _request(
    server: Any,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Tuple[int, Any]:
    """Send one request to the backend and return ``(status, body)``.

    The request carries the guard header every ``/api`` endpoint demands —
    without it the backend answers 403 before it looks at the route.

    ``server`` is either a :class:`Server` or the base URL of a backend this
    test booted itself (see :func:`_boot_coexist_server`).
    """
    base_url = server.base_url if isinstance(server, Server) else str(server)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            REQUEST_HEADER: REQUEST_HEADER_VALUE,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            return int(response.status), (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body}
        return int(exc.code), parsed


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class CoexistPlan:
    """The plan this flow runs on: a directory with **both** artifacts."""

    def __init__(
        self,
        plan_id: str,
        plan_dir: Path,
        doc: str,
        machine_plan: Dict[str, Any],
        repo_dir: Path,
        project_dir: Path,
    ):
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.doc = doc
        self.machine_plan = machine_plan
        self.repo_dir = repo_dir
        self.project_dir = project_dir

    @property
    def doc_path(self) -> Path:
        """Where the reviewed plan document lives."""
        return self.plan_dir / "verification-plan.md"

    @property
    def machine_plan_path(self) -> Path:
        """Where the machine plan (the translation) lives."""
        return self.plan_dir / "verification_plan.json"


@pytest.fixture
def coexist_plan(
    hermetic_env: Path,
    tmp_path: Path,
    plan_sqlite_seeder,
) -> CoexistPlan:
    """A plan directory that holds ``tasks.json`` **and** will hold a plan doc.

    The plan starts as a development-branch plan (task list only) — the plan
    document is produced by the flow itself, which is the point: the document
    is an **additional** artifact of a development plan, not a replacement of
    its task list.

    The baseline is recorded in the routing row (where the freeze step puts
    it), because the plan-document generator reads the baseline from the plan
    state and nowhere else.
    """
    upstream = build_upstream_repo(tmp_path / "upstream")
    plan_dir = hermetic_env / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The development branch's own artifact.  It stays there for the whole
    # flow: the plan document is added *next to* it, not instead of it.
    #
    # The task list is **not** empty on purpose: a plan with no tasks
    # short-circuits the verification round before a single verification
    # point is dispatched (the orchestrator's own guard), so an empty list
    # would make the round prove nothing about the machine plan.
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "T1",
                        "title": "让计划书与任务清单共存",
                        "files_to_modify": ["README.md"],
                        "status": "completed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    doc = build_plan_doc(upstream.repo, upstream.pr_number, upstream.pr_ref)
    baseline = {
        "repo_dir": str(upstream.repo),
        "ref": upstream.pr_ref,
        "commit_sha": upstream.head_sha(),
        "pr_number": upstream.pr_number,
    }

    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # The same rows production writes: the routing row carries the phase and
    # the baseline, the execution row carries the project directory the
    # verification round runs in.
    plan_sqlite_seeder(
        plan_dir,
        PLAN_ID,
        phase="executing",
        project_dir=str(project_dir),
        verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
            "baseline": baseline,
        },
    )

    return CoexistPlan(
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        doc=doc,
        machine_plan=build_machine_plan(doc),
        repo_dir=upstream.repo,
        project_dir=project_dir,
    )


@pytest.fixture
def scripted_scripts(tmp_path: Path, coexist_plan: CoexistPlan) -> Path:
    """The scripted model answers: the plan document, the rewrite, the plan.

    Three answers, three prompts:

    * the plan document (the generation prompt) — the reviewed document the
      whole flow is about;
    * the rewritten document (the refine prompt) — the same document: the
      flow asserts on the *number of calls*, not on the wording;
    * the machine plan (the translation prompt) — derived from the document,
      so the equivalence gate has something to agree with.

    The registry matches on a fragment of the prompt and never falls back:
    a call the registry does not know is an error, not a default answer.
    """
    script_dir = tmp_path / "llm-scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "prompts": {
            "verification_plan": "你要写一份「验证计划书」",
            "verification_plan_refine": "你要改写一份「验证计划书」",
            "verification_translate": "待补全的验证点骨架",
        }
    }
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )
    (script_dir / "verification_plan.txt").write_text(
        coexist_plan.doc, encoding="utf-8"
    )
    (script_dir / "verification_plan_refine.txt").write_text(
        coexist_plan.doc, encoding="utf-8"
    )
    (script_dir / "verification_translate.txt").write_text(
        json.dumps(coexist_plan.machine_plan, ensure_ascii=False),
        encoding="utf-8",
    )
    return script_dir


@pytest.fixture
def counter_file(tmp_path: Path) -> Path:
    """Where the child process appends its model-call records."""
    path = tmp_path / "llm-calls.jsonl"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def backend_factory(
    tmp_path: Path,
    scripted_scripts: Path,
    counter_file: Path,
    hermetic_env: Path,
):
    """Spawn a real backend, with extra environment for the child.

    The extra environment is applied to the **child's** environment at spawn
    time, so a caller can steer the child (the negative control's forced
    plan source, for instance) without touching this process's environment
    and without depending on fixture ordering.
    """

    def _spawn(extra_env: Optional[Dict[str, str]] = None) -> Server:
        return _spawn_server(
            tmp_path / "uvicorn.log",
            _child_bootstrap_dir(tmp_path),
            scripted_scripts,
            counter_file,
            _verification_workspace_root(tmp_path),
            extra_env=extra_env,
        )

    return _spawn


@pytest.fixture
def backend(backend_factory) -> Server:
    """A real backend process on an ephemeral port.

    The child's plan tree, state database and home are this process's — the
    ones the hermetic environment installed — so the server and the test look
    at the same plan.
    """
    server = backend_factory()
    try:
        yield server
    finally:
        server.stop()


def _read_records(counter_file: Path) -> List[Dict[str, Any]]:
    """The model-call records the child wrote."""
    if not counter_file.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in counter_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def _accept_every_item(server: Any, plan_id: str) -> List[Dict[str, Any]]:
    """Accept every review item, one request per item.

    Returns the items the review endpoint reported.
    """
    status, body = _request(server, "GET", f"/api/verification-plan/{plan_id}/review/items")
    assert status == 200, f"reading the review items failed: {status} {body}"
    items = body["items"]
    assert items, "the review surface reported no items to accept"
    doc_sha256 = body.get("doc_sha256")
    for item in items:
        status, body = _request(
            server,
            "POST",
            f"/api/verification-plan/{plan_id}/review/item/{item['index']}",
            {"action": "accept", "doc_sha256": doc_sha256},
        )
        assert status == 200, (
            f"accepting item {item['index']} failed: {status} {body}"
        )
    return items


def test_coexist_full_user_flow(
    backend: Server,
    coexist_plan: CoexistPlan,
    counter_file: Path,
) -> None:
    """生成 → 逐条审阅 → refine → 再审阅 → 启动验证轮 → 再启动一次。

    这是本文件的主用例，也是 PRD 验收③④里「完整用户流程由真实进程 E2E 钉住」
    那一条的载体。它断言的是**分桶后的调用次数**（生成 1、refine 1、
    翻译 1、Phase-1 0）以及机器计划确实落盘并被验证轮消费。
    """
    plan_id = coexist_plan.plan_id

    # --- 1. 生成 -------------------------------------------------------
    status, body = _request(
        backend, "POST", f"/api/verification-plan/{plan_id}/generate", {}
    )
    assert status == 200, f"generating the plan document failed: {status} {body}"
    assert coexist_plan.doc_path.exists(), (
        "the generate endpoint answered 200 but no plan document is on disk"
    )
    assert coexist_plan.doc_path.read_text(encoding="utf-8") == coexist_plan.doc

    # The development branch keeps its own phase: the plan document is an
    # additional artifact, not a detour.
    status, body = _request(backend, "GET", f"/api/plan/{plan_id}/state")
    assert status == 200, f"reading the plan state failed: {status} {body}"
    assert body.get("current_phase") == "executing", (
        "generating the plan document moved the development plan's phase: "
        f"{body.get('current_phase')!r}"
    )

    # --- 2. 逐条审阅 ---------------------------------------------------
    items = _accept_every_item(backend, plan_id)
    assert len(items) == CANONICAL_ITEM_COUNT, (
        f"the review surface reported {len(items)} items, the fixture's "
        f"document has {CANONICAL_ITEM_COUNT}"
    )
    status, body = _request(
        backend, "GET", f"/api/verification-plan/{plan_id}/review/items"
    )
    assert all(item["status"] == "accepted" for item in body["items"]), (
        f"not every item is accepted after the review: {body['items']}"
    )

    # --- 3. refine -----------------------------------------------------
    status, body = _request(
        backend,
        "POST",
        f"/api/verification-plan/{plan_id}/refine",
        {"feedback": "把验证范围收窄到这次改动本身。"},
    )
    assert status == 200, f"refining the plan document failed: {status} {body}"

    # The document bytes changed, so the review that covered the old bytes
    # does not cover the new ones.
    status, body = _request(
        backend, "GET", f"/api/verification-plan/{plan_id}/review/items"
    )
    assert all(item["status"] == "pending" for item in body["items"]), (
        "the rewrite did not send the review back to pending: "
        f"{body['items']}"
    )

    # --- 4. 再审阅 -----------------------------------------------------
    _accept_every_item(backend, plan_id)

    # --- 5. 启动验证轮 -------------------------------------------------
    assert not coexist_plan.machine_plan_path.exists(), (
        "the machine plan is on disk before the first start — the flow under "
        "test would be measuring nothing"
    )
    # The auto-verification loop is what keeps the durable state moving
    # after the round is over, so the plan leaves the routing phase it was
    # parked on and the second start reaches the plan-preparation step
    # instead of resuming a repair.
    status, body = _request(
        backend,
        "POST",
        f"/api/verification/{plan_id}/start",
        {"auto_fix": True},
    )
    assert status == 200, f"starting the verification round failed: {status} {body}"

    # The round is running now; wait for it to leave the running state so
    # the second start reaches the plan-preparation step instead of being
    # turned away by the "already running" guard.
    #
    # The wait comes **before** the machine-plan assertions on purpose: the
    # plan is translated in the request thread on the reviewed path, but a
    # plan with no reviewed document would have the worker produce it
    # instead.  Checking before the round is over would then measure a
    # race, not a difference.
    #
    # This wait asks for the **durable verdict** rather than for the
    # round's report: the loop is still running rounds, and a report on
    # disk only says that one round stopped.
    _wait_for_the_round_to_settle(backend, plan_id, require_durable_verdict=True)

    assert coexist_plan.machine_plan_path.exists(), (
        "the machine plan did not land: the reviewed plan document was not "
        "translated into a consumable plan"
    )
    landed = json.loads(coexist_plan.machine_plan_path.read_text(encoding="utf-8"))
    assert landed == coexist_plan.machine_plan, (
        "the machine plan on disk is not the translation of the reviewed "
        "document"
    )

    # The machine plan is not just on disk — the round **consumes** it.  The
    # round's report carries one result per verification point it dispatched,
    # and those are the points the machine plan carries.
    report_path = coexist_plan.plan_dir / "verification_report.json"
    assert report_path.exists(), (
        "the round wrote no report: it dispatched nothing, so it consumed "
        "nothing"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    reported = [result.get("id") for result in report.get("verification_results") or []]
    expected = [
        point.get("id")
        for point in (landed.get("verification_points") or landed.get("points") or [])
    ]
    assert reported == expected, (
        "the verification round did not consume the machine plan that landed: "
        f"{reported} != {expected}. Report: "
        f"{json.dumps(report.get('verification_results'), ensure_ascii=False)[:2000]}"
    )

    # --- 6. 第二次启动 -------------------------------------------------
    before = count_buckets(_read_records(counter_file))
    status, body = _start_again(
        backend,
        plan_id,
        {"auto_fix": True},
        require_durable_verdict=True,
    )
    after = count_buckets(_read_records(counter_file))
    # A 200 is not enough: the endpoint also answers 200 when it resumes a
    # repair instead of preparing a plan ("mode": "repair_resume").  That
    # branch prepares no plan, so the bucket assertions below would measure
    # nothing at all.  Say so out loud instead of passing vacuously.
    _assert_plan_preparation(status, body)
    assert after.get("translate", 0) - before.get("translate", 0) == 0, (
        "the second start translated the plan document again: the machine "
        "plan on disk was not reused. Counts: "
        f"{before} -> {after}"
    )

    # --- 分桶断言 -----------------------------------------------------
    counts = count_buckets(_read_records(counter_file))
    assert counts.get("generate", 0) == 1, f"the plan document was not generated once: {counts}"
    assert counts.get("refine", 0) == 1, f"the plan document was not refined once: {counts}"
    assert counts.get("translate", 0) == 1, f"the plan document was translated once: {counts}"
    assert counts.get("phase1", 0) == 0, (
        "the Phase-1 generation ran on a plan whose document was already "
        f"reviewed: {counts}"
    )

    # --- 对照组：计数器是活的 ------------------------------------------
    # 「Phase-1 是 0」只有在一个**看得见调用**的计数器面前才有意义。同一份
    # 记录里另外三桶都是非零 —— 这就是那个对照组。
    assert counts.get("generate", 0) + counts.get("refine", 0) + counts.get(
        "translate", 0
    ) == 3, (
        "the counter recorded nothing but zeros, so the zero above is not a "
        f"measurement: {counts}"
    )


def test_second_start_is_idempotent(
    tmp_path: Path,
    coexist_plan: CoexistPlan,
    scripted_scripts: Path,
    counter_file: Path,
) -> None:
    """第二次启动是幂等的：盘上的机器计划原样留着，没有重新翻译。

    主用例数的是**调用次数**（翻译桶：第一次 1、第二次 0）。这条用例数的是
    **产物**：盘上那份机器计划的 sha256 在第二次启动前后必须一字不差。

    **为什么还要换掉 fixture**
    「没问模型」和「问了模型、答案碰巧一样」光看 sha256 分不开 —— 两者都可能
    留下同一份字节。所以第二次启动**之前**把翻译用的 fixture 换成**不等价**的
    内容：一旦第二次启动真的去翻译，这份内容要么让等价性门禁拒掉它（判据编号
    对不上 → ``422 verification_plan_equivalence_failed``），要么让翻译自己
    抛错（验证点集合漂移）—— 两条路都不是 200。

    于是「第二次启动仍然返回 200」本身就把「它没有问模型」钉住了（问了就会拿
    到那份不等价的内容，就会被拒），而 sha256 不变把「盘上那份没有被改写」也
    钉住了。翻译桶不再增长是第三个、也是最直接的那个证据：模型一次都没被问。

    这个后端是自己起的（:func:`_boot_coexist_server`），所以它的收尾也在这条
    用例手里：``finally`` 里 terminate + wait —— 端口要等到进程真的走了才
    会被释放。
    """
    plan_id = coexist_plan.plan_id

    base_url, proc = _boot_coexist_server(
        tmp_path, scripted_scripts, counter_path=counter_file
    )
    try:
        # --- 1. 生成 + 逐条审阅：来源判定落在 REVIEWED 上 ----------------
        status, body = _request(
            base_url, "POST", f"/api/verification-plan/{plan_id}/generate", {}
        )
        assert status == 200, f"generating the plan document failed: {status} {body}"
        _accept_every_item(base_url, plan_id)

        # --- 2. 第一轮启动：机器计划落盘 --------------------------------
        status, body = _request(
            base_url,
            "POST",
            f"/api/verification/{plan_id}/start",
            {"auto_fix": True},
        )
        assert status == 200, f"starting the verification round failed: {status} {body}"

        _wait_for_the_round_to_settle(base_url, plan_id, require_durable_verdict=True)

        assert coexist_plan.machine_plan_path.exists(), (
            "the machine plan did not land: there is nothing for the second "
            "start to reuse"
        )
        before_sha = hashlib.sha256(
            coexist_plan.machine_plan_path.read_bytes()
        ).hexdigest()

        # --- 3. 把翻译用的 fixture 换成不等价的内容 ----------------------
        #
        # 这份内容与计划书**不等价**：它声称验一条计划书里没有声明过的判据
        # （C9）。翻译阶段只校验验证点集合与身份，不校验判据编号，所以它能过
        # 翻译那一关 —— 挡住它的是等价性门禁，也就是「第二次启动若真的翻译了，
        # 会拿到 422」这件事的依据。
        drifted = json.loads(json.dumps(coexist_plan.machine_plan))
        drifted["verification_points"][0]["assertions"] = [
            {"name": "C9: 计划书里没有的判据"}
        ]
        (scripted_scripts / "verification_translate.txt").write_text(
            json.dumps(drifted, ensure_ascii=False), encoding="utf-8"
        )

        before_counts = count_buckets(_read_records(counter_file))

        # --- 4. 第二轮启动 ----------------------------------------------
        status, body = _start_again(
            base_url,
            plan_id,
            {"auto_fix": True},
            require_durable_verdict=True,
        )
        assert status == 200, (
            "the second start did not answer 200 although the machine plan on "
            "disk was reusable — a start that re-translated would have "
            f"consulted the drifted fixture and been refused: {status} {body}"
        )
        _assert_plan_preparation(status, body)

        # --- 5. 盘上那份机器计划一字未改 --------------------------------
        after_sha = hashlib.sha256(
            coexist_plan.machine_plan_path.read_bytes()
        ).hexdigest()
        assert after_sha == before_sha, (
            "the second start rewrote the machine plan: the plan document was "
            f"translated again. sha256 {before_sha} -> {after_sha}"
        )

        # --- 6. 模型一次都没被问 ----------------------------------------
        after_counts = count_buckets(_read_records(counter_file))
        assert after_counts.get("translate", 0) - before_counts.get(
            "translate", 0
        ) == 0, (
            "the second start translated the plan document again: the machine "
            f"plan on disk was not reused. Counts: {before_counts} -> {after_counts}"
        )
    finally:
        _stop_process(proc)

    # --- 收尾的三件事 --------------------------------------------------
    #
    # 这一段不是凑数：它是「finally 中 terminate + wait(timeout)、端口释放后
    # 可重绑、pgrep 无残留」这三条边界在这条用例里的落点。
    #
    # ``wait()`` 返回了，所以那个进程真的走了 —— ``poll()`` 不是 ``None``
    # 就是这句话的证据；端口能立刻被重新 bind，说明没有进程还占着那个监听
    # 套接字（探针自己开 ``SO_REUSEADDR``：被关掉的连接会留 TIME_WAIT，那不
    # 是「端口被占」）；``pgrep`` 那一下查的是**命令行**里还留着这个后端的
    # 进程。模式里带的是**这个端口的字面量**（不是一个字符类）：这台机器上
    # 还跑着别的后端，用字符类会把它们一并算成残留。
    assert proc.poll() is not None, (
        "the backend process is still alive after the finally block: the "
        "port would not be free and the pid would still be around"
    )

    port = int(base_url.rsplit(":", 1)[1])
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()

    pgrep = shutil.which("pgrep")
    if pgrep is not None:
        residue = subprocess.run(  # noqa: S603
            [
                pgrep,
                "-f",
                f"server:app --host 127.0.0.1 --port {port} ",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert residue.returncode != 0, (
            "a process is still carrying this backend's command line after "
            f"the finally block: {residue.stdout.strip()}"
        )


def test_the_counter_sees_the_calls_it_is_counting(counter_file: Path) -> None:
    """The counter file is what the buckets are read from — and it is read.

    Without this, a counter that never fires would make every bucket zero and
    the main case's assertions would pass without measuring anything.
    """
    counter_file.write_text(
        json.dumps({"system": "你是一位资深测试架构师。", "prompt": "..."})
        + "\n"
        + json.dumps(
            {"system": "", "prompt": "你要写一份「验证计划书」，它既是人工审核的对象。"}
        )
        + "\n",
        encoding="utf-8",
    )
    records = _read_records(counter_file)
    counts = count_buckets(records)
    assert counts == {"phase1": 1, "generate": 1}, (
        f"the bucket classification does not see the calls it is counting: {counts}"
    )


def _stub_status(
    monkeypatch: pytest.MonkeyPatch,
    payloads: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Answer status reads from ``payloads``; record every request made.

    The last payload is repeated once the list runs out, so a case can say
    "this is what the backend says" without caring how often it is asked.
    """
    asked: List[Dict[str, Any]] = []

    def fake_request(
        server: Any,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Tuple[int, Any]:
        asked.append({"method": method, "path": path})
        index = min(len(asked) - 1, len(payloads) - 1)
        return 200, payloads[index]

    monkeypatch.setattr(sys.modules[__name__], "_request", fake_request)
    return asked


def test_a_repairing_phase_is_not_the_end_of_the_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``verification_repairing`` is written **mid round** as well.

    A verification point that hits its hard timeout is auto-split, and
    the agent parks the plan on that phase while the round keeps
    running.  A wait that reads the phase alone calls the round over
    there — and the request that follows it races the round it is
    supposed to follow.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    asked = _stub_status(
        monkeypatch,
        [
            {
                "verification_status": "running",
                "current_phase": "verification_repairing",
                "verification_round": 1,
            },
            {
                "verification_status": "running",
                "current_phase": "verification_repairing",
                "verification_round": 1,
            },
            {
                "verification_status": "failed",
                "current_phase": "executing",
                "verification_round": 1,
            },
        ],
    )

    _wait_for_the_round_to_settle(object(), "some-plan")

    assert len(asked) == 3, (
        "the wait called the round over while it was parked on "
        f"``verification_repairing``: {asked}"
    )


def test_an_unreadable_status_is_not_the_end_of_the_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty signal means "not read", not "finished".

    A status payload that has no verdict in it yet says nothing about
    the round, and a wait that reads it as "over" turns the next request
    into a race.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    asked = _stub_status(
        monkeypatch,
        [
            {
                "verification_status": "",
                "current_phase": "executing",
                "verification_round": 1,
            },
            {
                "verification_status": "failed",
                "current_phase": "",
                "verification_round": 1,
            },
            {
                "verification_status": "failed",
                "current_phase": "executing",
                "verification_round": 1,
            },
        ],
    )

    _wait_for_the_round_to_settle(object(), "some-plan")

    assert len(asked) == 3, (
        "the wait called the round over on a status it could not read: "
        f"{asked}"
    )


def test_a_round_that_wrote_its_report_has_settled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A durable status that went stale must not hold the wait for ever.

    The worker records the round's verdict *after* the round has written
    its report, so a worker that dies in between leaves the status
    saying "running" for ever.  The report on disk is the round's own
    statement that it finished judging.
    """
    plans = tmp_path / "plans"
    plan_dir = plans / "some-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "verification_report.json").write_text(
        json.dumps({"_metadata": {"current_round": 2}}), encoding="utf-8"
    )
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans))
    asked = _stub_status(
        monkeypatch,
        [
            {
                "verification_status": "running",
                "current_phase": "verification_repairing",
                "verification_round": 2,
            }
        ],
    )

    _wait_for_the_round_to_settle(object(), "some-plan")

    assert len(asked) == 1, (
        "the wait kept waiting although the round had written its report: "
        f"{asked}"
    )


def test_the_report_of_an_earlier_round_does_not_settle_this_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A report left over from an earlier round is not this round's verdict."""
    plans = tmp_path / "plans"
    plan_dir = plans / "some-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "verification_report.json").write_text(
        json.dumps({"_metadata": {"current_round": 1}}), encoding="utf-8"
    )
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans))

    assert not _current_round_report("some-plan", 2), (
        "the report of round 1 was taken for the verdict of round 2"
    )
    assert _current_round_report("some-plan", 1), (
        "the report of round 1 was not recognised as round 1's own verdict"
    )


def test_the_backend_is_started_by_the_interpreter_under_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ambient ``uvicorn`` does not get to run this test's backend.

    The case asserts about the environment it runs in, so the backend it
    spawns has to come from that environment — the ``uvicorn`` beside the
    interpreter, or the interpreter itself.  A ``uvicorn`` found on the
    ``PATH`` is some other install, and picking that one first is how
    this test used to depend on whatever the operator happened to have
    installed.
    """
    poison = tmp_path / "uvicorn"
    poison.write_text("#!/bin/sh\nexit 66\n", encoding="utf-8")
    poison.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    command = _uvicorn_command()

    assert command is not None, (
        "no uvicorn for the interpreter under test — the end-to-end case "
        "cannot run at all"
    )
    assert str(poison) not in command, (
        f"the backend would be started by the ambient uvicorn: {command}"
    )
    beside = Path(sys.executable).parent / "uvicorn"
    expected = [str(beside)] if beside.exists() else [sys.executable, "-m", "uvicorn"]
    assert command == expected, (
        "the backend is not started by the interpreter under test: "
        f"{command} != {expected}"
    )


def test_the_second_start_has_to_reach_the_plan_preparation_step() -> None:
    """The second start is only allowed to pass when it prepared a plan.

    The endpoint answers ``200`` in two different ways: it either enters
    the plan-preparation step, or it resumes a repair
    (``"mode": "repair_resume"``) — and the repair branch prepares no
    plan, so the bucket assertions that follow would measure nothing.
    A ``409`` means the request never got that far at all.
    """
    _assert_plan_preparation(
        200,
        {
            "plan_id": "some-plan",
            "status": "started",
            "project_dir": "/somewhere",
            "round": 2,
            "resumed": False,
        },
    )

    with pytest.raises(AssertionError):
        _assert_plan_preparation(
            200, {"status": "started", "mode": "repair_resume", "round": 1}
        )

    with pytest.raises(AssertionError):
        _assert_plan_preparation(409, {"error": "Already running"})


def test_a_409_is_not_yet_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A backend that still owns the plan is waited out, not failed.

    The "already running" guard reads the backend's **in-memory** state,
    which can disagree with the durable state the wait reads for a
    moment.  A 409 is therefore a "not yet": the start is tried again
    once the round has settled.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    asked: List[Dict[str, Any]] = []

    def fake_request(
        server: Any,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Tuple[int, Any]:
        asked.append({"method": method, "path": path})
        if path.endswith("/status"):
            return 200, {
                "verification_status": "failed",
                "current_phase": "executing",
                "verification_round": 1,
            }
        if len([a for a in asked if a["method"] == "POST"]) < 3:
            return 409, {"error": "Already running"}
        return 200, {"status": "started"}

    monkeypatch.setattr(sys.modules[__name__], "_request", fake_request)

    status, body = _start_again(object(), "some-plan", {"auto_fix": False})

    assert status == 200, (
        f"the start was not retried while the backend owned the plan: "
        f"{status} {body}"
    )
    starts = [a for a in asked if a["method"] == "POST"]
    assert len(starts) == 3, f"the start was not tried again: {asked}"


#: The vocabulary of "this round is still in flight".
#:
#: ``verification_repairing`` is in it because the phase is written **mid
#: round** as well: a verification point that hits its hard timeout is
#: auto-split, and the agent parks the plan on that phase while the round
#: keeps running.  A round parked there is not finished.
#:
#: An **empty** signal is deliberately not in this vocabulary: an empty
#: string means "not read", not "finished".
_IN_FLIGHT_VERIFICATION_STATES = frozenset(
    {
        "running",
        "verification_running",
        "verification_repairing",
        "pending",
        "not_started",
        "queued",
    }
)


def _assert_plan_preparation(status: int, body: Any) -> None:
    """The second start has to reach the plan-preparation step.

    The endpoint answers ``200`` in two different ways: it either enters
    the plan-preparation step, or it resumes a repair
    (``"mode": "repair_resume"``).  The repair branch prepares no plan,
    so every bucket assertion that follows it would measure nothing —
    and a ``409`` means the request never got that far at all.
    """
    assert status == 200, (
        "the second start did not go through: a 409 here would mean the "
        "request never reached the plan-preparation step, and the "
        "zero-translation assertion below would be vacuous. "
        f"{status} {body}"
    )
    mode = body.get("mode") if isinstance(body, dict) else None
    assert mode != "repair_resume", (
        "the second start resumed a repair instead of reaching the "
        "plan-preparation step, so the bucket assertions below would be "
        f"vacuous: {body}"
    )


def _current_round_report(plan_id: str, round_number: Any) -> bool:
    """Has the round that is running now already written its report?

    The durable status can go **stale**: the worker records the round's
    verdict *after* the round has written its report, so a worker that
    dies in between leaves the status saying "running" for ever — and a
    wait that only reads that status would never return.  The report on
    disk is the round's own statement that it finished judging, and it
    names the round it belongs to, so a report left over from an earlier
    round does not pass for the current one.

    The plans root comes from the environment (``PDT_PLANS_DIR``) — the
    same override the backend honours — so this follows the hermetic plan
    tree instead of guessing where it is.
    """
    root = os.environ.get("PDT_PLANS_DIR")
    if not root:
        return False
    try:
        current_round = int(round_number)
    except (TypeError, ValueError):
        return False
    report_path = Path(root) / plan_id / "verification_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    metadata = report.get("_metadata") or {}
    try:
        reported_round = int(metadata.get("current_round"))
    except (TypeError, ValueError):
        return False
    return reported_round == current_round


def _wait_for_the_round_to_settle(
    server: Any,
    plan_id: str,
    *,
    require_durable_verdict: bool = False,
    timeout: float = 120.0,
) -> None:
    """Wait until the verification round has left the in-flight vocabulary.

    The second ``/start`` has to reach the plan-preparation step; while a
    round is in flight the backend's "already running" guard turns the
    request away before that, and the zero-translation assertion would
    then measure nothing.

    Two durable signals say where the round is: the round's own state
    (``verification_status``) and the plan's routing phase
    (``current_phase``).  **Both** of them have to leave the in-flight
    vocabulary before the round counts as over — one signal leaving is not
    enough, and an empty one is not a leaving at all:

    * ``verification_repairing`` is in the vocabulary because the phase is
      written mid round too (the hard-timeout auto-split), so a round
      parked on it may still be running.
    * an empty signal is "not read", not "finished": a payload that
      cannot be read yet says nothing about the round.

    The one thing that settles a round whose durable status went stale is
    the round's own report — see :func:`_current_round_report`.

    ``require_durable_verdict`` refuses that shortcut.  A plan that runs
    the auto-verification loop keeps moving its durable state on its own,
    so such a caller can afford to wait for the verdict itself instead of
    taking the report of a round that is still being followed by another
    one.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        status, body = _request(
            server, "GET", f"/api/verification/{plan_id}/status"
        )
        if status != 200:
            last = body
            time.sleep(0.5)
            continue
        last = body
        round_state = str(body.get("verification_status") or "").strip().lower()
        phase = str(body.get("current_phase") or "").strip().lower()
        if not round_state or not phase:
            # Not readable is not "finished": keep waiting.
            time.sleep(0.25)
            continue
        if (
            round_state in _IN_FLIGHT_VERIFICATION_STATES
            or phase in _IN_FLIGHT_VERIFICATION_STATES
        ):
            # At least one signal still says "in flight".  The round may
            # still be running — unless it has already written the report
            # of *this* round, which only a finished round does.
            if not require_durable_verdict and _current_round_report(
                plan_id, body.get("verification_round")
            ):
                return
            time.sleep(0.25)
            continue
        return
    raise AssertionError(
        "the verification round never left the running state, so the second "
        f"start could not be observed: {last}"
    )


def _start_again(
    server: Any,
    plan_id: str,
    payload: Dict[str, Any],
    *,
    attempts: int = 5,
    budget: float = 30.0,
    require_durable_verdict: bool = False,
) -> Tuple[int, Any]:
    """Start the plan again, waiting out a backend that still owns it.

    The backend answers ``409 Already running`` while its **own** in-memory
    state says a round owns the plan — and that state is not the durable
    state the wait above reads, so the two can disagree for a moment
    (the durable row is written before the in-memory one is released).  A
    409 is therefore a "not yet", not a verdict: wait for the round to
    settle again, sleep a moment, and try once more.

    Both the number of attempts and the wall-clock budget are bounded, so
    a plan that really is still owned fails the test instead of hanging.
    The re-wait runs inside the same budget, so the whole retry stays
    bounded too.
    """
    deadline = time.monotonic() + budget
    status, body = _request(
        server, "POST", f"/api/verification/{plan_id}/start", payload
    )
    for _ in range(attempts - 1):
        if status != 409:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            break
        _wait_for_the_round_to_settle(
            server,
            plan_id,
            require_durable_verdict=require_durable_verdict,
            timeout=remaining,
        )
        time.sleep(1.0)
        status, body = _request(
            server, "POST", f"/api/verification/{plan_id}/start", payload
        )
    return status, body
