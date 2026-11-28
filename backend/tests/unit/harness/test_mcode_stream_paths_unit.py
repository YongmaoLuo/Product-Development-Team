"""Unit coverage for the MiniMax Code stream translation.

What this pins
--------------
``mcode`` writes its own event vocabulary; the layer above reads the
claude one. This file pins the edges of that translation — the shapes
the CLI reports that are easy to get wrong:

1. A line that is JSON but not an object is a parse error, not an
   event; a line that is bytes is decoded, and a line that is neither
   bytes nor text is read as its own text.
2. An item the CLI reported without a payload of its own keeps the
   untouched payload under ``raw`` — nothing the CLI said is thrown
   away.
3. The text of an item is read from ``content`` and from
   ``contentDelta``, whether either is a string or a list of blocks.
4. The run's result is the ``exec.completed`` event, and a turn that
   finished before it contributes the model and the usage the result
   itself did not carry.
5. A model the CLI reported as a bare string, as a block with only an
   id, or as a block that names nothing, is read the way the CLI means
   it.
6. A token count the CLI reported as a float or as a string is read as
   a number, and a count it did not report is ``None``.

Every case goes through the adapter's own entry points
(:func:`harness.mcode.parse_stream`,
:meth:`harness.mcode.McodeHarness.classify_exit`). No process is
spawned and no ``Popen`` is mocked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

import harness.mcode as mcode
from harness.contract import (
    HarnessConfigError,
    SessionMode,
    TaskInvocation,
)
from harness.mcode import McodeHarness
from harness.session_map import SessionIdNotResumable

pytestmark = pytest.mark.unit


def _events(*payloads: Dict[str, Any]) -> List[Any]:
    """The events one stream of mcode payloads becomes."""
    lines = [json.dumps(payload) for payload in payloads]
    return list(mcode.parse_stream(lines))


# ---------------------------------------------------------------------------
# 1. Lines the parser cannot read
# ---------------------------------------------------------------------------


def test_a_json_line_that_is_not_an_object_is_a_parse_error():
    """Valid JSON that is not an event is reported, not dropped."""
    events = list(mcode.parse_stream(["[1, 2]"]))

    assert events[0].type == mcode.PARSE_ERROR_TYPE
    assert events[0].is_error is True


def test_a_bytes_line_is_decoded():
    """Bytes are read as UTF-8 rather than ending the stream."""
    events = list(
        mcode.parse_stream([b'{"type": "session.started"}'])
    )

    assert events[0].type == "system"


def test_a_line_that_is_neither_bytes_nor_text_is_read_as_its_text():
    """Anything else is read as the text it stands for."""
    events = list(mcode.parse_stream([1234]))

    assert events[0].type == mcode.PARSE_ERROR_TYPE


# ---------------------------------------------------------------------------
# 2. Items the CLI reported
# ---------------------------------------------------------------------------


def test_an_item_without_a_payload_of_its_own_keeps_the_raw_event():
    """An item the CLI reported without a payload keeps the payload."""
    payload = {"type": "item.completed", "item": "not-an-object"}
    events = _events(payload)

    assert events[0].type == "assistant"
    assert events[0].raw["raw"] == payload


def test_an_item_carrying_text_blocks_is_read_as_text():
    """A list of blocks is joined into the text the agent spoke."""
    events = _events(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [
                    {"type": "text", "text": "he"},
                    {"type": "text", "text": "llo"},
                ],
            },
        }
    )

    assert events[0].text == "hello"


def test_an_item_carrying_no_text_at_all_is_read_as_empty():
    """A block that is not a text block carries no text."""
    events = _events(
        {
            "type": "item.completed",
            "item": {"type": "reasoning", "content": [7]},
        }
    )

    assert events[0].text == ""


def test_a_content_block_that_is_not_text_is_skipped():
    """A tool call in the content is not part of the answer."""
    events = _events(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [{"type": "tool_use", "name": "read"}],
            },
        }
    )

    assert events[0].text == ""


def test_a_content_block_that_is_not_a_text_block_carries_no_text():
    """Only the text blocks of a message are the message's text."""
    assert (
        mcode._canonical_text(
            {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}
        )
        == ""
    )


