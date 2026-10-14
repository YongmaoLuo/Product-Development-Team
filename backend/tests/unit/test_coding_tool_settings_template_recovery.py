"""A ``--settings`` template that was swept must not become a 0.2s corpse.

The failure this file pins (observed once)
------------------------------------------
``SubagentConfig.write_tmp_settings`` writes the template into a fresh
``private_dir()`` — ``~/.pdt-scratch/pdt-subagent-<rand>/``. Anything that
sweeps that root (a boot-time cleaner, another run's TTL, an operator's
``rm``) can take the template away while the run is still holding its
path in ``self.settings``.

``_run_claude_interactive`` used to notice only ``.exists() is False``,
leave ``settings_data`` at ``None``, and then forward the dangling path
verbatim::

    claude ... --settings /Users/…/pdt-subagent-vw_xtnmf/subagent_settings_<uuid>.json

The Claude CLI validates ``--settings`` *before* any model call, so the
child died in 0.2s with ``Error: Settings file not found`` on stderr and
no assistant text. The read loop could only render that as
``Claude sub-process exited without assistant text`` — a message that
points at the provider and the model when the fault is a swept local
file. One task was marked failed and four downstream tasks stalled.

The contract now enforced
-------------------------
1. Readable  → unchanged behaviour, and the pristine payload is cached
   on the instance so a later sweep is survivable.
2. Missing, cache present → the dispatch self-heals from the cache and
   writes its own fresh settings file. Guardrails intact: the healed
   payload still carries the template's hooks.
3. Missing, no cache → raise :class:`SettingsTemplateMissing` naming the
   path, before any subprocess is spawned. NOT "launch without
   ``--settings``": that would run a sub-agent with no hooks and no lock
   broker while looking perfectly healthy, which is the outcome
   ``claude_sandbox.py`` already names as the worse one.

Why the cache can be instance-level and not global
--------------------------------------------------
``_run_interactive_resilient`` is a ``while True:`` loop that calls
``self._run_claude_interactive(...)`` again on the *same* instance after
an empty response, so a template that vanishes between two attempts of
one task is still recoverable from the instance's own cache.

No subprocess is ever spawned here: ``subprocess.Popen`` is patched, so
only the ``cmd`` handed to it and the on-disk settings file are
inspected. Everything private goes under ``tmp_path`` via
``PDT_SECRET_TEMP_ROOT`` — a test that wrote into the operator's real
``~/.pdt-scratch`` would both read unrelated production residue and
pollute it.
"""

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from coding_tool import ApiError, ClaudeCodingTool, SettingsTemplateMissing
from subagent_config import SubagentConfig


# ---------------------------------------------------------------------------
# Helpers — same harness shape as test_coding_tool_settings_write_order.py
# ---------------------------------------------------------------------------


_HOOK_MARKER = "/abs/pre_tool_use.sh"


def _make_mock_process() -> MagicMock:
    """A MagicMock that satisfies the ``_run_claude_interactive`` read loop."""
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({
            "type": "result", "result": "ok", "is_error": False,
        }) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    return mock_proc


def _write_template() -> Path:
    """Write a real SubagentConfig settings template; return its path.

    ``hook_scripts`` is what makes this worth deleting: a template with no
    hooks would make the "guardrails survived the heal" assertion vacuous,
    because there would be nothing to lose.
    """
    cfg = SubagentConfig(
        provider_name="vendor-a-pro",
        base_url="https://vendor-a.example.invalid/anthropic",
        api_key="sk-template-key",
        auth_token="sk-template-token",
        hook_scripts=[Path(_HOOK_MARKER)],
    )
    return cfg.write_tmp_settings()


def _build_tool(settings: str) -> ClaudeCodingTool:
    """A real ClaudeCodingTool whose provider walk resolves nothing.

    The non-empty sentinel priority keeps the dispatch from resolving a
    real CC Switch row (and leaking its live credentials into the
    assertions); ``[]`` is falsy and falls back to the constructor's
    default pair, which does resolve one.
    """
    return ClaudeCodingTool(
        api_key="sk-test-key",
        base_url="https://vendor-a.example.invalid/anthropic",
        auth_token="sk-test-token",
        provider_priority=["__no_such_provider__"],
        settings=settings,
    )


def _settings_arg(mock_popen: MagicMock) -> str:
    """The value the ``cmd`` handed to Popen puts after ``--settings``."""
    cmd = mock_popen.call_args[0][0]
    assert "--settings" in cmd, f"cmd missing '--settings': {cmd!r}"
    return cmd[cmd.index("--settings") + 1]


