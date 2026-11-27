"""2026-11-01: 判定产物解析失败 ≠ 代码失败 —— 解析预算耗尽记 UNKNOWN。

上游材料 §1 的实测（``20261023-supervisor-CC-Connec-2`` round 4）::

    VP-017 FAILED  ['verdict parse error: top-level JSON must be an object, got list', ...]
    VP-018 DEFERRED ['Phase 2 关卡延后：Phase 1 的 VP-017 结论为 FAILED']
    VP-019 DEFERRED ['Phase 2 关卡延后：Phase 1 的 VP-017 结论为 FAILED']

模型返回了一个 JSON **数组**而不是对象，解析失败，于是：

  1. 一个**没有测过任何东西**的 VP 被记成 ``FAILED``（不是 ``BLOCKED``、
     不是 ``UNKNOWN``）；
  2. 这个假 FAILED 通过 Phase 2 关卡，把两个 VP 连锁成 ``DEFERRED``。

arch 决策点 1：引入显式「判定不可用」状态 ``UNKNOWN``，解析失败不得记
``FAILED``。arch 决策点 2：解析失败先重试一次，仍失败才降级，并保留原始
输出与失败原因。test 决策点 2：首次返回数组、第二次合法 → 重试成功、
最终有结论。

本文件钉住的契约：

  * 解析预算耗尽 → ``verdict == VERDICT_UNKNOWN``（``"UNKNOWN"``），不是
    ``FAILED`` —— 没测到任何东西的 VP 不是代码失败；
  * 证据首项以 ``raw_output:`` 开头，带**最后一次**解析失败的原始输出，
    失败原因同时留在 ``reasons`` 里（可复核）；
  * 第二次即为合法对象 → 用第二次的结论（``PASSED``），不得出现
    ``UNKNOWN``；
  * 传输层预算耗尽 → 仍 ``FAILED``（这一支不改：传输失败与判定不合规是
    两回事）；
  * **末次故障说了算**：预算是被哪一次故障吃掉的，判定就记哪一次 ——
    watchdog 杀进程是进程类故障（子 agent 根本没交上判定），走传输那一支
    落 ``FAILED`` 并在理由里点名这次 kill；判定不合规才落 ``UNKNOWN``。
    故障的**先后顺序**会改变判定：先解析失败后被 kill → ``FAILED``，
    先被 kill 后解析失败 → ``UNKNOWN``；
  * ``max_retries=0``（无重试预算）→ 直接 ``UNKNOWN``，不新增重试次数；
  * 原始输出为空 → 证据首项为 ``raw_output: <empty>``。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent as _verification_subagent  # noqa: E402
from verification_subagent import (  # noqa: E402
    VERDICT_UNKNOWN,
    VerificationSubAgent,
    VerdictParseError,
    WatchdogKilledError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedCodingTool:
    """A coding tool that answers with a scripted list of payloads.

    It is deliberately *not* a ``ClaudeCodingTool`` instance, so
    ``_execute_attempt`` uses it as its own ``scoped_tool``; the only
    method it has to provide is ``query_json``.
    """

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def query_json(self, prompt=None, system_instruction=None, timeout=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError(
                "the scripted coding tool ran out of responses"
            )
        return self._responses.pop(0)


async def _no_sleep(_delay: float) -> None:
    """Backoff stand-in — the retry *pacing* is not what these tests
    pin, and a real 1s + 2s wait would only slow the suite down."""
    return None


def _vp_node() -> Dict[str, Any]:
    return {
        "id": "VP-017",
        "title": "the module exposes its public API",
        "verification_method": "code_review",
        "expected_result": "the public API is reachable",
    }


#: The payload the measured run produced: a JSON **array** where the
#: contract asks for an object. ``json.loads`` decodes it into a list,
#: so the parse layer rejects it with "top-level JSON must be an
#: object, got list".
ARRAY_PAYLOAD: List[Dict[str, Any]] = [{"verdict": "PASSED"}]

#: A well-formed payload — the second attempt's answer.
VALID_PAYLOAD: Dict[str, Any] = {
    "verdict": "PASSED",
    "reasons": ["the public API is reachable"],
    "evidence": ["src/pkg/__init__.py:12"],
}


# ---------------------------------------------------------------------------
# 1. 解析预算耗尽 → UNKNOWN（不是 FAILED）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_exhaustion_yields_unknown(tmp_path, monkeypatch):
    """两次都返回数组 → 判定不可用（UNKNOWN），不是代码失败（FAILED）。

    这是上游实测的那一类：一个没有测过任何东西的 VP，不得被记成
    ``FAILED`` —— 那个假 FAILED 会连锁把下游 VP 记成 ``DEFERRED``。
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    tool = _ScriptedCodingTool([ARRAY_PAYLOAD, ARRAY_PAYLOAD])
    sub = VerificationSubAgent(method="code_review", max_retries=1)

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=tmp_path / "vp.log",
        project_dir=None,
    )

    assert tool.calls == 2, (
        f"the parse failure must consume the whole malfunction budget "
        f"(max_retries + 1 = 2 attempts); the tool was called "
        f"{tool.calls} times"
    )
    assert verdict.verdict == VERDICT_UNKNOWN, (
        f"an exhausted parse budget means 'no verdict was reached', not "
        f"'the code is broken'; got {verdict.verdict!r}"
    )
    assert verdict.verdict != "FAILED", (
        "a parse failure must never be filed as a code failure"
    )

    # The reason says which malfunction ate the budget, and why the
    # payload could not be parsed.
    joined = " | ".join(verdict.reasons)
    assert "verdict unparseable after 2 attempts" in joined, (
        f"the reason must name the malfunction and the attempt count; got "
        f"{verdict.reasons}"
    )
    assert "top-level JSON must be an object, got list" in joined, (
        f"the reason must keep the parse failure's own reason; got "
        f"{verdict.reasons}"
    )

    # The original output is kept, so the failure can be reviewed.
    assert verdict.evidence, (
        "an exhausted parse budget must leave the raw output behind"
    )
    assert verdict.evidence[0].startswith("raw_output:"), (
        f"the first evidence item must carry the raw output; got "
        f"{verdict.evidence[0]!r}"
    )
    assert "verdict" in verdict.evidence[0], (
        f"the raw output must be the payload itself; got "
        f"{verdict.evidence[0]!r}"
    )

    # The attempt malfunctioned; the deliverable is not implicated.
    assert verdict.failure_class == "infra", (
        f"the attempt malfunctioned, so the class is infra; got "
        f"{verdict.failure_class!r}"
    )
    assert verdict.failure_class_source == "deterministic", (
        f"the class was derived by rule, not claimed; got "
        f"{verdict.failure_class_source!r}"
    )


