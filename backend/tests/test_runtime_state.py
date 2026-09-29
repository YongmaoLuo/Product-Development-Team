"""Tests for ``backend.runtime_state.RuntimeState``.

The :class:`RuntimeState` dataclass wraps the JSON blob persisted in
the SQLite ``plan_verification.runtime_state`` column. The contract
is small but pinning it here protects the executor's
recovery-from-disk path from accidental field-shape drift.

These tests are pure unit-level — they import the dataclass directly
and exercise its public surface (constructor defaults, mutable
fields, ``to_dict``/``from_dict`` round-trip, and a ``pending_vps``
helper that mirrors the executor's
``list(runtime_state.get("pending_vps", []))`` read shape).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.runtime_state import RuntimeState


def test_default_construction_yields_empty_pending_vps() -> None:
    """A freshly-built :class:`RuntimeState` exposes ``pending_vps == []``.

    The executor's recovery path falls back to ``[]`` when the JSON
    column is missing the key entirely; constructing the dataclass
    without arguments must agree with that fallback so a default
    object is interchangeable with an empty dict read.
    """
    state = RuntimeState()
    assert state.pending_vps == []


def test_pending_vps_is_a_list_when_provided() -> None:
    """Passing ``pending_vps`` to the constructor preserves the value
    verbatim (no copy-on-construct coercion that would drop members).
    """
    state = RuntimeState(pending_vps=["VP-001", "VP-002"])
    assert state.pending_vps == ["VP-001", "VP-002"]


def test_to_dict_round_trip_preserves_pending_vps() -> None:
    """``from_dict(state.to_dict())`` reproduces the original object.

    This is the contract the SQLite ``plan_verification.runtime_state``
    column relies on: writers call ``to_dict()`` (or build the dict
    by hand), readers call ``from_dict()`` to get a typed object
    instead of poking a raw dict.
    """
    state = RuntimeState(pending_vps=["VP-A", "VP-B", "VP-C"])
    payload: Dict[str, Any] = state.to_dict()
    assert payload == {"pending_vps": ["VP-A", "VP-B", "VP-C"]}
    rebuilt = RuntimeState.from_dict(payload)
    assert rebuilt == state
    assert rebuilt.pending_vps == ["VP-A", "VP-B", "VP-C"]


def test_from_dict_handles_missing_pending_vps_key() -> None:
    """A dict lacking ``pending_vps`` (e.g. an empty JSON column or a
    legacy row written before the field existed) deserialises to a
    ``RuntimeState`` with ``pending_vps == []`` — never ``KeyError``.
    """
    rebuilt = RuntimeState.from_dict({})
    assert rebuilt.pending_vps == []


def test_from_dict_handles_none_value_safely() -> None:
    """A dict with ``pending_vps == None`` (defensive write from older
    code paths) is normalised to ``[]`` rather than propagated as
    ``None`` (which would later blow up ``list(None)`` in the
    executor).
    """
    rebuilt = RuntimeState.from_dict({"pending_vps": None})
    assert rebuilt.pending_vps == []


def test_from_dict_preserves_unknown_keys_for_forward_compat() -> None:
    """Unknown keys in the source dict are stored as ``extra`` so a
    newer writer's metadata survives a round-trip through an older
    reader (the executor's recovery loop is tolerant of future
    fields — silent loss would be the worse failure mode).
    """
    payload: Dict[str, Any] = {
        "pending_vps": ["VP-1"],
        "future_field": {"nested": True},
        "future_counter": 7,
    }
    rebuilt = RuntimeState.from_dict(payload)
    assert rebuilt.pending_vps == ["VP-1"]
    assert rebuilt.extra == {"future_field": {"nested": True}, "future_counter": 7}
    # Round-trip preserves extras so writes back to SQLite don't drop
    # newer callers' metadata.
    assert rebuilt.to_dict() == payload


def test_equality_is_value_based() -> None:
    """Two :class:`RuntimeState` instances with the same field values
    compare equal — the dataclass is a plain value object, no identity
    tricks.
    """
    a = RuntimeState(pending_vps=["VP-1"])
    b = RuntimeState(pending_vps=["VP-1"])
    c = RuntimeState(pending_vps=["VP-2"])
    assert a == b
    assert a != c


def test_pending_vps_mutation_is_observable() -> None:
    """``pending_vps`` is a mutable list (matching the executor's
    read-and-mutate pattern at ``verification_executor.py:702``).
    Appending to it must be visible via the attribute — no
    ``@property`` returning a fresh copy.
    """
    state = RuntimeState(pending_vps=["VP-1"])
    state.pending_vps.append("VP-2")
    assert state.pending_vps == ["VP-1", "VP-2"]


def test_import_path_is_backend_runtime_state() -> None:
    """Importing via the package-style path ``backend.runtime_state``
    must succeed — the test suite (and downstream code) imports from
    the package, not the bare module name.
    """
    import importlib

    module = importlib.import_module("backend.runtime_state")
    assert module.RuntimeState is RuntimeState
