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
``ci_entry`` 退出 0 → PASSED；门禁自己的 summary 说它「判不了」（退出 3，
或预算用尽）→ INCONCLUSIVE；其余非 0 / 超时 / 入口不可执行 → FAILED，并把
stdout/stderr 尾部附在 verdict 的 evidence 里、原始输出落盘
``plans/<id>/vp_artifacts/<vp_id>/ci_output.json``。**只有这一条判据。**

INCONCLUSIVE 不是「过了」，也不是「代码坏了」—— 门禁没有判，它只说明这次
运行没跑完。把它写成 FAILED 会让修复循环去改一个没被证明有缺陷的交付物。

截尾的输出只留最后 :data:`MAX_OUTPUT_CHARS` 个字符，而门禁是三段串行跑的
—— 段 1 的失败在段 3 结束时早就被截掉了。所以门禁脚本在
:data:`GATE_JUNIT_DIR_ENV` 指向一个目录时，会**每段写一份 junit XML**
（``phase-<n>.xml``）；跑完后本模块把这份证据读回 verdict：

* ``junit_phases`` —— 每段的用例数 / 失败数 / 错误数 / 跳过数；
* ``junit_failures`` —— 每条失败的 node id 与首行消息。

这两条字段**只是证据**：判定仍然只看退出码。XML 缺失 → 两个字段为空；
XML 解析不了 → 记 ``junit_error`` 并继续，绝不抛异常、绝不影响判定。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bounded_subprocess import run_bounded
from credentials import _FD_ENV_PREFIX as _SECRET_FD_ENV_PREFIX

logger = logging.getLogger(__name__)

STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"

#: The gate could not decide: its budget ran out, or the host could not
#: be measured.  The gate has three exit codes (0 passed / 1 a phase
#: failed / 3 could not decide) and exit 3 says nothing about the code.
#:
#: It must not be reported as :data:`STATUS_FAILED`: a FAILED verdict is
#: what the repair loop turns into a fix task, and a machine that ran
#: out of time has not proven a deliverable defective.
STATUS_INCONCLUSIVE = "INCONCLUSIVE"

#: The one field a ``full_ci`` VP must declare.
CI_ENTRY_FIELD = "ci_entry"

#: Ceiling for a full CI gate. A whole-repo nightly run is measured in
#: tens of minutes (a whole workspace build plus the full test suite), so
#: the default is deliberately far
#: above the Phase-1 evidence-command ceiling (600s). It is still a hard
#: cap: a hung gate is killed and reported, not left to eat the round.
#:
#: This is the *fallback*: it applies only when the gate does not declare
#: a budget of its own.  A gate that declares one (see
#: :func:`gate_budget_seconds`) is watched with that budget plus a
#: margin, because a watchdog that is shorter than the gate it watches
#: cannot report anything except the fact that it fired.
DEFAULT_CI_TIMEOUT_SECONDS = 3600

#: The margin the watchdog adds to the budget the gate declares.
#:
#: The gate stops *itself* at its budget and prints a verdict; the
#: margin is the time it needs to finish that verdict, write its
#: summary and be reaped.  Without it the watchdog would fire in the
#: very second the gate is trying to report that it ran out of time.
GATE_BUDGET_MARGIN_SECONDS = 300

#: Where the gate lives inside a project, relative to the project root.
GATE_SCRIPT_RELATIVE_PATH = ("scripts", "run_tests.sh")

#: The declaration the gate publishes its budget with.  The gate is the
#: single source of truth — the watchdog reads the number out of the
#: gate's own script rather than keeping a second copy of it, which is
#: how the two budgets drifted apart in the first place.
_GATE_BUDGET_RE = re.compile(
    r"^GATE_BUDGET_SECONDS_([A-Z0-9]+)=([0-9]+)\s*$", re.MULTILINE
)

#: The lane a ``--lane`` argument names, in either spelling.
_LANE_ARGUMENT_RE = re.compile(r"(?:^|\s)--lane(?:=|\s+)([A-Za-z0-9_]+)")

