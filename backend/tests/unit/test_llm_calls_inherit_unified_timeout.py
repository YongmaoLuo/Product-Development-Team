"""2026-09-15 static gate — LLM calls must inherit the unified timeout budget.

Background: “别传 timeout 了，继承统一的规则就可以”.

``CodingTool.query`` / ``query_json`` take an optional ``timeout`` that
does NOT nest inside the unified windows — it REPLACES them (the value
becomes both the adaptive-silence cap and the wall-clock guard for that
call). So a magic number written at a call site silently overrides the
900s-silence / 1800s-idle / 1-hour-ceiling design.

Live damage this rule prevents: ``timeout=60`` on the per-VP supplement
review turned healthy Vendor A responses into 17/30 silent degradations
on a production plan — each failure burned ~3 minutes across the provider
failover while CC Switch recorded 100% success on the provider side
(we were aborting our own requests). The same 60s sat on the
task-execution inline spec review.

Rule enforced here: in production modules, an LLM call's ``timeout``
keyword may be ``None``, a plain variable, or a ``self.<CONSTANT>``
attribute — never a numeric literal. Named constants stay fine (they are
greppable and documented); bare numbers do not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]

#: Production modules that issue LLM calls through a coding tool.
_LLM_MODULES = (
    "agent.py",
    "verification_agent.py",
    "verification_executor.py",
    "verification_subagent.py",
    "repair_generator.py",
    "orchestrator.py",
    "server.py",
)

#: Methods on a coding tool that accept ``timeout``.
_LLM_METHODS = ("query", "query_json")


def _timeout_offences(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, rendered_value)`` for every numeric-literal timeout."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offences: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) not in _LLM_METHODS:
            continue
        for kw in node.keywords:
            if kw.arg != "timeout":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(
                kw.value.value, (int, float)
            ) and not isinstance(kw.value.value, bool):
                offences.append((node.lineno, ast.unparse(kw.value)))
    return offences


@pytest.mark.parametrize("module", _LLM_MODULES)
def test_no_magic_number_timeout_on_llm_calls(module: str):
    path = _BACKEND / module
    if not path.exists():  # module renamed/removed — nothing to police
        pytest.skip(f"{module} not present")

    offences = _timeout_offences(path)
    assert not offences, (
        f"{module}: LLM call sites pass a numeric-literal timeout "
        f"{offences}. That value REPLACES the unified 900s-silence / "
        f"1800s-idle windows instead of nesting inside them. Drop the "
        f"kwarg to inherit, or extract a named constant."
    )
