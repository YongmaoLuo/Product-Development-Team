"""Tests for the static legacy-JSON-path scanner + the load_fallback_order contract.

This file pins two contracts the provider-config refactor relies on:

  1. ``test_scan_no_violations`` — the static scanner
     (``scripts/scanners/scan_provider_url_map_refs.py``) finds zero references
     to the legacy JSON-path lookup table in
     production code. Run both as a CLI subprocess (so the on-disk
     gate is exercised end-to-end) and as an in-process import (so a
     future refactor that breaks the public API is caught in unit
     tests without needing a subprocess).

  2. ``test_load_fallback_order_uses_cc_switch_db`` — the
     ``provider_order.load_fallback_order`` chain is filtered through
     the CC Switch SQLite consumer layer
     (``cc_switch.list_provider_names``), so the
     fallback chain returned to the backend dispatch layer cannot
     include providers that are not actually configured in the CC
     Switch database. This pins the contract introduced when
     ``provider-order.json`` replaced the legacy
     the legacy JSON-path table.

TDD spec
--------
The original task spec lists two tests:

  * ``test_scan_no_violations`` — verify the scanner finds no
    violations on the current tree (subprocess form).
  * ``test_load_fallback_order_uses_cc_switch_db`` — mock the CC
    Switch DB layer (``cc_switch.list_provider_names``)
    and verify ``load_fallback_order`` calls it and respects its
    return value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple
from unittest import mock

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
# Test 2: load_fallback_order consults the CC Switch DB
# ---------------------------------------------------------------------------


def _write_valid_order_json(path: Path, order: List[str]) -> None:
    """Write a minimal-but-valid ``provider-order.json`` to ``path``.

    The on-disk schema (see ``backend/provider_order._validate_schema``)
    requires: ``version == 1``, ``order`` is a non-empty list of
    strings, ``updated_at`` is a parseable ISO 8601 timestamp. The
    ``providers`` dict is optional — we omit it to keep the fixture
    minimal.
    """
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "order": list(order),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_fallback_order_uses_cc_switch_db(tmp_path):
    """``load_fallback_order`` filters via the CC Switch DB layer.

    The backend reads the live provider chain from
    :func:`provider_order.load_fallback_order`. That function MUST
    filter the JSON's ``order`` list through the CC Switch consumer
    layer (``cc_switch.list_provider_names``) so only
    providers actually declared in the database survive. This test
    pins that contract end-to-end:

      1. Mock ``cc_switch.list_provider_names`` to
      2. Write a ``provider-order.json`` whose ``order`` contains
         the same IDs in the same order.
      3. Patch ``_default_order_file`` so the function reads our
         fixture instead of the production contract file.
      4. Drop the in-process cache (the function memoizes).
      5. Assert the returned list equals the mocked DB chain.

    If a future refactor replaces the database call with a hard-
    coded fallback, the mock is never consulted and the assertion
    fails.

    The mock is applied to ``provider_order.cc_switch``
    (the bound name the function actually uses) so we don't have to
    fight Python's import-system caching: every call from
    ``provider_order`` resolves through the patched attribute.
    """
    import provider_order

    # into the contract file. Using a list of 3 IDs makes ordering
    # bugs easy to spot in failure output.
    db_chain = [
        "Vendor B Pro",
        "Vendor A Pro",
        "Vendor C App",
    ]
    order_file = tmp_path / "provider-order.json"
    _write_valid_order_json(order_file, db_chain)

    # Drop the in-process cache so the function actually reads the
    # fixture (and so the mock's call count is unambiguous: a
    # cache-hit would short-circuit the DB lookup entirely).
    provider_order.cache_clear()

    with mock.patch.object(
        provider_order, "_default_order_file", return_value=order_file
    ):
        with mock.patch.object(
            provider_order.cc_switch,
            "list_provider_names",
            return_value=list(db_chain),
        ) as mock_list_ids:
            result = provider_order.load_fallback_order()

    # 1. The DB layer MUST be consulted. ``call_count >= 1`` (the
    #    exact count is an implementation detail of the cache code;
    #    we care that the call happened at all).
    assert mock_list_ids.call_count >= 1, (
        "load_fallback_order did not call cc_switch."
        "list_provider_names -- the chain is no longer filtered "
        "through the CC Switch DB layer"
    )

    # 2. The returned chain MUST equal the DB chain exactly. This
    #    is the contract: the JSON's ``order`` is an optimization
    expected = list(db_chain)
    assert result == expected, (
        f"load_fallback_order returned {result!r}; expected {expected!r}. "
        "The chain must be filtered through the CC Switch DB layer."
    )

    # 3. Belt-and-braces: every returned ID is a string (the
    #    consumer layer returns ``set[str]`` but the chain
    #    contract is a ``list[str]``).
    for provider_id in result:
        assert isinstance(provider_id, str), (
            f"load_fallback_order returned non-string id "
            f"{provider_id!r} of type {type(provider_id).__name__}"
        )


def test_load_fallback_order_drops_providers_not_in_db(tmp_path):
    """``load_fallback_order`` drops providers missing from the DB.

    A subtler contract than the happy-path filter: if the JSON
    ``order`` contains a provider the DB does not know about, the
    function must drop that ID (with a warning) and return only
    the surviving ones, in their original order. A refactor that
    removes the DB filter would expose every ID in the JSON
    verbatim — including the unrecognized one — and this test
    fails.
    """
    import provider_order

    # JSON declares 3 IDs; the DB only knows 2 of them. The
    # unknown one must be dropped silently (well, ``log.warning``,
    # but the function still returns the filtered list).
    json_order = [
        "Vendor B Pro",
        "unknown-provider-from-json",
        "Vendor A Pro",
    ]
    db_chain = ["Vendor B Pro", "Vendor A Pro"]

    order_file = tmp_path / "provider-order.json"
    _write_valid_order_json(order_file, json_order)
    provider_order.cache_clear()

    with mock.patch.object(
        provider_order, "_default_order_file", return_value=order_file
    ):
        with mock.patch.object(
            provider_order.cc_switch,
            "list_provider_names",
            return_value=list(db_chain),
        ):
            result = provider_order.load_fallback_order()

    # The unknown provider must be dropped; the surviving IDs
    expected = list(db_chain)
    assert result == expected, (
        f"load_fallback_order returned {result!r}; expected {expected!r}. "
        "An ID present in the JSON but missing from the CC Switch "
        "DB must be dropped (the DB is the source of truth)."
    )
    assert "unknown-provider-from-json" not in result, (
        "load_fallback_order leaked a provider not in the CC Switch "
        "DB into the returned chain"
    )


# ---------------------------------------------------------------------------
# Test 3: lru_cache(maxsize=1) once-per-process caching mechanism
# ---------------------------------------------------------------------------
#
# These tests pin VP-049: the fallback-order read is wrapped in
# :func:`functools.lru_cache` (``maxsize=1``) so two calls in the
# same process read the file at most once, and :func:`cache_clear`
# exposes a public way to drop the cache (operator escape hatch
# after a manual JSON edit).


def test_load_fallback_order_lru_cache_decorator_is_maxsize_one():
    """``_load_fallback_order_cached`` is wrapped in lru_cache(maxsize=1).

    Pins the implementation choice: the read is wrapped in stdlib
    :func:`functools.lru_cache` with ``maxsize=1`` so the backend
    reads ``provider-order.json`` at most once per process per
    distinct path. We assert the wrapper exposes ``cache_info`` and
    ``cache_clear`` (the canonical ``lru_cache`` surface) and that
    the ``maxsize`` reported matches 1.
    """
    import provider_order
    import functools

    cached = provider_order._load_fallback_order_cached
    # lru_cache-decorated functions carry a ``cache_info`` method.
    assert hasattr(cached, "cache_info"), (
        "_load_fallback_order_cached is not decorated with "
        "functools.lru_cache (missing cache_info)"
    )
    assert hasattr(cached, "cache_clear"), (
        "_load_fallback_order_cached is not decorated with "
        "functools.lru_cache (missing cache_clear)"
    )
    # The function must still be callable as a regular function.
    assert callable(cached), (
        "_load_fallback_order_cached is not callable"
    )
    # cache_info returns a CacheInfo namedtuple with a ``maxsize``
    # field. We pin it to 1 so a future bump to ``maxsize=None``
    # (unbounded) or a larger number is caught here.
    info = cached.cache_info()
    assert info.maxsize == 1, (
        f"_load_fallback_order_cached has maxsize={info.maxsize}; "
        "expected 1 (once-per-process, single-path contract)"
    )


def test_load_fallback_order_lru_cache_hits_in_same_process(tmp_path):
    """Two ``load_fallback_order`` calls in one process = one file read.

    Pins the "once-per-process" contract end-to-end: the second call
    must be served from the :func:`functools.lru_cache` instead of
    re-reading the file and re-calling the CC Switch consumer layer.
    We count calls to ``cc_switch.list_provider_names``
    as the proxy for "the file was re-read" — that function is
    consulted on every uncached read, so a cache miss bumps the
    counter and a cache hit does not.
    """
    import provider_order

    db_chain = [
        "Vendor B Pro",
        "Vendor A Pro",
        "Vendor C App",
    ]
    order_file = tmp_path / "provider-order.json"
    _write_valid_order_json(order_file, db_chain)
    # Drop any cached state left over from earlier tests in the
    # same process. ``lru_cache(maxsize=1)`` would otherwise evict
    # this test's path on the second test run, but we want a
    # deterministic miss → hit sequence.
    provider_order.cache_clear()

    with mock.patch.object(
        provider_order, "_default_order_file", return_value=order_file
    ):
        with mock.patch.object(
            provider_order.cc_switch,
            "list_provider_names",
            return_value=list(db_chain),
        ) as mock_list_ids:
            first = provider_order.load_fallback_order()
            second = provider_order.load_fallback_order()
            third = provider_order.load_fallback_order()

    # 1. All three calls return the same chain — the cached tuple is
    #    converted back to a list on every call.
    expected = list(db_chain)
    assert first == expected, f"first call returned {first!r}"
    assert second == expected, f"second call returned {second!r}"
    assert third == expected, f"third call returned {third!r}"

    # 2. The CC Switch DB layer is consulted exactly once across
    #    all three calls. Calls 2 and 3 are served from the
    #    ``lru_cache`` (maxsize=1) keyed on the resolved path.
    assert mock_list_ids.call_count == 1, (
        f"load_fallback_order called list_provider_names "
        f"{mock_list_ids.call_count} times across 3 calls; "
        "expected exactly 1 (lru_cache should serve calls 2 and 3 "
        "from the cached tuple, not re-read the file)"
    )


def test_load_fallback_order_cache_clear_resets_lru_cache(tmp_path):
    """``cache_clear()`` forces the next call to re-read the file.

    Pins the reversibility contract: after ``cache_clear()``, the
    next :func:`load_fallback_order` call must miss the
    :func:`functools.lru_cache` and re-read the file (and re-call
    the CC Switch consumer layer). This is the operator escape
    hatch — after a manual JSON edit, the operator calls
    ``cache_clear()`` and the next read picks up the new file.
    """
    import provider_order

    db_chain = ["Vendor B Pro", "Vendor A Pro"]
    order_file = tmp_path / "provider-order.json"
    _write_valid_order_json(order_file, db_chain)
    provider_order.cache_clear()

    with mock.patch.object(
        provider_order, "_default_order_file", return_value=order_file
    ):
        with mock.patch.object(
            provider_order.cc_switch,
            "list_provider_names",
            return_value=list(db_chain),
        ) as mock_list_ids:
            # 1st call: cache miss, file is read, list_provider_names
            # is called once.
            provider_order.load_fallback_order()
            calls_after_first = mock_list_ids.call_count
            # 2nd call: cache hit (lru_cache served the same tuple),
            # list_provider_names is NOT called.
            provider_order.load_fallback_order()
            assert mock_list_ids.call_count == calls_after_first, (
                "lru_cache did not serve the 2nd call from cache"
            )
            # Clear the cache — the operator's escape hatch.
            provider_order.cache_clear()
            # 3rd call: must miss the cache, re-read the file, and
            # re-call list_provider_names. This is the contract under
            # test: cache_clear() drops the lru_cache entry.
            provider_order.load_fallback_order()

    # 3rd call is the one we care about. Across the 3 calls there
    # should be exactly 2 list_provider_names invocations: 1st (miss)
    # and 3rd (post-clear miss). The 2nd is served from the cache.
    assert mock_list_ids.call_count == calls_after_first + 1, (
        f"cache_clear() did not reset the lru_cache: "
        f"list_provider_names was called {mock_list_ids.call_count} "
        f"times across 3 load_fallback_order calls (1 before clear, "
        f"1 served from cache, expected 1 more after clear). "
        f"call_count after 1st={calls_after_first}, "
        f"after clear+3rd={mock_list_ids.call_count}"
    )