# ---------------------------------------------------------------------------
# 2. 重试一次就成功 → 用真实结论，不得出现 UNKNOWN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_then_success_keeps_real_verdict(tmp_path, monkeypatch):
    """首次返回数组、二次合法 → 重试成功，最终有结论（PASSED）。

    降级只在重试也失败之后发生：一次 garble 不该把一条本可以判定的
    验证点变成「判定不可用」。
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    tool = _ScriptedCodingTool([ARRAY_PAYLOAD, VALID_PAYLOAD])
    sub = VerificationSubAgent(method="code_review", max_retries=1)

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=tmp_path / "vp.log",
        project_dir=None,
    )

    assert tool.calls == 2, (
        f"the garbled attempt must be retried exactly once; the tool was "
        f"called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        f"the retry's verdict is the one that counts; got "
        f"{verdict.verdict!r}"
    )
    assert verdict.verdict != VERDICT_UNKNOWN, (
        "a retry that produced a real verdict must not be reported as "
        "'verdict unavailable'"
    )
    assert "UNKNOWN" not in " | ".join(verdict.reasons), (
        f"no UNKNOWN marker may survive a successful retry; got "
        f"{verdict.reasons}"
    )


# ---------------------------------------------------------------------------
# 3. 传输层预算耗尽 → 仍 FAILED（这一支不改）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_exhaustion_still_failed(tmp_path):
    """两次传输异常 → FAILED。

    传输失败与判定不合规是两种不同的故障：前者是「没跑起来」，后者是
    「跑起来了但结论不认」。把判定不合规改成 UNKNOWN 的同时，传输这一支
    必须原样保留 —— 它本来就是一次环境故障，不是一次代码判定。
    """
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    exec_mock = AsyncMock(side_effect=RuntimeError("api 502"))

    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value="healed")):
        verdict = await sub._self_heal_loop(
            vp_node=_vp_node(),
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp.log",
            project_dir=None,
        )

    assert exec_mock.await_count == 2, (
        f"the malfunction budget is max_retries + 1 = 2 attempts; got "
        f"{exec_mock.await_count}"
    )
    assert verdict.verdict == "FAILED", (
        f"an exhausted transport budget is still a FAILED; got "
        f"{verdict.verdict!r}"
    )
    assert any("max retries exhausted" in r for r in verdict.reasons), (
        f"the transport-exhausted marker must survive; got "
        f"{verdict.reasons}"
    )


# ---------------------------------------------------------------------------
# 3b. 故障的**先后顺序**决定判定：末次故障是什么，就记什么
# ---------------------------------------------------------------------------


def _parse_error(raw_output: str = "") -> VerdictParseError:
    """一次判定解析失败，带上子 agent 实际交上来的原始输出。"""
    error = VerdictParseError("top-level JSON must be an object, got list")
    error.raw_output = raw_output
    return error


def _watchdog_kill() -> WatchdogKilledError:
    """一次 watchdog 杀进程 —— 进程类故障（子 agent 根本没交上判定）。"""
    return WatchdogKilledError(1, RuntimeError("idle 1300s > 1200s"))


@pytest.mark.asyncio
async def test_parse_then_watchdog_kill_ends_failed(tmp_path, monkeypatch):
    """先解析失败、后被 watchdog 杀掉 → FAILED，理由指向这一次 kill。

    ``last_error_is_parse_error`` 曾经是**粘性**的：第一次尝试解析失败
    之后它一直为真，于是末次故障（进程被杀）被记成了「判定不合规」——
    判定落成 UNKNOWN，理由还引用了上一轮那条早已过期的解析错误。

    watchdog 杀进程是**进程类**故障：子 agent 压根没交上判定，预算是被
    这一次 kill 吃掉的。它必须走传输那一支（FAILED），理由必须说出是哪
    一次故障吃掉了预算，而不是引用上一轮那条过期的解析错误。
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    sub = VerificationSubAgent(method="code_review", max_retries=1)
    exec_mock = AsyncMock(
        side_effect=[
            _parse_error('[{"verdict": "PASSED"}]'),
            _watchdog_kill(),
        ]
    )

    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value="healed")):
        verdict = await sub._self_heal_loop(
            vp_node=_vp_node(),
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp.log",
            project_dir=None,
        )

    assert exec_mock.await_count == 2, (
        f"the malfunction budget is max_retries + 1 = 2 attempts; got "
        f"{exec_mock.await_count}"
    )
    assert verdict.verdict == "FAILED", (
        f"a watchdog kill is a process-class malfunction, so the budget was "
        f"eaten by it — not by the verdict parse; got {verdict.verdict!r}"
    )

    joined = " | ".join(verdict.reasons)
    assert "max retries exhausted" in joined, (
        f"the transport-exhausted marker must survive; got "
        f"{verdict.reasons}"
    )
    assert "watchdog" in joined.lower(), (
        f"the reason must name the malfunction that ate the budget; got "
        f"{verdict.reasons}"
    )
    assert "top-level JSON must be an object, got list" not in joined, (
        f"the reason must not blame the stale parse error from the earlier "
        f"attempt; got {verdict.reasons}"
    )


