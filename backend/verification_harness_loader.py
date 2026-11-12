"""这次判定用哪两份 harness 模块 —— 以及它们是从哪来的（2026-11-08）。

为什么有这个模块
----------------
2026-11-08 的 VP-015 连续两轮失败，两轮都卡在 ``elapsed=900.0 /
threshold=900.0``（计划给这条 VP 声明的是 ``timeout_seconds=3600``），而
工作树里那份 ``verification_subagent.py`` 早就修好了 —— 它认那条声明
（``_declared_timeout_seconds``），预算真的给到 3600；它也会在宽限期内读回
``vp_artifacts/<vp_id>/checkpoints.json``（``_await_stuck_artifact`` →
``_verdict_from_checkpoints``），产物在就在宽限期内把判决重建出来。

差别只在**谁在判**：

* 验证轮由 harness（主检出）里的常驻进程执行（AC server 常年不重启）；
* 它通过 ``sys.path`` 解析 ``verification_subagent`` /
  ``verification_executor``，拿到的是**主检出里那份没修过的**文件；
* 于是修复停在分支上、判定跑的是旧代码 —— 而旧代码把框架自己的超时
  归档成了交付物缺陷（"no test output captured ... cannot summarize"）。

这条教训仓库里已经写过一次（``verification_ci_runner_loader.py`` 开头
那段）：CI runner 那条腿早就是用「按路径加载被测树那份 + 每轮重载 + 出处
写进证据」修好的。本模块把同一套纪律推广到**决定 e2e 腿的那两个模块**：

* ``verification_subagent`` —— 预算（``wall_clock_cap``）与超时收尾
  （产物回收 / attempt 终止）；
* ``verification_executor`` —— 派发集（``_rebuild_dispatch_set``）与轮次
  作用域（复用判据）。

解析规则
--------
1. 候选路径 ``<project_dir>/backend/verification_subagent.py`` 与
   ``<project_dir>/backend/verification_executor.py``；
2. ``<project_dir>/backend/pyproject.toml`` 存在 —— 这个文件说明被测树
   **就是框架仓的检出** —— 且两个候选都在，于是被测树那两份**能用**；
3. 用 ``importlib.util.spec_from_file_location`` 以摘要命名的模块名加载
   （沿用 ``verification_ci_runner_loader._load_module_from_path`` 的纪律：
   摘要命名 + ``sys.modules`` 登记 + 执行后摘除）；
4. 加载失败 / 不是框架检出 / 候选缺失 → 回落 harness 自己那一份，并把
   ``fallback_reason`` 写进出处。

两个模块**各自独立判定**
------------------------
这两个模块不是一份代码的两个名字，而是两条腿的两端：方法表
（``SUPPORTED_METHODS`` / ``LEGACY_METHODS``）住在 ``verification_subagent``
里，执行器住在 ``verification_executor`` 里。所以**一份坏掉不該拖累另一份**：
subagent 可以来自被测树而 executor 回落（或者反过来），出处分别记录。

这与 ``is_framework_checkout`` 的"整体"答案并不矛盾：

* ``is_framework_checkout(project_dir)`` 回答的是**整棵树**的问题 ——
  它是不是框架仓的检出，**且**两个候选模块都在；
* ``load_harness_modules`` 回答的是**每个模块**的问题 —— 这一份能不能用。
  树里只有一份候选时，那一份仍按被测树那份判（它就是被测树的模块），
  缺的那一份单独回落，并说清是"候选缺失"。

只有一件事是**整体**的：那棵树根本不是框架仓的检出（没有
``backend/pyproject.toml``）——那时两份都回落。

每轮重新加载
------------
被测树那两份是**每次调用新建**的。常驻进程（AC server 常年不重启）因此不会
拿着一份陈旧的模块判定一整轮 —— 这正是「改磁盘不改内存」那个坑的解法：
模块不是被缓存的，是每轮从磁盘新建的。

harness 自己那两份是**回落**
----------------------------
走普通 import：它们是框架自己的代码，由框架自己的进程 import 着；同时是
框架**替换实现的注入点**（框架的测试就是替换
``verification_agent.VerificationSubAgent``）。所以回落走普通 import，被替换过
的那份就是该跑的那份 —— 从磁盘重载它反而会把那次替换悄悄丢掉，那是同一个
坑的更安静的版本。

平铺 import 的约定
------------------
被测树那两份模块的平铺 import（``from base_executor import ...``、
``from utils.atomic_io import ...``）解析到**harness 的副本**，不是被测树的
副本 —— 与 ``verification_ci_runner_loader`` 写明的约定一致；写在这里是为了
让它不悄悄变成隐式契约。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:  # the flat-import convention: ``backend/`` sits on ``sys.path``
    from verification_ci_runner_loader import file_sha256, git_head_at
except ImportError:  # pragma: no cover - the package-relative layout
    from backend.verification_ci_runner_loader import file_sha256, git_head_at

logger = logging.getLogger(__name__)


#: The module came from the tree under test.
RESOLUTION_PROJECT = "project"
#: The module came from the harness (this framework's own copy).
RESOLUTION_HARNESS = "harness"

#: The module that owns the e2e leg's budget and its timeout handling.
SUBAGENT_MODULE = "verification_subagent"
#: The module that owns the dispatch set and the round scope.
EXECUTOR_MODULE = "verification_executor"

#: The two modules this loader resolves — the ones that decide a round.
_HARNESS_MODULE_NAMES = (SUBAGENT_MODULE, EXECUTOR_MODULE)

#: The keys the two modules carry in the provenance.
_PROVENANCE_KEYS = ("subagent", "executor")


@dataclass
class LoadedHarness:
    """The harness modules that judge this round, and where they came from."""

    subagent_module: Any
    executor_module: Any
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def from_project(self) -> bool:
        """True when **both** modules come from the tree under test.

        A mixed verdict (one from the tree, one from the harness) is not
        "from the tree" — the per-module provenance says which is which.
        """
        return all(
            self.provenance.get(key, {}).get("resolution") == RESOLUTION_PROJECT
            for key in _PROVENANCE_KEYS
        )

    @property
    def subagent_class(self) -> Any:
        """The ``VerificationSubAgent`` class of the module in use."""
        return getattr(self.subagent_module, "VerificationSubAgent", None)

    @property
    def executor_class(self) -> Any:
        """The ``VerificationExecutor`` class of the module in use."""
        return getattr(self.executor_module, "VerificationExecutor", None)

    @property
    def supported_methods(self) -> Tuple[str, ...]:
        """The method table of the module in use (never a second copy)."""
        return tuple(getattr(self.subagent_module, "SUPPORTED_METHODS", ()) or ())

    @property
    def legacy_methods(self) -> Tuple[str, ...]:
        """The retired-method table of the module in use."""
        return tuple(getattr(self.subagent_module, "LEGACY_METHODS", ()) or ())


# ---------------------------------------------------------------------------
# Where the two candidates live
# ---------------------------------------------------------------------------


def harness_backend_dir() -> Path:
    """The ``backend/`` directory this loader was loaded from."""
    return Path(__file__).resolve().parent


def harness_module_path(module_name: str) -> Path:
    """The harness' own ``backend/<module_name>.py``."""
    return harness_backend_dir() / f"{module_name}.py"