#: The machine-readable lines of the gate's own summary.
_GATE_BUDGET_LINE_RE = re.compile(r"^gate budget:\s*(\d+)s\s*$", re.MULTILINE)
_GATE_ELAPSED_LINE_RE = re.compile(r"^gate elapsed:\s*(\d+)s\s*$", re.MULTILINE)
_GATE_EXHAUSTED_LINE_RE = re.compile(
    r"^gate budget exhausted in:\s*(.+?)\s*$", re.MULTILINE
)
_GATE_VERDICT_LINE_RE = re.compile(
    r"^gate:\s*([A-Z]+)\s*\(exit (\d+)\)", re.MULTILINE
)
_GATE_PHASE_HEADER_RE = re.compile(r"^=== phase (.+?) ===\s*$", re.MULTILINE)

#: Output kept per stream in the verdict evidence.
MAX_OUTPUT_CHARS = 4000

#: The environment variable that tells the gate script where to write one
#: junit report per phase.
#:
#: The gate runs three phases back to back, and the verdict keeps only
#: the last :data:`MAX_OUTPUT_CHARS` characters of the whole run's
#: output — so a failure in phase 1 is gone from the evidence by the
#: time phase 3 ends.  A junit report per phase is what makes the
#: failure locatable again: each phase writes its own report, and a
#: report *names* the cases that failed instead of leaving them in a
#: scrolled-off stream.
GATE_JUNIT_DIR_ENV = "PDT_GATE_JUNIT_DIR"

#: Ceiling on the number of failures the evidence carries.  A gate that
#: fails five thousand cases says less with five thousand entries than
#: with the first fifty — and the total is in ``junit_phases`` either
#: way, so nothing is hidden by the ceiling.
MAX_JUNIT_FAILURES = 50

#: Ceiling on the length of a failure's first line.  The line is a
#: pointer ("assert 41 == 42"), not the traceback: the whole traceback
#: is in the junit file the evidence names.
MAX_JUNIT_MESSAGE_CHARS = 300

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


