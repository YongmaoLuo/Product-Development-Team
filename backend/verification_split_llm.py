"""LLM-driven verification-point split for single-clause VPs.

Sister module to :mod:`verification_split`. Where
:class:`verification_split.SplitDecision` is a pure-function over
``expected_result`` split on ``;``, :class:`LLMVPSplitDecision` asks
the LLM to decompose a single-clause VP into 2-4 sub-VPs based on
the verification method and any other VP-shape hints. This is the
slow path invoked only when ``SplitDecision.should_split`` returned
``None`` (e.g. VP-034's "全量 pytest 基线 1,482 全绿" has no ``;``
separator).

Contract (mirrors ``SplitDecision.should_split``):

    Return: Optional[List[Dict[str, Any]]]
      * ``None`` if the LLM could not produce a valid list (parse
        error, empty list, schema violation, or the inner
        coding-tool call itself raised ``HardTimeoutError``).
      * ``[{"id": ..., "parent_vp_id": ..., "expected_result": ...,
          "verification_method": ..., "timeout_seconds": ...,
          "test_command": ...}, ...]`` on success.

Why a separate module: keeps ``verification_split.py`` as a pure
local-logic file (no LLM dependency), matching the codebase's
existing convention — ``SplitDecision`` is intentionally
state-free / deterministic per its docstring
(``verification_split.py:6-13``).

Design rationale (2026-09-08 plan):
  * Reusing ``TaskRefiner.refine`` was considered but rejected:
    TaskRefiner's contract is SubTask-shaped (different schema),
    its prompt is task-shaped, and routing VPs through it would
    re-implement ~40% of the file. A sibling class with the same
    return contract is cleaner.
  * Never raises: on ``HardTimeoutError`` from the inner coding
    tool (the LLM call itself timed out), returns ``None`` so the
    caller falls through to the original timeout verdict instead
    of recursing into another 1-hour cap.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from coding_tool import HardTimeoutError

logger = logging.getLogger(__name__)


# 2026-09-13: per-VP timeout interface deleted — child sub-VPs
# always carry the flat 1-hour cap value, same as top-level VPs.
# Enforcement is the outer HARD_WALL_CLOCK_CAP_SECONDS plus the inner
# 15-min idle detector; the per-child value is surface metadata only.
DEFAULT_SUBTASK_TIMEOUT_SECONDS: int = 3600

# Cap retries to keep the outer 1-hour wall-clock honest. The outer
# cap means we have ~30 min of "real work" left after firing; 2
# attempts × ~5 min each fits comfortably with headroom.
MAX_LLM_SPLIT_ATTEMPTS: int = 2

# Per-attempt timeout for the LLM call (NOT the test_command
# timeout). 5 min gives the LLM enough time to think and respond
# without consuming the 1-hour outer cap.
LLM_CALL_TIMEOUT_SECONDS: int = 300

# Max recursion depth for auto-splitting VPs on hard_timeout.
# 2026-09-08 runaway: VP-034 was split 8 levels deep (471 VP attempts
# in vp_attempts/, 113 at L7, 68 at L8), each level firing a Claude
# LLM call, burning massive tokens before anyone noticed. The 1-hour
# outer cap does NOT catch this — recursion happens at the 60-300s
# leaf pytest timeout. 2026-09-08: split depth is capped at 5.
MAX_SPLIT_DEPTH: int = 5


def _split_depth(vp_id: str) -> int:
    """Return the split depth encoded in ``vp_id``.

    Each level of auto-split appends ``-L<N>`` to the parent id (per
    the splitter contract at :mod:`verification_split`). Examples::

        "VP-034"               -> 0   (root, never split)
        "VP-034-L1"            -> 1   (split once)
        "VP-034-L1-L2"         -> 2   (split twice)
        "VP-034-L1-L2-L3-L4-L5" -> 5   (split 5 times — at cap)

    Tokens inside non-L segments are ignored: ``"VP-L-test"`` = 0
    because ``test`` does not match ``L<digit>+``. Multi-digit
    level numbers are matched (``L12`` counts as one segment).
    """
    if not isinstance(vp_id, str) or not vp_id:
        return 0
    depth = 0
    for seg in vp_id.split("-"):
        if len(seg) >= 2 and seg[0] == "L" and seg[1:].isdigit():
            depth += 1
    return depth


_LLM_SPLIT_SYSTEM_PROMPT: str = (
    "You are a verification planner. Decompose a single verification "
    "point (VP) into 2-4 smaller sub-VPs that can each be executed "
    "independently within the time budget. Each sub-VP must:\n\n"
    "* Test a DISTINCT sub-claim of the original VP's expected_result.\n"
    "* Be independently verifiable (own pass/fail criterion).\n"
    "* Carry enough context that an executor can run it without seeing "
    "the original VP.\n\n"
    "Output a JSON object of shape:\n"
    "{\n"
    '  "children": [\n'
    "    {\n"
    '      "id": "<original>-L1",\n'
    '      "expected_result": "<distinct sub-claim>",\n'
    '      "verification_method": "<inherited>",\n'
    '      "test_command": "<a runnable shell command, smaller than the parent>"\n'
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n\n"
    "If the VP cannot be meaningfully decomposed (it IS the smallest "
    'unit), output {"children": []}.'
)


_LLM_SPLIT_USER_PROMPT: str = (
    "Verification Point to decompose (hit 1-hour hard timeout):\n\n"
    "ID: {vp_id}\n"
    "verification_method: {method}\n"
    "expected_result: {expected_result}\n"
    "test_command: {test_command}\n"
    "original_status: {status}\n"
    "original_error: {error}\n\n"
    "Produce 2-4 sub-VPs covering distinct sub-claims of expected_result."
)


class LLMVPSplitDecision:
    """LLM-driven sister of :class:`verification_split.SplitDecision`.

    See module docstring for design rationale.
    """

    @classmethod
    async def should_split(
        cls,
        agent: Any,  # VerificationAgent — typed as Any to avoid cycle
        vp: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Optional[List[Dict[str, Any]]]:
        """Return sub-VPs (2-4) if LLM can decompose, else ``None``.

        Never raises. On ``HardTimeoutError`` from the inner coding
        tool (the LLM call itself timed out), returns ``None`` so the
        caller falls through to the original timeout verdict instead
        of recursing into another 1-hour cap.
        """
        if not isinstance(vp, Mapping):
            return None
        vp_id_str = vp.get("id", "unknown") if isinstance(vp.get("id"), str) else "unknown"
        depth = _split_depth(vp_id_str)
        if depth >= MAX_SPLIT_DEPTH:
            # 2026-09-08 guard: prevent runaway recursion. Without
            # this, a single bad VP can be split 8+ levels deep,
            # spawning hundreds of sub-VPs and burning massive
            # tokens. Returning ``None`` here lets the caller fall
            # through to the plain ``status="timeout"`` verdict —
            # the VP will be reported FAILED, but no new sub-VPs
            # are created.
            logger.warning(
                "[LLMVPSplit] vp=%s already at depth %d — refusing to "
                "split further (MAX_SPLIT_DEPTH=%d). Falling through "
                "to plain timeout verdict.",
                vp_id_str, depth, MAX_SPLIT_DEPTH,
            )
            return None
        method = vp.get("verification_method", "code_review")
        method_str = method if isinstance(method, str) else "code_review"

        user_prompt = _LLM_SPLIT_USER_PROMPT.format(
            vp_id=vp.get("id", "unknown"),
            method=method_str,
            expected_result=vp.get("expected_result", ""),
            test_command=vp.get("test_command", ""),
            status=result.get("status", "hard_timeout"),
            error=str(result.get("reasons", [""])[0])[:500],
        )

        last_err: Optional[Exception] = None
        for attempt in range(MAX_LLM_SPLIT_ATTEMPTS):
            try:
                # Use the agent's existing coding_tool handle so the
                # split inherits the same provider / API key / logging
                # as the original VP run.
                #
                # NOTE: ``coding_tool.query_json`` is SYNC — it returns a
                # dict, not an awaitable. Awaiting it raises
                # ``TypeError: object dict can't be used in 'await'
                # expression`` (regression: 2026-09-08, VP-034 round 3
                # on the 2026-09-04 plan produced two
                # such failures then gave up). The split helper is
                # ``async`` only because the surrounding
                # ``_llm_split_vp_on_hard_timeout`` may run alongside
                # other async work — the LLM call itself does not need
                # to be awaited.
                coding_tool = getattr(agent, "coding_tool", None)
                if coding_tool is None:
                    logger.warning(
                        "[LLMVPSplit] agent has no coding_tool handle "
                        "— cannot query LLM for vp=%s",
                        vp.get("id"),
                    )
                    return None
                response = coding_tool.query_json(
                    prompt=user_prompt,
                    system_instruction=_LLM_SPLIT_SYSTEM_PROMPT,
                    timeout=LLM_CALL_TIMEOUT_SECONDS,
                )
                return cls._parse_and_validate(vp, response, method_str)
            except HardTimeoutError:
                # Split itself hit a hard cap. Bail out — don't
                # recurse. Operator-visible log line so we can see
                # this happened.
                logger.warning(
                    "[HARD TIMEOUT split-bail] vp=%s could not be "
                    "split via LLM — HardTimeoutError on attempt %d",
                    vp.get("id"), attempt + 1,
                )
                return None
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "LLMVPSplitDecision attempt %d failed for vp=%s: %s",
                    attempt + 1, vp.get("id"), exc,
                )
                continue
        if last_err is not None:
            logger.warning(
                "LLMVPSplitDecision gave up after %d attempts: %s",
                MAX_LLM_SPLIT_ATTEMPTS, last_err,
            )
        return None

    @classmethod
    def _parse_and_validate(
        cls,
        vp: Mapping[str, Any],
        response: Any,
        method_str: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Parse the LLM response and return validated child VPs, or None.

        Schema (mirrors SplitDecision):
          * response is a dict
          * response["children"] is a non-empty list of 2-4 dicts
          * each child has ``expected_result`` (non-empty str) and
            ``test_command`` (non-empty str)
        """
        if not isinstance(response, dict):
            return None
        raw_children = (
            response.get("children")
            or response.get("sub_vps")
            or response.get("result")
        )
        if not isinstance(raw_children, list) or not (2 <= len(raw_children) <= 4):
            return None
        parent_id = str(vp.get("id", ""))
        if not parent_id:
            return None

        out: List[Dict[str, Any]] = []
        for idx, raw in enumerate(raw_children, start=1):
            if not isinstance(raw, dict):
                return None
            er = raw.get("expected_result", "")
            cmd = raw.get("test_command", "")
            if not isinstance(er, str) or not er.strip():
                return None
            if not isinstance(cmd, str) or not cmd.strip():
                return None
            # 2026-09-13: the LLM-provided ``timeout_seconds`` is
            # ignored (per-VP timeout interface deleted) — children
            # always get the flat 1-hour cap value.
            timeout_seconds = DEFAULT_SUBTASK_TIMEOUT_SECONDS
            out.append({
                "id": f"{parent_id}-L{idx}",
                "parent_vp_id": parent_id,
                "original_vp_id": parent_id,
                "split_clause_index": idx,
                "verification_method": method_str,
                "expected_result": er.strip(),
                "test_command": cmd.strip(),
                "timeout_seconds": timeout_seconds,
            })
        return out
