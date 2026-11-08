"""The one place a harness backend is chosen.

Why this module exists
======================

Choosing a backend used to be a code change: a branch in the middle of
the runner, a different argument to a different constructor, a decision
somebody had to remember to make in every place the process started a
model. A run could not be pointed at another backend without editing
and redeploying the process.

This module is that decision, written down once. It reads one
environment variable, refuses anything it does not recognise, and hands
back the adapter for the backend that was asked for. Everything above it
asks the registry instead of deciding for itself, so "which backend is
this process driving" has exactly one answer in the codebase.

Two rules make the choice safe to make once, at start-up.

**A wrong value is refused, not repaired.** ``PDT_HARNESS=MCODE`` is not
a typo to be quietly corrected — it is an operator who believes
something the process does not believe, and running the other backend
anyway would hide that. So an unrecognised value raises
:class:`HarnessConfigError`, naming the legal values, and nothing is
constructed.

**The value is checked before anything is built.** The check happens
first, so a misconfigured process cannot leave a half-built adapter
behind — and so a test can assert that no factory was called.

Choosing the backend is also where the MiniMax Code backend's profile
is bound — but not here. The map that translates a PDT provider name
into a MiniMax Code account belongs to the backend that runs the
account, and it is read by :mod:`harness.startup`: the start-up
decides, from the environment it is given, which account the process
runs on, and hands the answer to the adapter this module builds. The
two halves are kept apart on purpose. The decision used to live inside
the adapter factory, where it read ``os.environ`` — so a caller that
handed this module an environment of its own got an answer about the
process environment instead, and the decision could not be reproduced
from the environment the caller had in hand.

The default path is deliberately lazy. Importing this module must not
import an adapter: the adapter modules bring their own dependencies, and
a process that only ever asked a question about configuration should not
pay for them. The imports therefore live in the body of
:func:`_default_factory`, and a caller that wants a different adapter —
a test, or a future backend — injects a factory instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

from .contract import Harness, HarnessConfigError

__all__ = [
    "DEFAULT_HARNESS_NAME",
    "HARNESS_ENV_VAR",
    "HARNESS_NAMES",
    "RuntimeConfig",
    "get_runtime_config",
    "reset_runtime_config_for_tests",
    "resolve_harness",
    "resolve_harness_name",
]

#: The environment variable that selects the backend.
HARNESS_ENV_VAR = "PDT_HARNESS"

#: The backend that runs when nothing is configured.
DEFAULT_HARNESS_NAME = "claude"

#: The backends this process knows how to run. The order is the order
#: the error message lists them in.
HARNESS_NAMES: Tuple[str, ...] = ("claude", "mcode")


@dataclass(frozen=True)
class RuntimeConfig:
    """The process-wide harness choice.

    Attributes:
        harness: The adapter that runs.
        name: Which backend it is — ``"claude"`` or ``"mcode"``. The
            adapter does not have to know its own name, so the registry
            keeps it next to the adapter rather than inside it.
    """

    harness: Harness
    name: str


def resolve_harness_name(env: Mapping[str, str]) -> str:
    """The backend ``env`` asks for, or the default when it asks for none.

    Args:
        env: The environment to read :data:`HARNESS_ENV_VAR` from.

    Returns:
        The name of the selected backend.

    Raises:
        HarnessConfigError: The variable is set to something this
            process does not know how to run. The message names the
            legal values, because an operator who typed a wrong value
            needs to be told what the right ones are.
    """
    raw = env.get(HARNESS_ENV_VAR)
    if raw is None:
        return DEFAULT_HARNESS_NAME
    if raw not in HARNESS_NAMES:
        raise HarnessConfigError(
            f"{HARNESS_ENV_VAR} must be {_allowed_values()}, got {raw!r}"
        )
    return raw


def _allowed_values() -> str:
    """The legal backend names, as they read inside an error message."""
    return " or ".join(repr(name) for name in HARNESS_NAMES)


def _default_factory(name: str) -> Callable[[], Harness]:
    """The built-in adapter constructor for ``name``.

    The import is deferred into this body on purpose: importing
    :mod:`harness.registry` must not import an adapter, so that a
    process which only asks about configuration does not pay for an
    adapter's dependencies — and so that a backend this process does not
    use is never loaded at all.

    The constructor builds a **stateless** adapter. The MiniMax Code
    account the process runs on is decided by :mod:`harness.startup`
    from the environment the start-up is given, and bound there; the
    factory does not read the environment, so it cannot answer a
    question about a caller's environment by accident.

    Args:
        name: The backend to build.

    Returns:
        A constructor that builds the adapter.

    Raises:
        HarnessConfigError: There is no built-in adapter for ``name``.
    """
    if name == "claude":
        from .claude import ClaudeHarness

        return ClaudeHarness
    if name == "mcode":
        from .mcode import McodeHarness

        return McodeHarness
    raise HarnessConfigError(
        f"no built-in harness for {name!r}; expected {_allowed_values()}"
    )


def _resolve(
    env: Mapping[str, str],
    factories: Optional[Mapping[str, Callable[[], Harness]]] = None,
) -> Tuple[str, Harness]:
    """The selected backend's name and the adapter that runs it.

    The name is validated first, before any factory is reached for: a
    value the process cannot honour must not leave a built adapter
    behind.
    """
    from .startup import resolve_startup_config

    config = resolve_startup_config(env, factories=factories)
    return config.name, config.harness


def resolve_harness(
    env: Mapping[str, str],
    factories: Optional[Mapping[str, Callable[[], Harness]]] = None,
) -> Harness:
    """Build the adapter ``env`` selects.

    The environment is the whole of the answer. Which backend runs is
    read from it, and so is the MiniMax Code account that backend runs
    on: :mod:`harness.startup` decides both from ``env`` alone, so a
    caller that hands this function an environment of its own gets an
    adapter decided by that environment.

    Args:
        env: The environment to read :data:`HARNESS_ENV_VAR` from.
        factories: Adapter constructors to use instead of the built-in
            ones. A name the mapping does not mention falls back to the
            built-in constructor for that name.

    Returns:
        The adapter for the selected backend.

    Raises:
        HarnessConfigError: :data:`HARNESS_ENV_VAR` names a backend
            this process cannot run. Raised before any factory is
            called, so a refused configuration builds nothing.
    """
    _, harness = _resolve(env, factories)
    return harness


#: The cached configuration. ``None`` means "not resolved yet".
_RUNTIME_CONFIG: Optional[RuntimeConfig] = None


def get_runtime_config() -> RuntimeConfig:
    """The process-wide harness configuration, resolved once.

    The configuration is resolved on the first call and cached from then
    on: a run must not change backend halfway through because the
    environment changed under it. An invalid value raises before
    anything is cached, so the next call tries again — the operator has
    to be told, not silently served the default.

    Returns:
        The configuration for this process.
    """
    global _RUNTIME_CONFIG
    if _RUNTIME_CONFIG is None:
        from .startup import resolve_startup_config

        # The start-up decides everything this process runs on — the
        # backend and the account it runs on — from the process
        # environment, and records the decision as it goes.
        config = resolve_startup_config(os.environ)
        _RUNTIME_CONFIG = RuntimeConfig(harness=config.harness, name=config.name)
    return _RUNTIME_CONFIG


def reset_runtime_config_for_tests() -> None:
    """Forget the cached configuration, so the next call resolves again."""
    global _RUNTIME_CONFIG
    _RUNTIME_CONFIG = None