@pytest.mark.asyncio
async def test_watchdog_then_parse_ends_unknown(tmp_path, monkeypatch):
    """先被 watchdog 杀掉、后解析失败 → 仍是 UNKNOWN。

    顺序反过来，判定也反过来：末次故障是判定不合规，就只能记 UNKNOWN
    （没测到任何东西的 VP 不是代码失败）。这一条钉住「末次故障说了算」，
    免得把修复写成「只要出现过 watchdog 就一律 FAILED」。
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    sub = VerificationSubAgent(method="code_review", max_retries=1)
    exec_mock = AsyncMock(
        side_effect=[
            _watchdog_kill(),
            _parse_error('[{"verdict": "PASSED"}]'),
        ]
    )

    with patch.object(sub, "_execute_attempt", new=exec_mock), \
         patch.object(sub, "_self_heal", new=AsyncMock(return_value="healed")):
        verdict = await sub._self_heal_loop(
            vp_node=_vp_node(),
            coding_tool=None,
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp.log",
            project_dir=None,
        )

    assert verdict.verdict == VERDICT_UNKNOWN, (
        f"the LAST malfunction was a verdict-parse garble, so the budget was "
        f"eaten by it and the verdict is unavailable; got {verdict.verdict!r}"
    )
    joined = " | ".join(verdict.reasons)
    assert "verdict unparseable after 2 attempts" in joined, (
        f"the parse-exhausted marker must survive; got {verdict.reasons}"
    )


# ---------------------------------------------------------------------------
# 4. 无重试预算（max_retries=0）→ 直接 UNKNOWN，不新增重试次数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_retry_budget_yields_unknown_without_extra_attempt(
    tmp_path, monkeypatch,
):
    """``max_retries=0``：一次尝试就是全部预算，解析失败直接 UNKNOWN。

    降级不得凭空多花一次尝试 —— 预算语义是 ``max_retries + 1`` 次尝试，
    这次改动不能改写它。
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    tool = _ScriptedCodingTool([ARRAY_PAYLOAD])
    sub = VerificationSubAgent(method="code_review", max_retries=0)

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=tmp_path / "vp.log",
        project_dir=None,
    )

    assert tool.calls == 1, (
        f"max_retries=0 means exactly one attempt; the tool was called "
        f"{tool.calls} times"
    )
    assert verdict.verdict == VERDICT_UNKNOWN, (
        f"with no retry budget left the verdict is unavailable; got "
        f"{verdict.verdict!r}"
    )
    assert any(
        "verdict unparseable after 1 attempts" in r for r in verdict.reasons
    ), f"the attempt count must be in the reason; got {verdict.reasons}"


