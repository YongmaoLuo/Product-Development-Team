"""MiniMax Code profile enumeration and the narrow provider → profile mapping.

Why this module exists
======================

The MiniMax Code CLI (``mcode``) does not know anything about PDT's
provider names. It picks an account through one environment variable,
``MCODE_PROFILE``, and isolates that account's state through two more:
``HOME`` points at the profile's own home directory, and
``MINIMAX_DATA_DIR`` points at ``<home>/.minimax``. Which accounts
exist is a fact only the ``mcode-profile`` tool knows — it owns the
profile directory and reports it as::

    {"current": "shared",
     "profiles": [{"name": ..., "home": ..., "login_state": ...}]}

PDT's provider names and the profile names are two naming systems. They
are kept apart (arch decision point 5): a mapping file translates one
into the other, exactly, and a miss falls back to the default profile
rather than blocking the run. This module owns the *narrow* half of
that: enumerate the profiles, resolve a provider name against the
mapping, and describe the environment delta a resolved profile needs.

The mapping file itself
------------------------

The mapping lives in a file, not in the code: ``<config dir>/
mcode_profile_map.yaml`` (see
:func:`config_paths.resolve_mcode_profile_map_file`), with a template
in ``example/``. It is read by the start-up, and only by a process
that actually runs the MiniMax Code backend — a Claude Code deployment
has no use for an account mapping and never opens the file.

The provider the start-up looks up
----------------------------------

The map is keyed by provider name, so the start-up has to know which
provider it is about to run before it can look one up. That is a
process-level fact, read from the environment:
:func:`resolve_startup_provider` names it — the provider the process
runs, or the first provider of the chain it tries them in. A process
with neither has no provider to look up, and runs on the default
profile.

Four rules this module holds to
-------------------------------

1. **Enumeration is loud.** A missing ``mcode-profile`` binary, a tool
   that exits non-zero, or output that is not a profile list all raise
   :class:`HarnessConfigError`. Silently returning an empty profile
   list would turn a broken installation into "the account you asked
   for does not exist" — a far harder failure to diagnose.
2. **Matching is exact.** The mapping is a dictionary keyed by provider
   name; a lookup misses when the case differs or when the string
   carries surrounding whitespace. No normalisation, no fuzzy matching:
   a profile that runs the wrong account burns the wrong quota.
3. **A miss is not an error.** An unmapped provider, an empty mapping,
   or a mapping value that names no known profile yields ``None`` — the
   caller then runs on the default profile. Only an unusable
   *installation* raises.
4. **A broken map file is an error, a missing one is not.** A file that
   cannot be parsed, or that has no ``profiles`` key, is a
   configuration mistake and it stops the start-up: read as "no
   mapping", it would run every provider on the default account while
   the operator believed a specific account was chosen. A file that is
   not there at all means exactly that — no mapping — and the process
   starts on the default profile.

The type this module hands out
------------------------------

:meth:`list_profiles` returns :class:`ProfileBinding`, a subclass of the
backend-agnostic ``harness.contract.ProfileBinding``. The base class
carries the generic facts — which profile, which harness, which model,
which environment — and the subclass adds the two fields the MiniMax
launcher selects on: the profile ``name`` (``MCODE_PROFILE``) and its
``home`` directory (``HOME`` and ``MINIMAX_DATA_DIR``). The base class
is imported, not redefined, so a binding stays usable anywhere the
contract type is expected.

The external process is stubbed at the function boundary (test decision
point 2): :func:`list_profiles` takes the command runner as an argument,
so a test can answer with a recorded payload without spawning a
process and without mocking ``Popen``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Union

from config_paths import (
    MCODE_PROFILE_MAP_FILE_ENV,
    PROJECT_ROOT,
    resolve_mcode_profile_map_file,
)
from harness.contract import HarnessConfigError
from harness.contract import ProfileBinding as ContractProfileBinding
from harness.registry import resolve_harness_name

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROFILE_CMD",
    "MCODE_HARNESS",
    "PROVIDER_NAME_ENV_VAR",
    "PROVIDER_PRIORITY_ENV_VAR",
    "CommandRunner",
    "HarnessConfigError",
    "ProfileBinding",
    "list_profiles",
    "load_mapping",
    "load_startup_mapping",
    "map_file_for",
    "parse_profiles",
    "profile_env",
    "resolve_profile",
    "resolve_startup_provider",
    "validate_mapping",
]

#: The tool that owns the MiniMax Code profiles.
DEFAULT_PROFILE_CMD = "mcode-profile"

#: The harness name a MiniMax Code profile is bound to.
MCODE_HARNESS = "mcode"

#: The environment variable that names the provider a run is charged to.
PROVIDER_NAME_ENV_VAR = "PDT_PROVIDER_NAME"

#: The environment variable that lists the providers, in the order they
#: are tried.
PROVIDER_PRIORITY_ENV_VAR = "PDT_PROVIDER_PRIORITY"

#: The YAML tag PyYAML gives a plain string scalar.
_YAML_STR_TAG = "tag:yaml.org,2002:str"

#: How long ``mcode-profile list --json`` may take before it is a
#: configuration problem rather than a slow machine.
LIST_TIMEOUT_SECONDS = 30

#: A command runner: it is handed the argv, it returns the command's
#: standard output, and it raises :class:`HarnessConfigError` when the
#: command could not be run or failed.
CommandRunner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class ProfileBinding(ContractProfileBinding):
    """One MiniMax Code profile, described as a harness profile binding.

    The base class — ``harness.contract.ProfileBinding`` — carries the
    facts every harness needs: the profile name, the harness it is
    bound to, the model, the settings file and the environment. This
    subclass adds what the MiniMax launcher selects on:

    Attributes:
        name: The profile name as ``mcode-profile`` reports it, and as
            ``MCODE_PROFILE`` is set to. The same string as the base
            class's ``profile``; both are kept in step.
        home: The profile's home directory. It becomes ``HOME`` and,
            through it, ``MINIMAX_DATA_DIR``.
        login_state: The profile's login state as reported by the
            tool (``"<region>/<status>"``, or ``"none"``). Carried for
            diagnostics; nothing in the launch path depends on it.
    """

    profile: str = ""
    harness: str = MCODE_HARNESS
    name: str = ""
    home: str = ""
    login_state: str = ""

    def __post_init__(self) -> None:
        # ``name`` is what the launcher selects on and ``profile`` is
        # what the contract calls it. They are the same string; keeping
        # one source of truth means a caller can read either field and
        # get the same answer.
        if not self.profile and self.name:
            object.__setattr__(self, "profile", self.name)
        elif not self.name and self.profile:
            object.__setattr__(self, "name", self.profile)


def _run_profile_command(argv: Sequence[str]) -> str:
    """Run ``argv`` and return its standard output.

    Every way this can fail — the tool is not installed, it timed out,
    it could not be executed, it exited non-zero — is reported as a
    :class:`HarnessConfigError`, because from the caller's side they
    are the same fact: this machine cannot enumerate MiniMax Code
    profiles.

    Args:
        argv: The command to run, argv[0] included.

    Returns:
        The command's standard output.

    Raises:
        HarnessConfigError: The tool is missing, timed out, could not
            be executed, or exited non-zero.
    """
    program = argv[0]

    # ``which`` first: a missing tool is the common case, and checking
    # it here means the caller gets a configuration error instead of an
    # operating-system error from deep inside the subprocess machinery.
    if shutil.which(program) is None:
        raise HarnessConfigError(
            f"mcode profile tool {program!r} is not installed or not on PATH"
        )

    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - race with which()
        raise HarnessConfigError(
            f"mcode profile tool {program!r} could not be executed: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessConfigError(
            f"mcode profile tool {program!r} timed out after "
            f"{LIST_TIMEOUT_SECONDS}s"
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise HarnessConfigError(
            f"mcode profile tool {program!r} exited with "
            f"{completed.returncode}: {detail}"
        )

    return completed.stdout


def parse_profiles(payload: str) -> List[ProfileBinding]:
    """Parse the output of ``mcode-profile list --json``.

    Args:
        payload: The tool's standard output.

    Returns:
        One binding per profile, in the order the tool reported them.

    Raises:
        HarnessConfigError: The payload is not JSON, is not a profile
            list, or holds an entry without a name or a home directory.
            A profile whose home is unknown cannot be launched, so it is
            reported rather than carried along half-formed.
    """
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise HarnessConfigError(
            f"mcode profile list output is not JSON: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise HarnessConfigError(
            "mcode profile list output is not a JSON object"
        )

    entries = document.get("profiles")
    if not isinstance(entries, list):
        raise HarnessConfigError(
            "mcode profile list output has no 'profiles' list"
        )

    bindings: List[ProfileBinding] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HarnessConfigError(
                "mcode profile list output holds a profile that is not an object"
            )

        name = entry.get("name")
        home = entry.get("home")
        if not isinstance(name, str) or not name:
            raise HarnessConfigError(
                "mcode profile list output holds a profile without a name"
            )
        if not isinstance(home, str) or not home:
            raise HarnessConfigError(
                f"mcode profile {name!r} has no home directory"
            )

        login_state = entry.get("login_state")
        bindings.append(
            ProfileBinding(
                name=name,
                home=home,
                login_state=login_state if isinstance(login_state, str) else "",
            )
        )

    return bindings


def list_profiles(
    cmd: str = DEFAULT_PROFILE_CMD,
    runner: CommandRunner = _run_profile_command,
) -> List[ProfileBinding]:
    """Enumerate the MiniMax Code profiles the tool knows about.

    Args:
        cmd: The profile tool to run. Injectable so a caller can point
            at a different installation, and so a test can answer with
            a recorded payload instead of running the real tool.
        runner: How the command is run. The default runs it for real and
            reports every failure as a configuration error; a test
            injects its own.

    Returns:
        One binding per profile, in the order the tool reported them.

    Raises:
        HarnessConfigError: The tool is missing, failed, timed out, or
            printed something that is not a profile list.
    """
    payload = runner([cmd, "list", "--json"])
    return parse_profiles(payload)


def load_mapping(path: Union[str, Path]) -> Dict[str, str]:
    """Read the provider → profile map from a YAML file.

    The file is the one place where PDT's provider names are turned into
    MiniMax Code account names (arch decision point 5). Its shape is one
    key — ``profiles``, a mapping of provider name to profile name::

        profiles:
          shared-account: acc2

    Two outcomes are deliberately different from each other:

    * **A missing file is not an error.** An operator who never wrote
      one has no mapping; the loader answers with an empty mapping and
      the process runs on the default profile.
    * **A broken file is an error.** A file that cannot be parsed, or
      whose root has no ``profiles`` key, is a configuration mistake.
      Read as "no mapping" it would run every provider on the default
      account while the operator believed a specific account had been
      chosen — so the start-up stops instead. The message names the
      file and the line, because a configuration error nobody can locate
      is not an error anybody can fix.

    A provider listed twice is decided rather than arbitrated: the last
    value wins, and a warning names the repeated key. Silently taking
    one of two values is how a map ends up routing an account nobody
    chose.

    Args:
        path: The file to read.

    Returns:
        The mapping, provider name to profile name. Empty when the file
        does not exist.

    Raises:
        HarnessConfigError: The file exists but cannot be used — it is
            not valid YAML, its root is not a mapping, it has no
            ``profiles`` key, or one of its entries is not a name.
    """
    resolved = Path(path)

    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No map configured. This is the ordinary case for a deployment
        # that runs every provider on the default account.
        return {}
    except OSError as exc:
        raise HarnessConfigError(
            f"mcode profile map file {resolved} is unreadable: {exc}"
        ) from exc

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML ships with the backend
        raise HarnessConfigError(
            f"PyYAML is unavailable; cannot read the mcode profile map file "
            f"{resolved}"
        ) from exc

    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        # ``yaml.YAMLError`` is not a ``ValueError``; catch it explicitly
        # or a malformed file escapes as an unrelated exception type.
        mark = getattr(exc, "problem_mark", None)
        if mark is None:
            where = str(resolved)
            detail = str(exc)
        else:
            where = f"{resolved}:{mark.line + 1}:{mark.column + 1}"
            detail = getattr(exc, "problem", None) or str(exc)
        raise HarnessConfigError(
            f"mcode profile map file {where} is not valid YAML: {detail}"
        ) from exc

    if root is None:
        raise HarnessConfigError(
            f"mcode profile map file {resolved}:1 is empty; expected a "
            f"'profiles' mapping"
        )

    if not isinstance(root, yaml.MappingNode):
        raise HarnessConfigError(
            f"mcode profile map file {resolved}:{root.start_mark.line + 1} must "
            f"be a mapping with a 'profiles' key, got "
            f"{type(root).__name__}"
        )

    profiles_node = None
    for key_node, value_node in root.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == "profiles":
            profiles_node = value_node
            break

    if profiles_node is None:
        raise HarnessConfigError(
            f"mcode profile map file {resolved}:{root.start_mark.line + 1} has no "
            f"'profiles' key"
        )

    if not isinstance(profiles_node, yaml.MappingNode):
        raise HarnessConfigError(
            f"mcode profile map file {resolved}:{profiles_node.start_mark.line + 1} "
            f"'profiles' must be a mapping of provider name to profile name"
        )

    mapping: Dict[str, str] = {}
    for key_node, value_node in profiles_node.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != _YAML_STR_TAG:
            raise HarnessConfigError(
                f"mcode profile map file {resolved}:{key_node.start_mark.line + 1} "
                f"has a provider name that is not a string"
            )
        if (
            not isinstance(value_node, yaml.ScalarNode)
            or value_node.tag != _YAML_STR_TAG
        ):
            raise HarnessConfigError(
                f"mcode profile map file {resolved}:{value_node.start_mark.line + 1} "
                f"maps provider {key_node.value!r} to a profile name that is not "
                f"a string"
            )
        if key_node.value in mapping:
            # A repeated provider key is a mistake in the file, not a
            # question with two answers. The rule is fixed — the last
            # value wins — and the operator is told, because silently
            # picking one of two values is how a map ends up routing an
            # account nobody chose.
            log.warning(
                "mcode profile map %s lists provider %r more than once; "
                "the last value wins",
                resolved,
                key_node.value,
            )
        mapping[key_node.value] = value_node.value

    return mapping


def validate_mapping(
    mapping: Mapping[str, str],
    profiles: Iterable[ProfileBinding],
) -> None:
    """Check that every mapped profile name is one the tool knows.

    A map that names a profile the tool does not have has a dangerous
    failure mode: the provider would run on the *default* account
    instead of the one the operator named, quietly spending the wrong
    quota. The check is therefore loud — the start-up stops, and the
    message names the profile that is missing.

    Args:
        mapping: The provider → profile mapping to check.
        profiles: The known profiles to check it against. A binding
            carries the profile name; a bare name is accepted too, so a
            caller can validate against a plain list of names.

    Returns:
        ``None`` — the mapping is usable.

    Raises:
        HarnessConfigError: The mapping is not a mapping at all, or it
            names a profile that is not among ``profiles``.
    """
    if not isinstance(mapping, Mapping):
        raise HarnessConfigError(
            f"mcode profile mapping must be a mapping, got {type(mapping).__name__}"
        )

    known: Set[str] = set()
    for binding in profiles:
        known.add(getattr(binding, "name", None) or str(binding))

    missing = sorted({name for name in mapping.values() if name not in known})
    if missing:
        raise HarnessConfigError(
            f"mcode profile map names unknown profile(s): {', '.join(missing)}; "
            f"known profiles: {', '.join(sorted(known)) or '(none)'}"
        )


def resolve_profile(
    mapping: Mapping[str, str],
    provider: str,
    profiles: Iterable[ProfileBinding],
) -> Optional[ProfileBinding]:
    """Resolve a PDT provider name to a MiniMax Code profile.

    The mapping is keyed by provider name and valued by profile name,
    and both sides are matched exactly: a provider name that differs in
    case or carries surrounding whitespace does not match, and neither
    does a mapped profile name that no known profile carries.

    Args:
        mapping: Provider name to profile name.
        provider: The provider name to resolve.
        profiles: The known profiles to resolve against.

    Returns:
        The matching binding, or ``None`` when the provider is not
        mapped, the mapping is empty, or the mapped profile does not
        exist. ``None`` means "run on the default profile"; it never
        means "stop".

    Raises:
        HarnessConfigError: The mapping is not a mapping at all — a
            malformed configuration file, not a miss.
    """
    if not isinstance(mapping, Mapping):
        raise HarnessConfigError(
            f"mcode profile mapping must be a mapping, got {type(mapping).__name__}"
        )

    if not mapping:
        return None

    target = mapping.get(provider)
    if not target:
        return None

    if not profiles:
        return None

    for binding in profiles:
        if binding.name == target:
            return binding

    return None


def resolve_startup_provider(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The provider name the process starts with.

    The map is keyed by provider name, so the start-up has to know which
    provider it is about to run before it can look one up. Two
    environment variables answer that, and the order between them is
    fixed:

    1. :data:`PROVIDER_NAME_ENV_VAR` — the provider this process runs.
    2. :data:`PROVIDER_PRIORITY_ENV_VAR` — the chain of providers, of
       which the first entry is the provider the process starts with.

    Neither is set means the process has no provider to look up. That is
    a miss, not an error: the default profile applies and nothing is
    injected — the same shape as a provider the map does not name.

    Args:
        env: The environment to read. ``None`` reads the process
            environment.

    Returns:
        The provider name, or ``None`` when the process has none.
    """
    environment = os.environ if env is None else env

    name = (environment.get(PROVIDER_NAME_ENV_VAR) or "").strip()
    if name:
        return name

    chain = environment.get(PROVIDER_PRIORITY_ENV_VAR) or ""
    for candidate in chain.split(","):
        candidate = candidate.strip()
        if candidate:
            return candidate

    return None


