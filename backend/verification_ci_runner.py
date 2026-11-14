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
      "ci_timeout_seconds": 7200,
      "expected_result": "整仓门禁与全部测试通过，退出码 0"
    }

判定
----
``ci_entry`` 退出 0 → PASSED；门禁自己的 summary 说它「判不了」（退出 3，
或预算用尽）→ INCONCLUSIVE；其余非 0 / 超时 / 入口不可执行 → FAILED，并把
stdout/stderr 的**头尾**附在 verdict 的 evidence 里、原始输出落盘
``plans/<id>/vp_artifacts/<vp_id>/ci_output.json``。**只有这一条判据。**

INCONCLUSIVE 不是「过了」，也不是「代码坏了」—— 门禁没有判，它只说明这次
运行没跑完。把它写成 FAILED 会让修复循环去改一个没被证明有缺陷的交付物。

一次**什么也没说**的退出 3（门禁没有打印 summary）仍然是 FAILED：没有
声明可读，历史判定不动 —— 两侧对这一条的判断一致。

e2e 的两个子类（2026-11-04，arch 决策点 7）
-------------------------------------------
``e2e`` 以前只有一种意思：「真开浏览器」。于是本地 CLI / 服务级的端到端
也得起一个 LLM 子会话去驱动一个它根本没有的浏览器 —— 判定落在子 agent 的
自报上，而自报不可复跑。现在按 ``e2e_kind`` 分流：

* ``e2e_kind: "command"`` —— 声明一条 ``entry_command``，**由框架直接
  执行**，判定是退出码的纯函数（与 ``full_ci`` 同一档，不起 LLM）：

  ::

      {
        "id": "VP-009",
        "verification_method": "e2e",
        "e2e_kind": "command",
        "entry_command": "python -m pytest tests/e2e/test_cli.py -q"
      }

* ``e2e_kind: "browser"``（**缺省**）—— 保留现状：由子 agent 驱动真实
  浏览器。**框架判不了它**，所以 :func:`validate_vp` 对它返回 issue，
  上层据此 ESCALATE 回 LLM 子 agent —— 浏览器走查本来就该在那儿。

两类的**判定依据**是同一件事：一条命令的退出码。所以它们共用这一个 runner
和同一份裁决契约，差别只在**谁来判**：command 类由框架判，browser 类由
子 agent 判。缺省是 browser，是为了让拆分之前写的计划（``target_url``
走浏览器）行为一个字没变。

整仓门禁必须由项目适配脚本执行（2026-11-05，arch 决策点 2）
-------------------------------------------------------
一条 ``ci_entry`` 必须是**对项目适配脚本的一次调用**，不能是内联复合命令
（``(test -e backend/.venv) && bash scripts/run_tests.sh``）。复合命令把
「跑不跑」「跑哪一段」「失败了算谁的」全写进了入口 —— 于是同一条门禁在不同
机器上跑的是**不同的东西**，退出码也不再是「整仓门禁」这一条判据。

这条规则是**规划期**的规则：:func:`validate_vp` 缺省就报 issue（生成阶段
退回重写）。**执行期不启用** —— 历史计划里的复合入口不重写，执行时仍然要
能跑，所以 :func:`run_ci_verification` 与分派器的 ``_runner_issues`` 都传
``require_single_entry=False``。

三态（2026-10-31，VP-020）
--------------------------
本仓的整仓门禁 ``scripts/run_tests.sh`` 有**三个**退出码，不是两个：

==================  ==========================================  ==========
退出码              含义                                        signal
==================  ==========================================  ==========
0                   绿                                          无
1                   某条 lane 红（代码的问题）                    ``gate_failed``
3                   机器答不了（宿主不可测，不是代码失败）          ``gate_inconclusive``
无（被预算杀掉）      没跑完                                      ``timeout``
==================  ==========================================  ==========

框架以前把「非 0」一律读成「交付物坏了」，于是**机器的问题被记成了交付
缺陷**：它被计入同因、升级成人，最后变成一份针对没坏过的东西的修复任务。
现在退出码与超时各自带一个机器可读的 signal，交给
:mod:`verification.failure_triage` 按规则分类 —— ``gate_inconclusive`` 与
``timeout`` 是 ``infra``（重试，不计入交付），``gate_failed`` 才是
``deliverable_defect``。

证据（2026-10-31，VP-020）
--------------------------
以前只保留 stdout 的**末尾** 4000 字符。被预算杀掉的那一刻，末尾是第二条
lane 的进度条，而**已经跑完**的第一条 lane 的汇总（``1 failed, 10530 passed
… in 822.48s``）早就滚出了这 4000 字符 —— 于是「已跑完的 lane 是红的」在
证据里读起来像「没发现失败」。现在 evidence 同时保留：

* ``stdout_head`` / ``stderr_head`` —— 开头 4000 字符；
* ``lane_summaries`` —— 承载各 lane 结论的行（pytest 汇总、``phase N/3``、
  ``gate:``、``host timing probe:``、``host measurability:``、失败的用例）；
* ``timeout_seconds`` / ``budget_exceeded`` —— 实际生效的预算，以及它是否
  被这个预算杀掉。

被杀的运行因此仍然能回答「哪几条 lane 跑完了、结论是什么」。

per-phase junit（2026-10-31）
--------------------------------
截尾的输出只留最后 :data:`MAX_OUTPUT_CHARS` 个字符，而门禁是三段串行跑的
—— 段 1 的失败在段 3 结束时早就被截掉了。所以门禁脚本在
:data:`GATE_JUNIT_DIR_ENV` 指向一个目录时，会**每段写一份 junit XML**
（``phase-<n>.xml``）；跑完后本模块把这份证据读回 verdict：

* ``junit_phases`` —— 每段的用例数 / 失败数 / 错误数 / 跳过数；
* ``junit_failures`` —— 每条失败的 node id 与首行消息。

这两条字段**只是证据**：判定仍然只看退出码。XML 缺失 → 两个字段为空；
XML 解析不了 → 记 ``junit_error`` 并继续，绝不抛异常、绝不影响判定。

证据必须是**本轮写的**（2026-11-08）
------------------------------------
一个 VP 的产物目录是**跨轮复用**的（审计副本 ``ci_output.json`` 也住在里面），
所以上一轮写的 ``phase-<n>.xml`` 在下一轮开始时还在那儿。它不是本轮的证据 ——
当成是，就会把「本轮根本没跑过的用例」算成失败，修复轮于是去修一个不存在的
缺陷（2026-11-08 那一轮就是这么修的：修完照样红，因为那些用例一次都没跑过）。

所以门禁启动**之前**先把目录里旧的 ``phase-<n>.xml`` 清掉；门禁跑完之后目录里
剩下的，就是本轮写的。

另一条路：本轮**一张都没有**。门禁把整仓的跑派到了别处（例如派给一个 CI
runner）时，报告留在那边，本机自然没有。这种情况下 evidence 记
``junit_available: False`` 并说明原因，**不去读**本机上的残留 ——
「本轮拿不到」和「本轮没有失败」是两件事。

这个判据是**观测**，不是猜的：框架不认识项目自己的门禁脚本叫什么，也不该
认识（入口由项目决定，框架不得硬编码项目脚本名）。「这次有没有产出报告」是
门禁跑完看一眼目录就知道的事实 —— 目录在跑之前已经清空过，所以里面剩下的
就是本轮写的。

取证副本按轮次另存（2026-11-09）
--------------------------------
一个 VP 的产物目录是**跨轮复用**的，所以 ``ci_output.json`` 里留下的永远
是**最后一轮**的结论 —— 后一轮把它覆盖掉，前一轮的取证就没了。于是同一束
证据自相矛盾：产物文件里记的是后一轮的退出码，同目录的 gate 日志里记的
是前一轮的结论，谁也说不清自己引用的是哪一轮。

所以每一次运行除了 ``ci_output.json``（路径与语义一个字不改，**仍然是
full_ci 的唯一判据**）还另存一份 ``ci_output.round-<n>.json``：

* 副本用**独占创建**落地（``os.link``），后一轮因此**不可能**覆盖前一轮
  已经留下的副本；
* 轮次号写进**副本的**载荷（``round`` / ``round_source``），所以「这份产物属于哪
  一轮」是可以核对的，而不是从文件名猜；``ci_output.json`` 本身**只带契约
  字段** —— 写入器不替调用方加键；
* 轮次由**调用方**给出（``run_ci_verification(..., round_no=…)`` —— agent
  知道自己是第几轮），取不到时退回计划的 context pack，再取不到就**不写
  副本** —— 不猜。

副本写不出来**不改变判决**：判决在写副本之前就定了，取证是事后追认的
东西，不是判定的输入。

两套证据互不越界
----------------
``stdout_head`` / ``stderr_head`` 与 ``stdout_tail`` / ``stderr_tail`` 是
**截断**出来的文本，``lane_summaries`` 是从文本里**筛**出来的结论行，
``junit_phases`` / ``junit_failures`` 是**读** XML 读出来的结构化证据。
它们并列写在同一份 evidence 里，但**没有一条参与判定**：verdict 只由
退出码（及其 signal）决定，junit 读不出来只记 ``junit_error``。

轮次副本（2026-11-09）
---------------------
VP 的证据目录是**跨轮复用**的：``<plan>/vp_artifacts/<vp_id>/`` 在第 1 轮
和第 2 轮是同一个目录。所以 runner 在第 2 轮写 ``ci_output.json`` 时，
**覆盖**掉了第 1 轮写的那一份 —— 而「第 1 轮的门禁当时说了什么」正是
两轮结论不一致时唯一能查的东西。

所以每轮写**两个**文件：

* ``ci_output.json`` —— runner 写出的产物本身。**不含**轮次字段：契约里
  没有 ``round``，产物就不能有。
* ``ci_output.round-<n>.json`` —— 同一份证据，外加 ``round``（第几轮）与
  ``round_source``（轮次号从哪来：调用方 ``parameter``，还是上下文包
  ``context_pack``）。

轮次号优先取调用方给的正整数；没给（或给了 0 / 负数）就从
``<plan>/context/round-<n>.json`` 里取**最大**的 n（``<plan>`` =
``Path(artifact_dir).resolve().parent``）；两处都没有就**不写副本** ——
「第 0 轮」不是一轮。

副本用**硬链接**落地而不是 replace：链接不会落在已经被占用的名字上，
所以前一轮的副本**原样保留**（这是副本存在的全部理由）。副本写失败
**不改判决** —— 判决在写副本之前就定了，磁盘的问题不是代码的问题；
``.tmp`` 一定回收（残留对孤儿扫描是不可见的，只会跨轮悄悄堆积）。

