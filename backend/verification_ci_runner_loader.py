"""这次判定用哪份 ``verification_ci_runner`` —— 以及它是从哪来的（2026-11-05）。

为什么有这个模块
----------------
2026-11-05 的 VP-015 连续两轮失败，两轮的失败 detail **逐字节相同**
（``ci_entry 引用的路径不存在：backend/.venv)``），而工作树里那份
``verification_ci_runner.py`` 早就修好了 —— 用它跑同一个 ``ci_entry``，
结果是零 issue。差别只在**谁在判**：

* 验证轮由 harness（主检出）里的常驻进程执行（AC server 常年不重启）；
* 它通过 ``sys.path`` 解析 ``verification_ci_runner``，拿到的是**主检出
  里那份没修过的**文件；
* 于是修复停在分支上、判定跑的是旧代码。而失败指纹取的是"失败集合"的
  哈希，集合里少了一条（工作树多了个 ``backend/.venv`` 软链），指纹就换了
  —— 循环以为"换了个问题"，于是又派了一次针对旧根因的修复。

所以本模块只做一件事：**给定 ``project_dir``，决定"这次判定用哪份
``verification_ci_runner``"，并如实报告来源。**

解析规则
--------
1. 候选路径 ``<project_dir>/backend/verification_ci_runner.py``；
2. 该文件存在 **且** ``<project_dir>/backend/pyproject.toml`` 存在
   —— 第二个文件说明被测树**就是框架仓的检出** —— 那么必须用**被测树
   那份**；
3. 用 ``importlib.util.spec_from_file_location`` 以摘要命名的模块名加载
   （沿用 ``verification_agent._load_service_manager`` 的先例：摘要命名 +
   ``sys.modules`` 登记 + 执行后摘除）；
4. 加载失败（语法错 / 依赖缺失 / 签名不兼容）→ 回落 harness 自己的
   ``run_ci_verification``，并把 ``fallback_reason`` 写进出处。

每轮重新加载（**被测树那份**）
------------------------------
被测树那份模块是**每次调用新建**的。长驻进程（AC server 常年不重启）
因此不会拿着一份陈旧的模块判定一整轮 —— 这正是"改磁盘不改内存"那个坑
的解法：模块不是被缓存的，是每轮从磁盘新建的。

harness 自己那份是**回落**，走的是普通 import
-------------------------------------------
harness 的副本是框架自己的代码，由框架自己的进程 import 着；它同时是
框架**替换 runner 的注入点**（框架的测试就是替换
``verification_ci_runner.run_ci_verification``）。所以回落走普通 import：
被替换过的那份就是该跑的那份。本模块要修的不是它 —— 要修的是
"被测树那份修好了却没被跑到"，所以每轮重载的是**被测树那份**。

平铺 import 的约定
------------------
被测树那份 ``verification_ci_runner.py`` 只依赖 stdlib +
``bounded_subprocess`` + ``credentials``，而这两个是**平铺** import 的
（``backend/`` 坐在 ``sys.path`` 上）—— 它们解析到 **harness 的副本**，
不是被测树的副本。这与 ``_load_service_manager`` 注释里写明的约定一致；
写在这里是为了让它不悄悄变成隐式契约。
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


#: The runner came from the tree under test.
RESOLUTION_PROJECT = "project"
#: The runner came from the harness (this framework's own copy).
RESOLUTION_HARNESS = "harness"

#: The entry point every caller needs from a loaded runner module.
_ENTRY_POINT = "run_ci_verification"

#: The name the harness' own copy is imported under.  It is also the
#: framework's injection point: replacing ``run_ci_verification`` on it
#: swaps the runner, and the harness branch below has to honour that.
_HARNESS_MODULE_NAME = "verification_ci_runner"

#: The keyword arguments the caller passes to the entry point.  A module
#: that does not accept them is not usable here — and a *silent* use of
#: the wrong signature is how a verdict ends up without a gate.
_REQUIRED_KWARGS = ("project_dir", "artifact_dir")


@dataclass
class LoadedRunner:
    """The runner that judges this round, and where it came from."""

    module: Any
    run_ci_verification: Callable[..., Any]
    provenance: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Where the two candidates live
# ---------------------------------------------------------------------------


def harness_backend_dir() -> Path:
    """The ``backend/`` directory this loader was loaded from."""
    return Path(__file__).resolve().parent


def harness_runner_path() -> Path:
    """The harness' own ``verification_ci_runner.py``."""
    return harness_backend_dir() / "verification_ci_runner.py"


