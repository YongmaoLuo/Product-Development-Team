"""TDD spec for the mcode ``.mcp.json`` lifecycle.

What is pinned here
-------------------

``mcode exec`` has no ``--mcp-config`` flag — that is one of the five
claude-only flags the adapter refuses to emit. The only place mcode
reads MCP servers from is a ``.mcp.json`` in the workspace root, so the
adapter has to write that file itself and then take the workspace back
to exactly the state it found it in.

A file the adapter writes into somebody else's working tree is a file
that can end up in a commit, in a diff, or in a diff of a diff. The
rules pinned here are therefore:

1. what was there before comes back byte for byte — content *and*
   mode — when the run ends;
2. what was not there before is gone again;
3. the file never leaves the workspace: a symlink that points out of
   it, or a ``..`` that climbs out of it, is refused before a single
   byte is written;
4. the account's own data directory — where mcode keeps the user's
   persistent MCP registry, ``<dataDir>/mcp.json`` — is never written
   to;
5. a configuration larger than :data:`_SNAPSHOT_MAX_BYTES` is refused
   outright, never truncated;
6. ``${VAR}`` stays a literal. The configuration is handed over as it
   was given; whatever a shell would have expanded is mcode's business,
   not the adapter's.

The last two are the ones that are easy to get wrong quietly: a
truncated configuration is a configuration that means something else,
and an expanded ``${VAR}`` is a secret in a file the adapter was only
supposed to be passing along.

Nothing is spawned and no ``Popen`` is mocked (test decision point 2):
the observable is the file on disk, before and after.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from harness.contract import HarnessConfigError, TaskInvocation
from harness.mcode import McodeHarness, _SNAPSHOT_MAX_BYTES, _assert_scope

pytestmark = pytest.mark.unit


#: The configuration the lifecycle is exercised with. ``${SENTINEL}`` is
#: a variable reference that has to survive the round trip as a literal.
CONFIG = {"mcpServers": {"x": {"url": "${SENTINEL}"}}}

#: The bytes that configuration is written as: one line of JSON, with
#: the spacing ``json.dumps`` uses by default.
CONFIG_BYTES = b'{"mcpServers": {"x": {"url": "${SENTINEL}"}}}'


def _workspace(tmp_path: Path) -> Path:
    """An empty workspace directory to write into."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_snapshot_restores_bytes_and_mode(tmp_path):
    """An existing file comes back with its bytes and its mode."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"
    original = b'{"mcpServers": {"old": {}}}\n'
    target.write_bytes(original)
    os.chmod(target, 0o600)

    harness = McodeHarness()
    with harness.mcp_prepare(workspace, CONFIG):
        # The adapter's own configuration is in place while the run
        # lasts, and it is not the file that was there before.
        assert target.exists()
        assert target.read_bytes() != original

    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_removes_when_absent_before(tmp_path):
    """A file that was not there before is not there afterwards."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"

    harness = McodeHarness()
    with harness.mcp_prepare(workspace, CONFIG):
        assert target.exists()
        assert target.read_bytes() == CONFIG_BYTES

    assert not target.exists()


def test_symlink_escape_rejected(tmp_path):
    """A symlink out of the workspace is refused, and nothing is written."""
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.json"
    victim.write_text("untouched")
    (workspace / ".mcp.json").symlink_to(victim)

    harness = McodeHarness()
    with pytest.raises(HarnessConfigError):
        with harness.mcp_prepare(workspace, CONFIG):
            pass

    assert victim.read_text() == "untouched"


def test_oversize_rejected(tmp_path):
    """A configuration over the limit is refused, not truncated."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"
    config = {"mcpServers": {"x": {"url": "x" * _SNAPSHOT_MAX_BYTES}}}

    harness = McodeHarness()
    with pytest.raises(HarnessConfigError):
        with harness.mcp_prepare(workspace, config):
            pass

    assert not target.exists()


def test_vars_not_expanded(tmp_path, monkeypatch):
    """``${VAR}`` is written as it was given, whatever the environment says."""
    monkeypatch.setenv("SENTINEL", "expanded-value")
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"

    harness = McodeHarness()
    with harness.mcp_prepare(workspace, CONFIG):
        content = target.read_text()

    assert content == CONFIG_BYTES.decode("utf-8")
    assert "${SENTINEL}" in content
    assert "expanded-value" not in content
    assert json.loads(content) == CONFIG


# ---------------------------------------------------------------------------
# The remaining boundary conditions
# ---------------------------------------------------------------------------


def test_config_given_as_text(tmp_path):
    """A configuration that arrives as text is read as one."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"

    harness = McodeHarness()
    with harness.mcp_prepare(workspace, json.dumps(CONFIG)):
        assert target.read_bytes() == CONFIG_BYTES

    assert not target.exists()


def test_dotdot_escape_rejected(tmp_path):
    """``..`` that climbs out of the workspace is refused."""
    workspace = _workspace(tmp_path)

    with pytest.raises(HarnessConfigError):
        _assert_scope(workspace / ".." / "outside" / "config.json", workspace)