def project_backend_dir(project_dir: Any) -> Optional[Path]:
    """The tree under test's ``backend/`` directory (``None`` when unknown)."""
    if not project_dir:
        return None
    try:
        return Path(project_dir) / "backend"
    except TypeError:
        return None


def project_module_path(project_dir: Any, module_name: str) -> Optional[Path]:
    """The tree under test's ``backend/<module_name>.py`` (``None`` if absent)."""
    backend = project_backend_dir(project_dir)
    if backend is None:
        return None
    try:
        candidate = backend / f"{module_name}.py"
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _is_framework_checkout_for(project_dir: Any, module_name: str) -> bool:
    """Is the tree under test a checkout of **this** framework — for one module?

    Two files have to say yes: ``backend/pyproject.toml`` (that is what says
    "this tree is the framework") and the candidate module itself.
    """
    backend = project_backend_dir(project_dir)
    if backend is None:
        return False
    try:
        if not (backend / "pyproject.toml").is_file():
            return False
    except OSError:
        return False
    return project_module_path(project_dir, module_name) is not None


def is_framework_checkout(project_dir: Any) -> bool:
    """Is the tree under test a checkout of **this** framework?

    Three files have to say yes: ``backend/pyproject.toml`` (it says this
    tree **is** the framework), ``backend/verification_subagent.py`` and
    ``backend/verification_executor.py`` (these two are the modules this
    loader resolves).  One missing -> not a framework checkout.

    This is the **whole-tree** answer.  The per-module answer lives in
    :func:`load_harness_modules`, which decides each module on its own —
    see the module docstring.
    """
    return all(
        _is_framework_checkout_for(project_dir, name)
        for name in _HARNESS_MODULE_NAMES
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_module_from_path(path: Path, kind: str) -> Any:
    """Load a module from its path, under a name of its own.

    The name carries a digest of the path, so two trees never collide in
    ``sys.modules``; the module is registered while it executes (so a
    module that looks itself up by ``__name__`` finds itself) and removed
    again afterwards, so the registry keeps describing the harness' own
    modules.

    The name is **not** the bare module name: registering
    ``verification_subagent`` would make the framework's next ordinary
    import pick up the tree under test's copy — exactly the pollution this
    loader exists to prevent.
    """
    backend_dir = str(harness_backend_dir())
    if backend_dir not in sys.path:
        # The flat-import convention: a project module does
        # ``from base_executor import ...``, and that name has to resolve
        # to the harness' copy.
        sys.path.append(backend_dir)
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    module_name = f"pdt_project_{kind}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"no loader for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _harness_module(module_name: str) -> Any:
    """The harness' own copy of ``module_name`` — the imported one.

    Deliberate: the harness' copy is the framework's own **injection
    point** (replacing ``VerificationSubAgent`` on it swaps the
    implementation), and it is loaded by the framework's own process.
    Reloading it from disk every round would be a second, quieter version
    of the very problem this module exists to fix.
    """
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    return importlib.import_module(module_name)


def _module_path_of(module: Any, module_name: str) -> Path:
    """The file a loaded module came from.

    The harness' own path is the answer unless the module says otherwise —
    a module imported from somewhere else is reported where it came from,
    because the provenance's whole job is to name the file that judged.
    """
    raw = getattr(module, "__file__", None)
    if raw:
        try:
            return Path(raw).resolve()
        except OSError:
            pass
    return harness_module_path(module_name)


# ---------------------------------------------------------------------------
# 出处 —— 说得清"谁在判"
# ---------------------------------------------------------------------------


def _git_head_of(path: Path) -> str:
    """The HEAD of the repository that owns ``path``.

    An empty answer is not an answer: when the HEAD cannot be read the
    provenance says so, and says why.
    """
    head = git_head_at(path)
    if head:
        return head
    return f"unknown: no git HEAD could be read for {path}"


def _module_provenance(
    resolution: str,
    module: Any,
    module_name: str,
    fallback_reason: str = "",
) -> Dict[str, Any]:
    """The provenance of one module — where it came from, and what it is.

    ``sha256`` and ``git_head`` describe the file that is **in use**: the
    tree under test's file when the resolution is ``project``, the
    harness' own file when the resolution is ``harness``.
    """
    path = _module_path_of(module, module_name)
    return {
        "resolution": resolution,
        "module_path": str(path),
        "sha256": file_sha256(path),
        "git_head": _git_head_of(path),
        "fallback_reason": fallback_reason,
    }


def _resolve_module(project_dir: Any, module_name: str) -> Tuple[Any, str]:
    """Decide for **one** module — independently of the other one.

    Returns ``(module, fallback_reason)``.  ``module`` is ``None`` when the
    tree under test's copy cannot be used; ``fallback_reason`` then names
    the rule that triggered the fallback — one of

    * ``not a framework checkout`` — the tree has no ``backend/pyproject.toml``;
    * ``candidate missing`` — the tree does not carry this module;
    * ``load failed`` — the module is there but cannot be executed
      (the reason carries the exception type and its message).
    """
    backend = project_backend_dir(project_dir)
    if backend is None:
        return None, "not a framework checkout: no backend directory"
    pyproject = backend / "pyproject.toml"
    try:
        framework = pyproject.is_file()
    except OSError:
        framework = False
    if not framework:
        return None, f"not a framework checkout: {pyproject} is missing"
    path = project_module_path(project_dir, module_name)
    if path is None:
        return None, f"candidate missing: {backend / (module_name + '.py')}"
    try:
        return _load_module_from_path(path, module_name), ""
    except Exception as exc:  # noqa: BLE001 — a fallback, never a failure
        logger.warning(
            "[harness_loader] could not load the tree under test's own %s "
            "(%s); falling back to the harness' copy: %s",
            module_name, path, exc,
        )
        return None, f"load failed: {type(exc).__name__}: {exc}"


def _harness_copy(module_name: str) -> Tuple[Any, str]:
    """The harness' own copy of one module — and why it may be unusable."""
    try:
        return _harness_module(module_name), ""
    except Exception as exc:  # noqa: BLE001 — a fallback, never a failure
        logger.error(
            "[harness_loader] the harness' own %s could not be imported: %s",
            module_name, exc,
        )
        return None, f"harness copy unusable: {type(exc).__name__}: {exc}"


def _combined_reason(subagent_reason: str, executor_reason: str) -> str:
    """One line saying why **anything** fell back."""
    if not subagent_reason:
        return executor_reason
    if not executor_reason:
        return subagent_reason
    if subagent_reason == executor_reason:
        return subagent_reason
    return f"subagent: {subagent_reason}; executor: {executor_reason}"


def load_harness_modules(project_dir: Any = None) -> LoadedHarness:
    """The modules that should judge ``project_dir`` — and where they came from.

    Never raises: a tree whose modules cannot be loaded falls back to the
    harness' own copies, with the reason in the provenance.  A round that
    cannot be judged is worse than one judged by the wrong file, but a
    round that **crashes** is worse than both.

    The two modules are decided **independently** (see the module
    docstring): one of them may come from the tree under test while the
    other one falls back.

    The top-level ``module_path`` / ``sha256`` / ``git_head`` describe the
    **subagent** —— the module that owns the e2e leg's budget and its
    timeout handling; ``provenance["executor"]`` carries the executor's own
    identity, and ``provenance["subagent"]`` repeats the subagent's, so a
    reader never has to guess which module a field describes.
    """
    resolved: Dict[str, Tuple[Any, str, str]] = {}
    for module_name in _HARNESS_MODULE_NAMES:
        module, reason = _resolve_module(project_dir, module_name)
        if module is None:
            harness_module, harness_reason = _harness_copy(module_name)
            resolved[module_name] = (
                harness_module, RESOLUTION_HARNESS, reason or harness_reason,
            )
        else:
            resolved[module_name] = (module, RESOLUTION_PROJECT, "")

    subagent, subagent_resolution, subagent_reason = resolved[SUBAGENT_MODULE]
    executor, executor_resolution, executor_reason = resolved[EXECUTOR_MODULE]

    subagent_provenance = _module_provenance(
        subagent_resolution, subagent, SUBAGENT_MODULE, subagent_reason,
    )
    executor_provenance = _module_provenance(
        executor_resolution, executor, EXECUTOR_MODULE, executor_reason,
    )

    provenance = dict(subagent_provenance)
    provenance["subagent"] = subagent_provenance
    provenance["executor"] = executor_provenance
    provenance["fallback_reason"] = _combined_reason(
        subagent_reason, executor_reason,
    )

    return LoadedHarness(
        subagent_module=subagent,
        executor_module=executor,
        provenance=provenance,
    )