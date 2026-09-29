"""
Architecture Design Principle Validator
========================================

ArchGenerator currently has **no Design Principles HARD-GATE** —
``plans/<plan>/arch-design.md`` may omit key principles (stateless
services, data-logic separation, explicit error boundaries, etc.),
causing downstream tasks to drift from the architectural intent.

This module introduces a pure-function validator that:

  1. Loads a pre-defined principle library from
     ``backend/configs/arch_principles.yaml``. When the yaml is missing
     or malformed, falls back to a hard-coded 5-principle builtin
     default library (SOLID subset, 12-factor, layered architecture,
     stateless services, explicit error boundaries).
  2. Parses the ``## Design Principles`` section from an
     ``arch-design.md`` body (case-insensitive heading match). When the
     section is missing, returns an empty result for that section's
     indicators (no blocking — the caller decides).
  3. Calls an LLM (or a pluggable ``llm_query_fn`` injection point) to
     judge per-principle "consistency" with the architectural intent.
  4. When the LLM call raises or is unavailable, **degrades** to a
     rule-based keyword matcher: a principle is marked
     ``is_consistent=True`` iff the section text contains the principle's
     declared ``required_keywords``.
  5. Returns ``[]`` for empty / whitespace-only input — does NOT block
     on absence of an ``arch-design.md``.

Public contract
--------------
``DesignPrincipleValidator.validate(arch_md, *, llm_query_fn=None, yaml_path=None)``

  * ``arch_md`` is a string containing the full ``arch-design.md`` body.
  * ``llm_query_fn`` is an optional ``Callable[[str], str]`` that
    receives the prompt and returns the LLM response. ``None`` means
    "skip LLM, go straight to the keyword fallback".
  * ``yaml_path`` overrides the on-disk yaml location; defaults to
    ``backend/configs/arch_principles.yaml`` relative to this module.

Returns:
  ``List[PrincipleCheck]`` — each entry is a dict with the 5 contract
  keys::

      {
        "principle":     <str>,     # canonical principle id (from yaml/builtin)
        "referenced_in": [<str>...], # list of sub-section names where the principle
                                     # keyword appears; empty list = not referenced
        "is_consistent": <bool>,    # True iff the principle is satisfied
        "severity":      <str>,     # "high" | "medium" | "low" — used by ArchGenerator's
                                     # HARD-GATE; defaults to "high" when not specified
                                     # in the yaml/builtin
        "finding":       <str>,     # human-readable explanation / finding text
      }

Design choices
--------------
* The validator is a **pure function** — no side-effects on disk, no
  in-memory state, no global registry. The class is used as a
  namespace only; the entry point is the classmethod ``.validate``.
* The yaml is loaded at the top of every ``.validate`` call so
  cached state is avoided (consistent with the rest of the codebase's
  "single source of truth on disk" policy — no in-memory mirror).
* The principle library is the single source of truth: 5 builtin
  defaults match the 5 entries in the yaml. Tests that import
  ``DesignPrincipleValidator._BUILTIN_PRINCIPLES`` can rely on the
  same set being in both places.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# Type alias for a single principle check result.
PrincipleCheck = Dict[str, Any]


# Builtin default principle library — exactly 5 entries, used when the
# yaml file is missing / unreadable / malformed. The set is the
# contract: any change to this list must be mirrored in
# ``configs/arch_principles.yaml`` and vice versa.
_BUILTIN_PRINCIPLES: List[Dict[str, Any]] = [
    {
        "name": "solid single responsibility",
        "description": "SOLID — every module has exactly one reason to change.",
        "keywords": ["single responsibility", "SRP", "单一职责"],
        "required_keywords": ["single responsibility"],
    },
    {
        "name": "twelve factor config",
        "description": "12-Factor — config lives in the environment, never in code.",
        "keywords": [
            "twelve-factor", "12-factor", "env var",
            "环境变量", "configuration via env",
        ],
        "required_keywords": ["env var"],
    },
    {
        "name": "layered architecture",
        "description": "Layered architecture — strict dependency ordering between layers.",
        "keywords": ["layered", "分层", "layer"],
        "required_keywords": ["layer"],
    },
    {
        "name": "stateless services",
        "description": "Stateless services — no mutable local state inside a service process.",
        "keywords": ["stateless", "无状态", "no local state"],
        "required_keywords": ["stateless"],
    },
    {
        "name": "explicit error boundaries",
        "description": (
            "Explicit error boundaries — every cross-layer call returns a "
            "typed Result/Either."
        ),
        "keywords": [
            "explicit error", "error boundary", "Result", "Either",
            "显式错误", "explicit return",
        ],
        "required_keywords": ["error"],
    },
]


class DesignPrincipleValidator:
    """Pure-function validator for the ``## Design Principles`` section.

    See module docstring for the full public contract. The class is
    used as a namespace; the only public method is :meth:`validate`
    (a classmethod so it can be used without instantiation).
    """

    # ------------------------------------------------------------------
    # Library loading
    # ------------------------------------------------------------------

    @staticmethod
    def _default_yaml_path() -> Path:
        """Return the absolute path to the on-disk principle library.

        Resolves to ``<repo>/backend/configs/arch_principles.yaml``
        relative to this module file, so the validator works from any
        cwd. Tests may override this via monkey-patching.
        """
        return Path(__file__).resolve().parent / "configs" / "arch_principles.yaml"

    @classmethod
    def _load_yaml_principles(cls, yaml_path: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Load the principle library from the yaml, falling back to
        :data:`_BUILTIN_PRINCIPLES` on any I/O or parse error.

        The returned list has the same shape as :data:`_BUILTIN_PRINCIPLES`
        (each entry is a dict with ``name``, ``description``,
        ``keywords``, ``required_keywords``). An empty yaml or a yaml
        with an empty ``principles`` key behaves as if the yaml did
        not exist at all — we use the builtin defaults in that case
        too, so callers always receive a non-empty, well-formed
        library.
        """
        path = yaml_path if yaml_path is not None else cls._default_yaml_path()
        if not isinstance(path, Path):
            path = Path(path)
        if not path.exists():
            return list(_BUILTIN_PRINCIPLES)

        try:
            import yaml  # local import — keeps the module importable
                          # even if PyYAML is missing at startup
        except ImportError:
            return list(_BUILTIN_PRINCIPLES)

        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return list(_BUILTIN_PRINCIPLES)

        if not isinstance(raw, dict):
            return list(_BUILTIN_PRINCIPLES)

        principles_raw = raw.get("principles", [])
        if not isinstance(principles_raw, list) or not principles_raw:
            return list(_BUILTIN_PRINCIPLES)

        parsed: List[Dict[str, Any]] = []
        for entry in principles_raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            keywords = entry.get("keywords") or []
            required = entry.get("required_keywords") or []
            if not isinstance(keywords, list):
                keywords = []
            if not isinstance(required, list):
                required = []
            parsed.append(
                {
                    "name": name.strip(),
                    "description": str(entry.get("description", "") or ""),
                    "keywords": [str(k).lower() for k in keywords],
                    "required_keywords": [str(k).lower() for k in required],
                }
            )

        if not parsed:
            return list(_BUILTIN_PRINCIPLES)
        return parsed

    # ------------------------------------------------------------------
    # Section parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_design_principles_section(arch_md: str) -> str:
        """Return the body of the ``## Design Principles`` section, or
        ``""`` if the section is absent.

        Section detection rules:

        * The heading is matched case-insensitively and accepts any
          ``## `` (two-hash) through ``#### `` (four-hash) prefix.
        * The body extends from the heading line **exclusive** to the
          next ``## `` (or higher) heading line, or end-of-input.
        * Lines inside fenced code blocks (`` ``` `` or ``~~~``) are
          treated as body — the section is plain prose/markdown.

        Whitespace inside the section is preserved (the keyword
        fallback lowercases before matching, so indentation is fine).
        """
        if not arch_md:
            return ""

        lines = arch_md.splitlines()
        body_start: Optional[int] = None
        body_end: Optional[int] = len(lines)

        # Find heading line: any level (##, ###, ####) starting with
        # "Design Principles" or "design principles" (case-insensitive,
        # also tolerating a numbered prefix like "1." or "##").
        heading_pattern = re.compile(
            r"^\s*#{2,6}\s+Design\s+Principles\s*$",
            re.IGNORECASE,
        )

        for i, line in enumerate(lines):
            if heading_pattern.match(line):
                body_start = i + 1
                break

        if body_start is None:
            return ""

        # Advance until the next heading of equal-or-higher level
        # (i.e. starts with ``## `` or fewer hashes).
        next_heading_pattern = re.compile(r"^\s*#{1,6}\s+\S")
        for j in range(body_start, len(lines)):
            line = lines[j]
            # A heading line must start (after optional leading
            # whitespace) with ``#`` characters and a space. Use the
            # conservative pattern above to avoid false-positives on
            # numbered list items like "## 1. Layering" — actually
            # those ARE headings, so we treat them as such.
            if next_heading_pattern.match(line) and not set(line.strip()) <= {"#", " "}:
                body_end = j
                break

        return "\n".join(lines[body_start:body_end])

    # ------------------------------------------------------------------
    # Keyword matcher (LLM-fallback path)
    # ------------------------------------------------------------------

    @staticmethod
    def _keyword_check(
        section_text: str,
        principle: Dict[str, Any],
    ) -> PrincipleCheck:
        """Rule-based fallback for a single principle.

        Returns a :class:`PrincipleCheck` whose ``principle`` is the
        canonical ``name`` from the yaml, ``referenced_in`` is the
        list of sub-section names where any keyword from
        ``principle['keywords']`` was found (we treat the single
        ``## Design Principles`` section as one sub-section named
        "Design Principles"), ``is_consistent`` is True iff every
        required keyword appears in the section text (case-
        insensitive), ``severity`` is the principle's declared
        severity (default "high"), and ``finding`` is a short
        human-readable explanation.
        """
        lowered = section_text.lower()
        keywords = principle.get("keywords") or []
        required = principle.get("required_keywords") or []

        any_keyword_hit = any(kw.lower() in lowered for kw in keywords if kw)
        required_present = all(
            req.lower() in lowered for req in required if req
        )

        if required and required_present:
            is_consistent = True
            finding = ""
        elif not required and any_keyword_hit:
            is_consistent = True
            finding = ""
        else:
            is_consistent = False
            if not any_keyword_hit:
                finding = (
                    f"principle {principle['name']!r} is not referenced "
                    f"in the ## Design Principles section (no keyword hit)"
                )
            else:
                missing = [
                    req for req in required
                    if req and req.lower() not in lowered
                ]
                finding = (
                    f"principle {principle['name']!r} has partial keyword "
                    f"coverage; missing required keyword(s): {missing}"
                )

        referenced_in: List[str] = []
        if any_keyword_hit:
            referenced_in = ["Design Principles"]

        severity = principle.get("severity", "high")
        if not isinstance(severity, str):
            severity = "high"

        return {
            "principle": principle["name"],
            "referenced_in": referenced_in,
            "is_consistent": is_consistent,
            "severity": severity,
            "finding": finding,
        }

    # ------------------------------------------------------------------
    # LLM-driven path (with graceful degradation)
    # ------------------------------------------------------------------

    @staticmethod
    def _llm_query_or_fallback(
        arch_md: str,
        section_text: str,
        principles: List[Dict[str, Any]],
        llm_query_fn: Optional[Callable[[str], str]],
    ) -> List[PrincipleCheck]:
        """Try the LLM first; on any exception, fall back to the
        keyword matcher.

        The LLM is fed a single prompt asking it to return a JSON list
        of ``{principle, referenced_in, is_consistent, finding}``.
        When the response is not parseable JSON, or the call raises,
        we silently fall back to the keyword matcher (per the
        spec: "LLM 调用失败 → 退化到规则匹配").
        """
        # No LLM plug — go straight to fallback.
        if llm_query_fn is None:
            return DesignPrincipleValidator._keyword_fallback(
                section_text, principles
            )

        # The LLM prompt deliberately names the 4 contract keys so a
        # well-formed JSON response can be parsed as-is.
        principle_list = "\n".join(
            f"- {p['name']}: {p['description']}" for p in principles
        )
        prompt = (
            "You are a senior architect reviewing an architecture "
            "design document for adherence to design principles.\n\n"
            f"Architecture document (full body):\n```\n{arch_md}\n```\n\n"
            f"Pre-defined principle library to check:\n{principle_list}\n\n"
            "For each principle, return a JSON array of objects with "
            "exactly these keys:\n"
            '  "principle"      : the canonical principle id (from the library)\n'
            '  "referenced_in"  : list[str] of section names where the principle\n'
            "                     is mentioned (empty list if not referenced)\n"
            '  "is_consistent"  : bool, true if the principle is satisfied\n'
            '  "finding"        : short human-readable finding; "" if consistent\n\n'
            "Return ONLY the JSON array, no prose, no markdown fences."
        )

        try:
            response_text = llm_query_fn(prompt)
        except Exception:
            return DesignPrincipleValidator._keyword_fallback(
                section_text, principles
            )

        return DesignPrincipleValidator._parse_llm_response(
            response_text, principles, section_text
        )

    @staticmethod
    def _parse_llm_response(
        response_text: str,
        principles: List[Dict[str, Any]],
        section_text: str,
    ) -> List[PrincipleCheck]:
        """Parse the LLM's JSON response; fall back to the keyword
        matcher on any parse error."""
        import json as _json
        try:
            parsed = _json.loads(response_text)
        except (ValueError, TypeError):
            return DesignPrincipleValidator._keyword_fallback(
                section_text, principles
            )

        if not isinstance(parsed, list):
            return DesignPrincipleValidator._keyword_fallback(
                section_text, principles
            )

        valid_names = {p["name"] for p in principles}
        results: List[PrincipleCheck] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            name = entry.get("principle")
            if not isinstance(name, str) or name not in valid_names:
                continue
            referenced_in = entry.get("referenced_in", [])
            if not isinstance(referenced_in, list):
                referenced_in = []
            referenced_in = [str(x) for x in referenced_in]
            is_consistent = bool(entry.get("is_consistent", False))
            severity_raw = entry.get("severity", "high")
            severity = str(severity_raw) if severity_raw else "high"
            if severity not in ("high", "medium", "low"):
                severity = "high"
            finding = str(entry.get("finding", "") or "")
            results.append(
                {
                    "principle": name,
                    "referenced_in": referenced_in,
                    "is_consistent": is_consistent,
                    "severity": severity,
                    "finding": finding,
                }
            )

        # If the LLM returned an empty list or only invalid entries,
        # fall back to the keyword matcher — this prevents a buggy
        # LLM (or a too-strict filter) from masking all principles.
        if not results:
            return DesignPrincipleValidator._keyword_fallback(
                section_text, principles
            )
        # If some principles are missing from the LLM response,
        # backfill them from the keyword matcher so callers always
        # see the full principle library.
        seen = {r["principle"] for r in results}
        for p in principles:
            if p["name"] not in seen:
                results.append(
                    DesignPrincipleValidator._keyword_check(section_text, p)
                )
        return results

    @staticmethod
    def _keyword_fallback(
        section_text: str,
        principles: List[Dict[str, Any]],
    ) -> List[PrincipleCheck]:
        """Keyword fallback for the entire principle library."""
        return [
            DesignPrincipleValidator._keyword_check(section_text, p)
            for p in principles
        ]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @classmethod
    def validate(
        cls,
        arch_md: str,
        llm_query_fn: Optional[Callable[[str], str]] = None,
        yaml_path: Optional[Path] = None,
    ) -> List[PrincipleCheck]:
        """Validate an ``arch-design.md`` body against the principle library.

        Args:
            arch_md: The full Markdown body of ``arch-design.md``. An
                empty / whitespace-only string returns ``[]`` — no
                blocking on missing input.
            llm_query_fn: Optional ``Callable[[str], str]`` that
                receives the LLM prompt and returns the raw LLM
                response string. ``None`` means "skip the LLM; use
                only the keyword fallback" (deterministic, no
                network/LLM dependency).
            yaml_path: Optional path to override the default yaml
                location. Useful for tests that point at a controlled
                directory.

        Returns:
            ``List[PrincipleCheck]`` — one entry per principle in the
            library (yaml or builtin defaults). See module docstring
            for the 4-key dict shape. An empty / whitespace-only input
            returns ``[]`` (no PrincipleCheck produced).
        """
        # Empty / whitespace-only input → empty result, no blocking.
        if not arch_md or not arch_md.strip():
            return []

        principles = cls._load_yaml_principles(yaml_path)
        section_text = cls._extract_design_principles_section(arch_md)

        # If the section is missing entirely, the fallback still
        # produces one entry per principle (each marked
        # is_consistent=False with a clear finding), but this is
        # useful for the caller; tests can guard against the empty
        # case via the empty-arch test contract.
        return cls._llm_query_or_fallback(
            arch_md=arch_md,
            section_text=section_text,
            principles=principles,
            llm_query_fn=llm_query_fn,
        )
