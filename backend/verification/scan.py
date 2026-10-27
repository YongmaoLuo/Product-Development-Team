"""全库验证点扫描聚合内核（纯聚合：只读、零外部依赖）。

这个模块解决什么
----------------
A4 需要一份**同口径、可复跑**的全库扫描：数清楚「可确定性执行的 VP」到底
有多少、占全部 VP 的多少。这份数字是收益上界，也是后续优化的验收基准。

「手工数一遍」不满足这个要求 —— 每次数都会换一个口径（这次把 ``e2e`` 算进去、
下次不算；这次按 ``ci_entry`` 存在与否算、下次按入口有没有被收窄算），数字之间
无法比较。所以判定必须写成一个内核，同一份输入跑两次必须给出同一份报告。

口径（唯一一处定义，别处只引用）
--------------------------------
「可确定性执行」= **这条 VP 的判定不依赖任何一次模型往返**，且**它声明的
执行入口是齐的**。三个方法满足这个定义：

``api_test``
    判定是 ``(request, response, assertions)`` 的纯函数，框架自己跑、自己判。
    口径直接复用 :func:`verification_api_runner.validate_vp`：``request``
    （method + url）与 ``assertions`` 齐全、且断言本身合法 —— 少一样，这条
    VP 连发都发不出去，不该算进「可确定性执行」。
``full_ci``
    判定是整仓 CI 入口那条命令的**退出码**，同样没有自报环节。口径复用
    :func:`verification_ci_runner.validate_vp`：``ci_entry`` 必须存在，且
    **没有被收窄**成「只跑一个用例」—— 收窄过的入口证明的不是整仓门禁。
``e2e``
    判定走真实链路，端到端跑一遍。它的执行入口是 ``target_url``：入口缺失
    这条 VP 根本无从跑起，所以「入口齐全」是它能被确定性执行的最低要求。

其余方法（``code_review`` / ``ui_validation`` 以及任何未知方法）**不进**
``deterministic``。其中「未知方法」的处理值得单说：``verification_method``
不在已知集合里的 VP 是一条**合法**的 VP，它只是不由框架执行 —— 所以它
计入 ``by_method``（VP 总数不能因此少算），但不计入 ``deterministic``
（它不是可确定性执行的方法）。把未知方法悄悄算进分子，会让这份报告虚高。

为什么复用现有 runner 的判定，而不是自己再写一套
------------------------------------------------
扫描报告的价值全在「同口径」上。如果扫描用的是一套自己的判定，而真正跑起来
用的是 ``verification_api_runner`` / ``verification_ci_runner`` 里的那一套，
两份数字会在同一个仓库上打架 —— 而报告是用来当验收基准的，基准和现实打架
就没有意义。所以这里的口径**直接调用**那两个 runner 的 ``validate_vp``，
它们改了这里自动跟着改。

边界条件
--------
* **空目录** → 零值报告，不抛错。「库里没有计划」是一个合法的答案，不是错误。
* **缺 ``id`` 的 VP** → 跳过。``id`` 是 VP 的唯一标识；没有它，这条 VP
  无法被指认，也无法被回溯，跳过比给它编一个键更诚实。
* **读不出机器计划的计划目录** → 整个计划跳过，不进 ``plans``。理由同上：
  我们对它的 VP 数**一无所知**，报「0 条」是一句谎话；跳过只是少统计了它。
  （注意这不会让占比虚高：被跳过的计划对分子分母的贡献都是 0。）
* **机器计划不是 JSON / 不是对象** → 同上，整个计划跳过。
* **不带 ``verification_method`` 的 VP** → 计入 ``by_method`` 的
  ``(unspecified)`` 桶，不计入 ``deterministic``。

三条不可协商的约束
------------------
1. **只读入参** ``plans_dir``。不读环境变量、不发网络请求、不调 LLM。
   扫描是统计，不是判定 —— 任何一次带外部不确定性的动作都会让这份报告
   不再「可复跑」。
2. **纯函数**：``scan_plans`` 不改全局状态、不打日志、不写文件（除了 CLI
   自己落盘的那一份报告）。
3. **确定性**：计划按目录名排序、``by_method`` 按出现顺序、``deterministic``
   固定按 :data:`DETERMINISTIC_METHODS` 的顺序输出 —— 同一份输入，两次运行
   逐字节相同。

命令行
------
::

    # 仓库根目录下
    PYTHONPATH=backend python -m verification.scan --plans-dir plans --out scan.json

    # backend/ 目录下
    PYTHONPATH=.. python -m verification.scan --plans-dir ../plans --out ../scan.json

``--plans-dir`` 是必填的：这条内核**不读环境变量**，计划目录必须由调用方
明确给出，否则「扫的是哪个库」这件事就没有答案。``--out`` 省略时报告打到
stdout。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from verification_api_runner import validate_vp as _validate_api_vp
from verification_ci_runner import validate_vp as _validate_ci_vp

__all__ = [
    "SCHEMA_VERSION",
    "DETERMINISTIC_METHODS",
    "PLAN_FILENAME",
    "UNSPECIFIED_METHOD",
    "ScanReport",
    "scan_plans",
    "main",
]

#: The report schema version. Bump it whenever the report's shape changes
#: in a way a consumer could notice — a report is written to disk and read
#: back later, so "what shape was this" has to be answerable from the file.
SCHEMA_VERSION = 1

#: The methods whose verdict is a pure function of a declared input the
#: framework executes itself — no sub-agent, no LLM, no self-reporting.
#: Order is part of the contract: it fixes the key order of the
#: ``deterministic`` block in every report, so two runs produce
#: byte-identical files.
DETERMINISTIC_METHODS: Tuple[str, ...] = ("api_test", "full_ci", "e2e")

#: The machine plan's file name inside a plan directory.
PLAN_FILENAME = "verification_plan.json"

#: The ``by_method`` bucket for a VP that declares no method at all. It is
#: counted (the VP exists) but never deterministic (we cannot say how it
#: would be executed).
UNSPECIFIED_METHOD = "(unspecified)"

#: The report shape produced by :func:`scan_plans`.
ScanReport = Dict[str, Any]


# ---------------------------------------------------------------------------
# Per-VP classification
# ---------------------------------------------------------------------------


def _vp_method(vp: Dict[str, Any]) -> str:
    """Return the method a VP declares, or :data:`UNSPECIFIED_METHOD`."""
    return str(vp.get("verification_method") or "").strip() or UNSPECIFIED_METHOD


def _has_text_field(vp: Dict[str, Any], field: str) -> bool:
    """True when ``vp`` declares a non-blank string in ``field``."""
    return bool(str(vp.get(field) or "").strip())


def _is_deterministic(method: str, vp: Dict[str, Any]) -> bool:
    """True when this VP can be executed and graded without a model.

    The two framework-executed methods delegate to the very same
    ``validate_vp`` their runners use at execution time, so the scan's
    verdict and the runner's verdict cannot drift apart.
    """
    if method == "api_test":
        # No issue at all ⇒ the request and the assertions are complete and
        # the assertions are gradeable.
        return not _validate_api_vp(vp)
    if method == "full_ci":
        # ``project_dir`` is deliberately not passed: the scan reads the
        # plans directory and nothing else, so it cannot check whether the
        # referenced script exists in some project checkout. What it does
        # check is that the entry exists and was not narrowed down to a
        # single test case.
        return not _validate_ci_vp(vp)
    if method == "e2e":
        # The e2e entry point is the URL the flow is driven against.
        return _has_text_field(vp, "target_url")
    return False


def _scan_plan(plan_id: str, plan_data: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate one plan's verification points into the per-plan block."""
    by_method: Dict[str, int] = {}
    deterministic: Dict[str, int] = {method: 0 for method in DETERMINISTIC_METHODS}

    points = plan_data.get("verification_points")
    if not isinstance(points, list):
        points = []

    for vp in points:
        if not isinstance(vp, dict):
            continue
        # A VP without an id cannot be pointed at; skip it rather than
        # invent a key for it.
        if not str(vp.get("id") or "").strip():
            continue
        method = _vp_method(vp)
        by_method[method] = by_method.get(method, 0) + 1
        if method in deterministic and _is_deterministic(method, vp):
            deterministic[method] += 1

    return {
        "plan_id": plan_id,
        "vp_total": sum(by_method.values()),
        "by_method": by_method,
        "deterministic": deterministic,
        "deterministic_total": sum(deterministic.values()),
    }


