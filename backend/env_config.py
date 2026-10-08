"""Read the deployment's dotenv into the process environment.

The loader has one job: get ``<repo>/.env`` into ``os.environ`` so that
whatever reads configuration from the environment — the notifiers, the
provider layer, the Claude CLI's own subprocesses — sees the same values.
It is called by the two ``main()`` surfaces (:mod:`cli` and
:mod:`pipeline`), which do not import :mod:`server` and therefore do not
get the dotenv any other way.

**Nothing here is required, and that is deliberate** (2026-10-08). This
used to demand three variables and raise on the first absent one. All
three claims have since stopped being true:

* A provider API key is not this repository's to require. Its value is
  supplied at runtime by whatever provider layer a deployment runs —
  :mod:`cc_switch` is the integration this codebase ships, and it reads
  one provider row at a time — and a deployment with no such layer
  simply runs no routed provider. Demanding the key *here* could only
  ever recognise a deployment as configured when the value happened to
  be in *this* file, so any deployment whose credentials were supplied
  the supported way was refused a start it did not need. The demand was
  fiction in the other direction too: nothing in the production path
  ever read the value this module returned. Both call sites discard the
  returned dict.
* The other two names have no reader anywhere in this repository. What
  remains of the feature they were configuration for is a skill name
  inside generated PRD prose — an instruction to a model, not a lookup.

So the list is empty, and that is the accurate description of the
deployment rather than a relaxation of a real constraint. See
:data:`config_paths.ENV_FILE` for where the file lives and why the two
historical locations disagreed.

What is deliberately *not* done here: inferring whether the deployment
has usable credentials. That question is answerable — :mod:`credentials`
can enumerate the configured providers — but not from this module, whose
inputs are a path and ``os.environ``. A loader that guessed would either
duplicate that logic or fail in a new way, so it stays a loader.
"""
from __future__ import annotations

import os
from typing import Dict, List

from config_paths import ENV_FILE

try:
    from dotenv import load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

#: Variables the loader refuses to return without.
#:
#: Empty — see the module docstring for why each of the three names it
#: used to hold stopped being a real requirement. It stays a list, and
#: stays exported, because ``load_env_config`` is written against it and a
#: future deployment-specific requirement should be added here rather than
#: open-coded into the loader.
REQUIRED_VARS: List[str] = []

#: Values used when the environment does not supply them.
DEFAULTS: Dict[str, str] = {
    "TIMEZONE": "Asia/Shanghai",
}


def load_env_config(env_path: str = None) -> Dict[str, str]:
    """Load the dotenv at ``env_path`` and return the resolved settings.

    ``env_path`` defaults to :data:`config_paths.ENV_FILE` — the
    repository root's ``.env``, the same file the server reads. It is
    loaded without ``override``, so a variable already exported in the
    shell wins over the file; that precedence is the reason a deployment
    can be pointed at a different configuration for one run without
    editing anything.

    A missing file is not an error. Plenty of deployments export their
    configuration directly — a container's ``env:``, a CI job's
    environment — and requiring a file they legitimately do not have would
    be a startup failure over nothing.
    """
    if env_path is None:
        env_path = str(ENV_FILE)

    if os.path.exists(env_path) and DOTENV_AVAILABLE:
        load_dotenv(env_path)

    config = {}
    for key in REQUIRED_VARS:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(key)
        config[key] = value

    for key, default in DEFAULTS.items():
        config[key] = os.environ.get(key, default)

    return config


def load_env(env_path: str = None) -> Dict[str, str]:
    """Entry-point helper that fails loudly on missing required env vars.

    Wraps :func:`load_env_config` so that the surface every CLI
    ``main()`` calls surfaces a ``RuntimeError`` (with the missing
    key name in the message) instead of a bare ``KeyError``. The
    KeyError form is too quiet — operators see only the key string
    with no context, while the RuntimeError form makes it obvious
    that startup-time configuration is the problem.

    The return value is the same populated config dict, so callers
    can still use the result if they want.

    **With :data:`REQUIRED_VARS` empty this wrapper cannot currently
    raise**, and it is kept for the day a deployment-specific
    requirement is added rather than deleted — the two call sites import
    it by name, and the diagnostic it produces is worth more than the
    four lines it costs. Note that what it adds over
    :func:`load_env_config` is a *message*, not a *policy*: neither
    function decides what must be configured.
    """
    try:
        return load_env_config(env_path)
    except KeyError as exc:
        missing_key = exc.args[0] if exc.args else "UNKNOWN"
        raise RuntimeError(
            f"Missing required environment variable: {missing_key}. "
            f"Populate {ENV_FILE} (see .env.example) or export "
            f"the variable in your shell before starting this module."
        ) from exc