def gate_budget_seconds(
    project_dir: Optional[Path],
    entry: Optional[str] = None,
) -> Optional[int]:
    """The budget the gate declares for the run *entry* asks for.

    The gate publishes its budget in its own script (``scripts/run_tests.sh``)
    and this reads that publication — the gate is the single source of
    truth, and the watchdog is a reader of it rather than a second copy
    of it.

    *entry* is the command the VP runs; it may name a lane
    (``--lane unit``), and a lane has a smaller budget than the whole
    gate.  An entry that names no lane asks for the whole gate.

    ``GATE_BUDGET_SECONDS`` in the environment overrides both — the gate
    reads the same variable, so an operator who raises the budget raises
    it on both sides of the fence.

    ``None`` means "the gate declares no budget" — the caller falls back
    to the historical default.
    """
    override = os.environ.get("GATE_BUDGET_SECONDS", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    if project_dir is None:
        return None
    script = Path(project_dir).joinpath(*GATE_SCRIPT_RELATIVE_PATH)
    try:
        text = script.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    budgets = {
        lane.lower(): int(value)
        for lane, value in _GATE_BUDGET_RE.findall(text)
    }
    if not budgets:
        return None
    return budgets.get(_lane_of(entry), budgets.get("all"))


def _lane_of(entry: Optional[str]) -> str:
    """The lane an entry asks for; the whole gate when it names none."""
    if not entry:
        return "all"
    match = _LANE_ARGUMENT_RE.search(entry)
    if not match:
        return "all"
    return match.group(1).lower()


def parse_gate_summary(stdout: str) -> Dict[str, Any]:
    """What the gate's own summary says about the run it just did.

    The summary is the gate's account of itself: the budget it was
    given, the time it spent, the phase that ran out of budget, and the
    verdict it reached.  A reader that cannot parse it is left with the
    last line of stdout — which is what the round-2 artifact recorded,
    and it answers nothing.

    Anything that is not a gate summary reads as "nothing is known",
    which is what it is: an entry that is not the gate has no summary.
    """
    summary: Dict[str, Any] = {
        "budget_seconds": None,
        "elapsed_seconds": None,
        "verdict": "",
        "exit_code": None,
        "budget_exhausted_in": "",
        "running_phase": "",
    }
    if not stdout:
        return summary
    budget = _GATE_BUDGET_LINE_RE.search(stdout)
    if budget:
        summary["budget_seconds"] = int(budget.group(1))
    elapsed = _GATE_ELAPSED_LINE_RE.search(stdout)
    if elapsed:
        summary["elapsed_seconds"] = int(elapsed.group(1))
    exhausted = _GATE_EXHAUSTED_LINE_RE.search(stdout)
    if exhausted:
        summary["budget_exhausted_in"] = exhausted.group(1)
    verdict = _GATE_VERDICT_LINE_RE.search(stdout)
    if verdict:
        summary["verdict"] = verdict.group(1)
        summary["exit_code"] = int(verdict.group(2))
    phases = _GATE_PHASE_HEADER_RE.findall(stdout)
    if phases:
        # The last phase that started is the one that was running.
        summary["running_phase"] = phases[-1]
    return summary


def ci_timeout(
    vp: Dict[str, Any],
    *,
    project_dir: Optional[Path] = None,
    entry: Optional[str] = None,
) -> int:
    """The watchdog's budget for this VP.

    The watchdog is **never shorter than the gate it watches**.  When the
    gate declares a budget, the watchdog gets that budget plus a margin
    — a VP that declares a shorter timeout does not get one, because a
    watchdog shorter than the gate is the failure this whole mechanism
    exists to prevent: the gate is killed from the outside and reports
    nothing.

    When the gate declares nothing, the historical default stands.
    """
    declared = _declared_ci_timeout(vp)
    budget = gate_budget_seconds(project_dir, entry)
    if budget is None:
        if declared is None:
            return DEFAULT_CI_TIMEOUT_SECONDS
        return declared
    floor = budget + GATE_BUDGET_MARGIN_SECONDS
    if declared is None:
        return floor
    return max(declared, floor)


def _declared_ci_timeout(vp: Dict[str, Any]) -> Optional[int]:
    """The timeout the VP itself declares, or ``None`` when it declares none."""
    raw = vp.get("ci_timeout_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
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


def _spawn_registry() -> Tuple[Path, Path]:
    """The registry of this run's detached processes, and its directory.

    The gate is about to start services, and a service is started
    detached: it leads its own session, so the watchdog that kills the
    gate's process group does not reach it.  The registry is how the
    runner finds those services afterwards — including when the gate
    was killed and never got to its own cleanup.
    """
    directory = Path(tempfile.mkdtemp(prefix="pdt-ci-spawn-registry-"))
    return directory, directory / "spawn_registry.jsonl"


def _reap_spawned(registry: Path, *, reason: str) -> List[Dict[str, Any]]:
    """Give back the processes the registry names.  Never raises.

    A cleanup path that raises would take the verdict down with it, and
    the verdict is the thing this module exists to produce.
    """
    try:
        import orphan_reaper
    except Exception:  # pragma: no cover - the module is stdlib-only
        return []
    try:
        return list(
            orphan_reaper.reap_spawned_processes(registry, reason=reason)
        )
    except Exception as exc:  # noqa: BLE001 - a cleanup never fails a verdict
        logger.warning(
            "[verification_ci_runner] could not give the spawned "
            "processes back: %s", exc,
        )
        return []


# ---------------------------------------------------------------------------
# The per-phase junit reports (evidence, never a verdict)
# ---------------------------------------------------------------------------
#
# The gate script writes one junit report per phase when
# :data:`GATE_JUNIT_DIR_ENV` names a directory.  Everything below reads
# those reports back into the verdict's evidence.
#
# The rule that governs this whole section: **the verdict is the exit
# code and nothing else.**  A missing report, an unreadable report and a
# malformed report all leave the verdict exactly where it was; they only
# decide what the evidence says.  A report that cannot be parsed is a
# broken report, not a broken gate — so it is recorded and the run goes
# on, exactly like the audit copy in :func:`_write_artifact`.

#: The junit file name carries the phase it belongs to.
_JUNIT_PHASE_RE = re.compile(r"^phase-(\d+)\.xml$")


def _junit_phase_number(path: Path) -> Optional[int]:
    """The phase a junit report belongs to (``phase-2.xml`` → ``2``).

    ``None`` for a file whose name does not follow the convention — the
    report is then not ours and is not read.
    """
    match = _JUNIT_PHASE_RE.match(path.name)
    return int(match.group(1)) if match else None


def _junit_node_id(case: ET.Element) -> str:
    """The node id a junit ``<testcase>`` stands for.

    The junit format does not carry pytest's node id — it carries the
    *class* part of it as a dotted path (``backend.tests.unit.test_foo``)
    and the test name beside it, so the id has to be rebuilt from what
    the format does carry:

    * a ``file`` attribute (the xunit1 family, and other producers) is
      the file path and is used verbatim;
    * otherwise the dotted class name is turned back into a path, and
      the components that name a *class* — they start with an upper-case
      letter — are kept as node-id segments instead of being folded
      into the file path.

    So a module-level test comes back as the id pytest itself prints
    (``backend/tests/unit/test_foo.py::test_bar``), and a class-based
    test keeps its class segment (``...::TestBar::test_baz``).  A class
    whose name does not follow the upper-case convention cannot be told
    apart from a package by the format itself, and comes back as part of
    the file path — the phase it failed in and the case's name are still
    exact.
    """
    name = (case.get("name") or "").strip()
    classname = (case.get("classname") or "").strip()
    file_attr = (case.get("file") or "").strip()

    if file_attr:
        module_dotted = file_attr.replace("\\", "/").replace("/", ".")
        if module_dotted.endswith(".py"):
            module_dotted = module_dotted[: -len(".py")]
        class_part = ""
        if classname.startswith(module_dotted):
            class_part = classname[len(module_dotted):].strip(".")
        segments = [file_attr]
        if class_part:
            segments.append(class_part)
        if name:
            segments.append(name)
        return "::".join(segments)

    if not classname:
        return name

    parts = classname.split(".")
    class_parts: List[str] = []
    while len(parts) > 1 and parts[-1][:1].isupper():
        class_parts.insert(0, parts.pop())
    segments = ["/".join(parts) + ".py"] + class_parts
    if name:
        segments.append(name)
    return "::".join(segments)


def _junit_first_line(node: ET.Element) -> str:
    """The first line of a failure's message, truncated.

    The ``message`` attribute is what pytest puts there — the exception's
    own message (``assert 41 == 42``), which is the line that says
    *what* broke.  The element's text is the whole traceback, and its
    first non-blank line is the source line; it is the fallback for a
    report that carries no message.
    """
    for source in (node.get("message") or "", node.text or ""):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:MAX_JUNIT_MESSAGE_CHARS]
    return ""


def _junit_count(suite: ET.Element, attribute: str, fallback: int) -> int:
    """One count off a ``<testsuite>``, counted from its cases if absent.

    The attribute is what pytest wrote, and it is the authority; the
    count from the cases is the fallback for a report that does not
    carry it (and for one whose attribute is not a number).
    """
    raw = suite.get(attribute)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _read_junit_evidence(junit_dir: Optional[Path]) -> Dict[str, Any]:
    """The per-phase junit evidence a gate run left behind.

    Best-effort by construction: this function never raises.  A missing
    report, an unreadable one or a malformed one is recorded and the
    evidence comes back empty — the verdict has already been decided by
    the exit code, and a report that cannot be parsed must not be able
    to disturb it.
    """
    evidence: Dict[str, Any] = {"junit_phases": [], "junit_failures": []}
    if junit_dir is None:
        return evidence

    try:
        reports = sorted(
            path for path in junit_dir.glob("phase-*.xml")
            if _junit_phase_number(path) is not None
        )
    except OSError as exc:  # noqa: BLE001 - evidence must not break a verdict
        evidence["junit_error"] = f"{type(exc).__name__}: {exc}"
        return evidence

    phases: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    errors: List[str] = []

    for path in reports:
        phase = _junit_phase_number(path)
        # The report is not foreign input: this process created the
        # directory it lives in for this very run
        # (:data:`GATE_JUNIT_DIR_ENV`), and the pytest that writes
        # ``phase-<n>.xml`` into it is the one the gate script
        # (``scripts/run_tests.sh``) started on the repository's own
        # test suite.  The gate therefore already runs code that can
        # execute arbitrary commands on this machine, so parsing the
        # report it wrote grants no capability that was not there
        # before.
        #
        # ``defusedxml`` is deliberately not introduced here: it is not
        # a declared dependency of this repository, and adding a
        # dependency for the sake of one static-scan finding is out of
        # scope for this change.
        try:
            root = ET.parse(path).getroot()  # nosec B314 - the gate's own pytest wrote it, see above
        except Exception as exc:  # noqa: BLE001 - a broken report is not a verdict
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue

        suites = (
            root.findall("testsuite") if root.tag == "testsuites" else [root]
        )
        tests = suite_failures = suite_errors = suite_skipped = 0
        for suite in suites:
            cases = suite.findall("testcase")
            tests += _junit_count(suite, "tests", len(cases))
            suite_failures += _junit_count(
                suite, "failures",
                sum(1 for case in cases if case.find("failure") is not None),
            )
            suite_errors += _junit_count(
                suite, "errors",
                sum(1 for case in cases if case.find("error") is not None),
            )
            suite_skipped += _junit_count(
                suite, "skipped",
                sum(1 for case in cases if case.find("skipped") is not None),
            )
            for case in cases:
                node_id = _junit_node_id(case)
                for kind in ("failure", "error"):
                    for node in case.findall(kind):
                        failures.append({
                            "phase": phase,
                            "node_id": node_id,
                            "kind": kind,
                            "first_line": _junit_first_line(node),
                        })

        phases.append({
            "phase": phase,
            "tests": tests,
            "failures": suite_failures,
            "errors": suite_errors,
            "skipped": suite_skipped,
        })

    if len(failures) > MAX_JUNIT_FAILURES:
        failures = failures[:MAX_JUNIT_FAILURES]
        evidence["junit_failures_truncated"] = True

    evidence["junit_phases"] = phases
    evidence["junit_failures"] = failures
    if errors:
        evidence["junit_error"] = "; ".join(errors)
    return evidence


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
    project_dir = Path(project_dir)
    timeout = ci_timeout(vp, project_dir=project_dir, entry=entry)
    declared_budget = gate_budget_seconds(project_dir, entry)

    started = time.monotonic()
    exit_code: Optional[int] = None
    stdout = stderr = ""
    timed_out = False
    error = ""
    gate_summary: Dict[str, Any] = {}
    reaped: List[Dict[str, Any]] = []

    # The registry of the processes this run starts.  It is named in the
    # gate's environment, so the service, the reaper daemon the service
    # starts and the harness that spawns a server all write themselves
    # into the same file — and the runner can give them back when the
    # gate is gone.
    registry_dir, registry = _spawn_registry()
    gate_env = ci_subprocess_env()
    gate_env["PDT_SPAWN_REGISTRY"] = str(registry)

    # The junit reports of the phases land next to the audit copy of this
    # verdict — the same directory ``_write_artifact`` uses — so the two
    # pieces of evidence sit together and the report names the phase it
    # belongs to.  The gate environment already carries the spawn
    # registry above; the junit directory joins it on the same dict, so
    # the one subprocess sees both.
    junit_dir: Optional[Path] = None
    if artifact_dir is not None:
        candidate = Path(artifact_dir) / vp_id
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: BLE001 - evidence must not break a verdict
            # The directory could not be created, so pytest would fail to
            # write its report — and a gate that fails because its
            # *report* could not be written is a verdict about the
            # reporting, not about the code. Run without it.
            logger.warning(
                "[ci_runner] could not create the junit directory %s: %s",
                candidate, exc,
            )
        else:
            junit_dir = candidate
            gate_env[GATE_JUNIT_DIR_ENV] = str(candidate)

    try:
        try:
            completed = run_bounded(
                entry,
                cwd=str(project_dir),
                text=True,
                timeout=timeout,
                env=gate_env,
            )
            exit_code = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            gate_summary = parse_gate_summary(stdout)
            error = _timeout_reason(
                timeout,
                time.monotonic() - started,
                gate_summary,
                declared_budget,
            )
        except Exception as exc:  # noqa: BLE001 - a broken gate is FAILED
            error = f"{type(exc).__name__}: {exc}"
    finally:
        # The gate may have been killed before its own cleanup could
        # run — and a service is detached, so it outlives the gate that
        # started it.  The runner is the process that outlives the
        # gate, so the runner is the process that gives it back.
        reaped = _reap_spawned(registry, reason="the CI gate ended")
        shutil.rmtree(registry_dir, ignore_errors=True)

    duration = time.monotonic() - started
    stdout_tail = stdout[-MAX_OUTPUT_CHARS:]
    stderr_tail = stderr[-MAX_OUTPUT_CHARS:]
    if not gate_summary:
        gate_summary = parse_gate_summary(stdout)

    # The gate's own account of itself, lifted to the top level of the
    # evidence so a reader does not have to dig into ``gate_summary``
    # for it.  ``gate_elapsed_seconds`` is what the gate reported; a
    # gate that reported nothing falls back to the time the runner
    # measured, which is the same number for a gate that ran to its end.
    exhausted_in = str(gate_summary.get("budget_exhausted_in") or "")
    gate_verdict = str(gate_summary.get("verdict") or "")
    gate_elapsed = gate_summary.get("elapsed_seconds")
    if gate_elapsed is None:
        gate_elapsed = int(duration)

    evidence: Dict[str, Any] = {
        "vp_id": vp_id,
        "ci_entry": entry,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 2),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "ci_timeout_seconds": timeout,
        "gate_budget_seconds": declared_budget,
        "gate_summary": gate_summary,
        "spawned_processes_reaped": reaped,
        "budget_exhausted_in": exhausted_in,
        "gate_elapsed_seconds": gate_elapsed,
    }
    if error:
        evidence["error"] = error

    # The junit evidence is added to the evidence and never decides the
    # verdict: the exit code alone decides PASSED / FAILED.  The reports
    # are read after the gate ran, and a report that cannot be parsed
    # changes nothing here — it only changes what the evidence says.
    evidence.update(_read_junit_evidence(junit_dir))

    if not timed_out and not error and exit_code == 0:
        result.status = STATUS_PASSED
        result.reasons = [
            f"[full_ci] `{entry}` 退出码 0（用时 {duration:.0f}s）"
        ]
    elif not timed_out and not error and _gate_could_not_decide(
        exit_code, gate_summary,
    ):
        # The gate could not decide.  That is not a verdict about the
        # code, so it is not a FAILED verdict: a FAILED verdict is what
        # the repair loop turns into a fix task, and a machine that ran
        # out of time has not proven a deliverable defective.
        result.status = STATUS_INCONCLUSIVE
        result.reasons = [
            f"[full_ci] `{entry}` "
            + _inconclusive_reason(
                exit_code=exit_code,
                gate_verdict=gate_verdict,
                exhausted_in=exhausted_in,
                summary=gate_summary,
                declared_budget=declared_budget,
                elapsed=gate_elapsed,
                duration=evidence["duration_seconds"],
            )
        ]
        tail = stderr_tail.strip() or stdout_tail.strip()
        if tail:
            result.reasons.append(f"输出尾部：{tail[-600:]}")
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


