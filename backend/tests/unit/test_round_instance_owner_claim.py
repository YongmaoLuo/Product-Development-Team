"""Claiming the plan's "current service owner" role (2026-11-13).

The criterion this file pins
----------------------------
The owner registry (``service_owners.json`` beside the plan's ledger)
records one special entry: ``current_owner`` — the round that is
*starting services right now*.  The services a round starts are stamped
with that token, so whoever holds the role is the round the ledger
entries belong to.

``register_service_owner(current=True)`` overwrites ``current_owner``
unconditionally.  That is right for the round that starts services and
wrong for anybody else, because a round that ended without releasing
its token leaves a ``current_owner`` behind — and the next round would
inherit it.  The services the new round starts would then be recorded
under a name that belongs to a round that is over.

So the claim is made conditionally:

  * nobody holds the role → take it;
  * this round already holds it → take it again (idempotent);
  * somebody else holds it AND that owner is still alive — it holds a
    fresh workflow lease — → refuse, with a warning; a running round
    keeps the role;
  * somebody else holds it but is NOT alive — its lease is gone or
    stale, so its round is over → take it over.

Liveness is judged by the plan's workflow lease
(:func:`verification_loop._round_instance_is_live`), NOT by the owner
registry: the registry's owner is the long-lived backend, so a token
whose round was released keeps looking alive for ever.

What this file pins:

  1. no current owner → the round registers and owns the role;
  2. a leftover current owner whose round has ended → it is replaced;
  3. a current owner whose round is still alive → the claim is refused,
     the registry is left alone, and the refusal is logged;
  4. a stale lease (the heartbeat stopped) → the leftover is replaced;
  5. an empty token, or no plan directory → nothing is claimed and
     nothing is written;
  6. the registry reader survives a corrupt file: it answers ``{}``;
  7. a token that is NOT a string names no round → the claim is refused
     and nothing is written;
  8. a plan directory that does not exist is not conjured up: the claim
     is refused and the directory stays absent.

Hermeticity
------------
Everything happens inside ``tmp_path``: the registry and the lease are
the only things written, and both are written into the test's own plan
directory.  No service is started and no process is signalled.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

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
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path, plan_id: str = "p1") -> Path:
    """The plan's directory — the registry and the lease live inside it."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_lease(
    plan_dir: Path,
    round_token: str,
    *,
    heartbeat_at: Optional[str] = None,
    pid: Optional[int] = None,
) -> Path:
    """Write the plan's workflow lease, held by ``round_token``."""
    path = vl._verification_workflow_lease_path(plan_dir)
    record: Dict[str, Any] = {
        "round_token": round_token,
        "run_id": "run-1",
        "pid": os.getpid() if pid is None else pid,
        "started_at": datetime.now().isoformat(),
        "heartbeat_at": (
            datetime.now().isoformat()
            if heartbeat_at is None else heartbeat_at
        ),
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _current_owner(plan_dir: Path) -> str:
    """The token the registry currently names as ``current_owner``."""
    owners = service_manager.read_owners(plan_dir)
    return str(owners.get("current_owner") or "")


# ---------------------------------------------------------------------------
# The named cases
# ---------------------------------------------------------------------------


def test_registers_when_no_current_owner(tmp_path: Path) -> None:
    """Nobody holds the role → the round takes it."""
    plan_dir = _plan_dir(tmp_path)

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_replaces_a_finished_rounds_leftover(tmp_path: Path) -> None:
    """A leftover owner whose round ended may be replaced."""
    plan_dir = _plan_dir(tmp_path)
    # The previous round ended without releasing its token.
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )
    assert _current_owner(plan_dir) == "verification_loop-OLD"

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_does_not_steal_from_a_live_round(
    tmp_path: Path, caplog: Any,
) -> None:
    """A current owner whose round is still alive keeps the role."""
    plan_dir = _plan_dir(tmp_path)
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )
    # The old round is still running: it holds a fresh lease.
    _write_lease(plan_dir, "verification_loop-OLD")

    with caplog.at_level(logging.WARNING):
        ok = vl._claim_round_instance_ownership(
            plan_dir, "verification_loop-NEW",
        )

    assert ok is False
    # The registry is untouched — the live round still owns the role.
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-OLD"
    )
    assert any(
        record.levelno >= logging.WARNING for record in caplog.records
    )