def profile_env(binding: Optional[ProfileBinding]) -> Dict[str, str]:
    """Describe the environment a resolved profile needs.

    The MiniMax launcher reads exactly three variables: ``MCODE_PROFILE``
    selects the account, ``HOME`` isolates the account's files, and
    ``MINIMAX_DATA_DIR`` isolates the account's data directory inside
    that home.

    Args:
        binding: The resolved profile, or ``None`` when nothing was
            mapped.

    Returns:
        The environment delta, or an empty mapping when there is no
        binding — the default profile then applies and nothing is
        injected.
    """
    if binding is None:
        return {}

    home = binding.home
    return {
        "MCODE_PROFILE": binding.name,
        "HOME": home,
        "MINIMAX_DATA_DIR": str(Path(home) / ".minimax"),
    }


def map_file_for(env: Optional[Mapping[str, str]] = None) -> Path:
    """The map file this environment points at.

    The precedence is the one
    :func:`config_paths.resolve_mcode_profile_map_file` documents — the
    environment variable first, the configuration directory second —
    but the override is read from **the environment it is given**, not
    from the process environment. A start-up that answers from an
    environment of its own has to answer about that environment: the
    whole point of passing the environment around is that the decision
    can be reproduced from it.

    Args:
        env: The environment to read. ``None`` reads the process
            environment.

    Returns:
        The path of the map file. The file need not exist — a missing
        map means no mapping, not an error.
    """
    environment = os.environ if env is None else env

    override = environment.get(MCODE_PROFILE_MAP_FILE_ENV)
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate

    # No override: the machine's own map file applies.
    return resolve_mcode_profile_map_file()


