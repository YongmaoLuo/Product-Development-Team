"""Unit tests for :mod:`utils.secret_files` (2026-09-27).

The module exists because the subagent ``--settings`` payload carries the
routed provider's credentials and used to be written to a world-readable
``/tmp/subagent_settings_<uuid>.json`` that was never removed. Because
nothing deleted them, the population grew with every dispatch: each file
a ``0644`` copy of a live provider credential, readable by any account on
the box.

Two properties are easy to get wrong and are pinned hardest here:

* the **file** mode, not just the directory mode — ``mkdtemp`` sets the
  directory to ``0700`` and a file created inside is still ``0644``
  under the default umask, so the file mode must be forced explicitly;
* :func:`redact` must **refuse** paths outside our own temp directories,
  because ``coding_tool`` will hand it ``self.settings``, which in some
  configurations is the operator's own ``~/.claude/settings.json``.
  Rewriting that in place would destroy the user's configuration — a
  worse outcome than the leak this module closes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from utils.secret_files import (  # noqa: E402
    DIR_PREFIX,
    PRIVATE_ROOT_ENV_VAR,
    REDACTED,
    SENSITIVE_ENV_KEYS,
    is_managed,
    private_dir,
    redact,
    redact_all,
    write_private_json,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _payload(**env_over):
    env = {
        "ANTHROPIC_BASE_URL": "https://api.vendor-a.example/anthropic",
        "ANTHROPIC_API_KEY": "sk-live-do-not-leak",
        "ANTHROPIC_AUTH_TOKEN": "sk-live-do-not-leak",
        "ANTHROPIC_MODEL": "vendor-a-model",
        "PDT_SUBAGENT_UUID": "abc123",
    }
    env.update(env_over)
    return {"env": env, "hooks": {"PreToolUse": []}}


# ---------------------------------------------------------------------------
# The directory layer
# ---------------------------------------------------------------------------


def test_private_dir_is_0700_under_the_temp_root_and_not_in_the_workspace():
    d = private_dir()
    try:
        assert _mode(d) == 0o700, (
            f"private_dir must be 0700 so other local accounts cannot "
            f"traverse it; got {oct(_mode(d))}"
        )
        assert d.name.startswith(DIR_PREFIX), (
            "the directory name is how `is_managed` recognises our own "
            "files, and how an operator greps for the residue"
        )
        # Not inside the repo: these are per-dispatch artefacts, and a
        # file under the project would pollute the delivered tree.
        assert not str(d.resolve()).startswith(str(Path.cwd().resolve()))
    finally:
        d.rmdir()


def test_private_dir_returns_a_fresh_directory_each_call():
    a, b = private_dir(), private_dir()
    try:
        assert a != b, (
            "two dispatches sharing one directory would let a crashed "
            "run's files be mistaken for a live one's"
        )
    finally:
        a.rmdir()
        b.rmdir()


def test_private_dir_defaults_to_the_system_temp_root(monkeypatch):
    """The unset case must stay exactly what it was before the override.

    A test suite points the root somewhere it owns; production does not,
    and a default that quietly moved would put dispatches' credential
    payloads wherever the new value happened to point.
    """
    monkeypatch.delenv(PRIVATE_ROOT_ENV_VAR, raising=False)
    d = private_dir()
    try:
        expected = Path(tempfile.gettempdir()).resolve()
        assert d.parent.resolve() == expected, (
            f"with no override the directory must be minted directly in "
            f"{expected}, not {d.parent}"
        )
    finally:
        d.rmdir()


def test_private_dir_honours_the_root_override_and_stays_managed(
    tmp_path: Path, monkeypatch
):
    """The override moves the directory without loosening the guard.

    ``is_managed`` keys on the directory *name*, so relocating the root
    must not cost the payload its eligibility for redaction — a redirect
    that silently made the files unmanaged would turn "clean up after the
    child" into a no-op.
    """
    root = tmp_path / "redirected"
    monkeypatch.setenv(PRIVATE_ROOT_ENV_VAR, str(root))
    d = private_dir()
    try:
        assert d.parent == root, (
            f"the override root was ignored: {d} is not under {root}"
        )
        assert _mode(d) == 0o700
        assert is_managed(d / "payload.json"), (
            "a redirected directory is still one of ours; if the name "
            "check stopped recognising it the credentials would never be "
            "redacted"
        )
        payload = d / "payload.json"
        write_private_json(payload, _payload())
        assert redact(payload) is True, "the redirected payload was not eligible"
        written = json.loads(payload.read_text(encoding="utf-8"))
        for key in SENSITIVE_ENV_KEYS:
            assert written["env"][key] == REDACTED, (
                f"{key} survived redaction in a redirected directory"
            )
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


def test_private_dir_creates_a_missing_override_root(tmp_path: Path, monkeypatch):
    """The override may name a root that does not exist yet.

    ``mkdtemp(dir=...)`` fails on a missing parent, so the module has to
    create it — and create it private, since it is about to hold
    credential payloads.
    """
    root = tmp_path / "not" / "created" / "yet"
    monkeypatch.setenv(PRIVATE_ROOT_ENV_VAR, str(root))
    d = private_dir()
    try:
        assert root.is_dir(), "the override root was not created"
        assert _mode(root) == 0o700, (
            f"the created root must be traversable only by its owner; got "
            f"{oct(_mode(root))}"
        )
        assert d.parent == root
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(tmp_path / "not", ignore_errors=True)


def test_the_suite_redirects_the_private_root():
    """Asserted from a test rather than trusted to conftest.

    Every dispatch mints one ``pdt-subagent-*`` directory, and the suite
    dispatches constantly. Without the redirect they land in the machine's
    temp root where nothing collects them — the boot sweep is off under
    test and they hold a payload, so the empty-directory prune cannot
    reach them either. Deleting the line would silently reopen that.
    """
    assert os.environ.get(PRIVATE_ROOT_ENV_VAR), (
        "the suite is minting credential payload directories under the "
        "machine's temp root; every dispatch would leave one behind"
    )


# ---------------------------------------------------------------------------
# The file layer — the one mkdtemp does NOT cover
# ---------------------------------------------------------------------------


def test_written_file_is_0600_despite_a_permissive_umask():
    """``mkdtemp`` sets the directory mode and nothing else.

    A file created inside a 0700 directory is still 0644 under the
    default umask, so the leak survived the directory being private. The
    umask is deliberately widened here to prove the mode is *forced*
    rather than inherited.
    """
    d = private_dir()
    old_umask = os.umask(0o000)
    try:
        target = d / "settings.json"
        write_private_json(target, _payload())

        assert _mode(target) == 0o600, (
            f"settings file mode is {oct(_mode(target))}; the payload "
            f"carries {list(SENSITIVE_ENV_KEYS)}"
        )
        # The directory is the second layer, and must still be private.
        assert _mode(d) == 0o700
    finally:
        os.umask(old_umask)
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_write_is_atomic_and_leaves_no_tmpfile():
    d = private_dir()
    try:
        target = d / "settings.json"
        write_private_json(target, _payload())
        assert target.exists()
        assert not (d / "settings.json.tmp").exists(), (
            "a leftover .tmp carries the same credentials and would be "
            "missed by anything that globs for the final name"
        )
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_write_creates_missing_parents():
    d = private_dir()
    try:
        target = d / "nested" / "settings.json"
        write_private_json(target, {"env": {}})
        assert target.exists()
    finally:
        import shutil
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# The redaction layer
# ---------------------------------------------------------------------------


def test_redact_replaces_only_the_credentials():
    """Routing config stays — it is the point of the post-mortem copy."""
    d = private_dir()
    try:
        target = d / "settings.json"
        write_private_json(target, _payload())

        assert redact(target) is True

        doc = json.loads(target.read_text(encoding="utf-8"))
        for key in SENSITIVE_ENV_KEYS:
            assert doc["env"][key] == REDACTED, (
                f"{key} survived redaction — this is the whole leak"
            )
        # Not secrets, and the most useful thing in the file when
        # debugging a routing mistake.
        assert doc["env"]["ANTHROPIC_BASE_URL"].startswith("https://")
        assert doc["env"]["ANTHROPIC_MODEL"] == "vendor-a-model"
        assert doc["env"]["PDT_SUBAGENT_UUID"] == "abc123"
        # Structure preserved so an operator can still read the hooks.
        assert doc["hooks"] == {"PreToolUse": []}
        # And the file stayed private through the rewrite.
        assert _mode(target) == 0o600
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_redact_is_idempotent():
    d = private_dir()
    try:
        target = d / "settings.json"
        write_private_json(target, _payload())
        assert redact(target) is True
        assert redact(target) is False, (
            "a second pass has nothing left to replace; reporting True "
            "would make the caller's rewritten-count meaningless"
        )
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_redact_refuses_a_path_outside_our_own_directories(tmp_path: Path):
    """The safety guard, and the only test here that protects the user.

    ``coding_tool`` calls ``redact_all([effective_settings_path,
    self.settings])``. ``self.settings`` is whatever the caller passed,
    and in some configurations that is the operator's own
    ``~/.claude/settings.json``. Redacting it would overwrite the user's
    provider key with ``<redacted>`` and break their Claude Code install
    — strictly worse than the leak.
    """
    outsider = tmp_path / "settings.json"
    write_private_json(outsider, _payload())
    before = outsider.read_text(encoding="utf-8")

    assert is_managed(outsider) is False
    assert redact(outsider) is False
    assert outsider.read_text(encoding="utf-8") == before, (
        "redact rewrote a file that is not ours; a caller passing the "
        "operator's own settings.json would have it destroyed"
    )


def test_is_managed_recognises_files_in_a_private_dir():
    d = private_dir()
    try:
        target = d / "settings.json"
        write_private_json(target, _payload())
        assert is_managed(target) is True
        assert is_managed(None) is False
        assert is_managed(Path("/tmp/settings.json")) is False
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_redact_tolerates_missing_and_malformed_files(tmp_path: Path):
    """A finished task must not fail because cleanup could not read."""
    d = private_dir()
    try:
        assert redact(d / "never-written.json") is False

        broken = d / "settings.json"
        broken.write_text("{not json", encoding="utf-8")
        assert redact(broken) is False, (
            "an unparseable file leaves the credential in place; the "
            "0600/0700 layers are the protection on that path"
        )
        # The guard must not have destroyed it either.
        assert broken.read_text(encoding="utf-8") == "{not json"
    finally:
        import shutil
        shutil.rmtree(d)


def test_redact_all_collapses_duplicates_and_counts_rewrites():
    d = private_dir()
    try:
        a = d / "a.json"
        b = d / "b.json"
        write_private_json(a, _payload())
        write_private_json(b, _payload())

        assert redact_all([a, b, a, None]) == 2, (
            "the two writers can hand the same path twice; it must not "
            "be counted — or rewritten — twice"
        )
    finally:
        import shutil
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# The writer that actually ships in the dispatch path
# ---------------------------------------------------------------------------


def test_subagent_config_writes_into_a_private_dir(tmp_path: Path):
    """End-to-end on the real writer, not on this module's own helpers.

    ``SubagentConfig.write_tmp_settings`` is the file ``--settings``
    points at, so it is the one whose mode decides whether the routed
    provider key is readable by every account on the box.
    """
    from subagent_config import SubagentConfig

    cfg = SubagentConfig(
        provider_name="vendor-a",
        api_key="sk-live-do-not-leak",
        auth_token="sk-live-do-not-leak",
        base_url="https://api.vendor-a.example/anthropic",
        model_env={"ANTHROPIC_MODEL": "vendor-a-model"},
    )
    path = cfg.write_tmp_settings()
    try:
        assert _mode(path) == 0o600, (
            f"write_tmp_settings produced mode {oct(_mode(path))}; this is "
            f"the file the credential leak was measured on"
        )
        assert _mode(path.parent) == 0o700
        assert is_managed(path), "not in a directory this run created"
        assert "sk-live-do-not-leak" in path.read_text(encoding="utf-8"), (
            "sanity: the credential must actually be in the file for the "
            "mode assertions above to mean anything"
        )
    finally:
        import shutil
        shutil.rmtree(path.parent, ignore_errors=True)
