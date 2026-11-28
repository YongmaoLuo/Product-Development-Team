"""
Agent Configuration
===================

Two kinds of configuration live in this module, and they are kept apart
on purpose.

**The domain agent's configuration** — :class:`AgentConfig` — is built
from a domain YAML file by :func:`backend.config_loader.load_config`.
It carries the prompts, the file patterns and the model map.

**The backend's own configuration** — the plain dict that
:func:`load_config` returns — is read from ``backend/config.yaml``.
That file is the operator's lever on the backend *process*: the
subsidiary processes the lifespan brings up, the scheduler's settings,
and the framework-fix lane's capacity, timeouts and merge policy.

The two loaders do not do the same job and are not interchangeable:
``config_loader.load_config`` needs a path and answers with an
:class:`AgentConfig`; ``config.load_config`` needs nothing and answers
with a dict — ``cfg["framework_fix"]["auto_merge"]`` and the rest of
the backend's settings, as the file spells them.

The framework-fix section
-------------------------
The framework-fix lane's settings are the one part of that file the
lane reads through a schema rather than as raw text, and the schema
has three rules.

**Absent means default.**  A missing section, a missing key inside it,
or a file that is not there at all means "use the default" — not
"fail".  The defaults are the ones the architecture's decision table
pins, and they are what the lane runs with when nobody has said
otherwise.

**A value that cannot be used is refused.**  A capacity of zero is not
a capacity, and a lane that accepted one would either admit nothing or
divide by it.  The refusal raises :class:`ConfigError`, so the mistake
is found when the file is written rather than when the lane
misbehaves.  A *missing* key never raises — that is the rule above.

**The merge switch is off unless somebody turned it on.**
``auto_merge`` merges a fix plan's work without asking the operator,
which is the one thing in this file whose wrong answer touches
somebody else's repository.  So it answers ``True`` only for the
boolean ``true``: a missing key, an empty value, a number, a quoted
string — every one of them leaves the switch off, and a value that
looks like an attempt to turn it on is warned about rather than
obeyed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

import yaml

log = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for domain-specific agent behavior."""

    # Planner prompts
    planner_system_prompt: str  # System prompt for planning
    planner_task_template: Optional[str] = None  # Template for task generation (optional)

    # Executor prompts
    executor_system_prompt: str = ""  # System prompt for execution
    executor_task_template: Optional[str] = None  # Template for task execution (optional)

    # Refiner prompts
    refiner_system_prompt: str = ""  # System prompt for refinement

    # Domain context
    domain_knowledge: str = ""  # Background knowledge for the domain
    file_patterns: List[str] = field(default_factory=lambda: ["*"])  # Relevant file patterns

    # Behavior settings
    max_retries: int = 5
    background_task_timeout: int = 1800  # 30 minutes max per task

    # Model map (复杂/中等模型分类). 详见 _base.yaml 中 model_map 段。
    # 结构：
    #   {
    #     "complex": {"vendor-b": {"default": "...", "haiku": "..."},
    #                 "vendor-a": {...}},
    #     "medium":  {"vendor-b": {"default": "...", "haiku": "..."},
    #                 "vendor-a": {...}},
    #   }
    # 当前默认 complex 和 medium 同值，预留未来对 medium 做降级处理。
    model_map: Optional[Dict[str, Dict]] = None

    # Provider fallback order — list of provider keys to try in order.
    # Reads from configs/_base.yaml  field, e.g.:
    #   provider_priority: [vendor-b, vendor-a]   # cheap first, then fallback
    #   provider_priority: [vendor-a, vendor-b]   # fallback first (e.g. for rate limit avoidance)
    # Each item must match a key in model_map (e.g. "vendor-b", "vendor-a").
    # "parent" is always appended implicitly as the last fallback.
    provider_priority: Optional[list] = None


# ---------------------------------------------------------------------------
# The backend's own configuration file
# ---------------------------------------------------------------------------

#: The file the backend's own settings are read from.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

#: The YAML section the framework-fix lane's settings are read from.
FRAMEWORK_FIX_SECTION = "framework_fix"

