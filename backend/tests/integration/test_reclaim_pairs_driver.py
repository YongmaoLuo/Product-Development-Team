"""The driver for the three process-state contracts in ``reclaim_pairs.py``.

Why this exists
---------------
``reclaim_pairs.py`` holds three pairs — a polluter that ends with its
hands full, and the test right after it that asserts the next test began
with clean hands. The property they pin is "the autouse teardown in
``tests/conftest.py`` reclaimed what the previous test left", and it is
only observable *across* a test boundary: by the time one test is running,
the teardown it would have to watch has either not happened yet or already
finished.

That makes the pair definition order, and definition order is not a
property a parallel scheduler honours. Left in ``tests/unit`` the polluter
and its checker get split across workers — ``-n`` fans a file out, and
``--dist worksteal`` moves tests between workers mid-run — and the checker
then finds an empty module and fails for a reason that has nothing to do
with the code under test. That is not hypothetical: it is what a
three-seed shuffled run of ``tests/unit`` caught.

So the pairs run in their own session, started by exactly one test here.
The outer suite sees a single atomic test with no ordering dependency, and
everything else in the shard is free to use the scheduler that balances
it best.

Why the driver carries ``integration``
--------------------------------------
This test starts a second interpreter, and the unit lanes' expression does
not filter a file out — only the tests inside it — so an unmarked driver
would be collected there and run under a per-test ceiling a cold
interpreter start cannot meet on a hosted runner. That is the same rule
``test_ci_lanes_match_marker_contracts.py`` states for the other nested
drivers, and the reason it exists is the first complete CI run, where the
nested run was killed mid-``subprocess.run``.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
_PAIRS = pathlib.Path(__file__).resolve().parent / "reclaim_pairs.py"

#: ``addopts`` is inherited by the nested run, and so is ``PYTEST_ADDOPTS``
#: from the operator's shell. Anything that turned that session parallel
#: would split the pairs apart again and defeat the whole arrangement, so
#: the one flag that starts a scheduler is disabled on the command line
#: rather than left to the environment.
_NO_SCHEDULER = ("-p", "no:xdist")


def _declared_pairs() -> int:
    """How many tests ``reclaim_pairs.py`` says it holds.

    Derived rather than written down: a pair added to that file without a
    matching update here must not silently fall outside the check.
    """
    return len(re.findall(r"^def test_", _PAIRS.read_text(encoding="utf-8"), re.M))


def test_the_reclaim_pairs_run_green_in_their_own_session() -> None:
    """The nested session passes, and it ran every pair the file declares.

    ``returncode`` alone is not enough. pytest exits 5 for "no tests were
    collected", which is a different failure from a pair that broke, and a
    driver that only read the exit status would report an emptied file as
    health. The count is asserted so the two cannot be confused.
    """
    expected = _declared_pairs()
    assert expected, "reclaim_pairs.py declares no tests — the check below would pass vacuously"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(_PAIRS), "-q", "--no-header", *_NO_SCHEDULER],
        cwd=str(_BACKEND_DIR),
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, (
        f"the reclaim pairs failed in their own session (exit {proc.returncode}).\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    reported = re.search(r"(\d+) passed", proc.stdout)
    assert reported is not None, (
        f"the nested run reported no pass count.\n--- stdout ---\n{proc.stdout}"
    )
    assert int(reported.group(1)) == expected, (
        f"the nested run passed {reported.group(1)} of the {expected} tests "
        f"reclaim_pairs.py declares.\n--- stdout ---\n{proc.stdout}"
    )


def test_the_pairs_file_is_not_collected_by_any_lane() -> None:
    """The pairs run only when named, never as part of a shard.

    The arrangement depends entirely on ``reclaim_pairs.py`` not matching
    ``python_files``: the moment a lane collects it on its own, the pairs
    are back to being split across workers by whatever scheduler that lane
    uses. Checked against the configured pattern rather than the default,
    so an override in ``pytest.ini`` cannot quietly reintroduce it.
    """
    pytest_ini = _BACKEND_DIR / "pytest.ini"
    configured = re.search(
        r"^\s*python_files\s*=\s*(.+)$",
        pytest_ini.read_text(encoding="utf-8"),
        re.M,
    )
    patterns = (
        [p.strip() for p in configured.group(1).split(",")]
        if configured
        else ["test_*.py"]
    )

    name = _PAIRS.name
    for pattern in patterns:
        assert not pathlib.PurePath(name).match(pattern), (
            f"{name} matches python_files={pattern!r}, so a lane will collect "
            f"it and split the pairs across workers. Rename it, or the "
            f"encapsulation this driver provides is gone."
        )
