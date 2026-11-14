"""How a round instance ends (2026-11-13).

A round *instance* is one run of the verification loop over one plan.
It ends in three steps, and the ORDER of those steps is the contract:

  1. **close** the round in the process ledger — the ledger forgets the
     tools the round owned;
  2. **release** the service owner — the round is over, so the owner
     registry must stop saying it owns anything;
  3. **reap** the services the round started — *only* when the exit is
     terminal.

The order is not incidental.  The reap aims at the entries a round owns,
so the round must already be closed and released by the time it fires —
otherwise the reap finds a round that still looks like it owns the
services it is about to stop, and a newer round that inherited the
registry can be reached through it.

There are two kinds of exit:

  * **terminal** — the workflow really exited.  The plan's services are
    no longer needed, so they are reaped.
  * **hand-off** — the loop returned while a repair execution is still
    in flight.  The workflow has NOT exited, so a reap here would stop
    the services the repair round is about to use.  A hand-off closes
    and releases, and does NOT reap.

What this file pins:

  1. the terminal exit closes, then releases, then reaps — in that
     order — and the reap is aimed at THIS round's token;
  2. the hand-off closes and releases, and never reaps;
  3. an empty token, or no plan directory, is a no-op: none of the
     three steps runs;
  4. a step that raises does not stop the ones after it, and does not
     escape the function — the cleanup never raises;
  5. the pins are not touched: ending an instance does not unpin the
     plan's services, because a pin is released by the round that
     wrote it or by its TTL, not by an unrelated exit;
  6. a token that is NOT a string names no round: close, release and
     reap are all no-ops;
  7. a plan directory that does not exist is not conjured up: the
     three steps do not run and the directory stays absent.

Hermeticity
------------
Every collaborator is replaced by a recorder, so nothing is written,
nothing is spawned, and no service is signalled.  The plan directory
is the test's own ``tmp_path``.
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    """The plan's directory."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _recorders(
    monkeypatch,
    *,
    release_raises: bool = False,
) -> (List[str], Dict[str, Any]):
    """Replace the three cleanup steps with recorders.

    Returns the ordered list of step names and a dict of what the
    steps were handed.
    """
    calls: List[str] = []
    captured: Dict[str, Any] = {}

    def _close(plan_dir: Path, round_token: str) -> None:
        calls.append("close")
        captured["close"] = {"plan_dir": plan_dir, "round_token": round_token}

    def _release(plan_dir: Path, owner_token: str) -> None:
        calls.append("release")
        captured["release"] = {
            "plan_dir": plan_dir, "owner_token": owner_token,
        }
        if release_raises:
            raise RuntimeError("release blew up")

    def _reap(
        plan_id: str,
        plan_dir: Path,
        reason: str,
        owner_token: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        calls.append("reap")
        captured["reap"] = {
            "plan_id": plan_id,
            "plan_dir": plan_dir,
            "reason": reason,
            "owner_token": owner_token,
        }

    monkeypatch.setattr(vl, "_close_round_in_ledger", _close)
    monkeypatch.setattr(vl._server, "_release_service_owner", _release)
    monkeypatch.setattr(vl._server, "_reap_managed_services", _reap)
    return calls, captured


# ---------------------------------------------------------------------------
# The named cases
# ---------------------------------------------------------------------------


def test_terminal_exit_closes_releases_then_reaps(
    monkeypatch, tmp_path: Path,
) -> None:
    """A terminal exit closes, then releases, then reaps — in that order."""
    plan_dir = _plan_dir(tmp_path)
    calls, captured = _recorders(monkeypatch)
    token = "verification_loop-9-abc"

    vl._end_round_instance("p1", plan_dir, token, terminal=True)

    assert calls == ["close", "release", "reap"]
    # The reap must be aimed at THIS round's token — a reap without one
    # owns nothing and would leave this round's services running.
    assert captured["reap"]["owner_token"] == token
    assert captured["reap"]["plan_id"] == "p1"
    assert captured["reap"]["plan_dir"] == plan_dir
    assert captured["reap"]["reason"] == "verification_loop_exit"
    # The release hands back the same token the round was named by.
    assert captured["release"]["owner_token"] == token


def test_handoff_does_not_reap(monkeypatch, tmp_path: Path) -> None:
    """A hand-off closes and releases, but the workflow has not exited."""
    plan_dir = _plan_dir(tmp_path)
    calls, _ = _recorders(monkeypatch)

    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc", terminal=False,
    )

    assert calls == ["close", "release"]
    assert "reap" not in calls