def load_startup_mapping(
    profiles: Optional[Iterable[ProfileBinding]] = None,
    map_file: Optional[Path] = None,
    runner: CommandRunner = _run_profile_command,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Load and validate the profile map the way a start-up does.

    This is the start-up half of the mapping. It decides **whether** the
    map is read at all, reads it, and refuses to hand back a map that
    names an account this machine does not have.

    The decision belongs to the backend: the map exists to translate a
    PDT provider name into a MiniMax Code account, so a process running
    the Claude Code backend has no use for it and must not open it. A
    Claude Code deployment therefore starts cleanly even when the map
    file is broken — the file is not merely ignored, it is never read,
    so it cannot fail the start-up either.

    Args:
        profiles: The known profiles to validate against. ``None``
            enumerates them with the profile tool, so the map is
            checked against the accounts this machine actually has.
        map_file: The map to read. ``None`` resolves it through
            :func:`map_file_for`, which honours
            ``PDT_MCODE_PROFILE_MAP_FILE``.
        runner: How the profile tool is run.
        env: The environment the start-up is running in. ``None`` reads
            the process environment. The backend and the map file are
            both decided from it, so a caller can reproduce the
            decision from an environment of its own.

    Returns:
        The validated mapping. Empty when the process does not run the
        MiniMax Code backend, when there is no map file, and when the
        map itself is empty.

    Raises:
        HarnessConfigError: The map file is broken, or it names a
            profile the tool does not know.
    """
    environment = os.environ if env is None else env

    if resolve_harness_name(environment) != MCODE_HARNESS:
        # Not our backend: the map is not read at all.
        return {}

    resolved = Path(map_file) if map_file is not None else map_file_for(environment)
    mapping = load_mapping(resolved)
    if not mapping:
        # Nothing to validate — and nothing that needs the profile tool.
        return mapping

    known = list_profiles(runner=runner) if profiles is None else list(profiles)
    validate_mapping(mapping, known)
    return mapping