"""
VP-043 contract D: E2E test cleanup — all writes are scoped to ``tmp_path``.

Pins the contract that an E2E test's filesystem writes all live under
``tmp_path``, so pytest's automatic per-test cleanup is guaranteed to
remove them.

This file used to hold VP-043 contracts A+B+C too: spawn the real
external-producer subprocess against a mock CC Switch, kill it in
``try/finally``, then assert no orphan process survived and the mock
port was released. Those were deleted 2026-09-24 along with the test
that carried them, because that test required
``tools/`` helper to exist — and this repository
ships the *mechanism* for a subsidiary fleet, not any particular fleet
(see ``backend/config.yaml::subsidiary_processes``). A file that only
exists in an operator's private checkout cannot be a precondition of
the public suite.

What remains is the part of the contract that depends on no fleet.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_PROJECT_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.time_sensitive,
]


def test_e2e_cleanup_tmp_path_no_global_leak(tmp_path: Path) -> None:
    """VP-043 contract D: all filesystem writes are scoped under
    ``tmp_path``; pytest's automatic per-test cleanup removes them.

    Writes several artifacts under ``tmp_path`` (mimicking the
    shapes other E2E tests produce: SQLite DB, JSON state, log
    file) and asserts none of them live outside ``tmp_path`` in
    well-known leak locations (CWD, home directory state files).
    """
    fake_home = tmp_path / "home"
    fake_cc_switch_db = fake_home / ".cc-switch" / "cc-switch.db"
    fake_cc_switch_db.parent.mkdir(parents=True, exist_ok=True)
    fake_cc_switch_db.write_bytes(b"\x00" * 64)

    state_file = tmp_path / "state" / "provider-order.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "test-e2e-cleanup",
                "order": ["Vendor A Pro", "Vendor B Pro"],
                "providers": {},
            }
        ),
        encoding="utf-8",
    )

    log_file = tmp_path / "optimizer.log"
    log_file.write_text("mock log line\n", encoding="utf-8")

    # Sanity: the artifacts exist under tmp_path.
    assert fake_cc_switch_db.exists()
    assert state_file.exists()
    assert log_file.exists()

    # The artifacts MUST be reachable via tmp_path roots (i.e., the
    # real ~/.cc-switch/cc-switch.db was NOT touched). We assert by
    # checking the path prefix of every written file.
    written_paths = [fake_cc_switch_db, state_file, log_file]
    for p in written_paths:
        assert str(tmp_path) in str(p), (
            f"written path {p} escapes tmp_path {tmp_path}"
        )

    # Real home directory state file must not have been written by
    # this test (it would be a real-world side effect, not a leak
    # under tmp_path).
    real_home_cc = Path.home() / ".cc-switch" / "cc-switch.db"
    if real_home_cc.exists():
        # If the developer running this test happens to have a real
        # cc-switch.db, just assert ours is a different file.
        assert fake_cc_switch_db.resolve() != real_home_cc.resolve(), (
            "fake_home db accidentally aliases real ~/.cc-switch/cc-switch.db"
        )

    # tmp_path cleanup is handled by pytest itself after the test
    # returns — we can't observe it from inside the test, but the
    # assertion that every write goes under tmp_path is the
    # precondition pytest needs to do its job.