def test_empty_token_is_a_noop(monkeypatch, tmp_path: Path) -> None:
    """An unnameable instance, or no plan directory: nothing is cleaned."""
    plan_dir = _plan_dir(tmp_path)
    calls, _ = _recorders(monkeypatch)

    vl._end_round_instance("p1", plan_dir, "", terminal=True)
    vl._end_round_instance("p1", None, "verification_loop-9-abc", terminal=True)
    vl._end_round_instance("p1", plan_dir, None, terminal=True)

    assert calls == []


def test_never_raises_when_a_step_fails(
    monkeypatch, tmp_path: Path, caplog,
) -> None:
    """A step that raises is logged and stepped over, not propagated."""
    plan_dir = _plan_dir(tmp_path)
    calls, _ = _recorders(monkeypatch, release_raises=True)

    with caplog.at_level(logging.WARNING):
        vl._end_round_instance(
            "p1", plan_dir, "verification_loop-9-abc", terminal=True,
        )

    # The release failed, yet the reap still happened — the steps after
    # a failure still run, and the failure never escapes.
    assert calls == ["close", "release", "reap"]
    assert any(
        "verification_loop" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_pins_are_not_touched(monkeypatch, tmp_path: Path) -> None:
    """Ending an instance does not unpin the plan's services."""
    plan_dir = _plan_dir(tmp_path)
    unpinned: List[Any] = []

    def _unpin(plan_dir: Path, round_token: str) -> int:
        unpinned.append(round_token)
        return 0

    monkeypatch.setattr(
        service_manager, "unpin_services_for_round", _unpin,
    )
    _recorders(monkeypatch)

    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc", terminal=True,
    )

    assert unpinned == []
    # The source must not even name it: a pin is released by the round
    # that wrote it or by its TTL, never by an unrelated exit.
    source = inspect.getsource(vl._end_round_instance)
    assert "unpin_services_for_round" not in source


# ---------------------------------------------------------------------------
# The boundaries around it
# ---------------------------------------------------------------------------


def test_custom_reason_reaches_the_reap(
    monkeypatch, tmp_path: Path,
) -> None:
    """The reason says why the reap happened, and it is passed through."""
    plan_dir = _plan_dir(tmp_path)
    _, captured = _recorders(monkeypatch)

    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc",
        terminal=True, reason="repair_handoff",
    )

    assert captured["reap"]["reason"] == "repair_handoff"


def test_close_failure_does_not_stop_the_rest(
    monkeypatch, tmp_path: Path,
) -> None:
    """Even the first step failing leaves the other two to run."""
    plan_dir = _plan_dir(tmp_path)
    calls: List[str] = []

    def _close(plan_dir: Path, round_token: str) -> None:
        calls.append("close")
        raise RuntimeError("close blew up")

    def _release(plan_dir: Path, owner_token: str) -> None:
        calls.append("release")

    def _reap(*args: Any, **kwargs: Any) -> None:
        calls.append("reap")

    monkeypatch.setattr(vl, "_close_round_in_ledger", _close)
    monkeypatch.setattr(vl._server, "_release_service_owner", _release)
    monkeypatch.setattr(vl._server, "_reap_managed_services", _reap)

    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc", terminal=True,
    )

    assert calls == ["close", "release", "reap"]


