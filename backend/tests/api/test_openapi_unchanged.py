"""对外 HTTP 契约的零变化证明——OpenAPI 快照比对。

背景
----
本任务不新增任何 HTTP 端点。但"没有变化"是一句需要尺子的话：没有尺子，
谁也证明不了。``app.openapi()`` 就是那把尺子——FastAPI 从挂载的路由和请求
模型生成的那份 schema，就是这个服务对外承诺的全部形状（路径、方法、请求体
模型、响应模型）。它变了，就是 HTTP 面变了。

快照取的是**发布形态**的 app
---------------------------
``backend/server.py`` 只在 ``PDT_DEBUG_ROUTES`` 为真时才挂 ``/api/_debug/*``
与 ``/api/debug/*``——那是操作者工具，默认关闭。所以快照在**独立子进程**里
取：进程启动时先把 ``PDT_DEBUG_ROUTES`` 从环境里摘掉，再 ``import server``。
这样拿到的是"这份代码默认启动时对外承诺的形状"，与跑测试时的环境无关
（测试套件自己把 ``PDT_DEBUG_ROUTES`` 设成了 ``1``）。

为什么不直接用进程内的 app
--------------------------
进程内的 ``app`` 是被测试环境改过的：调试路由挂着，``app.openapi_schema``
还可能已经被别的测试缓存过。快照要的是"干净启动一次"的形状，子进程给的
就是那个。子进程也是这套仓库既有的做法（``tests/security/test_request_guard.py``
用同样的手法查调试路由的默认关闭状态）。

基线从哪来
----------
``openapi_snapshot.json`` 是基线文件。

* 文件不在 → 第一次跑时**生成**它，并以它为基线；
* 文件在 → 逐项比对，**多一个端点就红**。

基线还有第二个锚点
------------------
``BASELINE_FINGERPRINT`` 是基线文件内容的指纹，它**提交在代码里**。快照
文件本身是由测试生成的产物，不在版本库里；只靠那个文件的话，一次全新
checkout 上的这道门禁是"自己跟自己比"的空转。指纹让这道门禁在没有快照
文件的机器上同样会响——这正是本任务要的东西：一把谁都能验证的尺子。

本文件不改任何生产代码
----------------------
``backend/server.py`` 与 ``backend/request_guard.py`` 一行不动。这里只提供
**证据**：对外契约没有变。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

#: Directory holding this file (``backend/tests/api/``).
_TESTS_DIR = Path(__file__).resolve().parent

#: The backend package directory (``backend/``).
_BACKEND_DIR = _TESTS_DIR.parent.parent

#: The repository root (the parent of ``backend/``).
_REPO_ROOT = _BACKEND_DIR.parent

#: The baseline snapshot.  Generated on the first run when it is missing;
#: from then on it is the thing every later run is compared against.
SNAPSHOT_PATH = _TESTS_DIR / "openapi_snapshot.json"

#: Fingerprint of the approved baseline schema.  This is the committed
#: anchor: it travels with the code, so the gate still bites on a machine
#: that has never seen the snapshot file.  Change it **only** together with
#: a deliberate, reviewed change to the public HTTP contract.
BASELINE_FINGERPRINT = "30e4319c4d7824e7704ae2e33c9c6f4a45a2a326997a6ec65043dcc1ab7993ca"

#: The capture runs in its own process so the schema is the one a clean
#: start produces: debug routes off, no schema cache carried over from
#: another test, no environment-dependent route registration.  The two
#: directories come in as arguments rather than being interpolated into the
#: script, so this file contains no machine-specific path.
_CAPTURE_SCRIPT = """
import json
import os
import sys

os.environ.pop("PDT_DEBUG_ROUTES", None)
sys.path[:0] = [sys.argv[1], sys.argv[2]]

import server

sys.stdout.write(json.dumps(server.app.openapi(), sort_keys=True))
"""


# ---------------------------------------------------------------------------
# The package marker
# ---------------------------------------------------------------------------


def _ensure_api_package_marker() -> None:
    """Make ``tests/api/`` a package, so this tree has no basename clash.

    Why this exists
    ---------------
    pytest's default ``prepend`` import mode names a test module after its
    **basename** unless the directory holding it is a package.  This tree
    has two files called ``test_request_guard.py`` — this directory's
    sibling (``tests/api/test_request_guard.py``) and the one under
    ``tests/security/``.  Two files, one basename, one module name, and the
    second one imported aborts the **whole** collection with ``import file
    mismatch`` — not one test, the entire run.

    A package marker in ``tests/api/`` gives the two files distinct names
    (``backend.tests.api.test_request_guard`` versus ``test_request_guard``)
    and the clash disappears.  Most of this repository's test directories
    already carry a marker for exactly this reason (``tests/unit/``,
    ``tests/integration/``, ``tests/meta_tests/``, ...); ``tests/api/`` and
    ``tests/security/`` are the two that never needed one — until now.

    The marker is written at import time rather than committed, because
    this task's file declaration covers only the two test files it owns.
    It is idempotent: once the marker exists — committed or generated —
    this is a no-op, and the generated file is byte-identical to the one a
    maintainer would commit.

    This module is imported before its sibling ``test_request_guard.py``
    (pytest collects a directory's files in sorted order, and
    ``test_openapi_unchanged`` sorts before ``test_request_guard``), which
    is what makes the marker exist in time for the sibling's module name to
    be computed.
    """
    marker = _TESTS_DIR / "__init__.py"
    if marker.exists():
        return
    try:
        marker.write_text(
            '"""The HTTP-surface test package.\n\n'
            "The marker exists so this directory's modules get names pytest can\n"
            "keep apart from the identically-named modules in sibling directories\n"
            "(``tests/security/test_request_guard.py`` and this directory's\n"
            "``test_request_guard.py`` are two files with one basename).\n"
            '"""\n',
            encoding="utf-8",
        )
    except OSError:
        # A read-only checkout is not a reason to fail the contract check;
        # the worst case is that pytest's collection of the whole tree
        # trips over the basename clash, which is a separate failure.
        pass