def test_replaces_when_the_lease_is_stale(tmp_path: Path) -> None:
    """A lease whose heartbeat stopped means the round is over."""
    plan_dir = _plan_dir(tmp_path)
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )
    stale = (datetime.now() - timedelta(seconds=600)).isoformat()
    _write_lease(plan_dir, "verification_loop-OLD", heartbeat_at=stale)

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_empty_token_is_a_noop(tmp_path: Path) -> None:
    """An unnameable round, or no plan directory: nothing is claimed."""
    plan_dir = _plan_dir(tmp_path)
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )

    assert vl._claim_round_instance_ownership(plan_dir, "") is False
    assert vl._claim_round_instance_ownership(plan_dir, "   ") is False
    assert vl._claim_round_instance_ownership(plan_dir, None) is False
    assert vl._claim_round_instance_ownership(
        None, "verification_loop-NEW",
    ) is False
    # Nothing was written: the old owner still holds the role.
    assert _current_owner(plan_dir) == "verification_loop-OLD"


def test_non_string_token_is_refused(tmp_path: Path) -> None:
    """A token that is not a string names no round: nothing is claimed.

    The role is stamped onto the services the next round starts, so a
    name that was never minted must not be handed the role.  A number,
    a flag, a list, a dict or a blob of bytes is not a round.
    """
    plan_dir = _plan_dir(tmp_path)

    for bad in (123, 4.5, True, ["x"], {"a": 1}, b"bytes"):
        assert vl._claim_round_instance_ownership(plan_dir, bad) is False

    # Nothing was written into the plan directory at all.
    assert list(plan_dir.iterdir()) == []


def test_a_missing_plan_dir_is_not_created(tmp_path: Path) -> None:
    """A plan directory that is not there is not conjured up.

    Claiming the role writes the registry, and the write creates the
    directory it writes into — so a claim aimed at a path that does    not exist would bring a plan directory into being.
    """
    plan_dir = tmp_path / "plans" / "never-created"

    assert vl._claim_round_instance_ownership(
        plan_dir, "verification_loop-NEW",
    ) is False
    assert not plan_dir.exists()


def test_registry_reader_tolerates_corrupt_file(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    """A damaged registry answers "nobody owns the role"."""
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).write_text(
        "not json at all", encoding="utf-8",
    )

    assert server._service_owner_registry(plan_dir) == {}

    # The same answer when the module's reader is gone and the file
    # itself is read directly.
    monkeypatch.setattr(server, "_read_service_owners", None)
    assert server._service_owner_registry(plan_dir) == {}


# ---------------------------------------------------------------------------
# The boundaries around it
# ---------------------------------------------------------------------------


def test_claiming_twice_is_idempotent(tmp_path: Path) -> None:
    """The round that already owns the role may claim it again."""
    plan_dir = _plan_dir(tmp_path)

    assert vl._claim_round_instance_ownership(
        plan_dir, "verification_loop-NEW",
    ) is True
    assert vl._claim_round_instance_ownership(
        plan_dir, "verification_loop-NEW",
    ) is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_only_the_current_owners_liveness_matters(tmp_path: Path) -> None:
    """A live lease held by a THIRD token does not block the claim.

    The role belongs to the token the registry names.  Whether some
    other round holds a lease says nothing about that token — so a
    leftover owner whose own lease is gone is replaced.
    """
    plan_dir = _plan_dir(tmp_path)
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )
    _write_lease(plan_dir, "verification_loop-SOMEONE-ELSE")

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_registry_reader_falls_back_to_the_file(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    """Without the module's reader, the registry file itself is read.

    The registry is a file on disk precisely because two module
    instances may share one plan directory — so the file is a reader
    of last resort, and it is the same file the write side writes.
    """
    plan_dir = _plan_dir(tmp_path)
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )
    monkeypatch.setattr(server, "_read_service_owners", None)

    registry = server._service_owner_registry(plan_dir)

    assert registry.get("current_owner") == "verification_loop-OLD"


def test_registry_reader_never_raises(tmp_path: Path) -> None:
    """No plan directory, or a missing registry → an empty answer."""
    plan_dir = _plan_dir(tmp_path)

    assert server._service_owner_registry(plan_dir) == {}
    assert server._service_owner_registry(None) == {}
    assert server._service_owner_registry(tmp_path / "never-created") == {}


# ---------------------------------------------------------------------------
# A registry that is damaged
# ---------------------------------------------------------------------------
#
# The registry is written as UTF-8 JSON by this module, so bytes that are
# not UTF-8 are the wreckage of something else (a latin-1 write, a
# utf-16 write, a truncated file), and a *directory* in the registry's
# place is wreckage of a third kind.  None of them names an owner, so
# none of them may raise — and none of them may stay: a registry that
# cannot be written would make every later claim read "nobody owns the
# role" and then fail to take it.


