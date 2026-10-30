"""全量 CI 门禁的框架执行器（2026-09-18，Phase 2 门禁补全）。

这个模块为什么存在
------------------
2026-09-16 用户定了两阶段验证：Phase 1 是子任务级验收，Phase 2 是
"**必须经过、但只能放在最后**"的全量关卡，一共两种 —— 先全量
Nightly CI，后全量 E2E。

2026-09-18 的 VP 判定重构把 ``test_command`` 从 VP 里整个删掉之后，
Phase 2 的这两种关卡里有一种**失去了表达方式**：

* 全量 E2E 还能落在 ``ui_validation`` 上（真开浏览器、真交互）；
* 全量 CI 门禁落在哪都别扭 —— ``api_test`` 要 request/assertions，
  ``code_review`` 要评审，``ui_validation`` 要浏览器，而
  ``evidence_command`` 按定义"不参与判定"。

结果是一次重新生成的计划里 Phase 2 的 Nightly CI 关卡凭空消失 ——
而那条正是 PRD 验收标准第 9 条。**这是删掉 test_command 时留下的洞，
不是规划模型没写好。** 本模块补回的是"整仓 CI 门禁"这个**验证种类**
的判定能力。

为什么不是 ``test_command`` 的回归
----------------------------------
``test_command`` 的病根有两条，这个模块一条都不沾：

* 它是一条**任意命令**，且**由它决定判定** —— 于是 10/28 条 VP 被
  ``echo 'Basic functionality check'`` 填充，退出 0 但什么都没证。
  这里的 ``ci_entry`` 被约束到"仓库的 CI 总入口"这一个形态上：
  :func:`validate_vp` 会拒绝点名单个测试文件 / 用 ``-k`` / ``::``
  收窄过的入口（见 :data:`NARROWING_MARKERS` 与
  :func:`looks_narrowed`），规划期还有护栏核对它是否引用了真实存在的
  入口脚本。
* 它的判定依赖执行者**自报**的退出码（C7 删掉的 cross-verify 就是
  栽在这里：同一个工件两轮判出相反结果）。这里的命令由**框架自己跑、
  自己读退出码**，没有任何自报环节，所以同一条命令在同一棵树上永远
  给同一个判定。

它也不是 ``automated_test`` 的回归：那个方法的罪名是在 **Phase 1**
把开发自测原样再跑一遍（重复且不黑盒），还要为此拉一个完整子 agent。
``full_ci`` 只允许出现在 Phase 2，且不启 LLM。

Schema
------
::

    {
      "id": "VP-017",
      "title": "【全量】Nightly CI：整仓 Rust workspace + 全量 pytest",
      "verification_method": "full_ci",
      "verification_phase": 2,
      "phase_order": 1,
      "priority": "high",
      "ci_entry": "python scripts/ci_local.py --provider vendor-a-pro",
      "ci_timeout_seconds": 3600,
      "expected_result": "整仓门禁与全部测试通过，退出码 0"
    }

判定
----
``ci_entry`` 退出 0 → PASSED；非 0 / 超时 / 入口不可执行 → FAILED，并把
stdout/stderr 尾部附在 verdict 的 evidence 里、原始输出落盘
``plans/<id>/vp_artifacts/<vp_id>/ci_output.json``。**只有这一条判据。**
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bounded_subprocess import run_bounded
from credentials import _FD_ENV_PREFIX as _SECRET_FD_ENV_PREFIX

logger = logging.getLogger(__name__)

STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"

#: The one field a ``full_ci`` VP must declare.
CI_ENTRY_FIELD = "ci_entry"

#: Ceiling for a full CI gate. A whole-repo nightly run is measured in
#: tens of minutes (a whole workspace build plus the full test suite), so
#: the default is deliberately far
#: above the Phase-1 evidence-command ceiling (600s). It is still a hard
#: cap: a hung gate is killed and reported, not left to eat the round.
DEFAULT_CI_TIMEOUT_SECONDS = 3600

#: Output kept per stream in the verdict evidence.
MAX_OUTPUT_CHARS = 4000

#: 结构性收窄标记 —— 一条 ``ci_entry`` 命中任何一个，说明它指的不是"整仓"，
#: 而是"某个测试子集"。``full_ci`` 的职责是整仓门禁，收窄过的入口属于
#: Phase 1（而且 Phase 1 不该跑命令，见模块 docstring）。
#:
#: 这些刻意只放**结构性**标记（选择器 / 单文件 / 节点 id），不做"命令里有没有
#: 出现某个词"的语义猜测 —— 2026-09-16 那次裸子串匹配（``re.compile("e2e")``
#: 命中的是路径 ``tests/e2e/tooltip.spec.ts``，把 5 条只跑单个 spec 的
#: 有界 VP 误提升成 Phase 2）就是栽在这种猜测上。
#:
#: ``::`` 是 pytest 的用例节点分隔符（``tests/test_x.py::TestY::test_z``）
#: —— 出现它就说明点名到了具体用例，不是跑整套。
NODE_ID_MARKER = "::"

#: pytest 的用例收窄选择器 —— 用了它就不是"跑整套"。
_PYTEST_NARROWING_FLAGS = ("-k", "--keyword", "-m", "--markexpr")

#: python 解释器的可执行文件名 —— ``python`` / ``python3`` / ``python3.11``
#: / ``python3.exe``（含 Windows 的 ``.exe`` 后缀）。
#:
#: 存在的理由：``-m`` 是唯一一个**拼写会撞车**的收窄选择器。
#: ``python3 -m pytest backend/tests`` 里的 ``-m`` 是**解释器**的"以模块
#: 方式运行"标志（``-m pytest``），后面的 ``pytest backend/tests`` 跑的是
#: 整个目录 —— 但旧判据只看 token 本身，把解释器的 ``-m`` 当成了 pytest 的
#: ``--markexpr`` 简写，于是把一条**整仓**入口判成"收窄了验证范围"，
#: 门禁一行都没跑（2026-10-24 的 VP-019）。
#:
#: 判据：``-m`` 紧跟在 python 解释器后面 → 它属于解释器，不算收窄；
#: 否则 → 它是 pytest 自己的选择器，照旧判为收窄。
_PYTHON_INTERPRETER_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$")

#: cargo test 的收窄选择器。
_CARGO_NARROWING_FLAGS = ("--test", "--lib", "--bins", "--examples", "--doc")

#: 单文件后缀：入口点名到一个测试文件就是收窄。
_TEST_FILE_SUFFIXES = (
    ".spec.ts", ".spec.js", ".spec.tsx",
    ".test.ts", ".test.js", ".test.tsx",
)

#: Shell executables whose ``-c`` flag takes a *command string*.
#:
#: The payload of such a flag is a whole command line, not an argument —
#: it has to be split again before the paths inside it can be read.
_SHELL_BINARIES = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

#: Letters allowed in a shell flag cluster that carries a command string.
#: ``-c`` / ``-lc`` / ``-ic`` qualify; ``--norc`` does not (it contains a
#: ``c`` but is not a command-string flag).
_SHELL_COMMAND_FLAG_LETTERS = frozenset("cliev")

#: Shell options that take a value of their own (``bash -o pipefail -c …``).
_SHELL_OPTIONS_WITH_VALUE = frozenset({"-o", "+o"})

#: Characters that end a command inside a compound command line.
_COMMAND_SEPARATORS = ";&|"

#: An environment assignment in front of a command (``FOO=bar cmd``).
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class SchemaIssue:
    """One reason a ``full_ci`` VP cannot be executed as declared."""

    vp_id: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"vp_id": self.vp_id, "detail": self.detail}


@dataclass
class CiRunResult:
    """Everything one ``full_ci`` gate produced — verdict plus raw basis."""

    status: str = STATUS_FAILED
    reasons: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_verdict_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# Schema + anti-narrowing validation (pure — reused by the plan guard)
# ---------------------------------------------------------------------------


def _belongs_to_python_interpreter(tokens: List[str], index: int) -> bool:
    """Is ``tokens[index]`` a ``-m`` that belongs to a python interpreter?

    True means the ``-m`` is the interpreter's "run this module" flag
    (``python3 -m pytest``), not pytest's ``--markexpr`` shorthand.

    The discriminator is the token in front of the ``-m``: when its
    basename is a python interpreter (``python`` / ``python3`` /
    ``python3.11`` / ``python3.exe``) the ``-m`` is the interpreter's.
    A ``-m`` with nothing in front of it — or with anything else in
    front of it — stays pytest's, and stays a narrowing signal.
    """
    if index == 0:
        return False
    previous = tokens[index - 1]
    if previous.startswith("-"):
        # A flag cannot be an interpreter path; ``-m`` after another
        # flag is pytest's own selector.
        return False
    return bool(_PYTHON_INTERPRETER_RE.match(os.path.basename(previous)))


def looks_narrowed(entry: str) -> Optional[str]:
    """Return why ``entry`` is a *scoped* command rather than a full gate.

    ``None`` means "no narrowing marker found" — which is not the same as
    "provably whole-repo". The plan-time guard (``verification_plan_completeness``)
    carries the positive check (does it reference a real repo entry
    point?); this function carries the negative one, which is the part
    that stops ``ci_entry`` from decaying back into a general-purpose
    ``test_command``.

    One marker has an exception: ``-m``. ``python3 -m pytest <dir>``
    puts a ``-m`` on the command line that belongs to the *interpreter*
    ("run this module"), not to pytest — the entry still runs the whole
    directory. See :func:`_belongs_to_python_interpreter` for how the
    two are told apart. Every other marker keeps its plain reading.
    """
    text = str(entry or "").strip()
    if not text:
        return None

    lowered = text.lower()
    for suffix in _TEST_FILE_SUFFIXES:
        if suffix in lowered:
            return f"入口点名到单个测试文件（`{suffix}`）"

    if NODE_ID_MARKER in text:
        return f"入口点名到具体用例（`{NODE_ID_MARKER}` 节点语法）"

    try:
        tokens = shlex.split(text)
    except ValueError:
        # Unbalanced quotes — the shell will decide at run time. Not a
        # narrowing signal, so don't claim it is one.
        tokens = text.split()

    for index, token in enumerate(tokens):
        if any(flag == token or token.startswith(flag + "=")
               for flag in _PYTEST_NARROWING_FLAGS):
            if token == "-m" and _belongs_to_python_interpreter(tokens, index):
                # ``python3 -m pytest …`` —— 这个 ``-m`` 是解释器的
                # "以模块方式运行"标志，不是 pytest 的 markexpr。入口
                # 跑的是 ``pytest`` 后面的整个目标，不是收窄过的用例集。
                continue
            return f"入口用了用例收窄选择器 `{token}`"
        if any(flag == token for flag in _CARGO_NARROWING_FLAGS):
            return f"入口用了收窄选择器 `{token}`"

    # ``pytest path/to/test_x.py`` (or a bare test file path) is a
    # narrowed run even without a flag.
    for token in tokens:
        if token.startswith("-"):
            continue
        base = os.path.basename(token)
        if base.startswith("test_") and base.endswith(".py"):
            return f"入口点名到单个测试文件（`{token}`）"
        if base.endswith("_test.rs"):
            return f"入口点名到单个测试文件（`{token}`）"

    return None


def _referenced_paths(entry: str, project_dir: Optional[Path] = None) -> List[str]:
    """Path-like tokens in ``entry`` that look repo-local.

    Heuristic on purpose, and only used for the *existence* check: a
    token with a ``/`` or a known script suffix that isn't an absolute
    path and isn't a URL.

    A ``ci_entry`` is a *shell* command line, so the reader has to
    understand compound commands — otherwise the entry is misread and the
    gate is rejected before it runs:

    * the payload of a shell's ``-c`` flag (``bash -c 'cd backend && …'``)
      is a command **string**, not an argument: it is split again, and
      the paths inside it are read as paths;
    * a ``cd <dir>`` moves the commands that follow it into ``<dir>``, so
      a relative path named after it is relative to that directory, not
      to the repository root (``state_machine/tests`` after
      ``cd backend`` is ``backend/state_machine/tests``).

    The flag / absolute-path / URL skip rules are unchanged, and the
    existence check in :func:`validate_vp` is not relaxed: a path that
    cannot be found is still an issue.
    """
    return _collect_referenced_paths(_split_command_text(entry), "")


def _split_command_text(text: str) -> List[str]:
    """Split one command line into tokens, tolerating unbalanced quotes."""
    try:
        return shlex.split(text)
    except ValueError:
        # Unbalanced quotes — the shell will decide at run time.
        return text.split()


def _shell_command_payload(tokens: List[str], index: int) -> Optional[int]:
    """Index of the command string a shell invocation carries, if any.

    ``bash -c 'cd backend && …'`` — the payload is a whole command line,
    so it has to be split again before the paths inside it can be read.

    The flag has to belong to *this* invocation: the scan stops at the
    first word that is not a flag, so the ``-c`` of a *later* command in
    the chain is never mistaken for this one's payload.
    """
    if os.path.basename(tokens[index]) not in _SHELL_BINARIES:
        return None
    offset = index + 1
    while offset < len(tokens):
        flag = tokens[offset]
        if not flag.startswith("-"):
            # A command word — this shell invocation carries no -c.
            return None
        if flag in _SHELL_OPTIONS_WITH_VALUE:
            offset += 2  # skip the option and its value
            continue
        letters = flag[1:]
        if "c" in letters and set(letters) <= _SHELL_COMMAND_FLAG_LETTERS:
            return offset + 1 if offset + 1 < len(tokens) else None
        offset += 1
    return None


def _looks_like_path(token: str) -> bool:
    """Is this token a repo-local path the gate names?"""
    if token.startswith("-") or "://" in token or token.startswith("#"):
        return False
    if token.startswith("/") or token.startswith("~"):
        # Absolute paths are outside the repo's contract; the
        # project root is what a gate must be runnable from.
        return False
    return "/" in token or token.endswith((".py", ".sh", ".js", ".cjs", ".mjs", ".ts"))


def _resolve_cd_target(base: str, target: str) -> str:
    """The directory a ``cd <target>`` moves to, relative to the root."""
    if not target:
        return base
    if target.startswith("~"):
        target = os.path.expanduser(target)
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(base, target))


def _join_base(base: str, token: str) -> str:
    """The token resolved against the directory the command runs in."""
    if not base:
        return token
    return os.path.normpath(os.path.join(base, token))


def _collect_referenced_paths(tokens: List[str], base: str) -> List[str]:
    """Repo-relative paths named by ``tokens``, given the current ``base``.

    ``base`` is the directory the commands run in — relative to the
    repository root (``""`` = the root itself), or absolute when a
    ``cd`` named an absolute directory.
    """
    out: List[str] = []
    index = 0
    at_command_start = True

    while index < len(tokens):
        token = tokens[index].rstrip(_COMMAND_SEPARATORS)
        if not token:
            # A separator (``&&`` / ``||`` / ``;`` …): the next token
            # starts a new command.
            at_command_start = True
            index += 1
            continue

        if at_command_start and _ENV_ASSIGNMENT_RE.match(token):
            # ``FOO=bar cmd`` — an assignment, not a command word.
            index += 1
            continue

        payload_index = _shell_command_payload(tokens, index)
        if payload_index is not None:
            # A shell command string: split it again and read the paths
            # inside it. The payload runs in a subshell, so a ``cd`` in
            # it does not change the directory of the outer chain.
            out.extend(_collect_referenced_paths(
                _split_command_text(tokens[payload_index]), base,
            ))
            index = payload_index + 1
            at_command_start = False
            continue

        if at_command_start and token == "cd" and index + 1 < len(tokens):
            base = _resolve_cd_target(
                base, tokens[index + 1].rstrip(_COMMAND_SEPARATORS),
            )
            index += 2
            at_command_start = False
            continue

        if _looks_like_path(token):
            out.append(_join_base(base, token))

        at_command_start = False
        index += 1

    return out


def validate_vp(vp: Any, *, project_dir: Optional[Path] = None) -> List[SchemaIssue]:
    """Return every schema problem that makes this VP unrunnable.

    Called at plan-generation time (so a malformed ``full_ci`` plan is
    regenerated rather than run) **and** again by
    :func:`run_ci_verification` (so a hand-edited plan fails loudly at
    execution time instead of verifying nothing).

    ``project_dir`` switches on the existence check: a gate whose entry
    names a repo script that does not exist cannot pass, and finding that
    out at plan time is far cheaper than finding it out after a 40-minute
    round.
    """
    issues: List[SchemaIssue] = []
    if not isinstance(vp, dict):
        return [SchemaIssue("unknown", f"VP is {type(vp).__name__}, expected an object")]
    vp_id = str(vp.get("id", "unknown"))

    entry = vp.get(CI_ENTRY_FIELD)
    if not isinstance(entry, str) or not entry.strip():
        issues.append(SchemaIssue(
            vp_id,
            f"full_ci requires a non-empty '{CI_ENTRY_FIELD}' string — "
            f"整仓门禁的判定依据就是这条入口的退出码",
        ))
        return issues

    narrowed = looks_narrowed(entry)
    if narrowed:
        issues.append(SchemaIssue(
            vp_id,
            f"{CI_ENTRY_FIELD} 收窄了验证范围（{narrowed}）："
            f"full_ci 的职责是整仓门禁；收窄到用例级的验证属于 Phase 1，"
            f"而 Phase 1 不跑命令。",
        ))

    if project_dir is not None:
        root = Path(project_dir)
        for rel in _referenced_paths(entry.strip(), root):
            if not (root / rel).exists():
                issues.append(SchemaIssue(
                    vp_id,
                    f"{CI_ENTRY_FIELD} 引用的路径不存在：{rel}",
                ))

    return issues


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def ci_timeout(vp: Dict[str, Any]) -> int:
    raw = vp.get("ci_timeout_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CI_TIMEOUT_SECONDS
    return max(1, value)


def ci_subprocess_env() -> Dict[str, str]:
    """The environment a CI gate runs in, without the descriptor addresses.

    A ``PDT_SECRET_FD_*`` variable carries the *number* of a pipe the
    launcher published to the backend. That number is an address in the
    publisher's descriptor table, and it means nothing in any other
    process — but the variable itself is inherited by everything this
    backend starts, including the gate.

    The gate is not given those pipes: it is a plain subprocess, so it
    never receives the descriptors themselves. Forwarding the numbers
    therefore hands the gate an address that resolves to whatever
    happens to occupy that slot in *its* table — and a reader that
    trusted the number would block on a stranger's pipe or close a
    stranger's descriptor. The gate has no use for the variables, so
    they are dropped here rather than passed down.

    Everything else is inherited unchanged. The gate is the repository's
    own CI entry, and it is entitled to the environment it would have
    run in by hand.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_SECRET_FD_ENV_PREFIX)
    }