#: The framework-fix lane's defaults — what the lane runs with when the
#: configuration file says nothing.
#:
#: ``max_active`` and ``max_per_repo`` are the two capacity ceilings the
#: PRD fixed: at most two active fix plans overall, and at most two per
#: framework repository (one normal slot plus one oversubscribed one).
#: ``oversubscribe`` is how many slots may be taken beyond the ceiling,
#: and ``repro_runs`` is how many rounds one signature may be fixed in.
#: ``merge_reminder_minutes`` is how often an unapproved merge-ready
#: fix is announced again, ``waiting_on_user_merge_hours`` is how long
#: it waits for an answer before it is escalated, and
#: ``deadlock_minutes`` is how long a fix plan may sit without an
#: execution path before it is escalated.
#:
#: ``auto_merge`` is the odd one out: it is a switch, not a number, and
#: its default is *off*.  Merging is the operator's decision, and a
#: switch that defaults to on would make that decision without them.
DEFAULT_FRAMEWORK_FIX_CONFIG: Dict[str, Any] = {
    "max_active": 2,
    "max_per_repo": 2,
    "oversubscribe": 1,
    "repro_runs": 2,
    "merge_reminder_minutes": 30,
    "waiting_on_user_merge_hours": 2,
    "deadlock_minutes": 10,
    "auto_merge": False,
}

#: The smallest value each count may hold.  A capacity is a number of
#: plans, so it starts at one: zero is not "no plans", it is a ceiling
#: nothing can be admitted into.  ``oversubscribe`` is the number of
#: slots that may be taken *beyond* the ceiling, so it starts at zero.
#: A wait may start at zero — "escalate at once" is a coherent answer —
#: but a negative one is not a duration at all.
_FRAMEWORK_FIX_MINIMUMS: Dict[str, int] = {
    "max_active": 1,
    "max_per_repo": 1,
    "oversubscribe": 0,
    "repro_runs": 1,
    "merge_reminder_minutes": 0,
    "waiting_on_user_merge_hours": 0,
    "deadlock_minutes": 0,
}


class ConfigError(ValueError):
    """A configuration value the loader refuses to use.

    A :class:`ValueError` subclass, so a caller that only knows "this
    configuration is not usable" can catch ``ValueError`` and be right.
    """


def _coerce_int(value: Any, *, key: str, minimum: int) -> int:
    """Return ``value`` as an int of at least ``minimum``.

    A string that spells a number is a number — ``"2"`` is 2 — because
    a quoted number in a YAML file is a formatting accident, not a
    different intent.  A boolean is not a number: ``True`` is an ``int``
    in Python, but a flag is not a count.  Anything else, and anything
    below ``minimum``, is refused.
    """
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ConfigError(f"{key} must be a whole number, got {value!r}")
        number = int(value)
    elif isinstance(value, str):
        word = value.strip()
        try:
            number = int(word)
        except ValueError:
            try:
                as_float = float(word)
            except ValueError:
                raise ConfigError(
                    f"{key} must be a whole number, got {value!r}"
                ) from None
            if not math.isfinite(as_float) or not as_float.is_integer():
                raise ConfigError(f"{key} must be a whole number, got {value!r}")
            number = int(as_float)
    else:
        raise ConfigError(f"{key} must be a whole number, got {value!r}")

    if number < minimum:
        raise ConfigError(f"{key} must be at least {minimum}, got {number}")
    return number


def _auto_merge_setting(section: Mapping[str, Any]) -> bool:
    """Whether the merge switch is on.

    Only the boolean ``true`` turns it on.  Everything else — a missing
    key, an empty value, a number, a quoted string — leaves it off, and
    a value that looks like an attempt to turn it on is warned about
    rather than obeyed.  The switch merges a fix plan's work without
    asking, so being wrong about it is worse than being unable to use
    it.
    """
    if "auto_merge" not in section:
        return False

    value = section["auto_merge"]
    if value is True:
        return True
    if value is False or value is None:
        return False

    log.warning(
        "the %s.auto_merge switch stays off: only the boolean true turns it "
        "on, and %r is not that",
        FRAMEWORK_FIX_SECTION,
        value,
    )
    return False