def _gate_could_not_decide(
    exit_code: Optional[int],
    summary: Dict[str, Any],
) -> bool:
    """Did the gate stop without saying anything about the code?

    The gate's own summary is the evidence, and it is the only evidence
    that fits: a run that could not decide says so.  Three ways it says
    it:

    * it names the phase its budget ran out in
      (``gate budget exhausted in: …``);
    * it declares itself INCONCLUSIVE (``gate: INCONCLUSIVE (exit 3)``)
      — which stands on its own even when no phase could be parsed;
    * it exits 3 — the code the gate uses for a run that could not
      decide (``scripts/run_tests.sh`` uses 0 for a pass, 1 for a failed
      phase, 3 for a run that could not decide).

    A run whose summary says nothing at all is not a gate that spoke:
    there is no declaration to read, and the historical FAILED verdict
    stands.
    """
    if not _summary_has_content(summary):
        return False
    if str(summary.get("budget_exhausted_in") or ""):
        return True
    if exit_code == 3:
        return True
    return str(summary.get("verdict") or "").upper() == STATUS_INCONCLUSIVE


def _summary_has_content(summary: Dict[str, Any]) -> bool:
    """Did the gate print anything the runner could read back?"""
    if not summary:
        return False
    return any(value not in (None, "", 0) for value in summary.values())