def run_ci_verification(
    vp: Dict[str, Any],
    *,
    project_dir: Path,
    artifact_dir: Optional[Path] = None,
) -> CiRunResult:
    """Run one ``full_ci`` gate and grade it on the exit code.

    Deterministic end to end — same tree, same entry, same verdict, every
    round. Never raises: a broken entry is a FAILED verdict carrying the
    reason, not an exception the round has to survive.
    """
    vp_id = str(vp.get("id", "unknown"))
    result = CiRunResult()

    issues = validate_vp(vp, project_dir=project_dir)
    if issues:
        result.reasons = [f"schema: {i.detail}" for i in issues]
        result.evidence = {
            "vp_id": vp_id,
            "schema_issues": [i.to_dict() for i in issues],
        }
        return result

    entry = str(vp[CI_ENTRY_FIELD]).strip()
    timeout = ci_timeout(vp)
    project_dir = Path(project_dir)

    started = time.monotonic()
    exit_code: Optional[int] = None
    stdout = stderr = ""
    timed_out = False
    error = ""

    try:
        completed = run_bounded(
            entry,
            cwd=str(project_dir),
            text=True,
            timeout=timeout,
            env=ci_subprocess_env(),
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        error = f"超时：{timeout}s 内没有跑完"
    except Exception as exc:  # noqa: BLE001 - a broken gate is FAILED
        error = f"{type(exc).__name__}: {exc}"

    duration = time.monotonic() - started
    stdout_tail = stdout[-MAX_OUTPUT_CHARS:]
    stderr_tail = stderr[-MAX_OUTPUT_CHARS:]

    evidence: Dict[str, Any] = {
        "vp_id": vp_id,
        "ci_entry": entry,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 2),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    if error:
        evidence["error"] = error

    if not timed_out and not error and exit_code == 0:
        result.status = STATUS_PASSED
        result.reasons = [
            f"[full_ci] `{entry}` 退出码 0（用时 {duration:.0f}s）"
        ]
    else:
        result.status = STATUS_FAILED
        if timed_out:
            detail = error
        elif error:
            detail = f"入口无法执行：{error}"
        else:
            detail = f"退出码 {exit_code}"
        result.reasons = [
            f"[full_ci] `{entry}` {detail}（用时 {duration:.0f}s）"
        ]
        tail = stderr_tail.strip() or stdout_tail.strip()
        if tail:
            result.reasons.append(f"输出尾部：{tail[-600:]}")

    result.evidence = evidence
    _write_artifact(artifact_dir, vp_id, evidence)
    return result


def _write_artifact(
    artifact_dir: Optional[Path], vp_id: str, payload: Dict[str, Any],
) -> None:
    """Persist the raw basis of this verdict for post-hoc investigation.

    A failed write is logged and ignored: losing the audit copy must not
    change the verdict, which has already been computed.

    ``mkstemp`` allocates ``.tmp`` *before* the write — so when
    ``json.dump`` raises (unserialisable payload, mid-write disk
    full), the half-written tempfile is on disk and we have to
    ``os.unlink`` it explicitly.  Without that, ``.tmp`` files
    accumulate under ``plans/<id>/vp_artifacts/<vp>/`` across rounds,
    invisible to ``find_orphans`` (which only knows about non-``tmp``
    files), so the residue silently grows until the next operator
    sweep.
    """
    if artifact_dir is None:
        return
    tmp_name: Optional[str] = None
    try:
        target_dir = Path(artifact_dir) / vp_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "ci_output.json"
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, target)
        tmp_name = None  # ownership transferred; do not unlink on success
    except Exception as exc:  # noqa: BLE001 - audit copy is best-effort
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        logger.warning(
            "[ci_runner] could not write artifact for %s: %s", vp_id, exc,
        )