def test_reap_failure_does_not_escape(monkeypatch, tmp_path: Path) -> None:
    """A reap that raises is swallowed — the function never raises."""
    plan_dir = _plan_dir(tmp_path)

    def _reap(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("reap blew up")

    monkeypatch.setattr(vl._server, "_reap_managed_services", _reap)

    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc", terminal=True,
    )


def test_non_string_token_is_a_noop(monkeypatch, tmp_path: Path) -> None:
    """A token that is not a string names no round: nothing is cleaned.

    Close, release and reap are all aimed at a round *by name*.  A
    number, a flag, a list, a dict or a blob of bytes is not a name
    that was ever minted, so none of the three steps may run for it.
    """
    plan_dir = _plan_dir(tmp_path)
    calls, _ = _recorders(monkeypatch)

    for bad in (123, 4.5, True, ["x"], {"a": 1}, b"bytes"):
        vl._end_round_instance("p1", plan_dir, bad, terminal=True)

    assert calls == []
    # Nothing was written into the plan directory at all.
    assert list(plan_dir.iterdir()) == []


def test_a_missing_plan_dir_is_not_created(
    monkeypatch, tmp_path: Path,
) -> None:
    """A plan directory that is not there is not conjured up.

    The release writes the owner registry, and the write creates the
    directory it writes into — so an exit aimed at a path that does not
    exist would bring a plan directory into being.
    """
    plan_dir = tmp_path / "plans" / "never-created"
    calls, _ = _recorders(monkeypatch)

    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc", terminal=True,
    )

    assert calls == []
    assert not plan_dir.exists()


# ---------------------------------------------------------------------------
# A registry that is damaged
# ---------------------------------------------------------------------------
#
# The release reads the owner registry to find the entries this round
# owns.  A registry that cannot be read names no owner — so it must not
# cost the round its exit, and it must not stay damaged either.


def _release_against_the_real_registry(monkeypatch, plan_dir: Path) -> None:
    """End a round instance with the REAL release in place.

    Only the ledger close and the reap are replaced — the release is
    the step that touches the registry, so replacing it would hide the
    very thing these cases are about.
    """
    monkeypatch.setattr(vl, "_close_round_in_ledger", lambda *a, **k: None)
    monkeypatch.setattr(
        vl._server, "_reap_managed_services", lambda *a, **k: None,
    )
    vl._end_round_instance(
        "p1", plan_dir, "verification_loop-9-abc", terminal=True,
    )


def test_a_corrupt_registry_does_not_stop_the_release(
    monkeypatch, tmp_path: Path,
) -> None:
    """A registry that cannot be decoded does not cost the round its exit.

    The round is over either way, so the release has to happen: the
    round's own entries have to become reapable by whoever reaps next.
    A registry that names no owner says exactly that — it just also has
    to be rewritten, or the damage outlives the round.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / service_manager.OWNERS_FILENAME).write_bytes(
        b"\xff\xfe\x00garbage",
    )

    _release_against_the_real_registry(monkeypatch, plan_dir)

    registry = service_manager.read_owners(plan_dir)
    assert registry.get("current_owner") is None
    assert registry.get("owners") == []


def test_a_directory_shaped_registry_does_not_stop_the_release(
    monkeypatch, tmp_path: Path,
) -> None:
    """A directory in the registry's place does not cost the round its exit.

    The release has to be able to write the registry, or the round's
    entries never become reapable.  An empty directory where the
    registry belongs is removed so the registry can take its place.
    """
    plan_dir = _plan_dir(tmp_path)
    occupied = plan_dir / service_manager.OWNERS_FILENAME
    occupied.mkdir()

    _release_against_the_real_registry(monkeypatch, plan_dir)

    assert occupied.is_file()
    registry = service_manager.read_owners(plan_dir)
    assert registry.get("current_owner") is None
    assert registry.get("owners") == []