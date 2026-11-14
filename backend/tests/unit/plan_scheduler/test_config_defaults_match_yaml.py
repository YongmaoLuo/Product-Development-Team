"""Unit tests: the default minimum runtime is zero in all three places.

What this file holds down
-------------------------
The scheduler's minimum runtime — the threshold ``M``, how long a young
run is left alone before it may be interrupted — is written down three
times in this repository:

1. ``backend/plan_scheduler/config.py`` — the constant
   :data:`DEFAULT_MIN_RUNTIME_SECONDS`, which is what the loader falls
   back to for a key no configuration file names;
2. ``backend/config.yaml`` — the ``scheduler:`` section this repository
   ships, which is what a fresh checkout runs with;
3. ``example/scheduler.yaml.example`` — the file an operator copies to
   set the scheduler up for themselves.

Those three have to say the same thing.  The default is **zero**: no run
is protected on age alone, so the plan that has run the shortest is the
one the scheduler interrupts first.  A default that is zero in the code
but 600 in the file an operator copies is a default that gives every
operator a ten-minute protection window nobody asked for — and the
shortest-running plan can never reach the candidate set.

This file reads all three and asserts each of them **separately**, so a
change made to one of them and not to the others is reported against
the place that was left behind, instead of hiding behind a combined
comparison that passes as long as any one of them agrees.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from plan_scheduler.config import DEFAULT_MIN_RUNTIME_SECONDS

pytestmark = pytest.mark.unit


#: The backend directory — ``backend/``.
BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: The repository root.
REPO_ROOT = BACKEND_ROOT.parent

#: The configuration file this repository ships.
CONFIG_PATH = BACKEND_ROOT / "config.yaml"

#: The example file an operator copies into their own config directory.
EXAMPLE_PATH = REPO_ROOT / "example" / "scheduler.yaml.example"


def _scheduler_value(path: Path) -> Any:
    """Return the ``scheduler.min_runtime_seconds`` the file carries."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} must be a mapping"
    section = document.get("scheduler")
    assert isinstance(section, dict), f"{path.name} must carry a scheduler section"
    return section.get("min_runtime_seconds")


def test_python_yaml_and_example_agree_on_zero() -> None:
    """The constant, the shipped file and the example all say zero.

    Three places, three assertions.  Each one names the place that was
    left behind, so a change made to two of the three and forgotten in
    the third fails here — and the failure says which one.

    The cross-checks at the end say the same thing from the other
    side: the two files and the constant cannot drift apart, whichever
    of them is moved.
    """
    python_default = DEFAULT_MIN_RUNTIME_SECONDS
    config_yaml_value = _scheduler_value(CONFIG_PATH)
    example_value = _scheduler_value(EXAMPLE_PATH)

    assert python_default == 0.0, (
        "plan_scheduler.config.DEFAULT_MIN_RUNTIME_SECONDS must be 0.0"
    )
    assert config_yaml_value == 0, (
        f"{CONFIG_PATH.name}: scheduler.min_runtime_seconds must be 0"
    )
    assert example_value == 0, (
        f"{EXAMPLE_PATH.name}: scheduler.min_runtime_seconds must be 0"
    )

    assert config_yaml_value == python_default, (
        f"{CONFIG_PATH.name} and plan_scheduler.config disagree"
    )
    assert example_value == python_default, (
        f"{EXAMPLE_PATH.name} and plan_scheduler.config disagree"
    )