# ---------------------------------------------------------------------------
# 5. 原始输出为空 → raw_output: <empty>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_raw_output_is_marked_empty(tmp_path, monkeypatch):
    """子 agent 什么都没交上来 → 证据首项是 ``raw_output: <empty>``。

    空输出也要留痕：留一条空白的证据等于什么都没留。
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    tool = _ScriptedCodingTool(["", ""])
    sub = VerificationSubAgent(method="code_review", max_retries=1)

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=tmp_path / "vp.log",
        project_dir=None,
    )

    assert verdict.verdict == VERDICT_UNKNOWN, (
        f"an exhausted parse budget is UNKNOWN; got {verdict.verdict!r}"
    )
    assert verdict.evidence, "the evidence must not be empty"
    assert verdict.evidence[0] == "raw_output: <empty>", (
        f"an empty raw output must be marked as such; got "
        f"{verdict.evidence[0]!r}"
    )


# ---------------------------------------------------------------------------
# 6. 常量本身
# ---------------------------------------------------------------------------


def test_unknown_constant_is_the_documented_literal():
    """``VERDICT_UNKNOWN`` 是这个模块对外承诺的那一个字面量。

    下游按字符串取值（判定记录、数据库列、报告），所以这个值不能被
    悄悄改掉 —— 改了就是另一套词汇。
    """
    assert VERDICT_UNKNOWN == "UNKNOWN"
    assert _verification_subagent.VERDICT_UNKNOWN == "UNKNOWN"


# ---------------------------------------------------------------------------
# 7. 端到端：执行器必须放行 UNKNOWN（不得改写回 FAILED）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_keeps_unknown_verdict_intact():
    """``UNKNOWN`` 必须原样到达裁决表。

    裁决状态的唯一白名单是执行器的 ``ALLOWED_STATUSES``。不在里面的
    状态会被 ``_validate_verdict`` 拒绝，然后被 ``_run_single_vp``
    **改写成 FAILED** —— 原始输出（``raw_output``）与 ``infra`` 分类
    一起丢掉。也就是说：子 agent 记的 UNKNOWN 到不了裁决表，
    「解析失败不得记 FAILED」在端到端上根本不成立。

    这一条钉的是白名单本身：它必须收下 ``UNKNOWN``，并且执行器不得
    把这条裁决换成一条代码失败。
    """
    import verification_executor

    assert VERDICT_UNKNOWN in verification_executor.ALLOWED_STATUSES, (
        f"the executor's status whitelist must accept "
        f"{VERDICT_UNKNOWN!r}; got "
        f"{list(verification_executor.ALLOWED_STATUSES)}"
    )

    # 绕过 ``__init__`` 只设属性时，这里必须列出 ``_run_single_vp`` 这条路径
    # 真正读到的每一个属性（当前是 ``plan_id`` / ``plan_mode`` /
    # ``sub_agent_runner``）；生产侧新增属性时要同步补齐，否则夹具会因为
    # 少一个属性而以 AttributeError 失配。
    executor = object.__new__(verification_executor.VerificationExecutor)
    executor.plan_id = "plan-unknown-probe"
    executor.plan_mode = verification_executor.MODE_TEST
    executor.sub_agent_runner = lambda vp: {
        "status": VERDICT_UNKNOWN,
        "reasons": ["verdict unparseable after 2 attempts: bad payload"],
        "evidence": {
            "details": "raw_output: [{'verdict': 'PASSED'}]",
            "raw": ["raw_output: [{'verdict': 'PASSED'}]"],
        },
        "failure_class": "infra",
        "failure_class_source": "deterministic",
    }

    out = await executor._run_single_vp({"id": "VP-017"})

    assert out["status"] == VERDICT_UNKNOWN, (
        f"the executor must not rewrite an unavailable verdict into a "
        f"code failure; got {out['status']!r}"
    )
    assert "raw_output" in out["evidence"]["raw"][0], (
        f"the raw output must survive the executor; got {out['evidence']!r}"
    )
    assert out["failure_class"] == "infra", (
        f"the infra classification must survive the executor; got "
        f"{out.get('failure_class')!r}"
    )