def project_runner_path(project_dir: Any) -> Optional[Path]:
    """The tree under test's ``backend/verification_ci_runner.py``.

    ``None`` when the tree does not carry one — a plain project has no
    such module, and the harness' copy is what judges it.
    """
    if not project_dir:
        return None
    try:
        candidate = Path(project_dir) / "backend" / "verification_ci_runner.py"
    except TypeError:
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def is_framework_checkout(project_dir: Any) -> bool:
    """Is the tree under test a checkout of **this** framework?

    Two files have to say yes: the runner module itself, and
    ``backend/pyproject.toml`` — the latter is what says "this tree is
    the framework", so a tree that merely happens to carry a file with
    that name is not judged by it.
    """
    path = project_runner_path(project_dir)
    if path is None:
        return False
    try:
        return (path.parent / "pyproject.toml").is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 出处 —— 说得清"谁在判"
# ---------------------------------------------------------------------------


def file_sha256(path: Any) -> str:
    """The sha256 of a file's bytes ("" when it cannot be read)."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return ""


def git_head_at(path: Any) -> str:
    """The HEAD of the repository that owns ``path`` ("" when unknown).

    Read from the repository's own files rather than by running ``git``:
    the answer is needed in a round that may run without a git binary in
    ``PATH``, and reading three small files cannot fail the way a
    subprocess can.

    A worktree's ``.git`` is a *file* that points at the worktree's own
    git directory, and the refs live in the **common** directory — so
    both are followed.
    """
    try:
        module_path = Path(path).resolve()
    except (OSError, TypeError):
        return ""
    repo_root = _repo_root_of(module_path)
    if repo_root is None:
        return ""
    git_dir = _git_dir_of(repo_root)
    if git_dir is None:
        return ""
    common_dir = _common_dir_of(git_dir)
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head if _is_sha(head) else ""
    ref = head.split(":", 1)[1].strip()
    return _read_ref(common_dir, git_dir, ref)


def _repo_root_of(path: Path) -> Optional[Path]:
    """The directory that holds the ``.git`` of the tree ``path`` is in."""
    for parent in path.parents:
        try:
            if (parent / ".git").exists():
                return parent
        except OSError:
            continue
    return None


def _git_dir_of(repo_root: Path) -> Optional[Path]:
    """The git directory of ``repo_root`` (a directory, or a ``.git`` file)."""
    dot_git = repo_root / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if not dot_git.is_file():
            return None
        text = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    target = text[len(prefix):].strip()
    if not target:
        return None
    git_dir = Path(target)
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()
    try:
        return git_dir if git_dir.is_dir() else None
    except OSError:
        return None


def _common_dir_of(git_dir: Path) -> Path:
    """The directory the refs live in (the common dir, for a worktree)."""
    marker = git_dir / "commondir"
    try:
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text:
                common = Path(text)
                if not common.is_absolute():
                    common = (git_dir / common).resolve()
                if common.is_dir():
                    return common
    except OSError:
        pass
    return git_dir


def _read_ref(common_dir: Path, git_dir: Path, ref: str) -> str:
    """The sha a ref points at (loose file first, then ``packed-refs``)."""
    for base in (common_dir, git_dir):
        try:
            candidate = base / ref
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if _is_sha(text):
                    return text
        except OSError:
            continue
    try:
        packed = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in packed.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == ref and _is_sha(parts[0]):
            return parts[0].strip()
    return ""


def _is_sha(text: str) -> bool:
    return len(text) == 40 and all(c in "0123456789abcdef" for c in text)


def harness_shared_venv() -> Optional[str]:
    """The harness' own ``backend/.venv`` ("" when it has none).

    The venv a gate may reuse when the tree under test has none.  It is
    the **harness'** venv on purpose: the tree under test may be a
    worktree with no environment of its own, and the gate has to run
    *somewhere*.
    """
    venv = harness_backend_dir() / ".venv"
    try:
        return str(venv) if venv.is_dir() else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_module_from_path(path: Path) -> Any:
    """Load a module from its path, under a name of its own.

    The name carries a digest of the path, so two trees never collide
    in ``sys.modules``; the module is registered while it executes (so a
    module that looks itself up by ``__name__`` finds itself) and
    removed again afterwards, so the registry keeps describing the
    harness' own modules.
    """
    backend_dir = str(harness_backend_dir())
    if backend_dir not in sys.path:
        # The flat-import convention: a project module does
        # ``from bounded_subprocess import ...``, and that name has to
        # resolve to the harness' copy.
        sys.path.append(backend_dir)
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    module_name = f"pdt_project_ci_runner_{digest}"
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


def _entry_point_of(module: Any, path: Path) -> Callable[..., Any]:
    """The module's ``run_ci_verification``, checked for the shape we call.

    A module that does not offer it, or that does not accept the
    arguments the caller passes, is **not** usable here: falling back is
    better than calling it wrong.
    """
    entry = getattr(module, _ENTRY_POINT, None)
    if not callable(entry):
        raise AttributeError(f"{path} does not offer {_ENTRY_POINT}")
    try:
        parameters = inspect.signature(entry).parameters
    except (TypeError, ValueError):
        return entry
    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for name in _REQUIRED_KWARGS:
        if name not in parameters and not accepts_any:
            raise TypeError(
                f"{path}: {_ENTRY_POINT} does not accept '{name}'"
            )
    return entry


def _provenance(
    resolution: str,
    module: Any,
    path: Path,
    fallback_reason: str,
) -> Dict[str, Any]:
    """The provenance of a loaded runner — where it came from, and why."""
    module_path = str(Path(path).resolve())
    sha256 = ""
    git_head = ""
    source = getattr(module, "verifier_source", None)
    if callable(source):
        try:
            reported = source()
            if isinstance(reported, dict):
                module_path = str(
                    reported.get("module_path") or module_path
                )
                sha256 = str(reported.get("sha256") or "")
                git_head = str(reported.get("git_head") or "")
        except Exception:  # noqa: BLE001 — provenance never fails a round
            pass
    if not sha256:
        sha256 = file_sha256(path)
    if not git_head:
        # The module's own report is preferred, but it is not required:
        # a project copy whose flat imports do not reach the loader
        # cannot compute the HEAD — and the provenance may not be half
        # an answer.
        git_head = git_head_at(path)
    return {
        "resolution": resolution,
        "module_path": module_path,
        "sha256": sha256,
        "git_head": git_head,
        "fallback_reason": fallback_reason,
    }


def _harness_module() -> Any:
    """The harness' own ``verification_ci_runner`` — the imported one.

    The harness' copy is resolved the way the framework has always
    resolved it: through the import system.  That is deliberate:

    * it is the framework's own **injection point** — replacing
      ``run_ci_verification`` on the imported module swaps the runner,
      and the harness branch has to honour that replacement;
    * it is the framework's own code, loaded by the framework's own
      process — reloading it from disk every round would be a second,
      quieter version of the very problem this module exists to fix.

    The tree under test's copy is the one that is loaded fresh (see
    :func:`load_ci_runner`); this is the fallback, and the fallback
    behaves exactly as it did before the loader existed.
    """
    module = sys.modules.get(_HARNESS_MODULE_NAME)
    if module is not None:
        return module
    import verification_ci_runner  # noqa: PLC0415

    return verification_ci_runner


def _module_path_of(module: Any) -> Path:
    """The file a loaded module came from.

    The harness' own path is the answer unless the module says
    otherwise — a module imported from somewhere else is reported where
    it came from, because the provenance's whole job is to name the
    file that judged.
    """
    raw = getattr(module, "__file__", None)
    if not raw:
        return harness_runner_path()
    try:
        return Path(raw).resolve()
    except OSError:
        return harness_runner_path()


def _harness_runner(fallback_reason: str) -> LoadedRunner:
    """The harness' own copy of the runner."""
    module = _harness_module()
    path = _module_path_of(module)
    try:
        entry = _entry_point_of(module, path)
    except Exception as exc:  # noqa: BLE001 — a fallback, never a failure
        reason = fallback_reason or (
            f"harness copy unusable: {type(exc).__name__}: {exc}"
        )
        logger.warning(
            "[ci_runner_loader] the harness' own verification_ci_runner "
            "(%s) has no usable %s: %s", path, _ENTRY_POINT, exc,
        )
        entry = getattr(module, _ENTRY_POINT, None)
        if not callable(entry):
            raise
        return LoadedRunner(
            module=module,
            run_ci_verification=entry,
            provenance=_provenance(
                RESOLUTION_HARNESS, module, path, reason,
            ),
        )
    return LoadedRunner(
        module=module,
        run_ci_verification=entry,
        provenance=_provenance(RESOLUTION_HARNESS, module, path, fallback_reason),
    )


def load_ci_runner(project_dir: Any = None) -> LoadedRunner:
    """The runner that should judge ``project_dir`` — and where it came from.

    Never raises: a tree whose runner cannot be loaded falls back to the
    harness' own copy, with the reason in the provenance.  A round that
    cannot be judged is worse than one judged by the wrong file, but a
    round that **crashes** is worse than both.
    """
    if is_framework_checkout(project_dir):
        path = project_runner_path(project_dir)
        try:
            module = _load_module_from_path(path)
            entry = _entry_point_of(module, path)
        except Exception as exc:  # noqa: BLE001 — a fallback, never a failure
            logger.warning(
                "[ci_runner_loader] could not load the tree under test's "
                "own verification_ci_runner (%s); falling back to the "
                "harness' copy: %s", path, exc,
            )
            return _harness_runner(f"{type(exc).__name__}: {exc}")
        return LoadedRunner(
            module=module,
            run_ci_verification=entry,
            provenance=_provenance(RESOLUTION_PROJECT, module, path, ""),
        )
    return _harness_runner("")
