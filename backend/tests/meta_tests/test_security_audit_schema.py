r"""Meta-test pinning the SECURITY_AUDIT.md schema and five-element entry shape.

The audit document is the authoritative deliverable of this codebase's
security sweep — every concrete finding lands here and every "blocker
fixed" acceptance bullet is checked against an entry in this file.

This module pins three things so that subsequent tasks (14/15/16) can
build on the same parser and the same structure:

1. ``FACES`` — the canonical five surface sections the document must
   contain.  Adding a sixth face would silently orphan every entry
   written under the new name; this module pins the contract.
2. ``TIERS`` — the canonical severity ordering (``blocker`` before
   ``should-fix`` before ``note``).  Anything else would break the
   per-tier acceptance bullet.
3. ``parse_entries(text)`` — the single entry parser reused by tasks
   14/15/16.  An entry is the block beginning with
   ``### Finding ENTRY-NNN`` and ending at the next ``### Finding``
   (or end of text).  Each parsed entry exposes ``problem``,
   ``impact``, ``attack_path``, ``fix``, ``verification`` and
   ``tier`` keys, plus ``valid: bool`` and a list of ``reasons`` for
   why the entry failed validation.

Boundary conditions covered:

* Empty document / headings only -> no ``### Finding`` entries parsed
  -> no entries to validate, so section-coverage test fails instead of
  silently passing on zero entries.
* ``attack_path`` missing any of 前置条件 / 触发步骤 / 可观测后果
  -> entry is marked ``valid=False`` with a reason naming the missing
  sub-section.
* ``verification`` not wrapped in a ``\`\`\`bash ... \`\`\`` code block
  -> entry is marked ``valid=False`` with a reason naming the missing
  command block.
* ``/Users/a/b``, ``example.com``, etc. are example placeholders in
  the audit body and must NOT trip the parser.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

# Locate the repository root (this file lives at
# backend/tests/meta_tests/test_security_audit_schema.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUDIT_PATH = _REPO_ROOT / "SECURITY_AUDIT.md"

# Five face sections — order is significant because each task reuses
# the same names as headings in the rendered document.
FACES: tuple[str, ...] = (
    "入口面",
    "路由与执行面",
    "前端调用面",
    "配置与样例面",
    "工程面",
)

# Severity tiers, ordered most → least severe so that the per-tier
# acceptance bullets can iterate in a stable sequence.
TIERS: tuple[str, ...] = (
    "blocker",
    "should-fix",
    "note",
)

# Required keys on every parsed entry dict (the "five elements").
_REQUIRED_ENTRY_KEYS: tuple[str, ...] = (
    "problem",
    "impact",
    "attack_path",
    "fix",
    "verification",
)

# The three sub-sections that must appear inside an entry's
# ``攻击路径:`` paragraph.  Order is preserved in error messages.
_ATTACK_PATH_SUBSECTIONS: tuple[str, ...] = (
    "前置条件",
    "触发步骤",
    "可观测后果",
)

# Single-line field headings (besides `档位`, which has its own
# extractor).  Used both to close the multi-line ``攻击路径``
# paragraph and to extract simple scalar values.
_SIMPLE_FIELDS: tuple[str, ...] = ("问题", "影响", "修复")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Match the entry header — `### Finding ENTRY-NNN` (case-sensitive on
# the ENTRY prefix so accidental prose like "### Findings" does not
# get pulled in as an entry).
_ENTRY_HEADER_RE = re.compile(
    r"^###\s+Finding\s+(?P<entry_id>ENTRY-[A-Za-z0-9_-]+)\s*$",
    re.MULTILINE,
)

# `档位:` line — must be one of TIERS, otherwise the entry is invalid.
_TIER_LINE_RE = re.compile(r"^\s*档位\s*:\s*(?P<tier>\S+)\s*$", re.MULTILINE)

# `攻击路径:` line opens the multi-line attack-path paragraph.  It
# closes at the next field line or the next heading, whichever comes
# first.
_ATTACK_PATH_OPEN_RE = re.compile(r"^\s*攻击路径\s*:\s*$", re.MULTILINE)


def _slice_entry_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``[(entry_id, body), ...]`` for every entry in *text*.

    *body* is the text between the entry header and the next heading
    at the same or deeper level (or end of file).  An empty body is
    preserved — the caller decides whether that's a problem.
    """
    blocks: list[tuple[str, str]] = []
    matches = list(_ENTRY_HEADER_RE.finditer(text))
    for idx, match in enumerate(matches):
        entry_id = match.group("entry_id")
        start = match.end()
        # Find the next line beginning with `###` or deeper — either
        # the next entry's header or another section heading.
        next_header = re.search(r"^#{3,}\s+", text[start:], re.MULTILINE)
        end = start + next_header.start() if next_header else len(text)
        blocks.append((entry_id, text[start:end]))
    return blocks