def _inconclusive_reason(
    *,
    exit_code: Optional[int],
    gate_verdict: str,
    exhausted_in: str,
    summary: Dict[str, Any],
    declared_budget: Optional[int],
    elapsed: int,
    duration: float,
) -> str:
    """Why the run is inconclusive — in words that answer questions.

    "退出码 3" is a true sentence that answers no question: not which
    phase ran out of budget, not what the gate had budgeted for itself,
    not how long it had been running.  The reason carries all three,
    because the gate's own summary carries them and a verdict that
    drops them sends the reader to dig for a summary that was there
    all along.
    """
    budget = summary.get("budget_seconds") or declared_budget
    budget_text = f"{budget}s" if budget else "未声明"
    if exhausted_in:
        return (
            f"退出码 {exit_code}：门禁在 {exhausted_in} 用尽了它自己声明的 "
            f"{budget_text} 预算（预算用尽，不是代码失败；"
            f"budget_exhausted_in={exhausted_in}，duration={duration}s，"
            f"门禁自报用时 {elapsed}s）"
        )
    verdict_text = f"门禁自报 {gate_verdict}；" if gate_verdict else ""
    return (
        f"退出码 {exit_code}：门禁没能给出结论（{verdict_text}"
        f"预算用尽或主机不可测量，不是代码失败；duration={duration}s，"
        f"门禁自报用时 {elapsed}s）"
    )


