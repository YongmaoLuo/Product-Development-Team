"""The owner registry's contract, pinned (2026-11-13).

The round-instance ownership work leans on three properties of the
plan's service-owner registry (``service_manager``).  They are old
behaviour — the work did not change them — and that is exactly why they
are worth pinning: the round-instance chain is built on top of them,
and a change to any of them would move the chain's ground without any
test of the chain going red.

The three properties:

  1. **roundtrip** — a round that registers itself as the current
     service owner can be found in the registry, and releasing it leaves
     the role unheld.
  2. **token shape** — a token names ONE owner and says enough to
     recognise a dead one: the prefix the caller asked for, this
     process' pid (a registry left behind by a crashed backend must be
     recognisable as stale) and a fresh suffix (two live rounds must
     never share a name).
  3. **one current owner** — the newest round that claims the role
     owns it.  A round that ended without releasing its token leaves a
     ``current_owner`` behind, and the next round would inherit it.

Scope
-----
Only behaviour that is already verified — read out of
``service_manager`` itself — is asserted here.  Nothing about the
internals of ``end_round`` or of a release with an unknown token is
assumed: the registry is asked the question it is asked in production
and the answer is checked.

Hermeticity
------------
Every registry this file touches lives in ``tmp_path``, and the process
ledger — process-wide state — is reset around every test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager  # noqa: E402
import round_process_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_ledger():
    """The ledger is process-wide state — a test must not lend its round
    to the next one."""
    round_process_ledger.reset_for_tests()
    yield
    round_process_ledger.reset_for_tests()


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A plan directory of the test's own."""
    path = tmp_path / "plans" / "p-owner-registry"
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# 1. The roundtrip
# ---------------------------------------------------------------------------


def test_owner_registry_roundtrip(plan_dir: Path) -> None:
    """A round that claims the role can be found, and can give it back.

    The claim is what a round instance does when it starts, and the
    release is what it does when it ends.  Between them the registry
    has to answer "who owns the plan right now?" — and after the
    release it has to answer "nobody".
    """
    token = "verification_loop-roundtrip"

    service_manager.register_service_owner(
        plan_dir, token, current=True,
    )

    registry = service_manager.read_owners(plan_dir)
    assert registry.get("current_owner") == token, (
        f"the registry names {registry.get('current_owner')!r} instead of "
        f"the token that claimed the role ({token!r})"
    )
    assert any(
        str(entry.get("token")) == token
        for entry in (registry.get("owners") or [])
    ), f"the registry does not carry the claiming token: {registry!r}"

    service_manager.release_service_owner(plan_dir, token)

    registry = service_manager.read_owners(plan_dir)
    assert registry.get("current_owner") in (None, ""), (
        f"the registry still names {registry.get('current_owner')!r} as "
        "the current owner after the round released it — the next round "
        "would inherit a role that belongs to a round that is over"
    )


# ---------------------------------------------------------------------------
# 2. The token's shape
# ---------------------------------------------------------------------------


def test_new_owner_token_shape_is_unchanged() -> None:
    """A token names one owner, and says enough to recognise a dead one.

    Three segments — the caller's prefix, this process' pid and a fresh
    suffix.  The pid is what lets a registry left behind by a crashed
    backend be recognised as stale; the fresh suffix is what keeps two
    live rounds from sharing a name.
    """
    token = service_manager.new_owner_token("verification_loop")

    assert isinstance(token, str) and token.strip(), (
        f"the token is not a usable string: {token!r}"
    )

    parts = token.split("-")
    assert len(parts) == 3, (
        f"the token {token!r} has {len(parts)} segments instead of three "
        "— the prefix, the pid and the fresh suffix"
    )
    assert parts[0] == "verification_loop", (
        f"the token {token!r} does not carry the prefix it was asked for"
    )
    assert parts[1] == str(os.getpid()), (
        f"the token {token!r} does not carry this process' pid — a "
        "registry left behind by a crashed backend could not be "
        "recognised as stale"
    )
    assert parts[2], f"the token {token!r} has an empty suffix"

    other = service_manager.new_owner_token("verification_loop")
    assert other != token, (
        "two owner tokens were equal — a cleanup aimed at one of them "
        "would reach the other"
    )


# ---------------------------------------------------------------------------
# 3. One current owner
# ---------------------------------------------------------------------------


def test_register_current_overwrites_previous_current(plan_dir: Path) -> None:
    """The newest round that claims the role owns it.

    A round that ended without releasing its token leaves a
    ``current_owner`` behind.  When the next round claims the role, the
    name that answers "which round is starting services RIGHT NOW" must
    be the new one — otherwise the services the new round starts are
    recorded under a name that belongs to a round that is over.
    """
    first = "verification_loop-first"
    second = "verification_loop-second"

    service_manager.register_service_owner(plan_dir, first, current=True)
    service_manager.register_service_owner(plan_dir, second, current=True)

    registry = service_manager.read_owners(plan_dir)
    assert registry.get("current_owner") == second, (
        f"the registry names {registry.get('current_owner')!r} as the "
        f"current owner instead of the newest claim ({second!r})"
    )