def _extract_field_value(body: str, field_name: str) -> str | None:
    """Return the value of a single-line ``<field_name>: ...`` field.

    Only used for single-line fields (``问题``, ``影响``, ``修复``,
    ``档位``).  ``攻击路径`` and ``验证方式`` span multiple lines and
    have their own extractors.
    """
    pattern = re.compile(
        r"^\s*" + re.escape(field_name) + r"\s*:\s*(?P<value>[^\n]+?)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(body)
    if not match:
        return None
    return match.group("value").strip()


def _extract_attack_path(body: str) -> tuple[str, list[str]]:
    r"""Return ``(raw_text, missing_subsections)`` for the attack path.

    The attack-path paragraph runs from the ``攻击路径:`` marker until
    the next single-line field heading (``问题``, ``影响``, ``修复``,
    ``验证方式``, ``档位``) or the next ``###`` heading, whichever
    comes first.

    Two layouts are accepted:

    * inline (default): the sub-sections are on the same line as the
      marker — ``攻击路径: 前置条件 ...； 触发步骤 ...； 可观测后果 ...``.
      This is the layout the rest of this codebase uses (single-line
      attack-path summary, terse enough to scan in a PR diff).
    * block: the marker is alone on its line and the sub-sections
      follow on subsequent lines.  We support this for completeness
      but do not require it.

    The extracted ``raw_text`` is checked for all three required
    sub-section tokens (``前置条件``, ``触发步骤``, ``可观测后果``);
    any missing token is reported in ``missing_subsections``.
    """
    # Match the marker with optional content on the same line — we
    # capture everything from the colon onward, then trim the marker
    # prefix so the returned text starts at the first sub-section.
    open_re = re.compile(
        r"^\s*攻击路径\s*:\s*(?P<inline>.*?)$",
        re.MULTILINE,
    )
    open_match = open_re.search(body)
    if not open_match:
        return "", list(_ATTACK_PATH_SUBSECTIONS)
    start = open_match.end()
    # If the inline content carries everything (the common case), the
    # raw text is just the inline content.  If the marker was alone
    # on its line, scan forward to the closer.
    inline_content = open_match.group("inline").strip()
    if inline_content:
        raw = inline_content
    else:
        # Block layout: collect from the next line forward.
        field_names = list(_SIMPLE_FIELDS) + ["验证方式", "档位"]
        field_alt = "|".join(re.escape(n) for n in field_names)
        closer_re = re.compile(
            r"^\s*(?:" + field_alt + r")\s*:"
            + r"|^\s*#{3,}\s+",
            re.MULTILINE,
        )
        closer = closer_re.search(body, pos=start)
        end = closer.start() if closer else len(body)
        raw = body[start:end].strip()

    missing: list[str] = []
    for sub in _ATTACK_PATH_SUBSECTIONS:
        if sub not in raw:
            missing.append(sub)
    return raw, missing


def _extract_verification(body: str) -> tuple[str, str]:
    r"""Return ``(raw_text, fenced_lang)`` for the verification block.

    The block is fenced as ``\`\`\`bash ... \`\`\``.  We require the
    fence to be ``bash`` (the contract pins the verification stack to
    pytest invocations).  If the fence is missing or the language tag
    is anything else, the caller marks the entry invalid.
    """
    pattern = re.compile(
        r"^\s*验证方式\s*:\s*$\s*"
        r"(?P<block>```(?P<lang>[A-Za-z0-9_-]*)\n.*?\n```)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return "", ""
    return match.group("block"), match.group("lang").strip()


def parse_entries(text: str) -> list[dict]:
    """Parse every ``### Finding ENTRY-NNN`` block in *text*.

    Each returned dict has the keys::

        entry_id       — e.g. "ENTRY-001"
        problem        — value of the `问题:` line
        impact         — value of the `影响:` line
        attack_path    — raw text of the `攻击路径:` paragraph
        fix            — value of the `修复:` line
        verification   — text of the `验证方式:` fenced code block
        verification_lang — language tag of the fence (must be "bash")
        tier           — value of the `档位:` line, if present and valid
        valid          — True iff every check below passed
        reasons        — list of human-readable failure reasons

    The parser is deliberately tolerant of Markdown noise (extra blank
    lines, trailing whitespace, indented code blocks).  It is NOT
    tolerant of structural omissions — missing fields, missing attack
    sub-sections, or a non-bash verification fence all mark the entry
    ``valid=False`` with a reason.
    """
    out: list[dict] = []
    for entry_id, body in _slice_entry_blocks(text):
        problem = _extract_field_value(body, "问题") or ""
        impact = _extract_field_value(body, "影响") or ""
        attack_path, missing_subs = _extract_attack_path(body)
        fix = _extract_field_value(body, "修复") or ""
        verification, lang = _extract_verification(body)
        tier_value = _extract_field_value(body, "档位") or ""

        reasons: list[str] = []
        if not problem:
            reasons.append("missing `问题:` line")
        if not impact:
            reasons.append("missing `影响:` line")
        if not fix:
            reasons.append("missing `修复:` line")
        if missing_subs:
            reasons.append(
                "attack_path missing sub-section(s): " + ", ".join(missing_subs)
            )
        if not verification:
            reasons.append("verification is not wrapped in a fenced code block")
        elif lang != "bash":
            reasons.append(
                f"verification fence is ```{lang}```, contract requires ```bash```"
            )
        if tier_value not in TIERS:
            reasons.append(
                f"tier {tier_value!r} is not one of {list(TIERS)}"
            )

        out.append(
            {
                "entry_id": entry_id,
                "problem": problem,
                "impact": impact,
                "attack_path": attack_path,
                "fix": fix,
                "verification": verification,
                "verification_lang": lang,
                "tier": tier_value,
                "valid": not reasons,
                "reasons": reasons,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Meta-tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audit_text() -> str:
    """Read ``SECURITY_AUDIT.md`` from the repo root.

    Skipped if the file does not yet exist — the gate is supposed to
    *fail* when the file is missing, not error on a missing path.
    A separate test (``test_audit_doc_exists_and_is_english``) is the
    canonical "does the file exist?" gate; here we only assert on the
    *content* of an existing file.
    """
    if not _AUDIT_PATH.exists():
        pytest.skip(f"{_AUDIT_PATH} not created yet")
    return _AUDIT_PATH.read_text(encoding="utf-8")


def test_audit_doc_exists_and_is_english() -> None:
    """``SECURITY_AUDIT.md`` must exist and be predominantly English.

    This is the first gate of the audit sweep — every later gate
    reads the file through this fixture, so a missing file would
    cascade into confusing skip messages instead of a loud failure.
    """
    assert _AUDIT_PATH.exists(), (
        f"authoritative audit deliverable {_AUDIT_PATH} is missing — "
        "create it before claiming any 'blocker fixed' acceptance bullet"
    )
    text = _AUDIT_PATH.read_text(encoding="utf-8")
    assert text.strip(), "SECURITY_AUDIT.md is empty"

    # Strip Markdown punctuation and Chinese face-section identifiers
    # (those are *contractual* tokens, not prose), then count ASCII
    # letters.  At least 60% of remaining word tokens must be ASCII so
    # we accept headings and short summaries but reject a doc that's
    # only CJK prose.
    cleaned = re.sub(r"[`*#_|\-\[\](){}!.,?:;\n\r\t]", " ", text)
    # Drop the five face section tokens — they're identifiers, not prose.
    for face in FACES:
        cleaned = cleaned.replace(face, " ")
    tokens = [t for t in cleaned.split() if t]
    assert tokens, "no word tokens after stripping Markdown punctuation"
    ascii_tokens = sum(1 for t in tokens if all(ord(c) < 128 for c in t))
    ascii_ratio = ascii_tokens / len(tokens)
    assert ascii_ratio >= 0.60, (
        f"SECURITY_AUDIT.md is not predominantly English "
        f"(ASCII ratio {ascii_ratio:.2%} < 60%); rebuild the document body in English"
    )


def test_audit_sections_match_five_faces(audit_text: str) -> None:
    """Every face in ``FACES`` must appear as a section heading.

    We accept any heading level (``#`` through ``######``) — section
    depth is a stylistic choice the document author can make per
    section.  What matters is that all five names appear, in any
    order, somewhere in the document.
    """
    missing = [
        face
        for face in FACES
        if not re.search(rf"^#{{1,6}}\s+{re.escape(face)}\s*$", audit_text, re.MULTILINE)
    ]
    assert not missing, (
        f"SECURITY_AUDIT.md is missing face sections: {missing}; "
        f"every face in {list(FACES)} must appear as a heading"
    )


def test_every_entry_has_five_elements_and_tier(audit_text: str) -> None:
    """Every parsed entry must expose the five elements and a valid tier.

    If the document has zero entries the gate passes vacuously, which
    would be useless: combine this assertion with the prior section
    test (which would still fail on an empty doc) and a "at least one
    entry exists" check here so the gate has teeth.
    """
    entries = parse_entries(audit_text)
    assert entries, (
        "SECURITY_AUDIT.md contains no `### Finding ENTRY-NNN` entries; "
        "the audit document is empty even though the skeleton requires entries"
    )

    for entry in entries:
        # Tier must be present and one of TIERS.
        assert entry["tier"] in TIERS, (
            f"entry {entry['entry_id']} has tier {entry['tier']!r}; "
            f"must be one of {list(TIERS)}"
        )

        # Five-element contract: problem / impact / attack_path / fix /
        # verification must each be non-empty.
        for key in _REQUIRED_ENTRY_KEYS:
            assert entry[key], (
                f"entry {entry['entry_id']} is missing required element {key!r}"
            )

        # Attack path must contain the three sub-sections.
        for sub in _ATTACK_PATH_SUBSECTIONS:
            assert sub in entry["attack_path"], (
                f"entry {entry['entry_id']} attack_path is missing sub-section "
                f"{sub!r}; required sub-sections: {list(_ATTACK_PATH_SUBSECTIONS)}"
            )


def test_entry_without_command_block_is_rejected() -> None:
    """A synthetic entry whose 验证方式 is plain prose must be invalid.

    This pins the boundary condition from the task brief: the
    verification command is the only "execute-and-check" link between
    a finding and its acceptance bullet, so a fence-less verification
    cannot be enforced by automation and must not pass the schema gate.
    """
    sample = textwrap.dedent(
        """
        ### Finding ENTRY-SYN-001
        档位: blocker
        问题: synthetic
        影响: synthetic
        攻击路径: 前置条件 ...; 触发步骤 ...; 可观测后果 ...
        修复: synthetic
        验证方式: just run pytest, no fence
        """
    ).strip("\n")
    entries = parse_entries(sample)
    assert len(entries) == 1, (
        f"expected exactly 1 parsed entry, got {len(entries)}: {entries}"
    )
    entry = entries[0]
    assert not entry["valid"], (
        f"entry {entry['entry_id']} should be invalid (no ```bash fence) but was marked valid; "
        f"reasons: {entry['reasons']}"
    )
    # The reason must specifically call out the missing fenced block
    # — this is what makes the failure actionable in a PR review.
    assert any("fenced code block" in r for r in entry["reasons"]), (
        f"reasons should mention the missing fenced code block; got {entry['reasons']}"
    )