def framework_fix_config(section: Any) -> Dict[str, Any]:
    """Turn a ``framework_fix`` section into the settings the lane runs with.

    Args:
        section: the raw section from the configuration file.  ``None``
            means the section is absent, which is not an error: every
            setting takes its default.

    Returns:
        A dict holding every setting of the section — the ones the file
        named, and the ones it stayed silent about.

    Raises:
        ConfigError: the section is not a mapping, or a value it names
            cannot be used.
    """
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ConfigError(
            f"the {FRAMEWORK_FIX_SECTION!r} section must be a mapping, "
            f"got {type(section).__name__}"
        )

    for name in sorted(set(section) - set(DEFAULT_FRAMEWORK_FIX_CONFIG)):
        log.warning(
            "ignoring unknown %s setting %r", FRAMEWORK_FIX_SECTION, name
        )

    values: Dict[str, Any] = dict(DEFAULT_FRAMEWORK_FIX_CONFIG)

    for name, minimum in _FRAMEWORK_FIX_MINIMUMS.items():
        if name not in section or section[name] is None:
            # Not set — the default above applies.
            continue
        values[name] = _coerce_int(
            section[name], key=f"{FRAMEWORK_FIX_SECTION}.{name}", minimum=minimum
        )

    values["auto_merge"] = _auto_merge_setting(section)
    return values


def _read_config_document(path: Path) -> Any:
    """Read a configuration file, or answer with an empty document.

    A file that is not there is not a failure: the backend runs on the
    defaults it carries.  A file that is there but cannot be read, or
    is not valid YAML, *is* a failure — a half-written configuration
    that is silently ignored is a configuration nobody notices is
    broken.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning(
            "no backend configuration at %s; the built-in defaults apply", path
        )
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"the backend configuration at {path} cannot be read: {exc}"
        ) from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"the backend configuration at {path} is not valid yaml: {exc}"
        ) from exc

    if document is None:
        return {}
    return document


def load_config(
    config_path: Optional[Union[str, Path, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Read the backend's own configuration as a plain dict.

    Args:
        config_path: where to read the configuration from — a path, or a
            document that has already been parsed.  ``None`` means the
            backend's own ``config.yaml``, the file this repository
            ships.

    Returns:
        The configuration, with its ``framework_fix`` section resolved:
        the settings the file named, and the defaults for the ones it
        did not.

    Raises:
        ConfigError: the file cannot be read, is not valid YAML, is not
            a mapping, or names a value that cannot be used.
    """
    if isinstance(config_path, Mapping):
        document: Any = dict(config_path)
    else:
        path = (
            Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
        )
        document = _read_config_document(path)

    if not isinstance(document, Mapping):
        raise ConfigError(
            f"the backend configuration must be a mapping, "
            f"got {type(document).__name__}"
        )

    config: Dict[str, Any] = dict(document)
    config[FRAMEWORK_FIX_SECTION] = framework_fix_config(
        config.get(FRAMEWORK_FIX_SECTION)
    )
    return config


@lru_cache(maxsize=1)
def framework_fix_settings(
    config_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """The framework-fix lane's settings, as the production reads them.

    :func:`load_config` is the loader; this is the **reader** — the one
    place the running backend asks "what does this lane run with?".

    The distinction matters because the two answers go to different
    places: the loader is asked by the code that starts the backend and
    refuses a configuration it cannot use, while the reader is asked by
    the code that is already running — the scheduler tick — and must not
    refuse to run because a file says something odd.  So a configuration
    this reader cannot use falls back to the defaults, with a warning:
    a tick that stops because of a number in a file is a lane nobody
    repairs.

    The thresholds live here rather than in the call site for the same
    reason they live in a file at all: a number written where it is used
    is a number nobody can change without reading the code.

    Args:
        config_path: where to read the configuration from.  ``None`` means
            the backend's own ``config.yaml`` — the file the operator
            edits.

    Returns:
        The framework-fix section, with the defaults filled in for every
        setting the file stayed silent about.

    Raises:
        ConfigError: never — an unusable configuration falls back to the
            defaults instead.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.warning(
            "the framework-fix settings fall back to their defaults "
            "(%s: %s)",
            type(exc).__name__,
            exc,
        )
        return dict(DEFAULT_FRAMEWORK_FIX_CONFIG)

    section = config.get(FRAMEWORK_FIX_SECTION)
    if not isinstance(section, Mapping):
        return dict(DEFAULT_FRAMEWORK_FIX_CONFIG)
    return dict(section)