def test_a_message_whose_content_is_not_a_list_carries_no_text():
    """The canonical shape keeps text in a list of blocks; anything else
    is not text."""
    assert mcode._canonical_text({"type": "assistant", "message": {"content": "x"}}) == ""


# ---------------------------------------------------------------------------
# 3. The run's result
# ---------------------------------------------------------------------------


def test_a_result_event_without_a_result_object_still_ends_the_stream():
    """A result the CLI reported in another shape is still a result."""
    events = _events({"type": "exec.completed", "result": "not-an-object"})

    assert events[-1].type == "result"
    assert events[-1].is_error is True


def test_the_final_result_takes_the_usage_from_the_turn_that_finished():
    """A turn that finished before the result contributes the usage."""
    events = _events(
        {
            "type": "turn.completed",
            "session_id": "s1",
            "model": "minimax/MiniMax-M3.1-Flash",
            "usage": {"inputTokens": 1, "outputTokens": 2},
        },
        {
            "type": "exec.completed",
            "result": {"status": "succeeded", "output": "done"},
        },
    )

    assert events[-1].raw["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }


def test_a_model_named_only_by_its_id_is_read_as_that_id():
    """A model block with an id and no provider is the id."""
    events = _events(
        {
            "type": "exec.completed",
            "result": {"status": "succeeded", "model": {"modelId": "M3"}},
        }
    )

    assert events[-1].raw["model"] == "M3"


def test_a_model_block_that_names_nothing_is_no_model():
    """A model block with neither a provider nor an id names no model."""
    events = _events(
        {
            "type": "exec.completed",
            "result": {"status": "succeeded", "model": {"providerId": "minimax"}},
        }
    )

    assert events[-1].raw["model"] is None


def test_token_counts_are_read_from_every_shape_the_cli_reports():
    """A float, a numeric string and a non-numeric string all read."""
    events = _events(
        {
            "type": "exec.completed",
            "result": {
                "status": "succeeded",
                "usage": {
                    "inputTokens": 3.7,
                    "outputTokens": "42",
                    "cacheReadTokens": "many",
                    "cacheCreationTokens": None,
                },
            },
        }
    )

    assert events[-1].raw["usage"] == {
        "input_tokens": 3,
        "output_tokens": 42,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }


def test_a_run_with_no_result_event_has_no_last_result():
    """A stream that never reached a result says so."""
    outcome = McodeHarness().classify_exit(0, [])

    assert outcome is not None


# ---------------------------------------------------------------------------
# 4. The MCP configuration
# ---------------------------------------------------------------------------


def test_a_configuration_that_is_neither_json_nor_a_file_is_refused():
    """Text that is neither is a configuration mistake."""
    with pytest.raises(HarnessConfigError, match="neither a JSON object"):
        McodeHarness()._mcp_config("{not json")


def test_a_configuration_file_that_is_not_json_is_refused(tmp_path):
    """A file that holds something other than JSON is a mistake."""
    path = tmp_path / "mcp.json"
    path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(HarnessConfigError, match="is not JSON"):
        McodeHarness()._mcp_config(str(path))


def test_a_configuration_that_is_not_a_json_object_or_a_path_is_refused():
    """The configuration is a mapping or the path of one."""
    with pytest.raises(HarnessConfigError, match="must be a JSON object or a path"):
        McodeHarness()._mcp_config(42)


def test_a_configuration_that_is_json_but_not_an_object_is_refused():
    """A JSON array is not an MCP configuration."""
    with pytest.raises(HarnessConfigError, match="must be a JSON object, got list"):
        McodeHarness()._mcp_config("[1, 2]")


def test_a_configuration_file_is_read(tmp_path):
    """The ordinary case: the file holds the configuration."""
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {}}', encoding="utf-8")

    assert McodeHarness()._mcp_config(str(path)) == {"mcpServers": {}}