契约从**自己脚下**解析（2026-11-10）
----------------------------------
产物契约 (``ci_artifact``) 由 runner 在写产物之前校验。它**不是**平铺
import —— 解析建立在 **runner 自己所在目录**上。

原因在加载器：``verification_ci_runner_loader`` 加载的是**被测树那份**
runner（从它的路径加载，名字是自己的），而 ``sys.path`` 上只有 **harness 的**
``backend/``。平铺 import 于是向 harness 要一个它未必带着的模块 ——
2026-11-09 的整仓门禁就是这么判的：被测树那份 runner 加载不了，回落成
harness 自己那份（``fallback_reason="ModuleNotFoundError: No module named
'ci_artifact'"``），分支上的交付物**一次都没被执行过**。

所以「跟 runner 一起发布的那份契约」就是 runner 用的那份。找不到就报错，
不静默降级成「不校验」。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from bounded_subprocess import run_bounded
from credentials import _FD_ENV_PREFIX as _SECRET_FD_ENV_PREFIX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The contract for the CI artifact — resolved next to this file
# ---------------------------------------------------------------------------
#
# ``ci_artifact`` is the one copy of the contract for ``ci_output.json``
# (see :mod:`ci_artifact`), and the runner checks its payload against it
# before writing it down.
#
# It is resolved from **this file's own directory**, not by a flat import.
# The reason is the loader: ``verification_ci_runner_loader`` loads the
# **tree under test's** runner from its path, under a name of its own, and
# puts only the **harness'** ``backend/`` on ``sys.path``.  A flat
# ``from ci_artifact import ...`` therefore asks the harness for a module it
# need not carry — and 2026-11-09's whole-repository gate judged a round
# with the **harness'** runner, because the tree under test's runner could
# not be loaded at all:
# ``fallback_reason="ModuleNotFoundError: No module named 'ci_artifact'"``.
# The contract that ships with the runner is the contract the runner uses.


def _load_ci_artifact_validator() -> Callable[..., List[str]]:
    """The validator, taken from the file that ships next to this one.

    The module is loaded under a name of its own (the digest convention
    ``verification_agent._load_service_manager`` uses) and is **not** kept
    in ``sys.modules`` afterwards: a long-lived process must not judge a
    later round by a contract that has changed on disk since.

    Never degrades.  When the file is not next to the runner either, the
    flat import is tried once — and if that fails too, the ImportError
    propagates and the round does not judge at all.  A validator that
    cannot be found has to stop the round: a round that quietly stopped
    checking its artifact is worse than one that says it cannot.
    """
    module_path = Path(__file__).resolve().parent / "ci_artifact.py"
    if module_path.is_file():
        digest = hashlib.sha1(
            str(module_path).encode("utf-8"),
        ).hexdigest()[:12]
        module_name = f"pdt_ci_artifact_{digest}"
        spec = importlib.util.spec_from_file_location(
            module_name, str(module_path),
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {module_path}")
        module = importlib.util.module_from_spec(spec)
        # Registered while it executes, so a module that looks itself up
        # by ``__name__`` finds itself; removed again afterwards, so the
        # registry keeps describing the modules that are really there.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
        validator = getattr(module, "validate_ci_artifact", None)
        if not callable(validator):
            raise ImportError(
                f"{module_path} does not offer validate_ci_artifact",
            )
        return validator

    from ci_artifact import validate_ci_artifact  # noqa: PLC0415

    return validate_ci_artifact


validate_ci_artifact = _load_ci_artifact_validator()

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

#: The verification method that has **two sub-classes** (2026-11-04，arch
#: 决策点 7 —— e2e 与 UI 解耦）。
#:
#: ``e2e`` 以前只有一种意思：「真开浏览器」。于是本地 CLI / 服务级的端到端
#: 也得起一个 LLM 子会话去驱动一个它根本没有的浏览器，判定落在子 agent 的
#: 自报上。拆成两类之后：
#:
#: * :data:`E2E_KIND_BROWSER` —— 保留现状：子 agent 驱动真实浏览器。
#: * :data:`E2E_KIND_COMMAND` —— 声明一条入口命令，**框架直接执行**，
#:   判定是退出码的纯函数（与 ``full_ci`` 同一档，不起 LLM）。
E2E_METHOD = "e2e"

#: The field that names which sub-class an ``e2e`` VP is.
E2E_KIND_FIELD = "e2e_kind"

#: The browser sub-class —— the sub-agent's job, not the framework's.
E2E_KIND_BROWSER = "browser"

#: The command sub-class —— the framework runs the entry and reads the exit code.
E2E_KIND_COMMAND = "command"

#: The field a ``command`` sub-class declares its entry in.
ENTRY_COMMAND_FIELD = "entry_command"

#: The directory the per-round context packs live in, under the plan's
#: own directory (``<plan>/context/round-<n>.json``).
#:
#: The pack is written by :mod:`context_pack` and read by every
#: sub-agent of the round; it is the one file that says which round is
#: running.  The runner asks it for the round number when its caller
#: does not know it.
CONTEXT_PACK_DIRNAME = "context"

#: The file name one round's context pack has.  A file that does not
#: match is not a pack: it is somebody else's file.
_CONTEXT_PACK_FILENAME_RE = re.compile(r"^round-(\d+)\.json$")

#: The round number came from the caller.
ROUND_SOURCE_PARAMETER = "parameter"

#: The round number came from the plan's context pack.
ROUND_SOURCE_CONTEXT_PACK = "context_pack"

#: The round number came from the payload's own ``round`` key — what a
#: caller that already knew the round wrote into the evidence.
ROUND_SOURCE_PAYLOAD = "payload"

#: Ceiling for a full CI gate. A whole-repo nightly run is measured in
#: tens of minutes (a whole workspace build plus the full test suite), so
#: the default is deliberately far
#: above the Phase-1 evidence-command ceiling (600s). It is still a hard
#: cap: a hung gate is killed and reported, not left to eat the round.
#:
#: 2026-10-31 (VP-020): 3600 was **smaller than the gate's own cost**.
#: The three lanes measured 3457.67 s in total (unit 478.27 s, e2e
#: 2348.75 s, wallclock 630.65 s) — a margin of 142 s, 3.9 % — and the
#: host's load moves that number: the unit lane alone measured 822.48 s
#: on a busier host, so the whole gate passed 3600 s and was killed by
#: this budget **before it had finished**. The default is therefore
#: 7200 s, roughly twice the measured cost.
DEFAULT_CI_TIMEOUT_SECONDS = 7200

#: The floor a whole-repo gate's budget may not fall below.
#:
#: A plan declares its own ``ci_timeout_seconds``, but the plan cannot be
#: corrected from the outside: ``verification_plan_delta`` only knows
#: ``add`` / ``obsolete`` / ``replacement_for``, so a budget that was
#: written too small stays too small. The runner therefore reads a
#: declared budget as a **floor, not a ceiling** for ``full_ci`` — the
#: declared value is honoured when it is larger, and raised to this
#: floor when it is smaller. Other verification methods are untouched:
#: a 120 s api_test is not a whole-repo gate.
MIN_FULL_CI_TIMEOUT_SECONDS = 7200

#:
#: 2026-10-31: this default is the **fallback** for the case where
#: neither the gate nor the VP declares a budget.  When the gate
#: declares one of its own (``GATE_BUDGET_SECONDS_*`` in
#: ``scripts/run_tests.sh``), that declaration is the truth and the
#: watchdog is derived from it — see :func:`ci_timeout`.
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

#: The gate's own account of the host it ran on.  The gate measures the
#: host before it runs anything, and says what it found on this line;
#: a runner that does not read it throws away the one fact that
#: separates "the code is broken" from "the machine could not keep
#: time".
_GATE_HOST_MEASURABLE_LINE_RE = re.compile(
    r"^host measurability:\s*(.+?)\s*$", re.MULTILINE
)

#: Output kept per stream in the verdict evidence — head **and** tail.
MAX_OUTPUT_CHARS = 4000

#: The exit code the repository's gate uses for "the run could not
#: decide" (the host could not be measured, or a phase called itself
#: inconclusive). It is **not** a code failure, and it is not the same
#: thing as exit 1.
GATE_INCONCLUSIVE_EXIT_CODE = 3

#: The machine-readable signals this runner emits. They are the names
#: :mod:`verification.failure_triage` knows, so the four-class triage
#: decides them by rule instead of guessing from the text.
SIGNAL_GATE_FAILED = "gate_failed"
SIGNAL_GATE_INCONCLUSIVE = "gate_inconclusive"
SIGNAL_TIMEOUT = "timeout"
SIGNAL_GATE_NOT_EXECUTABLE = "gate_not_executable"

#: The lines that carry a lane's conclusion. A gate run that is killed
#: mid-way still has to answer "which lanes finished, and what did they
#: say" — and the answer is in these lines, not in the tail of the
#: progress bar.
#:
#: The shapes come from the two producers: pytest's own summary banner
#: (``= 1 failed, 10530 passed, … in 822.48s (0:13:42) =``) and the
#: gate script's summary block (``phase 1/3 (parallel): passed``,
#: ``gate: FAIL (exit 1) — a phase failed``). The failing cases are
#: included too: "which test failed" is the first question anybody asks
#: of a red gate.
_LANE_SUMMARY_RE = re.compile(
    r"^(?:=+\s.*\s=+$"                        # pytest's summary banner
    r"|\d+\s+(?:failed|passed|error|errors|xfailed|xpassed|skipped"
    r"|deselected)\b.*\bin\s+[\d.]+s"        # pytest's summary, undecorated
    r"|phase\s+\d+/\d+"                       # the gate's per-phase verdict
    r"|gate:"                                # the gate's own verdict
    r"|host (?:measurability|timing probe):"  # the host's own measurement
    r"|(?:FAILED|ERROR)\s+\S+"               # a failing case (short summary)
    r"|\S+::\S+.*\b(?:FAILED|ERROR)\b)"      # a failing case (verbose mode)
)

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

#: The name of the per-round copy of the audit artifact.
#:
#: The artifact directory of a VP is reused across rounds, so the live
#: audit copy (``ci_output.json``) holds the verdict of the **newest**
#: round, and the forensics of an earlier round are gone the moment the
#: next round writes.  A conclusion that quotes the file therefore
#: quotes whichever round wrote it last -- which is how a report ended
#: up claiming one exit code while the artifact next to it held
#: another.  The copy is named after the round, so the round a claim
#: belongs to is in the file name.
ROUND_ARTIFACT_TEMPLATE = "ci_output.round-{round_no}.json"

#: The directory of a plan's context packs, relative to the plan
#: directory.  One pack per round; the newest one names the round a
#: run belongs to.
CONTEXT_PACK_DIRNAME = "context"

#: How long the runner waits for a killed gate's owner to be *observed*
#: gone before it judges who may give a registered process back.
#:
#: The gate is killed from the outside, and the processes it started are
#: written down against *it* as their owner.  While that owner is still
#: there the reaper leaves them alone -- correctly: an owner that is
#: still there may want the process.  The runner is the process that
#: outlives the gate, so the runner is the process that waits for that
#: window to close.  The wait is bounded, because an owner that is
#: still there after it is somebody who is *not* going away.
REAP_OWNER_TIMEOUT_SECONDS = 5.0

#: How long one wait between two sweeps sleeps.
REAP_POLL_SECONDS = 0.25

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

#: Shell grouping punctuation: ``(`` opens a group, ``)`` closes one.
#:
#: A bracket is **not** part of the token it is glued to: ``(test`` is the
#: ``test`` builtin, and ``backend/.venv)`` is the path ``backend/.venv``
#: with the group's closing bracket stuck to it.  Reading the bracket as
#: part of the path made the pre-flight look up a path that can never
#: exist — and it hid the ``cd`` of ``(cd backend && …)``, so the paths
#: after it were resolved against the wrong directory (2026-11-05).
_GROUPING_PREFIX = "("
_GROUPING_SUFFIX = ")"

#: Commands whose operands *create* a path.
#:
#: ``ln`` creates its last operand, ``mkdir`` / ``touch`` create all of
#: theirs, ``cp`` / ``mv`` create their last one (the destination).  An
#: entry is allowed to build the very path it goes on to read — that is
#: how a fresh worktree bootstraps its own virtualenv
#: (``ln -sfn … backend/.venv``), and the pre-flight must not call such
#: an entry "it reads a path that does not exist".
#:
#: Only the *producer* position is exempt.  The consumed side of the
#: same command (``cp missing.txt out.txt``) still has to exist.
#:
#: ``install`` is here too (2026-11-01, 本分支的 VP-018 自建路径修复):
#: ``install -m 755 src dst`` creates ``dst`` exactly like ``cp`` does.
_PATH_CREATING_COMMANDS = {
    "ln": "last",
    "mkdir": "all",
    "touch": "all",
    "cp": "last",
    "mv": "last",
    "install": "last",
}

#: An environment assignment in front of a command (``FOO=bar cmd``).
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Shell 操作符 —— 一条 ``ci_entry`` 里出现任何一个，说明它是**内联复合命令**
#: （自己拼出来的命令链），而不是"对项目适配脚本的一次调用"。
#:
#: 2026-11-05（arch 决策点 2 —— VP 校验）：整仓门禁必须由**项目适配脚本**
#: 执行。内联复合命令（``(test -e backend/.venv) && bash scripts/run_tests.sh``）
#: 把「跑不跑」「跑哪一段」「失败了算谁的」全写进了 ``ci_entry`` —— 于是同一条
#: 门禁在不同机器上跑的是**不同的东西**，退出码也不再是「整仓门禁」这一条判据。
#:
#: 重定向（``2>&1``）同样算：它把 shell 的语义也塞进了入口。
_COMPOUND_ENTRY_OPERATORS = ("&&", "||", ";", "(", ")", "|", ">", "<")


@dataclass
class SchemaIssue:
    """One reason a ``full_ci`` VP cannot be executed as declared."""

    vp_id: str
    detail: str
    #: The machine-written **rule** that fired — the identity of the
    #: defect.  ``detail`` is the human-readable explanation; this is
    #: the part that stays the same when the explanation is reworded.
    code: str = ""
    #: The machine-written **subject** of the rule: the offending path,
    #: the narrowed target.  Empty when the rule has no subject.
    subject: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "vp_id": self.vp_id,
            "detail": self.detail,
            "code": self.code,
            "subject": self.subject,
        }

    def failure_id(self) -> str:
        """The machine-written identity of this defect.

        The failure identity has to be the same whenever the defect is
        the same — including when the round's report words it
        differently.  So it is built from the **rule** and its
        **subject**, never from ``detail``: the detail is prose, and
        prose is what the report rewords from round to round.

        On 2026-11-05 that difference cost a whole round: the same
        schema rejection produced three different failure fingerprints
        (``a61e387a…`` / ``be0a4e42…`` / ``db929c18…``) because the
        fingerprint hashed the report's first line of evidence, and the
        report reworded it every round.  Three narratives, one defect,
        two false "the root cause changed" signals.
        """
        code = str(self.code or "").strip() or "unknown"
        subject = str(self.subject or "").strip()
        if subject:
            return f"schema:{code}:{subject}"
        return f"schema:{code}"


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


