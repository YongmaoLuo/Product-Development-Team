"""LLM JSON repair helper.

Background
----------
``coding_tool.query_json`` and ``self_review._parse_llm_response`` both
grok an LLM response via a bracket-extraction idiom::

    start = text.find('{')
    end = text.rfind('}') + 1
    parsed = json.loads(text[start:end])

This handles "JSON wrapped in conversational prose" but **not** the
common failure mode where the LLM output was truncated mid-way (e.g.
``max_tokens`` wall or provider-side output cap). A truncated reply
often looks like::

    {"findings": [{"severity": "high", "finding": "前置条件：任务 1、

…with the closing brace, the closing quote, and the closing array
all missing. The bracket-extraction idiom happily returns this
fragment, ``json.loads`` raises ``JSONDecodeError``, and the caller
treats the entire stage as failed.

This module provides a tolerant parser that, on parse failure, tries
to repair the truncated JSON by walking the partial token stream and
closing the unterminated bracket context (``{`` / ``[`` / ``"``).
The repair is conservative — it never invents data, only adds
closing punctuation. If repair succeeds, the parsed dict reflects
exactly what the LLM emitted; if it fails, the caller still gets a
``JSONDecodeError`` and can decide whether to retry, fall back, or
propagate.

Design choices
--------------
* We deliberately do NOT swap providers here. Provider-fallback on
  transient JSON failures is a separate concern (see
  ``coding_tool``'s timeout-retry path); this module's job is to
  make the most of one provider's reply before giving up.
* We DO preserve ``atomic_write_json`` at the persistence layer
  (see ``utils.atomic_io``) — even if repair yields a valid dict,
  the write is still atomic; torn writes never reach disk.

Usage::

    from utils.json_repair import parse_llm_json

    parsed = parse_llm_json(llm_reply)  # raises JSONDecodeError on hard failure

    # With fallback:
    parsed = parse_llm_json(llm_reply, fallback={"findings": []})
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


# v20 (ported from a sibling checkout commit 10f0df2): strip raw
# control characters from the LLM reply BEFORE any other parsing step.
#
# In M1 interactive mode the LLM sometimes echoes raw binary file
# content (e.g. ``Read`` tool output for a non-UTF-8 file) into the
# JSON response, raising ``Invalid control character`` on
# ``json.loads``. Python rejects unescaped control characters in
# string values. Strip ALL control characters (chr 0-31 except
# ``\t \n \r``, plus 0x7f DEL) so the JSON parses cleanly.
#
# This is conservative — the LLM is supposed to emit valid UTF-8
# JSON, so any control character that survived is a side effect of
# the tool chain, not intentional data.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_characters(text: str) -> str:
    """Remove raw control characters from the LLM reply.

    Preserves ``\\t`` / ``\\n`` / ``\\r`` (legal in escaped JSON
    strings); strips every other byte in the 0x00-0x1f and 0x7f
    ranges. See the ``_CONTROL_CHARS_RE`` comment above for the
    rationale.
    """
    return _CONTROL_CHARS_RE.sub("", text)


def _strip_code_fence(text: str) -> str:
    """Remove surrounding Markdown code-fence markers if present.

    Recognises the ``\\`\\`\\`json ... \\`\\`\\`` and bare
    ``\\`\\`\\` ... \\`\\`\\`` forms, with optional prose on either
    side. Other shapes are passed through unchanged.

    Bug fix 2026-08-14 (smoke test): the original implementation
    scanned the entire text for the first `` ``` `` and treated it
    as a fence opener, then matched it to the *next* `` ``` ``.
    That was wrong when the JSON document itself contains `` ``` ``
    inside a string field (LLMs routinely emit Markdown-fenced
    examples inside ``description`` strings, e.g.
    ``"## 输入示例\n```json\n{...}\n```"``). The second `` ``` ``
    then became the "closer", and everything between the LLM's
    opening ``{`` and the *next* string-internal `` ``` `` was
    dropped — producing the empty / truncated parse failures seen
    by ``tasks_generator.query_json``.

    Fix: only strip fences when the first `` ``` `` appears *before*
    the first ``{`` / ``[`` in the text. If the JSON starts first,
    any `` ``` `` in the body is by definition inside a string and
    must be left alone. This matches the LLM convention of
    prose-then-fence (``"Here's the plan:\n```json\n{...}\n```"``)
    and rejects the broken JSON-then-fence case.
    """
    stripped = text.strip()
    if "```" not in stripped:
        return text

    # Find the first code fence and the matching closing fence.
    first = stripped.find("```")
    if first == -1:
        return text

    # Bug fix (2026-08-14): if the first JSON opener (``{`` / ``[``)
    # appears BEFORE the first `` ``` ``, the fence opener is inside
    # the JSON (in a string field) — leave the text alone and let
    # ``_extract_json_slice`` walk past it via ``raw_decode``.
    json_opener_idx = stripped.find("{")
    if json_opener_idx == -1:
        json_opener_idx = stripped.find("[")
    if json_opener_idx != -1 and first > json_opener_idx:
        # The JSON opens before the first fence marker — the fence
        # is part of the JSON document, not a wrapping construct.
        return text

    # Skip the opening fence and its optional language tag.
    body_start = first + 3
    # Advance past the language tag (e.g. "json") on the same line.
    nl = stripped.find("\n", body_start)
    if nl == -1:
        # Single-line fenced block — treat the rest as the body.
        body_start = body_start
    else:
        body_start = nl + 1

    # Find the closing fence.
    closing = stripped.find("```", body_start)
    if closing == -1:
        # Unterminated fence — best-effort: return everything after
        # the opening fence, stripped.
        return stripped[body_start:].strip()

    return stripped[body_start:closing].strip()


def _extract_json_slice(text: str) -> Optional[str]:
    """Locate the first complete JSON object/array in ``text``.

    Uses :func:`json.JSONDecoder.raw_decode` to walk past the first
    *complete* top-level value. The earlier implementation looked
    for the first ``{`` (or ``[``) up to the last ``}`` / ``]``,
    which concatenated multiple top-level objects when the LLM
    emitted something like ``{"a":1}{"b":2}`` and then raised
    ``JSONDecodeError: Extra data`` — the parser saw valid JSON
    followed by trailing garbage.

    Two modes:

    * **Closed** (no truncation): the decoder walks past the
      first complete object; trailing content is silently dropped.
      Handles "JSON wrapped in conversational prose" and
      "primary-JSON + trailing-JSON" by keeping only the first.

    * **Open-ended** (likely truncation): if no closing bracket
      exists but a bracket opener does, returns the substring
      from the opener to the end of ``text``. The repair walker
      then closes the bracket/quote stack.
    """
    decoder = json.JSONDecoder()
    # Try to find a complete top-level value anywhere in the text.
    for opener in ("{", "["):
        opener_pos = text.find(opener)
        if opener_pos == -1:
            continue
        try:
            obj, end = decoder.raw_decode(text, opener_pos)
            return text[opener_pos:end]
        except json.JSONDecodeError:
            pass

    # No complete value parsed. Try open-ended (truncation case).
    for opener in ("{", "["):
        opener_pos = text.find(opener)
        if opener_pos != -1:
            return text[opener_pos:]
    return None


def _try_repair_truncated_json(text: str) -> Optional[str]:
    """Best-effort repair of an LLM reply that was truncated mid-token.

    Strategy:

    1. Try ``json.loads`` straight. If it works, return as-is.
    2. Otherwise walk ``text`` character-by-character with an escape-
       aware state machine (string context + bracket stack). At the
       end-of-input, close any open string and any open brackets,
       and inject ``null`` if the last non-whitespace char was a
       dangling key separator ``:`` (Python's JSON parser rejects
       ``{"a":}``).
    3. Try parsing the result. If it still fails, trim trailing
       partial tokens (where JSON would expect punctuation like
       ``,`` ``:`` or whitespace) and retry, up to 4 trims.

    Returns the repaired string or ``None`` if no parseable
    fragment can be recovered (e.g. structural corruption deep
    inside a string literal).

    Conservative: the only data this function invents is a single
    ``null`` value when the LLM emitted a key separator with no
    value. Otherwise it only adds closing punctuation or trims
    partial tokens at the tail.
    """
    # Step 1: already valid?
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Step 2: walk + close open contexts (handles dangling colon).
    candidate = _close_open_contexts(text)

    # Step 3: parse; if it still fails, trim trailing partial tokens.
    for _ in range(4):
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            if len(candidate) < 2:
                break
            candidate = candidate[:-1]

    return None


def _close_open_contexts(text: str) -> str:
    """Append the closing punctuation that ``text`` is missing.

    Scans ``text`` to determine the unterminated bracket/quote state
    at the end-of-string, then appends the right number of ``"``,
    ``]``, ``}`` in LIFO order.

    Note on dangling colons: Python's ``json`` parser rejects
    ``{"a":}`` (a key with no value), so this function also appends
    a ``null`` placeholder whenever the last non-whitespace token
    is a key separator ``:``. This is the only place the walker
    invents data; without it, short-form truncations like
    ``{"findings":`` cannot be repaired.
    """
    open_stack: list[str] = []
    in_string = False
    escape_next = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "{":
            open_stack.append("}")
            i += 1
            continue
        if ch == "[":
            open_stack.append("]")
            i += 1
            continue
        if ch == "}" or ch == "]":
            if open_stack and open_stack[-1] == ch:
                open_stack.pop()
            i += 1
            continue
        i += 1

    # Build the closing suffix.
    suffix_parts: list[str] = []
    # If we ended inside an unterminated string, close it.
    if in_string:
        suffix_parts.append('"')

    # If the last non-whitespace char BEFORE our new suffix is a
    # dangling punctuation (``:`` needs a value, ``,`` would be
    # followed by an element that never came), handle it:
    #   * ``:`` → inject ``null``
    #   * ``,`` → drop it (no element to follow)
    # Python's JSON parser rejects both raw.
    if not in_string:
        last_non_ws = ""
        for ch in reversed(text):
            if not ch.isspace():
                last_non_ws = ch
                break
        if last_non_ws == ":":
            suffix_parts.append("null")
        elif last_non_ws == ",":
            # Strip the trailing comma from ``text`` before
            # appending the closing suffix.
            text = text.rstrip().rstrip(",").rstrip()

    suffix_parts.extend(reversed(open_stack))
    return text + "".join(suffix_parts)


def parse_llm_json(
    text: str,
    fallback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse an LLM JSON reply, attempting local repair on failure.

    Behaviour matrix:

    ============  ===========  =========  ===========
    input         boundaries?  parse OK?  result
    ============  ===========  =========  ===========
    valid JSON    yes          yes        parsed dict
    prose-wrapped yes          yes        parsed dict
    truncated     yes          no         repair → parsed dict
    unrepairable  yes          no         raise JSONDecodeError
    no boundaries  no          n/a        raise JSONDecodeError
    empty         n/a          n/a        raise JSONDecodeError
    ============  ===========  =========  ===========

    Args:
        text: The raw LLM reply (string).
        fallback: Optional default to return instead of raising when
            repair fails. Use sparingly — silent fallbacks can mask
            real LLM regressions.

    Returns:
        The parsed dict.

    Raises:
        json.JSONDecodeError: when the input is empty, lacks any
            JSON-shaped region, or cannot be repaired back into a
            valid JSON document.
        ValueError: when the input is not a string.
    """
    if not isinstance(text, str):
        raise ValueError(f"parse_llm_json: expected str, got {type(text).__name__}")

    # v20: strip raw control characters before any other parsing step
    # so a stray ``\x00`` from a binary Read doesn't crash ``json.loads``.
    text = _strip_control_characters(text)
    stripped = _strip_code_fence(text)
    slice_ = _extract_json_slice(stripped)
    if slice_ is None:
        if fallback is not None:
            return fallback
        raise json.JSONDecodeError(
            "no JSON object / array boundaries found", text, 0
        )

    try:
        return json.loads(slice_)
    except json.JSONDecodeError:
        repaired = _try_repair_truncated_json(slice_)
        if repaired is not None:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        if fallback is not None:
            return fallback
        # Re-raise the original (not the repaired) error — its
        # ``pos`` points at where the LLM truncated, which is the
        # most actionable diagnostic.
        raise