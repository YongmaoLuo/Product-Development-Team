"""Budget tiers for the cross-verify runner.

Why the budget has two tiers
-----------------------------
The cross-verify step runs a task's own ``test_command``, and that
command does not have one runtime magnitude — it has two.

* A **full-suite lane** selects a *directory* of tests
  (``pytest tests/unit``).  Every module under it is imported, every
  fixture is built, and the wall time is the sum of the tree.  It costs
  minutes, and it grows with the suite.
* A **single-file lane** selects one file
  (``pytest tests/unit/test_x.py``).  One module is imported and its
  cases run.  It costs seconds.

One number for both is wrong in both directions.  Budget the
single-file lane at the full-suite figure and a stuck run costs the
caller the whole tree-sized wait, for a command that had either
finished or was genuinely wedged long before it.  Budget the full-suite
lane at the single-file figure and a run that was about to finish gets
killed instead — and a killed run reports itself as a failure, so a
slow pass turns into a red verdict.

The tier therefore follows what the command *selects*, and the number
behind each tier is configuration rather than a constant baked into the
caller: the right figure is a property of the machine and the suite, so
it is read from the environment.

``PDT_CROSS_VERIFY_TIMEOUT_SEC`` sizes the light lane and
``PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC`` sizes the heavy one.  A value
that is not a positive integer is ignored in favour of the default: a
mistyped budget must not become a zero-second timeout that fails every
verification it was meant to size.

How the tier is read
--------------------
The command is inspected as text, because that is what the caller has
and what decides the magnitude.  A command that runs ``pytest`` against
a path with a ``tests/`` *directory* segment (``tests/unit``,
``backend/tests/unit``) is the full-suite lane; a command that names a
single ``.py`` file under ``tests/`` is the single-file lane; a
command that does not run pytest at all (``cargo test``) has no pytest
magnitude and takes the light lane.

The ladder
----------
:func:`budget_ladder` returns three rungs: the tier's own budget, the
heavy budget, and twice the heavy budget.  A caller that escalates
needs the rungs to be non-decreasing, and needs them *known* rather
than guessed per call site — the next rung is the next attempt, and
the rungs are where that answer comes from.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

__all__ = [
    "BASE_TIMEOUT_ENV",
    "HEAVY_TIMEOUT_ENV",
    "DEFAULT_BASE_TIMEOUT_SEC",
    "DEFAULT_HEAVY_TIMEOUT_SEC",
    "is_heavy_lane",
    "timeout_seconds",
    "budget_ladder",
]


#: Environment variable holding the light-lane budget, in seconds.
BASE_TIMEOUT_ENV = "PDT_CROSS_VERIFY_TIMEOUT_SEC"

#: Environment variable holding the heavy-lane budget, in seconds.
HEAVY_TIMEOUT_ENV = "PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC"

#: Default light-lane budget, in seconds.
DEFAULT_BASE_TIMEOUT_SEC = 600

#: Default heavy-lane budget, in seconds.
DEFAULT_HEAVY_TIMEOUT_SEC = 1800


#: What separates one word of a shell command from the next.  Shell
#: operators are separators here for the same reason whitespace is: they
#: end a word, they are never part of a path.
_TOKEN_SPLIT = re.compile(r"[\s;&|()<>]+")

#: Quote characters that decorate a word without being part of a path.
_QUOTES = "\"'`"

#: The path segment that marks a test tree.
_TESTS_SEGMENT = "tests"

#: The suffix that makes a path a file rather than a directory.
_FILE_SUFFIX = ".py"


def _tokens(command: str) -> list[str]:
    """The words of ``command``, with quotes and operators removed."""
    if not isinstance(command, str) or not command.strip():
        return []
    words = []
    for raw in _TOKEN_SPLIT.split(command):
        word = raw.strip().strip(_QUOTES)
        if word:
            words.append(word)
    return words


def _names_tests_directory(word: str) -> bool:
    """Whether ``word`` names a directory under a ``tests/`` segment.

    A ``.py`` file under ``tests/`` is a single-file target, not a
    directory, so the suffix decides it.  A pytest node id
    (``tests/unit/test_x.py::test_case``) is the same file with a
    selector attached, so the selector is cut before the suffix is
    read — otherwise the file would look like a directory.
    """
    path = word.split("::", 1)[0]
    if not path or path.endswith(_FILE_SUFFIX):
        return False
    return any(segment == _TESTS_SEGMENT for segment in path.split("/") if segment)


def is_heavy_lane(command: str) -> bool:
    """Whether ``command`` is the full-suite lane rather than the light one.

    True when the command runs ``pytest`` against a ``tests/``
    *directory* — the whole tree, and therefore the long runtime.
    False for a single test file, for a command that does not run
    pytest at all, and for an empty command.
    """
    words = _tokens(command)
    if not any("pytest" in word for word in words):
        return False
    return any(_names_tests_directory(word) for word in words)


def _env_lookup(env: Optional[Mapping[str, Any]], name: str) -> Any:
    """Read ``name`` from ``env``, or from the process environment."""
    if env is None:
        return os.environ.get(name)
    try:
        return env.get(name)
    except AttributeError:
        # Not a mapping at all — nothing to read, so the default stands.
        return None


def _positive_seconds(raw: Any, default: int) -> int:
    """``raw`` as a positive number of seconds, or ``default``.

    Anything unusable is the default: an empty string, a value that is
    not a number, zero and a negative number all describe a budget that
    cannot be run, and a budget that cannot be run must not become a
    failure at every verification.
    """
    if raw is None:
        return default
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if seconds <= 0:
        return default
    return seconds


def timeout_seconds(command: str, env: Optional[Mapping[str, Any]] = None) -> int:
    """The budget in seconds for running ``command``.

    The heavy lane reads ``PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC`` and the
    light lane reads ``PDT_CROSS_VERIFY_TIMEOUT_SEC``; each falls back
    to its own default when the environment does not supply a usable
    value.  ``env`` defaults to the process environment.
    """
    if is_heavy_lane(command):
        return _positive_seconds(
            _env_lookup(env, HEAVY_TIMEOUT_ENV), DEFAULT_HEAVY_TIMEOUT_SEC
        )
    return _positive_seconds(
        _env_lookup(env, BASE_TIMEOUT_ENV), DEFAULT_BASE_TIMEOUT_SEC
    )


def budget_ladder(
    command: str, env: Optional[Mapping[str, Any]] = None
) -> tuple[int, int, int]:
    """The three budgets to try for ``command``, smallest first.

    The first rung is the budget the command's own tier gets; the
    second is the heavy budget; the third is twice the heavy budget.
    Each rung is at least the one before it, so a caller that escalates
    never spends a smaller budget than the one that already failed.
    """
    base = timeout_seconds(command, env)
    heavy = _positive_seconds(
        _env_lookup(env, HEAVY_TIMEOUT_ENV), DEFAULT_HEAVY_TIMEOUT_SEC
    )
    return (base, max(base, heavy), max(base, 2 * heavy))
