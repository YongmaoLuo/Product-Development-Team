"""Every ``fetch(`` call in ``frontend/`` must go through the ``api()`` or
``apiRequest()`` wrapper so the request guard header is sent.

Why this gate exists
--------------------
``backend/request_guard.py`` answers only requests that carry
``X-PDT-Request: 1``. The header is a *non-simple* CORS header, which
forces a preflight the server never grants — so the loopback API is
reachable only from a page this UI owns. The header is currently
attached at exactly two call sites: ``frontend/api.js``'s
``apiRequest(...)`` and ``frontend/app.js``'s ``api(...)``. Every
other module must reach the API through one of those wrappers.

Adding a bare ``const r = await fetch('/api/...')`` somewhere is a
one-keystroke accident that does not break at build time and does not
break in tests — the failure is a 403 the next time the page tries the
URL. The author usually does not know why their request "stopped
working" and removes whatever they touched. Pinning the rule in
source turns the same mistake into a loud test failure on the next
``git push``.

The rule
--------
``ALLOWED_FETCH_FILES`` — files whose entire content is allowed to
contain ``fetch(`` because *they are* the wrapper (currently only
``frontend/api.js``).

``ALLOWED_FETCH_FUNCTIONS`` — function names whose bodies may contain
``fetch(`` because the wrapper itself lives there (currently only
``api`` in ``frontend/app.js``).

Any ``fetch(`` outside those allowances — another function, a top-level
expression, a fresh module — is a violation. Matches inside ``//`` line
comments, ``/* ... */`` block comments, and string literals
(``'...'``, ``"..."``, ``\\`...\\```) are not flagged: they are not real
call sites, and flagging them would force documentation to lie about
exactly the API surface it exists to document.

The scan target is the gate itself: ``frontend/*.js``. ``index.html``
and ``style.css`` are not JavaScript; vendored caches like
``backend/.venv`` are never in this audit.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_DIR = _REPO_ROOT / "frontend"


# ---------------------------------------------------------------------------
# Allowances
# ---------------------------------------------------------------------------

#: Files whose entire content may contain ``fetch(`` because the
#: file *is* the wrapper. The rule is "every other module goes
#: through these", and exempting the wrapper file keeps the gate
#: idempotent — the regex must not flag the very line that
#: implements the contract it enforces.
ALLOWED_FETCH_FILES: frozenset[str] = frozenset({
    "frontend/api.js",
})

#: Function names whose bodies may contain ``fetch(`` because the
#: wrapper itself lives in that function body. Only meaningful
#: in files not listed in :data:`ALLOWED_FETCH_FILES`; the wrapper
#: in ``frontend/app.js`` is ``api()`` and that one function is
#: allowed to issue the actual ``fetch(`` call on the wire.
ALLOWED_FETCH_FUNCTIONS: frozenset[str] = frozenset({
    "api",
})


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _function_body_offsets(source: str, name: str) -> list[tuple[int, int]]:
    """Return one ``(body_open_pos, body_close_exclusive)`` pair per
    top-level declaration of ``name`` in ``source``.

    ``body_open_pos`` is the character index of the opening ``{``;
    ``body_close_exclusive`` is one past the matching closing ``}``.
    Brace counting is naive (does not understand strings or
    comments), but the wrapper functions in this project are short
    and use uniform 4-space indentation, and a brace literal inside
    a comment is exceedingly unlikely — the simple rule holds for
    the actual codebase and the synthetic test fixtures.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    results: list[tuple[int, int]] = []
    for match in pattern.finditer(source):
        body_start = match.end() - 1  # index of the opening '{'
        depth = 0
        i = body_start
        while i < len(source):
            ch = source[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    results.append((body_start, i + 1))
                    break
            i += 1
    return results


def find_bare_fetches(frontend_dir: Path) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, snippet)`` for every ``fetch(`` that
    violates the wrapper rule across ``frontend_dir``.

    ``path`` is the file path relative to the repository root
    (``Path("frontend/<name>.js")``), ``lineno`` is the 1-based line,
    ``snippet`` is the literal ``"fetch("`` token that matched. A
    clean tree returns ``[]``; a one-off violation returns a single
    tuple; multiple matches return multiple rows in source order.

    This is the canonical scanner — reused by tasks 18 and 20, which
    must hold the same judgement criteria. Both gates import the
    function rather than re-implementing the regex so adding a new
    exemption (e.g. ``fetch("data:...")`` in an image helper) lands
    in exactly one place.

    Exempt regions
    --------------
    * Files listed in :data:`ALLOWED_FETCH_FILES` are skipped
      entirely — the wrapper itself is the only legitimate
      ``fetch(`` source in those files.
    * Function bodies in :data:`ALLOWED_FETCH_FUNCTIONS` are exempt
      in any other file — the wrapper may call ``fetch(`` internally
      without being routed back through itself.

    Suppressed positions (still relevant in non-exempt files)
    ---------------------------------------------------------
    * ``//`` line comments — anything after ``//`` to end of line.
    * ``/* ... */`` block comments — may span lines.
    * ``'...'``, ``"..."``, ``\\`...\\``` string literals — the
      ``fetch(`` substring is just text and cannot actually call the
      network.

    Suppression is implemented as a single-pass tokenizer rather
    than per-line heuristics so a ``fetch(`` substring that happens
    to occur inside a multi-line template literal does not produce
    a false positive. Bare ``/`` characters (potential regex-literal
    starts in real JS) are treated as ordinary code characters: the
    codebase contains no regex literal that resembles ``fetch(``,
    so this simplification cannot produce a false negative for the
    gate's actual purpose.
    """
    findings: list[tuple[Path, int, str]] = []
    needle = "fetch("
    for path in sorted(frontend_dir.glob("*.js")):
        rel = Path("frontend") / path.name
        if rel.as_posix() in ALLOWED_FETCH_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        allowed_spans: list[tuple[int, int]] = []
        for fname in ALLOWED_FETCH_FUNCTIONS:
            allowed_spans.extend(_function_body_offsets(text, fname))

        findings.extend(_scan_text(text, rel, allowed_spans, needle))

    return findings


def _scan_text(
    text: str,
    rel: Path,
    allowed_spans: list[tuple[int, int]],
    needle: str,
) -> list[tuple[Path, int, str]]:
    """Walk ``text`` with a comment/string tokenizer; flag every
    ``needle`` at a code position outside ``allowed_spans``.

    State machine
    -------------
    * ``line_comment`` — true from ``//`` up to (but not including)
      the next newline.
    * ``block_comment`` — true between ``/*`` and the matching ``*/``;
      may span multiple lines.
    * ``string_quote`` — ``None`` in code, ``"'"'`` / ``'"'`` / ``'`'``
      while inside the corresponding string literal. Backslash
      escapes skip the next character so ``"\\\\""`` is not the
      end of the string.

    The match attempt only fires in code position (none of the
    three states active) — a ``fetch(`` substring inside a comment
    or string is silently skipped. ``/`` is recognised only as the
    start of a comment (``//`` / ``/*``); see the function-level
    docstring for why that simplification is safe here.
    """
    findings: list[tuple[Path, int, str]] = []
    needle_len = len(needle)

    # Pre-compute line-start offsets so the line number is a single
    # binary search per finding, not an O(n) recount.
    line_starts: list[int] = [0]
    for j, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(j + 1)

    line_comment = False
    block_comment = False
    string_quote: str | None = None

    i = 0
    while i < len(text):
        ch = text[i]

        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue

        if block_comment:
            if ch == "*" and i + 1 < len(text) and text[i + 1] == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue

        if string_quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == string_quote:
                string_quote = None
            i += 1
            continue

        # Code position: detect comment / string transitions before
        # attempting the ``fetch(`` match.
        if ch == "/" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "/":
                line_comment = True
                i += 2
                continue
            if nxt == "*":
                block_comment = True
                i += 2
                continue
        if ch in ('"', "'", "`"):
            string_quote = ch
            i += 1
            continue

        if text[i : i + needle_len] == needle:
            if any(start <= i < end for start, end in allowed_spans):
                i += needle_len
                continue
            findings.append((rel, _line_at(line_starts, i), needle))
            i += needle_len
            continue

        i += 1

    return findings


def _line_at(line_starts: list[int], pos: int) -> int:
    """1-based line number for character position ``pos``."""
    # ``line_starts`` is monotonically increasing; a linear walk is
    # O(line-count) and matches file sizes the gate actually sees
    # (single-file JS, ~1000 lines).
    line_idx = 0
    for k, start in enumerate(line_starts):
        if start > pos:
            break
        line_idx = k
    return line_idx + 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_frontend_js_files_are_scanned() -> None:
    """The scan target must produce at least one ``*.js`` file.

    A gate whose target directory has drifted away (the
    extension filter swallowed the tree, ``frontend/`` was
    emptied, the JS convention was abandoned) silently passes.
    This test fails the moment that happens so the regression
    is visible.
    """
    files = list(_FRONTEND_DIR.glob("*.js"))
    assert files, (
        f"{_FRONTEND_DIR} has no *.js files — the gate would now "
        "pass on an empty result. Either the *.js filter has "
        "drifted away from the frontend tree, or frontend/ has "
        "been emptied out."
    )


def test_no_bare_fetch_outside_api_wrapper() -> None:
    """Full ``frontend/`` scan: zero bare ``fetch(`` outside the wrapper.

    Any ``fetch(`` not inside :data:`ALLOWED_FETCH_FILES` /
    :data:`ALLOWED_FETCH_FUNCTIONS` is a bare fetch: a new
    contributor adding ``const x = await fetch('/api/...')``
    somewhere else would 403 at runtime because the request would
    lack the ``X-PDT-Request`` header. The whole point of the gate
    is to break the suite, not the browser.
    """
    findings = find_bare_fetches(_FRONTEND_DIR)
    assert not findings, (
        "frontend/ contains a bare fetch(...) outside the api() / "
        "apiRequest() wrappers. Each fetch() must go through api() "
        "in frontend/app.js or apiRequest() in frontend/api.js so "
        "that X-PDT-Request: 1 is always sent; without that "
        "header, /api/* answers 403.\n  "
        + "\n  ".join(f"{p.as_posix()}:{ln}: {s}" for p, ln, s in findings)
    )


def test_bare_fetch_is_detected(tmp_path) -> None:
    """A synthetic ``fetch(`` outside the wrapper must be flagged.

    Pins the scanner: a refactor that breaks the match logic
    (forgetting the needle, breaking the tokenizer, dropping the
    allowed-spans short-circuit) would let real regressions
    through and the gate would go silent. The fixture lives in
    ``tmp_path`` so the real ``frontend/`` tree is not contaminated
    by, and other tests cannot be affected by, this assertion.
    """
    synth_frontend = tmp_path / "frontend"
    synth_frontend.mkdir()
    (synth_frontend / "x.js").write_text(
        "async function loadThing() {\n"
        "    const r = await fetch('/api/thing');\n"
        "    return r.json();\n"
        "}\n",
        encoding="utf-8",
    )

    findings = find_bare_fetches(synth_frontend)

    assert findings == [
        (Path("frontend/x.js"), 2, "fetch(")
    ], findings


def test_wrapper_fetch_is_allowed(tmp_path) -> None:
    """``fetch(`` inside the ``api()`` body must not be flagged.

    The whole gate exists so the wrapper itself can use
    ``fetch(`` — exempting the body of :data:`ALLOWED_FETCH_FUNCTIONS`
    is half the contract. A bug that flagged the wrapper too would
    either force the gate to special-case its own exception or
    delete the wrapper entirely (which makes every other call in
    the codebase regress). This test pins the exemption.
    """
    synth_frontend = tmp_path / "frontend"
    synth_frontend.mkdir()
    (synth_frontend / "app.js").write_text(
        "async function api(path, options = {}) {\n"
        "    const { headers, ...rest } = options;\n"
        "    const res = await fetch(`/api${path}`, { headers });\n"
        "    return res.json();\n"
        "}\n",
        encoding="utf-8",
    )

    findings = find_bare_fetches(synth_frontend)

    assert findings == [], findings
