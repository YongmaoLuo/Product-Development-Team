"""The start-up decision: which backend runs, and on which account.

Why this module exists
======================

Choosing a backend is one decision; *running it on an account* is
another. :mod:`harness.registry` answers the first — "which backend
does this process drive" — and this module answers the second: "on
which MiniMax Code account does it run, and how would anybody tell".

The two used to be one thing, and that was the problem. The profile
map — the file that translates a PDT provider name into a MiniMax Code
account name — was read inside the adapter factory, and the factory
read ``os.environ`` to decide whether to read the map at all. So a
caller that handed the registry an environment of its own got an
answer about the *process* environment instead: the environment the
caller had in hand could not reproduce the decision, could not test
it, and could not explain it.

So the decision is lifted out of the factory and into this module:

* the environment is **passed in**, never read from the process;
* the map is opened **only** by the process that runs the backend the
  map is for — a Claude Code deployment never opens it, not even to
  look at it;
* the answer is **audited**: one log record says which backend ran, on
  which account, and why that account was chosen — so "the map is
  configured but it did not take effect" is a fact in the log rather
  than a suspicion.

The rules the map obeys are the ones
:mod:`mcode_profiles` has always held to: a map that names an account
this machine does not have **stops** the start-up and names the
account; a map that is absent, a provider the map does not name, and a
process with no provider at all fall back to the default account
without stopping anything.

The shape of the answer
=======================

:func:`resolve_startup_config` is a function of one environment. It
returns a :class:`StartupConfig`, which says what was decided and why:

* ``binding`` — the account the adapter runs on, or ``None`` for the
  default account;
* ``profile_source`` — where that answer came from, so a log reader
  can tell "there was no map" from "the map did not name this
  provider" from "the map named it and it was applied";
* ``map_path`` and ``provider`` — the two facts the decision was made
  from.

Everything the module touches on the machine — the map file, the
profile tool — is injectable, so a test can decide the whole start-up
without a real ``mcode-profile`` binary and without a real YAML file.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Iterable,
    List,
    Mapping,
    Optional,
)

from .contract import Harness
from .registry import resolve_harness_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcode_profiles import ProfileBinding

logger = logging.getLogger(__name__)

__all__ = [
    "MCODE_HARNESS_NAME",
    "PROFILE_SOURCE_DEFAULT",
    "PROFILE_SOURCE_MAPPED",
    "PROFILE_SOURCE_NO_MAP",
    "PROFILE_SOURCE_NO_PROVIDER",
    "StartupConfig",
    "resolve_startup_config",
]

#: The backend whose accounts come from the profile map.
MCODE_HARNESS_NAME = "mcode"

#: The map named this provider, and the account was applied.
PROFILE_SOURCE_MAPPED = "mapped"

#: The map did not name this provider: the default account applies.
PROFILE_SOURCE_DEFAULT = "default"

#: The process runs no provider, so there was nothing to look up.
PROFILE_SOURCE_NO_PROVIDER = "no-provider"

#: There is no map to look in.
PROFILE_SOURCE_NO_MAP = "no-map"


@dataclass(frozen=True)
class StartupConfig:
    """What the process decided when it started.

    Attributes:
        name: The backend that runs.
        harness: The adapter that runs.
        binding: The MiniMax Code account the adapter runs on, or
            ``None`` when the default account applies.
        profile_source: Where the account came from — one of
            :data:`PROFILE_SOURCE_MAPPED`,
            :data:`PROFILE_SOURCE_DEFAULT`,
            :data:`PROFILE_SOURCE_NO_PROVIDER` or
            :data:`PROFILE_SOURCE_NO_MAP`.
        map_path: The map file the decision was made from, or ``None``
            when no map was consulted.
        provider: The provider the decision was made for, or ``None``
            when the process runs no provider.
    """

    name: str
    harness: Harness
    binding: Optional["ProfileBinding"] = None
    profile_source: str = PROFILE_SOURCE_DEFAULT
    map_path: Optional[Path] = None
    provider: Optional[str] = None


class _KnownProfiles:
    """The accounts this machine has, listed the first time they are needed.

    The account list is a fact only the ``mcode-profile`` tool can
    answer, and the tool may not even be installed. So it is asked for
    lazily: a start-up that never looks at a map never asks the
    machine about its accounts, and a start-up that looks at one asks
    exactly once.
    """

    def __init__(self, lister: Callable[[], Iterable["ProfileBinding"]]) -> None:
        self._lister = lister
        self._profiles: Optional[List["ProfileBinding"]] = None

    def __iter__(self):
        if self._profiles is None:
            self._profiles = list(self._lister())
        return iter(self._profiles)


def resolve_startup_config(
    env: Mapping[str, str],
    *,
    factories: Optional[Mapping[str, Callable[[], Harness]]] = None,
    profile_lister: Optional[Callable[..., Iterable["ProfileBinding"]]] = None,
    profile_map_loader: Optional[Callable[..., Mapping[str, str]]] = None,
) -> StartupConfig:
    """The start-up decision, made from ``env``.

    Args:
        env: The environment to decide from. It is the *only* source of
            the decision: the backend, the provider and the map are all
            read from here, never from the process environment.
        factories: Adapter constructors to use instead of the built-in
            ones. A backend the mapping does not mention falls back to
            the built-in constructor.
        profile_lister: How the accounts this machine has are
            enumerated. The default runs the ``mcode-profile`` tool.
        profile_map_loader: How the provider → account map is read.
            The default reads the map file the environment points at.

    Returns:
        The decision: the backend, the adapter, the account it runs on
        and where that answer came from.

    Raises:
        HarnessConfigError: ``env`` names a backend this process cannot
            run, or the map names an account this machine does not
            have. Both stop the start-up: running on the default
            account while the operator believes a specific one was
            chosen is the failure the map exists to prevent.
    """
    name = resolve_harness_name(env)

    if name != MCODE_HARNESS_NAME:
        # The Claude Code path. The map is not opened at all — not read,
        # not resolved, not imported. A deployment that runs Claude Code
        # starts cleanly even when the map file is broken.
        config = StartupConfig(
            name=name,
            harness=_build_harness(name, None, factories),
            profile_source=PROFILE_SOURCE_DEFAULT,
        )
        return _audit(config)

    # The MiniMax Code path. Everything below reads the map, and the
    # map is read only here.
    from mcode_profiles import (
        list_profiles,
        load_startup_mapping,
        map_file_for,
        resolve_profile,
        resolve_startup_provider,
        validate_mapping,
    )

    provider = resolve_startup_provider(env)
    if provider is None:
        # Nothing to look up: the map is not opened, the default
        # account applies, and the start-up goes on.
        config = StartupConfig(
            name=name,
            harness=_build_harness(name, None, factories),
            profile_source=PROFILE_SOURCE_NO_PROVIDER,
        )
        return _audit(config)

    known = _KnownProfiles(
        profile_lister if profile_lister is not None else list_profiles
    )
    map_path = map_file_for(env)

    if profile_map_loader is not None:
        mapping = _call_loader(profile_map_loader, env)
    else:
        # The loader reads the map *and* checks that every account it
        # names is one this machine has. It is given the lazy account
        # list, so the tool is only run when there is a map to check.
        mapping = load_startup_mapping(env=env, profiles=known)

    if not mapping:
        # No map, or an empty one: the default account applies and
        # nothing is injected.
        config = StartupConfig(
            name=name,
            harness=_build_harness(name, None, factories),
            profile_source=PROFILE_SOURCE_NO_MAP,
            map_path=map_path,
            provider=provider,
        )
        return _audit(config)

    profiles = list(known)
    # The map names accounts; the machine must have them. A name it does
    # not have is named in the error, so the operator knows which line
    # of the map to fix.
    validate_mapping(mapping, profiles)

    binding = resolve_profile(mapping, provider, profiles)
    config = StartupConfig(
        name=name,
        harness=_build_harness(name, binding, factories),
        binding=binding,
        profile_source=(
            PROFILE_SOURCE_MAPPED if binding is not None else PROFILE_SOURCE_DEFAULT
        ),
        map_path=map_path,
        provider=provider,
    )
    return _audit(config)


def _build_harness(
    name: str,
    binding: Optional["ProfileBinding"],
    factories: Optional[Mapping[str, Callable[[], Harness]]],
) -> Harness:
    """The adapter the start-up runs.

    A caller that injected a factory owns the adapter it gets: the
    factory is called with no arguments, exactly as
    :func:`harness.registry.resolve_harness` has always called it.

    Args:
        name: The backend to build.
        binding: The account to bind, or ``None`` for the default one.
        factories: Adapter constructors to use instead of the built-in
            ones.

    Returns:
        The adapter for the selected backend.
    """
    if factories is not None:
        factory = factories.get(name)
        if factory is not None:
            return factory()

    # The registry owns the built-in constructors: it is the module that
    # decides which adapter a backend name means, and it keeps the
    # adapter imports out of the import path.
    from .registry import _default_factory

    constructor = _default_factory(name)
    if binding is None:
        return constructor()

    # The MiniMax Code adapter takes the account at construction: the
    # adapter itself is stateless, and the account is a fact about this
    # start-up rather than about the adapter.
    from .mcode import McodeHarness

    return McodeHarness(binding=binding)


def _call_loader(loader: Callable, env: Mapping[str, str]):
    """Call an injected loader, with the environment when it takes one.

    An injected loader is a test seam, and a test seam should not have
    to agree on a signature to be useful: a loader that wants the
    environment gets it, a loader that does not is called with none.

    Args:
        loader: The injected loader.
        env: The environment to hand it.

    Returns:
        Whatever the loader returned.
    """
    if not callable(loader):
        return loader

    try:
        parameters = inspect.signature(loader).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        parameters = {}

    if parameters:
        return loader(env)
    return loader()


def _audit(config: StartupConfig) -> StartupConfig:
    """Record the decision, so "configured but not applied" is visible.

    A map that is configured and never applied looks exactly like a map
    that was never configured, unless something says which it was. This
    record says: which backend ran, on which account, for which
    provider, decided from which map — and where the account came from.

    Args:
        config: The decision that was made.

    Returns:
        The same decision, unchanged.
    """
    payload = {
        "harness": config.name,
        "provider": config.provider,
        "profile": config.binding.name if config.binding is not None else None,
        "profile_source": config.profile_source,
        "map_path": str(config.map_path) if config.map_path is not None else None,
    }
    logger.info(
        "harness_startup harness=%s provider=%s profile=%s profile_source=%s "
        "map_path=%s",
        payload["harness"],
        payload["provider"],
        payload["profile"],
        payload["profile_source"],
        payload["map_path"],
        extra={"data": payload},
    )
    return config