def _run_one_dispatch(tool: ClaudeCodingTool):
    """One mocked dispatch; returns the Popen mock for cmd inspection."""
    with patch("coding_tool.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_process()
        tool.query("test prompt")
    return mock_popen


@pytest.fixture(autouse=True)
def _private_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep every settings file inside ``tmp_path``.

    Without this the test would write into (and delete from) the real
    ``~/.pdt-scratch`` — the directory whose sweep caused the incident.
    """
    monkeypatch.setenv("PDT_SECRET_TEMP_ROOT", str(tmp_path / "scratch"))


@pytest.fixture(autouse=True)
def _no_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the settings file as *written*, not as redacted.

    Redaction (``utils.secret_files``) rewrites the credentials once the
    child is reaped; that contract has its own suite. Here the file's
    *content* is what is under test.
    """
    monkeypatch.setattr("coding_tool.redact_all", lambda *a, **k: 0)


# ---------------------------------------------------------------------------
# 1. Readable template — existing behaviour, plus the new cache
# ---------------------------------------------------------------------------


class TestReadableTemplate:
    def test_readable_template_still_dispatches(self) -> None:
        """The ordinary path is unchanged: a settings file is written and
        handed to the CLI, and it lives in a ``pdt-subagent-*`` dir."""
        template = _write_template()
        tool = _build_tool(str(template))

        mock_popen = _run_one_dispatch(tool)

        value = Path(_settings_arg(mock_popen))
        assert value.is_absolute() and value.is_file()
        assert value.name.startswith("subagent_settings_")
        assert value.parent.name.startswith("pdt-subagent-"), (
            f"effective settings file {value} is not in a private "
            f"pdt-subagent-* directory"
        )

    def test_readable_template_caches_a_pristine_payload(self) -> None:
        """The cache is taken BEFORE this module stamps its own keys.

        ``_run_claude_interactive`` adds ``PDT_SUBAGENT_UUID`` and the
        Edit|Write guard hook to the payload it dispatches. If those
        leaked into the cache, a later self-heal would re-apply a second
        guard hook on top of the first and carry a stale sub-agent uuid.
        The pristine payload is what ``SubagentConfig`` actually wrote.
        """
        template = _write_template()
        tool = _build_tool(str(template))
        assert tool._settings_template_cache is None, (
            "a fresh instance must start with no cached template"
        )

        _run_one_dispatch(tool)

        cache = tool._settings_template_cache
        assert cache is not None, (
            "a readable template must be cached so a later sweep heals"
        )
        assert "PDT_SUBAGENT_UUID" not in cache.get("env", {}), (
            "the cache captured a mutated payload; it must be the "
            "template as read, before PDT_* stamping"
        )
        assert _HOOK_MARKER in json.dumps(cache), (
            "the cache lost the template's hook scripts"
        )


# ---------------------------------------------------------------------------
# 2. Swept template, cache present — the self-heal
# ---------------------------------------------------------------------------


class TestSweptTemplateHealsFromCache:
    """The reproduction of the incident, minus the corpse.

    Dispatch once (template readable, cache filled), delete the whole
    ``pdt-subagent-*`` directory the template lived in, then dispatch
    again on the SAME instance — which is exactly what the
    ``while True:`` retry loop in ``_run_interactive_resilient`` does.
    """

    def test_second_dispatch_writes_a_new_private_dir_file(self) -> None:
        """The healed dispatch writes its own fresh settings file.

        This is the load-bearing assertion of the fix: the path handed to
        the CLI is a file that *exists*, in a *new* ``pdt-subagent-*``
        directory (``private_dir()`` mkdtemps per call), and is neither
        the swept template nor the first dispatch's file.
        """
        template = _write_template()
        tool = _build_tool(str(template))
        first = Path(_settings_arg(_run_one_dispatch(tool)))

        # The sweep: the whole private directory goes, not just the file.
        shutil.rmtree(template.parent)
        assert not template.parent.exists()

        second = Path(_settings_arg(_run_one_dispatch(tool)))

        assert second.is_file(), (
            f"--settings points at {second}, which does not exist — that "
            f"is the 0.2s 'Settings file not found' corpse"
        )
        assert second.parent.name.startswith("pdt-subagent-"), (
            f"healed settings file {second} is not in a pdt-subagent-* "
            f"directory"
        )
        assert second.parent != first.parent, (
            "the healed dispatch must write into a NEW private dir; "
            f"reused {first.parent}"
        )
        assert second != template, (
            "the swept template path was forwarded to the CLI verbatim"
        )
        assert second != first

    def test_the_dangling_path_never_reaches_the_cli(self) -> None:
        """The swept path is absent from the whole argv, not just the flag."""
        template = _write_template()
        tool = _build_tool(str(template))
        _run_one_dispatch(tool)
        shutil.rmtree(template.parent)

        mock_popen = _run_one_dispatch(tool)

        cmd = mock_popen.call_args[0][0]
        assert str(template) not in cmd, (
            f"the swept template path {template} is still in argv: {cmd!r}"
        )

    def test_the_heal_preserves_the_template_hooks(self) -> None:
        """Self-heal must not cost the sub-agent its guardrails.

        This is why the cached copy is used and why the dispatch is never
        simply run without ``--settings``: a healed payload missing the
        hooks is a sub-agent with no Edit/Write containment guard and no
        lock broker, running normally and looking fine.
        """
        template = _write_template()
        tool = _build_tool(str(template))
        _run_one_dispatch(tool)
        shutil.rmtree(template.parent)

        payload = json.loads(
            Path(_settings_arg(_run_one_dispatch(tool))).read_text("utf-8")
        )

        hooks = payload.get("hooks", {})
        cmds = [
            h["command"]
            for entries in hooks.values()
            for entry in entries
            for h in entry.get("hooks", [])
        ]
        assert any(_HOOK_MARKER in c for c in cmds), (
            f"the healed settings file lost the template's hooks: "
            f"{payload.get('hooks')!r}"
        )
        assert "PDT_SUBAGENT_UUID" in payload.get("env", {}), (
            "the healed payload was not re-stamped for this sub-agent"
        )


# ---------------------------------------------------------------------------
# 3. Missing template, no cache — name the path, spawn nothing
# ---------------------------------------------------------------------------


class TestMissingTemplateWithoutCache:
    """A fresh instance whose very first read comes up empty."""

    def test_raises_and_names_the_missing_path(self, tmp_path) -> None:
        template = tmp_path / "pdt-subagent-gone" / "subagent_settings_x.json"
        tool = _build_tool(str(template))

        with pytest.raises(SettingsTemplateMissing) as excinfo:
            tool.query("test prompt")

        assert str(template) in str(excinfo.value), (
            f"the error must name the missing path; got "
            f"{str(excinfo.value)!r}"
        )
        assert excinfo.value.path == str(template)

    def test_no_subprocess_is_spawned(self, tmp_path) -> None:
        """The raise happens at assembly time, before any ``claude`` child.

        Before the fix this path produced a real 0.2s subprocess that
        exited non-zero with no assistant text. Nothing should be
        spawned at all now — and in particular no *silent* dispatch that
        dropped ``--settings``.
        """
        template = tmp_path / "pdt-subagent-gone" / "subagent_settings_x.json"
        tool = _build_tool(str(template))

        with patch("coding_tool.subprocess.Popen") as mock_popen:
            with pytest.raises(SettingsTemplateMissing):
                tool.query("test prompt")

        assert mock_popen.call_count == 0, (
            "a dispatch was attempted without a usable settings template"
        )

    def test_it_is_not_an_api_error(self) -> None:
        """The base class is the provider-fallback decision, pinned.

        ``_query_internal_m1``'s fallback is an ``except ApiError``: it
        excludes the provider, PARKS it for a cooldown, and re-dispatches
        with the next one. For a swept local file that is wrong three
        ways — the next provider dies identically (the file is gone for
        the whole process), a healthy provider gets sidelined by a
        cooldown that is a lie, and the real cause never reaches the log.
        So this must fall through to the plain ``except Exception`` arm
        and propagate on the first raise.
        """
        assert not issubclass(SettingsTemplateMissing, ApiError), (
            "an ApiError here would trigger provider rotation: identical "
            "death once per provider, plus a healthy provider parked"
        )
        assert issubclass(SettingsTemplateMissing, RuntimeError)

    def test_a_template_of_json_null_is_rejected(self, tmp_path) -> None:
        """A file whose whole content is ``null`` is not a template.

        ``json.load`` succeeds and returns ``None``, so a naive check on
        "did the read raise?" would treat it as a usable settings file
        and launch a sub-agent with neither hooks nor env.
        """
        null_template = tmp_path / "null_settings.json"
        null_template.write_text("null", encoding="utf-8")
        tool = _build_tool(str(null_template))

        with pytest.raises(SettingsTemplateMissing) as excinfo:
            tool.query("test prompt")

        assert str(null_template) in str(excinfo.value)

    def test_a_readable_null_template_never_becomes_a_cached_object(
        self, tmp_path
    ) -> None:
        """The rejected read must not poison the cache with ``None``."""
        null_template = tmp_path / "null_settings.json"
        null_template.write_text("null", encoding="utf-8")
        tool = _build_tool(str(null_template))

        with pytest.raises(SettingsTemplateMissing):
            tool.query("test prompt")

        assert tool._settings_template_cache is None
