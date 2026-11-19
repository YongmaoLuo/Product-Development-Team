"""The one place where a plan's ``mode`` becomes a value.

A plan may declare that it runs in ``test`` mode: the pipeline is
exercised, but the run must not touch anything real.  A plan that says
nothing runs for real -- that is what every plan did before modes
existed, and it is the mode that is safe to assume.

Several readers need that value.  The cost gate decides what may be
skipped, the executor injects the mode into the subprocess environment,
the verification sub-agent reads it back out of its settings.  If each of
them parsed the mode on its own, the parsers would drift: one strips and
lower-cases, another does not; one rejects ``prod``, another accepts it.
Then the cost gate believes the plan is in ``test`` mode while the
subprocess believes it is in ``production`` mode -- and the run does the
opposite of what it was asked to do.

So the parsing lives here, once.

The contract
------------
:func:`resolve_mode` returns ``(mode, is_fallback)``:

* a **valid** mode resolves to itself after ``strip()`` and ``lower()``,
  and ``is_fallback`` is ``False``;
* a **missing** mode -- ``None``, an empty string, a whitespace-only
  string, or no key at all -- resolves to ``production`` with
  ``is_fallback`` ``True``, and logs ``plan_mode_missing``;
* an **invalid** mode resolves to ``production`` with ``is_fallback``
  ``True``, and logs ``plan_mode_invalid=`` naming the value that was
  ignored.

``production`` is the fallback because it is the mode that must be safe
to assume: a plan that says nothing gets run for real.  Falling back to
``test`` would silently turn real work into a rehearsal.

Nothing here raises.  A mode is metadata, and metadata must not be able
to stop a plan from running -- but it is never dropped silently either.
Both fallbacks log a warning, so a plan whose mode was ignored shows up
in the logs instead of being indistinguishable from a plan that declares
``production`` on purpose.

Only the standard library is imported here.  This module is imported by
the cost gate, by the executor and by the verification sub-agent
registry, so it must not import anything that imports them back.
"""

from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)

#: The environment variable that carries the mode into a subprocess.
PLAN_MODE_ENV = "PDT_PLAN_MODE"

#: The default mode -- a plan that says nothing runs for real.
MODE_PRODUCTION = "production"

#: The mode for a plan that only exercises the pipeline.
MODE_TEST = "test"

#: Every mode this codebase knows.  A value outside this set is
#: invalid, and is replaced by :data:`MODE_PRODUCTION`.
VALID_MODES: Tuple[str, ...] = (MODE_PRODUCTION, MODE_TEST)

__all__ = [
    "PLAN_MODE_ENV",
    "MODE_PRODUCTION",
    "MODE_TEST",
    "VALID_MODES",
    "resolve_mode",
    "mode_from_flags",
    "plan_mode_env",
]


def _fallback_missing() -> Tuple[str, bool]:
    """The production fallback for a mode that was never given."""
    logger.warning("plan_mode_missing fallback=production")
    return MODE_PRODUCTION, True


def _fallback_invalid(raw: Any) -> Tuple[str, bool]:
    """The production fallback for a mode that is not a mode."""
    logger.warning("plan_mode_invalid=%r fallback=production", raw)
    return MODE_PRODUCTION, True


def resolve_mode(raw: Any) -> Tuple[str, bool]:
    """Resolve a raw mode value into ``(mode, is_fallback)``.

    ``raw`` is whatever the caller had: a string, ``None``, or a value of
    some other type.  A string is matched against :data:`VALID_MODES`
    after ``strip()`` and ``lower()``, so ``" Test "`` is the ``test``
    mode.  ``None``, an empty string and a whitespace-only string count
    as **missing**; anything else that is not a known mode counts as
    **invalid**.

    Both fallbacks return ``(MODE_PRODUCTION, True)`` and log a warning
    -- see the module docstring for why the fallback is ``production``
    and why it is never silent.
    """
    if raw is None:
        return _fallback_missing()

    if not isinstance(raw, str):
        return _fallback_invalid(raw)

    candidate = raw.strip().lower()
    if not candidate:
        return _fallback_missing()

    if candidate in VALID_MODES:
        return candidate, False

    return _fallback_invalid(raw)


def mode_from_flags(flags: Any) -> Tuple[str, bool]:
    """Resolve the mode stored in a plan's ``flags`` mapping.

    The mode lives under ``flags["mode"]``.  A ``flags`` value that is
    not a dict is treated as a missing mode rather than as an error: the
    caller has no flags to read, and that is the same situation as a
    dict without the key.
    """
    if not isinstance(flags, dict):
        return _fallback_missing()

    return resolve_mode(flags.get("mode"))


def plan_mode_env(mode: Any) -> dict[str, str]:
    """Build the environment fragment that carries ``mode`` to a child.

    The value is **resolved** first, so an invalid mode never reaches a
    subprocess as-is: the child is told ``production`` -- the same
    answer every other reader of the mode gets.
    """
    resolved, _fallback = resolve_mode(mode)
    return {PLAN_MODE_ENV: resolved}