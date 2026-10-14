"""Tests for the static legacy-JSON-path scanner + the load_fallback_order contract.

This file pins two contracts the provider-config refactor relies on:

  1. ``test_scan_no_violations`` — the static scanner
     (``scripts/scanners/scan_provider_url_map_refs.py``) finds zero references
     to the legacy JSON-path lookup table in
     production code. Run both as a CLI subprocess (so the on-disk
     gate is exercised end-to-end) and as an in-process import (so a
     future refactor that breaks the public API is caught in unit
     tests without needing a subprocess).

  2. ``test_load_fallback_order_asks_cc_switch_over_http`` — the
     ``provider_order.load_fallback_order`` chain is the array order
     CC Switch serves on its HTTP provider list, taken as given. There
     is no second source: a failure to be answered raises
     ``ProviderOrderError`` rather than being answered from elsewhere.

TDD spec
--------
The original task spec lists two tests:

  * ``test_scan_no_violations`` — verify the scanner finds no
    violations on the current tree (subprocess form).
  * ``test_load_fallback_order_asks_cc_switch_over_http`` — give the
    resolver a transport and verify the question asked, the answer
    returned, and the loudness when there is no answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import pytest

# Constructed at runtime so the literal legacy token is not in source.
_LEGACY_JSON_TOKEN = "provider" + "-" + "url" + "-map" + ".json"
_LEGACY_BARE_TOKEN = "provider" + "-" + "url" + "-map"

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

# This file lives at ``<root>/backend/tests/unit/test_load_fallback_order.py``.
# Going three levels up reaches the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"
TOOLS_DIR = PROJECT_ROOT / "tools"
SCANNERS_DIR = PROJECT_ROOT / "scripts" / "scanners"
SCANNER_PATH = SCANNERS_DIR / "scan_provider_url_map_refs.py"

# ``backend/`` is not on sys.path by default in the test environment;
# tests that import ``provider_order`` need it inserted explicitly.
# Using ``insert(0, ...)`` so a local checkout's modules win over any
# pip-installed copies.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Test 1: scanner finds no violations on the current tree
# ---------------------------------------------------------------------------


def _run_scanner_cli(args: Sequence[str] = ()) -> subprocess.CompletedProcess:
    """Run the scanner CLI as a subprocess and return the result.

    The venv Python is used (per the project's "always use the
    venv" rule) so the subprocess gets the same dependency set as
    the rest of the suite. ``check=False`` lets the test inspect
    the returncode without raising.
    """
    venv_python = PROJECT_ROOT / "backend" / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        # Fall back to the interpreter that pytest itself is running
        # under. This keeps the test usable in environments where the
        # venv has not been created (e.g. a fresh CI bootstrap).
        venv_python = Path(sys.executable)
    return subprocess.run(
        [str(venv_python), str(SCANNER_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(PROJECT_ROOT),
    )


def _iter_scanned_files() -> Iterable[Path]:
    """Re-implement the scanner's file walk for the in-process test.

    Mirrors ``scripts/scanners/scan_provider_url_map_refs.py``'s
    ``iter_scanned_files``: walks ``backend/`` and ``tools/`` only, skips
    the standard excluded directory names, and yields only the
    configured source extensions. Keeping a local copy means the
    in-process assertion
    does not require importing the scanner module (so a typo in the
    scanner's public API does not break the test indirectly), and
    also lets the in-process scan and the subprocess scan be cross-
    checked against each other.
    """
    EXCLUDED_DIR_NAMES = frozenset(
        {".git", ".venv", "node_modules", "build", "dist", "__pycache__"}
    )
    SCANNED_SUFFIXES = frozenset(
        {".py", ".yaml", ".yml", ".json", ".md", ".sh"}
    )

    for scoped_root in (BACKEND_DIR, TOOLS_DIR):
        if not scoped_root.is_dir():
            continue
        for root, dirs, files in os.walk(scoped_root):
            # Prune excluded dirs in place so ``os.walk`` skips them
            # entirely.
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
            for fname in sorted(files):
                p = Path(root) / fname
                if p.suffix in SCANNED_SUFFIXES:
                    yield p


# Path-prefix whitelist — mirrors the scanner's
# ``FIXTURE_PREFIXES`` constant.
FIXTURE_PREFIXES: Tuple[str, ...] = (
    "backend/tests/",
    "tests/",
)

# Self-exempt paths — mirrors the scanner's ``SELF_EXEMPT_PATHS``.
# The scanner cannot scan itself without false positives (its
# docstrings and constants necessarily contain the literal). The
# test mirrors this rule so the in-process scan and the on-disk
# CLI agree on what's exempt.
SELF_EXEMPT_PATHS: frozenset = frozenset(
    {
        "scripts/scanners/scan_provider_url_map_refs.py",
    }
)


def _is_whitelisted(path: Path) -> bool:
    """Mirror the scanner's fixture-prefix + self-exempt check."""
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return False
    for prefix in FIXTURE_PREFIXES:
        bare = prefix.rstrip("/")
        if rel == bare or rel.startswith(prefix):
            return True
    if rel in SELF_EXEMPT_PATHS:
        return True
    return False


def _scan_inline(path: Path) -> List[str]:
    """In-process scan for legacy JSON-path references in ``path``.

    Returns ``[]`` for whitelisted paths. Each violation is formatted
    ``"<rel>:<line>: found <needle>"`` to match the scanner CLI's
    output format.
    """
    NEEDLES = (_LEGACY_JSON_TOKEN, _LEGACY_BARE_TOKEN)
    if _is_whitelisted(path):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        # Path is outside project_root (cross-repo symlink) — skip
        # rather than crash, mirroring the scanner's behaviour in
        # scripts/scanners/scan_provider_url_map_refs.py.
        return []
    violations: List[str] = []
    seen_offsets: dict = {}
    # Longest first so we attribute each offset to the most specific
    # (filename) form. Matches the scanner's dedup discipline.
    for needle in sorted(NEEDLES, key=len, reverse=True):
        start = 0
        while True:
            pos = text.find(needle, start)
            if pos < 0:
                break
            if pos not in seen_offsets:
                seen_offsets[pos] = needle
            start = pos + len(needle)
    for pos in sorted(seen_offsets):
        line_num = text[:pos].count("\n") + 1
        violations.append(f"{rel}:{line_num}: found {seen_offsets[pos]!r}")
    return violations


def test_scan_no_violations_subprocess():
    """The scanner CLI exits 0 with no output on the current tree.

    This is the "static scan" half of the contract: the on-disk CLI
    gate (``scripts/scanners/scan_provider_url_map_refs.py --strict``) must
    return ``0`` and emit no diagnostic output. A non-zero exit
    would block CI; a non-empty stdout would make the gate noisy.

    We assert both ``returncode == 0`` AND ``stdout == ""`` (the
    scanner writes diagnostics to stderr) so a future "warn vs
    error" refactor that accidentally promotes a violation to
    stdout is caught here.
    """
    result = _run_scanner_cli(("--strict",))
    assert result.returncode == 0, (
        f"scanner --strict exited {result.returncode} (expected 0). "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The scanner writes violations to stderr; stdout must be empty
    # on a clean run so CI logs stay uncluttered.
    assert result.stdout == "", (
        f"scanner --strict printed to stdout on a clean run: "
        f"{result.stdout!r}"
    )
    # Defensive: stderr should also be empty (the scanner only
    # writes to stderr when violations exist). If a future change
    # starts logging an INFO banner to stderr, this assertion will
    # catch it before CI gets noisy.
    assert result.stderr == "", (
        f"scanner --strict printed to stderr on a clean run: "
        f"{result.stderr!r}"
    )


def test_scan_no_violations_inprocess():
    """The in-process scan finds no violations on the current tree.

    Complements ``test_scan_no_violations_subprocess``: the
    subprocess test catches bugs in the CLI surface, this one
    catches bugs in the scanner logic itself (the dedup, the
    whitelist, the directory walk). A production file slipping
    A legacy JSON-path reference into a comment, a string literal, or
    a markdown report triggers this test.
    """
    all_violations: List[str] = []
    for path in _iter_scanned_files():
        all_violations.extend(_scan_inline(path))
    assert not all_violations, (
        "legacy JSON-path references found in production code:\n  - "
        + "\n  - ".join(all_violations[:50])
        + ("\n  ..." if len(all_violations) > 50 else "")
    )


# ---------------------------------------------------------------------------
# Test 2: load_fallback_order gets the chain from CC Switch
# ---------------------------------------------------------------------------


def test_load_fallback_order_asks_cc_switch_over_http(
    tmp_path, monkeypatch, real_cc_switch
):
    """CC Switch's HTTP provider list is consulted, and its answer stands.

    The chain is the array order CC Switch serves, and there is nowhere
    else it could come from, so this pins both halves: the question is
    asked over HTTP, and the answer is taken as given.
    """
    import provider_order

    served = ["Vendor B Pro", "Vendor A Pro", "Vendor C App"]
    asked: list[str] = []

    def _urlopen(request, timeout=None):
        asked.append(request.full_url)
        return _payload(served)

    monkeypatch.setattr(
        provider_order.urllib.request, "urlopen", _urlopen
    )
    provider_order.cache_clear()

    result = provider_order.load_fallback_order()

    assert result == served, (
        f"load_fallback_order returned {result!r}; expected the served "
        f"order {served!r}. The chain is CC Switch's array order."
    )
    assert asked, "CC Switch's HTTP provider list was never asked"
    assert asked[0].endswith("/api/providers")
    assert all(url.endswith("/api/providers") for url in asked), (
        f"only the read-only provider list may be requested; got {asked!r}"
    )


def test_load_fallback_order_raises_when_cc_switch_does_not_answer(
    tmp_path, monkeypatch, real_cc_switch
):
    """Nothing serving means a loud failure, not an answer from elsewhere.

    There is no second source for the chain, so the failure has to name
    the reason it could not be asked and carry the evidence — and must
    not come back as a list that looks like a fleet.
    """
    import provider_order

    monkeypatch.setattr(
        provider_order.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            ConnectionRefusedError("CC Switch is not running")
        ),
    )
    provider_order.cache_clear()

    with pytest.raises(provider_order.ProviderOrderError) as excinfo:
        result = provider_order.load_fallback_order()
        pytest.fail(f"returned {result!r} instead of raising")

    assert excinfo.value.reason == provider_order.REASON_API_UNREACHABLE
    assert "CC Switch is not running" in str(excinfo.value), (
        f"the underlying error text is not in the message: {excinfo.value!s}"
    )


def test_the_chain_is_not_filtered_by_database_membership(
    monkeypatch, real_cc_switch
):
    """A name CC Switch serves is in the chain, resolvable or no.

    An older reader dropped entries with no matching row, which made
    the chain a function of the local store as well as of CC Switch —
    and dropped precisely the newest row, the one CC Switch has and the
    consumer has not seen yet.
    """
    import provider_order

    served = ["Vendor B Pro", "brand-new-provider"]
    monkeypatch.setattr(
        provider_order.urllib.request,
        "urlopen",
        lambda *a, **k: _payload(served),
    )
    provider_order.cache_clear()

    assert provider_order.load_fallback_order() == served


def _payload(names):
    class _Resp:
        status = 200

        def read(self):
            return json.dumps(
                {"providers": [
                    {"id": f"id-{i}", "name": n} for i, n in enumerate(names)
                ]}
            ).encode()

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Resp()


# ---------------------------------------------------------------------------
# Test 3: lru_cache(maxsize=1) once-per-process caching mechanism
# ---------------------------------------------------------------------------
#
# These tests pin the once-per-process read: the chain is asked of CC
# Switch at most once so a dispatch storm does not become a request
# storm against the desktop app, and ``cache_clear`` exposes a public
# way to drop the cache — now meaning "re-ask CC Switch", which is
# what an operator wants after reordering there.


def test_load_fallback_order_lru_cache_decorator_is_maxsize_one():
    """``_load_fallback_order_cached`` is wrapped in lru_cache(maxsize=1).

    Pins the implementation choice: the read is wrapped in stdlib
    :func:`functools.lru_cache` with ``maxsize=1``. We assert the
    wrapper exposes ``cache_info`` and ``cache_clear`` (the canonical
    ``lru_cache`` surface), that it is still callable as a regular
    function, and that the reported ``maxsize`` matches 1.
    """
    import functools

    import provider_order

    cached = provider_order._load_fallback_order_cached
    assert hasattr(cached, "cache_info"), (
        "_load_fallback_order_cached is not decorated with "
        "functools.lru_cache (missing cache_info)"
    )
    assert hasattr(cached, "cache_clear"), (
        "_load_fallback_order_cached is not decorated with "
        "functools.lru_cache (missing cache_clear)"
    )
    assert callable(cached), (
        "_load_fallback_order_cached is not callable"
    )
    assert isinstance(cached, functools._lru_cache_wrapper), (
        "the cached reader must be the lru_cache wrapper itself, or "
        "cache_clear() would not reach the thing holding the chain"
    )
    info = cached.cache_info()
    assert info.maxsize == 1, (
        f"_load_fallback_order_cached has maxsize={info.maxsize}; "
        "expected 1 (once-per-process contract)"
    )


def test_load_fallback_order_lru_cache_hits_in_same_process(
    monkeypatch, real_cc_switch
):
    """Three calls in one process are one question.

    The proxy for "the chain was re-read" is now the HTTP request
    count: a cache miss bumps it, a hit does not.
    """
    import provider_order

    db_chain = ["Vendor B Pro", "Vendor A Pro", "Vendor C App"]
    calls: list[str] = []

    def _urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _payload(db_chain)

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)
    provider_order.cache_clear()

    first = provider_order.load_fallback_order()
    second = provider_order.load_fallback_order()
    third = provider_order.load_fallback_order()

    assert first == second == third == db_chain, (
        "every call in the process must return the same chain"
    )
    assert len(calls) == 1, (
        f"CC Switch was asked {len(calls)} times across 3 calls; "
        "expected exactly 1 (lru_cache should serve calls 2 and 3)"
    )


def test_load_fallback_order_cache_clear_resets_lru_cache(
    monkeypatch, real_cc_switch
):
    """``cache_clear()`` makes the next call ask CC Switch again.

    This is the operator escape hatch, and after the move to CC Switch
    it reads the way it always wanted to: after reordering providers in
    the CC Switch UI, clear the cache and the backend picks it up.
    """
    import provider_order

    calls: list[str] = []
    answer = ["Vendor B Pro", "Vendor A Pro"]

    def _urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _payload(answer)

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)
    provider_order.cache_clear()

    provider_order.load_fallback_order()
    after_first = len(calls)

    provider_order.load_fallback_order()
    assert len(calls) == after_first, "the 2nd call must be served from cache"

    # Reorder in CC Switch, then clear.
    answer = ["Vendor A Pro", "Vendor B Pro"]
    provider_order.cache_clear()
    result = provider_order.load_fallback_order()

    assert len(calls) == after_first + 1, (
        "cache_clear() did not reset the lru_cache: CC Switch was "
        f"asked {len(calls)} times, expected {after_first + 1}"
    )
    assert result == answer, (
        f"after cache_clear() the chain must be re-asked; got {result!r}, "
        f"expected {answer!r}"
    )