def compound_operators(entry: str) -> List[str]:
    """The shell operators ``entry`` contains, in a fixed order.

    An empty list means "one command" — the shape a ``ci_entry`` is
    supposed to have: **one call to the project's adapter script**.

    A non-empty list means the entry is an **inline compound command**:
    it decides for itself what to run, in what order, and what a failure
    means.  See :data:`_COMPOUND_ENTRY_OPERATORS` for why that is not
    an acceptable whole-repo gate.
    """
    text = str(entry or "")
    found: List[str] = []
    for op in _COMPOUND_ENTRY_OPERATORS:
        # ``||`` already covers ``|`` and ``&&`` already covers ``&`` — a
        # redundant entry in the message is noise, not information.
        if op in text and not any(op in seen for seen in found):
            found.append(op)
    return found


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
      ``cd backend`` is ``backend/state_machine/tests``);
    * the shell's grouping brackets are punctuation, not part of the
      token they are glued to (``(test`` → ``test``,
      ``backend/.venv)`` → ``backend/.venv``).

    The flag / absolute-path / URL skip rules are unchanged, and the
    existence check in :func:`validate_vp` is not relaxed: a path that
    cannot be found is still an issue — unless the entry itself creates
    it (see :func:`_missing_referenced_paths`).
    """
    referenced, _created = _collect_entry_paths(entry)
    return referenced


def _created_paths(entry: str) -> Set[str]:
    """Repo-relative paths ``entry`` brings into being itself.

    The counterpart of :func:`_referenced_paths`: the paths this entry
    **creates** (``ln -s src dst`` creates ``dst``) rather than merely
    reads.  Such a path is not an input the entry depends on, so the
    existence check in :func:`validate_vp` does not apply to it.

    Both halves are read from the same scan (:func:`_collect_entry_paths`),
    so the two views can never disagree about where a command ends.
    """
    _referenced, created = _collect_entry_paths(entry)
    return set(created)


def _split_command_text(text: str) -> List[str]:
    """Split one command line into tokens, tolerating unbalanced quotes."""
    try:
        return shlex.split(text)
    except ValueError:
        # Unbalanced quotes — the shell will decide at run time.
        return text.split()


def _normalize_token(token: str) -> str:
    """Strip the shell punctuation that is not part of a token's meaning.

    * the command separators (``&&`` / ``||`` / ``;`` / ``|``) at the end;
    * the grouping brackets at either end (``(`` … ``)``).

    A token that is *only* punctuation normalises to the empty string,
    which the reader takes as "a separator".
    """
    text = token.rstrip(_COMMAND_SEPARATORS)
    if not text:
        return ""
    return text.lstrip(_GROUPING_PREFIX).rstrip(_GROUPING_SUFFIX)


def _token_ends_command(token: str) -> bool:
    """Did this token carry the separator that ends its command?

    ``backend/.venv;`` — the path and the separator are **one** token, so
    the command ends there and the next token starts a new one.  A
    separator that stands on its own (``&&`` / ``||`` / ``;`` …) is a
    token of its own and is read as such; this is about the glued form.

    Without this, the operand scan of the command *before* the
    separator runs on into the next command and reads its words as its
    own operands — ``ln -s src dst; bash run.sh`` would make ``run.sh``
    the destination ``ln`` creates.
    """
    return len(token.rstrip(_COMMAND_SEPARATORS)) != len(token)


def _shell_command_payload(tokens: List[str], index: int) -> Optional[int]:
    """Index of the command string a shell invocation carries, if any.

    ``bash -c 'cd backend && …'`` — the payload is a whole command line,
    so it has to be split again before the paths inside it can be read.

    The flag has to belong to *this* invocation: the scan stops at the
    first word that is not a flag, so the ``-c`` of a *later* command in
    the chain is never mistaken for this one's payload.
    """
    if os.path.basename(_normalize_token(tokens[index])) not in _SHELL_BINARIES:
        return None
    offset = index + 1
    while offset < len(tokens):
        flag = _normalize_token(tokens[offset])
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


def _command_operands(tokens: List[str], start: int) -> List[str]:
    """The non-option operands of the command that starts at ``start``.

    The scan stops at the first separator: the operands belong to *this*
    command only.  ``--`` ends the option parsing — everything after it
    is an operand.

    A separator **glued** to the end of a token (``backend/.venv;``)
    ends the command too: that token is the command's last operand, and
    the words after it belong to the next command.  Without this, ``ln
    -s src dst; bash run.sh`` would make ``run.sh`` the destination
    ``ln`` creates — and the created path would be wrong.
    """
    operands: List[str] = []
    options_done = False
    for token in tokens[start:]:
        text = _normalize_token(token)
        if not text:
            # A separator (``&&`` / ``||`` / ``;`` …) or a lone bracket
            # ends this command.
            break
        ends = _token_ends_command(token)
        if not options_done:
            if text == "--":
                options_done = True
                if ends:
                    break
                continue
            if text.startswith("-"):
                if ends:
                    break
                continue
        operands.append(text)
        if ends:
            break
    return operands


def _collect_created_paths(
    command: str,
    tokens: List[str],
    start: int,
    base: str,
    created: List[str],
) -> None:
    """Record the paths the command at ``start`` creates.

    ``ln`` creates its last operand, ``mkdir`` / ``touch`` create all of
    theirs, ``cp`` / ``mv`` create their last one (the destination).
    """
    rule = _PATH_CREATING_COMMANDS.get(os.path.basename(command))
    if rule is None:
        return
    operands = _command_operands(tokens, start)
    if not operands:
        return
    targets = operands if rule == "all" else operands[-1:]
    for target in targets:
        created.append(_join_base(base, target))


def _collect_referenced_paths(
    tokens: List[str],
    base: str,
    created: Optional[List[str]] = None,
) -> List[str]:
    """Repo-relative paths named by ``tokens``, given the current ``base``.

    ``base`` is the directory the commands run in — relative to the
    repository root (``""`` = the root itself), or absolute when a
    ``cd`` named an absolute directory.

    ``created`` collects the paths the command line *creates* itself
    (``ln -sfn … backend/.venv``); the caller exempts them from the
    existence check, because an entry that builds the path it then reads
    is runnable by definition.
    """
    out: List[str] = []
    if created is None:
        created = []
    index = 0
    at_command_start = True

    while index < len(tokens):
        raw = tokens[index]
        token = _normalize_token(raw)
        if not token:
            # A separator (``&&`` / ``||`` / ``;`` …): the next token
            # starts a new command. A lone ``)`` lands here too — it is
            # a grouping symbol, not a path.
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
                _split_command_text(tokens[payload_index]), base, created,
            ))
            index = payload_index + 1
            at_command_start = False
            continue

        if at_command_start and token == "cd" and index + 1 < len(tokens):
            target_raw = tokens[index + 1]
            base = _resolve_cd_target(base, _normalize_token(target_raw))
            index += 2
            # ``cd backend;`` — the command ends with its target, so the
            # next token starts a new command.
            at_command_start = _token_ends_command(target_raw)
            continue

        if at_command_start:
            # The command word itself — it may be a producer (``ln`` …),
            # in which case its operand is a path this entry creates.
            _collect_created_paths(token, tokens, index + 1, base, created)

        if _looks_like_path(token):
            out.append(_join_base(base, token))

        # A token that carries the separator that ends its command
        # (``backend/.venv;``) is the last token of that command: the
        # next one starts a new one.
        at_command_start = _token_ends_command(raw)
        index += 1

    return out


def _collect_entry_paths(entry: str) -> Tuple[List[str], List[str]]:
    """The paths an entry reads, and the paths it creates itself."""
    created: List[str] = []
    referenced = _collect_referenced_paths(
        _split_command_text(entry), "", created,
    )
    return (
        [canonical_repo_path(path) for path in referenced],
        [canonical_repo_path(path) for path in created],
    )


def canonical_repo_path(rel: str) -> str:
    """The canonical spelling of a repo-relative path.

    ``./scripts/missing.sh`` and ``scripts/missing.sh`` name the **same**
    file.  The failure identity is built from the offending path, so
    leaving the spelling in would make the identity a property of the
    report's wording — and a reworded report would read as "the root
    cause evolved", which is the false signal this whole chain exists to
    kill (2026-11-05, VP-015).

    The canonical form is the one the repository itself uses: no ``./``
    prefix, no redundant separators, no ``..`` segments.  An absolute
    path keeps its shape — the rule never rewrites what it cannot
    resolve.

    The shell punctuation that **surrounds** a path in a command line is
    not part of the path either: ``backend/.venv)`` — the ``)`` closes
    the grouping the entry opened — names the same file as
    ``backend/.venv``.  The runner reads that punctuation off while it
    tokenizes; a path that arrives from **elsewhere** (an artifact an
    older runner wrote, a report that quoted the raw command line) can
    still carry it, and the identity must not depend on it.
    """
    text = _normalize_token(str(rel or "").strip())
    if not text:
        return text
    return posixpath.normpath(text)


def _missing_referenced_paths(entry: str, root: Path) -> List[str]:
    """The paths ``entry`` reads that do not exist under ``root``.

    A path the entry *creates* itself is not missing — the entry builds
    it before it reads it (``(test -e backend/.venv || ln -sfn … backend/.venv)``).
    Every other referenced path still has to exist: the exemption is for
    the producer position only, never for the consumed one.
    """
    referenced, created = _collect_entry_paths(entry)
    created_paths: Set[str] = set(created)
    missing: List[str] = []
    seen: Set[str] = set()
    for rel in referenced:
        if rel in seen:
            continue
        seen.add(rel)
        if rel in created_paths:
            continue
        if not (root / rel).exists():
            missing.append(rel)
    return missing


def e2e_kind(vp: Any) -> str:
    """Which sub-class is this ``e2e`` verification point?

    ``browser`` unless the VP says ``command``.  The default is what
    keeps the plans that were written before the split working the way
    they did: an ``e2e`` point that names a ``target_url`` is a browser
    flow, and a browser flow is the sub-agent's job.

    An unknown value is read as ``browser`` too: the framework cannot
    judge a browser flow, so the safe reading is the one that sends the
    point back to the sub-agent that can.
    """
    if not isinstance(vp, dict):
        return E2E_KIND_BROWSER
    kind = str(vp.get(E2E_KIND_FIELD) or "").strip().lower()
    if kind == E2E_KIND_COMMAND:
        return E2E_KIND_COMMAND
    return E2E_KIND_BROWSER


def _validate_e2e_vp(
    vp: Dict[str, Any], *, vp_id: str, project_dir: Optional[Path],
) -> List[SchemaIssue]:
    """The schema rules of the two ``e2e`` sub-classes.

    * **command** —— the framework runs the entry and grades the exit
      code.  The entry may name a single test file: that is what an
      end-to-end point *is*, so the anti-narrowing check does not apply
      here (it exists to stop a whole-repo gate from decaying into one
      test case).
    * **browser** —— the sub-agent drives the real browser.  **The
      framework cannot judge it**, so this returns an issue and the
      caller escalates back to the LLM path — which is exactly where a
      browser flow belongs.
    """
    if e2e_kind(vp) == E2E_KIND_COMMAND:
        return _validate_command_e2e_vp(
            vp, vp_id=vp_id, project_dir=project_dir,
        )
    return [SchemaIssue(
        vp_id,
        f"e2e({E2E_KIND_BROWSER}) 由子 agent 驱动真实浏览器，框架判不了："
        f"要由框架直接执行就声明 `e2e_kind: \"{E2E_KIND_COMMAND}\"` "
        f"和一条 `{ENTRY_COMMAND_FIELD}`。",
        code="e2e_browser_unsupported",
    )]


def _validate_command_e2e_vp(
    vp: Dict[str, Any], *, vp_id: str, project_dir: Optional[Path],
) -> List[SchemaIssue]:
    """The ``command`` sub-class: one entry command, graded on its exit code."""
    issues: List[SchemaIssue] = []
    entry = vp.get(ENTRY_COMMAND_FIELD)
    if not isinstance(entry, str) or not entry.strip():
        issues.append(SchemaIssue(
            vp_id,
            f"e2e({E2E_KIND_COMMAND}) 要求一条非空的 "
            f"'{ENTRY_COMMAND_FIELD}' 字符串 —— "
            f"判定依据就是这条入口的退出码",
            code="e2e_command_missing",
        ))
        return issues

    if project_dir is not None:
        root = Path(project_dir)
        for rel in _missing_referenced_paths(entry.strip(), root):
            issues.append(SchemaIssue(
                vp_id,
                f"{ENTRY_COMMAND_FIELD} 引用的路径不存在：{rel}",
                code="e2e_command_path_missing",
                subject=rel,
            ))

    return issues


def validate_vp(
    vp: Any,
    *,
    project_dir: Optional[Path] = None,
    require_single_entry: bool = True,
) -> List[SchemaIssue]:
    """Return every schema problem that makes this VP unrunnable.

    Called at plan-generation time (so a malformed ``full_ci`` plan is
    regenerated rather than run) **and** again by
    :func:`run_ci_verification` (so a hand-edited plan fails loudly at
    execution time instead of verifying nothing).

    ``project_dir`` switches on the existence check: a gate whose entry
    names a repo script that does not exist cannot pass, and finding that
    out at plan time is far cheaper than finding it out after a 40-minute
    round.

    ``require_single_entry`` is the **形态契约**（2026-11-05，arch 决策点 2）：
    一条 ``ci_entry`` 必须是对项目适配脚本的**一次调用**，不能是内联复合
    命令（``(test -e backend/.venv) && bash scripts/run_tests.sh``）。它
    缺省打开，所以规划期就会把不合形态的入口退回重写；**执行期显式关掉**
    （``require_single_entry=False``）—— 历史计划不重写，复合入口在执行时
    仍然要能跑。两个执行入口（:func:`run_ci_verification` 与分派器的
    ``_runner_issues``）都传的是关掉的那一个。

    **The two ``e2e`` sub-classes are judged by different rules** (the
    dispatch is on ``verification_method``).  See :func:`_validate_e2e_vp`
    for why a browser flow is an *issue* here rather than something this
    runner grades.
    """
    issues: List[SchemaIssue] = []
    if not isinstance(vp, dict):
        return [SchemaIssue(
            "unknown", f"VP is {type(vp).__name__}, expected an object",
            code="vp_not_an_object",
        )]
    vp_id = str(vp.get("id", "unknown"))

    if str(vp.get("verification_method") or "").strip() == E2E_METHOD:
        return _validate_e2e_vp(vp, vp_id=vp_id, project_dir=project_dir)

    entry = vp.get(CI_ENTRY_FIELD)
    if not isinstance(entry, str) or not entry.strip():
        issues.append(SchemaIssue(
            vp_id,
            f"full_ci requires a non-empty '{CI_ENTRY_FIELD}' string — "
            f"整仓门禁的判定依据就是这条入口的退出码",
            code="ci_entry_missing",
        ))
        return issues

    if require_single_entry:
        operators = compound_operators(entry)
        if operators:
            # 身份必须是**机器写的**：形态违规只有一条「detail」散文时，
            # 指纹分不出「哪种操作符」这一类缺陷与「哪一条入口」这一条缺陷
            # —— 循环于是把两者混成一次「根因演进」。
            issues.append(SchemaIssue(
                vp_id,
                f"{CI_ENTRY_FIELD} 含 shell 操作符（{', '.join(operators)}）："
                f"整仓门禁必须由项目适配脚本执行，不能是内联复合命令",
                code="ci_entry_compound",
                subject=",".join(operators),
            ))
            return issues

    narrowed = looks_narrowed(entry)
    if narrowed:
        issues.append(SchemaIssue(
            vp_id,
            f"{CI_ENTRY_FIELD} 收窄了验证范围（{narrowed}）："
            f"full_ci 的职责是整仓门禁；收窄到用例级的验证属于 Phase 1，"
            f"而 Phase 1 不跑命令。",
            code="ci_entry_narrowed",
            subject=narrowed,
        ))

    if project_dir is not None:
        root = Path(project_dir)
        for rel in _missing_referenced_paths(entry.strip(), root):
            issues.append(SchemaIssue(
                vp_id,
                f"{CI_ENTRY_FIELD} 引用的路径不存在：{rel}",
                code="ci_entry_path_missing",
                subject=rel,
            ))

    return issues


def _entry_of(vp: Dict[str, Any]) -> str:
    """The command this runner will run for ``vp``.

    ``full_ci`` runs ``ci_entry``; an ``e2e`` command point runs
    ``entry_command``.  The two are the same kind of thing — a command
    whose exit code *is* the verdict — so they share one runner and one
    verdict contract.
    """
    if str(vp.get("verification_method") or "").strip() == E2E_METHOD:
        return str(vp.get(ENTRY_COMMAND_FIELD) or "").strip()
    return str(vp.get(CI_ENTRY_FIELD) or "").strip()


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
    given, the time it spent, the phase that ran out of budget, the
    verdict it reached — and **whether the host could measure
    wall-clock time at all** (``host_measurable``).

    The last one is a three-valued fact, not a boolean: ``True`` the
    host could measure, ``False`` it could not, ``None`` the gate
    never measured (or never said).  ``None`` is not ``False``: a run
    that said nothing about the host has not declared the host
    untrustworthy, and the historical verdict stands.

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
        "host_measurable": None,
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
    measurable = _GATE_HOST_MEASURABLE_LINE_RE.search(stdout)
    if measurable:
        summary["host_measurable"] = _host_measurability(measurable.group(1))
    return summary


def _host_measurability(note: str) -> Optional[bool]:
    """Read the gate's host-measurability note into a three-valued fact.

    The gate writes one of three notes: ``OK (…)``, ``the host cannot
    measure wall-clock (…)`` or ``not measured (…)``.  Only the first
    two are declarations; the third says the gate never looked, which
    is not the same as "the host is untrustworthy".
    """
    text = str(note or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("the host cannot measure"):
        return False
    if lowered.startswith("ok"):
        return True
    return None


def ci_timeout(
    vp: Dict[str, Any],
    *,
    project_dir: Optional[Path] = None,
    entry: Optional[str] = None,
) -> int:
    """The watchdog's budget for this VP.

    The watchdog is **never shorter than the gate it watches**.  When the
    gate declares a budget of its own, the watchdog gets that budget
    plus a margin — a VP that declares a shorter timeout does not get
    one, because a watchdog shorter than the gate is the failure this
    whole mechanism exists to prevent: the gate is killed from the
    outside and reports nothing.

    When the gate declares nothing, the VP's own number stands: the gate
    has said that it needs no floor, and the historical default stands
    for a VP that declares nothing either.

    There is one case left — **there is no project to ask**.  A caller
    that does not say where the project is has told the runner nothing
    about what the gate costs, and a whole-repo gate then keeps the
    universal floor :data:`MIN_FULL_CI_TIMEOUT_SECONDS`: the measured
    cost of this repo's gate was 3457.67 s, so a declared 3600 s left a
    3.9 % margin, and the host's load took it away (the unit lane alone
    measured 822.48 s on a busier host).  A budget that is smaller than
    the thing it bounds kills the gate before the gate has finished,
    and the framework then has no verdict to report.

    The floor is a floor, not a new ceiling: a plan that declares
    *more* than the floor keeps its own number.
    """
    declared = _declared_ci_timeout(vp)
    value = DEFAULT_CI_TIMEOUT_SECONDS if declared is None else declared
    budget = gate_budget_seconds(project_dir, entry)
    if budget is not None:
        # The gate declares its own budget: the watchdog is that budget
        # plus a margin, never shorter than the gate.
        return max(value, budget + GATE_BUDGET_MARGIN_SECONDS)
    if project_dir is None and str(
        vp.get("verification_method") or ""
    ).strip().lower() == "full_ci":
        # Nothing is known about the gate: the measured cost is the floor.
        value = max(value, MIN_FULL_CI_TIMEOUT_SECONDS)
    return value


def _declared_ci_timeout(vp: Dict[str, Any]) -> Optional[int]:
    """The timeout the VP itself declares, or ``None`` when it declares none."""

    raw = vp.get("ci_timeout_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        # A budget that cannot be read is no budget: the caller
        # falls back to the default, or to the gate's own declaration.
        return None
    return max(1, value)


def lane_summaries(stdout: str) -> List[str]:
    """The lines of ``stdout`` that carry a lane's conclusion.

    Kept in order, and kept whole: a killed run has to be able to say
    which lanes finished and what each of them concluded.  The order is
    the order the gate printed them in, and no line is dropped — the
    point of this list is to be the summary the tail cannot be.
    """
    out: List[str] = []
    for raw in str(stdout or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if _LANE_SUMMARY_RE.search(line):
            out.append(line)
    return out


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


def _resolution_of(module_path: Path, project_dir: Any) -> str:
    """Is this module the tree under test's own copy, or the harness'?

    The module knows which file it is, and it is told which tree it is
    judging — so it can answer that itself.  The answer has to travel
    **with the verdict**, because the verdict is what outlives the
    round: the artifact a reader finds on disk has to say not only
    "this file judged" but also "that file was the tree under test's
    own".

    ``""`` when the question cannot be asked (no project, no readable
    path): an unanswerable question is not a wrong answer.
    """
    try:
        expected = (
            Path(project_dir) / "backend" / "verification_ci_runner.py"
        ).resolve()
    except (OSError, TypeError):
        return ""
    try:
        from verification_ci_runner_loader import (
            RESOLUTION_HARNESS,
            RESOLUTION_PROJECT,
        )
    except Exception:  # noqa: BLE001 — the loader is optional here
        project, harness = "project", "harness"
    else:
        project, harness = RESOLUTION_PROJECT, RESOLUTION_HARNESS
    return project if module_path == expected else harness


def verifier_source(project_dir: Any = None) -> Dict[str, str]:
    """Which file is judging — this module's own path, hash and HEAD.

    2026-11-05: a round could not answer "was the code that judged me the
    code I fixed?".  The failure was byte-for-byte the same in two
    rounds while the file on disk was already fixed — because the round
    ran *another* copy of this module.  So every verdict now carries the
    answer, and this function is where the answer comes from.

    The path is this module's own file, resolved: the module is loaded
    from wherever the tree under test keeps it, and that is exactly the
    file that has to be named.

    ``project_dir`` is optional: without it the module cannot say which
    copy it is, and it says so rather than guessing.  The loader knows
    the answer anyway — it is the one that chose the file — so it
    reports its own resolution alongside this one.
    """
    module_path = Path(__file__).resolve()
    source = {
        "module_path": str(module_path),
        "sha256": "",
        "git_head": "",
        "resolution": "",
    }
    if project_dir is not None:
        source["resolution"] = _resolution_of(module_path, project_dir)
    try:
        from verification_ci_runner_loader import file_sha256, git_head_at

        source["sha256"] = file_sha256(module_path)
        source["git_head"] = git_head_at(module_path)
    except Exception:  # noqa: BLE001 — provenance never fails a round
        logger.debug(
            "[ci_runner] could not compute the provenance of %s",
            module_path, exc_info=True,
        )
    return source


def shared_venv_for(project_dir: Any) -> Optional[str]:
    """The venv a gate in ``project_dir`` may reuse when it has none.

    ``scripts/run_tests.sh`` hard-requires ``$PROJECT_ROOT/backend/.venv``.
    A tree under test that has none — a fresh worktree is the usual
    case — therefore needs one, and the cheapest deterministic answer is
    the harness' own: **offline**, and the same versions the harness
    itself runs.

    The tree under test's own venv wins: if it has one, that is the one
    its lockfile pinned.
    """
    candidates = []
    try:
        candidates.append(Path(project_dir) / "backend" / ".venv")
    except TypeError:
        pass
    try:
        from verification_ci_runner_loader import harness_shared_venv

        shared = harness_shared_venv()
        if shared:
            candidates.append(Path(shared))
    except Exception:  # noqa: BLE001 — a hint, never a failure
        logger.debug(
            "[ci_runner] no shared venv available", exc_info=True,
        )
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return str(candidate)
        except OSError:
            continue
    return None


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


def _reap_spawned(
    registry: Path, *, reason: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Give back the processes the registry names.  Never raises.

    Returns the entries that were given back and the entries that are
    still registered.  The second list is the proof that the run did
    not leave a service behind: an empty one means every process this
    run started is gone.

    A cleanup path that raises would take the verdict down with it, and
    the verdict is the thing this module exists to produce.
    """
    try:
        import orphan_reaper
    except Exception:  # pragma: no cover - the module is stdlib-only
        return [], []
    given_back: List[Dict[str, Any]] = []
    deadline = time.monotonic() + REAP_OWNER_TIMEOUT_SECONDS
    while True:
        try:
            given_back.extend(
                orphan_reaper.reap_spawned_processes(registry, reason=reason)
            )
        except Exception as exc:  # noqa: BLE001 - a cleanup never fails a verdict
            logger.warning(
                "[verification_ci_runner] could not give the spawned "
                "processes back: %s", exc,
            )
            break
        outstanding = _registered_processes(registry)
        if not outstanding:
            break
        if time.monotonic() >= deadline:
            logger.warning(
                "[verification_ci_runner] %d registered process(es) are "
                "still there after %.1fs; their owner(s) are still alive",
                len(outstanding), REAP_OWNER_TIMEOUT_SECONDS,
            )
            break
        # The gate is killed from the outside, and for a moment after
        # the kill the processes it started are still attributed to a
        # process that is on its way out.  A sweep that runs in that
        # window leaves them alone -- correctly, while that owner is
        # still there -- and nobody else is coming to give them back.
        # The runner is the process that outlives the gate, so the
        # runner is the process that waits for that window to close and
        # sweeps again.
        time.sleep(REAP_POLL_SECONDS)
    return given_back, _registered_processes(registry)


