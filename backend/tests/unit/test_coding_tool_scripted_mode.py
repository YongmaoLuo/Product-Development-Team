"""The scripted LLM double: ``PDT_LLM_MODE=scripted``.

Everything in an end-to-end run is real — git, the filesystem, SQLite,
FastAPI — except the LLM, whose non-determinism is the one thing that
makes such a run unreproducible.  This module pins the seam where the
LLM is replaced by a file on disk:

* ``PDT_LLM_MODE=scripted`` switches the double on.  Unset means the
  existing behaviour, unchanged.
* ``PDT_LLM_SCRIPT_DIR`` names the directory holding ``registry.json``
  (which prompt answers under which key) and one ``<key>.txt`` per
  registered prompt.
* A prompt nobody registered raises.  It never answers with an empty
  string, and it never falls back to a real provider.

The four tests below are the contract; the fifth is the negative
control that says the double really is a dead end for the network.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as ct
from coding_tool import ClaudeCodingTool, create_coding_tool


PLAN_PROMPT = "生成验证计划书"
PLAN_ANSWER = "这是脚本化的验证计划书"


def _write_script_dir(tmp_path, entries, responses):
    """Write a script directory: ``registry.json`` + one ``<key>.txt``.

    ``entries`` maps a prompt key to the text that identifies the
    prompt; ``responses`` maps the same key to the canned answer.
    """
    script_dir = tmp_path / "responses"
    script_dir.mkdir()
    (script_dir / "registry.json").write_text(
        json.dumps({"prompts": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    for key, text in responses.items():
        (script_dir / f"{key}.txt").write_text(text, encoding="utf-8")
    return script_dir


@pytest.fixture
def script_dir(tmp_path, monkeypatch):
    """A script directory with one registered prompt, switched on."""
    directory = _write_script_dir(
        tmp_path,
        entries={"verification_plan": PLAN_PROMPT},
        responses={"verification_plan": PLAN_ANSWER},
    )
    monkeypatch.setenv("PDT_LLM_MODE", "scripted")
    monkeypatch.setenv("PDT_LLM_SCRIPT_DIR", str(directory))
    return directory


def test_scripted_mode_returns_registered_response(script_dir):
    """A registered prompt answers with its fixture, verbatim."""
    tool = create_coding_tool()
    assert isinstance(tool, ct.ScriptedCodingTool)

    answer = tool.query(f"{PLAN_PROMPT}：第 3 层")
    assert answer == PLAN_ANSWER


def test_unregistered_prompt_fails_loudly(script_dir):
    """A prompt nobody registered raises instead of answering empty."""
    tool = create_coding_tool()

    with pytest.raises(RuntimeError) as excinfo:
        tool.query("一个没有登记的 prompt")

    message = str(excinfo.value)
    assert message, "the failure must say something"
    assert "一个没有登记的 prompt" in message


def test_default_mode_unchanged(monkeypatch):
    """Without the switch, the old implementation is the one that runs."""
    monkeypatch.delenv("PDT_LLM_MODE", raising=False)
    monkeypatch.delenv("PDT_LLM_SCRIPT_DIR", raising=False)

    tool = create_coding_tool()
    assert isinstance(tool, ClaudeCodingTool)
    assert not isinstance(tool, ct.ScriptedCodingTool)

    # The original dispatch is still what answers.
    monkeypatch.setattr(
        ClaudeCodingTool,
        "_query_internal_m1",
        lambda self, *args, **kwargs: "REAL-LLM-ANSWER",
    )
    assert tool.query("随便什么 prompt") == "REAL-LLM-ANSWER"


def test_missing_script_dir_raises(tmp_path, monkeypatch):
    """A script directory that is not there is a startup failure."""
    missing = tmp_path / "no-such-dir"
    monkeypatch.setenv("PDT_LLM_MODE", "scripted")
    monkeypatch.setenv("PDT_LLM_SCRIPT_DIR", str(missing))

    with pytest.raises(RuntimeError):
        create_coding_tool()

    with pytest.raises(RuntimeError):
        ct.ScriptedCodingTool(script_dir=str(missing))


def test_scripted_mode_never_reaches_the_cli(script_dir, monkeypatch):
    """The double answers every call; the CLI is never dispatched."""
    def _must_not_run(*args, **kwargs):
        raise AssertionError("the scripted double must not reach the CLI")

    monkeypatch.setattr(ClaudeCodingTool, "_query_internal_m1", _must_not_run)
    monkeypatch.setattr(ClaudeCodingTool, "_query_json_internal", _must_not_run)

    # The factory's tool ...
    tool = create_coding_tool()
    assert tool.query(PLAN_PROMPT) == PLAN_ANSWER

    # ... and a tool built directly, which is how most of the codebase
    # builds one.
    direct = ClaudeCodingTool()
    assert direct.query(PLAN_PROMPT) == PLAN_ANSWER