def _timeout_reason(
    timeout: int,
    elapsed: float,
    summary: Dict[str, Any],
    declared_budget: Optional[int],
) -> str:
    """Why the watchdog fired — in words that answer questions.

    "超时：3600s 内没有跑完" was true and useless: it did not say
    whether the gate had run out of *its own* budget (the machine was
    too slow) or had been killed from the outside (something went wrong
    between the gate and the watchdog), it did not name the phase that
    was running, and it did not say how long the gate had been running
    or what it had budgeted for itself.

    The verdict has to carry those four things, because the round-2
    artifact carried none of them.
    """
    budget = summary.get("budget_seconds") or declared_budget
    budget_text = f"{budget}s" if budget else "未声明"
    phase = summary.get("running_phase") or "未知阶段"
    elapsed_seconds = summary.get("elapsed_seconds")
    if elapsed_seconds is None:
        elapsed_seconds = int(elapsed)
    exhausted_in = summary.get("budget_exhausted_in") or ""
    if exhausted_in:
        return (
            f"超时：门禁在 {exhausted_in} 用尽了它自己声明的 {budget_text} 预算"
            f"（预算用尽，不是被外部杀掉；看门狗给的是 {timeout}s，已用 "
            f"{elapsed_seconds}s）"
        )
    return (
        f"超时：{timeout}s 内没有跑完，门禁被外部杀掉（看门狗开的枪；"
        f"已用 {elapsed_seconds}s；被杀的时候正在跑 {phase}；"
        f"门禁声明的预算是 {budget_text}）"
    )


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