def _registered_processes(registry: Path) -> List[Dict[str, Any]]:
    """The processes the registry still names.  Never raises.

    An empty list is the answer the verdict needs: everything this run
    started has been given back.
    """
    try:
        import orphan_reaper
    except Exception:  # pragma: no cover - the module is stdlib-only
        return []
    try:
        return list(orphan_reaper.read_spawn_registry(registry))
    except Exception as exc:  # noqa: BLE001 - a cleanup never fails a verdict
        logger.warning(
            "[verification_ci_runner] could not read the spawn registry "
            "%s: %s", registry, exc,
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


def _clear_stale_junit_reports(junit_dir: Optional[Path]) -> List[str]:
    """Take the reports of an earlier round out of the junit directory.

    The directory of a VP is reused across rounds -- it holds this VP's
    audit copy as well -- so the reports the last round wrote are still
    there when the next round starts.  A report that belongs to an
    earlier round is not evidence about this one: read as if it were,
    it names cases this round never ran, and the repair loop then goes
    and fixes a failure that is not there (2026-11-08: a whole repair
    round was built out of reports six days old, and the gate it was
    meant to fix was green).

    So the reports go before the gate runs.  What is in the directory
    afterwards was written by this run.
    """
    removed: List[str] = []
    if junit_dir is None:
        return removed
    try:
        reports = list(junit_dir.glob("phase-*.xml"))
    except OSError as exc:  # noqa: BLE001 - evidence must not break a verdict
        logger.warning(
            "[ci_runner] could not list the stale junit reports in %s: %s",
            junit_dir, exc,
        )
        return removed
    for path in reports:
        if _junit_phase_number(path) is None:
            # Not one of ours by name: it is not read either, and it is
            # not ours to delete.
            continue
        try:
            path.unlink()
        except OSError as exc:  # noqa: BLE001 - a stale report is not fatal
            logger.warning(
                "[ci_runner] could not remove the stale junit report %s: %s",
                path, exc,
            )
            continue
        removed.append(path.name)
    return removed


def _read_junit_evidence(
    junit_dir: Optional[Path],
) -> Dict[str, Any]:
    """The per-phase junit evidence a gate run left behind.

    Best-effort by construction: this function never raises.  A missing
    report, an unreadable one or a malformed one is recorded and the
    evidence comes back empty — the verdict has already been decided by
    the exit code, and a report that cannot be parsed must not be able
    to disturb it.

    Whether the reports are **available** is an observed fact, not a
    guess about the entry: the directory was emptied before the gate
    started (:func:`_clear_stale_junit_reports`), so what is in it
    afterwards is what this run wrote.  Reports in it → available; no
    reports in it → not available, with a reason.  A gate that ran the
    whole-repo gate somewhere else leaves its reports there, and this
    machine has none — reading whatever it happens to be carrying would
    attribute another round's failures to this one, so the evidence says
    "not available this run" instead.

    The framework does not know what the project's gate scripts are
    called: the entry is the project's to choose, and a name the
    framework recognises would be a name it would also mis-recognise
    somewhere else.
    """
    evidence: Dict[str, Any] = {
        "junit_phases": [],
        "junit_failures": [],
        # 2026-11-06: the machine-written failing set is present even
        # when there is no junit report to read — the key has to mean
        # the same thing on every path, or its absence reads as "the
        # machine did not say".
        "failed_cases": [],
        # Whether this run produced a junit report at all.
        # "the reports are not there" and "the reports were never going
        # to be here" are different facts, and a reader has to be able
        # to tell them apart: the first is a gap in the run, the second
        # is a property of the gate that was run.
        #
        # Spelled out rather than named by a constant (2026-11-10): the
        # contract's field set is **derived from this source** — the
        # allowed keys are read out of these evidence dictionaries — so
        # a key the derivation cannot see is a key the contract can
        # never allow.
        "junit_available": True,
    }
    if junit_dir is None:
        evidence["junit_available"] = False
        evidence["junit_unavailable_reason"] = (
            "the junit directory could not be created, so this run had "
            "nowhere to write a junit report"
        )
        return evidence

    try:
        reports = sorted(
            path for path in junit_dir.glob("phase-*.xml")
            if _junit_phase_number(path) is not None
        )
    except OSError as exc:  # noqa: BLE001 - evidence must not break a verdict
        evidence["junit_error"] = f"{type(exc).__name__}: {exc}"
        return evidence

    if not reports:
        # Nothing in the directory the run started from an empty one:
        # this run wrote no report.  A gate that ran the whole-repo
        # gate elsewhere leaves its reports there, and this machine has
        # none.
        evidence["junit_available"] = False
        evidence["junit_unavailable_reason"] = (
            "this run produced no junit report on this machine; a gate "
            "that runs elsewhere leaves its reports there"
        )
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
    # 2026-11-06: the machine-written identity of the failing set, under
    # the same key the schema path uses.  One key, one meaning: "which
    # cases failed, as the machine says it".  The fingerprint reads it
    # instead of the report's wording, so re-wording a round's report
    # can no longer look like a different defect.
    evidence["failed_cases"] = sorted({
        str(failure.get("node_id") or "").strip()
        for failure in failures
        if str(failure.get("node_id") or "").strip()
    })
    if errors:
        evidence["junit_error"] = "; ".join(errors)
    return evidence


# ---------------------------------------------------------------------------
# The wall-clock shape of a failure
# ---------------------------------------------------------------------------
#
# One rule, two users.  The gate script asks it before it decides
# whether a failed phase is the code's fault or the machine's, and this
# module asks it again when the gate's own verdict says "inconclusive" —
# because a gate's exit code is not the only witness, and a phase that
# contains one deterministic failure is a code failure on any host.
#
# The rule is about the *shape* of the message, not about the case that
# produced it: the messages are written by the cases themselves, so the
# shape is what is left when the case's own name is gone.
#
# A wall-clock shape says the machine ran out of time:
#
#   * a service that was not ready in time ("not ready after 90s");
#   * a run that did not settle in time ("did not settle within 120.0s");
#   * a subprocess the clock killed ("timed out after 120 seconds");
#   * a budget that was exceeded ("took 2441.8 ms (budget 200 ms)");
#   * a wall-clock bound that was missed ("to finish in < 2s, got 5.94s").
#
# Everything else is the code's: an assertion about behaviour, a wrong
# value, a refused start.  Those stay failures on every host, and a
# rule that treated them as the machine's would hide real regressions.

#: The shapes that mean "the machine ran out of time".
_WALL_CLOCK_SHAPE_PATTERNS = (
    # a service that did not answer in time
    re.compile(r"\bnot ready (?:after|within)\b", re.IGNORECASE),
    re.compile(r"\bdid not become ready within\b", re.IGNORECASE),
    # a run that did not settle in time
    re.compile(r"\bdid not settle within\b", re.IGNORECASE),
    re.compile(r"\bdid not (?:finish|complete) within\b", re.IGNORECASE),
    # a subprocess the clock killed
    re.compile(r"\btimed out\b", re.IGNORECASE),
    re.compile(r"\btimeout", re.IGNORECASE),
    # a budget that was exceeded
    re.compile(r"\btook\b.*\bbudget\b", re.IGNORECASE),
    re.compile(r"\bwithin (?:the |its )?budget\b", re.IGNORECASE),
    re.compile(r"\bbudget (?:was )?exceeded\b", re.IGNORECASE),
    re.compile(r"\bwall-clock budget\b", re.IGNORECASE),
    # a wall-clock bound that was missed
    re.compile(r"\bfinish in\b.*\bgot\b", re.IGNORECASE),
    re.compile(r"\bexpected\b.*\bin <\s*[\d.]+s\b", re.IGNORECASE),
)


def is_wall_clock_shaped(message: str) -> bool:
    """Does this failure message describe a wall-clock failure?

    An empty message is **not** a wall-clock shape: a failure the
    evidence does not describe cannot be excused by the machine.
    """
    text = str(message or "")
    if not text.strip():
        return False
    return any(pattern.search(text) for pattern in _WALL_CLOCK_SHAPE_PATTERNS)


def _non_wall_clock_failures(
    junit_evidence: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """The failures in the evidence that are **not** wall-clock shapes.

    These are the failures that keep a phase — and a whole gate run —
    FAILED even on a host that could not measure wall-clock time.
    """
    failures = junit_evidence.get("junit_failures") or []
    return [
        failure for failure in failures
        if not is_wall_clock_shaped(str(failure.get("first_line") or ""))
    ]


def phase_report_evidence(report_path: str) -> str:
    """What one phase's junit report says about its failures.

    Returns ``"<shape>|<skipped>"``, where ``shape`` is one of:

    * ``no-evidence`` — the report is missing, unreadable, malformed, or
      names no failure at all.  Nothing in it says the failure was the
      machine's, so the failure is the code's;
    * ``wall-clock`` — every failure in the report is a wall-clock shape;
    * ``code`` — at least one failure is not a wall-clock shape.

    The gate script calls this to decide whether a failed phase is
    INCONCLUSIVE or FAILED, and the count of skipped cases comes back
    with it because a lane that skipped half its cases and failed the
    other half says something a reader needs to know.
    """
    try:
        root = ET.parse(Path(report_path)).getroot()  # nosec B314 - the gate's own pytest wrote it
    except Exception:  # noqa: BLE001 - a broken report names no failure
        return "no-evidence|?"
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    skipped = 0
    messages: List[str] = []
    for suite in suites:
        cases = suite.findall("testcase")
        skipped += sum(1 for case in cases if case.find("skipped") is not None)
        for case in cases:
            for kind in ("failure", "error"):
                for node in case.findall(kind):
                    messages.append(_junit_first_line(node))
    if not messages:
        return f"no-evidence|{skipped}"
    if all(is_wall_clock_shaped(message) for message in messages):
        return f"wall-clock|{skipped}"
    return f"code|{skipped}"


def run_ci_verification(
    vp: Dict[str, Any],
    *,
    project_dir: Path,
    artifact_dir: Optional[Path] = None,
    round_no: Optional[int] = None,
) -> CiRunResult:
    """Run one ``full_ci`` gate (or one ``e2e`` command entry) and grade it
    on the exit code.

    Deterministic end to end — same tree, same entry, same verdict, every
    round. Never raises: a broken entry is a FAILED verdict carrying the
    reason, not an exception the round has to survive.

    The two kinds share this runner because they are the same kind of
    thing: **a command whose exit code is the verdict**.  A ``full_ci`` VP
    runs its ``ci_entry``; an ``e2e`` command point runs its
    ``entry_command`` — and neither of them opens an LLM session.

    ``round_no`` names the round this run belongs to.  It is **evidence**,
    not a knob: the artifact directory of a VP is reused across rounds,
    so without it a later round silently replaces the forensic copy of an
    earlier one and nobody can say which round a conclusion is quoting
    (2026-11-08).  It is **not** part of the artifact the runner writes —
    the artifact stays exactly what the contract describes — but it names
    the **copy** the runner writes beside it
    (``ci_output.round-<n>.json``), because the evidence directory is
    reused across rounds and the next round would otherwise overwrite
    this round's evidence.  A caller that does not know its round gets it
    from the plan's context pack; see :func:`_resolve_round`.  ``None``
    means "the round is not known here" — the artifact then says so
    instead of inventing a number.
    """
    vp_id = str(vp.get("id", "unknown"))
    result = CiRunResult()

    issues = validate_vp(
        vp, project_dir=project_dir, require_single_entry=False,
    )
    if issues:
        result.reasons = [f"schema: {i.detail}" for i in issues]
        result.evidence = {
            "vp_id": vp_id,
            "schema_issues": [i.to_dict() for i in issues],
            # 2026-11-06: the machine-written identity of the defect.
            # This path never ran a single case, so there were no junit
            # node ids to name — and the failure identity fell back to
            # the report's **wording**, which is reworded every round.
            # That is what made the same rejection look like a new
            # root cause three rounds in a row (2026-11-05).
            "failed_cases": sorted({i.failure_id() for i in issues}),
            # 2026-11-05: this path used to carry no provenance at all
            # and wrote no artifact — so a round that never ran a single
            # case left **nothing** on disk, and the next round could
            # not tell "which file judged me" from "what did it say".
            "verifier_source": verifier_source(project_dir),
            # 2026-11-09: the round is **not** part of this evidence:
            # ``ci_output.json`` carries the contract fields only, and
            # the round this artifact belongs to is named by the
            # per-round copy (``ci_output.round-<n>.json``) that
            # :func:`_write_artifact` writes next to it.
        }
        _write_ci_artifacts(
            artifact_dir, vp_id, result.evidence, round_no=round_no,
        )
        return result

    entry = _entry_of(vp)
    # The label and the evidence key name the kind of entry that ran, so
    # a reader is never told a command point ran a whole-repo gate.
    is_e2e = str(vp.get("verification_method") or "").strip() == E2E_METHOD
    label = E2E_METHOD if is_e2e else "full_ci"
    entry_key = ENTRY_COMMAND_FIELD if is_e2e else CI_ENTRY_FIELD
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
    outstanding: List[Dict[str, Any]] = []

    # The registry of the processes this run starts.  It is named in the
    # gate's environment, so the service, the reaper daemon the service
    # starts and the harness that spawns a server all write themselves
    # into the same file — and the runner can give them back when the
    # gate is gone.
    registry_dir, registry = _spawn_registry()
    gate_env = ci_subprocess_env()
    gate_env["PDT_SPAWN_REGISTRY"] = str(registry)
    # 2026-11-05: the gate entry used to have to create the venv
    # itself, and the way it did that was a ``ln -sfn`` shell hack
    # pointing at another checkout — which is what the path preflight
    # then had to guess about ("which operand does this command
    # *produce*?").  The venv the gate may reuse is named here instead,
    # so a new entry can be the plain ``CI=true bash scripts/run_tests.sh``.
    shared_venv = shared_venv_for(project_dir)
    if shared_venv:
        gate_env["PDT_SHARED_VENV"] = shared_venv

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

    # The reports of an earlier round are still in that directory -- it
    # is reused across rounds -- and they are not evidence about this
    # run.  They go before the gate starts, so that whatever is in the
    # directory afterwards was written by this run.
    stale_reports = _clear_stale_junit_reports(junit_dir)

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
        reaped, outstanding = _reap_spawned(
            registry, reason="the CI gate ended",
        )
        shutil.rmtree(registry_dir, ignore_errors=True)

    duration = time.monotonic() - started
    stdout_head = stdout[:MAX_OUTPUT_CHARS]
    stdout_tail = stdout[-MAX_OUTPUT_CHARS:]
    stderr_head = stderr[:MAX_OUTPUT_CHARS]
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
        entry_key: entry,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 2),
        "timeout_seconds": timeout,
        "budget_exceeded": timed_out,
        "stdout_head": stdout_head,
        "stdout_tail": stdout_tail,
        "stderr_head": stderr_head,
        "stderr_tail": stderr_tail,
        "lane_summaries": lane_summaries(stdout),
        "ci_timeout_seconds": timeout,
        "gate_budget_seconds": declared_budget,
        "gate_summary": gate_summary,
        "spawned_processes_reaped": reaped,
        # 2026-11-08: the proof that this run left nothing behind.  The
        # entries that are still registered are the processes this run
        # started and could not give back -- an empty list says every
        # one of them is gone.
        "spawned_processes_outstanding": outstanding,
        "spawned_processes_all_given_back": not outstanding,
        "budget_exhausted_in": exhausted_in,
        "gate_elapsed_seconds": gate_elapsed,
        # 2026-11-05: the verdict says which file judged it — the same
        # question the schema-failure path answers, asked of the normal
        # path too.
        "verifier_source": verifier_source(project_dir),
        # 2026-11-10: which round this run belongs to is **not** a field
        # of this evidence.  The artifact carries the contract fields
        # only — a round label in here would make the artifact a copy of
        # itself — and the round is named by the per-round copy
        # (``ci_output.round-<n>.json``) that sits next to it.
    }
    if error:
        evidence["error"] = error

    # The junit evidence is added to the evidence, and it never decides
    # the verdict on its own: the exit code alone decides PASSED /
    # FAILED.  The reports are read after the gate ran, and a report
    # that cannot be parsed changes nothing here — it only changes what
    # the evidence says.
    #
    # There is one exception, and it is the exception the whole
    # host-measurability work is about: a gate that says "the host
    # could not be measured" is asking to be believed, and the only way
    # to check that claim is to read the failures the gate already
    # wrote down.  A failure that is not a wall-clock shape is the
    # code's, whatever the machine was doing — so the gate's exit code
    # is not allowed to be the only witness.
    junit_evidence = _read_junit_evidence(junit_dir)
    evidence.update(junit_evidence)
    if stale_reports:
        # The reports of an earlier round that were in the way.  They
        # are named so a reader can see that this run started from a
        # clean directory rather than from somebody else's evidence.
        evidence["junit_stale_reports_removed"] = sorted(stale_reports)
    # The host's own account of itself, lifted to the top level of the
    # evidence: an operator reading the artifact has to be able to see
    # *why* this machine's verdict is worth less than a quiet one's.
    evidence["host_measurable"] = gate_summary.get("host_measurable")

    could_not_decide = (
        not timed_out
        and not error
        and _gate_could_not_decide(exit_code, gate_summary)
    )
    blocking_failures = (
        _non_wall_clock_failures(junit_evidence) if could_not_decide else []
    )

    if not timed_out and not error and exit_code == 0:
        result.status = STATUS_PASSED
        result.reasons = [
            f"[{label}] `{entry}` 退出码 0（用时 {duration:.0f}s）"
        ]
    elif could_not_decide and not blocking_failures:
        # The gate could not decide.  That is not a verdict about the
        # code, so it is not a FAILED verdict: a FAILED verdict is what
        # the repair loop turns into a fix task, and a machine that ran
        # out of time has not proven a deliverable defective.
        result.status = STATUS_INCONCLUSIVE
        # 2026-11-05: the machine could not answer — the gate ran out of
        # its budget, said it could not decide, or said the host was not
        # measurable.  That is a fact about the *run*, and it says so in
        # the same machine-readable vocabulary the FAILED branch uses.
        # Without it the triage layer sees "no signal", which it reads
        # as "nothing was determined about the deliverable" and files as
        # a ``deliverable_defect`` — a repair task against a delivery
        # nobody has proven defective.  The verdict already said
        # INCONCLUSIVE; the evidence now says *why* in the words the
        # classifier knows: ``gate_inconclusive`` is an infrastructure
        # problem.
        evidence["signal"] = SIGNAL_GATE_INCONCLUSIVE
        result.reasons = [
            f"[{label}] `{entry}` "
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
        signal = ""
        if timed_out:
            detail = error
            signal = SIGNAL_TIMEOUT
        elif error:
            detail = f"入口无法执行：{error}"
            signal = SIGNAL_GATE_NOT_EXECUTABLE
        elif blocking_failures:
            # The gate said the machine could not be measured, and the
            # evidence says otherwise: at least one failure is not a
            # wall-clock shape, so it is the code's.  The reason names
            # the case and its message — "the host was busy" is not an
            # explanation a reader can check.
            sample = blocking_failures[0]
            detail = (
                f"退出码 {exit_code}（门禁说机器答不了，但失败里有不是墙钟"
                f"形状的：{sample.get('node_id') or '未知用例'} — "
                f"{sample.get('first_line') or '（无消息）'}）"
            )
            signal = SIGNAL_GATE_FAILED
        elif exit_code == GATE_INCONCLUSIVE_EXIT_CODE:
            detail = (
                f"退出码 {exit_code}（机器答不了：宿主不可测或某条 lane 自称"
                f"不确定，这不是代码失败）"
            )
            signal = SIGNAL_GATE_INCONCLUSIVE
        else:
            detail = f"退出码 {exit_code}（门禁报告某条 lane 红）"
            signal = SIGNAL_GATE_FAILED
        if signal:
            evidence["signal"] = signal
        result.reasons = [
            f"[{label}] `{entry}` {detail}（用时 {duration:.0f}s）"
        ]
        tail = stderr_tail.strip() or stdout_tail.strip()
        if tail:
            result.reasons.append(f"输出尾部：{tail[-600:]}")

    result.evidence = evidence
    _write_ci_artifacts(
        artifact_dir, vp_id, evidence, round_no=round_no,
    )
    return result


def _gate_could_not_decide(
    exit_code: Optional[int],
    summary: Dict[str, Any],
) -> bool:
    """Did the gate stop without saying anything about the code?

    The gate's own summary is the evidence, and it is the only evidence
    that fits: a run that could not decide says so.  Four ways it says
    it:

    * it names the phase its budget ran out in
      (``gate budget exhausted in: …``);
    * it declares itself INCONCLUSIVE (``gate: INCONCLUSIVE (exit 3)``)
      — which stands on its own even when no phase could be parsed;
    * it exits 3 — the code the gate uses for a run that could not
      decide (``scripts/run_tests.sh`` uses 0 for a pass, 1 for a failed
      phase, 3 for a run that could not decide);
    * **it says the host could not be measured**
      (``host measurability: the host cannot measure wall-clock …``).
      That is a declaration about the machine, and it stands even when
      the gate's exit code was 1 — which is exactly what a gate that
      measured the host after judging the phase used to do.

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
    if str(summary.get("verdict") or "").upper() == STATUS_INCONCLUSIVE:
        return True
    return summary.get("host_measurable") is False


def _summary_has_content(summary: Dict[str, Any]) -> bool:
    """Did the gate print anything the runner could read back?"""
    if not summary:
        return False
    for key, value in summary.items():
        if key == "host_measurable":
            # ``False`` is a fact, not an absence: "the host cannot be
            # measured" is precisely what the gate said, and ``False
            # == 0`` would otherwise hide it behind the empty-value
            # test below.
            if value is not None:
                return True
            continue
        if value not in (None, "", 0):
            return True
    return False


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
    host_text = ""
    if summary.get("host_measurable") is False:
        host_text = "门禁自报宿主不可测墙钟；"
    return (
        f"退出码 {exit_code}：门禁没能给出结论（{verdict_text}{host_text}"
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
    round_no: Optional[int] = None,
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

    Two files are written, and they have different jobs:

    * ``ci_output.json`` — the **live** verdict.  Its path and its
      meaning do not change: it is the verdict of the run that just
      happened, and the next round replaces it.  It is the payload
      the caller gave, byte for byte: this writer adds no key of its
      own, so the contract fields it carries are the caller's and
      only the caller's.
    * ``ci_output.round-<n>.json`` — this round's own copy of the
      same verdict.  The artifact directory of a VP is **reused**
      across rounds, so the live verdict of one round is the only
      thing that survives it; the copy is what a later round cannot
      take away.  No copy is ever overwritten (see
      :func:`_write_round_copy`).  **This** is the file that says
      which round it belongs to (``round`` / ``round_source``): the
      round label the writer resolves lives in the copy, never in the
      live verdict.

    ``round_no`` is the round this run belongs to, told by the caller
    (the agent knows which round it is in).  Without it the round is
    read off the plan's context packs — and when neither knows, **no
    round copy is written**: the writer does not guess a number.
    """
    if artifact_dir is None:
        return
    tmp_name: Optional[str] = None
    try:
        target_dir = Path(artifact_dir) / vp_id
        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_round, round_source = _resolve_round(
            round_no, artifact_dir, payload,
        )
        target = target_dir / "ci_output.json"
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, target)
        tmp_name = None  # ownership transferred; do not unlink on success
        if resolved_round is not None and round_source is not None:
            # The round label lives in the **copy**, not in the live
            # verdict: the live verdict is the payload the caller gave
            # (contract fields only), and the copy is the file that
            # names the round.
            #
            # The round copy is **evidence of a round that already
            # happened**: losing it must not change the verdict, so it
            # is written on its own terms and its failure is logged.
            try:
                _write_round_copy(
                    artifact_dir,
                    vp_id,
                    payload,
                    round_no=resolved_round,
                    round_source=round_source,
                )
            except Exception as exc:  # noqa: BLE001 - the copy is best-effort
                logger.warning(
                    "[ci_runner] could not write the round copy for %s: %s",
                    vp_id, exc,
                )
    except Exception as exc:  # noqa: BLE001 - audit copy is best-effort
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        logger.warning(
            "[ci_runner] could not write artifact for %s: %s", vp_id, exc,
        )


