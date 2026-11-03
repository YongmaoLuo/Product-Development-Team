"""Provider re-selection for the verification sub-agent.

Arch decision point 2 — *infra 的跨轮有界重试与 provider 重选* — needs
two things from the verification sub-agent, and neither of them existed:

  * the caller must be able to say "do not use that provider again".
    The exclusion set has to travel from ``run()`` down to
    ``query_json``, which is where the provider is actually picked
    (``excluded = set(excluded_providers or [])`` in ``coding_tool``).

  * the verdict has to name the provider that served it. An infra
    failure is only actionable if the ledger can say *whose* provider
    broke — ``_delegate_to_sub_agent``'s ``sub_agent_completed`` event
    already reads ``verdict.provider``.

The two doubles below deliberately have different keyword surfaces:

  * ``_CapturingCodingTool`` accepts anything, so it can report the
    keyword arguments it was handed;
  * ``_ScriptedCodingTool`` accepts exactly the three keywords the old
    call site used, so handing it ``excluded_providers`` raises
    ``TypeError`` — the signal the empty-exclusion path must never
    produce.

Contracts pinned here:

  * ``test_excluded_providers_reach_query_json`` — a non-empty
    exclusion set reaches ``query_json`` as a keyword argument;
  * ``test_empty_excluded_providers_omits_kwarg`` — an empty (or
    absent) exclusion set adds no keyword at all;
  * ``test_verdict_records_actual_provider`` — the verdict names the
    provider that actually served the attempt;
  * ``test_infra_verdict_carries_provider_and_class`` — a transport
    failure yields an ``infra`` verdict that still names the provider.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_subagent import VerificationSubAgent  # noqa: E402


#: Sentinel for "this double has no ``current_call_provider`` attribute
#: at all" — the case the verdict's provider lookup has to survive.
_NO_PROVIDER = object()


VP_NODE: Dict[str, Any] = {
    "id": "VP-001",
    "title": "provider re-selection reaches the dispatch",
    "verification_method": "code_review",
    "test_command": "pytest -q",
    "expected_result": "no failures",
}


def _payload(provider: str = "") -> Dict[str, Any]:
    """A well-formed Verdict payload (what ``parse_verdict`` accepts)."""
    payload: Dict[str, Any] = {
        "verdict": "PASSED",
        "reasons": ["the change looks right"],
        "evidence": ["read the diff"],
    }
    if provider:
        payload["provider"] = provider
    return payload


class _CapturingCodingTool:
    """A coding-tool double that records every keyword it is handed."""

    def __init__(
        self,
        payload: Optional[Dict[str, Any]] = None,
        provider: Any = _NO_PROVIDER,
        error: Optional[BaseException] = None,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._payload = payload
        self._error = error
        if provider is not _NO_PROVIDER:
            self.current_call_provider = provider

    def query_json(
        self,
        prompt: Optional[str] = None,
        system_instruction: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            "timeout": timeout,
            "kwargs": kwargs,
        })
        if self._error is not None:
            raise self._error
        return dict(self._payload or _payload())


class _ScriptedCodingTool:
    """A coding-tool double with the OLD narrow keyword surface.

    ``query_json(self, prompt=None, system_instruction=None, timeout=None)``
    is exactly what the call site used before the exclusion set existed.
    Handing this double an ``excluded_providers`` keyword raises
    ``TypeError`` — which the dispatch would swallow as a transport
    error and turn a passing verification point into a failing one.
    """

    def __init__(
        self,
        payload: Optional[Dict[str, Any]] = None,
        provider: Any = _NO_PROVIDER,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._payload = payload
        if provider is not _NO_PROVIDER:
            self.current_call_provider = provider

    def query_json(
        self,
        prompt: Optional[str] = None,
        system_instruction: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            "timeout": timeout,
        })
        return dict(self._payload or _payload())


async def _run(
    sub: VerificationSubAgent,
    tool: Any,
    tmp_path: Path,
    **kwargs: Any,
):
    """Drive one full ``run()`` against a coding-tool double."""
    return await sub.run(
        vp_node=dict(VP_NODE),
        coding_tool=tool,
        log_dir=tmp_path,
        project_dir=tmp_path,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The exclusion set has to reach the dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_excluded_providers_reach_query_json(tmp_path: Path) -> None:
    """A non-empty exclusion set reaches ``query_json`` as a keyword.

    The whole chain is exercised: ``run`` → ``_self_heal_loop`` →
    ``_execute_attempt`` → ``query_json``. The provider that failed on
    the previous round can only be skipped if the set survives every
    hop of that chain.
    """
    sub = VerificationSubAgent(method="code_review")
    tool = _CapturingCodingTool(provider="vendor-b")

    verdict = await _run(
        sub, tool, tmp_path, excluded_providers=["vendor-a-pro"],
    )

    assert len(tool.calls) == 1, (
        f"expected exactly one dispatch, got {len(tool.calls)}"
    )
    assert tool.calls[0]["kwargs"].get("excluded_providers") == ["vendor-a-pro"], (
        f"the exclusion set must reach query_json unchanged, got "
        f"{tool.calls[0]['kwargs']!r}"
    )
    assert verdict.verdict == "PASSED", (
        f"the stand-in answered a PASSED payload; got "
        f"{verdict.verdict!r} / {verdict.reasons!r}"
    )


@pytest.mark.parametrize(
    "excluded",
    [[], None],
    ids=["empty_list", "none"],
)
@pytest.mark.asyncio
async def test_empty_excluded_providers_omits_kwarg(
    tmp_path: Path, excluded: Any
) -> None:
    """An empty (or absent) exclusion set adds NO keyword at all.

    The double has the old narrow signature, so a stray
    ``excluded_providers=`` would raise ``TypeError`` inside the
    dispatch; the transport-error arm would swallow it and report a
    FAILED verdict for a verification point that actually passed.
    """
    sub = VerificationSubAgent(method="code_review")
    tool = _ScriptedCodingTool(provider="vendor-b")

    verdict = await _run(
        sub, tool, tmp_path, excluded_providers=excluded,
    )

    assert len(tool.calls) == 1, (
        f"expected exactly one dispatch, got {len(tool.calls)}"
    )
    assert verdict.verdict == "PASSED", (
        f"the narrow-signature double must never see the keyword — a "
        f"TypeError would have been reported as {verdict.verdict!r} / "
        f"{verdict.reasons!r}"
    )


# ---------------------------------------------------------------------------
# The verdict has to name the provider that served it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_records_actual_provider(tmp_path: Path) -> None:
    """The verdict names the provider that actually served the attempt.

    The sub-agent's own payload does not declare one, so the provider
    has to come from the dispatch (``current_call_provider``).
    """
    sub = VerificationSubAgent(method="code_review")
    tool = _CapturingCodingTool(provider="vendor-b")

    verdict = await _run(sub, tool, tmp_path)

    assert verdict.verdict == "PASSED"
    assert verdict.provider == "vendor-b", (
        f"the verdict must name the provider that served it, got "
        f"{verdict.provider!r}"
    )


@pytest.mark.asyncio
async def test_infra_verdict_carries_provider_and_class(tmp_path: Path) -> None:
    """A transport failure yields an ``infra`` verdict that names the
    provider.

    Both facts are needed downstream: the class says the failure
    belongs to the environment rather than to the deliverable, and the
    provider says *which* environment. The retry that follows is the
    one that excludes that provider.
    """
    sub = VerificationSubAgent(method="code_review")
    tool = _CapturingCodingTool(
        provider="vendor-b", error=RuntimeError("api 502"),
    )

    verdict = await _run(sub, tool, tmp_path)

    assert verdict.verdict == "FAILED", (
        f"a transport error must not be reported as a pass, got "
        f"{verdict.verdict!r}"
    )
    assert verdict.failure_class == "infra", (
        f"a transport failure is an infra failure, got "
        f"{verdict.failure_class!r}"
    )
    assert verdict.provider == "vendor-b", (
        f"the infra verdict must name the provider that failed, got "
        f"{verdict.provider!r}"
    )


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_verdict_provider_is_not_overwritten(tmp_path: Path) -> None:
    """A verdict that already declares a provider keeps it.

    The dispatch's provider is a *fallback* for a payload that says
    nothing — it must never overwrite what the payload claimed.
    """
    sub = VerificationSubAgent(method="code_review")
    tool = _CapturingCodingTool(
        payload=_payload(provider="vendor-c"), provider="vendor-b",
    )

    verdict = await _run(sub, tool, tmp_path)

    assert verdict.provider == "vendor-c", (
        f"a provider declared by the payload must survive, got "
        f"{verdict.provider!r}"
    )


@pytest.mark.asyncio
async def test_tool_without_provider_attribute_yields_empty_provider(
    tmp_path: Path,
) -> None:
    """A tool with no ``current_call_provider`` yields an empty string.

    Not an exception, not ``None`` — the field is a ``str`` and the
    verdict contract says "empty string if unknown".
    """
    sub = VerificationSubAgent(method="code_review")
    tool = _CapturingCodingTool()

    verdict = await _run(sub, tool, tmp_path)

    assert verdict.verdict == "PASSED"
    assert verdict.provider == "", (
        f"an unknown provider must be the empty string, got "
        f"{verdict.provider!r}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