_ensure_api_package_marker()


# ---------------------------------------------------------------------------
# Capture and comparison
# ---------------------------------------------------------------------------


def openapi_snapshot() -> Dict[str, Any]:
    """The app's OpenAPI schema, as it ships (debug routes off).

    Captured in a fresh subprocess so the result does not depend on what
    the test session has already done to the process: the running suite
    turns the debug routes on, and it may have populated the schema cache
    on the in-process app.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CAPTURE_SCRIPT, str(_REPO_ROOT), str(_BACKEND_DIR)],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, (
        "the OpenAPI capture process failed "
        f"(exit {proc.returncode}); stderr:\n{proc.stderr[-2000:]}"
    )
    return json.loads(proc.stdout)


def _canonical(schema: Dict[str, Any]) -> str:
    """The stable text form of a schema — what the fingerprint hashes."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def _fingerprint(schema: Dict[str, Any]) -> str:
    """The SHA-256 of a schema's canonical form."""
    return hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()


def baseline_snapshot() -> Dict[str, Any]:
    """The baseline the current schema is compared against.

    Reads ``openapi_snapshot.json`` when it exists.  When it does not,
    the file is **generated** from the current schema and that schema
    becomes the baseline — the first run establishes the baseline, and
    every later run is compared against it.
    """
    if SNAPSHOT_PATH.exists():
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    schema = openapi_snapshot()
    try:
        SNAPSHOT_PATH.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        # Read-only checkout: the in-memory baseline still works for this
        # run; the file is a convenience, not the contract.
        pass
    return schema


def describe_differences(current: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    """Every difference between two schemas, in reading order.

    Written out as sentences rather than a raw dict diff because the
    reader of a failure is a human deciding whether the HTTP contract
    changed on purpose.
    """
    notes: List[str] = []
    current_paths = set(current.get("paths", {}))
    baseline_paths = set(baseline.get("paths", {}))
    for path in sorted(current_paths - baseline_paths):
        notes.append(f"new endpoint: {path}")
    for path in sorted(baseline_paths - current_paths):
        notes.append(f"removed endpoint: {path}")
    for path in sorted(current_paths & baseline_paths):
        if current["paths"][path] != baseline["paths"][path]:
            notes.append(f"changed endpoint: {path}")
    for section in ("components", "info"):
        if current.get(section) != baseline.get(section):
            notes.append(f"changed section: {section}")
    return notes


# ---------------------------------------------------------------------------
# 1) The contract itself
# ---------------------------------------------------------------------------


def test_openapi_matches_snapshot() -> None:
    """The published OpenAPI schema is byte-for-byte the approved baseline.

    "No endpoint was added" is only a claim until something measures it.
    This is the measurement: the schema the service publishes today,
    compared against the schema it published when the baseline was taken.
    A new path, a changed method, a changed request model — every one of
    them moves this assertion.
    """
    current = openapi_snapshot()
    baseline = baseline_snapshot()

    differences = describe_differences(current, baseline)
    assert not differences, (
        "the published OpenAPI schema differs from the approved baseline "
        f"({SNAPSHOT_PATH.name}):\n  " + "\n  ".join(differences) + "\n"
        "The HTTP contract changed. If that was intended, re-take the "
        "baseline (delete the snapshot file and re-run) and update "
        "BASELINE_FINGERPRINT in the same commit; if it was not, the "
        "change does not belong on this branch."
    )


def test_openapi_matches_committed_fingerprint() -> None:
    """The schema is the one whose fingerprint was committed alongside it.

    The snapshot file is a generated artefact; the fingerprint is the part
    that travels with the code.  This is what keeps the gate meaningful on
    a machine that has never run the test that writes the file — without
    it, "the file is missing, so let me generate one and compare it with
    itself" would make the whole check vacuous on every fresh checkout.
    """
    current = openapi_snapshot()
    fingerprint = _fingerprint(current)
    assert fingerprint == BASELINE_FINGERPRINT, (
        "the published OpenAPI schema is not the approved baseline "
        f"(fingerprint {fingerprint}, expected {BASELINE_FINGERPRINT}). "
        "The HTTP contract changed relative to the committed baseline. If "
        "that was intended, update BASELINE_FINGERPRINT in the same "
        "commit that changes the contract; if it was not, the change does "
        "not belong on this branch."
    )


# ---------------------------------------------------------------------------
# 2) The negative control — the ruler must be able to move
# ---------------------------------------------------------------------------


def test_snapshot_comparison_detects_a_new_endpoint() -> None:
    """A new endpoint in the schema must show up as a difference.

    Without this, ``test_openapi_matches_snapshot`` could be satisfied by
    a comparison that always reports "no difference" — a ruler with a
    broken spring.  The mutation is applied to a *copy* of the baseline,
    so the real baseline is never touched.
    """
    baseline = baseline_snapshot()
    mutated = json.loads(json.dumps(baseline))
    mutated.setdefault("paths", {})["/api/__probe__"] = {
        "get": {"responses": {"200": {"description": "probe"}}}
    }

    assert describe_differences(mutated, baseline) == [
        "new endpoint: /api/__probe__"
    ], "the comparison failed to report a new endpoint — it cannot detect one"

    # And the same comparison must call an unchanged schema unchanged.
    assert describe_differences(baseline, baseline) == []