def test_undecodable_registry_reads_as_empty(tmp_path: Path) -> None:
    """A registry that is not UTF-8 names nobody.

    The reader is asked who owns the role, not whether the file is
    well-formed.  A file that cannot even be decoded cannot have been
    written by this module, so it cannot name an owner — the answer is
    "nobody", never an exception.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).write_bytes(
        b"\xff\xfe{\x00}\x00",
    )

    assert service_manager.read_owners(plan_dir) == {}
    assert server._service_owner_registry(plan_dir) == {}


def test_undecodable_registry_is_replaced_by_the_claim(tmp_path: Path) -> None:
    """A damaged registry is rewritten by the round that claims the role.

    Reading it as "nobody owns the role" is only half of it: the round
    that claims the role has to be able to *write* it, otherwise the
    damage is permanent — every later round would read "nobody owns the
    role" and every claim would be refused.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).write_bytes(
        b"\xff\xfe\x00garbage",
    )

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_registry_that_is_a_directory_reads_as_empty(tmp_path: Path) -> None:
    """A registry that is a directory names nobody."""
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).mkdir()

    assert service_manager.read_owners(plan_dir) == {}
    assert server._service_owner_registry(plan_dir) == {}


def test_registry_that_is_a_directory_is_replaced_by_the_claim(
    tmp_path: Path,
) -> None:
    """A directory where the registry belongs is replaced by the claim.

    A file cannot replace a directory (``Path.replace`` refuses it), so
    a directory left in the registry's place would keep every later
    claim from taking the role.  An empty directory is removed so the
    registry can be written where it stood.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).mkdir()

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )


def test_a_directory_that_holds_something_is_not_removed(
    tmp_path: Path,
) -> None:
    """Only an EMPTY directory is removed — nothing else is ever touched.

    The registry's place is a file, so a directory there is wreckage;
    but a directory that holds something is somebody's data, and
    removing it recursively would take the plan's files with it.  The
    removal refuses to go deeper than an empty directory, and the
    claim is refused rather than forced through.
    """
    plan_dir = _plan_dir(tmp_path)
    occupied = plan_dir / service_manager.OWNERS_FILENAME
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep me", encoding="utf-8")

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is False
    assert occupied.is_dir()
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "keep me"


def test_registry_reader_falls_back_when_the_reader_raises(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    """A reader that blows up does not cost the answer.

    The module's reader is tried first; when it raises, the registry
    file itself is read — the same file the write side writes.
    """

    def _boom(plan_dir: Path) -> Dict[str, Any]:
        raise RuntimeError("the reader blew up")

    plan_dir = _plan_dir(tmp_path)
    service_manager.register_service_owner(
        plan_dir, "verification_loop-OLD", current=True,
    )
    monkeypatch.setattr(server, "_read_service_owners", _boom)

    registry = server._service_owner_registry(plan_dir)

    assert registry.get("current_owner") == "verification_loop-OLD"

# ---------------------------------------------------------------------------
# A registry that is a JSON bomb
# ---------------------------------------------------------------------------
#
# A third kind of damage: the file IS valid UTF-8 and it IS valid JSON,
# but it is nested past what ``json.loads`` can decode — the decoder
# recurses once per bracket and blows the interpreter's recursion limit,
# raising ``RecursionError``.  ``RecursionError`` is a ``RuntimeError``,
# so an ``except (OSError, ValueError)`` does not catch it and the
# exception walks out of the reader, into the writer, and out of the
# claim — leaving the registry damaged for ever.


def test_read_owners_survives_deeply_nested_json(tmp_path: Path) -> None:
    """A deeply nested registry names nobody — it does not raise.

    ``json.loads`` raises ``RecursionError`` on a payload nested past
    the recursion limit.  That is a wrecked file, not a programming
    error, so the reader answers ``{}`` like it answers ``{}`` for any
    other damage.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).write_text(
        "[" * 2000 + "]" * 2000, encoding="utf-8",
    )

    assert service_manager.read_owners(plan_dir) == {}


def test_deeply_nested_registry_is_replaced_by_the_claim(tmp_path: Path) -> None:
    """The damage is repaired by the round that claims the role.

    Reading the bomb as "nobody owns the role" is only half of it: the
    claim has to be able to *write* the registry, otherwise the bomb
    stays and every later round would read "nobody owns the role" and
    every claim would be refused.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).write_text(
        "[" * 2000 + "]" * 2000, encoding="utf-8",
    )

    ok = vl._claim_round_instance_ownership(plan_dir, "verification_loop-NEW")

    assert ok is True
    assert service_manager.read_owners(plan_dir)["current_owner"] == (
        "verification_loop-NEW"
    )