def test_scope_accepts_a_file_inside_the_workspace(tmp_path):
    """A file inside the workspace is not an escape."""
    workspace = _workspace(tmp_path)

    _assert_scope(workspace / ".mcp.json", workspace)


def test_data_dir_mcp_json_rejected(tmp_path, monkeypatch):
    """The account's own data directory is never written to."""
    data_dir = tmp_path / "minimax"
    data_dir.mkdir()
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(data_dir))

    harness = McodeHarness()
    with pytest.raises(HarnessConfigError):
        harness.mcp_prepare(data_dir, CONFIG)

    assert not (data_dir / "mcp.json").exists()
    assert not (data_dir / ".mcp.json").exists()


def test_restores_after_an_exception(tmp_path):
    """An exception in the run still restores the file."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"
    original = b'{"mcpServers": {}}\n'
    target.write_bytes(original)

    harness = McodeHarness()
    with pytest.raises(RuntimeError):
        with harness.mcp_prepare(workspace, CONFIG):
            raise RuntimeError("the run failed")

    assert target.read_bytes() == original


def test_restores_after_a_cancellation(tmp_path):
    """A cancellation (a ``BaseException``) still restores the file."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"

    harness = McodeHarness()
    with pytest.raises(KeyboardInterrupt):
        with harness.mcp_prepare(workspace, CONFIG):
            raise KeyboardInterrupt

    assert not target.exists()


# ---------------------------------------------------------------------------
# The wiring into the invocation
# ---------------------------------------------------------------------------


def test_build_invocation_attaches_prepare_for_an_mcp_config(tmp_path):
    """A request that carries MCP servers gets a prepare hook."""
    workspace = _workspace(tmp_path)
    req = TaskInvocation(
        prompt="p",
        mcp_config=json.dumps(CONFIG),
        cwd=str(workspace),
    )

    invocation = McodeHarness().build_invocation(req)

    assert invocation.prepare is not None
    with invocation.prepare():
        assert (workspace / ".mcp.json").exists()
    assert not (workspace / ".mcp.json").exists()


def test_build_invocation_without_mcp_config_has_no_prepare(tmp_path):
    """A request without MCP servers gets no prepare hook."""
    workspace = _workspace(tmp_path)
    req = TaskInvocation(prompt="p", cwd=str(workspace))

    invocation = McodeHarness().build_invocation(req)

    assert invocation.prepare is None


def test_argv_carries_no_mcp_flag(tmp_path):
    """The configuration travels through the workspace, never as a flag."""
    workspace = _workspace(tmp_path)
    req = TaskInvocation(
        prompt="p",
        mcp_config=json.dumps(CONFIG),
        cwd=str(workspace),
    )

    invocation = McodeHarness().build_invocation(req)

    assert "--mcp-config" not in invocation.argv
    assert "mcpServers" not in " ".join(invocation.argv)


def test_mcp_config_given_as_a_path(tmp_path):
    """A configuration that names a file is read from it."""
    workspace = _workspace(tmp_path)
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps(CONFIG))

    req = TaskInvocation(
        prompt="p",
        mcp_config=str(config_file),
        cwd=str(workspace),
    )

    invocation = McodeHarness().build_invocation(req)

    assert invocation.prepare is not None
    with invocation.prepare():
        assert (workspace / ".mcp.json").read_bytes() == CONFIG_BYTES


def test_mcp_config_path_is_honoured(tmp_path):
    """The path the request names is the configuration that is used."""
    workspace = _workspace(tmp_path)
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps(CONFIG))

    req = TaskInvocation(
        prompt="p",
        mcp_config_path=str(config_file),
        cwd=str(workspace),
    )

    invocation = McodeHarness().build_invocation(req)

    assert invocation.prepare is not None
    with invocation.prepare():
        assert (workspace / ".mcp.json").read_bytes() == CONFIG_BYTES


def test_symlink_inside_the_workspace_is_kept(tmp_path):
    """A symlink that stays inside the workspace survives the run."""
    workspace = _workspace(tmp_path)
    real = workspace / "real-mcp.json"
    real.write_bytes(b'{"mcpServers": {"old": {}}}\n')
    link = workspace / ".mcp.json"
    link.symlink_to(real)

    harness = McodeHarness()
    with harness.mcp_prepare(workspace, CONFIG):
        assert link.is_symlink()
        assert real.read_bytes() == CONFIG_BYTES

    assert link.is_symlink()
    assert real.read_bytes() == b'{"mcpServers": {"old": {}}}\n'


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root may write a read-only file",
)
def test_read_only_file_is_left_alone(tmp_path):
    """A file the adapter cannot write is refused, not forced open."""
    workspace = _workspace(tmp_path)
    target = workspace / ".mcp.json"
    original = b'{"mcpServers": {"old": {}}}\n'
    target.write_bytes(original)
    os.chmod(target, 0o400)

    harness = McodeHarness()
    with pytest.raises(HarnessConfigError):
        with harness.mcp_prepare(workspace, CONFIG):
            pass

    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