def _load_plan(plan_file: Path) -> Optional[Dict[str, Any]]:
    """Return the machine plan, or ``None`` when it cannot be read.

    ``None`` means "we do not know what this plan contains" — the caller
    skips the plan entirely rather than reporting a zero-VP plan it never
    actually read.
    """
    try:
        raw = plan_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        plan_data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(plan_data, dict):
        return None
    return plan_data


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------


def scan_plans(plans_dir: Any) -> ScanReport:
    """Scan every plan under ``plans_dir`` and aggregate the counts.

    ``plans_dir`` is the directory that *contains* plan directories; each
    plan directory holds a ``verification_plan.json``. The plan's id is its
    directory name — the same source the verification agent uses.

    The result is a plain dict (see :data:`ScanReport`):

    * ``schema_version`` — the report schema version.
    * ``plans`` — one block per scannable plan, ordered by directory name.
    * ``totals`` — the same counters across every plan, plus ``ratio``:
      ``deterministic_total / vp_total`` rounded to three decimals (0.0
      when there is no VP at all).

    Nothing outside ``plans_dir`` is read, no environment variable is
    consulted, no request is made and no model is called.
    """
    root = Path(plans_dir)
    plans: List[Dict[str, Any]] = []

    if root.is_dir():
        # Sorted so two runs over the same tree emit the same order.
        for plan_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            plan_file = plan_dir / PLAN_FILENAME
            if not plan_file.is_file():
                continue
            plan_data = _load_plan(plan_file)
            if plan_data is None:
                continue
            plans.append(_scan_plan(plan_dir.name, plan_data))

    vp_total = sum(entry["vp_total"] for entry in plans)
    deterministic_total = sum(entry["deterministic_total"] for entry in plans)
    ratio = round(deterministic_total / vp_total, 3) if vp_total else 0.0

    return {
        "schema_version": SCHEMA_VERSION,
        "plans": plans,
        "totals": {
            "plans": len(plans),
            "vp_total": vp_total,
            "deterministic_total": deterministic_total,
            "ratio": ratio,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Scan a plans directory and write the report out.

    ``--plans-dir`` is required (this kernel reads no environment
    variable). ``--out`` is optional: when it is given the report is
    written there, otherwise it goes to stdout.
    """
    parser = argparse.ArgumentParser(
        prog="python -m verification.scan",
        description=(
            "全库扫描验证点，统计「可确定性执行的 VP 数」与其占比。"
        ),
        epilog=(
            "示例：\n"
            "  PYTHONPATH=backend python -m verification.scan "
            "--plans-dir plans --out scan.json\n"
            "  PYTHONPATH=backend python -m verification.scan "
            "--plans-dir plans   # 报告打到 stdout"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--plans-dir",
        required=True,
        help="包含计划目录的父目录（每个子目录一个计划）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="报告输出路径；省略则打到 stdout",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = scan_plans(args.plans_dir)
    text = json.dumps(report, ensure_ascii=False, indent=2)

    if args.out:
        out_path = Path(args.out)
        parent = out_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())