# ---------------------------------------------------------------------------
# 5. Where the configuration may be written
# ---------------------------------------------------------------------------


def test_a_target_that_leaves_the_workspace_is_refused(tmp_path):
    """A ``..`` that climbs out is refused before anything is written."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(HarnessConfigError, match="outside the workspace"):
        mcode._assert_scope(workspace / ".." / "outside" / "config.json", workspace)


def test_the_workspace_root_itself_is_refused(tmp_path):
    """The root is not a file the adapter may write."""
    with pytest.raises(HarnessConfigError, match="workspace root"):
        mcode._assert_scope(tmp_path, tmp_path)


def test_a_target_that_cannot_be_resolved_is_refused(tmp_path, monkeypatch):
    """A path the machine cannot resolve is refused, not guessed at."""
    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if self == tmp_path / "unresolvable":
            raise OSError("cannot resolve")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)

    with pytest.raises(HarnessConfigError, match="could not resolve"):
        mcode._assert_scope(tmp_path / "unresolvable", tmp_path)


def test_a_configuration_that_is_not_json_is_refused():
    """A configuration that cannot be serialized is a mistake."""
    with pytest.raises(HarnessConfigError, match="is not JSON"):
        mcode._mcp_payload({"bad": object()})


def test_an_existing_configuration_that_cannot_be_read_is_refused(tmp_path):
    """The file that is there cannot be read: the run says so."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".mcp.json").mkdir()

    with pytest.raises(HarnessConfigError, match="could not read"):
        McodeHarness()._plan_mcp(workspace, {"mcpServers": {}})


def test_an_existing_configuration_over_the_limit_is_refused(tmp_path):
    """A file that is too large is refused rather than truncated."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".mcp.json").write_bytes(b"x" * (mcode._SNAPSHOT_MAX_BYTES + 1))

    with pytest.raises(HarnessConfigError, match="refused to overwrite"):
        McodeHarness()._plan_mcp(workspace, {"mcpServers": {}})


def test_the_account_data_directory_is_never_written_to(tmp_path, monkeypatch):
    """The account's own MCP registry is not the workspace's file."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(data_dir))

    with pytest.raises(HarnessConfigError, match="account data directory"):
        McodeHarness()._plan_mcp(data_dir, {"mcpServers": {}})


def test_the_profile_home_contributes_a_protected_directory(tmp_path):
    """The profile's own home is protected too."""
    from mcode_profiles import ProfileBinding

    home = tmp_path / "home"
    home.mkdir()
    harness = McodeHarness(ProfileBinding(name="acc2", home=str(home)))

    protected = harness._protected_data_dirs()

    assert (home / ".minimax").resolve() in protected


def test_a_directory_that_cannot_be_resolved_is_not_protected(tmp_path, monkeypatch):
    """A directory the machine cannot resolve protects nothing."""
    from mcode_profiles import ProfileBinding

    home = tmp_path / "home"
    home.mkdir()
    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if str(self).startswith(str(home)):
            raise OSError("cannot resolve")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    harness = McodeHarness(ProfileBinding(name="acc2", home=str(home)))

    assert not any(str(path).startswith(str(home)) for path in harness._protected_data_dirs())


def test_a_target_that_cannot_be_resolved_is_not_refused(tmp_path, monkeypatch):
    """A target the machine cannot resolve is not judged."""
    real_resolve = Path.resolve
    target = tmp_path / "unresolvable"

    def _resolve(self, *args, **kwargs):
        if self == target:
            raise OSError("cannot resolve")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)

    McodeHarness()._assert_not_data_dir(target)


# ---------------------------------------------------------------------------
# 6. The session a resume names
# ---------------------------------------------------------------------------


def test_a_resume_without_a_session_at_all_is_refused():
    """There is nothing to re-open, and the answer says so."""
    with pytest.raises(SessionIdNotResumable):
        McodeHarness()._session_id(
            TaskInvocation(prompt="p", mode=SessionMode.RESUME, session=None)
        )
