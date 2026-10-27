"""TDD spec for the mcode session mapping — pinned key → real session id.

Why this mapping exists
=======================

``mcode exec`` has no ``--session-id``: a conversation cannot be
created under an id the caller chose. The Runtime mints the id
instead, and it looks like ``mvs_`` followed by 32 hex digits.
Resuming is the other way round — ``--session <real id>`` re-opens a
conversation that exists.

So the caller and the CLI disagree about what a session id is. The
caller pins a stable key (a uuid5 derived from the plan and the task);
the CLI owns the id of the conversation that was really created. The
mapping between the two is the only thing that makes a resume
possible, and it has to outlive the process that created it — a task
that was stopped and started again must land in the *same*
conversation, not in a brand-new one the caller believes is the old
one.

What is pinned here
-------------------

1. the mapping is **durable**: a second instance, reading the same
   file, resolves the same real id;
2. the registration is **persisted before the observed id is handed
   back** — that order is what keeps a crash from producing the
   unrecoverable state (an observed id that persisted but cannot be
   resolved back);
3. a value that is not a real mcode session id is **refused**, not
   stored;
4. a mapping that cannot be written **fails loudly** — no forged id,
   no silently substituted random one;
5. a mapping that is not there is a **miss** (``None``), which the
   caller turns into an explicit create — never into some other
   session;
6. the file is the caller's to place: nothing is written below the
   repository.

No process is spawned and no CLI is invoked (test decision point 2):
the mapping is a file, and the file is the whole observable.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from harness import mcode
from harness.mcode import SessionMap

pytestmark = pytest.mark.unit

#: The key the caller pins: a uuid5 derived from the plan and the task.
#: mcode can never be asked to honour it — it exists so that the same
#: task always looks up the same entry.
PINNED = str(uuid.uuid5(uuid.NAMESPACE_URL, "pdt:plan-1:task:3"))

#: The real session id, as measured on the CLI: the ``mvs_`` prefix and
#: 32 hex digits.
OBSERVED = "mvs_0ecaf7d26ff845a19fbaf6ecd45285e9"


def test_roundtrip_across_instances(tmp_path):
    """A second instance of the map resolves the id the first one wrote.

    This is the whole point of the file: the instance that registers
    the id is not the instance that will need to resolve it.
    """
    path = tmp_path / "session_map.json"

    SessionMap(path).register(PINNED, OBSERVED)

    # A fresh instance — as a later process would build — reads it back.
    assert SessionMap(path).lookup(PINNED) == OBSERVED

    # The file on disk is the mapping itself, not a wrapper around it.
    assert json.loads(path.read_text(encoding="utf-8")) == {PINNED: OBSERVED}

    # And it names a conversation, not the world: mode 0600.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"the mapping must be private, got {mode:04o}"


def test_register_before_return(tmp_path, monkeypatch):
    """The mapping is on disk before the observed id can be handed on.

    The order is the contract: a crash between the two states is
    survivable (an orphaned mcode conversation nobody references),
    while the other order is not (an observed id that no later process
    can resolve back). The write hook observes when the mapping is
    written; the ``return`` marker observes when the caller is free to
    hand the id on.
    """
    path = tmp_path / "session_map.json"
    order = []
    original = mcode._write_mapping

    def write_hook(target, mapping):
        original(target, mapping)
        order.append("register")

    monkeypatch.setattr(mcode, "_write_mapping", write_hook)

    def run_and_return():
        session_map = SessionMap(path)
        session_map.register(PINNED, OBSERVED)
        order.append("return")
        return OBSERVED

    assert run_and_return() == OBSERVED
    assert order == ["register", "return"], (
        "the mapping must be persisted before the observed id is handed "
        f"back, got {order}"
    )

    # And it is readable by the next instance at that same moment.
    assert SessionMap(path).lookup(PINNED) == OBSERVED


@pytest.mark.parametrize(
    "observed",
    [
        "",
        "not-a-session",
        "mvs_tooshort",
        "mvs_0ecaf7d26ff845a19fbaf6ecd45285e",  # 31 hex digits
        "mvs_0ecaf7d26ff845a19fbaf6ecd45285e99",  # 33 hex digits
        "mvs_0ecaf7d26ff845a19fbaf6ecd45285eZ",  # not hex
        "mvs_0ECAF7D26FF845A19FBAF6ECD45285E9",  # not lower case
        "mvs_0ecaf7d2-6ff845a19fbaf6ecd45285e9",  # dashes
        "session_0ecaf7d26ff845a19fbaf6ecd45285e9",  # another prefix
    ],
)
def test_invalid_value_rejected(tmp_path, observed):
    """A value that is not a real session id is refused, not stored.

    Storing it would put a key in the map that ``--session`` can never
    be given: the resume would fail at the CLI, far from the mistake
    that caused it.
    """
    path = tmp_path / "session_map.json"
    session_map = SessionMap(path)

    with pytest.raises(ValueError):
        session_map.register(PINNED, observed)

    assert session_map.lookup(PINNED) is None
    assert not path.exists(), "a refused value must not reach the disk"


def test_readonly_dir_fails_loudly(tmp_path):
    """A mapping that cannot be written fails the call.

    The alternative — carrying on without recording the id — produces
    a run that cannot be resumed, and it produces it *quietly*: the
    caller believes the session is pinned, and the next process finds
    nothing.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")

    directory = tmp_path / "read-only"
    directory.mkdir()
    path = directory / "session_map.json"
    session_map = SessionMap(path)

    directory.chmod(0o500)
    try:
        with pytest.raises(OSError):
            session_map.register(PINNED, OBSERVED)

        # Nothing was forged: no id was handed out, no file was written.
        assert session_map.lookup(PINNED) is None
        assert not path.exists()
        assert list(directory.iterdir()) == [], "no half-written leftovers"
    finally:
        directory.chmod(0o700)


def test_missing_mapping_is_a_miss(tmp_path):
    """A mapping that is not there reads as ``None``, not as another id.

    The caller above turns the miss into an explicit create; what it
    must never do is resolve the miss to some other conversation.
    """
    path = tmp_path / "session_map.json"

    assert SessionMap(path).lookup(PINNED) is None

    # A file that was never written, and one that is not a mapping at
    # all, are the same kind of miss.
    path.write_text("not json at all", encoding="utf-8")
    assert SessionMap(path).lookup(PINNED) is None


def test_map_path_is_the_callers(tmp_path):
    """Nothing is written below the repository: the path is given.

    The mapping belongs next to the task's ``JsonSession``, which only
    the caller knows where that is.
    """
    SessionMap(tmp_path / "session_map.json").register(PINNED, OBSERVED)

    repo_root = Path(__file__).resolve().parents[4]
    assert not (repo_root / "session_map.json").exists()
    assert not (repo_root / "backend" / "session_map.json").exists()
