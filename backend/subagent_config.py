"""
Subagent Configuration
======================

Unified configuration dataclass for subagent (decisions 2/3/4) and
ClaudeCodingTool construction.

Replaces the 7+ scattered kwargs that ``agent.py:730`` previously passed
into ``ClaudeCodingTool(provider=..., model=..., api_key=..., base_url=...,
settings_file_path=..., hook_scripts=...)``, so the same dataclass instance
can be reused across tests (mocked) and production runs, and so provider-
isolated config can be passed through a single typed boundary.

Provider configuration is resolved through the
``cc_switch`` consumer layer, keyed on the provider name
CC Switch uses. The legacy ``cc_switch`` module is NOT imported by
this file; the consumer layer is the single source of truth for runtime
provider config.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from utils.secret_files import private_dir, write_private_json


# 2026-09-13: the tiered model_map → _MODEL_ROLES_TO_ENV mapping is
# REMOVED. Model management is delegated to CC Switch — the dispatch
# walk forwards the selected provider row's own ANTHROPIC_* model env,
# and SubagentConfig optionally accepts a flat ``model_env`` dict for
# the same purpose.

# Minimum ANTHROPIC_* env fields the settings tmpfile must carry: the
# endpoint and the credentials. ``hooks`` is a separate top-level key
# (decision 4) and is not part of this count. ``ANTHROPIC_MODEL`` is
# *not* required — see ``to_settings_dict``: it is stamped only when a
# model actually resolves, and left unset otherwise so the Claude CLI
# falls back to the local config rather than to a model this project
# picked.
_SETTINGS_ENV_FIELD_COUNT = 3


@dataclass
class SubagentConfig:
    """Subagent config — provider endpoints, credentials, model map, hooks.

    Attributes:
        provider_name:    The provider's cc-switch display name, kebab-cased
                          (e.g. ``vendor-a-pro``).
        base_url:         The provider's own endpoint, never the
                          parent's proxy endpoint.
        api_key:          ANTHROPIC_API_KEY.
        auth_token:       ANTHROPIC_AUTH_TOKEN.
        model_env:        Optional flat dict of extra ANTHROPIC_* model
                          env vars forwarded verbatim (2026-09-13: model
                          management is delegated to CC Switch).
        settings_file_path: --settings flag tmpfile (written by
                          ``write_tmp_settings`` after construction).
        hook_scripts:     List of post_tool_use hook script paths
                          (decision 4 injection target).
        provider_priority: Fallback order across providers. Defaults to
                          ``['vendor-b-pro', 'vendor-a-pro']`` — every entry
                          is kebab-case so the static-scan gate stays green.
        task_type:        Decision-4 tag used to select which post_tool_use
                          hook to inject. Defaults to 'general'.
        task_summary:     Free-form task description appended to the
                          post_tool_use hook (decision 4). Defaults to ''.
        inherit_env:      When True, the settings payload carries **no**
                          ``ANTHROPIC_*`` entries at all — the subprocess
                          is left to resolve provider configuration from
                          Claude Code's own settings (and the ambient
                          env). Used when CC Switch is not installed: the
                          user's existing Claude Code configuration is
                          already the answer, and anything written here
                          would outrank it. Hooks are unaffected.
    """

    provider_name: str = ''
    base_url: str = ''
    api_key: str = ''
    auth_token: str = ''
    model_env: Dict[str, str] = field(default_factory=dict)
    settings_file_path: Optional[Path] = None
    hook_scripts: List[Path] = field(default_factory=list)
    provider_priority: List[str] = field(
        default_factory=lambda: ['vendor-b-pro', 'vendor-a-pro']
    )
    task_type: str = 'general'
    task_summary: str = ''
    inherit_env: bool = False

    def __post_init__(self) -> None:
        # Defensive: callers may pass None for model_env; treat as an
        # empty dict so downstream code can safely iterate it.
        if self.model_env is None:
            self.model_env = {}

    # ------------------------------------------------------------------
    # Serialization (decisions 2 + 3)
    # ------------------------------------------------------------------

    def to_settings_dict(self) -> dict:
        """Serialize to the Anthropic SDK ``settings.json`` schema.

        Returns a dict with two top-level keys:

          * ``env`` — 7 ``ANTHROPIC_*`` entries the SDK reads to wire up
            base URL, credentials, and the 4 default model tiers
            (opus / sonnet / haiku / medium). The count is enforced
            by ``_SETTINGS_ENV_FIELD_COUNT``. When
            ``settings_file_path`` is set (i.e. ``write_tmp_settings``
            has been called), ``CLAUDE_SETTINGS_PATH`` is also
            included so the Claude Code CLI forwards the path to hook
            subprocesses — pre_tool_use.sh and post_tool_use.sh read
            it off stdin / env to identify the originating settings
            file.
          * ``hooks`` — Claude Code hook configuration. When
            ``hook_scripts`` is non-empty, the path of every script
            is matched by filename (pre_tool_use vs post_tool_use) and
            wired into the corresponding PreToolUse / PostToolUse hook
            list. ``task_type`` and ``task_summary`` are passed through
            as a ``stdin_extra`` payload so the hook scripts can read
            them when the SDK forwards stdin. When ``hook_scripts`` is
            empty (the default), the hooks dict is empty ``{}``.

        Boundary: the ANTHROPIC_* env field minimum (endpoint +
        credentials) is enforced with an AssertionError so a silent
        drift from the contract surfaces immediately — **except** in
        ``inherit_env`` mode, where emitting no ANTHROPIC_* entries is
        the point rather than a drift.
        """
        if self.inherit_env:
            return self._inherit_env_payload()

        env: Dict[str, str] = {}
        env['ANTHROPIC_BASE_URL'] = self.base_url
        env['ANTHROPIC_AUTH_TOKEN'] = self.auth_token
        env['ANTHROPIC_API_KEY'] = self.api_key

        # 2026-09-13: the tiered model_map block is REMOVED. Model
        # management is delegated to CC Switch — the dispatch walk
        # (coding_tool) forwards the selected provider row's own
        # ANTHROPIC_* model env verbatim. Passing ``model_env``
        # (optional) forwards extra ANTHROPIC_* model vars from the
        # provider row when the caller has them at hand.
        for key, value in (self.model_env or {}).items():
            if key.startswith('ANTHROPIC_') and key not in env:
                env[key] = value

        # Model resolution. ``ANTHROPIC_MODEL`` is stamped only when a model
        # actually resolves — it deliberately has no last-resort default.
        #
        # Resolution order: ``model_env`` (caller-supplied, most specific) >
        # the provider row's own model (from the CC Switch DB).
        #
        # When neither resolves, the field is left unset and the Claude CLI
        # resolves the model from the local config — the same path it takes
        # for every other caller, and the only place the choice belongs.
        # Naming a fallback model here would compile one deployment's model
        # choice into every install, which is the same reasoning that keeps
        # provider names out of ``example/``.
        if 'ANTHROPIC_MODEL' not in env:
            _model = (self.model_env or {}).get('ANTHROPIC_MODEL', '')
            if not _model and self.provider_name:
                try:
                    from cc_switch import get_provider
                    _cfg = get_provider(self.provider_name)
                    # ``ProviderConfig.model`` is the row's declared default
                    # tier, falling back to its opus tier — the same two
                    # keys this block used to read out of the raw env dict.
                    _model = (_cfg.model or '') if _cfg else ''
                except Exception:
                    pass                      # DB unavailable → leave unset
            if _model:
                env['ANTHROPIC_MODEL'] = _model

        # Defence-in-depth: the endpoint and credential fields are a hard
        # contract. Refuse to silently drift away from it.
        anthropic_keys = [k for k in env if k.startswith('ANTHROPIC_')]
        if len(anthropic_keys) < _SETTINGS_ENV_FIELD_COUNT:
            raise AssertionError(
                f"SubagentConfig.to_settings_dict produced "
                f"{len(anthropic_keys)} ANTHROPIC_* env keys, "
                f"expected at least {_SETTINGS_ENV_FIELD_COUNT}"
            )

        # Decision-4 hook plumbing: pre_tool_use.sh and post_tool_use.sh
        # read $CLAUDE_SETTINGS_PATH to identify the originating
        # settings file (for cross-process correlation in execution.log).
        # The SDK forwards env entries from settings.json to the hook
        # subprocess, so we publish the path here instead of having
        # callers wrangle os.environ before each spawn.
        if self.settings_file_path is not None:
            env['CLAUDE_SETTINGS_PATH'] = str(self.settings_file_path)

        return {
            'env': env,
            'hooks': self._build_hooks_payload(),
        }

    def _inherit_env_payload(self) -> dict:
        """Settings payload for inherit mode: hooks, and no provider config.

        Inherit mode is what runs when CC Switch is not installed. The
        user already has a working Claude Code configuration — that is
        the whole premise of owning Claude Code — so the job here is to
        **not interfere** with it.

        That is why the env block is empty rather than merely reduced.
        ``--settings`` outranks ``~/.claude/settings.json``, so *any*
        ``ANTHROPIC_*`` entry written here would override the user's own
        endpoint, credentials or model choice — turning "reuse your
        existing configuration" into "silently replace it". A single
        stray ``ANTHROPIC_MODEL`` is enough to do it, which is why this
        returns early rather than filtering a populated ``env``.

        Hooks are still emitted, and that is deliberate. They are this
        project's own instrumentation — the per-subagent activity log the
        watchdog reads to tell "stuck inside an LLM call" from "a long
        pytest run" — not provider configuration. Dropping them to make
        "we inject nothing" literally true would trade a working
        capability for a slogan.

        ``CLAUDE_SETTINGS_PATH`` is included for the same reason it is in
        the normal path: the hook scripts read it to identify the
        originating settings file. It is not an ``ANTHROPIC_*`` key and
        carries no provider configuration.
        """
        env: Dict[str, str] = {}
        if self.settings_file_path is not None:
            env['CLAUDE_SETTINGS_PATH'] = str(self.settings_file_path)
        return {
            'env': env,
            'hooks': self._build_hooks_payload(),
        }

    def _build_hooks_payload(self) -> dict:
        """Map ``hook_scripts`` to the Claude Code settings.json hooks schema.

        Scripts whose filename contains ``pre`` go into ``PreToolUse``;
        scripts whose filename contains ``post`` go into ``PostToolUse``.
        ``task_type`` and ``task_summary`` are embedded as
        ``stdin_extra`` so the hook can read them off its stdin payload
        (decision 4 pass-through contract).

        Returns:
            dict suitable for the ``hooks`` key of a Claude Code
            ``settings.json``. Empty ``{}`` when ``hook_scripts`` is
            empty.
        """
        hooks: Dict[str, list] = {}
        pre_cmds = [str(p) for p in self.hook_scripts if 'pre' in p.name]
        post_cmds = [str(p) for p in self.hook_scripts if 'post' in p.name]

        if pre_cmds:
            hooks['PreToolUse'] = [
                {
                    'matcher': '*',
                    'hooks': [{'type': 'command', 'command': cmd}],
                }
                for cmd in pre_cmds
            ]
        if post_cmds:
            post_entries = []
            for cmd in post_cmds:
                entry = {
                    'type': 'command',
                    'command': cmd,
                }
                # decision 4: pass task_type / task_summary through to
                # the post_tool_use hook. post_tool_use.sh reads its
                # payload from stdin; the SDK forwards this field on
                # the JSON stdin.
                if self.task_type or self.task_summary:
                    entry['stdin_extra'] = {
                        'task_type': self.task_type,
                        'task_summary': self.task_summary,
                    }
                post_entries.append(entry)
            hooks['PostToolUse'] = [
                {'matcher': '*', 'hooks': post_entries}
            ]
        return hooks

    def write_tmp_settings(self, logger: Optional[object] = None) -> Path:
        """Write a ``<private tmpdir>/subagent_settings_<uuid>.json`` file.

        The tmpfile is the payload passed to ClaudeCodingTool's
        ``--settings`` flag. Format: ``to_settings_dict()`` serialized
        to JSON. The file is written atomically (write to
        ``<path>.tmp`` then ``os.replace``) so a concurrent reader
        never sees a half-written JSON, and it is written into a fresh
        ``0700`` directory with ``0600`` on the file — the payload
        carries the routed provider's credentials.

        Side effects:
          * ``self.settings_file_path`` is updated to the new path
            so callers can pass ``cfg.settings_file_path`` to
            ``--settings``.
          * If ``logger`` is provided, ``logger.emit`` is called with
            ``event='subagent_settings_file_written'`` and
            ``data={'path': str, 'uuid': str, 'fields': 7}``. The
            7-field count is a hard contract the test pins and the
            log entry is the only trail a debug session has for
            correlating a settings-file path back to a tmpfile uuid.

        The tmpfile itself is NOT deleted at process exit — a child
        process may read it asynchronously after this function returns
        (``pre_tool_use.sh`` reads ``$CLAUDE_SETTINGS_PATH`` on every
        tool call), and post-mortem debugging wants the hook layout to
        still be readable. The *credentials* in it are another matter:
        the dispatch path redacts them with
        :func:`utils.secret_files.redact` once the child is reaped, so
        what survives on disk is the structure without the key. A crash
        before that point leaves them in place, which is why the file is
        written ``0600`` inside a ``0700`` directory rather than relying
        on the cleanup happening.

        Returns:
            The ``Path`` of the written tmpfile.

        Raises:
            OSError: if the temp root is unwritable (propagated from open()).
        """
        file_uuid = uuid.uuid4().hex
        # A private 0700 directory under the system temp root, with a
        # 0600 file inside it. Both layers matter: the payload carries
        # the routed provider's ``ANTHROPIC_API_KEY`` /
        # ``ANTHROPIC_AUTH_TOKEN``, and the previous flat
        # ``/tmp/subagent_settings_<uuid>.json`` was created with the
        # default mode — world-readable on a ``1777`` /tmp. See
        # :mod:`utils.secret_files` for the measurement and rationale.
        path = private_dir() / f"subagent_settings_{file_uuid}.json"

        # Set settings_file_path BEFORE serializing so the env block
        # includes CLAUDE_SETTINGS_PATH (decision-4 hook plumbing:
        # pre_tool_use.sh / post_tool_use.sh read this env var to
        # correlate their output with the originating settings file).
        self.settings_file_path = path

        payload = self.to_settings_dict()
        # Boundary (task 7-4-3): when hook_scripts is empty, the
        # in-memory ``hooks`` block is an empty ``{}``. Persist
        # that empty block on disk and the SDK can misinterpret
        # it as "matcher configured with no commands" (a schema
        # hazard on some SDK versions). Strip the key before
        # writing so the on-disk JSON does not carry it at all
        # — the in-memory contract remains the lenient "either
        # form" form (7-4-1 / 7-4-3 in-memory tests still pass)
        # but the on-disk file is forward-compatible.
        if not payload.get('hooks'):
            payload.pop('hooks', None)
        write_private_json(path, payload)

        if logger is not None:
            data = {
                'path': str(path),
                'uuid': file_uuid,
                'fields': _SETTINGS_ENV_FIELD_COUNT,
            }
            if hasattr(logger, 'emit'):
                logger.emit(
                    event='subagent_settings_file_written',
                    data=data,
                )
            elif hasattr(logger, 'log'):
                logger.log(
                    level='INFO',
                    event='subagent_settings_file_written',
                    message=f'subagent settings file written: {path}',
                    data=data,
                )
            elif hasattr(logger, 'info'):
                logger.info(
                    'subagent_settings_file_written',
                    f'subagent settings file written: {path}',
                    **{'data': data},
                )

        return path
