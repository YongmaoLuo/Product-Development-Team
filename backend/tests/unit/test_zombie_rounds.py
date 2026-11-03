"""A round that was declared dead stays dead.

What this pins
--------------
The watchdog can declare a verification round dead -- its log went
silent past the hard ceiling, its VP is gone, the round's thread is
wedged.  Declaring it dead is not the same as making it stop: the LLM
call that was in flight can still return, the sub-process can still
exit, and the round's code can still wake up and try to write a
terminal state, change the plan's phase, or push a card.

So the death has to leave a *mark the round itself can check*.  The
mark is keyed by the round's own ``round_token`` -- the identity the
round already carries everywhere else -- and it is persisted, because
the thing that has to consult it may be a different process from the
one that wrote it.

``backend/zombie_rounds.py`` is that file layer, and these tests pin
the file contract only -- no watchdog, no loop, no routes:

* **invalidate then look up** -- the marker is there after the write;
* **unknown token** -- a round that was never declared dead is not
  declared dead by accident;
* **persistence** -- the marker survives a reload, because the round
  that wakes up is not necessarily the process that died;
* **corrupt file** -- a torn or garbage file reads as "nothing was
  invalidated", never as an exception, because an unreadable marker
  must never be the reason a plan cannot proceed;
* **clear** -- removes exactly the named round and leaves the others
  alone.

The extra tests pin the boundary conditions the module promises: a
repeated invalidation of the same token changes nothing, an invalidation
over a corrupt file recovers, an empty token is never a round, and the
record carries the reason and the actor that declared the death.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import zombie_rounds  # noqa: E402

#: A token shaped like the ones the verification loop mints.
TOKEN = "verification_loop-1-ab"

#: Another token, for the tests that need two rounds at once.
OTHER_TOKEN = "verification_loop-2-cd"


def _write_corrupt_file(plan_dir: Path) -> Path:
    """Put a file that is not JSON where the marker file belongs."""
    path = zombie_rounds.zombie_rounds_path(plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")
    return path


def test_invalidate_then_lookup_is_true(tmp_path):
    """The marker is there after the write."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    assert zombie_rounds.is_invalidated(tmp_path, TOKEN) is True


def test_unknown_token_is_false(tmp_path):
    """A round that was never declared dead is not declared dead."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    assert zombie_rounds.is_invalidated(tmp_path, OTHER_TOKEN) is False
    assert zombie_rounds.is_invalidated(tmp_path, "no-such-round") is False


def test_invalidation_persists_across_reload(tmp_path):
    """The marker is on disk, not in the writer's memory.

    Two independent checks: a reloaded module (a fresh interpreter
    state for this process) and a brand-new interpreter (a different
    process entirely).  The second one is the one that matters -- the
    round that wakes up is frequently not the process that died.
    """
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    reloaded = importlib.reload(zombie_rounds)
    try:
        assert reloaded.is_invalidated(tmp_path, TOKEN) is True
    finally:
        importlib.reload(zombie_rounds)

    probe = (
        "import sys;"
        f"sys.path.insert(0, {str(BACKEND_DIR)!r});"
        "import zombie_rounds;"
        f"print(zombie_rounds.is_invalidated({str(tmp_path)!r}, {TOKEN!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stdout


def test_corrupt_file_reads_as_empty(tmp_path):
    """An unreadable marker file must never block a plan."""
    _write_corrupt_file(tmp_path)

    assert zombie_rounds.is_invalidated(tmp_path, TOKEN) is False
    assert zombie_rounds.invalidated_round(tmp_path) is None


def test_clear_removes_only_the_named_round(tmp_path):
    """``clear`` is surgical: one token goes, the others stay."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )
    zombie_rounds.invalidate(
        tmp_path, OTHER_TOKEN,
        reason="verification_results_stale",
        actor="verification_watchdog",
    )

    zombie_rounds.clear(tmp_path, TOKEN)

    assert zombie_rounds.is_invalidated(tmp_path, TOKEN) is False
    assert zombie_rounds.is_invalidated(tmp_path, OTHER_TOKEN) is True
    # The surviving round is still fully described.
    survivor = zombie_rounds.invalidated_round(tmp_path)
    assert survivor is not None
    assert survivor["round_token"] == OTHER_TOKEN
    assert survivor["reason"] == "verification_results_stale"
    assert survivor["actor"] == "verification_watchdog"


def test_invalidated_round_reports_reason_actor_and_time(tmp_path):
    """The record says who declared the death, why, and when."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    record = zombie_rounds.invalidated_round(tmp_path)
    assert record is not None
    assert record["round_token"] == TOKEN
    assert record["reason"] == "verification_log_stale"
    assert record["actor"] == "verification_watchdog"
    assert record["at"]


def test_repeated_invalidation_is_idempotent(tmp_path):
    """Declaring the same round dead twice changes nothing."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )
    first = zombie_rounds.invalidated_round(tmp_path)

    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    assert zombie_rounds.invalidated_round(tmp_path) == first
    assert zombie_rounds.is_invalidated(tmp_path, TOKEN) is True


def test_invalidate_recovers_from_a_corrupt_file(tmp_path):
    """A corrupt marker file is replaced, not inherited."""
    _write_corrupt_file(tmp_path)

    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    assert zombie_rounds.is_invalidated(tmp_path, TOKEN) is True
    # The file on disk is valid JSON again -- a later reader gets a
    # verdict, not another exception.
    path = zombie_rounds.zombie_rounds_path(tmp_path)
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def test_empty_round_token_is_never_invalidated(tmp_path):
    """An empty token names no round, so it can be neither killed nor live."""
    zombie_rounds.invalidate(
        tmp_path, "",
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    assert zombie_rounds.is_invalidated(tmp_path, "") is False
    assert zombie_rounds.invalidated_round(tmp_path) is None


def test_clear_of_an_unknown_token_is_a_noop(tmp_path):
    """Clearing something that was never there does not touch the file."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )
    before = zombie_rounds.zombie_rounds_path(tmp_path).read_text(
        encoding="utf-8"
    )

    zombie_rounds.clear(tmp_path, "no-such-round")

    assert zombie_rounds.zombie_rounds_path(tmp_path).read_text(
        encoding="utf-8"
    ) == before
    assert zombie_rounds.is_invalidated(tmp_path, TOKEN) is True


def test_marker_file_lives_inside_the_plan_directory(tmp_path):
    """The file is a plan-local runtime artefact, not plan content."""
    zombie_rounds.invalidate(
        tmp_path, TOKEN,
        reason="verification_log_stale",
        actor="verification_watchdog",
    )

    path = zombie_rounds.zombie_rounds_path(tmp_path)
    assert path.parent == tmp_path
    assert path.name == ".verification_zombie_rounds.json"
    assert path.exists()


def test_missing_plan_directory_reads_as_no_invalidation(tmp_path):
    """A plan that was never invalidated has no marker file at all."""
    missing = tmp_path / "no-such-plan"

    assert zombie_rounds.is_invalidated(missing, TOKEN) is False
    assert zombie_rounds.invalidated_round(missing) is None