def _resolve_round(
    round_no: Optional[int],
    artifact_dir: Optional[Path],
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Which round is this run, and where does the number come from?

    Three sources, in this order:

    * the **caller** knows the round — it is the orchestrator, and it
      counts the rounds.  A positive integer is taken as it is, and the
      number is reported as coming from the ``parameter``.
    * the **context pack** knows the round too: the plan writes one
      pack per round (``<plan>/context/round-<n>.json``) and the
      **largest** ``n`` is the round that is running now.
    * the **payload** says which round it is — what a caller that
      already knew the round wrote into the evidence.

    ``0``, a negative number, a ``bool`` and ``None`` all mean "not
    given" — round 0 is not a round, and ``bool`` is a subclass of
    ``int``, so ``True`` would otherwise be resolved as round 1 and
    would name a file no contract describes.  When none of the three
    knows, the answer is ``(None, None)`` and no copy is written at all.

    The plan directory is derived from the artifact directory's parent
    (``<plan>/vp_artifacts`` → ``<plan>``), because that is where the
    runner's caller puts the artifact directory.
    """
    if (
        isinstance(round_no, int)
        and not isinstance(round_no, bool)
        and round_no > 0
    ):
        return round_no, ROUND_SOURCE_PARAMETER

    from_pack = _round_from_context_pack(artifact_dir)
    if from_pack is not None:
        return from_pack, ROUND_SOURCE_CONTEXT_PACK

    if payload is not None:
        claimed = payload.get("round")
        if (
            isinstance(claimed, int)
            and not isinstance(claimed, bool)
            and claimed > 0
        ):
            return claimed, ROUND_SOURCE_PAYLOAD
    return None, None


def _round_copy_name(round_no: int, target_dir: Path) -> str:
    """The name of this round's copy — a name that is free.

    The first copy of a round takes the round's own name; a further run
    in the same round takes the next free number after it.
    """
    first = ROUND_ARTIFACT_TEMPLATE.format(round_no=round_no)
    if not (target_dir / first).exists():
        return first
    sequence = 1
    while True:
        name = f"ci_output.round-{round_no}.{sequence}.json"
        if not (target_dir / name).exists():
            return name
        sequence += 1


def _round_from_context_pack(artifact_dir: Optional[Path]) -> Optional[int]:
    """The largest round number the plan's context packs name.

    ``None`` when there is no pack to ask — no artifact directory, no
    context directory, or no file in it that is a pack.  A directory that
    cannot be read is not an error here: the round is a convenience, and
    a directory that cannot be listed simply leaves the question
    unanswered.
    """
    if artifact_dir is None:
        return None
    try:
        plan_dir = Path(artifact_dir).resolve().parent
        context_dir = plan_dir / CONTEXT_PACK_DIRNAME
        if not context_dir.is_dir():
            return None
        rounds = [
            int(match.group(1))
            for match in (
                _CONTEXT_PACK_FILENAME_RE.match(entry.name)
                for entry in context_dir.iterdir()
            )
            if match is not None
        ]
    except OSError:
        return None
    return max(rounds) if rounds else None


def _write_ci_artifacts(
    artifact_dir: Optional[Path],
    vp_id: str,
    payload: Dict[str, Any],
    *,
    round_no: Optional[int] = None,
) -> None:
    """Check the payload against the contract, then write it down.

    2026-11-09: the runner wrote the artifact straight from the
    evidence dictionary it had just built, and nobody looked at that
    payload again.  So the contract for ``ci_output.json`` was written
    down three times — once by the producer, once by the consumer, once
    by the tests — and when the producer's copy drifted (``round`` /
    ``round_source``), the drift was found by the whole-repository
    gate, long after the thing that drifted had been written.

    :func:`ci_artifact.validate_ci_artifact` is the one copy of the
    contract now, and **this** is where the producer asks it.  A
    payload that breaks the contract is reported **by name** at ERROR
    level, so the log says which key is wrong rather than that
    something is.

    The report changes nothing else: no exception, no different
    verdict, and the artifact is still written.  A broken contract is a
    fact about the producer, and losing the evidence because of it would
    make the incident impossible to investigate.  ``artifact_dir=None``
    means "no audit copy wanted" — the payload is still checked,
    because the report is about the payload, not about the disk.

    The writer underneath stays a pure writer: it does not know the
    contract exists, it does not add a key and it does not drop one.

    The **round copy** is a second file, written beside the artifact:
    the evidence directory is reused across rounds, so the next round
    would overwrite this round's artifact.  The copy carries the round
    bookkeeping; the artifact does not — see :func:`_write_round_copy`.
    """
    for issue in validate_ci_artifact(payload):
        logger.error("[ci_artifact] %s", issue)
    _write_artifact(artifact_dir, vp_id, payload, round_no=round_no)


def _round_copy_says_the_same(
    target: Path, copy_payload: Dict[str, Any],
) -> bool:
    """Whether the copy that is already there says what this run says.

    Two runs in one round are two runs — but they are not two **facts**.
    When the second run's evidence is the same as the copy that is
    already on disk, that copy already says it: writing a second file
    would say the same thing twice, and a reader looking for "the copy
    of round 2" would have to guess which of the two is the one.

    A copy that cannot be read is **not** "the same": the safe reading
    of a file nobody can parse is that it says something we cannot see,
    so the new evidence gets a file of its own.
    """
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return existing == copy_payload


def _write_round_copy(
    artifact_dir: Optional[Path],
    vp_id: str,
    payload: Dict[str, Any],
    *,
    round_no: int,
    round_source: str,
) -> None:
    """Write this round's copy of the artifact, beside the artifact.

    Why a copy at all: the evidence directory of a VP is **reused
    across rounds**, so ``ci_output.json`` of round 2 overwrites the one
    of round 1 — and the round-1 evidence is exactly what a reader needs
    when the rounds disagree.  So each round also writes
    ``ci_output.round-<n>.json``.

    The copy carries two bookkeeping keys on top of the evidence —
    ``round`` (which round it is) and ``round_source`` (where the number
    came from).  They are **not** part of the artifact the runner
    writes, so the copy is checked with ``allow_round_fields=True``.

    **No copy is ever overwritten.**  A copy that is already there is
    evidence of an earlier run, so it is kept exactly as it is.  What
    this run writes beside it depends on what it has to say: a run
    whose evidence is **the same** as the copy that is there has
    nothing new to say, so it writes no second file at all — two files
    saying the same thing is one file too many.  A run whose evidence
    **differs** writes its own copy under the next free name
    (``ci_output.round-<n>.1.json``), so both runs stay on disk.  The
    runner says at WARNING level that the earlier copy was kept,
    because a reader who wonders why the file did not change has to be
    able to find the answer.

    The copy is written with a **hard link**, not a replace: a link
    refuses to land on a name that is already taken, so a copy from an
    earlier round cannot be clobbered even if another process claims
    the name in between.

    A copy that cannot be written changes **nothing** about the verdict:
    the verdict was decided before this ran, and it is not about the
    disk.  The temp file is removed either way — a ``.tmp`` left behind
    is invisible to the orphan sweep and grows silently across rounds.
    """
    if artifact_dir is None:
        return

    copy_payload = {**payload, "round": round_no, "round_source": round_source}
    for issue in validate_ci_artifact(copy_payload, allow_round_fields=True):
        logger.error("[ci_artifact] %s", issue)

    tmp_name: Optional[str] = None
    try:
        target_dir = Path(artifact_dir) / vp_id
        target_dir.mkdir(parents=True, exist_ok=True)
        first = ROUND_ARTIFACT_TEMPLATE.format(round_no=round_no)
        target = target_dir / first
        if target.exists():
            # The copy of this round is already there.  It is evidence of
            # an earlier run of this round, so it is kept exactly as it
            # is — and the runner says so, because a reader who wonders
            # why the file did not change has to be able to find the
            # answer.
            logger.warning(
                "[ci_runner] round copy %s already exists; keeping it", target,
            )
            if _round_copy_says_the_same(target, copy_payload):
                # This run says what the copy that is there already
                # says: a second file would only say it twice.
                return
            # This run says something the copy that is there does not.
            # The earlier copy is evidence and is never touched; this
            # one is written beside it.
            target = target_dir / _round_copy_name(round_no, target_dir)
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(copy_payload, handle, indent=2, ensure_ascii=False)
        try:
            os.link(tmp_name, target)
        except FileExistsError:
            logger.warning(
                "[ci_runner] round copy %s already exists; keeping it", target,
            )
    except Exception as exc:  # noqa: BLE001 - the copy is best-effort
        logger.warning(
            "[ci_runner] could not write round copy for %s: %s", vp_id, exc,
        )
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
