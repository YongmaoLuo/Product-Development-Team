"""The identity a round instance mints for itself (2026-11-13).

A round *instance* is one run of the verification loop over one plan.
The repair-complete callback re-enters the verification, and when it
does it needs an identity of its own — not the one the previous loop
run was holding, which names the round that has already ended.

What this file pins is the shape of that identity, and the two ways it
must not lie:

  1. it is **fresh** — two calls never return the same token, because a
     reused token would name two instances as one and the cleanup aimed
     at the second would reach the first;
  2. it carries the **pid**, so a registry left behind by a crashed
     backend can be recognised as stale (the same reason
     ``service_manager.new_owner_token`` puts it there);
  3. it carries **no round number** — the instance is identified by
     being a different instance, not by counting rounds;
  4. it is **minted even when the helper misbehaves** — a broken or
     empty owner-token call must not stop a round from having a name.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server  # noqa: E402,F401
import service_manager  # noqa: E402
import verification_loop as vl  # noqa: E402


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_returns_unique_tokens():
    """Two hundred calls, two hundred different identities."""
    tokens = [vl._new_round_instance_token() for _ in range(200)]
    assert len(set(tokens)) == 200


def test_token_shape():
    """``verification_loop-<pid>-<uuid12>`` — three segments, this pid."""
    token = vl._new_round_instance_token()

    assert token
    assert token.startswith("verification_loop-")

    parts = token.split("-")
    assert len(parts) == 3
    assert parts[1] == str(os.getpid())


@pytest.mark.parametrize("failure", ["raises", "empty"])
def test_falls_back_when_new_owner_token_is_unusable(monkeypatch, failure):
    """A broken owner-token call still leaves the instance named."""

    def _unusable(prefix):
        if failure == "raises":
            raise RuntimeError("new_owner_token is broken")
        return ""

    monkeypatch.setattr(service_manager, "new_owner_token", _unusable)

    token = vl._new_round_instance_token()

    assert token
    assert token.startswith("verification_loop-")
